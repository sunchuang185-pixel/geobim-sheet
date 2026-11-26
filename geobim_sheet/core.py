import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

import ezdxf
import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.geolocation
from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError, ProjError
from shapely import force_2d, make_valid, transform, union_all
from shapely.errors import ShapelyError
from shapely.geometry import LineString, Point, Polygon, mapping, shape


class InputError(ValueError):
    """A problem with a command or input file that the user can resolve."""


@dataclass(frozen=True)
class Layer:
    label: str
    color: str
    fill: str | None
    aliases: tuple[str, ...]


LAYERS = {
    "terrain": Layer("Contours", "#586c62", None, ("terrain", "contours", "contour", "地形", "等高线")),
    "boundary": Layer("Site boundary", "#a9af77", "#334338", ("site boundary", "boundary", "用地", "红线", "边界", "矿界")),
    "slope": Layer("Slope / Bench edge", "#c28f78", None, ("slope", "bench", "embankment", "边坡", "台阶", "坡顶", "坡脚")),
    "roads": Layer("Roads", "#cfac79", "#574c3e", ("roads", "road", "haul road", "道路", "厂区路", "运矿路")),
    "buildings": Layer("IFC footprints", "#a4b4a9", "#52675d", ("buildings", "building", "footprints", "建筑", "构筑物", "厂房")),
    "drainage": Layer("Drainage / Flood ditch", "#9b9fc7", None, ("drainage", "drain", "stormwater", "flood", "排水", "雨水", "污水", "截洪沟", "给排水")),
    "water": Layer("Water supply", "#73c9cb", None, ("water supply", "water", "process water", "给水", "供水", "给水管线", "生产新水", "回水", "给排水")),
    "slurry": Layer("Slurry / Tailings", "#e08c5c", None, ("slurry", "tailings", "concentrate", "矿浆", "尾矿", "尾矿管", "矿浆管", "渣浆")),
    "power": Layer("Power / Cable trench", "#e5c158", None, ("power", "cable", "electrical", "供电", "电缆", "电缆沟", "高压")),
    "other": Layer("Other GIS features", "#aab8b2", None, ("other", "其他")),
}


@dataclass(frozen=True)
class Asset:
    guid: str
    tag: str
    name: str
    footprint: object
    ifc_class: str = "IfcElement"
    elevation_m: float | None = None


@dataclass(frozen=True)
class GISFeature:
    feature_id: str
    layer: str
    name: str
    geometry: object
    bury_depth_m: float | None = None
    elevation_m: float | None = None
    diameter_mm: float | None = None
    material: str | None = None
    properties: dict = field(default_factory=dict)

    @property
    def elevation_missing(self) -> bool:
        if self.layer == "slope":
            return False
        return self.bury_depth_m is None and self.elevation_m is None

    @property
    def elevation_note(self) -> str:
        if self.layer == "slope":
            return f"坡顶标高 {self.elevation_m:g} m" if self.elevation_m is not None else "边坡线"
        if self.bury_depth_m is not None:
            return f"埋深 {self.bury_depth_m:g} m"
        if self.elevation_m is not None:
            return f"高程 {self.elevation_m:g} m"
        return "待核实高程"


@dataclass(frozen=True)
class Project:
    ifc_name: str
    gis_name: str
    crs: CRS
    assets: tuple[Asset, ...]
    layers: dict
    gis_features: dict = field(default_factory=dict)

    def summary(self):
        return {
            "ifc": self.ifc_name,
            "gis": self.gis_name,
            "crs": self.crs.to_string(),
            "assets": [
                {"guid": asset.guid, "tag": asset.tag, "name": asset.name, "ifc_class": asset.ifc_class}
                for asset in self.assets
            ],
            "layers": {key: asdict(LAYERS[key]) for key in LAYERS if self.layers.get(key)},
        }


@dataclass(frozen=True)
class Selection:
    guid: str
    radius: float
    layers: tuple[str, ...]


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold()


def contains(text, phrase):
    if phrase.isascii():
        return re.search(rf"(?<![a-z0-9_-]){re.escape(phrase)}(?![a-z0-9_-])", text) is not None
    return phrase in text


def _optional_float(properties: dict, keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in properties:
            continue
        raw = properties[key]
        if raw is None or raw == "":
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(val):
            return val
    return None


def parse_crs_override(crs_override: dict | None) -> tuple[CRS, tuple[float, float], float, tuple[tuple[float, float], ...]]:
    """Validate explicit user-supplied CRS + origin/rotation override for un-georeferenced IFCs."""
    if not isinstance(crs_override, dict) or not crs_override.get("crs"):
        raise InputError("Coordinate override requires a projected EPSG CRS name, such as EPSG:4547 or EPSG:32650.")
    try:
        crs = CRS.from_user_input(str(crs_override["crs"]).strip())
        if crs.is_compound:
            crs = crs.sub_crs_list[0]
    except CRSError as error:
        raise InputError(f"Unrecognized override CRS '{crs_override['crs']}'. Use a valid EPSG code.") from error
    if not crs.is_projected or len(crs.axis_info) < 2 or any(
        not math.isclose(axis.unit_conversion_factor, 1) for axis in crs.axis_info[:2]
    ):
        raise InputError(f"Override CRS {crs.to_string()} must be a projected CRS in horizontal metres.")
    try:
        eastings = float(crs_override.get("eastings", 0.0))
        northings = float(crs_override.get("northings", 0.0))
        height = float(crs_override.get("orthogonal_height", 0.0))
        rotation_deg = float(crs_override.get("rotation_deg", 0.0))
    except (TypeError, ValueError) as error:
        raise InputError("Override eastings, northings, orthogonal_height, and rotation_deg must be numeric.") from error
    if not all(math.isfinite(v) for v in (eastings, northings, height, rotation_deg)):
        raise InputError("Override coordinate values must be finite numbers.")
    theta = math.radians(rotation_deg)
    c, s = math.cos(theta), math.sin(theta)
    vectors = ((c, s), (-s, c), (0.0, 0.0))
    return crs, (eastings, northings), height, vectors


def inspect_ifc_georeferencing(path, crs_override: dict | None = None) -> dict:
    """Inspect IFC coordinate metadata and return structured diagnostics without crashing."""
    filename = Path(path).name
    missing_info = []
    try:
        model = ifcopenshell.open(path)
    except (OSError, RuntimeError, ValueError) as error:
        return {
            "ok": False,
            "file": filename,
            "crs": None,
            "crs_string": None,
            "schema": None,
            "georeference_source": None,
            "missing_info": [
                {
                    "field": "ifc_file",
                    "source": filename,
                    "message": f"无法打开 IFC 文件 {filename}：{error}",
                    "required": "有效的 IFC STEP 文件",
                }
            ],
        }

    schema = getattr(model, "schema", "") or ""
    if crs_override:
        try:
            crs, _, _, _ = parse_crs_override(crs_override)
            return {
                "ok": True,
                "file": filename,
                "crs": crs,
                "crs_string": crs.to_string(),
                "schema": schema,
                "georeference_source": "user_override",
                "missing_info": [],
            }
        except InputError as error:
            return {
                "ok": False,
                "file": filename,
                "crs": None,
                "crs_string": None,
                "schema": schema,
                "georeference_source": "user_override",
                "missing_info": [
                    {
                        "field": "crs_override",
                        "source": filename,
                        "message": str(error),
                        "required": "有效的平面投影 EPSG 编码（如 EPSG:4547 / EPSG:32650）与原点坐标",
                    }
                ],
            }

    if not schema.startswith("IFC4"):
        missing_info.append(
            {
                "field": "ifc_schema",
                "source": filename,
                "message": f"{filename} 的 IFC Schema 为 {schema or '未知'}，缺少 IFC4/IFC4.3 地理参考支持。",
                "required": "IFC4/IFC4.3（含 IfcMapConversion），或通过 georeference_project 提供显式投影坐标系与原点配准参数",
            }
        )

    operations = model.by_type("IfcMapConversion")
    if len(operations) != 1:
        missing_info.append(
            {
                "field": "IfcMapConversion",
                "source": filename,
                "message": f"{filename} 缺少唯一有效的 IfcMapConversion（当前数量：{len(operations)}），无法确认模型平面坐标原点与换算参数。",
                "required": "在建模软件中配置 IfcMapConversion，或提供显式配准参数（如 EPSG:4547 CGCS2000 + Eastings/Northings）",
            }
        )
        return {
            "ok": False,
            "file": filename,
            "crs": None,
            "crs_string": None,
            "schema": schema,
            "georeference_source": None,
            "missing_info": missing_info,
        }

    op = operations[0]
    target_crs = op.TargetCRS
    if not target_crs or not target_crs.is_a("IfcProjectedCRS") or not target_crs.Name:
        missing_info.append(
            {
                "field": "IfcProjectedCRS.Name",
                "source": filename,
                "message": f"{filename} 的 IfcMapConversion 缺少命名的投影坐标系（IfcProjectedCRS.Name）。",
                "required": "标准 EPSG 投影坐标系名称，例如 EPSG:4547 或 EPSG:32650",
            }
        )
        return {
            "ok": False,
            "file": filename,
            "crs": None,
            "crs_string": None,
            "schema": schema,
            "georeference_source": None,
            "missing_info": missing_info,
        }

    try:
        crs = CRS.from_user_input(target_crs.Name)
        if crs.is_compound:
            crs = crs.sub_crs_list[0]
    except CRSError:
        missing_info.append(
            {
                "field": "IfcProjectedCRS.Name",
                "source": filename,
                "message": f"{filename} 中的投影坐标系名称 '{target_crs.Name}' 无法识别。",
                "required": "有效的 EPSG 编码（如 EPSG:4547 或 EPSG:32650）",
            }
        )
        return {
            "ok": False,
            "file": filename,
            "crs": None,
            "crs_string": None,
            "schema": schema,
            "georeference_source": None,
            "missing_info": missing_info,
        }

    if not crs.is_projected or len(crs.axis_info) < 2 or any(
        not math.isclose(axis.unit_conversion_factor, 1) for axis in crs.axis_info[:2]
    ):
        missing_info.append(
            {
                "field": "projected_crs_units",
                "source": filename,
                "message": f"{filename} 的坐标系 {crs.to_string()} 不是以米为单位的平面投影坐标系。",
                "required": "水平单位为米（metre）的平面投影坐标系（EPSG）",
            }
        )

    map_unit = target_crs.MapUnit
    if map_unit and (not map_unit.is_a("IfcSIUnit") or map_unit.Name != "METRE" or map_unit.Prefix):
        missing_info.append(
            {
                "field": "IfcProjectedCRS.MapUnit",
                "source": filename,
                "message": f"{filename} 的 MapUnit 与米制投影坐标系不一致。",
                "required": "IfcSIUnit(METRE)",
            }
        )

    return {
        "ok": not missing_info,
        "file": filename,
        "crs": crs if not missing_info else None,
        "crs_string": crs.to_string() if not missing_info else str(target_crs.Name),
        "schema": schema,
        "georeference_source": "ifc_embedded",
        "missing_info": missing_info,
    }


def read_ifc(path, crs_override: dict | None = None):
    try:
        model = ifcopenshell.open(path)
    except (OSError, RuntimeError, ValueError) as error:
        raise InputError("The IFC model could not be opened. Check that it is an IFC STEP file.") from error

    if crs_override is not None:
        crs, origin, origin_z, vectors = parse_crs_override(crs_override)
    else:
        if not model.schema.startswith("IFC4"):
            raise InputError("Use an IFC4 or IFC4.3 model with an embedded projected coordinate system.")
        operations = model.by_type("IfcMapConversion")
        if len(operations) != 1 or not operations[0].TargetCRS or not operations[0].TargetCRS.is_a("IfcProjectedCRS") or not operations[0].TargetCRS.Name:
            raise InputError("The IFC needs one map conversion and a named projected CRS, such as EPSG:32650. Set these in the authoring tool and export it again.")
        try:
            crs = CRS.from_user_input(operations[0].TargetCRS.Name)
            if crs.is_compound:
                crs = crs.sub_crs_list[0]
        except CRSError as error:
            raise InputError("The IFC projected CRS could not be identified. Provide an EPSG code in its CRS name.") from error
        if not crs.is_projected or len(crs.axis_info) < 2 or any(
            not math.isclose(axis.unit_conversion_factor, 1) for axis in crs.axis_info[:2]
        ):
            raise InputError("The IFC must use a projected CRS whose horizontal units are metres.")
        map_unit = operations[0].TargetCRS.MapUnit
        if map_unit and (not map_unit.is_a("IfcSIUnit") or map_unit.Name != "METRE" or map_unit.Prefix):
            raise InputError("The IFC map unit disagrees with the metre-based project CRS.")
        to_map = ifcopenshell.util.geolocation.auto_xyz2enh
        origin_xyz = to_map(model, 0, 0, 0)
        origin = origin_xyz[:2]
        origin_z = origin_xyz[2]
        unit_axes = [to_map(model, *axis)[:2] for axis in ((1, 0, 0), (0, 1, 0), (0, 0, 1))]
        vectors = tuple((east - origin[0], north - origin[1]) for east, north in unit_axes)
        if not all(math.isfinite(value) for pair in (origin, *vectors) for value in pair):
            raise InputError("The IFC map conversion contains invalid coordinates.")

    settings = ifcopenshell.geom.settings()
    settings.set("use-world-coords", True)
    settings.set("convert-back-units", True)
    candidates = [
        element for element in model.by_type("IfcElement")
        if element.Representation and not element.is_a("IfcFeatureElement")
    ]
    if not candidates:
        raise InputError("The IFC has no elements with a geometric representation.")
    iterator = ifcopenshell.geom.iterator(settings, model, 2, include=candidates)
    if not iterator.initialize():
        raise InputError("Could not read footprints from the IFC model.")
    assets = []
    while True:
        result = iterator.get()
        element = model.by_id(result.id)
        footprint = project_mesh(origin, vectors, result.geometry)
        if not footprint.is_empty:
            tag = (getattr(element, "Tag", None) or "").strip()
            name = (element.Name or tag or element.is_a()).strip()
            verts = result.geometry.verts
            z_values = [verts[i + 2] for i in range(0, len(verts), 3)] if len(verts) >= 3 else []
            elevation_m = round(float(origin_z + min(z_values)), 3) if z_values and math.isfinite(origin_z) else None
            assets.append(Asset(element.GlobalId, tag, name, footprint, element.is_a(), elevation_m))
        if not iterator.next():
            break
    if not assets:
        raise InputError("The IFC elements have no non-empty plan geometry.")
    return crs, tuple(sorted(assets, key=lambda asset: (not asset.tag, asset.tag, asset.name, asset.guid)))


def project_mesh(origin, vectors, geometry):
    vertices = geometry.verts
    projected = [
        tuple(origin[axis] + sum(vertices[i + j] * vectors[j][axis] for j in range(3)) for axis in range(2))
        for i in range(0, len(vertices), 3)
    ]
    if not all(math.isfinite(value) for point in projected for value in point):
        raise InputError("The IFC contains an invalid projected vertex.")
    faces = geometry.faces
    triangles = []
    for index in range(0, len(faces), 3):
        triangle = Polygon([projected[j] for j in faces[index:index + 3]])
        if triangle.area > 1e-8:
            triangles.append(triangle)
    return union_all(triangles)


def _match_layer_name(raw_layer: str) -> str:
    value = normalize(str(raw_layer or "")).strip()
    if not value:
        return "other"
    for key, info in LAYERS.items():
        if value == key or value in info.aliases or any(contains(value, alias) for alias in info.aliases):
            return key
    return "other"


def read_geojson_features(path, crs):
    """Read WGS 84 GeoJSON and return both raw geometry layers and metadata-rich GISFeature lists."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (UnicodeError, OSError, ValueError) as error:
        raise InputError("The GIS file could not be read. Provide UTF-8 GeoJSON.") from error
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list):
        raise InputError("The GIS file must be a GeoJSON FeatureCollection.")
    if "crs" in data:
        raise InputError("Export standard GeoJSON in WGS 84 (longitude, latitude), without a legacy crs member.")
    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    layers = {}
    features_by_layer = {}
    for index, feature in enumerate(data["features"], start=1):
        try:
            if feature.get("type") != "Feature":
                raise ValueError("Expected Feature")
            properties = feature.get("properties") or {}
            if not feature.get("geometry"):
                continue
            geometry = force_2d(shape(feature["geometry"]))
            if geometry.is_empty:
                continue
            west, south, east, north = geometry.bounds
            if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
                raise ValueError("Expected WGS 84 longitude and latitude")
            projected = make_valid(transform(geometry, transformer.transform, interleaved=False))
            if not all(math.isfinite(value) for value in projected.bounds):
                raise ValueError("Projection failed")
            layer = _match_layer_name(properties.get("layer", ""))
            layers.setdefault(layer, []).append(projected)
            feature_id = str(
                properties.get("id")
                or properties.get("pipe_id")
                or properties.get("code")
                or feature.get("id")
                or f"{layer.upper()}-{index:03d}"
            ).strip()
            name = str(properties.get("name") or properties.get("label") or feature_id).strip()
            bury_depth_m = _optional_float(
                properties,
                ("bury_depth_m", "bury_depth", "depth_m", "depth", "cover_depth_m", "埋深"),
            )
            elevation_m = _optional_float(
                properties,
                ("elevation_m", "invert_elevation_m", "invert_z", "z_m", "高程"),
            )
            diameter_mm = _optional_float(properties, ("diameter_mm", "dn", "diameter", "管径"))
            material = properties.get("material") or properties.get("材质")
            features_by_layer.setdefault(layer, []).append(
                GISFeature(
                    feature_id=feature_id,
                    layer=layer,
                    name=name,
                    geometry=projected,
                    bury_depth_m=bury_depth_m,
                    elevation_m=elevation_m,
                    diameter_mm=diameter_mm,
                    material=str(material).strip() if material else None,
                    properties=dict(properties),
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError, ProjError, ShapelyError) as error:
            raise InputError(f"GIS feature {index} has invalid geometry or coordinates. Use WGS 84 GeoJSON.") from error
    if not layers:
        raise InputError("The GIS file has no usable features.")
    return layers, features_by_layer


def read_dxf_features(path, crs):
    """Read a 2D/3D DXF utility/site drawing already in the project's projected CRS (metres)."""
    try:
        doc = ezdxf.readfile(path)
    except (OSError, ezdxf.DXFError, ValueError) as error:
        raise InputError(f"The DXF site/utility file could not be read: {error}") from error

    msp = doc.modelspace()
    # Collect nearby TEXT/MTEXT labels so utility lines can inherit human-readable names/depths if annotated
    labels: list[tuple[Point, str, str]] = []
    for entity in msp:
        etype = entity.dxftype()
        if etype in ("TEXT", "MTEXT"):
            try:
                text = entity.dxf.text if etype == "TEXT" else entity.plain_text()
                pos = entity.dxf.insert
                labels.append((Point(float(pos.x), float(pos.y)), text.strip(), entity.dxf.layer))
            except Exception:
                continue

    layers = {}
    features_by_layer = {}
    seq_by_layer: dict[str, int] = {}

    for entity in msp:
        etype = entity.dxftype()
        if etype in ("TEXT", "MTEXT"):
            continue
        raw_layer = getattr(entity.dxf, "layer", "0") or "0"
        layer = _match_layer_name(raw_layer)
        geom = None
        elev_z = None

        try:
            if etype == "LINE":
                s, e = entity.dxf.start, entity.dxf.end
                geom = LineString([(float(s.x), float(s.y)), (float(e.x), float(e.y))])
                if abs(float(s.z)) > 1e-6 or abs(float(e.z)) > 1e-6:
                    elev_z = round((float(s.z) + float(e.z)) / 2.0, 3)
            elif etype == "LWPOLYLINE":
                pts = [(float(p[0]), float(p[1])) for p in entity.get_points("xy")]
                if len(pts) >= 2:
                    if entity.closed and len(pts) >= 3:
                        geom = make_valid(Polygon(pts))
                    else:
                        geom = LineString(pts)
                elev = float(getattr(entity.dxf, "elevation", 0.0) or 0.0)
                if abs(elev) > 1e-6:
                    elev_z = round(elev, 3)
            elif etype == "POLYLINE":
                verts = list(entity.vertices)
                pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in verts]
                zs = [float(v.dxf.location.z) for v in verts]
                if len(pts) >= 2:
                    if entity.is_closed and len(pts) >= 3:
                        geom = make_valid(Polygon(pts))
                    else:
                        geom = LineString(pts)
                if zs and any(abs(z) > 1e-6 for z in zs):
                    elev_z = round(sum(zs) / len(zs), 3)
            elif etype == "POINT":
                loc = entity.dxf.location
                geom = Point(float(loc.x), float(loc.y))
                if abs(float(loc.z)) > 1e-6:
                    elev_z = round(float(loc.z), 3)
        except (ValueError, TypeError, ShapelyError):
            continue

        if geom is None or geom.is_empty or not all(math.isfinite(v) for v in geom.bounds):
            continue

        seq_by_layer[layer] = seq_by_layer.get(layer, 0) + 1
        idx = seq_by_layer[layer]
        prefix = {
            "water": "WL",
            "slurry": "SL",
            "power": "PW",
            "drainage": "DR",
            "slope": "SLP",
            "roads": "RD",
            "boundary": "BND",
            "terrain": "CNT",
        }.get(layer, "DXF")
        feature_id = f"{prefix}-DXF-{idx:02d}"
        name = f"{raw_layer} #{idx}"
        bury_depth = None

        # Match closest annotation on same DXF layer within 12m (e.g. "SL-01 DN250 矿浆管 埋深1.8m")
        best_label = None
        best_dist = 12.0
        for lpt, ltext, llayer in labels:
            if normalize(llayer) != normalize(raw_layer):
                continue
            d = float(geom.distance(lpt))
            if d < best_dist:
                best_dist = d
                best_label = ltext
        if best_label:
            name = best_label
            id_match = re.match(r"^([A-Za-z]{1,4}-\d{1,4})\b", best_label)
            if id_match:
                feature_id = id_match.group(1).upper()
            depth_match = re.search(r"(?:埋深|depth)\s*[:=]?\s*(\d+(?:\.\d+)?)\s*m?", best_label, re.IGNORECASE)
            if depth_match:
                bury_depth = float(depth_match.group(1))

        layers.setdefault(layer, []).append(geom)
        features_by_layer.setdefault(layer, []).append(
            GISFeature(
                feature_id=feature_id,
                layer=layer,
                name=name,
                geometry=geom,
                bury_depth_m=bury_depth,
                elevation_m=elev_z,
                properties={"source_format": "dxf", "dxf_layer": raw_layer, "dxf_handle": entity.dxf.handle},
            )
        )

    if not layers:
        raise InputError("The DXF file has no usable 2D/3D site or utility geometry.")
    return layers, features_by_layer


def read_site_features(path, crs):
    """Unified reader for WGS 84 GeoJSON and projected-CRS DXF site/utility layers."""
    suffix = Path(path).suffix.lower()
    if suffix == ".dxf":
        return read_dxf_features(path, crs)
    return read_geojson_features(path, crs)


def read_geojson(path, crs):
    layers, _ = read_geojson_features(path, crs)
    return layers


def load_project(ifc_path, gis_path, ifc_name, gis_name, crs_override: dict | None = None):
    crs, assets = read_ifc(ifc_path, crs_override=crs_override)
    layers, gis_features = read_site_features(gis_path, crs)
    layers.setdefault("buildings", []).extend(asset.footprint for asset in assets)
    return Project(ifc_name, gis_name, crs, assets, layers, gis_features)


def select(project, previous, prompt, layers=None):
    if not isinstance(prompt, str):
        raise InputError("Enter a drawing instruction.")
    text = normalize(prompt.strip())
    if len(text) > 600:
        raise InputError("Keep the instruction under 600 characters.")
    settings = previous or {}
    if not isinstance(settings, dict):
        raise InputError("The drawing state is not valid.")
    guid = settings.get("guid")
    try:
        radius = float(settings.get("radius", 100))
    except (ValueError, TypeError) as error:
        raise InputError("Radius must be a number in metres.") from error
    active = set(project.layers)
    previous_layers = settings.get("layers", tuple(project.layers))
    if not isinstance(previous_layers, (list, tuple)) or any(
        not isinstance(key, str) or key not in active for key in previous_layers
    ):
        raise InputError("One or more selected layers are not available in this project.")
    layer_keys = set(previous_layers)
    if layers is not None:
        if not isinstance(layers, list) or any(not isinstance(key, str) or key not in active for key in layers):
            raise InputError("One or more selected layers are not available in this project.")
        layer_keys = set(layers)

    matches = [asset for asset in project.assets if asset.tag and contains(text, normalize(asset.tag))]
    if not matches and text:
        matches = [asset for asset in project.assets if text == normalize(asset.name)]
    requested_tag = re.search(r"(?<![a-z0-9_-])[a-z][a-z0-9]*[-_]\d+[a-z0-9_-]*(?![a-z0-9_-])", text)
    if not matches and requested_tag:
        raise InputError(f"Asset tag {requested_tag.group().upper()} was not found in this project.")
    if len(matches) > 1:
        raise InputError("Choose one unambiguous IFC asset. Duplicate tags can be resolved in the asset list.")
    if matches:
        guid = matches[0].guid
    asset = next((asset for asset in project.assets if asset.guid == guid), None)
    if asset is None:
        raise InputError("Name an IFC asset by its tag or choose one from the asset list.")

    radius_matches = re.findall(r"(?<![\d.\-])(\-?\d+(?:\.\d+)?)\s*(?:米|metres?\b|meters?\b|m\b)", text)
    if radius_matches:
        radius = float(radius_matches[-1])
    if not math.isfinite(radius) or not 5 <= radius <= 1000:
        raise InputError("Choose a radius between 5 and 1,000 metres.")

    requested = {key for key, info in LAYERS.items() if any(contains(text, alias) for alias in info.aliases)}
    all_layers = re.search(r"\ball layers\b|全部图层|所有图层", text)
    if requested.difference(active):
        names = ", ".join(LAYERS[key].label for key in LAYERS if key in requested.difference(active))
        raise InputError(f"This project does not contain these layers: {names}.")
    if all_layers:
        layer_keys = active
    elif requested:
        if re.search(r"\bonly\b|只保留|仅保留|只显示", text):
            layer_keys = requested
        elif re.search(r"\b(?:hide|remove|exclude)\b|隐藏|去掉|移除", text):
            layer_keys.difference_update(requested)
        else:
            layer_keys.update(requested)
    if text and not (matches or radius_matches or requested or all_layers or re.search(r"\b(?:export|pdf|dxf)\b|导出|出图", text)):
        raise InputError("Try an asset tag, a radius such as 60 m, or a layer instruction such as ‘only water supply’.")
    return Selection(asset.guid, radius, tuple(key for key in LAYERS if key in layer_keys)), asset


def compose(project, selection, asset):
    center = asset.footprint.centroid
    clip = center.buffer(selection.radius, quad_segs=32)
    features = {}
    for key in selection.layers:
        geometries = []
        for geometry in project.layers[key]:
            if geometry.intersects(clip):
                clipped = geometry.intersection(clip)
                if not clipped.is_empty:
                    geometries.append(clipped)
        features[key] = geometries
    return center, features, asset.footprint.intersection(clip)


def geometry_json(geometry):
    return mapping(geometry)
