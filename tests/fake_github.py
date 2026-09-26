"""An in-memory stand-in for the GitHub REST API, served through httpx's mock transport."""

import json

import httpx

from lastday.github import GitHub


class FakeGitHub:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, object]] = {}
        self.calls: list[tuple[str, str]] = []

    def on(self, method: str, path: str, body: object = None, status: int = 200) -> "FakeGitHub":
        self.routes[(method, path)] = (status, body)
        return self

    def client(self) -> GitHub:
        return GitHub("test-token", transport=httpx.MockTransport(self._handle))

    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        self.calls.append(key)
        status, body = self.routes.get(key, (404, {"message": "Not Found"}))
        if status == 204:
            return httpx.Response(204)
        return httpx.Response(status, content=json.dumps(body))
