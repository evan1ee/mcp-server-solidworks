"""Offline tests for dimension_plan (no SolidWorks needed)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dimension_plan as dp  # noqa: E402

FACTS = {
    "width": 0.12, "height": 0.08,
    "boss": {"diameter": 0.05}, "bore": {"diameter": 0.03},
    "hole_groups": [
        {"role": "mount", "diameter": 0.008,
         "centers": [[-0.05, -0.03], [0.05, -0.03], [-0.05, 0.03], [0.05, 0.03]]},
        {"role": "pcd", "diameter": 0.005, "depth": 0.012,
         "centers": [[0.02, 0], [0, 0.02], [-0.02, 0], [0, -0.02]]},
    ],
    "depth": {"total": 0.05, "flange": 0.015, "boss": 0.035, "boss_width": 0.05},
}
VIEWS = {"front": {"pos": [0.12, 0.12], "scale": 1.0}, "top": {"pos": [0.12, 0.235], "scale": 1.0}}


def _by_label(plan):
    return {d["label"]: d for d in plan}


def test_plan_is_deterministic():
    assert dp.plan_dimensions(FACTS, VIEWS) == dp.plan_dimensions(FACTS, VIEWS)


def test_expected_values_cover_the_scheme():
    plan = _by_label(dp.plan_dimensions(FACTS, VIEWS))
    expected = {k: v["expected_value"] for k, v in plan.items()}
    assert expected["overall width"] == 0.12
    assert expected["overall height"] == 0.08
    assert expected["hole pitch x"] == 0.1
    assert expected["hole pitch y"] == 0.06
    assert expected["PCD"] == 0.04
    assert expected["overall depth"] == 0.05
    assert expected["flange thickness"] == 0.015
    assert expected["boss length"] == 0.035
    assert expected["boss width"] == 0.05


def test_hole_callouts_carry_count_prefix():
    plan = dp.plan_dimensions(FACTS, VIEWS)
    prefixes = {d["expected_value"]: d["prefix"] for d in plan if d["label"].startswith("callout ")}
    assert prefixes == {0.008: "4×Ø", 0.005: "4×Ø"}


def test_no_duplicate_overall_dimensions_across_views():
    plan = dp.plan_dimensions(FACTS, VIEWS)
    overall = [d["expected_value"] for d in plan if d["label"].startswith("overall")]
    assert len(overall) == len(set(overall))


def test_outer_tier_is_farther_than_inner_tier():
    plan = _by_label(dp.plan_dimensions(FACTS, VIEWS))
    top = 0.12 + 0.04
    assert plan["overall width"]["text_y"] - top > plan["hole pitch x"]["text_y"] - top > 0


def test_precision_hides_trailing_zeros():
    assert dp._fmt_precision(0.12) == 0
    assert dp._fmt_precision(0.0015) == 1


def test_cross_view_duplicate_overall_rejected():
    plan = [
        {"expected_value": 0.08, "orientation": "vertical", "prefix": "", "view": "front", "label": "overall height"},
        {"expected_value": 0.08, "orientation": "vertical", "prefix": "", "view": "right", "label": "overall height"},
    ]
    with pytest.raises(ValueError):
        dp._reject_duplicates(plan)


def test_boss_diameter_not_repeated_in_front_when_depth_view_dimensions_it():
    labels = [d["label"] for d in dp.plan_dimensions(FACTS, VIEWS)]
    assert "boss diameter" not in labels and "boss width" in labels


def _bracket_facts():
    import math
    four = [[sx * 0.04, sy * 0.02] for sx in (-1, 1) for sy in (-1, 1)]
    six = [[round(0.012 * math.cos(math.radians(a)), 6), round(0.012 * math.sin(math.radians(a)), 6)]
           for a in range(0, 360, 60)]
    return {
        "width": 0.10, "height": 0.06, "boss": {"diameter": 0.030}, "bore": {"diameter": 0.016},
        "corner_radius": 0.005,
        "slot": {"x": [-0.015, 0.015], "y": [-0.0265, -0.0185]},
        "rib": {"thickness": 0.006, "z_end": 0.026},
        "hole_groups": [
            {"role": "mount", "diameter": 0.006, "depth": None, "centers": four},
            {"role": "pcd", "diameter": 0.003, "depth": 0.008, "centers": six},
            {"role": "cbore", "diameter": 0.010, "depth": 0.003, "centers": four},
        ],
        "depth": {"total": 0.032, "flange": 0.012, "boss": 0.020, "boss_width": 0.030, "boss_chamfer": 0.001},
    }


def test_bracket_plan_covers_every_feature_with_expected_values():
    plan = _by_label(dp.plan_dimensions(_bracket_facts(), VIEWS))
    got = {k: v["expected_value"] for k, v in plan.items()}
    assert got["slot length"] == 0.03 and got["slot width"] == 0.008
    assert got["corner radius"] == 0.005 and got["rib thickness"] == 0.006
    assert got["PCD"] == 0.024 and got["bore diameter"] == 0.016
    assert got["overall depth"] == 0.032 and got["boss length"] == 0.02


def test_hole_callouts_use_thru_or_deep_suffix_and_separate_rows():
    plan = dp.plan_dimensions(_bracket_facts(), VIEWS)
    calls = [d for d in plan if d["label"].startswith("callout ")]
    assert [d["suffix"] for d in calls] == [" THRU", " DEEP 8  ON Ø24 PCD", " DEEP 3"]
    assert len({d["text_y"] for d in calls}) == 3


def test_small_hole_pcd_picks_stay_off_the_center_line():
    # Picks within ~30 deg of the axis landed on the extended center line on live SolidWorks.
    pcd = _by_label(dp.plan_dimensions(_bracket_facts(), VIEWS))["PCD"]
    cy = VIEWS["front"]["pos"][1]
    assert pcd["y1"] - cy > 0.0005 and pcd["y2"] - cy > 0.0005


def test_small_hole_callout_pick_stays_off_the_center_line():
    # Live: the 6x callout pick on the 0 deg point measured the 28 mm chamfer circle, not the 3 mm hole.
    calls = [d for d in dp.plan_dimensions(_bracket_facts(), VIEWS) if d["label"] == "callout 6x"]
    cy = VIEWS["front"]["pos"][1]
    assert calls[0]["y1"] - cy > 0.0005


def test_depth_picks_clear_the_boss_end_chamfer_line():
    plan = _by_label(dp.plan_dimensions(_bracket_facts(), VIEWS))
    boss_end = VIEWS["top"]["pos"][1] - 0.016
    assert plan["overall depth"]["y2"] < boss_end and plan["boss length"]["y2"] < boss_end


def _pairs(plan):
    return {frozenset(p) for p in dp.check_crossings(plan)}


def test_bracket_plan_has_no_dimension_crossings():
    assert dp.check_crossings(dp.plan_dimensions(_bracket_facts(), VIEWS)) == []


def test_flange_block_plan_has_no_dimension_crossings():
    assert dp.check_crossings(dp.plan_dimensions(FACTS, VIEWS)) == []


def test_check_crossings_detects_extension_through_dimension_line():
    inner = dp._dim(0, 0, 0.0, -0.01, 0.02, x2=0.02, y2=0, orientation="horizontal", label="inner")
    outer = dp._dim(0.01, 0, 0.01, -0.02, 0.01, x2=0.015, y2=0, orientation="horizontal", label="outer")
    assert _pairs([{**inner, "view": "f"}, {**outer, "view": "f"}]) == {frozenset(("inner", "outer"))}


def _hdim(label, x1, x2, edge=0.0, side="bottom"):
    return {**dp._dim(x1, edge, (x1 + x2) / 2, edge, x2 - x1, x2=x2, y2=edge, orientation="horizontal",
                      label=label, side=side, edge=edge), "view": "v"}


def _offset(d):
    return abs(d["text_y"] - d["_edge"])


def test_enclosed_span_goes_on_the_inner_tier():
    wide, narrow = _hdim("wide", -0.015, 0.015), _hdim("narrow", -0.003, 0.003)
    out = _by_label(dp.assign_tiers([wide, narrow]))
    assert _offset(out["narrow"]) < _offset(out["wide"])
    assert out["narrow"]["text_y"] < 0 and out["wide"]["text_y"] < out["narrow"]["text_y"]


def test_disjoint_spans_share_a_tier():
    out = _by_label(dp.assign_tiers([_hdim("a", -0.03, -0.01), _hdim("b", 0.01, 0.03)]))
    assert out["a"]["text_y"] == out["b"]["text_y"]


def test_partial_overlap_is_flagged_unavoidable():
    out = _by_label(dp.assign_tiers([_hdim("a", 0.0, 0.02), _hdim("b", 0.01, 0.03)]))
    assert out["a"]["_unavoidable"] and out["b"]["_unavoidable"]


def test_three_nested_spans_use_three_tiers():
    out = dp.assign_tiers([_hdim("c", -0.03, 0.03), _hdim("a", -0.01, 0.01), _hdim("b", -0.02, 0.02)])
    assert sorted(_offset(d) for d in out) == pytest.approx(
        [dp.TIER1, dp.TIER1 + dp.TIER_STEP, dp.TIER1 + 2 * dp.TIER_STEP])


def test_rib_is_inner_to_boss_width():
    plan = _by_label(dp.plan_dimensions(_bracket_facts(), VIEWS))
    assert _offset(plan["rib thickness"]) < _offset(plan["boss width"])


def test_pcd_is_measure_only_and_folded_into_the_callout():
    plan = dp.plan_dimensions(_bracket_facts(), VIEWS)
    assert _by_label(plan)["PCD"]["measure_only"] is True
    assert not any(d.get("measure_only") for d in plan if d["label"] != "PCD")
    assert [d["suffix"] for d in plan if d["label"].startswith("callout")][1].endswith("ON Ø24 PCD")


def test_execute_plan_strips_private_keys_and_reports_layout():
    calls = []
    plan = dp.plan_dimensions(_bracket_facts(), VIEWS)
    result = dp.execute_plan(plan, lambda tool, params: calls.append(params))
    assert all(not k.startswith("_") and k not in ("label", "view") for p in calls for k in p)
    assert result["measured_only"] == ["PCD"] and "PCD" not in result["placed"]
    assert result["crossings"] == [] and result["unavoidable"] == []
    assert [p for p in calls if p.get("measure_only")][0]["expected_value"] == 0.024


def test_leader_router_skips_a_blocked_first_choice():
    # A vertical dimension line through the first (225 deg) candidate forces the router to another angle.
    blocker = {**dp._dim(0.0, 0.0, 0.1, 0.1, 0.01, x2=0.0, y2=0.2, orientation="vertical",
                         label="blocker"), "view": "front"}
    leader = dp._diameter_leader(0.03, (0.12, 0.12), 1.0, 225.0, -dp.LEADER, -dp.LEADER, "bore")
    leader = {**leader, "view": "front"}
    first = {**leader, **leader["_alts"][0]}
    assert dp._crosses(first, blocker)
    routed = _by_label(dp._route_leaders([blocker, leader]))["bore"]
    assert not dp._crosses(routed, blocker)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
