"""Unified backends for cell-level notebook operations and WL queries.

Two backends share an interface so the LLM-facing `notebook_*` tools can dispatch
based on whether a shared-kernel bridge is present:

- `BridgeBackend` (collaborative): the user has Mathematica open with the file +
  `StartSharedKernelBridge[...]` evaluated. We talk to the user's kernel via the
  authenticated socket bridge; edits land live in their open notebook; kernel
  state is shared. Works for both `.m`/`.wl` and `.nb` files.
- `SoloBackend` (solo): no bridge. We mutate the `.m`/`.wl` file on disk and run
  code in a kernel the MCP spawns via wolframclient. The user is not involved.
  `.nb` files require collab mode; the dispatcher refuses them in solo.

A `get_backend_for(path)` dispatcher picks the right backend based on whether
a matching bridge exists in the global bridge registry.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from .bridge import BridgeError, SharedKernelBridge, bridge_record_for_file
from .parser import (
    CellMarkerCollisionError,
    CommentNestingError,
    StaleCellReferenceError,
    create_m_cell,
    delete_m_cell,
    parse_file,
    resolve_m_cell,
    update_m_cell,
)


_WRITE_REFUSAL_ERRORS = (CellMarkerCollisionError, CommentNestingError)


_NOTEBOOK_EXTENSION = ".nb"


def _is_notebook_path(path: str) -> bool:
    return Path(path).suffix.lower() == _NOTEBOOK_EXTENSION


class Backend(ABC):
    """Common interface for collaborative and solo cell-level operations."""

    mode: str  # "collab" | "solo"

    @abstractmethod
    def read(
        self,
        path: str,
        *,
        include_content: bool = True,
        preview_chars: int = 80,
    ) -> dict: ...

    @abstractmethod
    def run_cell(
        self, path: str, cell_id, eval_timeout: float | None = None
    ) -> dict: ...

    @abstractmethod
    def update_cell(
        self, path: str, cell_id, cell_type: str, content: str
    ) -> dict: ...

    @abstractmethod
    def insert_cell_after(
        self, path: str, anchor_cell_id, cell_type: str, content: str
    ) -> dict: ...

    @abstractmethod
    def insert_cell_before(
        self, path: str, anchor_cell_id, cell_type: str, content: str
    ) -> dict: ...

    @abstractmethod
    def delete_cell(self, path: str, cell_id) -> dict: ...

    @abstractmethod
    def evaluate(
        self, path: str, code: str, eval_timeout: float | None = None
    ) -> dict: ...

    @abstractmethod
    def eval_inline(
        self,
        path: str,
        anchor_cell_id,
        code: str,
        cell_type: str = "Code",
        eval_timeout: float | None = None,
    ) -> dict: ...

    @abstractmethod
    def sweep_outputs(self, path: str) -> dict: ...

    @abstractmethod
    def evaluate_for_json(self, code: str):
        """Evaluate a WL expression and return its JSON-serializable result."""
        ...

    @abstractmethod
    def abort_evaluation(self, signal: str = "SIGINT") -> dict:
        """Signal the underlying kernel to abort its current evaluation."""
        ...


# ---------------------------------------------------------------------------
# Bridge (collaborative) backend
# ---------------------------------------------------------------------------


def _integer_cell_id(value, *, label: str = "cell_id") -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{label} must be an integer CellID in collab mode, or a "
            "source ref returned by notebook_read for solo .m/.wl files"
        ) from exc


class BridgeBackend(Backend):
    mode = "collab"

    def __init__(self, path: str, timeout: float = 30.0):
        self.bridge = SharedKernelBridge.for_file(path, timeout=timeout)

    def read(
        self,
        path: str,
        *,
        include_content: bool = True,
        preview_chars: int = 80,
    ) -> dict:
        return self.bridge.read_notebook(
            path,
            include_content=include_content,
            preview_chars=preview_chars,
        )

    def run_cell(self, path: str, cell_id, eval_timeout: float | None = None) -> dict:
        return self.bridge.run_cell(
            path, _integer_cell_id(cell_id), eval_timeout=eval_timeout
        )

    def update_cell(self, path, cell_id, cell_type, content):
        return self.bridge.update_cell(
            path, _integer_cell_id(cell_id), cell_type, content
        )

    def insert_cell_after(self, path, anchor_cell_id, cell_type, content):
        return self.bridge.insert_cell_after(
            path,
            _integer_cell_id(anchor_cell_id, label="anchor_cell_id"),
            cell_type,
            content,
        )

    def insert_cell_before(self, path, anchor_cell_id, cell_type, content):
        return self.bridge.insert_cell_before(
            path,
            _integer_cell_id(anchor_cell_id, label="anchor_cell_id"),
            cell_type,
            content,
        )

    def delete_cell(self, path, cell_id):
        return self.bridge.delete_cell(path, _integer_cell_id(cell_id))

    def evaluate(self, _path, code, eval_timeout: float | None = None):
        return self.bridge.evaluate(code, eval_timeout=eval_timeout)

    def eval_inline(
        self, path, anchor_cell_id, code, cell_type="Code",
        eval_timeout: float | None = None,
    ):
        ins = self.bridge.insert_cell_after(
            path,
            _integer_cell_id(anchor_cell_id, label="anchor_cell_id"),
            cell_type,
            code,
        )
        new_id = ins.get("newCellID")
        if new_id is None:
            return {"error": "insert_failed", "insert_result": ins}
        run = self.bridge.run_cell(path, int(new_id), eval_timeout=eval_timeout)
        return {
            "status": run.get("status"),
            "newCellID": new_id,
            "anchorCellID": _integer_cell_id(anchor_cell_id, label="anchor_cell_id"),
            "resultInputForm": run.get("resultInputForm"),
            "resultInputFormTruncated": run.get("resultInputFormTruncated"),
            "resultInputFormChars": run.get("resultInputFormChars"),
            "inNumber": run.get("inNumber"),
            "outNumber": run.get("outNumber"),
            "messages": run.get("messages", []),
            "prints": run.get("prints", []),
            "durationSeconds": run.get("durationSeconds"),
        }

    def sweep_outputs(self, path):
        return self.bridge.sweep_stale_outputs(path)

    def evaluate_for_json(self, code):
        return self.bridge.call_for_json(code)

    def abort_evaluation(self, signal: str = "SIGINT") -> dict:
        return self.bridge.abort_evaluation(signal=signal)


# ---------------------------------------------------------------------------
# Solo backend
# ---------------------------------------------------------------------------


def _cell_to_payload(cell, *, source_refs: bool = False) -> dict:
    """Render a parser `Cell` as the same dict shape BridgeBackend returns."""
    payload = {
        "index": cell.number,
        "cellID": cell.cell_id if source_refs else cell.number,
        "style": cell.cell_type,
        "content": cell.content,
    }
    if source_refs:
        payload.update(
            {
                "sourceRef": cell.cell_id,
                "lineStart": cell.line_start,
                "lineEnd": cell.line_end,
            }
        )
    return payload


def _cell_selector(cell_id):
    if isinstance(cell_id, str) and cell_id.startswith("src:v1:"):
        return {"cell_id": cell_id}
    return {"cell_number": _integer_cell_id(cell_id)}


def _stale_reference_payload(exc: StaleCellReferenceError) -> dict:
    return exc.to_payload()


class SoloBackend(Backend):
    mode = "solo"

    def __init__(self, manager):
        # manager: SessionManager (lazy-started by the caller)
        self.manager = manager

    def read(
        self,
        path,
        *,
        include_content: bool = True,
        preview_chars: int = 80,
    ):
        cells = parse_file(path)
        payload_cells = [_cell_to_payload(c, source_refs=True) for c in cells]
        if not include_content:
            for c in payload_cells:
                flat = c.get("content", "").replace("\n", " ")
                c["preview"] = (
                    flat
                    if len(flat) <= preview_chars
                    else flat[: max(0, preview_chars - 3)] + "..."
                )
                c.pop("content", None)
        return {
            "status": "ok",
            "path": path,
            "cells": payload_cells,
        }

    def run_cell(self, path, cell_id, eval_timeout: float | None = None):
        try:
            cell = resolve_m_cell(path, **_cell_selector(cell_id))
        except StaleCellReferenceError as exc:
            return _stale_reference_payload(exc)

        if cell.cell_type not in {"Input", "Code"}:
            return {
                "status": "skipped",
                "cellID": cell.cell_id,
                "index": cell.number,
                "reason": "not_executable",
                "cellType": cell.cell_type,
            }
        kwargs = {"timeout": int(eval_timeout)} if eval_timeout else {}
        result = self.manager.evaluate(cell.content, **kwargs)
        return {
            "status": result.status,
            "cellID": cell.cell_id,
            "index": cell.number,
            "resultInputForm": result.output_summary,
            "messages": result.messages,
            "in_number": result.in_number,
            "out_number": result.out_number,
            "head": result.head,
            "byte_size": result.byte_size,
            "leaf_count": result.leaf_count,
            "is_truncated": result.is_truncated,
        }

    def update_cell(self, path, cell_id, cell_type, content):
        try:
            new_cell = update_m_cell(
                path,
                **_cell_selector(cell_id),
                content=content,
                cell_type=cell_type,
            )
        except StaleCellReferenceError as exc:
            return _stale_reference_payload(exc)
        except _WRITE_REFUSAL_ERRORS as exc:
            return exc.to_payload()
        return {
            "status": "ok",
            "cellID": new_cell.cell_id,
            "cellType": new_cell.cell_type,
        }

    def insert_cell_after(self, path, anchor_cell_id, cell_type, content):
        selector = _cell_selector(anchor_cell_id)
        try:
            new_cell = create_m_cell(
                path,
                cell_type,
                content,
                after_cell=selector.get("cell_number"),
                after_cell_id=selector.get("cell_id"),
            )
        except StaleCellReferenceError as exc:
            return _stale_reference_payload(exc)
        except _WRITE_REFUSAL_ERRORS as exc:
            return exc.to_payload()
        return {
            "status": "ok",
            "newCellID": new_cell.cell_id,
            "anchorCellID": anchor_cell_id,
            "position": "After",
        }

    def insert_cell_before(self, path, anchor_cell_id, cell_type, content):
        selector = _cell_selector(anchor_cell_id)
        try:
            new_cell = create_m_cell(
                path,
                cell_type,
                content,
                before_cell=selector.get("cell_number"),
                before_cell_id=selector.get("cell_id"),
            )
        except StaleCellReferenceError as exc:
            return _stale_reference_payload(exc)
        except _WRITE_REFUSAL_ERRORS as exc:
            return exc.to_payload()
        return {
            "status": "ok",
            "newCellID": new_cell.cell_id,
            "anchorCellID": anchor_cell_id,
            "position": "Before",
        }

    def delete_cell(self, path, cell_id):
        try:
            removed = delete_m_cell(path, **_cell_selector(cell_id))
        except StaleCellReferenceError as exc:
            return _stale_reference_payload(exc)
        return {"status": "ok", "deletedCellID": removed.cell_id}

    def evaluate(self, _path, code, eval_timeout: float | None = None):
        kwargs = {"timeout": int(eval_timeout)} if eval_timeout else {}
        result = self.manager.evaluate(code, **kwargs)
        return {
            "status": result.status,
            "code": code,
            "resultInputForm": result.output_summary,
            "messages": result.messages,
            "in_number": result.in_number,
            "out_number": result.out_number,
            "head": result.head,
            "is_truncated": result.is_truncated,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def eval_inline(
        self, path, anchor_cell_id, code, cell_type="Code",
        eval_timeout: float | None = None,
    ):
        # Insert + run + return. The Input cell is persisted in the .m/.wl file.
        ins = self.insert_cell_after(path, anchor_cell_id, cell_type, code)
        new_id = ins.get("newCellID")
        if new_id is None:
            return {"error": "insert_failed", "insert_result": ins}
        run = self.run_cell(path, new_id, eval_timeout=eval_timeout)
        return {
            "status": run.get("status"),
            "newCellID": new_id,
            "anchorCellID": anchor_cell_id,
            "resultInputForm": run.get("resultInputForm"),
            "messages": run.get("messages", []),
        }

    def sweep_outputs(self, _path):
        # No-op in solo mode: .m/.wl files don't persist Output cells.
        return {"status": "ok", "swept_count": 0, "note": "solo mode no-op"}

    def evaluate_for_json(self, code):
        raw = self.manager.evaluate_raw(f'ExportString[({code}), "RawJSON"]')
        # ExportString[..., "RawJSON"] returns a WL String whose codes are the
        # UTF-8 bytes of the encoded JSON; recode so non-ASCII content comes
        # back as proper Unicode rather than as bytes-as-chars.
        try:
            raw = raw.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        return json.loads(raw)

    def abort_evaluation(self, signal: str = "SIGINT") -> dict:
        pid = self.manager.abort_session("main", signal=signal)
        return {
            "status": "ok",
            "pid": pid,
            "signal": signal.upper(),
        }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def has_bridge_for(path: str) -> bool:
    return bridge_record_for_file(path, include_tokens=False) is not None


def get_backend_for(path: str, manager_factory, timeout: float = 30.0) -> Backend:
    """Return the right Backend for a file path.

    `manager_factory` is a zero-arg callable that returns a SessionManager. It's
    only invoked when solo mode is selected, so collab-mode usage doesn't pay
    the cost of spawning a kernel.

    Raises `BridgeError` for a `.nb` file with no live bridge: solo mode
    cannot mutate `.nb` files; open the notebook in Mathematica and run
    `StartSharedKernelBridge[]`.
    """
    if has_bridge_for(path):
        return BridgeBackend(path, timeout=timeout)
    if _is_notebook_path(path):
        raise BridgeError(
            f".nb files require collab mode. No live shared kernel bridge is "
            f"registered for {path!s}. Open the notebook in Mathematica and "
            "run `StartSharedKernelBridge[]`."
        )
    return SoloBackend(manager_factory())
