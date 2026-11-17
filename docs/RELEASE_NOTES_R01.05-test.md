# Eversource Tester R01.05-test – geometry attribution fixes

Test build for the NMIP26028 Bakery Geometry feedback (Clusters 12, 17, 39, 55).
Before/after on Cluster 12: `R01.05-test_Cluster12_before_after.png`.
Built on R01.04. Only the 01 Geometry stage changes. LiDAR, PLS and points stages are untouched.

## Install

1. Make a copy of an existing `Eversource_Tester_R01.04` folder (keep `.venv`, `source`, `input`).
2. Copy `scripts/eversource_pipeline_core.py` and `scripts/bakery_version.py` from this package over the copy.
3. Run Geometry as usual. The console shows `EVERSOURCE TESTER R01.05-test - 01 GEOMETRY`.

Do not overwrite the production R01.04 folder.

## Changes

**A. 2D circuit wires are published per RoW**
A circuit is written to a RoW folder only where it has spans assigned to that RoW. Each RoW folder gets only its own section, with ROW set to that RoW. Previously the whole circuit was copied into every RoW that contained any of its Structures.
Feedback addressed: 371 identical in CT_06 and CT_16; extra wires where RoWs overlap or cross (NH_05/NH_14).

**B. Terminal POA recovery no longer duplicates Structures**
- A terminal POA is skipped if the same circuit already has a Structure within 15 m in any RoW. Previously only the same RoW was checked.
- The same POA cannot be recovered twice for one circuit.
- A recovered terminal POA is assigned only to a RoW that carries its circuit in the SoW.

Feedback addressed: duplicated POAs where circuits/RoWs overlap; POAs appearing in the wrong RoW.

**C. Circuits are no longer cut at a single failed span**
- **C1.** Conductors split in the middle of a span in the DXF are re-joined for topology. The pieces must meet in XYZ within 0.5 m, more than 30 m from any POA, and be collinear. The source 3D wires are unchanged; both pieces stay in Caydence. Rejoined pieces are flagged `SPLICED_MIDSPAN` in `source_wire_review`.
- **C2.** Disconnected parts of a circuit with at least 2 spans are delivered as `PATH_ROLE = MAIN_PART`. Previously they were dropped as `DISCONNECTED_CIRCUIT_COMPONENT`, and only their POAs came back as terminal POAs, without wires.

Feedback addressed: missing wires and gaps; circuits stopping mid-span; POAs without wires.

**D. Caydence 3D wires follow their circuit's RoW**
A Caydence wire attributed to a circuit is moved to the nearest RoW that carries that circuit, if its nearest-centreline RoW does not. The moves are listed in the new `01_geometry/01_caydence_row_circuit_fix.csv`.
Feedback addressed: Caydence wires mixed between RoWs in shared tiles (C55).

## Cluster 12 test results (NH_05/09/11/14/25, same inputs)

| Check | R01.04 | R01.05-test |
|---|---:|---:|
| Circuit folders | 45 | 34 |
| Circuit folders with < 50 % of wire inside the RoW | 15 | 0 |
| 2D wire length written into another RoW's folder | 206.8 km | 0 km |
| Circuit length discarded as disconnected | 18.4 km | 0 km |
| Total delivered 2D circuit length | 214.4 km | 233.2 km |
| Recovered terminal POAs | 173 | 6 |
| Structure pairs closer than 3 m | 39 | 5 |
| Structures in a RoW without their circuit (SoW) | 26 | 0 |
| Caydence wires in a RoW without their circuit | 67 | 0 |
| Source structure reused within a circuit | 33 | 1 |

Recovered circuit length:
- 326: +7.7 km
- Z119: +8.4 km (the NH_25 section is now delivered)
- K165: +2.8 km

Folders removed as wrong: NH_05 I158 and W144; NH_09 326 and 380; NH_11 3124, 326 and 380; NH_14 3124, 326, 380 and T120.

The R01.04 SCHF/SCSH regression tests still pass.
The baseline run reproduced the operator's R01.04 Cluster 12 output exactly: 173 terminal POAs, 38 terminal 2D, 62 corridor matches.

## Known limits / please check

- **Shared structures:** the 5 remaining close pairs are one physical structure carrying two circuits (e.g. K165/T77, K165/O149, G192/T33). Each circuit still gets its own record. We need a decision on whether Caydence RoW Structures should hold one record per physical structure.
- **MAIN_PART quality:** these parts are delivered with the circuit attribution they already had. Please QA them first. They are listed in `selected_span_wires` with `PATH_ROLE = MAIN_PART`.
- **Attribution not yet changed:** circuit attribution on shared centrelines is unchanged (e.g. 3195/380, H123/K165, CONF = REVIEW). That is the next step.
- **No LiDAR in our test run:** ground Z and LiDAR wire roles in our run are not meaningful. They do not affect the 2D/attribution changes above.
