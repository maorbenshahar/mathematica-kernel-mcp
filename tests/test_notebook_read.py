"""Tests for the notebook_read tool's composable filters and cell payload shape.

In solo mode the backend now goes through Mathematica's own notebook
machinery (Solo* WL primitives + UsingFrontEnd), so the tests here mock
the manager-level WL call rather than exercising any Python-side parser.
"""

import pytest

from mathematica_kernel_mcp.backends import SoloBackend
from mathematica_kernel_mcp.server import notebook_read


def _fake_solo_payload():
    """Shape returned by SharedKernelMCP`SoloReadNotebook."""
    return {
        "status": "ok",
        "path": "/tmp/sample.m",
        "cellIDAssigned": [],
        "cellIDRemapped": [],
        "cells": [
            {"index": 1, "cellID": 1, "style": "Package",
             "label": "", "contentChars": 0, "content": ""},
            {"index": 2, "cellID": 2, "style": "Title",
             "label": "", "contentChars": 26, "content": "Sample Package for Testing"},
            {"index": 3, "cellID": 3, "style": "Section",
             "label": "", "contentChars": 5, "content": "Setup"},
            {"index": 4, "cellID": 4, "style": "Input",
             "label": "", "contentChars": 5, "content": "x = 5"},
            {"index": 5, "cellID": 5, "style": "Input",
             "label": "", "contentChars": 16, "content": "f[n_] := n^2 + x"},
            {"index": 6, "cellID": 6, "style": "Section",
             "label": "", "contentChars": 11, "content": "Computation"},
            {"index": 7, "cellID": 7, "style": "Input",
             "label": "", "contentChars": 24, "content": "Table[f[i], {i, 1, 10}]"},
            {"index": 8, "cellID": 8, "style": "Text",
             "label": "", "contentChars": 42,
             "content": "This computes f for the first 10 integers."},
            {"index": 9, "cellID": 9, "style": "Input",
             "label": "", "contentChars": 31,
             "content": "Total[Table[f[i], {i, 1, 10}]]"},
        ],
    }


class _FakeManager:
    """Stand-in for SessionManager whose evaluate_native returns canned WL output."""

    def __init__(self, payload):
        self._payload = payload

    def evaluate_native(self, code, session_name="main", timeout=30, store_output=True):
        return {"status": "ok", "value": self._payload}


@pytest.fixture
def patched_backend(monkeypatch):
    """Route notebook_read through a SoloBackend whose WL call is mocked."""
    backend = SoloBackend(manager=_FakeManager(_fake_solo_payload()))

    def fake_get_backend(path, manager_factory, timeout=30.0):
        return backend

    monkeypatch.setattr(
        "mathematica_kernel_mcp.server.get_backend_for", fake_get_backend
    )
    return backend


def test_default_read_is_preview_only(patched_backend):
    """D: notebook_read defaults to include_content=False — outline-first."""
    result = notebook_read("/tmp/sample.m")

    assert result["status"] == "ok"
    assert result["cells"], "no cells returned"
    for cell in result["cells"]:
        assert "preview" in cell
        assert "content" not in cell


def test_cells_carry_label_and_content_chars(patched_backend):
    """B: every cell payload has label and contentChars, even in solo."""
    result = notebook_read("/tmp/sample.m", include_content=False)

    for cell in result["cells"]:
        assert "label" in cell
        assert "contentChars" in cell
        assert isinstance(cell["contentChars"], int)
        assert cell["contentChars"] >= len(cell.get("preview", ""))


def test_styles_filter_gives_outline_view(patched_backend):
    """A: styles=[...] yields an outline (just Title/Section cells)."""
    result = notebook_read(
        "/tmp/sample.m", styles=["Title", "Section"], include_content=True
    )

    styles = [c["style"] for c in result["cells"]]
    assert styles == ["Title", "Section", "Section"]
    assert [c["content"] for c in result["cells"]] == [
        "Sample Package for Testing",
        "Setup",
        "Computation",
    ]


def test_start_end_slice(patched_backend):
    """A: start/end gives a 1-indexed positional slice."""
    result = notebook_read("/tmp/sample.m", start=3, end=5)

    indices = [c["index"] for c in result["cells"]]
    assert indices == [3, 4, 5]


def test_around_cell_window(patched_backend):
    """A: around_cell + window returns local context."""
    full = notebook_read("/tmp/sample.m")
    target_id = next(c["cellID"] for c in full["cells"] if c["index"] == 4)

    result = notebook_read("/tmp/sample.m", around_cell=target_id, window=1)
    indices = sorted(c["index"] for c in result["cells"])
    assert indices == [3, 4, 5]


def test_around_cell_unknown_id_returns_error(patched_backend):
    result = notebook_read("/tmp/sample.m", around_cell=9999)
    assert "error" in result
    assert "not found" in result["error"]


def test_explicit_cells_filter(patched_backend):
    """The pre-existing cells=[id1, id2] filter still works."""
    full = notebook_read("/tmp/sample.m")
    first_two = [c["cellID"] for c in full["cells"][:2]]

    result = notebook_read("/tmp/sample.m", cells=first_two)
    assert {c["cellID"] for c in result["cells"]} == set(first_two)


def test_start_with_prior_styles_filter_keeps_high_index_cells(patched_backend):
    """Regression: when `styles=` first shrinks `all_cells`, the `start`
    bound used to be computed as `len(all_cells) + start`, so a Section at
    original index 6 would be wrongly excluded by `start=4`."""
    result = notebook_read(
        "/tmp/sample.m",
        styles=["Title", "Section"],
        start=4,
        include_content=True,
    )

    indices = [c["index"] for c in result["cells"]]
    full = notebook_read(
        "/tmp/sample.m", styles=["Title", "Section"], include_content=True
    )
    expected = [c["index"] for c in full["cells"] if c["index"] >= 4]
    assert indices == expected
