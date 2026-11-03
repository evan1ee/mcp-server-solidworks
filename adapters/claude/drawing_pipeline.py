"""Adapter-side DXF/DWG drawing analysis and direct-build orchestration (`analyze_drawing`)."""
import json
from typing import Callable


def summarise_direct(art, summary, graph):
    """The DIRECT_BUILDABLE result: everything needed to JUDGE the build, and nothing else.

    The contour deliberately stays out. A real laser outline is scores of segments; showing it and
    then having the model echo it back into submit_feature_graph would cost the tokens twice over
    and invite a transcription error in between — which is the whole reason the lowering exists."""
    b, th = summary["blank"], summary["thickness"]
    cuts = ", ".join("%gx%g @ (%g,%g)"
                     % (c["bbox"][2] - c["bbox"][0], c["bbox"][3] - c["bbox"][1],
                        round((c["bbox"][0] + c["bbox"][2]) / 2, 2),
                        round((c["bbox"][1] + c["bbox"][3]) / 2, 2))
                     for c in b["cutouts"]) or "none"
    lines = [
        "DIRECT_BUILDABLE | sheet-metal flat pattern | %s | sha256 %s"
        % (art["source"]["file"], art["source"]["sha256"][:12]),
        "  blank      %g x %g mm | outer loop %d segments | cutouts: %s"
        % (b["size"][0], b["size"][1], b["outer_segments"], cuts),
        "  thickness  %g mm  (%s)   bend radius %g mm   K %g"
        % (th["value_mm"], th["source"], summary["bends"][0]["radius_mm"], summary["k_factor"]),
        "  bends      %d, each corroborated by its line's edge class:" % len(summary["bends"]),
    ]
    for r in summary["bends"]:
        x1, y1, x2, y2 = r["line"]
        lines.append("               %-4s %g deg R%g  line %s  midpoint (%g, %g)%s"
                     % (r["dir"], r["angle_deg"], r["radius_mm"], r["class"],
                        round((x1 + x2) / 2, 4), round((y1 + y2) / 2, 4),
                        "  [WARNING: in a closed loop]" if r["in_loop"] else ""))
    for r in summary["skipped_bends"]:
        lines.append("  SKIPPED    %s note at %s — %s (candidates %s). It will NOT be built."
                     % (r["dir"], r["at"], r["reason"], r["candidates"]))
    lines += [
        "  expected   blank area %g mm^2 -> V = %r m^3  (bending does not change it at K=0.5)"
        % (b["area_mm2"], summary["expected"]["volume_m3"]),
        "  graph      %d IR nodes: sketch + sheet_metal, then sketch + sketched_bend per direction"
        % len(graph["nodes"]),
        "  NEXT       mode='build' builds it | mode='ir' shows the graph first.",
        "             AFTER building, verify: analyze_model('mass_properties')",
        "             against the expected volume above, and read one bend face back — a mirrored",
        "             fold is invisible to volume, area and topology alike (recipe R14).",
    ]
    if summary["skipped_bends"]:
        lines.append("             A skipped bend is invisible to ALL of those (the blank volume is "
                     "unchanged): add it by hand or declare it as a gap (R15).")
    return "\n".join(lines)


def advisories(art) -> str:
    """A short plain-text block naming the conditions this read actually raised. '' when none."""
    out = []
    views = art.get("views") or []

    scaled = [v["vid"] for v in views if v.get("scale_factor") is not None]
    if scaled:
        out.append("view scale_factor @ %s — this view is drawn at a DIFFERENT ratio from the "
                   "sheet; say which factor produced a number before you quote it."
                   % ", ".join(scaled))

    not_read = (art.get("sheet") or {}).get("not_read") or {}
    if not_read:
        out.append("not_read @ sheet (%s) — the reader saw these and did not emit them; a named "
                   "block you cannot account for is evidence that never reached you."
                   % ", ".join(sorted(not_read)))

    ang, lin = [], []
    for k, d in enumerate(art.get("dimensions") or []):
        if d.get("printed_mismatch"):
            (ang if d.get("kind") == "angular" else lin).append("dim[%d]" % k)
    if ang:
        out.append("printed_mismatch/ANGULAR @ %s — the value is a JUDGEMENT, not a reading; "
                   "corroborate it against the geometry or an ellipse ratio before building."
                   % ", ".join(ang))
    if lin:
        out.append("printed_mismatch @ %s — the scale, the arc match or the per-view factor is "
                   "wrong. Reading the flag is free; ignoring it is not." % ", ".join(lin))

    tier_b = ["%s/%s" % (v["vid"], lp.get("id"))
              for v in views for lp in (v.get("loops") or []) if lp.get("tier") == "B"]
    if tier_b:
        out.append("tier:B @ %s — this boundary was WALKED, not chained; check its area against a "
                   "stated length or width before you build on it." % ", ".join(tier_b))

    unpaired = [b for b in (art.get("bend_notes") or []) if b.get("unpaired")]
    if unpaired:
        out.append("unpaired bend x%d — the reader refused to guess, so those bends are NOT built. "
                   "Resolve them from the drawing or declare a gap; never split the difference."
                   % len(unpaired))
    in_loop = [b for b in (art.get("bend_notes") or [])
               if (b.get("bend_line") or {}).get("in_loop")]
    if in_loop:
        out.append("bend_line in_loop x%d — the match landed on OUTLINE geometry, and a bend line "
                   "belongs to no closed contour. Say so." % len(in_loop))

    vg = art.get("view_graph")
    if isinstance(vg, dict) and vg.get("solved") is False:
        out.append("view_graph.solved:false (%s) — the axes did not join; that joining is yours."
                   % vg.get("reason", "reason in the field"))

    if not out:
        return ""
    return "\nADVISORIES (%d) — only what actually fired in THIS read:\n  %s" % (
        len(out), "\n  ".join(out))


def wire_json(obj) -> str:
    """The drawing payload, serialized as tightly as JSON allows."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def format_direct_build_result(art, summary, run_graph_fn: Callable, graph) -> str:
    """Execute a lowered direct-buildable Feature Graph and format the verification tail."""
    text, ok, sv = run_graph_fn(graph, fresh_document=True)
    if not ok:
        return text
    tail = ["", "built from %s (sha256 %s) | state_version=%s"
            % (art["source"]["file"], art["source"]["sha256"][:12], sv),
            "VERIFY: analyze_model('mass_properties') should read V = %r m^3 "
            "(blank %g mm^2 x %g mm)." % (summary["expected"]["volume_m3"], summary["blank"]["area_mm2"],
                                          summary["thickness"]["value_mm"]),
            "        then read one bend face back — volume, area and topology cannot see a "
            "mirrored fold (recipe R14)."]
    for r in summary["skipped_bends"]:
        tail.append("NOT BUILT: %s bend at %s (%s). The blank volume is unchanged by it, so no "
                    "measurement will catch it — add it or declare it (R15)."
                    % (r["dir"], r["at"], r["reason"]))
    return text + "\n".join(tail)
