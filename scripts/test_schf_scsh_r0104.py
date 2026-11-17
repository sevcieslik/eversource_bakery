"""Regression for R01.04 SCSH single-top positioning and SCHF preservation."""
import geopandas as gpd
from shapely.geometry import Point, LineString
from eversource_pipeline_core import relocate_schf_to_middle_phase

CRS='EPSG:6434'

def inputs(kind='SCSH', others=(), circuit='3361'):
    structures=gpd.GeoDataFrame([dict(STR_TYPE=kind,CIRCUIT=circuit,STRUCT_ID='13~106',
        TOPO_ID='ST1',geometry=Point(100,5))],geometry='geometry',crs=CRS)
    phase=gpd.GeoDataFrame([dict(WIRE_TYPE='PHASE',CIRCUIT='3361',WIRE_ID=str(i),
        geometry=LineString([(0,y,30),(200,y,30)])) for i,y in enumerate((0,10,20))],geometry='geometry',crs=CRS)
    poa=gpd.GeoDataFrame([dict(SOW_CIRCUIT='3361',TOPO_ID='ST1',geometry=Point(100,5))] +
        [dict(SOW_CIRCUIT=c,TOPO_ID='ST1',geometry=Point(x,y)) for x,y,c in others],
        geometry='geometry',crs=CRS)
    return structures,phase,poa

def check():
    s,w,p=inputs()
    out,qa=relocate_schf_to_middle_phase(s,w,1.,raw_poa=p)
    assert qa.iloc[0].STATUS=='MOVED' and qa.iloc[0].NEARBY_TOPS==1
    assert out.geometry.iloc[0].equals(Point(100,10)) and out.iloc[0].POS_RULE=='SCSH_MIDDLE_PHASE'
    assert s.geometry.iloc[0].equals(Point(100,5))
    # 2+ separate nearby tower tops of same circuit means possible trident.
    s,w,p=inputs(others=[(100,0,'3361'),(100,12,'3361')])
    out,qa=relocate_schf_to_middle_phase(s,w,1.,raw_poa=p)
    assert qa.iloc[0].STATUS=='REVIEW' and qa.iloc[0].REASON=='MULTIPLE_NEARBY_TOWER_TOPS'
    assert qa.iloc[0].NEARBY_TOPS==3 and out.geometry.iloc[0].equals(s.geometry.iloc[0])
    # Different circuit attachment marker does not block the same-circuit top.
    s,w,p=inputs(others=[(100,12,'336')])
    out,qa=relocate_schf_to_middle_phase(s,w,1.,raw_poa=p)
    assert qa.iloc[0].STATUS=='MOVED'
    # Duplicate same top within 0.75 m does not imply trident.
    s,w,p=inputs(others=[(100.2,5,'3361')])
    out,qa=relocate_schf_to_middle_phase(s,w,1.,raw_poa=p)
    assert qa.iloc[0].STATUS=='MOVED' and qa.iloc[0].NEARBY_TOPS==1
    # Without original POA data: no SCSH guessing.
    s,w,p=inputs()
    out,qa=relocate_schf_to_middle_phase(s,w,1.,raw_poa=None)
    assert qa.iloc[0].REASON=='NO_RAW_POA_FOR_SCSH'
    # Keep SCHF behavior unchanged even without raw POA.
    s,w,p=inputs('SCHF')
    out,qa=relocate_schf_to_middle_phase(s,w,1.)
    assert qa.iloc[0].STATUS=='MOVED' and out.geometry.iloc[0].equals(Point(100,10))
    # One marker from wrong circuit does not license SCSH relocation.
    s,w,p=inputs()
    p.at[0,'SOW_CIRCUIT']='336'
    out,qa=relocate_schf_to_middle_phase(s,w,1.,raw_poa=p)
    assert qa.iloc[0].REASON=='NO_SAME_CIRCUIT_POA'
    print('R01.04 SCHF/SCSH regression: PASS (7 cases)')

if __name__=='__main__': check()
