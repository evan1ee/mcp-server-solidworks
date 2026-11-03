"""Deterministic dimension planner for `dimension_drawing(strategy='full')`.

Pure functions: turns part facts + drawing-view placement into an ordered list of
`add_drawing_dimension_ex` parameter dicts. Never imports server.py; the MCP tool in server.py
executes the returned plan through its own `_call`.

Conventions
-----------
* All lengths are meters. A view's `pos` is the sheet position of its BOUNDING-BOX CENTER (this is what
  SolidWorks reports as view position), `scale` its view scale.
* Front view: local x/y are model x/y relative to the bbox center.
* Depth views (top / right): the flange (`depth.flange`) sits on the MAX side (up in the top view,
  right in the right view); the boss extends to the MIN side. Positions are relative to the bbox center.
* Offsets use tiers (`assign_tiers`): on one side of a view, a dimension whose span encloses another's goes
  on a farther tier, disjoint spans share a tier, so extension lines never cross another dimension line.
* Interior bolt circles are `measure_only` (verified, then removed) and written into the hole callout;
  leaders pick their angle/text position from alternatives (`_alts`) so they cross nothing.

Facts schema (see tests for an example)::

    {"width": W, "height": H,
     "boss": {"diameter": D},                 # optional, coaxial with the origin
     "bore": {"diameter": d},                 # optional
     "hole_groups": [{"diameter": d, "centers": [[x, y], ...],
                      "depth": None | m, "role": "mount" | "pcd" | "cbore"}],
     "corner_radius": r,                      # optional, plate corner fillet
     "slot": {"x": [x1, x2], "y": [y1, y2]},  # optional, rectangular pocket (front-view local)
     "rib": {"thickness": t, "z_end": z},     # optional, rib seen as a strip in the depth view
     "depth": {"total": T, "flange": F, "boss": B, "boss_width": D, "boss_chamfer": c}}  # chamfer optional

Hole-group order sets the callout rows (top to bottom); each callout reads `<n>×Ø<d> THRU|DEEP <depth>`.
"""
from __future__ import annotations

import math
from typing import Any

TIER1 = 0.012
TIER2 = 0.026
TIER_STEP = TIER2 - TIER1
LEADER = 0.036
_EPS = 1e-9


def _fmt_precision(value: float) -> int:
    """Decimal places (in mm) needed to print `value` without trailing zeros: 0.12 -> 0, 0.0015 -> 1."""
    mm = round(value * 1000.0, 4)
    return 0 if abs(mm - round(mm)) < 1e-9 else 1


def _dim(x1, y1, tx, ty, expected, *, x2=None, y2=None, orientation="auto", prefix="", suffix="", label="",
         side=None, edge=None, measure_only=False):
    d: dict[str, Any] = {
        "x1": round(x1, 6), "y1": round(y1, 6),
        "text_x": round(tx, 6), "text_y": round(ty, 6),
        "orientation": orientation, "expected_value": round(expected, 6),
        "prefix": prefix, "suffix": suffix, "precision": _fmt_precision(expected), "label": label,
    }
    if x2 is not None and y2 is not None:
        d["x2"] = round(x2, 6)
        d["y2"] = round(y2, 6)
    if side:
        d["_side"], d["_edge"] = side, edge  # tiered dimension: assign_tiers sets its offset from `edge`
    if measure_only:
        d["measure_only"] = True
    return d


def _circle_pick(cx, cy, r, scale, angle_deg=0.0):
    a = math.radians(angle_deg)
    return cx + scale * r * math.cos(a), cy + scale * r * math.sin(a)


def _bbox(view_pos, scale, w, h):
    cx, cy = view_pos
    return cx - scale * w / 2, cx + scale * w / 2, cy - scale * h / 2, cy + scale * h / 2


def _front_overall(f, pos, s):
    left, right, bottom, top = _bbox(pos, s, f["width"], f["height"])
    cx, cy = pos
    width = _dim(left, cy, cx, top + TIER2, f["width"], x2=right, y2=cy,
                 orientation="horizontal", label="overall width", side="top", edge=top)
    # picked on the side edges, but the extension lines start at the silhouette corners
    width["_ext_from"] = ((left, top), (right, top))
    return [
        width,
        _dim(cx - 0.03 * s, bottom, left - TIER2, cy, f["height"], x2=cx - 0.03 * s, y2=top,
             orientation="vertical", label="overall height", side="left", edge=left),
    ]


def _unique(values, tol=1e-6):
    out: list[float] = []
    for v in sorted(values):
        if not out or abs(v - out[-1]) > tol:
            out.append(v)
    return out


def _mount_pitch(group, pos, s, left, top):
    """Center-to-center pitch of a rectangular hole pattern (one dimension per axis)."""
    cx, cy = pos
    r = group["diameter"] / 2
    xs = _unique(c[0] for c in group["centers"])
    ys = _unique(c[1] for c in group["centers"])
    out = []
    if len(xs) == 2:
        y = ys[-1]
        px1, py1 = _circle_pick(cx + s * xs[0], cy + s * y, r, s)
        px2, py2 = _circle_pick(cx + s * xs[1], cy + s * y, r, s)
        out.append(_dim(px1, py1, cx, top + TIER1, xs[1] - xs[0], x2=px2, y2=py2,
                        orientation="horizontal", label="hole pitch x", side="top", edge=top))
    if len(ys) == 2:
        x = xs[0]
        px1, py1 = _circle_pick(cx + s * x, cy + s * ys[0], r, s)
        px2, py2 = _circle_pick(cx + s * x, cy + s * ys[1], r, s)
        out.append(_dim(px1, py1, left - TIER1, cy, ys[1] - ys[0], x2=px2, y2=py2,
                        orientation="vertical", label="hole pitch y", side="left", edge=left))
    return out


def _pcd_value(group) -> float | None:
    """Bolt-circle diameter: center-to-center between the two outermost holes on the x axis."""
    on_axis = sorted((c[0] for c in group["centers"] if abs(c[1]) < 1e-9))
    return round(on_axis[-1] - on_axis[0], 6) if len(on_axis) >= 2 else None


def _pcd(group, pos, s, bottom):
    """Bolt-circle diameter as center-to-center between two opposite holes on the x axis.

    `measure_only`: the value is verified live and the dimension removed, because its extension lines
    would cross everything between the holes and the dimension line. The callout carries `ON Ø.. PCD`."""
    cx, cy = pos
    r = group["diameter"] / 2
    on_axis = sorted((c for c in group["centers"] if abs(c[1]) < 1e-9), key=lambda c: c[0])
    if len(on_axis) < 2:
        return []
    a, b = on_axis[0], on_axis[-1]
    # 120 / 60 deg: a pick within ~30 deg of the axis can land on the auto-inserted extended center line
    # (live-verified on small holes: it measured the 28 mm line instead of the 3 mm circle)
    p1 = _circle_pick(cx + s * a[0], cy, r, s, 120.0)
    p2 = _circle_pick(cx + s * b[0], cy, r, s, 60.0)
    return [_dim(*p1, cx, bottom - TIER1, b[0] - a[0], x2=p2[0], y2=p2[1],
                 orientation="horizontal", label="PCD", measure_only=True)]


def _hole_callout(group, pos, s, right, row):
    cx, cy = pos
    r = group["diameter"] / 2
    count = len(group["centers"])
    sign = -1 if row < 0 else 1  # leader runs toward its row: lower rows pick the lowest hole
    target = max(group["centers"], key=lambda c: (c[0], sign * c[1]))
    # pcd holes are small and sit on the axis: 0 deg lands on the extended center line (see _pcd), so use 60
    px, py = _circle_pick(cx + s * target[0], cy + s * target[1], r, s,
                          60.0 if group.get("role") == "pcd" else 0.0)
    depth = group.get("depth")
    suffix = " THRU" if not depth else f" DEEP {depth * 1000:g}"
    pcd = _pcd_value(group) if group.get("role") == "pcd" else None
    if pcd:
        suffix += f"  ON Ø{pcd * 1000:g} PCD"
    return _dim(px, py, right + 0.03, cy + row, group["diameter"],
                prefix=(f"{count}×" if count > 1 else "") + "Ø", suffix=suffix,
                label=f"callout {count}x")


def _slot_dims(slot, pos, s, bottom):
    """Rectangular pocket: length (below the view, inner tier) and width (right of the slot)."""
    cx, cy = pos
    (x1, x2), (y1, y2) = slot["x"], slot["y"]
    ymid, xmid = (y1 + y2) / 2, (x1 + x2) / 2
    return [
        _dim(cx + s * x1, cy + s * ymid, cx + s * xmid, bottom - TIER1, x2 - x1,
             x2=cx + s * x2, y2=cy + s * ymid, orientation="horizontal", label="slot length",
             side="bottom", edge=bottom),
        _dim(cx + s * xmid, cy + s * y1, cx + s * (x2 + 0.011), cy + s * ymid, y2 - y1,
             x2=cx + s * xmid, y2=cy + s * y2, orientation="vertical", label="slot width"),
    ]


def _with_alts(dim: dict, alts: list[dict]) -> dict:
    """A leader dimension with alternative placements (`x1,y1,text_x,text_y`); `_route_leaders` picks one."""
    return {**dim, "_alts": alts}


def _corner_radius(facts, pos, s, right, top):
    r = facts["corner_radius"]
    cx, cy = pos
    left = cx - (right - cx)
    cy_c = cy + s * (facts["height"] / 2 - r)
    # (corner x sign, pick angle, text x, text y): top-right first, then out of the same corner below the
    # top edge, then the top-left corner (the right side is busy with extension lines and callouts)
    options = [(1, 45.0, right + 0.012, top + 0.018), (1, 45.0, right + 0.012, top - 0.004),
               (-1, 135.0, left - 0.010, top - 0.004), (-1, 135.0, left - 0.010, top + 0.006)]
    alts = []
    for sx, ang, tx, ty in options:
        px, py = _circle_pick(cx + sx * s * (facts["width"] / 2 - r), cy_c, r, s, ang)
        alts.append({"x1": round(px, 6), "y1": round(py, 6), "text_x": round(tx, 6), "text_y": round(ty, 6)})
    first = alts[0]
    return _with_alts(_dim(first["x1"], first["y1"], first["text_x"], first["text_y"], r,
                           label="corner radius"), alts)


# (pick angle, text dx sign, text dy sign): the preferred placement is moved to the front
_LEADER_CANDIDATES = ((225.0, -1, -1), (135.0, -1, 1), (315.0, 1, -1), (45.0, 1, 1))


def _diameter_leader(diameter, pos, s, angle, text_dx, text_dy, label):
    cx, cy = pos
    first = (angle, 1 if text_dx > 0 else -1, 1 if text_dy > 0 else -1)
    candidates = [first] + [c for c in _LEADER_CANDIDATES if c != first]
    alts = []
    for ang, sx, sy in candidates:
        px, py = _circle_pick(cx, cy, diameter / 2, s, ang)
        alts.append({"x1": round(px, 6), "y1": round(py, 6),
                     "text_x": round(cx + sx * LEADER * s, 6), "text_y": round(cy + sy * LEADER * s, 6)})
    return _with_alts(_dim(alts[0]["x1"], alts[0]["y1"], alts[0]["text_x"], alts[0]["text_y"], diameter,
                           label=label), alts)


def plan_front_view(facts: dict, pos: tuple[float, float], scale: float = 1.0) -> list[dict]:
    left, right, bottom, top = _bbox(pos, scale, facts["width"], facts["height"])
    plan = _front_overall(facts, pos, scale)
    groups = facts.get("hole_groups", [])
    slot = facts.get("slot")
    for g in groups:
        if g.get("role") == "mount":
            plan += _mount_pitch(g, pos, scale, left, top)
        elif g.get("role") == "pcd":
            plan += _pcd(g, pos, scale, bottom)
    if slot:
        plan += _slot_dims(slot, pos, scale, bottom)
    if facts.get("corner_radius"):
        plan.append(_corner_radius(facts, pos, scale, right, top))
    for i, g in enumerate(groups):
        plan.append(_hole_callout(g, pos, scale, right, (0.04 - 0.04 * i) * scale))
    # The boss diameter is dimensioned once, as a linear width in the depth view, when that view exists.
    if facts.get("boss") and not (facts.get("depth") or {}).get("boss_width"):
        plan.append(_diameter_leader(facts["boss"]["diameter"], pos, scale, 135.0,
                                     -LEADER * scale, LEADER * scale, "boss diameter"))
    if facts.get("bore"):
        plan.append(_diameter_leader(facts["bore"]["diameter"], pos, scale, 225.0,
                                     -LEADER * scale, -LEADER * scale, "bore diameter"))
    return plan


def plan_top_view(facts: dict, pos: tuple[float, float], scale: float = 1.0) -> list[dict]:
    """Depth profile seen from the side of the flange: overall depth, flange thickness, boss length/width."""
    dp = facts.get("depth")
    if not dp:
        return []
    cx, cy = pos
    half_w = scale * facts["width"] / 2
    top_edge = cy + scale * dp["total"] / 2          # flange outer face
    flange_in = top_edge - scale * dp["flange"]      # flange inner face
    boss_end = cy - scale * dp["total"] / 2          # boss end face
    # A boss-end chamfer puts a second line `chamfer` above the end face; pick just outside the end face.
    end_pick = boss_end - scale * dp.get("boss_chamfer", 0.0) / 2
    boss_half = scale * dp["boss_width"] / 2
    right = cx + half_w
    left = cx - half_w
    plan = [
        _dim(cx + 0.65 * half_w, top_edge, right + TIER2, cy, dp["total"],
             x2=cx + 0.15 * half_w, y2=end_pick, orientation="vertical", label="overall depth", side="right", edge=right),
        _dim(cx + 0.65 * half_w, top_edge, right + TIER1, cy, dp["flange"],
             x2=cx + 0.65 * half_w, y2=flange_in, orientation="vertical", label="flange thickness", side="right", edge=right),
        _dim(cx + 0.65 * half_w, flange_in, left - TIER1, cy, dp["boss"],
             x2=cx + 0.15 * half_w, y2=end_pick, orientation="vertical", label="boss length", side="left", edge=left),
        _dim(cx - boss_half, flange_in - scale * 0.01, cx, boss_end - TIER1, dp["boss_width"],
             x2=cx + boss_half, y2=flange_in - scale * 0.01, orientation="horizontal",
             label="boss width", side="bottom", edge=boss_end),
    ]
    rib = facts.get("rib")
    if rib:
        t = scale * rib["thickness"] / 2
        ymid = (flange_in + top_edge - scale * rib["z_end"]) / 2
        plan.append(_dim(cx - t, ymid, cx, boss_end - TIER2, rib["thickness"], x2=cx + t, y2=ymid,
                         orientation="horizontal", label="rib thickness", side="bottom", edge=boss_end))
    return plan


def plan_dimensions(facts: dict, views: dict) -> list[dict]:
    """Full plan: views = {"front": {"pos": [x, y], "scale": s}, "top": {...}} (extra roles ignored)."""
    plan: list[dict] = []
    for role, fn in (("front", plan_front_view), ("top", plan_top_view)):
        v = views.get(role)
        if v:
            for d in fn(facts, tuple(v["pos"]), float(v.get("scale", 1.0))):
                plan.append({**d, "view": role})
    _reject_duplicates(plan)
    return _route_leaders(assign_tiers(plan))


def _span(d: dict) -> tuple[float, float]:
    a, b = (d["y1"], d["y2"]) if d["orientation"] == "vertical" else (d["x1"], d["x2"])
    return min(a, b), max(a, b)


def _tier_offset(level: int) -> float:
    return TIER1 + level * TIER_STEP


def assign_tiers(plan: list[dict]) -> list[dict]:
    """R1: per (view, side), a dimension whose span encloses another's goes on a farther tier; disjoint
    spans share a tier. Partially overlapping spans cannot be ordered apart: both get `_unavoidable`.
    Returns a new plan; `measure_only` dimensions take no tier."""
    groups: dict[tuple, list[int]] = {}
    for i, d in enumerate(plan):
        if d.get("_side") and not d.get("measure_only"):
            groups.setdefault((d["view"], d["_side"]), []).append(i)
    out = [dict(d) for d in plan]
    for idxs in groups.values():
        spans = {i: _span(plan[i]) for i in idxs}

        def encloses(i: int, j: int) -> bool:
            (a, b), (c, e) = spans[i], spans[j]
            if not (a - _EPS <= c and e <= b + _EPS):
                return False
            return (b - a) > (e - c) + _EPS or i > j  # equal spans: the later one goes outside

        levels: dict[int, int] = {}

        def level(i: int) -> int:
            if i not in levels:
                inner = [level(j) for j in idxs if j != i and encloses(i, j)]
                levels[i] = 1 + max(inner) if inner else 0
            return levels[i]

        for i in idxs:
            d = out[i]
            off = _tier_offset(level(i))
            if d["_side"] == "top":
                d["text_y"] = round(d["_edge"] + off, 6)
            elif d["_side"] == "bottom":
                d["text_y"] = round(d["_edge"] - off, 6)
            elif d["_side"] == "left":
                d["text_x"] = round(d["_edge"] - off, 6)
            else:
                d["text_x"] = round(d["_edge"] + off, 6)
            for j in idxs:
                (a, b), (c, e) = spans[i], spans[j]
                if (j != i and a < e - _EPS and c < b - _EPS
                        and not encloses(i, j) and not encloses(j, i)):
                    d["_unavoidable"] = True
    return out


def _route_leaders(plan: list[dict]) -> list[dict]:
    """R3: for leaders with alternatives, take the first placement that crosses nothing already placed;
    if none is clean, keep the first and flag `_unavoidable`."""
    placed = [d for d in plan if "_alts" not in d]
    out: list[dict] = []
    for d in plan:
        if "_alts" not in d:
            out.append(d)
            continue
        base = {k: v for k, v in d.items() if k != "_alts"}
        clean = next((c for c in ({**base, **alt} for alt in d["_alts"])
                      if not any(_crosses(c, o) for o in placed)), None)
        chosen = clean or {**base, **d["_alts"][0], "_unavoidable": True}
        placed.append(chosen)
        out.append(chosen)
    return out


def _segments(d: dict) -> list[tuple[str, tuple[float, float], tuple[float, float]]]:
    """Drawn line segments of one dimension: ('ext'|'dim'|'leader', p, q). Mirrors what SolidWorks draws
    from the pick points and the text position."""
    x1, y1, tx, ty = d["x1"], d["y1"], d["text_x"], d["text_y"]
    if "x2" not in d:
        return [("leader", (x1, y1), (tx, ty))]
    x2, y2 = d["x2"], d["y2"]
    (x1, y1), (x2, y2) = d.get("_ext_from") or ((x1, y1), (x2, y2))
    if d["orientation"] == "vertical":
        return [("ext", (x1, y1), (tx, y1)), ("ext", (x2, y2), (tx, y2)),
                ("dim", (tx, min(y1, y2)), (tx, max(y1, y2)))]
    return [("ext", (x1, y1), (x1, ty)), ("ext", (x2, y2), (x2, ty)),
            ("dim", (min(x1, x2), ty), (max(x1, x2), ty))]


def _properly_cross(p, q, r, s, eps=1e-9) -> bool:
    """True when segments pq and rs intersect in the interior of both (touching ends / collinear don't)."""
    d1x, d1y, d2x, d2y = q[0] - p[0], q[1] - p[1], s[0] - r[0], s[1] - r[1]
    den = d1x * d2y - d1y * d2x
    if abs(den) < eps:
        return False
    t = ((r[0] - p[0]) * d2y - (r[1] - p[1]) * d2x) / den
    u = ((r[0] - p[0]) * d1y - (r[1] - p[1]) * d1x) / den
    return eps < t < 1 - eps and eps < u < 1 - eps


def check_crossings(plan: list[dict]) -> list[tuple[str, str]]:
    """Every pair of dimensions (labels) whose drawn lines cross, leaders included.
    Measure-only dimensions are not drawn and are skipped."""
    drawn = [d for d in plan if not d.get("measure_only")]
    return [(a["label"], b["label"]) for i, a in enumerate(drawn) for b in drawn[i + 1:] if _crosses(a, b)]


def _crosses(a: dict, b: dict) -> bool:
    if a.get("measure_only") or b.get("measure_only"):
        return False
    # ext x ext is normal drafting practice; only dimension lines, text rows and leaders matter
    return any(not (sa[0] == sb[0] == "ext") and _properly_cross(sa[1], sa[2], sb[1], sb[2])
               for sa in _segments(a) for sb in _segments(b))


def _reject_duplicates(plan: list[dict]) -> None:
    seen: dict[tuple, str] = {}
    for d in plan:
        key = (round(d["expected_value"], 6), d["orientation"], d.get("prefix", ""))
        if key in seen and seen[key] != d["view"] and d["label"].startswith("overall"):
            raise ValueError(f"duplicate dimension {key} in views {seen[key]} and {d['view']}")
        seen.setdefault(key, d["view"])


def execute_plan(plan: list[dict], call) -> dict:
    """Run a plan through `call(tool, params) -> str` (server.py's `_call`, passed in).

    A failed dimension (e.g. DIMENSION_VALUE_MISMATCH) never aborts the run: it is recorded so the
    caller sees exactly which dimensions are missing."""
    placed, measured, failed = [], [], []
    for d in plan:
        params = {k: v for k, v in d.items() if k not in ("label", "view") and not k.startswith("_")}
        try:
            call("add_drawing_dimension_ex", params)
            (measured if d.get("measure_only") else placed).append(d["label"])
        except RuntimeError as exc:
            failed.append({"label": d["label"], "error": str(exc)[:200]})
    return {"placed": placed, "measured_only": measured, "failed": failed, "planned": len(plan),
            "crossings": check_crossings(plan),
            "unavoidable": [d["label"] for d in plan if d.get("_unavoidable")]}
