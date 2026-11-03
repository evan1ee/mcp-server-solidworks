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
    // SolidWorksService partial: automated drawing annotations (AutoDimensionDrawing, AutoCenterMarks, AddHoleCallout).
    public partial class SolidWorksService
    {
        // auto_dimension_drawing — automatically transfer the MODEL's driving dimensions into the drawing
        // views (the SolidWorks "Insert Model Items > Dimensions" automation). This is the robust
        // alternative to add_drawing_dimension's fragile coordinate pick (KNOWN-LIMITATIONS #6, the drawing
        // counterpart of part index-based selection): the dimensions come straight from the model's
        // parametric dimensions, correctly placed, instead of being guessed from a sheet coordinate.
        // Uses IDrawingDoc.InsertModelAnnotations3(Option, Types, AllViews, DuplicateDims, HiddenFeatureDims,
        // UsePlacementInSketch) — signature verified against the interop (IView.InsertModelDimensions does
        // NOT exist; the insertion API lives on IDrawingDoc). All swconst values are inlined int constants
        // (ADR-018). Bumps state_version (the drawing gains annotations); echoes inserted_count in
        // result_geometry for in-band verification (ADR-023 spirit) so the caller can confirm dims landed
        // without a separate analyze_drawing round-trip.
        public ExecutionResponse AutoDimensionDrawing(ToolRequest request)
        {
            return ExecuteTool(request, p =>
            {
                bool allViews = p?.Value<bool?>("all_views") ?? true;
                bool includeUnmarked = p?.Value<bool?>("include_unmarked") ?? false;
                bool eliminateDuplicates = p?.Value<bool?>("eliminate_duplicates") ?? true;

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var drawingDoc = modelDoc as IDrawingDoc;
                if (drawingDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NOT_A_DRAWING", "Active document is not a drawing. Call create_drawing + add_drawing_view first.");

                // InsertModelAnnotations3 args (swconst inlined as ints — ADR-018):
                //   Option = swImportModelItemsSource_e.swImportModelItemsFromEntireModel (0)
                //   Types  = swInsertAnnotation_e bitmask: swInsertDimensionsMarkedForDrawing (32768),
                //            optionally | swInsertDimensionsNotMarkedForDrawing (524288) for ALL driving dims
                //   AllViews, DuplicateDims, HiddenFeatureDims(false), UsePlacementInSketch(true)
                int option = 0;
                int types = 32768;
                if (includeUnmarked) types |= 524288;
                bool duplicateDims = !eliminateDuplicates;

                modelDoc.ClearSelection2(true);

                object inserted = drawingDoc.InsertModelAnnotations3(
                    option, types, allViews, duplicateDims, false, true);

                modelDoc.GraphicsRedraw2();

                int insertedCount = 0;
                var arr = inserted as object[];
                if (arr != null) insertedCount = arr.Length;

                ExecLog.Write($"auto_dimension_drawing: inserted {insertedCount} dimension(s) " +
                    $"allViews={allViews} includeUnmarked={includeUnmarked} types={types}");

                return BuildCompletedResponse(
                    request,
                    modelDoc,
                    "DRAWING",
                    resultGeometry: new JObject
                    {
                        ["inserted_count"] = insertedCount,
                        ["all_views"] = allViews,
                        ["include_unmarked"] = includeUnmarked
                    });
            });
        }

        // auto_center_marks — automatically place center marks (+ extended centerlines) on every hole/slot
        // in every model view of the active drawing (the SolidWorks "Auto Insert > Center Marks"). The
        // difficulty-2 forcing-function capability: a bracket/flange with holes needs center marks the
        // model-item auto-dimension pass doesn't add. ROBUST/automatic — `IView.AutoInsertCenterMarks2`
        // operates per view with NO coordinate pick (the drawing-side analogue of index-based selection,
        // ADR-033/034). All swconst values are inlined int constants (ADR-018). Bumps state_version; echoes
        // the total center-mark count in result_geometry (in-band verify, ADR-023 spirit).
        public ExecutionResponse AutoCenterMarks(ToolRequest request)
        {
            return ExecuteTool(request, p =>
            {
                bool includeSlots = p?.Value<bool?>("include_slots") ?? true;
                bool extendedLines = p?.Value<bool?>("extended_lines") ?? true;

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var drawingDoc = modelDoc as IDrawingDoc;
                if (drawingDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NOT_A_DRAWING", "Active document is not a drawing. Call create_drawing + add_drawing_view first.");

                // swAutoInsertCenterMarkTypes_e bitmask (inlined ints, ADR-018): Hole=1, Slots=4.
                int insertType = 1;
                if (includeSlots) insertType |= 4;
                int insertOption = 2; // swCenterMarkStyle_e.swCenterMark_Single

                int viewsProcessed = 0;
                int viewsMarked = 0;
                // Marks already present (the "auto insert center marks" document option adds them when
                // a view is created). AutoInsertCenterMarks2 does not skip those holes — it stacks a
                // second mark — so stacked copies are removed afterwards and the delta is what THIS
                // call actually added.
                int marksBefore = 0;
                var modelViews = new List<IView>();
                object viewObj = drawingDoc.GetFirstView(); // first view = the sheet
                int guardCounter = 0;
                while (viewObj != null && guardCounter++ < 2000)
                {
                    var view = viewObj as IView;
                    object next = (view != null) ? view.GetNextView() : null;
                    if (view != null)
                    {
                        int vType = -1;
                        try { vType = view.Type; } catch { } // swDrawingViewTypes_e: 1 = sheet
                        if (vType != 1)
                        {
                            modelViews.Add(view);
                            marksBefore += CountCenterMarks(view);
                            bool ok = false;
                            // Many drawing annotation ops act on the ACTIVE view — activate it first.
                            try { drawingDoc.ActivateView(view.Name); } catch { }
                            try
                            {
                                // AutoInsertCenterMarks2(InsertType, InsertOption, LinearSlotCenter,
                                //   ArcSlotCenter, UseDocumentDefaults, Size, Gap, ExtendedLines,
                                //   CenterLineFont, Angle). UseDocumentDefaults=true → SW's own center-mark
                                //   size/style (robust); the explicit args are then ignored by SW.
                                ok = view.AutoInsertCenterMarks2(insertType, insertOption,
                                    includeSlots, includeSlots, true, 0.0025, 0.0,
                                    extendedLines, true, 0.0);
                                viewsProcessed++;
                            }
                            catch (Exception exv) { ExecLog.Write($"auto_center_marks: view '{view.Name}' threw {exv.Message}"); }
                            if (ok) viewsMarked++;
                            ExecLog.Write($"auto_center_marks: view '{view.Name}' type={vType} ok={ok}");
                        }
                    }
                    viewObj = next;
                }

                // Commit so the inserted center marks/centerlines are realized (and so the per-view
                // counters below are not stale — this interop's GetCenterMarkCount can read 0 pre-rebuild).
                try { modelDoc.EditRebuild3(); } catch { }

                int duplicatesRemoved = 0;
                foreach (var v in modelViews)
                    duplicatesRemoved += RemoveStackedCenterMarks(modelDoc, v);
                if (duplicatesRemoved > 0)
                    try { modelDoc.EditRebuild3(); } catch { }
                modelDoc.GraphicsRedraw2();

                // Count by walking the annotation chains: GetCenterMarkCount/GetCenterLineCount read 0
                // here even though the marks exist (DXF had the SW_CENTERMARKSYMBOL blocks), whereas the
                // GetFirstCenterMark/GetNext walk — the same one analyze_slddrw_test reads — sees them.
                int totalMarks = 0, totalLines = 0, apiMarks = 0;
                foreach (var v in modelViews)
                {
                    totalMarks += CountCenterMarks(v);
                    totalLines += CountCenterLines(v);
                    try { apiMarks += v.GetCenterMarkCount(); } catch { }
                }
                int marksAdded = Math.Max(0, totalMarks - marksBefore);

                ExecLog.Write($"auto_center_marks: views_marked={viewsMarked}/{viewsProcessed} marks={totalMarks} " +
                    $"(before={marksBefore} added={marksAdded} duplicates_removed={duplicatesRemoved} " +
                    $"GetCenterMarkCount={apiMarks}) lines={totalLines} " +
                    $"includeSlots={includeSlots} extendedLines={extendedLines}");

                return BuildCompletedResponse(
                    request,
                    modelDoc,
                    "DRAWING",
                    resultGeometry: new JObject
                    {
                        ["views_marked"] = viewsMarked,
                        ["views_processed"] = viewsProcessed,
                        ["center_marks"] = totalMarks,          // marks now on the model views
                        ["center_marks_added"] = marksAdded,    // inserted by this call
                        ["duplicates_removed"] = duplicatesRemoved, // stacked marks deleted
                        ["center_lines"] = totalLines,
                        ["include_slots"] = includeSlots
                    });
            });
        }

        // Center marks in a view, one per marked hole: a grouped mark (pattern) counts its GroupCount.
        private static int CountCenterMarks(IView view)
        {
            int count = 0, guard = 0;
            object cmObj = null;
            try { cmObj = view.GetFirstCenterMark(); } catch { return 0; }
            while (cmObj is ICenterMark cm && guard++ < 500)
            {
                int members = 1;
                try { if (cm.IsGrouped) members = Math.Max(1, cm.GroupCount); } catch { }
                count += members;
                try { cmObj = cm.GetNext(); } catch { cmObj = null; }
            }
            return count;
        }

        // Delete center marks stacked on the same hole(s) as an earlier mark in the view's chain (the
        // first one — normally the pre-existing mark — is kept). Positions are view-space mark centres
        // (ICenterMark.GetPosition, see ReadCenterMarks). Returns the number deleted.
        private static int RemoveStackedCenterMarks(IModelDoc2 modelDoc, IView view)
        {
            var seen = new HashSet<string>();
            var stacked = new List<ICenterMark>();
            object cmObj = null;
            try { cmObj = view.GetFirstCenterMark(); } catch { return 0; }
            int guard = 0;
            while (cmObj is ICenterMark cm && guard++ < 500)
            {
                string key = CenterMarkKey(cm);
                if (key != null && !seen.Add(key)) stacked.Add(cm);
                try { cmObj = cm.GetNext(); } catch { cmObj = null; }
            }
            if (stacked.Count == 0) return 0;

            modelDoc.ClearSelection2(true);
            int selected = 0;
            foreach (var cm in stacked)
                try { if (cm.Select(true, null)) selected++; } catch { }
            if (selected > 0) modelDoc.EditDelete();
            modelDoc.ClearSelection2(true);
            ExecLog.Write($"auto_center_marks: view '{view.Name}' stacked={stacked.Count} deleted={selected}");
            return selected;
        }

        // Identity of a mark = its centre position(s) on a 1e-5 m grid (all members for a grouped mark).
        private static string CenterMarkKey(ICenterMark cm)
        {
            int members = 1;
            try { if (cm.IsGrouped) members = Math.Max(1, cm.GroupCount); } catch { }
            var parts = new List<string>();
            for (int i = 0; i < members && i < 100; i++)
            {
                double[] p = null;
                try { p = ToDoubleArray(cm.GetPosition(i)); } catch { }
                if (p == null || p.Length < 2) return null;
                parts.Add($"{Math.Round(p[0], 5):R},{Math.Round(p[1], 5):R}");
            }
            parts.Sort(StringComparer.Ordinal);
            return string.Join(";", parts);
        }

        // Centerline annotations in a view (the extended lines of a center mark are NOT separate
        // centerlines — they belong to the mark).
        private static int CountCenterLines(IView view)
        {
            int count = 0, guard = 0;
            object clObj = null;
            try { clObj = view.GetFirstCenterLine(); } catch { return 0; }
            while (clObj is ICenterLine cl && guard++ < 500)
            {
                count++;
                try { clObj = cl.GetNext(); } catch { clObj = null; }
            }
            return count;
        }

        // add_hole_callout — insert a hole callout (e.g. "n× ØD THRU") on the hole edge nearest the sheet
        // point (px, py) in the active drawing. Coordinate-based, mirroring add_drawing_dimension's EDGE
        // pick (the accepted #6 idiom; a drawing edge is selected by point). Uses
        // IDrawingDoc.AddHoleCallout2(X, Y, Z) — verified against the interop. Bumps state_version.
        // NOTE: coordinate selection is fragile for crowded geometry (KNOWN-LIMITATIONS #6); prefer
        // auto_center_marks for centerlines and use this for explicit hole callouts where wanted.
        public ExecutionResponse AddHoleCallout(ToolRequest request)
        {
            return ExecuteTool(request, p =>
            {
                double? px = p?.Value<double?>("px");
                double? py = p?.Value<double?>("py");
                if (px == null || py == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "px and py are required (a point on the hole edge in sheet meters).");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var drawingDoc = modelDoc as IDrawingDoc;
                if (drawingDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NOT_A_DRAWING", "Active document is not a drawing. Use create_drawing + add_drawing_view first.");

                // Select the hole's projected EDGE at the point (drawing views project the model as EDGE
                // entities; same pick order add_drawing_dimension uses). This also sets the owning view.
                modelDoc.ClearSelection2(true);
                bool selected = modelDoc.Extension.SelectByID2(
                    "", "EDGE", px.Value, py.Value, 0, false, 0, null, 0);
                if (!selected)
                    selected = modelDoc.Extension.SelectByID2(
                        "", "SILHOUETTE", px.Value, py.Value, 0, false, 0, null, 0);
                if (!selected)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "EDGE_NOT_FOUND", $"No drawing hole edge found at or near ({px.Value}, {py.Value}). Point at a projected circular edge in a view.");

                object callout = drawingDoc.AddHoleCallout2(px.Value, py.Value, 0.0);
                if (callout == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "CALLOUT_FAILED", "AddHoleCallout2 returned null — the selected edge may not be a hole/circular edge. Point at a hole's projected circle.");

                modelDoc.GraphicsRedraw2();

                return BuildCompletedResponse(
                    request,
                    modelDoc,
                    "DRAWING",
                    dimensions: new List<string> { $"hole_callout@({px.Value},{py.Value})" });
            });
        }
    }
}
