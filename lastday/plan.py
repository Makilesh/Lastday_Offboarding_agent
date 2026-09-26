"""Offboarding plans: an allowlist of step types, a content hash, and checks against the
server's own scan. A plan is only stored if it covers every blocking dependency and
contains nothing else.
"""

import hashlib
import json

from lastday.dependencies import Dependency
from lastday.errors import LastDayError
from lastday.github import GitHub
from lastday.inventory import CODEOWNERS_LOCATIONS
from lastday.scope import Scope

# Allowed step types, their fields, and the order they always run in.
STEP_FIELDS = {
    "grant_team_write": ("repo", "team"),
    "hand_over_file": ("repo", "path", "successor"),
    "downgrade_collaborator": ("repo",),
    "remove_from_team": ("team",),
}
STEP_ORDER = list(STEP_FIELDS)


class PlanError(LastDayError):
    pass


def normalize_steps(steps: list[dict]) -> list[dict]:
    """Reject unknown step types or fields, and put steps in their fixed execution order."""
    normalized = []
    for index, step in enumerate(steps):
        kind = step.get("type")
        if kind not in STEP_FIELDS:
            raise PlanError(f"step {index + 1}: type {kind!r} is not allowed; allowed types: {', '.join(STEP_ORDER)}")
        fields = STEP_FIELDS[kind]
        extra = set(step) - {"type", *fields}
        missing = [field for field in fields if not isinstance(step.get(field), str) or not step[field].strip()]
        if extra or missing:
            raise PlanError(f"step {index + 1} ({kind}) must have exactly these text fields: {', '.join(fields)}")
        normalized.append({"type": kind, **{field: step[field].strip() for field in fields}})
    if not normalized:
        raise PlanError("a plan needs at least one step")
    unique = {json.dumps(step, sort_keys=True): step for step in normalized}
    return sorted(unique.values(), key=lambda s: (STEP_ORDER.index(s["type"]), json.dumps(s, sort_keys=True)))


def plan_hash(member: str, steps: list[dict]) -> str:
    canonical = json.dumps({"member": member.lower(), "steps": steps}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def describe_step(step: dict) -> str:
    kind = step["type"]
    if kind == "grant_team_write":
        return f"grant team {step['team']} write access to {step['repo']}"
    if kind == "hand_over_file":
        return f"hand over {step['repo']}:{step['path']} to {step['successor']} (pull request, then merge)"
    if kind == "downgrade_collaborator":
        return f"downgrade the member's direct access to {step['repo']} to read"
    return f"remove the member from team {step['team']}"


def check_plan(gh: GitHub, scope: Scope, member: str, steps: list[dict], dependencies: list[Dependency]) -> None:
    """Refuse a plan that leaves a blocking dependency unhandled, adds a step nothing needs,
    or hands anything to an invalid successor."""
    problems = _uncovered(steps, dependencies) + _unneeded(steps, dependencies)
    for step in steps:
        problems += _successor_problems(gh, scope, member, step, steps)
    if problems:
        raise PlanError("the plan was not stored:\n- " + "\n- ".join(problems))


def _file_key(dep: Dependency) -> tuple[str, str]:
    return dep.repo, dep.where.rsplit(":", 1)[0]


def _uncovered(steps: list[dict], dependencies: list[Dependency]) -> list[str]:
    handed_over = {(s["repo"], s["path"]) for s in steps if s["type"] == "hand_over_file"}
    downgraded = {s["repo"] for s in steps if s["type"] == "downgrade_collaborator"}
    left_teams = {s["team"] for s in steps if s["type"] == "remove_from_team"}
    problems = []
    for dep in dependencies:
        covered = (
            not dep.blocking
            or (dep.kind in ("codeowner", "file_reference") and _file_key(dep) in handed_over)
            or (dep.kind == "direct_access" and dep.repo in downgraded)
            or (dep.kind == "team_membership" and dep.where.removeprefix("team ") in left_teams)
        )
        if not covered:
            problems.append(f"nothing handles {dep.describe()}")
    return problems


def _unneeded(steps: list[dict], dependencies: list[Dependency]) -> list[str]:
    files = {_file_key(d) for d in dependencies if d.kind in ("codeowner", "file_reference")}
    elevated = {d.repo for d in dependencies if d.kind == "direct_access" and d.blocking}
    teams = {d.where.removeprefix("team ") for d in dependencies if d.kind == "team_membership"}
    team_handovers = {
        (s["repo"], s["successor"].rsplit("/", 1)[-1])
        for s in steps
        if s["type"] == "hand_over_file" and "/" in s["successor"]
    }
    problems = []
    for step in steps:
        needed = {
            "hand_over_file": lambda s: (s["repo"], s["path"]) in files,
            "downgrade_collaborator": lambda s: s["repo"] in elevated,
            "remove_from_team": lambda s: s["team"] in teams,
            "grant_team_write": lambda s: (s["repo"], s["team"]) in team_handovers,
        }[step["type"]](step)
        if not needed:
            problems.append(f"no dependency needs this step: {describe_step(step)}")
    return problems


def _successor_problems(gh: GitHub, scope: Scope, member: str, step: dict, steps: list[dict]) -> list[str]:
    kind = step["type"]
    if kind in ("grant_team_write", "remove_from_team"):
        return _team_problems(gh, scope, member, step["team"])
    if kind != "hand_over_file":
        return []

    successor = step["successor"]
    where = f"{step['repo']}:{step['path']}"
    if step["path"] in CODEOWNERS_LOCATIONS:
        if not successor.startswith("@"):
            return [f"{where}: a code owner must be written as @user or @{scope.org}/team, got {successor!r}"]
        name = successor[1:]
        if "/" in name:
            org, team = name.split("/", 1)
            if org.lower() != scope.org.lower():
                return [f"{where}: team {successor} is not in {scope.org}"]
            problems = _team_problems(gh, scope, member, team)
            granted = {"type": "grant_team_write", "repo": step["repo"], "team": team} in steps
            if not problems and not granted and not _team_can_write(gh, scope, team, step["repo"]):
                problems.append(
                    f"{where}: GitHub ignores code owners without write access; team {team} has no write "
                    f"access to {step['repo']} and the plan does not grant it"
                )
            return problems
        return _user_problems(gh, scope, member, name, where, repo=step["repo"])
    if successor.startswith("@") or "/" in successor:
        return [f"{where}: the successor here must be a user login, got {successor!r}"]
    return _user_problems(gh, scope, member, successor, where)


def _team_problems(gh: GitHub, scope: Scope, member: str, team: str) -> list[str]:
    """A successor team must exist, be visible, and keep a member after the departing one leaves."""
    info = gh.get_or_none(f"/orgs/{scope.org}/teams/{team}")
    if info is None:
        return [f"team {team} does not exist in {scope.org}"]
    if info["privacy"] != "closed":
        return [f"team {team} is secret; GitHub ignores secret teams as code owners"]
    others = [
        m["login"]
        for m in gh.paginate(f"/orgs/{scope.org}/teams/{team}/members")
        if m["login"].lower() != member.lower()
    ]
    if not others:
        return [f"team {team} would be left with no members once {member} leaves"]
    return []


def _team_can_write(gh: GitHub, scope: Scope, team: str, repo: str) -> bool:
    access = gh.get_or_none(
        f"/orgs/{scope.org}/teams/{team}/repos/{scope.org}/{repo}",
        headers={"Accept": "application/vnd.github.v3.repository+json"},
    )
    return bool(access and access["permissions"].get("push"))


def _user_problems(gh: GitHub, scope: Scope, member: str, login: str, where: str, repo: str | None = None) -> list[str]:
    if login.lower() == member.lower():
        return [f"{where}: cannot hand over to the departing member"]
    membership = gh.get_or_none(f"/orgs/{scope.org}/memberships/{login}")
    if membership is None or membership["state"] != "active":
        return [f"{where}: successor {login} is not an active member of {scope.org}"]
    if repo:
        permission = gh.get_or_none(f"/repos/{scope.org}/{repo}/collaborators/{login}/permission")
        if not permission or permission["role_name"] not in ("write", "maintain", "admin"):
            return [f"{where}: GitHub ignores code owners without write access; {login} cannot write to {repo}"]
    return []

