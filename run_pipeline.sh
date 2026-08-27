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
DIRS=("Stormdrain_map/streets_25m"
      "Stormdrain_map/streets_10m"
      "Stormdrain_map/streets_5m")
# Must track SPACING in extract_centerline_latlon.py, which names this file.
POINTS="derived/segments_points_0p1m.parquet"
DEM_DIR="dem_livermore"

usage() {
    cat <<'EOF'
usage: run_pipeline.sh [options]

  (no options)     full rebuild: fetch, derive, render
  --render-only    skip the fetches and the elevation join; rebuild the pages
                   and the overview from the existing derived/ parquet
  --no-parallel    build the page corpora one after the other
  -h, --help       this message

Writes Stormdrain_map/ -- the whole deliverable. Open Stormdrain_map/index.html.
EOF
}

render_only=0
parallel=1
for arg in "$@"; do
    case "$arg" in
        --render-only) render_only=1 ;;
        --no-parallel) parallel=0 ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "run_pipeline.sh: unknown option '$arg'" >&2; usage >&2; exit 2 ;;
    esac
done

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
    step "street centerline"     "${PY[@]}" fetch_livermore_street_centerlines.py
    step "storm drain inlets"    "${PY[@]}" fetch_inlets.py
    step "lidar DEM tiles"       "${PY[@]}" fetch_usgs_lidar.py --aoi livermore \
        --project CA_AlamedaCounty_2021_B21 --out "./$DEM_DIR" \
        --manifest livermore_tiles.csv
    # --no-csv: at 0.1 m the intermediate CSV is ~600 MB and nothing reads it.
    step "centerline resampling" "${PY[@]}" extract_centerline_latlon.py --slim \
        --parquet --no-csv
    step "elevation join"        "${PY[@]}" add_elevation.py --no-csv
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
            --outdir "${DIRS[$i]}" >"$lg" 2>&1 &
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
            --all --smooth "${SMOOTHS[$i]}" --outdir "${DIRS[$i]}"
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
step "city overview" "${PY[@]}" plot_city_overview.py "${ovw[@]}"

printf '\ndone in %dm %ds -- open Stormdrain_map/index.html\n' \
    "$(((SECONDS - start) / 60))" "$(((SECONDS - start) % 60))"
