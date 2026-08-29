#!/usr/bin/env python3
"""
fetch_usgs_lidar.py
===================

Find and download USGS 3DEP high-resolution lidar-derived DEMs for the
San Francisco Bay Area (or any AOI) via the USGS "The National Map" (TNM)
Access API.

Why OPR?
--------
USGS distributes elevation at several resolutions:

    * "Digital Elevation Model (DEM) 1 meter"  -> national 1 m product (resampled)
    * "Original Product Resolution (OPR) DEM"   -> the collection's NATIVE grid
    * "Lidar Point Cloud (LPC)"                 -> raw/classified points (LAZ)

For the Bay Area the OPR DEMs are gridded at ~0.25 m (2023 CA_SanFrancisco
collect) up to 0.5 m (older collects). That is your ~0.3 m / ~1 survey-foot
resolution. This script defaults to OPR.

The API returns direct, public, no-auth GeoTIFF tile URLs on the USGS S3
bucket (prd-tnm.s3.amazonaws.com), so downloads are plain HTTPS GETs.

Typical workflow
----------------
1. See which projects/years cover your AOI (so you don't mix collects):

       python fetch_usgs_lidar.py --aoi sf --list-projects

2. Inspect / estimate size of the tiles for one project:

       python fetch_usgs_lidar.py --aoi sf --project CA_SanFrancisco --dry-run

3. Download them:

       python fetch_usgs_lidar.py --aoi sf --project CA_SanFrancisco --out ./dem_sf

You can pass an explicit bbox instead of a named AOI:

       python fetch_usgs_lidar.py --bbox -122.30 37.79 -122.24 37.83 --dry-run

Or derive the bbox from a vector AOI file (needs geopandas):

       python fetch_usgs_lidar.py --aoi-file sites.geojson --project CA_SanFrancisco

Multi-block deliveries
----------------------
A collect can be shipped in several delivery blocks (CA_AlamedaCounty_2021_B21
arrives as CA_AlamedaCo_1_2021 and CA_AlamedaCo_3_2021). Where a block boundary
crosses a tile, USGS publishes that tile once per block -- same filename, same
footprint, different URL -- each clipped to its own block with the rest NoData.
The two are a partition, not a duplicate and not a flight-line overlap: they
agree exactly on the hairline seam where they meet, and together they
reconstruct the tile with no gap.

Writing both to the basename would race and leave one fragment behind, so
colliding tiles are staged under <out>/_fragments/ and merged back into the
plain filename. <out>/*.tif is therefore always whole tiles.

Downloading needs only the Python standard library. Merging fragments needs
rasterio + numpy; without them the fragments are left in place and reported.
geopandas is optional (only to read an --aoi-file).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

TNM_PRODUCTS_URL = "https://tnmaccess.nationalmap.gov/api/v1/products"

# Full dataset tag strings the TNM API expects (must match exactly).
DATASETS = {
    "opr": "Original Product Resolution (OPR) Digital Elevation Model (DEM)",
    "1m":  "Digital Elevation Model (DEM) 1 meter",
    "lpc": "Lidar Point Cloud (LPC)",  # returns LAZ point clouds, not rasters
}

# Approximate bounding boxes: (west, south, east, north) in WGS84 degrees.
# These are convenience presets -- refine to your real AOI for production.
AOIS = {
    "bayarea":  (-123.02, 36.85, -121.20, 38.32),  # 9-county Bay Area (large!)
    "sf":       (-122.53, 37.70, -122.35, 37.83),  # City & County of San Francisco
    "alameda":  (-122.37, 37.45, -121.46, 37.91),  # Alameda County
    "oakland":  (-122.35, 37.70, -122.11, 37.89),  # Oakland / inner East Bay
    "contracosta": (-122.43, 37.72, -121.53, 38.10),
    "sanmateo": (-122.52, 37.11, -122.08, 37.71),
    "marin":    (-122.90, 37.79, -122.44, 38.32),
    "santaclara": (-122.20, 36.89, -121.21, 37.48),
    # Sized to contain the whole published street centerline, not just the
    # built-up core: the first cut of this box stopped at -121.82 and clipped
    # 4,352 centerline points off the western end of the network.
    "livermore": (-121.86, 37.62, -121.68, 37.74),  # Livermore + margin (E Alameda Co)
}


@dataclass
class Tile:
    title: str
    url: str
    project: str
    size_bytes: int = 0
    fmt: str = ""
    pub_date: str = ""
    meta_url: str = ""
    bbox: dict = field(default_factory=dict)

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]

    @property
    def delivery(self) -> str:
        """Delivery block folder under the project (e.g. CA_AlamedaCo_1_2021)."""
        parts = self.url.split("/")
        if "Projects" in parts:
            i = parts.index("Projects")
            if i + 2 < len(parts):
                return parts[i + 2]
        return "unknown"


def project_from_url(url: str) -> str:
    """Extract the collection/project name from a StagedProducts URL."""
    parts = url.split("/")
    if "Projects" in parts:
        i = parts.index("Projects")
        if i + 1 < len(parts):
            return parts[i + 1]
    return "unknown"


def query_tnm(bbox, dataset_tag, prod_formats="GeoTIFF", page=1000, verbose=True):
    """Page through the TNM Access API and yield Tile objects."""
    offset = 0
    total = None
    while True:
        params = {
            "datasets": dataset_tag,
            "bbox": "{:.6f},{:.6f},{:.6f},{:.6f}".format(*bbox),
            "prodFormats": prod_formats,
            "outputFormat": "JSON",
            "max": page,
            "offset": offset,
        }
        # LPC is delivered as LAZ, not GeoTIFF -- don't over-filter it away.
        if "Point Cloud" in dataset_tag:
            params.pop("prodFormats", None)

        url = TNM_PRODUCTS_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "fetch_usgs_lidar/1.0"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.load(resp)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    raise
                if verbose:
                    print(f"  API retry {attempt + 1} after error: {e}", file=sys.stderr)
                time.sleep(2 * (attempt + 1))

        if total is None:
            total = int(data.get("total", 0))
            if verbose:
                print(f"  API reports {total} matching item(s).", file=sys.stderr)

        items = data.get("items", []) or []
        if not items:
            break

        for it in items:
            dl = it.get("downloadURL") or it.get("urls", {}).get("TIFF") or ""
            if not dl:
                continue
            yield Tile(
                title=it.get("title", ""),
                url=dl,
                project=project_from_url(dl),
                size_bytes=int(it.get("sizeInBytes") or 0),
                fmt=it.get("format", ""),
                pub_date=it.get("publicationDate", "") or it.get("lastUpdated", ""),
                meta_url=it.get("metaUrl", ""),
                bbox=it.get("boundingBox", {}) or {},
            )

        offset += len(items)
        if offset >= total:
            break


def human_mb(nbytes: int) -> str:
    return f"{nbytes / 1_048_576:.1f} MB"


def probe_resolution(tile, timeout=60):
    """Metres per pixel for one tile, read from its header over the network.

    TNM publishes no resolution field. The API item's `body` is boilerplate --
    byte-identical across collects -- and nothing in the title or URL encodes
    the grid, so the only honest source is the raster. /vsicurl/ range-reads the
    GeoTIFF header rather than the tile, so this costs a few KB, not 20 MB.

    METRES, always. A 1 US survey foot grid is 0.3048 m and is FINER than a
    0.5 m one despite the larger number; ranking on the raw CRS value orders
    them backwards. add_elevation.py records dem_res in metres for this reason.

    Returns None when rasterio is absent or the probe fails -- downloading needs
    only the standard library, and losing the resolution column is not a reason
    to fail a fetch.
    """
    try:
        import rasterio  # type: ignore
    except ImportError:
        return None
    try:
        with rasterio.open("/vsicurl/" + tile.url) as ds:
            px = abs(ds.transform.a)
            if not ds.crs:
                return None
            factor = ds.crs.linear_units_factor[1]
            return px * factor
    except Exception:                                          # noqa: BLE001
        return None


def choose_project(by_proj, verbose=True):
    """Pick the collect to use for an AOI: finest resolution, then coverage.

    Resolution alone does not decide it. For San Jose's buffered AOI the Santa
    Clara and Alameda collects are BOTH 1 ftUS, and only the tile count --
    3,755 against 242 -- says which one is the city and which is the sliver
    where the buffer crosses a county line. So: finest metres-per-pixel wins,
    and an exact tie goes to whichever covers more of the AOI.

    Projects whose resolution cannot be probed are ranked last rather than
    dropped, so a total probe failure still returns the best-covered collect.
    """
    scored = []
    for proj in sorted(by_proj):
        ts = by_proj[proj]
        res = probe_resolution(ts[0])
        scored.append((res, len(ts), proj))
        if verbose:
            shown = f"{res:.4f} m" if res else "unknown"
            print(f"  {proj:<45} {len(ts):>6} tiles  {shown:>12}", file=sys.stderr)
    # (resolution ascending, tiles descending). None sorts last via the flag.
    scored.sort(key=lambda s: (s[0] is None, s[0] if s[0] else 0.0, -s[1]))
    return scored[0][2], scored


def download_one(tile: Tile, dest: str, verbose=True) -> tuple[str, str]:
    """Download a single tile to an explicit path. Returns (status, path).

    `dest` is passed in rather than built from tile.filename: tiles whose names
    collide across delivery blocks each need their own path (see plan_paths).
    The temp file is pid-tagged so two runs sharing an --out cannot collide on it.
    """
    tmp = f"{dest}.{os.getpid()}.part"

    # Skip if a complete file already exists (size matches API, when known).
    if os.path.exists(dest):
        if not tile.size_bytes or os.path.getsize(dest) == tile.size_bytes:
            return ("skip", dest)

    req = urllib.request.Request(tile.url, headers={"User-Agent": "fetch_usgs_lidar/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 20)  # 1 MiB
                    if not chunk:
                        break
                    fh.write(chunk)
            os.replace(tmp, dest)
            return ("ok", dest)
        except Exception as e:  # noqa: BLE001
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if attempt == 3:
                return (f"FAIL: {e}", dest)
            time.sleep(2 * (attempt + 1))
    return ("FAIL: unknown", dest)


FRAGMENT_DIR = "_fragments"


def valid_mask(arr, nodata):
    """Boolean mask of real samples, tolerating a NaN NoData value."""
    import numpy as np
    if nodata is None:
        return np.ones(arr.shape, dtype=bool)
    if isinstance(nodata, float) and nodata != nodata:  # NaN
        return ~np.isnan(arr)
    return arr != nodata


def plan_paths(tiles, out_dir):
    """Map each tile URL -> its download path, and group same-named tiles.

    Singletons go straight to <out>/<filename>. Names claimed by more than one
    URL are multi-block fragments (see module docstring) and are staged under
    <out>/_fragments/<delivery>__<filename> so neither copy overwrites the other.

    Returns (paths, groups); groups maps filename -> [tiles] for collisions only.
    """
    by_name: dict[str, list[Tile]] = {}
    for t in tiles:
        by_name.setdefault(t.filename, []).append(t)

    paths, groups = {}, {}
    for name, ts in by_name.items():
        if len(ts) == 1:
            paths[ts[0].url] = os.path.join(out_dir, name)
            continue
        groups[name] = ts
        for t in ts:
            paths[t.url] = os.path.join(out_dir, FRAGMENT_DIR,
                                        f"{t.delivery}__{name}")
    return paths, groups


def merge_fragments(groups, paths, out_dir, verbose=True) -> tuple[int, int]:
    """Reassemble each set of same-named fragments into one complete tile.

    The fragments partition the tile, so this is a NoData-priority fill: no
    blending, no resampling, no reprojection. Where fragments do overlap they
    are expected to agree exactly; a real disagreement is reported rather than
    silently resolved, since that would mean they are overlapping flight lines
    rather than a partition and the choice between them needs a human.

    Returns (merged, skipped).
    """
    if not groups:
        return (0, 0)
    try:
        import numpy as np
        import rasterio
    except ImportError:
        print(f"\n!! {len(groups)} tile(s) arrived as multi-block fragments in "
              f"{os.path.join(out_dir, FRAGMENT_DIR)}.\n"
              "   Merging them needs rasterio + numpy (pip install rasterio).\n"
              "   The fragments are complete on disk -- install, then re-run to merge.",
              file=sys.stderr)
        return (0, len(groups))

    merged = skipped = 0
    for name in sorted(groups):
        dest = os.path.join(out_dir, name)
        frags = sorted(paths[t.url] for t in groups[name])
        if not all(os.path.exists(f) for f in frags):
            print(f"  skip {name}: not all fragments present", file=sys.stderr)
            skipped += 1
            continue
        # Already merged, and no fragment has changed since. Nothing to do.
        if os.path.exists(dest) and os.path.getmtime(dest) >= max(
                os.path.getmtime(f) for f in frags):
            continue

        with rasterio.open(frags[0]) as src:
            profile = src.profile.copy()
            grid = (src.transform, src.width, src.height, src.dtypes[0])
            nodata = src.nodata
            tags = src.tags()
            overviews = src.overviews(1)
            struct = src.tags(ns="IMAGE_STRUCTURE")
            block = src.block_shapes[0] if src.is_tiled else None
            out = src.read(1)
        filled = valid_mask(out, nodata)

        ok, worst = True, 0.0
        for f in frags[1:]:
            with rasterio.open(f) as src:
                if (src.transform, src.width, src.height, src.dtypes[0]) != grid:
                    print(f"  SKIP {name}: fragments are not on a common grid",
                          file=sys.stderr)
                    ok = False
                    break
                arr = src.read(1)
            valid = valid_mask(arr, nodata)
            seam = valid & filled
            if seam.any():
                worst = max(worst, float(np.abs(arr[seam] - out[seam]).max()))
            take = valid & ~filled
            out[take] = arr[take]
            filled |= valid
        if not ok:
            skipped += 1
            continue
        if worst > 0:
            print(f"  !! {name}: fragments disagree by up to {worst:g} in the "
                  "overlap -- kept the first; inspect before trusting it",
                  file=sys.stderr)

        # Preserve the source's own structure (compression/predictor/tiling) and
        # CRS, which for these collects is a compound horizontal + vertical datum.
        if block:
            profile.update(tiled=True, blockysize=block[0], blockxsize=block[1])
        profile.update(driver="GTiff",
                       compress=struct.get("COMPRESSION", "LZW").lower(),
                       predictor=int(struct.get("PREDICTOR", 1)),
                       interleave=struct.get("INTERLEAVE", "BAND").lower())
        tmp = f"{dest}.{os.getpid()}.merging"
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.write(out, 1)
            dst.update_tags(**tags)
            if overviews:
                dst.build_overviews(overviews, rasterio.enums.Resampling.average)
        os.replace(tmp, dest)
        merged += 1
        if verbose:
            print(f"  merged {len(frags)} fragments -> {name} "
                  f"({filled.mean() * 100:.1f}% valid)")

    return (merged, skipped)


# A few common EPSG codes seen in Bay Area 3DEP OPR collects (ProjectedCSTypeGeoKey).
_EPSG_HINT = {
    6420: "EPSG:6420 NAD83(2011) / California zone 3 (ftUS)",
    6419: "EPSG:6419 NAD83(2011) / California zone 2 (ftUS)",
    6421: "EPSG:6421 NAD83(2011) / California zone 4 (ftUS)",
    2227: "EPSG:2227 NAD83 / California zone 3 (ftUS)",
    26910: "EPSG:26910 NAD83 / UTM zone 10N (meters)",
    6339: "EPSG:6339 NAD83(2011) / UTM zone 10N (meters)",
}


def _geotiff_res_stdlib(path: str):
    """Read (xres, yres, epsg) straight from GeoTIFF tags -- no geo libs needed."""
    import struct
    with open(path, "rb") as fh:
        b = fh.read()  # tag value offsets are absolute; read the whole (small) tile
    if b[:2] not in (b"II", b"MM"):
        return None
    en = "<" if b[:2] == b"II" else ">"
    ifd = struct.unpack(en + "I", b[4:8])[0]
    n = struct.unpack(en + "H", b[ifd:ifd + 2])[0]
    tags = {}
    for i in range(n):
        e = ifd + 2 + i * 12
        tag, typ, cnt = struct.unpack(en + "HHI", b[e:e + 8])
        tags[tag] = (typ, cnt, b[e + 8:e + 12])
    xres = yres = epsg = None
    if 33550 in tags:  # ModelPixelScaleTag -> (scaleX, scaleY, scaleZ) doubles
        off = struct.unpack(en + "I", tags[33550][2])[0]
        xres, yres = struct.unpack(en + "dd", b[off:off + 16])
    if 34735 in tags:  # GeoKeyDirectoryTag -> find ProjectedCSTypeGeoKey (3072)
        off = struct.unpack(en + "I", tags[34735][2])[0]
        cnt = tags[34735][1]
        keys = struct.unpack(en + ("H" * cnt), b[off:off + 2 * cnt])
        it = iter(keys[4:])
        for kid, loc, c, v in zip(it, it, it, it):
            if kid == 3072 and loc == 0:
                epsg = v
    if xres is None:
        return None
    crs = _EPSG_HINT.get(epsg, f"EPSG:{epsg}" if epsg else "unknown CRS")
    return (xres, yres, crs)


def report_resolution(path: str):
    """Report native pixel size + CRS. Uses rasterio if present, else reads tags."""
    try:
        import rasterio  # type: ignore
        with rasterio.open(path) as ds:
            xres, yres = ds.res
            return (xres, yres, str(ds.crs))
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        return None
    try:
        return _geotiff_res_stdlib(path)
    except Exception:  # noqa: BLE001
        return None


def resolve_bbox(args) -> tuple:
    if args.bbox:
        return tuple(args.bbox)
    if args.aoi_file:
        try:
            import geopandas as gpd  # type: ignore
        except ImportError:
            sys.exit("--aoi-file needs geopandas installed (pip install geopandas).")
        gdf = gpd.read_file(args.aoi_file)
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(4326)
        minx, miny, maxx, maxy = gdf.total_bounds
        return (float(minx), float(miny), float(maxx), float(maxy))
    if args.aoi:
        key = args.aoi.lower()
        if key not in AOIS:
            sys.exit(f"Unknown --aoi '{args.aoi}'. Choices: {', '.join(sorted(AOIS))}")
        return AOIS[key]
    sys.exit("Provide an AOI: --aoi <name>, --bbox W S E N, or --aoi-file <path>.")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Fetch USGS 3DEP high-res (OPR) lidar DEMs for an AOI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    src = ap.add_argument_group("Area of interest (choose one)")
    src.add_argument("--aoi", help=f"Named preset: {', '.join(sorted(AOIS))}")
    src.add_argument("--bbox", type=float, nargs=4, metavar=("W", "S", "E", "N"),
                     help="Bounding box in WGS84 lon/lat degrees.")
    src.add_argument("--aoi-file", help="Vector file (GeoJSON/SHP); bbox taken from its bounds. Needs geopandas.")

    ap.add_argument("--dataset", choices=sorted(DATASETS), default="opr",
                    help="opr=native res DEM (default), 1m=1 meter DEM, lpc=point cloud.")
    ap.add_argument("--project", default=None,
                    help="Only keep tiles whose project name contains this substring "
                         "(e.g. CA_SanFrancisco, 2023). Case-insensitive.")
    ap.add_argument("--out", default="./usgs_lidar", help="Output directory (default ./usgs_lidar).")
    ap.add_argument("--workers", type=int, default=6, help="Parallel download threads (default 6).")

    ap.add_argument("--list-projects", action="store_true",
                    help="Just list the projects/years covering the AOI, with tile counts & sizes.")
    ap.add_argument("--best-project", action="store_true",
                    help="Print the best collect for the AOI to stdout and exit -- "
                         "finest resolution, ties broken by coverage. Everything "
                         "else goes to stderr, so this is safe to capture in a "
                         "shell substitution. Needs rasterio to read resolution.")
    ap.add_argument("--check-project", metavar="NAME",
                    help="Exit non-zero if NAME is not a credible collect for this "
                         "AOI -- fewer than --min-coverage of the best-covered "
                         "project's tiles. Catches a --project left pointing at "
                         "the previous city's county, which otherwise downloads a "
                         "sliver and fails much later at the elevation join.")
    ap.add_argument("--min-coverage", type=float, default=0.25, metavar="FRAC",
                    help="Coverage floor for --check-project (default 0.25).")
    ap.add_argument("--dry-run", action="store_true",
                    help="List matching tiles and total size, but don't download.")
    ap.add_argument("--manifest", default=None,
                    help="Write a CSV manifest of matching tiles to this path.")
    ap.add_argument("--prune-fragments", action="store_true",
                    help="Delete the staged _fragments/ copies after a successful "
                         "merge. Off by default: keeping them makes a re-run a "
                         "no-op instead of a re-download.")
    args = ap.parse_args(argv)

    bbox = resolve_bbox(args)
    dataset_tag = DATASETS[args.dataset]
    print(f"Dataset : {dataset_tag}", file=sys.stderr)
    print(f"BBox    : W={bbox[0]} S={bbox[1]} E={bbox[2]} N={bbox[3]}", file=sys.stderr)

    tiles = list(query_tnm(bbox, dataset_tag))
    if args.project:
        needle = args.project.lower()
        tiles = [t for t in tiles if needle in t.project.lower() or needle in t.title.lower()]

    # De-duplicate on URL (overlapping API pages / projects can repeat).
    seen, uniq = set(), []
    for t in tiles:
        if t.url not in seen:
            seen.add(t.url)
            uniq.append(t)
    tiles = uniq

    if not tiles:
        print("No tiles matched. Try --list-projects to see coverage, or widen the AOI.")
        return 0

    # --- summarize by project -------------------------------------------------
    by_proj: dict[str, list[Tile]] = {}
    for t in tiles:
        by_proj.setdefault(t.project, []).append(t)

    if args.check_project:
        # Substring match, because --project itself matches on substring.
        needle = args.check_project.lower()
        hit = {p: ts for p, ts in by_proj.items() if needle in p.lower()}
        best_n = max(len(ts) for ts in by_proj.values())
        have = max((len(ts) for ts in hit.values()), default=0)
        share = have / best_n if best_n else 0.0
        biggest = max(by_proj, key=lambda p: len(by_proj[p]))
        print(f"{args.check_project}: {have} tile(s) here, "
              f"{share:.0%} of the best-covered collect ({biggest}, {best_n}).",
              file=sys.stderr)
        if share < args.min_coverage:
            print(f"\nThat is below --min-coverage {args.min_coverage:.0%}. This "
                  f"collect does not cover the AOI;\ndownloading it would give a "
                  f"sliver and fail at the elevation join.\nUse --project "
                  f"{biggest}, or --best-project to pick automatically.",
                  file=sys.stderr)
            return 2
        return 0

    if args.best_project:
        print(f"\nProbing {len(by_proj)} project(s) for resolution:", file=sys.stderr)
        best, scored = choose_project(by_proj)
        res = next(r for r, _, p in scored if p == best)
        print(f"-> {best}"
              + (f"  ({res:.4f} m, {len(by_proj[best])} tiles)" if res
                 else f"  ({len(by_proj[best])} tiles, resolution unknown)"),
              file=sys.stderr)
        print(best)                    # stdout, alone, for $(...) capture
        return 0

    if args.list_projects:
        print("\nProjects covering this AOI (pick one with --project):\n")
        print(f"{'PROJECT':<45} {'TILES':>6} {'SIZE':>12} {'RES':>10}")
        print("-" * 77)
        for proj in sorted(by_proj):
            ts = by_proj[proj]
            tot = sum(t.size_bytes for t in ts)
            res = probe_resolution(ts[0])
            shown = f"{res:.4f} m" if res else "-"
            print(f"{proj:<45} {len(ts):>6} {human_mb(tot):>12} {shown:>10}")
        print("\nRES is metres per pixel, read from one tile's header -- TNM publishes\n"
              "no resolution field. A 1 ftUS grid shows as 0.3048 m and is finer than\n"
              "a 0.5 m one. '-' means rasterio is missing or the probe failed.\n"
              "--best-project picks for you: finest resolution, ties broken by coverage.")
        return 0

    total_bytes = sum(t.size_bytes for t in tiles)
    paths, groups = plan_paths(tiles, args.out)
    n_distinct = len(tiles) - sum(len(v) - 1 for v in groups.values())
    print(f"\nMatched {len(tiles)} file(s) across {len(by_proj)} project(s), "
          f"~{human_mb(total_bytes)} total.", file=sys.stderr)
    if groups:
        # Distinguishing these two counts matters: the file count is what gets
        # downloaded, the tile count is what ends up in --out.
        print(f"          {n_distinct} distinct tile(s); {len(groups)} of them are "
              f"split across delivery blocks and will be merged.", file=sys.stderr)

    if args.manifest:
        with open(args.manifest, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["project", "title", "filename", "size_bytes", "format", "pub_date", "url"])
            for t in tiles:
                w.writerow([t.project, t.title, t.filename, t.size_bytes, t.fmt, t.pub_date, t.url])
        print(f"Wrote manifest: {args.manifest}", file=sys.stderr)

    if args.dry_run:
        for t in tiles[:20]:
            split = "  [fragment]" if t.filename in groups else ""
            print(f"  {t.delivery}  {t.filename}  "
                  f"({human_mb(t.size_bytes)}){split}")
        if len(tiles) > 20:
            print(f"  ... and {len(tiles) - 20} more")
        print("\n(dry run -- nothing downloaded)")
        return 0

    os.makedirs(args.out, exist_ok=True)
    if groups:
        os.makedirs(os.path.join(args.out, FRAGMENT_DIR), exist_ok=True)
    print(f"\nDownloading {len(tiles)} file(s) -> {args.out} "
          f"with {args.workers} worker(s)...\n")

    ok = skip = fail = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(download_one, t, paths[t.url]): t for t in tiles}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            t = futs[fut]
            status, path = fut.result()
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            else:
                fail += 1
            tag = {"ok": "  ok", "skip": "skip"}.get(status, "FAIL")
            # Qualify fragments by delivery block -- two same-named lines in the
            # log would otherwise look like the same tile downloaded twice.
            label = (f"{t.delivery}/{t.filename}" if t.filename in groups
                     else t.filename)
            print(f"[{i}/{len(tiles)}] {tag}  {label}"
                  + ("" if status in ("ok", "skip") else f"  ({status})"))

    print(f"\nDownloaded={ok} skipped={skip} failed={fail}")

    if groups:
        print(f"\nMerging {len(groups)} multi-block tile(s)...")
        merged, unmerged = merge_fragments(groups, paths, args.out)
        print(f"Merged={merged} already-current={len(groups) - merged - unmerged} "
              f"unmerged={unmerged}")
        if args.prune_fragments and not unmerged and not fail:
            for t in tiles:
                if t.filename in groups and os.path.exists(paths[t.url]):
                    os.remove(paths[t.url])
            try:
                os.rmdir(os.path.join(args.out, FRAGMENT_DIR))
            except OSError:
                pass
            print(f"Pruned staged fragments from {FRAGMENT_DIR}/")

    n_on_disk = len([f for f in os.listdir(args.out) if f.lower().endswith(".tif")])
    print(f"\nDone. {n_on_disk} tile(s) in {args.out} "
          f"(expected {n_distinct}).")

    sample = next((os.path.join(args.out, t.filename) for t in tiles
                   if os.path.exists(os.path.join(args.out, t.filename))), None)
    if sample:
        res = report_resolution(sample)
        if res:
            xres, yres, crs = res
            print(f"Sample native resolution: {abs(xres):.3f} x {abs(yres):.3f} "
                  f"map-units/pixel  |  CRS: {crs}")
            print("(If CRS units are US survey feet, ~1.0 ft/px; if meters, ~0.25-0.5 m/px.)")

    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
