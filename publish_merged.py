"""Stage a merged multi-city map as a self-contained GitHub Pages site.

The merged index that merge_cities.py writes points at the per-city corpora
with relative hops -- ../../san_francisco/streets_10m/X.html -- which is what
keeps a local merge from copying half a gigabyte of pages that already exist.
A published site cannot do that: the hop climbs above the repository root.

So publishing rewrites the hop. The staged tree is

    <stage>/index.html                     the overview
    <stage>/_index.csv                     page index, file = <city>/...
    <stage>/<city>/streets_10m/*.html      every city's pages, side by side
    <stage>/.nojekyll                      or Pages drops _index.csv

and the overview is re-rendered against it, so `pfx` comes out empty and each
href is just "<city>/streets_10m/<page>.html".

Pages are HARDLINKED, not copied, when the stage sits on the same volume as
the build. 850 MB of identical HTML does not need to exist twice on the way to
a push; git reads through a link exactly as it reads a file. Falls back to a
copy across volumes.

Usage:
    python publish_merged.py --name SF_and_North_Peninsula \
        --title "San Francisco and the North Peninsula"
    python publish_merged.py ... --push          # also create and force-push
"""

import argparse
import os
import shutil
import subprocess
import sys

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_NAME = "SF_and_North_Peninsula"
DEFAULT_TITLE = "San Francisco and the North Peninsula"
SMOOTH = "10m"


def link_or_copy(src, dst):
    """A hardlink where the filesystem allows one, else a copy."""
    try:
        os.link(src, dst)
        return "link"
    except OSError:
        shutil.copy2(src, dst)
        return "copy"


def run(cmd, cwd=None):
    print("    $ " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        sys.stdout.write(r.stdout[-3000:])
        sys.stderr.write(r.stderr[-3000:])
        raise SystemExit("command failed: %s" % " ".join(cmd))
    return r.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default=DEFAULT_NAME,
                    help="merged corpus slug (default %s)" % DEFAULT_NAME)
    ap.add_argument("--title", default=DEFAULT_TITLE,
                    help="what the overview calls itself")
    ap.add_argument("--smooth", default=SMOOTH)
    ap.add_argument("--stage", default=None,
                    help="where to build the site (default "
                         "Stormdrain_map/_publish_<name>/)")
    ap.add_argument("--repo", default=None,
                    help="GitHub repo to publish to "
                         "(default <name>_stormdrain_map)")
    ap.add_argument("--push", action="store_true",
                    help="create the repo if needed and force-push gh-pages")
    args = ap.parse_args()

    merged_pages = os.path.join(HERE, "Stormdrain_map", args.name,
                                "streets_" + args.smooth)
    verts = os.path.join(HERE, "derived", args.name, "segments_vertices.csv")
    gap = os.path.join(HERE, "derived", args.name, "gap_vertices.csv")
    idx_path = os.path.join(merged_pages, "_index.csv")
    for p in (idx_path, verts):
        if not os.path.exists(p):
            raise SystemExit("missing %s -- run merge_cities.py first"
                             % os.path.relpath(p, HERE))

    stage = os.path.abspath(args.stage or os.path.join(
        HERE, "Stormdrain_map", "_publish_" + args.name))
    if os.path.exists(stage):
        print("clearing %s" % os.path.relpath(stage, HERE))
        shutil.rmtree(stage)
    os.makedirs(stage)

    idx = pd.read_csv(idx_path)
    # ../../<city>/streets_10m/<page>.html  ->  <city>, <page>.html
    parts = idx.file.astype(str).str.split("/")
    idx["_city"] = parts.str[2]
    idx["_leaf"] = parts.str[-1]
    print("staging %d page(s) from %d corpora"
          % (len(idx), idx._city.nunique()))

    modes = {"link": 0, "copy": 0}
    for city, grp in idx.groupby("_city"):
        src_dir = os.path.join(HERE, "Stormdrain_map", city,
                               "streets_" + args.smooth)
        dst_dir = os.path.join(stage, city, "streets_" + args.smooth)
        os.makedirs(dst_dir, exist_ok=True)
        for leaf in grp._leaf:
            s = os.path.join(src_dir, leaf)
            if not os.path.exists(s):
                raise SystemExit("missing page %s" % s)
            modes[link_or_copy(s, os.path.join(dst_dir, leaf))] += 1
        print("  %-22s %5d page(s)" % (city, len(grp)))
    print("  %d hardlinked, %d copied" % (modes["link"], modes["copy"]))

    out_idx = idx.drop(columns=["_city", "_leaf"]).copy()
    out_idx["file"] = idx._city + "/streets_" + args.smooth + "/" + idx._leaf
    out_idx.to_csv(os.path.join(stage, "_index.csv"), index=False)

    cmd = [sys.executable, os.path.join(HERE, "plot_city_overview.py"),
           "--city", args.name, "--name", args.title,
           "--verts", os.path.relpath(verts, HERE),
           "--pages", os.path.relpath(stage, HERE),
           "--label", args.smooth.replace("m", " m"),
           "--out", os.path.relpath(os.path.join(stage, "index.html"), HERE),
           "--color-by", "sags"]
    if os.path.exists(gap):
        cmd += ["--gap-verts", os.path.relpath(gap, HERE)]
    print("rendering the overview against the staged tree")
    sys.stdout.write(run(cmd, cwd=HERE)[-600:])

    open(os.path.join(stage, ".nojekyll"), "w").close()

    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(stage) for f in fs)
    n = sum(len(fs) for _, _, fs in os.walk(stage))
    print("staged: %.1f MB over %d file(s) -> %s"
          % (total / 1e6, n, os.path.relpath(stage, HERE)))
    if total > 1_000_000_000:
        print("  !! over the 1 GB GitHub Pages limit")

    # ------------------------------------------------------- verify the links
    bad = [f for f in out_idx.file
           if not os.path.exists(os.path.join(stage, f))]
    print("broken hrefs in the staged tree: %d" % len(bad))
    if bad:
        raise SystemExit("refusing to publish: " + ", ".join(bad[:3]))

    if not args.push:
        print("\nnot pushing (--push to publish)")
        return

    repo = args.repo or (args.name + "_stormdrain_map")
    user = run(["gh", "api", "user", "-q", ".login"]).strip()
    full = "%s/%s" % (user, repo)
    exists = subprocess.run(["gh", "repo", "view", full],
                            capture_output=True, text=True).returncode == 0
    if not exists:
        print("creating %s" % full)
        run(["gh", "repo", "create", full, "--public",
             "--description", args.title + " -- storm drain sag map"])
    else:
        print("%s already exists; force-pushing a fresh orphan commit" % full)

    # A fresh orphan each time: a second generation of ~850 MB would otherwise
    # live in the repository forever. This is why the push is --force.
    run(["git", "init", "-q", "-b", "gh-pages"], cwd=stage)
    run(["git", "add", "-A"], cwd=stage)
    run(["git", "commit", "-q", "-m",
         "%s -- %d pages over %d cities" % (args.title, len(out_idx),
                                            idx._city.nunique())], cwd=stage)
    run(["git", "remote", "add", "origin",
         "https://github.com/%s.git" % full], cwd=stage)
    print("pushing %.0f MB, this takes a while" % (total / 1e6))
    run(["git", "push", "-q", "--force", "origin", "gh-pages"], cwd=stage)

    # GitHub turns Pages on by itself for a branch literally named gh-pages,
    # so this POST answers 409 "already enabled" on the happy path. That is
    # the sibling of success, not a failure -- asking for the state you wanted
    # and being told you already have it. Only a different error is fatal.
    enable = subprocess.run(
        ["gh", "api", "-X", "POST", "repos/%s/pages" % full,
         "-f", "source[branch]=gh-pages", "-f", "source[path]=/"],
        capture_output=True, text=True)
    if enable.returncode and "already enabled" not in enable.stderr + enable.stdout:
        sys.stderr.write(enable.stderr[-2000:])
        raise SystemExit("could not enable Pages")
    print("  Pages: %s" % ("already enabled" if enable.returncode
                           else "enabled now"))
    print("\npublished: https://%s.github.io/%s/" % (user, repo))
    print("  the first build takes a minute or two; a 404 before then is the")
    print("  site still deploying, not a bad push")


if __name__ == "__main__":
    main()
