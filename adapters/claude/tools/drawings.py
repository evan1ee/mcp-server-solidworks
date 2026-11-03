from __future__ import annotations

from typing import Annotated, Any, Callable, Dict, Literal, Optional
from pydantic import Field
from mcp.server import MCPServer
from dimension_plan import plan_dimensions, execute_plan
from tools.common import CallCallable

_call: Optional[CallCallable] = None

# Tool: create_drawing
# ---------------------------------------------------------------------------
def create_drawing(model_path: str = "") -> str:
    """Open a new SolidWorks drawing document (A3 sheet, 1:1 scale).
    model_path: optional path to the part/assembly to reference. If omitted, an empty drawing is created.
    Note: add_drawing_view requires the referenced part to be saved to disk."""
    return _call("create_drawing", {"model_path": model_path})


# ---------------------------------------------------------------------------
# Tool: add_drawing_view
# ---------------------------------------------------------------------------
def add_drawing_view(
    view_type: Literal["front", "top", "right", "isometric", "back", "bottom", "left"],
    pos_x: float = 0.1,
    pos_y: float = 0.1,
    scale: Annotated[float, Field(
        gt=0, description="View scale (1.0 = 1:1)")] = 1.0,
    model_path: str = "",
    display_mode: Literal["", "hlv", "hlr", "wireframe", "shaded", "shaded_edges", "default"] = "",
) -> str:
    """Add a model view to the active drawing sheet.
    view_type: 'front', 'top', 'right', 'isometric', 'back', 'bottom', 'left'.
    pos_x/pos_y: position on the drawing sheet in meters.
    scale: view scale (default 1.0 = 1:1).
    model_path: path to the part file; if omitted, uses the first open part document.
    display_mode: view display style — 'hlv' (Hidden Lines Visible), 'hlr' (Hidden Lines Removed),
        'wireframe', 'shaded', 'shaded_edges', or 'default' (document default). Leave '' to apply the
        DRAFTING-CONVENTION default: orthographic views (front/top/right/back/bottom/left) become HLV
        (hidden lines shown for reference — but never dimension to them), isometric keeps the document
        default. Section views (add_section_view) are separate and show the cut, not hidden lines.
    Requires the part to be saved to disk (in-memory parts cannot be projected)."""
    return _call(
        "add_drawing_view",
        {"view_type": view_type, "pos_x": pos_x, "pos_y": pos_y,
            "scale": scale, "model_path": model_path, "display_mode": display_mode},
    )


# ---------------------------------------------------------------------------
# Tool: add_flat_pattern_view
# ---------------------------------------------------------------------------
def add_flat_pattern_view(
    pos_x: float = 0.1,
    pos_y: float = 0.1,
    scale: Annotated[float, Field(
        gt=0, description="View scale (1.0 = 1:1)")] = 1.0,
    model_path: str = "",
    config_name: str = "",
    hide_bend_lines: bool = False,
    flip_view: bool = False,
) -> str:
    """Add a FLAT PATTERN view of a SHEET-METAL part to the active drawing — the unfolded blank with
    bend lines/notes. Sheet metal is dimensioned on its FLAT PATTERN (blank size, hole positions,
    bend lines), not on the folded orthographic views, so use this rather than them; then call
    auto_dimension_drawing.

    pos_x/pos_y: position on the drawing sheet in meters.
    scale: view scale (default 1.0 = 1:1).
    model_path: path to the sheet-metal part; if omitted, uses the first open part document.
    config_name: configuration to flatten; if omitted, the part's active configuration is used
        (falling back to 'Default').
    hide_bend_lines: hide the bend lines in the flat pattern (default False).
    flip_view: flip the flat pattern view (default False).

    The part must be sheet metal (have a Flat-Pattern feature) and saved to disk, else returns
    FLAT_PATTERN_VIEW_FAILED. Bumps state_version; result_geometry echoes {view_name, config}."""
    return _call(
        "add_flat_pattern_view",
        {"pos_x": pos_x, "pos_y": pos_y, "scale": scale, "model_path": model_path,
            "config_name": config_name, "hide_bend_lines": hide_bend_lines,
            "flip_view": flip_view},
    )


# ---------------------------------------------------------------------------
# Tool: add_drawing_dimension
# ---------------------------------------------------------------------------
def add_drawing_dimension(
    px: float,
    py: float,
    value: Annotated[float, Field(gt=0, description="Dimension value in METERS")],
    label_offset_x: float = 0.0,
    label_offset_y: float = -0.015,
) -> str:
    """Add a smart dimension to a drawing view segment nearest to point (px, py).
    Works the same way as add_dimension but operates in a drawing document context.
    px/py should be on or very near the target segment in drawing sheet coordinates (meters).
    Requires an active drawing document."""
    return _call(
        "add_drawing_dimension",
        {"px": px, "py": py, "value": value, "label_offset_x": label_offset_x,
            "label_offset_y": label_offset_y},
    )


# ---------------------------------------------------------------------------
# Tool: add_drawing_dimension_ex
# ---------------------------------------------------------------------------
def add_drawing_dimension_ex(
    x1: float,
    y1: float,
    text_x: float,
    text_y: float,
    x2: Optional[float] = None,
    y2: Optional[float] = None,
    orientation: Literal["auto", "horizontal", "vertical", "aligned"] = "auto",
    arc_condition: Literal["center", "min", "max"] = "center",
    expected_value: Optional[float] = None,
    prefix: str = "",
    suffix: str = "",
    precision: int = -1,
    measure_only: bool = False,
) -> str:
    """Add a VERIFIED two-pick dimension to the active drawing. Picks the projected edge nearest (x1,y1)
    and, if given, the one nearest (x2,y2) (sheet meters), dimensions between them (horizontal / vertical
    / aligned / auto) and places the text at (text_x,text_y). Two circle picks measure center-to-center
    by default (hole pitch, bolt circle); use arc_condition min/max for edge-to-edge. One circle pick
    (omit x2,y2) gives a diameter.
    expected_value: the value you expect in METERS; if the measured value differs by > 1e-6 m the
    dimension is deleted and the call fails (DIMENSION_VALUE_MISMATCH) — always pass it.
    prefix: dimension text prefix such as '4×Ø' (replaces the automatic Ø). suffix: e.g. ' THRU'. precision: displayed decimal places (-1 = default).
    measure_only: create and verify the dimension, then delete it (nothing stays on the sheet); the value is
    returned in result_geometry.measured_value. Use for values written elsewhere, e.g. a bolt-circle diameter in a hole callout.
    Requires an active drawing document. Bumps state_version."""
    params: dict = {"x1": x1, "y1": y1, "text_x": text_x, "text_y": text_y,
                    "orientation": orientation, "arc_condition": arc_condition,
                    "prefix": prefix, "suffix": suffix, "precision": precision}
    if x2 is not None and y2 is not None:
        params.update({"x2": x2, "y2": y2})
    if expected_value is not None:
        params["expected_value"] = expected_value
    if measure_only:
        params["measure_only"] = True
    return _call("add_drawing_dimension_ex", params)


# ---------------------------------------------------------------------------
# Tool: move_drawing_dimension
# ---------------------------------------------------------------------------
def move_drawing_dimension(name: str, x: float, y: float) -> str:
    """Move an existing drawing dimension's text to sheet position (x, y) in METERS; the dimension and
    extension lines follow. name is the full dimension name returned by add_drawing_dimension_ex
    (e.g. 'D1@Drawing View1'). Use it to fix crossing dimensions after placement (swap nested tiers).
    Returns the before/after position. Requires an active drawing document. Bumps state_version."""
    return _call("move_drawing_dimension", {"name": name, "x": x, "y": y})


# ---------------------------------------------------------------------------
# Tool: dimension_drawing
# ---------------------------------------------------------------------------
def dimension_drawing(
    facts_json: str,
    views_json: str,
    dry_run: bool = False,
) -> str:
    """Fully dimension an IR-built part's drawing views (strategy 'full'): overall size, hole pitch,
    bolt circle, hole callouts with count prefix, boss/bore diameters and the depth profile — with
    non-overlapping offset tiers, no cross-view duplicates, every value verified against the model
    (add_drawing_dimension_ex expected_value). Call AFTER add_drawing_view for the views.

    facts_json: JSON {"width","height" (m), "boss":{"diameter"}, "bore":{"diameter"},
      "hole_groups":[{"role":"mount"|"pcd","diameter","centers":[[x,y]..]}],
      "depth":{"total","flange","boss","boss_width"}} — front-view-local meters from analyze_model.
    views_json: JSON {"front":{"pos":[x,y],"scale":s},"top":{"pos":[x,y],"scale":s}} where pos is the
      sheet position of each view's bounding-box center (as reported by analyze_slddrw_test).
    dry_run: return the planned dimension calls without creating anything."""
    plan = plan_dimensions(json.loads(facts_json), json.loads(views_json))
    if dry_run:
        return json.dumps(plan, ensure_ascii=False)
    return json.dumps(execute_plan(plan, _call), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: auto_dimension_drawing
# ---------------------------------------------------------------------------
def auto_dimension_drawing(
    all_views: bool = True,
    include_unmarked: bool = False,
    eliminate_duplicates: bool = True,
) -> str:
    """Transfer the MODEL's driving dimensions into the active drawing's views (SolidWorks 'Insert
    Model Items > Dimensions'). PREFER this over add_drawing_dimension's coordinate pick — the
    values come from the model's own parametric dimensions and are placed for you. Call it AFTER
    create_drawing + add_drawing_view(s), then verify with analyze_slddrw_test (dimension_count > 0,
    values matching the model).

    all_views: insert into all drawing views (True) or only the currently selected view (False). Default True.
    include_unmarked: also insert driving dimensions NOT marked for drawing (i.e. ALL driving dims), not just
        those flagged 'marked for drawing'. More complete but noisier. Default False — if a marked-only pass
        inserts 0 dimensions (the part's dims weren't marked for drawing), re-call with include_unmarked=True.
    eliminate_duplicates: avoid inserting the same model dimension into more than one view. Default True.

    Returns COMPLETED with result_geometry.inserted_count (the number of dimensions placed; 0 is a valid
    outcome). Requires an active drawing with at least one model view. Bumps state_version."""
    return _call(
        "auto_dimension_drawing",
        {"all_views": all_views, "include_unmarked": include_unmarked,
            "eliminate_duplicates": eliminate_duplicates},
    )


# ---------------------------------------------------------------------------
# Tool: auto_center_marks
# ---------------------------------------------------------------------------
def auto_center_marks(
    include_slots: bool = True,
    extended_lines: bool = True,
) -> str:
    """Automatically place center marks (with extended centerlines) on every hole/slot in every
    model view of the active drawing (SolidWorks 'Auto Insert > Center Marks'). This is the robust,
    automatic way to add centerlines to a holed part's drawing — no coordinate picking. Call it
    after the views exist (create_drawing + add_drawing_view), typically alongside
    auto_dimension_drawing, for a difficulty-2-grade drawing of a bracket/flange with holes.

    include_slots: also add slot center marks/centerlines for linear & arc slots (not just round holes). Default True.
    extended_lines: draw the extended centerlines through each hole rather than a small cross. Default True.

    Returns COMPLETED with result_geometry.center_marks (total marks now on all views; 0 if the
    part has no holes/slots) and center_marks_added (how many this call added). A hole that already
    has a mark keeps exactly one (stacked copies are deleted, counted in duplicates_removed). Bumps state_version. Requires an active drawing with model views."""
    return _call(
        "auto_center_marks",
        {"include_slots": include_slots, "extended_lines": extended_lines},
    )


# ---------------------------------------------------------------------------
# Tool: add_hole_callout
# ---------------------------------------------------------------------------
def add_hole_callout(px: float, py: float) -> str:
    """Insert a hole callout (e.g. '4× Ø8 THRU') on the hole edge nearest the sheet point (px, py)
    in the active drawing. Coordinate-based selection — same fragile point pick as add_drawing_dimension
    (KNOWN-LIMITATIONS #6), so place px/py right on the hole's projected circular edge. For centerlines
    prefer auto_center_marks (robust/automatic); use this for explicit per-hole callouts where wanted.
    px/py: a point on the target hole's projected circle, in drawing sheet coordinates (meters).
    Requires an active drawing document. Bumps state_version."""
    return _call("add_hole_callout", {"px": px, "py": py})


# ---------------------------------------------------------------------------
# Tool: add_section_view
# ---------------------------------------------------------------------------
def add_section_view(
    px: float,
    py: float,
    edge_x: Optional[float] = None,
    edge_y: Optional[float] = None,
    x1: Optional[float] = None,
    y1: Optional[float] = None,
    x2: Optional[float] = None,
    y2: Optional[float] = None,
    label: str = "A",
    flip: bool = False,
    scale: float = 0.0,
) -> str:
    """Create a SECTION VIEW that cuts the model so internal/blind features become visible, dimensionable
    edges. Use this when a feature's size can't be shown in a projected view — most importantly a BLIND
    POCKET's DEPTH (its floor is a hidden edge in an orthographic view, so auto_dimension_drawing can't
    place the depth).

    Two ways to define the cut (provide ONE pair-set):
    - EDGE mode (edge_x, edge_y): cut collinear with an existing straight edge already projected in
      a view — point at it.
    - LINE mode (x1,y1 → x2,y2): draw the cut line, for cutting THROUGH a feature's interior where
      no edge exists. It must fully cross the view through the feature.
    All coordinates are drawing sheet coordinates in meters (KNOWN-LIMITATIONS #6 — coordinate-based).

    px,py: where to place the section view (meters) — also picks which side the section projects toward.
    label: section label (default 'A' → 'SECTION A-A'). flip: reverse the cut/viewing direction.
    scale: optional section view scale; inherits the parent view's scale if 0/omitted.

    After the section lands, call auto_dimension_drawing or add_drawing_dimension to dimension the
    now-visible depth. Bumps state_version. Requires an active drawing with a parent model view. If it
    returns SECTION_VIEW_FAILED, the drawing state may be wedged by prior failed attempts — a fresh
    server/drawing state clears it."""
    params: dict = {"px": px, "py": py, "label": label, "flip": flip}
    for k, v in (("edge_x", edge_x), ("edge_y", edge_y),
                 ("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2)):
        if v is not None:
            params[k] = v
    if scale and scale > 0:
        params["scale"] = scale
    return _call("add_section_view", params)


# ---------------------------------------------------------------------------


def register_drawing_tools(mcp: MCPServer, call_fn: CallCallable) -> Dict[str, Callable]:
    global _call
    _call = call_fn

    tools = [
        create_drawing,
        add_drawing_view,
        add_flat_pattern_view,
        add_drawing_dimension,
        add_drawing_dimension_ex,
        dimension_drawing,
        move_drawing_dimension,
        auto_dimension_drawing,
        auto_center_marks,
        add_hole_callout,
        add_section_view,
    ]
    for fn in tools:
        mcp.tool(structured_output=False)(fn)
    return {fn.__name__: fn for fn in tools}
