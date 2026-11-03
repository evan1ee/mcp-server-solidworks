from __future__ import annotations

from typing import Any, Callable, Dict, Optional
from mcp.server import MCPServer
from tools.common import CallCallable

_call: Optional[CallCallable] = None
_ensure_ready_client: Optional[Callable[[], Dict[str, Any]]] = None
_set_state_version: Optional[Callable[[int], None]] = None


def ensure_ready() -> str:
    """Bring the SolidWorks environment up and confirm it is ready to use.

    Starts the execution server if it isn't running, and launches SolidWorks if it is closed
    (just attaches if it's already open). Does NOT open or create any document — use
    open_new_part / create_drawing for that (so assembly/drawing workflows aren't forced into a
    part). Safe to call anytime; idempotent. Call this first at the start of a session, or
    whenever a tool fails with a connection / COM-attach error."""
    if _ensure_ready_client is None:
        raise RuntimeError("ensure_ready client is not configured")
    body = _ensure_ready_client()
    if not body.get("comAttached"):
        raise RuntimeError(
            "SolidWorks is not ready | "
            f"launch_error={body.get('launchError') or body.get('ensureError')}"
        )
    sv = body.get("stateVersion")
    if sv is not None and _set_state_version is not None:
        _set_state_version(sv)
    return (
        f"READY | server=UP | "
        f"com_attached={body.get('comAttached')} | "
        f"sw_launched={body.get('swLaunched')} | "
        f"active_document={body.get('activeDocument')} | "
        f"sw_version={body.get('swVersion')} | "
        f"state_version={body.get('stateVersion')}"
    )


def open_new_part(template_path: str = "") -> str:
    """Open a new SolidWorks part document."""
    params: Dict[str, Any] = {}
    if template_path:
        params["template_path"] = template_path
    return _call("open_new_part", params)


def open_document(file_path: str, as_assembly: bool = False) -> str:
    """Open an EXISTING document from disk (counterpart to open_new_part, which makes a BLANK doc).
    file_path: full path to the file.
      - Native .sldprt / .sldasm / .slddrw open directly.
      - Foreign .step / .iges / .x_t / NX .prt / .ipt / .CATPart import via a STAGED path: the
        CLASSIC translator (LoadFile4) first — proven for STEP/IGES and NX .prt — with a
        3D-Interconnect retry as the last resort. A clear OPEN_FAILED means no usable translator:
        convert to STEP or defer that file.
      - as_assembly=True (foreign files only): the file is an ASSEMBLY STEP — the classic import
        would flatten it to a MULTIBODY PART; this asks for a real assembly import instead.
    Makes the opened document active and bumps state_version."""
    params: Dict[str, Any] = {"file_path": file_path}
    if as_assembly:
        params["as_assembly"] = True
    return _call("open_document", params)


def open_new_assembly(template_path: str = "") -> str:
    """Open a new blank SolidWorks ASSEMBLY document (the twin of open_new_part).
    Use before insert_component / add_mate. template_path: optional .asmdot template
    (default: the user-configured assembly template)."""
    params: Dict[str, Any] = {}
    if template_path:
        params["template_path"] = template_path
    return _call("open_new_assembly", params)


def save_body_as_part(body_index: int, file_path: str) -> str:
    """Extract ONE solid body of the ACTIVE multibody part into its own .SLDPRT file.
    body_index: from analyze_model(analysis_type='bodies'). The body's geometry stays in the
    SOURCE coordinates (recover the instance transform separately); the new part is saved to
    file_path and closed, and the source document is re-activated. Use for flattened assembly
    imports whose per-part files don't exist: one file per DISTINCT body (fingerprint-deduped),
    then instances reference it with recovered transforms."""
    return _call("save_body_as_part", {"body_index": body_index, "file_path": file_path})


def verify_state() -> str:
    """Read and return the current CAD state without modifying it."""
    return _call("verify_state", {})


def close_document(save: bool = False, close_all: bool = False) -> str:
    """Close the active SolidWorks document. Set save=True to save before closing.
    close_all=True closes ALL open documents (incl. invisibly-loaded component docs that a
    per-document close loop can never reach), DISCARDING unsaved changes — use to fully reset
    the document space between assembly jobs."""
    params: Dict[str, Any] = {"save": save}
    if close_all:
        params["close_all"] = True
    return _call("close_document", params)


def save_document(file_path: str = "") -> str:
    """Save the active SolidWorks document to disk.
    file_path: full output path including the document extension
    (.sldprt for parts, .sldasm for assemblies, .slddrw for drawings).
    If omitted, saves in place (only works if the document was saved before).
    A part must be saved to disk before it can be referenced by a drawing view."""
    return _call("save_document", {"file_path": file_path})


def activate_document(title: str) -> str:
    """Switch the ACTIVE SolidWorks document to an already-open one by its title (e.g. 'gear' or
    'gear.SLDPRT'). Use this to read/compare another open part — e.g. activate the original, analyze_model
    it, then activate your copy — without OS window switching. Does NOT change geometry or state_version.
    The document must already be OPEN. Returns the now-active document title."""
    return _call("activate_document", {"title": title})


def register_lifecycle_tools(
    mcp: MCPServer,
    call_fn: CallCallable,
    ensure_ready_fn: Callable[[], Dict[str, Any]],
    set_state_version_fn: Callable[[int], None],
) -> Dict[str, Callable]:
    global _call, _ensure_ready_client, _set_state_version
    _call = call_fn
    _ensure_ready_client = ensure_ready_fn
    _set_state_version = set_state_version_fn

    tools = [
        ensure_ready,
        open_new_part,
        open_document,
        open_new_assembly,
        save_body_as_part,
        verify_state,
        close_document,
        save_document,
        activate_document,
    ]
    for fn in tools:
        mcp.tool(structured_output=False)(fn)
    return {fn.__name__: fn for fn in tools}
