# 09 · MCP Tool & Resource Reference

**47 tools**, all defined in `adapters/claude/server.py` and mirrored by name and parameters in `execution/solidworks/contracts/tool-schemas.json` (the contract test enforces this). The contract's `description` fields hold the detailed COM-level notes; the `server.py` docstrings hold the model-facing guidance.

Route legend: **E** = forwarded to the C# execution layer (`POST /api/tool/execute`, state-versioned), **E·ro** = forwarded but read-only (no `state_version` bump), **A** = adapter-only (no `ToolController` case), **C** = adapter → `pycompiler` → execution.

All lengths are **meters**. Tool angles are **degrees** except `modify_dimension` (radians). Array-valued params are **JSON strings**.

## Lifecycle & documents

| Tool | Route | Purpose / key params |
|---|---|---|
| `ensure_ready` | lifecycle | Start the execution server if needed, attach to or launch SolidWorks. Not a versioned op. |
| `open_new_part` / `open_new_assembly` | E | Blank document (`template_path?`) |
| `open_document` | E | Open a file from disk: native, or STEP/IGES/NX/… via staged import (`as_assembly` for assembly STEP) |
| `activate_document` | E·ro | Switch to an open document by `title` |
| `save_document` | E | `file_path?` (save-as when given) |
| `close_document` | E | `save`, `close_all` |
| `save_body_as_part` | E | Extract body `body_index` of a multibody part into its own `.sldprt` |

## Sketch

| Tool | Route | Purpose / key params |
|---|---|---|
| `create_sketch` | E | On `plane` (Front/Top/Right or a named ref plane) or on a face (`on_face`, `face_index` preferred over `face_x/y/z`) |
| `edit_sketch` | E | Reopen `sketch_name` |
| `add_sketch_entity` | E | `entity_type` rectangle\|circle\|line\|arc\|arc_center\|ellipse\|spline\|fillet\|chamfer + coordinates; `points` (JSON string) for splines; `construction` |
| `add_sketch_entities` | E | Batch of the above in one call (`segments`, JSON string). Not transactional. |
| `add_sketch_constraint` | E | horizontal\|vertical\|coincident\|parallel\|perpendicular\|tangent\|equal\|midpoint, selected by points |
| `add_dimension` | E | Smart dimension on the segment at (`px`, `py`) |

## Features

| Tool | Route | Purpose / key params |
|---|---|---|
| `extrude_feature` | E | `feature_type` boss\|cut\|revolve\|sweep\|loft from the active sketch; `depth`, `through`, `up_to_face_index`, `mid_plane`, `reverse`; revolve `angle` + axis; sweep `path_sketch`; loft `profiles` |
| `create_rib` | E | Rib from the active sketch |
| `add_edge_feature` | E | fillet\|chamfer on `edge_indices` (preferred) or a point pick; chamfer distance_angle\|distance_distance |
| `add_reference_geometry` | E | `type` plane\|axis\|point (offset plane, axis from two entities, …) |
| `create_pattern` | E | linear\|circular\|mirror (`feature_name`, `features_json`, `axis_name`, `plane`). `count2`/`spacing2` accepted but no second direction is wired. |
| `sheet_metal_feature` | E | base_flange\|edge_flange\|edge_flange_sketch\|edge_flange_finish\|flat_pattern\|sketched_bend |
| `set_part_material` | E | `material_name`, `library?` |
| `modify_dimension` | E | Set a named dimension (`D1@Sketch1`) in SI units, rebuild |
| `edit_feature` | E | suppress\|unsuppress\|delete\|rename |

## Assembly

| Tool | Route | Purpose / key params |
|---|---|---|
| `insert_component` | E | `file_path`, position or `transform_json` (13 numbers), `config`, `fixed` |
| `add_mate` | E | `mate_type`, two sides as (`component`, `kind`, `index`) from `analyze_assembly`, `alignment`, `flip`, `value` |
| `analyze_assembly` | E·ro | components\|components_flat\|mates\|faces\|edges (`component` for per-component faces/edges) |

## Analysis & query

| Tool | Route | Purpose / key params |
|---|---|---|
| `verify_state` | E·ro | Current CadState |
| `analyze_model` | E·ro | mass_properties\|geometry\|bodies\|edges\|faces\|features\|sketch\|feature_map; `near`/`k`/`axis` narrow face/edge reads. Face/edge **indices** from here feed other tools. |
| `get_selection` | E·ro | What the user selected in the GUI, mapped to analyze indices |
| `analyze_slddrw_test` | E·ro | Native `.slddrw` reader — test/reference only |

## Drawing (output)

| Tool | Route | Purpose |
|---|---|---|
| `create_drawing` | E | New drawing (A3, 1:1) for `model_path` |
| `add_drawing_view` | E | front\|top\|right\|isometric\|back\|bottom\|left (the model must be saved to disk) |
| `add_flat_pattern_view` | E | Sheet-metal flat pattern view |
| `add_section_view` | E | Section view through a cut line |
| `add_drawing_dimension` | E | Smart dimension on a view segment |
| `auto_dimension_drawing` | E | Insert Model Items → dimensions |
| `auto_center_marks` | E | Center marks on holes/slots |
| `add_hole_callout` | E | Hole callout at a point |

## Export

| Tool | Route | Purpose |
|---|---|---|
| `export_document` | E | STEP\|IGES\|STL\|PDF\|DWG\|DXF (PDF/DWG/DXF need a drawing) |
| `batch_export` | E | Several formats at once (`formats_json`) |

## Analysis pipeline & IR (adapter orchestration)

| Tool | Route | Purpose |
|---|---|---|
| `get_recipe` | A | Serve a section of `recipe-usage.md` or a schema (see below) |
| `save_analysis` | A | Write the analysis artifact for a `.sldprt`/`.sldasm` to `.solidpilot/` |
| `rebuild_from_ir` | C | **Reverse door**: rebuild from an artifact's `ir.graph` in a fresh document; warns `source_stale` |
| `submit_feature_graph` | C | **Forward door**: build from an IR JSON string supplied by the model |
| `compare_parts` | A | Topology + volume/area diff → `verified_criteria` PASS/FAIL; `detail='faces'` lists unmatched faces |
| `compare_assemblies` | A | Component set, transforms, mates, mass diff |
| `analyze_drawing` | A + C | DXF/DWG reader; `mode` auto\|ir\|build ([07](07-drawing-reader.md)) |

## Resources

| URI | Content |
|---|---|
| `recipe://usage/index` | Table of contents of the recipe |
| `recipe://usage/<slug>` | One recipe section: `contract`, `canonicalization`, `forward`, `mapping`, `mapping_part`, `mapping_sheet_metal`, `mapping_assembly`, `verification`, `reverse`, `coverage` |
| `recipe://usage/{section}` | Template alias of the above |
| `schema://feature-graph` | IR schema (trimmed of internal notes) |
| `schema://analysis-artifact` | Analysis artifact schema |

`get_recipe(section)` serves the same sections plus `feature_graph_schema` (`profile='part'` drops assembly types) and `analysis_artifact_schema`. It exists because most MCP hosts don't let the model read resources itself.

## Result strings

- Forwarded success: `COMPLETED | state_version=N | document=… | sketch=… | features=[…] | result_geometry={…}`
- Replay: `DUPLICATE | operation already executed | last_known_state_version=N`
- Forwarded failure: raised as an MCP error, `CAD operation failed | code=<CODE> | message=…`
- Adapter-only failure: returned as text, `FAILED | <CODE> | <detail>`
