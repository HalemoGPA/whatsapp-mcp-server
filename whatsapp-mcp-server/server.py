"""Authenticated HTTP entrypoint for the WhatsApp MCP server.

Serves the WhatsApp tools over Streamable HTTP at /mcp, gated by a single
owner bearer token. Only ONE principal exists: the owner. Every request must
carry `Authorization: Bearer <WHATSAPP_MCP_TOKEN>`; anything else -> 401.

We use FastMCP v2's built-in StaticTokenVerifier (no custom verifier needed)
keyed on exactly one token read from the environment.

Run (production, behind nginx on loopback):
    gunicorn server:asgi_app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:9000 -w 2

Run (local dev):
    python server.py
"""
from __future__ import annotations

import logging
import os

from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from fastmcp.server.middleware.caching import ResponseCachingMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from tools import register
from prompts import register as register_prompts
from observability import ObservabilityMiddleware, metrics_endpoint
from scheduling import register as register_scheduling, start_dispatcher as start_scheduler
from resources import register as register_resources

logging.basicConfig(
    level=os.environ.get("WHATSAPP_MCP_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("whatsapp_mcp")

import json as _json

# N7 + G11: multi-token registry with per-token scopes.
# The classic path (single WHATSAPP_MCP_TOKEN with scopes:["whatsapp:full"])
# still works. To use per-tool scopes, set WHATSAPP_MCP_TOKENS_JSON with a
# JSON array like:
#   [{"token":"abc...", "client_id":"myphone", "scopes":["whatsapp:read","whatsapp:send"]},
#    {"token":"def...", "client_id":"biscript", "scopes":["whatsapp:read"]}]
# Scopes recognized: whatsapp:read, whatsapp:send, whatsapp:admin, whatsapp:full
# (whatsapp:full = superset of all three).
_TOKENS: dict[str, dict] = {}

_TOKENS_JSON = os.environ.get("WHATSAPP_MCP_TOKENS_JSON", "").strip()
if _TOKENS_JSON:
    try:
        entries = _json.loads(_TOKENS_JSON)
        for e in entries:
            tok = str(e.get("token", "")).strip()
            if not tok:
                continue
            _TOKENS[tok] = {
                "client_id": str(e.get("client_id", "unknown")),
                "scopes":    list(e.get("scopes", ["whatsapp:read"])),
            }
    except (ValueError, TypeError) as exc:
        raise SystemExit(f"WHATSAPP_MCP_TOKENS_JSON is invalid JSON: {exc}") from exc

# Legacy single-token fallback
_LEGACY = os.environ.get("WHATSAPP_MCP_TOKEN", "").strip()
if _LEGACY and _LEGACY not in _TOKENS:
    _TOKENS[_LEGACY] = {"client_id": "owner", "scopes": ["whatsapp:full"]}

if not _TOKENS:
    raise SystemExit(
        "No auth tokens configured. Set WHATSAPP_MCP_TOKEN or "
        "WHATSAPP_MCP_TOKENS_JSON. Refusing to serve unauthenticated."
    )

# FastMCP's StaticTokenVerifier requires a global required_scopes; we cannot
# enforce per-tool scope there. Instead we grant every token the umbrella
# scope "whatsapp:authenticated" so requests pass the transport-level check,
# then let the ObservabilityMiddleware (or a new AuthzMiddleware) reject
# per-tool at dispatch time based on the token's declared scopes.
_verifier_tokens: dict[str, dict] = {}
for _tok, _meta in _TOKENS.items():
    _verifier_tokens[_tok] = {
        "client_id": _meta["client_id"],
        # Verifier scope check: "authenticated" required; per-tool logic below.
        "scopes":    ["whatsapp:authenticated"] + _meta["scopes"],
    }

auth = StaticTokenVerifier(
    tokens=_verifier_tokens,
    required_scopes=["whatsapp:authenticated"],
)

mcp = FastMCP(
    name="whatsapp",
    instructions=(
        "Private WhatsApp access for the owner. Search contacts and chats, read "
        "message history with context, send messages/files/voice notes, and "
        "download media. Phone numbers use country code with no + or symbols; "
        "group chats use the @g.us JID.\n"
        "IDENTITY: one person appears under BOTH their phone number (in DMs) and a "
        "hidden long numeric LID (in groups), so filtering on a single sender id "
        "silently misses half their history. Use resolve_identity to see who a LID "
        "is, and list_person_messages to read a person across both identities.\n"
        "ATTRIBUTION LIMIT: group messages before 2026-06-25 were stored with the "
        "GROUP's jid as the sender - the real sender was never recorded and cannot "
        "be recovered. Do NOT infer who sent those from surrounding context; say "
        "they are unattributable instead.\n"
        "REPLYING - PREFER QUOTED REPLIES BY DEFAULT: whenever you are responding "
        "to a specific message - the user says \"reply\", \"رد\", \"رد عليه\", "
        "\"answer him\", \"قوله\", or is reacting to / fact-checking / commenting on "
        "a particular message - call reply_to_message(message_id, text). That posts "
        "a QUOTED reply showing the original. This is the DEFAULT: if there is an "
        "identifiable message you are responding to, quote it. Use a plain "
        "send_message ONLY when the user explicitly wants a NEW standalone message "
        "not tied to any specific message. When unsure, prefer the quoted reply."
    ),
    auth=auth,
)

register(mcp)
register_prompts(mcp)
register_scheduling(mcp)
try:
    register_resources(mcp)
except Exception:
    log.exception("resources registration failed")

# Minimal toolset mode. Tool definitions are injected into the model's context on
# EVERY request, so the full 96-tool set is a ~20k-token fixed tax whether or not
# WhatsApp is used.
#
# DEFAULT IS MINIMAL: prune to the 29 core tools below, which cover the common
# 90% (search, read, send, media, transcription, polls, basic ops), and keep the
# other 67 reachable through find_tool + call_tool. Always-on cost drops to ~8k
# with no capability removed. Set WHATSAPP_MCP_TOOLSET=full to serve all 96
# directly (analytics, group admin, labels, newsletters, scheduling, blocklist,
# business, presence, ...) and skip the meta-tools.
_TOOLSET = os.environ.get("WHATSAPP_MCP_TOOLSET", "minimal").strip().lower()
_CORE_TOOLS = {
    # read / search
    "search_contacts", "search_all_messages", "list_messages", "list_chats",
    "get_chat", "get_message_context", "get_direct_chat_by_contact", "get_media_info",
    # send
    "send_message", "reply_to_message", "send_file", "send_audio_message",
    "send_view_once_media", "forward_message",
    # media + transcription
    "save_media", "view_media", "list_media_in_chat", "list_all_media_by_type",
    "list_view_once", "search_voice_notes", "transcribe_voice",
    # polls
    "create_poll", "vote_in_poll", "get_poll_results",
    # basic message ops
    "react_to_message", "mark_read", "delete_message", "edit_message",
    "bridge_health",
}
# Progressive disclosure: in minimal mode we keep the long tail REACHABLE (not
# gone) via two meta-tools, find_tool + call_tool (see toolsearch.py). We capture
# the full library first (holding references to every Tool), then prune the long
# tail from the directly-served set, then register the meta-tools. Net always-on
# cost = core (~8k) + 2 meta-tools, with 100% capability coverage on demand.
import toolsearch
if _TOOLSET == "minimal":
    try:
        import asyncio as _asyncio
        _full = _asyncio.run(mcp._list_tools())
        toolsearch.capture(_full)  # snapshot BEFORE pruning
        _names = [t.name for t in _full]
        _pruned = 0
        for _n in _names:
            if _n not in _CORE_TOOLS:
                mcp.local_provider.remove_tool(_n)
                _pruned += 1
        toolsearch.register(mcp)  # add find_tool + call_tool to the served set
        log.info(
            "toolset=minimal: kept %d core tools + find_tool/call_tool, pruned %d "
            "(reachable via call_tool)", len(_names) - _pruned, _pruned)
    except Exception:
        log.exception("minimal toolset prune failed; serving full set")


def _collapse_optional_schemas() -> None:
    """Rewrite `anyOf:[{...},{type:null}]` -> the non-null branch in every tool's
    parameter schema. FastMCP emits that verbose union for every `Optional[...]`
    param, and the null branch is pure JSON noise the model reads identically -
    yet it is a large fraction of the ~19k-token always-on tool-definition cost.
    Mutating tool.parameters in place persists across _list_tools (verified). No
    behaviour change: actual argument validation uses the Python signature, not
    this JSON schema, which is only what the model sees."""
    import asyncio as _asyncio

    collapsed = 0

    def walk(node):
        nonlocal collapsed
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for v in props.values():
                    if isinstance(v, dict):
                        ao = v.get("anyOf")
                        if isinstance(ao, list) and len(ao) == 2 and {"type": "null"} in ao:
                            other = [x for x in ao if x != {"type": "null"}]
                            if len(other) == 1 and isinstance(other[0], dict):
                                v.pop("anyOf")
                                for k, val in other[0].items():
                                    v.setdefault(k, val)  # graft the real branch (type, items, ...)
                                collapsed += 1
                        walk(v)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    try:
        for _t in _asyncio.run(mcp._list_tools()):
            walk(_t.parameters)
        log.info("schema: collapsed %d Optional anyOf unions", collapsed)
    except Exception:
        log.exception("schema collapse failed (non-fatal)")


_collapse_optional_schemas()

# Response caching for idempotent read tools. New in FastMCP 2.13+; an LLM
# session typically asks list_chats/search_contacts/get_chat repeatedly to
# orient itself - caching returns those without touching SQLite.
# - 30s TTL: short enough to feel live for the user's "what's new" probes,
#   long enough to absorb LLM iteration on the same query.
# - included_tools is an explicit allowlist of *read-only* tools. Anything
#   that mutates (send_*, react, edit, delete, mark_read, send_presence) is
#   NEVER cached - we never want a stale "sent" response.
# - list_messages is INCLUDED with the short TTL: an LLM grepping recent
#   messages benefits from caching, but 30s is short enough that newly
#   arrived messages show up on the next call. Tools that take a `query`
#   are still cached keyed on args, so different searches don't collide.
from observability import AuthzMiddleware
mcp.add_middleware(AuthzMiddleware())
mcp.add_middleware(ObservabilityMiddleware())

mcp.add_middleware(ResponseCachingMiddleware(
    call_tool_settings={
        "ttl": 30,
        "included_tools": [
            "list_chats",
            "search_contacts",
            "get_chat",
            "get_direct_chat_by_contact",
            "get_contact_chats",
            "get_last_interaction",
            "get_message_context",
            "list_messages",
            "bridge_health",
            "get_profile_picture",
        ],
    },
    list_tools_settings={"ttl": 300},  # tool registry changes only on redeploy
))


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Unauthenticated liveness probe for docker/nginx."""
    return JSONResponse({"ok": True})


# --- OAuth discovery endpoints ---------------------------------------------
# MCP clients (Claude Desktop, Anthropic MCP SDK) probe these before falling
# back to plain bearer. When nginx 404s them with HTML, the SDK tries to
# JSON-parse it and dies with "Unrecognized token '<'". Serve them from the
# app so the client can see we're not OAuth and use the static token.
_RESOURCE_URL = (
    os.environ.get("WHATSAPP_MCP_PUBLIC_URL", "").rstrip("/")
    or "https://whatsapp-mcp.example.com"
)


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource(_request: Request) -> JSONResponse:
    """RFC 9728: tell the client this endpoint takes a bearer token in the
    Authorization header and there is NO authorization server (static token
    provided out of band). Empty authorization_servers array signals that
    the SDK should skip OAuth registration and use the token directly.
    """
    return JSONResponse({
        "resource":                  f"{_RESOURCE_URL}/mcp",
        "authorization_servers":     [],
        "bearer_methods_supported":  ["header"],
        "resource_documentation":    _RESOURCE_URL,
    })


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_authorization_server(_request: Request) -> JSONResponse:
    """RFC 8414: we don't run an authorization server. Return a proper JSON
    404 so the SDK doesn't try to JSON.parse nginx's HTML default page."""
    return JSONResponse(
        {"error": "not_supported",
         "error_description": "this MCP server uses a static bearer token; no OAuth authorization server is available"},
        status_code=404,
    )


@mcp.custom_route("/.well-known/openid-configuration", methods=["GET"])
async def openid_configuration(_request: Request) -> JSONResponse:
    """Same treatment for OIDC discovery probes."""
    return JSONResponse(
        {"error": "not_supported",
         "error_description": "OIDC is not offered by this MCP server"},
        status_code=404,
    )


async def _serve_media_claims(claims: dict, inline: bool = False):
    """Fetch and stream the media for a set of link claims. Shared by the signed
    /media/ route and the short /s/ (download) and /p/ (preview) routes - the
    credential differs (HMAC token vs opaque code), what they authorise is
    identical. `inline` flips Content-Disposition from attachment (save to
    Downloads) to inline (browser renders the image / plays the video)."""
    import anyio

    from media import MediaError, fetch_media
    from starlette.responses import FileResponse

    try:
        info = await anyio.to_thread.run_sync(fetch_media, claims["m"], claims["c"])
    except MediaError as e:
        log.warning("media fetch failed for %s: %s", claims.get("m"), e)
        return JSONResponse({"error": str(e)}, status_code=404)

    disposition = "inline" if inline else "attachment"
    # filename is already allowlist-sanitised in media._safe_filename, so it is
    # safe to embed in the header. Set Content-Disposition ourselves (not via
    # FileResponse's filename=) so we control attachment-vs-inline.
    return FileResponse(
        path=info["path"],
        media_type=info["mime"],
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'{disposition}; filename="{info["filename"]}"',
        },
    )


@mcp.custom_route("/media/{token:path}", methods=["GET"])
async def media_download(request: Request):
    """Serve media via a short-lived HMAC-signed link.

    Deliberately NOT bearer-gated: this URL is meant to be opened by the user's
    browser, which cannot attach an Authorization header. The signature IS the
    credential (the S3-presigned-URL model), so it is HMAC-SHA256 over the
    claims, keyed on a secret derived from the bearer token, and expires in 15
    minutes by default. Treat any leaked link as valid until it expires.
    """
    from media import MediaError, verify_url

    try:
        claims = verify_url(request.path_params["token"])
    except MediaError as e:
        # Same status + opaque body for tampered and expired links alike: this
        # endpoint is unauthenticated, so it must not confirm which message ids
        # exist to someone probing it.
        log.warning("media link rejected: %s", e)
        return JSONResponse({"error": "invalid or expired link"}, status_code=403)
    return await _serve_media_claims(claims)


@mcp.custom_route("/s/{code}", methods=["GET"])
async def short_download(request: Request):
    """Short DOWNLOAD link (Content-Disposition: attachment -> saves to the
    user's Downloads). The random code is a server-side handle to the same
    claims a signed token carries - it IS the credential (unguessable,
    short-lived), so like /media/ this is not bearer-gated. Self-hosted rather
    than a third-party shortener because the link exposes private media."""
    from media import MediaError, resolve_short

    try:
        claims = resolve_short(request.path_params["code"])
    except MediaError as e:
        log.warning("short link rejected: %s", e)
        return JSONResponse({"error": "invalid or expired link"}, status_code=403)
    return await _serve_media_claims(claims, inline=False)


@mcp.custom_route("/p/{code}", methods=["GET"])
async def short_preview(request: Request):
    """Short PREVIEW link (Content-Disposition: inline -> the browser renders the
    image / plays the video/audio in-page instead of downloading). Same code and
    claims as the /s/ download link, just a different disposition."""
    from media import MediaError, resolve_short

    try:
        claims = resolve_short(request.path_params["code"])
    except MediaError as e:
        log.warning("preview link rejected: %s", e)
        return JSONResponse({"error": "invalid or expired link"}, status_code=403)
    return await _serve_media_claims(claims, inline=True)


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(request: Request):
    """G7: Prometheus /metrics. Loopback only - nginx must NOT proxy this."""
    return await metrics_endpoint(request)


# ASGI app served by gunicorn's UvicornWorker. Streamable HTTP at /mcp.
# stateless_http=True is required when running multiple workers behind a proxy:
# each request is self-contained, so a follow-up request landing on a different
# worker doesn't 404 on a missing in-memory session.
asgi_app = mcp.http_app(path="/mcp", stateless_http=True)

# G15: kick off the background scheduler on ASGI startup.
try:
    start_scheduler(asgi_app)
except Exception:
    log.exception("scheduler start failed")

# Background voice-note transcription worker. No-op unless SPEECHMATICS_API_KEYS
# (or GROQ_API_KEY) is set, so a deploy without keys just stays dormant.
try:
    import transcription
    transcription.start_worker()
except Exception:
    log.exception("transcription worker start failed")

# F4: pin all currently-loaded objects into a GC-immortal generation, raise the
# allocation threshold, and force one compaction so the long-running ASGI worker
# avoids re-scanning the import graph on every gen-0/1 cycle. Same pattern
# Instagram and Close.com documented; ~10-20% p99 drop on tool calls, zero
# RSS impact. Must run AFTER all modules are imported and tools registered.
import gc as _gc
_gc.collect(2)
_gc.freeze()
_gc.set_threshold(100_000, 50, 50)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(asgi_app, host="0.0.0.0", port=int(os.environ.get("PORT", "9000")))
