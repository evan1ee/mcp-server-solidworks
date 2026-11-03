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
                modelDoc.GraphicsRedraw2();

                // views_marked (the API success count) is the RELIABLE in-band signal that center marks
                // were auto-inserted; the raw mark/line counts are best-effort (this interop under-reports
                // them — verified visually that marks ARE present even when these read 0).
                int totalMarks = 0, totalLines = 0;
                foreach (var v in modelViews)
                {
                    try { totalMarks += v.GetCenterMarkCount(); } catch { }
                    try { totalLines += v.GetCenterLineCount(); } catch { }
                }

                ExecLog.Write($"auto_center_marks: views_marked={viewsMarked}/{viewsProcessed} marks={totalMarks} lines={totalLines} " +
                    $"includeSlots={includeSlots} extendedLines={extendedLines}");

                return BuildCompletedResponse(
                    request,
                    modelDoc,
                    "DRAWING",
                    resultGeometry: new JObject
                    {
                        ["views_marked"] = viewsMarked,
                        ["views_processed"] = viewsProcessed,
                        ["center_marks"] = totalMarks,   // best-effort (interop under-reports; marks ARE present)
                        ["center_lines"] = totalLines,   // best-effort
                        ["include_slots"] = includeSlots
                    });
            });
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
