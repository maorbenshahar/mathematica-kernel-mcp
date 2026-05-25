import pytest

import mathematica_kernel_mcp.server as server_mod
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


def test_documentation_candidate_limit_bounds_expensive_metadata_work():
    from mathematica_kernel_mcp.server import _documentation_candidate_limit

    assert _documentation_candidate_limit(1) == 15
    assert _documentation_candidate_limit(3) == 15
    assert _documentation_candidate_limit(10) == 50
    assert _documentation_candidate_limit(25) == 75


def test_documentation_tokens_preserves_user_case():
    """Regression: tokenizer used to lowercase the query, silently
    destroying information for mixed-case names ("Plot3D" → "plot3d") and
    making the kernel-side search miss them. Now case is preserved; the
    WL side does the case-insensitive match via StringContainsQ."""
    from mathematica_kernel_mcp.server import _documentation_tokens

    assert _documentation_tokens("Plot3D") == ["Plot3D"]
    # Stop words are filtered case-insensitively.
    assert _documentation_tokens("the Fourier transform") == [
        "Fourier", "transform"
    ]
    # Tokens shorter than 3 chars are skipped, but the raw query is the
    # fallback when no tokens survive.
    assert _documentation_tokens("PT") == ["PT"]
    # Empty query yields an empty token list.
    assert _documentation_tokens("   ") == []


def test_wl_string_preserves_non_ascii():
    """Regression: server-side `_wl_string` is inline-substituted into WL
    source. WL's string-literal lexer recognizes `\\:NNNN` for unicode, NOT
    `\\uNNNN`. The default `json.dumps(ensure_ascii=True)` produced `\\uNNNN`
    escapes that the kernel parsed as the literal 6 chars `\\uXXXX`, so a
    documentation query containing Greek letters silently matched nothing."""
    from mathematica_kernel_mcp.server import _wl_string, _wl_string_list

    assert _wl_string("Plot") == '"Plot"'
    # Non-ASCII must pass through as the real codepoint, not as \\uXXXX.
    assert _wl_string("Α") == '"Α"'  # Greek capital alpha, U+0391
    assert "\\u" not in _wl_string("Α")
    assert _wl_string_list(["Α", "β"]) == '{"Α", "β"}'


def test_kernel_eval_json_forwards_messages_on_failure():
    """Regression: when SafeEval returns parse_error / timeout / aborted,
    _kernel_eval_json used to drop everything but `status`, leaving the
    agent with no diagnostic context. Now `messages`, `head`, and
    `inputForm` flow through when present."""
    from mathematica_kernel_mcp.server import _kernel_eval_json

    class FakeManager:
        def evaluate_native(self, code, session_name="main", timeout=30):
            return {
                "status": "parse_error",
                "head": "$Failed",
                "inputForm": "$Failed",
                "messages": ["ToExpression::sntxi: Incomplete expression"],
            }

    out = _kernel_eval_json(FakeManager(), "main", "Sqrt[")
    assert out["status"] == "parse_error"
    assert out["head"] == "$Failed"
    assert out["inputForm"] == "$Failed"
    assert "Incomplete expression" in out["messages"][0]


def test_truncate_result_input_form_preserves_small_resultjson():
    payload = {"resultJSON": [1, 2, 3]}
    result = _truncate_result_input_form(payload, json_max_chars=1000)
    assert result["resultJSON"] == [1, 2, 3]
    assert "resultJSONTruncated" not in result


def test_truncate_result_input_form_caller_max_chars_also_caps_resultjson():
    """Regression: when the caller asks for max_response_chars=N, BOTH
    resultInputForm AND resultJSON should be bounded by N. Earlier code
    only applied N to resultInputForm and let resultJSON pass up to the
    safety cap (20k), so a request for a tiny budget still returned a
    medium-sized JSON payload."""
    payload = {
        "status": "ok",
        "resultInputForm": "x" * 5000,
        "resultJSON": list(range(500)),  # serializes to ~2900 chars
    }

    result = _truncate_result_input_form(
        payload, max_chars=200, json_max_chars=20000
    )

    assert result["resultInputFormTruncated"] is True
    assert len(result["resultInputForm"]) <= 250  # 200 + truncation marker
    assert result["resultJSON"] is None
    assert result["resultJSONTruncated"] is True
    assert result["resultJSONChars"] > 200


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


def test_notebook_get_output_full_uses_json_path_and_caps_in_kernel(monkeypatch):
    """Regression: full/short output retrieval must not go through the
    summarized eval envelope, which quotes and pre-truncates long strings.
    """
    captured = {}

    class FakeBackend:
        mode = "solo"

        def evaluate(self, *_args, **_kwargs):
            raise AssertionError("notebook_get_output should use evaluate_for_json")

        def evaluate_for_json(self, code):
            captured["code"] = code
            return {"output": "abcdef", "chars": 9}

    monkeypatch.setattr(
        server_mod,
        "get_backend_for",
        lambda _path, _manager_factory, timeout=30.0: FakeBackend(),
    )

    result = server_mod.notebook_get_output(
        "/tmp/sample.m", 3, view="full", max_chars=6
    )

    assert result == {
        "out_number": 3,
        "view": "full",
        "output": "abcdef...",
        "is_truncated": True,
    }
    assert "StringTake" in captured["code"]
    assert "UpTo[6]" in captured["code"]
    assert "ToString[Out[3], InputForm]" in captured["code"]
