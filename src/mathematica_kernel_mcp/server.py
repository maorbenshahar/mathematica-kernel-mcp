"""FastMCP server with unified `notebook_*` tools for Wolfram development.

Each tool dispatches at call time:

- If a matching global bridge registry record exists, we use the live socket
  bridge (collaborative mode). Edits land in the user's open notebook and
  kernel state is shared with them.
- Otherwise we mutate the file on disk and run code in a kernel the MCP spawns
  itself (solo mode) — requires a locatable WolframKernel binary.

The LLM-facing API is the same in both modes; only the backend differs.
"""

import json
import logging
import re as _re
import unicodedata
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from textwrap import dedent
from uuid import uuid4

from fastmcp import FastMCP

from mathematica_kernel_mcp.backends import get_backend_for
from mathematica_kernel_mcp.bridge import (
    BridgeError,
    BridgeTimeout,
    bridge_registry_directories,
    discover_bridge_records,
)
from mathematica_kernel_mcp.session import DEFAULT_TIMEOUT, SessionManager

logger = logging.getLogger(__name__)

_manager: SessionManager | None = None


def _manager_factory() -> SessionManager:
    """Lazily create the embedded WolframKernel manager for solo/scratch mode."""
    global _manager
    if _manager is None:
        _manager = SessionManager()
        logger.info("Embedded WolframKernel manager initialized for solo/scratch mode")
    return _manager


@asynccontextmanager
async def lifespan(_app):
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


# Default cap on `resultInputForm` (the textual result rendering). Kept small
# by default so a casual eval doesn't flood the agent's context window —
# callers wanting more can pass `max_response_chars` explicitly, and the full
# value is still recoverable via kernel_get_output(out_number, view="full",
# max_chars=N) when needed.
_RESULT_INPUTFORM_MAX_CHARS = 1500
_RESULT_MESSAGE_PRINT_MAX_CHARS = 1000
_RESULT_CODE_MAX_CHARS = 1000
_RESULT_JSON_MAX_CHARS = 20000


def _truncate_text(value: str, max_chars: int) -> tuple[str, bool, int]:
    chars = len(value)
    if chars <= max_chars:
        return value, False, chars
    omitted = chars - max_chars
    return value[:max_chars] + f"... [truncated {omitted} chars]", True, chars


def _truncate_result_input_form(
    payload,
    max_chars: int = _RESULT_INPUTFORM_MAX_CHARS,
    message_print_max_chars: int = _RESULT_MESSAGE_PRINT_MAX_CHARS,
    code_max_chars: int = _RESULT_CODE_MAX_CHARS,
    json_max_chars: int = _RESULT_JSON_MAX_CHARS,
):
    if isinstance(payload, list):
        return [
            _truncate_result_input_form(
                item,
                max_chars,
                message_print_max_chars,
                code_max_chars,
                json_max_chars,
            )
            for item in payload
        ]
    if not isinstance(payload, dict):
        return payload

    result = dict(payload)
    code = result.get("code")
    if isinstance(code, str):
        result["code"], truncated, chars = _truncate_text(code, code_max_chars)
        if truncated:
            result["codeTruncated"] = True
            result["codeChars"] = chars

    raw = result.get("resultInputForm")
    if isinstance(raw, str):
        result["resultInputForm"], truncated, chars = _truncate_text(raw, max_chars)
        if truncated:
            result["resultInputFormTruncated"] = True
            result["resultInputFormChars"] = chars

    # resultJSON can be any JSON-serializable value. Apply BOTH caps:
    # - the caller-supplied max_chars (the request-level response budget,
    #   defaulting to _RESULT_INPUTFORM_MAX_CHARS=1500). Whichever is
    #   stricter wins. Without this, asking for max_response_chars=200 still
    #   sent back a 20k-char resultJSON.
    # - json_max_chars (hard cap to keep us under MCP transport limits).
    # On exceed, drop the value + mark with resultJSONTruncated; the caller
    # falls back to resultInputForm.
    if "resultJSON" in result and result["resultJSON"] is not None:
        try:
            serialized = json.dumps(result["resultJSON"], ensure_ascii=False)
        except (TypeError, ValueError):
            serialized = None
        effective_cap = min(json_max_chars, max_chars)
        if serialized is not None and len(serialized) > effective_cap:
            result["resultJSON"] = None
            result["resultJSONTruncated"] = True
            result["resultJSONChars"] = len(serialized)

    for key in ("messages", "prints"):
        values = result.get(key)
        if not isinstance(values, list):
            continue
        next_values = []
        chars_by_item = []
        any_truncated = False
        for item in values:
            if isinstance(item, str):
                text, truncated, chars = _truncate_text(
                    item, message_print_max_chars
                )
                next_values.append(text)
                chars_by_item.append(chars)
                any_truncated = any_truncated or truncated
            else:
                next_values.append(item)
                chars_by_item.append(None)
        result[key] = next_values
        if any_truncated:
            result[f"{key}Truncated"] = True
            result[f"{key}Chars"] = chars_by_item

    if isinstance(result.get("results"), list):
        result["results"] = [
            _truncate_result_input_form(
                item,
                max_chars,
                message_print_max_chars,
                code_max_chars,
                json_max_chars,
            )
            for item in result["results"]
        ]
    return result


_KERNEL_ID_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_RESERVED_KERNEL_IDS = {"collab", "shared", "notebook"}


def _validate_kernel_id(kernel_id: str) -> str:
    kid = kernel_id.strip()
    if not kid:
        raise ValueError("kernel_id must be non-empty")
    if kid in _RESERVED_KERNEL_IDS:
        raise ValueError(
            f"kernel_id {kid!r} is reserved; use an explicit scratch kernel name"
        )
    if not _KERNEL_ID_RE.match(kid):
        raise ValueError(
            "kernel_id must start with a letter and contain only letters, "
            "digits, '_', '-', or '.', max length 64"
        )
    return kid


def _kernel_timeout(eval_timeout: float | None) -> int:
    if eval_timeout is None:
        return DEFAULT_TIMEOUT
    if eval_timeout <= 0:
        raise ValueError("eval_timeout must be positive when provided")
    return max(1, int(eval_timeout))


def _session_payload(info) -> dict:
    return {
        "kernel_id": info.name,
        "owner": "agent",
        "is_alive": info.is_alive,
        "is_busy": info.is_busy,
        "out_count": info.out_count,
        "pid": info.pid,
    }


def _kernel_eval_payload(
    manager: SessionManager,
    kernel_id: str,
    code: str,
    *,
    eval_timeout: float | None = None,
    summary_max: int = 500,
) -> dict:
    kid = _validate_kernel_id(kernel_id)
    result = manager.evaluate(
        code,
        session_name=kid,
        timeout=_kernel_timeout(eval_timeout),
        summary_max=summary_max,
    )
    code_preview, code_truncated, code_chars = _truncate_text(
        code,
        _RESULT_CODE_MAX_CHARS,
    )
    payload = {
        "status": result.status,
        "kernel_id": kid,
        "owner": "agent",
        "code": code_preview,
        "resultInputForm": result.output_summary,
        "messages": result.messages,
        "in_number": result.in_number,
        "out_number": result.out_number,
        "head": result.head,
        "byte_size": result.byte_size,
        "leaf_count": result.leaf_count,
        "is_truncated": result.is_truncated,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if code_truncated:
        payload["codeTruncated"] = True
        payload["codeChars"] = code_chars
    return payload


def _kernel_eval_json(
    manager: SessionManager,
    kernel_id: str,
    code: str,
    *,
    eval_timeout: float | None = None,
) -> dict:
    """Evaluate WL and return a JSON envelope: {status, value?, inputForm}.

    Calls SafeEval via wolframclient, which delivers values as native Python
    types (proper Unicode strings, arbitrary-precision ints, lists, dicts).
    For values that contain non-JSON-serializable parts (Symbols, Graphics,
    Rationals, etc.) the status becomes "not_json_encodable" and the caller
    falls back to `inputForm`.
    """
    kid = _validate_kernel_id(kernel_id)
    timeout = _kernel_timeout(eval_timeout)
    env = manager.evaluate_native(code, session_name=kid, timeout=timeout)
    status = env.get("status", "ok")
    if status != "ok":
        out = {"status": status}
        if "inputForm" in env:
            out["inputForm"] = env["inputForm"]
        return out
    value = env.get("value")
    try:
        json.dumps(value)
        return {
            "status": "ok",
            "value": value,
            "inputForm": env.get("inputForm"),
        }
    except (TypeError, ValueError):
        return {
            "status": "not_json_encodable",
            "head": env.get("head"),
            "inputForm": env.get("inputForm"),
        }


def _kernel_call(fn) -> dict:
    try:
        return fn()
    except ValueError as exc:
        return {"error": "value_error", "message": str(exc)}
    except json.JSONDecodeError as exc:
        return {"error": "json_decode_error", "message": str(exc)}
    except Exception as exc:
        return {"error": "kernel_error", "message": str(exc)}


mcp = FastMCP(
    "mathematica-kernel-mcp",
    instructions="""MCP server for collaborative or solo Mathematica/Wolfram Language development.

## Two modes (auto-detected per file)
- **Collaborative**: a matching global bridge registry record indicates the
  user has Mathematica open with the file and has evaluated
  `<< SharedKernelMCP`` + `StartSharedKernelBridge[]`. The bridge uses an
  authenticated localhost socket. You drive a kernel they share with you, edits
  land live in their open notebook, and they see your activity in real time.
- **Solo**: no bridge. Files are mutated on disk; code runs in a kernel the
  MCP spawns via `wolframclient`. Requires a locatable WolframKernel binary.

You don't pick a mode — every `notebook_*` tool dispatches based on whether a
bridge is present for the file you pass in. Use `bridge_list()` to inspect
currently registered collaborative bridges.

## Cell-level workflow
1. `notebook_read(path)` — defaults to outline-only previews with `cellID`,
   `style`, `label` (the user-visible `In[N]:= ` / `Out[N]= `, your shared
   vocabulary with the user), and `contentChars`. Compose filters: `cells`,
   `start`/`end`, `styles=["Title","Section","Subsection"]`, `around_cell`+
   `window`. Pass `include_content=True` only when you need full bodies.
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

## Context budget — responses count against your window
- `notebook_read` defaults to previews (no full bodies) and tiny per-cell
  overhead. Use `styles=["Section",...]` for an outline, `around_cell`+
  `window` for local context after a search hit, `start`/`end` for a slice.
- Eval and run tools accept `max_response_chars` (default ~1500). For a big
  result, eval first, then `notebook_get_output(path, out_number,
  view="summary")` to see `{head, leaf_count, byte_count}`, and only then
  `view="full", max_chars=N` if you really want the bytes.
- Use `kernel_eval(kernel_id, code)` instead of `kernel_eval_json` when you
  only need the textual summary — `kernel_eval_json` returns the full Python
  value (capped, but bigger by default).

## Kernel state + introspection
- `notebook_kernel_state(path, fields=...)` — memory, version, context, packages.
- `notebook_abort_evaluation(path, signal="SIGINT")` —
  best-effort kernel abort when an eval is already in flight. Silent and
  headless only in solo mode on POSIX; in collab mode the GUI front-end
  surfaces a dialog the user must dismiss. Prefer `eval_timeout`. To restart
  an agent-owned scratch kernel, call `kernel_restart(kernel_id)`.
- `notebook_symbol_info(path, name, fields=...)` — usage, definition, attributes,
  options, context, *Values, messages.
- `notebook_documentation_search(path, query)` — ranked WL doc matches.
- `notebook_list_symbols(path, context="Global`")` — symbols by context or
  `Names` pattern (`"Plot*"`, `"*Integrate*"`).
- `notebook_get_output(path, out_number, view="full"|"short"|"summary")` —
  inspect a previous `Out[n]` from the file's collab/solo kernel. For scratch
  kernels, use `kernel_get_output(kernel_id, out_number, ...)` instead.

## Agent-owned scratch kernels
- `kernel_create(kernel_id?)`, `kernel_list`, `kernel_eval`, `kernel_eval_json`,
  `kernel_state`, `kernel_get_output`, `kernel_restart`, `kernel_abort`, and
  `kernel_close` manage explicit agent-owned kernels. Use these for probes
  that should not pollute the user's shared notebook kernel.
- `notebook_run_cell(..., kernel_id=...)` and `notebook_run_cells(..., kernel_id=...)`
  read cell content from the file but evaluate it in the named scratch kernel
  (no visible notebook Output). For path-free scratch probes, call
  `kernel_eval(kernel_id, code)` directly.

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
In collab it's a Mathematica `CellID` (integer, stable across reorders).
In solo `.m`/`.wl` it is an opaque source ref that is invalidated when the file
changes; re-read after mutations or `stale_cell_reference` responses. `.nb`
files require collab mode — solo mode refuses them.
""",
    lifespan=lifespan,
)


# ----------------------------------------------------------------------------
# notebook_* tools — all dispatch through get_backend_for()
# ----------------------------------------------------------------------------


@mcp.tool()
def bridge_list(
    include_stale: bool = False,
    prune_stale: bool = False,
) -> dict:
    """List collaborative bridge registry records published by Mathematica.

    This reports user-owned notebook kernels discoverable through the global
    registry. Authentication tokens are intentionally omitted from the response.

    - `include_stale=True`: also return records whose kernel PID or socket
      metadata no longer looks alive.
    - `prune_stale=True`: delete stale registry records while scanning.
    """
    try:
        bridges = discover_bridge_records(
            include_stale=include_stale,
            prune_stale=prune_stale,
        )
    except Exception as exc:
        return {"error": "bridge_registry_error", "message": str(exc)}
    return {
        "status": "ok",
        "registry_directories": [
            str(directory) for directory in bridge_registry_directories()
        ],
        "bridge_count": len(bridges),
        "bridges": bridges,
    }


@mcp.tool()
def notebook_read(
    path: str,
    include_content: bool = False,
    cells: list[int | str] | None = None,
    start: int | None = None,
    end: int | None = None,
    styles: list[str] | None = None,
    around_cell: int | str | None = None,
    window: int = 2,
    preview_chars: int = 80,
    timeout: float = 30.0,
) -> dict:
    """Read the cell layout of `path`.

    In collab mode this reflects the live front-end notebook (with stable
    CellIDs injected on first read). In solo mode this parses the file on
    disk; `.m`/`.wl` cells return opaque source refs that go stale when the
    file changes.

    Each cell carries `index`, `cellID`, `style`, `label` (the front-end's
    `In[N]:= ` / `Out[N]= ` text in collab mode — your shared vocabulary with
    the user — or `""` in solo), `contentChars` (full length even when only
    `preview` is returned), and either `content` or `preview`.

    Defaults to `include_content=False` — outline-first to keep responses
    cheap. Pass `include_content=True` only when you actually need the full
    bodies. Composable filters (apply in this order, the matching cells
    become your view):

    - `cells=[id1, id2, ...]`: only those opaque cell IDs.
    - `start=N, end=M`: 1-indexed positional slice (inclusive).
    - `styles=["Title", "Section", "Subsection"]`: only matching cell styles.
      Combined with `include_content=False` this gives a cheap outline view.
    - `around_cell=ID, window=K`: the named cell plus K cells before/after.

    `preview_chars` caps the per-cell preview length (default 80).
    Responses count against your context window — prefer the default
    outline + targeted re-reads over a single full-content dump.
    """
    def _do(backend):
        result = backend.read(
            path,
            include_content=include_content,
            preview_chars=preview_chars,
        )
        all_cells = result.get("cells", [])

        if cells is not None:
            wanted = {str(cell_id) for cell_id in cells}
            all_cells = [c for c in all_cells if str(c.get("cellID")) in wanted]

        if start is not None or end is not None:
            lo = start if start is not None else 1
            hi = end if end is not None else len(all_cells) + (start or 0)
            all_cells = [c for c in all_cells if lo <= c.get("index", 0) <= hi]

        if styles is not None:
            wanted_styles = set(styles)
            all_cells = [c for c in all_cells if c.get("style") in wanted_styles]

        if around_cell is not None:
            full = result.get("cells", [])
            target_pos = next(
                (i for i, c in enumerate(full)
                 if str(c.get("cellID")) == str(around_cell)),
                None,
            )
            if target_pos is None:
                return {"error": f"around_cell={around_cell!r} not found"}
            lo = max(0, target_pos - max(0, window))
            hi = min(len(full), target_pos + max(0, window) + 1)
            wanted_indices = {c.get("index") for c in full[lo:hi]}
            all_cells = [c for c in all_cells if c.get("index") in wanted_indices]

        if not include_content:
            for c in all_cells:
                c["preview"] = c.get(
                    "preview",
                    _cell_preview(c.get("content", ""), preview_chars),
                )
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
    max_matches: int = 200,
    timeout: float = 30.0,
) -> dict:
    """Search cells of `path` for `pattern`, returning line-level matches.

    Returns each cell containing at least one matching line, with line numbers
    (1-indexed within the cell) and a short context window around each match.

    - `regex=False`: literal substring (default). `regex=True`: Python regex.
    - `styles=["Code", "Section", ...]`: restrict to cells of these styles.
    - `context_lines`: lines of context before/after each match.
    - `include_full_cell=True`: also include the cell's full content per match.
    - `max_matches`: cap on total returned matches to keep the response under
      MCP transport size limits. When truncated, `match_count` reflects the
      total found and `truncated=True` is set; narrow the pattern or filter
      by style to see the rest.
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
        returned_matches = 0
        truncated = False
        for c in all_cells:
            content = c.get("content", "")
            lines = content.split("\n")
            matches = []
            for i, line in enumerate(lines, start=1):
                if _match(line):
                    total_matches += 1
                    if returned_matches >= max_matches:
                        truncated = True
                        continue
                    lo = max(1, i - context_lines)
                    hi = min(len(lines), i + context_lines)
                    context = [
                        {"line": j, "text": lines[j - 1], "is_match": j == i}
                        for j in range(lo, hi + 1)
                    ]
                    matches.append({"line": i, "text": line, "context": context})
                    returned_matches += 1
            if matches:
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

        payload = {
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
        if truncated:
            payload["truncated"] = True
            payload["returned_matches"] = returned_matches
        return payload

    return _backend_call(path, _do, timeout=timeout)


@mcp.tool()
def notebook_run_cell(
    path: str,
    cell_id: int | str,
    timeout: float = 60.0,
    eval_timeout: float | None = None,
    kernel_id: str | None = None,
    max_response_chars: int = _RESULT_INPUTFORM_MAX_CHARS,
) -> dict:
    """Evaluate the cell with the given cellID.

    In collab mode, the Output appears under the input cell in the live notebook
    (replacing any prior bridge-tagged output for the same cell). In solo mode,
    the result is returned but no notebook is updated.

    If `kernel_id` is provided, the cell content is read from the notebook/file
    but evaluated in that agent-owned scratch kernel. No visible notebook Output
    is written in this mode.

    `timeout` bounds how long Python waits for the backend call to return.
    `eval_timeout` (seconds) is enforced kernel-side via TimeConstrained: when
    set, an evaluation exceeding it is aborted and returned with status="timeout".
    Use `eval_timeout` proactively when you don't trust a computation to finish.
    `max_response_chars` caps the textual result (default ~1500); raise it or
    call `notebook_get_output(..., view="full", max_chars=N)` for more.
    """
    if kernel_id is not None:
        def _scratch(backend):
            result = backend.read(path)
            cell = _find_cell_payload(result.get("cells", []), cell_id)
            return _run_cell_in_kernel_payload(
                path=path,
                source_mode=backend.mode,
                cell=cell,
                kernel_id=kernel_id,
                eval_timeout=eval_timeout,
            )

        return _backend_call(
            path,
            lambda backend: _truncate_result_input_form(
                _scratch(backend), max_chars=max_response_chars
            ),
            timeout=timeout,
        )

    return _backend_call(
        path,
        lambda b: _truncate_result_input_form(
            b.run_cell(path, cell_id, eval_timeout=eval_timeout),
            max_chars=max_response_chars,
        ),
        timeout=timeout,
    )


@mcp.tool()
def notebook_update_cell(
    path: str, cell_id: int | str, cell_type: str, content: str, timeout: float = 30.0
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
    anchor_cell_id: int | str,
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
    anchor_cell_id: int | str,
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
def notebook_delete_cell(path: str, cell_id: int | str, timeout: float = 30.0) -> dict:
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
    max_response_chars: int = _RESULT_INPUTFORM_MAX_CHARS,
) -> dict:
    """Evaluate WL code SILENTLY in the file's collab/solo kernel.

    No notebook trace — use for LLM-internal probes that should share the
    user's shared kernel state. For scratch probes that should not touch the
    user's kernel, call `kernel_eval(kernel_id, code)` directly.

    `eval_timeout` (seconds) wraps the eval in TimeConstrained kernel-side;
    on expiry, status="timeout". On a parse error, status="parse_error".

    `max_response_chars` caps the textual result (`resultInputForm`) so a
    casual probe doesn't flood your context window. Default ~1500 chars; if
    you need the full value, call `notebook_get_output(path, out_number,
    view="full", max_chars=N)`.
    """
    return _backend_call(
        path,
        lambda b: _truncate_result_input_form(
            b.evaluate(path, code, eval_timeout=eval_timeout),
            max_chars=max_response_chars,
        ),
        timeout=timeout,
    )


@mcp.tool()
def notebook_eval_inline(
    path: str,
    anchor_cell_id: int | str,
    code: str,
    cell_type: str = "Code",
    timeout: float = 60.0,
    eval_timeout: float | None = None,
    max_response_chars: int = _RESULT_INPUTFORM_MAX_CHARS,
) -> dict:
    """Insert a Code cell after `anchor_cell_id`, evaluate it, return the result.

    Single-call "let me try this" gesture. In collab mode, the Input + Output
    appear in the live notebook so the user sees what you tried. The cell stays
    in the file/notebook — `notebook_delete_cell` it later if you want to clean up.

    `eval_timeout` (seconds) wraps the eval in TimeConstrained kernel-side; on
    expiry, status="timeout". `max_response_chars` caps the textual result.
    """
    return _backend_call(
        path,
        lambda b: _truncate_result_input_form(
            b.eval_inline(
                path, anchor_cell_id, code, cell_type, eval_timeout=eval_timeout
            ),
            max_chars=max_response_chars,
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
_DOCUMENTATION_CANDIDATE_MIN = 15
_DOCUMENTATION_CANDIDATE_PER_RESULT = 5
_DOCUMENTATION_CANDIDATE_MAX = 75
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
    """Guard against mojibake / NFKC issues in doc/usage strings.

    The kernel-side `normalizeDisplayText` handles the bulk of Wolfram-
    specific replacements (\\[Ellipsis], \\[Rule], etc.). This is a thin
    safety net for transport-layer encoding glitches and lingering
    compatibility-form codepoints.
    """
    if not isinstance(text, str):
        return text
    normalized = text
    if any(marker in normalized for marker in ("Ã", "â", "Î", "ï", "ð")):
        with suppress(UnicodeEncodeError, UnicodeDecodeError):
            normalized = normalized.encode("latin-1").decode("utf-8")
    normalized = unicodedata.normalize("NFKC", normalized)
    return normalized.strip()


def _wl_string(value: str) -> str:
    return json.dumps(value)


def _wl_string_list(values: list[str]) -> str:
    if not values:
        return "{}"
    return "{" + ", ".join(json.dumps(v) for v in values) + "}"


def _symbol_list_expr(expr: str) -> str:
    return f'If[Length[{expr}] > 0, ToString[{expr}, InputForm], "None"]'


def _documentation_candidate_limit(max_results: int) -> int:
    return min(
        _DOCUMENTATION_CANDIDATE_MAX,
        max(
            _DOCUMENTATION_CANDIDATE_MIN,
            int(max_results) * _DOCUMENTATION_CANDIDATE_PER_RESULT,
        ),
    )


def _documentation_tokens(query: str) -> list[str]:
    """Split `query` into search tokens, preserving the user's case.

    The WL side uses `StringContainsQ[..., IgnoreCase -> True]` for the
    actual match, so we never need to manufacture case variants — case is
    handled at the matching site. Earlier versions lowercased the query
    here, which silently destroyed information for mixed-case names like
    `"Plot3D"` (became `"plot3d"` → `"Plot3d"`, never matching the real
    symbol).
    """
    raw = _re.findall(r"[A-Za-z$][A-Za-z0-9$]*", query)
    out: list[str] = []
    seen: set[str] = set()
    for tok in raw:
        if len(tok) < 3 or tok.lower() in _DOCUMENTATION_STOP_WORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    if not out:
        # Fall back to whatever was in the query so a short or unusual
        # input still gets one search attempt (otherwise we'd return zero
        # tokens and the kernel-side search would match nothing).
        stripped = query.strip()
        if stripped:
            out.append(stripped)
    return out


def _normalize_context_query(context: str) -> tuple[str, str]:
    n = context.strip()
    if not n:
        raise ValueError("Context must be non-empty")
    if "*" in n or "@" in n:
        return n, n
    if not n.endswith("`"):
        n += "`"
    return n, f"{n}*"


def _find_cell_payload(cells: list[dict], cell_id: int | str) -> dict:
    for c in cells:
        if c.get("cellID") == cell_id or str(c.get("cellID")) == str(cell_id):
            return c
    raise ValueError(f"cellID {cell_id} not found")


def _run_cell_in_kernel_payload(
    *,
    path: str,
    source_mode: str,
    cell: dict,
    kernel_id: str,
    eval_timeout: float | None = None,
) -> dict:
    if cell.get("style") not in _EXECUTABLE_CELL_TYPES:
        return {
            "status": "skipped",
            "path": path,
            "mode": "scratch",
            "source_mode": source_mode,
            "kernel_id": _validate_kernel_id(kernel_id),
            "cellID": cell.get("cellID"),
            "index": cell.get("index"),
            "reason": "not_executable",
            "cellType": cell.get("style"),
            "visible_output": False,
        }
    result = _kernel_eval_payload(
        _manager_factory(),
        kernel_id,
        cell.get("content", ""),
        eval_timeout=eval_timeout,
    )
    result.update(
        {
            "path": path,
            "mode": "scratch",
            "source_mode": source_mode,
            "cellID": cell.get("cellID"),
            "index": cell.get("index"),
            "style": cell.get("style"),
            "visible_output": False,
        }
    )
    return result


@mcp.tool()
def kernel_list() -> dict:
    """List agent-owned Wolfram kernels managed by the MCP process.

    This does not include user-owned collaborative notebook kernels; those are
    addressed by notebook path through the `notebook_*` tools.
    """
    if _manager is None:
        return {
            "status": "ok",
            "kernel_count": 0,
            "kernels": [],
            "note": "No agent-owned kernel manager has been started yet.",
        }
    return {
        "status": "ok",
        "kernel_count": len(_manager.list_sessions()),
        "kernels": [_session_payload(info) for info in _manager.list_sessions()],
    }


@mcp.tool()
def kernel_create(kernel_id: str | None = None) -> dict:
    """Create an explicit agent-owned scratch kernel.

    If `kernel_id` is omitted, a `scratch-<hex>` ID is generated. Scratch
    kernels are isolated from the lazy default `main` solo kernel and from any
    user-owned collab notebook kernel.
    """
    def _do():
        manager = _manager_factory()
        if kernel_id is None:
            existing = {info.name for info in manager.list_sessions()}
            while True:
                kid = f"scratch-{uuid4().hex[:8]}"
                if kid not in existing:
                    break
        else:
            kid = _validate_kernel_id(kernel_id)
        manager.create_session(kid)
        info = next(
            (i for i in manager.list_sessions() if i.name == kid),
            None,
        )
        return {"status": "ok", "kernel": _session_payload(info)}

    return _kernel_call(_do)


@mcp.tool()
def kernel_close(kernel_id: str) -> dict:
    """Close an agent-owned scratch kernel.

    The built-in `main` kernel cannot be closed; use `kernel_restart("main")`
    if you need a fresh default solo kernel.
    """
    def _do():
        if _manager is None:
            raise ValueError("No agent-owned kernel manager has been started")
        kid = _validate_kernel_id(kernel_id)
        _manager.close_session(kid)
        return {"status": "ok", "closed": kid}

    return _kernel_call(_do)


@mcp.tool()
def kernel_restart(kernel_id: str = "main") -> dict:
    """Restart an agent-owned kernel, clearing its definitions and outputs."""
    def _do():
        if _manager is None:
            raise ValueError("No agent-owned kernel manager has been started")
        kid = _validate_kernel_id(kernel_id)
        _manager.restart_session(kid)
        return {"status": "ok", "restarted": kid}

    return _kernel_call(_do)


@mcp.tool()
def kernel_abort(kernel_id: str, signal: str = "SIGINT") -> dict:
    """Signal an agent-owned kernel to abort its current evaluation."""
    def _do():
        if _manager is None:
            raise ValueError("No agent-owned kernel manager has been started")
        kid = _validate_kernel_id(kernel_id)
        pid = _manager.abort_session(kid, signal=signal)
        return {"status": "ok", "kernel_id": kid, "pid": pid, "signal": signal.upper()}

    return _kernel_call(_do)


@mcp.tool()
def kernel_eval(
    kernel_id: str,
    code: str,
    eval_timeout: float | None = None,
    summary_max: int = 500,
) -> dict:
    """Evaluate Wolfram Language code in an explicit agent-owned kernel.

    Use this for scratch probes that should not modify the user's shared
    notebook kernel. Create a scratch kernel first with `kernel_create`.
    """
    return _kernel_call(
        lambda: _kernel_eval_payload(
            _manager_factory(),
            kernel_id,
            code,
            eval_timeout=eval_timeout,
            summary_max=summary_max,
        )
    )


@mcp.tool()
def kernel_eval_json(
    kernel_id: str,
    code: str,
    eval_timeout: float | None = None,
) -> dict:
    """Evaluate WL code in an agent-owned kernel and return RawJSON-decoded output.

    Possible `status` values:
    - `"ok"` — response carries `result` (the decoded JSON value) AND
      `inputForm` (full-precision string). Prefer `inputForm` when the value
      may contain bignum integers, high-precision reals, or rationals — the
      JSON-RPC transport is float64, so e.g. `2^200` round-trips through
      `result` as `1.6e+60` (lossy) but `inputForm` carries the exact
      `"1606938...0376"`.
    - `"parse_error"` — the WL code did not parse.
    - `"timeout"` — evaluation exceeded `eval_timeout` (or the default).
    - `"not_json_encodable"` — the evaluated value (e.g. a `Symbol`,
      `Graphics`, pure function) has no JSON representation. Response carries
      `head` and `inputForm` so you can see what came back; if you only need
      a textual summary, call `kernel_eval` instead.
    """
    def _do():
        kid = _validate_kernel_id(kernel_id)
        envelope = _kernel_eval_json(
            _manager_factory(),
            kernel_id,
            code,
            eval_timeout=eval_timeout,
        )
        status = envelope["status"]
        payload: dict = {"status": status, "kernel_id": kid}
        if status == "ok":
            value = envelope.get("value")
            # Cap the JSON-serialized result so a huge value (e.g. Range[10^6]
            # — millions of elements) doesn't overflow the MCP transport's
            # per-message limit. Callers can fall back to `inputForm` (also
            # truncated to 2000 chars kernel-side) for a textual summary.
            try:
                serialized = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                serialized = None
            if serialized is not None and len(serialized) > _RESULT_JSON_MAX_CHARS:
                payload["result"] = None
                payload["resultTruncated"] = True
                payload["resultChars"] = len(serialized)
            else:
                payload["result"] = value
            payload["inputForm"] = envelope.get("inputForm")
        elif status == "not_json_encodable":
            payload["head"] = envelope.get("head")
            payload["inputForm"] = envelope.get("inputForm")
        return payload

    return _kernel_call(_do)


@mcp.tool()
def kernel_state(
    kernel_id: str,
    fields: list[str] | None = None,
) -> dict:
    """Return structured state for an explicit agent-owned kernel."""
    def _do():
        manager = _manager_factory()
        kid = _validate_kernel_id(kernel_id)
        if fields is None:
            wanted = list(_DEFAULT_KERNEL_STATE_FIELDS)
        else:
            invalid = sorted(set(fields) - set(_KERNEL_STATE_FIELDS))
            if invalid:
                raise ValueError(
                    f"Unsupported kernel_state fields: {', '.join(invalid)}"
                )
            wanted = list(fields)
        selected_exprs = ", ".join(
            f'"{name}" -> {_KERNEL_STATE_FIELDS[name]}' for name in wanted
        )
        expr = f"<|{selected_exprs}|>"
        envelope = _kernel_eval_json(manager, kid, expr)
        return {
            "status": "ok",
            "kernel_id": kid,
            "state": envelope.get("value"),
        }

    return _kernel_call(_do)


@mcp.tool()
def kernel_get_output(
    kernel_id: str,
    out_number: int,
    view: str = "summary",
    max_chars: int = 2000,
    depth: int = 3,
) -> dict:
    """Inspect a previous result stored by `kernel_eval` in a scratch kernel.

    Defaults to `view="summary"` (head/leaf_count/byte_count, ~50 bytes) so a
    casual lookup doesn't pull a big value into your context. Call again with
    `view="full"` or `view="short"` once you've decided it's worth fetching.

    `view` mirrors `notebook_get_output`: `"full"` returns InputForm (truncated
    at `max_chars`), `"short"` returns `Shallow` at `depth`, `"summary"`
    returns head / leaf_count / depth / byte_count / dimensions.
    """
    def _do():
        if view not in _GET_OUTPUT_VIEWS:
            raise ValueError(
                f"Unsupported view '{view}'. Use one of: {sorted(_GET_OUTPUT_VIEWS)}"
            )
        kid = _validate_kernel_id(kernel_id)
        manager = _manager_factory()
        ref = f"wolfram$mcp$out[{int(out_number)}]"
        if view == "summary":
            expr = dedent(
                f"""
                With[{{res = {ref}}},
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
            envelope = _kernel_eval_json(manager, kid, expr)
            return {
                "status": "ok",
                "kernel_id": kid,
                "out_number": out_number,
                "view": view,
                "summary": envelope.get("value"),
            }
        if view == "full":
            wl = f"ToString[{ref}, InputForm]"
        else:
            wl = f"ToString[Shallow[{ref}, {{{depth}, 3}}], OutputForm]"
        raw = manager.evaluate_raw(wl, session_name=kid)
        is_truncated = len(raw) > max_chars
        if is_truncated:
            raw = raw[:max_chars] + "..."
        result = {
            "status": "ok",
            "kernel_id": kid,
            "out_number": out_number,
            "view": view,
            "output": raw,
            "is_truncated": is_truncated,
        }
        if view == "short":
            result["depth"] = depth
        return result

    return _kernel_call(_do)


@mcp.tool()
def notebook_run_cells(
    path: str,
    cells: list[int | str] | None = None,
    start: int | None = None,
    end: int | None = None,
    all: bool = False,
    stop_on_error: bool = True,
    timeout: float = 60.0,
    eval_timeout: float | None = None,
    kernel_id: str | None = None,
    max_response_chars: int = _RESULT_INPUTFORM_MAX_CHARS,
) -> dict:
    """Run multiple cells in order in a single MCP call.

    Cell selection (one of):
    - `cells=[id1, id2, ...]`: explicit opaque cell IDs (in the order to run).
    - `start` / `end`: 1-indexed positions in the read order (inclusive).
    - `all=True`: run every executable (Input/Code) cell.

    Returns a list of per-cell result dicts. With `stop_on_error=True` (default),
    halts on the first failure. Faster than looping `notebook_run_cell` because
    selection happens in one read instead of per call.

    `eval_timeout` (seconds) applies to each cell individually via TimeConstrained.
    If `kernel_id` is provided, selected cells are evaluated in that agent-owned
    scratch kernel and no visible notebook Output is written.
    `max_response_chars` caps each per-cell textual result; with N cells your
    aggregate response is roughly N × max_response_chars. Default ~1500.
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
            by_id = {str(c.get("cellID")): c for c in all_cells}
            missing = [cid for cid in wanted if str(cid) not in by_id]
            if missing:
                raise ValueError(f"cellID(s) not found: {missing}")
            selected = [by_id[str(cid)] for cid in wanted]
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
            if kernel_id is None:
                r = backend.run_cell(path, c["cellID"], eval_timeout=eval_timeout)
            else:
                r = _run_cell_in_kernel_payload(
                    path=path,
                    source_mode=backend.mode,
                    cell=c,
                    kernel_id=kernel_id,
                    eval_timeout=eval_timeout,
                )
            r["index"] = c.get("index")
            r["style"] = c.get("style")
            results.append(r)
            if stop_on_error and r.get("status") not in ("ok", "skipped"):
                stopped_early = True
                break

        payload = {
            "status": "ok",
            "path": path,
            "result_count": len(results),
            "stopped_early": stopped_early,
            "results": results,
        }
        if kernel_id is None:
            payload["mode"] = backend.mode
        else:
            payload.update(
                {
                    "mode": "scratch",
                    "source_mode": backend.mode,
                    "kernel_id": _validate_kernel_id(kernel_id),
                    "visible_output": False,
                }
            )
        return _truncate_result_input_form(payload, max_chars=max_response_chars)

    return _backend_call(path, _do, timeout=timeout)


@mcp.tool()
def notebook_abort_evaluation(
    path: str,
    signal: str = "SIGINT",
    timeout: float = 10.0,
) -> dict:
    """Signal the kernel to abort its current evaluation. Best-effort, not
    fully autonomous.

    Prefer `eval_timeout` on the call you're about to make — that's the
    headless cross-platform mechanism. Use `notebook_abort_evaluation` only
    when an eval is already in flight and the socket bridge can't reach it.

    Behavior depends on mode and OS:
    - **Solo mode, POSIX (Linux/macOS):** SIGINT is silent — the kernel calls
      `Abort[]` when it reaches an interruptible point. Headless.
    - **Solo mode, Windows:** POSIX signal semantics differ; this is not
      reliable. Use `eval_timeout` instead.
    - **Collab mode, any OS:** signals the user's kernel, but the GUI front-end
      typically intercepts SIGINT and pops a "Continue / Abort" dialog the user
      must dismiss. Treat as "tap on the user's shoulder," not a clean abort.

    SIGTERM is available but more forceful and may cause the kernel to exit;
    only use it if SIGINT didn't work and you're prepared to lose kernel state.

    """
    def _do(backend):
        return {
            "mode": backend.mode,
            **backend.abort_evaluation(signal=signal),
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
    # Check Names[name] FIRST — Names doesn't create symbols, but
    # ToExpression does (a previously-unknown bare name would otherwise be
    # interned in Global`, silently polluting the user's kernel). Only
    # proceed if at least one symbol matching `name` already exists on
    # $ContextPath, so the lookup stays side-effect-free.
    expr = dedent(
        f"""
        Module[{{}},
            {_TEXT_HELPERS_WL}
            With[{{matches = Names[{_wl_string(name)}]}},
                If[matches === {{}},
                    <|"error" -> "symbol_not_found"|>,
                    With[{{held = ToExpression[{_wl_string(name)}, InputForm, HoldComplete]}},
                        If[
                            MatchQ[held, HoldComplete[_Symbol]],
                            Replace[
                                held,
                                HoldComplete[s_Symbol] :> <|{selected}|>
                            ],
                            <|"error" -> "name_did_not_resolve_to_symbol"|>
                        ]
                    ]
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
    candidate_limit = _documentation_candidate_limit(limit)
    tokens = _documentation_tokens(q)

    expr = dedent(
        f"""
        Module[{{}},
            {_TEXT_HELPERS_WL}
            Module[
                {{
                    query = {_wl_string(q)},
                    queryTokens = {_wl_string_list(tokens)},
                    candidateLimit = {int(candidate_limit)},
                    candidates,
                    results
                }},
                (* Single Names[] call across System`; filter case-insensitively
                   against the user's raw query and each whitespace token. The
                   earlier implementation called Names["System`*<term>*"] per
                   case-permuted variant, which is case-sensitive and missed
                   mixed-case names like Plot3D. *)
                candidates = Select[
                    Names["System`*"],
                    Function[n,
                        Or[
                            StringContainsQ[n, query, IgnoreCase -> True],
                            AnyTrue[queryTokens, StringContainsQ[n, #, IgnoreCase -> True] &]
                        ]
                    ]
                ];
                (* WolframLanguageData calls dominate runtime. Bound the heavy
                   metadata pass after a cheap name-rank sort so short/common
                   queries like "3D" or "in" cannot trigger thousands of
                   documentation lookups before max_results is applied. *)
                candidates = Take[
                    SortBy[
                        candidates,
                        Function[n,
                            {{
                                If[ToLowerCase[n] === ToLowerCase[query], 0, 1],
                                -Count[queryTokens, t_ /; StringContainsQ[n, t, IgnoreCase -> True]],
                                If[StringContainsQ[n, query, IgnoreCase -> True], 0, 1],
                                StringLength[n],
                                n
                            }}
                        ]
                    ],
                    UpTo[candidateLimit]
                ];
                results = Map[
                    Function[name,
                        Module[
                            {{entity, usage, url, related,
                              exactNameHit, nameHit, tokenNameHits,
                              matchSources = {{}}, usageHit, tokenUsageHits,
                              usagePos}},
                            entity = Entity["WolframLanguageSymbol", name];
                            usage = Replace[
                                Quiet[Check[WolframLanguageData[entity, "PlaintextUsage"], ""]],
                                _Missing -> ""
                            ];
                            usage = normalizeDisplayText[usage];
                            url = Replace[
                                Quiet[Check[WolframLanguageData[entity, "URL"], Missing["NotAvailable"]]],
                                _Missing -> Missing["NotAvailable"]
                            ];
                            related = Replace[
                                Quiet[Check[WolframLanguageData[entity, "RelatedSymbols"], {{}}]],
                                _Missing -> {{}}
                            ];
                            exactNameHit = ToLowerCase[name] === ToLowerCase[query];
                            nameHit = StringContainsQ[name, query, IgnoreCase -> True];
                            usageHit = usage =!= "" && StringContainsQ[usage, query, IgnoreCase -> True];
                            tokenNameHits = Count[
                                queryTokens,
                                t_ /; StringContainsQ[name, t, IgnoreCase -> True]
                            ];
                            tokenUsageHits = Count[
                                queryTokens,
                                t_ /; usage =!= "" && StringContainsQ[usage, t, IgnoreCase -> True]
                            ];
                            If[exactNameHit, AppendTo[matchSources, "exact_name"]];
                            If[nameHit && !exactNameHit, AppendTo[matchSources, "name"]];
                            If[usageHit, AppendTo[matchSources, "usage"]];
                            If[tokenNameHits > 0 && !nameHit, AppendTo[matchSources, "name_tokens"]];
                            If[tokenUsageHits > 0 && !usageHit, AppendTo[matchSources, "usage_tokens"]];
                            usagePos = If[
                                usageHit,
                                First[First[StringPosition[ToLowerCase[usage], ToLowerCase[query]]]],
                                10^9
                            ];
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
                    ],
                    candidates
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
def notebook_list_symbols(
    path: str,
    context: str = "Global`",
    limit: int = 500,
    timeout: float = 30.0,
) -> dict:
    """List symbols for a context (e.g., 'Global`') or a `Names` pattern.

    Accepts either a context name like 'Global`' (auto-expanded to 'Global`*')
    or a literal Names pattern such as 'Plot*' or '*Integrate*'.

    Caps at `limit` symbols (default 500) to keep the response under MCP
    transport size limits — wide patterns like `System`*` would otherwise
    return ~9000 symbols (~130k chars). When truncated, `count` reflects the
    total available and `symbols` is the first `limit` of them.
    """
    try:
        normalized, pattern = _normalize_context_query(context)
    except ValueError as exc:
        return {"error": str(exc)}
    if limit < 1:
        return {"error": "limit must be >= 1"}
    # Sort + Take kernel-side so the trimmed-down list flows over the wire,
    # not the full ~130k-char System` list.
    expr = (
        f"With[{{wolfram$mcp$names = Sort[Names[{_wl_string(pattern)}]]}}, "
        f"{{Length[wolfram$mcp$names], Take[wolfram$mcp$names, UpTo[{int(limit)}]]}}]"
    )

    def _do(backend):
        payload = backend.evaluate_for_json(expr)
        if isinstance(payload, list) and len(payload) == 2:
            total, symbols = payload
        else:
            total, symbols = 0, []
        result = {
            "context": normalized,
            "pattern": pattern,
            "count": total,
            "symbols": symbols,
        }
        if isinstance(total, int) and total > len(symbols):
            result["truncated"] = True
            result["returnedCount"] = len(symbols)
        return result

    return _backend_call(path, _do, timeout=timeout)


@mcp.tool()
def notebook_get_output(
    path: str,
    out_number: int,
    view: str = "summary",
    max_chars: int = 2000,
    depth: int = 3,
    timeout: float = 30.0,
) -> dict:
    """Inspect a previous evaluation's `Out[n]` value in the file's collab/solo kernel.

    Default `view="summary"` returns only `{head, leaf_count, depth, byte_count,
    dimensions}` — ~50 bytes, free to call. Use it to decide whether the value
    is worth fetching before paying for `view="full"`.

    `view`:
    - `"summary"` (default): structured `{head, leaf_count, depth, byte_count, dimensions}`.
    - `"short"`: `Shallow` rendering at `depth` (truncated at `max_chars`).
    - `"full"`: full InputForm string (truncated at `max_chars`).

    For results stored in a scratch kernel by `kernel_eval`, use
    `kernel_get_output(kernel_id, out_number, view=...)` — same `view`
    semantics but no path.
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
        render_expr = f"ToString[Out[{int(out_number)}], InputForm]"
    else:
        render_expr = f"ToString[Shallow[Out[{int(out_number)}], {{{int(depth)}, 3}}], OutputForm]"
    limit = max(0, int(max_chars))
    wl = dedent(
        f"""
        Module[{{text = {render_expr}}},
            <|
                "output" -> StringTake[text, UpTo[{limit}]],
                "chars" -> StringLength[text]
            |>
        ]
        """
    )

    def _do(backend):
        payload = backend.evaluate_for_json(wl)
        raw = payload.get("output") if isinstance(payload, dict) else None
        chars = payload.get("chars") if isinstance(payload, dict) else None
        if not isinstance(raw, str) or not isinstance(chars, int):
            return {"out_number": out_number, "view": view, "error": "no_result", "payload": payload}
        is_truncated = chars > len(raw)
        if is_truncated:
            raw = raw + "..."
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
