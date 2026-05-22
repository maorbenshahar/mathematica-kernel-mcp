import pytest

from mathematica_kernel_mcp.session import SessionManager


class FakeSession:
    def __init__(self, pid: int):
        self.pid = pid
        self.started = False
        self.stopped = False

    def set_parameter(self, _name, _value):
        return None

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        self.started = False

    def evaluate(self, _expr):
        return self.pid


class FakeWrap:
    def __init__(self, result, messages=()):
        self.result = result
        self.messages = list(messages)


def _install_evaluating_session(monkeypatch, *, status_value, messages=(), meta=None):
    """Install a manager whose 'main' session returns a stubbed eval result.

    `status_value` is what `evaluate_wrap` returns as the status sentinel.
    `meta` is the list returned by the secondary `evaluate(meta_code)` call
    when status is "ok".
    """
    manager = SessionManager()

    class StubSession(FakeSession):
        def __init__(self, pid):
            super().__init__(pid)
            self.evaluate_wrap_calls: list = []

        def evaluate_wrap(self, expr):
            self.evaluate_wrap_calls.append(expr)
            return FakeWrap(status_value, messages)

        def evaluate(self, expr):
            self.last_meta_expr = expr
            return meta if meta is not None else self.pid

    monkeypatch.setattr(manager, "_create_kernel_session", lambda: StubSession(4242))
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


def test_evaluate_returns_parse_error_status_when_kernel_reports_it(monkeypatch):
    manager = _install_evaluating_session(
        monkeypatch,
        status_value="parse_error",
        messages=[("Message", "Invalid syntax in or before Sqrt[")],
    )

    result = manager.evaluate("Sqrt[")

    assert result.status == "parse_error"
    assert result.head == "$Failed"
    assert result.output_summary.startswith("$Failed")
    assert any("Invalid syntax" in m for m in result.messages)


def test_evaluate_returns_timeout_status_when_kernel_reports_it(monkeypatch):
    manager = _install_evaluating_session(monkeypatch, status_value="timeout")

    result = manager.evaluate("Pause[10]")

    assert result.status == "timeout"
    assert result.head == "$Aborted"
    assert "timed out" in result.output_summary


def test_evaluate_returns_ok_status_for_normal_eval(monkeypatch):
    manager = _install_evaluating_session(
        monkeypatch,
        status_value="ok",
        meta=["4", "Integer", 16, 1],
    )

    result = manager.evaluate("2+2")

    assert result.status == "ok"
    assert result.output_summary == "4"
    assert result.head == "Integer"
    assert result.byte_size == 16
    assert result.leaf_count == 1


def test_evaluate_does_not_misclassify_legitimate_aborted_result(monkeypatch):
    """Regression: user code returning the literal symbol `$Aborted` must
    surface as status='ok', not 'timeout' (the old string-compare heuristic
    would mis-label it)."""
    manager = _install_evaluating_session(
        monkeypatch,
        status_value="ok",
        meta=["$Aborted", "Symbol", 8, 1],
    )

    result = manager.evaluate("$Aborted")

    assert result.status == "ok"
    assert result.head == "Symbol"


def test_evaluate_wraps_multi_arg_holdcomplete_into_compound_expression(monkeypatch):
    """Regression: ToExpression on newline-separated multi-statement input
    yields HoldComplete[a, b, c] (multi-arg) instead of one CompoundExpression.
    Without the Replace step, the eval would assign Sequence[Null, Null, last]
    and downstream meta/int calls would choke. We assert the WL sent to the
    kernel includes the Replace fix."""
    manager = SessionManager()
    captured: list = []

    class CapturingSession(FakeSession):
        def evaluate_wrap(self, expr):
            captured.append(str(expr))
            return FakeWrap("ok")

        def evaluate(self, _expr):
            return ["1", "Integer", 0, 1]

    monkeypatch.setattr(
        manager, "_create_kernel_session", lambda: CapturingSession(5555)
    )

    manager.evaluate("f[x_]:=x; f[1]")

    assert captured, "evaluate_wrap was never called"
    wl = captured[0]
    assert "HoldComplete[args___]" in wl
    assert "HoldComplete[CompoundExpression[args]]" in wl


def test_evaluate_passes_unicode_code_without_uXXXX_escapes(monkeypatch):
    """Regression: json.dumps defaults to ensure_ascii=True, emitting \\uXXXX
    escapes that WL's string parser rejects with 'Unknown string escape \\u'.
    Code paths must use ensure_ascii=False so Unicode chars pass through."""
    manager = SessionManager()
    captured: list = []

    class CapturingSession(FakeSession):
        def evaluate_wrap(self, expr):
            captured.append(str(expr))
            return FakeWrap("ok")

        def evaluate(self, _expr):
            return ["1", "Integer", 0, 1]

    monkeypatch.setattr(
        manager, "_create_kernel_session", lambda: CapturingSession(7777)
    )

    manager.evaluate('"héllo → ∞"')

    assert captured, "evaluate_wrap was never called"
    wl = captured[0]
    # Unicode chars must appear literally, not as \uXXXX escapes.
    assert "héllo → ∞" in wl
    assert "\\u00e9" not in wl
    assert "\\u2192" not in wl
