"""Standalone R01.03 geometry regression. Run with python test_schf_r0103.py."""
import geopandas as gpd
from shapely.geometry import Point, LineString
from eversource_pipeline_core import relocate_schf_to_middle_phase


def structures():
    return gpd.GeoDataFrame([
        dict(STR_TYPE='SCHF', CIRCUIT='3361', STRUCT_ID='13~11', TOPO_ID='a', geometry=Point(100, 5)),
        dict(STR_TYPE='SCSP', CIRCUIT='3361', STRUCT_ID='other', TOPO_ID='b', geometry=Point(100, 5)),
    ], geometry='geometry', crs='EPSG:6434')


def phases(ys=(0, 10, 20)):
    return gpd.GeoDataFrame([
        dict(WIRE_TYPE='PHASE', CIRCUIT='3361', WIRE_ID=str(i),
             geometry=LineString([(0,y,30),(200,y,30)])) for i,y in enumerate(ys)
    ], geometry='geometry', crs='EPSG:6434')


def run():
    original = structures()
    moved, qa = relocate_schf_to_middle_phase(original, phases(), 1.)
    assert qa.iloc[0].STATUS == 'MOVED'
    assert moved.geometry.iloc[0].x == 100 and moved.geometry.iloc[0].y == 10
    assert moved.geometry.iloc[1].equals(original.geometry.iloc[1])
    assert original.geometry.iloc[0].y == 5
    # Ambiguous: four phase wires
    unchanged, qa = relocate_schf_to_middle_phase(original, phases((0,10,20,30)),1.)
    assert qa.iloc[0].STATUS == 'REVIEW'
    assert unchanged.geometry.iloc[0].equals(original.geometry.iloc[0])
    # Wrong circuit
    wrong = phases()
    wrong.at[2,'CIRCUIT'] = '336'
    unchanged, qa = relocate_schf_to_middle_phase(original,wrong,1.)
    assert qa.iloc[0].STATUS == 'REVIEW'
    # Missing overlap: wires stop 30 m before the support station
    far = phases()
    far.at[0,'geometry'] = LineString([(0,0,30),(70,0,30)])
    unchanged, qa = relocate_schf_to_middle_phase(original,far,1.)
    assert qa.iloc[0].STATUS == 'REVIEW'
    # Non-parallel
    skew = phases()
    skew.at[0,'geometry'] = LineString([(0,0,30),(200,90,30)])
    unchanged, qa = relocate_schf_to_middle_phase(original,skew,1.)
    assert qa.iloc[0].STATUS == 'REVIEW'
    print('R01.03 SCHF regression: PASS (5 cases)')


if __name__=='__main__': run()
