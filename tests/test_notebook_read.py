"""Tests for the notebook_read tool's composable filters and cell payload shape.

Driven against the SoloBackend on a fixture .m file so no live kernel is
needed. The fixture has cells of varied styles (Package/Title/Section/Input/
Text) which exercises style filtering and outline behaviour.
"""

from pathlib import Path

import pytest

from mathematica_kernel_mcp.backends import SoloBackend
from mathematica_kernel_mcp.server import notebook_read


FIXTURE = Path(__file__).parent / "fixtures" / "sample.m"


@pytest.fixture
def patched_backend(monkeypatch):
    """Route notebook_read through a SoloBackend (no real kernel)."""
    backend = SoloBackend(manager=None)

    def fake_get_backend(path, manager_factory, timeout=30.0):
        return backend

    monkeypatch.setattr(
        "mathematica_kernel_mcp.server.get_backend_for", fake_get_backend
    )
    return backend


def test_default_read_is_preview_only(patched_backend):
    """D: notebook_read defaults to include_content=False — outline-first."""
    result = notebook_read(str(FIXTURE))

    assert result["status"] == "ok"
    assert result["cells"], "no cells returned"
    for cell in result["cells"]:
        assert "preview" in cell
        assert "content" not in cell


def test_cells_carry_label_and_content_chars(patched_backend):
    """B: every cell payload has label (empty in solo) and contentChars
    (full length even when only preview is sent)."""
    result = notebook_read(str(FIXTURE), include_content=False)

    for cell in result["cells"]:
        assert "label" in cell
        assert cell["label"] == ""  # solo mode has no front-end label
        assert "contentChars" in cell
        assert isinstance(cell["contentChars"], int)
        # contentChars must reflect the FULL length, not the preview length.
        assert cell["contentChars"] >= len(cell.get("preview", ""))


def test_styles_filter_gives_outline_view(patched_backend):
    """A: styles=[...] yields an outline (just Title/Section cells)."""
    result = notebook_read(
        str(FIXTURE), styles=["Title", "Section"], include_content=True
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
    result = notebook_read(str(FIXTURE), start=3, end=5)

    indices = [c["index"] for c in result["cells"]]
    assert indices == [3, 4, 5]


def test_around_cell_window(patched_backend):
    """A: around_cell + window returns local context."""
    # Pick the cell at index 4 (an Input cell) and ask for +/- 1 neighbor.
    full = notebook_read(str(FIXTURE))
    target_id = next(c["cellID"] for c in full["cells"] if c["index"] == 4)

    result = notebook_read(str(FIXTURE), around_cell=target_id, window=1)
    indices = sorted(c["index"] for c in result["cells"])
    assert indices == [3, 4, 5]


def test_around_cell_unknown_id_returns_error(patched_backend):
    result = notebook_read(str(FIXTURE), around_cell="src:v1:nonexistent")
    assert "error" in result
    assert "not found" in result["error"]


def test_explicit_cells_filter(patched_backend):
    """The pre-existing cells=[id1, id2] filter still works."""
    full = notebook_read(str(FIXTURE))
    first_two = [c["cellID"] for c in full["cells"][:2]]

    result = notebook_read(str(FIXTURE), cells=first_two)
    assert {c["cellID"] for c in result["cells"]} == set(first_two)
