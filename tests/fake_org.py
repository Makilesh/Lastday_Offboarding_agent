"""A small fake organization with one departing member and the dependencies they leave."""

import base64

from lastday.scope import Scope
from tests.fake_github import FakeGitHub

ORG = "acme"
MEMBER = "leaver"
OPERATOR = "operator"
REPO = "app"
CODEOWNERS = ".github/CODEOWNERS"

PLAN_STEPS = [
    {"type": "remove_from_team", "team": "core"},
    {"type": "hand_over_file", "repo": REPO, "path": CODEOWNERS, "successor": f"@{ORG}/core"},
    {"type": "downgrade_collaborator", "repo": REPO},
    {"type": "grant_team_write", "repo": REPO, "team": "core"},
]


def fake_org(handed_over: bool = False) -> FakeGitHub:
    """`handed_over=False`: the member still owns /src/, is a direct admin and in team core.
    `handed_over=True`: the state after every reversible step has run."""
    owner = f"@{ORG}/core" if handed_over else f"@{MEMBER}"
    codeowners = f"* @{OPERATOR}\n/src/ {owner}\n"
    fake = (
        FakeGitHub()
        .on("GET", "/user", {"login": OPERATOR})
        .on("GET", f"/orgs/{ORG}/repos", [{"name": REPO, "default_branch": "main", "topics": ["lastday-demo"]}])
        .on("GET", f"/orgs/{ORG}/memberships/{MEMBER}", {"state": "active", "role": "member"})
        .on("GET", f"/orgs/{ORG}/memberships/{OPERATOR}", {"state": "active", "role": "admin"})
        .on("GET", f"/repos/{ORG}/{REPO}", {"default_branch": "main"})
        .on("GET", f"/repos/{ORG}/{REPO}/git/trees/main", {"tree": [{"path": CODEOWNERS, "type": "blob", "size": 40}]})
        .on(
            "GET",
            f"/repos/{ORG}/{REPO}/contents/{CODEOWNERS}",
            {"content": base64.b64encode(codeowners.encode()).decode(), "sha": "abc"},
        )
        .on(
            "GET",
            f"/repos/{ORG}/{REPO}/collaborators",
            [{"login": MEMBER, "role_name": "read" if handed_over else "admin"}],
        )
        .on(
            "GET",
            f"/repos/{ORG}/{REPO}/pulls",
            [{"number": 7, "user": {"login": MEMBER}, "title": "Half-done feature", "html_url": "https://pr/7"}],
        )
        .on("GET", f"/repos/{ORG}/{REPO}/teams", [{"slug": "core", "permission": "push" if handed_over else "pull"}])
        .on("GET", f"/orgs/{ORG}/teams", [{"slug": "core"}])
        .on("GET", f"/orgs/{ORG}/teams/core", {"slug": "core", "privacy": "closed"})
        .on("GET", f"/orgs/{ORG}/teams/core/members", [{"login": MEMBER}, {"login": OPERATOR}])
    )
    if not handed_over:
        fake.on("GET", f"/orgs/{ORG}/teams/core/memberships/{MEMBER}", {"state": "active", "role": "maintainer"})
    return fake


def scope_for(fake: FakeGitHub) -> Scope:
    return Scope(fake.client(), ORG)
