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
    // SolidWorksService partial: two-pick drawing dimensions (AddDrawingDimensionEx).
    public partial class SolidWorksService
    {
        private const double DimensionValueToleranceM = 1e-6;

        // add_drawing_dimension_ex — select one or two projected edges by sheet point, create a
        // horizontal / vertical / aligned / auto smart dimension, and VERIFY the measured value.
        // Unlike add_drawing_dimension (single pick, write-back of a read-only SystemValue), the measured
        // value is read back and compared with expected_value; a mismatch deletes the dimension and fails.
        // Optional: arc_condition (center|min|max) for circle picks, prefix text (e.g. "4×"), precision.
        public ExecutionResponse AddDrawingDimensionEx(ToolRequest request)
        {
            return ExecuteTool(request, p =>
            {
                double? x1 = p?.Value<double?>("x1");
                double? y1 = p?.Value<double?>("y1");
                double? x2 = p?.Value<double?>("x2");
                double? y2 = p?.Value<double?>("y2");
                double? tx = p?.Value<double?>("text_x");
                double? ty = p?.Value<double?>("text_y");
                string orientation = (p?.Value<string>("orientation") ?? "auto").ToLowerInvariant();
                string arcCondition = (p?.Value<string>("arc_condition") ?? "center").ToLowerInvariant();
                double? expected = p?.Value<double?>("expected_value");
                string prefix = p?.Value<string>("prefix");
                string suffix = p?.Value<string>("suffix");
                int precision = p?.Value<int?>("precision") ?? -1;
                bool measureOnly = p?.Value<bool?>("measure_only") ?? false;

                if (x1 == null || y1 == null || tx == null || ty == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "x1, y1, text_x and text_y are required.");
                if ((x2 == null) != (y2 == null))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "INVALID_PARAMETER", "x2 and y2 must be given together.");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");
                if (!(modelDoc is IDrawingDoc))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NOT_A_DRAWING", "Active document is not a drawing.");

                modelDoc.ClearSelection2(true);
                if (!SelectDrawingEdge(modelDoc, x1.Value, y1.Value, false))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SEGMENT_NOT_FOUND", $"No drawing edge at ({x1.Value}, {y1.Value}).");
                if (x2 != null && !SelectDrawingEdge(modelDoc, x2.Value, y2.Value, true))
                {
                    modelDoc.ClearSelection2(true);
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "SEGMENT_NOT_FOUND", $"No drawing edge at ({x2.Value}, {y2.Value}).");
                }

                bool prevInput = _solidWorks.GetUserPreferenceToggle((int)swUserPreferenceToggle_e.swInputDimValOnCreate);
                _solidWorks.SetUserPreferenceToggle((int)swUserPreferenceToggle_e.swInputDimValOnCreate, false);
                object created;
                try
                {
                    if (orientation == "horizontal")
                        created = modelDoc.AddHorizontalDimension2(tx.Value, ty.Value, 0);
                    else if (orientation == "vertical")
                        created = modelDoc.AddVerticalDimension2(tx.Value, ty.Value, 0);
                    else
                        created = modelDoc.AddDimension2(tx.Value, ty.Value, 0);
                }
                finally
                {
                    _solidWorks.SetUserPreferenceToggle((int)swUserPreferenceToggle_e.swInputDimValOnCreate, prevInput);
                }

                var dispDim = created as IDisplayDimension;
                if (dispDim == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DIMENSION_CREATION_FAILED", "SolidWorks returned no dimension for the selected pair.");

                var dim = dispDim.GetDimension2(0) as IDimension;
                if (dim != null && x2 != null)
                    ApplyArcCondition(dim, arcCondition);

                if (precision >= 0)
                    dispDim.SetPrecision3(precision, 0, 0, 0);
                if (!string.IsNullOrEmpty(prefix))
                    dispDim.SetText(1, prefix); // swDimensionTextPrefix
                if (!string.IsNullOrEmpty(suffix))
                    dispDim.SetText(2, suffix); // swDimensionTextSuffix

                double measured = ReadDimensionValue(dim);
                if (expected != null && Math.Abs(measured - expected.Value) > DimensionValueToleranceM)
                {
                    DeleteDisplayDimension(modelDoc, dispDim);
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DIMENSION_VALUE_MISMATCH",
                        $"Measured {measured:G9} m, expected {expected.Value:G9} m; dimension removed. " +
                        "Check the pick points land on the intended edges (and arc_condition for circles).");
                }

                string text = dispDim.GetText(0);
                string name = dim?.FullName;
                JArray textPos = ReadAnnotationPosition(dispDim);
                if (measureOnly)
                {
                    // Verified, now removed: the value is reported but nothing stays on the sheet.
                    DeleteDisplayDimension(modelDoc, dispDim);
                    name = null;
                    textPos = null;
                }

                modelDoc.GraphicsRedraw2();
                return BuildCompletedResponse(
                    request, modelDoc, "DRAWING",
                    dimensions: new List<string> { $"value={measured:G9}" },
                    resultGeometry: new JObject
                    {
                        ["measured_value"] = measured,
                        ["text"] = text,
                        ["name"] = name,
                        ["text_pos"] = textPos,
                        ["measure_only"] = measureOnly
                    });
            });
        }

        // move_drawing_dimension — reposition an existing drawing dimension's text (the dimension and
        // extension lines follow) by its full name (e.g. "D1@Drawing View1", as returned by
        // add_drawing_dimension_ex). Used by the post-placement crossing fix: swap nested tiers.
        public ExecutionResponse MoveDrawingDimension(ToolRequest request)
        {
            return ExecuteTool(request, p =>
            {
                string name = p?.Value<string>("name");
                double? x = p?.Value<double?>("x");
                double? y = p?.Value<double?>("y");
                if (string.IsNullOrEmpty(name) || x == null || y == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MISSING_PARAMETER", "name, x and y are required.");

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null || !(modelDoc is IDrawingDoc))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NOT_A_DRAWING", "Active document is not a drawing.");

                modelDoc.ClearSelection2(true);
                if (!modelDoc.Extension.SelectByID2(name, "DIMENSION", 0, 0, 0, false, 0, null, 0))
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DIMENSION_NOT_FOUND", $"No drawing dimension named '{name}'.");

                var sel = (ISelectionMgr)modelDoc.SelectionManager;
                var dispDim = sel.GetSelectedObject6(1, -1) as IDisplayDimension;
                var ann = dispDim?.GetAnnotation() as IAnnotation;
                if (ann == null)
                {
                    modelDoc.ClearSelection2(true);
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "DIMENSION_NOT_FOUND", $"'{name}' is not a dimension annotation.");
                }

                var before = ReadAnnotationPosition(dispDim);
                bool ok = ann.SetPosition2(x.Value, y.Value, 0);
                modelDoc.ClearSelection2(true);
                if (!ok)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "MOVE_FAILED", "IAnnotation.SetPosition2 returned false.");

                modelDoc.GraphicsRedraw2();
                return BuildCompletedResponse(
                    request, modelDoc, "DRAWING",
                    resultGeometry: new JObject
                    {
                        ["name"] = name,
                        ["before"] = before,
                        ["after"] = ReadAnnotationPosition(dispDim)
                    });
            });
        }

        private static JArray ReadAnnotationPosition(IDisplayDimension dispDim)
        {
            var ann = dispDim?.GetAnnotation() as IAnnotation;
            var pos = ann?.GetPosition() as double[];
            return pos == null ? null : new JArray(pos[0], pos[1], pos[2]);
        }

        private static bool SelectDrawingEdge(IModelDoc2 doc, double x, double y, bool append)
        {
            foreach (string type in new[] { "EDGE", "SILHOUETTE", "SKETCHSEGMENT" })
                if (doc.Extension.SelectByID2("", type, x, y, 0, append, 0, null, 0))
                    return true;
            return false;
        }

        // swArcEndConditions_e values (center=1, min=2, max=3) decoded against a live drawing.
        private static void ApplyArcCondition(IDimension dim, string condition)
        {
            int cond = condition == "min" ? 2 : condition == "max" ? 3 : 1;
            try { dim.SetArcEndCondition(1, cond); dim.SetArcEndCondition(2, cond); }
            catch (COMException) { /* not an arc/circle pick — nothing to set */ }
        }

        private static double ReadDimensionValue(IDimension dim)
        {
            if (dim == null) return double.NaN;
            var v = dim.GetSystemValue3((int)swInConfigurationOpts_e.swThisConfiguration, null) as double[];
            return v != null && v.Length > 0 ? v[0] : dim.SystemValue;
        }

        private static void DeleteDisplayDimension(IModelDoc2 doc, IDisplayDimension dispDim)
        {
            doc.ClearSelection2(true);
            var ann = dispDim.GetAnnotation() as IAnnotation;
            if (ann != null && ann.Select3(false, null))
                doc.Extension.DeleteSelection2(2); // swDelete_Absorbed
        }
    }
}
