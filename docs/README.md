# SolidPilot Developer Docs

Internal documentation for engineers and coding agents working **on** this repository. The user-facing introduction is the root [README.md](../README.md); the contribution and CLA rules are in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Reading order

| If you need to… | Read |
|---|---|
| Understand what this is in five minutes | [01-overview.md](01-overview.md) |
| Know the layers, boundaries and invariants | [02-architecture.md](02-architecture.md) |
| Change the MCP server / add a tool | [03-adapter.md](03-adapter.md), [09-tool-reference.md](09-tool-reference.md) |
| Change the IR vocabulary | [04-feature-graph-ir.md](04-feature-graph-ir.md), [05-compiler.md](05-compiler.md) |
| Change C# / COM code | [06-execution-layer.md](06-execution-layer.md) |
| Work on DXF/DWG reading | [07-drawing-reader.md](07-drawing-reader.md) |
| Understand artifacts and "verified" | [08-analysis-and-verification.md](08-analysis-and-verification.md) |
| Build, test, debug | [10-development.md](10-development.md) |
| Look up a term | [glossary.md](glossary.md) |

## Sources of truth

These docs explain the code; when they disagree with it, the code and contracts win.

| Topic | Authoritative file |
|---|---|
| IR vocabulary + capability registry | `cad-planner/contracts/feature-graph.schema.json` |
| IR validation rules | `compiler/solidworks/pycompiler/ir_schema.py` |
| Low-level tool names and params | `execution/solidworks/contracts/tool-schemas.json` ⇔ `adapters/claude/server.py` |
| Response / state shapes | `execution/solidworks/contracts/execution-response.json`, `state-format.json` |
| Analysis artifact | `cad-planner/contracts/analysis-artifact.schema.json` |
| Drawing reader vocabulary | `cad-planner/contracts/draw-dialect.schema.json` ⇔ `cad-planner/drawing/vocab.py` |
| Rules the model follows when writing IR | `cad-planner/recipe-usage.md` (served as `get_recipe` / `recipe://usage/*`) |

Each pair joined by ⇔ is kept in sync by an offline contract test.

## Stale statements elsewhere

Some older documents predate the current code:

- `adapters/claude/architecture.md` gives 38 / 46 tools and 13 resources with no `get_recipe`; the code has **47 tools** (including `get_recipe`), 13 static resources and 1 template.
- `compiler/solidworks/architecture.md` calls `submit_feature_graph` a commented-out test tool; it is **live** and is the primary forward build path.
- `pycompiler/__init__.py` and some `ir_schema.py` messages say "v0.5 subset" / "26 low-level tools"; the schema is at 0.7.x.
- `CONTRIBUTING.md` and `README.md` mention CI in `.github/workflows/ci.yml`; that file is intentionally local-only and not in the repo.
- `backend_ext` is described as the IR's escape hatch for backend-specific params, but nothing implements it yet.
