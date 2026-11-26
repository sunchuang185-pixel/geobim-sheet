"""Official Python MCP SDK stdio server exposing GeoBIM Sheet revision review tools."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .tools import (
    DEFAULT_WORKSPACE,
    check_utility_proximity as _check_utility_proximity,
    compare_revisions as _compare_revisions,
    export_review_package as _export_review_package,
    find_assets as _find_assets,
    georeference_project as _georeference_project,
    list_layers as _list_layers,
    open_project as _open_project,
)


mcp = FastMCP(
    "geobim-sheet-review",
    instructions=(
        "GIS/BIM design revision review tools for industrial & mining sites. Always use these tools "
        "to validate CRS (or apply explicit user-supplied CRS override via georeference_project), "
        "compare IFC revisions (GlobalId first, then unique Tag + IFC class candidates), check "
        "planar clearances against utility/slope rules (water, slurry, power, drainage, slope, all) "
        "marking missing bury depth as '待核实高程', and export verified PDF/CSV/DXF review packages."
    ),
)


@mcp.tool()
def open_project(
    rev_a_path: str | None = None,
    rev_b_path: str | None = None,
    gis_path: str | None = None,
    preset: str | None = None,
    project_id: str | None = None,
    crs_override: dict | None = None,
) -> dict:
    """Open a two-revision IFC + GIS/DXF review project (or synthetic preset) and verify CRS/coordinates."""
    return _open_project(
        rev_a_path=rev_a_path,
        rev_b_path=rev_b_path,
        gis_path=gis_path,
        preset=preset,
        project_id=project_id,
        crs_override=crs_override,
        workspace=DEFAULT_WORKSPACE,
    )


@mcp.tool()
def georeference_project(
    project_id: str,
    crs: str,
    eastings: float,
    northings: float,
    orthogonal_height: float = 0.0,
    rotation_deg: float = 0.0,
    apply_to_both: bool = False,
) -> dict:
    """Apply explicit user-supplied projected EPSG CRS + origin/rotation override to an un-georeferenced project."""
    return _georeference_project(
        project_id=project_id,
        crs=crs,
        eastings=eastings,
        northings=northings,
        orthogonal_height=orthogonal_height,
        rotation_deg=rotation_deg,
        apply_to_both=apply_to_both,
        workspace=DEFAULT_WORKSPACE,
    )


@mcp.tool()
def find_assets(
    project_id: str,
    query: str = "",
    revision: str = "both",
    tag: str | None = None,
    ifc_class: str | None = None,
) -> dict:
    """Search IFC assets by Chinese/English name, tag, or IFC class; reports ambiguous matches."""
    return _find_assets(
        project_id=project_id,
        query=query,
        revision=revision,
        tag=tag,
        ifc_class=ifc_class,
        workspace=DEFAULT_WORKSPACE,
    )


@mcp.tool()
def list_layers(project_id: str) -> dict:
    """List GIS/DXF site layers, feature counts, CRS, and utility lines missing bury depth/elevation."""
    return _list_layers(project_id=project_id, workspace=DEFAULT_WORKSPACE)


@mcp.tool()
def compare_revisions(
    project_id: str,
    asset_query: str | None = None,
    min_displacement_m: float = 0.05,
    confirmed_matches: list[dict] | None = None,
) -> dict:
    """Compare Rev A and Rev B IFC assets (GlobalId first, unique Tag+IFC class candidate fallback)."""
    return _compare_revisions(
        project_id=project_id,
        asset_query=asset_query,
        min_displacement_m=min_displacement_m,
        confirmed_matches=confirmed_matches,
        workspace=DEFAULT_WORKSPACE,
    )


@mcp.tool()
def check_utility_proximity(
    project_id: str,
    asset_guid: str | None = None,
    asset_tag: str | None = None,
    asset_query: str | None = None,
    layer: str = "water",
    buffer_m: float = 30.0,
    revision: str = "both",
) -> dict:
    """Compute planar distances & clearance compliance against utilities/slopes; flags missing bury depth."""
    return _check_utility_proximity(
        project_id=project_id,
        asset_guid=asset_guid,
        asset_tag=asset_tag,
        asset_query=asset_query,
        layer=layer,
        buffer_m=buffer_m,
        revision=revision,
        workspace=DEFAULT_WORKSPACE,
    )


@mcp.tool()
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
) -> dict:
    """Generate A3 before/after comparison PDF, traceable CSV issue list, and optional DXF on disk."""
    return _export_review_package(
        project_id=project_id,
        focus_asset_guid=focus_asset_guid,
        focus_asset_tag=focus_asset_tag,
        focus_asset_query=focus_asset_query,
        utility_layer=utility_layer,
        buffer_m=buffer_m,
        radius_m=radius_m,
        output_dir=output_dir,
        include_dxf=include_dxf,
        workspace=DEFAULT_WORKSPACE,
    )


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
