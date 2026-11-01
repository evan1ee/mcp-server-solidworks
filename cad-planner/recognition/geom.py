"""Tiny 3D vector toolkit + the recognizer's tolerances (stdlib only — no numpy by design)."""
import math
from typing import Optional, Sequence, Tuple

Vec = Tuple[float, float, float]

POS_TOL = 5e-5   # 50 µm — the same position tolerance compare_parts' face diff uses
DIR_TOL = 1e-6   # |a·b| >= 1 - DIR_TOL  ⇒ parallel

AXES: Tuple[Vec, Vec, Vec] = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def vec(v: Sequence[float]) -> Vec:
    return (float(v[0]), float(v[1]), float(v[2]))


def add(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def scale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def norm(a: Vec) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Vec) -> Vec:
    n = norm(a)
    if n < 1e-12:
        raise ValueError("zero-length vector")
    return scale(a, 1.0 / n)


def dist(a: Vec, b: Vec) -> float:
    return norm(sub(a, b))


def parallel(a: Vec, b: Vec) -> bool:
    return abs(dot(unit(a), unit(b))) >= 1.0 - DIR_TOL


def axis_index(v: Vec) -> Optional[int]:
    """0/1/2 when v is parallel to X/Y/Z, else None."""
    for k, ax in enumerate(AXES):
        if parallel(v, ax):
            return k
    return None


def project_on_line(p: Vec, origin: Vec, direction: Vec) -> float:
    """Signed coordinate of p along the line (origin, unit direction)."""
    return dot(sub(p, origin), direction)


def dist_to_line(p: Vec, origin: Vec, direction: Vec) -> float:
    d = sub(p, origin)
    t = dot(d, direction)
    return norm(sub(d, scale(direction, t)))


def same_line(o1: Vec, d1: Vec, o2: Vec, d2: Vec) -> bool:
    return parallel(d1, d2) and dist_to_line(o2, o1, unit(d1)) <= POS_TOL


def foot_through_origin(origin: Vec, direction: Vec) -> Vec:
    """The line's point closest to the world origin — a canonical point for an axis line."""
    d = unit(direction)
    return sub(origin, scale(d, dot(origin, d)))


def close(a: Sequence[float], b: Sequence[float], tol: float = POS_TOL) -> bool:
    return dist(vec(a), vec(b)) <= tol
