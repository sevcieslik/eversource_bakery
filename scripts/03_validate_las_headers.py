#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import laspy

from eversource_pipeline_core import (
    _crs_epsg_components,
    _count_crs_vlrs,
    _validate_delivery_las_crs,
)


def main():
    p = argparse.ArgumentParser(description="Validate CRS metadata in delivered LAS/LAZ files")
    p.add_argument("path", nargs="?", default=None, help="LAS/LAZ file or folder")
    p.add_argument("--epsg", type=int, required=True, help="Expected horizontal EPSG")
    args = p.parse_args()

    root = Path(args.path) if args.path else Path(__file__).resolve().parents[1] / "output" / "02_points" / "deliverables"
    files = [root] if root.is_file() else sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".las", ".laz"}
    )
    if not files:
        print(f"No LAS/LAZ files found under: {root}")
        return 1

    bad = 0
    for path in files:
        try:
            with laspy.open(path, read_evlrs=True) as fh:
                crs, h_epsg, v_epsg = _validate_delivery_las_crs(
                    fh.header, args.epsg, context=path.name
                )
                print(
                    f"OK  {path.name} | LAS {fh.header.version} | "
                    f"H={h_epsg} V={v_epsg} | CRS records={_count_crs_vlrs(fh.header)} | "
                    f"{crs.name}"
                )
        except Exception as exc:
            bad += 1
            print(f"FAIL {path.name} | {exc}")

    print()
    print(f"Validated: {len(files)} | failed: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
