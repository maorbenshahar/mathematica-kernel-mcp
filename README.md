# mathematica-kernel-mcp

MCP server for Mathematica / Wolfram Language. In **collab mode** you and the
LLM share the same kernel and the same open notebook — when the LLM edits or
runs a cell, you see it live. In **solo mode** the MCP runs its own kernel and
edits files on disk. Mode is auto-detected per file (collab if a
`.shared_kernel_bridge/` directory sits next to the file).

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

Open your LLM client and ask it to read or edit the file. The 18
`notebook_*` tools are available.

## Tools

- **Cell ops**: `notebook_read`, `notebook_search`, `notebook_run_cell`,
  `notebook_run_cells` (batch), `notebook_update_cell`,
  `notebook_insert_cell_after`/`_before`, `notebook_delete_cell`,
  `notebook_eval`, `notebook_eval_inline`, `notebook_sweep_outputs`.
- **State + introspection**: `notebook_kernel_state`, `notebook_kernel_restart`,
  `notebook_symbol_info`, `notebook_documentation_search`, `notebook_names`,
  `notebook_list_symbols`, `notebook_get_output`.

`cell_id` is opaque — pass back what you got from `notebook_read` /
`notebook_search`. In collab it's a Mathematica `CellID` (stable); in solo
it's a 1-indexed position (re-read after mutations).

`StartSharedKernelBridge[]` defaults to a `.shared_kernel_bridge/` directory in
`NotebookDirectory[]` and a 1s poll. Override with options if you really need
to.


## Troubleshooting

- `<< SharedKernelMCP`` "package not found" → paclet not loaded; run
  `PacletDirectoryLoad["/path/to/wolfram/"]` (session-scoped) or
  `PacletInstall[CreatePacletArchive["/path/to/wolfram/SharedKernelMCP/"]]`
  (permanent).
- Tool returns `bridge_unavailable` → no `.shared_kernel_bridge/` next to the
  file. Bootstrap the bridge in a notebook first.
- Tool hangs / times out → bridge polling task may be dead.
  `SharedKernelBridgeStatus[]` should show `Running -> True`. If not, re-run
  `StartSharedKernelBridge[]`.
- Solo mode: "Cannot locate a kernel automatically" → `wolframclient` can't
  find the WolframKernel binary. Easier path is collab mode (uses Mathematica's
  own kernel via the bridge).

## TODO

- `.nb` end-to-end coverage.
- Solo-mode: respect `WOLFRAM_KERNEL_PATH` env var.

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
