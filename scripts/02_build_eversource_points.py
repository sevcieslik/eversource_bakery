#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eversource_pipeline_core import (
    discover_lidar_input,
    find_single_file,
    package_root,
    prompt_epsg,
    run_points_stage,
)


def discover_sources(input_dir: Path, source_dir: Path):
    inp = Path(input_dir)
    src = Path(source_dir)
    lidar = discover_lidar_input(inp)
    client = find_single_file(src, ".zip", "client source ZIP")
    kmz = find_single_file(src, ".kmz", "Project SoW KMZ")
    aoi = inp / "AOI"

    gdbs = sorted([p for p in src.rglob("*.gdb") if p.is_dir()])
    preferred = [p for p in gdbs if p.name.lower() == "tvm_mus.gdb"]
    gdbs = preferred or gdbs
    mu = gdbs[0] if len(gdbs) == 1 else None
    return lidar, client, kmz, mu, aoi


def main():
    p = argparse.ArgumentParser(
        description="Build selected Eversource non-geometry delivery products"
    )
    p.add_argument("--input-dir", default=None)
    p.add_argument("--source-dir", default=None)
    p.add_argument("--output-root", default=None)
    p.add_argument("--epsg", type=int, default=None)
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--selection", default=None)
    p.add_argument(
        "--products",
        default="lidar,pls,tile_index,management_units",
    )
    p.add_argument("--force-clip", action="store_true")
    p.add_argument("--las2las", default=None)
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--keep-temp", action="store_true")
    p.add_argument(
        "-o", "--output", default=None,
        help="Explicit Stage 02 output. Overrides --output-root.",
    )
    args = p.parse_args()
    try:
        from bakery_version import bakery_build_label
        print(f"[BAKERY] {bakery_build_label()}", flush=True)
    except Exception:
        pass

    tool_root = package_root()
    input_dir = Path(args.input_dir) if args.input_dir else tool_root / "input"
    source_dir = Path(args.source_dir) if args.source_dir else tool_root / "source"
    output_root = (
        Path(args.output_root) if args.output_root
        else tool_root / "output"
    )
    selection = (
        Path(args.selection) if args.selection
        else output_root / "00_preflight" / "00_preflight_selection.json"
    )
    output = (
        Path(args.output) if args.output
        else output_root / "02_points"
    )

    try:
        if not selection.exists():
            raise RuntimeError(f"Missing preflight selection: {selection}")

        sel = json.loads(selection.read_text(encoding="utf-8"))
        epsg = args.epsg or int(sel.get("epsg") or 0) or prompt_epsg()
        products = {
            x.strip().lower()
            for x in args.products.split(",")
            if x.strip()
        }
        lidar, client, kmz, mu, aoi = discover_sources(
            input_dir, source_dir
        )

        print(f"\nInput folder:  {input_dir}")
        print(f"Source folder: {source_dir}")
        print(f"Output root:   {output_root}")

        run_points_stage(
            lidar, client, kmz, mu, aoi, selection,
            epsg, args.year, output,
            products=products,
            force_clip=args.force_clip,
            las2las_path=Path(args.las2las) if args.las2las else None,
            workers=args.workers,
            keep_temp=args.keep_temp,
        )
        print("\nSELECTED POINT PRODUCTS COMPLETE")
        return 0
    except Exception as exc:
        print(f"\nERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
