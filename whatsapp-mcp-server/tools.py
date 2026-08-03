"""WhatsApp MCP tools, registered onto a FastMCP server.

This is the general tool set, factored into a single register(mcp) function so
it can be mounted on an authenticated FastMCP v2 server (see server.py). Each
tool calls into whatsapp.py, which reads the shared SQLite DB and talks to the
Go bridge's REST API.

On the size of register(): it is long because FastMCP registers a tool by
decorating a function with @mcp.tool, and holding ~85 of those as nested defs
inside one function is how the decorator captures the `mcp` instance without a
module-level global. It is flat registration, not tangled control flow - every
nested def is an independent, self-contained tool. The alternative (a global
server plus 85 module-level defs) trades this length for import-time coupling
that made the auth/prune wiring in server.py harder to reason about.
"""
from __future__ import annotations

from datetime import datetime, UTC
from typing import Any

from fastmcp.utilities.types import Image, Audio, File

from media import (
    MediaError,
    fetch_media as media_fetch,
    sign_url as media_sign_url,
    make_short_url as media_short_url,
)

# --- Tool annotations (MCP spec ToolAnnotations) ----------------------------
# Per-tool hints so clients can render destructive-confirm dialogs and LLMs
# pick the right tool faster. Spec: modelcontextprotocol.io.
#
# READ_LOCAL  : read-only against SQLite only, no external side effects.
# READ_BRIDGE : read-only but touches whatsmeow (e.g. group info from WA).
# SEND        : mutates (sends to WhatsApp); not destructive (recipient gets msg).
# MUTATE      : mutates but not destructive (mark_read, set_disappearing toggle, etc.).
# DESTRUCTIVE : irreversible or hard-to-reverse (delete, block, leave_group, revoke).
READ_LOCAL: dict[str, Any]   = {"readOnlyHint": True,  "openWorldHint": False, "idempotentHint": True}
READ_BRIDGE: dict[str, Any]  = {"readOnlyHint": True,  "openWorldHint": True,  "idempotentHint": True}
SEND: dict[str, Any]         = {"readOnlyHint": False, "openWorldHint": True,  "idempotentHint": False, "destructiveHint": False}
MUTATE: dict[str, Any]       = {"readOnlyHint": False, "openWorldHint": True,  "idempotentHint": True,  "destructiveHint": False}
DESTRUCTIVE: dict[str, Any]  = {"readOnlyHint": False, "openWorldHint": True,  "idempotentHint": False, "destructiveHint": True}


# G10: explicit confirmation required for destructive ops. Older MCP clients
# don't support elicitation/createMessage, so the safest portable mechanism is
# a required confirm=True kwarg that the LLM must produce on purpose. The
# annotation already nudges clients to render a dialog; this enforces server-
# side that even a mis-prompted agent can't no-op into irreversible damage.
_CONFIRM_HINT = (
    "Refusing destructive operation without explicit confirmation. "
    "Re-call with confirm=True after the user (not the LLM) has agreed."
)


def _require_confirm(confirm: bool) -> dict[str, Any] | None:
    if not confirm:
        return {"success": False, "message": _CONFIRM_HINT, "confirmation_required": True}
    return None

from whatsapp import (
    search_contacts as whatsapp_search_contacts,
    list_messages as whatsapp_list_messages,
    list_chats as whatsapp_list_chats,
    get_chat as whatsapp_get_chat,
    get_direct_chat_by_contact as whatsapp_get_direct_chat_by_contact,
    get_contact_chats as whatsapp_get_contact_chats,
    get_last_interaction as whatsapp_get_last_interaction,
    get_message_context as whatsapp_get_message_context,
    send_message as whatsapp_send_message,
    send_file as whatsapp_send_file,
    send_audio_message as whatsapp_audio_voice_message,
    # NOTE: whatsapp.download_media (the bridge path) is deliberately not
    # imported here. Both media tools go through media.fetch_media, which tries
    # the CDN first and falls back to the bridge internally.
    # Batch E feature wrappers (thin POSTs to the new bridge endpoints).
    bridge_health as whatsapp_bridge_health,
    react_to_message as whatsapp_react_to_message,
    edit_message as whatsapp_edit_message,
    delete_message as whatsapp_delete_message,
    mark_read as whatsapp_mark_read,
    send_presence as whatsapp_send_presence,
    get_profile_picture as whatsapp_get_profile_picture,
    # Batch H Tier-2 features.
    send_location as whatsapp_send_location,
    set_disappearing as whatsapp_set_disappearing,
    create_poll as whatsapp_create_poll,
    # Batch N: view-once send (multi-view download already works via unwrap in extractMediaInfo).
    send_view_once_media as whatsapp_send_view_once_media,
    # Batch P (media access): list_media_in_chat helper for discovery.
    list_media_in_chat as whatsapp_list_media_in_chat,
    # Batch I Tier-3 features.
    create_group as whatsapp_create_group,
    update_group_participants as whatsapp_update_group_participants,
    get_group_info as whatsapp_get_group_info,
    list_joined_groups as whatsapp_list_joined_groups,
    get_blocklist as whatsapp_get_blocklist,
    block_user as whatsapp_block_user,
    unblock_user as whatsapp_unblock_user,
    list_subscribed_newsletters as whatsapp_list_subscribed_newsletters,
    get_newsletter_info as whatsapp_get_newsletter_info,
    # G2: receipt read-back
    get_message_receipts as whatsapp_get_message_receipts,
    # #73 delete_chat
    delete_chat as whatsapp_delete_chat,
    # batch omicron
    backfill_group_participants as whatsapp_backfill_group_participants,
    sender_activity as whatsapp_sender_activity,
    list_all_media_by_type as whatsapp_list_all_media_by_type,
    list_view_once as whatsapp_list_view_once,
    search_by_reaction as whatsapp_search_by_reaction,
    get_bridge_diagnostics as whatsapp_get_bridge_diagnostics,
    # batch xi
    get_replies_to as whatsapp_get_replies_to,
    get_message_thread as whatsapp_get_message_thread,
    groups_with_member as whatsapp_groups_with_member,
    list_group_members as whatsapp_list_group_members,
    # batch nu
    get_reactions_on as whatsapp_get_reactions_on,
    get_chat_stats as whatsapp_get_chat_stats,
    get_top_active_chats as whatsapp_get_top_active_chats,
    get_quiet_chats as whatsapp_get_quiet_chats,
    count_messages as whatsapp_count_messages,
    messages_by_day as whatsapp_messages_by_day,
    export_chat as whatsapp_export_chat,
    # batch lambda
    send_sticker as whatsapp_send_sticker,
    # batch kappa
    vote_in_poll as whatsapp_vote_in_poll,
    get_poll_results as whatsapp_get_poll_results,
    get_bridge_stats as whatsapp_get_bridge_stats,
    get_media_info as whatsapp_get_media_info,
    # batch iota
    archive_chat as whatsapp_archive_chat,
    mark_chat_unread as whatsapp_mark_chat_unread,
    set_status_message as whatsapp_set_status_message,
    list_chats_by_state as whatsapp_list_chats_by_state,
    # N10 labels
    edit_label as whatsapp_edit_label,
    set_chat_label as whatsapp_set_chat_label,
    set_message_label as whatsapp_set_message_label,
    # G6 + N9 + N11 (batch beta)
    mute_chat as whatsapp_mute_chat,
    pin_chat as whatsapp_pin_chat,
    star_message as whatsapp_star_message,
    post_status as whatsapp_post_status,
    get_privacy_settings as whatsapp_get_privacy_settings,
    forward_message as whatsapp_forward_message,
    # G3 + G4 + G5
    check_phones_on_whatsapp as whatsapp_check_phones_on_whatsapp,
    get_user_info_bulk as whatsapp_get_user_info_bulk,
    get_business_profile as whatsapp_get_business_profile,
    get_group_invite_link as whatsapp_get_group_invite_link,
    join_group_by_link as whatsapp_join_group_by_link,
    leave_group as whatsapp_leave_group,
    search_all_messages as whatsapp_search_all_messages,
    # G14
    send_contact_card as whatsapp_send_contact_card,
    mention_everyone as whatsapp_mention_everyone,
)


def register(mcp) -> None:
    """Attach all WhatsApp tools to the given FastMCP instance."""

    @mcp.tool(annotations=READ_LOCAL)
    def search_contacts(query: str) -> list[dict[str, Any]]:
        """Search WhatsApp contacts by name or phone number.

        Args:
            query: Search term to match against contact names or phone numbers
        """
        return whatsapp_search_contacts(query)

    @mcp.tool(annotations=READ_LOCAL)
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
    ) -> list[dict[str, Any]]:
        """Get WhatsApp messages matching specified criteria with optional context.

        Args:
            after: Optional ISO-8601 string; only messages after this date
            before: Optional ISO-8601 string; only messages before this date
            sender_phone_number: Optional phone number to filter by sender
            chat_jid: Optional chat JID to filter by chat
            query: Optional search term to filter by content (FTS5 word-tokenised)
            limit: Maximum number of messages to return (default 20, max 100)
            page: Page number for pagination (default 0). Prefer before_timestamp for deep paging.
            include_context: Include messages before/after matches (default True)
            context_before: Messages to include before each match (default 1, max 20)
            context_after: Messages to include after each match (default 1, max 20)
            before_timestamp: Cursor: ISO-8601 timestamp of the OLDEST message
                you've already seen. Returns the next older page in O(limit)
                instead of O(page*limit). Overrides `page` when both are set.
        """
        return whatsapp_list_messages(
            after=after,
            before=before,
            sender_phone_number=sender_phone_number,
            chat_jid=chat_jid,
            query=query,
            limit=limit,
            page=page,
            include_context=include_context,
            context_before=context_before,
            context_after=context_after,
            before_timestamp=before_timestamp,
        )

    @mcp.tool(annotations=READ_LOCAL)
    def list_chats(
        query: str | None = None,
        limit: int = 20,
        page: int = 0,
        include_last_message: bool = True,
        sort_by: str = "last_active",
    ) -> list[dict[str, Any]]:
        """Get WhatsApp chats matching specified criteria.

        Args:
            query: Optional search term to filter chats by name or JID
            limit: Maximum number of chats to return (default 20)
            page: Page number for pagination (default 0)
            include_last_message: Include the last message in each chat (default True)
            sort_by: "last_active" or "name" (default "last_active")
        """
        return whatsapp_list_chats(
            query=query,
            limit=limit,
            page=page,
            include_last_message=include_last_message,
            sort_by=sort_by,
        )

    @mcp.tool(annotations=READ_LOCAL)
    def get_chat(chat_jid: str, include_last_message: bool = True) -> dict[str, Any]:
        """Get WhatsApp chat metadata by JID.

        Args:
            chat_jid: The JID of the chat to retrieve
            include_last_message: Whether to include the last message (default True)
        """
        return whatsapp_get_chat(chat_jid, include_last_message)

    @mcp.tool(annotations=READ_LOCAL)
    def get_direct_chat_by_contact(sender_phone_number: str) -> dict[str, Any]:
        """Get WhatsApp chat metadata by sender phone number.

        Args:
            sender_phone_number: The phone number to search for
        """
        return whatsapp_get_direct_chat_by_contact(sender_phone_number)

    @mcp.tool(annotations=READ_LOCAL)
    def get_contact_chats(jid: str, limit: int = 20, page: int = 0) -> list[dict[str, Any]]:
        """Get all WhatsApp chats involving the contact.

        Args:
            jid: The contact's JID to search for
            limit: Maximum number of chats to return (default 20)
            page: Page number for pagination (default 0)
        """
        return whatsapp_get_contact_chats(jid, limit, page)

    @mcp.tool(annotations=READ_LOCAL)
    def get_last_interaction(jid: str) -> str:
        """Get most recent WhatsApp message involving the contact.

        Args:
            jid: The JID of the contact to search for
        """
        return whatsapp_get_last_interaction(jid)

    @mcp.tool(annotations=READ_LOCAL)
    def get_message_context(
        message_id: str, before: int = 5, after: int = 5
    ) -> dict[str, Any]:
        """Get context around a specific WhatsApp message.

        Args:
            message_id: The ID of the message to get context for
            before: Messages to include before the target (default 5)
            after: Messages to include after the target (default 5)
        """
        return whatsapp_get_message_context(message_id, before, after)

    @mcp.tool(annotations=SEND)
    def send_message(
        recipient: str,
        message: str,
        reply_to_message_id: str | None = None,
        reply_to_sender_jid: str | None = None,
        mentioned_jids: list[str] | None = None,
        delivery_timeout_seconds: int = 15,
    ) -> dict[str, Any]:
        """Send a WhatsApp message to a person or group. For group chats use the JID.

        To REPLY to a specific message, pass reply_to_message_id (the id of the
        message being replied to, from any read tool). That is all you need - the
        server auto-fills the original sender and shows the real quoted text in
        the reply. reply_to_sender_jid is optional and only needed to override the
        auto-resolved sender.

        Args:
            recipient: Phone number with country code (no + or symbols), or a JID
                       (e.g. "123456789@s.whatsapp.net" or group "123456789@g.us")
            message: The message text to send
            reply_to_message_id: Optional - id of the message to quote-reply to.
                       Sender + quoted preview are auto-filled from it.
            reply_to_sender_jid: Optional override - JID of the original sender.
                       Normally leave unset; it is derived from the quoted message.
            mentioned_jids: Optional - list of JIDs to @-mention; include "@<localpart>" in `message`
            delivery_timeout_seconds: Max seconds to wait for WhatsApp to ACK delivery
                       (default 15s). Real recipients online ACK in <2s; longer waits
                       mean the recipient is unreachable. Raise to 60+ for offline
                       recipients you DO want to queue (WA will deliver when they
                       come online; the ACK still won't come in those 60s).
        """
        if not recipient:
            return {"success": False, "message": "Recipient must be provided"}
        success, status_message = whatsapp_send_message(
            recipient,
            message,
            reply_to_message_id=reply_to_message_id,
            reply_to_sender_jid=reply_to_sender_jid,
            mentioned_jids=mentioned_jids,
            delivery_timeout_seconds=delivery_timeout_seconds,
        )
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=SEND)
    def reply_to_message(message_id: str, text: str,
                         chat_jid: str | None = None,
                         mentioned_jids: list[str] | None = None,
                         delivery_timeout_seconds: int = 15) -> dict[str, Any]:
        """Post a QUOTED reply to a specific message - the sent message shows the
        original quoted above it (a real WhatsApp reply, not a plain message).

        USE THIS whenever the user asks to reply to / quote / answer a specific
        message ("reply to this", "quote it", "رد على الرسالة دي"). You only need
        the id of the message being replied to and your reply text; the chat and
        the original sender are derived automatically. Do not use plain
        send_message for a reply - that does not thread or quote.

        Args:
            message_id: id of the message you are replying to (from any read tool).
            text: your reply text.
            chat_jid: normally omit - looked up from the message. Pass only to
                      disambiguate if the id somehow exists in more than one chat.
            mentioned_jids: optional @-mentions (include @<localpart> in text).
        """
        if not message_id or not text:
            return {"success": False, "message": "message_id and text are both required"}
        cj = chat_jid
        if not cj:
            import os as _os
            import sqlite3 as _sq
            mdb = _os.environ.get("MESSAGES_DB_PATH", "/data/store/messages.db")
            conn = _sq.connect(f"file:{mdb}?mode=ro", uri=True)
            try:
                rows = conn.execute("SELECT DISTINCT chat_jid FROM messages WHERE id = ?", (message_id,)).fetchall()
            finally:
                conn.close()
            if not rows:
                return {"success": False, "message": f"Message {message_id} not found in the store; pass chat_jid explicitly."}
            if len(rows) > 1:
                return {"success": False, "message": "That message id exists in multiple chats; pass chat_jid to disambiguate.",
                        "chats": [r[0] for r in rows]}
            cj = rows[0][0]
        success, status_message = whatsapp_send_message(
            cj, text,
            reply_to_message_id=message_id,
            mentioned_jids=mentioned_jids,
            delivery_timeout_seconds=delivery_timeout_seconds,
        )
        return {"success": success, "message": status_message, "replied_to": message_id, "chat_jid": cj}

    @mcp.tool(annotations=SEND)
    def send_file(
        recipient: str,
        media_path: str | None = None,
        media_url: str | None = None,
        media_data: str | None = None,
        filename: str | None = None,
        message: str = "",
    ) -> dict[str, Any]:
        """Send a file (image, video, raw audio, document) via WhatsApp.

        Specify EXACTLY ONE of:
          media_path  - absolute path inside the MCP container (sandboxed under
                        /tmp; only useful for files already there)
          media_url   - any http(s) URL the MCP container can fetch (max 64MB)
          media_data  - base64-encoded raw bytes (PREFERRED when the file lives
                        on the calling LLM client's machine - just base64-encode
                        the local file). Set `filename` so the right extension
                        drives the media-type detection.

        Args:
            recipient: Phone number (country code, no + or symbols) or JID
            media_path: optional - local path on the MCP container
            media_url: optional - URL to fetch
            media_data: optional - base64-encoded bytes
            filename: optional - filename hint (required-ish with media_data)
            message: optional - caption text
        """
        success, status_message = whatsapp_send_file(
            recipient, media_path,
            media_url=media_url, media_data=media_data,
            filename=filename, message=message,
        )
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=SEND)
    def send_audio_message(
        recipient: str,
        media_path: str | None = None,
        media_url: str | None = None,
        media_data: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Send an audio file as a WhatsApp voice message.

        Same input flexibility as send_file (path / url / base64). Non-.ogg
        inputs are converted to opus by the MCP container's bundled ffmpeg.

        Args:
            recipient: Phone number (country code, no + or symbols) or JID
            media_path: optional - local path on the MCP container
            media_url: optional - URL to fetch
            media_data: optional - base64-encoded bytes
            filename: optional - filename hint (recommended for media_data)
        """
        success, status_message = whatsapp_audio_voice_message(
            recipient, media_path,
            media_url=media_url, media_data=media_data, filename=filename,
        )
        return {"success": success, "message": status_message}

    # Above this, media is returned as a link rather than inline bytes. MCP
    # ships inline content base64'd, which inflates ~33%, and multi-MB blobs in
    # a tool result bloat the context for no benefit.
    INLINE_MAX_BYTES = 20 * 1024 * 1024

    def _wrap_inline(info: dict[str, Any]) -> Image | Audio | File:
        """Wrap cached media bytes as MCP content based on detected type."""
        data = info["path"].read_bytes()
        filename = info["filename"]
        mime = info.get("mime", "application/octet-stream")
        media_type = info.get("media_type", "")
        # Extract extension from filename for FastMCP's format= hint.
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else None
        if media_type == "image" or mime.startswith("image/"):
            return Image(data=data, format=ext or "jpg")
        if media_type in ("audio", "ptt", "voice") or mime.startswith("audio/"):
            return Audio(data=data, format=ext or "ogg")
        # Video / document / unknown -> File (lets the client save it).
        return File(data=data, format=ext, name=filename)

    def _link_payload(message_id: str, chat_jid: str, info: dict[str, Any],
                      ttl_minutes: int) -> dict[str, Any]:
        """Sign a link for media that has ALREADY been fetched.

        Takes the info dict rather than re-fetching so a caller that already has
        it (view_media) does not repeat the DB lookup - and so retrieved_via
        still reports how the bytes were really obtained, instead of collapsing
        to "cache" on the second look.
        """
        ttl_s = max(1, min(int(ttl_minutes or 15), 1440)) * 60
        # Short link is the primary download_url now (self-hosted shortener, same
        # expiry). Keep the long signed URL too for anyone who wants the
        # stateless form.
        download_url, preview_url, exp = media_short_url(
            message_id, chat_jid, info["filename"], ttl_seconds=ttl_s)
        signed_url, _ = media_sign_url(message_id, chat_jid, info["filename"], ttl_seconds=ttl_s)
        return {
            "success": True,
            "download_url": download_url,   # saves to the user's Downloads
            "preview_url": preview_url,     # opens/plays in the browser instead
            "signed_url": signed_url,       # long stateless form of the download link
            "filename": info["filename"],
            "size_bytes": info["size"],
            "media_type": info["media_type"],
            "expires_at": datetime.fromtimestamp(exp, tz=UTC).isoformat(),
            "retrieved_via": info["source"],
            "note": "download_url saves the file; preview_url opens it in the browser. "
                    "Both expire - anyone with either can access until then.",
        }

    @mcp.tool(annotations=READ_LOCAL)
    def list_view_once(chat_jid: str | None = None, limit: int = 50,
                       before: str | None = None) -> dict[str, Any]:
        """List view-once ("open once") messages that WE SENT.

        IMPORTANT - this only ever lists OUTGOING view-once media, and that is
        a hard limit, not a gap to work around. Since Meta's server-side fix of
        ~Nov 2024, view-once media sent TO this account is never delivered to a
        linked device at all: the server strips the encrypted payload and sends
        only an `unavailable` placeholder. whatsmeow then asks the primary
        phone to resend and the phone declines. There is no ciphertext to
        decrypt and no code change that obtains one. Only the phone itself ever
        holds incoming view-once media.

        So: if the user asks to save a view-once photo SOMEONE SENT THEM, the
        honest answer is that it is not possible through this bridge - do not
        imply otherwise or go hunting for the message id. It was never stored.

        For our own sends, save_media / view_media work normally, including
        after the recipient has burned their view.

        Only sends made after 2026-07-16 are flagged; earlier ones are
        indistinguishable from ordinary media (the envelope is unwrapped before
        storage, so there is nothing to back-fill from).

        Args:
            chat_jid: Restrict to one chat. Omit to search every chat.
            limit: Max rows (default 50)
            before: ISO-8601 timestamp; only return messages older than this
        """
        rows = whatsapp_list_view_once(chat_jid=chat_jid, limit=limit, before=before)
        return {
            "count": len(rows),
            "messages": rows,
            "note": (
                "Outgoing view-once only. Incoming view-once media is never delivered "
                "to a linked device (Meta server-side fix, ~Nov 2024) - the payload is "
                "stripped before it reaches us, so it cannot be listed or saved here. "
                "Flagged from 2026-07-16 onward only."
            ),
        }

    @mcp.tool(annotations=READ_BRIDGE)
    def save_media(message_id: str, chat_jid: str,
                   ttl_minutes: int = 15) -> dict[str, Any]:
        """Get a download link that saves WhatsApp media to the user's Downloads
        folder - the DEFAULT for any download/save/keep/"send me" request, and the
        ONLY tool that puts a file on their machine (view_media just renders
        inline). Works on DELETED messages and offline senders, pulling from
        WhatsApp's CDN. Returns download_url (saves), preview_url (opens in the
        browser), and signed_url. The link is a credential; keep the TTL short.

        Args:
            message_id: The ID of the message containing the media
            chat_jid: The JID of the chat containing the message
            ttl_minutes: How long the link stays valid (1-1440, default 15)
        """
        try:
            info = media_fetch(message_id, chat_jid)
            return _link_payload(message_id, chat_jid, info, ttl_minutes)
        except MediaError as e:
            return {"success": False, "message": str(e)}

    @mcp.tool(annotations=READ_BRIDGE)
    def view_media(message_id: str, chat_jid: str) -> Image | Audio | File | dict[str, Any]:
        """Fetch WhatsApp media inline so YOU (the model) can see/hear it and
        answer about its CONTENTS ("what's in this photo?", "which verse is
        this?", "what does this voice note say?"). Does NOT save a file - use
        save_media to download. Works on deleted messages and offline senders
        (pulls from CDN). Oversized media returns a link instead. Use
        list_media_in_chat to enumerate media first.

        Args:
            message_id: The ID of the message containing the media
            chat_jid: The JID of the chat containing the message
        """
        # Same retrieval as save_media (CDN first, bridge fallback) rather than
        # the bridge directly. The bridge cannot serve revoked media, and its
        # media-retry fallback needs the SENDER's phone online - which fails
        # routinely in practice ("timed out waiting for media retry"), leaving
        # this tool returning an error where the CDN would have worked.
        try:
            info = media_fetch(message_id, chat_jid)
        except MediaError as e:
            return {"success": False, "message": str(e)}

        if info["size"] > INLINE_MAX_BYTES:
            link = _link_payload(message_id, chat_jid, info, 15)
            link["note"] = (
                f"{info['size']:,} bytes is over the {INLINE_MAX_BYTES:,} byte inline cap; "
                f"returning a download link instead. " + link["note"]
            )
            return link

        return _wrap_inline(info)

    @mcp.tool(annotations=READ_LOCAL)
    def list_media_in_chat(
        chat_jid: str,
        media_type: str | None = None,
        limit: int = 50,
        before_timestamp: str | None = None,
    ) -> list[dict[str, Any]]:
        """Enumerate media messages in a chat (without downloading them).

        Useful for "what voice notes did X send last week" workflows -
        returns IDs/timestamps/sizes only. Pair with save_media (to get a file) or
        view_media (to look at one) to
        actually fetch a specific one.

        Args:
            chat_jid: JID of the chat
            media_type: Optional filter - "image", "video", "audio", "document"
            limit: Max records to return (default 50, max 100)
            before_timestamp: ISO-8601 cursor for older-page pagination
        """
        return whatsapp_list_media_in_chat(chat_jid, media_type, limit, before_timestamp)

    # --- Batch E: Tier-1 feature tools -----------------------------------

    @mcp.tool(annotations=READ_BRIDGE)
    def bridge_health() -> dict[str, Any]:
        """Get the WhatsApp bridge connection state (connected, logged_in, push_name)."""
        return whatsapp_bridge_health()

    @mcp.tool(annotations=MUTATE)
    def react_to_message(
        chat_jid: str, message_id: str, emoji: str,
        sender_jid: str | None = None,
    ) -> dict[str, Any]:
        """React to a WhatsApp message with an emoji (empty string clears the reaction).

        Args:
            chat_jid: JID of the chat containing the message
            message_id: ID of the message to react to
            emoji: The reaction emoji (e.g. "👍", "❤️"); empty string clears
            sender_jid: Optional - original sender's JID (required for group messages
                       you didn't send; omit for your own messages)
        """
        success, status_message = whatsapp_react_to_message(chat_jid, message_id, emoji, sender_jid)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=DESTRUCTIVE)
    def edit_message(chat_jid: str, message_id: str, new_text: str) -> dict[str, Any]:
        """Edit one of YOUR sent WhatsApp messages within the 24-hour edit window.

        Args:
            chat_jid: JID of the chat containing the message
            message_id: ID of YOUR original message to edit
            new_text: The replacement text
        """
        success, status_message = whatsapp_edit_message(chat_jid, message_id, new_text)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=DESTRUCTIVE)
    def delete_message(
        chat_jid: str, message_id: str,
        sender_jid: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Delete a WhatsApp message for everyone (revoke). Revoke window is 1h08m.

        Args:
            chat_jid: JID of the chat containing the message
            message_id: ID of the message to revoke
            sender_jid: Optional - original sender's JID (omit for your own messages)
            confirm: Must be True. Required by G10 - irreversible action guard.
        """
        if (denied := _require_confirm(confirm)) is not None:
            return denied
        success, status_message = whatsapp_delete_message(chat_jid, message_id, sender_jid)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=MUTATE)
    def mark_read(
        chat_jid: str, message_ids: list[str],
        sender_jid: str | None = None,
    ) -> dict[str, Any]:
        """Send read receipts (double blue tick) for one or more messages.

        Args:
            chat_jid: JID of the chat
            message_ids: List of message IDs to mark as read
            sender_jid: Optional - participant JID for group messages (omit for direct chats)
        """
        success, status_message = whatsapp_mark_read(chat_jid, message_ids, sender_jid)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=MUTATE)
    def send_presence(chat_jid: str, state: str = "composing", media: str | None = None) -> dict[str, Any]:
        """Send a typing or recording indicator to a chat.

        Args:
            chat_jid: JID of the chat
            state: "composing" (typing) or "paused"
            media: Optional - "audio" to indicate voice recording (combined with state="composing")
        """
        success, status_message = whatsapp_send_presence(chat_jid, state, media)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=READ_BRIDGE)
    def get_profile_picture(jid: str, preview: bool = False) -> dict[str, Any]:
        """Get the URL of a contact's or group's profile picture.

        Args:
            jid: JID of the user/group whose picture to fetch
            preview: If True, request a low-resolution preview instead of the full image
        """
        return whatsapp_get_profile_picture(jid, preview)

    # --- Batch H: Tier-2 message features --------------------------------

    @mcp.tool(annotations=SEND)
    def send_location(
        recipient: str, latitude: float, longitude: float,
        name: str | None = None, address: str | None = None,
    ) -> dict[str, Any]:
        """Share a static map pin with a contact or group.

        Args:
            recipient: Phone number with country code (no + or symbols), or a JID
            latitude: Decimal latitude (e.g. 30.0444)
            longitude: Decimal longitude (e.g. 31.2357)
            name: Optional place name (e.g. "Cairo")
            address: Optional street address
        """
        success, status_message = whatsapp_send_location(recipient, latitude, longitude, name, address)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=MUTATE)
    def set_disappearing(chat_jid: str, seconds: int) -> dict[str, Any]:
        """Set the ephemeral / disappearing message timer for a chat.

        Args:
            chat_jid: JID of the chat (individual or group)
            seconds: Timer in seconds. Common values: 0 (off), 86400 (24h),
                     604800 (7d), 7776000 (90d). Anything > 0 enables.
        """
        success, status_message = whatsapp_set_disappearing(chat_jid, seconds)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=SEND)
    def send_view_once_media(
        recipient: str,
        media_path: str | None = None,
        media_url: str | None = None,
        media_data: str | None = None,
        filename: str | None = None,
    ) -> dict[str, Any]:
        """Send an image or video as a 'view once' WhatsApp message.

        Recipient's official WhatsApp app shows the media exactly once then
        hides it. Other clients (including this bridge's media tools) can
        still fetch raw bytes - treat as a UX hint, not security.

        Specify EXACTLY ONE of media_path / media_url / media_data. For files
        on your LLM client's machine, base64-encode and pass via media_data
        with filename set (e.g. filename="aot.jpg").

        Args:
            recipient: Phone number (country code, no + or symbols) or JID
            media_path: optional - local path on the MCP container
            media_url: optional - http(s) URL to fetch (max 64MB)
            media_data: optional - base64-encoded raw bytes
            filename: optional - filename hint (recommended for media_data)
        """
        success, status_message = whatsapp_send_view_once_media(
            recipient, media_path,
            media_url=media_url, media_data=media_data, filename=filename,
        )
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=SEND)
    def create_poll(
        recipient: str, name: str, options: list[str],
        selectable_options_count: int = 1,
    ) -> dict[str, Any]:
        """Send a poll to a chat; returns message_id. Read the tally with
        get_poll_results, cast votes with vote_in_poll. selectable_options_count
        caps how many options each person may pick, but it is enforced only by
        the recipient's WhatsApp app, not the server - vote_in_poll ignores it,
        so you can cap everyone else while voting past the cap yourself.

        Args:
            recipient: Phone number with country code (no + or symbols), or a JID
            name: Poll question
            options: List of at least 2 option strings
            selectable_options_count: Max options each voter may select.
                1 = single-choice (default). N = up to N. Must not exceed
                len(options), or WhatsApp treats it as unlimited.
        """
        success, status_message = whatsapp_create_poll(recipient, name, options, selectable_options_count)
        return {"success": success, "message": status_message}

    # --- Batch I: group ops + blocklist + newsletters --------------------

    @mcp.tool(annotations=SEND)
    def create_group(subject: str, participants: list[str]) -> dict[str, Any]:
        """Create a new WhatsApp group with the caller as admin.

        Args:
            subject: Group name
            participants: List of phone numbers (country code, no +/symbols) or JIDs
        """
        return whatsapp_create_group(subject, participants)

    @mcp.tool(annotations=DESTRUCTIVE)
    def update_group_participants(
        group_jid: str, action: str, participants: list[str],
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Add, remove, promote, or demote group members. Caller must be an admin.

        Args:
            group_jid: Group JID (ends with @g.us)
            action: "add" | "remove" | "promote" | "demote"
            participants: List of JIDs to act on
            confirm: Must be True for remove/demote (G10 guard). add/promote skip the check.
        """
        if action in ("remove", "demote"):
            if (denied := _require_confirm(confirm)) is not None:
                return denied
        success, status_message = whatsapp_update_group_participants(group_jid, action, participants)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=READ_BRIDGE)
    def get_group_info(group_jid: str) -> dict[str, Any]:
        """Get full metadata for a group: name, topic, participants, admin status.

        Args:
            group_jid: Group JID (ends with @g.us)
        """
        return whatsapp_get_group_info(group_jid)

    @mcp.tool(annotations=READ_BRIDGE)
    def list_joined_groups() -> dict[str, Any]:
        """List all WhatsApp groups the bridge account is a member of."""
        return whatsapp_list_joined_groups()

    @mcp.tool(annotations=READ_BRIDGE)
    def get_blocklist() -> dict[str, Any]:
        """Get the list of currently blocked contact JIDs."""
        return whatsapp_get_blocklist()

    @mcp.tool(annotations=DESTRUCTIVE)
    def block_user(jid: str, confirm: bool = False) -> dict[str, Any]:
        """Block a WhatsApp contact (they will not see you online and cannot message you).

        Args:
            jid: JID of the contact to block (typically <number>@s.whatsapp.net)
            confirm: Must be True. Required by G10 - hard-to-reverse for the recipient.
        """
        if (denied := _require_confirm(confirm)) is not None:
            return denied
        success, status_message = whatsapp_block_user(jid)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=MUTATE)
    def unblock_user(jid: str) -> dict[str, Any]:
        """Unblock a previously-blocked WhatsApp contact.

        Args:
            jid: JID of the contact to unblock
        """
        success, status_message = whatsapp_unblock_user(jid)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=READ_BRIDGE)
    def list_subscribed_newsletters() -> dict[str, Any]:
        """List all WhatsApp newsletters/channels the bridge account follows."""
        return whatsapp_list_subscribed_newsletters()

    # --- G2: message receipt read-back -------------------------------------

    @mcp.tool(annotations=READ_LOCAL)
    def get_message_receipts(message_id: str, chat_jid: str) -> dict[str, Any]:
        """Get delivery / read / played receipts for a message you sent.

        Returns ISO-8601 timestamps for whichever of delivered_at, read_at,
        and played_at have been received from the recipient. Missing keys
        mean that receipt type hasn't arrived (recipient offline, hasn't
        opened the chat, or hasn't tapped play on a voice note).
        """
        return whatsapp_get_message_receipts(message_id, chat_jid)

    # --- batch omicron: diagnostics + sender activity + cross-chat + reactions search --

    @mcp.tool(annotations=MUTATE)
    def backfill_group_participants() -> dict[str, Any]:
        """One-shot: populate group_participants from existing message history.

        Runs a single INSERT-SELECT that groups messages by (chat_jid, sender).
        Idempotent - repeat calls only touch new (group, jid) pairs. Can take
        30-60 seconds on stores with 100k+ messages.
        """
        return whatsapp_backfill_group_participants()

    @mcp.tool(annotations=READ_BRIDGE)
    def get_bridge_diagnostics() -> dict[str, Any]:
        """Extended diagnostics: WAL bytes, reactions/participants counts,
        group count, chats with no name, uptime. Superset of get_bridge_stats.
        """
        return whatsapp_get_bridge_diagnostics()

    @mcp.tool(annotations=READ_LOCAL)
    def sender_activity(sender_jid: str, days: int = 30) -> dict[str, Any]:
        """Cross-chat activity for one sender over the last N days.

        Returns {total_messages, by_chat[top 20], by_day[]}.
        """
        return whatsapp_sender_activity(sender_jid, days)

    @mcp.tool(annotations=READ_LOCAL)
    def list_all_media_by_type(media_type: str, limit: int = 50,
                               before: str | None = None) -> list[dict[str, Any]]:
        """Cross-chat media search by type ("image" / "video" / "audio" /
        "document"). Newest first; pass `before` (ISO) for cursor pagination.
        """
        return whatsapp_list_all_media_by_type(media_type, limit, before)

    @mcp.tool(annotations=READ_LOCAL)
    def search_by_reaction(emoji: str, limit: int = 50) -> list[dict[str, Any]]:
        """Find messages that received a specific reaction emoji."""
        return whatsapp_search_by_reaction(emoji, limit)

    # --- batch xi: reply threads + group members (offline) ----------------

    @mcp.tool(annotations=READ_LOCAL)
    def get_replies_to(message_id: str, chat_jid: str, limit: int = 50) -> list[dict[str, Any]]:
        """List messages that quote-reply to the given message."""
        return whatsapp_get_replies_to(message_id, chat_jid, limit)

    @mcp.tool(annotations=READ_LOCAL)
    def get_message_thread(chat_jid: str, message_id: str) -> dict[str, Any]:
        """Return {original, replies, reply_count} for a message thread."""
        return whatsapp_get_message_thread(chat_jid, message_id)

    @mcp.tool(annotations=READ_LOCAL)
    def groups_with_member(jid: str, limit: int = 50) -> list[dict[str, Any]]:
        """Which groups contain this contact (based on observed activity).

        Args:
            jid: Contact JID (e.g. "12345@s.whatsapp.net") or bare phone.
            limit: Max groups (default 50).
        """
        return whatsapp_groups_with_member(jid, limit)

    @mcp.tool(annotations=READ_LOCAL)
    def list_group_members(group_jid: str, limit: int = 200) -> list[dict[str, Any]]:
        """Members of a group (based on observed activity in messages)."""
        return whatsapp_list_group_members(group_jid, limit)

    # --- batch nu: analytics + export + reactions read --------------------

    @mcp.tool(annotations=READ_LOCAL)
    def get_reactions_on(message_id: str, chat_jid: str) -> dict[str, Any]:
        """List all reactions on a specific message (who reacted, with what emoji, when)."""
        return whatsapp_get_reactions_on(message_id, chat_jid)

    @mcp.tool(annotations=READ_LOCAL)
    def get_chat_stats(chat_jid: str) -> dict[str, Any]:
        """Summarise a chat: total messages, from_me count, distinct senders,
        first/last message timestamps, top-5 senders, and media type breakdown.
        """
        return whatsapp_get_chat_stats(chat_jid)

    @mcp.tool(annotations=READ_LOCAL)
    def get_top_active_chats(hours: int = 24, limit: int = 10) -> list[dict[str, Any]]:
        """Chats sorted by message count in the last N hours (default 24)."""
        return whatsapp_get_top_active_chats(hours, limit)

    @mcp.tool(annotations=READ_LOCAL)
    def get_quiet_chats(days: int = 30, limit: int = 20) -> list[dict[str, Any]]:
        """Chats you haven't heard from in the last N days (default 30), oldest first."""
        return whatsapp_get_quiet_chats(days, limit)

    @mcp.tool(annotations=READ_LOCAL)
    def count_messages(chat_jid: str | None = None,
                       sender: str | None = None,
                       media_type: str | None = None,
                       after: str | None = None,
                       before: str | None = None) -> dict[str, Any]:
        """Cheap COUNT(*) with optional filters. Pass media_type="" for
        text-only, media_type="image" for images, etc.
        """
        return whatsapp_count_messages(chat_jid, sender, media_type, after, before)

    @mcp.tool(annotations=READ_LOCAL)
    def messages_by_day(chat_jid: str, days: int = 30) -> list[dict[str, Any]]:
        """Daily message-count histogram for a chat over the last N days."""
        return whatsapp_messages_by_day(chat_jid, days)

    @mcp.tool(annotations=READ_LOCAL)
    def export_chat(chat_jid: str, since: str | None = None,
                    fmt: str = "markdown", limit: int = 500) -> dict[str, Any]:
        """LLM-friendly chat export.

        Args:
            chat_jid: JID of the chat to export.
            since: Optional ISO-8601 lower bound; omit for full history (capped by limit).
            fmt: "markdown" (default), "text", or "json".
            limit: Max messages (default 500, hard cap 5000).
        """
        return whatsapp_export_chat(chat_jid, since, fmt, limit)

    # --- batch lambda: send_sticker ---------------------------------------

    @mcp.tool(annotations=SEND)
    def send_sticker(recipient: str,
                     media_path: str | None = None,
                     media_url: str | None = None,
                     media_data: str | None = None,
                     filename: str | None = None) -> dict[str, Any]:
        """Send a WhatsApp sticker (WebP, ideally 512x512).

        Same input flexibility as send_file: exactly ONE of media_path,
        media_url, or media_data. WhatsApp accepts static or animated WebP.
        Non-.webp inputs are uploaded as-is; WhatsApp may render them as a
        regular image instead of a sticker.
        """
        success, status_message = whatsapp_send_sticker(
            recipient, media_path=media_path, media_url=media_url,
            media_data=media_data, filename=filename,
        )
        return {"success": success, "message": status_message}

    # --- batch kappa: poll vote + bridge stats + media metadata ------------

    @mcp.tool(annotations=SEND)
    def vote_in_poll(poll_chat_jid: str, poll_message_id: str,
                     option_names: list[str],
                     poll_sender_jid: str | None = None) -> dict[str, Any]:
        """Cast a vote on any poll (yours or someone else's). Votes REPLACE the
        voter's prior selection - they do not accumulate. This tool IGNORES the
        poll's selectable_options_count cap and can select more options than
        allowed (the cap is enforced only by the WhatsApp app, not the server).

        Args:
            poll_chat_jid: JID of the chat containing the poll.
            poll_message_id: Message ID of the poll.
            option_names: Option strings to vote for. Must match the poll's
                          option text exactly (votes reference options by
                          sha256 of their name, so a typo silently votes for
                          nothing). May exceed the poll's cap - see above.
            poll_sender_jid: Required for group polls (original creator's JID).
                             Optional for direct chats or your own poll.
        """
        success, status_message = whatsapp_vote_in_poll(
            poll_chat_jid, poll_message_id, option_names, poll_sender_jid
        )
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=READ_LOCAL)
    def get_poll_results(message_id: str, chat_jid: str) -> dict[str, Any]:
        """Read a poll's results: vote counts per option and who voted for what,
        zero-vote options included. Use after create_poll/vote_in_poll. Polls
        created before the bridge began recording votes cannot be decoded - the
        tool says so rather than reporting an empty poll.

        Args:
            message_id: Message ID of the poll (create_poll returns it)
            chat_jid: JID of the chat containing the poll
        """
        return whatsapp_get_poll_results(message_id, chat_jid)

    @mcp.tool(annotations=READ_BRIDGE)
    def transcribe_voice(message_id: str, chat_jid: str) -> dict[str, Any]:
        """Transcribe a voice note / audio message to text.

        Voice notes are also transcribed automatically in the background (from
        2026-07-15 onward) and stored, so usually you can just read the text
        via get_message_context or search it with search_voice_notes. Use this
        tool to force a transcript now, or to (re)transcribe one on demand.

        Egyptian Arabic and Arabic/English code-switching are handled well
        (Speechmatics). English words come back written in Arabic script.

        Args:
            message_id: The ID of the voice/audio message
            chat_jid: The JID of the chat containing it
        """
        import transcription
        return transcription.transcribe_now(message_id, chat_jid)

    @mcp.tool(annotations=READ_LOCAL)
    def search_voice_notes(query: str, chat_jid: str | None = None,
                           limit: int = 20) -> dict[str, Any]:
        """Search across transcribed voice notes by their spoken content.

        Full-text search over voice-note transcripts (Arabic-aware). Returns
        matching messages with a snippet; pair the message_id with
        get_message_context to see it in its chat, or transcribe_voice for the
        full text.

        NOTE: this matches SPOKEN words only. A clip that is a sound with no
        speech (sound effect, music, animal noise, laughter, silence) has an
        empty transcript and never matches here - use list_nonspeech_voices for
        those, then view_media to identify each by listening.

        Args:
            query: Words/phrase to find in what people said in voice notes.
            chat_jid: Restrict to one chat. Omit to search all.
            limit: Max results (default 20).
        """
        import transcription
        rows = transcription.search(query, chat_jid=chat_jid, limit=limit)
        out = {"count": len(rows), "results": rows, "stats": transcription.stats()}
        if not rows:
            out["hint"] = ("No spoken-word match. If you're after a NON-SPEECH clip "
                           "(sound effect, music, animal noise, laughter), it has an "
                           "empty transcript and won't match text search - call "
                           "list_nonspeech_voices (scope by chat_jid/after/before), "
                           "then view_media each to identify it by listening.")
        return out

    @mcp.tool(annotations=READ_LOCAL)
    def list_nonspeech_voices(chat_jid: str | None = None,
                              after: str | None = None,
                              before: str | None = None,
                              limit: int = 20) -> dict[str, Any]:
        """List voice/video notes that contain NO speech - sound-only clips
        (sound effects, music, animal noises, laughter, silence) whose transcript
        came back empty. Text search (search_voice_notes) is blind to these
        because there are no spoken words to match; this is how you find them.

        Typical use: a request like "find the voice note that's just a wolf howl".
        Enumerate candidates here (scope by chat and/or time to narrow), then call
        view_media on each to hear it and identify the right one.

        Args:
            chat_jid: Restrict to one chat. Omit for all chats.
            after: Only clips whose message time is >= this ISO date/datetime
                   (e.g. "2026-07-16" or "2026-07-16T23:00:00").
            before: Only clips whose message time is <= this ISO date/datetime.
            limit: Max results, newest first (default 20).
        """
        import transcription
        rows = transcription.list_nonspeech(chat_jid=chat_jid, after=after,
                                            before=before, limit=limit)
        return {
            "count": len(rows),
            "results": rows,
            "note": ("These clips have no spoken words. Call view_media on a "
                     "message_id to listen and identify it."),
        }

    @mcp.tool(annotations=READ_LOCAL)
    def resolve_identity(number_or_lid: str) -> dict[str, Any]:
        """Resolve who a WhatsApp sender is - unify a person's phone number and
        their group LID, with their name.

        WhatsApp hides phone numbers behind LIDs inside groups, so the SAME person
        appears under their phone in DMs and under a different long numeric LID in
        group history. This maps between them using whatsmeow's authoritative
        lid<->phone table - no guessing.

        Use it when a sender is an unfamiliar long number (a LID), or before
        pulling one person's history so you query BOTH of their identities. Pass a
        phone number, a LID, or a JID (with or without @s.whatsapp.net / @lid).

        Returns: phone, lid, name, display ("Name (phone)"), and `senders` - the
        raw sender values (phone + lid) to filter the message store on so you get
        the person's COMPLETE history, not just half of it. Unknown ids echo back
        with resolved=false (never a wrong name).

        To go from a NAME to identities, use search_contacts (or this tool only
        resolves ids). Related: list_person_messages pulls a person's messages
        across both identities in one call.
        """
        import identity
        return identity.resolve(number_or_lid)

    @mcp.tool(annotations=READ_LOCAL)
    def list_person_messages(person: str, chat_jid: str | None = None,
                             after: str | None = None, before: str | None = None,
                             limit: int = 100, newest_first: bool = True) -> dict[str, Any]:
        """Return one person's messages, UNIFIED across their phone and their
        group LID identity - so you see their whole history, not the fragment
        stored under one identity.

        This exists because WhatsApp splits a person between their phone number
        (DMs) and a LID (groups); a naive "sender = X" query silently misses
        whichever half is stored under the other id. Here you pass the person
        once (phone number, LID, or JID) and it queries every identity they have.

        Scope with chat_jid (recommended - keeps reply context intact) and/or a
        time window. Voice/video notes include their transcript inline when one
        exists. Each row's `sender_label` shows the resolved name.

        Args:
            person: phone number, LID, or JID of the person.
            chat_jid: restrict to one chat (recommended). Omit for all chats.
            after / before: ISO date/datetime bounds on message time.
            limit: max messages (default 100).
            newest_first: order newest->oldest (default) or oldest->newest.
        """
        import identity as _id
        info = _id.resolve(person)
        senders = info["senders"]
        if not senders:
            return {"person": person, "resolved": False, "count": 0, "messages": [],
                    "note": "Could not resolve this person to any sender id. If you have a name, use search_contacts first."}

        import os as _os
        import sqlite3 as _sq
        mdb = _os.environ.get("MESSAGES_DB_PATH", "/data/store/messages.db")
        tdb = _os.environ.get("TRANSCRIPTS_DB", "/var/log/wamcp/transcripts.db")
        where = ["sender IN ({})".format(",".join("?" * len(senders)))]
        params: list = list(senders)
        if chat_jid:
            where.append("chat_jid = ?"); params.append(chat_jid)
        if after:
            where.append("timestamp >= ?"); params.append(after)
        if before:
            where.append("timestamp <= ?"); params.append(before)
        order = "DESC" if newest_first else "ASC"
        params.append(int(limit))
        conn = _sq.connect(f"file:{mdb}?mode=ro", uri=True)
        tx: dict = {}
        try:
            rows = conn.execute(
                f"""SELECT m.id, m.chat_jid, c.name, m.sender, m.content, m.timestamp, m.media_type
                    FROM messages m LEFT JOIN chats c ON c.jid = m.chat_jid
                    WHERE {' AND '.join(where)} ORDER BY m.timestamp {order} LIMIT ?""",
                tuple(params)).fetchall()
            # Inline transcripts for any voice/video rows (best-effort, read-only).
            vids = [r[0] for r in rows if (r[6] or "") in ("audio", "ptt", "voice", "video")]
            if vids:
                try:
                    tconn = _sq.connect(f"file:{tdb}?mode=ro", uri=True)
                    qmarks = ",".join("?" * len(vids))
                    for mid, text in tconn.execute(
                        f"SELECT message_id, text FROM transcripts WHERE message_id IN ({qmarks}) AND status='done'",
                        tuple(vids)):
                        if text:
                            tx[mid] = text
                    tconn.close()
                except _sq.Error:
                    pass
        finally:
            conn.close()

        label = info["display"]
        out = []
        for mid, cj, cname, snd, content, ts, mtype in rows:
            row = {"message_id": mid, "chat_jid": cj, "chat_name": cname,
                   "sender": snd, "sender_label": label, "timestamp": ts,
                   "content": content, "media_type": mtype or None}
            if mid in tx:
                row["transcript"] = tx[mid]
            out.append(row)
        return {"person": person, "resolved": info["resolved"], "name": info["name"],
                "phone": info["phone"], "lid": info["lid"], "identities_queried": senders,
                "count": len(out), "messages": out}

    @mcp.tool(annotations=READ_BRIDGE)
    def get_bridge_stats() -> dict[str, Any]:
        """Operational stats: connected, logged_in, chats/messages/media counts,
        db size, uptime, oldest/newest message timestamps.
        """
        return whatsapp_get_bridge_stats()

    @mcp.tool(annotations=READ_LOCAL)
    def get_media_info(message_id: str, chat_jid: str) -> dict[str, Any]:
        """Return media metadata (mime, size, filename, media_type) for a
        message WITHOUT downloading the payload. Cheap alternative to
        save_media / view_media for deciding whether/how to fetch.
        """
        return whatsapp_get_media_info(message_id, chat_jid)

    # --- batch iota: archive / mark-unread / set-status / list-by-state ---

    @mcp.tool(annotations=MUTATE)
    def archive_chat(chat_jid: str, archive: bool = True) -> dict[str, Any]:
        """Archive (or unarchive) a chat.

        Args:
            chat_jid: JID of the chat.
            archive: True to archive, False to unarchive.
        """
        success, status_message = whatsapp_archive_chat(chat_jid, archive)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=MUTATE)
    def mark_chat_unread(chat_jid: str, unread: bool = True) -> dict[str, Any]:
        """Mark a chat as unread (blue dot) or read.

        Args:
            chat_jid: JID of the chat.
            unread: True marks unread, False marks read.
        """
        success, status_message = whatsapp_mark_chat_unread(chat_jid, unread)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=MUTATE)
    def set_status_message(message: str) -> dict[str, Any]:
        """Update your WhatsApp 'About' text (the short bio shown in your profile)."""
        success, status_message = whatsapp_set_status_message(message)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=READ_LOCAL)
    def list_chats_by_state(state: str, limit: int = 50) -> list[dict[str, Any]]:
        """List chats matching a state flag.

        Args:
            state: One of "pinned", "muted", "archived", "unread".
            limit: Max chats to return (default 50).
        """
        return whatsapp_list_chats_by_state(state, limit)

    # --- #73 delete_chat --------------------------------------------------

    @mcp.tool(annotations=DESTRUCTIVE)
    def delete_chat(chat_jid: str, delete_media: bool = False,
                    confirm: bool = False) -> dict[str, Any]:
        """Delete a chat both remotely (WhatsApp app state) and locally.

        Args:
            chat_jid: JID of the chat to delete.
            delete_media: Also purge downloaded media on WhatsApp's side.
            confirm: Must be True (irreversible).
        """
        if (denied := _require_confirm(confirm)) is not None:
            return denied
        success, status_message = whatsapp_delete_chat(chat_jid, delete_media)
        return {"success": success, "message": status_message}

    # --- N10 labels (WhatsApp Business) -----------------------------------

    @mcp.tool(annotations=MUTATE)
    def edit_label(label_id: str, label_name: str = "", label_color: int = 0,
                   delete: bool = False) -> dict[str, Any]:
        """Create, rename, recolor, or delete a Business label.

        Args:
            label_id: Numeric string id (any positive integer as a string).
            label_name: Human-readable name (empty on delete).
            label_color: Palette index 0..19 (0 = default).
            delete: True to remove the label entirely.
        """
        success, status_message = whatsapp_edit_label(label_id, label_name, label_color, delete)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=MUTATE)
    def set_chat_label(label_id: str, chat_jid: str, labeled: bool = True) -> dict[str, Any]:
        """Attach or detach a Business label on a chat."""
        success, status_message = whatsapp_set_chat_label(label_id, chat_jid, labeled)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=MUTATE)
    def set_message_label(label_id: str, chat_jid: str, message_id: str,
                          labeled: bool = True) -> dict[str, Any]:
        """Attach or detach a Business label on a specific message."""
        success, status_message = whatsapp_set_message_label(label_id, chat_jid, message_id, labeled)
        return {"success": success, "message": status_message}

    # --- G6 chat state ops ------------------------------------------------

    @mcp.tool(annotations=MUTATE)
    def mute_chat(chat_jid: str, mute: bool = True, duration_seconds: int = 0) -> dict[str, Any]:
        """Mute (or unmute) a chat's notifications.

        Args:
            chat_jid: JID of the chat (person or group).
            mute: True to mute, False to unmute.
            duration_seconds: 0 = mute forever (default), or seconds to auto-unmute.
        """
        success, status_message = whatsapp_mute_chat(chat_jid, mute, duration_seconds)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=MUTATE)
    def pin_chat(chat_jid: str, pin: bool = True) -> dict[str, Any]:
        """Pin (or unpin) a chat to the top of the chat list."""
        success, status_message = whatsapp_pin_chat(chat_jid, pin)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=MUTATE)
    def star_message(chat_jid: str, message_id: str, starred: bool = True,
                     sender_jid: str | None = None, is_from_me: bool = False) -> dict[str, Any]:
        """Star (or unstar) a specific message.

        Args:
            chat_jid: JID of the chat containing the message.
            message_id: ID of the message.
            starred: True to star, False to unstar.
            sender_jid: Required for group messages (original sender's JID).
            is_from_me: True if the message was sent by us.
        """
        success, status_message = whatsapp_star_message(chat_jid, message_id, starred, sender_jid, is_from_me)
        return {"success": success, "message": status_message}

    # --- N9 status / privacy ----------------------------------------------

    @mcp.tool(annotations=SEND)
    def post_status(message: str) -> dict[str, Any]:
        """Post a text update to your WhatsApp status broadcast (visible to your contacts)."""
        success, status_message = whatsapp_post_status(message)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=READ_BRIDGE)
    def get_privacy_settings() -> dict[str, Any]:
        """Fetch your WhatsApp privacy settings (last_seen, status, profile, read receipts, group_add, online, call_add)."""
        return whatsapp_get_privacy_settings()

    # --- N11 forward message ----------------------------------------------

    @mcp.tool(annotations=SEND)
    def forward_message(source_chat_jid: str, message_id: str, target_jid: str) -> dict[str, Any]:
        """Forward an existing message from one chat to another (person or group).

        Preserves media when the original message has raw protobuf stored
        (messages received AFTER batch alpha only); older text messages are
        forwarded as text. The forwarded pill is shown to the recipient.

        Args:
            source_chat_jid: JID of the chat containing the original message.
            message_id: ID of the message to forward.
            target_jid: JID (or phone number) to forward TO.
        """
        success, status_message = whatsapp_forward_message(source_chat_jid, message_id, target_jid)
        return {"success": success, "message": status_message}

    # --- G3: phone resolver + user info + business profile -----------------

    @mcp.tool(annotations=READ_BRIDGE)
    def check_phones_on_whatsapp(phones: list[str]) -> dict[str, Any]:
        """Resolve raw phone numbers (E.164 without +) to WhatsApp JIDs.

        Returns per-phone: jid, on_whatsapp (bool), verified_business_name (if any).
        Use BEFORE send_message to validate an address-book before bulk sends.
        Bridge accepts 1..100 entries per call.
        """
        return whatsapp_check_phones_on_whatsapp(phones)

    @mcp.tool(annotations=READ_BRIDGE)
    def get_user_info_bulk(jids: list[str]) -> dict[str, Any]:
        """Bulk UserInfo: status text, picture_id, device count, verified-business name.

        Args:
            jids: List of WhatsApp JIDs (1..100).
        """
        return whatsapp_get_user_info_bulk(jids)

    @mcp.tool(annotations=READ_BRIDGE)
    def get_business_profile(jid: str) -> dict[str, Any]:
        """Verified WhatsApp Business profile metadata: address, email, categories, hours TZ."""
        return whatsapp_get_business_profile(jid)

    # --- G4: group invite link / join / leave ------------------------------

    @mcp.tool(annotations=READ_BRIDGE)
    def get_group_invite_link(group_jid: str, reset: bool = False) -> dict[str, Any]:
        """Get (or reset) a group's chat.whatsapp.com invite link.

        Args:
            group_jid: JID of a group you admin.
            reset: True rotates the link; old link stops working.
        """
        return whatsapp_get_group_invite_link(group_jid, reset)

    @mcp.tool(annotations=SEND)
    def join_group_by_link(link: str, preview_only: bool = False) -> dict[str, Any]:
        """Preview or join a WhatsApp group via invite link.

        Args:
            link: Either the full https://chat.whatsapp.com/<code> URL or just the code.
            preview_only: True returns the group's metadata without joining.
        """
        return whatsapp_join_group_by_link(link, preview_only)

    @mcp.tool(annotations=DESTRUCTIVE)
    def leave_group(group_jid: str, confirm: bool = False) -> dict[str, Any]:
        """Exit a WhatsApp group. Irreversible without re-invite.

        Args:
            group_jid: JID of the group to leave.
            confirm: Must be True. Required by G10 - irreversible.
        """
        if (denied := _require_confirm(confirm)) is not None:
            return denied
        success, status_message = whatsapp_leave_group(group_jid)
        return {"success": success, "message": status_message}

    # --- G5: cross-chat FTS5 search ----------------------------------------

    @mcp.tool(annotations=SEND)
    def send_contact_card(recipient: str, contacts: list[dict[str, str]]) -> dict[str, Any]:
        """Share one or more contact cards via vCard 3.0.

        Args:
            recipient: Phone number (country code, no + or symbols), or a JID.
            contacts: List of {name, phone, email?, organization?}; phone is E.164 without +.
        """
        success, status_message = whatsapp_send_contact_card(recipient, contacts)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=SEND)
    def mention_everyone(group_jid: str, text: str, delivery_timeout_seconds: int = 15) -> dict[str, Any]:
        """Send a message to a group that @-mentions every participant.

        Args:
            group_jid: Group JID (ends with @g.us). You must be a member.
            text: Message text to append after the mentions.
            delivery_timeout_seconds: Seconds to wait for WA delivery ACK (default 15).
        """
        success, status_message = whatsapp_mention_everyone(group_jid, text, delivery_timeout_seconds)
        return {"success": success, "message": status_message}

    @mcp.tool(annotations=READ_LOCAL)
    def search_all_messages(
        query: str,
        after: str | None = None,
        before: str | None = None,
        sender_jid: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search message content across ALL chats via FTS5.

        Returns up to `limit` matches with a snippet() preview (terms wrapped
        in <mark>...</mark>). Way more useful than list_messages with query
        because it doesn't require knowing the chat_jid up front.

        Args:
            query: Text to search (FTS5 word-tokenised).
            after: Optional ISO-8601 lower bound.
            before: Optional ISO-8601 upper bound.
            sender_jid: Optional filter to messages from one sender.
            limit: Max results (1..100, default 20).
            offset: Skip count for pagination.
        """
        return whatsapp_search_all_messages(query, after, before, sender_jid, limit, offset)

    @mcp.tool(annotations=READ_BRIDGE)
    def get_newsletter_info(newsletter_jid: str) -> dict[str, Any]:
        """Get metadata for a WhatsApp newsletter/channel.

        Args:
            newsletter_jid: Newsletter JID (typically ends with @newsletter)
        """
        return whatsapp_get_newsletter_info(newsletter_jid)
