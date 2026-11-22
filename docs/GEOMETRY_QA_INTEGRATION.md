# Geometry QA in Bakery: change log

Date: 2026-11-22
Branch: `feat/geometry-qa-gui-integration-20261122`
Scope: GUI and independent QA workflow only. No change to Geometry algorithms or products.

## Added
- **Run Geometry QA** button replaces the old generic QA folder action. It starts independent QA after Geometry or re-export without rebuilding Geometry.
- **QA Results** button opens the latest successful `QA_Report.html` (or its folder).
- `scripts/05_run_geometry_qa.py` discovers original wires/POA DXF, source Structure SHP/ZIP and SoW SQLite; verifies a single GPKG and delivered product directory. Ambiguous/missing inputs stop with an explicit error.
- Validated output always lands in `output/QA/geometry_validation/`: HTML report, QA issue GPKG/CSV, statistical CSVs, maps and `QA_Run_Manifest.json`.
- Successful reruns archive the prior `geometry_validation`; failed reruns leave the previous successful report intact. Intermediate/failed staging is retained for diagnosis.
- Separate QA dependencies: `scripts/requirements_geometry_qa.txt`.

## GUI state and safety
- `GEOMETRY QA RUNNING` while validation executes. It streams diagnostic output through the normal Bakery log, blocks concurrent Bakery tasks, and checks the subprocess exit code.
- `QA COMPLETE - REVIEW REQUIRED` on success. Bakery opens the HTML report automatically; completion is not delivery approval.
- On failure shows `QA ERROR`, re-enables controls and retains the previous successful report.
- Input/source/deliverable data remain read-only. No second GPKG comparison, no Geometry rerun.

## QA engine version
- Integrates **QA Inspector v0.3**. Copy its existing `qa_inspector.py` to `scripts/qa_inspector.py` in a Bakery installation (the Python source is distributed as a separate overlay package in this test phase).
- Key checks include DXF → GPKG → 267 inventory, product 45 source attribution, source/POA completeness, circuit mismatches, suspicious 2D kinks, T01 missing terminal support vs T02 lateral 2D offset, and substation polygon terminal exceptions.
- 3D network end statistics remain diagnostic, not automatically CRITICAL.

## Documentation
- Expanded offline operator manual with a new **Run Geometry QA after processing** section.
- Updated `docs/README.md` and this changelog.

## Not included / limitations
- No production operator approval workflow yet. A successful QA process only means a report was generated; findings require human decisions.
- Candidate source input auto-discovery requires unambiguous filenames. It does not guess between multiple wire/POA/source candidates.
- The v0.3 QA engine must be installed into Bakery separately for this branch.
- No Windows GUI end-to-end test was performed via the GitHub connector. Verify on local R01.05-test runtime before merging.
