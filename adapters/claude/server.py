import inspect
import json
import os
import sys
from typing import Annotated, Literal, Optional
from pydantic import Field
from mcp.server import MCPServer
from mcp.server.caching import CacheHint
from execution_client import call_tool, get_state, ensure_ready as _ensure_ready
from response_mapper import map_response
# Some names below are imported only so that server.<name> keeps resolving for existing
# tests/callers (backward-compatible re-exports after the module split) - they are unused here.
from state_manager import (
    next_operation_id as _next_operation_id,
    is_state_mismatch as _is_state_mismatch,
    analysis_items as _analysis_items,
    kv_dict as _kv_dict,
)
from recipes import (
    _RECIPE_SECTIONS,
    _recipe_sections,
    _recipe_section_text,
    _contract_json,
    _feature_graph_schema_text,
    format_get_recipe as _format_get_recipe,
    format_recipe_index_resource as _format_recipe_index_resource,
)
from dimension_plan import plan_dimensions, execute_plan
from drawing_pipeline import (
    summarise_direct as _summarise_direct,
    advisories as _advisories,
    wire_json as _wire_json,
    format_direct_build_result as _format_direct_build_result,
)
from analysis_pipeline import (
    ANALYSIS_SCHEMA_VERSION,
    collect_parameters as _collect_parameters,
    save_assembly_analysis as _save_assembly_analysis_impl,
    run_save_analysis as _run_save_analysis,
)
from comparison import (
    compare_docs as _compare_docs_impl,
    run_compare_parts as _run_compare_parts,
    run_compare_assemblies as _run_compare_assemblies,
    _diff_faces,
    _face_desc,
    _faces_compatible,
)
from ir_runner import (
    run_graph as _run_graph_impl,
    run_rebuild_from_ir as _run_rebuild_from_ir,
    run_submit_feature_graph as _run_submit_feature_graph,
)
from tools.lifecycle import register_lifecycle_tools
from tools.sketch import register_sketch_tools
from tools.features import register_feature_tools
from tools.sheet_metal import register_sheet_metal_tools
from tools.assembly import register_assembly_tools
from tools.drawings import register_drawing_tools
from tools.inspection import register_inspection_tools, _draw
from tools.ir_doors import register_ir_doors
from tools.resources import register_resources
from feature_conversion import (
    _RecognitionView,
    recognize_brep as _recognize_brep_impl,
    _default_output_path,
    _is_history_less,
    _drop_failed,
    run_convert_to_features as _run_convert_to_features,
)
# NOTE: pycompiler is reached via `from ir_execution_port import run_feature_graph` imported
# LAZILY inside rebuild_from_ir and submit_feature_graph — a missing compiler tree degrades to a
# clean tool error instead of killing the whole MCP server at startup.

# P0.4 MCP hardening: discriminators are `Literal[...]` (invalid values rejected at the
# MCP schema level, before any REST/COM round-trip) and numeric params carry Pydantic
# `Field` constraints (units/ranges). Coordinates stay plain float (negative/zero are
# legitimate); open-ended selector strings (entity1_type, plane/material names) stay str
# so valid selectors are never rejected. Editing this file requires reconnecting the
# `solidworks` MCP server (no hot-reload — KNOWN-LIMITATIONS #4).

# Every tool is registered with `structured_output=False`. All 46 return a `str`, so the
# SDK's structured mirror is `{"result": "<the same string>"}` — the whole payload a second
# time, for zero added information (measured: exactly 2.00x on both SDKs). Switching it off
# halves the wire cost of every reader (the 33 KB `schema://feature-graph` read went over at
# 66 KB before this) and drops the per-tool `outputSchema` from the tools/list prompt. Restore
# it only for a tool that returns real structure, never for a `-> str` one.

# Cache hints for the resource surface (SEP-2549, protocol 2026-07-28). NOTE: `ttl_ms`/
# `cache_scope` are set PER METHOD at construction, NOT per resource — the high-level
# `@mcp.resource` decorator returns str/bytes, so an individual handler cannot carry its own TTL.
# One value therefore covers every resource. `scope="public"`: the recipe rules and contract
# schemas are byte-identical for every caller, with nothing authorization-dependent in them.
# ttl_ms=1h is deliberately shorter than it could be — recipe-usage.md is read FRESH from disk on
# every read so an edit goes live without a reconnect, and a long TTL would defeat that.
# MEASURED 2026-07-30, and it settles the matter: the host does NOT honor these hints. A marker
# appended to recipe-usage.md showed up in `recipe://usage/coverage` immediately, with no
# reconnect, on a URI already read minutes earlier in the same session — a TTL-honoring client
# would have served the stale copy. Good news (reads are genuinely fresh), but the caching win is
# ZERO. And no host setting can change the deeper limit: a cached body STILL enters the model's
# context on every read, so TTL caching can only save a round-trip, never tokens. The whole
# measured saving of this migration is the per-turn `tools/list` shrinkage. Hints stay declared —
# spec-correct, free, and a future host may use them.
_RECIPE_CACHE = CacheHint(ttl_ms=3_600_000, scope="public")  # 1 h
mcp = MCPServer(
    "solidworks-execution-adapter",
    cache_hints={
        "resources/read": _RECIPE_CACHE,
        "resources/list": _RECIPE_CACHE,
        "resources/templates/list": _RECIPE_CACHE,
    },
)

# Tracks state_version in memory. Starts at 0, updated after every response.
# Auto-resyncs from the execution layer on an INVALID_STATE_VERSION mismatch
# (e.g. the execution server was restarted after a rebuild).
_state_version: int = 0


def _update_state_version(response: dict) -> None:
    global _state_version
    sv = response.get("stateVersion")
    if sv is not None:
        _state_version = sv

# ---------------------------------------------------------------------------
# Execution & Orchestration Callables
# ---------------------------------------------------------------------------
try:
    import recognition as _recognition
    _RECOGNITION_AVAILABLE = True
    _RECOGNITION_IMPORT_ERROR = ""
except ImportError as _exc:
    _recognition = None
    _RECOGNITION_AVAILABLE = False
    _RECOGNITION_IMPORT_ERROR = str(_exc)


def _call_raw(tool_name: str, params: dict, timeout: Optional[float] = None) -> dict:
    """Like _call() but returns the RAW ExecutionResponse dict (no string mapping, no raise)."""
    global _state_version
    response = call_tool(tool_name, _next_operation_id(), _state_version, params, timeout=timeout)
    if _is_state_mismatch(response):
        _state_version = get_state()
        response = call_tool(tool_name, _next_operation_id(), _state_version, params, timeout=timeout)
    _update_state_version(response)
    return response


def _resync_state_version() -> int:
    """Realign the adapter's local state_version with the authoritative GET /state."""
    global _state_version
    try:
        _state_version = get_state()
    except Exception:
        pass
    return _state_version


def _run_graph(graph_obj, fresh_document, want_result=False):
    """The ONE path from a Feature Graph to live geometry — shared by every IR door."""
    return _run_graph_impl(
        graph_obj,
        fresh_document,
        _call_raw,
        _resync_state_version,
        lambda: _state_version,
        want_result=want_result,
    )


def _compare_docs(doc_a: str, doc_b: str, want_faces: bool) -> dict:
    return _compare_docs_impl(doc_a, doc_b, want_faces, _call_raw)


def _recognize_brep(brep: dict) -> _RecognitionView:
    return _recognize_brep_impl(brep, _recognition, _RECOGNITION_IMPORT_ERROR)






def _call(tool_name: str, params: dict) -> str:
    """Send a tool call to the execution layer.

    If the server reports an INVALID_STATE_VERSION mismatch (it was restarted and
    its state_version reset while this adapter kept its old value), fetch the
    authoritative state_version and retry once with a fresh operation_id. This
    removes the need to restart the adapter after every execution-layer rebuild.
    """
    global _state_version
    response = call_tool(tool_name, _next_operation_id(),
                         _state_version, params)
    if _is_state_mismatch(response):
        _state_version = get_state()
        response = call_tool(
            tool_name, _next_operation_id(), _state_version, params)
    _update_state_version(response)
    return map_response(response)


def _set_state_version(sv: int) -> None:
    global _state_version
    _state_version = sv


_lifecycle_tools = register_lifecycle_tools(mcp, _call, _ensure_ready, _set_state_version)
globals().update(_lifecycle_tools)
_sketch_tools = register_sketch_tools(mcp, _call)
globals().update(_sketch_tools)
_feature_tools = register_feature_tools(mcp, _call)
globals().update(_feature_tools)
_sheet_metal_tools = register_sheet_metal_tools(mcp, _call)
globals().update(_sheet_metal_tools)
_assembly_tools = register_assembly_tools(mcp, _call)
globals().update(_assembly_tools)
_drawing_tools = register_drawing_tools(mcp, _call)
globals().update(_drawing_tools)
_inspection_tools = register_inspection_tools(
    mcp,
    _call,
    lambda *a, **k: _call_raw(*a, **k),
    lambda *a, **k: _run_graph(*a, **k),
)
globals().update(_inspection_tools)



# ---------------------------------------------------------------------------
# IR Doors & MCP Resources Registration
# ---------------------------------------------------------------------------
_ir_tools = register_ir_doors(
    mcp,
    lambda *a, **k: _call_raw(*a, **k),
    lambda *a, **k: _run_graph(*a, **k),
    lambda *a, **k: _compare_docs(*a, **k),
    lambda *a, **k: _recognize_brep(*a, **k),
)
globals().update(_ir_tools)
register_resources(mcp)


# Tool-surface normalisation (prompt cost)
# ---------------------------------------------------------------------------
def _normalize_tool_surface() -> None:
    """Undo three SDK artefacts that ride in the tools/list prompt on every turn.

    1. Docstrings keep their source indentation (the SDK does not dedent them).
    2. Every parameter gets an auto-generated `title` that just re-spells its own name.
    3. `additionalProperties` is absent, so a typo'd param name is not rejected by the
       schema — the P0.4 stance is to reject at the MCP layer, before any REST/COM
       round-trip, so put it back.

    `Tool.description` / `Tool.parameters` are plain mutable fields that `list_tools()`
    reads at call time, so one pass after registration is enough. Without this the payload
    is ~9% larger than the FastMCP surface it replaced; with it, ~2% smaller.

    Touches `mcp._tool_manager` (private). The public alternative is the `middleware`
    hook, which the SDK marks provisional — revisit if this breaks on an SDK bump; the
    schema-contract test is what catches it.
    """
    for tool in mcp._tool_manager.list_tools():
        if tool.description:
            tool.description = inspect.cleandoc(tool.description)
        schema = tool.parameters
        if isinstance(schema, dict):
            schema.pop("title", None)
            for prop in (schema.get("properties") or {}).values():
                if isinstance(prop, dict):
                    prop.pop("title", None)
            schema.setdefault("additionalProperties", False)


_normalize_tool_surface()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run()
