from __future__ import annotations

from typing import Annotated, Any, Callable, Dict, Literal, Optional
from pydantic import Field
from mcp.server import MCPServer
from tools.common import CallCallable

_call: Optional[CallCallable] = None


def sheet_metal_feature(
    feature_type: Literal[
        "base_flange",
        "edge_flange",
        "edge_flange_sketch",
        "edge_flange_finish",
        "flat_pattern",
        "sketched_bend",
    ],
    thickness: Annotated[float, Field(
        gt=0, description="Sheet thickness in METERS")] = 0.001,
    bend_radius: Annotated[float, Field(
        gt=0, description="Bend radius in METERS")] = 0.001,
    k_factor: Annotated[float, Field(
        gt=0, lt=1, description="Neutral-axis K-factor (0..1)")] = 0.5,
    ex: float = 0.0,
    ey: float = 0.0,
    ez: float = 0.0,
    flange_length: Annotated[float, Field(
        gt=0, description="Edge flange length in METERS; use >= 2*thickness+1mm (KNOWN-LIMITATIONS #11)")] = 0.02,
    angle: Annotated[float, Field(
        gt=0, le=180, description="Bend angle in DEGREES (edge_flange and sketched_bend; default 90)")] = 90.0,
    use_default_radius: bool = False,
    flip: bool = False,
    bend_position: Literal["centerline", "material_inside", "material_outside", "bend_outside"] = "centerline",
    fixed_face_index: int = -1,
    fixed_x: float = 0.0,
    fixed_y: float = 0.0,
    fixed_z: float = 0.0,
    reverse_thickness: bool = False,
    symmetric_thickness: bool = False,
    clear_profile: bool = True,
    edge_index: int = -1,
) -> str:
    """Create sheet metal features on the active part. Lengths METERS, angles DEGREES.

    'base_flange' — a sheet metal base from the ACTIVE sketch profile; exits sketch mode.
        thickness · bend_radius (default = thickness) · k_factor (0.5).
        THICKENING SIDE IS A BUILD DECISION, and it is silent when wrong: `reverse_thickness`
        thickens to the OPPOSITE side of the sketch plane, `symmetric_thickness` splits ±t/2.
        Rebuilding an analyzed part, replay the original's own flags (analyze_model(features)
        reports them on the SMBaseFlange) — they also set the sheet orientation later bends fold
        against, so a wrong side fails downstream and less clearly.
    'edge_flange' — a DEFAULT-profile (full-edge-width) flange on an existing sheet metal edge.
        Select by edge_index (from analyze_model(edges), PREFERRED — a coordinate pick can miss a
        real edge) or a point ex/ey/ez on it. flange_length · angle (90).
    'edge_flange_sketch' + 'edge_flange_finish' — the CUSTOM-profile flange, in two calls. The
        sketch call takes the attach edge (edge_index preferred, else ex/ey/ez), generates the
        edge-linked profile sketch (the API accepts ONLY a sketch it generated), clears it
        (clear_profile) and leaves it ACTIVE, echoing the sketch's MEASURED frame — express your
        profile in THAT frame. Draw with add_sketch_entity, then call the finish with the SAME
        edge (+ angle, bend_radius or use_default_radius, bend_position). The profile defines the
        outline, so flange_length is ignored.
    'sketched_bend' — bends the sheet about the bend LINE(S) in the ACTIVE sketch (draw them on a
        sheet face with create_sketch + add_sketch_entity first; the sketch must still be active).
        The side that stays PUT is the fixed face: fixed_face_index (from analyze_model(faces),
        PREFERRED) or fixed_x/y/z, a 3D point ON that face. angle (90) · bend_radius, or
        use_default_radius=True to take the sheet's default and ignore it · flip reverses the fold
        direction · bend_position 'centerline' (default) | 'material_inside' | 'material_outside' |
        'bend_outside', matching the `position` analyze_model(features) reports on an SM3dBend, so a
        recipe value replays as-is. ONE sketch with SEVERAL bend lines makes one feature with one
        bend per line.
    'flat_pattern' — unfolds every bend. No extra parameters; needs an existing base_flange.

    A wrong fold is expensive: there is no sketch-entity move/delete tool, so repairing one costs
    deleting the feature and re-creating it. And a mirrored fold is invisible to volume, area AND
    topology alike — read one bend face back after building (recipe R14)."""
    params: Dict[str, Any] = {
        "feature_type": feature_type,
        "thickness": thickness,
        "bend_radius": bend_radius,
        "k_factor": k_factor,
        "ex": ex, "ey": ey, "ez": ez,
        "flange_length": flange_length,
        "angle": angle,
        "use_default_radius": use_default_radius,
        "flip": flip,
        "bend_position": bend_position,
        "fixed_x": fixed_x, "fixed_y": fixed_y, "fixed_z": fixed_z,
        "reverse_thickness": reverse_thickness,
        "symmetric_thickness": symmetric_thickness,
        "clear_profile": clear_profile,
    }
    if fixed_face_index >= 0:
        params["fixed_face_index"] = fixed_face_index
    if edge_index >= 0:
        params["edge_index"] = edge_index
    return _call("sheet_metal_feature", params)


def register_sheet_metal_tools(mcp: MCPServer, call_fn: CallCallable) -> Dict[str, Callable]:
    global _call
    _call = call_fn

    tools = [sheet_metal_feature]
    for fn in tools:
        mcp.tool(structured_output=False)(fn)
    return {fn.__name__: fn for fn in tools}
