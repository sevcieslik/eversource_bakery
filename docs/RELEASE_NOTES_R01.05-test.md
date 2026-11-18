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
