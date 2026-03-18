"""Subprocess stdio tests against the real MCP server entry point."""

import asyncio
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports.stdio import StdioTransport

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def test_stdio_server_round_trip(tmp_path):
    async def run():
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "mathematica_kernel_mcp"],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            cwd=str(ROOT),
            keep_alive=False,
            log_file=tmp_path / "mathematica-kernel-mcp-stdio.log",
        )

        async with Client(transport) as client:
            tools = await client.list_tools()
            tool_names = {tool.name for tool in tools}
            assert "eval" in tool_names
            assert "run_cells" in tool_names

            outline = await client.call_tool(
                "file_outline",
                {
                    "path": str(FIXTURES / "sample.m"),
                    "include_content": True,
                    "executable_only": True,
                    "cells": [4, 5],
                },
            )
            assert outline.data["cell_count"] == 2

            ran = await client.call_tool(
                "run_cells",
                {"path": str(FIXTURES / "sample.m"), "cells": [4, 5]},
            )
            assert [item["status"] for item in ran.data["results"]] == ["ok", "ok"]

            result = await client.call_tool("eval", {"code": "f[5]"})
            assert result.data["summary"] == "30"
            assert result.data["head"] == "Integer"

            output = await client.call_tool(
                "get_output",
                {"out_number": result.data["out_number"], "view": "full"},
            )
            assert output.data["output"] == "30"

    asyncio.run(run())
