"""Tests for .m and .nb file parsing."""

from textwrap import dedent
from pathlib import Path

from mathematica_kernel_mcp.parser import create_m_cell, parse_m_file

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


def test_parse_m_file_assigns_fallback_cell_ids():
    cells = parse_m_file(FIXTURES / "sample.m")
    assert [cell.cell_id for cell in cells] == [f"cell-{index:04d}" for index in range(1, 10)]


def test_parse_m_file_reads_explicit_cell_ids(tmp_path):
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
    assert [cell.cell_id for cell in cells] == ["setup", "note"]
    assert cells[1].content == "hello"


def test_create_m_cell_persists_stable_ids_for_legacy_files(tmp_path):
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

    assert created.cell_id.startswith("cell-")
    assert created.cell_id not in {"cell-0001", "cell-0002"}
    assert [cell.cell_id for cell in cells] == ["cell-0001", created.cell_id, "cell-0002"]
    assert "[cell_id=cell-0001]" in path.read_text()


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
    assert [cell.cell_id for cell in cells] == ["cell-0001", "cell-0002", "cell-0003"]


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
    assert created.cell_id.startswith("cell-")
    assert "[cell_id=cell-0001]" in path.read_text()


def test_parse_m_file_empty():
    """An empty .m file should produce no cells."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".m", mode="w", delete=False) as f:
        f.write("")
        f.flush()
        cells = parse_m_file(f.name)
    assert cells == []
