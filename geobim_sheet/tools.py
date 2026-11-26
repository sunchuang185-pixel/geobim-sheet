"""Independently testable GIS/BIM revision review tools shared by MCP server and Web Agent."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

from pyproj import CRS

from .core import (
    Asset,
    GISFeature,
    InputError,
    LAYERS,
    contains,
    inspect_ifc_georeferencing,
    normalize,
    read_ifc,
    read_site_features,
)
from .exports import comparison_dxf_bytes, comparison_pdf_bytes, issues_csv_bytes
from .rules import UTILITY_REVIEW_LAYERS, evaluate_clearance


DEMO_REVIEW_DIR = Path(__file__).with_name("demo") / "review"

DEMO_SCENARIOS: dict[str, dict[str, str]] = {
    "mineworks": {
        "label": "矿山选矿厂正常换版（CGCS2000 · 渣浆泵房 SP-101 位移 + 矿浆/给水/边坡核查）",
        "label_en": "Mineworks concentrator revision (CGCS2000 EPSG:4547 · SP-101 moved + slurry/water/slope rules)",
        "rev_a": "mineworks-rev-a.ifc",
        "rev_b": "mineworks-rev-b.ifc",
        "gis": "mineworks-utilities.geojson",
    },
    "mineworks_dxf": {
        "label": "矿山选矿厂 DXF 管网直读（CGCS2000 · 直接读取 CAD DXF 管网总图）",
        "label_en": "Mineworks direct CAD DXF utility layer ingestion (CGCS2000 EPSG:4547)",
        "rev_a": "mineworks-rev-a.ifc",
        "rev_b": "mineworks-rev-b.ifc",
        "gis": "mineworks-utilities.dxf",
    },
    "mineworks_dup": {
        "label": "矿山同名设备（两座渣浆泵房 SP-101 / SP-102 触发反问续跑）",
        "label_en": "Mineworks duplicate slurry pump houses (SP-101 and SP-102)",
        "rev_a": "mineworks-dup-rev-a.ifc",
        "rev_b": "mineworks-dup-rev-b.ifc",
        "gis": "mineworks-utilities.geojson",
    },
    "normal": {
        "label": "通用厂区正常换版（泵房 P-101 位移 + 给水管线缺埋深）",
        "label_en": "Normal revision (P-101 moved + water lines missing bury depth)",
        "rev_a": "demoworks-rev-a.ifc",
        "rev_b": "demoworks-rev-b.ifc",
        "gis": "demoworks-utilities.geojson",
    },
    "id_changed": {
        "label": "ID 变化（P-101 重导出后 GlobalId 变更）",
        "label_en": "GlobalId changed on re-export (P-101 matched by Tag + IFC class)",
        "rev_a": "demoworks-rev-a.ifc",
        "rev_b": "demoworks-rev-b-id-changed.ifc",
        "gis": "demoworks-utilities.geojson",
    },
    "duplicate_pumps": {
        "label": "同名设备（两个名为 Pump station 的泵房 P-101 / P-102）",
        "label_en": "Duplicate asset names (two pump stations: P-101 and P-102)",
        "rev_a": "demoworks-dup-rev-a.ifc",
        "rev_b": "demoworks-dup-rev-b.ifc",
        "gis": "demoworks-utilities.geojson",
    },
    "missing_crs": {
        "label": "坐标缺失（Rev B 缺少 IfcMapConversion / 投影坐标系）",
        "label_en": "Missing coordinates (Rev B lacks IfcMapConversion / projected CRS)",
        "rev_a": "demoworks-rev-a.ifc",
        "rev_b": "demoworks-no-crs.ifc",
        "gis": "demoworks-utilities.geojson",
    },
}

BILINGUAL_ASSET_SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("渣浆泵房", "矿浆泵房", "泵房", "水泵房", "给水泵房", "slurry pump house", "pump station", "pump house", "pump"),
    ("选矿主厂房", "选矿厂", "磨浮", "concentrator main hall", "concentrator"),
    ("浓密机", "浓缩机", "thickener"),
    ("高位水池", "水池", "水箱", "储罐", "high-level water tank", "tank building", "tank"),
    ("粗碎站", "破碎站", "primary crusher", "crusher"),
    ("变电所", "变电站", "配电室", "35kv substation", "electrical substation", "substation"),
    ("药剂库", "reagent storage"),
    ("水处理", "净水", "water treatment"),
    ("综合楼", "服务楼", "service hall"),
    ("仓库", "库房", "warehouse"),
    ("设备间", "公用间", "utility shelter"),
    ("厂务", "plant services"),
    ("控制室", "中控室", "control room"),
    ("维修", "检修", "maintenance annex", "maintenance"),
)


@dataclass
class RevisionProject:
    project_id: str
    rev_a_name: str
    rev_b_name: str
    gis_name: str
    rev_a_path: str
    rev_b_path: str
    gis_path: str
    crs: CRS | None
    rev_a_assets: tuple[Asset, ...]
    rev_b_assets: tuple[Asset, ...]
    layers: dict
    gis_features: dict[str, list[GISFeature]]
    missing_info: list[dict]
    scenario: str | None = None
    georeference_source: str = "ifc_embedded"

    @property
    def crs_string(self) -> str:
        return self.crs.to_string() if self.crs else "UNKNOWN"

    @property
    def coordinates_valid(self) -> bool:
        return self.crs is not None and not any(item.get("blocking", True) for item in self.missing_info)


def default_data_dir() -> Path:
    env_dir = os.environ.get("GEOBIM_DATA_DIR", "").strip()
    if env_dir:
        base = Path(env_dir)
    else:
        base = Path.cwd() / "data" / "geobim_review"
    base.mkdir(parents=True, exist_ok=True)
    return base


class ReviewWorkspace:
    """In-memory project registry + disk output directory shared across tools."""

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._projects: dict[str, RevisionProject] = {}
        self._lock = Lock()

    def put_project(self, project: RevisionProject) -> None:
        with self._lock:
            self._projects[project.project_id] = project

    def get_project(self, project_id: str) -> RevisionProject | None:
        with self._lock:
            return self._projects.get(project_id)

    def require_project(self, project_id: str) -> RevisionProject:
        project = self.get_project(project_id)
        if project is None:
            raise InputError(f"未找到项目 {project_id}，请先调用 open_project 打开项目。")
        return project


DEFAULT_WORKSPACE = ReviewWorkspace()


def _resolve_layer_key(layer_name: str) -> str:
    text = normalize(layer_name or "water").strip()
    if text in ("all", "全部", "所有", "所有管线", "全部管线", "管网", "管线"):
        return "all"
    for key, info in LAYERS.items():
        if text == key or any(contains(text, alias) for alias in info.aliases):
            return key
    return text


def _expand_query_terms(query: str) -> list[str]:
    norm = normalize(query or "").strip()
    if not norm:
        return []
    terms = [norm]
    for group in BILINGUAL_ASSET_SYNONYMS:
        if any(contains(norm, alias) or alias in norm for alias in group):
            terms.extend(group)
    seen = set()
    ordered = []
    for item in terms:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _asset_matches_query(asset: Asset, query: str, tag: str | None = None, ifc_class: str | None = None) -> bool:
    if tag and normalize(asset.tag) != normalize(tag):
        return False
    if ifc_class and normalize(asset.ifc_class) != normalize(ifc_class):
        return False
    if not query or not query.strip():
        return True
    terms = _expand_query_terms(query)
    haystack_tag = normalize(asset.tag)
    haystack_name = normalize(asset.name)
    haystack_class = normalize(asset.ifc_class)
    for term in terms:
        if haystack_tag and (term == haystack_tag or contains(haystack_tag, term) or contains(term, haystack_tag)):
            return True
        if term in haystack_name or contains(haystack_name, term):
            return True
        if term in haystack_class:
            return True
    return False


def _asset_dict(asset: Asset, revision: str) -> dict:
    centroid = asset.footprint.centroid
    minx, miny, maxx, maxy = asset.footprint.bounds
    return {
        "guid": asset.guid,
        "tag": asset.tag,
        "name": asset.name,
        "ifc_class": asset.ifc_class,
        "revision": revision,
        "centroid_xy": [round(centroid.x, 3), round(centroid.y, 3)],
        "bbox": [round(minx, 3), round(miny, 3), round(maxx, 3), round(maxy, 3)],
        "area_m2": round(float(asset.footprint.area), 2),
        "elevation_m": asset.elevation_m,
    }


def _footprint_orientation_deg(geom) -> float:
    try:
        rect = geom.minimum_rotated_rectangle
        coords = list(rect.exterior.coords)
        if len(coords) < 3:
            return 0.0
        best_len = -1.0
        best_angle = 0.0
        for i in range(len(coords) - 1):
            dx = coords[i + 1][0] - coords[i][0]
            dy = coords[i + 1][1] - coords[i][1]
            length = math.hypot(dx, dy)
            if length > best_len:
                best_len = length
                best_angle = math.degrees(math.atan2(dy, dx)) % 180.0
        return best_angle
    except Exception:
        return 0.0


def _compute_displacement(asset_a: Asset, asset_b: Asset) -> dict:
    ca = asset_a.footprint.centroid
    cb = asset_b.footprint.centroid
    dx = cb.x - ca.x
    dy = cb.y - ca.y
    dist = math.hypot(dx, dy)
    rot_a = _footprint_orientation_deg(asset_a.footprint)
    rot_b = _footprint_orientation_deg(asset_b.footprint)
    rot_diff = abs(rot_b - rot_a)
    if rot_diff > 90:
        rot_diff = 180 - rot_diff
    area_a = float(asset_a.footprint.area)
    area_b = float(asset_b.footprint.area)
    return {
        "dx_m": round(dx, 3),
        "dy_m": round(dy, 3),
        "displacement_m": round(dist, 3),
        "rotation_diff_deg": round(rot_diff, 2),
        "area_rev_a_m2": round(area_a, 2),
        "area_rev_b_m2": round(area_b, 2),
        "area_diff_m2": round(area_b - area_a, 2),
        "centroid_rev_a": [round(ca.x, 3), round(ca.y, 3)],
        "centroid_rev_b": [round(cb.x, 3), round(cb.y, 3)],
    }


def open_project(
    rev_a_path: str | None = None,
    rev_b_path: str | None = None,
    gis_path: str | None = None,
    preset: str | None = None,
    project_id: str | None = None,
    crs_override: dict | None = None,
    workspace: ReviewWorkspace = DEFAULT_WORKSPACE,
) -> dict:
    """Open a two-revision IFC + GIS/DXF review project and validate CRS (supports explicit crs_override)."""
    scenario = None
    if preset:
        if preset not in DEMO_SCENARIOS:
            return {
                "status": "error",
                "error": f"未知演示场景 '{preset}'，可选值：{', '.join(DEMO_SCENARIOS)}。",
                "missing_info": [],
            }
        scenario = preset
        spec = DEMO_SCENARIOS[preset]
        rev_a_path = str(DEMO_REVIEW_DIR / spec["rev_a"])
        rev_b_path = str(DEMO_REVIEW_DIR / spec["rev_b"])
        gis_path = str(DEMO_REVIEW_DIR / spec["gis"])
    elif not (rev_a_path and rev_b_path and gis_path):
        scenario = "mineworks"
        spec = DEMO_SCENARIOS["mineworks"]
        rev_a_path = rev_a_path or str(DEMO_REVIEW_DIR / spec["rev_a"])
        rev_b_path = rev_b_path or str(DEMO_REVIEW_DIR / spec["rev_b"])
        gis_path = gis_path or str(DEMO_REVIEW_DIR / spec["gis"])

    pid = project_id or f"rev-{uuid4().hex[:8]}"
    missing_info: list[dict] = []

    for label, fpath in (("rev_a", rev_a_path), ("rev_b", rev_b_path), ("gis", gis_path)):
        if not Path(fpath).is_file():
            missing_info.append(
                {
                    "field": f"{label}_path",
                    "source": str(fpath),
                    "blocking": True,
                    "message": f"找不到输入文件 {label}: {fpath}",
                    "required": "存在的本地文件路径",
                }
            )
    if missing_info:
        return {
            "status": "error",
            "project_id": pid,
            "missing_info": missing_info,
            "error": "; ".join(item["message"] for item in missing_info),
        }

    # Check native IFC georeferencing first; only apply crs_override to whichever revision needs it (or both if specified)
    native_a = inspect_ifc_georeferencing(rev_a_path)
    native_b = inspect_ifc_georeferencing(rev_b_path)
    override_a = crs_override if (crs_override and (not native_a["ok"] or crs_override.get("apply_to_both"))) else None
    override_b = crs_override if (crs_override and (not native_b["ok"] or crs_override.get("apply_to_both"))) else None

    diag_a = inspect_ifc_georeferencing(rev_a_path, crs_override=override_a) if override_a else native_a
    diag_b = inspect_ifc_georeferencing(rev_b_path, crs_override=override_b) if override_b else native_b

    for item in diag_a["missing_info"]:
        missing_info.append({**item, "revision": "rev_a", "blocking": True})
    for item in diag_b["missing_info"]:
        missing_info.append({**item, "revision": "rev_b", "blocking": True})

    crs_a = diag_a["crs"]
    crs_b = diag_b["crs"]
    if crs_a and crs_b and crs_a != crs_b:
        missing_info.append(
            {
                "field": "crs_mismatch",
                "source": f"{diag_a['file']} vs {diag_b['file']}",
                "blocking": True,
                "message": f"两版 IFC 投影坐标系不一致：Rev A 为 {crs_a.to_string()}，Rev B 为 {crs_b.to_string()}，无法直接叠加比对。",
                "required": "将两版 IFC 统一到相同的平面投影坐标系（EPSG）或通过 georeference_project 提供显式配准参数",
            }
        )

    rev_a_name = Path(rev_a_path).name
    rev_b_name = Path(rev_b_path).name
    gis_name = Path(gis_path).name

    if any(item.get("blocking") for item in missing_info):
        project = RevisionProject(
            project_id=pid,
            rev_a_name=rev_a_name,
            rev_b_name=rev_b_name,
            gis_name=gis_name,
            rev_a_path=str(Path(rev_a_path).resolve()),
            rev_b_path=str(Path(rev_b_path).resolve()),
            gis_path=str(Path(gis_path).resolve()),
            crs=None,
            rev_a_assets=(),
            rev_b_assets=(),
            layers={},
            gis_features={},
            missing_info=missing_info,
            scenario=scenario,
        )
        workspace.put_project(project)
        return {
            "status": "incomplete_coordinates",
            "project_id": pid,
            "crs": None,
            "revisions": {
                "rev_a": {"file": rev_a_name, "crs": diag_a["crs_string"], "coordinates_ok": diag_a["ok"]},
                "rev_b": {"file": rev_b_name, "crs": diag_b["crs_string"], "coordinates_ok": diag_b["ok"]},
            },
            "gis": {"file": gis_name},
            "missing_info": missing_info,
            "summary": "无法确认项目坐标系，已停止空间比对并列出缺失项（可调用 georeference_project 提供 EPSG + 原点配准参数）。",
            "traceability": {
                "project_id": pid,
                "rev_a_file": rev_a_name,
                "rev_b_file": rev_b_name,
                "gis_file": gis_name,
            },
        }

    try:
        crs, rev_a_assets = read_ifc(rev_a_path, crs_override=override_a)
        _, rev_b_assets = read_ifc(rev_b_path, crs_override=override_b)
        layers, gis_features = read_site_features(gis_path, crs)
    except InputError as error:
        missing_info.append(
            {
                "field": "gis_or_ifc_coordinates",
                "source": gis_name,
                "blocking": True,
                "message": str(error),
                "required": "符合规范的 IFC4（含 IfcMapConversion 或显式配准）与 WGS 84 GeoJSON / 投影米制 DXF",
            }
        )
        project = RevisionProject(
            project_id=pid,
            rev_a_name=rev_a_name,
            rev_b_name=rev_b_name,
            gis_name=gis_name,
            rev_a_path=str(Path(rev_a_path).resolve()),
            rev_b_path=str(Path(rev_b_path).resolve()),
            gis_path=str(Path(gis_path).resolve()),
            crs=None,
            rev_a_assets=(),
            rev_b_assets=(),
            layers={},
            gis_features={},
            missing_info=missing_info,
            scenario=scenario,
        )
        workspace.put_project(project)
        return {
            "status": "incomplete_coordinates",
            "project_id": pid,
            "crs": None,
            "missing_info": missing_info,
            "summary": f"输入文件坐标或几何校验失败：{error}",
            "traceability": {
                "project_id": pid,
                "rev_a_file": rev_a_name,
                "rev_b_file": rev_b_name,
                "gis_file": gis_name,
            },
        }

    non_blocking_missing = []
    for layer_key in UTILITY_REVIEW_LAYERS:
        for feat in gis_features.get(layer_key, []):
            if feat.elevation_missing:
                non_blocking_missing.append(
                    {
                        "field": "bury_depth_m",
                        "blocking": False,
                        "source": gis_name,
                        "layer": layer_key,
                        "utility_id": feat.feature_id,
                        "utility_name": feat.name,
                        "flag": "待核实高程",
                        "message": f"图层 {layer_key} 中的管线 {feat.feature_id} ({feat.name}) 缺少埋深/高程信息，已标记为“待核实高程”。",
                    }
                )

    georef_source = "user_override" if (override_a or override_b) else "ifc_embedded"
    project = RevisionProject(
        project_id=pid,
        rev_a_name=rev_a_name,
        rev_b_name=rev_b_name,
        gis_name=gis_name,
        rev_a_path=str(Path(rev_a_path).resolve()),
        rev_b_path=str(Path(rev_b_path).resolve()),
        gis_path=str(Path(gis_path).resolve()),
        crs=crs,
        rev_a_assets=rev_a_assets,
        rev_b_assets=rev_b_assets,
        layers=layers,
        gis_features=gis_features,
        missing_info=non_blocking_missing,
        scenario=scenario,
        georeference_source=georef_source,
    )
    workspace.put_project(project)

    return {
        "status": "ok",
        "project_id": pid,
        "scenario": scenario,
        "crs": project.crs_string,
        "georeference_source": georef_source,
        "revisions": {
            "rev_a": {"file": rev_a_name, "asset_count": len(rev_a_assets)},
            "rev_b": {"file": rev_b_name, "asset_count": len(rev_b_assets)},
        },
        "gis": {
            "file": gis_name,
            "format": "dxf" if gis_name.lower().endswith(".dxf") else "geojson",
            "layers": {key: len(feats) for key, feats in gis_features.items()},
            "missing_elevation_count": len(non_blocking_missing),
        },
        "missing_info": non_blocking_missing,
        "traceability": {
            "project_id": pid,
            "rev_a_file": rev_a_name,
            "rev_b_file": rev_b_name,
            "gis_file": gis_name,
            "crs": project.crs_string,
            "georeference_source": georef_source,
        },
    }


def georeference_project(
    project_id: str,
    crs: str,
    eastings: float,
    northings: float,
    orthogonal_height: float = 0.0,
    rotation_deg: float = 0.0,
    apply_to_both: bool = False,
    workspace: ReviewWorkspace = DEFAULT_WORKSPACE,
) -> dict:
    """Apply explicit user-supplied CRS + origin/rotation override to an un-georeferenced project."""
    existing = workspace.require_project(project_id)
    override = {
        "crs": crs,
        "eastings": eastings,
        "northings": northings,
        "orthogonal_height": orthogonal_height,
        "rotation_deg": rotation_deg,
        "apply_to_both": apply_to_both,
    }
    return open_project(
        rev_a_path=existing.rev_a_path,
        rev_b_path=existing.rev_b_path,
        gis_path=existing.gis_path,
        project_id=project_id,
        crs_override=override,
        workspace=workspace,
    )


def list_layers(project_id: str, workspace: ReviewWorkspace = DEFAULT_WORKSPACE) -> dict:
    """List available GIS/DXF/BIM layers, feature counts, CRS, and utility elevation completeness."""
    project = workspace.require_project(project_id)
    if not project.coordinates_valid:
        return {
            "status": "incomplete_coordinates",
            "project_id": project_id,
            "crs": None,
            "layers": [],
            "missing_info": project.missing_info,
        }

    layer_entries = []
    for key, features in project.gis_features.items():
        missing_elev = [
            {"utility_id": f.feature_id, "name": f.name, "flag": "待核实高程"}
            for f in features
            if key in UTILITY_REVIEW_LAYERS and f.elevation_missing
        ]
        layer_entries.append(
            {
                "layer": key,
                "label": LAYERS[key].label if key in LAYERS else key,
                "feature_count": len(features),
                "missing_elevation_count": len(missing_elev),
                "missing_elevation_features": missing_elev,
                "sample_features": [
                    {
                        "id": f.feature_id,
                        "name": f.name,
                        "bury_depth_m": f.bury_depth_m,
                        "elevation_note": f.elevation_note if key in UTILITY_REVIEW_LAYERS else "",
                    }
                    for f in features[:10]
                ],
            }
        )

    return {
        "status": "ok",
        "project_id": project_id,
        "crs": project.crs_string,
        "gis_file": project.gis_name,
        "ifc_revisions": {"rev_a": project.rev_a_name, "rev_b": project.rev_b_name},
        "layers": layer_entries,
        "missing_info": project.missing_info,
    }


def find_assets(
    project_id: str,
    query: str = "",
    revision: str = "both",
    tag: str | None = None,
    ifc_class: str | None = None,
    workspace: ReviewWorkspace = DEFAULT_WORKSPACE,
) -> dict:
    """Find IFC assets by Chinese/English keyword, tag, or IFC class, flagging duplicate names."""
    project = workspace.require_project(project_id)
    if not project.coordinates_valid:
        return {
            "status": "incomplete_coordinates",
            "project_id": project_id,
            "assets": [],
            "missing_info": project.missing_info,
        }

    rev_scope = (revision or "both").lower()
    rev_a_matches = (
        [_asset_dict(a, "rev_a") for a in project.rev_a_assets if _asset_matches_query(a, query, tag, ifc_class)]
        if rev_scope in ("rev_a", "a", "both")
        else []
    )
    rev_b_matches = (
        [_asset_dict(a, "rev_b") for a in project.rev_b_assets if _asset_matches_query(a, query, tag, ifc_class)]
        if rev_scope in ("rev_b", "b", "both")
        else []
    )

    distinct: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for item in rev_b_matches + rev_a_matches:
        dedup_key = (normalize(item["tag"]), normalize(item["ifc_class"])) if item["tag"] else (item["guid"], item["ifc_class"])
        if dedup_key not in seen_keys:
            seen_keys.add(dedup_key)
            distinct.append(item)

    ambiguous = bool(query.strip()) and not tag and len(distinct) > 1
    result = {
        "status": "needs_disambiguation" if ambiguous else "ok",
        "project_id": project_id,
        "query": query,
        "crs": project.crs_string,
        "count": len(distinct),
        "ambiguous": ambiguous,
        "assets": distinct,
        "by_revision": {"rev_a": rev_a_matches, "rev_b": rev_b_matches},
        "missing_info": [],
    }
    if ambiguous:
        labels = ", ".join(f"{a['tag'] or a['guid']} ({a['name']})" for a in distinct)
        result["candidates"] = [
            {
                "tag": a["tag"],
                "guid": a["guid"],
                "name": a["name"],
                "ifc_class": a["ifc_class"],
                "centroid_xy": a["centroid_xy"],
            }
            for a in distinct
        ]
        result["question"] = f"查到 {len(distinct)} 个匹配“{query}”的设备：{labels}。请选择具体位号以继续复核。"
    return result


def _normalize_confirmed_matches(confirmed_matches: list[dict] | None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not confirmed_matches:
        return mapping
    for item in confirmed_matches:
        if isinstance(item, dict) and item.get("rev_a_guid") and item.get("rev_b_guid"):
            mapping[str(item["rev_a_guid"])] = str(item["rev_b_guid"])
    return mapping


def _compare_internal(
    project: RevisionProject,
    asset_query: str | None = None,
    min_displacement_m: float = 0.05,
    confirmed_matches: list[dict] | None = None,
) -> dict:
    confirmed_map = _normalize_confirmed_matches(confirmed_matches)
    a_by_guid = {a.guid: a for a in project.rev_a_assets}
    b_by_guid = {b.guid: b for b in project.rev_b_assets}

    matched_pairs: list[tuple[Asset, Asset, str, str, bool]] = []
    used_a: set[str] = set()
    used_b: set[str] = set()

    for guid_a, guid_b in confirmed_map.items():
        if guid_a in a_by_guid and guid_b in b_by_guid:
            used_a.add(guid_a)
            used_b.add(guid_b)
            matched_pairs.append(
                (a_by_guid[guid_a], b_by_guid[guid_b], "user_confirmed", f"用户已确认将 Rev A ({guid_a}) 对应到 Rev B ({guid_b})。", False)
            )

    for guid, asset_a in a_by_guid.items():
        if guid in used_a:
            continue
        asset_b = b_by_guid.get(guid)
        if asset_b is not None and asset_b.guid not in used_b:
            used_a.add(guid)
            used_b.add(asset_b.guid)
            matched_pairs.append((asset_a, asset_b, "global_id", f"IFC GlobalId 完全一致 ({guid})。", False))

    unmatched_a = [a for a in project.rev_a_assets if a.guid not in used_a]
    unmatched_b = [b for b in project.rev_b_assets if b.guid not in used_b]

    ambiguous_matches: list[dict] = []
    if unmatched_a and unmatched_b:
        a_by_tag_type: dict[tuple[str, str], list[Asset]] = {}
        b_by_tag_type: dict[tuple[str, str], list[Asset]] = {}
        for a in unmatched_a:
            if a.tag:
                a_by_tag_type.setdefault((normalize(a.tag), normalize(a.ifc_class)), []).append(a)
        for b in unmatched_b:
            if b.tag:
                b_by_tag_type.setdefault((normalize(b.tag), normalize(b.ifc_class)), []).append(b)

        for key in sorted(set(a_by_tag_type) & set(b_by_tag_type)):
            list_a = a_by_tag_type[key]
            list_b = b_by_tag_type[key]
            if len(list_a) == 1 and len(list_b) == 1:
                asset_a, asset_b = list_a[0], list_b[0]
                used_a.add(asset_a.guid)
                used_b.add(asset_b.guid)
                area_diff_pct = abs(asset_b.footprint.area - asset_a.footprint.area) / max(asset_a.footprint.area, 1e-6) * 100.0
                basis = (
                    f"GlobalId 由 {asset_a.guid} 变更为 {asset_b.guid}；"
                    f"按唯一位号 Tag='{asset_a.tag}' + 类型 '{asset_a.ifc_class}' 提出候选匹配"
                    f"（名称一致={asset_a.name == asset_b.name}，底面面积差异={area_diff_pct:.1f}%）。"
                )
                matched_pairs.append((asset_a, asset_b, "tag_and_type_candidate", basis, True))
            else:
                ambiguous_matches.append(
                    {
                        "reason": "duplicate_tag_and_type",
                        "tag": list_a[0].tag,
                        "ifc_class": list_a[0].ifc_class,
                        "basis": f"GlobalId 变化且存在多个相同位号 '{list_a[0].tag}' 与类型 '{list_a[0].ifc_class}' 的构件，无法唯一确认。",
                        "rev_a_candidates": [_asset_dict(a, "rev_a") for a in list_a],
                        "rev_b_candidates": [_asset_dict(b, "rev_b") for b in list_b],
                    }
                )

    rem_a = [a for a in project.rev_a_assets if a.guid not in used_a]
    rem_b = [b for b in project.rev_b_assets if b.guid not in used_b]
    if rem_a and rem_b:
        a_by_name_type: dict[tuple[str, str], list[Asset]] = {}
        b_by_name_type: dict[tuple[str, str], list[Asset]] = {}
        for a in rem_a:
            a_by_name_type.setdefault((normalize(a.name), normalize(a.ifc_class)), []).append(a)
        for b in rem_b:
            b_by_name_type.setdefault((normalize(b.name), normalize(b.ifc_class)), []).append(b)
        for key in sorted(set(a_by_name_type) & set(b_by_name_type)):
            list_a = a_by_name_type[key]
            list_b = b_by_name_type[key]
            ambiguous_matches.append(
                {
                    "reason": "name_and_type_without_unique_tag",
                    "name": list_a[0].name,
                    "ifc_class": list_a[0].ifc_class,
                    "basis": (
                        f"GlobalId 变化且缺少唯一位号匹配，按名称 '{list_a[0].name}' + 类型 '{list_a[0].ifc_class}' "
                        f"找到 {len(list_a)} 个旧版构件与 {len(list_b)} 个新版构件，需人工或 Agent 确认。"
                    ),
                    "rev_a_candidates": [_asset_dict(a, "rev_a") for a in list_a],
                    "rev_b_candidates": [_asset_dict(b, "rev_b") for b in list_b],
                }
            )

    comparisons = []
    moved_assets = []
    id_changed_candidates = []
    unchanged_assets = []

    for asset_a, asset_b, method, basis, needs_confirm in matched_pairs:
        if asset_query and not (_asset_matches_query(asset_a, asset_query) or _asset_matches_query(asset_b, asset_query)):
            continue
        metrics = _compute_displacement(asset_a, asset_b)
        moved = metrics["displacement_m"] >= min_displacement_m or metrics["rotation_diff_deg"] >= 0.5
        id_changed = asset_a.guid != asset_b.guid
        entry = {
            "tag": asset_b.tag or asset_a.tag,
            "name": asset_b.name or asset_a.name,
            "ifc_class": asset_b.ifc_class,
            "rev_a_guid": asset_a.guid,
            "rev_b_guid": asset_b.guid,
            "match_method": method,
            "match_basis": basis,
            "id_changed": id_changed,
            "needs_confirmation": needs_confirm,
            "moved": moved,
            **metrics,
            "_footprint_a": asset_a.footprint,
            "_footprint_b": asset_b.footprint,
        }
        comparisons.append(entry)
        if moved:
            moved_assets.append(entry)
        else:
            unchanged_assets.append(entry)
        if id_changed:
            id_changed_candidates.append(entry)

    added_assets = [_asset_dict(b, "rev_b") for b in rem_b if not asset_query or _asset_matches_query(b, asset_query)]
    removed_assets = [_asset_dict(a, "rev_a") for a in rem_a if not asset_query or _asset_matches_query(a, asset_query)]

    return {
        "comparisons": comparisons,
        "moved_assets": moved_assets,
        "unchanged_assets": unchanged_assets,
        "id_changed_candidates": id_changed_candidates,
        "ambiguous_matches": ambiguous_matches,
        "added_assets": added_assets,
        "removed_assets": removed_assets,
    }


def _strip_internal_geoms(items: list[dict]) -> list[dict]:
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]


def compare_revisions(
    project_id: str,
    asset_query: str | None = None,
    min_displacement_m: float = 0.05,
    confirmed_matches: list[dict] | None = None,
    workspace: ReviewWorkspace = DEFAULT_WORKSPACE,
) -> dict:
    """Compare Rev A and Rev B IFC models using GlobalId first, then unique Tag+Type fallback."""
    project = workspace.require_project(project_id)
    if not project.coordinates_valid:
        return {
            "status": "incomplete_coordinates",
            "project_id": project_id,
            "moved_assets": [],
            "id_changed_candidates": [],
            "ambiguous_matches": [],
            "missing_info": project.missing_info,
        }

    raw = _compare_internal(project, asset_query, min_displacement_m, confirmed_matches)
    moved_clean = _strip_internal_geoms(raw["moved_assets"])
    id_changed_clean = _strip_internal_geoms(raw["id_changed_candidates"])
    unchanged_clean = _strip_internal_geoms(raw["unchanged_assets"])

    needs_confirmation = bool(raw["ambiguous_matches"]) or any(item["needs_confirmation"] for item in id_changed_clean)
    status = "needs_confirmation" if raw["ambiguous_matches"] else "ok"

    return {
        "status": status,
        "project_id": project_id,
        "crs": project.crs_string,
        "rev_a_file": project.rev_a_name,
        "rev_b_file": project.rev_b_name,
        "min_displacement_m": min_displacement_m,
        "moved_count": len(moved_clean),
        "unchanged_count": len(unchanged_clean),
        "id_changed_count": len(id_changed_clean),
        "ambiguous_count": len(raw["ambiguous_matches"]),
        "needs_confirmation": needs_confirmation,
        "moved_assets": moved_clean,
        "id_changed_candidates": id_changed_clean,
        "ambiguous_matches": raw["ambiguous_matches"],
        "added_assets": raw["added_assets"],
        "removed_assets": raw["removed_assets"],
        "missing_info": [],
        "traceability": {
            "project_id": project_id,
            "rev_a_file": project.rev_a_name,
            "rev_b_file": project.rev_b_name,
            "crs": project.crs_string,
        },
    }


def _resolve_focus_pair(
    project: RevisionProject,
    asset_guid: str | None = None,
    asset_tag: str | None = None,
    asset_query: str | None = None,
    confirmed_matches: list[dict] | None = None,
):
    raw = _compare_internal(project, confirmed_matches=confirmed_matches)
    comparisons = raw["comparisons"]

    if asset_guid:
        matched = [c for c in comparisons if c["rev_a_guid"] == asset_guid or c["rev_b_guid"] == asset_guid]
        if matched:
            return "ok", matched[0], []
        for rev, assets in (("rev_b", project.rev_b_assets), ("rev_a", project.rev_a_assets)):
            for asset in assets:
                if asset.guid == asset_guid:
                    return (
                        "ok",
                        {
                            "tag": asset.tag,
                            "name": asset.name,
                            "ifc_class": asset.ifc_class,
                            "rev_a_guid": asset.guid if rev == "rev_a" else None,
                            "rev_b_guid": asset.guid if rev == "rev_b" else None,
                            "displacement_m": 0.0,
                            "_footprint_a": asset.footprint if rev == "rev_a" else None,
                            "_footprint_b": asset.footprint if rev == "rev_b" else None,
                        },
                        [],
                    )
        return "not_found", None, []

    if asset_tag:
        norm_tag = normalize(asset_tag)
        matched = [c for c in comparisons if normalize(c["tag"]) == norm_tag]
        if len(matched) == 1:
            return "ok", matched[0], []
        if len(matched) > 1:
            return "ambiguous", None, matched
        return "not_found", None, []

    if asset_query:
        matched = []
        for c in comparisons:
            fake_asset = Asset(c["rev_b_guid"] or c["rev_a_guid"], c["tag"], c["name"], c["_footprint_b"] or c["_footprint_a"], c["ifc_class"])
            if _asset_matches_query(fake_asset, asset_query):
                matched.append(c)
        if len(matched) == 1:
            return "ok", matched[0], []
        if len(matched) > 1:
            return "ambiguous", None, matched
        return "not_found", None, []

    if raw["moved_assets"]:
        return "ok", raw["moved_assets"][0], []
    if comparisons:
        return "ok", comparisons[0], []
    return "not_found", None, []


def check_utility_proximity(
    project_id: str,
    asset_guid: str | None = None,
    asset_tag: str | None = None,
    asset_query: str | None = None,
    layer: str = "water",
    buffer_m: float = 30.0,
    revision: str = "both",
    confirmed_matches: list[dict] | None = None,
    workspace: ReviewWorkspace = DEFAULT_WORKSPACE,
) -> dict:
    """Calculate planar distances and clearance compliance between an asset and nearby utilities/slopes."""
    project = workspace.require_project(project_id)
    if not project.coordinates_valid:
        return {
            "status": "incomplete_coordinates",
            "project_id": project_id,
            "items": [],
            "missing_info": project.missing_info,
        }

    state, target, candidates = _resolve_focus_pair(
        project,
        asset_guid=asset_guid,
        asset_tag=asset_tag,
        asset_query=asset_query,
        confirmed_matches=confirmed_matches,
    )
    if state == "ambiguous":
        cand_list = [
            {
                "tag": c["tag"],
                "guid": c["rev_b_guid"] or c["rev_a_guid"],
                "rev_a_guid": c["rev_a_guid"],
                "rev_b_guid": c["rev_b_guid"],
                "name": c["name"],
                "ifc_class": c["ifc_class"],
                "displacement_m": c["displacement_m"],
                "centroid_rev_b": c["centroid_rev_b"],
            }
            for c in candidates
        ]
        labels = "、".join(f"{c['tag']}（{c['name']}，位移 {c['displacement_m']:.2f}m）" for c in cand_list)
        return {
            "status": "needs_disambiguation",
            "project_id": project_id,
            "query": asset_query or asset_tag or "",
            "candidates": cand_list,
            "question": f"检测到 {len(cand_list)} 个同名或匹配设备：{labels}。请指定其中一个位号继续检查周边管线与边坡。",
            "items": [],
            "missing_info": [],
        }
    if state == "not_found" or target is None:
        return {
            "status": "not_found",
            "project_id": project_id,
            "error": f"未在项目中找到目标设备（query={asset_query}, tag={asset_tag}, guid={asset_guid}）。",
            "items": [],
            "missing_info": [],
        }

    layer_key = _resolve_layer_key(layer)
    if layer_key == "all":
        target_features = [f for k in UTILITY_REVIEW_LAYERS for f in project.gis_features.get(k, [])]
    else:
        target_features = list(project.gis_features.get(layer_key, []))

    geom_a = target.get("_footprint_a")
    geom_b = target.get("_footprint_b")
    active_geom = geom_b if (revision == "rev_b" or geom_a is None) else (geom_a if revision == "rev_a" else geom_b)

    items = []
    missing_info = []
    for feat in target_features:
        dist_a = float(geom_a.distance(feat.geometry)) if geom_a is not None else float(active_geom.distance(feat.geometry))
        dist_b = float(geom_b.distance(feat.geometry)) if geom_b is not None else float(active_geom.distance(feat.geometry))
        min_dist = min(dist_a, dist_b) if revision == "both" else (dist_a if revision == "rev_a" else dist_b)
        if min_dist > buffer_m:
            continue

        intersects_b = bool(geom_b is not None and geom_b.intersects(feat.geometry))
        intersects_a = bool(geom_a is not None and geom_a.intersects(feat.geometry))
        delta = dist_b - dist_a

        eval_res = evaluate_clearance(
            layer=feat.layer,
            distance_rev_a_m=dist_a,
            distance_rev_b_m=dist_b,
            intersects_rev_b=intersects_b,
            elevation_missing=feat.elevation_missing,
            buffer_m=buffer_m,
        )

        elevation_note = feat.elevation_note
        if feat.elevation_missing:
            missing_info.append(
                {
                    "field": "bury_depth_m",
                    "blocking": False,
                    "source": project.gis_name,
                    "layer": feat.layer,
                    "utility_id": feat.feature_id,
                    "utility_name": feat.name,
                    "flag": "待核实高程",
                    "message": f"管线 {feat.feature_id} ({feat.name}) 缺少埋深，需现场或竣工图复核高程（待核实高程）。",
                }
            )

        items.append(
            {
                "utility_id": feat.feature_id,
                "utility_name": feat.name,
                "layer": feat.layer,
                "diameter_mm": feat.diameter_mm,
                "material": feat.material,
                "bury_depth_m": feat.bury_depth_m,
                "elevation_m": feat.elevation_m,
                "elevation_note": elevation_note,
                "elevation_missing": feat.elevation_missing,
                "distance_rev_a_m": round(dist_a, 3),
                "distance_rev_b_m": round(dist_b, 3),
                "delta_distance_m": round(delta, 3),
                "intersects_rev_a": intersects_a,
                "intersects_rev_b": intersects_b,
                "gis_file": project.gis_name,
                **eval_res,
            }
        )

    items.sort(
        key=lambda x: (
            {"violation": 0, "warning": 1, "compliant_in_buffer": 2}.get(x["compliance_status"], 3),
            {"critical": 0, "high": 1, "medium": 2}.get(x["severity"], 3),
            x["distance_rev_b_m"],
        )
    )
    return {
        "status": "ok",
        "project_id": project_id,
        "crs": project.crs_string,
        "layer": layer_key,
        "buffer_m": buffer_m,
        "asset": {
            "tag": target["tag"],
            "name": target["name"],
            "ifc_class": target["ifc_class"],
            "rev_a_guid": target["rev_a_guid"],
            "rev_b_guid": target["rev_b_guid"],
            "displacement_m": target.get("displacement_m", 0.0),
        },
        "review_count": len(items),
        "violation_count": sum(1 for i in items if i["compliance_status"] == "violation"),
        "missing_elevation_count": sum(1 for i in items if i["elevation_missing"]),
        "items": items,
        "missing_info": missing_info,
        "traceability": {
            "project_id": project_id,
            "rev_a_file": project.rev_a_name,
            "rev_b_file": project.rev_b_name,
            "gis_file": project.gis_name,
            "gis_layer": layer_key,
            "crs": project.crs_string,
        },
    }


def export_review_package(
    project_id: str,
    focus_asset_guid: str | None = None,
    focus_asset_tag: str | None = None,
    focus_asset_query: str | None = None,
    utility_layer: str = "water",
    buffer_m: float = 30.0,
    radius_m: float = 80.0,
    output_dir: str | None = None,
    include_dxf: bool = True,
    confirmed_matches: list[dict] | None = None,
    workspace: ReviewWorkspace = DEFAULT_WORKSPACE,
) -> dict:
    """Export before/after comparison PDF, UTF-8-SIG CSV issue register, and optional DXF."""
    project = workspace.require_project(project_id)
    if not project.coordinates_valid:
        return {
            "status": "incomplete_coordinates",
            "project_id": project_id,
            "files": [],
            "missing_info": project.missing_info,
            "error": "坐标系不完整，无法生成对比图纸与问题清单。",
        }

    state, target, candidates = _resolve_focus_pair(
        project,
        asset_guid=focus_asset_guid,
        asset_tag=focus_asset_tag,
        asset_query=focus_asset_query,
        confirmed_matches=confirmed_matches,
    )
    if state == "ambiguous":
        cand_list = [
            {"tag": c["tag"], "guid": c["rev_b_guid"] or c["rev_a_guid"], "name": c["name"], "ifc_class": c["ifc_class"]}
            for c in candidates
        ]
        return {
            "status": "needs_disambiguation",
            "project_id": project_id,
            "candidates": cand_list,
            "question": "存在多个同名设备，请先指定唯一设备位号再生成评审包。",
            "files": [],
            "missing_info": [],
        }
    if state == "not_found" or target is None:
        return {
            "status": "not_found",
            "project_id": project_id,
            "error": "未找到用于出图的目标设备。",
            "files": [],
            "missing_info": [],
        }

    raw_cmp = _compare_internal(project, confirmed_matches=confirmed_matches)
    proximity = check_utility_proximity(
        project_id=project_id,
        asset_guid=target["rev_b_guid"] or target["rev_a_guid"],
        layer=utility_layer,
        buffer_m=buffer_m,
        confirmed_matches=confirmed_matches,
        workspace=workspace,
    )

    geom_b = target.get("_footprint_b")
    geom_a = target.get("_footprint_a")
    center = (geom_b or geom_a).centroid
    radius_m = max(20.0, min(500.0, float(radius_m)))
    clip = center.buffer(radius_m, quad_segs=32)

    gis_clipped: dict[str, list] = {}
    for layer_key, geoms in project.layers.items():
        clipped_list = []
        for g in geoms:
            if g.intersects(clip):
                cg = g.intersection(clip)
                if not cg.is_empty:
                    clipped_list.append(cg)
        gis_clipped[layer_key] = clipped_list

    unchanged_geoms = []
    for item in raw_cmp["unchanged_assets"]:
        g = item.get("_footprint_b") or item.get("_footprint_a")
        if g is not None and g.intersects(clip):
            cg = g.intersection(clip)
            if not cg.is_empty:
                unchanged_geoms.append(cg)

    feature_lookup = {f.feature_id: f for feats in project.gis_features.values() for f in feats}
    prox_for_draw = []
    for item in proximity.get("items", []):
        feat = feature_lookup.get(item["utility_id"])
        cg = feat.geometry.intersection(clip) if (feat and feat.geometry.intersects(clip)) else None
        prox_for_draw.append({**item, "_clipped_geometry": cg})

    focus_tag = target["tag"] or target["name"]
    focus_label = f"{target['tag']} · {target['name']}" if target["tag"] and target["tag"] != target["name"] else focus_tag

    csv_rows: list[dict] = []
    issue_seq = 1

    for moved in raw_cmp["moved_assets"]:
        method_label = "GlobalId匹配" if not moved["id_changed"] else f"ID变化候选匹配({moved['match_method']})"
        csv_rows.append(
            {
                "issue_id": f"ISS-{issue_seq:03d}",
                "project_id": project_id,
                "category": "asset_displacement",
                "severity": "high" if moved["displacement_m"] >= 3.0 else "medium",
                "compliance_status": "review_required",
                "rule_code": "REV-DISP-01",
                "status": "待确认匹配" if moved["needs_confirmation"] else "待复核",
                "asset_tag": moved["tag"],
                "asset_name": moved["name"],
                "ifc_class": moved["ifc_class"],
                "rev_a_file": project.rev_a_name,
                "rev_a_guid": moved["rev_a_guid"],
                "rev_b_file": project.rev_b_name,
                "rev_b_guid": moved["rev_b_guid"],
                "match_method": method_label,
                "displacement_m": moved["displacement_m"],
                "gis_file": project.gis_name,
                "gis_layer": "buildings",
                "utility_id": "",
                "utility_name": "",
                "distance_rev_a_m": "",
                "distance_rev_b_m": "",
                "min_horizontal_clearance_m": "",
                "bury_depth_m": "",
                "elevation_note": "",
                "crs": project.crs_string,
                "summary": f"{moved['tag']} ({moved['name']}) 平面位移 {moved['displacement_m']:.2f} m；{moved['match_basis']}",
                "recommended_action": "核对设备基础及接口偏移，必要时确认换版 GlobalId 对应关系。",
            }
        )
        issue_seq += 1

    for amb in raw_cmp["ambiguous_matches"]:
        csv_rows.append(
            {
                "issue_id": f"ISS-{issue_seq:03d}",
                "project_id": project_id,
                "category": "ambiguous_id_match",
                "severity": "medium",
                "compliance_status": "needs_confirmation",
                "rule_code": "REV-ID-01",
                "status": "待人工确认",
                "asset_tag": amb.get("tag", ""),
                "asset_name": amb.get("name", ""),
                "ifc_class": amb.get("ifc_class", ""),
                "rev_a_file": project.rev_a_name,
                "rev_a_guid": ";".join(c["guid"] for c in amb.get("rev_a_candidates", [])),
                "rev_b_file": project.rev_b_name,
                "rev_b_guid": ";".join(c["guid"] for c in amb.get("rev_b_candidates", [])),
                "match_method": amb.get("reason", "ambiguous"),
                "displacement_m": "",
                "gis_file": project.gis_name,
                "gis_layer": "buildings",
                "utility_id": "",
                "utility_name": "",
                "distance_rev_a_m": "",
                "distance_rev_b_m": "",
                "min_horizontal_clearance_m": "",
                "bury_depth_m": "",
                "elevation_note": "",
                "crs": project.crs_string,
                "summary": amb.get("basis", ""),
                "recommended_action": "人工核对旧版与新版构件映射关系。",
            }
        )
        issue_seq += 1

    for pitem in proximity.get("items", []):
        action = "核查管线/边坡净距与迁改或支护方案"
        if pitem["elevation_missing"]:
            action += "；补充管线埋深/管底高程实测资料（待核实高程）"
        csv_rows.append(
            {
                "issue_id": f"ISS-{issue_seq:03d}",
                "project_id": project_id,
                "category": "utility_proximity",
                "severity": pitem["severity"],
                "compliance_status": pitem["compliance_status"],
                "rule_code": pitem["rule_code"],
                "status": "待核实高程" if pitem["elevation_missing"] else ("净距违规" if pitem["compliance_status"] == "violation" else "待复核"),
                "asset_tag": target["tag"],
                "asset_name": target["name"],
                "ifc_class": target["ifc_class"],
                "rev_a_file": project.rev_a_name,
                "rev_a_guid": target["rev_a_guid"] or "",
                "rev_b_file": project.rev_b_name,
                "rev_b_guid": target["rev_b_guid"] or "",
                "match_method": target.get("match_method", "global_id"),
                "displacement_m": target.get("displacement_m", 0.0),
                "gis_file": project.gis_name,
                "gis_layer": pitem["layer"],
                "utility_id": pitem["utility_id"],
                "utility_name": pitem["utility_name"],
                "distance_rev_a_m": pitem["distance_rev_a_m"],
                "distance_rev_b_m": pitem["distance_rev_b_m"],
                "min_horizontal_clearance_m": pitem["min_horizontal_clearance_m"],
                "bury_depth_m": "" if pitem["bury_depth_m"] is None else pitem["bury_depth_m"],
                "elevation_note": pitem["elevation_note"],
                "crs": project.crs_string,
                "summary": pitem["review_reason"],
                "recommended_action": action,
            }
        )
        issue_seq += 1

    out_root = Path(output_dir) if output_dir else (workspace.data_dir / "exports" / project_id)
    out_root.mkdir(parents=True, exist_ok=True)
    safe_tag = re.sub(r"[^a-zA-Z0-9_-]+", "-", focus_tag).strip("-") or "site"

    pdf_content = comparison_pdf_bytes(
        project_id=project_id,
        rev_a_name=project.rev_a_name,
        rev_b_name=project.rev_b_name,
        gis_name=project.gis_name,
        crs_string=project.crs_string,
        focus_label=focus_label,
        focus_guid_a=target["rev_a_guid"],
        focus_guid_b=target["rev_b_guid"],
        center=center,
        radius_m=radius_m,
        gis_clipped=gis_clipped,
        unchanged_footprints=unchanged_geoms,
        moved_items=raw_cmp["moved_assets"],
        proximity_items=prox_for_draw,
        utility_layer=proximity["layer"],
        buffer_m=buffer_m,
    )
    pdf_path = out_root / f"revision-comparison-{safe_tag}.pdf"
    pdf_path.write_bytes(pdf_content)

    csv_content = issues_csv_bytes(csv_rows)
    csv_path = out_root / f"review-issues-{safe_tag}.csv"
    csv_path.write_bytes(csv_content)

    generated: list[dict] = []
    for kind, label, path, mime in (
        ("pdf", "前后对照图 (A3 PDF)", pdf_path, "application/pdf"),
        ("csv", "评审问题清单 (CSV)", csv_path, "text/csv; charset=utf-8"),
    ):
        if path.is_file() and path.stat().st_size > 0:
            generated.append(
                {
                    "kind": kind,
                    "label": label,
                    "filename": path.name,
                    "path": str(path.resolve()),
                    "size_bytes": path.stat().st_size,
                    "mimetype": mime,
                    "verified": True,
                }
            )

    if include_dxf:
        dxf_content = comparison_dxf_bytes(
            center=center,
            radius_m=radius_m,
            gis_clipped=gis_clipped,
            unchanged_footprints=unchanged_geoms,
            moved_items=raw_cmp["moved_assets"],
            proximity_items=prox_for_draw,
        )
        dxf_path = out_root / f"revision-comparison-{safe_tag}.dxf"
        dxf_path.write_bytes(dxf_content)
        if dxf_path.is_file() and dxf_path.stat().st_size > 0:
            generated.append(
                {
                    "kind": "dxf",
                    "label": "前后对照图 (可编辑 DXF)",
                    "filename": dxf_path.name,
                    "path": str(dxf_path.resolve()),
                    "size_bytes": dxf_path.stat().st_size,
                    "mimetype": "application/dxf",
                    "verified": True,
                }
            )

    return {
        "status": "ok",
        "project_id": project_id,
        "crs": project.crs_string,
        "focus_asset": {
            "tag": target["tag"],
            "name": target["name"],
            "rev_a_guid": target["rev_a_guid"],
            "rev_b_guid": target["rev_b_guid"],
        },
        "issue_count": len(csv_rows),
        "moved_count": len(raw_cmp["moved_assets"]),
        "utility_review_count": len(proximity.get("items", [])),
        "violation_count": proximity.get("violation_count", 0),
        "missing_elevation_count": proximity.get("missing_elevation_count", 0),
        "files": generated,
        "issues_preview": csv_rows[:25],
        "missing_info": proximity.get("missing_info", []),
        "traceability": {
            "project_id": project_id,
            "rev_a_file": project.rev_a_name,
            "rev_b_file": project.rev_b_name,
            "gis_file": project.gis_name,
            "gis_layer": proximity["layer"],
            "crs": project.crs_string,
            "georeference_source": project.georeference_source,
        },
    }


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "open_project",
            "description": "打开两版 IFC 模型和一份 GIS GeoJSON 或 CAD DXF 管网文件，校验坐标系并返回 project_id 与缺失信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "rev_a_path": {"type": "string", "description": "旧版 IFC 文件路径"},
                    "rev_b_path": {"type": "string", "description": "新版 IFC 文件路径"},
                    "gis_path": {"type": "string", "description": "GIS GeoJSON 或投影米制 DXF 管网文件路径"},
                    "preset": {
                        "type": "string",
                        "enum": list(DEMO_SCENARIOS.keys()),
                        "description": "内置演示场景：mineworks / mineworks_dxf / mineworks_dup / normal / id_changed / duplicate_pumps / missing_crs",
                    },
                    "crs_override": {
                        "type": "object",
                        "description": "可选：当 IFC 缺少 IfcMapConversion 时的显式配准参数",
                        "properties": {
                            "crs": {"type": "string", "description": "如 EPSG:4547 (CGCS2000) 或 EPSG:32650"},
                            "eastings": {"type": "number"},
                            "northings": {"type": "number"},
                            "orthogonal_height": {"type": "number"},
                            "rotation_deg": {"type": "number"},
                        },
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "georeference_project",
            "description": "当 open_project 提示缺少 IfcMapConversion 时，使用用户提供的投影 EPSG 坐标系与原点/旋转角完成显式配准并重新加载项目。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "crs": {"type": "string", "description": "平面投影 EPSG 编码，例如 EPSG:4547 或 EPSG:32650"},
                    "eastings": {"type": "number", "description": "模型原点东坐标（米）"},
                    "northings": {"type": "number", "description": "模型原点北坐标（米）"},
                    "orthogonal_height": {"type": "number", "default": 0.0},
                    "rotation_deg": {"type": "number", "default": 0.0},
                    "apply_to_both": {"type": "boolean", "default": False},
                },
                "required": ["project_id", "crs", "eastings", "northings"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_layers",
            "description": "列出项目的 GIS/DXF 图层（含矿浆、给水、排水、电缆沟、边坡）、要素数量、坐标系及缺少埋深/高程的管线清单。",
            "parameters": {
                "type": "object",
                "properties": {"project_id": {"type": "string"}},
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_assets",
            "description": "按中英文关键字、位号(Tag)或 IFC 类型查找设备；若存在多个同名设备会返回候选列表并请求确认。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "query": {"type": "string", "description": "查询关键字，例如 渣浆泵房、泵房、浓密机、SP-101"},
                    "revision": {"type": "string", "enum": ["rev_a", "rev_b", "both"], "default": "both"},
                    "tag": {"type": "string"},
                    "ifc_class": {"type": "string"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_revisions",
            "description": "对比两版 IFC 设计，优先使用 GlobalId，ID 变化时按唯一位号+类型提出候选匹配，计算位移量。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "asset_query": {"type": "string"},
                    "min_displacement_m": {"type": "number", "default": 0.05},
                    "confirmed_matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "rev_a_guid": {"type": "string"},
                                "rev_b_guid": {"type": "string"},
                            },
                            "required": ["rev_a_guid", "rev_b_guid"],
                        },
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_utility_proximity",
            "description": "计算指定设备与管线/边坡（water/slurry/power/drainage/slope/all）的平面距离并按矿山净距规范判定合规性；缺埋深标记“待核实高程”。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "asset_guid": {"type": "string"},
                    "asset_tag": {"type": "string"},
                    "asset_query": {"type": "string"},
                    "layer": {"type": "string", "description": "water / slurry / power / drainage / slope / all", "default": "water"},
                    "buffer_m": {"type": "number", "default": 30.0},
                    "revision": {"type": "string", "enum": ["rev_a", "rev_b", "both"], "default": "both"},
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_review_package",
            "description": "生成前后对照图 A3 PDF、含规范条文与待核实高程标记的可追溯 CSV 清单及可编辑 DXF。",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "focus_asset_guid": {"type": "string"},
                    "focus_asset_tag": {"type": "string"},
                    "focus_asset_query": {"type": "string"},
                    "utility_layer": {"type": "string", "default": "water"},
                    "buffer_m": {"type": "number", "default": 30.0},
                    "radius_m": {"type": "number", "default": 80.0},
                    "include_dxf": {"type": "boolean", "default": True},
                },
                "required": ["project_id"],
            },
        },
    },
]


def execute_tool(
    name: str,
    arguments: dict | None,
    *,
    workspace: ReviewWorkspace = DEFAULT_WORKSPACE,
    default_output_dir: str | None = None,
    confirmed_matches: list[dict] | None = None,
) -> dict:
    """Unified dispatcher used by both the MCP server and the Web Agent."""
    args = dict(arguments or {})
    if name == "open_project":
        return open_project(
            rev_a_path=args.get("rev_a_path"),
            rev_b_path=args.get("rev_b_path"),
            gis_path=args.get("gis_path"),
            preset=args.get("preset"),
            project_id=args.get("project_id"),
            crs_override=args.get("crs_override"),
            workspace=workspace,
        )
    if name == "georeference_project":
        return georeference_project(
            project_id=args["project_id"],
            crs=args["crs"],
            eastings=float(args["eastings"]),
            northings=float(args["northings"]),
            orthogonal_height=float(args.get("orthogonal_height", 0.0)),
            rotation_deg=float(args.get("rotation_deg", 0.0)),
            apply_to_both=bool(args.get("apply_to_both", False)),
            workspace=workspace,
        )
    if name == "list_layers":
        return list_layers(project_id=args["project_id"], workspace=workspace)
    if name == "find_assets":
        return find_assets(
            project_id=args["project_id"],
            query=args.get("query", ""),
            revision=args.get("revision", "both"),
            tag=args.get("tag"),
            ifc_class=args.get("ifc_class"),
            workspace=workspace,
        )
    if name == "compare_revisions":
        return compare_revisions(
            project_id=args["project_id"],
            asset_query=args.get("asset_query"),
            min_displacement_m=float(args.get("min_displacement_m", 0.05)),
            confirmed_matches=args.get("confirmed_matches") or confirmed_matches,
            workspace=workspace,
        )
    if name == "check_utility_proximity":
        return check_utility_proximity(
            project_id=args["project_id"],
            asset_guid=args.get("asset_guid"),
            asset_tag=args.get("asset_tag"),
            asset_query=args.get("asset_query"),
            layer=args.get("layer", "water"),
            buffer_m=float(args.get("buffer_m", 30.0)),
            revision=args.get("revision", "both"),
            confirmed_matches=confirmed_matches,
            workspace=workspace,
        )
    if name == "export_review_package":
        return export_review_package(
            project_id=args["project_id"],
            focus_asset_guid=args.get("focus_asset_guid"),
            focus_asset_tag=args.get("focus_asset_tag"),
            focus_asset_query=args.get("focus_asset_query"),
            utility_layer=args.get("utility_layer", "water"),
            buffer_m=float(args.get("buffer_m", 30.0)),
            radius_m=float(args.get("radius_m", 80.0)),
            output_dir=args.get("output_dir") or default_output_dir,
            include_dxf=bool(args.get("include_dxf", True)),
            confirmed_matches=confirmed_matches,
            workspace=workspace,
        )
    raise InputError(f"Unknown tool: {name}")
