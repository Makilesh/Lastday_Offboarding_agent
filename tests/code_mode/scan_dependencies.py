"""A scan of the kind the agent writes in Code Mode: fetch every in-scope snapshot in parallel,
then find each line and grant that depends on the member. Used to test the tool contract."""

import asyncio
import json
import re

from mcp_client import call_tool

MEMBER = "leaver"
NAMES_MEMBER = re.compile(rf"(?<![A-Za-z0-9-])@?{re.escape(MEMBER)}(?![A-Za-z0-9-])", re.IGNORECASE)


async def main():
    scoped = await call_tool("lastday", "list_scoped_repos", {})
    snapshots = await asyncio.gather(
        *(call_tool("lastday", "get_repo_snapshot", {"repo": repo["name"]}) for repo in scoped["repos"])
    )
    findings = []
    for snapshot in snapshots:
        for file in snapshot["files"]:
            for number, line in enumerate(file["text"].splitlines(), start=1):
                if not line.strip().startswith("#") and NAMES_MEMBER.search(line):
                    kind = "codeowner" if file["path"] == snapshot["active_codeowners_path"] else "file_reference"
                    findings.append({"kind": kind, "repo": snapshot["repo"], "path": file["path"], "line": number})
        for collaborator in snapshot["direct_collaborators"]:
            if collaborator["login"].lower() == MEMBER and collaborator["role"] in ("admin", "maintain"):
                findings.append({"kind": "direct_access", "repo": snapshot["repo"], "role": collaborator["role"]})
    print(json.dumps(findings))


asyncio.run(main())
