"""Replay a Code Mode script against the running Last Day MCP server, without TrueForge.

    uv run scripts/run_code_mode.py path/to/script.py

The script sees the same `mcp_client.call_tool(server, tool, body=...)` it gets in the
TrueForge sandbox. Useful to rerun a script the agent wrote (copy it from the session view).
"""

import os
import runpy
import sys

from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from lastday.codemode import install
from lastday.config import ConfigError, require_env


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        token = require_env("LASTDAY_MCP_TOKEN")
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    url = os.environ.get("LASTDAY_MCP_URL", "http://127.0.0.1:8765/mcp")

    def connect() -> Client:
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
        return Client(streamable_http_client(url, http_client=http))

    install(connect)
    runpy.run_path(sys.argv[1], run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
