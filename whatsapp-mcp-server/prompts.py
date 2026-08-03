"""G9: MCP prompt templates for one-click canonical WhatsApp workflows.

Prompts are MCP-spec primitives that the client surfaces (e.g. Claude Desktop
slash menu). Each one returns a single user-role message that calls into the
existing tool surface to do its work.
"""
from __future__ import annotations



def register(mcp) -> None:
    """Attach all WhatsApp prompt templates to the given FastMCP instance."""

    @mcp.prompt(
        name="summarize_chat",
        description="Summarize a chat's recent activity in 5-8 bullets.",
    )
    def summarize_chat(chat_jid: str, since_iso: str | None = None) -> str:
        return (
            f"Use list_messages with chat_jid='{chat_jid}'"
            + (f", after='{since_iso}'" if since_iso else "")
            + ", limit=80, include_context=false. From the returned messages, "
            "produce a tight 5-8 bullet summary covering: who participated, "
            "the main topics, any decisions or commitments, any open questions, "
            "and the overall mood. Quote the most important single message verbatim."
        )

    @mcp.prompt(
        name="draft_reply",
        description="Draft a thoughtful reply to the last message in a chat.",
    )
    def draft_reply(chat_jid: str, intent: str | None = None) -> str:
        intent_clause = f" My intended reply is: {intent}." if intent else ""
        return (
            f"Call get_last_interaction(jid='{chat_jid}') and list_messages("
            f"chat_jid='{chat_jid}', limit=10, include_context=false). Read "
            "the recent context. Then draft a reply that I can paste into "
            f"WhatsApp.{intent_clause} Match the language of the other person "
            "(Arabic / English / mixed) and keep the tone consistent with "
            "the prior thread. Output only the reply text, no preamble."
        )

    @mcp.prompt(
        name="catch_me_up",
        description="Briefing on every chat with new messages since a given timestamp.",
    )
    def catch_me_up(since_iso: str) -> str:
        return (
            "Use list_chats with sort_by='last_active', limit=20 to find chats "
            f"with activity. For each chat where last_message_time > '{since_iso}', "
            "use list_messages(chat_jid=..., limit=20, include_context=false) and "
            "summarize in ONE line. Format the output as a Markdown table with "
            "columns: Chat, Last Activity, One-line Summary, Action Needed (Y/N). "
            "Sort by 'Action Needed' descending so urgent items float to the top."
        )

    @mcp.prompt(
        name="weekly_digest",
        description="Markdown weekly digest of WhatsApp activity for the last 7 days.",
    )
    def weekly_digest() -> str:
        from datetime import datetime, timedelta
        since = (datetime.utcnow() - timedelta(days=7)).isoformat(timespec="seconds")
        return (
            f"Produce a weekly WhatsApp digest covering messages since {since}. "
            "Use list_chats(sort_by='last_active', limit=30) to find active "
            "chats, then for each: list_messages(chat_jid=..., limit=30, "
            f"include_context=false, after='{since}'). Output Markdown with "
            "sections: '## Top conversations', '## Key decisions and commitments', "
            "'## Open questions', '## People who waited too long for a reply'. "
            "Use search_all_messages to find any messages containing 'urgent' / "
            "'asap' / 'today' / 'deadline' and highlight them."
        )

    @mcp.prompt(
        name="find_unanswered",
        description="Find threads where the OTHER side sent the last message and you haven't replied.",
    )
    def find_unanswered(min_age_hours: int = 4) -> str:
        return (
            "Use list_chats(sort_by='last_active', limit=40, include_last_message=true). "
            f"Filter to chats where last_is_from_me == false AND the chat's last_message_time "
            f"is older than {min_age_hours} hour(s) ago. For each, call get_last_interaction "
            "and present the result as Markdown bullets: '- **chat name** (jid) - last msg from "
            "<sender>: \"<content snippet>\"'. Sort by oldest unreplied first. "
            "Append a one-line tldr at the end (e.g. '6 unanswered chats; oldest waited 2 days')."
        )
