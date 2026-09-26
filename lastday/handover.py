"""Runs one reversible step of an approved plan against GitHub.

Each step reads live state first and does only what is still missing, so re-running a plan
after an interruption or a demo reset does exactly the remaining work. File changes go
through a pull request that is then merged, so every handover has a reviewable record.
"""

import re

from lastday.dependencies import ELEVATED_ROLES, file_dependencies
from lastday.errors import LastDayError
from lastday.github import GitHub, GitHubError
from lastday.inventory import CODEOWNERS_LOCATIONS
from lastday.scope import Scope


class HandoverError(LastDayError):
    pass


def replace_member(text: str, path: str, member: str, successor: str) -> str:
    """Swap the member for the successor on every non-comment line that names them."""
    pattern = re.compile(rf"(?<![A-Za-z0-9-])(@?){re.escape(member)}(?![A-Za-z0-9-])", re.IGNORECASE)
    lines = []
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("#") or not pattern.search(line):
            lines.append(line)
        elif path in CODEOWNERS_LOCATIONS:
            pattern_part, *owners = line.split()
            new_owners = []
            for owner in owners:
                owner = successor if pattern.fullmatch(owner) else owner
                if owner.lower() not in (o.lower() for o in new_owners):
                    new_owners.append(owner)
            ending = "\n" if line.endswith("\n") else ""
            lines.append(" ".join([pattern_part, *new_owners]) + ending)
        else:
            lines.append(pattern.sub(lambda m: m.group(1) + successor.lstrip("@"), line))
    return "".join(lines)


class Handover:
    def __init__(self, gh: GitHub, scope: Scope, member: str, plan_hash: str):
        self.gh = gh
        self.scope = scope
        self.member = member
        self.tag = plan_hash[:8]

    def run(self, step: dict) -> str:
        fields = {key: value for key, value in step.items() if key != "type"}
        if "repo" in fields:
            self.scope.require_repo(fields["repo"])
        return getattr(self, f"_{step['type']}")(**fields)

    def _grant_team_write(self, repo: str, team: str) -> str:
        path = f"/orgs/{self.scope.org}/teams/{team}/repos/{self.scope.org}/{repo}"
        if self._team_can_write(path):
            return f"team {team} already had write access to {repo}"
        self.gh.put(path, {"permission": "push"})
        if not self._team_can_write(path):
            raise HandoverError(f"granted write to team {team} on {repo}, but GitHub does not show it")
        return f"granted team {team} write access to {repo}"

    def _team_can_write(self, path: str) -> bool:
        access = self.gh.get_or_none(path, headers={"Accept": "application/vnd.github.v3.repository+json"})
        return bool(access and access["permissions"].get("push"))

    def _hand_over_file(self, repo: str, path: str, successor: str) -> str:
        full_repo = f"{self.scope.org}/{repo}"
        base = self.gh.get(f"/repos/{full_repo}")["default_branch"]
        current = self.gh.read_file(full_repo, path, ref=base)
        if current is None:
            raise HandoverError(f"{repo}:{path} no longer exists on {base}")
        if not file_dependencies(repo, path, current.text, self.member):
            return f"{repo}:{path} no longer names {self.member}"

        slug = re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-")
        pr = self._open_handover_pr(full_repo, slug)
        branch = pr["head"]["ref"] if pr else self._create_branch(full_repo, base, slug)
        on_branch = self.gh.read_file(full_repo, path, ref=branch)
        if on_branch is None:
            raise HandoverError(f"{repo}:{path} is missing on branch {branch}")
        if file_dependencies(repo, path, on_branch.text, self.member):
            new_text = replace_member(on_branch.text, path, self.member, successor)
            title = f"Hand over {path} from @{self.member} to {successor}"
            message = f"{title}\n\nOffboarding by Last Day, plan {self.tag}."
            self.gh.write_file(full_repo, path, new_text, message, branch=branch, sha=on_branch.sha)
        if pr is None:
            pr = self.gh.post(
                f"/repos/{full_repo}/pulls",
                {
                    "title": f"Hand over {path} from @{self.member} to {successor}",
                    "head": branch,
                    "base": base,
                    "body": f"{self.member} is leaving the org. This moves what they owned in `{path}` to "
                    f"{successor}.\n\nOpened by Last Day after the plan `{self.tag}` was approved.",
                },
            )
        try:
            self.gh.put(f"/repos/{full_repo}/pulls/{pr['number']}/merge", {"merge_method": "squash"})
        except GitHubError as error:
            raise HandoverError(f"could not merge {pr['html_url']}: {error}") from error

        merged = self.gh.read_file(full_repo, path, ref=base)
        if merged is None or file_dependencies(repo, path, merged.text, self.member):
            raise HandoverError(f"merged {pr['html_url']}, but {repo}:{path} on {base} still names {self.member}")
        return f"merged {pr['html_url']}"

    def _open_handover_pr(self, full_repo: str, slug: str) -> dict | None:
        prefix = f"lastday/{self.tag}/{slug}-"
        for pr in self.gh.paginate(f"/repos/{full_repo}/pulls", state="open"):
            if pr["head"]["ref"].startswith(prefix):
                return pr
        return None

    def _create_branch(self, full_repo: str, base: str, slug: str) -> str:
        """One branch per plan, file and base commit, so a retry reuses it and a reset gets a new one."""
        base_sha = self.gh.get(f"/repos/{full_repo}/git/ref/heads/{base}")["object"]["sha"]
        branch = f"lastday/{self.tag}/{slug}-{base_sha[:7]}"
        try:
            self.gh.post(f"/repos/{full_repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
        except GitHubError as error:
            if error.status != 422:  # 422: the branch exists from an interrupted run
                raise
        return branch

    def _downgrade_collaborator(self, repo: str) -> str:
        full_repo = f"{self.scope.org}/{repo}"
        role = self._direct_role(full_repo)
        if role not in ELEVATED_ROLES:
            return f"{self.member}'s direct access to {repo} is already {role or 'none'}"
        self.gh.put(f"/repos/{full_repo}/collaborators/{self.member}", {"permission": "pull"})
        after = self._direct_role(full_repo)
        if after in ELEVATED_ROLES:
            raise HandoverError(f"asked GitHub to downgrade {self.member} on {repo}, but they still have {after}")
        # GitHub reports the effective role, which may still include access through a team.
        return f"set {self.member}'s direct access to {repo} to read (was {role})"

    def _direct_role(self, full_repo: str) -> str | None:
        for user in self.gh.paginate(f"/repos/{full_repo}/collaborators", affiliation="direct"):
            if user["login"].lower() == self.member.lower():
                return user["role_name"]
        return None

    def _remove_from_team(self, team: str) -> str:
        path = f"/orgs/{self.scope.org}/teams/{team}/memberships/{self.member}"
        if self.gh.get_or_none(path) is None:
            return f"{self.member} is already not in team {team}"
        others = [
            m["login"]
            for m in self.gh.paginate(f"/orgs/{self.scope.org}/teams/{team}/members")
            if m["login"].lower() != self.member.lower()
        ]
        if not others:
            raise HandoverError(f"team {team} would be left with no members; not removing {self.member}")
        self.gh.delete(path)
        if self.gh.get_or_none(path) is not None:
            raise HandoverError(f"removed {self.member} from team {team}, but GitHub still lists them")
        return f"removed {self.member} from team {team} ({', '.join(others)} remain)"
