"""Typed results of the read tools. They give each tool an output schema, so the agent knows
the exact shape before it writes a Code Mode script against them."""

from pydantic import BaseModel, Field


class ScopedRepo(BaseModel):
    name: str
    default_branch: str


class ScopedRepos(BaseModel):
    org: str
    topic: str = Field(description="Only repositories with this topic are in scope.")
    repos: list[ScopedRepo]


class ConfigFile(BaseModel):
    path: str
    text: str


class BranchProtection(BaseModel):
    requires_code_owner_review: bool
    required_approvals: int
    admins_enforced: bool


class Collaborator(BaseModel):
    login: str
    role: str = Field(description="admin, maintain, write, triage or read")


class TeamAccess(BaseModel):
    team: str
    permission: str


class PullRequest(BaseModel):
    number: int
    author: str
    title: str
    url: str


class RepoSnapshot(BaseModel):
    repo: str
    default_branch: str
    active_codeowners_path: str | None = Field(description="The CODEOWNERS file GitHub uses, if any.")
    files: list[ConfigFile] = Field(
        description="Text of every CODEOWNERS file and every file under .github/ on the default branch."
    )
    branch_protection: BranchProtection | None
    direct_collaborators: list[Collaborator]
    teams: list[TeamAccess]
    open_pull_requests: list[PullRequest]


class TeamRole(BaseModel):
    team: str
    role: str = Field(description="maintainer or member")


class RepoAccess(BaseModel):
    repo: str
    permission: str = Field(description="Effective permission from any source, or none.")
    direct_role: str | None = Field(description="Role granted to the member directly, if any.")


class MemberPullRequest(PullRequest):
    repo: str


class MemberAccess(BaseModel):
    member: str
    org_role: str | None = Field(description="admin (owner) or member; null when not in the org.")
    org_state: str
    teams: list[TeamRole]
    repos: list[RepoAccess]
    open_pull_requests: list[MemberPullRequest]
