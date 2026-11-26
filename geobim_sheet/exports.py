import csv
import math
from io import BytesIO, StringIO

import ezdxf
from ezdxf import units
from reportlab.lib import colors
from reportlab.lib.pagesizes import A3, landscape
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from .core import LAYERS


pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


def parts(geometry):
    if hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from parts(part)
    else:
        yield geometry


def fit_text(sheet, x, y, value, size=9, limit=95 * mm, font="Helvetica"):
    value = " ".join(str(value).split())
    if any(ord(character) > 255 for character in value):
        font = "STSong-Light"
    if pdfmetrics.stringWidth(value, font, size) > limit:
        while value and pdfmetrics.stringWidth(value + "...", font, size) > limit:
            value = value[:-1]
        value += "..."
    sheet.setFont(font, size)
    sheet.drawString(x, y, value)


def draw_shape(sheet, geometry, project_point, stroke, fill=None, width=0.8, dash=None):
    sheet.saveState()
    sheet.setStrokeColor(colors.HexColor(stroke))
    sheet.setFillColor(colors.HexColor(fill or stroke))
    sheet.setLineWidth(width)
    if dash:
        sheet.setDash(*dash)
    for part in parts(geometry):
        if part.is_empty:
            continue
        if part.geom_type == "Point":
            sheet.circle(*project_point(part.x, part.y), 2, stroke=0, fill=1)
            continue
        path = sheet.beginPath()
        rings = [part.exterior, *part.interiors] if part.geom_type == "Polygon" else [part]
        for ring in rings:
            coords = list(ring.coords)
            if not coords:
                continue
            transformed = [project_point(x, y) for x, y, *_ in coords]
            path.moveTo(*transformed[0])
            for point in transformed[1:]:
                path.lineTo(*point)
            if part.geom_type == "Polygon":
                path.close()
        sheet.drawPath(path, stroke=1, fill=int(fill is not None and part.geom_type == "Polygon"), fillMode=0)
    sheet.restoreState()


PDF_STYLES = {
    "terrain": ("#c1cabb", None),
    "boundary": ("#88914f", "#f1f3e8"),
    "slope": ("#9c5a3c", None),
    "roads": ("#9d7248", "#e8d4b9"),
    "buildings": ("#657a6d", "#c1d2c8"),
    "drainage": ("#69699f", None),
    "water": ("#237f89", None),
    "slurry": ("#c2541a", None),
    "power": ("#b58900", None),
    "other": ("#66736b", None),
}


def pdf_bytes(project, selection, asset, drawing):
    center, features, subject = drawing
    stream = BytesIO()
    width, height = landscape(A3)
    sheet = canvas.Canvas(stream, pagesize=(width, height))
    sheet.setTitle(f"GeoBIM Sheet - {asset.tag or asset.name}")
    ink = colors.HexColor("#1d3026")
    sheet.setFillColor(ink)
    fit_text(sheet, 14 * mm, height - 19 * mm, "Site plan", 26, 275 * mm, "Helvetica-Bold")
    fit_text(sheet, 14 * mm, height - 28 * mm, f"{asset.tag or asset.name}  /  {selection.radius:g} m radius", 11, 275 * mm)
    fit_text(sheet, 308 * mm, height - 19 * mm, "GEOBIM SHEET", 11, font="Helvetica-Bold")
    fit_text(sheet, 308 * mm, height - 28 * mm, "A3 / Landscape / 01", 9)

    left, bottom, side = 29 * mm, 22 * mm, 237 * mm
    scale = side / (2 * selection.radius)

    def to_page(x, y):
        return left + side / 2 + (x - center.x) * scale, bottom + side / 2 + (y - center.y) * scale

    sheet.setFillColor(colors.HexColor("#f5f7f2"))
    sheet.rect(14 * mm, bottom, 269 * mm, side, stroke=0, fill=1)
    for key in ("boundary", "terrain", "slope", "roads", "buildings", "drainage", "water", "slurry", "power", "other"):
        stroke, fill = PDF_STYLES[key]
        for geometry in features.get(key, ()):
            draw_shape(sheet, geometry, to_page, stroke, fill, 1 if key != "terrain" else 0.35)
    draw_shape(sheet, subject, to_page, "#9f4b2d", "#e9b48b", 1.4)

    sheet.setFillColor(ink)
    sheet.setStrokeColor(ink)
    sheet.setLineWidth(0.65)
    sheet.line(24 * mm, bottom + 14 * mm, 24 * mm, bottom + 25 * mm)
    sheet.line(24 * mm, bottom + 25 * mm, 22 * mm, bottom + 21 * mm)
    sheet.line(24 * mm, bottom + 25 * mm, 26 * mm, bottom + 21 * mm)
    fit_text(sheet, 20 * mm, bottom + 28 * mm, "Grid N", 8)
    bar = next(value for value in (500, 200, 100, 50, 20, 10, 5, 2) if value <= selection.radius / 2)
    x, y = 34 * mm, bottom + 10 * mm
    sheet.line(x, y, x + bar * scale, y)
    for tick in (x, x + bar * scale):
        sheet.line(tick, y - 1.2 * mm, tick, y + 1.2 * mm)
    fit_text(sheet, x, y + 2.5 * mm, f"{bar:g} m", 8)
    label_x, label_y = to_page(center.x, center.y)
    fit_text(sheet, label_x + 3 * mm, label_y + 3 * mm, asset.tag or asset.name, 9, 55 * mm, "Helvetica-Bold")

    right = 308 * mm
    y = 245 * mm
    fit_text(sheet, right, y, "Legend", 13, font="Helvetica-Bold")
    for key in ("subject", *selection.layers):
        y -= 8 * mm
        color = "#9f4b2d" if key == "subject" else PDF_STYLES[key][0]
        sheet.setStrokeColor(colors.HexColor(color))
        sheet.setLineWidth(1.6)
        sheet.line(right, y + 1.1 * mm, right + 8 * mm, y + 1.1 * mm)
        fit_text(sheet, right + 12 * mm, y, "Selected asset" if key == "subject" else LAYERS[key].label, 9)
    y -= 19 * mm
    for label, value in (
        ("ASSET", asset.name),
        ("MODEL", project.ifc_name),
        ("SITE LAYERS", project.gis_name),
        ("PROJECT CRS", project.crs.to_string()),
        ("ASSET GUID", asset.guid),
        ("DRAWING SCALE", f"1:{round(mm * 1000 / scale):,} at A3"),
    ):
        fit_text(sheet, right, y, label, 8, font="Helvetica-Bold")
        fit_text(sheet, right, y - 5 * mm, value, 9)
        y -= 15 * mm
    sheet.showPage()
    sheet.save()
    return stream.getvalue()


def dxf_bytes(selection, asset, drawing):
    center, features, subject = drawing
    document = ezdxf.new("R2010")
    document.units = units.M
    modelspace = document.modelspace()
    colors_by_layer = {
        "terrain": 8,
        "boundary": 3,
        "slope": 14,
        "roads": 30,
        "buildings": 7,
        "drainage": 6,
        "water": 4,
        "slurry": 20,
        "power": 2,
        "other": 9,
        "subject": 1,
    }
    for key, geometries in {**features, "subject": [subject]}.items():
        layer = key.upper()
        document.layers.new(layer, dxfattribs={"color": colors_by_layer.get(key, 7)})
        for geometry in geometries:
            for part in parts(geometry):
                if part.is_empty:
                    continue
                if part.geom_type == "Point":
                    modelspace.add_point((part.x, part.y), dxfattribs={"layer": layer})
                elif part.geom_type == "Polygon":
                    for ring in (part.exterior, *part.interiors):
                        modelspace.add_lwpolyline(list(ring.coords)[:-1], close=True, dxfattribs={"layer": layer})
                else:
                    modelspace.add_lwpolyline(list(part.coords), dxfattribs={"layer": layer})
    modelspace.add_text(
        " ".join((asset.tag or asset.name).split()),
        dxfattribs={"height": max(0.4, selection.radius / 34), "insert": (center.x, center.y), "layer": "SUBJECT"},
    )
    document.set_modelspace_vport(selection.radius * 2.2, center=(center.x, center.y))
    stream = StringIO()
    document.write(stream)
    return stream.getvalue().encode("utf-8")


def _draw_arrow(sheet, x1, y1, x2, y2, stroke="#991b1b"):
    sheet.saveState()
    sheet.setStrokeColor(colors.HexColor(stroke))
    sheet.setFillColor(colors.HexColor(stroke))
    sheet.setLineWidth(1.5)
    sheet.line(x1, y1, x2, y2)
    angle = math.atan2(y2 - y1, x2 - x1)
    head = 3.2 * mm
    for offset in (2.6, -2.6):
        hx = x2 + head * math.cos(angle + offset)
        hy = y2 + head * math.sin(angle + offset)
        sheet.line(x2, y2, hx, hy)
    sheet.restoreState()


def comparison_pdf_bytes(
    *,
    project_id: str,
    rev_a_name: str,
    rev_b_name: str,
    gis_name: str,
    crs_string: str,
    focus_label: str,
    focus_guid_a: str | None,
    focus_guid_b: str | None,
    center,
    radius_m: float,
    gis_clipped: dict,
    unchanged_footprints: list,
    moved_items: list[dict],
    proximity_items: list[dict],
    utility_layer: str,
    buffer_m: float,
) -> bytes:
    """Render an A3 Landscape Before/After Revision Comparison drawing with full traceability."""
    stream = BytesIO()
    width, height = landscape(A3)
    sheet = canvas.Canvas(stream, pagesize=(width, height))
    sheet.setTitle(f"GeoBIM Revision Comparison - {focus_label}")

    ink = colors.HexColor("#1d3026")
    muted = colors.HexColor("#4c5d53")
    sheet.setFillColor(ink)
    fit_text(sheet, 14 * mm, height - 18 * mm, "GIS/BIM Revision Comparison / 设计换版前后对照图", 18, 260 * mm, "Helvetica-Bold")
    fit_text(
        sheet,
        14 * mm,
        height - 26 * mm,
        f"Focus: {focus_label}  |  Radius: {radius_m:g} m  |  Utility check: {utility_layer} (buffer {buffer_m:g} m)",
        9.5,
        260 * mm,
    )
    fit_text(sheet, 286 * mm, height - 18 * mm, "GEOBIM REVISION REVIEW", 10.5, 120 * mm, "Helvetica-Bold")
    fit_text(sheet, 286 * mm, height - 26 * mm, f"Project: {project_id}  |  A3 Landscape", 8.5, 120 * mm)

    frame_x, frame_y, frame_w, frame_h = 14 * mm, 18 * mm, 262 * mm, 240 * mm
    side = min(frame_w, frame_h) - 10 * mm
    scale = side / (2 * radius_m)

    def to_page(x, y):
        return frame_x + frame_w / 2 + (x - center.x) * scale, frame_y + frame_h / 2 + (y - center.y) * scale

    sheet.saveState()
    sheet.setFillColor(colors.HexColor("#f5f7f2"))
    sheet.setStrokeColor(colors.HexColor("#a5b5aa"))
    sheet.setLineWidth(0.75)
    sheet.rect(frame_x, frame_y, frame_w, frame_h, stroke=1, fill=1)
    clip_path = sheet.beginPath()
    clip_path.rect(frame_x, frame_y, frame_w, frame_h)
    sheet.clipPath(clip_path, stroke=0, fill=0)

    for key in ("boundary", "terrain", "slope", "roads", "drainage", "water", "slurry", "power", "other"):
        stroke, fill = PDF_STYLES[key]
        lw = 0.35 if key == "terrain" else (1.2 if key in (utility_layer, "slurry", "slope") else 0.85)
        dash = (5, 3) if key == "slope" else None
        for geometry in gis_clipped.get(key, ()):
            draw_shape(sheet, geometry, to_page, stroke, fill, lw, dash=dash)

    for geom in unchanged_footprints:
        draw_shape(sheet, geom, to_page, "#657a6d", "#d5e0d9", 0.7)

    for item in proximity_items:
        geom = item.get("_clipped_geometry")
        if geom is None or geom.is_empty:
            continue
        is_violation = item.get("compliance_status") == "violation" or item["severity"] in ("high", "critical")
        stroke = "#dc2626" if is_violation else "#0284c7"
        draw_shape(sheet, geom, to_page, stroke, None, 2.2)
        rep = geom.representative_point()
        px, py = to_page(rep.x, rep.y)
        note = f"{item['utility_id']} ({item['distance_rev_b_m']:.1f}m)"
        if item["elevation_note"] == "待核实高程":
            note += " 待核实高程"
        sheet.setFillColor(colors.HexColor("#7f1d1d" if is_violation else "#0c4a6e"))
        fit_text(sheet, px + 2 * mm, py + 1.5 * mm, note, 7.5, 70 * mm)

    for moved in moved_items:
        geom_a = moved.get("_footprint_a")
        geom_b = moved.get("_footprint_b")
        if geom_a is not None and not geom_a.is_empty:
            draw_shape(sheet, geom_a, to_page, "#d97706", "#fde68a", 1.4, dash=(4, 2))
        if geom_b is not None and not geom_b.is_empty:
            draw_shape(sheet, geom_b, to_page, "#b91c1c", "#fca5a5", 1.7)
        if geom_a is not None and geom_b is not None and not geom_a.is_empty and not geom_b.is_empty:
            ca, cb = geom_a.centroid, geom_b.centroid
            x1, y1 = to_page(ca.x, ca.y)
            x2, y2 = to_page(cb.x, cb.y)
            _draw_arrow(sheet, x1, y1, x2, y2, "#991b1b")
            tag = moved.get("tag") or moved.get("name") or "Asset"
            disp = moved.get("displacement_m", 0.0)
            sheet.setFillColor(colors.HexColor("#7c2d12"))
            fit_text(sheet, x2 + 2.5 * mm, y2 + 2.5 * mm, f"{tag} Δ={disp:.2f}m", 8.5, 55 * mm, "Helvetica-Bold")
            sheet.setFillColor(colors.HexColor("#92400e"))
            fit_text(sheet, x1 - 12 * mm, y1 - 4 * mm, f"{tag} (Rev A)", 7.5, 45 * mm)

    sheet.restoreState()

    sheet.setFillColor(ink)
    sheet.setStrokeColor(ink)
    sheet.setLineWidth(0.65)
    nx, ny = frame_x + 10 * mm, frame_y + 13 * mm
    sheet.line(nx, ny, nx, ny + 11 * mm)
    sheet.line(nx, ny + 11 * mm, nx - 2 * mm, ny + 7 * mm)
    sheet.line(nx, ny + 11 * mm, nx + 2 * mm, ny + 7 * mm)
    fit_text(sheet, nx - 4 * mm, ny + 13 * mm, "Grid N", 7.5, 20 * mm)

    bar = next((value for value in (500, 200, 100, 50, 20, 10, 5, 2) if value <= radius_m / 2), 2)
    bx, by = frame_x + 22 * mm, frame_y + 9 * mm
    sheet.line(bx, by, bx + bar * scale, by)
    for tick in (bx, bx + bar * scale):
        sheet.line(tick, by - 1.2 * mm, tick, by + 1.2 * mm)
    fit_text(sheet, bx, by + 2.2 * mm, f"{bar:g} m  (1:{round(mm * 1000 / scale):,} at A3)", 7.5, 65 * mm)

    right = 286 * mm
    col_w = 120 * mm
    y = 254 * mm

    sheet.setFillColor(ink)
    fit_text(sheet, right, y, "LEGEND / 图例", 9.5, col_w, "Helvetica-Bold")
    y -= 6 * mm
    legend_rows = (
        ("#d97706", "Rev A (Before / 旧版位置)", (3, 2)),
        ("#b91c1c", "Rev B (After / 新版位置)", None),
        ("#dc2626", "Clearance violation / 违规或高风险管线", None),
        ("#0284c7", "Review utility / 复核范围内管线", None),
        ("#9c5a3c", "Slope / Bench edge / 台阶边坡线", (5, 3)),
        ("#657a6d", "Unchanged IFC footprints / 未变动构筑物", None),
    )
    for stroke, label, dash in legend_rows:
        sheet.saveState()
        sheet.setStrokeColor(colors.HexColor(stroke))
        sheet.setLineWidth(1.8)
        if dash:
            sheet.setDash(*dash)
        sheet.line(right, y + 1.2 * mm, right + 9 * mm, y + 1.2 * mm)
        sheet.restoreState()
        sheet.setFillColor(ink)
        fit_text(sheet, right + 12 * mm, y, label, 8, col_w - 12 * mm)
        y -= 5.2 * mm

    y -= 3.5 * mm
    sheet.setStrokeColor(colors.HexColor("#c7d1ca"))
    sheet.line(right, y + 2 * mm, right + col_w, y + 2 * mm)
    y -= 4 * mm
    sheet.setFillColor(ink)
    fit_text(sheet, right, y, f"MOVED ASSETS / 位置变动设备 ({len(moved_items)})", 9, col_w, "Helvetica-Bold")
    y -= 5.5 * mm
    if not moved_items:
        sheet.setFillColor(muted)
        fit_text(sheet, right, y, "No displaced assets detected.", 8, col_w)
        y -= 5.2 * mm
    else:
        for moved in moved_items[:5]:
            tag = moved.get("tag") or moved.get("name")
            method = "ID变化候选匹配" if moved.get("id_changed") else "GlobalId匹配"
            line = f"• {tag} ({moved.get('name', '')}): Δ={moved['displacement_m']:.2f}m [{method}]"
            sheet.setFillColor(ink)
            fit_text(sheet, right, y, line, 7.8, col_w)
            y -= 5.0 * mm

    y -= 3.5 * mm
    sheet.setStrokeColor(colors.HexColor("#c7d1ca"))
    sheet.line(right, y + 2 * mm, right + col_w, y + 2 * mm)
    y -= 4 * mm
    sheet.setFillColor(ink)
    fit_text(sheet, right, y, f"UTILITIES & CLEARANCE / 需复核管线与边坡 ({len(proximity_items)})", 9, col_w, "Helvetica-Bold")
    y -= 5.5 * mm
    if not proximity_items:
        sheet.setFillColor(muted)
        fit_text(sheet, right, y, f"No {utility_layer} lines within {buffer_m:g} m.", 8, col_w)
        y -= 5.2 * mm
    else:
        for item in proximity_items[:7]:
            flag = " [待核实高程]" if item["elevation_note"] == "待核实高程" else f" ({item['elevation_note']})"
            tag_rule = " [违规]" if item.get("compliance_status") == "violation" else ""
            line = f"• {item['utility_id']} {item['utility_name']}: {item['distance_rev_a_m']:.1f}→{item['distance_rev_b_m']:.1f}m{tag_rule}{flag}"
            sheet.setFillColor(
                colors.HexColor("#991b1b")
                if (item["elevation_note"] == "待核实高程" or item.get("compliance_status") == "violation")
                else ink
            )
            fit_text(sheet, right, y, line, 7.5, col_w)
            y -= 5.0 * mm

    ty = min(y - 4 * mm, 90 * mm)
    sheet.setStrokeColor(colors.HexColor("#c7d1ca"))
    sheet.line(right, ty + 3 * mm, right + col_w, ty + 3 * mm)
    ty -= 3 * mm
    sheet.setFillColor(ink)
    fit_text(sheet, right, ty, "TRACEABILITY / 可追溯信息", 9, col_w, "Helvetica-Bold")
    ty -= 5.2 * mm
    for label, val in (
        ("PROJECT ID", project_id),
        ("REV A (BEFORE)", rev_a_name),
        ("REV B (AFTER)", rev_b_name),
        ("GIS / CAD FILE", gis_name),
        ("PROJECT CRS", crs_string),
        ("FOCUS ASSET", focus_label),
        ("REV A GUID", focus_guid_a or "N/A"),
        ("REV B GUID", focus_guid_b or "N/A"),
    ):
        sheet.setFillColor(muted)
        fit_text(sheet, right, ty, f"{label}:", 7.2, 34 * mm, "Helvetica-Bold")
        sheet.setFillColor(ink)
        fit_text(sheet, right + 34 * mm, ty, str(val), 7.5, col_w - 34 * mm)
        ty -= 4.7 * mm

    sheet.showPage()
    sheet.save()
    return stream.getvalue()


def comparison_dxf_bytes(
    *,
    center,
    radius_m: float,
    gis_clipped: dict,
    unchanged_footprints: list,
    moved_items: list[dict],
    proximity_items: list[dict],
) -> bytes:
    """Export editable 2D DXF for the before/after revision comparison in projected CRS metres."""
    document = ezdxf.new("R2010")
    document.units = units.M
    modelspace = document.modelspace()
    layer_colors = {
        "BOUNDARY": 3,
        "TERRAIN": 8,
        "SLOPE": 14,
        "ROADS": 30,
        "DRAINAGE": 6,
        "WATER": 4,
        "SLURRY": 20,
        "POWER": 2,
        "OTHER": 9,
        "UNCHANGED": 7,
        "REV_A_BEFORE": 40,
        "REV_B_AFTER": 1,
        "DISPLACEMENT": 1,
        "UTILITY_REVIEW": 5,
    }
    for lname, color in layer_colors.items():
        document.layers.new(lname, dxfattribs={"color": color})

    def emit(geom, layer_name):
        if geom is None or geom.is_empty:
            return
        for part in parts(geom):
            if part.is_empty:
                continue
            if part.geom_type == "Point":
                modelspace.add_point((part.x, part.y), dxfattribs={"layer": layer_name})
            elif part.geom_type == "Polygon":
                for ring in (part.exterior, *part.interiors):
                    modelspace.add_lwpolyline(list(ring.coords)[:-1], close=True, dxfattribs={"layer": layer_name})
            else:
                modelspace.add_lwpolyline(list(part.coords), dxfattribs={"layer": layer_name})

    for key, geoms in gis_clipped.items():
        lname = key.upper() if key.upper() in layer_colors else "OTHER"
        for geom in geoms:
            emit(geom, lname)

    for geom in unchanged_footprints:
        emit(geom, "UNCHANGED")

    for moved in moved_items:
        geom_a = moved.get("_footprint_a")
        geom_b = moved.get("_footprint_b")
        emit(geom_a, "REV_A_BEFORE")
        emit(geom_b, "REV_B_AFTER")
        if geom_a is not None and geom_b is not None and not geom_a.is_empty and not geom_b.is_empty:
            ca, cb = geom_a.centroid, geom_b.centroid
            modelspace.add_line((ca.x, ca.y), (cb.x, cb.y), dxfattribs={"layer": "DISPLACEMENT"})
            tag = moved.get("tag") or moved.get("name") or "Asset"
            modelspace.add_text(
                f"{tag} d={moved['displacement_m']:.2f}m",
                dxfattribs={"height": max(0.5, radius_m / 45), "insert": (cb.x, cb.y), "layer": "REV_B_AFTER"},
            )

    for item in proximity_items:
        geom = item.get("_clipped_geometry")
        emit(geom, "UTILITY_REVIEW")
        if geom is not None and not geom.is_empty:
            rep = geom.representative_point()
            label = item["utility_id"] + (" [VERIFY ELEV]" if item["elevation_note"] == "待核实高程" else "")
            modelspace.add_text(
                label,
                dxfattribs={"height": max(0.4, radius_m / 55), "insert": (rep.x, rep.y), "layer": "UTILITY_REVIEW"},
            )

    document.set_modelspace_vport(radius_m * 2.2, center=(center.x, center.y))
    stream = StringIO()
    document.write(stream)
    return stream.getvalue().encode("utf-8")


ISSUE_CSV_FIELDS = [
    "issue_id",
    "project_id",
    "category",
    "severity",
    "compliance_status",
    "rule_code",
    "status",
    "asset_tag",
    "asset_name",
    "ifc_class",
    "rev_a_file",
    "rev_a_guid",
    "rev_b_file",
    "rev_b_guid",
    "match_method",
    "displacement_m",
    "gis_file",
    "gis_layer",
    "utility_id",
    "utility_name",
    "distance_rev_a_m",
    "distance_rev_b_m",
    "min_horizontal_clearance_m",
    "bury_depth_m",
    "elevation_note",
    "crs",
    "summary",
    "recommended_action",
]


def issues_csv_bytes(rows: list[dict]) -> bytes:
    """Serialize review issues into UTF-8-SIG CSV for clean Chinese rendering in Excel."""
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=ISSUE_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: row.get(field, "") for field in ISSUE_CSV_FIELDS})
    return buffer.getvalue().encode("utf-8-sig")
