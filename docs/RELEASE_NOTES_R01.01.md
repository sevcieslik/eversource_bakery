# Eversource Tester R01.01

Patch release on top of accepted R01.00 geometry.

- Preserve explicit DXF EARTH/PHASE throughout Caydence export and terminal recovery. For generic Mainline DXF layers, conservatively propagate a stable EARTH classification along physically continuous XYZ wire chains (QA CSV provided).
- Second-pass source attribution by circuit corridor and order between matched source controls; supports disconnected reference lines and preserves POA XY. Real EMA 3361 regression recovers 13~111 with unchanged geometry.
- Unmatched or ambiguous candidates remain dummy with QA warnings.
- Operator Manual moved to bottom-left of GUI; unnecessary final manual section removed.

QA report: `01_structure_corridor_attribution.csv`. Full production run is required before release acceptance.
