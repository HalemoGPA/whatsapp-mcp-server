import sqlite3
from datetime import datetime, timedelta, UTC
from dataclasses import dataclass
import os
import os.path
import re
import logging
import requests
import json
import audio

# Module logger - server.py configures the root logger; this just attaches.
# Use log.exception(...) inside `except` blocks so the traceback flows to
# docker logs instead of vanishing into print().
log = logging.getLogger("whatsapp_mcp.db")

# Hard upper bounds enforced at the tool boundary so an LLM mistake cannot
# OOM the 90MB Python worker or dump arbitrary amounts of history.
MAX_LIMIT = 100
MAX_CONTEXT = 20
MAX_MESSAGE_BYTES = 65536  # WhatsApp text message hard limit (~65k chars)

# Both are env-overridable so the same code runs locally (bridge on localhost,
# DB under ../whatsapp-bridge/store) and in Docker (bridge service + shared
# volume). Defaults preserve the original local behaviour.
MESSAGES_DB_PATH = os.environ.get(
    "MESSAGES_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'whatsapp-bridge', 'store', 'messages.db'),
)
WHATSAPP_API_BASE_URL = os.environ.get("WHATSAPP_API_BASE_URL", "http://localhost:8080/api")


# #146: all timestamps produced by the bridge are UTC (see time.Unix(...).UTC()
# in whatsapp-bridge/main.go). Python's fromisoformat returns naive datetimes;
# _as_utc marks them so JSON serialization and downstream consumers see the
# tz explicitly instead of guessing.
def _as_utc(ts_str):
    if not ts_str:
        return None
    s = str(ts_str).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt

# Module-level Session for the bridge HTTP path. Reusing a Session keeps the
# TCP connection to the bridge open across calls (~5-10ms saved per request
# vs a fresh handshake) and bounds the number of sockets the bridge has to
# accept. F5: pool_maxsize=40 to match uvicorn's default thread pool so we
# never bottleneck on the connection pool under burst.
_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({"User-Agent": "whatsapp-mcp/1.0"})
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
_adapter = HTTPAdapter(
    pool_connections=1,        # one bridge backend
    pool_maxsize=40,           # uvicorn default sync->async threadpool size
    max_retries=Retry(total=0),  # MCP tools handle retries explicitly
)
_HTTP_SESSION.mount("http://", _adapter)
_HTTP_SESSION.mount("https://", _adapter)

# Sandbox for send_file / send_audio_message media paths. Without this an LLM
# tricked into "send my chat history" could pass `/data/store/messages.db` as
# media_path and the bridge would happily POST the whole decrypted DB to a
# recipient.
# F6: tightened from '/tmp' (every container temp file) to a dedicated
# subdirectory we create mode-0700. Closes the 'send arbitrary /tmp file'
# primitive and matches the sandbox's documented scope.
_DEFAULT_MEDIA_ROOT_DIR = "/tmp/wamcp-media"
try:
    os.makedirs(_DEFAULT_MEDIA_ROOT_DIR, mode=0o700, exist_ok=True)
    os.chmod(_DEFAULT_MEDIA_ROOT_DIR, 0o700)
except OSError:
    pass
_MEDIA_ROOTS = tuple(
    os.path.realpath(p) for p in (
        [_DEFAULT_MEDIA_ROOT_DIR] +
        [p for p in os.environ.get("WHATSAPP_MEDIA_ROOTS", "").split(":") if p]
    )
)


def _is_safe_media_path(path: str) -> bool:
    """True if `path` resolves under any configured media root, with no symlink escape."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    return any(real == root or real.startswith(root.rstrip("/") + "/") for root in _MEDIA_ROOTS)


import threading
import contextlib

# Per-thread connection cache. FastMCP runs sync tool handlers in a thread
# pool, so a process-wide singleton sqlite3.Connection trips check_same_thread.
# Solution: one connection per thread, lazily initialized in _open_ro(). PRAGMAs
# still run once per thread (a tiny constant cost amortized across many calls
# from that thread). SQLite at threadsafety=3 (CPython default) serializes
# inside the C layer, so this is safe even if threads ever touch each other's
# connections.
_ro_local = threading.local()


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{MESSAGES_DB_PATH}?mode=ro",
        uri=True,
        check_same_thread=False,  # belt-and-braces; we already shard per-thread
    )
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA mmap_size=134217728")  # 128 MiB
    conn.execute("PRAGMA cache_size=-20000")    # 20 MiB
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


class _ConnProxy:
    """Forwards everything to the per-thread connection except close()."""

    __slots__ = ("_c",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._c = conn

    def __getattr__(self, name: str):
        return getattr(self._c, name)

    def close(self) -> None:  # no-op; the connection lives for the thread's life
        return None


def _open_ro() -> "_ConnProxy":
    """Return a proxy to this thread's read-only SQLite connection.

    - file:...?mode=ro prevents accidental writes (defense in depth).
    - busy_timeout retries when whatsmeow holds the write lock during checkpoints.
    - mmap+cache give a meaningful read perf bump for a near-free RAM cost.
    - The connection is created once per thread, so PRAGMAs and the open
      syscall happen at most a handful of times across the process lifetime
      (matches the size of uvicorn's default sync-to-async thread pool).
    """
    conn = getattr(_ro_local, "conn", None)
    if conn is None:
        conn = _make_conn()
        _ro_local.conn = conn
    return _ConnProxy(conn)


_FTS_OP_CHARS = re.compile(r"[^\wÀ-ɏͰ-ϿЀ-ӿ֐-׿؀-ۿ一-鿿\s]")


def _fts_escape(q: str) -> str:
    """Escape an arbitrary user query for FTS5 MATCH.

    Wraps every token in double quotes so FTS5 operator chars (AND/OR/NOT/NEAR
    and ", *, :, ^, (, )) are taken literally. Then joins tokens with AND so
    multi-word queries do prefix-equivalent intersection. Drops chars that
    aren't word chars, common Latin/Cyrillic/Greek/Hebrew/Arabic/CJK ranges,
    or whitespace - prevents weird injection while keeping non-Latin queries
    working (the user's chats are heavily Arabic).
    """
    cleaned = _FTS_OP_CHARS.sub(" ", q.strip())
    tokens = [t for t in cleaned.split() if t]
    if not tokens:
        return '""'
    # Double up any embedded double quotes inside each token (FTS5 quoting rule).
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _expand_contexts(
    conn,
    anchors: "list[Message]",
    before_n: int,
    after_n: int,
) -> "list[Message]":
    """Expand anchor messages with context windows in one query per anchor.

    Old shape: get_message_context() ran 3 SELECTs per anchor (target + before
    window + after window). 20 anchors = 60 queries. New shape: ONE UNION-ALL
    query per anchor (3 ordered slices in one round-trip), each slice an
    index-only seek on idx_messages_chat_time. 20 anchors = 20 queries, each
    sub-millisecond.

    A previous attempt used a per-chat ROW_NUMBER CTE which sounded smarter
    but materialised the entire chat's row-numbering before any filtering;
    that exploded to 191 seconds on a 25k-message chat. The win there would
    need a more careful windowing - left as a TODO.
    """
    out: list[Message] = []
    seen_ids: set = set()
    cur = conn.cursor()

    for anchor in anchors:
        # iso string for timestamp comparison; index stores TEXT timestamps.
        anchor_ts = anchor.timestamp.isoformat(sep=" ")
        sql = """
            SELECT * FROM (
                SELECT m.timestamp, m.sender, c.name AS chat_name, m.content,
                       m.is_from_me, c.jid, m.id, m.media_type
                FROM messages m JOIN chats c ON m.chat_jid = c.jid
                WHERE m.chat_jid = ? AND m.timestamp < ?
                ORDER BY m.timestamp DESC LIMIT ?
            )
            UNION ALL
            SELECT m.timestamp, m.sender, c.name AS chat_name, m.content,
                   m.is_from_me, c.jid, m.id, m.media_type
            FROM messages m JOIN chats c ON m.chat_jid = c.jid
            WHERE m.id = ?
            UNION ALL
            SELECT * FROM (
                SELECT m.timestamp, m.sender, c.name AS chat_name, m.content,
                       m.is_from_me, c.jid, m.id, m.media_type
                FROM messages m JOIN chats c ON m.chat_jid = c.jid
                WHERE m.chat_jid = ? AND m.timestamp > ?
                ORDER BY m.timestamp ASC LIMIT ?
            )
        """
        cur.execute(sql, (
            anchor.chat_jid, anchor_ts, before_n,
            anchor.id,
            anchor.chat_jid, anchor_ts, after_n,
        ))
        for r in cur.fetchall():
            if r[6] in seen_ids:
                continue
            seen_ids.add(r[6])
            out.append(Message(
                timestamp=_as_utc(r[0]),
                sender=r[1], chat_name=r[2], content=r[3],
                is_from_me=r[4], chat_jid=r[5], id=r[6], media_type=r[7],
            ))

    # Final order: timestamp DESC across all collected context messages.
    out.sort(key=lambda m: m.timestamp, reverse=True)
    return out


def _resolve_names(jids: list[str]) -> dict[str, str]:
    """Batch-resolve {jid -> display name} in ONE query instead of N.

    Replaces the per-message get_sender_name() round trips that turn a
    20-message list into 20+ extra SELECTs. For JIDs without a stored name we
    fall back to the JID's local-part (matches the old single-lookup behaviour).
    """
    if not jids:
        return {}
    uniq = list({j for j in jids if j})
    placeholders = ",".join("?" * len(uniq))
    out: dict[str, str] = {}
    conn = _open_ro()
    cur = conn.cursor()
    cur.execute(
        f"SELECT jid, name FROM chats WHERE jid IN ({placeholders})", uniq
    )
    for jid, name in cur.fetchall():
        if name:
            out[jid] = name
    # Fill in JIDs that didn't match with the local-part fallback.
    for j in uniq:
        out.setdefault(j, j.split("@", 1)[0] if "@" in j else j)
    return out

@dataclass
class Message:
    timestamp: datetime
    sender: str
    content: str
    is_from_me: bool
    chat_jid: str
    id: str
    chat_name: str | None = None
    media_type: str | None = None

@dataclass
class Chat:
    jid: str
    name: str | None
    last_message_time: datetime | None
    last_message: str | None = None
    last_sender: str | None = None
    last_is_from_me: bool | None = None
    # The contact's self-set display name (PushName) from WhatsApp, when it
    # differs from `name`. Stored separately so the client can show e.g.
    # `name="Smith" push_name="Jo"`. May be None for chats where only
    # one source is known.
    push_name: str | None = None

    @property
    def is_group(self) -> bool:
        """Determine if chat is a group based on JID pattern."""
        return self.jid.endswith("@g.us")

@dataclass
class Contact:
    phone_number: str
    name: str | None
    jid: str
    push_name: str | None = None  # contact's self-set WhatsApp display name

@dataclass
class MessageContext:
    message: Message
    before: list[Message]
    after: list[Message]

def get_sender_name(sender_jid: str) -> str:
    """Resolve one JID to a display name. Used by single-message callers.

    Behaviour matches the batched _resolve_names(): exact-JID hit returns the
    stored name; miss falls back to the local-part of the JID. The previous
    fallback used `LIKE '%number%'` which scanned the whole chats table AND
    could match the wrong contact (a substring hit anywhere in any JID).
    """
    try:
        conn = _open_ro()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM chats WHERE jid = ? LIMIT 1", (sender_jid,))
        row = cursor.fetchone()
        if row and row[0]:
            return row[0]
    except sqlite3.Error:
        log.exception("get_sender_name failed for jid=%s", sender_jid)
    finally:
        if 'conn' in locals():
            conn.close()
    return sender_jid.split('@', 1)[0] if '@' in sender_jid else sender_jid

_VOICE_MEDIA = ("audio", "ptt", "voice", "video")


def _voice_transcript(message_id: str, chat_jid: str) -> str | None:
    """Transcript text for a voice note, if one has been produced. Lazy import
    keeps transcription optional - if the module or its DB isn't there, callers
    just get the plain [audio] placeholder."""
    try:
        import transcription
        t = transcription.get_transcript(message_id, chat_jid)
        if t and t.get("status") == "done" and t.get("text"):
            return t["text"]
    except Exception:
        pass
    return None


def _format_message_with_name(message: Message, sender_name: str, show_chat_info: bool = True) -> str:
    """Format a single message with a pre-resolved sender name."""
    if show_chat_info and message.chat_name:
        output = f"[{message.timestamp:%Y-%m-%dT%H:%M:%SZ}] Chat: {message.chat_name} "
    else:
        output = f"[{message.timestamp:%Y-%m-%dT%H:%M:%SZ}] "

    content_prefix = ""
    if getattr(message, "media_type", None):
        content_prefix = f"[{message.media_type} - Message ID: {message.id} - Chat JID: {message.chat_jid}] "
        # Voice notes carry no text of their own; inline the transcript so it
        # shows up automatically wherever messages are read - the caller does
        # not have to know a separate transcript exists or ask for it.
        if message.media_type in _VOICE_MEDIA:
            tx = _voice_transcript(message.id, message.chat_jid)
            if tx:
                content_prefix += f'\n    Transcript: "{tx}" '

    output += f"From: {sender_name}: {content_prefix}{message.content}\n"
    return output


def format_message(message: Message, show_chat_info: bool = True) -> str:
    """Format a single message (kept for single-message callers)."""
    sender_name = "Me" if message.is_from_me else get_sender_name(message.sender)
    return _format_message_with_name(message, sender_name, show_chat_info)


def format_messages_list(messages: list[Message], show_chat_info: bool = True) -> str:
    if not messages:
        return "No messages to display."

    # Resolve every distinct non-self sender JID in ONE query, then format.
    senders_to_resolve = [m.sender for m in messages if not m.is_from_me]
    names = _resolve_names(senders_to_resolve)

    parts: list[str] = []
    for m in messages:
        sender_name = "Me" if m.is_from_me else names.get(m.sender, m.sender)
        parts.append(_format_message_with_name(m, sender_name, show_chat_info))
    return "".join(parts)

def list_messages(
    after: str | None = None,
    before: str | None = None,
    sender_phone_number: str | None = None,
    chat_jid: str | None = None,
    query: str | None = None,
    limit: int = 20,
    page: int = 0,
    include_context: bool = True,
    context_before: int = 1,
    context_after: int = 1,
    before_timestamp: str | None = None,
) -> list[Message]:
    """Get messages matching the specified criteria with optional context.

    Pagination: pass `before_timestamp` (an ISO-8601 string of the OLDEST
    message you've already seen) to get the next older page. This is O(limit)
    via idx_messages_timestamp, vs the page+OFFSET path which scans through
    `page*limit` rows. The two forms are mutually exclusive - `before_timestamp`
    wins when both are set.
    """
    # Hard clamps - an LLM asking for limit=10000 would otherwise materialize
    # ~10k Message dataclasses (and with include_context, 30k+ rows) in a 90MB worker.
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    context_before = max(0, min(int(context_before or 0), MAX_CONTEXT))
    context_after = max(0, min(int(context_after or 0), MAX_CONTEXT))
    try:
        conn = _open_ro()
        cursor = conn.cursor()

        # When `query` is supplied, route through the FTS5 mirror (messages_fts).
        # This replaces a full LOWER(content) LIKE scan of all 77k+ messages with
        # a posting-list lookup. We use LEFT JOIN messages_fts only when needed
        # so non-query calls retain the original index-only plan.
        where_clauses: list[str] = []
        params: list = []
        use_fts = bool(query and query.strip())

        if use_fts:
            from_clause = (
                "FROM messages_fts "
                "JOIN messages ON messages.rowid = messages_fts.rowid "
                "JOIN chats ON messages.chat_jid = chats.jid"
            )
            where_clauses.append("messages_fts MATCH ?")
            params.append(_fts_escape(query))
        else:
            from_clause = "FROM messages JOIN chats ON messages.chat_jid = chats.jid"

        if after:
            try:
                after_dt = datetime.fromisoformat(after)
            except ValueError:
                raise ValueError(f"Invalid date format for 'after': {after}. Please use ISO-8601 format.") from None
            where_clauses.append("messages.timestamp > ?")
            params.append(after_dt.isoformat(sep=" "))

        if before:
            try:
                before_dt = datetime.fromisoformat(before)
            except ValueError:
                raise ValueError(f"Invalid date format for 'before': {before}. Please use ISO-8601 format.") from None
            where_clauses.append("messages.timestamp < ?")
            params.append(before_dt.isoformat(sep=" "))

        if sender_phone_number:
            where_clauses.append("messages.sender = ?")
            params.append(sender_phone_number)

        if chat_jid:
            where_clauses.append("messages.chat_jid = ?")
            params.append(chat_jid)

        # Cursor mode: `before_timestamp` replaces OFFSET so deep pagination
        # stays O(limit) instead of O(page*limit). OFFSET kept as fallback
        # for callers that still pass `page` and haven't migrated yet.
        if before_timestamp:
            try:
                bts = datetime.fromisoformat(before_timestamp).isoformat(sep=" ")
            except ValueError:
                raise ValueError(f"Invalid date format for 'before_timestamp': {before_timestamp}.") from None
            where_clauses.append("messages.timestamp < ?")
            params.append(bts)
            offset = 0
        else:
            offset = page * limit

        sql = (
            "SELECT messages.timestamp, messages.sender, chats.name, messages.content, "
            "messages.is_from_me, chats.jid, messages.id, messages.media_type "
            f"{from_clause} "
            + ("WHERE " + " AND ".join(where_clauses) + " " if where_clauses else "")
            + "ORDER BY messages.timestamp DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()

        result: list[Message] = [
            Message(
                timestamp=_as_utc(r[0]),
                sender=r[1], chat_name=r[2], content=r[3],
                is_from_me=r[4], chat_jid=r[5], id=r[6], media_type=r[7],
            )
            for r in rows
        ]

        if include_context and result and (context_before or context_after):
            # Batched per-chat context fetch replaces the previous N+1 (1 + 20*3
            # queries). For each distinct chat in the result, run ONE windowed
            # query that returns every message within `max(context_before,
            # context_after)` rows of any anchor in that chat. Typical
            # multi-chat result with 20 anchors and 1-3 distinct chats collapses
            # 60 queries into 3.
            messages_with_context = _expand_contexts(
                conn, result, context_before, context_after
            )
            return format_messages_list(messages_with_context, show_chat_info=True)

        return format_messages_list(result, show_chat_info=True)
    except sqlite3.Error:
        log.exception("list_messages query failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_context(
    message_id: str,
    before: int = 5,
    after: int = 5
) -> MessageContext:
    """Get context around a specific message."""
    before = max(0, min(int(before or 0), MAX_CONTEXT))
    after = max(0, min(int(after or 0), MAX_CONTEXT))
    try:
        conn = _open_ro()
        cursor = conn.cursor()

        # Get the target message first
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.chat_jid, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.id = ?
        """, (message_id,))
        msg_data = cursor.fetchone()

        if not msg_data:
            raise ValueError(f"Message with ID {message_id} not found")

        target_message = Message(
            timestamp=_as_utc(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[8]
        )

        # Get messages before
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp < ?
            ORDER BY messages.timestamp DESC
            LIMIT ?
        """, (msg_data[7], msg_data[0], before))

        before_messages = []
        for msg in cursor.fetchall():
            before_messages.append(Message(
                timestamp=_as_utc(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))

        # Get messages after
        cursor.execute("""
            SELECT messages.timestamp, messages.sender, chats.name, messages.content, messages.is_from_me, chats.jid, messages.id, messages.media_type
            FROM messages
            JOIN chats ON messages.chat_jid = chats.jid
            WHERE messages.chat_jid = ? AND messages.timestamp > ?
            ORDER BY messages.timestamp ASC
            LIMIT ?
        """, (msg_data[7], msg_data[0], after))

        after_messages = []
        for msg in cursor.fetchall():
            after_messages.append(Message(
                timestamp=_as_utc(msg[0]),
                sender=msg[1],
                chat_name=msg[2],
                content=msg[3],
                is_from_me=msg[4],
                chat_jid=msg[5],
                id=msg[6],
                media_type=msg[7]
            ))

        return MessageContext(
            message=target_message,
            before=before_messages,
            after=after_messages
        )

    except sqlite3.Error:
        log.exception("get_message_context query failed")
        raise
    finally:
        if 'conn' in locals():
            conn.close()


def list_chats(
    query: str | None = None,
    limit: int = 20,
    page: int = 0,
    include_last_message: bool = True,
    sort_by: str = "last_active"
) -> list[Chat]:
    """Get chats matching the specified criteria."""
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    try:
        conn = _open_ro()
        cursor = conn.cursor()

        # The previous JOIN on `chats.last_message_time = messages.timestamp`
        # silently duplicated chats whenever two messages in that chat shared
        # the same UTC second (common: media + caption, bulk replies). Two rows
        # per chat ate the LIMIT budget and surfaced the same chat twice in the
        # listing. Correlated subquery on messages.id forces one row per chat.
        from_clause = (
            "FROM chats "
            "LEFT JOIN messages m_last ON m_last.rowid = ("
            "  SELECT rowid FROM messages WHERE chat_jid = chats.jid ORDER BY timestamp DESC LIMIT 1"
            ")"
        ) if include_last_message else "FROM chats"
        select_extra = (
            # G1: one rowid lookup + LEFT JOIN instead of three correlated
            # ORDER-BY-DESC-LIMIT-1 subqueries. 3x fewer index seeks per row.
            "m_last.content    AS last_message, "
            "m_last.sender     AS last_sender, "
            "m_last.is_from_me AS last_is_from_me"
            if include_last_message
            else "NULL AS last_message, NULL AS last_sender, NULL AS last_is_from_me"
        )

        where_clauses: list[str] = []
        params: list = []
        if query and query.strip():
            where_clauses.append("(LOWER(chats.name) LIKE LOWER(?) OR LOWER(chats.jid) LIKE LOWER(?))")
            params.extend([f"%{query.strip()}%", f"%{query.strip()}%"])

        # Stable pagination via composite key (avoid duplicates/missed rows on
        # page boundaries when many chats share a NULL/identical sort key).
        order_by = (
            "chats.last_message_time DESC, chats.jid"
            if sort_by == "last_active"
            else "COALESCE(chats.name, chats.jid) COLLATE NOCASE, chats.jid"
        )
        offset = page * limit
        params.extend([limit, offset])

        sql = (
            f"SELECT chats.jid, chats.name, chats.last_message_time, "
            f"{select_extra} {from_clause} "
            + ("WHERE " + " AND ".join(where_clauses) + " " if where_clauses else "")
            + f"ORDER BY {order_by} LIMIT ? OFFSET ?"
        )
        cursor.execute(sql, tuple(params))
        chats = cursor.fetchall()

        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=_as_utc(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5],
                push_name=chat_data[6] if len(chat_data) > 6 else None,
            )
            result.append(chat)

        return result

    except sqlite3.Error:
        log.exception("query failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_contacts(query: str) -> list[Contact]:
    """Search contacts by name or phone number.

    The query must contain at least one non-whitespace character - a blank
    query would otherwise dump the first 50 rows of the chats table.
    Searches BOTH name (user's saved label) and push_name (contact's self-set
    display name), so the LLM can find a contact by either name.
    """
    if not query or not query.strip():
        return []
    try:
        conn = _open_ro()
        cursor = conn.cursor()

        # chats.jid is PRIMARY KEY so DISTINCT is logically a no-op here; dropped.
        # Leading wildcards defeat any index; chats is ~1000 rows so a scan is fine.
        search_pattern = f"%{query.strip()}%"

        cursor.execute("""
            SELECT jid, name, push_name
            FROM chats
            WHERE (LOWER(name) LIKE LOWER(?) OR LOWER(push_name) LIKE LOWER(?) OR LOWER(jid) LIKE LOWER(?))
              AND jid NOT LIKE '%@g.us'
            ORDER BY name, jid
            LIMIT 50
        """, (search_pattern, search_pattern, search_pattern))

        contacts = cursor.fetchall()

        result = []
        for contact_data in contacts:
            contact = Contact(
                phone_number=contact_data[0].split('@')[0],
                name=contact_data[1],
                jid=contact_data[0],
                push_name=contact_data[2] if len(contact_data) > 2 else None,
            )
            result.append(contact)

        return result

    except sqlite3.Error:
        log.exception("query failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> list[Chat]:
    """Get all chats involving the contact.

    Args:
        jid: The contact's JID to search for
        limit: Maximum number of chats to return (default 20)
        page: Page number for pagination (default 0)
    """
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    try:
        conn = _open_ro()
        cursor = conn.cursor()

        # Rewrite of the original `WHERE m.sender = ? OR c.jid = ?` + DISTINCT.
        # The OR forced a full SCAN of messages joined to chats, then a temp
        # B-tree for DISTINCT and another for ORDER BY. Bench on the heaviest
        # sender: 138-163ms.
        # New shape: first materialise the set of relevant chat JIDs via the
        # idx_messages_sender_time index, UNION with the requested jid itself,
        # then join chats + a LEFT JOIN to messages on (chat_jid, last_message_time)
        # to pull the last message for each. ~10x faster on the heavy case.
        cursor.execute("""
            WITH jids AS (
                SELECT chat_jid AS jid FROM messages WHERE sender = ?
                UNION
                SELECT ? AS jid
            )
            SELECT c.jid, c.name, c.last_message_time,
                   m.content, m.sender, m.is_from_me, c.push_name
            FROM jids
            JOIN chats c ON c.jid = jids.jid
            LEFT JOIN messages m
              ON m.chat_jid = c.jid AND m.timestamp = c.last_message_time
            ORDER BY c.last_message_time DESC, c.jid
            LIMIT ? OFFSET ?
        """, (jid, jid, limit, page * limit))

        chats = cursor.fetchall()

        result = []
        for chat_data in chats:
            chat = Chat(
                jid=chat_data[0],
                name=chat_data[1],
                last_message_time=_as_utc(chat_data[2]) if chat_data[2] else None,
                last_message=chat_data[3],
                last_sender=chat_data[4],
                last_is_from_me=chat_data[5],
                push_name=chat_data[6] if len(chat_data) > 6 else None,
            )
            result.append(chat)

        return result

    except sqlite3.Error:
        log.exception("query failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_last_interaction(jid: str) -> str:
    """Get most recent message involving the contact."""
    try:
        conn = _open_ro()
        cursor = conn.cursor()

        # The original `WHERE m.sender = ? OR c.jid = ?` ORDER BY timestamp DESC
        # forced the planner to walk idx_messages_timestamp newest-first and probe
        # the OR predicate per row. For a STALE contact (one that hasn't messaged
        # in months) that walk hit ~70ms before finding the first match.
        # New: two index-ordered seeks (one per branch via idx_messages_sender_time
        # and idx_messages_chat_time), union, pick the latest, then PK lookup.
        # Stale-contact worst case drops to ~0.02ms (~4000x).
        cursor.execute("""
            WITH cand AS (
                SELECT id, chat_jid, timestamp FROM (
                    SELECT id, chat_jid, timestamp FROM messages
                    WHERE sender = ? ORDER BY timestamp DESC LIMIT 1
                )
                UNION ALL
                SELECT id, chat_jid, timestamp FROM (
                    SELECT id, chat_jid, timestamp FROM messages
                    WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT 1
                )
            ),
            pick AS (SELECT id, chat_jid FROM cand ORDER BY timestamp DESC LIMIT 1)
            SELECT m.timestamp, m.sender, c.name, m.content, m.is_from_me,
                   c.jid, m.id, m.media_type
            FROM pick
            JOIN messages m ON m.id = pick.id AND m.chat_jid = pick.chat_jid
            JOIN chats c ON c.jid = m.chat_jid
        """, (jid, jid))

        msg_data = cursor.fetchone()

        if not msg_data:
            return None

        message = Message(
            timestamp=_as_utc(msg_data[0]),
            sender=msg_data[1],
            chat_name=msg_data[2],
            content=msg_data[3],
            is_from_me=msg_data[4],
            chat_jid=msg_data[5],
            id=msg_data[6],
            media_type=msg_data[7]
        )

        return format_message(message)

    except sqlite3.Error:
        log.exception("query failed")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_chat(chat_jid: str, include_last_message: bool = True) -> Chat | None:
    """Get chat metadata by JID."""
    try:
        conn = _open_ro()
        cursor = conn.cursor()

        # Same duplicate-row fix as list_chats: correlated subqueries return
        # exactly one row even when multiple messages share the same UTC second.
        # G1: single rowid lookup beats 3 correlated subqueries.
        if include_last_message:
            sql = """
                SELECT c.jid, c.name, c.last_message_time,
                    m.content, m.sender, m.is_from_me, c.push_name
                FROM chats c
                LEFT JOIN messages m ON m.rowid = (
                    SELECT rowid FROM messages WHERE chat_jid = c.jid ORDER BY timestamp DESC LIMIT 1
                )
                WHERE c.jid = ?
            """
        else:
            sql = """
                SELECT c.jid, c.name, c.last_message_time, NULL, NULL, NULL, c.push_name
                FROM chats c WHERE c.jid = ?
            """

        cursor.execute(sql, (chat_jid,))
        chat_data = cursor.fetchone()

        if not chat_data:
            return None

        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=_as_utc(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5],
            push_name=chat_data[6] if len(chat_data) > 6 else None,
        )

    except sqlite3.Error:
        log.exception("query failed")
        return None
    finally:
        if 'conn' in locals():
            conn.close()


def get_direct_chat_by_contact(sender_phone_number: str) -> Chat | None:
    """Get chat metadata by sender phone number."""
    try:
        conn = _open_ro()
        cursor = conn.cursor()

        # Build a prefix-anchored search when the input is plain digits, which
        # turns the previous full SCAN of chats (leading wildcard LIKE) into an
        # index range seek. Falls back to substring LIKE for free-form input.
        bare = re.sub(r"\D", "", sender_phone_number or "")
        if bare:
            cursor.execute("""
                SELECT c.jid, c.name, c.last_message_time,
                    m.content, m.sender, m.is_from_me, c.push_name
                FROM chats c
                LEFT JOIN messages m ON m.rowid = (
                    SELECT rowid FROM messages WHERE chat_jid = c.jid ORDER BY timestamp DESC LIMIT 1
                )
                WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
                LIMIT 1
            """, (f"{bare}@%",))
        else:
            cursor.execute("""
                SELECT c.jid, c.name, c.last_message_time,
                    m.content, m.sender, m.is_from_me, c.push_name
                FROM chats c
                LEFT JOIN messages m ON m.rowid = (
                    SELECT rowid FROM messages WHERE chat_jid = c.jid ORDER BY timestamp DESC LIMIT 1
                )
                WHERE c.jid LIKE ? AND c.jid NOT LIKE '%@g.us'
                LIMIT 1
            """, (f"%{sender_phone_number}%",))

        chat_data = cursor.fetchone()

        if not chat_data:
            return None

        return Chat(
            jid=chat_data[0],
            name=chat_data[1],
            last_message_time=_as_utc(chat_data[2]) if chat_data[2] else None,
            last_message=chat_data[3],
            last_sender=chat_data[4],
            last_is_from_me=chat_data[5],
            push_name=chat_data[6] if len(chat_data) > 6 else None,
        )

    except sqlite3.Error:
        log.exception("query failed")
        return None
    finally:
        if 'conn' in locals():
            conn.close()

def send_message(
    recipient: str,
    message: str,
    reply_to_message_id: str | None = None,
    reply_to_sender_jid: str | None = None,
    mentioned_jids: list[str] | None = None,
    delivery_timeout_seconds: int = 15,
) -> tuple[bool, str]:
    try:
        if not recipient:
            return False, "Recipient must be provided"
        if message is None:
            return False, "Message body must be provided"
        if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
            return False, f"Message exceeds WhatsApp's {MAX_MESSAGE_BYTES}-byte text limit"

        url = f"{WHATSAPP_API_BASE_URL}/send?delivery_timeout_seconds={int(delivery_timeout_seconds)}"
        payload: dict[str, object] = {"recipient": recipient, "message": message}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_to_sender_jid:
            payload["reply_to_sender_jid"] = reply_to_sender_jid
        if mentioned_jids:
            payload["mentioned_jids"] = list(mentioned_jids)

        # MCP read timeout = bridge delivery timeout + 5s slack for network.
        response = _HTTP_SESSION.post(url, json=payload, timeout=(2, int(delivery_timeout_seconds) + 5))

        if response.status_code == 200:
            result = response.json()
            return result.get("success", False), result.get("message", "Unknown response")
        return False, f"Error: HTTP {response.status_code} - {response.text}"
    except requests.RequestException as e:
        log.exception("send_message HTTP error")
        return False, f"Request error: {e!s}"
    except json.JSONDecodeError:
        return False, f"Error parsing response: {response.text}"


# --- Batch E: Tier-1 feature helpers --------------------------------------
# Each is a thin POST to the matching bridge endpoint. Errors are logged but
# never raised (returning (False, msg) keeps the tools.py contract uniform).

def _bridge_root() -> str:
    """Strip the trailing /api from WHATSAPP_API_BASE_URL to get the bridge root."""
    base = WHATSAPP_API_BASE_URL.rstrip("/")
    return base[: -len("/api")] if base.endswith("/api") else base


def _post_json(path: str, body: dict[str, object], timeout=(2, 15)) -> tuple[int, dict[str, object]]:
    url = f"{_bridge_root()}{path}"
    r = _HTTP_SESSION.post(url, json=body, timeout=timeout)
    try:
        data = r.json()
    except json.JSONDecodeError:
        data = {"success": False, "message": r.text}
    return r.status_code, data


def _get_json(path: str, timeout=5) -> tuple[int, dict[str, object]]:
    url = f"{_bridge_root()}{path}"
    r = _HTTP_SESSION.get(url, timeout=timeout)
    try:
        data = r.json()
    except json.JSONDecodeError:
        data = {"success": False, "message": r.text}
    return r.status_code, data


def bridge_health() -> dict[str, object]:
    try:
        code, data = _get_json("/api/health")
        if code != 200:
            return {"connected": False, "logged_in": False, "error": data}
        return data
    except requests.RequestException as e:
        log.exception("bridge_health failed")
        return {"connected": False, "logged_in": False, "error": str(e)}


def react_to_message(chat_jid: str, message_id: str, emoji: str, sender_jid: str | None = None) -> tuple[bool, str]:
    try:
        body: dict[str, object] = {"chat_jid": chat_jid, "message_id": message_id, "emoji": emoji}
        if sender_jid:
            body["sender_jid"] = sender_jid
        code, data = _post_json("/api/react", body)
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("react_to_message failed")
        return False, f"Request error: {e}"


def edit_message(chat_jid: str, message_id: str, new_text: str) -> tuple[bool, str]:
    try:
        if not new_text:
            return False, "new_text must be non-empty"
        if len(new_text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            return False, f"new_text exceeds WhatsApp's {MAX_MESSAGE_BYTES}-byte limit"
        code, data = _post_json("/api/edit", {
            "chat_jid": chat_jid, "message_id": message_id, "new_text": new_text,
        })
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("edit_message failed")
        return False, f"Request error: {e}"


def delete_message(chat_jid: str, message_id: str, sender_jid: str | None = None) -> tuple[bool, str]:
    try:
        body: dict[str, object] = {"chat_jid": chat_jid, "message_id": message_id}
        if sender_jid:
            body["sender_jid"] = sender_jid
        code, data = _post_json("/api/delete", body)
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("delete_message failed")
        return False, f"Request error: {e}"


def mark_read(chat_jid: str, message_ids: list[str], sender_jid: str | None = None) -> tuple[bool, str]:
    try:
        if not message_ids:
            return False, "message_ids must be non-empty"
        body: dict[str, object] = {"chat_jid": chat_jid, "message_ids": message_ids}
        if sender_jid:
            body["sender_jid"] = sender_jid
        code, data = _post_json("/api/mark_read", body)
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("mark_read failed")
        return False, f"Request error: {e}"


def send_presence(chat_jid: str, state: str = "composing", media: str | None = None) -> tuple[bool, str]:
    try:
        body: dict[str, object] = {"chat_jid": chat_jid, "state": state}
        if media:
            body["media"] = media
        code, data = _post_json("/api/presence", body)
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("send_presence failed")
        return False, f"Request error: {e}"


def get_profile_picture(jid: str, preview: bool = False) -> dict[str, object]:
    try:
        code, data = _post_json("/api/profile_picture", {"jid": jid, "preview": preview})
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("get_profile_picture failed")
        return {"success": False, "message": f"Request error: {e}"}


def send_location(recipient: str, latitude: float, longitude: float,
                  name: str | None = None, address: str | None = None) -> tuple[bool, str]:
    try:
        body: dict[str, object] = {
            "recipient": recipient,
            "latitude": float(latitude),
            "longitude": float(longitude),
        }
        if name:
            body["name"] = name
        if address:
            body["address"] = address
        code, data = _post_json("/api/send_location", body)
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("send_location failed")
        return False, f"Request error: {e}"


def set_disappearing(chat_jid: str, seconds: int) -> tuple[bool, str]:
    try:
        if seconds < 0:
            return False, "seconds must be >= 0 (0 disables)"
        code, data = _post_json("/api/set_disappearing", {
            "chat_jid": chat_jid, "seconds": int(seconds),
        })
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("set_disappearing failed")
        return False, f"Request error: {e}"


def create_poll(recipient: str, name: str, options: list[str],
                selectable_options_count: int = 1) -> tuple[bool, str]:
    try:
        if not options or len(options) < 2:
            return False, "options must contain at least 2 entries"
        body: dict[str, object] = {
            "recipient": recipient,
            "name": name,
            "options": list(options),
            "selectable_options_count": max(1, int(selectable_options_count)),
        }
        code, data = _post_json("/api/create_poll", body)
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("create_poll failed")
        return False, f"Request error: {e}"


# --- Batch I: group ops + blocklist + newsletters -------------------------

def create_group(subject: str, participants: list[str]) -> dict[str, object]:
    try:
        if not subject or not participants:
            return {"success": False, "message": "subject and participants required"}
        code, data = _post_json("/api/group/create", {
            "subject": subject, "participants": list(participants),
        }, timeout=(2, 30))
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("create_group failed")
        return {"success": False, "message": f"Request error: {e}"}


def update_group_participants(group_jid: str, action: str, participants: list[str]) -> tuple[bool, str]:
    try:
        if action not in ("add", "remove", "promote", "demote"):
            return False, "action must be add|remove|promote|demote"
        code, data = _post_json("/api/group/participants", {
            "group_jid": group_jid, "action": action, "participants": list(participants),
        }, timeout=(2, 30))
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("update_group_participants failed")
        return False, f"Request error: {e}"


def get_group_info(group_jid: str) -> dict[str, object]:
    try:
        code, data = _post_json("/api/group/info", {"group_jid": group_jid})
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("get_group_info failed")
        return {"success": False, "message": f"Request error: {e}"}


def list_joined_groups() -> dict[str, object]:
    try:
        code, data = _get_json("/api/group/joined", timeout=15)
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("list_joined_groups failed")
        return {"success": False, "message": f"Request error: {e}"}


def get_blocklist() -> dict[str, object]:
    try:
        code, data = _get_json("/api/blocklist")
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("get_blocklist failed")
        return {"success": False, "message": f"Request error: {e}"}


def block_user(jid: str) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/block", {"jid": jid})
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("block_user failed")
        return False, f"Request error: {e}"


def unblock_user(jid: str) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/unblock", {"jid": jid})
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("unblock_user failed")
        return False, f"Request error: {e}"


def list_subscribed_newsletters() -> dict[str, object]:
    try:
        code, data = _get_json("/api/newsletters/subscribed", timeout=15)
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("list_subscribed_newsletters failed")
        return {"success": False, "message": f"Request error: {e}"}


def backfill_group_participants() -> dict[str, object]:
    """Batch omicron: manual trigger for the group_participants backfill.
    Runs a single SQL that populates the table from existing history.
    """
    try:
        r = _HTTP_SESSION.post(f"{_bridge_root()}/api/backfill/group_participants", timeout=(2, 300))
        if r.status_code == 200:
            return r.json()
        return {"success": False, "message": r.text}
    except requests.RequestException as e:
        log.exception("backfill_group_participants failed")
        return {"success": False, "message": f"Request error: {e}"}


def sender_activity(sender_jid: str, days: int = 30) -> dict[str, object]:
    """Batch omicron: cross-chat activity for a specific sender."""
    days = max(1, min(int(days or 1), 3650))
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(sep=" ")
    target = sender_jid if "@" in sender_jid else sender_jid
    try:
        conn = _open_ro()
        cur = conn.cursor()
        total = cur.execute(
            "SELECT COUNT(*) FROM messages WHERE sender = ? AND timestamp > ?",
            (target, since),
        ).fetchone()[0]
        by_chat = cur.execute(
            """SELECT m.chat_jid, COALESCE(NULLIF(c.name,''), c.push_name), COUNT(*) AS n
               FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
               WHERE m.sender = ? AND m.timestamp > ?
               GROUP BY m.chat_jid ORDER BY n DESC LIMIT 20""",
            (target, since),
        ).fetchall()
        by_day = cur.execute(
            """SELECT DATE(timestamp), COUNT(*) FROM messages
               WHERE sender = ? AND timestamp > ?
               GROUP BY DATE(timestamp) ORDER BY DATE(timestamp) ASC""",
            (target, since),
        ).fetchall()
        return {
            "sender": target,
            "days": days,
            "total_messages": total,
            "by_chat": [{"chat_jid": r[0], "chat_name": r[1], "count": r[2]} for r in by_chat],
            "by_day": [{"date": r[0], "count": r[1]} for r in by_day],
        }
    except sqlite3.Error as e:
        log.exception("sender_activity failed")
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()


def list_all_media_by_type(media_type: str, limit: int = 50, before: str | None = None) -> list[dict[str, object]]:
    """Batch omicron: cross-chat media search by type (image/video/audio/document)."""
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    where = ["media_type = ?"]
    params: list = [media_type]
    if before:
        where.append("timestamp < ?"); params.append(before)
    params.append(limit)
    try:
        conn = _open_ro()
        rows = conn.execute(
            f"""SELECT m.id, m.chat_jid, COALESCE(NULLIF(c.name,''), c.push_name),
                       m.sender, m.timestamp, m.filename, COALESCE(m.file_length, 0)
                FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
                WHERE {' AND '.join(where)}
                ORDER BY m.timestamp DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [{"id": r[0], "chat_jid": r[1], "chat_name": r[2],
                 "sender": r[3], "timestamp": r[4], "filename": r[5],
                 "file_length": r[6]} for r in rows]
    except sqlite3.Error:
        log.exception("list_all_media_by_type failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def list_view_once(chat_jid: str | None = None, limit: int = 50,
                   before: str | None = None) -> list[dict[str, object]]:
    """View-once messages, newest first, optionally scoped to one chat.

    The media stays fetchable after the single view has been burned: WhatsApp
    enforces view-once client-side only, so the CDN blob and media_key outlive
    it. This just tells you WHICH rows those are - the bridge unwraps the
    envelope before storing, so without the recorded flag they are
    indistinguishable from ordinary media.

    Forward-only: rows written before the view_once column existed report
    False, because raw_proto is marshalled post-unwrap and no longer carries
    the envelope to back-fill from.
    """
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    where = ["m.view_once = 1"]
    params: list = []
    if chat_jid:
        where.append("m.chat_jid = ?"); params.append(chat_jid)
    if before:
        where.append("m.timestamp < ?"); params.append(before)
    params.append(limit)
    try:
        conn = _open_ro()
        rows = conn.execute(
            f"""SELECT m.id, m.chat_jid, COALESCE(NULLIF(c.name,''), c.push_name),
                       m.sender, m.timestamp, m.media_type, m.filename,
                       COALESCE(m.file_length, 0), m.is_from_me,
                       COALESCE(m.content, '')
                FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
                WHERE {' AND '.join(where)}
                ORDER BY m.timestamp DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [{"id": r[0], "chat_jid": r[1], "chat_name": r[2],
                 "sender": r[3], "timestamp": r[4], "media_type": r[5],
                 "filename": r[6], "file_length": r[7],
                 "is_from_me": bool(r[8]), "caption": r[9]} for r in rows]
    except sqlite3.Error:
        log.exception("list_view_once failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def search_by_reaction(emoji: str, limit: int = 50) -> list[dict[str, object]]:
    """Batch omicron: messages that received this reaction emoji."""
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    try:
        conn = _open_ro()
        rows = conn.execute(
            """SELECT r.target_message_id, r.chat_jid, r.sender, r.timestamp,
                      m.content, m.sender AS orig_sender
               FROM reactions r
               LEFT JOIN messages m ON m.id = r.target_message_id AND m.chat_jid = r.chat_jid
               WHERE r.emoji = ?
               ORDER BY r.timestamp DESC LIMIT ?""",
            (emoji, limit),
        ).fetchall()
        return [{"message_id": r[0], "chat_jid": r[1], "reactor": r[2],
                 "reacted_at": r[3], "original_content": r[4], "original_sender": r[5]} for r in rows]
    except sqlite3.Error:
        log.exception("search_by_reaction failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_bridge_diagnostics() -> dict[str, object]:
    """Batch omicron: extended /api/bridge/diagnostics."""
    try:
        r = _HTTP_SESSION.get(f"{_bridge_root()}/api/bridge/diagnostics", timeout=10)
        if r.status_code == 200:
            return r.json()
        return {"success": False, "message": r.text}
    except requests.RequestException as e:
        log.exception("get_bridge_diagnostics failed")
        return {"success": False, "message": f"Request error: {e}"}


def get_replies_to(message_id: str, chat_jid: str, limit: int = 50) -> list[dict[str, object]]:
    """Batch xi: messages that quote the given message via reply_to_message_id."""
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    try:
        conn = _open_ro()
        rows = conn.execute(
            """SELECT m.id, m.sender, m.content, m.timestamp, m.media_type,
                      COALESCE(NULLIF(c.name,''), c.push_name) AS sender_name
               FROM messages m LEFT JOIN chats c ON c.jid = m.sender
               WHERE m.reply_to_message_id = ? AND m.chat_jid = ?
               ORDER BY m.timestamp ASC LIMIT ?""",
            (message_id, chat_jid, limit),
        ).fetchall()
        return [{"id": r[0], "sender": r[1], "content": r[2],
                 "timestamp": r[3], "media_type": r[4],
                 "sender_name": r[5]} for r in rows]
    except sqlite3.Error:
        log.exception("get_replies_to failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_message_thread(chat_jid: str, message_id: str) -> dict[str, object]:
    """Batch xi: original message + direct replies as a single object."""
    try:
        conn = _open_ro()
        row = conn.execute(
            """SELECT m.timestamp, m.sender, m.content, m.is_from_me, m.media_type,
                      COALESCE(NULLIF(c.name,''), c.push_name)
               FROM messages m LEFT JOIN chats c ON c.jid = m.sender
               WHERE m.id = ? AND m.chat_jid = ?""",
            (message_id, chat_jid),
        ).fetchone()
        if not row:
            return {"error": "message not found"}
        original = {
            "id": message_id, "chat_jid": chat_jid,
            "timestamp": row[0], "sender": row[1], "content": row[2],
            "is_from_me": bool(row[3]), "media_type": row[4], "sender_name": row[5],
        }
        replies = get_replies_to(message_id, chat_jid, limit=100)
        return {"original": original, "replies": replies, "reply_count": len(replies)}
    except sqlite3.Error as e:
        log.exception("get_message_thread failed")
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()


def groups_with_member(jid: str, limit: int = 50) -> list[dict[str, object]]:
    """Batch xi: which groups contain this JID (based on messages seen)."""
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    try:
        conn = _open_ro()
        # Accept either bare number or full JID
        target = jid if "@" in jid else f"{jid}@s.whatsapp.net"
        rows = conn.execute(
            """SELECT g.group_jid,
                      COALESCE(NULLIF(c.name,''), c.push_name),
                      g.is_admin, g.is_super_admin, g.first_seen_at, g.last_seen_at
               FROM group_participants g
               LEFT JOIN chats c ON c.jid = g.group_jid
               WHERE g.jid = ?
               ORDER BY g.last_seen_at DESC LIMIT ?""",
            (target, limit),
        ).fetchall()
        return [{"group_jid": r[0], "group_name": r[1],
                 "is_admin": bool(r[2]), "is_super_admin": bool(r[3]),
                 "first_seen_at": r[4], "last_seen_at": r[5]} for r in rows]
    except sqlite3.Error:
        log.exception("groups_with_member failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def list_group_members(group_jid: str, limit: int = 200) -> list[dict[str, object]]:
    """Batch xi: members of a group, based on message activity."""
    limit = max(1, min(int(limit or 1), 500))
    try:
        conn = _open_ro()
        rows = conn.execute(
            """SELECT g.jid,
                      COALESCE(NULLIF(c.name,''), c.push_name),
                      g.is_admin, g.is_super_admin, g.first_seen_at, g.last_seen_at
               FROM group_participants g
               LEFT JOIN chats c ON c.jid = g.jid
               WHERE g.group_jid = ?
               ORDER BY g.last_seen_at DESC LIMIT ?""",
            (group_jid, limit),
        ).fetchall()
        return [{"jid": r[0], "name": r[1],
                 "is_admin": bool(r[2]), "is_super_admin": bool(r[3]),
                 "first_seen_at": r[4], "last_seen_at": r[5]} for r in rows]
    except sqlite3.Error:
        log.exception("list_group_members failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_reactions_on(message_id: str, chat_jid: str) -> dict[str, object]:
    """Batch nu: read reactions on a specific message."""
    try:
        r = _HTTP_SESSION.get(
            f"{_bridge_root()}/api/reactions",
            params={"target_message_id": message_id, "chat_jid": chat_jid},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        try:
            return {"success": False, "message": r.json().get("message", r.text)}
        except json.JSONDecodeError:
            return {"success": False, "message": r.text}
    except requests.RequestException as e:
        log.exception("get_reactions_on failed")
        return {"success": False, "message": f"Request error: {e}"}


def get_chat_stats(chat_jid: str) -> dict[str, object]:
    """Batch nu: message count, top senders, media breakdown, date range."""
    try:
        conn = _open_ro()
        cur = conn.cursor()
        # aggregate in one pass
        row = cur.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN is_from_me = 1 THEN 1 ELSE 0 END),
                      COUNT(DISTINCT sender),
                      MIN(timestamp), MAX(timestamp),
                      SUM(CASE WHEN media_type IS NOT NULL AND media_type != '' THEN 1 ELSE 0 END)
                 FROM messages WHERE chat_jid = ?""",
            (chat_jid,),
        ).fetchone()
        total, from_me, distinct_senders, first_ts, last_ts, media_count = row or (0, 0, 0, None, None, 0)
        # top 5 senders
        top = cur.execute(
            """SELECT sender, COUNT(*) AS n FROM messages
               WHERE chat_jid = ? AND sender != ''
               GROUP BY sender ORDER BY n DESC LIMIT 5""",
            (chat_jid,),
        ).fetchall()
        # media breakdown
        media_breakdown = cur.execute(
            """SELECT media_type, COUNT(*) AS n FROM messages
               WHERE chat_jid = ? AND media_type IS NOT NULL AND media_type != ''
               GROUP BY media_type ORDER BY n DESC""",
            (chat_jid,),
        ).fetchall()
        return {
            "chat_jid": chat_jid,
            "total_messages": total or 0,
            "from_me": from_me or 0,
            "distinct_senders": distinct_senders or 0,
            "first_message_ts": first_ts,
            "last_message_ts": last_ts,
            "media_message_count": media_count or 0,
            "top_senders": [{"sender": s, "count": n} for s, n in top],
            "media_breakdown": [{"media_type": m, "count": n} for m, n in media_breakdown],
        }
    except sqlite3.Error as e:
        log.exception("get_chat_stats failed")
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()


def get_top_active_chats(hours: int = 24, limit: int = 10) -> list[dict[str, object]]:
    """Batch nu: chats sorted by message count in the last N hours."""
    hours = max(1, min(int(hours or 1), 24 * 90))
    limit = max(1, min(int(limit or 1), 100))
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(sep=" ")
    try:
        conn = _open_ro()
        rows = conn.execute(
            """SELECT m.chat_jid, c.name, c.push_name, COUNT(*) AS msg_count
               FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
               WHERE m.timestamp > ?
               GROUP BY m.chat_jid
               ORDER BY msg_count DESC
               LIMIT ?""",
            (since, limit),
        ).fetchall()
        return [{"chat_jid": r[0], "name": r[1], "push_name": r[2], "count": r[3]} for r in rows]
    except sqlite3.Error:
        log.exception("get_top_active_chats failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_quiet_chats(days: int = 30, limit: int = 20) -> list[dict[str, object]]:
    """Batch nu: chats we haven't heard from in the last N days."""
    days = max(1, min(int(days or 1), 3650))
    limit = max(1, min(int(limit or 1), 100))
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(sep=" ")
    try:
        conn = _open_ro()
        rows = conn.execute(
            """SELECT jid, name, push_name, last_message_time
               FROM chats
               WHERE last_message_time IS NOT NULL AND last_message_time < ?
               ORDER BY last_message_time ASC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()
        return [{"chat_jid": r[0], "name": r[1], "push_name": r[2],
                 "last_message_ts": r[3]} for r in rows]
    except sqlite3.Error:
        log.exception("get_quiet_chats failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def count_messages(chat_jid: str | None = None,
                   sender: str | None = None,
                   media_type: str | None = None,
                   after: str | None = None,
                   before: str | None = None) -> dict[str, object]:
    """Batch nu: cheap counting endpoint. All filters optional."""
    try:
        conn = _open_ro()
        clauses, params = [], []
        if chat_jid:
            clauses.append("chat_jid = ?"); params.append(chat_jid)
        if sender:
            clauses.append("sender = ?"); params.append(sender)
        if media_type is not None:
            if media_type == "":
                clauses.append("(media_type IS NULL OR media_type = '')")
            else:
                clauses.append("media_type = ?"); params.append(media_type)
        if after:
            clauses.append("timestamp > ?"); params.append(after)
        if before:
            clauses.append("timestamp < ?"); params.append(before)
        sql = "SELECT COUNT(*) FROM messages"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        (n,) = conn.execute(sql, tuple(params)).fetchone()
        return {"count": n}
    except sqlite3.Error as e:
        log.exception("count_messages failed")
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()


def messages_by_day(chat_jid: str, days: int = 30) -> list[dict[str, object]]:
    """Batch nu: histogram of message counts by day for a chat."""
    days = max(1, min(int(days or 1), 3650))
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(sep=" ")
    try:
        conn = _open_ro()
        rows = conn.execute(
            """SELECT DATE(timestamp) AS d, COUNT(*)
               FROM messages
               WHERE chat_jid = ? AND timestamp > ?
               GROUP BY d ORDER BY d ASC""",
            (chat_jid, since),
        ).fetchall()
        return [{"date": r[0], "count": r[1]} for r in rows]
    except sqlite3.Error:
        log.exception("messages_by_day failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def export_chat(chat_jid: str, since: str | None = None,
                fmt: str = "markdown", limit: int = 500) -> dict[str, object]:
    """Batch nu: LLM-friendly chat export. fmt in {markdown, text, json}."""
    limit = max(1, min(int(limit or 1), 5000))
    where = ["chat_jid = ?"]
    params: list = [chat_jid]
    if since:
        where.append("timestamp > ?"); params.append(since)
    try:
        conn = _open_ro()
        cur = conn.cursor()
        chat_name = ""
        row = cur.execute("SELECT name, push_name FROM chats WHERE jid = ?", (chat_jid,)).fetchone()
        if row:
            chat_name = row[0] or row[1] or chat_jid
        rows = cur.execute(
            f"""SELECT timestamp, sender, is_from_me, content, media_type
                FROM messages WHERE {' AND '.join(where)}
                ORDER BY timestamp ASC LIMIT ?""",
            (*tuple(params), limit),
        ).fetchall()
        # resolve sender names in one batch
        sender_jids = {r[1] for r in rows if r[1]}
        name_map = {}
        if sender_jids:
            ph = ",".join("?" * len(sender_jids))
            for jid, nm in cur.execute(
                f"SELECT jid, COALESCE(NULLIF(name,''), push_name) FROM chats WHERE jid IN ({ph})",
                tuple(sender_jids),
            ).fetchall():
                if nm:
                    name_map[jid] = nm
        if fmt == "json":
            body = [{
                "timestamp": r[0], "sender": r[1], "sender_name": name_map.get(r[1]),
                "is_from_me": bool(r[2]), "content": r[3], "media_type": r[4],
            } for r in rows]
            return {"chat_jid": chat_jid, "chat_name": chat_name, "count": len(body),
                    "format": "json", "content": body}
        # text / markdown share a formatter
        lines = []
        if fmt == "markdown":
            lines.append(f"# {chat_name}")
            lines.append(f"_{chat_jid}_ - {len(rows)} messages")
            lines.append("")
        for r in rows:
            who = "Me" if r[2] else (name_map.get(r[1]) or r[1] or "?")
            tag = f" [{r[4]}]" if r[4] else ""
            if fmt == "markdown":
                lines.append(f"**{who}** ({r[0]}){tag}: {r[3] or ''}")
            else:
                lines.append(f"[{r[0]}] {who}{tag}: {r[3] or ''}")
        return {"chat_jid": chat_jid, "chat_name": chat_name, "count": len(rows),
                "format": fmt, "content": "\n".join(lines)}
    except sqlite3.Error as e:
        log.exception("export_chat failed")
        return {"error": str(e)}
    finally:
        if 'conn' in locals():
            conn.close()


def send_sticker(recipient: str,
                 media_path: str | None = None,
                 media_url: str | None = None,
                 media_data: str | None = None,
                 filename: str | None = None) -> tuple[bool, str]:
    """Send a WebP sticker. Same path/url/base64 flexibility as send_file."""
    try:
        if not recipient:
            return False, "Recipient must be provided"
        body, fname = _bytes_from_sources(media_path, media_url, media_data, filename)
        if not fname.lower().endswith(".webp"):
            log.warning("send_sticker: file does not end in .webp (%s) - WA may still accept but may render as image", fname)
        files = {"media": (fname or "sticker.webp", body)}
        r = _HTTP_SESSION.post(f"{_bridge_root()}/api/send_sticker",
                               data={"recipient": recipient}, files=files, timeout=(2, 60))
        if r.status_code == 200:
            d = r.json()
            return d.get("success", True), str(d.get("message", ""))
        try:
            return False, f"HTTP {r.status_code}: {r.json().get('message', r.text)}"
        except json.JSONDecodeError:
            return False, f"HTTP {r.status_code}: {r.text}"
    except ValueError as e:
        return False, str(e)
    except requests.RequestException as e:
        log.exception("send_sticker failed")
        return False, f"Request error: {e}"


def get_poll_results(message_id: str, chat_jid: str) -> dict[str, object]:
    """Tally a poll: per-option counts plus who voted for what.

    Only knows polls the bridge has seen since it started recording them
    (2026-07-16). Votes are E2E-encrypted against the poll creation message, so
    a poll from before then has no stored option->hash mapping and its votes
    cannot be resolved.
    """
    try:
        r = _HTTP_SESSION.get(
            f"{_bridge_root()}/api/poll/results",
            params={"message_id": message_id, "chat_jid": chat_jid},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        try:
            return {"success": False, "message": r.json().get("message", r.text)}
        except json.JSONDecodeError:
            return {"success": False, "message": r.text}
    except requests.RequestException as e:
        log.exception("get_poll_results failed")
        return {"success": False, "message": f"Request error: {e}"}


def vote_in_poll(poll_chat_jid: str, poll_message_id: str,
                 option_names: list[str], poll_sender_jid: str | None = None) -> tuple[bool, str]:
    try:
        body: dict[str, object] = {
            "poll_chat_jid": poll_chat_jid,
            "poll_message_id": poll_message_id,
            "option_names": list(option_names),
        }
        if poll_sender_jid:
            body["poll_sender_jid"] = poll_sender_jid
        code, data = _post_json("/api/poll/vote", body, timeout=(2, 30))
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("vote_in_poll failed")
        return False, f"Request error: {e}"


def get_bridge_stats() -> dict[str, object]:
    try:
        r = _HTTP_SESSION.get(f"{_bridge_root()}/api/bridge/stats", timeout=10)
        if r.status_code == 200:
            return r.json()
        return {"success": False, "message": r.text}
    except requests.RequestException as e:
        log.exception("get_bridge_stats failed")
        return {"success": False, "message": f"Request error: {e}"}


def get_media_info(message_id: str, chat_jid: str) -> dict[str, object]:
    try:
        r = _HTTP_SESSION.get(
            f"{_bridge_root()}/api/media/info",
            params={"message_id": message_id, "chat_jid": chat_jid},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        try:
            return {"success": False, "message": r.json().get("message", r.text)}
        except json.JSONDecodeError:
            return {"success": False, "message": r.text}
    except requests.RequestException as e:
        log.exception("get_media_info failed")
        return {"success": False, "message": f"Request error: {e}"}


def archive_chat(chat_jid: str, archive: bool = True) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/chat/archive", {"chat_jid": chat_jid, "value": bool(archive)})
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("archive_chat failed")
        return False, f"Request error: {e}"


def mark_chat_unread(chat_jid: str, unread: bool = True) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/chat/mark_unread", {"chat_jid": chat_jid, "value": bool(unread)})
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("mark_chat_unread failed")
        return False, f"Request error: {e}"


def set_status_message(message: str) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/status/set_message", {"message": message}, timeout=(2, 15))
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("set_status_message failed")
        return False, f"Request error: {e}"


def list_chats_by_state(state: str, limit: int = 50) -> list[dict[str, object]]:
    """batch iota: read-side of chat state. state in {pinned, muted, archived, unread}."""
    col_map = {
        "pinned":   "is_pinned = 1",
        "muted":    "is_muted = 1",
        "archived": "is_archived = 1",
        "unread":   "mark_unread = 1",
    }
    if state not in col_map:
        return []
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    try:
        conn = _open_ro()
        cur = conn.cursor()
        cur.execute(
            f"SELECT jid, name, push_name, last_message_time, is_pinned, is_muted, mute_end_ts, "
            f"       is_archived, mark_unread "
            f"FROM chats WHERE {col_map[state]} "
            f"ORDER BY last_message_time DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        keys = ["jid", "name", "push_name", "last_message_time",
                "is_pinned", "is_muted", "mute_end_ts", "is_archived", "mark_unread"]
        return [dict(zip(keys, r, strict=False)) for r in rows]
    except sqlite3.Error:
        log.exception("list_chats_by_state failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def delete_chat(chat_jid: str, delete_media: bool = False) -> tuple[bool, str]:
    """Delete a chat via appstate.BuildDeleteChat. Bridge also erases local rows."""
    try:
        code, data = _post_json("/api/chat/delete",
                                {"chat_jid": chat_jid, "delete_media": bool(delete_media)})
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("delete_chat failed")
        return False, f"Request error: {e}"


def edit_label(label_id: str, label_name: str = "", label_color: int = 0,
               delete: bool = False) -> tuple[bool, str]:
    try:
        body = {"label_id": label_id, "label_name": label_name,
                "label_color": int(label_color), "delete": bool(delete)}
        code, data = _post_json("/api/labels/edit", body)
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("edit_label failed")
        return False, f"Request error: {e}"


def set_chat_label(label_id: str, chat_jid: str, labeled: bool = True) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/labels/chat",
                                {"label_id": label_id, "chat_jid": chat_jid, "labeled": bool(labeled)})
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("set_chat_label failed")
        return False, f"Request error: {e}"


def set_message_label(label_id: str, chat_jid: str, message_id: str,
                     labeled: bool = True) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/labels/message",
                                {"label_id": label_id, "chat_jid": chat_jid,
                                 "message_id": message_id, "labeled": bool(labeled)})
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("set_message_label failed")
        return False, f"Request error: {e}"


def mute_chat(chat_jid: str, mute: bool = True, duration_seconds: int = 0) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/chat/mute",
                                {"chat_jid": chat_jid, "value": bool(mute), "duration_s": int(duration_seconds)})
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("mute_chat failed")
        return False, f"Request error: {e}"


def pin_chat(chat_jid: str, pin: bool = True) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/chat/pin", {"chat_jid": chat_jid, "value": bool(pin)})
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("pin_chat failed")
        return False, f"Request error: {e}"


def star_message(chat_jid: str, message_id: str, starred: bool = True,
                 sender_jid: str | None = None, is_from_me: bool = False) -> tuple[bool, str]:
    try:
        body = {
            "chat_jid": chat_jid, "message_id": message_id,
            "starred": bool(starred), "is_from_me": bool(is_from_me),
        }
        if sender_jid:
            body["sender_jid"] = sender_jid
        code, data = _post_json("/api/message/star", body)
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("star_message failed")
        return False, f"Request error: {e}"


def post_status(message: str) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/status/post", {"message": message}, timeout=(2, 30))
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("post_status failed")
        return False, f"Request error: {e}"


def get_privacy_settings() -> dict[str, object]:
    try:
        r = _HTTP_SESSION.get(f"{_bridge_root()}/api/privacy/settings", timeout=10)
        if r.status_code == 200:
            return r.json()
        try:
            return {"success": False, "message": r.json().get("message", r.text)}
        except json.JSONDecodeError:
            return {"success": False, "message": r.text}
    except requests.RequestException as e:
        log.exception("get_privacy_settings failed")
        return {"success": False, "message": f"Request error: {e}"}


def forward_message(source_chat_jid: str, message_id: str, target_jid: str) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/forward",
                                {"source_chat_jid": source_chat_jid, "message_id": message_id, "target_jid": target_jid},
                                timeout=(2, 60))
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("forward_message failed")
        return False, f"Request error: {e}"


def get_message_receipts(message_id: str, chat_jid: str) -> dict[str, object]:
    """G2: return delivered_at / read_at / played_at for one message."""
    try:
        r = _HTTP_SESSION.get(
            f"{_bridge_root()}/api/message_receipts",
            params={"message_id": message_id, "chat_jid": chat_jid},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json()
        try:
            return {"success": False, "message": r.json().get("message", r.text)}
        except json.JSONDecodeError:
            return {"success": False, "message": r.text}
    except requests.RequestException as e:
        log.exception("get_message_receipts failed")
        return {"success": False, "message": f"Request error: {e}"}


def check_phones_on_whatsapp(phones: list[str]) -> dict[str, object]:
    try:
        code, data = _post_json("/api/contacts/check", {"phones": list(phones)})
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("check_phones_on_whatsapp failed")
        return {"success": False, "message": f"Request error: {e}"}


def get_user_info_bulk(jids: list[str]) -> dict[str, object]:
    try:
        code, data = _post_json("/api/users/info", {"jids": list(jids)})
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("get_user_info_bulk failed")
        return {"success": False, "message": f"Request error: {e}"}


def get_business_profile(jid: str) -> dict[str, object]:
    try:
        r = _HTTP_SESSION.get(f"{_bridge_root()}/api/business/profile", params={"jid": jid}, timeout=15)
        if r.status_code != 200:
            try:
                return {"success": False, "message": r.json().get("message", r.text)}
            except json.JSONDecodeError:
                return {"success": False, "message": r.text}
        return r.json()
    except requests.RequestException as e:
        log.exception("get_business_profile failed")
        return {"success": False, "message": f"Request error: {e}"}


def get_group_invite_link(group_jid: str, reset: bool = False) -> dict[str, object]:
    try:
        code, data = _post_json("/api/group/invite_link", {"group_jid": group_jid, "reset": bool(reset)})
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("get_group_invite_link failed")
        return {"success": False, "message": f"Request error: {e}"}


def join_group_by_link(link: str, preview_only: bool = False) -> dict[str, object]:
    try:
        code, data = _post_json("/api/group/join", {"link": link, "preview_only": bool(preview_only)}, timeout=(2, 30))
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("join_group_by_link failed")
        return {"success": False, "message": f"Request error: {e}"}


def leave_group(group_jid: str) -> tuple[bool, str]:
    try:
        code, data = _post_json("/api/group/leave", {"group_jid": group_jid})
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("leave_group failed")
        return False, f"Request error: {e}"


def send_contact_card(recipient: str, contacts: list[dict[str, str]]) -> tuple[bool, str]:
    try:
        if not contacts:
            return False, "at least 1 contact required"
        for c in contacts:
            if not c.get("name") or not c.get("phone"):
                return False, "each contact needs name + phone"
        code, data = _post_json("/api/send_contact", {"recipient": recipient, "contacts": contacts}, timeout=(2, 30))
        return data.get("success", code == 200), str(data.get("message", ""))
    except requests.RequestException as e:
        log.exception("send_contact_card failed")
        return False, f"Request error: {e}"


def mention_everyone(group_jid: str, text: str, delivery_timeout_seconds: int = 15) -> tuple[bool, str]:
    """G14: pure MCP-layer helper - get group members, mention them all, send."""
    try:
        info = get_group_info(group_jid)
        if not info.get("success", True):
            return False, str(info.get("message", "group info failed"))
        participants = info.get("participants") or []
        mentioned = [p.get("jid") for p in participants if p.get("jid")]
        body = " ".join("@" + j.split("@", 1)[0] for j in mentioned) + " " + (text or "")
        return send_message(
            group_jid,
            body,
            mentioned_jids=mentioned,
            delivery_timeout_seconds=delivery_timeout_seconds,
        )
    except Exception as e:
        log.exception("mention_everyone failed")
        return False, f"Error: {e}"


def search_all_messages(
    query: str,
    after: str | None = None,
    before: str | None = None,
    sender_jid: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict[str, object]]:
    """G5: Cross-chat FTS5 search.

    Uses messages_fts MATCH + snippet() for highlighted context across ALL
    chats. Removes the per-chat scope limit that list_messages with query
    imposes.
    """
    if not query or not query.strip():
        return []
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    offset = max(0, int(offset or 0))
    try:
        conn = _open_ro()
        cur = conn.cursor()
        where = ["messages_fts MATCH ?"]
        params: list = [_fts_escape(query)]
        if after:
            try:
                ats = datetime.fromisoformat(after).isoformat(sep=" ")
            except ValueError:
                raise ValueError(f"Invalid after: {after}") from None
            where.append("messages.timestamp > ?")
            params.append(ats)
        if before:
            try:
                bts = datetime.fromisoformat(before).isoformat(sep=" ")
            except ValueError:
                raise ValueError(f"Invalid before: {before}") from None
            where.append("messages.timestamp < ?")
            params.append(bts)
        if sender_jid:
            where.append("messages.sender = ?")
            params.append(sender_jid)
        params.extend([limit, offset])
        sql = (
            "SELECT messages.id, messages.chat_jid, chats.name, messages.sender, "
            "messages.timestamp, messages.content, "
            "snippet(messages_fts, 0, '<mark>', '</mark>', '...', 12) AS preview "
            "FROM messages_fts "
            "JOIN messages ON messages.rowid = messages_fts.rowid "
            "JOIN chats ON messages.chat_jid = chats.jid "
            "WHERE " + " AND ".join(where) + " "
            "ORDER BY messages.timestamp DESC LIMIT ? OFFSET ?"
        )
        cur.execute(sql, tuple(params))
        out: list[dict[str, object]] = []
        for r in cur.fetchall():
            out.append({
                "message_id": r[0],
                "chat_jid":   r[1],
                "chat_name":  r[2],
                "sender":     r[3],
                "timestamp":  r[4],
                "content":    r[5],
                "preview":    r[6],
            })
        return out
    except sqlite3.Error:
        log.exception("search_all_messages failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()


def get_newsletter_info(newsletter_jid: str) -> dict[str, object]:
    try:
        code, data = _post_json("/api/newsletter/info", {"newsletter_jid": newsletter_jid})
        if code != 200:
            return {"success": False, "message": data.get("message", f"HTTP {code}")}
        return data
    except requests.RequestException as e:
        log.exception("get_newsletter_info failed")
        return {"success": False, "message": f"Request error: {e}"}

def _bytes_from_sources(
    media_path: str | None,
    media_url: str | None,
    media_data: str | None,
    filename_hint: str | None = None,
) -> tuple[bytes, str]:
    """Return (bytes, filename) from exactly one of path/url/base64 inputs.

    - media_path: local file on the MCP container's filesystem (sandboxed via
      _is_safe_media_path).
    - media_url:  http(s) URL the MCP container can reach. Fetched server-side
      via _HTTP_SESSION; size capped at 64MB.
    - media_data: base64 string of the raw bytes. Filename should be supplied
      via filename_hint so the bridge picks the right media type.
    """
    import base64
    n_set = sum(1 for x in (media_path, media_url, media_data) if x)
    if n_set != 1:
        raise ValueError("specify exactly one of media_path / media_url / media_data")

    if media_path:
        if not _is_safe_media_path(media_path):
            raise ValueError("media_path is outside the allowed media root; use media_url or media_data instead")
        if not os.path.isfile(media_path):
            raise ValueError(f"Media file not found: {media_path}")
        with open(media_path, "rb") as f:
            data = f.read()
        return data, filename_hint or os.path.basename(media_path)

    if media_url:
        r = _HTTP_SESSION.get(media_url, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            raise ValueError(f"media_url fetch failed: HTTP {r.status_code}")
        body = r.content
        if len(body) > 64 * 1024 * 1024:
            raise ValueError("media_url body exceeds 64MB cap")
        # Derive filename from Content-Disposition, then URL path, then hint.
        fname = filename_hint
        if not fname:
            cd = r.headers.get("Content-Disposition", "")
            if "filename=" in cd:
                fname = cd.split("filename=", 1)[1].strip().strip('"').strip("'")
            if not fname:
                tail = media_url.rsplit("?", 1)[0].rsplit("#", 1)[0].rstrip("/").rsplit("/", 1)[-1]
                fname = tail or "media.bin"
        return body, fname

    # media_data: base64
    try:
        body = base64.b64decode(media_data, validate=True)
    except Exception as e:
        raise ValueError(f"media_data is not valid base64: {e}") from e
    if len(body) > 64 * 1024 * 1024:
        raise ValueError("media_data decoded body exceeds 64MB cap")
    return body, filename_hint or "upload.bin"


def _post_send_bytes(
    recipient: str,
    body: bytes,
    filename: str,
    *,
    message: str = "",
    view_once: bool = False,
    reply_to_message_id: str | None = None,
    reply_to_sender_jid: str | None = None,
    mentioned_jids: list[str] | None = None,
) -> tuple[bool, str]:
    """POST a multipart/form-data to the bridge's /api/send_bytes endpoint."""
    form: dict[str, str] = {"recipient": recipient, "filename": filename}
    if message:
        form["message"] = message
    if view_once:
        form["view_once"] = "true"
    if reply_to_message_id:
        form["reply_to_message_id"] = reply_to_message_id
    if reply_to_sender_jid:
        form["reply_to_sender_jid"] = reply_to_sender_jid
    if mentioned_jids:
        form["mentioned_jids"] = ",".join(mentioned_jids)
    files = {"media": (filename, body)}
    r = _HTTP_SESSION.post(
        f"{_bridge_root()}/api/send_bytes",
        data=form, files=files, timeout=(2, 300),
    )
    if r.status_code == 200:
        d = r.json()
        return d.get("success", True), str(d.get("message", ""))
    try:
        msg = r.json().get("message", r.text)
    except json.JSONDecodeError:
        msg = r.text
    return False, f"HTTP {r.status_code}: {msg}"


def send_view_once_media(
    recipient: str,
    media_path: str | None = None,
    *,
    media_url: str | None = None,
    media_data: str | None = None,
    filename: str | None = None,
) -> tuple[bool, str]:
    """Send an image or video as a 'view once' message.

    Accepts one of: media_path (on the MCP container's filesystem), media_url
    (any URL the MCP can fetch), or media_data (base64-encoded bytes from
    the caller's local file). For files that live on the calling LLM client's
    machine, base64-encode them and pass via media_data with `filename` set
    so the right media type is selected.

    Note: WhatsApp's view-once enforcement is client-side only.
    """
    try:
        if not recipient:
            return False, "recipient required"
        body, fname = _bytes_from_sources(media_path, media_url, media_data, filename)
        return _post_send_bytes(recipient, body, fname, view_once=True)
    except ValueError as e:
        return False, str(e)
    except requests.RequestException as e:
        log.exception("send_view_once_media failed")
        return False, f"Request error: {e}"


def send_file(
    recipient: str,
    media_path: str | None = None,
    *,
    media_url: str | None = None,
    media_data: str | None = None,
    filename: str | None = None,
    message: str = "",
) -> tuple[bool, str]:
    """Send any media via /api/send_bytes (multipart upload to the bridge).

    Accepts EXACTLY ONE source: media_path / media_url / media_data.
    The previous path-only signature only worked for files already on the
    bridge container, which is useless to a remote MCP client.
    """
    try:
        if not recipient:
            return False, "Recipient must be provided"
        body, fname = _bytes_from_sources(media_path, media_url, media_data, filename)
        return _post_send_bytes(recipient, body, fname, message=message)
    except ValueError as e:
        return False, str(e)
    except requests.RequestException as e:
        log.exception("send_file HTTP error")
        return False, f"Request error: {e}"


def send_audio_message(
    recipient: str,
    media_path: str | None = None,
    *,
    media_url: str | None = None,
    media_data: str | None = None,
    filename: str | None = None,
) -> tuple[bool, str]:
    """Send an audio file as a WhatsApp voice message.

    Same path/url/base64 input flexibility as send_file. Non-.ogg sources are
    converted to opus by the MCP container's bundled ffmpeg.
    """
    converted_path: str | None = None
    src_tmp: str | None = None
    try:
        if not recipient:
            return False, "Recipient must be provided"

        body, fname = _bytes_from_sources(media_path, media_url, media_data, filename)

        if not fname.lower().endswith(".ogg"):
            import tempfile
            ext = fname.rsplit(".", 1)[-1] if "." in fname else "tmp"
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(body)
                src_tmp = tmp.name
            try:
                converted_path = audio.convert_to_opus_ogg_temp(src_tmp)
            except Exception as e:
                log.exception("ffmpeg conversion failed")
                return False, f"Error converting file to opus ogg: {e!s}"
            with open(converted_path, "rb") as f:
                body = f.read()
            fname = os.path.basename(converted_path)

        return _post_send_bytes(recipient, body, fname)
    except ValueError as e:
        return False, str(e)
    except requests.RequestException as e:
        log.exception("send_audio HTTP error")
        return False, f"Request error: {e}"
    finally:
        for p in (src_tmp, converted_path):
            if p and os.path.isfile(p):
                with contextlib.suppress(OSError):
                    os.unlink(p)

def download_media(message_id: str, chat_jid: str) -> dict[str, object]:
    """Download media from a message via the bridge.

    Two modes selected by what the bridge returns:
      - <=20MB: bridge sends inline bytes via /api/download_bytes. We return
        {"bytes": <raw>, "mime": ..., "filename": ..., "media_type": ...,
         "size": int}; the caller (download_media tool) wraps as MCP Image/
         Audio/File so the LLM client gets actual viewable content.
      - >20MB:  bridge responds 413. Fall back to /api/download (writes to the
        bridge container's disk) so docker cp can fetch it. Returns
        {"file_path_on_bridge": "...", "size": int, "too_large_for_inline": True}.
    """
    try:
        url = f"{_bridge_root()}/api/download_bytes"
        payload = {"message_id": message_id, "chat_jid": chat_jid}
        r = _HTTP_SESSION.post(url, json=payload, timeout=(2, 120))

        if r.status_code == 200:
            return {
                "bytes": r.content,
                "size": len(r.content),
                "mime": r.headers.get("Content-Type", "application/octet-stream"),
                "filename": r.headers.get("X-Filename", "media"),
                "media_type": r.headers.get("X-Media-Type", ""),
            }

        if r.status_code == 413:
            # Fall back to disk-only download.
            r2 = _HTTP_SESSION.post(f"{_bridge_root()}/api/download", json=payload, timeout=(2, 120))
            if r2.status_code == 200:
                d = r2.json()
                return {
                    "too_large_for_inline": True,
                    "file_path_on_bridge": d.get("path"),
                    "filename": d.get("filename"),
                    "message": f"Media is over 20MB inline cap; downloaded to bridge filesystem. "
                               f"Retrieve with: docker cp whatsapp-bridge:{d.get('path')} <dest>",
                }
            return {"success": False, "message": f"HTTP {r2.status_code}: {r2.text}"}

        try:
            err = r.json().get("message", r.text)
        except json.JSONDecodeError:
            err = r.text
        return {"success": False, "message": f"HTTP {r.status_code}: {err}"}
    except requests.RequestException as e:
        log.exception("download_media failed")
        return {"success": False, "message": f"Request error: {e}"}


def list_media_in_chat(
    chat_jid: str,
    media_type: str | None = None,
    limit: int = 50,
    before_timestamp: str | None = None,
) -> list[dict[str, object]]:
    """Return messages in a chat that carry media (image/video/audio/document).

    Lets the LLM discover what media exists without paging through every text
    message. Returns lightweight records (no bytes); call download_media on
    the message_id to fetch one.
    """
    limit = max(1, min(int(limit or 1), MAX_LIMIT))
    try:
        conn = _open_ro()
        cur = conn.cursor()
        where = ["chat_jid = ?", "media_type IS NOT NULL", "media_type != ''"]
        params: list = [chat_jid]
        if media_type:
            where.append("media_type = ?")
            params.append(media_type)
        if before_timestamp:
            try:
                bts = datetime.fromisoformat(before_timestamp).isoformat(sep=" ")
            except ValueError:
                raise ValueError(f"Invalid before_timestamp: {before_timestamp}") from None
            where.append("timestamp < ?")
            params.append(bts)
        params.append(limit)
        sql = (
            "SELECT id, chat_jid, sender, timestamp, media_type, filename, file_length "
            "FROM messages "
            "WHERE " + " AND ".join(where) + " "
            "ORDER BY timestamp DESC LIMIT ?"
        )
        cur.execute(sql, tuple(params))
        out: list[dict[str, object]] = []
        for r in cur.fetchall():
            out.append({
                "message_id": r[0],
                "chat_jid": r[1],
                "sender": r[2],
                "timestamp": r[3],
                "media_type": r[4],
                "filename": r[5],
                "file_length": r[6],
            })
        return out
    except sqlite3.Error:
        log.exception("list_media_in_chat query failed")
        return []
    finally:
        if 'conn' in locals():
            conn.close()
