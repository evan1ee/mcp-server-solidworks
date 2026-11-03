# 06 · Execution Layer (`execution/solidworks/`)

C# .NET Framework 4.8, ASP.NET Web API 2 on an OWIN self-host. **The only code that touches the SolidWorks COM API**, and the authority on `state_version`. It has no AI or planning logic, and doesn't know whether the adapter or the compiler is calling it.

```
SolidworksExecution/
  Program.cs                 OWIN host on http://localhost:5000/ (hard-coded), headless; Ctrl+C to stop
  Startup.cs                 routes; JSON = camelCase, nulls omitted
  Controllers/ToolController.cs   endpoints + Dispatch switch (39 tool cases)
  Infrastructure/StaExecutor.cs   single STA thread — all COM runs here
  Infrastructure/OperationGuard.cs idempotency + state_version (in memory)
  Infrastructure/ExecLog.cs       execution.log next to the exe
  Models/                    ToolRequest, ExecutionResponse, CadState
  Services/SolidWorksService*.cs  one partial class split by tool family
contracts/                   tool-schemas.json, state-format.json, execution-response.json
```

## Endpoints

| Endpoint | Behaviour |
|---|---|
| `POST /api/tool/execute` | The one CAD endpoint. Body `{operationId, tool, stateVersion, params}`. Always HTTP 200 with an `ExecutionResponse` (including `FAILED`); 400 for a missing id/tool or unknown tool; 500 only if an exception escapes dispatch. |
| `GET /api/tool/state` | `{stateVersion}`, no check or increment — used by clients to resync. |
| `GET /health` | `status=UP`, `stateVersion`, plus a COM probe (`comAttached`, active document, SolidWorks process count/PIDs). Never 500. |
| `POST /ensure_ready` | Attach to or launch SolidWorks (below). Outside the version/idempotency envelope. Never 500. |

HTTP.sys matches on the Host header: call it as `localhost`, not `127.0.0.1`.

## Request handling

```
ToolController.Execute
  └─ StaExecutor.Instance.Run(() => Dispatch(request))   # serialized on "SolidWorksStaThread"
       └─ new SolidWorksService per request → switch(tool) → SolidWorksService.<Method>
```

Every tool method follows the same preamble:

1. `OperationGuard.IsDuplicate(opId)` → `DUPLICATE` (carries only `last_known_state_version`; the original response isn't replayed).
2. `stateVersion != current` → `FAILED INVALID_STATE_VERSION`.
3. Not attached to SolidWorks → `FAILED COM_ATTACH_FAILED`.
4. Do the work; on success build `COMPLETED` with `StateVersion = current + 1` and call `RegisterCompleted` (stores the response, increments the counter).
5. `COMException` → `COM_ERROR`; anything else → `UNEXPECTED_ERROR`.

- **Failures aren't registered**, so retrying the same `operation_id` re-executes.
- **Read-only tools** (`verify_state`, `analyze_model`, `get_selection`, `analyze_assembly`, `analyze_slddrw_test`) check the version but neither register nor bump. `activate_document` checks neither.
- **State is in memory only.** Restarting the exe resets `state_version` to 0; clients resync via `/api/tool/state`.
- **Quirk:** `save_document`, `export_document` and `batch_export` report the *current* version but still call `RegisterCompleted`, which increments the counter. The caller's next request then fails once with `INVALID_STATE_VERSION`, and the adapter recovers through its resync-and-retry.

### Why one STA thread

On the MTA OWIN threads, `InsertSheetMetalBaseFlange2` silently returned null and `add_edge_feature` deadlocked. `StaExecutor` is a singleton with one background STA thread draining a queue. `Run<T>` blocks the caller and rethrows exceptions wrapped as `InvalidOperationException("STA execution failed: …")`. All COM access is therefore serialized.

### Attaching to SolidWorks

- **Lazy:** `EnsureConnected()` → `Marshal.GetActiveObject("SldWorks.Application")`, then makes the window visible and user-controlled.
- **`EnsureReady()` never starts a second SolidWorks:** if attaching fails but a `SLDWORKS` process exists, it retries attaching for 30 s and reports likely causes (elevation mismatch, a modal/license dialog, not registered in the ROT). Only when no process exists does it `Activator.CreateInstance` and retry for up to 90 s. It returns `comAttached`, `swLaunched`, `swVersion`. It opens no document.

## Response shape

```json
{"operationId": "…", "status": "COMPLETED", "verified": true, "stateVersion": 12,
 "cadState": {"stateVersion": 12, "activeDocument": "Part1", "documentType": "PART",
              "activeSketch": null, "features": ["Boss-Extrude1"], "dimensions": []},
 "result_geometry": {"volume": 1.2e-5, "faces": 10, "edges": 24},
 "error": null}
```

- `CadState` is **rebuilt from the live document** on each response; nothing is cached server-side.
- `features` / `dimensions` double as a generic payload: read tools put **one compact JSON string in `features[0]`** (`analyze_model`, `analyze_assembly`, `get_selection`), `save`/`export` put the path, `insert_component` puts the instance name (`Part-1`) that mates reference, `create_sketch` sets `activeSketch`.
- `result_geometry` (snake_case) is an informational echo: body summary after `extrude_feature`/`create_pattern`/`modify_dimension`, the measured sketch frame after `create_sketch`, counts from drawing tools.
- `documentType` is hard-coded to `"PART"` in `verify_state`, `activate_document`, `analyze_model` and `get_selection`, whatever the actual document type.

Error codes follow `<THING>_NOT_FOUND`, `<OP>_FAILED`, `UNSUPPORTED_<X>`, plus `MISSING_PARAMETER`, `INVALID_PARAMETER`, `NO_ACTIVE_DOCUMENT`, `WRONG_DOCUMENT_TYPE`, `INVALID_STATE_VERSION`, `COM_ATTACH_FAILED`, `COM_ERROR`, `UNEXPECTED_ERROR`.

## Tool → file map

| File | Tools |
|---|---|
| `SolidWorksService.cs` (core + shared helpers) | `open_new_part`, `open_document`, `verify_state`, `close_document`, `save_document`, `export_document`, `batch_export`, `activate_document` |
| `.Sketch.cs` | `create_sketch`, `add_sketch_entity`, `add_sketch_entities`, `add_dimension`, `add_sketch_constraint`, `edit_sketch` |
| `.Features.cs` | `extrude_feature`, `create_rib`, `add_edge_feature`, `add_reference_geometry`, `create_pattern`, `set_part_material`, `modify_dimension`, `edit_feature` |
| `.SheetMetal.cs` | `sheet_metal_feature` (base_flange, edge_flange, edge_flange_sketch/finish, sketched_bend, flat_pattern) |
| `.Analyze.cs` | `analyze_model` (dispatch), `get_selection`, `FlattenFaces` / `FlattenEdges`, `IndexByJson` |
| `.Analyze.FeatureTree.cs` | `analyze_model(features)` reader: feature/sketch/dimension/pattern/equation JSON |
| `.Analyze.Topology.cs` | `analyze_model(edges|faces|bodies|feature_map)`: edge/face/body JSON, rollback-based feature attribution |
| `.Drawing.cs` | `create_drawing`, `add_drawing_view`, `add_flat_pattern_view`, `add_drawing_dimension`, `add_section_view` + view helpers |
| `.Drawing.Annotations.cs` | `auto_dimension_drawing`, `auto_center_marks`, `add_hole_callout` |
| `.Drawing.Reader.cs` | `analyze_slddrw_test` (method `AnalyzeDrawing`): view/dimension/geometry/relation/section readers |
| `.Assembly.cs` | `open_new_assembly`, `analyze_assembly`, `insert_component`, `add_mate`, `save_body_as_part` |

All files are `partial class SolidWorksService`; the split is by responsibility only (no behavior change). A new `.cs` file must be added to `SolidworksExecution.csproj` (old-style project, no globbing).

### Tool execution boundary

`SolidWorksService.ExecuteTool(...)` is the shared wrapper for a tool method: duplicate-`operationId` check → `stateVersion` validation → `EnsureConnected` → the tool body → `COMException` / `Exception` mapped to a `FAILED` response. `BuildCompletedResponse(...)` builds the standard `COMPLETED` payload. New tools should use both instead of re-implementing the preamble.

Naming trap: the C# method `AnalyzeDrawing` is the old native-SLDDRW reader, exposed as `analyze_slddrw_test`. The MCP tool `analyze_drawing` is the DXF/DWG reader and lives entirely in the adapter.

## Shared conventions and helpers

- **Units:** SI at COM (meters, radians). Tool parameters take degrees for revolve, pattern, bend and mate angles and convert internally.
- **`swconst` enums are inlined as int constants.** `SolidWorks.Interop.swconst` is referenced with `Private=False` and isn't copied to `bin/Debug`, so using an enum *type* at runtime fails to load. Casting an enum value in an expression compiles to a constant and is fine.
- **Index-based selection everywhere:** faces/edges are enumerated `GetBodies2(swSolidBody) → GetFaces()/GetEdges()`; the index is the `i` from `analyze_model(faces|edges)`. Coordinate picks (`SelectByID2` at a point) are a fragile fallback. Helpers: `SelectFaceByIndexAppend`, `FlattenFaces`/`FlattenEdges`, `FindFeatureByName`.
- **Datum planes are selected language-independently** (`SelectPlaneFlexible`): try the name, else take the 1st/2nd/3rd `RefPlane` in the tree for front/top/right. Feature names come back localized.
- **JSON-array params arrive as strings** and are parsed here (`edge_indices`, `edges_json`, `profiles`, `formats_json`, `segments`, `transform_json`).
- **Params** are a `JObject`: `p.Value<T>("key")`.

## COM gotchas worth knowing before you edit

**Always reflect the interop assembly before using an API.** The real API often differs from what looks plausible.

- **Sketch:** creation is wrapped in `SketchManager.AddToDB = true`, otherwise zoom-dependent snapping collapses short segments. `arc_center` without `direction` picks the minor arc; a wrong sign draws the complement. Sketch fillet/chamfer need `RegenerateSketchInPlace` to paint. `add_dimension` turns off `swInputDimValOnCreate`, otherwise a modal dialog blocks.
- **Extrude/cut:** cut tries the given direction, then flips on null. On sheet-metal parts the suppressed `Flat-Pattern` sits last, so cuts and mirrors roll the bar back before it, use Normal Cut, and restore. Revolve picks its axis by coordinate while the sketch is still active. Sweep needs profile mark 1 and path mark 4.
- **Rib:** `InsertRib` returns void, so success is detected by comparing the last feature name before and after.
- **Fillet:** options must include uniform radius (2), or SW2026 returns null.
- **Reference plane:** `InsertRefPlane` distance is unsigned — a negative offset needs the flip bit. Created planes/axes are hidden immediately.
- **Patterns:** linear direction 2 isn't wired (`count2` is ignored).
- **Sheet metal:** the legacy base-flange call returns null on SW2026, so it goes through `CreateDefinition` → `IBaseFlangeFeatureData` → `CreateFeature`. Thickness direction must copy the original's reverse flags. An edge flange's length is forced via `AccessSelections`/`ModifyDefinition`. A sketched bend ignores its `Flip` argument, so direction is set afterwards. Its `BendPos` encoding differs from the flange `PositionType` encoding. "Flatten" means unsuppressing `Flat-Pattern`, not `SetBendState`.
- **Documents:** `OpenDoc6` doesn't activate an already-open file, so it's followed by `ActivateDoc3` and a title check. Foreign files use a staged import (`OpenDoc6` → `LoadFile4` → enable 3D Interconnect toggle 691 and retry). `IModelDoc2.Close()` isn't implemented; use `ISldWorks.CloseDoc`. IGES must be `.igs`.
- **Assembly:** `AddComponent5` requires the part to be loaded, so it preloads with `OpenDoc6` and re-activates the assembly. The mate name is read from the MateGroup's last subfeature. `GetComponents` order is arbitrary, so tree order comes from walking `Reference` features. Mate types are mapped from enum ints, never from localized names.
- **Analyze:** `analyze_model` outputs are rounded to 6 decimals (1 µm). `features` walks the tree including one folder level and filters noise features. `feature_map` walks the rollback bar non-destructively to attribute consumed edges per feature.
- **Drawing:** dimensions are inserted via `IDrawingDoc.InsertModelAnnotations3` (`IView` has no such method). `GetLines3` returns empty; `GetPolylines7` works, but only after a rebuild + redraw. Section views must convert sheet coords with `ModelToSketchTransform`; `CreateSectionViewAt2` is disabled because it crashes SW 2026. `GetCenterMarkCount` under-reports, so **the exported PDF is the ground truth** for drawing changes. `create_drawing` is hard-coded to A3 landscape.

## Build and run

- Old-style csproj targeting `v4.8`, Compile items **listed explicitly** — a new `.cs` file needs a `<Compile Include>` entry.
- Interop DLLs are referenced from `C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\api\redist\` (`sldworks` Private=True; `swconst`, `swpublished` Private=False).
- NuGet: AspNet.WebApi 5.2.9, Microsoft.Owin 4.2.2, Newtonsoft.Json 13.0.3.
- `bin/Debug/` is committed on purpose so non-developers can run SolidPilot without building it; `execution.log` is not committed.
- CI can't build this layer (it needs the licensed interop assemblies). C# changes are verified live.

Build and restart commands: [10-development.md](10-development.md).
