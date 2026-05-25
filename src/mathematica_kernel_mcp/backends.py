"""Unified backends for cell-level notebook operations and WL queries.

Two backends share an interface so the LLM-facing `notebook_*` tools can dispatch
based on whether a shared-kernel bridge is present:

- `BridgeBackend` (collaborative): the user has Mathematica open with the file +
  `StartSharedKernelBridge[...]` evaluated. We talk to the user's kernel via the
  authenticated socket bridge; edits land live in their open notebook; kernel
  state is shared. Works for both `.m`/`.wl` and `.nb` files.
- `SoloBackend` (solo): no bridge. The MCP-managed kernel attaches a temporary
  headless front-end (via `UsingFrontEnd`) and opens the file with
  `NotebookOpen[..., Visible -> False]`. Cell layout and mutations go through
  Mathematica's own notebook machinery — same source of truth as the bridge
  path. The kernel caches the headless notebook per path so CellIDs stay
  stable across calls (Mathematica's package format doesn't persist CellIDs
  to `.m`/`.wl` on disk).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from .bridge import SharedKernelBridge, bridge_record_for_file


_EXECUTABLE_CELL_TYPES = {"Input", "Code"}


def _wl_string(value: str) -> str:
    """Render a Python string as a WL string literal for inline substitution.

    `ensure_ascii=False` keeps non-ASCII codepoints as themselves (WL's lexer
    doesn't understand `\\uXXXX` — it uses `\\:NNNN`).
    """
    return json.dumps(value, ensure_ascii=False)


class SoloBackendError(RuntimeError):
    """Solo backend WL call returned a non-ok SafeEval envelope.

    Distinct from `BridgeError` so `_backend_call` in server.py can label
    these as `backend_error` (the generic fallback) instead of misleadingly
    naming a bridge that solo mode doesn't use.
    """


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
            f"{label} must be an integer CellID returned by notebook_read"
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
# Solo backend — uses Mathematica's own notebook machinery via headless FE
# ---------------------------------------------------------------------------


class SoloBackend(Backend):
    mode = "solo"

    def __init__(self, manager):
        # `manager` is the SessionManager. We call the Solo* WL primitives
        # in `init.m`, which open the file in a headless front-end attached
        # via UsingFrontEnd and forward to the same Bridge* primitives the
        # collab path uses. So the cell view matches what Mathematica's UI
        # would show, not whatever a regex over (* ::Style:: *) markers
        # happens to produce.
        self.manager = manager

    def _eval_for_json(self, wl_code: str):
        """Run WL code via SafeEval and return the native Python value."""
        env = self.manager.evaluate_native(wl_code, store_output=False)
        if env.get("status") != "ok":
            raise SoloBackendError(
                f"Solo backend WL call failed: status={env.get('status')!r}; "
                f"messages={env.get('messages', [])}"
            )
        return env.get("value")

    def read(self, path, *, include_content=True, preview_chars=80):
        include = "True" if include_content else "False"
        wl = (
            f"SharedKernelMCP`SoloReadNotebook[{_wl_string(path)}, "
            f"{include}, {int(preview_chars)}]"
        )
        return self._eval_for_json(wl)

    def run_cell(self, path, cell_id, eval_timeout: float | None = None):
        # Step 1: pull the cell's content out of the headless notebook (WL).
        # Step 2: evaluate it through the standard SessionManager path so
        # out_count / Out[N] history is tracked the same way as for any
        # other eval in this kernel — agents that follow up with
        # `notebook_get_output(path, out_number)` need that history.
        cid = _integer_cell_id(cell_id)
        info = self._eval_for_json(
            f"SharedKernelMCP`SoloGetCellContent[{_wl_string(path)}, {cid}]"
        )
        if not isinstance(info, dict) or info.get("status") != "ok":
            return info
        style = info.get("style")
        if style not in _EXECUTABLE_CELL_TYPES:
            return {
                "status": "skipped",
                "cellID": cid,
                "reason": "not_executable",
                "cellType": style,
            }
        kwargs = {"timeout": int(eval_timeout)} if eval_timeout else {}
        result = self.manager.evaluate(info.get("content", ""), **kwargs)
        return {
            "status": result.status,
            "cellID": cid,
            "style": style,
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
        wl = (
            f"SharedKernelMCP`SoloUpdateCell[{_wl_string(path)}, "
            f"{_integer_cell_id(cell_id)}, {_wl_string(cell_type)}, "
            f"{_wl_string(content)}]"
        )
        return self._eval_for_json(wl)

    def insert_cell_after(self, path, anchor_cell_id, cell_type, content):
        wl = (
            f"SharedKernelMCP`SoloInsertCellAfter[{_wl_string(path)}, "
            f"{_integer_cell_id(anchor_cell_id, label='anchor_cell_id')}, "
            f"{_wl_string(cell_type)}, {_wl_string(content)}]"
        )
        return self._eval_for_json(wl)

    def insert_cell_before(self, path, anchor_cell_id, cell_type, content):
        wl = (
            f"SharedKernelMCP`SoloInsertCellBefore[{_wl_string(path)}, "
            f"{_integer_cell_id(anchor_cell_id, label='anchor_cell_id')}, "
            f"{_wl_string(cell_type)}, {_wl_string(content)}]"
        )
        return self._eval_for_json(wl)

    def delete_cell(self, path, cell_id):
        wl = (
            f"SharedKernelMCP`SoloDeleteCell[{_wl_string(path)}, "
            f"{_integer_cell_id(cell_id)}]"
        )
        return self._eval_for_json(wl)

    def evaluate(self, _path, code, eval_timeout: float | None = None):
        # Free-form eval: no notebook involvement, just run code in the kernel.
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
        # Two-step: insert the cell via WL (so the file gets it), then
        # evaluate the code through the standard manager path so out_count
        # advances. Mirrors run_cell — Python owns evaluation, WL owns
        # notebook structure.
        ins = self.insert_cell_after(path, anchor_cell_id, cell_type, code)
        if not isinstance(ins, dict) or ins.get("status") != "ok":
            return {"error": "insert_failed", "insert_result": ins}
        new_id = ins.get("newCellID")
        kwargs = {"timeout": int(eval_timeout)} if eval_timeout else {}
        result = self.manager.evaluate(code, **kwargs)
        return {
            "status": result.status,
            "newCellID": new_id,
            "anchorCellID": _integer_cell_id(anchor_cell_id, label="anchor_cell_id"),
            "resultInputForm": result.output_summary,
            "messages": result.messages,
            "in_number": result.in_number,
            "out_number": result.out_number,
            "head": result.head,
            "is_truncated": result.is_truncated,
        }

    def sweep_outputs(self, _path):
        # Solo run_cell evaluates through the kernel without writing Output
        # cells back to the headless notebook, so there are no
        # bridge-tagged outputs to sweep. No-op.
        return {"status": "ok", "swept_count": 0, "note": "solo mode no-op"}

    def evaluate_for_json(self, code):
        # Generic kernel-side JSON-returning evaluation. Goes through SafeEval
        # for timeout/error safety.
        return self._eval_for_json(code)

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

    Solo mode now handles `.nb`, `.m`, and `.wl` uniformly via a headless
    front-end (UsingFrontEnd + NotebookOpen[..., Visible -> False]). It needs
    a Mathematica installation with a usable front-end binary; otherwise the
    underlying WL call returns status='front_end_unavailable' with guidance.
    """
    if has_bridge_for(path):
        return BridgeBackend(path, timeout=timeout)
    return SoloBackend(manager_factory())
