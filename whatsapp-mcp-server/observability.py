"""G7 + G8: per-tool Prometheus metrics + append-only JSONL audit log.

Single Middleware subclass instead of two (one round of context hopping is
free, two is silly) that wraps every tool call with:
  - prometheus.Counter mcp_tool_calls_total{tool, outcome}
  - prometheus.Histogram mcp_tool_duration_seconds{tool}
  - JSONL row appended to AUDIT_LOG_PATH per MUTATING call.

The metrics endpoint is mounted at /metrics on the FastMCP app (loopback only;
nginx must NOT proxy it). Audit log is open-write, fsync per line so a crash
can't lose recent rows, in a file the bridge volume mount can persist.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.responses import Response

log = logging.getLogger("whatsapp_mcp.obs")

# Buckets tuned for our actual tool-call latencies: <1ms reads, 50ms tools,
# WhatsApp sends 200ms-30s. Going past 60s isn't useful - the bridge will have
# already returned 503.
_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60)

mcp_tool_calls_total = Counter(
    "mcp_tool_calls_total",
    "Count of MCP tool invocations.",
    ["tool", "outcome"],
)
mcp_tool_duration_seconds = Histogram(
    "mcp_tool_duration_seconds",
    "Wall time of MCP tool invocations.",
    ["tool"],
    buckets=_BUCKETS,
)

# Tools that mutate WhatsApp state - we audit-log every call to these. The
# annotation system in tools.py already classifies them (SEND / MUTATE /
# DESTRUCTIVE); this list mirrors. Pure-read tools (search_*, list_*, get_*,
# bridge_health) skip audit to keep the file small.
AUDIT_MUTATING_TOOLS = {
    # SEND
    "send_message", "reply_to_message", "send_file", "send_audio_message", "send_view_once_media",
    "send_location", "create_poll", "create_group", "send_contact_card",
    "mention_everyone", "join_group_by_link",
    # MUTATE
    "react_to_message", "mark_read", "send_presence", "set_disappearing", "unblock_user",
    # DESTRUCTIVE
    "edit_message", "delete_message", "block_user", "update_group_participants",
    "leave_group",
}

_AUDIT_FALLBACK = Path("/tmp/wamcp-audit.jsonl")
AUDIT_LOG_PATH = Path(os.environ.get("WHATSAPP_MCP_AUDIT_LOG", str(_AUDIT_FALLBACK)))


def _pick_audit_path() -> Path:
    """Pick a writable audit log path. Tries the requested one, then /tmp.

    Volume-mounted dirs default to root:root in docker, so a non-root container
    user (uid 10001) can't write unless the compose entrypoint chowned the
    volume on first start. Rather than require that for the simple case, fall
    back to /tmp so audit always works and we don't drop rows silently.
    """
    candidates = []
    if AUDIT_LOG_PATH != _AUDIT_FALLBACK:
        candidates.append(AUDIT_LOG_PATH)
    candidates.append(_AUDIT_FALLBACK)
    for p in candidates:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # Touch to verify writability.
            with p.open("a", encoding="utf-8") as f:
                f.write("")
            if p != AUDIT_LOG_PATH:
                log.warning(
                    "audit: requested path %s is not writable; falling back to %s "
                    "(ephemeral). Fix by chowning the wa-audit volume to uid 10001.",
                    AUDIT_LOG_PATH, p,
                )
            return p
        except OSError as e:
            log.warning("audit: candidate %s not writable: %s", p, e)
    log.warning("audit: NO writable path found; audit will be skipped")
    return _AUDIT_FALLBACK


AUDIT_LOG_PATH = _pick_audit_path()


def _safe_args_hash(args: Any) -> str:
    """SHA256 of JSON-stable args; never store raw values (PII / token leaks)."""
    try:
        payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = repr(args)
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()[:16]


class ObservabilityMiddleware(Middleware):
    """G7 + G8: counts, histograms, audit log per tool call."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        start = time.perf_counter()
        tool_name = getattr(context.message, "name", "unknown")
        client_id = "owner"  # single-token deployment; will gain per-client when G11 lands
        outcome = "ok"
        try:
            result = await call_next(context)
            return result
        except Exception:
            outcome = "error"
            raise
        finally:
            dur = time.perf_counter() - start
            try:
                mcp_tool_calls_total.labels(tool=tool_name, outcome=outcome).inc()
                mcp_tool_duration_seconds.labels(tool=tool_name).observe(dur)
            except Exception:
                pass
            if tool_name in AUDIT_MUTATING_TOOLS:
                args = getattr(context.message, "arguments", None) or {}
                row = {
                    "ts": time.time(),
                    "client_id": client_id,
                    "tool": tool_name,
                    "args_hash": _safe_args_hash(args),
                    "outcome": outcome,
                    "ms": round(dur * 1000, 2),
                }
                try:
                    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                        f.flush()
                        os.fsync(f.fileno())
                except OSError as e:
                    log.warning("audit write failed: %s", e)


async def metrics_endpoint(_request):
    """Loopback /metrics handler. Don't expose past nginx."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# G11: Per-tool scope enforcement --------------------------------------------
# Every tool declares which scope it needs. The AuthzMiddleware reads the
# token's scopes off the request auth context and rejects at dispatch time.

# READ_LOCAL + READ_BRIDGE + informational -> whatsapp:read
# SEND + MUTATE -> whatsapp:send
# DESTRUCTIVE + admin ops -> whatsapp:admin
TOOL_SCOPES: dict[str, str] = {
    # READ
    "search_contacts": "whatsapp:read",
    "list_messages": "whatsapp:read",
    "list_chats": "whatsapp:read",
    "get_chat": "whatsapp:read",
    "get_direct_chat_by_contact": "whatsapp:read",
    "get_contact_chats": "whatsapp:read",
    "get_last_interaction": "whatsapp:read",
    "get_message_context": "whatsapp:read",
    "list_media_in_chat": "whatsapp:read",
    "search_all_messages": "whatsapp:read",
    "bridge_health": "whatsapp:read",
    "get_profile_picture": "whatsapp:read",
    "get_blocklist": "whatsapp:read",
    "list_joined_groups": "whatsapp:read",
    "get_group_info": "whatsapp:read",
    "list_subscribed_newsletters": "whatsapp:read",
    "get_newsletter_info": "whatsapp:read",
    "view_media": "whatsapp:read",
    "get_poll_results": "whatsapp:read",
    "transcribe_voice": "whatsapp:read",
    "search_voice_notes": "whatsapp:read",
    "save_media": "whatsapp:read",
    "list_view_once": "whatsapp:read",
    "check_phones_on_whatsapp": "whatsapp:read",
    "get_user_info_bulk": "whatsapp:read",
    "get_business_profile": "whatsapp:read",
    "get_group_invite_link": "whatsapp:read",
    "get_message_receipts": "whatsapp:read",
    "get_privacy_settings": "whatsapp:read",
    # SEND / MUTATE
    "send_message": "whatsapp:send",
    "reply_to_message": "whatsapp:send",
    "send_file": "whatsapp:send",
    "send_audio_message": "whatsapp:send",
    "send_view_once_media": "whatsapp:send",
    "send_location": "whatsapp:send",
    "create_poll": "whatsapp:send",
    "send_contact_card": "whatsapp:send",
    "mention_everyone": "whatsapp:send",
    "join_group_by_link": "whatsapp:send",
    "react_to_message": "whatsapp:send",
    "mark_read": "whatsapp:send",
    "send_presence": "whatsapp:send",
    "unblock_user": "whatsapp:send",
    "mute_chat": "whatsapp:send",
    "pin_chat": "whatsapp:send",
    "star_message": "whatsapp:send",
    "post_status": "whatsapp:send",
    "forward_message": "whatsapp:send",
    "set_disappearing": "whatsapp:send",
    "create_group": "whatsapp:send",
    # ADMIN / DESTRUCTIVE
    "edit_message": "whatsapp:admin",
    "delete_message": "whatsapp:admin",
    "block_user": "whatsapp:admin",
    "leave_group": "whatsapp:admin",
    "update_group_participants": "whatsapp:admin",
}


class AuthzMiddleware(Middleware):
    """G11 + N7: enforce per-tool scope on the caller's token."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        tool_name = getattr(context.message, "name", "")
        needed = TOOL_SCOPES.get(tool_name)
        if needed:
            try:
                tok = get_access_token()
            except Exception:
                tok = None
            scopes = list(getattr(tok, "scopes", []) or []) if tok else []
            if "whatsapp:full" not in scopes and needed not in scopes:
                raise ToolError(
                    f"Insufficient scope: tool '{tool_name}' requires '{needed}'; "
                    f"caller has {scopes or ['(none)']}"
                )
        return await call_next(context)
