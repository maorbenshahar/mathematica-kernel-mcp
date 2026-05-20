# mathematica-kernel-mcp

MCP server for Mathematica / Wolfram Language. In **collab mode** you and the
LLM share the same kernel and the same open notebook — when the LLM edits or
runs a cell, you see it live. In **solo mode** the MCP runs its own kernel and
edits files on disk. Mode is auto-detected per file (collab if a
`.shared_kernel_bridge/` directory sits next to the file).

Collab mode now prefers an authenticated localhost socket transport, so normal
tool calls do not rely on bidirectional filesystem polling. The original file
queue transport is still available as a fallback with
`StartSharedKernelBridge["Transport" -> "File"]`.

## Quick start

Need: Mathematica 12.0+, Python 3.10+, an MCP-capable client (e.g.
[Claude Code](https://claude.ai/download)). All paths below should be absolute.

```bash
git clone https://github.com/maorbenshahar/mathematica-kernel-mcp.git
cd mathematica-kernel-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
claude mcp add mathematica-kernel-mcp -- $(pwd)/.venv/bin/python -m mathematica_kernel_mcp
```

Once, in any Mathematica notebook (session-scoped — re-run on each kernel
restart, or add to your personal `Kernel/init.m` for persistence):

```wl
PacletDirectoryLoad["/abs/path/to/mathematica-kernel-mcp/wolfram/"]
```

Or to install permanently into `$UserBasePacletsDirectory`:

```wl
PacletInstall[CreatePacletArchive["/abs/path/to/mathematica-kernel-mcp/wolfram/SharedKernelMCP/"]]
```

Then in the notebook of the file you want to work on:

```wl
<< SharedKernelMCP`
StartSharedKernelBridge[]
```

Open your LLM client and ask it to read or edit the file. The `notebook_*`
tools operate on files/notebooks, and the `kernel_*` tools manage explicit
agent-owned scratch kernels.

## Tools

- **Cell ops**: `notebook_read`, `notebook_search`, `notebook_run_cell`,
  `notebook_run_cells` (batch), `notebook_update_cell`,
  `notebook_insert_cell_after`/`_before`, `notebook_delete_cell`,
  `notebook_eval`, `notebook_eval_inline`, `notebook_sweep_outputs`.
- **State + introspection**: `notebook_kernel_state`, `notebook_kernel_restart`,
  `notebook_abort_evaluation`, `notebook_symbol_info`,
  `notebook_documentation_search`, `notebook_names`, `notebook_list_symbols`,
  `notebook_get_output`.
- **Agent-owned scratch kernels**: `kernel_create`, `kernel_list`,
  `kernel_eval`, `kernel_eval_json`, `kernel_state`, `kernel_get_output`,
  `kernel_restart`, `kernel_abort`, `kernel_close`.

Use scratch kernels for exploratory probes that should not pollute the user's
live notebook kernel. `notebook_eval`, `notebook_run_cell`,
`notebook_run_cells`, and `notebook_get_output` also accept `kernel_id` to
route evaluation/output lookup through an explicit agent-owned kernel while
still reading notebook cell contents from the target file.

`cell_id` is opaque — pass back what you got from `notebook_read` /
`notebook_search`. In collab it's a Mathematica `CellID` (stable); in solo
it's a 1-indexed position (re-read after mutations).

`StartSharedKernelBridge[]` defaults to `"Transport" -> "Auto"`: it starts a
localhost socket bridge when possible and falls back to the file queue if the
socket listener cannot be created. Override with
`"Transport" -> "Socket"` to require sockets or `"Transport" -> "File"` to use
the legacy queue. In file mode, `"PollInterval" -> 1.0` controls queue polling.
Socket responses are UTF-8 JSON with `Content-Length` framing, so large notebook
reads do not depend on connection-close parsing. `notebook_read(...,
include_content=False)` asks the bridge for previews only instead of moving full
cell contents and discarding them client-side.

## Bounding evaluation time

The run-style tools accept an optional `eval_timeout` (seconds). When set, the
kernel wraps the eval in `TimeConstrained[..., eval_timeout]`; an evaluation
that exceeds it returns `status="timeout"` instead of hanging the kernel.
**This is the recommended autonomous mechanism** — fully cross-platform,
headless, no signals involved.

```text
notebook_run_cell(path, cell_id, eval_timeout=10)   # abort the cell after 10s
notebook_eval(path, "Simplify[hugeExpr]", eval_timeout=30)
```

If an evaluation has already escaped `eval_timeout` (rare, but possible with
pathological C-level routines that don't poll), `notebook_abort_evaluation(path)`
is a *best-effort* fallback that sends SIGINT to the kernel PID. Behavior
depends on mode + OS:

- **Solo mode on POSIX (Linux/macOS)** — silent, headless. The kernel calls
  `Abort[]` at its next polling point.
- **Solo mode on Windows** — POSIX signal semantics differ; not reliable. Use
  `eval_timeout` instead.
- **Collab mode (any OS)** — the GUI front-end intercepts SIGINT and prompts
  the user via an interactive dialog. Treat this tool as "tap on the user's
  shoulder," not a fully autonomous abort.

`clear_queue=True` additionally drops any pending queued commands so the abort
isn't immediately followed by another runaway (collab only).


## Troubleshooting

- `<< SharedKernelMCP`` "package not found" → paclet not loaded; run
  `PacletDirectoryLoad["/path/to/wolfram/"]` (session-scoped) or
  `PacletInstall[CreatePacletArchive["/path/to/wolfram/SharedKernelMCP/"]]`
  (permanent).
- Tool returns `bridge_unavailable` → no `.shared_kernel_bridge/` next to the
  file. Bootstrap the bridge in a notebook first.
- Collab calls return socket errors → check `SharedKernelBridgeStatus[]`.
  If you need the old behavior while debugging, restart with
  `StartSharedKernelBridge["Transport" -> "File"]`.
- Tool hangs / times out → the bridge transport may be dead.
  In socket mode, `SharedKernelBridgeStatus[]` should show
  `Transport -> "Socket"` and `Running -> True`. In file mode, `Running -> True`
  means the polling task is active. If not, re-run `StartSharedKernelBridge[]`.
- Solo mode: "Cannot locate a kernel automatically" → `wolframclient` can't
  find the WolframKernel binary. Easier path is collab mode (uses Mathematica's
  own kernel via the bridge).

## TODO

- `.nb` end-to-end coverage.

## Contributing

Contributions welcome!

## Citation

If this tool helped your research, a citation is welcomed but not required:

```bibtex
@software{benshahar_mathematica_kernel_mcp,
  author = {Ben-Shahar, Maor},
  title  = {mathematica-kernel-mcp: Collaborative Mathematica via MCP},
  year   = {2026},
  url    = {https://github.com/maorbenshahar/mathematica-kernel-mcp}
}
```

## License

MIT — see [`LICENSE`](LICENSE).
