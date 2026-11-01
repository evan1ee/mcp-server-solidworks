# 07 · Drawing Reader (`cad-planner/drawing/`)

Reads a 2D manufacturing drawing (**.DXF**, or **.DWG** converted to DXF) and either builds the part directly or returns a structured analysis for the model to interpret. Reached through the `analyze_drawing` MCP tool. Analysis version: `ANALYSIS_VERSION` in `dxf_read.py` (0.7.0 at the time of writing; the version history is in the comment beside it).

```
.dwg ──(SolidWorks: open → export DXF → close)──> <stem>.dwg2dxf.dxf
.dxf ──> dxf_read.read() ──> "draw"-dialect artifact ──> lowering.assess()
                                                          ├─ NOT_DIRECT | reason → whole analysis to the model (recipe 'reverse')
                                                          └─ DIRECT_BUILDABLE → lower_flat_pattern() → IR → pycompiler
```

## Modules

| Module | Role |
|---|---|
| `dxf_read.py` | The **only** module that touches DXF/`ezdxf` (imported lazily, so tests and CI don't need it). `read(path, cfg)` runs the whole extraction. |
| `curvefit.py` | Collapses polyline "fans" (exploded curves) back into parametric ellipses under a residual gate. |
| `contour.py` | Chains primitives into closed loops (Tier A strict; Tier B outer-face fallback). |
| `pairing.py` | Pairs sheet-metal bend notes with bend lines. |
| `viewgraph.py` | Frame plausibility, view alignment, orthographic axis solving, section-view pairing. |
| `lowering.py` | `assess()` — the direct-build gate; `lower_flat_pattern()` — IR emission. |
| `wire.py` | Compact wire encoding of the artifact (`encode` / `decode`). |
| `vocab.py` | Closed vocabularies, mirrored by `contracts/draw-dialect.schema.json`. |
| `config.json` | Tunables (below). |

## What `read()` does, in order

1. Sheet scale = DIMLFAC of the first `SLD*` dimstyle (else 1.0).
2. Collect entities and text (including text rows inside INSERT blocks, re-joining per-glyph fragments). Count what can't be read (HATCH, SOLID, OLE2FRAME, geometry inside blocks) into `sheet.not_read`.
3. Cluster primitives by bounding-box proximity (`cluster_gap_mm`). The largest cluster is the **frame** only if it contains all the others; find the title-block band from its row rules.
4. Merge nested clusters into views; assign each view a scale from nearby `a : b` text.
5. Emit primitives in **view-local true mm** (`(raw − origin) × view scale`), dropping duplicates (the DWG route re-emits section geometry).
6. Classify edges by linetype: `HIDDEN*` → hidden, `PHANTOM*` → cut_line, `CENTER*` → center, else visible.
7. Collapse curve fans (`curvefit`), then chain contours (`contour`).
8. Resolve dimensions; sort text into bend notes, `notes`, `frame_notes` (title block, never dropped).
9. Pair bend notes; assign view roles (`view` / `annotation` / `frame_item`); compute alignment, `view_graph` and section pairs.

Artifact keys: `analysis_version`, `source {file, sha256, bytes, converted_from?}`, `config`, `sheet`, `frame`, `views[]` (`vid`, `paper_box`, `geom_box`, `size`, `geometry {lines, arcs, circles, ellipses}`, `loops`, `open_chains`, `role`, `scale_factor?`), `alignment[]`, `view_graph?`, `dimensions[]`, `bend_notes[]`, `notes[]`, `frame_notes[]`.

## Key algorithms

**Contour chaining — strict by design.** Endpoints are keyed on a `chain_eps_mm` grid, per edge class. A chain grows only through points where **exactly two** segments meet; at degree 1 or ≥ 3 it stops and the fragment is reported as an open chain. It never guesses, so the result doesn't depend on seed order. Loops get a shoelace area with exact arc/ellipse bulges; a loop's parent is the smallest larger loop containing it. **Tier B** (planar face walk by tangent angle) runs only when Tier A found no visible outer loop and there are no free ends; the direct-build gate ignores Tier B.

**Curve fit.** Degree-2 runs of lines whose chord turn is within (0.05°, `max_turn_deg`] with a consistent sign are fitted with a closed-form conic. Accepted only if every vertex **and every chord midpoint** is within `fit_eps_paper_mm × view scale` of the curve (the midpoint test is what keeps a hexagon a hexagon). Runs shorter than `min_segments` and arcs shorter than ~`min_sweep_deg` stay raw. Fitted records carry `n` and `fit`; native DXF ellipses don't.

**Dimensions — the printed string arbitrates.** Value = measurement × view scale (angular never scaled). The printed text is read from the dimension's block MTEXT (`%%c`→Ø, `%%d`→°, `%%p`→±). Radius/diameter: matched to the nearest round primitive, then the printed value wins. Angular: take the non-reflex candidate (a DWG round trip corrupts angles). Linear: keep the computed value, flag `printed_mismatch` if off by more than max(0.05, 1 %). A linear dimension whose override starts with `%%c` becomes `kind:"diameter"`, `kind_from:"printed"`.

**Bend-note pairing — three channels.** Notes match `bend_note_pattern` (`UP|DOWN <angle> R <radius>`). A candidate line must (1) have the edge class for that direction (UP = visible, DOWN = hidden), (2) be parallel to the note baseline, on its negative-up side, within `bend_note_max_offset_mm`, and (3) be the nearest, each line used once. A rival within `chain_eps_mm` of the same distance → `unpaired: ambiguous`; none → `no_candidate`. (This replaced "nearest parallel line", which once paired a note to the blank's outline.)

**View graph.** Alignment between views is graded `span` (both edges coincide), `mid` (midpoints coincide), or `label` (section caption `A-A`, also SECTION/SCHNITT/COUPE/KESİT). `solve_view_graph` merges (view, axis) slots into exactly three anonymous axes or refuses (`axes_ambiguous`, `inconsistent`, `axes_unmerged`). Only computed when there are no bend notes.

## The direct-build gate (`lowering.assess`)

v1 builds **sheet-metal flat patterns only**. Checks, in order (first failure is the `reason`):

| # | Check | Reason on failure |
|---|---|---|
| 1 | there are bend notes | `no_bend_notes` |
| 2 | they're all in one known view | `bend_notes_split` |
| 3 | no cut line in the flat view | `cut_line_in_flat_view` |
| 4 | exactly one visible Tier-A outer loop | `no_outer_loop` / `multiple_outer_loops` |
| 5 | every other loop is a cutout of that outer | `stray_loop` |
| 6 | at least one bend paired (unpaired ones are skipped and reported) | `no_bend_paired` |
| 7 | every open-chain segment is a bend line | `unclaimed_open_chain` |
| 8 | exactly one thickness (from a side view whose dimension equals its short side, or a bare `N mm` note) | `thickness_unresolved` / `thickness_ambiguous` |
| 9 | the outer-loop centroid is inside the blank, outside cutouts, ≥ max(1 mm, 2t) from bend lines | `fixed_point_ambiguous` |

On success the summary carries the blank (size, area, cutouts), thickness and its source, bends / skipped bends, k-factor, `expected.volume_m3` (area × t) and the fixed point.

**Emitted IR** (`lower_flat_pattern`): `sketch` (outer + cutouts, on `front`) → `sheet_metal` (thickness, min bend radius, k-factor) → one `sketch` + `sketched_bend` pair **per direction group**, outer-most group first, DOWN before UP on ties, `flip` from `bend_flip_map`. Coordinates are converted to meters and re-centred on the blank. Ellipse arcs become splines (the IR has no partial ellipse).

## `analyze_drawing` modes

| Mode | Gate passes | Gate fails |
|---|---|---|
| `auto` (default) | `DIRECT_BUILDABLE` + compact summary + advisories | `NOT_DIRECT \| reason \| detail` + **the whole analysis** |
| `ir` | the IR JSON | `FAILED \| NOT_DIRECTLY_BUILDABLE` |
| `build` | builds via `_run_graph` (same compiler as `submit_feature_graph`) and appends the expected volume to check against; skipped bends are listed as NOT BUILT (they don't show up in volume) | `FAILED \| NOT_DIRECTLY_BUILDABLE` |

With `save_analysis=True` the wire-encoded artifact is also written next to the source as `<stem>.analysis-v<version>.json`. Advisories (scale, `not_read`, printed mismatches, Tier B, unpaired bends) are computed in the adapter so the saved artifact stays pure data. When the gate fails, the model is expected to read the analysis under `get_recipe('reverse')` and author IR itself.

**Wire format:** `views[].geometry.lines` becomes `{class: [[index, x1, y1, x2, y2], …]}` and single-segment open chains collapse into `open_singles`. `decode(encode(x)) == x`; `encode` doesn't mutate its input.

## `config.json`

| Key | Default | Meaning |
|---|---|---|
| `projection` | `first_angle` | read from config, not detected |
| `sheet_metal.k_factor` | 0.5 | |
| `bend_note_pattern` | `^(UP\|DOWN)\s+([0-9.]+)\s*(?:°\|deg)?\s*R\s*([0-9.]+)$` | case-insensitive |
| `bend_class_map` | UP=visible, DOWN=hidden | pairing channel 1 |
| `bend_flip_map` | UP=false, DOWN=true | `sketched_bend.flip` |
| `bend_note_max_offset_mm` | 25 | |
| `tolerance.cluster_gap_mm` | 1.0 | view clustering / frame containment |
| `tolerance.chain_eps_mm` | 0.01 | endpoint coincidence, pairing rivals |
| `curve_fit.fit_eps_paper_mm` | 0.03 | residual gate (scaled by view scale) |
| `curve_fit.min_segments` | 8 | |
| `curve_fit.max_turn_deg` | 40 | keeps chamfers (45°) and hexagons (60°) out |
| `curve_fit.min_sweep_deg` | 90 | shorter arcs mis-fit semi-axes by 2–9 % |

## Tests

`tests/test_draw_contract.py` runs offline with no `ezdxf`, on frozen draw-dialect fixtures:

1. Vocabulary drift: `draw-dialect.schema.json` `covered.*` ⇔ `vocab.py`, both directions.
2. Angular dimension arbitration (7 real cases).
3. Re-chaining every fixture reproduces the stored loops/open chains exactly.
4. Re-pairing bends reproduces the stored pairs.
5. Gate verdict and IR node order per fixture: `s1_flat` (4 bends, 6 nodes), `s2_flat` (2, 4), `ambiguous_bend` (1 built + 1 skipped), `f2_ortho` → `no_bend_notes`, `open_contour` → `no_outer_loop`, `no_thickness` → `thickness_unresolved`.
6. s-1 absolute numbers (blank area 9285.312 mm²).
7. View graph cases; 8. curve-fit synthetic cases; 9. wire round-trip.
10. **The lowered IR validates against `pycompiler.ir_schema`** — the reader can't invent vocabulary the compiler can't build.

```bash
cd cad-planner/drawing && python -m tests.test_draw_contract
```

## Known limits

- Ortho views chain only partly: a silhouette ending mid-edge is a T-junction, so the chain stops (f-2's front view closes 1 loop of 15). Machined parts therefore go through the model, not the direct path.
- Direct build covers flat patterns only (plain plates are deliberately excluded).
- Rotated bend notes have only been seen at rotation 0.
- Section-arrow apparatus on borderless sheets inflates a view's measured size.
- Curve fitting recovers ellipses only; ellipse arcs become splines.
- A skipped bend is invisible to every volume check — always read the skipped list.
- `pairing.pair_bend_notes` converts back to paper units with the **sheet** scale; a flat view drawn at a different scale than the sheet is untested.
- `config.json` mentions a `k_factor_mismatch` flag that no code emits.
