"""Compiler orchestrator: validate -> (per node) resolve -> lower -> execute, threading
state_version through the run.

Failure model (CAD ops are NOT transactional): nodes run in declaration order; on the first
failure the run STOPS, no rollback is attempted, and the result reports how far it got plus a
feature-level error. The caller (the adapter tool) resyncs state_version afterwards either way.

Public API (consumed by pycompiler/__init__.py and the adapter):
    compile_and_run(port, graph) -> CompileResult

To add a new IR node type to the compiler:
  1. Implement its lowering helper in `lowering.py`.
  2. Define a `_plan_<type>(port, node, state_version, node_features)` function below.
  3. Register it in `_NODE_PLANNERS`.
"""
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import lowering
from .errors import FeatureError
from .ir_schema import validate
from .resolver import (
    resolve_center_on_face,
    resolve_component_entity_by_anchor,
    resolve_edges_by_anchor,
    resolve_face_by_anchor,
    resolve_face_on_plane,
    resolve_top_face,
)


class CompileResult(object):
    """Outcome of one compile+run. Plain data so the adapter can render it however it likes."""

    def __init__(self) -> None:
        self.status: str = "COMPLETED"     # COMPLETED | FAILED
        self.nodes_total: int = 0
        self.nodes_completed: int = 0
        self.exec_calls: int = 0           # state-bumping ops actually executed
        self.node_log: List[Dict[str, Any]] = []  # [{id, type, status, ops:[{tool, note, state_version}]}]
        self.final_state_version: Optional[int] = None
        self.error: Optional[Dict[str, Any]] = None  # {code, message, [node_id, node_type, step]}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "nodes_total": self.nodes_total,
            "nodes_completed": self.nodes_completed,
            "exec_calls": self.exec_calls,
            "final_state_version": self.final_state_version,
            "error": self.error,
            "node_log": self.node_log,
        }

    def summary(self) -> str:
        """One-line human-readable summary for the MCP result string."""
        if self.status == "COMPLETED":
            return ("COMPLETED | IR built %d/%d nodes | exec_calls=%d"
                    % (self.nodes_completed, self.nodes_total, self.exec_calls))
        err = self.error or {}
        if err.get("code") == "VALIDATION_FAILED":
            return "FAILED | VALIDATION_FAILED | %s | no execution performed" % err.get("message")
        node = err.get("node_id")
        step = err.get("step")
        return ("FAILED | %s | node %s (%s)%s: %s | completed %d/%d nodes before failure "
                "(partial geometry may remain — CAD ops are not transactional)"
                % (err.get("code"), node, err.get("node_type"),
                   (" step=%s" % step) if step else "", err.get("message"),
                   self.nodes_completed, self.nodes_total))


def _record_step_failure(
    result: CompileResult,
    rec: Dict[str, Any],
    sv: int,
    nid: Optional[str],
    ntype: Optional[str],
    exc: Exception,
) -> None:
    """Populate `rec` and `result` on a FeatureError or unexpected exception during node/material execution."""
    rec["status"] = "FAILED"
    result.node_log.append(rec)
    result.status = "FAILED"
    if isinstance(exc, FeatureError):
        result.error = {
            "code": exc.code,
            "message": exc.message,
            "node_id": exc.node_id or nid,
            "node_type": ntype,
            "step": exc.step,
        }
    else:
        result.error = {
            "code": "NODE_UNEXPECTED",
            "message": str(exc),
            "node_id": nid,
            "node_type": ntype,
        }
    result.final_state_version = sv


def compile_and_run(port: Any, graph: Any) -> CompileResult:
    result = CompileResult()

    # 1) Structural validation. If it fails, NO execution call is made.
    errors = validate(graph)
    if errors:
        result.status = "FAILED"
        result.error = {"code": "VALIDATION_FAILED", "message": "; ".join(errors)}
        return result

    nodes = graph["nodes"]
    result.nodes_total = len(nodes)

    # 2) Seed state_version from the authoritative source (also the basis for the post-run resync).
    try:
        sv = port.get_state()
    except Exception as ex:
        result.status = "FAILED"
        result.error = {"code": "STATE_SEED_FAILED", "message": str(ex)}
        return result

    # 3) Run nodes in order. node_features records each node's CREATED feature name (the last
    # non-empty cadState.features[0] among its ops) so later nodes can reference it by node id
    # (lowering.node_feature — e.g. a circular_pattern's seed), localization-proof.
    node_features: Dict[str, str] = {}
    for node in nodes:
        nid = node.get("id")
        ntype = node.get("type")
        rec: Dict[str, Any] = {"id": nid, "type": ntype, "status": "PENDING", "ops": []}
        try:
            ops = _plan_node(port, node, sv, node_features)  # may do a live (read-only) resolve
            last_resp = None
            frame_fn = None  # 2D coord transform for this node's entities (sketch frame fix)
            frame_mirror = False
            for op in ops:
                params = _fill_placeholders(op, last_resp, node_features, nid, ntype)
                if frame_fn is not None and op.tool == "add_sketch_entity":
                    params = _transform_entity_params(params, frame_fn, frame_mirror)
                resp = port.execute(op.tool, params, sv)
                result.exec_calls += 1
                sv = _advance(resp, sv, nid, ntype, op)
                last_resp = resp
                feats = ((resp.get("cadState") or {}).get("features")) or []
                if nid and feats and feats[0]:
                    node_features[nid] = feats[0]
                if op.tool == "create_sketch":
                    # A sketch's runtime NAME (renumbered/localized on the rebuild) lives in
                    # cadState.activeSketch, not features — record it so a later loft/sweep can
                    # reference this sketch node BY NAME.
                    ask = (resp.get("cadState") or {}).get("activeSketch")
                    if nid and ask:
                        node_features[nid] = ask
                if _echoes_frame(op) and node.get("frame"):
                    measured = ((resp.get("result_geometry") or {}).get("frame"))
                    if not measured:
                        raise FeatureError(
                            "the node declares a sketch frame but the sketch-creating op "
                            "echoed none (execution layer too old?)",
                            code="FRAME_UNAVAILABLE", node_id=nid, node_type=ntype,
                            step=op.tool)
                    frame_fn, frame_mirror = _sketch_frame_transform(node["frame"], measured)
                rec["ops"].append({"tool": op.tool, "note": op.note, "state_version": sv})
            rec["status"] = "COMPLETED"
            result.node_log.append(rec)
            result.nodes_completed += 1
        except Exception as exc:
            _record_step_failure(result, rec, sv, nid, ntype, exc)
            return result

    # 4) Graph-level material (v0.7.0) — document state, applied AFTER the geometry so a
    # material failure never aborts the build mid-tree. Part graphs only (validation enforced).
    material = graph.get("material")
    if material is not None:
        rec = {"id": "_material", "type": "material", "status": "PENDING", "ops": []}
        try:
            for op in lowering.lower_material(material):
                resp = port.execute(op.tool, op.params, sv)
                result.exec_calls += 1
                sv = _advance(resp, sv, "_material", "material", op)
                rec["ops"].append({"tool": op.tool, "note": op.note, "state_version": sv})
            rec["status"] = "COMPLETED"
            result.node_log.append(rec)
        except Exception as exc:
            _record_step_failure(result, rec, sv, "_material", "material", exc)
            return result

    result.final_state_version = sv
    return result


# ---------------------------------------------------------------------------
# Per-node planners (resolve live references -> lower to tool steps)
# ---------------------------------------------------------------------------

def _plan_sketch(port: Any, node: Dict[str, Any], sv: int, _nf: Dict[str, str]) -> List[lowering.Op]:
    ref = node.get("ref") or {}
    face_ref = ref.get("face")
    if face_ref is not None:
        face_index, _info = resolve_face_by_anchor(port, sv, face_ref["near"], node_id=node.get("id"))
        return lowering.lower_sketch(node, face_index=face_index)
    offset = float(ref.get("offset") or 0.0)
    face_index = None
    if offset != 0.0:
        face_index = resolve_face_on_plane(port, sv, ref.get("datum", "top"), offset, node_id=node.get("id"))
    return lowering.lower_sketch(node, face_index=face_index)


def _plan_extrude(port: Any, node: Dict[str, Any], sv: int, _nf: Dict[str, str]) -> List[lowering.Op]:
    if node.get("end") == "up_to_surface":
        near = ((node.get("up_to") or {}).get("face") or {}).get("near")
        face_index, _info = resolve_face_by_anchor(port, sv, near, node_id=node.get("id"))
        return lowering.lower_extrude(node, up_to_face_index=face_index)
    return lowering.lower_extrude(node)


def _plan_hole(port: Any, node: Dict[str, Any], sv: int, _nf: Dict[str, str]) -> List[lowering.Op]:
    face_index, info = resolve_top_face(port, sv, node_id=node.get("id"))
    center2d = resolve_center_on_face(info)
    return lowering.lower_hole(node, face_index, center2d)


def _plan_fillet(port: Any, node: Dict[str, Any], sv: int, _nf: Dict[str, str]) -> List[lowering.Op]:
    edge_indices, _info = resolve_edges_by_anchor(port, sv, node["edges"], node_id=node.get("id"))
    return lowering.lower_fillet(node, edge_indices)


def _plan_chamfer(port: Any, node: Dict[str, Any], sv: int, _nf: Dict[str, str]) -> List[lowering.Op]:
    edge_indices, _info = resolve_edges_by_anchor(port, sv, node["edges"], node_id=node.get("id"))
    return lowering.lower_chamfer(node, edge_indices)


def _plan_edge_flange(port: Any, node: Dict[str, Any], sv: int, _nf: Dict[str, str]) -> List[lowering.Op]:
    edge_indices, _info = resolve_edges_by_anchor(port, sv, [node["edge"]], node_id=node.get("id"))
    return lowering.lower_edge_flange(node, edge_indices[0])


def _plan_mirror(_port: Any, node: Dict[str, Any], _sv: int, node_features: Dict[str, str]) -> List[lowering.Op]:
    names = []
    for fid in node["features"]:
        nm = node_features.get(fid)
        if not nm:
            raise FeatureError("mirror feature '%s' has no recorded runtime feature name "
                               "(it must be an earlier feature-producing node)" % fid,
                               code="NODE_FEATURE_UNAVAILABLE", node_id=node.get("id"))
        names.append(nm)
    return lowering.lower_mirror(node, names)


def _plan_mate(port: Any, node: Dict[str, Any], sv: int, node_features: Dict[str, str]) -> List[lowering.Op]:
    sides = []
    for side in node["sides"]:
        cid = side["component"]
        comp_name = node_features.get(cid)
        if not comp_name:
            raise FeatureError("mate side references component node '%s' with no recorded "
                               "runtime component name" % cid,
                               code="NODE_FEATURE_UNAVAILABLE", node_id=node.get("id"))
        kind, index = resolve_component_entity_by_anchor(
            port, sv, comp_name, side["anchor"], node_id=node.get("id")
        )
        sides.append((comp_name, kind, index))
    return lowering.lower_mate(node, sides)


def _plan_loft(_port: Any, node: Dict[str, Any], _sv: int, node_features: Dict[str, str]) -> List[lowering.Op]:
    names = []
    for pid in node["profiles"]:
        nm = node_features.get(pid)
        if not nm:
            raise FeatureError("loft profile '%s' has no recorded runtime sketch name "
                               "(it must be an earlier sketch node)" % pid,
                               code="NODE_FEATURE_UNAVAILABLE", node_id=node.get("id"))
        names.append(nm)
    return lowering.lower_loft(node, names)


_NodePlanner = Callable[[Any, Dict[str, Any], int, Dict[str, str]], List[lowering.Op]]

_NODE_PLANNERS: Dict[str, _NodePlanner] = {
    "box": lambda _p, node, _sv, _nf: lowering.lower_box(node),
    "sketch": _plan_sketch,
    "extrude": _plan_extrude,
    "revolve": lambda _p, node, _sv, _nf: lowering.lower_revolve(node),
    "sweep": lambda _p, node, _sv, _nf: lowering.lower_sweep(node),
    "rib": lambda _p, node, _sv, _nf: lowering.lower_rib(node),
    "linear_pattern": lambda _p, node, _sv, _nf: lowering.lower_linear_pattern(node),
    "circular_pattern": lambda _p, node, _sv, _nf: lowering.lower_circular_pattern(node),
    "hole": _plan_hole,
    "fillet": _plan_fillet,
    "chamfer": _plan_chamfer,
    "sheet_metal": lambda _p, node, _sv, _nf: lowering.lower_sheet_metal(node),
    "sketched_bend": lambda _p, node, _sv, _nf: lowering.lower_sketched_bend(node),
    "edge_flange": _plan_edge_flange,
    "mirror": _plan_mirror,
    "component": lambda _p, node, _sv, _nf: lowering.lower_component(node),
    "mate": _plan_mate,
    "loft": _plan_loft,
}


def _plan_node(port: Any, node: Dict[str, Any], state_version: int, node_features: Dict[str, str]) -> List[lowering.Op]:
    ntype = node["type"]
    planner = _NODE_PLANNERS.get(ntype)
    if planner is not None:
        return planner(port, node, state_version, node_features)
    raise FeatureError("unknown node type '%s'" % ntype, code="UNKNOWN_NODE_TYPE", node_id=node.get("id"))


def _fill_placeholders(
    op: lowering.Op,
    last_resp: Optional[Dict[str, Any]],
    node_features: Dict[str, str],
    nid: Optional[str],
    ntype: Optional[str],
) -> Dict[str, Any]:
    """Substitute runtime-name sentinels in op params (returns a copy only when needed):
      - FROM_PREV_FEATURE -> the PREVIOUS op's created feature name (cadState.features[0]);
      - node_feature(id) -> the feature name recorded when node <id> executed."""
    def _fill(v: Any) -> Any:
        if v == lowering.FROM_PREV_FEATURE:
            feats = ((last_resp or {}).get("cadState") or {}).get("features") or []
            if not feats or not feats[0]:
                raise FeatureError(
                    "op '%s' needs the previous op's created feature name, but the previous "
                    "response carried none" % op.tool,
                    code="PREV_FEATURE_UNAVAILABLE", node_id=nid, node_type=ntype, step=op.tool)
            return feats[0]
        ref = lowering.parse_node_feature(v)
        if ref is not None:
            name = node_features.get(ref)
            if not name:
                raise FeatureError(
                    "op '%s' references node '%s's created feature, but no feature name was "
                    "recorded for it" % (op.tool, ref),
                    code="NODE_FEATURE_UNAVAILABLE", node_id=nid, node_type=ntype, step=op.tool)
            return name
        return v

    if not any(v == lowering.FROM_PREV_FEATURE or lowering.parse_node_feature(v) is not None
               for v in op.params.values()):
        return op.params
    return {k: _fill(v) for k, v in op.params.items()}


def _dot3(a: List[float], b: List[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _echoes_frame(op: lowering.Op) -> bool:
    """Ops that create/activate a sketch and echo its MEASURED frame in result_geometry:
    create_sketch, and sheet_metal_feature(edge_flange_sketch)."""
    if op.tool == "create_sketch":
        return True
    return (op.tool == "sheet_metal_feature"
            and (op.params or {}).get("feature_type") == "edge_flange_sketch")


def _sketch_frame_transform(
    declared: Dict[str, List[float]],
    measured: Dict[str, List[float]],
) -> Tuple[Optional[Callable[[float, float], Tuple[float, float]]], bool]:
    """Build the 2D transform ORIGINAL-sketch-frame -> MEASURED-rebuild-frame."""
    od, xd, yd = declared["origin"], declared["xdir"], declared["ydir"]
    om, xm, ym = measured["origin"], measured["xdir"], measured["ydir"]
    d_o = [od[i] - om[i] for i in range(3)]
    tu, tv = _dot3(d_o, xm), _dot3(d_o, ym)
    a, b = _dot3(xd, xm), _dot3(yd, xm)
    c, d = _dot3(xd, ym), _dot3(yd, ym)
    if (abs(tu) < 1e-9 and abs(tv) < 1e-9
            and abs(a - 1) < 1e-9 and abs(b) < 1e-9 and abs(c) < 1e-9 and abs(d - 1) < 1e-9):
        return None, False  # identical frames — no transform needed

    def fn(u: float, v: float) -> Tuple[float, float]:
        return (round(tu + a * u + b * v, 9), round(tv + c * u + d * v, 9))

    return fn, (a * d - b * c) < 0


_ENTITY_COORD_PAIRS = (("x1", "y1"), ("x2", "y2"), ("xm", "ym"), ("cx", "cy"), ("vx", "vy"))


def _transform_entity_params(
    params: Dict[str, Any],
    frame_fn: Callable[[float, float], Tuple[float, float]],
    mirrored: bool,
) -> Dict[str, Any]:
    """Apply the sketch-frame transform to an add_sketch_entity op's 2D coordinates."""
    out = dict(params)
    for kx, ky in _ENTITY_COORD_PAIRS:
        if kx in out and ky in out:
            out[kx], out[ky] = frame_fn(out[kx], out[ky])
    if out.get("entity_type") == "spline" and "points" in out:
        flat = out["points"]
        moved = []
        for i in range(0, len(flat) - 1, 2):
            u, v = frame_fn(flat[i], flat[i + 1])
            moved.extend((u, v))
        out["points"] = moved
    if mirrored and out.get("entity_type") == "arc_center" and "direction" in out:
        out["direction"] = -out["direction"]
    return out


def _advance(
    resp: Dict[str, Any],
    sv: int,
    nid: Optional[str],
    ntype: Optional[str],
    op: lowering.Op,
) -> int:
    """Read the resulting state_version, or map a low-level FAILED to a feature-level error."""
    status = resp.get("status")
    if status == "COMPLETED":
        nv = resp.get("stateVersion")
        return nv if nv is not None else sv
    if status == "DUPLICATE":
        nv = resp.get("last_known_state_version")
        return nv if nv is not None else sv
    err = resp.get("error") or {}
    raise FeatureError("low-level tool '%s' failed: %s" % (op.tool, err.get("message")),
                       code=err.get("code") or "TOOL_FAILED",
                       node_id=nid, node_type=ntype, step=op.tool)
