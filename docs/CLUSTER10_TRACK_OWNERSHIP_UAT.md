# Cluster 10: track ownership correction UAT

## Symptom
The completed Cluster 10 GPKG had 241 Structures: 93 matched source IDs and 148 unmatched (379:86, A152:62). Original POA evidence places 379/A152/N186 structures on physical supports attributed to different circuits by the Geometry track assignment.

## Root cause
The original `assign_tracks_to_circuits` ranked combined SoW geometry and DXF voltage/layer clues, but did not use the independently source-corroborated original POA support identity to verify a track-level circuit choice. An incorrectly labelled entire track produces systematic `DUMMY_NO_SOURCE` for structures that actually have compatible source records at the correct physical POA.

## Guardrail in this branch
- Keep the existing geometric/voltage assignment as initial choice.
- For each physical track, collect distinct original `TOPO_ID` endpoints of its source span bundles. A support counts only if its POA has >=2 wire ownership votes and the matching circuit has an original source Structure within 15 m.
- Require at least **4 independently corroborated physical supports** and >= **80% agreement** among corroborated supports before replacing the track circuit. A single POA or geometrically nearest wire is insufficient.
- Write `ORIGINAL_CKEY`, `POA_SOURCE_CKEY`, `POA_SOURCE_MATCHES`, `POA_SOURCE_TOTAL` and `LAYER_RULE` to the `track_assignment_review` layer. Corrected tracks have `LAYER_RULE=POA_SOURCE_CORRECTED`, `CONF=REVIEW`, with a console message.
- All output 2D spans and structures are rebuilt using the corrected track. Do not merely re-export an old GPKG; restart **Geometry**. Source 3D geometry is unchanged.

## Pre-change diagnostics, Cluster 10
In the operator-provided GPKG, matching original POAs against source structures within 15 m and a 2 m track-geometry proximity screen produced:
- Track 1: 381 -> 381 (62 verified supports).
- Track 2: A152, but 379 has 58 verified supports.
- Track 3: 379, but A152 has 19 verified supports.
- Track 4: 381 -> 381 (27 verified supports).
- Track 5: 379, but N186 has 20 verified supports.
- Track 6/7: no verified supports under the same geometry screen (retain original attribution).

These are a diagnostic prediction based on **previous output**, not proof that the modified pipeline has passed a fresh run. Bundle endpoint-based counts can differ.

## UAT
1. Replace `scripts/eversource_pipeline_core.py` only, in Rachel's already-installed branch and runtime.
2. Back up current output; rerun **Geometry**, not just `Re-export from GPKG`.
3. Check `track_assignment_review` for corrected track labels and source evidence counts, and compare `structures_final` source matches against 93/241 baseline.
4. Verify 379, A152, N186, 381 voltage, wire product 80, all source 3D wires in product 267, and end structures. Differences in DXF voltage labels must remain reviewable.
5. Do not merge to main until Cluster 10 and other cluster regressions pass.

## Cluster 10 failed UAT and voltage guard (23 Nov 2026)

Operator reran Geometry after the first POA/source track-owner correction. Although source-matched structures rose from **93 to 193**, Inspector reported **650 W04_VOLTAGE_MISMATCH** findings. The new GPKG confirmed:

- Track 2 A152 -> 379: source DXF `Mainline_115kV` (299 conductors) was assigned circuit 379 at 345 kV.
- Track 3 379 -> A152: source DXF `Mainline_345kV` (250 conductors) was assigned circuit A152 at 115 kV.
- Track 5 379 -> N186: source DXF `Mainline_345kV` (101 conductors) was assigned circuit N186 at 115 kV.
- Total 650 voltage conflicts are real cross-voltage mismatches caused by the first correction, not missing 3D wires. Source 3D wires remained 1,184.

**Correction:** do not change track `CKEY` if the strong POA+source votes point to a circuit with a known voltage different from the confidently derived source DXF voltage. Retain original circuit and mark `LAYER_RULE=POA_SOURCE_VOLTAGE_CONFLICT` and `CONF=REVIEW`, recording both voltage evidence and the candidate. This avoids a false increase in source matching at the cost of incorrect 3D wire attribution.

**Important:** The fundamental disagreement between source structure positions/POA votes and source DXF conductor voltage is **not resolved**. We must not claim that 379/A152/N186 source matching is repaired merely by blocking the reattribution. Test and inspect physical-track ownership and reference alignment with original wire geometry before merge.
