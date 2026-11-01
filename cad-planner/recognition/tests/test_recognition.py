"""Offline gate for the STEP→features recognizer (cad-planner/recognition).

The fixtures are `analyze_model(analysis_type='brep')` dumps of REAL STEP imports (SW 2025), recorded
live from the parts in tests/record_fixtures.md — so the tests see the import's actual topology
(full cylinders split into two halves, seam edges, ...), not an idealised B-rep.

Run two ways:
  - standalone:  python cad-planner/recognition/tests/test_recognition.py   (from the repo root)
  - pytest:      pytest cad-planner/recognition/tests/test_recognition.py
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CAD_PLANNER = os.path.normpath(os.path.join(_HERE, "..", ".."))
if _CAD_PLANNER not in sys.path:
    sys.path.insert(0, _CAD_PLANNER)

from recognition.blends import blend_face_ids, find_blends  # noqa: E402
from recognition.brep import parse_brep  # noqa: E402
from recognition.classify import classify as _classify  # noqa: E402
from recognition.geom import close  # noqa: E402
from recognition.prismatic import decompose_prismatic  # noqa: E402
from recognition.turned import decompose_turned  # noqa: E402
from recognition import recognize  # noqa: E402

sys.path.insert(0, os.path.normpath(os.path.join(_CAD_PLANNER, "..", "compiler", "solidworks")))
from pycompiler.ir_schema import validate  # noqa: E402


def classify(brep):
    """Production order: blends first, then classification ignoring blend faces."""
    return _classify(brep, blend_face_ids(find_blends(brep)))

_FIXTURES = os.path.join(_HERE, "fixtures")


def raw(name):
    with open(os.path.join(_FIXTURES, name + ".brep.json"), encoding="utf-8") as fh:
        return json.load(fh)


def load(name):
    return parse_brep(raw(name))


# ---- Task 2: brep model + classification ---------------------------------------------------------

def test_parse_block():
    b = load("block_hole_fillets")
    assert b.body_count == 1 and len(b.faces) == 12 and len(b.edges) == 30


def test_parse_neighbours_and_loops():
    b = load("block_hole_fillets")
    top = next(f for f in b.faces if f.type == "plane" and f.normal[1] > 0.99)
    assert sum(1 for lp in top.loops if lp.outer) == 1
    assert len(b.neighbours(top.i)) >= 4


def test_classify_prismatic():
    c = classify(load("block_hole_fillets"))
    assert (c.part_class, c.axis, c.refusal) == ("prismatic", 1, None)


def test_classify_plate_prismatic():
    c = classify(load("plate_features"))
    assert (c.part_class, c.axis) == ("prismatic", 1)


def test_classify_turned():
    c = classify(load("stepped_shaft"))
    assert (c.part_class, c.axis, c.refusal) == ("turned", 1, None)
    assert abs(c.turn_origin[0]) < 5e-5 and abs(c.turn_origin[2]) < 5e-5


def test_not_axis_aligned():
    c = classify(load("rotated_block"))
    assert c.refusal == "NOT_AXIS_ALIGNED" and "directions" in c.detail


def test_unsupported():
    assert classify(load("freeform")).refusal == "UNSUPPORTED_GEOMETRY"


def test_multibody():
    c = classify(parse_brep({"body_count": 2}))
    assert c.refusal == "MULTIBODY"


# ---- Task 3: blends --------------------------------------------------------------------------------

def test_block_fillets():
    bl = find_blends(load("block_hole_fillets"))
    assert sorted(b.kind for b in bl) == ["fillet"] * 4
    assert all(abs(b.size - 0.005) < 5e-5 for b in bl)


def test_block_fillet_anchor_is_sharp_corner():
    bl = find_blends(load("block_hole_fillets"))
    assert any(b.anchor is not None and close(b.anchor, (0.05, 0.01, 0.03)) for b in bl)
    assert all(b.anchor is not None for b in bl)


def test_slot_ends_and_hole_halves_are_not_fillets():
    kinds = [b.kind for b in find_blends(load("plate_features"))]
    assert "fillet" not in kinds


def test_plate_chamfers():
    bl = [b for b in find_blends(load("plate_features")) if b.kind == "chamfer"]
    assert len(bl) == 4 and all(abs(b.size - 0.001) < 5e-5 and b.distance2 is None for b in bl)
    assert any(close(b.anchor, (0.2, 0.045, 0.06)) for b in bl)


def test_shaft_end_chamfers():
    bl = [b for b in find_blends(load("stepped_shaft")) if b.kind == "chamfer"]
    assert len(bl) == 2 and all(b.sharp_circle is not None and b.anchor is None for b in bl)
    radii = sorted(round(b.sharp_circle[2], 4) for b in bl)
    assert radii == [0.01, 0.015]


# ---- Task 4: prismatic decomposition ---------------------------------------------------------------

def decompose(name, brep=None):
    b = brep or load(name)
    bl = find_blends(b)
    return decompose_prismatic(b, _classify(b, blend_face_ids(bl)), bl)


def _signature(feats):
    return sorted((f.op, f.end, round(f.depth or 0.0, 4)) for f in feats)


def test_block_decomposition():
    feats, unexpl = decompose("block_hole_fillets")
    assert [f.op for f in feats] == ["boss", "cut"]
    assert feats[1].end == "through_all" and unexpl == []
    assert feats[0].frame.datum == "top" and abs(feats[0].frame.offset - 0.0) < 5e-5
    assert feats[0].toward_plus_normal is True and abs(feats[0].depth - 0.02) < 5e-5


def test_section_loop_rectangle_after_fillet_removal():
    feats, _ = decompose("block_hole_fillets")
    prof = feats[0].profile
    assert len(prof) == 4 and all(p["kind"] == "line" for p in prof)
    xs = sorted({round(p["x1"], 5) for p in prof} | {round(p["x2"], 5) for p in prof})
    assert xs == [-0.05, 0.05]


def test_plate_decomposition():
    feats, unexpl = decompose("plate_features")
    assert feats[0].op == "boss" and abs(feats[0].frame.offset - 0.03) < 5e-5
    assert abs(feats[0].depth - 0.015) < 5e-5
    assert _signature(feats[1:]) == sorted([("cut", "blind", 0.006), ("cut", "blind", 0.004),
                                            ("cut", "through_all", 0.0), ("cut", "through_all", 0.0)])
    assert unexpl == []


def test_plate_blind_cuts_go_down_from_top():
    feats, _ = decompose("plate_features")
    for f in feats[1:]:
        if f.end == "blind":
            assert abs(f.frame.offset - 0.045) < 5e-5 and f.toward_plus_normal is False


def test_plate_slot_profile_has_two_arcs():
    feats, _ = decompose("plate_features")
    slot = [f for f in feats if any(p["kind"] == "arc" for p in f.profile)]
    assert len(slot) == 1 and sum(p["kind"] == "arc" for p in slot[0].profile) == 2


def test_unexplained_face_reported():
    raw_b = raw("block_hole_fillets")
    extra = len(raw_b["faces"])  # face `i` == its position, as in every real dump
    raw_b["faces"].append({"i": extra, "type": "sphere", "area": 1e-6, "point": [0, 0.5, 0],
                           "normal": [0, 1, 0], "origin": [0, 0.5, 0], "radius": 0.001})
    clean = load("block_hole_fillets")
    cls = _classify(clean, blend_face_ids(find_blends(clean)))  # the sphere alone would be refused
    injected = parse_brep(raw_b)
    feats, unexpl = decompose_prismatic(injected, cls, find_blends(injected))
    assert unexpl == [extra] and [f.op for f in feats] == ["boss", "cut"]


# ---- Task 5: turned parts, IR emission, recognize() ------------------------------------------------

def decompose_t(name):
    b = load(name)
    bl = find_blends(b)
    return decompose_turned(b, _classify(b, blend_face_ids(bl)), bl)


def _closed(profile):
    ends = []
    for p in profile:
        if p.get("construction") or p["kind"] != "line":
            continue
        ends += [(round(p["x1"], 6), round(p["y1"], 6)), (round(p["x2"], 6), round(p["y2"], 6))]
    return all(ends.count(e) % 2 == 0 for e in ends)


def test_turned_shaft():
    base, cuts, unexpl = decompose_t("stepped_shaft")
    assert len(cuts) == 1 and cuts[0].end == "mid_plane" and unexpl == []
    assert abs(cuts[0].profile[0]["diameter"] - 0.004) < 5e-5


def test_shaft_profile_closed_with_bore():
    base, _, _ = decompose_t("stepped_shaft")
    assert _closed(base.profile)
    assert sum(1 for p in base.profile if p.get("construction")) == 1
    pts = {(round(p["x1"], 4), round(p["y1"], 4)) for p in base.profile if not p.get("construction")}
    # sharp (chamfer-free) corners of the half-section, in the front-plane frame (u = X radial, v = Y axial)
    assert {(0.015, 0.0), (0.015, 0.04), (0.01, 0.07), (0.004, 0.07), (0.004, 0.055)} <= pts


def test_shaft_revolve_frame_is_measured_front():
    base, _, _ = decompose_t("stepped_shaft")
    assert base.frame.datum == "front" and abs(base.frame.offset) < 5e-5


def test_recognize_all_ladder_valid_ir():
    for name in ("block_hole_fillets", "plate_features", "stepped_shaft"):
        r = recognize(raw(name))
        assert r.status == "OK", (name, r.notes)
        assert validate(r.graph) == [], (name, validate(r.graph))
        assert r.unexplained_faces == (), (name, r.unexplained_faces)


def test_emit_cut_reversed_rule():
    g = recognize(raw("block_hole_fillets")).graph
    types = [n["type"] for n in g["nodes"]]
    assert types[:4] == ["sketch", "extrude", "sketch", "extrude"] and types[4:] == ["fillet"]
    assert g["nodes"][1]["operation"] == "boss" and g["nodes"][1].get("reversed") is False
    assert g["nodes"][3]["operation"] == "cut" and g["nodes"][3]["end"] == "through_all"
    assert g["nodes"][3].get("reversed") is False


def test_emit_edge_features_grouped():
    g = recognize(raw("plate_features")).graph
    ch = [n for n in g["nodes"] if n["type"] == "chamfer"]
    assert len(ch) == 1 and len(ch[0]["edges"]) == 4 and ch[0]["chamfer_type"] == "distance_angle"


def test_emit_shaft_chamfer_anchors_use_revolve_mid_rule():
    g = recognize(raw("stepped_shaft")).graph
    ch = [n for n in g["nodes"] if n["type"] == "chamfer"][0]
    nears = sorted(tuple(round(c, 6) for c in e["near"]) for e in ch["edges"])
    assert nears == [(0.0, 0.0, -0.015), (0.0, 0.07, -0.01)]


def test_recognize_refusals():
    assert recognize(raw("freeform")).status == "UNSUPPORTED_GEOMETRY"
    assert recognize(raw("rotated_block")).status == "NOT_AXIS_ALIGNED"
    assert recognize({"body_count": 2}).status == "MULTIBODY"


def _prism_brep(n_sides, flat_width, length):
    """Synthetic regular n-gon bar along Y (no edges needed for the chamfer-escape check)."""
    import math
    faces, edges = [], []
    r = flat_width / 2.0
    for k in range(n_sides):
        a = 2 * math.pi * k / n_sides
        faces.append({"i": k, "type": "plane", "area": flat_width * length,
                      "point": [r * math.cos(a), length / 2, r * math.sin(a)],
                      "normal": [math.cos(a), 0.0, math.sin(a)]})
    for k in range(n_sides):   # side-side edges along Y, convex, long
        a = 2 * math.pi * (k + 0.5) / n_sides
        R = r / math.cos(math.pi / n_sides)
        p0, p1 = [R * math.cos(a), 0.0, R * math.sin(a)], [R * math.cos(a), length, R * math.sin(a)]
        edges.append({"i": k, "type": "line", "start": p0, "end": p1,
                      "mid": [p0[0], length / 2, p0[2]], "length": length,
                      "faces": [k, (k + 1) % n_sides], "convexity": "convex"})
    return {"body_count": 1, "faces": faces, "edges": edges}


def test_hex_bar_faces_are_not_chamfers():
    # 60° side faces sit between non-axis-aligned neighbours: not chamfers, so the bar is refused
    # as NOT_AXIS_ALIGNED instead of slipping through. (A regular OCTAGON bar legitimately reads as a
    # square bar with four 45° chamfers between aligned faces — a correct rebuild, so not tested here.)
    b = parse_brep(_prism_brep(6, 0.01, 0.1))
    assert [x for x in find_blends(b) if x.kind == "chamfer"] == []
    assert _classify(b, blend_face_ids(find_blends(b))).refusal == "NOT_AXIS_ALIGNED"


def _run_all():
    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
        except Exception as ex:  # noqa: BLE001 - report every failing test
            failures += 1
            sys.stderr.write("FAIL %s: %s: %s\n" % (name, type(ex).__name__, ex))
    sys.stdout.write("%d/%d recognition tests passed\n" % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
