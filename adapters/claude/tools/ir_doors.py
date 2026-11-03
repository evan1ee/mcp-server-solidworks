from __future__ import annotations

from typing import Annotated, Any, Callable, Dict, Literal, Optional
from pydantic import Field
from mcp.server import MCPServer
from recipes import format_get_recipe as _format_get_recipe
from analysis_pipeline import (
    save_assembly_analysis as _save_assembly_analysis_impl,
    run_save_analysis as _run_save_analysis,
)
from comparison import (
    run_compare_parts as _run_compare_parts,
    run_compare_assemblies as _run_compare_assemblies,
)
from ir_runner import (
    run_rebuild_from_ir as _run_rebuild_from_ir,
    run_submit_feature_graph as _run_submit_feature_graph,
)
from feature_conversion import run_convert_to_features as _run_convert_to_features
from tools.common import CallRawCallable

_call_raw: Optional[CallRawCallable] = None
_run_graph: Optional[Callable] = None
_compare_docs: Optional[Callable] = None
_recognize_brep: Optional[Callable] = None

import sys

def _get_call_raw():
    srv = sys.modules.get("server")
    return getattr(srv, "_call_raw", _call_raw)

def _get_run_graph():
    srv = sys.modules.get("server")
    return getattr(srv, "_run_graph", _run_graph)

def _get_compare_docs():
    srv = sys.modules.get("server")
    return getattr(srv, "_compare_docs", _compare_docs)

def _get_recognize_brep():
    srv = sys.modules.get("server")
    return getattr(srv, "_recognize_brep", _recognize_brep)

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



def _save_assembly_analysis(src: str, sha: str) -> str:
    return _save_assembly_analysis_impl(src, sha, _get_call_raw())


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
    return _run_save_analysis(file_path, _get_call_raw())

# ---------------------------------------------------------------------------
# Tool: rebuild_from_ir  (adapter-only — the analysis pipeline's IR door, IR-ADR-005)
# ---------------------------------------------------------------------------
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
    return _run_rebuild_from_ir(artifact_path, fresh_document, _get_run_graph())

# ---------------------------------------------------------------------------
# Tool: compare_parts  (adapter-only — the objective round-trip verifier, ADR-040/A0)
# ---------------------------------------------------------------------------
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
    return _run_compare_parts(doc_a, doc_b, detail, _get_compare_docs())



# ---------------------------------------------------------------------------
# Tool: compare_assemblies  (adapter-only — the assembly round-trip verifier, ADR-047 B4)
# ---------------------------------------------------------------------------
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
    return _run_compare_assemblies(doc_a, doc_b, _get_call_raw())

# ---------------------------------------------------------------------------
# Tool: submit_feature_graph  (the FORWARD IR door — intent → IR → pycompiler → SolidWorks.
# Re-enabled GATE-FREE 2026-07-16 for the forward vocabulary effort (IR-ADR-017); the old
# experimental gates were deleted by IR-ADR-005. Two doors, ONE pycompiler: rebuild_from_ir
# replays an artifact's stored graph, this tool takes a graph directly — never fork the compiler.)
# ---------------------------------------------------------------------------

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
    return _run_submit_feature_graph(graph, fresh_document, _get_run_graph())

# ---------------------------------------------------------------------------
# Tool: convert_to_features  (adapter-only — STEP / history-less part → feature-based part)
#   open → history guard → analyze_model(brep) → cad-planner/recognition → _run_graph (the ONE
#   compile path) → save → verify (rebuild re-imported via STEP vs the original import) → artifact.
# ---------------------------------------------------------------------------

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
        _get_call_raw(),
        _get_run_graph(),
        _get_compare_docs(),
        _get_recognize_brep(),
    )

# ---------------------------------------------------------------------------

def register_ir_doors(
    mcp: MCPServer,
    call_raw_fn: CallRawCallable,
    run_graph_fn: Callable,
    compare_docs_fn: Callable,
    recognize_brep_fn: Callable,
) -> Dict[str, Callable]:
    global _call_raw, _run_graph, _compare_docs, _recognize_brep
    _call_raw = call_raw_fn
    _run_graph = run_graph_fn
    _compare_docs = compare_docs_fn
    _recognize_brep = recognize_brep_fn

    tools = [
        get_recipe,
        save_analysis,
        rebuild_from_ir,
        compare_parts,
        compare_assemblies,
        submit_feature_graph,
        convert_to_features,
    ]
    for fn in tools:
        mcp.tool(structured_output=False)(fn)
    return {fn.__name__: fn for fn in tools}
