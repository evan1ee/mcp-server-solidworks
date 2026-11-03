"""Regression and unit test suite for the SolidPilot MCP adapter and its internal modules.

Runs entirely without SolidWorks or the C# execution server by exercising fake callables
and temporary files, plus verifying the MCP tool and resource surface against contracts.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ADAPTER_DIR = os.path.normpath(os.path.join(_HERE, ".."))
if _ADAPTER_DIR not in sys.path:
    sys.path.insert(0, _ADAPTER_DIR)

from mcp.server.mcpserver.exceptions import ResourceError  # noqa: E402

import analysis_pipeline  # noqa: E402
import comparison  # noqa: E402
import drawing_pipeline  # noqa: E402
import ir_runner  # noqa: E402
import recipes  # noqa: E402
import server  # noqa: E402
import state_manager  # noqa: E402


class TestStateManager(unittest.TestCase):
    def test_next_operation_id_uniqueness(self):
        id1 = state_manager.next_operation_id()
        id2 = state_manager.next_operation_id()
        self.assertNotEqual(id1, id2)
        self.assertEqual(len(id1), 36)

    def test_is_state_mismatch(self):
        self.assertTrue(
            state_manager.is_state_mismatch(
                {"status": "FAILED", "error": {"code": "INVALID_STATE_VERSION"}}
            )
        )
        self.assertFalse(
            state_manager.is_state_mismatch(
                {"status": "COMPLETED", "error": {"code": "INVALID_STATE_VERSION"}}
            )
        )
        self.assertFalse(
            state_manager.is_state_mismatch(
                {"status": "FAILED", "error": {"code": "OTHER_ERROR"}}
            )
        )

    def test_delta_pct(self):
        self.assertAlmostEqual(state_manager.delta_pct(100.0, 101.5), 1.5)
        self.assertIsNone(state_manager.delta_pct(0.0, 10.0))
        self.assertIsNone(state_manager.delta_pct(None, 10.0))

    def test_kv_dict_handles_comma_decimals_and_ints(self):
        items = ["volume=4,22503E-06", "faces=12", "edges=-4", "label=box=1"]
        parsed = state_manager.kv_dict(items)
        self.assertAlmostEqual(parsed["volume"], 4.22503e-06)
        self.assertIsInstance(parsed["faces"], int)
        self.assertEqual(parsed["faces"], 12)
        self.assertIsInstance(parsed["edges"], int)
        self.assertEqual(parsed["edges"], -4)
        self.assertEqual(parsed["label"], "box=1")

    def test_analysis_items_raises_on_failed(self):
        with self.assertRaises(RuntimeError) as ctx:
            state_manager.analysis_items(
                {"status": "FAILED", "error": {"code": "NO_DOC", "message": "none"}}
            )
        self.assertIn("NO_DOC: none", str(ctx.exception))


class TestRecipesAndResources(unittest.TestCase):
    def test_get_recipe_index_and_sections(self):
        idx = server.get_recipe(section="index")
        self.assertIn("Sections (pass as `section`):", idx)
        for slug in recipes._RECIPE_SECTIONS:
            self.assertIn(f"- {slug} — ", idx)
            sec_text = server.get_recipe(section=slug)  # type: ignore[arg-type]
            self.assertTrue(sec_text.startswith(f"## {slug} — "))

    def test_get_recipe_schemas_and_profile_trim(self):
        all_schema = json.loads(
            server.get_recipe(section="feature_graph_schema", profile="all")
        )
        part_schema = json.loads(
            server.get_recipe(section="feature_graph_schema", profile="part")
        )
        self.assertNotIn("status", all_schema.get("_meta", {}))
        self.assertIn("assembly_types_v06", all_schema)
        self.assertNotIn("assembly_types_v06", part_schema)

        art_schema = json.loads(server.get_recipe(section="analysis_artifact_schema"))
        self.assertIn("artifact", art_schema)
        self.assertIn("identity", art_schema)

    def test_invalid_recipe_section_raises_resource_error(self):
        with self.assertRaises(ResourceError) as ctx:
            recipes._recipe_section_text("nonexistent_slug")
        self.assertIn("not a served recipe section", str(ctx.exception))


class TestDrawingPipeline(unittest.TestCase):
    def test_advisories_and_summarise_direct(self):
        art = {
            "source": {"file": "bracket.dxf", "sha256": "abcdef1234567890"},
            "sheet": {"not_read": {"HATCH": 2}},
            "views": [
                {
                    "vid": "v0",
                    "scale_factor": 2.0,
                    "loops": [{"id": "L0", "tier": "B"}],
                }
            ],
            "dimensions": [{"kind": "angular", "printed_mismatch": True}],
            "bend_notes": [{"unpaired": True}, {"bend_line": {"in_loop": True}}],
            "view_graph": {"solved": False, "reason": "disconnected"},
        }
        adv = drawing_pipeline.advisories(art)
        self.assertIn("ADVISORIES (7)", adv)
        self.assertIn("view scale_factor @ v0", adv)
        self.assertIn("not_read @ sheet (HATCH)", adv)
        self.assertIn("printed_mismatch/ANGULAR @ dim[0]", adv)
        self.assertIn("tier:B @ v0/L0", adv)
        self.assertIn("unpaired bend x1", adv)
        self.assertIn("bend_line in_loop x1", adv)
        self.assertIn("view_graph.solved:false (disconnected)", adv)

        summary = {
            "blank": {"size": [100, 50], "outer_segments": 4, "cutouts": [], "area_mm2": 5000},
            "thickness": {"value_mm": 2.0, "source": "note"},
            "k_factor": 0.5,
            "bends": [
                {
                    "dir": "UP",
                    "angle_deg": 90,
                    "radius_mm": 2.0,
                    "line": [0, 25, 100, 25],
                    "class": "center",
                    "in_loop": False,
                }
            ],
            "skipped_bends": [{"dir": "DOWN", "at": [10, 10], "reason": "ambiguous", "candidates": 2}],
            "expected": {"volume_m3": 1e-5},
        }
        direct_txt = drawing_pipeline.summarise_direct(art, summary, {"nodes": [{}, {}]})
        self.assertIn("DIRECT_BUILDABLE | sheet-metal flat pattern | bracket.dxf", direct_txt)
        self.assertIn("SKIPPED    DOWN note", direct_txt)

        build_txt = drawing_pipeline.format_direct_build_result(
            art, summary, lambda g, fresh_document=True: ("COMPLETED | 2/2 nodes", True, 7), {"nodes": [{}, {}]}
        )
        self.assertIn("built from bracket.dxf (sha256 abcdef123456) | state_version=7", build_txt)
        self.assertIn("NOT BUILT: DOWN bend", build_txt)


class TestAnalysisPipeline(unittest.TestCase):
    def test_save_analysis_part_and_assembly(self):
        with tempfile.TemporaryDirectory() as tmp:
            part_path = os.path.join(tmp, "sample.SLDPRT")
            drw_path = os.path.join(tmp, "sample drawing.SLDDRW")
            asm_path = os.path.join(tmp, "assy.SLDASM")
            with open(part_path, "wb") as f:
                f.write(b"part-bytes")
            with open(drw_path, "wb") as f:
                f.write(b"drw-bytes")
            with open(asm_path, "wb") as f:
                f.write(b"asm-bytes")

            def fake_call_raw(tool_name, params, timeout=None):
                if tool_name == "open_document":
                    return {"status": "COMPLETED"}
                if tool_name == "analyze_model":
                    atype = params["analysis_type"]
                    if atype == "features":
                        payload = {
                            "features": [
                                {
                                    "name": "Boss-Extrude1",
                                    "type": "Extrusion",
                                    "dimensions": [{"name": "D1@Boss-Extrude1@sample.Part", "value_si": 0.025}],
                                }
                            ]
                        }
                        return {"status": "COMPLETED", "cadState": {"features": [json.dumps(payload)]}}
                    if atype == "mass_properties":
                        return {
                            "status": "COMPLETED",
                            "cadState": {"features": ["volume=1.5e-05", "surface_area=0.004", "cx=0.01", "cy=0.02", "cz=0.03"]},
                        }
                    if atype == "geometry":
                        return {
                            "status": "COMPLETED",
                            "cadState": {"features": ["bodies=1", "faces=6", "edges=12", "vertices=8"]},
                        }
                if tool_name == "analyze_assembly":
                    atype = params["analysis_type"]
                    if atype == "components":
                        payload = {"component_count": 1, "components": [{"name": "sample-1", "path": part_path}]}
                        return {"status": "COMPLETED", "cadState": {"features": [json.dumps(payload)]}}
                    if atype == "mates":
                        payload = {"mate_count": 1, "mates": [{"name": "Coincident1", "type": "coincident"}]}
                        return {"status": "COMPLETED", "cadState": {"features": [json.dumps(payload)]}}
                raise AssertionError(f"Unexpected tool call: {tool_name} {params}")

            res_part = analysis_pipeline.run_save_analysis(part_path, fake_call_raw)
            self.assertIn("COMPLETED | artifact=", res_part)
            self.assertIn("features=1 | parameters=1", res_part)
            self.assertIn("drawings_linked=1", res_part)

            art_file = os.path.join(tmp, ".solidpilot", "sample.SLDPRT.analysis.json")
            self.assertTrue(os.path.isfile(art_file))
            with open(art_file, "r", encoding="utf-8") as fh:
                art_doc = json.load(fh)
            self.assertEqual(art_doc["document_type"], "part")
            self.assertEqual(len(art_doc["parameters"]), 1)

            res_asm = analysis_pipeline.run_save_analysis(asm_path, fake_call_raw)
            self.assertIn("COMPLETED | artifact=", res_asm)
            self.assertIn("components=1 | mates=1 | part_files=1", res_asm)


class TestComparison(unittest.TestCase):
    def test_compare_parts_with_faces(self):
        def fake_call_raw(tool_name, params, timeout=None):
            if tool_name == "activate_document":
                return {"status": "COMPLETED"}
            if tool_name == "analyze_model":
                atype = params["analysis_type"]
                if atype == "geometry":
                    return {"status": "COMPLETED", "cadState": {"features": ["bodies=1", "faces=6", "edges=12", "vertices=8"]}}
                if atype == "mass_properties":
                    return {"status": "COMPLETED", "cadState": {"features": ["volume=1.0e-5", "surface_area=6.0e-3", "cx=0.0", "cy=0.0", "cz=0.0"]}}
                if atype == "faces":
                    faces = [{"i": 0, "planar": True, "normal": [0, 0, 1], "point": [0, 0, 0.01], "area": 0.001}]
                    return {"status": "COMPLETED", "cadState": {"features": [json.dumps({"faces": faces})]}}
            raise AssertionError(f"Unexpected: {tool_name}")

        out = comparison.run_compare_parts(
            "PartA", "PartB", "faces", lambda a, b, wf: comparison.compare_docs(a, b, wf, fake_call_raw)
        )
        self.assertIn("COMPLETED | verified_criteria=PASS", out)
        self.assertIn("faces: matched=1 MISSING(in A only)=0 EXTRA(in B only)=0", out)

    def test_compare_assemblies(self):
        def fake_call_raw(tool_name, params, timeout=None):
            if tool_name == "activate_document":
                return {"status": "COMPLETED"}
            if tool_name == "analyze_assembly":
                if params["analysis_type"] == "components":
                    comps = {"components": [{"path": "C:/p1.SLDPRT", "config": "Default", "transform": [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]}]}
                    return {"status": "COMPLETED", "cadState": {"features": [json.dumps(comps)]}}
                if params["analysis_type"] == "mates":
                    mates = {"mates": [{"type": "coincident"}]}
                    return {"status": "COMPLETED", "cadState": {"features": [json.dumps(mates)]}}
            if tool_name == "analyze_model":
                return {"status": "COMPLETED", "cadState": {"features": ["volume=1.0e-5", "surface_area=6.0e-3"]}}
            raise AssertionError(f"Unexpected: {tool_name}")

        out = comparison.run_compare_assemblies("AssemA", "AssemB", fake_call_raw)
        self.assertIn("COMPLETED | verified_criteria=PASS", out)
        self.assertIn("components A=1 B=1 set=EXACT", out)


class TestIrRunnerAndStateResync(unittest.TestCase):
    def test_submit_feature_graph_invalid_json(self):
        res = ir_runner.run_submit_feature_graph("{bad json", True, lambda g, f: ("", False, 0))
        self.assertTrue(res.startswith("FAILED | INVALID_JSON |"))

    def test_rebuild_from_ir_stale_source_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_file = os.path.join(tmp, "part.SLDPRT")
            art_file = os.path.join(tmp, "part.SLDPRT.analysis.json")
            with open(src_file, "wb") as f:
                f.write(b"modified-content")
            art = {
                "identity": {"source_path": src_file, "source_hash": "sha256:000000"},
                "ir": {"graph": {"schema_version": "0.11.0-draft", "nodes": [{"id": "s1", "type": "sketch"}]}},
            }
            with open(art_file, "w", encoding="utf-8") as f:
                json.dump(art, f)

            res = ir_runner.run_rebuild_from_ir(art_file, True, lambda g, f: ("COMPLETED | 1/1 nodes", True, 5))
            self.assertIn("COMPLETED | 1/1 nodes", res)
            self.assertIn("source_stale=true", res)

    def test_call_resyncs_on_invalid_state_version(self):
        calls = []
        orig_call_tool = server.call_tool
        orig_get_state = server.get_state
        try:
            def fake_call_tool(tool_name, op_id, sv, params, timeout=None):
                calls.append((tool_name, sv))
                if len(calls) == 1:
                    return {"status": "FAILED", "error": {"code": "INVALID_STATE_VERSION", "message": "mismatch"}}
                return {"status": "COMPLETED", "stateVersion": 42, "cadState": {"activeDocument": "Part1"}}

            server.call_tool = fake_call_tool
            server.get_state = lambda: 41
            server._state_version = 5

            result = server._call("open_new_part", {})
            self.assertIn("COMPLETED", result)
            self.assertEqual(calls, [("open_new_part", 5), ("open_new_part", 41)])
            self.assertEqual(server._state_version, 42)
        finally:
            server.call_tool = orig_call_tool
            server.get_state = orig_get_state


if __name__ == "__main__":
    unittest.main()
