"""Recipe and CAD-planner contract schema readers for the MCP resource and tool surfaces."""
import json
import os
from mcp.server.mcpserver.exceptions import ResourceError

_CAD_PLANNER_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "cad-planner"))

# The served section slugs, EXPLICIT (this replaces get_recipe's `Literal[...]`). The surface is
# under the user's control by decision: a new `## slug — title` in recipe-usage.md does NOT
# publish itself as a resource until it is added here, deliberately.
_RECIPE_SECTIONS = (
    "contract", "canonicalization", "forward", "mapping", "mapping_part",
    "mapping_sheet_metal", "mapping_assembly", "verification", "reverse", "coverage",
)

_SCHEMA_DEV_ONLY_SUBSET_KEYS = ("_note", "_grammar_note", "assumptions")

_SCHEMA_PROFILE_DROPS = {
    "all": (),
    "part": ("assembly_types_v06",),
}


def _recipe_sections():
    """Parse cad-planner/recipe-usage.md into its version header + {slug: (title, body)} by the
    '## slug — title' section headers. Read fresh on every call — the file ships with the server
    and is small; no caching keeps edits live without a reconnect."""
    path = os.path.join(_CAD_PLANNER_DIR, "recipe-usage.md")
    with open(path, "r", encoding="utf-8-sig") as fh:
        text = fh.read()
    sections = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## ") and " — " in line:
            slug, _, title = line[3:].partition(" — ")
            current = slug.strip()
            sections[current] = [title.strip(), []]
        elif current is not None:
            sections[current][1].append(line)
    header = text.split("\n## ", 1)[0]  # the version/preamble block above the first section
    return header, {k: (t, "\n".join(body).strip()) for k, (t, body) in sections.items()}


def _recipe_section_text(section: str) -> str:
    """One section, rendered with its own `## slug — title` header restored.

    Read FRESH from disk on every call (via _recipe_sections), so editing recipe-usage.md goes
    live with no reconnect — only the registration metadata below is snapshotted at import."""
    _header, sections = _recipe_sections()
    if section not in sections:
        # MUST be ResourceError: the SDK re-raises ResourceError/MCPError verbatim but replaces any
        # other exception's message with a generic "Error creating resource from template <uri>"
        # (templates.py).
        raise ResourceError(
            f"'{section}' is not a served recipe section. Served: "
            f"{', '.join(_RECIPE_SECTIONS)}. Read recipe://usage/index for the full surface.")
    title, body = sections[section]
    return f"## {section} — {title}\n\n{body}"


def _contract_json(filename: str) -> str:
    with open(os.path.join(_CAD_PLANNER_DIR, "contracts", filename),
              "r", encoding="utf-8-sig") as fh:
        return fh.read()


def _feature_graph_schema_text(profile: str = "all") -> str:
    """`feature-graph.schema.json` with the developer-only blocks removed, re-serialized compactly.

    Falls back to the raw file if it will not parse: a malformed schema must still reach the caller
    (an unreadable capability registry is a loud failure, not a silent empty one).
    """
    raw = _contract_json("feature-graph.schema.json")
    try:
        doc = json.loads(raw)
    except ValueError:
        return raw
    meta = doc.get("_meta")
    if isinstance(meta, dict):
        meta.pop("status", None)
    subset = doc.get("covered_subset")
    if isinstance(subset, dict):
        for key in _SCHEMA_DEV_ONLY_SUBSET_KEYS:
            subset.pop(key, None)
    for key in _SCHEMA_PROFILE_DROPS.get(profile, ()):
        doc.pop(key, None)
    return json.dumps(doc, ensure_ascii=False, indent=1)


def format_get_recipe(section: str = "index", profile: str = "all") -> str:
    """Implementation behind the `get_recipe` MCP tool."""
    if section == "feature_graph_schema":
        return _feature_graph_schema_text(profile)
    if section == "analysis_artifact_schema":
        return _contract_json("analysis-artifact.schema.json")
    header, sections = _recipe_sections()
    if section == "index":
        toc = "\n".join(f"- {slug} — {title}" for slug, (title, _b) in sections.items())
        return (f"{header.strip()}\n\nSections (pass as `section`):\n{toc}\n"
                "- feature_graph_schema — the IR schema / capability registry (JSON)\n"
                "- analysis_artifact_schema — the analysis-artifact contract (JSON)")
    return _recipe_section_text(section)


def format_recipe_index_resource() -> str:
    """Implementation behind the `recipe://usage/index` MCP resource."""
    header, sections = _recipe_sections()
    lines = []
    for slug in _RECIPE_SECTIONS:
        title = (sections.get(slug) or ("(missing from recipe-usage.md)",))[0]
        lines.append(f"- recipe://usage/{slug} — {title}")
    return (
        f"{header.strip()}\n\n"
        f"Sections (read by URI):\n" + "\n".join(lines) + "\n\n"
        "Contracts:\n"
        "- schema://feature-graph — the IR schema / capability registry (JSON). What is NOT in\n"
        "  it cannot be built; it doubles as the capability list.\n"
        "- schema://analysis-artifact — the persistent analysis-artifact contract (JSON):\n"
        "  identity/hash, recipe, parameter table, ir block + the formal 'verified' definition.\n\n"
        "Reading order — ARTIFACT→IR: contract + canonicalization + mapping, then the vocabulary\n"
        "section matching the document (mapping_part / mapping_sheet_metal / mapping_assembly),\n"
        "then verification BEFORE labeling anything. INTENT→IR (no original part): forward.\n"
        "DXF/DWG DRAWING→PART: reverse."
    )
