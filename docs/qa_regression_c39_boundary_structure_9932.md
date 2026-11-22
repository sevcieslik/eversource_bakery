# QA regression case: C39 / circuit 310 / source 310:9932

**Input:** One Bakery Geometry run (CT_06, CT_16), original Wires DXF, POA DXF, source Structures SHP, SoW SQLite, one Geometry GPKG, final products 45/80/267.

**Incident:** POA PT00203 corresponds to source structure `310:9932`, `TOPO_ID=ST00192`. Its source-matched physical Structure and `selected_span_wires` edge `ST00196→ST00192` (`BUNDLE_ID=82`) were assigned to unselected `CT_17`. The Caydence conductor records were independently assigned to `CT_16`. Original failure: wires missing from 267; after recovering those, source structure missing from 45; after recovering its representation, the end 2D span still absent from 80.

**Correct result:** preserve original 3D wire geometry and source IDs. Recover the same physical source-matched structure to CT_16, and include its connected 2D span in CT_16, while retaining original CT_17 evidence. Never fabricate a structure at a bare wire endpoint. In this example, 3D endpoints have physical POA evidence and a selected-RoW assignment. Boundary POA recovery requires at least two independent original wire IDs and unambiguous circuit and RoW.

**Known geometry caveat:** the central 2D phase ends ~8.28m from the physical support point in v(5), even though some source 3D conductors terminate near the POA. Do not automatically draw a lateral connector, since it may create a whisker or phase jump. Report `BOUNDARY_POA_2D_ENDPOINT_OFFSET_REVIEW` and check original phase geometry.

**QA Inspector rule to add:** `TERMINAL_WIRE_WITHOUT_STRUCTURE` for 2D and 3D, spatial + topological + source-aware. Every real terminated conductor should resolve to a supported physical structure (including boundary shared supports). Bare endpoints at substation and out-of-scope limits require explicit exceptions, not invented supports. Group matching POA + source + 2D/3D findings into a single physical issue.

**Deliverables gate:** verify 45, 80 and 267 files, not just the Geometry GPKG. Check structure presence by physical/source identity, and continuity of 2D span at both ends. Record matched, missing, scope-excluded and reviewed cases separately.
