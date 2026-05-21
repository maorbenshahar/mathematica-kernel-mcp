"""Tests for .m and .nb file parsing."""

from textwrap import dedent
from pathlib import Path

from mathematica_kernel_mcp.parser import (
    StaleCellReferenceError,
    create_m_cell,
    parse_m_file,
    update_m_cell,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_m_file_cell_count():
    cells = parse_m_file(FIXTURES / "sample.m")
    # Package, Title, Section(Setup), Input(x=5), Input(f[n_]),
    # Section(Computation), Input(Table), Text, Input(Total)
    assert len(cells) == 9


def test_parse_m_file_cell_types():
    cells = parse_m_file(FIXTURES / "sample.m")
    types = [c.cell_type for c in cells]
    assert types == [
        "Package", "Title", "Section", "Input", "Input",
        "Section", "Input", "Text", "Input",
    ]


def test_parse_m_file_input_content():
    cells = parse_m_file(FIXTURES / "sample.m")
    input_cells = [c for c in cells if c.cell_type == "Input"]
    assert input_cells[0].content == "x = 5"
    assert input_cells[1].content == "f[n_] := n^2 + x"
    assert "Table[f[i]" in input_cells[2].content


def test_parse_m_file_text_content():
    cells = parse_m_file(FIXTURES / "sample.m")
    text_cells = [c for c in cells if c.cell_type == "Text"]
    assert len(text_cells) == 1
    assert "first 10 integers" in text_cells[0].content


def test_parse_m_file_section_content():
    cells = parse_m_file(FIXTURES / "sample.m")
    sections = [c for c in cells if c.cell_type == "Section"]
    assert sections[0].content == "Setup"
    assert sections[1].content == "Computation"


def test_parse_m_file_cell_numbering():
    cells = parse_m_file(FIXTURES / "sample.m")
    numbers = [c.number for c in cells]
    assert numbers == list(range(1, 10))


def test_parse_m_file_returns_source_refs():
    cells = parse_m_file(FIXTURES / "sample.m")

    assert all(cell.cell_id.startswith("src:v1:") for cell in cells)
    assert len({cell.cell_id for cell in cells}) == len(cells)


def test_parse_m_file_ignores_legacy_explicit_cell_ids(tmp_path):
    path = tmp_path / "with_ids.m"
    path.write_text(
        dedent(
            """\
            (* ::Input:: [cell_id=setup] *)
            x = 5

            (* ::Text:: [cell_id=note] *)
            (*hello*)
            """
        )
    )

    cells = parse_m_file(path)
    assert [cell.content for cell in cells] == ["x = 5", "hello"]
    assert all(cell.cell_id.startswith("src:v1:") for cell in cells)


def test_create_m_cell_does_not_persist_cell_ids(tmp_path):
    path = tmp_path / "mutable.m"
    path.write_text(
        dedent(
            """\
            (* ::Input:: *)
            x = 5

            (* ::Input:: *)
            x^2
            """
        )
    )

    created = create_m_cell(path, "Input", "x + 1", after_cell=1)
    cells = parse_m_file(path)

    assert created.cell_id.startswith("src:v1:")
    assert [cell.content for cell in cells] == ["x = 5", "x + 1", "x^2"]
    assert "[cell_id=" not in path.read_text()


def test_parse_m_file_falls_back_for_unmarked_scripts(tmp_path):
    path = tmp_path / "legacy.wl"
    path.write_text(
        dedent(
            """\
            x = 1


            y = x + 1


            (*helper note*)
            """
        )
    )

    cells = parse_m_file(path)

    assert [cell.cell_type for cell in cells] == ["Input", "Input", "Text"]
    assert [cell.content for cell in cells] == ["x = 1", "y = x + 1", "helper note"]
    assert all(cell.cell_id.startswith("src:v1:") for cell in cells)


def test_create_m_cell_bootstraps_markerless_file_into_mutable_cells(tmp_path):
    path = tmp_path / "legacy.m"
    path.write_text(
        dedent(
            """\
            x = 1


            y = x + 1
            """
        )
    )

    created = create_m_cell(path, "Input", "y^2", after_cell=2)
    cells = parse_m_file(path)

    assert [cell.content for cell in cells] == ["x = 1", "y = x + 1", "y^2"]
    assert created.cell_id.startswith("src:v1:")
    assert "[cell_id=" not in path.read_text()


def test_parse_m_file_empty():
    """An empty .m file should produce no cells."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".m", mode="w", delete=False) as f:
        f.write("")
        f.flush()
        cells = parse_m_file(f.name)
    assert cells == []

def test_update_m_cell_accepts_current_source_ref(tmp_path):
    path = tmp_path / "mutable.m"
    path.write_text(
        dedent(
            """\
            (* ::Input:: *)
            x = 5

            (* ::Input:: *)
            x^2
            """
        )
    )
    ref = parse_m_file(path)[1].cell_id

    updated = update_m_cell(path, cell_id=ref, content="x^3")

    assert updated.content == "x^3"
    assert [cell.content for cell in parse_m_file(path)] == ["x = 5", "x^3"]
    assert "[cell_id=" not in path.read_text()


def test_update_m_cell_rejects_stale_source_ref(tmp_path):
    path = tmp_path / "mutable.m"
    path.write_text(
        dedent(
            """\
            (* ::Input:: *)
            x = 5

            (* ::Input:: *)
            x^2
            """
        )
    )
    ref = parse_m_file(path)[1].cell_id
    path.write_text("(* changed outside MCP *)\\n" + path.read_text())

    try:
        update_m_cell(path, cell_id=ref, content="x^3")
    except StaleCellReferenceError as exc:
        payload = exc.to_payload()
    else:
        raise AssertionError("expected StaleCellReferenceError")

    assert payload["status"] == "stale_cell_reference"
    assert payload["cellID"] == ref
    assert "expectedFileHash" in payload
    assert "currentFileHash" in payload
