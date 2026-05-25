"""Session manager for persistent Wolfram Language kernel sessions.

Evaluation goes through the SharedKernelMCP`SafeEval paclet helper — the
same WL function the socket bridge uses. wolframclient delivers SafeEval's
Association as a Python mapping, so values come back natively (Unicode strings,
arbitrary-precision ints, lists/dicts) without any JSON intermediary.
"""

import logging
import os
import signal as _signal
import shutil
import threading
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from wolframclient.evaluation import WolframLanguageSession
from wolframclient.language import wl, wlexpr

try:
    from wolframclient.utils.packedarray import PackedArray
except ImportError:  # pragma: no cover - depends on wolframclient version
    PackedArray = None

from mathematica_kernel_mcp.models import EvalResult, SessionInfo

logger = logging.getLogger(__name__)

# Default max characters for output summary
DEFAULT_SUMMARY_MAX = 500
# Default timeout for evaluations (seconds)
DEFAULT_TIMEOUT = 30
# Kernel startup can be slow, especially on first launch
KERNEL_STARTUP_TIMEOUT = 60


def _default_kernel_path() -> str | None:
    """Return an explicit kernel path when one is available from env/PATH."""
    env_path = os.environ.get("WOLFRAM_KERNEL_PATH")
    if env_path:
        return env_path
    for executable in ("WolframKernel", "MathKernel"):
        found = shutil.which(executable)
        if found:
            return found
    return None


def _paclet_directory() -> str | None:
    """Return the directory containing the SharedKernelMCP paclet, if found.

    The paclet ships alongside the Python package at <repo>/wolfram/. For
    editable installs (`pip install -e .`) we can locate it relative to this
    module. If the paclet is installed system-wide (PacletInstall), Needs[]
    will find it without PacletDirectoryLoad.
    """
    candidate = Path(__file__).resolve().parents[2] / "wolfram"
    if (candidate / "SharedKernelMCP" / "Kernel" / "init.m").is_file():
        return str(candidate)
    return None


def _to_python(value):
    """Recursively normalize wolframclient quirks into ordinary Python types.

    - WL Lists arrive as tuples or PackedArray; we want lists.
    - WL Associations arrive as `immutabledict`; we want plain dicts.
    - WL Reals arrive as `Decimal`. Machine-precision values that survive a
      float64 roundtrip are downcast to `float` (JSON-friendly); arbitrary-
      precision values stay as `Decimal` so `json.dumps` fails and the
      caller falls back to `inputForm` instead of silently dropping ~38
      digits on `N[Pi, 50]`-style results.
    """
    if isinstance(value, Decimal):
        try:
            as_float = float(value)
        except (OverflowError, ValueError):
            return value
        if Decimal(str(as_float)) == value:
            return as_float
        return value
    if PackedArray is not None and isinstance(value, PackedArray):
        return _to_python(value.tolist())
    if isinstance(value, Mapping):
        return {k: _to_python(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_python(v) for v in value]
    return value


def _normalize_env(env: Mapping, out_number: int | None) -> dict:
    """Normalize SafeEval's wolframclient-delivered dict for downstream use."""
    out = {k: _to_python(v) for k, v in env.items()}
    if out_number is None:
        out.pop("outNumber", None)
    else:
        out.setdefault("outNumber", out_number)
    return out


@dataclass
class ManagedSession:
    """A kernel session with metadata."""

    name: str
    session: WolframLanguageSession
    is_busy: bool = False
    out_count: int = 0
    pid: int | None = None


_ABORT_SIGNALS = {
    "SIGINT": _signal.SIGINT,
    "SIGTERM": _signal.SIGTERM,
}


class SessionManager:
    """Manages named Wolfram Language kernel sessions."""

    def __init__(self, kernel_path: str | None = None, startup_timeout: int = KERNEL_STARTUP_TIMEOUT):
        """Initialize the session manager.

        Args:
            kernel_path: Path to WolframKernel binary. If None, wolframclient
                        will attempt to find it automatically.
            startup_timeout: Seconds to wait for kernel startup.
        """
        self._kernel_path = kernel_path or _default_kernel_path()
        self._startup_timeout = startup_timeout
        self._sessions: dict[str, ManagedSession] = {}
        self._lock = threading.Lock()

    def _create_kernel_session(self) -> WolframLanguageSession:
        """Create a new WolframLanguageSession with SharedKernelMCP loaded."""
        if self._kernel_path:
            session = WolframLanguageSession(self._kernel_path)
        else:
            session = WolframLanguageSession()
        session.set_parameter("STARTUP_TIMEOUT", self._startup_timeout)
        return session

    def _load_paclet(self, session: WolframLanguageSession) -> None:
        """Make SharedKernelMCP`SafeEval available in this kernel.

        Tries the dev-checkout location first; falls back to whatever the
        user installed system-wide. If neither works, Needs[] returns
        $Failed and subsequent evaluate() calls will surface a kernel_error
        with a clear message.
        """
        paclet_dir = _paclet_directory()
        if paclet_dir is not None:
            session.evaluate(wl.PacletDirectoryLoad(paclet_dir))
        result = session.evaluate(wl.Needs("SharedKernelMCP`"))
        if str(result) == "$Failed":
            logger.warning(
                "Needs[\"SharedKernelMCP`\"] failed in scratch kernel. "
                "Install the paclet with PacletInstall, or point "
                "PacletDirectoryLoad at the paclet root."
            )

    def start(self) -> None:
        """Start the session manager and create the main session."""
        self.create_session("main")

    def stop(self) -> None:
        """Stop all sessions."""
        with self._lock:
            for managed in self._sessions.values():
                # Best-effort cleanup of any cached solo-mode headless
                # notebooks so the embedded front-end doesn't outlive the
                # kernel (would otherwise leave orphaned FE processes).
                with suppress(Exception):
                    managed.session.evaluate(
                        wl.SharedKernelMCP.SoloCloseAllNotebooks()
                    )
                try:
                    managed.session.stop()
                except Exception:
                    logger.warning("Failed to stop session %s", managed.name)
            self._sessions.clear()

    def _create_session_locked(self, name: str) -> str:
        """Create a new named kernel session. Caller must hold `_lock`."""
        if name in self._sessions:
            raise ValueError(f"Session '{name}' already exists")
        session = self._create_kernel_session()
        session.start()
        self._load_paclet(session)
        pid: int | None
        try:
            pid = int(session.evaluate(wlexpr("$ProcessID")))
        except Exception:
            pid = None
            logger.warning("Could not capture $ProcessID for session %s", name)
        self._sessions[name] = ManagedSession(name=name, session=session, pid=pid)
        logger.info("Created session '%s' (pid=%s)", name, pid)
        return name

    def create_session(self, name: str) -> str:
        """Create a new named kernel session.

        Returns the session name.
        """
        with self._lock:
            return self._create_session_locked(name)

    def close_session(self, name: str) -> None:
        """Close and remove a named session."""
        if name == "main":
            raise ValueError("Cannot close the main session. Use kernel_restart instead.")
        with self._lock:
            managed = self._sessions.pop(name, None)
            if managed is None:
                raise ValueError(f"Session '{name}' does not exist")
            with suppress(Exception):
                managed.session.evaluate(wl.SharedKernelMCP.SoloCloseAllNotebooks())
            managed.session.stop()
            logger.info("Closed session '%s'", name)

    def get_session(self, name: str = "main") -> ManagedSession:
        """Get a managed session by name."""
        with self._lock:
            managed = self._sessions.get(name)
            if managed is None and name == "main":
                self._create_session_locked(name)
                managed = self._sessions[name]
            if managed is None:
                raise ValueError(f"Session '{name}' does not exist")
            return managed

    def list_sessions(self) -> list[SessionInfo]:
        """List all sessions with their status."""
        with self._lock:
            result = []
            for managed in self._sessions.values():
                alive = False
                with suppress(Exception):
                    alive = managed.session.started
                result.append(
                    SessionInfo(
                        name=managed.name,
                        is_alive=alive,
                        is_busy=managed.is_busy,
                        out_count=managed.out_count,
                        pid=managed.pid,
                    )
                )
            return result

    def evaluate(
        self,
        code: str,
        session_name: str = "main",
        timeout: int = DEFAULT_TIMEOUT,
        summary_max: int = DEFAULT_SUMMARY_MAX,
    ) -> EvalResult:
        """Evaluate code via the SharedKernelMCP`SafeEval paclet helper.

        wolframclient delivers SafeEval's Association as a Python mapping so
        values (incl. Unicode strings, bignum ints) come back natively. No
        JSON intermediary, no second meta round-trip.
        """
        managed = self.get_session(session_name)
        managed.is_busy = True
        try:
            managed.out_count += 1
            in_number = managed.out_count
            out_number = managed.out_count

            env = managed.session.evaluate(
                wl.SharedKernelMCP.SafeEval(
                    code,
                    wl.Rule("EvalTimeout", timeout),
                    wl.Rule("StoreOutNumber", out_number),
                    wl.Rule("SummaryMax", summary_max),
                )
            )

            if not isinstance(env, Mapping):
                # Needs[] probably failed; SafeEval isn't defined. Surface the
                # raw return so the caller sees what happened.
                return EvalResult(
                    output_summary=str(env)[:summary_max],
                    head="$Failed",
                    byte_size=0,
                    leaf_count=0,
                    messages=[
                        "SharedKernelMCP`SafeEval is not defined in this kernel. "
                        "Install the paclet (PacletInstall) or point "
                        "PacletDirectoryLoad at the paclet root."
                    ],
                    is_truncated=False,
                    in_number=in_number,
                    out_number=out_number,
                    status="kernel_error",
                )

            messages = [str(m) for m in env.get("messages", ())]
            summary = str(env.get("summary", ""))
            is_truncated = summary.endswith(" chars]") and "... [truncated " in summary

            return EvalResult(
                output_summary=summary,
                head=str(env.get("head", "Unknown")),
                byte_size=int(env.get("byteCount", 0)),
                leaf_count=int(env.get("leafCount", 0)),
                messages=messages,
                is_truncated=is_truncated,
                in_number=in_number,
                out_number=out_number,
                status=str(env.get("status", "ok")),
            )
        finally:
            managed.is_busy = False

    def evaluate_native(self, code: str, session_name: str = "main",
                        timeout: int = DEFAULT_TIMEOUT,
                        store_output: bool = True) -> dict:
        """Evaluate code and return SafeEval's raw Association as a Python dict.

        Use this when the caller wants the native Python value (e.g. the
        `kernel_eval_json` tool needs the actual list/int/string/etc rather
        than the textual summary). wolframclient delivers WL Strings as
        Unicode `str`, WL Integers as arbitrary-precision `int`, WL Lists as
        Python `list`, WL Associations as Python mappings.
        """
        managed = self.get_session(session_name)
        managed.is_busy = True
        try:
            args = [code, wl.Rule("EvalTimeout", timeout)]
            if store_output:
                managed.out_count += 1
                out_number: int | None = managed.out_count
                args.append(wl.Rule("StoreOutNumber", out_number))
            else:
                out_number = None
            env = managed.session.evaluate(wl.SharedKernelMCP.SafeEval(*args))
            if not isinstance(env, Mapping):
                return {
                    "status": "kernel_error",
                    "messages": [
                        "SharedKernelMCP`SafeEval not available; "
                        f"raw return was {env!r}"
                    ],
                }
            # Normalize wolframclient containers into JSON-friendly Python values.
            return _normalize_env(env, out_number)
        finally:
            managed.is_busy = False

    def evaluate_raw(
        self,
        code: str,
        session_name: str = "main",
    ) -> str:
        """Evaluate code and return raw string result. For internal/inspection use."""
        managed = self.get_session(session_name)
        managed.is_busy = True
        try:
            result = managed.session.evaluate(wlexpr(code))
            return str(result)
        finally:
            managed.is_busy = False

    def restart_session(self, name: str = "main") -> None:
        """Restart a kernel session (fresh state)."""
        managed = self.get_session(name)
        with suppress(Exception):
            managed.session.evaluate(wl.SharedKernelMCP.SoloCloseAllNotebooks())
        managed.session.stop()
        managed.session.start()
        self._load_paclet(managed.session)
        managed.out_count = 0
        try:
            managed.pid = int(managed.session.evaluate(wlexpr("$ProcessID")))
        except Exception:
            managed.pid = None
        logger.info("Restarted session '%s' (pid=%s)", name, managed.pid)

    def abort_session(self, name: str = "main", signal: str = "SIGINT") -> int:
        """Signal the kernel process to abort its current evaluation.

        Returns the PID that was signaled.
        """
        sig_num = _ABORT_SIGNALS.get(signal.upper())
        if sig_num is None:
            raise ValueError(
                f"Unsupported abort signal {signal!r}; use one of {sorted(_ABORT_SIGNALS)}."
            )
        managed = self.get_session(name)
        if managed.pid is None:
            raise RuntimeError(
                f"Session '{name}' has no captured PID; cannot signal kernel."
            )
        os.kill(managed.pid, sig_num)
        return managed.pid
