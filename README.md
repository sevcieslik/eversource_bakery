# Eversource Tester R01.05-test

**R01.05-test** is the current **test build** for operator UAT. **R01.04** remains the last accepted release. The initial R01.00 geometry deliverables for CT_17 and CT_33 were reviewed and accepted by the project operator.

Detailed changes and test results: [`docs/RELEASE_NOTES_R01.05-test.md`](docs/RELEASE_NOTES_R01.05-test.md).

## Purpose

Eversource Tester builds selected NMIP26068 Eversource geometry and point-cloud deliverables.

## Which build am I running?

Each package carries its build name and git commit, shown as a label like:

```
R01.05-test build 20261119b (commit 6807c1e)
```

The label appears in:
- the GUI window title and header,
- the run log (banner, and a `[BAKERY] …` line at the start of every stage),
- every `QA_RUN_MANIFEST.json`, under `"bakery"` with the full commit hash,
- the `export_settings` layer of the QA GeoPackage.

Always quote the label when reporting results. The value comes from `scripts/BUILD_INFO.json`, which is written when the package is built. When the tool runs from a git checkout instead, the commit is read from git, with `-dirty` added if there are local changes.

## Operator manual

Use **Operator Manual** at the bottom of the left GUI panel to open `docs/operator_manual.html` in the default browser. The illustrated manual works offline.

## Folder layout

- `input/`: default run input; the GUI can use a different input path.
- `source/`: packaged project references, including SoW SQLite; the source directory is fixed in the GUI.
- `output/`: default results folder. Use a **new, empty output folder for each cluster** (see below).
- `scripts/`: implementation and installed Python environment; operators should not edit it.
- `setup_eversource.bat`: first-time installation.
- `run_eversource.bat`: launches the GUI.

## Run

1. Run `setup_eversource.bat` once, then `run_eversource.bat`.
2. Check the build label in the GUI title.
3. Confirm Input Folder, Output Folder, CRS, capture year, workers and product selection.
4. Select **Geometry** for circuit 2D wires, structures and Caydence outputs.
5. Click **Start Batch** and wait for preflight. Preflight results are cached and reused while the inputs do not change. Use `--no-cache` or `BAKERY_NO_PREFLIGHT_CACHE=1` to force a fresh preflight.
6. Select READY RoWs and click **Continue Batch**. LiDAR sampling runs on the selected number of workers and logs its progress.
7. When the run completes without error, use **Open Deliverables** and **Open QA**.
8. Perform QGIS QA before delivery, especially circuit continuity, POA structures, DCWP location and terminal 2D coverage.

**One output folder per cluster.** The tool does not clear old results. RoW folders from a previous cluster stay in the output folder and can be delivered by mistake. If two clusters share a RoW, the second run overwrites the first one's deliverables for that RoW.

## Deliverables layout

Deliverables are organised by client output product, so that after QA each product folder can be copied straight into the output products:

```
deliverables/
  29_tile_index/<RoW>/<RoW>_Tile_Index_2026.shp                  area
  45_structure_shapefile/<Circuit>/<Circuit>_Structure_2026.shp  circuit
  68_standalone_gis/<RoW>/<RoW>_Management_Unit_2026.shp         area
  80_powerline_vectors/<Circuit>/<Circuit>_Wires_2026.shp        circuit
  267_caydence_conductors/<RoW>/<RoW>_3D_Wires_2026.shp          area
  267_caydence_conductors/<RoW>/<RoW>_3D_Structures_2026.shp     area
```

- **Circuits in two RoWs.** A circuit that runs through two RoWs has one file, with one 2D feature per RoW (field `ROW`). A structure on the RoW boundary appears once, with `ROW = CT_06;CT_16`.
- **Shared supports in 267.** Caydence keeps one structure per physical support, and its `CIRCUIT` lists every circuit on it, e.g. `1500;1605`.
- **Copying several clusters.** A circuit that continues into another cluster gets a file from each cluster's run. Merge them, don't overwrite.
- **LiDAR and PLS** stay under `deliverables/<RoW>/LiDAR` and `PLS`.
- `BAKERY_DELIVERY_LAYOUT=row` restores the R01.04 tree (`<RoW>/Circuit/<Circuit>`, `<RoW>/Caydence`, ...).

## Fixing results in the QA GeoPackage

1. Edit `output/01_geometry/01_eversource_geometry.gpkg` in QGIS: spans, structures, Caydence wires and their `CIRCUIT`/`ROW`.
2. Press **Re-export from GPKG** in the GUI, choose the RoWs to write and confirm. Geometry is not run again, and the previous deliverables are backed up.
3. Features in other RoWs are skipped and reported as `ROW_NOT_EXPORTED`. The 45 and 80 folders of the affected circuits and the 267 folders of the chosen RoWs are rebuilt.

Command line equivalent:

```
python scripts/04_export_geometry_from_gpkg.py --output-root output --rows NH_02,NH_27
python scripts/04_export_geometry_from_gpkg.py --output-root output --list-rows
```

## Naming DXF levels (MicroStation)

The tool reads voltage, line tag and circuit from the 3D wire level name:

| Level | Meaning |
|---|---|
| `Mainline_345kV` | Voltage only. This is the minimum; always keep the voltage. |
| `Mainline_1_115kV`, `Mainline_2_115kV` | Separate lines of the same voltage that cross or share a corridor |
| `Mainline_391_345kV` | Best option: the circuit is taken from the level for bundling, tracks, 2D and Caydence |

Wires on an unnamed level (e.g. `0`) are tolerated as a small minority.

## Expected behaviour

- **2D circuit wire.** It represents the **central phase** of the circuit. An EARTH wire is never used: after the LiDAR wire roles are known, any span whose chosen conductor is LiDAR-confirmed EARTH is chosen again (`01_2d_lidar_earth_reselect.csv`). Short steps between spans (up to 6 m) become one bend at the structure, with no hooks. A step of about 4 m where horizontal phases change to a vertical arrangement can remain.
- **Forks.** A branch part no longer runs on top of the main line: it starts where it really diverges (`01_fork_overlap_trim.csv`).
- **Duplicate conductors.** Identical copies of one conductor in the DXF are ignored for 2D. In Caydence they follow the kept wire.
- **Parallel lines.** Distinct nearby circuit POAs are not merged on distance alone. POAs of parallel lines are split into separate structures (`01_support_split_qa.csv`).
- **RoW boundaries.** RoW boundaries fall on structures. A structure at the start or end of two RoWs is written to both RoW deliverables. This is intended, so don't delete these duplicates (`01_row_boundary_qa.csv`).
- **Caydence Structures (267).** One record per physical support; on a support shared by several circuits, `CIRCUIT` lists them all, e.g. `1500;1605` (`01_caydence_structure_dedupe_qa.csv`). The circuit product 45 keeps one structure per circuit.
- **Caydence wires.** They are published in the RoW of their circuit. A wire is never attributed to a circuit of a different voltage than its DXF level.
- **SCHF/SCSH.** These are positioned on the middle phase, as in R01.04.
- **POA structures.** Observed POA structures remain even without matching source data; such records use NULL source-mapped fields.
- **Shared structures.** For confirmed shared structures, circuit Structures are created at a shared physical location.
- **DCWP.** Use the confirmed middle member; source XY may serve as a candidate fallback requiring review.
- **Taps.** Taps (branches leaving the line part-way, e.g. into a substation) are added to the circuit 2D from the observed 3D wires (`01_tap_2d_recovery.csv`). A tap may start up to 30 m from its circuit's 2D (`CONNECT_M` in QA). A lone wire, or one running alongside the line, is not used. These are listed as `REJECTED_*` for review.
- **Substations.** 3D conductors extending into substations, including sharp turns into a bay, can form terminal 2D without an end POA or a made-up structure. A substation boundary does not justify deleting a real POA or source structure.

## QA and support

For each issue report:
- the **build label**,
- the **cluster**, **RoW** and **circuit**,
- the **span ID or nearest structure** (`STRUCT_ID` or `TOPO_ID`),
- the run's QA folder,
- the last error lines, if any.

Do not assume deliverables are complete when a final Geometry/QA export error is present.

## Circuit and structure attribution

- Explicit EARTH/PHASE DXF layers take precedence over inferred wire types, including at terminal spans.
- An unmatched Structure may inherit attributes from an unused source Structure on the same circuit, in longitudinal order between already matched controls. The POA-derived point never moves.
- When a POA is the only unmatched one between two matched structures, it takes the nearest source structure. These records are marked `SRC_MATCH = CORRIDOR_AMBIGUOUS_NEAREST` for review.
- The corridor check uses a lateral width, not a circular source-matching radius. Ambiguous and out-of-corridor cases keep NULL source attributes and are listed in `01_structure_corridor_attribution.csv`.

**DXF layer caveat:** `Mainline_345kV` identifies voltage, not PHASE/EARTH. The tool uses source LiDAR classes 187/210 for PHASE/EARTH when sufficient evidence is available; continuity inference is a fallback only. The evidence report is `01_caydence_lidar_wire_roles.csv`.

## LiDAR-based Caydence wire types

Geometry reads the source LAS/LAZ once, both for structure ground height and for the Tx Conductor class **187** and Tx Shield class **210** points around each original DXF 3D wire. Sufficient, consistent 3D evidence determines PHASE/EARTH. This does **not** read or depend on the exported PLS XYZ products.

Review `01_caydence_lidar_wire_roles.csv`. `INSUFFICIENT_LIDAR` means there was no confirmed LiDAR assignment, so the prior role is kept pending review.

## SCHF representative position

SCHF structures with three unambiguous parallel PHASE wires are positioned transversely on the middle wire. Their circuit station is preserved, source attributes are not modified, and ground height is resampled at the final XY. Ambiguous arrangements are left unchanged and reported in `01_structure_middle_phase_qa.csv`.

## SCSH single-top positioning

SCSH structures are moved transversely to the middle of three same-circuit PHASE wires only when exactly one distinct raw Tower Top Attachment marker is present locally (15 m search radius). Where several raw POA markers occur, the point is not moved and QA reports `MULTIPLE_NEARBY_TOWER_TOPS` (possible trident). Coincident CAD duplicates within 0.75 m count as one top.

---

## Release notes

Changes after the R01.00 baseline, most recent first.

### R01.05-test (test build)

Full details, measurements and per-cluster results: [`docs/RELEASE_NOTES_R01.05-test.md`](docs/RELEASE_NOTES_R01.05-test.md).

- **2D wire.** Back to the central phase; EARTH wires are excluded; duplicate DXF conductors are collapsed; when one circuit has several bundles on the same structure pair, continuity picks between them.
- **Structures.** Parallel-line POAs are split into separate structures. A single pending POA between two matched structures takes the nearest source structure.
- **Terminal spans.** Spans turning sharply into substations are recovered.
- **RoW handling.** RoW boundaries are set at structures. 2D, Circuit and Caydence products are written per RoW. Deliverables are organised by product (29/45/68/80/267). Caydence keeps one structure per physical support, with all circuits in `CIRCUIT`. Taps are recovered in 2D from 3D.
- **Caydence wires.** Wires use the RoW of their circuit. Pieces split at crossings are attributed to the line they run alongside.
- **DXF level names.** Voltage, line tag and circuit name are read from the level name and used in bundling, tracks, circuit attribution and Caydence.
- **Re-export.** Deliverables can be rebuilt from the edited QA GeoPackage (GUI button or `04_export_geometry_from_gpkg.py`), with RoW selection.
- **Performance.** Preflight cache; parallel, selective LiDAR sampling with progress; GUI layout fixes for high DPI.
- **Build identity.** The build label is shown in the GUI, the log, the QA manifests and the GPKG.
- **Tests.** `test_row_boundary_r0105.py`, `test_schf_scsh_r0104.py`, `test_schf_r0103.py`.

### R01.04

- Extends the R01.03 transverse middle-PHASE positioning rule to SCSH structures.
- SCSH requires exactly one distinct original Tower Top Attachment point of the same circuit in a 15 m neighbourhood; multiple markers (potential trident) block relocation.
- Original CAD point duplicates within 0.75 m count as a single top. Missing raw POA, wrong circuit, ambiguous PHASE geometry and overly distant POA are held for QA.
- SCHF geometry logic remains unchanged. Source attribution, CIRCUIT, and all 2D/3D wires remain unchanged.
- Ground is resampled at any moved XY.
- QA output: `01_structure_middle_phase_qa.csv` (includes STR_TYPE and NEARBY_TOPS).
- Regression test: `python scripts/test_schf_scsh_r0104.py`.

### R01.03

- The representative structure point for `STR_TYPE=SCHF` is moved transversely to the middle of three local parallel PHASE wires, without longitudinal displacement.
- Uses LiDAR-classified Caydence PHASE wires (187), not unrelated output products.
- Requires exactly three local wires with consistent tangents and spacing; otherwise the structure is left unchanged with a QA warning.
- Resamples ground class 2 at every moved structure before delivery.
- Does not change the 3D wires, 2D wires, circuit attribution, source attributes or other structure types.
- New QA: `01_schf_middle_phase_qa.csv`.

### R01.02

- Geometry reads classified source LiDAR class 187 (PHASE) and 210 (EARTH) **during the existing ground-elevation scan**, not from PLS XYZ products.
- DXF explicit EARTH/PHASE levels remain authoritative. Generic levels use full 3D nearest-sample voting, which needs ≥12 LiDAR matches, ≥4 distinct sampled locations and ≥80% majority.
- Raw source wire geometry, circuit assignment, 2D products and structure geometry are unchanged by this classification pass.
- New `01_caydence_lidar_wire_roles.csv` records the evidence and decision for each wire; unconfirmed roles are explicitly identified.

### R01.01

Patch release on top of the accepted R01.00 geometry.

- Preserve explicit DXF EARTH/PHASE throughout Caydence export and terminal recovery. For generic Mainline DXF layers, conservatively propagate a stable EARTH classification along physically continuous XYZ wire chains (QA CSV provided).
- Second-pass source attribution by circuit corridor and order between matched source controls; supports disconnected reference lines and preserves POA XY.
- Unmatched or ambiguous candidates remain dummy, with QA warnings.
- Operator Manual moved to the bottom left of the GUI.

QA report: `01_structure_corridor_attribution.csv`.
