import json
import socket
import threading

import pytest

from mathematica_kernel_mcp.bridge import BridgeError, SharedKernelBridge


def make_bridge_root(tmp_path):
    (tmp_path / "queue").mkdir()
    (tmp_path / "results").mkdir()
    return tmp_path


def write_connection(root, **overrides):
    payload = {
        "transport": "socket",
        "protocol": "jsonl-content-length-v1",
        "host": "127.0.0.1",
        "port": 54321,
        "token": "secret",
    }
    payload.update(overrides)
    (root / "connection.json").write_text(json.dumps(payload), encoding="utf-8")


def framed_response(payload: bytes) -> bytes:
    return b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload


def test_socket_connection_metadata_is_loaded(tmp_path):
    root = make_bridge_root(tmp_path)
    write_connection(root)

    bridge = SharedKernelBridge(root)

    assert bridge.socket_connection is not None
    assert bridge.socket_connection.host == "127.0.0.1"
    assert bridge.socket_connection.port == 54321
    assert bridge.socket_connection.token == "secret"
    assert bridge.socket_connection.protocol == "jsonl-content-length-v1"


def test_non_socket_connection_metadata_is_ignored(tmp_path):
    root = make_bridge_root(tmp_path)
    write_connection(root, transport="file")

    bridge = SharedKernelBridge(root)

    assert bridge.socket_connection is None


def test_socket_connection_must_be_loopback(tmp_path):
    root = make_bridge_root(tmp_path)
    write_connection(root, host="203.0.113.10")

    with pytest.raises(BridgeError, match="loopback"):
        SharedKernelBridge(root)


def test_socket_connection_requires_valid_port_and_token(tmp_path):
    root = make_bridge_root(tmp_path)
    write_connection(root, port=70000)

    with pytest.raises(BridgeError, match="invalid port or token"):
        SharedKernelBridge(root)


def test_socket_call_sends_authenticated_json_line(tmp_path):
    root = make_bridge_root(tmp_path)
    received = {}
    ready = threading.Event()

    def serve_once():
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
                conn.sendall(framed_response(
                    b'{\n'
                    b'  "status":"ok",\n'
                    b'  "resultInputForm":"2",\n'
                    b'  "resultJSON":2\n'
                    b'}\n'
                ))

    thread = threading.Thread(target=serve_once)
    thread.start()
    assert ready.wait(timeout=5)
    write_connection(root, port=received["port"])

    bridge = SharedKernelBridge(root, timeout=5)
    result = bridge.evaluate("1+1")

    thread.join(timeout=5)
    assert result["status"] == "ok"
    assert result["resultJSON"] == 2
    assert received["request"]["token"] == "secret"
    assert received["request"]["code"] == "(*SILENT*)\n1+1"


def test_socket_call_supports_legacy_unframed_response(tmp_path):
    root = make_bridge_root(tmp_path)
    received = {}
    ready = threading.Event()

    def serve_once():
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
                conn.sendall(b'{"status":"ok","resultJSON":4}\n')

    thread = threading.Thread(target=serve_once)
    thread.start()
    assert ready.wait(timeout=5)
    write_connection(root, port=received["port"], protocol="json-lines")

    bridge = SharedKernelBridge(root, timeout=5)
    result = bridge.evaluate("2+2")

    thread.join(timeout=5)
    assert result["status"] == "ok"
    assert result["resultJSON"] == 4


def test_socket_call_rejects_truncated_framed_response(tmp_path):
    root = make_bridge_root(tmp_path)
    ready = threading.Event()

    def serve_once():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            write_connection(root, port=port)
            ready.set()
            conn, _ = server.accept()
            with conn:
                while not conn.recv(4096).endswith(b"\n"):
                    pass
                conn.sendall(b"Content-Length: 10\r\n\r\n{}")

    thread = threading.Thread(target=serve_once)
    thread.start()
    assert ready.wait(timeout=5)

    bridge = SharedKernelBridge(root, timeout=5)
    with pytest.raises(BridgeError, match="expected 10"):
        bridge.evaluate("1")

    thread.join(timeout=5)


def test_compact_read_uses_new_bridge_signature_for_v2_socket(tmp_path):
    root = make_bridge_root(tmp_path)
    received = {}
    ready = threading.Event()

    def serve_once():
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
                conn.sendall(framed_response(b'{"status":"ok","resultJSON":{"cells":[]}}'))

    thread = threading.Thread(target=serve_once)
    thread.start()
    assert ready.wait(timeout=5)
    write_connection(root, port=received["port"])

    bridge = SharedKernelBridge(root, timeout=5)
    bridge.read_notebook("/tmp/test.nb", include_content=False, preview_chars=17)

    thread.join(timeout=5)
    assert 'BridgeReadNotebook["/tmp/test.nb", False, 17]' in received["request"]["code"]


def test_compact_read_keeps_legacy_bridge_signature_for_old_socket(tmp_path):
    root = make_bridge_root(tmp_path)
    received = {}
    ready = threading.Event()

    def serve_once():
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
                conn.sendall(b'{"status":"ok","resultJSON":{"cells":[]}}\n')

    thread = threading.Thread(target=serve_once)
    thread.start()
    assert ready.wait(timeout=5)
    write_connection(root, port=received["port"], protocol="json-lines")

    bridge = SharedKernelBridge(root, timeout=5)
    bridge.read_notebook("/tmp/test.nb", include_content=False, preview_chars=17)

    thread.join(timeout=5)
    assert 'BridgeReadNotebook["/tmp/test.nb"]' in received["request"]["code"]
