"""Integration tests for session manager (requires wolframscript)."""

import pytest

from mathematica_kernel_mcp.session import SessionManager


@pytest.fixture(scope="module")
def manager():
    """Create a session manager with a main kernel for the test module."""
    m = SessionManager()
    m.start()
    yield m
    m.stop()


def test_basic_eval(manager):
    result = manager.evaluate("1 + 1")
    assert result.output_summary == "2"
    assert result.messages == []


def test_state_persists(manager):
    manager.evaluate("testVar = 42")
    result = manager.evaluate("testVar")
    assert result.output_summary == "42"


def test_function_definition(manager):
    manager.evaluate("g[x_] := x^2 + 1")
    result = manager.evaluate("g[5]")
    assert result.output_summary == "26"


def test_out_number_increments(manager):
    r1 = manager.evaluate("1")
    r2 = manager.evaluate("2")
    assert r2.out_number == r1.out_number + 1


def test_messages_captured(manager):
    result = manager.evaluate("1/0")
    assert len(result.messages) > 0


def test_timeout_returns_aborted(manager):
    result = manager.evaluate("Pause[10]", timeout=2)
    assert "$Aborted" in result.output_summary


def test_kernel_survives_timeout(manager):
    """After a timeout, the kernel should still work."""
    manager.evaluate("Pause[10]", timeout=2)
    result = manager.evaluate("1 + 1")
    assert result.output_summary == "2"


def test_evaluate_raw(manager):
    raw = manager.evaluate_raw("ToString[2 + 2]")
    assert "4" in raw


def test_named_session(manager):
    manager.create_session("worker1")
    result = manager.evaluate("workerVar = 99", session_name="worker1")
    assert "99" in result.output_summary

    # worker's variable should not be in main
    raw = manager.evaluate_raw("ToString[Head[workerVar]]")
    assert "Symbol" in raw  # undefined symbol has Head Symbol

    manager.close_session("worker1")


def test_cannot_close_main(manager):
    with pytest.raises(ValueError, match="Cannot close the main session"):
        manager.close_session("main")


def test_list_sessions(manager):
    sessions = manager.list_sessions()
    names = [s.name for s in sessions]
    assert "main" in names
