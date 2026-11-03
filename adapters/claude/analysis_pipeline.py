"""Adapter-side analysis artifact pipeline (`save_analysis` for .SLDPRT and .SLDASM)."""
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Callable
from state_manager import analysis_items, kv_dict

ANALYSIS_SCHEMA_VERSION = "0.1.0-draft"  # cad-planner/contracts/analysis-artifact.schema.json


def collect_parameters(node, out: list, feature_name: str = "") -> None:
    """Walk the features recipe and lift dimension entries into the flat named-parameter table.

    A dimension entry is a dict whose 'name' is a FULL dimension name (contains '@', e.g.
    'D1@Boss-Extrude1@Part1.Part' — the modify_dimension target) with a numeric value under
    'value_si' or 'value'. Tolerant by design: the exact recipe keys are C#-owned; anything that
    doesn't match the shape is simply not lifted.
    """
    if isinstance(node, dict):
        owner = node.get("name") if node.get("type") else None
        value = node.get("value_si", node.get("value"))
        name = node.get("name")
        if isinstance(name, str) and "@" in name and isinstance(value, (int, float)):
            out.append({"name": name, "value_si": value, "feature": feature_name})
            return
        for child in node.values():
            collect_parameters(child, out, owner or feature_name)
    elif isinstance(node, list):
        for child in node:
            collect_parameters(child, out, feature_name)


def save_assembly_analysis(src: str, sha: str, call_raw: Callable) -> str:
    """The .SLDASM branch of save_analysis (Phase B): components (tree order, full transforms)
    + mates (creation order, enum types, entity anchors) + assembly mass properties. Component
    source files get their own sha256 in relationships so staleness is loud per part
    (ADR-047b: the assembly references part FILES — their artifacts live separately)."""
    try:
        comp_items = analysis_items(call_raw("analyze_assembly", {"analysis_type": "components"}))
        mate_items = analysis_items(call_raw("analyze_assembly", {"analysis_type": "mates"}))
        mass_kv = kv_dict(analysis_items(call_raw("analyze_model", {"analysis_type": "mass_properties", "name": ""})))
    except RuntimeError as ex:
        return f"FAILED | ANALYZE_FAILED | {ex}"
    try:
        components = json.loads(comp_items[0]) if comp_items else {}
        mates = json.loads(mate_items[0]) if mate_items else {}
    except (ValueError, TypeError) as ex:
        return f"FAILED | PAYLOAD_UNPARSEABLE | {ex}"

    part_files = []
    seen_paths = set()
    for c in components.get("components", []):
        p = c.get("path")
        if not p or p in seen_paths:
            continue
        seen_paths.add(p)
        entry = {"path": p}
        if os.path.isfile(p):
            with open(p, "rb") as fh:
                entry["hash"] = "sha256:" + hashlib.sha256(fh.read()).hexdigest()
        else:
            entry["missing"] = True
        part_files.append(entry)

    artifact = {
        "identity": {
            "source_path": src,
            "source_filename": os.path.basename(src),
            "source_hash": "sha256:" + sha,
            "source_mtime": datetime.fromtimestamp(os.path.getmtime(src), timezone.utc).isoformat(),
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "generator": {"kind": "deterministic"},
        },
        "document_type": "assembly",
        "recipe": {
            "components": components,
            "mates": mates,
            "mass_properties": {
                "volume_m3": mass_kv.get("volume"),
                "surface_area_m2": mass_kv.get("surface_area"),
                "cg": {"x": mass_kv.get("cx"), "y": mass_kv.get("cy"), "z": mass_kv.get("cz")},
            },
        },
        "parameters": [],
        "ir": None,
        "relationships": {"part_files": part_files, "category": None, "cluster_signals": {}},
        "notes": [],
    }

    out_dir = os.path.join(os.path.dirname(src), ".solidpilot")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, os.path.basename(src) + ".analysis.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2)

    return (f"COMPLETED | artifact={out_path} | components={components.get('component_count')} | "
            f"mates={mates.get('mate_count')} | part_files={len(part_files)} | hash=sha256:{sha[:12]}")


def run_save_analysis(file_path: str, call_raw: Callable) -> str:
    """Implementation behind the `save_analysis` MCP tool."""
    src = os.path.abspath(file_path)
    if not os.path.isfile(src):
        return f"FAILED | FILE_NOT_FOUND | {src}"
    is_assembly = src.lower().endswith(".sldasm")
    if not src.lower().endswith(".sldprt") and not is_assembly:
        return ("FAILED | UNSUPPORTED_TYPE | save_analysis analyzes .SLDPRT parts and .SLDASM "
                "assemblies (drawing artifacts come with later pipeline steps)")
    with open(src, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()

    # Open (or activate, if it is already open under its title).
    opened = call_raw("open_document", {"file_path": src})
    if opened.get("status") == "FAILED":
        activated = call_raw("activate_document", {"title": os.path.basename(src)})
        if activated.get("status") == "FAILED":
            err = opened.get("error") or {}
            return f"FAILED | OPEN_FAILED | {err.get('code')}: {err.get('message')}"

    if is_assembly:
        return save_assembly_analysis(src, sha, call_raw)

    try:
        features_items = analysis_items(call_raw("analyze_model", {"analysis_type": "features", "name": ""}))
        mass_kv = kv_dict(analysis_items(call_raw("analyze_model", {"analysis_type": "mass_properties", "name": ""})))
        geometry = kv_dict(analysis_items(call_raw("analyze_model", {"analysis_type": "geometry", "name": ""})))
    except RuntimeError as ex:
        return f"FAILED | ANALYZE_FAILED | {ex}"

    notes = []
    if not features_items:
        return "FAILED | NO_PAYLOAD | analyze_model(features) returned an empty payload"
    try:
        features = json.loads(features_items[0])
    except (ValueError, TypeError):
        features = {"raw": features_items}
        notes.append("features payload was not parseable JSON — stored raw (reader refinement pending)")

    mass = {
        "volume_m3": mass_kv.get("volume"),
        "surface_area_m2": mass_kv.get("surface_area"),
        "cg": {"x": mass_kv.get("cx"), "y": mass_kv.get("cy"), "z": mass_kv.get("cz")},
    }

    parameters: list = []
    collect_parameters(features, parameters)

    # V1 relationships: drawing files next to the part whose stem is the part's stem, optionally
    # followed by a suffix word (e.g. 'part-1 drawing.SLDDRW' for 'part-1.SLDPRT').
    folder = os.path.dirname(src)
    stem = os.path.splitext(os.path.basename(src))[0].lower()
    drawings = sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.lower().endswith(".slddrw")
        and (os.path.splitext(f)[0].lower() == stem
             or os.path.splitext(f)[0].lower().startswith(stem + " "))
    )

    artifact = {
        "identity": {
            "source_path": src,
            "source_filename": os.path.basename(src),
            "source_hash": "sha256:" + sha,
            "source_mtime": datetime.fromtimestamp(os.path.getmtime(src), timezone.utc).isoformat(),
            "schema_version": ANALYSIS_SCHEMA_VERSION,
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "generator": {"kind": "deterministic"},
        },
        "document_type": "part",
        "recipe": {
            "features": features,
            "mass_properties": mass,
            "geometry": geometry,
            "bbox": None,
            "material": None,
            "equations": [],
        },
        "parameters": parameters,
        "ir": None,
        "relationships": {"drawings": drawings, "category": None, "cluster_signals": {}},
        "notes": notes + ["bbox/material/equations extraction pending refinement (A2 live pass)"],
    }

    out_dir = os.path.join(folder, ".solidpilot")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, os.path.basename(src) + ".analysis.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2)

    geo = geometry if isinstance(geometry, dict) else {}
    feature_count = len(features.get("features", [])) if isinstance(features, dict) else "?"
    return (f"COMPLETED | artifact={out_path} | features={feature_count} | "
            f"parameters={len(parameters)} | geometry={{bodies:{geo.get('bodies')},"
            f"faces:{geo.get('faces')},edges:{geo.get('edges')},vertices:{geo.get('vertices')}}} | "
            f"drawings_linked={len(drawings)} | hash=sha256:{sha[:12]}")
