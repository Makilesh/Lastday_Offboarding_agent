"""Read-only views of GitHub state, limited to in-scope repositories."""

from lastday.github import GitHub
from lastday.scope import Scope

CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
WORKFLOWS_DIR = ".github/workflows"


def member_access(gh: GitHub, scope: Scope, member: str) -> dict:
    """Everything that gives `member` access: org role, teams, and per-repo permissions."""
    membership = gh.get_or_none(f"/orgs/{scope.org}/memberships/{member}")
    teams = []
    for team in gh.paginate(f"/orgs/{scope.org}/teams"):
        team_membership = gh.get_or_none(f"/orgs/{scope.org}/teams/{team['slug']}/memberships/{member}")
        if team_membership and team_membership["state"] == "active":
            teams.append({"team": team["slug"], "role": team_membership["role"]})

    repos = []
    open_pull_requests = []
    for repo in scope.repos():
        full_repo = f"{scope.org}/{repo['name']}"
        direct = _direct_collaborators(gh, full_repo)
        permission = gh.get_or_none(f"/repos/{full_repo}/collaborators/{member}/permission")
        repos.append(
            {
                "repo": repo["name"],
                "permission": permission["role_name"] if permission else "none",
                "direct_role": direct.get(member),
            }
        )
        open_pull_requests += [
            {"repo": repo["name"], **pr} for pr in _open_pull_requests(gh, full_repo) if pr["author"] == member
        ]

    return {
        "member": member,
        "org_role": membership["role"] if membership else None,
        "org_state": membership["state"] if membership else "not a member",
        "teams": teams,
        "repos": repos,
        "open_pull_requests": open_pull_requests,
    }


def repo_snapshot(gh: GitHub, scope: Scope, name: str) -> dict:
    """The files and settings of one in-scope repository that can depend on a person."""
    repo = scope.require_repo(name)
    full_repo = f"{scope.org}/{name}"
    branch = repo["default_branch"]
    return {
        "repo": name,
        "default_branch": branch,
        "codeowners": _codeowners(gh, full_repo, branch),
        "workflows": _workflows(gh, full_repo, branch),
        "branch_protection": _branch_protection(gh, full_repo, branch),
        "direct_collaborators": [
            {"login": login, "role": role} for login, role in _direct_collaborators(gh, full_repo).items()
        ],
        "teams": [
            {"team": team["slug"], "permission": team["permission"]}
            for team in gh.paginate(f"/repos/{full_repo}/teams")
        ],
        "open_pull_requests": _open_pull_requests(gh, full_repo),
    }


def _codeowners(gh: GitHub, full_repo: str, branch: str) -> dict | None:
    # GitHub uses the first CODEOWNERS file it finds, in this order.
    for path in CODEOWNERS_LOCATIONS:
        file = gh.read_file(full_repo, path, ref=branch)
        if file is not None:
            return {"path": path, "text": file.text}
    return None


def _workflows(gh: GitHub, full_repo: str, branch: str) -> list[dict]:
    listing = gh.get_or_none(f"/repos/{full_repo}/contents/{WORKFLOWS_DIR}", ref=branch) or []
    workflows = []
    for entry in listing:
        if entry["type"] == "file" and entry["name"].endswith((".yml", ".yaml")):
            file = gh.read_file(full_repo, entry["path"], ref=branch)
            workflows.append({"path": entry["path"], "text": file.text})
    return workflows


def _branch_protection(gh: GitHub, full_repo: str, branch: str) -> dict | None:
    protection = gh.get_or_none(f"/repos/{full_repo}/branches/{branch}/protection")
    if protection is None:
        return None
    reviews = protection.get("required_pull_request_reviews") or {}
    return {
        "requires_code_owner_review": bool(reviews.get("require_code_owner_reviews")),
        "required_approvals": reviews.get("required_approving_review_count", 0),
        "admins_enforced": protection.get("enforce_admins", {}).get("enabled", False),
    }


def _direct_collaborators(gh: GitHub, full_repo: str) -> dict[str, str]:
    return {
        user["login"]: user["role_name"]
        for user in gh.paginate(f"/repos/{full_repo}/collaborators", affiliation="direct")
    }


def _open_pull_requests(gh: GitHub, full_repo: str) -> list[dict]:
    return [
        {"number": pr["number"], "author": pr["user"]["login"], "title": pr["title"], "url": pr["html_url"]}
        for pr in gh.paginate(f"/repos/{full_repo}/pulls", state="open")
    ]
