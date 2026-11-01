# 05 · Deterministic Compiler (`compiler/solidworks/pycompiler`)

Lowers a Feature Graph IR into ordered execution-layer tool calls and runs them. **No LLM, no MCP, no COM, no HTTP of its own** — it talks to execution only through an injected `ExecutionPort`. Same IR + same starting state ⇒ same tool sequence.

| Module | Job |
|---|---|
| `compiler.py` | `compile_and_run(port, graph) -> CompileResult`: orchestration, placeholder substitution, frame transform, failure mapping |
| `ir_schema.py` | Structural validation (see [04](04-feature-graph-ir.md#validation-split)) |
| `lowering.py` | IR node → list of ops `(tool, params, note)` |
| `resolver.py` | Semantic refs / anchors → face & edge indices via read-only `analyze_*` calls |
| `execution_port.py` | `ExecutionPort` interface: `execute(tool, params, state_version) -> dict`, `get_state() -> int` |
| `errors.py` | `FeatureError(code, message, node_id, node_type, step)` |

Packaged as `solidpilot-pycompiler` (`pyproject.toml`). The adapter imports it in-process (`adapters/claude/ir_execution_port.py` is the production port) and falls back to a `sys.path` insert when it isn't pip-installed. It imports nothing from the adapter, so it can be pulled out into a standalone service later.

## Pipeline

```
validate(graph) ──errors──> FAILED VALIDATION_FAILED   (0 execution calls)
      │ ok
port.get_state() ──fail──> FAILED STATE_SEED_FAILED
      │
for node in nodes (in order):
      ops = _plan_node(node)            # lowering + read-only resolver calls
      for op in ops:
          fill placeholders             # FROM_PREV_FEATURE, node_feature(id), frame transform
          resp = port.execute(op.tool, op.params, sv)
          sv = _advance(resp)           # COMPLETED → stateVersion; DUPLICATE → last_known_state_version; else FeatureError
      record runtime feature name for node.id
if graph.material: run set_part_material as pseudo-node "_material"
```

- **First failure stops the run.** No rollback and no retry (CAD ops aren't transactional). Executed ops stay in `node_log`; partial geometry stays in the document.
- A failed tool becomes `FeatureError(code=<tool's code or TOOL_FAILED>, step=<tool>)`; any other exception becomes `NODE_UNEXPECTED`.
- `CompileResult`: `status`, `nodes_total`, `nodes_completed`, `exec_calls` (includes read-only analyze calls), `final_state_version`, `error {code, message, node_id, node_type, step}`, `node_log [{id, type, status, ops:[{tool, note, state_version}]}]`; `summary()` gives the one-line MCP result.

Error codes you'll see: `VALIDATION_FAILED`, `STATE_SEED_FAILED`, `REFERENCE_UNRESOLVED`, `REFERENCE_AMBIGUOUS`, `NODE_FEATURE_UNAVAILABLE`, `PREV_FEATURE_UNAVAILABLE`, `FRAME_UNAVAILABLE`, `UNKNOWN_NODE_TYPE`, `NODE_UNEXPECTED`, plus pass-through execution codes (e.g. `EXTRUSION_FAILED`).

### Runtime names and placeholders

SolidWorks names features itself, and the names are **localized** (`Düzlem1`, `Çizim3`). The compiler never guesses them:

- After each node, the last op's `cadState.features[0]` is recorded under the node id (for `create_sketch`, `cadState.activeSketch` wins).
- `node_feature(id)` placeholders (used by loft, sweep path, mirror, patterns, mates) are replaced with that recorded name → `NODE_FEATURE_UNAVAILABLE` if missing.
- `FROM_PREV_FEATURE` is replaced with the previous op's `features[0]` (an offset plane's or a pattern axis's name) → `PREV_FEATURE_UNAVAILABLE`.

### Sketch frame transform

If a node declares a `frame` and the op echoes the frame SolidWorks actually used (`result_geometry.frame` — snake_case, unlike the rest of the response), the compiler builds a 2D affine map from the original to the rebuilt frame and applies it to every `add_sketch_entity` coordinate pair and spline point; a mirrored frame also flips `arc_center.direction`. Missing echo → `FRAME_UNAVAILABLE`. `create_sketch` and `sheet_metal_feature(edge_flange_sketch)` echo frames.

## Lowering table

Tool names/params below are the execution layer's ([09-tool-reference.md](09-tool-reference.md)).

| Node | Ops |
|---|---|
| `sketch` | `create_sketch` on a datum plane; **or** on a face (`on_face:true, face_index` from the resolver); **or**, for an offset datum with no matching face, `add_reference_geometry{type:'plane', offset}` → `create_sketch{plane: FROM_PREV_FEATURE}`. Then one `add_sketch_entity` per primitive (no constraints — shared endpoints close the contour). |
| `extrude` | `extrude_feature{feature_type: boss\|cut}` + `depth` / `through:true` / `up_to_face_index` / `mid_plane:true`; `reversed` → `reverse:true` |
| `revolve` / `sweep` / `loft` | `extrude_feature{feature_type: revolve\|sweep\|loft}` (revolve angle in degrees + axis; sweep `path_sketch`; loft `profiles` as a JSON string of runtime names) |
| `rib` | `create_rib` |
| `box` | `create_sketch` → `add_sketch_entity{rectangle}` → `extrude_feature{boss}` |
| `hole` | `create_sketch{on_face, face_index}` → circle → `extrude_feature{cut, through:true}` |
| `fillet` / `chamfer` | `add_edge_feature{edge_indices: "[i,j]"}` (sorted, JSON string; chamfer angle in degrees) |
| `linear_pattern` | `create_pattern{linear, feature_name, direction X/Y/Z, spacing, count}` |
| `circular_pattern` | `add_reference_geometry{type:'axis', two planes}` → `create_pattern{circular, axis_name: FROM_PREV_FEATURE}` |
| `mirror` | `create_pattern{mirror, features_json, plane}` |
| `sheet_metal` | `sheet_metal_feature{base_flange}` |
| `sketched_bend` | `sheet_metal_feature{sketched_bend, angle°, fixed_x/y/z, …}` (`fixed.near` passes straight through) |
| `edge_flange` | mode B: one `sheet_metal_feature{edge_flange}`; mode A: `edge_flange_sketch` → profile entities (frame-transformed) → `edge_flange_finish` |
| `component` | `insert_component{file_path, transform_json, fixed, config?}` |
| `mate` | `add_mate{mate_type, a_/b_component, a_/b_kind, a_/b_index, alignment?, flip?, value?}` |
| `material` | `set_part_material` |

Through-all cuts don't compute `reverse`; they rely on the execution layer's flipped-direction auto-retry.

## Reference resolver

Only reads `analyze_model(faces|edges)` and `analyze_assembly(faces|edges, component)`; the payload is a JSON string in `cadState.features[0]`. A failed or unparseable read → `REFERENCE_UNRESOLVED`.

| Reference | Resolution | Tolerance |
|---|---|---|
| `selector:'top'` | planar faces with normal·(+Y) ≥ 0.9, highest `point.y` wins; tie → AMBIGUOUS | tie 1e-6 |
| `position:'center'` | always sketch (0, 0) | — |
| offset datum | an existing planar face parallel to the datum at that offset (largest area wins); none → create a reference plane | normal dot 0.999, offset 1e-5 |
| face anchor `near` | planar faces whose plane contains `near`; several distinct planes → AMBIGUOUS | 1e-5 m |
| edge anchor `near` | exactly one edge `mid` within the ball; 0 → UNRESOLVED, > 1 or two anchors on one edge → AMBIGUOUS | 1e-4 m |
| mate anchor | `plane` (containment, optional `dir`), `cylinder` (distance to axis = radius), `line`/`circle` (edge mid); `cone`/`sphere` → always UNRESOLVED | 1e-5 / 1e-4 |

## Tests

`pycompiler/tests/test_compiler.py` (52 tests) runs offline against `FakePort`, which serves canned faces/edges, returns **localized** feature names to prove substitution works, echoes sketch frames, and can inject a failure on the nth call of a tool (`fail_on=(tool, n)`). Areas covered: validation rejections per vocabulary batch, sketch/extrude paths, offset planes, resolver ambiguity, fillet/chamfer, loft/sweep, frame transforms (including mirror), patterns, sheet metal (base flange, sketched bend, edge flange both modes), assemblies (anchors, mates, cone/sphere gaps), material, partial progress on failure.

```bash
cd compiler/solidworks && python -m pycompiler.tests.test_compiler
pytest compiler/solidworks/pycompiler/tests/test_compiler.py -k chamfer
```
