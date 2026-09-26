"""A small GitHub REST client. Errors carry GitHub's own message, never the token."""

import base64
from dataclasses import dataclass
from typing import Any

import httpx

API_URL = "https://api.github.com"


class GitHubError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, message: str):
        self.status = status
        hint = ""
        if status in (401, 403):
            hint = " (check that the token is valid and has the permissions listed in .env.example)"
        super().__init__(f"GitHub {method} {path} failed with {status}: {message}{hint}")


@dataclass
class RepoFile:
    text: str
    sha: str


class GitHub:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "lastday",
            },
            timeout=30,
            transport=transport,
        )

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._http.request(method, path, **kwargs)
        if response.is_error:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise GitHubError(method, path, response.status_code, message)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def get(self, path: str, *, headers: dict | None = None, **params: Any) -> Any:
        return self.request("GET", path, params=params or None, headers=headers)

    def get_or_none(self, path: str, *, headers: dict | None = None, **params: Any) -> Any:
        """GET that returns None instead of raising when the resource does not exist."""
        try:
            return self.get(path, headers=headers, **params)
        except GitHubError as error:
            if error.status == 404:
                return None
            raise

    def paginate(self, path: str, **params: Any) -> list[Any]:
        items: list[Any] = []
        page = 1
        while True:
            batch = self.get(path, per_page=100, page=page, **params)
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1

    def put(self, path: str, body: dict | None = None) -> Any:
        return self.request("PUT", path, json=body)

    def post(self, path: str, body: dict) -> Any:
        return self.request("POST", path, json=body)

    def patch(self, path: str, body: dict) -> Any:
        return self.request("PATCH", path, json=body)

    def delete(self, path: str) -> Any:
        return self.request("DELETE", path)

    def read_file(self, full_repo: str, path: str, ref: str | None = None) -> RepoFile | None:
        params = {"ref": ref} if ref else {}
        data = self.get_or_none(f"/repos/{full_repo}/contents/{path}", **params)
        if data is None:
            return None
        text = base64.b64decode(data["content"]).decode("utf-8")
        return RepoFile(text=text, sha=data["sha"])

    def write_file(
        self,
        full_repo: str,
        path: str,
        text: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> None:
        body = {
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        self.put(f"/repos/{full_repo}/contents/{path}", body)
