# Plan: STEP → feature-based SolidWorks model

Status: Phase 1 partly done (see "Progress"). Scope: single-body **machined parts first**; sheet metal later; no assemblies.
Success criterion: `compare_parts` returns `verified` (topology exact, |ΔV| ≤ 1%, |ΔA| ≤ 1%) between the imported STEP and the rebuilt part.

## Approach

Work from the 3D faces, not from drawings. Pipeline:

```
STEP → open_document → analyze_model(faces/edges/bodies) → recognizer → Feature Graph IR
     → rebuild_from_ir → compare_parts → verified | partial + unmatched faces
```

Tracks: **A** deterministic recognizer (primary), **B** drawing/DXF route (sheet metal, spike), **C** LLM fallback for what A cannot explain. All end in the same rebuild + compare.

## Progress

| Item | State |
|---|---|
| Face selectors top/bottom/front/back/right/left (IR, resolver, schema, tests) | Done, offline-tested (commit 04d7829) |
| `analyze_model(faces)`: `kind`, `reversed`, cone/sphere/torus params | Written in C#; **not compiled or run** |
| Live check on an imported STEP body | Open (needs Windows + SolidWorks) |
| Face adjacency | Open |

## Phase 1 — Groundwork (remaining)

1. **Live check (you).** Build the execution layer; open a STEP; run `analyze_model(faces)`. Confirm `kind`, `reversed`, params look right. Gate for everything below.
2. **Adjacency.** Add per-face neighbor face indices (and per-edge adjacent faces, convex/concave) to analysis. Interop has no `IsSameAs`, so match by the existing geometry-JSON equality approach or by edge midpoint anchors. Needed for fillet and blend detection.
3. **Concavity of curved faces.** Use `reversed` + axis to classify cylinder as hole (concave) or boss (convex); confirm on a part with both.
4. **Imported-body quirks.** Check face/edge counts, body count, units (mm vs m) and tolerance on imported solids; record findings in this file.
5. **Fixtures.** Save the live face/edge JSON of 2–3 real parts under `cad-planner/` as frozen fixtures for offline recognizer tests.

Gate: fixtures committed; contract tests pass.

## Phase 2 — Recognizer (offline Python, tested on fixtures)

Order of recognition (each step removes the faces it explains):

1. Base solid: bounding planar-face loops → extrude or revolve (axis from cylinder/cone/torus faces).
2. Through and blind holes: concave coaxial cylinders (+ cone tip for drilled blind holes, counterbore/countersink as stacked cylinders).
3. Bosses and pockets: planar loops with convex/concave wall sets.
4. Fillets and chamfers: constant-radius torus/cylinder faces between neighbors; planar bevels.
5. Patterns and mirrors: repeated holes by spacing or angle → `linear_pattern`, `circular_pattern`, `mirror`.
6. Output: IR + **unmatched face list** and the share of faces explained.

Rules: deterministic; canonical feature order; never guess — unexplained faces are reported, not invented. The emitted IR is validated by the compiler's own validator.
Gate: fixtures reproduce expected IR; recognizer unit tests pass.

## Phase 3 — Tool

- `step_to_features(src, dst_dir?)`: import → analyze → recognize → rebuild → compare. Returns verdict, feature list, unmatched faces.
- Folder mode with per-file results.
- Register in `adapters/claude/server.py` and `execution/solidworks/contracts/tool-schemas.json` (contract test keeps them in sync).
- Add a `step` recipe section for Track C (LLM fills the gaps).

Gate: live run on the spike parts returns `verified`.

## Phase 4 — Hardening

- Sheet metal via flat-pattern DXF and `analyze_drawing(mode='build')`.
- Import diagnosis and clearer failure codes (current `OPEN_FAILED` can be misleading).
- Larger STEP set, coverage report, agreed pass rate.

## Risks

| Risk | Mitigation |
|---|---|
| Imported bodies expose less data than native parts | Phase 1 live check first |
| Many feature trees give the same solid | Canonical order; accept any tree that verifies |
| v0 anchors break on edits | Known limit; durable resolver is a separate roadmap item |
| Freeform surfaces | Partial result + unmatched list; Track C |
| No SolidWorks in the cloud environment | Recognizer is pure Python, tested offline; live checks on Windows |

## Decisions needed

- 2–3 representative STEP files for the spike and fixtures.
- Result of the Phase 1 live check.
