#!/usr/bin/env python3
"""
02_build_eversource_wires_v11.py
===============================

Simplified Eversource 2D wire builder.

Required inputs only:
  1. 3D wire vectors DXF
  2. Structure Top Attachment Point DXF
  3. LiDAR LAS/LAZ (file, ZIP, or folder) for ground elevation
  4. Client source ZIP/folder (lines + structures)
  5. Project SoW KMZ
  6. TVM Management Units FileGDB (auto-discovered from source/ or client ZIP)
  7. EPSG code for the two CAD inputs

Core rule:
  - Do not invent a new plan geometry from structure axes / intersections.
  - Each raw DXF wire is treated as a source conductor span.
  - Source conductors are flattened from 3D to 2D.
  - At each physical span, non-phase wires are discarded.
  - For a horizontal 3-phase arrangement, use the central phase.
  - For vertical/mixed arrangements, choose one phase consistently by continuity.
  - Circuit identity is assigned to physical wire tracks globally, not independently
    at every span. This prevents left/right switching and duplicate circuit paths.
  - One longest continuous sequence is retained per circuit. Branches/extras are
    written to QA and excluded from the final circuit wire.
  - Final circuit wires are simplified conservatively between structure endpoints;
    structure/join endpoints are preserved exactly.
  - Nearby contractor top points may share one INTERNAL topology group, but they
    are never averaged for normal delivered Structure geometry. Each circuit chooses
    the original top point nearest its own final wire. A three-pole support is a
    special case: the actual middle contractor pole is delivered at its physical XY
    and is NOT snapped to the 2D wire. LiDAR supplies only the final ground Z.
  - LiDAR tile filenames are validated, converted to delivery names, and assigned to
    FULL RoWs by tile-area majority. If two or more FULL RoWs each cover >=99% of a
    tile, a delivery copy is generated for every qualifying RoW.

Outputs:
  output/
    02_eversource_2d_wires.gpkg
      wires_2d                  one feature per final circuit
      structures_final          3D ground-level circuit/structure point features
      structure_source_match    nearest client structure attribution QA
      structure_ground_qa       LiDAR ground-elevation QA
      selected_span_wires       selected source conductor per retained span
      discarded_branches        circuit-attributed spans not on final sequence
      earth_wires_excluded      excluded earth/static conductors
      phase_wires_discarded     non-selected phase conductors
      support_points            internal topology support groups (not delivered structure XY)
      source_structure_points   original contractor top points + topology group ID
      span_bundles              physical span/circuit-bundle QA
      track_assignment_review   physical-track -> client-circuit QA
      join_review               support-to-support sequence join QA
      client_reference
      kmz_reference

    deliverables/<RoW>/
      LiDAR/
        <RoW>_<TileRef>_LAS_<Year>.las   (uncompressed LAS 1.4 by default)
      Tile_Index/
        <RoW>_Tile_Index_<Year>.shp
      Management_Units/
        <RoW>_Management_Unit_<Year>.shp
      Circuit/<Circuit>/
        <Circuit>_Wires_<Year>.shp
        <Circuit>_Structure_<Year>.shp
        Caydence/
          <Circuit>_3D_Wires_<Year>.shp
          <Circuit>_Tower_Tops_<Year>.shp

    delivery_inventory.csv

Dependencies:
  pip install ezdxf geopandas shapely pyproj pyogrio pandas numpy scipy laspy lazrs
"""

from __future__ import annotations

import argparse
import copy
import html
import hashlib
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import ezdxf
import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyproj import CRS, Transformer
from scipy.spatial import cKDTree, Delaunay, QhullError
from shapely.geometry import LineString, MultiLineString, Point, box
from shapely.ops import linemerge, transform, unary_union

try:
    import laspy
except Exception:  # optional until LiDAR elevation is requested
    laspy = None


# Keep the console focused on actionable pipeline messages.
# These warnings are expected when reading TerraScan/DGN-derived measured geometry
# or writing ESRI Shapefiles with DBF's 10-character field-name limit.
warnings.filterwarnings(
    "ignore",
    message=r".*Measured \(M\) geometry types are not supported.*",
)
warnings.filterwarnings(
    "ignore",
    message=r".*Column names longer than 10 characters will be truncated.*",
)
warnings.filterwarnings(
    "ignore",
    message=r".*Normalized/laundered field name.*",
)

KML_NS = "http://www.opengis.net/kml/2.2"
K = "{" + KML_NS + "}"


# =============================================================================
# GENERIC
# =============================================================================

_PROGRESS_STARTS: dict[str, float] = {}


def info(msg: str = "") -> None:
    print(msg, flush=True)


def _hidden_process_kwargs() -> dict[str, Any]:
    """Run Windows CLI children completely hidden.

    LAStools executables are console applications. Some Windows environments
    still flash/open a console when only CREATE_NO_WINDOW is supplied,
    particularly from parallel worker processes. Use both CREATE_NO_WINDOW and
    STARTUPINFO(SW_HIDE) so per-tile lasboundary/las2las/lasthin/las2txt jobs
    remain background processes while stdout/stderr stays capturable.
    """
    if os.name != "nt":
        return {}

    kwargs: dict[str, Any] = {}

    try:
        kwargs["creationflags"] = int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    except Exception:
        pass

    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = si
    except Exception:
        pass

    return kwargs


def console_stage(code: str, description: str = "") -> None:
    label = txt(code).upper()
    info("")
    info("=" * 80)
    info(f"[ {label} ]" + (f"  {description}" if description else ""))
    info("=" * 80)


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None or not np.isfinite(seconds) or seconds < 0:
        return "--:--:--"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def console_progress(
    key: str,
    label: str,
    current: int,
    total: int,
    detail: str = "",
    width: int = 28,
) -> None:
    """Single-line progress bar with elapsed/ETA; no external dependency."""
    total = max(int(total), 1)
    current = max(0, min(int(current), total))
    now = time.monotonic()
    if key not in _PROGRESS_STARTS or current <= 0:
        _PROGRESS_STARTS[key] = now

    elapsed = max(0.0, now - _PROGRESS_STARTS[key])
    frac = current / total
    filled = int(round(width * frac))
    bar = "#" * filled + "-" * (width - filled)
    pct = int(round(frac * 100))

    eta = None
    if current > 0 and current < total:
        eta = elapsed / current * (total - current)

    tail = f" | {detail}" if detail else ""
    line = (
        f"\r  {label:<24} [{bar}] {pct:3d}% "
        f"{current:>4}/{total:<4} "
        f"| elapsed {_fmt_duration(elapsed)}"
    )
    if current < total:
        line += f" | ETA {_fmt_duration(eta)}"
    line += tail[:80]

    print(line, end="", flush=True)
    if current >= total:
        print("", flush=True)
        _PROGRESS_STARTS.pop(key, None)


def txt(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"nan", "none"} else s


def clean_path(value: str) -> Path:
    s = txt(value)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in {'"', "'"}:
        s = s[1:-1]
    return Path(s).expanduser()


def circuit_key(v: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", txt(v).upper())


def row_key(v: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", txt(v).upper())


def safe_filename(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", txt(s)).strip("._")
    return s or "UNKNOWN"


def normalise_voltage(v: Any) -> str:
    s = txt(v)
    if not s:
        return ""
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return s
    x = float(m.group(0))
    return str(int(x)) if x.is_integer() else str(x)


def unit_to_metre(crs) -> float:
    return float(CRS.from_user_input(crs).axis_info[0].unit_conversion_factor)


def is_line(g) -> bool:
    return g is not None and not g.is_empty and g.geom_type in {"LineString", "MultiLineString"}


def longest_line(g):
    if g is None or g.is_empty:
        return None
    if g.geom_type == "LineString":
        return g
    if g.geom_type == "MultiLineString":
        parts = list(g.geoms)
        return max(parts, key=lambda x: x.length) if parts else None
    return None


def coords3d(g):
    line = longest_line(g)
    if line is None:
        return []
    out = []
    for c in line.coords:
        out.append((float(c[0]), float(c[1]), float(c[2]) if len(c) >= 3 else 0.0))
    return out


def force_2d(g):
    line = longest_line(g)
    if line is None:
        return g
    return LineString([(float(c[0]), float(c[1])) for c in line.coords])


def midpoint(g):
    line = longest_line(g)
    return line.interpolate(0.5, normalized=True) if line is not None else Point()


def rows_to_gdf(rows, crs, geom_col="geometry"):
    if rows:
        return gpd.GeoDataFrame(rows, geometry=geom_col, crs=crs)
    return gpd.GeoDataFrame({geom_col: gpd.GeoSeries([], crs=crs)}, geometry=geom_col, crs=crs)


def write_layer(gdf, gpkg: Path, layer: str):
    if gdf is not None and not gdf.empty:
        gdf.to_file(gpkg, layer=layer, driver="GPKG", engine="pyogrio", index=False)


class UnionFind:
    def __init__(self, n: int):
        self.p = list(range(n))
        self.r = [0] * n

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.r[ra] < self.r[rb]:
            ra, rb = rb, ra
        self.p[rb] = ra
        if self.r[ra] == self.r[rb]:
            self.r[ra] += 1



def _archive_qa_run(
    output_dir: Path,
    stage: str,
    products: list[str] | set[str] | tuple[str, ...] | None = None,
    selected_rows: list[str] | set[str] | tuple[str, ...] | None = None,
    notes: dict[str, Any] | None = None,
) -> Path:
    """Preserve one immutable QA snapshot after a successful stage run.

    Canonical QA files under output/00_preflight, 01_geometry and 02_points remain
    the latest view for compatibility.  Every successful run is additionally
    copied to output/QA/<timestamp>_<stage>[_products], so later selected-product
    runs cannot erase the evidence from earlier runs.
    """
    output_dir = Path(output_dir)
    qa_root = output_dir.parent / "QA"
    qa_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%fZ")
    product_list = sorted({txt(x) for x in (products or []) if txt(x)})
    suffix = "_" + "-".join(safe_filename(x) for x in product_list) if product_list else ""
    run_dir = qa_root / f"{stamp}_{safe_filename(stage)}{suffix}"
    run_dir.mkdir(parents=True, exist_ok=False)

    copied = []
    if output_dir.exists():
        for p in sorted(output_dir.iterdir()):
            if not p.is_file():
                continue
            # Temp/workspace files are directories; all direct stage files are QA,
            # manifests, or compact spatial evidence worth preserving.
            dst = run_dir / p.name
            shutil.copy2(p, dst)
            copied.append(p.name)

    manifest = {
        "qa_archive_version": 1,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "products": product_list,
        "selected_rows": sorted({txt(x) for x in (selected_rows or []) if txt(x)}),
        "source_output_dir": str(output_dir),
        "copied_files": copied,
        "notes": notes or {},
    }
    (run_dir / "QA_RUN_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    info(f"QA archive: {run_dir}")
    return run_dir


def _write_xyz_class_array(path: Path, arr: np.ndarray, cls_value: int) -> int:
    """Write X Y Z CLASS, matching the project PLS ASCII feature format."""
    path = Path(path)
    if arr is None or len(arr) == 0:
        if path.exists():
            path.unlink()
        return 0
    a = np.asarray(arr, dtype=float)
    if a.ndim == 1:
        a = a.reshape(1, -1)
    a = a[:, :3]
    path.parent.mkdir(parents=True, exist_ok=True)
    out = np.empty((len(a), 4), dtype=float)
    out[:, :3] = a
    out[:, 3] = int(cls_value)
    np.savetxt(path, out, fmt=["%.3f", "%.3f", "%.3f", "%d"])
    return int(len(a))

# =============================================================================
# DXF INPUTS
# =============================================================================

def decode_cad_layer(name: str) -> str:
    s = txt(name)
    def repl(m):
        try:
            return chr(int(m.group(1), 16))
        except Exception:
            return m.group(0)
    s = re.sub(r"_x([0-9A-Fa-f]{4})_", repl, s)
    s = re.sub(r"__x([0-9A-Fa-f]{4})__", repl, s)
    return re.sub(r"_+", "_", s).strip("_")


def dxf_entity_coords_wcs(entity):
    etype = entity.dxftype()
    if etype == "LINE":
        a, b = entity.dxf.start, entity.dxf.end
        return [(float(a.x), float(a.y), float(a.z)),
                (float(b.x), float(b.y), float(b.z))]
    if etype == "LWPOLYLINE":
        return [(float(p.x), float(p.y), float(p.z)) for p in entity.vertices_in_wcs()]
    if etype == "POLYLINE":
        return [(float(v.dxf.location.x), float(v.dxf.location.y), float(v.dxf.location.z))
                for v in entity.vertices]
    return []


def clean_consecutive_coords(coords):
    if not coords:
        return []
    out = [coords[0]]
    for p in coords[1:]:
        q = out[-1]
        if any(abs(p[i] - q[i]) > 1e-9 for i in range(3)):
            out.append(p)
    return out


def read_dxf_wires(path: Path, crs) -> gpd.GeoDataFrame:
    doc = ezdxf.readfile(path)
    rows = []
    for entity in doc.modelspace():
        if entity.dxftype() not in {"LINE", "POLYLINE", "LWPOLYLINE"}:
            continue
        coords = clean_consecutive_coords(dxf_entity_coords_wcs(entity))
        if len(coords) < 2:
            continue
        rows.append({
            "WIRE_ID": f"DXF_W{len(rows)+1:06d}",
            "SOURCE_LAYER": decode_cad_layer(entity.dxf.layer),
            "SOURCE_ENTITY": entity.dxftype(),
            "geometry": LineString(coords),
        })
    if not rows:
        raise RuntimeError("No LINE/POLYLINE/LWPOLYLINE 3D wires found in DXF")
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)


def _dxf_point_from_entity(entity):
    t = entity.dxftype()
    try:
        if t == "POINT":
            p = entity.dxf.location
            return float(p.x), float(p.y), float(p.z)
        if t == "INSERT":
            p = entity.dxf.insert
            return float(p.x), float(p.y), float(p.z)
        if t == "CIRCLE":
            p = entity.dxf.center
            return float(p.x), float(p.y), float(p.z)
    except Exception:
        return None
    return None


def cluster_xyz_rows(rows, tolerance_source: float):
    if not rows:
        return []
    xy = np.asarray([[r["X"], r["Y"]] for r in rows], dtype=float)
    tree = cKDTree(xy)
    uf = UnionFind(len(rows))
    for a, b in tree.query_pairs(r=max(tolerance_source, 1e-9)):
        uf.union(int(a), int(b))
    groups = defaultdict(list)
    for i in range(len(rows)):
        groups[uf.find(i)].append(i)
    out = []
    for inds in groups.values():
        xs = [rows[i]["X"] for i in inds]
        ys = [rows[i]["Y"] for i in inds]
        zs = [rows[i]["Z"] for i in inds]
        layers = sorted({rows[i]["SOURCE_LAYER"] for i in inds if rows[i]["SOURCE_LAYER"]})
        ents = sorted({rows[i]["SOURCE_ENTITY"] for i in inds if rows[i]["SOURCE_ENTITY"]})
        out.append({
            "X": float(np.mean(xs)), "Y": float(np.mean(ys)), "Z": float(np.mean(zs)),
            "SOURCE_LAYER": ";".join(layers), "SOURCE_ENTITY": ";".join(ents),
            "N_MARKERS": len(inds), "RAW_INDICES": inds,
        })
    return out


def read_dxf_structure_points(path: Path, crs, unit_to_m: float,
                              marker_cluster_m: float, marker_max_length_m: float) -> gpd.GeoDataFrame:
    """Read point/marker entities from Structure Top Attachment Point DXF."""
    doc = ezdxf.readfile(path)
    rows, fallback = [], []
    max_marker_source = marker_max_length_m / unit_to_m
    for entity in doc.modelspace():
        layer = decode_cad_layer(entity.dxf.layer)
        p = _dxf_point_from_entity(entity)
        if p is not None:
            rows.append({"X": p[0], "Y": p[1], "Z": p[2],
                         "SOURCE_LAYER": layer, "SOURCE_ENTITY": entity.dxftype()})
            continue
        if entity.dxftype() in {"LINE", "POLYLINE", "LWPOLYLINE"}:
            coords = clean_consecutive_coords(dxf_entity_coords_wcs(entity))
            if len(coords) < 2:
                continue
            geom = LineString(coords)
            if force_2d(geom).length <= max_marker_source:
                mid = force_2d(geom).interpolate(0.5, normalized=True)
                fallback.append({
                    "X": float(mid.x), "Y": float(mid.y),
                    "Z": float(np.mean([c[2] for c in coords])),
                    "SOURCE_LAYER": layer, "SOURCE_ENTITY": entity.dxftype(),
                })
    if not rows:
        rows = fallback
    elif fallback:
        rows.extend(fallback)
    if not rows:
        raise RuntimeError("No structure points/markers found in structure DXF")

    clustered = cluster_xyz_rows(rows, marker_cluster_m / unit_to_m)
    out = []
    for i, r in enumerate(clustered, 1):
        out.append({
            "POINT_ID": f"PT{i:05d}", "SOURCE_LAYER": r["SOURCE_LAYER"],
            "SOURCE_ENTITY": r["SOURCE_ENTITY"], "N_MARKERS": r["N_MARKERS"],
            "X": r["X"], "Y": r["Y"], "Z": r["Z"],
            "geometry": Point(r["X"], r["Y"], r["Z"]),
        })
    return gpd.GeoDataFrame(out, geometry="geometry", crs=crs)


def build_support_sites(points: gpd.GeoDataFrame, unit_to_m: float, support_cluster_m: float):
    """Cluster nearby contractor attachment markers into one physical support site.

    This intentionally only creates a support topology key. It does NOT move source
    wire geometry. The final wire is always derived from an actual source conductor.
    """
    xy = np.column_stack([points.geometry.x, points.geometry.y])
    tree = cKDTree(xy)
    uf = UnionFind(len(points))
    radius = support_cluster_m / unit_to_m
    for a, b in tree.query_pairs(r=max(radius, 1e-9)):
        uf.union(int(a), int(b))
    groups = defaultdict(list)
    for i in range(len(points)):
        groups[uf.find(i)].append(i)

    rows = []
    point_to_support = {}
    for n, inds in enumerate(groups.values(), 1):
        sg = points.iloc[inds]
        x = float(sg.geometry.x.mean())
        y = float(sg.geometry.y.mean())
        zvals = [float(g.z) if getattr(g, "has_z", False) else float(r.get("Z", 0.0))
                 for _, r in sg.iterrows() for g in [r.geometry]]
        z = float(np.mean(zvals)) if zvals else 0.0
        sid = f"ST{n:05d}"
        for idx in inds:
            point_to_support[int(idx)] = sid
        rows.append({
            "STRUCTURE_ID": sid,
            "N_SOURCE_POINTS": int(len(inds)),
            "X": x, "Y": y, "Z": z,
            "geometry": Point(x, y, z),
        })
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=points.crs), point_to_support


def _wire_endpoint_tangent(geom, at_start: bool, sample_source: float) -> np.ndarray | None:
    """Undirected local wire tangent near one endpoint, using plan geometry."""
    line = force_2d(geom)
    if line is None or line.is_empty or line.length <= 1e-9:
        return None
    d = min(float(sample_source), float(line.length))
    if at_start:
        a = line.interpolate(0.0)
        b = line.interpolate(d)
    else:
        a = line.interpolate(float(line.length))
        b = line.interpolate(max(0.0, float(line.length) - d))
    v = np.asarray([float(b.x - a.x), float(b.y - a.y)], dtype=float)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else None


def recenter_supports_on_wire_axis(
    supports: gpd.GeoDataFrame,
    wire_matches: gpd.GeoDataFrame,
    wires: gpd.GeoDataFrame,
    unit_to_m: float,
    tangent_sample_m: float = 5.0,
) -> gpd.GeoDataFrame:
    """Move contractor top markers laterally onto the physical wire axis.

    The contractor point is retained as the along-line station. Only the transverse
    component is corrected. The correction is derived from all matched 3D wire
    endpoints at the support, so an H-pole marker placed on one pole is shifted to
    the median attachment axis rather than being treated as the structure centre.
    """
    out = supports.copy()
    by_sid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sample_source = float(tangent_sample_m) / unit_to_m

    for _, r in wire_matches.iterrows():
        wi = int(r["WIRE_ROW"])
        geom = wires.iloc[wi].geometry
        for side, sid, x, y in (
            ("A", txt(r["STRUCT_A"]), float(r["A_X"]), float(r["A_Y"])),
            ("B", txt(r["STRUCT_B"]), float(r["B_X"]), float(r["B_Y"])),
        ):
            v = _wire_endpoint_tangent(geom, side == "A", sample_source)
            if v is None:
                continue
            by_sid[sid].append({"xy": np.asarray([x, y], dtype=float), "tangent": v})

    new_rows = []
    for _, r in out.iterrows():
        sid = txt(r["STRUCTURE_ID"])
        p0 = np.asarray([float(r.geometry.x), float(r.geometry.y)], dtype=float)
        entries = by_sid.get(sid, [])
        top_z = float(r.geometry.z) if getattr(r.geometry, "has_z", False) else float(r.get("Z", 0.0))

        q = p0.copy()
        shift_m = 0.0
        tangent = np.asarray([1.0, 0.0], dtype=float)
        n_ep = len(entries)

        if entries:
            # Principal undirected direction: v and -v contribute identically.
            M = np.zeros((2, 2), dtype=float)
            for e in entries:
                v = e["tangent"]
                M += np.outer(v, v)
            vals, vecs = np.linalg.eigh(M)
            tangent = vecs[:, int(np.argmax(vals))]
            tangent = tangent / max(float(np.linalg.norm(tangent)), 1e-12)
            normal = np.asarray([-tangent[1], tangent[0]], dtype=float)
            offsets = [float(np.dot(e["xy"] - p0, normal)) for e in entries]
            # Keep a good contractor centre point unchanged when it already sits
            # inside the transverse envelope of the attached wires. Only correct
            # the H-pole / one-top-marker case where every wire lies on the same
            # side of the supplied marker.
            if offsets and not (min(offsets) <= 0.0 <= max(offsets)):
                lateral = float(np.median(offsets))
                q = p0 + lateral * normal
                shift_m = abs(lateral) * unit_to_m
                axis_rule = "OUTSIDE_WIRE_ENVELOPE"
            else:
                axis_rule = "KEEP_INPUT_POINT"

        if not entries:
            axis_rule = "NO_WIRE_ENDPOINTS"

        rec = r.to_dict()
        rec.update({
            "RAW_X": float(p0[0]),
            "RAW_Y": float(p0[1]),
            "TOP_Z": top_z,
            "AXIS_X": float(q[0]),
            "AXIS_Y": float(q[1]),
            "AXIS_SHIFT_M": float(shift_m),
            "AXIS_N_EP": int(n_ep),
            "AXIS_RULE": axis_rule,
            "TAN_X": float(tangent[0]),
            "TAN_Y": float(tangent[1]),
            "geometry": Point(float(q[0]), float(q[1]), top_z),
        })
        new_rows.append(rec)

    return gpd.GeoDataFrame(new_rows, geometry="geometry", crs=supports.crs)


# =============================================================================
# LIDAR GROUND ELEVATION
# =============================================================================

def discover_lidar_files(source: Path, temp_dir: Path) -> list[Path]:
    if source.is_file() and source.suffix.lower() in {".las", ".laz"}:
        return [source]
    if source.is_dir():
        files = sorted([p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in {".las", ".laz"}])
        if not files:
            raise RuntimeError(f"No LAS/LAZ files found in {source}")
        return files
    if source.is_file() and source.suffix.lower() == ".zip":
        root = temp_dir / "lidar_source"
        root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as zf:
            zf.extractall(root)
        files = sorted([p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".las", ".laz"}])
        if not files:
            raise RuntimeError("No LAS/LAZ files found inside LiDAR ZIP")
        return files
    raise RuntimeError("LiDAR source must be LAS, LAZ, ZIP, or a folder containing LAS/LAZ")


def _las_header_crs(header):
    try:
        return header.parse_crs()
    except Exception:
        return None


def _horizontal_projected_crs(crs):
    """Return the projected horizontal component from projected/compound CRS."""
    if crs is None:
        return None
    try:
        c = CRS.from_user_input(crs)
    except Exception:
        return None

    # Compound CRS must be decomposed first. pyproj can report a compound CRS
    # as projected, so checking c.is_projected before sub_crs_list is unsafe.
    try:
        subs = c.sub_crs_list or []
    except Exception:
        subs = []

    for sub in subs:
        try:
            if sub.is_projected:
                return sub
        except Exception:
            continue

    try:
        if c.is_projected:
            return c
    except Exception:
        pass

    return c


def _horizontal_epsg(crs) -> int | None:
    c = _horizontal_projected_crs(crs)
    if c is None:
        return None
    try:
        code = c.to_epsg()
        return int(code) if code is not None else None
    except Exception:
        return None


def _horizontal_crs_matches(lidar_crs, project_crs) -> bool:
    """Compare only the horizontal/projected CRS component."""
    if lidar_crs is None:
        return True

    l = _horizontal_projected_crs(lidar_crs)
    p = _horizontal_projected_crs(project_crs)
    if l is None or p is None:
        return False

    le = _horizontal_epsg(l)
    pe = _horizontal_epsg(p)
    if le is not None and pe is not None:
        return le == pe

    try:
        return bool(l.equals(p, ignore_axis_order=True))
    except TypeError:
        try:
            return bool(l.equals(p))
        except Exception:
            return l == p
    except Exception:
        return l == p


def _horizontal_crs_label(crs) -> str:
    c = _horizontal_projected_crs(crs)
    if c is None:
        return "UNKNOWN"
    epsg = _horizontal_epsg(c)
    if epsg is not None:
        return f"EPSG:{epsg}"
    try:
        return c.to_string()
    except Exception:
        return str(c)


def add_ground_elevation_from_lidar(
    structures: gpd.GeoDataFrame,
    lidar_source: Path,
    project_crs,
    unit_to_m: float,
    temp_dir: Path,
    search_radii_m: tuple[float, ...] = (3.0, 6.0, 12.0, 25.0),
    max_fit_points: int = 60,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Populate final circuit-structure Z from classified LiDAR ground (class 2).

    XY must already be the FINAL delivered structure position on the final circuit
    wire. LiDAR is used only to obtain ground elevation at that XY. The function
    never changes XY and never uses the contractor top-attachment Z as ground.
    """
    if laspy is None:
        raise RuntimeError(
            "LiDAR ground elevation requires laspy + lazrs. Install with: "
            "pip install laspy lazrs"
        )
    if structures is None or structures.empty:
        return structures, gpd.GeoDataFrame(
            {"geometry": gpd.GeoSeries([], crs=project_crs)},
            geometry="geometry", crs=project_crs
        )

    files = discover_lidar_files(lidar_source, temp_dir)
    pcrs = CRS.from_user_input(project_crs)
    qxy = np.column_stack([structures.geometry.x.to_numpy(), structures.geometry.y.to_numpy()])
    max_radius_m = float(max(search_radii_m))
    max_radius_src = max_radius_m / unit_to_m

    candidates: dict[int, list[tuple[float, float, float, float, str]]] = defaultdict(list)
    used_files = 0

    for path in files:
        try:
            with laspy.open(path) as fh:
                h = fh.header
                lcrs = _las_header_crs(h)
                if lcrs is not None and not _horizontal_crs_matches(lcrs, pcrs):
                    raise RuntimeError(
                        f"LiDAR horizontal CRS differs from project CRS for {path.name}: "
                        f"{_horizontal_crs_label(lcrs)} vs {_horizontal_crs_label(pcrs)}. "
                        "Reproject the LiDAR or use the matching horizontal EPSG."
                    )
                mins = np.asarray(h.mins[:2], dtype=float)
                maxs = np.asarray(h.maxs[:2], dtype=float)
                hit = np.where(
                    (qxy[:, 0] >= mins[0] - max_radius_src) &
                    (qxy[:, 0] <= maxs[0] + max_radius_src) &
                    (qxy[:, 1] >= mins[1] - max_radius_src) &
                    (qxy[:, 1] <= maxs[1] + max_radius_src)
                )[0]
                if len(hit) == 0:
                    continue
                used_files += 1
                local_queries = qxy[hit]
                for chunk in fh.chunk_iterator(2_000_000):
                    try:
                        cls = np.asarray(chunk.classification)
                    except Exception:
                        continue
                    mask = cls == 2
                    if not np.any(mask):
                        continue
                    x = np.asarray(chunk.x)[mask]
                    y = np.asarray(chunk.y)[mask]
                    z = np.asarray(chunk.z)[mask]
                    if len(x) == 0:
                        continue
                    pts = np.column_stack([x, y])
                    tree = cKDTree(pts)
                    for local_i, global_i in enumerate(hit):
                        ids = tree.query_ball_point(local_queries[local_i], r=max_radius_src)
                        if not ids:
                            continue
                        q = local_queries[local_i]
                        ids_arr = np.asarray(ids, dtype=int)
                        dx = x[ids_arr] - q[0]
                        dy = y[ids_arr] - q[1]
                        d = np.hypot(dx, dy)
                        order = np.argsort(d)[:max_fit_points]
                        for j in order:
                            candidates[int(global_i)].append(
                                (float(d[j]), float(x[ids_arr[j]]), float(y[ids_arr[j]]),
                                 float(z[ids_arr[j]]), path.name)
                            )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed reading LiDAR {path}: {exc}") from exc

    if used_files == 0:
        raise RuntimeError("No LiDAR tile overlaps the final structure locations")

    rows = []
    qa_rows = []
    id_field = next(
        (c for c in ("TOPO_ID", "STRUCT_ID", "STRUCTURE_ID") if c in structures.columns),
        None
    )

    for i, r in structures.reset_index(drop=True).iterrows():
        qx, qy = float(r.geometry.x), float(r.geometry.y)
        vals = sorted(candidates.get(i, []), key=lambda t: t[0])
        chosen = []
        used_radius = None

        for radius_m in search_radii_m:
            radius_src = radius_m / unit_to_m
            within = [v for v in vals if v[0] <= radius_src]
            if len(within) >= 3:
                chosen = within[:max_fit_points]
                used_radius = float(radius_m)
                break

        if not chosen and vals:
            chosen = vals[:min(max_fit_points, len(vals))]
            used_radius = float(max_radius_m)

        if not chosen:
            ground_z = np.nan
            method = "NO_GROUND"
            npts = 0
            lidar_file = ""
        else:
            arr = np.asarray([[v[1] - qx, v[2] - qy, v[3]] for v in chosen], dtype=float)
            npts = len(arr)
            if npts >= 3:
                A = np.column_stack([arr[:, 0], arr[:, 1], np.ones(npts)])
                coef, *_ = np.linalg.lstsq(A, arr[:, 2], rcond=None)
                ground_z = float(coef[2])
                method = "GROUND_PLANE"
            else:
                ground_z = float(np.median(arr[:, 2]))
                method = "GROUND_NEAREST"
            lidar_file = chosen[0][4]

        rec = r.to_dict()
        rec.update({
            "GROUND_Z": ground_z,
            "Z_SOURCE": "LIDAR_CLASS2" if np.isfinite(ground_z) else "NO_GROUND",
            "Z_METHOD": method,
            "Z_NPTS": int(npts),
            "Z_RADIUS_M": used_radius,
            "Z_LIDAR": lidar_file,
            "geometry": Point(qx, qy, ground_z) if np.isfinite(ground_z) else Point(qx, qy),
        })
        rows.append(rec)

        qa = {
            "CIRCUIT": txt(r.get("CIRCUIT")),
            "STRUCT_ID": txt(r.get("STRUCT_ID")),
            "GROUND_Z": ground_z,
            "Z_METHOD": method,
            "Z_NPTS": int(npts),
            "Z_RADIUS_M": used_radius,
            "Z_LIDAR": lidar_file,
            "geometry": Point(qx, qy),
        }
        if id_field:
            qa["TOPO_ID"] = txt(r.get(id_field))
        qa_rows.append(qa)

    result = gpd.GeoDataFrame(rows, geometry="geometry", crs=structures.crs)
    qa = gpd.GeoDataFrame(qa_rows, geometry="geometry", crs=structures.crs)
    missing = int((~np.isfinite(pd.to_numeric(result["GROUND_Z"], errors="coerce"))).sum())
    if missing:
        raise RuntimeError(
            f"LiDAR ground elevation could not be resolved for {missing} final structure(s). "
            "See structure_ground_qa before producing final Structure SHPs."
        )
    return result, qa


# =============================================================================
# CLIENT SOURCE + KMZ
# =============================================================================

def locate_client_lines(root: Path) -> Path:
    scored = []
    for p in root.rglob("*.shp"):
        try:
            fields = {str(x).upper() for x in pyogrio.read_info(p).get("fields", [])}
        except Exception:
            continue
        if not {"LINE_NO", "VOLTAGE"}.issubset(fields):
            continue
        score = 0
        if "T_OH_LINES" in p.name.upper(): score += 100
        if "LIDAREXPORT" in p.name.upper(): score += 20
        scored.append((score, p))
    if not scored:
        raise RuntimeError("Could not find client OH line SHP with LINE_NO and VOLTAGE")
    return max(scored, key=lambda x: x[0])[1]


def locate_client_structures(root: Path) -> Path | None:
    scored = []
    for p in root.rglob("*.shp"):
        try:
            fields = {str(x).upper() for x in pyogrio.read_info(p).get("fields", [])}
        except Exception:
            continue
        if "LINE_NO" not in fields:
            continue
        if not ({"STRUCTURE_", "GIS_NO", "STR_TYPE", "GLOBALID"} & fields):
            continue
        score = 0
        up = p.name.upper()
        if "T_STRUCTURES" in up: score += 100
        if "LIDAREXPORT" in up: score += 20
        if "STRUCT" in up: score += 10
        scored.append((score, p))
    return max(scored, key=lambda x: x[0])[1] if scored else None


def load_client_source(source: Path, temp_dir: Path) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    if source.is_dir():
        root = source
    elif source.suffix.lower() == ".zip":
        root = temp_dir / "client_source"
        root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as zf:
            zf.extractall(root)
    elif source.suffix.lower() == ".shp":
        # Advanced fallback: a standalone line SHP has no structure dataset.
        lines = gpd.read_file(source, engine="pyogrio")
        empty = gpd.GeoDataFrame({"geometry": gpd.GeoSeries([], crs=lines.crs)}, geometry="geometry", crs=lines.crs)
        return lines, empty
    else:
        raise RuntimeError("Client source must be ZIP/folder/SHP")

    line_shp = locate_client_lines(root)
    struct_shp = locate_client_structures(root)
    info(f"  client lines:      {line_shp.name}")
    lines = gpd.read_file(line_shp, engine="pyogrio")
    if struct_shp is None:
        info("  client structures: not found; internal topology IDs will be retained")
        structures = gpd.GeoDataFrame({"geometry": gpd.GeoSeries([], crs=lines.crs)}, geometry="geometry", crs=lines.crs)
    else:
        info(f"  client structures: {struct_shp.name}")
        structures = gpd.read_file(struct_shp, engine="pyogrio")
    return lines, structures


_STRUCTURE_CANONICAL_FIELDS = {
    "ROW", "CIRCUIT", "VOLTAGE", "COMPANY", "STRUCT_ID", "TOPO_ID",
}

_STRUCTURE_INTERNAL_FIELDS = {
    "SRC_GID", "SRC_LINE", "SRC_STRUCT", "SRC_X", "SRC_Y",
    "SRC_DIST_M", "SRC_MATCH", "ROW_DIST_M",
    "POS_RULE", "POS_SOURCE", "POS_CONF", "EXPECTED_POLES", "SOURCE_KIND",
    "N_TOP_CAND", "N_STRUCTURAL", "N_OUTLIERS",
    "CLIENT_USED", "CLIENT_X", "CLIENT_Y", "CLIENT_DELTA_M",
    "CLIENT_DX_M", "CLIENT_DY_M", "WIRE_DIST_M",
    "TOP_POINT_ID", "TOP_X", "TOP_Y", "TOP_Z",
    "HFRAME_COMPANION_ID", "HFRAME_PAIR_M", "HFRAME_PAIR_ANGLE_DEG",
    "HFRAME_PAIR_ALONG_M", "HFRAME_PAIR_CROSS_M",
    "GROUND_Z", "Z_SOURCE",
}

_SOURCE_COLLISION_ALIASES = {
    "ROW": "SRC_ROW",
    "CIRCUIT": "SRC_CKT",
    "VOLTAGE": "SRC_VOLT",
    "COMPANY": "SRC_COMP",
    "STRUCT_ID": "SRC_STRID",
    "TOPO_ID": "SRC_TOPO",
}


def _dbf_safe_field_name(name: str, used: set[str]) -> str:
    """Create a deterministic unique <=10-char DBF/Shapefile field name."""
    raw = re.sub(r"[^A-Za-z0-9_]+", "_", txt(name).upper()).strip("_")
    if not raw:
        raw = "SRC_FIELD"

    base = raw[:10]
    candidate = base
    n = 1
    while candidate.upper() in used:
        suffix = str(n)
        candidate = (base[: 10 - len(suffix)] + suffix)[:10]
        n += 1
    used.add(candidate.upper())
    return candidate


def _build_structure_source_field_map(columns: list[str]) -> dict[str, str]:
    """Map every client Structure source field to a unique delivery DBF field."""
    used = {
        x.upper()
        for x in (_STRUCTURE_CANONICAL_FIELDS | _STRUCTURE_INTERNAL_FIELDS)
    }
    mapping: dict[str, str] = {}

    for col in columns:
        name = str(col)
        upper = name.upper()

        if upper in {"GEOMETRY", "_CKEY"}:
            continue

        # Source SHP fields are already DBF-compatible. Preserve the original
        # field name wherever it does not collide with generated delivery fields.
        if (
            len(name) <= 10
            and re.fullmatch(r"[A-Za-z0-9_]+", name)
            and upper not in used
        ):
            target = name
            used.add(upper)
        else:
            preferred = _SOURCE_COLLISION_ALIASES.get(upper)
            if preferred and preferred.upper() not in used:
                target = preferred
                used.add(preferred.upper())
            else:
                target = _dbf_safe_field_name(f"S_{name}", used)

        mapping[name] = target

    return mapping


def _source_attribute_value(value):
    """Normalise pandas/numpy values for stable SHP writing."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (list, tuple, set, dict)):
        return str(value)
    return value


def client_structure_all_values(row, index) -> dict[str, Any]:
    """Return every mapped attribute from the matched client Structure row."""
    if row is None:
        return {}

    field_map = index.get("source_field_map", {}) if index else {}
    out: dict[str, Any] = {}
    for source_name, delivery_name in field_map.items():
        if source_name not in row.index:
            continue
        out[delivery_name] = _source_attribute_value(row.get(source_name))
    return out


def build_client_structure_index(g: gpd.GeoDataFrame):
    if g is None or g.empty:
        return {
            "groups": {},
            "global": None,
            "columns": {},
            "source_field_map": {},
            "delivery_source_fields": [],
        }

    x = g[g.geometry.notna() & ~g.geometry.is_empty].copy().reset_index(drop=True)
    cols = {str(c).upper(): c for c in x.columns}

    original_fields = [
        str(c) for c in x.columns
        if str(c).lower() != "geometry" and str(c) != "_CKEY"
    ]
    source_field_map = _build_structure_source_field_map(original_fields)
    delivery_source_fields = [
        source_field_map[c]
        for c in original_fields
        if c in source_field_map
    ]

    line_col = cols.get("LINE_NO")
    if line_col is None:
        return {
            "groups": {},
            "global": None,
            "columns": cols,
            "source_field_map": source_field_map,
            "delivery_source_fields": delivery_source_fields,
        }

    x["_CKEY"] = x[line_col].apply(circuit_key)
    groups = {}
    for key, sg in x.groupby("_CKEY", sort=False):
        if not key or sg.empty:
            continue
        sg = sg.reset_index(drop=True)
        groups[key] = (
            sg,
            cKDTree(np.column_stack([sg.geometry.x, sg.geometry.y])),
        )

    global_tree = (
        x,
        cKDTree(np.column_stack([x.geometry.x, x.geometry.y])),
    ) if not x.empty else None

    return {
        "groups": groups,
        "global": global_tree,
        "columns": cols,
        "source_field_map": source_field_map,
        "delivery_source_fields": delivery_source_fields,
    }


def match_client_structure(point: Point, circuit: str, index, unit_to_m: float):
    key = circuit_key(circuit)
    method = ""
    pack = index.get("groups", {}).get(key)
    if pack is not None:
        method = "SAME_CIRCUIT_NEAREST"
    else:
        pack = index.get("global")
        method = "GLOBAL_NEAREST" if pack is not None else "NO_SOURCE_STRUCTURE"
    if pack is None:
        return None, None, method
    sg, tree = pack
    d, i = tree.query([float(point.x), float(point.y)], k=1)
    return sg.iloc[int(i)], float(d) * unit_to_m, method


def client_structure_values(row, topo_id: str):
    if row is None:
        return {
            "STRUCT_ID": topo_id, "GIS_NO": "", "STR_TYPE": "", "MATERIAL": "",
            "SRC_GID": "", "SRC_LINE": "", "SRC_STRUCT": "", "SRC_X": np.nan, "SRC_Y": np.nan,
        }
    def val(name):
        return txt(row.get(name)) if name in row.index else ""
    struct = val("STRUCTURE_") or val("GIS_NO") or val("GLOBALID") or topo_id
    return {
        "STRUCT_ID": struct,
        "GIS_NO": val("GIS_NO"),
        "STR_TYPE": val("STR_TYPE"),
        "MATERIAL": val("MATERIAL"),
        "SRC_GID": val("GLOBALID"),
        "SRC_LINE": val("LINE_NO"),
        "SRC_STRUCT": val("STRUCTURE_"),
        "SRC_X": float(row.geometry.x) if row.geometry is not None and not row.geometry.is_empty else np.nan,
        "SRC_Y": float(row.geometry.y) if row.geometry is not None and not row.geometry.is_empty else np.nan,
    }


def client_lookup(g: gpd.GeoDataFrame):
    cols = {str(c).upper(): c for c in g.columns}
    line_col = cols["LINE_NO"]
    volt_col = cols.get("VOLTAGE")
    company_col = cols.get("COMPANY")
    gid_col = cols.get("GLOBALID")
    keyed = g.copy()
    keyed["_CKEY"] = keyed[line_col].apply(circuit_key)
    result = {}
    for key, sg in keyed.groupby("_CKEY", sort=False):
        if not key:
            continue
        labels = sorted({txt(v) for v in sg[line_col] if txt(v)})
        voltages = sorted({normalise_voltage(v) for v in sg[volt_col] if normalise_voltage(v)}) if volt_col else []
        companies = sorted({txt(v) for v in sg[company_col] if txt(v)}) if company_col else []
        gids = sorted({txt(v) for v in sg[gid_col] if txt(v)}) if gid_col else []
        geoms = [x for x in sg.geometry if x is not None and not x.is_empty]
        result[key] = {
            "line_no": labels[0] if labels else key,
            "voltages": voltages, "companies": companies, "globalids": gids,
            "geometry": unary_union(geoms) if geoms else None,
        }
    return result


@dataclass
class KmzRef:
    row: str
    circuit: str
    voltage: str
    company: str
    globalid: str
    geometry_wgs84: LineString
    geometry: LineString | None = None


def load_kml_root(kmz: Path):
    with zipfile.ZipFile(kmz) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
        if not names:
            raise RuntimeError("No KML inside KMZ")
        return ET.fromstring(zf.read(names[0]))


def folder_name(f):
    return txt(f.findtext(K + "name"))


def find_named_folder(root, name: str):
    for f in root.iter(K + "Folder"):
        if folder_name(f).lower() == name.lower():
            return f
    return None


def parse_description(desc: str):
    if not desc:
        return {}
    s = html.unescape(desc)
    pairs = re.findall(r"<B>\s*([^<]+?)\s*</B>\s*=\s*([^<\r\n]+)", s, flags=re.I)
    out = {txt(k).upper(): txt(v) for k, v in pairs}
    if out:
        return out
    for line in re.split(r"<BR\s*/?>|\r?\n", s, flags=re.I):
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = re.sub(r"<[^>]+>", "", k).strip().upper()
        v = re.sub(r"<[^>]+>", "", v).strip()
        if k:
            out[k] = v
    return out


def parse_kml_line(ls):
    raw = txt(ls.findtext(K + "coordinates"))
    coords = []
    for token in raw.split():
        p = token.split(",")
        if len(p) < 2:
            continue
        try:
            coords.append((float(p[0]), float(p[1])))
        except ValueError:
            pass
    return LineString(coords) if len(coords) >= 2 else None


def parse_sow_kmz(kmz: Path):
    root = load_kml_root(kmz)
    circuits_folder = find_named_folder(root, "Circuits")
    if circuits_folder is None:
        raise RuntimeError("Could not find Circuits folder in KMZ")
    rows = []
    for row_folder in circuits_folder.findall(K + "Folder"):
        row_name = folder_name(row_folder)
        for circuit_folder in row_folder.findall(K + "Folder"):
            circuit_name = folder_name(circuit_folder)
            for pm in circuit_folder.findall(K + "Placemark"):
                attrs = parse_description(txt(pm.findtext(K + "description")))
                circuit = txt(attrs.get("LINE_NO")) or circuit_name
                voltage = normalise_voltage(attrs.get("VOLTAGE"))
                company = txt(attrs.get("COMPANY"))
                gid = txt(attrs.get("GLOBALID"))
                for ls in pm.iter(K + "LineString"):
                    geom = parse_kml_line(ls)
                    if geom is not None:
                        rows.append(KmzRef(row_name, circuit, voltage, company, gid, geom))
    if not rows:
        raise RuntimeError("No circuit linework found in KMZ")
    return rows


def project_kmz(refs, target_crs):
    tr = Transformer.from_crs("EPSG:4326", target_crs, always_xy=True)
    return [KmzRef(r.row, r.circuit, r.voltage, r.company, r.globalid,
                   r.geometry_wgs84, transform(tr.transform, r.geometry_wgs84)) for r in refs]


def kmz_lookup(refs):
    grouped = defaultdict(list)
    for r in refs:
        grouped[circuit_key(r.circuit)].append(r)
    result = {}
    for key, items in grouped.items():
        by_row = defaultdict(list)
        voltages, companies, gids = set(), set(), set()
        for r in items:
            if r.geometry is not None:
                by_row[r.row].append(r.geometry)
            if r.voltage: voltages.add(r.voltage)
            if r.company: companies.add(r.company)
            if r.globalid: gids.add(r.globalid)
        geoms = [r.geometry for r in items if r.geometry is not None]
        result[key] = {
            "rows": {row: unary_union(gs) for row, gs in by_row.items()},
            "geometry": unary_union(geoms) if geoms else None,
            "voltages": sorted(voltages), "companies": sorted(companies),
            "globalids": sorted(gids),
        }
    return result


# =============================================================================
# RAW WIRES -> PHYSICAL SPAN BUNDLES
# =============================================================================

def endpoint_assignment(wires, supports, unit_to_m, max_endpoint_m):
    sxy = np.column_stack([supports.geometry.x, supports.geometry.y])
    tree = cKDTree(sxy)
    rows, review = [], []
    for wi, r in wires.reset_index(drop=True).iterrows():
        c = coords3d(r.geometry)
        if len(c) < 2:
            continue
        vals = []
        for side, p in (("A", c[0]), ("B", c[-1])):
            d, idx = tree.query([p[0], p[1]], k=1)
            vals.append({
                "SIDE": side, "STRUCTURE_ID": txt(supports.iloc[int(idx)]["STRUCTURE_ID"]),
                "DIST_M": float(d) * unit_to_m,
                "X": float(p[0]), "Y": float(p[1]), "Z": float(p[2]),
            })
        ok = vals[0]["STRUCTURE_ID"] != vals[1]["STRUCTURE_ID"] and max(v["DIST_M"] for v in vals) <= max_endpoint_m
        if not ok:
            review.append({
                "WIRE_ID": txt(r["WIRE_ID"]),
                "STRUCT_A": vals[0]["STRUCTURE_ID"], "STRUCT_B": vals[1]["STRUCTURE_ID"],
                "DIST_A_M": vals[0]["DIST_M"], "DIST_B_M": vals[1]["DIST_M"],
                "REASON": "ENDPOINT_STRUCTURE_MATCH_FAILED",
                "geometry": force_2d(r.geometry),
            })
            continue
        rows.append({
            "WIRE_ROW": int(wi), "WIRE_ID": txt(r["WIRE_ID"]),
            "STRUCT_A": vals[0]["STRUCTURE_ID"], "STRUCT_B": vals[1]["STRUCTURE_ID"],
            "DIST_A_M": vals[0]["DIST_M"], "DIST_B_M": vals[1]["DIST_M"],
            "A_X": vals[0]["X"], "A_Y": vals[0]["Y"], "A_Z": vals[0]["Z"],
            "B_X": vals[1]["X"], "B_Y": vals[1]["Y"], "B_Z": vals[1]["Z"],
            "SOURCE_LAYER": txt(r["SOURCE_LAYER"]),
            "geometry": r.geometry,
        })
    return rows_to_gdf(rows, wires.crs), rows_to_gdf(review, wires.crs)


def support_lookup(supports):
    return {txt(r["STRUCTURE_ID"]): r for _, r in supports.iterrows()}


def orient_wire_record(r, sa: str, sb: str, supports_by_id):
    A = np.asarray([supports_by_id[sa].geometry.x, supports_by_id[sa].geometry.y], dtype=float)
    B = np.asarray([supports_by_id[sb].geometry.x, supports_by_id[sb].geometry.y], dtype=float)
    c = coords3d(r.geometry)
    p0, p1 = np.asarray(c[0][:2]), np.asarray(c[-1][:2])
    if np.linalg.norm(p0-A) + np.linalg.norm(p1-B) <= np.linalg.norm(p1-A) + np.linalg.norm(p0-B):
        coords = c
    else:
        coords = list(reversed(c))
    return coords


def choose_bundle_count(n_wires: int) -> int:
    """Infer number of circuit bundles in one physical structure-to-structure span.

    Each circuit is expected to contribute 3 phase conductors plus 0-2 earth/static
    wires, i.e. 3..5 source vectors. Choose the smallest plausible number of bundles
    that keeps each bundle in that range.
    """
    if n_wires <= 5:
        return 1
    possible = [k for k in range(2, max(3, n_wires // 3 + 1)) if 3*k <= n_wires <= 5*k]
    if not possible:
        return max(1, round(n_wires / 5))
    # Prefer bundle size closest to 5 (typical 3 phase + 2 shield/static).
    return min(possible, key=lambda k: abs(n_wires / k - 5.0))


def partition_sorted_indices(n: int, k: int):
    """Contiguous near-equal partition. Used after sorting wires by lateral position."""
    base, rem = divmod(n, k)
    sizes = [base + (1 if i < rem else 0) for i in range(k)]
    out, pos = [], 0
    for s in sizes:
        out.append(list(range(pos, pos+s)))
        pos += s
    return out


def build_span_bundles(wire_matches, supports, unit_to_m,
                       horizontal_z_range_m, horizontal_z_ratio):
    by_sid = support_lookup(supports)
    # normalise each wire to an unordered physical support pair
    working = wire_matches.copy()
    working["PAIR"] = working.apply(
        lambda r: "|".join(sorted((txt(r["STRUCT_A"]), txt(r["STRUCT_B"])))), axis=1
    )

    bundles, earth_rows, phase_discard_seed = [], [], []
    bundle_id = 0
    earth_words = re.compile(r"(?:earth|shield|static|ground(?:[_ -]?wire)?)", re.I)

    for pair_key, sg in working.groupby("PAIR", sort=False):
        sa, sb = pair_key.split("|", 1)
        A = np.asarray([by_sid[sa].geometry.x, by_sid[sa].geometry.y], dtype=float)
        B = np.asarray([by_sid[sb].geometry.x, by_sid[sb].geometry.y], dtype=float)
        dvec = B - A
        dn = float(np.linalg.norm(dvec))
        if dn <= 1e-9:
            continue
        tangent = dvec / dn
        normal = np.asarray([-tangent[1], tangent[0]])

        records = []
        for _, r in sg.iterrows():
            coords = orient_wire_record(r, sa, sb, by_sid)
            pa = np.asarray(coords[0][:2]); pb = np.asarray(coords[-1][:2])
            za, zb = float(coords[0][2]), float(coords[-1][2])
            off_a = float(np.dot(pa - A, normal)) * unit_to_m
            off_b = float(np.dot(pb - B, normal)) * unit_to_m
            zavg = (za + zb) / 2.0
            records.append({
                "WIRE_ROW": int(r["WIRE_ROW"]), "WIRE_ID": txt(r["WIRE_ID"]),
                "SOURCE_LAYER": txt(r["SOURCE_LAYER"]),
                "coords": coords, "geometry3d": r.geometry,
                "PA": pa, "PB": pb, "ZA": za, "ZB": zb,
                "OFF_A_M": off_a, "OFF_B_M": off_b,
                "OFF_MEAN_M": (off_a + off_b)/2.0,
                "Z_MEAN": zavg,
            })
        if not records:
            continue
        recs = pd.DataFrame(records).sort_values("OFF_MEAN_M", kind="stable").reset_index(drop=True)
        k = choose_bundle_count(len(recs))
        partitions = partition_sorted_indices(len(recs), k)

        for local_idx, positions in enumerate(partitions, 1):
            part = recs.iloc[positions].copy().reset_index(drop=True)
            if len(part) < 3:
                # not enough conductors to define a 3-phase circuit bundle
                continue

            explicit_earth = part[part["SOURCE_LAYER"].apply(lambda x: bool(earth_words.search(txt(x))))]
            if not explicit_earth.empty and len(part) - len(explicit_earth) >= 3:
                earth_ids = set(int(x) for x in explicit_earth["WIRE_ROW"])
            else:
                earth_count = max(0, min(2, len(part) - 3))
                earth_ids = set(
                    int(x) for x in part.sort_values("Z_MEAN", ascending=False, kind="stable").head(earth_count)["WIRE_ROW"]
                )

            phase = part[~part["WIRE_ROW"].isin(earth_ids)].copy()
            # Keep exactly three phase conductors. If more remain, choose the three
            # forming the tightest lateral group around the median position.
            if len(phase) > 3:
                med = float(np.median(phase["OFF_MEAN_M"]))
                phase = phase.assign(_D=(phase["OFF_MEAN_M"]-med).abs()).sort_values("_D").head(3).copy()
            if len(phase) < 3:
                continue

            for _, er in part[part["WIRE_ROW"].isin(earth_ids)].iterrows():
                earth_rows.append({
                    "WIRE_ROW": int(er["WIRE_ROW"]), "WIRE_ID": txt(er["WIRE_ID"]),
                    "STRUCT_A": sa, "STRUCT_B": sb, "geometry": er["geometry3d"],
                })

            # Arrangement must be horizontal at both ends to force the central phase.
            zra = (float(phase["ZA"].max()) - float(phase["ZA"].min())) * unit_to_m
            zrb = (float(phase["ZB"].max()) - float(phase["ZB"].min())) * unit_to_m
            tra = float(phase["OFF_A_M"].max() - phase["OFF_A_M"].min())
            trb = float(phase["OFF_B_M"].max() - phase["OFF_B_M"].min())
            ha = zra <= horizontal_z_range_m or (tra > 0.25 and zra <= horizontal_z_ratio * tra)
            hb = zrb <= horizontal_z_range_m or (trb > 0.25 and zrb <= horizontal_z_ratio * trb)
            arrangement = "HORIZONTAL" if ha and hb else "VERTICAL_OR_MIXED"

            # End centroid is used only for physical track continuity. It is not final geometry.
            ca = np.mean(np.stack(phase["PA"].to_list()), axis=0)
            cb = np.mean(np.stack(phase["PB"].to_list()), axis=0)

            candidates = []
            med_a, med_b = float(np.median(phase["OFF_A_M"])), float(np.median(phase["OFF_B_M"]))
            outer = max(float(np.max(np.abs(phase["OFF_A_M"]))), float(np.max(np.abs(phase["OFF_B_M"]))))
            for _, pr in phase.iterrows():
                central_score = abs(float(pr["OFF_A_M"])-med_a) + abs(float(pr["OFF_B_M"])-med_b)
                mean_abs = (abs(float(pr["OFF_A_M"])) + abs(float(pr["OFF_B_M"]))) / 2.0
                outer_penalty = max(0.0, outer - mean_abs)
                candidates.append({
                    "WIRE_ROW": int(pr["WIRE_ROW"]), "WIRE_ID": txt(pr["WIRE_ID"]),
                    "coords": pr["coords"], "geometry3d": pr["geometry3d"],
                    "OFF_A_M": float(pr["OFF_A_M"]), "OFF_B_M": float(pr["OFF_B_M"]),
                    "ZA": float(pr["ZA"]), "ZB": float(pr["ZB"]),
                    "CENTRAL_SCORE": float(central_score), "OUTER_PENALTY": float(outer_penalty),
                })

            bundle_id += 1
            bundles.append({
                "BUNDLE_ID": bundle_id, "STRUCT_A": sa, "STRUCT_B": sb,
                "PAIR": pair_key, "BUNDLE_LOCAL": local_idx,
                "N_SOURCE_WIRES": int(len(part)), "N_PHASE": 3,
                "ARRANGEMENT": arrangement,
                "Z_RANGE_A_M": zra, "Z_RANGE_B_M": zrb,
                "TRANS_RANGE_A_M": tra, "TRANS_RANGE_B_M": trb,
                "END_A_X": float(ca[0]), "END_A_Y": float(ca[1]),
                "END_B_X": float(cb[0]), "END_B_Y": float(cb[1]),
                # Retain the original 3D source vectors belonging to this physical
                # circuit bundle so they can be repackaged for Caydence without
                # flattening, simplifying or otherwise changing their geometry.
                "ALL_WIRE_ROWS": [int(x) for x in part["WIRE_ROW"].tolist()],
                "EARTH_WIRE_ROWS": sorted(int(x) for x in earth_ids),
                "CANDIDATES": candidates,
                "geometry": LineString([tuple(A), tuple(B)]),
            })

    bgdf = rows_to_gdf([
        {k:v for k,v in b.items() if k not in {"CANDIDATES", "ALL_WIRE_ROWS", "EARTH_WIRE_ROWS"}}
        for b in bundles
    ], supports.crs)
    return bundles, bgdf, rows_to_gdf(earth_rows, supports.crs), rows_to_gdf(phase_discard_seed, supports.crs)


# =============================================================================
# PHYSICAL TRACKS + GLOBAL CIRCUIT ATTRIBUTION
# =============================================================================

def angle_deg(a, b):
    a = np.asarray(a, dtype=float); b = np.asarray(b, dtype=float)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na <= 1e-12 or nb <= 1e-12:
        return 0.0
    return float(np.degrees(np.arccos(np.clip(np.dot(a/na, b/nb), -1.0, 1.0))))


def build_physical_tracks(
    bundles,
    supports,
    unit_to_m,
    max_track_join_m,
    ambiguity_margin_m: float = 2.0,
):
    """Join span bundles into physical tracks, but never force an ambiguous join.

    The old greedy pairing could join the wrong outgoing bundle at a shared
    structure where two circuits temporarily converge and then split again.
    That made one circuit's final tail look like a branch of the other circuit.

    Pairing is now accepted only when two bundle ends are mutual best matches
    and the next-best alternative is sufficiently worse.  Ambiguous ends remain
    as separate physical-track segments.  Circuit attribution is then applied
    independently to those segments, and `build_circuit_paths()` reconnects
    segments that share a structure AND the same circuit identity.

    This preserves normal unambiguous double-circuit continuity while allowing
    merge -> shared structure -> split cases to recover both circuit tails.
    """
    by_sid = support_lookup(supports)
    ends = defaultdict(list)
    for b in bundles:
        A = np.asarray([
            by_sid[b["STRUCT_A"]].geometry.x,
            by_sid[b["STRUCT_A"]].geometry.y
        ])
        B = np.asarray([
            by_sid[b["STRUCT_B"]].geometry.x,
            by_sid[b["STRUCT_B"]].geometry.y
        ])
        v = B - A
        v = v / max(np.linalg.norm(v), 1e-12)
        ends[b["STRUCT_A"]].append({
            "bid": b["BUNDLE_ID"],
            "other": b["STRUCT_B"],
            "pt": np.asarray([b["END_A_X"], b["END_A_Y"]]),
            "dir": v,
        })
        ends[b["STRUCT_B"]].append({
            "bid": b["BUNDLE_ID"],
            "other": b["STRUCT_A"],
            "pt": np.asarray([b["END_B_X"], b["END_B_Y"]]),
            "dir": -v,
        })

    uf = UnionFind(max([b["BUNDLE_ID"] for b in bundles], default=0) + 1)
    pair_rows = []

    for sid, items in ends.items():
        candidates = []
        by_item = defaultdict(list)

        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i], items[j]
                if a["other"] == b["other"]:
                    continue  # parallel bundles in same physical span

                ang = angle_deg(a["dir"], b["dir"])
                if ang < 90.0:
                    continue

                dist_m = float(np.linalg.norm(a["pt"] - b["pt"])) * unit_to_m
                if dist_m > max_track_join_m:
                    continue

                # Distance dominates; straight-through angle is the tie-breaker.
                cost = dist_m + 0.15 * (180.0 - ang)
                rec = {
                    "cost": float(cost),
                    "i": int(i),
                    "j": int(j),
                    "dist_m": float(dist_m),
                    "ang": float(ang),
                }
                candidates.append(rec)
                by_item[i].append((float(cost), j))
                by_item[j].append((float(cost), i))

        for idx in by_item:
            by_item[idx].sort(key=lambda x: (x[0], x[1]))

        candidates.sort(key=lambda r: (r["cost"], r["i"], r["j"]))
        used = set()

        for rec in candidates:
            i, j = rec["i"], rec["j"]
            if i in used or j in used:
                continue

            best_i = by_item.get(i, [])
            best_j = by_item.get(j, [])
            if not best_i or not best_j:
                continue

            # A continuation must be the best option from BOTH sides.
            mutual = (best_i[0][1] == j and best_j[0][1] == i)

            second_i = best_i[1][0] if len(best_i) > 1 else math.inf
            second_j = best_j[1][0] if len(best_j) > 1 else math.inf
            margin_i = second_i - rec["cost"]
            margin_j = second_j - rec["cost"]
            margin = min(margin_i, margin_j)

            if not mutual or margin < float(ambiguity_margin_m):
                # Record only the best ambiguous proposal for QA.  Do not union:
                # circuit-level logic will reconnect it after attribution.
                if best_i[0][1] == j:
                    pair_rows.append({
                        "STRUCTURE_ID": sid,
                        "BUNDLE_A": int(items[i]["bid"]),
                        "BUNDLE_B": int(items[j]["bid"]),
                        "JOIN_DIST_M": rec["dist_m"],
                        "ANGLE_DEG": rec["ang"],
                        "PAIR_STATUS": "AMBIGUOUS_NOT_JOINED",
                        "PAIR_MARGIN": (
                            float(margin) if np.isfinite(margin) else np.nan
                        ),
                        "geometry": Point(
                            float(by_sid[sid].geometry.x),
                            float(by_sid[sid].geometry.y),
                        ),
                    })
                continue

            used.add(i)
            used.add(j)
            uf.union(int(items[i]["bid"]), int(items[j]["bid"]))
            pair_rows.append({
                "STRUCTURE_ID": sid,
                "BUNDLE_A": int(items[i]["bid"]),
                "BUNDLE_B": int(items[j]["bid"]),
                "JOIN_DIST_M": rec["dist_m"],
                "ANGLE_DEG": rec["ang"],
                "PAIR_STATUS": "PAIRED",
                "PAIR_MARGIN": (
                    float(margin) if np.isfinite(margin) else np.nan
                ),
                "geometry": Point(
                    float(by_sid[sid].geometry.x),
                    float(by_sid[sid].geometry.y),
                ),
            })

    groups = defaultdict(list)
    for b in bundles:
        groups[uf.find(int(b["BUNDLE_ID"]))].append(int(b["BUNDLE_ID"]))

    track_lookup = {}
    for tid, bids in enumerate(
        sorted(groups.values(), key=lambda x: (-len(x), min(x))),
        1,
    ):
        for bid in bids:
            track_lookup[bid] = tid

    return track_lookup, rows_to_gdf(pair_rows, supports.crs)


def combined_references(client, kmz):
    refs = {}
    labels = {}
    for key, rec in client.items():
        if rec.get("geometry") is not None:
            refs[key] = rec["geometry"]
            labels[key] = rec.get("line_no") or key
    for key, rec in kmz.items():
        if key not in refs and rec.get("geometry") is not None:
            refs[key] = rec["geometry"]
            labels[key] = key
    return refs, labels


def assign_tracks_to_circuits(bundles, track_lookup, wires, client, kmz, unit_to_m):
    refs, labels = combined_references(client, kmz)
    by_track = defaultdict(list)
    for b in bundles:
        by_track[track_lookup[b["BUNDLE_ID"]]].append(b)

    rows, track_meta = [], {}
    for tid, bs in sorted(by_track.items()):
        samples = []
        for b in bs:
            # use all three phase candidates; circuit attribution is track-level, not phase-level
            for c in b["CANDIDATES"]:
                g = force_2d(c["geometry3d"])
                samples.extend([g.interpolate(f, normalized=True) for f in (0.2,0.5,0.8)])
        scores = []
        for key, ref in refs.items():
            ds = np.asarray([float(p.distance(ref))*unit_to_m for p in samples], dtype=float)
            if len(ds):
                scores.append((float(np.median(ds)), float(np.quantile(ds,0.9)), key))
        scores.sort()
        if not scores:
            best_key, best_d, second_d = "", None, None
        else:
            best_d, _, best_key = scores[0]
            second_d = scores[1][0] if len(scores)>1 else None
        margin = (second_d-best_d) if best_d is not None and second_d is not None else None
        conf = "HIGH" if best_d is not None and best_d <= 15 and (margin is None or margin >= 5) else (
            "MEDIUM" if best_d is not None and best_d <= 40 and (margin is None or margin >= 2) else "REVIEW"
        )
        label = client.get(best_key, {}).get("line_no") or labels.get(best_key, best_key)
        track_meta[tid] = {"CKEY":best_key, "CIRCUIT":label, "DIST_M":best_d,
                           "SECOND_M":second_d, "MARGIN_M":margin, "CONF":conf}
        rows.append({
            "TRACK_ID":tid, "N_BUNDLES":len(bs), "CKEY":best_key, "CIRCUIT":label,
            "DIST_M":best_d, "SECOND_M":second_d, "MARGIN_M":margin, "CONF":conf,
            "geometry": unary_union([b["geometry"] for b in bs]),
        })
        for b in bs:
            b.update({"TRACK_ID":tid, **track_meta[tid]})
    return track_meta, rows_to_gdf(rows, wires.crs)


# =============================================================================
# ONE FINAL SEQUENCE PER CIRCUIT
# =============================================================================

def edge_length_support(sa, sb, supports_by_id, unit_to_m):
    a = supports_by_id[sa].geometry; b = supports_by_id[sb].geometry
    return float(a.distance(b))*unit_to_m


def maximum_spanning_tree_edges(edges):
    """edges: list dict with U,V,WEIGHT,BUNDLE. Returns selected bundle ids."""
    nodes = sorted({e["U"] for e in edges} | {e["V"] for e in edges})
    idx = {n:i for i,n in enumerate(nodes)}
    uf = UnionFind(len(nodes))
    chosen = []
    for e in sorted(edges, key=lambda x:x["WEIGHT"], reverse=True):
        a,b = idx[e["U"]], idx[e["V"]]
        if uf.find(a) != uf.find(b):
            uf.union(a,b); chosen.append(e)
    return chosen


def tree_diameter_path(tree_edges):
    if not tree_edges:
        return [], 0.0
    adj = defaultdict(list)
    for e in tree_edges:
        adj[e["U"]].append((e["V"],e["WEIGHT"],e["BUNDLE_ID"]))
        adj[e["V"]].append((e["U"],e["WEIGHT"],e["BUNDLE_ID"]))

    # components; select component with largest weighted diameter
    seen_all = set(); best_path=[]; best_dist=-1.0
    def farthest(start):
        stack=[(start,None,0.0)]
        parent={start:(None,None)}; dist={start:0.0}
        while stack:
            u,pu,du=stack.pop()
            for v,w,bid in adj[u]:
                if v==pu: continue
                parent[v]=(u,bid); dist[v]=du+w; stack.append((v,u,du+w))
        end=max(dist,key=dist.get)
        return end,dist[end],parent,dist

    for root in list(adj):
        if root in seen_all: continue
        # collect component
        comp=set([root]); stack=[root]
        while stack:
            u=stack.pop()
            for v,_,_ in adj[u]:
                if v not in comp: comp.add(v); stack.append(v)
        seen_all |= comp
        a,_,_,_=farthest(root)
        b,d,parent,_=farthest(a)
        nodes=[b]; cur=b
        while cur!=a:
            cur=parent[cur][0]; nodes.append(cur)
        nodes.reverse()
        if d>best_dist:
            best_dist=d; best_path=nodes
    return best_path,best_dist


def bundle_by_pair_for_circuit(bundles, ckey):
    out=defaultdict(list)
    for b in bundles:
        if txt(b.get("CKEY"))==ckey:
            out[tuple(sorted((b["STRUCT_A"],b["STRUCT_B"])))].append(b)
    return out


def choose_best_parallel_bundle(candidates):
    # track confidence first, then lower attribution distance
    rank={"HIGH":0,"MEDIUM":1,"REVIEW":2}
    return min(candidates, key=lambda b:(rank.get(txt(b.get("CONF")),9),
                                         float(b.get("DIST_M")) if b.get("DIST_M") is not None else 1e99,
                                         int(b["BUNDLE_ID"])))



def _connected_edge_components(edges: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    if not edges:
        return []

    by_node = defaultdict(list)
    by_bid = {}
    for e in edges:
        bid = int(e["BUNDLE_ID"])
        by_bid[bid] = e
        by_node[e["U"]].append(bid)
        by_node[e["V"]].append(bid)

    components = []
    unseen = set(by_bid)
    while unseen:
        seed = next(iter(unseen))
        stack = [seed]
        bids = set()
        while stack:
            bid = stack.pop()
            if bid in bids:
                continue
            bids.add(bid)
            unseen.discard(bid)
            e = by_bid[bid]
            for node in (e["U"], e["V"]):
                for other in by_node[node]:
                    if other not in bids:
                        stack.append(other)
        components.append([by_bid[bid] for bid in sorted(bids)])
    return components


def _tree_component_segments(
    component_edges: list[dict[str, Any]],
    anchor_node: str,
) -> list[dict[str, Any]]:
    """Split one accepted tap tree into maximal non-branching segments."""
    if not component_edges:
        return []

    adj = defaultdict(list)
    for e in component_edges:
        bid = int(e["BUNDLE_ID"])
        adj[e["U"]].append((e["V"], bid))
        adj[e["V"]].append((e["U"], bid))

    depth = {anchor_node: 0}
    queue_nodes = [anchor_node]
    for u in queue_nodes:
        for v, _ in adj.get(u, []):
            if v not in depth:
                depth[v] = depth[u] + 1
                queue_nodes.append(v)

    visited = set()
    segments = []

    starts = sorted(
        set([anchor_node] + [n for n in adj if len(adj[n]) != 2]),
        key=lambda n: (depth.get(n, 10**9), txt(n)),
    )

    for start in starts:
        for nxt, bid in sorted(
            adj.get(start, []),
            key=lambda x: (depth.get(x[0], 10**9), int(x[1])),
        ):
            bid = int(bid)
            if bid in visited:
                continue

            nodes = [start, nxt]
            bids = [bid]
            visited.add(bid)
            cur = nxt

            while len(adj.get(cur, [])) == 2:
                opts = [
                    (v, int(ebid))
                    for v, ebid in adj[cur]
                    if int(ebid) not in visited
                ]
                if not opts:
                    break
                v, ebid = opts[0]
                visited.add(ebid)
                bids.append(ebid)
                nodes.append(v)
                cur = v

            if depth.get(nodes[-1], -1) < depth.get(nodes[0], -1):
                nodes.reverse()
                bids.reverse()

            segments.append({
                "NODES": nodes,
                "BUNDLES": bids,
            })

    for e in component_edges:
        bid = int(e["BUNDLE_ID"])
        if bid not in visited:
            segments.append({
                "NODES": [e["U"], e["V"]],
                "BUNDLES": [bid],
            })
            visited.add(bid)

    return segments


def _bundle_branch_confident(bundle: dict[str, Any]) -> bool:
    return txt(bundle.get("CONF")).upper() in {"HIGH", "MEDIUM"}


def build_circuit_paths(bundles, supports, unit_to_m):
    """Build MAIN circuit spine plus legitimate connected TAP_BRANCH paths.

    Pairing and circuit attribution remain upstream and unchanged. Off-spine
    tree components are retained only when they attach to MAIN at exactly one
    support and all bundles have HIGH/MEDIUM circuit confidence.
    """
    by_sid = support_lookup(supports)
    circuits = defaultdict(list)
    for b in bundles:
        if txt(b.get("CKEY")):
            circuits[txt(b["CKEY"])].append(b)

    result = {}
    dropped = []

    for ckey, bs in circuits.items():
        by_bid = {int(b["BUNDLE_ID"]): b for b in bs}

        pair_groups = defaultdict(list)
        for b in bs:
            pair_groups[
                tuple(sorted((b["STRUCT_A"], b["STRUCT_B"])))
            ].append(b)

        edges = []
        for pair, cands in pair_groups.items():
            winner = choose_best_parallel_bundle(cands)
            for x in cands:
                if x is not winner:
                    dropped.append({
                        "CKEY": ckey,
                        "CIRCUIT": txt(x.get("CIRCUIT")),
                        "BUNDLE_ID": int(x["BUNDLE_ID"]),
                        "PATH_ROLE": "DISCARDED",
                        "REASON": "DUPLICATE_PARALLEL_BUNDLE",
                        "geometry": x["geometry"],
                    })

            u, v = pair
            edges.append({
                "U": u,
                "V": v,
                "WEIGHT": edge_length_support(u, v, by_sid, unit_to_m),
                "BUNDLE_ID": int(winner["BUNDLE_ID"]),
            })

        tree = maximum_spanning_tree_edges(edges)
        tree_bids = {int(e["BUNDLE_ID"]) for e in tree}

        for e in edges:
            bid = int(e["BUNDLE_ID"])
            if bid not in tree_bids:
                b = by_bid[bid]
                dropped.append({
                    "CKEY": ckey,
                    "CIRCUIT": txt(b.get("CIRCUIT")),
                    "BUNDLE_ID": bid,
                    "PATH_ROLE": "DISCARDED",
                    "REASON": "ALTERNATE_OR_CYCLE_ROUTE",
                    "geometry": b["geometry"],
                })

        path_nodes, path_len = tree_diameter_path(tree)
        pair_to_bundle = {
            tuple(sorted((e["U"], e["V"]))): int(e["BUNDLE_ID"])
            for e in tree
        }
        path_bids = [
            pair_to_bundle[tuple(sorted((a, b)))]
            for a, b in zip(path_nodes[:-1], path_nodes[1:])
        ]

        main_nodes = set(path_nodes)
        main_bids = set(path_bids)
        off_main_tree = [
            e for e in tree
            if int(e["BUNDLE_ID"]) not in main_bids
        ]

        branches = []
        branch_counter = 0

        for comp in _connected_edge_components(off_main_tree):
            comp_nodes = {
                n for e in comp for n in (e["U"], e["V"])
            }
            anchors = sorted(comp_nodes & main_nodes)

            if len(anchors) == 0:
                reason = "DISCONNECTED_CIRCUIT_COMPONENT"
            elif len(anchors) != 1:
                reason = "MULTI_ANCHOR_ALTERNATE_ROUTE"
            else:
                comp_bundles = [
                    by_bid[int(e["BUNDLE_ID"])]
                    for e in comp
                ]
                if not all(
                    _bundle_branch_confident(b)
                    for b in comp_bundles
                ):
                    reason = "BRANCH_ATTRIBUTION_REVIEW"
                else:
                    reason = ""

            if reason:
                for e in comp:
                    b = by_bid[int(e["BUNDLE_ID"])]
                    dropped.append({
                        "CKEY": ckey,
                        "CIRCUIT": txt(b.get("CIRCUIT")),
                        "BUNDLE_ID": int(e["BUNDLE_ID"]),
                        "PATH_ROLE": "DISCARDED",
                        "REASON": reason,
                        "geometry": b["geometry"],
                    })
                continue

            anchor = anchors[0]
            branch_counter += 1
            branch_id = f"TAP{branch_counter:02d}"

            for seg_index, seg in enumerate(
                _tree_component_segments(comp, anchor),
                1,
            ):
                branches.append({
                    "PATH_ROLE": "TAP_BRANCH",
                    "BRANCH_ID": branch_id,
                    "SEGMENT_ID": f"{branch_id}_{seg_index:02d}",
                    "ANCHOR_NODE": anchor,
                    "NODES": list(seg["NODES"]),
                    "BUNDLES": list(seg["BUNDLES"]),
                })

        label = next(
            (
                txt(b.get("CIRCUIT"))
                for b in bs
                if txt(b.get("CIRCUIT"))
            ),
            ckey,
        )

        result[ckey] = {
            "CIRCUIT": label,
            "PATH_ROLE": "MAIN",
            "NODES": path_nodes,
            "BUNDLES": path_bids,
            "LENGTH_M": path_len,
            "BRANCHES": branches,
            "N_TAP_BRANCHES": len({
                b["BRANCH_ID"] for b in branches
            }),
            "N_TAP_SEGMENTS": len(branches),
            "N_TAP_SPANS": sum(
                len(b["BUNDLES"]) for b in branches
            ),
        }

    return result, rows_to_gdf(dropped, supports.crs)


def oriented_candidate(candidate, start_sid, end_sid, bundle, supports_by_id):
    coords=list(candidate["coords"])
    A=np.asarray([supports_by_id[start_sid].geometry.x,supports_by_id[start_sid].geometry.y])
    p0=np.asarray(coords[0][:2]); p1=np.asarray(coords[-1][:2])
    if np.linalg.norm(p0-A) <= np.linalg.norm(p1-A):
        out=coords
        off_start=candidate["OFF_A_M"] if bundle["STRUCT_A"]==start_sid else candidate["OFF_B_M"]
        off_end=candidate["OFF_B_M"] if bundle["STRUCT_B"]==end_sid else candidate["OFF_A_M"]
    else:
        out=list(reversed(coords))
        off_start=candidate["OFF_B_M"] if bundle["STRUCT_B"]==start_sid else candidate["OFF_A_M"]
        off_end=candidate["OFF_A_M"] if bundle["STRUCT_A"]==end_sid else candidate["OFF_B_M"]
    return out,float(off_start),float(off_end)


def select_phase_sequence(path, bundle_lookup, supports, unit_to_m):
    """Dynamic programming across a circuit path.

    Horizontal bundle: central phase has a strong local preference.
    Vertical/mixed bundle: continuity dominates; outermost is a weak tie-break.
    """
    by_sid=support_lookup(supports)
    states=[]
    for i,bid in enumerate(path["BUNDLES"]):
        b=bundle_lookup[bid]
        s0,s1=path["NODES"][i],path["NODES"][i+1]
        cand=[]
        for c in b["CANDIDATES"]:
            coords,off0,off1=oriented_candidate(c,s0,s1,b,by_sid)
            if b["ARRANGEMENT"]=="HORIZONTAL":
                local=8.0*float(c["CENTRAL_SCORE"])
            else:
                local=0.35*float(c["OUTER_PENALTY"])
            cand.append({**c,"coords_oriented":coords,"OFF_START_M":off0,"OFF_END_M":off1,"LOCAL_COST":local})
        states.append(cand)

    if not states:
        return [],[]
    costs=[np.asarray([c["LOCAL_COST"] for c in states[0]],dtype=float)]
    back=[]
    join_mats=[]
    for i in range(1,len(states)):
        prev,curr=states[i-1],states[i]
        cur_cost=np.full(len(curr),np.inf); cur_back=np.full(len(curr),-1,dtype=int)
        join_mat=np.zeros((len(prev),len(curr)),dtype=float)
        for a,p in enumerate(prev):
            pxy=np.asarray(p["coords_oriented"][-1][:2])
            for b,c in enumerate(curr):
                cxy=np.asarray(c["coords_oriented"][0][:2])
                j=float(np.linalg.norm(pxy-cxy))*unit_to_m
                join_mat[a,b]=j
                val=costs[-1][a]+j+float(c["LOCAL_COST"])
                if val<cur_cost[b]: cur_cost[b]=val;cur_back[b]=a
        costs.append(cur_cost);back.append(cur_back);join_mats.append(join_mat)
    idx=int(np.argmin(costs[-1])); chosen=[idx]
    for b in reversed(back):
        idx=int(b[idx]);chosen.append(idx)
    chosen.reverse()
    selected=[states[i][chosen[i]] for i in range(len(states))]
    joins=[]
    for i in range(1,len(selected)):
        a=np.asarray(selected[i-1]["coords_oriented"][-1][:2]);b=np.asarray(selected[i]["coords_oriented"][0][:2])
        joins.append(float(np.linalg.norm(a-b))*unit_to_m)
    return selected,joins



def simplify_selected_phase_sequence(selected, tolerance_source: float):
    """Douglas-Peucker simplify each selected source span while locking endpoints.

    The source conductor remains the planimetric truth; this only removes redundant
    intermediate vertices. Because every span is simplified independently, all
    structure/join endpoints remain exact and the delivered Structure-on-Wire
    invariant is preserved.
    """
    if not selected:
        return selected, 0, 0
    tol = max(float(tolerance_source), 0.0)
    out = []
    before = 0
    after = 0
    for c in selected:
        rec = dict(c)
        coords = list(c.get("coords_oriented", []))
        before += len(coords)
        if tol <= 0.0 or len(coords) <= 2:
            rec["coords_oriented"] = coords
            after += len(coords)
            out.append(rec)
            continue
        line = LineString([(float(x[0]), float(x[1])) for x in coords])
        simp = line.simplify(tol, preserve_topology=False)
        simp_coords = list(simp.coords) if simp is not None and not simp.is_empty else []
        if len(simp_coords) < 2:
            simp_coords = [(float(coords[0][0]), float(coords[0][1])),
                           (float(coords[-1][0]), float(coords[-1][1]))]
        # Lock source span endpoints exactly.
        simp_coords[0] = (float(coords[0][0]), float(coords[0][1]))
        simp_coords[-1] = (float(coords[-1][0]), float(coords[-1][1]))
        rec["coords_oriented"] = simp_coords
        after += len(simp_coords)
        out.append(rec)
    return out, before, after

def concatenate_selected(selected):
    if not selected:
        return None
    out=[(float(c[0]),float(c[1])) for c in selected[0]["coords_oriented"]]
    for c in selected[1:]:
        pts=[(float(x[0]),float(x[1])) for x in c["coords_oriented"]]
        # Appending the next source endpoint creates only the minimal structure
        # connection required to make one circuit polyline. No intermediate
        # synthetic alignment/centreline geometry is introduced.
        out.extend(pts)
    clean=[out[0]]
    for p in out[1:]:
        if abs(p[0]-clean[-1][0])>1e-9 or abs(p[1]-clean[-1][1])>1e-9:
            clean.append(p)
    return LineString(clean) if len(clean)>=2 else None


def circuit_attributes(ckey, circuit, client, kmz):
    crec=client.get(ckey);krec=kmz.get(ckey)
    sv=crec.get("voltages",[]) if crec else []
    kv=krec.get("voltages",[]) if krec else []
    voltage=sv[0] if len(sv)==1 else (kv[0] if len(kv)==1 else "")
    companies=crec.get("companies",[]) if crec and crec.get("companies") else (krec.get("companies",[]) if krec else [])
    gids=crec.get("globalids",[]) if crec and crec.get("globalids") else (krec.get("globalids",[]) if krec else [])
    return voltage,(companies[0] if companies else ""),(gids[0] if gids else "")


def choose_row_for_geom(geom,krec,unit_to_m):
    if not krec or not krec.get("rows"):
        return "",None
    p=midpoint(geom)
    scores=sorted((float(p.distance(ref))*unit_to_m,row) for row,ref in krec["rows"].items())
    return (scores[0][1],scores[0][0]) if scores else ("",None)


def choose_row_for_point(point,krec,unit_to_m):
    if not krec or not krec.get("rows"):
        return "",None
    scores=sorted((float(point.distance(ref))*unit_to_m,row) for row,ref in krec["rows"].items())
    return (scores[0][1],scores[0][0]) if scores else ("",None)


# =============================================================================
# FINAL PRODUCTS
# =============================================================================

def structure_members_by_topology(raw_points: gpd.GeoDataFrame) -> dict[str, list[pd.Series]]:
    """Return original contractor top markers grouped by internal physical support ID."""
    out: dict[str, list[pd.Series]] = defaultdict(list)
    if raw_points is None or raw_points.empty or "TOPO_ID" not in raw_points.columns:
        return out
    for _, r in raw_points.iterrows():
        sid = txt(r.get("TOPO_ID"))
        if sid:
            out[sid].append(r)
    return out


def local_circuit_node_geometry(selected: list[dict[str, Any]], node_index: int):
    """Geometry of the FINAL circuit line exactly at one topology node.

    At an internal structure the final circuit line contains the direct join between
    the previous selected source-wire endpoint and the next selected source-wire
    endpoint. At the two circuit ends the structure is the corresponding final
    source-wire endpoint. This guarantees that delivered Structure XY lies on the
    delivered Circuit wire and does not use a cluster centroid.
    """
    n = len(selected)
    if n == 0:
        return None, "NO_SELECTED_WIRE"
    if node_index <= 0:
        p = selected[0]["coords_oriented"][0]
        return Point(float(p[0]), float(p[1])), "START_ENDPOINT"
    if node_index >= n:
        p = selected[-1]["coords_oriented"][-1]
        return Point(float(p[0]), float(p[1])), "END_ENDPOINT"

    a = selected[node_index - 1]["coords_oriented"][-1]
    b = selected[node_index]["coords_oriented"][0]
    pa = Point(float(a[0]), float(a[1]))
    pb = Point(float(b[0]), float(b[1]))
    if pa.distance(pb) <= 1e-9:
        return pa, "COMMON_ENDPOINT"
    return LineString([(pa.x, pa.y), (pb.x, pb.y)]), "JOIN_SEGMENT"


def choose_circuit_structure_xy(
    sid: str,
    selected: list[dict[str, Any]],
    node_index: int,
    member_lookup: dict[str, list[pd.Series]],
    support_row: pd.Series,
    unit_to_m: float,
):
    """Choose contractor-derived Structure XY.

    Normal case: choose the original contractor top marker nearest THIS circuit, then
    project it onto the local final circuit wire.

    Three-pole case: when exactly three distinct contractor top markers describe one
    physical support, deliver the actual MIDDLE pole position. The middle pole is the
    source marker nearest the three-point centroid (equivalent to the median pole for
    the usual near-collinear three-pole arrangement). It is deliberately NOT snapped
    to the 2D wire: Structure represents physical structure position while Wires
    represents the selected flattened conductor.

    Client/source structure geometry never moves the delivered point.
    """
    local_geom, method = local_circuit_node_geometry(selected, node_index)
    members = member_lookup.get(sid, [])

    top_rule = ""
    if len(members) == 3:
        xs = np.asarray([float(r.geometry.x) for r in members], dtype=float)
        ys = np.asarray([float(r.geometry.y) for r in members], dtype=float)
        centre = np.asarray([float(xs.mean()), float(ys.mean())], dtype=float)
        chosen = min(
            members,
            key=lambda r: float(np.hypot(float(r.geometry.x) - centre[0],
                                         float(r.geometry.y) - centre[1]))
        )
        top_pt = chosen.geometry
        top_id = txt(chosen.get("POINT_ID"))
        top_z = float(top_pt.z) if getattr(top_pt, "has_z", False) else float(chosen.get("Z", np.nan))
        top_rule = "THREE_POLE_MIDDLE"
        method = "PHYSICAL_MIDDLE_POLE"
        # By design the Structure point can be offset from the 2D wire.
        final_pt = Point(float(top_pt.x), float(top_pt.y))
        snap_m = 0.0
    elif members:
        if local_geom is None:
            chosen = members[0]
        else:
            chosen = min(members, key=lambda r: r.geometry.distance(local_geom))
        top_pt = chosen.geometry
        top_id = txt(chosen.get("POINT_ID"))
        top_z = float(top_pt.z) if getattr(top_pt, "has_z", False) else float(chosen.get("Z", np.nan))
        top_rule = "NEAREST_TOP_TO_CIRCUIT"

        if local_geom is None:
            final_pt = Point(float(top_pt.x), float(top_pt.y))
        elif local_geom.geom_type == "Point":
            final_pt = Point(float(local_geom.x), float(local_geom.y))
        else:
            final_pt = local_geom.interpolate(local_geom.project(Point(float(top_pt.x), float(top_pt.y))))
        snap_m = float(Point(float(top_pt.x), float(top_pt.y)).distance(final_pt)) * unit_to_m
    else:
        # Internal topology fallback only. Rare and explicitly flagged.
        top_pt = support_row.geometry
        top_id = ""
        top_z = float(top_pt.z) if getattr(top_pt, "has_z", False) else float(support_row.get("TOP_Z", np.nan))
        method = "TOPOLOGY_FALLBACK_" + method
        top_rule = "TOPOLOGY_FALLBACK"

        if local_geom is None:
            final_pt = Point(float(top_pt.x), float(top_pt.y))
        elif local_geom.geom_type == "Point":
            final_pt = Point(float(local_geom.x), float(local_geom.y))
        else:
            final_pt = local_geom.interpolate(local_geom.project(Point(float(top_pt.x), float(top_pt.y))))
        snap_m = float(Point(float(top_pt.x), float(top_pt.y)).distance(final_pt)) * unit_to_m

    wire_dist_m = (
        float(final_pt.distance(local_geom)) * unit_to_m
        if local_geom is not None and not local_geom.is_empty else np.nan
    )

    return {
        "geometry": final_pt,
        "TOP_POINT_ID": top_id,
        "TOP_RULE": top_rule,
        "TOP_X": float(top_pt.x),
        "TOP_Y": float(top_pt.y),
        "TOP_Z": top_z,
        "WIRE_SNAP_M": snap_m,
        "WIRE_DIST_M": wire_dist_m,
        "WIRE_LOC": method,
        "N_TOP_CAND": int(len(members)),
    }


def build_final_products(paths,bundles,supports,raw_points,client,kmz,client_structure_index,unit_to_m,max_join_review_m,wire_simplify_m):
    blookup={int(b["BUNDLE_ID"]):b for b in bundles}
    by_sid=support_lookup(supports)
    member_lookup=structure_members_by_topology(raw_points)
    wire_rows=[];span_rows=[];structure_rows=[];discard_phase=[];join_rows=[]

    for ckey,path in sorted(paths.items(),key=lambda kv:kv[1]["CIRCUIT"]):
        selected,joins=select_phase_sequence(path,blookup,supports,unit_to_m)
        if not selected:
            continue
        selected,nodes_before,nodes_after=simplify_selected_phase_sequence(
            selected, float(wire_simplify_m)/unit_to_m
        )
        circuit=path["CIRCUIT"]
        voltage,company,gid=circuit_attributes(ckey,circuit,client,kmz)
        krec=kmz.get(ckey)
        full_geom=concatenate_selected(selected)
        if full_geom is None:
            continue

        row_names=[]
        for i,(bid,sel) in enumerate(zip(path["BUNDLES"],selected)):
            b=blookup[bid]
            s0,s1=path["NODES"][i],path["NODES"][i+1]
            geom2=LineString([(float(x[0]),float(x[1])) for x in sel["coords_oriented"]])
            row_name,row_dist=choose_row_for_geom(geom2,krec,unit_to_m)
            if row_name: row_names.append(row_name)
            span_rows.append({
                "ROW":row_name,"CIRCUIT":circuit,"VOLTAGE":voltage,"COMPANY":company,"GLOBALID":gid,
                "SEQ":i+1,"BUNDLE_ID":bid,"STRUCT_A":s0,"STRUCT_B":s1,
                "WIRE_ID":sel["WIRE_ID"],"ARRANGEMENT":b["ARRANGEMENT"],
                "SELECT_METHOD":"HORIZONTAL_CENTRAL" if b["ARRANGEMENT"]=="HORIZONTAL" else "VERTICAL_CONTINUITY",
                "ROW_DIST_M":row_dist,"LENGTH_M":float(geom2.length)*unit_to_m,"geometry":geom2,
            })
            for cand in b["CANDIDATES"]:
                if int(cand["WIRE_ROW"])!=int(sel["WIRE_ROW"]):
                    discard_phase.append({
                        "CIRCUIT":circuit,"BUNDLE_ID":bid,"WIRE_ID":cand["WIRE_ID"],
                        "REASON":"NON_SELECTED_PHASE","geometry":cand["geometry3d"],
                    })
            if i>0:
                j=joins[i-1]
                join_rows.append({
                    "CIRCUIT":circuit,"AT_STRUCTURE":s0,"JOIN_M":j,
                    "REVIEW":int(j>max_join_review_m),
                    "geometry":Point(float(by_sid[s0].geometry.x),float(by_sid[s0].geometry.y)),
                })

        rows_unique=";".join(sorted(set(row_names)))
        wire_rows.append({
            "ROW":rows_unique,"CIRCUIT":circuit,"VOLTAGE":voltage,"COMPANY":company,"GLOBALID":gid,
            "N_SPANS":len(selected),
            "LENGTH_M":float(full_geom.length)*unit_to_m,
            "LENGTH_FT":float(full_geom.length)*unit_to_m/0.30480060960121924,
            "NODES_BEFORE":int(nodes_before),"NODES_AFTER":int(nodes_after),
            "MAX_JOIN_M":max(joins) if joins else 0.0,"geometry":full_geom,
        })

        # One structure feature per circuit/node. Geometry is derived from the
        # contractor top marker + THIS circuit's final wire, never from a cluster
        # centroid and never from client/source geometry.
        for node_index,sid in enumerate(path["NODES"]):
            s=by_sid[sid]
            loc=choose_circuit_structure_xy(
                sid,selected,node_index,member_lookup,s,unit_to_m
            )
            p2=loc["geometry"]
            row_name,row_dist=choose_row_for_point(p2,krec,unit_to_m)
            src_row,src_dist,src_method=match_client_structure(
                p2,circuit,client_structure_index,unit_to_m
            )
            sval=client_structure_values(src_row,sid)
            structure_rows.append({
                "ROW":row_name,"CIRCUIT":circuit,"VOLTAGE":voltage,"COMPANY":company,
                "TOPO_ID":sid,
                **sval,
                "SRC_DIST_M":src_dist,"SRC_MATCH":src_method,
                "ROW_DIST_M":row_dist,
                "TOP_POINT":loc["TOP_POINT_ID"],"TOP_RULE":loc["TOP_RULE"],
                "TOP_X":loc["TOP_X"],"TOP_Y":loc["TOP_Y"],"TOP_Z":loc["TOP_Z"],
                "WIRE_SNAP_M":loc["WIRE_SNAP_M"],"WIRE_DIST_M":loc["WIRE_DIST_M"],"WIRE_LOC":loc["WIRE_LOC"],
                "N_TOP_CAND":loc["N_TOP_CAND"],
                "GROUND_Z":np.nan,"Z_SOURCE":"PENDING_LIDAR",
                "geometry":p2,
            })

    return (rows_to_gdf(wire_rows,supports.crs), rows_to_gdf(span_rows,supports.crs),
            rows_to_gdf(structure_rows,supports.crs), rows_to_gdf(discard_phase,supports.crs),
            rows_to_gdf(join_rows,supports.crs))


def dissolve_row_wires(span_edges):
    rows=[]
    for (row,circuit,voltage,company),sg in span_edges.groupby(["ROW","CIRCUIT","VOLTAGE","COMPANY"],dropna=False,sort=False):
        if not txt(row): continue
        u=unary_union(list(sg.geometry))
        try:
            g=linemerge(u) if u.geom_type in {"MultiLineString","GeometryCollection"} else u
        except Exception:
            g=u
        rows.append({"ROW":txt(row),"CIRCUIT":txt(circuit),"VOLTAGE":txt(voltage),"COMPANY":txt(company),
                     "N_SPANS":len(sg),"geometry":g})
    return rows_to_gdf(rows,span_edges.crs)


def build_structure_source_match_lines(final_structures: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    rows=[]
    for _,r in final_structures.iterrows():
        if not (pd.notna(r.get("SRC_X")) and pd.notna(r.get("SRC_Y"))):
            continue
        a=(float(r.geometry.x),float(r.geometry.y))
        b=(float(r["SRC_X"]),float(r["SRC_Y"]))
        rows.append({
            "ROW":txt(r.get("ROW")),"CIRCUIT":txt(r.get("CIRCUIT")),
            "TOPO_ID":txt(r.get("TOPO_ID")),"STRUCT_ID":txt(r.get("STRUCT_ID")),
            "SRC_DIST_M":r.get("SRC_DIST_M"),"SRC_MATCH":txt(r.get("SRC_MATCH")),
            "geometry":LineString([a,b]),
        })
    return rows_to_gdf(rows,final_structures.crs)


def _remove_shapefile(path: Path) -> None:
    for f in path.parent.glob(path.stem + ".*"):
        if f.is_file():
            try:
                f.unlink()
            except Exception:
                pass


def write_circuit_shapefiles(final_wires, final_structures, year, outroot):
    """Write only the circuit-delivery Wires and Structures products."""
    cdir = outroot / "circuit"
    cdir.mkdir(parents=True, exist_ok=True)
    for old in cdir.iterdir():
        if old.is_file():
            old.unlink()

    for _, r in final_wires.iterrows():
        circuit = txt(r["CIRCUIT"])
        g = gpd.GeoDataFrame([r.to_dict()], geometry="geometry", crs=final_wires.crs)
        cols = [c for c in ["ROW", "CIRCUIT", "VOLTAGE", "COMPANY", "LENGTH_FT", "geometry"] if c in g.columns]
        out = cdir / f"{safe_filename(circuit)}_Wires_{year}.shp"
        _remove_shapefile(out)
        g[cols].to_file(out, driver="ESRI Shapefile", engine="pyogrio", index=False)

    for circuit, sg in final_structures.groupby("CIRCUIT", sort=False):
        cols = [c for c in [
            "ROW", "CIRCUIT", "VOLTAGE", "COMPANY", "STRUCT_ID", "GIS_NO", "STR_TYPE",
            "MATERIAL", "geometry"
        ] if c in sg.columns]
        out = cdir / f"{safe_filename(circuit)}_Structure_{year}.shp"
        _remove_shapefile(out)
        sg[cols].to_file(out, driver="ESRI Shapefile", engine="pyogrio", index=False)


def _extract_zip_to(zip_path: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest)
    return dest


def _find_file_gdb(root: Path) -> list[Path]:
    if root.is_dir() and root.suffix.lower() == ".gdb":
        return [root]
    if not root.exists() or not root.is_dir():
        return []
    return sorted([p for p in root.rglob("*.gdb") if p.is_dir()])


def resolve_management_units_gdb(
    explicit_source: Path | None,
    client_source: Path,
    temp_dir: Path,
) -> Path:
    """Resolve TVM_MUs.gdb without introducing another mandatory input.

    Priority:
      1. explicit --management-units path;
      2. FileGDB placed under package source/;
      3. FileGDB embedded in the client source ZIP/folder.
    """
    search_roots = []

    if explicit_source is not None:
        if explicit_source.is_dir():
            search_roots.append(explicit_source)
        elif explicit_source.is_file() and explicit_source.suffix.lower() == ".zip":
            search_roots.append(_extract_zip_to(explicit_source, temp_dir / "mu_explicit"))
        else:
            raise RuntimeError("Management Units source must be a .gdb folder or ZIP containing a .gdb")
    else:
        # Only fall back to the client source when no explicit/package GDB was found.
        if client_source.is_dir():
            search_roots.append(client_source)
        elif client_source.is_file() and client_source.suffix.lower() == ".zip":
            search_roots.append(_extract_zip_to(client_source, temp_dir / "mu_client_source"))

    candidates = []
    for root in search_roots:
        candidates.extend(_find_file_gdb(root))

    # Prefer the project-specific TVM_MUs.gdb when other geodatabases exist.
    preferred = [p for p in candidates if p.name.lower() == "tvm_mus.gdb"]
    candidates = preferred or candidates
    uniq = []
    seen = set()
    for p in candidates:
        rp = str(p.resolve())
        if rp not in seen:
            uniq.append(p)
            seen.add(rp)

    if len(uniq) != 1:
        raise RuntimeError(
            "Could not uniquely resolve Management Units FileGDB. "
            f"Found {len(uniq)} candidate(s). Put TVM_MUs.gdb under source/ "
            "or provide --management-units."
        )
    return uniq[0]


def _mu_row_base(mu_id: str) -> str:
    # Client MU IDs encode the RoW followed by the MU sequence number, e.g.
    # EMA-13-13A-42 -> EMA-13-13A; CT-01-235 -> CT-01.
    return re.sub(r"-\d+$", "", txt(mu_id))


def load_management_units(
    gdb_path: Path,
    target_crs,
    kmz_refs,
    client_name: str,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Read all *_MUs layers, preserve client attributes and map MU_ID -> SoW RoW."""
    row_names = sorted({txt(r.row) for r in kmz_refs if txt(r.row)})
    row_lookup = {row_key(r): r for r in row_names}
    frames = []
    review = []

    for layer_name, geom_type in pyogrio.list_layers(gdb_path):
        if "polygon" not in txt(geom_type).lower():
            continue
        if not re.search(r"(?:^|_)MUs?$", txt(layer_name), re.I):
            continue
        g = gpd.read_file(gdb_path, layer=layer_name, engine="pyogrio")
        if g.empty:
            continue
        if g.crs is None:
            raise RuntimeError(f"Management Unit layer has no CRS: {layer_name}")
        if "MU_ID" not in g.columns:
            raise RuntimeError(f"Management Unit layer {layer_name} does not contain MU_ID")
        if CRS.from_user_input(g.crs) != CRS.from_user_input(target_crs):
            g = g.to_crs(target_crs)

        g = g.copy()
        g["MU_LAYER"] = layer_name
        g["MU_ROW_RAW"] = g["MU_ID"].apply(_mu_row_base)
        g["ROW"] = g["MU_ROW_RAW"].apply(lambda v: row_lookup.get(row_key(v), ""))
        g["CLIENT"] = client_name
        for _, r in g[g["ROW"] == ""].iterrows():
            review.append({
                "MU_ID": txt(r.get("MU_ID")),
                "MU_LAYER": layer_name,
                "MU_ROW_RAW": txt(r.get("MU_ROW_RAW")),
                "REASON": "NO_KMZ_ROW_MATCH",
            })
        # Safe fallback keeps the feature usable if a client MU exists outside current KMZ scope.
        g.loc[g["ROW"] == "", "ROW"] = g.loc[g["ROW"] == "", "MU_ROW_RAW"].str.replace("-", "_", regex=False)
        frames.append(g)

    if not frames:
        raise RuntimeError(f"No polygon Management Unit layers found in {gdb_path}")

    out = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs=target_crs)
    return out, pd.DataFrame(review)



def management_unit_duplicate_review(mus: gpd.GeoDataFrame) -> pd.DataFrame:
    """Report duplicate client ID values without changing source attribution."""
    rows = []
    if mus is None or mus.empty or "ID" not in mus.columns:
        return pd.DataFrame(rows)
    layer_col = "MU_LAYER" if "MU_LAYER" in mus.columns else None
    if layer_col:
        for layer, sg in mus.groupby(layer_col, dropna=False, sort=False):
            mask = sg["ID"].notna() & sg["ID"].duplicated(keep=False)
            for _, r in sg[mask].iterrows():
                rows.append({
                    "SCOPE": "WITHIN_LAYER", "MU_LAYER": txt(layer), "ID": r.get("ID"),
                    "MU_ID": txt(r.get("MU_ID")), "ROW": txt(r.get("ROW")),
                })
    mask = mus["ID"].notna() & mus["ID"].duplicated(keep=False)
    for _, r in mus[mask].iterrows():
        rows.append({
            "SCOPE": "GLOBAL", "MU_LAYER": txt(r.get("MU_LAYER")), "ID": r.get("ID"),
            "MU_ID": txt(r.get("MU_ID")), "ROW": txt(r.get("ROW")),
        })
    return pd.DataFrame(rows).drop_duplicates().reset_index(drop=True)

def _parse_tile_origin(path: Path) -> tuple[float, float] | None:
    """Extract the last coordinate-like X_Y pair from a source or delivery filename."""
    matches = re.findall(r"(?<!\d)(-?\d{4,}(?:\.\d+)?)_(-?\d{4,}(?:\.\d+)?)(?!\d)", path.stem)
    if not matches:
        return None
    x, y = matches[-1]
    return float(x), float(y)


def _coord_token(v: float) -> str:
    x = float(v)
    if abs(x - round(x)) <= 1e-6:
        return str(int(round(x)))
    return f"{x:.3f}".rstrip("0").rstrip(".")


def _tile_ref(origin: tuple[float, float]) -> str:
    return f"{_coord_token(origin[0])}_{_coord_token(origin[1])}"


def _min_positive_step(values: list[float]) -> float | None:
    u = sorted(set(values))
    diffs = [b - a for a, b in zip(u[:-1], u[1:]) if b - a > 1e-9]
    return float(min(diffs)) if diffs else None


def _grid_origin_from_header(minx: float, miny: float, tile_size: float | None) -> tuple[float, float]:
    if tile_size is None or tile_size <= 0:
        return float(minx), float(miny)
    return (math.floor(float(minx) / float(tile_size)) * float(tile_size),
            math.floor(float(miny) / float(tile_size)) * float(tile_size))


def build_lidar_tile_footprints(
    lidar_source: Path,
    project_crs,
    temp_dir: Path,
    capture_year: int,
    tile_size_override: float | None = None,
) -> tuple[gpd.GeoDataFrame, float | None]:
    """Create validated physical tile polygons and stable Tile Refs.

    Contractor filenames are treated as source metadata only. A coordinate pair in
    the filename is validated against LAS/LAZ header bounds. If it is unavailable or
    materially inconsistent, a nominal grid origin is derived from the header.
    """
    if laspy is None:
        raise RuntimeError("Tile Index generation requires laspy + lazrs")

    files = discover_lidar_files(lidar_source, temp_dir)
    pcrs = CRS.from_user_input(project_crs)
    meta = []
    xs, ys = [], []

    for path in files:
        origin = _parse_tile_origin(path)
        if origin:
            xs.append(origin[0]); ys.append(origin[1])
        try:
            with laspy.open(path) as fh:
                h = fh.header
                lcrs = _las_header_crs(h)
                if lcrs is not None and not _horizontal_crs_matches(lcrs, pcrs):
                    raise RuntimeError(
                        f"LiDAR horizontal CRS differs from project CRS for {path.name}: "
                        f"{_horizontal_crs_label(lcrs)} vs {_horizontal_crs_label(pcrs)}"
                    )
                bounds = (float(h.mins[0]), float(h.mins[1]), float(h.maxs[0]), float(h.maxs[1]))
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed reading LiDAR header {path}: {exc}") from exc
        meta.append((path, origin, bounds))

    inferred = tile_size_override
    if inferred is None:
        dx, dy = _min_positive_step(xs), _min_positive_step(ys)
        steps = [v for v in (dx, dy) if v is not None and v > 0]
        if steps:
            inferred = min(steps)
        else:
            dims = []
            for _, _, (x0, y0, x1, y1) in meta:
                if x1 > x0: dims.append(x1 - x0)
                if y1 > y0: dims.append(y1 - y0)
            inferred = float(np.median(dims)) if dims else None

    rows = []
    for path, origin, bounds in meta:
        x0h, y0h, x1h, y1h = bounds
        validation = "OK"
        origin_source = "FILENAME"
        chosen_origin = origin

        if chosen_origin is None:
            chosen_origin = _grid_origin_from_header(x0h, y0h, inferred)
            origin_source = "HEADER_GRID"
            validation = "NO_FILENAME_TILE_REF"
        elif inferred is not None and inferred > 0:
            nx0, ny0 = chosen_origin
            nx1, ny1 = nx0 + float(inferred), ny0 + float(inferred)
            tol = max(float(inferred) * 0.02, 1.0)
            if x0h < nx0 - tol or y0h < ny0 - tol or x1h > nx1 + tol or y1h > ny1 + tol:
                chosen_origin = _grid_origin_from_header(x0h, y0h, inferred)
                origin_source = "HEADER_GRID"
                validation = "HEADER_OUTSIDE_NAMED_TILE"

        tile_ref = _tile_ref(chosen_origin)
        if inferred is not None and inferred > 0:
            x0, y0 = chosen_origin
            x1, y1 = x0 + float(inferred), y0 + float(inferred)
            geom = box(x0, y0, x1, y1)
            index_src = "NOMINAL_GRID"
        else:
            geom = box(x0h, y0h, x1h, y1h)
            index_src = "LAS_HEADER"

        rows.append({
            "SOURCE_FILE": path.name,
            "SOURCE_PATH": str(path),
            "SOURCE_EXT": path.suffix.lower(),
            "TILE_REF": tile_ref,
            "TILE_REF_SRC": origin_source,
            "NAME_STATUS": validation,
            "CAP_YEAR": int(capture_year),
            "INDEX_SRC": index_src,
            "HDR_MINX": x0h, "HDR_MINY": y0h, "HDR_MAXX": x1h, "HDR_MAXY": y1h,
            "geometry": geom,
        })

    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=project_crs)
    dup = out[out["TILE_REF"].duplicated(keep=False)]
    if not dup.empty:
        vals = ", ".join(sorted(set(dup["TILE_REF"].astype(str))))
        raise RuntimeError(f"Duplicate LiDAR Tile Ref(s) after filename/header validation: {vals}")
    return out, inferred


def assess_complete_rows(
    mus: gpd.GeoDataFrame,
    physical_tiles: gpd.GeoDataFrame,
    min_area_coverage: float = 0.99,
) -> pd.DataFrame:
    """Classify RoWs as FULL or PARTIAL for the current LiDAR package."""
    if mus.empty:
        raise RuntimeError("Cannot assess RoW completeness: Management Units are empty")
    if physical_tiles.empty:
        raise RuntimeError("Cannot assess RoW completeness: LiDAR tile index is empty")

    tile_union = unary_union([g for g in physical_tiles.geometry if g is not None and not g.is_empty])
    if tile_union is None or tile_union.is_empty:
        raise RuntimeError("Cannot assess RoW completeness: LiDAR tile footprint union is empty")

    rows = []
    for row, sg in mus[mus["ROW"].apply(bool)].groupby("ROW", sort=False):
        valid = sg[sg.geometry.notna() & ~sg.geometry.is_empty].copy()
        if valid.empty:
            continue
        total_mus = int(len(valid))
        touched_mask = valid.geometry.intersects(tile_union)
        touched_mus = int(touched_mask.sum())
        row_union = unary_union(list(valid.geometry))
        total_area = float(row_union.area) if row_union is not None and not row_union.is_empty else 0.0
        covered_area = float(row_union.intersection(tile_union).area) if total_area > 0 else 0.0
        area_ratio = covered_area / total_area if total_area > 0 else 0.0
        tile_hits = int(physical_tiles.geometry.intersects(row_union).sum()) if row_union is not None and not row_union.is_empty else 0
        complete = bool(touched_mus == total_mus and area_ratio >= float(min_area_coverage))
        rows.append({
            "ROW": txt(row), "STATUS": "FULL" if complete else "PARTIAL",
            "N_MUS_TOTAL": total_mus, "N_MUS_TOUCHED": touched_mus,
            "MU_TOUCH_RATIO": (touched_mus / total_mus) if total_mus else 0.0,
            "AREA_COVERAGE": area_ratio, "N_TILES": tile_hits,
            "MIN_AREA_REQ": float(min_area_coverage),
        })
    return pd.DataFrame(rows).sort_values(["STATUS", "ROW"], ascending=[True, True]).reset_index(drop=True)


def _delivery_extension(source_ext: str, output_format: str) -> str:
    fmt = txt(output_format).lower() or "same"
    if fmt == "same":
        ext = txt(source_ext).lower()
        return ext if ext in {".las", ".laz"} else ".laz"
    return "." + fmt


def build_delivery_lidar_name(row: str, tile_ref: str, year: int, source_ext: str, output_format: str) -> str:
    ext = _delivery_extension(source_ext, output_format)
    return f"{safe_filename(row)}_{safe_filename(tile_ref)}_LAS_{int(year)}{ext}"


def assign_tiles_to_delivery_rows(
    physical_tiles: gpd.GeoDataFrame,
    mus: gpd.GeoDataFrame,
    complete_rows: set[str],
    year: int,
    multi_row_threshold: float = 0.99,
    lidar_output_format: str = "same",
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Assign every physical tile to FULL delivery RoW(s) by tile-area coverage.

    Normal case: the RoW with the largest fraction of tile area wins and secondary
    overlaps are ignored. If two or more FULL RoWs each cover at least the configured
    threshold (default 99%) of the tile, create one delivery assignment per qualifying
    RoW. Coverage is always measured as intersection area / TILE area.
    """
    eligible = mus[mus["ROW"].isin(complete_rows) & mus.geometry.notna() & ~mus.geometry.is_empty][["ROW", "geometry"]].copy()
    if eligible.empty:
        raise RuntimeError("No Management Unit geometry for FULL RoWs")
    row_areas = eligible.dissolve(by="ROW", as_index=False)
    row_areas = row_areas[row_areas.geometry.notna() & ~row_areas.geometry.is_empty].reset_index(drop=True)

    rows = []
    review = []
    try:
        sidx = row_areas.sindex
    except Exception:
        sidx = None

    for _, tr in physical_tiles.reset_index(drop=True).iterrows():
        geom = tr.geometry
        if geom is None or geom.is_empty or geom.area <= 0:
            review.append({"SOURCE_FILE": txt(tr.get("SOURCE_FILE")), "TILE_REF": txt(tr.get("TILE_REF")),
                           "REASON": "EMPTY_TILE_GEOMETRY"})
            continue
        if sidx is not None:
            try:
                cand = list(sidx.query(geom, predicate="intersects"))
            except Exception:
                cand = list(sidx.query(geom))
        else:
            cand = list(range(len(row_areas)))

        cover = []
        for i in cand:
            rr = row_areas.iloc[int(i)]
            rg = rr.geometry
            if rg is None or rg.is_empty or not geom.intersects(rg):
                continue
            ratio = float(geom.intersection(rg).area) / float(geom.area)
            if ratio > 0:
                cover.append((ratio, txt(rr["ROW"])))
        cover.sort(reverse=True)

        high = [(ratio, row) for ratio, row in cover if ratio >= float(multi_row_threshold)]
        if len(high) >= 2:
            chosen = high
            method = "MULTI_99"
        elif cover:
            chosen = [cover[0]]
            method = "PRIMARY_MAJORITY"
        else:
            # Rare edge case: no polygon intersection. Use nearest FULL RoW but flag it.
            d = row_areas.geometry.distance(geom)
            if d.empty or d.isna().all():
                review.append({"SOURCE_FILE": txt(tr.get("SOURCE_FILE")), "TILE_REF": txt(tr.get("TILE_REF")),
                               "REASON": "NO_FULL_ROW_ASSIGNMENT"})
                continue
            idx = d.idxmin()
            chosen = [(0.0, txt(row_areas.loc[idx, "ROW"]))]
            method = "NEAREST_NO_OVERLAP"

        for ratio, row in chosen:
            rec = tr.to_dict()
            rec["ROW"] = row
            rec["ROW_COVER"] = float(ratio)
            rec["COVER_PCT"] = float(ratio) * 100.0
            rec["ASSIGNMENT"] = method
            rec["DELIVERY_FILE"] = build_delivery_lidar_name(
                row, txt(tr.get("TILE_REF")), year, txt(tr.get("SOURCE_EXT")), lidar_output_format
            )
            rec["TILE_NAME"] = rec["DELIVERY_FILE"]
            rows.append(rec)

    assigned = gpd.GeoDataFrame(rows, geometry="geometry", crs=physical_tiles.crs) if rows else rows_to_gdf([], physical_tiles.crs)
    return assigned, pd.DataFrame(review)


def filter_complete_row_products(
    management_units_all: gpd.GeoDataFrame,
    physical_tiles: gpd.GeoDataFrame,
    min_area_coverage: float,
    year: int,
    multi_row_threshold: float,
    lidar_output_format: str,
):
    """Return delivery Tile Index + MU products for FULL RoWs only, plus QA."""
    review = assess_complete_rows(management_units_all, physical_tiles, min_area_coverage)
    complete_rows = set(review.loc[review["STATUS"] == "FULL", "ROW"].astype(str))
    if not complete_rows:
        raise RuntimeError("No complete RoW found in the current LiDAR package. See 02_row_completeness.csv.")

    eligible_mus = management_units_all[management_units_all["ROW"].isin(complete_rows)].copy()
    delivery_tiles, tile_assignment_review = assign_tiles_to_delivery_rows(
        physical_tiles, eligible_mus, complete_rows, year,
        multi_row_threshold=multi_row_threshold,
        lidar_output_format=lidar_output_format,
    )
    if delivery_tiles.empty:
        raise RuntimeError("No LiDAR tiles could be assigned to FULL RoWs")
    management_units = build_management_unit_product(eligible_mus, delivery_tiles)
    return delivery_tiles, management_units, tile_assignment_review, review, complete_rows


def _tile_list_chunks(tile_names: list[str], max_chars: int = 250) -> list[str]:
    chunks, current = [], ""
    for name in sorted(set(txt(v) for v in tile_names if txt(v))):
        candidate = name if not current else current + ";" + name
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = name
    if current:
        chunks.append(current)
    return chunks or [""]


def build_management_unit_product(
    mus: gpd.GeoDataFrame,
    delivery_tiles: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Attach delivery tile filenames while preserving client MU attribution."""
    base = mus.reset_index(drop=True).copy()
    base["MU_IDX"] = np.arange(len(base), dtype=int)
    tile_lookup = defaultdict(list)

    for row, msg in base.groupby("ROW", sort=False):
        tg = delivery_tiles[delivery_tiles["ROW"] == row][["TILE_NAME", "geometry"]].copy()
        if tg.empty:
            continue
        j = gpd.sjoin(msg[["MU_IDX", "geometry"]], tg, how="left", predicate="intersects")
        for _, r in j.iterrows():
            if pd.notna(r.get("TILE_NAME")) and txt(r.get("TILE_NAME")):
                tile_lookup[int(r["MU_IDX"])].append(txt(r["TILE_NAME"]))

    rows = []
    max_chunks = 1
    for _, r in base.iterrows():
        mu_idx = int(r["MU_IDX"])
        chunks = _tile_list_chunks(tile_lookup.get(mu_idx, []))
        if not chunks or not chunks[0]:
            continue
        max_chunks = max(max_chunks, len(chunks))
        rows.append((r, chunks, mu_idx))

    internal_drop = {"MU_IDX", "MU_LAYER", "MU_ROW_RAW"}
    records = []
    for r, chunks, mu_idx in rows:
        rec = {}
        for col in base.columns:
            if col == "geometry" or col in internal_drop:
                continue
            rec[col] = r.get(col)
        rec["ROW"] = txt(r.get("ROW"))
        rec["CLIENT"] = txt(r.get("CLIENT"))
        rec["TILES"] = chunks[0] if chunks else ""
        for i in range(1, max_chunks):
            rec[f"TILES_{i+1}"] = chunks[i] if i < len(chunks) else ""
        rec["geometry"] = r.geometry
        records.append(rec)

    return gpd.GeoDataFrame(records, geometry="geometry", crs=mus.crs) if records else rows_to_gdf([], mus.crs)


def write_row_products(
    tile_index: gpd.GeoDataFrame,
    management_units: gpd.GeoDataFrame,
    year: int,
    outroot: Path,
) -> None:
    rdir = outroot / "row"
    rdir.mkdir(parents=True, exist_ok=True)
    for old in rdir.iterdir():
        if old.is_file():
            old.unlink()

    for row, sg in tile_index.groupby("ROW", sort=False):
        if not txt(row):
            continue
        cols = [c for c in ["ROW", "TILE_NAME", "CAP_YEAR", "geometry"] if c in sg.columns]
        out = rdir / f"{safe_filename(row)}_Tile_Index_{year}.shp"
        _remove_shapefile(out)
        sg[cols].to_file(out, driver="ESRI Shapefile", engine="pyogrio", index=False)

    for row, sg in management_units.groupby("ROW", sort=False):
        if not txt(row):
            continue
        internal = {"MU_IDX", "MU_LAYER", "MU_ROW_RAW"}
        cols = [c for c in sg.columns if c not in internal and c != "geometry"] + ["geometry"]
        out = rdir / f"{safe_filename(row)}_Management_Unit_{year}.shp"
        _remove_shapefile(out)
        sg[cols].to_file(out, driver="ESRI Shapefile", engine="pyogrio", index=False)



def _remove_cpg(path: Path) -> None:
    cpg = path.with_suffix(".cpg")
    if cpg.exists():
        try:
            cpg.unlink()
        except Exception:
            pass


def _write_clean_shapefile(gdf: gpd.GeoDataFrame, out: Path, cols: list[str]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    _remove_shapefile(out)
    use = [c for c in cols if c in gdf.columns]
    gdf[use].to_file(out, driver="ESRI Shapefile", engine="pyogrio", index=False)
    _remove_cpg(out)


def _transcode_lidar(src: Path, dst: Path) -> dict[str, Any]:
    """Copy/transcode a delivery LiDAR file and return header QA metadata.

    When the delivery extension is ``.las`` the output is always written as
    uncompressed LAS 1.4. The source point format, dimensions, scales, offsets,
    VLRs/CRS and point records are preserved; only the LAS file version/container
    is upgraded where required. Processing is streamed in chunks so large tiles do
    not have to be loaded fully into memory.
    """
    if laspy is None:
        raise RuntimeError("LiDAR copy/conversion requires laspy + lazrs")

    dst.parent.mkdir(parents=True, exist_ok=True)
    src_ext = src.suffix.lower()
    dst_ext = dst.suffix.lower()
    if src_ext not in {".las", ".laz"} or dst_ext not in {".las", ".laz"}:
        raise RuntimeError(f"Unsupported LiDAR conversion: {src.name} -> {dst.name}")

    with laspy.open(src, read_evlrs=True) as reader:
        src_version = str(reader.header.version)
        src_pf = int(reader.header.point_format.id)
        src_count = int(reader.header.point_count)

        # Non-LAS outputs retain the existing file version. A same-format LAZ copy
        # can be copied byte-for-byte; otherwise it is streamed through laspy.
        if dst_ext != ".las":
            if src_ext == dst_ext:
                shutil.copy2(src, dst)
            else:
                header = copy.deepcopy(reader.header)
                with laspy.open(dst, mode="w", header=header, do_compress=True) as writer:
                    for pts in reader.chunk_iterator(2_000_000):
                        writer.write_points(pts)
        else:
            # Final Eversource delivery standard: uncompressed LAS 1.4.
            from laspy.header import Version
            header = copy.deepcopy(reader.header)
            header.set_version_and_point_format(Version(1, 4), header.point_format)
            with laspy.open(dst, mode="w", header=header, do_compress=False) as writer:
                for pts in reader.chunk_iterator(2_000_000):
                    writer.write_points(pts)

    # Re-open the output as a hard validation rather than trusting the filename.
    with laspy.open(dst, read_evlrs=False) as check:
        out_version = str(check.header.version)
        out_pf = int(check.header.point_format.id)
        out_count = int(check.header.point_count)
        if dst_ext == ".las" and out_version != "1.4":
            raise RuntimeError(f"LAS 1.4 validation failed for {dst.name}: wrote version {out_version}")
        if out_count != src_count:
            raise RuntimeError(
                f"LiDAR point-count validation failed for {dst.name}: "
                f"source={src_count:,}, output={out_count:,}"
            )
        if out_pf != src_pf:
            raise RuntimeError(
                f"LiDAR point-format changed unexpectedly for {dst.name}: "
                f"source={src_pf}, output={out_pf}"
            )

    return {
        "SOURCE_LAS_VERSION": src_version,
        "LAS_VERSION": out_version,
        "POINT_FORMAT": out_pf,
        "POINT_COUNT": out_count,
    }


def materialise_delivery_lidar(delivery_tiles: gpd.GeoDataFrame, deliverables_root: Path, copy_files: bool = True):
    rows = []
    seen = set()
    for _, r in delivery_tiles.iterrows():
        row = txt(r.get("ROW"))
        fn = txt(r.get("DELIVERY_FILE"))
        src = Path(txt(r.get("SOURCE_PATH")))
        if not row or not fn:
            continue
        dest = deliverables_root / safe_filename(row) / "LiDAR" / fn
        key = str(dest).lower()
        if key in seen:
            continue
        seen.add(key)
        status = "PLANNED_ONLY"
        lidar_meta = {
            "SOURCE_LAS_VERSION": "", "LAS_VERSION": "1.4" if dest.suffix.lower() == ".las" else "",
            "POINT_FORMAT": "", "POINT_COUNT": "",
        }
        if copy_files:
            if not src.exists():
                raise RuntimeError(f"LiDAR source disappeared before delivery copy: {src}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            lidar_meta = _transcode_lidar(src, dest)
            status = "WRITTEN"
        rows.append({
            "PRODUCT": "LiDAR", "ROW": row, "CIRCUIT": "", "TILE_REF": txt(r.get("TILE_REF")),
            "SOURCE_FILE": txt(r.get("SOURCE_FILE")), "ROW_COVER": r.get("ROW_COVER"),
            "COVER_PCT": r.get("COVER_PCT"), "ASSIGNMENT": txt(r.get("ASSIGNMENT")), "DELIVERY_FILE": fn,
            "OUTPUT_PATH": str(dest.relative_to(deliverables_root.parent)), "STATUS": status,
            **lidar_meta,
        })
    return rows


def _row_list(value: Any) -> list[str]:
    return [x.strip() for x in txt(value).split(";") if x.strip()]



def _caydence_row_references(
    kmz_refs: list[KmzRef],
    selected_rows: set[str] | None = None,
) -> dict[str, Any]:
    """Dissolve reference linework by RoW for Caydence source attribution."""
    selected = {txt(x) for x in (selected_rows or set()) if txt(x)}
    grouped = defaultdict(list)

    for ref in kmz_refs or []:
        row = txt(ref.row)
        if not row or ref.geometry is None or ref.geometry.is_empty:
            continue
        if selected and row not in selected:
            continue
        grouped[row].append(ref.geometry)

    return {
        row: unary_union(geoms)
        for row, geoms in grouped.items()
        if geoms
    }


def _assign_source_geometry_to_row(
    geom,
    row_refs: dict[str, Any],
    fallback_rows: set[str] | None,
    unit_to_m: float,
) -> tuple[str, float | None]:
    """Assign one source DXF entity to exactly one RoW without dropping it.

    Caydence is a repackaging of the contractor 3D source, not a circuit-derived
    analysis product. Therefore every source entity must survive.

    If one RoW is selected, attribution is deterministic and no geometry test is
    needed. With multiple RoWs, choose the closest SoW/KMZ RoW reference.
    """
    fallback = sorted({txt(x) for x in (fallback_rows or set()) if txt(x)})

    if len(fallback) == 1:
        return fallback[0], 0.0

    candidates = row_refs
    if fallback:
        candidates = {
            row: g for row, g in row_refs.items()
            if row in set(fallback)
        }

    if not candidates:
        if fallback:
            return fallback[0], None
        return "UNASSIGNED", None

    best_row = ""
    best_dist = float("inf")
    for row, ref_geom in candidates.items():
        try:
            d = float(geom.distance(ref_geom)) * float(unit_to_m)
        except Exception:
            continue
        if d < best_dist:
            best_dist = d
            best_row = row

    if best_row:
        return best_row, best_dist

    if fallback:
        return fallback[0], None
    return "UNASSIGNED", None



def _infer_caydence_wire_type(source_layer: str, role_hint: str = "") -> str:
    """Return a stable Caydence wire type without filtering source geometry."""
    role = txt(role_hint).upper()
    if role in {"EARTH", "PHASE", "GUY"}:
        return role

    layer = txt(source_layer).upper()
    if any(x in layer for x in ("EARTH", "SHIELD", "STATIC", "GROUND_WIRE", "OHGW")):
        return "EARTH"
    if "GUY" in layer:
        return "GUY"
    if any(x in layer for x in ("PHASE", "CONDUCT", "MAINLINE")):
        return "PHASE"
    return "OTHER"


def _nearest_circuit_for_source_wire(
    geom,
    client: dict,
    kmz: dict,
    unit_to_m: float,
) -> tuple[str, str, float | None]:
    """Spatial fallback circuit attribution for a raw source wire."""
    refs, labels = combined_references(client, kmz)
    if not refs:
        return "", "", None

    g = force_2d(geom)
    if g is None or g.is_empty:
        return "", "", None

    samples = [
        g.interpolate(f, normalized=True)
        for f in (0.2, 0.5, 0.8)
    ]
    scores = []
    for key, ref in refs.items():
        try:
            ds = np.asarray(
                [float(p.distance(ref)) * unit_to_m for p in samples],
                dtype=float,
            )
        except Exception:
            continue
        if len(ds):
            scores.append((float(np.median(ds)), key))
    if not scores:
        return "", "", None

    scores.sort(key=lambda x: x[0])
    dist_m, key = scores[0]
    circuit = (
        txt(client.get(key, {}).get("line_no"))
        or txt(labels.get(key))
        or txt(key)
    )
    return txt(key), circuit, float(dist_m)



def _nearest_structure_anchor_for_wire(
    geom,
    structures: gpd.GeoDataFrame | None,
    unit_to_m: float,
    max_anchor_m: float = 15.0,
):
    """Return the nearest final Structure to either endpoint of a raw 3D wire.

    This is intentionally simple and is used only as a fallback for raw Caydence
    wires that were not already attributed through TRACK_BUNDLE.

    If either endpoint is within max_anchor_m of a final Structure carrying a
    CIRCUIT value, the wire inherits that Structure circuit. The closest valid
    endpoint/Structure pair wins.
    """
    if structures is None or structures.empty:
        return None

    usable = structures[
        structures.geometry.notna()
        & ~structures.geometry.is_empty
        & (structures["CIRCUIT"].astype(str).str.len() > 0)
    ].copy()
    if usable.empty:
        return None

    g = force_2d(geom)
    if g is None or g.is_empty:
        return None

    try:
        coords = list(g.coords)
    except Exception:
        return None
    if len(coords) < 2:
        return None

    endpoints = [
        Point(float(coords[0][0]), float(coords[0][1])),
        Point(float(coords[-1][0]), float(coords[-1][1])),
    ]

    best = None
    for endpoint_index, endpoint in enumerate(endpoints):
        dists = usable.geometry.distance(endpoint)
        if len(dists) == 0:
            continue
        pos = int(np.argmin(np.asarray(dists, dtype=float)))
        row = usable.iloc[pos]
        dist_m = float(dists.iloc[pos]) * unit_to_m

        if dist_m > float(max_anchor_m):
            continue

        candidate = {
            "row": row,
            "distance_m": dist_m,
            "endpoint_index": endpoint_index,
        }
        if (
            best is None
            or candidate["distance_m"] < best["distance_m"]
        ):
            best = candidate

    return best


def build_caydence_wire_attribution(
    wires: gpd.GeoDataFrame,
    bundles: list[dict[str, Any]],
    client: dict,
    kmz: dict,
    unit_to_m: float,
    structures: gpd.GeoDataFrame | None = None,
    structure_anchor_m: float = 15.0,
) -> dict[int, dict[str, Any]]:
    """Enrich every raw DXF wire while preserving 1:1 source completeness.

    Bundle/track attribution is preferred when available and is never
    overridden here. Raw source entities that were not part of an analytical
    bundle first try a simple final-Structure endpoint anchor. Only if no wire
    endpoint lies within structure_anchor_m of a Structure does the existing
    nearest-reference fallback run.
    """
    by_wire: dict[int, list[dict[str, Any]]] = defaultdict(list)

    for b in bundles or []:
        ckey = txt(b.get("CKEY"))
        circuit = txt(b.get("CIRCUIT"))
        dist = b.get("DIST_M")
        try:
            dist_val = float(dist) if dist is not None and pd.notna(dist) else np.inf
        except Exception:
            dist_val = np.inf

        earth_ids = {int(x) for x in b.get("EARTH_WIRE_ROWS", [])}
        all_ids = {int(x) for x in b.get("ALL_WIRE_ROWS", [])}

        for wi in all_ids:
            by_wire[wi].append({
                "CKEY": ckey,
                "CIRCUIT": circuit,
                "DIST_M": dist_val,
                "WIRE_TYPE": "EARTH" if wi in earth_ids else "PHASE",
                "METHOD": "TRACK_BUNDLE",
            })

    result: dict[int, dict[str, Any]] = {}
    for wi, r in wires.reset_index(drop=True).iterrows():
        candidates = by_wire.get(int(wi), [])
        if candidates:
            # Prefer the circuit seen most often for this source entity; distance
            # is the tie-break. This is stable where one source wire participates
            # in multiple adjacent span bundles.
            grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
            for c in candidates:
                key = (c["CKEY"], c["CIRCUIT"], c["WIRE_TYPE"])
                rec = grouped.setdefault(
                    key,
                    {"count": 0, "dist": np.inf, **c},
                )
                rec["count"] += 1
                rec["dist"] = min(float(rec["dist"]), float(c["DIST_M"]))
            best = sorted(
                grouped.values(),
                key=lambda x: (-int(x["count"]), float(x["dist"]), txt(x["CIRCUIT"])),
            )[0]
            ckey = txt(best["CKEY"])
            circuit = txt(best["CIRCUIT"])
            attr_dist = None if not np.isfinite(best["dist"]) else float(best["dist"])
            method = "TRACK_BUNDLE"
            role = txt(best["WIRE_TYPE"])
        else:
            anchor = _nearest_structure_anchor_for_wire(
                r.geometry,
                structures,
                unit_to_m,
                max_anchor_m=structure_anchor_m,
            )
            if anchor is not None:
                sr = anchor["row"]
                ckey = circuit_key(sr.get("CIRCUIT"))
                circuit = txt(sr.get("CIRCUIT"))
                attr_dist = float(anchor["distance_m"])
                method = "STRUCTURE_ANCHOR"
                role = ""

                # Prefer the final Structure's formal attribution because it is
                # already circuit-resolved. Fall back to reference metadata below
                # only if one of these values is blank.
                voltage = txt(sr.get("VOLTAGE"))
                company = txt(sr.get("COMPANY"))
            else:
                ckey, circuit, attr_dist = _nearest_circuit_for_source_wire(
                    r.geometry, client, kmz, unit_to_m
                )
                method = "NEAREST_REFERENCE"
                role = ""
                voltage = ""
                company = ""

        if candidates:
            voltage = ""
            company = ""

        if ckey or circuit:
            ref_voltage, ref_company, _ = circuit_attributes(
                ckey, circuit, client, kmz
            )
            voltage = voltage or txt(ref_voltage)
            company = company or txt(ref_company)

        result[int(wi)] = {
            "CIRCUIT": circuit,
            "VOLTAGE": voltage,
            "COMPANY": company,
            "WIRE_TYPE": _infer_caydence_wire_type(
                txt(r.get("SOURCE_LAYER")),
                role,
            ),
            "ATTR_METHOD": method,
            "ATTR_DIST_M": attr_dist,
        }

    return result


def build_caydence_structures_by_row(
    structures_final: gpd.GeoDataFrame,
    selected_rows: set[str],
) -> gpd.GeoDataFrame:
    """Use the final 3D circuit Structure product unchanged, grouped by RoW.

    No new structure geometry is calculated for Caydence. This is simply the
    existing final circuit-level 3D Structure dataset concatenated at RoW level.
    """
    if structures_final is None or structures_final.empty:
        return rows_to_gdf([], getattr(structures_final, "crs", None))

    out = structures_final.copy()
    selected = {txt(x) for x in (selected_rows or set()) if txt(x)}
    if selected and "ROW" in out.columns:
        out = out[out["ROW"].astype(str).isin(selected)].copy()
    return out


def build_caydence_row_products(
    wires: gpd.GeoDataFrame,
    kmz_refs: list[KmzRef],
    selected_rows: set[str],
    unit_to_m: float,
    bundles: list[dict[str, Any]],
    client: dict,
    kmz: dict,
    structures: gpd.GeoDataFrame | None = None,
    structure_anchor_m: float = 15.0,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Lossless raw 3D wire source, grouped by RoW and enriched with attribution."""
    if wires is None or wires.empty:
        raise RuntimeError("Caydence export requires source 3D wires")

    selected = {txt(x) for x in (selected_rows or set()) if txt(x)}
    row_refs = _caydence_row_references(kmz_refs, selected)
    target_rows = selected or set(row_refs.keys())
    if not target_rows:
        raise RuntimeError(
            "Could not determine RoW(s) for Caydence source packaging"
        )

    attribution = build_caydence_wire_attribution(
        wires,
        bundles,
        client,
        kmz,
        unit_to_m,
        structures=structures,
        structure_anchor_m=structure_anchor_m,
    )

    wire_rows = []
    for wi, r in wires.reset_index(drop=True).iterrows():
        row, row_dist = _assign_source_geometry_to_row(
            r.geometry, row_refs, target_rows, unit_to_m
        )
        a = attribution.get(int(wi), {})
        wire_rows.append({
            "ROW": row,
            "CIRCUIT": txt(a.get("CIRCUIT")),
            "VOLTAGE": txt(a.get("VOLTAGE")),
            "COMPANY": txt(a.get("COMPANY")),
            "WIRE_TYPE": txt(a.get("WIRE_TYPE")),
            "WIRE_ID": txt(r.get("WIRE_ID")) or f"DXF_W{wi+1:06d}",
            "SRC_LAYER": txt(r.get("SOURCE_LAYER")),
            "SRC_ENT": txt(r.get("SOURCE_ENTITY")),
            "ROW_DIST_M": row_dist,
            "ATTR_METHOD": txt(a.get("ATTR_METHOD")),
            "ATTR_DIST_M": a.get("ATTR_DIST_M"),
            "SOURCE_IDX": int(wi),
            "geometry": r.geometry,
        })

    cayd_w = rows_to_gdf(wire_rows, wires.crs)

    n_wire_input = int(len(wires))
    n_wire_output = int(len(cayd_w))
    n_wire_unique = (
        int(cayd_w["SOURCE_IDX"].nunique())
        if not cayd_w.empty else 0
    )
    if n_wire_output != n_wire_input or n_wire_unique != n_wire_input:
        raise RuntimeError(
            "Caydence 3D wire completeness check failed: "
            f"input={n_wire_input}, output={n_wire_output}, "
            f"unique_source={n_wire_unique}"
        )

    qa_rows = []
    for row in sorted(set(cayd_w["ROW"].astype(str))):
        wg = cayd_w[cayd_w["ROW"].astype(str) == row]
        qa_rows.append({
            "ROW": row,
            "WIRE_COUNT": int(len(wg)),
            "WIRE_UNIQUE_SOURCE": int(wg["SOURCE_IDX"].nunique()),
            "ATTRIBUTED_CIRCUIT": int(
                (wg["CIRCUIT"].astype(str).str.len() > 0).sum()
            ),
            "ATTRIBUTED_VOLTAGE": int(
                (wg["VOLTAGE"].astype(str).str.len() > 0).sum()
            ),
            "MAX_WIRE_ROW_DIST_M": (
                float(pd.to_numeric(
                    wg["ROW_DIST_M"], errors="coerce"
                ).max())
                if len(wg) else np.nan
            ),
        })

    method_counts = (
        cayd_w["ATTR_METHOD"]
        .astype(str)
        .value_counts()
        .to_dict()
        if not cayd_w.empty
        else {}
    )

    qa_rows.append({
        "ROW": "__TOTAL__",
        "WIRE_COUNT": n_wire_output,
        "WIRE_UNIQUE_SOURCE": n_wire_unique,
        "TRACK_BUNDLE": int(method_counts.get("TRACK_BUNDLE", 0)),
        "STRUCTURE_ANCHOR": int(method_counts.get("STRUCTURE_ANCHOR", 0)),
        "NEAREST_REFERENCE": int(method_counts.get("NEAREST_REFERENCE", 0)),
        "ATTRIBUTED_CIRCUIT": int(
            (cayd_w["CIRCUIT"].astype(str).str.len() > 0).sum()
        ),
        "ATTRIBUTED_VOLTAGE": int(
            (cayd_w["VOLTAGE"].astype(str).str.len() > 0).sum()
        ),
        "MAX_WIRE_ROW_DIST_M": np.nan,
    })

    return cayd_w, pd.DataFrame(qa_rows)


def build_caydence_3d_products(
    paths: dict,
    bundles: list[dict[str, Any]],
    wires: gpd.GeoDataFrame,
    raw_points: gpd.GeoDataFrame,
    final_wires: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Repackage original subcontractor 3D wires and tower-top points by circuit.

    Geometry is copied from the source DXFs exactly as read.  Nothing is flattened,
    simplified, snapped or re-elevated.  The circuit/path solution is used only to
    decide which original source entities belong in each circuit delivery package.

    For a three-pole support all original tower-top points in the topology group are
    retained.  This output is intended as a practical Caydence interchange product;
    the formal 2D Wire and ground-level Structure deliverables remain separate.
    """
    if wires is None or wires.empty:
        return rows_to_gdf([], getattr(wires, "crs", None)), rows_to_gdf([], getattr(raw_points, "crs", None))

    blookup = {int(b["BUNDLE_ID"]): b for b in bundles}
    meta = {}
    if final_wires is not None and not final_wires.empty:
        for _, r in final_wires.iterrows():
            meta[txt(r.get("CIRCUIT"))] = {
                "ROW": txt(r.get("ROW")),
                "VOLTAGE": txt(r.get("VOLTAGE")),
                "COMPANY": txt(r.get("COMPANY")),
            }

    wire_rows = []
    top_rows = []
    seen_wires = set()
    seen_tops = set()

    for _, path in sorted(paths.items(), key=lambda kv: txt(kv[1].get("CIRCUIT"))):
        circuit = txt(path.get("CIRCUIT"))
        m = meta.get(circuit, {"ROW":"", "VOLTAGE":"", "COMPANY":""})
        source_ids = set()
        earth_ids = set()
        for bid in path.get("BUNDLES", []):
            b = blookup.get(int(bid))
            if not b:
                continue
            source_ids.update(int(x) for x in b.get("ALL_WIRE_ROWS", []))
            earth_ids.update(int(x) for x in b.get("EARTH_WIRE_ROWS", []))

        for wi in sorted(source_ids):
            if wi < 0 or wi >= len(wires):
                continue
            key = (circuit, wi)
            if key in seen_wires:
                continue
            seen_wires.add(key)
            r = wires.iloc[int(wi)]
            wire_rows.append({
                "ROW": m["ROW"], "CIRCUIT": circuit, "VOLTAGE": m["VOLTAGE"], "COMPANY": m["COMPANY"],
                "WIRE_ID": txt(r.get("WIRE_ID")),
                "WIRE_ROLE": "EARTH" if wi in earth_ids else "PHASE",
                "SRC_LAYER": txt(r.get("SOURCE_LAYER")),
                "SRC_ENT": txt(r.get("SOURCE_ENTITY")),
                "geometry": r.geometry,
            })

        node_ids = {txt(x) for x in path.get("NODES", []) if txt(x)}
        if raw_points is not None and not raw_points.empty and "TOPO_ID" in raw_points.columns:
            sg = raw_points[raw_points["TOPO_ID"].astype(str).isin(node_ids)]
            for idx, r in sg.iterrows():
                key = (circuit, int(idx))
                if key in seen_tops:
                    continue
                seen_tops.add(key)
                top_rows.append({
                    "ROW": m["ROW"], "CIRCUIT": circuit, "VOLTAGE": m["VOLTAGE"], "COMPANY": m["COMPANY"],
                    "POINT_ID": txt(r.get("POINT_ID")), "TOPO_ID": txt(r.get("TOPO_ID")),
                    "SRC_LAYER": txt(r.get("SOURCE_LAYER")), "SRC_ENT": txt(r.get("SOURCE_ENTITY")),
                    "N_MARKERS": int(r.get("N_MARKERS", 1)) if pd.notna(r.get("N_MARKERS", 1)) else 1,
                    "geometry": r.geometry,
                })

    return rows_to_gdf(wire_rows, wires.crs), rows_to_gdf(top_rows, raw_points.crs if raw_points is not None else wires.crs)


def write_delivery_tree(
    final_wires: gpd.GeoDataFrame,
    final_structures: gpd.GeoDataFrame,
    tile_index: gpd.GeoDataFrame,
    management_units: gpd.GeoDataFrame,
    caydence_wires_3d: gpd.GeoDataFrame,
    caydence_tops_3d: gpd.GeoDataFrame,
    complete_rows: set[str],
    year: int,
    output: Path,
    copy_lidar: bool,
) -> pd.DataFrame:
    """Write final delivery hierarchy: RoW -> RoW products + Circuit products."""
    root = output / "deliverables"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    inv = []

    # RoW products.
    for row in sorted(complete_rows):
        safe_row = safe_filename(row)
        tg = tile_index[tile_index["ROW"] == row].copy()
        if not tg.empty:
            out = root / safe_row / "Tile_Index" / f"{safe_row}_Tile_Index_{year}.shp"
            _write_clean_shapefile(tg, out, ["ROW", "TILE_NAME", "TILE_REF", "CAP_YEAR", "geometry"])
            inv.append({"PRODUCT":"Tile Index","ROW":row,"CIRCUIT":"","TILE_REF":"","SOURCE_FILE":"",
                        "ROW_COVER":"","COVER_PCT":"","ASSIGNMENT":"","DELIVERY_FILE":out.name,
                        "OUTPUT_PATH":str(out.relative_to(output)),"STATUS":"WRITTEN"})
        mg = management_units[management_units["ROW"] == row].copy()
        if not mg.empty:
            out = root / safe_row / "Management_Units" / f"{safe_row}_Management_Unit_{year}.shp"
            base = ["ROW","CLIENT","ID","MU_ID","SHAPE_LEN","SHAPE_AREA","N_TILES","TILES"]
            extra = sorted([c for c in mg.columns if re.fullmatch(r"TILES_\d+", str(c))], key=lambda x:int(str(x).split("_")[1]))
            _write_clean_shapefile(mg, out, base + extra + ["geometry"])
            inv.append({"PRODUCT":"Management Units","ROW":row,"CIRCUIT":"","TILE_REF":"","SOURCE_FILE":"",
                        "ROW_COVER":"","COVER_PCT":"","ASSIGNMENT":"","DELIVERY_FILE":out.name,
                        "OUTPUT_PATH":str(out.relative_to(output)),"STATUS":"WRITTEN"})

    # Circuit products are placed under every FULL RoW parent referenced by the circuit.
    struct_groups = {txt(c): sg.copy() for c, sg in final_structures.groupby("CIRCUIT", sort=False)}
    for _, wr in final_wires.iterrows():
        circuit = txt(wr.get("CIRCUIT"))
        parents = set(_row_list(wr.get("ROW"))) & set(complete_rows)
        if not parents and circuit in struct_groups:
            parents = set(txt(v) for v in struct_groups[circuit]["ROW"] if txt(v)) & set(complete_rows)
        if not parents:
            continue
        wg = gpd.GeoDataFrame([wr.to_dict()], geometry="geometry", crs=final_wires.crs)
        sg = struct_groups.get(circuit, rows_to_gdf([], final_structures.crs))
        for row in sorted(parents):
            cdir = root / safe_filename(row) / "Circuit" / safe_filename(circuit)
            wout = cdir / f"{safe_filename(circuit)}_Wires_{year}.shp"
            _write_clean_shapefile(wg, wout, ["ROW","CIRCUIT","VOLTAGE","COMPANY","LENGTH_FT","geometry"])
            inv.append({"PRODUCT":"Wires","ROW":row,"CIRCUIT":circuit,"TILE_REF":"","SOURCE_FILE":"",
                        "ROW_COVER":"","COVER_PCT":"","ASSIGNMENT":"","DELIVERY_FILE":wout.name,
                        "OUTPUT_PATH":str(wout.relative_to(output)),"STATUS":"WRITTEN"})
            if sg is not None and not sg.empty:
                sout = cdir / f"{safe_filename(circuit)}_Structure_{year}.shp"
                # QC fields SRC_LINE, SRC_DIST_M and GROUND_Z intentionally stay out of deliverable.
                _write_clean_shapefile(sg, sout, ["ROW","CIRCUIT","VOLTAGE","COMPANY","STRUCT_ID","GIS_NO",
                                                   "STR_TYPE","MATERIAL","geometry"])
                inv.append({"PRODUCT":"Structures","ROW":row,"CIRCUIT":circuit,"TILE_REF":"","SOURCE_FILE":"",
                            "ROW_COVER":"","COVER_PCT":"","ASSIGNMENT":"","DELIVERY_FILE":sout.name,
                            "OUTPUT_PATH":str(sout.relative_to(output)),"STATUS":"WRITTEN"})

            # Caydence interchange: original subcontractor 3D geometry, repackaged
            # by the resolved circuit.  These are deliberately separate from the
            # formal 2D Wire / ground-level Structure deliverables.
            caydir = cdir / "Caydence"
            if caydence_wires_3d is not None and not caydence_wires_3d.empty:
                cw = caydence_wires_3d[caydence_wires_3d["CIRCUIT"].astype(str) == circuit].copy()
                if not cw.empty:
                    w3out = caydir / f"{safe_filename(circuit)}_3D_Wires_{year}.shp"
                    _write_clean_shapefile(cw, w3out, ["ROW","CIRCUIT","VOLTAGE","COMPANY","WIRE_ID","WIRE_ROLE","SRC_LAYER","SRC_ENT","geometry"])
                    inv.append({"PRODUCT":"3D Wires - Caydence","ROW":row,"CIRCUIT":circuit,"TILE_REF":"","SOURCE_FILE":"",
                                "ROW_COVER":"","COVER_PCT":"","ASSIGNMENT":"SOURCE_3D","DELIVERY_FILE":w3out.name,
                                "OUTPUT_PATH":str(w3out.relative_to(output)),"STATUS":"WRITTEN"})
            if caydence_tops_3d is not None and not caydence_tops_3d.empty:
                ct = caydence_tops_3d[caydence_tops_3d["CIRCUIT"].astype(str) == circuit].copy()
                if not ct.empty:
                    t3out = caydir / f"{safe_filename(circuit)}_Tower_Tops_{year}.shp"
                    _write_clean_shapefile(ct, t3out, ["ROW","CIRCUIT","VOLTAGE","COMPANY","POINT_ID","TOPO_ID","SRC_LAYER","SRC_ENT","N_MARKERS","geometry"])
                    inv.append({"PRODUCT":"Tower Tops - Caydence","ROW":row,"CIRCUIT":circuit,"TILE_REF":"","SOURCE_FILE":"",
                                "ROW_COVER":"","COVER_PCT":"","ASSIGNMENT":"SOURCE_3D","DELIVERY_FILE":t3out.name,
                                "OUTPUT_PATH":str(t3out.relative_to(output)),"STATUS":"WRITTEN"})
    return pd.DataFrame(inv)


# =============================================================================
# PACKAGE DISCOVERY + RUN
# =============================================================================

def package_root():
    """Return package root; core lives under scripts/ in the packaged tester."""
    here = Path(__file__).resolve().parent
    return here.parent if here.name.lower() == "scripts" else here


def _dxf_content_profile(path: Path) -> dict[str, Any]:
    """Inspect a DXF and describe its geometry without using the filename.

    The Eversource package contains two very different CAD inputs:
      * 3D conductor vectors - predominantly long LINE/POLYLINE/LWPOLYLINE entities
      * structure top/attachment markers - POINT/INSERT/CIRCLE entities and/or
        much shorter linear marker geometry

    Names are deliberately ignored. This allows files such as AOIs.dxf and
    wires.dxf as long as their contents identify their roles.
    """
    doc = ezdxf.readfile(path)
    point_like = 0
    linear = 0
    linear_3d = 0
    closed_linear = 0
    lengths = []
    vertices = 0

    for entity in doc.modelspace():
        et = entity.dxftype()
        if et in {"POINT", "INSERT", "CIRCLE"}:
            point_like += 1
            continue
        if et not in {"LINE", "POLYLINE", "LWPOLYLINE"}:
            continue
        coords = clean_consecutive_coords(dxf_entity_coords_wcs(entity))
        if len(coords) < 2:
            continue
        linear += 1
        vertices += len(coords)
        length = 0.0
        zs = []
        for a, b in zip(coords[:-1], coords[1:]):
            length += math.hypot(float(b[0])-float(a[0]), float(b[1])-float(a[1]))
        for p in coords:
            zs.append(float(p[2]))
        lengths.append(float(length))
        if zs and (max(zs)-min(zs) > 1e-6 or max(abs(z) for z in zs) > 1e-6):
            linear_3d += 1
        try:
            if bool(getattr(entity, 'closed', False)) or bool(getattr(entity, 'is_closed', False)):
                closed_linear += 1
        except Exception:
            pass

    med = float(np.median(lengths)) if lengths else 0.0
    p75 = float(np.quantile(lengths, 0.75)) if lengths else 0.0
    short = sum(1 for v in lengths if v <= 25.0)
    long = sum(1 for v in lengths if v > 25.0)

    # Scores are used only to rank files relative to each other.
    point_score = 1000.0*point_like + 25.0*short - 5.0*long
    wire_score = 50.0*linear_3d + 10.0*long + 2.0*linear + min(p75, 10000.0)/100.0 - 20.0*closed_linear
    return {
        "path": path,
        "point_like": int(point_like),
        "linear": int(linear),
        "linear_3d": int(linear_3d),
        "closed_linear": int(closed_linear),
        "short_linear": int(short),
        "long_linear": int(long),
        "median_length": med,
        "p75_length": p75,
        "vertices": int(vertices),
        "point_score": float(point_score),
        "wire_score": float(wire_score),
    }


def discover_two_dxfs(input_dir: Path):
    """Identify the wire and structure-marker DXFs from their contents.

    Filenames are not used. If more than two DXFs are present, the strongest
    wire-like and point/marker-like pair is selected and the profiles are shown.
    """
    dxfs = sorted(input_dir.glob("*.dxf"))
    if len(dxfs) < 2:
        raise RuntimeError("input/ must contain the 3D wire DXF and Structure Top Attachment Point DXF")

    profiles = [_dxf_content_profile(p) for p in dxfs]
    info("DXF content inspection:")
    for pr in profiles:
        info(
            f"  {pr['path'].name}: point-like={pr['point_like']}, "
            f"linear={pr['linear']}, 3D-linear={pr['linear_3d']}, "
            f"median-length={pr['median_length']:.2f}"
        )

    # Prefer explicit point-like CAD entities when they exist.
    point_rank = sorted(
        profiles,
        key=lambda p: (p['point_score'], -p['median_length'], p['point_like']),
        reverse=True,
    )
    wire_rank = sorted(
        profiles,
        key=lambda p: (p['wire_score'], p['median_length'], p['linear_3d']),
        reverse=True,
    )

    points = point_rank[0]
    wire = next((p for p in wire_rank if p['path'] != points['path']), None)

    # If neither file contains point-like entities, short marker geometry should
    # be substantially shorter than the conductor geometry. Re-evaluate pairwise.
    if max(p['point_like'] for p in profiles) == 0:
        candidates = [p for p in profiles if p['linear'] > 0]
        if len(candidates) >= 2:
            points = min(candidates, key=lambda p: (p['median_length'] or float('inf'), -p['short_linear']))
            wire = max((p for p in candidates if p['path'] != points['path']), key=lambda p: (p['wire_score'], p['median_length']))

    if wire is None or wire['path'] == points['path'] or wire['linear'] == 0:
        raise RuntimeError(
            "Could not identify distinct wire and structure-marker DXFs from their contents. "
            "Check that one file contains conductor vectors and one contains structure/attachment markers."
        )

    # A point-marker file with no point entities and geometry as long as the wire
    # file is genuinely ambiguous; fail with useful diagnostics rather than guess.
    if points['point_like'] == 0 and points['median_length'] > 0 and wire['median_length'] > 0:
        ratio = wire['median_length'] / max(points['median_length'], 1e-9)
        if ratio < 2.0:
            raise RuntimeError(
                "DXF contents are ambiguous: both files look predominantly linear and their typical "
                "entity lengths are similar. Filenames are intentionally ignored."
            )

    info(f"  -> 3D wires: {wire['path'].name}")
    info(f"  -> structure/attachment markers: {points['path'].name}")
    return wire['path'], points['path']


def find_single_file(root: Path,suffix: str,label: str):
    m=sorted([p for p in root.glob("*") if p.is_file() and p.suffix.lower()==suffix])
    if len(m)!=1:
        raise RuntimeError(f"Expected exactly one {label} in {root}; found {len(m)}")
    return m[0]


def discover_lidar_input(input_dir: Path) -> Path:
    # Preferred: one folder under input/ containing LAS/LAZ tiles.
    named = [input_dir / "lidar", input_dir / "laz", input_dir / "ground_lidar"]
    for d in named:
        if d.exists() and d.is_dir() and any(p.suffix.lower() in {".las", ".laz"} for p in d.rglob("*")):
            return d

    direct = sorted([p for p in input_dir.glob("*") if p.is_file() and p.suffix.lower() in {".las", ".laz"}])
    if len(direct) == 1:
        return direct[0]
    if len(direct) > 1:
        # Multiple tiles directly in input are treated as one logical LiDAR source.
        return input_dir

    zips = sorted([p for p in input_dir.glob("*.zip") if re.search(r"lidar|laz|las|ground", p.name, re.I)])
    if len(zips) == 1:
        return zips[0]

    # Fallback: exactly one subfolder anywhere under input containing LAS/LAZ.
    dirs = []
    for d in [x for x in input_dir.iterdir() if x.is_dir()]:
        if any(p.suffix.lower() in {".las", ".laz"} for p in d.rglob("*")):
            dirs.append(d)
    if len(dirs) == 1:
        return dirs[0]

    raise RuntimeError(
        "LiDAR source not found. Put LAS/LAZ files under input/lidar/ "
        "(preferred), or place LAS/LAZ directly in input/."
    )


def discover_package_inputs():
    root=package_root(); inp=root/"input"; src=root/"source"
    if not inp.exists(): raise RuntimeError(f"Missing input folder: {inp}")
    if not src.exists(): raise RuntimeError(f"Missing source folder: {src}")
    wires,points=discover_two_dxfs(inp)
    lidar=discover_lidar_input(inp)
    client=find_single_file(src,".zip","client source ZIP")
    kmz=find_single_file(src,".kmz","Project SoW KMZ")

    gdbs=sorted([p for p in src.rglob("*.gdb") if p.is_dir()])
    preferred=[p for p in gdbs if p.name.lower()=="tvm_mus.gdb"]
    gdbs=preferred or gdbs
    if len(gdbs)>1:
        raise RuntimeError("More than one Management Units .gdb found under source/. Keep TVM_MUs.gdb only or use --management-units.")
    mu_source=gdbs[0] if len(gdbs)==1 else None
    return wires,points,lidar,client,kmz,mu_source


PROJECT_STATE_PLANES = {
    "1": (6434, "Connecticut - NAD83(2011) / Connecticut (ftUS)"),
    "2": (6492, "Massachusetts Mainland - NAD83(2011) / Massachusetts Mainland (ftUS)"),
    "3": (6525, "New Hampshire - NAD83(2011) / New Hampshire (ftUS)"),
}


def prompt_epsg():
    """Prompt for the project State Plane CRS, with manual EPSG fallback."""
    info("\nSelect project coordinate system:")
    info("  1. Connecticut          - EPSG:6434")
    info("  2. Massachusetts Mainland - EPSG:6492")
    info("  3. New Hampshire        - EPSG:6525")
    info("  4. Other EPSG")

    while True:
        choice = input("Selection [1-4]: ").strip()
        if choice in PROJECT_STATE_PLANES:
            epsg, label = PROJECT_STATE_PLANES[choice]
            info(f"Selected: {label} | EPSG:{epsg}")
            return epsg

        if choice == "4":
            while True:
                raw = input("Enter EPSG code: ").strip()
                if raw.isdigit():
                    try:
                        crs = CRS.from_epsg(int(raw))
                        info(f"Selected: {crs.name} | EPSG:{int(raw)}")
                        return int(raw)
                    except Exception:
                        pass
                info("Enter a valid numeric EPSG code.")

        info("Enter 1, 2, 3 or 4.")


def run(wires_path,points_path,lidar_source,client_source,kmz_path,management_units_source,year,output,epsg,
        marker_cluster_m,support_cluster_m,marker_max_length_m,max_endpoint_m,
        track_join_m,horizontal_z_range_m,horizontal_z_ratio,max_join_review_m,
        client_name="Eversource", tile_size_override=None, row_complete_area=0.99,
        wire_simplify_m=0.25, tile_multi_row_threshold=0.99, lidar_output_format="las",
        copy_lidar=True):
    output.mkdir(parents=True,exist_ok=True)
    legacy_shp=output/"shp"
    if legacy_shp.exists():
        shutil.rmtree(legacy_shp)
    crs=CRS.from_epsg(epsg);u2m=unit_to_metre(crs)
    info("="*80);info("EVERSOURCE STAGE 02 - SIMPLE SOURCE-WIRE SEQUENCE");info("="*80)
    info(f"CRS: EPSG:{epsg} | unit->metre={u2m}")

    info("\nReading 3D wires DXF...")
    wires=read_dxf_wires(wires_path,crs);info(f"  source wires: {len(wires):,}")
    info("Reading Structure Top Attachment Point DXF...")
    raw_points=read_dxf_structure_points(points_path,crs,u2m,marker_cluster_m,marker_max_length_m)
    info(f"  source structure/attachment points: {len(raw_points):,}")
    supports,point_to_support=build_support_sites(raw_points,u2m,support_cluster_m)
    raw_points=raw_points.copy().reset_index(drop=True)
    raw_points["TOPO_ID"]=[point_to_support.get(int(i), "") for i in raw_points.index]
    info(f"  physical support groups after {support_cluster_m:g} m clustering: {len(supports):,}")
    info("  NOTE: grouping is topology-only; contractor top points are preserved and never averaged for final Structure XY")

    info("\nAssociating source-wire endpoints with supports...")
    wm,wire_review=endpoint_assignment(wires,supports,u2m,max_endpoint_m)
    info(f"  accepted source wires: {len(wm):,}")
    info(f"  endpoint-match review: {len(wire_review):,}")

    info("Building internal physical-support reference axis (topology QA only)...")
    supports=recenter_supports_on_wire_axis(supports,wm,wires,u2m,tangent_sample_m=5.0)
    if not supports.empty:
        info(f"  median internal reference shift: {float(supports['AXIS_SHIFT_M'].median()):.2f} m")
        info(f"  max internal reference shift:    {float(supports['AXIS_SHIFT_M'].max()):.2f} m")

    info("Building physical span bundles...")
    bundles,bundle_gdf,earth,phase_seed=build_span_bundles(
        wm,supports,u2m,horizontal_z_range_m,horizontal_z_ratio
    )
    if not bundles: raise RuntimeError("No valid 3-phase span bundles were built")
    info(f"  circuit bundles: {len(bundles):,}")
    info(f"  earth/static wires excluded: {len(earth):,}")

    info("Building continuous physical tracks...")
    track_lookup,track_pairs=build_physical_tracks(bundles,supports,u2m,track_join_m)
    info(f"  physical tracks: {len(set(track_lookup.values())):,}")

    with tempfile.TemporaryDirectory(prefix="eversource02_") as td:
        td=Path(td)

        info("\nReading client source...")
        client_lines,client_structures=load_client_source(client_source,td)
        if client_lines.crs is None: raise RuntimeError("Client line source has no CRS")
        if CRS.from_user_input(client_lines.crs)!=crs: client_lines=client_lines.to_crs(crs)
        if not client_structures.empty:
            if client_structures.crs is None:
                raise RuntimeError("Client structure source has no CRS")
            if CRS.from_user_input(client_structures.crs)!=crs:
                client_structures=client_structures.to_crs(crs)
        client=client_lookup(client_lines);info(f"  client circuits: {len(client):,}")
        client_structure_index=build_client_structure_index(client_structures)
        info(f"  client structure records: {len(client_structures):,}")
        info("Reading Project SoW KMZ...")
        kmz_refs=project_kmz(parse_sow_kmz(kmz_path),crs);kmz=kmz_lookup(kmz_refs)
        info(f"  KMZ circuit/RoW references: {len(kmz):,}")

        info("Reading Management Units FileGDB...")
        mu_gdb=resolve_management_units_gdb(management_units_source,client_source,td)
        management_units_all,mu_review=load_management_units(mu_gdb,crs,kmz_refs,client_name)
        info(f"  Management Units: {len(management_units_all):,} from {mu_gdb.name}")

        info("Indexing LiDAR tiles...")
        physical_tiles,inferred_tile_size=build_lidar_tile_footprints(
            lidar_source,crs,td,year,tile_size_override
        )
        if inferred_tile_size is not None:
            info(f"  nominal tile size: {inferred_tile_size:g} CRS units")
        info(f"  physical LiDAR tiles: {len(physical_tiles):,}")
        if lidar_output_format == "las":
            info("  delivery LiDAR format: LAS 1.4 (uncompressed)")
        elif lidar_output_format == "same":
            info("  delivery LiDAR format: preserve source LAS/LAZ version and compression")
        else:
            info("  delivery LiDAR format: LAZ (source LAS version retained)")
        tile_index,management_units,tile_assignment_review,row_completeness,complete_rows=filter_complete_row_products(
            management_units_all,physical_tiles,row_complete_area,year,
            tile_multi_row_threshold,lidar_output_format
        )
        mu_duplicate_review=management_unit_duplicate_review(management_units_all)
        full_rows=row_completeness[row_completeness["STATUS"]=="FULL"]
        partial_rows=row_completeness[row_completeness["STATUS"]!="FULL"]
        info("  FULL RoWs: " + ", ".join(full_rows["ROW"].astype(str)) if not full_rows.empty else "  FULL RoWs: none")
        if not partial_rows.empty:
            for _,rr in partial_rows.iterrows():
                info(
                    f"  SKIP partial RoW {rr['ROW']}: "
                    f"MUs {int(rr['N_MUS_TOUCHED'])}/{int(rr['N_MUS_TOTAL'])}, "
                    f"area {100.0*float(rr['AREA_COVERAGE']):.1f}%"
                )
        info(f"  delivery Tile Index features: {len(tile_index):,}")
        n_multi=int((tile_index.get("ASSIGNMENT",pd.Series(dtype=object))=="MULTI_99").sum()) if not tile_index.empty else 0
        info(f"  >=99% multi-RoW tile assignments: {n_multi:,}")
        info(f"  Management Unit features for FULL RoWs: {len(management_units):,}")

        info("\nAssigning complete physical tracks to client circuits...")
        track_meta,track_review=assign_tracks_to_circuits(bundles,track_lookup,wires,client,kmz,u2m)
        counts=track_review["CIRCUIT"].value_counts().to_dict() if not track_review.empty else {}
        info("  track attribution: "+", ".join(f"{k}={v}" for k,v in counts.items()))

        info("Selecting ONE longest sequence per circuit...")
        paths,dropped=build_circuit_paths(bundles,supports,u2m)
        for ckey,p in paths.items():
            info(f"  {p['CIRCUIT']}: {len(p['BUNDLES'])} retained spans")

        info("Selecting one actual source phase per retained span...")
        final_wires,span_edges,final_structures,phase_discarded,join_review=build_final_products(
            paths,bundles,supports,raw_points,client,kmz,client_structure_index,u2m,max_join_review_m,wire_simplify_m
        )
        if final_wires.empty: raise RuntimeError("No final circuit wires generated")
        if final_structures.empty: raise RuntimeError("No final circuit structures generated")
        nb=int(final_wires.get("NODES_BEFORE",pd.Series(dtype=float)).sum()) if "NODES_BEFORE" in final_wires.columns else 0
        na=int(final_wires.get("NODES_AFTER",pd.Series(dtype=float)).sum()) if "NODES_AFTER" in final_wires.columns else 0
        if nb:
            info(f"  final wire vertices: {nb:,} -> {na:,} (simplify {wire_simplify_m:g} m)")
        if "TOP_RULE" in final_structures.columns:
            n3=int((final_structures["TOP_RULE"]=="THREE_POLE_MIDDLE").sum())
            info(f"  three-pole middle structures (off-wire allowed): {n3:,}")

        # Final Structure XY is now fixed. Normal/H-frame structures are on the
        # circuit wire; three-pole structures use the physical middle pole XY.
        # Only now do we sample LiDAR ground Z at the delivered XY.
        info("\nSampling ground elevation at FINAL circuit-structure XY...")
        final_structures,ground_qa=add_ground_elevation_from_lidar(
            final_structures,lidar_source,crs,u2m,td
        )
        info(f"  final structures with LiDAR ground Z: {len(final_structures):,}")

        # QA invariant is conditional. Normal/H-frame structures remain wire-aligned.
        # Three-pole structures deliberately represent the physical middle pole and
        # therefore may sit off the selected 2D conductor.
        wire_map={txt(r["CIRCUIT"]):r.geometry for _,r in final_wires.iterrows()}
        aligned_off=[]
        three_pole_off=[]
        actual_dist=[]
        for idx,sr in final_structures.iterrows():
            wg=wire_map.get(txt(sr["CIRCUIT"]))
            d=np.nan
            if wg is not None:
                d=float(Point(sr.geometry.x,sr.geometry.y).distance(wg))*u2m
                if txt(sr.get("TOP_RULE"))=="THREE_POLE_MIDDLE":
                    three_pole_off.append(d)
                else:
                    aligned_off.append(d)
            actual_dist.append(d)
        final_structures=final_structures.copy()
        final_structures["WIRE_DIST_M"]=actual_dist
        max_aligned=max(aligned_off) if aligned_off else 0.0
        max_three=max(three_pole_off) if three_pole_off else 0.0
        info(f"  max wire-aligned Structure XY distance: {max_aligned:.6f} m")
        if three_pole_off:
            info(f"  three-pole Structures off wire: {len(three_pole_off):,}; max distance {max_three:.3f} m")
        if max_aligned > 0.001:
            raise RuntimeError(
                f"Structure-on-wire invariant failed for a wire-aligned structure: max XY offset {max_aligned:.6f} m"
            )

        gpkg=output/"02_eversource_2d_wires.gpkg"
        if gpkg.exists(): gpkg.unlink()
        caydence_wires_3d,caydence_tops_3d=build_caydence_3d_products(
            paths,bundles,wires,raw_points,final_wires
        )
        info(f"  Caydence 3D source wires: {len(caydence_wires_3d):,}")
        info(f"  Caydence tower-top points: {len(caydence_tops_3d):,}")

        write_layer(final_wires,gpkg,"wires_2d")
        write_layer(final_structures,gpkg,"structures_final")
        write_layer(caydence_wires_3d,gpkg,"caydence_wires_3d")
        write_layer(caydence_tops_3d,gpkg,"caydence_tower_tops_3d")
        write_layer(build_structure_source_match_lines(final_structures),gpkg,"structure_source_match")
        write_layer(ground_qa,gpkg,"structure_ground_qa")
        write_layer(span_edges,gpkg,"selected_span_wires")
        write_layer(dropped,gpkg,"discarded_branches")
        write_layer(earth,gpkg,"earth_wires_excluded")
        write_layer(phase_discarded,gpkg,"phase_wires_discarded")
        write_layer(supports,gpkg,"support_points")
        write_layer(raw_points,gpkg,"source_structure_points")
        write_layer(bundle_gdf,gpkg,"span_bundles")
        write_layer(track_review,gpkg,"track_assignment_review")
        write_layer(track_pairs,gpkg,"track_pairing")
        write_layer(join_review,gpkg,"join_review")
        write_layer(wire_review,gpkg,"source_wire_review")
        write_layer(client_lines,gpkg,"client_reference")
        write_layer(client_structures,gpkg,"client_structure_reference")
        kmz_rows=[{"ROW":r.row,"CIRCUIT":r.circuit,"VOLTAGE":r.voltage,"COMPANY":r.company,
                   "GLOBALID":r.globalid,"geometry":r.geometry} for r in kmz_refs if r.geometry is not None]
        write_layer(rows_to_gdf(kmz_rows,crs),gpkg,"kmz_reference")
        write_layer(physical_tiles,gpkg,"lidar_tile_footprints")
        write_layer(tile_index,gpkg,"tile_index")
        write_layer(management_units,gpkg,"management_units")

        delivery_inventory=write_delivery_tree(
            final_wires,final_structures,tile_index,management_units,
            caydence_wires_3d,caydence_tops_3d,complete_rows,year,output,copy_lidar
        )
        lidar_inventory=materialise_delivery_lidar(tile_index,output/"deliverables",copy_files=copy_lidar)
        if lidar_inventory:
            delivery_inventory=pd.concat([delivery_inventory,pd.DataFrame(lidar_inventory)],ignore_index=True)

        audit=[]
        for _,r in final_wires.iterrows():
            c=txt(r["CIRCUIT"]);sp=span_edges[span_edges["CIRCUIT"]==c];st=final_structures[final_structures["CIRCUIT"]==c]
            jr=join_review[join_review["CIRCUIT"]==c] if not join_review.empty else join_review
            audit.append({
                "CIRCUIT":c,"N_SPANS":len(sp),"N_STRUCTURES":len(st),"ROWS":txt(r["ROW"]),
                "VOLTAGE":txt(r["VOLTAGE"]),"LENGTH_M":float(r["LENGTH_M"]),
                "LENGTH_FT":float(r.get("LENGTH_FT", np.nan)),
                "NODES_BEFORE":int(r.get("NODES_BEFORE",0)),"NODES_AFTER":int(r.get("NODES_AFTER",0)),
                "MAX_JOIN_M":float(r["MAX_JOIN_M"]),
                "N_JOIN_REVIEW":int(jr["REVIEW"].sum()) if not jr.empty else 0,
                "N_HORIZONTAL":int((sp["ARRANGEMENT"]=="HORIZONTAL").sum()),
                "N_VERTICAL_MIXED":int((sp["ARRANGEMENT"]!="HORIZONTAL").sum()),
            })
        pd.DataFrame(audit).to_csv(output/"02_eversource_audit.csv",index=False)
        final_structures.drop(columns="geometry",errors="ignore").sort_values(
            "SRC_DIST_M",ascending=False,na_position="last"
        ).to_csv(output/"02_structure_source_review.csv",index=False)
        ground_qa.drop(columns="geometry",errors="ignore").to_csv(output/"02_structure_ground_review.csv",index=False)
        track_review.drop(columns="geometry",errors="ignore").to_csv(output/"02_track_assignment_review.csv",index=False)
        wire_review.drop(columns="geometry",errors="ignore").to_csv(output/"02_source_wire_review.csv",index=False)
        dropped.drop(columns="geometry",errors="ignore").to_csv(output/"02_discarded_branches.csv",index=False)
        join_review.drop(columns="geometry",errors="ignore").to_csv(output/"02_join_review.csv",index=False)
        tile_assignment_review.to_csv(output/"02_tile_assignment_review.csv",index=False)
        tile_index.drop(columns="geometry",errors="ignore").to_csv(output/"02_lidar_naming_review.csv",index=False)
        row_completeness.to_csv(output/"02_row_completeness.csv",index=False)
        mu_review.to_csv(output/"02_management_unit_row_review.csv",index=False)
        mu_duplicate_review.to_csv(output/"02_management_unit_duplicate_ids.csv",index=False)
        delivery_inventory.to_csv(output/"delivery_inventory.csv",index=False)

        info("\nResult:")
        info(f"  final circuits: {len(final_wires):,}")
        info(f"  final circuit structures: {len(final_structures):,}")
        info(f"  GPKG: {gpkg}")
        info(f"  delivery tree: {output/'deliverables'}")
        info(f"  inventory:     {output/'delivery_inventory.csv'}")
        return gpkg


def build_parser():
    p=argparse.ArgumentParser(description="Build one simple 2D source-wire sequence per Eversource circuit")
    p.add_argument("wires",nargs="?",help="3D wires DXF")
    p.add_argument("structure_points",nargs="?",help="Structure Top Attachment Point DXF")
    p.add_argument("lidar_source",nargs="?",help="LiDAR LAS/LAZ file, ZIP, or folder for ground elevation")
    p.add_argument("client_source",nargs="?",help="Client source ZIP/folder/SHP")
    p.add_argument("sow_kmz",nargs="?",help="Project SoW KMZ")
    p.add_argument("--management-units",default=None,help="Optional TVM_MUs.gdb folder or ZIP; package mode auto-discovers source/TVM_MUs.gdb and otherwise falls back to the client ZIP")
    p.add_argument("--client-name",default="Eversource",help="CLIENT value for Management Unit product")
    p.add_argument("--tile-size",type=float,default=None,help="Optional LiDAR tile size in project CRS units. Default: infer from coordinate filenames/header bounds")
    p.add_argument("--row-complete-area",type=float,default=0.99,
                   help="Minimum fraction of the full client MU area that LiDAR tiles must cover for a RoW to be delivered. Every MU must also be touched. Default: 0.99")
    p.add_argument("--epsg",type=int,default=None)
    p.add_argument("--year",type=int,default=2026)
    p.add_argument("-o","--output",default=None)
    p.add_argument("--marker-cluster-m",type=float,default=1.0,help="Merge duplicate DXF marker primitives; default 1 m")
    p.add_argument("--support-cluster-m",type=float,default=15.0,help="Cluster nearby attachment markers into one physical support; default 15 m")
    p.add_argument("--marker-max-length-m",type=float,default=5.0)
    p.add_argument("--max-endpoint-m",type=float,default=30.0,help="Maximum source-wire endpoint to support distance")
    p.add_argument("--track-join-m",type=float,default=25.0,help="Maximum same-circuit attachment separation used to continue a physical track")
    p.add_argument("--horizontal-z-range-m",type=float,default=2.0)
    p.add_argument("--horizontal-z-ratio",type=float,default=0.35)
    p.add_argument("--max-join-review-m",type=float,default=25.0,help="Flag final between-span joins longer than this")
    p.add_argument("--wire-simplify-m",type=float,default=0.25,help="Douglas-Peucker tolerance for final 2D wire spans; structure endpoints stay locked. Default 0.25 m")
    p.add_argument("--tile-multi-row-threshold",type=float,default=0.99,help="If >=2 FULL RoWs each cover this fraction of a tile, create a delivery copy for each. Default 0.99")
    p.add_argument("--lidar-output-format",choices=["las","same","laz"],default="las",help="Delivery LiDAR format. Default: uncompressed LAS 1.4. Advanced overrides: same or laz")
    p.add_argument("--no-lidar-copy",action="store_true",help="Build names/index/inventory but do not physically copy/convert LiDAR files")
    return p


def main():
    args=build_parser().parse_args()
    if all(x is None for x in (args.wires,args.structure_points,args.lidar_source,args.client_source,args.sow_kmz)):
        try:
            wires,points,lidar,client,kmz,mu_source=discover_package_inputs()
        except Exception as exc:
            info(f"\nINPUT ERROR: {exc}");return 1
        output=Path(args.output) if args.output else package_root()/"output"
        if args.management_units:
            mu_source=clean_path(args.management_units)
    else:
        missing=[n for n,v in (("wires",args.wires),("structure_points",args.structure_points),("lidar_source",args.lidar_source),("client_source",args.client_source),("sow_kmz",args.sow_kmz)) if v is None]
        if missing:
            info("ERROR: provide all five positional inputs or none. Missing: "+", ".join(missing));return 1
        wires,points,lidar,client,kmz=map(clean_path,(args.wires,args.structure_points,args.lidar_source,args.client_source,args.sow_kmz))
        mu_source=clean_path(args.management_units) if args.management_units else None
        output=Path(args.output) if args.output else package_root()/"output"
    epsg=args.epsg or prompt_epsg()
    try:
        run(wires,points,lidar,client,kmz,mu_source,args.year,output,epsg,args.marker_cluster_m,args.support_cluster_m,
            args.marker_max_length_m,args.max_endpoint_m,args.track_join_m,args.horizontal_z_range_m,
            args.horizontal_z_ratio,args.max_join_review_m,args.client_name,args.tile_size,args.row_complete_area,
            args.wire_simplify_m,args.tile_multi_row_threshold,args.lidar_output_format,not args.no_lidar_copy)
        info("\nSTEP 02 COMPLETE");return 0
    except Exception as exc:
        info(f"\nERROR: {exc}");return 1




# =============================================================================
# V12 REFACTOR: PREFLIGHT + STRUCTURE POSITION + POINT-CLOUD DELIVERY
# =============================================================================
# This section deliberately overrides selected legacy functions above while
# keeping the proven wire/track logic intact.

import itertools
import json
from dataclasses import asdict

try:
    from pyproj.crs import CompoundCRS
except Exception:
    CompoundCRS = None


PLS_XYZ_CLASS_MAP = {
    # NMIP26068 PLS output schema - source of truth:
    #   NMIP26068_XYZ_Outputs_v1.mac
    #   NMIP26068_Class_v1.ptc
    #   NMIP26068_Circuit_Feature_Code_Descriptions_v1.xlsx
    #
    # The TerraScan macro mirrors class values into Intensity for legacy
    # LAS 1.2 compatibility. This pipeline works on LAS 1.4 and therefore
    # reads the Classification field directly; original Intensity is not
    # modified.
    1: "Feature_1_Unclassified.xyz",
    2: "Feature_2_Ground.xyz",
    4: "Feature_4_Medium Vegetation.xyz",
    5: "Feature_5_High Vegetation.xyz",
    79: "Feature_79_Unmade_Tracks_Paths.xyz",
    126: "Feature_126_Buildings.xyz",
    132: "Feature_132_Wall_Fence.xyz",
    186: "Feature_186_Substation.xyz",
    187: "Feature_187_Tx_Conductors.xyz",
    210: "Feature_210_Tx_Shield_Wires.xyz",
    215: "Feature_215_Tx_Structures.xyz",
    224: "Feature_224_Dx_Structures.xyz",
    226: "Feature_226_Dx_Wires.xyz",
    253: "Feature_253_Dx_Guy_Wire.xyz",
    254: "Feature_254_Tx_Guy_Wire.xyz",
}


def _ensure_expected_pls_files(points_dir: Path, full_dir: Path | None = None) -> None:
    """Guarantee the complete NMIP26068 PLS feature file set.

    TerraScan's macro has an explicit FnScanOutput entry for every expected
    feature.  Mirror that delivery behaviour: even if a class contains zero
    points, create the corresponding zero-byte XYZ so downstream packaging can
    rely on a stable 15-file schema.

    Full Class is intentionally limited to Ground / Medium Veg / High Veg.
    """
    points_dir.mkdir(parents=True, exist_ok=True)
    for name in PLS_XYZ_CLASS_MAP.values():
        p = points_dir / name
        if not p.exists():
            p.touch()

    if full_dir is not None:
        full_dir.mkdir(parents=True, exist_ok=True)
        for cls in (2, 4, 5):
            p = full_dir / PLS_XYZ_CLASS_MAP[cls]
            if not p.exists():
                p.touch()

STATE_EPSG = {
    "CT": 6434,
    "EMA": 6492,
    "MA": 6492,
    "NH": 6525,
}


def _state_from_row(row: str) -> str:
    s = txt(row).upper().replace("-", "_").replace(" ", "_")
    if s.startswith("CT"):
        return "CT"
    if s.startswith("NH"):
        return "NH"
    if s.startswith("EMA") or s.startswith("WMA") or s.startswith("MA"):
        return "EMA"
    return ""


def _expected_epsg_for_row(row: str, fallback: int | None = None) -> int | None:
    return STATE_EPSG.get(_state_from_row(row), fallback)


def _canonical_row_name(raw: str, known_rows: list[str] | set[str] | None = None) -> str:
    s = txt(raw)
    s = re.sub(r"(?i)[ _-]*polygon$", "", s).strip(" _-")
    if known_rows:
        lookup = {row_key(v): v for v in known_rows if txt(v)}
        hit = lookup.get(row_key(s))
        if hit:
            return hit
    return s


def _polygon_only(g: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if g is None or g.empty:
        return g
    keep = g.geometry.notna() & ~g.geometry.is_empty & g.geom_type.isin(["Polygon", "MultiPolygon"])
    return g[keep].copy()


def discover_aoi_polygons(aoi_dir: Path, target_crs, known_rows: list[str] | set[str], temp_dir: Path):
    """Discover published RoW Polygon SHPs under input/AOI.

    Supports the final unpacked folder layout and ZIPs during testing. Only
    Polygon shapefiles are accepted as clip geometry; Lines/DXF are ignored.
    """
    if not aoi_dir.exists() or not aoi_dir.is_dir():
        return {}, pd.DataFrame([{"REASON": "AOI_FOLDER_MISSING", "PATH": str(aoi_dir)}])

    candidates: list[tuple[Path, str]] = []
    for p in sorted(aoi_dir.rglob("*.shp")):
        if re.search(r"(?i)(?:^|[_ -])polygon(?:$|[_ -])", p.stem) or p.stem.lower().endswith("polygon"):
            candidates.append((p, "SHP"))

    # Temporary/testing convenience: allow ZIPs containing *_Polygon.shp.
    for zp in sorted(aoi_dir.rglob("*.zip")):
        dest = temp_dir / ("aoi_" + safe_filename(zp.stem))
        dest.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zp) as zf:
                zf.extractall(dest)
            for p in sorted(dest.rglob("*.shp")):
                if re.search(r"(?i)(?:^|[_ -])polygon(?:$|[_ -])", p.stem) or p.stem.lower().endswith("polygon"):
                    candidates.append((p, f"ZIP:{zp.name}"))
        except Exception as exc:
            info(f"  WARNING: could not inspect AOI ZIP {zp.name}: {exc}")

    out: dict[str, dict[str, Any]] = {}
    review = []
    target = CRS.from_user_input(target_crs)
    for shp, source_kind in candidates:
        try:
            g = gpd.read_file(shp, engine="pyogrio")
            g = _polygon_only(g)
            if g.empty:
                review.append({"AOI": shp.name, "REASON": "NO_POLYGON_GEOMETRY", "PATH": str(shp)})
                continue
            if g.crs is None:
                review.append({"AOI": shp.name, "REASON": "AOI_CRS_MISSING", "PATH": str(shp)})
                continue
            source_crs = CRS.from_user_input(g.crs)
            same_crs = source_crs == target
            practical_delta_m = 0.0
            compatible_crs = bool(same_crs)
            if not same_crs:
                try:
                    raw_union = unary_union(list(g.geometry))
                    c = raw_union.centroid
                    tr = Transformer.from_crs(source_crs, target, always_xy=True)
                    tx, ty = tr.transform(float(c.x), float(c.y))
                    practical_delta_m = float(math.hypot(tx-float(c.x), ty-float(c.y))) * unit_to_metre(target)
                    compatible_crs = practical_delta_m <= 0.05
                    g = g.to_crs(target)
                except Exception as exc:
                    review.append({"AOI": shp.name, "REASON": f"AOI_CRS_TRANSFORM_FAILED:{exc}", "PATH": str(shp)})
                    continue
            row = _canonical_row_name(shp.stem, known_rows)
            geom = unary_union(list(g.geometry))
            if geom is None or geom.is_empty:
                review.append({"AOI": shp.name, "REASON": "AOI_EMPTY_AFTER_UNION", "PATH": str(shp)})
                continue
            key = row_key(row)
            rec = {
                "ROW": row,
                "ROW_KEY": key,
                "SOURCE_FILE": shp.name,
                "SOURCE_KIND": source_kind,
                "SOURCE_CRS": source_crs.to_string(),
                "TARGET_CRS": target.to_string(),
                "CRS_EXACT": int(same_crs),
                "CRS_COMPATIBLE": int(compatible_crs),
                "CRS_DELTA_M": float(practical_delta_m),
                "geometry": geom,
            }
            # Prefer unpacked SHP over a ZIP copy when both exist.
            if key not in out or (out[key]["SOURCE_KIND"].startswith("ZIP:") and source_kind == "SHP"):
                out[key] = rec
        except Exception as exc:
            review.append({"AOI": shp.name, "REASON": f"AOI_READ_FAILED:{exc}", "PATH": str(shp)})
    return out, pd.DataFrame(review)



def find_lasboundary_exe(explicit: Path | None = None) -> Path:
    import os
    candidates = []
    if explicit is not None:
        candidates.append(Path(explicit))
    env_value = os.environ.get("LASBOUNDARY_EXE", "").strip()
    if env_value:
        candidates.append(Path(env_value))
    las2las_env = os.environ.get("LAS2LAS_EXE", "").strip()
    if las2las_env:
        p = Path(las2las_env)
        candidates += [p.with_name("lasboundary64.exe"), p.with_name("lasboundary.exe")]
    root = package_root()
    for base in [
        root / "tools" / "LAStools" / "bin",
        root / "scripts" / "LAStools" / "bin",
        Path(r"C:\Tools\LAStools\bin"),
        Path(r"C:\LAStools\bin"),
        Path(r"C:\Program Files\LAStools\bin"),
    ]:
        candidates += [base / "lasboundary64.exe", base / "lasboundary.exe"]
    for name in ("lasboundary64", "lasboundary64.exe", "lasboundary", "lasboundary.exe"):
        q = shutil.which(name)
        if q:
            candidates.append(Path(q))
    seen = set()
    for p in candidates:
        try:
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.exists() and p.is_file():
            return p
    raise RuntimeError(
        "lasboundary.exe / lasboundary64.exe was not found. "
        "Preflight now requires the actual LiDAR point boundary. "
        "Set LASBOUNDARY_EXE or pass --lasboundary <path>."
    )


def _remove_vector_sidecars(path: Path) -> None:
    for f in path.parent.glob(path.stem + ".*"):
        if f.is_file():
            try:
                f.unlink()
            except Exception:
                pass



def _lidar_source_fingerprint(lidar_source: Path) -> dict[str, Any]:
    """Create a stable fingerprint of the LiDAR input without reading point data.

    Folder/file sources use relative name + size + mtime_ns.
    ZIP sources use the ZIP central directory (name + uncompressed size + CRC),
    so re-extracting the same ZIP does not invalidate the cache.
    """
    source = Path(lidar_source)

    entries = []
    source_type = ""

    if source.is_file() and source.suffix.lower() in {".las", ".laz"}:
        st = source.stat()
        source_type = "single_file"
        entries = [{
            "name": source.name,
            "size": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns),
        }]

    elif source.is_dir():
        source_type = "folder"
        files = sorted(
            p for p in source.rglob("*")
            if p.is_file() and p.suffix.lower() in {".las", ".laz"}
        )
        for p in files:
            st = p.stat()
            entries.append({
                "name": str(p.relative_to(source)).replace("\\", "/"),
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
            })

    elif source.is_file() and source.suffix.lower() == ".zip":
        source_type = "zip"
        with zipfile.ZipFile(source) as zf:
            infos = sorted(
                (
                    zi for zi in zf.infolist()
                    if not zi.is_dir()
                    and Path(zi.filename).suffix.lower() in {".las", ".laz"}
                ),
                key=lambda zi: zi.filename.lower(),
            )
            for zi in infos:
                entries.append({
                    "name": zi.filename.replace("\\", "/"),
                    "size": int(zi.file_size),
                    "crc": int(zi.CRC),
                })
    else:
        raise RuntimeError(
            "LiDAR source must be LAS, LAZ, ZIP, or a folder containing LAS/LAZ"
        )

    if not entries:
        raise RuntimeError("No LAS/LAZ entries found while fingerprinting LiDAR input")

    payload = {
        "source_type": source_type,
        "entries": entries,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["sha256"] = hashlib.sha256(encoded).hexdigest()
    payload["count"] = len(entries)
    return payload


def _lasboundary_cache_descriptor(
    lidar_source: Path,
    project_crs,
    lasboundary_exe: Path,
    concavity: float,
    buffer_ft: float,
) -> dict[str, Any]:
    fp = _lidar_source_fingerprint(lidar_source)
    try:
        tool_stat = Path(lasboundary_exe).stat()
        tool_sig = {
            "name": Path(lasboundary_exe).name,
            "size": int(tool_stat.st_size),
            "mtime_ns": int(tool_stat.st_mtime_ns),
        }
    except Exception:
        tool_sig = {"name": Path(lasboundary_exe).name}

    crs = CRS.from_user_input(project_crs)
    return {
        "cache_version": 1,
        "lidar_sha256": fp["sha256"],
        "lidar_count": int(fp["count"]),
        "source_type": fp["source_type"],
        "epsg": crs.to_epsg(),
        "crs_wkt": crs.to_wkt(),
        "concavity": float(concavity),
        "merge_buffer_ft": float(buffer_ft),
        "lasboundary_tool": tool_sig,
    }


def _load_cached_lidar_boundary(
    output_dir: Path,
    descriptor: dict[str, Any],
    project_crs,
) -> gpd.GeoDataFrame | None:
    """Return cached dissolved boundary only when source + settings still match."""
    manifest = output_dir / "00_lidar_boundary_cache.json"
    final_shp = output_dir / "00_lidar_boundary.shp"
    tiles_gpkg = output_dir / "00_lidar_boundary_tiles.gpkg"

    if not manifest.exists() or not final_shp.exists() or not tiles_gpkg.exists():
        return None

    try:
        cached = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None

    # Compare only deterministic inputs that affect the generated boundary.
    for key in (
        "cache_version",
        "lidar_sha256",
        "lidar_count",
        "source_type",
        "epsg",
        "crs_wkt",
        "concavity",
        "merge_buffer_ft",
        "lasboundary_tool",
    ):
        if cached.get(key) != descriptor.get(key):
            return None

    try:
        g = gpd.read_file(final_shp, engine="pyogrio")
        g = g[g.geometry.notna() & ~g.geometry.is_empty].copy()
        if g.empty:
            return None
        if g.crs is None:
            g = g.set_crs(project_crs, allow_override=True)
        elif CRS.from_user_input(g.crs) != CRS.from_user_input(project_crs):
            return None
        return g
    except Exception:
        return None


def _write_lasboundary_cache_manifest(
    output_dir: Path,
    descriptor: dict[str, Any],
) -> None:
    path = output_dir / "00_lidar_boundary_cache.json"
    tmp = output_dir / "00_lidar_boundary_cache.json.tmp"
    tmp.write_text(json.dumps(descriptor, indent=2), encoding="utf-8")
    tmp.replace(path)


def build_actual_lidar_boundary(
    lidar_source: Path,
    project_crs,
    temp_dir: Path,
    output_dir: Path,
    lasboundary_exe: Path,
    cores: int = 6,
    concavity_m: float = 50.0,
    merge_buffer_ft: float = 0.1,
) -> gpd.GeoDataFrame:
    """Build the actual LiDAR footprint from per-tile lasboundary jobs.

    Performance / robustness:
      1. Each LAS/LAZ tile is processed independently by lasboundary.
      2. Python runs several tiles in parallel; each lasboundary uses one core.
      3. Per-tile polygons are read only after the external process completes.
      4. Boundaries receive +0.1 ft buffer, are dissolved, then -0.1 ft buffer
         to close microscopic seams without materially expanding the footprint.
      5. Only compact vector geometry is merged in Python; point clouds are never
         loaded into memory by this routine.

    ``cores`` is retained as the public parameter name for compatibility with
    older wrappers; it now controls the number of parallel tile workers.
    """
    workers = max(1, int(cores or 1))
    buffer_ft = max(0.0, float(merge_buffer_ft))
    concavity = float(concavity_m)

    output_dir.mkdir(parents=True, exist_ok=True)
    final_shp = output_dir / "00_lidar_boundary.shp"

    descriptor = _lasboundary_cache_descriptor(
        lidar_source,
        project_crs,
        lasboundary_exe,
        concavity,
        buffer_ft,
    )
    cached = _load_cached_lidar_boundary(output_dir, descriptor, project_crs)
    if cached is not None:
        info("Reusing cached ACTUAL LiDAR point boundary.")
        info(
            f"  cache valid: {int(descriptor['lidar_count']):,} LiDAR file(s), "
            "input fingerprint unchanged"
        )
        info(f"  final boundary: {final_shp}")
        info("  Skipping lasboundary. Recalculating AOI/MU coverage only...")
        return cached

    files = discover_lidar_files(lidar_source, temp_dir)
    if not files:
        raise RuntimeError("No LAS/LAZ files available for lasboundary")

    _remove_vector_sidecars(final_shp)

    # Do not expose half-written per-tile SHPs in the production output folder.
    tile_work = temp_dir / "lasboundary_tiles"
    tile_work.mkdir(parents=True, exist_ok=True)

    info("Building ACTUAL LiDAR point boundary with per-tile lasboundary...")
    info(f"  executable: {lasboundary_exe}")
    info(f"  input files: {len(files):,}")
    info(f"  parallel tile workers: {min(workers, len(files))}")
    info("  lasboundary cores per tile: 1")
    info(f"  concavity: {concavity:g} project units")
    info(f"  merge gap-close buffer: +/-{buffer_ft:g} ft")

    def run_one(index_and_src):
        idx, src = index_and_src
        # Numeric prefix guarantees uniqueness even if two source folders contain
        # files with the same stem.
        out_shp = tile_work / f"{idx:06d}_{safe_filename(src.stem)}_boundary.shp"
        _remove_vector_sidecars(out_shp)
        cmd = [
            str(lasboundary_exe),
            "-i", str(src),
            "-disjoint",
            "-only_2d",
            "-feet",
            "-concavity", str(concavity),
            "-cores", "1",
            "-o", str(out_shp),
        ]
        cp = subprocess.run(
            cmd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **_hidden_process_kwargs(),
        )
        if cp.returncode != 0:
            raise RuntimeError(
                f"lasboundary failed for {src.name} "
                f"(exit code {cp.returncode}).\n{cp.stdout}"
            )
        if not out_shp.exists():
            raise RuntimeError(
                f"lasboundary reported success but did not create a boundary "
                f"for {src.name}"
            )
        return idx, src, out_shp

    completed = []
    nworkers = min(workers, len(files))
    with ThreadPoolExecutor(max_workers=nworkers) as pool:
        futures = {
            pool.submit(run_one, (idx, src)): (idx, src)
            for idx, src in enumerate(files, 1)
        }
        done = 0
        for fut in as_completed(futures):
            idx, src, shp = fut.result()
            completed.append((idx, src, shp))
            done += 1
            console_progress("lasboundary", "Preheating | boundary", done, len(files), src.name)

    # Read the tiny vector outputs serially. This avoids concurrent GDAL reads and
    # keeps all point-cloud I/O outside Python.
    tile_rows = []
    for idx, src, shp in sorted(completed, key=lambda x: x[0]):
        g = gpd.read_file(shp, engine="pyogrio")
        g = g[g.geometry.notna() & ~g.geometry.is_empty].copy()
        if g.empty:
            raise RuntimeError(f"lasboundary output is empty for {src.name}")
        if g.crs is None:
            g = g.set_crs(project_crs, allow_override=True)
        elif CRS.from_user_input(g.crs) != CRS.from_user_input(project_crs):
            g = g.to_crs(project_crs)

        for geom in g.geometry:
            if geom is None or geom.is_empty:
                continue
            tile_rows.append({
                "SOURCE_FILE": src.name,
                "SOURCE_PATH": str(src),
                "TILE_ORDER": int(idx),
                "geometry": geom,
            })

    tile_gdf = rows_to_gdf(tile_rows, project_crs)
    if tile_gdf.empty:
        raise RuntimeError("No valid per-tile LiDAR boundaries were produced")

    info(f"  merging {len(tile_gdf):,} per-tile boundary polygon(s)...")

    raw_geoms = [
        g for g in tile_gdf.geometry
        if g is not None and not g.is_empty
    ]
    if buffer_ft > 0.0:
        expanded = [g.buffer(buffer_ft) for g in raw_geoms]
        merged = unary_union(expanded)
        closed = merged.buffer(-buffer_ft)
        if closed is None or closed.is_empty:
            info("  WARNING: negative closing buffer produced an empty geometry; using expanded dissolve")
            closed = merged
    else:
        closed = unary_union(raw_geoms)

    # Normalise invalid artefacts that can appear after many polygon unions.
    try:
        if not closed.is_valid:
            closed = closed.buffer(0)
    except Exception:
        pass

    if closed is None or closed.is_empty:
        raise RuntimeError("Merged LiDAR boundary is empty")

    final_gdf = gpd.GeoDataFrame(
        [{"N_SOURCE_TILES": int(len(files)),
          "BUFFER_FT": float(buffer_ft),
          "geometry": closed}],
        geometry="geometry",
        crs=project_crs,
    )

    # Persist raw per-tile boundaries for QA and a clean dissolved final footprint.
    tiles_gpkg = output_dir / "00_lidar_boundary_tiles.gpkg"
    if tiles_gpkg.exists():
        tiles_gpkg.unlink()
    write_layer(tile_gdf, tiles_gpkg, "tile_boundaries")

    final_gdf.to_file(
        final_shp,
        driver="ESRI Shapefile",
        engine="pyogrio",
        index=False,
    )

    spatial_gpkg = output_dir / "00_preflight_spatial.gpkg"
    if spatial_gpkg.exists():
        spatial_gpkg.unlink()
    write_layer(final_gdf, spatial_gpkg, "lidar_boundary")
    write_layer(tile_gdf, spatial_gpkg, "lidar_boundary_tiles")

    info(f"  per-tile QA: {tiles_gpkg}")
    _write_lasboundary_cache_manifest(output_dir, descriptor)
    info(f"  final boundary: {final_shp}")
    info(f"  boundary cache: {output_dir / '00_lidar_boundary_cache.json'}")
    info("  LiDAR boundary complete. Calculating AOI coverage...")
    return final_gdf


def _inside_ratio(container_geom, subject_geom) -> float:
    if container_geom is None or container_geom.is_empty:
        return 0.0
    if subject_geom is None or subject_geom.is_empty or subject_geom.area <= 0:
        return 0.0
    return float(subject_geom.intersection(container_geom).area) / float(subject_geom.area)


def _coverage_ratio(area_geom, tile_union) -> float:
    if area_geom is None or area_geom.is_empty or area_geom.area <= 0:
        return 0.0
    if tile_union is None or tile_union.is_empty:
        return 0.0
    return float(area_geom.intersection(tile_union).area) / float(area_geom.area)


def _positive_area_overlap(a, b) -> bool:
    if a is None or b is None or a.is_empty or b.is_empty:
        return False
    if not a.intersects(b):
        return False
    try:
        inter = a.intersection(b)
        return inter is not None and not inter.is_empty and float(inter.area) > 1e-9
    except Exception:
        return True


def tile_aoi_membership(physical_tiles: gpd.GeoDataFrame, aoi_lookup: dict[str, dict[str, Any]]) -> pd.DataFrame:
    """Describe which published RoW AOIs overlap each input LiDAR tile footprint.

    This is used only to decide whether point-level clipping is actually required.
    A tile that belongs to one published AOI can pass through unchanged spatially
    (header is still rewritten). A tile overlapping two or more AOIs is shared and
    must be clipped per RoW.
    """
    rows = []
    for _, tr in physical_tiles.iterrows():
        geom = tr.geometry
        hits = []
        for rec in aoi_lookup.values():
            if _positive_area_overlap(geom, rec.get("geometry")):
                hits.append(txt(rec.get("ROW")))
        hits = sorted({x for x in hits if x})
        rows.append({
            "SOURCE_FILE": txt(tr.get("SOURCE_FILE")),
            "SOURCE_PATH": txt(tr.get("SOURCE_PATH")),
            "TILE_REF": txt(tr.get("TILE_REF")),
            "AOI_ROWS": ";".join(hits),
            "N_AOI_ROWS": len(hits),
            "SHARED_TILE": int(len(hits) >= 2),
            "geometry": geom,
        })
    return rows_to_gdf(rows, physical_tiles.crs)


def add_point_processing_summary(
    table: pd.DataFrame,
    physical_tiles: gpd.GeoDataFrame,
    aoi_lookup: dict[str, dict[str, Any]],
    single_aoi_inside_threshold: float = 0.995,
) -> pd.DataFrame:
    if table is None or table.empty:
        return table
    memberships = tile_aoi_membership(physical_tiles, aoi_lookup)
    out = table.copy()
    out["N_INPUT_TILES"] = 0
    out["N_SHARED_TILES"] = 0
    out["N_EXCLUSIVE_TILES"] = 0
    out["POINT_MODE"] = "NO_AOI"
    only_one_aoi = len(aoi_lookup) <= 1

    for idx, r in out.iterrows():
        row = txt(r.get("ROW"))
        aoi = aoi_lookup.get(row_key(row))
        if aoi is None:
            continue
        key = row_key(row)
        hit_mask = memberships["AOI_ROWS"].apply(
            lambda v: key in {row_key(x) for x in txt(v).split(";") if txt(x)}
        )
        mg = memberships[hit_mask]
        n = int(len(mg))
        shared = int(mg["SHARED_TILE"].sum()) if n else 0
        exclusive = n - shared

        if only_one_aoi:
            inside = r.get("LIDAR_INSIDE_AOI")
            if pd.notna(inside) and float(inside) >= float(single_aoi_inside_threshold):
                mode = "NO_CLIP_BOUNDARY_OK"
            else:
                mode = "CLIP_OUTSIDE_AOI"
        elif shared == 0:
            mode = "HEADER_ONLY"
        elif exclusive > 0:
            mode = "HYBRID"
        else:
            mode = "CLIP_SHARED"

        out.at[idx, "N_INPUT_TILES"] = n
        out.at[idx, "N_SHARED_TILES"] = shared
        out.at[idx, "N_EXCLUSIVE_TILES"] = exclusive
        out.at[idx, "POINT_MODE"] = mode
    return out


def build_preflight_table(
    management_units_all: gpd.GeoDataFrame,
    physical_tiles: gpd.GeoDataFrame,
    aoi_lookup: dict[str, dict[str, Any]],
    lidar_boundary: gpd.GeoDataFrame | None = None,
    lidar_boundary_tiles: gpd.GeoDataFrame | None = None,
) -> pd.DataFrame:
    tile_union = unary_union([g for g in physical_tiles.geometry if g is not None and not g.is_empty])
    boundary_union = None
    if lidar_boundary is not None and not lidar_boundary.empty:
        bg = [g for g in lidar_boundary.geometry if g is not None and not g.is_empty]
        boundary_union = unary_union(bg) if bg else None
    coverage_geom = boundary_union if boundary_union is not None and not boundary_union.is_empty else tile_union
    coverage_source = "LASBOUNDARY" if boundary_union is not None and not boundary_union.is_empty else "TILE_BOUNDS"

    rows = []
    for row, sg in management_units_all[management_units_all["ROW"].apply(bool)].groupby("ROW", sort=True):
        geoms = [g for g in sg.geometry if g is not None and not g.is_empty]
        if not geoms:
            continue
        mu_geom = unary_union(geoms)
        mu_cov = _coverage_ratio(mu_geom, coverage_geom)
        aoi = aoi_lookup.get(row_key(row))
        aoi_cov = _coverage_ratio(aoi["geometry"], coverage_geom) if aoi else np.nan

        # Candidate LiDAR in AOI: denominator is only the actual point-boundary
        # footprint of source tiles selected for THIS RoW, not the whole input set.
        candidate_inside = np.nan
        if aoi is not None:
            aoi_geom = aoi["geometry"]
            candidate_files = set(
                physical_tiles.loc[
                    physical_tiles.geometry.apply(lambda g: _positive_area_overlap(g, aoi_geom)),
                    "SOURCE_FILE"
                ].astype(str).tolist()
            ) if not physical_tiles.empty else set()

            if lidar_boundary_tiles is not None and not lidar_boundary_tiles.empty and candidate_files:
                bg = lidar_boundary_tiles[
                    lidar_boundary_tiles["SOURCE_FILE"].astype(str).isin(candidate_files)
                ]
                cgeoms = [g for g in bg.geometry if g is not None and not g.is_empty]
                if cgeoms:
                    candidate_union = unary_union(cgeoms)
                    candidate_inside = _inside_ratio(aoi_geom, candidate_union)
            elif candidate_files:
                pg = physical_tiles[physical_tiles["SOURCE_FILE"].astype(str).isin(candidate_files)]
                cgeoms = [g for g in pg.geometry if g is not None and not g.is_empty]
                if cgeoms:
                    candidate_inside = _inside_ratio(aoi_geom, unary_union(cgeoms))

        display_cov = float(aoi_cov) if aoi is not None else float(mu_cov)
        if display_cov <= 0.0 and mu_cov <= 0.0:
            continue
        rows.append({
            "ROW": txt(row),
            "STATE": _state_from_row(row),
            "COVERAGE": display_cov,
            "COVERAGE_PCT": 100.0 * display_cov,
            "COVERAGE_SOURCE": coverage_source,
            "MU_COVERAGE": float(mu_cov),
            "AOI_COVERAGE": float(aoi_cov) if aoi is not None else np.nan,
            "AOI_COVERAGE_PCT": 100.0 * float(aoi_cov) if aoi is not None else np.nan,
            "CANDIDATE_LIDAR_IN_AOI": float(candidate_inside) if pd.notna(candidate_inside) else np.nan,
            "CANDIDATE_LIDAR_IN_AOI_PCT": 100.0 * float(candidate_inside) if pd.notna(candidate_inside) else np.nan,
            # Backward-compatible aliases used by existing selection JSON/code.
            "LIDAR_INSIDE_AOI": float(candidate_inside) if pd.notna(candidate_inside) else np.nan,
            "LIDAR_INSIDE_AOI_PCT": 100.0 * float(candidate_inside) if pd.notna(candidate_inside) else np.nan,
            "AOI_OK": int(aoi is not None),
            "AOI_FILE": aoi["SOURCE_FILE"] if aoi else "",
            "AOI_SOURCE": aoi["SOURCE_KIND"] if aoi else "",
            "AOI_CRS": aoi["SOURCE_CRS"] if aoi else "",
            "AOI_CRS_EXACT": int(aoi["CRS_EXACT"]) if aoi else 0,
            "AOI_CRS_COMPATIBLE": int(aoi.get("CRS_COMPATIBLE",0)) if aoi else 0,
            "AOI_CRS_DELTA_M": float(aoi.get("CRS_DELTA_M",np.nan)) if aoi else np.nan,
            "N_MUS": int(len(sg)),
            "ACTION": (
                "READY" if aoi is not None
                else ("ADD_AOI" if float(mu_cov) >= 0.95
                      else ("LOW_COVERAGE" if float(mu_cov) > 0.0 else "NO_DATA"))
            ),
        })
    return pd.DataFrame(rows).sort_values("ROW").reset_index(drop=True) if rows else pd.DataFrame()

def _parse_row_selection(raw: str, table: pd.DataFrame) -> list[str]:
    available = table[table["AOI_OK"] == 1].reset_index(drop=True)
    if available.empty:
        return []
    s = txt(raw)
    if not s:
        return []
    if s.upper() == "A":
        return available["ROW"].astype(str).tolist()
    chosen = []
    for token in re.split(r"[,; ]+", s):
        if not token:
            continue
        if token.isdigit():
            i = int(token) - 1
            if 0 <= i < len(table) and int(table.iloc[i]["AOI_OK"]) == 1:
                chosen.append(txt(table.iloc[i]["ROW"]))
        else:
            key = row_key(token)
            hits = available[available["ROW"].apply(row_key) == key]
            if not hits.empty:
                chosen.append(txt(hits.iloc[0]["ROW"]))
    return list(dict.fromkeys(chosen))


def print_preflight_table(table: pd.DataFrame) -> None:
    info("\nDetected RoW input coverage (ACTUAL point boundary):")
    info("  #   RoW                    State   MU cov.   AOI cov.   Cand. in AOI   AOI       Action")
    info("  ------------------------------------------------------------------------------------------------")
    for i, r in table.reset_index(drop=True).iterrows():
        aoi = "OK" if int(r.get("AOI_OK", 0)) else "MISSING"
        mu_cov = r.get("MU_COVERAGE")
        aoi_cov = r.get("AOI_COVERAGE_PCT")
        inside = r.get("CANDIDATE_LIDAR_IN_AOI_PCT")
        mu_s = f"{100.0*float(mu_cov):7.1f}%" if pd.notna(mu_cov) else "      - "
        aoi_s = f"{float(aoi_cov):7.1f}%" if pd.notna(aoi_cov) else "      - "
        inside_s = f"{float(inside):7.1f}%" if pd.notna(inside) else "      - "
        action = txt(r.get("ACTION")) or "-"
        if action == "ADD_AOI":
            action = "ADD AOI TO PROCESS"
        info(
            f"  {i+1:<3} {txt(r['ROW']):<22} {txt(r['STATE']):<7} "
            f"{mu_s:>8}   {aoi_s:>8}   {inside_s:>12}   {aoi:<9} {action}"
        )

def run_preflight(
    lidar_source: Path,
    client_source: Path,
    kmz_path: Path,
    management_units_source: Path | None,
    aoi_dir: Path,
    epsg: int,
    year: int,
    output: Path,
    selected_rows: list[str] | None = None,
    interactive: bool = True,
    low_coverage_warning: float = 0.99,
    lasboundary_path: Path | None = None,
    boundary_cores: int = 6,
    boundary_concavity_m: float = 50.0,
    boundary_merge_buffer_ft: float = 0.1,
) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    crs = CRS.from_epsg(epsg)
    with tempfile.TemporaryDirectory(prefix="eversource_preflight_") as td_raw:
        td = Path(td_raw)
        info("Indexing LiDAR input...")
        physical_tiles, inferred = build_lidar_tile_footprints(lidar_source, crs, td, year, None)
        info(f"  tiles: {len(physical_tiles):,}")
        if inferred:
            info(f"  nominal tile size: {inferred:g} CRS units")

        info("Reading Project SoW + Management Units...")
        kmz_refs = project_kmz(parse_sow_kmz(kmz_path), crs)
        gdb = resolve_management_units_gdb(management_units_source, client_source, td)
        mus, mu_review = load_management_units(gdb, crs, kmz_refs, "Eversource")
        known_rows = sorted({txt(r.row) for r in kmz_refs if txt(r.row)} | set(mus["ROW"].astype(str)))

        info(f"Checking AOIs under: {aoi_dir}")
        aois, aoi_review = discover_aoi_polygons(aoi_dir, crs, known_rows, td)

        lasboundary_exe = find_lasboundary_exe(lasboundary_path)
        lidar_boundary = build_actual_lidar_boundary(
            lidar_source, crs, td, output, lasboundary_exe,
            cores=boundary_cores,
            concavity_m=boundary_concavity_m,
            merge_buffer_ft=boundary_merge_buffer_ft,
        )

        boundary_tiles_gpkg = output / "00_lidar_boundary_tiles.gpkg"
        boundary_tiles = None
        if boundary_tiles_gpkg.exists():
            try:
                boundary_tiles = gpd.read_file(
                    boundary_tiles_gpkg, layer="tile_boundaries", engine="pyogrio"
                )
            except Exception as exc:
                info(f"  WARNING: could not read per-tile boundary QA for candidate coverage: {exc}")

        table = build_preflight_table(
            mus, physical_tiles, aois, lidar_boundary=lidar_boundary,
            lidar_boundary_tiles=boundary_tiles
        )
        table = add_point_processing_summary(table, physical_tiles, aois)

        spatial_gpkg = output / "00_preflight_spatial.gpkg"
        if spatial_gpkg.exists():
            spatial_gpkg.unlink()

        # Rebuild spatial QA every run. The expensive point boundary can come
        # from cache; AOI/MU relationships must reflect the current input/AOI.
        write_layer(lidar_boundary, spatial_gpkg, "lidar_boundary")
        boundary_tiles_gpkg = output / "00_lidar_boundary_tiles.gpkg"
        if boundary_tiles_gpkg.exists():
            try:
                cached_tiles = gpd.read_file(
                    boundary_tiles_gpkg,
                    layer="tile_boundaries",
                    engine="pyogrio",
                )
                write_layer(cached_tiles, spatial_gpkg, "lidar_boundary_tiles")
            except Exception as exc:
                info(f"  WARNING: could not copy cached tile-boundary QA layer: {exc}")

        boundary_union = unary_union([g for g in lidar_boundary.geometry if g is not None and not g.is_empty])
        aoi_rows = []
        for rec in aois.values():
            g = rec.get("geometry")
            if g is not None and not g.is_empty:
                aoi_rows.append({"ROW": txt(rec.get("ROW")), "geometry": g})
        aoi_gdf = rows_to_gdf(aoi_rows, crs)
        write_layer(aoi_gdf, spatial_gpkg, "aoi_polygons")
        overlap_rows = []
        for _, ar in aoi_gdf.iterrows():
            inter = ar.geometry.intersection(boundary_union)
            if inter is not None and not inter.is_empty:
                overlap_rows.append({"ROW": txt(ar.get("ROW")), "geometry": inter})
        write_layer(rows_to_gdf(overlap_rows, crs), spatial_gpkg, "lidar_aoi_overlap")
        if table.empty:
            raise RuntimeError("No RoW overlap found between LiDAR input and project Management Units")
        print_preflight_table(table)

        if selected_rows is None and interactive:
            info("\nOnly RoWs with a published Polygon AOI can be selected.")
            raw = input("Select RoWs (numbers/names, A=all available, Q=quit): ").strip()
            if raw.upper() == "Q":
                raise RuntimeError("Processing cancelled by user")
            selected_rows = _parse_row_selection(raw, table)
            if raw.upper() == "A":
                skipped = table.loc[table["AOI_OK"] != 1, "ROW"].astype(str).tolist()
                if skipped:
                    info(
                        "Process All: skipping RoW(s) without a published AOI: "
                        + ", ".join(skipped)
                    )
        elif selected_rows is None:
            selected_rows = table.loc[table["AOI_OK"] == 1, "ROW"].astype(str).tolist()
        else:
            selected_rows = _parse_row_selection(";".join(selected_rows), table)

        if not selected_rows:
            # This is not a processing failure. Preserve all preflight outputs,
            # especially the expensive lasboundary cache, and stop the full
            # pipeline cleanly until an AOI is added.
            waiting_rows = table.loc[table["AOI_OK"] != 1, "ROW"].astype(str).tolist()
            selection = {
                "version": 2,
                "status": "WAITING_FOR_AOI",
                "epsg": int(epsg),
                "capture_year": int(year),
                "aoi_dir": str(aoi_dir),
                "selected_rows": [],
                "waiting_for_aoi": waiting_rows,
                "rows": [],
            }
            sel_path = output / "00_preflight_selection.json"
            sel_path.write_text(json.dumps(selection, indent=2), encoding="utf-8")
            table.to_csv(output / "00_preflight_coverage.csv", index=False)
            if not aoi_review.empty:
                aoi_review.to_csv(output / "00_preflight_aoi_review.csv", index=False)
            if not mu_review.empty:
                mu_review.to_csv(output / "00_preflight_mu_review.csv", index=False)
            info("")
            info("No AOI-ready RoWs selected.")
            if waiting_rows:
                info("Add Polygon AOI file(s) for: " + ", ".join(waiting_rows))
            info("Preflight boundary/cache has been preserved.")
            info("Add the AOI and run the BAT again; lasboundary will be reused.")
            info(f"Selection file: {sel_path}")
            _archive_qa_run(
                output, "00_preflight", selected_rows=[],
                notes={"status": "WAITING_FOR_AOI"}
            )
            return sel_path

        chosen = table[table["ROW"].isin(selected_rows)].copy()
        low = chosen[chosen["COVERAGE"] < float(low_coverage_warning)]
        if interactive and not low.empty:
            for _, r in low.iterrows():
                ans = input(f"WARNING: {r['ROW']} coverage is {100*float(r['COVERAGE']):.1f}%. Process anyway? [y/N]: ").strip().lower()
                if ans not in {"y", "yes"}:
                    selected_rows = [x for x in selected_rows if x != txt(r["ROW"])]
        if not selected_rows:
            raise RuntimeError("No RoWs remain selected")

        selection = {
            "version": 2,
            "status": "READY",
            "epsg": int(epsg),
            "capture_year": int(year),
            "aoi_dir": str(aoi_dir),
            "selected_rows": selected_rows,
            "rows": [
                {
                    "row": txt(r["ROW"]),
                    "state": txt(r["STATE"]),
                    "coverage": float(r["COVERAGE"]),
                    "coverage_pct": float(r["COVERAGE_PCT"]),
                    "coverage_source": txt(r["COVERAGE_SOURCE"]),
                    "mu_coverage_pct": 100.0*float(r.get("MU_COVERAGE", np.nan)),
                    "aoi_coverage_pct": float(r.get("AOI_COVERAGE_PCT", np.nan)),
                    "lidar_inside_aoi_pct": float(r.get("LIDAR_INSIDE_AOI_PCT", np.nan)),
                    "candidate_lidar_in_aoi_pct": float(r.get("CANDIDATE_LIDAR_IN_AOI_PCT", np.nan)),
                    "action": txt(r.get("ACTION")),
                    "aoi_file": txt(r["AOI_FILE"]),
                    "aoi_source": txt(r["AOI_SOURCE"]),
                    "point_mode": txt(r.get("POINT_MODE")),
                    "n_input_tiles": int(r.get("N_INPUT_TILES", 0)),
                    "n_shared_tiles": int(r.get("N_SHARED_TILES", 0)),
                    "n_exclusive_tiles": int(r.get("N_EXCLUSIVE_TILES", 0)),
                }
                for _, r in table[table["ROW"].isin(selected_rows)].iterrows()
            ],
        }
        sel_path = output / "00_preflight_selection.json"
        sel_path.write_text(json.dumps(selection, indent=2), encoding="utf-8")
        table.to_csv(output / "00_preflight_coverage.csv", index=False)
        if not aoi_review.empty:
            aoi_review.to_csv(output / "00_preflight_aoi_review.csv", index=False)
        if not mu_review.empty:
            mu_review.to_csv(output / "00_preflight_mu_review.csv", index=False)
        info("\nSelected RoWs: " + ", ".join(selected_rows))
        info(f"Selection file: {sel_path}")
        _archive_qa_run(
            output, "00_preflight", selected_rows=selected_rows,
            notes={"status": "READY"}
        )
        return sel_path


def load_preflight_selection(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"selected_rows": []}
    return json.loads(path.read_text(encoding="utf-8"))


# -----------------------------------------------------------------------------
# Structure position v12
# -----------------------------------------------------------------------------

_EVERSOURCE_HFRAME_CODES = {
    "SCSH",   # single-circuit steel H-frame
    "SCWH",   # single-circuit wood H-frame
    "SCHF",   # single-circuit H-frame
    "DCSH",   # double-circuit steel H-frame
    "DCHF",   # double-circuit H-frame
    "LSCHF",  # legacy/special single-circuit H-frame code
}

_EVERSOURCE_SINGLE_POLE_CODES = {
    "SCSP",
    "SCWP",
    "DCSP",
    "DCWP",
}


def _normalise_structure_type(value) -> str:
    return re.sub(r"\s+", " ", txt(value).strip().upper())


def _structure_type_tokens(value) -> set[str]:
    st = _normalise_structure_type(value)
    return {
        token for token in re.split(r"[^A-Z0-9]+", st)
        if token
    }


def _is_hframe_type(value) -> bool:
    st = _normalise_structure_type(value)
    if not st:
        return False
    if re.search(r"(?i)h[-_ ]?frame", st):
        return True

    tokens = _structure_type_tokens(st)

    # Ambiguous source strings such as "SCSP / SCHF" should not override a
    # numeric pole-count field. They are deliberately left unresolved here.
    has_h = any(t in _EVERSOURCE_HFRAME_CODES for t in tokens)
    has_single = any(t in _EVERSOURCE_SINGLE_POLE_CODES for t in tokens)
    if has_h and has_single:
        return False

    if has_h:
        return True

    # Common coded variants such as SCHF-WP remain H-frame types.
    for token in tokens:
        if token.startswith(("SCHF", "DCHF")):
            return True
    return False


def _is_single_pole_type(value) -> bool:
    st = _normalise_structure_type(value)
    if not st:
        return False
    if re.search(r"(?i)(?:single[-_ ]?pole|monopole)", st):
        return True
    tokens = _structure_type_tokens(st)
    has_h = any(t in _EVERSOURCE_HFRAME_CODES for t in tokens)
    has_single = any(t in _EVERSOURCE_SINGLE_POLE_CODES for t in tokens)
    return bool(has_single and not has_h)


def _source_expected_poles(client_row) -> int | None:
    if client_row is None:
        return None

    # Explicit numeric source data wins when available.
    for name in ("NO_OF_POLE", "ESTPOLECOU"):
        if name in client_row.index:
            v = client_row.get(name)
            try:
                if pd.notna(v):
                    n = int(round(float(v)))
                    if 1 <= n <= 10:
                        return n
            except Exception:
                pass

    st = txt(client_row.get("STR_TYPE")) if "STR_TYPE" in client_row.index else ""
    if _is_hframe_type(st):
        return 2
    if re.search(r"(?i)(?:3[-_ ]?pole|three[-_ ]?pole)", st):
        return 3
    if _is_single_pole_type(st):
        return 1
    return None


def _source_structure_kind(client_row) -> str:
    st = (
        txt(client_row.get("STR_TYPE"))
        if client_row is not None and "STR_TYPE" in client_row.index
        else ""
    )
    if _is_hframe_type(st):
        return "H_FRAME"
    if re.search(r"(?i)(?:3[-_ ]?pole|three[-_ ]?pole)", st):
        return "THREE_POLE"
    if _is_single_pole_type(st):
        return "SINGLE"
    return ""


def _member_xy(members: list[pd.Series]) -> np.ndarray:
    return np.asarray([[float(r.geometry.x), float(r.geometry.y)] for r in members], dtype=float)


def _pca_axis(xy: np.ndarray):
    centre = np.mean(xy, axis=0)
    if len(xy) < 2:
        return centre, np.asarray([1.0, 0.0]), np.zeros(len(xy)), np.zeros(len(xy))
    q = xy - centre
    cov = q.T @ q
    vals, vecs = np.linalg.eigh(cov)
    axis = vecs[:, int(np.argmax(vals))]
    axis = axis / max(float(np.linalg.norm(axis)), 1e-12)
    normal = np.asarray([-axis[1], axis[0]])
    along = q @ axis
    cross = q @ normal
    return centre, axis, along, cross


def _best_structural_triplet(members: list[pd.Series], unit_to_m: float, client_point: Point | None = None):
    """Find the most plausible 3-member structural row among 3+ markers."""
    if len(members) < 3:
        return None
    best = None
    for inds in itertools.combinations(range(len(members)), 3):
        subset = [members[i] for i in inds]
        xy = _member_xy(subset)
        centre, axis, along, cross = _pca_axis(xy)
        order = np.argsort(along)
        s = along[order]
        span_m = float(s[-1] - s[0]) * unit_to_m
        if span_m < 0.75:
            continue
        residual_m = float(np.max(np.abs(cross))) * unit_to_m
        gap1 = float(s[1] - s[0]) * unit_to_m
        gap2 = float(s[2] - s[1]) * unit_to_m
        symmetry = abs(gap1 - gap2) / max(gap1 + gap2, 1e-9)
        mid_i = int(order[1])
        mid_pt = subset[mid_i].geometry
        client_penalty = 0.0
        if client_point is not None and not client_point.is_empty:
            client_penalty = float(Point(mid_pt.x, mid_pt.y).distance(client_point)) * unit_to_m
        # Residual dominates; symmetry then client proximity are weak tie-breaks.
        score = 3.0 * residual_m + 2.0 * symmetry + 0.05 * client_penalty - 0.02 * min(span_m, 30.0)
        rec = (score, inds, subset[mid_i], residual_m, symmetry, span_m)
        if best is None or rec[0] < best[0]:
            best = rec
    return best


def _nearest_member(members: list[pd.Series], target: Point | None):
    if not members:
        return None
    if target is None or target.is_empty:
        return members[0]
    return min(members, key=lambda r: Point(float(r.geometry.x), float(r.geometry.y)).distance(target))



def _support_tangent_xy(support_row: pd.Series, local_geom=None) -> np.ndarray | None:
    """Best available local line tangent for structure-member pairing."""
    try:
        tx = float(support_row.get("TAN_X", np.nan))
        ty = float(support_row.get("TAN_Y", np.nan))
        if np.isfinite(tx) and np.isfinite(ty):
            v = np.asarray([tx, ty], dtype=float)
            n = float(np.linalg.norm(v))
            if n > 1e-9:
                return v / n
    except Exception:
        pass

    if local_geom is not None and not local_geom.is_empty:
        try:
            if local_geom.geom_type == "LineString" and local_geom.length > 1e-9:
                a = local_geom.interpolate(0.0)
                b = local_geom.interpolate(local_geom.length)
                v = np.asarray([b.x - a.x, b.y - a.y], dtype=float)
                n = float(np.linalg.norm(v))
                if n > 1e-9:
                    return v / n
        except Exception:
            pass
    return None


def _find_hframe_companion_marker(
    base_member: pd.Series,
    raw_points: gpd.GeoDataFrame,
    support_row: pd.Series,
    local_geom,
    unit_to_m: float,
    min_separation_m: float = 8.0,
    max_separation_m: float = 60.0,
    min_cross_m: float = 10.0,
    max_along_m: float = 30.0,
    min_angle_deg: float = 40.0,
):
    """Find the opposite physical H-frame leg from the full contractor marker set.

    The original 15 m support clustering is intentionally left unchanged. Some
    real Eversource H-frames are wider than that, so their two top markers become
    separate topology supports. For an explicitly identified H-frame, search the
    complete contractor marker set for the nearest plausible opposite leg.

    A companion must be:
      - sufficiently separated from the current marker;
      - mostly transverse to the local line direction;
      - close to the same along-line station.

    This prevents a global cluster-radius increase from joining adjacent lines.
    """
    if raw_points is None or raw_points.empty:
        return None

    tangent = _support_tangent_xy(support_row, local_geom)
    if tangent is None:
        return None

    p = base_member.geometry
    pxy = np.asarray([float(p.x), float(p.y)], dtype=float)
    pz = (
        float(p.z)
        if getattr(p, "has_z", False)
        else float(base_member.get("Z", np.nan))
    )
    base_id = txt(base_member.get("POINT_ID"))

    candidates = []
    for _, cand in raw_points.iterrows():
        cand_id = txt(cand.get("POINT_ID"))
        if base_id and cand_id == base_id:
            continue

        q = cand.geometry
        qxy = np.asarray([float(q.x), float(q.y)], dtype=float)
        dv = qxy - pxy
        dist_src = float(np.linalg.norm(dv))
        if dist_src <= 1e-9:
            continue

        dist_m = dist_src * unit_to_m
        if dist_m < min_separation_m or dist_m > max_separation_m:
            continue

        along_m = abs(float(np.dot(dv, tangent))) * unit_to_m
        cross_m = abs(
            float(tangent[0] * dv[1] - tangent[1] * dv[0])
        ) * unit_to_m

        if cross_m < min_cross_m or along_m > max_along_m:
            continue

        angle_deg = float(np.degrees(np.arctan2(cross_m, max(along_m, 1e-9))))
        if angle_deg < min_angle_deg:
            continue

        qz = (
            float(q.z)
            if getattr(q, "has_z", False)
            else float(cand.get("Z", np.nan))
        )
        z_delta_m = (
            abs(qz - pz) * unit_to_m
            if np.isfinite(qz) and np.isfinite(pz)
            else 0.0
        )

        # Prefer the nearest same-station transverse marker. Z is only a weak
        # tie-break because attachment heights can legitimately differ.
        score = (
            dist_m
            + 2.0 * along_m
            + 0.20 * z_delta_m
            + 0.20 * (90.0 - angle_deg)
        )

        candidates.append({
            "score": float(score),
            "row": cand,
            "distance_m": float(dist_m),
            "along_m": float(along_m),
            "cross_m": float(cross_m),
            "angle_deg": float(angle_deg),
            "z_delta_m": float(z_delta_m),
        })

    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            x["score"],
            x["distance_m"],
            txt(x["row"].get("POINT_ID")),
        )
    )
    return candidates[0]


def derive_structure_xy(
    members: list[pd.Series],
    support_row: pd.Series,
    local_geom,
    client_row,
    unit_to_m: float,
    raw_points: gpd.GeoDataFrame | None = None,
):
    """Derive physical Structure XY from local contractor/topology geometry only.

    IMPORTANT:
    Client/source STR_TYPE, NO_OF_POLE and related attributes are QA/reference
    metadata only. They do not control the geometry decision.

    Geometry rules:
      0 local markers -> topology fallback / review
      1 local marker  -> exact original DXF XY
      2 local markers -> midpoint
      3 local markers -> structural middle member
      >3 markers      -> best structural triplet + outlier handling

    Client XY remains a conservative fallback only for ambiguous/review cases,
    never for a confirmed local DXF geometry rule.
    """
    client_pt = None
    if (
        client_row is not None
        and getattr(client_row, "geometry", None) is not None
        and not client_row.geometry.is_empty
    ):
        client_pt = Point(
            float(client_row.geometry.x),
            float(client_row.geometry.y),
        )

    # Source fields remain useful for QA, but must not steer geometry.
    expected = _source_expected_poles(client_row)
    source_kind = _source_structure_kind(client_row)

    n = len(members)
    n_struct = n
    n_out = 0
    conf = "HIGH"
    top_id = ""
    top_z = np.nan
    source = "CONTRACTOR"
    geom_type = ""
    type_match = ""

    # ------------------------------------------------------------------
    # 0 markers: topology fallback only.
    # ------------------------------------------------------------------
    if n == 0:
        p = Point(
            float(support_row.geometry.x),
            float(support_row.geometry.y),
        )
        rule = "TOPOLOGY_FALLBACK"
        conf = "REVIEW"
        source = "TOPOLOGY"
        n_struct = 0
        geom_type = "NO_DXF_MARKER"

    # ------------------------------------------------------------------
    # 1 marker: exact contractor DXF XY. Never move it because source says
    # H-frame/single-pole/etc.
    # ------------------------------------------------------------------
    elif n == 1:
        chosen = members[0]
        p = Point(
            float(chosen.geometry.x),
            float(chosen.geometry.y),
        )
        top_id = txt(chosen.get("POINT_ID"))
        top_z = (
            float(chosen.geometry.z)
            if getattr(chosen.geometry, "has_z", False)
            else float(chosen.get("Z", np.nan))
        )
        rule = "SINGLE_DXF_MARKER"
        n_struct = 1
        n_out = 0
        conf = "HIGH"
        geom_type = "SINGLE_MARKER"

    # ------------------------------------------------------------------
    # 2 local markers: same topology support -> physical two-member structure.
    # Use midpoint. No source STR_TYPE required.
    # ------------------------------------------------------------------
    elif n == 2:
        a, b = members[0], members[1]
        p = Point(
            (float(a.geometry.x) + float(b.geometry.x)) / 2.0,
            (float(a.geometry.y) + float(b.geometry.y)) / 2.0,
        )
        top_id = ";".join(
            x for x in (
                txt(a.get("POINT_ID")),
                txt(b.get("POINT_ID")),
            )
            if x
        )
        za = (
            float(a.geometry.z)
            if getattr(a.geometry, "has_z", False)
            else float(a.get("Z", np.nan))
        )
        zb = (
            float(b.geometry.z)
            if getattr(b.geometry, "has_z", False)
            else float(b.get("Z", np.nan))
        )
        zvals = [z for z in (za, zb) if np.isfinite(z)]
        top_z = float(np.mean(zvals)) if zvals else np.nan

        rule = "TWO_MEMBER_MIDPOINT"
        n_struct = 2
        n_out = 0
        conf = "HIGH"
        geom_type = "TWO_MEMBER"

    # ------------------------------------------------------------------
    # 3+ local markers: select physical structural triplet and middle member.
    # Existing triplet scoring handles 3-pole and 3+outlier/tension cases.
    # ------------------------------------------------------------------
    else:
        best = _best_structural_triplet(
            members,
            unit_to_m,
            client_pt,
        )

        if best is not None:
            score, inds, chosen, residual_m, symmetry, span_m = best
            p = Point(
                float(chosen.geometry.x),
                float(chosen.geometry.y),
            )
            top_id = txt(chosen.get("POINT_ID"))
            top_z = (
                float(chosen.geometry.z)
                if getattr(chosen.geometry, "has_z", False)
                else float(chosen.get("Z", np.nan))
            )
            n_struct = 3
            n_out = max(0, n - 3)
            rule = (
                "THREE_MEMBER_MIDDLE"
                if n == 3
                else "THREE_MEMBER_WITH_OUTLIER"
            )
            geom_type = (
                "THREE_MEMBER"
                if n == 3
                else "THREE_MEMBER_PLUS_OUTLIER"
            )
            conf = (
                "HIGH"
                if residual_m <= 0.75 and symmetry <= 0.45
                else "MEDIUM"
            )
            if residual_m > 2.0 or symmetry > 0.8:
                conf = "REVIEW"
        else:
            # Ambiguous multi-marker support. Prefer the closest local contractor
            # marker to the client/reference position if available, otherwise to
            # the local wire/topology context. This is explicitly QA-reviewable.
            target = (
                client_pt
                if client_pt is not None
                else (
                    local_geom.centroid
                    if local_geom is not None
                    else support_row.geometry
                )
            )
            chosen = _nearest_member(members, target)
            p = Point(
                float(chosen.geometry.x),
                float(chosen.geometry.y),
            )
            top_id = txt(chosen.get("POINT_ID"))
            top_z = (
                float(chosen.geometry.z)
                if getattr(chosen.geometry, "has_z", False)
                else float(chosen.get("Z", np.nan))
            )
            rule = "AMBIGUOUS_MARKER_FALLBACK"
            conf = "REVIEW"
            geom_type = "AMBIGUOUS_MULTI_MARKER"

    # ------------------------------------------------------------------
    # Source-vs-geometry QA only. Never drives the chosen point.
    # ------------------------------------------------------------------
    if source_kind:
        if source_kind == "H_FRAME":
            type_match = (
                "MATCH"
                if geom_type == "TWO_MEMBER"
                else "REVIEW"
            )
        elif source_kind == "THREE_POLE":
            type_match = (
                "MATCH"
                if geom_type in {"THREE_MEMBER", "THREE_MEMBER_PLUS_OUTLIER"}
                else "REVIEW"
            )
        elif source_kind == "SINGLE":
            type_match = (
                "MATCH"
                if geom_type == "SINGLE_MARKER"
                else "REVIEW"
            )
        else:
            type_match = ""
    else:
        type_match = ""

    # Client XY is diagnostic. Only ambiguous/no-marker cases may fall back to
    # client XY, never a 1/2/3-member geometry derived from local DXF markers.
    client_used = False
    client_delta_m = np.nan
    if client_pt is not None:
        client_delta_m = float(p.distance(client_pt)) * unit_to_m
        if (
            conf == "REVIEW"
            and rule in {"TOPOLOGY_FALLBACK", "AMBIGUOUS_MARKER_FALLBACK"}
            and client_delta_m <= 3.0
        ):
            p = client_pt
            source = "CLIENT"
            rule = "CLIENT_PREFERRED_REVIEW"
            client_used = True
            conf = "MEDIUM"

    wire_dist = (
        float(p.distance(local_geom)) * unit_to_m
        if local_geom is not None and not local_geom.is_empty
        else np.nan
    )

    return {
        "geometry": p,
        "POS_RULE": rule,
        "POS_SOURCE": source,
        "POS_CONF": conf,
        "GEOM_TYPE": geom_type,
        "SOURCE_TYPE": source_kind,
        "TYPE_MATCH": type_match,
        "EXPECTED_POLES": expected if expected is not None else np.nan,
        "SOURCE_KIND": source_kind,
        "N_TOP_CAND": n,
        "N_STRUCTURAL": int(n_struct),
        "N_OUTLIERS": int(n_out),
        "CLIENT_USED": int(client_used),
        "CLIENT_X": float(client_pt.x) if client_pt is not None else np.nan,
        "CLIENT_Y": float(client_pt.y) if client_pt is not None else np.nan,
        "CLIENT_DELTA_M": client_delta_m,
        "CLIENT_DX_M": (
            (float(client_pt.x) - float(p.x)) * unit_to_m
            if client_pt is not None and not client_used
            else np.nan
        ),
        "CLIENT_DY_M": (
            (float(client_pt.y) - float(p.y)) * unit_to_m
            if client_pt is not None and not client_used
            else np.nan
        ),
        "WIRE_DIST_M": wire_dist,
        "TOP_POINT_ID": top_id,
        "TOP_X": float(p.x),
        "TOP_Y": float(p.y),
        "TOP_Z": top_z,
    }



def _route_records_for_circuit(path: dict[str, Any]) -> list[dict[str, Any]]:
    routes = [{
        "PATH_ROLE": "MAIN",
        "BRANCH_ID": "",
        "SEGMENT_ID": "MAIN",
        "NODES": list(path.get("NODES", [])),
        "BUNDLES": list(path.get("BUNDLES", [])),
    }]
    for branch in path.get("BRANCHES", []) or []:
        routes.append({
            "PATH_ROLE": "TAP_BRANCH",
            "BRANCH_ID": txt(branch.get("BRANCH_ID")),
            "SEGMENT_ID": txt(branch.get("SEGMENT_ID")),
            "ANCHOR_NODE": txt(branch.get("ANCHOR_NODE")),
            "NODES": list(branch.get("NODES", [])),
            "BUNDLES": list(branch.get("BUNDLES", [])),
        })
    return routes


def _multipart_circuit_geometry(route_geometries: list):
    lines = []
    for geom in route_geometries:
        if geom is None or geom.is_empty:
            continue
        if geom.geom_type == "LineString":
            lines.append(geom)
        elif geom.geom_type == "MultiLineString":
            lines.extend(list(geom.geoms))
        elif geom.geom_type == "GeometryCollection":
            lines.extend([
                g for g in geom.geoms
                if g.geom_type == "LineString"
            ])

    if not lines:
        return None
    if len(lines) == 1:
        return lines[0]
    return MultiLineString([list(g.coords) for g in lines])


def _add_node_local_geometry(store, nodes, selected):
    for node_index, sid in enumerate(nodes):
        local_geom, _ = local_circuit_node_geometry(
            selected, node_index
        )
        if local_geom is not None and not local_geom.is_empty:
            store[sid].append(local_geom)


def _combine_local_geometries(geoms):
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    try:
        return unary_union(geoms)
    except Exception:
        return geoms[0]


def build_final_products(
    paths,
    bundles,
    supports,
    raw_points,
    client,
    kmz,
    client_structure_index,
    unit_to_m,
    max_join_review_m,
    wire_simplify_m,
):
    """Build formal circuit geometry with MAIN and accepted TAP_BRANCH parts."""
    blookup = {int(b["BUNDLE_ID"]): b for b in bundles}
    by_sid = support_lookup(supports)
    member_lookup = structure_members_by_topology(raw_points)

    wire_rows = []
    span_rows = []
    structure_rows = []
    discard_phase = []
    join_rows = []

    for ckey, path in sorted(
        paths.items(),
        key=lambda kv: kv[1]["CIRCUIT"],
    ):
        circuit = path["CIRCUIT"]
        voltage, company, gid = circuit_attributes(
            ckey, circuit, client, kmz
        )
        krec = kmz.get(ckey)

        route_outputs = []
        node_local_geoms = defaultdict(list)
        node_roles = defaultdict(set)

        row_names = []
        all_joins = []
        nodes_before_total = 0
        nodes_after_total = 0
        total_spans = 0
        tap_spans = 0

        for route in _route_records_for_circuit(path):
            if not route["BUNDLES"]:
                continue

            selected, joins = select_phase_sequence(
                route, blookup, supports, unit_to_m
            )
            if not selected:
                continue

            selected, nodes_before, nodes_after = (
                simplify_selected_phase_sequence(
                    selected,
                    float(wire_simplify_m) / unit_to_m,
                )
            )

            route_geom = concatenate_selected(selected)
            if route_geom is None:
                continue

            route_outputs.append({
                **route,
                "selected": selected,
                "joins": joins,
                "geometry": route_geom,
            })

            nodes_before_total += int(nodes_before)
            nodes_after_total += int(nodes_after)
            all_joins.extend(joins)
            total_spans += len(selected)
            if route["PATH_ROLE"] == "TAP_BRANCH":
                tap_spans += len(selected)

            _add_node_local_geometry(
                node_local_geoms,
                route["NODES"],
                selected,
            )
            for sid in route["NODES"]:
                node_roles[sid].add(route["PATH_ROLE"])

            for i, (bid, sel) in enumerate(
                zip(route["BUNDLES"], selected)
            ):
                b = blookup[bid]
                s0 = route["NODES"][i]
                s1 = route["NODES"][i + 1]

                geom2 = LineString([
                    (float(x[0]), float(x[1]))
                    for x in sel["coords_oriented"]
                ])
                row_name, row_dist = choose_row_for_geom(
                    geom2, krec, unit_to_m
                )
                if row_name:
                    row_names.append(row_name)

                span_rows.append({
                    "ROW": row_name,
                    "CIRCUIT": circuit,
                    "VOLTAGE": voltage,
                    "COMPANY": company,
                    "GLOBALID": gid,
                    "PATH_ROLE": route["PATH_ROLE"],
                    "BRANCH_ID": route.get("BRANCH_ID", ""),
                    "SEGMENT_ID": route.get("SEGMENT_ID", ""),
                    "SEQ": i + 1,
                    "BUNDLE_ID": bid,
                    "STRUCT_A": s0,
                    "STRUCT_B": s1,
                    "WIRE_ID": sel["WIRE_ID"],
                    "ARRANGEMENT": b["ARRANGEMENT"],
                    "ATTR_CONF": txt(b.get("CONF")),
                    "SELECT_METHOD": (
                        "HORIZONTAL_CENTRAL"
                        if b["ARRANGEMENT"] == "HORIZONTAL"
                        else "VERTICAL_CONTINUITY"
                    ),
                    "ROW_DIST_M": row_dist,
                    "LENGTH_M": float(geom2.length) * unit_to_m,
                    "geometry": geom2,
                })

                for cand in b["CANDIDATES"]:
                    if int(cand["WIRE_ROW"]) != int(sel["WIRE_ROW"]):
                        discard_phase.append({
                            "CIRCUIT": circuit,
                            "BUNDLE_ID": bid,
                            "WIRE_ID": cand["WIRE_ID"],
                            "PATH_ROLE": route["PATH_ROLE"],
                            "BRANCH_ID": route.get("BRANCH_ID", ""),
                            "REASON": "NON_SELECTED_PHASE",
                            "geometry": cand["geometry3d"],
                        })

                if i > 0:
                    j = joins[i - 1]
                    join_rows.append({
                        "CIRCUIT": circuit,
                        "PATH_ROLE": route["PATH_ROLE"],
                        "BRANCH_ID": route.get("BRANCH_ID", ""),
                        "AT_STRUCTURE": s0,
                        "JOIN_M": j,
                        "REVIEW": int(j > max_join_review_m),
                        "geometry": Point(
                            float(by_sid[s0].geometry.x),
                            float(by_sid[s0].geometry.y),
                        ),
                    })

        if not route_outputs:
            continue

        full_geom = _multipart_circuit_geometry([
            r["geometry"] for r in route_outputs
        ])
        if full_geom is None:
            continue

        route_length_source = sum(
            r["geometry"].length for r in route_outputs
        )
        n_taps = len({
            txt(r.get("BRANCH_ID"))
            for r in route_outputs
            if r["PATH_ROLE"] == "TAP_BRANCH"
            and txt(r.get("BRANCH_ID"))
        })

        wire_rows.append({
            "ROW": ";".join(sorted(set(row_names))),
            "CIRCUIT": circuit,
            "VOLTAGE": voltage,
            "COMPANY": company,
            "GLOBALID": gid,
            "N_SPANS": int(total_spans),
            "N_MAIN_SP": int(total_spans - tap_spans),
            "N_TAP_SP": int(tap_spans),
            "N_TAPS": int(n_taps),
            "LENGTH_M": float(route_length_source) * unit_to_m,
            "LENGTH_FT": (
                float(route_length_source)
                * unit_to_m
                / 0.30480060960121924
            ),
            "NODES_BEFORE": int(nodes_before_total),
            "NODES_AFTER": int(nodes_after_total),
            "MAX_JOIN_M": max(all_joins) if all_joins else 0.0,
            "geometry": full_geom,
        })

        # Every Structure node on MAIN or an accepted tap is delivered once.
        all_nodes = []
        seen_nodes = set()
        for route in route_outputs:
            for sid in route["NODES"]:
                if sid not in seen_nodes:
                    seen_nodes.add(sid)
                    all_nodes.append(sid)

        for sid in all_nodes:
            srow = by_sid[sid]
            local_geom = _combine_local_geometries(
                node_local_geoms.get(sid, [])
            )
            members = member_lookup.get(sid, [])

            if members:
                prelim_xy = np.mean(_member_xy(members), axis=0)
                prelim = Point(
                    float(prelim_xy[0]),
                    float(prelim_xy[1]),
                )
            else:
                prelim = Point(
                    float(srow.geometry.x),
                    float(srow.geometry.y),
                )

            src_row, _, src_method = match_client_structure(
                prelim,
                circuit,
                client_structure_index,
                unit_to_m,
            )

            loc = derive_structure_xy(
                members,
                srow,
                local_geom,
                src_row,
                unit_to_m,
                raw_points=raw_points,
            )
            p2 = loc["geometry"]

            src_row_final, src_dist, src_method_final = (
                match_client_structure(
                    p2,
                    circuit,
                    client_structure_index,
                    unit_to_m,
                )
            )
            if src_row_final is not None:
                src_row = src_row_final
                src_method = src_method_final

            row_name, row_dist = choose_row_for_point(
                p2, krec, unit_to_m
            )
            sval = client_structure_values(src_row, sid)
            source_attrs = client_structure_all_values(
                src_row, client_structure_index
            )

            roles = node_roles.get(sid, set())
            if "MAIN" in roles and "TAP_BRANCH" in roles:
                structure_role = "MAIN_TAP_JUNCTION"
            elif "TAP_BRANCH" in roles:
                structure_role = "TAP_BRANCH"
            else:
                structure_role = "MAIN"

            structure_rows.append({
                "ROW": row_name,
                "CIRCUIT": circuit,
                "VOLTAGE": voltage,
                "COMPANY": company,
                "STRUCT_ID": sval["STRUCT_ID"],
                "TOPO_ID": sid,
                "PATH_ROLE": structure_role,
                **source_attrs,
                "SRC_GID": sval["SRC_GID"],
                "SRC_LINE": sval["SRC_LINE"],
                "SRC_STRUCT": sval["SRC_STRUCT"],
                "SRC_X": sval["SRC_X"],
                "SRC_Y": sval["SRC_Y"],
                "SRC_DIST_M": src_dist,
                "SRC_MATCH": src_method,
                "ROW_DIST_M": row_dist,
                **{
                    k: v
                    for k, v in loc.items()
                    if k != "geometry"
                },
                "GROUND_Z": np.nan,
                "Z_SOURCE": "PENDING_LIDAR",
                "geometry": p2,
            })

    return (
        rows_to_gdf(wire_rows, supports.crs),
        rows_to_gdf(span_rows, supports.crs),
        rows_to_gdf(structure_rows, supports.crs),
        rows_to_gdf(discard_phase, supports.crs),
        rows_to_gdf(join_rows, supports.crs),
    )


# -----------------------------------------------------------------------------
# Geometry stage
# -----------------------------------------------------------------------------

def discover_geometry_inputs():
    root=package_root(); inp=root/"input"; src=root/"source"
    if not inp.exists(): raise RuntimeError(f"Missing input folder: {inp}")
    if not src.exists(): raise RuntimeError(f"Missing source folder: {src}")
    wires,points=discover_two_dxfs(inp)
    lidar=discover_lidar_input(inp)
    client=find_single_file(src,".zip","client source ZIP")
    kmz=find_single_file(src,".kmz","Project SoW KMZ")
    return wires,points,lidar,client,kmz



FORMAL_STRUCTURE_DELIVERY_FIELDS = [
    "ROW",
    "CIRCUIT",
    "VOLTAGE",
    "COMPANY",
    "STRUCT_ID",
    "SRC_COMP",
    "MATERIAL",
    "YEAR_CONST",
    "STR_TYPE",
    "GLOBALID",
]


def structure_delivery_columns(
    gdf: gpd.GeoDataFrame,
    source_fields: list[str] | None = None,
) -> list[str]:
    """Minimal formal Structure schema shared by Circuit and Caydence.

    Full matched client/source attribution remains available in internal QA
    products (01_eversource_geometry.gpkg and supporting CSVs). Formal client
    Structure shapefiles intentionally expose only the agreed delivery fields.
    """
    ordered = [
        field
        for field in FORMAL_STRUCTURE_DELIVERY_FIELDS
        if field in gdf.columns
    ]
    ordered.append("geometry")
    return ordered


def write_geometry_delivery_products(
    final_wires: gpd.GeoDataFrame,
    structures_final: gpd.GeoDataFrame,
    cayd_w: gpd.GeoDataFrame,
    cayd_structures: gpd.GeoDataFrame,
    selected_rows: set[str],
    year: int,
    deliverables: Path,
    structure_source_fields: list[str] | None = None,
) -> pd.DataFrame:
    """Publish Stage 01 geometry.

    Formal 2D Wires/Structures remain circuit products.

    Caydence 3D Wires are a RoW-level, lossless repackaging of the original DXF.
    Caydence 3D Structures are the final circuit 3D Structure product merged
    into one RoW-level shapefile.
    """
    deliverables.mkdir(parents=True, exist_ok=True)
    inv = []

    effective_rows = set(selected_rows)
    if not effective_rows and structures_final is not None and not structures_final.empty:
        effective_rows = {
            txt(v)
            for v in structures_final.get("ROW", pd.Series(dtype=object))
            if txt(v)
        }
    if not effective_rows and cayd_w is not None and not cayd_w.empty:
        effective_rows = {
            txt(v)
            for v in cayd_w.get("ROW", pd.Series(dtype=object))
            if txt(v)
        }

    # Refresh only geometry product families.
    for row in effective_rows:
        row_root = deliverables / safe_filename(row)
        croot = row_root / "Circuit"
        cayroot = row_root / "Caydence"
        if croot.exists():
            shutil.rmtree(croot)
        if cayroot.exists():
            shutil.rmtree(cayroot)

    # --------------------------------------------------------------
    # Formal circuit products.
    # --------------------------------------------------------------
    struct_groups = {
        txt(c): sg.copy()
        for c, sg in structures_final.groupby("CIRCUIT", sort=False)
    } if structures_final is not None and not structures_final.empty else {}

    for _, wr in final_wires.iterrows():
        circuit = txt(wr.get("CIRCUIT"))
        sg = struct_groups.get(
            circuit,
            rows_to_gdf(
                [],
                structures_final.crs
                if structures_final is not None
                else final_wires.crs,
            ),
        )

        parents = (
            set(
                txt(v)
                for v in sg.get("ROW", pd.Series(dtype=object))
                if txt(v)
            )
            & effective_rows
        )
        if not parents:
            parents = set(_row_list(wr.get("ROW"))) & effective_rows
        if not parents:
            continue

        wg = gpd.GeoDataFrame(
            [wr.to_dict()],
            geometry="geometry",
            crs=final_wires.crs,
        )

        for row in sorted(parents):
            cdir = (
                deliverables
                / safe_filename(row)
                / "Circuit"
                / safe_filename(circuit)
            )

            wout = cdir / f"{safe_filename(circuit)}_Wires_{year}.shp"
            _write_clean_shapefile(
                wg, wout,
                ["ROW", "CIRCUIT", "VOLTAGE", "geometry"],
            )
            inv.append({
                "PRODUCT": "Wires",
                "ROW": row,
                "CIRCUIT": circuit,
                "DELIVERY_FILE": wout.name,
                "STATUS": "WRITTEN",
            })

            srow = (
                sg[sg["ROW"].astype(str) == row].copy()
                if sg is not None and not sg.empty
                else sg
            )
            if srow is not None and not srow.empty:
                sout = cdir / f"{safe_filename(circuit)}_Structure_{year}.shp"
                _write_clean_shapefile(
                    srow,
                    sout,
                    structure_delivery_columns(
                        srow, structure_source_fields
                    ),
                )
                inv.append({
                    "PRODUCT": "Structures",
                    "ROW": row,
                    "CIRCUIT": circuit,
                    "DELIVERY_FILE": sout.name,
                    "STATUS": "WRITTEN",
                })

    # --------------------------------------------------------------
    # Caydence RoW products - no circuit filtering.
    # --------------------------------------------------------------
    for row in sorted(effective_rows):
        caydir = deliverables / safe_filename(row) / "Caydence"

        if cayd_w is not None and not cayd_w.empty:
            cw = cayd_w[cayd_w["ROW"].astype(str) == row].copy()
            if not cw.empty:
                o = caydir / f"{safe_filename(row)}_3D_Wires_{year}.shp"
                _write_clean_shapefile(
                    cw,
                    o,
                    [
                        "ROW",
                        "CIRCUIT",
                        "VOLTAGE",
                        "COMPANY",
                        "WIRE_TYPE",
                        "WIRE_ID",
                        "SRC_LAYER",
                        "SRC_ENT",
                        "geometry",
                    ],
                )
                inv.append({
                    "PRODUCT": "3D Wires - Caydence",
                    "ROW": row,
                    "CIRCUIT": "",
                    "DELIVERY_FILE": o.name,
                    "STATUS": "WRITTEN",
                })

        if cayd_structures is not None and not cayd_structures.empty:
            cs = cayd_structures[
                cayd_structures["ROW"].astype(str) == row
            ].copy()
            if not cs.empty:
                o = caydir / f"{safe_filename(row)}_3D_Structures_{year}.shp"
                _write_clean_shapefile(
                    cs,
                    o,
                    structure_delivery_columns(
                        cs, structure_source_fields
                    ),
                )
                inv.append({
                    "PRODUCT": "3D Structures - Caydence",
                    "ROW": row,
                    "CIRCUIT": "",
                    "DELIVERY_FILE": o.name,
                    "STATUS": "WRITTEN",
                })

    return pd.DataFrame(inv)


def run_geometry_stage(
    wires_path: Path,
    points_path: Path,
    lidar_source: Path,
    client_source: Path,
    kmz_path: Path,
    epsg: int,
    year: int,
    output: Path,
    selection_path: Path | None = None,
    marker_cluster_m: float = 1.0,
    support_cluster_m: float = 15.0,
    marker_max_length_m: float = 5.0,
    max_endpoint_m: float = 30.0,
    track_join_m: float = 25.0,
    horizontal_z_range_m: float = 2.0,
    horizontal_z_ratio: float = 0.35,
    max_join_review_m: float = 25.0,
    wire_simplify_m: float = 0.25,
):
    output.mkdir(parents=True, exist_ok=True)
    crs=CRS.from_epsg(epsg); u2m=unit_to_metre(crs)
    selection=load_preflight_selection(selection_path)
    selected_rows=set(selection.get("selected_rows",[]))

    console_stage("MIXING", "Building 2D wires, 3D structures and Caydence products")
    info("EVERSOURCE 01 - GEOMETRY")
    info(f"CRS: EPSG:{epsg}")
    if selected_rows:
        info("Selected RoWs: "+", ".join(sorted(selected_rows)))

    wires=read_dxf_wires(wires_path,crs);info(f"3D source wires: {len(wires):,}")
    raw_points=read_dxf_structure_points(points_path,crs,u2m,marker_cluster_m,marker_max_length_m)
    supports,point_to_support=build_support_sites(raw_points,u2m,support_cluster_m)
    raw_points=raw_points.copy().reset_index(drop=True)
    raw_points["TOPO_ID"]=[point_to_support.get(int(i), "") for i in raw_points.index]
    wm,wire_review=endpoint_assignment(wires,supports,u2m,max_endpoint_m)
    supports=recenter_supports_on_wire_axis(supports,wm,wires,u2m,tangent_sample_m=5.0)
    bundles,bundle_gdf,earth,_=build_span_bundles(wm,supports,u2m,horizontal_z_range_m,horizontal_z_ratio)
    if not bundles: raise RuntimeError("No valid 3-phase span bundles were built")
    track_lookup,track_pairs=build_physical_tracks(bundles,supports,u2m,track_join_m)

    with tempfile.TemporaryDirectory(prefix="eversource_geom_") as td_raw:
        td=Path(td_raw)
        client_lines,client_structures=load_client_source(client_source,td)
        if client_lines.crs is None: raise RuntimeError("Client line source has no CRS")
        if CRS.from_user_input(client_lines.crs)!=crs: client_lines=client_lines.to_crs(crs)
        if not client_structures.empty:
            if client_structures.crs is None: raise RuntimeError("Client structure source has no CRS")
            if CRS.from_user_input(client_structures.crs)!=crs: client_structures=client_structures.to_crs(crs)
        client=client_lookup(client_lines)
        csi=build_client_structure_index(client_structures)
        kmz_refs=project_kmz(parse_sow_kmz(kmz_path),crs);kmz=kmz_lookup(kmz_refs)

        track_meta,track_review=assign_tracks_to_circuits(bundles,track_lookup,wires,client,kmz,u2m)
        paths,dropped=build_circuit_paths(bundles,supports,u2m)
        final_wires,span_edges,structures_xy,phase_discarded,join_review=build_final_products(
            paths,bundles,supports,raw_points,client,kmz,csi,u2m,max_join_review_m,wire_simplify_m
        )
        if final_wires.empty: raise RuntimeError("No final circuit wires generated")
        if structures_xy.empty: raise RuntimeError("No structure XY generated")

        # Preflight controls project scope, but circuit geometry remains whole-circuit.
        # Retain only circuits touching selected RoWs when a selection exists.
        if selected_rows:
            keep_circuits=set()
            for _,r in span_edges.iterrows():
                if txt(r.get("ROW")) in selected_rows:
                    keep_circuits.add(txt(r.get("CIRCUIT")))
            final_wires=final_wires[final_wires["CIRCUIT"].astype(str).isin(keep_circuits)].copy()
            span_edges=span_edges[span_edges["CIRCUIT"].astype(str).isin(keep_circuits)].copy()
            structures_xy=structures_xy[structures_xy["CIRCUIT"].astype(str).isin(keep_circuits)].copy()

        # Caydence 3D wires are lossless source geometry enriched with the
        # analytical/reference attribution. Attribution must never filter wires.
        cayd_w,caydence_qa=build_caydence_row_products(
            wires,
            kmz_refs,
            selected_rows,
            u2m,
            bundles,
            client,
            kmz,
            structures=structures_xy,
            structure_anchor_m=15.0,
        )
        caydence_qa.to_csv(
            output/"01_caydence_completeness.csv",
            index=False
        )
        info(
            f"Caydence wire completeness: "
            f"{len(cayd_w):,}/{len(wires):,} source 3D wires"
        )

        # Structures are a geometry deliverable, so obtain final ground Z here
        # rather than waiting for the point-cloud stage.
        info("Sampling Structure ground elevations from source LiDAR...")
        structures_final,ground_qa=add_ground_elevation_from_lidar(
            structures_xy,lidar_source,crs,u2m,td
        )

        # Caydence Structures are exactly the final 3D circuit Structure product,
        # simply collected at RoW level instead of split into Circuit folders.
        cayd_structures=build_caydence_structures_by_row(
            structures_final,selected_rows
        )

        deliverables=output.parent/"deliverables"
        structure_source_fields=list(
            csi.get("delivery_source_fields", [])
        )
        geom_inventory=write_geometry_delivery_products(
            final_wires,
            structures_final,
            cayd_w,
            cayd_structures,
            selected_rows,
            year,
            deliverables,
            structure_source_fields=structure_source_fields,
        )
        geom_inventory.to_csv(
            output/"01_geometry_delivery_inventory.csv",
            index=False,
        )

        # Explicit audit of how every source Structure field appears in the
        # Shapefile deliverables. Usually names are unchanged because the source
        # is already a DBF/Shapefile; only generated-field collisions need aliases.
        pd.DataFrame(
            [
                {
                    "SOURCE_FIELD": src_name,
                    "DELIVERY_FIELD": dst_name,
                }
                for src_name, dst_name
                in csi.get("source_field_map", {}).items()
            ]
        ).to_csv(
            output/"01_structure_source_field_map.csv",
            index=False,
        )
        info(f"Geometry deliverables: {deliverables}")

        gpkg=output/"01_eversource_geometry.gpkg"
        if gpkg.exists(): gpkg.unlink()
        for g,l in [
            (final_wires,"wires_2d"),(structures_xy,"structures_xy"),(structures_final,"structures_final"),
            (ground_qa,"structure_ground_qa"),(span_edges,"selected_span_wires"),
            (cayd_w,"caydence_wires_3d"),(cayd_structures,"caydence_structures_3d"),
            (build_structure_source_match_lines(structures_xy),"structure_source_match"),
            (dropped,"discarded_branches"),(earth,"earth_wires_excluded"),
            (phase_discarded,"phase_wires_discarded"),(supports,"support_points"),
            (raw_points,"source_structure_points"),(bundle_gdf,"span_bundles"),
            (track_review,"track_assignment_review"),(track_pairs,"track_pairing"),
            (join_review,"join_review"),(wire_review,"source_wire_review"),
            (client_lines,"client_reference"),(client_structures,"client_structure_reference"),
        ]:
            write_layer(g,gpkg,l)
        kmz_rows=[{"ROW":r.row,"CIRCUIT":r.circuit,"VOLTAGE":r.voltage,"COMPANY":r.company,
                   "GLOBALID":r.globalid,"geometry":r.geometry} for r in kmz_refs if r.geometry is not None]
        write_layer(rows_to_gdf(kmz_rows,crs),gpkg,"kmz_reference")

        structures_xy.drop(columns="geometry",errors="ignore").to_csv(output/"01_structure_position_review.csv",index=False)
        shift_rows=[]
        if not structures_xy.empty and {"CIRCUIT","CLIENT_DELTA_M"}.issubset(structures_xy.columns):
            for c,sg in structures_xy.groupby("CIRCUIT",sort=False):
                d=pd.to_numeric(sg["CLIENT_DELTA_M"],errors="coerce").dropna()
                dx=pd.to_numeric(sg.get("CLIENT_DX_M",pd.Series(dtype=float)),errors="coerce").dropna()
                dy=pd.to_numeric(sg.get("CLIENT_DY_M",pd.Series(dtype=float)),errors="coerce").dropna()
                shift_rows.append({"CIRCUIT":txt(c),"N_MATCHED":int(len(d)),
                                   "MED_CLIENT_DIST_M":float(d.median()) if len(d) else np.nan,
                                   "P90_CLIENT_DIST_M":float(d.quantile(0.9)) if len(d) else np.nan,
                                   "MED_DX_M":float(dx.median()) if len(dx) else np.nan,
                                   "MED_DY_M":float(dy.median()) if len(dy) else np.nan})
        pd.DataFrame(shift_rows).to_csv(output/"01_client_structure_shift_summary.csv",index=False)
        final_wires.drop(
            columns="geometry", errors="ignore"
        ).to_csv(
            output/"01_wire_audit.csv",
            index=False,
        )
        if span_edges is not None and not span_edges.empty:
            (
                span_edges.groupby(
                    ["CIRCUIT","PATH_ROLE","BRANCH_ID"],
                    dropna=False,
                )
                .agg(
                    N_SPANS=("BUNDLE_ID","count"),
                    LENGTH_M=("LENGTH_M","sum"),
                )
                .reset_index()
                .to_csv(
                    output/"01_path_role_summary.csv",
                    index=False,
                )
            )
        info(f"Geometry GPKG: {gpkg}")
        _archive_qa_run(
            output, "01_geometry", products=["geometry"],
            selected_rows=sorted(selected_rows)
        )
        return gpkg


# -----------------------------------------------------------------------------
# Point-cloud stage: clip, overwrite CRS metadata, XYZ, final ground Z
# -----------------------------------------------------------------------------

def _delivery_compound_crs(horizontal_epsg: int):
    h = CRS.from_epsg(int(horizontal_epsg))
    if CompoundCRS is None:
        return h
    try:
        v = CRS.from_epsg(6360)  # NAVD88 height (ftUS)
        return CompoundCRS(f"{h.name} + NAVD88 height (ftUS)", components=[h, v])
    except Exception:
        return h


def _set_delivery_las_header(header, epsg: int):
    """Return a deep-copied LAS 1.4 header with delivery CRS metadata assigned."""
    from laspy.header import Version
    h=copy.deepcopy(header)
    h.set_version_and_point_format(Version(1,4), h.point_format)
    crs=_delivery_compound_crs(epsg)
    try:
        h.add_crs(crs, keep_compatibility=False)
    except TypeError:
        h.add_crs(crs)
    return h


def _point_mask_in_polygon(geom, x, y):
    # Shapely 2 fast vectorized path; fallback is intentionally slower but robust.
    try:
        import shapely
        if hasattr(shapely, "intersects_xy"):
            return np.asarray(shapely.intersects_xy(geom, x, y), dtype=bool)
        if hasattr(shapely, "contains_xy"):
            return np.asarray(shapely.contains_xy(geom, x, y), dtype=bool)
    except Exception:
        pass
    from shapely.geometry import Point as _P
    prepared = None
    try:
        from shapely.prepared import prep
        prepared = prep(geom)
    except Exception:
        prepared = None
    if prepared is not None:
        return np.asarray([prepared.intersects(_P(float(a),float(b))) for a,b in zip(x,y)],dtype=bool)
    return np.asarray([geom.intersects(_P(float(a),float(b))) for a,b in zip(x,y)],dtype=bool)


THIN_GROUND_CLASS = 2
THIN_VEG_CLASSES = {4, 5}

# NMIP26068 TerraScan macro:
#   UAT tuning based on Honi ModelKey: ~15 ft spacing / 0.3 ft tolerance
#   FnScanThinPoints("4",9997,0,0,3.000,3.000,0)
#   FnScanThinPoints("5",9997,0,0,3.000,3.000,0)
#
# TerraScan's exact ModelKey implementation is proprietary.
# Production PLS thinning therefore uses LAStools RoW-wide operations:
#   - Ground: adaptive TIN thinning across the entire RoW, +/-0.30 ftUS vertical
#             tolerance and maximum 15 ftUS spacing, with sparse processing.
#             A RoW-wide 15-ft lowest-Z grid is the fail-safe fallback.
#   - Vegetation: 3 ftUS XY thinning, keeping the highest source point.
#                 No Z bucketing is used.
#
# Full, unthinned class 2/4/5 XYZs are also written under PLS/Full Class.
PLS_GROUND_INITIAL_SPACING_FT = 15.0
PLS_GROUND_TOLERANCE_FT = 0.30
PLS_GROUND_PREGRID_FT = 1.0
PLS_GROUND_MAX_ITER = 8

PLS_VEG_XY_FT = 3.0
PLS_VEG_Z_FT = 3.0

# PLS UAT/output format and ground-neighbour processing.
PLS_XYZ_FORMAT_VERSION = 2          # X Y Z CLASS
PLS_PARTIAL_SCHEMA_VERSION = 2      # invalidates old 3-column partials
GROUND_THIN_ALGORITHM_VERSION = "lastools_rowwide_adaptive_0p30_15_sparse_v4"
GROUND_NEIGHBOUR_FT = 10.0


def _open_xyz_handles(points_dir: Path):
    """Open direct-export XYZ files, excluding classes thinned separately."""
    points_dir.mkdir(parents=True, exist_ok=True)
    handles = {}
    for cls, name in PLS_XYZ_CLASS_MAP.items():
        if int(cls) in {THIN_GROUND_CLASS, *THIN_VEG_CLASSES}:
            continue
        handles[int(cls)] = open(points_dir / name, "w", encoding="ascii", newline="\n")
    return handles


def _open_thinned_xyz_handles(points_dir: Path):
    points_dir.mkdir(parents=True, exist_ok=True)
    return {
        int(cls): open(points_dir / PLS_XYZ_CLASS_MAP[int(cls)], "w", encoding="ascii", newline="\n")
        for cls in (2, 4, 5)
    }


def _open_full_class_handles(full_dir: Path):
    """Open unthinned Ground / Medium Veg / High Veg backup XYZs."""
    full_dir.mkdir(parents=True, exist_ok=True)
    return {
        int(cls): open(full_dir / PLS_XYZ_CLASS_MAP[int(cls)], "w", encoding="ascii", newline="\n")
        for cls in (2, 4, 5)
    }


def _close_handles(handles):
    for h in handles.values():
        try:
            h.close()
        except Exception:
            pass


def _xyz_array_from_points(pts, cls_value: int) -> np.ndarray:
    try:
        cls = np.asarray(pts.classification)
    except Exception:
        return np.empty((0, 3), dtype=float)
    mask = cls == int(cls_value)
    if not np.any(mask):
        return np.empty((0, 3), dtype=float)
    return np.column_stack([
        np.asarray(pts.x)[mask],
        np.asarray(pts.y)[mask],
        np.asarray(pts.z)[mask],
    ]).astype(float, copy=False)


def _append_direct_xyz(handles, pts) -> dict[int, int]:
    """Write all non-thinned mapped classes directly."""
    try:
        cls = np.asarray(pts.classification)
    except Exception:
        return {}
    x = np.asarray(pts.x)
    y = np.asarray(pts.y)
    z = np.asarray(pts.z)
    counts = {}
    for c, h in handles.items():
        mask = cls == int(c)
        n = int(np.count_nonzero(mask))
        if not n:
            continue
        arr = np.column_stack([x[mask], y[mask], z[mask]])
        np.savetxt(h, arr, fmt="%.3f %.3f %.3f")
        counts[int(c)] = n
    return counts


def _min_point_per_grid(
    arr: np.ndarray,
    step: float,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> np.ndarray:
    """Keep the lowest Z point per globally aligned XY grid cell."""
    if arr is None or len(arr) == 0:
        return np.empty((0, 3), dtype=float)
    step = max(float(step), 1e-9)
    ix = np.floor((arr[:, 0] - float(origin_x)) / step).astype(np.int64)
    iy = np.floor((arr[:, 1] - float(origin_y)) / step).astype(np.int64)
    order = np.lexsort((arr[:, 2], iy, ix))
    ix_s, iy_s = ix[order], iy[order]
    a = arr[order]
    first = np.r_[True, (ix_s[1:] != ix_s[:-1]) | (iy_s[1:] != iy_s[:-1])]
    # Because Z is the final sort key, first in each XY cell is minimum Z.
    return a[first]


def _group_pick_lowest(
    arr: np.ndarray,
    step: float,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> np.ndarray:
    """Return indices of lowest-Z representative per globally aligned XY cell."""
    if len(arr) == 0:
        return np.empty(0, dtype=np.int64)
    step = max(float(step), 1e-9)
    ix = np.floor((arr[:, 0] - float(origin_x)) / step).astype(np.int64)
    iy = np.floor((arr[:, 1] - float(origin_y)) / step).astype(np.int64)
    order = np.lexsort((arr[:, 2], iy, ix))
    ix_s, iy_s = ix[order], iy[order]
    first = np.r_[True, (ix_s[1:] != ix_s[:-1]) | (iy_s[1:] != iy_s[:-1])]
    return order[first]


def _tin_interpolated_z(tri: Delaunay, selected_xyz: np.ndarray, query_xy: np.ndarray):
    """Vectorised linear Z interpolation on a scipy Delaunay TIN."""
    simplex = tri.find_simplex(query_xy)
    z_pred = np.full(len(query_xy), np.nan, dtype=float)
    inside = simplex >= 0
    if not np.any(inside):
        return z_pred, simplex

    s = simplex[inside]
    q = query_xy[inside]
    transforms = tri.transform[s]
    delta = q - transforms[:, 2, :]
    bary12 = np.einsum("ijk,ik->ij", transforms[:, :2, :], delta)
    bary3 = 1.0 - bary12.sum(axis=1)
    bary = np.column_stack([bary12, bary3])
    vertices = tri.simplices[s]
    zverts = selected_xyz[vertices, 2]
    z_pred[inside] = np.sum(bary * zverts, axis=1)
    return z_pred, simplex


def _thin_ground_model_key(
    arr: np.ndarray,
    initial_spacing_ft: float = PLS_GROUND_INITIAL_SPACING_FT,
    tolerance_ft: float = PLS_GROUND_TOLERANCE_FT,
    pregrid_ft: float = PLS_GROUND_PREGRID_FT,
    max_iter: int = PLS_GROUND_MAX_ITER,
    grid_origin_x: float = 0.0,
    grid_origin_y: float = 0.0,
) -> np.ndarray:
    """Approximate TerraScan ClassifyModelKey for class 2.

    UAT v6 tuning:
      * initial spacing: 15 ftUS
      * vertical tolerance: +/-0.30 ftUS
      * 10-ft neighbour halo handled by the caller
      * every tile uses the SAME global grid origin

    The previous 65-ft model was visibly too sparse and each tile also forced
    its own local hull extremes into the model.  Those tile-specific seeds could
    create density changes exactly at block boundaries.

    This version keeps the adaptive TIN approach but:
      1. aligns every pre-grid/refinement grid to one RoW-wide origin;
      2. does NOT force per-tile extreme points into the TIN;
      3. lets outside-hull samples enter naturally as refinement violations.

    The result remains an approximation of TerraScan ModelKey, but neighbouring
    blocks now use the same seed lattice and the denser UAT-requested spacing.
    """
    if arr is None or len(arr) == 0:
        return np.empty((0, 3), dtype=float)

    pts = _min_point_per_grid(
        np.asarray(arr, dtype=float),
        pregrid_ft,
        grid_origin_x,
        grid_origin_y,
    )
    if len(pts) <= 4:
        return pts

    seed_idx = _group_pick_lowest(
        pts,
        initial_spacing_ft,
        grid_origin_x,
        grid_origin_y,
    )
    selected = np.zeros(len(pts), dtype=bool)
    selected[seed_idx] = True

    for iteration in range(int(max_iter)):
        sel_idx = np.flatnonzero(selected)
        if len(sel_idx) < 3:
            break

        sel = pts[sel_idx]
        try:
            tri = Delaunay(sel[:, :2], qhull_options="QJ")
        except (QhullError, ValueError):
            break

        rem_idx = np.flatnonzero(~selected)
        if len(rem_idx) == 0:
            break
        rem = pts[rem_idx]

        z_pred, _ = _tin_interpolated_z(tri, sel, rem[:, :2])
        residual = rem[:, 2] - z_pred

        # Points outside the current TIN hull are deliberately violations:
        # they extend the hull using the same global refinement lattice rather
        # than being forced in as tile-specific edge points.
        violation = (
            (~np.isfinite(z_pred))
            | (np.abs(residual) > float(tolerance_ft))
        )
        if not np.any(violation):
            break

        viol_global = rem_idx[violation]
        viol_pts = pts[viol_global]
        refine_step = max(
            float(pregrid_ft),
            float(initial_spacing_ft) / (2.0 ** (iteration + 1)),
        )
        local_pick = _group_pick_lowest(
            viol_pts,
            refine_step,
            grid_origin_x,
            grid_origin_y,
        )
        add_idx = viol_global[local_pick]

        if len(add_idx) == 0:
            break
        before = int(selected.sum())
        selected[add_idx] = True
        if int(selected.sum()) == before:
            break

    out = pts[selected]
    if len(out):
        order = np.lexsort((out[:, 1], out[:, 0]))
        out = out[order]
    return out


def _thin_vegetation_highest(
    arr: np.ndarray,
    xy_ft: float = 3.0,
) -> np.ndarray:
    """Keep one highest real source point per XY cell.

    No Z bucketing. This matches the intended vegetation rule and the LAStools
    `lasthin -step 3 -highest` implementation used by the production path.
    """
    if arr is None or len(arr) == 0:
        return np.empty((0, 3), dtype=float)

    a = np.asarray(arr, dtype=float)
    step = max(float(xy_ft), 1e-9)
    ix = np.floor(a[:, 0] / step).astype(np.int64)
    iy = np.floor(a[:, 1] / step).astype(np.int64)

    order = np.lexsort((-a[:, 2], iy, ix))
    ix_s, iy_s = ix[order], iy[order]
    first = np.r_[
        True,
        (ix_s[1:] != ix_s[:-1]) |
        (iy_s[1:] != iy_s[:-1])
    ]
    out = a[order[first]]
    if len(out):
        s = np.lexsort((out[:, 1], out[:, 0]))
        out = out[s]
    return out


def _append_xyz_array(handle, arr: np.ndarray) -> int:
    if arr is None or len(arr) == 0:
        return 0
    np.savetxt(handle, np.asarray(arr, dtype=float), fmt="%.3f %.3f %.3f")
    return int(len(arr))



def find_las2las_exe(explicit: Path | None = None) -> Path:
    """Locate LAStools las2las.exe used for authoritative delivery header assignment.

    Resolution order:
      1. explicit --las2las path
      2. LAS2LAS_EXE environment variable
      3. package-local tools/scripts locations
      4. PATH
      5. common C:\\LAStools locations
    """
    import os

    candidates = []
    if explicit is not None:
        candidates.append(Path(explicit))

    env_value = os.environ.get("LAS2LAS_EXE", "").strip()
    if env_value:
        candidates.append(Path(env_value))

    root = package_root()
    candidates.extend([
        root / "tools" / "LAStools" / "bin" / "las2las.exe",
        root / "scripts" / "LAStools" / "bin" / "las2las.exe",
        root / "scripts" / "las2las.exe",
        Path(r"C:\LAStools\bin\las2las.exe"),
        Path(r"C:\Program Files\LAStools\bin\las2las.exe"),
    ])

    which = shutil.which("las2las") or shutil.which("las2las.exe")
    if which:
        candidates.append(Path(which))

    seen = set()
    for p in candidates:
        try:
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.exists() and p.is_file():
            return p

    raise RuntimeError(
        "las2las.exe was not found. The delivery header is intentionally assigned "
        "with the same LAStools command used by Production. Install LAStools or set "
        "LAS2LAS_EXE, e.g.:\n"
        r'  set LAS2LAS_EXE=C:\LAStools\bin\las2las.exe' "\n"
        "Alternatively pass --las2las <path> to 02_build_eversource_points.py."
    )



def find_lastools_exe(tool: str, anchor_exe: Path | None = None) -> Path:
    """Locate a 64-bit LAStools executable, preferring the las2las install folder."""
    import os

    base_name = tool.lower().replace(".exe", "")
    names = [f"{base_name}64.exe", f"{base_name}.exe"]
    candidates = []

    if anchor_exe is not None:
        anchor = Path(anchor_exe).parent
        candidates.extend(anchor / n for n in names)

    env_key = f"{base_name.upper()}_EXE"
    env_val = os.environ.get(env_key, "").strip()
    if env_val:
        candidates.append(Path(env_val))

    root = package_root()
    for base in [
        root / "tools" / "LAStools" / "bin",
        root / "scripts" / "LAStools" / "bin",
        Path(r"C:\Tools\LAStools\bin"),
        Path(r"C:\LAStools\bin"),
        Path(r"C:\Program Files\LAStools\bin"),
    ]:
        candidates.extend(base / n for n in names)

    for n in names:
        q = shutil.which(n) or shutil.which(Path(n).stem)
        if q:
            candidates.append(Path(q))

    seen = set()
    for p in candidates:
        try:
            key = str(p.resolve()).lower()
        except Exception:
            key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.exists() and p.is_file():
            return p

    raise RuntimeError(
        f"{tool} executable was not found. It should normally be installed "
        f"beside las2las.exe in the LAStools bin folder."
    )


def _write_lof(paths: list[Path], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(str(Path(p).resolve()) for p in paths) + "\n",
        encoding="utf-8",
    )
    return path


def _run_quiet_command(cmd: list[str], label: str) -> None:
    cp = subprocess.run(
        cmd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **_hidden_process_kwargs(),
    )
    if cp.returncode != 0:
        raise RuntimeError(
            f"{label} failed (exit code {cp.returncode}).\n{cp.stdout}"
        )


def _merged_xyzc_export_and_split(
    las2txt_exe: Path,
    final_las_files: list[Path],
    points_dir: Path,
    full_dir: Path,
    temp_dir: Path,
) -> dict[int, int]:
    """Export mapped classes with two LAStools merged passes.

    LAStools distinguishes standard classes (0-31) from extended classes (>31).
    They cannot be supplied together to `-keep_class`, so run:
      * one pass with -keep_class for <=31
      * one pass with -keep_extended_class for >31

    Both passes still use the same merged RoW input list, so this remains much
    faster than opening every tile in Python.
    """
    points_dir.mkdir(parents=True, exist_ok=True)
    full_dir.mkdir(parents=True, exist_ok=True)

    mapped_classes = sorted(int(c) for c in PLS_XYZ_CLASS_MAP)
    standard_classes = [c for c in mapped_classes if c <= 31]
    extended_classes = [c for c in mapped_classes if c > 31]

    direct_classes = [c for c in mapped_classes if c not in {2, 4, 5}]
    direct_handles = {
        c: open(points_dir / PLS_XYZ_CLASS_MAP[c], "w", encoding="ascii", newline="\n")
        for c in direct_classes
    }
    full_handles = {
        c: open(full_dir / PLS_XYZ_CLASS_MAP[c], "w", encoding="ascii", newline="\n")
        for c in (2, 4, 5)
    }
    counts = defaultdict(int)

    lof = _write_lof(final_las_files, temp_dir / "final_las_files.lof")

    def run_pass(classes: list[int], filter_arg: str, label: str) -> None:
        if not classes:
            return

        # Include scanner channel ('l') so las2txt treats classification as LAS 1.4
        # extended classification when required. We only consume x y z c below.
        cmd = [
            str(las2txt_exe),
            "-lof", str(lof),
            "-merged",
            filter_arg, *[str(c) for c in classes],
            "-parse", "xyzcl",
            "-sep", "space",
            "-stdout",
            "-quiet",
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1024 * 1024,
            **_hidden_process_kwargs(),
        )
        assert proc.stdout is not None

        for line in proc.stdout:
            parts = line.strip().split()
            if len(parts) < 4:
                continue
            try:
                c = int(float(parts[3]))
            except Exception:
                continue
            if c not in PLS_XYZ_CLASS_MAP:
                continue

            xyz = f"{parts[0]} {parts[1]} {parts[2]}\n"
            if c in full_handles:
                full_handles[c].write(xyz)
            else:
                h = direct_handles.get(c)
                if h is not None:
                    h.write(xyz)
            counts[c] += 1

        stderr = proc.stderr.read() if proc.stderr is not None else ""
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(
                f"las2txt merged {label} export failed "
                f"(exit code {rc}).\n{stderr}"
            )

    try:
        info(
            f"  las2txt standard classes: "
            f"{' '.join(str(c) for c in standard_classes)}"
        )
        run_pass(standard_classes, "-keep_class", "standard-class")

        info(
            f"  las2txt extended classes: "
            f"{' '.join(str(c) for c in extended_classes)}"
        )
        run_pass(extended_classes, "-keep_extended_class", "extended-class")
    finally:
        _close_handles(direct_handles)
        _close_handles(full_handles)

    for c in direct_classes:
        p = points_dir / PLS_XYZ_CLASS_MAP[c]
        if counts.get(c, 0) == 0 and p.exists():
            p.unlink()

    for c in (2, 4, 5):
        p = full_dir / PLS_XYZ_CLASS_MAP[c]
        if counts.get(c, 0) == 0 and p.exists():
            p.unlink()

    return dict(counts)



def _ground_global_signature(final_las_files: list[Path]) -> dict[str, Any]:
    """Fingerprint the RoW-wide ground-thinning input and algorithm."""
    return {
        "complete": True,
        "algorithm": GROUND_THIN_ALGORITHM_VERSION,
        "adaptive_vertical_ft": float(PLS_GROUND_TOLERANCE_FT),
        "adaptive_max_spacing_ft": float(PLS_GROUND_INITIAL_SPACING_FT),
        "fallback_step_ft": float(PLS_GROUND_INITIAL_SPACING_FT),
        "xyz_format_version": int(PLS_XYZ_FORMAT_VERSION),
        "sources": sorted(
            [_file_stat_signature(Path(p)) for p in final_las_files],
            key=lambda x: x["name"],
        ),
    }


def _lastools_thin_ground_rowwide(
    lasthin_exe: Path,
    las2txt_exe: Path,
    final_las_files: list[Path],
    lof: Path,
    out_xyz: Path,
    temp_dir: Path,
) -> dict[str, Any]:
    """Thin class-2 ground as ONE logical RoW dataset.

    Primary mode uses LAStools adaptive TIN thinning:
        -adaptive 0.30 15
    which keeps a TIN within 0.30 ft vertical tolerance while enforcing a
    maximum 15 ft horizontal spacing.

    `-sparse` is deliberately enabled because long utility corridors can have
    very large sparse XY extents.  No Python point array is built.

    If adaptive thinning fails (for example due to an unusually large/awkward
    dataset), the run does NOT abort.  It falls back to one globally consistent
    15-ft lowest-Z grid across the complete RoW.  The fallback is less
    ModelKey-like but is deterministic and cannot create source-tile seams.
    """
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    out_xyz = Path(out_xyz)
    out_xyz.parent.mkdir(parents=True, exist_ok=True)

    temp_laz = temp_dir / "ground_class_2_rowwide_thin.laz"
    marker = temp_dir / "ground_class_2_rowwide_thin.json"
    signature = _ground_global_signature(final_las_files)

    # Reuse a completed temporary adaptive/fallback LAZ after an interrupted
    # later step when the exact LiDAR inputs and algorithm are unchanged.
    reusable = False
    cached_mode = ""
    if marker.exists() and temp_laz.exists() and temp_laz.stat().st_size > 0:
        try:
            old = json.loads(marker.read_text(encoding="utf-8"))
            old_sig = dict(old)
            cached_mode = txt(old_sig.pop("mode", ""))
            if old_sig == signature:
                reusable = True
        except Exception:
            reusable = False

    if out_xyz.exists():
        out_xyz.unlink()

    mode = cached_mode if reusable else ""
    adaptive_log = ""

    if reusable:
        info(
            "  ground thinning: reusing completed RoW-wide LAStools temporary "
            f"({mode or 'cached'})"
        )
    else:
        if temp_laz.exists():
            try:
                temp_laz.unlink()
            except Exception:
                pass
        if marker.exists():
            try:
                marker.unlink()
            except Exception:
                pass

        adaptive_cmd = [
            str(lasthin_exe),
            "-lof", str(lof),
            "-merged",
            "-keep_class", "2",
            "-adaptive",
            f"{float(PLS_GROUND_TOLERANCE_FT):g}",
            f"{float(PLS_GROUND_INITIAL_SPACING_FT):g}",
            "-sparse",
            "-olaz",
            "-o", str(temp_laz),
            "-quiet",
        ]

        info(
            "  ground thinning: RoW-wide LAStools adaptive TIN "
            f"(+/-{PLS_GROUND_TOLERANCE_FT:g} ft, "
            f"max {PLS_GROUND_INITIAL_SPACING_FT:g} ft, sparse)"
        )
        cp = subprocess.run(
            adaptive_cmd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **_hidden_process_kwargs(),
        )
        adaptive_log = cp.stdout or ""

        if cp.returncode == 0 and temp_laz.exists() and temp_laz.stat().st_size > 0:
            mode = "LASTOOLS_ROWWIDE_ADAPTIVE"
        else:
            # Robust fallback.  Still one grid for the entire RoW, so no
            # source-tile density discontinuities can be introduced.
            info(
                "  WARNING: adaptive ground thinning did not complete; "
                "falling back to RoW-wide 15-ft lowest-Z sparse grid."
            )
            if adaptive_log.strip():
                # Keep console useful without dumping pages of LAStools output.
                tail = "\n".join(adaptive_log.strip().splitlines()[-8:])
                info("  adaptive lasthin tail:\n" + tail)

            if temp_laz.exists():
                try:
                    temp_laz.unlink()
                except Exception:
                    pass

            fallback_cmd = [
                str(lasthin_exe),
                "-lof", str(lof),
                "-merged",
                "-keep_class", "2",
                "-step", f"{float(PLS_GROUND_INITIAL_SPACING_FT):g}",
                "-lowest",
                "-sparse",
                "-olaz",
                "-o", str(temp_laz),
                "-quiet",
            ]
            cp2 = subprocess.run(
                fallback_cmd,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **_hidden_process_kwargs(),
            )
            if cp2.returncode != 0 or not temp_laz.exists():
                raise RuntimeError(
                    "Both adaptive and fallback RoW-wide ground thinning failed.\n"
                    f"Adaptive exit: {cp.returncode}\n"
                    f"Fallback exit: {cp2.returncode}\n"
                    f"Fallback output:\n{cp2.stdout}"
                )
            mode = "LASTOOLS_ROWWIDE_15FT_LOWEST_FALLBACK"

        marker_payload = dict(signature)
        marker_payload["mode"] = mode
        marker.write_text(json.dumps(marker_payload, indent=2), encoding="utf-8")

    # Export directly from the one thinned LAZ.  Classification is preserved,
    # therefore the final file is X Y Z 2.
    txt_cmd = [
        str(las2txt_exe),
        "-i", str(temp_laz),
        "-parse", "xyzc",
        "-sep", "space",
        "-o", str(out_xyz),
        "-quiet",
    ]
    _run_quiet_command(txt_cmd, "las2txt RoW-wide thinned ground")

    count = 0
    try:
        with laspy.open(temp_laz) as r:
            count = int(r.header.point_count)
    except Exception:
        # XYZ still exists even if the count read fails.
        pass

    if count == 0 and out_xyz.exists():
        try:
            count = sum(1 for _ in open(out_xyz, "r", encoding="ascii"))
        except Exception:
            pass

    if count == 0 and out_xyz.exists() and out_xyz.stat().st_size == 0:
        out_xyz.unlink()

    return {
        "count": int(count),
        "mode": mode,
        "temp_laz": str(temp_laz),
    }


def _lastools_thin_vegetation(
    lasthin_exe: Path,
    las2txt_exe: Path,
    lof: Path,
    cls: int,
    out_xyz: Path,
    temp_dir: Path,
) -> int:
    """Thin one vegetation class RoW-wide and output X Y Z CLASS."""
    temp_laz = temp_dir / f"veg_class_{int(cls)}_thin.laz"
    if temp_laz.exists():
        temp_laz.unlink()
    if out_xyz.exists():
        out_xyz.unlink()

    thin_cmd = [
        str(lasthin_exe),
        "-lof", str(lof),
        "-merged",
        "-keep_class", str(int(cls)),
        "-step", "3.0",
        "-highest",
        "-sparse",
        "-olaz",
        "-o", str(temp_laz),
        "-quiet",
    ]
    _run_quiet_command(thin_cmd, f"lasthin class {cls}")

    if not temp_laz.exists():
        return 0

    txt_cmd = [
        str(las2txt_exe),
        "-i", str(temp_laz),
        "-parse", "xyzc",
        "-sep", "space",
        "-o", str(out_xyz),
        "-quiet",
    ]
    _run_quiet_command(txt_cmd, f"las2txt thinned class {cls}")

    count = 0
    try:
        with laspy.open(temp_laz) as r:
            count = int(r.header.point_count)
    except Exception:
        pass

    try:
        temp_laz.unlink()
    except Exception:
        pass

    if count == 0 and out_xyz.exists() and out_xyz.stat().st_size == 0:
        out_xyz.unlink()
    return count

def _ground_thin_one_tile(las_path: Path, out_xyz: Path) -> tuple[str, int, int]:
    """Thin class-2 ground for one final tile using the accepted TIN approximation."""
    parts = []
    source_count = 0
    with laspy.open(las_path, read_evlrs=True) as reader:
        for pts in reader.chunk_iterator(1_000_000):
            arr = _xyz_array_from_points(pts, 2)
            if len(arr):
                source_count += int(len(arr))
                parts.append(arr)
    if not parts:
        if out_xyz.exists():
            out_xyz.unlink()
        return las_path.name, 0, 0

    ground = np.concatenate(parts, axis=0)
    thinned = _thin_ground_model_key(ground)
    out_xyz.parent.mkdir(parents=True, exist_ok=True)
    np.savetxt(out_xyz, thinned, fmt="%.3f %.3f %.3f")
    return las_path.name, source_count, int(len(thinned))


def _parallel_ground_thin(
    final_las_files: list[Path],
    output_xyz: Path,
    temp_dir: Path,
    workers: int,
) -> tuple[int, int]:
    """Run the existing ground thinning per tile in parallel, then concatenate."""
    ground_dir = temp_dir / "ground_tiles"
    ground_dir.mkdir(parents=True, exist_ok=True)
    nworkers = max(1, min(int(workers or 1), 4, len(final_las_files)))

    jobs = []
    for i, p in enumerate(final_las_files, 1):
        jobs.append((p, ground_dir / f"{i:06d}.xyz"))

    source_total = 0
    output_total = 0
    with ThreadPoolExecutor(max_workers=nworkers) as pool:
        futures = {
            pool.submit(_ground_thin_one_tile, p, o): (p, o)
            for p, o in jobs
        }
        done = 0
        for fut in as_completed(futures):
            name, src_n, out_n = fut.result()
            source_total += int(src_n)
            output_total += int(out_n)
            done += 1
            info(f"  ground [{done}/{len(jobs)}]: {name}")

    output_xyz.parent.mkdir(parents=True, exist_ok=True)
    with open(output_xyz, "wb") as dst:
        for _, part in jobs:
            if not part.exists():
                continue
            with open(part, "rb") as src:
                shutil.copyfileobj(src, dst, length=1024 * 1024 * 8)

    if output_total == 0 and output_xyz.exists():
        output_xyz.unlink()

    return source_total, output_total


def _plain_las14_header(header):
    """Copy source header and make the intermediate container LAS 1.4.

    CRS assignment is deliberately NOT trusted here. The authoritative header
    rewrite is performed by las2las afterwards.
    """
    from laspy.header import Version
    h = copy.deepcopy(header)
    h.set_version_and_point_format(Version(1, 4), h.point_format)
    return h


def _header_horizontal_epsg(crs) -> int | None:
    if crs is None:
        return None
    try:
        if crs.is_projected:
            code = crs.to_epsg()
            if code:
                return int(code)
    except Exception:
        pass
    try:
        for sub in getattr(crs, "sub_crs_list", []) or []:
            try:
                if sub.is_projected:
                    code = sub.to_epsg()
                    if code:
                        return int(code)
            except Exception:
                continue
    except Exception:
        pass
    return None


def _header_vertical_text(crs) -> str:
    if crs is None:
        return ""
    bits = []
    try:
        bits.append(crs.name)
    except Exception:
        pass
    try:
        for sub in getattr(crs, "sub_crs_list", []) or []:
            bits.append(getattr(sub, "name", "") or "")
            try:
                code = sub.to_epsg()
                if code:
                    bits.append(f"EPSG:{code}")
            except Exception:
                pass
    except Exception:
        pass
    return " | ".join(x for x in bits if x)


def apply_production_las_header(
    intermediate_las: Path,
    final_las: Path,
    epsg: int,
    las2las_exe: Path,
) -> dict[str, Any]:
    """Apply the Production LAStools header recipe and verify the written LAS.

    Equivalent to the supplied Production batch files:
      las2las -i <file> -epsg <state-plane>
              -survey_feet -elevation_survey_feet -vertical_navd88
              -olas -o <output>

    Coordinates are not reprojected. This is a header/metadata assignment step.
    """
    final_las.parent.mkdir(parents=True, exist_ok=True)
    if final_las.exists():
        final_las.unlink()

    cmd = [
        str(las2las_exe),
        "-i", str(intermediate_las),
        "-epsg", str(int(epsg)),
        "-survey_feet",
        "-elevation_survey_feet",
        "-vertical_navd88",
        "-olas",
        "-o", str(final_las),
    ]
    info("  header: " + " ".join(f'"{x}"' if " " in x else x for x in cmd))
    try:
        cp = subprocess.run(
            cmd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **_hidden_process_kwargs(),
        )
    except Exception as exc:
        raise RuntimeError(f"Could not run las2las.exe: {exc}") from exc

    if cp.returncode != 0:
        raise RuntimeError(
            f"las2las header assignment failed for {intermediate_las.name} "
            f"(exit code {cp.returncode}).\n{cp.stdout}"
        )
    if not final_las.exists():
        raise RuntimeError(
            f"las2las reported success but did not create {final_las}"
        )

    with laspy.open(final_las, read_evlrs=True) as check:
        h = check.header
        out_version = str(h.version)
        out_count = int(h.point_count)
        out_pf = int(h.point_format.id)
        out_crs = _las_header_crs(h)
        horizontal_epsg = _header_horizontal_epsg(out_crs)
        crs_text = out_crs.to_string() if out_crs is not None else ""
        vertical_text = _header_vertical_text(out_crs)

        if out_version != "1.4":
            raise RuntimeError(
                f"Header validation failed for {final_las.name}: "
                f"expected LAS 1.4, read {out_version}"
            )
        if out_crs is None:
            raise RuntimeError(
                f"Header validation failed for {final_las.name}: "
                "no CRS metadata can be parsed after las2las."
            )
        if horizontal_epsg != int(epsg):
            raise RuntimeError(
                f"Header validation failed for {final_las.name}: "
                f"expected horizontal EPSG:{epsg}, read "
                f"{horizontal_epsg or crs_text}."
            )

        vlr_count = len(getattr(h, "vlrs", []) or [])
        evlr_count = len(getattr(h, "evlrs", []) or [])

    return {
        "LAS_VERSION": out_version,
        "POINT_COUNT": out_count,
        "POINT_FORMAT": out_pf,
        "HEADER_CRS": crs_text,
        "H_EPSG": horizontal_epsg,
        "VERTICAL_INFO": vertical_text,
        "VLR_COUNT": vlr_count,
        "EVLR_COUNT": evlr_count,
        "HEADER_METHOD": "LASTOOLS_LAS2LAS",
    }



def _concat_files(parts: list[Path], destination: Path) -> int:
    """Concatenate text partials without parsing them again."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    parts = [Path(p) for p in parts if Path(p).exists() and Path(p).stat().st_size > 0]
    if not parts:
        if destination.exists():
            destination.unlink()
        return 0

    with open(destination, "wb") as dst:
        for part in parts:
            with open(part, "rb") as src:
                shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)
    return len(parts)


def _safe_load_xyz(path: Path) -> np.ndarray:
    """Load one tile-level XYZ partial as Nx3 without special-casing one row."""
    if not path.exists() or path.stat().st_size == 0:
        return np.empty((0, 3), dtype=float)
    arr = np.loadtxt(path, dtype=float)
    if arr.size == 0:
        return np.empty((0, 3), dtype=float)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    return np.asarray(arr[:, :3], dtype=float)


def _tile_partial_marker_matches(marker: Path, las_path: Path) -> bool:
    if not marker.exists():
        return False
    try:
        rec = json.loads(marker.read_text(encoding="utf-8"))
        st = las_path.stat()
        return (
            int(rec.get("partial_schema_version", -1)) == int(PLS_PARTIAL_SCHEMA_VERSION)
            and int(rec.get("xyz_format_version", -1)) == int(PLS_XYZ_FORMAT_VERSION)
            and rec.get("source_name") == las_path.name
            and int(rec.get("source_size", -1)) == int(st.st_size)
            and int(rec.get("source_mtime_ns", -1)) == int(st.st_mtime_ns)
            and bool(rec.get("complete"))
        )
    except Exception:
        return False

def _write_tile_partial_marker(marker: Path, las_path: Path, counts: dict) -> None:
    st = las_path.stat()
    payload = {
        "complete": True,
        "partial_schema_version": int(PLS_PARTIAL_SCHEMA_VERSION),
        "xyz_format_version": int(PLS_XYZ_FORMAT_VERSION),
        "source_name": las_path.name,
        "source_size": int(st.st_size),
        "source_mtime_ns": int(st.st_mtime_ns),
        "counts": {str(k): int(v) for k, v in counts.items()},
    }
    marker.write_text(json.dumps(payload, indent=2), encoding="utf-8")

def _load_tile_partial_counts(marker: Path) -> dict:
    try:
        rec = json.loads(marker.read_text(encoding="utf-8"))
        out = {}
        for k, v in (rec.get("counts") or {}).items():
            try:
                key = int(k)
            except Exception:
                key = k
            out[key] = int(v)
        return out
    except Exception:
        return {}


def _tile_xyz_partials_worker(
    las2txt_exe: Path,
    final_las: Path,
    tile_temp: Path,
) -> dict[str, Any]:
    """Create restartable per-tile direct/full-class XYZ partials on disk.

    Output format is X Y Z CLASS. Ground thinning is deliberately NOT performed
    here because the model-key operation requires a 10-ft neighbour halo.  That
    second pass runs after all tile partials are available.
    """
    tile_temp.mkdir(parents=True, exist_ok=True)
    marker = tile_temp / "_DONE.json"

    if _tile_partial_marker_matches(marker, final_las):
        counts = _load_tile_partial_counts(marker)
        return {
            "source": final_las,
            "tile_temp": tile_temp,
            "counts": counts,
            "resumed": True,
        }

    for child in tile_temp.iterdir():
        if child.name == "_DONE.json":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except Exception:
                pass
    if marker.exists():
        try:
            marker.unlink()
        except Exception:
            pass

    direct_dir = tile_temp / "direct"
    full_dir = tile_temp / "full"
    direct_dir.mkdir(parents=True, exist_ok=True)
    full_dir.mkdir(parents=True, exist_ok=True)

    handles = {}
    counts = defaultdict(int)

    def get_handle(cls_value: int):
        if cls_value in handles:
            return handles[cls_value]
        if cls_value in {2, 4, 5}:
            out = full_dir / PLS_XYZ_CLASS_MAP[cls_value]
        else:
            out = direct_dir / PLS_XYZ_CLASS_MAP[cls_value]
        out.parent.mkdir(parents=True, exist_ok=True)
        h = open(out, "w", encoding="ascii", newline="\n")
        handles[cls_value] = h
        return h

    cmd = [
        str(las2txt_exe),
        "-i", str(final_las),
        "-parse", "xyzcl",
        "-sep", "space",
        "-stdout",
        "-quiet",
    ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1024 * 1024,
        **_hidden_process_kwargs(),
    )

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            parts = line.strip().split()
            if len(parts) < 4:
                continue
            try:
                cls_value = int(float(parts[3]))
            except Exception:
                continue
            if cls_value not in PLS_XYZ_CLASS_MAP:
                continue
            # Project feature-point format: X Y Z Feature/Class Number.
            get_handle(cls_value).write(
                f"{parts[0]} {parts[1]} {parts[2]} {cls_value}\n"
            )
            counts[int(cls_value)] += 1

        stderr = proc.stderr.read() if proc.stderr is not None else ""
        rc = proc.wait()
        if rc != 0:
            raise RuntimeError(
                f"las2txt tile export failed for {final_las.name} "
                f"(exit code {rc}).\n{stderr}"
            )
    finally:
        _close_handles(handles)

    _write_tile_partial_marker(marker, final_las, counts)
    return {
        "source": final_las,
        "tile_temp": tile_temp,
        "counts": dict(counts),
        "resumed": False,
    }

def _build_pls_partials_multicore(
    las2txt_exe: Path,
    final_las_files: list[Path],
    temp_root: Path,
    workers: int,
) -> tuple[list[dict[str, Any]], dict]:
    """Build independent tile partials in parallel and aggregate counts."""
    temp_root.mkdir(parents=True, exist_ok=True)
    tile_root = temp_root / "tiles"
    tile_root.mkdir(parents=True, exist_ok=True)

    jobs = []
    for i, las_path in enumerate(final_las_files, 1):
        tile_dir = tile_root / f"{i:06d}_{safe_filename(las_path.stem)}"
        jobs.append((las_path, tile_dir))

    nworkers = max(1, min(int(workers or 1), len(jobs)))
    info(f"  PLS partial export: {len(jobs):,} tile(s), {nworkers} worker(s)")

    results = []
    totals = defaultdict(int)

    with ThreadPoolExecutor(max_workers=nworkers) as pool:
        futures = {
            pool.submit(_tile_xyz_partials_worker, las2txt_exe, las_path, tile_dir):
                (las_path, tile_dir)
            for las_path, tile_dir in jobs
        }
        done = 0
        resumed = 0
        for fut in as_completed(futures):
            result = fut.result()
            results.append(result)
            done += 1
            if result.get("resumed"):
                resumed += 1
            for k, v in (result.get("counts") or {}).items():
                totals[k] += int(v)
            state = "resume" if result.get("resumed") else "done"
            console_progress("pls_partials", "Icing | PLS partials", done, len(jobs), f"{state}: {Path(result['source']).name}")

    if resumed:
        info(f"  resumed tile partials: {resumed:,}")

    # Keep deterministic source/tile order for final concatenation.
    order = {str(p): i for i, p in enumerate(final_las_files)}
    results.sort(key=lambda r: order.get(str(r["source"]), 10**9))
    return results, dict(totals)


def _merge_pls_tile_partials(
    partial_results: list[dict[str, Any]],
    points_dir: Path,
    full_dir: Path,
) -> None:
    """Concatenate direct/full-class tile partials without reparsing XYZ."""
    points_dir.mkdir(parents=True, exist_ok=True)
    full_dir.mkdir(parents=True, exist_ok=True)

    for cls_value, name in PLS_XYZ_CLASS_MAP.items():
        c = int(cls_value)
        if c in {2, 4, 5}:
            continue
        parts = [Path(r["tile_temp"]) / "direct" / name for r in partial_results]
        _concat_files(parts, points_dir / name)

    for c in (2, 4, 5):
        name = PLS_XYZ_CLASS_MAP[c]
        parts = [Path(r["tile_temp"]) / "full" / name for r in partial_results]
        _concat_files(parts, full_dir / name)

def _aoi_geometry_sha256(geom) -> str:
    if geom is None or geom.is_empty:
        return ""
    return hashlib.sha256(bytes(geom.wkb)).hexdigest()


def _delivery_las_manifest_path(output_dir: Path, row: str) -> Path:
    return Path(output_dir) / "cache" / safe_filename(row) / "lidar_delivery_manifest.json"


def _write_delivery_las_manifest(
    output_dir: Path,
    row: str,
    lidar_source: Path,
    aoi_geom,
    epsg: int,
    year: int,
    final_las_files: list[Path],
) -> None:
    path = _delivery_las_manifest_path(output_dir, row)
    path.parent.mkdir(parents=True, exist_ok=True)
    source_fp = _lidar_source_fingerprint(lidar_source)
    files = []
    for p in sorted((Path(x) for x in final_las_files), key=lambda x: x.name.lower()):
        st = p.stat()
        files.append({
            "name": p.name,
            "size": int(st.st_size),
            "mtime_ns": int(st.st_mtime_ns),
        })
    payload = {
        "manifest_version": 1,
        "processing_rule": "AOI_HARD_BOUNDARY_V1",
        "row": row,
        "epsg": int(epsg),
        "year": int(year),
        "source_sha256": source_fp.get("sha256"),
        "source_count": int(source_fp.get("count", 0)),
        "aoi_sha256": _aoi_geometry_sha256(aoi_geom),
        "files": files,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_reusable_delivery_las(
    output_dir: Path,
    row: str,
    lidar_source: Path,
    aoi_geom,
    epsg: int,
    year: int,
    lidar_dir: Path,
) -> list[Path]:
    """Return prior AOI-bounded delivery LAS only when source/AOI/settings match."""
    manifest = _delivery_las_manifest_path(output_dir, row)
    if not manifest.exists() or not Path(lidar_dir).exists():
        return []
    try:
        rec = json.loads(manifest.read_text(encoding="utf-8"))
        source_fp = _lidar_source_fingerprint(lidar_source)
        if rec.get("manifest_version") != 1:
            return []
        if rec.get("processing_rule") != "AOI_HARD_BOUNDARY_V1":
            return []
        if txt(rec.get("row")) != txt(row):
            return []
        if int(rec.get("epsg", -1)) != int(epsg) or int(rec.get("year", -1)) != int(year):
            return []
        if rec.get("source_sha256") != source_fp.get("sha256"):
            return []
        if rec.get("aoi_sha256") != _aoi_geometry_sha256(aoi_geom):
            return []
        out = []
        for f in rec.get("files", []):
            p = Path(lidar_dir) / txt(f.get("name"))
            if not p.exists():
                return []
            st = p.stat()
            if int(f.get("size", -1)) != int(st.st_size):
                return []
            if int(f.get("mtime_ns", -1)) != int(st.st_mtime_ns):
                return []
            out.append(p)
        return out
    except Exception:
        return []



def _file_stat_signature(path: Path) -> dict[str, Any]:
    st = Path(path).stat()
    return {
        "name": Path(path).name,
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
    }


def _infer_delivery_tile_size(final_las_files: list[Path]) -> float:
    origins = [_parse_tile_origin(Path(p)) for p in final_las_files]
    origins = [o for o in origins if o is not None]
    xs = [o[0] for o in origins]
    ys = [o[1] for o in origins]
    steps = [v for v in (_min_positive_step(xs), _min_positive_step(ys)) if v and v > 0]
    if steps:
        return float(min(steps))

    dims = []
    for p in final_las_files:
        with laspy.open(p) as r:
            w = float(r.header.maxs[0] - r.header.mins[0])
            h = float(r.header.maxs[1] - r.header.mins[1])
            if w > 0: dims.append(w)
            if h > 0: dims.append(h)
    if not dims:
        raise RuntimeError("Cannot infer nominal tile size for 10-ft ground neighbours")
    return float(max(dims))


def _ground_tile_plan(final_las_files: list[Path]) -> list[dict[str, Any]]:
    """Build nominal grid extents and 10-ft neighbour sets from delivery names."""
    tile_size = _infer_delivery_tile_size(final_las_files)
    records = []
    for p in final_las_files:
        p = Path(p)
        origin = _parse_tile_origin(p)
        if origin is None:
            with laspy.open(p) as r:
                origin = _grid_origin_from_header(
                    float(r.header.mins[0]), float(r.header.mins[1]), tile_size
                )
        x0, y0 = float(origin[0]), float(origin[1])
        records.append({
            "path": p,
            "x0": x0, "y0": y0,
            "x1": x0 + tile_size, "y1": y0 + tile_size,
        })

    # One origin for every tile in this RoW. With the project 1500-ft grid,
    # 15-ft cells divide tile boundaries exactly and remain stable across blocks.
    global_origin_x = min(r["x0"] for r in records)
    global_origin_y = min(r["y0"] for r in records)

    halo = float(GROUND_NEIGHBOUR_FT)
    for rec in records:
        rec["grid_origin_x"] = float(global_origin_x)
        rec["grid_origin_y"] = float(global_origin_y)
        hx0, hy0 = rec["x0"] - halo, rec["y0"] - halo
        hx1, hy1 = rec["x1"] + halo, rec["y1"] + halo
        neighbours = []
        for other in records:
            if (
                other["x1"] >= hx0 and other["x0"] <= hx1 and
                other["y1"] >= hy0 and other["y0"] <= hy1
            ):
                neighbours.append(other["path"])
        rec["neighbours"] = neighbours
        rec["tile_size"] = tile_size
    return records


def _read_ground_points_in_bounds(path: Path, bounds: tuple[float, float, float, float]) -> np.ndarray:
    x0, y0, x1, y1 = [float(v) for v in bounds]
    parts = []
    with laspy.open(path, read_evlrs=True) as reader:
        h = reader.header
        if float(h.maxs[0]) < x0 or float(h.mins[0]) > x1 or float(h.maxs[1]) < y0 or float(h.mins[1]) > y1:
            return np.empty((0, 3), dtype=float)
        for pts in reader.chunk_iterator(1_000_000):
            cls = np.asarray(pts.classification)
            mask = cls == 2
            if not np.any(mask):
                continue
            x = np.asarray(pts.x)[mask]
            y = np.asarray(pts.y)[mask]
            z = np.asarray(pts.z)[mask]
            keep = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
            if np.any(keep):
                parts.append(np.column_stack([x[keep], y[keep], z[keep]]))
    return np.concatenate(parts, axis=0) if parts else np.empty((0, 3), dtype=float)


def _ground_halo_signature(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "complete": True,
        "algorithm": GROUND_THIN_ALGORITHM_VERSION,
        "neighbour_ft": float(GROUND_NEIGHBOUR_FT),
        "initial_spacing_ft": float(PLS_GROUND_INITIAL_SPACING_FT),
        "tolerance_ft": float(PLS_GROUND_TOLERANCE_FT),
        "pregrid_ft": float(PLS_GROUND_PREGRID_FT),
        "max_iter": int(PLS_GROUND_MAX_ITER),
        "grid_origin_x": float(rec.get("grid_origin_x", 0.0)),
        "grid_origin_y": float(rec.get("grid_origin_y", 0.0)),
        "xyz_format_version": int(PLS_XYZ_FORMAT_VERSION),
        "central": _file_stat_signature(rec["path"]),
        "sources": sorted(
            [_file_stat_signature(p) for p in rec["neighbours"]],
            key=lambda x: x["name"],
        ),
    }


def _ground_halo_worker(rec: dict[str, Any], tile_temp: Path) -> dict[str, Any]:
    ground_dir = Path(tile_temp) / "ground_thin"
    ground_dir.mkdir(parents=True, exist_ok=True)
    out_xyz = ground_dir / PLS_XYZ_CLASS_MAP[2]
    marker = ground_dir / "_DONE.json"
    signature = _ground_halo_signature(rec)

    if marker.exists() and out_xyz.exists():
        try:
            old = json.loads(marker.read_text(encoding="utf-8"))
            if old == signature:
                n = 0
                try:
                    n = sum(1 for _ in open(out_xyz, "r", encoding="ascii"))
                except Exception:
                    pass
                return {"path": rec["path"], "tile_temp": tile_temp, "count": n, "resumed": True}
        except Exception:
            pass

    halo = float(GROUND_NEIGHBOUR_FT)
    halo_bounds = (rec["x0"] - halo, rec["y0"] - halo, rec["x1"] + halo, rec["y1"] + halo)
    chunks = []
    for p in rec["neighbours"]:
        arr = _read_ground_points_in_bounds(p, halo_bounds)
        if len(arr):
            chunks.append(arr)
    all_ground = np.concatenate(chunks, axis=0) if chunks else np.empty((0, 3), dtype=float)

    if len(all_ground):
        thinned = _thin_ground_model_key(
            all_ground,
            grid_origin_x=float(rec.get("grid_origin_x", 0.0)),
            grid_origin_y=float(rec.get("grid_origin_y", 0.0)),
        )
        # Retain only the central nominal tile.  Halo points influence the TIN but
        # are never emitted into the neighbouring tile's output.
        keep = (
            (thinned[:, 0] >= rec["x0"]) & (thinned[:, 0] < rec["x1"]) &
            (thinned[:, 1] >= rec["y0"]) & (thinned[:, 1] < rec["y1"])
        )
        central = thinned[keep]
        n = _write_xyz_class_array(out_xyz, central, 2)
    else:
        if out_xyz.exists(): out_xyz.unlink()
        n = 0

    marker.write_text(json.dumps(signature, indent=2), encoding="utf-8")
    return {"path": rec["path"], "tile_temp": tile_temp, "count": int(n), "resumed": False}


def _build_ground_halo_multicore(
    final_las_files: list[Path],
    partial_results: list[dict[str, Any]],
    points_dir: Path,
    workers: int,
) -> int:
    """Ground ModelKey per tile with 10-ft neighbour halo, then concatenate."""
    plan = _ground_tile_plan(final_las_files)
    temp_by_source = {str(Path(r["source"])): Path(r["tile_temp"]) for r in partial_results}
    nworkers = max(1, min(int(workers or 1), 4, len(plan)))
    info(
        f"  ground thinning: {len(plan):,} tile(s), 15-ft / 0.30-ft ModelKey, 10-ft neighbours, "
        f"{nworkers} worker(s)"
    )
    results = []
    with ThreadPoolExecutor(max_workers=nworkers) as pool:
        futures = {}
        for rec in plan:
            tile_temp = temp_by_source[str(Path(rec["path"]))]
            futures[pool.submit(_ground_halo_worker, rec, tile_temp)] = rec
        done = 0
        resumed = 0
        for fut in as_completed(futures):
            result = fut.result()
            results.append(result)
            done += 1
            if result.get("resumed"): resumed += 1
            state = "resume" if result.get("resumed") else "done"
            info(f"  ground [{done}/{len(plan)}] ({state}): {Path(result['path']).name}")
    if resumed:
        info(f"  resumed ground tiles: {resumed:,}")

    order = {str(Path(p)): i for i, p in enumerate(final_las_files)}
    results.sort(key=lambda r: order.get(str(Path(r["path"])), 10**9))
    parts = [Path(r["tile_temp"]) / "ground_thin" / PLS_XYZ_CLASS_MAP[2] for r in results]
    _concat_files(parts, Path(points_dir) / PLS_XYZ_CLASS_MAP[2])
    return int(sum(int(r.get("count", 0)) for r in results))

def _build_pls_from_final_las(
    final_las_files: list[Path],
    pls_root: Path,
    las2las_exe: Path,
    workers: int,
    temp_root: Path,
    keep_temp: bool = False,
) -> dict:
    """Build project PLS outputs from final AOI-bounded/headered LAS files."""
    if not final_las_files:
        raise RuntimeError("PLS export requires at least one final LAS file")
    points_dir = Path(pls_root) / "Points"
    full_dir = Path(pls_root) / "Full Class"
    temp_root = Path(temp_root)
    temp_root.mkdir(parents=True, exist_ok=True)
    las2txt_exe = find_lastools_exe("las2txt", las2las_exe)
    lasthin_exe = find_lastools_exe("lasthin", las2las_exe)
    xyz_counts = defaultdict(int)

    partial_results, partial_counts = _build_pls_partials_multicore(
        las2txt_exe, final_las_files, temp_root, workers
    )
    for k, v in partial_counts.items():
        xyz_counts[k] += int(v)

    info("  merging direct/full-class tile partials -> final PLS files")
    _merge_pls_tile_partials(partial_results, points_dir, full_dir)

    lof = _write_lof(final_las_files, temp_root / "final_las_files.lof")

    # Ground is deliberately processed as one logical RoW dataset.  This removes
    # the source-tile boundary effect seen in UAT while keeping point processing
    # inside LAStools rather than Python RAM.
    ground_result = _lastools_thin_ground_rowwide(
        lasthin_exe,
        las2txt_exe,
        final_las_files,
        lof,
        points_dir / PLS_XYZ_CLASS_MAP[2],
        temp_root,
    )
    xyz_counts["OUT_2"] = int(ground_result.get("count", 0))
    xyz_counts["GROUND_MODE"] = txt(ground_result.get("mode"))

    info("  vegetation thinning: RoW-wide lasthin 3 ft XY, highest Z")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {
            pool.submit(
                _lastools_thin_vegetation,
                lasthin_exe, las2txt_exe, lof, c,
                points_dir / PLS_XYZ_CLASS_MAP[c], temp_root
            ): c
            for c in (4, 5)
        }
        for fut in as_completed(futs):
            c = futs[fut]
            n = int(fut.result())
            xyz_counts[f"OUT_{c}"] = n
            info(f"  veg class {c}: {n:,} output point(s)")

    _ensure_expected_pls_files(points_dir, full_dir)
    info(
        "  PLS complete: "
        f"ground {xyz_counts.get('OUT_2',0):,}/{xyz_counts.get(2,0):,}, "
        f"veg4 {xyz_counts.get('OUT_4',0):,}/{xyz_counts.get(4,0):,}, "
        f"veg5 {xyz_counts.get('OUT_5',0):,}/{xyz_counts.get(5,0):,}"
    )
    info("  XYZ format: X Y Z CLASS")
    info(f"  Full Class backup: {full_dir}")
    if not keep_temp:
        shutil.rmtree(temp_root, ignore_errors=True)
        info("  temp partials cleaned after successful PLS build")
    else:
        info(f"  temp partials retained: {temp_root}")
    return dict(xyz_counts)

def clip_lidar_for_row(
    row: str,
    aoi_geom,
    lidar_files: list[Path],
    project_crs,
    epsg: int,
    year: int,
    lidar_dir: Path,
    pls_root: Path | None,
    tile_size: float | None,
    write_xyz: bool = True,
    all_aoi_lookup: dict[str, dict[str, Any]] | None = None,
    force_clip: bool = False,
    las2las_exe: Path | None = None,
    workers: int = 6,
    temp_root: Path | None = None,
    keep_temp: bool = False,
):
    if laspy is None:
        raise RuntimeError("Point-cloud stage requires laspy + lazrs")
    if las2las_exe is None:
        raise RuntimeError("Internal error: las2las executable was not resolved")

    workers=max(1,int(workers or 1))
    lidar_dir=Path(lidar_dir)
    lidar_dir.mkdir(parents=True,exist_ok=True)
    xyz_dir=(Path(pls_root)/"Points") if pls_root is not None else None

    aoi_lookup=all_aoi_lookup or {}
    jobs=[]

    info("  discovering candidate LiDAR tiles...")
    for src in lidar_files:
        with laspy.open(src,read_evlrs=True) as reader:
            hb=box(
                float(reader.header.mins[0]),float(reader.header.mins[1]),
                float(reader.header.maxs[0]),float(reader.header.maxs[1])
            )
            origin=_parse_tile_origin(src) or _grid_origin_from_header(
                float(reader.header.mins[0]),float(reader.header.mins[1]),tile_size
            )
            nominal_geom=(
                box(float(origin[0]),float(origin[1]),
                    float(origin[0])+float(tile_size),float(origin[1])+float(tile_size))
                if tile_size else hb
            )
            if not nominal_geom.intersects(aoi_geom) and not hb.intersects(aoi_geom):
                continue

            overlapping_rows=[]
            for rec in aoi_lookup.values():
                if _positive_area_overlap(nominal_geom,rec.get("geometry")):
                    overlapping_rows.append(txt(rec.get("ROW")))
            overlapping_rows=sorted({x for x in overlapping_rows if x})

            # AOI is a hard delivery boundary. A tile may pass through only when
            # its entire LAS header XY extent is covered by the Polygon AOI.
            # "exclusive to one RoW" is NOT sufficient evidence for pass-through.
            fully_inside=bool(aoi_geom.covers(hb))
            do_clip=bool(force_clip or not fully_inside)
            mode=(
                "CLIPPED_TO_AOI" if do_clip else
                "PASS_THROUGH_HEADER_BOUNDS_INSIDE_AOI"
            )
            tref=_tile_ref(origin)
            out_name=f"{safe_filename(row)}_{safe_filename(tref)}_LAS_{int(year)}.las"
            jobs.append({
                "src":src,
                "dst":lidar_dir/out_name,
                "tref":tref,
                "out_name":out_name,
                "nominal_geom":nominal_geom,
                "process_mode":mode,
                "overlapping_rows":overlapping_rows,
                "do_clip":do_clip,
                "tmp_las":None,
            })

    if not jobs:
        return rows_to_gdf([],project_crs),pd.DataFrame(),{}

    nclip=sum(1 for j in jobs if j["do_clip"])
    info(f"  candidate tiles: {len(jobs):,} | pass-through: {len(jobs)-nclip:,} | clip: {nclip:,}")

    clip_jobs=[j for j in jobs if j["do_clip"]]
    for i,job in enumerate(clip_jobs,1):
        if i == 1:
            console_progress("aoi_clip", "Baking | AOI clip", 0, len(clip_jobs))
        tmp=lidar_dir/(job["dst"].stem+".__preheader__.las")
        if tmp.exists():
            tmp.unlink()
        written=0
        with laspy.open(job["src"],read_evlrs=True) as reader:
            header=_plain_las14_header(reader.header)
            with laspy.open(tmp,mode="w",header=header,do_compress=False) as writer:
                for pts in reader.chunk_iterator(1_000_000):
                    x=np.asarray(pts.x); y=np.asarray(pts.y)
                    mask=_point_mask_in_polygon(aoi_geom,x,y)
                    if not np.any(mask):
                        continue
                    cut=pts[mask]
                    writer.write_points(cut)
                    written+=len(cut)
        if written==0:
            try: tmp.unlink()
            except Exception: pass
            job["skip"]=True
        else:
            job["tmp_las"]=tmp
        console_progress("aoi_clip", "Baking | AOI clip", i, len(clip_jobs), job["src"].name)

    jobs=[j for j in jobs if not j.get("skip")]
    if not jobs:
        return rows_to_gdf([],project_crs),pd.DataFrame(),{}

    for j in jobs:
        if j["dst"].exists():
            j["dst"].unlink()

    nworkers=min(workers,len(jobs))
    info(f"  header rewrite: {len(jobs):,} tile(s), {nworkers} parallel las2las worker(s)")
    results={}
    with ThreadPoolExecutor(max_workers=nworkers) as pool:
        futures={}
        for job in jobs:
            src_for_header=job["tmp_las"] if job["tmp_las"] is not None else job["src"]
            fut=pool.submit(apply_production_las_header,src_for_header,job["dst"],epsg,las2las_exe)
            futures[fut]=job
        done=0
        for fut in as_completed(futures):
            job=futures[fut]
            results[str(job["dst"])]=fut.result()
            done+=1
            console_progress("header_rewrite", "Baking | LAS headers", done, len(jobs), job["dst"].name)

    tile_rows=[]; inv=[]
    for job in jobs:
        meta=results[str(job["dst"])]
        tmp=job.get("tmp_las")
        if tmp is not None and Path(tmp).exists():
            try: Path(tmp).unlink()
            except Exception: pass
        with laspy.open(job["dst"],read_evlrs=True) as check:
            bx=box(float(check.header.mins[0]),float(check.header.mins[1]),
                   float(check.header.maxs[0]),float(check.header.maxs[1]))
        overlap=job["overlapping_rows"]
        tile_index_geom = job["nominal_geom"] if job["nominal_geom"] is not None else bx
        tile_rows.append({
            "ROW":row,"TILE_NAME":job["out_name"],"TILE_REF":job["tref"],"CAP_YEAR":int(year),
            "SOURCE_FILE":job["src"].name,"POINT_COUNT":int(meta["POINT_COUNT"]),
            "LAS_VERSION":txt(meta["LAS_VERSION"]),"HEADER_CRS":txt(meta.get("HEADER_CRS")),
            "H_EPSG":meta.get("H_EPSG"),"VERT_INFO":txt(meta.get("VERTICAL_INFO")),
            "VLR_COUNT":meta.get("VLR_COUNT"),"EVLR_COUNT":meta.get("EVLR_COUNT"),
            "HDR_METHOD":txt(meta.get("HEADER_METHOD")),"PROCESS":job["process_mode"],
            "AOI_HITS":";".join(overlap),"N_AOI_HITS":len(overlap),
            "geometry":tile_index_geom,
        })
        inv.append({
            "PRODUCT":"LiDAR","ROW":row,"DELIVERY_FILE":job["out_name"],
            "SOURCE_FILE":job["src"].name,"POINT_COUNT":int(meta["POINT_COUNT"]),
            "LAS_VERSION":txt(meta["LAS_VERSION"]),"HEADER_CRS":txt(meta.get("HEADER_CRS")),
            "H_EPSG":meta.get("H_EPSG"),"VERTICAL_INFO":txt(meta.get("VERTICAL_INFO")),
            "VLR_COUNT":meta.get("VLR_COUNT"),"EVLR_COUNT":meta.get("EVLR_COUNT"),
            "HEADER_METHOD":txt(meta.get("HEADER_METHOD")),"PROCESS":job["process_mode"],
            "AOI_HITS":";".join(overlap),"STATUS":"WRITTEN",
        })

    xyz_counts=defaultdict(int)
    if write_xyz:
        if pls_root is None:
            raise RuntimeError("Internal error: PLS output root missing for XYZ export")
        if temp_root is None:
            raise RuntimeError("Internal error: PLS temp_root was not supplied")
        final_las_files=[Path(j["dst"]) for j in jobs]
        xyz_counts.update(_build_pls_from_final_las(
            final_las_files, Path(pls_root), las2las_exe, workers,
            Path(temp_root), keep_temp=keep_temp
        ))

    return rows_to_gdf(tile_rows,project_crs),pd.DataFrame(inv),dict(xyz_counts)


def _build_index_only_tiles_for_row(
    row: str,
    aoi_geom,
    physical_tiles: gpd.GeoDataFrame,
    boundary_tiles: gpd.GeoDataFrame | None,
    year: int,
    project_crs,
) -> gpd.GeoDataFrame:
    """Build Tile Index metadata without decoding point data.

    When LiDAR/PLS are not selected, use the persisted per-tile lasboundary QA
    from Stage 00 to decide which source tiles actually contain points in the AOI.
    Tile Index geometry remains the full nominal grid square.
    """
    boundary_lookup = {}
    if boundary_tiles is not None and not boundary_tiles.empty:
        for src, sg in boundary_tiles.groupby('SOURCE_FILE', sort=False):
            geoms=[g for g in sg.geometry if g is not None and not g.is_empty]
            if geoms:
                boundary_lookup[txt(src)] = unary_union(geoms)

    rows=[]
    for _, tr in physical_tiles.iterrows():
        src=txt(tr.get('SOURCE_FILE'))
        actual=boundary_lookup.get(src)
        if actual is not None:
            if not _positive_area_overlap(actual,aoi_geom):
                continue
        else:
            hb=box(float(tr.get('HDR_MINX')),float(tr.get('HDR_MINY')),float(tr.get('HDR_MAXX')),float(tr.get('HDR_MAXY')))
            if not hb.intersects(aoi_geom):
                continue
        tref=txt(tr.get('TILE_REF'))
        out_name=f"{safe_filename(row)}_{safe_filename(tref)}_LAS_{int(year)}.las"
        rows.append({
            'ROW':row,'TILE_NAME':out_name,'TILE_REF':tref,'CAP_YEAR':int(year),
            'SOURCE_FILE':src,'POINT_COUNT':0,'PROCESS':'INDEX_ONLY_FROM_BOUNDARY',
            'AOI_HITS':row,'N_AOI_HITS':1,'geometry':tr.geometry,
        })
    return rows_to_gdf(rows,project_crs)


def run_points_stage(
    lidar_source: Path,
    client_source: Path,
    kmz_path: Path,
    management_units_source: Path | None,
    aoi_dir: Path,
    selection_path: Path,
    epsg: int,
    year: int,
    output: Path,
    products: set[str] | list[str] | tuple[str,...] | None = None,
    force_clip: bool = False,
    las2las_path: Path | None = None,
    workers: int = 6,
    keep_temp: bool = False,
):
    """Generate selected non-geometry delivery product families.

    Supported product keys: lidar, pls, tile_index, management_units.
    Geometry is generated independently in Stage 01 and appears first.
    """
    output.mkdir(parents=True,exist_ok=True)
    crs=CRS.from_epsg(epsg)
    selection=load_preflight_selection(selection_path)
    selected_rows=selection.get('selected_rows',[])
    if not selected_rows: raise RuntimeError('Preflight selection contains no RoWs')

    selected_products={str(x).strip().lower() for x in (products or {'lidar','pls','tile_index','management_units'}) if str(x).strip()}
    valid={'lidar','pls','tile_index','management_units'}
    unknown=selected_products-valid
    if unknown: raise RuntimeError('Unknown point product(s): '+', '.join(sorted(unknown)))
    if not selected_products:
        info('No point-cloud product groups selected; Stage 02 skipped.')
        return None

    need_point_data=bool({'lidar','pls'} & selected_products)
    want_lidar='lidar' in selected_products
    want_pls='pls' in selected_products
    want_tile='tile_index' in selected_products
    want_mu='management_units' in selected_products

    info('='*80); info('EVERSOURCE 02 - SELECTED DELIVERY PRODUCTS'); info('='*80)
    info('Selected: '+', '.join(sorted(selected_products)))

    las2las_exe=find_las2las_exe(las2las_path) if need_point_data else None
    workers=max(1,int(workers or 1))
    if las2las_exe is not None:
        info(f'LAStools header writer: {las2las_exe}')
        info(f'Parallel las2las workers: {workers}')

    with tempfile.TemporaryDirectory(prefix='eversource_points_') as td_raw:
        td=Path(td_raw)
        kmz_refs=project_kmz(parse_sow_kmz(kmz_path),crs)
        known_rows=sorted({txt(r.row) for r in kmz_refs if txt(r.row)})
        aois,aoi_review=discover_aoi_polygons(aoi_dir,crs,known_rows,td)
        missing_aoi=[r for r in selected_rows if row_key(r) not in aois]
        if missing_aoi: raise RuntimeError('Selected RoW AOI missing: '+', '.join(missing_aoi))

        mu_gdb=resolve_management_units_gdb(management_units_source,client_source,td)
        mus_all,mu_review=load_management_units(mu_gdb,crs,kmz_refs,'Eversource')
        physical_tiles,tile_size=build_lidar_tile_footprints(lidar_source,crs,td,year,None)
        lidar_files=discover_lidar_files(lidar_source,td) if need_point_data else []

        # Persisted per-tile actual boundaries allow Tile Index / MU testing
        # without running the expensive LAS clipping/header path.
        boundary_tiles=None
        boundary_gpkg=selection_path.parent/'00_lidar_boundary_tiles.gpkg'
        if boundary_gpkg.exists():
            try:
                boundary_tiles=gpd.read_file(boundary_gpkg,layer='tile_boundaries',engine='pyogrio')
            except Exception:
                boundary_tiles=None

        deliverables=output.parent/'deliverables'; deliverables.mkdir(parents=True,exist_ok=True)
        all_tiles=[]; inv_frames=[]; xyz_review=[]; inventory=[]

        # Refresh only selected product families.
        for row in selected_rows:
            rroot=deliverables/safe_filename(row)
            if want_lidar and (rroot/'LiDAR').exists(): shutil.rmtree(rroot/'LiDAR')
            if want_pls and (rroot/'PLS').exists(): shutil.rmtree(rroot/'PLS')
            if want_tile and (rroot/'Tile_Index').exists(): shutil.rmtree(rroot/'Tile_Index')
            if want_mu and (rroot/'Management_Units').exists(): shutil.rmtree(rroot/'Management_Units')

        for row in selected_rows:
            rec=aois[row_key(row)]
            expected_epsg=_expected_epsg_for_row(row,epsg)
            if expected_epsg != epsg:
                raise RuntimeError(f'Selected RoW {row} expects EPSG:{expected_epsg}, but current processing CRS is EPSG:{epsg}')

            if need_point_data:
                row_temp=output/'temp'/safe_filename(row)
                pls_root=(deliverables/safe_filename(row)/'PLS') if want_pls else None
                counts={}
                inv=pd.DataFrame()

                reusable=[]
                if want_pls and not want_lidar:
                    delivery_lidar_dir=deliverables/safe_filename(row)/'LiDAR'
                    reusable=_load_reusable_delivery_las(
                        output,row,lidar_source,rec['geometry'],epsg,year,delivery_lidar_dir
                    )

                if reusable:
                    info(f'\nPLS for {row}: reusing {len(reusable):,} existing AOI-bounded delivery LAS file(s).')
                    info('  clipping/header rewrite skipped')
                    counts=_build_pls_from_final_las(
                        reusable,pls_root,las2las_exe,workers,row_temp,keep_temp=keep_temp
                    )
                    tg=_build_index_only_tiles_for_row(
                        row,rec['geometry'],physical_tiles,boundary_tiles,year,crs
                    )
                else:
                    info(f'\nPreparing AOI-bounded LAS working set for {row}...')
                    if want_lidar:
                        lidar_dir=deliverables/safe_filename(row)/'LiDAR'
                    else:
                        lidar_dir=row_temp/'working_las'
                    tg,inv,counts=clip_lidar_for_row(
                        row,rec['geometry'],lidar_files,crs,epsg,year,
                        lidar_dir,pls_root,tile_size,
                        write_xyz=want_pls,all_aoi_lookup=aois,
                        force_clip=force_clip,las2las_exe=las2las_exe,
                        workers=workers,temp_root=row_temp,keep_temp=keep_temp
                    )
                    if want_lidar and not tg.empty:
                        final_files=[deliverables/safe_filename(row)/'LiDAR'/txt(v) for v in tg['TILE_NAME']]
                        _write_delivery_las_manifest(
                            output,row,lidar_source,rec['geometry'],epsg,year,final_files
                        )

                if tg.empty:
                    raise RuntimeError(f'No LiDAR points written inside AOI for {row}')
                all_tiles.append(tg)
                if want_lidar and not inv.empty:
                    inv_frames.append(inv)

                if want_pls:
                    for cls,name in PLS_XYZ_CLASS_MAP.items():
                        raw_n=int(counts.get(int(cls),0)); full_file=''
                        if int(cls)==2:
                            out_n=int(counts.get('OUT_2',0))
                            mode=txt(counts.get('GROUND_MODE')) or 'LASTOOLS_ROWWIDE_ADAPTIVE'
                            note=(f'Ground: one RoW-wide LAStools adaptive TIN; '
                                  f'tolerance +/-{PLS_GROUND_TOLERANCE_FT:g} ftUS, '
                                  f'max spacing {PLS_GROUND_INITIAL_SPACING_FT:g} ftUS, sparse. '
                                  f'Automatic RoW-wide lowest-grid fallback if adaptive fails.')
                            full_file=str(Path('PLS')/'Full Class'/name)
                        elif int(cls) in {4,5}:
                            out_n=int(counts.get(f'OUT_{int(cls)}',0)); mode='LASTOOLS_THIN_3FT_XY_MAX'
                            note='Vegetation: RoW-wide LAStools lasthin; 3 ftUS XY grid; highest Z; no Z bucketing.'
                            full_file=str(Path('PLS')/'Full Class'/name)
                        else:
                            out_n=raw_n; mode='DIRECT_CLASS_EXPORT'; note=''
                        xyz_review.append({'ROW':row,'CLASS':int(cls),'FILE':name,'FULL_CLASS_FILE':full_file,
                                           'N_SOURCE_POINTS':raw_n,'N_OUTPUT_POINTS':out_n,'MODE':mode,'NOTE':note})
            else:
                tg=_build_index_only_tiles_for_row(row,rec['geometry'],physical_tiles,boundary_tiles,year,crs)
                if tg.empty:
                    raise RuntimeError(f'No LiDAR tile boundary overlaps AOI for {row}')
                all_tiles.append(tg)

        tile_index=gpd.GeoDataFrame(pd.concat(all_tiles,ignore_index=True),geometry='geometry',crs=crs)

        if inv_frames:
            inventory.extend(pd.concat(inv_frames,ignore_index=True).to_dict('records'))

        management_units=rows_to_gdf([],crs)
        if want_mu:
            selected_mus=mus_all[mus_all['ROW'].isin(selected_rows)].copy()
            management_units=build_management_unit_product(selected_mus,tile_index)

        if want_tile:
            for row in selected_rows:
                tg=tile_index[tile_index['ROW']==row].copy()
                if tg.empty: continue
                out=deliverables/safe_filename(row)/'Tile_Index'/f"{safe_filename(row)}_Tile_Index_{year}.shp"
                _write_clean_shapefile(tg,out,['ROW','TILE_NAME','CAP_YEAR','geometry'])
                inventory.append({'PRODUCT':'Tile Index','ROW':row,'DELIVERY_FILE':out.name,'STATUS':'WRITTEN'})

        if want_mu and management_units is not None and not management_units.empty:
            for row in selected_rows:
                mg=management_units[management_units['ROW']==row].copy()
                if mg.empty: continue
                out=deliverables/safe_filename(row)/'Management_Units'/f"{safe_filename(row)}_Management_Unit_{year}.shp"
                internal={'MU_IDX','MU_LAYER','MU_ROW_RAW'}
                cols=[c for c in mg.columns if c not in internal and c!='geometry']+['geometry']
                _write_clean_shapefile(mg,out,cols)
                inventory.append({'PRODUCT':'Management Units','ROW':row,'DELIVERY_FILE':out.name,'STATUS':'WRITTEN'})

        gpkg=output/'02_eversource_points.gpkg'
        if gpkg.exists(): gpkg.unlink()
        write_layer(tile_index,gpkg,'tile_index')
        if want_mu: write_layer(management_units,gpkg,'management_units')
        aoi_rows=[{'ROW':aois[row_key(r)]['ROW'],'SOURCE_FILE':aois[row_key(r)]['SOURCE_FILE'],'geometry':aois[row_key(r)]['geometry']} for r in selected_rows]
        write_layer(rows_to_gdf(aoi_rows,crs),gpkg,'row_aoi')

        pd.DataFrame(inventory).to_csv(output/'delivery_inventory.csv',index=False)
        if want_pls: pd.DataFrame(xyz_review).to_csv(output/'02_xyz_export_review.csv',index=False)
        if not aoi_review.empty: aoi_review.to_csv(output/'02_aoi_review.csv',index=False)
        if not mu_review.empty: mu_review.to_csv(output/'02_mu_review.csv',index=False)
        info(f'\nPoint-product GPKG: {gpkg}')
        info(f'Delivery tree:     {deliverables}')
        _archive_qa_run(
            output, "02_points", products=sorted(selected_products),
            selected_rows=selected_rows,
            notes={"xyz_format": "X Y Z CLASS" if want_pls else "not generated"}
        )
        return gpkg
