# 08 · Analysis Pipeline & Round-Trip Verification

How an existing part becomes a trusted Feature Graph IR, and how a newly authored IR is checked. Motto (IR-ADR-006): **"the LLM proposes, the round-trip decides."**

## Reverse round-trip (existing part → IR)

```
part.sldprt ──save_analysis──> .solidpilot/part.sldprt.analysis.json   (recipe, parameters, ir: null)
                 │
  model reads artifact + get_recipe('mapping*') ──writes──> ir.graph
                 │
        rebuild_from_ir(artifact) ──> fresh document built by pycompiler
                 │
        compare_parts(original, rebuilt) ──> verified / failed ──> model writes ir.verification
```

1. **`save_analysis(file_path)`** writes the artifact (schema: `cad-planner/contracts/analysis-artifact.schema.json`).
2. The model maps the artifact's `recipe` to IR following `recipe-usage.md`: `contract`, `canonicalization` (rules C1–C7), `mapping`, `mapping_part`, `mapping_sheet_metal`, `mapping_assembly`.
3. **`rebuild_from_ir`** builds it in a fresh document through the same compiler as every other IR door.
4. **`compare_parts`** / **`compare_assemblies`** diff the original and the rebuild objectively.
5. The outcome goes into `ir.verification`. **Only `verified` IR may be reused** for rebuilds, variants or pattern matching.

## The analysis artifact

Location: `<folder>/.solidpilot/<source filename>.analysis.json`. It's a **cache, never the truth**: `identity.source_hash` (`sha256:` of the file bytes) and `identity.schema_version` are required; a hash mismatch means stale (re-analyze), and a schema change means regenerate, never migrate.

| Field | Content |
|---|---|
| `identity` | `source_path`, `source_filename`, `source_hash`, `source_mtime`, `schema_version`, `analyzed_at`, `generator {kind, model?, recipe_version?}` |
| `document_type` | `part` / `drawing` / assembly |
| `recipe` | parts: `features[]` in **exact tree order**, `mass_properties`, `geometry` counts, `bbox`, `material`, `equations`; assemblies: components, mates, mass |
| `parameters[]` | every named dimension (`name@feature`): `name`, `value_si`, `feature`, `kind` |
| `ir` | `{ir_schema_version, graph, generator, verification {status: verified\|unverified\|failed, method, verified_at, detail}}` |
| `relationships` | linked drawings (same stem), model, category |

`rebuild_from_ir` re-hashes the source and appends `source_stale=true` when it changed (informational; the rebuild still runs).

Drawing analyses from `analyze_drawing` are a different format (the draw dialect) and are saved **next to the drawing** as `<stem>.analysis-v<version>.json`, not in `.solidpilot/`.

## What "verified" means

From the recipe's `verification` section:

- **Part:** body, face, edge and vertex counts **all exactly equal**, AND |ΔV| ≤ 1 %, AND |ΔA| ≤ 1 %.
- **Assembly:** component set exact (source + config + instance counts), every transform within tolerance (position ≤ 1 µm, rotation ≤ 1e-6), mate count and type multiset equal, |ΔV| ≤ 1 % and |ΔA| ≤ 1 %.
- Anything that built but misses → `failed`, `detail.reason = MISMATCH` + measured deltas. Couldn't build or map → `BUILD_FAILED`, `VOCABULARY_GAP` (name the missing type), `RESOLVER_GAP`.

`compare_parts(..., detail='faces')` helps localize a mismatch: it lists faces missing from or extra in the rebuild (planes matched by normal + offset, cylinders by axis + radius, 50 µm tolerance).

**Blind spots:** extrude `reverse`, sheet-metal thickening direction and a skipped bend can leave volume, area and topology unchanged. That's why canonicalization rule C7 requires copying the original's stored direction/selection flags instead of inferring them from the geometry.

## Forward authoring (design intent → IR)

`submit_feature_graph(graph)` has no original to compare against and no hash to go stale. The recipe's `forward` section requires the model to **compute the expected outcome first** (e.g. volume) and check it against `analyze_model(mass_properties)` after the build.

## Drawing → part

`analyze_drawing` either builds a sheet-metal flat pattern directly (and reports the expected volume = blank area × thickness to check against), or returns the full analysis for the model to read under `get_recipe('reverse')` and author IR through `submit_feature_graph`. Production drawings are verified against the weight in their own title block. See [07](07-drawing-reader.md).

## STEP → feature-based part

`convert_to_features(file_path, output_path?)` turns a "dumb" imported body (STEP/IGES, or a `.sldprt` whose tree holds only the import — `MBimport`/`BaseBody`) into a feature tree, deterministically:

1. **Read** — `analyze_model(brep)`: faces (type, surface params, outward normal), edges (line/circle data, `pmid`, the two adjacent faces, convex/concave/tangent). Loops are rebuilt in Python from adjacency (walking them over COM cost ~0.4 s/face).
2. **Recognize** — `cad-planner/recognition` (stdlib only): merge the import's split half-faces; detect blends (fillet = tangent cylinder/torus between non-parallel supports; chamfer = narrow convex plane, or a coaxial cone on turned parts) and the sharp edge each replaced; classify **prismatic** (2.5D along X/Y/Z) or **turned** (coaxial surfaces ≥ 80 % of the lateral area); refuse `MULTIBODY`, `UNSUPPORTED_GEOMETRY`, `NOT_AXIS_ALIGNED`.
3. **Decompose** — prismatic: base boss = largest cap silhouette; every lower floor → cut down from the top; every raised ceiling → cut up from the bottom; unexplained inner loops of the extreme caps → through-all cuts; cross holes → mid-plane circle cuts. Turned: ONE revolve of the sharp-cornered half-section on the datum containing the axis, then hole cuts. Edge blends come back as `chamfer`/`fillet` nodes anchored on the sharp edge (straight: its midpoint; revolve circles: centre − r·N, the live-calibrated mid rule).
4. **Build** through `_run_graph` (fresh part); a node that fails is dropped (with its sketch) and the rest continues → the result is UNVERIFIED and lists `dropped=`. A failing base feature is `BUILD_FAILED` (nothing saved). An existing output is refused (`OUTPUT_EXISTS`) unless `overwrite=True`; the input is never the output.
5. **Verify like with like** — a STEP import splits every cylinder in two and a native rebuild does not, so the rebuild is exported to STEP, re-imported and `compare_parts`-ed against the original import under the strict definition above — **plus** position checks the counts/volume/area criteria lack (a hole mirrored to the wrong side keeps all of them): every surface must pair in the face diff and the centre of gravity may move at most 10 µm. VERIFIED also requires no dropped node and no unexplained face; anything else is saved and labelled UNVERIFIED. The artifact (`.solidpilot/<output>.analysis.json`) records the IR and `ir.verification` (`failed` + `MISMATCH`/`BUILD_FAILED` on a miss).

Calibrated facts it relies on (SW 2025): datum frames front (X, Y) / top (X, −Z) / right (−Z, Y); a blind boss runs along the datum +normal, a blind cut along −normal; cones narrow along +axis; a forward-slash STEP path fails `LoadFile4` (inputs are normalized). Offline fixtures are real import dumps — see `cad-planner/recognition/tests/record_fixtures.md`.

## Coverage reporting

Batch runs end with `parts_total / verified_without_ai / verified_with_ai / unverified / failed`, plus a ranked list of missing vocabulary drawn from the failures (recipe `coverage` section). Growing the IR vocabulary from these real gaps is how the project advances.
