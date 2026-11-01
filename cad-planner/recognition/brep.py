"""Immutable model of the analyze_model(analysis_type='brep') JSON (single solid body)."""
from dataclasses import dataclass, replace
from typing import Optional, Tuple

from .geom import POS_TOL, Vec, cross, dist, dot, vec


@dataclass(frozen=True)
class Loop:
    outer: bool
    edges: Tuple[int, ...]


@dataclass(frozen=True)
class Face:
    i: int
    type: str                       # plane | cylinder | cone | sphere | torus | other
    area: float
    point: Optional[Vec]
    normal: Optional[Vec]           # OUTWARD unit normal at `point`
    origin: Optional[Vec] = None    # axis point (cylinder/cone/torus) or centre (sphere)
    axis: Optional[Vec] = None
    radius: Optional[float] = None  # cylinder/sphere radius, cone radius at origin, torus major radius
    half_angle: Optional[float] = None
    minor_radius: Optional[float] = None
    loops: Tuple[Loop, ...] = ()

    @property
    def edge_ids(self) -> Tuple[int, ...]:
        return tuple(e for lp in self.loops for e in lp.edges)


@dataclass(frozen=True)
class Edge:
    i: int
    type: str                       # line | circle | other
    start: Vec
    end: Vec
    mid: Vec                        # analyze_model(edges)'s mid — what the resolver matches
    length: Optional[float]
    faces: Tuple[int, int]
    convexity: str                  # convex | concave | tangent | unknown
    center: Optional[Vec] = None
    axis: Optional[Vec] = None
    radius: Optional[float] = None
    pmid: Optional[Vec] = None      # on-curve point at the parametric midpoint (arc side/sense)


@dataclass(frozen=True)
class Brep:
    body_count: int
    bbox: Optional[Tuple[float, ...]]
    faces: Tuple[Face, ...]
    edges: Tuple[Edge, ...]

    def face(self, i: int) -> Face:
        return self.faces[i]

    def edge(self, i: int) -> Edge:
        return self.edges[i]

    def neighbours(self, face_i: int) -> Tuple[int, ...]:
        out = []
        for ei in self.faces[face_i].edge_ids:
            if 0 <= ei < len(self.edges):
                for f in self.edges[ei].faces:
                    if f != face_i and f >= 0 and f not in out:
                        out.append(f)
        return tuple(out)

    def edges_between(self, fa: int, fb: int) -> Tuple[Edge, ...]:
        return tuple(e for e in self.edges if set(e.faces) == {fa, fb})


def _opt_vec(d: dict, key: str) -> Optional[Vec]:
    v = d.get(key)
    return vec(v) if v is not None else None


def _face(d: dict) -> Face:
    return Face(
        i=int(d["i"]), type=d.get("type", "other"), area=float(d.get("area") or 0.0),
        point=_opt_vec(d, "point"), normal=_opt_vec(d, "normal"), origin=_opt_vec(d, "origin"),
        axis=_opt_vec(d, "axis"), radius=d.get("radius"), half_angle=d.get("half_angle"),
        minor_radius=d.get("minor_radius"),
        loops=tuple(Loop(bool(lp.get("outer")), tuple(int(e) for e in lp.get("edges", [])))
                    for lp in d.get("loops", [])))


def _edge(d: dict) -> Edge:
    fs = list(d.get("faces") or [-1, -1]) + [-1, -1]
    return Edge(
        i=int(d["i"]), type=d.get("type", "other"), start=vec(d["start"]), end=vec(d["end"]),
        mid=vec(d.get("mid") or d["start"]), length=d.get("length"),
        faces=(int(fs[0]), int(fs[1])), convexity=d.get("convexity", "unknown"),
        center=_opt_vec(d, "center"), axis=_opt_vec(d, "axis"), radius=d.get("radius"),
        pmid=_opt_vec(d, "pmid"))


def _chain_loops(face: Face, edges: Tuple[Edge, ...]) -> Tuple[Loop, ...]:
    """Group a face's edges into closed loops by shared endpoints; for a plane the loop enclosing the
    largest area is OUTER. (The C# read skips walking loops — each coedge proxy is a slow COM round
    trip — so loops are rebuilt here from edge adjacency + geometry.)"""
    mine = [e for e in edges if face.i in e.faces]
    parent = {e.i: e.i for e in mine}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in mine:
        for b in mine:
            if a.i < b.i and any(dist(p, q) <= POS_TOL for p in (a.start, a.end) for q in (b.start, b.end)):
                parent[find(a.i)] = find(b.i)
    groups: dict = {}
    for e in mine:
        groups.setdefault(find(e.i), []).append(e)
    loops = [tuple(sorted(e.i for e in g)) for g in groups.values()]
    if face.type != "plane" or face.normal is None or len(loops) == 1:
        return tuple(Loop(True, lp) for lp in loops)
    by_id = {e.i: e for e in mine}
    areas = [_loop_area(face.normal, [by_id[i] for i in lp]) for lp in loops]
    outer = max(range(len(loops)), key=lambda k: areas[k])
    return tuple(Loop(k == outer, lp) for k, lp in enumerate(loops))


def _loop_area(normal: Vec, loop_edges) -> float:
    """|area| of the polygon through each edge's start/pmid/end, projected on the plane — an
    approximation that only has to RANK loops (the outer loop encloses all others)."""
    pts = []
    for e in loop_edges:
        pts.extend([e.start, e.pmid or e.mid, e.end])
    u = cross(normal, (1.0, 0.0, 0.0)) if abs(normal[0]) < 0.9 else cross(normal, (0.0, 1.0, 0.0))
    v = cross(normal, u)
    xy = [(dot(p, u), dot(p, v)) for p in pts]
    cx = sum(x for x, _ in xy) / len(xy)
    cy = sum(y for _, y in xy) / len(xy)
    import math
    xy.sort(key=lambda q: math.atan2(q[1] - cy, q[0] - cx))
    return abs(sum(xy[k][0] * xy[k - 1][1] - xy[k - 1][0] * xy[k][1] for k in range(len(xy)))) / 2.0


def parse_brep(obj: dict) -> Brep:
    bbox = obj.get("bbox")
    faces = tuple(_face(f) for f in obj.get("faces", []))
    edges = tuple(_edge(e) for e in obj.get("edges", []))
    faces = tuple(f if f.loops else replace(f, loops=_chain_loops(f, edges)) for f in faces)
    return Brep(
        body_count=int(obj.get("body_count", 0)),
        bbox=tuple(float(x) for x in bbox) if bbox else None,
        faces=faces, edges=edges)
