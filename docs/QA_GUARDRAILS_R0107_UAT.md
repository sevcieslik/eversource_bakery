# R01.07 UAT - source structure attribution and EARTH-only 2D forks

Branch: `feat/standalone-qa-inspector-sidebar-20261123`
Status: not merged; operator regression required.

## Bakery Geometry
- `CORRIDOR_ORDERED` now consults original POA circuit evidence. Where **SOW_CIRCUIT and WIRE_CIRCUIT agree on a different circuit**, with at least two source wire votes, the candidate remains unmatched (`S01_CIRCUIT_CONFLICT`) instead of inheriting a wrong client structure number.
- Source structure matches beyond 15 metres need two existing corridor anchor matches; beyond 75 metres they remain unmatched (`S02_DISTANT_SOURCE_UNCONFIRMED`). A valid 15-75 m two-anchor match is tagged `CORRIDOR_DISTANT_REVIEW`. These values are conservative UAT thresholds, not a definitive geodetic standard.
- New post-LiDAR check `suppress_earth_only_2d_branches` removes **secondary** 2D parts only when confirmed EARTH conductors occupy >=85% of interior samples, PHASE/unknown conductors occupy <=5%, branch is connected to the principal line, and it is >=15 m long. EARTH means explicit DXF EARTH type or LiDAR 210 confirmed by voting, **not a heuristic or an unconfirmed wire role**.
- Original 3D source/Caydence conductors remain in product 267. Suppressed 2D branches are recorded in `01_geometry/01_earth_only_2d_branches.csv` as `EARTH_ONLY_BRANCH_EXCLUDED`; ambiguous branches stay visible and are marked `REVIEW_OR_RETAIN`.
- Does not discard the longest circuit geometry. Does not remove a branch that has PHASE/unknown conductors. Existing terminal/substation and 3D recovery routines remain untouched.

## Independent QA Inspector
- `S06_FOREIGN_CIRCUIT_SUPPORT`: confirmed disagreement between assigned client circuit and source POA SoW + raw 3D circuit votes.
- `S07_DISTANT_SOURCE_MATCH`: corridor-assigned source structure >15m away from physical support.
- `S08_SOURCE_ID_MULTIPLE_SUPPORTS`: same source structure identity assigned to multiple distinct TOPO_IDs in one circuit; RoW duplicates of one TOPO_ID are allowed.
- `E01_EARTH_ONLY_2D_BRANCH`: secondary 2D geometry appears along EARTH conductor(s) without non-EARTH conductor support. This is an independent diagnostic, not a proof of classification if source role evidence is incomplete.
- Original source wire inventory, GPKG, 45/80/267 comparisons and terminal T01/T02 remain unchanged.

## Required UAT
1. Re-run Cluster 17 G128 162/163/164: expect no automatic G128/163 assignment to independently confirmed C129 POA. Check whether 164 is still source-numbered by terminal POA recovery; that separate matcher was not changed.
2. Check Cluster 55 EARTH-only side branch: product 80 should omit only the confirmed EARTH-only part; product 267 must retain all original 3D wires.
3. Regression on Clusters 12, 39 and 55: source match totals, unassigned structures, 2D branch counts, terminal connectivity and delivered wire inventory.
4. Run integrated and left-sidebar QA Inspectors; review S06-S08 and E01, and check no false positives on shared supports.

This is an UAT branch. No assertion that local Windows runtime or full project data have been tested by this change.
