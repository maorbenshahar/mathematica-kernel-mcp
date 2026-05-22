BeginPackage["SharedKernelMCP`"];

StartSharedKernelBridge::usage =
  "StartSharedKernelBridge[] starts an authenticated localhost socket bridge inside the current notebook kernel. Commands are evaluated in this kernel and notebook edits are applied through the front end.";

StopSharedKernelBridge::usage =
  "StopSharedKernelBridge[] stops the active socket listener, removes its registry record, and clears the bridge state.";

SharedKernelBridgeStatus::usage =
  "SharedKernelBridgeStatus[] returns an association describing the active bridge state.";

SharedKernelBridgeRegistryDirectory::usage =
  "SharedKernelBridgeRegistryDirectory[] returns the global directory where active bridge registry records are written.";

SharedKernelBridgeRegistrySnapshot::usage =
  "SharedKernelBridgeRegistrySnapshot[] writes and returns the current bridge registry record.";

InstallSharedKernelMCPAutostart::usage =
  "InstallSharedKernelMCPAutostart[] installs a quiet Kernel/init.m block that starts SharedKernelMCP automatically for front-end notebook kernels.";

UninstallSharedKernelMCPAutostart::usage =
  "UninstallSharedKernelMCPAutostart[] removes the Kernel/init.m autostart block installed by InstallSharedKernelMCPAutostart[].";

BridgeRunCell::usage =
  "BridgeRunCell[path, cellID] evaluates the cell with the given integer CellID in the notebook \
open at `path`, and writes an Output cell directly beneath that input (replacing any prior output \
written by the bridge for the same cell). Returns an association with status, messages, prints, \
duration, and the result in InputForm. BridgeRunCell[path, cellID, evalTimeout] wraps the cell \
evaluation in TimeConstrained[..., evalTimeout]; on time-out, status is \"timeout\".";

BridgeUpdateCell::usage =
  "BridgeUpdateCell[path, cellID, cellType, content] replaces the cell with the given integer \
CellID in the notebook open at `path` with a Cell of the given style and content. Returns an \
association with status and cellID.";

BridgeReadNotebook::usage =
  "BridgeReadNotebook[path] walks the notebook open at `path`, assigns a stable integer CellID to \
any cell that lacks one, and returns an association with each cell's index, cellID, style, and \
plain-InputForm content. BridgeReadNotebook[path, False, previewChars] returns compact previews \
instead of full content. Use the returned cellIDs (not the indices) when calling other bridge \
primitives.";

BridgeInsertCellAfter::usage =
  "BridgeInsertCellAfter[path, cellID, cellType, content] inserts a new cell directly after the \
cell with the given anchor cellID. Returns an association with status and newCellID.";

BridgeInsertCellBefore::usage =
  "BridgeInsertCellBefore[path, cellID, cellType, content] inserts a new cell directly before the \
cell with the given anchor cellID. Returns an association with status and newCellID.";

BridgeDeleteCell::usage =
  "BridgeDeleteCell[path, cellID] deletes the cell with the given cellID and any Output cell \
tagged for that cell. Returns an association with status.";

BridgeSweepStaleOutputs::usage =
  "BridgeSweepStaleOutputs[path] removes Output cells tagged BridgeOutputFor:<n> whose anchor \
cellID `n` no longer exists in the notebook. Returns an association with status and sweptCount.";

Begin["`Private`"];

If[! ValueQ[$BridgeState], $BridgeState = <||>];
If[! ValueQ[$SocketBridgeBuffers], $SocketBridgeBuffers = <||>];
If[
  ! ValueQ[$SharedKernelMCPPacletRoot] ||
    ! StringQ[$SharedKernelMCPPacletRoot] ||
    $SharedKernelMCPPacletRoot === "",
  $SharedKernelMCPPacletRoot = If[
    StringQ[$InputFileName] && $InputFileName =!= "",
    DirectoryName[DirectoryName[DirectoryName[$InputFileName]]],
    ""
  ]
];

Options[StartSharedKernelBridge] = {
  "Notebook" -> Automatic,
  "SocketPort" -> 0,
  "EchoInput" -> True,
  "EchoOutput" -> True,
  "EchoPrint" -> True
};

Options[InstallSharedKernelMCPAutostart] = {
  "PacletDirectory" -> Automatic
};

normalizeNotebook[Automatic] :=
  If[$FrontEnd === Null,
    Missing["NoFrontEnd"],
    Quiet @ Check[EvaluationNotebook[], Missing["NoNotebook"]]
  ];

normalizeNotebook[nb_NotebookObject] := nb;
normalizeNotebook[_] := Missing["InvalidNotebook"];

ensureDirectory[path_String] := Module[{},
  If[! DirectoryQ[path],
    CreateDirectory[path, CreateIntermediateDirectories -> True]
  ];
  path
];

bridgeRegistryDirectoryPath[] := FileNameJoin[{
    $UserBaseDirectory,
    "ApplicationData",
    "SharedKernelMCP",
    "bridges"
  }];

bridgeRegistryDirectory[] := ensureDirectory[bridgeRegistryDirectoryPath[]];

restrictRegistryPermissions[file_String] := Module[{dir = DirectoryName[file]},
  If[$OperatingSystem =!= "Windows",
    Quiet @ Check[RunProcess[{"chmod", "700", dir}], Null];
    Quiet @ Check[RunProcess[{"chmod", "600", file}], Null]
  ];
  file
];

SharedKernelBridgeRegistryDirectory[] := bridgeRegistryDirectory[];

bridgeRegistryFileForNotebook[nb_] := Module[{path, key, digest},
  path = notebookPathString[nb];
  key = If[StringQ[path] && path =!= "", path, "kernel-" <> ToString[$ProcessID]];
  digest = IntegerString[Hash[key, "SHA256"], 16, 64];
  FileNameJoin[{
    bridgeRegistryDirectory[],
    ToString[$ProcessID] <> "-" <> digest <> ".json"
  }]
];

notebookPathString[nb_NotebookObject] := Module[{file},
  file = Quiet @ Check[NotebookFileName[nb], ""];
  If[StringQ[file] && file =!= "", ExpandFileName[file], ""]
];

notebookPathString[_] := "";

bridgeRegistryRecord[state_Association] := Module[
  {host, port, token, notebookPath},
  host = Lookup[state, "SocketHost", ""];
  port = Lookup[state, "SocketPort", Null];
  token = Lookup[state, "SocketToken", ""];
  notebookPath = notebookPathString[Lookup[state, "Notebook", Missing["NoNotebook"]]];
  <|
    "schemaVersion" -> 2,
    "transport" -> "Socket",
    "protocol" -> "jsonl-content-length-v1",
    "host" -> If[StringQ[host], host, ""],
    "port" -> If[IntegerQ[port], port, Null],
    "token" -> If[StringQ[token], token, ""],
    "kernelPID" -> $ProcessID,
    "notebookPath" -> notebookPath,
    "notebooks" -> If[notebookPath === "", {}, {<|"path" -> notebookPath|>}],
    "createdAt" -> Lookup[state, "CreatedAt", DateString[{"ISODate", " ", "Time"}]],
    "lastSeen" -> DateString[{"ISODate", " ", "Time"}]
  |>
];

removeSupersededBridgeRegistryFiles[file_String, state_Association] := Module[
  {record, pid, notebookPath, files},
  record = bridgeRegistryRecord[state];
  pid = Lookup[record, "kernelPID", Missing["NoPID"]];
  notebookPath = Lookup[record, "notebookPath", ""];
  files = DeleteCases[FileNames["*.json", DirectoryName[file]], file];
  Scan[
    Function[otherFile,
      Module[{other = Quiet @ Check[Import[otherFile, "RawJSON"], $Failed]},
        If[
          AssociationQ[other] &&
            Lookup[other, "kernelPID", Missing["NoPID"]] === pid &&
            Lookup[other, "notebookPath", ""] === notebookPath,
          Quiet @ Check[DeleteFile[otherFile], Null]
        ]
      ]
    ],
    files
  ];
  Null
];

writeBridgeRegistryFile[state_Association] := Module[{file},
  file = Lookup[state, "RegistryFile", Automatic];
  If[! StringQ[file], file = bridgeRegistryFileForNotebook[Lookup[state, "Notebook", Missing["NoNotebook"]]]];
  removeSupersededBridgeRegistryFiles[file, state];
  Quiet @ Check[
    Export[file, bridgeRegistryRecord[state], "RawJSON"],
    Return[$Failed]
  ];
  restrictRegistryPermissions[file]
];

refreshBridgeRegistry[] := Module[{file},
  If[
    ! AssociationQ[$BridgeState] ||
      ! MatchQ[Lookup[$BridgeState, "SocketListener", Missing["NoSocketListener"]], _SocketListener] ||
      ! KeyExistsQ[$BridgeState, "SocketToken"],
    Return[$Failed]
  ];
  file = writeBridgeRegistryFile[$BridgeState];
  If[StringQ[file], $BridgeState["RegistryFile"] = file];
  file
];

removeBridgeRegistryFile[file_] := Module[{},
  If[StringQ[file] && FileExistsQ[file],
    Quiet @ Check[DeleteFile[file], Null]
  ];
  Null
];

autostartBeginMarker = "(* BEGIN SharedKernelMCP autostart *)";
autostartEndMarker = "(* END SharedKernelMCP autostart *)";

kernelInitFile[] := FileNameJoin[{$UserBaseDirectory, "Kernel", "init.m"}];

resolveAutostartPacletDirectory[Automatic] := If[
  StringQ[$SharedKernelMCPPacletRoot] && $SharedKernelMCPPacletRoot =!= "",
  $SharedKernelMCPPacletRoot,
  None
];

resolveAutostartPacletDirectory[path_String] := ExpandFileName[path];
resolveAutostartPacletDirectory[None] := None;
resolveAutostartPacletDirectory[_] := None;

stripAutostartBlock[text_String] := FixedPoint[
  Function[s,
    Module[{start, stop},
      start = StringPosition[s, autostartBeginMarker];
      If[start === {},
        s,
        stop = Select[
          StringPosition[s, autostartEndMarker],
          #[[1]] > start[[1, 1]] &
        ];
        If[stop === {},
          s,
          StringTake[s, start[[1, 1]] - 1] <> StringDrop[s, stop[[1, 2]]]
        ]
      ]
    ]
  ],
  text
];

autostartBlock[pacletDir_] := Module[
  {loadLine},
  loadLine = If[StringQ[pacletDir],
    "  Quiet @ Check[PacletDirectoryLoad[" <> ToString[pacletDir, InputForm] <> "], Null];\n",
    ""
  ];
  StringJoin[
    autostartBeginMarker, "\n",
    "Quiet @ Check[\n",
    " Module[{},\n",
    loadLine,
    "  Needs[\"SharedKernelMCP`\"];\n",
    "  If[$FrontEnd =!= Null && OwnValues[$Pre] === {} && ! TrueQ[SharedKernelMCP`Private`$AutostartPreInstalled],\n",
    "    SharedKernelMCP`Private`$AutostartPreInstalled = True;\n",
    "    $Pre = Function[expr,\n",
    "      Quiet @ Check[\n",
    "        Module[{nb, status},\n",
    "          nb = Quiet @ Check[EvaluationNotebook[], $Failed];\n",
    "          If[MatchQ[nb, _NotebookObject],\n",
    "            status = SharedKernelMCP`StartSharedKernelBridge[\"Notebook\" -> nb];\n",
    "            If[AssociationQ[status] && TrueQ[Lookup[status, \"Running\", False]],\n",
    "              SharedKernelMCP`Private`$AutostartBridgeStarted = True;\n",
    "              Unset[$Pre];\n",
    "            ];\n",
    "          ];\n",
    "        ],\n",
    "        Null\n",
    "      ];\n",
    "      expr\n",
    "    ];\n",
    "  ];\n",
    " ],\n",
    " Null\n",
    "];\n",
    autostartEndMarker
  ]
];

stringify[expr_] := ToString[Unevaluated[expr], InputForm, PageWidth -> Infinity];

$ResultInputFormMaxChars = 50000;
$ResultJSONMaxChars = 200000;
$MessagePrintMaxChars = 1000;
$CodeEchoMaxChars = 1000;

truncateBridgeString[text_String, maxChars_Integer:$MessagePrintMaxChars] := Module[{chars},
  chars = StringLength[text];
  If[chars > maxChars,
    StringTake[text, maxChars] <> "... [truncated " <> ToString[chars - maxChars] <> " chars]",
    text
  ]
];

bridgeStringListFields[key_String, values_List] := Module[{texts, chars},
  texts = ToString /@ values;
  chars = StringLength /@ texts;
  <|
    key -> (truncateBridgeString /@ texts),
    key <> "Truncated" -> AnyTrue[chars, # > $MessagePrintMaxChars &],
    key <> "Chars" -> chars
  |>
];

bridgeCodeFields[code_String] := Module[{chars},
  chars = StringLength[code];
  <|
    "code" -> truncateBridgeString[code, $CodeEchoMaxChars],
    "codeTruncated" -> TrueQ[chars > $CodeEchoMaxChars],
    "codeChars" -> chars
  |>
];

resultInputFormFields[result_] := Module[{text, chars, maxChars},
  text = stringify[result];
  chars = StringLength[text];
  maxChars = $ResultInputFormMaxChars;
  <|
    "resultInputForm" -> If[chars > maxChars,
      StringTake[text, maxChars] <> "... [truncated " <> ToString[chars - maxChars] <> " chars]",
      text
    ],
    "resultInputFormTruncated" -> TrueQ[chars > maxChars],
    "resultInputFormChars" -> chars
  |>
];

fullResultJSONRequestedQ[code_String] := StringContainsQ[code, "(*FULLJSON*)"];

resultJSONFields[code_String, result_] := Module[{json, chars, full},
  json = Quiet @ Check[ExportString[result, "RawJSON"], $Failed];
  If[! StringQ[json],
    Return[<|"resultJSON" -> Null, "resultJSONTruncated" -> False|>]
  ];
  chars = StringLength[json];
  full = fullResultJSONRequestedQ[code];
  If[full || chars <= $ResultJSONMaxChars,
    <|
      "resultJSON" -> Quiet @ Check[ImportString[json, "RawJSON"], Null],
      "resultJSONTruncated" -> False,
      "resultJSONChars" -> chars
    |>,
    <|
      "resultJSON" -> Null,
      "resultJSONTruncated" -> True,
      "resultJSONChars" -> chars
    |>
  ]
];

appendNotebookCell[nb_, cell_Cell] := Module[{},
  If[$FrontEnd === Null || ! MatchQ[nb, _NotebookObject],
    Return[Null]
  ];
  SelectionMove[nb, After, Notebook];
  NotebookWrite[nb, cell];
  Null
];

appendInputCell[nb_, held_HoldComplete] := appendNotebookCell[
  nb,
  Cell[
    BoxData @ ToBoxes[held /. HoldComplete[expr_] :> Defer[expr], StandardForm],
    "Input"
  ]
];

appendOutputCell[nb_, expr_] := appendNotebookCell[
  nb,
  Cell[BoxData @ ToBoxes[expr, StandardForm], "Output"]
];

appendTextCell[nb_, text_String, style_String : "Text"] := appendNotebookCell[
  nb,
  Cell[text, style]
];

SetAttributes[withNotebookViewPreserved, HoldRest];

withNotebookViewPreserved[nb_, expr_] := Module[
  {savedSel, savedScroll, result},
  If[! MatchQ[nb, _NotebookObject],
    Return[expr]
  ];
  savedSel = Quiet @ Check[SelectedCells[nb], {}];
  savedScroll = Quiet @ Check[CurrentValue[nb, "WindowScrollPosition"], Null];
  result = expr;
  If[ListQ[savedSel] && Length[savedSel] > 0,
    Quiet @ Check[
      SelectionMove[First[savedSel], All, Cell, AutoScroll -> False],
      Null
    ]
  ];
  If[savedScroll =!= Null,
    Quiet @ Check[CurrentValue[nb, "WindowScrollPosition"] = savedScroll, Null];
    Quiet @ Check[SetOptions[nb, WindowScrollPosition -> savedScroll], Null]
  ];
  result
];

bridgeResultAssociation[id_String, code_String, status_String, result_, messages_List, prints_List, duration_] := Join[
  <|
    "id" -> id,
    "timestamp" -> DateString[{"ISODate", " ", "Time"}],
    "status" -> status
  |>,
  bridgeCodeFields[code],
  resultInputFormFields[result],
  resultJSONFields[code, result],
  bridgeStringListFields["messages", messages],
  bridgeStringListFields["prints", prints],
  <|"durationSeconds" -> N[duration, 6]|>
];

evaluateBridgeCommand[commandId_String, code_String, state_Association] := Module[
  {
    held,
    result = Null,
    status = "ok",
    messages = {},
    prints = {},
    parseMessages = {},
    duration = 0.,
    nb = state["Notebook"],
    started,
    printFunction,
    silent = False,
    evalTimeout = Null,
    timeoutTag
  },

  silent = StringStartsQ[code, "(*SILENT*)"];
  evalTimeout = Replace[
    First[
      StringCases[
        code,
        "(*TIMEOUT:" ~~ n:NumberString ~~ "*)" :> ToExpression[n],
        1
      ],
      Null
    ],
    x_ /; ! (NumericQ[x] && x > 0) -> Null
  ];

  Block[
    {
      $Context = "Global`",
      $ContextPath = DeleteDuplicates @ Join[{"System`", "Global`"}, $ContextPath],
      $MessageList = {}
    },
    held = Quiet @ Check[ToExpression[code, InputForm, HoldComplete], $Failed];
    parseMessages = stringify /@ $MessageList;
  ];

  If[held === $Failed,
    status = "parse_error";
    messages = parseMessages;
    If[!silent,
      appendTextCell[nb, "Bridge parse error in command " <> commandId <> ".", "Message"]
    ];
    Return[bridgeResultAssociation[commandId, code, status, $Failed, messages, prints, 0.]]
  ];

  If[!silent && TrueQ[state["EchoInput"]],
    appendInputCell[nb, held]
  ];

  printFunction[args___] := Module[{printed = SequenceForm[args]},
    AppendTo[prints, stringify[printed]];
    If[!silent && TrueQ[state["EchoPrint"]],
      appendNotebookCell[nb, Cell[BoxData @ ToBoxes[printed, StandardForm], "Print"]]
    ];
    Null
  ];

  started = AbsoluteTime[];
  result = CheckAbort[
    Block[
      {
        $Context = "Global`",
        $ContextPath = DeleteDuplicates @ Join[{"System`", "Global`"}, $ContextPath],
        $MessageList = {},
        $Messages = If[silent, {}, $Messages],
        Print = printFunction
      },
      With[{evaluated = If[
          NumericQ[evalTimeout],
          TimeConstrained[ReleaseHold[held], evalTimeout, timeoutTag],
          ReleaseHold[held]
        ]},
        messages = stringify /@ $MessageList;
        evaluated
      ]
    ],
    status = "aborted";
    $Aborted
  ];
  duration = AbsoluteTime[] - started;
  If[result === timeoutTag,
    status = "timeout";
    result = $Aborted
  ];

  If[!silent && status === "ok" && messages =!= {},
    appendTextCell[nb, StringRiffle[messages, "\n"], "Message"]
  ];

  If[!silent && status === "ok" && TrueQ[state["EchoOutput"]] && result =!= Null,
    appendOutputCell[nb, result]
  ];

  bridgeResultAssociation[commandId, code, status, result, messages, prints, duration]
];

socketBridgeToken[] := StringReplace[CreateUUID[], "-" -> ""];

socketListenerPort[listener_] := Module[{socket, port},
  socket = Quiet @ Check[listener["Socket"], $Failed];
  If[socket === $Failed, Return[$Failed]];
  port = Quiet @ Check[socket["DestinationPort"], $Failed];
  If[IntegerQ[port], Return[port]];
  port = Quiet @ Check[socket["SourcePort"], $Failed];
  If[IntegerQ[port], port, $Failed]
];

socketErrorResponse[id_, status_String, message_String] := <|
  "id" -> ToString[Replace[id, Missing[_] -> "unknown"]],
  "timestamp" -> DateString[{"ISODate", " ", "Time"}],
  "status" -> status,
  "code" -> "",
  "resultInputForm" -> message,
  "resultJSON" -> Null,
  "messages" -> {message},
  "prints" -> {},
  "durationSeconds" -> 0.
|>;

socketWriteResponse[source_, response_String] := Module[
  {bodyBytes, header, packetBytes},
  bodyBytes = StringToByteArray[response, "UTF-8"];
  header = StringJoin[
    "Content-Length: ", ToString[Length[bodyBytes]], "\r\n",
    "Content-Type: application/json; charset=utf-8\r\n",
    "\r\n"
  ];
  packetBytes = StringToByteArray[header <> response, "UTF-8"];
  Quiet @ Check[
    Scan[
      (BinaryWrite[source, ByteArray[#]]; Flush[source]) &,
      Partition[Normal[packetBytes], UpTo[8192]]
    ],
    Null
  ];
  Quiet @ Check[Flush[source], Null];
  Pause[0.1];
  Quiet @ Check[Close[source], Null];
  Null
];

handleSocketBridgeEvent[event_Association] := Module[
  {
    source = Lookup[event, "SourceSocket", $Failed],
    data = Lookup[event, "Data", ""],
    sourceKey,
    buffer,
    parts,
    requestText,
    request,
    state = $BridgeState,
    expectedToken,
    id,
    code,
    result,
    response
  },
  sourceKey = ToString[source, InputForm];
  buffer = Lookup[$SocketBridgeBuffers, sourceKey, ""] <> data;
  If[! StringContainsQ[buffer, "\n"],
    $SocketBridgeBuffers[sourceKey] = buffer;
    Return[Null]
  ];

  parts = StringSplit[buffer, "\n", 2];
  requestText = StringTrim[First[parts]];
  $SocketBridgeBuffers = KeyDrop[$SocketBridgeBuffers, sourceKey];

  request = Quiet @ Check[ImportString[requestText, "RawJSON"], $Failed];
  result = Which[
    ! AssociationQ[request],
      socketErrorResponse["unknown", "bad_request", "Request was not valid JSON."],

    ! AssociationQ[state] || ! KeyExistsQ[state, "SocketToken"],
      socketErrorResponse[Lookup[request, "id", "unknown"], "bridge_not_running", "Socket bridge is not active."],

    True,
      expectedToken = state["SocketToken"];
      id = ToString[Lookup[request, "id", "socket_" <> socketBridgeToken[]]];
      If[Lookup[request, "token", Missing["NoToken"]] =!= expectedToken,
        socketErrorResponse[id, "unauthorized", "Invalid socket bridge token."],
        code = Lookup[request, "code", $Failed];
        If[! StringQ[code],
          socketErrorResponse[id, "bad_request", "Request did not include a string code field."],
          evaluateBridgeCommand[id, code, state]
        ]
      ]
  ];

  If[AssociationQ[state] && KeyExistsQ[state, "SocketToken"],
    Quiet @ Check[refreshBridgeRegistry[], Null]
  ];
  response = Quiet @ Check[ExportString[result, "RawJSON"], "{\"status\":\"encode_error\"}"];
  If[MatchQ[source, _SocketObject],
    socketWriteResponse[source, response]
  ];
  Null
];

startSocketTransport[port_Integer, token_String] := SocketListen[
  port,
  handleSocketBridgeEvent,
  CharacterEncoding -> "UTF8"
];

findNotebookByPath[path_String] := Module[{target, candidates, normalize},
  target = Quiet @ Check[AbsoluteFileName[path], $Failed];
  If[! StringQ[target],
    Return[Missing["FileNotFound"]]
  ];
  normalize[nb_NotebookObject] := Module[{nbPath},
    nbPath = Quiet @ Check[NotebookFileName[nb], $Failed];
    If[StringQ[nbPath], Quiet @ Check[AbsoluteFileName[nbPath], ""], ""]
  ];
  candidates = Notebooks[];
  SelectFirst[candidates, normalize[#] === target &, Missing["NotOpen"]]
];

cellByPosition[nb_NotebookObject, index_Integer] := Module[{cells},
  cells = Cells[nb];
  If[1 <= index <= Length[cells], cells[[index]], Missing["OutOfRange"]]
];

cellInputCode[cellObject_CellObject] := Module[{cellExpr, data, exported},
  cellExpr = NotebookRead[cellObject];
  data = Replace[cellExpr, HoldPattern[Cell[d_, ___]] :> d];
  If[StringQ[data], Return[data]];
  exported = Quiet @ Check[
    FrontEndExecute[ExportPacket[cellExpr, "InputText"]],
    $Failed
  ];
  Which[
    StringQ[exported], exported,
    ListQ[exported] && Length[exported] >= 1 && StringQ[First[exported]], First[exported],
    True, ToString[data, InputForm]
  ]
];

cellStyleOf[cellExpr_] := Replace[
  cellExpr,
  HoldPattern[Cell[_, style_String, ___]] :> style
];

cellIDOf[cell_Cell] := Module[{ids},
  ids = Cases[Rest[List @@ cell], HoldPattern[CellID -> v_Integer] :> v, {1}];
  If[ids === {}, Missing["NoID"], Last[ids]]
];

cellIDOf[_] := Missing["NoID"];

cellWithCellID[cell_Cell, id_Integer] := Module[{parts},
  parts = List @@ cell;
  Apply[
    Cell,
    Join[
      {First[parts]},
      DeleteCases[Rest[parts], HoldPattern[CellID -> _], {1}],
      {CellID -> id}
    ]
  ]
];

cellWithCellID[other_, _Integer] := other;

cellIDsInNotebook[nb_NotebookObject] := DeleteMissing[
  cellIDOf /@ (Quiet @ Check[NotebookRead /@ Cells[nb], {}])
];

nextFreeCellIDFromUsed[used_Association, start_Integer] := Module[
  {candidate = Max[1, start]},
  While[KeyExistsQ[used, candidate], candidate++];
  candidate
];

nextFreeCellID[nb_NotebookObject] := Module[{ids},
  ids = cellIDsInNotebook[nb];
  If[ids === {}, 1, Max[ids] + 1]
];

normalizeCellIDsInNotebook[nb_NotebookObject] := Module[
  {
    currentCells,
    nextId,
    finalCells,
    savedSel,
    savedScroll,
    didWrite = False,
    used = <||>,
    assigned = {},
    remapped = {}
  },
  savedSel = Quiet @ Check[SelectedCells[nb], {}];
  savedScroll = Quiet @ Check[
    CurrentValue[nb, "WindowScrollPosition"],
    Null
  ];
  currentCells = Cells[nb];
  nextId = nextFreeCellID[nb];
  Do[
    Module[{cellObj, cellExpr, existing, newId},
      cellObj = currentCells[[i]];
      cellExpr = NotebookRead[cellObj];
      existing = cellIDOf[cellExpr];
      If[MissingQ[existing] || KeyExistsQ[used, existing],
        newId = nextFreeCellIDFromUsed[used, nextId];
        NotebookWrite[cellObj, cellWithCellID[cellExpr, newId]];
        If[MissingQ[existing],
          AppendTo[assigned, <|"index" -> i, "newCellID" -> newId|>],
          AppendTo[
            remapped,
            <|"index" -> i, "oldCellID" -> existing, "newCellID" -> newId|>
          ]
        ];
        used[newId] = True;
        nextId = newId + 1;
        didWrite = True,
        used[existing] = True
      ]
    ],
    {i, Length[currentCells]}
  ];
  finalCells = Cells[nb];
  If[didWrite,
    If[ListQ[savedSel] && Length[savedSel] > 0,
      Quiet @ Check[
        SelectionMove[First[savedSel], All, Cell, AutoScroll -> False],
        Null
      ]
    ];
    If[savedScroll =!= Null,
      Quiet @ Check[CurrentValue[nb, "WindowScrollPosition"] = savedScroll, Null];
      Quiet @ Check[SetOptions[nb, WindowScrollPosition -> savedScroll], Null]
    ]
  ];
  <|
    "cells" -> MapIndexed[
      Module[{cellExpr, id},
        cellExpr = NotebookRead[#1];
        id = cellIDOf[cellExpr];
        <|"index" -> First[#2], "cellID" -> id, "cellObject" -> #1|>
      ] &,
      finalCells
    ],
    "assigned" -> assigned,
    "remapped" -> remapped
  |>
];

assignCellIDsToNotebook[nb_NotebookObject] := Lookup[
  normalizeCellIDsInNotebook[nb],
  "cells",
  {}
];

findCellObjectsByID[nb_NotebookObject, cellID_Integer] := Select[
  Cells[nb],
  cellIDOf[NotebookRead[#]] === cellID &
];

findCellByID[nb_NotebookObject, cellID_Integer] := Module[{matches},
  matches = findCellObjectsByID[nb, cellID];
  Which[
    Length[matches] === 1, First[matches],
    Length[matches] === 0, Missing["NotFound"],
    True, Missing["DuplicateCellID"]
  ]
];

ambiguousCellIDResponse[cellID_Integer, remapped_: {}] := <|
  "status" -> "cell_id_ambiguous",
  "cellID" -> cellID,
  "remapped" -> remapped,
  "message" -> "CellID was duplicated and has been repaired; re-read the notebook and retry with the current CellID."
|>;

cellIDTargetAfterNormalization[nb_NotebookObject, cellID_Integer] := Module[
  {normalization, remapped, target},
  normalization = normalizeCellIDsInNotebook[nb];
  remapped = Select[
    Lookup[normalization, "remapped", {}],
    Lookup[#, "oldCellID", Missing["NoID"]] === cellID &
  ];
  If[remapped =!= {},
    Return[ambiguousCellIDResponse[cellID, remapped]]
  ];
  target = findCellByID[nb, cellID];
  Which[
    MatchQ[target, _CellObject], target,
    target === Missing["DuplicateCellID"], ambiguousCellIDResponse[cellID],
    True, <|"status" -> "cell_id_not_found", "cellID" -> cellID|>
  ]
];

bridgeOutputTagFor[cellID_Integer] :=
  "BridgeOutputFor:" <> ToString[cellID];

findExistingOutputCell[nb_NotebookObject, tag_String] := SelectFirst[
  Cells[nb],
  Module[{tags},
    tags = Cases[
      NotebookRead[#],
      HoldPattern[CellTags -> v_] :> v,
      Infinity
    ];
    MemberQ[Flatten[{tags}], tag]
  ] &,
  Null
];

BridgeReadNotebook[path_String, includeContent_: True, previewChars_: 80] := Module[
  {nb, normalization, mapping, include = TrueQ[includeContent], maxPreview},
  maxPreview = Replace[previewChars, (n_Integer /; n > 0) :> n, {0}];
  If[! IntegerQ[maxPreview], maxPreview = 80];
  nb = findNotebookByPath[path];
  If[! MatchQ[nb, _NotebookObject],
    Return[<|"status" -> "notebook_not_open", "path" -> path|>]
  ];
  withNotebookViewPreserved[nb,
    normalization = normalizeCellIDsInNotebook[nb];
    mapping = Lookup[normalization, "cells", {}];
    <|
      "status" -> "ok",
      "path" -> path,
      "cellIDAssigned" -> Lookup[normalization, "assigned", {}],
      "cellIDRemapped" -> Lookup[normalization, "remapped", {}],
      "cells" -> Map[
        Module[{cellObj, cellExpr, style, content},
          cellObj = #["cellObject"];
          cellExpr = NotebookRead[cellObj];
          style = Replace[cellStyleOf[cellExpr], Except[_String] -> "Cell"];
          content = cellInputCode[cellObj];
          Join[
            <|
              "index" -> #["index"],
              "cellID" -> #["cellID"],
              "style" -> style
            |>,
            If[include,
              <|"content" -> content|>,
              <|"preview" -> StringTake[StringReplace[content, WhitespaceCharacter.. -> " "], UpTo[maxPreview]]|>
            ]
          ]
        ] &,
        mapping
      ]
    |>
  ]
];

BridgeRunCell[path_String, cellID_Integer, evalTimeout_:Null] := Module[
  {
    nb,
    target,
    content,
    parseHeld,
    parseMessages = {},
    prints = {},
    messages = {},
    status = "ok",
    started,
    duration,
    result = Null,
    outputTag,
    outputCell,
    existingOutput,
    printFunction,
    lineNum,
    timeoutTag,
    effectiveTimeout
  },
  effectiveTimeout = If[NumericQ[evalTimeout] && evalTimeout > 0, evalTimeout, Null];

  nb = findNotebookByPath[path];
  If[! MatchQ[nb, _NotebookObject],
    Return[<|"status" -> "notebook_not_open", "path" -> path|>]
  ];

  target = cellIDTargetAfterNormalization[nb, cellID];
  If[! MatchQ[target, _CellObject], Return[target]];

  content = cellInputCode[target];

  Block[{$MessageList = {}},
    parseHeld = Quiet @ Check[
      ToExpression[content, InputForm, HoldComplete],
      $Failed
    ];
    parseMessages = stringify /@ $MessageList;
  ];
  If[parseHeld === $Failed,
    Return[<|
      "status" -> "parse_error",
      "messages" -> parseMessages,
      "content" -> content
    |>]
  ];

  printFunction[args___] := (
    AppendTo[prints, stringify[SequenceForm[args]]];
    Null
  );

  started = AbsoluteTime[];
  $Line++;
  lineNum = $Line;
  result = CheckAbort[
    Block[
      {
        $Context = "Global`",
        $ContextPath = DeleteDuplicates @ Join[{"System`", "Global`"}, $ContextPath],
        $MessageList = {},
        Print = printFunction
      },
      With[{evaluated = If[
          NumericQ[effectiveTimeout],
          TimeConstrained[ReleaseHold[parseHeld], effectiveTimeout, timeoutTag],
          ReleaseHold[parseHeld]
        ]},
        messages = stringify /@ $MessageList;
        evaluated
      ]
    ],
    status = "aborted"; $Aborted
  ];
  duration = AbsoluteTime[] - started;
  If[result === timeoutTag,
    status = "timeout";
    result = $Aborted
  ];

  (* Persist Out[lineNum] so the kernel's history mirrors a main-loop eval, *)
  (* and label both input + output cells so the front end shows In[N]:= / Out[N]= *)
  Quiet @ Check[Out[lineNum] = result, Null];
  (* CellLabelAutoDelete -> False keeps the label text from being cleared. *)
  (* CellLabelStyle -> "CellLabel" (without "CellLabelExpired") keeps the  *)
  (* label rendered as a fresh / just-evaluated label, not a stale one.    *)
  withNotebookViewPreserved[nb,
    Quiet @ Check[
      SetOptions[target,
        CellLabel -> "In[" <> ToString[lineNum] <> "]:= ",
        CellLabelAutoDelete -> False,
        CellLabelStyle -> "CellLabel"
      ],
      Null
    ];

    outputTag = bridgeOutputTagFor[cellID];
    existingOutput = findExistingOutputCell[nb, outputTag];

    If[status === "ok" && result === Null,
      If[existingOutput =!= Null, NotebookDelete[existingOutput]],
      outputCell = Cell[
        BoxData @ ToBoxes[result, StandardForm],
        "Output",
        CellLabel -> "Out[" <> ToString[lineNum] <> "]= ",
        CellLabelAutoDelete -> False,
        CellLabelStyle -> "CellLabel",
        CellTags -> {outputTag}
      ];
      If[existingOutput =!= Null,
        NotebookWrite[existingOutput, outputCell],
        SelectionMove[target, After, Cell, AutoScroll -> False];
        NotebookWrite[nb, outputCell]
      ]
    ]
  ];

  Join[
    <|
      "status" -> status,
      "cellID" -> cellID,
      "inNumber" -> lineNum,
      "outNumber" -> lineNum
    |>,
    resultInputFormFields[result],
    bridgeStringListFields["messages", messages],
    bridgeStringListFields["prints", prints],
    <|"durationSeconds" -> N[duration, 6]|>
  ]
];

BridgeUpdateCell[path_String, cellID_Integer, cellType_String, content_String] := Module[
  {nb, target, replacement},
  nb = findNotebookByPath[path];
  If[! MatchQ[nb, _NotebookObject],
    Return[<|"status" -> "notebook_not_open", "path" -> path|>]
  ];
  target = cellIDTargetAfterNormalization[nb, cellID];
  If[! MatchQ[target, _CellObject], Return[target]];
  replacement = Cell[content, cellType, CellID -> cellID];
  withNotebookViewPreserved[nb, NotebookWrite[target, replacement]];
  <|
    "status" -> "ok",
    "cellID" -> cellID,
    "cellType" -> cellType
  |>
];

bridgeInsertCellAt[
  path_String, cellID_Integer, cellType_String, content_String, position_Symbol
] := Module[
  {nb, anchor, newId, newCell},
  nb = findNotebookByPath[path];
  If[! MatchQ[nb, _NotebookObject],
    Return[<|"status" -> "notebook_not_open", "path" -> path|>]
  ];
  anchor = cellIDTargetAfterNormalization[nb, cellID];
  If[! MatchQ[anchor, _CellObject], Return[anchor]];
  newId = nextFreeCellID[nb];
  newCell = Cell[content, cellType, CellID -> newId];
  withNotebookViewPreserved[nb,
    SelectionMove[anchor, position, Cell, AutoScroll -> False];
    NotebookWrite[nb, newCell]
  ];
  <|
    "status" -> "ok",
    "newCellID" -> newId,
    "anchorCellID" -> cellID,
    "position" -> ToString[position]
  |>
];

BridgeInsertCellAfter[path_String, cellID_Integer, cellType_String, content_String] :=
  bridgeInsertCellAt[path, cellID, cellType, content, After];

BridgeInsertCellBefore[path_String, cellID_Integer, cellType_String, content_String] :=
  bridgeInsertCellAt[path, cellID, cellType, content, Before];

BridgeDeleteCell[path_String, cellID_Integer] := Module[
  {nb, target, outputTag, existingOutput, deletedOutput = False},
  nb = findNotebookByPath[path];
  If[! MatchQ[nb, _NotebookObject],
    Return[<|"status" -> "notebook_not_open", "path" -> path|>]
  ];
  target = cellIDTargetAfterNormalization[nb, cellID];
  If[! MatchQ[target, _CellObject], Return[target]];
  outputTag = bridgeOutputTagFor[cellID];
  existingOutput = findExistingOutputCell[nb, outputTag];
  withNotebookViewPreserved[nb,
    If[existingOutput =!= Null,
      NotebookDelete[existingOutput];
      deletedOutput = True
    ];
    NotebookDelete[target]
  ];
  <|
    "status" -> "ok",
    "deletedCellID" -> cellID,
    "deletedTaggedOutput" -> deletedOutput
  |>
];

bridgeOutputTagPrefix = "BridgeOutputFor:";

parseBridgeOutputTag[tag_String] := If[
  StringStartsQ[tag, bridgeOutputTagPrefix],
  Quiet @ Check[
    ToExpression[StringDrop[tag, StringLength[bridgeOutputTagPrefix]]],
    $Failed
  ],
  $Failed
];

BridgeSweepStaleOutputs[path_String] := Module[
  {nb, allCells, allCellIDs, swept = {}},
  nb = findNotebookByPath[path];
  If[! MatchQ[nb, _NotebookObject],
    Return[<|"status" -> "notebook_not_open", "path" -> path|>]
  ];
  allCells = Cells[nb];
  allCellIDs = DeleteMissing[cellIDOf /@ (NotebookRead /@ allCells)];
  Do[
    Module[{cellExpr, tags, bridgeTags, parents, hasValidParent, doomed},
      cellExpr = NotebookRead[cellObj];
      tags = Flatten[Cases[cellExpr, HoldPattern[CellTags -> v_] :> v, Infinity]];
      bridgeTags = Cases[tags, s_String /; StringStartsQ[s, bridgeOutputTagPrefix]];
      parents = parseBridgeOutputTag /@ bridgeTags;
      hasValidParent = AnyTrue[parents, IntegerQ[#] && MemberQ[allCellIDs, #] &];
      doomed = bridgeTags =!= {} && ! hasValidParent;
      If[doomed,
        NotebookDelete[cellObj];
        AppendTo[swept, <|"tags" -> bridgeTags|>]
      ]
    ],
    {cellObj, allCells}
  ];
  <|
    "status" -> "ok",
    "sweptCount" -> Length[swept],
    "swept" -> swept
  |>
];

SharedKernelBridgeRegistrySnapshot[] := Module[{file, record},
  file = refreshBridgeRegistry[];
  If[! StringQ[file],
    Return[<|"status" -> "not_running"|>]
  ];
  record = bridgeRegistryRecord[$BridgeState];
  Join[
    record,
    <|
      "status" -> "ok",
      "registryDirectory" -> bridgeRegistryDirectory[],
      "registryFile" -> file
    |>
  ]
];

InstallSharedKernelMCPAutostart[OptionsPattern[]] := Module[
  {pacletDir, file, existing, cleaned, block},
  pacletDir = resolveAutostartPacletDirectory[OptionValue["PacletDirectory"]];
  file = kernelInitFile[];
  ensureDirectory[DirectoryName[file]];
  existing = If[FileExistsQ[file], Quiet @ Check[Import[file, "Text"], ""], ""];
  cleaned = stripAutostartBlock[existing];
  block = autostartBlock[pacletDir];
  Quiet @ Check[
    Export[
      file,
      If[StringTrim[cleaned] === "",
        block <> "
",
        StringTrim[cleaned] <> "

" <> block <> "
"
      ],
      "Text"
    ],
    Return[<|"status" -> "error", "message" -> "Could not write Kernel/init.m."|>]
  ];
  <|
    "status" -> "ok",
    "file" -> file,
    "pacletDirectory" -> pacletDir
  |>
];

UninstallSharedKernelMCPAutostart[] := Module[{file, existing, cleaned},
  file = kernelInitFile[];
  If[! FileExistsQ[file],
    Return[<|"status" -> "not_installed", "file" -> file|>]
  ];
  existing = Quiet @ Check[Import[file, "Text"], ""];
  cleaned = stripAutostartBlock[existing];
  If[cleaned === existing,
    Return[<|"status" -> "not_installed", "file" -> file|>]
  ];
  Quiet @ Check[
    Export[
      file,
      If[StringTrim[cleaned] === "", "", StringTrim[cleaned] <> "\n"],
      "Text"
    ],
    Return[<|"status" -> "error", "message" -> "Could not write Kernel/init.m."|>]
  ];
  <|"status" -> "ok", "file" -> file|>
];

StopSharedKernelBridge[] := Module[
  {listener = Lookup[$BridgeState, "SocketListener", Missing["NoSocketListener"]],
   registryFile = Lookup[$BridgeState, "RegistryFile", Automatic]},
  If[MatchQ[listener, _SocketListener],
    Quiet @ Check[DeleteObject[listener], Null]
  ];
  removeBridgeRegistryFile[registryFile];
  $BridgeState = <||>;
  Null
];

StartSharedKernelBridge[OptionsPattern[]] := Module[
  {
    nb,
    status,
    listener = Missing["NoSocketListener"],
    token,
    port
  },
  StopSharedKernelBridge[];

  nb = normalizeNotebook[OptionValue["Notebook"]];
  token = socketBridgeToken[];
  listener = Quiet @ Check[
    startSocketTransport[OptionValue["SocketPort"], token],
    Missing["SocketListenFailed"]
  ];

  If[! MatchQ[listener, _SocketListener],
    $BridgeState = <||>;
    Return[<|
      "Running" -> False,
      "status" -> "socket_unavailable",
      "message" -> "Could not start SocketListen transport."
    |>]
  ];

  port = socketListenerPort[listener];
  If[! IntegerQ[port],
    Quiet @ Check[DeleteObject[listener], Null];
    $BridgeState = <||>;
    Return[<|
      "Running" -> False,
      "status" -> "socket_port_unavailable",
      "message" -> "Could not determine SocketListen port."
    |>]
  ];

  $BridgeState = <|
    "Notebook" -> nb,
    "Transport" -> "Socket",
    "SocketListener" -> listener,
    "SocketToken" -> token,
    "SocketHost" -> "127.0.0.1",
    "SocketPort" -> port,
    "EchoInput" -> TrueQ[OptionValue["EchoInput"]],
    "EchoOutput" -> TrueQ[OptionValue["EchoOutput"]],
    "EchoPrint" -> TrueQ[OptionValue["EchoPrint"]],
    "KernelPID" -> $ProcessID,
    "CreatedAt" -> DateString[{"ISODate", " ", "Time"}]
  |>;

  status = SharedKernelBridgeStatus[];
  status
];

SharedKernelBridgeStatus[] := Module[{registryFile},
  registryFile = refreshBridgeRegistry[];
  <|
    "Running" -> MatchQ[Lookup[$BridgeState, "SocketListener", Missing["NoSocketListener"]], _SocketListener],
    "Transport" -> Lookup[$BridgeState, "Transport", Missing["NoTransport"]],
    "NotebookAttached" -> MatchQ[Lookup[$BridgeState, "Notebook", Missing["NoNotebook"]], _NotebookObject],
    "RegistryDirectory" -> bridgeRegistryDirectoryPath[],
    "RegistryFile" -> registryFile,
    "SocketHost" -> Lookup[$BridgeState, "SocketHost", Missing["NoSocketHost"]],
    "SocketPort" -> Lookup[$BridgeState, "SocketPort", Missing["NoSocketPort"]],
    "KernelPID" -> Lookup[$BridgeState, "KernelPID", $ProcessID]
  |>
];

End[];

EndPackage[];
