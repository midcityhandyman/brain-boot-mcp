"""The MCP surface: four tools over a BrainService.

Written against the v2 MCP Python SDK (MCPServer, the successor of FastMCP).
Tools return plain dicts with a dict[str, Any] annotation, which the SDK sends
as both text content and structured_content. Bad input raises ToolError, which
the SDK reports to the client as a tool result with is_error set.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from . import __version__
from .service import BrainError, BrainService
from .store import StoreError

SERVER_NAME = "brain-boot"

SERVER_INSTRUCTIONS = (
    "Shared memory for agents. Call boot once at the start of every session and "
    "follow the pack it returns. Call pack_check with your pack_id before any action "
    "that is hard to undo. Use recall to look things up instead of guessing. "
    "Use propose_rule when a human corrects you in a way that should outlive this session."
)


def build_server(service: BrainService) -> MCPServer:
    """Wire a service into a new MCPServer. One server per service instance."""
    mcp = MCPServer(SERVER_NAME, instructions=SERVER_INSTRUCTIONS, version=__version__)

    def guarded(call: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return call(*args, **kwargs)
        except (BrainError, StoreError) as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool()
    def boot(
        agent_key: Annotated[
            str, Field(description="Stable name of the calling agent, for example ops_agent.")
        ],
    ) -> dict[str, Any]:
        """Get your boot pack: standing rules, recent human corrections, due items and a pack_id.

        Call this once at session start, before any other work. A receipt is
        written so that pack_check can later tell you what changed.
        """
        return guarded(service.boot, agent_key)

    @mcp.tool()
    def pack_check(
        pack_id: Annotated[str, Field(description="The pack_id from boot or a previous check.")],
        agent_key: Annotated[str, Field(description="The same agent_key you booted with.")],
    ) -> dict[str, Any]:
        """Ask whether your pack is still current.

        status is UNCHANGED, CHANGED (added and removed list the lines to
        follow and to drop) or UNKNOWN_PACK (call boot again).
        """
        return guarded(service.pack_check, pack_id, agent_key)

    @mcp.tool()
    def recall(
        query: Annotated[str, Field(description="Words to look for. Any word may match.")],
        limit: Annotated[int, Field(description="Maximum hits, 1 to 25.", ge=1, le=25)] = 5,
        kinds: Annotated[
            list[str] | None,
            Field(description="Restrict to any of: memory, correction, rule."),
        ] = None,
    ) -> dict[str, Any]:
        """Search stored memories, past corrections and active rules, best match first."""
        return guarded(service.recall, query, limit, kinds)

    @mcp.tool()
    def propose_rule(
        agent_key: Annotated[str, Field(description="Who is proposing.")],
        text: Annotated[str, Field(description="The rule, one imperative sentence.")],
        rationale: Annotated[
            str, Field(description="What happened that makes this rule worth keeping.")
        ] = "",
        scope: Annotated[
            str, Field(description="An agent_key the rule should apply to, or * for all.")
        ] = "*",
    ) -> dict[str, Any]:
        """Propose a new standing rule. A human must approve it before it enters any pack."""
        return guarded(service.propose_rule, agent_key, text, rationale, scope)

    return mcp
