"""Publish one city's built corpus as its own GitHub Pages site.

The merged map needs publish_merged.py because merge_cities.py leaves its index
pointing at the per-city corpora with ../../ hops that climb above the repo
root. A single city has no such problem: plot_city_overview.py writes
"streets_10m/Abalone_Place.html" relative to the city directory, which is
already the layout a Pages site wants. So publishing a city is a straight
restage of

    Stormdrain_map/<city>/index.html
    Stormdrain_map/<city>/streets_<w>m/*.html   for each PUBLISHED window
    .nojekyll                                   or Pages drops dotted paths

with nothing rewritten.

Which windows go up is a per-city decision and NOT "whatever is on disk":
Livermore and Pleasanton publish all three, the rest publish 10 m only, because
the three corpora are within 1 KB of each other per page and a 9,000-page city
times three does not fit in the 1 GB a Pages site gets. Passing --smooth
overrides it.

Pages are HARDLINKED, as in publish_merged.py: half a gigabyte of HTML does not
need to exist twice on the way to a push, and git reads through a link.

Usage:
    python publish_city.py --city livermore
    python publish_city.py --city san_jose --push
    python publish_city.py --all --push
"""

import argparse
import os
import subprocess
import sys

from publish_merged import force_rmtree, link_or_copy, run

HERE = os.path.dirname(os.path.abspath(__file__))

# city -> the smoothing windows that city's site publishes, and the repository
# name it publishes to. A city absent from here has no site of its own; the
# five in the merged map (brisbane, burlingame, san_bruno, san_francisco,
# south_san_francisco) are published by publish_merged.py instead.
SITES = {
    "livermore":  dict(repo="Livermore_stormdrain_map",  smooth=[25, 10, 5]),
    "pleasanton": dict(repo="Pleasanton_stormdrain_map", smooth=[25, 10, 5]),
    "hayward":    dict(repo="Hayward_stormdrain_map",    smooth=[10]),
    "fremont":    dict(repo="Fremont_stormdrain_map",    smooth=[10]),
    "san_jose":   dict(repo="San_Jose_stormdrain_map",   smooth=[10]),
}

PAGES_LIMIT = 1_000_000_000


def publish(city, smooths, repo, push, stage_root):
    src = os.path.join(HERE, "Stormdrain_map", city)
    idx = os.path.join(src, "index.html")
    if not os.path.exists(idx):
        raise SystemExit("missing %s -- build the city first" % idx)

    stage = os.path.join(stage_root, "_publish_" + city)
    if os.path.exists(stage):
        force_rmtree(stage)
    os.makedirs(stage)

    modes = {"link": 0, "copy": 0}
    link_or_copy(idx, os.path.join(stage, "index.html"))
    total_pages = 0
    for w in smooths:
        name = "streets_%dm" % w
        sdir = os.path.join(src, name)
        if not os.path.isdir(sdir):
            raise SystemExit("%s has no %s -- rebuild with --smooth %d"
                             % (city, name, w))
        ddir = os.path.join(stage, name)
        os.makedirs(ddir, exist_ok=True)
        n = 0
        for e in os.scandir(sdir):
            if e.is_file():
                modes[link_or_copy(e.path, os.path.join(ddir, e.name))] += 1
                n += e.name.endswith(".html")
        total_pages += n
        print("  %-14s %6d page(s)" % (name, n))

    open(os.path.join(stage, ".nojekyll"), "w").close()

    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(stage) for f in fs)
    nfiles = sum(len(fs) for _, _, fs in os.walk(stage))
    print("  staged %.1f MB over %d file(s), %d hardlinked / %d copied"
          % (total / 1e6, nfiles, modes["link"], modes["copy"]))
    if total > PAGES_LIMIT:
        raise SystemExit("  !! %.0f MB is over the 1 GB GitHub Pages limit"
                         % (total / 1e6))

    if not push:
        print("  not pushing (--push to publish)")
        return None

    user = run(["gh", "api", "user", "-q", ".login"]).strip()
    full = "%s/%s" % (user, repo)
    exists = subprocess.run(["gh", "repo", "view", full],
                            capture_output=True, text=True).returncode == 0
    if not exists:
        print("  creating %s" % full)
        run(["gh", "repo", "create", full, "--public", "--description",
             "%s -- storm drain sag map" % city.replace("_", " ").title()])

    # A fresh orphan each time; see publish_merged.py for why the push is
    # --force rather than a commit on top.
    run(["git", "init", "-q", "-b", "gh-pages"], cwd=stage)
    run(["git", "add", "-A"], cwd=stage)
    run(["git", "commit", "-q", "-m",
         "%s -- %d pages over %s" % (city, total_pages,
                                     ", ".join("%dm" % w for w in smooths))],
        cwd=stage)
    run(["git", "remote", "add", "origin",
         "https://github.com/%s.git" % full], cwd=stage)
    print("  pushing %.0f MB" % (total / 1e6))
    run(["git", "push", "-q", "--force", "origin", "gh-pages"], cwd=stage)

    # 409 "already enabled" is the sibling of success -- see publish_merged.py.
    en = subprocess.run(
        ["gh", "api", "-X", "POST", "repos/%s/pages" % full,
         "-f", "source[branch]=gh-pages", "-f", "source[path]=/"],
        capture_output=True, text=True)
    if en.returncode and "already enabled" not in en.stderr + en.stdout:
        sys.stderr.write(en.stderr[-2000:])
        raise SystemExit("could not enable Pages for %s" % full)
    url = "https://%s.github.io/%s/" % (user, repo)
    print("  published: %s" % url)
    return url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", help="city slug (see SITES)")
    ap.add_argument("--all", action="store_true",
                    help="every city in SITES")
    ap.add_argument("--smooth", type=int, nargs="+", default=None,
                    help="override which windows this city publishes")
    ap.add_argument("--repo", default=None, help="override the repository")
    ap.add_argument("--push", action="store_true")
    ap.add_argument("--stage", default=os.path.join(HERE, "Stormdrain_map"),
                    help="where to build the staging trees")
    args = ap.parse_args()

    if args.all:
        cities = list(SITES)
    elif args.city:
        cities = [args.city]
    else:
        raise SystemExit("--city or --all")

    done = []
    for c in cities:
        cfg = SITES.get(c)
        if cfg is None and (args.repo is None or args.smooth is None):
            raise SystemExit("%s is not in SITES; pass --repo and --smooth" % c)
        cfg = cfg or {}
        smooths = args.smooth or cfg["smooth"]
        repo = args.repo or cfg["repo"]
        print("=== %s -> %s ===" % (c, repo))
        url = publish(c, smooths, repo, args.push, args.stage)
        if url:
            done.append((c, url))
    if done:
        print("\npublished %d site(s):" % len(done))
        for c, u in done:
            print("  %-14s %s" % (c, u))
        print("  the first build takes a minute or two; a 404 before then is")
        print("  the site deploying, not a bad push")


if __name__ == "__main__":
    main()
