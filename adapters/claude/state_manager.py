"""Shared adapter-side helpers for state_version, payload parsing, and document activation."""
import os
import uuid
from typing import Callable, Optional


def next_operation_id() -> str:
    return str(uuid.uuid4())


def is_state_mismatch(response: dict) -> bool:
    """Return True if the execution layer rejected the call with INVALID_STATE_VERSION."""
    if response.get("status") != "FAILED":
        return False
    return (response.get("error") or {}).get("code") == "INVALID_STATE_VERSION"


def delta_pct(a, b) -> Optional[float]:
    """Percentage change from `a` to `b`: (b - a) / a * 100.0, or None if invalid/zero."""
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or a == 0:
        return None
    return (b - a) / a * 100.0


def analysis_items(response: dict) -> list:
    """Return the `cadState.features` list an analyze_model call carries its payload in.

    Payload shapes (C#-owned): 'features' mode = ONE item holding the whole recipe as a JSON
    string; 'mass_properties'/'geometry' modes = 'key=value' strings. Raises RuntimeError on a
    FAILED response (mirrors map_response's error discipline).
    """
    if response.get("status") != "COMPLETED":
        err = response.get("error") or {}
        raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
    return (response.get("cadState") or {}).get("features") or []


def kv_dict(items: list) -> dict:
    """Parse ['volume=4,22503E-06', 'faces=12', ...] into a dict with real numbers.

    Numeric values may use a COMMA decimal separator (localized SolidWorks formatting);
    normalize to float, and to int for plain integer counts.
    """
    out = {}
    for item in items:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, _, raw = item.partition("=")
        raw = raw.strip()
        try:
            num = float(raw.replace(",", "."))
            is_plain_int = raw.isdigit() or (raw.startswith("-") and raw[1:].isdigit())
            out[key.strip()] = int(num) if is_plain_int else num
        except ValueError:
            out[key.strip()] = raw
    return out


def open_or_activate_doc(doc: str, label: str, call_raw: Callable[[str, dict], dict]) -> dict:
    """Open `doc` from disk if it is a file path, or activate it by title if already open.

    Raises RuntimeError('DOC_<label>_UNAVAILABLE | ...') on failure.
    """
    if os.path.isfile(doc):
        r = call_raw("open_document", {"file_path": os.path.abspath(doc)})
        if r.get("status") != "COMPLETED":
            r = call_raw("activate_document", {"title": os.path.splitext(os.path.basename(doc))[0]})
    else:
        r = call_raw("activate_document", {"title": doc})
    if r.get("status") != "COMPLETED":
        err = r.get("error") or {}
        raise RuntimeError(f"DOC_{label}_UNAVAILABLE | {doc} | {err.get('code')}: {err.get('message')}")
    return r
