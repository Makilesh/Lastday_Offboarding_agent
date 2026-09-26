"""propose_plan and apply_reversible_steps refuse anything outside the rules."""

import pytest

from lastday import actions
from lastday.handover import replace_member
from lastday.plan import PlanError, normalize_steps
from lastday.store import PlanStore
from tests.fake_org import MEMBER, ORG, PLAN_STEPS, REPO, fake_org, scope_for


@pytest.fixture
def store(tmp_path):
    return PlanStore(tmp_path)


def propose(fake, store, steps):
    return actions.propose_plan(fake.client(), scope_for(fake), store, MEMBER, steps)


def test_complete_plan_is_stored_in_fixed_order_with_a_stable_hash(store):
    fake = fake_org()
    first = propose(fake, store, PLAN_STEPS)
    second = propose(fake, store, list(reversed(PLAN_STEPS)))
    assert first["plan_hash"] == second["plan_hash"]
    assert [s["type"] for s in first["steps"]] == [
        "grant_team_write",
        "hand_over_file",
        "downgrade_collaborator",
        "remove_from_team",
    ]
    assert store.load(first["plan_hash"])["steps"] == first["steps"]
    assert fake.writes() == []


def test_plan_that_leaves_a_dependency_unhandled_is_refused(store):
    steps = [s for s in PLAN_STEPS if s["type"] != "downgrade_collaborator"]
    with pytest.raises(PlanError, match="nothing handles app:collaborators: direct admin access"):
        propose(fake_org(), store, steps)


def test_step_that_no_dependency_needs_is_refused(store):
    steps = [*PLAN_STEPS, {"type": "grant_team_write", "repo": REPO, "team": "other"}]
    with pytest.raises(PlanError, match="no dependency needs this step: grant team other write"):
        propose(fake_org(), store, steps)


def test_unknown_step_type_is_refused():
    with pytest.raises(PlanError, match="type 'delete_repo' is not allowed"):
        normalize_steps([{"type": "delete_repo", "repo": REPO}])


def test_extra_fields_are_refused():
    with pytest.raises(PlanError, match="exactly these text fields: repo"):
        normalize_steps([{"type": "downgrade_collaborator", "repo": REPO, "member": "someone"}])


def test_code_owner_team_without_write_access_is_refused(store):
    steps = [s for s in PLAN_STEPS if s["type"] != "grant_team_write"]
    with pytest.raises(PlanError, match="GitHub ignores code owners without write access"):
        propose(fake_org(), store, steps)


def test_successor_team_that_would_be_left_empty_is_refused(store):
    fake = fake_org().on("GET", f"/orgs/{ORG}/teams/core/members", [{"login": MEMBER}])
    with pytest.raises(PlanError, match="team core would be left with no members"):
        propose(fake, store, PLAN_STEPS)


def test_apply_refuses_steps_that_differ_from_the_stored_plan(store):
    fake = fake_org()
    plan = propose(fake, store, PLAN_STEPS)
    tampered = [s for s in plan["steps"] if s["type"] != "remove_from_team"]
    with pytest.raises(PlanError, match="differ from stored plan"):
        actions.apply_reversible_steps(fake.client(), scope_for(fake), store, plan["plan_hash"], tampered)
    assert fake.writes() == []


def test_apply_refuses_an_unknown_plan(store):
    fake = fake_org()
    with pytest.raises(PlanError, match="no stored plan"):
        actions.apply_reversible_steps(fake.client(), scope_for(fake), store, "0" * 64, PLAN_STEPS)


def test_codeowners_handover_swaps_only_the_member():
    text = "# owners, formerly @leaver\n* @operator\n/src/ @leaver @acme/core\n/api/ @Leaver\n"
    assert replace_member(text, ".github/CODEOWNERS", "leaver", "@acme/core") == (
        "# owners, formerly @leaver\n* @operator\n/src/ @acme/core\n/api/ @acme/core\n"
    )


def test_workflow_handover_keeps_the_surrounding_expression():
    text = "    if: github.actor == 'leaver'\n"
    assert replace_member(text, ".github/workflows/deploy.yml", "leaver", "heir") == "    if: github.actor == 'heir'\n"
