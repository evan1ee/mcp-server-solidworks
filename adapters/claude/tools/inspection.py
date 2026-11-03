from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Any, Callable, Dict, Literal, Optional
from pydantic import Field
from mcp.server import MCPServer
from drawing_pipeline import (
    summarise_direct as _summarise_direct,
    advisories as _advisories,
    wire_json as _wire_json,
    format_direct_build_result as _format_direct_build_result,
)
from tools.common import CallCallable, CallRawCallable

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_CAD_PLANNER_PKG_DIR = os.path.normpath(os.path.join(_TOOLS_DIR, '..', '..', '..', 'cad-planner'))
if _CAD_PLANNER_PKG_DIR not in sys.path:
    sys.path.insert(0, _CAD_PLANNER_PKG_DIR)

try:
    import drawing as _draw
    _DRAWING_AVAILABLE = True
    _DXF_READER_IMPORT_ERROR = ""
except Exception as _exc:
    _draw = None
    _DRAWING_AVAILABLE = False
    _DXF_READER_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc} (pip install ezdxf)" 

_call: Optional[CallCallable] = None
_call_raw: Optional[CallRawCallable] = None
_run_graph: Optional[Callable] = None

# Tool: export_document
# ---------------------------------------------------------------------------
def export_document(format: Literal["STEP", "IGES", "STL", "PDF", "DWG", "DXF"], file_path: str) -> str:
    """Export the active SolidWorks document to a file. The document remains open after export.
    format: 'STEP', 'IGES', 'STL', 'PDF', 'DWG', 'DXF'.
    file_path: full output path including filename and extension (e.g. 'C:/output/part.step').
    Note: DWG and DXF require an active drawing document (use create_drawing first).
    PDF uses SolidWorks PDF export options. STEP/IGES/STL work with part or assembly documents.
    IGES must use a .igs extension (.iges is auto-corrected to .igs)."""
    # export does NOT change CAD state; the execution layer returns the same state_version.
    return _call("export_document", {"format": format, "file_path": file_path})


# ---------------------------------------------------------------------------
# Tool: batch_export
# ---------------------------------------------------------------------------
def batch_export(file_path_base: str, formats_json: str) -> str:
    """Export the active document to multiple formats in one call.
    file_path_base: output path without extension (e.g. 'C:/output/mypart').
    formats_json: JSON array of format strings (e.g. '[\"STEP\",\"STL\",\"PDF\"]').
    Each format is exported as file_path_base.<ext>. DWG/DXF skipped if active doc is not a drawing.
    Partial success is reported in the response Features list."""
    return _call("batch_export", {"file_path_base": file_path_base, "formats_json": formats_json})


# ---------------------------------------------------------------------------
# Tool: analyze_model
# ---------------------------------------------------------------------------
def analyze_model(
    analysis_type: Literal["mass_properties", "geometry", "bodies", "edges", "faces", "features", "sketch", "feature_map", "brep"],
    name: str = "",
    from_feature: str = "",
    to_feature: str = "",
    near: str = "",
    k: int = 0,
    axis: str = "",
) -> str:
    """Analyze the active SolidWorks part document (read-only, does NOT change state).
    analysis_type='mass_properties': returns volume, surface_area, center of gravity (cx, cy, cz) in document units.
    analysis_type='geometry': returns bodies, faces, edges, vertices counts of all solid bodies.
    analysis_type='bodies': PER-BODY fingerprints for multibody parts (e.g. a flattened assembly
        STEP import): volume, area, centroid, edge/vertex counts, and the body's face-index RANGE
        in the same enumeration 'faces' walks — segment the faces list per body with it.
    analysis_type='edges': returns a JSON object listing EVERY solid edge with its start/end/MIDPOINT 3D coords
        in meters (rounded to 6 decimals; closed/circular edges also report length) and a stable index `i`. Use
        the index `i` for add_edge_feature(edge_indices=...) — robust for crowded/concave edges — or a MIDPOINT
        for coordinate-based selection (e.g. edge_flange's ex/ey/ez). On a very large part (many hundreds of edges)
        this is the slowest mode; prefer 'features' for a general understanding. TARGETED (recommended when you
        already know roughly WHERE): pass near='[x,y,z]' (+ optional k, axis) to get only the k nearest edges to
        a point instead of the full dump — see near/k/axis below.
    analysis_type='faces': returns a JSON object listing EVERY solid face with a stable index `i`; planar faces
        also carry normal, area, and a representative on-plane point (meters, rounded to 6 decimals). Use the
        index `i` for create_sketch(on_face=True, face_index=i) — robust where a coordinate pick is ambiguous
        (e.g. a revolve end-cap / shaft end whose centre lies on the axis). TARGETED: pass near='[x,y,z]'
        (+ optional k, axis) for only the k nearest faces to a point — see near/k/axis below.
    analysis_type='brep': the FULL boundary representation of a SINGLE-body part (large; meant for the
        convert_to_features recognizer, rarely needed directly). Faces: type (plane/cylinder/cone/sphere/
        torus/other), surface params, OUTWARD normal at a representative point, and loops (outer/inner,
        edge indices). Edges: type (line/circle/other), start/end/mid (same mid as 'edges'), circle centre/
        axis/radius, the two adjacent face indices and convexity (convex/concave/tangent). A multibody
        part returns only {body_count}. Indices match the 'faces' and 'edges' modes.
    analysis_type='features': the part's COMPACT RECIPE — this is the default "understand the part" read. Returns
        the ordered feature tree (name/type; suppressed shown only when true), each feature's driving dimensions
        (deduped, SI meters/radians, rounded to 6 decimals), a per-sketch SUMMARY (segment counts + an inline
        {cx,cy,r} for single-full-circle profiles), pattern semantics (instances / spacing_deg / distinct_instances
        / wraps), and equations / globals. Each sketch reports its plane as {ref, offset} where ref is the
        canonical English default plane ('Front Plane'/'Top Plane'/'Right Plane') and offset is the signed
        distance along its normal (offset 0 = that default plane itself; offset != 0 = a parallel face/plane
        at that height — sketch on the face there). Each extrude/cut reports extrude:{end (blind/through_all/
        ...), depth (blind only), reversed (present only when the direction is flipped)} so you know HOW it
        was built; for direction use the sketch's plane.offset (offset != 0 ⇒ a face, so a cut goes into the
        part). Features are listed in tree (history) ORDER — reproduce them in that exact order (order
        matters wherever features overlap). It does NOT dump every sketch segment's coordinates — that keeps the payload small
        and is enough to understand the part and build dimension/pattern variants. Use this first.
    analysis_type='sketch' (requires name=...): ONE sketch's FULL geometry — every segment's coordinates (rounded
        to 6 decimals) plus its plane. Use this only when you actually need exact sketch geometry (e.g. to reproduce
        an irregular profile); get the sketch name from the 'features' read first. name = the sketch feature name,
        e.g. 'Sketch2'.
    analysis_type='feature_map': per-feature geometry ATTRIBUTION — deterministically answers "which edges/faces
        did each feature act on?" (e.g. WHICH edge a fillet/chamfer was applied to — never guess an anchor). Walks
        the tree base→end with the rollback bar and diffs the topology between stops INSIDE the tool; returns one
        compact JSON {feature_count, map:[{feature, type, delta:{faces,edges,vertices}, consumed_edges:[{mid,len}],
        created_faces:[{point, normal?, planar, area}]}]} (6-decimal meters). consumed_edges = edges that existed
        BEFORE the feature and were consumed by it (both endpoints gone; merely trimmed neighbours excluded) — use
        a consumed edge's `mid` as the fillet/chamfer anchor `near` point (it references the PRE-feature geometry,
        exactly what the IR anchor needs). created_faces = genuinely new surfaces (trimmed existing planes excluded).
        Sketches/planes are listed without a delta; suppressed features are skipped. Optional from_feature/to_feature
        (tree names) limit the walk to a range. NON-DESTRUCTIVE: the rollback bar is restored to the end, nothing is
        saved — but it does rebuild the model feature-by-feature, so it is the slowest mode on big trees.

    near / k / axis (edges and faces modes ONLY — a TARGETED read instead of the full dump): when you already
        know approximately where the geometry is (the common build-time case — you almost always do), pass
        near='[x,y,z]' (a JSON string, meters) to return only the nearest entities to that point, each annotated
        with `dist` (meters) and still carrying its STABLE full-enumeration index `i` (so selection is unchanged).
        k (default 0 = no cap): keep only the k nearest. axis='[x,y,z]' (optional JSON string): keep only entities
        whose direction is ~parallel to axis — for faces that's the planar-face NORMAL (e.g. axis='[0,0,1]' → only
        faces on +Z planes), for edges the chord direction. The response adds total_edge_count/total_face_count so
        you know how many were filtered out. Omitting near preserves the exact historical full-dump behavior.
    Does NOT increment state_version."""
    return _call("analyze_model", {"analysis_type": analysis_type, "name": name,
                                   "from_feature": from_feature, "to_feature": to_feature,
                                   "near": near, "k": k, "axis": axis})


# ---------------------------------------------------------------------------
# Tool: analyze_drawing  (2026-07-27 — the SHIPPING drawing reader: DXF/DWG)
# ---------------------------------------------------------------------------
# The reader is a permanent module of the CAD-neutral planner layer (ADR-064): a drawing is design
# intent expressed in 2D, so it belongs beside the IR schema and the recipe, not in a research dir.
_CAD_PLANNER_PKG_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "cad-planner"))

# Imported EAGERLY, at startup, in the MAIN thread -- deliberately, and NOT lazily inside the tool
# like pycompiler is. ezdxf pulls in numpy, and loading numpy's C extensions from an MCP worker
# thread DEADLOCKS on Windows: the tool hung forever with no error and no traceback (diagnosed by
# faulthandler thread dump, 2026-07-27 -- stuck in importlib create_module for numpy._core.
# multiarray). A failure here must still never kill the server, so it degrades to a clean per-call
# error instead.
try:
    if _CAD_PLANNER_PKG_DIR not in sys.path:
        sys.path.insert(0, _CAD_PLANNER_PKG_DIR)
    import drawing as _draw
    _DXF_READER_IMPORT_ERROR = None
except Exception as _exc:                     # noqa: BLE001 - startup must survive anything
    _draw = None
    _DXF_READER_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc} (pip install ezdxf)"

# The STEP→features recognizer (cad-planner/recognition): stdlib-only, so a plain import — kept next
# to `drawing` and equally non-fatal.
try:
    import recognition as _recognition
    _RECOGNITION_IMPORT_ERROR = None
except Exception as _exc:                     # noqa: BLE001 - startup must survive anything
    _recognition = None
    _RECOGNITION_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"


def analyze_drawing(file_path: str, save_analysis: bool = True,
                    mode: Literal["auto", "ir", "build"] = "auto") -> str:
    """Read a 2D technical drawing — **.DXF or .DWG** — and either BUILD the part from it or hand
    you the evidence to build it yourself. This is the drawing→part front end: a real drawing
    arrives as DXF/DWG (a .SLDDRW is model-linked and nobody ships one), so start here for ANY
    "build the part from this drawing" job. A .DWG is converted automatically.

    **mode='auto' (default) returns ONE OF TWO SHAPES — the reader decides, not you:**

    (A) `DIRECT_BUILDABLE` — a short plain-text summary. Every decision this drawing needs is
        forced by the drawing itself (today: sheet-metal flat patterns), so the whole part has
        already been lowered to Feature Graph IR deterministically. You get the blank size, the
        thickness and its source, every bend with its edge-class corroboration, and the EXPECTED
        VOLUME — enough to judge the build — but NOT the contour, on purpose: a real outline is
        scores of segments and echoing it back would cost the tokens twice. Read it, then call
        `mode='build'`. Anything the reader could not attribute is listed as SKIPPED and will not
        be built; completing or declaring it is then yours (recipe R15).

    (B) `NOT_DIRECT | <reason>` followed by the WHOLE analysis — the drawing needs real reading
        (the normal path for machined parts); the reason names exactly which decision the drawing
        left open. You get sheet/frame, every view complete (boxes, size, geometry, loops with
        their `seq`, open chains), alignment, view_graph, every dimension, bend note and note, in
        one answer. Read it per get_recipe('reverse') and build with submit_feature_graph.

    mode='ir'    — the lowered Feature Graph IR, without building. For inspection or saving.
    mode='build' — lower and BUILD, through the same deterministic pycompiler as
                   submit_feature_graph. **CHANGES GEOMETRY and bumps state_version.** Opens a new
                   part. Re-derives from the same file, so it needs no cached graph — but verify
                   the sha256 in the summary is the file you looked at.

    ⮕ Before interpreting a single number below, call `get_recipe(section='reverse')` — the TOOL,
      not the `recipe://usage/reverse` resource, which most hosts give the model no way to read
      (ADR-077). It holds the reading discipline (projection standard, face binding, contour gaps,
      sheet-metal bend arithmetic) and the self-verification rules. When you then need the IR
      vocabulary, `get_recipe('feature_graph_schema', profile='part')` is the right shape for this
      job — it drops the assembly vocabulary and keeps every part and sheet-metal feature type.

    The artifact — WHAT EVERY FIELD MEANS. (The DECISIONS you make with them are the recipe's;
    read get_recipe('reverse') for those. Nothing is documented in both places.)
      sheet — dxf_version, units, **scale_factor** and the sheet box. `scale_factor` is ALREADY
        applied to every size/coordinate/dimension below, so all numbers are TRUE model mm and
        multiplying again builds a half- or double-size part. An ANGLE is never scaled (DIMLFAC is
        a length factor).
        `not_read` counts what the reader saw and did NOT emit, by entity class and — for an
        INSERT — by block name, e.g. {"HATCH": 5, "INSERT": {"WALZRICHTUNG": 1}}. Nothing vanishes
        silently: a named block you cannot account for is evidence that exists on the sheet and
        did not reach you.
      frame — {paper_box, title_block_top, primitives, circles[], note_count}: the border/title
        block cluster, which is never emitted as a view. A candidate is accepted ONLY when it
        CONTAINS every other cluster, so `paper_box: null` means this sheet has no frame and every
        cluster was a view candidate. A cluster in the title-block corner that no dimension points
        at arrives as `role: "frame_item"` (a projection symbol, a weld symbol) — furniture.
      frame_notes[] / notes[] — the sheet's text in reading order, `frame_notes` being the title
        block's. Text INSIDE inserted blocks (SW_NOTE, BIEGETEILE, a company stamp) is included,
        one entry per ROW, nested to any depth. The DWG route splits a row into per-glyph
        fragments that are re-joined left to right, so match on content, not on exact spacing.
      views[] — one per detected view: `vid`, `role`, `paper_box` (sheet coords, for reading the
        LAYOUT), `geom_box`, `size` [w,h] in true mm, `dropped_duplicates`? (exact duplicates the
        DWG route re-emitted), `scale_factor`? — PRESENT ONLY when this view is drawn at a ratio
        DIFFERENT from the sheet's (a detail or section labelled 'A-A 1 : 1' on a 1:10 sheet); its
        geometry and dimensions are already scaled with THAT factor.
        Then `geometry` {lines, arcs, circles, ellipses} in view-local
        true mm with the view's own bottom-left as origin. The EDGE CLASS is carried on every
        primitive as `c`: 'visible' (near-side edge) | 'hidden' (obscured — a real feature seen
        through material) | 'cut_line' (a section's cutting line, not part geometry) | 'center'
        (an axis).
        `lines` is the one array grouped BY that class instead of carrying it per record:
        {"visible": [[index, x1, y1, x2, y2], …], "hidden": […]}. THE FIRST NUMBER IS THE INDEX —
        the one a `seq` entry names — written in rather than implied by position, so ['l', 12, 1]
        is found by looking for the row starting with 12, in whichever group, and never by
        recomputing an offset. Indices run 0..n-1 across all the groups together.
        `ellipses` {cx, cy, rx, ry, rot (+ t1, t2, x1..y2 for an arc)} are PARAMETRIC — a circle
        on a plane tilted by θ projects with ry/rx = cos θ and rx its true radius, so a Ø10 hole
        on a 20° face arrives as rx 5 / ry 4.699, not as 48 line segments. A record carrying
        `n` + `fit` was recovered from an exploded fan of n segments (fit = max deviation, mm);
        one without is a real DXF ellipse.
        The sheet border/title block is NOT a view; a cluster INSIDE a view is that view's feature.
      views[].loops[] — the CLOSED contours, already chained: {id, class, role outer|inner, parent,
        area (mm^2, arc bulges exact), bbox, seq, tier?}. `seq` is [[code, index, dir], …]
        referencing that view's own geometry arrays — the contour IN ORDER, ready to transcribe as
        a sketch path profile. Traverse it as given; consecutive entries share an endpoint exactly.
        `tier` is PRESENT ONLY when it is "B", so the common case costs nothing. Absent = Tier A,
        strict chaining, which stops dead at any junction of three or more and therefore never
        guesses. "B" = planar face traversal, run ONLY on a view whose visible graph has no free
        end — with no dangling edge the figure is a closed planar subdivision and its faces are
        DEFINED by the angular order of the edges at each vertex, so walking it is a reading. It
        appears only where Tier A found nothing, and the direct-build path ignores it by design.
      views[].open_singles[] — [position, code, index] triples: the SINGLE-segment open chains,
        which are most of them. Nothing is abbreviated away — the id is 'O<position>', the class is
        that primitive's own `c`, the direction is 1 — and a chain whose id/class/direction are not
        all recoverable stays a full record in `open_chains` instead. Read the two together: a
        primitive named here is exactly as unattributed as one in `open_chains`.
      views[].open_chains[] — the MULTI-segment chains belonging to no contour: bend lines, centre
        lines, and (in an ortho view) silhouette fragments the chainer refused to guess through
        a T-junction.
      alignment[] — GRADED view pairs: shares='x' is a vertical projection pair, 'y' a horizontal
        one, and `grade` says how strong the evidence is. 'span' = the two share that axis's span
        edge-for-edge, the strongest. 'mid' = only the midpoints coincide — still a measurement,
        but silhouette ends may genuinely differ, and on a bend-note sheet a mid pair may relate
        the FLAT and the BENT state (two states of the part, not two projections). 'label' = a
        section caption ties the section view `b` to the view `a` carrying its cut line, with
        shares=null, so it survives a cross-scale section.
      view_graph — {solved, views:{vid: [h_axis, v_axis]}} when the pairs pin every view's two
        paper axes onto the part's three. TOPOLOGY ONLY, over ANONYMOUS axes ax0/ax1/ax2: which
        axis is depth and which view is "front" is the projection convention's SIGN, not a field.
        `solved: false` names why, and the joining is yours again.
      dimensions[] — `value` (TRUE), `kind` (linear|aligned|angular|diameter|radius|ordinate),
        `defpts` (the dimension's own reference points, sheet coords) and the owning `view`.
        `printed` is the string the CAD system actually DREW, taken from the dimension's own
        block, with `%%c`→Ø, `%%d`→°, `%%p`→± decoded (the symbol is evidence about what was
        dimensioned); `text` beside it is the raw undecoded override (e.g. '8x <>' = a count).
        The DWG→DXF route corrupts the stored measurement in two whole classes — an ANGULAR value
        comes back as 180+θ, a RADIUS loses its arc side — so the reader ARBITRATES: for radius and
        diameter the printed value wins outright, and for an angle it takes the candidate ≤ 180°.
        `printed_mismatch: true` marks what it could not settle.
        `kind_from: "printed"` marks a dimension whose TYPE the drawn symbol overruled — a plain
        linear dim drawn across a circle with a hand-typed Ø is re-emitted as `kind: "diameter"`.
        `measures` links a dimension to the primitive the reader matched it to.
      bend_notes[] — sheet metal: {dir UP|DOWN, angle_deg, radius, view} plus either `bend_line`
        (the line it annotates, that line's edge class, and `in_loop` — true would mean the match
        landed on outline geometry, which a bend line never is) or `unpaired` with the reason and
        the candidates. Direction comes from the NOTE; the class corroborates it.
      notes[] — other free text on the sheet (e.g. a bare '2 mm' thickness note, 'SECTION C-C'),
        text inside note blocks included.

    file_path: absolute path to the .DXF or .DWG.
    save_analysis (default True): also write `<name>.analysis-v<version>.json` beside the source —
        a research artifact for cross-part study, versioned by the analyzer. Same version
        overwrites. You never need to read it back: this answer already carries everything in it."""
    src = os.path.abspath(file_path)
    if not os.path.exists(src):
        return f"FAILED | FILE_NOT_FOUND | {src}"
    ext = os.path.splitext(src)[1].lower()
    if ext not in (".dxf", ".dwg"):
        return ("FAILED | UNSUPPORTED_TYPE | analyze_drawing reads .DXF or .DWG. "
                "A native .SLDDRW is a test-only path (analyze_slddrw_test).")
    # DWG is binary; the reader parses DXF only. SolidWorks opens the DWG as a drawing and exports
    # DXF — measured lossless for geometry, dimensions, DIMLFAC, linetypes and notes.
    dxf_path = src if ext == ".dxf" else os.path.splitext(src)[0] + ".dwg2dxf.dxf"

    def _load():
        """Convert (DWG) + read + save -> (art, cfg, None) | (None, None, error string)."""
        converted_from = None
        if ext == ".dwg":
            opened = _call_raw("open_document", {"file_path": src})
            if opened.get("status") != "COMPLETED":
                err = opened.get("error") or {}
                return None, None, f"FAILED | DWG_OPEN_FAILED | {err.get('code')}: {err.get('message')}"
            exported = _call_raw("export_document", {"format": "DXF", "file_path": dxf_path})
            _call_raw("close_document", {})      # the import is scratch — discard, never save
            if exported.get("status") != "COMPLETED":
                err = exported.get("error") or {}
                return None, None, f"FAILED | DWG_CONVERT_FAILED | {err.get('code')}: {err.get('message')}"
            converted_from = os.path.basename(src)
        if _draw is None:
            return None, None, f"FAILED | READER_UNAVAILABLE | {_DXF_READER_IMPORT_ERROR}"
        try:
            cfg = _draw.load_config()
            art = _draw.read(dxf_path, cfg)
        except Exception as exc:
            return None, None, f"FAILED | DXF_READ_FAILED | {type(exc).__name__}: {exc}"
        if converted_from:
            art["source"]["converted_from"] = converted_from
        if save_analysis:
            out = os.path.join(os.path.dirname(dxf_path),
                               "%s.analysis-v%s.json" % (os.path.splitext(os.path.basename(src))[0],
                                                         art["analysis_version"]))
            try:
                with open(out, "w", encoding="utf-8") as fh:
                    json.dump(_draw.wire_encode(art), fh, indent=1, ensure_ascii=False)
            except OSError:
                pass                              # the artifact is research-only, never the result
        return art, cfg, None

    art, cfg, err = _load()
    if err:
        return err
    full = _wire_json(_draw.wire_encode(art))

    # The GATE decides the result shape. The model must never be asked to choose between "give me
    # the analysis" and "just build it" BEFORE it has seen any geometry — that was the flaw in
    # making this an opt-in flag (ADR-064).
    try:
        verdict = _draw.assess(art, cfg)
    except Exception as exc:                      # noqa: BLE001 - a gate bug must not lose the read
        return f"NOT_DIRECT | GATE_ERROR | {type(exc).__name__}: {exc}\n{full}"

    if not verdict["direct"]:
        if mode in ("ir", "build"):
            return (f"FAILED | NOT_DIRECTLY_BUILDABLE | {verdict['reason']} | {verdict['detail']} — "
                    f"call analyze_drawing(mode='auto') and build with submit_feature_graph.")
        # ONE SHAPE, the whole analysis (payload phase 1, 2026-09-07). This used to be a SUMMARY
        # with the geometry pulled back per view, and the split was retired on its own measurement:
        # once the wire form landed, the complete artifact came out smaller on all 10 samples than
        # the summary plus the pulls it made necessary (f-3: 14.0 KB against 28.7).
        return (f"NOT_DIRECT | {verdict['reason']} | {verdict['detail']}\n"
                f"The whole analysis is below. Read it per get_recipe(section='reverse') and build "
                f"with submit_feature_graph.{_advisories(art)}\n{full}")

    try:
        graph = _draw.lower_flat_pattern(art, cfg, verdict)
    except Exception as exc:                      # noqa: BLE001
        return f"NOT_DIRECT | LOWERING_ERROR | {type(exc).__name__}: {exc}\n{full}"

    if mode == "ir":
        return _wire_json(graph)
    if mode == "auto":
        # The direct path reports its own skipped bends; the advisories add what the READ raised
        # (unread entities, a walked boundary, a mismatched dimension) — equally true when the
        # drawing turned out to be buildable without you.
        return _summarise_direct(art, verdict["summary"], graph) + _advisories(art)

    # mode == "build": the THIRD IR door — same _run_graph, same pycompiler (IR-ADR-005).
    return _format_direct_build_result(art, verdict["summary"], _run_graph, graph)


# ---------------------------------------------------------------------------
# Tool: analyze_slddrw_test  (TEST/REFERENCE ONLY — was analyze_drawing until 2026-07-27)
# ---------------------------------------------------------------------------
def analyze_slddrw_test(include_geometry: bool = False, include_relations: bool = False) -> str:
    """TEST/REFERENCE TOOL — reads a NATIVE SolidWorks drawing (.SLDDRW) through COM.

    ⚠ NOT the shipping drawing→part path. A .SLDDRW is SLDPRT-linked (its views go blank if the part
      is gone) and nobody ships one — real inputs are DXF/DWG (and PDF). This tool survives for two
      jobs ONLY: (a) checking a drawing YOU produced (do its dimensions match the model?), and
      (b) generating COM ground truth to compare a DXF reader against. For a DXF/DWG job do NOT use
      this tool and do NOT apply its discipline — the fields below (measures, section, normal_axis,
      relations, extent, center_marks) are COM constructs that DO NOT EXIST in a DXF.

    ⮕ Reconstructing a part from a NATIVE SLDDRW (test/comparison work only)? The FROZEN
      reverse-reading discipline is deliberately NOT on the MCP surface (ADR-069) — it is the repo
      file `cad-planner/slddrw-testing-recipe-usage.md`. Read it from disk when this tool is the
      job; port a rule out of it into the active recipe only as a conscious decision (ADR-063).

    Read-only, does NOT change state. Requires an active NATIVE drawing document. All lengths are
    METERS (angles radians), 6 decimals. Returns {view_count, dimension_count, views:[…]}, and the
    FIRST view is the drawing SHEET.

    view — {name, type (swDrawingViewTypes_e int), scale, pos [x,y], dimensions[], section?}
    dimension — {name, value_si, diametric?, attached?, anchors?}. `diametric` true means Ø is DATA,
      not a guess. `anchors` are the dim's reference points in MODEL space; an anchor beyond the base
      body's depth signals a feature that extends past it (offset-plane boss/loft), not bad data.
    section (SECTION views only) — {parent_view, cut_normal (the cutting plane's MODEL-space normal,
      i.e. the viewing direction), axis? (that normal snapped to X/Y/Z when axis-aligned), frame
      {origin, xdir, ydir}, label?}. A 2D coord maps to 3D as p = origin + u*xdir + v*ydir.

    include_geometry (default False) — each view's projected 2D geometry as clean primitives:
      {lines[{x1,y1,x2,y2}], curves[{n,x1,y1,xm,ym,x2,y2,cx?,cy?,r?}] (partial arcs as start/mid/end
      + fitted centre), circles[{cx,cy,r}] (FULL circles, true centre+radius), frame{origin,xdir,ydir,
      normal_axis}, extent? }. Source IView.GetPolylines7; heavier payload, so use it when you need
      the SHAPE, not for a dimension check. Two rules that fail silently if broken:
      · `normal_axis` is the view's viewing direction as a SIGNED principal axis. A circle is the
        cross-section of a feature whose axis runs along it, so circles in views with DIFFERENT
        normal_axis are DIFFERENT features — never chain them into one by radius alone.
      · `extent` {axis:[min,max]} is the SERVER-computed model-space span, frame signs applied. Read
        it before any "no material beyond X" claim; never re-derive a span from raw 2D coordinates,
        because a frame's direction components can be negative.

    include_relations (default False; forces include_geometry) — additive deterministic enrichment;
      pass BOTH for a reconstruction. Ids are positional per read: vid 'v<i>', and within a view
      c<i>/a<i>/l<i> for circles/curves/lines. Every relation carries `source` (closed enum) and
      `residual` (max deviation), so "why is this concentric?" is answerable by reading the field.
      · relations — concentric {members,center,radii} (TRUST these centres, never re-derive),
        equal_diameter {members,r,centers} (same Ø at distinct centres = twin bores), tangent.
      · measures — per dimension, the primitive id(s) it measures. `unattached:true` means it
        resolved to NO primitive: a loud gap to close from geometry, never to drop.
      · center_marks / centerlines — which circles carry marks (on:[ids]), which segments run
        through which (through:[ids]). A view that cannot expose its centerlines says so
        ({centerlines_reported, unreadable:true}) instead of guessing."""
    return _call("analyze_slddrw_test", {"include_geometry": include_geometry,
                                         "include_relations": include_relations})


# ---------------------------------------------------------------------------
# Tool: get_selection
# ---------------------------------------------------------------------------
def get_selection() -> str:
    """Report what the USER currently has selected in the SolidWorks GUI — the inverse of index-based selection.
    When the user clicks geometry in the SolidWorks window (e.g. while telling you what to do — "put a hole on
    THIS face"), call this to learn exactly which entity they mean. Each selected item is mapped to the SAME
    stable index analyze_model(faces/edges) reports, so you can act on it immediately:
      • a FACE  → {type:'face', i, planar, normal?, area, point?} → create_sketch(on_face=True, face_index=i)
      • an EDGE → {type:'edge', i, start, end, mid}              → add_edge_feature(edge_indices='[i]')
      • a VERTEX → {type:'vertex', point:[x,y,z]};  a reference PLANE → {type:'plane', name}
    Returns {selected_count, selection:[...]}. i = -1 if a selected face/edge couldn't be matched to a
    solid-body index. Read-only: does NOT change state_version and does NOT clear the selection — but call it
    BEFORE any tool that clears the selection (create_sketch, add_edge_feature, extrude cut, etc.), otherwise
    the user's pick is gone. If selected_count is 0, ask the user to click the face/edge they mean."""
    return _call("get_selection", {})


# ---------------------------------------------------------------------------

def register_inspection_tools(
    mcp: MCPServer,
    call_fn: CallCallable,
    call_raw_fn: CallRawCallable,
    run_graph_fn: Callable,
) -> Dict[str, Callable]:
    global _call, _call_raw, _run_graph
    _call = call_fn
    _call_raw = call_raw_fn
    _run_graph = run_graph_fn

    tools = [
        export_document,
        batch_export,
        analyze_model,
        analyze_drawing,
        analyze_slddrw_test,
        get_selection,
    ]
    for fn in tools:
        mcp.tool(structured_output=False)(fn)
    return {fn.__name__: fn for fn in tools}
