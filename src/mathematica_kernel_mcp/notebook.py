"""Kernel-backed helpers for parsing and mutating Mathematica notebooks."""

import json
from pathlib import Path
from textwrap import dedent

from mathematica_kernel_mcp.models import Cell
from mathematica_kernel_mcp.session import SessionManager

NOTEBOOK_EXTENSION = ".nb"

_NOTEBOOK_WL_HELPERS = dedent(
    """
    cellTypeOf[cell_Cell] := Replace[
        cell,
        HoldPattern[Cell[_, cellType_String, ___]] :> cellType
    ];

    cellIdOf[cell_Cell, index_Integer] := Module[{cellId, uuid},
        cellId = Cases[
            cell,
            HoldPattern[CellID -> value_] :> value,
            Infinity
        ];
        If[
            cellId =!= {},
            "CellID:" <> ToString[First[cellId], InputForm],
            uuid = Cases[
                cell,
                HoldPattern[ExpressionUUID -> value_String] :> value,
                Infinity
            ];
            If[uuid =!= {}, First[uuid], "Index:" <> ToString[index]]
        ]
    ];

    cellContentOf[cell_Cell] := Module[{data, held},
        data = Replace[cell, HoldPattern[Cell[cellData_, ___]] :> cellData];
        Which[
            StringQ[data],
            data,
            MatchQ[data, BoxData[_]],
            held = Quiet[Check[ToExpression[data, StandardForm, HoldComplete], $Failed]];
            If[
                MatchQ[held, HoldComplete[_]],
                Replace[held, HoldComplete[expr_] :> ToString[Unevaluated[expr], InputForm]],
                ToString[data, InputForm]
            ],
            MatchQ[data, TextData[_]],
            StringJoin[Cases[data, _String, Infinity]],
            True,
            ToString[data, InputForm]
        ]
    ];

    serializeCells[notebook_] := Module[{positions},
        positions = Position[notebook, _Cell, Infinity, Heads -> False];
        MapIndexed[
            With[{cell = Extract[notebook, #1], index = #2[[1]]},
                <|
                    "number" -> index,
                    "cell_id" -> cellIdOf[cell, index],
                    "cell_type" -> cellTypeOf[cell],
                    "content" -> cellContentOf[cell]
                |>
            ] &,
            positions
        ]
    ];

    nextNotebookCellId[notebook_] := Module[{usedIds},
        usedIds = Cases[
            notebook,
            HoldPattern[CellID -> value_Integer] :> value,
            Infinity
        ];
        If[usedIds === {}, 1, Max[usedIds] + 1]
    ];

    ensurePersistentIds[cell_Cell, nextId_] := Module[{hasCellId},
        hasCellId = !FreeQ[cell, HoldPattern[CellID -> _], Infinity];
        If[hasCellId, cell, Append[cell, CellID -> nextId]]
    ];

    normalizeNotebookIds[notebook_] := Module[{nextId},
        nextId = nextNotebookCellId[notebook];
        Replace[
            notebook,
            cell_Cell /; FreeQ[cell, HoldPattern[CellID -> _], Infinity] :> With[
                {cellId = nextId++},
                ensurePersistentIds[cell, cellId]
            ],
            Infinity
        ]
    ];

    resolveCellIndex[notebook_, cellId_, cellNumber_] := Module[{serialized, match},
        serialized = serializeCells[notebook];
        If[
            cellNumber =!= Null,
            If[1 <= cellNumber <= Length[serialized], cellNumber, $Failed],
            match = SelectFirst[
                serialized,
                Lookup[#, "cell_id"] === cellId &,
                Missing["NotFound"]
            ];
            If[AssociationQ[match], Lookup[match, "number"], $Failed]
        ]
    ];

    cellPosition[notebook_, index_Integer] := Position[
        notebook,
        _Cell,
        Infinity,
        Heads -> False
    ][[index]];

    insertCellAtPosition[notebook_, pos_List, newCell_] := Module[
        {containerPos, insertIndex, container},
        containerPos = Most[pos];
        insertIndex = Last[pos];
        container = Extract[notebook, containerPos];
        ReplacePart[notebook, containerPos -> Insert[container, newCell, insertIndex]]
    ];

    insertCellAfterPosition[notebook_, pos_List, newCell_] := Module[
        {containerPos, insertIndex, container},
        containerPos = Most[pos];
        insertIndex = Last[pos] + 1;
        container = Extract[notebook, containerPos];
        ReplacePart[notebook, containerPos -> Insert[container, newCell, insertIndex]]
    ];

    buildCell[content_String, cellType_String, cellId_Integer] := Cell[
        content,
        cellType,
        CellID -> cellId
    ];

    updateCellExpr[cell_Cell, content_, cellType_] := Module[{args, nextType, nextContent},
        args = List @@ cell;
        nextType = If[cellType === Null, cellTypeOf[cell], cellType];
        nextContent = If[content === Null, cellContentOf[cell], content];
        If[
            Length[args] < 2,
            Cell[nextContent, nextType],
            args[[1]] = nextContent;
            args[[2]] = nextType;
            Apply[Cell, args]
        ]
    ];

    """
)


def is_notebook_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() == NOTEBOOK_EXTENSION


def _wl_string(value: str) -> str:
    return json.dumps(value)


def _wl_optional_string(value: str | None) -> str:
    return "Null" if value is None else json.dumps(value)


def _wl_optional_int(value: int | None) -> str:
    return "Null" if value is None else str(value)


def _load_json(manager: SessionManager, code: str, session_name: str = "main") -> dict | list:
    raw = manager.evaluate_raw(code, session_name=session_name)
    return json.loads(raw)


def _build_module(path: str | Path, body: str) -> str:
    normalized_path = str(Path(path).resolve())
    return dedent(
        f"""
        Block[{{$ContextPath = {{"System`"}}}},
            Module[{{path = {_wl_string(normalized_path)}}},
                {_NOTEBOOK_WL_HELPERS}
                {body}
            ]
        ]
        """
    )


def _cells_from_payload(payload: list[dict]) -> list[Cell]:
    return [
        Cell(
            number=int(item["number"]),
            cell_id=str(item["cell_id"]),
            cell_type=str(item["cell_type"]),
            content=str(item["content"]),
            line_start=int(item["number"]),
            line_end=int(item["number"]),
        )
        for item in payload
    ]


def parse_nb_file_with_kernel(
    path: str | Path,
    manager: SessionManager,
    session_name: str = "main",
) -> list[Cell]:
    """Parse a notebook through the Wolfram kernel."""
    payload = _load_json(
        manager,
        _build_module(
            path,
            """
            ExportString[
                serializeCells[Import[path, "NB"]],
                "RawJSON"
            ]
            """,
        ),
        session_name=session_name,
    )
    return _cells_from_payload(payload)


def create_nb_cell(
    path: str | Path,
    manager: SessionManager,
    cell_type: str,
    content: str,
    *,
    before_cell_id: str | None = None,
    after_cell_id: str | None = None,
    before_cell: int | None = None,
    after_cell: int | None = None,
    session_name: str = "main",
) -> Cell:
    """Create a cell in a notebook and return the new persisted cell."""
    payload = _load_json(
        manager,
        _build_module(
            path,
            f"""
            Module[
                {{
                    selectorCount,
                    rawNotebook,
                    notebook,
                    index = Null,
                    insertPos,
                    newCell,
                    newCellId,
                    created,
                    nextId
                }},
                selectorCount = Count[
                    {{
                        {_wl_optional_string(before_cell_id)},
                        {_wl_optional_string(after_cell_id)},
                        {_wl_optional_int(before_cell)},
                        {_wl_optional_int(after_cell)}
                    }},
                    Except[Null]
                ];
                If[
                    selectorCount > 1,
                    ExportString[
                        <|"error" -> "Use at most one insertion selector"|>,
                        "RawJSON"
                    ],
                    rawNotebook = If[FileExistsQ[path], Import[path, "NB"], Notebook[{{}}]];
                    If[
                        {_wl_optional_string(before_cell_id)} =!= Null ||
                        {_wl_optional_int(before_cell)} =!= Null,
                        index = resolveCellIndex[
                            rawNotebook,
                            {_wl_optional_string(before_cell_id)},
                            {_wl_optional_int(before_cell)}
                        ];
                    ];
                    If[
                        {_wl_optional_string(after_cell_id)} =!= Null ||
                        {_wl_optional_int(after_cell)} =!= Null,
                        index = resolveCellIndex[
                            rawNotebook,
                            {_wl_optional_string(after_cell_id)},
                            {_wl_optional_int(after_cell)}
                        ];
                    ];
                    If[
                        selectorCount == 1 && index === $Failed,
                        ExportString[
                            <|"error" -> "Target cell not found"|>,
                            "RawJSON"
                        ],
                        notebook = normalizeNotebookIds[rawNotebook];
                        nextId = nextNotebookCellId[notebook];
                        newCell = buildCell[
                            {_wl_string(content)},
                            {_wl_string(cell_type)},
                            nextId
                        ];
                        newCellId = cellIdOf[newCell, 0];
                        If[
                            {_wl_optional_string(before_cell_id)} =!= Null ||
                            {_wl_optional_int(before_cell)} =!= Null,
                            insertPos = cellPosition[notebook, index];
                            notebook = insertCellAtPosition[notebook, insertPos, newCell],
                            If[
                                {_wl_optional_string(after_cell_id)} =!= Null ||
                                {_wl_optional_int(after_cell)} =!= Null,
                                insertPos = cellPosition[notebook, index];
                                notebook = insertCellAfterPosition[notebook, insertPos, newCell],
                                notebook = ReplacePart[
                                    notebook,
                                    1 -> Append[notebook[[1]], newCell]
                                ]
                            ]
                        ];
                        Export[path, notebook, "NB"];
                        created = SelectFirst[
                            serializeCells[notebook],
                            Lookup[#, "cell_id"] === newCellId &
                        ];
                        ExportString[created, "RawJSON"]
                    ]
                ]
            ]
            """,
        ),
        session_name=session_name,
    )
    if isinstance(payload, dict) and "error" in payload:
        raise ValueError(str(payload["error"]))
    return _cells_from_payload([payload])[0]


def update_nb_cell(
    path: str | Path,
    manager: SessionManager,
    *,
    cell_id: str | None = None,
    cell_number: int | None = None,
    content: str | None = None,
    cell_type: str | None = None,
    session_name: str = "main",
) -> Cell:
    """Update a notebook cell and return the persisted cell."""
    if content is None and cell_type is None:
        raise ValueError("Provide `content`, `cell_type`, or both")

    payload = _load_json(
        manager,
        _build_module(
            path,
            f"""
            Module[
                {{rawNotebook, notebook, index, pos, existing, updated, updatedId}},
                rawNotebook = Import[path, "NB"];
                index = resolveCellIndex[
                    rawNotebook,
                    {_wl_optional_string(cell_id)},
                    {_wl_optional_int(cell_number)}
                ];
                If[
                    index === $Failed,
                    ExportString[
                        <|"error" -> "Target cell not found"|>,
                        "RawJSON"
                    ],
                    notebook = normalizeNotebookIds[rawNotebook];
                    pos = cellPosition[notebook, index];
                    existing = Extract[notebook, pos];
                    updated = updateCellExpr[
                        existing,
                        {_wl_optional_string(content)},
                        {_wl_optional_string(cell_type)}
                    ];
                    updatedId = cellIdOf[updated, index];
                    notebook = ReplacePart[notebook, pos -> updated];
                    Export[path, notebook, "NB"];
                    ExportString[
                        SelectFirst[
                            serializeCells[notebook],
                            Lookup[#, "cell_id"] === updatedId &
                        ],
                        "RawJSON"
                    ]
                ]
            ]
            """,
        ),
        session_name=session_name,
    )
    if isinstance(payload, dict) and "error" in payload:
        raise ValueError(str(payload["error"]))
    return _cells_from_payload([payload])[0]


def delete_nb_cell(
    path: str | Path,
    manager: SessionManager,
    *,
    cell_id: str | None = None,
    cell_number: int | None = None,
    session_name: str = "main",
) -> Cell:
    """Delete a notebook cell and return the deleted cell metadata."""
    payload = _load_json(
        manager,
        _build_module(
            path,
            f"""
            Module[
                {{rawNotebook, notebook, index, pos, deleted}},
                rawNotebook = Import[path, "NB"];
                index = resolveCellIndex[
                    rawNotebook,
                    {_wl_optional_string(cell_id)},
                    {_wl_optional_int(cell_number)}
                ];
                If[
                    index === $Failed,
                    ExportString[
                        <|"error" -> "Target cell not found"|>,
                        "RawJSON"
                    ],
                    deleted = serializeCells[rawNotebook][[index]];
                    notebook = normalizeNotebookIds[rawNotebook];
                    pos = cellPosition[notebook, index];
                    notebook = Delete[notebook, pos];
                    Export[path, notebook, "NB"];
                    ExportString[deleted, "RawJSON"]
                ]
            ]
            """,
        ),
        session_name=session_name,
    )
    if isinstance(payload, dict) and "error" in payload:
        raise ValueError(str(payload["error"]))
    return _cells_from_payload([payload])[0]
