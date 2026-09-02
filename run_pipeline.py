#!/usr/bin/env python3
"""Rebuild one city's stormdrain study end to end -- --city picks which.

A port of run_pipeline.sh, same steps in the same order, same defaults. It
exists because the SHELL turned out to be the most fragile part of an otherwise
solid pipeline. On a Windows box with Git Bash, WSL, PowerShell and cmd, plus a
conda env, a uv venv and two unrelated pythons on PATH, "which interpreter am I
running?" caused more failures than anything about drainage:

  - PowerShell resolves `bash` to WSL, not Git Bash. The script then ran under
    Linux, and although WSL can see .conda/env/python.exe (drvfs marks every
    .exe executable) and duly selected it, WSL has no cygpath. native_path()
    therefore returned GDAL_DATA as /mnt/d/... -- a path the Windows python it
    launched through interop cannot read. GDAL reported "GDAL_DATA is not
    defined" and the "does it exist" guard stayed quiet, because bash-under-WSL
    can stat /mnt/d perfectly well. A Linux shell configuring a Windows process.
  - Bare `python` in any shell here resolved to an unrelated venv with no geo
    stack, so hand-run steps failed as a missing GDAL rather than a missing env.
  - Editing run_pipeline.sh while it was executing corrupted a live run: bash
    reads a script incrementally and picked up the next chunk at a shifted
    offset, landing mid-word. Python compiles the whole file before running it.

This file is STDLIB ONLY and imports nothing from the project, so it runs under
any interpreter that can start it -- including the wrong one -- and then does
every step with the right one. That is the whole point: the launcher must not
require the environment it is responsible for selecting.

    python run_pipeline.py --city livermore
    python run_pipeline.py --city livermore --render-only --branch-split

Works from PowerShell, cmd, Git Bash or a POSIX shell without modification.
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Three page corpora by default. All read the SAME point corpus and differ only
# in the rolling-mean window, so they are named for that window and not for the
# spacing -- which is --spacing, and belongs to the corpus rather than to any
# one set of pages. --smooth overrides this; a single window is a third of the
# deliverable, which is the cheapest size cut available.
SMOOTHS = [25, 10, 5]
# Default resample interval, and it must track SPACING in
# extract_centerline_latlon.py -- that module names the corpus file, this
# one has to predict the name to check the file is there. Nothing imports
# it: this file is stdlib only so that it can run under the wrong
# interpreter and still select the right one.
SPACING = 0.15
# Which window the overview OPENS on. Not the same as listing it first: the
# selector keeps the order above, which reads as a scale, while the page can
# open on any of them. Must be one of SMOOTHS, or the overview opens on a
# corpus that was never built -- nothing else validates this.
OPENS_AT = 10

# The USGS OPR collect covering the AOI. It varies by county, and a single
# hardcoded default is a trap: --city san_jose against Livermore's Alameda
# collect matches 242 real tiles, all of them the sliver where the buffered AOI
# crosses the county line, so the fetch "succeeds" and the mistake only surfaces
# at the elevation join with almost every point outside the DEM.
#
# Precedence: DEM_PROJECT in the environment, then this registry, then derived
# from the AOI (finest resolution, ties broken by coverage). Whichever supplies
# it is checked against the AOI before any download starts.
# A LIST per city, because one collect is not always enough. Fremont was
# registered as Alameda alone, on the reasoning that Fremont is in Alameda
# County -- and 12.6% of its points then came back with no elevation, 454 whole
# streets among them. Its buffered AOI reaches two miles south into Milpitas,
# which is Santa Clara County, and that collect has 533 tiles for this AOI
# against Alameda's 707. Both are California zone 3 at 1 ftUS, so they mosaic
# as a plain same-CRS VRT with no reprojection.
#
# Nothing caught the gap, which is the part worth remembering: --check-project
# passes at --min-coverage, and that DEFAULTS TO 0.25. A collect covering a
# quarter of the AOI verifies clean. The check exists to catch a wholly wrong
# county, not a partial one.
#
# Hayward, for contrast, needs only Alameda: Santa Clara offers it 6 tiles.
# Its 3.3% of unsampled points is over San Francisco Bay, where no collect has
# data, and past the AOI edge where streets run on but tiles were never fetched.
DEM_PROJECTS = {
    "livermore": ["CA_AlamedaCounty_2021_B21"],
    "pleasanton": ["CA_AlamedaCounty_2021_B21"],
    "hayward": ["CA_AlamedaCounty_2021_B21"],
    "fremont": ["CA_AlamedaCounty_2021_B21", "CA_SantaClaraCounty_2020_A20"],
    "san_jose": ["CA_SantaClaraCounty_2020_A20"],
}


def label(spacing):
    """Filename suffix for a spacing: 0.15 -> 0p15m.

    A copy of extract_centerline_latlon.label(), for the same reason
    SPACING is copied above. Four lines, and the format is frozen by every
    parquet already on disk.
    """
    return ("%g" % spacing).replace(".", "p") + "m"


def interpreter():
    """The conda env's python, or exit saying how to get one.

    No fallback to `python`. That fallback is precisely how the wrong
    interpreter used to be chosen in silence -- an unrelated venv on PATH, or a
    Linux python under WSL -- and the resulting failure surfaced steps later as
    a missing GDAL, which sends you looking in the wrong place entirely.
    """
    for rel in ("python.exe", "bin/python"):
        cand = ROOT / ".conda" / "env" / rel
        if cand.is_file():
            return cand
    sys.exit(
        "run_pipeline.py: no .conda/env found at %s\n"
        "  create it:  .conda/micromamba.exe create -y -p .conda/env "
        "-f environment.yml" % (ROOT / ".conda" / "env")
    )


def child_env(py):
    """The environment every step runs in.

    GDAL_DATA and PATH, the two things `micromamba run` contributes that matter.
    PATH is the one with teeth: Library/bin holds the DLLs, and without it a
    delay-loaded DLL fails with 0xC06D007F (ERROR_MOD_NOT_FOUND) and the process
    dies with no traceback and no output -- numpy.linalg's LAPACK and
    matplotlib's tick path both hit it. GDAL_DATA is the quieter one: missing,
    pyogrio warns at import and GDAL cannot find header.dxf or gdalvrt.xsd,
    which is mostly cosmetic but is the first thing blamed when anything later
    breaks.

    Paths come from pathlib, so they are native on whatever platform this is.
    That removes the cygpath dependency that made the shell version wrong under
    WSL -- there is no POSIX/Windows translation step left to get wrong.
    """
    env = dict(os.environ)
    base = py.parent
    share = base / "Library" / "share" / "gdal"      # Windows layout
    if not share.is_dir():
        share = base / "share" / "gdal"              # POSIX layout
    if share.is_dir():
        env["GDAL_DATA"] = str(share)
    parts = [base, base / "Library" / "mingw-w64" / "bin",
             base / "Library" / "usr" / "bin", base / "Library" / "bin",
             base / "Scripts", base / "bin"]
    env["PATH"] = os.pathsep.join(
        [str(p) for p in parts if p.is_dir()] + [env.get("PATH", "")])
    return env


def step(name, py, env, *args):
    """Run one stage, timed and labelled, and stop the run if it fails."""
    print("\n=== %s ===" % name, flush=True)
    t0 = time.time()
    r = subprocess.run([str(py), *[str(a) for a in args]], cwd=ROOT, env=env)
    if r.returncode != 0:
        sys.exit("\nrun_pipeline.py: %s failed (exit %d)" % (name, r.returncode))
    print("--- %s: %ds" % (name, round(time.time() - t0)), flush=True)


def capture(py, env, *args):
    """A step whose stdout IS the answer -- see --best-project."""
    r = subprocess.run([str(py), *[str(a) for a in args]], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    sys.stderr.write(r.stderr)
    return r.stdout.strip() if r.returncode == 0 else ""


def main():
    ncpu = os.cpu_count() or 4
    ap = argparse.ArgumentParser(
        description="Rebuild one city's stormdrain study end to end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Writes Stormdrain_map/<city>/ -- the whole deliverable for one "
               "city.\nRegion-wide is a loop over the slugs in "
               "city_geojson/_index.csv, one at a time:\ndem_livermore/ is 8 GB "
               "of 1 ft tiles and dem_san_jose/ is 67 GB.")
    ap.add_argument("--city", default=os.environ.get("CITY", "livermore"),
                    help="city slug; everything derives from it -- derived/<city>/, "
                         "dem_<city>/, Stormdrain_map/<city>/, "
                         "city_geojson/<city>.geojson (default livermore)")
    ap.add_argument("--render-only", action="store_true",
                    help="skip the fetches and the elevation join; rebuild the "
                         "pages and the overview from the existing derived/ parquet")
    ap.add_argument("--jobs", "-j", type=int,
                    default=int(os.environ.get("JOBS", max(1, ncpu - 2))),
                    help="render N streets at once (default cores-2 = %d). Each "
                         "worker holds a FULL copy of the corpus, so this is a "
                         "memory knob, not just a speed one: San Jose's workers "
                         "ran ~10 GB apiece and 8 of them exhausted 64 GB."
                         % max(1, ncpu - 2))
    ap.add_argument("--no-parallel", action="store_true", help="same as --jobs 1")
    # Both ON by default -- see the plotters. Every city built here used them,
    # so the old opt-in default reproduced the bug they exist to fix.
    ap.add_argument("--no-branch-split", dest="branch_split", action="store_false",
                    help="do NOT split a street at junctions of three or more "
                         "segment ends. A divided road that CONVERGES then "
                         "renders as one page walking out and back rather than "
                         "one page per carriageway (Stanley Boulevard, Hopyard "
                         "Road)")
    ap.add_argument("--no-fold-split", dest="fold_split", action="store_false",
                    help="do NOT split a part that doubles back where the "
                         "carriageways form a RING with no junction to cut at "
                         "(Del Valle Parkway). Independent of --no-branch-split; "
                         "the two cover different causes of the same symptom")
    ap.add_argument("--spacing", type=float, default=SPACING, metavar="M",
                    help="resample interval in metres (default "
                         + repr(SPACING) + "). Names the corpus file and is "
                         "passed to every step that reads it. Coarser is "
                         "much smaller but not proportionally so: a page "
                         "costs about 33 KB of fixed scaffolding plus about "
                         "46 B per point, which puts a 0.3 m build at three "
                         "fifths of a 0.15 m one rather than half")
    ap.add_argument("--smooth", type=int, nargs="+", default=SMOOTHS,
                    metavar="M",
                    help="rolling-mean window(s) in metres, one page corpus "
                         "each (default " + " ".join(str(x) for x in SMOOTHS)
                         + "). The bluntest size lever there is: the three "
                         "corpora are within 1 KB of each other per page, so "
                         "one window instead of three is a third of the "
                         "deliverable")
    ap.add_argument("--site", default=None, metavar="NAME",
                    help="write under Stormdrain_map/<NAME>/ instead of "
                         "Stormdrain_map/<city>/, for building a variant "
                         "beside a city rather than over the top of it")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="skip the boundary, street, inlet and DEM fetches "
                         "but still resample and re-join elevations. What to "
                         "use when the sources are already on disk and only "
                         "--spacing changed: --render-only would skip the "
                         "resample too, leaving no corpus at the new spacing "
                         "to draw from")
    ap.add_argument("--list-cities", action="store_true",
                    help="print the registered city slugs, one per line, and "
                         "exit. run_all_cities.bat reads this rather than "
                         "keeping its own copy of the list, so adding a city "
                         "to DEM_PROJECTS is enough to include it")
    args = ap.parse_args()

    if args.list_cities:
        # Registry order, not sorted: it groups by collect, which is the order
        # a region-wide run wants anyway (same tiles stay warm in the OS cache).
        for slug in DEM_PROJECTS:
            print(slug)
        return

    city = args.city
    jobs = 1 if args.no_parallel else args.jobs
    # Forward only the NEGATIVES: the plotters default both on too, so an
    # empty list means "both enabled" on either side.
    branch = [] if args.branch_split else ["--no-branch-split"]
    if not args.fold_split:
        branch.append("--no-fold-split")

    py = interpreter()
    env = child_env(py)
    print("interpreter: %s" % py)
    print("GDAL_DATA  : %s" % env.get("GDAL_DATA", "UNSET"))

    smooths = args.smooth
    spacing = args.spacing
    # --site keeps a variant off the city it varies: a 0.3 m San Jose and
    # the 0.15 m one differ in no path but this, and the second would
    # otherwise be written straight over the first.
    site = Path("Stormdrain_map") / (args.site or city)
    dirs = [site / ("streets_%dm" % s) for s in smooths]
    points = (Path("derived") / city
              / ("segments_points_%s.parquet" % label(spacing)))
    dem_dir = Path("dem_%s" % city)
    overview = site / "index.html"
    aoi = Path("city_geojson") / ("%s.geojson" % city)
    start = time.time()

    if not (args.render_only or args.skip_fetch):
        # The AOI everything downstream is cut against: the Overture split, the
        # DEM bbox, and the clip the plotters apply to the inlet corpus.
        # Buffered two miles, which is the margin the DEM needs for streets on
        # the city edge and the reason a neighbouring city's inlets are kept --
        # they drain into this one. One TIGER fetch writes all 101 cities, so
        # this runs once and every later --city finds its file already there.
        if not (ROOT / aoi).is_file():
            step("city boundaries", py, env,
                 "fetch_city_boundaries.py", "--buffer-miles", 2)
        step("street centerline", py, env,
             "fetch_overture_streets.py", "--cities", city, "--roads-only")
        # Every city in the registry, into derived/storm_inlets_all.csv. The
        # plotters clip it to city_geojson/<city>.geojson at load, so fetching
        # the whole corpus once serves any --city without a refetch. --require:
        # another publisher's server dropping a connection must not kill a build
        # that does not need that city.
        step("storm drain inlets", py, env,
             "fetch_inlets.py", "--all", "--require", city)

        # DEM_PROJECT takes a space-separated list too, matching the registry.
        dem_project = [p for p in os.environ.get("DEM_PROJECT", "").split() if p]
        if dem_project:
            print("DEM collect(s): %s (from DEM_PROJECT)" % ", ".join(dem_project))
        elif city in DEM_PROJECTS:
            dem_project = list(DEM_PROJECTS[city])
            print("DEM collect(s): %s (from the registry in this script)"
                  % ", ".join(dem_project))
        else:
            print("\n=== choosing a DEM collect for %s ===" % city)
            # --best-project keeps stdout to the name alone. It probes one tile
            # header per candidate; a few KB, not a tile.
            dem_project = capture(py, env, "fetch_usgs_lidar.py",
                                  "--aoi-file", aoi, "--best-project")
            if not dem_project:
                sys.exit("run_pipeline.py: could not derive a DEM collect for %s.\n"
                         "Set DEM_PROJECT=<name>, or add %s to DEM_PROJECTS."
                         % (city, city))
            # One name, and only one: --best-project picks a single finest
            # collect. A city that needs two has to be registered above; there
            # is no automatic way to notice a partial gap, for the
            # --min-coverage reason in the registry comment.
            dem_project = [dem_project]
            print("DEM collect: %s (derived from the AOI)" % dem_project[0])

        # Fail before the download, not two steps later. A collect that covers a
        # sliver of the AOI is the failure mode this catches, and the DEM fetch
        # is the longest step -- Pleasanton's was 11 minutes, San Jose's 67 GB.
        # Each collect is verified and fetched on its own, into the SAME tile
        # directory -- add_elevation.py mosaics whatever it finds there. Note
        # the verify is per-collect: nothing checks that the collects TOGETHER
        # cover the AOI, so a city needing three and registered with two still
        # comes back quietly short.
        for proj in dem_project:
            suffix = "" if len(dem_project) == 1 else " (%s)" % proj
            step("verify DEM collect" + suffix, py, env, "fetch_usgs_lidar.py",
                 "--aoi-file", aoi, "--check-project", proj)
        for proj in dem_project:
            suffix = "" if len(dem_project) == 1 else " (%s)" % proj
            manifest = ("%s_tiles.csv" % city if len(dem_project) == 1
                        else "%s_tiles_%s.csv" % (city, proj))
            step("lidar DEM tiles" + suffix, py, env, "fetch_usgs_lidar.py",
                 "--aoi-file", aoi, "--project", proj,
                 "--out", "./%s" % dem_dir, "--manifest", manifest)
    # Outside the fetch block, because --skip-fetch reruns exactly these two.
    # They are also the only steps --spacing changes the output OF rather
    # than the input to: everything above is spacing-blind.
    if not args.render_only:
        # --no-csv: at 0.15 m the intermediate CSV is ~470 MB and nothing reads it.
        step("centerline resampling", py, env, "extract_centerline_latlon.py",
             "--slim", "--parquet", "--no-csv", "--city", city,
             "--spacing", spacing,
             "--src", "streets/overture/%s.geojson" % city)
        step("elevation join", py, env, "add_elevation.py", "--no-csv",
             "--city", city, "--spacing", spacing, "--dem-dir", dem_dir)
    elif not (ROOT / points).is_file():
        sys.exit("run_pipeline.py: --render-only needs %s, which is not there.\n"
                 "Run without --render-only once to build it." % points)

    # One build, not one per window. The three corpora differ ONLY in the
    # rolling mean: chaining, the chainage axis and the inlet snap are identical
    # across them, and three processes each recomputed all of it.
    what = "street pages, %s m smoothing at %g m spacing" % (
        " ".join(str(s) for s in smooths), spacing)
    what += ", " + (" ".join(branch) if branch else "branch+fold split")
    step(what, py, env, "plot_street_bokeh.py", "--all", "--city", city,
         "--jobs", jobs, *branch, "--spacing", spacing,
         "--smooth", *smooths, "--outdir", *dirs)

    # Last, and it must be: it reads the _index.csv that each page build writes.
    # The first corpus is --pages; every other one is a repeated --pages-alt,
    # which grows the selector from a toggle into an N-way switch.
    ovw = ["--pages", dirs[0], "--label", "%d m" % smooths[0]]
    for d, s in zip(dirs[1:], smooths[1:]):
        ovw += ["--pages-alt", d, "--alt-label", "%d m" % s]
    # OPENS_AT can name a window that was never built -- --smooth 10 alone
    # leaves a 25 m selector pointing at empty air. Fall back to the first
    # corpus, which by definition exists.
    opens = OPENS_AT if OPENS_AT in smooths else smooths[0]
    ovw += ["--opens-at", "%d m" % opens, "--city", city, "--out", overview]
    step("city overview", py, env, "plot_city_overview.py", *ovw)

    el = round(time.time() - start)
    print("\ndone in %dm %ds -- open %s" % (el // 60, el % 60, overview))


if __name__ == "__main__":
    main()
