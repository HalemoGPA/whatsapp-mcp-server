"""server.py must import and register its tools and prompts.

The rest of this suite tests helpers below the FastMCP layer, so a FastMCP
upgrade that breaks startup (a removed import, a changed decorator) would pass
every other test. This boots server.py in a subprocess with throwaway database
paths and lists what it registered through FastMCP's in-memory client.

Offline: no bridge, no network. Skipped when fastmcp is not installed, so a bare
`pytest` without the server's dependencies still runs the rest of the suite.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

SERVER_DIR = Path(__file__).resolve().parents[2] / "whatsapp-mcp-server"

PROBE = """
import asyncio, json
import server
from fastmcp import Client

async def main():
    async with Client(server.mcp) as client:
        tools = await client.list_tools()
        prompts = await client.list_prompts()
    print(json.dumps({"tools": sorted(t.name for t in tools), "prompts": len(prompts)}))

asyncio.run(main())
"""


def test_server_boots_and_registers_tools(tmp_path):
    for name in ("messages.db", "whatsapp.db"):
        sqlite3.connect(tmp_path / name).close()
    env = {
        **os.environ,
        "WHATSAPP_MCP_TOKEN": "test-token",
        "MESSAGES_DB_PATH": str(tmp_path / "messages.db"),
        "WHATSMEOW_DB_PATH": str(tmp_path / "whatsapp.db"),
        "WHATSAPP_MCP_SHORT_DB": str(tmp_path / "short.db"),
        "WHATSAPP_MCP_SCHEDULE_DB": str(tmp_path / "schedule.db"),
        # Nothing listens here; tool registration must not need the bridge.
        "WHATSAPP_API_BASE_URL": "http://127.0.0.1:9/api",
    }
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=SERVER_DIR, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert "bridge_health" in result["tools"], result["tools"]
    assert "send_message" in result["tools"], result["tools"]
    assert result["prompts"] > 0
