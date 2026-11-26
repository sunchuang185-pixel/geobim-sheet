"""Generate synthetic Demoworks and Mineworks examples bundled with GeoBIM Sheet."""

import hashlib
import json
import math
import uuid
from pathlib import Path

import ezdxf
from ezdxf import units
import ifcopenshell.guid
import numpy as np
from ifcopenshell.api import aggregate, context, geometry, georeference, project, root, spatial, unit
from pyproj import Transformer
from shapely import transform
from shapely.geometry import LineString, Polygon, mapping


DESTINATION = Path(__file__).parent
REVIEW_DIR = DESTINATION / "review"
ORIGIN = (457110, 4420500)
MINE_CRS = "EPSG:4547"  # CGCS2000 / 3-degree Gauss-Kruger CM 114E (typical Chinese mining CRS)
MINE_ORIGIN = (518400, 3426800)

BASELINE_ASSETS = (
    ("P-101", "Pump station", -11, -7, 24, 16, 6, 8),
    ("E-204", "Electrical substation", -57, 28, 25, 15, 5, 8),
    ("W-310", "Water treatment building", 31, -52, 25, 18, 7, 8),
    ("B-020", "Service hall", 25, 21, 28, 18, 8, 8),
    ("B-021", "Warehouse", -59, -40, 31, 17, 7, 8),
    ("B-022", "Utility shelter", 13, -29, 12, 9, 3, 8),
    ("B-023", "Plant services", 56, -11, 20, 13, 5, 8),
    ("B-024", "Control room", 4, 23, 12, 10, 4, 8),
    ("B-025", "Tank building", -24, 46, 17, 12, 4, 8),
    ("B-026", "Maintenance annex", -64, -15, 11, 9, 4, 8),
)

MINE_BASELINE_ASSETS = (
    ("SP-101", "Slurry pump house", -12, -6, 22, 14, 7, 5),
    ("ML-001", "Concentrator main hall", -58, 16, 42, 24, 14, 5),
    ("TK-201", "Thickener", 24, -18, 20, 20, 6, 0),
    ("WT-301", "High-level water tank", -42, 54, 18, 14, 8, 5),
    ("CR-101", "Primary crusher", -72, -32, 24, 16, 11, 5),
    ("ES-101", "35kV Substation", 28, 24, 22, 14, 6, 5),
    ("B-040", "Reagent storage", 56, -8, 18, 12, 5, 5),
)


def stable_ifc_guid(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()[:16]
    return ifcopenshell.guid.compress(uuid.UUID(bytes=digest).hex)


def build_ifc(
    output_path: Path,
    assets,
    *,
    include_crs: bool = True,
    crs_name: str = "EPSG:32650",
    origin: tuple[float, float] = ORIGIN,
    project_name: str = "Demoworks Industrial Site",
    site_name: str = "Demoworks",
    guid_overrides: dict[str, str] | None = None,
    seed_prefix: str = "demoworks-v1",
):
    guid_overrides = guid_overrides or {}
    model = project.create_file(version="IFC4")
    ifc_project = root.create_entity(model, ifc_class="IfcProject", name=project_name)
    unit.assign_unit(model, units=[unit.add_si_unit(model, unit_type="LENGTHUNIT")])
    world = context.add_context(model, context_type="Model")
    body = context.add_context(
        model,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=world,
    )
    if include_crs:
        georeference.add_georeferencing(model, name=crs_name)
        georeference.edit_georeferencing(
            model,
            coordinate_operation={"Eastings": origin[0], "Northings": origin[1], "OrthogonalHeight": 0},
        )
    site = root.create_entity(model, ifc_class="IfcSite", name=site_name)
    aggregate.assign_object(model, products=[site], relating_object=ifc_project)

    for index, spec in enumerate(assets):
        tag, name, x, y, width, depth, height, angle = spec[:8]
        ifc_class = spec[8] if len(spec) > 8 else "IfcBuildingElementProxy"
        element = root.create_entity(model, ifc_class=ifc_class, name=name)
        element.GlobalId = guid_overrides.get(tag, stable_ifc_guid(f"{seed_prefix}:{tag or name}:{index}"))
        element.Tag = tag
        spatial.assign_container(model, products=[element], relating_structure=site)
        representation = geometry.add_wall_representation(
            model, context=body, length=width, thickness=depth, height=height
        )
        geometry.assign_representation(model, product=element, representation=representation)
        theta = math.radians(angle)
        c, s = math.cos(theta), math.sin(theta)
        placement = np.array(((c, -s, 0, x), (s, c, 0, y), (0, 0, 1, 0), (0, 0, 0, 1)))
        geometry.edit_object_placement(model, product=element, matrix=placement)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    model.write(output_path)


def write_ifc():
    build_ifc(DESTINATION / "demoworks-r03.ifc", BASELINE_ASSETS, seed_prefix="demoworks-baseline")


def write_gis():
    to_wgs84 = Transformer.from_crs("EPSG:32650", "EPSG:4326", always_xy=True)
    features = []

    def add(layer, name, geometry_value, extra_props=None):
        def local_to_map(x, y):
            return to_wgs84.transform(np.asarray(x) + ORIGIN[0], np.asarray(y) + ORIGIN[1])

        props = {"layer": layer, "name": name}
        if extra_props:
            props.update(extra_props)
        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": mapping(transform(geometry_value, local_to_map, interleaved=False)),
            }
        )

    add(
        "boundary",
        "Demoworks Site",
        Polygon([(-79, -42), (-66, -69), (10, -77), (73, -66), (87, -19), (83, 48), (43, 67), (-32, 66), (-78, 45)]),
        {"id": "BND-01"},
    )
    for idx, base in enumerate(range(-140, 146, 5), start=1):
        line = [(x, base + 10 * math.sin(x / 38 + base / 31) + 0.20 * x) for x in range(-150, 151, 5)]
        add("terrain", "Synthetic contour", LineString(line), {"id": f"CNT-{idx:02d}"})
    for idx, (name, points, width) in enumerate(
        (
            ("South access", [(-130, -60), (-88, -60), (-67, -50), (-29, -47), (16, -51), (83, -32), (132, -14)], 4),
            ("East road", [(67, -38), (60, -13), (64, 10), (74, 24), (93, 36), (132, 38)], 3),
            ("North loop", [(-112, 47), (-79, 38), (-62, 22), (-37, 16), (-12, 18), (19, 15), (56, 19), (97, 29)], 3),
            ("Plant access", [(-40, -49), (-36, -23), (-23, -7), (-17, 16)], 2.2),
            ("Yard access", [(65, -6), (36, -10), (23, -19), (19, -48)], 2.2),
        ),
        start=1,
    ):
        centerline = LineString(points)
        add("roads", name, centerline.buffer(width, cap_style="flat", join_style="round"), {"id": f"RD-{idx:02d}"})
        add("roads", name + " centerline", centerline, {"id": f"RDC-{idx:02d}"})
    for name, points in (
        ("Supply loop", [(-130, 38), (-80, 31), (-67, 13), (-41, 7), (-10, 8), (17, 5), (45, 9), (64, 18), (114, 43)]),
        ("Treatment spur", [(17, 5), (23, -15), (22, -35), (34, -42), (73, -53), (116, -65)]),
        ("Pump spur", [(-5, 8), (-3, 0), (1, -1)]),
        ("West spur", [(-41, 7), (-47, -16), (-54, -22)]),
    ):
        add("water", name, LineString(points))
    add("drainage", "Drainage main", LineString([(-119, -49), (-86, -53), (-69, -43), (-29, -39), (16, -44), (61, -31), (83, -22), (114, -11)]))
    add("drainage", "Pump drain", LineString([(1, -4), (-4, -18), (-14, -28), (-22, -40)]))
    add("drainage", "East drain", LineString([(54, 14), (52, -9), (56, -23), (61, -31)]))
    (DESTINATION / "demoworks-site-r02.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=2) + "\n", encoding="utf-8"
    )


def write_review_demos():
    """Generate synthetic two-revision Demoworks IFC & GIS datasets."""
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    rev_a_assets = list(BASELINE_ASSETS)
    rev_b_assets = []
    for item in BASELINE_ASSETS:
        tag, name, x, y, w, d, h, angle = item
        if tag == "P-101":
            rev_b_assets.append((tag, name, -4.8, -2.5, w, d, h, angle))
        elif tag == "B-022":
            rev_b_assets.append((tag, name, 15.0, -27.5, w, d, h, angle))
        else:
            rev_b_assets.append(item)

    build_ifc(REVIEW_DIR / "demoworks-rev-a.ifc", rev_a_assets, project_name="Demoworks Industrial Site - Rev A", seed_prefix="demoworks-review")
    build_ifc(REVIEW_DIR / "demoworks-rev-b.ifc", rev_b_assets, project_name="Demoworks Industrial Site - Rev B", seed_prefix="demoworks-review")
    build_ifc(
        REVIEW_DIR / "demoworks-rev-b-id-changed.ifc",
        rev_b_assets,
        project_name="Demoworks Industrial Site - Rev B (Re-exported)",
        guid_overrides={"P-101": stable_ifc_guid("demoworks-review-reexported:P-101-new-guid")},
        seed_prefix="demoworks-review",
    )

    dup_rev_a = list(BASELINE_ASSETS) + [("P-102", "Pump station", 20.0, -26.0, 16, 12, 5, 8)]
    dup_rev_b = []
    for item in dup_rev_a:
        tag, name, x, y, w, d, h, angle = item
        if tag == "P-101":
            dup_rev_b.append((tag, name, -4.8, -2.5, w, d, h, angle))
        elif tag == "P-102":
            dup_rev_b.append((tag, name, 24.0, -23.0, w, d, h, angle))
        else:
            dup_rev_b.append(item)
    build_ifc(REVIEW_DIR / "demoworks-dup-rev-a.ifc", dup_rev_a, project_name="Demoworks Dual Pump Site - Rev A", seed_prefix="demoworks-dup")
    build_ifc(REVIEW_DIR / "demoworks-dup-rev-b.ifc", dup_rev_b, project_name="Demoworks Dual Pump Site - Rev B", seed_prefix="demoworks-dup")
    build_ifc(REVIEW_DIR / "demoworks-no-crs.ifc", rev_b_assets, include_crs=False, project_name="Demoworks Missing CRS - Rev B", seed_prefix="demoworks-review")

    to_wgs84 = Transformer.from_crs("EPSG:32650", "EPSG:4326", always_xy=True)
    features = []

    def add(layer, name, geometry_value, extra_props=None):
        def local_to_map(x, y):
            return to_wgs84.transform(np.asarray(x) + ORIGIN[0], np.asarray(y) + ORIGIN[1])

        props = {"layer": layer, "name": name}
        if extra_props:
            props.update(extra_props)
        features.append({"type": "Feature", "properties": props, "geometry": mapping(transform(geometry_value, local_to_map, interleaved=False))})

    add("boundary", "Demoworks Site", Polygon([(-79, -42), (-66, -69), (10, -77), (73, -66), (87, -19), (83, 48), (43, 67), (-32, 66), (-78, 45)]), {"id": "BND-01"})
    for idx, base in enumerate(range(-140, 146, 5), start=1):
        line = [(x, base + 10 * math.sin(x / 38 + base / 31) + 0.20 * x) for x in range(-150, 151, 5)]
        add("terrain", f"Contour {idx}", LineString(line), {"id": f"CNT-{idx:02d}"})
    for idx, (name, points, width) in enumerate(
        (
            ("South access", [(-130, -60), (-88, -60), (-67, -50), (-29, -47), (16, -51), (83, -32), (132, -14)], 4),
            ("East road", [(67, -38), (60, -13), (64, 10), (74, 24), (93, 36), (132, 38)], 3),
            ("North loop", [(-112, 47), (-79, 38), (-62, 22), (-37, 16), (-12, 18), (19, 15), (56, 19), (97, 29)], 3),
            ("Plant access", [(-40, -49), (-36, -23), (-23, -7), (-17, 16)], 2.2),
            ("Yard access", [(65, -6), (36, -10), (23, -19), (19, -48)], 2.2),
        ),
        start=1,
    ):
        centerline = LineString(points)
        add("roads", name, centerline.buffer(width, cap_style="flat", join_style="round"), {"id": f"RD-{idx:02d}"})
        add("roads", name + " centerline", centerline, {"id": f"RDC-{idx:02d}"})

    water_specs = (
        ("WL-01", "DN300 Supply loop", [(-130, 38), (-80, 31), (-67, 13), (-41, 7), (-10, 8), (17, 5), (45, 9), (64, 18), (114, 43)], {"diameter_mm": 300, "material": "DIP", "bury_depth_m": 1.8}),
        ("WL-02", "DN200 Pump spur", [(-5, 8), (-3, 0), (1, -1)], {"diameter_mm": 200, "material": "Steel"}),
        ("WL-03", "DN150 Pump bypass", [(-22, -13), (-8, -10), (5, -8)], {"diameter_mm": 150, "material": "HDPE", "bury_depth_m": None}),
        ("WL-04", "DN250 Treatment spur", [(17, 5), (23, -15), (22, -35), (34, -42), (73, -53), (116, -65)], {"diameter_mm": 250, "material": "DIP", "bury_depth_m": 2.1}),
        ("WL-05", "DN150 West spur", [(-41, 7), (-47, -16), (-54, -22)], {"diameter_mm": 150, "material": "HDPE", "bury_depth_m": 1.6}),
    )
    for fid, name, points, extra in water_specs:
        add("water", name, LineString(points), {"id": fid, **extra})

    drainage_specs = (
        ("DR-01", "DN400 Drainage main", [(-119, -49), (-86, -53), (-69, -43), (-29, -39), (16, -44), (61, -31), (83, -22), (114, -11)], {"diameter_mm": 400, "material": "RCP", "bury_depth_m": 2.4}),
        ("DR-02", "DN200 Pump drain", [(1, -4), (-4, -18), (-14, -28), (-22, -40)], {"diameter_mm": 200, "material": "HDPE"}),
        ("DR-03", "DN300 East drain", [(54, 14), (52, -9), (56, -23), (61, -31)], {"diameter_mm": 300, "material": "RCP", "bury_depth_m": 2.0}),
    )
    for fid, name, points, extra in drainage_specs:
        add("drainage", name, LineString(points), {"id": fid, **extra})

    (REVIEW_DIR / "demoworks-utilities.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=2) + "\n", encoding="utf-8"
    )


def write_mineworks_demos():
    """Generate CGCS2000 (EPSG:4547) mining concentrator site IFC + GeoJSON + DXF datasets."""
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    rev_a = list(MINE_BASELINE_ASSETS)
    rev_b = []
    for item in MINE_BASELINE_ASSETS:
        tag, name, x, y, w, d, h, angle = item
        if tag == "SP-101":
            # Slurry pump house shifts 6.80m toward slurry line SL-01, water line WL-01, and bench slope SLP-01
            rev_b.append((tag, name, -6.0, -2.8, w, d, h, angle))
        elif tag == "TK-201":
            rev_b.append((tag, name, 26.5, -16.5, w, d, h, angle))
        else:
            rev_b.append(item)

    build_ifc(
        REVIEW_DIR / "mineworks-rev-a.ifc",
        rev_a,
        crs_name=MINE_CRS,
        origin=MINE_ORIGIN,
        project_name="Mineworks Concentrator Site - Rev A (CGCS2000)",
        site_name="Mineworks",
        seed_prefix="mineworks-v3",
    )
    build_ifc(
        REVIEW_DIR / "mineworks-rev-b.ifc",
        rev_b,
        crs_name=MINE_CRS,
        origin=MINE_ORIGIN,
        project_name="Mineworks Concentrator Site - Rev B (CGCS2000)",
        site_name="Mineworks",
        seed_prefix="mineworks-v3",
    )
    # Un-georeferenced mining IFC (for testing explicit CGCS2000 two-step georeferencing)
    build_ifc(
        REVIEW_DIR / "mineworks-rev-b-local.ifc",
        rev_b,
        include_crs=False,
        project_name="Mineworks Concentrator Site - Rev B (Local Un-georeferenced)",
        site_name="Mineworks",
        seed_prefix="mineworks-v3",
    )

    # Duplicate slurry pump house scenario (SP-101 & SP-102)
    dup_a = list(MINE_BASELINE_ASSETS) + [("SP-102", "Slurry pump house", 18.0, -34.0, 16, 12, 6, 5)]
    dup_b = []
    for item in dup_a:
        tag, name, x, y, w, d, h, angle = item
        if tag == "SP-101":
            dup_b.append((tag, name, -6.0, -2.8, w, d, h, angle))
        elif tag == "SP-102":
            dup_b.append((tag, name, 21.5, -31.5, w, d, h, angle))
        else:
            dup_b.append(item)
    build_ifc(REVIEW_DIR / "mineworks-dup-rev-a.ifc", dup_a, crs_name=MINE_CRS, origin=MINE_ORIGIN, project_name="Mineworks Dual Slurry Pump - Rev A", site_name="Mineworks", seed_prefix="mineworks-dup")
    build_ifc(REVIEW_DIR / "mineworks-dup-rev-b.ifc", dup_b, crs_name=MINE_CRS, origin=MINE_ORIGIN, project_name="Mineworks Dual Slurry Pump - Rev B", site_name="Mineworks", seed_prefix="mineworks-dup")

    # Shared geometry specs in local metres (shifted by MINE_ORIGIN into EPSG:4547)
    boundary_poly = Polygon([(-96, -56), (-74, -78), (18, -76), (82, -52), (94, 8), (82, 62), (22, 74), (-58, 72), (-94, 40)])
    haul_roads = (
        ("Main haul road", [(-125, -48), (-82, -46), (-42, -36), (12, -38), (68, -24), (118, -8)], 5.0),
        ("Concentrator loop", [(-88, 42), (-46, 36), (-8, 28), (32, 34), (78, 42)], 3.8),
        ("Pump house access", [(-36, -36), (-28, -14), (-22, 10), (-18, 30)], 2.8),
    )
    slurry_specs = (
        ("SL-01", "DN250 High-pressure slurry main", [(-42, 12), (-14, 7), (4, 1), (26, -10), (74, -22)], {"diameter_mm": 250, "material": "Rubber-lined Steel"}),  # Missing depth -> 待核实高程
        ("SL-02", "DN300 Tailings return line", [(14, -22), (38, -28), (78, -36), (118, -42)], {"diameter_mm": 300, "material": "HDPE", "bury_depth_m": 1.6}),
    )
    water_specs = (
        ("WL-01", "DN300 Process water main", [(-44, 48), (-22, 22), (-4, 9), (18, 6), (52, 12)], {"diameter_mm": 300, "material": "Steel", "bury_depth_m": 1.9}),
        ("WL-02", "DN200 Reclaim water spur", [(-4, 9), (-2, -1), (6, -3)], {"diameter_mm": 200, "material": "Steel"}),  # Missing depth -> 待核实高程
    )
    power_specs = (
        ("PW-01", "10kV Cable trench", [(26, 20), (8, 12), (-10, 10), (-36, 14)], {"material": "Concrete trench", "bury_depth_m": 1.1}),
    )
    drainage_specs = (
        ("DR-01", "South flood intercept ditch", [(-98, -26), (-48, -22), (-6, -15), (36, -24), (88, -34)], {"material": "Masonry", "bury_depth_m": 1.5}),
    )
    slope_specs = (
        ("SLP-01", "Bench crest EL+420m", [(-85, -18), (-32, -14), (2, -11), (42, -18), (90, -28)], {"elevation_m": 420.0}),
    )

    # 1. Write WGS 84 GeoJSON
    to_wgs84 = Transformer.from_crs(MINE_CRS, "EPSG:4326", always_xy=True)
    features = []

    def add_gj(layer, name, geom_val, extra=None):
        def local_to_wgs(x, y):
            return to_wgs84.transform(np.asarray(x) + MINE_ORIGIN[0], np.asarray(y) + MINE_ORIGIN[1])

        props = {"layer": layer, "name": name}
        if extra:
            props.update(extra)
        features.append({"type": "Feature", "properties": props, "geometry": mapping(transform(geom_val, local_to_wgs, interleaved=False))})

    add_gj("boundary", "Mining industrial site boundary", boundary_poly, {"id": "BND-M01"})
    for idx, base in enumerate(range(-120, 125, 8), start=1):
        line = [(x, base + 9 * math.sin(x / 35 + base / 28) + 0.16 * x) for x in range(-135, 136, 8)]
        add_gj("terrain", f"Contour EL+{400 + idx * 2}m", LineString(line), {"id": f"CNT-M{idx:02d}"})
    for idx, (name, pts, width) in enumerate(haul_roads, start=1):
        cl = LineString(pts)
        add_gj("roads", name, cl.buffer(width, cap_style="flat", join_style="round"), {"id": f"RD-M{idx:02d}"})
    for layer_key, specs in (
        ("slurry", slurry_specs),
        ("water", water_specs),
        ("power", power_specs),
        ("drainage", drainage_specs),
        ("slope", slope_specs),
    ):
        for fid, name, pts, extra in specs:
            add_gj(layer_key, name, LineString(pts), {"id": fid, **extra})

    (REVIEW_DIR / "mineworks-utilities.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=2) + "\n", encoding="utf-8"
    )

    # 2. Write projected-CRS DXF (EPSG:4547 metres) for direct CAD utility ingestion
    doc = ezdxf.new("R2010")
    doc.units = units.M
    msp = doc.modelspace()
    dxf_layers = {
        "BOUNDARY": 3,
        "TERRAIN": 8,
        "ROADS": 30,
        "WATER": 4,
        "SLURRY": 20,
        "POWER": 2,
        "DRAINAGE": 6,
        "SLOPE": 14,
    }
    for lname, color in dxf_layers.items():
        doc.layers.new(lname, dxfattribs={"color": color})

    def shift(pts):
        return [(x + MINE_ORIGIN[0], y + MINE_ORIGIN[1]) for x, y in pts]

    msp.add_lwpolyline(shift(list(boundary_poly.exterior.coords)[:-1]), close=True, dxfattribs={"layer": "BOUNDARY"})
    for _, pts, _ in haul_roads:
        msp.add_lwpolyline(shift(pts), dxfattribs={"layer": "ROADS"})
    for lname, specs in (
        ("SLURRY", slurry_specs),
        ("WATER", water_specs),
        ("POWER", power_specs),
        ("DRAINAGE", drainage_specs),
        ("SLOPE", slope_specs),
    ):
        for fid, name, pts, extra in specs:
            spts = shift(pts)
            msp.add_lwpolyline(spts, dxfattribs={"layer": lname})
            mid = spts[len(spts) // 2]
            depth_txt = f" 埋深{extra['bury_depth_m']}m" if extra.get("bury_depth_m") is not None else ""
            msp.add_text(f"{fid} {name}{depth_txt}", dxfattribs={"height": 1.2, "insert": mid, "layer": lname})

    doc.saveas(REVIEW_DIR / "mineworks-utilities.dxf")


if __name__ == "__main__":
    write_ifc()
    write_gis()
    write_review_demos()
    write_mineworks_demos()
