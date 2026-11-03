# 03 · MCP Adapter (`adapters/claude/`)

The adapter is the MCP server the AI client launches. It translates tool calls to REST, tracks `state_version`, hosts the in-process compiler for the IR doors, and serves the recipe/schema prose. It holds **no CAD logic and no COM**.

| File | Role |
|---|---|
| `server.py` (~2.0k lines) | The whole MCP **surface**: every `@mcp.tool` / `@mcp.resource` definition (names, docstrings, type hints = the contract), `_state_version` and the `_call` / `_call_raw` transport wrappers. Orchestration tools are thin: they delegate to the modules below, passing `_call_raw` / `_run_graph` / `_compare_docs` in as callables. |
| `state_manager.py` | Pure helpers: operation ids, `is_state_mismatch`, `analysis_items`, `kv_dict` (comma-decimal tolerant), `delta_pct`. |
| `recipes.py` | `recipe-usage.md` section parsing, contract-JSON loading, the served (trimmed) feature-graph schema, `get_recipe` / `recipe://usage/index` bodies. |
| `analysis_pipeline.py` | `save_analysis` for `.SLDPRT` / `.SLDASM` (artifact writing, parameter lifting). |
| `comparison.py` | `compare_parts` / `compare_assemblies` and the face-diff matcher (`compare_docs` is shared with `convert_to_features`). |
| `drawing_pipeline.py` | `analyze_drawing` result formatting: direct-build summary, advisories, compact wire JSON, build tail. The `_draw.*` reader calls stay in `server.py` (the contract test scans them there). |
| `ir_runner.py` | `run_graph` — the ONE path from a Feature Graph to geometry — plus the `rebuild_from_ir` / `submit_feature_graph` bodies. |
| `feature_conversion.py` | `convert_to_features` orchestration: history guard, recognition view, drop-and-continue build, STEP re-import verification, artifact. |
| `execution_client.py` | Sync `httpx` REST client to the C# server: auto-start, timeouts, `/health` probe, logging. |
| `ir_execution_port.py` | `AdapterExecutionPort` — the production `ExecutionPort` that lets `pycompiler` drive the execution layer; `run_feature_graph(graph)`. |
| `response_mapper.py` | `ExecutionResponse` dict → compact result string (or raise on `FAILED`). |
| `config.py` | Env vars / endpoints (loads `adapters/claude/.env` by explicit path). |
| `adapter_log.py` | Append-only file log `adapters/claude/adapter.log` (stdout is the JSON-RPC stream, so never `print`). |

## MCP SDK usage

- Official MCP Python SDK **v2**: `from mcp.server import MCPServer` (not FastMCP), `mcp = MCPServer("solidworks-execution-adapter", cache_hints=...)`, `mcp.run()` → **stdio** transport.
- Tools: `@mcp.tool(structured_output=False)` on plain functions — every tool returns a `str` (a structured mirror would double wire size). Docstring = description, type hints = input schema.
- `_normalize_tool_surface()` (runs at import, touches the private `mcp._tool_manager`) cleans docstrings, strips auto `title` fields, and sets `additionalProperties: false` so a typo'd parameter name is rejected at the MCP layer. An SDK bump that breaks this is caught by the contract test.
- Resources: `@mcp.resource(uri, ...)` plus a loop registering one static resource per recipe section (templated URIs don't appear in `resources/list`, so a template-only surface would be undiscoverable).
- `CacheHint(ttl_ms=3_600_000)` is declared but was measured not to be honoured by the host; reads are always fresh.

## Call path for a forwarded tool

```
tool fn ──> _call(name, params)
              ├─ op id = uuid4, payload {operationId, tool, stateVersion, params}
              ├─ execution_client.call_tool → POST /api/tool/execute
              ├─ FAILED + INVALID_STATE_VERSION → GET /api/tool/state, retry ONCE with a new op id
              ├─ _state_version = response.stateVersion
              └─ response_mapper.map_response → "COMPLETED | state_version=N | document=… | features=[…] | result_geometry=…"
```

- `_call_raw` does the same but returns the raw dict and never raises — used by orchestration tools.
- `FAILED` from execution → `RuntimeError("CAD operation failed | code=… | message=…")` → surfaced as an MCP error. Adapter-only tools instead **return** `"FAILED | CODE | detail"` strings.
- `DUPLICATE` → `"DUPLICATE | operation already executed | last_known_state_version=…"`.
- Read-only tools (`analyze_*`, `activate_document`, `get_selection`) do not bump `state_version`.

### Auto-start and timeouts (`execution_client.py`)

- `_request_with_autostart` catches only `httpx.ConnectError`: it spawns `EXECUTION_EXE_PATH` detached + windowless (`_ensure_server_up`, guarded by a lock), polls `/health` every 0.5 s up to `SERVER_SPAWN_TIMEOUT`, then retries once.
- `_http_client` uses `trust_env=False` (a system proxy such as Clash once hijacked localhost) and always applies a fixed connect timeout `CONNECT_TIMEOUT`, so the exception type (`ConnectError` vs `ConnectTimeout`) — and therefore auto-start — doesn't depend on `HTTP_TIMEOUT`.
- `/health` failures (exception or non-200, e.g. a proxy's 502) are logged once per outage.
- `ensure_ready` tool → POST `/ensure_ready` (spawning the server first if needed) with `ENSURE_TIMEOUT`; attaches to or launches SolidWorks, opens no document, sets `_state_version`.

### Configuration (`config.py`, overridable in `adapters/claude/.env`)

| Variable | Default |
|---|---|
| `EXECUTION_BASE_URL` | `http://localhost:5000` — keep `localhost`; HTTP.sys answers 400 to `127.0.0.1` |
| `HTTP_TIMEOUT` | 30 s |
| `ENSURE_TIMEOUT` | 120 s (cold SolidWorks launch) |
| `CONNECT_TIMEOUT` | 10 s |
| `EXECUTION_EXE_PATH` | `<repo>/execution/solidworks/SolidworksExecution/bin/Debug/SolidworksExecution.exe` |
| `SERVER_SPAWN_TIMEOUT` | 20 s |

## Orchestration tools (adapter-side logic)

### `save_analysis(file_path)` → analysis artifact

1. Accepts `.sldprt` / `.sldasm`; sha256 of the file.
2. Opens (or activates) the document; for a part runs `analyze_model` × {features, mass_properties, geometry}; for an assembly reads components, mates, mass properties and hashes each component file.
3. Lifts every dimension named `…@…` into the flat `parameters` table; links sibling drawings by stem.
4. Writes `<folder>/.solidpilot/<filename>.analysis.json` with `identity.source_hash = "sha256:…"`, `ir: null`. See [08-analysis-and-verification.md](08-analysis-and-verification.md).

### The three IR doors share `_run_graph(graph, fresh_document)`

| Door | Input | Extra |
|---|---|---|
| `rebuild_from_ir(artifact_path)` | artifact's `ir.graph` | appends `source_stale=true` if the source file's hash no longer matches (informational, not blocking) |
| `submit_feature_graph(graph: str)` | IR JSON from the model | `INVALID_JSON` on parse failure; documented as the primary build path |
| `analyze_drawing(mode='build')` | IR lowered from a DXF/DWG | see [07-drawing-reader.md](07-drawing-reader.md) |

`_run_graph`: optionally opens a fresh document (assembly if any node is `component`/`mate`, else part) → lazily imports `ir_execution_port.run_feature_graph` (a missing compiler degrades to `FAILED | COMPILER_UNAVAILABLE`) → `pycompiler.compile_and_run` → returns `result.summary()` → `_resync_state_version()` (GET `/state`), because the compiler issued many versioned ops outside `_call`. Not transactional: a failure leaves partial geometry.

`AdapterExecutionPort` gives each sub-operation its own uuid4 and retries once after a resync on `INVALID_STATE_VERSION`. `pycompiler` imports nothing from the adapter (it's relocatable); the adapter imports it from an installed copy or falls back to inserting `compiler/solidworks` on `sys.path`.

### `compare_parts(doc_a, doc_b, detail="")` / `compare_assemblies(doc_a, doc_b)`

Objective diff used to grade an IR. Parts: topology counts + |ΔV| and |ΔA| ≤ 1 % → `verified_criteria=PASS`; `detail="faces"` lists unmatched faces (planes by normal + offset ≤ 50 µm, cylinders by axis + radius ≤ 50 µm). Assemblies: component set keyed by (source basename, config, instance ordinal), transforms (rotation ≤ 1e-6, position ≤ 1e-6 m), mate count + type multiset, ΔV/ΔA ≤ 1 %. Neither writes `ir.verification`; the caller does.

### `get_recipe(section, profile)` and resources

`cad-planner/recipe-usage.md` is split by `## <slug> — <title>` headers and served two ways, both reading the file fresh on every call (edits go live without reconnecting):

- **Resources** (13 static + 1 template): `recipe://usage/index`; `recipe://usage/<slug>` for `contract, canonicalization, forward, mapping, mapping_part, mapping_sheet_metal, mapping_assembly, verification, reverse, coverage`; `schema://feature-graph` (trimmed: `_meta.status`, `covered_subset` notes removed); `schema://analysis-artifact`; template `recipe://usage/{section}`.
- **Tool** `get_recipe(section)` — same content plus `feature_graph_schema` (`profile='part'` drops assembly types) and `analysis_artifact_schema`. It exists because most hosts give the model no way to read resources (ADR-077).

Unknown slugs raise `ResourceError` (other exceptions get their message replaced by the SDK).

## Contract tests

`tests/test_schema_contract.py` (also run standalone):

- **Tool drift** — `{tool: param names}` from `mcp.list_tools()` must equal `execution/solidworks/contracts/tool-schemas.json` (`spec.input.params`). Names only, not types. Adapter-only tools are in the JSON too.
- **Resource drift** — every recipe slug is registered and has a header; no header goes unserved; every `recipe://` / `schema://` URI written anywhere in `server.py` (docstrings included) resolves.
- **Drawing API drift** — every `_draw.<name>` used in `server.py` exists in `cad-planner/drawing.__all__`.

`tests/test_mcp_surface_regression.py` unit-tests the extracted modules with faked `call_raw` callables (state helpers, recipe sections and schema trimming, advisories/summary formatting, `save_analysis` part + assembly artifacts, `compare_*` verdicts, IR doors, and the `_call` resync-on-`INVALID_STATE_VERSION` retry).

### Dependency rule

`server.py` → extracted modules → (`state_manager`, stdlib). The extracted modules never import `server.py` and never touch `_state_version`; anything stateful (`_call_raw`, `_run_graph`, `_compare_docs`, the recognizer) is **injected** as a parameter. This keeps them testable without SolidWorks and keeps `server.<name>` monkeypatch points (used by `test_convert_features.py`) effective.

`tests/test_execution_client_http.py` stubs the HTTP client: `trust_env=False`, the connect-timeout floor, no bare `httpx.Client(` elsewhere, health-probe semantics, and log de-duplication.

## Gotchas

- **No hot reload** of `server.py`; reconnect the MCP server (resets `_state_version` to 0). Recipe/schema files are the exception.
- **`ezdxf`/numpy are imported eagerly at startup on the main thread** — importing numpy C extensions from an MCP worker thread deadlocked on Windows. A failed import degrades `analyze_drawing` to `READER_UNAVAILABLE`. `pycompiler` is imported lazily.
- **Variable-length arrays are JSON strings** at the MCP boundary (`points`, `segments`, `profiles`, `edge_indices`, `edges_json`, `features_json`, `transform_json` (13 numbers), `formats_json`, `near`, `axis`, `graph`). MCP clients stringify list-typed args, making `list` params uncallable.
- **Optional indices use `-1` sentinels** (`face_index`, `up_to_face_index`, `fixed_face_index`, `edge_index`); the wrapper sends them only when `>= 0`.
- Model-facing constraints belong here (`Literal`, Pydantic `Field`) — the model never sees `tool-schemas.json`.
- Some wrong inputs fail *silently*: `arc_center` with the wrong `direction` draws the complementary arc; extrude `reverse` and sheet-metal thickening direction don't show up in volume/area/topology. The recipe documents these.

### `dimension_plan.py`

Pure planner for `dimension_drawing`: part facts + view placement -> ordered `add_drawing_dimension_ex` calls with extent-based offset tiers (`assign_tiers`: enclosed spans go inner, disjoint spans share a tier), leader routing from alternatives, `measure_only` bolt circles folded into the hole callout (`ON Ø24 PCD`), `check_crossings` layout check and no cross-view duplicates; `execute_plan` runs it through a `call` callable passed in by `server.py`. Tests: `adapters/claude/tests/test_dimension_plan.py`.
