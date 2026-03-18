"""Data models for mathematica-kernel-mcp."""

from dataclasses import dataclass, field


@dataclass
class CellRuntimeInfo:
    """Runtime metadata for a cell in a specific kernel session."""

    last_in: int | None = None
    last_out: int | None = None
    messages: list[str] = field(default_factory=list)
    last_run_at: str | None = None


@dataclass
class Cell:
    """A single cell parsed from a .m or .nb file."""

    number: int  # 1-indexed position in file
    cell_id: str  # stable identifier for mutation and run tracking
    cell_type: str  # "Title", "Section", "Text", "Input", "Code", etc.
    content: str  # the actual code or text
    line_start: int  # line number in source file
    line_end: int  # line number in source file
    runtime: CellRuntimeInfo = field(default_factory=CellRuntimeInfo)


@dataclass
class EvalResult:
    """Result summary from a kernel evaluation."""

    output_summary: str  # Short[result] or truncated InputForm
    head: str  # Head of the expression
    byte_size: int  # ByteCount
    leaf_count: int  # LeafCount
    messages: list[str] = field(default_factory=list)  # kernel warnings/errors
    is_truncated: bool = False  # whether summary is truncated
    in_number: int = 0  # the In[n] reference in the kernel
    out_number: int = 0  # the Out[n] reference in the kernel


@dataclass
class SessionInfo:
    """Information about a kernel session."""

    name: str
    is_alive: bool
    is_busy: bool
    kernel_version: str = ""
    memory_used: int = 0  # bytes
    loaded_packages: list[str] = field(default_factory=list)
    out_count: int = 0  # number of Out[] entries
