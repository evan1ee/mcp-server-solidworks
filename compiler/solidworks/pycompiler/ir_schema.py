"""Structural (plan-time) validation of a Feature Graph IR.

Validates STRUCTURE only (registered types, required params present, node references resolvable,
units correct, anchors well-formed) — NOT geometry. Geometric validity (does the top face exist,
does an anchor resolve to exactly one edge) is the compiler/resolver/execution's job at
compile/exec time (ADR-004). If this returns errors, the compiler performs ZERO execution calls.

Covered vocabulary (must stay in sync with cad-planner/contracts/feature-graph.schema.json,
enforced by pycompiler/tests/test_ir_schema_contract.py):
  Part nodes:
    box, sketch, extrude, revolve, sweep, rib, hole, fillet, chamfer, loft,
    linear_pattern, circular_pattern, sheet_metal, sketched_bend, mirror, edge_flange
  Assembly nodes:
    component, mate
  Graph-level part properties:
    material

To add a new IR node type:
  1. Add the type token to NODE_TYPES (and FEATURE_PRODUCERS / BODY_PRODUCERS if applicable).
  2. Define a `_validate_<type>(node, label, ctx, errors)` function below.
  3. Register it in `_NODE_VALIDATORS`.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

DATUMS = frozenset(("front", "top", "right"))
# Assembly sub-vocabulary (v0.6.0 — Phase B, ADR-047): component + mate. An assembly graph is
# components first (tree order) then mates (creation order); part and assembly node types never
# mix in one graph (a component references its part FILE — part IRs are not embedded).
ASSEMBLY_NODE_TYPES = frozenset(("component", "mate"))
MATE_TYPES = frozenset(("coincident", "concentric", "perpendicular", "parallel", "tangent",
                        "distance", "angle", "lock"))
MATE_ALIGNMENTS = frozenset(("aligned", "anti_aligned", "closest"))
# Anchor kinds mirror the reader's swMateEntity2ReferenceType_e names verbatim (C7). The kind
# decides WHERE the resolver searches: face kinds vs edge kinds.
ANCHOR_KINDS_FACE = frozenset(("plane", "cylinder", "cone", "sphere"))
ANCHOR_KINDS_EDGE = frozenset(("line", "circle"))
ANCHOR_KINDS = ANCHOR_KINDS_FACE | ANCHOR_KINDS_EDGE
NODE_TYPES = frozenset(("box", "sketch", "extrude", "revolve", "sweep", "rib", "hole", "fillet",
                        "chamfer", "loft", "linear_pattern", "circular_pattern", "sheet_metal",
                        "sketched_bend", "mirror", "edge_flange")) \
    | ASSEMBLY_NODE_TYPES
PROFILE_KINDS = frozenset(("rectangle", "circle", "line", "arc", "ellipse", "spline"))
# Optional boolean flags any profile primitive may carry (mirrors analyze_model's per-segment flag).
PROFILE_FLAGS = frozenset(("construction",))
FACE_SELECTORS = frozenset(("top",))          # v0.5: only 'top'
POSITIONS = frozenset(("center",))            # v0.5: only 'center'
THROUGH_DEPTHS = frozenset(("through_all", "through"))
EXTRUDE_ENDS = frozenset(("blind", "through_all", "up_to_surface", "mid_plane"))
# Canonical model axes for linear_pattern's direction (the tool selects the default plane whose
# normal is that axis; 'flip' patterns toward the negative axis).
PATTERN_DIRECTIONS = frozenset(("x", "y", "z"))
BODY_PRODUCERS = frozenset(("box", "extrude", "revolve", "sweep"))  # node types whose body a 'hole' may reference
# Node types that CREATE a named feature — valid seeds for a linear/circular pattern / mirror
# (the compiler substitutes the seed's runtime-created feature name). Patterns themselves are
# producers too: a grid composes as a pattern OF a pattern (the D2-less linear tool).
FEATURE_PRODUCERS = frozenset(("box", "extrude", "revolve", "sweep", "rib", "hole", "fillet", "chamfer",
                               "loft", "linear_pattern", "circular_pattern", "sheet_metal",
                               "sketched_bend", "mirror", "edge_flange"))
CHAMFER_TYPES = frozenset(("distance_angle", "distance_distance"))
BEND_POSITIONS = frozenset(("centerline", "material_inside", "material_outside", "bend_outside"))
# Edge flange adds 'bend_sharp' (swFlangePositionTypes_e 5) to the shared position set.
EDGE_FLANGE_POSITIONS = BEND_POSITIONS | frozenset(("bend_sharp",))


@dataclass
class _ValidationContext:
    """Mutable context threaded across sequential node validation."""
    nodes: List[Any]
    pos: int
    seen: Dict[str, str]
    seen_mate: bool = False

    @property
    def prev_node(self) -> Optional[Dict[str, Any]]:
        if self.pos > 0 and isinstance(self.nodes[self.pos - 1], dict):
            return self.nodes[self.pos - 1]
        return None


def _is_pos_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_point3(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 3 and all(_is_number(c) for c in v)


def _is_frame3(v: Any) -> bool:
    return isinstance(v, dict) and all(_is_point3(v.get(k)) for k in ("origin", "xdir", "ydir"))


def _check_adjacent_sketch(
    node: Dict[str, Any],
    label: str,
    ctx: _ValidationContext,
    errors: List[str],
    *,
    required_desc: str = "id of a sketch node",
    adj_noun: str,
) -> Optional[str]:
    """Verify `node['sketch']` references the immediately preceding sketch node."""
    sk = node.get("sketch")
    prev = ctx.prev_node
    if not isinstance(sk, str) or not sk:
        errors.append("%s: 'sketch' (%s) is required." % (label, required_desc))
        return None
    if ctx.seen.get(sk) != "sketch":
        errors.append("%s: 'sketch' must reference an earlier sketch node (got '%s')." % (label, sk))
        return sk
    if not (prev is not None and prev.get("id") == sk):
        errors.append("%s: %s must immediately follow its %s '%s'."
                      % (label, adj_noun, "PROFILE sketch node" if adj_noun == "a sweep" else "sketch node", sk))
    return sk


def _check_edge_anchors(node: Dict[str, Any], label: str, errors: List[str]) -> None:
    """Validate `node['edges']` as a non-empty list of `{near: [x, y, z], hint?}` anchors."""
    edges = node.get("edges")
    if not isinstance(edges, list) or len(edges) == 0:
        errors.append("%s: 'edges' must be a non-empty array of edge anchors "
                      "({near:[x,y,z], hint?})." % label)
        return
    for j, e in enumerate(edges):
        if not isinstance(e, dict) or not _is_point3(e.get("near")):
            errors.append("%s.edges[%d]: 'near' must be a 3D point [x, y, z] in meters."
                          % (label, j))


def _check_feature_seed(node: Dict[str, Any], label: str, seen: Dict[str, str], errors: List[str]) -> None:
    """Validate `node['feature']` references an earlier FEATURE_PRODUCERS node."""
    tgt = node.get("feature")
    if not isinstance(tgt, str) or not tgt:
        errors.append("%s: 'feature' (id of an earlier feature-producing node — the pattern seed) "
                      "is required." % label)
    elif seen.get(tgt) not in FEATURE_PRODUCERS:
        errors.append("%s: 'feature' must reference an earlier %s node (got '%s')."
                      % (label, "/".join(sorted(FEATURE_PRODUCERS)), tgt))


def _check_pattern_count(node: Dict[str, Any], label: str, errors: List[str]) -> None:
    count = node.get("count")
    if not (isinstance(count, int) and not isinstance(count, bool) and count >= 2):
        errors.append("%s: 'count' must be an integer >= 2 (total instances incl. the seed)." % label)


# ---------------------------------------------------------------------------
# Per-node structural validators
# ---------------------------------------------------------------------------

def _validate_box(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    _check_datum(node, label, errors)
    for k in ("width", "depth", "height"):
        if not _is_pos_number(node.get(k)):
            errors.append("%s: '%s' must be a positive number (meters)." % (label, k))


def _validate_sketch(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    _check_datum(node, label, errors)
    profile = node.get("profile")
    if not isinstance(profile, list) or len(profile) == 0:
        errors.append("%s: 'profile' must be a non-empty array of primitives." % label)
    else:
        for j, prim in enumerate(profile):
            _check_profile_prim(prim, "%s.profile[%d]" % (label, j), errors)
    frame = node.get("frame")
    if frame is not None and not _is_frame3(frame):
        errors.append("%s: 'frame' must be {origin:[x,y,z], xdir:[x,y,z], ydir:[x,y,z]} "
                      "— the ORIGINAL sketch's frame from analyze_model(sketch)." % label)


def _validate_extrude(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    _check_adjacent_sketch(node, label, ctx, errors, adj_noun="an extrude")
    op = node.get("operation")
    if op not in ("boss", "cut"):
        errors.append("%s: 'operation' must be 'boss' or 'cut' (got %r)." % (label, op))
    if node.get("reversed") is not None and not isinstance(node.get("reversed"), bool):
        errors.append("%s: 'reversed' must be a boolean (the recipe's flipped-direction flag)." % label)
    _check_extrude_end(node, op, label, errors)


def _validate_hole(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    ref = node.get("ref") or {}
    nf = ref.get("node_face") or {}
    tgt = nf.get("node")
    if not isinstance(tgt, str) or not tgt:
        errors.append("%s: ref.node_face.node (an earlier node id) is required." % label)
    elif ctx.seen.get(tgt) not in BODY_PRODUCERS:
        errors.append("%s: ref.node_face.node '%s' must reference an earlier box/extrude node." % (label, tgt))
    if nf.get("selector") not in FACE_SELECTORS:
        errors.append("%s: ref.node_face.selector %r unsupported in v0.5 (only %s)."
                      % (label, nf.get("selector"), sorted(FACE_SELECTORS)))
    if ref.get("position") not in POSITIONS:
        errors.append("%s: ref.position %r unsupported in v0.5 (only %s)."
                      % (label, ref.get("position"), sorted(POSITIONS)))
    if not _is_pos_number(node.get("diameter")):
        errors.append("%s: 'diameter' must be a positive number (meters)." % label)
    depth = node.get("depth")
    if depth not in THROUGH_DEPTHS:
        errors.append("%s: 'depth' must be 'through_all' in v0.5 (got %r)." % (label, depth))


def _validate_revolve(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    _check_adjacent_sketch(node, label, ctx, errors, adj_noun="a revolve")
    axis = node.get("axis")
    if not isinstance(axis, dict) or not all(_is_number(axis.get(k)) for k in ("x1", "y1", "x2", "y2")):
        errors.append("%s: 'axis' must be {x1, y1, x2, y2} — the axis SEGMENT's exact 2D "
                      "sketch coords (normally the construction centerline)." % label)
    elif axis.get("x1") == axis.get("x2") and axis.get("y1") == axis.get("y2"):
        errors.append("%s: axis start and end coincide — not a segment." % label)
    angle = node.get("angle")
    if angle is not None and not _is_pos_number(angle):
        errors.append("%s: 'angle' must be a positive number (RADIANS; omit for a full 360°)." % label)


def _validate_rib(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    _check_adjacent_sketch(node, label, ctx, errors, adj_noun="a rib")
    if not _is_pos_number(node.get("thickness")):
        errors.append("%s: 'thickness' must be a positive number (meters)." % label)
    for k in ("two_sided", "reverse_thickness_dir", "reverse_material_dir", "is_norm_to_sketch"):
        if node.get(k) is not None and not isinstance(node.get(k), bool):
            errors.append("%s: '%s' must be a boolean." % (label, k))


def _validate_sweep(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    sk = _check_adjacent_sketch(
        node, label, ctx, errors,
        required_desc="id of the PROFILE sketch node",
        adj_noun="a sweep",
    )
    path = node.get("path")
    if not isinstance(path, str) or not path:
        errors.append("%s: 'path' (id of an EARLIER sketch node holding the sweep path) "
                      "is required." % label)
    elif ctx.seen.get(path) != "sketch":
        errors.append("%s: 'path' must reference an earlier sketch node (got '%s')." % (label, path))
    elif path == sk:
        errors.append("%s: 'path' and 'sketch' (profile) must be two DIFFERENT sketch nodes." % label)


def _validate_linear_pattern(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    _check_feature_seed(node, label, ctx.seen, errors)
    if node.get("direction") not in PATTERN_DIRECTIONS:
        errors.append("%s: 'direction' must be one of %s (got %r)."
                      % (label, sorted(PATTERN_DIRECTIONS), node.get("direction")))
    if not _is_pos_number(node.get("spacing")):
        errors.append("%s: 'spacing' must be a positive number (meters between instances)." % label)
    _check_pattern_count(node, label, errors)
    if node.get("flip") is not None and not isinstance(node.get("flip"), bool):
        errors.append("%s: 'flip' must be a boolean (pattern toward the negative axis)." % label)


def _validate_circular_pattern(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    _check_feature_seed(node, label, ctx.seen, errors)
    axis = node.get("axis")
    datums = axis.get("datums") if isinstance(axis, dict) else None
    if (not isinstance(datums, list) or len(datums) != 2
            or any(d not in DATUMS for d in datums) or datums[0] == datums[1]):
        errors.append("%s: axis.datums must be two DISTINCT canonical datums out of %s."
                      % (label, sorted(DATUMS)))
    _check_pattern_count(node, label, errors)
    angle = node.get("angle_deg")
    if not _is_pos_number(angle) or angle > 360:
        errors.append("%s: 'angle_deg' must be a number in (0, 360]." % label)
    eq = node.get("equal_spacing")
    if eq is not None and not isinstance(eq, bool):
        errors.append("%s: 'equal_spacing' must be a boolean." % label)


def _validate_fillet(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    if not _is_pos_number(node.get("radius")):
        errors.append("%s: 'radius' must be a positive number (meters)." % label)
    _check_edge_anchors(node, label, errors)


def _validate_chamfer(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    ctype = node.get("chamfer_type", "distance_angle")
    if ctype not in CHAMFER_TYPES:
        errors.append("%s: 'chamfer_type' must be one of %s (got %r)."
                      % (label, sorted(CHAMFER_TYPES), ctype))
    if not _is_pos_number(node.get("distance")):
        errors.append("%s: 'distance' (first-face setback D1) must be a positive number (meters)." % label)
    if ctype == "distance_distance":
        if not _is_pos_number(node.get("distance2")):
            errors.append("%s: chamfer_type 'distance_distance' requires 'distance2' "
                          "(second-face setback D2) as a positive number (meters)." % label)
    else:  # distance_angle
        angle = node.get("angle")
        if angle is not None and not _is_pos_number(angle):
            errors.append("%s: 'angle' must be a positive number (RADIANS; omit for 45°)." % label)
    if node.get("flip") is not None and not isinstance(node.get("flip"), bool):
        errors.append("%s: 'flip' must be a boolean (distance_distance side swap)." % label)
    _check_edge_anchors(node, label, errors)


def _validate_sheet_metal(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    _check_adjacent_sketch(node, label, ctx, errors, adj_noun="a sheet_metal base flange")
    if not _is_pos_number(node.get("thickness")):
        errors.append("%s: 'thickness' must be a positive number (meters)." % label)
    if node.get("bend_radius") is not None and not _is_pos_number(node.get("bend_radius")):
        errors.append("%s: 'bend_radius' must be a positive number (meters) when present." % label)
    kf = node.get("k_factor")
    if kf is not None and not (_is_number(kf) and 0 < kf < 1):
        errors.append("%s: 'k_factor' must be a number in (0, 1) when present." % label)
    if node.get("reverse_thickness") is not None and not isinstance(node.get("reverse_thickness"), bool):
        errors.append("%s: 'reverse_thickness' must be a boolean." % label)
    if node.get("symmetric_thickness") is not None and not isinstance(node.get("symmetric_thickness"), bool):
        errors.append("%s: 'symmetric_thickness' must be a boolean." % label)


def _validate_sketched_bend(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    _check_adjacent_sketch(
        node, label, ctx, errors,
        required_desc="id of the bend-line sketch node",
        adj_noun="a sketched_bend",
    )
    angle = node.get("angle")
    if angle is not None and not _is_pos_number(angle):
        errors.append("%s: 'angle' must be a positive number (RADIANS; omit for 90°)." % label)
    if node.get("radius") is not None and not _is_pos_number(node.get("radius")):
        errors.append("%s: 'radius' must be a positive number (meters); OMIT it to use the "
                      "sheet's default bend radius." % label)
    posn = node.get("position")
    if posn is not None and posn not in BEND_POSITIONS:
        errors.append("%s: 'position' must be one of %s (got %r)."
                      % (label, sorted(BEND_POSITIONS), posn))
    if node.get("flip") is not None and not isinstance(node.get("flip"), bool):
        errors.append("%s: 'flip' must be a boolean." % label)
    fixed = node.get("fixed")
    if not isinstance(fixed, dict) or not _is_point3(fixed.get("near")):
        errors.append("%s: 'fixed' must be {near:[x,y,z], hint?} — a 3D point ON the sheet "
                      "face, on the SIDE that stays put (PRE-bend geometry)." % label)


def _validate_edge_flange(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    edge = node.get("edge")
    if not isinstance(edge, dict) or not _is_point3(edge.get("near")):
        errors.append("%s: 'edge' must be {near:[x,y,z], hint?} — a 3D point ON the attach "
                      "edge (the reader's edge `mid`)." % label)
    angle = node.get("angle")
    if angle is not None and not _is_pos_number(angle):
        errors.append("%s: 'angle' must be a positive number (RADIANS; omit for 90°)." % label)
    length = node.get("length")
    frame = node.get("frame")
    profile = node.get("profile")
    if length is not None:
        # MODE B — simple length flange.
        if not _is_pos_number(length):
            errors.append("%s: 'length' must be a positive number (meters)." % label)
        if frame is not None or profile is not None:
            errors.append("%s: 'length' (simple mode) and 'frame'/'profile' (custom-profile "
                          "mode) are mutually exclusive." % label)
        for k in ("radius", "position", "flip"):
            if node.get(k) is not None:
                errors.append("%s: '%s' is not supported in length mode (the simple flange "
                              "uses the sheet default radius, position material_inside, no "
                              "flip) — use the frame+profile mode for it." % (label, k))
    else:
        # MODE A — custom profile (the original v0.5.6 contract).
        if not _is_frame3(frame):
            errors.append("%s: 'frame' is REQUIRED {origin, xdir, ydir} — the ORIGINAL profile "
                          "sketch's frame; the generated sketch's frame is unpredictable, so "
                          "the coordinate transform is mandatory (or give 'length' for the "
                          "simple full-width flange)." % label)
        if not isinstance(profile, list) or len(profile) == 0:
            errors.append("%s: 'profile' must be a non-empty array of primitives (the ORIGINAL "
                          "profile sketch's segments, original 2D coords)." % label)
        else:
            for j, prim in enumerate(profile):
                _check_profile_prim(prim, "%s.profile[%d]" % (label, j), errors)
        if node.get("radius") is not None and not _is_pos_number(node.get("radius")):
            errors.append("%s: 'radius' must be a positive number (meters); OMIT it to use the "
                          "sheet's default bend radius." % label)
        posn = node.get("position")
        if posn is not None and posn not in EDGE_FLANGE_POSITIONS:
            errors.append("%s: 'position' must be one of %s (got %r)."
                          % (label, sorted(EDGE_FLANGE_POSITIONS), posn))
        if node.get("flip") is not None and not isinstance(node.get("flip"), bool):
            errors.append("%s: 'flip' must be a boolean." % label)


def _validate_mirror(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    plane = node.get("plane")
    datum = plane.get("datum") if isinstance(plane, dict) else None
    if datum not in DATUMS:
        errors.append("%s: plane.datum must be one of %s (got %r)."
                      % (label, sorted(DATUMS), datum))
    feats = node.get("features")
    if not isinstance(feats, list) or len(feats) == 0:
        errors.append("%s: 'features' must be a non-empty array of earlier "
                      "feature-producing node ids." % label)
    else:
        for fid in feats:
            if not isinstance(fid, str) or not fid:
                errors.append("%s: each mirrored feature must be a node id (non-empty string)." % label)
            elif ctx.seen.get(fid) not in FEATURE_PRODUCERS:
                errors.append("%s: feature '%s' must reference an earlier %s node."
                              % (label, fid, "/".join(sorted(FEATURE_PRODUCERS))))


def _validate_component(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    src = node.get("source")
    if not isinstance(src, dict) or not isinstance(src.get("path"), str) or not src.get("path"):
        errors.append("%s: 'source' must be {path: '<part file>', hash?: 'sha256:...'}." % label)
    elif src.get("hash") is not None and not isinstance(src.get("hash"), str):
        errors.append("%s: source.hash must be a string when present." % label)
    tr = node.get("transform")
    if (not isinstance(tr, list) or len(tr) != 13
            or not all(_is_number(v) for v in tr)):
        errors.append("%s: 'transform' must be 13 numbers (9 rotation row-major + "
                      "3 translation (meters) + scale) — the reader's exact layout." % label)
    if node.get("fixed") is not None and not isinstance(node.get("fixed"), bool):
        errors.append("%s: 'fixed' must be a boolean." % label)
    if node.get("config") is not None and not isinstance(node.get("config"), str):
        errors.append("%s: 'config' must be a string (referenced configuration name)." % label)
    if ctx.seen_mate:
        errors.append("%s: components must come BEFORE all mates (components in tree "
                      "order, then mates in creation order)." % label)


def _validate_mate(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    ctx.seen_mate = True
    mt = node.get("mate_type")
    if mt not in MATE_TYPES:
        errors.append("%s: 'mate_type' must be one of %s (got %r)."
                      % (label, sorted(MATE_TYPES), mt))
    al = node.get("alignment")
    if al is not None and al not in MATE_ALIGNMENTS:
        errors.append("%s: 'alignment' must be one of %s (got %r)."
                      % (label, sorted(MATE_ALIGNMENTS), al))
    if mt in ("distance", "angle") and not _is_number(node.get("value")):
        errors.append("%s: mate_type '%s' requires 'value' (SI — meters / RADIANS, C4)."
                      % (label, mt))
    if node.get("flip") is not None and not isinstance(node.get("flip"), bool):
        errors.append("%s: 'flip' must be a boolean." % label)
    sides = node.get("sides")
    if not isinstance(sides, list) or len(sides) != 2:
        errors.append("%s: 'sides' must be exactly 2 entries "
                      "[{component: <node id>, anchor: {kind, near, hint?}}, ...]." % label)
    else:
        for j, side in enumerate(sides):
            swhere = "%s.sides[%d]" % (label, j)
            if not isinstance(side, dict):
                errors.append("%s must be an object." % swhere)
                continue
            cid = side.get("component")
            if not isinstance(cid, str) or not cid:
                errors.append("%s: 'component' (an earlier component node id) is required." % swhere)
            elif ctx.seen.get(cid) != "component":
                errors.append("%s: component '%s' must reference an earlier component node." % (swhere, cid))
            anchor = side.get("anchor")
            if not isinstance(anchor, dict) or not _is_point3(anchor.get("near")):
                errors.append("%s: anchor must be {kind, near:[x,y,z] (COMPONENT-LOCAL "
                              "coords, meters), dir?, radius?, hint?}." % swhere)
            else:
                if anchor.get("kind") not in ANCHOR_KINDS:
                    errors.append("%s: anchor.kind must be one of %s (the reader's entity "
                                  "kind, verbatim — got %r)."
                                  % (swhere, sorted(ANCHOR_KINDS), anchor.get("kind")))
                if anchor.get("dir") is not None and not _is_point3(anchor.get("dir")):
                    errors.append("%s: anchor.dir must be a 3D unit vector when present." % swhere)
                if anchor.get("radius") is not None and not _is_pos_number(anchor.get("radius")):
                    errors.append("%s: anchor.radius must be a positive number when present." % swhere)


def _validate_loft(node: Dict[str, Any], label: str, ctx: _ValidationContext, errors: List[str]) -> None:
    profiles = node.get("profiles")
    if not isinstance(profiles, list) or len(profiles) < 2:
        errors.append("%s: 'profiles' must be an array of >= 2 sketch node ids "
                      "(the loft profiles, in order)." % label)
    else:
        for pid in profiles:
            if not isinstance(pid, str) or not pid:
                errors.append("%s: each profile must be a sketch node id (non-empty string)." % label)
            elif ctx.seen.get(pid) != "sketch":
                errors.append("%s: profile '%s' must reference an earlier sketch node." % (label, pid))


_NodeValidator = Callable[[Dict[str, Any], str, _ValidationContext, List[str]], None]

_NODE_VALIDATORS: Dict[str, _NodeValidator] = {
    "box": _validate_box,
    "sketch": _validate_sketch,
    "extrude": _validate_extrude,
    "hole": _validate_hole,
    "revolve": _validate_revolve,
    "rib": _validate_rib,
    "sweep": _validate_sweep,
    "linear_pattern": _validate_linear_pattern,
    "circular_pattern": _validate_circular_pattern,
    "fillet": _validate_fillet,
    "chamfer": _validate_chamfer,
    "sheet_metal": _validate_sheet_metal,
    "sketched_bend": _validate_sketched_bend,
    "edge_flange": _validate_edge_flange,
    "mirror": _validate_mirror,
    "component": _validate_component,
    "mate": _validate_mate,
    "loft": _validate_loft,
}


def validate(graph: Any) -> List[str]:
    """Return a list of human-readable error strings ([] == structurally valid)."""
    errors: List[str] = []
    if not isinstance(graph, dict):
        return ["graph must be a JSON object."]

    if graph.get("units") != "meters":
        errors.append("units must be 'meters' (got %r)." % graph.get("units"))

    nodes = graph.get("nodes")
    if not isinstance(nodes, list) or len(nodes) == 0:
        errors.append("graph.nodes must be a non-empty array.")
        return errors  # nothing more to check without nodes

    # Part vs assembly vocabularies never mix in one graph: an assembly's parts are separate
    # FILES (verified by their own V1 loop, ADR-047b), never inline sub-graphs.
    kinds = set()
    for node in nodes:
        if isinstance(node, dict) and node.get("type") in NODE_TYPES:
            kinds.add("assembly" if node.get("type") in ASSEMBLY_NODE_TYPES else "part")
    if len(kinds) > 1:
        errors.append("graph mixes part-vocabulary and assembly-vocabulary nodes — an assembly "
                      "IR references part FILES (component.source), it never embeds part nodes.")

    # Graph-level material (v0.7.0): document state, not a tree feature — PART graphs only.
    material = graph.get("material")
    if material is not None:
        if not isinstance(material, dict) or not isinstance(material.get("name"), str) \
                or not material.get("name"):
            errors.append("graph.material must be {name: '<exact library material name>', "
                          "library?: '<database>'} when present.")
        elif material.get("library") is not None and not isinstance(material.get("library"), str):
            errors.append("graph.material.library must be a string when present.")
        if "assembly" in kinds:
            errors.append("graph.material is PART-only — set_part_material requires a part "
                          "document, not an assembly.")

    ctx = _ValidationContext(nodes=nodes, pos=0, seen={})
    for pos, node in enumerate(nodes):
        ctx.pos = pos
        where = "nodes[%d]" % pos
        if not isinstance(node, dict):
            errors.append("%s must be an object." % where)
            continue

        nid = node.get("id")
        if not isinstance(nid, str) or not nid:
            errors.append("%s.id is required (non-empty string)." % where)
            nid = None
        elif nid in ctx.seen:
            errors.append("%s.id '%s' is duplicated." % (where, nid))

        ntype = node.get("type")
        if ntype not in NODE_TYPES:
            errors.append("%s.type '%s' is not a registered v0.5 type %s."
                          % (where, ntype, sorted(NODE_TYPES)))
            if nid:
                ctx.seen[nid] = ntype
            continue

        label = "%s (%s '%s')" % (where, ntype, nid)
        validator = _NODE_VALIDATORS.get(ntype)
        if validator is not None:
            validator(node, label, ctx, errors)

        if nid:
            ctx.seen[nid] = ntype

    return errors


def _check_extrude_end(node: Dict[str, Any], op: Any, label: str, errors: List[str]) -> None:
    """End-condition rules. Back-compat: no 'end' field behaves like blind/through_all."""
    end = node.get("end")
    if end is not None and end not in EXTRUDE_ENDS:
        errors.append("%s: 'end' %r unsupported (only %s)." % (label, end, sorted(EXTRUDE_ENDS)))
        return
    if end == "up_to_surface":
        up_to = node.get("up_to") or {}
        face = up_to.get("face") if isinstance(up_to, dict) else None
        if not isinstance(face, dict) or not _is_point3(face.get("near")):
            errors.append("%s: end 'up_to_surface' requires up_to.face.near = [x, y, z] "
                          "(a point ON the terminating face's plane, meters)." % label)
        return
    if end == "through_all":
        return
    if end == "mid_plane":
        if not _is_pos_number(node.get("depth")):
            errors.append("%s: end 'mid_plane' requires 'depth' = the TOTAL symmetric width "
                          "(positive meters)." % label)
        return
    # end == 'blind' or omitted -> the legacy rules
    if end == "blind" or end is None:
        is_through = (node.get("through") is True or node.get("depth") in THROUGH_DEPTHS)
        if end == "blind" and is_through:
            errors.append("%s: end 'blind' contradicts through/through_all depth." % label)
            return
        if op == "boss" and not is_through and not _is_pos_number(node.get("depth")):
            errors.append("%s: 'depth' (positive meters) is required for a blind boss extrude." % label)
        if op == "cut" and not (is_through or _is_pos_number(node.get("depth"))):
            errors.append("%s: a cut needs 'through': true, depth 'through_all', or a positive blind depth." % label)


def _check_datum(node: Dict[str, Any], label: str, errors: List[str]) -> None:
    ref = node.get("ref")
    if ref is None:
        return  # default datum 'top', offset 0
    if not isinstance(ref, dict):
        errors.append("%s: 'ref' must be an object." % label)
        return
    # A sketch may sit on a NON-axis-aligned support (a bent flange's face) via a face
    # anchor instead of datum+offset — {face: {near:[x,y,z], hint?}}. Mutually exclusive with datum.
    face = ref.get("face")
    if face is not None:
        if not isinstance(face, dict) or not _is_point3(face.get("near")):
            errors.append("%s: ref.face must be {near:[x,y,z], hint?} — a point ON the support "
                          "face's plane (from the ORIGINAL's analyze read)." % label)
        if ref.get("datum") is not None or ref.get("offset") is not None:
            errors.append("%s: ref.face and ref.datum/offset are mutually exclusive." % label)
        return
    datum = ref.get("datum", "top")
    if datum not in DATUMS:
        errors.append("%s: ref.datum '%s' must be one of %s." % (label, datum, sorted(DATUMS)))
    offset = ref.get("offset")
    if offset is not None and not _is_number(offset):
        errors.append("%s: ref.offset must be a signed number (meters)." % label)


def _check_profile_prim(prim: Any, label: str, errors: List[str]) -> None:
    if not isinstance(prim, dict):
        errors.append("%s must be an object." % label)
        return
    kind = prim.get("kind")
    if kind not in PROFILE_KINDS:
        errors.append("%s.kind '%s' unsupported in v0.5 (only %s)." % (label, kind, sorted(PROFILE_KINDS)))
        return
    for flag in sorted(PROFILE_FLAGS):
        if prim.get(flag) is not None and not isinstance(prim.get(flag), bool):
            errors.append("%s: '%s' must be a boolean." % (label, flag))
    if kind == "rectangle":
        for k in ("width", "height"):
            if not _is_pos_number(prim.get(k)):
                errors.append("%s: rectangle '%s' must be a positive number." % (label, k))
    elif kind == "circle":
        if not _is_pos_number(prim.get("diameter")):
            errors.append("%s: circle 'diameter' must be a positive number." % label)
        for k in ("cx", "cy"):
            if prim.get(k) is not None and not _is_number(prim.get(k)):
                errors.append("%s: circle '%s' must be a number." % (label, k))
    elif kind == "line":
        for k in ("x1", "y1", "x2", "y2"):
            if not _is_number(prim.get(k)):
                errors.append("%s: line '%s' must be a number (exact 2D sketch coords, meters)." % (label, k))
    elif kind == "arc":
        for k in ("cx", "cy", "x1", "y1", "x2", "y2"):
            if not _is_number(prim.get(k)):
                errors.append("%s: arc '%s' must be a number (exact 2D sketch coords, meters)." % (label, k))
        if prim.get("dir") not in (1, -1):
            errors.append("%s: arc 'dir' must be 1 (CCW) or -1 (CW) — centre+start+end alone "
                          "describe two arcs." % label)
    elif kind == "ellipse":
        for k in ("cx", "cy", "x1", "y1", "x2", "y2"):
            if not _is_number(prim.get(k)):
                errors.append("%s: ellipse '%s' must be a number (centre / major-axis point / "
                              "minor-axis point, exact 2D sketch coords)." % (label, k))
    elif kind == "spline":
        pts = prim.get("points")
        if (not isinstance(pts, list) or len(pts) < 4 or len(pts) % 2 != 0
                or not all(_is_number(v) for v in pts)):
            errors.append("%s: spline 'points' must be a FLAT number list [x1, y1, x2, y2, ...] "
                          "with >= 2 points (even count)." % label)
