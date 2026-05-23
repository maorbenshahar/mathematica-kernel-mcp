import pytest

from mathematica_kernel_mcp.backends import _integer_cell_id
from mathematica_kernel_mcp.server import (
    _truncate_result_input_form,
    _validate_kernel_id,
)


@pytest.mark.parametrize(
    "kernel_id",
    ["main", "scratch-abc123", "Probe_1", "analysis.kernel"],
)
def test_validate_kernel_id_accepts_safe_names(kernel_id):
    assert _validate_kernel_id(kernel_id) == kernel_id


@pytest.mark.parametrize(
    "kernel_id",
    ["", "1bad", "has space", "../bad", "shared", "collab", "notebook"],
)
def test_validate_kernel_id_rejects_unsafe_or_reserved_names(kernel_id):
    with pytest.raises(ValueError):
        _validate_kernel_id(kernel_id)


def test_integer_cell_id_reports_clear_error_for_invalid_strings():
    with pytest.raises(ValueError, match="cell_id must be an integer CellID"):
        _integer_cell_id("not-a-cell-id")

    with pytest.raises(ValueError, match="anchor_cell_id must be an integer CellID"):
        _integer_cell_id("not-a-cell-id", label="anchor_cell_id")


def test_truncate_result_input_form_marks_long_outputs_without_mutating():
    payload = {"status": "ok", "resultInputForm": "x" * 12}

    result = _truncate_result_input_form(payload, max_chars=5)

    assert result["resultInputForm"] == "xxxxx... [truncated 7 chars]"
    assert result["resultInputFormTruncated"] is True
    assert result["resultInputFormChars"] == 12
    assert payload["resultInputForm"] == "x" * 12


def test_truncate_result_input_form_recurses_into_batch_results():
    payload = {"results": [{"resultInputForm": "abcdef"}, {"resultInputForm": "ok"}]}

    result = _truncate_result_input_form(payload, max_chars=3)

    assert result["results"][0]["resultInputForm"] == "abc... [truncated 3 chars]"
    assert result["results"][0]["resultInputFormTruncated"] is True
    assert result["results"][1]["resultInputForm"] == "ok"


def test_truncate_result_input_form_drops_oversized_resultjson():
    """Regression: bridge can return resultJSON up to 200k chars, which can
    blow past the MCP transport's per-message limit. Truncate by dropping the
    field (caller can fall back to resultInputForm) rather than letting the
    response error out."""
    payload = {
        "status": "ok",
        "resultJSON": "x" * 50000,
    }

    result = _truncate_result_input_form(payload, json_max_chars=1000)

    assert result["resultJSON"] is None
    assert result["resultJSONTruncated"] is True
    assert result["resultJSONChars"] == 50002  # 50000 chars + 2 quotes


def test_truncate_result_input_form_preserves_small_resultjson():
    payload = {"resultJSON": [1, 2, 3]}
    result = _truncate_result_input_form(payload, json_max_chars=1000)
    assert result["resultJSON"] == [1, 2, 3]
    assert "resultJSONTruncated" not in result


def test_truncate_result_input_form_bounds_code_messages_and_prints():
    payload = {
        "status": "ok",
        "code": "c" * 9,
        "messages": ["ok", "m" * 8],
        "prints": ["p" * 10],
    }

    result = _truncate_result_input_form(
        payload,
        max_chars=4,
        message_print_max_chars=4,
        code_max_chars=5,
    )

    assert result["code"] == "ccccc... [truncated 4 chars]"
    assert result["codeTruncated"] is True
    assert result["codeChars"] == 9
    assert result["messages"] == ["ok", "mmmm... [truncated 4 chars]"]
    assert result["messagesTruncated"] is True
    assert result["messagesChars"] == [2, 8]
    assert result["prints"] == ["pppp... [truncated 6 chars]"]
    assert result["printsTruncated"] is True
    assert result["printsChars"] == [10]
    assert payload["prints"] == ["p" * 10]
