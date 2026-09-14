"""Tools must return what their output schema declares.

MCP clients that validate structured output (the fastmcp Client does) reject a
result that does not match the tool's outputSchema, and a tool that declares a
schema but returns nothing gives the caller an empty, error-free result. This
boots server.py in a subprocess against a throwaway messages.db built from the
bridge's own CREATE/ALTER statements, runs the tools through FastMCP's tool
runner (below the auth middleware, so no token is needed), and validates every
structured result against the tool's schema.

Offline: no bridge, no network. Skipped when fastmcp or jsonschema is missing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")
pytest.importorskip("jsonschema")

REPO = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO / "whatsapp-mcp-server"
BRIDGE_MAIN = REPO / "whatsapp-bridge" / "main.go"

JID = "10000000000@s.whatsapp.net"
MISSING_JID = "19999999999@s.whatsapp.net"

PROBE = """
import asyncio, json, sys
import jsonschema
import server
from fastmcp.exceptions import ToolError

async def main():
    out = []
    for name, args in json.loads(sys.argv[1]):
        tool = await server.mcp.get_tool(name)
        try:
            result = await tool.run(args)
        except ToolError as exc:
            out.append({"name": name, "outcome": "tool_error", "message": str(exc)})
            continue
        structured = result.structured_content
        if tool.output_schema is not None:
            if structured is None:
                out.append({"name": name, "outcome": "missing_structured_content"})
                continue
            jsonschema.validate(structured, tool.output_schema)
        out.append({"name": name, "outcome": "ok", "structured": structured})
    print(json.dumps(out, default=str))

asyncio.run(main())
"""

CASES = [
    ["list_messages", {"limit": 5, "include_context": False}],
    ["get_chat", {"chat_jid": JID}],
    ["get_chat", {"chat_jid": MISSING_JID}],
    ["get_direct_chat_by_contact", {"sender_phone_number": "10000000000"}],
    ["get_direct_chat_by_contact", {"sender_phone_number": "19999999999"}],
    ["get_message_context", {"message_id": "STUB1", "before": 1, "after": 1}],
]


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> None:
    row = {}
    for _, name, col_type, *_ in conn.execute(f"PRAGMA table_info({table})"):
        kind = (col_type or "").upper()
        default = 0 if ("INT" in kind or "BOOL" in kind) else None
        row[name] = values.get(name, default)
    placeholders = ",".join("?" * len(row))
    conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({placeholders})", list(row.values()))


def _build_messages_db(path: Path) -> None:
    source = BRIDGE_MAIN.read_text()
    conn = sqlite3.connect(path)
    for stmt in re.findall(r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+IF\s+NOT\s+EXISTS[\s\S]*?\)\s*;", source):
        with contextlib.suppress(sqlite3.Error):
            conn.execute(stmt)
    for stmt in set(re.findall(r"ALTER TABLE [a-z_]+ ADD COLUMN [a-z_]+ [A-Z]+(?: [A-Z]+)*", source)):
        with contextlib.suppress(sqlite3.Error):
            conn.execute(stmt)
    chat_columns = {row[1] for row in conn.execute("PRAGMA table_info(chats)")}
    assert {"jid", "name", "last_message_time", "push_name"} <= chat_columns, chat_columns
    _insert(conn, "chats", {"jid": JID, "name": "Stub Chat",
                            "last_message_time": "2026-09-14 10:00:00", "push_name": "Stubby"})
    _insert(conn, "messages", {"id": "STUB1", "chat_jid": JID, "sender": "10000000000",
                               "content": "hello stub", "timestamp": "2026-09-14 10:00:00"})
    conn.commit()
    conn.close()


def test_tools_return_what_their_output_schema_declares(tmp_path):
    _build_messages_db(tmp_path / "messages.db")
    sqlite3.connect(tmp_path / "whatsapp.db").close()
    env = {
        **os.environ,
        "WHATSAPP_MCP_TOKEN": "test-token",
        "MESSAGES_DB_PATH": str(tmp_path / "messages.db"),
        "WHATSMEOW_DB_PATH": str(tmp_path / "whatsapp.db"),
        "WHATSAPP_MCP_SHORT_DB": str(tmp_path / "short.db"),
        "WHATSAPP_MCP_SCHEDULE_DB": str(tmp_path / "schedule.db"),
        "WHATSAPP_API_BASE_URL": "http://127.0.0.1:9/api",
    }
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, json.dumps(CASES)],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    results = json.loads(proc.stdout.strip().splitlines()[-1])
    by_case = dict(zip([f"{n}:{json.dumps(a, sort_keys=True)}" for n, a in CASES], results, strict=True))

    def outcome(name, args):
        return by_case[f"{name}:{json.dumps(args, sort_keys=True)}"]

    messages = outcome(*CASES[0])
    assert messages["outcome"] == "ok", messages
    assert "hello stub" in messages["structured"]["result"]

    found = outcome(*CASES[1])
    assert found["outcome"] == "ok", found
    assert found["structured"]["jid"] == JID

    missing = outcome(*CASES[2])
    assert missing["outcome"] == "tool_error", missing
    assert MISSING_JID in missing["message"]

    direct = outcome(*CASES[3])
    assert direct["outcome"] == "ok", direct
    assert direct["structured"]["jid"] == JID

    direct_missing = outcome(*CASES[4])
    assert direct_missing["outcome"] == "tool_error", direct_missing

    context = outcome(*CASES[5])
    assert context["outcome"] == "ok", context
    assert context["structured"]["message"]["content"] == "hello stub"
