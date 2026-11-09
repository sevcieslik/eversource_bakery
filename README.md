# NM Group — TX Veg Delivery Bakery

Production workflow for NMIP26068 Eversource delivery products.

## Package layout

```text
Eversource_Tester_v25\
│
├─ input\
├─ output\
├─ source\
├─ scripts\
├─ README.md
├─ setup_eversource.bat
└─ run_eversource.bat
```

The root is intentionally operator-facing only.

### input
Default run-specific inputs. The GUI can also point to an external input folder.

### output
Default output location. The GUI can also point to an external output folder.

### source
Project-wide reference files shared across runs:
- client source ZIP
- Project SoW KMZ
- Management Units GDB

### scripts
All workflow internals:
- GUI
- preflight
- geometry pipeline
- LiDAR / PLS pipeline
- requirements
- internal fallback runner
- `.venv` after setup

Processors should not need to edit this folder.

## First-time setup

Run:

```text
setup_eversource.bat
```

This creates the Python environment at:

```text
scripts\.venv\
```

## Run

Run:

```text
run_eversource.bat
```

This launches the TX Veg Delivery Bakery GUI.


## v25 H-frame structure centering fix

Known Eversource H-frame structure types are now recognised explicitly,
including `SCSH`, `SCWH`, `SCHF`, `DCSH`, `DCHF` and `LSCHF`.

The H-frame rule now runs before the generic single-marker rule.

When both contractor leg markers belong to the same topology group, the delivered
Structure XY remains their midpoint.

When the H-frame is wider than the 15 m topology clustering radius and the second
leg was therefore assigned to another topology support, the structure step now
searches the complete contractor `Structure Top Attachment Point` set for the
nearest plausible opposite leg. The candidate must be predominantly transverse
to the local line direction and close to the same along-line station.

The global support clustering radius remains 15 m. It has deliberately not been
increased, avoiding accidental joins between adjacent transmission lines.

If no reliable companion marker exists, the output is explicitly flagged with
`H_FRAME_SINGLE_MARKER_REVIEW` (or uses the existing wire-axis fallback when a
meaningful axis correction is available) instead of silently treating an H-frame
leg as a confirmed structure centre.


## v25 Structure source attribution

Both Structure deliverables now use the same enriched final Structure dataset:

- `Circuit\<Circuit>\<Circuit>_Structure_<year>.shp`
- `Caydence\<RoW>_3D_Structures_<year>.shp`

For every structure successfully matched to `T_Structures_LiDARexport`, **all
non-geometry source fields** are carried into both deliverables.

Source SHP field names are retained unchanged whenever possible. If a source
field collides with a generated formal delivery field such as `COMPANY`,
`VOLTAGE`, `ROW` or `CIRCUIT`, a deterministic `SRC_*` alias is used so no
source value is lost.

The formal delivery fields remain first:

- ROW
- CIRCUIT
- VOLTAGE
- COMPANY
- STRUCT_ID

All mapped source fields follow these columns.

Stage 01 writes `01_structure_source_field_map.csv`, showing the exact
source-field -> delivery-field mapping used for the run.


## v25 update — safe H-frame fallback and AOI-driven CRS

### H-frame geometry

For a known H-frame:

- two reliable contractor markers in the same local topology support -> midpoint;
- one contractor marker -> keep the exact original DXF XY;
- no global search for a second leg is performed;
- no wire-axis fallback is applied to the single-marker H-frame;
- client/source XY is not allowed to move this single-marker fallback.

Single-marker H-frames are flagged as `H_FRAME_SINGLE_MARKER_DXF` for QA.

This prevents a structure on a parallel circuit from being mistaken for the
opposite H-frame leg.

### Project CRS

The GUI now treats published `input\AOI\*_Polygon.prj` files as the preferred
project CRS source.

When all published Polygon AOIs resolve to one EPSG:

- the Project CRS is selected automatically;
- known CT / MA Mainland / NH EPSGs use the normal named dropdown value;
- other EPSGs use `Other EPSG` with the detected code;
- the CRS controls are locked for the run.

If no published Polygon AOI `.prj` exists, manual CRS selection remains
available.

If AOI Polygon `.prj` files exist but are unreadable or disagree with each
other, the GUI blocks the run rather than guessing a CRS.


## v25 update — Structure geometry fully decoupled from client STR_TYPE

Client/source structure type fields are now attribution and QA only. They no
longer control Structure XY.

Geometry is determined from local contractor DXF/topology only:

- 0 local markers -> topology/client review fallback
- 1 local marker -> exact original DXF XY
- 2 local markers -> midpoint
- 3 local markers -> middle structural member
- 4+ local markers -> best structural triplet + outlier handling

`STR_TYPE`, `NO_OF_POLE`, `ESTPOLECOU` and related client fields are still
retained in the deliverables and may be compared against geometry through QA
fields such as `GEOM_TYPE`, `SOURCE_TYPE` and `TYPE_MATCH`, but they cannot
move a Structure point.


## v25 hotfix — ESRI AOI `.prj` CRS recognition

Some valid ArcGIS/ESRI `.prj` files do not contain an EPSG `AUTHORITY` tag.
For example, Massachusetts Mainland may be named:

`NSRS_2011_Massachusetts_State_Planes_Mainland_Zone_US_Foot`

The GUI/preflight CRS detector no longer rejects these files solely because
`CRS.to_epsg()` returns `None`.

Resolution now tries:

1. normal EPSG/authority lookup;
2. pyproj low-confidence authority matching;
3. semantic CRS equivalence against the supported project CRSs;
4. known ESRI name aliases for CT, Massachusetts Mainland and New Hampshire.

The CRS is still locked only when all published Polygon AOIs resolve to one
consistent supported project CRS.


## v25 update — CRS suggestion + operator override

AOI `.prj` remains the preferred automatic CRS source, but the GUI no longer
locks the CRS control.

Behaviour:

- a consistent published Polygon AOI `.prj` sets the default CRS;
- the Project CRS dropdown remains editable;
- if the operator keeps the detected value, the run records `CRS_SOURCE=AOI_PRJ`;
- if the operator selects a different EPSG, the GUI asks for confirmation;
- a confirmed override records `CRS_SOURCE=OPERATOR_OVERRIDE`;
- when no AOI `.prj` can be resolved, manual selection records
  `CRS_SOURCE=OPERATOR_MANUAL`.

The previous low-confidence `pyproj.to_epsg(min_confidence=0)` fallback has
been removed because it could incorrectly return the metric Massachusetts
Mainland EPSG:6491 for a US-survey-foot ESRI `.prj` that should use EPSG:6492.

Stage 00 now writes:

`output\00_preflight\00_crs_selection.json`

with detected EPSG, used EPSG, source/provenance, override flag and the AOI
`.prj` files used for detection. The same metadata is also embedded in
`00_preflight_selection.json`.


## v25 update — legitimate circuit taps preserved

Circuit geometry is now built as a MAIN spine plus accepted TAP_BRANCH parts.

The double-circuit pairing and circuit-attribution logic is unchanged.

An off-spine component is retained only when it is part of the circuit spanning
tree, attaches to MAIN at exactly one support, and every bundle has HIGH or
MEDIUM circuit-attribution confidence.

Disconnected components, REVIEW-confidence branches and alternate/cycle routes
remain excluded.

The formal Wires product remains one feature per circuit. If taps exist, that
feature is a multipart polyline containing the MAIN and TAP_BRANCH parts.

Structures belonging to accepted taps are included in Circuit Structures and
the RoW-level Caydence Structures product.

QA additions include `PATH_ROLE`, `BRANCH_ID`, final wire `N_TAPS` /
`N_TAP_SP`, and `01_path_role_summary.csv`.


## v25 update — trimmed formal 3D Structure attributes

The formal Structure deliverables now use the same concise schema for both:

- `Circuit\<Circuit>\<Circuit>_Structure_<year>.shp`
- `Caydence\<RoW>_3D_Structures_<year>.shp`

Fields:

- ROW
- CIRCUIT
- VOLTAGE
- COMPANY
- STRUCT_ID
- SRC_COMP
- MATERIAL
- YEAR_CONST
- STR_TYPE
- GLOBALID

All other matched source attributes remain available in internal QA /
`01_eversource_geometry.gpkg` and are not published in the formal Structure SHPs.


## v25 update — Structure-anchored Caydence circuit attribution

This change is limited to the `CIRCUIT` attribution fallback for lossless
Caydence 3D wires.

Attribution priority is now:

1. `TRACK_BUNDLE` — unchanged and never overridden;
2. `STRUCTURE_ANCHOR` — for a raw source wire not present in an analytical
   bundle, if either endpoint is within 15 m of a final Structure carrying a
   circuit, inherit that Structure's `CIRCUIT`;
3. `NEAREST_REFERENCE` — unchanged fallback when no Structure anchor is within
   15 m.

If more than one Structure is within 15 m, the closest endpoint/Structure pair
wins.

The change does not alter 2D wires, Structure geometry, tap-branch logic,
LiDAR, PLS, CRS, or source 3D wire geometry. `WIRE_TYPE` logic is unchanged.

`01_caydence_completeness.csv` now also reports total counts by attribution
method (`TRACK_BUNDLE`, `STRUCTURE_ANCHOR`, `NEAREST_REFERENCE`).
