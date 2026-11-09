# NMIP26068 Eversource Tester v13

## Folder layout

```text
Eversource_Tester_v13/
├─ scripts/
│  ├─ 00_preflight_eversource.py
│  ├─ 01_build_eversource_geometry.py
│  ├─ 02_build_eversource_points.py
│  └─ eversource_pipeline_core.py
├─ input/
│  ├─ AOI/        published RoW *_Polygon.shp files
│  └─ lidar/      LAS/LAZ input tiles
├─ source/        client source ZIP, SoW KMZ, TVM_MUs.gdb
├─ output/        created automatically
├─ run_eversource_pipeline.py
├─ setup_eversource_tester.py
├─ requirements_eversource.txt
└─ README.md
```

The two geometry DXFs are placed directly under `input/`.

## Run

Use 64-bit Python 3.14.

```powershell
py -3.14 .\setup_eversource_tester.py
.\run_eversource.bat
```

or:

```powershell
.venv\Scripts\python.exe .\run_eversource_pipeline.py
```

## Stage 00 — preflight

Preflight indexes the LiDAR, compares it with Project RoWs and published Polygon AOIs, reports input coverage, and lets the operator select RoWs.

It also predicts point-cloud handling:

- `NO_CLIP_ASSUMED` — only one published AOI is present. Input LiDAR is assumed to have already been spatially prepared for that RoW. Points are not re-clipped.
- `HEADER_ONLY` — multiple AOIs exist, but the RoW's input tiles overlap only that AOI. Points pass through unchanged spatially.
- `HYBRID` — some tiles are exclusive and some overlap multiple AOIs. Only shared tiles are clipped.
- `CLIP_SHARED` — all relevant tiles are shared between multiple AOIs and are clipped.

Coverage below 100% is a warning, not an automatic rejection. A published Polygon AOI is required to select a RoW.

## Stage 01 — geometry

Builds 2D wires, physical Structure XY, circuit/client attribution and Caydence source geometry. Structure positions are not forced onto the selected 2D conductor.

Structure rules include single-member, H-frame midpoint, three-member middle and robust three-member-with-outlier detection, with client structure XY retained as comparison/fallback evidence.

## Stage 02 — point cloud / delivery

For each selected RoW:

1. rewrites delivery LAS metadata to LAS 1.4 with the project State Plane / NAVD88 delivery CRS;
2. does **not** point-clip tiles that are exclusive to the RoW;
3. point-clips only tiles whose footprint overlaps two or more published RoW AOIs;
4. exports PLS XYZ feature files from the same delivered point set;
5. samples LiDAR class-2 ground elevation at final Structure XY;
6. builds final Tile Index, Management Units and delivery tree.

Each delivered tile records `PROCESS` (`PASS_THROUGH_SINGLE_AOI`, `PASS_THROUGH_EXCLUSIVE_TILE`, or `CLIPPED_SHARED_TILE`) and number of AOIs intersecting the source tile.

For conservative testing, Stage 02 can be forced to clip every intersecting tile:

```powershell
.venv\Scripts\python.exe .\scripts\02_build_eversource_points.py --force-clip
```

## Important XYZ note

The current Python XYZ export maps source classes directly to the feature files defined by the supplied TerraScan/PLS macro. TerraScan-specific model-key ground generation and vegetation thinning are not yet reproduced exactly and remain explicitly flagged in the XYZ review output.


## v14 - LAS header hard validation

Delivery LAS files now receive an explicit LAS 1.4 OGC WKT CRS record:

- `LASF_Projection`, record ID `2112`
- Horizontal State Plane CRS: selected project EPSG
- Vertical CRS: NAVD88 height (ftUS), EPSG:6360
- LAS Global Encoding WKT flag enabled

After every LAS is written, the file is reopened from disk and its CRS is parsed.
The pipeline fails rather than delivering a file with missing or incorrect CRS metadata.

The Tile Index and delivery inventory now also record:
`HEADER_CRS`, `H_EPSG`, `V_EPSG`, and `CRS_VLRS`.

Optional independent check:

```powershell
.venv\Scripts\python.exe .\scripts\03_validate_las_headers.py .\output\02_points\deliverables --epsg 6492
```

Use EPSG 6434 for CT, 6492 for Massachusetts Mainland, or 6525 for New Hampshire.


## v15 - Production header assignment through LAStools

The three supplied Production header BAT files are retained under:

`scripts\reference_header_bats\`

They are reference copies only. The pipeline now executes `las2las.exe`
directly after point clipping/pass-through and before a LAS is accepted as final.

Commands reproduced by the Python pipeline:

- CT: `-epsg 6434 -survey_feet -elevation_survey_feet -vertical_navd88`
- Massachusetts: `-epsg 6492 -survey_feet -elevation_survey_feet -vertical_navd88`
- NH: `-epsg 6525 -survey_feet -elevation_survey_feet -vertical_navd88`

This removes the date/version-folder behaviour from the old BAT files but retains
the actual Production CRS/header recipe.

### las2las.exe discovery

The pipeline checks, in order:

1. `--las2las <path>`
2. environment variable `LAS2LAS_EXE`
3. package-local `tools\LAStools\bin\las2las.exe`
4. package-local `scripts\LAStools\bin\las2las.exe`
5. PATH
6. common `C:\LAStools\bin\las2las.exe` locations

Example:

```powershell
set LAS2LAS_EXE=C:\LAStools\bin\las2las.exe
.venv\Scripts\python.exe .\scripts\02_build_eversource_points.py
```

The header operation runs on the final point set, so shared-RoW clipping happens
first. For pass-through tiles only the container/header is rewritten; XYZ coordinates
are not reprojected.


## v16 - parallel LAS header processing

Stage 02 now runs independent `las2las` tile jobs concurrently.

Default:

```powershell
python .\scripts\02_build_eversource_points.py --workers 6
```

For a fast local SSD, 6 workers is a reasonable starting point. If the source/output
is on a network drive or a slower disk, 3-4 workers may be faster because the process
is strongly I/O-bound.

`--workers 1` restores sequential behaviour.

For pass-through tiles there is no longer a `.__preheader__.las` copy: the original
source LAS/LAZ is passed directly to `las2las`, reducing disk I/O. Temporary LAS files
are created only for tiles that actually require RoW clipping.


## v17 - PLS thinning and final output layout

Final client-facing products now live directly under:

`output\deliverables\<RoW>\...`

`output\02_points\` is QA / processing evidence only:
GPKG, audits, structure review, XYZ review and inventory.

PLS XYZ thinning now follows the confirmed selection rule:

- Ground (class 2 -> Feature_904_Ground.xyz):
  one point per 65 ftUS XY cell, keep the **minimum Z** point.
- Medium vegetation (class 4 -> Feature_2202_Medium Veg.xyz):
  one point per 13 ftUS XY cell, keep the **maximum Z** point.
- High vegetation (class 5 -> Feature_2203_High Veg.xyz):
  one point per 13 ftUS XY cell, keep the **maximum Z** point.
- Other mapped classes are exported directly.

Thinning is accumulated across the entire selected RoW, not independently per
tile, so duplicate cells at tile boundaries are resolved consistently.

`02_xyz_export_review.csv` reports both source-point count and final output-point
count for each mapped class.


## v18 - actual LiDAR boundary and visible Stage 02 progress

Preflight now runs LAStools `lasboundary` on the actual LAS/LAZ points.
Tile bounding boxes are no longer the coverage truth.

Console/output:
- AOI coverage from the actual point-cloud boundary
- percentage of the LiDAR footprint inside each AOI
- `output\00_preflight\00_lidar_boundary.shp`
- `output\00_preflight\00_preflight_spatial.gpkg`

For one published AOI:
- >= 99.5% of LiDAR boundary inside AOI -> pass-through
- < 99.5% -> clip to AOI

Stage 02 no longer performs a long silent XYZ scan before LAS output.
It now runs:
1. tile discovery
2. clipping only if needed
3. parallel las2las header rewrite
4. PLS XYZ export/thinning with `xyz [n/N]` progress


## v19 - Tile Index restored + PLS thinning corrected + Full Class backup

### Tile Index
Tile Index geometry is again the full nominal tile grid (normally 1500 x 1500
project units). It is never clipped to the RoW AOI. A LAS file may be clipped to
the RoW, but its Tile Index feature remains the full source/delivery tile square.

### PLS thinning
The NMIP26068 TerraScan macro parameters are now mirrored more closely:

Ground:
`FnScanClassifyModelKey("2",6,65.00,0.33,0.33,0)`

Python implementation:
- 65 ftUS initial key-point spacing
- +/-0.33 ftUS adaptive surface tolerance
- TIN-based iterative refinement
- lowest/lower-envelope source point favoured when a cell needs another key point

This is an approximation of TerraScan's proprietary ModelKey implementation,
not a 65 x 65 ft regular grid.

Medium / High Vegetation:
`FnScanThinPoints(...,3.000,3.000,0)`

Python implementation:
- 3 ftUS XY
- 3 ftUS Z
- highest real source Z retained in each local 3-D bucket

### Full Class
For validation and fallback, unthinned point exports are also written:

`output\deliverables\<RoW>\PLS\Full Class\`

containing the full:
- Ground
- Medium Vegetation
- High Vegetation

using the same filenames as the thinned files in `PLS\Points`.


## v20 - multicore per-tile lasboundary + buffered dissolve

Preflight LiDAR coverage is now built in parallel per physical LAS/LAZ tile.

Default behaviour:

1. Up to 6 independent `lasboundary` processes run concurrently.
2. Each `lasboundary` process uses `-cores 1`.
3. Console progress is shown as `boundary [n/N]`.
4. Per-tile polygons are buffered by +0.1 ft.
5. Python dissolves / unions the buffered polygons.
6. The merged footprint is buffered by -0.1 ft.
7. AOI coverage is calculated from that final footprint.
8. Preflight then continues normally to the coverage table and RoW selection.

Outputs:

- `output\00_preflight\00_lidar_boundary_tiles.gpkg`
- `output\00_preflight\00_lidar_boundary.shp`
- `output\00_preflight\00_preflight_spatial.gpkg`

The individual temporary SHP files are kept outside the output folder and disappear
when preflight exits.

CLI:

```powershell
python .\scripts\00_preflight_eversource.py --boundary-workers 6 --boundary-merge-buffer-ft 0.1
```

`--boundary-cores` remains accepted as a backward-compatible alias for
`--boundary-workers`.


## v21 - faster PLS export, vegetation fix, cleaner preflight

### Vegetation
Medium and High Vegetation no longer use a Python 3-D bucket. The pipeline now
uses LAStools directly across the complete RoW:

`lasthin64 -lof <final LAS list> -keep_class <4|5> -step 3 -highest`

There is no Z bucketing. One highest source point is retained per 3 ftUS XY cell.

### PLS XYZ speed
The old `xyz [1/N]` Python/laspy pass has been removed for normal class export.

LAStools `las2txt64` now decodes the complete final RoW once using `-lof -merged`
and streams `x y z classification` to Python. Python only routes those rows into
the final feature files and the `PLS\Full Class` backups.

Ground keeps the accepted adaptive TIN/model-key approximation but runs on
physical tiles in parallel (up to four workers) and concatenates the results.

### Preflight
The table now shows both `MU cov.` and `AOI cov.`.

If no published AOI exists:
- MU coverage >=95% -> `ADD AOI TO PROCESS`
- lower positive coverage -> `LOW_COVERAGE`

RoWs without AOI remain blocked from processing.

### Console
Expected pyogrio/GDAL warnings for measured M geometry and Shapefile field-name
laundering are suppressed so normal runs show pipeline progress rather than
library diagnostics.


## v22 - LAStools extended-class fix

The merged PLS export now uses two `las2txt64` passes:

- standard classification values 0-31: `-keep_class`
- extended classification values >31: `-keep_extended_class`

This fixes the LAStools error:

`needs arguments between 0 and 31 but '64' is out of range`

The parse string is now `xyzcl`; the extra scanner-channel field forces LAS 1.4
classification handling while Python still consumes only XYZ + classification.

Vegetation thinning also explicitly uses `-merged`, so class 4 / 5 thinning is
performed across the complete RoW rather than independently per input file.


## v23 - disk-based multicore PLS workspace

PLS generation no longer uses one RoW-wide Python stream.

### Tile partial workflow

Each final LAS tile is processed independently on a worker:

`output\02_points\temp\<RoW>\tiles\<tile>\`

Each tile workspace contains only the classes present in that tile:

- `direct\` - partial XYZ files for normal PLS feature classes
- `full\` - unthinned Ground / Medium Veg / High Veg
- `ground_thin\` - thinned ground for that tile
- `_DONE.json` - source file fingerprint and point counts

The worker uses one `las2txt64` process for its LAS tile and routes only mapped
classes to partial files. No RoW-wide point array is held in Python memory.

Ground loads only one tile's Full Class Ground at a time for the accepted
adaptive TIN/model-key approximation.

After all workers finish, final direct/full/ground XYZ files are built by binary
concatenation of the partial files. They are not parsed again.

### Vegetation

Medium/High vegetation remains intentionally RoW-wide:

`lasthin64 -lof <final LAS list> -merged -keep_class 4|5 -step 3 -highest`

This avoids thinning seams at physical tile boundaries.

### Restart behaviour

If Stage 02 is interrupted, completed tile folders remain under
`output\02_points\temp\<RoW>` with `_DONE.json` markers. A rerun reuses matching
partials instead of re-exporting those tiles.

After a fully successful PLS build the temp folder is deleted by default.

Use:

```powershell
--keep-temp
```

to retain all tile partials after success for QA/debugging.


## v24 - NMIP26068 PLS feature schema aligned to project macro

PLS output mapping now follows exactly:

- `NMIP26068_XYZ_Outputs_v1.mac`
- `NMIP26068_Class_v1.ptc`
- `NMIP26068_Circuit_Feature_Code_Descriptions_v1.xlsx`

Expected `PLS\Points` files:

- Feature_2_Ground.xyz
- Feature_4_Medium Vegetation.xyz
- Feature_5_High Vegetation.xyz
- Feature_79_Unmade_Tracks_Paths.xyz
- Feature_126_Buildings.xyz
- Feature_132_Wall_Fence.xyz
- Feature_186_Substation.xyz
- Feature_187_Tx_Conductors.xyz
- Feature_210_Tx_Shield_Wires.xyz
- Feature_215_Tx_Structures.xyz
- Feature_224_Dx_Structures.xyz
- Feature_226_Dx_Wires.xyz
- Feature_253_Dx_Guy_Wire.xyz
- Feature_254_Tx_Guy_Wire.xyz

All 14 files are created even when a class has zero points.

Ground and vegetation thinning are unchanged from v23.
`PLS\Full Class` remains limited to Ground, Medium Vegetation and High Vegetation.

The TerraScan macro copies classification values into Intensity for historical
LAS 1.2 compatibility. The Python/LAStools pipeline uses LAS 1.4 classification
values directly and deliberately leaves Intensity unchanged.


## v24 update - AOI-safe Process All and persistent lasboundary cache

### Process All
`A = all available` now means all RoWs that currently have a published Polygon
AOI. RoWs detected from Management Units / LiDAR but missing an AOI are reported
and skipped rather than causing the BAT to fail.

If no RoW has an AOI yet, preflight writes a selection with status
`WAITING_FOR_AOI` and the full pipeline stops cleanly after Stage 00.

### LiDAR boundary cache
The expensive per-tile `lasboundary` result is now cached under:

- `output\00_preflight\00_lidar_boundary.shp`
- `output\00_preflight\00_lidar_boundary_tiles.gpkg`
- `output\00_preflight\00_lidar_boundary_cache.json`

The cache fingerprint includes the LiDAR file set, CRS, lasboundary settings and
the lasboundary executable. Adding or changing an AOI does NOT invalidate it.

Therefore:

1. run preflight without an AOI;
2. lasboundary is calculated once;
3. add the missing `*_Polygon.shp`;
4. run the BAT again;
5. the existing boundary is reused and only AOI/MU coverage is recalculated.

Changing the LAS/LAZ input, CRS, boundary settings, or lasboundary executable
invalidates the cache automatically.


## v24 update - product selection, geometry-first delivery and DXF content detection

### Product menu
After Stage 00 / RoW selection the BAT now asks:

- `A` - Generate all
- `S` - Generate selected

Selectable groups are:

1. Geometry - 2D Wires, ground-level Structures and Caydence geometry
2. LiDAR - AOI-bounded LAS with delivery header
3. PLS XYZ - NMIP26068 feature outputs
4. Tile Index
5. Management Units

Only selected product folders are refreshed. Unselected existing product folders are
left untouched so individual products can be tested independently.

Geometry runs first and is written immediately to `output\deliverables\<RoW>\Circuit`.
Structure ground Z is sampled from the source LiDAR during Stage 01, so Structures no
longer wait for Stage 02.

If PLS is selected without LiDAR, clipped/headered LAS files are created only as a
technical dependency under `output\02_points\temp` and are not published as LiDAR
products.

### AOI clipping
AOI is now a hard point-cloud delivery boundary. A source tile is passed through only
when the entire LAS header XY bounding box is covered by the AOI. All edge tiles are
point-clipped before header rewrite. Whether a tile is exclusive/shared between RoWs
no longer decides clipping.

### DXF names
DXF filenames are ignored when deciding which file is the 3D wire source and which is
the Structure Top Attachment Point source. The tester inspects entity types, geometry
lengths and 3D linear content. Names such as `AOIs.dxf` and `wires.dxf` are accepted if
their actual contents identify their roles.


## v24 update - client attribution and PLS reuse

- Tile Index client shapefile contains `ROW`, `TILE_NAME`, `CAP_YEAR` plus geometry only.
  Point count, processing mode and AOI-hit fields remain QA-only.
- Wires and Structures client shapefiles contain the Technical Assessment attribution
  `ROW`, `CIRCUIT`, `VOLTAGE`; richer processing attributes remain in QA layers.
- Management Units preserve original client source attribution and append `CLIENT`, `ROW`
  and intersecting `TILES` fields; pipeline-only helper fields are excluded.
- If PLS is selected after LiDAR was already generated, Stage 02 reuses the existing
  AOI-bounded/headered delivery LAS files when a persistent manifest confirms the same
  source LiDAR, AOI, EPSG and capture year. Clipping/header rewrite are skipped.
- If the reusable delivery set is absent or stale, PLS falls back to a temporary AOI-bounded
  working LAS set under `output\02_points\temp`.


## v24 UAT fixes - PLS class column, ground neighbours and QA history

### PLS ASCII format
Every `PLS\Points` and `PLS\Full Class` XYZ now uses four columns:

`X Y Z CLASS`

The fourth value is the feature/class number represented by that file.

### Ground thinning
Ground keeps the existing adaptive ModelKey/TIN approximation, but now follows the
project macro requirement to run with 10 ft neighbours.  Each central tile is
processed with class-2 ground from its own tile plus a 10 ft halo from adjacent
final LAS tiles.  The TIN is built from the combined set and only key points inside
the central nominal tile are emitted.  This removes tile-edge thinning seams while
keeping memory bounded per tile.

Ground partial cache markers include the algorithm version, 10 ft neighbour value,
ModelKey parameters, XYZ format version, and fingerprints of all contributing LAS
files.  Old 3-column/old-algorithm partials are therefore invalidated automatically.

### QA history
Canonical QA files remain under `output\00_preflight`, `01_geometry` and `02_points`
as the latest view.  Every successful run is additionally archived under:

`output\QA\<timestamp>_<stage>[_products]\`

with a `QA_RUN_MANIFEST.json`.  Selected-product runs can no longer erase the QA
evidence from earlier successful runs.

### Preflight candidate LiDAR metric
`Cand. in AOI` is now calculated from the actual per-tile LiDAR boundaries for only
the source tiles that are candidates for that RoW.  The denominator is no longer
the footprint of the entire multi-RoW input package.


## v24 UAT update - double-circuit split recovery + ground v6 tuning

### Double circuit
Physical-track continuation at a structure is no longer forced when two possible
continuations are effectively tied. A bundle pair must now be a mutual best match
with at least 2 m of pairing-cost separation from the next alternative.

At an ambiguous shared structure the bundles remain separate physical-track
segments. They are attributed to circuits independently and then reconnected by
the circuit graph through their common structure. This specifically targets the
observed merge -> shared structure -> split case where one final circuit tail was
previously dropped.

`track_pairing` QA now includes `PAIR_STATUS` and `PAIR_MARGIN`.
Ambiguous cases are recorded as `AMBIGUOUS_NOT_JOINED`.

### Ground
Ground ModelKey approximation is now tuned to:

- initial XY spacing: 15 ftUS
- vertical tolerance: +/-0.30 ftUS
- neighbour halo: 10 ft
- one RoW-wide grid origin for all tiles

Per-tile forced hull/extreme seeds have been removed. Outside-hull points enter
through the normal refinement process, using the same globally aligned grid as
every other block.

The ground partial-cache algorithm version has changed, so old ground partials
are automatically invalidated and rebuilt.


## v24 UAT update - RoW-wide ground thinning for long corridors

The per-source-tile Python/TIN ground thinning path is no longer used by the
production PLS build.

Ground class 2 is now processed as one logical RoW using LAStools:

`lasthin64 -lof <all final LAS> -merged -keep_class 2 -adaptive 0.30 15 -sparse`

This is intentionally RoW-wide so the thinning model cannot reset at physical
source-tile boundaries. `-sparse` is used specifically to support long, narrow
utility corridors without constructing one RoW-wide NumPy array in Python.

The thinned result is written first to one temporary LAZ on disk and then
converted to the final four-column:

`Feature_2_Ground.xyz` -> `X Y Z 2`

### Long-corridor fail-safe

If adaptive LAStools thinning returns an error on an unusually large dataset, the
pipeline does not immediately fail. It automatically retries class 2 using one
RoW-wide sparse 15-ft lowest-Z grid:

`lasthin64 ... -step 15 -lowest -sparse`

The fallback is less ModelKey-like but remains globally consistent and cannot
introduce source-tile density seams.

A fingerprinted temporary ground LAZ/manifest can also be reused after an
interrupted later PLS step when the LiDAR inputs and ground algorithm are
unchanged.


## v24 Release Candidate

This package consolidates the UAT fixes accepted to date.

Key production behaviour:

- Geometry is generated first when selected.
- Double-circuit merge/shared-structure/split logic uses the validated v3
  physical-track pairing approach.
- LiDAR is hard-clipped to published Polygon AOI before delivery.
- Existing valid delivery LAS can be reused for later PLS-only runs.
- Ground is thinned RoW-wide with LAStools adaptive TIN processing rather than
  independently per source tile, removing visible block-boundary density seams.
- Vegetation thinning remains RoW-wide.
- PLS XYZ output is `X Y Z CLASS`.
- QA outputs are archived per run instead of being overwritten.
- Preflight lasboundary is cached when LiDAR input is unchanged.
- Default per-tile LAStools worker count is 12. RoW-wide merged operations keep
  their own LAStools execution model and are not multiplied to 12 processes.

The worker count can still be overridden manually:

`00_preflight_eversource.py --boundary-workers N`

`02_build_eversource_points.py --workers N`


## External workspace paths

The tool no longer needs project data to live inside the tool installation
folder.

Keep one installed copy of the tool and its `.venv`, for example:

```text
C:\NMGroup\Tools\EversourceProcessor\
    .venv\
    scripts\
    run_eversource.bat
    workspace_config.json
```

Project data can live anywhere:

```text
D:\Projects\NMIP26068_UAT\
    input\
        AOI\
        lidar\
        wires.dxf
        towers.dxf
    source\
        client_source.zip
        Project_SoW.kmz
        TVM_MUs.gdb
    output\
```

On the first `run_eversource.bat`, the tool asks for:

1. project input folder;
2. project source folder;
3. output root.

If the selected input folder has a sibling folder named `source`, it is offered
automatically.

The paths are saved in `workspace_config.json` beside the tool. Subsequent runs
show the saved paths:

- Enter = reuse them;
- C = choose different project paths;
- Q = quit.

`reset_workspace_paths.bat` deletes only the saved path configuration. It does
not touch the venv, project inputs or outputs.

All stage folders and deliverables are created under the selected external
output root:

```text
<output>\
    00_preflight\
    01_geometry\
    02_points\
    deliverables\
    QA\
```

The Python environment therefore remains installed once in the central tool
folder and can be reused across different project workspaces.


## Workspace path behaviour - revised

`source` is now fixed inside the installed tool because those reference files are
shared across the project.

Recommended layout:

```text
C:\NMGroup\Tools\EversourceProcessor\
    .venv\
    scripts\
    source\
        client_source.zip
        Project_SoW.kmz
        TVM_MUs.gdb
    run_eversource.bat
```

Only `input` and `output` are project/run-specific.

Every time `run_eversource.bat` starts, the tool asks for both paths.

If a previous path exists:

```text
Last input: D:\Projects\Job_A\input
Press Enter to use last input, type a new path, or B to browse:
```

The same applies to output.

Therefore the last paths are remembered only as a convenience. They are never
silently reused: every run requires an Enter confirmation or a new path.

The fixed `source` path is displayed at the start of every run but is not
prompted for.


## v24 UAT update - lossless Caydence 3D source by RoW

Caydence 3D Wires and Tower Tops are now RoW-level products:

```text
deliverables\
  <RoW>\
    Caydence\
      <RoW>_3D_Wires_<year>.shp
      <RoW>_Tower_Tops_<year>.shp
    Circuit\
      <Circuit>\
        <Circuit>_Wires_<year>.shp
        <Circuit>_Structure_<year>.shp
```

The Caydence 3D wire export no longer depends on circuit/path analysis.

Previously, only source wires belonging to surviving analytical span bundles and
final circuit paths were repackaged. This could remove source spans after
endpoint matching, bundle recognition or branch/path filtering and create holes
in the 3D Caydence shapefile.

The new rule is deliberately lossless:

- every source 3D DXF wire is copied unchanged exactly once;
- every source tower-top marker is copied exactly once;
- source geometry is never flattened, simplified or reconstructed;
- entities are grouped by RoW using the SoW/KMZ reference;
- if only one RoW is selected, all source entities go directly to that RoW;
- circuit attribution is not required for Caydence.

Stage 01 now writes `01_caydence_completeness.csv` and hard-fails if the number
of delivered source entities differs from the input count. Analytical 2D Wires
and Structures continue to use the existing circuit logic.


## v24 fix - compound LAS CRS validation

LiDAR CRS validation now compares only the horizontal/projected CRS component.

A LAS carrying a correct compound CRS such as:

`EPSG:6492 + NAVD88`

is accepted when Massachusetts Mainland `EPSG:6492` is selected. The vertical
NAVD88 component is retained and no longer causes a false CRS mismatch.

Actual horizontal mismatches still fail.


## v24 production update - PLS class 1, Caydence attribution/structures, console UI

### PLS
Class 1 is now a normal project PLS output:

`Feature_1_Unclassified.xyz`

The final PLS Points schema therefore contains 15 feature XYZ files. Class 1
uses the same `X Y Z CLASS` format as all other features.

### Caydence 3D Wires
The 3D wire export remains lossless: every original DXF wire is still emitted
exactly once. Attribution is now added without controlling survival of geometry:

- ROW
- CIRCUIT
- VOLTAGE
- COMPANY
- WIRE_TYPE
- WIRE_ID
- source layer/entity

Existing analytical bundle/track attribution is preferred. Source wires outside
the analytical circuit bundles use nearest client/KMZ circuit reference as a
fallback rather than being discarded.

### Caydence 3D Structures
Caydence Structures no longer use the raw tower-top marker package.

They are now exactly the final circuit-level 3D Structure product (including
LiDAR-derived ground Z), concatenated into one shapefile per RoW:

`<RoW>_3D_Structures_<year>.shp`

The circuit-level Structure products remain unchanged under `Circuit\<Circuit>`.

### Console
The console now uses a lightweight bakery theme:

- PREHEATING = preflight / boundary / AOI checks
- MIXING = geometry
- BAKING = LiDAR clipping/header work
- ICING = PLS
- PACKING = final supporting products

Major tile loops use in-place ASCII progress bars with elapsed time and ETA.
No new Python package is required.


## v24 GUI preview - TX Veg Delivery Bakery

The default `run_eversource.bat` now launches a Tkinter desktop GUI.

The existing processing engine is unchanged. The GUI orchestrates the same
production scripts:

- `00_preflight_eversource.py`
- `01_build_eversource_geometry.py`
- `02_build_eversource_points.py`

### GUI controls

- Input folder browser
- fixed project `source` folder
- output folder browser
- project CRS
- capture year
- worker count
- product selection
- Start / Stop batch
- live processing log
- progress bars
- selected-RoW summary
- Open Deliverables / Open QA buttons

For this first GUI version, preflight automatically selects all RoWs that have a
published Polygon AOI. The resulting selected RoWs are shown in the Batch
Summary.

### Fallback

`run_eversource_console.bat` keeps the existing console workflow available.

The GUI uses only Python's standard `tkinter`; no additional venv dependency is
introduced.


## GUI fix - hidden LAStools child consoles

When the GUI is used on Windows, all LAStools child processes now run with
`CREATE_NO_WINDOW`.

This applies to per-tile and merged processing such as:

- lasboundary
- las2las
- las2txt
- lasthin

Their stdout/stderr is still captured by the Python processing engine, so GUI
progress bars and the Run Log continue to update normally. Only the separate
black CMD windows are suppressed.


## GUI child-process visibility fix v2

All Windows command-line child processes now use both:

- `CREATE_NO_WINDOW`
- `STARTUPINFO` with `STARTF_USESHOWWINDOW` and `SW_HIDE`

This is applied both to GUI stage runners and to every LAStools subprocess in
the processing core, including calls made inside parallel workers.

The GUI Run Log and progress parsing remain available because stdout/stderr are
still piped back to Python.


## GUI preflight pause - operator RoW selection

The GUI no longer proceeds automatically with every AOI-ready RoW.

Workflow:

1. Start Batch.
2. PREHEATING/preflight completes fully and writes the coverage QA.
3. Processing PAUSES.
4. A RoW selection window opens with:
   - RoW
   - State
   - MU coverage
   - AOI coverage
   - Candidate LiDAR in AOI
   - status/action
5. Only READY RoWs can be selected.
6. The operator may select individual rows, `Select all ready`, or clear the
   selection.
7. Production continues only after `Continue Batch`.

The selected rows are written back to the standard
`00_preflight_selection.json`, so Stage 01 and Stage 02 continue using the same
pipeline interfaces as before.


## GUI Pause / Resume

The GUI now has a `Pause` button during active processing.

On Windows the button suspends the complete active process tree using `psutil`,
including the current pipeline Python process and its LAStools descendants.

This is a true processing pause rather than only pausing GUI updates. CPU and
disk activity from the batch should drop while paused so the workstation can be
used for other work.

The button changes to `Resume`. Runtime shown by the GUI excludes time spent
paused.

`Stop` and closing the GUI automatically resume suspended processes first so
they can be terminated cleanly.

The RoW-selection stage is already an idle pause, so pressing Pause while the
RoW selector is open simply reports that processing is already waiting.


## GUI launcher startup check

`run_eversource.bat` now validates `tkinter` and `psutil` with the visible
`python.exe` before launching the GUI using `pythonw.exe`.

If `psutil` is missing from an older existing `.venv`, it is installed
automatically once.

If the GUI module itself cannot import, the launcher stops with a visible
message instead of silently disappearing.

`run_eversource_debug.bat` launches the same GUI using `python.exe` and keeps
the console open so the complete traceback can be read.


## RoW selector visibility fix

After PREHEATING completes the GUI now explicitly enters
`Waiting for RoW selection`.

The selection dialog is centered over the main application, raised to the
foreground and temporarily marked topmost on Windows.

A `Select RoWs` button is also enabled while the batch is waiting. If Windows
ever places the selector behind another application/window, pressing this button
raises/reopens it. No production stage can start until `Continue Batch` is
pressed in the selector.


## RoW selector rendering fix v3

The RoW selection dialog now constructs and renders its complete contents before
Windows transient/topmost/grab behaviour is applied.

If the selector throws any Tk/UI exception, the blank popup is destroyed and
the exact exception is written to the main Run Log and shown in an error dialog.


## RoW selector v4 - embedded in the main GUI

The preflight RoW selector no longer opens a separate Tk child window.

After PREHEATING, a large selection panel appears directly inside the main
application. This avoids Windows/Tk child-window rendering and focus issues.

The processing worker remains paused until the operator presses `Continue Batch`.


## RoW selector event-loop fix

`Select all ready` no longer re-enters the `<<TreeviewSelect>>` callback.

The previous implementation called `selection_set()` inside the selection event
handler even when the selection was already valid. Tk then emitted another
selection event, which could flood the GUI event loop and make Windows report
the application as `Not Responding`.

The selector now uses a re-entry guard and only rewrites the selection when a
blocked/non-ready row actually needs to be removed.
