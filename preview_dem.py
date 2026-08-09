"""Render quick-look PNGs of the 3DEP 1 m DEM tiles.

A raw elevation ramp shows almost nothing on urban terrain -- the whole city
sits inside a ~100 m range, so everything reads as one flat wash. Hillshading
is what makes the surface legible, so both views blend a terrain colour ramp
with a shaded relief.

Outputs (into derived/):
    dem_overview.png  - all four tiles mosaicked at 10 m, street network overlaid
    dem_detail.png    - 1 km window at native 1 m, showing road crown and gutters

Usage:
    python preview_dem.py
    python preview_dem.py --detail-lat 37.6819 --detail-lon -121.7680 --detail-m 1000
"""

import argparse
import glob
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from matplotlib.colors import LightSource
from pyproj import Transformer
from rasterio.merge import merge
from rasterio.windows import from_bounds

from fetch_livermore_street_centerlines import DEFAULT_OUT as SRC_GEOJSON

DEMDIR = "dem"
OUTDIR = "derived"
DEM_CRS = "EPSG:26910"


def shaded(elev, cmap="terrain", vert_exag=3.0, dx=1.0, dy=1.0):
    """Terrain ramp blended with hillshade; NaN stays transparent."""
    ls = LightSource(azdeg=315, altdeg=45)
    finite = np.isfinite(elev)
    filled = np.where(finite, elev, np.nanmedian(elev[finite]) if finite.any() else 0)
    rgb = ls.shade(filled, cmap=plt.get_cmap(cmap), blend_mode="soft",
                   vert_exag=vert_exag, dx=dx, dy=dy,
                   vmin=np.nanpercentile(elev[finite], 1) if finite.any() else 0,
                   vmax=np.nanpercentile(elev[finite], 99) if finite.any() else 1)
    # shade() already returns RGBA; keep RGB and substitute our own nodata mask.
    return np.dstack([rgb[..., :3], finite.astype(float)])


def street_lines(path, crs):
    """Centerline vertices reprojected into the DEM CRS, as (x, y) segments."""
    with open(path, encoding="utf-8-sig") as fh:
        feats = json.load(fh)["features"]
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    out = []
    for f in feats:
        g = f.get("geometry")
        if not g or g["type"] != "LineString":
            continue
        lon = [c[0] for c in g["coordinates"]]
        lat = [c[1] for c in g["coordinates"]]
        x, y = tr.transform(lon, lat)
        out.append((x, y))
    return out


def overview(files, streets, out_png, res=10.0):
    """Mosaic every tile at coarse resolution -- full res would be ~1.6 GB."""
    srcs = [rasterio.open(f) for f in files]
    mosaic, transform = merge(srcs, res=res, nodata=srcs[0].nodata)
    nodata = srcs[0].nodata
    for s in srcs:
        s.close()

    elev = mosaic[0].astype("float64")
    elev[elev == nodata] = np.nan
    h, w = elev.shape
    left, top = transform.c, transform.f
    extent = [left, left + w * res, top - h * res, top]

    fig, ax = plt.subplots(figsize=(13, 11), dpi=130)
    ax.imshow(shaded(elev, dx=res, dy=res), extent=extent, origin="upper",
              interpolation="bilinear")
    for x, y in streets:
        ax.plot(x, y, color="#0b3d91", lw=0.35, alpha=0.75,
                solid_capstyle="round")
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_title(f"3DEP 1 m DEM — CA_AlamedaCounty_2021_B21, mosaicked at {res:g} m\n"
                 f"street centerlines in blue", fontsize=11)
    ax.set_xlabel("UTM 10N easting (m)")
    ax.set_ylabel("UTM 10N northing (m)")
    ax.ticklabel_format(style="plain")
    fig.colorbar(plt.cm.ScalarMappable(
        norm=plt.Normalize(np.nanpercentile(elev, 1), np.nanpercentile(elev, 99)),
        cmap="terrain"), ax=ax, shrink=0.7, label="elevation (m, NAVD88)")
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)
    print(f"  {out_png}  ({elev.shape[1]}x{elev.shape[0]} @ {res:g} m)")


def detail(files, streets, out_png, lat, lon, side_m):
    """Native 1 m window, so the road surface itself is visible."""
    tr = Transformer.from_crs("EPSG:4326", DEM_CRS, always_xy=True)
    cx, cy = tr.transform(lon, lat)
    half = side_m / 2
    bounds = (cx - half, cy - half, cx + half, cy + half)

    elev = None
    for f in files:
        with rasterio.open(f) as src:
            b = src.bounds
            if not (bounds[0] < b.right and bounds[2] > b.left
                    and bounds[1] < b.top and bounds[3] > b.bottom):
                continue
            win = from_bounds(*bounds, transform=src.transform)
            arr = src.read(1, window=win, boundless=True, fill_value=src.nodata)
            arr = arr.astype("float64")
            arr[arr == src.nodata] = np.nan
            elev = arr if elev is None else np.where(np.isfinite(elev), elev, arr)
    if elev is None:
        print("  detail window falls outside all tiles; skipped")
        return

    extent = [bounds[0], bounds[2], bounds[1], bounds[3]]
    fig, ax = plt.subplots(figsize=(11, 10), dpi=130)
    # Low vertical exaggeration: at 1 m the crown-to-gutter drop is only a few cm.
    ax.imshow(shaded(elev, vert_exag=12.0), extent=extent, origin="upper",
              interpolation="nearest")
    for x, y in streets:
        ax.plot(x, y, color="#d81b60", lw=1.1, alpha=0.9)
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_title(f"Native 1 m detail — {side_m:g} m window at {lat:.4f}, {lon:.4f}\n"
                 f"centerlines in pink; road crown and gutter lines visible in the relief",
                 fontsize=11)
    ax.set_xlabel("UTM 10N easting (m)")
    ax.set_ylabel("UTM 10N northing (m)")
    ax.ticklabel_format(style="plain")
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)
    v = elev[np.isfinite(elev)]
    print(f"  {out_png}  ({elev.shape[1]}x{elev.shape[0]} @ 1 m, "
          f"{v.min():.1f}-{v.max():.1f} m)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=float, default=10.0, help="overview resolution (m)")
    ap.add_argument("--detail-lat", type=float, default=37.6819)
    ap.add_argument("--detail-lon", type=float, default=-121.7680)
    ap.add_argument("--detail-m", type=float, default=1000.0)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    files = sorted(glob.glob(os.path.join(here, DEMDIR, "*.tif")))
    if not files:
        print(f"No .tif in {DEMDIR}/ — run fetch_dem.py first")
        return 1
    os.makedirs(os.path.join(here, OUTDIR), exist_ok=True)

    print(f"{len(files)} tile(s); reprojecting street network for overlay...")
    streets = street_lines(os.path.join(here, SRC_GEOJSON), DEM_CRS)

    overview(files, streets, os.path.join(here, OUTDIR, "dem_overview.png"), args.res)
    detail(files, streets, os.path.join(here, OUTDIR, "dem_detail.png"),
           args.detail_lat, args.detail_lon, args.detail_m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
