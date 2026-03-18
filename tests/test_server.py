"""Basic MCP integration tests for the slimmed tool surface."""

import asyncio
from pathlib import Path
from textwrap import dedent

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


def test_mcp_lists_expected_tools():
    async def run():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            tool_names = {tool.name for tool in tools}
            assert tool_names == EXPECTED_TOOLS

    asyncio.run(run())


def test_mcp_workflow_run_cells_and_output_views():
    async def run():
        async with Client(mcp) as client:
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
            assert outline.data["cells"][0]["content"] == "x = 5"
            assert outline.data["cells"][0]["cell_id"] == "cell-0004"

            ran = await client.call_tool(
                "run_cells",
                {"path": str(FIXTURES / "sample.m"), "cells": [4, 5]},
            )
            assert [item["status"] for item in ran.data["results"]] == ["ok", "ok"]
            assert ran.data["results"][0]["cell_id"] == "cell-0004"
            assert ran.data["results"][0]["in_number"] == ran.data["results"][0]["out_number"]

            value = await client.call_tool("eval", {"code": "f[5]"})
            assert value.data["in_number"] == value.data["out_number"]
            assert value.data["summary"] == "30"

            full = await client.call_tool(
                "get_output",
                {"out_number": value.data["out_number"], "view": "full"},
            )
            assert full.data["output"] == "30"

            summary = await client.call_tool(
                "get_output",
                {"out_number": value.data["out_number"], "view": "summary"},
            )
            assert summary.data["summary"]["head"] == "Integer"

            short = await client.call_tool(
                "get_output",
                {"out_number": value.data["out_number"], "view": "short"},
            )
            assert short.data["output"] == "30"

            sessions = await client.call_tool("session_list", {})
            session_names = {session["name"] for session in sessions.data["sessions"]}
            assert "main" in session_names

            global_symbols = await client.call_tool("list_symbols", {})
            assert any(symbol.endswith("x") for symbol in global_symbols.data["symbols"])
            assert any(symbol.endswith("f") for symbol in global_symbols.data["symbols"])

            x_info = await client.call_tool(
                "symbol_info",
                {"name": "x", "fields": ["context", "own_values"]},
            )
            assert x_info.data["info"]["context"] == "Global`"
            assert "x" in x_info.data["info"]["own_values"]

            f_info = await client.call_tool(
                "symbol_info",
                {"name": "f", "fields": ["context", "down_values", "messages"]},
            )
            assert f_info.data["info"]["context"] == "Global`"
            assert "f[n_]" in f_info.data["info"]["down_values"]
            assert f_info.data["info"]["messages"] == "None"

            sin_info = await client.call_tool(
                "symbol_info",
                {"name": "Sin", "fields": ["usage"]},
            )
            assert sin_info.data["info"]["usage"] == "Sin[z] gives the sine of z."

            plot_info = await client.call_tool(
                "symbol_info",
                {"name": "Plot", "fields": ["usage"]},
            )
            assert "..." in plot_info.data["info"]["usage"]
            assert " in " in plot_info.data["info"]["usage"]
            assert "…" not in plot_info.data["info"]["usage"]
            assert "∈" not in plot_info.data["info"]["usage"]

            natural_docs = await client.call_tool(
                "documentation_search",
                {"query": "integrate symbolic function", "max_results": 10},
            )
            assert any(result["symbol"] == "Integrate" for result in natural_docs.data["results"])

    asyncio.run(run())


def test_mcp_edit_run_inspect_workflow(tmp_path):
    async def run():
        path = tmp_path / "mutable_workflow.m"
        path.write_text(
            dedent(
                """\
                (* ::Input:: *)
                x = 10

                (* ::Input:: *)
                x^2
                """
            )
        )

        async with Client(mcp) as client:
            outline = await client.call_tool(
                "file_outline",
                {"path": str(path), "include_content": True},
            )
            first_cell, second_cell = outline.data["cells"]
            assert first_cell["cell_id"] == "cell-0001"
            assert second_cell["cell_id"] == "cell-0002"

            updated = await client.call_tool(
                "update_cell",
                {
                    "path": str(path),
                    "cell_id": second_cell["cell_id"],
                    "content": "x^3",
                },
            )
            assert updated.data["cell"]["content"] == "x^3"

            first_run = await client.call_tool(
                "run_cell",
                {"path": str(path), "cell_id": first_cell["cell_id"]},
            )
            assert first_run.data["status"] == "ok"
            assert first_run.data["summary"] == "10"

            second_run = await client.call_tool(
                "run_cell",
                {"path": str(path), "cell_id": second_cell["cell_id"]},
            )
            assert second_run.data["status"] == "ok"
            assert second_run.data["summary"] == "1000"
            assert second_run.data["in_number"] == second_run.data["out_number"]

            output = await client.call_tool(
                "get_output",
                {"out_number": second_run.data["out_number"], "view": "full"},
            )
            assert output.data["output"] == "1000"

            cell_output = await client.call_tool(
                "get_cell_output",
                {"path": str(path), "cell_id": second_cell["cell_id"], "view": "full"},
            )
            assert cell_output.data["output"] == "1000"
            assert cell_output.data["last_out"] == second_run.data["out_number"]

            inspected = await client.call_tool(
                "file_outline",
                {"path": str(path), "include_content": True},
            )
            inspected_second = inspected.data["cells"][1]
            assert inspected_second["last_in"] == second_run.data["in_number"]
            assert inspected_second["last_out"] == second_run.data["out_number"]
            assert inspected_second["last_run_at"] is not None

            created = await client.call_tool(
                "create_cell",
                {
                    "path": str(path),
                    "cell_type": "Input",
                    "content": "x + 1",
                    "after_cell_id": second_cell["cell_id"],
                },
            )
            created_id = created.data["cell"]["cell_id"]
            assert created_id.startswith("cell-")
            assert created_id not in {"cell-0001", "cell-0002"}

            created_run = await client.call_tool(
                "run_cell",
                {"path": str(path), "cell_id": created_id},
            )
            assert created_run.data["summary"] == "11"

            deleted = await client.call_tool(
                "delete_cell",
                {"path": str(path), "cell_id": created_id},
            )
            assert deleted.data["deleted"]["cell_id"] == created_id

            final_outline = await client.call_tool("file_outline", {"path": str(path)})
            assert final_outline.data["cell_count"] == 2
            assert "[cell_id=cell-0001]" in path.read_text()

    asyncio.run(run())


def test_mcp_bulk_edit_workflow_and_output_lookup(tmp_path):
    async def run():
        path = tmp_path / "bulk_workflow.wl"

        async with Client(mcp) as client:
            created = await client.call_tool(
                "create_cells",
                {
                    "path": str(path),
                    "cells": [
                        {"cell_type": "Input", "content": "x = 3"},
                        {"cell_type": "Input", "content": "x^2"},
                    ],
                },
            )
            assert created.data["created_count"] == 2
            first_id = created.data["results"][0]["cell"]["cell_id"]
            second_id = created.data["results"][1]["cell"]["cell_id"]

            updated = await client.call_tool(
                "update_cells",
                {
                    "path": str(path),
                    "updates": [
                        {"cell_id": first_id, "content": "x = 4"},
                        {"cell_id": second_id, "content": "x^3"},
                    ],
                },
            )
            assert updated.data["updated_count"] == 2
            assert updated.data["results"][1]["cell"]["content"] == "x^3"

            run_result = await client.call_tool(
                "run_cells",
                {"path": str(path), "all": True},
            )
            assert [item["status"] for item in run_result.data["results"]] == ["ok", "ok"]
            assert run_result.data["results"][1]["summary"] == "64"

            output = await client.call_tool(
                "get_cell_output",
                {"path": str(path), "cell_id": second_id, "view": "full"},
            )
            assert output.data["output"] == "64"
            assert "[cell_id=" in path.read_text()

    asyncio.run(run())


def test_mcp_update_cell_clears_runtime_metadata_for_legacy_and_notebook(tmp_path):
    async def run():
        legacy_path = tmp_path / "legacy_runtime.wl"
        legacy_path.write_text(
            dedent(
                """\
                x = 10


                x^2
                """
            )
        )
        notebook_path = tmp_path / "runtime_clear.nb"
        notebook_path.write_text('Notebook[{Cell["x = 10", "Input"], Cell["x^2", "Input"]}]\n')

        async with Client(mcp) as client:
            legacy_run = await client.call_tool(
                "run_cells",
                {"path": str(legacy_path), "all": True},
            )
            assert legacy_run.data["results"][1]["summary"] == "100"

            legacy_outline = await client.call_tool(
                "file_outline",
                {"path": str(legacy_path), "include_content": True},
            )
            legacy_second = legacy_outline.data["cells"][1]
            assert legacy_second["last_out"] is not None

            await client.call_tool(
                "update_cell",
                {
                    "path": str(legacy_path),
                    "cell_id": legacy_second["cell_id"],
                    "content": "x^3",
                },
            )

            legacy_after = await client.call_tool(
                "file_outline",
                {"path": str(legacy_path), "include_content": True},
            )
            legacy_second_after = legacy_after.data["cells"][1]
            assert legacy_second_after["last_in"] is None
            assert legacy_second_after["last_out"] is None
            assert legacy_second_after["last_run_at"] is None

            notebook_run = await client.call_tool(
                "run_cells",
                {"path": str(notebook_path), "all": True},
            )
            assert notebook_run.data["results"][1]["summary"] == "100"

            notebook_outline = await client.call_tool(
                "file_outline",
                {"path": str(notebook_path), "include_content": True},
            )
            notebook_second = notebook_outline.data["cells"][1]
            assert notebook_second["cell_id"] == "Index:2"
            assert notebook_second["last_out"] is not None

            updated = await client.call_tool(
                "update_cell",
                {
                    "path": str(notebook_path),
                    "cell_id": notebook_second["cell_id"],
                    "content": "x^3",
                },
            )
            assert updated.data["cell"]["cell_id"] == "CellID:2"

            notebook_after = await client.call_tool(
                "file_outline",
                {"path": str(notebook_path), "include_content": True},
            )
            notebook_second_after = notebook_after.data["cells"][1]
            assert notebook_second_after["cell_id"] == "CellID:2"
            assert notebook_second_after["last_in"] is None
            assert notebook_second_after["last_out"] is None
            assert notebook_second_after["last_run_at"] is None

    asyncio.run(run())


def test_mcp_nb_edit_run_inspect_workflow(tmp_path):
    async def run():
        path = tmp_path / "mutable_workflow.nb"
        path.write_text('Notebook[{Cell["x = 10", "Input"], Cell["x * x", "Input"]}]\n')

        async with Client(mcp) as client:
            outline = await client.call_tool(
                "file_outline",
                {"path": str(path), "include_content": True},
            )
            assert outline.data["cell_count"] == 2
            assert [cell["cell_id"] for cell in outline.data["cells"]] == ["Index:1", "Index:2"]

            updated = await client.call_tool(
                "update_cell",
                {
                    "path": str(path),
                    "cell_id": "Index:2",
                    "content": "x * x * x",
                },
            )
            updated_id = updated.data["cell"]["cell_id"]
            assert updated_id == "CellID:2"
            assert updated.data["cell"]["content"] == "x * x * x"

            rerendered = await client.call_tool(
                "file_outline",
                {"path": str(path), "include_content": True},
            )
            assert [cell["cell_id"] for cell in rerendered.data["cells"]] == [
                "CellID:1",
                "CellID:2",
            ]

            first_run = await client.call_tool(
                "run_cell",
                {"path": str(path), "cell_id": "CellID:1"},
            )
            assert first_run.data["summary"] == "10"

            second_run = await client.call_tool(
                "run_cell",
                {"path": str(path), "cell_id": updated_id},
            )
            assert second_run.data["summary"] == "1000"

            created = await client.call_tool(
                "create_cell",
                {
                    "path": str(path),
                    "cell_type": "Input",
                    "content": "x + 1",
                    "after_cell_id": updated_id,
                },
            )
            created_id = created.data["cell"]["cell_id"]
            assert created_id == "CellID:3"

            created_run = await client.call_tool(
                "run_cell",
                {"path": str(path), "cell_id": created_id},
            )
            assert created_run.data["summary"] == "11"

            inspected = await client.call_tool(
                "file_outline",
                {"path": str(path), "include_content": True},
            )
            inspected_created = inspected.data["cells"][2]
            assert inspected_created["cell_id"] == created_id
            assert inspected_created["last_out"] == created_run.data["out_number"]

            deleted = await client.call_tool(
                "delete_cell",
                {"path": str(path), "cell_id": created_id},
            )
            assert deleted.data["deleted"]["cell_id"] == created_id

            final_outline = await client.call_tool("file_outline", {"path": str(path)})
            assert final_outline.data["cell_count"] == 2
            assert "CellID->1" in path.read_text()

    asyncio.run(run())


def test_mcp_notebook_run_can_persist_outputs(tmp_path):
    async def run():
        path = tmp_path / "persist_outputs.nb"
        path.write_text('Notebook[{Cell["x = 10", "Input"], Cell["x^2", "Input"]}]\n')

        async with Client(mcp) as client:
            initial_run = await client.call_tool(
                "run_cells",
                {"path": str(path), "all": True, "persist_output": True},
            )
            assert [item["summary"] for item in initial_run.data["results"]] == ["10", "100"]
            assert len(initial_run.data["synced_outputs"]) == 2

            outlined = await client.call_tool(
                "file_outline",
                {"path": str(path), "include_content": True},
            )
            assert [cell["type"] for cell in outlined.data["cells"]] == [
                "Input",
                "Output",
                "Input",
                "Output",
            ]
            assert outlined.data["cells"][1]["content"] == "10"
            assert outlined.data["cells"][3]["content"] == "100"

            await client.call_tool(
                "update_cell",
                {"path": str(path), "cell_id": "CellID:1", "content": "x = 11"},
            )

            rerun = await client.call_tool(
                "run_cells",
                {"path": str(path), "cells": [1, 3], "persist_output": True},
            )
            assert [item["summary"] for item in rerun.data["results"]] == ["11", "121"]
            assert len(rerun.data["synced_outputs"]) == 2

            rerendered = await client.call_tool(
                "file_outline",
                {"path": str(path), "include_content": True},
            )
            assert rerendered.data["cell_count"] == 4
            assert rerendered.data["cells"][1]["content"] == "11"
            assert rerendered.data["cells"][3]["content"] == "121"
            assert "WolframMCPOutputFor:CellID:1" in path.read_text()

    asyncio.run(run())


def test_mcp_create_cell_bootstraps_missing_notebook(tmp_path):
    async def run():
        path = tmp_path / "new_notebook.nb"

        async with Client(mcp) as client:
            created = await client.call_tool(
                "create_cell",
                {
                    "path": str(path),
                    "cell_type": "Input",
                    "content": "40 + 2",
                },
            )
            created_id = created.data["cell"]["cell_id"]
            assert created_id == "CellID:1"
            assert path.exists()

            run_result = await client.call_tool(
                "run_cell",
                {"path": str(path), "cell_id": created_id},
            )
            assert run_result.data["summary"] == "42"

            output = await client.call_tool(
                "get_cell_output",
                {"path": str(path), "cell_id": created_id, "view": "full"},
            )
            assert output.data["output"] == "42"
            assert "CellID->1" in path.read_text()

    asyncio.run(run())


def test_mcp_run_cells_stops_on_semantic_errors(tmp_path):
    async def run():
        path = tmp_path / "semantic_errors.m"
        path.write_text(
            dedent(
                """\
                (* ::Input:: *)
                1/0

                (* ::Input:: *)
                Pause[2]

                (* ::Input:: *)
                2 + 2
                """
            )
        )

        async with Client(mcp) as client:
            message_error = await client.call_tool(
                "run_cells",
                {
                    "path": str(path),
                    "all": True,
                    "fail_on_messages": True,
                },
            )
            assert message_error.data["stopped_early"] is True
            assert len(message_error.data["results"]) == 1
            assert message_error.data["results"][0]["status"] == "error"
            assert "messages" in message_error.data["results"][0]["error"]

            aborted = await client.call_tool(
                "run_cells",
                {
                    "path": str(path),
                    "cells": [2, 3],
                    "timeout": 1,
                },
            )
            assert aborted.data["stopped_early"] is True
            assert len(aborted.data["results"]) == 1
            assert aborted.data["results"][0]["status"] == "error"
            assert aborted.data["results"][0]["head"] == "$Aborted"

    asyncio.run(run())
