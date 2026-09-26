"""Run Code Mode scripts locally, the way TrueForge runs them in its sandbox.

In TrueForge, `from mcp_client import call_tool` inside a sandbox script is bridged back to
the harness, which calls the MCP server with the stored credentials. `install` registers a
module named `mcp_client` that does the same over a connection you provide, so the scripts
the agent writes can be replayed and tested without TrueForge or a sandbox.
"""

import sys
import types
from collections.abc import Callable
from typing import Any

from mcp import Client


class ToolCallError(RuntimeError):
    pass


def install(connect: Callable[[], Client], server_name: str = "lastday") -> None:
    """Make `from mcp_client import call_tool` resolve to tools on the connected server."""

    # `body` is required, as it is in the sandbox client.
    async def call_tool(server: str, tool: str, body: dict[str, Any]) -> Any:
        if server != server_name:
            raise ToolCallError(f"unknown MCP server {server!r}; only {server_name!r} is connected")
        async with connect() as client:
            result = await client.call_tool(tool, body)
        if result.is_error:
            # Same type (a RuntimeError) and message format as TrueForge's sandbox client.
            text = "; ".join(getattr(part, "text", "") for part in result.content)
            raise ToolCallError(f"MCP tool error (server={server}, tool={tool}): {text}")
        return result.structured_content

    module = types.ModuleType("mcp_client")
    module.call_tool = call_tool
    sys.modules["mcp_client"] = module
