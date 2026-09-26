"""A Code Mode scan, run through the same `mcp_client` interface as the sandbox, finds both
kinds of planted dependency from the read tools alone."""

import asyncio
import base64
import json
import runpy
import sys
from pathlib import Path

import pytest
from mcp import Client

from lastday.codemode import ToolCallError, install
from lastday.server import build_server
from lastday.store import PlanStore
from tests.fake_org import ORG, REPO, fake_org, scope_for

SCAN_SCRIPT = Path(__file__).parent / "code_mode" / "scan_dependencies.py"
WORKFLOW = ".github/workflows/deploy.yml"


def org_with_actor_gated_workflow():
    workflow = "on: workflow_dispatch\njobs:\n  deploy:\n    if: github.actor == 'leaver'\n"
    return (
        fake_org()
        .on(
            "GET",
            f"/orgs/{ORG}/repos",
            [
                {"name": REPO, "default_branch": "main", "topics": ["lastday-demo"]},
                {"name": "ops", "default_branch": "main", "topics": ["lastday-demo"]},
                {"name": "private", "default_branch": "main", "topics": []},
            ],
        )
        .on("GET", f"/repos/{ORG}/ops/git/trees/main", {"tree": [{"path": WORKFLOW, "type": "blob", "size": 80}]})
        .on("GET", f"/repos/{ORG}/ops/contents/{WORKFLOW}", {"content": base64.b64encode(workflow.encode()).decode(), "sha": "w"})
        .on("GET", f"/repos/{ORG}/ops/collaborators", [])
        .on("GET", f"/repos/{ORG}/ops/pulls", [])
        .on("GET", f"/repos/{ORG}/ops/teams", [])
    )


@pytest.fixture
def connected(tmp_path):
    fake = org_with_actor_gated_workflow()
    server = build_server(fake.client(), scope_for(fake), PlanStore(tmp_path))
    install(lambda: Client(server))
    yield fake
    sys.modules.pop("mcp_client", None)


def test_scan_script_finds_codeowner_and_actor_gated_workflow(connected, capsys):
    runpy.run_path(str(SCAN_SCRIPT), run_name="__main__")
    findings = json.loads(capsys.readouterr().out)
    assert {"kind": "codeowner", "repo": REPO, "path": ".github/CODEOWNERS", "line": 2} in findings
    assert {"kind": "file_reference", "repo": "ops", "path": WORKFLOW, "line": 4} in findings
    assert {"kind": "direct_access", "repo": REPO, "role": "admin"} in findings
    assert not any(f["repo"] == "private" for f in findings)
    assert connected.writes() == []


def test_tool_errors_reach_the_script_as_exceptions(connected):
    from mcp_client import call_tool

    with pytest.raises(RuntimeError, match=r"^MCP tool error \(server=lastday, tool=get_repo_snapshot\): .*out of scope"):
        asyncio.run(call_tool("lastday", "get_repo_snapshot", {"repo": "private"}))
