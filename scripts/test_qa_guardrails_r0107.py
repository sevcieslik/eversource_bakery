#!/usr/bin/env python3
"""Fast, synthetic regression tests for R01.07 EARTH-only branch rules.

Run: python scripts/test_qa_guardrails_r0107.py
"""
import unittest
import pandas as pd
import geopandas as gpd
from shapely.geometry import LineString, MultiLineString

from eversource_pipeline_core import suppress_earth_only_2d_branches


def lines():
    main = LineString([(0, 0), (100, 0)])
    branch = LineString([(50, 0), (50, 100)])
    circuit = gpd.GeoDataFrame(
        [{"CIRCUIT": "G128", "ROW": "NH_27",
          "geometry": MultiLineString([main, branch]), "LENGTH_M": 200.0}],
        geometry="geometry", crs="EPSG:3857"
    )
    wires = gpd.GeoDataFrame([
        {"CIRCUIT": "G128", "WIRE_ID": "E1", "WIRE_TYPE": "EARTH",
         "geometry": LineString([(50, 0, 10), (50, 100, 10)])},
        {"CIRCUIT": "G128", "WIRE_ID": "P1", "WIRE_TYPE": "PHASE",
         "geometry": LineString([(0, 0, 10), (100, 0, 10)])},
    ], geometry="geometry", crs="EPSG:3857")
    return circuit, wires


class EarthOnlyBranchTests(unittest.TestCase):
    def test_verified_earth_only_branch_removed_from_2d(self):
        circuit, wires = lines()
        verified = pd.DataFrame([
            {"WIRE_ID": "E1", "METHOD": "LIDAR_CLASS_187_210", "FINAL_TYPE": "EARTH"},
            {"WIRE_ID": "P1", "METHOD": "LIDAR_CLASS_187_210", "FINAL_TYPE": "PHASE"},
        ])
        out, audit = suppress_earth_only_2d_branches(circuit, wires, 1.0, verified)
        self.assertAlmostEqual(out.geometry.iloc[0].length, 100.0)
        self.assertEqual(audit.iloc[0]["ACTION"], "EARTH_ONLY_BRANCH_EXCLUDED")
        self.assertEqual(len(wires), 2, "Original 3D wire inventory must not change")

    def test_unconfirmed_earth_branch_preserved(self):
        circuit, wires = lines()
        unconfirmed = pd.DataFrame([
            {"WIRE_ID": "E1", "METHOD": "INSUFFICIENT_LIDAR", "FINAL_TYPE": "EARTH"},
        ])
        out, _ = suppress_earth_only_2d_branches(circuit, wires, 1.0, unconfirmed)
        self.assertAlmostEqual(out.geometry.iloc[0].length, 200.0)

    def test_phase_branch_preserved(self):
        circuit, wires = lines()
        wires.loc[wires["WIRE_ID"] == "E1", "WIRE_TYPE"] = "PHASE"
        verified = pd.DataFrame([
            {"WIRE_ID": "E1", "METHOD": "LIDAR_CLASS_187_210", "FINAL_TYPE": "PHASE"},
        ])
        out, _ = suppress_earth_only_2d_branches(circuit, wires, 1.0, verified)
        self.assertAlmostEqual(out.geometry.iloc[0].length, 200.0)


if __name__ == "__main__":
    unittest.main()
