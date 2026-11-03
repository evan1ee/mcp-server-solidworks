"""Reference resolver — semantic refs and geometric anchors -> concrete indices.

Resolves references against LIVE geometry using the read-only analyze_model / analyze_assembly
tools. This is index-based selection (ADR-033, KNOWN-LIMITATIONS #6): the resolver returns
face/edge INDICES that create_sketch(on_face, face_index) / extrude_feature(up_to_face_index) /
add_edge_feature(edge_indices) / add_mate select directly — no fragile coordinate pick.

Semantic selectors:
  - top_face: among PLANAR, upward-facing faces (normal . +Y >= 0.9), the one with the greatest
    centroid Y. Deterministic and independent of the actual extrude/cut direction.
  - center: origin-centered box -> sketch (0, 0).

Geometric anchors (see feature-graph.schema.json reference_model):
  - face anchor ('near' on the target face's plane): plane-containment match — planar faces whose
    plane contains 'near' within _PLANE_TOL; >1 DISTINCT plane matching => REFERENCE_AMBIGUOUS
    (coplanar faces are interchangeable for an up-to-surface, so among them the nearest
    representative point wins).
  - edge anchor ('near' at the target edge's midpoint): tolerance-ball nearest-mid match —
    exactly one edge mid within _EDGE_TOL, else REFERENCE_UNRESOLVED / REFERENCE_AMBIGUOUS.
"""
import json
import math
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import FeatureError

_UP = (0.0, 1.0, 0.0)   # SolidWorks default "up" axis (+Y).
_NORMAL_TOL = 0.9       # a face counts as "upward" when normal . up >= this
_TIE_EPS = 1e-6         # two upward faces this close in height => ambiguous
_PLANE_TOL = 1e-5       # anchor->plane distance for a face-anchor match (10x the 1µm grid)
_EDGE_TOL = 1e-4        # anchor->mid distance ball for an edge-anchor match (0.1 mm)

_DATUM_AXIS = {"front": (0.0, 0.0, 1.0), "top": (0.0, 1.0, 0.0), "right": (1.0, 0.0, 0.0)}
_FACE_ANCHOR_KINDS = frozenset(("plane", "cylinder", "cone", "sphere"))
_EDGE_ANCHOR_KINDS = frozenset(("line", "circle"))


def _dist3(a: List[float], b: List[float]) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _point_axis_distance(p: Iterable[float], origin: List[float], axis: List[float]) -> float:
    """Distance from point p to the infinite line origin + t*axis (axis unit-ish)."""
    pts = list(p)
    d = [pts[i] - origin[i] for i in range(3)]
    t = d[0] * axis[0] + d[1] * axis[1] + d[2] * axis[2]
    proj = [origin[i] + t * axis[i] for i in range(3)]
    return _dist3(pts, proj)


def _execute_analyze_json(
    port: Any,
    tool: str,
    params: Dict[str, Any],
    state_version: int,
    *,
    call_label: str,
    parse_label: str,
    empty_msg: str,
    node_id: Optional[str],
    step: str,
) -> Dict[str, Any]:
    """Execute a read-only analyze tool via `port` and parse its first `cadState.features` JSON string."""
    resp = port.execute(tool, params, state_version)
    if resp.get("status") != "COMPLETED":
        err = resp.get("error") or {}
        raise FeatureError("%s failed: %s" % (call_label, err.get("message")),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    feats = (resp.get("cadState") or {}).get("features") or []
    if not feats:
        raise FeatureError(empty_msg, code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    try:
        return json.loads(feats[0])
    except Exception as ex:
        raise FeatureError("could not parse %s payload: %s" % (parse_label, ex),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)


def _analyze_payload(
    port: Any,
    state_version: int,
    analysis_type: str,
    node_id: Optional[str],
    step: str,
    *,
    empty_noun: str = "data",
) -> Dict[str, Any]:
    """Run read-only analyze_model(analysis_type) through the port and parse its JSON payload."""
    call_label = "analyze_model(%s)" % analysis_type
    return _execute_analyze_json(
        port,
        "analyze_model",
        {"analysis_type": analysis_type, "name": ""},
        state_version,
        call_label=call_label,
        parse_label=call_label,
        empty_msg="%s returned no %s (is there a solid body?)" % (call_label, empty_noun),
        node_id=node_id,
        step=step,
    )


def _analyze_assembly_payload(
    port: Any,
    state_version: int,
    analysis_type: str,
    component: str,
    node_id: Optional[str],
    step: str,
) -> Dict[str, Any]:
    """Run read-only analyze_assembly(analysis_type, component) and parse its JSON payload."""
    call_label = "analyze_assembly(%s, %s)" % (analysis_type, component)
    return _execute_analyze_json(
        port,
        "analyze_assembly",
        {"analysis_type": analysis_type, "component": component},
        state_version,
        call_label=call_label,
        parse_label="analyze_assembly(%s)" % analysis_type,
        empty_msg="%s returned no data" % call_label,
        node_id=node_id,
        step=step,
    )


def resolve_top_face(port: Any, state_version: int, node_id: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
    """Return (face_index, info) for the semantic 'top' face. Raises FeatureError on
    REFERENCE_UNRESOLVED (no upward face / analyze failed) or REFERENCE_AMBIGUOUS (a tie)."""
    step = "resolve_top_face"
    data = _analyze_payload(port, state_version, "faces", node_id, step, empty_noun="face data")

    candidates = []  # (centroid_y, face_index, face)
    for f in data.get("faces") or []:
        if not f.get("planar"):
            continue
        n = f.get("normal")
        p = f.get("point")
        if not (isinstance(n, list) and isinstance(p, list) and len(n) >= 3 and len(p) >= 3):
            continue
        dot = n[0] * _UP[0] + n[1] * _UP[1] + n[2] * _UP[2]
        if dot >= _NORMAL_TOL:
            candidates.append((p[1], int(f.get("i", -1)), f))

    if not candidates:
        raise FeatureError("no upward-facing planar face found — a 'top face' could not be resolved",
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)

    candidates.sort(key=lambda c: c[0], reverse=True)
    top_y, top_i, top_face = candidates[0]
    if top_i < 0:
        raise FeatureError("resolved top face has no valid index",
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    if len(candidates) > 1 and abs(candidates[1][0] - top_y) < _TIE_EPS:
        raise FeatureError("top face is ambiguous — multiple upward faces at the same height",
                           code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)

    return top_i, {"face_index": top_i, "centroid_y": top_y, "point": top_face.get("point")}


def resolve_center_on_face(face_info: Dict[str, Any]) -> Tuple[float, float]:
    """Resolve position 'center' to sketch-2D coords. Origin-centered box -> sketch (0, 0)."""
    return (0.0, 0.0)


def resolve_face_by_anchor(
    port: Any,
    state_version: int,
    near: List[float],
    node_id: Optional[str] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Face anchor -> face index (the up-to-surface end-condition or sketch support reference).

    Match rule: planar faces whose PLANE contains 'near' (|(near - point) . normal| <= _PLANE_TOL).
    The face's reported 'point' is a REPRESENTATIVE on-plane point, not a centroid — so the plane
    test is primary and the nearest representative point only breaks ties among COPLANAR matches.
    Faces on DISTINCT planes both matching => REFERENCE_AMBIGUOUS."""
    step = "resolve_face_by_anchor"
    data = _analyze_payload(port, state_version, "faces", node_id, step)

    matches = []  # (euclidean_dist_to_point, face_index, plane_key)
    for f in data.get("faces") or []:
        if not f.get("planar"):
            continue
        n = f.get("normal")
        p = f.get("point")
        if not (isinstance(n, list) and isinstance(p, list) and len(n) >= 3 and len(p) >= 3):
            continue
        plane_dist = abs((near[0] - p[0]) * n[0] + (near[1] - p[1]) * n[1] + (near[2] - p[2]) * n[2])
        if plane_dist <= _PLANE_TOL:
            nn = tuple(round(abs(c), 3) for c in n[:3])
            d = round(abs(near[0] * n[0] + near[1] * n[1] + near[2] * n[2]), 5)
            matches.append((_dist3(near, p), int(f.get("i", -1)), (nn, d)))

    if not matches:
        raise FeatureError("no planar face's plane contains the anchor point %s — the up-to face "
                           "could not be resolved" % (list(near),),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    distinct_planes = set(m[2] for m in matches)
    if len(distinct_planes) > 1:
        raise FeatureError("face anchor %s matches faces on %d DISTINCT planes — ambiguous"
                           % (list(near), len(distinct_planes)),
                           code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
    matches.sort(key=lambda m: m[0])
    idx = matches[0][1]
    if idx < 0:
        raise FeatureError("anchored face has no valid index",
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    return idx, {"face_index": idx, "coplanar_candidates": len(matches)}


def resolve_face_on_plane(
    port: Any,
    state_version: int,
    datum: str,
    offset: float,
    node_id: Optional[str] = None,
) -> Optional[int]:
    """An EXISTING planar face lying ON the (datum, offset) plane -> face index, or None.

    Preference order for an offset sketch: sketch ON the face when one exists there — matching
    how the original was authored and avoiding scaffolding planes; create a reference plane only
    when no face lies on that plane. Among coplanar candidates the LARGEST face wins."""
    try:
        data = _analyze_payload(port, state_version, "faces", node_id, "resolve_face_on_plane")
    except FeatureError:
        return None  # e.g. first sketch, no solid body yet
    axis = _DATUM_AXIS[datum]
    best = None  # (area, face_index)
    for f in data.get("faces") or []:
        if not f.get("planar"):
            continue
        n = f.get("normal")
        p = f.get("point")
        if not (isinstance(n, list) and isinstance(p, list) and len(n) >= 3 and len(p) >= 3):
            continue
        if abs(n[0] * axis[0] + n[1] * axis[1] + n[2] * axis[2]) < 0.999:
            continue  # not parallel to the datum
        pos = p[0] * axis[0] + p[1] * axis[1] + p[2] * axis[2]
        if abs(pos - float(offset)) > _PLANE_TOL:
            continue
        area = f.get("area") or 0.0
        idx = int(f.get("i", -1))
        if idx >= 0 and (best is None or area > best[0]):
            best = (area, idx)
    return best[1] if best else None


def resolve_component_entity_by_anchor(
    port: Any,
    state_version: int,
    component_name: str,
    anchor: Dict[str, Any],
    node_id: Optional[str] = None,
) -> Tuple[str, int]:
    """A mate side's COMPONENT-LOCAL anchor {kind, near} -> ('face'|'edge', index) on that
    component — index-first selection for add_mate (KNOWN-LIMITATIONS #6)."""
    step = "resolve_component_entity_by_anchor"
    kind = anchor.get("kind")
    near = anchor["near"]

    if kind in _EDGE_ANCHOR_KINDS:
        data = _analyze_assembly_payload(port, state_version, "edges", component_name, node_id, step)
        in_ball = []
        nearest = None
        for e in data.get("edges") or []:
            m = e.get("mid")
            if not (isinstance(m, list) and len(m) >= 3):
                continue
            d = _dist3(near, m)
            nearest = d if nearest is None or d < nearest else nearest
            if d <= _EDGE_TOL:
                in_ball.append((d, int(e.get("i", -1))))
        if not in_ball:
            raise FeatureError("edge anchor %s matched no edge mid on '%s' within %g m (nearest %.6f m)"
                               % (list(near), component_name, _EDGE_TOL, nearest if nearest is not None else -1),
                               code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
        if len(in_ball) > 1:
            raise FeatureError("edge anchor %s matched %d edges on '%s' — ambiguous"
                               % (list(near), len(in_ball), component_name),
                               code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
        return "edge", in_ball[0][1]

    if kind not in _FACE_ANCHOR_KINDS:
        raise FeatureError("anchor kind %r is not resolvable (supported: %s)"
                           % (kind, sorted(_FACE_ANCHOR_KINDS | _EDGE_ANCHOR_KINDS)),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    if kind in ("cone", "sphere"):
        raise FeatureError("anchor kind %r is a recorded RESOLVER gap (v0 resolves plane/cylinder "
                           "faces and line/circle edges)" % kind,
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)

    a_dir = anchor.get("dir")
    a_radius = anchor.get("radius")

    data = _analyze_assembly_payload(port, state_version, "faces", component_name, node_id, step)
    matches = []  # (tie_break_dist, face_index, group_key)
    for f in data.get("faces") or []:
        idx = int(f.get("i", -1))
        p = f.get("point")
        if kind == "plane":
            if not f.get("planar"):
                continue
            n = f.get("normal")
            if not (isinstance(n, list) and isinstance(p, list) and len(n) >= 3 and len(p) >= 3):
                continue
            if a_dir is not None and abs(n[0] * a_dir[0] + n[1] * a_dir[1] + n[2] * a_dir[2]) < 0.999:
                continue  # normal disagrees with the stored one — a different plane
            plane_dist = abs((near[0] - p[0]) * n[0] + (near[1] - p[1]) * n[1] + (near[2] - p[2]) * n[2])
            if plane_dist <= _PLANE_TOL:
                nn = tuple(round(abs(c), 3) for c in n[:3])
                d = round(abs(near[0] * n[0] + near[1] * n[1] + near[2] * n[2]), 5)
                matches.append((_dist3(near, p), idx, (nn, d)))
        else:  # cylinder
            if f.get("kind") != "cylinder":
                continue
            origin, axis, radius = f.get("origin"), f.get("axis"), f.get("radius")
            if not (isinstance(origin, list) and isinstance(axis, list) and _is_num(radius)):
                continue
            if a_dir is not None and abs(axis[0] * a_dir[0] + axis[1] * a_dir[1] + axis[2] * a_dir[2]) < 0.999:
                continue  # axis disagrees with the stored one
            if a_radius is not None and abs(float(radius) - float(a_radius)) > _PLANE_TOL:
                continue  # the stored fit radius pins WHICH coaxial surface is meant
            if abs(_point_axis_distance(near, origin, axis) - float(radius)) <= _PLANE_TOL:
                ax = tuple(round(abs(c), 3) for c in axis[:3])
                op_ = _point_axis_distance((0.0, 0.0, 0.0), origin, axis)
                key = (ax, round(op_, 5), round(float(radius), 5))
                tie = _dist3(near, p) if isinstance(p, list) and len(p) >= 3 else 0.0
                matches.append((tie, idx, key))

    if not matches:
        raise FeatureError("%s anchor %s matched no face on '%s'"
                           % (kind, list(near), component_name),
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    if len(set(m[2] for m in matches)) > 1:
        raise FeatureError("%s anchor %s matches faces on %d DISTINCT surfaces of '%s' — ambiguous"
                           % (kind, list(near), len(set(m[2] for m in matches)), component_name),
                           code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
    matches.sort(key=lambda m: m[0])
    idx = matches[0][1]
    if idx < 0:
        raise FeatureError("anchored face has no valid index",
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
    return "face", idx


def resolve_edges_by_anchor(
    port: Any,
    state_version: int,
    anchors: Iterable[Dict[str, Any]],
    node_id: Optional[str] = None,
) -> Tuple[List[int], Dict[str, Any]]:
    """Edge anchors -> edge indices (for add_edge_feature edge_indices).

    ONE analyze_model(edges) read serves all anchors. Per anchor: edges whose MID lies within
    the _EDGE_TOL ball around 'near' — exactly one => matched; zero => REFERENCE_UNRESOLVED;
    more than one => REFERENCE_AMBIGUOUS. Two anchors matching the SAME edge is an error too
    (duplicate anchor)."""
    step = "resolve_edges_by_anchor"
    data = _analyze_payload(port, state_version, "edges", node_id, step)

    mids = []  # (edge_index, mid)
    for e in data.get("edges") or []:
        m = e.get("mid")
        if isinstance(m, list) and len(m) >= 3:
            mids.append((int(e.get("i", -1)), m))
    if not mids:
        raise FeatureError("analyze_model(edges) reported no edge midpoints",
                           code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)

    resolved: List[int] = []
    taken: Dict[int, int] = {}
    for k, anchor in enumerate(anchors):
        near = anchor["near"]
        in_ball = [(d, i) for (i, m) in mids for d in (_dist3(near, m),) if d <= _EDGE_TOL]
        if not in_ball:
            nearest = min(_dist3(near, m) for (_i, m) in mids)
            raise FeatureError("edge anchor %d %s matched no edge midpoint within %g m "
                               "(nearest was %.6f m away)" % (k, list(near), _EDGE_TOL, nearest),
                               code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
        if len(in_ball) > 1:
            raise FeatureError("edge anchor %d %s matched %d edge midpoints within %g m — ambiguous"
                               % (k, list(near), len(in_ball), _EDGE_TOL),
                               code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
        idx = in_ball[0][1]
        if idx < 0:
            raise FeatureError("edge anchor %d resolved to an edge with no valid index" % k,
                               code="REFERENCE_UNRESOLVED", node_id=node_id, step=step)
        if idx in taken:
            raise FeatureError("edge anchors %d and %d resolved to the SAME edge #%d — duplicate anchor"
                               % (taken[idx], k, idx),
                               code="REFERENCE_AMBIGUOUS", node_id=node_id, step=step)
        taken[idx] = k
        resolved.append(idx)
    return resolved, {"edge_indices": resolved}
