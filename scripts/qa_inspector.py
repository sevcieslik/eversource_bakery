#!/usr/bin/env python3
"""Eversource independent source -> Bakery GPKG -> deliverables QA.

Read-only. One Bakery run only. Produces HTML/CSV/QA GPKG/maps.
Usage: python qa_inspector.py --wires-dxf ... --poa-dxf ... --source-zip ...
    --gpkg ... --deliverables ... --out ...
Dependencies: geopandas, shapely>=2, ezdxf, pandas, numpy, scipy, matplotlib,
              pyogrio (optional geopandas IO backend).
All metric thresholds are in metres, converted via GPKG CRS.
"""
from __future__ import annotations
import argparse, collections, csv, html, json, math, re, tempfile, zipfile, shutil, sqlite3
from pathlib import Path
from datetime import datetime, timezone

import ezdxf
import geopandas as gpd
import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from shapely.geometry import Point, LineString
import shapely
from shapely.ops import unary_union
from shapely.strtree import STRtree
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def val(v):
    if v is None: return ''
    try:
        if bool(pd.isna(v)): return ''
    except (TypeError, ValueError): pass
    s=str(v).strip()
    return '' if s.lower() in ('nan','none','null') else s


def norm(v): return val(v).strip().lower().strip('{}')

def split(v): return [x.strip() for x in val(v).replace(',', ';').split(';') if x.strip()]

def source_circuit(layer):
    match=re.search(r'(?:^|_)Mainline_([A-Za-z0-9]+)_(\d+)kV',val(layer),re.I)
    if match: return match.group(1),match.group(2)
    match=re.search(r'(\d+)kV',val(layer),re.I)
    return '',match.group(1) if match else ''

def get_layer(gpkg, layer):
    # Optional layers can be absent. An unreadable required layer must NOT
    # silently turn into an empty GeoDataFrame (which hides wrong paths,
    # permission problems, invalid SQLite and driver failures).
    return gpd.read_file(gpkg, layer=layer)


def inspect_geometry_gpkg(gpkg):
    import sqlite3
    gpkg = Path(gpkg).expanduser()
    print(f"[INPUT] Geometry GPKG: {gpkg.resolve()}", flush=True)
    if not gpkg.is_file():
        raise FileNotFoundError(
            f"Geometry GPKG not found: {gpkg}\n"
            "Point --gpkg to Bakery output/01_geometry/01_eversource_geometry.gpkg, "
            "NOT QA_Issues.gpkg or a source GeoPackage."
        )
    if gpkg.stat().st_size == 0:
        raise RuntimeError(f"Geometry GPKG has zero bytes: {gpkg}")
    try:
        with sqlite3.connect(f"file:{gpkg.resolve().as_posix()}?mode=ro", uri=True) as con:
            rows = con.execute("SELECT table_name FROM gpkg_contents").fetchall()
            layers = {row[0] for row in rows}
            required = {'caydence_wires_3d', 'structures_final'}
            print(f"[INPUT] GPKG layers ({len(layers)}): " + ', '.join(sorted(layers)), flush=True)
            absent = required - layers
            if absent:
                raise RuntimeError(
                    f"Geometry GPKG lacks required layers: {', '.join(sorted(absent))}\n"
                    f"Actual file: {gpkg.resolve()}\n"
                    "Check that --gpkg points to the Geometry output, not a QA issues GPKG."
                )
            for name in sorted(required):
                # Table names are fixed literals from trusted code, not user input.
                count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                print(f"[INPUT] {name}: {count:,} records", flush=True)
                if count == 0:
                    raise RuntimeError(f"Geometry GPKG has an empty required layer: {name}")
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"Cannot inspect SQLite/GeoPackage at {gpkg}: {exc}") from exc
    return layers


def source_wires(dxf):
    doc=ezdxf.readfile(str(dxf)); output=[]
    for ix,e in enumerate(x for x in doc.modelspace() if x.dxftype() in ('LWPOLYLINE','POLYLINE','LINE')):
        if e.dxftype()=='LWPOLYLINE':
            pts=np.array([[p.x,p.y,p.z] for p in e.vertices_in_wcs()],dtype=float)
        elif e.dxftype()=='LINE':
            pts=np.array([tuple(e.dxf.start),tuple(e.dxf.end)],dtype=float)
        else:
            pts=np.array([[v.dxf.location.x,v.dxf.location.y,v.dxf.location.z] for v in e.vertices],dtype=float)
        if len(pts)<2:continue
        circ,kv=source_circuit(e.dxf.layer)
        output.append(dict(wire_id=f'DXF_W{ix+1:06d}',layer=e.dxf.layer,circuit=circ,voltage=kv,pts=pts,geometry=LineString(pts)))
    return output


def poa_points(dxf):
    doc=ezdxf.readfile(str(dxf));out=[]
    for ix,e in enumerate(x for x in doc.modelspace() if x.dxftype() in ('POINT','INSERT')):
        p=e.dxf.location if e.dxftype()=='POINT' else e.dxf.insert
        out.append((f'POA_{ix+1:05d}',Point(p.x,p.y)))
    return out


def find_source_structures(source_zip, direct, workspace):
    if direct:
        path=Path(direct)
        if not path.is_file(): raise FileNotFoundError(path)
        return path
    if not source_zip: return None
    z=zipfile.ZipFile(source_zip)
    shp=[n for n in z.namelist() if n.lower().endswith('.shp') and 'structures' in n.lower()]
    if not shp: return None
    root=Path(shp[0]).with_suffix('')
    for name in z.namelist():
        if str(Path(name).with_suffix('')).lower()==str(root).lower():
            z.extract(name,workspace)
    return workspace/shp[0]


def scan_deliverables(root):
    root=Path(root)
    if (root/'deliverables').is_dir():root=root/'deliverables'
    products={'45':list(root.glob('45_structure_shapefile/*/*.shp')),
              '80':list(root.glob('80_powerline_vectors/*/*.shp')),
              '267_w':list(root.glob('267_caydence_conductors/*/*3D_Wires*.shp')),
              '267_s':list(root.glob('267_caydence_conductors/*/*3D_Structures*.shp'))}
    result={}
    for category,files in products.items():
        gs=[]
        for f in sorted(files):
            g=gpd.read_file(f)
            if g.empty:continue
            g=g.copy()
            g['__delivery_file']=str(f.relative_to(root))
            if category in ('267_w','267_s'):g['__file_row']=f.parent.name
            elif category in ('45','80'):g['__file_circuit']=f.parent.name
            gs.append(g)
        result[category]=gpd.GeoDataFrame(pd.concat(gs,ignore_index=True),geometry='geometry',crs=gs[0].crs) if gs else gpd.GeoDataFrame(geometry=[])
    return result,products


def substation_polygons(sqlite_path, crs):
    """Read source *polygons*, not point symbols, from project SoW SQLite."""
    if not sqlite_path:
        return None
    path=Path(sqlite_path)
    if not path.is_file():
        raise FileNotFoundError(f"Source SoW SQLite not found: {path}")
    data=gpd.read_file(path,layer='substations')
    if data.crs is None:
        raise RuntimeError('Source SQLite substations layer has no CRS')
    data=data[data.geometry.notna() & ~data.geometry.is_empty]
    data=data[data.geometry.geom_type.isin(['Polygon','MultiPolygon'])].to_crs(crs)
    return data.reset_index(drop=True)


def iter_line_ends(geom):
    """Return physical end points of each linestring part, not all vertices."""
    if geom is None or geom.is_empty:return
    parts=geom.geoms if hasattr(geom,'geoms') else [geom]
    for part in parts:
        if not isinstance(part, LineString) or len(part.coords)<2:continue
        yield Point(part.coords[0][:2]), Point(part.coords[-1][:2]), part


def build_terminal_audit(wire2, cayd, s45, substations, unit, tol_m=3.0,
                         spans=None, structure_nodes=None, topology_m=30.0):
    """Check terminal support topology before treating lateral XY offsets as missing.

    A 2D circuit end can be on the chosen middle phase, several metres away
    from the representative POA. Matching the same-circuit STRUCT_A/B node via
    selected_span_wires and confirming its physical product-45 support avoids
    false 'missing support' alarms. An offset remains REVIEW, never an auto-fix.
    """
    subst_geoms=list(substations.geometry) if substations is not None else []
    subst_index=STRtree(subst_geoms) if subst_geoms else None
    st_by_circ={str(c):np.asarray([[p.x,p.y] for p in g.geometry if p is not None and not p.is_empty])
                for c,g in s45.groupby(s45.CIRCUIT.astype(str))} if not s45.empty else {}
    st_trees={c:cKDTree(pts) for c,pts in st_by_circ.items() if len(pts)}
    node_by_circ={}
    if structure_nodes is not None and not structure_nodes.empty:
        for c, g in structure_nodes.groupby(structure_nodes.CIRCUIT.astype(str)):
            node_by_circ[str(c)]={}
            for _,r in g.iterrows():
                topo=val(r.get('TOPO_ID'))
                if topo and r.geometry is not None and not r.geometry.is_empty:
                    node_by_circ[str(c)].setdefault(topo,[]).append((r.geometry,val(r.get('ROW'))))
    spans_by_circ={}
    if spans is not None and not spans.empty:
        spans_by_circ={str(c):g for c,g in spans.groupby(spans.CIRCUIT.astype(str))}
    def in_sub(point):
        if subst_index is None:return False
        return any(subst_geoms[int(j)].covers(point) for j in subst_index.query(point))
    def match_struct(c,point):
        tree=st_trees.get(str(c))
        return float(tree.query([point.x,point.y])[0])*unit if tree is not None else None
    def topological_support(c,point,row_values):
        """Only accept a real linked node, with a delivered support close to it."""
        options=[]
        for _,sp in spans_by_circ.get(c,[]).iterrows() if c in spans_by_circ else []:
            sg=sp.geometry
            if sg is None or sg.is_empty:continue
            if point.distance(sg)*unit>topology_m:continue
            sp_rows=set(split(sp.get('ROW')))
            if row_values and sp_rows and not (row_values & sp_rows):continue
            if not hasattr(sg,'coords'):continue
            ends=list(sg.coords)
            for key,coord in [('STRUCT_A',ends[0]),('STRUCT_B',ends[-1])]:
                node_id=val(sp.get(key))
                if not node_id:continue
                node_point=Point(coord[:2])
                span_endpoint_gap=point.distance(node_point)*unit
                if span_endpoint_gap>topology_m:continue
                for support,support_row in node_by_circ.get(c,{}).get(node_id,[]):
                    if row_values and support_row and not (set(split(support_row)) & row_values):continue
                    support_gap=point.distance(support)*unit
                    if support_gap>topology_m:continue
                    delivered_gap=match_struct(c,support)
                    if delivered_gap is None or delivered_gap>tol_m:continue
                    options.append((support_gap,node_id,delivered_gap,span_endpoint_gap,support_row))
        if not options:return None
        return min(options,key=lambda z:z[0])
    records=[]
    for dataset, frame in (('2D',wire2),('3D',cayd)):
        for circuit,sg in frame.groupby(frame.CIRCUIT.astype(str)):
            ends=[]
            for _,r in sg.iterrows():
                for p0,p1,part in iter_line_ends(r.geometry):
                    ends.extend([(p0,(p1.x-p0.x,p1.y-p0.y),val(r.get('ROW'))),
                                 (p1,(p0.x-p1.x,p0.y-p1.y),val(r.get('ROW')))])
            if not ends:continue
            pts=np.array([[p.x,p.y] for p,_,_ in ends]);tree=cKDTree(pts)
            radius=(1.0 if dataset=='2D' else 4.0)/unit
            parent=list(range(len(ends)))
            def root(a):
                while parent[a]!=a:
                    parent[a]=parent[parent[a]];a=parent[a]
                return a
            for a,b in tree.query_pairs(radius):
                ra,rb=root(a),root(b)
                if ra!=rb:parent[rb]=ra
            groups=collections.defaultdict(list)
            for i in range(len(ends)):groups[root(i)].append(i)
            for inds in groups.values():
                if dataset=='2D' and len(inds)>=2:continue
                if dataset=='3D':
                    vectors=[]
                    for i in inds:
                        v=np.asarray(ends[i][1],dtype=float);normv=np.linalg.norm(v)
                        if normv>0:vectors.append(v/normv)
                    through=any(np.dot(a,b)<-0.85 for j,a in enumerate(vectors) for b in vectors[j+1:])
                    if through:continue
                mid=Point(float(np.mean(pts[inds,0])),float(np.mean(pts[inds,1])))
                rows={row for i in inds for row in split(ends[i][2])}
                dist=match_struct(circuit,mid)
                linked=None
                if in_sub(mid):ss='SUBSTATION_TERMINAL'
                elif dist is not None and dist<=tol_m:ss='STRUCTURE'
                elif dataset=='2D':
                    linked=topological_support(circuit,mid,rows)
                    ss='STRUCTURE_LINKED_OFFSET' if linked else 'UNRESOLVED'
                else:ss='UNRESOLVED'
                records.append(dict(DATASET=dataset,CIRCUIT=circuit,ROW=';'.join(sorted(rows)),
                   STATUS=ss,N_ENDPOINTS=len(inds),NEAREST_STRUCTURE_M=round(dist,3) if dist is not None else None,
                   LINKED_TOPO_ID=linked[1] if linked else '',
                   LINKED_STRUCTURE_M=round(linked[0],3) if linked else None,
                   LINKED_PRODUCT45_M=round(linked[2],3) if linked else None,
                   geometry=mid))
    return records


def geometry_delta(src, rec, unit):
    a=np.array(src.coords); b=np.array(rec.coords)
    if len(a)==len(b) and a.shape[1]>=3 and b.shape[1]>=3:
        return min(np.max(np.linalg.norm(a[:,:3]-b[:,:3],axis=1)),np.max(np.linalg.norm(a[:,:3]-b[::-1,:3],axis=1)))*unit
    return src.hausdorff_distance(rec)*unit


def build(args):
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    gpkg=Path(args.gpkg)
    available_layers=inspect_geometry_gpkg(gpkg)
    for src_name, src_path in (("Wires DXF", args.wires_dxf), ("POA DXF", args.poa_dxf)):
        if not Path(src_path).is_file():
            raise FileNotFoundError(f"{src_name} does not exist: {src_path}")
    if not Path(args.deliverables).is_dir():
        raise FileNotFoundError(f"Deliverables folder does not exist: {args.deliverables}")
    wdf=source_wires(args.wires_dxf);poa=poa_points(args.poa_dxf)
    requested=('caydence_wires_3d','structures_final','wires_2d','selected_span_wires','kmz_reference','export_settings','recovered_tap_2d','source_wire_review','source_structure_points')
    gpkg_data={layer:(get_layer(gpkg,layer) if layer in available_layers else gpd.GeoDataFrame(geometry=[],crs=None)) for layer in requested}
    cayd=gpkg_data['caydence_wires_3d'];st=gpkg_data['structures_final'];wire2=gpkg_data['wires_2d']
    if cayd.empty or st.empty: raise RuntimeError('Required Geometry layers are empty; see [INPUT] diagnostics above')
    crs=cayd.crs
    if crs is None:raise RuntimeError('GPKG CRS missing')
    unit=float(crs.axis_info[0].unit_conversion_factor)
    d,files=scan_deliverables(args.deliverables)
    substations=substation_polygons(args.sow_sqlite,crs)
    for typ,g in d.items():
        if not g.empty and g.crs!=crs:d[typ]=g.to_crs(crs)
    sourcepath=find_source_structures(args.source_zip,args.source_structures,out/'_source_temp')
    source=gpd.read_file(sourcepath).to_crs(crs) if sourcepath else gpd.GeoDataFrame(geometry=[],crs=crs)
    source=source[source.geometry.notna() & ~source.geometry.is_empty].reset_index(drop=True)
    if args.source_zip and (out/'_source_temp').exists():
        shutil.rmtree(out/'_source_temp')
    records=[];evidence=[]
    pending_missing_source=[]
    counters=collections.Counter()
    def issue(code,severity,circuit,row,geom,description,source_id='',output_id='',metric=None,why=''):
        if geom is None or geom.is_empty:geom=Point(0,0)
        if geom.geom_type!='Point':geom=geom.representative_point()
        x=dict(ISSUE_ID=f'QA-{len(records)+1:05d}',CHECK=code,SEVERITY=severity,
               CIRCUIT=val(circuit),ROW=val(row),SOURCE_ID=val(source_id),OUTPUT_ID=val(output_id),
               METRIC_M=round(float(metric),3) if metric is not None and np.isfinite(metric) else None,
               DESCRIPTION=description,EVIDENCE=why,geometry=geom)
        records.append(x)

    c_by=collections.defaultdict(list)
    for _,r in cayd.iterrows():c_by[val(r.get('WIRE_ID'))].append(r)
    deliver_by=collections.defaultdict(list)
    for _,r in d['267_w'].iterrows():deliver_by[val(r.get('WIRE_ID'))].append(r)
    selected=set()
    if not gpkg_data['export_settings'].empty:
        sett={val(r.KEY):val(r.VALUE) for r in gpkg_data['export_settings'].itertuples()}
        selected=set(split(sett.get('SELECTED_ROWS')))
    if not selected:selected=set(d['267_w'].get('__file_row',pd.Series(dtype=str)).astype(str).unique())
    wire_audit=[]
    for src in wdf:
        wid=src['wire_id']; in_gp=c_by.get(wid,[]); delivered=deliver_by.get(wid,[])
        src_rows=set(split(r.get('ROW'))[0] for r in in_gp if split(r.get('ROW')))
        shp_rows={val(r.get('__file_row')) for r in delivered}
        deltas=[geometry_delta(src['geometry'],r.geometry,unit) for r in in_gp]
        best=min(deltas) if deltas else float('nan')
        status='MATCHED'
        if not in_gp:
            issue('W01_MISSING_FROM_GPKG','CRITICAL',src['circuit'],'',src['geometry'],'Source DXF conductor missing in Geometry GPKG',wid,why=src['layer']);status='MISSING_GPKG'
        else:
            if best>args.wire_tolerance:
                issue('W02_WIRE_GEOMETRY','HIGH',src['circuit'],'',src['geometry'],f'3D geometry differs from DXF by {best:.2f} m',wid,metric=best);status='GEOMETRY_DIFFERENCE'
            if src['circuit'] and any(src['circuit'] not in split(r.get('CIRCUIT')) for r in in_gp):
                issue('W03_CIRCUIT_MISMATCH','HIGH',src['circuit'],'',src['geometry'],'DXF level circuit differs from GPKG Caydence circuit',wid,why=src['layer'])
            if src['voltage'] and any(val(r.get('VOLTAGE')).replace('.0','')!=src['voltage'] for r in in_gp):
                issue('W04_VOLTAGE_MISMATCH','HIGH',src['circuit'],'',src['geometry'],'DXF level voltage differs from Caydence voltage',wid)
        expected={row for r in in_gp for row in split(r.get('ROW')) if row in selected}
        if not in_gp:delivery_state='NO_GPKG'
        elif not expected:
            delivery_state='OUTSIDE_SELECTION'
            issue('W05_OUTSIDE_SELECTED_ROWS','REVIEW',src['circuit'],';'.join(sorted(src_rows)),src['geometry'],
                  'Wire in GPKG but ROW outside selected RoWs; verify against SoW/geometry',wid)
        elif not expected.issubset(shp_rows):
            delivery_state='MISSING_267'
            issue('W06_MISSING_FROM_267','CRITICAL',src['circuit'],';'.join(sorted(expected-shp_rows)),src['geometry'],
                  'Wire exists in GPKG, missing from corresponding final product 267',wid)
        else:delivery_state='DELIVERED'
        if in_gp and delivered:
            if any(geometry_delta(src['geometry'],r.geometry,unit)>args.wire_tolerance for r in delivered):
                issue('W07_267_GEOMETRY','HIGH',src['circuit'],';'.join(sorted(shp_rows)),src['geometry'],
                      'Final 267 geometry differs from original source DXF',wid)
        wire_audit.append(dict(WIRE_ID=wid,SRC_LAYER=src['layer'],SRC_CIRCUIT=src['circuit'],GPKG_STATUS=status,
                               GPKG_ROWS=';'.join(sorted(src_rows)),OUTPUT_ROWS=';'.join(sorted(shp_rows)),
                               DELIVERY_STATUS=delivery_state,MAX_GEOM_DIFF_M=round(best,4) if np.isfinite(best) else None))
    source_ids={x['wire_id'] for x in wdf}
    for wid,rr in c_by.items():
        if wid not in source_ids:
            issue('W08_EXTRA_GPKG_WIRE','HIGH',rr[0].get('CIRCUIT'),rr[0].get('ROW'),rr[0].geometry,
                  'Caydence WIRE_ID has no corresponding DXF source wire',output_id=wid)
    for wid,rr in deliver_by.items():
        if wid not in source_ids:
            issue('W09_EXTRA_DELIVERED_WIRE','HIGH',rr[0].get('CIRCUIT'),rr[0].get('__file_row'),rr[0].geometry,
                  'Product 267 WIRE_ID absent from source DXF',output_id=wid)
    # Detect same source wire repeated within a single delivered RoW (intentional across RoWs is permitted).
    for (wid,row),n in collections.Counter((wid,val(r.get('__file_row'))) for wid,rs in deliver_by.items() for r in rs).items():
        if n>1:
            issue('W10_DUPLICATE_267','HIGH','',row,deliver_by[wid][0].geometry,
                  f'Same WIRE_ID occurs {n} times within one 267 RoW',output_id=wid)

    # Validate final delivered structures against source GLOBALID (not null optional source fields).
    src_id=collections.defaultdict(list)
    for ix,r in source.iterrows():
        if norm(r.get('GLOBALID')):src_id[norm(r.get('GLOBALID'))].append(r)
    s45=d['45'];structure_audit=[]
    for ix,r in s45.iterrows():
        key=norm(r.get('GLOBALID'));match=src_id.get(key,[]) if key else []
        missing=[fld for fld in ('GLOBALID','STRUCT_ID','STR_TYPE') if not val(r.get(fld))]
        if missing:
            # Don't borrow fields from a nearby structure on a different circuit.
            same_circuit=source[source.get('LINE_NO',pd.Series(index=source.index,dtype=str)).astype(str)==val(r.get('CIRCUIT'))]
            if not same_circuit.empty:
                nearby=same_circuit[same_circuit.geometry.distance(r.geometry)*unit<=args.source_search_m]
            else:nearby=same_circuit
            if len(nearby)==0:
                counters['EXPECTED_UNMATCHED_NO_COMPATIBLE_SOURCE']+=1
            else:
                issue('S01_POSSIBLE_MISSING_ATTRIBUTION','REVIEW',r.get('CIRCUIT'),r.get('ROW'),r.geometry,
                      'Source attributes missing and same-circuit source candidate nearby: '+', '.join(missing),output_id=f'45:{ix}')
        if key and not match:
            issue('S02_SOURCE_GLOBALID_NOT_FOUND','HIGH',r.get('CIRCUIT'),r.get('ROW'),r.geometry,
                  'Delivered GLOBALID not found in source structures',output_id=f'45:{ix}',why=val(r.get('GLOBALID')))
        conflicts=[]
        if match:
            ref=match[0]
            for col in ('MATERIAL','STR_TYPE','YEAR_CONST'):
                if val(ref.get(col)) and val(r.get(col)) and norm(ref.get(col))!=norm(r.get(col)):
                    conflicts.append(col)
            if val(ref.get('STRUCTURE_')) and val(r.get('STRUCT_ID')) and norm(ref.get('STRUCTURE_'))!=norm(r.get('STRUCT_ID')):
                conflicts.append('STRUCT_ID')
            if conflicts:
                issue('S03_ATTRIBUTE_CONFLICT','HIGH',r.get('CIRCUIT'),r.get('ROW'),r.geometry,
                      'Source fields differ: '+', '.join(conflicts),source_id=ref.get('GLOBALID'),output_id=f'45:{ix}')
        structure_audit.append(dict(CIRCUIT=val(r.get('CIRCUIT')),ROW=val(r.get('ROW')),STRUCT_ID=val(r.get('STRUCT_ID')),
                       GLOBALID=val(r.get('GLOBALID')),SOURCE_MATCH='MATCHED' if match else 'UNMATCHED',
                       MISSING_FIELDS=';'.join(missing),CONFLICTS=';'.join(conflicts)))
    # Independent checks for unreliable ordered structure-source attribution.
    # POA circuit evidence is compared to the physical TOPO_ID, not to a
    # geometrically nearest conductor of a different circuit.
    poa_layer=gpkg_data['source_structure_points']
    if not poa_layer.empty and 'TOPO_ID' in poa_layer.columns:
        by_topo=collections.defaultdict(list)
        for _,marker in poa_layer.iterrows():
            topo=val(marker.get('TOPO_ID'))
            if topo and marker.geometry is not None and not marker.geometry.is_empty:
                by_topo[topo].append(marker)
        for _,support in st.iterrows():
            topo=val(support.get('TOPO_ID'))
            circuit=val(support.get('CIRCUIT'))
            match_method=val(support.get('SRC_MATCH'))
            if not topo or not circuit or not match_method.startswith('CORRIDOR'):
                continue
            for marker in by_topo.get(topo,[]):
                if support.geometry.distance(marker.geometry)*unit > 3.0:
                    continue
                sow=val(marker.get('SOW_CIRCUIT'))
                wire=val(marker.get('WIRE_CIRCUIT'))
                try:votes=int(marker.get('WIRE_VOTES') or 0)
                except (TypeError,ValueError):votes=0
                if sow and wire and norm(sow)==norm(wire) and norm(sow)!=norm(circuit) and votes>=2:
                    issue('S06_FOREIGN_CIRCUIT_SUPPORT','HIGH',circuit,support.get('ROW'),
                          support.geometry,'Assigned client structure is on a POA with corroborated foreign-circuit attribution',
                          source_id=val(support.get('SRC_STRUCT')),
                          output_id=topo,why=f'SOW={sow}, WIRE={wire}, VOTES={votes}, SRC_MATCH={match_method}')
                    break
            try:source_distance=float(support.get('SRC_DIST_M'))
            except (TypeError,ValueError):continue
            if np.isfinite(source_distance) and source_distance>15.0:
                issue('S07_DISTANT_SOURCE_MATCH','REVIEW',circuit,support.get('ROW'),
                      support.geometry,'Ordered source structure assignment is more than 15m from its physical support',
                      source_id=val(support.get('SRC_STRUCT')),output_id=topo,
                      metric=source_distance,why='Verify independent source anchors and circuit topology')
    # Two different TOPO_IDs with the same source structure identity on the
    # same circuit are suspicious unless one is an intentional RoW duplicate.
    source_to_topos=collections.defaultdict(set)
    for _,support in st.iterrows():
        source_id=val(support.get('SRC_GID')) or val(support.get('SRC_STRUCT'))
        if source_id and val(support.get('CIRCUIT')) and val(support.get('TOPO_ID')):
            source_to_topos[(val(support.get('CIRCUIT')),source_id)].add(val(support.get('TOPO_ID')))
    for (c,source_id),topos in source_to_topos.items():
        if len(topos)>1:
            subset=st[(st['CIRCUIT'].astype(str)==c) &
                      ((st.get('SRC_GID',pd.Series(index=st.index,dtype=str)).astype(str)==source_id) |
                       (st.get('SRC_STRUCT',pd.Series(index=st.index,dtype=str)).astype(str)==source_id))]
            if not subset.empty:
                issue('S08_SOURCE_ID_MULTIPLE_SUPPORTS','REVIEW',c,'',subset.geometry.iloc[0],
                      'One source structure ID is assigned to several distinct topology supports',
                      source_id=source_id,why='TOPO_IDs: '+', '.join(sorted(topos)))

    # Source-side omission review: only sources near original POA & within output circuit scope.
    if len(source) and len(poa) and len(s45):
        source_coords=np.array([[p.x,p.y] for p in source.geometry]);pcoords=np.array([[p.x,p.y] for _,p in poa]);
        s_coords=np.array([[p.x,p.y] for p in s45.geometry])
        p_tree=cKDTree(pcoords);out_tree=cKDTree(s_coords)
        del_c={val(x) for x in s45['CIRCUIT']}
        source_scope=[]
        delivered_gids={norm(v) for v in s45.get('GLOBALID',[])}
        for ix,r in source.iterrows():
            c=val(r.get('LINE_NO'));dist_p=float(p_tree.query(source_coords[ix])[0])*unit
            if c not in del_c or dist_p>args.source_search_m:continue
            matched=norm(r.get('GLOBALID')) in delivered_gids
            source_scope.append(dict(LINE_NO=c,STRUCTURE_ID=val(r.get('STRUCTURE_')),GLOBALID=val(r.get('GLOBALID')),
                                     MATCH='MATCHED' if matched else 'UNMATCHED',POA_DISTANCE_M=round(dist_p,2)))
            if not matched:
                pending_missing_source.append((r,c,dist_p,int(p_tree.query(source_coords[ix])[1])))
        # Many-to-one source evidence, screen near-source pairs that are distinct and each near separate POAs.
        src_tree=cKDTree(source_coords)
        scoped={int(ix) for ix,r in source.iterrows() if val(r.get('LINE_NO')) in del_c and float(p_tree.query(source_coords[ix])[0])*unit<=args.source_search_m}
        for i in sorted(scoped):
            ri=source.iloc[i]
            for j in src_tree.query_ball_point(source_coords[i],r=args.merge_distance_m/unit):
                if j<=i or j not in scoped:continue
                rj=source.iloc[j]
                if norm(ri.get('GLOBALID'))==norm(rj.get('GLOBALID')):continue
                dist=float(np.linalg.norm(source_coords[i]-source_coords[j]))*unit
                if dist<1.5:continue
                # Both must have distinct nearest POAs and only one nearby delivered output location.
                pi=int(p_tree.query(source_coords[i])[1]);pj=int(p_tree.query(source_coords[j])[1])
                if pi==pj:continue
                oi=set(out_tree.query_ball_point(source_coords[i],r=args.merge_distance_m/unit))
                oj=set(out_tree.query_ball_point(source_coords[j],r=args.merge_distance_m/unit))
                common=oi&oj
                if common and (norm(ri.get('GLOBALID')) not in delivered_gids or norm(rj.get('GLOBALID')) not in delivered_gids):
                    issue('S05_POSSIBLE_SUPPORT_MERGE','REVIEW',val(ri.get('LINE_NO'))+';'+val(rj.get('LINE_NO')),'',
                      Point((source_coords[i]+source_coords[j])/2),
                      'Two distinct source supports with different nearby POAs share a delivered support candidate',
                      str(ri.get('GLOBALID'))+';'+str(rj.get('GLOBALID')),metric=dist)
    else:source_scope=[]
    # Correlate source->delivery and POA->delivery at one physical location.
    paired_poa=set()
    for r,c,dist_p,pi in pending_missing_source:
        pid,ppt=poa[pi]
        near_same=[x for x in pending_missing_source if x[1]==c and x[3]==pi]
        src_unique=(len(near_same)==1)
        ref_gid=val(r.get('GLOBALID'))
        nearby_same=s45[(s45.CIRCUIT.astype(str)==c)&
                        (s45.geometry.distance(ppt)*unit<=args.poa_distance_m)]
        # Strong evidence means source+POA are near, both genuinely absent from
        # delivered 45, and the source/POA association is unambiguous.
        if (src_unique and dist_p<=args.source_search_m and nearby_same.empty
                and ref_gid and pid not in paired_poa):
            issue('S07_MISSING_STRUCTURE_WITH_POA','CRITICAL',c,'',ppt,
                  'Source-matched POA structure absent from product 45: check Bakery RoW boundary and export',
                  source_id=ref_gid,output_id=pid,metric=dist_p,
                  why=f'STRUCTURE_={val(r.get("STRUCTURE_"))}; LINE_NO={c}; POA={pid}')
            paired_poa.add(pid)
        else:
            issue('S04_SOURCE_NOT_DELIVERED','REVIEW',c,'',r.geometry,
                  'Source structure near POA absent by GLOBALID from product 45; verify scope/POA',
                  ref_gid,metric=dist_p)
    # Check POA standalone proximity to 45; do not duplicate a confirmed S07.
    if len(poa) and len(s45):
        tree=cKDTree(np.array([[p.x,p.y] for p in s45.geometry]))
        for pid,p in poa:
            if pid in paired_poa:continue
            dist=float(tree.query([p.x,p.y])[0])*unit
            if dist>args.poa_distance_m:
                issue('S06_POA_FAR_FROM_STRUCTURE','REVIEW','','',p,
                    'Tower Top POA has no nearby product-45 structure; may be legitimate topology/scope',pid,metric=dist)
    # 80/2D artifact detection: local lateral zigzag + short dangling geometries; no prior-run comparison.
    art_summary=collections.Counter()
    for _,r in wire2.iterrows():
        c=val(r.get('CIRCUIT'));g=r.geometry
        parts=list(g.geoms) if hasattr(g,'geoms') else [g]
        for part in parts:
            if not isinstance(part,LineString):continue
            pts=np.array(part.coords)[:,:2]
            for k in range(1,len(pts)-1):
                a,b,nxt=pts[k-1],pts[k],pts[k+1]
                v1=b-a;v2=nxt-b;n1=np.linalg.norm(v1);n2=np.linalg.norm(v2)
                if min(n1,n2)*unit<args.artifact_min_seg_m:continue
                cos=float(np.dot(v1,v2)/(n1*n2));turn=math.degrees(math.acos(np.clip(cos,-1,1)))
                if turn>=args.kink_deg and min(n1,n2)*unit<args.kink_max_segment_m:
                    issue('G01_POSSIBLE_PHASE_JUMP','REVIEW',c,val(r.get('ROW')),Point(b),
                      f'2D directional kink {turn:.0f}° over short segments; check against original DXF phases',metric=min(n1,n2)*unit)
                    art_summary['kinks']+=1
    # Short dangling spur ('whisker') candidate: one end joins a second 2D part,
    # the other ends freely, and there is no original POA supporting that tip.
    # A normal stand-alone 2D part with valid terminal POAs is not considered a whisker.
    if not wire2.empty and poa:
        poa_tree=cKDTree(np.array([[p.x,p.y] for _,p in poa]))
        for c,g in wire2.groupby('CIRCUIT'):
            parts=[]
            for geom in g.geometry:
                parts += list(geom.geoms) if hasattr(geom,'geoms') else [geom]
            parts=[p for p in parts if isinstance(p,LineString) and not p.is_empty]
            if len(parts)<2:continue
            for ix,part in enumerate(parts):
                length_m=part.length*unit
                if length_m>args.whisker_max_m or length_m<1.0:continue
                others=unary_union([p for j,p in enumerate(parts) if j!=ix])
                ends=[Point(part.coords[0]),Point(part.coords[-1])]
                endpoint_distances=[p.distance(others)*unit for p in ends]
                tip=ends[0] if endpoint_distances[0]>endpoint_distances[1] else ends[1]
                near=min(endpoint_distances);far=max(endpoint_distances)
                if near<=args.whisker_join_m and far>=args.whisker_tip_m:
                    poa_dist=float(poa_tree.query([tip.x,tip.y])[0])*unit
                    if poa_dist>args.whisker_poa_m:
                        issue('G03_POSSIBLE_WHISKER','REVIEW',c,val(g.iloc[0].get('ROW')),tip,
                          'Short 2D part attached at one end, with unsupported free tip; inspect original 3D',
                          metric=length_m,why=f'Tip-to-POA {poa_dist:.1f}m')
    # 3D -> 2D corridor only screens strong deviations; not an assertion of missing 2D.
    by2={val(c):unary_union(list(g.geometry)) for c,g in wire2.groupby('CIRCUIT')} if not wire2.empty else {}
    corridor_audit=[]
    for src in wdf:
        if not src['circuit'] or src['circuit'] not in by2:continue
        geom=src['geometry'];target=by2[src['circuit']]
        sample_n=max(3,min(120,math.ceil(geom.length*unit/args.sample_step_m)+1))
        ps=[geom.interpolate(v,normalized=True) for v in np.linspace(0,1,sample_n)]
        ds=np.array([p.distance(target)*unit for p in ps]);pct=float((ds>args.coverage_corridor_m).mean()*100)
        corridor_audit.append(dict(WIRE_ID=src['wire_id'],CIRCUIT=src['circuit'],MAX_DISTANCE_M=round(float(ds.max()),2),PCT_OUTSIDE=round(pct,1)))
        if pct>=args.coverage_pct and float(ds.max())>args.coverage_corridor_m:
            ix=int(np.argmax(ds));issue('G02_3D_OUTSIDE_2D_CORRIDOR','REVIEW',src['circuit'],'',ps[ix],
              f'{pct:.0f}% of source 3D conductor >{args.coverage_corridor_m}m from representative 2D; check tap/scope',
              src['wire_id'],metric=float(ds[ix].max()))

    # EARTH-only branch audit: inspect secondary 2D parts after independent
    # LiDAR conductor typing. Do not report a branch when evidence is uncertain
    # or a PHASE conductor runs along its interior.
    if not wire2.empty and not cayd.empty and 'WIRE_TYPE' in cayd.columns:
        for _,row2 in wire2.iterrows():
            geom=row2.geometry
            parts=[p for p in getattr(geom,'geoms',[geom])
                   if isinstance(p,LineString) and not p.is_empty]
            if len(parts)<2:continue
            main=max(parts,key=lambda p:p.length)
            c=val(row2.get('CIRCUIT'))
            candidates=cayd[cayd['CIRCUIT'].astype(str)==c]
            earth=[r for _,r in candidates.iterrows() if val(r.get('WIRE_TYPE')).upper()=='EARTH']
            non_earth=[r for _,r in candidates.iterrows() if val(r.get('WIRE_TYPE')).upper()!='EARTH']
            for part in parts:
                if part.equals(main) or part.length*unit<15:continue
                if min(main.distance(Point(part.coords[0])),main.distance(Point(part.coords[-1])))*unit>8:continue
                samples=[part.interpolate(f,normalized=True) for f in np.linspace(.15,.85,13)]
                ec=sum(any(shapely.force_2d(w.geometry).distance(p)*unit<=4 for w in earth) for p in samples)/len(samples)
                pc=sum(any(shapely.force_2d(w.geometry).distance(p)*unit<=6 for w in non_earth) for p in samples)/len(samples)
                if ec>=.85 and pc<=.05:
                    issue('E01_EARTH_ONLY_2D_BRANCH','HIGH',c,row2.get('ROW'),
                          part,'Secondary 2D branch has confirmed EARTH but no nearby PHASE evidence',
                          metric=part.length*unit,
                          why=f'EARTH sample coverage={ec:.0%}, non-EARTH={pc:.0%}; check original 3D')
    # 80 product completeness against 2D span inventory in the *same run*.
    # Explicit source 2D is the selected_span_wires layer (never earlier GPKGs).
    delivered80=d['80']
    spans=gpkg_data['selected_span_wires']
    if delivered80.empty:
        issue('D80_MISSING_DELIVERABLE','CRITICAL','','',wire2.geometry.iloc[0] if not wire2.empty else Point(0,0),
              'Product 80 contains no readable Wires SHP')
    else:
        for c,g in wire2.groupby(wire2.CIRCUIT.astype(str)):
            if c not in set(delivered80.CIRCUIT.astype(str)):
                issue('D80_MISSING_CIRCUIT','CRITICAL',c,'',g.geometry.iloc[0],
                      'Circuit present in GPKG wires_2d but absent from actual product 80')
    # End-of-line rule: physical supports or SQLite substation polygon (incl boundary).
    # Raw 3D wire-end clustering is counted as diagnostic only, not a confirmed
    # missing Structure: parallel source phases and DXF cuts are not topology.
    # 3D terminal groups are only tentative where raw span connectivity is ambiguous.
    term=build_terminal_audit(wire2,cayd,d['45'],substations,unit,args.endpoint_structure_m,
                              spans=spans,structure_nodes=st,topology_m=args.terminal_topology_m)
    for rec in term:
        counters[f"{rec['DATASET']}_{rec['STATUS']}"]+=1
        if rec['DATASET']=='2D' and rec['STATUS']=='STRUCTURE_LINKED_OFFSET':
            issue('T02_STRUCTURE_2D_OFFSET','REVIEW',rec['CIRCUIT'],rec['ROW'],rec['geometry'],
                  'Physical support verified through span topology and product 45, but representative 2D endpoint is laterally offset; check conductor arrangement (possible trident), not missing Structure',
                  source_id=rec['LINKED_TOPO_ID'],metric=rec['LINKED_STRUCTURE_M'],
                  why=f"Linked TOPO_ID={rec['LINKED_TOPO_ID']}; product-45 support verified at {rec['LINKED_PRODUCT45_M']} m")
        elif rec['STATUS']=='UNRESOLVED' and rec['DATASET']=='2D':
            issue('T01_TERMINAL_WITHOUT_STRUCTURE','REVIEW',rec['CIRCUIT'],rec['ROW'],rec['geometry'],
                  '2D end has no confirmed linked physical support or SQLite substation; inspect source POA and circuit topology',
                  metric=rec['NEAREST_STRUCTURE_M'],why=f"{rec['N_ENDPOINTS']} endpoint(s); matching same-circuit span and delivered support not established")
    pd.DataFrame([{k:v for k,v in r.items() if k!='geometry'} for r in term]).to_csv(out/'06_terminal_endpoint_audit.csv',index=False)
    pd.DataFrame([{'METRIC':k,'COUNT':v} for k,v in sorted(counters.items())]).to_csv(out/'07_positive_terminal_stats.csv',index=False)
    if substations is not None:
        print(f"[SOURCE] SQLite substation polygons: {len(substations)}",flush=True)
    # Positive matching and product statistics are reported separately from issues.
    structure_total=len(d['45'])
    structure_matches=sum(row['SOURCE_MATCH']=='MATCHED' for row in structure_audit)
    attr_conflicts=sum(bool(row['CONFLICTS']) for row in structure_audit)
    expected_rows=[row for row in wire_audit if row['DELIVERY_STATUS']=='DELIVERED']
    positive={
      'Source DXF wires':len(wdf),
      '3D source wires present in GPKG':sum(row['GPKG_STATUS']!='MISSING_GPKG' for row in wire_audit),
      '3D source wires in product 267':len(expected_rows),
      'Delivered product 45 structures':structure_total,
      'Product 45 GLOBALID matched to source':structure_matches,
      'Product 45 source attribute conflicts':attr_conflicts,
      'Delivered product 80 circuit wires':len(delivered80),
      'Delivered product 267 structures':len(d['267_s']),
      'Delivered product 267 wires':len(d['267_w']),
      'Original 2D circuit geometries':len(wire2),
      'Valid 2D terminal supports':counters['2D_STRUCTURE'],
      'Topologically linked 2D terminals with offset (review)':counters['2D_STRUCTURE_LINKED_OFFSET'],
      'Valid 2D substation terminals':counters['2D_SUBSTATION_TERMINAL'],
      '2D terminals requiring review':counters['2D_UNRESOLVED']+counters['2D_STRUCTURE_LINKED_OFFSET'],
      'Tentative 3D endpoints requiring topology assessment':counters['3D_UNRESOLVED'],
    }
    pd.DataFrame([{'MEASURE':k,'COUNT':v} for k,v in positive.items()]).to_csv(out/'08_delivery_and_positive_matching.csv',index=False)
    # Save all statistics and QA findings.
    for name,rows in [('01_wire_inventory.csv',wire_audit),('02_structure_source_audit.csv',structure_audit),
                      ('03_reverse_source_audit.csv',source_scope),('04_3d_to_2d_corridor.csv',corridor_audit)]:
        pd.DataFrame(rows).to_csv(out/name,index=False)
    fields=['ISSUE_ID','CHECK','SEVERITY','CIRCUIT','ROW','SOURCE_ID','OUTPUT_ID','METRIC_M','DESCRIPTION','EVIDENCE']
    pd.DataFrame([{k:r.get(k) for k in fields} for r in records],columns=fields).to_csv(out/'QA_Issues.csv',index=False)
    if records:
        issues_gdf=gpd.GeoDataFrame(records,geometry='geometry',crs=crs)
        issues_gdf.to_file(out/'QA_Issues.gpkg',layer='qa_issues',driver='GPKG')
    else:
        (out/'QA_Issues_Empty.txt').write_text('No findings.\n')
    stats={
        'source_dxf_wires':len(wdf),'source_poa':len(poa),'source_structure_records':len(source),
        'gpkg_wires_3d':len(cayd),'gpkg_structures':len(st),'gpkg_2d_circuits':len(wire2),
        'gpkg_spans':len(gpkg_data['selected_span_wires']),
        'delivered_267_wires':len(d['267_w']),'delivered_267_structures':len(d['267_s']),
        'delivered_45_structures':len(s45),'delivered_80_features':len(d['80']),
        'source_wires_in_gpkg':sum(v['GPKG_STATUS']!='MISSING_GPKG' for v in wire_audit),
        'source_wires_delivered':sum(v['DELIVERY_STATUS']=='DELIVERED' for v in wire_audit),
        'source_wires_outside_selection':sum(v['DELIVERY_STATUS']=='OUTSIDE_SELECTION' for v in wire_audit),
        'structures_source_matched':sum(r['SOURCE_MATCH']=='MATCHED' for r in structure_audit),
        'structures_source_unmatched':sum(r['SOURCE_MATCH']!='MATCHED' for r in structure_audit),
        'issues_by_check':dict(collections.Counter(r['CHECK'] for r in records)),
        'issues_by_severity':dict(collections.Counter(r['SEVERITY'] for r in records)),
        'selected_rows':sorted(selected),'epsg':crs.to_epsg(),
        'run_timestamp_utc':datetime.now(timezone.utc).isoformat(),
        'input_files':dict(gpkg=str(gpkg),wires_dxf=str(args.wires_dxf),poa_dxf=str(args.poa_dxf),deliverables=str(args.deliverables))}
    (out/'QA_Statistics.json').write_text(json.dumps(stats,indent=2,ensure_ascii=False),encoding='utf8')
    # Circuit overview and per-circuit overview, in GPKG coordinates with source RoW context.
    maps=out/'maps';maps.mkdir(exist_ok=True)
    def draw_map(filename,subset,context=True,full=False,title=''):
        fig,ax=plt.subplots(figsize=(12,7.5))
        if context and not gpkg_data['kmz_reference'].empty:
            g=gpkg_data['kmz_reference']
            if not full and subset and 'CIRCUIT' in g.columns:g=g[g['CIRCUIT'].astype(str).isin(subset)]
            if not g.empty:g.plot(ax=ax,color='#d0d0d0',linewidth=.45,alpha=.6)
        for c,group in wire2.groupby('CIRCUIT') if not wire2.empty else []:
            if subset and val(c) not in subset:continue
            group.plot(ax=ax,linewidth=1.2,label=str(c))
        if full and records:
            gg=gpd.GeoDataFrame(records,geometry='geometry',crs=crs)
            gg.plot(ax=ax,markersize=17,color='red',alpha=.8)
        elif subset and records:
            rr=[r for r in records if r['CIRCUIT'] in subset]
            if rr:gpd.GeoDataFrame(rr,geometry='geometry',crs=crs).plot(ax=ax,markersize=25,color='red')
        # Source SoW contains the entire client network. Always zoom the map
        # to delivered 2D geometry, not the union of the full source network.
        focus=wire2
        if subset and not wire2.empty:
            focus=wire2[wire2['CIRCUIT'].astype(str).isin(subset)]
        if not focus.empty:
            x0,y0,x1,y1=focus.total_bounds
            padding=max((x1-x0)*.10,(y1-y0)*.10,100.0)
            ax.set_xlim(x0-padding,x1+padding);ax.set_ylim(y0-padding,y1+padding)
        ax.set_title(title or 'Circuit QA overview');ax.set_aspect('equal',adjustable='box')
        ax.grid(alpha=.12);ax.tick_params(labelsize=7);fig.tight_layout()
        fig.savefig(maps/filename,dpi=125);plt.close(fig)
    draw_map('all_circuits.png',None,full=True,title='Cluster overview: 2D circuits and QA findings')
    circuits=sorted({val(x) for x in wire2.get('CIRCUIT',pd.Series(dtype=str)) if val(x)})
    for c in circuits:
        draw_map('circuit_'+re.sub('[^A-Za-z0-9_-]','_',c)+'.png',{c},title=f'Circuit {c}: source SoW, output 2D and QA findings')
    if records:
        gg=gpd.GeoDataFrame(records,geometry='geometry',crs=crs)
        fig,ax=plt.subplots(figsize=(12,8))
        if not wire2.empty:wire2.plot(ax=ax,color='#a6b2bb',linewidth=.6)
        for sev,color in [('CRITICAL','#b7212a'),('HIGH','#e38b23'),('REVIEW','#6c54b5')]:
            sub=gg[gg.SEVERITY==sev]
            if not sub.empty:sub.plot(ax=ax,color=color,markersize=24,label=f'{sev} ({len(sub)})',alpha=.85)
        ax.legend(loc='best');ax.set_title('QA findings: location and severity');ax.grid(alpha=.12)
        if not wire2.empty:
            x0,y0,x1,y1=wire2.total_bounds; pad=max((x1-x0)*.08,(y1-y0)*.08,100.0)
            ax.set_xlim(x0-pad,x1+pad);ax.set_ylim(y0-pad,y1+pad)
        fig.tight_layout();fig.savefig(maps/'qa_issues.png',dpi=140);plt.close(fig)
    # Operator-friendly local maps for each issue (source DXF and result 2D).
    # Keep source wire context limited to a tight bounding box around the issue.
    for finding in records:
        pt=finding['geometry'];radius=args.issue_map_radius_m/unit
        bbox=(pt.x-radius,pt.y-radius,pt.x+radius,pt.y+radius)
        fig,ax=plt.subplots(figsize=(7,6))
        for item in wdf:
            if item['geometry'].bounds[2]<bbox[0] or item['geometry'].bounds[0]>bbox[2] or item['geometry'].bounds[3]<bbox[1] or item['geometry'].bounds[1]>bbox[3]:continue
            xx,yy=item['geometry'].xy
            ax.plot(xx,yy,color='#b1bdc5',lw=0.7,alpha=.7)
        for _,row in wire2.iterrows():
            geom=row.geometry
            if geom.bounds[2]<bbox[0] or geom.bounds[0]>bbox[2] or geom.bounds[3]<bbox[1] or geom.bounds[1]>bbox[3]:continue
            geoms=list(geom.geoms) if hasattr(geom,'geoms') else [geom]
            for ls in geoms:
                xx,yy=ls.xy;ax.plot(xx,yy,color='#2470a5',lw=1.3,alpha=.95)
        ax.scatter([pt.x],[pt.y],s=80,c='#cd3742',marker='x',linewidths=2,zorder=15)
        ax.set_xlim(bbox[0],bbox[2]);ax.set_ylim(bbox[1],bbox[3]);ax.set_aspect('equal')
        ax.set_title(f"{finding['ISSUE_ID']} | {finding['CHECK']} | {finding['CIRCUIT']}",fontsize=10)
        ax.tick_params(labelsize=6);ax.grid(alpha=.1);fig.tight_layout()
        fig.savefig(maps/(finding['ISSUE_ID']+'.png'),dpi=115);plt.close(fig)
    # Write static offline HTML, table of checks, circuit statistics and maps.
    wa=pd.DataFrame(wire_audit);sa=pd.DataFrame(structure_audit);iss=pd.DataFrame(records)
    by_circuit=[]
    for c in sorted(set(wa.SRC_CIRCUIT)-{''} | set(sa.CIRCUIT)-{''}):
        by_circuit.append(dict(CIRCUIT=c,DXF_WIRES=int((wa.SRC_CIRCUIT==c).sum()),
         GPKG_MATCHED=int(((wa.SRC_CIRCUIT==c)&(wa.GPKG_STATUS!='MISSING_GPKG')).sum()),
         DELIVERY_MATCHED=int(((wa.SRC_CIRCUIT==c)&(wa.DELIVERY_STATUS=='DELIVERED')).sum()),
         STRUCTURES=int((sa.CIRCUIT==c).sum()),
         SOURCE_MATCHED=int(((sa.CIRCUIT==c)&(sa.SOURCE_MATCH=='MATCHED')).sum()),
         ISSUES=int((iss.CIRCUIT==c).sum()) if not iss.empty else 0))
    pd.DataFrame(by_circuit).to_csv(out/'05_circuit_scorecard.csv',index=False)
    def esc(x):return html.escape(val(x))
    def table(rows,cols,limit=1000):
        h='<div class="tablewrap"><table><thead><tr>'+''.join('<th>'+esc(c)+'</th>' for c in cols)+'</tr></thead><tbody>'
        for row in rows[:limit]:h+='<tr>'+''.join('<td>'+esc(row.get(c,''))+'</td>' for c in cols)+'</tr>'
        return h+'</tbody></table></div>'
    issues_rows=[{k:v for k,v in r.items() if k!='geometry'} for r in records]
    c_rows=by_circuit
    content=f'''<!doctype html><html><head><meta charset="utf-8"><title>Eversource QA Inspector</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:0;color:#233242;background:#f4f6fa}}.wrap{{max-width:1400px;margin:auto;padding:24px}}
header{{background:#122a43;color:white;padding:24px 28px;border-radius:10px}}h1{{margin:0 0 6px;font-size:28px}}h2{{margin-top:35px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:18px 0}}.card{{background:white;padding:17px;border:1px solid #dae0e7;border-radius:9px}}.num{{font-size:28px;font-weight:700}}small,.muted{{color:#68798b}}table{{border-collapse:collapse;width:100%;font-size:12px;background:white}}th,td{{border-bottom:1px solid #e6eaf0;text-align:left;padding:8px 9px}}th{{background:#e9eef5;position:sticky;top:0}}.tablewrap{{overflow:auto;max-height:550px;border:1px solid #dbe1e8;border-radius:6px}}
img{{width:100%;border:1px solid #dce2e8;background:white;border-radius:8px}}.mapgrid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(380px,1fr));gap:12px}}.mapgrid figure{{margin:0}}figcaption{{font-size:12px;color:#546678}}code{{font-size:12px}}</style></head><body><div class="wrap">
<header><h1>Eversource QA Inspector</h1><div>Independent source → Geometry GPKG → product 45/80/267 validation</div><div style="font-size:12px;opacity:.8;margin-top:10px">{esc(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))} · EPSG:{stats['epsg']} · RoWs {esc('; '.join(sorted(selected)))}</div></header>
<h2>Executive summary</h2><div class="cards">'''
    for title,n in [('Source DXF wires',len(wdf)),('Matched in GPKG',stats['source_wires_in_gpkg']),('Delivered wires',stats['source_wires_delivered']),('Source-matched structures',stats['structures_source_matched']),('Unmatched structures',stats['structures_source_unmatched']),('Critical findings',stats['issues_by_severity'].get('CRITICAL',0)),('High findings',stats['issues_by_severity'].get('HIGH',0)),('Review findings',stats['issues_by_severity'].get('REVIEW',0))]:
        content+=f'<div class="card"><div class="num">{n:,}</div><div class="muted">{esc(title)}</div></div>'
    content+='''</div><p><strong>Scope:</strong> one GPKG and matching original sources/deliverables. GPKG and product-267 totals can differ where one wire is legitimately in multiple RoWs. Detection is evidence-based; REVIEW is not a confirmed defect. No production files modified.</p>
<h2>Checks and results</h2>'''
    content+=table([{'CHECK':k,'COUNT':v} for k,v in sorted(stats['issues_by_check'].items())],['CHECK','COUNT'])
    content+='<h2>Terminal matching, positive and unresolved</h2>'+table([{'METRIC':k,'COUNT':v} for k,v in sorted(counters.items())],['METRIC','COUNT'])
    content+='<h2>Delivery completeness and positive matching</h2>'+table([{'MEASURE':k,'COUNT':v} for k,v in positive.items()],['MEASURE','COUNT'])
    content+='<h2>Circuit scorecard</h2>'+table(c_rows,['CIRCUIT','DXF_WIRES','GPKG_MATCHED','DELIVERY_MATCHED','STRUCTURES','SOURCE_MATCHED','ISSUES'])
    content+='<h2>Maps</h2><div class="mapgrid"><figure><img src="maps/all_circuits.png"><figcaption>All 2D circuits and QA locations</figcaption></figure>'
    if records:content+='<figure><img src="maps/qa_issues.png"><figcaption>Issues by severity</figcaption></figure>'
    content+='</div><h2>Individual circuits</h2><div class="mapgrid">'
    for c in circuits:
        safe=re.sub('[^A-Za-z0-9_-]','_',c)
        content+=f'<figure><img loading="lazy" src="maps/circuit_{safe}.png"><figcaption>Circuit {esc(c)}</figcaption></figure>'
    content+='</div><h2>Issues for operator review</h2>'
    content+=table(issues_rows,['ISSUE_ID','CHECK','SEVERITY','CIRCUIT','ROW','SOURCE_ID','OUTPUT_ID','METRIC_M','DESCRIPTION'],limit=3000)
    content+='<h2>Issue close-ups</h2><div class="mapgrid">'
    for item in records:
        content+=f'<figure><img loading="lazy" src="maps/{esc(item["ISSUE_ID"])}.png"><figcaption>{esc(item["ISSUE_ID"])} · {esc(item["CHECK"])} · {esc(item["DESCRIPTION"])}</figcaption></figure>'
    content+='</div>'
    content+='''<h2>QA method and limitations</h2><p>DXF wires are read in WCS including LWPOLYLINE OCS conversion. WIRE_ID is used as an identity hint; source 3D geometry is checked independently. Source structures are matched using GLOBALID. Missing optional attributes that are already NULL in source are not counted as conflicts. Source → output omissions are reported only for records near original POAs and belonging to delivered circuits. Physical shared supports can produce many-to-one relationships legitimately.</p><p>A 2D endpoint matched through selected_span_wires STRUCT_A/B and a real product-45 support is recorded as T02 (offset REVIEW), not T01 (missing). A lateral offset must not be automatically connected to a POA, since it may indicate a trident layout or wrong conductor lane. Substation endpoints from source SQLite polygons (including boundaries) are valid and require no POA or artificial Structure. Without --sow-sqlite the substation exception cannot be applied and endpoint review findings remain provisional.</p><p>The Geometry GPKG is not proof of delivery. The inspector separately checks actual product 267 SHPs. 2D corridor distances and kinks indicate inspection candidates, not confirmed misclassification. For full 2D PHASE/EARTH evidence, LiDAR or explicit source wire role data is required.</p><p>All distances are in metres; CRS axis-unit conversion is applied. Output files: <code>QA_Issues.gpkg</code>, <code>QA_Issues.csv</code>, full wire/structure audits, circuit scorecard and JSON metrics.</p></div></body></html>'''
    (out/'QA_Report.html').write_text(content,encoding='utf8')
    print('Source wires',len(wdf),'GPKG',len(cayd),'267',len(d['267_w']))
    print('Source-matched structures',stats['structures_source_matched'],'/',len(s45))
    print('Findings',len(records),stats['issues_by_check'])
    print('Output:',out)
    return stats


def main():
    p=argparse.ArgumentParser(description='Eversource QA Inspector, one run, source-to-deliverable validation')
    p.add_argument('--wires-dxf',type=Path,required=True)
    p.add_argument('--poa-dxf',type=Path,required=True)
    p.add_argument('--source-zip',type=Path)
    p.add_argument('--source-structures',type=Path)
    p.add_argument('--sow-sqlite',type=Path,help='Source SoW SQLite for substation polygon endpoint exception')
    p.add_argument('--endpoint-structure-m',type=float,default=3.0)
    p.add_argument('--terminal-topology-m',type=float,default=30.0,help='Maximum endpoint-to-associated span/node proximity in metres; never used alone to declare a physical match')
    p.add_argument('--gpkg',type=Path,required=True)
    p.add_argument('--deliverables',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--wire-tolerance',type=float,default=0.3)
    p.add_argument('--source-search-m',type=float,default=15.0)
    p.add_argument('--merge-distance-m',type=float,default=15.0)
    p.add_argument('--poa-distance-m',type=float,default=15.0)
    p.add_argument('--artifact-min-seg-m',type=float,default=0.5)
    p.add_argument('--kink-deg',type=float,default=32.0)
    p.add_argument('--kink-max-segment-m',type=float,default=15.0)
    p.add_argument('--issue-map-radius-m',type=float,default=120.0)
    p.add_argument('--whisker-max-m',type=float,default=30.0)
    p.add_argument('--whisker-join-m',type=float,default=1.0)
    p.add_argument('--whisker-tip-m',type=float,default=3.0)
    p.add_argument('--whisker-poa-m',type=float,default=10.0)
    p.add_argument('--coverage-corridor-m',type=float,default=20.0)
    p.add_argument('--coverage-pct',type=float,default=40.0)
    p.add_argument('--sample-step-m',type=float,default=20.0)
    a=p.parse_args()
    if not a.source_zip and not a.source_structures:p.error('Provide --source-zip or --source-structures')
    build(a)

if __name__=='__main__':main()