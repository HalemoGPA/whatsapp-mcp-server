"""Server-side tool search + dispatch (progressive disclosure / RAG-over-tools).

The full ~90-tool set is a fixed ~20k-token tax: MCP tool definitions are
injected into the model's context on EVERY request whether or not WhatsApp is
touched. The enterprise fix (Anthropic "Tool Search Tool", Nov 2025; the
progressive-disclosure pattern) is to load only a small hot core directly and
keep the long tail reachable through two meta-tools:

    find_tool(query)            -> ranked catalog entries (name, description, params)
    call_tool(name, arguments)  -> dispatch to ANY tool in the full library

So the always-on cost stays at the core (~8k) while every capability remains one
search away - no capability removed, no mode to flip. This is complementary to a
client that already defers tools (Claude Code v2.1.7+ auto-defers when a server's
tools exceed ~10% of context): when the client defers, these meta-tools are just
two more cheap entries; when it doesn't, they are how the long tail stays usable
under minimal mode.

Security parity: call_tool dispatches INSIDE the process, past the MCP
on_call_tool middleware chain, so it must re-apply what that chain would have
done - per-tool scope enforcement (observability.AuthzMiddleware) and audit
logging of mutating calls (observability.ObservabilityMiddleware). Both are
mirrored below so call_tool can never become a scope-bypass or an audit hole.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from typing import Any

log = logging.getLogger("whatsapp_mcp.toolsearch")

# name -> Tool object, for the FULL library (captured before any pruning).
_LIBRARY: dict[str, Any] = {}
# Parallel search index: list of (name, haystack_lower, description, signature).
# Each row: (name, name_l, name_parts:set, hay_stems:set, description, signature)
_INDEX: list[tuple] = []
# Inverse document frequency per stemmed term (rare terms discriminate more).
_IDF: dict[str, float] = {}
# Optional semantic layer: name -> unit-normalised embedding vector. Empty unless
# an embedding provider is configured (see embeddings.py); when present, find_tool
# blends cosine similarity with the lexical score (hybrid retrieval).
_TOOL_VECS: dict[str, list[float]] = {}
_EMBED_READY = False
# Small in-process cache of query-embedding vectors (a find_tool call and the
# eval both re-query the same string; embed it once).
_QVEC_CACHE: dict[str, list[float] | None] = {}
# Semantic weight in the hybrid blend (lexical weight = 1 - this). Env-tunable.
_EMBED_WEIGHT = float(os.environ.get("WHATSAPP_MCP_EMBED_WEIGHT", "0.6"))
_EMBED_CACHE_PATH = os.environ.get("WHATSAPP_MCP_EMBED_CACHE", "/var/log/wamcp/tool_embeddings.json")
# Meta-tools are never themselves dispatchable via call_tool (no recursion).
_META_NAMES = {"find_tool", "call_tool"}

# Queries that mean "show me everything" -> full catalog browse.
_BROWSE_TOKENS = frozenset({"", "*", "all", "list", "everything", "catalog", "any", "browse", "tools"})
# Below this top score, matches are treated as low-confidence and find_tool
# actively points the model at the full-catalog browse. Calibrated to ~one
# generic (low-IDF) term hit; a distinctive name-part match scores well above it.
_WEAK_SCORE = 6.0

_TOKEN_RE = re.compile(r"[^a-z0-9]+")

# Synonym expansion for lexical recall. find_tool ranks by keyword overlap, so a
# tool whose NAME/description shares no vocabulary with how a user phrases the
# task is invisible - e.g. "send this tomorrow morning" never lexically matches
# `schedule_message`. True semantic retrieval would use embeddings, but this box
# has no GPU and forbids local ML models (see CLAUDE.md), so we expand the search
# haystack with hand-curated natural-language synonyms instead. Only long-tail /
# semantically-named tools need entries; lexically-obvious names (send_message,
# list_chats) already match. Keywords are general phrasings a user would really
# use, not copies of any eval query.
_ALIASES: dict[str, str] = {
    "schedule_message": "later tomorrow tonight morning evening delay defer timed at a specific time send at in an hour remind postpone queue",
    "cancel_scheduled": "unschedule cancel a scheduled pending queued remove upcoming",
    "list_scheduled": "upcoming pending queued scheduled outbox what will be sent",
    "get_top_active_chats": "busiest most active most messages talkative frequent who i talk to most",
    "get_quiet_chats": "inactive silent least active dormant quiet neglected haven't talked",
    "messages_by_day": "busiest days daily histogram activity over time per day trend",
    "count_messages": "how many messages message count total number of messages",
    "sender_activity": "how active is someone what has a person sent across chats",
    "get_reactions_on": "reactions emoji who reacted likes thumbs reacted to this message",
    "search_by_reaction": "messages that got a reaction reacted with emoji laughing heart find by reaction",
    "list_nonspeech_voices": "wolf howl animal sound sound effect music laughter non speech empty transcript weird noise silent clip no words voice that is just a sound",
    "resolve_identity": "who is this lid map lid to phone number unify same person hidden number group id whatsapp lid resolve identity what is this long number sender",
    "list_person_messages": "all messages from one person across dms and groups unified full history trace someone their phone and lid same person over time person history",
    "get_replies_to": "replies answers responses to this message",
    "get_message_thread": "thread conversation replies chain quoted",
    "get_message_receipts": "read receipts delivered seen ticks who read",
    "create_group": "new group start a group make a group create chat group",
    "leave_group": "exit group leave quit a group",
    "list_group_members": "who is in the group members participants people in group",
    "get_group_invite_link": "invite link share group join link group url",
    "join_group_by_link": "join a group via link accept invite",
    "update_group_participants": "add remove promote demote admin participants members",
    "groups_with_member": "which groups is someone in shared groups common groups",
    "mention_everyone": "tag everyone at everyone ping all notify all members",
    "block_user": "block spammer stop someone bar contact",
    "unblock_user": "unblock allow again remove block",
    "get_blocklist": "blocked contacts who did i block block list",
    "mute_chat": "silence stop notifications mute quiet a chat",
    "pin_chat": "pin to top stick chat unpin",
    "archive_chat": "archive hide move out of inbox",
    "delete_chat": "remove chat clear conversation delete thread",
    "mark_chat_unread": "mark unread show as unread badge",
    "star_message": "star bookmark save favorite important message",
    "set_disappearing": "disappearing vanishing ephemeral auto delete self destruct timer",
    "send_location": "share location where i am my location send a pin place",
    "send_sticker": "sticker send a sticker",
    "send_contact_card": "share a contact send someone's number vcard contact card",
    "post_status": "post a story status update my story broadcast",
    "set_status_message": "set my status about text availability busy away",
    "send_presence": "typing online last seen presence show typing",
    "check_phones_on_whatsapp": "is this number on whatsapp has whatsapp registered account exists",
    "get_user_info_bulk": "profile info status picture device count for contacts",
    "get_business_profile": "business profile catalog business info company details",
    "get_profile_picture": "profile photo avatar picture dp",
    "get_privacy_settings": "privacy who can see last seen read receipts settings",
    "set_chat_label": "label tag categorize a chat folder",
    "set_message_label": "label a message tag message",
    "set_chat_note": "note reminder memo about a chat annotate",
    "save_draft": "draft save a message for later composing",
    "export_chat": "export backup save conversation download chat history",
    "get_bridge_stats": "server stats bridge metrics counts diagnostics",
    "get_bridge_diagnostics": "diagnostics health details bridge debug info",
    "get_last_interaction": "last talked last message when did we last speak most recent",
    "get_contact_chats": "all chats with a person conversations with contact",
    "list_joined_groups": "my groups groups i am in all groups",
    "list_subscribed_newsletters": "channels newsletters i follow subscribed channels",
}


def _sig(params: Any) -> str:
    """Compact one-line parameter signature from a JSON-schema `parameters`.

    e.g. `chat_jid:string, query:string, limit?:integer`. `?` marks optional.
    Kept terse on purpose: this is what find_tool shows so the model can call
    the tool without a second round-trip for the full schema."""
    if not isinstance(params, dict):
        return ""
    props = params.get("properties")
    if not isinstance(props, dict):
        return ""
    required = set(params.get("required") or [])
    parts: list[str] = []
    for name, spec in props.items():
        t: Any = spec.get("type") if isinstance(spec, dict) else None
        if isinstance(t, list):
            t = "|".join(str(x) for x in t if x != "null") or "any"
        t = t or "any"
        parts.append(f"{name}:{t}" if name in required else f"{name}?:{t}")
    return ", ".join(parts)


def _load_mined_aliases() -> dict[str, str]:
    """Optional data-mined synonyms from tests/toolsearch-eval/mine_aliases.py,
    merged into the search index (never into the always-on tool definitions, so
    they are free on token cost). Path via WHATSAPP_MCP_ALIASES_FILE, else a
    conventional location next to the server. Missing/invalid -> {} (hand-curated
    aliases still apply)."""
    path = os.environ.get("WHATSAPP_MCP_ALIASES_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "aliases_mined.json"
    )
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (OSError, ValueError):
        pass
    return {}


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else v


def _cos(a: list[float] | None, b: list[float] | None) -> float:
    """Dot product of two unit vectors = cosine similarity. Both are pre-
    normalised, so this is just the dot; returns 0 if either is missing."""
    if not a or not b:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=False))


def _tool_doc(name: str, desc: str, aliases: str) -> str:
    """The text embedded to represent a tool: readable name + description +
    synonyms, so the semantic space captures both the formal purpose and the
    casual ways a user might ask for it."""
    return f"{name.replace('_', ' ')}. {desc} {aliases}".strip()


def _load_embed_cache() -> dict:
    try:
        with open(_EMBED_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_embed_cache(cache: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_EMBED_CACHE_PATH), exist_ok=True)
        tmp = _EMBED_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, _EMBED_CACHE_PATH)
    except OSError as e:
        log.warning("embed cache write failed: %s", e)


def _embed_tools(docs: dict[str, str]) -> None:
    """Populate _TOOL_VECS for every tool, using a disk cache so we re-embed only
    docs whose text changed (provider/model/hash keyed). One batch API call for
    the misses; no-op (stays lexical) if no provider is configured."""
    global _TOOL_VECS, _EMBED_READY, _QVEC_CACHE
    _TOOL_VECS = {}
    _EMBED_READY = False
    _QVEC_CACHE = {}

    try:
        import embeddings
    except Exception:
        return
    if not embeddings.available():
        log.info("embeddings: no provider configured; find_tool stays lexical")
        return

    import hashlib
    prov, model = embeddings.provider_name(), os.environ.get("WHATSAPP_MCP_EMBED_MODEL", "")
    cache = _load_embed_cache()
    ok = cache.get("provider") == prov and cache.get("model") == model
    cached_vecs = cache.get("vecs", {}) if ok else {}

    vecs: dict[str, list[float]] = {}
    missing_names, missing_docs = [], []
    for name, doc in docs.items():
        h = hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]
        ent = cached_vecs.get(name)
        if ent and ent.get("h") == h:
            vecs[name] = ent["v"]  # already unit-normalised when stored
        else:
            missing_names.append((name, h))
            missing_docs.append(doc)

    if missing_docs:
        got = embeddings.embed(missing_docs, input_type="document")
        if not got:
            # Provider errored: keep whatever cache we had; if nothing, stay lexical.
            if not vecs:
                return
        else:
            for (name, _h), vec in zip(missing_names, got, strict=False):
                vecs[name] = _unit(vec)

    if not vecs:
        return
    _TOOL_VECS = vecs
    _EMBED_READY = True
    # Persist (store unit vectors so load is cheap).
    _save_embed_cache({
        "provider": prov, "model": model,
        "vecs": {n: {"h": hashlib.sha256(docs[n].encode("utf-8")).hexdigest()[:16], "v": v}
                 for n, v in vecs.items() if n in docs},
    })
    log.info("embeddings: %s ready for %d/%d tools (%d newly embedded)",
             prov, len(vecs), len(docs), len(missing_docs))


def _query_vec(query: str) -> list[float] | None:
    if not _EMBED_READY:
        return None
    q = (query or "").strip()
    if q in _QVEC_CACHE:
        return _QVEC_CACHE[q]
    try:
        import embeddings
        got = embeddings.embed([q], input_type="query")
        vec = _unit(got[0]) if got else None
    except Exception:
        vec = None
    if len(_QVEC_CACHE) > 2000:
        _QVEC_CACHE.clear()
    _QVEC_CACHE[q] = vec
    return vec


def _stem(w: str) -> str:
    """Very small suffix stemmer so plural/tense variants match (days->day,
    messages->message, replies->reply, muting->mut). It does NOT have to be
    linguistically correct: the SAME stemmer runs on both the query and the
    indexed text, so even a wrong-but-consistent stem still makes the two sides
    agree. Guard on length so short words aren't mangled to nothing.

    The ONE thing it must get right is that a word and its plural land on the
    same stem. A blanket "es" strip broke exactly that for every noun already
    ending in -e, which is most of this domain's vocabulary: `messages`->`messag`
    while `message`->`message`, so a query saying "message" missed a description
    saying "messages" and vice versa. Only sibilant stems genuinely gain an "e"
    in the plural (box->boxes, dish->dishes); everything else just gained an "s".
    Caught by tests/unit/test_toolsearch.py::test_singular_and_plural_land_on_the_same_stem.
    """
    for suf in ("ing", "ies", "es", "ed", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            if suf == "ies":
                return w[:-3] + "y"
            if suf == "es":
                return w[:-2] if w[-3] in "sxzh" else w[:-1]
            return w[: -len(suf)]
    return w


def capture(tools: list[Any]) -> None:
    """Snapshot the full tool library BEFORE pruning.

    Called from server.py right after all tools are registered and before the
    minimal-mode prune removes the long tail from the directly-served set. The
    removed tools stay reachable here (we hold the object references), so
    find_tool can list them and call_tool can run them.

    Per tool we precompute, at capture time (once), a stemmed name-part set and a
    stemmed haystack-token set, so scoring is O(query terms) set lookups instead
    of substring scans over every description on every keystroke."""
    global _LIBRARY, _INDEX, _IDF
    _LIBRARY = {t.name: t for t in tools}
    _INDEX = []
    mined = _load_mined_aliases()  # data-mined synonyms, merged with hand-curated
    df: dict[str, int] = {}  # document frequency: tools whose text contains a stem
    docs: dict[str, str] = {}  # per-tool text to embed (semantic layer)
    for t in tools:
        name = t.name
        desc = (t.description or "").strip()
        tags = " ".join(sorted(t.tags or [])) if getattr(t, "tags", None) else ""
        aliases = f"{_ALIASES.get(name, '')} {mined.get(name, '')}".strip()
        name_l = name.lower()
        name_parts = frozenset(_stem(p) for p in name_l.split("_"))
        hay_text = f"{name_l} {desc} {tags} {aliases}".lower()
        hay_stems = frozenset(
            _stem(w) for w in _TOKEN_RE.split(hay_text) if w and w not in _STOPWORDS
        )
        for tokn in (name_parts | hay_stems):
            df[tokn] = df.get(tokn, 0) + 1
        _INDEX.append((name, name_l, name_parts, hay_stems, desc, _sig(getattr(t, "parameters", None))))
        if name not in _META_NAMES:
            docs[name] = _tool_doc(name, desc, aliases)

    # Inverse document frequency: a query word that occurs in MANY tools (group,
    # chat, message) barely discriminates, so it must score far below a rare,
    # distinctive word (mute, pin, star). Without this, "mute this noisy group"
    # scored every *_group tool as high as mute_chat. Smoothed so it is always
    # positive; a token in ~1 tool weighs ~2.5x one in ~12 tools.
    n = max(1, len(tools))
    _IDF = {tokn: math.log((n + 1) / (c + 1)) + 1.0 for tokn, c in df.items()}
    log.info("tool library captured: %d tools, %d index terms", len(_LIBRARY), len(df))

    # Optional semantic layer (no-op without an embedding provider).
    _embed_tools(docs)


# Function words carry no tool signal but match many descriptions, so unfiltered
# they drown the real query terms (e.g. "mute this group for a week" scored
# forward_message high on "for"/"a"/"this"). Strip them before scoring.
# True function words only. Do NOT stopword intent verbs (make, new, share, add,
# turn, set, ...) or noun-ish query words - those carry the tool signal. Also
# leave out get/show/list: they appear in many tool NAMES, so as query tokens
# they over-match; but they are harmless in the description haystack, and
# dropping them from the query avoids +8 name-part hits on every get_*/list_*.
_STOPWORDS = frozenset("""
a an the this that these those it its is are was were be been being am
i me my we our you your he she they them his her their to of in on at by
with from into onto over under and or but if then so as do does did done
please just here there now to too
get show list
""".split())


def _tokens(query: str) -> list[str]:
    toks = [t for t in _TOKEN_RE.split((query or "").lower()) if t]
    kept = [_stem(t) for t in toks if t not in _STOPWORDS and len(t) > 1]
    # If stopword stripping emptied it (a very terse query), fall back to raw.
    return kept or [_stem(t) for t in toks]


def _score(query_tokens: list[str], phrase: str, name_l: str,
           name_parts: frozenset[str], hay_stems: frozenset[str]) -> float:
    # Each hit is weighted by the term's IDF, so a distinctive word (mute, pin)
    # outscores a generic one (group, chat, message) that matches many tools.
    # Name-part hits weigh 3x a description hit; a name-substring hit 1.5x.
    score = 0.0
    for tok in query_tokens:
        idf = _IDF.get(tok, 1.0)
        if tok in name_parts:
            score += 3.0 * idf
        elif tok in name_l:        # substring of the name (block -> blocklist)
            score += 1.5 * idf
        if tok in hay_stems:       # appears in description/tags/aliases
            score += 1.0 * idf
    if phrase and phrase in name_l:  # whole phrase is the name-ish
        score += 2.0
    return score


def _ranked(query: str, limit: int) -> list[tuple[float, str, str, str]]:
    """Score every tool, return the top `limit` as (score, name, desc, sig).

    Lexical by default. When an embedding provider is configured, this becomes
    HYBRID: cosine similarity between the query and each tool's embedding is
    blended with the (per-query normalised) lexical score. Semantics can surface
    a tool that shares NO keywords with the query - the exact case lexical search
    misses - while lexical keeps precise name matches sharp. If the query
    embedding call fails, it silently degrades to pure lexical."""
    phrase = (query or "").strip().lower()
    tokens = _tokens(query)
    qvec = _query_vec(query)  # None unless embeddings ready and the call succeeds

    lex: list[tuple[float, str, str, str]] = []
    for name, name_l, name_parts, hay_stems, desc, sig in _INDEX:
        if name in _META_NAMES:
            continue
        s = _score(tokens, phrase, name_l, name_parts, hay_stems)
        lex.append((s, name, desc, sig))

    if qvec is None:
        pure = [r for r in lex if r[0] > 0]
        pure.sort(key=lambda r: (-r[0], r[1]))
        return pure[: max(1, limit)]

    # Hybrid: normalise lexical to [0,1] (by this query's max), clamp cosine to
    # [0,1], blend. Score ALL tools so a lexically-invisible tool can still rank
    # on semantics alone.
    max_lex = max((r[0] for r in lex), default=0.0) or 1.0
    w_sem = _EMBED_WEIGHT
    w_lex = 1.0 - w_sem
    blended: list[tuple[float, str, str, str]] = []
    for s, name, desc, sig in lex:
        sem = max(0.0, _cos(qvec, _TOOL_VECS.get(name)))
        final = w_lex * (s / max_lex) + w_sem * sem
        if final > 0:
            blended.append((final, name, desc, sig))
    blended.sort(key=lambda r: (-r[0], r[1]))
    return blended[: max(1, limit)]


def _search(query: str, limit: int) -> list[dict[str, Any]]:
    return [{"name": n, "description": d, "params": sig}
            for _s, n, d, sig in _ranked(query, limit)]


def _catalog(limit: int) -> list[dict[str, Any]]:
    """Fallback when a query matches nothing: a compact browse list (name +
    first line of the description) so the model can still orient itself."""
    out: list[dict[str, Any]] = []
    for row in _INDEX:
        name, desc = row[0], row[4]
        if name in _META_NAMES:
            continue
        first = desc.split("\n", 1)[0].strip()
        out.append({"name": name, "description": first})
    out.sort(key=lambda r: r["name"])
    return out[: max(1, limit)]


def _suggest(name: str, k: int = 5) -> list[str]:
    frag = (name or "").lower()
    names = [row[0] for row in _INDEX if row[0] not in _META_NAMES]
    near = [n for n in names if frag and frag in n.lower()]
    return (near or names)[:k]


def _enforce_scope(name: str) -> None:
    """Replicate observability.AuthzMiddleware for an internally dispatched tool.
    Without this, call_tool('delete_message', ...) would skip the per-tool scope
    check that a direct delete_message call is subject to."""
    try:
        from observability import TOOL_SCOPES
    except Exception:
        return
    needed = TOOL_SCOPES.get(name)
    if not needed:
        return
    from fastmcp.exceptions import ToolError
    from fastmcp.server.dependencies import get_access_token
    try:
        tok = get_access_token()
    except Exception:
        tok = None
    scopes = list(getattr(tok, "scopes", []) or []) if tok else []
    if "whatsapp:full" not in scopes and needed not in scopes:
        raise ToolError(
            f"Insufficient scope: tool '{name}' requires '{needed}'; "
            f"caller has {scopes or ['(none)']}"
        )


def _audit(name: str, args: Any, outcome: str, ms: float) -> None:
    """Mirror observability.ObservabilityMiddleware's audit row for a mutating
    tool dispatched through call_tool, tagged via=call_tool so the two paths are
    distinguishable in the log."""
    try:
        from observability import AUDIT_LOG_PATH, AUDIT_MUTATING_TOOLS, _safe_args_hash
        if name not in AUDIT_MUTATING_TOOLS:
            return
        row = {
            "ts": time.time(),
            "client_id": "owner",
            "tool": name,
            "args_hash": _safe_args_hash(args),
            "outcome": outcome,
            "ms": round(ms, 2),
            "via": "call_tool",
        }
        with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        log.warning("call_tool audit failed for %s", name, exc_info=True)


_FIND_DESC = (
    "Search the full WhatsApp tool library for a capability that is not in the "
    "directly-loaded core (e.g. group admin, analytics, newsletters, labels, "
    "scheduling, presence, blocklist, business profiles). Returns ranked matches "
    "with each tool's name, description, and parameter signature; then invoke the "
    "chosen one with call_tool(name, arguments). If none of the matches fit, call "
    "find_tool(query=\"*\") to BROWSE THE ENTIRE catalog (every tool, one line "
    "each) and pick from it - so any capability is always reachable, never hidden. "
    "Only needed for the long tail; common actions (search, read, send, media, "
    "transcription, polls) are already loaded as their own tools."
)

_CALL_DESC = (
    "Invoke any WhatsApp tool by name with a dictionary of its arguments - "
    "including tools not in the directly-loaded core. Use find_tool first to get "
    "the exact name and parameter signature. Example: "
    'call_tool(name="create_group", arguments={"subject": "Trip", "participants": ["201234567890"]}). '
    "Per-tool permission and audit rules apply identically to a direct call."
)


def register(mcp) -> None:
    """Register the two meta-tools. Safe to call in any mode; if the library was
    never captured (e.g. full mode) find_tool/call_tool simply operate over
    whatever was captured, or report an empty library."""

    @mcp.tool(
        name="find_tool",
        description=_FIND_DESC,
        annotations={"readOnlyHint": True, "openWorldHint": False, "idempotentHint": True},
    )
    def find_tool(query: str, limit: int = 8) -> dict[str, Any]:
        if not _LIBRARY:
            return {"tools": [], "note": "tool library not captured; all tools may already be loaded directly"}
        q = (query or "").strip().lower()
        # Browse mode: an empty/wildcard query returns the ENTIRE catalog (name +
        # one-line description for every tool). This is the guarantee that no tool
        # is ever hidden - worst case the model reads all 92 one-liners and picks,
        # same capability as loading every tool, at a fraction of the tokens.
        if q in _BROWSE_TOKENS:
            cat = _catalog(10_000)
            return {
                "query": query, "browse": True, "count": len(cat),
                "note": "Full tool catalog (every tool, one line each). Pick one and run it with call_tool(name, arguments).",
                "tools": cat,
            }
        ranked = _ranked(query, limit)
        if not ranked:
            cat = _catalog(60)
            return {
                "query": query, "count": 0,
                "note": "No keyword match. Browse this catalog, or call find_tool(query=\"*\") for the full list, then call_tool.",
                "tools": cat,
            }
        top = ranked[0][0]
        tools = [{"name": n, "description": d, "params": sig} for _s, n, d, sig in ranked]
        resp = {"query": query, "count": len(tools), "tools": tools}
        # Always tell the model the escape hatch exists; flag it louder when the
        # top match is weak. Threshold differs by mode: hybrid scores are blended
        # into [0,1], lexical scores are raw IDF sums.
        weak = 0.35 if _EMBED_READY else _WEAK_SCORE
        if top < weak:
            resp["note"] = ("Low-confidence matches. If none fit, call find_tool(query=\"*\") "
                            "to browse ALL tools, then call_tool(name, arguments).")
        else:
            resp["hint"] = "If none fit, call find_tool(query=\"*\") to see all tools."
        return resp

    @mcp.tool(
        name="call_tool",
        description=_CALL_DESC,
        # Not readOnly: it can dispatch a mutating tool. openWorld true; the inner
        # tool's own annotation is the real classification, but we can't know it
        # statically here, so mark the worst case so clients confirm-gate it.
        annotations={"readOnlyHint": False, "openWorldHint": True, "idempotentHint": False},
    )
    async def call_tool(name: str, arguments: dict[str, Any] | None = None):
        tool = _LIBRARY.get(name)
        if tool is None:
            return {
                "error": f"unknown tool '{name}'",
                "did_you_mean": _suggest(name),
                "hint": "use find_tool(query) to discover the exact tool name",
            }
        if name in _META_NAMES:
            return {"error": f"'{name}' is a meta-tool and cannot be dispatched via call_tool"}

        _enforce_scope(name)  # raises ToolError on insufficient scope (parity with direct call)

        args = arguments or {}
        start = time.perf_counter()
        outcome = "ok"
        try:
            result = await tool.run(args)
        except Exception as e:
            outcome = "error"
            _audit(name, args, outcome, (time.perf_counter() - start) * 1000)
            # Surface a helpful, structured error instead of a bare 500 so the
            # model can correct its arguments and retry.
            return {
                "error": f"{type(e).__name__}: {e}",
                "tool": name,
                "expected_params": _sig(getattr(tool, "parameters", None)),
                "hint": "check arguments against expected_params, then retry",
            }
        _audit(name, args, outcome, (time.perf_counter() - start) * 1000)
        return result
