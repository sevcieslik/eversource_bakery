# Eversource Tester R01.05 – Release Notes

**Project:** NMIP26068 Eversource Tx Veg  
**Release:** R01.05  
**Previous release:** R01.04  
**Scope:** Geometry processing, circuit and RoW attribution, 2D/3D wire generation, Caydence exports, QA and GUI improvements.

## 1. Overview

R01.05 introduces substantial improvements to the Geometry processing workflow, focusing on reliable circuit attribution, conductor continuity, RoW boundaries and consistency between generated products.

The release addresses issues identified during operator reviews of Clusters 12, 17, 39 and 55, including missing conductors, incorrect circuit assignments, duplicated structures, discontinuous 2D representations and incorrectly assigned RoW deliverables.

The main objectives are to:

- Preserve all valid source 3D conductors in Caydence outputs.
- Improve circuit attribution using source geometry and DXF layer information.
- Generate continuous and representative 2D circuit wires.
- Assign geometry to appropriate RoWs without creating unnecessary duplicates.
- Improve handling of terminals, substations, taps and shared structures.
- Allow approved geometry corrections through GPKG editing and re-export.
- Improve processing performance, GUI usability and traceability.

The changes primarily affect **01 Geometry**. LiDAR and PLS XYZ processing algorithms remain outside the scope of this release.

## 2. Circuit and RoW Attribution

### 2.1 Circuit-based geometry assignment

Circuit attribution uses available geometric and source evidence, including:

- Original DXF layer names and explicit circuit identifiers.
- Voltage information embedded in DXF layers.
- SoW circuit and RoW geometries.
- Physical conductor bundles and parallel-wire relationships.
- Source structures and Tower Top Attachment Points.

Explicit circuit identifiers in DXF layer names take priority when they match a known circuit.

Voltage mismatches are identified during attribution rather than silently accepted.

For example:

| DXF layer | Interpretation |
|---|---|
| `Mainline_345kV` | 345 kV conductor |
| `Mainline_1_115kV` | 115 kV conductor, line identifier 1 |
| `Mainline_391_345kV` | Circuit 391, 345 kV |

Where the source information is insufficient or contradictory, the assignment is retained for operator review.

### 2.2 RoW-based delivery

Geometry is assigned according to the RoW containing the relevant circuit section.

A circuit crossing several RoWs retains its circuit identity, while individual spans carry the appropriate RoW attribution.

RoW transitions occur at physical structures wherever supported by the source topology.

Structures shared by adjacent RoWs may be intentionally represented in both RoW sections.

This prevents full circuit geometries from being unnecessarily duplicated into every RoW that contains part of the circuit.

### 2.3 Caydence wire attribution

Caydence 3D conductors follow their assigned circuit and corresponding spans.

Conductors belonging to the same physical span are kept together when determining RoW ownership.

Additional checks prevent conductors from being assigned to an unrelated circuit simply because another structure or centreline is geographically closer.

## 3. 2D Circuit Wire Generation

### 3.1 Central phase representation

The default 2D representation uses the **central phase conductor** rather than an artificial centreline.

This applies to horizontal, vertical and mixed conductor arrangements.

Selection considers conductor position, circuit continuity and available EARTH/PHASE evidence.

Conductors identified as EARTH are excluded from the 2D selection when valid PHASE conductors are available.

For unusual arrangements, the selected conductor remains subject to QA review.

### 3.2 LiDAR-assisted conductor verification

Where available, classified source LiDAR provides additional evidence for wire roles:

- Class 187: PHASE
- Class 210: EARTH

If the initial 2D selection uses a conductor subsequently identified as EARTH, the selection is recalculated using eligible PHASE conductors.

Dependent terminal and branch geometry is then regenerated from the corrected representation.

Results are recorded in `01_2d_lidar_earth_reselect.csv`.

### 3.3 Conductor continuity

R01.05 improves continuity between consecutive 2D spans.

The processing includes:

- Rejoining physically continuous conductors split in the source DXF.
- Avoiding unnecessary changes between conductor positions.
- Removing small hooks at span junctions.
- Aligning joins with confirmed structure nodes.
- Handling legitimate lateral transitions at structures.
- Preventing artificial connections between unrelated circuits.

Where a conductor changes its lateral position between spans, geometry may be connected diagonally to the structure rather than introducing an offset beside it.

Source 3D wire geometry is preserved.

### 3.4 Duplicate conductors

Duplicate source conductors with matching 3D endpoints are consolidated for 2D selection.

The original source wires remain available for Caydence delivery and retain consistent circuit and conductor classifications.

This prevents duplicate DXF entities from distorting the selection of the central phase.

## 4. Structures and Physical Supports

### 4.1 Structure positioning

The SCHF and SCSH middle-phase positioning rules introduced in R01.03 and R01.04 remain supported.

Structures are repositioned only when the conductor arrangement and source evidence satisfy the required geometric conditions.

Ambiguous configurations are retained for operator review.

Ground elevations are resampled where structure XY positions change.

The relevant output is:

`01_structure_middle_phase_qa.csv`

### 4.2 Shared supports

Multiple circuits may share one physical support.

For circuit-specific products, structure records retain the information required to represent each circuit.

For Caydence product 267, duplicate records for the same physical support are consolidated, with the associated circuit identifiers retained in the `CIRCUIT` attribute.

This prevents unnecessary structure duplication without losing information about circuits carried by the support.

### 4.3 Parallel lines

Nearby attachment points are not automatically treated as one physical support.

Where source wire continuity demonstrates distinct parallel lines, supports are separated rather than merged.

The relevant output is:

`01_support_split_qa.csv`

## 5. Terminal Wires, Taps and Substations

### 5.1 Terminal structure recovery

Terminal attachment points may be recovered when supported by original DXF and source structure evidence.

Recovery avoids creating duplicate structures where a valid support for the same circuit already exists nearby.

The recovered structure must be assigned to a RoW compatible with the circuit.

### 5.2 Substation terminals

A circuit may legitimately terminate inside a source substation boundary without a conventional Tower Top Attachment Point.

R01.05 improves the recovery of terminal 2D spans approaching substation connections, including geometry that changes direction relative to the main circuit alignment.

Substation boundaries are interpreted using the original SoW data.

No artificial attachment point should be introduced solely to complete a 2D line.

### 5.3 Tap recovery

Branches identified in the original 3D conductor geometry can be represented as additional 2D circuit features.

Recovery requires sufficient conductor evidence and a meaningful departure from the existing circuit alignment.

The process avoids adding unsupported straight-line connections between a tap and the main circuit.

Recovered taps are retained in the editable GPKG layer:

`recovered_tap_2d`

Supporting diagnostics are written to:

`01_tap_2d_recovery.csv`

### 5.4 Fork and branch cleanup

Overlapping branch sections are trimmed where appropriate.

Branch junctions are connected to confirmed structure nodes when supported by the existing geometry.

This reduces small hooks and unnecessary overlapping segments without deleting legitimate network branches.

## 6. Delivery Products and Output Structure

Geometry deliverables are organised by client product rather than exclusively by RoW.

| Product | Output folder | Organisation |
|---|---|---|
| 29 – Tile Index | `29_tile_index/<RoW>/` | Area / RoW |
| 45 – Structures | `45_structure_shapefile/<Circuit>/` | Circuit |
| 68 – Standalone GIS | `68_standalone_gis/<RoW>/` | Area / RoW |
| 80 – Powerline Vectors | `80_powerline_vectors/<Circuit>/` | Circuit |
| 267 – Caydence Conductors | `267_caydence_conductors/<RoW>/` | Area / RoW |

### 6.1 Product 80

Powerline vectors are exported as individual `LineString` features.

Main circuit sections, RoW sections and recovered branches are represented separately where appropriate.

Circuits crossing multiple RoWs remain within their circuit-specific delivery folder.

### 6.2 Product 267

Caydence conductors retain the original observed 3D geometry.

Circuit, RoW and conductor classifications are generated using the combined source and geometric attribution rules.

Caydence structures are consolidated by physical support, while preserving the associated circuit identifiers.

### 6.3 Source traceability

The processing retains source references and diagnostic attributes to support comparisons between original DXF entities, the Geometry GPKG and delivered products.

Uncertain assignments remain visible for QA rather than being silently discarded.

## 7. Editable Geometry GPKG and Re-export

R01.05 introduces the ability to regenerate Geometry deliverables from an operator-edited GPKG without repeating the complete Geometry processing stage.

The main editable file is:

`output/01_geometry/01_eversource_geometry.gpkg`

Relevant layers include:

| Layer | Purpose |
|---|---|
| `selected_span_wires` | Selected 2D spans, circuit and RoW attribution |
| `recovered_terminal_2d` | Recovered terminal conductors |
| `recovered_tap_2d` | Recovered branches and taps |
| `structures_final` | Structure geometry and attributes |
| `caydence_wires_3d` | 3D conductor geometry and attribution |

After saving approved edits, the operator can select **Re-export from GPKG** in the Bakery GUI.

The re-export:

- Allows selection of the RoWs to regenerate.
- Rebuilds the affected product 45, 80 and 267 deliverables.
- Preserves unrelated products and source files.
- Archives previous deliverables before replacement.
- Generates QA diagnostics for excluded or inconsistent records.

Geometry processing and LiDAR sampling are not repeated.

The command-line equivalent is:

```bash
python scripts/04_export_geometry_from_gpkg.py --output-root output --rows NH_02,NH_27
```

## 8. Performance Improvements

### 8.1 Preflight caching

Preflight caches reusable results, including:

- LiDAR tile indexing.
- SoW and Management Unit interpretation.
- RoW, AOI and LiDAR coverage calculations.

Caches are invalidated when relevant source inputs or processing logic change.

This reduces repeated setup work without requiring the operator to manage intermediate files.

### 8.2 Parallel LiDAR sampling

Structure ground elevations and conductor classification evidence are collected using parallel tile processing.

Improvements include:

- Progress reporting with processed tile counts and elapsed time.
- Skipping tiles without relevant structures or conductors.
- Smaller spatial search structures for ground sampling.
- Reduced unnecessary LAZ decompression.

The processing supports configurable worker counts.

These optimisations do not intentionally change the underlying classification or elevation calculations.

## 9. GUI and Operational Improvements

R01.05 improves the existing Bakery interface through:

- Better support for Windows display scaling at 125% and 150%.
- Responsive arrangement of workspace and product controls.
- Scrollable content on smaller displays.
- Improved button layout and visibility.
- More detailed processing progress and status messages.
- Build identification in the GUI and processing logs.

The version/build identity allows operators to identify which code produced a particular dataset.

## 10. QA and Validation

The R01.05 changes were evaluated using representative datasets from Clusters 12, 17, 39 and 55.

### 10.1 Cluster 12 results

The RoW attribution changes produced the following comparisons against R01.04:

| Metric | R01.04 | R01.05 test |
|---|---:|---:|
| Circuit folders | 45 | 34 |
| Circuit folders with over 50% of wire outside the RoW | 15 | 0 |
| 2D wire length delivered into an incorrect RoW | 206.8 km | 0 km |
| Circuit length discarded as disconnected | 18.4 km | 0 km |
| Total delivered 2D circuit length | 214.4 km | 233.2 km |
| Recovered terminal POAs | 173 | 6 |
| Structures in RoWs without their source circuit | 26 | 0 |
| Caydence conductors in RoWs without their source circuit | 67 | 0 |

These results represent the recorded regression configuration and are not a guarantee that every subsequent dataset will exhibit the same results.

### 10.2 Additional validation

Regression testing covered:

- Circuit and RoW boundaries.
- DXF layer-based circuit assignment.
- Separation of parallel conductor bundles.
- Central-phase selection.
- Duplicate conductor handling.
- EARTH wire exclusion.
- Structure positioning.
- Tap recovery.
- Fork and junction cleanup.
- GPKG re-export consistency.

The recorded tests include unchanged-length re-export checks and comparisons of generated Geometry products.

### 10.3 QA outputs

Depending on the processed data and triggered checks, diagnostic outputs include:

- `01_structure_middle_phase_qa.csv`
- `01_support_split_qa.csv`
- `01_caydence_row_circuit_fix.csv`
- `01_row_boundary_qa.csv`
- `01_caydence_structure_dedupe_qa.csv`
- `01_tap_2d_recovery.csv`
- `01_2d_lidar_earth_reselect.csv`
- `01_fork_overlap_trim.csv`
- `01_gpkg_export_checks.csv`

These reports support operator investigation and should be interpreted alongside the source geometry.

## 11. Known Limitations

The following cases may still require manual review:

1. Complex shared corridors where multiple circuits have similar geometry and incomplete source identifiers.
2. Unusual conductor arrangements with ambiguous central-phase selection.
3. Structures carrying several circuits or located near overlapping RoW boundaries.
4. Terminal connections and taps where insufficient source wires are available.
5. Geometric offsets at structures where the physical arrangement changes significantly between spans.

Incorrect attribution should be corrected using source-supported evidence rather than adding artificial conductor connections.

## 12. Release Boundary

R01.05 establishes the improved Geometry generation and editable re-export workflow.

Subsequent R01.06 changes address additional selected-RoW boundary recovery cases, integrate the independent Geometry QA Inspector directly into Bakery, and improve final QA status handling.

**R01.05 does not include the integrated `Run Geometry QA` workflow introduced in R01.06.**

---

**Release status:** Superseded by R01.06.  
**Reference:** `RELEASE_NOTES_R01.06.md`.
