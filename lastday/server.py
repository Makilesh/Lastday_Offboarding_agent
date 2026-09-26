"""Last Day's MCP server: the GitHub tools the agent uses to offboard one org member.

No model runs here. The server only exposes read tools and, later, plan-bound actions,
and enforces its own rules whatever the caller asks for.

    uv run lastday-server
"""

import functools
import hmac
import os
from collections.abc import Callable
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope as AsgiScope, Send

from lastday import inventory
from lastday.config import SCOPE_TOPIC, require_env
from lastday.errors import LastDayError
from lastday.github import GitHub
from lastday.scope import Scope

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

INSTRUCTIONS = f"""\
Tools for offboarding one member of a GitHub organization. Only repositories tagged
{SCOPE_TOPIC} are visible. Read tools never change anything."""


def honest_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Pass Last Day's own failure messages to the caller instead of a generic error."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except LastDayError as error:
            raise ToolError(str(error)) from error

    return wrapper


def build_server(gh: GitHub, scope: Scope) -> MCPServer:
    server = MCPServer(name="lastday", instructions=INSTRUCTIONS)

    @server.tool(annotations=READ_ONLY)
    @honest_errors
    def list_scoped_repos() -> dict:
        """List the repositories Last Day may read or change (tagged with the scope topic)."""
        return {"org": scope.org, "topic": SCOPE_TOPIC, "repos": scope.repos()}

    @server.tool(annotations=READ_ONLY)
    @honest_errors
    def get_member_access(member: str) -> dict:
        """Show how a member can reach the org: org role, team memberships, their permission and
        direct role on each in-scope repository, and their open pull requests."""
        return inventory.member_access(gh, scope, member)

    @server.tool(annotations=READ_ONLY)
    @honest_errors
    def get_repo_snapshot(repo: str) -> dict:
        """Return one in-scope repository as of its default branch: the text of every CODEOWNERS
        file and every file under .github/ (`files`, with the CODEOWNERS file GitHub uses named in
        `active_codeowners_path`), branch protection, direct collaborators, team access and open
        pull requests. `repo` is the repository name without the org."""
        return inventory.repo_snapshot(gh, scope, repo)

    return server


class BearerAuth:
    """Rejects any HTTP request that does not carry the shared bearer token."""

    def __init__(self, app: ASGIApp, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: AsgiScope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                await JSONResponse({"error": "missing or wrong bearer token"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main() -> None:
    org = require_env("GITHUB_ORG")
    gh = GitHub(require_env("GITHUB_TOKEN"))
    server = build_server(gh, Scope(gh, org))
    host = os.environ.get("LASTDAY_HOST", "127.0.0.1")
    port = int(os.environ.get("LASTDAY_PORT", "8765"))
    app = BearerAuth(server.streamable_http_app(host=host), require_env("LASTDAY_MCP_TOKEN"))
    uvicorn.run(app, host=host, port=port)
