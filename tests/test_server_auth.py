from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from lastday.server import BearerAuth


def client() -> TestClient:
    inner = Starlette(routes=[Route("/mcp", lambda request: PlainTextResponse("ok"), methods=["POST"])])
    return TestClient(BearerAuth(inner, "secret"))


def test_request_without_token_is_rejected():
    assert client().post("/mcp").status_code == 401


def test_request_with_wrong_token_is_rejected():
    assert client().post("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_request_with_token_reaches_the_server():
    response = client().post("/mcp", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
