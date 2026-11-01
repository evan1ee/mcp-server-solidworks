"""Edge-blend detection (fillets, chamfers) and the SHARP edge each one replaced.

A blend is recorded with the sharp edge's position because that is what the rebuilt IR anchors on:
the fillet/chamfer node runs on the sharp-cornered rebuild, and the resolver matches an anchor
against that edge's `mid`.
  - fillet: a cylinder/torus surface joined by TANGENT edges to exactly two supports that are not
    parallel planes (excludes slot ends between parallel walls; hole halves are one surface).
  - chamfer: a plane joined by CONVEX edges (its two longest shared boundaries) to two supports, its
    normal strictly between theirs; or, on turned parts, a coaxial cone between a plane ⟂ the axis
    and a cylinder.
Straight sharp edges get `anchor` (the sharp edge's midpoint); circular ones get `sharp_circle`
(centre, axis, radius) — their resolver `mid` depends on how the rebuild creates the circle, so the
emitter computes it.
"""
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Literal, Optional, Tuple

from .brep import Brep, Edge, Face
from .geom import (Vec, add, axis_index, cross, dist_to_line, dot, norm, parallel, project_on_line, scale,
                   sub, unit, POS_TOL)

CHAMFER_MAX_WIDTH_RATIO = 0.25   # a chamfer is narrow: width < ratio × its longest support edge


@dataclass(frozen=True)
class Blend:
    kind: Literal["fillet", "chamfer"]
    faces: Tuple[int, ...]
    size: float
    distance2: Optional[float]
    support: Tuple[int, int]
    anchor: Optional[Vec]
    sharp_circle: Optional[Tuple[Vec, Vec, float]]


# ---- surfaces shared by several faces (STEP splits full cylinders/cones into halves) -------------

def same_surface(a: Face, b: Face) -> bool:
    if a.type != b.type or a.type not in ("cylinder", "cone", "torus", "sphere"):
        return False
    if a.origin is None or b.origin is None:
        return False
    if a.type == "sphere":
        return norm(sub(a.origin, b.origin)) <= POS_TOL and abs(a.radius - b.radius) <= POS_TOL
    if a.axis is None or b.axis is None or not parallel(a.axis, b.axis):
        return False
    if dist_to_line(b.origin, a.origin, unit(a.axis)) > POS_TOL:
        return False
    if a.type == "cylinder":
        return abs(a.radius - b.radius) <= POS_TOL
    if a.type == "cone":
        return _cone_matches(a, b)
    return (norm(sub(a.origin, b.origin)) <= POS_TOL and abs(a.radius - b.radius) <= POS_TOL
            and abs((a.minor_radius or 0) - (b.minor_radius or 0)) <= POS_TOL)


def _cone_matches(a: Face, b: Face) -> bool:
    # Same apex half-angle (up to axis direction) and the same radius at a common axial station.
    ha, hb = abs(a.half_angle or 0.0), abs(b.half_angle or 0.0)
    if abs(ha - hb) > 1e-6:
        return False
    return abs(cone_radius_at(a, b.origin) - cone_radius_at(b, b.origin)) <= POS_TOL


def cone_radius_at(f: Face, p: Vec) -> float:
    """Radius of cone f at the axial station of point p (sign-safe for either axis direction)."""
    import math
    d = unit(f.axis)
    t = project_on_line(p, f.origin, d)
    # SolidWorks' ConeParams cone NARROWS along +axis: r(t) = r0 − t·tan(half_angle) (decoded live).
    return f.radius - t * math.tan(f.half_angle or 0.0)


def surface_groups(brep: Brep) -> Dict[int, int]:
    """face index -> representative face index of its surface group (union over seam edges)."""
    parent = {f.i: f.i for f in brep.faces}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in brep.edges:
        a, b = e.faces
        if a < 0 or b < 0 or a == b:
            continue
        if same_surface(brep.face(a), brep.face(b)):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    return {i: find(i) for i in parent}


def members_of(groups: Dict[int, int]) -> Dict[int, Tuple[int, ...]]:
    out: Dict[int, List[int]] = {}
    for i, rep in groups.items():
        out.setdefault(rep, []).append(i)
    return {rep: tuple(sorted(m)) for rep, m in out.items()}


# ---- geometry helpers ---------------------------------------------------------------------------

def plane_offset(f: Face) -> float:
    return dot(f.normal, f.point)


def plane_intersection(a: Face, b: Face) -> Tuple[Vec, Vec]:
    """(point, unit direction) of the line where planes a and b meet."""
    na, nb = a.normal, b.normal
    d = cross(na, nb)
    denom = dot(d, d)
    p = scale(add(scale(cross(nb, d), plane_offset(a)), scale(cross(d, na), plane_offset(b))), 1.0 / denom)
    return p, unit(d)


def _boundary(brep: Brep, members: Iterable[int]) -> List[Edge]:
    ms = set(members)
    return [brep.edge(ei) for i in ms for ei in brep.face(i).edge_ids
            if 0 <= ei < len(brep.edges) and not set(brep.edge(ei).faces) <= ms]


def _other(e: Edge, members: Iterable[int]) -> int:
    ms = set(members)
    return e.faces[1] if e.faces[0] in ms else e.faces[0]


def _project_point(p: Vec, line_p: Vec, line_d: Vec) -> Vec:
    return add(line_p, scale(line_d, project_on_line(p, line_p, line_d)))


def _mean(points: List[Vec]) -> Vec:
    s = (0.0, 0.0, 0.0)
    for p in points:
        s = add(s, p)
    return scale(s, 1.0 / len(points))


def _axial_circle(axis_face: Face, plane: Face, radius: float) -> Tuple[Vec, Vec, float]:
    d = unit(axis_face.axis)
    t = (plane_offset(plane) - dot(plane.normal, axis_face.origin)) / dot(plane.normal, d)
    return add(axis_face.origin, scale(d, t)), d, radius


def _coaxial(a: Face, b: Face) -> bool:
    return (a.axis is not None and b.axis is not None and parallel(a.axis, b.axis)
            and dist_to_line(b.origin, a.origin, unit(a.axis)) <= POS_TOL)


# ---- fillets -------------------------------------------------------------------------------------

def _fillet(brep: Brep, groups: Dict[int, int], members: Tuple[int, ...]) -> Optional[Blend]:
    f = brep.face(members[0])
    tangent = [e for e in _boundary(brep, members) if e.convexity == "tangent"]
    support_reps = sorted({groups[_other(e, members)] for e in tangent})
    if len(support_reps) != 2:
        return None
    sa, sb = brep.face(support_reps[0]), brep.face(support_reps[1])
    size = f.minor_radius if f.type == "torus" else f.radius
    if sa.type == "plane" and sb.type == "plane":
        if parallel(sa.normal, sb.normal):
            return None
        lp, ld = plane_intersection(sa, sb)
        anchor = _project_point(_mean([e.mid for e in tangent]), lp, ld)
        return Blend("fillet", members, size, None, (sa.i, sb.i), anchor, None)
    plane, cyl = (sa, sb) if sa.type == "plane" else (sb, sa)
    if (plane.type == "plane" and cyl.type == "cylinder" and f.type == "torus"
            and _coaxial(f, cyl) and parallel(plane.normal, cyl.axis)):
        return Blend("fillet", members, size, None, (plane.i, cyl.i), None,
                     _axial_circle(cyl, plane, cyl.radius))
    return None


# ---- chamfers ------------------------------------------------------------------------------------

def _two_supports(brep: Brep, groups: Dict[int, int], members: Tuple[int, ...]):
    shared: Dict[int, List[Edge]] = {}
    for e in _boundary(brep, members):
        if e.convexity == "convex":
            shared.setdefault(groups[_other(e, members)], []).append(e)
    if len(shared) < 2:
        return None
    ranked = sorted(shared.items(), key=lambda kv: -sum(x.length or 0.0 for x in kv[1]))
    return ranked[0], ranked[1]


def _plane_chamfer(brep: Brep, groups: Dict[int, int], c: Face) -> Optional[Blend]:
    pair = _two_supports(brep, groups, (c.i,))
    if pair is None:
        return None
    (ra, ea), (rb, eb) = pair
    a, b = brep.face(ra), brep.face(rb)
    if a.type != "plane" or b.type != "plane" or parallel(a.normal, b.normal):
        return None
    # A chamfer breaks the edge between two AXIS-ALIGNED faces. Without this, every side of a
    # long hex/octagon bar passes as a "chamfer" and slips past the NOT_AXIS_ALIGNED refusal.
    if axis_index(a.normal) is None or axis_index(b.normal) is None:
        return None
    if parallel(c.normal, a.normal) or parallel(c.normal, b.normal):
        return None
    if dot(c.normal, a.normal) <= 0 or dot(c.normal, b.normal) <= 0:
        return None
    lp, ld = plane_intersection(a, b)
    ma, mb = _mean([e.mid for e in ea]), _mean([e.mid for e in eb])
    da, db = dist_to_line(ma, lp, ld), dist_to_line(mb, lp, ld)
    longest = max((e.length or 0.0) for e in ea + eb)
    if longest <= 0 or max(da, db) > CHAMFER_MAX_WIDTH_RATIO * longest:
        return None
    anchor = _project_point(_mean([ma, mb]), lp, ld)
    return Blend("chamfer", (c.i,), da, db if abs(da - db) > POS_TOL else None, (a.i, b.i), anchor, None)


def _cone_chamfer(brep: Brep, groups: Dict[int, int], members: Tuple[int, ...]) -> Optional[Blend]:
    cone = brep.face(members[0])
    pair = _two_supports(brep, groups, members)
    if pair is None:
        return None
    (ra, ea), (rb, eb) = pair
    a, b = brep.face(ra), brep.face(rb)
    plane, cyl = (a, b) if a.type == "plane" else (b, a)
    pe, ce = (ea, eb) if a.type == "plane" else (eb, ea)
    if plane.type != "plane" or cyl.type != "cylinder" or not _coaxial(cone, cyl):
        return None
    if not parallel(plane.normal, cyl.axis):
        return None
    centre, axis, radius = _axial_circle(cyl, plane, cyl.radius)
    d_plane = abs(radius - (pe[0].radius or radius))
    d_cyl = abs(project_on_line(ce[0].center or ce[0].mid, centre, axis))
    return Blend("chamfer", members, d_plane, d_cyl if abs(d_plane - d_cyl) > POS_TOL else None,
                 (plane.i, cyl.i), None, (centre, axis, radius))


# ---- entry ---------------------------------------------------------------------------------------

def find_blends(brep: Brep) -> Tuple[Blend, ...]:
    groups = surface_groups(brep)
    out: List[Blend] = []
    for rep, members in sorted(members_of(groups).items()):
        f = brep.face(rep)
        blend = None
        if f.type in ("cylinder", "torus"):
            blend = _fillet(brep, groups, members)
        elif f.type == "cone":
            blend = _cone_chamfer(brep, groups, members)
        elif f.type == "plane" and f.normal is not None:
            blend = _plane_chamfer(brep, groups, f)
        if blend is not None:
            out.append(blend)
    return tuple(out)


def blend_face_ids(blends: Iterable[Blend]) -> FrozenSet[int]:
    return frozenset(i for b in blends for i in b.faces)
