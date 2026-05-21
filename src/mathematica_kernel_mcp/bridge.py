"""Socket client and discovery helpers for the shared kernel bridge."""

from contextlib import suppress
from dataclasses import dataclass
from ipaddress import ip_address
import json
import os
import signal as _signal
import socket
import uuid
from pathlib import Path
from typing import Any


class BridgeError(RuntimeError):
    """Bridge call failed — kernel returned a non-ok status or unexpected payload."""


class BridgeTimeout(BridgeError):
    """Bridge call timed out."""


@dataclass(frozen=True)
class SocketConnection:
    """Connection details written by the Wolfram-side socket bridge."""

    host: str
    port: int
    token: str
    protocol: str = "jsonl-content-length-v1"
    kernel_pid: int | None = None


_ABORT_SIGNALS = {
    "SIGINT": _signal.SIGINT,
    "SIGTERM": _signal.SIGTERM,
}
REGISTRY_ENV_VAR = "MATHEMATICA_KERNEL_MCP_REGISTRY_DIR"


def bridge_registry_directories() -> list[Path]:
    """Return candidate global bridge registry directories."""
    candidates: list[Path] = []
    env_dir = os.environ.get(REGISTRY_ENV_VAR)
    if env_dir:
        candidates.append(Path(env_dir).expanduser())

    home = Path.home()
    candidates.extend(
        [
            home / ".Wolfram" / "ApplicationData" / "SharedKernelMCP" / "bridges",
            home / ".Mathematica" / "ApplicationData" / "SharedKernelMCP" / "bridges",
            home
            / "Library"
            / "Mathematica"
            / "ApplicationData"
            / "SharedKernelMCP"
            / "bridges",
        ]
    )
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(
            Path(appdata)
            / "Mathematica"
            / "ApplicationData"
            / "SharedKernelMCP"
            / "bridges"
        )
        candidates.append(
            Path(appdata)
            / "Wolfram"
            / "ApplicationData"
            / "SharedKernelMCP"
            / "bridges"
        )

    seen: set[Path] = set()
    result: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            result.append(resolved)
    return result


def _registry_record_files() -> list[Path]:
    files: list[Path] = []
    for directory in bridge_registry_directories():
        if directory.is_dir():
            files.extend(sorted(directory.glob("*.json")))
    return files


def _load_registry_record(path: Path) -> dict[str, Any] | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None
    record["_registry_file"] = str(path)
    return record


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _pid_is_alive(pid: Any) -> bool | None:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_int <= 0:
        return None
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _socket_connection_from_mapping(payload: dict[str, Any]) -> SocketConnection:
    if str(payload.get("transport", "")).lower() != "socket":
        raise BridgeError(
            f"Bridge record has transport={payload.get('transport')!r}; expected 'Socket'."
        )

    host = str(payload.get("host", ""))
    if not _is_loopback_host(host):
        raise BridgeError(f"Refusing socket bridge host {host!r}; expected loopback.")

    try:
        port = int(payload["port"])
        token = str(payload["token"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BridgeError(f"Malformed socket bridge record: {exc}") from exc

    if not (0 < port < 65536) or not token:
        raise BridgeError("Malformed socket bridge record: invalid port or token.")

    pid = payload.get("kernelPID")
    try:
        kernel_pid = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        kernel_pid = None
    if kernel_pid is not None and kernel_pid <= 0:
        kernel_pid = None

    return SocketConnection(
        host=host,
        port=port,
        token=token,
        protocol=str(payload.get("protocol", "jsonl-content-length-v1")),
        kernel_pid=kernel_pid,
    )


def _record_socket_ok(record: dict[str, Any]) -> bool:
    try:
        _socket_connection_from_mapping(record)
    except BridgeError:
        return False
    return True


def _normal_path(value: str | Path) -> str:
    return str(Path(value).expanduser().resolve())


def _record_notebook_paths(record: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    notebook_path = record.get("notebookPath")
    if isinstance(notebook_path, str) and notebook_path:
        paths.append(notebook_path)
    notebooks = record.get("notebooks")
    if isinstance(notebooks, list):
        for notebook in notebooks:
            if isinstance(notebook, dict):
                path = notebook.get("path")
                if isinstance(path, str) and path:
                    paths.append(path)
    return paths


def _record_matches_path(record: dict[str, Any], file_path: str | Path) -> bool:
    target = _normal_path(file_path)
    for notebook_path in _record_notebook_paths(record):
        try:
            if _normal_path(notebook_path) == target:
                return True
        except OSError:
            continue
    return False


def discover_bridge_records(
    *,
    include_stale: bool = False,
    prune_stale: bool = False,
    include_tokens: bool = False,
) -> list[dict[str, Any]]:
    """Load global socket bridge registry records written by Mathematica kernels."""
    records: list[dict[str, Any]] = []
    for file in _registry_record_files():
        record = _load_registry_record(file)
        if record is None:
            continue
        alive = _pid_is_alive(record.get("kernelPID"))
        socket_ok = _record_socket_ok(record)
        stale = alive is False or not socket_ok
        if stale and prune_stale:
            with suppress(OSError):
                file.unlink()
        if stale and not include_stale:
            continue
        payload = dict(record)
        payload["is_alive"] = alive
        payload["socket_ok"] = socket_ok
        if not include_tokens:
            payload.pop("token", None)
        records.append(payload)
    return records


def bridge_record_for_file(
    file_path: str | Path,
    *,
    include_tokens: bool = True,
) -> dict[str, Any] | None:
    """Return the newest live registry record for a notebook path, if any."""
    p = Path(file_path).resolve()
    candidates = [
        record
        for record in discover_bridge_records(include_tokens=include_tokens)
        if _record_matches_path(record, p)
    ]
    candidates.sort(key=lambda r: str(r.get("lastSeen", "")), reverse=True)
    return candidates[0] if candidates else None


class SharedKernelBridge:
    """Client for the in-notebook shared kernel socket bridge."""

    DEFAULT_TIMEOUT = 30.0

    def __init__(
        self,
        socket_connection: SocketConnection,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.socket_connection = socket_connection
        self.timeout = timeout

    @classmethod
    def for_file(
        cls, file_path: str | Path, timeout: float = DEFAULT_TIMEOUT
    ) -> "SharedKernelBridge":
        """Locate the socket bridge for a file from the global registry."""
        record = bridge_record_for_file(file_path, include_tokens=True)
        if record is None:
            raise BridgeError(
                f"No live shared kernel socket bridge is registered for {file_path!s}. "
                "Start the bridge in the Mathematica notebook with "
                "`StartSharedKernelBridge[]`."
            )
        return cls(_socket_connection_from_mapping(record), timeout=timeout)

    def call(
        self,
        code: str,
        *,
        silent: bool = True,
        eval_timeout: float | None = None,
    ) -> dict:
        """Evaluate Wolfram Language code through the authenticated socket bridge.

        `eval_timeout` (seconds) is enforced kernel-side via `TimeConstrained`: if
        the evaluation runs longer, the bridge aborts it and returns status
        "timeout". This is distinct from `self.timeout`, which bounds the socket
        call itself.
        """
        cmd_id = f"mcp_{uuid.uuid4().hex[:12]}"

        markers = []
        if silent:
            markers.append("(*SILENT*)")
        if eval_timeout is not None and eval_timeout > 0:
            markers.append(f"(*TIMEOUT:{eval_timeout}*)")
        prefix = ("\n".join(markers) + "\n") if markers else ""
        command = prefix + code

        return self._call_socket(cmd_id, command)

    def _read_socket_response(self, sock: socket.socket, cmd_id: str) -> bytes:
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
        result = self.call(f"(*FULLJSON*)\n{code}", eval_timeout=eval_timeout)
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
        pid = self.socket_connection.kernel_pid
        if pid is None:
            raise BridgeError(
                "The bridge registry record does not include a kernel PID. "
                "Re-run StartSharedKernelBridge[] in the notebook."
            )
        try:
            os.kill(pid, 0)  # liveness probe, no signal sent
        except ProcessLookupError as exc:
            raise BridgeError(
                f"PID {pid} from the bridge registry is not running; "
                "kernel may have exited."
            ) from exc
        except PermissionError as exc:
            raise BridgeError(
                f"Cannot signal PID {pid}: permission denied."
            ) from exc
        return pid

    def abort_evaluation(self, signal: str = "SIGINT") -> dict:
        """Send a POSIX signal to the kernel PID to abort the in-flight evaluation.

        SIGINT is what the Mathematica front-end uses for its Abort button: the
        kernel aborts when it reaches an interruptible point. SIGTERM is more
        forceful and may cause the kernel to exit.

        Use this only when an evaluation has hung the kernel so badly that
        TimeConstrained can't reach it.
        """
        sig_num = _ABORT_SIGNALS.get(signal.upper())
        if sig_num is None:
            raise BridgeError(
                f"Unsupported abort signal {signal!r}; use one of "
                f"{sorted(_ABORT_SIGNALS)}."
            )
        pid = self._read_kernel_pid()
        os.kill(pid, sig_num)
        return {
            "status": "ok",
            "pid": pid,
            "signal": signal.upper(),
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
        if include_content:
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
