"""Client for the shared kernel bridge.

Newer bridge instances expose an authenticated localhost JSON-lines socket.
Older instances use the file queue protocol: the MCP drops a `.wl` file into
`<bridge_root>/queue/`, the bridge running in the user's Mathematica kernel
polls and evaluates it, and writes the result as JSON to
`<bridge_root>/results/<id>.json`. This client supports both protocols.
"""

from dataclasses import dataclass
from ipaddress import ip_address
import json
import os
import signal as _signal
import socket
import time
import uuid
from pathlib import Path


class BridgeError(RuntimeError):
    """Bridge call failed — kernel returned a non-ok status or unexpected payload."""


class BridgeTimeout(BridgeError):
    """Bridge call did not produce a result file within the allotted time."""


@dataclass(frozen=True)
class SocketConnection:
    """Connection details written by the Wolfram-side socket bridge."""

    host: str
    port: int
    token: str
    protocol: str = "json-lines"


_ABORT_SIGNALS = {
    "SIGINT": _signal.SIGINT,
    "SIGTERM": _signal.SIGTERM,
}


class SharedKernelBridge:
    """Client for the in-notebook shared kernel bridge."""

    DEFAULT_DIR = ".shared_kernel_bridge"
    POLL_INTERVAL = 0.2
    DEFAULT_TIMEOUT = 30.0

    def __init__(self, root_dir: str | Path, timeout: float = DEFAULT_TIMEOUT):
        self.root = Path(root_dir).resolve()
        self.queue_dir = self.root / "queue"
        self.results_dir = self.root / "results"
        self.connection_file = self.root / "connection.json"
        self.timeout = timeout
        if not self.queue_dir.exists() or not self.results_dir.exists():
            raise BridgeError(
                f"No shared kernel bridge at {self.root}; expected `queue/` and `results/` "
                f"subdirectories. Start the bridge in a Mathematica notebook with "
                f"`StartSharedKernelBridge[\"RootDirectory\" -> \"{self.root}\"]`."
            )
        self.socket_connection = self._load_socket_connection()

    @classmethod
    def for_file(
        cls, file_path: str | Path, timeout: float = DEFAULT_TIMEOUT
    ) -> "SharedKernelBridge":
        """Locate the bridge by looking next to the given file.

        Each notebook gets its own bridge subtree at
        ``<file_dir>/.shared_kernel_bridge/<filename>/`` so multiple files in
        one directory can run independent bridges without sharing a queue.
        """
        p = Path(file_path).resolve()
        bridge_root = p.parent / cls.DEFAULT_DIR / p.name
        return cls(bridge_root, timeout=timeout)

    def call(
        self,
        code: str,
        *,
        silent: bool = True,
        eval_timeout: float | None = None,
    ) -> dict:
        """Drop a queue file with the given WL code and wait for the JSON result.

        `eval_timeout` (seconds) is enforced kernel-side via `TimeConstrained`: if
        the evaluation runs longer, the bridge aborts it and returns status
        "timeout". This is distinct from `self.timeout`, which only bounds how
        long the Python side waits for the result file.
        """
        cmd_id = f"mcp_{uuid.uuid4().hex[:12]}"
        queue_file = self.queue_dir / f"{cmd_id}.wl"
        results_file = self.results_dir / f"{cmd_id}.json"

        markers = []
        if silent:
            markers.append("(*SILENT*)")
        if eval_timeout is not None and eval_timeout > 0:
            markers.append(f"(*TIMEOUT:{eval_timeout}*)")
        prefix = ("\n".join(markers) + "\n") if markers else ""
        command = prefix + code

        if self.socket_connection is not None:
            return self._call_socket(cmd_id, command)

        queue_file.write_text(command, encoding="utf-8")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            if results_file.exists():
                with results_file.open() as f:
                    return json.load(f)
            time.sleep(self.POLL_INTERVAL)

        raise BridgeTimeout(f"Bridge call {cmd_id} timed out after {self.timeout}s")

    @staticmethod
    def _is_loopback_host(host: str) -> bool:
        if host == "localhost":
            return True
        try:
            return ip_address(host).is_loopback
        except ValueError:
            return False

    def _load_socket_connection(self) -> SocketConnection | None:
        if not self.connection_file.exists():
            return None
        try:
            payload = json.loads(self.connection_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError(
                f"Malformed socket bridge metadata at {self.connection_file}: {exc}"
            ) from exc

        if payload.get("transport") != "socket":
            return None

        host = str(payload.get("host", ""))
        if not self._is_loopback_host(host):
            raise BridgeError(
                f"Refusing socket bridge host {host!r}; expected a loopback host."
            )

        try:
            port = int(payload["port"])
            token = str(payload["token"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BridgeError(
                f"Malformed socket bridge metadata at {self.connection_file}: {exc}"
            ) from exc

        if not (0 < port < 65536) or not token:
            raise BridgeError(
                f"Malformed socket bridge metadata at {self.connection_file}: "
                "invalid port or token."
            )

        protocol = str(payload.get("protocol", "json-lines"))
        return SocketConnection(
            host=host,
            port=port,
            token=token,
            protocol=protocol,
        )

    def _read_socket_response(self, sock: socket.socket, cmd_id: str) -> bytes:
        assert self.socket_connection is not None
        if self.socket_connection.protocol != "jsonl-content-length-v1":
            with sock.makefile("rb") as response:
                return response.read()

        with sock.makefile("rb") as response:
            headers: dict[str, str] = {}
            while True:
                line = response.readline()
                if line == b"":
                    raise BridgeError(
                        f"Socket bridge call {cmd_id} closed before response headers"
                    )
                if line in (b"\r\n", b"\n"):
                    break
                try:
                    header_line = line.decode("ascii").strip()
                except UnicodeDecodeError as exc:
                    raise BridgeError(
                        f"Socket bridge call {cmd_id} returned non-ASCII header"
                    ) from exc
                if ":" not in header_line:
                    raise BridgeError(
                        f"Socket bridge call {cmd_id} returned malformed header "
                        f"{header_line!r}"
                    )
                name, value = header_line.split(":", 1)
                headers[name.lower()] = value.strip()

            try:
                content_length = int(headers["content-length"])
            except (KeyError, ValueError) as exc:
                raise BridgeError(
                    f"Socket bridge call {cmd_id} did not include a valid "
                    "Content-Length header"
                ) from exc
            if content_length < 0:
                raise BridgeError(
                    f"Socket bridge call {cmd_id} returned negative Content-Length"
                )

            raw_response = response.read(content_length)
            if len(raw_response) != content_length:
                raise BridgeError(
                    f"Socket bridge call {cmd_id} returned {len(raw_response)} "
                    f"bytes, expected {content_length}"
                )
            return raw_response

    def _call_socket(self, cmd_id: str, command: str) -> dict:
        assert self.socket_connection is not None
        conn = self.socket_connection
        request = {
            "id": cmd_id,
            "token": conn.token,
            "code": command,
        }
        data = (json.dumps(request, separators=(",", ":")) + "\n").encode("utf-8")

        try:
            with socket.create_connection(
                (conn.host, conn.port), timeout=self.timeout
            ) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(data)
                raw_response = self._read_socket_response(sock, cmd_id)
        except socket.timeout as exc:
            raise BridgeTimeout(
                f"Socket bridge call {cmd_id} timed out after {self.timeout}s"
            ) from exc
        except OSError as exc:
            raise BridgeError(f"Socket bridge call {cmd_id} failed: {exc}") from exc

        if not raw_response:
            raise BridgeError(f"Socket bridge call {cmd_id} returned no response")

        try:
            result = json.loads(raw_response.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BridgeError(
                f"Socket bridge call {cmd_id} returned malformed JSON: {exc}"
            ) from exc

        if not isinstance(result, dict):
            raise BridgeError(
                f"Socket bridge call {cmd_id} returned "
                f"{type(result).__name__}, expected object"
            )
        return result

    def call_for_json(
        self, code: str, *, eval_timeout: float | None = None
    ) -> dict | list:
        """Run a primitive that returns a JSON-serializable association/list."""
        result = self.call(code, eval_timeout=eval_timeout)
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

    def _read_kernel_pid(self) -> int:
        pid_file = self.root / "kernel.pid"
        if not pid_file.exists():
            raise BridgeError(
                f"No kernel.pid at {pid_file}. The bridge running in your kernel "
                f"may pre-date the abort feature. Re-run StartSharedKernelBridge[] "
                f"in your notebook to write it."
            )
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError as exc:
            raise BridgeError(f"Malformed PID in {pid_file}: {exc}") from exc
        try:
            os.kill(pid, 0)  # liveness probe, no signal sent
        except ProcessLookupError as exc:
            raise BridgeError(
                f"PID {pid} from {pid_file} is not running; kernel may have exited."
            ) from exc
        except PermissionError as exc:
            raise BridgeError(
                f"Cannot signal PID {pid}: permission denied."
            ) from exc
        return pid

    def abort_evaluation(
        self, signal: str = "SIGINT", clear_queue: bool = False
    ) -> dict:
        """Send a POSIX signal to the kernel PID to abort the in-flight evaluation.

        SIGINT is what the Mathematica front-end uses for its Abort button: the
        kernel calls Abort[] at its next polling point. SIGTERM is more forceful
        and may cause the kernel to exit.

        Use this only when an evaluation has hung the kernel so badly that
        TimeConstrained / queued aborts can't reach it — the bridge poller is
        blocked behind the in-flight eval, so any in-band approach is dead.

        `clear_queue=True` deletes pending `.wl` files in the queue dir so the
        scheduled task doesn't immediately pick up the next runaway command.
        """
        sig_num = _ABORT_SIGNALS.get(signal.upper())
        if sig_num is None:
            raise BridgeError(
                f"Unsupported abort signal {signal!r}; use one of "
                f"{sorted(_ABORT_SIGNALS)}."
            )
        pid = self._read_kernel_pid()
        cleared: list[str] = []
        if clear_queue:
            for queued in self.queue_dir.glob("*.wl"):
                try:
                    queued.unlink()
                    cleared.append(queued.name)
                except OSError:
                    pass
        os.kill(pid, sig_num)
        return {
            "status": "ok",
            "pid": pid,
            "signal": signal.upper(),
            "cleared_queue_files": cleared,
        }

    @staticmethod
    def _wl_string(value: str) -> str:
        return json.dumps(value)

    def read_notebook(
        self,
        path: str,
        *,
        include_content: bool = True,
        preview_chars: int = 80,
    ) -> dict:
        supports_compact_read = (
            self.socket_connection is not None
            and self.socket_connection.protocol == "jsonl-content-length-v1"
        )
        if include_content or not supports_compact_read:
            return self.call_for_json(f"BridgeReadNotebook[{self._wl_string(path)}]")
        include_arg = "True" if include_content else "False"
        return self.call_for_json(
            f"BridgeReadNotebook[{self._wl_string(path)}, "
            f"{include_arg}, {int(preview_chars)}]"
        )

    def run_cell(
        self, path: str, cell_id: int, eval_timeout: float | None = None
    ) -> dict:
        if eval_timeout is not None and eval_timeout > 0:
            tail = f", {eval_timeout}"
        else:
            tail = ""
        return self.call_for_json(
            f"BridgeRunCell[{self._wl_string(path)}, {cell_id}{tail}]"
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

    def evaluate(self, code: str, eval_timeout: float | None = None) -> dict:
        """Evaluate arbitrary WL code in the shared kernel.

        Returns the raw bridge result envelope (status, resultInputForm, messages, …)
        so callers can decide how to interpret non-JSON-able results.
        """
        return self.call(code, eval_timeout=eval_timeout)
