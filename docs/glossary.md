# Glossary

| Term | Meaning |
|---|---|
| **Adapter** | The per-AI-client MCP server (`adapters/claude/`). Translation only, no CAD logic. |
| **Analysis artifact** | JSON cache describing an existing part/assembly (`.solidpilot/<file>.analysis.json`), with an optional embedded IR and its verification status. |
| **Anchor (geometric)** | `{near:[x,y,z], hint?}` — a point used to find a face/edge in the live model. Exact on fresh replay, fragile across edits. |
| **Capability registry** | The set of buildable features. It *is* `feature-graph.schema.json`; there's no separate discovery API. |
| **CadState** | The document snapshot returned with each execution response (active document, sketch, features, dimensions). Rebuilt live on every response. |
| **Canonicalization (C1–C7)** | Recipe rules making the same part always produce the same IR (tree order, `n1..nN` ids, lifted params, SI units, no silent gaps, copy stored orientation flags). |
| **Door (IR door)** | An entry point that runs IR through the compiler: `rebuild_from_ir` (reverse), `submit_feature_graph` (forward), `analyze_drawing mode=build`. |
| **Draw dialect** | The JSON format the drawing reader produces (views, loops, dimensions, bend notes…), defined by `draw-dialect.schema.json`. |
| **DIRECT_BUILDABLE / NOT_DIRECT** | The drawing reader's verdict: build directly from the drawing, or hand the analysis to the model. |
| **DUPLICATE** | Response for an `operation_id` that already completed. |
| **ExecutionPort** | The compiler's interface to execution (`execute`, `get_state`). Faked in tests; `AdapterExecutionPort` in production. |
| **Feature Graph IR** | The CAD-neutral JSON graph of feature nodes the model writes and the compiler builds. |
| **Frame (sketch frame)** | A sketch's origin + x/y directions. The compiler maps profile coordinates from the original frame to the frame SolidWorks actually used. |
| **Lowering** | Turning one IR node into ordered low-level tool calls. |
| **OperationGuard** | C# component enforcing idempotency and `state_version`. |
| **Recipe** | `cad-planner/recipe-usage.md` — the rule book the model follows to write IR, served via `get_recipe` and `recipe://usage/*`. |
| **Resolver** | Compiler module that turns references (selectors, anchors, offsets) into concrete face/edge indices from live `analyze_*` reads. |
| **Round-trip** | Analyze → write IR → rebuild in a fresh document → compare. The only way an IR becomes `verified`. |
| **`source_stale`** | Warning from `rebuild_from_ir` when the source file's hash no longer matches the artifact. |
| **StaExecutor** | The single STA thread that serializes all COM calls in the C# server. |
| **`state_version`** | Integer the client must echo on each execute request; incremented on each successful state-changing operation. |
| **Tier A / Tier B** | Drawing-reader contour chaining: strict degree-2 chaining, and an outer-face fallback. Only Tier A feeds direct builds. |
| **Verified** | Rebuilt part matches the original: exact topology counts and ≤ 1 % volume and area difference (assemblies: components, transforms, mates). |
| **Wire format** | Compact encoding of the drawing analysis (`drawing/wire.py`). |
