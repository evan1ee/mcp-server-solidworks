using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Newtonsoft.Json.Linq;
using SolidWorks.Interop.sldworks;
using SolidworksExecution.Infrastructure;

namespace SolidworksExecution.Services
{
    // analyze_model(analysis_type='brep'): the full boundary representation of a SINGLE solid body,
    // read-only — the input of the STEP→features recognizer (cad-planner/recognition). Face/edge `i`
    // follow the SAME GetBodies2(swSolidBody,true) → GetFaces()/GetEdges() order as the 'faces' and
    // 'edges' modes, and each edge's `mid` is BuildEdgeJson's (the resolver matches anchors on it).
    //
    // COM layouts used here (interop reflected 2026-11-01, decoded live on a box+hole):
    //   ICurve.LineParams    [0..2]=root, [3..5]=direction
    //   ICurve.CircleParams  [0..2]=centre, [3..5]=axis, [6]=radius
    //   ISurface.CylinderParams [0..2]=origin, [3..5]=axis, [6]=radius
    //   ISurface.ConeParams  [0..2]=origin, [3..5]=axis, [6]=radius at origin, [7]=half angle (rad)
    //   ISurface.SphereParams [0..2]=centre, [3]=radius
    //   ISurface.TorusParams [0..2]=centre, [3..5]=axis, [6]=major radius, [7]=minor radius
    //   IEdge.GetCurveParams2 [0..2]=start, [3..5]=end, [6]=uStart, [7]=uEnd
    //   ICurve.Evaluate2(t,1) [0..2]=point, [3..5]=first derivative
    // Outward normals of curved faces are computed GEOMETRICALLY from those parameters (pointing away
    // from the axis/centre) and flipped when IFace2.FaceInSurfaceSense() is true.
    public partial class SolidWorksService
    {
        private const double ConvexityTol = 1e-6;

        // Every COM call here is a cross-process round trip (~10 ms, more when it marshals new proxies),
        // so the read is shaped to minimise calls: surface data is read ONCE per face (FaceGeo) and reused
        // by every edge's convexity test, and face LOOPS are NOT walked (coedge→edge proxies cost ~0.4 s
        // per face) — the recognizer rebuilds them from each edge's adjacent faces + endpoints.
        private JObject ReadBrep(IPartDoc partDoc)
        {
            var root = new JObject();
            object[] bodies = partDoc.GetBodies2(0 /* swSolidBody */, true) as object[];
            int bodyCount = bodies?.Length ?? 0;
            root["body_count"] = bodyCount;
            if (bodyCount != 1) return root;

            var sw = System.Diagnostics.Stopwatch.StartNew();
            var body = (IBody2)bodies[0];
            var box = body.GetBodyBox() as double[];
            if (box != null && box.Length >= 6)
                root["bbox"] = new JArray { R6(box[0]), R6(box[1]), R6(box[2]), R6(box[3]), R6(box[4]), R6(box[5]) };

            // Single body ⇒ its GetFaces()/GetEdges() order IS the 'faces'/'edges' modes' enumeration.
            var faces = new List<IFace2>();
            foreach (var f in (body.GetFaces() as object[]) ?? new object[0]) if (f is IFace2 fa) faces.Add(fa);
            var edges = new List<IEdge>();
            foreach (var e in (body.GetEdges() as object[]) ?? new object[0]) if (e is IEdge ed) edges.Add(ed);
            var faceIndex = new Dictionary<IntPtr, int>();
            for (int i = 0; i < faces.Count; i++) faceIndex[ComKey(faces[i])] = i;

            var geo = new FaceGeo[faces.Count];
            var faceArr = new JArray();
            for (int i = 0; i < faces.Count; i++)
            {
                geo[i] = new FaceGeo(faces[i]);
                faceArr.Add(BuildBrepFace(faces[i], geo[i], i));
            }
            root["faces"] = faceArr;

            var edgeArr = new JArray();
            for (int i = 0; i < edges.Count; i++)
                edgeArr.Add(BuildBrepEdge(edges[i], i, geo, faceIndex));
            root["edges"] = edgeArr;
            ExecLog.Write($"ReadBrep: faces={faces.Count} edges={edges.Count} total_ms={sw.ElapsedMilliseconds}");
            return root;
        }

        // Per-face surface data read once: type, raw params and (planes) the normal.
        private sealed class FaceGeo
        {
            public readonly IFace2 Face;
            public readonly string Type;
            public readonly double[] Params;      // the surface-type's params array (see header layouts)
            public readonly double[] PlaneNormal; // planes only
            public readonly bool Reversed;        // FaceInSurfaceSense()

            public FaceGeo(IFace2 face)
            {
                Face = face;
                var s = face.IGetSurface();
                Type = SurfaceType(s);
                Reversed = face.FaceInSurfaceSense();
                if (Type == "plane")
                {
                    var n = face.Normal as double[];
                    if (n != null && n.Length >= 3) PlaneNormal = Unit(new[] { n[0], n[1], n[2] });
                }
                else if (s != null)
                {
                    Params = Type == "cylinder" ? SolidWorksService.Params(s.CylinderParams)
                           : Type == "cone" ? SolidWorksService.Params(s.ConeParams)
                           : Type == "torus" ? SolidWorksService.Params(s.TorusParams)
                           : Type == "sphere" ? SolidWorksService.Params(s.SphereParams) : null;
                }
            }

            public double[] NormalAt(double[] p)
            {
                return Type == "plane" ? PlaneNormal : CurvedOutwardNormal(Type, Params, Reversed, p);
            }
        }

        // COM identity: the same SolidWorks entity reached by two calls yields the same IUnknown pointer.
        private static IntPtr ComKey(object o)
        {
            IntPtr p = Marshal.GetIUnknownForObject(o);
            Marshal.Release(p);
            return p;
        }

        private JObject BuildBrepFace(IFace2 face, FaceGeo g, int idx)
        {
            var fj = new JObject { ["i"] = idx };
            try { fj["area"] = R6(face.GetArea()); } catch { }
            fj["type"] = g.Type;
            double[] p = FacePoint(face, face.IGetSurface());
            if (p != null) fj["point"] = V3(p);
            AddSurfaceParams(fj, g);
            if (p != null)
            {
                double[] n = g.NormalAt(p);
                if (n != null) fj["normal"] = V3(n);
            }
            return fj;
        }

        private static string SurfaceType(ISurface s)
        {
            if (s == null) return "other";
            if (s.IsPlane()) return "plane";
            if (s.IsCylinder()) return "cylinder";
            if (s.IsCone()) return "cone";
            if (s.IsSphere()) return "sphere";
            if (s.IsTorus()) return "torus";
            return "other";
        }

        private static double[] FacePoint(IFace2 face, ISurface surf)
        {
            var uv = face.GetUVBounds() as double[];
            if (surf == null || uv == null || uv.Length < 4) return null;
            var pt = surf.Evaluate((uv[0] + uv[1]) / 2.0, (uv[2] + uv[3]) / 2.0, 0, 0) as double[];
            return pt != null && pt.Length >= 3 ? new[] { pt[0], pt[1], pt[2] } : null;
        }

        private static double[] Params(object o)
        {
            if (o is double[] d) return d;
            if (o is object[] oa) return Array.ConvertAll(oa, x => Convert.ToDouble(x));
            return null;
        }

        private static void AddSurfaceParams(JObject fj, FaceGeo g)
        {
            double[] q = g.Params;
            if (q == null) return;
            switch (g.Type)
            {
                case "cylinder":
                    if (q.Length >= 7) { fj["origin"] = V3(q, 0); fj["axis"] = V3(q, 3); fj["radius"] = R6(q[6]); }
                    break;
                case "cone":
                    if (q.Length >= 8) { fj["origin"] = V3(q, 0); fj["axis"] = V3(q, 3); fj["radius"] = R6(q[6]); fj["half_angle"] = R6(q[7]); }
                    break;
                case "sphere":
                    if (q.Length >= 4) { fj["origin"] = V3(q, 0); fj["radius"] = R6(q[3]); }
                    break;
                case "torus":
                    if (q.Length >= 8) { fj["origin"] = V3(q, 0); fj["axis"] = V3(q, 3); fj["radius"] = R6(q[6]); fj["minor_radius"] = R6(q[7]); }
                    break;
            }
        }

        // Surface normal pointing AWAY from the axis/centre at p (pure math on the cached params — no COM),
        // flipped when the face runs against its surface sense (FaceInSurfaceSense).
        private static double[] CurvedOutwardNormal(string type, double[] q, bool reversed, double[] p)
        {
            if (q == null) return null;
            double[] n = null;
            if (type == "sphere")
            {
                if (q.Length >= 4) n = Unit(new[] { p[0] - q[0], p[1] - q[1], p[2] - q[2] });
            }
            else if (type == "cylinder" || type == "cone" || type == "torus")
            {
                if (q.Length < 7) return null;
                double[] o = { q[0], q[1], q[2] }, a = Unit(new[] { q[3], q[4], q[5] });
                double[] d = { p[0] - o[0], p[1] - o[1], p[2] - o[2] };
                double t = Dot(d, a);
                double[] radial = { d[0] - t * a[0], d[1] - t * a[1], d[2] - t * a[2] };
                double[] ru = Unit(radial);
                if (ru == null) return null;
                if (type == "torus")
                {
                    // Point minus the tube centre (on the major circle) — the torus' true normal.
                    double major = q[6];
                    double[] c = { o[0] + t * a[0] + major * ru[0], o[1] + t * a[1] + major * ru[1], o[2] + t * a[2] + major * ru[2] };
                    n = Unit(new[] { p[0] - c[0], p[1] - c[1], p[2] - c[2] });
                }
                else if (type == "cone")
                {
                    // SolidWorks cones NARROW along +axis (decoded live 2026-11-01: r(t) = r0 − t·tan α),
                    // so the away-from-axis normal is ∝ radial + tan α · axis. The slant matters: with the
                    // radial direction alone a 45° chamfer cone reads TANGENT to its cylinder.
                    if (q.Length < 8) return null;
                    double ta = Math.Tan(q[7]);
                    n = Unit(new[] { ru[0] + ta * a[0], ru[1] + ta * a[1], ru[2] + ta * a[2] });
                }
                else n = ru;
            }
            if (n == null) return null;
            if (reversed) { n[0] = -n[0]; n[1] = -n[1]; n[2] = -n[2]; }
            return n;
        }

        private JObject BuildBrepEdge(IEdge edge, int idx, FaceGeo[] geo, Dictionary<IntPtr, int> faceIndex)
        {
            JObject ej = BuildEdgeJson(edge, idx);   // i/start/end/mid(+length) — resolver-identical
            var curve = edge.IGetCurve();
            var cp = edge.GetCurveParams2() as double[];
            string type = "other";
            if (curve != null)
            {
                if (curve.IsLine()) type = "line";
                else if (curve.IsCircle())
                {
                    type = "circle";
                    var q = Params(curve.CircleParams);
                    if (q != null && q.Length >= 7) { ej["center"] = V3(q, 0); ej["axis"] = V3(q, 3); ej["radius"] = R6(q[6]); }
                }
                if (cp != null && cp.Length >= 8 && ej["length"] == null)
                    try { ej["length"] = R6(curve.GetLength3(cp[6], cp[7])); } catch { }
            }
            ej["type"] = type;

            var adj = edge.GetTwoAdjacentFaces2() as object[];
            var fa = adj != null && adj.Length > 0 ? adj[0] as IFace2 : null;
            var fb = adj != null && adj.Length > 1 ? adj[1] as IFace2 : null;
            int ia = fa != null && faceIndex.TryGetValue(ComKey(fa), out int x) ? x : -1;
            int ib = fb != null && faceIndex.TryGetValue(ComKey(fb), out int y) ? y : -1;
            ej["faces"] = new JArray { ia, ib };
            if (cp != null && cp.Length >= 8)
            {
                // ONE Evaluate2 at the parametric midpoint serves both pmid and convexity.
                var ev = edge.Evaluate2((cp[6] + cp[7]) / 2.0, 1) as double[];
                if (ev != null && ev.Length >= 6)
                {
                    // pmid: ON-curve point at the parametric midpoint (unlike `mid`, the chord midpoint,
                    // which sits at the centre of a half circle) — fixes an arc's side/sweep sense.
                    ej["pmid"] = V3(ev);
                    if (ia >= 0 && ib >= 0)
                        ej["convexity"] = Convexity(ev, geo[ia], geo[ib]);
                }
            }
            return ej;
        }

        // Convexity at the edge's parametric midpoint p, with outward normals nA, nB. dB is the in-surface
        // direction from the edge INTO face B: ±(nB × t), the sign chosen by PROBING — step ProbeStep along
        // the candidate and keep it if face B's closest point is (nearly) the probe itself. (A coedge-sense
        // derivation proved unreliable live: the loop→face identity lookup silently missed on some arcs.)
        // nA·dB < 0 ⇒ B falls away below A's tangent plane ⇒ convex; > 0 ⇒ concave; ~0 ⇒ tangent.
        private const double ProbeStep = 1e-4;

        private static string Convexity(double[] ev, FaceGeo a, FaceGeo b)
        {
            double[] pt = { ev[0], ev[1], ev[2] };
            double[] tangent = Unit(new[] { ev[3], ev[4], ev[5] });
            double[] nA = a.NormalAt(pt), nB = b.NormalAt(pt);
            if (tangent == null || nA == null || nB == null) return "unknown";
            double[] dB = Unit(Cross(nB, tangent));
            if (dB == null) return "unknown";
            if (!ProbeLandsOnFace(b.Face, pt, dB)) dB = new[] { -dB[0], -dB[1], -dB[2] };
            double s = Dot(nA, dB);
            if (s < -ConvexityTol) return "convex";
            if (s > ConvexityTol) return "concave";
            return "tangent";
        }

        private static bool ProbeLandsOnFace(IFace2 face, double[] p, double[] d)
        {
            double[] q = { p[0] + ProbeStep * d[0], p[1] + ProbeStep * d[1], p[2] + ProbeStep * d[2] };
            var c = face.GetClosestPointOn(q[0], q[1], q[2]) as double[];
            if (c == null || c.Length < 3) return false;
            double dx = c[0] - q[0], dy = c[1] - q[1], dz = c[2] - q[2];
            return Math.Sqrt(dx * dx + dy * dy + dz * dz) < ProbeStep * 0.5;
        }

        private static JArray V3(double[] v, int o = 0) { return new JArray { R6(v[o]), R6(v[o + 1]), R6(v[o + 2]) }; }
        private static double Dot(double[] a, double[] b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
        private static double[] Cross(double[] a, double[] b)
        {
            return new[] { a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0] };
        }
        private static double[] Unit(double[] v)
        {
            double l = Math.Sqrt(Dot(v, v));
            return l < 1e-12 ? null : new[] { v[0] / l, v[1] / l, v[2] / l };
        }
    }
}
