"""Register the Last Day MCP server and agent with a running TrueForge instance.

    uv run scripts/register_agent.py

Safe to re-run: the MCP server and the agent are created, or replaced if they exist.
The Last Day MCP server (`uv run lastday-server`) must be running so TrueForge can list its tools.
"""

import os
import sys
from pathlib import Path

import httpx

from lastday.config import ConfigError, require_env

SERVER_NAME = "lastday"
AGENT_NAME = "last-day"
INSTRUCTIONS_PATH = Path(__file__).resolve().parent.parent / "agent" / "instructions.md"
# Named explicitly: the default @destructive selector only matches annotated tools.
GATED_TOOLS = ["apply_reversible_steps", "remove_org_member"]


class TrueForgeError(RuntimeError):
    pass


def call(http: httpx.Client, method: str, path: str, body: dict | None = None) -> dict:
    response = http.request(method, path, json=body)
    if response.is_error:
        raise TrueForgeError(f"TrueForge {method} {path} failed with {response.status_code}: {response.text}")
    return response.json() if response.content else {}


def register_mcp_server(http: httpx.Client, url: str, token: str) -> list[str]:
    call(
        http,
        "PUT",
        "/api/v1/settings/mcp-servers",
        {
            "manifest": {
                "type": "remote",
                "name": SERVER_NAME,
                "url": url,
                "description": "Last Day: scoped GitHub tools to offboard one org member.",
                "auth": {"type": "header", "headers": {"Authorization": f"Bearer {token}"}},
            }
        },
    )
    tools = call(http, "GET", f"/api/v1/mcp-servers/{SERVER_NAME}/tools")
    return sorted(tool["name"] for tool in tools.get("data", []))


def agent_manifest(model: str) -> dict:
    return {
        "model": {"name": model},
        "instructions": INSTRUCTIONS_PATH.read_text(encoding="utf-8"),
        "mcp_servers": [
            {
                "name": SERVER_NAME,
                "enable_tools": ["@all"],
                "preload": True,
                "require_approval_for_tools": GATED_TOOLS,
            }
        ],
        "config": {
            "sandbox": {"enabled": True},
            "dynamic_sub_agents": {"enabled": False},
            "iteration_limit": 60,
        },
    }


def upsert_agent(http: httpx.Client, model: str) -> str:
    description = "Offboards a GitHub org member: finds what depends on them, hands it over, then asks before removal."
    manifest = agent_manifest(model)
    existing = [agent for agent in call(http, "GET", "/api/v1/agents")["data"] if agent["name"] == AGENT_NAME]
    if existing:
        call(http, "PUT", f"/api/v1/agents/{existing[0]['id']}", {"description": description, "manifest": manifest})
        return "updated"
    call(http, "POST", "/api/v1/agents", {"name": AGENT_NAME, "description": description, "manifest": manifest})
    return "created"


def main() -> int:
    try:
        token = require_env("LASTDAY_MCP_TOKEN")
        model = require_env("LASTDAY_MODEL")
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    base_url = os.environ.get("TRUEFORGE_URL", "http://localhost:8790")
    mcp_url = os.environ.get("LASTDAY_MCP_URL", "http://127.0.0.1:8765/mcp")

    try:
        with httpx.Client(base_url=base_url, timeout=60) as http:
            tools = register_mcp_server(http, mcp_url, token)
            print(f"MCP server '{SERVER_NAME}' registered at {mcp_url}; tools: {', '.join(tools) or 'none'}")
            print(f"Agent '{AGENT_NAME}' {upsert_agent(http, model)} with model {model}")
    except httpx.ConnectError:
        print(f"error: cannot reach TrueForge at {base_url}. Is it running?", file=sys.stderr)
        return 1
    except TrueForgeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
