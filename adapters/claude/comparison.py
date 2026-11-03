"""Adapter-side part and assembly comparison pipelines (`compare_parts`, `compare_assemblies`)."""
import json
import os
from typing import Callable
from state_manager import analysis_items, delta_pct, kv_dict, open_or_activate_doc

_FACE_POS_TOL = 5e-5   # 50µm — supporting plane offset / point match
_FACE_RAD_TOL = 5e-5   # 50µm — cylinder radius match


def _face_cat(f: dict) -> str:
    if f.get("planar") and isinstance(f.get("normal"), list) and isinstance(f.get("point"), list):
        return "plane"
    if f.get("kind") == "cylinder" and isinstance(f.get("axis"), list):
        return "cyl"
    return f.get("kind", "surf")


def _plane_no(f: dict):
    """Unit normal (sign-canonicalized so the dominant axis is positive) + signed offset from origin."""
    n = [float(x) for x in f["normal"]]
    p = [float(x) for x in f["point"]]
    offset = n[0] * p[0] + n[1] * p[1] + n[2] * p[2]
    dom = max(range(3), key=lambda i: abs(n[i]))
    if n[dom] < 0:
        n = [-x for x in n]
        offset = -offset
    return n, offset


def _faces_compatible(fa: dict, fb: dict) -> bool:
    """True if two faces (from analyze_model(faces) JSON) are the SAME SURFACE within tolerance —
    matched by supporting geometry, not by trim/area. Planar → same unit normal (sign-canonicalized)
    AND same signed offset from origin (±50µm). Cylinder → parallel axis AND same radius (±50µm).
    Other → same kind AND representative point within 50µm."""
    ka, kb = _face_cat(fa), _face_cat(fb)
    if ka != kb:
        return False
    if ka == "plane":
        na, oa = _plane_no(fa)
        nb, ob = _plane_no(fb)
        dot = na[0] * nb[0] + na[1] * nb[1] + na[2] * nb[2]
        return dot > 0.999 and abs(oa - ob) <= _FACE_POS_TOL
    if ka == "cyl":
        aa = [abs(float(x)) for x in fa["axis"]]
        ab = [abs(float(x)) for x in fb["axis"]]
        parallel = sum(x * y for x, y in zip(aa, ab)) > 0.999
        return parallel and abs(float(fa.get("radius", 0)) - float(fb.get("radius", 0))) <= _FACE_RAD_TOL
    pa, pb = fa.get("point"), fb.get("point")
    if isinstance(pa, list) and isinstance(pb, list):
        return all(abs(float(x) - float(y)) <= _FACE_POS_TOL for x, y in zip(pa, pb))
    return False


def _diff_faces(faces_a: list, faces_b: list):
    """Greedy tolerance set-difference of two face lists. Returns (missing, extra)."""
    used_b = [False] * len(faces_b)
    missing = []
    for fa in faces_a:
        hit = -1
        for j, fb in enumerate(faces_b):
            if not used_b[j] and _faces_compatible(fa, fb):
                hit = j
                break
        if hit >= 0:
            used_b[hit] = True
        else:
            missing.append(fa)
    extra = [fb for j, fb in enumerate(faces_b) if not used_b[j]]
    return missing, extra


def _face_desc(f: dict) -> str:
    """Compact human description of a face for the diff report."""
    i = f.get("i")
    if f.get("planar"):
        n = f.get("normal")
        area = f.get("area")
        nstr = ("[" + ",".join(format(float(x), ".3g") for x in n) + "]") if isinstance(n, list) else "?"
        return f"i={i} plane n={nstr} area={format(float(area), '.4g') if isinstance(area, (int, float)) else '?'}"
    if f.get("kind") == "cylinder":
        return f"i={i} cyl r={format(float(f.get('radius', 0)), '.4g')}"
    return f"i={i} {f.get('kind', 'surf')}"


def compare_docs(doc_a: str, doc_b: str, want_faces: bool, call_raw: Callable) -> dict:
    """The measuring + judging half of compare_parts, as data (shared with convert_to_features).

    Returns {verified, topology_exact, topo_a, topo_b, dv, da, cg_dist, mass_a, mass_b,
    face_count_a, missing, extra}; raises RuntimeError('DOC_<A|B>_UNAVAILABLE | ...').
    """
    def _read(doc: str, label: str, want_faces_flag: bool):
        open_or_activate_doc(doc, label, call_raw)
        geo = kv_dict(analysis_items(call_raw("analyze_model", {"analysis_type": "geometry", "name": ""})))
        mass = kv_dict(analysis_items(call_raw("analyze_model", {"analysis_type": "mass_properties", "name": ""})))
        faces = None
        if want_faces_flag:
            items = analysis_items(call_raw("analyze_model", {"analysis_type": "faces", "name": ""}))
            faces = json.loads(items[0]).get("faces", []) if items else []
        return geo, mass, faces

    geo_a, mass_a, faces_a = _read(doc_a, "A", want_faces)
    geo_b, mass_b, faces_b = _read(doc_b, "B", want_faces)

    topo_keys = ("bodies", "faces", "edges", "vertices")
    topo_a = [geo_a.get(k) for k in topo_keys]
    topo_b = [geo_b.get(k) for k in topo_keys]
    topology_exact = topo_a == topo_b and None not in topo_a

    dv = delta_pct(mass_a.get("volume"), mass_b.get("volume"))
    da = delta_pct(mass_a.get("surface_area"), mass_b.get("surface_area"))
    cg_dist = None
    if all(isinstance(mass_x.get(k), (int, float)) for mass_x in (mass_a, mass_b) for k in ("cx", "cy", "cz")):
        cg_dist = ((mass_a["cx"] - mass_b["cx"]) ** 2 + (mass_a["cy"] - mass_b["cy"]) ** 2
                   + (mass_a["cz"] - mass_b["cz"]) ** 2) ** 0.5

    verified = (topology_exact and dv is not None and da is not None
                and abs(dv) <= 1.0 and abs(da) <= 1.0)
    missing, extra = _diff_faces(faces_a, faces_b) if want_faces else ([], [])
    return {"verified": verified, "topology_exact": topology_exact, "topo_a": topo_a, "topo_b": topo_b,
            "dv": dv, "da": da, "cg_dist": cg_dist, "mass_a": mass_a, "mass_b": mass_b,
            "face_count_a": len(faces_a or []), "missing": missing, "extra": extra}


def run_compare_parts(doc_a: str, doc_b: str, detail: str, compare_docs_fn: Callable) -> str:
    """Implementation behind the `compare_parts` MCP tool."""
    want_faces = detail == "faces"
    try:
        v = compare_docs_fn(doc_a, doc_b, want_faces)
    except RuntimeError as ex:
        return f"FAILED | {ex}"
    topo_a, topo_b, dv, da, cg_dist = v["topo_a"], v["topo_b"], v["dv"], v["da"], v["cg_dist"]
    mass_a, mass_b = v["mass_a"], v["mass_b"]
    verified, topology_exact = v["verified"], v["topology_exact"]

    fmt = lambda x, spec=".6g": ("?" if x is None else format(x, spec))  # noqa: E731
    line = (f"COMPLETED | verified_criteria={'PASS' if verified else 'FAIL'} | "
            f"topology A={'-'.join(str(t) for t in topo_a)} B={'-'.join(str(t) for t in topo_b)} "
            f"{'EXACT' if topology_exact else 'DIFFER'} | "
            f"volume A={fmt(mass_a.get('volume'))} B={fmt(mass_b.get('volume'))} dV={fmt(dv, '.4f')}% | "
            f"area A={fmt(mass_a.get('surface_area'))} B={fmt(mass_b.get('surface_area'))} dA={fmt(da, '.4f')}% | "
            f"cg_distance={fmt(cg_dist)} m | reference=A ({doc_a})")

    if want_faces:
        missing, extra = v["missing"], v["extra"]
        line += (f"\nfaces: matched={v['face_count_a'] - len(missing)} "
                 f"MISSING(in A only)={len(missing)} EXTRA(in B only)={len(extra)}")

        def _list(faces, cap=20):
            shown = "; ".join(_face_desc(f) for f in faces[:cap])
            if len(faces) > cap:
                shown += f"; …(+{len(faces) - cap} more)"
            return shown
        if missing:
            line += "\n  MISSING: " + _list(missing)
        if extra:
            line += "\n  EXTRA:   " + _list(extra)
    return line


def run_compare_assemblies(doc_a: str, doc_b: str, call_raw: Callable) -> str:
    """Implementation behind the `compare_assemblies` MCP tool."""
    def _read(doc: str, label: str):
        open_or_activate_doc(doc, label, call_raw)
        comps = json.loads(analysis_items(call_raw("analyze_assembly", {"analysis_type": "components"}))[0])
        mates = json.loads(analysis_items(call_raw("analyze_assembly", {"analysis_type": "mates"}))[0])
        mass = kv_dict(analysis_items(call_raw("analyze_model", {"analysis_type": "mass_properties", "name": ""})))
        return comps.get("components", []), mates.get("mates", []), mass

    try:
        comps_a, mates_a, mass_a = _read(doc_a, "A")
        comps_b, mates_b, mass_b = _read(doc_b, "B")
    except (RuntimeError, ValueError, IndexError) as ex:
        return f"FAILED | {ex}"

    def _key_seq(comps):
        counters: dict = {}
        out = []
        for c in comps:
            base = os.path.basename(c.get("path") or "").lower()
            cfg = c.get("config") or ""
            n = counters.get((base, cfg), 0) + 1
            counters[(base, cfg)] = n
            out.append(((base, cfg, n), c))
        return dict(out), sorted(k for k, _c in out)

    map_a, keys_a = _key_seq(comps_a)
    map_b, keys_b = _key_seq(comps_b)
    set_exact = keys_a == keys_b

    # Transform comparison over the paired components (rotation elements vs 1e-6; translation
    # meters vs 1µm). Only meaningful when the sets pair up.
    max_rot = max_pos = 0.0
    transforms_ok = set_exact
    if set_exact:
        for k in keys_a:
            ta, tb = map_a[k].get("transform"), map_b[k].get("transform")
            if not (isinstance(ta, list) and isinstance(tb, list) and len(ta) >= 12 and len(tb) >= 12):
                transforms_ok = False
                break
            max_rot = max(max_rot, max(abs(ta[i] - tb[i]) for i in range(9)))
            max_pos = max(max_pos, max(abs(ta[i] - tb[i]) for i in range(9, 12)))
        transforms_ok = transforms_ok and max_rot <= 1e-6 and max_pos <= 1e-6

    types_a = sorted(m.get("type") or "" for m in mates_a)
    types_b = sorted(m.get("type") or "" for m in mates_b)
    mates_ok = len(mates_a) == len(mates_b) and types_a == types_b

    dv = delta_pct(mass_a.get("volume"), mass_b.get("volume"))
    da = delta_pct(mass_a.get("surface_area"), mass_b.get("surface_area"))
    mass_ok = dv is not None and da is not None and abs(dv) <= 1.0 and abs(da) <= 1.0

    verified = set_exact and transforms_ok and mates_ok and mass_ok
    fmt = lambda v, spec=".6g": ("?" if v is None else format(v, spec))  # noqa: E731
    return (f"COMPLETED | verified_criteria={'PASS' if verified else 'FAIL'} | "
            f"components A={len(comps_a)} B={len(comps_b)} set={'EXACT' if set_exact else 'DIFFER'} | "
            f"transforms max_rot={fmt(max_rot, '.3g')} max_pos={fmt(max_pos, '.3g')} m "
            f"{'OK' if transforms_ok else 'FAIL'} | "
            f"mates A={len(mates_a)} B={len(mates_b)} types={'MATCH' if types_a == types_b else 'DIFFER'} | "
            f"volume dV={fmt(dv, '.4f')}% area dA={fmt(da, '.4f')}% | reference=A ({doc_a})")
