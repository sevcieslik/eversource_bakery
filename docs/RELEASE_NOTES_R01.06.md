# Eversource Tester R01.06 – Release Notes

**Project:** NMIP26068 Eversource Tx Veg  
**Release:** R01.06  
**Release date:** 22 November 2026  
**Previous release:** R01.05  
**Status:** Released / Merged to main

## 1. Overview

R01.06 improves the reliability and quality assurance of Eversource Geometry deliverables, with particular emphasis on circuit continuity across RoW boundaries, source structure recovery and independent validation of completed products.

The release addresses issues identified during the final review of Cluster 39, where valid source conductors and terminal structures could be omitted from selected RoW deliverables despite existing in the original source data.

It also integrates the independent **Geometry QA Inspector v0.3** directly into Bakery, allowing operators to validate Geometry outputs without running a separate application or configuring additional input paths.

The main objectives are to:

- Prevent valid source 3D conductors from being omitted because of RoW attribution.
- Recover source-supported terminal structures and missing 2D spans.
- Preserve the original physical conductor geometry and source traceability.
- Validate Geometry GPKG and delivered products against the original source data.
- Provide operators with clear QA reports, mapped issues and positive matching statistics.
- Integrate validation into the existing Bakery GUI and runtime.
- Improve processing status handling and prevent premature delivery approval.

The release affects the **Geometry processing, QA and GUI workflows**. LiDAR and PLS XYZ processing algorithms remain unchanged.

## 2. Geometry Reliability Improvements

### 2.1 Caydence conductors at RoW boundaries

R01.06 improves the handling of source 3D conductors located near the boundaries of selected RoWs.

Previously, conductors could be excluded from the final Caydence product when their source span was associated with a neighbouring RoW that was not included in the current processing selection.

This could occur even when the conductors belonged to a circuit that continued into the selected RoW.

The revised logic:

- Evaluates conductor ownership using circuit and RoW relationships.
- Preserves valid conductors belonging to the selected circuit geometry.
- Recovers eligible source conductors that would otherwise be excluded by boundary attribution.
- Retains original source wire identifiers for traceability.
- Avoids creating new or artificial 3D conductor geometry.

The correction is particularly relevant to circuits crossing several RoWs or passing through shared transmission corridors.

### 2.2 Source-supported boundary structure recovery

The terminal POA recovery logic has been extended to handle structures located at the boundary between selected and unselected RoWs.

A valid source structure may be recovered when the available evidence confirms its relationship to a circuit or span included in the processing selection.

Recovery considers:

- Original Tower Top Attachment Points.
- Source structure attributes and identifiers.
- Circuit identity.
- Existing span topology.
- RoW compatibility.
- Nearby physical supports already present in the Geometry output.

The process avoids creating duplicate structures where a suitable physical support already exists.

Source attributes and original identifiers are retained wherever available.

### 2.3 Missing terminal 2D span recovery

R01.06 introduces additional recovery of terminal 2D spans associated with confirmed boundary structures.

Where sufficient source evidence exists, the process restores a missing span between actual structure nodes.

The recovered span is linked to the appropriate circuit and RoW.

The algorithm does not generate arbitrary straight-line connections to nearby structures.

Recovery is restricted to cases supported by the existing network topology and original source data.

### 2.4 Cluster 39 regression case

A specific regression case was identified for circuit 310 in Cluster 39.

The source data contained:

| Source element | Identifier |
|---|---|
| Circuit | 310 |
| Source structure | `310:9932` |
| Terminal structure | `ST00192` |
| Associated POA | `PT00203` |
| Terminal 2D span | `ST00196 → ST00192` |
| Bundle | `BUNDLE_ID 82` |
| Original 3D wires | `DXF_W000376` to `DXF_W000380` |

The original source structure and attachment point were present, but their RoW attribution prevented them from being correctly represented in the selected output.

The correction restores the relevant source conductors, terminal structure and associated 2D span within the selected processing scope.

This case is retained as a regression reference for future Geometry changes.

### 2.5 Preservation of physical geometry

All boundary recovery changes follow the principle that source-supported physical geometry must take priority over artificial geometric completion.

In particular:

- Original 3D wire geometry is not modified solely to improve visual continuity.
- Lateral connections are not automatically drawn between displaced conductors and structures.
- Physical conductor transitions and phase arrangements must remain distinguishable.
- Ambiguous terminal relationships are flagged for review rather than automatically corrected.

This is important for horizontal, vertical and mixed conductor arrangements, including multi-pole and trident configurations.

## 3. Independent Geometry QA Inspector v0.3

### 3.1 Integration into Bakery

QA Inspector v0.3 is now included directly in the Bakery repository:

`scripts/qa_inspector.py`

The application launches the validator through:

`scripts/05_run_geometry_qa.py`

The inspector operates as an independent, read-only validation process.

It evaluates completed Geometry outputs against the original source data, without modifying the processing results or automatically correcting identified issues.

### 3.2 Validation inputs

The validator automatically uses the current Bakery project paths.

| Input | Purpose |
|---|---|
| Original 3D Wires DXF | Source conductor inventory and geometry |
| Original Tower Top Attachment Points DXF | Physical attachment point evidence |
| Source Structures SHP | Source structure identifiers, attributes and positions |
| SoW SQLite | Circuit, RoW and substation reference data |
| `01_eversource_geometry.gpkg` | Generated Geometry features |
| Product 45 | Delivered structure records |
| Product 80 | Delivered 2D circuit vectors |
| Product 267 | Delivered Caydence 3D wires and structures |

Validation operates on **one completed Geometry run**.

It does not compare unrelated or historical GPKG files.

### 3.3 Source conductor completeness

The inspector compares the original 3D DXF conductor inventory against the generated Geometry GPKG and the final delivered Caydence product.

The validation identifies:

- Source conductors missing from the GPKG.
- Source conductors present in the GPKG but missing from product 267.
- Unexpected differences between source and delivered wire inventories.
- Circuit attribution discrepancies.
- Source wires excluded from the selected processing scope.

Source identifiers are preserved to allow operators to locate and investigate specific conductors.

### 3.4 Structure source matching

Delivered product 45 structures are compared with the original client source structure records.

The inspector records:

- Successfully matched source structures.
- Unmatched delivered structures.
- Conflicting source attributes.
- Structures associated with an incompatible circuit.
- Cases where a compatible source structure does not exist.

Unmatched records are not automatically classified as errors when the available source evidence does not support a valid match.

### 3.5 2D continuity and geometry checks

The inspector evaluates representative 2D circuit geometry for potential problems, including:

- Unexpected discontinuities.
- Possible phase changes between consecutive spans.
- Geometric offsets at junctions.
- Circuit attribution inconsistencies.
- Missing terminal support relationships.

Potential geometry issues are recorded for operator review.

A detected offset or discontinuity does not necessarily indicate an incorrect network representation.

### 3.6 Terminal structure validation

Terminal validation distinguishes between genuinely unsupported endpoints and valid circuit terminals with geometric offsets.

**T01 – Terminal Without Structure**

Identifies a terminal endpoint where no valid physical structure can be confirmed through the available circuit and topology information.

**T02 – Structure 2D Offset**

Identifies a terminal endpoint that is topologically associated with a valid physical structure, but whose representative 2D position is offset from that structure.

T02 is a review finding rather than evidence that the structure is missing.

This distinction is particularly important where the 2D representative conductor does not pass directly through the physical support location.

### 3.7 Substation exceptions

Circuit endpoints located within or on source substation polygons are treated separately.

A genuine substation terminal does not require an artificial Tower Top Attachment Point or source structure to be considered valid.

Substation geometry is read from the original SoW SQLite data.

### 3.8 Diagnostic 3D terminal statistics

The inspector also reports unresolved 3D conductor endpoints.

These statistics are diagnostic and are not automatically treated as confirmed missing structures.

Individual DXF conductor segment endpoints do not necessarily represent actual circuit termination points.

Further topology assessment may be required before escalating these cases.

## 4. QA Reports and Deliverables

### 4.1 Output location

All Geometry validation results are generated under:

`output/QA/geometry_validation/`

The output includes:

| File or folder | Description |
|---|---|
| `QA_Report.html` | Main interactive QA report |
| `QA_Issues.gpkg` | Spatial QA findings for GIS review |
| `QA_Issues.csv` | Tabular issue register |
| `QA_Statistics.json` | Summary statistics |
| `01_wire_inventory.csv` | Source and delivered wire inventory |
| `02_structure_source_audit.csv` | Delivered-to-source structure matching |
| `03_reverse_source_audit.csv` | Source-to-delivery structure audit |
| `04_3d_to_2d_corridor.csv` | 3D and 2D corridor comparison |
| `05_circuit_scorecard.csv` | Circuit-level QA statistics |
| `06_terminal_endpoint_audit.csv` | Terminal endpoint assessment |
| `07_positive_terminal_stats.csv` | Confirmed and diagnostic terminal categories |
| `08_delivery_and_positive_matching.csv` | Delivery completeness and positive matching |
| `maps/` | Circuit and issue visualisations |
| `QA_Run_Manifest.json` | Validation run and input metadata |
| `QA_Execution.log` | Execution log and error diagnostics |

### 4.2 Positive matching statistics

In addition to identifying errors, the inspector reports successful validation results.

These include:

- Number of source wires correctly represented in the GPKG.
- Number of source wires delivered in product 267.
- Number of successfully matched source structures.
- Number of valid 2D terminal supports.
- Number of valid substation terminals.
- Number of confirmed 3D structure relationships.

This provides a more complete assessment than an issue list alone.

### 4.3 QA severity and review

Findings are classified according to their impact and level of certainty.

Not all findings represent confirmed defects.

Potential phase transitions, structure offsets and unresolved terminal relationships may require manual interpretation.

Successful execution of the QA Inspector means that validation results have been generated.

**It does not mean that the Geometry deliverables have been automatically approved.**

## 5. GUI Integration

### 5.1 Run Geometry QA

The previous generic `QA` folder button has been replaced with:

**Run Geometry QA**

The operator can launch validation directly after completing Geometry processing.

Bakery automatically passes the current input, source and output paths to the validator.

No additional path selection or external command-line execution is required.

The Geometry processing stage is not repeated.

### 5.2 QA Results

A new **QA Results** button provides access to the latest successful Geometry QA report.

When the report exists, it opens directly in the default web browser.

### 5.3 Processing statuses

GUI status handling has been revised.

| Stage | Status |
|---|---|
| Application initialisation | `READY FOR PREFLIGHT` |
| Preflight completed | `Waiting for RoW selection` |
| Geometry processing completed | `PROCESSING COMPLETE - QA REQUIRED` |
| Geometry QA running | `GEOMETRY QA RUNNING` |
| Geometry QA completed | `QA COMPLETE - REVIEW REQUIRED` |
| Validation failure | `QA ERROR` |

An existing preflight selection file no longer causes Bakery to incorrectly display a validated batch status before a new preflight is completed.

QA execution is reported through the existing Bakery Run Log.

The HTML report opens automatically following successful validation.

### 5.4 Revalidation and report history

QA may be repeated after approved Geometry corrections or re-export of deliverables.

Previous successful validation results are archived before replacement.

If a validation attempt fails, the preceding successful report is retained.

A run manifest records the validated GPKG and deliverable file information.

When the operator opens QA Results after the Geometry GPKG or delivered files have changed, Bakery warns that the previous validation may be outdated.

A new Geometry QA run is required to validate the revised products.

## 6. Installation and Runtime

### 6.1 Unified Python environment

QA Inspector is included in the standard Bakery installation.

Both the Geometry pipeline and QA Inspector use:

`scripts/.venv`

The standard installer is:

`setup_eversource.bat`

Required dependencies are maintained in:

`scripts/requirements_eversource.txt`

The environment includes the existing geospatial processing libraries and the additional `matplotlib` dependency required for QA reporting.

No separate QA environment, installation package or manual script copying is required.

### 6.2 Installation verification

The installer checks the availability of the required Python packages and bundled QA scripts.

R01.06 also corrects an installer verification error caused by referencing `__file__` in a Python command executed through `python -c`.

This previously produced:

`NameError: name '__file__' is not defined`

The corrected verification uses the actual Bakery scripts directory.

### 6.3 Error diagnostics

QA Inspector standard output and error messages are captured by the Bakery launcher.

The execution log includes Python tracebacks when validation fails.

This allows operators and developers to identify the cause of a failure without independently reproducing the full Geometry processing run.

## 7. Validation Results – Cluster 39

The independent QA Inspector v0.3 was evaluated using Cluster 39 with selected RoWs CT_06 and CT_16.

### 7.1 Delivery completeness

| Validation metric | Result |
|---|---:|
| Original source DXF wires | 2,821 |
| Source wires represented in GPKG | 2,821 |
| Source wires delivered in product 267 | 2,821 |
| Missing source wires | 0 |
| Product 45 delivered structures | 621 |
| Structures successfully matched to source | 619 |
| Unmatched structures | 2 |
| Product 267 delivered structures | 520 |
| Product 80 delivered features | 15 |

All 2,821 source 3D conductors were represented in the Geometry GPKG and delivered in product 267.

The two unmatched delivered structures were retained as expected cases where no compatible source record could be confirmed.

### 7.2 QA findings

| Finding | Count |
|---|---:|
| W03 – Circuit mismatch | 1 |
| G01 – Possible phase jump | 8 |
| T02 – Structure 2D offset | 3 |
| T01 – Terminal without structure | 0 |

The identified circuit mismatch concerned source conductor `DXF_W000266`, whose original DXF layer indicated circuit 310 while the output attribution identified circuit 348.

The possible phase jumps remained subject to operator review.

The three T02 findings represented valid terminal structure relationships with lateral offsets in the selected 2D geometry.

### 7.3 Positive terminal statistics

| Terminal category | Count |
|---|---:|
| Valid 2D structure terminals | 9 |
| Topologically linked 2D terminals with offset | 3 |
| Valid 2D substation terminals | 18 |
| Confirmed 3D structure endpoints | 51 |
| Valid 3D substation terminals | 44 |
| Unresolved 3D endpoint candidates | 78 |

The unresolved 3D endpoint candidates are diagnostic and should not be interpreted as 78 confirmed missing structures.

### 7.4 Interpretation

The Cluster 39 validation demonstrated complete source 3D wire representation across the GPKG and delivered product 267 for the tested processing selection.

Structure source matching was also largely successful, while the independent QA process identified a limited number of circuit and geometric relationships requiring review.

The test confirmed the value of separating source completeness, geometric integrity and operator review findings.

## 8. Operator Workflow

The standard Geometry workflow for R01.06 is:

1. Configure the Bakery project and select the required products.
2. Run Preflight and confirm the RoWs to process.
3. Complete Geometry processing.
4. Review the Geometry processing output and status.
5. Select **Run Geometry QA**.
6. Review the generated HTML report, mapped issues and supporting statistics.
7. Correct confirmed issues in the approved Geometry GPKG where necessary.
8. Use **Re-export from GPKG** to regenerate affected deliverables.
9. Run Geometry QA again after changes.
10. Complete operator review before releasing deliverables.

The integrated QA process does not modify the original source files, generated GPKG or delivered Shapefiles.

## 9. Known Limitations

The following cases remain subject to operator review or future development:

- Complex shared corridors where source circuit identifiers and geometric evidence conflict.
- Possible phase jumps that may represent legitimate conductor transitions.
- Lateral offsets between physical structures and representative 2D conductors.
- Unresolved 3D endpoints requiring full network topology interpretation.
- Ambiguous source structure matches.
- Multiple source files with similar names where automatic input identification cannot determine a unique candidate.

The inspector does not automatically approve, reject or modify geometry.

A successful validation run confirms that the QA process completed and produced its reports, not that all findings have been resolved.

## 10. Release Validation and Acceptance

R01.06 was validated through the existing Geometry regression cases and the integrated QA workflow.

The operator confirmed successful installation and testing of the combined Bakery and QA Inspector environment before the changes were merged into the main repository.

The release was completed through:

- **PR #5:** Geometry QA Inspector v0.3 integration.
- **PR #6:** R01.06 version promotion and release documentation.

The final release merge commit is:

`5d75a8d5a5b36f980bf7f68ae93c5347b240ddbf`

R01.06 retains the Geometry improvements established in R01.05 and adds source-supported boundary recovery, integrated independent validation and improved operator-facing QA controls.

---

**Release status:** R01.06 – Released  
**Repository:** `sevcieslik/eversource_bakery`  
**Branch:** `main`  
**Reference:** `RELEASE_NOTES_R01.05.md`
