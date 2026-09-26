"""The hard boundary: which repositories and which people Last Day may act on.

Every tool goes through this module. The checks read live GitHub state on each call, so
removing the topic from a repository takes it out of scope immediately.
"""

from functools import cached_property

from lastday.config import SCOPE_TOPIC
from lastday.errors import LastDayError
from lastday.github import GitHub


class ScopeError(LastDayError):
    pass


class Scope:
    def __init__(self, gh: GitHub, org: str):
        self.gh = gh
        self.org = org

    @cached_property
    def operator(self) -> str:
        """The login the token acts as."""
        return self.gh.get("/user")["login"]

    def repos(self) -> list[dict]:
        """Non-archived org repositories tagged with the scope topic."""
        return [
            {"name": repo["name"], "default_branch": repo["default_branch"]}
            for repo in self.gh.paginate(f"/orgs/{self.org}/repos", type="all")
            if SCOPE_TOPIC in repo.get("topics", []) and not repo.get("archived")
        ]

    def require_repo(self, name: str) -> dict:
        for repo in self.repos():
            if repo["name"] == name:
                return repo
        raise ScopeError(
            f"{self.org}/{name} is out of scope: Last Day only works on repositories tagged {SCOPE_TOPIC}."
        )

    def require_departing_member(self, login: str) -> dict:
        """The member being offboarded must be a regular, active member and not the operator."""
        if login.lower() == self.operator.lower():
            raise ScopeError(f"{login} is the account Last Day acts as; it cannot offboard itself.")
        membership = self.gh.get_or_none(f"/orgs/{self.org}/memberships/{login}")
        if membership is None or membership["state"] != "active":
            raise ScopeError(f"{login} is not an active member of {self.org}.")
        if membership["role"] == "admin":
            raise ScopeError(f"{login} is an owner of {self.org}; Last Day never offboards owners.")
        return membership
