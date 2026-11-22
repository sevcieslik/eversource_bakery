#!/usr/bin/env python3
"""Reconcile Caydence 3D wires in one Geometry GPKG with product 267 SHPs."""
from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import Counter
from pathlib import Path

import geopandas as gpd


def split_rows(value):
    return [x.strip() for x in str(value or "").replace(",", ";").split(";") if x.strip()]


def audit(gpkg, deliverables):
    gpkg, deliverables = Path(gpkg), Path(deliverables)
    with sqlite3.connect(gpkg) as db:
        settings = dict(db.execute("SELECT KEY, VALUE FROM export_settings"))
    selected = set(split_rows(settings.get("SELECTED_ROWS")))
    year = str(settings.get("YEAR") or "2026")
    g = gpd.read_file(gpkg, layer="caydence_wires_3d")
    expected = {row: Counter() for row in selected}
    issues = []
    for r in g.itertuples():
        for row in split_rows(r.ROW):
            key = (str(r.WIRE_ID), str(r.CIRCUIT))
            if row in selected:
                expected[row][key] += 1
            else:
                issues.append(dict(CHECK="ROW_OUTSIDE_SELECTION", ROW=row,
                                   WIRE_ID=key[0], CIRCUIT=key[1],
                                   DETAILS="Exists in GPKG but is outside selected deliverable RoWs"))
    counts = {}
    for row in sorted(selected):
        shp = deliverables / "267_caydence_conductors" / row / f"{row}_3D_Wires_{year}.shp"
        if not shp.is_file():
            issues.append(dict(CHECK="MISSING_PRODUCT_267", ROW=row,
                               WIRE_ID="", CIRCUIT="", DETAILS=str(shp)))
            counts[row] = (sum(expected[row].values()), None)
            continue
        delivered = gpd.read_file(shp)
        actual = Counter((str(r.WIRE_ID), str(r.CIRCUIT)) for r in delivered.itertuples())
        counts[row] = (sum(expected[row].values()), sum(actual.values()))
        for (wid, circ), n in (expected[row] - actual).items():
            issues.append(dict(CHECK="MISSING_FROM_267", ROW=row,
                               WIRE_ID=wid, CIRCUIT=circ, DETAILS=f"{n} record(s)"))
        for (wid, circ), n in (actual - expected[row]).items():
            issues.append(dict(CHECK="EXTRA_IN_267", ROW=row,
                               WIRE_ID=wid, CIRCUIT=circ, DETAILS=f"{n} record(s)"))
    return issues, counts


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--gpkg", type=Path, required=True)
    p.add_argument("--deliverables", type=Path, required=True)
    p.add_argument("--csv", type=Path, default=Path("caydence_delivery_audit.csv"))
    args = p.parse_args()
    issues, counts = audit(args.gpkg, args.deliverables)
    with args.csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["CHECK", "ROW", "WIRE_ID", "CIRCUIT", "DETAILS"])
        w.writeheader()
        w.writerows(issues)
    for row, (expected, actual) in sorted(counts.items()):
        print(f"{row}: expected={expected} delivered={actual}")
    print("Findings:", dict(Counter(x["CHECK"] for x in issues)))
    print("CSV:", args.csv)
    # An outside-selection wire is explicitly reported; whether it should be
    # delivered elsewhere requires the operator/source RoW decision.
    return int(any(x["CHECK"] != "ROW_OUTSIDE_SELECTION" for x in issues))


if __name__ == "__main__":
    raise SystemExit(main())
