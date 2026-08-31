#!/usr/bin/env bash
#
# DEPRECATED -- use `python run_pipeline.py` instead.
#
# Superseded in full: run_pipeline.py runs the same steps in the same order with
# the same defaults, and was checked against this script on Hayward -- identical
# file names across all three corpora, zero differences over 8,904 index rows
# and 13 computed columns, and pages identical once Bokeh's per-run UUID and
# model-id counter are normalised.
#
# What it fixes is the launcher, not the pipeline. This is a bash script, so it
# needs a bash, and on Windows choosing one is a trap:
#
#   - PowerShell resolves `bash` to WSL, not Git Bash. WSL then runs this under
#     Linux, correctly selects .conda/env/python.exe (drvfs marks every .exe
#     executable) and launches that Windows interpreter through interop -- but
#     WSL has no cygpath, so native_path() leaves GDAL_DATA as /mnt/d/..., which
#     the Windows process cannot read. It reports "GDAL_DATA is not defined",
#     which sends you looking at GDAL rather than at the shell.
#   - Editing this file while it is running corrupts the run in progress. Bash
#     reads a script incrementally, so an edit shifts the byte offset of what it
#     has not read yet and it resumes mid-token. That happened here: a live
#     build died on `allelises: command not found`, a fragment of the word
#     "parallelises" in a comment.
#
# run_pipeline.py has no shell in the loop, is stdlib-only so it starts under any
# interpreter and then runs every step with .conda/env/python.exe, and takes its
# paths from pathlib so they are native by construction -- no cygpath step left
# to get wrong. It works from PowerShell, cmd or Git Bash unchanged.
#
# Kept because it is the original and still works from Git Bash. Nothing
# documents it any more; readme.txt points at run_pipeline.py throughout.
#
# Rebuild one city's stormdrain study end to end -- --city picks which.
#
# This is the command list from readme.txt, in order, with one difference: the
# page build renders every smoothing window in a single pass and spreads the
# streets across cores, rather than running one process per window.
#
# Everything is reproducible from the four fetches, so a full run needs a
# network connection and pulls the city's 1 ft DEM tiles -- 8 GB for Livermore,
# 12 GB for Pleasanton, plus ~1.5 GB of staged fragments (see
# fetch_usgs_lidar.py). --render-only skips everything already derived and
# rebuilds just the deliverable.
#
# A full run is ~12 min per city on 18 cores once the DEM is cached: fetches
# under a minute each, resampling ~45s, elevation join ~100s, and the three page
# corpora together ~9 min, which is the whole cost.

set -euo pipefail

cd "$(dirname "$0")"

# Three page corpora. All read the SAME 0.15 m point corpus and differ only in
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
# dem_livermore/ is 8 GB of 1 ft tiles and dem_pleasanton/ is 12 GB, so a
# region-wide run means fetching, processing and deleting a city before
# starting the next.
CITY="${CITY:-livermore}"
# The USGS OPR collect covering the AOI. It varies by county, and a single
# hardcoded default is a trap: --city san_jose against Livermore's Alameda
# collect matches 242 real tiles, all of them the sliver where the buffered AOI
# crosses the county line, so the fetch "succeeds" and the mistake only surfaces
# at the elevation join with almost every point outside the DEM.
#
# Three ways to settle it, in precedence order:
#   1. DEM_PROJECT in the environment -- explicit always wins
#   2. this registry, for cities already pinned to a known-good collect
#   3. derived from the AOI: finest resolution, ties broken by coverage
# Whichever supplies it, the choice is checked against the AOI before any
# download starts.
declare -A DEM_PROJECTS=(
    [livermore]=CA_AlamedaCounty_2021_B21
    [pleasanton]=CA_AlamedaCounty_2021_B21
    # Fremont and Hayward are Alameda County too, so the same 2021 collect
    # should cover them. Registering it skips deriving one from the AOI, which
    # is a second round-trip to the TNM API -- and that API has been answering
    # 504 about two times in three. The "verify DEM collect" step still checks
    # this against the AOI before anything downloads, so a wrong guess here
    # fails loudly rather than fetching the wrong county.
    [fremont]=CA_AlamedaCounty_2021_B21
    [hayward]=CA_AlamedaCounty_2021_B21
    [san_jose]=CA_SantaClaraCounty_2020_A20
)
DEM_PROJECT="${DEM_PROJECT:-}"
# Streets render independently, so this is the knob that decides how long the
# longest step takes. Two cores are left free so the machine stays usable.
_ncpu=$(nproc 2>/dev/null || echo 4)
JOBS="${JOBS:-$(( _ncpu > 3 ? _ncpu - 2 : 1 ))}"

usage() {
    cat <<'EOF'
usage: run_pipeline.sh [options]

  (no options)     full rebuild: fetch, derive, render
  --city SLUG      which city (default livermore). Everything derives from it:
                   derived/<slug>/, dem_<slug>/, Stormdrain_map/<slug>/
  --render-only    skip the fetches and the elevation join; rebuild the pages
                   and the overview from the existing derived/ parquet
  --jobs N         render N streets at once (default: cores - 2)
  --no-parallel    same as --jobs 1
  --fold-split     also split a part that doubles back where the carriageways
                   form a ring with no junction to cut at -- Del Valle Parkway,
                   Stoneridge Mall Road. Independent of --branch-split; the two
                   cover different causes of the same symptom.
  --branch-split   split a street at every junction of three or more segment
                   ends, so a divided road that CONVERGES -- Stanley Boulevard,
                   Hopyard Road, Valley Avenue -- gives one page per carriageway
                   instead of one page walking out and back again. Costs ~12%
                   more pages, most of them short stubs at the cut junctions.
  -h, --help       this message

environment:
  DEM_PROJECT      the USGS OPR collect to fetch. Unset, the script uses its
                   DEM_PROJECTS registry for known cities and otherwise derives
                   one from the AOI -- finest resolution, ties broken by
                   coverage. Whatever supplies it is verified against the AOI
                   before the download starts.

Writes Stormdrain_map/<city>/ -- the whole deliverable for one city.
Region-wide is a loop:  for c in $(cut -d, -f1 city_geojson/_index.csv | tail -n +2);
do bash run_pipeline.sh --city "$c"; done   -- but see the 8 GB/city DEM note above.
EOF
}

render_only=0
BRANCH=()
while [ $# -gt 0 ]; do
    case "$1" in
        --render-only) render_only=1 ;;
        --branch-split) BRANCH+=(--branch-split) ;;
        --fold-split)   BRANCH+=(--fold-split) ;;
        --no-parallel) JOBS=1 ;;
        --jobs)        JOBS="$2"; shift ;;
        --jobs=*)      JOBS="${1#*=}" ;;
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
POINTS="derived/$CITY/segments_points_0p15m.parquet"
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
# The env's python is called DIRECTLY, not through `micromamba run`. The wrapper
# re-execs the interpreter from a second process, and under a PowerShell-hosted
# Git Bash it has been seen to exit without running anything and without an
# error -- the step prints its header and the script dies, `set -e` doing its
# job on an exit status nothing explained.
#
# The comment this replaces claimed activation was mandatory because it sets
# GDAL_DATA and PROJ_LIB. Only half of that is true: `micromamba run` sets
# GDAL_DATA and leaves PROJ_LIB unset -- pyproj carries its own data. So the one
# thing activation contributed is set here instead, and CRS lookups against the
# compound NAVD88 datum were checked against both paths.
#
# PYTHON=... overrides all of it.
# native_path: Git Bash's $PWD is /d/Claude/..., which the Windows GDAL build
# cannot open. cygpath -w gives it back as D:\Claude\..., which is what
# `micromamba run` was exporting. No-op where cygpath does not exist.
native_path() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}
if [ -n "${PYTHON:-}" ]; then
    read -r -a PY <<< "$PYTHON"
elif [ -x .conda/env/python.exe ];   then PY=(.conda/env/python.exe)
elif [ -x .conda/env/bin/python ];   then PY=(.conda/env/bin/python)
else
    # No silent fallback to `python`. On this machine bare `python` has resolved
    # to an unrelated venv, and under WSL to a Linux interpreter with no geo
    # stack at all -- both of which fail much later, as a missing GDAL rather
    # than a missing environment. Say it here instead.
    echo "run_pipeline.sh: no .conda/env found." >&2
    echo "  create it:  .conda/micromamba.exe create -y -p .conda/env -f environment.yml" >&2
    echo "  and run this from Git Bash, not WSL or PowerShell." >&2
    exit 1
fi

# The conda env's variables, set whenever that env EXISTS rather than only when
# it happened to be the interpreter chosen above. Those are separate questions,
# and tying them together meant PYTHON=... silently disabled the environment as
# well as the interpreter -- pointing PYTHON at the conda python itself was
# enough to lose GDAL_DATA and PATH, which is a trap with no visible cause.
#
# PATH matters more than GDAL_DATA. `micromamba run` prepends six env
# directories, and Library/bin is where the DLLs live. Without it a delay-loaded
# DLL fails with 0xC06D007F (ERROR_MOD_NOT_FOUND) and the process dies with no
# traceback and no output: add_elevation.py never hit one, but preview_dem.py's
# matplotlib tick path does immediately, and so does numpy.linalg's LAPACK.
#
# GDAL_DATA is the quieter one. Missing, pyogrio warns at import ("Could not
# detect GDAL data files") and GDAL cannot find header.dxf or gdalvrt.xsd. Most
# of that is cosmetic -- gdalvrt.xsd only validates VRT XML -- but it is noise
# on every step and it is the first thing suspected when something later breaks.
if [ -d .conda/env ]; then
    E="$PWD/.conda/env"
    if [ -d "$E/Library/share/gdal" ]; then          # Windows layout
        GDAL_DATA=$(native_path "$E/Library/share/gdal"); export GDAL_DATA
        PATH="$E:$E/Library/mingw-w64/bin:$E/Library/usr/bin:$E/Library/bin:$E/Scripts:$E/bin:$PATH"
        export PATH
    elif [ -d "$E/share/gdal" ]; then                # POSIX layout
        GDAL_DATA=$(native_path "$E/share/gdal"); export GDAL_DATA
        PATH="$E/bin:$PATH"; export PATH
    fi
fi

# An `if`, not an && chain: under `set -e` the chain's status is the failed test
# when the directory does exist, which is the normal case.
if [ -n "${GDAL_DATA:-}" ] && [ ! -d "$GDAL_DATA" ]; then
    echo "run_pipeline.sh: warning, GDAL_DATA=$GDAL_DATA does not exist" >&2
fi
# Say so up front rather than leaving it to a pyogrio warning buried in a step's
# output. A Hayward run was seen warning "GDAL_DATA is not defined" mid-download
# with PYTHON unset and the block above apparently taken -- never reproduced, so
# this reports the fact at the top of the run where it cannot be missed.
if [ -d .conda/env ] && [ -z "${GDAL_DATA:-}" ]; then
    echo "run_pipeline.sh: warning, .conda/env exists but GDAL_DATA is unset;" \
         "expected $PWD/.conda/env/Library/share/gdal" >&2
fi

echo "run_pipeline.sh is DEPRECATED -- use: python run_pipeline.py --city $CITY" >&2
echo "  (it works from PowerShell, cmd or Git Bash; this script needs Git Bash)" >&2

start=$SECONDS
step() {
    local name=$1; shift
    printf '\n=== %s ===\n' "$name"
    local t0=$SECONDS
    "$@"
    printf -- '--- %s: %ds\n' "$name" "$((SECONDS - t0))"
}

if [ "$render_only" -eq 0 ]; then
    # The AOI everything downstream is cut against: the Overture split, the DEM
    # bbox, and the clip the plotters apply to the inlet corpus. Buffered two
    # miles, which is the margin the DEM needs for streets on the city edge and
    # the reason a neighbouring city's inlets are kept -- they drain into this
    # one. A buffered polygon is no longer a city limit; see fetch_city_
    # boundaries.py. One TIGER fetch writes all 101 cities, not just $CITY, so
    # this runs once and every later --city finds its file already there.
    if [ ! -f "city_geojson/$CITY.geojson" ]; then
        step "city boundaries" "${PY[@]}" fetch_city_boundaries.py \
            --buffer-miles 2
    fi
    # Overture for every city. Livermore's own portal layer was the baseline the
    # corpus was checked against, not a second supported path.
    step "street centerline" "${PY[@]}" fetch_overture_streets.py \
        --cities "$CITY" --roads-only
    SRC="streets/overture/$CITY.geojson"
    # Every city in the registry, into derived/storm_inlets_all.csv. The
    # plotters clip it to city_geojson/$CITY.geojson at load, so fetching the
    # whole corpus once serves any --city without a refetch.
    # --require "$CITY": another publisher's server dropping a connection must
    # not kill a build that does not need that city. Only the city being built
    # is fatal, and its absence still is.
    step "storm drain inlets"    "${PY[@]}" fetch_inlets.py --all \
        --require "$CITY"
    # Settle the collect before downloading anything. See DEM_PROJECTS above.
    AOI="city_geojson/$CITY.geojson"
    if [ -n "$DEM_PROJECT" ]; then
        echo "DEM collect: $DEM_PROJECT (from DEM_PROJECT)"
    elif [ -n "${DEM_PROJECTS[$CITY]:-}" ]; then
        DEM_PROJECT="${DEM_PROJECTS[$CITY]}"
        echo "DEM collect: $DEM_PROJECT (from the registry in this script)"
    else
        printf '\n=== choosing a DEM collect for %s ===\n' "$CITY"
        # Command substitution, so --best-project must keep stdout to the name
        # alone. It probes one tile header per candidate; a few KB, not a tile.
        DEM_PROJECT=$("${PY[@]}" fetch_usgs_lidar.py --aoi-file "$AOI" --best-project)
        if [ -z "$DEM_PROJECT" ]; then
            echo "run_pipeline.sh: could not derive a DEM collect for $CITY." >&2
            echo "Set DEM_PROJECT=<name>, or add $CITY to DEM_PROJECTS." >&2
            exit 1
        fi
        echo "DEM collect: $DEM_PROJECT (derived from the AOI)"
    fi
    # Fail before the download, not two steps later. A collect that covers a
    # sliver of the AOI is the failure mode this catches, and the DEM fetch is
    # the longest step in the pipeline -- Pleasanton's was 11 minutes, San
    # Jose's is 68 GB.
    step "verify DEM collect" "${PY[@]}" fetch_usgs_lidar.py \
        --aoi-file "$AOI" --check-project "$DEM_PROJECT"
    # The buffered boundary, not a hand-kept bbox: it already reaches two miles
    # past the city limit, which is the margin the DEM needs for streets on the
    # edge, and it exists for all 101 cities.
    step "lidar DEM tiles"       "${PY[@]}" fetch_usgs_lidar.py \
        --aoi-file "$AOI" \
        --project "$DEM_PROJECT" --out "./$DEM_DIR" \
        --manifest "${CITY}_tiles.csv"
    # --no-csv: at 0.15 m the intermediate CSV is ~470 MB and nothing reads it.
    step "centerline resampling" "${PY[@]}" extract_centerline_latlon.py --slim \
        --parquet --no-csv --city "$CITY" --src "$SRC"
    step "elevation join"        "${PY[@]}" add_elevation.py --no-csv \
        --city "$CITY" --dem-dir "$DEM_DIR"
elif [ ! -f "$POINTS" ]; then
    echo "run_pipeline.sh: --render-only needs $POINTS, which is not there." >&2
    echo "Run without --render-only once to build it." >&2
    exit 1
fi

# One build, not one per window. The three corpora differ ONLY in the rolling
# mean: chaining, the chainage axis and the inlet snap are identical across
# them, and three processes each recomputed all of it. plot_street_bokeh.py now
# takes every window in one pass and parallelises across STREETS instead, which
# scales with cores rather than being stuck at three.
step "street pages, ${SMOOTHS[*]} m${BRANCH[*]:+, ${BRANCH[*]}}" "${PY[@]}" plot_street_bokeh.py --all \
    --city "$CITY" --jobs "$JOBS" "${BRANCH[@]}" \
    --smooth "${SMOOTHS[@]}" --outdir "${DIRS[@]}"

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
