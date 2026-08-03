"""Media recovery + signed download links.

Solves two distinct problems.

**1. Revoked media is unreachable through the bridge.**
`/api/download{,_bytes}` goes through whatsmeow. When a message is revoked
("delete for everyone") whatsmeow's direct CDN fetch fails, so it falls back to
asking the *sender's phone* for a media retry - which refuses, because they
deleted their copy:

    failed to decrypt media retry notification: media no longer available on phone

That error is misleading: the CDN blob normally outlives the revoke by weeks
(the URL carries its own `oe=` expiry). The `messages` row also survives, since
the bridge has no revoke handler, so `url` + `media_key` + `file_sha256` are all
still on hand. We therefore fetch the ciphertext straight from the CDN and
decrypt it locally, which recovers media the bridge cannot. Verified 2026-07-16
recovering a revoked image.

**2. A remote MCP server cannot write to the client's filesystem.**
The server runs in a container on the box; the Claude/Cursor session runs on the
user's laptop. There is no channel from here to their Downloads folder. So
"download to disk" is delivered as a short-lived HMAC-signed URL: the browser
GETs it and the file lands in Downloads. Same model as an S3 presigned URL, and
the same caveats apply - the URL *is* the credential, so it is high-entropy,
signed with a key derived from (never equal to) the bearer token, and expires
fast. Default TTL follows the 5-15 min guidance for download links.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import contextlib

log = logging.getLogger("whatsapp_mcp.media")

# The slim runtime image ships without an /etc/mime.types, so mimetypes.guess_type
# returns None for .ogg / .m4a / .webp etc. -> the server then labels them
# application/octet-stream and a browser DOWNLOADS them even on a preview
# (inline) link. Register the WhatsApp media types explicitly so audio/video
# preview links actually play in-page. audio/ogg is Ogg-Opus, which browsers
# render inline.
for _ext, _mt in {
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".mp4": "video/mp4", ".webm": "video/webm", ".3gp": "video/3gpp", ".mov": "video/quicktime",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}.items():
    mimetypes.add_type(_mt, _ext)

MEDIA_CACHE_DIR = Path(os.environ.get("WHATSAPP_MCP_MEDIA_DIR", "/data/media"))
DEFAULT_TTL_SEC = int(os.environ.get("WHATSAPP_MCP_MEDIA_URL_TTL", "900"))  # 15 min
# Container runs under mem_limit 512m, so we never hold a whole file in RAM -
# download and decrypt are both streamed. This cap bounds *disk*, not memory.
MAX_MEDIA_BYTES = int(os.environ.get("WHATSAPP_MCP_MAX_MEDIA_BYTES", str(200 * 1024 * 1024)))

_CHUNK = 64 * 1024
_MAC_LEN = 10
_PUBLIC_URL = (
    os.environ.get("WHATSAPP_MCP_PUBLIC_URL", "").rstrip("/")
    or "https://whatsapp-mcp.example.com"
)


class MediaError(Exception):
    """Media could not be retrieved or failed integrity checks."""


# --- WhatsApp media crypto ---------------------------------------------------
# Every media type uses the same scheme and differs only in the HKDF info
# string. Stickers reuse the image keys; ptt/voice are audio; gif is video.
_MEDIA_INFO = {
    "image":    b"WhatsApp Image Keys",
    "sticker":  b"WhatsApp Image Keys",
    "video":    b"WhatsApp Video Keys",
    "gif":      b"WhatsApp Video Keys",
    "audio":    b"WhatsApp Audio Keys",
    "ptt":      b"WhatsApp Audio Keys",
    "voice":    b"WhatsApp Audio Keys",
    "document": b"WhatsApp Document Keys",
}

_DEFAULT_EXT = {
    "image": ".jpg", "sticker": ".webp", "video": ".mp4", "gif": ".mp4",
    "audio": ".ogg", "ptt": ".ogg", "voice": ".ogg", "document": ".bin",
}


def _info_for(media_type: str) -> bytes:
    info = _MEDIA_INFO.get((media_type or "").strip().lower())
    if info is None:
        raise MediaError(
            f"unsupported media_type {media_type!r}; expected one of "
            f"{sorted(set(_MEDIA_INFO))}"
        )
    return info


def _expand_key(media_key: bytes, media_type: str) -> tuple[bytes, bytes, bytes]:
    """mediaKey -> (iv, cipher_key, mac_key) per WhatsApp's HKDF-SHA256 scheme."""
    if len(media_key) != 32:
        raise MediaError(f"media_key must be 32 bytes, got {len(media_key)}")
    expanded = HKDF(
        algorithm=hashes.SHA256(),
        length=112,
        salt=b"\0" * 32,
        info=_info_for(media_type),
    ).derive(media_key)
    return expanded[:16], expanded[16:48], expanded[48:80]


def decrypt_media_stream(enc_path: Path, out_path: Path, media_key: bytes,
                         media_type: str) -> str:
    """Stream-decrypt a downloaded .enc blob. Returns the plaintext sha256 hex.

    Layout is `AES-256-CBC(plaintext) || HMAC-SHA256(iv||ciphertext)[:10]`. The
    MAC covers the ciphertext, so it can only be checked once the whole stream
    has been read - we therefore decrypt to a temp file and unlink it if the MAC
    fails, so a forged blob never reaches the caller.

    Streamed rather than buffered: this container is capped at 512 MiB and
    WhatsApp documents run to hundreds of MB.
    """
    iv, cipher_key, mac_key = _expand_key(media_key, media_type)
    total = enc_path.stat().st_size
    if total < _MAC_LEN + 16:
        raise MediaError(f"blob too small to be valid media ({total} bytes)")

    ct_len = total - _MAC_LEN
    if ct_len % 16:
        raise MediaError(f"ciphertext of {ct_len} bytes is not an AES block multiple")

    # Pass 1: authenticate. WhatsApp is encrypt-then-MAC, so the MAC MUST be
    # verified before we decrypt or unpad anything - unpadding unverified
    # ciphertext is how padding oracles are born. The blob is already on local
    # disk, so the extra read is page-cache cheap and buys correct ordering.
    mac = hmac.new(mac_key, iv, hashlib.sha256)
    with open(enc_path, "rb") as fin:
        remaining = ct_len
        while remaining > 0:
            chunk = fin.read(min(_CHUNK, remaining))
            if not chunk:
                raise MediaError("truncated media blob")
            remaining -= len(chunk)
            mac.update(chunk)
        got_mac = fin.read(_MAC_LEN)
    if not hmac.compare_digest(mac.digest()[:_MAC_LEN], got_mac):
        raise MediaError("MAC mismatch - wrong media_key or corrupt blob")

    # Pass 2: decrypt. The bytes are authenticated now, so a padding failure
    # here means genuinely malformed media rather than an attack probe - but
    # surface it as MediaError so callers never see a raw ValueError.
    decryptor = Cipher(algorithms.AES(cipher_key), modes.CBC(iv)).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    digest = hashlib.sha256()

    tmp_out = out_path.with_suffix(out_path.suffix + ".part")
    try:
        with open(enc_path, "rb") as fin, open(tmp_out, "wb") as fout:
            remaining = ct_len
            while remaining > 0:
                chunk = fin.read(min(_CHUNK, remaining))
                remaining -= len(chunk)
                plain = unpadder.update(decryptor.update(chunk))
                digest.update(plain)
                fout.write(plain)
            tail = unpadder.update(decryptor.finalize()) + unpadder.finalize()
            digest.update(tail)
            fout.write(tail)
        tmp_out.replace(out_path)
        return digest.hexdigest()
    except ValueError as e:
        tmp_out.unlink(missing_ok=True)
        raise MediaError(f"decryption produced malformed plaintext: {e}") from e
    except Exception:
        tmp_out.unlink(missing_ok=True)
        raise


# --- Lookup + fetch ----------------------------------------------------------

def _lookup(message_id: str, chat_jid: str) -> dict[str, Any]:
    from whatsapp import _open_ro  # local import: avoids a circular import

    conn = _open_ro()
    row = conn.execute(
        "SELECT media_type, filename, url, media_key, file_sha256, file_length "
        "FROM messages WHERE id = ? AND chat_jid = ?",
        (message_id, chat_jid),
    ).fetchone()
    if row is None:
        raise MediaError(f"no message {message_id} in {chat_jid}")
    media_type, filename, url, media_key, file_sha256, file_length = row
    if not media_type:
        raise MediaError(f"message {message_id} carries no media")
    return {
        "media_type": media_type,
        "filename": filename or "",
        "url": url or "",
        "media_key": media_key,
        "file_sha256": file_sha256,
        "file_length": file_length or 0,
    }


_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(name: str, media_type: str, sha_hex: str) -> str:
    """Never let a DB-sourced name steer the path. Basename, allowlist, cap."""
    name = os.path.basename(name or "")
    name = _SAFE_NAME.sub("_", name).lstrip(".")[:120]
    if not name or "." not in name:
        name = f"{media_type or 'media'}_{sha_hex[:12]}{_DEFAULT_EXT.get(media_type, '.bin')}"
    return name


def _cache_path(message_id: str, chat_jid: str, filename: str) -> Path:
    key = hashlib.sha256(f"{chat_jid}|{message_id}".encode()).hexdigest()[:32]
    return MEDIA_CACHE_DIR / key / filename


def _stream_download(url: str, dest: Path) -> str:
    """GET url to dest, enforcing the size cap. Returns the sha256 of what
    landed on disk."""
    digest = hashlib.sha256()
    with requests.get(url, stream=True, timeout=(5, 120)) as r:
        if r.status_code != 200:
            raise MediaError(
                f"CDN returned HTTP {r.status_code} (blob likely expired past its oe= deadline)"
            )
        written = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(_CHUNK):
                written += len(chunk)
                if written > MAX_MEDIA_BYTES:
                    raise MediaError(f"media exceeds {MAX_MEDIA_BYTES} byte cap")
                digest.update(chunk)
                f.write(chunk)
    return digest.hexdigest()


def _check_sha(sha_hex: str, expected: bytes | None, dest: Path) -> None:
    """The DB's file_sha256 is the sender's hash of the PLAINTEXT. Matching it
    proves we recovered the original, not merely something that decrypted
    without raising."""
    if expected and not hmac.compare_digest(sha_hex, bytes(expected).hex()):
        dest.unlink(missing_ok=True)
        raise MediaError("sha256 mismatch against file_sha256 in DB")


def _fetch_from_cdn(meta: dict[str, Any], dest: Path) -> None:
    """Direct CDN GET (+ local decrypt when encrypted).

    This is the path that recovers revoked media, and the only path that works
    for newsletter posts at all.
    """
    if not meta["url"]:
        raise MediaError("message has no CDN url; cannot fetch directly")

    dest.parent.mkdir(parents=True, exist_ok=True)

    if not meta["media_key"]:
        # UNENCRYPTED path. Newsletter (WhatsApp Channels) posts are public
        # broadcast, so they are not E2E encrypted: WhatsApp serves the raw
        # file and sends no mediaKey and no fileEncSha256 - there is nothing to
        # decrypt. Verified 2026-07-16: the CDN blob for a newsletter video
        # starts with `ftypmp42`, and 117/117 newsletter media rows carry a
        # NULL media_key versus ~0% for groups/DMs/status. Their URLs also have
        # a distinct shape (mmg.whatsapp.net/m1/v/..., no `.enc` suffix).
        # Integrity still holds: file_sha256 is present and is checked below.
        sha_hex = _stream_download(meta["url"], dest)
        _check_sha(sha_hex, meta.get("file_sha256"), dest)
        return

    enc_tmp = dest.with_suffix(dest.suffix + ".enc")
    try:
        _stream_download(meta["url"], enc_tmp)
        sha_hex = decrypt_media_stream(enc_tmp, dest, meta["media_key"], meta["media_type"])
        _check_sha(sha_hex, meta.get("file_sha256"), dest)
    finally:
        enc_tmp.unlink(missing_ok=True)


def _fetch_via_bridge(message_id: str, chat_jid: str, dest: Path) -> None:
    """Bridge fallback. Handles media whose CDN URL has aged out but whose
    sender can still service a media-retry request."""
    from whatsapp import download_media as _bridge_download

    res = _bridge_download(message_id, chat_jid)
    if res.get("success") is False:
        raise MediaError(str(res.get("message", "bridge download failed")))
    if res.get("too_large_for_inline"):
        raise MediaError(
            f"media exceeds the bridge's 20MB inline cap and its CDN blob is gone; "
            f"it is on the bridge at {res.get('file_path_on_bridge')}"
        )
    data = res.get("bytes")
    if not data:
        raise MediaError("bridge returned no bytes")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def fetch_media(message_id: str, chat_jid: str) -> dict[str, Any]:
    """Materialise media into the local cache. Returns cache metadata.

    CDN-direct is tried first: it is the only path that works for revoked
    media, it costs the bridge nothing, and it streams instead of buffering.
    The bridge is the fallback for blobs whose CDN URL has expired.
    """
    meta = _lookup(message_id, chat_jid)
    sha_hex = bytes(meta["file_sha256"]).hex() if meta["file_sha256"] else ""
    filename = _safe_filename(meta["filename"], meta["media_type"], sha_hex)
    dest = _cache_path(message_id, chat_jid, filename)

    if dest.exists() and dest.stat().st_size > 0:
        source = "cache"
    else:
        try:
            _fetch_from_cdn(meta, dest)
            source = "cdn"
        except MediaError as cdn_err:
            log.info("CDN fetch failed for %s (%s); trying bridge", message_id, cdn_err)
            try:
                _fetch_via_bridge(message_id, chat_jid, dest)
                source = "bridge"
            except MediaError as bridge_err:
                raise MediaError(
                    f"could not retrieve media. CDN: {cdn_err} | bridge: {bridge_err}"
                ) from bridge_err

    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return {
        "path": dest,
        "filename": filename,
        "mime": mime,
        "media_type": meta["media_type"],
        "size": dest.stat().st_size,
        "source": source,
    }


# --- Signed URLs -------------------------------------------------------------

def _signing_key() -> bytes:
    """Derive a URL-signing key from the bearer token.

    Key separation: the raw bearer token never signs anything. If a signature
    were ever inverted the token itself stays secret, and rotating the token
    rotates every outstanding link.
    """
    secret = os.environ.get("WHATSAPP_MCP_TOKEN", "").strip()
    if not secret:
        raw = os.environ.get("WHATSAPP_MCP_TOKENS_JSON", "").strip()
        if raw:
            try:
                entries = json.loads(raw)
                secret = str(entries[0].get("token", "")) if entries else ""
            except (ValueError, TypeError, IndexError, AttributeError):
                secret = ""
    if not secret:
        raise MediaError("no bearer token configured; cannot sign media URLs")
    return hmac.new(secret.encode(), b"whatsapp-mcp/media-url/v1", hashlib.sha256).digest()


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_url(message_id: str, chat_jid: str, filename: str,
             ttl_seconds: int = DEFAULT_TTL_SEC) -> tuple[str, int]:
    """Return (url, expires_at_epoch) for a capability link to this media."""
    ttl = max(60, min(int(ttl_seconds or DEFAULT_TTL_SEC), 24 * 3600))
    exp = int(time.time()) + ttl
    payload = _b64e(json.dumps(
        {"m": message_id, "c": chat_jid, "f": filename, "e": exp},
        separators=(",", ":"), sort_keys=True,
    ).encode())
    sig = _b64e(hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest())
    return f"{_PUBLIC_URL}/media/{payload}.{sig}", exp


# --- self-hosted short links ------------------------------------------------
# The signed token above is ~180 chars because it carries the claims + HMAC in
# the URL (stateless). A short link trades that for a tiny server-side row: a
# random code maps to the same claims. We host it ourselves rather than use a
# third-party shortener on purpose - the URL is a capability to private media,
# so handing it to TinyURL/is.gd would leak it, and those services are built for
# permanent links while ours expire in minutes. The code IS the credential, so
# it is high-entropy (secrets.token_urlsafe) and short-lived.
_SHORT_DB = Path(os.environ.get("WHATSAPP_MCP_SHORT_DB", "/var/log/wamcp/shorturls.db"))
# Code length in random bytes. The code IS the credential for the link, so more
# entropy is strictly better and the user is fine with a longer URL. 18 bytes ->
# ~24-char code -> ~60-char total URL (domain is ~37 of that). token_urlsafe
# gives ~1.33 chars/byte; raise this env to lengthen further.
_SHORT_CODE_BYTES = max(6, int(os.environ.get("WHATSAPP_MCP_SHORT_CODE_BYTES", "18")))
_short_ready = False


def _short_init() -> None:
    global _short_ready
    if _short_ready:
        return
    with contextlib.suppress(OSError):
        _SHORT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_SHORT_DB))
    try:
        with conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS short (
                    code       TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    chat_jid   TEXT NOT NULL,
                    filename   TEXT,
                    exp        INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_short_exp ON short(exp)")
    finally:
        conn.close()
    _short_ready = True


def make_short_url(message_id: str, chat_jid: str, filename: str,
                   ttl_seconds: int = DEFAULT_TTL_SEC) -> tuple[str, str, int]:
    """Store the claims under a random code and return (download_url, preview_url,
    exp) - the same code served two ways (/s/ downloads, /p/ previews inline).

    Same TTL semantics as sign_url; the code stands in for the signed token.
    Expired rows are swept on each new link so the table stays tiny."""
    _short_init()
    ttl = max(60, min(int(ttl_seconds or DEFAULT_TTL_SEC), 24 * 3600))
    now = int(time.time())
    exp = now + ttl
    code = secrets.token_urlsafe(_SHORT_CODE_BYTES)  # ~24 chars / 144 bits by default
    conn = sqlite3.connect(str(_SHORT_DB))
    try:
        with conn:
            conn.execute("DELETE FROM short WHERE exp < ?", (now,))
            conn.execute(
                "INSERT OR REPLACE INTO short (code, message_id, chat_jid, filename, exp, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (code, message_id, chat_jid, filename, exp, now),
            )
    finally:
        conn.close()
    # Same code, two dispositions: /s/ downloads (attachment), /p/ previews
    # (inline - browser renders it). Returns (download_url, preview_url, exp).
    return f"{_PUBLIC_URL}/s/{code}", f"{_PUBLIC_URL}/p/{code}", exp


def resolve_short(code: str) -> dict[str, Any]:
    """Resolve a short code to its media claims. Raises MediaError if unknown or
    expired - the same shape verify_url returns, so the route can serve both
    identically."""
    _short_init()
    conn = sqlite3.connect(f"file:{_SHORT_DB}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT message_id, chat_jid, filename, exp FROM short WHERE code = ?",
            (code,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise MediaError("unknown link")
    if int(row[3]) < int(time.time()):
        raise MediaError("link expired")
    return {"m": row[0], "c": row[1], "f": row[2], "e": row[3]}


def verify_url(token: str) -> dict[str, Any]:
    """Validate a signed token. Raises MediaError on tamper/expiry.

    Signature is checked *before* expiry so a tampered token can never have its
    claims read, and compare_digest keeps the check constant-time.
    """
    try:
        payload, sig = token.rsplit(".", 1)
    except ValueError:
        raise MediaError("malformed token") from None

    expected = _b64e(hmac.new(_signing_key(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        raise MediaError("bad signature")

    try:
        claims = json.loads(_b64d(payload))
    except (ValueError, TypeError):
        raise MediaError("malformed payload") from None

    if int(claims.get("e", 0)) < int(time.time()):
        raise MediaError("link expired")
    return claims
