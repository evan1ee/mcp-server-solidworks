"""Offline tests for the convert_to_features orchestration (server.py) — SolidWorks is faked.

Every execution-layer call goes through server._call_raw, the build through server._run_graph and the
verdict through server._compare_docs, so patching those three exercises the whole flow without COM.

Run two ways:
  - standalone:  python tests/test_convert_features.py   (from adapters/claude/)
  - pytest:      pytest tests/test_convert_features.py
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import server  # noqa: E402


class _Result:
    def __init__(self, status, node_id=None, completed=0, total=0):
        self.status = status
        self.error = {"code": "REFERENCE_UNRESOLVED", "node_id": node_id, "message": "x"} if node_id else None
        self.nodes_completed = completed
        self.nodes_total = total

    def summary(self):
        return "%s | fake" % self.status


GRAPH = {"schema_version": "0.5.0-draft", "units": "meters", "nodes": [
    {"id": "s1", "type": "sketch"}, {"id": "e1", "type": "extrude"},
    {"id": "s2", "type": "sketch"}, {"id": "c2", "type": "extrude"},
    {"id": "s3", "type": "sketch"}, {"id": "c3", "type": "extrude"},
    {"id": "fl1", "type": "fillet"}]}

IMPORT_TREE = {"features": [{"name": "Front Plane", "type": "RefPlane"}, {"name": "Origin", "type": "OriginProfileFeature"},
                            {"name": "a.step<1>", "type": "MBimport"}, {"name": "Solid Bodies1", "type": "FeatSolidBodyFolder"}]}
EDITED_TREE = {"features": IMPORT_TREE["features"] + [{"name": "Boss-Extrude1", "type": "Extrusion"}]}


class _Fake:
    """Patches server internals for one scenario; restores them on exit."""

    def __init__(self, tree=IMPORT_TREE, build_results=None, verdict=None, unexplained=()):
        self.tree = tree
        self.build_results = list(build_results or [_Result("COMPLETED")])
        self.verdict = verdict or {"verified": True, "topology_exact": True, "dv": 0.0, "da": 0.0,
                                   "topo_a": [1, 12, 30, 20], "topo_b": [1, 12, 30, 20],
                                   "missing": [], "extra": [], "cg_dist": 0.0}
        self.unexplained = tuple(unexplained)
        self.calls = []
        self.graphs = []

    def _call_raw(self, tool, params, timeout=None):
        self.calls.append((tool, params))
        doc = "a_features" if tool == "save_document" else "a.SLDPRT"
        if tool == "save_document":   # the real save writes the file the artifact hashes
            with open(params["file_path"], "wb") as fh:
                fh.write(b"sldprt")
        items = []
        if tool == "analyze_model" and params.get("analysis_type") == "features":
            items = [json.dumps(self.tree)]
        elif tool == "analyze_model" and params.get("analysis_type") == "brep":
            items = [json.dumps({"body_count": 1})]
        return {"status": "COMPLETED", "cadState": {"activeDocument": doc, "features": items}}

    def _run_graph(self, graph, fresh, want_result=False):
        self.graphs.append((graph, fresh))
        res = self.build_results.pop(0)
        return res.summary(), True, 1, res

    def _compare_docs(self, a, b, want_faces):
        return dict(self.verdict)

    def _recognize(self, brep):
        return server._RecognitionView("OK", "prismatic", json.loads(json.dumps(GRAPH)), self.unexplained, ())

    def __enter__(self):
        self.saved = {n: getattr(server, n) for n in ("_call_raw", "_run_graph", "_compare_docs", "_recognize_brep")}
        server._call_raw = self._call_raw
        server._run_graph = self._run_graph
        server._compare_docs = self._compare_docs
        server._recognize_brep = self._recognize
        return self

    def __exit__(self, *exc):
        for n, f in self.saved.items():
            setattr(server, n, f)


def _input_step():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "a.step")
    with open(p, "w") as fh:
        fh.write("ISO-10303-21;")
    return p


def test_default_output_path():
    assert server._default_output_path(os.path.normpath("C:/x/a.step")) == os.path.normpath("C:/x/a_features.SLDPRT")


def test_output_path_never_input():
    src = _input_step()
    with _Fake():
        out = server.convert_to_features(src, src)
    assert out.startswith("FAILED | OUTPUT_IS_INPUT")


def test_refuses_has_history():
    with _Fake(tree=EDITED_TREE):
        out = server.convert_to_features(_input_step())
    assert out.startswith("FAILED | HAS_HISTORY")


def test_history_less_accepts_import_types():
    assert server._is_history_less(IMPORT_TREE["features"])
    assert server._is_history_less([{"type": "BaseBody"}, {"type": "RefPlane"}])
    assert not server._is_history_less(EDITED_TREE["features"])
    assert not server._is_history_less([{"type": "RefPlane"}])


def test_drop_failed_node_pairs_sketch():
    rest, dropped = server._drop_failed(GRAPH, "c2")
    assert dropped == ["s2", "c2"] and [n["id"] for n in rest["nodes"]] == ["s3", "c3", "fl1"]
    rest, dropped = server._drop_failed(GRAPH, "s3")
    assert dropped == ["s3", "c3"] and [n["id"] for n in rest["nodes"]] == ["fl1"]
    rest, dropped = server._drop_failed(GRAPH, "fl1")
    assert dropped == ["fl1"] and rest["nodes"] == []


def test_drop_failed_node_continues():
    fake = _Fake(build_results=[_Result("FAILED", node_id="c2", completed=2, total=7), _Result("COMPLETED")])
    with fake:
        out = server.convert_to_features(_input_step())
    assert out.startswith("COMPLETED | UNVERIFIED"), out
    assert "dropped=s2,c2" in out
    assert [n["id"] for n in fake.graphs[1][0]["nodes"]] == ["s3", "c3", "fl1"] and fake.graphs[1][1] is False


def test_verified_label():
    with _Fake() as fake:
        out = server.convert_to_features(_input_step())
    assert out.startswith("COMPLETED | VERIFIED"), out
    assert any(t == "save_document" for t, _ in fake.calls)
    assert any(t == "export_document" and p.get("format") == "STEP" for t, p in fake.calls)


def test_split_topology_label():
    v = {"verified": False, "topology_exact": False, "dv": 0.0, "da": 0.0, "topo_a": [1, 12, 30, 20],
         "topo_b": [1, 11, 26, 16], "missing": [], "extra": []}
    with _Fake(verdict=v):
        out = server.convert_to_features(_input_step())
    assert out.startswith("COMPLETED | UNVERIFIED (geometry matches, topology split differs)"), out


def test_unexplained_faces_force_unverified():
    with _Fake(unexplained=(7,)):
        out = server.convert_to_features(_input_step())
    assert out.startswith("COMPLETED | UNVERIFIED") and "unexplained_faces=[7]" in out


def test_artifact_written_with_failed_mismatch():
    v = {"verified": False, "topology_exact": True, "dv": 5.0, "da": 2.0, "topo_a": [1, 1, 1, 1],
         "topo_b": [1, 1, 1, 1], "missing": [], "extra": []}
    src = _input_step()
    with _Fake(verdict=v):
        server.convert_to_features(src)
    art = os.path.join(os.path.dirname(src), ".solidpilot", "a_features.SLDPRT.analysis.json")
    with open(art, encoding="utf-8") as fh:
        a = json.load(fh)
    assert a["ir"]["verification"]["status"] == "failed"
    assert a["ir"]["verification"]["detail"]["reason"] == "MISMATCH"
    assert a["ir"]["graph"]["nodes"][0]["id"] == "s1"


def test_moved_feature_is_not_verified():
    # counts + volume + area all equal, but the CG moved: a hole in the wrong place
    v = {"verified": True, "topology_exact": True, "dv": 0.0, "da": 0.0, "topo_a": [1, 7, 14, 8],
         "topo_b": [1, 7, 14, 8], "missing": [], "extra": [], "cg_dist": 6e-4}
    with _Fake(verdict=v):
        out = server.convert_to_features(_input_step())
    assert out.startswith("COMPLETED | UNVERIFIED"), out


def test_face_diff_blocks_verified():
    v = {"verified": True, "topology_exact": True, "dv": 0.0, "da": 0.0, "topo_a": [1, 7, 14, 8],
         "topo_b": [1, 7, 14, 8], "missing": [{"i": 3}], "extra": [{"i": 4}], "cg_dist": 0.0}
    with _Fake(verdict=v):
        out = server.convert_to_features(_input_step())
    assert out.startswith("COMPLETED | UNVERIFIED"), out


def test_existing_output_refused_without_overwrite():
    src = _input_step()
    out_path = os.path.join(os.path.dirname(src), "other.SLDPRT")
    with open(out_path, "wb") as fh:
        fh.write(b"precious")
    with _Fake():
        out = server.convert_to_features(src, out_path)
    assert out.startswith("FAILED | OUTPUT_EXISTS"), out
    with open(out_path, "rb") as fh:
        assert fh.read() == b"precious"
    with _Fake():
        out = server.convert_to_features(src, out_path, overwrite=True)
    assert out.startswith("COMPLETED"), out


def test_base_node_failure_is_build_failed():
    fake = _Fake(build_results=[_Result("FAILED", node_id="e1", completed=1, total=7)])
    with fake:
        out = server.convert_to_features(_input_step())
    assert out.startswith("FAILED | BUILD_FAILED"), out
    assert not any(t == "save_document" for t, _ in fake.calls)


def test_unknown_failed_node_is_build_failed():
    fake = _Fake(build_results=[_Result("FAILED", node_id="_material", completed=7, total=7)])
    with fake:
        out = server.convert_to_features(_input_step())
    assert out.startswith("FAILED | BUILD_FAILED"), out


def _run_all():
    failures = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
        except Exception as ex:  # noqa: BLE001
            failures += 1
            sys.stderr.write("FAIL %s: %s: %s\n" % (name, type(ex).__name__, ex))
    sys.stdout.write("%d/%d convert tests passed\n" % (len(tests) - failures, len(tests)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
