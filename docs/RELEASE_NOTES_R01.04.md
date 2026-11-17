# Eversource Tester R01.04

- Extends the R01.03 transverse middle-PHASE positioning rule to SCSH structures.
- SCSH requires exactly one distinct original Tower Top Attachment point of the same circuit in a 15 m neighborhood; multiple markers (potential trident) block relocation.
- Original CAD point duplicates within 0.75 m count as a single top. Missing raw POA, wrong circuit, ambiguous PHASE geometry and overly distant POA are held for QA.
- SCHF geometry logic remains unchanged. Source attribution, CIRCUIT, and all 2D/3D wires remain unchanged.
- Ground is resampled at any moved XY.
- QA output: `01_structure_middle_phase_qa.csv` (includes STR_TYPE and NEARBY_TOPS).
- Run regression `python scripts/test_schf_scsh_r0104.py`. Full production LiDAR rerun not performed.
