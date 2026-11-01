# 10 · Development Guide

## Requirements

- Windows + SolidWorks 2026 (only for live testing and the C# build).
- Visual Studio 2022 / MSBuild with .NET Framework 4.8.
- Python ≥ 3.9 (the compiler package allows 3.9; the adapter needs the MCP SDK v2: `mcp>=2.0.0,<3`).

```bash
pip install -r adapters/claude/requirements-dev.txt
```

That file pulls the runtime requirements (`mcp`, `httpx`, `python-dotenv`, `ezdxf`), `pytest`, and an editable install of `compiler/solidworks` (`solidpilot-pycompiler`).

## Offline tests (no SolidWorks needed)

| Suite | Command | Guards |
|---|---|---|
| Tool + resource contract | `cd adapters/claude && python tests/test_schema_contract.py` | `server.py` ⇔ `tool-schemas.json`; recipe sections ⇔ resources; every `recipe://`/`schema://` URI in `server.py`; `_draw.*` API |
| HTTP client | `pytest adapters/claude/tests/test_execution_client_http.py` | proxy bypass, connect-timeout floor, health probe |
| IR contract | `cd compiler/solidworks && python -m pycompiler.tests.test_ir_schema_contract` | schema `covered_subset` ⇔ `ir_schema.py` |
| Compiler | `cd compiler/solidworks && python -m pycompiler.tests.test_compiler` | validation, lowering, resolver, frames, failures (FakePort) |
| Drawing reader | `cd cad-planner/drawing && python -m tests.test_draw_contract` | dialect drift, chaining/pairing/gate goldens, IR validity |

Each also runs under pytest, e.g. `pytest compiler/solidworks/pycompiler/tests/test_compiler.py::test_sketch_extrude_boss_path`. The standalone runners print `ok`/`FAIL` per test and exit non-zero on failure.

**Automatic check:** `.claude/settings.json` registers a PostToolUse hook (`.claude/hooks/contract_check.py`) that, after any Edit/Write, re-runs the suites covering the edited path and reports failures:

| Edited path | Suites re-run |
|---|---|
| `adapters/claude/`, `tool-schemas.json`, `recipe-usage.md` | tool contract |
| `compiler/solidworks/pycompiler/` | IR contract, compiler, drawing |
| `feature-graph.schema.json` | IR contract, drawing |
| `cad-planner/drawing/` | drawing, tool contract |
| `draw-dialect.schema.json` | drawing |

## Execution layer

```powershell
& "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe" execution\solidworks\SolidworksExecution.sln /t:Build /p:Configuration=Debug
Get-Process SolidworksExecution -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Process execution\solidworks\SolidworksExecution\bin\Debug\SolidworksExecution.exe -WindowStyle Hidden
```

- Stop the running exe before building, or the output files stay locked.
- Restarting the exe resets `state_version` to 0; the adapter recovers automatically on its next call.
- Quick check: `curl http://localhost:5000/health` (use `localhost`, not `127.0.0.1`).
- Logs: `execution\solidworks\SolidworksExecution\bin\Debug\execution.log`.
- `bin/Debug` is committed; rebuild and verify live before committing a new binary.

## Adapter

- Normally launched by the MCP client. Claude Desktop config (`%APPDATA%\Claude\claude_desktop_config.json`) registers `python <repo>\adapters\claude\server.py` under `mcpServers`.
- **No hot reload:** after editing `server.py`, reconnect the MCP server. Edits to `recipe-usage.md` and the schemas are served fresh without a reconnect.
- Never write to stdout from adapter code (it's the JSON-RPC channel); use `adapter_log.write` → `adapters/claude/adapter.log`.
- Config overrides go in `adapters/claude/.env` ([03-adapter.md](03-adapter.md#configuration-configpy-overridable-in-adaptersclaudeenv)).

## Adding a low-level tool

1. **Contract:** add the entry to `execution/solidworks/contracts/tool-schemas.json` (description, `input.params`).
2. **C#:** add `case "tool_name":` to `ToolController.Dispatch` and implement the method in the matching `SolidWorksService.<Family>.cs`, following the standard preamble (duplicate → version → connected → work → `RegisterCompleted`). A new file also needs a `<Compile Include>` in the csproj.
   - Reflect the interop assembly to confirm every COM signature; decode unknown return shapes against a live document before writing the parser.
   - Inline `swconst` values as ints; don't use the enum types.
3. **Adapter:** add the `@mcp.tool(structured_output=False)` function in `server.py`. Use `Literal` for discriminators, `Annotated[float, Field(...)]` for ranges and units, JSON strings for arrays, `-1` sentinels for optional indices, and forward via `_call(name, params)`.
4. Run the contract test, build, restart the exe, reconnect MCP, verify live.

## Adding an IR feature

Usually no new low-level tool is needed: see [04-feature-graph-ir.md](04-feature-graph-ir.md#adding-a-node-type). Then update the relevant `mapping_*` / `forward` section of `recipe-usage.md` so the model knows how to emit it.

## Live verification (manual by design)

- With SolidWorks open, choose a coherent batch of tools and test them together.
- On failure, read `execution.log` (and `adapter.log`) and record: expected result, actual response, hypothesis.
- For IR work, the bar is a round-trip `verified` result ([08](08-analysis-and-verification.md)).
- For drawing tools, export to PDF and check the PDF; in-band counts under-report.
- Known runtime behaviour: analysis tools may wait for a click in the SolidWorks GUI, and after a very long session MCP calls can start failing on the client side; retrying continues.

## Git and repo hygiene

- Work on `develop`; `main` is the PR target.
- `.gitignore` ignores **all `*.md` files** except an allow-list (`README.md`, `CONTRIBUTING.md`, `CLA.md`, `master-architecture.md`, `architecture.md`, `recipe-usage.md`, `docs/*.md`). Any other new Markdown file — including `CLAUDE.md` — stays local unless un-ignored explicitly.
- `.github/workflows/ci.yml` (which runs the offline suites) is local-only by project decision.
- Contributions require the CLA: `git commit -s` or the CLA line in the PR description.
