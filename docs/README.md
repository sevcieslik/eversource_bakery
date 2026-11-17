# Eversource Tester R01.04

First release. CT_17 and CT_33 deliverables were reviewed and accepted by the project operator.

## Purpose

Eversource Tester builds selected NMIP26068 Eversource geometry and point-cloud deliverables. The application version must display **R01.04** at launch and during the Geometry stage.

## Operator documents

Open `docs/operator_manual.html` in your browser for the illustrated, step-by-step workflow. It works offline. Use **Operator Manual** in the GUI to open it offline.

## Folder layout

- `input/`: default run input; the GUI can use a different input path
- `source/`: packaged project references, including SoW SQLite; source directory is fixed in GUI
- `output/`: default results folder; use a separate output path per test
- `scripts/`: implementation and installed Python environment; operators should not edit
- `setup_eversource.bat`: first-time installation
- `run_eversource.bat`: launch GUI

## Run

1. Run `setup_eversource.bat` once, then `run_eversource.bat`.
2. Confirm Input Folder, Output Folder, CRS, capture year, workers and product selection.
3. Select **Geometry** for circuit 2D wires, structures and Caydence outputs.
4. Click **Start Batch** and wait for preflight.
5. Select READY RoWs and click **Continue Batch**.
6. When the run completes without error, use **Open Deliverables** and **Open QA**.
7. Perform QGIS QA before delivery, especially circuit continuity, POA structures, DCWP location and terminal 2D coverage.

## Expected behaviour

- Circuit attribution prioritises conductor continuity and available reference evidence.
- Distinct nearby circuit POAs are not merged on distance alone.
- Observed POA structures remain even without matching source data; such records use NULL source-mapped fields.
- For confirmed shared structures, create circuit-specific records at a shared physical location.
- For DCWP, use the confirmed middle member; source XY may serve as a candidate fallback requiring review.
- 3D conductors extending into substations can form terminal 2D without an end POA or a made-up structure.
- A substation boundary does not justify deleting a real POA/source structure.

## QA and support

Save the console version, affected RoW/circuit, relevant DXF/SHP examples, QA outputs and last error lines. Do not assume deliverables are complete when a final Geometry/QA export error is present.

## In-app help

Click **Operator Manual** at the bottom of the left panel. It opens `docs/operator_manual.html` using the system default browser and works offline.

## R01.04 attribution and conductor types

- EARTH / PHASE markings on DXF layers take precedence over analytical bundle inference, including at terminal spans.
- An unmatched Structure may inherit attributes from an unused source Structure on the same circuit, in longitudinal order between already matched controls. The POA-derived point never moves.
- The corridor check uses a lateral width, not a circular source-matching radius. Ambiguous and out-of-corridor cases retain NULL source attributes and are listed in `01_structure_corridor_attribution.csv`.
- Operator Manual is located at the bottom of the left navigation panel.

**DXF layer caveat:** `Mainline_345kV` identifies voltage, not PHASE/EARTH. R01.04 uses source LiDAR classes 187/210 for this determination when sufficient evidence is available. Existing continuity inference is a fallback only. The independent evidence report is `01_caydence_lidar_wire_roles.csv`.

## R01.04 LiDAR-based Caydence wire types

In Geometry the same source LAS/LAZ read used to sample structure ground height also collects Tx Conductor class **187** and Tx Shield class **210** around each original DXF 3D wire. Sufficient, consistent 3D evidence determines PHASE/EARTH. This does **not** read or depend upon the exported PLS XYZ products. Review `01_caydence_lidar_wire_roles.csv`: `INSUFFICIENT_LIDAR` means no confirmed LiDAR assignment, so the prior role is retained pending review.

## R01.04 SCHF representative position

SCHF structures with three unambiguous parallel PHASE wires are positioned transversely on the middle wire. Their circuit station is preserved, source attributes are not modified, and ground height is resampled at the final XY. Ambiguous arrangements are left unchanged and reported in `01_schf_middle_phase_qa.csv`. Other structure types are unaffected.

## R01.04 SCSH single-top positioning

SCSH structures are moved transversely to the middle of three same-circuit PHASE wires only when exactly one distinct raw Tower Top Attachment marker is present locally (15 m search radius). Where multiple raw POA markers occur, the point is not moved and QA reports `MULTIPLE_NEARBY_TOWER_TOPS` (possible trident). Coincident CAD duplicates within 0.75 m are treated as one top. Other fallback and SCHF rules are unchanged. See `01_structure_middle_phase_qa.csv`.
