"""Shared fixtures.

These tests exercise the pure/offline half of the server: identity resolution
and tool retrieval. Neither needs WhatsApp, the bridge, or the network - the
identity resolver reads a whatsmeow store, so we build a synthetic one with the
same two tables the real store exposes.
"""
from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[2] / "whatsapp-mcp-server"
sys.path.insert(0, str(SERVER_DIR))


def _build_store(path: Path) -> None:
    """A minimal whatsmeow store: the lid<->pn map and the contacts table.

    Only the columns identity.py actually selects. Deliberately includes the
    awkward cases: a person with a LID but no saved name, a person with a saved
    name but no LID, and a contact whose saved name differs from their push
    name (the case that motivated storing both).
    """
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT);
        CREATE TABLE whatsmeow_contacts (
            their_jid TEXT PRIMARY KEY, first_name TEXT, full_name TEXT,
            push_name TEXT, business_name TEXT
        );
        """
    )
    c.executemany(
        "INSERT INTO whatsmeow_lid_map (lid, pn) VALUES (?, ?)",
        [
            ("10000000000001@lid", "201234567890@s.whatsapp.net"),  # named contact
            ("10000000000002@lid", "201234567891@s.whatsapp.net"),  # unnamed
        ],
    )
    c.executemany(
        "INSERT INTO whatsmeow_contacts VALUES (?, ?, ?, ?, ?)",
        [
            # saved name differs from what they call themselves
            ("201234567890@s.whatsapp.net", "Alex", "Alex Doe", "al3x", None),
            # push name only - never saved to the address book
            ("201234567892@s.whatsapp.net", None, None, "Jordan", None),
            # business contact, no personal name
            ("201234567893@s.whatsapp.net", None, None, None, "Cairo Coffee"),
        ],
    )
    c.commit()
    c.close()


@pytest.fixture()
def identity(tmp_path, monkeypatch):
    """A freshly-imported identity module bound to a synthetic store.

    identity.py memoises the store in module globals on first use, so each test
    gets a reloaded module rather than a shared cache.
    """
    db = tmp_path / "whatsapp.db"
    _build_store(db)
    monkeypatch.setenv("WHATSMEOW_DB_PATH", str(db))
    mod = importlib.import_module("identity")
    return importlib.reload(mod)


@pytest.fixture()
def toolsearch():
    """A freshly-imported toolsearch module with no captured library."""
    mod = importlib.import_module("toolsearch")
    return importlib.reload(mod)


class FakeTool:
    """Stands in for a FastMCP Tool. capture() only reads these attributes."""

    def __init__(self, name: str, description: str, parameters: dict | None = None):
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}


@pytest.fixture()
def library(toolsearch):
    """A small but realistic tool library, captured into the search index."""
    tools = [
        FakeTool("send_message", "Send a WhatsApp text message to a chat.",
                 {"type": "object",
                  "properties": {"chat_jid": {"type": "string"},
                                 "message": {"type": "string"},
                                 "reply_to_message_id": {"type": "string"}},
                  "required": ["chat_jid", "message"]}),
        FakeTool("list_messages", "List messages in a chat, newest first."),
        FakeTool("search_contacts", "Search contacts by name or phone number."),
        FakeTool("transcribe_voice", "Transcribe a voice note to text."),
        FakeTool("create_poll", "Create a poll in a group chat."),
        FakeTool("block_user", "Add a contact to the blocklist."),
        FakeTool("schedule_message", "Queue a message to be sent at a later time."),
        FakeTool("mark_read", "Mark a chat's messages as read."),
    ]
    toolsearch.capture(tools)
    return toolsearch
