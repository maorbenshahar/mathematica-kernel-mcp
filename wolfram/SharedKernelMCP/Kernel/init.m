BeginPackage["SharedKernelMCP`"];

StartSharedKernelBridge::usage =
  "StartSharedKernelBridge[] starts a queue-driven bridge inside the current notebook kernel. \
Commands dropped into the queue directory are evaluated in this kernel, formatted cells are appended \
to the notebook, and JSON result files are written back out.";

StopSharedKernelBridge::usage =
  "StopSharedKernelBridge[] stops the scheduled polling task and clears the bridge state.";

SharedKernelBridgeStatus::usage =
  "SharedKernelBridgeStatus[] returns an association describing the active bridge state.";

ProcessSharedKernelBridgeQueue::usage =
  "ProcessSharedKernelBridgeQueue[] processes queued command files immediately.";

ExportNotebookSnapshot::usage =
  "ExportNotebookSnapshot[path] writes a readable Markdown snapshot of the notebook cells, using \
the front end when available to render cell contents as plain text.";

BridgeRunCell::usage =
  "BridgeRunCell[path, cellID] evaluates the cell with the given integer CellID in the notebook \
open at `path`, and writes an Output cell directly beneath that input (replacing any prior output \
written by the bridge for the same cell). Returns an association with status, messages, prints, \
duration, and the result in InputForm.";

BridgeUpdateCell::usage =
  "BridgeUpdateCell[path, cellID, cellType, content] replaces the cell with the given integer \
CellID in the notebook open at `path` with a Cell of the given style and content. Returns an \
association with status and cellID.";

BridgeReadNotebook::usage =
  "BridgeReadNotebook[path] walks the notebook open at `path`, assigns a stable integer CellID to \
any cell that lacks one, and returns an association with each cell's index, cellID, style, and \
plain-InputForm content. Use the returned cellIDs (not the indices) when calling other bridge \
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

Options[StartSharedKernelBridge] = {
  "RootDirectory" -> Automatic,
  "Notebook" -> Automatic,
  "PollInterval" -> 1.0,
  "EchoInput" -> True,
  "EchoOutput" -> True,
  "EchoPrint" -> True,
  "SnapshotPath" -> Automatic
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

defaultRootDirectory[nb_] := Module[{base, name},
  base = Quiet @ Check[
    If[MatchQ[nb, _NotebookObject], NotebookDirectory[nb], $Failed],
    $Failed
  ];
  name = Quiet @ Check[
    If[MatchQ[nb, _NotebookObject],
      FileNameTake[NotebookFileName[nb], -1],
      "untitled"
    ],
    "untitled"
  ];
  If[!StringQ[name], name = "untitled"];
  If[StringQ[base],
    FileNameJoin[{base, ".shared_kernel_bridge", name}],
    FileNameJoin[{Directory[], ".shared_kernel_bridge", name}]
  ]
];

initializeDirectories[root_String] := <|
  "Root" -> ensureDirectory[root],
  "Queue" -> ensureDirectory[FileNameJoin[{root, "queue"}]],
  "Processing" -> ensureDirectory[FileNameJoin[{root, "processing"}]],
  "Done" -> ensureDirectory[FileNameJoin[{root, "done"}]],
  "Results" -> ensureDirectory[FileNameJoin[{root, "results"}]],
  "Logs" -> ensureDirectory[FileNameJoin[{root, "logs"}]]
|>;

stringify[expr_] := ToString[Unevaluated[expr], InputForm, PageWidth -> Infinity];

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

cellStyle[cell_Cell] := Replace[cell, Cell[_, style_String, ___] :> style, {0}];

cellPlainText[cell_Cell] := Module[{rendered},
  rendered = Quiet @ Check[
    FrontEndExecute[ExportPacket[cell, "PlainText"]],
    $Failed
  ];
  Which[
    StringQ[rendered], rendered,
    ListQ[rendered] && Length[rendered] >= 1 && StringQ[First[rendered]], First[rendered],
    True, stringify[cell]
  ]
];

ExportNotebookSnapshot[path_: Automatic, notebook_: Automatic] := Module[
  {nb, dest, cells, sections, style, text},
  nb = normalizeNotebook[notebook];
  If[! MatchQ[nb, _NotebookObject],
    Return[$Failed]
  ];

  dest = Replace[path, Automatic :>
    If[AssociationQ[$BridgeState] && KeyExistsQ[$BridgeState, "Directories"],
      FileNameJoin[{$BridgeState["Directories"]["Root"], "notebook_snapshot.md"}],
      FileNameJoin[{Directory[], "notebook_snapshot.md"}]
    ]
  ];

  cells = Quiet @ Check[NotebookRead /@ Cells[nb], $Failed];
  If[cells === $Failed,
    Return[$Failed]
  ];

  sections = Map[
    Function[cell,
      style = Replace[cellStyle[cell], Except[_String] -> "Cell"];
      text = StringTrim[cellPlainText[cell]];
      StringRiffle[
        {
          "## " <> style,
          "",
          "```text",
          text,
          "```"
        },
        "\n"
      ]
    ],
    cells
  ];

  Export[dest, StringRiffle[sections, "\n\n"], "Text"];
  dest
];

bridgeResultAssociation[id_String, code_String, status_String, result_, messages_List, prints_List, duration_] := <|
  "id" -> id,
  "timestamp" -> DateString[{"ISODate", " ", "Time"}],
  "status" -> status,
  "code" -> code,
  "resultInputForm" -> stringify[result],
  "resultJSON" -> Quiet @ Check[
    ImportString[ExportString[result, "RawJSON"], "RawJSON"],
    Null
  ],
  "messages" -> messages,
  "prints" -> prints,
  "durationSeconds" -> N[duration, 6]
|>;

processCommandFile[file_String, state_Association] := Module[
  {
    queueName,
    queueId,
    processingFile,
    doneFile,
    resultFile,
    logFile,
    code,
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
    silent = False
  },

  queueName = FileNameTake[file];
  queueId = FileBaseName[file];
  processingFile = FileNameJoin[{state["Directories"]["Processing"], queueName}];
  doneFile = FileNameJoin[{state["Directories"]["Done"], queueName}];
  resultFile = FileNameJoin[{state["Directories"]["Results"], queueId <> ".json"}];
  logFile = FileNameJoin[{state["Directories"]["Logs"], queueId <> ".log"}];

  Quiet @ Check[
    RenameFile[file, processingFile, OverwriteTarget -> True],
    Return[$Failed]
  ];

  code = Quiet @ Check[Import[processingFile, "Text", CharacterEncoding -> "UTF8"], $Failed];
  If[! StringQ[code],
    status = "read_error";
    Export[resultFile, bridgeResultAssociation[queueId, "", status, $Failed, {}, {}, 0.], "JSON"];
    RenameFile[processingFile, doneFile, OverwriteTarget -> True];
    Return[status]
  ];

  silent = StringStartsQ[code, "(*SILENT*)"];

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
      appendTextCell[nb, "Bridge parse error in queued command " <> queueId <> ".", "Message"]
    ];
    Export[resultFile, bridgeResultAssociation[queueId, code, status, $Failed, messages, prints, 0.], "JSON"];
    Export[logFile, StringRiffle[messages, "\n"], "Text"];
    RenameFile[processingFile, doneFile, OverwriteTarget -> True];
    Return[status]
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
        Print = printFunction
      },
      With[{evaluated = ReleaseHold[held]},
        messages = stringify /@ $MessageList;
        evaluated
      ]
    ],
    status = "aborted";
    $Aborted
  ];
  duration = AbsoluteTime[] - started;

  If[!silent && status === "ok" && messages =!= {},
    appendTextCell[nb, StringRiffle[messages, "\n"], "Message"]
  ];

  If[!silent && status === "ok" && TrueQ[state["EchoOutput"]] && result =!= Null,
    appendOutputCell[nb, result]
  ];

  If[StringQ[state["SnapshotPath"]],
    Quiet @ Check[ExportNotebookSnapshot[state["SnapshotPath"], nb], Null]
  ];

  Export[
    resultFile,
    bridgeResultAssociation[queueId, code, status, result, messages, prints, duration],
    "JSON"
  ];

  Export[
    logFile,
    StringRiffle[
      {
        "status: " <> status,
        "durationSeconds: " <> ToString @ N[duration, 6],
        "messages:",
        If[messages === {}, "(none)", StringRiffle[messages, "\n"]],
        "prints:",
        If[prints === {}, "(none)", StringRiffle[prints, "\n"]],
        "resultInputForm:",
        stringify[result]
      },
      "\n\n"
    ],
    "Text"
  ];

  RenameFile[processingFile, doneFile, OverwriteTarget -> True];
  status
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

cellIDOf[cellExpr_] := Module[{ids},
  ids = Cases[cellExpr, HoldPattern[CellID -> v_Integer] :> v, Infinity];
  If[ids === {}, Missing["NoID"], First[ids]]
];

nextFreeCellID[nb_NotebookObject] := Module[{ids},
  ids = Cases[
    NotebookRead /@ Cells[nb],
    HoldPattern[CellID -> v_Integer] :> v,
    Infinity
  ];
  If[ids === {}, 1, Max[ids] + 1]
];

assignCellIDsToNotebook[nb_NotebookObject] := Module[
  {currentCells, nextId, finalCells, savedSel, savedScroll, didWrite = False},
  savedSel = Quiet @ Check[SelectedCells[nb], {}];
  savedScroll = Quiet @ Check[
    CurrentValue[nb, "WindowScrollPosition"],
    Null
  ];
  currentCells = Cells[nb];
  nextId = nextFreeCellID[nb];
  Do[
    Module[{cellObj, cellExpr, existing},
      cellObj = currentCells[[i]];
      cellExpr = NotebookRead[cellObj];
      existing = cellIDOf[cellExpr];
      If[MissingQ[existing],
        NotebookWrite[cellObj, Append[cellExpr, CellID -> nextId]];
        nextId++;
        didWrite = True
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
      Quiet @ Check[SetOptions[nb, WindowScrollPosition -> savedScroll], Null]
    ]
  ];
  MapIndexed[
    Module[{cellExpr, id},
      cellExpr = NotebookRead[#1];
      id = cellIDOf[cellExpr];
      <|"index" -> First[#2], "cellID" -> id, "cellObject" -> #1|>
    ] &,
    finalCells
  ]
];

findCellByID[nb_NotebookObject, cellID_Integer] := SelectFirst[
  Cells[nb],
  Module[{ids},
    ids = Cases[NotebookRead[#], HoldPattern[CellID -> v_Integer] :> v, Infinity];
    MemberQ[ids, cellID]
  ] &,
  Missing["NotFound"]
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

BridgeReadNotebook[path_String] := Module[{nb, mapping},
  nb = findNotebookByPath[path];
  If[! MatchQ[nb, _NotebookObject],
    Return[<|"status" -> "notebook_not_open", "path" -> path|>]
  ];
  mapping = assignCellIDsToNotebook[nb];
  <|
    "status" -> "ok",
    "path" -> path,
    "cells" -> Map[
      Module[{cellObj, cellExpr, style, content},
        cellObj = #["cellObject"];
        cellExpr = NotebookRead[cellObj];
        style = Replace[cellStyleOf[cellExpr], Except[_String] -> "Cell"];
        content = cellInputCode[cellObj];
        <|
          "index" -> #["index"],
          "cellID" -> #["cellID"],
          "style" -> style,
          "content" -> content
        |>
      ] &,
      mapping
    ]
  |>
];

BridgeRunCell[path_String, cellID_Integer] := Module[
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
    lineNum
  },

  nb = findNotebookByPath[path];
  If[! MatchQ[nb, _NotebookObject],
    Return[<|"status" -> "notebook_not_open", "path" -> path|>]
  ];

  target = findCellByID[nb, cellID];
  If[! MatchQ[target, _CellObject],
    Return[<|
      "status" -> "cell_id_not_found",
      "cellID" -> cellID
    |>]
  ];

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
      With[{evaluated = ReleaseHold[parseHeld]},
        messages = stringify /@ $MessageList;
        evaluated
      ]
    ],
    status = "aborted"; $Aborted
  ];
  duration = AbsoluteTime[] - started;

  (* Persist Out[lineNum] so the kernel's history mirrors a main-loop eval, *)
  (* and label both input + output cells so the front end shows In[N]:= / Out[N]= *)
  Quiet @ Check[Out[lineNum] = result, Null];
  (* CellLabelAutoDelete -> False keeps the label text from being cleared. *)
  (* CellLabelStyle -> "CellLabel" (without "CellLabelExpired") keeps the  *)
  (* label rendered as a fresh / just-evaluated label, not a stale one.    *)
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
  ];

  <|
    "status" -> status,
    "cellID" -> cellID,
    "resultInputForm" -> stringify[result],
    "messages" -> messages,
    "prints" -> prints,
    "durationSeconds" -> N[duration, 6]
  |>
];

BridgeUpdateCell[path_String, cellID_Integer, cellType_String, content_String] := Module[
  {nb, target, replacement},
  nb = findNotebookByPath[path];
  If[! MatchQ[nb, _NotebookObject],
    Return[<|"status" -> "notebook_not_open", "path" -> path|>]
  ];
  target = findCellByID[nb, cellID];
  If[! MatchQ[target, _CellObject],
    Return[<|
      "status" -> "cell_id_not_found",
      "cellID" -> cellID
    |>]
  ];
  replacement = Cell[content, cellType, CellID -> cellID];
  NotebookWrite[target, replacement];
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
  anchor = findCellByID[nb, cellID];
  If[! MatchQ[anchor, _CellObject],
    Return[<|"status" -> "cell_id_not_found", "cellID" -> cellID|>]
  ];
  newId = nextFreeCellID[nb];
  newCell = Cell[content, cellType, CellID -> newId];
  SelectionMove[anchor, position, Cell, AutoScroll -> False];
  NotebookWrite[nb, newCell];
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
  target = findCellByID[nb, cellID];
  If[! MatchQ[target, _CellObject],
    Return[<|"status" -> "cell_id_not_found", "cellID" -> cellID|>]
  ];
  outputTag = bridgeOutputTagFor[cellID];
  existingOutput = findExistingOutputCell[nb, outputTag];
  If[existingOutput =!= Null,
    NotebookDelete[existingOutput];
    deletedOutput = True
  ];
  NotebookDelete[target];
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
  allCellIDs = Cases[
    NotebookRead /@ allCells,
    HoldPattern[CellID -> v_Integer] :> v,
    Infinity
  ];
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

ProcessSharedKernelBridgeQueue[] := Module[{state, files},
  state = $BridgeState;
  If[! AssociationQ[state] || ! KeyExistsQ[state, "Directories"],
    Return[0]
  ];
  files = Sort @ FileNames["*.wl", state["Directories"]["Queue"]];
  Scan[processCommandFile[#, state] &, files];
  Length[files]
];

StopSharedKernelBridge[] := Module[{task = Lookup[$BridgeState, "Task", Missing["NoTask"]]},
  If[MatchQ[task, _ScheduledTaskObject],
    Quiet @ Check[RemoveScheduledTask[task], Null]
  ];
  $BridgeState = <||>;
  Null
];

StartSharedKernelBridge[OptionsPattern[]] := Module[
  {nb, root, dirs, snapshotPath, task, status},
  StopSharedKernelBridge[];

  nb = normalizeNotebook[OptionValue["Notebook"]];
  root = Replace[OptionValue["RootDirectory"], Automatic :> defaultRootDirectory[nb]];
  dirs = initializeDirectories[root];
  snapshotPath = Replace[
    OptionValue["SnapshotPath"],
    Automatic :> FileNameJoin[{dirs["Root"], "notebook_snapshot.md"}]
  ];

  $BridgeState = <|
    "Notebook" -> nb,
    "Directories" -> dirs,
    "PollInterval" -> OptionValue["PollInterval"],
    "EchoInput" -> TrueQ[OptionValue["EchoInput"]],
    "EchoOutput" -> TrueQ[OptionValue["EchoOutput"]],
    "EchoPrint" -> TrueQ[OptionValue["EchoPrint"]],
    "SnapshotPath" -> snapshotPath
  |>;

  task = RunScheduledTask[ProcessSharedKernelBridgeQueue[], OptionValue["PollInterval"]];
  $BridgeState["Task"] = task;

  status = SharedKernelBridgeStatus[];
  Quiet @ Check[ExportNotebookSnapshot[snapshotPath, nb], Null];
  status
];

SharedKernelBridgeStatus[] := <|
  "Running" -> MatchQ[Lookup[$BridgeState, "Task", Missing["NoTask"]], _ScheduledTaskObject],
  "NotebookAttached" -> MatchQ[Lookup[$BridgeState, "Notebook", Missing["NoNotebook"]], _NotebookObject],
  "QueueDirectory" -> Lookup[Lookup[$BridgeState, "Directories", <||>], "Queue", Missing["NoQueue"]],
  "ResultsDirectory" -> Lookup[Lookup[$BridgeState, "Directories", <||>], "Results", Missing["NoResults"]],
  "SnapshotPath" -> Lookup[$BridgeState, "SnapshotPath", Missing["NoSnapshot"]],
  "PollInterval" -> Lookup[$BridgeState, "PollInterval", Missing["NoInterval"]]
|>;

End[];

EndPackage[];
