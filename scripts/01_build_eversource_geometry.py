#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from eversource_pipeline_core import (
    discover_lidar_input,
    discover_two_dxfs,
    find_single_file,
    package_root,
    prompt_epsg,
    run_geometry_stage,
)


def discover_geometry_inputs(input_dir: Path, source_dir: Path):
    inp = Path(input_dir)
    src = Path(source_dir)
    wires, points = discover_two_dxfs(inp)
    lidar = discover_lidar_input(inp)
    client = find_single_file(src, ".zip", "client source ZIP")
    kmz = find_single_file(src, ".kmz", "Project SoW KMZ")
    return wires, points, lidar, client, kmz


def main():
    p = argparse.ArgumentParser(
        description="Build Eversource wires + physical Structure XY geometry"
    )
    p.add_argument("--input-dir", default=None)
    p.add_argument("--source-dir", default=None)
    p.add_argument("--output-root", default=None)
    p.add_argument("--epsg", type=int, default=None)
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--selection", default=None)
    p.add_argument("--workers", type=int, default=None,
                   help="Processes for LiDAR ground/wire sampling (default: auto, max 8).")
    p.add_argument(
        "-o", "--output", default=None,
        help="Explicit Stage 01 output. Overrides --output-root.",
    )
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
        else output_root / "01_geometry"
    )
    selection = (
        Path(args.selection) if args.selection
        else output_root / "00_preflight" / "00_preflight_selection.json"
    )

    try:
        wires, points, lidar, client, kmz = discover_geometry_inputs(
            input_dir, source_dir
        )
        epsg = args.epsg
        if epsg is None and selection.exists():
            epsg = (
                int(json.loads(selection.read_text(encoding="utf-8")).get("epsg") or 0)
                or None
            )
        epsg = epsg or prompt_epsg()

        print(f"\nInput folder:  {input_dir}")
        print(f"Source folder: {source_dir}")
        print(f"Output root:   {output_root}")

        run_geometry_stage(
            wires, points, lidar, client, kmz,
            epsg, args.year, output,
            selection if selection.exists() else None,
            lidar_workers=args.workers,
        )
        print("\nGEOMETRY COMPLETE")
        return 0
    except Exception as exc:
        print(f"\nERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
