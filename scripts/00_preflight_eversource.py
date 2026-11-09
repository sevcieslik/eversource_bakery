#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import re
from pyproj import CRS

from eversource_pipeline_core import (
    discover_lidar_input,
    find_single_file,
    package_root,
    prompt_epsg,
    run_preflight,
)


def discover_sources(input_dir: Path, source_dir: Path):
    inp = Path(input_dir)
    src = Path(source_dir)
    if not inp.exists():
        raise RuntimeError(f"Missing input folder: {inp}")
    if not src.exists():
        raise RuntimeError(f"Missing source folder: {src}")

    lidar = discover_lidar_input(inp)
    client = find_single_file(src, ".zip", "client source ZIP")
    kmz = find_single_file(src, ".kmz", "Project SoW KMZ")
    aoi = inp / "AOI"

    gdbs = sorted([p for p in src.rglob("*.gdb") if p.is_dir()])
    preferred = [p for p in gdbs if p.name.lower() == "tvm_mus.gdb"]
    gdbs = preferred or gdbs
    mu = gdbs[0] if len(gdbs) == 1 else None
    return lidar, client, kmz, mu, aoi




SUPPORTED_PROJECT_EPSGS = (6434, 6492, 6525)

_CRS_NAME_ALIASES = {
    # Connecticut
    "NSRS_2011_CONNECTICUT_ZONE_US_FOOT": 6434,
    "NAD_1983_2011_STATEPLANE_CONNECTICUT_FIPS_0600_FT_US": 6434,

    # Massachusetts Mainland
    "NSRS_2011_MASSACHUSETTS_STATE_PLANES_MAINLAND_ZONE_US_FOOT": 6492,
    "NAD_1983_2011_STATEPLANE_MASSACHUSETTS_MAINLAND_FIPS_2001_FT_US": 6492,

    # New Hampshire
    "NSRS_2011_NEW_HAMPSHIRE_ZONE_US_FOOT": 6525,
    "NAD_1983_2011_STATEPLANE_NEW_HAMPSHIRE_FIPS_2800_FT_US": 6525,
}


def _normalise_crs_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value).upper()).strip("_")


def resolve_project_epsg(crs: CRS) -> int | None:
    """Resolve a project horizontal CRS robustly from ESRI/WKT metadata.

    Order:
      1. normal EPSG/authority lookup;
      2. direct equivalence against supported project CRSs;
      3. normalised projection-name aliases for known ESRI WKT variants.

    The alias step is deliberately limited to the project-supported CRSs, so the
    tool does not guess arbitrary EPSG codes from names.
    """
    # Normal authority matching.
    try:
        epsg = crs.to_epsg()
        if epsg is not None:
            return int(epsg)
    except Exception:
        pass

    try:
        auth = crs.to_authority()
        if auth and str(auth[0]).upper() == "EPSG":
            return int(auth[1])
    except Exception:
        pass

    # Some ESRI WKT1 definitions only match below pyproj's default 70%
    # confidence threshold.

    # Compare projection semantics rather than requiring an AUTHORITY tag.
    for candidate in SUPPORTED_PROJECT_EPSGS:
        try:
            target = CRS.from_epsg(candidate)
            if crs.equals(target, ignore_axis_order=True):
                return int(candidate)
        except Exception:
            continue

    # Known ArcGIS/ESRI naming variants seen in client .prj files.
    names = []
    try:
        names.append(crs.name)
    except Exception:
        pass
    try:
        names.append(crs.coordinate_operation.name)
    except Exception:
        pass

    # Also inspect the raw WKT names because projected CRS and operation names
    # can differ across WKT1_ESRI / WKT2 serialisations.
    try:
        wkt = crs.to_wkt()
        names.extend(re.findall(r'(?:PROJCS|PROJCRS)\["([^"]+)"', wkt))
    except Exception:
        pass

    for name in names:
        key = _normalise_crs_name(name)
        if key in _CRS_NAME_ALIASES:
            return int(_CRS_NAME_ALIASES[key])

    return None

def detect_aoi_epsg(aoi_dir: Path):
    if not aoi_dir.exists():
        return None

    prjs = [
        q for q in sorted(aoi_dir.rglob("*.prj"))
        if re.search(r"(?i)(?:^|[_ -])polygon(?:$|[_ -])", q.stem)
        or q.stem.lower().endswith("polygon")
    ]
    if not prjs:
        return None

    values = []
    for prj in prjs:
        wkt = prj.read_text(encoding="utf-8", errors="ignore").strip()
        if not wkt:
            raise RuntimeError(f"AOI .prj is empty: {prj.name}")
        crs = CRS.from_wkt(wkt)
        epsg = resolve_project_epsg(crs)
        if epsg is None:
            raise RuntimeError(
                f"Could not resolve supported project EPSG from AOI .prj: "
                f"{prj.name} ({crs.name})"
            )
        values.append((prj.name, int(epsg)))

    unique = sorted({v for _, v in values})
    if len(unique) != 1:
        detail = ", ".join(f"{name}=EPSG:{epsg}" for name, epsg in values)
        raise RuntimeError(
            "AOI Polygon files have inconsistent CRS: " + detail
        )
    return unique[0]

def main():
    p = argparse.ArgumentParser(
        description="Validate Eversource inputs, show RoW coverage and select RoWs"
    )
    p.add_argument("--input-dir", default=None)
    p.add_argument("--source-dir", default=None)
    p.add_argument("--output-root", default=None)
    p.add_argument("--epsg", type=int, default=None)
    p.add_argument("--all-ready", action="store_true", help="Non-interactive: select all RoWs with a published AOI.")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument(
        "--rows", default="",
        help="Optional comma-separated RoW names. Default: interactive selection",
    )
    p.add_argument(
        "-o", "--output", default=None,
        help="Explicit Stage 00 output. Overrides --output-root.",
    )
    p.add_argument("--lasboundary", default=None)
    p.add_argument(
        "--boundary-workers", "--boundary-cores",
        dest="boundary_cores", type=int, default=12,
    )
    p.add_argument("--boundary-concavity-m", type=float, default=50.0)
    p.add_argument("--boundary-merge-buffer-ft", type=float, default=0.1)
    args = p.parse_args()

    tool_root = package_root()
    input_dir = Path(args.input_dir) if args.input_dir else tool_root / "input"
    source_dir = Path(args.source_dir) if args.source_dir else tool_root / "source"
    output_root = (
        Path(args.output_root) if args.output_root
        else tool_root / "output"
    )
    output = (
        Path(args.output) if args.output
        else output_root / "00_preflight"
    )

    try:
        lidar, client, kmz, mu, aoi = discover_sources(input_dir, source_dir)
        epsg = args.epsg
        if epsg is None:
            epsg = detect_aoi_epsg(aoi)
            if epsg is not None:
                print(f"Project CRS from AOI .prj: EPSG:{epsg}")
            else:
                epsg = prompt_epsg()
        rows = [x.strip() for x in args.rows.split(",") if x.strip()] or None
        interactive = (rows is None and not args.all_ready)

        print(f"\nInput folder:  {input_dir}")
        print(f"Source folder: {source_dir}")
        print(f"Output root:   {output_root}")

        run_preflight(
            lidar, client, kmz, mu, aoi, epsg, args.year, output,
            selected_rows=rows,
            interactive=interactive,
            lasboundary_path=Path(args.lasboundary) if args.lasboundary else None,
            boundary_cores=args.boundary_cores,
            boundary_concavity_m=args.boundary_concavity_m,
            boundary_merge_buffer_ft=args.boundary_merge_buffer_ft,
        )
        print("\nPRE-FLIGHT COMPLETE")
        return 0
    except Exception as exc:
        print(f"\nERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
