"""Client for the file-based shared kernel bridge.

The MCP drops a `.wl` file into `<bridge_root>/queue/`, the bridge running in the
user's Mathematica kernel polls and evaluates it, and writes the result as JSON
to `<bridge_root>/results/<id>.json`. This client wraps that protocol.
"""

import json
import time
import uuid
from pathlib import Path


class BridgeError(RuntimeError):
    """Bridge call failed — kernel returned a non-ok status or unexpected payload."""


class BridgeTimeout(BridgeError):
    """Bridge call did not produce a result file within the allotted time."""


class SharedKernelBridge:
    """File-protocol client for the in-notebook shared kernel bridge."""

    DEFAULT_DIR = ".shared_kernel_bridge"
    POLL_INTERVAL = 0.2
    DEFAULT_TIMEOUT = 30.0

    def __init__(self, root_dir: str | Path, timeout: float = DEFAULT_TIMEOUT):
        self.root = Path(root_dir).resolve()
        self.queue_dir = self.root / "queue"
        self.results_dir = self.root / "results"
        self.timeout = timeout
        if not self.queue_dir.exists() or not self.results_dir.exists():
            raise BridgeError(
                f"No shared kernel bridge at {self.root}; expected `queue/` and `results/` "
                f"subdirectories. Start the bridge in a Mathematica notebook with "
                f"`StartSharedKernelBridge[\"RootDirectory\" -> \"{self.root}\"]`."
            )

    @classmethod
    def for_file(cls, file_path: str | Path, timeout: float = DEFAULT_TIMEOUT) -> "SharedKernelBridge":
        """Locate the bridge by looking next to the given file.

        Each notebook gets its own bridge subtree at
        ``<file_dir>/.shared_kernel_bridge/<filename>/`` so multiple files in
        one directory can run independent bridges without sharing a queue.
        """
        p = Path(file_path).resolve()
        bridge_root = p.parent / cls.DEFAULT_DIR / p.name
        return cls(bridge_root, timeout=timeout)

    def call(self, code: str, *, silent: bool = True) -> dict:
        """Drop a queue file with the given WL code and wait for the JSON result."""
        cmd_id = f"mcp_{uuid.uuid4().hex[:12]}"
        queue_file = self.queue_dir / f"{cmd_id}.wl"
        results_file = self.results_dir / f"{cmd_id}.json"

        prefix = "(*SILENT*)\n" if silent else ""
        queue_file.write_text(prefix + code, encoding="utf-8")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if results_file.exists():
                with results_file.open() as f:
                    return json.load(f)
            time.sleep(self.POLL_INTERVAL)

        raise BridgeTimeout(f"Bridge call {cmd_id} timed out after {self.timeout}s")

    def call_for_json(self, code: str) -> dict | list:
        """Run a primitive that returns a JSON-serializable association/list."""
        result = self.call(code)
        if result.get("status") != "ok":
            raise BridgeError(
                f"Bridge call returned status={result.get('status')}: "
                f"{result.get('resultInputForm', '')}"
            )
        payload = result.get("resultJSON")
        if payload is None:
            raise BridgeError(
                f"Bridge call did not return resultJSON. resultInputForm: "
                f"{result.get('resultInputForm', '')}"
            )
        return payload

    @staticmethod
    def _wl_string(value: str) -> str:
        return json.dumps(value)

    def read_notebook(self, path: str) -> dict:
        return self.call_for_json(f"BridgeReadNotebook[{self._wl_string(path)}]")

    def run_cell(self, path: str, cell_id: int) -> dict:
        return self.call_for_json(
            f"BridgeRunCell[{self._wl_string(path)}, {cell_id}]"
        )

    def update_cell(self, path: str, cell_id: int, cell_type: str, content: str) -> dict:
        return self.call_for_json(
            f"BridgeUpdateCell[{self._wl_string(path)}, {cell_id}, "
            f"{self._wl_string(cell_type)}, {self._wl_string(content)}]"
        )

    def insert_cell_after(
        self, path: str, anchor_cell_id: int, cell_type: str, content: str
    ) -> dict:
        return self.call_for_json(
            f"BridgeInsertCellAfter[{self._wl_string(path)}, {anchor_cell_id}, "
            f"{self._wl_string(cell_type)}, {self._wl_string(content)}]"
        )

    def insert_cell_before(
        self, path: str, anchor_cell_id: int, cell_type: str, content: str
    ) -> dict:
        return self.call_for_json(
            f"BridgeInsertCellBefore[{self._wl_string(path)}, {anchor_cell_id}, "
            f"{self._wl_string(cell_type)}, {self._wl_string(content)}]"
        )

    def delete_cell(self, path: str, cell_id: int) -> dict:
        return self.call_for_json(
            f"BridgeDeleteCell[{self._wl_string(path)}, {cell_id}]"
        )

    def sweep_stale_outputs(self, path: str) -> dict:
        return self.call_for_json(
            f"BridgeSweepStaleOutputs[{self._wl_string(path)}]"
        )

    def evaluate(self, code: str) -> dict:
        """Evaluate arbitrary WL code in the shared kernel.

        Returns the raw bridge result envelope (status, resultInputForm, messages, …)
        so callers can decide how to interpret non-JSON-able results.
        """
        return self.call(code)
