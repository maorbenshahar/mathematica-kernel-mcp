from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

INIT_M = (
    ROOT
    / "wolfram"
    / "SharedKernelMCP"
    / "Kernel"
    / "init.m"
)
SERVER_PY = ROOT / "src" / "mathematica_kernel_mcp" / "server.py"


def _definition_between(source: str, name: str, next_name: str) -> str:
    start = source.index(f"\n{name}[")
    end = source.index(f"\n{next_name}[", start)
    return source[start:end]


def test_bridge_run_cell_does_not_reference_socket_silent_marker():
    body = _definition_between(INIT_M.read_text(), "BridgeRunCell", "BridgeUpdateCell")

    assert "$Messages = If[silent" not in body
    assert "silent" not in body


def test_evaluate_bridge_command_uses_structured_opts():
    """Regression (protocol v3): evaluateBridgeCommand now receives structured
    `silent`/`eval_timeout`/`full_json` via the opts Association rather than
    parsing them out of `code` as `(*SILENT*)` / `(*TIMEOUT:N*)` markers.
    Marker injection collided with any user code starting with those
    literals."""
    body = _definition_between(
        INIT_M.read_text(),
        "evaluateBridgeCommand",
        "socketBridgeToken",
    )

    # Marker-comment parsing is gone.
    assert "(*SILENT*)" not in body
    assert "(*TIMEOUT:" not in body
    assert "StringStartsQ" not in body
    # Structured opts replace it.
    assert 'Lookup[opts, "silent"' in body
    assert 'Lookup[opts, "eval_timeout"' in body
    # The bridge now delegates the actual eval pipeline to SafeEval.
    assert "SafeEval[code" in body


def test_bridge_run_cell_refuses_non_executable_cells():
    """Regression: BridgeRunCell used to send Section/Title/Text content
    straight to the kernel as WL code, so running a Section cell with content
    "integration functions" produced `integration*functions` (implicit
    multiplication). Solo refuses with status="skipped"; collab must too."""
    body = _definition_between(INIT_M.read_text(), "BridgeRunCell", "BridgeUpdateCell")

    # The style-gate is present before the eval.
    assert "cellStyleOf[NotebookRead[target]]" in body
    assert 'MemberQ[{"Input", "Code"}, cellStyle]' in body
    assert '"status" -> "skipped"' in body
    assert '"reason" -> "not_executable"' in body


def test_documentation_search_caps_heavy_metadata_candidates():
    """Regression: short/common docs queries can match many System` names.
    The implementation must cap candidates before calling WolframLanguageData
    so max_results is not applied only after hundreds of metadata lookups.
    """
    source = SERVER_PY.read_text()
    body = source[source.index("def notebook_documentation_search("):]
    body = body[: body.index("\n\n@mcp.tool()", 1)]

    sort_pos = body.index("candidates = Take[")
    metadata_pos = body.index("WolframLanguageData[entity")
    assert sort_pos < metadata_pos
    assert "candidateLimit" in body
    assert "UpTo[candidateLimit]" in body


def test_notebook_symbol_info_gates_on_names_to_avoid_polluting_global():
    """Regression: notebook_symbol_info used to interpret an unknown bare
    name (e.g. "FFT") via ToExpression, which interns the symbol in Global`
    as a side-effect. Now Names[name] is consulted FIRST (Names doesn't
    create symbols); only an existing symbol gets passed to ToExpression."""
    source = SERVER_PY.read_text()
    body = source[source.index("def notebook_symbol_info("):]
    body = body[: body.index("\n\n@mcp.tool()")]

    # Names check happens before ToExpression and short-circuits when empty.
    names_pos = body.index("Names[")
    to_expression_pos = body.index("ToExpression[")
    assert names_pos < to_expression_pos, (
        "Names[] check must run before ToExpression to avoid creating the symbol"
    )
    assert '"error" -> "symbol_not_found"' in body


def test_normalize_cell_ids_preserves_dynamic_in_out_labels():
    """Regression: assigning a CellID during notebook_read used to clobber
    the front-end's In[N]:= / Out[N]= labels because NotebookWrite replaces
    the whole cell expression. The fix captures CurrentValue[cellObj,
    CellLabel] before rewriting and bakes it back in via the
    cellWithPreservedCellID helper."""
    source = INIT_M.read_text()
    body = _definition_between(source, "normalizeCellIDsInNotebook", "assignCellIDsToNotebook")

    # The normalization path now goes through the label-preserving helper.
    assert "cellWithPreservedCellID" in body
    # The helper itself queries the current dynamic CellLabel.
    helper = source[source.index("cellWithPreservedCellID["):]
    assert 'CurrentValue[cellObj, CellLabel]' in helper
    # And re-applies it as an explicit option (with AutoDelete -> False so
    # the front-end doesn't immediately discard it).
    assert "CellLabel -> dynamicLabel" in helper
    assert "CellLabelAutoDelete -> False" in helper


def test_autostart_pre_clears_itself_after_successful_attach():
    """Regression: the autostart $Pre hook must Unset itself once
    StartSharedKernelBridge reports Running -> True, otherwise the wrapper
    runs on every user evaluation forever and can collide with anything else
    the user assigns to $Pre."""
    source = INIT_M.read_text()
    start = source.index("autostartBlock[")
    end = source.index("\nstringify[", start)
    block = source[start:end]

    # The hook must consult the bridge's return value before declaring success.
    assert 'Lookup[status, \\"Running\\", False]' in block
    # And then remove itself.
    assert "Unset[$Pre]" in block


def test_safeeval_stores_private_and_best_effort_out_history():
    """Regression: direct Out[n] assignment is protected in WSTP kernels.
    SafeEval must write the private MCP store used by kernel_get_output, while
    also attempting to mirror Mathematica Out[n] history.
    """
    source = INIT_M.read_text()
    helper = source[source.index("storeOutHistory["):source.index("safeEvalParse[")]
    safe_eval = _definition_between(source, "SafeEval", "appendNotebookCell")

    assert "Global`wolfram$mcp$out[outNumber] = result" in helper
    assert "Unprotect[Out]" in helper
    assert "Out[outNumber] = result" in helper
    assert "storeOutHistory[storeOut, result]" in safe_eval


def test_kernel_get_output_reads_private_safeeval_history():
    """Regression: kernel_get_output must read wolfram$mcp$out[n], the stable
    history slot written by SafeEval, not direct Out[n] alone.
    """
    source = SERVER_PY.read_text()
    start = source.index("def kernel_get_output(")
    end = source.index("\n\n@mcp.tool()\ndef notebook_run_cells", start)
    body = source[start:end]

    assert 'ref = f"wolfram$mcp$out[{int(out_number)}]"' in body
