"""Fetch USGS 3DEP 1 m lidar DEM tiles covering the street-centerline extent.

Queries the USGS National Map (TNM) Access API for 1 m DEM GeoTIFFs
intersecting a bounding box, then downloads them with resume support.

The bbox defaults to the extent of the source centerline GeoJSON plus a buffer,
so the tiles always match the data rather than a hardcoded guess.

Several lidar projects can overlap the same tile footprint; --project keeps the
set coherent (one acquisition, one date, one vertical reference) instead of
mixing a full-coverage tile with a sliver from a neighbouring county's flight.

Usage:
    python fetch_dem.py --dry-run          # list tiles + total size, download nothing
    python fetch_dem.py                    # fetch CA_AlamedaCounty_2021_B21 tiles
    python fetch_dem.py --project ""       # no project filter (may mix acquisitions)
    python fetch_dem.py --bbox -121.9 37.6 -121.6 37.8
"""

import argparse
import json
import math
import os
import sys
import urllib.parse
import urllib.request

# The script that writes the centerline owns its path; importing it keeps the
# two from drifting apart -- they already did once, when the file moved into
# streets/ and this bbox lookup kept pointing at the old location.
from fetch_livermore_street_centerlines import DEFAULT_OUT as SRC_GEOJSON

TNM_API = "https://tnmaccess.nationalmap.gov/api/v1/products"
DATASET = "Digital Elevation Model (DEM) 1 meter"
DEFAULT_PROJECT = "CA_AlamedaCounty_2021_B21"
OUTDIR = "dem"
UA = {"User-Agent": "stormdrain-dem-fetch/1.0"}


def data_bbox(path, buffer_m):
    """Extent of the centerline GeoJSON, expanded by buffer_m."""
    with open(path, encoding="utf-8-sig") as fh:
        feats = json.load(fh)["features"]
    xs, ys = [], []
    for f in feats:
        g = f.get("geometry")
        if not g:
            continue
        parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        for part in parts:
            for c in part:
                xs.append(c[0])
                ys.append(c[1])
    lat_mid = (min(ys) + max(ys)) / 2
    dlat = buffer_m / 110574.0
    dlon = buffer_m / (111320.0 * math.cos(math.radians(lat_mid)))
    return (min(xs) - dlon, min(ys) - dlat, max(xs) + dlon, max(ys) + dlat)


def query_tiles(bbox, project):
    q = urllib.parse.urlencode({
        "datasets": DATASET,
        "bbox": ",".join(f"{v:.6f}" for v in bbox),
        "prodFormats": "GeoTIFF",
        "max": 200,
    })
    req = urllib.request.Request(f"{TNM_API}?{q}", headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        payload = json.load(r)

    items = payload.get("items", [])
    if project:
        items = [i for i in items if project in (i.get("title") or "")]
    # One entry per tile footprint; TNM can list duplicates across formats.
    seen, out = set(), []
    for i in items:
        url = i.get("downloadURL")
        if url and url not in seen:
            seen.add(url)
            out.append(i)
    return out, payload.get("total")


def content_length(url):
    """Authoritative size from the server.

    The TNM API's own sizeInBytes runs ~256 bytes short of what S3 actually
    serves, so it is only good enough for a human-facing total -- checking
    completeness against it re-downloads every already-complete file.
    """
    req = urllib.request.Request(url, headers=UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=120) as r:
        cl = r.headers.get("Content-Length")
    return int(cl) if cl else None


def download(url, dest):
    """Resumable GET. Returns True if the file ends up complete."""
    expected = content_length(url)
    have = os.path.getsize(dest) if os.path.exists(dest) else 0

    if expected:
        if have == expected:
            print(f"  already complete ({have/1e6:.1f} MB), skipping")
            return True
        if have > expected:
            print(f"  local file larger than server copy "
                  f"({have} > {expected}); re-fetching")
            os.remove(dest)
            have = 0

    headers = dict(UA)
    if have:
        headers["Range"] = f"bytes={have}-"
        print(f"  resuming at {have/1e6:.1f} MB")

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        resumed = have and r.status == 206
        if have and not resumed:
            print("  server ignored Range; restarting from 0")
        with open(dest, "ab" if resumed else "wb") as fh:
            _stream(r, fh, have if resumed else 0, expected)

    size = os.path.getsize(dest)
    ok = (not expected) or size == expected
    print(f"  {'ok' if ok else 'SIZE MISMATCH'}: {size/1e6:.1f} MB"
          + ("" if ok else f" (expected {expected} bytes, got {size})"))
    return ok


def _stream(resp, fh, start, expected):
    done = start
    last = -1
    while True:
        chunk = resp.read(1 << 20)
        if not chunk:
            break
        fh.write(chunk)
        done += len(chunk)
        if expected:
            pct = int(done * 100 / expected)
            if pct >= last + 10:
                last = pct
                print(f"    {pct:3d}%  {done/1e6:.0f}/{expected/1e6:.0f} MB", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"),
                    help="override the auto-derived extent")
    ap.add_argument("--centerline", default=SRC_GEOJSON,
                    help=f"GeoJSON whose extent sets the bbox "
                         f"(default {SRC_GEOJSON})")
    ap.add_argument("--buffer", type=float, default=500.0,
                    help="metres of padding around the data extent (default 500)")
    ap.add_argument("--project", default=DEFAULT_PROJECT,
                    help='lidar project substring; "" to disable filtering')
    ap.add_argument("--outdir", default=OUTDIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    if args.bbox:
        bbox = tuple(args.bbox)
    else:
        src = os.path.join(here, args.centerline)
        if not os.path.exists(src):
            raise SystemExit(
                f"no centerline GeoJSON at {src}\n"
                "run fetch_livermore_street_centerlines.py first, or pass"
                " --centerline / --bbox")
        bbox = data_bbox(src, args.buffer)
    print("bbox  : " + ", ".join(f"{v:.6f}" for v in bbox))

    tiles, total = query_tiles(bbox, args.project)
    print(f"TNM   : {total} product(s) intersect; "
          f"{len(tiles)} match project {args.project or '(any)'}")
    if not tiles:
        print("No tiles matched. Try --project '' to see all overlapping products.")
        return 1

    nbytes = sum(t.get("sizeInBytes") or 0 for t in tiles)
    for t in tiles:
        print(f"  - {t['title']}  ({(t.get('sizeInBytes') or 0)/1e6:.1f} MB)")
    print(f"total : ~{nbytes/1e9:.2f} GB into {args.outdir}/  "
          f"(API estimate; exact size comes from Content-Length per file)")

    if args.dry_run:
        return 0

    outdir = os.path.join(here, args.outdir)
    os.makedirs(outdir, exist_ok=True)

    failures = []
    for t in tiles:
        url = t["downloadURL"]
        dest = os.path.join(outdir, os.path.basename(urllib.parse.urlparse(url).path))
        print(f"\n{os.path.basename(dest)}")
        try:
            if not download(url, dest):
                failures.append(dest)
        except Exception as exc:                      # noqa: BLE001
            print(f"  FAILED: {exc}")
            failures.append(dest)

    print(f"\n{len(tiles) - len(failures)}/{len(tiles)} tiles complete in {outdir}")
    if failures:
        print("incomplete (re-run to resume): "
              + ", ".join(os.path.basename(f) for f in failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
