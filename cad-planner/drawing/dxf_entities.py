"""Low-level DXF entity, bounding-box, text-block, and dimension-text helpers for `dxf_read`.

Pure arithmetic and entity inspection helpers that do not require ezdxf at module import time,
allowing unit/contract tests (such as `reconcile_angular` and `printed_value`) to run offline
without ezdxf installed.
"""
from __future__ import annotations

import math
import re

try:                             # normal: imported as part of the `drawing` package
    from . import curvefit
except ImportError:              # fallback: run directly as a script from this directory
    import curvefit


def _r(v, nd=3):
    """3 decimals = 1 um in TRUE mm, the house convention."""
    return round(float(v), nd)


# Entity classes the reader consumes. Everything else it sees is COUNTED in sheet.not_read.
_READ_TYPES = frozenset(("LINE", "ARC", "CIRCLE", "ELLIPSE", "DIMENSION", "MTEXT", "TEXT",
                         "INSERT", "VIEWPORT"))

# DXF dimension type bitmask (& 7) -> canonical dimension kind.
DIMKIND = {0: "linear", 1: "aligned", 2: "angular", 3: "diameter",
           4: "radius", 5: "angular3p", 6: "ordinate"}


def _text_of(e):
    return (e.text if e.dxftype() == "MTEXT" else e.dxf.text) or ""


def _walk_block(ins):
    """Every entity inside an INSERT, nested blocks included, in WCS (ezdxf applies the insert
    transform). A block the file does not define yields nothing."""
    try:
        for ve in ins.virtual_entities():
            if ve.dxftype() == "INSERT":
                yield from _walk_block(ve)
            else:
                yield ve
    except Exception:
        return


def _block_rows(ins):
    """Text inside a note block -> [(row_text, x, y)] in reading order.

    A SolidWorks note block holds one MTEXT per line -- or, through the DWG route, one MTEXT per
    GLYPH RUN. The fragments of one row share a baseline; they are re-joined left to right, with
    a space only where the gap after the previous fragment's estimated extent says there was one."""
    frags = []
    for ve in _walk_block(ins):
        if ve.dxftype() in ("MTEXT", "TEXT"):
            t = re.sub(r"\s+", " ", _text_of(ve).replace("\n", " ")).strip()
            if t:
                h = (ve.dxf.char_height if ve.dxftype() == "MTEXT" else ve.dxf.height) or 1.0
                frags.append((t, ve.dxf.insert.x, ve.dxf.insert.y, h))
    frags.sort(key=lambda f: (-f[2], f[1]))
    rows = []
    for f in frags:
        if rows and abs(rows[-1][0][2] - f[2]) <= 0.5 * f[3]:
            rows[-1].append(f)
        else:
            rows.append([f])
    out = []
    for row in rows:
        row.sort(key=lambda f: f[1])
        text, end = row[0][0], row[0][1] + 0.75 * row[0][3] * len(row[0][0])
        for t, x, _y, h in row[1:]:
            text += (" " if x - end > 0.35 * h else "") + t
            end = x + 0.75 * h * len(t)
        out.append((text, row[0][1], row[0][2]))
    return out


def _block_has_geometry(ins):
    return any(ve.dxftype() not in ("MTEXT", "TEXT", "ATTRIB", "ATTDEF") for ve in _walk_block(ins))


def ellipse_params(e):
    """A DXF ELLIPSE -> (center, rx, ry, rot_deg, p1, p2, ccw) in PAPER mm; p1/p2 None = full."""
    ct = e.construction_tool()
    maj = ct.major_axis
    rx = math.hypot(maj.x, maj.y)
    ry = rx * float(ct.ratio)
    rot = math.degrees(math.atan2(maj.y, maj.x)) % 180.0
    c = (float(ct.center.x), float(ct.center.y))
    s, en = float(ct.start_param), float(ct.end_param)
    span = (en - s) % (2.0 * math.pi)
    if span < 1e-9:
        return c, rx, ry, rot, None, None, True
    p1, p2, pm = [(float(v.x), float(v.y)) for v in ct.vertices([s, en, s + span / 2.0])]
    el = (c[0], c[1], rx, ry, rot)
    t1, t2, tm = (curvefit.param_of(p, el) for p in (p1, p2, pm))
    two_pi = 2.0 * math.pi
    ccw = ((tm - t1) % two_pi) <= ((t2 - t1) % two_pi)
    return c, rx, ry, rot, p1, p2, ccw


def edge_class(doc, e):
    """Entity linetype with BYLAYER resolved -> a closed class enum."""
    lt = getattr(e.dxf, "linetype", "BYLAYER") or "BYLAYER"
    if lt.upper() == "BYLAYER":
        try:
            lt = doc.layers.get(e.dxf.layer).dxf.linetype
        except Exception:
            lt = "Continuous"
    u = lt.upper()
    if u.startswith("HIDDEN"):
        return "hidden"
    if u.startswith("PHANTOM"):
        return "cut_line"
    if u.startswith("CENTER"):
        return "center"
    return "visible"


def bbox(e):
    t = e.dxftype()
    if t == "LINE":
        s, en = e.dxf.start, e.dxf.end
        return (min(s.x, en.x), min(s.y, en.y), max(s.x, en.x), max(s.y, en.y))
    if t in ("CIRCLE", "ARC"):
        c, r = e.dxf.center, e.dxf.radius
        return (c.x - r, c.y - r, c.x + r, c.y + r)
    if t == "ELLIPSE":
        return _ellipse_extent(e)
    return None


def _ellipse_extent(e):
    """True extent of an ELLIPSE (arc) by sampling its construction tool."""
    try:
        ct = e.construction_tool()
        pts = [(float(v.x), float(v.y)) for v in ct.vertices(ct.params(64))]
    except Exception:
        return None
    if not pts:
        return None
    return (min(p[0] for p in pts), min(p[1] for p in pts),
            max(p[0] for p in pts), max(p[1] for p in pts))


# A drawing-scale ratio inside a view label ("A-A 1 : 1", "DETAIL B 2:1", "M 1:2").
_SCALE_RE = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)(?![\d.,])")

_NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


def printed_text(doc, e):
    """What the DRAWING PRINTS for this dimension."""
    blk = e.dxf.geometry if e.dxf.hasattr("geometry") else None
    if not blk or blk not in doc.blocks:
        return None
    out = [(b.text if b.dxftype() == "MTEXT" else b.dxf.text)
           for b in doc.blocks[blk] if b.dxftype() in ("MTEXT", "TEXT")]
    return _decode_cad_escapes(" ".join(t for t in out if t).strip()) or None


# AutoCAD's %% control codes.
_CAD_ESCAPES = (("%%c", "Ø"), ("%%C", "Ø"),   # diameter
                ("%%d", "°"), ("%%D", "°"),   # degree
                ("%%p", "±"), ("%%P", "±"),   # plus/minus
                ("%%%", "%"))


def _decode_cad_escapes(txt):
    if not txt:
        return txt
    for code, glyph in _CAD_ESCAPES:
        txt = txt.replace(code, glyph)
    return txt


def printed_value(txt):
    """The number inside a printed dimension string, or None."""
    if not txt:
        return None
    t = re.sub(r"\\[A-Za-z][^;]*;", " ", txt)
    for junk in ("%%c", "%%C", "%%d", "%%D", "%%p", "%%P",
                 "Ø", "°", "±", "{", "}"):
        t = t.replace(junk, " ")
    m = _NUM_RE.search(t)
    return float(m.group(0).replace(",", ".")) if m else None


def reconcile_angular(computed, printed):
    """Which of an ANGULAR dim's two candidates is the real angle. Returns (value, mismatch)."""
    if printed is None:
        return computed, False
    if computed is None:
        return printed, False
    mismatch = abs(computed - printed) > max(0.01, 0.01 * abs(printed))
    if mismatch and (computed <= 180.0) != (printed <= 180.0):
        return (computed if computed <= 180.0 else printed), True
    return printed, mismatch


_CARDINALS = (0.0, 90.0, 180.0, 270.0)


def arc_bbox(cx, cy, r, a1, a2):
    """The TRUE bbox of an arc, not of its full circle."""
    a1, a2 = a1 % 360.0, a2 % 360.0
    sweep = (a2 - a1) % 360.0 or 360.0
    xs = [cx + r * math.cos(math.radians(a)) for a in (a1, a2)]
    ys = [cy + r * math.sin(math.radians(a)) for a in (a1, a2)]
    for ang in _CARDINALS:
        if ((ang - a1) % 360.0) <= sweep:
            xs.append(cx + r * math.cos(math.radians(ang)))
            ys.append(cy + r * math.sin(math.radians(ang)))
    return (min(xs), min(ys), max(xs), max(ys))


def geom_bbox(doc, items):
    """Bbox over REAL part geometry only — the box a view's SIZE and its projection-pair test must use."""
    for keep in (("visible",), ("visible", "hidden"), None):
        boxes = []
        for e, b in items:
            if keep is not None and edge_class(doc, e) not in keep:
                continue
            if e.dxftype() == "ARC":
                c = e.dxf.center
                boxes.append(arc_bbox(c.x, c.y, e.dxf.radius, e.dxf.start_angle, e.dxf.end_angle))
            else:
                boxes.append(b)
        if boxes:
            return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))
    return None


def cluster(items, gap):
    """Union-find on bbox proximity -> connected groups of primitives."""
    par = list(range(len(items)))

    def find(a):
        while par[a] != a:
            par[a] = par[par[a]]
            a = par[a]
        return a

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i][1], items[j][1]
            if a[0] <= b[2] + gap and b[0] <= a[2] + gap and a[1] <= b[3] + gap and b[1] <= a[3] + gap:
                ra, rb = find(i), find(j)
                if ra != rb:
                    par[ra] = rb
    groups = {}
    for i, it in enumerate(items):
        groups.setdefault(find(i), []).append(it)
    return list(groups.values())
