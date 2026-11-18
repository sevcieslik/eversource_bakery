"""Regression for R01.05 RoW boundaries at Structures and GPKG re-export."""
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
from shapely.geometry import LineString, Point

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eversource_pipeline_core import (  # noqa: E402
    apply_row_boundaries_at_structures,
    circuit_key,
    export_geometry_from_gpkg,
    spans_one_row_per_feature,
    write_export_settings,
)

CRS = 6525
U2M = 0.3048006096
A, B, C = (0, 0), (500, 0), (1000, 300)  # RoW R1 ends and R2 starts at B
kmz = {circuit_key("X1"): {"rows": {"R1": LineString([A, B]), "R2": LineString([B, C])}}}

spans = gpd.GeoDataFrame([
    {"ROW": "R1", "CIRCUIT": "X1", "VOLTAGE": "115", "COMPANY": "PSNH", "BUNDLE_ID": 1,
     "STRUCT_A": "SA", "STRUCT_B": "SB", "geometry": LineString([A, B])},
    {"ROW": "R2", "CIRCUIT": "X1", "VOLTAGE": "115", "COMPANY": "PSNH", "BUNDLE_ID": 2,
     "STRUCT_A": "SB", "STRUCT_B": "SC", "geometry": LineString([B, C])},
], crs=CRS)
structs = gpd.GeoDataFrame([
    {"ROW": "R1", "CIRCUIT": "X1", "STRUCT_ID": "1", "TOPO_ID": "SA", "geometry": Point(A)},
    {"ROW": "R2", "CIRCUIT": "X1", "STRUCT_ID": "2", "TOPO_ID": "SB", "geometry": Point(B)},
    {"ROW": "R2", "CIRCUIT": "X1", "STRUCT_ID": "3", "TOPO_ID": "SC", "geometry": Point(C)},
], crs=CRS)
# Wire 0 lies on span A-B but was given R2 by the nearest-RoW rule.
cayd = gpd.GeoDataFrame([
    {"ROW": "R2", "CIRCUIT": "X1", "WIRE_ID": "W0", "SOURCE_IDX": 0, "WIRE_TYPE": "PHASE",
     "geometry": LineString([(0, 2, 50), (500, 2, 50)])},
    {"ROW": "R2", "CIRCUIT": "X1", "WIRE_ID": "W1", "SOURCE_IDX": 1, "WIRE_TYPE": "PHASE",
     "geometry": LineString([(500, 2, 50), (1000, 302, 50)])},
], crs=CRS)
bundles = [{"BUNDLE_ID": 1, "ALL_WIRE_ROWS": [0]}, {"BUNDLE_ID": 2, "ALL_WIRE_ROWS": [1]}]

se, sf, cw, qa = apply_row_boundaries_at_structures(spans, structs, cayd, bundles, kmz, {"R1", "R2"}, U2M)
assert cw.loc[cw.WIRE_ID == "W0", "ROW"].tolist() == ["R1"], "span wire must follow its span RoW"
assert cw.loc[cw.WIRE_ID == "W1", "ROW"].tolist() == ["R2"]
assert sorted(sf.loc[sf.TOPO_ID == "SB", "ROW"]) == ["R1", "R2"], "boundary Structure in both RoWs"
assert sorted(sf.loc[sf.TOPO_ID == "SA", "ROW"]) == ["R1"]
assert list(se["ROWS"]) == ["R1", "R2"], "no overlap duplication when RoW lines meet at a Structure"

# Overlap: R2's SoW line also runs along A-B -> span A-B delivered in both RoWs.
kmz2 = {circuit_key("X1"): {"rows": {"R1": LineString([A, B]), "R2": LineString([A, B, C])}}}
se2, sf2, cw2, _ = apply_row_boundaries_at_structures(spans, structs, cayd, bundles, kmz2, {"R1", "R2"}, U2M)
assert se2.loc[se2.BUNDLE_ID == 1, "ROWS"].item() == "R1;R2"
assert sorted(cw2.loc[cw2.WIRE_ID == "W0", "ROW"]) == ["R1", "R2"]
assert sorted(sf2.loc[sf2.TOPO_ID == "SA", "ROW"]) == ["R1", "R2"]

# GPKG re-export: an operator moves W1 to R1 and removes Structure 3.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    gpkg = td / "01_geometry" / "g.gpkg"
    gpkg.parent.mkdir()
    spans_one_row_per_feature(se).to_file(gpkg, layer="selected_span_wires", driver="GPKG")
    wires2d = gpd.GeoDataFrame([{"ROW": "R1;R2", "CIRCUIT": "X1", "VOLTAGE": "115",
                                 "geometry": LineString([A, B, C])}], crs=CRS)
    wires2d.to_file(gpkg, layer="wires_2d", driver="GPKG")
    sf_e = sf[sf.STRUCT_ID != "3"]
    sf_e.to_file(gpkg, layer="structures_final", driver="GPKG")
    cw_e = cw.copy()
    cw_e.loc[cw_e.WIRE_ID == "W1", "ROW"] = "R1"
    cw_e.to_file(gpkg, layer="caydence_wires_3d", driver="GPKG")
    write_export_settings(gpkg, year=2026, selected_rows={"R1", "R2"}, unit_to_m=U2M, epsg=CRS)
    inv, checks = export_geometry_from_gpkg(gpkg, td / "deliverables")
    d = td / "deliverables"
    assert (d / "R1/Circuit/X1/X1_Wires_2026.shp").is_file()
    assert (d / "R2/Circuit/X1/X1_Wires_2026.shp").is_file()
    assert len(gpd.read_file(d / "R1/Caydence/R1_3D_Wires_2026.shp")) == 2
    assert not (d / "R2/Caydence/R2_3D_Wires_2026.shp").exists()
    w1 = gpd.read_file(d / "R1/Circuit/X1/X1_Wires_2026.shp").geometry.iloc[0]
    assert abs(w1.length - 500) <= 0.5 / U2M + 0.01, w1.length  # ends at Structure B (0.5 m clip tolerance)
    assert "SPAN_END_WITHOUT_STRUCTURE_IN_ROW" in set(checks["CHECK"]), "deleted Structure 3 is flagged"

print("R01.05 RoW boundary + GPKG re-export regression: PASS (12 checks)")
