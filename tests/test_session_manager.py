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
