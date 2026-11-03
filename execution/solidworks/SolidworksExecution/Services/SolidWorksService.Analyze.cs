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
    // SolidWorksService partial: read-only part analysis (analyze_model modes, selection readback, feature/sketch readers, topology snapshot/diff for feature_map).
    public partial class SolidWorksService
    {

        // -----------------------------------------------------------------------
        // M16 — analyze_model
        // -----------------------------------------------------------------------
        public ExecutionResponse AnalyzeModel(ToolRequest request)
        {
            return ExecuteTool(request, p =>
            {
                string analysisType = (p?.Value<string>("analysis_type") ?? "mass_properties").ToLowerInvariant();

                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var results = new List<string>();

                if (analysisType == "mass_properties")
                {
                    // IModelDoc2.GetMassProperties2 returns double[]:
                    // [0]=CG.x, [1]=CG.y, [2]=CG.z, [3]=volume, [4]=surface_area, [5]=mass, [6+]=moments.
                    // (Verified empirically against a 50mm cube: [0.025,0.025,0.025,0.000125,0.015,...].)
                    int massStatus = 0;
                    object rawProps = modelDoc.GetMassProperties2(ref massStatus);
                    double[] props = rawProps as double[];
                    if (props == null && rawProps is object[] objArr)
                        props = System.Array.ConvertAll(objArr, o => (double)o);

                    if (props == null || props.Length < 5)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "ANALYSIS_FAILED",
                            "GetMassProperties2 returned null or insufficient data. Ensure the document has at least one solid body.");

                    // GetMassProperties2 layout: [0]=cx, [1]=cy, [2]=cz, [3]=volume, [4]=surface_area
                    results.Add($"volume={props[3]:G6}");
                    results.Add($"surface_area={props[4]:G6}");
                    results.Add($"cx={props[0]:G6}");
                    results.Add($"cy={props[1]:G6}");
                    results.Add($"cz={props[2]:G6}");
                }
                else if (analysisType == "geometry")
                {
                    var partDoc = modelDoc as IPartDoc;
                    if (partDoc == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "WRONG_DOCUMENT_TYPE",
                            "analysis_type='geometry' requires a part document.");

                    object[] bodies = partDoc.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[];
                    int bodyCount = bodies != null ? bodies.Length : 0;
                    int totalFaces = 0, totalEdges = 0, totalVerts = 0;

                    if (bodies != null)
                    {
                        foreach (var b in bodies)
                        {
                            var body = b as IBody2;
                            if (body == null) continue;
                            totalFaces += body.GetFaceCount();
                            totalEdges += body.GetEdgeCount();
                            totalVerts += body.GetVertexCount();
                        }
                    }

                    results.Add($"bodies={bodyCount}");
                    results.Add($"faces={totalFaces}");
                    results.Add($"edges={totalEdges}");
                    results.Add($"vertices={totalVerts}");
                }
                else if (analysisType == "bodies")
                {
                    // Per-BODY fingerprint (multibody parts — e.g. a FLATTENED assembly STEP):
                    // volume/area/centroid + the body's FACE INDEX RANGE in the same enumeration
                    // ReadFaces walks, so a caller can segment analyze_model(faces) per body and
                    // match bodies to known parts (assembly reconstruction, Phase B 1-3 class).
                    var partDoc = modelDoc as IPartDoc;
                    if (partDoc == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "WRONG_DOCUMENT_TYPE", "analysis_type='bodies' requires a part document.");
                    var root = new JObject();
                    var arr = new JArray();
                    int faceCursor = 0, bidx = 0;
                    object[] bodies = partDoc.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[];
                    if (bodies != null)
                    {
                        foreach (var b in bodies)
                        {
                            var body = b as IBody2;
                            if (body == null) continue;
                            var bj = new JObject();
                            bj["i"] = bidx++;
                            try
                            {
                                // Imported breps often carry the source product name (Fusion/NX
                                // exports) — free naming for body->part extraction.
                                var bn = body.Name;
                                if (!string.IsNullOrEmpty(bn)) bj["name"] = bn;
                            }
                            catch { }
                            int fc = body.GetFaceCount();
                            bj["face_start"] = faceCursor;
                            bj["face_count"] = fc;
                            faceCursor += fc;
                            bj["edges"] = body.GetEdgeCount();
                            bj["vertices"] = body.GetVertexCount();
                            try
                            {
                                // IBody2.GetMassProperties(density=1): [0..2]=centroid, [3]=volume,
                                // [4]=area (layout sanity-checked live against document totals).
                                var mp = body.GetMassProperties(1.0) as double[];
                                if (mp == null && body.GetMassProperties(1.0) is object[] oa2)
                                    mp = Array.ConvertAll(oa2, o => (double)o);
                                if (mp != null && mp.Length >= 5)
                                {
                                    bj["centroid"] = new JArray { Math.Round(mp[0], 9), Math.Round(mp[1], 9), Math.Round(mp[2], 9) };
                                    bj["volume"] = mp[3];
                                    bj["area"] = mp[4];
                                }
                            }
                            catch { }
                            arr.Add(bj);
                        }
                    }
                    root["body_count"] = arr.Count;
                    root["bodies"] = arr;
                    results.Add(root.ToString(Newtonsoft.Json.Formatting.None));
                }
                else if (analysisType == "edges")
                {
                    // Edge list with start/end/MIDPOINT 3D coords — so a caller can pick an edge to
                    // select (e.g. edge_flange's ex/ey/ez = an edge midpoint) WITHOUT blind-guessing
                    // coordinates or reverse-deriving the extrude direction from mass_properties.
                    // Batch-1 Task-3: OPTIONAL near/k/axis narrow this to the k nearest edges to a point
                    // (with an optional direction filter) — a filtered READ, not the full dump. A
                    // parameterless call is byte-for-byte the historical full dump (backward compatible).
                    var partDoc = modelDoc as IPartDoc;
                    if (partDoc == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "WRONG_DOCUMENT_TYPE",
                            "analysis_type='edges' requires a part document.");
                    double[] near = ParseVec3(p?.Value<string>("near"));
                    double[] axis = ParseVec3(p?.Value<string>("axis"));
                    int k = p?.Value<int?>("k") ?? 0;
                    JObject edgeAnalysis = ReadEdges(partDoc, near, k, axis);
                    results.Add(edgeAnalysis.ToString(Newtonsoft.Json.Formatting.None));
                }
                else if (analysisType == "faces")
                {
                    // Planar-face list with centroid/normal/area + a stable index — the twin of 'edges'
                    // (ADR-027). Lets a caller pick a face to sketch on (create_sketch on_face + face_index)
                    // WITHOUT a coordinate pick, which is fragile on a revolve end-cap (the face centroid can
                    // sit on the revolve axis/origin → ambiguous SelectByID2). Baby reference-resolver → P1.3.
                    // Batch-1 Task-3: OPTIONAL near/k/axis narrow this to the k nearest faces to a point (with
                    // an optional normal filter). Parameterless call = the historical full dump (compatible).
                    var partDoc = modelDoc as IPartDoc;
                    if (partDoc == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "WRONG_DOCUMENT_TYPE",
                            "analysis_type='faces' requires a part document.");
                    double[] near = ParseVec3(p?.Value<string>("near"));
                    double[] axis = ParseVec3(p?.Value<string>("axis"));
                    int k = p?.Value<int?>("k") ?? 0;
                    JObject faceAnalysis = ReadFaces(partDoc, near, k, axis);
                    results.Add(faceAnalysis.ToString(Newtonsoft.Json.Formatting.None));
                }
                else if (analysisType == "brep")
                {
                    // Full B-rep of a single solid body (faces + surface params + loops, edges + curve
                    // data + adjacency + convexity) — the STEP→features recognizer's input. Read-only.
                    var partDoc = modelDoc as IPartDoc;
                    if (partDoc == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "WRONG_DOCUMENT_TYPE",
                            "analysis_type='brep' requires a part document.");
                    results.Add(ReadBrep(partDoc).ToString(Newtonsoft.Json.Formatting.None));
                }
                else if (analysisType == "features")
                {
                    // Feature-tree read for the "analyze -> build a variant" loop. Read-only.
                    // Packs a single consolidated JSON string into Features (the only field the
                    // adapter's response_mapper surfaces to the host). A COMPACT recipe: the ordered
                    // feature tree (name/type, suppressed only when true), each feature's driving
                    // display-dimensions (deduped, rounded SI), a SKETCH SUMMARY (counts + an inline
                    // circle for single-full-circle profiles — NO per-segment coordinate dump),
                    // pattern semantics, and equations / globals. For one sketch's exact geometry use
                    // analysis_type='sketch'.
                    JObject analysis = BuildFeatureTreeAnalysis(modelDoc);
                    results.Add(analysis.ToString(Newtonsoft.Json.Formatting.None));
                }
                else if (analysisType == "sketch")
                {
                    // Detail mode: ONE sketch's full geometry (every segment, rounded), on demand — so
                    // the common 'features' recipe stays cheap and you pay for coordinates only where
                    // you actually need them. Find the sketch feature by its exact tree name.
                    string sketchName = p?.Value<string>("name");
                    if (string.IsNullOrEmpty(sketchName))
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "MISSING_PARAMETER",
                            "analysis_type='sketch' requires 'name' (sketch feature name, e.g. 'Sketch2'). " +
                            "Call analysis_type='features' first to list sketch names.");

                    var allNames = new List<string>();
                    var sketchFeat = FindFeatureByName(modelDoc, sketchName, allNames);
                    if (sketchFeat == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "FEATURE_NOT_FOUND",
                            $"No feature named '{sketchName}'. Available: {string.Join(", ", allNames)}");

                    var theSketch = sketchFeat.GetSpecificFeature2() as ISketch;
                    if (theSketch == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "WRONG_FEATURE_TYPE",
                            $"Feature '{sketchName}' is not a sketch (type '{sketchFeat.GetTypeName2()}'). " +
                            "Pass a sketch/ProfileFeature name.");

                    var sObj = new JObject();
                    sObj["name"] = sketchFeat.Name;
                    sObj["sketch"] = ReadSketch(theSketch, true); // FULL geometry, rounded
                    var pl = ReadSketchPlane(theSketch);
                    if (pl != null) sObj["plane"] = pl;
                    results.Add(sObj.ToString(Newtonsoft.Json.Formatting.None));
                }
                else if (analysisType == "feature_map")
                {
                    // Per-feature geometry ATTRIBUTION via the rollback bar: walk the tree base→end and
                    // for each solid-affecting feature diff the topology against the previous stop — all
                    // INSIDE this call (one compact JSON out, no per-stop raw dumps to the host).
                    // consumed_edges = the deterministic fillet/chamfer edge anchors (they reference the
                    // PRE-feature geometry, exactly IR-ADR-008's provenance rule). Non-destructive: the
                    // bar is restored to the END in finally; nothing is saved; no state_version bump.
                    var partDoc = modelDoc as IPartDoc;
                    if (partDoc == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            "WRONG_DOCUMENT_TYPE",
                            "analysis_type='feature_map' requires a part document.");

                    string fromFeature = p?.Value<string>("from_feature");
                    string toFeature = p?.Value<string>("to_feature");
                    string errCode, errMsg;
                    JObject mapResult = BuildFeatureMap(modelDoc, partDoc, fromFeature, toFeature,
                        out errCode, out errMsg);
                    if (mapResult == null)
                        return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                            errCode ?? "FEATURE_MAP_FAILED", errMsg ?? "feature_map failed.");
                    results.Add(mapResult.ToString(Newtonsoft.Json.Formatting.None));
                }
                else
                {
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "UNSUPPORTED_ANALYSIS_TYPE",
                        $"analysis_type '{analysisType}' is not supported. Supported: mass_properties, geometry, bodies, edges, faces, features, sketch, feature_map.");
                }

                // AnalyzeModel does NOT increment state_version — read-only, same pattern as VerifyState
                return BuildCompletedResponse(
                    request,
                    modelDoc,
                    "PART",
                    incrementStateVersion: false,
                    registerCompleted: false,
                    features: results);
            });
        }

        // get_selection — report what the USER currently has selected in the SolidWorks GUI, mapped to the
        // SAME stable indices analyze_model(faces/edges) reports. The INVERSE of index-based selection
        // (ADR-033): instead of the AI telling SW "select face i", the human picks in the GUI and the AI
        // reads back which face/edge index it was — so a user can point at geometry mid-conversation and the
        // AI acts on it (face → create_sketch(on_face, face_index); edge → add_edge_feature(edge_indices)).
        // Read-only: does NOT clear the selection (would wipe the user's pick) and does NOT bump
        // state_version (same pattern as AnalyzeModel/VerifyState). Baby reference-resolver → feeds P1.3.
        public ExecutionResponse GetSelection(ToolRequest request)
        {
            return ExecuteTool(request, _ =>
            {
                var modelDoc = _solidWorks.IActiveDoc2 as IModelDoc2;
                if (modelDoc == null)
                    return BuildFailed(request.OperationId, _guard.GetCurrentStateVersion(),
                        "NO_ACTIVE_DOCUMENT", "No active document found in SolidWorks.");

                var selMgr = modelDoc.SelectionManager as ISelectionMgr;
                int count = selMgr?.GetSelectedObjectCount2(-1) ?? 0;

                // Enumerate part faces/edges LAZILY — only when a face/edge is actually in the selection — so
                // a vertex/plane/empty selection costs no body-walk. Same enumeration analyze_model uses, so
                // the matched index is the one create_sketch(face_index)/add_edge_feature(edge_indices) expect.
                var partDoc = modelDoc as IPartDoc;
                List<IFace2> allFaces = null;
                List<IEdge> allEdges = null;

                var selArr = new JArray();
                for (int s = 1; s <= count; s++)
                {
                    // GetSelectedObjectType3 → swSelectType_e (inlined ints, ADR-018): 1=EDGES, 2=FACES,
                    // 3=VERTICES, 4=DATUMPLANES. (Never cast to the swconst enum TYPE → would load swconst.dll.)
                    int t = selMgr.GetSelectedObjectType3(s, -1);
                    object obj = selMgr.GetSelectedObject6(s, -1);
                    JObject so;
                    if (t == 2 && obj is IFace2 selFace)
                    {
                        if (allFaces == null) allFaces = FlattenFaces(partDoc);
                        int i = IndexByJson(allFaces, BuildFaceJson(selFace, -1), BuildFaceJson);
                        so = BuildFaceJson(selFace, i);
                        so["type"] = "face";
                    }
                    else if (t == 1 && obj is IEdge selEdge)
                    {
                        if (allEdges == null) allEdges = FlattenEdges(partDoc);
                        int i = IndexByJson(allEdges, BuildEdgeJson(selEdge, -1), BuildEdgeJson);
                        so = BuildEdgeJson(selEdge, i);
                        so["type"] = "edge";
                    }
                    else if (t == 3 && obj is IVertex selVert)
                    {
                        so = new JObject { ["type"] = "vertex" };
                        var pt = selVert.GetPoint() as double[];
                        if (pt != null && pt.Length >= 3)
                            so["point"] = new JArray { R6(pt[0]), R6(pt[1]), R6(pt[2]) };
                    }
                    else if (t == 4)
                    {
                        so = new JObject { ["type"] = "plane", ["name"] = (obj as IFeature)?.Name };
                    }
                    else
                    {
                        so = new JObject { ["type"] = "other", ["sw_type"] = t };
                    }
                    selArr.Add(so);
                }

                var root = new JObject();
                root["selected_count"] = count;
                root["selection"] = selArr;

                // Read-only, same response shape as AnalyzeModel — does NOT bump state_version, no MarkComplete.
                return BuildCompletedResponse(
                    request,
                    modelDoc,
                    "PART",
                    incrementStateVersion: false,
                    registerCompleted: false,
                    activeSketch: (modelDoc.SketchManager.ActiveSketch as IFeature)?.Name,
                    features: new List<string> { root.ToString(Newtonsoft.Json.Formatting.None) });
            });
        }

        // Flatten all solid-body faces / edges in the SAME order analyze_model walks (GetBodies2(swSolidBody,
        // true) → GetFaces()/GetEdges()), so a list index here equals the `i` analyze_model reports.
        private List<IFace2> FlattenFaces(IPartDoc partDoc)
        {
            var list = new List<IFace2>();
            object[] bodies = partDoc?.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[];
            if (bodies != null)
                foreach (var b in bodies)
                {
                    var body = b as IBody2;
                    if (body == null) continue;
                    object[] fs = body.GetFaces() as object[];
                    if (fs == null) continue;
                    foreach (var f in fs) { var fa = f as IFace2; if (fa != null) list.Add(fa); }
                }
            return list;
        }

        private List<IEdge> FlattenEdges(IPartDoc partDoc)
        {
            var list = new List<IEdge>();
            object[] bodies = partDoc?.GetBodies2((int)swBodyType_e.swSolidBody, true) as object[];
            if (bodies != null)
                foreach (var b in bodies)
                {
                    var body = b as IBody2;
                    if (body == null) continue;
                    object[] es = body.GetEdges() as object[];
                    if (es == null) continue;
                    foreach (var e in es) { var ed = e as IEdge; if (ed != null) list.Add(ed); }
                }
            return list;
        }

        // Match a selected entity to its index in the enumerated list by GEOMETRY: build each candidate's
        // JSON (idx -1, so the `i` field doesn't interfere) and DeepEquals it against the selected entity's
        // JSON. Reuses the SAME BuildFaceJson/BuildEdgeJson readers, so the match key is exactly the reported
        // geometry. (We can't use IEntity.IsSameAs — not exposed by this interop version — and RCW reference
        // equality is unreliable across separate COM calls.) Collisions need two geometrically identical
        // entities, which a valid solid doesn't have. -1 if no match.
        private static int IndexByJson<T>(List<T> items, JObject target, Func<T, int, JObject> build)
        {
            for (int i = 0; i < items.Count; i++)
                if (JToken.DeepEquals(build(items[i], -1), target)) return i;
            return -1;
        }

    }
}
