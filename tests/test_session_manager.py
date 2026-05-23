from collections.abc import Mapping

import pytest

from mathematica_kernel_mcp.session import SessionManager
import mathematica_kernel_mcp.session as session_mod


class FakeImmutableMapping(Mapping):
    """Small stand-in for wolframclient immutable Association mapping."""

    def __init__(self, data):
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)


class FakePackedArray:
    """Small stand-in for wolframclient's PackedArray ndarray wrapper."""

    def __init__(self, values):
        self._values = values

    def tolist(self):
        return self._values


class FakeSession:
    """Stub that lets us drive SessionManager without spawning a real kernel.

    `evaluate_returns` is a sequence of (expression-matcher, return-value)
    tuples; `evaluate` walks them in order. If nothing matches, returns
    the fallback (which defaults to the FakeSession's `pid` so the
    `$ProcessID` capture in `_create_session_locked` succeeds).
    """

    def __init__(self, pid: int, evaluate_returns=()):
        self.pid = pid
        self.started = False
        self.stopped = False
        self.evaluate_calls: list = []
        self._evaluate_returns = list(evaluate_returns)

    def set_parameter(self, _name, _value):
        return None

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.started = False

    def evaluate(self, expr):
        self.evaluate_calls.append(expr)
        text = str(expr)
        for matcher, value in self._evaluate_returns:
            if matcher in text:
                return value
        # Fallback: numeric for $ProcessID, the matcher-less default otherwise.
        return self.pid


def _install_session(monkeypatch, *, pid=4242, evaluate_returns=()):
    """Install a SessionManager whose kernels are FakeSessions."""
    manager = SessionManager()
    monkeypatch.setattr(
        manager,
        "_create_kernel_session",
        lambda: FakeSession(pid, evaluate_returns),
    )
    return manager


def test_create_scratch_does_not_start_main(monkeypatch):
    manager = SessionManager()
    sessions: list[FakeSession] = []

    def fake_create():
        session = FakeSession(1000 + len(sessions))
        sessions.append(session)
        return session

    monkeypatch.setattr(manager, "_create_kernel_session", fake_create)

    manager.create_session("scratch")

    assert [info.name for info in manager.list_sessions()] == ["scratch"]
    assert len(sessions) == 1


def test_main_session_is_created_lazily(monkeypatch):
    manager = SessionManager()
    sessions: list[FakeSession] = []

    def fake_create():
        session = FakeSession(2000 + len(sessions))
        sessions.append(session)
        return session

    monkeypatch.setattr(manager, "_create_kernel_session", fake_create)

    assert manager.list_sessions() == []
    managed = manager.get_session("main")

    assert managed.name == "main"
    assert managed.pid == 2000
    assert sessions[0].started is True


def test_missing_non_main_session_is_not_created(monkeypatch):
    manager = SessionManager()
    monkeypatch.setattr(manager, "_create_kernel_session", lambda: FakeSession(3000))

    with pytest.raises(ValueError, match="does not exist"):
        manager.get_session("missing")

    assert manager.list_sessions() == []


def test_session_create_loads_shared_kernel_mcp(monkeypatch):
    """Regression: scratch kernels must load the SharedKernelMCP paclet so
    SafeEval is available; without it every evaluate() call would fail."""
    manager = _install_session(monkeypatch)

    manager.create_session("scratch")
    fake = next(iter(manager._sessions.values())).session

    paclet_calls = [str(e) for e in fake.evaluate_calls]
    assert any("PacletDirectoryLoad" in c for c in paclet_calls), paclet_calls
    assert any("Needs" in c and "SharedKernelMCP" in c for c in paclet_calls), paclet_calls


# --- evaluate() / SafeEval response handling ----------------------------


def _safe_eval_response(
    status="ok",
    value=None,
    *,
    head="Integer",
    byte_count=16,
    leaf_count=1,
    summary="ok",
    input_form="ok",
    messages=(),
    prints=(),
    out_number=1,
    duration=0.001,
):
    """Shape that wolframclient delivers when SafeEval returns its Association.

    The top-level envelope is a plain dict in most local tests; nested
    Association values can use FakeImmutableMapping to match wolframclient's
    immutable mapping behavior.
    """
    return {
        "status": status,
        "value": value,
        "head": head,
        "byteCount": byte_count,
        "leafCount": leaf_count,
        "summary": summary,
        "inputForm": input_form,
        "messages": tuple(messages),
        "prints": tuple(prints),
        "outNumber": out_number,
        "durationSeconds": duration,
    }


def test_evaluate_threads_through_safeeval_ok(monkeypatch):
    manager = _install_session(
        monkeypatch,
        evaluate_returns=[
            ("SafeEval", _safe_eval_response(
                value=4, summary="4", input_form="4",
                head="Integer", byte_count=16, leaf_count=1,
            )),
        ],
    )

    r = manager.evaluate("2+2")

    assert r.status == "ok"
    assert r.output_summary == "4"
    assert r.head == "Integer"
    assert r.byte_size == 16
    assert r.leaf_count == 1


def test_evaluate_surfaces_parse_error_status(monkeypatch):
    manager = _install_session(
        monkeypatch,
        evaluate_returns=[
            ("SafeEval", _safe_eval_response(
                status="parse_error", head="$Failed", byte_count=0, leaf_count=0,
                summary="$Failed (parse error)", input_form="$Failed",
                messages=("ToExpression::sntxi : Incomplete expression\n",),
            )),
        ],
    )

    r = manager.evaluate("Sqrt[")

    assert r.status == "parse_error"
    assert r.head == "$Failed"
    assert "$Failed" in r.output_summary
    assert any("Incomplete expression" in m for m in r.messages)


def test_evaluate_surfaces_timeout_status(monkeypatch):
    manager = _install_session(
        monkeypatch,
        evaluate_returns=[
            ("SafeEval", _safe_eval_response(
                status="timeout", head="Symbol", value="$Aborted",
                summary="$Aborted", input_form="$Aborted",
            )),
        ],
    )

    r = manager.evaluate("Pause[100]")

    assert r.status == "timeout"


def test_evaluate_returns_kernel_error_when_safeeval_unavailable(monkeypatch):
    """Regression: if Needs[SharedKernelMCP`] fails (paclet not installed),
    `wl.SharedKernelMCP.SafeEval(...)` returns the unevaluated WLFunction.
    We must surface that as kernel_error rather than crashing on a dict
    lookup."""
    # FakeSession.evaluate returns the integer pid for unmatched expressions,
    # which is not a dict → triggers our kernel_error branch.
    manager = _install_session(monkeypatch, pid=9999, evaluate_returns=[])

    r = manager.evaluate("1+1")

    assert r.status == "kernel_error"
    assert any("SafeEval" in m for m in r.messages)


def test_evaluate_native_returns_python_value(monkeypatch):
    """Regression: evaluate_native gives the caller the native Python value
    that wolframclient delivered. Used by kernel_eval_json to check
    JSON-serializability."""
    manager = _install_session(
        monkeypatch,
        evaluate_returns=[
            ("SafeEval", _safe_eval_response(
                value=[1, 2, 3], head="List",
                summary="{1, 2, 3}", input_form="{1, 2, 3}",
            )),
        ],
    )

    env = manager.evaluate_native("Range[3]")

    assert env["status"] == "ok"
    assert env["value"] == [1, 2, 3]
    assert env["inputForm"] == "{1, 2, 3}"


def test_evaluate_native_normalizes_nested_mapping_values(monkeypatch):
    """Regression: a returned WL Association can arrive as an immutable
    Mapping nested inside the SafeEval envelope. kernel_eval_json must see a
    plain dict so JSON-serializable associations are not mislabeled as
    not_json_encodable.
    """
    monkeypatch.setattr(session_mod, "PackedArray", FakePackedArray)
    manager = _install_session(
        monkeypatch,
        evaluate_returns=[
            ("SafeEval", _safe_eval_response(
                value=FakeImmutableMapping({
                    "unicode": "héllo -> infinity",
                    "list": FakePackedArray([1, 2, 3]),
                }),
                head="Association",
                summary="<|...|>",
                input_form="<|\"unicode\" -> \"héllo -> infinity\", \"list\" -> {1, 2, 3}|>",
            )),
        ],
    )

    env = manager.evaluate_native(
        "<|\"unicode\" -> \"héllo -> infinity\", \"list\" -> Range[3]|>"
    )

    assert env["status"] == "ok"
    assert env["value"] == {
        "unicode": "héllo -> infinity",
        "list": [1, 2, 3],
    }
