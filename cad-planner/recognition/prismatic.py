"""2.5D machined-part decomposition along the primary axis k (blends already removed logically).

  base     : the largest cap outer loop, boss from the lowest cap level up to the highest
  floors   : every +k cap below the top → its outer loop (+ island inner loops) cut DOWN from the top
             to the floor (pockets, counterbores, steps, blind holes)
  ceilings : every −k cap above the bottom → the same, cut UP from the bottom
  through  : convex inner loops of the extreme caps that no floor/ceiling explains → through-all cut
  side     : concave cylinders whose axis is ⟂ k → circle cut on the datum ⟂ that axis, mid-plane
             at the hole's middle station, depth = its span (+10% per open end)
Every face is then accounted for (cap, wall bordering a used loop, side hole, blend); the rest is
returned as unexplained.
"""
from typing import Callable, Dict, List, Optional, Set, Tuple

from .blends import Blend, members_of, surface_groups
from .brep import Brep, Face
from .classify import Classification
from .features import DATUM_FOR_AXIS, ExtrudeFeat, PlaneFrame, Profile2D, profile_area
from .geom import POS_TOL, axis_index, dot, parallel, project_on_line, unit
from .profile import loop_profile, signature

OPEN_END_MARGIN = 0.1   # fraction of the span added beyond an open side-hole end


def _caps(brep: Brep, k: int, blend_faces: Set[int]) -> Tuple[List[Face], List[Face]]:
    up, down = [], []
    for f in brep.faces:
        if f.type == "plane" and f.i not in blend_faces and axis_index(f.normal) == k:
            (up if f.normal[k] > 0 else down).append(f)
    return up, down


def _inner_loops(brep: Brep, cap: Face, convexity: str):
    for lp in cap.loops:
        if not lp.outer and lp.edges and all(brep.edge(e).convexity == convexity for e in lp.edges):
            yield lp


def _outer(cap: Face):
    return next(lp for lp in cap.loops if lp.outer)


class _Ctx:
    def __init__(self, brep: Brep, k: int, blends: Tuple[Blend, ...]):
        self.brep = brep
        self.k = k
        self.groups = surface_groups(brep)
        self.blend_of_face: Dict[int, Blend] = {i: b for b in blends for i in b.faces}
        self.used_loop_edges: Set[int] = set()

    def frame(self, level: float) -> PlaneFrame:
        return PlaneFrame(DATUM_FOR_AXIS[self.k], level)

    def profile(self, cap: Face, edge_ids, frame: PlaneFrame) -> Optional[Profile2D]:
        prof = loop_profile(self.brep, cap, edge_ids, frame, self.blend_of_face, self.groups)
        if prof is not None:
            self.used_loop_edges.update(edge_ids)
        return prof


def _with_islands(ctx: _Ctx, cap: Face, frame: PlaneFrame) -> Optional[Profile2D]:
    prof = ctx.profile(cap, _outer(cap).edges, frame)
    if prof is None:
        return None
    for lp in _inner_loops(ctx.brep, cap, "concave"):
        island = ctx.profile(cap, lp.edges, frame)
        if island is not None:
            prof = prof + island
    return prof


def decompose_prismatic(brep: Brep, cls: Classification,
                        blends: Tuple[Blend, ...]) -> Tuple[List[ExtrudeFeat], List[int]]:
    k = cls.axis
    blend_faces = {i for b in blends for i in b.faces}
    ctx = _Ctx(brep, k, blends)
    up, down = _caps(brep, k, blend_faces)
    if not up or not down:
        return [], [f.i for f in brep.faces]
    hmin = min(f.point[k] for f in down)
    hmax = max(f.point[k] for f in up)
    base_frame = ctx.frame(hmin)

    # base: the largest cap outer loop is the silhouette
    best = None
    for cap in up + down:
        prof = ctx.profile(cap, _outer(cap).edges, base_frame)
        if prof is not None and (best is None or profile_area(prof) > profile_area(best)):
            best = prof
    feats: List[ExtrudeFeat] = []
    if best is None:
        return feats, [f.i for f in brep.faces]
    feats.append(ExtrudeFeat("boss", base_frame, best, "blind", hmax - hmin, True, "base"))

    explained_sigs = set()
    top_frame = ctx.frame(hmax)
    for cap in up:
        h = cap.point[k]
        if h < hmax - POS_TOL:
            prof = _with_islands(ctx, cap, top_frame)
            if prof is not None:
                explained_sigs.add(signature(prof[:_outer_len(ctx, cap, top_frame)]))
                feats.append(ExtrudeFeat("cut", top_frame, prof, "blind", hmax - h, False, "floor %d" % cap.i))
    for cap in down:
        h = cap.point[k]
        if h > hmin + POS_TOL:
            prof = _with_islands(ctx, cap, base_frame)
            if prof is not None:
                explained_sigs.add(signature(prof[:_outer_len(ctx, cap, base_frame)]))
                feats.append(ExtrudeFeat("cut", base_frame, prof, "blind", h - hmin, True, "ceiling %d" % cap.i))

    through_sigs = set()
    extreme = [c for c in up if c.point[k] >= hmax - POS_TOL] + [c for c in down if c.point[k] <= hmin + POS_TOL]
    for cap in extreme:
        for lp in _inner_loops(brep, cap, "convex"):
            prof = ctx.profile(cap, lp.edges, top_frame)
            if prof is None:
                continue
            sig = signature(prof)
            if sig in explained_sigs or sig in through_sigs:
                continue
            through_sigs.add(sig)
            feats.append(ExtrudeFeat("cut", top_frame, prof, "through_all", None, False, "through"))

    side_feats, side_faces = side_holes(brep, ctx.groups, blend_faces, lambda f: axis_index(f.axis) == k)
    feats.extend(side_feats)
    feats = [feats[0]] + sorted(feats[1:], key=lambda f: -profile_area(f.profile))
    return feats, _unexplained(ctx, up + down, side_faces, blend_faces)


def _outer_len(ctx: _Ctx, cap: Face, frame: PlaneFrame) -> int:
    prof = loop_profile(ctx.brep, cap, _outer(cap).edges, frame, ctx.blend_of_face, ctx.groups)
    return len(prof) if prof else 0


def side_holes(brep: Brep, groups: Dict[int, int], blend_faces: Set[int],
               skip: Callable[[Face], bool]) -> Tuple[List[ExtrudeFeat], Set[int]]:
    """Concave (hole) cylinders the caller's base feature does not produce (skip(face) is False) →
    a circle cut on the datum ⟂ the hole axis, MID-PLANE at the hole's middle station, depth = span,
    + OPEN_END_MARGIN·span beyond each open (floorless) end. Returns (features, explained faces)."""
    feats, faces = [], set()
    for rep, members in members_of(groups).items():
        f = brep.face(rep)
        if f.type != "cylinder" or rep in blend_faces or f.axis is None or skip(f):
            continue
        j = axis_index(f.axis)
        if j is None or dot(f.normal, unit(_sub(f.point, f.origin))) >= 0:
            continue   # off-axis (refused earlier) or convex (a boss, not a hole)
        a = unit(f.axis)
        ts = []
        for m in members:
            for ei in brep.face(m).edge_ids:
                e = brep.edge(ei)
                ts.extend(project_on_line(p, f.origin, a) for p in (e.start, e.end, e.pmid or e.mid))
        t0, t1 = min(ts), max(ts)
        closed = [False, False]
        for m in members:
            for ei in brep.face(m).edge_ids:
                e = brep.edge(ei)
                nb = e.faces[1] if e.faces[0] == m else e.faces[0]
                g = brep.face(nb) if nb >= 0 else None
                if (g is not None and nb not in members and g.type == "plane"
                        and parallel(g.normal, a) and e.convexity == "concave"):
                    t = project_on_line(g.point, f.origin, a)
                    closed[0 if abs(t - t0) < abs(t - t1) else 1] = True
                    faces.add(nb)   # the hole's flat floor
        span = t1 - t0
        lo = t0 - (0.0 if closed[0] else OPEN_END_MARGIN * span)
        hi = t1 + (0.0 if closed[1] else OPEN_END_MARGIN * span)
        mid_pt = tuple(f.origin[i] + a[i] * (lo + hi) / 2.0 for i in range(3))
        frame = PlaneFrame(DATUM_FOR_AXIS[j], mid_pt[j])
        c = frame.to2d(mid_pt)
        prof = ({"kind": "circle", "cx": round(c[0], 9), "cy": round(c[1], 9), "diameter": 2.0 * f.radius},)
        feats.append(ExtrudeFeat("cut", frame, prof, "mid_plane", hi - lo, True, "hole %d" % rep))
        faces.update(members)
    return feats, faces


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _unexplained(ctx: _Ctx, caps: List[Face], side_faces: Set[int], blend_faces: Set[int]) -> List[int]:
    brep = ctx.brep
    explained = {c.i for c in caps} | side_faces | blend_faces
    for ei in ctx.used_loop_edges:
        explained.update(f for f in brep.edge(ei).faces if f >= 0)
    groups = ctx.groups
    rep_explained = {groups[i] for i in explained}
    return sorted(f.i for f in brep.faces if groups[f.i] not in rep_explained)
