"""remove_org_member: every refusal path, dry run, and the verified removal."""

import pytest

from lastday import actions
from lastday.plan import normalize_steps, plan_hash
from lastday.store import PlanStore
from tests.fake_org import MEMBER, OPERATOR, ORG, PLAN_STEPS, fake_org, scope_for

MEMBERS_PATH = f"/orgs/{ORG}/members/{MEMBER}"


def stored_plan(store: PlanStore, done: bool, member: str = MEMBER) -> str:
    steps = normalize_steps(PLAN_STEPS)
    digest = plan_hash(member, steps)
    store.save({"hash": digest, "member": member, "steps": steps})
    if done:
        for index in range(len(steps)):
            store.record(digest, "step", index=index, status="done", detail=f"step {index} done")
    return digest


def remove(fake, store, digest="", member=MEMBER, dry_run=False):
    return actions.remove_org_member(fake.client(), scope_for(fake), store, member, digest, dry_run)


@pytest.fixture
def store(tmp_path):
    return PlanStore(tmp_path)


def test_refusal_without_a_plan_lists_every_dependency_from_the_rescan(store):
    fake = fake_org()
    with pytest.raises(actions.RemovalRefused) as refused:
        remove(fake, store)
    message = str(refused.value)
    assert "there is no approved plan" in message
    assert "app:.github/CODEOWNERS:2: sole owner of /src/" in message
    assert "app:collaborators: direct admin access" in message
    assert "team core: maintainer of team core" in message
    assert "app:PR #7: open, needs a new owner" in message
    assert ("DELETE", MEMBERS_PATH) not in fake.calls


def test_refusal_when_approved_steps_are_not_done(store):
    fake = fake_org()
    digest = stored_plan(store, done=False)
    with pytest.raises(actions.RemovalRefused, match="not done yet: grant team core write access"):
        remove(fake, store, digest)
    assert fake.writes() == []


def test_refusal_when_rescan_still_finds_dependencies_even_if_journal_says_done(store):
    fake = fake_org(handed_over=False)
    digest = stored_plan(store, done=True)
    with pytest.raises(actions.RemovalRefused) as refused:
        remove(fake, store, digest)
    assert "re-scan still finds blocking dependencies" in str(refused.value)
    assert fake.writes() == []


def test_refusal_when_plan_belongs_to_someone_else(store):
    fake = fake_org(handed_over=True)
    digest = stored_plan(store, done=True, member="someone-else")
    with pytest.raises(actions.RemovalRefused, match="is for someone-else, not leaver"):
        remove(fake, store, digest)


def test_owners_are_never_removed(store):
    fake = fake_org(handed_over=True).on("GET", f"/orgs/{ORG}/memberships/{MEMBER}", {"state": "active", "role": "admin"})
    digest = stored_plan(store, done=True)
    with pytest.raises(actions.RemovalRefused, match="never offboards owners"):
        remove(fake, store, digest)
    assert fake.writes() == []


def test_the_operator_cannot_remove_itself(store):
    fake = fake_org(handed_over=True)
    with pytest.raises(actions.RemovalRefused, match="cannot offboard itself"):
        remove(fake, store, member=OPERATOR)


def test_dry_run_passes_every_check_and_changes_nothing(store):
    fake = fake_org(handed_over=True)
    digest = stored_plan(store, done=True)
    result = remove(fake, store, digest, dry_run=True)
    assert result["status"].startswith("DRY RUN")
    assert result["open_work"] == ["app:PR #7: open, needs a new owner: 'Half-done feature' https://pr/7"]
    assert fake.writes() == []


def test_removal_happens_only_when_everything_is_clear_and_is_verified(store):
    fake = fake_org(handed_over=True)
    fake.on("DELETE", MEMBERS_PATH, status=204)
    fake.on(
        "GET",
        f"/orgs/{ORG}/memberships/{MEMBER}",
        lambda: (404, {}) if ("DELETE", MEMBERS_PATH) in fake.calls else (200, {"state": "active", "role": "member"}),
    )
    digest = stored_plan(store, done=True)
    result = remove(fake, store, digest)
    assert result["status"] == f"removed {MEMBER} from {ORG} and verified they are no longer a member"
    assert fake.writes() == [("DELETE", MEMBERS_PATH)]

    again = remove(fake, store, digest)
    assert again["status"] == f"{MEMBER} was already removed from {ORG}"
    assert fake.writes() == [("DELETE", MEMBERS_PATH)]
