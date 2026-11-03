using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;
using SolidworksExecution.Infrastructure;
using SolidworksExecution.Models;

namespace SolidworksExecution.Services
{
    // SolidWorksService partial: feature-tree, sketch, dimension, pattern, sheet-metal, and equation readers for analyze_model.
    public partial class SolidWorksService
    {
        // -----------------------------------------------------------------------
        // analyze_model "features" helpers — read-only feature-tree extraction.
        // -----------------------------------------------------------------------

        // Tree folders / lights carry no design intent; skip them so the output is just
        // the meaningful feature tree (sketches, solid features, patterns, planes, equations).
        private static readonly HashSet<string> NoiseFeatureTypes = new HashSet<string>
        {
            "HistoryFolder", "SensorFolder", "CommentsFolder", "FavoriteFolder", "DetailCabinet",
            "MaterialFolder", "EnvironmentFolder", "DocsFolder", "BlocksFolder", "SunFolder",
            "AmbientLight", "DirectionLight", "PointLight", "SpotLight",
            // UI / annotation / body-container folders that carry no design intent — they leaked into
            // the feature list (8 of the gear's 25 "features" were these). Equations have their own
            // section (ReadEquations), so the EqnFolder entry is redundant too.
            "SelectionSetFolder", "NotesAreaFtrFolder", "SurfaceBodyFolder", "SolidBodyFolder",
            "EnvFolder", "InkMarkupFolder", "EqnFolder",
            // Sheet-metal cut-list folder ("Sheet<1>" under Solid Bodies) — a UI container, no design
            // intent (it also broke feature_map's baseline: rolling before it no-ops, 4-1 live).
            "CutListFolder"
        };

        private JObject BuildFeatureTreeAnalysis(IModelDoc2 modelDoc)
        {
            var root = new JObject();
            var featArr = new JArray();
            int meaningful = 0;

            // Complete tree walk: FirstFeature/GetNextFeature (top-level, in tree order) PLUS each
            // feature's sub-features (folder contents). GetFeatures(true) skips features grouped into
            // folders, which is how a real Cut-Extrude went missing from the analysis.
            var seen = new HashSet<string>();
            // Dedup driving dimensions across the whole tree by full-name (first feature to report a
            // dim owns it) — kills the repeat where e.g. D1@Sketch1 showed under BOTH the sketch and
            // the extrude that consumes it.
            var seenDims = new HashSet<string>();
            var feat = modelDoc.FirstFeature() as IFeature;
            int guard = 0;
            while (feat != null && guard++ < 5000)
            {
                string type = feat.GetTypeName2() ?? "";
                if (!NoiseFeatureTypes.Contains(type) && seen.Add(feat.Name))
                {
                    featArr.Add(BuildFeatureJson(feat, modelDoc, seenDims, false));
                    meaningful++;
                }

                // One level of sub-features (covers folders that group real features). Sketches consumed
                // by an extrude also appear here — already counted as top-level, so dedup by name skips them;
                // genuinely folder-hidden features are NOT top-level, so they get added.
                var sub = feat.GetFirstSubFeature() as IFeature;
                int subGuard = 0;
                while (sub != null && subGuard++ < 2000)
                {
                    string stype = sub.GetTypeName2() ?? "";
                    if (!NoiseFeatureTypes.Contains(stype) && seen.Add(sub.Name))
                    {
                        var sj = BuildFeatureJson(sub, modelDoc, seenDims, false);
                        sj["in_folder"] = feat.Name;
                        featArr.Add(sj);
                        meaningful++;
                    }
                    sub = sub.GetNextSubFeature() as IFeature;
                }

                feat = feat.GetNextFeature() as IFeature;
            }

            root["feature_count"] = meaningful;
            root["features"] = featArr;
            root["equations"] = ReadEquations(modelDoc);
            return root;
        }

        // seenDims: cross-feature dimension dedup (null = no dedup, e.g. a single-sketch read).
        // fullSketch: true = emit every sketch segment (the 'sketch' detail mode); false = a compact
        // summary (counts + an inline circle for the common single-full-circle profile, no segments).
        private JObject BuildFeatureJson(IFeature feat, IModelDoc2 modelDoc, HashSet<string> seenDims, bool fullSketch)
        {
            var fj = new JObject();
            fj["name"] = feat.Name;
            string type = feat.GetTypeName2() ?? "";
            fj["type"] = type;
            // Only emit suppressed when TRUE — 'suppressed:false' on every feature was pure noise.
            try { if (feat.IsSuppressed()) fj["suppressed"] = true; } catch { }

            var dims = ReadDisplayDimensions(feat, seenDims);
            if (dims.Count > 0) fj["dimensions"] = dims;

            var sketch = feat.GetSpecificFeature2() as ISketch;
            if (sketch != null)
            {
                var sk = ReadSketch(sketch, fullSketch);
                // Skip empty sketches (e.g. the Origin's degenerate sketch emitted "counts":{} noise).
                var counts = sk["counts"] as JObject;
                bool hasContent = (counts != null && counts.Count > 0)
                                  || sk["circle"] != null
                                  || (sk["segments"] is JArray segs && segs.Count > 0);
                if (hasContent)
                {
                    fj["sketch"] = sk;
                    var plane = ReadSketchPlane(sketch);
                    if (plane != null) fj["plane"] = plane;
                }
            }

            if (type == "CirPattern" || type == "LPattern")
                TryReadPattern(feat, type, fj);
            else if (type == "SM3dBend")
                TryReadSketchedBend(feat, modelDoc, fj);
            else if (type == "SMBaseFlange")
                TryReadBaseFlange(feat, fj);
            else if (type == "EdgeFlange")
                TryReadEdgeFlange(feat, modelDoc, fj);
            else if (type == "MirrorPattern")
                TryReadMirror(feat, modelDoc, fj);

            // Extrude/cut end-condition + depth (blind) + reverse flag, so a rebuild knows HOW it was made.
            var ex = ReadExtrude(feat);
            if (ex != null) fj["extrude"] = ex;

            return fj;
        }

        // EdgeFlange — angle/radius/position/gap are plain properties; the ATTACH edge(s) sit
        // behind AccessSelections (released in finally — TryReadMirror discipline) and are emitted
        // with the shared BuildEdgeJson, so each edge's `mid` replays directly as the tool's
        // ex/ey/ez. `profile_sketch` names the flange's ProfileFeature subfeature — the (possibly
        // custom-edited) profile, read fully via analyze_model(sketch, name=...). Ground-truth
        // readback (IR-ADR-014): a rebuild replays these values, it never derives them.
        private void TryReadEdgeFlange(IFeature feat, IModelDoc2 modelDoc, JObject fj)
        {
            try
            {
                var def = feat.GetDefinition() as IEdgeFlangeFeatureData;
                if (def == null) return;
                var ej = new JObject();
                ej["angle"] = R6(def.BendAngle); // radians
                if (!def.UseDefaultBendRadius) ej["radius"] = R6(def.BendRadius);
                string pos;
                switch (def.PositionType)
                {
                    case 1: pos = "material_inside"; break;
                    case 2: pos = "material_outside"; break;
                    case 3: pos = "bend_outside"; break;
                    case 4: pos = "centerline"; break;
                    default: pos = "bend_sharp"; break; // 5
                }
                ej["position"] = pos;
                try { if (def.GapDistance > 0) ej["gap"] = R6(def.GapDistance); } catch { }
                try
                {
                    var sub = feat.GetFirstSubFeature() as IFeature;
                    while (sub != null)
                    {
                        if (sub.GetTypeName2() == "ProfileFeature") { ej["profile_sketch"] = sub.Name; break; }
                        sub = sub.GetNextSubFeature() as IFeature;
                    }
                }
                catch { }
                bool acc = false;
                try { acc = def.AccessSelections(modelDoc, null); } catch { }
                if (acc)
                {
                    try
                    {
                        var edges = def.Edges as object[];
                        if (edges != null)
                        {
                            var arr = new JArray();
                            foreach (var o in edges)
                            {
                                var e = o as IEdge;
                                if (e != null) arr.Add(BuildEdgeJson(e, -1));
                            }
                            if (arr.Count > 0) ej["edges"] = arr;
                        }
                    }
                    finally
                    {
                        try { def.ReleaseSelectionAccess(); } catch { }
                    }
                }
                fj["edge_flange"] = ej;
            }
            catch { }
        }

        // SMBaseFlange — thickness/radius/k-factor + the two direction flags, all plain properties
        // (no AccessSelections). The flags matter beyond documentation: identical blank B-reps can
        // carry OPPOSITE intrinsic sheet orientations (ReverseThickness vs ReverseDirection routes),
        // and downstream bend directions are defined relative to that orientation (the 4-1 Bend1
        // mirror-fold lesson).
        private void TryReadBaseFlange(IFeature feat, JObject fj)
        {
            try
            {
                var def = feat.GetDefinition() as IBaseFlangeFeatureData;
                if (def == null) return;
                var bj = new JObject();
                bj["thickness"] = R6(def.Thickness);
                bj["bend_radius"] = R6(def.BendRadius);
                if (!def.UseGaugeTable) bj["k_factor"] = R6(def.KFactor);
                if (def.ReverseThickness) bj["reverse_thickness"] = true;
                if (def.ReverseDirection) bj["reverse_direction"] = true;
                if (def.SymmetricThickness) bj["symmetric_thickness"] = true;
                fj["base_flange"] = bj;
            }
            catch { }
        }

        // SM3dBend (sketched bend) parameters — angle/radius/position/direction read straight off
        // ISketchedBendFeatureData. `position` maps swFlangePositionTypes_e (4=centerline!) to the
        // canonical string sheet_metal_feature(sketched_bend, bend_position) accepts back, so the
        // recipe value is replayable as-is (the INSERT API's BendPos uses a different 0-based
        // encoding — the tool owns that mapping). `radius` omitted when the bend uses the sheet
        // default. The FIXED-face pick sits behind AccessSelections (temporarily rolls the model
        // back; released in finally — same discipline as TryReadMirror): `fixed_pick` is the stored
        // pick point (reflection-verified: object GetFixedFace(out x, out y, out z)); the returned
        // face's normal + bounding box are emitted too, so a rebuild can pick a model-space point on
        // the SAME side without guessing (the 4-1 Bend1 lesson: a derived pick chose the wrong region).
        private void TryReadSketchedBend(IFeature feat, IModelDoc2 modelDoc, JObject fj)
        {
            try
            {
                var def = feat.GetDefinition() as ISketchedBendFeatureData;
                if (def == null) return;
                var bj = new JObject();
                bj["angle"] = R6(def.BendAngle); // radians
                if (!def.UseDefaultBendRadius) bj["radius"] = R6(def.BendRadius);
                string pos;
                switch (def.PositionType)
                {
                    case 1: pos = "material_inside"; break;
                    case 2: pos = "material_outside"; break;
                    case 3: pos = "bend_outside"; break;
                    case 5: pos = "bend_sharp"; break;
                    default: pos = "centerline"; break; // 4 = swFlangePositionTypeBendCenterLine
                }
                bj["position"] = pos;
                if (def.ReverseDirection) bj["flip"] = true;
                bool acc = false;
                try { acc = def.AccessSelections(modelDoc, null); } catch { }
                if (acc)
                {
                    try
                    {
                        double px, py, pz;
                        object faceObj = def.GetFixedFace(out px, out py, out pz);
                        bj["fixed_pick"] = new JArray { R6(px), R6(py), R6(pz) };
                        var face = faceObj as IFace2;
                        if (face != null)
                        {
                            try
                            {
                                var n = face.Normal as double[];
                                if (n != null) bj["fixed_face_normal"] = new JArray { R6(n[0]), R6(n[1]), R6(n[2]) };
                            }
                            catch { }
                            try
                            {
                                var box = face.GetBox() as double[];
                                if (box != null && box.Length >= 6)
                                    bj["fixed_face_box"] = new JArray { R6(box[0]), R6(box[1]), R6(box[2]), R6(box[3]), R6(box[4]), R6(box[5]) };
                            }
                            catch { }
                        }
                    }
                    finally
                    {
                        try { def.ReleaseSelectionAccess(); } catch { }
                    }
                }
                fj["sketched_bend"] = bj;
            }
            catch { }
        }

        // MirrorPattern — the mirror PLANE name + the mirrored (seed) feature names, so a mirror is
        // reproducible without guessing what it mirrors. Plane/PatternFeatureArray sit behind
        // AccessSelections (temporarily rolls the model back); always released in finally — same
        // non-destructive discipline as feature_map. Best-effort: a failure just omits the block.
        private void TryReadMirror(IFeature feat, IModelDoc2 modelDoc, JObject fj)
        {
            try
            {
                var def = feat.GetDefinition() as IMirrorPatternFeatureData;
                if (def == null) return;
                var mj = new JObject();
                bool acc = false;
                try { acc = def.AccessSelections(modelDoc, null); } catch { }
                try
                {
                    var planeObj = def.Plane;
                    string planeName = (planeObj as IFeature)?.Name;
                    if (planeName != null) mj["plane"] = planeName;
                    else if (planeObj is IFace2) mj["plane_is_face"] = true;
                    var feats = def.PatternFeatureArray as object[];
                    if (feats != null)
                    {
                        var arr = new JArray();
                        foreach (var o in feats)
                        {
                            var f = o as IFeature;
                            if (f != null) arr.Add(f.Name);
                        }
                        if (arr.Count > 0) mj["features"] = arr;
                    }
                }
                finally
                {
                    if (acc) { try { def.ReleaseSelectionAccess(); } catch { } }
                }
                if (mj.Count > 0) fj["mirror"] = mj;
            }
            catch { }
        }

        // Read an extrude/cut feature's end-condition + depth (blind only) + the raw ReverseDirection flag.
        // We deliberately do NOT synthesize an absolute axis ("+Z"/"-Z"): tested on the gear, SW's cut
        // direction is "into material", which a simple sketch-normal × reverse does NOT capture (a top-face
        // pocket reads reverse=false yet cuts -Z), so a computed axis would be WRONG for cuts. The sketch
        // plane's offset (≠0 ⇒ a face) already gives the real direction cue; `reversed` is ground truth.
        // A guaranteed-correct absolute direction is deferred (KNOWN-LIMITATIONS #19).
        private JObject ReadExtrude(IFeature feat)
        {
            try
            {
                var ed = feat.GetDefinition() as IExtrudeFeatureData;
                if (ed == null) return null;
                var ej = new JObject();
                int endc = ed.GetEndCondition(true);
                ej["end"] = EndConditionName(endc);
                if (endc == (int)swEndConditions_e.swEndCondBlind)
                    ej["depth"] = R6(ed.GetDepth(true));
                // `reversed` is normalized against the CANONICAL plane axis, not the raw flag: the
                // real extrude direction = profile-sketch normal ⊕ ReverseDirection, and a sketch's
                // OWN normal depends on how it was authored (a Front-Plane sketch drawn "from behind"
                // carries a -Z normal). Found live on 1-1: Boss-Extrude2 read "not reversed" (raw
                // flag false) yet the wall is built toward -Z — a recipe replay via create_sketch
                // (which always sketches with the canonical +axis normal) then extruded the mirror.
                // After normalization, reversed=true ⇔ the feature's material lies toward the
                // canonical -axis ⇔ replay with extrude_feature(reverse=true). Non-axis-aligned
                // sketch planes (sign unknown) keep the raw flag unchanged.
                bool rev = ed.ReverseDirection;
                if (ProfileSketchNormalSign(feat) < 0) rev = !rev;
                if (rev) ej["reversed"] = true;
                return ej;
            }
            catch { return null; }
        }

        // swEndConditions_e → short label. The enum constants in the comparisons are compile-time folded to
        // ints, so the swconst assembly is never loaded at runtime (same safe pattern as line ~1012/ADR-018).
        private static string EndConditionName(int e)
        {
            if (e == (int)swEndConditions_e.swEndCondBlind) return "blind";
            if (e == (int)swEndConditions_e.swEndCondThroughAll) return "through_all";
            if (e == (int)swEndConditions_e.swEndCondThroughNext) return "through_next";
            if (e == (int)swEndConditions_e.swEndCondUpToVertex) return "up_to_vertex";
            if (e == (int)swEndConditions_e.swEndCondUpToSurface) return "up_to_surface";
            if (e == (int)swEndConditions_e.swEndCondOffsetFromSurface) return "offset_from_surface";
            if (e == (int)swEndConditions_e.swEndCondUpToBody) return "up_to_body";
            if (e == (int)swEndConditions_e.swEndCondMidPlane) return "mid_plane";
            return "end_" + e;
        }

        // Sketch plane/face in MODEL space — origin + normal — so a rebuild knows WHICH plane/face a
        // sketch sits on (e.g. Front Plane z=0 vs a part face at z=0.02). Derived from the sketch
        // transform's inverse (sketch→model): translation = origin, mapped Z-axis = normal. Best-effort.
        // Signed principal-axis direction of an extrude's PROFILE-SKETCH normal: +1 along the
        // canonical +axis (+Z Front / +Y Top / +X Right), -1 opposite, 0 unknown (no sketch parent
        // or a non-axis-aligned plane — in which case the caller must not flip anything). The
        // profile sketch is the extrude feature's ProfileFeature parent.
        private static int ProfileSketchNormalSign(IFeature feat)
        {
            try
            {
                object[] parents = feat.GetParents() as object[];
                if (parents == null) return 0;
                foreach (var p in parents)
                {
                    var pf = p as IFeature;
                    if (pf == null || pf.GetTypeName2() != "ProfileFeature") continue;
                    var sk = pf.GetSpecificFeature2() as ISketch;
                    var s2m = sk?.ModelToSketchTransform?.IInverse();
                    double[] d = s2m?.ArrayData as double[];
                    if (d == null || d.Length < 12) return 0;
                    double nx = d[2], ny = d[5], nz = d[8];
                    double ax = Math.Abs(nx), ay = Math.Abs(ny), az = Math.Abs(nz);
                    double dom = (az >= ax && az >= ay) ? nz : (ay >= ax ? ny : nx);
                    if (Math.Abs(dom) < 0.999) return 0;  // angled plane — don't guess
                    return dom >= 0 ? 1 : -1;
                }
            }
            catch { }
            return 0;
        }

        private JObject ReadSketchPlane(ISketch sketch)
        {
            try
            {
                var m2s = sketch.ModelToSketchTransform;
                if (m2s == null) return null;
                var s2m = m2s.IInverse();
                if (s2m == null) return null;
                double[] d = s2m.ArrayData as double[];
                if (d == null || d.Length < 12) return null;
                double ox = d[9], oy = d[10], oz = d[11];
                double nx = d[2], ny = d[5], nz = d[8];
                var pj = new JObject();
                // The FULL sketch frame (in-plane axes + origin in model space, columns of the
                // sketch→model rotation). {ref, offset} alone is LOSSY: it drops the support's
                // normal SIGN and the in-plane axis orientation — a sketch on a -Y-normal face is
                // the MIRROR of one on a +Y-normal plane at the same height, so replaying its 2D
                // coordinates without the frame builds the pockets in the wrong place (found live
                // on 2-1's Sketch5). The compiler compares this recorded frame against the frame
                // create_sketch MEASURES on the rebuild and transforms coordinates — no assumptions.
                // ArrayData layout is COLUMN-major w.r.t. sketch axes (proven empirically on the
                // 2-1 rebuild: its Right-plane sketch builds toward -Z, and only the d[0..2]
                // reading yields xdir=(0,0,-1) to match): sketch x-axis = d[0..2], y = d[3..5],
                // normal = d[6..8]. (The legacy nx/ny/nz=(d[2],d[5],d[8]) classification above is
                // abs-based and live-proven — deliberately left untouched.)
                pj["frame"] = new JObject
                {
                    ["origin"] = new JArray { R6(ox), R6(oy), R6(oz) },
                    ["xdir"] = new JArray { R6(d[0]), R6(d[1]), R6(d[2]) },
                    ["ydir"] = new JArray { R6(d[3]), R6(d[4]), R6(d[5]) },
                };
                string axisName = PrincipalAxis(nx, ny, nz);
                string refName = DefaultPlaneForAxis(axisName);
                if (refName != null)
                {
                    // Canonical English default-plane name, computed from the NORMAL — never read from the
                    // localized plane name, so it's correct on any SW localization (create_sketch maps
                    // the English name language-independently via SelectPlaneFlexible, ADR-007). offset =
                    // signed perpendicular distance from the global origin: 0 ⇒ the default plane itself;
                    // ≠0 ⇒ a parallel surface at that height (sketch on the face there, or an offset plane).
                    pj["ref"] = refName;
                    // Offset along the CANONICAL +axis (X/Y/Z), NOT the legacy nx/ny/nz pseudo-normal:
                    // that vector is (xdir.z, ydir.z, normal.z) — fine for the ABS-based axis classification
                    // above, but its SIGN flips with the sketch's in-plane orientation. Live-caught on
                    // level-2-2's Sketch2 (+y face, ydir (0,0,-1)): legacy read offset -0.052444, and the
                    // rebuild resolved the face on the OPPOSITE side — a silently mirrored hole that the
                    // topology/volume verified-criteria cannot catch. The consumers (resolve_face_on_plane,
                    // InsertRefPlane lowering) interpret offset as the coordinate along the datum's +normal.
                    double off = R6(axisName == "X" ? ox : axisName == "Y" ? oy : oz);
                    if (Math.Abs(off) > 1e-9) pj["offset"] = off;
                }
                else
                {
                    // Non-axis-aligned (angled face) — fall back to raw origin + normal.
                    pj["origin"] = new JArray { R6(ox), R6(oy), R6(oz) };
                    pj["normal"] = new JArray { R6(nx), R6(ny), R6(nz) };
                }
                return pj;
            }
            catch { return null; }
        }

        // Map an (approximately) axis-aligned unit vector to its principal axis "X"/"Y"/"Z", else null.
        private static string PrincipalAxis(double x, double y, double z)
        {
            double ax = Math.Abs(x), ay = Math.Abs(y), az = Math.Abs(z);
            const double near1 = 0.999, near0 = 0.01;
            if (az >= near1 && ax <= near0 && ay <= near0) return "Z";
            if (ay >= near1 && ax <= near0 && az <= near0) return "Y";
            if (ax >= near1 && ay <= near0 && az <= near0) return "X";
            return null;
        }

        // Canonical English default plane whose normal is the given axis (standard SW part template:
        // Front=Z, Top=Y, Right=X — confirmed live).
        private static string DefaultPlaneForAxis(string axis)
        {
            switch (axis)
            {
                case "Z": return "Front Plane";
                case "Y": return "Top Plane";
                case "X": return "Right Plane";
                default: return null;
            }
        }

        // Generic numeric-parameter recovery: every feature's driving dimensions (extrude depth,
        // fillet radius, hole diameter, revolve angle, ...) without per-type casting. SI units
        // (meters for length, radians for angle).
        private JArray ReadDisplayDimensions(IFeature feat, HashSet<string> seenDims)
        {
            var arr = new JArray();
            object dispObj = feat.GetFirstDisplayDimension();
            int guard = 0;
            while (dispObj != null && guard++ < 500)
            {
                var disp = dispObj as IDisplayDimension;
                if (disp != null)
                {
                    var dim = disp.GetDimension2(0) as IDimension;
                    if (dim != null)
                    {
                        string fullName = dim.FullName;
                        // First feature to report a dimension owns it; skip the repeat that shows up on
                        // a later feature (a sketch's dim re-appears on the extrude that consumes it).
                        if (seenDims == null || seenDims.Add(fullName))
                        {
                            var dj = new JObject();
                            dj["name"] = fullName;
                            double val = dim.Value;
                            try
                            {
                                // 1 = swThisConfiguration. Literal, not the swconst enum: using a swconst
                                // type at runtime forces loading SolidWorks.Interop.swconst (not deployed),
                                // which fails — the codebase relies on enum constants being inlined instead.
                                var sv = dim.GetSystemValue3(1, null) as double[];
                                if (sv != null && sv.Length > 0) val = sv[0];
                            }
                            catch { }
                            dj["value_si"] = R6(val);
                            arr.Add(dj);
                        }
                    }
                }
                dispObj = feat.GetNextDisplayDimension(dispObj);
            }
            return arr;
        }

        // full=false (recipe/summary): counts (zero buckets dropped) + an inline {cx,cy,r} when the
        //   profile is a single full circle (the common bore/boss case — fully defined, lossless, tiny).
        //   No per-segment coordinate dump.
        // full=true (the 'sketch' detail mode): every segment's geometry (rounded), for an exact rebuild.
        private JObject ReadSketch(ISketch sketch, bool full)
        {
            var sj = new JObject();
            var segArr = new JArray();
            int lines = 0, arcs = 0, splines = 0, ellipses = 0, other = 0, construction = 0;
            int realCount = 0; JObject lastReal = null;

            object[] segs = sketch.GetSketchSegments() as object[];
            if (segs != null)
            {
                foreach (var s in segs)
                {
                    var seg = s as ISketchSegment;
                    if (seg == null) continue;
                    var ej = ReadSegment(seg);
                    if (ej == null) continue;
                    if (ej.Value<bool?>("construction") == true) construction++;
                    else { realCount++; lastReal = ej; }
                    switch (ej.Value<string>("kind"))
                    {
                        case "line": lines++; break;
                        case "arc/circle": arcs++; break;
                        case "spline": splines++; break;
                        case "ellipse": ellipses++; break;
                        default: other++; break;
                    }
                    if (full) segArr.Add(ej);
                }
            }

            // Compact counts — omit the zero buckets that bloated the old payload.
            var counts = new JObject();
            if (lines > 0) counts["line"] = lines;
            if (arcs > 0) counts["arc_circle"] = arcs;
            if (splines > 0) counts["spline"] = splines;
            if (ellipses > 0) counts["ellipse"] = ellipses;
            if (other > 0) counts["other"] = other;
            if (construction > 0) counts["construction"] = construction;
            sj["counts"] = counts;

            if (full)
            {
                sj["segments"] = segArr;
            }
            else if (realCount == 1 && lastReal != null
                     && lastReal.Value<string>("kind") == "arc/circle"
                     && lastReal["radius"] != null && lastReal["x1"] == null)
            {
                // Single full circle (ReadSegment drops start/end for a full circle, so x1 is absent):
                // center + radius fully define it — emit inline, no detail-mode round-trip needed.
                sj["circle"] = new JObject
                {
                    ["cx"] = lastReal["cx"],
                    ["cy"] = lastReal["cy"],
                    ["r"] = lastReal["radius"]
                };
            }
            return sj;
        }

        // Read ONE sketch segment's geometry into a JObject. Shared by analyze_model (ReadSketch) and
        // the create-tool in-band echo (AddSketchEntity result_geometry) so analyze's output and a
        // create's reported geometry are byte-for-byte the same shape — the round-trip invariant
        // ("analyze ⊆ create") is enforced by construction, not by two parallel readers.
        private JObject ReadSegment(ISketchSegment seg)
        {
            if (seg == null) return null;
            var ej = new JObject();
            try
            {
                // Construction flag carries design intent (reference scaffolding). Without it,
                // a rebuild would draw construction lines as real profile edges.
                bool isCon = false;
                try { isCon = seg.ConstructionGeometry; } catch { }
                ej["construction"] = isCon;

                // swSketchSegments_e as plain ints (0=line 1=arc/circle 2=ellipse 3=spline)
                // so the runtime never loads SolidWorks.Interop.swconst as a type.
                int segType = seg.GetType();
                switch (segType)
                {
                    case 0:
                        ej["kind"] = "line";
                        var ln = seg as ISketchLine;
                        var a = ln.IGetStartPoint2();
                        var b = ln.IGetEndPoint2();
                        if (a != null) { ej["x1"] = R6(a.X); ej["y1"] = R6(a.Y); }
                        if (b != null) { ej["x2"] = R6(b.X); ej["y2"] = R6(b.Y); }
                        break;
                    case 1:
                        ej["kind"] = "arc/circle";
                        var ar = seg as ISketchArc;
                        var c = ar.IGetCenterPoint2();
                        if (c != null) { ej["cx"] = R6(c.X); ej["cy"] = R6(c.Y); }
                        ej["radius"] = R6(ar.GetRadius());
                        // Start/end points let a rebuild recreate a PARTIAL arc via
                        // add_sketch_entity(arc/arc_center). For a FULL circle start==end, so they're
                        // redundant (center+radius suffice) — omit them; the absence of x1 marks a full circle.
                        var asp = ar.IGetStartPoint2();
                        var aep = ar.IGetEndPoint2();
                        bool fullCircle = asp != null && aep != null
                            && Math.Abs(asp.X - aep.X) < 1e-9 && Math.Abs(asp.Y - aep.Y) < 1e-9;
                        if (!fullCircle)
                        {
                            if (asp != null) { ej["x1"] = R6(asp.X); ej["y1"] = R6(asp.Y); }
                            if (aep != null) { ej["x2"] = R6(aep.X); ej["y2"] = R6(aep.Y); }
                            // Sweep sense (+1 CCW / -1 CW w.r.t. the sketch-plane normal): without it,
                            // center+start+end describe TWO arcs (minor/major), so a deterministic
                            // rebuild via add_sketch_entity(arc_center, direction=dir) needs the sign.
                            try { ej["dir"] = ((ISketchArc)seg).GetRotationDir(); } catch { }
                        }
                        break;
                    case 2:
                        // Ellipse: center + major-axis point + minor-axis point — exactly the inputs
                        // add_sketch_entity(ellipse) consumes (cx,cy / x1,y1 / x2,y2), so it round-trips.
                        ej["kind"] = "ellipse";
                        var el = seg as ISketchEllipse;
                        var ec = el.IGetCenterPoint2();
                        var emaj = el.IGetMajorPoint2();
                        var emin = el.IGetMinorPoint2();
                        if (ec != null) { ej["cx"] = R6(ec.X); ej["cy"] = R6(ec.Y); }
                        if (emaj != null) { ej["x1"] = R6(emaj.X); ej["y1"] = R6(emaj.Y); }
                        if (emin != null) { ej["x2"] = R6(emin.X); ej["y2"] = R6(emin.Y); }
                        break;
                    case 3:
                        // Spline: best-effort through-points (x,y per point). NOTE: recreating a spline
                        // from through-points yields a VISUALLY-equivalent but not bit-identical curve
                        // (SW stores control points + tangency/curvature, not just interpolation points)
                        // — see KNOWN-LIMITATIONS. Enough for a faithful rebuild, not a lossless one.
                        ej["kind"] = "spline";
                        var sp = seg as ISketchSpline;
                        try
                        {
                            // GetPoints2 returns an array of ISketchPoint COM objects (the spline's
                            // through/interpolation points), NOT a flat double[] — read X,Y from each.
                            var rawArr = sp.GetPoints2() as Array;
                            if (rawArr != null && rawArr.Length > 0)
                            {
                                var pts = new JArray();
                                foreach (var o in rawArr)
                                {
                                    var skPt = o as ISketchPoint;
                                    if (skPt == null) continue;
                                    pts.Add(R6(skPt.X));
                                    pts.Add(R6(skPt.Y));
                                }
                                if (pts.Count > 0) ej["points"] = pts;
                            }
                        }
                        catch { }
                        break;
                    default:
                        ej["kind"] = "other";
                        break;
                }
            }
            catch { return null; }
            return ej;
        }

        // Pattern instance count is a feature parameter, NOT a display dimension — read it from
        // the feature definition. The gear case ("36 teeth") depends on this. Best-effort.
        private void TryReadPattern(IFeature feat, string type, JObject fj)
        {
            try
            {
                object def = feat.GetDefinition();
                if (type == "CirPattern")
                {
                    var cd = def as ICircularPatternFeatureData;
                    if (cd != null)
                    {
                        int instances = cd.TotalInstances;
                        double spacingRad = cd.Spacing;
                        bool equalSpacing = cd.EqualSpacing;
                        double spacingDeg = spacingRad * 180.0 / Math.PI;

                        // Disambiguated form — kills the "30@15° vs 24@15°" guesswork (the gear case).
                        // SW stores 'Spacing' as the angle BETWEEN adjacent instances when EqualSpacing
                        // is FALSE (it can exceed 360 and WRAP, overlapping earlier instances), and as
                        // the TOTAL spread when EqualSpacing is TRUE (instances are evenly distributed,
                        // so they never overlap). We report spacing in DEGREES (rounded) + the EFFECTIVE
                        // distinct-instance count + a wrapping flag. The raw radian 'angle_rad' and the
                        // 'inter_angle_deg' (== spacing_deg) duplicates were dropped as redundant.
                        fj["instances"] = instances;
                        fj["equal_spacing"] = equalSpacing;
                        fj["spacing_deg"] = R6(spacingDeg);
                        if (equalSpacing)
                        {
                            // Evenly distributed across 'spacingDeg' total → no overlap by construction.
                            fj["total_angle_deg"] = R6(spacingDeg);
                            fj["distinct_instances"] = instances;
                            fj["wraps"] = false;
                        }
                        else
                        {
                            // Per-instance angle. Instances sit at i*spacing (i=0..N-1); two coincide when
                            // their angles differ by a multiple of 360 → fewer DISTINCT teeth than stored.
                            fj["total_angle_deg"] = R6(spacingDeg * instances);
                            int distinct = CountDistinctAngles(instances, spacingDeg);
                            fj["distinct_instances"] = distinct;
                            fj["wraps"] = distinct < instances;
                        }
                    }
                }
                else
                {
                    var ld = def as ILinearPatternFeatureData;
                    if (ld != null)
                    {
                        fj["d1_instances"] = ld.D1TotalInstances;
                        fj["d1_spacing_si"] = R6(ld.D1Spacing);
                    }
                }
            }
            catch { }
        }

        // Count how many of N circular-pattern instances land on DISTINCT angular positions when each
        // is placed 'interDeg' degrees from the previous. Two instances coincide when their angles are
        // equal mod 360 (within tolerance) — this is what collapses "30 stored @ 15°" to "24 teeth".
        private static int CountDistinctAngles(int instances, double interDeg)
        {
            const double tol = 1e-4; // degrees
            var seen = new List<double>();
            for (int i = 0; i < instances; i++)
            {
                double ang = (i * interDeg) % 360.0;
                if (ang < 0) ang += 360.0;
                bool dup = false;
                foreach (var s in seen)
                {
                    double d = Math.Abs(s - ang);
                    if (d < tol || Math.Abs(d - 360.0) < tol) { dup = true; break; }
                }
                if (!dup) seen.Add(ang);
            }
            return seen.Count;
        }

        private JArray ReadEquations(IModelDoc2 modelDoc)
        {
            var arr = new JArray();
            try
            {
                var em = modelDoc.GetEquationMgr();
                if (em != null)
                {
                    int n = em.GetCount();
                    for (int i = 0; i < n; i++)
                    {
                        var ej = new JObject();
                        try { ej["equation"] = em.get_Equation(i); } catch { }
                        try { ej["value"] = em.get_Value(i); } catch { }
                        try { ej["global"] = em.get_GlobalVariable(i); } catch { }
                        arr.Add(ej);
                    }
                }
            }
            catch { }
            return arr;
        }
    }
}
