# Eversource Tester R01.05-test – geometry attribution fixes

Test build for the NMIP26028 Bakery Geometry feedback (Clusters 12, 17, 39, 55).
Before/after on Cluster 12: `R01.05-test_Cluster12_before_after.png`.
Built on R01.04. Only the 01 Geometry stage changes, plus a new re-export from the edited QA GPKG (F). LiDAR, PLS and points stages are untouched.

## Install

1. Make a copy of an existing `Eversource_Tester_R01.04` folder (keep `.venv`, `source`, `input`).
2. Copy these files from `scripts/` over the copy: `eversource_pipeline_core.py`, `bakery_version.py`, `04_export_geometry_from_gpkg.py`, `run_eversource_pipeline.py`, `tx_veg_bakery_gui.py`.
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

**E. RoW boundaries fall on Structures (update 2026-11-18)**
- **Spans:** each span keeps the RoW of the SoW circuit line it follows; the switch between RoWs happens on a Structure. A span is delivered in both RoWs only where both RoWs' SoW lines run along it (start/end overlap, mean distance ≤ 15 m).
- **Structures:** a Structure is delivered in every RoW whose spans end on it. The last Structure of one RoW is also the first of the next (`ROW_RULE = BOUNDARY_SPAN_END` in `structures_final`). Extra Structures are deliberate: removing one is easier than adding a missing one.
- **Caydence wires:** a Caydence 3D wire takes the RoW of the span it belongs to (`ROW_RULE = SPAN`). All conductors of a span now sit in one RoW and end on the same Structures. Wires outside delivered spans take the RoW of the nearest Structure of the same circuit (`STRUCTURE`).
- **QA:** every move and duplicate is listed in `01_geometry/01_row_boundary_qa.csv`.

Feedback addressed: X116 at NH_05/NH_25 (DXF_W004321/W004323 split between RoWs) and S188 at NH_05/NH_11 (DXF_W007247 in NH_05). The same fix covered Z119 (NH_05/NH_25) and H123/K165 (NH_05/NH_09).

**F. Export from an edited QA GPKG (new)**
1. Run Geometry.
2. Open `output/01_geometry/01_eversource_geometry.gpkg` in QGIS and edit:

   | Layer | What to edit |
   |---|---|
   | `selected_span_wires` | 2D spans: `ROW` / `CIRCUIT`; delete a feature to drop a span. One feature per span and RoW. |
   | `recovered_terminal_2d` | 2D conductors into substations: `CIRCUIT`. Their RoW follows the nearest span of that circuit. |
   | `structures_final` | Circuit and Caydence Structures: `ROW` / `CIRCUIT`; delete or copy features. |
   | `caydence_wires_3d` | Caydence 3D wires: `ROW` / `CIRCUIT` / `WIRE_TYPE`. |

   `ROW` may hold several RoWs, e.g. `NH_05;NH_25`, to deliver a feature in both. Caydence 3D Structures are always rebuilt from `structures_final`.
3. Save the edits and run **Re-export from GPKG**: a GUI button, launcher product 6, or `python scripts/04_export_geometry_from_gpkg.py --output-root <output>`.
   - Only `deliverables/<RoW>/Circuit` and `deliverables/<RoW>/Caydence` are rebuilt.
   - The previous folders are first copied to `QA/<time>_01_geometry_export_from_gpkg/deliverables_before`.
   - Tile Index, Management Units and LAS are not touched.
4. Check `01_gpkg_export_checks.csv` in the same QA folder. It flags:
   - span ends without a Structure of that circuit in the same RoW,
   - spans, Structures or Caydence wires placed in a RoW whose SoW does not carry the circuit.

Re-exporting an unedited GPKG gives identical deliverables: Cluster 12, 78/78 files, same feature counts, lengths within 0.1 m. The GPKG now also holds an `export_settings` table (year, selected RoWs, units).

**G. GUI layout fix (update 2026-11-18)**
- **Right column:** keeps its full width (it was being cut, already in R01.04).
- **Display scaling:** the side columns and the start-up window size follow Windows display scaling (125%/150%).
- **Narrow windows:** Workspace Setup and Product Selection stack when they do not fit side by side.
- **Small windows:** the work area scrolls instead of cutting off the log and buttons.
- **Buttons:** arranged in two columns. Labels: Deliverables / QA / Re-export from GPKG.

**H. Preflight cache (update 2026-11-18)**
Only the lasboundary footprint was cached before; everything else was recalculated on every run. Now three more steps are cached under `00_preflight/00_cache`:
- the LiDAR tile index (a LiDAR ZIP is no longer extracted just to read headers),
- the SoW + Management Units read,
- the RoW/AOI/LiDAR coverage table.

Each cache is keyed by a cheap fingerprint of its inputs (file names, sizes and modification times; for a ZIP, its directory) and of the code that builds it. Any change to the LiDAR, AOI files, SoW, MU source or the tool recalculates only the affected steps; adding an AOI recalculates only the coverage table. On Cluster 12 an unchanged preflight drops from 2.6 s to 0.2 s, excluding LiDAR; with full LiDAR the saving is larger.

To force a rebuild: run `00_preflight_eversource.py --no-cache`, set `BAKERY_NO_PREFLIGHT_CACHE=1`, or delete `00_preflight/00_cache`.

**I. Faster LiDAR sampling in Geometry, with progress (update 2026-11-18)**
The step "Sampling Structure ground and 3D wire classes 187/210 from source LiDAR" (unchanged since R01.04) ran in one thread with no progress output.

The work is unchanged: it reads every LAZ tile with structures or conductors, takes class-2 ground at each Structure, and counts class 187/210 points along each observed 3D conductor. What changed:
- **Progress:** tile headers and sampling show a progress bar with file count, elapsed time and ETA. The GUI also shows it in the Mixing stage.
- **Parallel:** tiles are processed in parallel worker processes (`--workers`, the GUI Workers value, or `BAKERY_WORKERS`; default up to 8).
- **Fewer tiles:** tiles with no Structure and no conductor are skipped. Before, every tile inside the bounding box of all wires was read.
- **Smaller KD-trees:** only ground points in grid cells next to a Structure go into the KD-tree. Before, all ground points of every 2-million-point chunk did; on dense tiles this alone halved the time.
- **Less decompression:** only XYZ and classification are decompressed where the LAS format allows (LAS 1.4, laspy ≥ 2.4).
- **ZIP progress:** a LiDAR ZIP shows unzip progress, and only LAS/LAZ members are extracted.

Results are identical to R01.04 on synthetic Cluster 12 LiDAR (191 LAZ tiles): ground Z for 1,734 Structures, class 187/210 votes and wire roles for 8,044 wires. This holds with 1 and with several workers.

## Cluster 12 test results (NH_05/09/11/14/25, same inputs)

| Check | R01.04 | R01.05-test |
|---|---:|---:|
| Circuit folders | 45 | 34 |
| Circuit folders with < 50 % of wire inside the RoW | 15 | 0 |
| 2D wire length written into another RoW's folder | 206.8 km | 0 km |
| Circuit length discarded as disconnected | 18.4 km | 0 km |
| Total delivered 2D circuit length | 214.4 km | 233.2 km |
| Recovered terminal POAs | 173 | 6 |
| Structure pairs closer than 3 m | 39 | 5 shared towers + 5 deliberate boundary Structures in the neighbouring RoW (E) |
| Structures in a RoW without their circuit (SoW) | 26 | 0 |
| Caydence wires in a RoW without their circuit | 67 | 0 |
| Source structure reused within a circuit and RoW | 33 | 1 |
| Caydence wires moved to their span's RoW (E) | – | 18 |

Recovered circuit length:
- 326: +7.7 km
- Z119: +8.4 km (the NH_25 section is now delivered)
- K165: +2.8 km

Folders removed as wrong: NH_05 I158 and W144; NH_09 326 and 380; NH_11 3124, 326 and 380; NH_14 3124, 326, 380 and T120.

The R01.03/R01.04 SCHF/SCSH regression tests still pass. New: `scripts/test_row_boundary_r0105.py` (RoW boundaries and GPKG re-export).
The baseline run reproduced the operator's R01.04 Cluster 12 output exactly: 173 terminal POAs, 38 terminal 2D, 62 corridor matches.

## Known limits / please check

- **Shared structures:** the 5 remaining close pairs are one physical structure carrying two circuits (e.g. K165/T77, K165/O149, G192/T33). Each circuit still gets its own record. We need a decision on whether Caydence RoW Structures should hold one record per physical structure.
- **MAIN_PART quality:** these parts are delivered with the circuit attribution they already had. Please QA them first. They are listed in `selected_span_wires` with `PATH_ROLE = MAIN_PART`.
- **Attribution not yet changed:** circuit attribution on shared centrelines is unchanged (e.g. 3195/380, H123/K165, CONF = REVIEW). That is the next step.
- **No LiDAR in our test run:** ground Z and LiDAR wire roles in our run are not meaningful. They do not affect the 2D/attribution changes above.

## J. Parallel-line supports split (feedback item 10)
POAs within 15 m used to be merged into one structure in the middle, even when they belonged to two separate parallel lines. A merged group is now split when at least two "through" POAs (wires attached within 3 m continuing in opposite directions) lead to different neighbouring structures. Split parts get new ST IDs appended at the end, so existing IDs do not change. QA: `01_support_split_qa.csv`.

## K. Terminal spans at substations (feedback item 8)
Recovery of terminal 2D spans from observed 3D now accepts spans that turn up to ~110° from the line direction, e.g. into a substation bay. It still requires the new tip to move at least half a span away from the original end. The new tips found in the test clusters all lie inside SoW substations.

## L. Re-export RoW selection
GPKG re-export now writes only the RoWs that were selected: the GUI shows a checkbox dialog, and `04_export_geometry_from_gpkg.py` takes `--rows NH_02,NH_27` and `--list-rows`. Features in other RoWs, for example boundary structures duplicated from a neighbouring RoW, are skipped and reported as CHECK `ROW_NOT_EXPORTED`.

## M. Corridor attribution: single pending POA (feedback item 14)
When a dummy POA is the only unmatched POA between two known anchors on a corridor, it now gets the nearest source structure even when the candidates are close in distance. It is marked SRC_MATCH `CORRIDOR_AMBIGUOUS_NEAREST` and QA status `MATCHED_AMBIGUOUS_NEAREST` so it can be reviewed.

## N. 2D representative: outer lane for non-horizontal bundles
For vertical, mixed and line-post bundles, the 2D wire is now the outermost conductor on one side of the circuit, kept along the whole path. Previously only continuity decided, so the representative could jump between conductors, especially at corners.
- The side is defined relative to the circuit's direction of travel, so it stays the same across corners.
- The side away from neighbouring circuits (within 40 m) is used. Without neighbours, the cheaper side is used.
- Horizontal bundles keep the central phase, unchanged.
- At transitions between horizontal and non-horizontal spans, the wire steps from the central phase to the outer one at the structure.
- New `selected_span_wires` fields: `LANE` (CENTRAL / OUTER_LEFT / OUTER_RIGHT), `LANE_OK`, `LANE_WHY` and `SIDE_CLASS`. `SELECT_METHOD` is `VERTICAL_OUTER_LANE`.
- `BAKERY_LANE_RULE=0` restores the R01.04 behaviour.

Test clusters:

| Metric | C12 R01.04 | C12 new | C17 R01.04 | C17 new |
|---|---|---|---|---|
| Side switches in non-horizontal runs | 59 | 0 | 13 | 0 |
| Joins > 3 m (all at horizontal↔non-horizontal transitions) | 7 | 72 | 0 | 11 |
| Maximum join | 4.9 m | 10.3 m | 0.9 m | 8.0 m |

## O. Outer lane also on horizontal spans (circuits with mixed spans)
If a circuit has any non-horizontal span with laterally spread phases, all of its spans use the outer lane, including horizontal ones. Circuits that are horizontal on their whole length keep the central phase. This removes the step from the central phase to the outer phase at transitions.
- C12: joins > 3 m went from 72 (rule N) to 0. The maximum join went from 10.3 m to 2.1 m.
- C39: joins > 3 m went from 38 to 12. The remaining ones are same-side joins at multi-pole structures.

**SCHF/SCSH on outer-lane circuits.** These structures are no longer moved to the middle phase. They stay on the circuit's 2D wire (Structure-on-Wire). `01_structure_middle_phase_qa.csv` gives `STATUS = UNCHANGED`, `REASON = OUTER_LANE_CIRCUIT`. In practice most circuits have at least one mixed span, so the R01.04 middle-phase rule now applies only to circuits that are horizontal end to end (C12: 152 moved, 823 kept on the outer wire). **Decision (18/11/2026): SCHF/SCSH on outer-lane circuits stay on the 2D wire.** (Superseded 19/11 by S: the 2D wire is central again, and SCHF goes to the middle phase.) The middle-phase rule applies only to circuits that are horizontal end to end.

## P. Caydence RoW Structures: one record per physical structure
A support that carries several circuits produced one Caydence record per circuit. Now only one record is kept per RoW, the one with the fullest attribution: the most filled source attributes, then a real (non-dummy) source match, then the smallest source distance.
- Same support means the same `TOPO_ID`, or markers within 0.5 m in the same RoW (e.g. a terminal POA of one circuit on another circuit's pole).
- Circuit folders still have one Structure per circuit.
- QA: `01_caydence_structure_dedupe_qa.csv`.
- Records removed: C12 6, C39 100, C55 37, C17 0.

## Assumptions
- **Structures on RoW boundaries are duplicated into each RoW.** A structure at the start or end of two RoWs (or circuits) appears in both RoW deliverables, by design (rule E). It is easier for QA to delete an extra structure than to add a missing one. Within one RoW, Caydence keeps one record per physical structure (P).
- **The 2D wire is a representative conductor, not a centreline.** Horizontal circuits use the central phase. Other circuits use the outermost phase on the side away from neighbouring circuits, kept along the whole circuit.
- **Units.** All distance thresholds are in metres and are converted with the CRS unit factor. EPSG 6525, 6434 and 6492 use US survey feet: 1 m = 3.2808 ft.

## Q. Caydence wires split at crossings (C17 NH_02 × NH_27)
The DXF can split a conductor where it crosses another line. If a piece does not belong to any analytical bundle, it used to take the circuit of the nearest Structure within 15 m. That is often a pole of the line being crossed: in C17, two 345 kV pieces of 391 (NH_02) ended up as Y170 in NH_27.

Two changes:
- **Parallel-bundle attribution (`ATTR_METHOD = PARALLEL_BUNDLE`).** A non-bundled wire first takes the circuit of the bundled conductors it runs alongside: within 10° of their direction and within 12 m along its whole length.
- **Voltage guard.** When the DXF layer names a voltage (e.g. `Mainline_345kV`), a parallel-bundle or Structure-anchor match to a circuit with a different voltage is rejected.

Wires whose layer voltage differs from their circuit's voltage:

| Cluster | Before | After |
|---|---|---|
| C17 | 2 | 0 |
| C39 | 7 | 4 |
| C55 | 9 | 5 |
| C12 | 0 | 0 |

The remaining cases come from the main track/bundle attribution (shared-corridor issue, still open).

## R. DXF layer names as attribution evidence (voltage, line tag, circuit name)
The 3D wire layer name is parsed. Examples:

| Layer | Read as |
|---|---|
| `Mainline_345kV` | voltage 345 |
| `Mainline_1_115kV` | voltage 115, line tag 1 |
| `Mainline_391_345kV` | circuit 391 (if 391 is a known circuit), voltage 345 |

The parsed values are used in two places:
- **Bundling.** When all wires on a support pair carry a layer line identity and there are at least 2 identities of at least 3 wires each, each line is bundled separately. Wires of a 115 kV and a 345 kV line, or of `_1` and `_2`, are no longer mixed by lateral order.
- **Track → circuit attribution.**
  - A circuit named in the layer wins.
  - Otherwise, candidates whose SoW/client voltage differs from the layer voltage go behind every same-voltage candidate. If only a different-voltage candidate exists, the track keeps it with `CONF = REVIEW` and `LAYER_RULE = KV_MISMATCH`.
  - `track_assignment_review` has the new fields `LAYER_KV` and `LAYER_RULE`.

Results: Caydence wires whose layer kV differs from their circuit kV are now 0 in all four test clusters (before: C39 4, C55 5). C39 also lost its 2D gaps on 364 and 371: no discarded parts, 2D +0.6 km.

Layer naming recommendation for operators:
- Keep the voltage in every layer name.
- Add `_1`, `_2`, … when two lines of the same voltage cross or share a corridor.
- Best of all, put the circuit name in the layer, e.g. `Mainline_391_345kV`.

Crossings between circuits of **different** voltages are usually real. Only same-voltage crossings point to attribution problems: C12 12, C39 2, C17 0, C55 0.

## S. 2D back to the centre of the circuit (19/11 feedback item 16)
The outer-lane rule (N, O) is no longer the default. The 2D wire is again the **central phase**, for every arrangement.
- Horizontal: strong preference for the central phase, as in R01.04.
- Vertical or mixed: preference for the conductor nearest the bundle centre, with continuity deciding between near-central conductors.
- SCHF/SCSH structures go to the middle phase again, consistent with the 2D wire, so the "stay on the outer wire" decision under O no longer applies.
- `BAKERY_2D_LANE=outer` restores the 20261118f–i behaviour. `BAKERY_2D_LANE=legacy` (or `BAKERY_LANE_RULE=0`) restores R01.04.
- C17 circuit 391, structures 8–10: with the outer rule, spans 8–9 and 9–10 ran on the outermost conductor, which on 9–10 was an earth wire, with 3.6–3.8 m dog legs at 8 and 10. Now it is the central phase with 0.0 m joins.

## T. Earth wires never represent the circuit in 2D
Any wire classified as EARTH in any bundle is removed from the 2D candidates when phase wires remain.

## U. Duplicate conductors in the DXF
Identical copies of one conductor (both 3D ends within 0.3 m) used to be sliced into different "phases". The result was a central phase offset by one phase spacing (C39 364 at ST00151–ST00153: 7.9 m dog leg).
- Copies are collapsed for 2D analysis.
- In Caydence they keep the same circuit and type as the kept wire.
- Copies found: C39 13, C12 4.

## V. Parallel bundles of the same circuit on one support pair
When one support pair holds more than one bundle of the same circuit, continuity now chooses between them, rather than distance to the SoW centreline.

## W. Circuit-named levels (MicroStation), end to end (item 17)
With levels like `Mainline_1250_115kV`:
- **Bundling** groups wires per named level. Wires on unnamed levels (e.g. `0`, up to 20%) join the nearest named group.
- **Physical tracks** are never joined across different named levels.
- **Track → circuit** uses the level circuit (`LAYER_RULE = LAYER_CIRCUIT`).
- **Caydence wires** take the level circuit directly (`ATTR_METHOD = LAYER_CIRCUIT`).

Synthetic C39 test (all wires renamed to their circuit, 3% left on level `0`):
- 2731/2731 named wires got the level circuit in Caydence.
- 1 of 606 2D spans runs on another circuit's level.
- 15/15 tracks were attributed by level.

Joins over 3 m that remain (C39: 14) are mostly at horizontal ↔ vertical configuration changes, where the middle of the circuit really moves on the structure.

## X. Build identity: which code did the operator run?
Every package now carries `scripts/BUILD_INFO.json` with the build name, git commit, branch and build time. The label, e.g. `R01.05-test build 20261119b (commit 1a2b3c4)`, appears in:
- the GUI window title and header,
- the run log: the banner and a `[BAKERY] …` line at the start of every stage (00, 01, 02, 04),
- every `QA_RUN_MANIFEST.json`, under `"bakery"` (full commit hash),
- the `export_settings` layer of the QA GeoPackage.

When run from a git checkout without `BUILD_INFO.json`, the commit is read from git, with `-dirty` added if there are local changes.

## Y. Caydence Structures: one record per circuit again (20/11)
Team feedback on C39 CT_06: on supports shared by 1500 and 1605, the per-structure rule (P) kept only the 1500 record, so 1605 dropped from 88 to 14 Caydence structures. Turning 1500 off in QGIS then showed no structures along that line.
- Caydence keeps one record **per circuit** again, as in R01.04. Only duplicates of the same circuit at the same support are removed.
- C39 CT_06 Caydence Structures now match the 13/11 R01.04 run: 1500 103, 1605 88. Records of other RoWs (310/348/383) no longer leak into CT_06.
- `BAKERY_CAYD_DEDUPE=support` restores rule P.

## Z. Taps recovered in 2D from 3D (20/11)
Terminal recovery (K) only extends the ends of a circuit path. A tap that leaves the line part-way, e.g. into a substation, had no 2D.
- How it works: the circuit's 3D wires (not EARTH) that lie away from its 2D are grouped into spans, and each span is represented by its central conductor. Spans are chained outward from the 2D.
- A chain is added as an extra part of the circuit 2D only if its first span has at least 2 wires and it reaches at least 25 m away from the line.
- QA: `01_tap_2d_recovery.csv`, with actions `RECOVERED_3D_TAP`, `REJECTED_FEW_WIRES`, `REJECTED_RUNS_ALONGSIDE` and `NOT_CONNECTED_TO_2D`.

Recovered taps:

| Cluster | Circuit | Tap |
|---|---|---|
| C12 | 3195 | 2 spans, 168 m + 65 m |
| C12 | R187 | 47 m |
| C39 | 1250 | 84 m |
| C55 | 107 | 76 m |
| C55 | 399 | 69 m |

Lone or alongside wires were rejected and listed for review.
