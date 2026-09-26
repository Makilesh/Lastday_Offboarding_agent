"""register_agent.py against a fake TrueForge: --check only reads, surprises are printed in full."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "register_agent.py"
spec = importlib.util.spec_from_file_location("register_agent", SCRIPT)
register_agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(register_agent)

SECRET = "s3cret-token"
TOOLS = ["apply_reversible_steps", "get_member_access", "propose_plan", "remove_org_member"]


class FakeTrueForge:
    def __init__(self, routes: dict[tuple[str, str], tuple[int, object]]):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []

    def client(self) -> register_agent.TrueForge:
        http = httpx.Client(base_url="http://trueforge.test", transport=httpx.MockTransport(self.handle))
        return register_agent.TrueForge(http, SECRET)

    def handle(self, request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        self.calls.append(key)
        status, body = self.routes.get(key, (404, {"error": {"message": "not found"}}))
        content = body if isinstance(body, str) else json.dumps(body)
        return httpx.Response(status, content=content)


def fresh_trueforge() -> FakeTrueForge:
    return FakeTrueForge(
        {
            ("GET", "/api/v1/capabilities"): (200, {"data": {}}),
            ("GET", "/api/v1/models"): (200, {"data": [{"name": "anthropic/some-model"}]}),
            ("GET", "/api/v1/agents"): (200, {"data": []}),
        }
    )


@pytest.fixture(autouse=True)
def local_server_up(monkeypatch):
    monkeypatch.setattr(register_agent, "local_server_status", lambda url: (True, "Last Day MCP server is up"))


def test_check_only_sends_get_requests_and_reports_what_is_missing(capsys):
    fake = fresh_trueforge()
    assert register_agent.check(fake.client(), "anthropic/some-model", "http://mcp.test/mcp") is True
    output = capsys.readouterr().out
    assert all(method == "GET" for method, _ in fake.calls)
    assert "model anthropic/some-model is configured" in output
    assert "no sandbox provider configured" in output
    assert "MCP server 'lastday' is not registered yet" in output
    assert "agent 'last-day' does not exist yet" in output


def test_check_fails_when_the_model_is_not_configured(capsys):
    assert register_agent.check(fresh_trueforge().client(), "openai/other", "http://mcp.test/mcp") is False
    assert "model openai/other is not configured; configured models: anthropic/some-model" in capsys.readouterr().out


def test_unexpected_response_body_is_shown_with_the_token_redacted():
    fake = FakeTrueForge({("GET", "/api/v1/agents"): (500, f"upstream failure, header was Bearer {SECRET}")})
    with pytest.raises(register_agent.TrueForgeError) as error:
        register_agent.find_agent(fake.client())
    message = str(error.value)
    assert "GET /api/v1/agents returned an error status (500)" in message
    assert "upstream failure, header was Bearer <LASTDAY_MCP_TOKEN>" in message
    assert SECRET not in message


def test_json_without_data_field_is_shown():
    fake = FakeTrueForge({("GET", "/api/v1/agents"): (200, {"agents": []})})
    with pytest.raises(register_agent.TrueForgeError, match="without a 'data' field:\n{'agents': \\[\\]}"):
        register_agent.find_agent(fake.client())


def test_register_verifies_the_saved_agent_gates_both_tools():
    saved_agent = {"id": "a1", "name": "last-day", "manifest": {"mcp_servers": [{"name": "lastday"}]}}
    fake = FakeTrueForge(
        {
            ("PUT", "/api/v1/settings/mcp-servers"): (200, {"data": {}}),
            ("GET", "/api/v1/mcp-servers/lastday/tools"): (200, {"data": [{"name": t} for t in TOOLS]}),
            ("GET", "/api/v1/agents"): (200, {"data": [saved_agent]}),
            ("PUT", "/api/v1/agents/a1"): (200, {"data": saved_agent}),
        }
    )
    with pytest.raises(register_agent.TrueForgeError, match="the saved agent does not match what was sent"):
        register_agent.register(fake.client(), "anthropic/some-model", "http://mcp.test/mcp", SECRET)
