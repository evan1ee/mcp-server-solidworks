from __future__ import annotations

from typing import Callable, Dict, Tuple
from mcp.server import MCPServer
from recipes import (
    _RECIPE_SECTIONS,
    _recipe_sections,
    _recipe_section_text,
    _contract_json,
    _feature_graph_schema_text,
    format_recipe_index_resource as _format_recipe_index_resource,
)


def _recipe_index() -> str:
    """The table of contents, generated from the file so it can never drift from what is served."""
    return _format_recipe_index_resource()


def _recipe_section_alias(section: str) -> str:
    return _recipe_section_text(section)


def _feature_graph_schema() -> str:
    return _feature_graph_schema_text("all")


def _analysis_artifact_schema() -> str:
    return _contract_json("analysis-artifact.schema.json")


def register_resources(mcp: MCPServer) -> None:
    """Register all static and dynamic recipe & schema resources on the MCP server."""
    mcp.resource(
        "recipe://usage/index",
        name="recipe_index",
        mime_type="text/markdown",
        description="START HERE for IR work: the recipe version header + every section URI. "
                    "Rules for turning an analysis artifact or a 2D drawing into a Feature "
                    "Graph IR.",
    )(_recipe_index)

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

    mcp.resource(
        "recipe://usage/{section}",
        name="recipe_section",
        mime_type="text/markdown",
        description="Alias: any recipe-usage.md section by slug. The per-section URIs above "
                    "are the discoverable form; this only catches a slug read off the index.",
    )(_recipe_section_alias)

    mcp.resource(
        "schema://feature-graph",
        name="feature_graph_schema",
        mime_type="application/json",
        description="The CAD-neutral Feature Graph IR schema AND capability registry — the "
                    "node types and params the deterministic compiler accepts. What is not "
                    "in it cannot be built. Read before authoring any ir.graph.",
    )(_feature_graph_schema)

    mcp.resource(
        "schema://analysis-artifact",
        name="analysis_artifact_schema",
        mime_type="application/json",
        description="The persistent per-file analysis-artifact contract "
                    "(.solidpilot/*.analysis.json): identity/hash, recipe, lifted parameter "
                    "table, ir block + the formal 'verified' definition.",
    )(_analysis_artifact_schema)
