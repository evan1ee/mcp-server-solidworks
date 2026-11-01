# 04 · Feature Graph IR

The CAD-neutral intermediate representation the model writes and the compiler builds.

- **Schema + capability registry (one file):** `cad-planner/contracts/feature-graph.schema.json` (DRAFT, `0.7.x`). If a type isn't in it, it can't be built. The model reads it via `get_recipe('feature_graph_schema')` / `schema://feature-graph`.
- **Enforced by:** `compiler/solidworks/pycompiler/ir_schema.py` (`validate(graph) -> list[str]`).
- **Kept in sync by:** `pycompiler/tests/test_ir_schema_contract.py`.
- **Authoring rules for the model:** `cad-planner/recipe-usage.md` (`canonicalization`, `forward`, `mapping_*` sections).

## Graph shape

```json
{
  "schema_version": "0.7.2-draft",
  "units": "meters",
  "material": {"name": "AISI 304", "library": "…"},
  "_params": {"<dimension name>": {"node": "n3", "field": "depth", "value_si": 0.01}},
  "nodes": [
    {"id": "n1", "type": "sketch", "ref": {"datum": "top"}, "profile": [{"kind": "rectangle", "width": 0.1, "height": 0.05}]},
    {"id": "n2", "type": "extrude", "sketch": "n1", "operation": "boss", "end": "blind", "depth": 0.02}
  ]
}
```

| Field | Rule |
|---|---|
| `units` | Must be `"meters"`. Angles are **radians** everywhere except `circular_pattern.angle_deg`. Lowering converts to degrees at the tool boundary. |
| `nodes` | Non-empty. **Array order is build order** ("tree order is law"). Ids are unique; a node may only reference **earlier** nodes. |
| `material` | Optional, parts only; applied once after the last node (`set_part_material`). |
| `_params` | Optional side table that lifts named dimensions (canonicalization rule C3). The compiler ignores it. |
| `schema_version` | Documented as required but **not checked** by the validator. |

Part graphs (16 node types) and assembly graphs (`component`, `mate`) **cannot be mixed**. In an assembly, all `component`s come before all `mate`s; components reference part files by `source: {path, hash?}` and never embed a part's IR.

`backend_ext` (a backend-specific escape hatch) appears in the architecture docs only. Nothing in the schema or code implements it yet.

## Node types

| Type | Key params |
|---|---|
| `sketch` | `ref` = `{datum: front\|top\|right, offset?}` **or** `{face: {near:[x,y,z], hint?}}`; `profile[]`; optional `frame {origin, xdir, ydir}` |
| `extrude` | `sketch`, `operation` boss\|cut, `end` blind\|through_all\|up_to_surface\|mid_plane, `depth`, `reversed`, `up_to.face.near` |
| `revolve` | `sketch`, `axis {x1,y1,x2,y2}`, `angle` (rad, default 2π) — boss only |
| `sweep` | `sketch` (profile), `path` (an earlier, different sketch) — boss only |
| `loft` | `profiles[]` (≥ 2 earlier sketches) — boss only, no guide curves |
| `rib` | `sketch`, `thickness`, `two_sided`, `reverse_thickness_dir`, `reverse_material_dir`, `is_norm_to_sketch` |
| `box` | convenience: `ref.datum`, `width`, `depth`, `height` |
| `hole` | `ref.node_face {node, selector:'top'}`, `ref.position:'center'`, `diameter`, `depth:'through_all'` |
| `fillet` | `radius`, `edges[]` of `{near, hint?}` |
| `chamfer` | `chamfer_type` distance_angle\|distance_distance, `distance`, `angle` (rad), `distance2`, `flip`, `edges[]` |
| `linear_pattern` | `feature`, `direction` x\|y\|z, `spacing`, `count ≥ 2`, `flip` (one direction; compose a pattern of a pattern for grids) |
| `circular_pattern` | `feature`, `axis.datums` (two datums), `count ≥ 2`, `angle_deg` (0, 360], `equal_spacing` |
| `mirror` | `plane.datum`, `features[]` |
| `sheet_metal` | base flange: `sketch`, `thickness`, `bend_radius?`, `k_factor` ∈ (0,1), `reverse_thickness`, `symmetric_thickness` |
| `sketched_bend` | `sketch`, `angle` (rad), `radius?`, `position` centerline\|material_inside\|material_outside\|bend_outside, `flip`, `fixed.near` (required) |
| `edge_flange` | `edge.near`, `angle`; **mode B:** `length`; **mode A:** `frame`, `profile[]`, `radius?`, `position`, `flip` (modes are mutually exclusive) |
| `component` | `source {path, hash?}`, `transform` (exactly 13 numbers), `fixed`, `config` |
| `mate` | `mate_type` coincident\|concentric\|perpendicular\|parallel\|tangent\|distance\|angle\|lock, `alignment`, `value` (distance/angle), `flip`, `sides[2]` of `{component, anchor{kind, near, dir?, radius?, hint?}}` |

**Grammar:** `extrude`, `revolve`, `rib`, `sweep` (its profile), `sheet_metal` and `sketched_bend` consume the **immediately preceding** sketch node. Patterns/mirror take any earlier feature-producing node; `hole` needs a body-producing node (`box`, `extrude`, `revolve`, `sweep`).

**Profile primitives** (`profile[]`): `rectangle` (width, height; centred on the sketch origin), `circle` (diameter, cx, cy), `line`, `arc` (cx, cy, x1, y1, x2, y2, `dir` ±1), `ellipse` (centre + major-axis point + minor-axis point), `spline` (flat `points`, ≥ 4 numbers, even count). Any primitive may carry `construction: true`.

## References: how a node names geometry

This is the hardest part of the design (the persistent-naming problem). v0 has four mechanisms; see [05-compiler.md](05-compiler.md#reference-resolver) for how each resolves.

1. **Datum planes** — `front` / `top` / `right`, optionally with a signed `offset` in meters.
2. **Semantic selectors** — only `node_face.selector:'top'` (highest +Y planar face) and `position:'center'` (sketch origin). Both assume a single, origin-centred body.
3. **Geometric anchors** — `{near:[x,y,z], hint?}` matched against live faces/edges. **Exact for replay in a fresh document, fragile across edits.** `hint` is never read by the compiler; it's recorded as data for a future semantic model.
4. **Node-id references** — `sketch`, `path`, `profiles`, `feature`, `features`, `component`, resolved to the **runtime feature names** the execution layer returned (which may be localized, e.g. `Ekstrüzyon1`).

## Validation split

| When | Who | Checks |
|---|---|---|
| Plan time | `ir_schema.validate` | types registered, ids unique, refs point to earlier nodes of the right type, required params, enums, positivity, grammar adjacency, part/assembly not mixed |
| Compile/exec time | resolver + execution | the face/edge exists, the selector matches exactly one entity, the feature actually builds |

`validate` returns plain strings prefixed with a location (`nodes[3] (extrude 'n4'): …`); the compiler joins them into one `VALIDATION_FAILED` error and makes **zero** execution calls. In some places it is stricter than the schema text (`k_factor` open interval, `angle_deg ≤ 360`).

## The contract test

`test_ir_schema_contract.py` maps each of the 19 arrays under the schema's `covered_subset` to a frozenset in `ir_schema.py` and requires exact two-way equality: node types, assembly node types, profile kinds/flags, datums, face selectors, positions, hole depths, extrude ends, pattern directions, chamfer types, bend positions, edge-flange positions, mate types/alignments, face/edge anchor kinds, body producers, feature producers. Declared prose keys (`_note`, `_grammar_note`, `datum_offset`, `assumptions`) are exempt; a new unmapped key fails. Per-param types and ranges are **not** cross-checked — they're covered by `test_compiler.py`.

## Adding a node type

1. Add it to `feature-graph.schema.json` (`covered_subset` + its param documentation).
2. Add it to the matching frozensets and its param checks in `ir_schema.py`.
3. Add its lowering in `lowering.py` (and any resolution in `resolver.py`).
4. Add cases to `test_compiler.py` (lowering + validation rejections). The PostToolUse hook re-runs the IR contract, compiler and drawing tests.
5. Prove it on a real part with the round-trip ([08](08-analysis-and-verification.md)). The vocabulary is grown from real production parts, not designed ahead of them.

## Known limits

- Anchors are edit-fragile; mapping a general 3D point to 2D sketch coordinates on arbitrary faces is deferred.
- Face anchors resolve **planar** faces only; mate anchors of kind `cone`/`sphere` validate but always fail to resolve.
- `hole` supports only through-all / `top` / `center`.
- Splines rebuilt from through-points are visually equivalent, not bit-identical; the IR has no partial ellipse.
- Mirroring a bare sketched bend is refused by SolidWorks.
- Assemblies: no in-context features, flexible subassemblies, component patterns or extra configurations.
- Some code comments still say "v0.5 subset" / "26 low-level tools" — stale; the schema is the authority.
