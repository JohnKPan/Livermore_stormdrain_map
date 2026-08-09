Data acquisition -- Livermore stormdrain study
==============================================

Everything runs from the project root. The whole pipeline, in order:

    .venv/Scripts/python.exe fetch_livermore_street_centerlines.py
    .venv/Scripts/python.exe fetch_inlets.py
    .venv/Scripts/python.exe fetch_dem.py
    .venv/Scripts/python.exe extract_centerline_latlon.py --slim --parquet
    .venv/Scripts/python.exe add_elevation.py --no-csv
    .venv/Scripts/python.exe plot_street_bokeh.py --all
    .venv/Scripts/python.exe plot_city_overview.py

The first three pull every external input; the rest derive everything else from
them. Nothing has to be downloaded by hand. Sections below document each step,
then the optional static renders.

(Plain `python fetch_inlets.py` works too if the venv is activated -- the
explicit interpreter path just avoids depending on that.)

Order matters in exactly two places: add_elevation.py needs the parquet that
extract_centerline_latlon.py writes, and plot_city_overview.py needs the
_index.csv that plot_street_bokeh.py --all writes.

All three fetches are safe to re-run. The centerline and inlet fetches
overwrite their outputs; fetch_dem.py skips tiles already fully downloaded and
resumes partial ones.


0. Street centerline
--------------------

    .venv/Scripts/python.exe fetch_livermore_street_centerlines.py

Source: City of Livermore open GIS portal, dataset 08e97ae0e0ee43cea3b376ef0bbc9884_1
        https://gisopendata.livermoreca.gov/datasets/08e97ae0e0ee43cea3b376ef0bbc9884_1
        which resolves to layer 1 of
        https://services7.arcgis.com/BJisQXdgVScP0JMy/arcgis/rest/services
            /Street_Centerline_-_Public/FeatureServer/1

Writes: streets/Street_Centerline_-_Public.geojson

The portal's export carried a long numeric suffix; nothing depended on it,
so it is dropped. extract_centerline_latlon.py defaults to this same path
and takes --src to override; fetch takes --out. Change both to move it.

Paged 2,000 at a time (the layer's maxRecordCount). The service speaks GeoJSON
natively, so f=geojson with outSR=4326 gives the file directly -- its own
storage is EPSG:6420.

The nine attribute fields it returns are exactly the ATTRS list in
extract_centerline_latlon.py. That script skips any feature whose geometry is
not a LineString, so a multi-part polyline would vanish without an error; the
fetch counts geometry types and warns if any appear.

Run of 2026-08-09: 4,867 features, 44,230 vertices, all LineString, 3.4 MB.
Verified against the portal export it replaces: same OBJECTID set, zero
differing property values, and coordinates identical to the last decimal.


1. Storm drain inlets
---------------------

    .venv/Scripts/python.exe fetch_inlets.py

Source: City of Livermore public ArcGIS FeatureServer, layer 2 (Inlet)
        https://gisweb.cityoflivermore.net/arcgis/rest/services
            /WetUtilities/StormStructures/FeatureServer/2/query

Writes: derived/storm_inlets.csv       (lon, lat + 15 asset fields)
        derived/storm_inlets.geojson

Paged 2,000 records at a time until the server stops setting
exceededTransferLimit; coordinates requested in WGS84 (outSR=4326) to match
the centerline data.

Run of 2026-08-09: 6,987 inlets, all OperationalStatus=Active, no missing
geometry. 4,488 carry TopOfGrate, 4,899 carry InvertElevation1. Types are
4,912 Curb Inlet / 2,070 Grated Inlet / 5 other.

DATUM -- important. TopOfGrate and InvertElevation1 are in FEET on NGVD29,
while the DEM is metres on NAVD88. plot_street_drains.py applies a measured
-0.794 m correction (DATUM_SHIFT_M, see that module's docstring) so the two
sources sit on one datum. Pass --no-datum-shift to see raw values.


2. Lidar DEM tiles
------------------

    .venv/Scripts/python.exe fetch_dem.py

Source: USGS 3DEP 1 m DEM, via the National Map (TNM) Access API
        https://tnmaccess.nationalmap.gov/api/v1/products

Writes: dem/*.tif

The bounding box defaults to the extent of the centerline GeoJSON plus a
buffer, so the tiles always follow the data. --project pins one lidar
acquisition (default CA_AlamedaCounty_2021_B21) so tiles share a date and
vertical reference instead of mixing flights -- 6 products intersect this
bbox but only 4 belong to that project.

Useful flags:
    --dry-run                      list tiles and total size, download nothing
    --bbox -121.9 37.6 -121.6 37.8 override the extent
    --project ""                   no project filter (may mix acquisitions)

Run of 2026-08-09: 4 tiles, 1.35 GB.
    USGS_1M_10_x60y417_CA_AlamedaCounty_2021_B21.tif   351.6 MB
    USGS_1M_10_x60y418_CA_AlamedaCounty_2021_B21.tif   324.5 MB
    USGS_1M_10_x61y417_CA_AlamedaCounty_2021_B21.tif   338.2 MB
    USGS_1M_10_x61y418_CA_AlamedaCounty_2021_B21.tif   332.7 MB


3. Centerline resampling
------------------------

    .venv/Scripts/python.exe extract_centerline_latlon.py --slim --parquet

Source: streets/Street_Centerline_-_Public.geojson (step 0; --src overrides)

Writes: derived/segments_endpoints.csv     one row per centerline feature
        derived/segments_vertices.csv      one row per native shape vertex
        derived/segments_points_1m.csv     resampled points
        derived/segments_points_1m.parquet same, ~4x smaller

--spacing now defaults to 1 metre, so the bare command gives the 1 m file the
rest of the pipeline wants. Pass --spacing 10 for a coarser pass.

Both flags matter and neither is the default:

  --slim     keep only OBJECTID + FullStreetName per point. add_elevation.py
             joins FunctionalClass/RoadType back on OBJECTID from the endpoints
             file, so carrying them per point would just duplicate them 667k
             times.
  --parquet  add_elevation.py reads the parquet, not the csv. Without this the
             next step has nothing to open.

Do NOT pass --points-only: add_elevation.py needs segments_endpoints.csv for
that attribute join.

Run of 2026-08-09: 667,425 points at 1 m, from 4,867 centerline features
(44,230 native vertices). 9.6 MB parquet vs 35.4 MB csv -- both grow once
add_elevation.py adds its 13 columns.


4. Elevation join
-----------------

    .venv/Scripts/python.exe add_elevation.py --no-csv

Reads : derived/segments_points_1m.parquet, dem/*.tif,
        derived/segments_endpoints.csv
Writes: derived/segments_points_1m.parquet   (in place, 6 columns -> 19)
        derived/segments_points_1m.meta.json

Adds easting/northing (UTM 10N, the DEM's own CRS), the containing 1 m cell,
elev_m (bilinear -- the better estimator), elev_cell_m (raw nearest cell),
elev_disc_cm (their disagreement; >20 suggests a curb, wall or bridge edge),
bearing_deg, and FunctionalClass/RoadType joined on OBJECTID.

--no-csv skips rewriting the 129 MB csv alongside the parquet and is much
faster. Nothing downstream reads that csv except plot_points_map.py, which only
wants lat/lon and is fine with the pre-elevation version left by step 3.

The meta.json it writes records the DEM tiles, datum, and measured accuracy --
read it before trusting a gradient. Short version: compute grades over >=25 m
baselines, because adjacent 1 m deltas are noise-dominated.

Run of 2026-08-09: 667,425 points sampled across all 4 tiles, no NaN
elevations, range 109.12..240.71 m. 23.7 MB parquet.


5. Render
---------

The deliverable -- interactive, one page per street plus a city-wide picker:

    .venv/Scripts/python.exe plot_street_bokeh.py --all
    .venv/Scripts/python.exe plot_city_overview.py

Writes: Stormdrain_map/index.html            city-wide picker -- OPEN THIS ONE
        Stormdrain_map/streets/<STREET>.html linked profile + plan view, one
                                             per street
        Stormdrain_map/streets/_index.csv    per-street inlet and sag counts

Stormdrain_map/ is the whole deliverable: self-contained, and the only thing
outside derived/. Zip that one folder to hand the study to someone.

plot_city_overview.py must run second: it reads _index.csv for the per-street
stats and for the filename each street maps to (those names come from a
collision-breaking counter and cannot be recomputed independently).

The overview does not sit beside the pages it opens, so its iframe links carry
a "streets/" prefix. That prefix is computed from --out and --pages rather than
hardcoded, so either can move -- put both in one directory and it disappears.
Change --outdir on plot_street_bokeh.py and --pages here together, or the
overview will point at pages that are not there.

Pages load BokehJS from a CDN, so viewing needs a network connection, but they
work opened straight from disk -- no local server needed.

Add --sv-key <KEY> to plot_street_bokeh.py --all to embed a live Street View
pane beside each street's map instead of a link out. Needs a Google Maps Embed
API key (that API is free and unmetered). The key is written into every page,
so restrict it to the Maps Embed API in the Cloud console -- HTTP referrer
restrictions cannot cover file:// pages. Changing the key means a full rebuild.

Run of 2026-08-09: 1,728 pages, 91.4 MB, about 10 minutes for --all; the
overview is seconds on top. 1,404 streets carry inlets; 568 sags found, of
which 158 have no inlet serving them.


Static renders -- optional, none of the above depends on them:

    .venv/Scripts/python.exe plot_street_drains.py --all    -> derived/drains/
    .venv/Scripts/python.exe plot_street_profiles.py        -> derived/profiles/
    .venv/Scripts/python.exe plot_points_map.py             -> derived/map_points_1m.html
    .venv/Scripts/python.exe preview_dem.py                 -> derived/dem_*.png
    .venv/Scripts/python.exe map_street_cells.py --street "AIRWAY BL"

plot_street_drains.py --all is the PNG twin of the interactive pages -- same
analysis, same numbers, ~580 MB of images. --unserved-only narrows it to the
streets with an unserved sag, which is usually what is actually wanted;
--outdir redirects the output; --min-drains skips sparse streets.

preview_dem.py and map_street_cells.py are DEM sanity checks rather than
deliverables: hillshaded tile overviews, and one street drawn against the
native 1 m grid.
