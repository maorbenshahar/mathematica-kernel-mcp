"""Regression tests for the perturbative and numerical example workflows."""

import asyncio
from pathlib import Path

from fastmcp import Client

from mathematica_kernel_mcp.server import mcp

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_perturbative_workflow_runs_through_mcp():
    async def run():
        path = str(EXAMPLES / "dim_reg_ode_perturbation.m")

        async with Client(mcp) as client:
            outline = await client.call_tool("file_outline", {"path": path})
            assert outline.data["cell_count"] == 21

            run = await client.call_tool(
                "run_cells",
                {"path": path, "cells": [5, 6, 10, 14, 12, 18, 21], "timeout": 60},
            )
            results = {item["cell"]: item for item in run.data["results"]}

            assert results[12]["head"] == "Function"
            assert results[21]["head"] == "List"

            sol0_output = await client.call_tool(
                "get_output",
                {"out_number": results[12]["out_number"], "view": "full", "max_chars": 4000},
            )
            assert "WhittakerM" in sol0_output.data["output"]
            assert "WhittakerW" in sol0_output.data["output"]

    asyncio.run(run())


def test_numerical_workflow_runs_through_mcp():
    async def run():
        path = str(EXAMPLES / "dim_reg_ode_numerics.m")

        async with Client(mcp) as client:
            outline = await client.call_tool("file_outline", {"path": path})
            assert outline.data["cell_count"] == 22

            sequence = [5, 6, 8, 9, 11, 12, 13, 15, 16, 18, 19, 20, 21, 22]
            run = await client.call_tool(
                "run_cells", {"path": path, "cells": sequence, "timeout": 60}
            )
            results = {item["cell"]: item for item in run.data["results"]}

            assert results[20]["head"] == "List"
            assert results[21]["head"] == "Association"
            assert results[22]["head"] == "List"

            stability_output = await client.call_tool(
                "get_output",
                {"out_number": results[21]["out_number"], "view": "full", "max_chars": 4000},
            )
            assert "maxProfileDiff_r0" in stability_output.data["output"]
            assert "maxProfileDiff_method" in stability_output.data["output"]

            sweep_output = await client.call_tool(
                "get_output",
                {"out_number": results[22]["out_number"], "view": "full", "max_chars": 4000},
            )
            assert "{0.49" in sweep_output.data["output"]

    asyncio.run(run())
