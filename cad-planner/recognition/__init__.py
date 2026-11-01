"""STEP → feature-based part recognition: a CAD-neutral, stdlib-only map from a single-body B-rep
(analyze_model(analysis_type='brep')) to a Feature Graph IR. See docs/superpowers/specs/
2026-11-01-step-to-features-design.md.

    recognize(brep_json) -> RecognitionResult(status, part_class, graph, unexplained_faces, notes)
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .blends import Blend, blend_face_ids, find_blends
from .brep import parse_brep
from .classify import classify
from .emit import to_graph
from .features import EdgeFeat, PlaneFrame
from .geom import Vec, scale, sub
from .prismatic import decompose_prismatic
from .turned import decompose_turned

__all__ = ["recognize", "RecognitionResult"]


@dataclass(frozen=True)
class RecognitionResult:
    status: str                         # OK | UNSUPPORTED_GEOMETRY | NOT_AXIS_ALIGNED | MULTIBODY
    part_class: Optional[str]
    graph: Optional[Dict]
    unexplained_faces: Tuple[int, ...]
    notes: Tuple[str, ...]


def _edge_feats(blends: Tuple[Blend, ...], revolve_frame: Optional[PlaneFrame]):
    """Group blends of equal kind/size into one IR node each. A circular sharp edge is anchored by
    the revolve mid rule (live-calibrated: a revolve-made circle reports mid = centre − r·N, N = the
    revolve sketch normal); without a revolve there is no known rule, so the blend is reported."""
    groups: Dict[Tuple, List[Vec]] = {}
    dropped: List[Blend] = []
    for b in blends:
        if b.anchor is not None:
            anchor = b.anchor
        elif b.sharp_circle is not None and revolve_frame is not None:
            centre, _axis, radius = b.sharp_circle
            anchor = sub(centre, scale(revolve_frame.normal, radius))
        else:
            dropped.append(b)
            continue
        key = (b.kind, round(b.size, 7), None if b.distance2 is None else round(b.distance2, 7))
        groups.setdefault(key, []).append(anchor)
    feats = [EdgeFeat(k[0], k[1], k[2], tuple(v)) for k, v in sorted(groups.items(), key=lambda kv: repr(kv[0]))]
    return feats, dropped


def recognize(brep_obj: Dict) -> RecognitionResult:
    brep = parse_brep(brep_obj)
    blends = find_blends(brep) if brep.body_count == 1 else ()
    cls = classify(brep, blend_face_ids(blends))
    if cls.refusal:
        return RecognitionResult(cls.refusal, None, None, (), (cls.detail,))
    notes = [cls.detail]
    if cls.part_class == "turned":
        base, cuts, unexplained = decompose_turned(brep, cls, blends)
        revolve_frame = base.frame if base is not None else None
    else:
        feats, unexplained = decompose_prismatic(brep, cls, blends)
        base, cuts, revolve_frame = (feats[0] if feats else None), feats[1:], None
    if base is None:
        return RecognitionResult("UNSUPPORTED_GEOMETRY", cls.part_class, None, tuple(unexplained),
                                 tuple(notes + ["no base feature could be derived"]))
    edge_feats, dropped = _edge_feats(blends, revolve_frame)
    for b in dropped:
        notes.append("%s on faces %s has a circular edge with no anchor rule — not rebuilt" % (b.kind, list(b.faces)))
        unexplained = sorted(set(unexplained) | set(b.faces))
    graph = to_graph(base, cuts, edge_feats)
    return RecognitionResult("OK", cls.part_class, graph, tuple(unexplained), tuple(notes))
