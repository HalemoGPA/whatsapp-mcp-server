"""Isolated, read-only WhatsApp identity resolver (LID <-> phone + name).

WhatsApp hides phone numbers behind LIDs inside groups, so the same person shows
up under their phone number (PN) in DMs and under a LID in group history. The
bridge stores whichever the wire carried, so a person's messages fragment across
two sender values and nothing joins them - which makes "trace this one person"
miss half their history and forces the agent to GUESS which LID is whom.

whatsmeow already knows the authoritative mapping: it keeps a `whatsmeow_lid_map`
(lid, pn) table plus `whatsmeow_contacts` (names) in its own store, mounted
read-only for us at /data/store/whatsapp.db. This module reads ONLY those two
tables and exposes resolution. Design guarantees:

  - Read-only. Never writes anything; never touches the message store.
  - Additive. No existing code path imports this; existing tools are unchanged,
    so they cannot regress. Resolution is an explicit lens a caller opts into.
  - Truth-preserving. The stored sender stays the stored sender; this returns the
    derived phone/name as separate, clearly-labelled fields - it never rewrites
    message content.
  - Best-effort. Anything not in the map resolves to itself (input echoed back),
    never to a wrong person. It can only ever make attribution MORE accurate.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading

log = logging.getLogger("whatsapp_mcp.identity")

_STORE_DB = os.environ.get("WHATSMEOW_DB_PATH", "/data/store/whatsapp.db")

_lock = threading.Lock()
_loaded = False
_LID2PN: dict[str, str] = {}
_PN2LID: dict[str, str] = {}
_NAME: dict[str, dict] = {}  # bare number -> {"name","push_name","saved_name"}

_DIGITS = re.compile(r"\D")


def _bare(x: str) -> str:
    """Reduce any identifier (JID or bare) to just its digits: '2011@s.w.net'->'2011'."""
    if not x:
        return ""
    head = x.split("@", 1)[0]
    return _DIGITS.sub("", head)


def _load() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        try:
            c = sqlite3.connect(f"file:{_STORE_DB}?mode=ro", uri=True)
        except sqlite3.Error as e:
            log.warning("identity: cannot open whatsmeow store %s: %s", _STORE_DB, e)
            _loaded = True  # don't retry-storm; degrade to no-op resolution
            return
        try:
            for lid, pn in c.execute("SELECT lid, pn FROM whatsmeow_lid_map"):
                lb, pb = _bare(str(lid)), _bare(str(pn))
                if lb and pb:
                    _LID2PN[lb] = pb
                    _PN2LID.setdefault(pb, lb)
            for tj, first, full, push, biz in c.execute(
                "SELECT their_jid, first_name, full_name, push_name, business_name FROM whatsmeow_contacts"
            ):
                b = _bare(str(tj))
                if not b:
                    continue
                saved = full or first or biz or None      # name YOU saved them as
                entry = _NAME.get(b, {})
                # Prefer a real saved name; keep the best push_name we have seen.
                if saved and not entry.get("saved_name"):
                    entry["saved_name"] = saved
                if push and not entry.get("push_name"):
                    entry["push_name"] = push
                _NAME[b] = entry
        except sqlite3.Error as e:
            log.warning("identity: read failed: %s", e)
        finally:
            c.close()
        log.info("identity: loaded %d lid<->pn pairs, %d named contacts",
                 len(_LID2PN), len(_NAME))
        _loaded = True


def _name_for(*bares: str) -> tuple[str | None, str | None]:
    """(saved_name, push_name) from the first identity that has each."""
    saved = push = None
    for b in bares:
        e = _NAME.get(b)
        if not e:
            continue
        saved = saved or e.get("saved_name")
        push = push or e.get("push_name")
    return saved, push


def resolve(identifier: str) -> dict:
    """Resolve a phone number, LID, or JID to a person's identities + name.

    Returns a dict:
      {input, phone, lid, name, push_name, display, senders}
    where `phone`/`lid` are bare numbers (either may be None if unknown), `name`
    is the best human label (saved name > push name > phone), `display` is
    "Name (phone)" for showing, and `senders` is the list of raw sender values to
    query the message store with (phone and lid, whichever exist) so a person's
    full history is reachable. Unknown identifiers echo back with resolved=False.
    """
    _load()
    b = _bare(identifier)
    if not b:
        return {"input": identifier, "phone": None, "lid": None, "name": None,
                "push_name": None, "display": identifier, "senders": [], "resolved": False}

    if b in _LID2PN:            # given a LID
        lid, phone = b, _LID2PN[b]
    elif b in _PN2LID:         # given a phone that has a known LID
        phone, lid = b, _PN2LID[b]
    else:                      # a phone with no LID mapping, or an unknown id
        phone, lid = b, None

    saved, push = _name_for(*(x for x in (phone, lid) if x))
    name = saved or push or phone
    resolved = bool(lid or saved or push)
    display = f"{name} ({phone})" if name and name != phone else (phone or identifier)
    # senders: the values the messages table actually stores (bare), de-duped, order stable.
    senders = [s for s in (phone, lid) if s]
    return {"input": identifier, "phone": phone, "lid": lid, "name": name,
            "push_name": push, "saved_name": saved, "display": display,
            "senders": senders, "resolved": resolved}


def senders_for(identifier: str) -> list[str]:
    """Just the raw sender values (phone + lid) to query for this person."""
    return resolve(identifier)["senders"] or [_bare(identifier)]


def label(sender: str) -> str:
    """A display label for a raw sender value, e.g. 'Alex Doe (10000000000000)'.
    Falls back to the raw value if unresolvable - never a wrong name."""
    r = resolve(sender)
    return r["display"] if r["resolved"] else (sender or "")


def find_by_name(query: str, limit: int = 10) -> list[dict]:
    """Contacts whose saved or push name contains `query` (case-insensitive).
    Returns resolve() dicts. For turning a name into identities without guessing:
    it returns ALL matches rather than picking one."""
    _load()
    q = (query or "").strip().lower()
    if not q:
        return []
    seen: set[str] = set()  # keyed on resolved phone so a person's pn+lid rows collapse to one
    out: list[dict] = []
    for b, e in _NAME.items():
        nm = f"{e.get('saved_name', '')} {e.get('push_name', '')}".lower()
        if q not in nm:
            continue
        r = resolve(b)
        key = r["phone"] or b
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out
