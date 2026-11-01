# 02 · Architecture

The decided design is in [master-architecture.md](../master-architecture.md). This page summarizes it and connects it to the code.

## Layers

```
                    AI client (Claude Desktop, other MCP hosts)
                                │  MCP (stdio)            ← the ONLY MCP boundary
                                ▼
 ┌────────────────────── adapters/claude (Python) ───────────────────────┐
 │ 47 tools · 13 resources · state_version tracking · orchestration       │
 │   low-level tools ─────────────┐        IR doors ──► pycompiler (in-proc)
 └────────────────────────────────┼─────────────────────────┼────────────┘
                                  │ REST                    │ REST via ExecutionPort
                                  ▼                         ▼
 ┌──────────────── execution/solidworks (C# .NET 4.8) ──────────────────┐
 │ OWIN :5000 · OperationGuard (idempotency, state_version) · StaExecutor │
 └──────────────────────────────────┬────────────────────────────────────┘
                                    │ COM (single STA thread)
                                    ▼
                               SolidWorks 2026

 cad-planner/ (CAD-neutral): IR schema = capability registry, recipe prose, drawing reader
```

| Layer | Language | Knows about | Must not |
|---|---|---|---|
| `cad-planner` | JSON / Markdown / Python (drawing reader) | feature intent, IR | touch SolidWorks, emit tool sequences, validate geometry |
| `compiler/solidworks` | Python | IR → tool calls, live-geometry resolution | use an LLM, MCP or COM |
| `execution/solidworks` | C# | COM, CAD state | contain AI/planning logic, know who is calling |
| `adapters/*` | Python | one specific AI client's protocol | contain CAD logic |

**Phase 1 (now):** the "planner" is the host model itself, guided by the IR schema and `recipe-usage.md`; there's no planner service. **Phase 2:** an optional dedicated or local planner using the same IR contract.

## Invariants

- The AI works at the **feature level**; only the compiler knows tools; only `SolidWorksService*.cs` touches COM.
- **MCP is the top boundary only.** Below it, everything is plain REST or in-process calls.
- The IR is **CAD-neutral**. A new CAD backend means a new compiler + execution layer; the IR and `cad-planner` stay unchanged.
- Execution and planner have **zero knowledge of the adapter/host**. A new AI client means a new adapter only.
- The compiler is deterministic. A second *LLM* re-doing the host's reasoning is what's forbidden, not a second planning step.
- **Never fork the compiler.** All three IR doors (`rebuild_from_ir`, `submit_feature_graph`, `analyze_drawing mode=build`) go through the same `_run_graph` → `pycompiler.compile_and_run`.

## Validation split

| Stage | Owner | Checks |
|---|---|---|
| Plan time | IR schema + `ir_schema.validate` | structure: types, params, enums, node references, grammar |
| Compile/exec time | resolver + execution | geometry: face exists, anchor matches exactly one entity, feature builds |

The planner can't validate geometry because it has no live model. Don't move geometric checks into the schema.

## Contracts and their guards

| Contract | Location | Guarded by |
|---|---|---|
| Feature Graph IR + capability registry | `cad-planner/contracts/feature-graph.schema.json` | `pycompiler/tests/test_ir_schema_contract.py` |
| Low-level tool surface | `execution/solidworks/contracts/tool-schemas.json` | `adapters/claude/tests/test_schema_contract.py` |
| MCP resource surface (recipe sections, URIs) | `cad-planner/recipe-usage.md` + `server.py` | same contract test |
| Draw dialect | `cad-planner/contracts/draw-dialect.schema.json` | `cad-planner/drawing/tests/test_draw_contract.py` |
| Drawing → IR compatibility | IR emitted by `drawing/lowering.py` | same drawing test (validates against `ir_schema`) |
| Response / CadState | `execution-response.json`, `state-format.json` | not tested (prose) |
| Analysis artifact | `analysis-artifact.schema.json` | not tested |

## Concurrency and consistency model

- **Optimistic concurrency:** each execute request carries the client's `state_version`; the server requires strict equality and increments it on each registered success. Mismatch → `FAILED INVALID_STATE_VERSION`.
- **Idempotency:** every request carries a fresh UUID `operation_id`; a replayed id returns `DUPLICATE`. Failures aren't recorded, so a retried failure re-executes.
- **Resync:** `GET /api/tool/state`. The adapter resyncs and retries once on a version mismatch, and always resyncs after an IR run (the compiler advances the version through its own port).
- **Serialization:** all COM work runs on one STA thread; the server handles one CAD operation at a time.
- **No transactions:** a failed multi-step operation (an IR run, `add_sketch_entities`) leaves partial geometry. Callers rebuild in a fresh document rather than roll back.
- Server state lives in memory; restarting the exe resets the version to 0.

## Runtime topology

- The AI client launches `adapters/claude/server.py` (stdio).
- On the first REST call that can't connect, the adapter **auto-starts** `SolidworksExecution.exe` (detached, windowless).
- The exe attaches to a running SolidWorks lazily, or `ensure_ready` launches it — never a second instance.
- Logs: `adapters/claude/adapter.log` and `execution/solidworks/SolidworksExecution/bin/Debug/execution.log`, both using `-> tool op=… sv=…` / `<- tool STATUS` lines so a request can be traced across both.
