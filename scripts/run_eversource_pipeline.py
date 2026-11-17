#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
PY = sys.executable
sys.path.insert(0, str(SCRIPT_DIR))

from workspace_paths import choose_workspace
from bakery_version import BAKERY_VERSION, BAKERY_RELEASE

PRODUCTS = {
    "1": ("geometry", "Geometry - 2D Wires, Structures, Caydence"),
    "2": ("lidar", "LiDAR - AOI-clipped LAS with delivery header"),
    "3": ("pls", "PLS XYZ - 15 NMIP26068 feature files + Full Class backups"),
    "4": ("tile_index", "Tile Index"),
    "5": ("management_units", "Management Units"),
}


def _fmt_runtime(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def banner():
    print("")
    print("=" * 80)
    print("  NM GROUP - TX VEG DELIVERY BAKERY")
    print(f"  NMIP26068 Eversource Tester {BAKERY_VERSION}")
    print(f"  {BAKERY_RELEASE}")
    print("=" * 80)


def call(stage, script, *args):
    print("\n" + "=" * 80)
    print(f"[ {stage} ]  {Path(script).name}")
    print("=" * 80)
    cmd = [PY, str(ROOT / script), *map(str, args)]
    r = subprocess.run(cmd)
    if r.returncode:
        raise SystemExit(r.returncode)


def choose_products():
    print("\nPRODUCT GENERATION")
    print("  A. Generate all")
    print("  S. Generate selected")
    print("  Q. Quit")
    while True:
        mode = input("Selection [A/S/Q]: ").strip().upper()
        if mode == "Q":
            return []
        if mode == "A":
            return [v[0] for v in PRODUCTS.values()]
        if mode == "S":
            print("\nSelect product groups:")
            for k, (_, label) in PRODUCTS.items():
                print(f"  {k}. {label}")
            raw = input(
                "Enter numbers separated by comma/space (e.g. 1,3,4): "
            ).strip()
            keys = []
            for token in raw.replace(",", " ").replace(";", " ").split():
                if token in PRODUCTS and token not in keys:
                    keys.append(token)
            if keys:
                return [PRODUCTS[k][0] for k in keys]
            print("No valid product selected.")
            continue
        print("Enter A, S or Q.")


def main():
    started = time.monotonic()
    banner()
    workspace = choose_workspace(ROOT)
    if workspace is None:
        print("\nCancelled.")
        return 0

    input_dir = Path(workspace["input_dir"])
    source_dir = Path(workspace["source_dir"])
    output_root = Path(workspace["output_root"])
    output_root.mkdir(parents=True, exist_ok=True)

    common = [
        "--input-dir", str(input_dir),
        "--source-dir", str(source_dir),
        "--output-root", str(output_root),
    ]

    call("PREHEATING", "scripts/00_preflight_eversource.py", *common)

    selection_path = output_root / "00_preflight" / "00_preflight_selection.json"
    try:
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
    except Exception:
        selection = {}

    if not selection.get("selected_rows"):
        print("\nPIPELINE STOPPED CLEANLY AFTER PRE-FLIGHT")
        print("No AOI-ready RoWs are selected.")
        print("Add the missing Polygon AOI file(s) and run this BAT again.")
        print("The cached LiDAR boundary will be reused if the LiDAR input is unchanged.")
        return 0

    products = choose_products()
    if not products:
        print("\nNo products selected. Exiting.")
        return 0

    product_file = output_root / "00_preflight" / "00_product_selection.json"
    product_file.parent.mkdir(parents=True, exist_ok=True)
    product_file.write_text(
        json.dumps({"products": products}, indent=2),
        encoding="utf-8",
    )
    print("\nSelected products: " + ", ".join(products))

    if "geometry" in products:
        call(
            "MIXING",
            "scripts/01_build_eversource_geometry.py",
            *common,
            "--selection", str(selection_path),
        )

    point_products = [p for p in products if p != "geometry"]
    if point_products:
        point_stage_bits = []
        if "lidar" in point_products:
            point_stage_bits.append("BAKING")
        if "pls" in point_products:
            point_stage_bits.append("ICING")
        if any(p in point_products for p in ("tile_index", "management_units")):
            point_stage_bits.append("PACKING")
        call(
            " / ".join(point_stage_bits) or "PACKING",
            "scripts/02_build_eversource_points.py",
            *common,
            "--selection", str(selection_path),
            "--products", ",".join(point_products),
        )

    elapsed = time.monotonic() - started
    print("\n" + "=" * 80)
    print("  BAKE COMPLETE")
    print(f"  Total runtime: {_fmt_runtime(elapsed)}")
    print(f"  Output: {output_root}")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
