"""Turned-part decomposition: ONE revolve of the sharp-cornered half-section + hole cuts.

The half-section lives in the (axial z, radial r) plane: coaxial cylinders give constant-r segments,
planes ⟂ the axis give constant-z segments, non-blend cones give slanted ones. A coaxial blend
(end chamfer / shoulder fillet) is removed by moving its two boundary points onto the sharp corner it
replaced. A section that reaches the axis is closed along it. The sketch sits on the default datum
CONTAINING the axis, in that datum's measured frame (revolve axis coordinates are not transformed).
"""
import math
from typing import Dict, List, Optional, Set, Tuple

from .blends import Blend, members_of, surface_groups
from .brep import Brep, Face
from .classify import Classification
from .features import ExtrudeFeat, PlaneFrame, RevolveFeat
from .geom import AXES, POS_TOL, Vec, add, dist_to_line, dot, parallel, scale, sub, unit
from .prismatic import side_holes

# turn axis -> (datum containing it, index of the datum's normal axis, radial direction in that datum)
_TURN_DATUM = {0: ("front", 2, (0.0, 1.0, 0.0)), 1: ("front", 2, (1.0, 0.0, 0.0)), 2: ("top", 1, (1.0, 0.0, 0.0))}
AXIS_MARGIN = 0.1   # construction centerline overhang, fraction of the part length

ZR = Tuple[float, float]


def _key(p: ZR) -> ZR:
    return (round(p[0], 6), round(p[1], 6))


class _Axis:
    def __init__(self, origin: Vec, j: int):
        self.o, self.e = origin, AXES[j]

    def z(self, p: Vec) -> float:
        return dot(sub(p, self.o), self.e)

    def r(self, p: Vec) -> float:
        return dist_to_line(p, self.o, self.e)

    def on_axis(self, f: Face) -> bool:
        return (f.axis is not None and f.origin is not None and parallel(f.axis, self.e)
                and dist_to_line(f.origin, self.o, self.e) <= POS_TOL)

    def circles(self, brep: Brep, members) -> List[ZR]:
        out = []
        for m in members:
            for ei in brep.face(m).edge_ids:
                e = brep.edge(ei)
                if (e.type == "circle" and e.center is not None and e.axis is not None
                        and parallel(e.axis, self.e) and dist_to_line(e.center, self.o, self.e) <= POS_TOL):
                    p = (self.z(e.center), e.radius)
                    if all(_key(p) != _key(q) for q in out):
                        out.append(p)
        return out


def _segments(brep: Brep, ax: _Axis, groups, blend_faces: Set[int]) -> Tuple[List[Tuple[ZR, ZR]], Set[int]]:
    segs, used = [], set()
    for rep, members in members_of(groups).items():
        f = brep.face(rep)
        if rep in blend_faces:
            continue
        pts = ax.circles(brep, members)
        if f.type in ("cylinder", "cone") and ax.on_axis(f) and len(pts) >= 2:
            zs = sorted(pts)
            segs.append((zs[0], zs[-1]))
            used.update(members)
        elif f.type == "plane" and parallel(f.normal, ax.e) and pts:
            z = ax.z(f.point)
            rs = sorted(r for _, r in pts)
            segs.append(((z, rs[0] if len(rs) > 1 else 0.0), (z, rs[-1])))
            used.update(members)
    return segs, used


def _sharpen(brep: Brep, ax: _Axis, blends: Tuple[Blend, ...], segs):
    """Move every endpoint that lies on a coaxial blend's boundary to that blend's sharp corner."""
    moves: Dict[ZR, ZR] = {}
    for b in blends:
        if b.sharp_circle is None:
            continue
        centre, axis, radius = b.sharp_circle
        if not (parallel(axis, ax.e) and dist_to_line(centre, ax.o, ax.e) <= POS_TOL):
            continue
        sharp = (ax.z(centre), radius)
        for p in ax.circles(brep, b.faces):
            moves[_key(p)] = sharp
    return [(moves.get(_key(a), a), moves.get(_key(b), b)) for a, b in segs]


def _chain(segs) -> Optional[List[ZR]]:
    """Order segments into one closed polygon, closing along the axis (r = 0) when the section
    touches it. Returns the vertex list, or None if the section is not one closed loop."""
    segs = [s for s in segs if _key(s[0]) != _key(s[1])]
    axis_pts = [p for s in segs for p in s if abs(p[1]) <= POS_TOL]
    if len(axis_pts) == 2:
        segs.append((axis_pts[0], axis_pts[1]))
    if not segs:
        return None
    poly = [segs[0][0], segs[0][1]]
    rest = segs[1:]
    while rest:
        tail = _key(poly[-1])
        for k, (a, b) in enumerate(rest):
            if _key(a) == tail:
                poly.append(b)
                rest.pop(k)
                break
            if _key(b) == tail:
                poly.append(a)
                rest.pop(k)
                break
        else:
            return None
    return poly[:-1] if _key(poly[0]) == _key(poly[-1]) else None


def decompose_turned(brep: Brep, cls: Classification,
                     blends: Tuple[Blend, ...]) -> Tuple[Optional[RevolveFeat], List[ExtrudeFeat], List[int]]:
    j = cls.axis
    ax = _Axis(cls.turn_origin, j)
    groups = surface_groups(brep)
    blend_faces = {i for b in blends for i in b.faces}
    segs, used = _segments(brep, ax, groups, blend_faces)
    poly = _chain(_sharpen(brep, ax, blends, segs))
    datum, normal_idx, radial = _TURN_DATUM[j]
    frame = PlaneFrame(datum, ax.o[normal_idx])

    cuts, hole_faces = side_holes(brep, groups, blend_faces, ax.on_axis)
    explained = used | hole_faces | blend_faces
    unexplained = sorted(f.i for f in brep.faces if groups[f.i] not in {groups[i] for i in explained})
    if poly is None:
        return None, cuts, sorted(set(unexplained) | used)

    def to2d(p: ZR) -> Tuple[float, float]:
        return frame.to2d(add(ax.o, add(scale(ax.e, p[0]), scale(radial, p[1]))))

    prims = []
    for k in range(len(poly)):
        a, b = to2d(poly[k]), to2d(poly[(k + 1) % len(poly)])
        prims.append({"kind": "line", "x1": round(a[0], 9), "y1": round(a[1], 9),
                      "x2": round(b[0], 9), "y2": round(b[1], 9)})
    zs = [p[0] for p in poly]
    margin = AXIS_MARGIN * (max(zs) - min(zs))
    # The centerline runs from the HIGH axial end to the LOW one — the direction the circle-mid rule
    # (mid = centre − r·N) was calibrated with; the revolve's circle parametrisation follows it.
    a, b = to2d((max(zs) + margin, 0.0)), to2d((min(zs) - margin, 0.0))
    axis2d = (round(a[0], 9), round(a[1], 9), round(b[0], 9), round(b[1], 9))
    prims.insert(0, {"kind": "line", "x1": axis2d[0], "y1": axis2d[1], "x2": axis2d[2], "y2": axis2d[3],
                     "construction": True})
    return RevolveFeat(frame, tuple(prims), axis2d, "turned base"), cuts, unexplained
