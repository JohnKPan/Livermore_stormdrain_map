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
# network connection and pulls ~1.35 GB of DEM tiles. --render-only skips
# everything already derived and rebuilds just the deliverable.

set -euo pipefail

cd "$(dirname "$0")"

ALT_SMOOTH=10
ALT_DIR="Stormdrain_map/streets_10m"
POINTS="derived/segments_points_1m.parquet"

usage() {
    cat <<'EOF'
usage: run_pipeline.sh [options]

  (no options)     full rebuild: fetch, derive, render
  --render-only    skip the fetches and the elevation join; rebuild the pages
                   and the overview from the existing derived/ parquet
  --no-parallel    build the two page corpora one after the other
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

# The venv layout differs by platform. Fall back to whatever python is on PATH,
# so the script still works in an already-activated shell.
if   [ -x .venv/Scripts/python.exe ]; then PY=".venv/Scripts/python.exe"
elif [ -x .venv/bin/python ];        then PY=".venv/bin/python"
else                                      PY="python"
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
    step "street centerline"     "$PY" fetch_livermore_street_centerlines.py
    step "storm drain inlets"    "$PY" fetch_inlets.py
    step "lidar DEM tiles"       "$PY" fetch_dem.py
    step "centerline resampling" "$PY" extract_centerline_latlon.py --slim --parquet
    step "elevation join"        "$PY" add_elevation.py --no-csv
elif [ ! -f "$POINTS" ]; then
    echo "run_pipeline.sh: --render-only needs $POINTS, which is not there." >&2
    echo "Run without --render-only once to build it." >&2
    exit 1
fi

if [ "$parallel" -eq 1 ]; then
    printf '\n=== street pages, 25 m and %s m together ===\n' "$ALT_SMOOTH"
    t0=$SECONDS
    # Logs go to temp files rather than interleaving two progress counters into
    # one unreadable stream; both are printed once the builds finish.
    log_a=$(mktemp); log_b=$(mktemp)
    "$PY" plot_street_bokeh.py --all >"$log_a" 2>&1 &
    pid_a=$!
    "$PY" plot_street_bokeh.py --all --smooth "$ALT_SMOOTH" --outdir "$ALT_DIR" \
        >"$log_b" 2>&1 &
    pid_b=$!
    # Wait on BOTH before failing: bailing on the first non-zero exit would
    # leave the other still writing pages into Stormdrain_map/ behind us.
    ok=1
    wait "$pid_a" || ok=0
    wait "$pid_b" || ok=0
    cat "$log_a" "$log_b"
    rm -f "$log_a" "$log_b"
    printf -- '--- street pages: %ds\n' "$((SECONDS - t0))"
    if [ "$ok" -ne 1 ]; then
        echo "run_pipeline.sh: a page build failed, see above." >&2
        exit 1
    fi
else
    step "street pages, 25 m"           "$PY" plot_street_bokeh.py --all
    step "street pages, ${ALT_SMOOTH} m" "$PY" plot_street_bokeh.py --all \
        --smooth "$ALT_SMOOTH" --outdir "$ALT_DIR"
fi

# Last, and it must be: it reads the _index.csv that each page build writes.
step "city overview" "$PY" plot_city_overview.py --pages-alt "$ALT_DIR"

printf '\ndone in %dm %ds -- open Stormdrain_map/index.html\n' \
    "$(((SECONDS - start) / 60))" "$(((SECONDS - start) % 60))"
