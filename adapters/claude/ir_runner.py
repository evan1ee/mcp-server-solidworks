"""Adapter-side Feature Graph IR execution doors (`submit_feature_graph`, `rebuild_from_ir`)."""
import hashlib
import json
import os
from typing import Callable


def run_graph(
    graph_obj,
    fresh_document: bool,
    call_raw: Callable,
    resync_state_version_fn: Callable[[], int],
    get_current_sv_fn: Callable[[], int],
    want_result: bool = False,
):
    """The ONE path from a Feature Graph to live geometry — shared by every IR door.

    IR-ADR-005 forbids FORKING the compiler, not having several doors into it. There are three
    (submit_feature_graph takes a graph, rebuild_from_ir replays an artifact's stored one,
    analyze_drawing(mode='build') lowers a drawing), and they all land here, so the document
    choice, the compiler entry point and the post-run resync cannot diverge between them.

    Returns (text, ok, state_version) — or, with want_result=True, (text, ok, state_version,
    CompileResult | None) for callers that react to WHICH node failed (convert_to_features).
    On failure `text` is already a complete FAILED line.
    """
    if fresh_document:
        # The graph type picks the document: an assembly graph (component/mate nodes) needs an
        # assembly, a part graph a part.
        nodes = graph_obj.get("nodes") or [] if isinstance(graph_obj, dict) else []
        is_assembly_graph = any(isinstance(n, dict) and n.get("type") in ("component", "mate")
                                for n in nodes)
        new_tool = "open_new_assembly" if is_assembly_graph else "open_new_part"
        opened = call_raw(new_tool, {})
        if opened.get("status") != "COMPLETED":
            err = opened.get("error") or {}
            text = f"FAILED | {new_tool.upper()}_FAILED | {err.get('code')}: {err.get('message')}"
            cur_sv = get_current_sv_fn()
            return (text, False, cur_sv, None) if want_result else (text, False, cur_sv)

    # Lazy import: a missing compiler tree degrades to a clean tool error instead of killing the
    # whole MCP server at startup.
    try:
        from ir_execution_port import run_feature_graph
    except Exception as ex:  # noqa: BLE001
        text = f"FAILED | COMPILER_UNAVAILABLE | {ex}"
        cur_sv = get_current_sv_fn()
        return (text, False, cur_sv, None) if want_result else (text, False, cur_sv)

    # NEVER let an exception crash the MCP server; resync state_version regardless (IR-ADR-001) —
    # one run performs MANY state-bumping sub-ops outside _call() (KNOWN-LIMITATIONS #5).
    try:
        result = run_feature_graph(graph_obj)
    except Exception as ex:  # noqa: BLE001
        sv = resync_state_version_fn()
        text = f"FAILED | UNEXPECTED | {type(ex).__name__}: {ex} | state_version resynced to {sv}"
        return (text, False, sv, None) if want_result else (text, False, sv)
    sv = resync_state_version_fn()
    return (result.summary(), True, sv, result) if want_result else (result.summary(), True, sv)


def run_rebuild_from_ir(artifact_path: str, fresh_document: bool, run_graph_fn: Callable) -> str:
    """Implementation behind the `rebuild_from_ir` MCP tool."""
    path = os.path.abspath(artifact_path)
    if not os.path.isfile(path):
        return f"FAILED | ARTIFACT_NOT_FOUND | {path}"
    try:
        # utf-8-sig: tolerate a BOM — artifacts hand-edited or written by other Windows tools
        # (e.g. PowerShell 5.1 Out-File) may carry one; save_analysis itself writes without.
        with open(path, "r", encoding="utf-8-sig") as fh:
            artifact = json.load(fh)
    except (ValueError, OSError) as ex:
        return f"FAILED | ARTIFACT_UNREADABLE | {ex}"

    ir = artifact.get("ir") or {}
    graph = ir.get("graph")
    if not isinstance(graph, dict) or not graph.get("nodes"):
        status = (ir.get("verification") or {}).get("status")
        reason = ((ir.get("verification") or {}).get("detail") or {}).get("reason")
        return (f"FAILED | NO_IR_GRAPH | the artifact carries no executable ir.graph"
                f"{f' (verification: {status} | {reason})' if status else ''} — run the AI/IR "
                f"pass per cad-planner/recipe.md first")

    # Stale-source signal (cache discipline, ADR-040): informative, not blocking — the graph
    # legitimately rebuilds the part AS-ANALYZED.
    source_stale = ""
    src = (artifact.get("identity") or {}).get("source_path")
    recorded = (artifact.get("identity") or {}).get("source_hash")
    if src and recorded and os.path.isfile(src):
        with open(src, "rb") as fh:
            current = "sha256:" + hashlib.sha256(fh.read()).hexdigest()
        if current != recorded:
            source_stale = " | source_stale=true (file changed since analysis — regenerate the artifact)"

    text, ok, _sv = run_graph_fn(graph, fresh_document)
    return text + (f" | artifact={os.path.basename(path)}{source_stale}" if ok else "")


def run_submit_feature_graph(graph: str, fresh_document: bool, run_graph_fn: Callable) -> str:
    """Implementation behind the `submit_feature_graph` MCP tool."""
    try:
        graph_obj = json.loads(graph)
    except Exception as ex:  # noqa: BLE001
        return f"FAILED | INVALID_JSON | the graph is not valid JSON: {ex}"

    text, ok, sv = run_graph_fn(graph_obj, fresh_document)
    return text + (f" | state_version={sv}" if ok else "")
