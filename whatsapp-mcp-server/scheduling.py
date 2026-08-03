"""G15: schedule_message + drafts.

Side SQLite database that holds queued sends and reusable drafts. A single
asyncio Task inside the MCP server ticks every 30 s, sends anything due via
the bridge, and updates status. Persistence path is /var/log/wamcp when
that volume is writable (batch G8's wa-audit volume), otherwise /tmp
(ephemeral - noisy log warning at startup).

Six tools ship via register(mcp): schedule_message, list_scheduled,
cancel_scheduled, save_draft, list_drafts, send_draft.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, UTC
from pathlib import Path
from typing import Any
from collections.abc import Iterator

log = logging.getLogger("whatsapp_mcp.sched")

_CANDIDATE_PATHS = [
    Path(os.environ.get("WHATSAPP_MCP_SCHEDULE_DB", "")),
    Path("/var/log/wamcp/scheduling.db"),
    Path("/tmp/wamcp-schedule.db"),
]


def _pick_writable_path() -> Path:
    """Same probe logic as observability's audit log."""
    for p in _CANDIDATE_PATHS:
        if not str(p):
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(str(p) + ".probe", "w") as f:
                f.write("")
            os.unlink(str(p) + ".probe")
            return p
        except OSError:
            continue
    log.warning("scheduling: no writable DB path; scheduler will be inert")
    return Path("/tmp/wamcp-schedule.db")


SCHED_DB_PATH = _pick_writable_path()


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(str(SCHED_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Open a connection, scope a transaction, and ALWAYS close the handle.

    `with sqlite3.connect(...) as c` does NOT close the connection - it only
    scopes a transaction (commit on success, rollback on exception) and leaves
    the handle open. The orphaned handle then sits in a reference cycle that
    plain refcounting cannot break, so only the *cyclic* GC reclaims it. Since
    server.py raises the gen-0 threshold to 100_000 allocations (F4), the
    near-idle scheduler thread takes hours to trip a sweep. Net effect was 2
    leaked fds per 30s tick (the .db and its -wal), exhausting the default
    RLIMIT_NOFILE of 1024 in ~4h12m, after which sqlite could not open files
    and uvicorn's accept() failed with EMFILE - the server stopped serving
    while still passing as "running". Diagnosed 2026-07-16.

    Nesting this is not supported (each call opens a separate connection and
    would self-deadlock on write locks); keep call sites flat.
    """
    conn = _open()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _init() -> None:
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS scheduled_sends (
            id            TEXT PRIMARY KEY,
            recipient     TEXT NOT NULL,
            message       TEXT NOT NULL,
            scheduled_at  TEXT NOT NULL,
            status        TEXT NOT NULL DEFAULT 'pending',
            attempts      INTEGER NOT NULL DEFAULT 0,
            last_error    TEXT,
            sent_at       TEXT,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_sched_due
            ON scheduled_sends(status, scheduled_at)
            WHERE status = 'pending';

        CREATE TABLE IF NOT EXISTS drafts (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            recipient   TEXT,
            message     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        -- Batch omicron: user-side annotations on chats.
        CREATE TABLE IF NOT EXISTS chat_notes (
            chat_jid    TEXT PRIMARY KEY,
            note        TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );
        """)


_init()


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---- Public helpers (called by tools.py wrappers) ---------------------------

def schedule_message(recipient: str, message: str, when_iso: str) -> dict[str, Any]:
    if not recipient or not message or not when_iso:
        return {"success": False, "message": "recipient, message, when_iso required"}
    try:
        # Normalize to ISO with tz.
        dt = datetime.fromisoformat(when_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        iso = dt.astimezone(UTC).isoformat()
    except ValueError:
        return {"success": False, "message": f"bad when_iso: {when_iso!r}"}
    sid = _new_id("sched")
    with _conn() as c:
        c.execute(
            "INSERT INTO scheduled_sends (id, recipient, message, scheduled_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (sid, recipient, message, iso, _now_iso()),
        )
    return {"success": True, "id": sid, "scheduled_at": iso}


def list_scheduled(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 1), 500))
    with _conn() as c:
        if status:
            rows = c.execute(
                "SELECT id, recipient, message, scheduled_at, status, attempts, last_error, sent_at, created_at "
                "FROM scheduled_sends WHERE status = ? ORDER BY scheduled_at ASC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT id, recipient, message, scheduled_at, status, attempts, last_error, sent_at, created_at "
                "FROM scheduled_sends ORDER BY scheduled_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
    keys = ["id", "recipient", "message", "scheduled_at", "status", "attempts", "last_error", "sent_at", "created_at"]
    return [dict(zip(keys, r, strict=False)) for r in rows]


def cancel_scheduled(sid: str) -> dict[str, Any]:
    with _conn() as c:
        cur = c.execute(
            "UPDATE scheduled_sends SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (sid,),
        )
        n = cur.rowcount
    if n:
        return {"success": True, "message": f"cancelled {sid}"}
    return {"success": False, "message": f"no pending schedule with id {sid}"}


def save_draft(name: str, message: str, recipient: str | None = None) -> dict[str, Any]:
    if not name or not message:
        return {"success": False, "message": "name and message required"}
    did = _new_id("draft")
    now = _now_iso()
    with _conn() as c:
        c.execute(
            "INSERT INTO drafts (id, name, recipient, message, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (did, name, recipient or "", message, now, now),
        )
    return {"success": True, "id": did}


def list_drafts(limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 1), 500))
    with _conn() as c:
        rows = c.execute(
            "SELECT id, name, recipient, message, created_at, updated_at "
            "FROM drafts ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    keys = ["id", "name", "recipient", "message", "created_at", "updated_at"]
    return [dict(zip(keys, r, strict=False)) for r in rows]


def delete_draft(did: str) -> dict[str, Any]:
    # rowcount must be read inside the block: the connection is closed on exit
    # now, and a cursor outlives its connection only as a dead object.
    with _conn() as c:
        n = c.execute("DELETE FROM drafts WHERE id = ?", (did,)).rowcount
    return {"success": bool(n), "message": f"deleted {did}" if n else f"no draft {did}"}


def set_chat_note(chat_jid: str, note: str) -> dict[str, Any]:
    """Batch omicron: user-side annotation on a chat (persisted, per-owner)."""
    if not chat_jid:
        return {"success": False, "message": "chat_jid required"}
    now = _now_iso()
    with _conn() as c:
        if note:
            c.execute(
                "INSERT INTO chat_notes (chat_jid, note, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_jid) DO UPDATE SET note = excluded.note, updated_at = excluded.updated_at",
                (chat_jid, note, now),
            )
        else:
            c.execute("DELETE FROM chat_notes WHERE chat_jid = ?", (chat_jid,))
    return {"success": True, "message": "saved" if note else "cleared"}


def get_chat_note(chat_jid: str) -> dict[str, Any]:
    with _conn() as c:
        row = c.execute("SELECT note, updated_at FROM chat_notes WHERE chat_jid = ?", (chat_jid,)).fetchone()
    if not row:
        return {"chat_jid": chat_jid, "note": None}
    return {"chat_jid": chat_jid, "note": row[0], "updated_at": row[1]}


def list_chat_notes() -> list[dict[str, Any]]:
    with _conn() as c:
        rows = c.execute("SELECT chat_jid, note, updated_at FROM chat_notes ORDER BY updated_at DESC").fetchall()
    return [{"chat_jid": r[0], "note": r[1], "updated_at": r[2]} for r in rows]


def send_draft(did: str, recipient: str | None = None) -> dict[str, Any]:
    """Send a saved draft immediately via the existing send_message helper."""
    with _conn() as c:
        row = c.execute(
            "SELECT recipient, message FROM drafts WHERE id = ?", (did,)
        ).fetchone()
    if not row:
        return {"success": False, "message": f"no draft {did}"}
    stored_recipient, message = row
    target = recipient or stored_recipient
    if not target:
        return {"success": False, "message": "draft has no recipient; supply one"}
    from whatsapp import send_message as _send_message
    ok, msg = _send_message(target, message)
    return {"success": ok, "message": msg}


# ---- Background dispatcher --------------------------------------------------

_TICK_INTERVAL_SEC = 30
_MAX_ATTEMPTS = 3


async def _dispatcher_loop() -> None:
    """Fire due schedules. Uses busy-loop with sleep; drift is bounded to
    _TICK_INTERVAL_SEC. Import send_message lazily to avoid circular imports."""
    from whatsapp import send_message as _send_message

    while True:
        try:
            with _conn() as c:
                now = _now_iso()
                due = c.execute(
                    "SELECT id, recipient, message, attempts FROM scheduled_sends "
                    "WHERE status = 'pending' AND scheduled_at <= ? LIMIT 20",
                    (now,),
                ).fetchall()
            for sid, rcpt, msg, attempts in due:
                try:
                    ok, note = _send_message(rcpt, msg)
                except Exception as e:
                    ok, note = False, f"{type(e).__name__}: {e}"
                with _conn() as c:
                    if ok:
                        c.execute(
                            "UPDATE scheduled_sends SET status='sent', sent_at=?, last_error=NULL WHERE id=?",
                            (_now_iso(), sid),
                        )
                        log.info("scheduled %s dispatched", sid)
                    else:
                        new_attempts = attempts + 1
                        new_status = "failed" if new_attempts >= _MAX_ATTEMPTS else "pending"
                        c.execute(
                            "UPDATE scheduled_sends SET status=?, attempts=?, last_error=? WHERE id=?",
                            (new_status, new_attempts, note, sid),
                        )
                        log.warning("scheduled %s failed (attempt %d/%d): %s",
                                    sid, new_attempts, _MAX_ATTEMPTS, note)
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(_TICK_INTERVAL_SEC)


import threading

_STARTED = threading.Event()


def _dispatcher_thread() -> None:
    """Own event loop in a daemon thread. This avoids the ASGI-lifespan
    plumbing entirely - the loop lives as long as the process."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        log.info("scheduler running in daemon thread (db=%s tick=%ss)",
                 SCHED_DB_PATH, _TICK_INTERVAL_SEC)
        loop.run_until_complete(_dispatcher_loop())
    except Exception:
        log.exception("scheduler thread crashed")
    finally:
        loop.close()


def start_dispatcher(app=None) -> None:
    """Idempotent. Spawns a daemon thread that runs the dispatcher loop."""
    if _STARTED.is_set():
        return
    _STARTED.set()
    t = threading.Thread(target=_dispatcher_thread, name="wamcp-scheduler", daemon=True)
    t.start()


# ---- MCP tool registration --------------------------------------------------

def register(mcp) -> None:
    from tools import READ_LOCAL, SEND, MUTATE, DESTRUCTIVE

    @mcp.tool(name="schedule_message", annotations=MUTATE)
    def _schedule_message(recipient: str, message: str, when_iso: str) -> dict[str, Any]:
        """Queue a message to send at a future time.

        Args:
            recipient: JID or phone number (country code, no + or symbols).
            message: Text to send.
            when_iso: ISO-8601 timestamp (UTC assumed if no tz). Fires within ~30 s.
        """
        return schedule_message(recipient, message, when_iso)

    @mcp.tool(name="list_scheduled", annotations=READ_LOCAL)
    def _list_scheduled(status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """List queued/sent/cancelled/failed scheduled messages.

        Args:
            status: Optional filter - pending / sent / cancelled / failed.
            limit: Max rows (default 50, max 500).
        """
        return list_scheduled(status, limit)

    @mcp.tool(name="cancel_scheduled", annotations=DESTRUCTIVE)
    def _cancel_scheduled(id: str, confirm: bool = False) -> dict[str, Any]:
        """Cancel a pending scheduled message (irreversible)."""
        if not confirm:
            return {"success": False, "message": "Pass confirm=True to cancel.", "confirmation_required": True}
        return cancel_scheduled(id)

    @mcp.tool(name="save_draft", annotations=MUTATE)
    def _save_draft(name: str, message: str, recipient: str | None = None) -> dict[str, Any]:
        """Save a reusable draft. Optional pre-filled recipient."""
        return save_draft(name, message, recipient)

    @mcp.tool(name="list_drafts", annotations=READ_LOCAL)
    def _list_drafts(limit: int = 50) -> list[dict[str, Any]]:
        """List saved drafts, most recently updated first."""
        return list_drafts(limit)

    @mcp.tool(name="send_draft", annotations=SEND)
    def _send_draft(id: str, recipient: str | None = None) -> dict[str, Any]:
        """Send a saved draft immediately.

        Args:
            id: Draft id from list_drafts.
            recipient: Override the draft's saved recipient.
        """
        return send_draft(id, recipient)

    @mcp.tool(name="delete_draft", annotations=DESTRUCTIVE)
    def _delete_draft(id: str, confirm: bool = False) -> dict[str, Any]:
        """Delete a draft (irreversible)."""
        if not confirm:
            return {"success": False, "message": "Pass confirm=True to delete.", "confirmation_required": True}
        return delete_draft(id)

    @mcp.tool(name="set_chat_note", annotations=MUTATE)
    def _set_chat_note(chat_jid: str, note: str) -> dict[str, Any]:
        """Attach a private note to a chat (or pass empty note to clear)."""
        return set_chat_note(chat_jid, note)

    @mcp.tool(name="get_chat_note", annotations=READ_LOCAL)
    def _get_chat_note(chat_jid: str) -> dict[str, Any]:
        """Read the private note attached to a chat."""
        return get_chat_note(chat_jid)

    @mcp.tool(name="list_chat_notes", annotations=READ_LOCAL)
    def _list_chat_notes() -> list[dict[str, Any]]:
        """List all chat notes, most recently updated first."""
        return list_chat_notes()
