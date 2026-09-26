"""The three state-changing operations behind the MCP tools: store a plan, apply its
reversible steps, and remove the member. Each enforces its own preconditions."""

import httpx

from lastday.dependencies import Dependency, find_dependencies
from lastday.errors import LastDayError
from lastday.github import GitHub
from lastday.handover import Handover
from lastday.plan import PlanError, check_plan, describe_step, normalize_steps, plan_hash
from lastday.scope import Scope, ScopeError
from lastday.store import PlanStore


class RemovalRefused(LastDayError):
    pass


def propose_plan(gh: GitHub, scope: Scope, store: PlanStore, member: str, steps: list[dict]) -> dict:
    scope.require_departing_member(member)
    normalized = normalize_steps(steps)
    for step in normalized:
        if "repo" in step:
            scope.require_repo(step["repo"])
    dependencies = find_dependencies(gh, scope, member)
    check_plan(gh, scope, member, normalized, dependencies)

    digest = plan_hash(member, normalized)
    store.save({"hash": digest, "member": member, "steps": normalized})
    store.record(digest, "proposed")
    return {
        "plan_hash": digest,
        "member": member,
        "steps": normalized,
        "summary": [describe_step(step) for step in normalized],
        "open_work": [dep.describe() for dep in dependencies if not dep.blocking],
        "next": "Ask for approval by calling apply_reversible_steps with this plan_hash and exactly these steps.",
    }


def apply_reversible_steps(gh: GitHub, scope: Scope, store: PlanStore, digest: str, steps: list[dict]) -> dict:
    plan = _load_plan(store, digest)
    if normalize_steps(steps) != plan["steps"]:
        raise PlanError(f"these steps differ from stored plan {digest[:8]}; nothing was changed")
    member = plan["member"]
    scope.require_departing_member(member)

    handover = Handover(gh, scope, member, digest)
    results = []
    for index, step in enumerate(plan["steps"]):
        try:
            detail = handover.run(step)
        except (LastDayError, httpx.HTTPError) as error:
            store.record(digest, "step", index=index, status="failed", detail=str(error))
            return {
                "status": "stopped at the first failure; later steps were not run",
                "plan_hash": digest,
                "done": results,
                "failed": {"step": describe_step(step), "error": str(error)},
                "not_run": [describe_step(s) for s in plan["steps"][index + 1 :]],
            }
        store.record(digest, "step", index=index, status="done", detail=detail)
        results.append({"step": describe_step(step), "result": detail})
    return {"status": "all reversible steps done", "plan_hash": digest, "done": results}


def remove_org_member(
    gh: GitHub, scope: Scope, store: PlanStore, member: str, digest: str, dry_run: bool
) -> dict:
    """Remove the member only if the approved plan is fully done and the server's own
    re-scan finds nothing that still depends on them."""
    plan = store.load(digest) if digest else None
    if plan and plan["member"].lower() == member.lower() and _already_removed(gh, scope, store, plan):
        return {"status": f"{member} was already removed from {scope.org}", "plan_hash": digest}

    dependencies = find_dependencies(gh, scope, member)
    reasons = []
    try:
        scope.require_departing_member(member)
    except ScopeError as error:
        reasons.append(str(error))
    reasons += _plan_problems(store, plan, digest, member)
    if any(dep.blocking for dep in dependencies):
        reasons.append("the server's own re-scan still finds blocking dependencies")
    if reasons:
        raise RemovalRefused(_refusal(member, reasons, dependencies))

    open_work = [dep.describe() for dep in dependencies if not dep.blocking]
    if dry_run:
        store.record(digest, "dry_run")
        return {
            "status": f"DRY RUN: every check passed; {member} would be removed from {scope.org}, nothing was changed",
            "plan_hash": digest,
            "open_work": open_work,
        }

    gh.delete(f"/orgs/{scope.org}/members/{member}")
    if gh.get_or_none(f"/orgs/{scope.org}/memberships/{member}") is not None:
        raise LastDayError(f"asked GitHub to remove {member}, but {scope.org} still lists them")
    store.record(digest, "removed", member=member)
    return {
        "status": f"removed {member} from {scope.org} and verified they are no longer a member",
        "plan_hash": digest,
        "handed_over": [entry["detail"] for entry in store.step_status(digest).values()],
        "open_work": open_work,
    }


def _load_plan(store: PlanStore, digest: str) -> dict:
    plan = store.load(digest)
    if plan is None:
        raise PlanError(f"there is no stored plan {digest!r}; call propose_plan first")
    return plan


def _plan_problems(store: PlanStore, plan: dict | None, digest: str, member: str) -> list[str]:
    if plan is None:
        return [f"there is no approved plan{f' {digest!r}' if digest else ''} for {member}"]
    if plan["member"].lower() != member.lower():
        return [f"plan {digest[:8]} is for {plan['member']}, not {member}"]
    status = store.step_status(digest)
    unfinished = [
        describe_step(step) for index, step in enumerate(plan["steps"]) if status.get(index, {}).get("status") != "done"
    ]
    if unfinished:
        return ["these approved steps are not done yet: " + "; ".join(unfinished)]
    return []


def _already_removed(gh: GitHub, scope: Scope, store: PlanStore, plan: dict) -> bool:
    removed = any(entry["event"] == "removed" for entry in store.entries(plan["hash"]))
    return removed and gh.get_or_none(f"/orgs/{scope.org}/memberships/{plan['member']}") is None


def _refusal(member: str, reasons: list[str], dependencies: list[Dependency]) -> str:
    lines = [f"Refused: {member} was not removed.", "Why:"]
    lines += [f"- {reason}" for reason in reasons]
    blocking = [dep for dep in dependencies if dep.blocking]
    lines.append(f"Still depending on {member} (server re-scan):" if blocking else "Server re-scan: nothing blocking.")
    lines += [f"- {dep.describe()}" for dep in blocking]
    open_work = [dep for dep in dependencies if not dep.blocking]
    if open_work:
        lines.append("Open work that needs a new owner:")
        lines += [f"- {dep.describe()}" for dep in open_work]
    return "\n".join(lines)
