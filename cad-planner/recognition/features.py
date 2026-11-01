"""Intermediate, CAD-neutral feature records the decomposers produce and emit.py lowers to IR.

PlaneFrame carries the MEASURED sketch frame SolidWorks reports for each default datum (calibrated
live 2026-11-01); emitting exactly that frame means pycompiler applies no 2D transform, which matters
for revolve axes (their coordinates are not transformed).
"""
import math
from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

from .geom import Vec, add, dot, scale, sub

Profile2D = Tuple[Dict, ...]   # IR profile primitives (line/arc/circle) in the frame's 2D coordinates

# datum -> (xdir, ydir, normal) as SolidWorks measures it on the default planes.
_DATUM_FRAMES = {
    "front": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "top": ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0)),
    "right": ((0.0, 0.0, -1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)),
}
# world axis index (0=X,1=Y,2=Z) -> the datum whose normal is that axis
DATUM_FOR_AXIS = {0: "right", 1: "top", 2: "front"}


@dataclass(frozen=True)
class PlaneFrame:
    datum: Literal["front", "top", "right"]
    offset: float

    @property
    def xdir(self) -> Vec:
        return _DATUM_FRAMES[self.datum][0]

    @property
    def ydir(self) -> Vec:
        return _DATUM_FRAMES[self.datum][1]

    @property
    def normal(self) -> Vec:
        return _DATUM_FRAMES[self.datum][2]

    @property
    def origin(self) -> Vec:
        return scale(self.normal, self.offset)

    def to2d(self, p: Vec) -> Tuple[float, float]:
        d = sub(p, self.origin)
        return (dot(d, self.xdir), dot(d, self.ydir))

    def to3d(self, u: float, v: float) -> Vec:
        return add(self.origin, add(scale(self.xdir, u), scale(self.ydir, v)))


@dataclass(frozen=True)
class ExtrudeFeat:
    op: Literal["boss", "cut"]
    frame: PlaneFrame
    profile: Profile2D
    end: Literal["blind", "through_all", "mid_plane"]
    depth: Optional[float]
    toward_plus_normal: bool
    note: str = ""


@dataclass(frozen=True)
class RevolveFeat:
    frame: PlaneFrame
    profile: Profile2D
    axis2d: Tuple[float, float, float, float]
    note: str = ""


@dataclass(frozen=True)
class EdgeFeat:
    kind: Literal["fillet", "chamfer"]
    size: float
    distance2: Optional[float]
    anchors: Tuple[Vec, ...]


def profile_area(profile: Profile2D) -> float:
    """Approximate enclosed area (circles exact; polygons through line/arc endpoints) — for ORDERING."""
    total = 0.0
    pts: List[Tuple[float, float]] = []
    for p in profile:
        if p.get("construction"):
            continue
        if p["kind"] == "circle":
            total += math.pi * (p["diameter"] / 2.0) ** 2
        else:
            pts.append((p["x1"], p["y1"]))
    if len(pts) >= 3:
        total += abs(sum(pts[k][0] * pts[k - 1][1] - pts[k - 1][0] * pts[k][1] for k in range(len(pts)))) / 2.0
    return total
