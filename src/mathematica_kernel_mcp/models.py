"""Data models for mathematica-kernel-mcp."""

from dataclasses import dataclass, field


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
    status: str = "ok"  # "ok" | "parse_error" | "timeout" | "kernel_error"


@dataclass
class SessionInfo:
    """Information about a kernel session."""

    name: str
    is_alive: bool
    is_busy: bool
    out_count: int = 0  # number of Out[] entries
    pid: int | None = None
