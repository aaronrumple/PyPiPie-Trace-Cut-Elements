# -*- coding: utf-8 -*-
"""Trace cut elements cut by the active section into one filled region.

How it works (slice-first):
  1. Collect elements visible in the view whose bounding box crosses the cut
     plane, and ask which categories to fill (the choice is remembered).
  2. Trim each element's solids to a thin slab starting at the cut plane.
     This throws away almost all of the geometry, so the union is fast.
  3. Union the slabs. A piece that refuses is retried at other slab depths;
     anything still refusing is filled separately.
  4. Take the faces lying on the cut plane, clean out segments shorter than
     Revit allows, and draw them as filled regions tagged in Comments.
  5. On the next run in the same view, tagged regions are deleted first.

Shift+Click to configure region kind, filled-region type, and rejected-outline line style.
"""
from pyrevit import revit, DB, forms, script
from System.Collections.Generic import List

# ----------------------------------------------------------------- settings
TAG = "Trace Cut Elements"          # written to Comments; marks regions to replace

# Categories offered in the picker (only those actually cut in the view are
# shown). The first time, the ones marked True are pre-checked; after that the
# picker remembers your last choice.
CANDIDATES = [
    ("OST_Walls", True),
    ("OST_Floors", True),
    ("OST_Roofs", True),
    ("OST_Windows", True),
    ("OST_Doors", True),
    ("OST_StructuralColumns", True),
    ("OST_StructuralFraming", True),
    ("OST_StructuralFoundation", True),
    ("OST_Ceilings", True),
    ("OST_Stairs", True),
    ("OST_Columns", False),
    ("OST_StairsRailing", False),
    ("OST_CurtainWallPanels", False),
    ("OST_CurtainWallMullions", False),
    ("OST_Casework", False),
    ("OST_Furniture", False),
    ("OST_SpecialityEquipment", False),
    ("OST_GenericModel", False),
    ("OST_Toposolid", False),
]

SKIP_GLASS = False             # True = leave solids with a glass/glazing material unfilled
SLAB = 0.25                    # ft; how much geometry behind the cut plane to keep
# --------------------------------------------------------------------------

doc = revit.doc
view = doc.ActiveView
output = script.get_output()

SUPPORTED_VIEW_TYPES = (
    DB.ViewType.Section,
    DB.ViewType.Elevation,
    DB.ViewType.Detail,
    DB.ViewType.FloorPlan,
    DB.ViewType.CeilingPlan,
)
if view.ViewType not in SUPPORTED_VIEW_TYPES:
    forms.alert(
        "Open a section, elevation, detail, floor plan, or reflected ceiling plan first.",
        exitscript=True)

IS_PLAN = view.ViewType in (DB.ViewType.FloorPlan, DB.ViewType.CeilingPlan)

cfg = script.get_config()

def cfg_option(name, default=None):
    try:
        value = cfg.get_option(name, default)
    except Exception:
        value = default
    return value if value not in (None, "") else default

OUTPUT_KIND = cfg_option("output_kind", "Filled Region")
REGION_TYPE_NAME = cfg_option("region_type", None)
REGION_LINE_STYLE = cfg_option("line_style", "Claude")
REJECT_LINE_STYLE = REGION_LINE_STYLE

def filled_region_type_id(name):
    types = []
    for t in DB.FilteredElementCollector(doc).OfClass(DB.FilledRegionType):
        p = t.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
        if p and p.AsString():
            types.append((p.AsString(), t.Id))
    if not types:
        return None
    for type_name, eid in types:
        if type_name == name:
            return eid
    return types[0][1]

type_id = filled_region_type_id(REGION_TYPE_NAME) if OUTPUT_KIND == "Filled Region" else None
if OUTPUT_KIND == "Filled Region" and type_id is None:
    forms.alert("This project has no filled region types.", exitscript=True)

def region_line_style_id(name):
    if not name:
        return None
    for eid in DB.FilledRegion.GetValidLineStyleIdsForFilledRegion(doc):
        gs = doc.GetElement(eid)
        if gs is not None and gs.Name == name:
            return eid
    return None

REGION_LINE_STYLE_ID = region_line_style_id(REGION_LINE_STYLE)

PLAN_CUT_INFO = {}

def plan_cut_origin(v):
    """Return the model-space View Range Cut Plane point.

    Geometry coordinates are relative to Revit's project origin, so use
    Level.ProjectElevation rather than Level.Elevation.  Never fall back to
    View.Origin.Z for a plan/RCP.
    """
    if not IS_PLAN:
        return v.Origin

    try:
        vr = v.GetViewRange()
        level_id = vr.GetLevelId(DB.PlanViewPlane.CutPlane)
        offset = vr.GetOffset(DB.PlanViewPlane.CutPlane)
    except Exception as ex:
        forms.alert(
            "Could not read this plan/RCP View Range Cut Plane.\n\n{}".format(ex),
            exitscript=True)

    if level_id is None or level_id == DB.ElementId.InvalidElementId:
        forms.alert(
            "The plan/RCP Cut Plane does not reference a valid level.",
            exitscript=True)

    level = doc.GetElement(level_id)
    if level is None or not isinstance(level, DB.Level):
        forms.alert(
            "Could not resolve the level used by the plan/RCP Cut Plane.",
            exitscript=True)

    try:
        level_z = level.ProjectElevation
    except Exception:
        level_z = level.Elevation

    cut_z = level_z + offset
    PLAN_CUT_INFO["level_name"] = level.Name
    PLAN_CUT_INFO["level_z"] = level_z
    PLAN_CUT_INFO["offset"] = offset
    PLAN_CUT_INFO["cut_z"] = cut_z
    return DB.XYZ(v.Origin.X, v.Origin.Y, cut_z)


o = plan_cut_origin(view)

# Sections/elevations/details use the view direction exactly as before.
# A ViewPlan Cut Plane, however, is always horizontal in model coordinates.
# Do not depend on ViewDirection for the Boolean half-spaces: explicitly keep
# the thin slice immediately BELOW the View Range Cut Plane.  Floor plans and
# RCPs therefore produce the same geometric cross-section at the Cut Plane.
if IS_PLAN:
    n = DB.XYZ.BasisZ
    # CutWithHalfSpace retains the half-space opposite the plane normal.
    # Keep BELOW the Cut Plane, then ABOVE the back plane. Their overlap is
    # the thin horizontal slice: (cut Z - SLAB) <= Z <= cut Z.
    plane_cut = DB.Plane.CreateByNormalAndOrigin(n, o)
    plane_back = DB.Plane.CreateByNormalAndOrigin(
        n.Negate(), DB.XYZ(o.X, o.Y, o.Z - SLAB))
else:
    n = view.ViewDirection.Normalize()
    plane_cut = DB.Plane.CreateByNormalAndOrigin(n.Negate(), o)
    plane_back = DB.Plane.CreateByNormalAndOrigin(
        n, o.Subtract(n.Multiply(SLAB)))

short_tol = doc.Application.ShortCurveTolerance * 1.5


# ------------------------------------------------------------- crop clipping
def make_crop_solid():
    """Return a thin solid matching the active view crop rectangle.

    The crop box is expressed in its own coordinate system.  Transform its
    four XY corners to model coordinates, project them onto the section cut
    plane, then extrude the rectangle behind the cut plane by SLAB.
    """
    try:
        if not view.CropBoxActive:
            return None
        cb = view.CropBox
        if cb is None:
            return None

        tr = cb.Transform
        z = cb.Min.Z
        local = [
            DB.XYZ(cb.Min.X, cb.Min.Y, z),
            DB.XYZ(cb.Max.X, cb.Min.Y, z),
            DB.XYZ(cb.Max.X, cb.Max.Y, z),
            DB.XYZ(cb.Min.X, cb.Max.Y, z),
        ]
        pts = []
        for q in local:
            p = tr.OfPoint(q)
            # Put the crop profile exactly on the active view cut plane.
            p = p.Subtract(n.Multiply(p.Subtract(o).DotProduct(n)))
            pts.append(p)

        loop = DB.CurveLoop()
        for i in range(4):
            loop.Append(DB.Line.CreateBound(pts[i], pts[(i + 1) % 4]))

        crop_dir = n.Negate()
        return DB.GeometryCreationUtilities.CreateExtrusionGeometry(
            List[DB.CurveLoop]([loop]), crop_dir, SLAB)
    except Exception:
        return None


CROP_SOLID = make_crop_solid()


# ------------------------------------------------------------- collect
def crosses_plane(el):
    bb = el.get_BoundingBox(None)
    if bb is None:
        return False
    sides = set()
    for x in (bb.Min.X, bb.Max.X):
        for y in (bb.Min.Y, bb.Max.Y):
            for z in (bb.Min.Z, bb.Max.Z):
                sides.add(DB.XYZ(x, y, z).Subtract(o).DotProduct(n) > 0)
    return len(sides) == 2


# Resolve category names (skipping any this Revit version doesn't have)
bics = []
for name, default_on in CANDIDATES:
    bic = getattr(DB.BuiltInCategory, name, None)
    if bic is not None:
        bics.append((bic, default_on))

cat_filter = DB.ElementMulticategoryFilter(List[DB.BuiltInCategory]([b for b, _ in bics]))
by_cat = {}
for el in (DB.FilteredElementCollector(doc, view.Id)
           .WherePasses(cat_filter)
           .WhereElementIsNotElementType()):
    # Section/elevation/detail: retain the inexpensive bounding-box test.
    # Plan/RCP: a view-specific bounding box is not a reliable test against
    # the View Range Cut Plane.  Keep visible candidates here and let the
    # actual solid/half-space operation below decide whether they are cut.
    if el.Category is not None and (IS_PLAN or crosses_plane(el)):
        by_cat.setdefault(el.Category.Name, []).append(el)

if not by_cat:
    if IS_PLAN:
        forms.alert("No candidate model elements are visible in this plan/RCP view.", exitscript=True)
    else:
        forms.alert("Nothing in this view is cut by the section plane.", exitscript=True)

try:
    saved = cfg.get_option("categories", None)
except Exception:
    saved = None
saved = set(saved.split("|")) if saved else None

default_names = set()
for bic, default_on in bics:
    if default_on:
        try:
            default_names.add(doc.Settings.Categories.get_Item(bic).Name)
        except Exception:
            pass


class CatItem(forms.TemplateListItem):
    @property
    def name(self):
        return "{}  ({})".format(self.item, len(by_cat[self.item]))


items = []
for cat_name in sorted(by_cat.keys()):
    on = (cat_name in saved) if saved is not None else (cat_name in default_names)
    items.append(CatItem(cat_name, checked=on))

chosen = forms.SelectFromList.show(
    items,
    title="Trace Cut Elements: categories to fill",
    button_name="Fill",
    multiselect=True,
    width=420,
    height=520,
)
if not chosen:
    script.exit()

# Remember the choice (merging with categories not shown this time)
remember = set(chosen)
if saved is not None:
    remember |= set(c for c in saved if c not in by_cat)
cfg.categories = "|".join(sorted(remember))
script.save_config()

elements = [el for c in chosen for el in by_cat[c]]


# ------------------------------------------------------------- geometry
def iter_solids(geom):
    if geom is None:
        return
    for g in geom:
        if isinstance(g, DB.Solid):
            if g.Volume > 1e-9:
                yield g
        elif isinstance(g, DB.GeometryInstance):
            for s in iter_solids(g.GetInstanceGeometry()):
                yield s


def is_glass(solid):
    for f in solid.Faces:
        mid = f.MaterialElementId
        if mid and mid != DB.ElementId.InvalidElementId:
            mat = doc.GetElement(mid)
            if mat is not None:
                name = mat.Name.lower()
                return "glass" in name or "glaz" in name
    return False


def plan_slice_solid(solid, depth):
    """Build an explicit horizontal slab around this solid and intersect it.

    This avoids CutWithHalfSpace entirely for plan/RCP views.  The slab runs
    from (Cut Plane Z - depth) up to Cut Plane Z and only needs to cover the
    solid's XY bounding box.
    """
    bb = solid.GetBoundingBox()
    if bb is None:
        return None

    # Solid bounding-box Min/Max are in the box's LOCAL coordinate system.
    # Convert all eight corners through bb.Transform before deriving XY limits.
    # Using raw Min/Max here can place the slice slab nowhere near transformed
    # family/instance geometry even though its Z range crosses the Cut Plane.
    tr = bb.Transform
    corners = []
    for xx in (bb.Min.X, bb.Max.X):
        for yy in (bb.Min.Y, bb.Max.Y):
            for zz in (bb.Min.Z, bb.Max.Z):
                corners.append(tr.OfPoint(DB.XYZ(xx, yy, zz)))

    min_x = min(p.X for p in corners)
    max_x = max(p.X for p in corners)
    min_y = min(p.Y for p in corners)
    max_y = max(p.Y for p in corners)

    pad = max(short_tol * 4.0, 0.01)
    z0 = o.Z - depth
    pts = [
        DB.XYZ(min_x - pad, min_y - pad, z0),
        DB.XYZ(max_x + pad, min_y - pad, z0),
        DB.XYZ(max_x + pad, max_y + pad, z0),
        DB.XYZ(min_x - pad, max_y + pad, z0),
    ]
    loop = DB.CurveLoop()
    for i in range(4):
        loop.Append(DB.Line.CreateBound(pts[i], pts[(i + 1) % 4]))
    slab = DB.GeometryCreationUtilities.CreateExtrusionGeometry(
        List[DB.CurveLoop]([loop]), DB.XYZ.BasisZ, depth)
    return DB.BooleanOperationsUtils.ExecuteBooleanOperation(
        solid, slab, DB.BooleanOperationsType.Intersect)


def slab_piece(solid):
    """Return (piece, stage). stage identifies why a solid was rejected."""
    if IS_PLAN:
        # Safer plan/RCP path: intersect with an explicit solid representing
        # the View Range slice.  No half-space normal/orientation ambiguity.
        try:
            b = plan_slice_solid(solid, SLAB)
        except Exception:
            return None, "slice_error"
        if b is None or b.Volume < 1e-9:
            return None, "slice"
    else:
        a = DB.BooleanOperationsUtils.CutWithHalfSpace(solid, plane_cut)
        if a is None or a.Volume < 1e-9:
            return None, "cut"
        b = DB.BooleanOperationsUtils.CutWithHalfSpace(a, plane_back)
        if b is None or b.Volume < 1e-9:
            return None, "slab"

    # Physically clip the poche geometry to the active crop rectangle so the
    # created Filled/Masking Region never extends beyond the view crop.
    if CROP_SOLID is not None:
        try:
            b = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                b, CROP_SOLID, DB.BooleanOperationsType.Intersect)
        except Exception:
            return None, "crop_error"
        if b is None or b.Volume < 1e-9:
            return None, "crop"
    return b, None


# Full model geometry (not view-clipped) at the view's detail level
opts = DB.Options()
opts.DetailLevel = view.DetailLevel
opts.ComputeReferences = False

pieces = []
failed_elems = []
solid_count = 0
missed = 0
missed_cut = 0
missed_slab = 0
missed_crop = 0
crop_errors = 0
slice_errors = 0
missed_slice = 0
first_error = None
solid_z_min = None
solid_z_max = None
with forms.ProgressBar(
        title="Trace Cut Elements - Reading geometry {value} of {max_value}",
        cancellable=True) as pb:
    for el_index, el in enumerate(elements, 1):
        if pb.cancelled:
            script.exit()
        pb.update_progress(el_index, len(elements))
        try:
            for s in iter_solids(el.get_Geometry(opts)):
                solid_count += 1
                try:
                    sbb = s.GetBoundingBox()
                    if sbb is not None:
                        tr = sbb.Transform
                        for zz in (sbb.Min.Z, sbb.Max.Z):
                            for xx in (sbb.Min.X, sbb.Max.X):
                                for yy in (sbb.Min.Y, sbb.Max.Y):
                                    wz = tr.OfPoint(DB.XYZ(xx, yy, zz)).Z
                                    solid_z_min = wz if solid_z_min is None else min(solid_z_min, wz)
                                    solid_z_max = wz if solid_z_max is None else max(solid_z_max, wz)
                except Exception:
                    pass
                if SKIP_GLASS and is_glass(s):
                    continue
                try:
                    p, reject_stage = slab_piece(s)
                except Exception as ex:
                    p, reject_stage = None, "error"
                    failed_elems.append(el.Id)
                    first_error = first_error or str(ex)
                if p is None:
                    missed += 1
                    if reject_stage == "cut":
                        missed_cut += 1
                    elif reject_stage == "slab":
                        missed_slab += 1
                    elif reject_stage == "crop":
                        missed_crop += 1
                    elif reject_stage == "crop_error":
                        crop_errors += 1
                    elif reject_stage == "slice":
                        missed_slice += 1
                    elif reject_stage == "slice_error":
                        slice_errors += 1
                else:
                    pieces.append(p)
        except Exception as ex:
            failed_elems.append(el.Id)
            first_error = first_error or str(ex)

if not pieces:
    forms.alert(
        ("No solid geometry was found at the plan/RCP View Range Cut Plane.\n\n"
         if IS_PLAN else "No solid geometry was found at the cut plane.\n\n") +
        "Elements checked: {}\nSolids found: {}\n"
        "Outside View Range slice: {}\nSlice Boolean errors: {}\n"
        "Rejected at cut plane (section views): {}\n"
        "Rejected at back of slab (section views): {}\n"
        "Outside crop: {}\nCrop Boolean errors: {}\n"
        "Other trim errors: {}\n"
        "Cut Plane level: {}\n"
        "Level ProjectElevation: {:.4f} ft\n"
        "Cut Plane offset: {:.4f} ft\n"
        "Calculated Cut Z: {:.4f} ft\n"
        "Slice bottom Z: {:.4f} ft\n"
        "Solid geometry Z range: {}\n"
        "First error: {}".format(
            len(elements), solid_count, missed_slice, slice_errors,
            missed_cut, missed_slab, missed_crop, crop_errors,
            len(failed_elems),
            PLAN_CUT_INFO.get("level_name", "n/a"),
            PLAN_CUT_INFO.get("level_z", o.Z),
            PLAN_CUT_INFO.get("offset", 0.0), o.Z, o.Z - SLAB,
            ("{:.4f} to {:.4f} ft".format(solid_z_min, solid_z_max)
             if solid_z_min is not None and solid_z_max is not None else "unavailable"),
            first_error or "none"),
        exitscript=True)

def union(a, b):
    return DB.BooleanOperationsUtils.ExecuteBooleanOperation(
        a, b, DB.BooleanOperationsType.Union)


def thinner(piece, depth):
    """Re-trim a piece to a different depth so its back face no longer lies
    exactly on the other pieces' back faces (a common cause of union failure)."""
    if IS_PLAN:
        try:
            t = plan_slice_solid(piece, depth)
        except Exception:
            return None
    else:
        back = DB.Plane.CreateByNormalAndOrigin(n, o.Subtract(n.Multiply(depth)))
        t = DB.BooleanOperationsUtils.CutWithHalfSpace(piece, back)
    if t is None or t.Volume < 1e-9:
        return None
    return t


RETRY_DEPTHS = [SLAB * f for f in (0.61, 0.37, 0.83, 0.19)]


def try_merge(main_solid, piece):
    """Return the merged solid, or None if every attempt fails."""
    try:
        return union(main_solid, piece)
    except Exception:
        pass
    for depth in RETRY_DEPTHS:
        try:
            t = thinner(piece, depth)
            if t is not None:
                return union(main_solid, t)
        except Exception:
            pass
    return None


# Union; pieces that fail are retried at other depths, then again at the end
# once the main shape has grown. Anything still failing is filled separately.
main = None
pending = []
with forms.ProgressBar(
        title="Trace Cut Elements - Merging solids {value} of {max_value}",
        cancellable=True) as pb:
    for piece_index, p in enumerate(pieces, 1):
        if pb.cancelled:
            script.exit()
        pb.update_progress(piece_index, len(pieces))
        if main is None:
            main = p
            continue
        merged = try_merge(main, p)
        if merged is None:
            pending.append(p)
        else:
            main = merged

retried_ok = 0
progress = True
while pending and progress:
    progress = False
    still = []
    for p in pending:
        merged = try_merge(main, p)
        if merged is None:
            still.append(p)
        else:
            main = merged
            retried_ok += 1
            progress = True
    pending = still
loose = pending


# ------------------------------------------------------------- 2D outlines
def cut_faces(solid):
    faces = []
    for f in solid.Faces:
        if not isinstance(f, DB.PlanarFace):
            continue
        if abs(f.FaceNormal.DotProduct(n)) < 0.9999:
            continue
        if abs(f.Origin.Subtract(o).DotProduct(n)) > 1e-5:
            continue
        faces.append(f)
    return faces


def to_plane(p):
    return p.Subtract(n.Multiply(p.Subtract(o).DotProduct(n)))


def drop_straight_and_spikes(pts):
    """Remove points in the middle of a straight run, and zero-width spikes
    where the outline doubles back on itself (Revit rejects those)."""
    changed = True
    while changed and len(pts) > 3:
        changed = False
        count = len(pts)
        for i in range(count):
            prev_pt = pts[i - 1]
            pt = pts[i]
            next_pt = pts[(i + 1) % count]
            a = pt.Subtract(prev_pt)
            b = next_pt.Subtract(pt)
            if a.CrossProduct(b).GetLength() < 1e-6 or next_pt.DistanceTo(prev_pt) <= short_tol:
                del pts[i]
                changed = True
                break
    return pts


right = view.RightDirection
up = view.UpDirection


def uv(p):
    d = p.Subtract(o)
    return d.DotProduct(right), d.DotProduct(up)


def signed_area(pts):
    q = [uv(p) for p in pts]
    s = 0.0
    for i in range(len(q)):
        x1, y1 = q[i]
        x2, y2 = q[(i + 1) % len(q)]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def inside(pt_uv, poly):
    """Ray-casting point-in-polygon test in view (u, v) coordinates."""
    x, y = pt_uv
    q = [uv(p) for p in poly]
    hit = False
    j = len(q) - 1
    for i in range(len(q)):
        xi, yi = q[i]
        xj, yj = q[j]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                hit = not hit
        j = i
    return hit


def sample_inside(pts):
    """A point just inside the polygon, next to the middle of its longest edge."""
    best = 0
    best_len = -1.0
    for i in range(len(pts)):
        length = pts[i].DistanceTo(pts[(i + 1) % len(pts)])
        if length > best_len:
            best, best_len = i, length
    (x1, y1), (x2, y2) = uv(pts[best]), uv(pts[(best + 1) % len(pts)])
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    dx, dy = (x2 - x1) / best_len, (y2 - y1) / best_len
    side = 1.0 if signed_area(pts) > 0 else -1.0
    nudge = 0.002
    return mx - dy * nudge * side, my + dx * nudge * side


def clean_points(curve_loop):
    """The loop as points, dropping any segment shorter than Revit allows."""
    pts = []
    for c in curve_loop:
        if isinstance(c, DB.Line):
            pts.append(c.GetEndPoint(0))
        else:
            tess = list(c.Tessellate())
            pts.extend(tess[:-1])
    clean = []
    for p in pts:
        p = to_plane(p)
        if not clean or p.DistanceTo(clean[-1]) > short_tol:
            clean.append(p)
    while len(clean) > 2 and clean[-1].DistanceTo(clean[0]) <= short_tol:
        clean.pop()
    return clean


def split_at_repeats(pts):
    """Split a figure-8 (a loop that touches itself at a point) into separate loops."""
    count = len(pts)
    for i in range(count):
        for j in range(i + 2, count):
            if i == 0 and j == count - 1:
                continue
            if pts[i].DistanceTo(pts[j]) <= short_tol:
                return split_at_repeats(pts[i:j]) + split_at_repeats(pts[j:] + pts[:i])
    return [pts]


def to_curveloop(pts):
    loop = DB.CurveLoop()
    for i in range(len(pts)):
        loop.Append(DB.Line.CreateBound(pts[i], pts[(i + 1) % len(pts)]))
    return loop


def loop_area(curve_loop):
    return abs(signed_area([c.GetEndPoint(0) for c in curve_loop]))


def shrink_hole(pts):
    """Make a hole very slightly smaller so it no longer touches the outer edge."""
    base = to_curveloop(pts)
    start = abs(signed_area(pts))
    for d in (0.003, -0.003):
        try:
            off = DB.CurveLoop.CreateViaOffset(base, d, n)
            if loop_area(off) < start:
                return off
        except Exception:
            pass
    return None


def face_groups(face):
    """Sort a face's loops into [outer, [holes]] groups."""
    polys = []
    for cl in face.GetEdgesAsCurveLoops():
        for part in split_at_repeats(clean_points(cl)):
            part = drop_straight_and_spikes(part)
            if len(part) >= 3 and abs(signed_area(part)) > short_tol * short_tol:
                polys.append(part)
    polys.sort(key=lambda p: -abs(signed_area(p)))

    groups = []
    owner = {}   # poly index -> group index, for outers
    for idx, p in enumerate(polys):
        s = sample_inside(p)
        containers = [k for k in range(idx) if inside(s, polys[k])]
        if len(containers) % 2 == 0:
            owner[idx] = len(groups)
            groups.append([p, []])
        else:
            parent = containers[-1]
            if parent in owner:
                groups[owner[parent]][1].append(p)
            else:
                owner[idx] = len(groups)
                groups.append([p, []])
    return groups


def make_region(loops):
    if OUTPUT_KIND == "Masking Region":
        # Revit 2024+: masking regions are FilledRegion elements with IsMasking=True.
        region = DB.FilledRegion.CreateMaskingRegion(doc, view.Id, List[DB.CurveLoop](loops))
    else:
        region = DB.FilledRegion.Create(doc, type_id, view.Id, List[DB.CurveLoop](loops))
    if REGION_LINE_STYLE_ID is not None:
        try:
            region.SetLineStyleId(REGION_LINE_STYLE_ID)
        except Exception:
            pass
    p = region.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if p and not p.IsReadOnly:
        p.Set(TAG)
    return region


def note_reason(ex, loops_pts=None):
    if loops_pts:
        rejected_outlines.extend(loops_pts)
    msg = str(ex).strip()
    if msg not in reject_reasons:
        reject_reasons.append(msg)


def make_group_region(outer, holes):
    """Try the outline with all its holes; then with holes shrunk; then hole by hole."""
    global holes_dropped, holes_shrunk
    outer_loop = to_curveloop(outer)
    try:
        return make_region([outer_loop] + [to_curveloop(h) for h in holes])
    except Exception as ex:
        if not holes:
            note_reason(ex, [outer])
            return None
    keep = [outer_loop]
    for h in holes:
        try:
            test = make_region(keep + [to_curveloop(h)])
            doc.Delete(test.Id)
            keep.append(to_curveloop(h))
            continue
        except Exception:
            pass
        shrunk = shrink_hole(h)
        if shrunk is not None:
            try:
                test = make_region(keep + [shrunk])
                doc.Delete(test.Id)
                keep.append(shrunk)
                holes_shrunk += 1
                continue
            except Exception:
                pass
        holes_dropped += 1
    try:
        return make_region(keep)
    except Exception as ex:
        note_reason(ex, [outer] + holes)
        return None


def regions_for_solid(solid, as_one):
    """One region for everything if Revit accepts it; otherwise one per outline."""
    made, bad = [], 0
    groups = []
    for f in cut_faces(solid):
        groups.extend(face_groups(f))
    outline_count[0] += len(groups)
    if not groups:
        return made, bad
    if as_one:
        try:
            loops = []
            for outer, holes in groups:
                loops.append(to_curveloop(outer))
                loops.extend(to_curveloop(h) for h in holes)
            made.append(make_region(loops))
            return made, bad
        except Exception:
            pass
    for outer, holes in groups:
        r = make_group_region(outer, holes)
        if r is None:
            bad += 1
        else:
            made.append(r)
    if not made:
        # Fallback: the simpler per-face method from the earlier version
        for f in cut_faces(solid):
            loops = []
            for cl in f.GetEdgesAsCurveLoops():
                pts = drop_straight_and_spikes(clean_points(cl))
                if len(pts) >= 3:
                    loops.append(to_curveloop(pts))
            if loops:
                try:
                    made.append(make_region(loops))
                    bad = max(0, bad - 1)
                except Exception as ex:
                    note_reason(ex)
    return made, bad


# ------------------------------------------------------------- write to model
created = []
bad_faces = 0
reject_reasons = []
holes_dropped = 0
holes_shrunk = 0
rejected_outlines = []
outline_count = [0]
DEBUG_TAG = TAG + " (rejected outline)"
reject_group = [None]
style_missing = [False]


def safe_name(text):
    for ch in '{}[]:;|\\<>?`~':
        text = text.replace(ch, "-")
    return text


REJECT_GROUP_NAME = safe_name("Trace Cut elements rejected - " + view.Name)


def reject_line_style():
    lines_cat = doc.Settings.Categories.get_Item(DB.BuiltInCategory.OST_Lines)
    for sub in lines_cat.SubCategories:
        if sub.Name == REJECT_LINE_STYLE:
            return sub.GetGraphicsStyle(DB.GraphicsStyleType.Projection)
    style_missing[0] = True
    return None
with revit.Transaction(TAG):
    old = List[DB.ElementId]()
    # Filled and masking regions are both Autodesk.Revit.DB.FilledRegion elements.
    for fr in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.FilledRegion):
        p = fr.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p and p.AsString() == TAG:
            old.Add(fr.Id)
    for ce in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.CurveElement):
        if ce.GroupId != DB.ElementId.InvalidElementId:
            continue
        p = ce.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p and p.AsString() == DEBUG_TAG:
            old.Add(ce.Id)
    for gt in DB.FilteredElementCollector(doc).OfClass(DB.GroupType):
        p = gt.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
        if p and p.AsString() == REJECT_GROUP_NAME:
            old.Add(gt.Id)
    if old.Count:
        doc.Delete(old)

    made, bad = regions_for_solid(main, True)
    created += made
    bad_faces += bad
    for s in loose:
        made, bad = regions_for_solid(s, False)
        created += made
        bad_faces += bad

    # Draw rejected outlines as grouped detail lines so they can be inspected
    style = reject_line_style()
    line_ids = List[DB.ElementId]()
    for pts in rejected_outlines:
        for i in range(len(pts)):
            try:
                dc = doc.Create.NewDetailCurve(
                    view, DB.Line.CreateBound(pts[i], pts[(i + 1) % len(pts)]))
                if style is not None:
                    dc.LineStyle = style
                p = dc.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if p and not p.IsReadOnly:
                    p.Set(DEBUG_TAG)
                line_ids.Add(dc.Id)
            except Exception:
                pass
    if line_ids.Count:
        try:
            grp = doc.Create.NewGroup(line_ids)
            grp.GroupType.Name = REJECT_GROUP_NAME
            reject_group[0] = grp.Id
        except Exception as ex:
            note_reason(ex)

# ------------------------------------------------------------- report
if not created:
    forms.alert(
        "No {} could be created.\n\n".format(OUTPUT_KIND.lower()) +
        "Solid pieces: {}  (merged into 1, {} separate)\n"
        "Outlines found at the cut plane: {}\n"
        "Rejected outlines drawn as grouped detail lines: {}\n\n"
        "Revit said:\n{}".format(
            len(pieces), len(loose), outline_count[0], len(rejected_outlines),
            "\n".join(reject_reasons) or "nothing (no outlines reached Revit)"))
elif failed_elems or loose or bad_faces or holes_dropped or holes_shrunk:
    output.print_md("### Trace Cut Elementts: {} {}(s) created".format(len(created), OUTPUT_KIND.lower()))
    if loose:
        output.print_md("- {} piece(s) wouldn't merge and were filled separately "
                        "(they may overlap the main fill).".format(len(loose)))
    if bad_faces:
        output.print_md("- {} outline(s) were rejected by Revit and left unfilled. Revit said:".format(bad_faces))
        for r in reject_reasons:
            print("    " + r)
        if rejected_outlines:
            output.print_md("- The rejected outline(s) are drawn as detail lines in the "
                            "group \"{}\"; it's replaced on the next run.".format(REJECT_GROUP_NAME))
            if reject_group[0] is not None:
                print(output.linkify(reject_group[0]))
            if style_missing[0]:
                output.print_md("- Line style \"{}\" wasn't found, so the default "
                                "line style was used.".format(REJECT_LINE_STYLE))
    if holes_shrunk:
        output.print_md("- {} opening(s) touched the outer edge and were shrunk by about 1/32\" "
                        "so Revit would accept them.".format(holes_shrunk))
    if holes_dropped:
        output.print_md("- {} opening(s) couldn't be kept and were filled over.".format(holes_dropped))
    output.print_md("- Regions created:")
    for r in created:
        print(output.linkify(r.Id))
    if failed_elems:
        uniq = []
        for i in failed_elems:
            if i not in uniq:
                uniq.append(i)
        output.print_md("- Geometry couldn't be read or trimmed for these elements:")
        for i in uniq:
            print(output.linkify(i))