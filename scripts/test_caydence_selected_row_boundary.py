"""Regression: an unselected span must not hide its Caydence wires from a selected RoW."""
from pathlib import Path
import sys

import geopandas as gpd
from shapely.geometry import LineString, Point

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eversource_pipeline_core import apply_row_boundaries_at_structures, circuit_key

CRS = 6434
U2M = 0.3048006096
def geo(rows):
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=CRS)

# A whole-circuit span is assigned CT_17, but the run selects CT_06,CT_16.
# The individual DXF source wire has already been assigned CT_16 from SoW.
# Old apply_row_boundaries_at_structures overwrote that with CT_17 (SPAN),
# so product 267 in CT_16 did not contain the wire.
spans = geo([{
    "ROW": "CT_17", "CIRCUIT": "310", "BUNDLE_ID": 82,
    "STRUCT_A": "ST00196", "STRUCT_B": "ST00192",
    "geometry": LineString([(0, 0), (720, 0)]),
}])
structs = geo([
    {"ROW": "CT_16", "CIRCUIT": "310", "TOPO_ID": "ST00196", "geometry": Point(0, 0)},
    {"ROW": "CT_17", "CIRCUIT": "310", "TOPO_ID": "ST00192", "geometry": Point(720, 0)},
])
wires = geo([{
    "ROW": "CT_16", "CIRCUIT": "310", "SOURCE_IDX": 379,
    "WIRE_ID": "DXF_W000380", "geometry": LineString([(0, 4), (720, 4)]),
}])
bundles = [{"BUNDLE_ID": 82, "ALL_WIRE_ROWS": [379]}]
kmz = {circuit_key("310"): {"rows": {
    "CT_16": LineString([(0, 0), (35, 0)]),
    "CT_17": LineString([(0, 0), (720, 0)]),
}}}

_, _, out, qa = apply_row_boundaries_at_structures(
    spans, structs, wires, bundles, kmz, {"CT_06", "CT_16"}, U2M)
assert out["ROW"].tolist() == ["CT_16"], out[["WIRE_ID", "ROW"]].to_dict("records")
assert out["ROW_RULE"].tolist() == ["SPAN_OUTSIDE_SELECTION"]
assert "CAYDENCE_SPAN_ROW_OUTSIDE_SELECTION" in qa["KIND"].tolist()

# When the span's RoW is selected, its explicit span ownership still wins.
_, _, out2, _ = apply_row_boundaries_at_structures(
    spans, structs, wires, bundles, kmz, {"CT_16", "CT_17"}, U2M)
assert out2.loc[out2["WIRE_ID"] == "DXF_W000380", "ROW"].tolist() == ["CT_17"]
assert out2.loc[out2["WIRE_ID"] == "DXF_W000380", "ROW_RULE"].tolist() == ["SPAN"]
print("Selected-RoW Caydence boundary regression: PASS")
