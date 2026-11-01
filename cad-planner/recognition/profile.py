"""Planar face loop → 2D sketch profile, with edge blends REMOVED (the sharp-cornered section).

A cap loop edge that borders a blend face is handled by what that blend joined:
  - the blend's supports include the cap (a chamfer/fillet ALONG the cap edge): the edge is replaced
    by the trace of the other support (the wall) in the sketch plane — a full line whose endpoints are
    re-derived from its neighbours;
  - otherwise (a blend between two walls crossing the cap, e.g. a vertical corner fillet): the edge is
    dropped and its neighbouring lines are extended to meet.
Returns None when the loop cannot be rebuilt (a blend between non-line neighbours) — the caller then
leaves those faces unexplained instead of guessing.
"""
import math
from typing import Dict, List, Optional, Sequence, Tuple

from .blends import Blend
from .brep import Brep, Edge, Face
from .features import PlaneFrame, Profile2D
from .geom import POS_TOL, cross, dist, unit

Pt = Tuple[float, float]


def _close2(a: Pt, b: Pt) -> bool:
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= POS_TOL


def _intersect(p1: Pt, d1: Pt, p2: Pt, d2: Pt) -> Optional[Pt]:
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-12:
        return None
    t = ((p2[0] - p1[0]) * d2[1] - (p2[1] - p1[1]) * d2[0]) / den
    return (p1[0] + t * d1[0], p1[1] + t * d1[1])


def order_loop(brep: Brep, edge_ids: Sequence[int]) -> List[Tuple[Edge, bool]]:
    """Chain a loop's edges head-to-tail: [(edge, forward)], forward ⇒ traverse start→end."""
    left = [brep.edge(i) for i in edge_ids]
    if not left:
        return []
    out = [(left.pop(0), True)]
    while left:
        tail = out[-1][0].end if out[-1][1] else out[-1][0].start
        for k, e in enumerate(left):
            if dist(e.start, tail) <= POS_TOL:
                out.append((left.pop(k), True))
                break
            if dist(e.end, tail) <= POS_TOL:
                out.append((left.pop(k), False))
                break
        else:
            return out + [(e, True) for e in left]   # not a single chain; caller validates closure
    return out


def _arc_dir(c: Pt, a: Pt, m: Pt) -> int:
    return 1 if (a[0] - c[0]) * (m[1] - c[1]) - (a[1] - c[1]) * (m[0] - c[0]) > 0 else -1


def loop_profile(brep: Brep, cap: Face, edge_ids: Sequence[int], frame: PlaneFrame,
                 blend_of_face: Dict[int, Blend], groups: Dict[int, int]) -> Optional[Profile2D]:
    chain = order_loop(brep, edge_ids)
    items: List[dict] = []
    for e, fwd in chain:
        other = e.faces[1] if e.faces[0] == cap.i else e.faces[0]
        blend = blend_of_face.get(other)
        if blend is not None:
            sup = [groups.get(s, s) for s in blend.support]
            if groups.get(cap.i, cap.i) in sup:
                wall = brep.face(blend.support[1] if sup[0] == groups.get(cap.i, cap.i) else blend.support[0])
                item = _wall_trace(wall, cap, frame)
                if item is None:
                    return None
                items.append(item)
            else:
                items.append({"t": "gap"})
            continue
        a, b = frame.to2d(e.start), frame.to2d(e.end)
        if not fwd:
            a, b = b, a
        if e.type == "circle" and e.center is not None:
            c = frame.to2d(e.center)
            if _close2(a, b):
                items.append({"t": "circle", "c": c, "r": e.radius})
            else:
                m = frame.to2d(e.pmid or e.mid)
                items.append({"t": "arc", "c": c, "a": a, "b": b, "r": e.radius, "dir": _arc_dir(c, a, m)})
        elif e.type == "line":
            items.append({"t": "line", "a": a, "b": b, "p": a, "d": (b[0] - a[0], b[1] - a[1])})
        else:
            return None
    return _finish(items)


def _wall_trace(wall: Face, cap: Face, frame: PlaneFrame) -> Optional[dict]:
    if wall.type == "plane":
        d3 = unit(cross(cap.normal, wall.normal))
        p = frame.to2d(wall.point)
        d = (d3[0] * frame.xdir[0] + d3[1] * frame.xdir[1] + d3[2] * frame.xdir[2],
             d3[0] * frame.ydir[0] + d3[1] * frame.ydir[1] + d3[2] * frame.ydir[2])
        return {"t": "line", "a": None, "b": None, "p": p, "d": d}
    if wall.type == "cylinder":   # chamfer/fillet around a hole rim: the full hole circle
        return {"t": "circle", "c": frame.to2d(wall.origin), "r": wall.radius}
    return None


def _finish(items: List[dict]) -> Optional[Profile2D]:
    real = [it for it in items if it["t"] != "gap"]
    if not real:
        return None
    if all(it["t"] in ("arc", "circle") for it in real):
        c0, r0 = real[0]["c"], real[0]["r"]
        if all(_close2(it["c"], c0) and abs(it["r"] - r0) <= POS_TOL for it in real):
            return ({"kind": "circle", "cx": c0[0], "cy": c0[1], "diameter": 2.0 * r0},)
    # Joints that need re-deriving: after a gap, or touching a line with unknown endpoints.
    n = len(items)
    seq = []
    for k, it in enumerate(items):
        if it["t"] == "gap":
            continue
        gap_before = items[k - 1]["t"] == "gap"
        seq.append((it, gap_before))
    m = len(seq)
    for k in range(m):
        cur, _ = seq[k]
        nxt, gap = seq[(k + 1) % m]
        loose = gap or cur.get("b") is None or nxt.get("a") is None
        if not loose:
            continue
        if cur["t"] != "line" or nxt["t"] != "line":
            return None
        x = _intersect(cur["p"], cur["d"], nxt["p"], nxt["d"])
        if x is None:
            return None
        cur["b"], nxt["a"] = x, x
    prims = []
    for it, _ in seq:
        if it["t"] == "line":
            if _close2(it["a"], it["b"]):
                continue
            prims.append({"kind": "line", "x1": it["a"][0], "y1": it["a"][1], "x2": it["b"][0], "y2": it["b"][1]})
        elif it["t"] == "arc":
            prims.append({"kind": "arc", "cx": it["c"][0], "cy": it["c"][1], "x1": it["a"][0], "y1": it["a"][1],
                          "x2": it["b"][0], "y2": it["b"][1], "dir": it["dir"]})
        elif it["t"] == "circle":
            prims.append({"kind": "circle", "cx": it["c"][0], "cy": it["c"][1], "diameter": 2.0 * it["r"]})
    return tuple(_round(p) for p in prims)


def _round(p: dict) -> dict:
    return {k: (round(v, 9) if isinstance(v, float) else v) for k, v in p.items()}


def signature(profile: Profile2D) -> Tuple:
    """Order-independent identity of a closed profile (for de-duplicating the same contour seen on
    several caps)."""
    keys = []
    for p in profile:
        if p["kind"] == "circle":
            keys.append(("c", round(p["cx"], 5), round(p["cy"], 5), round(p["diameter"], 5)))
        else:
            keys.append(tuple(sorted([(round(p["x1"], 5), round(p["y1"], 5)), (round(p["x2"], 5), round(p["y2"], 5))])))
    return tuple(sorted(keys, key=repr))
