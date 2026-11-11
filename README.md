# Eversource Tester R01.04

R01.04 is the current tester release. The initial R01.00 geometry deliverables for CT_17 and CT_33 were reviewed and accepted by the project operator.

## Purpose

Eversource Tester builds selected NMIP26068 Eversource geometry and point-cloud deliverables. The application version must display **R01.04** at launch and during the Geometry stage.

## Operator manual

Use **Operator Manual** at the bottom of the left GUI panel to open `docs/operator_manual.html` in the default browser. The illustrated manual works offline.

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

## Circuit and structure attribution

- Explicit EARTH / PHASE DXF layers take precedence over inferred wire types, including at terminal spans.
- An unmatched Structure may inherit attributes from an unused source Structure on the same circuit, in longitudinal order between already matched controls. The POA-derived point never moves.
- The corridor check uses a lateral width, not a circular source-matching radius. Ambiguous and out-of-corridor cases retain NULL source attributes and are listed in `01_structure_corridor_attribution.csv`.
- Operator Manual is located at the bottom of the left navigation panel.

**DXF layer caveat:** `Mainline_345kV` identifies voltage, not PHASE/EARTH. R01.04 uses source LiDAR classes 187/210 for this determination when sufficient evidence is available. Existing continuity inference is a fallback only. The independent evidence report is `01_caydence_lidar_wire_roles.csv`.

## LiDAR-based Caydence wire types

In Geometry the same source LAS/LAZ read used to sample structure ground height also collects Tx Conductor class **187** and Tx Shield class **210** around each original DXF 3D wire. Sufficient, consistent 3D evidence determines PHASE/EARTH. This does **not** read or depend upon the exported PLS XYZ products. Review `01_caydence_lidar_wire_roles.csv`: `INSUFFICIENT_LIDAR` means no confirmed LiDAR assignment, so the prior role is retained pending review.

## SCHF representative position

SCHF structures with three unambiguous parallel PHASE wires are positioned transversely on the middle wire. Their circuit station is preserved, source attributes are not modified, and ground height is resampled at the final XY. Ambiguous arrangements are left unchanged and reported in `01_schf_middle_phase_qa.csv`. Other structure types are unaffected.

## SCSH single-top positioning

SCSH structures are moved transversely to the middle of three same-circuit PHASE wires only when exactly one distinct raw Tower Top Attachment marker is present locally (15 m search radius). Where multiple raw POA markers occur, the point is not moved and QA reports `MULTIPLE_NEARBY_TOWER_TOPS` (possible trident). Coincident CAD duplicates within 0.75 m are treated as one top. Other fallback and SCHF rules are unchanged. See `01_structure_middle_phase_qa.csv`.


---

## Release notes

This section records changes introduced after the R01.00 baseline. Most recent first.

### R01.04

- Extends the R01.03 transverse middle-PHASE positioning rule to SCSH structures.
- SCSH requires exactly one distinct original Tower Top Attachment point of the same circuit in a 15 m neighborhood; multiple markers (potential trident) block relocation.
- Original CAD point duplicates within 0.75 m count as a single top. Missing raw POA, wrong circuit, ambiguous PHASE geometry and overly distant POA are held for QA.
- SCHF geometry logic remains unchanged. Source attribution, CIRCUIT, and all 2D/3D wires remain unchanged.
- Ground is resampled at any moved XY.
- QA output: `01_structure_middle_phase_qa.csv` (includes STR_TYPE and NEARBY_TOPS).
- Run regression `python scripts/test_schf_scsh_r0104.py`. Full production LiDAR rerun not performed.

### R01.03

- Representative structure point for `STR_TYPE=SCHF` is moved transversely to the middle of three local parallel PHASE wires, without longitudinal displacement.
- Uses LiDAR-classified Caydence PHASE wires (187), not unrelated output products.
- Requires exactly three local wires with consistent tangents and spacing; otherwise leaves the structure unchanged with QA warning.
- Resamples ground class 2 at every moved structure before delivery.
- Does not change the 3D wires, 2D wires, circuit attribution, source attributes or other structure types.
- New QA: `01_schf_middle_phase_qa.csv`.
- Validation: syntax and standalone geometry cases; full Geometry LiDAR rerun required.

### R01.02

- Geometry reads classified source LiDAR class 187 (PHASE) and 210 (EARTH) **during the existing ground-elevation scan**, not from PLS XYZ products.
- DXF explicit EARTH/PHASE levels remain authoritative; generic levels use full 3D nearest sample voting with >=12 LiDAR matches, >=4 distinct sampled locations and >=80% majority.
- Raw source wire geometry, circuit assignment, 2D products and structure geometry are unchanged by this classification pass.
- New `01_caydence_lidar_wire_roles.csv` records the evidence and decisions per wire; unconfirmed roles are explicitly identified.
- This release requires UAT on source LAZ tiles: no complete pipeline run was possible in the authoring environment.

### R01.01

Patch release on top of accepted R01.00 geometry.

- Preserve explicit DXF EARTH/PHASE throughout Caydence export and terminal recovery. For generic Mainline DXF layers, conservatively propagate a stable EARTH classification along physically continuous XYZ wire chains (QA CSV provided).
- Second-pass source attribution by circuit corridor and order between matched source controls; supports disconnected reference lines and preserves POA XY. Real EMA 3361 regression recovers 13~111 with unchanged geometry.
- Unmatched or ambiguous candidates remain dummy with QA warnings.
- Operator Manual moved to bottom-left of GUI; unnecessary final manual section removed.

QA report: `01_structure_corridor_attribution.csv`. Full production run is required before release acceptance.