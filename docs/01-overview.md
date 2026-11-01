# 01 · Overview

**SolidPilot** (repo `mcp-server-solidworks`) is an MCP server that lets an AI model drive **SolidWorks 2026** (target SP3) on Windows. License: AGPL-3.0 with a commercial dual license (every contribution falls under `CLA.md`).

## The problem it solves

The SolidWorks API has thousands of methods. Exposing each as an MCP tool would blow up context and token cost. SolidPilot instead raises the level of abstraction:

1. The model expresses **design intent at the feature level** ("a sketch on the top plane, extruded 20 mm, with a through hole in the top face").
2. That intent is written as a CAD-neutral **Feature Graph IR** (JSON).
3. A **deterministic compiler** (no LLM) lowers the IR into ordered low-level tool calls.
4. A C# **execution layer** carries them out over COM.

So one model call can produce an entire part, and everything below the IR is reproducible.

## What works today

| Capability | Entry point | Status |
|---|---|---|
| Low-level CAD operations (sketch, features, sheet metal, assemblies, drawings, export) | 40-odd MCP tools | Verified live; still the most-used path |
| Build from design intent | `submit_feature_graph` (forward IR door) | Live |
| Reproduce an existing part/assembly from its IR | `save_analysis` → model writes IR → `rebuild_from_ir` → `compare_parts` | Live; real production parts reproduced to `verified` |
| Rebuild a part from a 2D drawing file | `analyze_drawing` (.DXF/.DWG) | Sheet-metal flat patterns build directly; other parts return an analysis for the model to interpret |
| Single high-level tool replacing the low-level surface | — | Planned; blocked on a reference resolver that survives edits |

## Repository map

```
adapters/claude/          Python MCP server (the only thing the AI client talks to)
cad-planner/              CAD-neutral: IR schema + capability registry, recipe for the model,
  contracts/              analysis-artifact & draw-dialect schemas
  drawing/                DXF/DWG → analysis → IR reader
  recipe-usage.md         rules the model follows when writing IR (served over MCP)
compiler/solidworks/      pycompiler: deterministic IR → tool-call compiler + reference resolver
execution/solidworks/     C# .NET 4.8 REST server — the only layer touching SolidWorks COM
  contracts/              tool-schemas.json, state-format.json, execution-response.json
  SolidworksExecution/bin/Debug/   prebuilt server (committed on purpose)
master-architecture.md    decided architecture (short)
docs/                     this folder
```

## How a request flows (forward build)

```
AI client ──MCP stdio──> server.py: submit_feature_graph(graph)
                            │  (in process)
                            ▼
                     pycompiler.compile_and_run
                       validate → for each node: resolve refs (analyze_model reads) → lower → execute
                            │  REST  POST http://localhost:5000/api/tool/execute
                            ▼
                     SolidworksExecution.exe → StaExecutor (one STA thread) → SolidWorks COM
```

A low-level tool call (e.g. `extrude_feature`) skips the compiler: `server.py` → REST → C#.

## Project status and philosophy

- Early alpha / working prototype; schemas are marked DRAFT.
- **IR vocabulary grows from real production parts**, not from speculation. A node type is added when a real part needs it and proven by a round-trip `verified` result.
- Automated tests run offline (no SolidWorks). CAD behaviour itself is verified manually against a live SolidWorks GUI.
- Private dev ledgers (ADRs, `KNOWN-LIMITATIONS`, session logs) are deliberately kept out of the repo. Code comments cite them as `ADR-0xx`, `IR-ADR-0xx` or `KNOWN-LIMITATIONS #n`; treat those comments as the available rationale.
