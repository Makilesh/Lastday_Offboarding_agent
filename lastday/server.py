"""Last Day's MCP server: the GitHub tools the agent uses to offboard one org member.

No model runs here. The server only exposes read tools and, later, plan-bound actions,
and enforces its own rules whatever the caller asks for.

    uv run lastday-server
"""

import functools
import hmac
import os
from collections.abc import Callable
from typing import Any, Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope as AsgiScope, Send

from lastday import actions, inventory
from lastday.config import SCOPE_TOPIC, require_env
from lastday.errors import LastDayError
from lastday.github import GitHub
from lastday.schemas import MemberAccess, RepoSnapshot, ScopedRepos
from lastday.scope import Scope
from lastday.store import PlanStore

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

INSTRUCTIONS = f"""\
Tools for offboarding one member of a GitHub organization. Only repositories tagged
{SCOPE_TOPIC} are visible. Read tools never change anything. Changes only happen through a
stored plan: propose_plan, then apply_reversible_steps, then remove_org_member."""


def honest_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Pass Last Day's own failure messages to the caller instead of a generic error."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except LastDayError as error:
            raise ToolError(str(error)) from error

    return wrapper


def build_server(gh: GitHub, scope: Scope, store: PlanStore) -> MCPServer:
    server = MCPServer(name="lastday", instructions=INSTRUCTIONS)

    @server.tool(annotations=READ_ONLY)
    @honest_errors
    def list_scoped_repos() -> ScopedRepos:
        """List the repositories Last Day may read or change (tagged with the scope topic)."""
        return ScopedRepos(org=scope.org, topic=SCOPE_TOPIC, repos=scope.repos())

    @server.tool(annotations=READ_ONLY)
    @honest_errors
    def get_member_access(member: str) -> MemberAccess:
        """Show how a member can reach the org: org role, team memberships, their permission and
        direct role on each in-scope repository, and their open pull requests."""
        return MemberAccess.model_validate(inventory.member_access(gh, scope, member))

    @server.tool(annotations=READ_ONLY)
    @honest_errors
    def get_repo_snapshot(repo: str) -> RepoSnapshot:
        """Return one in-scope repository as of its default branch: the text of every CODEOWNERS
        file and every file under .github/ (`files`, with the CODEOWNERS file GitHub uses named in
        `active_codeowners_path`), branch protection, direct collaborators, team access and open
        pull requests. `repo` is the repository name without the org."""
        return RepoSnapshot.model_validate(inventory.repo_snapshot(gh, scope, repo))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
    @honest_errors
    def propose_plan(member: str, steps: list[Step]) -> dict:
        """Check and store an offboarding plan; changes nothing on GitHub. Step types, run in this order:
        grant_team_write {repo, team}: give a successor team write access so GitHub honours it as a code owner.
        hand_over_file {repo, path, successor}: replace the member in a file via a merged pull request;
          in CODEOWNERS the successor is @user or @org/team, elsewhere a plain login.
        downgrade_collaborator {repo}: set the member's direct access to read.
        remove_from_team {team}: take the member out of a team.
        The server refuses plans that leave a blocking dependency unhandled or add unneeded steps.
        Returns the plan_hash to pass, with the same steps, to apply_reversible_steps."""
        return actions.propose_plan(gh, scope, store, member, _dump(steps))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
    @honest_errors
    def apply_reversible_steps(plan_hash: str, steps: list[Step]) -> dict:
        """Run the reversible steps of a stored plan, in order, after a human approves them.
        `steps` must be exactly the stored plan's steps. Steps already done are detected and
        skipped; the run stops at the first failure and reports what did and did not run."""
        return actions.apply_reversible_steps(gh, scope, store, plan_hash, _dump(steps))

    @server.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True))
    @honest_errors
    def remove_org_member(member: str, plan_hash: str = "") -> dict:
        """Remove the member from the organization. Irreversible. Refused, with the list of what
        still depends on the member, unless every step of their approved plan is done and the
        server's own re-scan finds nothing blocking. Owners are never removed."""
        dry_run = os.environ.get("LASTDAY_DRY_RUN", "").lower() in ("1", "true", "yes")
        return actions.remove_org_member(gh, scope, store, member, plan_hash, dry_run)

    return server


class Step(BaseModel):
    """One plan step; which fields apply depends on `type` (see propose_plan)."""

    type: Literal["grant_team_write", "hand_over_file", "downgrade_collaborator", "remove_from_team"]
    repo: str | None = None
    path: str | None = None
    successor: str | None = None
    team: str | None = None


def _dump(steps: list[Step]) -> list[dict]:
    return [step.model_dump(exclude_none=True) for step in steps]


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
    server = build_server(gh, Scope(gh, org), PlanStore.from_env())
    host = os.environ.get("LASTDAY_HOST", "127.0.0.1")
    port = int(os.environ.get("LASTDAY_PORT", "8765"))
    app = BearerAuth(server.streamable_http_app(host=host), require_env("LASTDAY_MCP_TOKEN"))
    uvicorn.run(app, host=host, port=port)
