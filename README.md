# mathematica-kernel-mcp

MCP server for collaborative or solo Mathematica / Wolfram Language development.

## Two modes

The MCP exposes a single `notebook_*` tool surface (read, search, run, update,
insert, delete, eval). Each tool dispatches per call based on whether a shared
kernel bridge is present next to the target file:

- **Collaborative** — the user has Mathematica open with the file and has
  evaluated `StartSharedKernelBridge[...]` from `shared_kernel/shared_kernel_bridge.wl`.
  The MCP talks to the user's kernel through a file-based queue/results
  protocol; edits land live in the user's open notebook; kernel state is
  shared.
- **Solo** — no bridge. The MCP parses `.m`/`.nb` from disk and runs code in a
  kernel it spawns via `wolframclient`. Requires a locatable WolframKernel
  binary.

Detection is automatic: a `<file_dir>/.shared_kernel_bridge/queue/` directory
selects collab mode; otherwise solo.

## Tools

`notebook_read`, `notebook_search`, `notebook_run_cell`, `notebook_update_cell`,
`notebook_insert_cell_after`, `notebook_insert_cell_before`,
`notebook_delete_cell`, `notebook_eval`, `notebook_eval_inline`,
`notebook_sweep_outputs`.

`cell_id` is opaque — pass back what you got from `notebook_read` /
`notebook_search`. In collab it's a Mathematica `CellID` (integer, stable
across reorders); in solo it's a 1-indexed position (re-read after mutations).

## Setting up the bridge (collab mode)

In a notebook that has the file open, evaluate:

```wl
Get[FileNameJoin[{NotebookDirectory[], "shared_kernel_bridge.wl"}]];
StartSharedKernelBridge[]
```

`StartSharedKernelBridge[]` with no args picks sensible defaults: it attaches
to the evaluation notebook and creates a `<NotebookDirectory[]>/.shared_kernel_bridge/`
directory next to the file. The bridge polls `<RootDirectory>/queue/` for queued
`.wl` files, evaluates them in the notebook's kernel, and writes JSON results to
`<RootDirectory>/results/`.

Override defaults if needed (rare):

```wl
StartSharedKernelBridge[
  "RootDirectory" -> "/some/other/dir",
  "PollInterval" -> 0.5
]
```

## TODO

- **Paclet packaging** for the bridge so the bootstrap is `<< SharedKernelMCP\``
  + `StartSharedKernelBridge[]`, with no `Get[FileNameJoin[...]]` path dance.
  Ship the paclet under the Python package and add a `notebook_install_paclet`
  tool that copies it into `$UserBasePacletsDirectory`.
- **Per-notebook bridge directories** (`.shared_kernel_bridge/<basename>/`) so
  multiple `.m`/`.nb` files in the same folder don't share a queue and race.
- **`.nb` end-to-end coverage** — the backends are `.nb`-aware in principle but
  haven't been exercised.
- **Solo-mode kernel-path discovery** — let `SessionManager` pick up
  `WOLFRAM_KERNEL_PATH` from env when `wolframclient` can't auto-locate.

## Layout

```
src/mathematica_kernel_mcp/
  server.py     # FastMCP entry point + the 10 notebook_* tools
  backends.py   # BridgeBackend + SoloBackend + dispatcher
  bridge.py     # Python client for the file-based bridge protocol
  session.py    # wolframclient-backed kernel session manager (solo)
  parser.py     # .m / .wl cell parser (solo)
  notebook.py   # .nb cell helpers using a kernel (solo)
shared_kernel/
  shared_kernel_bridge.wl  # Wolfram package the user loads in their notebook
```
