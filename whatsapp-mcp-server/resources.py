"""N4: MCP resources for chat, message, and media.

FastMCP treats URIs with `{param}` segments as templated resources. Clients
call resources/list to discover templates and resources/read with a
concrete URI. Exposing our data via resources lets Claude Desktop's attach
UI use them directly instead of having to synthesize tool call chains.
"""
from __future__ import annotations

import json
import logging
from typing import Any


log = logging.getLogger("whatsapp_mcp.res")


def register(mcp) -> None:
    from whatsapp import (
        get_chat as _get_chat,
        list_messages as _list_messages,
    )
    # media.fetch_media, not whatsapp.download_media: the bridge cannot serve
    # revoked media and its media-retry fallback needs the sender's phone
    # online, so resources hit the same failures the tools used to.
    from media import MediaError, fetch_media as _fetch_media

    @mcp.resource(
        "chat://{chat_jid}",
        name="WhatsApp chat",
        description="Chat metadata plus the last 20 messages.",
        mime_type="application/json",
    )
    def chat_resource(chat_jid: str) -> str:
        chat = _get_chat(chat_jid, include_last_message=True)
        msgs = _list_messages(chat_jid=chat_jid, limit=20, include_context=False)
        # dataclasses -> dict
        chat_d = chat.__dict__ if chat else None
        if chat_d and chat_d.get("last_message_time"):
            chat_d["last_message_time"] = chat_d["last_message_time"].isoformat()
        msg_ds = []
        for m in (msgs or []):
            d = m.__dict__.copy()
            if d.get("timestamp"):
                d["timestamp"] = d["timestamp"].isoformat()
            msg_ds.append(d)
        return json.dumps({"chat": chat_d, "messages": msg_ds}, ensure_ascii=False)

    @mcp.resource(
        "message://{message_id}/{chat_jid}",
        name="WhatsApp message",
        description="Single message by id + chat_jid.",
        mime_type="application/json",
    )
    def message_resource(message_id: str, chat_jid: str) -> str:
        from whatsapp import get_message_context as _get_ctx
        try:
            ctx = _get_ctx(message_id, before=0, after=0)
            m = ctx.message
            d = m.__dict__.copy()
            if d.get("timestamp"):
                d["timestamp"] = d["timestamp"].isoformat()
            return json.dumps(d, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    @mcp.resource(
        "media://{message_id}/{chat_jid}",
        name="WhatsApp media",
        description="Media bytes attached to a message (image / audio / video / doc).",
        mime_type="application/octet-stream",
    )
    def media_resource(message_id: str, chat_jid: str) -> dict[str, Any]:
        """Returns a BlobResourceContents payload."""
        try:
            info = _fetch_media(message_id, chat_jid)
        except MediaError as e:
            # Represent failure as a small JSON blob so the client sees a
            # readable body instead of empty bytes.
            return {"blob": json.dumps({"error": str(e)}).encode(),
                    "mimeType": "application/json"}
        import base64
        return {
            "blob": base64.b64encode(info["path"].read_bytes()).decode("ascii"),
            "mimeType": info.get("mime", "application/octet-stream"),
        }
