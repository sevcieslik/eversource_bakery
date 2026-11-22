"""Regression for C39 source 310:9932 / POA PT00203 omitted from product 45.

The Structure exists in the analysis but owns an unselected RoW (CT_17).
Two original 3D wires end at it and are already selected for CT_16.
Only the selected-RoW deliverable copy is added; source IDs stay intact.
"""
from pathlib import Path
import sys
import geopandas as gpd
from shapely.geometry import LineString, Point
sys.path.insert(0, str(Path(__file__).resolve().parent))
from eversource_pipeline_core import apply_row_boundaries_at_structures, circuit_key

CRS=6434
U2M=0.3048006096
def gdf(rows):
    return gpd.GeoDataFrame(rows,geometry="geometry",crs=CRS)

span=gdf([dict(ROW="CT_17",CIRCUIT="310",BUNDLE_ID=82,
    STRUCT_A="ST00196",STRUCT_B="ST00192",geometry=LineString([(0,0),(500,0)]))])
structures=gdf([
    dict(ROW="CT_17",CIRCUIT="310",TOPO_ID="ST00192",STRUCT_ID="9932",
         SRC_GID="source-310-9932",GLOBALID="source-310-9932",geometry=Point(500,0)),
    dict(ROW="CT_16",CIRCUIT="371",TOPO_ID="ST00193",STRUCT_ID="5792",
         SRC_GID="source-371-5792",geometry=Point(500,50))
])
wires=gdf([dict(ROW="CT_16",CIRCUIT="310",SOURCE_IDX=n,
                WIRE_ID=f"DXF_W{n:06d}",geometry=LineString([(0,dy),(500,dy)]))
           for n,dy in ((379,3),(380,4))])
bundles=[dict(BUNDLE_ID=82,ALL_WIRE_ROWS=[379,380])]
# Use an endpoint 1.5 m from the POA for each recovered wire.
wires.loc[0,"geometry"]=LineString([(0,3),(500,3)])
wires.loc[1,"geometry"]=LineString([(0,4),(500,4)])
# Coordinates are US survey feet: offsets 3/4 feet are under 3 m.
kmz={circuit_key("310"):dict(rows={"CT_17":LineString([(0,0),(500,0)]),
                                  "CT_16":LineString([(0,200),(500,200)])})}
_,sf,cw,qa=apply_row_boundaries_at_structures(span,structures,wires,bundles,kmz,
                                              {"CT_06","CT_16"},U2M)
st=sf[(sf.CIRCUIT=="310")&(sf.TOPO_ID=="ST00192")]
assert sorted(st.ROW.tolist())==["CT_16","CT_17"],st.ROW.tolist()
assert st.loc[st.ROW=="CT_16","ROW_RULE"].iloc[0]=="BOUNDARY_WIRE_POA_RECOVERY"
assert st.loc[st.ROW=="CT_16","SRC_GID"].iloc[0]=="source-310-9932"
assert "BOUNDARY_POA_STRUCTURE_RECOVERED" in set(qa.KIND)
assert all(cw.ROW=="CT_16")

# Without two distinct matching 3D wires, do not recover automatically.
_,sf2,_,qa2=apply_row_boundaries_at_structures(span,structures,wires.iloc[:1],
                                               bundles,kmz,{"CT_06","CT_16"},U2M)
assert len(sf2[(sf2.CIRCUIT=="310")&(sf2.TOPO_ID=="ST00192")])==1
assert "BOUNDARY_POA_STRUCTURE_RECOVERED" not in set(qa2.KIND)

# No selected RoW limitation means no additional copy.
_,sf3,_,_=apply_row_boundaries_at_structures(span,structures,wires,bundles,kmz,set(),U2M)
assert len(sf3[(sf3.CIRCUIT=="310")&(sf3.TOPO_ID=="ST00192")])==1
print("Boundary POA Structure recovery: PASS")
