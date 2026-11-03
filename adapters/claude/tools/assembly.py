from __future__ import annotations

from typing import Any, Callable, Dict, Literal, Optional
from mcp.server import MCPServer
from tools.common import CallCallable

_call: Optional[CallCallable] = None


def analyze_assembly(
    analysis_type: Literal["components", "components_flat", "mates", "faces", "edges"],
    component: str = "",
) -> str:
    """Analyze the active ASSEMBLY document (read-only, does NOT change state).
    analysis_type='components': top-level component occurrences in FEATURE-TREE order — per
        instance: name (instance-numbered, e.g. 'Shaft-1'), source file path, config, fixed /
        suppressed flags, children count (a subassembly — out of scope, recorded as a gap), and
        the FULL transform (13 numbers: 3x3 rotation row-major + translation in meters + scale)
        — the position ground truth for round-trip verification.
    analysis_type='components_flat': LEAF occurrences across ALL nesting levels (depth-first
        tree order, transforms in ROOT space, each with its parent's name) — flattens a
        wrapper/subassembly level for ground-truth acquisition; the IR itself stays flat.
    analysis_type='mates': all mates in CREATION order — type and alignment from the SolidWorks
        ENUMS (locale-proof), the SI value for distance/angle mates, and per mate entity: owning
        component, entity kind (plane/cylinder/circle/...), and its geometric params in ASSEMBLY
        space (location + direction + radii).
    analysis_type='faces' / 'edges': ONE component's face/edge list with stable indices — feeds
        add_mate's index-based entity selection. Requires `component` (the Name2 from
        'components'). Coordinates are COMPONENT-LOCAL (the payload carries the transform)."""
    params: Dict[str, Any] = {"analysis_type": analysis_type}
    if component:
        params["component"] = component
    return _call("analyze_assembly", params)


def insert_component(
    file_path: str,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    config: str = "",
    fixed: bool = False,
    transform_json: str = "[]",
) -> str:
    """Insert a part file as a component of the ACTIVE assembly (open_new_assembly first).
    file_path: the .SLDPRT to insert (meters for all coordinates).
    x/y/z: insertion point — used when transform_json is not given.
    transform_json: OPTIONAL full placement as a JSON array STRING of 13 numbers
        (3x3 rotation row-major + translation xyz + scale — exactly the layout
        analyze_assembly(components) reports). Needed for rotated placements.
    fixed: True fixes the occurrence (its transform becomes authoritative); False leaves it
        floating for mates to position. The state is applied explicitly either way.
    Returns the runtime component name (e.g. 'Shaft-2') + fixed/transform readbacks."""
    params: Dict[str, Any] = {"file_path": file_path, "x": x, "y": y, "z": z, "fixed": fixed}
    if config:
        params["config"] = config
    if transform_json and transform_json != "[]":
        params["transform_json"] = transform_json
    return _call("insert_component", params)


def add_mate(
    mate_type: Literal[
        "coincident",
        "concentric",
        "perpendicular",
        "parallel",
        "tangent",
        "distance",
        "angle",
        "lock",
    ],
    a_component: str,
    a_index: int,
    b_component: str,
    b_index: int,
    a_kind: Literal["face", "edge"] = "face",
    b_kind: Literal["face", "edge"] = "face",
    alignment: Literal["aligned", "anti_aligned", "closest"] = "closest",
    flip: bool = False,
    value: float = 0.0,
) -> str:
    """Add a mate between two component entities in the ACTIVE assembly.
    Entity selection is INDEX-based (robust — coordinate picks miss real geometry): each side is
    (component name from analyze_assembly(components), kind 'face'|'edge', index from
    analyze_assembly(faces|edges, component=...)).
    value: METERS for distance mates, DEGREES for angle mates (ignored otherwise).
    alignment 'closest' lets SolidWorks pick the nearer solution; the mates reader reports the
    RESOLVED alignment afterwards.
    Returns the mate name + both components' post-rebuild transforms — compare against the
    original's readback after EVERY mate (the stop-check discipline)."""
    params: Dict[str, Any] = {
        "mate_type": mate_type,
        "alignment": alignment,
        "a_component": a_component,
        "a_kind": a_kind,
        "a_index": a_index,
        "b_component": b_component,
        "b_kind": b_kind,
        "b_index": b_index,
    }
    if flip:
        params["flip"] = True
    if mate_type in ("distance", "angle"):
        params["value"] = value
    return _call("add_mate", params)


def register_assembly_tools(mcp: MCPServer, call_fn: CallCallable) -> Dict[str, Callable]:
    global _call
    _call = call_fn

    tools = [analyze_assembly, insert_component, add_mate]
    for fn in tools:
        mcp.tool(structured_output=False)(fn)
    return {fn.__name__: fn for fn in tools}
