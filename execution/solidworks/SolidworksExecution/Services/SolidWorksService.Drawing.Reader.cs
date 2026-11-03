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
    // SolidWorksService partial: structural SLDDRW drawing reader (AnalyzeDrawing, ReadViewDimensions, geometry/relation/centermark/section readers).
    public partial class SolidWorksService
    {
        // analyze_slddrw_test (was analyze_drawing until 2026-07-27) — read the ACTIVE NATIVE .SLDDRW
        // structurally. TEST/REFERENCE ONLY: the shipping reverse path is moving to a DXF/DWG reader;
        // the fields below (measures/section/normal_axis/relations/extent) are COM constructs a DXF lacks.:
        // every view's {name, type, scale, pos} + its dimensions {name, value_si}. Read-only, does NOT bump
        // state_version (same pattern as AnalyzeModel/VerifyState). Feeds the objective Stage-1 check (do the
        // drawing's dims match the model?) and the Stage-2 re-modeling input. Packs one JSON string into
        // Features (the field the adapter's response_mapper surfaces). NOTE: GetFirstView() returns the SHEET
        // as the first view; it is reported too (type int distinguishes it) — interpret view[0] as the sheet.
        public ExecutionResponse AnalyzeDrawing(ToolRequest request)
        {
            return ExecuteTool(request, pAd =>
            {
                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var drawingDoc = modelDoc as IDrawingDoc;
                if (drawingDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NOT_A_DRAWING", "Active document is not a drawing. Use analyze_model for part documents.");

                bool includeGeometry = pAd?.Value<bool?>("include_geometry") ?? false;
                // Batch-4: deterministic relations + cross-view station join keys. Relations are computed
                // FROM the geometry read, so the flag forces geometry on. All Batch-4 blocks are ADDITIVE —
                // the raw geometry arrays are never replaced or altered by them.
                bool includeRelations = pAd?.Value<bool?>("include_relations") ?? false;
                if (includeRelations) includeGeometry = true;

                // A freshly-opened drawing has not computed its view DISPLAY geometry yet, so GetPolylines7
                // returns empty (per-view UpdateViewDisplayGeometry alone is insufficient on a cold doc that
                // has never been displayed this session). Force a rebuild AND a redraw/zoom so the views
                // actually render and the projected edges exist before we read them.
                if (includeGeometry)
                {
                    try { modelDoc.ForceRebuild3(false); } catch { }
                    try { modelDoc.EditRebuild3(); } catch { }
                    try { modelDoc.GraphicsRedraw2(); } catch { }
                    try { modelDoc.ViewZoomtofit2(); } catch { }
                }

                var root = new JObject();
                var viewsArr = new JArray();
                int totalDims = 0;
                var stationViews = new List<StationViewData>();   // Batch-4: per-view data for the station join
                var relSkippedVids = new JArray();                // views excluded from relations (loud, C5)

                object viewObj = drawingDoc.GetFirstView(); // first view = the sheet container
                int guard = 0;
                while (viewObj != null && guard++ < 2000)
                {
                    var view = viewObj as IView;
                    if (view != null)
                    {
                        var vj = new JObject();
                        string vid = "v" + viewsArr.Count;   // views[] index — the contract's view alias
                        vj["name"] = view.Name;
                        int vType = -1;
                        try { vType = view.Type; vj["type"] = vType; } catch { }   // swDrawingViewTypes_e (raw int)
                        try { vj["scale"] = R6(view.ScaleDecimal); } catch { }
                        try
                        {
                            var pos = view.Position as double[];
                            if (pos != null && pos.Length >= 2)
                                vj["pos"] = new JArray { R6(pos[0]), R6(pos[1]) };
                        }
                        catch { }
                        if (includeRelations) vj["vid"] = vid;

                        // Geometry first (Batch-4 reordering): the relation/measure blocks and the dimension
                        // attachment both consume the SAME extraction — one read, no second GetPolylines7 pass.
                        ViewGeomData geomData = null;
                        double[] frO = null, frX = null, frY = null;
                        if (includeGeometry)
                        {
                            geomData = ExtractViewGeometry(view);
                            vj["geometry"] = BuildGeometryJson(geomData, view);
                        }
                        bool relationsHere = includeRelations && vType != 1
                            && geomData != null && geomData.Err == null;
                        if (relationsHere)
                        {
                            if (ViewModelFrameRaw(view, out frO, out frX, out frY))
                            {
                                var rel = ComputeViewRelations(geomData);
                                if (rel != null) vj["relations"] = rel;
                                var cm = ReadCenterMarks(view, frO, frX, frY, geomData);
                                if (cm != null && cm.Count > 0) vj["center_marks"] = cm;
                                var cl = ReadCenterlines(view, geomData);
                                if (cl != null && cl.Count > 0) vj["centerlines"] = cl;
                                stationViews.Add(new StationViewData { Vid = vid, O = frO, X = frX, Y = frY, Geom = geomData });
                            }
                            else
                            {
                                // No usable view→model transform ⇒ this view cannot join relations/stations.
                                // Recorded loudly instead of silently thinning the output (C5).
                                relSkippedVids.Add(vid);
                                relationsHere = false;
                            }
                        }
                        var dimsArr = ReadViewDimensions(view, relationsHere ? geomData : null, frO, frX, frY);
                        totalDims += dimsArr.Count;
                        vj["dimensions"] = dimsArr;
                        // Batch-1 Task-2: section views also carry cutting-plane metadata (parent view +
                        // model-space normal/frame) so section-coordinate → 3D mapping is arithmetic.
                        var sect = ReadSectionMetadata(view);
                        if (sect != null) vj["section"] = sect;
                        viewsArr.Add(vj);
                    }
                    viewObj = (view != null) ? view.GetNextView() : null;
                }

                root["view_count"] = viewsArr.Count;
                root["dimension_count"] = totalDims;
                root["views"] = viewsArr;
                if (includeRelations)
                {
                    // stations table DROPPED (2026-07-19 trim) — the benchmark data-consumption report
                    // showed it unconsumed (the model cross-references views by dimension, not this table).
                    if (relSkippedVids.Count > 0) root["relations_skipped_views"] = relSkippedVids;
                }

                // Read-only — does NOT bump state_version (mirrors AnalyzeModel).
                return BuildCompletedResponse(
                    request,
                    modelDoc,
                    "DRAWING",
                    incrementStateVersion: false,
                    registerCompleted: false,
                    features: new List<string> { root.ToString(Newtonsoft.Json.Formatting.None) });
            });
        }

        // Per-view display dimensions, reusing the same IDisplayDimension→IDimension reader shape as the
        // feature-side ReadDisplayDimensions (FullName + SI value). View-level iteration uses
        // GetFirstDisplayDimension5()/GetNext5() (the IView/IDisplayDimension API). Best-effort + guarded.
        //
        // Batch-1 Task-1 (DIMENSION→GEOMETRY MAPPING): each dim also reports WHAT it measures, as DATA
        // (never inference — the "what is this 17mm the dimension of?" question). Added fields:
        //   diametric  — true when the display shows it as a Ø (IDisplayDimension.Diametric) — kills "is 17 the
        //                diameter?" (this IS reliable; IDimension.GetType() is NOT — it comes back Unknown(0)
        //                for most model-inserted drawing dims and mis-labels a chamfer angle, so it is not used).
        //   attached   — the KIND(s) of geometry the dimension is attached to (swSelectType_e: edge/vertex/
        //                sketch_seg/...), read from the underlying IAnnotation's attached-entity types.
        //   anchors    — the dimension's REFERENCE POINTS in MODEL space (meters) for a model dimension — the
        //                concrete 3D geometry it snaps to (live-proven: the Ø17 bore's anchor is [0,0,0.0651],
        //                on the axis at the top plane), so a reader maps the value to a 3D location arithmetically.
        // All reflection-verified against SolidWorks.Interop.sldworks (IAnnotation.GetAttachedEntityTypes,
        // IDimension.GetReferencePointsCount/IGetReferencePoints→IMathPoint.ArrayData). swconst ints inlined
        // (ADR-018 — never load swconst as a type at runtime).
        //
        // Batch-4 (DIMENSION → PRIMITIVE ATTACHMENT): when the view's extracted geometry + model frame are
        // handed in (include_relations mode), each dimension additionally names the primitive id(s) it
        // measures — `measures` — resolved ARITHMETICALLY from its anchors: anchor (model 3D) → view 2D via
        // the frame (u=(p−o)·xdir, v=(p−o)·ydir), then point-vs-primitive distance ≤ RelTol. Sources:
        // anchor_at_center (anchor sits on a circle/arc CENTER — the live-proven Ø-dim shape) or
        // anchor_on_primitive (anchor lies on the primitive's geometry). A dimension whose anchors resolve
        // to NO primitive is flagged `unattached:true` — loud, never dropped, never guessed (C5).
        private JArray ReadViewDimensions(IView view, ViewGeomData geom = null,
            double[] frO = null, double[] frX = null, double[] frY = null)
        {
            var arr = new JArray();
            bool attach = geom != null && frO != null && frX != null && frY != null;
            object dispObj = null;
            try { dispObj = view.GetFirstDisplayDimension5(); } catch { return arr; }
            int guard = 0;
            while (dispObj != null && guard++ < 1000)
            {
                var disp = dispObj as IDisplayDimension;
                if (disp != null)
                {
                    var dim = disp.GetDimension2(0) as IDimension;
                    if (dim != null)
                    {
                        var dj = new JObject();
                        dj["name"] = dim.FullName;
                        // value_si must be SI (meters/radians) for EVERY dim. GetSystemValue3(swThisConfiguration)
                        // is correct for MODEL dims shown in the drawing, but for reference dims CREATED in the
                        // drawing (RD*@Drawing View…) it resolves no configuration and the value falls through in
                        // DOCUMENT units (live on level-2-2: RD4 read 125.864691 "meters" = the 125.86 mm block
                        // width). SystemValue is the configuration-independent SI accessor (the same one
                        // modify_dimension writes through) — prefer it here; drawings have no configurations, so
                        // nothing is lost. GetSystemValue3 stays as the fallback, raw Value as the last resort.
                        double val = dim.Value;
                        bool gotSi = false;
                        try { val = dim.SystemValue; gotSi = true; } catch { }
                        if (!gotSi)
                        {
                            try
                            {
                                var sv = dim.GetSystemValue3(1, null) as double[]; // 1 = swThisConfiguration (inlined)
                                if (sv != null && sv.Length > 0) val = sv[0];
                            }
                            catch { }
                        }
                        dj["value_si"] = R6(val);

                        // --- Ø flag (the reliable dimension-kind signal) ---
                        try { if (disp.Diametric) dj["diametric"] = true; } catch { }

                        // --- attached-entity KIND(s): what geometry this dim hangs off ---
                        try
                        {
                            var ann = disp.GetAnnotation() as IAnnotation;
                            if (ann != null)
                            {
                                object rawTypes = null;
                                try { rawTypes = ann.GetAttachedEntityTypes(); } catch { }
                                var kinds = new JArray();
                                var seenKinds = new HashSet<string>();
                                foreach (int et in ToIntArray(rawTypes))
                                {
                                    string kn = AttachEntityKindName(et);
                                    if (seenKinds.Add(kn)) kinds.Add(kn);
                                }
                                if (kinds.Count > 0) dj["attached"] = kinds;
                            }
                        }
                        catch { }

                        // --- ANCHOR points (sheet space, meters): the concrete geometry the dim references ---
                        JArray anchorsArr = null;
                        try
                        {
                            anchorsArr = ReadDimensionAnchors(dim);
                            if (anchorsArr != null && anchorsArr.Count > 0) dj["anchors"] = anchorsArr;
                        }
                        catch { }

                        // --- Batch-4: name the primitive(s) this dimension measures (or flag it loudly) ---
                        if (attach)
                        {
                            try { AttachDimensionToPrimitives(dj, anchorsArr, geom, frO, frX, frY); }
                            catch { }
                        }

                        arr.Add(dj);
                    }
                }
                object next = null;
                try { next = disp != null ? disp.GetNext5() : null; } catch { next = null; }
                dispObj = next;
            }
            return arr;
        }

        // The dimension's reference points (the points it snaps to) in MODEL space (meters) for a model
        // dimension. IDimension exposes GetReferencePointsCount() + IGetReferencePoints(i)→IMathPoint
        // (ArrayData = [x,y,z]); the aggregate get_ReferencePoints() returns an array of the same MathPoints.
        // We prefer the indexed accessor and fall back to the aggregate. Rounded to 6 decimals (1µm). Identical
        // points are de-duped: model-inserted drawing dims frequently report both reference points at the same
        // spot (the attach location), so a lone point carries the same information without the duplicate noise.
        private JArray ReadDimensionAnchors(IDimension dim)
        {
            var raw = new JArray();
            int n = 0;
            try { n = dim.GetReferencePointsCount(); } catch { n = 0; }
            if (n > 0)
            {
                for (int i = 0; i < n; i++)
                {
                    IMathPoint mp = null;
                    try { mp = dim.IGetReferencePoints(i) as IMathPoint; } catch { mp = null; }
                    var pj = MathPointJson(mp);
                    if (pj != null) raw.Add(pj);
                }
            }
            if (raw.Count == 0)
            {
                // Fallback: the aggregate accessor (array of MathPoint COM objects).
                try
                {
                    var agg = dim.ReferencePoints as object[];
                    if (agg != null)
                        foreach (var o in agg)
                        {
                            var pj = MathPointJson(o as IMathPoint);
                            if (pj != null) raw.Add(pj);
                        }
                }
                catch { }
            }
            // De-dup identical consecutive points.
            var pts = new JArray();
            foreach (var t in raw)
            {
                bool dup = false;
                foreach (var u in pts)
                    if (JToken.DeepEquals(t, u)) { dup = true; break; }
                if (!dup) pts.Add(t);
            }
            return pts;
        }

        private static JArray MathPointJson(IMathPoint mp)
        {
            if (mp == null) return null;
            try
            {
                var d = mp.ArrayData as double[];
                if (d != null && d.Length >= 3)
                    return new JArray { R6(d[0]), R6(d[1]), R6(d[2]) };
            }
            catch { }
            return null;
        }

        // Coerce a COM VARIANT array (int[] or object[] of boxed ints) into an int[]. GetAttachedEntityTypes
        // and section-line getters come back as one of these shapes depending on marshaling.
        private static int[] ToIntArray(object raw)
        {
            if (raw is int[] ia) return ia;
            if (raw is object[] oa)
            {
                var outp = new int[oa.Length];
                for (int i = 0; i < oa.Length; i++)
                {
                    try { outp[i] = Convert.ToInt32(oa[i]); } catch { outp[i] = 0; }
                }
                return outp;
            }
            if (raw is double[] da)
            {
                var outp = new int[da.Length];
                for (int i = 0; i < da.Length; i++) outp[i] = (int)Math.Round(da[i]);
                return outp;
            }
            return System.Array.Empty<int>();
        }

        // Coerce a COM VARIANT array (double[] or object[] of boxed numbers) into a double[].
        private static double[] ToDoubleArray(object raw)
        {
            if (raw is double[] da) return da;
            if (raw is object[] oa)
            {
                var outp = new double[oa.Length];
                for (int i = 0; i < oa.Length; i++)
                {
                    try { outp[i] = Convert.ToDouble(oa[i]); } catch { return null; }
                }
                return outp;
            }
            return null;
        }

        // swSelectType_e (subset that a drawing dimension attaches to) → label. Inlined ints (ADR-018).
        private static string AttachEntityKindName(int t)
        {
            switch (t)
            {
                case 1: return "edge";
                case 2: return "face";
                case 3: return "vertex";
                case 9: return "sketch";
                case 10: return "sketch_seg";
                case 11: return "sketch_point";
                default: return "sel_" + t;
            }
        }

        // Batch-1 Task-2 — SECTION VIEW cutting-plane metadata. For a section view (Type==2 =
        // swDrawingSectionView, or one that owns an IDrSection) report, as DATA:
        //   parent_view  — the view the section was cut from (IView.GetBaseView, fallback IDrSection.GetView)
        //   cut_normal   — the cutting-plane NORMAL in MODEL space (= the section view's viewing direction):
        //                  the section view's view→model transform maps view-Z to this. Read exactly as
        //                  ReadSketchPlane reads a sketch normal (inverse transform, columns d[2],d[5],d[8]),
        //                  so the sign convention matches the part-side readers. Normalized.
        //   axis         — cut_normal snapped to a principal axis ("X"/"Y"/"Z") when axis-aligned — the direct
        //                  answer to "which axis is A-A perpendicular to?" (else null → read cut_normal).
        //   frame        — the section view's model-space frame {origin, xdir, ydir} (view→model), so a caller
        //                  maps a 2D section-view coordinate to 3D: p_model = origin + u*xdir + v*ydir.
        //   label        — the section label (A, B, ...) when available from the IDrSection.
        // All reflection-verified: IView.Type / GetBaseView / ModelToViewTransform / GetSection→IDrSection.
        // Best-effort — any failure just omits the affected field; a non-section view returns null.
        private JObject ReadSectionMetadata(IView view)
        {
            IDrSection section = null;
            try { section = view.GetSection() as IDrSection; } catch { section = null; }
            int vtype = -1;
            try { vtype = view.Type; } catch { }
            // Gate: swDrawingSectionView (2) OR an owning IDrSection is present.
            if (vtype != 2 && section == null) return null;

            var sj = new JObject();

            // Parent view — the view the cut was taken in.
            try
            {
                var baseView = view.GetBaseView() as IView;
                if (baseView != null) sj["parent_view"] = baseView.Name;
            }
            catch { }
            if (sj["parent_view"] == null && section != null)
            {
                try { var pv = section.GetView() as IView; if (pv != null) sj["parent_view"] = pv.Name; }
                catch { }
            }

            // Section label (A / B / ...).
            if (section != null)
            {
                try { var lbl = section.GetLabel(); if (!string.IsNullOrEmpty(lbl)) sj["label"] = lbl; }
                catch { }
                try { if (section.IsAligned()) sj["aligned"] = true; } catch { }
            }

            // Cutting-plane normal + model-space frame, from the section view's view→model transform.
            try
            {
                var m2v = view.ModelToViewTransform;   // model → view
                var v2m = m2v != null ? m2v.IInverse() : null;   // view → model
                double[] d = v2m != null ? v2m.ArrayData as double[] : null;
                if (d != null && d.Length >= 12)
                {
                    // Same column reading as ReadSketchPlane: normal = mapped view-Z (d[2],d[5],d[8]);
                    // in-plane axes xdir = d[0..2], ydir = d[3..5].
                    double nx = d[2], ny = d[5], nz = d[8];
                    double nlen = Math.Sqrt(nx * nx + ny * ny + nz * nz);
                    if (nlen > 1e-9) { nx /= nlen; ny /= nlen; nz /= nlen; }
                    sj["cut_normal"] = new JArray { R6(nx), R6(ny), R6(nz) };
                    string ax = PrincipalAxis(nx, ny, nz);
                    if (ax != null) sj["axis"] = ax;
                }
                // Frame origin: the raw v2m translation (view 0,0,0 → model) is NOT the reference the
                // view's projected geometry uses (live on level-2-2 Section C-C it landed half a meter
                // off the part), so the frame is computed by ViewModelFrame — origin = the model-space
                // point of the view geometry's (0,0) — and shared with the geometry read.
                var fr = ViewModelFrame(view);
                if (fr != null) sj["frame"] = fr;
            }
            catch { }

            return sj.Count > 0 ? sj : null;
        }

        // The model-space frame of a drawing view's PROJECTED 2D geometry: p_model = origin + u*xdir +
        // v*ydir for a (u,v) from ReadViewGeometry (model-scale meters, centered on the view anchor).
        // xdir/ydir = the view→model rotation columns (same column convention as ReadSketchPlane, proven
        // on the section axis classification). origin = the view's sheet-space anchor (IView.Position)
        // mapped through the view→model transform via IMathUtility (MultiplyTransform applies the
        // transform's scale component, so the 1:viewscale sheet→model scaling is handled) — the raw v2m
        // translation is the model point of SHEET (0,0), not of the view anchor, which is why it read
        // half a meter off the part. Verified live on level-2-2 (block corners / bore heights map exactly).
        private JObject ViewModelFrame(IView view)
        {
            double[] o, x, y;
            if (!ViewModelFrameRaw(view, out o, out x, out y)) return null;
            var fj = new JObject
            {
                ["origin"] = new JArray { R6(o[0]), R6(o[1]), R6(o[2]) },
                ["xdir"] = new JArray { R6(x[0]), R6(x[1]), R6(x[2]) },
                ["ydir"] = new JArray { R6(y[0]), R6(y[1]), R6(y[2]) },
            };
            // Batch-4 follow-up (ADR-054): the view's NORMAL snapped to a signed principal axis
            // ("Y" / "-Z" ...) — the deterministic restatement of "which way does this view look".
            // Drafting semantics the recipe builds on it: a CIRCLE in this view is the cross-section
            // of a feature whose axis runs along this normal, so circles in different-normal views
            // are DIFFERENT features unless a section proves otherwise (the 2026-07-17 benchmark
            // model conflated a top-view Y-axis stepped hole with the front-view Z-axis loft chain).
            // Computed on the UNROUNDED frame vectors; omitted (loudly absent) when not axis-aligned.
            double nx = x[1] * y[2] - x[2] * y[1];
            double ny = x[2] * y[0] - x[0] * y[2];
            double nz = x[0] * y[1] - x[1] * y[0];
            string ax = PrincipalAxis(nx, ny, nz);
            if (ax != null)
            {
                double comp = ax == "X" ? nx : (ax == "Y" ? ny : nz);
                fj["normal_axis"] = (comp < 0 ? "-" : "") + ax;
            }
            return fj;
        }

        // Raw (unrounded) frame vectors — the single transform-reading path ViewModelFrame wraps. The
        // Batch-4 relation/station/attachment arithmetic runs on these raw doubles and rounds only at
        // JSON-emit time, so residuals report the true measured deviation.
        private bool ViewModelFrameRaw(IView view, out double[] origin, out double[] xdir, out double[] ydir)
        {
            origin = null; xdir = null; ydir = null;
            try
            {
                var m2v = view.ModelToViewTransform;
                var v2m = m2v != null ? m2v.IInverse() : null;
                double[] d = v2m != null ? v2m.ArrayData as double[] : null;
                if (d == null || d.Length < 12) return false;
                var pos = view.Position as double[];
                if (pos == null || pos.Length < 2) return false;
                var mu = _solidWorks.GetMathUtility() as IMathUtility;
                var pt = mu != null ? mu.CreatePoint(new double[] { pos[0], pos[1], 0.0 }) as IMathPoint : null;
                var mapped = pt != null ? pt.MultiplyTransform(v2m) as IMathPoint : null;
                double[] o = mapped != null ? mapped.ArrayData as double[] : null;
                if (o == null || o.Length < 3) return false;
                origin = new double[] { o[0], o[1], o[2] };
                xdir = new double[] { d[0], d[1], d[2] };
                ydir = new double[] { d[3], d[4], d[5] };
                return true;
            }
            catch { return false; }
        }

        // Read a drawing view's PROJECTED 2D geometry as clean primitives — the "clean shape" needed to
        // reverse-engineer a part from its drawing WITHOUT depending on a cluttered raster (dimension lines
        // overlapping the geometry). Source: IView.GetPolylines7 (the only view-geometry getter that returns
        // data here — GetLines3/GetArcs4/GetSplines3 come back empty, KNOWN-LIMITATIONS #21).
        // UpdateViewDisplayGeometry() is called first to force the display geometry to compute.
        //
        // GetPolylines7 returns a flat double[] of records; each record = [header][N][N*(x,y,z)], coords in
        // MODEL-scale meters CENTERED on the view centroid (z==0, planar). Decoded empirically (ADR-035):
        //  - straight edges: an 8-double header [0,0,0,0, nx,ny,nz, 0] then N=2 then 2 points -> a LINE.
        //    Extracted by a desync-proof SIGNATURE SCAN (cylindrical-wall silhouettes come out here — this is
        //    where a revolve profile's UP/DOWN steps live, the info a dimension value can't carry).
        //  - curved edges (fillets/chamfers, and the circular boundaries of annular faces): tessellated
        //    multi-point runs, captured best-effort as {start, mid, end} via a tolerant walk.
        // Raw primitive holders for one view's extracted geometry (Batch-4). Values are UNROUNDED —
        // relations/stations/attachment compute on them and round only at emit time. Ids are positional
        // and documented in the contract: l<i> = lines[i], a<i> = curves[i], c<i> = circles[i].
        private class LineRec { public double X1, Y1, X2, Y2; }
        private class CurveRec
        {
            public int N;
            public double SX, SY, MX, MY, EX, EY;   // start / mid / end tessellation points
            public bool HasCenter;
            public double CX, CY, R;
        }
        private class CircleRec { public double CX, CY, R; }
        private class ViewGeomData
        {
            public readonly List<LineRec> Lines = new List<LineRec>();
            public readonly List<CurveRec> Curves = new List<CurveRec>();
            public readonly List<CircleRec> Circles = new List<CircleRec>();
            public string Err;
        }

        private ViewGeomData ExtractViewGeometry(IView view)
        {
            var g = new ViewGeomData();
            try { view.UpdateViewDisplayGeometry(); } catch { }
            double[] arr;
            try { object polys; view.GetPolylines7((short)0, out polys); arr = polys as double[]; }
            catch (Exception ex) { g.Err = ex.Message; return g; }
            int nlen = (arr == null) ? 0 : arr.Length;

            // Hidden-edge filtering (ORTHO views only). GetPolylines7 returns visible AND obscured
            // (hidden) edges; each record's 6-double "display block" immediately before its point-count
            // distinguishes them — VISIBLE carries [.,.,-1,1,-1,0], HIDDEN (obscured) carries
            // [2,1,1,0,-1,0]. Pinned empirically on the 'test' cube (Ø25 blind hole from the top: its
            // hidden walls + bottom circle) and confirmed 100% clean across level-2-2's lines+curves via
            // an A/B against a hand-cleaned copy (2026-07-19): every record classified, zero ambiguous.
            // Ortho views must not leak hidden edges — a hidden bore's circle otherwise reads as a phantom
            // feature section and the model mis-reconstructs (run #4/#5 "hidden-line confusion"). Section
            // views (Type==2 / owning an IDrSection) are EXEMPT: the cut face is visible geometry and, as
            // the A/B showed, carries no hidden records anyway. Hidden straight LINES were already dropped
            // (the line-scan below requires the visible display block d0==d1==0); this adds the same
            // discipline to the curve walk, which previously kept obscured arcs/circles.
            bool _isSection = false;
            try { _isSection = (view.Type == 2); } catch { }
            if (!_isSection) { try { _isSection = (view.GetSection() != null); } catch { } }
            bool filterHidden = !_isSection;

            // Line-record signature: arr[i..i+3]==0, arr[i+7]==0, arr[i+8]==2.0, then two points. Each point
            // is (x, y, depth); the 3rd coord is the view DEPTH — CONSTANT per record (0 for some views,
            // non-zero for others), not necessarily 0. Require the two points to share the same depth.
            for (int i = 0; i + 15 <= nlen;)
            {
                if (Z(arr[i]) && Z(arr[i + 1]) && Z(arr[i + 2]) && Z(arr[i + 3]) && Z(arr[i + 7])
                    && arr[i + 8] == 2.0 && Math.Abs(arr[i + 11] - arr[i + 14]) < 1e-6 && InR(arr[i + 11])
                    && InR(arr[i + 9]) && InR(arr[i + 10]) && InR(arr[i + 12]) && InR(arr[i + 13]))
                {
                    g.Lines.Add(new LineRec { X1 = arr[i + 9], Y1 = arr[i + 10], X2 = arr[i + 12], Y2 = arr[i + 13] });
                    i += 15;
                }
                else i++;
            }

            // Curves: tolerant walk for N>=3 point-runs (tessellated arcs/circles). Every run gets a
            // fitted CENTER + RADIUS (circumcenter of three spread tessellation points — exact for any
            // circular arc, independent of tessellation uniformity). A CLOSED run (start == end) is a
            // FULL CIRCLE and goes to Circles as {cx, cy, r} — explicit, so a reader never has
            // to decode the "x1=+r, xm=-r" start/mid/end pattern (the Sonnet benchmark read that pattern
            // as "the tool doesn't report centers" and mis-placed every bore). Open runs stay in Curves
            // as {n, start, mid, end} + fitted center.
            // Points are (x, y, depth); require all points in a run to share one depth (planar) — this is
            // the validity filter (a header's stray values are not consistently planar).
            for (int j = 0; j < nlen - 1;)
            {
                int N = -1, basep = -1;
                for (int h = 0; h <= 13 && j + h < nlen; h++)
                {
                    double Nd = arr[j + h];
                    if (Nd != Math.Floor(Nd)) continue;
                    int Ni = (int)Nd;
                    if (Ni < 3 || Ni > 500) continue;
                    int b = j + h + 1;
                    if (b + 3 * Ni > nlen) continue;
                    double depth = arr[b + 2];
                    if (!InR(depth)) continue;
                    bool ok = true;
                    for (int k = 0; k < Ni; k++)
                        if (Math.Abs(arr[b + 3 * k + 2] - depth) > 1e-6 || !InR(arr[b + 3 * k]) || !InR(arr[b + 3 * k + 1])) { ok = false; break; }
                    if (ok) { N = Ni; basep = b; break; }
                }
                if (basep < 0) { j++; continue; }
                // Drop hidden (obscured) curves/circles from ortho views. The 6-double display block sits
                // immediately before the point-count (which is at basep-1), i.e. arr[basep-7 .. basep-2];
                // HIDDEN == [2,1,1,0,-1,0]. Matching the FULL signature is fail-safe — the VISIBLE block
                // [.,.,-1,1,-1,0] differs in the first field, so a visible edge is never dropped.
                if (filterHidden && basep >= 7
                    && Math.Abs(arr[basep - 7] - 2) < 0.5 && Math.Abs(arr[basep - 6] - 1) < 0.5
                    && Math.Abs(arr[basep - 5] - 1) < 0.5 && Math.Abs(arr[basep - 4]) < 0.5
                    && Math.Abs(arr[basep - 3] + 1) < 0.5 && Math.Abs(arr[basep - 2]) < 0.5)
                {
                    j = basep + 3 * N;   // hidden run — skip entirely
                    continue;
                }
                int mid = N / 2;
                double sx = arr[basep], sy = arr[basep + 1];
                double ex = arr[basep + 3 * (N - 1)], ey = arr[basep + 3 * (N - 1) + 1];
                // Center fit from three spread points (0, N/3, 2N/3) — for a closed run the endpoints
                // coincide, so start/mid/end would be only two distinct points; the spread triple is
                // always three distinct points on the arc.
                int i1 = N / 3, i2 = (2 * N) / 3;
                double[] cc = Circumcenter(
                    sx, sy,
                    arr[basep + 3 * i1], arr[basep + 3 * i1 + 1],
                    arr[basep + 3 * i2], arr[basep + 3 * i2 + 1]);
                bool closed = Math.Abs(sx - ex) < 1e-6 && Math.Abs(sy - ey) < 1e-6;
                if (closed && cc != null)
                {
                    g.Circles.Add(new CircleRec { CX = cc[0], CY = cc[1], R = cc[2] });
                }
                else
                {
                    var cr = new CurveRec
                    {
                        N = N,
                        SX = sx, SY = sy,
                        MX = arr[basep + 3 * mid], MY = arr[basep + 3 * mid + 1],
                        EX = ex, EY = ey
                    };
                    if (cc != null) { cr.HasCenter = true; cr.CX = cc[0]; cr.CY = cc[1]; cr.R = cc[2]; }
                    g.Curves.Add(cr);
                }
                j = basep + 3 * N;
            }
            return g;
        }

        // Emit the extracted geometry in the contract shape (unchanged since Batch-1.5): lines / curves /
        // circles / frame, all R6-rounded.
        private JObject BuildGeometryJson(ViewGeomData g, IView view)
        {
            var gj = new JObject();
            if (g.Err != null) { gj["err"] = g.Err; return gj; }

            var lines = new JArray();
            foreach (var l in g.Lines)
                lines.Add(new JObject { ["x1"] = R6(l.X1), ["y1"] = R6(l.Y1), ["x2"] = R6(l.X2), ["y2"] = R6(l.Y2) });

            var curves = new JArray();
            foreach (var c in g.Curves)
            {
                var cj = new JObject();
                cj["n"] = c.N;
                cj["x1"] = R6(c.SX); cj["y1"] = R6(c.SY);
                cj["xm"] = R6(c.MX); cj["ym"] = R6(c.MY);
                cj["x2"] = R6(c.EX); cj["y2"] = R6(c.EY);
                if (c.HasCenter) { cj["cx"] = R6(c.CX); cj["cy"] = R6(c.CY); cj["r"] = R6(c.R); }
                curves.Add(cj);
            }

            var circles = new JArray();
            foreach (var c in g.Circles)
                circles.Add(new JObject { ["cx"] = R6(c.CX), ["cy"] = R6(c.CY), ["r"] = R6(c.R) });

            gj["lines"] = lines;
            gj["curves"] = curves;
            gj["circles"] = circles;
            // Model-space frame: p_model = origin + x*xdir + y*ydir for every coordinate above (and for
            // the circle centers) — the deterministic view-2D → model-3D mapping. Falls back to the
            // legacy prose note when the view exposes no transform (e.g. the sheet container).
            var vfr = ViewModelFrame(view);
            if (vfr != null)
            {
                gj["frame"] = vfr;
                var ext = ComputeViewExtent(g, view);
                if (ext != null) gj["extent"] = ext;
            }
            else gj["frame"] = "model-scale meters (x,y in the view plane), centered on view centroid";
            return gj;
        }

        // ADR-055: the view's MODEL-SPACE span along its two resolved axes, computed SERVER-SIDE over
        // ALL extracted primitives with the frame's component SIGNS applied — so "is there material
        // beyond z=0.045 in this view?" is a field read, not client-side frame arithmetic (benchmark
        // run #2 dropped ydir's sign and read the z∈[0.045,0.065] loft silhouette as absent). Exact:
        // circles contribute center±r, arcs contribute endpoints + any axis-extreme point that lies
        // within their angular span. Omitted (loudly absent) when the frame is not axis-aligned or the
        // view has no primitives.
        private JObject ComputeViewExtent(ViewGeomData g, IView view)
        {
            double[] o, x, y;
            if (!ViewModelFrameRaw(view, out o, out x, out y)) return null;
            string axX = PrincipalAxis(x[0], x[1], x[2]);
            string axY = PrincipalAxis(y[0], y[1], y[2]);
            if (axX == null || axY == null) return null;
            int kx = AxisIndex(axX), ky = AxisIndex(axY);

            double[] mn = { double.MaxValue, double.MaxValue, double.MaxValue };
            double[] mx = { double.MinValue, double.MinValue, double.MinValue };
            bool any = false;
            Action<double, double> add = (u, v) =>
            {
                foreach (int k in new int[] { kx, ky })
                {
                    double val = o[k] + u * x[k] + v * y[k];
                    if (val < mn[k]) mn[k] = val;
                    if (val > mx[k]) mx[k] = val;
                }
                any = true;
            };

            foreach (var l in g.Lines) { add(l.X1, l.Y1); add(l.X2, l.Y2); }
            foreach (var c in g.Circles)
            {
                add(c.CX - c.R, c.CY); add(c.CX + c.R, c.CY);
                add(c.CX, c.CY - c.R); add(c.CX, c.CY + c.R);
            }
            foreach (var a in g.Curves)
            {
                add(a.SX, a.SY); add(a.MX, a.MY); add(a.EX, a.EY);
                if (a.HasCenter)
                {
                    // Axis-extreme points of the arc's circle, included only when ON the arc.
                    double[][] cand = {
                        new double[] { a.CX - a.R, a.CY }, new double[] { a.CX + a.R, a.CY },
                        new double[] { a.CX, a.CY - a.R }, new double[] { a.CX, a.CY + a.R } };
                    foreach (var p in cand)
                        if (AngOnArc(a, p[0], p[1])) add(p[0], p[1]);
                }
            }
            if (!any) return null;

            var ext = new JObject();
            // Canonical X→Y→Z key order.
            foreach (int k in kx < ky ? new int[] { kx, ky } : new int[] { ky, kx })
            {
                string ax = k == 0 ? "X" : (k == 1 ? "Y" : "Z");
                ext[ax] = new JArray { R6(mn[k]), R6(mx[k]) };
            }
            return ext;
        }

        // Circumcenter + radius of the circle through three 2D points; null when degenerate (collinear).
        private static double[] Circumcenter(double ax, double ay, double bx, double by, double cx, double cy)
        {
            double dd = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by));
            if (Math.Abs(dd) < 1e-12) return null;
            double a2 = ax * ax + ay * ay, b2 = bx * bx + by * by, c2 = cx * cx + cy * cy;
            double ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / dd;
            double uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / dd;
            double r = Math.Sqrt((ux - ax) * (ux - ax) + (uy - ay) * (uy - ay));
            return new double[] { ux, uy, r };
        }

        private static bool Z(double v) { return Math.Abs(v) < 1e-6; }
        private static bool InR(double v) { return Math.Abs(v) < 2.0; }

        // ====================================================================================
        // Batch-4 — deterministic RELATIONS with PROVENANCE + cross-view STATION join keys.
        //
        // Design rules (Observation-Graph review 2026-07-16, approved subset):
        //  - GROUP form only, never pairwise dumps (ADR-030 payload lesson).
        //  - Every relation/group carries `source` (a slug from the CLOSED enum documented in
        //    tool-schemas.json) + `residual` (max measured deviation, meters, 6dp). A relation that
        //    cannot name a deterministic source is structurally impossible to emit — the detector
        //    itself assigns the slug. No confidence scores, no inference fields.
        //  - Cross-view output = join keys + candidate sets. NEVER "same physical entity".
        //  - All math runs on RAW (unrounded) extracted values; rounding happens at emit time only,
        //    so `residual` reports the true deviation.
        //  - touches = junction contacts only (shared_endpoint + endpoint_on_curve). X-crossings of
        //    projected edges are DELIBERATELY not emitted: a 2D projection crossing carries no
        //    same-depth guarantee, so emitting it would invite a false "contact" reading.
        // ====================================================================================

        // Cluster / match tolerance: 10 µm at model scale. Payload resolution is 1 µm (6dp); the
        // circumcenter fit is exact for true circular arcs (sub-µm noise); genuinely distinct design
        // geometry sits ≥ 100 µm apart — two orders of margin on both sides.
        private const double RelTol = 1e-5;

        private class StationViewData
        {
            public string Vid;
            public double[] O, X, Y;
            public ViewGeomData Geom;
        }

        // Minimal union-find for the clustering passes.
        private class UF
        {
            private readonly int[] _p;
            public UF(int n) { _p = new int[n]; for (int i = 0; i < n; i++) _p[i] = i; }
            public int Find(int i) { while (_p[i] != i) { _p[i] = _p[_p[i]]; i = _p[i]; } return i; }
            public void Union(int a, int b) { int ra = Find(a), rb = Find(b); if (ra != rb) _p[ra] = rb; }
        }

        private static double Dist2D(double ax, double ay, double bx, double by)
        {
            double dx = ax - bx, dy = ay - by;
            return Math.Sqrt(dx * dx + dy * dy);
        }

        // Sort primitive ids deterministically: kind rank c < a < l, then numeric index.
        private static void SortPrimitiveIds(List<string> ids)
        {
            ids.Sort((x, y) =>
            {
                int rx = x[0] == 'c' ? 0 : (x[0] == 'a' ? 1 : 2);
                int ry = y[0] == 'c' ? 0 : (y[0] == 'a' ? 1 : 2);
                if (rx != ry) return rx.CompareTo(ry);
                int ix, iy;
                int.TryParse(x.Substring(1), out ix);
                int.TryParse(y.Substring(1), out iy);
                return ix.CompareTo(iy);
            });
        }

        // Is the point (px,py) — assumed to lie ON the arc's circle — within the arc's swept span?
        // The arc runs start → mid → end; mid disambiguates the direction. Angular tolerance is the
        // linear RelTol mapped through the radius.
        private static bool AngOnArc(CurveRec a, double px, double py)
        {
            if (!a.HasCenter || a.R < 1e-9) return false;
            double TwoPi = 2.0 * Math.PI;
            double aS = Math.Atan2(a.SY - a.CY, a.SX - a.CX);
            double aM = Math.Atan2(a.MY - a.CY, a.MX - a.CX);
            double aE = Math.Atan2(a.EY - a.CY, a.EX - a.CX);
            double aP = Math.Atan2(py - a.CY, px - a.CX);
            double ccw = ((aE - aS) % TwoPi + TwoPi) % TwoPi;
            double posM = ((aM - aS) % TwoPi + TwoPi) % TwoPi;
            double posP = ((aP - aS) % TwoPi + TwoPi) % TwoPi;
            double tolA = Math.Min(0.5, RelTol / a.R);
            if (posM <= ccw)   // CCW arc from S to E
                return posP <= ccw + tolA || posP >= TwoPi - tolA;
            // CW arc: swept region is [ccw, 2π] (in CCW-offset terms)
            return posP >= ccw - tolA || posP <= tolA;
        }

        // Perpendicular foot of (px,py) on the INFINITE line p1→p2. Returns false when degenerate.
        private static bool LinePerpFoot(double px, double py, double x1, double y1, double x2, double y2,
            out double t, out double fx, out double fy, out double perpDist)
        {
            t = 0; fx = 0; fy = 0; perpDist = 0;
            double dx = x2 - x1, dy = y2 - y1;
            double l2 = dx * dx + dy * dy;
            if (l2 < 1e-18) return false;
            t = ((px - x1) * dx + (py - y1) * dy) / l2;
            fx = x1 + t * dx; fy = y1 + t * dy;
            perpDist = Dist2D(px, py, fx, fy);
            return true;
        }

        // Distance from (px,py) to the SEGMENT p1→p2 (clamped), with the unclamped param out.
        private static double SegDist(double px, double py, double x1, double y1, double x2, double y2, out double tRaw)
        {
            double t, fx, fy, pd;
            if (!LinePerpFoot(px, py, x1, y1, x2, y2, out t, out fx, out fy, out pd))
            { tRaw = 0; return Dist2D(px, py, x1, y1); }
            tRaw = t;
            double tc = t < 0 ? 0 : (t > 1 ? 1 : t);
            return Dist2D(px, py, x1 + tc * (x2 - x1), y1 + tc * (y2 - y1));
        }

        // ---- Per-view relation groups (all four mandated families) ----
        private JObject ComputeViewRelations(ViewGeomData g)
        {
            // Centered items: circles first, then arcs with a fitted center — ids c<i> / a<i>.
            int nc = g.Circles.Count;
            var items = new List<double[]>();       // {cx, cy, r}
            var itemIds = new List<string>();
            var itemIsArc = new List<bool>();
            var itemArc = new List<CurveRec>();
            for (int i = 0; i < nc; i++)
            {
                items.Add(new double[] { g.Circles[i].CX, g.Circles[i].CY, g.Circles[i].R });
                itemIds.Add("c" + i); itemIsArc.Add(false); itemArc.Add(null);
            }
            for (int i = 0; i < g.Curves.Count; i++)
            {
                var c = g.Curves[i];
                if (!c.HasCenter) continue;
                items.Add(new double[] { c.CX, c.CY, c.R });
                itemIds.Add("a" + i); itemIsArc.Add(true); itemArc.Add(c);
            }
            int n = items.Count;

            // concentric: shared center within RelTol.
            var concentric = new JArray();
            if (n >= 2)
            {
                var uf = new UF(n);
                for (int i = 0; i < n; i++)
                    for (int j = i + 1; j < n; j++)
                        if (Dist2D(items[i][0], items[i][1], items[j][0], items[j][1]) <= RelTol)
                            uf.Union(i, j);
                var groups = new Dictionary<int, List<int>>();
                for (int i = 0; i < n; i++)
                {
                    int r = uf.Find(i);
                    if (!groups.ContainsKey(r)) groups[r] = new List<int>();
                    groups[r].Add(i);
                }
                var ordered = new List<List<int>>();
                foreach (var kv in groups) if (kv.Value.Count >= 2) ordered.Add(kv.Value);
                ordered.Sort((a, b) => a[0].CompareTo(b[0]));
                foreach (var grp in ordered)
                {
                    double mx = 0, my = 0;
                    foreach (int i in grp) { mx += items[i][0]; my += items[i][1]; }
                    mx /= grp.Count; my /= grp.Count;
                    double resid = 0;
                    var radii = new List<double>();
                    var mids = new List<string>();
                    foreach (int i in grp)
                    {
                        resid = Math.Max(resid, Dist2D(items[i][0], items[i][1], mx, my));
                        radii.Add(items[i][2]);
                        mids.Add(itemIds[i]);
                    }
                    radii.Sort();
                    SortPrimitiveIds(mids);
                    var radiiArr = new JArray(); foreach (double r in radii) radiiArr.Add(R6(r));
                    concentric.Add(new JObject
                    {
                        ["members"] = new JArray(mids.ToArray()),
                        ["center"] = new JArray { R6(mx), R6(my) },
                        ["radii"] = radiiArr,
                        ["source"] = "center_coincide",
                        ["residual"] = R6(resid)
                    });
                }
            }

            // equal_diameter: full CIRCLES of the same r at distinct centers.
            var equalDia = new JArray();
            if (nc >= 2)
            {
                var uf = new UF(nc);
                for (int i = 0; i < nc; i++)
                    for (int j = i + 1; j < nc; j++)
                        if (Math.Abs(g.Circles[i].R - g.Circles[j].R) <= RelTol)
                            uf.Union(i, j);
                var groups = new Dictionary<int, List<int>>();
                for (int i = 0; i < nc; i++)
                {
                    int r = uf.Find(i);
                    if (!groups.ContainsKey(r)) groups[r] = new List<int>();
                    groups[r].Add(i);
                }
                var ordered = new List<List<int>>();
                foreach (var kv in groups)
                {
                    if (kv.Value.Count < 2) continue;
                    bool distinct = false;   // require at least one pair of genuinely distinct centers
                    for (int a = 0; a < kv.Value.Count && !distinct; a++)
                        for (int b = a + 1; b < kv.Value.Count && !distinct; b++)
                            if (Dist2D(g.Circles[kv.Value[a]].CX, g.Circles[kv.Value[a]].CY,
                                       g.Circles[kv.Value[b]].CX, g.Circles[kv.Value[b]].CY) > RelTol)
                                distinct = true;
                    if (distinct) ordered.Add(kv.Value);
                }
                ordered.Sort((a, b) => a[0].CompareTo(b[0]));
                foreach (var grp in ordered)
                {
                    double mr = 0;
                    foreach (int i in grp) mr += g.Circles[i].R;
                    mr /= grp.Count;
                    double resid = 0;
                    var mids = new JArray();
                    var centers = new JArray();
                    foreach (int i in grp)
                    {
                        resid = Math.Max(resid, Math.Abs(g.Circles[i].R - mr));
                        mids.Add("c" + i);
                        centers.Add(new JArray { R6(g.Circles[i].CX), R6(g.Circles[i].CY) });
                    }
                    equalDia.Add(new JObject
                    {
                        ["members"] = mids,
                        ["r"] = R6(mr),
                        ["centers"] = centers,
                        ["source"] = "radius_equal",
                        ["residual"] = R6(resid)
                    });
                }
            }

            // tangent: circle/arc ↔ circle/arc (dist = r1+r2 external / |r1−r2| internal) and
            // line ↔ circle/arc (perpendicular distance = r, foot inside the segment span).
            var tangent = new JArray();
            for (int i = 0; i < n; i++)
            {
                for (int j = i + 1; j < n; j++)
                {
                    double d = Dist2D(items[i][0], items[i][1], items[j][0], items[j][1]);
                    if (d <= RelTol) continue;   // concentric — its own family
                    double ri = items[i][2], rj = items[j][2];
                    double devExt = Math.Abs(d - (ri + rj));
                    double devInt = Math.Abs(d - Math.Abs(ri - rj));
                    bool ext = devExt <= RelTol && devExt <= devInt;
                    bool intr = !ext && devInt <= RelTol && Math.Abs(ri - rj) > RelTol;
                    if (!ext && !intr) continue;
                    double px = items[i][0] + (items[j][0] - items[i][0]) * (ri / d);
                    double py = items[i][1] + (items[j][1] - items[i][1]) * (ri / d);
                    if (itemIsArc[i] && !AngOnArc(itemArc[i], px, py)) continue;
                    if (itemIsArc[j] && !AngOnArc(itemArc[j], px, py)) continue;
                    var mids = new List<string> { itemIds[i], itemIds[j] };
                    SortPrimitiveIds(mids);
                    tangent.Add(new JObject
                    {
                        ["members"] = new JArray(mids.ToArray()),
                        ["at"] = new JArray { R6(px), R6(py) },
                        ["source"] = ext ? "dist_eq_radius_sum" : "dist_eq_radius_diff",
                        ["residual"] = R6(ext ? devExt : devInt)
                    });
                }
            }
            for (int li = 0; li < g.Lines.Count; li++)
            {
                var l = g.Lines[li];
                double segLen = Dist2D(l.X1, l.Y1, l.X2, l.Y2);
                if (segLen < 1e-12) continue;
                double tolT = RelTol / segLen;
                for (int k = 0; k < n; k++)
                {
                    double t, fx, fy, pd;
                    if (!LinePerpFoot(items[k][0], items[k][1], l.X1, l.Y1, l.X2, l.Y2, out t, out fx, out fy, out pd))
                        continue;
                    if (pd <= RelTol) continue;   // line through the center — not tangency
                    double dev = Math.Abs(pd - items[k][2]);
                    if (dev > RelTol) continue;
                    if (t < -tolT || t > 1 + tolT) continue;
                    double px = items[k][0] + (fx - items[k][0]) * (items[k][2] / pd);
                    double py = items[k][1] + (fy - items[k][1]) * (items[k][2] / pd);
                    if (itemIsArc[k] && !AngOnArc(itemArc[k], px, py)) continue;
                    var mids = new List<string> { itemIds[k], "l" + li };
                    SortPrimitiveIds(mids);
                    tangent.Add(new JObject
                    {
                        ["members"] = new JArray(mids.ToArray()),
                        ["at"] = new JArray { R6(px), R6(py) },
                        ["source"] = "line_dist_eq_radius",
                        ["residual"] = R6(dev)
                    });
                }
            }

            // touches DROPPED (2026-07-19 trim) — the benchmark data-consumption report showed it
            // fully redundant with `tangent` for corner/junction identification and otherwise unused.
            var rel = new JObject();
            if (concentric.Count > 0) rel["concentric"] = concentric;
            if (equalDia.Count > 0) rel["equal_diameter"] = equalDia;
            if (tangent.Count > 0) rel["tangent"] = tangent;
            return rel.Count > 0 ? rel : null;   // empty buckets dropped
        }

        // Merge same-position T-contacts into one group record (position within RelTol).
        private static void AddTContact(List<double[]> recs, List<List<string>> members,
            double px, double py, double residual, string idA, string idB)
        {
            for (int i = 0; i < recs.Count; i++)
            {
                if (Dist2D(recs[i][0], recs[i][1], px, py) <= RelTol)
                {
                    if (!members[i].Contains(idA)) members[i].Add(idA);
                    if (!members[i].Contains(idB)) members[i].Add(idB);
                    recs[i][2] = Math.Max(recs[i][2], residual);
                    return;
                }
            }
            recs.Add(new double[] { px, py, residual });
            members.Add(new List<string> { idA, idB });
        }

        // ---- Cross-view STATION join keys ----
        // Each axis-aligned view resolves the two MODEL axes its frame spans; a primitive's key points
        // (circle center and center±r, line endpoints or its constant coordinate, arc center+endpoints)
        // yield model-axis coordinate values ("stations"). Values are clustered within RelTol per axis;
        // only clusters carrying members from ≥2 DIFFERENT views are emitted — those are the join keys.
        // Members are CANDIDATE sets; concluding "same physical entity" is the reader's job, and with
        // >1 candidate the ambiguity stays visible by construction.
        private JObject ComputeStations(List<StationViewData> svs)
        {
            var perAxis = new Dictionary<string, List<object[]>>();   // axis → {value, vid, id}
            var skipped = new JArray();

            foreach (var sv in svs)
            {
                string axX = PrincipalAxis(sv.X[0], sv.X[1], sv.X[2]);
                string axY = PrincipalAxis(sv.Y[0], sv.Y[1], sv.Y[2]);
                if (axX == null || axY == null)
                {
                    skipped.Add(sv.Vid);   // loud: this view cannot join the station table
                    continue;
                }
                int kx = AxisIndex(axX), ky = AxisIndex(axY);

                Action<string, double, double, double, string> addPoint = (id, u, v, r, mode) =>
                {
                    // p = O + u*X + v*Y (full 3D); take the two in-plane axis components.
                    double[] p = new double[3];
                    for (int k = 0; k < 3; k++) p[k] = sv.O[k] + u * sv.X[k] + v * sv.Y[k];
                    foreach (int k in new int[] { kx, ky })
                    {
                        string ax = k == 0 ? "X" : (k == 1 ? "Y" : "Z");
                        AddStation(perAxis, ax, p[k], sv.Vid, id);
                        if (mode == "circle" && r > 0)
                        {
                            AddStation(perAxis, ax, p[k] - r, sv.Vid, id);
                            AddStation(perAxis, ax, p[k] + r, sv.Vid, id);
                        }
                    }
                };

                var gd = sv.Geom;
                for (int i = 0; i < gd.Circles.Count; i++)
                    addPoint("c" + i, gd.Circles[i].CX, gd.Circles[i].CY, gd.Circles[i].R, "circle");
                for (int i = 0; i < gd.Lines.Count; i++)
                {
                    var l = gd.Lines[i];
                    // Constant-coordinate handling happens naturally: both endpoints land in one cluster.
                    addPoint("l" + i, l.X1, l.Y1, 0, "point");
                    addPoint("l" + i, l.X2, l.Y2, 0, "point");
                }
                for (int i = 0; i < gd.Curves.Count; i++)
                {
                    var a = gd.Curves[i];
                    if (a.HasCenter) addPoint("a" + i, a.CX, a.CY, 0, "point");
                    addPoint("a" + i, a.SX, a.SY, 0, "point");
                    addPoint("a" + i, a.EX, a.EY, 0, "point");
                }
            }

            var axes = new JArray();
            foreach (string ax in new string[] { "X", "Y", "Z" })
            {
                if (!perAxis.ContainsKey(ax)) continue;
                var entries = perAxis[ax];
                entries.Sort((a, b) => ((double)a[0]).CompareTo((double)b[0]));
                var values = new JArray();
                int i0 = 0;
                while (i0 < entries.Count)
                {
                    int i1 = i0;
                    while (i1 + 1 < entries.Count && (double)entries[i1 + 1][0] - (double)entries[i1][0] <= RelTol)
                        i1++;
                    // cluster = entries[i0..i1]
                    var vids = new HashSet<string>();
                    for (int i = i0; i <= i1; i++) vids.Add((string)entries[i][1]);
                    if (vids.Count >= 2)
                    {
                        double mean = 0;
                        for (int i = i0; i <= i1; i++) mean += (double)entries[i][0];
                        mean /= (i1 - i0 + 1);
                        double resid = 0;
                        for (int i = i0; i <= i1; i++) resid = Math.Max(resid, Math.Abs((double)entries[i][0] - mean));
                        var membersByVid = new SortedDictionary<string, List<string>>();
                        for (int i = i0; i <= i1; i++)
                        {
                            string vid = (string)entries[i][1], id = (string)entries[i][2];
                            if (!membersByVid.ContainsKey(vid)) membersByVid[vid] = new List<string>();
                            if (!membersByVid[vid].Contains(id)) membersByVid[vid].Add(id);
                        }
                        // Payload trim (ADR-057): only emit a station that joins a FEATURE axis (some
                        // circle/arc member); all-line coincidences (block/slot edges) are corroborating.
                        bool _stHasFeat = false;
                        foreach (var kv in membersByVid)
                        {
                            foreach (var id in kv.Value) if (id[0] == 'c' || id[0] == 'a') { _stHasFeat = true; break; }
                            if (_stHasFeat) break;
                        }
                        if (_stHasFeat)
                        {
                            var membersJ = new JObject();
                            foreach (var kv in membersByVid)
                            {
                                SortPrimitiveIds(kv.Value);
                                membersJ[kv.Key] = new JArray(kv.Value.ToArray());
                            }
                            values.Add(new JObject
                            {
                                ["v"] = R6(mean),
                                ["members"] = membersJ,
                                ["residual"] = R6(resid)
                            });
                        }
                    }
                    i0 = i1 + 1;
                }
                if (values.Count > 0)
                    axes.Add(new JObject { ["axis"] = ax, ["values"] = values });
            }

            if (axes.Count == 0 && skipped.Count == 0) return null;
            var stations = new JObject { ["tol"] = RelTol, ["axes"] = axes };
            if (skipped.Count > 0) stations["skipped_views"] = skipped;
            return stations;
        }

        private static int AxisIndex(string ax) { return ax == "X" ? 0 : (ax == "Y" ? 1 : 2); }

        private static void AddStation(Dictionary<string, List<object[]>> perAxis, string ax, double v, string vid, string id)
        {
            if (!perAxis.ContainsKey(ax)) perAxis[ax] = new List<object[]>();
            perAxis[ax].Add(new object[] { v, vid, id });
        }

        // ---- Dimension → primitive attachment (Batch-4 deliverable 3) ----
        // Each MODEL-space anchor is mapped into the view's 2D frame (u=(p−o)·xdir, v=(p−o)·ydir) and
        // matched against the extracted primitives arithmetically. `measures` lists ALL primitives any
        // anchor resolves to (a corner anchor legitimately touches two lines — candidates stay visible);
        // `measure_src` = anchor_at_center when any anchor sits on a circle/arc center (the Ø/R shape),
        // else anchor_on_primitive; `measure_residual` = the worst contributing deviation. No resolvable
        // primitive at all ⇒ `unattached: true` — loud, never dropped, never guessed.
        private void AttachDimensionToPrimitives(JObject dj, JArray anchors, ViewGeomData g,
            double[] O, double[] X, double[] Y)
        {
            if (anchors == null || anchors.Count == 0) { dj["unattached"] = true; return; }
            var matched = new List<string>();
            bool anyCenter = false;
            double maxResid = 0;
            int unmatchedAnchors = 0;

            foreach (var aTok in anchors)
            {
                var pa = aTok as JArray;
                if (pa == null || pa.Count < 3) continue;
                double px = (double)pa[0], py = (double)pa[1], pz = (double)pa[2];
                double dx = px - O[0], dy = py - O[1], dz = pz - O[2];
                double u = dx * X[0] + dy * X[1] + dz * X[2];
                double v = dx * Y[0] + dy * Y[1] + dz * Y[2];
                bool any = false;

                for (int i = 0; i < g.Circles.Count; i++)
                {
                    double dc = Dist2D(u, v, g.Circles[i].CX, g.Circles[i].CY);
                    if (dc <= RelTol)
                    { AddMatch(matched, "c" + i, dc, ref maxResid); anyCenter = true; any = true; }
                    else if (Math.Abs(dc - g.Circles[i].R) <= RelTol)
                    { AddMatch(matched, "c" + i, Math.Abs(dc - g.Circles[i].R), ref maxResid); any = true; }
                }
                for (int i = 0; i < g.Curves.Count; i++)
                {
                    var a = g.Curves[i];
                    if (a.HasCenter)
                    {
                        double dc = Dist2D(u, v, a.CX, a.CY);
                        if (dc <= RelTol)
                        { AddMatch(matched, "a" + i, dc, ref maxResid); anyCenter = true; any = true; continue; }
                        double dev = Math.Abs(dc - a.R);
                        if (dev <= RelTol && AngOnArc(a, u, v))
                        { AddMatch(matched, "a" + i, dev, ref maxResid); any = true; continue; }
                    }
                    if (Dist2D(u, v, a.SX, a.SY) <= RelTol || Dist2D(u, v, a.EX, a.EY) <= RelTol)
                    { AddMatch(matched, "a" + i, 0, ref maxResid); any = true; }
                }
                for (int i = 0; i < g.Lines.Count; i++)
                {
                    var l = g.Lines[i];
                    double tRaw;
                    double d = SegDist(u, v, l.X1, l.Y1, l.X2, l.Y2, out tRaw);
                    if (d <= RelTol) { AddMatch(matched, "l" + i, d, ref maxResid); any = true; }
                }
                if (!any) unmatchedAnchors++;
            }

            if (matched.Count > 0)
            {
                SortPrimitiveIds(matched);
                dj["measures"] = new JArray(matched.ToArray());
                dj["measure_src"] = anyCenter ? "anchor_at_center" : "anchor_on_primitive";
                dj["measure_residual"] = R6(maxResid);
                if (unmatchedAnchors > 0) dj["unmatched_anchors"] = unmatchedAnchors;
            }
            else dj["unattached"] = true;
        }

        private static void AddMatch(List<string> matched, string id, double residual, ref double maxResid)
        {
            if (!matched.Contains(id)) matched.Add(id);
            maxResid = Math.Max(maxResid, residual);
        }

        // ---- Center marks (Batch-4 deliverable 5) ----
        // ICenterMark walk (GetFirstCenterMark/GetNext — reflection-verified). Position source PINNED by
        // the 2026-07-17 live A/B on level-2-2 (ADR-035 empiricism): `ICenterMark.GetPosition(index)`
        // returns the mark center directly in the VIEW-2D geometry space (its x,y matched the circle
        // centers exactly — "matched via view-space" for every mark), while `IAnnotation.GetPosition()`
        // is the sheet-space LABEL anchor (y ≈ 0.25 on the A3 sheet — never a mark center) and is
        // therefore NOT read. A mark whose position matches no circle center within RelTol is emitted
        // `unmatched: true` — loud, never guessed.
        private JArray ReadCenterMarks(IView view, double[] O, double[] X, double[] Y, ViewGeomData g)
        {
            var arrOut = new JArray();
            object cmObj = null;
            try { cmObj = view.GetFirstCenterMark(); } catch { return arrOut; }
            int guard = 0;
            while (cmObj != null && guard++ < 500)
            {
                var cm = cmObj as ICenterMark;
                if (cm == null) break;

                var rawPts = new List<double[]>();
                try
                {
                    bool grouped = false; int gc = 1;
                    try { grouped = cm.IsGrouped; } catch { }
                    try { if (grouped) gc = cm.GroupCount; } catch { gc = 1; }
                    for (int i = 0; i < Math.Max(1, gc) && i < 100; i++)
                    {
                        var p = ToDoubleArray(cm.GetPosition(i));
                        if (p != null && p.Length >= 2) rawPts.Add(new double[] { p[0], p[1] });
                    }
                }
                catch { }

                foreach (var raw in rawPts)
                {
                    double u = raw[0], v = raw[1];
                    var on = new List<string>();
                    double res = 0;
                    for (int i = 0; i < g.Circles.Count; i++)
                    {
                        double dc = Dist2D(u, v, g.Circles[i].CX, g.Circles[i].CY);
                        if (dc <= RelTol) { on.Add("c" + i); res = Math.Max(res, dc); }
                    }
                    if (on.Count > 0)
                    {
                        SortPrimitiveIds(on);
                        arrOut.Add(new JObject
                        {
                            ["at"] = new JArray { R6(u), R6(v) },
                            ["on"] = new JArray(on.ToArray()),
                            ["source"] = "center_coincide",
                            ["residual"] = R6(res)
                        });
                    }
                    else
                    {
                        ExecLog.Write($"center_mark UNMATCHED at=({R6(u)},{R6(v)}) view='{view.Name}'");
                        arrOut.Add(new JObject
                        {
                            ["at"] = new JArray { R6(u), R6(v) },
                            ["unmatched"] = true
                        });
                    }
                }

                object next = null;
                try { next = cm.GetNext(); } catch { next = null; }
                cmObj = next;
            }
            return arrOut;
        }

        // ---- Centerlines (Batch-4 deliverable 5, best-effort per the API's shape) ----
        // ICenterLine itself exposes NO endpoints (only GetAnnotation — a single point), so the
        // segment geometry is read from IView.GetCenterLineSketch()'s sketch segments, presumed to be
        // in the view's sketch space (= the geometry space; live A/B decides — raw values ExecLog'd).
        // When the view reports centerlines but the sketch route yields nothing readable, the caller
        // emits `centerlines_unreadable` — the gap stays loud instead of heuristically inferred.
        private JArray ReadCenterlines(IView view, ViewGeomData g)
        {
            var arrOut = new JArray();
            int reported = 0;
            try { reported = view.GetCenterLineCount(); } catch { }
            try
            {
                var sk = view.GetCenterLineSketch() as ISketch;
                if (sk != null)
                {
                    var segs = sk.GetSketchSegments() as object[];
                    if (segs != null)
                    {
                        foreach (var so in segs)
                        {
                            var seg = so as ISketchSegment;
                            if (seg == null) continue;
                            int st = -1;
                            try { st = seg.GetType(); } catch { }
                            if (st != 0) continue;   // swSketchLINE = 0 (inlined, ADR-018)
                            var ln = seg as ISketchLine;
                            if (ln == null) continue;
                            ISketchPoint sp = null, ep = null;
                            try { sp = ln.IGetStartPoint2(); ep = ln.IGetEndPoint2(); } catch { }
                            if (sp == null || ep == null) continue;
                            double x1 = sp.X, y1 = sp.Y, x2 = ep.X, y2 = ep.Y;
                            ExecLog.Write($"centerline raw=({x1:R},{y1:R})-({x2:R},{y2:R}) view='{view.Name}'");
                            var cj = new JObject
                            {
                                ["x1"] = R6(x1), ["y1"] = R6(y1),
                                ["x2"] = R6(x2), ["y2"] = R6(y2)
                            };
                            // Circles whose center lies ON this centerline (arithmetic, RelTol).
                            var through = new List<string>();
                            double worst = 0;
                            for (int i = 0; i < g.Circles.Count; i++)
                            {
                                double tRaw;
                                double d = SegDist(g.Circles[i].CX, g.Circles[i].CY, x1, y1, x2, y2, out tRaw);
                                if (d <= RelTol) { through.Add("c" + i); worst = Math.Max(worst, d); }
                            }
                            if (through.Count > 0)
                            {
                                SortPrimitiveIds(through);
                                cj["through"] = new JArray(through.ToArray());
                                cj["source"] = "center_coincide";
                                cj["residual"] = R6(worst);
                            }
                            arrOut.Add(cj);
                        }
                    }
                }
            }
            catch (Exception ex) { ExecLog.Write($"ReadCenterlines err: {ex.Message}"); }

            if (arrOut.Count == 0 && reported > 0)
            {
                // Loud gap: the view SAYS it has centerlines but the sketch route exposed none.
                var gap = new JObject { ["centerlines_reported"] = reported, ["unreadable"] = true };
                arrOut.Add(gap);
            }
            return arrOut;
        }
    }
}
