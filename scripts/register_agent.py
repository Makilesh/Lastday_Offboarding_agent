"""Register the Last Day MCP server and agent with a running TrueForge instance.

    uv run scripts/register_agent.py --check   # read-only: report what TrueForge has, change nothing
    uv run scripts/register_agent.py           # create or replace the MCP server and the agent

Safe to re-run. The Last Day MCP server (`uv run lastday-server`) must be running so TrueForge
can list its tools. Any response the script does not expect is printed in full (with the
bearer token redacted) so a first run against a new TrueForge is easy to debug.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import httpx

from lastday.config import ConfigError, require_env

SERVER_NAME = "lastday"
AGENT_NAME = "last-day"
INSTRUCTIONS_PATH = Path(__file__).resolve().parent.parent / "agent" / "instructions.md"
# Named explicitly: the default @destructive selector only matches annotated tools.
GATED_TOOLS = ["apply_reversible_steps", "remove_org_member"]
DESCRIPTION = "Offboards a GitHub org member: finds what depends on them, hands it over, then asks before removal."


class TrueForgeError(RuntimeError):
    pass


class TrueForge:
    def __init__(self, http: httpx.Client, secret: str):
        self.http = http
        self.secret = secret

    def request(self, method: str, path: str, body: dict | None = None, allow_404: bool = False) -> Any:
        response = self.http.request(method, path, json=body)
        if allow_404 and response.status_code == 404:
            return None
        if response.is_error:
            raise self.unexpected(method, path, response, "an error status")
        try:
            return response.json() if response.content else {}
        except ValueError:
            raise self.unexpected(method, path, response, "a body that is not JSON") from None

    def data(self, method: str, path: str, body: dict | None = None, allow_404: bool = False) -> Any:
        """The `data` field every TrueForge API response carries."""
        payload = self.request(method, path, body, allow_404)
        if payload is None:
            return None
        if not isinstance(payload, dict) or "data" not in payload:
            raise TrueForgeError(f"TrueForge {method} {path} returned JSON without a 'data' field:\n{self.redact(str(payload))}")
        return payload["data"]

    def unexpected(self, method: str, path: str, response: httpx.Response, what: str) -> TrueForgeError:
        return TrueForgeError(
            f"TrueForge {method} {path} returned {what} ({response.status_code}). Response body:\n"
            f"{self.redact(response.text)[:4000]}"
        )

    def redact(self, text: str) -> str:
        return text.replace(self.secret, "<LASTDAY_MCP_TOKEN>") if self.secret else text


def mcp_server_manifest(url: str, token: str) -> dict:
    return {
        "type": "remote",
        "name": SERVER_NAME,
        "url": url,
        "description": "Last Day: scoped GitHub tools to offboard one org member.",
        "auth": {"type": "header", "headers": {"Authorization": f"Bearer {token}"}},
    }


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


def local_server_status(mcp_url: str) -> tuple[bool, str]:
    """GET the MCP endpoint without the token: a running Last Day server answers 401."""
    try:
        status = httpx.get(mcp_url, timeout=5).status_code
    except httpx.HTTPError as error:
        return False, f"Last Day MCP server is not reachable at {mcp_url} ({error.__class__.__name__}); run `uv run lastday-server`"
    if status == 401:
        return True, f"Last Day MCP server is up at {mcp_url} and rejects requests without the token"
    return False, f"{mcp_url} answered {status} without a token; expected 401 from the Last Day MCP server"


def find_agent(tf: TrueForge) -> dict | None:
    agents = tf.data("GET", "/api/v1/agents")
    return next((agent for agent in agents if agent.get("name") == AGENT_NAME), None)


def registered_tools(tf: TrueForge) -> list[str]:
    return sorted(tool["name"] for tool in tf.data("GET", f"/api/v1/mcp-servers/{SERVER_NAME}/tools"))


def check(tf: TrueForge, model: str, mcp_url: str) -> bool:
    """Report TrueForge's state using GET requests only."""
    ok = True

    def report(status: str, message: str) -> None:
        nonlocal ok
        ok = ok and status != "fail"
        print(f"  {status:<5} {message}")

    tf.data("GET", "/api/v1/capabilities")
    report("ok", f"TrueForge is reachable at {tf.http.base_url}")

    models = [m["name"] for m in tf.data("GET", "/api/v1/models")]
    if not model:
        report("todo", f"LASTDAY_MODEL is empty; configured models: {', '.join(models) or 'none'}")
    elif model in models:
        report("ok", f"model {model} is configured")
    else:
        report("fail", f"model {model} is not configured; configured models: {', '.join(models) or 'none'}")

    sandbox = tf.data("GET", "/api/v1/settings/sandbox-providers", allow_404=True)
    if sandbox is None:
        report("todo", "no sandbox provider configured (Settings -> Sandbox providers); Code Mode needs one")
    else:
        reason = f" ({sandbox['status_reason']})" if sandbox.get("status_reason") else ""
        report("ok" if sandbox.get("status") == "ready" else "todo", f"sandbox provider status: {sandbox.get('status')}{reason}")

    running, message = local_server_status(mcp_url)
    report("ok" if running else "fail", message)

    server = tf.data("GET", f"/api/v1/settings/mcp-servers/{SERVER_NAME}", allow_404=True)
    if server is None:
        report("todo", f"MCP server '{SERVER_NAME}' is not registered yet")
    else:
        report("ok", f"MCP server '{SERVER_NAME}' is registered at {server['manifest'].get('url')}")
        if running:
            tools = registered_tools(tf)
            missing = [tool for tool in GATED_TOOLS if tool not in tools]
            report("fail" if missing else "ok", f"TrueForge lists tools: {', '.join(tools) or 'none'}")

    agent = find_agent(tf)
    if agent is None:
        report("todo", f"agent '{AGENT_NAME}' does not exist yet")
    else:
        attached = next((s for s in agent["manifest"].get("mcp_servers", []) if s["name"] == SERVER_NAME), {})
        gated = attached.get("require_approval_for_tools", [])
        report("ok" if set(GATED_TOOLS) <= set(gated) else "fail", f"agent '{AGENT_NAME}' exists; approval required for: {', '.join(gated) or 'nothing'}")
    return ok


def register(tf: TrueForge, model: str, mcp_url: str, token: str) -> None:
    running, message = local_server_status(mcp_url)
    if not running:
        raise TrueForgeError(message)

    tf.data("PUT", "/api/v1/settings/mcp-servers", {"manifest": mcp_server_manifest(mcp_url, token)})
    tools = registered_tools(tf)
    missing = [tool for tool in GATED_TOOLS if tool not in tools]
    if missing:
        raise TrueForgeError(f"TrueForge does not list the gated tools {missing}; it lists: {tools}")
    print(f"MCP server '{SERVER_NAME}' registered at {mcp_url}; tools: {', '.join(tools)}")

    manifest = agent_manifest(model)
    existing = find_agent(tf)
    if existing:
        tf.data("PUT", f"/api/v1/agents/{existing['id']}", {"description": DESCRIPTION, "manifest": manifest})
    else:
        tf.data("POST", "/api/v1/agents", {"name": AGENT_NAME, "description": DESCRIPTION, "manifest": manifest})

    saved = find_agent(tf)
    attached = next((s for s in (saved or {}).get("manifest", {}).get("mcp_servers", []) if s["name"] == SERVER_NAME), {})
    sandbox_on = (saved or {}).get("manifest", {}).get("config", {}).get("sandbox", {}).get("enabled")
    if not saved or not set(GATED_TOOLS) <= set(attached.get("require_approval_for_tools", [])) or not sandbox_on:
        raise TrueForgeError(f"the saved agent does not match what was sent:\n{tf.redact(str(saved))}")
    print(f"Agent '{AGENT_NAME}' {'updated' if existing else 'created'} with model {model}; "
          f"sandbox on; approval required for {', '.join(GATED_TOOLS)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only read TrueForge's state (GET requests), change nothing")
    args = parser.parse_args()

    try:
        token = require_env("LASTDAY_MCP_TOKEN")
        model = os.environ.get("LASTDAY_MODEL", "").strip() if args.check else require_env("LASTDAY_MODEL")
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    base_url = os.environ.get("TRUEFORGE_URL", "http://localhost:8790")
    mcp_url = os.environ.get("LASTDAY_MCP_URL", "http://127.0.0.1:8765/mcp")

    try:
        with httpx.Client(base_url=base_url, timeout=60) as http:
            tf = TrueForge(http, token)
            if args.check:
                print(f"Checking TrueForge at {base_url} (read-only):")
                return 0 if check(tf, model, mcp_url) else 1
            register(tf, model, mcp_url, token)
    except httpx.ConnectError:
        print(f"error: cannot reach TrueForge at {base_url}. Is it running?", file=sys.stderr)
        return 1
    except TrueForgeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
