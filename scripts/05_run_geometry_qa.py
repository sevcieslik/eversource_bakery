#!/usr/bin/env python3
"""Bakery Geometry QA launcher. Read-only, independent from Geometry execution."""
from __future__ import annotations
import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def files(root, suffix):
    return sorted((p for p in root.rglob("*") if p.is_file() and p.suffix.lower() == suffix),
                  key=lambda p: (len(p.parts), str(p).lower())) if root.is_dir() else []


def choose_dxf(folder, kind):
    candidates = files(folder, ".dxf")
    if kind == "wires":
        ranked = [p for p in candidates if "wire" in p.name.lower()
                  and "vector" in p.name.lower()]
        ranked = ranked or [p for p in candidates if "wire" in p.name.lower()]
    else:
        ranked = [p for p in candidates if "attachment" in p.name.lower()
                  and "point" in p.name.lower()]
        ranked = ranked or [p for p in candidates if "tower" in p.name.lower() and "top" in p.name.lower()]
    if len(ranked) != 1:
        raise RuntimeError(f"Expected exactly one original {kind} DXF in {folder}, found {len(ranked)}: "
                           + ", ".join(str(p.name) for p in ranked))
    return ranked[0]


def choose_source(source):
    zips = files(source, ".zip")
    shapefiles = [p for p in files(source, ".shp")
                  if "structure" in p.name.lower() and "reference" not in p.name.lower()]
    preferred_shp = [p for p in shapefiles if "t_structures" in p.name.lower()]
    if len(preferred_shp) == 1:
        return "--source-structures", preferred_shp[0]
    pref_zip = [p for p in zips if "source" in p.name.lower()]
    if len(pref_zip) == 1:
        return "--source-zip", pref_zip[0]
    if len(shapefiles) == 1:
        return "--source-structures", shapefiles[0]
    raise RuntimeError("Cannot select source structures unambiguously. "
                       "Provide one T_Structures*.shp or one *source*.zip under source/.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input-dir", type=Path, required=True)
    p.add_argument("--source-dir", type=Path, required=True)
    p.add_argument("--output-root", type=Path, required=True)
    p.add_argument("--qa-script", type=Path, default=Path(__file__).with_name("qa_inspector.py"))
    a = p.parse_args()
    out = a.output_root.resolve()
    gpkg = out / "01_geometry" / "01_eversource_geometry.gpkg"
    delivery = out / "deliverables"
    if not gpkg.is_file():
        raise RuntimeError(f"Geometry GPKG not found: {gpkg}. Run Geometry first.")
    if not delivery.is_dir():
        raise RuntimeError(f"Deliverables folder missing: {delivery}. Complete Geometry first.")
    if not a.qa_script.is_file():
        raise RuntimeError(f"QA Inspector v0.3 missing: {a.qa_script}. "
                           "Install the bundled QA Inspector into Bakery/scripts/.")
    wire = choose_dxf(a.input_dir, "wires")
    poa = choose_dxf(a.input_dir, "poa")
    kind, source = choose_source(a.source_dir)
    sqlite = [x for x in files(a.source_dir, ".sqlite") if "sow" in x.name.lower()]
    sqlite += [x for x in files(a.source_dir, ".db") if "sow" in x.name.lower()]
    sqlite = list(dict.fromkeys(sqlite))
    if len(sqlite) != 1:
        raise RuntimeError("Expected exactly one SoW SQLite database in source/; "
                           f"found {len(sqlite)}. Substation terminal QA requires the actual source SoW.")
    qa_root = out / "QA"
    target = qa_root / "geometry_validation"
    qa_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%fZ")
    staging = qa_root / f".geometry_validation_{stamp}"
    staging.mkdir()
    cmd = [sys.executable, str(a.qa_script), "--wires-dxf", str(wire),
           "--poa-dxf", str(poa), kind, str(source),
           "--sow-sqlite", str(sqlite[0]), "--gpkg", str(gpkg),
           "--deliverables", str(delivery), "--out", str(staging)]
    print("Geometry QA: inspecting one GPKG and its deliverables", flush=True)
    print("Input DXF:", wire, flush=True)
    print("Source:", source, flush=True)
    print("SoW SQLite:", sqlite[0], flush=True)
    print("GPKG:", gpkg, flush=True)
    print("Deliverables:", delivery, flush=True)
    rc = subprocess.call(cmd)
    if rc != 0 or not (staging / "QA_Report.html").is_file():
        print(f"QA FAILED (exit={rc}). Staged diagnostics retained in {staging}", flush=True)
        return rc or 2
    if target.exists():
        archive = qa_root / f"geometry_validation_archive_{stamp}"
        target.rename(archive)
        print("Previous QA archived:", archive, flush=True)
    staging.rename(target)
    deliverable_snapshot = sorted(
        (str(x.relative_to(delivery)).replace("\\\\", "/"), x.stat().st_size, x.stat().st_mtime_ns)
        for x in delivery.rglob("*") if x.is_file()
    )
    manifest = {"created_utc": stamp, "gpkg": str(gpkg),
                "gpkg_size": gpkg.stat().st_size, "gpkg_mtime_ns": gpkg.stat().st_mtime_ns,
                "deliverables": str(delivery), "delivery_snapshot": deliverable_snapshot,
                "source_dxf": str(wire), "poa_dxf": str(poa),
                "sow_sqlite": str(sqlite[0])}
    (target / "QA_Run_Manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("QA COMPLETE; operator review required", flush=True)
    print("QA_REPORT=" + str(target / "QA_Report.html"), flush=True)
    print("QA_OUTPUT=" + str(target), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"QA ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2)
