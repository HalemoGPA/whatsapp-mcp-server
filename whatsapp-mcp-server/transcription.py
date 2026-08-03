"""Automatic voice-note transcription.

A background worker polls the (read-only) message store for voice notes since a
cutoff that have no transcript yet, fetches + decrypts the audio via
media.fetch_media, transcribes it, and stores the text in a SEPARATE writable
SQLite DB. FTS5 over the transcripts makes voice notes searchable like text.

Why a separate DB rather than a column on `messages`: the message store is
mounted read-only here, and the bridge writes it with INSERT OR REPLACE, so a
transcript column on `messages` would be wiped on any resync - the same trap the
view_once flag hit. Keeping transcripts in their own DB sidesteps both.

Transcription backend: Speechmatics (language=ar, enhanced - proven on real
Egyptian + code-switched audio; English terms come back transliterated into
Arabic script, which reads fine), with KEY FAILOVER across SPEECHMATICS_API_KEYS
and an optional Groq-Whisper floor.

Key strategy is failover, not round-robin. Monthly need (~5h measured) fits
inside one key's 8h free tier, so every key stays under limit regardless of
strategy; failover keeps the extra accounts DORMANT (a smaller multi-account
footprint, which is the only real ToS risk) and is self-correcting - a key that
returns a quota error is parked until the UTC month rolls over. Each transcript
records which engine produced it, so a rougher Groq result is never mistaken for
Speechmatics quality.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, UTC
from pathlib import Path
from collections.abc import Iterator

import requests

log = logging.getLogger("whatsapp_mcp.transcribe")

# --- config ------------------------------------------------------------------
SPEECHMATICS_KEYS: list[str] = [
    k.strip() for k in os.environ.get("SPEECHMATICS_API_KEYS", "").split(",") if k.strip()
]
GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()
LANGUAGE = os.environ.get("TRANSCRIBE_LANGUAGE", "ar")
# Only transcribe notes at or after this instant. Default: 2026-07-15 (yesterday
# relative to when this shipped) so we start fresh and skip the year of backlog.
SINCE = os.environ.get("TRANSCRIBE_SINCE", "2026-07-15 00:00:00")
POLL_SEC = int(os.environ.get("TRANSCRIBE_POLL_SEC", "60"))
# Per-tick cap so a burst of voice notes can't hammer the API (or the quota) in
# one sweep. At the measured ~10 notes/day this is never the binding constraint.
BATCH = int(os.environ.get("TRANSCRIBE_BATCH", "10"))
MESSAGES_DB = os.environ.get("MESSAGES_DB_PATH", "/data/store/messages.db")

SM_ROOT = "https://asr.api.speechmatics.com/v2"
_SM_TIMEOUT = (10, 60)
# Media types the worker transcribes. Videos carry a speech track too - we pull
# the audio out with ffmpeg (already in the image) and feed it the same pipeline,
# so a video's spoken content is transcribed, stored on its own message row, and
# searchable exactly like a voice note.
_VOICE_TYPES = ("audio", "ptt", "voice", "video")
_VIDEO_TYPES = ("video", "gif")

# Arabic search normalisation. FTS token matching is wrong for Arabic: clitics
# (ال/و/ب/ل/ف) attach as prefixes, so "فارماسي" would never match the stored
# "الفارماسي". Substring matching on a normalised copy fixes that AND folds the
# spelling variants Egyptians type inconsistently (alef forms, ة/ه, ى/ي), so
# "انا" and "أنا" search alike.
import re as _re

_AR_FOLD = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ى": "ي", "ئ": "ي", "ة": "ه", "ؤ": "و",
})
_AR_DIACRITICS = _re.compile(r"[ً-ْـ]")  # tanwin/harakat + tatweel


def _norm(s: str) -> str:
    if not s:
        return ""
    return _AR_DIACRITICS.sub("", s.translate(_AR_FOLD)).lower()


# --- storage -----------------------------------------------------------------
_CANDIDATE_PATHS = [
    Path(os.environ.get("TRANSCRIPTS_DB", "")),
    Path("/var/log/wamcp/transcripts.db"),
    Path("/tmp/wamcp-transcripts.db"),
]


def _pick_writable_path() -> Path:
    for p in _CANDIDATE_PATHS:
        if not str(p):
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            probe = str(p) + ".probe"
            with open(probe, "w"):
                pass
            os.unlink(probe)
            return p
        except OSError:
            continue
    return Path("/tmp/wamcp-transcripts.db")


DB_PATH = _pick_writable_path()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    """Commit-and-close context manager. `with sqlite3.connect(...)` scopes a
    transaction but never closes - the leak that took the scheduler down."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _init() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcripts (
                message_id TEXT NOT NULL,
                chat_jid   TEXT NOT NULL,
                text       TEXT,
                norm_text  TEXT,            -- Arabic-folded copy, for search
                engine     TEXT,
                lang       TEXT,
                status     TEXT NOT NULL,   -- done | failed | unavailable
                error      TEXT,
                duration_s REAL,
                created_at TIMESTAMP,
                PRIMARY KEY (message_id, chat_jid)
            );
            CREATE INDEX IF NOT EXISTS idx_tx_status ON transcripts(status);
            -- Key-exhaustion cache: which Speechmatics key is spent, until when.
            CREATE TABLE IF NOT EXISTS key_state (
                key_hash    TEXT PRIMARY KEY,
                exhausted_until TEXT
            );
            """
        )


def _store(message_id: str, chat_jid: str, text: str | None, engine: str | None,
           status: str, error: str | None = None, duration_s: float | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    with _conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO transcripts
               (message_id, chat_jid, text, norm_text, engine, lang, status, error, duration_s, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (message_id, chat_jid, text, _norm(text or ""), engine, LANGUAGE, status, error, duration_s, now),
        )


def already_done(message_id: str, chat_jid: str) -> bool:
    """A note is 'handled' if we succeeded, or failed in a way retrying won't
    fix (unavailable media). Genuine transient failures are left to retry."""
    with _conn() as c:
        row = c.execute(
            "SELECT status FROM transcripts WHERE message_id=? AND chat_jid=?",
            (message_id, chat_jid),
        ).fetchone()
    return bool(row) and row[0] in ("done", "unavailable")


# --- Speechmatics key failover ----------------------------------------------
def _khash(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _month_tag(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(UTC)
    return dt.strftime("%Y-%m")


def _live_keys() -> list[str]:
    """Speechmatics keys not currently parked as quota-exhausted this month."""
    now_month = _month_tag()
    with _conn() as c:
        rows = dict(c.execute("SELECT key_hash, exhausted_until FROM key_state").fetchall())
    live = []
    for k in SPEECHMATICS_KEYS:
        until = rows.get(_khash(k))
        # exhausted_until stores the month tag the key was spent in; it frees up
        # once the month advances.
        if until and until == now_month:
            continue
        live.append(k)
    return live


def _park_key(key: str) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO key_state (key_hash, exhausted_until) VALUES (?,?)",
                  (_khash(key), _month_tag()))
    log.warning("speechmatics key parked (quota) for %s", _month_tag())


class TranscribeError(Exception):
    pass


def _speechmatics(audio_path: Path, key_order: list[str] | None = None) -> tuple[str, float]:
    """Transcribe via Speechmatics, failing over across live keys in the given
    order (defaults to all live keys). Returns (text, duration_seconds).

    A 401/403 or an explicit "quota" message means the key is spent for the
    month -> park it and move on. A 429 is transient back-pressure (common under
    concurrency), NOT monthly exhaustion -> do NOT park; just fail this attempt
    and let a later tick retry. Conflating the two would let parallelism disable
    every key at once."""
    keys = key_order if key_order is not None else _live_keys()
    keys = [k for k in keys if k in set(_live_keys())]  # drop any parked since
    if not keys:
        raise TranscribeError("all speechmatics keys exhausted this month")

    cfg = (
        '{"type":"transcription","transcription_config":'
        f'{{"language":"{LANGUAGE}","operating_point":"enhanced"}}}}'
    )
    last = ""
    for key in keys:
        hdr = {"Authorization": f"Bearer {key}"}
        try:
            with open(audio_path, "rb") as f:
                r = requests.post(f"{SM_ROOT}/jobs/", headers=hdr,
                                  files={"data_file": f}, data={"config": cfg},
                                  timeout=_SM_TIMEOUT)
            if r.status_code in (401, 403) or "quota" in r.text.lower():
                _park_key(key)
                last = f"key spent: {r.status_code}"
                continue
            if r.status_code == 429:
                last = "429 rate-limit (transient)"
                continue
            if r.status_code >= 400:
                last = f"submit HTTP {r.status_code}: {r.text[:160]}"
                continue
            job = r.json().get("id")
            if not job:
                last = f"no job id: {r.text[:160]}"
                continue

            text, dur = _sm_wait(job, hdr)
            return text, dur
        except requests.RequestException as e:
            last = f"request error: {e}"
            continue
    raise TranscribeError(last or "speechmatics failed")


def _sm_wait(job: str, hdr: dict) -> tuple[str, float]:
    for _ in range(60):  # ~5 min ceiling; voice notes finish in seconds
        r = requests.get(f"{SM_ROOT}/jobs/{job}", headers=hdr, timeout=_SM_TIMEOUT)
        j = r.json().get("job", {})
        st = j.get("status")
        if st == "done":
            dur = float(j.get("duration", 0) or 0)
            tr = requests.get(f"{SM_ROOT}/jobs/{job}/transcript?format=txt",
                              headers=hdr, timeout=_SM_TIMEOUT)
            # The transcript endpoint returns text/plain with no charset, so
            # requests would decode this UTF-8 Arabic as latin-1 and mangle it
            # (mojibake that also breaks FTS tokenisation). Decode the raw bytes
            # as UTF-8 explicitly.
            return tr.content.decode("utf-8", errors="replace").strip(), dur
        if st in ("rejected", "deleted", "expired"):
            raise TranscribeError(f"job {st}")
        time.sleep(5)
    raise TranscribeError("job poll timeout")


def _groq(audio_path: Path) -> tuple[str, float]:
    """Fallback floor. Whisper - weaker on Egyptian, but keeps things moving if
    every Speechmatics key is spent. Engine-tagged so the drop is visible."""
    if not GROQ_KEY:
        raise TranscribeError("no groq key configured")
    with open(audio_path, "rb") as f:
        r = requests.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            files={"file": (audio_path.name, f)},
            data={"model": "whisper-large-v3", "language": LANGUAGE},
            timeout=(10, 120),
        )
    if r.status_code >= 400:
        raise TranscribeError(f"groq HTTP {r.status_code}: {r.text[:160]}")
    return r.json().get("text", "").strip(), 0.0


def transcribe_file(audio_path: Path, key_order: list[str] | None = None) -> tuple[str, str, float]:
    """Transcribe one audio file. Returns (text, engine, duration_s). key_order
    lets the parallel worker pin each note to a different key first, so N keys
    run N concurrent jobs (one each) rather than piling onto key 1."""
    try:
        text, dur = _speechmatics(audio_path, key_order=key_order)
        return text, "speechmatics", dur
    except TranscribeError as sm_err:
        if GROQ_KEY:
            try:
                text, dur = _groq(audio_path)
                return text, "groq", dur
            except TranscribeError as g_err:
                raise TranscribeError(f"speechmatics: {sm_err} | groq: {g_err}") from g_err
        raise


# --- worker ------------------------------------------------------------------
def _pending(limit: int) -> list[tuple[str, str]]:
    """Voice notes since SINCE with fetchable media and no handled transcript,
    newest first.

    Must scan the WHOLE since-cutoff set, not a `LIMIT n` newest window: the
    worker processes newest-first, so once the recent notes are done a capped
    query keeps re-finding only those (all done) and never reaches the older
    backlog. Instead we load the handled-id set once (from the transcripts DB)
    and filter the full candidate list against it in memory - the corpus is a
    few hundred rows, this is cheap and correct."""
    with _conn() as c:
        handled = {r[0] for r in c.execute(
            "SELECT message_id FROM transcripts WHERE status IN ('done','unavailable')"
        )}
    ro = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
    try:
        rows = ro.execute(
            f"""SELECT id, chat_jid FROM messages
                WHERE media_type IN ({",".join("?" * len(_VOICE_TYPES))})
                  AND url != '' AND media_key IS NOT NULL
                  AND timestamp >= ?
                ORDER BY timestamp DESC""",
            (*_VOICE_TYPES, SINCE),
        ).fetchall()
    finally:
        ro.close()
    out = []
    for mid, cj in rows:
        if mid not in handled:
            out.append((mid, cj))
        if len(out) >= limit:
            break
    return out


def _extract_audio(video_path: Path) -> Path | None:
    """Pull a mono 16kHz Opus audio track out of a video with ffmpeg. Returns the
    audio path, or None if the video has no usable audio (silent clip / gif)."""
    out = video_path.with_suffix(video_path.suffix + ".aud.ogg")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
             "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", str(out)],
            capture_output=True, timeout=180, check=False,
        )
    except (subprocess.SubprocessError, OSError):
        out.unlink(missing_ok=True)
        return None
    # A silent video yields a tiny/near-empty container; treat that as no audio.
    if out.exists() and out.stat().st_size > 512:
        return out
    out.unlink(missing_ok=True)
    return None


def _process(message_id: str, chat_jid: str, key_order: list[str] | None = None) -> None:
    from media import MediaError, fetch_media  # local import: avoids import cycle
    try:
        info = fetch_media(message_id, chat_jid)
    except MediaError as e:
        # Media gone from the CDN and un-fetchable - retrying won't help.
        _store(message_id, chat_jid, None, None, "unavailable", error=str(e))
        return

    src = info["path"]
    extracted: Path | None = None
    if (info.get("media_type") or "") in _VIDEO_TYPES:
        extracted = _extract_audio(src)
        if extracted is None:
            # Video with no audio track - nothing to transcribe, but it IS
            # handled (mark done/empty so we never retry a silent clip).
            _store(message_id, chat_jid, "", None, "done", duration_s=0.0)
            return
        src = extracted
    try:
        text, engine, dur = transcribe_file(src, key_order=key_order)
        _store(message_id, chat_jid, text, engine, "done", duration_s=dur)
        log.info("transcribed %s (%s) via %s (%.0fs)", message_id,
                 info.get("media_type"), engine, dur)
    except TranscribeError as e:
        # Transient (quota this month, API hiccup) - leave as 'failed' so a later
        # tick retries once a key frees up.
        _store(message_id, chat_jid, None, None, "failed", error=str(e))
        log.warning("transcription failed for %s: %s", message_id, e)
    finally:
        if extracted is not None:
            extracted.unlink(missing_ok=True)


def _worker() -> None:
    from concurrent.futures import ThreadPoolExecutor
    log.info("transcription worker up (since=%s, keys=%d, groq=%s, db=%s)",
             SINCE, len(SPEECHMATICS_KEYS), bool(GROQ_KEY), DB_PATH)
    while True:
        did = 0
        try:
            batch = _pending(BATCH)
            if batch:
                keys = _live_keys()
                # Concurrency = one job per live Speechmatics key (capped), so N
                # keys run N jobs at once with each key seeing a single job -
                # fast, and immune to any per-key concurrency limit. Each note is
                # handed a key_order rotated by its index, so note i starts on
                # key i%N. Groq-only (no SM keys) still parallelises modestly.
                n = max(1, min(len(keys) or 2, 4))
                # `keys` is bound as a default rather than closed over. Today the
                # executor is drained inside this iteration so a closure would be
                # equivalent, but that is a property of the `list(ex.map(...))`
                # below, not of `job`. Binding here means the jobs keep using the
                # key list they were scheduled with even if the drain ever stops
                # being synchronous.
                def job(item, keys=keys):
                    idx, (mid, cj) = item
                    order = keys[idx % len(keys):] + keys[:idx % len(keys)] if keys else None
                    _process(mid, cj, key_order=order)
                with ThreadPoolExecutor(max_workers=n) as ex:
                    list(ex.map(job, enumerate(batch)))
                did = len(batch)
        except Exception:
            log.exception("transcription tick failed")
        # If we just cleared a full batch there is probably more backlog - loop
        # straight back rather than nap, so catch-up runs near-continuously.
        time.sleep(1 if did >= BATCH else POLL_SEC)


_STARTED = threading.Event()


def start_worker() -> None:
    """Idempotent. No keys configured -> stay dormant rather than spin."""
    if not SPEECHMATICS_KEYS and not GROQ_KEY:
        log.info("transcription disabled (no SPEECHMATICS_API_KEYS / GROQ_API_KEY)")
        return
    if _STARTED.is_set():
        return
    _STARTED.set()
    _init()
    threading.Thread(target=_worker, name="wamcp-transcribe", daemon=True).start()


# --- read API (used by MCP tools) -------------------------------------------
def get_transcript(message_id: str, chat_jid: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            """SELECT text, engine, status, error, duration_s, created_at
               FROM transcripts WHERE message_id=? AND chat_jid=?""",
            (message_id, chat_jid),
        ).fetchone()
    if not row:
        return None
    return {"text": row[0], "engine": row[1], "status": row[2],
            "error": row[3], "duration_s": row[4], "transcribed_at": row[5]}


def transcribe_now(message_id: str, chat_jid: str) -> dict:
    """On-demand: transcribe a specific note synchronously (or return cached)."""
    _init()
    cached = get_transcript(message_id, chat_jid)
    if cached and cached["status"] == "done":
        return {"success": True, "cached": True, **cached}
    _process(message_id, chat_jid)
    res = get_transcript(message_id, chat_jid) or {}
    return {"success": res.get("status") == "done", "cached": False, **res}


def _snippet(text: str, norm_query: str, width: int = 60) -> str:
    """A window of the ORIGINAL text around the (normalised) match."""
    pos = _norm(text).find(norm_query)
    if pos < 0:
        return text[:width * 2]
    start = max(0, pos - width // 2)
    end = min(len(text), pos + len(norm_query) + width)
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")


def search(query: str, chat_jid: str | None = None, limit: int = 20) -> list[dict]:
    """Substring search over normalised transcripts (Arabic clitic/spelling
    tolerant). LIKE-scan is fine here - the corpus is at most a few thousand
    voice notes."""
    _init()
    nq = _norm(query)
    if not nq:
        return []
    where = ["status='done'", "norm_text LIKE ? ESCAPE '\\'"]
    like = "%" + nq.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    params: list = [like]
    if chat_jid:
        where.append("chat_jid = ?")
        params.append(chat_jid)
    params.append(limit)
    with _conn() as c:
        rows = c.execute(
            f"""SELECT message_id, chat_jid, text, engine, created_at
                FROM transcripts WHERE {' AND '.join(where)}
                ORDER BY created_at DESC LIMIT ?""",
            tuple(params),
        ).fetchall()
    return [{"message_id": r[0], "chat_jid": r[1], "snippet": _snippet(r[2], nq),
             "engine": r[3], "transcribed_at": r[4]} for r in rows]


def list_nonspeech(chat_jid: str | None = None, after: str | None = None,
                   before: str | None = None, limit: int = 20) -> list[dict]:
    """List voice/video notes that transcribed to NO speech - an empty transcript
    with status='done'. These are sound-only clips (sound effects, music, animal
    noises, laughter, silence) that text search can never match, because there is
    no spoken text. The agent uses this to enumerate candidates and then listen to
    each with view_media to identify it by ear.

    Filter by chat and by message time window (after/before are ISO date/datetime
    strings compared against the message timestamp). Newest message first. Message
    timestamp + sender + duration come from a join on the read-only message store.
    """
    _init()
    with _conn() as c:
        try:
            c.execute("ATTACH ? AS m", (f"file:{MESSAGES_DB}?mode=ro",))
        except sqlite3.OperationalError:
            # URI form not honoured by ATTACH on some builds; fall back to path.
            c.execute("ATTACH ? AS m", (MESSAGES_DB,))
        where = ["t.status = 'done'", "(t.text IS NULL OR t.text = '')"]
        params: list = []
        if chat_jid:
            where.append("t.chat_jid = ?")
            params.append(chat_jid)
        if after:
            where.append("msg.timestamp >= ?")
            params.append(after)
        if before:
            where.append("msg.timestamp <= ?")
            params.append(before)
        params.append(int(limit))
        rows = c.execute(
            f"""SELECT t.message_id, t.chat_jid, msg.sender, msg.timestamp,
                       t.duration_s, msg.media_type
                FROM transcripts t
                JOIN m.messages msg
                  ON msg.id = t.message_id AND msg.chat_jid = t.chat_jid
                WHERE {' AND '.join(where)}
                ORDER BY msg.timestamp DESC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        c.execute("DETACH m")
    return [{"message_id": r[0], "chat_jid": r[1], "sender": r[2],
             "timestamp": r[3], "duration_s": r[4], "media_type": r[5]}
            for r in rows]


def stats() -> dict:
    _init()
    with _conn() as c:
        by = dict(c.execute("SELECT status, COUNT(*) FROM transcripts GROUP BY status").fetchall())
        eng = dict(c.execute(
            "SELECT engine, COUNT(*) FROM transcripts WHERE status='done' GROUP BY engine"
        ).fetchall())
    return {"by_status": by, "by_engine": eng, "since": SINCE,
            "keys_configured": len(SPEECHMATICS_KEYS), "groq_fallback": bool(GROQ_KEY)}
