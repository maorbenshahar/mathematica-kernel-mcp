"""FastMCP server with Wolfram Language tools."""

import json
import logging
import re
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path
from textwrap import dedent

from fastmcp import FastMCP

from mathematica_kernel_mcp.notebook import (
    create_nb_cell,
    delete_nb_cell,
    is_notebook_path,
    parse_nb_file_with_kernel,
    sync_nb_output_cells,
    update_nb_cell,
)
from mathematica_kernel_mcp.parser import create_m_cell, delete_m_cell, parse_file, update_m_cell
from mathematica_kernel_mcp.session import SessionManager

logger = logging.getLogger(__name__)

_manager: SessionManager | None = None

_EXECUTABLE_CELL_TYPES = {"Input", "Code"}
_SYMBOL_INFO_FIELDS = {
    "usage",
    "definition",
    "attributes",
    "options",
    "context",
    "own_values",
    "down_values",
    "up_values",
    "sub_values",
    "messages",
}
_GET_OUTPUT_VIEWS = {"full", "short", "summary"}
_KERNEL_STATE_FIELDS = {
    "memory_bytes": "MemoryInUse[]",
    "max_memory_bytes": "MaxMemoryUsed[]",
    "packages": "$Packages",
    "context": "$Context",
    "context_path": "$ContextPath",
    "version": "ToString[$VersionNumber]",
}
_DEFAULT_KERNEL_STATE_FIELDS = ["memory_bytes", "max_memory_bytes", "context", "version"]
_MAX_DOCUMENTATION_RESULTS = 25
_DOCUMENTATION_STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "function",
    "functions",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "symbolic",
    "the",
    "to",
    "with",
}
_TEXT_HELPERS_WL = dedent(
    """
    normalizeDisplayText[text_] := Module[{normalized},
        normalized = ToString[text, OutputForm];
        normalized = StringReplace[
            normalized,
            {
                "\\r\\n" -> "\\n",
                "\\r" -> "\\n",
                "\\t" -> "    ",
                "\\[Ellipsis]" -> "...",
                FromCharacterCode[8230] -> "...",
                "\\[Element]" -> " in ",
                FromCharacterCode[8712] -> " in ",
                "\\[Rule]" -> " -> "
                ,
                FromCharacterCode[8594] -> " -> ",
                FromCharacterCode[8658] -> " => ",
                FromCharacterCode[8804] -> " <= ",
                FromCharacterCode[8805] -> " >= ",
                FromCharacterCode[8734] -> "Infinity"
            }
        ];
        normalized = StringReplace[
            normalized,
            {
                RegularExpression["[ ]+\\n"] -> "\\n",
                RegularExpression["\\n[ ]+"] -> "\\n",
                RegularExpression["\\n{3,}"] -> "\\n\\n"
            }
        ];
        StringTrim[normalized]
    ];

    symbolUsageText[s_Symbol] := Module[{docUsage, fallback},
        docUsage = If[
            Context[s] === "System`",
            Quiet[
                Check[
                    WolframLanguageData[
                        Entity["WolframLanguageSymbol", SymbolName[s]],
                        "PlaintextUsage"
                    ],
                    Missing["NotAvailable"]
                ]
            ],
            Missing["NotAvailable"]
        ];
        fallback = Quiet[Check[MessageName[s, "usage"], Missing["NotAvailable"]]];
        normalizeDisplayText[
            Replace[
                docUsage,
                (_Missing | $Failed) :> Replace[fallback, (_Missing | $Failed) :> "None"]
            ]
        ]
    ];
    """
)


def get_manager() -> SessionManager:
    if _manager is None:
        raise RuntimeError("Session manager not initialized")
    return _manager


@asynccontextmanager
async def lifespan(app):
    """Start/stop the session manager with the MCP server."""
    global _manager
    _manager = SessionManager()
    _manager.start()
    logger.info("Mathematica Kernel MCP server started, main kernel ready")
    try:
        yield
    finally:
        _manager.stop()
        _manager = None
        logger.info("Mathematica Kernel MCP server stopped")


def _is_executable_cell(cell) -> bool:
    return cell.cell_type in _EXECUTABLE_CELL_TYPES


def _cell_preview(content: str, max_chars: int = 80) -> str:
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "..."


def _normalize_terminal_text(text: str) -> str:
    normalized = text
    if any(marker in normalized for marker in ("Ã", "â", "Î", "ï", "ð")):
        try:
            normalized = normalized.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

    normalized = unicodedata.normalize("NFKC", normalized)
    replacements = {
        "…": "...",
        "∈": " in ",
        "→": " -> ",
        "⇒": " => ",
        "≤": " <= ",
        "≥": " >= ",
        "∞": "Infinity",
        "∫": "Integral ",
        "ⅆ": "d",
        "": "d",
    }
    for old, new in replacements.items():
        normalized = normalized.replace(old, new)
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _normalized_path(path: str) -> str:
    return str(Path(path).resolve())


def _attach_runtime_metadata(path: str, parsed_cells: list, session: str) -> list:
    normalized_path = _normalized_path(path)
    manager = get_manager()
    for cell in parsed_cells:
        cell.runtime = manager.get_cell_run_info(
            normalized_path,
            cell.cell_id,
            session_name=session,
        )
    return parsed_cells


def _parse_cells(path: str, session: str = "main") -> list:
    if is_notebook_path(path):
        return parse_nb_file_with_kernel(path, get_manager(), session_name=session)
    return parse_file(path)


def _outline_entry(cell, include_content: bool = False) -> dict:
    entry = {
        "cell": cell.number,
        "cell_id": cell.cell_id,
        "type": cell.cell_type,
        "lines": f"{cell.line_start}-{cell.line_end}",
        "executable": _is_executable_cell(cell),
        "last_in": cell.runtime.last_in,
        "last_out": cell.runtime.last_out,
        "messages": cell.runtime.messages,
        "last_run_at": cell.runtime.last_run_at,
    }
    if include_content:
        entry["content"] = cell.content
    else:
        entry["preview"] = _cell_preview(cell.content)
    return entry


def _validate_symbol_info_fields(fields: list[str] | None) -> list[str] | str:
    if fields is None:
        return sorted(_SYMBOL_INFO_FIELDS)
    invalid = sorted(set(fields) - _SYMBOL_INFO_FIELDS)
    if invalid:
        return f"Unsupported symbol_info fields: {', '.join(invalid)}"
    return fields


def _validate_kernel_state_fields(fields: list[str] | None) -> list[str] | str:
    if fields is None:
        return list(_DEFAULT_KERNEL_STATE_FIELDS)
    invalid = sorted(set(fields) - set(_KERNEL_STATE_FIELDS))
    if invalid:
        return f"Unsupported kernel_state fields: {', '.join(invalid)}"
    return fields


def _normalize_context_query(context: str) -> tuple[str, str]:
    normalized = context.strip()
    if not normalized:
        raise ValueError("Context must be non-empty")
    if "*" in normalized or "@" in normalized:
        return normalized, normalized
    if not normalized.endswith("`"):
        normalized += "`"
    return normalized, f"{normalized}*"


def _symbol_list_expr(expr: str) -> str:
    return f'If[Length[{expr}] > 0, ToString[{expr}, InputForm], "None"]'


def _wl_string_list(values: list[str]) -> str:
    if not values:
        return "{}"
    return "{" + ", ".join(json.dumps(value) for value in values) + "}"


def _documentation_tokens(query: str) -> list[str]:
    tokens = []
    seen = set()
    raw_tokens = re.findall(r"[A-Za-z$][A-Za-z0-9$]*", query.lower())
    for token in raw_tokens:
        if len(token) < 3 or token in _DOCUMENTATION_STOP_WORDS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    if not tokens:
        for token in raw_tokens:
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return tokens


def _validate_max_results(max_results: int) -> int:
    if max_results < 1:
        raise ValueError("`max_results` must be at least 1")
    return min(max_results, _MAX_DOCUMENTATION_RESULTS)


def _create_cell_impl(
    path: str,
    cell_type: str,
    content: str,
    *,
    before_cell_id: str | None = None,
    after_cell_id: str | None = None,
    before_cell: int | None = None,
    after_cell: int | None = None,
):
    if is_notebook_path(path):
        return create_nb_cell(
            path,
            get_manager(),
            cell_type,
            content,
            before_cell_id=before_cell_id,
            after_cell_id=after_cell_id,
            before_cell=before_cell,
            after_cell=after_cell,
        )
    return create_m_cell(
        path,
        cell_type,
        content,
        before_cell_id=before_cell_id,
        after_cell_id=after_cell_id,
        before_cell=before_cell,
        after_cell=after_cell,
    )


def _update_cell_impl(
    path: str,
    *,
    cell_id: str | None = None,
    cell: int | None = None,
    content: str | None = None,
    cell_type: str | None = None,
):
    if is_notebook_path(path):
        return update_nb_cell(
            path,
            get_manager(),
            cell_id=cell_id,
            cell_number=cell,
            content=content,
            cell_type=cell_type,
        )
    return update_m_cell(
        path,
        cell_id=cell_id,
        cell_number=cell,
        content=content,
        cell_type=cell_type,
    )


def _delete_cell_impl(
    path: str,
    *,
    cell_id: str | None = None,
    cell: int | None = None,
):
    if is_notebook_path(path):
        return delete_nb_cell(
            path,
            get_manager(),
            cell_id=cell_id,
            cell_number=cell,
    )
    return delete_m_cell(path, cell_id=cell_id, cell_number=cell)


def _clear_runtime_metadata(path: str, cell_id: str) -> None:
    get_manager().clear_cell_run_info(_normalized_path(path), cell_id)


def _clear_runtime_metadata_ids(path: str, *cell_ids: str | None) -> None:
    for cell_id in {cell_id for cell_id in cell_ids if cell_id}:
        _clear_runtime_metadata(path, cell_id)


def _sync_notebook_outputs(path: str, results: list[dict], session: str) -> list[dict]:
    if not is_notebook_path(path):
        return []
    output_specs = [
        {
            "input_cell_id": result["cell_id"],
            "out_number": result["out_number"],
        }
        for result in results
        if result.get("status") == "ok" and result.get("out_number") is not None
    ]
    if not output_specs:
        return []
    return sync_nb_output_cells(
        path,
        get_manager(),
        output_specs,
        session_name=session,
    )


def _symbol_info_query(name: str, fields: list[str], session_name: str = "main") -> dict:
    field_exprs = {
        "usage": "symbolUsageText[s]",
        "definition": "ToString[Definition[s], InputForm]",
        "attributes": _symbol_list_expr("Attributes[s]"),
        "options": _symbol_list_expr("Options[s]"),
        "context": "Context[s]",
        "own_values": _symbol_list_expr("OwnValues[s]"),
        "down_values": _symbol_list_expr("DownValues[s]"),
        "up_values": _symbol_list_expr("UpValues[s]"),
        "sub_values": _symbol_list_expr("SubValues[s]"),
        "messages": _symbol_list_expr("Messages[s]"),
    }
    selected_exprs = ", ".join(f'"{field}" -> {field_exprs[field]}' for field in fields)
    return _json_eval(
        f"""
        {_TEXT_HELPERS_WL}
        With[{{held = ToExpression[{json.dumps(name)}, InputForm, HoldComplete]}},
            If[
                MatchQ[held, HoldComplete[_Symbol]],
                Replace[
                    held,
                    HoldComplete[s_Symbol] :> ExportString[
                        <|{selected_exprs}|>,
                        "RawJSON"
                    ]
                ],
                ExportString[
                    <|"error" -> "Name did not resolve to a symbol"|>,
                    "RawJSON"
                ]
            ]
        ]
        """,
        session_name=session_name,
    )


def _documentation_search_query(
    query: str,
    *,
    max_results: int,
    session_name: str = "main",
) -> dict:
    normalized_query = query.strip()
    limit = _validate_max_results(max_results)
    tokens = _documentation_tokens(normalized_query)
    if not normalized_query:
        raise ValueError("Query must be non-empty")
    return _json_eval(
        f"""
        {_TEXT_HELPERS_WL}
        Module[
            {{
                query = {json.dumps(normalized_query)},
                queryLower,
                queryTokens = {_wl_string_list(tokens)},
                queryTerms = {{}},
                symbolNames,
                entities,
                results
            }},
            queryLower = ToLowerCase[query];
            queryTerms = DeleteDuplicates[
                Join[
                    queryTokens,
                    Map[
                        ToUpperCase[StringTake[#, 1]] <> StringDrop[#, 1] &,
                        queryTokens
                    ]
                ]
            ];
            symbolNames = DeleteDuplicates[
                Flatten[
                    Map[
                        Names["System`*" <> # <> "*"] &,
                        queryTerms
                    ],
                    1
                ]
            ];
            entities = Map[StringReplace[#, StartOfString ~~ "System`" -> ""] &, symbolNames];
            results = Map[
                Function[name,
                    Module[
                        {{
                            nameLower,
                            entity,
                            usage,
                            usageLower,
                            url,
                            related,
                            matchSources = {{}},
                            exactNameHit,
                            nameHit,
                            usageHit,
                            tokenNameHits,
                            tokenUsageHits,
                            usagePos
                        }},
                        nameLower = ToLowerCase[name];
                        entity = Entity["WolframLanguageSymbol", name];
                        usage = Replace[
                            Quiet[Check[WolframLanguageData[entity, "PlaintextUsage"], ""]],
                            _Missing -> ""
                        ];
                        usage = normalizeDisplayText[usage];
                        usageLower = ToLowerCase[usage];
                        url = Replace[
                            Quiet[Check[WolframLanguageData[entity, "URL"], Missing["NotAvailable"]]],
                            _Missing -> Missing["NotAvailable"]
                        ];
                        related = Replace[
                            Quiet[Check[WolframLanguageData[entity, "RelatedSymbols"], {{}}]],
                            _Missing -> {{}}
                        ];
                        exactNameHit = nameLower === queryLower;
                        nameHit = StringContainsQ[nameLower, queryLower];
                        usageHit = usage =!= "" && StringContainsQ[usageLower, queryLower];
                        tokenNameHits = Count[queryTokens, token_ /; StringContainsQ[nameLower, token]];
                        tokenUsageHits = Count[
                            queryTokens,
                            token_ /; usage =!= "" && StringContainsQ[usageLower, token]
                        ];
                        If[exactNameHit, AppendTo[matchSources, "exact_name"]];
                        If[nameHit, AppendTo[matchSources, "name"]];
                        If[usageHit, AppendTo[matchSources, "usage"]];
                        If[tokenNameHits > 0, AppendTo[matchSources, "name_tokens"]];
                        If[tokenUsageHits > 0, AppendTo[matchSources, "usage_tokens"]];
                        If[
                            !(exactNameHit || nameHit || usageHit || tokenNameHits > 0 || tokenUsageHits > 0),
                            Nothing,
                            usagePos = If[
                                usageHit,
                                First[First[StringPosition[usageLower, queryLower]]],
                                10^9
                            ];
                            <|
                                "symbol" -> name,
                                "usage" -> usage,
                                "url" -> If[url === Missing["NotAvailable"], "", ToString[url, OutputForm]],
                                "match_sources" -> matchSources,
                                "related_symbols" -> Map[
                                    ToString[#, OutputForm] &,
                                    DeleteMissing[
                                        Map[
                                            Quiet[Check[
                                                WolframLanguageData[#, "Name"],
                                                Missing["NotAvailable"]
                                            ]] &,
                                            Take[related, UpTo[5]]
                                        ]
                                    ]
                                ],
                                "rank" -> {{
                                    If[exactNameHit, 0, 1],
                                    -tokenNameHits,
                                    -tokenUsageHits,
                                    If[nameHit, 0, 1],
                                    If[usageHit, 0, 1],
                                    usagePos,
                                    StringLength[name],
                                    name
                                }}
                            |>
                        ]
                    ]
                ],
                entities
            ];
            results = Cases[results, _Association];
            results = Map[
                KeyDrop[#, "rank"] &,
                Take[SortBy[results, Lookup[#, "rank"] &], UpTo[{limit}]]
            ];
            ExportString[
                <|
                    "query" -> query,
                    "result_count" -> Length[results],
                    "results" -> results
                |>,
                "RawJSON"
            ]
        ]
        """,
        session_name=session_name,
    )


def _json_eval(code: str, session_name: str = "main") -> dict | list:
    raw = get_manager().evaluate_raw(code, session_name=session_name)
    return json.loads(raw)


def _evaluate_cell(cell, session: str, timeout: int, path: str | None = None) -> dict:
    if not _is_executable_cell(cell):
        return {
            "cell": cell.number,
            "cell_id": cell.cell_id,
            "type": cell.cell_type,
            "lines": f"{cell.line_start}-{cell.line_end}",
            "status": "skipped",
            "reason": "not_executable",
        }

    result = get_manager().evaluate(cell.content, session_name=session, timeout=timeout)
    runtime = cell.runtime
    if path is not None:
        runtime = get_manager().record_cell_run(
            path,
            cell.cell_id,
            result,
            session_name=session,
        )
        cell.runtime = runtime
    return {
        "cell": cell.number,
        "cell_id": cell.cell_id,
        "type": cell.cell_type,
        "lines": f"{cell.line_start}-{cell.line_end}",
        "status": "ok",
        "in_number": result.in_number,
        "out_number": result.out_number,
        "summary": result.output_summary,
        "head": result.head,
        "byte_size": result.byte_size,
        "leaf_count": result.leaf_count,
        "messages": result.messages,
        "is_truncated": result.is_truncated,
        "last_run_at": runtime.last_run_at,
    }


def _select_cells(
    parsed_cells: list,
    cells: list[int] | None = None,
    start: int | None = None,
    end: int | None = None,
    require_selector: bool = False,
) -> list:
    if cells is not None and (start is not None or end is not None):
        raise ValueError("Use either `cells` or `start`/`end`, not both")

    if cells is not None:
        selected = []
        for cell_number in cells:
            if cell_number < 1 or cell_number > len(parsed_cells):
                raise ValueError(
                    f"Cell {cell_number} out of range (file has {len(parsed_cells)} cells)"
                )
            selected.append(parsed_cells[cell_number - 1])
        return selected

    if start is not None or end is not None:
        lo = 1 if start is None else start
        hi = len(parsed_cells) if end is None else end
        if lo < 1 or hi > len(parsed_cells) or lo > hi:
            raise ValueError(
                f"Invalid cell range {lo}-{hi} (file has {len(parsed_cells)} cells)"
            )
        return parsed_cells[lo - 1 : hi]

    if require_selector:
        raise ValueError("Explicit cell selection required unless `all=True`")

    return parsed_cells


def _select_single_cell(
    parsed_cells: list,
    cell_id: str | None = None,
    cell: int | None = None,
):
    if cell_id is not None and cell is not None:
        raise ValueError("Use either `cell_id` or `cell`, not both")
    if cell_id is None and cell is None:
        raise ValueError("Provide either `cell_id` or `cell`")

    if cell_id is not None:
        for parsed_cell in parsed_cells:
            if parsed_cell.cell_id == cell_id:
                return parsed_cell
        raise ValueError(f"Cell '{cell_id}' not found")

    assert cell is not None
    if cell < 1 or cell > len(parsed_cells):
        raise ValueError(f"Cell {cell} out of range (file has {len(parsed_cells)} cells)")
    return parsed_cells[cell - 1]


def _cell_error(result: dict, fail_on_aborted: bool, fail_on_messages: bool) -> str | None:
    if result["status"] != "ok":
        return None
    if fail_on_aborted and result["head"] == "$Aborted":
        return "Evaluation returned $Aborted"
    if fail_on_messages and result["messages"]:
        return f"Kernel emitted messages: {result['messages'][0]}"
    return None


mcp = FastMCP(
    "mathematica-kernel-mcp",
    instructions="""MCP server for Mathematica/Wolfram Language development with persistent sessions.

## Core workflow
1. Use `file_outline` to inspect `.m`, `.wl`, or `.nb` files.
2. Use `run_cells` with `cells=[...]`, `start`/`end`, or `all=True` to execute cells in order.
3. Use `create_cell`, `update_cell`, `delete_cell`, `create_cells`, or `update_cells` to mutate files at the cell layer.
4. Use `eval` for ad hoc Wolfram Language code.
5. Use `get_output` or `get_cell_output` to inspect stored results.

## Tooling guidance
- Prefer `eval` for one-off operations that do not need a dedicated structured tool.
- Prefer `run_cells` over manual single-cell execution when state matters.
- Use `create_cell`, `update_cell`, `delete_cell`, `create_cells`, and `update_cells` to mutate `.m`, `.wl`, or `.nb` files.
- Use `list_symbols`, `symbol_info`, and `documentation_search` for structured inspection before falling back to ad hoc probes.
- Use `file_outline(include_content=True, cells=[...])` when you need source content for specific cells.
- Use `symbol_info` and `kernel_state` when you need structured metadata rather than raw strings.
""",
    lifespan=lifespan,
)


@mcp.tool()
def eval(code: str, session: str = "main", timeout: int = 30) -> dict:
    """Evaluate Wolfram Language code in a persistent kernel session."""
    result = get_manager().evaluate(code, session_name=session, timeout=timeout)
    return {
        "in_number": result.in_number,
        "out_number": result.out_number,
        "summary": result.output_summary,
        "head": result.head,
        "byte_size": result.byte_size,
        "leaf_count": result.leaf_count,
        "messages": result.messages,
        "is_truncated": result.is_truncated,
    }


@mcp.tool()
def get_output(
    out_number: int,
    session: str = "main",
    view: str = "full",
    max_chars: int = 2000,
    depth: int = 3,
) -> dict:
    """Inspect a previous evaluation result by full text, short preview, or structured summary."""
    if view not in _GET_OUTPUT_VIEWS:
        return {"error": f"Unsupported view '{view}'. Use one of: {sorted(_GET_OUTPUT_VIEWS)}"}

    manager = get_manager()
    if view == "full":
        raw = manager.evaluate_raw(
            f"ToString[wolfram$mcp$out[{out_number}], InputForm]",
            session_name=session,
        )
        is_truncated = len(raw) > max_chars
        if is_truncated:
            raw = raw[:max_chars] + "..."
        return {
            "out_number": out_number,
            "view": view,
            "output": raw,
            "is_truncated": is_truncated,
        }

    if view == "short":
        raw = manager.evaluate_raw(
            f"ToString[Shallow[wolfram$mcp$out[{out_number}], {{{depth}, 3}}], OutputForm]",
            session_name=session,
        )
        is_truncated = len(raw) > max_chars
        if is_truncated:
            raw = raw[:max_chars] + "..."
        return {
            "out_number": out_number,
            "view": view,
            "depth": depth,
            "output": raw,
            "is_truncated": is_truncated,
        }

    summary = _json_eval(
        f"""
        ExportString[
            With[{{res = wolfram$mcp$out[{out_number}]}},
                <|
                    "head" -> ToString[Head[res]],
                    "leaf_count" -> LeafCount[res],
                    "depth" -> Depth[res],
                    "byte_count" -> ByteCount[res],
                    "dimensions" -> Quiet[Check[Dimensions[res], Null]]
                |>
            ],
            "RawJSON"
        ]
        """,
        session_name=session,
    )
    return {"out_number": out_number, "view": view, "summary": summary}


@mcp.tool()
def get_cell_output(
    path: str,
    cell_id: str | None = None,
    cell: int | None = None,
    session: str = "main",
    view: str = "full",
    max_chars: int = 2000,
    depth: int = 3,
) -> dict:
    """Inspect the most recent output produced by a previously run file cell."""
    parsed_cells = _attach_runtime_metadata(path, _parse_cells(path, session=session), session=session)
    try:
        selected_cell = _select_single_cell(parsed_cells, cell_id=cell_id, cell=cell)
    except ValueError as exc:
        return {"error": str(exc)}

    runtime = selected_cell.runtime
    if runtime.last_out is None:
        return {
            "error": f"Cell '{selected_cell.cell_id}' has not been run in session '{session}'",
            "path": path,
            "session": session,
            "cell": selected_cell.number,
            "cell_id": selected_cell.cell_id,
        }

    output = get_output(
        out_number=runtime.last_out,
        session=session,
        view=view,
        max_chars=max_chars,
        depth=depth,
    )
    if "error" in output:
        return output
    return {
        "path": path,
        "session": session,
        "cell": selected_cell.number,
        "cell_id": selected_cell.cell_id,
        "last_in": runtime.last_in,
        "last_out": runtime.last_out,
        "messages": runtime.messages,
        "last_run_at": runtime.last_run_at,
        **output,
    }


@mcp.tool()
def get_output_part(out_number: int, part_spec: str, session: str = "main") -> dict:
    """Extract a specific part of a previous result using Wolfram Language."""
    expr = part_spec.replace("%", f"wolfram$mcp$out[{out_number}]")
    raw = get_manager().evaluate_raw(
        f"ToString[{expr}, InputForm]",
        session_name=session,
    )
    return {"out_number": out_number, "result": raw}


@mcp.tool()
def symbol_info(
    name: str,
    fields: list[str] | None = None,
    session: str = "main",
) -> dict:
    """Return structured information about a symbol."""
    normalized_fields = _validate_symbol_info_fields(fields)
    if isinstance(normalized_fields, str):
        return {"error": normalized_fields}
    info = _symbol_info_query(name, normalized_fields, session_name=session)
    if "error" in info:
        return info
    if isinstance(info.get("usage"), str):
        info["usage"] = _normalize_terminal_text(info["usage"])
    return {"symbol": name, "info": info}


@mcp.tool()
def names(pattern: str, session: str = "main") -> dict:
    """Find symbols matching a pattern."""
    matches = _json_eval(
        f'ExportString[Names[{json.dumps(pattern)}], "RawJSON"]',
        session_name=session,
    )
    return {"pattern": pattern, "matches": matches}


@mcp.tool()
def list_symbols(context: str = "Global`", session: str = "main") -> dict:
    """List symbols for a context or names-pattern query."""
    try:
        normalized_context, pattern = _normalize_context_query(context)
    except ValueError as exc:
        return {"error": str(exc)}
    symbols = _json_eval(
        f'ExportString[Names[{json.dumps(pattern)}], "RawJSON"]',
        session_name=session,
    )
    return {
        "context": normalized_context,
        "pattern": pattern,
        "count": len(symbols),
        "symbols": symbols,
    }


@mcp.tool()
def documentation_search(query: str, max_results: int = 10, session: str = "main") -> dict:
    """Search Wolfram Language symbol documentation by name or usage text."""
    try:
        result = _documentation_search_query(
            query,
            max_results=max_results,
            session_name=session,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    for item in result.get("results", []):
        usage = item.get("usage")
        if isinstance(usage, str):
            item["usage"] = _normalize_terminal_text(usage)
    return result


@mcp.tool()
def file_outline(
    path: str,
    include_content: bool = False,
    executable_only: bool = False,
    cells: list[int] | None = None,
    start: int | None = None,
    end: int | None = None,
    session: str = "main",
) -> dict:
    """Parse a file and return its cell structure."""
    parsed_cells = _attach_runtime_metadata(path, _parse_cells(path, session=session), session=session)
    try:
        selected_cells = _select_cells(parsed_cells, cells=cells, start=start, end=end)
    except ValueError as exc:
        return {"error": str(exc)}
    if executable_only:
        selected_cells = [cell for cell in selected_cells if _is_executable_cell(cell)]
    return {
        "path": path,
        "cell_count": len(selected_cells),
        "cells": [_outline_entry(cell, include_content=include_content) for cell in selected_cells],
    }


@mcp.tool()
def run_cells(
    path: str,
    cells: list[int] | None = None,
    start: int | None = None,
    end: int | None = None,
    all: bool = False,
    session: str = "main",
    timeout: int = 30,
    stop_on_error: bool = True,
    fail_on_aborted: bool = True,
    fail_on_messages: bool = False,
    persist_output: bool = False,
) -> dict:
    """Execute selected cells from a file in order."""
    parsed_cells = _attach_runtime_metadata(path, _parse_cells(path, session=session), session=session)
    try:
        if all:
            if cells is not None or start is not None or end is not None:
                raise ValueError("Use either `all=True` or an explicit selector, not both")
            selected_cells = [cell for cell in parsed_cells if _is_executable_cell(cell)]
        else:
            selected_cells = _select_cells(
                parsed_cells,
                cells=cells,
                start=start,
                end=end,
                require_selector=True,
            )
    except ValueError as exc:
        return {"error": str(exc)}

    results = []
    stopped_early = False
    for cell in selected_cells:
        try:
            cell_result = _evaluate_cell(cell, session=session, timeout=timeout, path=path)
        except Exception as exc:
            cell_result = {
                "cell": cell.number,
                "cell_id": cell.cell_id,
                "type": cell.cell_type,
                "lines": f"{cell.line_start}-{cell.line_end}",
                "status": "error",
                "error": str(exc),
            }
        else:
            semantic_error = _cell_error(
                cell_result,
                fail_on_aborted=fail_on_aborted,
                fail_on_messages=fail_on_messages,
            )
            if semantic_error is not None:
                cell_result = {
                    **cell_result,
                    "status": "error",
                    "error": semantic_error,
                }

        results.append(cell_result)
        if stop_on_error and cell_result["status"] == "error":
            stopped_early = True
            break

    synced_outputs = []
    if persist_output:
        try:
            synced_outputs = _sync_notebook_outputs(path, results, session)
        except Exception as exc:
            return {
                "path": path,
                "session": session,
                "result_count": len(results),
                "stopped_early": stopped_early,
                "results": results,
                "sync_error": str(exc),
            }

    return {
        "path": path,
        "session": session,
        "result_count": len(results),
        "stopped_early": stopped_early,
        "results": results,
        "synced_outputs": synced_outputs,
    }


@mcp.tool()
def run_cell(
    path: str,
    cell_id: str | None = None,
    cell: int | None = None,
    session: str = "main",
    timeout: int = 30,
    fail_on_aborted: bool = True,
    fail_on_messages: bool = False,
    persist_output: bool = False,
) -> dict:
    """Execute a single cell by stable ID or positional number."""
    parsed_cells = _attach_runtime_metadata(path, _parse_cells(path, session=session), session=session)
    try:
        selected_cell = _select_single_cell(parsed_cells, cell_id=cell_id, cell=cell)
    except ValueError as exc:
        return {"error": str(exc)}

    try:
        result = _evaluate_cell(selected_cell, session=session, timeout=timeout, path=path)
    except Exception as exc:
        return {
            "path": path,
            "session": session,
            "cell": selected_cell.number,
            "cell_id": selected_cell.cell_id,
            "status": "error",
            "error": str(exc),
        }

    semantic_error = _cell_error(
        result,
        fail_on_aborted=fail_on_aborted,
        fail_on_messages=fail_on_messages,
    )
    if semantic_error is not None:
        result = {
            **result,
            "status": "error",
            "error": semantic_error,
        }

    synced_outputs = []
    if persist_output:
        try:
            synced_outputs = _sync_notebook_outputs(path, [result], session)
        except Exception as exc:
            return {
                "path": path,
                "session": session,
                **result,
                "sync_error": str(exc),
            }

    return {"path": path, "session": session, **result, "synced_outputs": synced_outputs}


@mcp.tool()
def create_cell(
    path: str,
    cell_type: str,
    content: str,
    before_cell_id: str | None = None,
    after_cell_id: str | None = None,
    before_cell: int | None = None,
    after_cell: int | None = None,
) -> dict:
    """Create a cell in a mutable file."""
    try:
        cell = _create_cell_impl(
            path,
            cell_type,
            content,
            before_cell_id=before_cell_id,
            after_cell_id=after_cell_id,
            before_cell=before_cell,
            after_cell=after_cell,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"path": path, "cell": _outline_entry(cell, include_content=True)}


@mcp.tool()
def create_cells(
    path: str,
    cells: list[dict],
    before_cell_id: str | None = None,
    after_cell_id: str | None = None,
    before_cell: int | None = None,
    after_cell: int | None = None,
) -> dict:
    """Create multiple cells contiguously in a mutable file."""
    if not cells:
        return {"error": "Provide at least one cell spec"}

    created = []
    stopped_early = False
    next_before_cell_id = before_cell_id
    next_after_cell_id = after_cell_id
    next_before_cell = before_cell
    next_after_cell = after_cell

    for index, cell_spec in enumerate(cells, start=1):
        if "cell_type" not in cell_spec or "content" not in cell_spec:
            created.append(
                {
                    "index": index,
                    "status": "error",
                    "error": "Each cell spec must include `cell_type` and `content`",
                }
            )
            stopped_early = True
            break
        try:
            created_cell = _create_cell_impl(
                path,
                str(cell_spec["cell_type"]),
                str(cell_spec["content"]),
                before_cell_id=next_before_cell_id,
                after_cell_id=next_after_cell_id,
                before_cell=next_before_cell,
                after_cell=next_after_cell,
            )
        except ValueError as exc:
            created.append(
                {
                    "index": index,
                    "status": "error",
                    "error": str(exc),
                }
            )
            stopped_early = True
            break

        created.append(
            {
                "index": index,
                "status": "ok",
                "cell": _outline_entry(created_cell, include_content=True),
            }
        )
        next_before_cell_id = None
        next_before_cell = None
        next_after_cell = None
        next_after_cell_id = created_cell.cell_id

    return {
        "path": path,
        "created_count": sum(1 for item in created if item["status"] == "ok"),
        "stopped_early": stopped_early,
        "results": created,
    }


@mcp.tool()
def update_cell(
    path: str,
    cell_id: str | None = None,
    cell: int | None = None,
    content: str | None = None,
    cell_type: str | None = None,
) -> dict:
    """Update a cell in a mutable file."""
    try:
        previous = _select_single_cell(_parse_cells(path), cell_id=cell_id, cell=cell)
        updated = _update_cell_impl(
            path,
            cell_id=cell_id,
            cell=cell,
            content=content,
            cell_type=cell_type,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    _clear_runtime_metadata_ids(path, previous.cell_id, updated.cell_id)
    return {"path": path, "cell": _outline_entry(updated, include_content=True)}


@mcp.tool()
def update_cells(path: str, updates: list[dict]) -> dict:
    """Update multiple cells in a mutable file."""
    if not updates:
        return {"error": "Provide at least one update spec"}

    results = []
    stopped_early = False
    for index, update_spec in enumerate(updates, start=1):
        if "cell_id" not in update_spec and "cell" not in update_spec:
            results.append(
                {
                    "index": index,
                    "status": "error",
                    "error": "Each update spec must include `cell_id` or `cell`",
                }
            )
            stopped_early = True
            break
        try:
            previous = _select_single_cell(
                _parse_cells(path),
                cell_id=update_spec.get("cell_id"),
                cell=update_spec.get("cell"),
            )
            updated = _update_cell_impl(
                path,
                cell_id=update_spec.get("cell_id"),
                cell=update_spec.get("cell"),
                content=update_spec.get("content"),
                cell_type=update_spec.get("cell_type"),
            )
        except ValueError as exc:
            results.append(
                {
                    "index": index,
                    "status": "error",
                    "error": str(exc),
                }
            )
            stopped_early = True
            break

        _clear_runtime_metadata_ids(path, previous.cell_id, updated.cell_id)

        results.append(
            {
                "index": index,
                "status": "ok",
                "cell": _outline_entry(updated, include_content=True),
            }
        )

    return {
        "path": path,
        "updated_count": sum(1 for item in results if item["status"] == "ok"),
        "stopped_early": stopped_early,
        "results": results,
    }


@mcp.tool()
def delete_cell(
    path: str,
    cell_id: str | None = None,
    cell: int | None = None,
) -> dict:
    """Delete a cell from a mutable file."""
    try:
        previous = _select_single_cell(_parse_cells(path), cell_id=cell_id, cell=cell)
        deleted = _delete_cell_impl(path, cell_id=cell_id, cell=cell)
    except ValueError as exc:
        return {"error": str(exc)}
    _clear_runtime_metadata_ids(path, previous.cell_id, deleted.cell_id)
    return {
        "path": path,
        "deleted": _outline_entry(deleted, include_content=True),
        "cell_count": len(_parse_cells(path)),
    }


@mcp.tool()
def kernel_state(session: str = "main", fields: list[str] | None = None) -> dict:
    """Return structured kernel state information."""
    normalized_fields = _validate_kernel_state_fields(fields)
    if isinstance(normalized_fields, str):
        return {"error": normalized_fields}

    selected_exprs = ", ".join(
        f'"{field}" -> {_KERNEL_STATE_FIELDS[field]}' for field in normalized_fields
    )
    state = _json_eval(
        f'ExportString[<|{selected_exprs}|>, "RawJSON"]',
        session_name=session,
    )
    return {"session": session, "state": state}


@mcp.tool()
def kernel_restart(session: str = "main") -> dict:
    """Restart a kernel session."""
    get_manager().restart_session(session)
    return {"restarted": session}


@mcp.tool()
def session_create(name: str) -> dict:
    """Create a new named kernel session."""
    get_manager().create_session(name)
    return {"created": name}


@mcp.tool()
def session_list() -> dict:
    """List all kernel sessions with their status."""
    sessions = get_manager().list_sessions()
    return {
        "sessions": [
            {
                "name": session.name,
                "alive": session.is_alive,
                "busy": session.is_busy,
                "out_count": session.out_count,
            }
            for session in sessions
        ]
    }


@mcp.tool()
def session_close(name: str) -> dict:
    """Close a named session."""
    get_manager().close_session(name)
    return {"closed": name}
