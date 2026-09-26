"""The server's own scan for everything that still depends on a member.

Plans are checked against this scan and removal is refused while it finds anything
blocking. It never relies on what the agent reports.
"""

import re
from dataclasses import asdict, dataclass

from lastday.github import GitHub
from lastday.inventory import CODEOWNERS_LOCATIONS, config_files
from lastday.scope import Scope

# Direct repository roles that can change settings, so they must be handed back first.
ELEVATED_ROLES = {"admin", "maintain"}


@dataclass(frozen=True)
class Dependency:
    kind: str  # codeowner | file_reference | direct_access | team_membership | open_pull_request
    repo: str | None
    where: str
    detail: str
    blocking: bool

    def describe(self) -> str:
        location = f"{self.repo}:{self.where}" if self.repo else self.where
        return f"{location}: {self.detail}"

    def to_dict(self) -> dict:
        return asdict(self)


def mentions(text: str, login: str) -> bool:
    """True when `text` names the login as a whole word, with or without a leading @."""
    pattern = rf"(?<![A-Za-z0-9-])@?{re.escape(login)}(?![A-Za-z0-9-])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def file_dependencies(repo: str, path: str, text: str, member: str) -> list[Dependency]:
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") or not mentions(stripped, member):
            continue
        if path in CODEOWNERS_LOCATIONS:
            pattern, *owners = stripped.split()
            sole = len(owners) == 1
            detail = f"{'sole owner' if sole else 'co-owner'} of {pattern}"
            found.append(Dependency("codeowner", repo, f"{path}:{number}", detail, blocking=True))
        else:
            found.append(Dependency("file_reference", repo, f"{path}:{number}", f"named in `{stripped}`", blocking=True))
    return found


def find_dependencies(gh: GitHub, scope: Scope, member: str) -> list[Dependency]:
    """Every in-scope thing that names or relies on `member`. Blocking ones must be handed over
    before the member can be removed; the rest are open work to report."""
    found: list[Dependency] = []
    for repo in scope.repos():
        name, full_repo = repo["name"], f"{scope.org}/{repo['name']}"
        for file in config_files(gh, full_repo, repo["default_branch"]):
            found += file_dependencies(name, file["path"], file["text"], member)
        for user in gh.paginate(f"/repos/{full_repo}/collaborators", affiliation="direct"):
            # Lesser direct access ends with the membership, so only elevated roles are reported.
            if user["login"].lower() == member.lower() and user["role_name"] in ELEVATED_ROLES:
                detail = f"direct {user['role_name']} access"
                found.append(Dependency("direct_access", name, "collaborators", detail, blocking=True))
        for pr in gh.paginate(f"/repos/{full_repo}/pulls", state="open"):
            if pr["user"]["login"].lower() == member.lower():
                detail = f"open, needs a new owner: '{pr['title']}' {pr['html_url']}"
                found.append(Dependency("open_pull_request", name, f"PR #{pr['number']}", detail, blocking=False))
    for team in gh.paginate(f"/orgs/{scope.org}/teams"):
        membership = gh.get_or_none(f"/orgs/{scope.org}/teams/{team['slug']}/memberships/{member}")
        if membership and membership["state"] == "active":
            detail = f"{membership['role']} of team {team['slug']}"
            found.append(Dependency("team_membership", None, f"team {team['slug']}", detail, blocking=True))
    return found
