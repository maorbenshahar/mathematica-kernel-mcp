"""Unified backends for cell-level notebook operations and WL queries.

Two backends share an interface so the LLM-facing `notebook_*` tools can dispatch
based on whether a shared-kernel bridge is present:

- `BridgeBackend` (collaborative): the user has Mathematica open with the file +
  `StartSharedKernelBridge[...]` evaluated. We talk to the user's kernel via the
  file-based queue/results protocol; edits land live in their open notebook;
  kernel state is shared.
- `SoloBackend` (solo): no bridge. We mutate the `.m`/`.nb` file on disk and run
  code in a kernel the MCP spawns via wolframclient. The user is not involved.

A `get_backend_for(path)` dispatcher picks the right backend based on whether
`<file_dir>/.shared_kernel_bridge/queue/` exists next to the target file.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

from .bridge import SharedKernelBridge
from .notebook import (
    create_nb_cell,
    delete_nb_cell,
    is_notebook_path,
    parse_nb_file_with_kernel,
    update_nb_cell,
)
from .parser import create_m_cell, delete_m_cell, parse_file, update_m_cell


class Backend(ABC):
    """Common interface for collaborative and solo cell-level operations."""

    mode: str  # "collab" | "solo"

    @abstractmethod
    def read(self, path: str) -> dict: ...

    @abstractmethod
    def run_cell(self, path: str, cell_id) -> dict: ...

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
    def evaluate(self, path: str, code: str) -> dict: ...

    @abstractmethod
    def eval_inline(
        self, path: str, anchor_cell_id, code: str, cell_type: str = "Code"
    ) -> dict: ...

    @abstractmethod
    def sweep_outputs(self, path: str) -> dict: ...

    @abstractmethod
    def evaluate_for_json(self, code: str): ...
    """Evaluate a WL expression that produces a JSON-serializable result, return Python object."""

    @abstractmethod
    def kernel_restart(self) -> dict: ...
    """Restart the underlying kernel. In collab mode this is refused (it would
    clobber the user's session). In solo mode it restarts the spawned kernel."""


# ---------------------------------------------------------------------------
# Bridge (collaborative) backend
# ---------------------------------------------------------------------------


class BridgeBackend(Backend):
    mode = "collab"

    def __init__(self, path: str, timeout: float = 30.0):
        self.bridge = SharedKernelBridge.for_file(path, timeout=timeout)

    def read(self, path: str) -> dict:
        return self.bridge.read_notebook(path)

    def run_cell(self, path: str, cell_id) -> dict:
        return self.bridge.run_cell(path, int(cell_id))

    def update_cell(self, path, cell_id, cell_type, content):
        return self.bridge.update_cell(path, int(cell_id), cell_type, content)

    def insert_cell_after(self, path, anchor_cell_id, cell_type, content):
        return self.bridge.insert_cell_after(path, int(anchor_cell_id), cell_type, content)

    def insert_cell_before(self, path, anchor_cell_id, cell_type, content):
        return self.bridge.insert_cell_before(path, int(anchor_cell_id), cell_type, content)

    def delete_cell(self, path, cell_id):
        return self.bridge.delete_cell(path, int(cell_id))

    def evaluate(self, path, code):
        return self.bridge.evaluate(code)

    def eval_inline(self, path, anchor_cell_id, code, cell_type="Code"):
        ins = self.bridge.insert_cell_after(path, int(anchor_cell_id), cell_type, code)
        new_id = ins.get("newCellID")
        if new_id is None:
            return {"error": "insert_failed", "insert_result": ins}
        run = self.bridge.run_cell(path, int(new_id))
        return {
            "status": run.get("status"),
            "newCellID": new_id,
            "anchorCellID": int(anchor_cell_id),
            "resultInputForm": run.get("resultInputForm"),
            "messages": run.get("messages", []),
            "prints": run.get("prints", []),
            "durationSeconds": run.get("durationSeconds"),
        }

    def sweep_outputs(self, path):
        return self.bridge.sweep_stale_outputs(path)

    def evaluate_for_json(self, code):
        return self.bridge.call_for_json(code)

    def kernel_restart(self) -> dict:
        return {
            "status": "refused",
            "reason": "collab_mode_kernel_owned_by_user",
            "message": (
                "Cannot restart the user's shared kernel from the MCP — that would "
                "clobber their session. Ask the user to Quit Kernel themselves if needed."
            ),
        }


# ---------------------------------------------------------------------------
# Solo backend
# ---------------------------------------------------------------------------


def _cell_to_payload(cell) -> dict:
    """Render a parser `Cell` as the same dict shape BridgeBackend returns."""
    return {
        "index": cell.number,
        "cellID": cell.number,  # 1-indexed position; stable within a single read
        "style": cell.cell_type,
        "content": cell.content,
    }


class SoloBackend(Backend):
    mode = "solo"

    def __init__(self, manager):
        # manager: SessionManager (lazy-started by the caller)
        self.manager = manager

    def _parse(self, path: str):
        if is_notebook_path(path):
            return parse_nb_file_with_kernel(path, self.manager)
        return parse_file(path)

    def _resolve(self, cells, cell_id):
        cid = int(cell_id)
        for c in cells:
            if c.number == cid:
                return c
        raise ValueError(f"cell {cid} not found (file has {len(cells)} cells)")

    def read(self, path):
        cells = self._parse(path)
        return {
            "status": "ok",
            "path": path,
            "cells": [_cell_to_payload(c) for c in cells],
        }

    def run_cell(self, path, cell_id):
        cells = self._parse(path)
        cell = self._resolve(cells, cell_id)
        if cell.cell_type not in {"Input", "Code"}:
            return {
                "status": "skipped",
                "cellID": cell.number,
                "reason": "not_executable",
                "cellType": cell.cell_type,
            }
        result = self.manager.evaluate(cell.content)
        return {
            "status": "ok",
            "cellID": cell.number,
            "resultInputForm": result.output_summary,
            "messages": result.messages,
            "in_number": result.in_number,
            "out_number": result.out_number,
            "head": result.head,
            "byte_size": result.byte_size,
            "leaf_count": result.leaf_count,
            "is_truncated": result.is_truncated,
        }

    def _mutating_helpers(self, path):
        if is_notebook_path(path):
            return create_nb_cell, update_nb_cell, delete_nb_cell, True
        return create_m_cell, update_m_cell, delete_m_cell, False

    def update_cell(self, path, cell_id, cell_type, content):
        _, update, _, is_nb = self._mutating_helpers(path)
        if is_nb:
            new_cell = update(
                path, self.manager, cell_number=int(cell_id),
                content=content, cell_type=cell_type,
            )
        else:
            new_cell = update(
                path, cell_number=int(cell_id), content=content, cell_type=cell_type
            )
        return {"status": "ok", "cellID": new_cell.number, "cellType": new_cell.cell_type}

    def insert_cell_after(self, path, anchor_cell_id, cell_type, content):
        create, _, _, is_nb = self._mutating_helpers(path)
        if is_nb:
            new_cell = create(
                path, self.manager, cell_type, content,
                after_cell=int(anchor_cell_id),
            )
        else:
            new_cell = create(
                path, cell_type, content, after_cell=int(anchor_cell_id)
            )
        return {
            "status": "ok",
            "newCellID": new_cell.number,
            "anchorCellID": int(anchor_cell_id),
            "position": "After",
        }

    def insert_cell_before(self, path, anchor_cell_id, cell_type, content):
        create, _, _, is_nb = self._mutating_helpers(path)
        if is_nb:
            new_cell = create(
                path, self.manager, cell_type, content,
                before_cell=int(anchor_cell_id),
            )
        else:
            new_cell = create(
                path, cell_type, content, before_cell=int(anchor_cell_id)
            )
        return {
            "status": "ok",
            "newCellID": new_cell.number,
            "anchorCellID": int(anchor_cell_id),
            "position": "Before",
        }

    def delete_cell(self, path, cell_id):
        _, _, delete, is_nb = self._mutating_helpers(path)
        if is_nb:
            removed = delete(path, self.manager, cell_number=int(cell_id))
        else:
            removed = delete(path, cell_number=int(cell_id))
        return {"status": "ok", "deletedCellID": removed.number}

    def evaluate(self, path, code):
        result = self.manager.evaluate(code)
        return {
            "status": "ok",
            "code": code,
            "resultInputForm": result.output_summary,
            "messages": result.messages,
            "in_number": result.in_number,
            "out_number": result.out_number,
            "head": result.head,
            "is_truncated": result.is_truncated,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def eval_inline(self, path, anchor_cell_id, code, cell_type="Code"):
        # Insert + run + return. The Input cell is persisted in the file (.m/.nb).
        ins = self.insert_cell_after(path, anchor_cell_id, cell_type, code)
        new_id = ins.get("newCellID")
        if new_id is None:
            return {"error": "insert_failed", "insert_result": ins}
        run = self.run_cell(path, new_id)
        return {
            "status": run.get("status"),
            "newCellID": new_id,
            "anchorCellID": int(anchor_cell_id),
            "resultInputForm": run.get("resultInputForm"),
            "messages": run.get("messages", []),
        }

    def sweep_outputs(self, path):
        # No-op in solo mode: .m files don't persist Output cells; .nb files
        # could in principle, but cleanup is the user's call there. Treat as a
        # success that did nothing rather than failing.
        return {"status": "ok", "swept_count": 0, "note": "solo mode no-op"}

    def evaluate_for_json(self, code):
        raw = self.manager.evaluate_raw(f'ExportString[({code}), "RawJSON"]')
        return json.loads(raw)

    def kernel_restart(self) -> dict:
        self.manager.restart_session("main")
        return {"status": "ok", "restarted": "main"}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def has_bridge_for(path: str) -> bool:
    p = Path(path).resolve()
    bridge_root = p.parent / ".shared_kernel_bridge" / p.name
    return (bridge_root / "queue").exists() and (bridge_root / "results").exists()


def get_backend_for(path: str, manager_factory, timeout: float = 30.0) -> Backend:
    """Return the right Backend for a file path.

    `manager_factory` is a zero-arg callable that returns a SessionManager. It's
    only invoked when solo mode is selected, so collab-mode usage doesn't pay
    the cost of spawning a kernel.
    """
    if has_bridge_for(path):
        return BridgeBackend(path, timeout=timeout)
    return SoloBackend(manager_factory())
