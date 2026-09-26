"""Plant (or restore) the demo dependencies that Last Day is built to find.

    uv run scripts/setup_demo.py                  # preview: read the org, print what would change
    uv run scripts/setup_demo.py --apply          # make the changes
    uv run scripts/setup_demo.py --reset --apply  # also re-invite the demo member if they were removed

Every step reads the current state first and only acts if it differs, so the script is safe
to re-run. It never deletes anything: files are restored with new, clearly labelled commits.
It only touches the two demo repositories named below.
"""

import argparse
import sys
from collections.abc import Callable

from lastday.config import SCOPE_TOPIC, ConfigError, require_env
from lastday.github import GitHub, GitHubError

MEMBER = "fatbatman85"  # test account playing the departing member
TEAM = "core-team"  # successor for code ownership
CODE_REPO = "voice_mvp_dupe"
DEPLOY_REPO = "demo-repo"
CODEOWNERS_PATH = ".github/CODEOWNERS"
DEPLOY_WORKFLOW_PATH = ".github/workflows/deploy.yml"
COMMIT_PREFIX = "chore(lastday-demo):"

DEPLOY_WORKFLOW = f"""\
# Planted by Last Day's demo setup: only one person can run this deploy.
name: Deploy
on:
  workflow_dispatch:
jobs:
  deploy:
    if: github.actor == '{MEMBER}'
    runs-on: ubuntu-latest
    steps:
      - run: echo "Demo deploy of demo-repo; nothing is deployed."
"""


def codeowners(operator: str) -> str:
    return f"# Planted by Last Day's demo setup.\n* @{operator}\n/src/ @{MEMBER}\n"


class Runner:
    """Reports each desired state as ok, change (preview only) or done (applied)."""

    def __init__(self, apply: bool):
        self.apply = apply
        self.pending = 0

    def ensure(self, satisfied: bool, description: str, action: Callable[[], None]) -> None:
        if satisfied:
            print(f"  ok      {description}")
        elif not self.apply:
            print(f"  change  {description}")
            self.pending += 1
        else:
            action()
            print(f"  done    {description}")


def preflight(gh: GitHub, org: str) -> str:
    operator = gh.get("/user")["login"]
    membership = gh.get_or_none(f"/orgs/{org}/memberships/{operator}")
    if not membership or membership["role"] != "admin":
        raise SystemExit(f"{operator} must be an owner of {org} to plant the demo.")
    member = gh.get_or_none(f"/orgs/{org}/memberships/{MEMBER}")
    if member and member["role"] == "admin":
        raise SystemExit(f"{MEMBER} is an owner of {org}; the demo member must be a regular member.")
    return operator


def ensure_member_in_org(run: Runner, gh: GitHub, org: str, reset: bool) -> None:
    membership = gh.get_or_none(f"/orgs/{org}/memberships/{MEMBER}")
    active = bool(membership and membership["state"] == "active")
    if not active and not reset:
        raise SystemExit(f"{MEMBER} is not an active member of {org}. Re-run with --reset.")

    def invite_and_accept() -> None:
        if membership is None:
            user_id = gh.get(f"/users/{MEMBER}")["id"]
            gh.post(f"/orgs/{org}/invitations", {"invitee_id": user_id, "role": "direct_member"})
        GitHub(require_env("FATBATMAN_TOKEN")).patch(f"/user/memberships/orgs/{org}", {"state": "active"})

    run.ensure(active, f"{MEMBER} is a member of {org} (invite, then accept as {MEMBER})", invite_and_accept)


def ensure_topic(run: Runner, gh: GitHub, full_repo: str) -> None:
    names = gh.get(f"/repos/{full_repo}/topics")["names"]
    run.ensure(
        SCOPE_TOPIC in names,
        f"{full_repo} has topic {SCOPE_TOPIC}",
        lambda: gh.put(f"/repos/{full_repo}/topics", {"names": [*names, SCOPE_TOPIC]}),
    )


def ensure_team(run: Runner, gh: GitHub, org: str, operator: str) -> None:
    team_path = f"/orgs/{org}/teams/{TEAM}"
    run.ensure(
        gh.get_or_none(team_path) is not None,
        f"team {TEAM} exists and is visible to the org",
        lambda: gh.post(
            f"/orgs/{org}/teams",
            {"name": TEAM, "description": "Successor owners in the Last Day demo", "privacy": "closed"},
        ),
    )
    for user, role in ((MEMBER, "maintainer"), (operator, None)):
        current = gh.get_or_none(f"{team_path}/memberships/{user}")
        satisfied = bool(current and current["state"] == "active" and role in (None, current["role"]))
        run.ensure(
            satisfied,
            f"{user} is {'a ' + role + ' of' if role else 'in'} {TEAM}",
            lambda user=user, role=role: gh.put(f"{team_path}/memberships/{user}", {"role": role or "member"}),
        )


def ensure_team_read_only(run: Runner, gh: GitHub, org: str, full_repo: str) -> None:
    """The successor team starts with read only, so the handover must grant write first."""
    repo = gh.get_or_none(
        f"/orgs/{org}/teams/{TEAM}/repos/{full_repo}",
        headers={"Accept": "application/vnd.github.v3.repository+json"},
    )
    permissions = (repo or {}).get("permissions", {})
    run.ensure(
        bool(permissions.get("pull")) and not permissions.get("push"),
        f"{TEAM} has read, not write, on {full_repo}",
        lambda: gh.put(f"/orgs/{org}/teams/{TEAM}/repos/{full_repo}", {"permission": "pull"}),
    )


def ensure_direct_admin(run: Runner, gh: GitHub, full_repo: str) -> None:
    direct = gh.paginate(f"/repos/{full_repo}/collaborators", affiliation="direct")
    role = next((c["role_name"] for c in direct if c["login"] == MEMBER), None)
    run.ensure(
        role == "admin",
        f"{MEMBER} is a direct admin of {full_repo}",
        lambda: gh.put(f"/repos/{full_repo}/collaborators/{MEMBER}", {"permission": "admin"}),
    )


def ensure_file(run: Runner, gh: GitHub, full_repo: str, branch: str, path: str, text: str) -> None:
    current = gh.read_file(full_repo, path, ref=branch)
    verb = "restore" if current else "add"
    run.ensure(
        current is not None and current.text == text,
        f"{full_repo}:{path} is the planted version",
        lambda: gh.write_file(
            full_repo,
            path,
            text,
            message=f"{COMMIT_PREFIX} {verb} {path} for the offboarding demo\n\n"
            "Planted by Last Day's setup script so the agent has a real dependency to hand over.",
            branch=branch,
            sha=current.sha if current else None,
        ),
    )


def ensure_code_owner_review(run: Runner, gh: GitHub, full_repo: str, branch: str) -> None:
    protection = gh.get_or_none(f"/repos/{full_repo}/branches/{branch}/protection")
    reviews = (protection or {}).get("required_pull_request_reviews", {})
    admins_enforced = (protection or {}).get("enforce_admins", {}).get("enabled", False)
    run.ensure(
        bool(reviews.get("require_code_owner_reviews")) and not admins_enforced,
        f"{full_repo}:{branch} requires code-owner review (admins not enforced)",
        lambda: gh.put(
            f"/repos/{full_repo}/branches/{branch}/protection",
            {
                "required_status_checks": None,
                "enforce_admins": False,
                "required_pull_request_reviews": {
                    "require_code_owner_reviews": True,
                    "required_approving_review_count": 1,
                },
                "restrictions": None,
            },
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="make the changes (default: preview only)")
    parser.add_argument("--reset", action="store_true", help="re-invite the demo member if they were removed")
    args = parser.parse_args()

    try:
        org = require_env("GITHUB_ORG")
        gh = GitHub(require_env("GITHUB_TOKEN"))
        operator = preflight(gh, org)
        code_repo, deploy_repo = f"{org}/{CODE_REPO}", f"{org}/{DEPLOY_REPO}"
        run = Runner(apply=args.apply)
        print(f"{'Applying' if args.apply else 'Preview of'} the Last Day demo state in {org} (as {operator}):")

        ensure_member_in_org(run, gh, org, reset=args.reset)
        for full_repo in (deploy_repo, code_repo):
            ensure_topic(run, gh, full_repo)
        ensure_team(run, gh, org, operator)
        ensure_team_read_only(run, gh, org, code_repo)
        ensure_direct_admin(run, gh, code_repo)
        code_branch = gh.get(f"/repos/{code_repo}")["default_branch"]
        deploy_branch = gh.get(f"/repos/{deploy_repo}")["default_branch"]
        ensure_file(run, gh, code_repo, code_branch, CODEOWNERS_PATH, codeowners(operator))
        ensure_file(run, gh, deploy_repo, deploy_branch, DEPLOY_WORKFLOW_PATH, DEPLOY_WORKFLOW)
        ensure_code_owner_review(run, gh, code_repo, code_branch)
    except (ConfigError, GitHubError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if run.pending:
        print(f"{run.pending} change(s) pending. Re-run with --apply to make them.")
    else:
        print("The demo state is in place.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
