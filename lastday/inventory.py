"""Read-only views of GitHub state, limited to in-scope repositories."""

from lastday.github import GitHub
from lastday.scope import Scope

# GitHub uses the first of these that exists.
CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")
MAX_FILE_BYTES = 256_000


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
    files = config_files(gh, full_repo, branch)
    return {
        "repo": name,
        "default_branch": branch,
        "active_codeowners_path": next((f["path"] for f in files if f["path"] in CODEOWNERS_LOCATIONS), None),
        "files": files,
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


def config_files(gh: GitHub, full_repo: str, branch: str) -> list[dict]:
    """Files that assign ownership or run automation: every CODEOWNERS location and everything
    under .github/. The agent's scan and the server's own re-scan both read exactly these."""
    tree = gh.get(f"/repos/{full_repo}/git/trees/{branch}", recursive="1")["tree"]
    paths = [
        entry["path"]
        for entry in tree
        if entry["type"] == "blob"
        and (entry["path"] in CODEOWNERS_LOCATIONS or entry["path"].startswith(".github/"))
        and entry.get("size", 0) <= MAX_FILE_BYTES
    ]
    # CODEOWNERS locations first, in the order GitHub looks for them.
    paths.sort(key=lambda p: (CODEOWNERS_LOCATIONS.index(p) if p in CODEOWNERS_LOCATIONS else len(CODEOWNERS_LOCATIONS), p))
    files = []
    for path in paths:
        try:
            file = gh.read_file(full_repo, path, ref=branch)
        except UnicodeDecodeError:
            continue  # binary files (images and the like) cannot name anyone
        if file is not None:
            files.append({"path": path, "text": file.text})
    return files


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
