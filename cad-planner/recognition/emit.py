"""Intermediate features → Feature Graph IR (schema 0.5.0-draft, meters/radians).

Direction rule, calibrated live (SW 2025): a blind BOSS runs along the datum's +normal (reversed
flips it); a blind CUT runs along −normal by default (reversed ⇒ +normal).
"""
import math
from typing import Dict, List, Sequence, Union

from .features import EdgeFeat, ExtrudeFeat, PlaneFrame, RevolveFeat

SCHEMA_VERSION = "0.5.0-draft"


def _frame(f: PlaneFrame) -> Dict:
    return {"origin": [round(c, 9) for c in f.origin], "xdir": list(f.xdir), "ydir": list(f.ydir)}


def _sketch(sid: str, frame: PlaneFrame, profile) -> Dict:
    return {"id": sid, "type": "sketch", "ref": {"datum": frame.datum, "offset": round(frame.offset, 9)},
            "frame": _frame(frame), "profile": [dict(p) for p in profile]}


def _extrude(nid: str, sid: str, f: ExtrudeFeat) -> Dict:
    node = {"id": nid, "type": "extrude", "sketch": sid, "operation": f.op, "end": f.end}
    if f.end in ("blind", "mid_plane"):
        node["depth"] = round(f.depth, 9)
    if f.end != "mid_plane":
        node["reversed"] = (not f.toward_plus_normal) if f.op == "boss" else f.toward_plus_normal
    return node


def _edge_node(nid: str, f: EdgeFeat) -> Dict:
    edges = [{"near": [round(c, 9) for c in a]} for a in f.anchors]
    if f.kind == "fillet":
        return {"id": nid, "type": "fillet", "radius": round(f.size, 9), "edges": edges}
    if f.distance2 is None:
        return {"id": nid, "type": "chamfer", "chamfer_type": "distance_angle", "distance": round(f.size, 9),
                "angle": math.pi / 4.0, "edges": edges}
    return {"id": nid, "type": "chamfer", "chamfer_type": "distance_distance", "distance": round(f.size, 9),
            "distance2": round(f.distance2, 9), "edges": edges}


def to_graph(base: Union[ExtrudeFeat, RevolveFeat], cuts: Sequence[ExtrudeFeat],
             edge_feats: Sequence[EdgeFeat]) -> Dict:
    nodes: List[Dict] = [_sketch("s1", base.frame, base.profile)]
    if isinstance(base, RevolveFeat):
        x1, y1, x2, y2 = base.axis2d
        nodes.append({"id": "r1", "type": "revolve", "sketch": "s1",
                      "axis": {"x1": x1, "y1": y1, "x2": x2, "y2": y2}})
    else:
        nodes.append(_extrude("e1", "s1", base))
    for n, c in enumerate(cuts, start=2):
        nodes.append(_sketch("s%d" % n, c.frame, c.profile))
        nodes.append(_extrude("c%d" % n, "s%d" % n, c))
    ordered = [f for f in edge_feats if f.kind == "chamfer"] + [f for f in edge_feats if f.kind == "fillet"]
    for n, f in enumerate(ordered, start=1):
        nodes.append(_edge_node(("ch%d" if f.kind == "chamfer" else "fl%d") % n, f))
    return {"schema_version": SCHEMA_VERSION, "units": "meters", "nodes": nodes}
