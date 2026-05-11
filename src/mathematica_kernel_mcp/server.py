"""FastMCP server with unified `notebook_*` tools for Wolfram development.

Each tool dispatches at call time:

- If `<file_dir>/.shared_kernel_bridge/queue/` exists, we use the live bridge
  (collaborative mode) — edits land in the user's open notebook, kernel state
  is shared with them.
- Otherwise we mutate the file on disk and run code in a kernel the MCP spawns
  itself (solo mode) — requires a locatable WolframKernel binary.

The LLM-facing API is the same in both modes; only the backend differs.
"""

import json
import logging
import re as _re
import unicodedata
from contextlib import asynccontextmanager
from textwrap import dedent

from fastmcp import FastMCP

from mathematica_kernel_mcp.backends import get_backend_for
from mathematica_kernel_mcp.bridge import BridgeError, BridgeTimeout
from mathematica_kernel_mcp.session import SessionManager

logger = logging.getLogger(__name__)

_manager: SessionManager | None = None


def _manager_factory() -> SessionManager:
    """Lazily create the embedded WolframKernel session (only used in solo mode)."""
    global _manager
    if _manager is None:
        _manager = SessionManager()
        _manager.start()
        logger.info("Embedded WolframKernel session started for solo mode")
    return _manager


@asynccontextmanager
async def lifespan(app):
    global _manager
    logger.info("Mathematica Kernel MCP server started")
    try:
        yield
    finally:
        if _manager is not None:
            _manager.stop()
            _manager = None
        logger.info("Mathematica Kernel MCP server stopped")


def _backend_call(path: str, fn, *, timeout: float = 30.0) -> dict:
    """Run a backend operation, converting backend-layer errors to dict responses."""
    try:
        backend = get_backend_for(path, _manager_factory, timeout=timeout)
    except BridgeError as exc:
        return {"error": "bridge_unavailable", "message": str(exc)}
    except Exception as exc:  # SessionManager spawn failure, etc.
        return {"error": "backend_init_failed", "message": str(exc)}
    try:
        return fn(backend)
    except BridgeTimeout as exc:
        return {"error": "bridge_timeout", "message": str(exc)}
    except BridgeError as exc:
        return {"error": "bridge_error", "message": str(exc)}
    except ValueError as exc:
        return {"error": "value_error", "message": str(exc)}
    except Exception as exc:
        return {"error": "backend_error", "message": str(exc)}


def _cell_preview(content: str, max_chars: int = 80) -> str:
    flat = content.replace("\n", " ")
    return flat if len(flat) <= max_chars else flat[: max_chars - 3] + "..."


mcp = FastMCP(
    "mathematica-kernel-mcp",
    instructions="""MCP server for collaborative or solo Mathematica/Wolfram Language development.

## Two modes (auto-detected per file)
- **Collaborative**: a `.shared_kernel_bridge/` directory next to the target file
  indicates the user has Mathematica open with the file and has evaluated
  `<< SharedKernelMCP`` + `StartSharedKernelBridge[]`. You drive a kernel they
  share with you, edits land live in their open notebook, and they see your
  activity in real time.
- **Solo**: no bridge. Files are mutated on disk; code runs in a kernel the
  MCP spawns via `wolframclient`. Requires a locatable WolframKernel binary.

You don't pick a mode — every `notebook_*` tool dispatches based on whether a
bridge is present next to the file you pass in.

## Cell-level workflow
1. `notebook_read(path, include_content=False)` — get a cell index with stable IDs.
2. `notebook_search(path, pattern)` — find cells by content with line-level matches.
3. `notebook_run_cell(path, cell_id)` — evaluate a cell. In collab the Output appears
   under it in the live notebook; in solo just returns the result.
4. `notebook_run_cells(path, cells=[...] | start/end | all=True)` — batch runner;
   one MCP call instead of N. Halts on first error by default.
5. `notebook_update_cell(path, cell_id, cell_type, content)` — rewrite a cell.
6. `notebook_insert_cell_after(...)` / `notebook_insert_cell_before(...)` — add cells.
7. `notebook_delete_cell(path, cell_id)` — remove a cell (and its tagged output).
8. `notebook_eval_inline(path, anchor_cell_id, code)` — single-call "let me try this".
9. `notebook_eval(path, code)` — evaluate WL ad hoc, no notebook trace.
10. `notebook_sweep_outputs(path)` — collab only: clean up Output cells whose
    anchor is gone. No-op in solo.

## Kernel state + introspection
- `notebook_kernel_state(path, fields=...)` — memory, version, context, packages.
- `notebook_kernel_restart(path)` — solo only; refused in collab.
- `notebook_abort_evaluation(path, signal="SIGINT", clear_queue=False)` —
  best-effort kernel abort when an eval is already in flight. Silent and
  headless only in solo mode on POSIX; in collab mode the GUI front-end
  surfaces a dialog the user must dismiss. Prefer `eval_timeout`.
- `notebook_symbol_info(path, name, fields=...)` — usage, definition, attributes,
  options, context, *Values, messages.
- `notebook_documentation_search(path, query)` — ranked WL doc matches.
- `notebook_names(path, pattern)` — `Names[pattern]` symbol list.
- `notebook_list_symbols(path, context="Global`")` — symbols by context.
- `notebook_get_output(path, out_number, view="full"|"short"|"summary")` —
  inspect a previous `Out[n]` (full / shallow / metadata).

## Bounding evaluation time
The run-style tools (`notebook_run_cell`, `notebook_run_cells`, `notebook_eval`,
`notebook_eval_inline`) accept `eval_timeout` (seconds). When set, the
kernel wraps the eval in `TimeConstrained[..., eval_timeout]`; exceeding it
returns status="timeout" instead of hanging the kernel. **This is the
recommended autonomous mechanism** — cross-platform, headless, no signals.
Use it whenever a computation might run away (`Simplify`, `Solve` on large
systems, etc.). `notebook_abort_evaluation` exists as a fallback but its
behavior depends on mode + OS (see that tool's docstring) — in collab mode
it surfaces a dialog the user must dismiss.

`cell_id` is opaque — pass back what you got from `notebook_read` / `notebook_search`.
In collab it's a Mathematica `CellID` (integer, stable across reorders); in solo
it's a 1-indexed position (re-read after mutations).
""",
    lifespan=lifespan,
)


# ----------------------------------------------------------------------------
# notebook_* tools — all dispatch through get_backend_for()
# ----------------------------------------------------------------------------


@mcp.tool()
def notebook_read(
    path: str,
    include_content: bool = True,
    cells: list[int] | None = None,
    preview_chars: int = 80,
    timeout: float = 30.0,
) -> dict:
    """Read the cell layout of `path`.

    In collab mode this reflects the live front-end notebook (with stable CellIDs
    injected on first read). In solo mode this parses the file on disk.

    Filters:
    - `include_content=False`: return one-line previews instead of full content
      (cheap to scan when picking a cell).
    - `cells=[id1, id2, ...]`: only return cells with those cellIDs.
    - `preview_chars`: max chars per preview when `include_content=False`.
    """
    def _do(backend):
        result = backend.read(path)
        all_cells = result.get("cells", [])
        if cells is not None:
            wanted = set(cells)
            all_cells = [c for c in all_cells if c.get("cellID") in wanted]
        if not include_content:
            for c in all_cells:
                c["preview"] = _cell_preview(c.get("content", ""), preview_chars)
                c.pop("content", None)
        result["cells"] = all_cells
        result["cell_count"] = len(all_cells)
        result["mode"] = backend.mode
        return result

    return _backend_call(path, _do, timeout=timeout)


@mcp.tool()
def notebook_search(
    path: str,
    pattern: str,
    regex: bool = False,
    case_sensitive: bool = False,
    styles: list[str] | None = None,
    context_lines: int = 2,
    include_full_cell: bool = False,
    timeout: float = 30.0,
) -> dict:
    """Search cells of `path` for `pattern`, returning line-level matches.

    Returns each cell containing at least one matching line, with line numbers
    (1-indexed within the cell) and a short context window around each match.

    - `regex=False`: literal substring (default). `regex=True`: Python regex.
    - `styles=["Code", "Section", ...]`: restrict to cells of these styles.
    - `context_lines`: lines of context before/after each match.
    - `include_full_cell=True`: also include the cell's full content per match.
    """
    def _do(backend):
        result = backend.read(path)
        all_cells = result.get("cells", [])
        if styles is not None:
            wanted_styles = set(styles)
            all_cells = [c for c in all_cells if c.get("style") in wanted_styles]

        if regex:
            flags = 0 if case_sensitive else _re.IGNORECASE
            try:
                rx = _re.compile(pattern, flags)
            except _re.error as exc:
                return {"error": f"Invalid regex: {exc}"}
            def _match(line: str) -> bool:
                return rx.search(line) is not None
        else:
            needle = pattern if case_sensitive else pattern.lower()
            def _match(line: str) -> bool:
                hay = line if case_sensitive else line.lower()
                return needle in hay

        cell_results = []
        total_matches = 0
        for c in all_cells:
            content = c.get("content", "")
            lines = content.split("\n")
            matches = []
            for i, line in enumerate(lines, start=1):
                if _match(line):
                    lo = max(1, i - context_lines)
                    hi = min(len(lines), i + context_lines)
                    context = [
                        {"line": j, "text": lines[j - 1], "is_match": j == i}
                        for j in range(lo, hi + 1)
                    ]
                    matches.append({"line": i, "text": line, "context": context})
            if matches:
                total_matches += len(matches)
                entry = {
                    "cellID": c.get("cellID"),
                    "index": c.get("index"),
                    "style": c.get("style"),
                    "match_count": len(matches),
                    "matches": matches,
                }
                if include_full_cell:
                    entry["content"] = content
                cell_results.append(entry)

        return {
            "status": "ok",
            "path": path,
            "mode": backend.mode,
            "pattern": pattern,
            "regex": regex,
            "context_lines": context_lines,
            "cell_count": len(cell_results),
            "match_count": total_matches,
            "cells": cell_results,
        }

    return _backend_call(path, _do, timeout=timeout)


@mcp.tool()
def notebook_run_cell(
    path: str,
    cell_id: int,
    timeout: float = 60.0,
    eval_timeout: float | None = None,
) -> dict:
    """Evaluate the cell with the given cellID.

    In collab mode, the Output appears under the input cell in the live notebook
    (replacing any prior bridge-tagged output for the same cell). In solo mode,
    the result is returned but no notebook is updated.

    `timeout` bounds how long Python waits for the result file (bridge-side).
    `eval_timeout` (seconds) is enforced kernel-side via TimeConstrained: when
    set, an evaluation exceeding it is aborted and returned with status="timeout".
    Use `eval_timeout` proactively when you don't trust a computation to finish.
    """
    return _backend_call(
        path,
        lambda b: b.run_cell(path, cell_id, eval_timeout=eval_timeout),
        timeout=timeout,
    )


@mcp.tool()
def notebook_update_cell(
    path: str, cell_id: int, cell_type: str, content: str, timeout: float = 30.0
) -> dict:
    """Rewrite the cell with the given cellID.

    In collab mode the user sees the edit happen live; in solo mode the file on
    disk is rewritten. Cell types: Input, Code, Section, Subsection, Text, etc.
    """
    return _backend_call(
        path,
        lambda b: b.update_cell(path, cell_id, cell_type, content),
        timeout=timeout,
    )


@mcp.tool()
def notebook_insert_cell_after(
    path: str,
    anchor_cell_id: int,
    cell_type: str,
    content: str,
    timeout: float = 30.0,
) -> dict:
    """Insert a new cell directly after `anchor_cell_id`. Returns its newCellID."""
    return _backend_call(
        path,
        lambda b: b.insert_cell_after(path, anchor_cell_id, cell_type, content),
        timeout=timeout,
    )


@mcp.tool()
def notebook_insert_cell_before(
    path: str,
    anchor_cell_id: int,
    cell_type: str,
    content: str,
    timeout: float = 30.0,
) -> dict:
    """Insert a new cell directly before `anchor_cell_id`. Returns its newCellID."""
    return _backend_call(
        path,
        lambda b: b.insert_cell_before(path, anchor_cell_id, cell_type, content),
        timeout=timeout,
    )


@mcp.tool()
def notebook_delete_cell(path: str, cell_id: int, timeout: float = 30.0) -> dict:
    """Delete the cell with the given cellID (and any tagged output, in collab)."""
    return _backend_call(path, lambda b: b.delete_cell(path, cell_id), timeout=timeout)


@mcp.tool()
def notebook_sweep_outputs(path: str, timeout: float = 30.0) -> dict:
    """Remove Output cells whose anchor cellID no longer exists. Collab-only; no-op in solo."""
    return _backend_call(path, lambda b: b.sweep_outputs(path), timeout=timeout)


@mcp.tool()
def notebook_eval(
    path: str,
    code: str,
    timeout: float = 60.0,
    eval_timeout: float | None = None,
) -> dict:
    """Evaluate WL code SILENTLY (no notebook trace).

    Use for LLM-internal probes. The result envelope has status, resultInputForm,
    messages, etc. Use `notebook_eval_inline` instead when the user should see
    the input + output.

    `eval_timeout` (seconds) wraps the eval in TimeConstrained kernel-side; on
    expiry, status="timeout".
    """
    return _backend_call(
        path,
        lambda b: b.evaluate(path, code, eval_timeout=eval_timeout),
        timeout=timeout,
    )


@mcp.tool()
def notebook_eval_inline(
    path: str,
    anchor_cell_id: int,
    code: str,
    cell_type: str = "Code",
    timeout: float = 60.0,
    eval_timeout: float | None = None,
) -> dict:
    """Insert a Code cell after `anchor_cell_id`, evaluate it, return the result.

    Single-call "let me try this" gesture. In collab mode, the Input + Output
    appear in the live notebook so the user sees what you tried. The cell stays
    in the file/notebook — `notebook_delete_cell` it later if you want to clean up.

    `eval_timeout` (seconds) wraps the eval in TimeConstrained kernel-side; on
    expiry, status="timeout".
    """
    return _backend_call(
        path,
        lambda b: b.eval_inline(
            path, anchor_cell_id, code, cell_type, eval_timeout=eval_timeout
        ),
        timeout=timeout,
    )


# ============================================================================
# Batch / state / introspection tools
# ============================================================================


_KERNEL_STATE_FIELDS = {
    "memory_bytes": "MemoryInUse[]",
    "max_memory_bytes": "MaxMemoryUsed[]",
    "packages": "$Packages",
    "context": "$Context",
    "context_path": "$ContextPath",
    "version": "ToString[$VersionNumber]",
}
_DEFAULT_KERNEL_STATE_FIELDS = ["memory_bytes", "max_memory_bytes", "context", "version"]
_SYMBOL_INFO_FIELDS = {
    "usage", "definition", "attributes", "options", "context",
    "own_values", "down_values", "up_values", "sub_values", "messages",
}
_GET_OUTPUT_VIEWS = {"full", "short", "summary"}
_MAX_DOCUMENTATION_RESULTS = 25
_DOCUMENTATION_STOP_WORDS = {
    "a", "an", "and", "for", "function", "functions", "from", "in", "into",
    "of", "on", "or", "symbolic", "the", "to", "with",
}
_EXECUTABLE_CELL_TYPES = {"Input", "Code"}

_TEXT_HELPERS_WL = dedent(
    """
    normalizeDisplayText[text_] := Module[{normalized},
        normalized = ToString[text, OutputForm];
        normalized = StringReplace[
            normalized,
            {
                "\\r\\n" -> "\\n",
                "\\r" -> "\\n",
                "\\t" -> "    ",
                "\\[Ellipsis]" -> "...",
                FromCharacterCode[8230] -> "...",
                "\\[Element]" -> " in ",
                FromCharacterCode[8712] -> " in ",
                "\\[Rule]" -> " -> ",
                FromCharacterCode[8594] -> " -> ",
                FromCharacterCode[8658] -> " => ",
                FromCharacterCode[8804] -> " <= ",
                FromCharacterCode[8805] -> " >= ",
                FromCharacterCode[8734] -> "Infinity"
            }
        ];
        normalized = StringReplace[
            normalized,
            {
                RegularExpression["[ ]+\\n"] -> "\\n",
                RegularExpression["\\n[ ]+"] -> "\\n",
                RegularExpression["\\n{3,}"] -> "\\n\\n"
            }
        ];
        StringTrim[normalized]
    ];

    symbolUsageText[s_Symbol] := Module[{docUsage, fallback},
        docUsage = If[
            Context[s] === "System`",
            Quiet[
                Check[
                    WolframLanguageData[
                        Entity["WolframLanguageSymbol", SymbolName[s]],
                        "PlaintextUsage"
                    ],
                    Missing["NotAvailable"]
                ]
            ],
            Missing["NotAvailable"]
        ];
        fallback = Quiet[Check[MessageName[s, "usage"], Missing["NotAvailable"]]];
        normalizeDisplayText[
            Replace[
                docUsage,
                (_Missing | $Failed) :> Replace[fallback, (_Missing | $Failed) :> "None"]
            ]
        ]
    ];
    """
)


def _normalize_terminal_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    normalized = text
    if any(marker in normalized for marker in ("Ã", "â", "Î", "ï", "ð")):
        try:
            normalized = normalized.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    normalized = unicodedata.normalize("NFKC", normalized)
    replacements = {
        "…": "...", "∈": " in ", "→": " -> ", "⇒": " => ",
        "≤": " <= ", "≥": " >= ", "∞": "Infinity",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    normalized = _re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = _re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _wl_string(value: str) -> str:
    return json.dumps(value)


def _wl_string_list(values: list[str]) -> str:
    if not values:
        return "{}"
    return "{" + ", ".join(json.dumps(v) for v in values) + "}"


def _symbol_list_expr(expr: str) -> str:
    return f'If[Length[{expr}] > 0, ToString[{expr}, InputForm], "None"]'


def _documentation_tokens(query: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    raw = _re.findall(r"[A-Za-z$][A-Za-z0-9$]*", query.lower())
    for tok in raw:
        if len(tok) < 3 or tok in _DOCUMENTATION_STOP_WORDS or tok in seen:
            continue
        seen.add(tok)
        tokens.append(tok)
    if not tokens:
        for tok in raw:
            if tok in seen:
                continue
            seen.add(tok)
            tokens.append(tok)
    return tokens


def _normalize_context_query(context: str) -> tuple[str, str]:
    n = context.strip()
    if not n:
        raise ValueError("Context must be non-empty")
    if "*" in n or "@" in n:
        return n, n
    if not n.endswith("`"):
        n += "`"
    return n, f"{n}*"


@mcp.tool()
def notebook_run_cells(
    path: str,
    cells: list[int] | None = None,
    start: int | None = None,
    end: int | None = None,
    all: bool = False,
    stop_on_error: bool = True,
    timeout: float = 60.0,
    eval_timeout: float | None = None,
) -> dict:
    """Run multiple cells in order in a single MCP call.

    Cell selection (one of):
    - `cells=[id1, id2, ...]`: explicit cellIDs (in the order to run).
    - `start` / `end`: 1-indexed positions in the read order (inclusive).
    - `all=True`: run every executable (Input/Code) cell.

    Returns a list of per-cell result dicts. With `stop_on_error=True` (default),
    halts on the first failure. Faster than looping `notebook_run_cell` because
    selection happens in one read instead of per call.

    `eval_timeout` (seconds) applies to each cell individually via TimeConstrained.
    """
    def _do(backend):
        all_cells = backend.read(path).get("cells", [])
        if all:
            if cells is not None or start is not None or end is not None:
                raise ValueError("Use either `all=True` or an explicit selector, not both")
            selected = [c for c in all_cells if c.get("style") in _EXECUTABLE_CELL_TYPES]
        elif cells is not None:
            if start is not None or end is not None:
                raise ValueError("Use either `cells=[...]` or `start`/`end`, not both")
            wanted = list(cells)
            by_id = {c.get("cellID"): c for c in all_cells}
            missing = [cid for cid in wanted if cid not in by_id]
            if missing:
                raise ValueError(f"cellID(s) not found: {missing}")
            selected = [by_id[cid] for cid in wanted]
        elif start is not None or end is not None:
            lo = start if start is not None else 1
            hi = end if end is not None else len(all_cells)
            if lo < 1 or hi > len(all_cells) or lo > hi:
                raise ValueError(
                    f"Invalid cell range {lo}-{hi} (file has {len(all_cells)} cells)"
                )
            selected = [c for c in all_cells if lo <= c.get("index", 0) <= hi]
        else:
            raise ValueError("Specify cells=[...], start/end, or all=True")

        results: list[dict] = []
        stopped_early = False
        for c in selected:
            r = backend.run_cell(path, c["cellID"], eval_timeout=eval_timeout)
            r["index"] = c.get("index")
            r["style"] = c.get("style")
            results.append(r)
            if stop_on_error and r.get("status") not in ("ok", "skipped"):
                stopped_early = True
                break

        return {
            "status": "ok",
            "path": path,
            "mode": backend.mode,
            "result_count": len(results),
            "stopped_early": stopped_early,
            "results": results,
        }

    return _backend_call(path, _do, timeout=timeout)


@mcp.tool()
def notebook_abort_evaluation(
    path: str,
    signal: str = "SIGINT",
    clear_queue: bool = False,
    timeout: float = 10.0,
) -> dict:
    """Signal the kernel to abort its current evaluation. Best-effort, not
    fully autonomous.

    Prefer `eval_timeout` on the call you're about to make — that's the
    headless cross-platform mechanism. Use `notebook_abort_evaluation` only
    when an eval is already in flight and the queued bridge can't reach it.

    Behavior depends on mode and OS:
    - **Solo mode, POSIX (Linux/macOS):** SIGINT is silent — the kernel calls
      `Abort[]` at its next polling point. Headless.
    - **Solo mode, Windows:** POSIX signal semantics differ; this is not
      reliable. Use `eval_timeout` instead.
    - **Collab mode, any OS:** signals the user's kernel, but the GUI front-end
      typically intercepts SIGINT and pops a "Continue / Abort" dialog the user
      must dismiss. Treat as "tap on the user's shoulder," not a clean abort.

    SIGTERM is available but more forceful and may cause the kernel to exit;
    only use it if SIGINT didn't work and you're prepared to lose kernel state.

    `clear_queue=True` deletes pending `.wl` files in the bridge queue so the
    scheduled task doesn't immediately pick up the next runaway (collab only).
    """
    def _do(backend):
        return {
            "mode": backend.mode,
            **backend.abort_evaluation(signal=signal, clear_queue=clear_queue),
        }

    return _backend_call(path, _do, timeout=timeout)


@mcp.tool()
def notebook_kernel_state(
    path: str, fields: list[str] | None = None, timeout: float = 30.0
) -> dict:
    """Return structured kernel state.

    Default fields: memory_bytes, max_memory_bytes, context, version.
    Available: memory_bytes, max_memory_bytes, packages, context, context_path, version.
    `path` is used to locate the bridge / dispatch backend.
    """
    if fields is None:
        wanted = list(_DEFAULT_KERNEL_STATE_FIELDS)
    else:
        invalid = sorted(set(fields) - set(_KERNEL_STATE_FIELDS))
        if invalid:
            return {"error": f"Unsupported kernel_state fields: {', '.join(invalid)}"}
        wanted = list(fields)

    selected_exprs = ", ".join(
        f'"{name}" -> {_KERNEL_STATE_FIELDS[name]}' for name in wanted
    )
    expr = f"<|{selected_exprs}|>"
    return _backend_call(
        path,
        lambda b: {"mode": b.mode, "state": b.evaluate_for_json(expr)},
        timeout=timeout,
    )


@mcp.tool()
def notebook_kernel_restart(path: str, timeout: float = 30.0) -> dict:
    """Restart the kernel.

    In collab mode this is refused (the kernel is the user's; we won't clobber
    it). In solo mode it restarts the MCP-spawned kernel, clearing all defs.
    """
    return _backend_call(path, lambda b: b.kernel_restart(), timeout=timeout)


@mcp.tool()
def notebook_symbol_info(
    path: str,
    name: str,
    fields: list[str] | None = None,
    timeout: float = 30.0,
) -> dict:
    """Return structured info about a symbol: usage, definition, attributes, etc.

    Default fields: all of usage, definition, attributes, options, context,
    own_values, down_values, up_values, sub_values, messages.
    """
    if fields is None:
        wanted = sorted(_SYMBOL_INFO_FIELDS)
    else:
        invalid = sorted(set(fields) - _SYMBOL_INFO_FIELDS)
        if invalid:
            return {"error": f"Unsupported symbol_info fields: {', '.join(invalid)}"}
        wanted = list(fields)

    field_exprs = {
        "usage": "symbolUsageText[s]",
        "definition": "ToString[Definition[s], InputForm]",
        "attributes": _symbol_list_expr("Attributes[s]"),
        "options": _symbol_list_expr("Options[s]"),
        "context": "Context[s]",
        "own_values": _symbol_list_expr("OwnValues[s]"),
        "down_values": _symbol_list_expr("DownValues[s]"),
        "up_values": _symbol_list_expr("UpValues[s]"),
        "sub_values": _symbol_list_expr("SubValues[s]"),
        "messages": _symbol_list_expr("Messages[s]"),
    }
    selected = ", ".join(f'"{f}" -> {field_exprs[f]}' for f in wanted)
    expr = dedent(
        f"""
        Module[{{}},
            {_TEXT_HELPERS_WL}
            With[{{held = ToExpression[{_wl_string(name)}, InputForm, HoldComplete]}},
                If[
                    MatchQ[held, HoldComplete[_Symbol]],
                    Replace[
                        held,
                        HoldComplete[s_Symbol] :> <|{selected}|>
                    ],
                    <|"error" -> "Name did not resolve to a symbol"|>
                ]
            ]
        ]
        """
    )

    def _do(backend):
        info = backend.evaluate_for_json(expr)
        if isinstance(info, dict) and "error" in info:
            return {"symbol": name, "error": info["error"]}
        if isinstance(info, dict) and isinstance(info.get("usage"), str):
            info["usage"] = _normalize_terminal_text(info["usage"])
        return {"mode": backend.mode, "symbol": name, "info": info}

    return _backend_call(path, _do, timeout=timeout)


@mcp.tool()
def notebook_documentation_search(
    path: str, query: str, max_results: int = 10, timeout: float = 120.0
) -> dict:
    """Search Wolfram Language symbol documentation by name or usage text.

    Ranked results with usage strings, URLs, and related symbols.
    Note: `WolframLanguageData` lookups can be slow on first call (often 30–60s
    on a cold kernel), so the default timeout is generous.
    """
    q = query.strip()
    if not q:
        return {"error": "Query must be non-empty"}
    limit = max(1, min(int(max_results), _MAX_DOCUMENTATION_RESULTS))
    tokens = _documentation_tokens(q)

    expr = dedent(
        f"""
        Module[{{}},
            {_TEXT_HELPERS_WL}
            Module[
                {{
                    query = {_wl_string(q)},
                    queryLower,
                    queryTokens = {_wl_string_list(tokens)},
                    queryTerms = {{}},
                    symbolNames,
                    entities,
                    results
                }},
                queryLower = ToLowerCase[query];
                queryTerms = DeleteDuplicates[
                    Join[
                        queryTokens,
                        Map[ToUpperCase[StringTake[#, 1]] <> StringDrop[#, 1] &, queryTokens]
                    ]
                ];
                symbolNames = DeleteDuplicates[
                    Flatten[Map[Names["System`*" <> # <> "*"] &, queryTerms], 1]
                ];
                entities = Map[StringReplace[#, StartOfString ~~ "System`" -> ""] &, symbolNames];
                results = Map[
                    Function[name,
                        Module[
                            {{
                                nameLower, entity, usage, usageLower, url, related,
                                matchSources = {{}},
                                exactNameHit, nameHit, usageHit,
                                tokenNameHits, tokenUsageHits, usagePos
                            }},
                            nameLower = ToLowerCase[name];
                            entity = Entity["WolframLanguageSymbol", name];
                            usage = Replace[
                                Quiet[Check[WolframLanguageData[entity, "PlaintextUsage"], ""]],
                                _Missing -> ""
                            ];
                            usage = normalizeDisplayText[usage];
                            usageLower = ToLowerCase[usage];
                            url = Replace[
                                Quiet[Check[WolframLanguageData[entity, "URL"], Missing["NotAvailable"]]],
                                _Missing -> Missing["NotAvailable"]
                            ];
                            related = Replace[
                                Quiet[Check[WolframLanguageData[entity, "RelatedSymbols"], {{}}]],
                                _Missing -> {{}}
                            ];
                            exactNameHit = nameLower === queryLower;
                            nameHit = StringContainsQ[nameLower, queryLower];
                            usageHit = usage =!= "" && StringContainsQ[usageLower, queryLower];
                            tokenNameHits = Count[queryTokens, token_ /; StringContainsQ[nameLower, token]];
                            tokenUsageHits = Count[
                                queryTokens, token_ /; usage =!= "" && StringContainsQ[usageLower, token]
                            ];
                            If[exactNameHit, AppendTo[matchSources, "exact_name"]];
                            If[nameHit, AppendTo[matchSources, "name"]];
                            If[usageHit, AppendTo[matchSources, "usage"]];
                            If[tokenNameHits > 0, AppendTo[matchSources, "name_tokens"]];
                            If[tokenUsageHits > 0, AppendTo[matchSources, "usage_tokens"]];
                            If[
                                !(exactNameHit || nameHit || usageHit || tokenNameHits > 0 || tokenUsageHits > 0),
                                Nothing,
                                usagePos = If[usageHit, First[First[StringPosition[usageLower, queryLower]]], 10^9];
                                <|
                                    "symbol" -> name,
                                    "usage" -> usage,
                                    "url" -> If[url === Missing["NotAvailable"], "", ToString[url, OutputForm]],
                                    "match_sources" -> matchSources,
                                    "related_symbols" -> Map[
                                        ToString[#, OutputForm] &,
                                        DeleteMissing[
                                            Map[
                                                Quiet[Check[
                                                    WolframLanguageData[#, "Name"],
                                                    Missing["NotAvailable"]
                                                ]] &,
                                                Take[related, UpTo[5]]
                                            ]
                                        ]
                                    ],
                                    "rank" -> {{
                                        If[exactNameHit, 0, 1],
                                        -tokenNameHits,
                                        -tokenUsageHits,
                                        If[nameHit, 0, 1],
                                        If[usageHit, 0, 1],
                                        usagePos,
                                        StringLength[name],
                                        name
                                    }}
                                |>
                            ]
                        ]
                    ],
                    entities
                ];
                results = Cases[results, _Association];
                results = Map[
                    KeyDrop[#, "rank"] &,
                    Take[SortBy[results, Lookup[#, "rank"] &], UpTo[{limit}]]
                ];
                <|
                    "query" -> query,
                    "result_count" -> Length[results],
                    "results" -> results
                |>
            ]
        ]
        """
    )

    def _do(backend):
        result = backend.evaluate_for_json(expr)
        if isinstance(result, dict):
            for item in result.get("results", []):
                usage = item.get("usage")
                if isinstance(usage, str):
                    item["usage"] = _normalize_terminal_text(usage)
        return result

    return _backend_call(path, _do, timeout=timeout)


@mcp.tool()
def notebook_names(path: str, pattern: str, timeout: float = 30.0) -> dict:
    """Return all symbol names matching a pattern (e.g., "Plot*", "*Integrate*")."""
    expr = f"Names[{_wl_string(pattern)}]"
    return _backend_call(
        path,
        lambda b: {"pattern": pattern, "matches": b.evaluate_for_json(expr)},
        timeout=timeout,
    )


@mcp.tool()
def notebook_list_symbols(
    path: str, context: str = "Global`", timeout: float = 30.0
) -> dict:
    """List symbols for a context (e.g., 'Global`') or a Names pattern."""
    try:
        normalized, pattern = _normalize_context_query(context)
    except ValueError as exc:
        return {"error": str(exc)}
    expr = f"Names[{_wl_string(pattern)}]"
    def _do(backend):
        symbols = backend.evaluate_for_json(expr)
        return {
            "context": normalized,
            "pattern": pattern,
            "count": len(symbols) if isinstance(symbols, list) else 0,
            "symbols": symbols,
        }
    return _backend_call(path, _do, timeout=timeout)


@mcp.tool()
def notebook_get_output(
    path: str,
    out_number: int,
    view: str = "full",
    max_chars: int = 2000,
    depth: int = 3,
    timeout: float = 30.0,
) -> dict:
    """Inspect a previous evaluation's `Out[n]` value.

    `view`:
    - `"full"`: full InputForm string (truncated at `max_chars`).
    - `"short"`: `Shallow` rendering at `depth` (truncated at `max_chars`).
    - `"summary"`: structured `<|head, leaf_count, depth, byte_count, dimensions|>`.
    """
    if view not in _GET_OUTPUT_VIEWS:
        return {"error": f"Unsupported view '{view}'. Use one of: {sorted(_GET_OUTPUT_VIEWS)}"}

    if view == "summary":
        expr = dedent(
            f"""
            With[{{res = Out[{out_number}]}},
                <|
                    "head" -> ToString[Head[res]],
                    "leaf_count" -> LeafCount[res],
                    "depth" -> Depth[res],
                    "byte_count" -> ByteCount[res],
                    "dimensions" -> Quiet[Check[Dimensions[res], Null]]
                |>
            ]
            """
        )
        return _backend_call(
            path,
            lambda b: {"out_number": out_number, "view": view, "summary": b.evaluate_for_json(expr)},
            timeout=timeout,
        )

    if view == "full":
        wl = f"ToString[Out[{out_number}], InputForm]"
    else:
        wl = f"ToString[Shallow[Out[{out_number}], {{{depth}, 3}}], OutputForm]"

    def _do(backend):
        envelope = backend.evaluate(path, wl)
        raw = envelope.get("resultInputForm") if isinstance(envelope, dict) else None
        if not isinstance(raw, str):
            return {"out_number": out_number, "view": view, "error": "no_result", "envelope": envelope}
        # `evaluate`'s resultInputForm wraps strings with quotes; strip them.
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            raw = raw[1:-1].encode().decode("unicode_escape")
        is_truncated = len(raw) > max_chars
        if is_truncated:
            raw = raw[:max_chars] + "..."
        result = {
            "out_number": out_number,
            "view": view,
            "output": raw,
            "is_truncated": is_truncated,
        }
        if view == "short":
            result["depth"] = depth
        return result

    return _backend_call(path, _do, timeout=timeout)
