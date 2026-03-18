"""Comprehensive in-process MCP coverage for the current public tool set."""

import asyncio
from pathlib import Path
from uuid import uuid4

from fastmcp import Client

from mathematica_kernel_mcp.server import mcp

FIXTURES = Path(__file__).parent / "fixtures"

EXPECTED_TOOLS = {
    "create_cell",
    "create_cells",
    "delete_cell",
    "documentation_search",
    "eval",
    "get_cell_output",
    "file_outline",
    "get_output",
    "get_output_part",
    "kernel_restart",
    "kernel_state",
    "list_symbols",
    "names",
    "run_cell",
    "run_cells",
    "session_close",
    "session_create",
    "session_list",
    "symbol_info",
    "update_cell",
    "update_cells",
}


def test_all_tools_are_explicitly_exercised_by_name(tmp_path):
    async def run():
        called: set[str] = set()
        sample_path = str(FIXTURES / "sample.m")
        mutable_path = tmp_path / "mutable_sample.m"
        mutable_path.write_text(Path(sample_path).read_text())

        async with Client(mcp) as client:
            tools = await client.list_tools()
            available = {tool.name for tool in tools}
            assert available == EXPECTED_TOOLS

            async def call(name: str, args: dict | None = None) -> dict:
                called.add(name)
                result = await client.call_tool(name, args or {})
                return result.data

            outline = await call(
                "file_outline",
                {
                    "path": sample_path,
                    "include_content": True,
                    "executable_only": True,
                    "cells": [4, 5],
                },
            )
            assert outline["cell_count"] == 2
            assert [cell["cell"] for cell in outline["cells"]] == [4, 5]
            assert [cell["cell_id"] for cell in outline["cells"]] == ["cell-0004", "cell-0005"]
            assert outline["cells"][1]["content"] == "f[n_] := n^2 + x"

            missing_selector = await call("run_cells", {"path": sample_path})
            assert "Explicit cell selection required" in missing_selector["error"]

            run_all = await call("run_cells", {"path": sample_path, "all": True})
            assert run_all["result_count"] == 4
            assert [item["status"] for item in run_all["results"]] == ["ok", "ok", "ok", "ok"]

            run_result = await call("run_cells", {"path": sample_path, "cells": [4, 5]})
            assert [item["status"] for item in run_result["results"]] == ["ok", "ok"]

            basic_eval = await call("eval", {"code": "f[5]"})
            assert basic_eval["in_number"] == basic_eval["out_number"]
            assert basic_eval["summary"] == "30"
            assert basic_eval["head"] == "Integer"

            full_output = await call(
                "get_output",
                {"out_number": basic_eval["out_number"], "view": "full"},
            )
            assert full_output["output"] == "30"

            summary_view = await call(
                "get_output",
                {"out_number": basic_eval["out_number"], "view": "summary"},
            )
            assert summary_view["summary"]["head"] == "Integer"

            short_view = await call(
                "get_output",
                {"out_number": basic_eval["out_number"], "view": "short"},
            )
            assert short_view["output"] == "30"

            list_eval = await call("eval", {"code": "{10, 20, 30}"})
            part = await call(
                "get_output_part",
                {"out_number": list_eval["out_number"], "part_spec": "Part[%, 2]"},
            )
            assert part["result"] == "20"

            mutable_outline = await call(
                "file_outline",
                {"path": str(mutable_path), "include_content": True, "cells": [4, 5]},
            )
            updated = await call(
                "update_cell",
                {
                    "path": str(mutable_path),
                    "cell_id": mutable_outline["cells"][1]["cell_id"],
                    "content": "f[n_] := n^2 + x + 1",
                },
            )
            assert updated["cell"]["content"] == "f[n_] := n^2 + x + 1"

            created = await call(
                "create_cell",
                {
                    "path": str(mutable_path),
                    "cell_type": "Input",
                    "content": "f[2]",
                    "after_cell_id": mutable_outline["cells"][1]["cell_id"],
                },
            )
            created_id = created["cell"]["cell_id"]

            run_single = await call(
                "run_cell",
                {"path": str(mutable_path), "cell_id": created_id},
            )
            assert run_single["status"] == "ok"

            cell_output = await call(
                "get_cell_output",
                {"path": str(mutable_path), "cell_id": created_id, "view": "full"},
            )
            assert cell_output["output"] == run_single["summary"]

            deleted = await call(
                "delete_cell",
                {"path": str(mutable_path), "cell_id": created_id},
            )
            assert deleted["deleted"]["cell_id"] == created_id

            bulk_path = tmp_path / "bulk_scaffold.nb"
            bulk_created = await call(
                "create_cells",
                {
                    "path": str(bulk_path),
                    "cells": [
                        {"cell_type": "Input", "content": "a = 5"},
                        {"cell_type": "Input", "content": "a^2"},
                    ],
                },
            )
            assert bulk_created["created_count"] == 2
            bulk_first_id = bulk_created["results"][0]["cell"]["cell_id"]
            bulk_second_id = bulk_created["results"][1]["cell"]["cell_id"]

            bulk_updated = await call(
                "update_cells",
                {
                    "path": str(bulk_path),
                    "updates": [
                        {"cell_id": bulk_first_id, "content": "a = 6"},
                        {"cell_id": bulk_second_id, "content": "a^3"},
                    ],
                },
            )
            assert bulk_updated["updated_count"] == 2

            bulk_run = await call(
                "run_cells",
                {"path": str(bulk_path), "all": True, "persist_output": True},
            )
            assert len(bulk_run["synced_outputs"]) == 2

            info = await call(
                "symbol_info", {"name": "Sin", "fields": ["usage", "attributes"]}
            )
            assert info["info"]["usage"] == "Sin[z] gives the sine of z."
            assert "Protected" in info["info"]["attributes"]

            plot_info = await call(
                "symbol_info", {"name": "Plot", "fields": ["usage"]}
            )
            assert "..." in plot_info["info"]["usage"]
            assert "∈" not in plot_info["info"]["usage"]

            globals_info = await call("list_symbols", {})
            assert any(symbol.endswith("x") for symbol in globals_info["symbols"])
            assert any(symbol.endswith("f") for symbol in globals_info["symbols"])

            x_info = await call(
                "symbol_info", {"name": "x", "fields": ["context", "own_values"]}
            )
            assert x_info["info"]["context"] == "Global`"
            assert "x" in x_info["info"]["own_values"]

            f_info = await call(
                "symbol_info",
                {"name": "f", "fields": ["context", "down_values", "messages"]},
            )
            assert f_info["info"]["context"] == "Global`"
            assert "f[n_]" in f_info["info"]["down_values"]
            assert f_info["info"]["messages"] == "None"

            matches = await call("names", {"pattern": "Sin*"})
            assert "Sin" in matches["matches"]

            natural_docs = await call(
                "documentation_search",
                {"query": "integrate symbolic function", "max_results": 10},
            )
            assert any(result["symbol"] == "Integrate" for result in natural_docs["results"])

            state = await call("kernel_state", {})
            assert state["state"]["context"] == "Global`"
            assert "packages" not in state["state"]
            assert "context_path" not in state["state"]

            verbose_state = await call(
                "kernel_state", {"fields": ["context", "packages", "context_path"]}
            )
            assert verbose_state["state"]["context"] == "Global`"
            assert "Global`" in verbose_state["state"]["packages"]
            assert "Global`" in verbose_state["state"]["context_path"]

            worker = f"toolcov_{uuid4().hex[:8]}"
            created = await call("session_create", {"name": worker})
            assert created["created"] == worker

            sessions = await call("session_list", {})
            names_list = {session["name"] for session in sessions["sessions"]}
            assert worker in names_list
            assert "main" in names_list

            worker_eval = await call("eval", {"code": "workerValue = 7", "session": worker})
            assert worker_eval["head"] == "Integer"

            restarted = await call("kernel_restart", {})
            assert restarted["restarted"] == "main"

            after_restart = await call("eval", {"code": "1 + 1"})
            assert after_restart["summary"] == "2"

            closed = await call("session_close", {"name": worker})
            assert closed["closed"] == worker

            assert called == EXPECTED_TOOLS

    asyncio.run(run())
