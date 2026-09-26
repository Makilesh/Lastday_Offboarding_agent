import pytest

from lastday.scope import Scope, ScopeError
from tests.fake_github import FakeGitHub

ORG = "acme"


def fake_org() -> FakeGitHub:
    return (
        FakeGitHub()
        .on("GET", "/user", {"login": "operator"})
        .on(
            "GET",
            f"/orgs/{ORG}/repos",
            [
                {"name": "tagged", "default_branch": "main", "topics": ["lastday-demo"]},
                {"name": "untagged", "default_branch": "main", "topics": []},
                {"name": "archived", "default_branch": "main", "topics": ["lastday-demo"], "archived": True},
            ],
        )
        .on("GET", f"/orgs/{ORG}/memberships/leaver", {"state": "active", "role": "member"})
        .on("GET", f"/orgs/{ORG}/memberships/owner", {"state": "active", "role": "admin"})
        .on("GET", f"/orgs/{ORG}/memberships/invited", {"state": "pending", "role": "member"})
    )


def scope() -> Scope:
    return Scope(fake_org().client(), ORG)


def test_only_tagged_unarchived_repos_are_in_scope():
    assert [repo["name"] for repo in scope().repos()] == ["tagged"]


@pytest.mark.parametrize("name", ["untagged", "archived", "missing"])
def test_out_of_scope_repo_is_refused(name):
    with pytest.raises(ScopeError, match="out of scope.*In-scope repositories: tagged\\.$"):
        scope().require_repo(name)


def test_regular_active_member_can_be_offboarded():
    assert scope().require_departing_member("leaver")["role"] == "member"


@pytest.mark.parametrize(
    ("login", "reason"),
    [
        ("owner", "never offboards owners"),
        ("operator", "cannot offboard itself"),
        ("invited", "not an active member"),
        ("stranger", "not an active member"),
    ],
)
def test_departing_member_guard(login, reason):
    with pytest.raises(ScopeError, match=reason):
        scope().require_departing_member(login)
