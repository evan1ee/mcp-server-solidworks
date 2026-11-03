"""Adapter-side STEP / history-less solid to feature-based part pipeline (`convert_to_features`)."""
import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Callable
from adapter_log import write as _adapter_log
from analysis_pipeline import ANALYSIS_SCHEMA_VERSION
from execution_client import ExecutionLayerError
from state_manager import analysis_items

_CONVERT_INPUT_EXTS = (".step", ".stp", ".igs", ".iges", ".sldprt")
_IMPORT_FEATURE_TYPES = ("MBimport", "BaseBody")   # what a STEP import leaves in the tree (SW 2025)
_NON_SOLID_TYPES = ("RefPlane", "RefAxis", "RefPoint", "OriginProfileFeature", "DetailCabinet")
_BREP_TIMEOUT_S = 600.0     # brep is ~0.15–0.3 s per face/edge over cross-process COM
_MAX_BUILD_ROUNDS = 10
_CONVERT_CG_TOL_M = 1e-5    # 10 µm: a feature built in the wrong PLACE moves the centre of gravity


class _RecognitionView(tuple):
    """(status, part_class, graph, unexplained_faces, notes) — the shape convert_to_features reads
    from recognition.RecognitionResult (a separate type so tests can fake the recognizer)."""
    __slots__ = ()

    def __new__(cls, status, part_class, graph, unexplained_faces, notes):
        return tuple.__new__(cls, (status, part_class, graph, tuple(unexplained_faces), tuple(notes)))

    status = property(lambda s: s[0])
    part_class = property(lambda s: s[1])
    graph = property(lambda s: s[2])
    unexplained_faces = property(lambda s: s[3])
    notes = property(lambda s: s[4])


def recognize_brep(brep: dict, recognition_mod, import_error: str = None) -> _RecognitionView:
    if recognition_mod is None:
        return _RecognitionView("RECOGNIZER_UNAVAILABLE", None, None, (), (import_error,))
    r = recognition_mod.recognize(brep)
    return _RecognitionView(r.status, r.part_class, r.graph, r.unexplained_faces, r.notes)


def _default_output_path(src: str) -> str:
    stem = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(os.path.dirname(src), stem + "_features.SLDPRT")


def _is_history_less(features: list) -> bool:
    """True iff the tree holds an import body and NOTHING solid-producing besides it (folders,
    datums and the origin are ignored). A sketch or any modelling feature ⇒ real history."""
    kinds = [f.get("type", "") for f in features if isinstance(f, dict)]
    relevant = [k for k in kinds if "Folder" not in k and k not in _NON_SOLID_TYPES]
    return bool(relevant) and all(k in _IMPORT_FEATURE_TYPES for k in relevant)


def _drop_failed(graph: dict, failed_id: str):
    """(graph of the nodes AFTER the failed one, dropped ids). A failed extrude/revolve takes its
    sketch with it; a failed sketch takes the feature that consumes it."""
    nodes = graph.get("nodes") or []
    idx = next((i for i, n in enumerate(nodes) if n.get("id") == failed_id), None)
    if idx is None:
        raise KeyError(failed_id)
    lo, hi = idx, idx
    kind = nodes[idx].get("type")
    if kind in ("extrude", "revolve") and idx > 0 and nodes[idx - 1].get("type") == "sketch":
        lo = idx - 1
    elif kind == "sketch" and idx + 1 < len(nodes) and nodes[idx + 1].get("type") in ("extrude", "revolve"):
        hi = idx + 1
    rest = dict(graph, nodes=nodes[hi + 1:])
    return rest, [n.get("id") for n in nodes[lo:hi + 1]]


def _active_title(resp: dict) -> str:
    return ((resp or {}).get("cadState") or {}).get("activeDocument") or ""


def _build_with_drops(graph: dict, run_graph_fn: Callable):
    """Build the graph in a fresh part; on a node failure drop that node (+ its sketch partner) and
    continue with the rest in the same document. Returns (ok, dropped_ids, last_text)."""
    remaining, dropped, fresh = graph, [], True
    base_ids = {n.get("id") for n in (graph.get("nodes") or [])[:2]}   # the base sketch + boss/revolve
    for _ in range(_MAX_BUILD_ROUNDS):
        if not remaining.get("nodes"):
            return True, dropped, "COMPLETED | nothing left to build"
        text, ok, _sv, result = run_graph_fn(remaining, fresh, want_result=True)
        fresh = False
        if not ok or result is None:
            return False, dropped, text
        if result.status == "COMPLETED":
            return True, dropped, text
        node_id = (result.error or {}).get("node_id")
        if not node_id or node_id in base_ids:
            return False, dropped, text   # no base body => nothing worth saving
        _adapter_log(f"convert_to_features: node {node_id} failed — dropping it and continuing ({text})")
        try:
            remaining, gone = _drop_failed(remaining, node_id)
        except KeyError:
            return False, dropped, text
        dropped.extend(gone)
    return False, dropped, "FAILED | TOO_MANY_FAILURES"


def _verify_by_reimport(original_title: str, rebuild_title: str, call_raw: Callable, compare_docs_fn: Callable):
    """Compare like with like: a STEP import splits every cylinder/cone in two, a native rebuild
    does not — so the rebuild is exported to STEP, re-imported, and THAT is compared to the original
    import (the strict verified definition then applies unchanged)."""
    tmp = os.path.join(tempfile.gettempdir(), "solidpilot_verify_%s.step" % uuid.uuid4().hex[:8])
    call_raw("activate_document", {"title": rebuild_title})
    exp = call_raw("export_document", {"format": "STEP", "file_path": tmp})
    if exp.get("status") != "COMPLETED":
        raise RuntimeError("VERIFY_EXPORT_FAILED | %s" % (exp.get("error") or {}).get("message"))
    opened = call_raw("open_document", {"file_path": tmp})
    if opened.get("status") != "COMPLETED":
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError("VERIFY_IMPORT_FAILED | %s" % (opened.get("error") or {}).get("message"))
    reimport_title = _active_title(opened)
    try:
        return compare_docs_fn(original_title, reimport_title, True)
    finally:
        call_raw("activate_document", {"title": reimport_title})
        call_raw("close_document", {"save": False})
        try:
            os.remove(tmp)
        except OSError:
            pass
        call_raw("activate_document", {"title": rebuild_title})


def _convert_label(verdict: dict, dropped: list, unexplained) -> str:
    """VERIFIED needs more than the counts + |dV|,|dA| criteria, which are blind to WHERE a feature
    sits (a hole mirrored to the wrong side keeps every count, volume and area): the face diff must
    pair every surface and the centre of gravity must not move."""
    placed = (not verdict.get("missing") and not verdict.get("extra")
              and verdict.get("cg_dist") is not None and verdict["cg_dist"] <= _CONVERT_CG_TOL_M)
    if verdict.get("verified") and placed and not dropped and not unexplained:
        return "VERIFIED"
    geometry_ok = (not verdict.get("missing") and not verdict.get("extra")
                   and verdict.get("dv") is not None and abs(verdict["dv"]) <= 1.0
                   and verdict.get("da") is not None and abs(verdict["da"]) <= 1.0)
    if not verdict.get("topology_exact") and geometry_ok and not dropped and not unexplained:
        return "UNVERIFIED (geometry matches, topology split differs)"
    return "UNVERIFIED"


def _write_convert_artifact(out: str, graph: dict, verdict: dict, label: str, dropped, unexplained) -> str:
    with open(out, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    status = "verified" if label == "VERIFIED" else "failed"
    cg = verdict.get("cg_dist")
    detail = {"topology": {"original": verdict.get("topo_a"), "rebuilt": verdict.get("topo_b")},
              "volume_delta_pct": verdict.get("dv"), "surface_delta_pct": verdict.get("da"),
              "cg_delta_mm": None if cg is None else cg * 1000.0}
    if status == "failed":
        detail.update({"reason": "BUILD_FAILED" if dropped else "MISMATCH", "dropped_nodes": list(dropped),
                       "unexplained_faces": list(unexplained),
                       "missing_faces": len(verdict.get("missing") or []), "extra_faces": len(verdict.get("extra") or [])})
    artifact = {
        "identity": {"source_path": out, "source_filename": os.path.basename(out), "source_hash": "sha256:" + sha,
                     "source_mtime": datetime.fromtimestamp(os.path.getmtime(out), timezone.utc).isoformat(),
                     "schema_version": ANALYSIS_SCHEMA_VERSION,
                     "analyzed_at": datetime.now(timezone.utc).isoformat(),
                     "generator": {"kind": "deterministic"}},
        "document_type": "part",
        "recipe": None,
        "parameters": [],
        "ir": {"ir_schema_version": graph.get("schema_version"), "graph": graph,
               "generator": {"kind": "deterministic", "recipe_version": "recognition-v1"},
               "verification": {"status": status, "method": "convert_to_features: rebuild → STEP re-import + compare_parts",
                                "verified_at": datetime.now(timezone.utc).isoformat(), "detail": detail}},
        "relationships": {"drawings": [], "category": None, "cluster_signals": {}},
        "notes": ["written by convert_to_features; recipe not extracted (the IR is the recipe)"],
    }
    out_dir = os.path.join(os.path.dirname(out), ".solidpilot")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, os.path.basename(out) + ".analysis.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2)
    return path


def run_convert_to_features(
    file_path: str,
    output_path: str,
    overwrite: bool,
    call_raw: Callable,
    run_graph_fn: Callable,
    compare_docs_fn: Callable,
    recognize_brep_fn: Callable,
) -> str:
    """Implementation behind the `convert_to_features` MCP tool."""
    src = os.path.normpath(os.path.abspath(file_path))
    if not os.path.isfile(src):
        return f"FAILED | FILE_NOT_FOUND | {src}"
    if not src.lower().endswith(_CONVERT_INPUT_EXTS):
        return f"FAILED | UNSUPPORTED_TYPE | expected one of {', '.join(_CONVERT_INPUT_EXTS)}"
    out = os.path.normpath(os.path.abspath(output_path)) if output_path else _default_output_path(src)
    if os.path.normcase(out) == os.path.normcase(src) or (os.path.exists(out) and os.path.samefile(out, src)):
        return "FAILED | OUTPUT_IS_INPUT | choose an output path different from the input"
    if os.path.exists(out) and not overwrite:
        return f"FAILED | OUTPUT_EXISTS | {out} already exists — pass overwrite=True to replace it"

    try:
        opened = call_raw("open_document", {"file_path": src})
        if opened.get("status") != "COMPLETED":
            err = opened.get("error") or {}
            return f"FAILED | OPEN_FAILED | {err.get('code')}: {err.get('message')}"
        original_title = _active_title(opened)
        tree_items = analysis_items(call_raw("analyze_model", {"analysis_type": "features", "name": ""}))
        tree = json.loads(tree_items[0]).get("features", []) if tree_items else []
        if not _is_history_less(tree):
            return ("FAILED | HAS_HISTORY | the part already has a feature tree "
                    "(only imported bodies are converted)")
        brep_items = analysis_items(call_raw("analyze_model", {"analysis_type": "brep", "name": ""},
                                             timeout=_BREP_TIMEOUT_S))
        brep = json.loads(brep_items[0]) if brep_items else {}
    except (RuntimeError, ValueError, ExecutionLayerError) as ex:
        return f"FAILED | READ_FAILED | {ex}"

    rec = recognize_brep_fn(brep)
    if rec.status != "OK":
        return f"FAILED | {rec.status} | {'; '.join(str(n) for n in rec.notes)}"

    built, dropped, build_text = _build_with_drops(rec.graph, run_graph_fn)
    if not built:
        return f"FAILED | BUILD_FAILED | {build_text}"
    rebuild_title = _active_title(call_raw("verify_state", {}))
    saved = call_raw("save_document", {"file_path": out})
    if saved.get("status") != "COMPLETED":
        err = saved.get("error") or {}
        return f"FAILED | SAVE_FAILED | {err.get('code')}: {err.get('message')}"
    rebuild_title = _active_title(saved) or rebuild_title

    try:
        verdict = _verify_by_reimport(original_title, rebuild_title, call_raw, compare_docs_fn)
    except (RuntimeError, ExecutionLayerError) as ex:
        verdict = {"verified": False, "topology_exact": False, "dv": None, "da": None,
                   "topo_a": None, "topo_b": None, "missing": [], "extra": [], "error": str(ex)}
    label = _convert_label(verdict, dropped, rec.unexplained_faces)
    artifact = _write_convert_artifact(out, rec.graph, verdict, label, dropped, rec.unexplained_faces)

    fmt = lambda v: "?" if v is None else format(v, ".4f")  # noqa: E731
    kinds = {}
    for n in rec.graph.get("nodes", []):
        if n.get("type") != "sketch":
            kinds[n["type"]] = kinds.get(n["type"], 0) + 1
    line = (f"COMPLETED | {label} | saved={out} | class={rec.part_class} | "
            f"features={','.join(f'{k}x{v}' for k, v in kinds.items())} | dV={fmt(verdict.get('dv'))}% "
            f"dA={fmt(verdict.get('da'))}% | topology A={verdict.get('topo_a')} B={verdict.get('topo_b')} | "
            f"missing_faces={len(verdict.get('missing') or [])} extra_faces={len(verdict.get('extra') or [])} | "
            f"cg_shift={fmt(None if verdict.get('cg_dist') is None else verdict['cg_dist'] * 1000.0)}mm")
    if dropped:
        line += f" | dropped={','.join(dropped)}"
    if rec.unexplained_faces:
        line += f" | unexplained_faces={list(rec.unexplained_faces)}"
    if verdict.get("error"):
        line += f" | verify_error={verdict['error']}"
    return line + f" | artifact={artifact}"
