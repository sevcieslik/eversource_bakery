# Eversource Bakery R01.06 - Release Notes

Release date: 22 November 2026
Previous: R01.05-test

## Geometry reliability
- Restore Caydence conductors at selected RoW boundaries when source spans are attributed outside the run selection, preserving source IDs and traceability.
- Recover source-matched POA structures into the selected RoW, including a related terminal 2D span and both physical end supports when evidence is unambiguous.
- Flag phase/2D terminal lateral offsets for review instead of inventing a cross-phase connector or changing original DXF geometry.

## Integrated independent Quality Assurance
- Bundle QA Inspector v0.3 under `scripts/qa_inspector.py`; `Run Geometry QA` runs it from the Bakery GUI using the same `scripts/.venv`.
- Validate one run: original DXF and source SQLite/Structures against Geometry GPKG and final delivered 45/80/267 products.
- Include source attribution statistics, circuit inventory, missing wires/structures, geometric issues, T01 missing terminal vs T02 existing support with 2D offset, and SQLite substation endpoint exceptions.
- Write full report, QA issue GPKG/CSV, statistics and maps to `output/QA/geometry_validation`. Archive previous successful results and warn when results are outdated.
- Capture QA stdout/stderr and tracebacks in GUI Run Log and on disk.

## User interface and setup
- Initial status is `READY FOR PREFLIGHT`; an old selection JSON no longer falsely validates preflight.
- Successful processing is `PROCESSING COMPLETE - QA REQUIRED` until independent operator review.
- `Run Geometry QA` and `QA Results` are integrated into the main GUI.
- Single installer `setup_eversource.bat` provides Geometry and QA dependencies in the same venv, including matplotlib. Fixed the `python -c` verification that could raise `NameError: __file__`.

## Acceptance
- Operator confirmed test run successful before merging the integration branch.
- The QA result is diagnostic and does not constitute automatic client delivery approval.
- No further changes to Geometry algorithms are introduced in this release promotion commit.
