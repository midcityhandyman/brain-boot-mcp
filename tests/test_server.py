"""The MCP layer, exercised through a real MCP client.

Most tests connect the SDK client to the server in process. The last one
launches `python -m brain_boot_mcp serve` as a subprocess and talks to it over
stdio, which is how an agent host would run it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

from brain_boot_mcp.__main__ import main as cli_main
from brain_boot_mcp.server import build_server
from brain_boot_mcp.service import BrainService

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _call(service: BrainService, name: str, args: dict[str, Any]):
    async def go():
        async with Client(build_server(service)) as client:
            return await client.call_tool(name, args)

    return asyncio.run(go())


def test_server_lists_exactly_the_four_tools(seeded: BrainService):
    async def go():
        async with Client(build_server(seeded)) as client:
            return await client.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(go()).tools}
    assert sorted(tools) == ["boot", "pack_check", "propose_rule", "recall"]
    assert tools["boot"].input_schema["required"] == ["agent_key"]
    assert "approve" not in " ".join(tools), "human side actions must not be tools"
    for tool in tools.values():
        assert tool.description


def test_boot_tool_returns_structured_pack(seeded: BrainService):
    result = _call(seeded, "boot", {"agent_key": "ops_agent"})
    assert not result.is_error
    pack = result.structured_content
    assert pack["pack_id"].startswith("pk_")
    assert pack["counts"]["standing_rules"] == 4
    assert result.content and "standing_rules" in result.content[0].text


def test_full_session_over_one_connection(seeded: BrainService):
    async def go():
        async with Client(build_server(seeded)) as client:
            pack = (await client.call_tool("boot", {"agent_key": "ops_agent"})).structured_content
            same = await client.call_tool(
                "pack_check", {"pack_id": pack["pack_id"], "agent_key": "ops_agent"}
            )
            seeded.add_correction("Mid session correction.")
            changed = await client.call_tool(
                "pack_check", {"pack_id": pack["pack_id"], "agent_key": "ops_agent"}
            )
            found = await client.call_tool("recall", {"query": "gutter metre", "limit": 3})
            proposed = await client.call_tool(
                "propose_rule",
                {"agent_key": "ops_agent", "text": "Confirm the slot twice.", "rationale": "x"},
            )
            return same, changed, found, proposed

    same, changed, found, proposed = asyncio.run(go())
    assert same.structured_content["status"] == "UNCHANGED"
    assert changed.structured_content["status"] == "CHANGED"
    assert changed.structured_content["added"][0]["text"] == "Mid session correction."
    assert found.structured_content["hits"][0]["kind"] == "memory"
    assert proposed.structured_content["status"] == "pending"


def test_bad_input_comes_back_as_a_tool_error_not_a_crash(seeded: BrainService):
    result = _call(seeded, "pack_check", {"pack_id": "garbage", "agent_key": "ops_agent"})
    assert result.is_error
    assert "pk_" in result.content[0].text

    result = _call(seeded, "boot", {"agent_key": "not a key"})
    assert result.is_error

    result = _call(seeded, "recall", {"query": "invoice", "kinds": ["secrets"]})
    assert result.is_error


def test_stdio_subprocess_round_trip(tmp_path):
    db = str(tmp_path / "brain.db")
    assert cli_main(["--db", db, "seed"]) == 0

    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "brain_boot_mcp", "--db", db, "serve"],
        env=env,
    )

    async def go():
        async with Client(params) as client:
            tools = await client.list_tools()
            pack = await client.call_tool("boot", {"agent_key": "ops_agent"})
            check = await client.call_tool(
                "pack_check",
                {"pack_id": pack.structured_content["pack_id"], "agent_key": "ops_agent"},
            )
            return tools, pack, check

    tools, pack, check = asyncio.run(asyncio.wait_for(go(), timeout=60))
    assert len(tools.tools) == 4
    assert pack.structured_content["counts"]["standing_rules"] == 4
    assert check.structured_content["status"] == "UNCHANGED"
