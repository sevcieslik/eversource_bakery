# Eversource Tester R01.03

- Representative structure point for `STR_TYPE=SCHF` is moved transversely to the middle of three local parallel PHASE wires, without longitudinal displacement.
- Uses LiDAR-classified Caydence PHASE wires (187), not unrelated output products.
- Requires exactly three local wires with consistent tangents and spacing; otherwise leaves the structure unchanged with QA warning.
- Resamples ground class 2 at every moved structure before delivery.
- Does not change the 3D wires, 2D wires, circuit attribution, source attributes or other structure types.
- New QA: `01_schf_middle_phase_qa.csv`.
- Validation: syntax and standalone geometry cases; full Geometry LiDAR rerun required.
