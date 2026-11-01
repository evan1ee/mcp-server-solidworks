"""Part classification: prismatic (2.5D machined) vs turned, or a refusal code.

Refusals, checked in this order: MULTIBODY → UNSUPPORTED_GEOMETRY (sphere / freeform faces) →
NOT_AXIS_ALIGNED (any plane normal or surface axis off X/Y/Z — v1 sketches only on the default datums).
TURNED: the curved faces sharing ONE axis line make up >= TURNED_SHARE of the LATERAL area (every face
that is not a plane perpendicular to that axis) and include an outward-facing (convex) cylinder.
Measuring against lateral area — not total area — keeps a plate with one big hole prismatic: its large
caps are perpendicular to the hole axis and would otherwise dominate.
"""
from dataclasses import dataclass
from typing import FrozenSet, List, Literal, Optional, Tuple

from .brep import Brep, Face
from .geom import (Vec, axis_index, dot, dist_to_line, foot_through_origin, parallel, sub, unit,
                   POS_TOL)

TURNED_SHARE = 0.8
_CURVED_AXIAL = ("cylinder", "cone", "torus")


@dataclass(frozen=True)
class Classification:
    part_class: Optional[Literal["prismatic", "turned"]]
    axis: Optional[int]                 # prismatic: primary axis; turned: the turn axis (0/1/2)
    turn_origin: Optional[Vec]          # turned: canonical point on the turn axis
    refusal: Optional[str]
    detail: str


def _refuse(code: str, detail: str) -> Classification:
    return Classification(None, None, None, code, detail)


def _misaligned_directions(brep: Brep, blend_faces: FrozenSet[int]) -> List[Vec]:
    found: List[Vec] = []
    for f in brep.faces:
        if f.i in blend_faces:
            continue  # a 45° chamfer face is legitimately off-axis
        d = f.normal if f.type == "plane" else f.axis
        if d is None or axis_index(d) is not None:
            continue
        if not any(parallel(d, g) for g in found):
            found.append(tuple(round(x, 4) for x in unit(d)))
    return found


def _is_convex_cylinder(f: Face) -> bool:
    """Normal points AWAY from the axis ⇒ an outside (shaft) surface, not a bore."""
    return dot(f.normal, sub(f.point, f.origin)) > 0


def _axis_groups(brep: Brep) -> List[Tuple[Vec, Vec, List[Face]]]:
    groups: List[Tuple[Vec, Vec, List[Face]]] = []
    for f in brep.faces:
        if f.type not in _CURVED_AXIAL or f.axis is None or f.origin is None:
            continue
        for o, d, members in groups:
            if parallel(d, f.axis) and dist_to_line(f.origin, o, d) <= POS_TOL:
                members.append(f)
                break
        else:
            groups.append((f.origin, unit(f.axis), [f]))
    return groups


def _try_turned(brep: Brep) -> Optional[Classification]:
    total = sum(f.area for f in brep.faces)
    best = None
    for o, d, members in _axis_groups(brep):
        caps = sum(f.area for f in brep.faces if f.type == "plane" and parallel(f.normal, d))
        lateral = total - caps
        share = sum(f.area for f in members) / lateral if lateral > 0 else 0.0
        has_outer = any(f.type == "cylinder" and _is_convex_cylinder(f) for f in members)
        if share >= TURNED_SHARE and has_outer and (best is None or share > best[0]):
            best = (share, o, d)
    if best is None:
        return None
    _, o, d = best
    return Classification("turned", axis_index(d), foot_through_origin(o, d), None,
                          "turned about axis %d (lateral share %.2f)" % (axis_index(d), best[0]))


def _primary_axis(brep: Brep, blend_faces: FrozenSet[int]) -> int:
    area = [0.0, 0.0, 0.0]
    for f in brep.faces:
        if f.type == "plane" and f.i not in blend_faces:
            area[axis_index(f.normal)] += f.area
    return max(range(3), key=lambda k: area[k])


def classify(brep: Brep, blend_faces: FrozenSet[int] = frozenset()) -> Classification:
    """blend_faces: faces find_blends() explained — excluded from the axis-alignment check."""
    if brep.body_count != 1:
        return _refuse("MULTIBODY", "body_count=%d — split it first (save_body_as_part)" % brep.body_count)
    bad = sorted({f.type for f in brep.faces if f.type not in ("plane",) + _CURVED_AXIAL})
    if bad:
        ids = [f.i for f in brep.faces if f.type in bad]
        return _refuse("UNSUPPORTED_GEOMETRY", "face types %s on faces %s" % (bad, ids))
    off = _misaligned_directions(brep, blend_faces)
    if off:
        return _refuse("NOT_AXIS_ALIGNED", "directions: %s are not parallel to X/Y/Z" % (off,))
    turned = _try_turned(brep)
    if turned is not None:
        return turned
    k = _primary_axis(brep, blend_faces)
    return Classification("prismatic", k, None, None, "prismatic along axis %d" % k)
