Published builds -- nothing to install, nothing to run:

    https://johnkpan.github.io/Livermore_stormdrain_map/
    https://johnkpan.github.io/Pleasanton_stormdrain_map/


Data acquisition -- stormdrain study
====================================

Each is the Stormdrain_map/<city>/ folder of step 5, served from a gh-pages
branch. Everything below is how to rebuild either from scratch.

Two repositories, not two branches of one. GitHub Pages publishes ONE site per
repository, from a single branch and folder, and caps a published site at 1 GB.
Livermore's three corpora are 917 MB and Pleasanton's are ~890 MB, so they do
not fit together and cannot be served as two sites from here. This repo holds
the pipeline and publishes Livermore; JohnKPan/Pleasanton_stormdrain_map holds
nothing but the rendered Pleasanton deliverable on an orphan gh-pages branch.
Both are built by the same run_pipeline.sh in this repo.

Republishing a city means force-pushing a fresh orphan commit rather than
adding to history -- a second generation of ~900 MB would otherwise stay in the
repo forever. And page FILENAMES are not stable across a spacing or split-flag
change: Vallecitos Road was 4 parts at 0.1 m and is 3 at 0.15 m, so a link to
..._1_of_4.html dies. The overview is the durable entry point; deep links are
not.

Everything runs from the project root. One command runs the lot:

    bash run_pipeline.sh --city livermore                 full rebuild
    bash run_pipeline.sh --city livermore --render-only   rebuild pages only

It is a bash script, so on Windows run it from Git Bash -- `bash run_pipeline.sh`
rather than PowerShell or cmd. --city defaults to livermore; it is spelled out
above because everything derives from it, and getting it wrong silently builds
the wrong city:

    derived/<city>/   dem_<city>/   Stormdrain_map/<city>/   city_geojson/<city>.geojson

Slugs are lowercase, matching city_geojson/. `--city Pleasanton` fails on the
AOI lookup; `--city pleasanton` works.

That script is the list below, in order, with one difference: the page build is
ONE process covering every smoothing window, spread across cores. The windows
share a pass over each street -- chaining, the chainage axis and the inlet snap
do not depend on the window -- so three processes were recomputing all of it.
--jobs sets the width, defaulting to cores - 2; --no-parallel means --jobs 1.
It picks the conda env if .conda/ exists, then the venv, then whatever is on PATH.

Everything it decides for you, and how to override it:

    CITY            livermore            --city SLUG, or CITY= in the env
    smoothing       25 10 5 m            SMOOTHS= in the script
    overview opens  10 m                 OPENS_AT= in the script (must be in
                                         SMOOTHS, or the selector opens on a
                                         corpus that was never built)
    jobs            cores - 2, min 1     --jobs N, --no-parallel, JOBS= in env
    DEM collect     per-city registry    DEM_PROJECT= in the env wins; unknown
                                         cities derive one from the AOI, finest
                                         resolution, ties broken by coverage
    splitting       OFF                  --branch-split, --fold-split
    scope           full rebuild         --render-only

Everything else is derived from CITY and changes with it -- derived/<city>/,
dem_<city>/, Stormdrain_map/<city>/, city_geojson/<city>.geojson -- so the one
setting worth being deliberate about is --city.

Two of those defaults are worth a second look before a real build. The DEM
registry only knows livermore, pleasanton and san_jose; any other city derives
its collect from the AOI, and that derivation is checked against the AOI before
the download starts but is still a guess. And the splitting flags are OFF, so a
default run reproduces the divided-road folding they exist to fix -- the
published builds were both made WITH them.

Two optional flags change how a street is cut into pages. Both address the same
symptom -- a divided road chained into one profile that walks out and comes back,
so half its chainage runs backwards -- from different causes, and both are off by
default:

  --branch-split  cut at any junction of three or more segment ends. Handles a
                  divided road that CONVERGES: Stanley Boulevard's carriageways
                  meet at a Y where the median ends, so the street is one
                  connected component and the carriageway test in
                  merge_components() -- which only ever compares separate
                  components -- never sees it. Costs ~12% more pages, most of
                  them short stubs at the junctions it cuts.
  --fold-split    cut a part that doubles back where the carriageways form a
                  RING with no junction to cut at. Del Valle Parkway's are
                  joined end to end by U-turns, so every node is degree 2 and
                  --branch-split cannot reach it. Costs ~12 pages on Pleasanton.

Together on Pleasanton they take the path length sitting inside a folded part
from 447 km to 144 km, for 1,688 pages to 1,900. What remains folded is mostly
genuine loops, which score the same on the measure and are correctly one page.

Measured on Pleasanton, same machine, 18 jobs:

                        2026-08-27   2026-08-29   2026-08-29
                             0.1 m        0.1 m       0.15 m
    centerline                 50s          44s          45s
    inlets                     17s         122s           4s   (cached)
    DEM tiles                 669s           5s           4s   (cached)
    resampling                 57s          43s          46s
    elevation join             96s         100s         102s
    street pages            2,211s         425s         592s
    overview                    5s           4s           8s
    TOTAL                  51m 45s      12m 26s      13m 25s

Read those as a history, not a controlled comparison -- each column changed more
than one thing. The middle one introduced the single multi-window page build
(--min-drains 1 also skipped 4,989 part-builds carrying no inlet; per page it
went 0.74s -> 0.11s). The right one turned on --branch-split and --fold-split,
which took the corpus from 1,688 to 1,900 pages, AS WELL AS coarsening the
spacing -- so its page step is not slower than 425s because of the spacing.

Isolating the spacing, at identical flags on the same corpus:

                          0.1 m      0.15 m
    points            12,671,251   8,450,663
    parquet               461 MB      324 MB
    street pages            867s        592s
    pages per corpus      402 MB      296 MB

Livermore the same day, same flags: 11m 56s total, 7,735,119 points, 289.6 MB
parquet, 2,067 pages per corpus at 320 MB -- against 20m 24s and 438 MB at
0.1 m. Sag counts moved by single digits either way (893/1,117/1,229 at
25/10/5 m, against 890/1,115/1,228), which is the point: the smoothing window
is converted from metres using the spacing measured from the data, so the
corpora stay what they claim to be at any spacing.

The steps themselves, should you want to run them by hand:

    .venv/Scripts/python.exe fetch_city_boundaries.py --buffer-miles 2
    .venv/Scripts/python.exe fetch_overture_streets.py --cities livermore --roads-only
    .venv/Scripts/python.exe fetch_inlets.py --all
    .venv/Scripts/python.exe fetch_usgs_lidar.py \
        --aoi-file city_geojson/livermore.geojson \
        --project CA_AlamedaCounty_2021_B21 --out ./dem_livermore \
        --manifest livermore_tiles.csv
    .venv/Scripts/python.exe extract_centerline_latlon.py --slim --parquet --no-csv \
        --city livermore
    .venv/Scripts/python.exe add_elevation.py --no-csv --city livermore \
        --dem-dir dem_livermore
    .venv/Scripts/python.exe plot_street_bokeh.py --all --city livermore \
        --jobs 18 --branch-split --fold-split \
        --smooth 25 10 5 \
        --outdir Stormdrain_map/livermore/streets_25m \
                 Stormdrain_map/livermore/streets_10m \
                 Stormdrain_map/livermore/streets_5m
    .venv/Scripts/python.exe plot_city_overview.py --city livermore \
        --pages Stormdrain_map/livermore/streets_25m --label "25 m" \
        --pages-alt Stormdrain_map/livermore/streets_10m --alt-label "10 m" \
        --pages-alt Stormdrain_map/livermore/streets_5m --alt-label "5 m" \
        --opens-at "10 m" --out Stormdrain_map/livermore/index.html

The first four pull every external input; the rest derive everything else from
them. Nothing has to be downloaded by hand. Sections below document each step,
then the optional static renders.

--smooth takes several windows and --outdir one directory each. All three
corpora are kept, and the overview's selector switches between them, opening at
10 m -- see section 5.

(Plain `python fetch_inlets.py --all` works too if the venv is activated -- the
explicit interpreter path just avoids depending on that.)

Order matters in exactly two places: add_elevation.py needs the parquet that
extract_centerline_latlon.py writes, and plot_city_overview.py needs the
_index.csv the page build writes -- one per smoothing window, all three before
the overview.

All four fetches are safe to re-run. Boundaries, centerline and inlets overwrite
their outputs; fetch_usgs_lidar.py skips tiles already fully downloaded, resumes
partial ones, and re-merges only what changed. run_pipeline.sh runs the boundary
fetch only when city_geojson/<city>.geojson is missing, since one TIGER fetch
writes all 101 cities at once.


0. Street centerline
--------------------

    .venv/Scripts/python.exe fetch_overture_streets.py --cities livermore --roads-only

Source: Overture Maps transportation theme. One source for all 101 cities,
        selected by --cities against city_geojson/.

Writes: streets/overture/<city>.geojson
        streets/overture/_index.csv      per-city totals
        streets/overture/_classes.csv    per-city, per-road-class totals

Both index files ACCUMULATE across runs. --cities fetches one city at a time,
but the two CSVs describe the corpus, so each run merges its rows into what is
there rather than replacing it -- a Pleasanton fetch used to erase Livermore's
row. Rows whose .geojson has since been deleted are dropped.

--roads-only keeps the eight classified road classes plus service/alley, and
drops `link`. A link is usually a ramp, and a ramp is not a street -- but that
was measured on MOTORWAY links and does not generalise. Across Livermore's 171
links: motorway 0 of 96 named, trunk 4 of 18, primary 2 of 18, tertiary 1 of 9,
secondary 1 of 30. The named ones are not ramps at all; they carry a road
THROUGH a grade-separated interchange, and Overture tags them `link` for the
geometry rather than the identity. Dropping them severed Vallecitos Road at the
Isabel Avenue interchange -- it rendered as two blue lines with a hole between
them -- and did the same to Railroad Avenue, Valley Avenue, Santa Rita Road and
Olivina Avenue. So a link is kept when it has a display_name: 8 such in
Livermore, 16 in Pleasanton, zero motorway ramps in either. The original
justification is untouched, since every segment this adds back has a name by
construction.

Livermore's own portal layer (Street_Centerline_-_Public, 4,867 features) was
the baseline the Overture corpus was checked against -- same OBJECTID set, zero
differing property values, coordinates identical to the last decimal. It is no
longer a pipeline step: one fetch that works everywhere beat two that disagree
about which city they serve. fetch_livermore_street_centerlines.py is
DEPRECATED: kept for that comparison, called by nothing, and imported by
nothing since extract_centerline_latlon.py took over the SRC path it exported.
It is also the only schema with no road_flags, so a corpus built from it cannot
mark bridges (section 5).

fetch_livermore_inlets.py is DEPRECATED the same way -- `fetch_inlets.py
livermore` fetches the same layer with domain decoding, retry backoff and a
`source` column.


1. Storm drain inlets
---------------------

    .venv/Scripts/python.exe fetch_inlets.py --all

Source: one vetted ArcGIS layer per city, see fetch_inlets.py --list.
        livermore   gisweb.cityoflivermore.net  ...StormStructures/FeatureServer/2
        san_jose    geo.sanjoseca.gov           ...OPN_OpenDataService/MapServer/295
        pleasanton  gisdata.cityofpleasantonca.gov  ...UtStormDrain/MapServer/1

Writes: derived/storm_inlets_all.csv   (lon, lat, source + 8 canonical + extras)
        derived/storm_inlets_all.geojson

Each publisher's fields are mapped onto one canonical schema -- Livermore's
names, because the plotters already read them -- and every row carries a
`source` column. A single city still writes its own file: `fetch_inlets.py
pleasanton` -> derived/storm_inlets_pleasanton.csv.

The plotters clip this corpus to city_geojson/<city>.geojson at load, so one
fetch serves any --city. That polygon is buffered 2 miles, so the clip keeps a
neighbouring city's inlets where they fall inside it -- deliberate, since those
drain into the city, but it means the clip is not the same as the city limit.
Pass --no-aoi to skip it, or --aoi with your own polygon.

Run of 2026-08-28: 51,334 inlets over 37 columns -- livermore 6,987,
san_jose 36,094, pleasanton 8,253. For Livermore alone: all
OperationalStatus=Active, no missing geometry, 4,488 carry TopOfGrate,
4,899 carry InvertElevation1, types 4,912 Curb Inlet / 2,070 Grated Inlet /
5 other.

DATUM -- important. TopOfGrate and InvertElevation1 are in FEET on NGVD29,
while the DEM is metres on NAVD88. plot_street_drains.py applies a measured
-0.794 m correction (DATUM_SHIFT_M, see that module's docstring) so the two
sources sit on one datum. Pass --no-datum-shift to see raw values.


2. Lidar DEM tiles
------------------

    .venv/Scripts/python.exe fetch_usgs_lidar.py \
        --aoi-file city_geojson/livermore.geojson \
        --project CA_AlamedaCounty_2021_B21 --out ./dem_livermore \
        --manifest livermore_tiles.csv

Source: USGS 3DEP OPR (Original Product Resolution) DEM, via the National Map
        (TNM) Access API
        https://tnmaccess.nationalmap.gov/api/v1/products

Writes: dem_livermore/*.tif             whole tiles, the only thing to read
        dem_livermore/_fragments/*.tif  staged multi-block pieces, see below
        dem_livermore/_mosaic.vrt       rebuilt per run by add_elevation.py
        livermore_tiles.csv             manifest of every matching file

OPR is the collection's NATIVE grid, not the resampled national product. For
CA_AlamedaCounty_2021_B21 that is 1 US survey foot (0.3048 m) in EPSG:6420
(NAD83(2011) / California zone 3, ftUS) on a NAVD88 ftUS vertical datum -- about
3.3x finer per axis than the 1 m DEM this project used until 2026-08-25.

fetch_dem.py, which pulls the 1 m product into dem/, is kept but is no longer
in the pipeline. The two agree closely: sampled at 59,472 points, the 1 ft
product sits 0.19 cm above the 1 m one with 1.05 cm RMS, which is what you would
expect of two derivatives of the same collect.

--aoi-file takes the bbox from the buffered city polygon, which is the same AOI
the plotters clip inlets to, so the DEM cannot end up narrower than the streets
it has to cover. Reading it needs geopandas -- see requirements.txt.

There is also a named --aoi livermore preset, no longer used by the pipeline. It
was widened on 2026-08-25 from (-121.82, 37.63, -121.68, 37.73) to
(-121.86, 37.62, -121.68, 37.74): the first box stopped short of the western end
of the street network and left 4,352 centerline points with no elevation. That
class of mistake is what --aoi-file removes.

MULTI-BLOCK TILES. This collect ships in two delivery blocks, CA_AlamedaCo_1_2021
and CA_AlamedaCo_3_2021. Where a block boundary crosses a tile, USGS publishes
that tile once per block -- same filename, same footprint, different URL -- each
clipped to its own block with the rest NoData. They are a partition, not
duplicates and not overlapping flight lines: they agree exactly on the hairline
seam where they meet, and together reconstruct the tile with no gap.

Downloading both to the basename would race and leave one arbitrary fragment
behind, so colliding tiles are staged under _fragments/ and merged back into the
plain filename. dem_livermore/*.tif is therefore always whole tiles. Fragments
are kept so a re-run is a no-op rather than a re-download; --prune-fragments
reclaims the space once you are done.

ANOTHER AOI. Nothing here is Livermore-specific. Pick a box, see what covers
it, pin ONE collect, and point the pipeline at the result:

    python fetch_usgs_lidar.py --bbox -122.53 37.70 -122.35 37.83 --list-projects
    python fetch_usgs_lidar.py --bbox ... --project CA_SanFrancisco_B23 --out ./dem_sf
    python add_elevation.py --dem-dir ./dem_sf

The VRT mosaic is regenerated from whatever tiles are in the directory, every
run -- there is nothing to hand-edit per AOI. --project matters though: every
Bay Area box is covered by several collects at different resolutions and years
(SF has 5, Contra Costa 7), and add_elevation.py refuses a directory holding
more than one grid rather than blending acquisition dates and vertical
references into a single surface. The refusal names the grids it found.

MIXED RESOLUTION. If one collect does not cover the AOI, add_elevation.py can
mosaic collects of DIFFERENT resolutions. Each VRT source declares its own
source rectangle and the destination rectangle it covers, so GDAL resamples it
onto a common grid on read:

    --vrt-resolution highest   target the finest source (default), or
                     lowest    the coarsest, or a number in CRS units
    --vrt-resampling bilinear  nearest | bilinear (default) | cubic | average

Two things to know before relying on it.

First, provenance. Every point gets dem_tile (which source tile) and dem_res
(that tile's NATIVE resolution). Where dem_res is larger than mosaic_res in the
meta.json, that sample sits on UPSAMPLED data -- the number is real but the
detail is not there. Collects also differ in acquisition date and vertical
reference, so a sag sitting on a boundary between two of them may be the seam
rather than the ground. Check dem_tile before believing one.

Second, seam accuracy. VRT sources are resampled independently and then
composited, so at the outermost pixel of a rescaled source there is no
neighbour to interpolate against and the value clamps. Measured on a synthetic
ramp: exactly 2 pixels of 600 are affected -- the source's first and last -- by
up to half a source pixel; the interior is exact. Negligible for a 3 ft seam,
worth knowing if you mosaic many small tiles.

MIXED PROJECTIONS need the conda-forge environment. Bay Area collects do not
share a CRS -- CA_SanFrancisco_B23 is San Francisco CS13 at 0.25 m,
CA_ContraCosta_B22 is UTM 10N at 0.5 m, CA_AlamedaCounty_2021_B21 and
CA_SantaClaraCounty_2020_A20 are California zone 3 at 1 ftUS. A VRT cannot
reproject and neither can gdalbuildvrt, so that path uses gdal.Warp, and GDAL
has no Windows wheel on PyPI at any version. See environment.yml:

    .conda/micromamba.exe create -y -p .conda/env -f environment.yml
    bash run_pipeline.sh --city livermore    # picks the env up automatically

Everything else still runs in the uv venv exactly as before -- the same-CRS VRT
is written by rasterio alone and is byte-for-byte what it always was. Only a
cross-projection mosaic needs osgeo, and the error says so if it is missing.

How the cross-CRS mosaic is built, and why it is three stages rather than one:
gdal.Warp(format="VRT") honours only its FIRST source dataset -- it warns about
this and returns a mosaic that is otherwise NoData. GDAL's own remedy is to
mosaic same-projection sources first and warp the result. So:

    tiles -> one plain VRT per (CRS, resolution) group     [rasterio]
          -> gdal.Warp each group onto the target grid     [osgeo, 1 source]
          -> one plain VRT over the warped groups          [rasterio]

Grouping on (CRS, resolution) rather than CRS alone keeps each group internally
uniform, so ordering groups coarsest-first reproduces the per-tile "finest wins"
rule exactly. targetAlignedPixels snaps every group to the same whole-pixel
grid, so they compose without sub-pixel registration drift.

PREFER HIGHEST RESOLUTION is the overlap rule throughout. Where collects
overlap, the finest-resolution tile wins; the target grid takes the finest
source's CRS and pixel size. Resolutions are compared in METRES, which matters:
a 1 ftUS tile is 0.3048 m and is FINER than a 0.5 m one despite the larger
number, so ranking on the raw CRS value would order them backwards. dem_res is
recorded in metres for the same reason.

Useful flags:
    --list-projects                which collects cover the AOI, with sizes
    --dry-run                      list tiles and total size, download nothing
    --bbox -121.9 37.6 -121.6 37.8 override the AOI
    --dataset 1m                   the resampled 1 m product instead of OPR
    --prune-fragments              delete _fragments/ after a clean merge

Run of 2026-08-25: 296 tiles, 6.2 GB, plus 144 staged fragments (1.5 GB).
72 tiles arrived split across the two delivery blocks and were merged, each
back to 100% valid.


3. Centerline resampling
------------------------

    .venv/Scripts/python.exe extract_centerline_latlon.py --slim --parquet \
        --no-csv --city livermore --src streets/overture/livermore.geojson

Source: streets/overture/<city>.geojson (step 0; --src overrides)

Writes, all under derived/<city>/:

        segments_endpoints.csv       one row per centerline feature
        segments_vertices.csv        one row per native shape vertex
        segments_points_0p15m.csv     resampled points (--no-csv removes)
        segments_points_0p15m.parquet same, ~4x smaller

--spacing defaults to 0.15 metre (SPACING in that module), which is what the
rest of the pipeline expects. The interval is in the filename, so corpora at
different spacings sit side by side instead of overwriting each other, and
points_path() is the single place that name is built -- every consumer imports
it rather than hardcoding a path. run_pipeline.sh is the one exception: it names
the parquet as a shell literal, so POINTS there has to change in step with
SPACING or --render-only will look for a file the build no longer writes. Pass
--spacing 1 for a coarser pass.

0.15 m is deliberately ~2x finer than the 1 ft (0.3048 m) DEM grid it will be
sampled against. That still oversamples: neighbouring points are correlated and
carry no independent elevation information. The reason to do it is horizontal --
placing points precisely along the centerline -- not vertical.

It was 0.1 m until 2026-08-29, which oversampled ~3x. The measurement that
argued for coarsening: at 0.1 m, 62.9% of Pleasanton's points shared a DEM cell
with the point before them (same_cell_as_prev), so nearly two thirds carried no
new elevation at all. 0.15 m costs two thirds of the points, two thirds of the
parquet, and two thirds of the per-point work in the page build -- Livermore's
full rebuild went 20m 24s to 11m 56s and its pages 438 MB to 320 MB per corpus.
Nothing real is lost until the spacing passes 0.3048 m, one DEM cell.

Both flags matter and neither is the default:

  --slim     keep only OBJECTID + FullStreetName per point. add_elevation.py
             joins FunctionalClass/RoadType back on OBJECTID from the endpoints
             file, so carrying them per point would just duplicate them 667k
             times.
  --parquet  add_elevation.py reads the parquet, not the csv. Without this the
             next step has nothing to open.
  --no-csv   delete the points csv once --parquet has converted it. At 0.15 m
             that csv is ~430 MB and nothing downstream reads it. Requires
             --parquet.

Do NOT pass --points-only: add_elevation.py needs segments_endpoints.csv for
that attribute join.

Run of 2026-08-29, Livermore: 7,735,119 points at 0.15 m from 8,215 Overture
centerline features. 97.9 MB parquet vs 431.0 MB csv, growing to 289.6 MB once
add_elevation.py adds its columns. Pleasanton the same day: 8,450,663 points,
112.4 MB -> 324.1 MB.

(The 2026-08-25 Livermore figure, for comparison, was 6,630,822 points at 0.1 m
from 4,867 portal centerline features. Both the spacing and the centerline
source changed since, so it is not a like-for-like row.)


4. Elevation join
-----------------

    .venv/Scripts/python.exe add_elevation.py --no-csv --city livermore \
        --dem-dir dem_livermore

Reads : derived/<city>/segments_points_0p15m.parquet, dem_<city>/*.tif,
        derived/<city>/segments_endpoints.csv
Writes: derived/<city>/segments_points_0p15m.parquet  (in place, 6 cols -> 21)
        derived/<city>/segments_points_0p15m.meta.json
        dem_<city>/_mosaic.vrt                       rebuilt every run

Adds easting/northing (UTM 10N metres -- the frame every plot works in),
dem_x/dem_y (the same point in the DEM's EPSG:6420 ftUS), the containing 1 ftUS
cell, elev_m (bilinear -- the better estimator), elev_cell_m (raw nearest cell),
elev_disc_cm (their disagreement; >20 suggests a curb, wall or bridge edge),
bearing_deg, and FunctionalClass/RoadType joined on OBJECTID.

Elevations are converted from the DEM's US survey feet to METRES on read, so
every column here -- and everything downstream -- stays metric and on NAVD88,
exactly as under the old 1 m product. Nothing else in the pipeline had to learn
that the DEM changed units.

Two properties of the OPR tiles are handled here:

  * They do not overlap; they butt up exactly on a 3000 ft grid. A point within
    one pixel of a tile edge therefore has no bilinear neighbourhood inside its
    own tile. Sampling goes through a VRT mosaic (hand-written, since there is
    no osgeo binding in the venv) so those neighbours come from the adjoining
    tile -- the job the 1 m product's 12 m tile overlap did for free.

  * AREA_OR_POINT=Point rather than Area. This does NOT move the sample
    location: GDAL's geotransform is corner-based either way, and the flag only
    records that the value is a point measurement rather than a cell average.
    The half-pixel shift to cell centres is right for both. Checked against the
    1 m product at 59,472 points -- centre 1.05 cm RMS, node 1.26 cm.

--no-csv skips rewriting the csv alongside the parquet and is much faster.
Nothing downstream reads that csv; plot_points_map.py now reads the parquet.

The meta.json it writes records the DEM, datum, point spacing and measured
accuracy -- read it before trusting a gradient. Short version: compute grades
over >=25 m baselines. Adjacent deltas are noise-dominated, and at 0.1 m
spacing on a 0.3 m grid neighbouring samples are correlated as well.

Run of 2026-08-25: 6,630,822 points sampled across 112 tiles, no NaN
elevations, range 109.12..240.71 m -- the same range the 1 m product gave.
242.7 MB parquet.

elev_disc_cm is much tighter on the finer grid: median 0.24 cm and p99 2.07 cm,
against 1.5 cm / 8.2 cm on the 1 m product. The >20 cm flag still means what it
did, though -- above about 8 cm the two grids flag near-identical counts
(1,610 vs 1,593 points), because up there it is real curbs and walls rather
than the grid's own noise floor. So the threshold did not need rescaling.


5. Render
---------

The deliverable -- interactive, one page per street plus a city-wide picker:

    .venv/Scripts/python.exe plot_street_bokeh.py --all --city livermore \
        --smooth 25 --outdir Stormdrain_map/livermore/streets_25m

    (and again at --smooth 10 and --smooth 5, then plot_city_overview.py --city
     livermore over all three -- the full commands are in the list at the top)

Writes, all under Stormdrain_map/<city>/:

        index.html                     city-wide picker -- OPEN THIS ONE
        streets_25m/<STREET>.html      linked profile + plan view, one per
                                       street, at 25 m smoothing
        streets_25m/_index.csv         per-street inlet and sag counts
        streets_10m/...  streets_5m/   the same, at 10 m and 5 m

Run of 2026-08-28 for Pleasanton: 2,995 pages per corpus, 609 MB at 5 m, and a
7.4 MB overview over 8,526 segments. 1,693 pages carry inlets; the rest are
profile-only, which the buffered AOI makes expected -- it reaches past the city
limit and picks up streets the inlet layer does not cover.

Stormdrain_map/ is the whole deliverable: self-contained, and the only thing
outside derived/. Zip that one folder to hand the study to someone.

plot_city_overview.py must run second: it reads _index.csv for the per-street
stats and for the filename each street maps to (those names come from a
collision-breaking counter and cannot be recomputed independently).

The overview does not sit beside the pages it opens, so its iframe links carry
a "streets/" prefix. That prefix is computed from --out and --pages rather than
hardcoded, so either can move -- put both in one directory and it disappears.
Change --outdir on plot_street_bokeh.py and --pages (or --pages-alt) here
together, or the overview will point at pages that are not there.

The iframe is sized to the street page's own height: FRAME_H in
plot_city_overview.py against PROF_H + MAP_H in plot_street_bokeh.py. Raise the
elevation profile there and FRAME_H has to follow, or the embedded page gains an
inner scrollbar. Width is the tighter constraint and runs the other way --
PANEL_W must stay under FRAME_W, or the iframe scrolls sideways -- which is why
the profile was made taller rather than wider.

The overview carries two colourings, switched by the radio button beside its
search box:

  road class       the default. Colour is FunctionalClass, width follows it too.
  sags per street  the street's n_sags from _index.csv, binned 0 / 1 / 2 / 3-4 /
                   5-9 / 10+ on a grey-to-dark-red ramp. Both colourings are
                   always built; --color-by sags only picks which one the page
                   opens on.

Legend entries hide their streets in either colouring -- hiding "no sag" leaves
just the streets that have one (356 at 25 m, 464 at 10 m), which is the view
worth bookmarking. The switch resets whatever the other legend had hidden, since
the legends filter on different keys and cannot be kept in step; so does the
smoothing selector, for the same reason. Tap, hover, search and the #street
bookmark work the same in both, and so does the tapped-street highlight: one
blue line in either colouring. Blue rather than the obvious red because red
vanishes into the top of the sag ramp, which is itself red. It does sit near
Major Collector's blue under the road-class colouring, but at 5 px against 2 it
is the widest and brightest line on the map.

The #street bookmark holds the street name only, not the smoothing -- a
bookmarked link reopens at whatever --opens-at the overview was built with
(10 m from run_pipeline.sh), whichever window was selected when it was made.

Note what the sag colouring is and is not: it is a count per street, painted
along the whole street, so ISABEL AV reads dark red over all 15 km for 11 sags
that sit at 11 points. Long streets collect more sags -- take the colour as
"how much of this street's drainage is worth reading", not as a density. The
legend counts streets; the road-class legend counts centerline segments.

Smoothing -- 25 m, 10 m or 5 m, and the selector between them
-------------------------------------------------------------

--smooth is the rolling-mean window over the elevation samples, in METRES.
It lives in plot_street_profiles.py (SMOOTH_M = 25.0, the default) and
plot_street_drains.py and plot_street_bokeh.py take the same flag, because all
three build their profile through that module's build_profile(). It is the one
knob they share; changing SMOOTH_M moves all of them.

The window is converted from metres to samples using the spacing MEASURED from
the data (sample_step in that module). It used to be converted 1:1, which
silently assumed 1 m spacing -- at the current 0.1 m that would have turned
--smooth 25 into a 2.5 m window and moved every sag count with it.

It is not cosmetic. Sags are found ON the smoothed profile -- unsmoothed, the
~2 cm DEM noise manufactures minima everywhere -- so the window decides how
shallow a dip still counts, and the two builds genuinely disagree:

                        25 m          10 m
    sags                 597           823
    unserved sags        168           246
    streets with a sag   356           464

Re-measured on 2026-08-26 against the 1 ft DEM at 0.1 m spacing, 25 m window
(the 25 m corpus at 0.1 m spacing):

                    1 m DEM / 1 m    1 ft DEM / 0.1 m
    sags                 597               599
    unserved sags        168               168
    streets with a sag   356               357

So the DEM change moved the sag count by TWO, out of 599, and left the unserved
count untouched. The smoothing window moved it by 226. That is the useful
result: what counts as a sag is set almost entirely by the 25 m rolling mean,
not by the resolution underneath it -- a 25 m average over 250 samples of a
0.3 m grid lands in the same place as a 25 m average over 25 samples of a 1 m
grid. The finer DEM buys horizontal precision and a tighter elev_disc_cm, not
different sags.

176 streets differ, and never in the other direction: a shorter window only ever
keeps more dips, because a longer one averages them away. Neither is the right
answer. 25 m is the baseline the DEM's own accuracy note argues for (see
derived/<city>/segments_points_0p15m.meta.json -- adjacent deltas are noise); 10 m
catches real but shallow ponding that 25 m flattens, at the cost of promoting
some noise; 5 m keeps more of both again. The selector exists so they can be
read against each other rather than one being picked blind. The pipeline builds
all three and opens at 10 m.

Pleasanton, 2026-08-28, shows the same monotone shape at city scale:

                    25 m     10 m      5 m
    sags           1,606    1,996    2,195
    unserved sags    801      989    1,099

Choosing a window, between the map and the iframe, switches at once: the page in
the iframe, the sag counts in the hover tooltips and the picked-street line, and
the sag colouring with its own legend. The road-class colouring does not move --
it has nothing to do with elevation. The view does not re-zoom, so a street
stays where it is while the smoothing changes underneath it.

The selector appears only when --pages-alt is given; with none the overview
behaves as it always did, over one corpus. Every corpus must cover the same
streets -- a street missing from any of them is dropped, with a warning, since
switching would otherwise break on a street that exists in only some.

Pages load BokehJS from a CDN, so viewing needs a network connection, but they
work opened straight from disk -- no local server needed.

Add --sv-key <KEY> to plot_street_bokeh.py --all to embed a live Street View
pane beside each street's map instead of a link out. Needs a Google Maps Embed
API key (that API is free and unmetered). The key is written into every page,
so restrict it to the Maps Embed API in the Cloud console -- HTTP referrer
restrictions cannot cover file:// pages. Changing the key means a full rebuild.

Run of 2026-08-12, via run_pipeline.sh --render-only: 1,728 pages per corpus,
88 MB each, 6m 03s for both --all runs in parallel and 4s for the overview on
top, which lands at 4.2 MB. 1,404 streets carry inlets. 597 sags at 25 m, of
which 168 have no inlet serving them; 823 and 246 at 10 m.

(The earlier run of 2026-08-09 reported 568 sags and 158 unserved at 25 m. The
difference is not the smoothing: it is the chaining fix that starts each street
at a real terminal rather than the bbox corner, which changed some streets'
paths and therefore their profiles.)


Static renders -- optional, none of the above depends on them:

    .venv/Scripts/python.exe plot_street_drains.py --all    -> derived/drains/
    .venv/Scripts/python.exe plot_street_profiles.py        -> derived/profiles/
    .venv/Scripts/python.exe plot_points_map.py --every 100 -> derived/map_points_0p15m_every100.html
    .venv/Scripts/python.exe preview_dem.py                 -> derived/dem_*.png
    .venv/Scripts/python.exe map_street_cells.py --street "AIRWAY BL"

plot_street_drains.py --all is the PNG twin of the interactive pages -- same
analysis, same numbers, ~580 MB of images. --unserved-only narrows it to the
streets with an unserved sag, which is usually what is actually wanted;
--outdir redirects the output; --min-drains skips sparse streets.

plot_points_map.py wants --every at the 0.1 m default: 6.6 M markers is far
more than a browser will draw comfortably. --every 100 gives ~66k, which is
about what the old 1 m corpus plotted whole.

preview_dem.py and map_street_cells.py are DEM sanity checks rather than
deliverables: hillshaded mosaic overviews, and one street drawn against the
native 1 ft grid. map_street_cells.py is the one plot that works in the DEM's
own CRS (EPSG:6420, ftUS) rather than UTM -- the grid is only square and
whole-numbered there.
