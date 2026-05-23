import json
import os
import socket
import threading

import pytest

from mathematica_kernel_mcp.bridge import (
    REGISTRY_ENV_VAR,
    BridgeError,
    SharedKernelBridge,
    SocketConnection,
    bridge_record_for_file,
    discover_bridge_records,
)


def isolate_registry(monkeypatch, tmp_path):
    registry = tmp_path / "registry"
    registry.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv(REGISTRY_ENV_VAR, str(registry))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("APPDATA", raising=False)
    return registry


def write_registry_record(registry, **overrides):
    payload = {
        "schemaVersion": 2,
        "transport": "Socket",
        "protocol": "jsonl-content-length-v1",
        "host": "127.0.0.1",
        "port": 54321,
        "token": "secret",
        "kernelPID": os.getpid(),
        "notebookPath": "/tmp/demo.nb",
        "notebooks": [{"path": "/tmp/demo.nb"}],
        "createdAt": "2026-05-20 12:00:00",
        "lastSeen": "2026-05-20 12:00:00",
    }
    payload.update(overrides)
    path = registry / overrides.get("filename", "bridge.json")
    payload.pop("filename", None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def framed_response(payload: bytes) -> bytes:
    return b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload


def serve_once(response: bytes, received: dict, ready: threading.Event):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        received["port"] = server.getsockname()[1]
        ready.set()
        conn, _ = server.accept()
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                data += conn.recv(4096)
            received["request"] = json.loads(data.decode("utf-8"))
            conn.sendall(response)


def test_socket_call_sends_authenticated_json_line():
    received = {}
    ready = threading.Event()
    thread = threading.Thread(
        target=serve_once,
        args=(
            framed_response(
                b'{\n'
                b'  "status":"ok",\n'
                b'  "resultInputForm":"2",\n'
                b'  "resultJSON":2\n'
                b'}\n'
            ),
            received,
            ready,
        ),
    )
    thread.start()
    assert ready.wait(timeout=5)

    bridge = SharedKernelBridge(
        SocketConnection("127.0.0.1", received["port"], "secret"),
        timeout=5,
    )
    result = bridge.evaluate("1+1")

    thread.join(timeout=5)
    assert result["status"] == "ok"
    assert result["resultJSON"] == 2
    assert received["request"]["token"] == "secret"
    # Protocol v3: code is the raw user input; silent/eval_timeout/full_json
    # come as structured JSON fields, not as comment-marker injection.
    assert received["request"]["code"] == "1+1"
    assert received["request"]["silent"] is True
    assert "eval_timeout" not in received["request"] or received["request"]["eval_timeout"] is None
    assert received["request"]["full_json"] is False


def test_socket_call_supports_unframed_response():
    received = {}
    ready = threading.Event()
    thread = threading.Thread(
        target=serve_once,
        args=(b'{"status":"ok","resultJSON":4}\n', received, ready),
    )
    thread.start()
    assert ready.wait(timeout=5)

    bridge = SharedKernelBridge(
        SocketConnection(
            "127.0.0.1",
            received["port"],
            "secret",
            protocol="json-lines",
        ),
        timeout=5,
    )
    result = bridge.evaluate("2+2")

    thread.join(timeout=5)
    assert result["status"] == "ok"
    assert result["resultJSON"] == 4


def test_socket_call_rejects_truncated_framed_response():
    received = {}
    ready = threading.Event()
    thread = threading.Thread(
        target=serve_once,
        args=(b"Content-Length: 10\r\n\r\n{}", received, ready),
    )
    thread.start()
    assert ready.wait(timeout=5)

    bridge = SharedKernelBridge(
        SocketConnection("127.0.0.1", received["port"], "secret"),
        timeout=5,
    )
    with pytest.raises(BridgeError, match="expected 10"):
        bridge.evaluate("1")

    thread.join(timeout=5)


def test_compact_read_uses_preview_bridge_signature():
    received = {}
    ready = threading.Event()
    thread = threading.Thread(
        target=serve_once,
        args=(
            framed_response(b'{"status":"ok","resultJSON":{"cells":[]}}'),
            received,
            ready,
        ),
    )
    thread.start()
    assert ready.wait(timeout=5)

    bridge = SharedKernelBridge(
        SocketConnection("127.0.0.1", received["port"], "secret"),
        timeout=5,
    )
    bridge.read_notebook("/tmp/test.nb", include_content=False, preview_chars=17)

    thread.join(timeout=5)
    # Protocol v3: full_json is now a structured field, not a comment marker.
    assert received["request"]["full_json"] is True
    assert 'BridgeReadNotebook["/tmp/test.nb", False, 17]' in received["request"]["code"]


def test_registry_records_are_discovered_and_tokens_are_redacted(tmp_path, monkeypatch):
    registry = isolate_registry(monkeypatch, tmp_path)
    notebook = tmp_path / "notebooks" / "demo.nb"
    notebook.parent.mkdir()
    notebook.write_text("Notebook[{}]", encoding="utf-8")
    write_registry_record(registry, notebookPath=str(notebook), notebooks=[{"path": str(notebook)}])

    records = discover_bridge_records()

    assert len(records) == 1
    assert records[0]["is_alive"] is True
    assert records[0]["socket_ok"] is True
    assert "token" not in records[0]

    redacted = bridge_record_for_file(notebook, include_tokens=False)
    assert redacted is not None
    assert "token" not in redacted

    full = bridge_record_for_file(notebook, include_tokens=True)
    assert full is not None
    assert full["token"] == "secret"


def test_for_file_falls_back_to_registry_record(tmp_path, monkeypatch):
    registry = isolate_registry(monkeypatch, tmp_path)
    notebook = tmp_path / "registry-only.nb"
    notebook.write_text("Notebook[{}]", encoding="utf-8")
    received = {}
    ready = threading.Event()
    thread = threading.Thread(
        target=serve_once,
        args=(framed_response(b'{"status":"ok","resultJSON":5}'), received, ready),
    )
    thread.start()
    assert ready.wait(timeout=5)
    write_registry_record(
        registry,
        notebookPath=str(notebook),
        notebooks=[{"path": str(notebook)}],
        port=received["port"],
    )

    bridge = SharedKernelBridge.for_file(notebook, timeout=5)
    result = bridge.evaluate("2+3")

    thread.join(timeout=5)
    assert result["resultJSON"] == 5
    assert received["request"]["token"] == "secret"


def test_invalid_socket_registry_records_are_stale(tmp_path, monkeypatch):
    registry = isolate_registry(monkeypatch, tmp_path)
    notebook = tmp_path / "demo.nb"
    notebook.write_text("Notebook[{}]", encoding="utf-8")
    record = write_registry_record(
        registry,
        notebookPath=str(notebook),
        notebooks=[{"path": str(notebook)}],
        host="203.0.113.10",
    )

    assert discover_bridge_records() == []
    assert bridge_record_for_file(notebook) is None

    stale = discover_bridge_records(include_stale=True, prune_stale=True)

    assert len(stale) == 1
    assert stale[0]["socket_ok"] is False
    assert not record.exists()
