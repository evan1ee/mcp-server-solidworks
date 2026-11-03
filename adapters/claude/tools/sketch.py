from __future__ import annotations

import json
from typing import Annotated, Any, Callable, Dict, Literal, Optional
from pydantic import Field
from mcp.server import MCPServer
from tools.common import CallCallable

_call: Optional[CallCallable] = None


def create_sketch(
    plane: str = "",
    on_face: bool = False,
    face_index: int = -1,
    face_x: float = 0.0,
    face_y: float = 0.0,
    face_z: float = 0.0,
) -> str:
    """Create a new sketch on a plane OR on an existing model face.
    Default (on_face=False): provide plane (e.g. 'Front Plane', 'Top Plane', or a named ref plane like 'Plane1').
    On a model face (on_face=True), pick the face one of two ways:
      • face_index (PREFERRED) — the integer index of the target face from analyze_model(analysis_type='faces').
        Robust: it selects the face directly, so it works on a revolve end-cap / shaft end where a coordinate
        pick is ambiguous (the flat circular face's centre lies on the revolve axis → FACE_NOT_FOUND).
      • face_x/face_y/face_z — a point in METERS lying ON the target planar face (interior, NOT on an edge).
    Use on_face for features on existing geometry (recess/hub/keyway on a part face, a bore on a shaft end);
    plane names cannot reach model faces. face_index takes priority over face_x/y/z when >= 0."""
    params: Dict[str, Any] = {
        "plane": plane,
        "on_face": on_face,
        "face_x": face_x,
        "face_y": face_y,
        "face_z": face_z,
    }
    if face_index >= 0:
        params["face_index"] = face_index
    return _call("create_sketch", params)


def add_sketch_entity(
    entity_type: Literal[
        "rectangle", "circle", "line", "arc", "arc_center", "ellipse", "spline", "fillet", "chamfer"
    ],
    x1: float = 0.0,
    y1: float = 0.0,
    x2: float = 0.0,
    y2: float = 0.0,
    xm: float = 0.0,
    ym: float = 0.0,
    cx: float = 0.0,
    cy: float = 0.0,
    radius: float = 0.0,
    vx: float = 0.0,
    vy: float = 0.0,
    distance: float = 0.0,
    direction: float = 0.0,
    points: str = "[]",
    construction: bool = False,
) -> str:
    """Add a sketch entity to the active sketch.
    entity_type='rectangle':  uses x1,y1 (first corner) and x2,y2 (opposite corner).
    entity_type='circle':     uses cx,cy (center) and radius.
    entity_type='line':       uses x1,y1 (start) and x2,y2 (end).
    entity_type='arc':        uses x1,y1 (start), x2,y2 (end), xm,ym (mid-arc point) — a 3-point arc.
    entity_type='arc_center': uses cx,cy (exact center), x1,y1 (start), x2,y2 (end) and optional
                              direction. PREFER this over 'arc' whenever the exact centre/radius are
                              known: it guarantees the radius, and the 3-point fit is unreliable on
                              shallow or near-collinear arcs.
    entity_type='ellipse':    uses cx,cy (center), x1,y1 (a point on the MAJOR axis), x2,y2 (a point
                              on the MINOR axis).
    entity_type='spline':     uses points — a JSON array STRING of flat through-points
                              '[x1,y1,x2,y2,...]' (>= 2 points), round-tripped exactly.
    entity_type='fillet':     uses vx,vy (vertex to round) and radius.
    entity_type='chamfer':    uses vx,vy (vertex to cut) and distance.
    direction: only for 'arc_center' — sweep sense from start to end. OMIT IT (or pass 0) for the
               MINOR (<=180°) arc, which a corner round virtually always is; a wrong explicit sign
               SILENTLY draws the >180° complement. Pass +1 (CCW) or -1 (CW) only for a deliberate
               major sweep (a round-trip replays analyze's 'dir' verbatim).
    construction: True makes the entity CONSTRUCTION/reference geometry — it guides the profile but
               adds no edge. Mirrors analyze_model's per-segment 'construction' flag.

    This tool MIRRORS analyze_model 1:1 — every segment it emits (line, arc/circle, ellipse, spline,
    with cx/cy/x1/y1/x2/y2/radius/points/construction, and 'dir' on partial arcs) maps onto an
    entity_type here, so an analyzed sketch rebuilds without dropping or simplifying a curve.
    On COMPLETED the result carries result_geometry: what SolidWorks actually created, READ BACK
    rather than echoed — check the radius/endpoints against your intent before moving on.
    Rebuilding from exact coordinates: do NOT add coincident constraints between segments. Shared
    endpoint coordinates already close the profile, and a constraint that fails rolls the sketch back.
    All coordinates in METERS."""
    return _call(
        "add_sketch_entity",
        {
            "entity_type": entity_type,
            "x1": x1,
            "y1": y1,
            "x2": x2,
            "y2": y2,
            "xm": xm,
            "ym": ym,
            "cx": cx,
            "cy": cy,
            "radius": radius,
            "vx": vx,
            "vy": vy,
            "distance": distance,
            "direction": direction,
            "points": json.loads(points) if points else [],
            "construction": construction,
        },
    )


def add_sketch_entities(segments: str) -> str:
    """Add MANY sketch entities to the active sketch in ONE call (the batch form of
    add_sketch_entity — prefer this whenever a profile has more than ~3 segments).
    segments: a JSON array STRING of entity records, each record = add_sketch_entity's
    parameters for one entity, e.g.
      '[{"entity_type":"line","x1":0,"y1":0,"x2":0.05,"y2":0},
        {"entity_type":"arc_center","cx":0.05,"cy":0.005,"x1":0.05,"y1":0,"x2":0.055,"y2":0.005},
        {"entity_type":"circle","cx":0.02,"cy":0.02,"radius":0.004,"construction":true}]'
    Corner rounds / fillet arcs: use arc_center WITHOUT "direction" — the tool then draws the
    MINOR (<=180°) arc deterministically (an explicit wrong sign silently draws the 270°
    complement); pass direction ±1 only for a deliberate major arc.
    Supported entity_type values and their parameters are IDENTICAL to add_sketch_entity
    (rectangle/circle/line/arc/arc_center/ellipse/spline/fillet/chamfer; per-record
    'construction': true supported; one difference: a 'spline' record's "points" is a plain
    JSON array [x1,y1,x2,y2,...], not a nested string). Segments are created in array order —
    list a profile's segments in contour order with EXACT shared endpoints (no coincident
    constraints needed).
    NOT transactional: on the first failing record the call returns FAILED naming segments[i]
    and how many earlier records were created — those remain in the sketch.
    On COMPLETED, result_geometry echoes {segment_count, counts per entity_type} (compact —
    NO per-segment geometry echo; read back with analyze_model(sketch, name=...) if needed).
    All coordinates in document units (meters)."""
    return _call(
        "add_sketch_entities",
        {"segments": json.loads(segments) if segments else []},
    )


def add_sketch_constraint(
    constraint_type: Literal[
        "horizontal",
        "vertical",
        "coincident",
        "parallel",
        "perpendicular",
        "tangent",
        "equal",
        "midpoint",
    ],
    px1: float,
    py1: float,
    px2: float = 0.0,
    py2: float = 0.0,
    entity_type1: Literal["SKETCHSEGMENT", "SKETCHPOINT"] = "SKETCHSEGMENT",
    entity_type2: Literal["SKETCHSEGMENT", "SKETCHPOINT"] = "SKETCHSEGMENT",
) -> str:
    """Apply a geometric constraint to sketch entities in the active sketch.
    constraint_type: 'horizontal', 'vertical' — single entity (px1, py1 only).
    constraint_type: 'coincident', 'parallel', 'perpendicular', 'tangent', 'equal', 'midpoint' — requires two entities (px2, py2).
    px1/py1: point on the first entity. px2/py2: point on the second entity (two-entity constraints).
    entity_type1/2: 'SKETCHSEGMENT' (default) or 'SKETCHPOINT' for point-based constraints like coincident/midpoint.
    All coordinates in document units (meters)."""
    return _call(
        "add_sketch_constraint",
        {
            "constraint_type": constraint_type,
            "px1": px1,
            "py1": py1,
            "px2": px2,
            "py2": py2,
            "entity_type1": entity_type1,
            "entity_type2": entity_type2,
        },
    )


def add_dimension(
    px: float,
    py: float,
    value: Annotated[float, Field(gt=0, description="Dimension value in METERS (e.g. 0.05 = 50mm)")],
    label_offset_x: float = 0.0,
    label_offset_y: float = -0.015,
) -> str:
    """Apply a smart dimension to the sketch segment nearest to point (px, py).
    px/py should be on or very near the target segment — e.g. the midpoint of a line.
    label_offset_x/y control where the dimension label is placed relative to the segment point.
    Works for any sketch geometry (rectangles, polygons, complex profiles)."""
    return _call(
        "add_dimension",
        {
            "px": px,
            "py": py,
            "value": value,
            "label_offset_x": label_offset_x,
            "label_offset_y": label_offset_y,
        },
    )


def edit_sketch(sketch_name: str) -> str:
    """Reopen an existing sketch for editing. Counterpart to create_sketch.
    sketch_name: exact name of the sketch feature in the feature tree (e.g. 'Sketch1').
    Returns COMPLETED with ActiveSketch set to sketch_name.
    After editing, call extrude_feature (or any feature) to exit the sketch."""
    return _call("edit_sketch", {"sketch_name": sketch_name})


def register_sketch_tools(mcp: MCPServer, call_fn: CallCallable) -> Dict[str, Callable]:
    global _call
    _call = call_fn

    tools = [
        create_sketch,
        add_sketch_entity,
        add_sketch_entities,
        add_sketch_constraint,
        add_dimension,
        edit_sketch,
    ]
    for fn in tools:
        mcp.tool(structured_output=False)(fn)
    return {fn.__name__: fn for fn in tools}
