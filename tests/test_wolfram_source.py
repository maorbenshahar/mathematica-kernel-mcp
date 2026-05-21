from pathlib import Path


INIT_M = (
    Path(__file__).resolve().parents[1]
    / "wolfram"
    / "SharedKernelMCP"
    / "Kernel"
    / "init.m"
)


def _definition_between(source: str, name: str, next_name: str) -> str:
    start = source.index(f"\n{name}[")
    end = source.index(f"\n{next_name}[", start)
    return source[start:end]


def test_bridge_run_cell_does_not_reference_socket_silent_marker():
    body = _definition_between(INIT_M.read_text(), "BridgeRunCell", "BridgeUpdateCell")

    assert "$Messages = If[silent" not in body
    assert "silent" not in body


def test_evaluate_bridge_command_owns_silent_message_suppression():
    body = _definition_between(
        INIT_M.read_text(),
        "evaluateBridgeCommand",
        "socketBridgeToken",
    )

    assert "silent = StringStartsQ" in body
    assert "$Messages = If[silent, {}, $Messages]" in body
