#!/usr/bin/env python3
"""Re-export geometry deliverables from an operator-edited QA GeoPackage.

Workflow
  1. Run Geometry as usual.
  2. Open output/01_geometry/01_eversource_geometry.gpkg in QGIS and edit:
       selected_span_wires    ROW / CIRCUIT of 2D spans (delete = drop span)
       recovered_terminal_2d  CIRCUIT of 2D conductors into substations
       structures_final       ROW / CIRCUIT of Structures (delete = drop)
       caydence_wires_3d      ROW / CIRCUIT / WIRE_TYPE of Caydence wires
     ROW may hold several RoWs, e.g. NH_05;NH_25, to deliver in both.
  3. Run this script (or "Export from edited GPKG" in the GUI / launcher).

Only deliverables/<RoW>/Circuit and deliverables/<RoW>/Caydence are rebuilt.
The previous Circuit/Caydence folders are copied to QA first.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from eversource_pipeline_core import (
    check_gpkg_export_consistency,  # noqa: F401  (re-exported for tests)
    export_geometry_from_gpkg,
    gpkg_export_rows,
    info,
    package_root,
)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Rebuild Circuit and Caydence deliverables from an edited 01_eversource_geometry.gpkg"
    )
    p.add_argument("--output-root", default=None,
                   help="Pipeline output root (holds 01_geometry and deliverables).")
    p.add_argument("--gpkg", default=None,
                   help="Edited GPKG. Default: <output-root>/01_geometry/01_eversource_geometry.gpkg")
    p.add_argument("--year", type=int, default=None,
                   help="Delivery year. Default: the year stored in the GPKG.")
    p.add_argument("--rows", default=None,
                   help="RoWs to publish, e.g. NH_02,NH_27. Default: the RoWs selected for "
                        "the Geometry run (stored in the GPKG).")
    p.add_argument("--list-rows", action="store_true",
                   help="Print the stored and found RoWs and exit.")
    p.add_argument("--no-backup", action="store_true",
                   help="Do not copy the current Circuit/Caydence folders to QA first.")
    # Accepted for launcher compatibility; not used.
    p.add_argument("--input-dir", default=None)
    p.add_argument("--source-dir", default=None)
    p.add_argument("--epsg", default=None)
    args = p.parse_args()
    try:
        from bakery_version import bakery_build_label
        print(f"[BAKERY] {bakery_build_label()}", flush=True)
    except Exception:
        pass

    output_root = Path(args.output_root) if args.output_root else package_root() / "output"
    gpkg = Path(args.gpkg) if args.gpkg else output_root / "01_geometry" / "01_eversource_geometry.gpkg"
    if not gpkg.is_file():
        print(f"ERROR: GPKG not found: {gpkg}")
        return 2
    if args.list_rows:
        selected, found = gpkg_export_rows(gpkg)
        print("SELECTED_ROWS=" + ";".join(selected))
        print("FOUND_ROWS=" + ";".join(found))
        return 0
    rows = [r.strip() for r in args.rows.replace(";", ",").split(",") if r.strip()] if args.rows else None
    deliverables = output_root / "deliverables"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    qa_dir = output_root / "QA" / f"{stamp}_01_geometry_export_from_gpkg"
    qa_dir.mkdir(parents=True, exist_ok=True)
    backup = None if args.no_backup else qa_dir / "deliverables_before"

    info("=" * 70)
    info("EXPORT GEOMETRY FROM EDITED GPKG")
    info(f"GPKG:         {gpkg}")
    info(f"Deliverables: {deliverables}")
    if backup:
        info(f"Backup:       {backup}")
    info("=" * 70)

    inv, checks = export_geometry_from_gpkg(gpkg, deliverables, year=args.year,
                                            backup_dir=backup, rows=rows)
    info("RoWs exported: " + ", ".join(inv.attrs.get("rows", [])))
    inv.to_csv(qa_dir / "01_geometry_delivery_inventory.csv", index=False)
    checks.to_csv(qa_dir / "01_gpkg_export_checks.csv", index=False)

    written = inv[inv.get("STATUS", "") == "WRITTEN"] if not inv.empty else inv
    for product, n in written.groupby("PRODUCT").size().items() if not written.empty else []:
        info(f"  {product}: {n} file(s)")
    if checks.empty:
        info("Consistency checks: OK")
    else:
        for check, n in checks.groupby("CHECK")["N"].sum().items():
            info(f"  CHECK {check}: {n}")
        info(f"Consistency findings for QA: {qa_dir / '01_gpkg_export_checks.csv'}")
    info(f"QA: {qa_dir}")
    info("EXPORT FROM GPKG COMPLETE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
