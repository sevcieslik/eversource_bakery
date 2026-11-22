# Eversource Tester R01.02

- Geometry reads classified source LiDAR class 187 (PHASE) and 210 (EARTH) **during the existing ground-elevation scan**, not from PLS XYZ products.
- DXF explicit EARTH/PHASE levels remain authoritative; generic levels use full 3D nearest sample voting with >=12 LiDAR matches, >=4 distinct sampled locations and >=80% majority.
- Raw source wire geometry, circuit assignment, 2D products and structure geometry are unchanged by this classification pass.
- New `01_caydence_lidar_wire_roles.csv` records the evidence and decisions per wire; unconfirmed roles are explicitly identified.
- This release requires UAT on source LAZ tiles: no complete pipeline run was possible in the authoring environment.
