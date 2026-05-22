"""Session manager for persistent Wolfram Language kernel sessions."""

import json
import logging
import os
import signal as _signal
import shutil
import threading
from contextlib import suppress
from dataclasses import dataclass
from textwrap import dedent

from wolframclient.evaluation import WolframLanguageSession
from wolframclient.language import wlexpr

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
        """Create a new WolframLanguageSession."""
        if self._kernel_path:
            session = WolframLanguageSession(self._kernel_path)
        else:
            session = WolframLanguageSession()
        session.set_parameter("STARTUP_TIMEOUT", self._startup_timeout)
        return session

    def start(self) -> None:
        """Start the session manager and create the main session."""
        self.create_session("main")

    def stop(self) -> None:
        """Stop all sessions."""
        with self._lock:
            for managed in self._sessions.values():
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
        """Evaluate code in a named session, return a summarized result.

        Distinguishes parse_error, timeout, and ok cases:
        - Pre-parses with `ToExpression[..., HoldComplete]`; `$Failed` =>
          status="parse_error" (matches the bridge path's semantics).
        - Runs the parsed expression under `TimeConstrained[..., timeout, tag]`
          with a unique sentinel tag; matching the tag => status="timeout".
          (Avoids the old `str(result) == "$Aborted"` heuristic that would
          mislabel user code legitimately returning `$Aborted`.)
        """
        managed = self.get_session(session_name)
        managed.is_busy = True
        try:
            managed.out_count += 1
            in_number = managed.out_count
            out_number = managed.out_count

            # Evaluate in a single Module so parse / timeout / ok are
            # distinguished by a sentinel returned alongside the value.
            # `wolfram$mcp$out[n]` is set so kernel_get_output can retrieve it.
            code_literal = json.dumps(code)
            store_code = dedent(
                f"""
                Module[{{wolfram$mcp$held, wolfram$mcp$timeoutTag, wolfram$mcp$eval}},
                    wolfram$mcp$held = Check[
                        ToExpression[{code_literal}, InputForm, HoldComplete],
                        $Failed
                    ];
                    If[wolfram$mcp$held === $Failed,
                        wolfram$mcp$out[{out_number}] = $Failed;
                        "parse_error",
                        wolfram$mcp$eval = TimeConstrained[
                            ReleaseHold[wolfram$mcp$held],
                            {timeout},
                            wolfram$mcp$timeoutTag
                        ];
                        If[wolfram$mcp$eval === wolfram$mcp$timeoutTag,
                            wolfram$mcp$out[{out_number}] = $Aborted;
                            "timeout",
                            wolfram$mcp$out[{out_number}] = wolfram$mcp$eval;
                            "ok"
                        ]
                    ]
                ]
                """
            )

            raw = managed.session.evaluate_wrap(wlexpr(store_code))

            messages: list[str] = []
            if hasattr(raw, "messages") and raw.messages:
                for msg in raw.messages:
                    if isinstance(msg, tuple) and len(msg) >= 2:
                        messages.append(str(msg[1]))
                    else:
                        messages.append(str(msg))

            status_value = raw.result if hasattr(raw, "result") else raw
            status = str(status_value)
            if status not in {"ok", "parse_error", "timeout"}:
                # Unexpected — surface as parse_error with the raw status so
                # callers can see what happened.
                messages.insert(0, f"Unexpected eval status: {status!r}")
                status = "parse_error"

            if status == "parse_error":
                return EvalResult(
                    output_summary="$Failed (parse error)",
                    head="$Failed",
                    byte_size=0,
                    leaf_count=0,
                    messages=messages,
                    is_truncated=False,
                    in_number=in_number,
                    out_number=out_number,
                    status="parse_error",
                )
            if status == "timeout":
                return EvalResult(
                    output_summary="$Aborted (timed out)",
                    head="$Aborted",
                    byte_size=0,
                    leaf_count=0,
                    messages=messages,
                    is_truncated=False,
                    in_number=in_number,
                    out_number=out_number,
                    status="timeout",
                )

            meta_code = dedent(
                f"""
                With[{{res = wolfram$mcp$out[{out_number}]}},
                    {{
                        If[
                            AtomQ[res],
                            ToString[res, InputForm],
                            ToString[Shallow[res, {{5, 3}}], OutputForm]
                        ],
                        ToString[Head[res]],
                        ByteCount[res],
                        LeafCount[res]
                    }}
                ]
                """
            )
            meta = managed.session.evaluate(wlexpr(meta_code))

            summary = str(meta[0]) if meta else ""
            head = str(meta[1]) if meta else "Unknown"
            byte_size = int(meta[2]) if meta else 0
            leaf_count = int(meta[3]) if meta else 0

            is_truncated = len(summary) > summary_max
            if is_truncated:
                summary = summary[:summary_max] + "..."

            return EvalResult(
                output_summary=summary,
                head=head,
                byte_size=byte_size,
                leaf_count=leaf_count,
                messages=messages,
                is_truncated=is_truncated,
                in_number=in_number,
                out_number=out_number,
                status="ok",
            )
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
        managed.session.stop()
        managed.session.start()
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
