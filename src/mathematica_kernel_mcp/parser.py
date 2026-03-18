"""Parse .m and .nb files into unified Cell models."""

import re
from pathlib import Path
from uuid import uuid4

from mathematica_kernel_mcp.models import Cell

# Pattern matching Mathematica .m cell markers:
# (* ::CellType:: *)
# (* ::CellType:: [cell_id=cell-0001] *)
CELL_MARKER_RE = re.compile(
    r"^\(\*\s*::(?P<cell_type>\w+)::(?:\s*\[cell_id=(?P<cell_id>[-\w]+)\])?\s*\*\)$"
)

# Pattern for cell content wrapped in (* ... *)
COMMENT_CONTENT_RE = re.compile(r"^\(\*(.+)\*\)$", re.DOTALL)
EXECUTABLE_CELL_TYPES = {"Input", "Code"}
MUTABLE_EXTENSIONS = {".m", ".wl"}


def _default_cell_id(number: int) -> str:
    return f"cell-{number:04d}"


def _normalize_cell_content(cell_type: str, content: str) -> str:
    normalized = content.strip()
    if cell_type not in EXECUTABLE_CELL_TYPES:
        match = COMMENT_CONTENT_RE.match(normalized)
        if match:
            normalized = match.group(1).strip()
    return normalized


def _unmarked_cell_type(content: str) -> str:
    stripped = content.strip()
    if stripped.startswith("(*") and stripped.endswith("*)"):
        return "Text"
    return "Input"


def _next_generated_cell_id(cells: list[Cell]) -> str:
    existing_ids = {cell.cell_id for cell in cells}
    while True:
        candidate = f"cell-{uuid4().hex[:8]}"
        if candidate not in existing_ids:
            return candidate


def _validate_mutable_path(path: Path) -> None:
    if path.suffix.lower() not in MUTABLE_EXTENSIONS:
        raise ValueError("Cell mutation is currently only supported for .m and .wl files")


def _resolve_cell(
    cells: list[Cell],
    cell_id: str | None = None,
    cell_number: int | None = None,
) -> tuple[int, Cell]:
    if cell_id is not None and cell_number is not None:
        raise ValueError("Use either `cell_id` or `cell`, not both")
    if cell_id is None and cell_number is None:
        raise ValueError("Provide either `cell_id` or `cell`")

    if cell_id is not None:
        for index, cell in enumerate(cells):
            if cell.cell_id == cell_id:
                return index, cell
        raise ValueError(f"Cell '{cell_id}' not found")

    assert cell_number is not None
    if cell_number < 1 or cell_number > len(cells):
        raise ValueError(f"Cell {cell_number} out of range (file has {len(cells)} cells)")
    index = cell_number - 1
    return index, cells[index]


def _render_cell(cell: Cell) -> str:
    marker = f"(* ::{cell.cell_type}:: [cell_id={cell.cell_id}] *)"
    content = cell.content.strip()
    if cell.cell_type in EXECUTABLE_CELL_TYPES or not content:
        body = content
    else:
        body = f"(*{content}*)"
    return f"{marker}\n{body}"


def write_m_file(path: str | Path, cells: list[Cell]) -> None:
    """Serialize cells back to a mutable .m/.wl file."""
    path = Path(path)
    _validate_mutable_path(path)
    rendered_cells = []
    for number, cell in enumerate(cells, start=1):
        cell.number = number
        rendered_cells.append(_render_cell(cell))
    text = "\n\n".join(rendered_cells)
    if text:
        text += "\n"
    path.write_text(text)


def _parse_unmarked_m_file(lines: list[str]) -> list[Cell]:
    """Best-effort fallback for legacy .m/.wl files without explicit cell markers."""
    cells: list[Cell] = []
    line_count = len(lines)
    index = 0

    while index < line_count:
        while index < line_count and not lines[index].strip():
            index += 1
        if index >= line_count:
            break

        start = index
        end = index
        while end < line_count:
            if not lines[end].strip():
                blank_end = end
                while blank_end < line_count and not lines[blank_end].strip():
                    blank_end += 1
                if blank_end - end >= 2:
                    break
                end = blank_end
                continue
            end += 1

        block_lines = lines[start:end]
        while block_lines and not block_lines[-1].strip():
            block_lines.pop()
        if block_lines:
            number = len(cells) + 1
            content = "\n".join(block_lines)
            cell_type = _unmarked_cell_type(content)
            cells.append(
                Cell(
                    number=number,
                    cell_id=_default_cell_id(number),
                    cell_type=cell_type,
                    content=_normalize_cell_content(cell_type, content),
                    line_start=start + 1,
                    line_end=start + len(block_lines),
                )
            )

        index = end

    return cells


def parse_m_file(path: str | Path) -> list[Cell]:
    """Parse a .m (Mathematica package) file into cells.

    Cell boundaries are marked by (* ::CellType:: *) comments.
    Content between markers belongs to the preceding cell type.
    """
    path = Path(path)
    lines = path.read_text().splitlines()
    if not any(CELL_MARKER_RE.match(line.strip()) for line in lines):
        return _parse_unmarked_m_file(lines)

    cells: list[Cell] = []
    current_type: str | None = None
    current_cell_id: str | None = None
    current_lines: list[str] = []
    current_start: int = 0
    cell_count = 0

    def flush_cell():
        nonlocal cell_count
        if current_type is not None:
            cell_count += 1
            content = _normalize_cell_content(current_type, "\n".join(current_lines))
            cells.append(
                Cell(
                    number=cell_count,
                    cell_id=current_cell_id or _default_cell_id(cell_count),
                    cell_type=current_type,
                    content=content,
                    line_start=current_start,
                    line_end=current_start + max(len(current_lines) - 1, 0),
                )
            )

    for i, line in enumerate(lines, start=1):
        marker_match = CELL_MARKER_RE.match(line.strip())
        if marker_match:
            flush_cell()
            current_type = marker_match.group("cell_type")
            current_cell_id = marker_match.group("cell_id")
            current_lines = []
            current_start = i + 1  # content starts on next line
        elif current_type is not None:
            current_lines.append(line)

    # Flush the last cell
    flush_cell()

    return cells


def parse_nb_file(path: str | Path) -> list[Cell]:
    """Parse a .nb (Mathematica notebook) file into cells.

    Notebooks are Mathematica expressions (not XML). We extract Cell[...]
    expressions and their types.
    """
    path = Path(path)
    text = path.read_text()

    cells: list[Cell] = []
    cell_count = 0

    # .nb files contain Cell[content, "CellType", ...] expressions
    # We use a simple regex approach for common cases
    # This handles: Cell[BoxData[...], "Input", ...] and Cell["text", "Section", ...]
    cell_pattern = re.compile(
        r'Cell\[([^]]*(?:\[[^]]*\])*[^]]*),\s*"(\w+)"',
        re.MULTILINE,
    )

    for match in cell_pattern.finditer(text):
        cell_count += 1
        raw_content = match.group(1)
        cell_type = match.group(2)

        # Try to extract readable content from BoxData or raw strings
        content = _extract_cell_content(raw_content)

        # Approximate line numbers from character offset
        line_start = text[:match.start()].count("\n") + 1
        line_end = text[:match.end()].count("\n") + 1

        cells.append(
            Cell(
                number=cell_count,
                cell_id=_default_cell_id(cell_count),
                cell_type=cell_type,
                content=content,
                line_start=line_start,
                line_end=line_end,
            )
        )

    return cells


def _extract_cell_content(raw: str) -> str:
    """Best-effort extraction of readable content from .nb cell data."""
    # String content: "some text"
    string_match = re.match(r'^"(.*)"$', raw.strip(), re.DOTALL)
    if string_match:
        return string_match.group(1)

    # BoxData with RowBox containing string tokens
    # This is a simplification — real .nb parsing would need a full
    # Mathematica expression parser. For now, extract quoted strings.
    strings = re.findall(r'"([^"]*)"', raw)
    if strings:
        return "".join(strings)

    return raw.strip()


def parse_file(path: str | Path) -> list[Cell]:
    """Parse a .m or .nb file into cells, choosing parser by extension."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".m" or ext == ".wl":
        return parse_m_file(path)
    elif ext == ".nb":
        return parse_nb_file(path)
    else:
        raise ValueError(f"Unsupported file extension: {ext}. Expected .m, .wl, or .nb")


def create_m_cell(
    path: str | Path,
    cell_type: str,
    content: str,
    *,
    before_cell_id: str | None = None,
    after_cell_id: str | None = None,
    before_cell: int | None = None,
    after_cell: int | None = None,
) -> Cell:
    """Insert a new cell into a mutable .m/.wl file and persist cell IDs."""
    path = Path(path)
    _validate_mutable_path(path)
    cells = parse_m_file(path) if path.exists() else []

    selector_count = sum(
        value is not None
        for value in (before_cell_id, after_cell_id, before_cell, after_cell)
    )
    if selector_count > 1:
        raise ValueError("Use at most one insertion selector")

    insert_at = len(cells)
    if before_cell_id is not None or before_cell is not None:
        insert_at, _ = _resolve_cell(cells, cell_id=before_cell_id, cell_number=before_cell)
    elif after_cell_id is not None or after_cell is not None:
        insert_at, _ = _resolve_cell(cells, cell_id=after_cell_id, cell_number=after_cell)
        insert_at += 1

    new_cell = Cell(
        number=insert_at + 1,
        cell_id=_next_generated_cell_id(cells),
        cell_type=cell_type,
        content=_normalize_cell_content(cell_type, content),
        line_start=0,
        line_end=0,
    )
    cells.insert(insert_at, new_cell)
    write_m_file(path, cells)
    return _resolve_cell(parse_m_file(path), cell_id=new_cell.cell_id)[1]


def update_m_cell(
    path: str | Path,
    *,
    cell_id: str | None = None,
    cell_number: int | None = None,
    content: str | None = None,
    cell_type: str | None = None,
) -> Cell:
    """Update an existing cell in a mutable .m/.wl file."""
    path = Path(path)
    _validate_mutable_path(path)
    if content is None and cell_type is None:
        raise ValueError("Provide `content`, `cell_type`, or both")

    cells = parse_m_file(path)
    _, cell = _resolve_cell(cells, cell_id=cell_id, cell_number=cell_number)
    if cell_type is not None:
        cell.cell_type = cell_type
    if content is not None:
        cell.content = _normalize_cell_content(cell.cell_type, content)
    write_m_file(path, cells)
    return _resolve_cell(parse_m_file(path), cell_id=cell.cell_id)[1]


def delete_m_cell(
    path: str | Path,
    *,
    cell_id: str | None = None,
    cell_number: int | None = None,
) -> Cell:
    """Delete a cell from a mutable .m/.wl file."""
    path = Path(path)
    _validate_mutable_path(path)
    cells = parse_m_file(path)
    index, cell = _resolve_cell(cells, cell_id=cell_id, cell_number=cell_number)
    removed = cells.pop(index)
    write_m_file(path, cells)
    return removed
