"""Parse .m and .wl package files into unified Cell models.

`.nb` files are not handled here — they go through the live shared kernel
bridge (collab mode) for both reads and mutations. The solo dispatcher
refuses `.nb`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from mathematica_kernel_mcp.models import Cell

# Pattern matching Mathematica package cell markers:
# (* ::CellType:: *)
# Legacy MCP versions also wrote [cell_id=...] metadata. We still parse those
# markers as cell boundaries, but we no longer persist or trust the embedded IDs.
CELL_MARKER_RE = re.compile(
    r"^\(\*\s*::(?P<cell_type>\w+)::(?:\s*\[cell_id=(?P<cell_id>[-\w]+)\])?\s*\*\)$"
)

SOURCE_REF_RE = re.compile(
    r"^src:v1:(?P<number>\d+):(?P<start>\d+):(?P<end>\d+):"
    r"(?P<cell_hash>[0-9a-f]{16}):(?P<file_hash>[0-9a-f]{16})$"
)

# Pattern for cell content wrapped in (* ... *)
COMMENT_CONTENT_RE = re.compile(r"^\(\*(.+)\*\)$", re.DOTALL)
EXECUTABLE_CELL_TYPES = {"Input", "Code"}
MUTABLE_EXTENSIONS = {".m", ".wl"}


class StaleCellReferenceError(ValueError):
    """A source-backed cell reference no longer matches the current file."""

    def __init__(
        self,
        cell_ref: str,
        reason: str,
        *,
        expected_file_hash: str | None = None,
        current_file_hash: str | None = None,
    ):
        super().__init__(reason)
        self.cell_ref = cell_ref
        self.reason = reason
        self.expected_file_hash = expected_file_hash
        self.current_file_hash = current_file_hash

    def to_payload(self) -> dict:
        payload = {
            "status": "stale_cell_reference",
            "cellID": self.cell_ref,
            "message": self.reason,
        }
        if self.expected_file_hash is not None:
            payload["expectedFileHash"] = self.expected_file_hash
        if self.current_file_hash is not None:
            payload["currentFileHash"] = self.current_file_hash
        return payload


def _short_hash(text: str, length: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def _file_hash(text: str) -> str:
    return _short_hash(text)


def _cell_hash(cell_type: str, content: str, line_start: int, line_end: int) -> str:
    return _short_hash(f"{cell_type}\0{line_start}\0{line_end}\0{content}")


def _source_ref(
    number: int,
    cell_type: str,
    content: str,
    line_start: int,
    line_end: int,
    file_hash: str,
) -> str:
    digest = _cell_hash(cell_type, content, line_start, line_end)
    return f"src:v1:{number}:{line_start}:{line_end}:{digest}:{file_hash}"


def _source_ref_parts(cell_ref: str) -> dict | None:
    match = SOURCE_REF_RE.match(cell_ref)
    if not match:
        return None
    parts = match.groupdict()
    return {
        "number": int(parts["number"]),
        "start": int(parts["start"]),
        "end": int(parts["end"]),
        "cell_hash": parts["cell_hash"],
        "file_hash": parts["file_hash"],
    }


def _assign_source_refs(cells: list[Cell], file_hash: str) -> list[Cell]:
    for cell in cells:
        cell.cell_id = _source_ref(
            cell.number,
            cell.cell_type,
            cell.content,
            cell.line_start,
            cell.line_end,
            file_hash,
        )
    return cells


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


def _validate_mutable_path(path: Path) -> None:
    if path.suffix.lower() not in MUTABLE_EXTENSIONS:
        raise ValueError("Cell mutation is currently only supported for .m and .wl files")


def _resolve_cell(
    cells: list[Cell],
    *,
    cell_id: str | None = None,
    cell_number: int | None = None,
    current_file_hash: str | None = None,
) -> tuple[int, Cell]:
    if cell_id is not None and cell_number is not None:
        raise ValueError("Use either `cell_id` or `cell`, not both")
    if cell_id is None and cell_number is None:
        raise ValueError("Provide either `cell_id` or `cell`")

    if cell_id is not None:
        parts = _source_ref_parts(cell_id)
        if parts is not None:
            if current_file_hash is not None and parts["file_hash"] != current_file_hash:
                raise StaleCellReferenceError(
                    cell_id,
                    "The file changed after this source cell reference was read; re-read the notebook and retry.",
                    expected_file_hash=parts["file_hash"],
                    current_file_hash=current_file_hash,
                )
            for index, cell in enumerate(cells):
                if cell.cell_id == cell_id:
                    return index, cell
            raise StaleCellReferenceError(
                cell_id,
                "The source cell reference no longer matches any current cell; re-read the notebook and retry.",
                expected_file_hash=parts["file_hash"],
                current_file_hash=current_file_hash,
            )

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
    marker = f"(* ::{cell.cell_type}:: *)"
    content = cell.content.strip()
    if cell.cell_type in EXECUTABLE_CELL_TYPES or not content:
        body = content
    else:
        body = f"(*{content}*)"
    return f"{marker}\n{body}"


def write_m_file(path: str | Path, cells: list[Cell]) -> None:
    """Serialize cells back to a mutable .m/.wl file without MCP cell IDs."""
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


def _parse_unmarked_m_lines(lines: list[str], file_hash: str) -> list[Cell]:
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
                    cell_id="",
                    cell_type=cell_type,
                    content=_normalize_cell_content(cell_type, content),
                    line_start=start + 1,
                    line_end=start + len(block_lines),
                )
            )

        index = end

    return _assign_source_refs(cells, file_hash)


def parse_m_file(path: str | Path) -> list[Cell]:
    """Parse a .m/.wl package file into cells.

    Cell boundaries are marked by Mathematica package comments such as
    ``(* ::Section:: *)``. Returned cell IDs are source refs derived from the
    current file content; they are never persisted into the source file.
    """
    path = Path(path)
    text = path.read_text()
    file_hash = _file_hash(text)
    lines = text.splitlines()
    if not any(CELL_MARKER_RE.match(line.strip()) for line in lines):
        return _parse_unmarked_m_lines(lines, file_hash)

    cells: list[Cell] = []
    current_type: str | None = None
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
                    cell_id="",
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
            current_lines = []
            current_start = i + 1  # content starts on next line
        elif current_type is not None:
            current_lines.append(line)

    flush_cell()
    return _assign_source_refs(cells, file_hash)


def parse_file(path: str | Path) -> list[Cell]:
    """Parse a .m or .wl file into cells. `.nb` is not handled here; in collab
    mode it goes through the bridge, and solo mode does not support `.nb`."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in {".m", ".wl"}:
        return parse_m_file(path)
    raise ValueError(f"Unsupported file extension: {ext}. Expected .m or .wl")


def resolve_m_cell(
    path: str | Path,
    *,
    cell_id: str | None = None,
    cell_number: int | None = None,
) -> Cell:
    path = Path(path)
    text = path.read_text()
    cells = parse_m_file(path)
    _, cell = _resolve_cell(
        cells,
        cell_id=cell_id,
        cell_number=cell_number,
        current_file_hash=_file_hash(text),
    )
    return cell


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
    """Insert a new cell into a mutable .m/.wl file without writing MCP IDs."""
    path = Path(path)
    _validate_mutable_path(path)
    if path.exists():
        text = path.read_text()
        cells = parse_m_file(path)
        current_file_hash = _file_hash(text)
    else:
        cells = []
        current_file_hash = _file_hash("")

    selector_count = sum(
        value is not None
        for value in (before_cell_id, after_cell_id, before_cell, after_cell)
    )
    if selector_count > 1:
        raise ValueError("Use at most one insertion selector")

    insert_at = len(cells)
    if before_cell_id is not None or before_cell is not None:
        insert_at, _ = _resolve_cell(
            cells,
            cell_id=before_cell_id,
            cell_number=before_cell,
            current_file_hash=current_file_hash,
        )
    elif after_cell_id is not None or after_cell is not None:
        insert_at, _ = _resolve_cell(
            cells,
            cell_id=after_cell_id,
            cell_number=after_cell,
            current_file_hash=current_file_hash,
        )
        insert_at += 1

    new_cell = Cell(
        number=insert_at + 1,
        cell_id="",
        cell_type=cell_type,
        content=_normalize_cell_content(cell_type, content),
        line_start=0,
        line_end=0,
    )
    cells.insert(insert_at, new_cell)
    write_m_file(path, cells)
    return parse_m_file(path)[insert_at]


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

    text = path.read_text()
    cells = parse_m_file(path)
    index, cell = _resolve_cell(
        cells,
        cell_id=cell_id,
        cell_number=cell_number,
        current_file_hash=_file_hash(text),
    )
    if cell_type is not None:
        cell.cell_type = cell_type
    if content is not None:
        cell.content = _normalize_cell_content(cell.cell_type, content)
    write_m_file(path, cells)
    return parse_m_file(path)[index]


def delete_m_cell(
    path: str | Path,
    *,
    cell_id: str | None = None,
    cell_number: int | None = None,
) -> Cell:
    """Delete a cell from a mutable .m/.wl file."""
    path = Path(path)
    _validate_mutable_path(path)
    text = path.read_text()
    cells = parse_m_file(path)
    index, cell = _resolve_cell(
        cells,
        cell_id=cell_id,
        cell_number=cell_number,
        current_file_hash=_file_hash(text),
    )
    removed = cells.pop(index)
    write_m_file(path, cells)
    return removed
