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
_inspection_tools = register_inspection_tools(mcp, _call, _call_raw, _run_graph)
globals().update(_inspection_tools)


# ---------------------------------------------------------------------------
# RESOURCE surface: the IR-generation recipe + the two cad-planner contract JSONs
#
# These were the `get_recipe(section=...)` tool until 2026-07-30 (ADR-069). They are now MCP
# *resources*: the content is large, near-static, identical for every caller and re-read
# constantly — exactly what `resources/read` + the 2026-07-28 `ttlMs`/`cacheScope` hints are for.
# MEASURED reason to move (not estimated): `get_recipe` cost 2,369 chars of `tools/list` on EVERY
# turn (2.97% of the 47-tool surface; 1,928 of it the docstring), paid whether or not any recipe
# was read. A resource read costs ~50 chars MORE than the equivalent tool result (the
# `uri`+`mimeType` wrapper), so the win is entirely the per-turn surface — which is why the tool
# had to GO, not sit beside the resources.
#
# Shape decided with the user (2026-07-30):
#   - EVERY section is its own STATIC resource. Probe finding: templated URIs are readable but
#     appear in NO listing (`resources/list` shows static entries only), so a template-only
#     surface would be undiscoverable. Static-per-section makes ONE `resources/list` the manifest.
#   - `recipe://usage/{section}` stays as a TEMPLATE ALIAS so a slug guessed from the index
#     still resolves. Concrete resources win over templates in the SDK's resolution order, so
#     the static entries above are never shadowed by it.
#   - Version lives in the BODY only (`Version: 0.21.0`), never in the URI: bumps must not
#     break the URI pointers embedded in the tool docstrings below.
#   - The FROZEN `slddrw-testing-recipe-usage.md` pair (ADR-063) is deliberately NOT served —
#     it stays a repo file; port from it consciously if a rule there is wanted again.
# The recipe's PRIVATE full edition (`recipe.md`) is NEVER served — it carries lesson history and
# provenance, is gitignored, and only the PUBLIC `-usage` twin appears on this surface.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Tool: get_recipe  (RESTORED 2026-09-03, ADR-077 — reverses ADR-069's retirement)
# ---------------------------------------------------------------------------
# ADR-069 moved the recipe from a TOOL to 13 MCP resources on a measured saving of 2,146 chars of
# `tools/list` per turn. The measurement was right; the assumption underneath it was not. MCP has
# three primitives and only ONE of them is in the MODEL's action space:
#
#     tool      tools/call       <- the model can call this
#     resource  resources/read   <- the CLIENT calls this; the model needs a host-provided bridge
#     prompt    prompts/get      <- the user invokes this
#
# ADR-069 probed the host and found it DID bridge resources for the model, so the migration looked
# safe. That bridge is gone: neither this host nor the one running the isolated tests exposes any
# `ReadMcpResource`-style tool, so the recipe left the model's action space entirely. Two isolated
# reconstructions of f-3 both reported they could not read `recipe://usage/reverse` and proceeded
# on their own judgement — which is exactly the "a resource no host reads is WORSE than a tool"
# risk ADR-069 named and accepted. A server cannot push a resource at a model; the only handle we
# can put in a model's hand is a TOOL. So the tool comes back.
#
# The RESOURCES STAY. They are spec-correct, the user can attach them by hand, and a host that
# does bridge them still benefits. Both channels read the same `recipe-usage.md` on every call, so
# there is exactly one copy of the rules and no sync burden.
@mcp.tool(structured_output=False)
def get_recipe(
    section: Literal["index", "contract", "canonicalization", "forward", "mapping",
                     "mapping_part", "mapping_sheet_metal", "mapping_assembly", "verification",
                     "reverse", "coverage", "feature_graph_schema",
                     "analysis_artifact_schema"] = "index",
    profile: Literal["all", "part"] = "all",
) -> str:
    """The IR-generation recipe — REQUIRED READING before writing any Feature Graph IR
    (an artifact's `ir.graph`), reconstructing a part from a drawing, or producing a drawing
    meant for reconstruction. Serves the rules section-by-section so token cost stays
    proportional to the task.

    Call order for the ARTIFACT→IR flow: 'contract' + 'canonicalization' + 'mapping' first,
    then the vocabulary section matching the document ('mapping_part' / 'mapping_sheet_metal' /
    'mapping_assembly'), then 'verification' before labeling anything. 'forward' holds the
    INTENT→IR authoring discipline (grammar cheat-sheet, anchor design without an original,
    computed-expectation self-verification) — read it FIRST when writing a graph for
    submit_feature_graph from design intent. 'reverse' holds the DXF/DWG drawing→part
    reconstruction discipline (scale/DIMLFAC, edge classes, view clustering, projection
    standard, contour tiers, sheet-metal bend arithmetic) — read it FIRST when rebuilding a part
    from a 2D drawing, before you interpret a single number of `analyze_drawing`'s answer.

    section='index' (default): the version header + a one-line table of contents.
    section='feature_graph_schema': the Feature Graph IR schema / capability registry JSON —
        the node types and params the compiler accepts (what is NOT in it cannot be built).
        `profile='part'` drops the ASSEMBLY vocabulary from the answer (~2.4 KB) and is what a
        part or drawing→part job wants; every feature type stays, sheet metal included, because a
        drawing is not known to be sheet metal until you decide it (R21). `profile='all'`
        (default) is the whole registry.
    section='analysis_artifact_schema': the persistent analysis-artifact contract JSON
        (identity/hash, recipe, parameters, ir block + the formal 'verified' definition).
    The same rules are also published as `recipe://usage/*` MCP resources; use whichever your
    host actually lets you reach. Read-only; does NOT touch SolidWorks or state_version."""
    # Resource references preserved for contract visibility:
    # schema://feature-graph, schema://analysis-artifact
    return _format_get_recipe(section, profile)


@mcp.resource("recipe://usage/index", name="recipe_index", mime_type="text/markdown",
              description="START HERE for IR work: the recipe version header + every section URI. "
                          "Rules for turning an analysis artifact or a 2D drawing into a Feature "
                          "Graph IR.")
def _recipe_index() -> str:
    """The table of contents, generated from the file so it can never drift from what is served."""
    return _format_recipe_index_resource()


def _register_recipe_sections() -> None:
    """Register one STATIC resource per served section.

    Static (not template-only) because a templated URI appears in NO listing — `resources/list`
    carries concrete resources only — so this is what makes the rule set DISCOVERABLE: one
    `resources/list` call returns every section with its title as the description.

    Titles are snapshotted at import for the descriptions (the listing metadata is fixed at
    registration time); the BODIES stay fresh per read. A missing/unreadable recipe-usage.md
    degrades to a generic description instead of killing the MCP server at startup — the same
    stance as the lazily-imported compiler.
    """
    try:
        _header, sections = _recipe_sections()
    except OSError:
        sections = {}
    for slug in _RECIPE_SECTIONS:
        title = (sections.get(slug) or ("",))[0]
        reader = (lambda s: lambda: _recipe_section_text(s))(slug)
        reader.__name__ = f"_recipe_{slug}"
        mcp.resource(
            f"recipe://usage/{slug}",
            name=f"recipe_{slug}",
            mime_type="text/markdown",
            description=(f"IR recipe — {title}" if title
                         else f"IR recipe section '{slug}' (see recipe://usage/index)"),
        )(reader)


_register_recipe_sections()


@mcp.resource("recipe://usage/{section}", name="recipe_section", mime_type="text/markdown",
              description="Alias: any recipe-usage.md section by slug. The per-section URIs above "
                          "are the discoverable form; this only catches a slug read off the index.")
def _recipe_section_alias(section: str) -> str:
    return _recipe_section_text(section)


@mcp.resource("schema://feature-graph", name="feature_graph_schema",
              mime_type="application/json",
              description="The CAD-neutral Feature Graph IR schema AND capability registry — the "
                          "node types and params the deterministic compiler accepts. What is not "
                          "in it cannot be built. Read before authoring any ir.graph.")
def _feature_graph_schema() -> str:
    # Same trimmed copy the tool serves at profile='all' — one shape, whichever channel reaches
    # the model. A resource URI takes no arguments, so the resource is always the full registry.
    return _feature_graph_schema_text("all")


@mcp.resource("schema://analysis-artifact", name="analysis_artifact_schema",
              mime_type="application/json",
              description="The persistent per-file analysis-artifact contract "
                          "(.solidpilot/*.analysis.json): identity/hash, recipe, lifted parameter "
                          "table, ir block + the formal 'verified' definition.")
def _analysis_artifact_schema() -> str:
    return _contract_json("analysis-artifact.schema.json")


# ---------------------------------------------------------------------------
# Tool: save_analysis  (Phase A analysis pipeline — ADAPTER-ONLY, no C#; ADR-040)
# ---------------------------------------------------------------------------
def _save_assembly_analysis(src: str, sha: str) -> str:
    return _save_assembly_analysis_impl(src, sha, _call_raw)


@mcp.tool(structured_output=False)
def save_analysis(file_path: str) -> str:
    """Analyze a part FILE and persist its analysis ARTIFACT — the entry tool of the analysis
    pipeline. Opens the part (activates it if already open), runs the standard reads (features
    recipe + mass_properties + geometry), computes the file's sha256, and writes
    `<folder>/.solidpilot/<filename>.analysis.json` per
    cad-planner/contracts/analysis-artifact.schema.json.

    The artifact is a CACHE of the file's state at analysis time: consumers must compare
    identity.source_hash against the current file and re-analyze on a mismatch. The `ir` block
    is left null here — the AI/IR pass fills it later: BEFORE writing an ir.graph, read the MCP
    resource `recipe://usage/index` for the mapping rules. The part is left OPEN and
    ACTIVE for follow-up work.

    file_path: absolute path of the .SLDPRT part OR .SLDASM assembly to analyze (drawing
        artifacts arrive with later pipeline steps). An assembly artifact's recipe holds the
        component tree (tree order, full transforms) + mates (creation order, enum types,
        entity anchors) + assembly mass properties.
    Returns a token-frugal summary (artifact path + counts) — the artifact JSON stays on disk;
    read it from there when the full content is needed."""
    return _run_save_analysis(file_path, _call_raw)

# ---------------------------------------------------------------------------
# Tool: rebuild_from_ir  (adapter-only — the analysis pipeline's IR door, IR-ADR-005)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def rebuild_from_ir(artifact_path: str, fresh_document: bool = True) -> str:
    """Rebuild a part from its analysis artifact's Feature Graph IR — the verification half of
    the round-trip ("the LLM proposes, the round-trip decides", IR-ADR-006). Reads
    `<...>.analysis.json`, takes `ir.graph`, and executes it through the SAME deterministic
    pycompiler as the forward door submit_feature_graph (two doors, ONE compiler — never forked).

    fresh_document (default True): open a NEW blank part first — the normal round-trip flow
        (rebuild fresh, then compare_parts against the original). Pass False only if you have
        already prepared the target document yourself.

    The rebuild reproduces the part AS-ANALYZED. If the source file changed since the artifact
    was written (hash mismatch) the result carries source_stale=true — re-run save_analysis and
    regenerate the IR rather than trusting a stale graph.

    Returns the compiler's per-node summary (COMPLETED n/n, or the feature-level error and how
    far it got — partial geometry may remain; CAD ops are not transactional). Afterwards, verify
    with compare_parts and only then label the artifact's ir.verification. If you are GENERATING
    the ir.graph yourself, read the MCP resource `recipe://usage/index` first — it indexes the
    mapping/canonicalization rules the compiler expects."""
    return _run_rebuild_from_ir(artifact_path, fresh_document, _run_graph)

# ---------------------------------------------------------------------------
# Tool: compare_parts  (adapter-only — the objective round-trip verifier, ADR-040/A0)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def compare_parts(doc_a: str, doc_b: str, detail: str = "") -> str:
    """Objectively diff two part documents — the round-trip verifier behind the artifact's
    `verified` label (and a general-purpose "are these the same part?" check).

    doc_a / doc_b: EITHER an absolute .SLDPRT path (opened/activated from disk) OR the TITLE of
        an already-open document (e.g. 'Part4' for an unsaved rebuild). doc_a is the REFERENCE
        (deltas are relative to it — normally the original; doc_b = the rebuild).

    For each doc it runs analyze_model(geometry + mass_properties) and reports topology
    (bodies/faces/edges/vertices), volume, surface area and CG side by side with deltas, plus
    the DECIDED verified-criteria verdict (analysis-artifact.schema.json): topology EXACT AND
    |ΔV| <= 1% AND |ΔA| <= 1%. The verdict is a REPORT — writing ir.verification into the
    artifact stays the caller's job. Read-only geometry-wise (activation may switch the active
    document; doc_b is left active). bbox comparison: pending (analyze doesn't expose it yet).

    detail='faces' (optional): ALSO run analyze_model(faces) on both and list the surfaces that
        are EXTRA (in doc_b only) or MISSING (in doc_a only), matched by supporting plane (normal
        + offset) for planar faces and by (axis, radius) for cylinders. This is the ONE diagnosis a
        point-query can't do — it turns "which face did my rebuild add/drop?" into one call instead
        of manually pairing two full face lists. Faces are reported with their key attributes + the
        stable index `i` in their own document."""
    return _run_compare_parts(doc_a, doc_b, detail, _compare_docs)



# ---------------------------------------------------------------------------
# Tool: compare_assemblies  (adapter-only — the assembly round-trip verifier, ADR-047 B4)
# ---------------------------------------------------------------------------
@mcp.tool(structured_output=False)
def compare_assemblies(doc_a: str, doc_b: str) -> str:
    """Objectively diff two ASSEMBLY documents against the RATIFIED verified criteria
    (ADR-047): component set EXACT (source file + config + instance counts) AND every
    component transform within tolerance (position <= 1µm, rotation <= 1e-6) AND mate
    count + types match AND mass properties within the V1 thresholds (|dV| <= 1%, |dA| <= 1%).

    doc_a / doc_b: EITHER an absolute .SLDASM path OR the TITLE of an already-open document
        (e.g. 'Assem2' for an unsaved rebuild). doc_a is the REFERENCE (the original).

    Components are paired by (source basename, instance ordinal in tree order) — instance
    numbering may legitimately differ between original and rebuild. The verdict is a REPORT;
    writing ir.verification into the artifact stays the caller's job. doc_b is left active."""
    return _run_compare_assemblies(doc_a, doc_b, _call_raw)

# ---------------------------------------------------------------------------
# Tool: submit_feature_graph  (the FORWARD IR door — intent → IR → pycompiler → SolidWorks.
# Re-enabled GATE-FREE 2026-07-16 for the forward vocabulary effort (IR-ADR-017); the old
# experimental gates were deleted by IR-ADR-005. Two doors, ONE pycompiler: rebuild_from_ir
# replays an artifact's stored graph, this tool takes a graph directly — never fork the compiler.)
# ---------------------------------------------------------------------------

@mcp.tool(structured_output=False)
def submit_feature_graph(graph: str, fresh_document: bool = True) -> str:
    """Build a part (or assembly) from a Feature Graph IR in ONE call — the deterministic
    compiler lowers each IR node to the right low-level tool sequence and resolves references
    (geometric anchors, runtime feature names) against live geometry.

    ★ PRIMARY BUILD PATH — prefer this whenever you are CREATING a part/assembly (including a
    reverse reconstruction from a drawing). Assemble the WHOLE feature graph and submit it ONCE;
    do NOT hand-build feature-by-feature with the individual low-level tools
    (create_sketch/extrude_feature/add_edge_feature/…). One graph keeps tree order intact, lets
    the compiler resolve anchors + runtime feature names deterministically, and takes far fewer
    round-trips. The low-level tools are for RECOVERY (re-doing one failed node), a genuine
    one-off edit, or a feature the IR vocabulary cannot yet express — not for routine
    construction. If you truly cannot express the whole part in one graph yet, still submit the
    LARGEST coherent batches (fresh_document=True for the first, append with fresh_document=False),
    never one node at a time.

    graph: the Feature Graph as a JSON STRING. Authoring from DESIGN INTENT: read the MCP resource
        `recipe://usage/forward` FIRST (grammar, anchor design, self-verification), plus
        `schema://feature-graph` — the schema IS the capability registry.
        Replaying an ANALYZED part instead: `recipe://usage/canonicalization` +
        `recipe://usage/mapping_part` (…/mapping_sheet_metal, …/mapping_assembly).
        Essentials: units METERS, angles RADIANS (the compiler converts at tool boundaries);
        nodes build in array order (tree order is law); extrude/revolve/rib/sweep/sheet_metal/
        sketched_bend consume the IMMEDIATELY preceding sketch node; loft profiles, a sweep's
        path, pattern seeds and mirror features reference EARLIER nodes by id; an optional
        graph-level material {name, library?} applies after the last node.
    fresh_document (default True): open a new blank document first — part graphs a part,
        assembly graphs (component/mate nodes) an assembly. Pass False to build into the
        CURRENT active document instead.

    Returns the compiler's per-node report: COMPLETED n/n, or FAILED with a feature-level error
    and how far it got. CAD ops are NOT transactional — on failure partial geometry remains
    (reported, never hidden). The adapter resyncs state_version after every run, so subsequent
    normal tools keep working. Verify the result objectively (analyze_model / compare_parts) —
    never assume."""
    return _run_submit_feature_graph(graph, fresh_document, _run_graph)

# ---------------------------------------------------------------------------
# Tool: convert_to_features  (adapter-only — STEP / history-less part → feature-based part)
#   open → history guard → analyze_model(brep) → cad-planner/recognition → _run_graph (the ONE
#   compile path) → save → verify (rebuild re-imported via STEP vs the original import) → artifact.
# ---------------------------------------------------------------------------

@mcp.tool(structured_output=False)
def convert_to_features(
    file_path: Annotated[str, Field(description="Absolute path of the input: a STEP/IGES file, or a .SLDPRT that holds only an imported body (no feature history).")],
    output_path: Annotated[str, Field(description="Where to save the feature-based part (.SLDPRT). Blank = '<input name>_features.SLDPRT' next to the input. Must differ from the input.")] = "",
    overwrite: Annotated[bool, Field(description="Replace output_path if it already exists (default false: refuse with OUTPUT_EXISTS).")] = False,
) -> str:
    """Convert a 'dumb' imported solid (STEP/IGES, or a history-less .SLDPRT) into a FEATURE-BASED
    SolidWorks part: a real tree of sketches + extrude/revolve + cuts (each hole/pocket/slot its own
    cut) + chamfers/fillets, rebuilt through the IR compiler and verified against the original.

    Scope (v1): single-body MACHINED PRISMATIC and TURNED parts whose faces are aligned with X/Y/Z.
    The input file is NEVER modified.

    Returns ONE line:
      COMPLETED | VERIFIED | ...   the rebuild matches the original exactly (topology + |ΔV|,|ΔA| <= 1%)
      COMPLETED | UNVERIFIED ...   saved anyway — differences listed (dropped nodes = features that
                                   failed to build; unexplained_faces = geometry not recognized)
      FAILED | <CODE> | ...        nothing saved. Codes: FILE_NOT_FOUND, UNSUPPORTED_TYPE,
                                   OUTPUT_IS_INPUT, OUTPUT_EXISTS (pass overwrite=True), OPEN_FAILED,
                                   HAS_HISTORY (already feature-based),
                                   MULTIBODY (split first with save_body_as_part), NOT_AXIS_ALIGNED,
                                   UNSUPPORTED_GEOMETRY (freeform/sheet metal), BUILD_FAILED.
    An analysis artifact with the IR and its verification is written to .solidpilot/ next to the
    output, so rebuild_from_ir can replay it. Large parts take minutes (the B-rep read is slow)."""
    return _run_convert_to_features(
        file_path,
        output_path,
        overwrite,
        _call_raw,
        _run_graph,
        _compare_docs,
        _recognize_brep,
    )

# ---------------------------------------------------------------------------
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
