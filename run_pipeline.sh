#!/usr/bin/env bash
#
# Rebuild the Livermore stormdrain study end to end.
#
# This is the command list from readme.txt, in order, with one difference: the
# two page builds run at the same time. They read the same parquet, write to
# different directories and never touch each other, so running them together
# halves the longest step of the pipeline from about twenty minutes to ten.
#
# Everything is reproducible from the three fetches, so a full run needs a
# network connection and pulls ~6 GB of 1 ft DEM tiles (plus ~1.5 GB of staged
# fragments -- see fetch_usgs_lidar.py). --render-only skips everything already
# derived and rebuilds just the deliverable.

set -euo pipefail

cd "$(dirname "$0")"

# Three page corpora. All read the SAME 0.1 m point corpus and differ only in
# the rolling-mean window, so they are named for that window -- the spacing is a
# property of the whole pipeline now (SPACING in extract_centerline_latlon.py),
# not of an individual build.
#
# The window is the knob that actually moves the sag count: 25 m and 10 m
# differ by ~226 sags on identical elevation data, where changing the DEM from
# 1 m to 1 ft moved it by two. 5 m is the new shortest window -- expect it to
# keep more shallow dips again, and more noise with them.
SMOOTHS=(25 10 5)
# Which window the overview OPENS on. Not the same as listing it first: the
# selector keeps the order above, which reads as a scale, while the page can
# open on any of them. 25 m is the smoothest and misses shallow sags; 5 m keeps
# noise; 10 m is the working default. Must match one of SMOOTHS.
OPENS_AT=10
# The city is the unit of work. Everything below derives from it, so a second
# city collides with nothing and "region-wide" is a loop over the slugs in
# city_geojson/_index.csv. One at a time is the practical mode regardless:
# dem_livermore/ alone is 8 GB of 1 ft tiles, so a region-wide run means
# fetching, processing and deleting a city before starting the next.
CITY="${CITY:-livermore}"
# The USGS OPR collect covering the AOI. Varies by county; find another city's
# with fetch_usgs_lidar.py --aoi-file ... --list-projects.
DEM_PROJECT="${DEM_PROJECT:-CA_AlamedaCounty_2021_B21}"

usage() {
    cat <<'EOF'
usage: run_pipeline.sh [options]

  (no options)     full rebuild: fetch, derive, render
  --city SLUG      which city (default livermore). Everything derives from it:
                   derived/<slug>/, dem_<slug>/, Stormdrain_map/<slug>/
  --render-only    skip the fetches and the elevation join; rebuild the pages
                   and the overview from the existing derived/ parquet
  --no-parallel    build the page corpora one after the other
  -h, --help       this message

Writes Stormdrain_map/<city>/ -- the whole deliverable for one city.
Region-wide is a loop:  for c in $(cut -d, -f1 city_geojson/_index.csv | tail -n +2);
do ./run_pipeline.sh --city "$c"; done   -- but see the 8 GB/city DEM note above.
EOF
}

render_only=0
parallel=1
while [ $# -gt 0 ]; do
    case "$1" in
        --render-only) render_only=1 ;;
        --no-parallel) parallel=0 ;;
        --city)        CITY="$2"; shift ;;
        --city=*)      CITY="${1#*=}" ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "run_pipeline.sh: unknown option '$1'" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

DIRS=("Stormdrain_map/$CITY/streets_25m"
      "Stormdrain_map/$CITY/streets_10m"
      "Stormdrain_map/$CITY/streets_5m")
# Must track SPACING in extract_centerline_latlon.py, which names this file.
POINTS="derived/$CITY/segments_points_0p1m.parquet"
DEM_DIR="dem_$CITY"
OVERVIEW="Stormdrain_map/$CITY/index.html"

# Interpreter choice, in preference order. PY is an ARRAY because the conda
# case is a multi-word command.
#
# The conda-forge env (environment.yml) is preferred when present: it is the
# only one that can mosaic collects in DIFFERENT projections, since that needs
# gdal.Warp and GDAL has no PyPI wheel on Windows. It runs everything else
# identically -- the package versions match the uv venv.
#
# It MUST go through `micromamba run`, not .conda/env/python.exe directly:
# activation is what sets GDAL_DATA and PROJ_LIB, and without them CRS lookups
# fail. This project leans on a compound CRS with a NAVD88 vertical datum.
if [ -x .conda/env/python.exe ] && [ -x .conda/micromamba.exe ]; then
    PY=(.conda/micromamba.exe run -p .conda/env python)
elif [ -x .conda/env/bin/python ] && [ -x .conda/micromamba ]; then
    PY=(.conda/micromamba run -p .conda/env python)
elif [ -x .venv/Scripts/python.exe ]; then PY=(.venv/Scripts/python.exe)
elif [ -x .venv/bin/python ];        then PY=(.venv/bin/python)
else                                      PY=(python)
fi

start=$SECONDS
step() {
    local name=$1; shift
    printf '\n=== %s ===\n' "$name"
    local t0=$SECONDS
    "$@"
    printf -- '--- %s: %ds\n' "$name" "$((SECONDS - t0))"
}

if [ "$render_only" -eq 0 ]; then
    # Overture for every city. Livermore's own portal layer was the baseline the
    # corpus was checked against, not a second supported path.
    step "street centerline" "${PY[@]}" fetch_overture_streets.py \
        --cities "$CITY" --roads-only
    SRC="streets/overture/$CITY.geojson"
    # Every city in the registry, into derived/storm_inlets_all.csv. The
    # plotters clip it to city_geojson/$CITY.geojson at load, so fetching the
    # whole corpus once serves any --city without a refetch.
    step "storm drain inlets"    "${PY[@]}" fetch_inlets.py --all
    # The buffered boundary, not a hand-kept bbox: it already reaches two miles
    # past the city limit, which is the margin the DEM needs for streets on the
    # edge, and it exists for all 101 cities.
    step "lidar DEM tiles"       "${PY[@]}" fetch_usgs_lidar.py \
        --aoi-file "city_geojson/$CITY.geojson" \
        --project "$DEM_PROJECT" --out "./$DEM_DIR" \
        --manifest "${CITY}_tiles.csv"
    # --no-csv: at 0.1 m the intermediate CSV is ~600 MB and nothing reads it.
    step "centerline resampling" "${PY[@]}" extract_centerline_latlon.py --slim \
        --parquet --no-csv --city "$CITY" --src "$SRC"
    step "elevation join"        "${PY[@]}" add_elevation.py --no-csv \
        --city "$CITY" --dem-dir "$DEM_DIR"
elif [ ! -f "$POINTS" ]; then
    echo "run_pipeline.sh: --render-only needs $POINTS, which is not there." >&2
    echo "Run without --render-only once to build it." >&2
    exit 1
fi

if [ "$parallel" -eq 1 ]; then
    printf '\n=== street pages, %s m together ===\n' "${SMOOTHS[*]}"
    t0=$SECONDS
    # Logs go to temp files rather than interleaving three progress counters
    # into one unreadable stream; all are printed once the builds finish.
    pids=(); logs=()
    for i in "${!SMOOTHS[@]}"; do
        lg=$(mktemp); logs+=("$lg")
        "${PY[@]}" plot_street_bokeh.py --all --smooth "${SMOOTHS[$i]}" \
            --city "$CITY" --outdir "${DIRS[$i]}" >"$lg" 2>&1 &
        pids+=($!)
    done
    # Wait on ALL of them before failing: bailing on the first non-zero exit
    # would leave the others still writing pages into Stormdrain_map/ behind us.
    ok=1
    for pid in "${pids[@]}"; do wait "$pid" || ok=0; done
    cat "${logs[@]}"
    rm -f "${logs[@]}"
    printf -- '--- street pages: %ds\n' "$((SECONDS - t0))"
    if [ "$ok" -ne 1 ]; then
        echo "run_pipeline.sh: a page build failed, see above." >&2
        exit 1
    fi
else
    for i in "${!SMOOTHS[@]}"; do
        step "street pages, ${SMOOTHS[$i]} m" "${PY[@]}" plot_street_bokeh.py \
            --all --smooth "${SMOOTHS[$i]}" --city "$CITY" --outdir "${DIRS[$i]}"
    done
fi

# Last, and it must be: it reads the _index.csv that each page build writes.
# The first corpus is --pages; every other one is a repeated --pages-alt, which
# is what grows the selector on the overview from a toggle into an N-way switch.
ovw=(--pages "${DIRS[0]}" --label "${SMOOTHS[0]} m")
for i in "${!SMOOTHS[@]}"; do
    [ "$i" -eq 0 ] && continue
    ovw+=(--pages-alt "${DIRS[$i]}" --alt-label "${SMOOTHS[$i]} m")
done
ovw+=(--opens-at "${OPENS_AT} m" --city "$CITY" --out "$OVERVIEW")
step "city overview" "${PY[@]}" plot_city_overview.py "${ovw[@]}"

printf '\ndone in %dm %ds -- open %s\n' \
    "$(((SECONDS - start) / 60))" "$(((SECONDS - start) % 60))" "$OVERVIEW"
