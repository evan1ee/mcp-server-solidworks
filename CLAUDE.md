# CLAUDE.md

This file provides guidance to Claude Code (`claude.ai/code`) when working with this repository.

SolidPilot (`mcp-server-solidworks`) is an MCP server that lets an LLM drive SolidWorks at the **CAD feature level**. The model writes a CAD-neutral Feature Graph IR; a deterministic Python compiler lowers it to tool calls; a C# execution server executes them over COM.

Development environment: Windows, PowerShell, Visual Studio 2022 / .NET Framework 4.8, SolidWorks (for live checks only).

## 1. Documentation

**Start at [`docs/README.md`](docs/README.md).** Read a layer's page before changing that layer.

| Area | Documentation |
|---|---|
| Overview, repository map, request flow | [`docs/01-overview.md`](docs/01-overview.md) |
| Layers, invariants, contracts, concurrency | [`docs/02-architecture.md`](docs/02-architecture.md) |
| MCP adapter (`adapters/claude/`) | [`docs/03-adapter.md`](docs/03-adapter.md) |
| Feature Graph IR (`cad-planner/contracts/`) | [`docs/04-feature-graph-ir.md`](docs/04-feature-graph-ir.md) |
| Compiler (`compiler/solidworks/pycompiler`) | [`docs/05-compiler.md`](docs/05-compiler.md) |
| C# execution layer and COM behavior | [`docs/06-execution-layer.md`](docs/06-execution-layer.md) |
| DXF/DWG reader (`cad-planner/drawing/`) | [`docs/07-drawing-reader.md`](docs/07-drawing-reader.md) |
| Analysis artifacts, round-trip, verification | [`docs/08-analysis-and-verification.md`](docs/08-analysis-and-verification.md) |
| MCP tools and resources | [`docs/09-tool-reference.md`](docs/09-tool-reference.md) |
| Setup, tests, build, and extending capabilities | [`docs/10-development.md`](docs/10-development.md) |

Contracts and implementation take precedence over prose. Older `*/architecture.md` files may contain stale counts; see [`docs/README.md#stale-statements-elsewhere`](docs/README.md#stale-statements-elsewhere). Update the matching `docs/` page whenever documented behavior or a contract changes.

## 2. Architectural Constraints

- Only the compiler knows MCP tools; only `SolidWorksService*.cs` touches COM. The adapter contains no CAD business logic.
- MCP is the **top-level boundary only**. It is never an internal transport between the compiler and execution layer; compiler-to-execution communication uses REST through `ExecutionPort`.
- The IR remains CAD-neutral.
- Validate structural IR constraints at plan time in `ir_schema.py`. Validate geometric constraints during compilation or execution through the resolver and execution layer. Do not move geometry-dependent checks into the schema.
- Never fork the compiler execution path. `rebuild_from_ir`, `submit_feature_graph`, and `analyze_drawing(mode='build')` must go through `_run_graph` → `pycompiler.compile_and_run`.
- `pycompiler` must not import from the adapter; it must remain independently extractable.
- Extend the IR vocabulary in response to real production parts and demonstrate correctness through a round-trip `verified` result, rather than adding speculative node types.

## 3. Coding Rules

### SolidWorks COM and C#

- Never guess a SolidWorks COM signature. Inspect the interop assembly first and decode unknown return values against a live document.
- Plausible-looking COM APIs can be incorrect. Follow [`docs/06-execution-layer.md`](docs/06-execution-layer.md#com-gotchas-worth-knowing-before-you-edit).
- Inline `swconst` values as integer constants; do not use enum types that would require a missing DLL in `bin/Debug`.
- Tool parameters are `JObject`; read them using the established `p.Value<T>("key")` pattern.
- Follow the standard tool preamble: duplicate check → version check → connected check → work → `RegisterCompleted`.
- A new `.cs` file requires a corresponding `<Compile Include>` entry in the project file.

### MCP adapter

- Model-facing tool guidance belongs in `server.py`, including `Literal`, Pydantic `Field`, and docstrings. The model does not see `tool-schemas.json` directly.
- Array parameters are JSON strings, not Python `list` types.
- Optional indices use `-1` sentinels.
- Never write debug output to stdout; stdout is the JSON-RPC stream. Use `adapter_log`.

### Units and geometry

Lengths are always meters. Angles depend on where they appear:

| Context | Angle unit |
|---|---|
| Low-level tools (default) | degrees |
| `modify_dimension` tool | radians |
| IR angles (default) | radians |
| IR `circular_pattern.angle_deg` | degrees |

Preserve established unit conversions and tolerances. Never introduce implicit unit assumptions.

### Contract changes

A new low-level tool must update all three contract locations, which the adapter schema contract test keeps aligned:

1. `execution/solidworks/contracts/tool-schemas.json`
2. `ToolController.cs` and the relevant `SolidWorksService.<Family>.cs`
3. `adapters/claude/server.py`

A new IR node must update the schema, `ir_schema.py`, `lowering.py` and/or `resolver.py`, tests, and the relevant recipe. Follow `docs/10-development.md`.

## 4. Runtime Facts

- Address the C# server as `http://localhost:5000`, never `127.0.0.1`; HTTP.sys rejects the latter.
- The adapter auto-starts the executable.
- `server.py` does not hot-reload. Reconnect the MCP server after editing it.
- `recipe-usage.md` and schemas are read fresh.
- `state_version` uses strict equality and is held in memory by the C# process. Restarting the process resets it to `0`.
- The adapter adopts the version returned by each response and resynchronizes and retries once on `INVALID_STATE_VERSION`.
- CAD results use HTTP 200 with `COMPLETED`, `FAILED`, or `DUPLICATE`. HTTP 400 indicates a malformed request or unknown tool.
- CAD operations are not transactional. A failed IR run can leave partial geometry; rebuild in a fresh document.
- Live verification is manual against an open SolidWorks GUI.
- Adapter logs: `adapters/claude/adapter.log`.
- Execution logs: `execution/solidworks/SolidworksExecution/bin/Debug/execution.log`.
- For drawing tools, the exported PDF is the ground truth for output verification.

## 5. Commands

Run from the repository root. Subshells keep the working directory at the root.

```bash
pip install -r adapters/claude/requirements-dev.txt
```

```bash
(cd adapters/claude && python tests/test_schema_contract.py)                       # adapter schema contract
(cd compiler/solidworks && python -m pycompiler.tests.test_ir_schema_contract)     # IR schema contract
(cd compiler/solidworks && python -m pycompiler.tests.test_compiler)               # compiler tests
(cd cad-planner/drawing && python -m tests.test_draw_contract)                     # drawing contract
pytest compiler/solidworks/pycompiler/tests/test_compiler.py::test_sketch_extrude_boss_path   # single test
```

These suites are offline and do not require SolidWorks.

Build the C# execution server (PowerShell):

```powershell
& "C:\Program Files\Microsoft Visual Studio\2022\Community\MSBuild\Current\Bin\MSBuild.exe" execution\solidworks\SolidworksExecution.sln /t:Build /p:Configuration=Debug
```

Stop the running `SolidworksExecution.exe` before building; restart steps are in [`docs/10-development.md`](docs/10-development.md).

A `PostToolUse` hook (`.claude/hooks/contract_check.py`) reruns the suites associated with edited files. Fix the drift it reports; do not ignore or disable the hook merely to finish a task. Its path-to-suite mapping is in `docs/10-development.md`.

## 6. Workflow

- **Non-trivial changes:** read the layer docs, contracts, and tests; write a short plan (scope, affected contracts, tests, required live checks) before editing. Cross-layer or IR-semantics changes get an architecture review first.
- **Keep changes focused.** No unrelated refactors; preserve unrelated working-tree changes. Never weaken contracts, suppress tests, or bypass validation to get a green result.
- **Debugging:** after two failed fix attempts on the same issue, stop guessing. Record the error output and what was tried, reassess the root cause (an Opus subagent is appropriate here), and revise the plan before the next change.
- **Subagent models:** Haiku for searches and routine git; Sonnet for implementing a written plan; Opus for architecture, COM/IR-semantics review, and stuck debugging.

### Verification

- Run the most targeted test first, then the applicable contract suites; build the C# server when it changed.
- Review the final diff for unintended changes, generated files, and boundary violations.
- Offline tests do not establish live SolidWorks compatibility. A successful C# build does not establish correct COM behavior. A successful MCP handshake does not establish correct CAD output.
- Changes touching COM, geometry, or drawings need live SolidWorks verification. If it was not possible, say so explicitly with the remaining risk.
- Report each check as passed, failed, blocked, or not run. A task can be "implemented but not live-verified"; say so rather than claiming completion.

## 7. Repository Conventions

- Work on `develop`; PRs target `main`. Do not commit or push unless asked.
- Use Conventional Commit prefixes such as `feat:`, `fix:`, and `docs:`.
- Never use `git reset --hard`, `git clean -fd`, or force-push as routine cleanup.
- The C# server's `bin/Debug/` output is committed intentionally. Rebuild and verify live before committing a changed binary.

