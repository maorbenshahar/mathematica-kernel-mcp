"""Real workflow tests using a non-trivial dimensional-regularization ODE."""

import asyncio
from pathlib import Path

from fastmcp import Client

from mathematica_kernel_mcp.server import mcp

FIXTURES = Path(__file__).parent / "fixtures"


def test_dimensional_regularization_m_file_workflow():
    async def run():
        path = str(FIXTURES / "dim_reg_ode.m")

        async with Client(mcp) as client:
            outline = await client.call_tool("file_outline", {"path": path})
            assert outline.data["cell_count"] == 13

            eqn_cell = await client.call_tool(
                "file_outline", {"path": path, "cells": [6], "include_content": True}
            )
            assert eqn_cell.data["cell_count"] == 1
            eqn_cell = eqn_cell.data["cells"][0]
            assert "v''[r]" in eqn_cell["content"]
            assert "r^(1 - 2 e)" in eqn_cell["content"]

            run = await client.call_tool(
                "run_cells",
                {"path": path, "cells": [5, 6, 8, 10, 12, 11, 13], "timeout": 60},
            )
            results = {item["cell"]: item for item in run.data["results"]}

            assert results[6]["head"] == "Equal"
            assert results[8]["head"] == "DSolveValue"
            assert results[10]["head"] == "Function"
            assert results[11]["head"] == "Function"
            assert results[12]["summary"] == "True"
            assert results[13]["summary"] == "True"

            general_output = await client.call_tool(
                "get_output",
                {"out_number": results[8]["out_number"], "view": "full", "max_chars": 2000},
            )
            assert "DSolveValue" in general_output.data["output"]

            e0_output = await client.call_tool(
                "get_output",
                {"out_number": results[10]["out_number"], "view": "full", "max_chars": 4000},
            )
            assert "WhittakerM" in e0_output.data["output"]
            assert "WhittakerW" in e0_output.data["output"]

    asyncio.run(run())


def test_dimensional_regularization_nb_file_workflow():
    async def run():
        path = str(FIXTURES / "dim_reg_ode.nb")

        async with Client(mcp) as client:
            outline = await client.call_tool("file_outline", {"path": path})
            assert outline.data["cell_count"] == 7

            eqn_cell = await client.call_tool(
                "file_outline", {"path": path, "cells": [4], "include_content": True}
            )
            assert eqn_cell.data["cell_count"] == 1
            eqn_cell = eqn_cell.data["cells"][0]
            assert "v'' @ r" in eqn_cell["content"]

            run = await client.call_tool(
                "run_cells", {"path": path, "cells": [3, 4, 5, 6, 7], "timeout": 60}
            )
            results = {item["cell"]: item for item in run.data["results"]}

            assert results[4]["head"] == "Equal"
            assert results[5]["head"] == "DSolveValue"
            assert results[6]["head"] == "Function"
            assert results[7]["summary"] == "True"

            special_output = await client.call_tool(
                "get_output",
                {"out_number": results[6]["out_number"], "view": "full", "max_chars": 4000},
            )
            assert "BesselJ" in special_output.data["output"]
            assert "BesselY" in special_output.data["output"]

    asyncio.run(run())
