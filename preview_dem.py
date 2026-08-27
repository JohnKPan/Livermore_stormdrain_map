"""Render quick-look PNGs of the 3DEP 1 ft OPR DEM tiles.

A raw elevation ramp shows almost nothing on urban terrain -- the whole city
sits inside a ~100 m range, so everything reads as one flat wash. Hillshading
is what makes the surface legible, so both views blend a terrain colour ramp
with a shaded relief.

Outputs (into derived/):
    dem_overview.png  - the whole mosaic decimated to 10 m, street network over it
    dem_detail.png    - 1 km window at native 1 ft, showing road crown and gutters

Axes are in the DEM's own CRS (EPSG:6420, US survey feet); elevations are
converted to metres, which is the unit every other output in this project uses.

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
from rasterio.enums import Resampling
from rasterio.windows import from_bounds

from add_elevation import DEM_CRS, DEMDIR, FT_TO_M, VRT_NAME, build_vrt
from fetch_livermore_street_centerlines import DEFAULT_OUT as SRC_GEOJSON

OUTDIR = "derived"


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


def overview(vrt, streets, out_png, res=10.0):
    """Decimated read of the whole mosaic -- at native 1 ft it is ~7 GB.

    Nearest, not average: NoData is -999999, and averaging it into the edge
    cells would swamp the colour ramp instead of staying masked.
    """
    step = max(1, int(round(res / FT_TO_M)))     # decimation factor, in pixels
    with rasterio.open(vrt) as src:
        oh, ow = src.height // step, src.width // step
        elev = src.read(1, out_shape=(oh, ow),
                        resampling=Resampling.nearest).astype("float64")
        nodata = src.nodata
        left, top = src.transform.c, src.transform.f

    elev[elev == nodata] = np.nan
    elev *= FT_TO_M                              # ftUS -> metres
    h, w = elev.shape
    extent = [left, left + w * step, top - h * step, top]

    fig, ax = plt.subplots(figsize=(13, 11), dpi=130)
    # cells are `step` feet across; shade() needs that in the same unit as elev
    ax.imshow(shaded(elev, dx=step * FT_TO_M, dy=step * FT_TO_M),
              extent=extent, origin="upper", interpolation="bilinear")
    for x, y in streets:
        ax.plot(x, y, color="#0b3d91", lw=0.35, alpha=0.75,
                solid_capstyle="round")
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_title(f"3DEP 1 ft OPR DEM — CA_AlamedaCounty_2021_B21, shown at "
                 f"{step * FT_TO_M:.1f} m\nstreet centerlines in blue", fontsize=11)
    ax.set_xlabel("CA zone 3 easting (ftUS)")
    ax.set_ylabel("CA zone 3 northing (ftUS)")
    ax.ticklabel_format(style="plain")
    fig.colorbar(plt.cm.ScalarMappable(
        norm=plt.Normalize(np.nanpercentile(elev, 1), np.nanpercentile(elev, 99)),
        cmap="terrain"), ax=ax, shrink=0.7, label="elevation (m, NAVD88)")
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)
    print(f"  {out_png}  ({elev.shape[1]}x{elev.shape[0]} @ {step * FT_TO_M:.1f} m)")


def detail(vrt, streets, out_png, lat, lon, side_m):
    """Native 1 ft window, so the road surface itself is visible."""
    tr = Transformer.from_crs("EPSG:4326", DEM_CRS, always_xy=True)
    cx, cy = tr.transform(lon, lat)
    half = (side_m / FT_TO_M) / 2                # side_m is metres, bounds are feet
    bounds = (cx - half, cy - half, cx + half, cy + half)

    with rasterio.open(vrt) as src:
        win = from_bounds(*bounds, transform=src.transform)
        elev = src.read(1, window=win, boundless=True,
                        fill_value=src.nodata).astype("float64")
        elev[elev == src.nodata] = np.nan
    elev *= FT_TO_M
    if not np.isfinite(elev).any():
        print("  detail window falls outside the mosaic; skipped")
        return

    extent = [bounds[0], bounds[2], bounds[1], bounds[3]]
    fig, ax = plt.subplots(figsize=(11, 10), dpi=130)
    # Low vertical exaggeration: the crown-to-gutter drop is only a few cm.
    ax.imshow(shaded(elev, vert_exag=12.0, dx=FT_TO_M, dy=FT_TO_M),
              extent=extent, origin="upper", interpolation="nearest")
    for x, y in streets:
        ax.plot(x, y, color="#d81b60", lw=1.1, alpha=0.9)
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_title(f"Native 1 ft detail — {side_m:g} m window at {lat:.4f}, {lon:.4f}\n"
                 f"centerlines in pink; road crown and gutter lines visible in the relief",
                 fontsize=11)
    ax.set_xlabel("CA zone 3 easting (ftUS)")
    ax.set_ylabel("CA zone 3 northing (ftUS)")
    ax.ticklabel_format(style="plain")
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)
    v = elev[np.isfinite(elev)]
    print(f"  {out_png}  ({elev.shape[1]}x{elev.shape[0]} @ 1 ft, "
          f"{v.min():.1f}-{v.max():.1f} m)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--res", type=float, default=10.0, help="overview resolution (m)")
    ap.add_argument("--detail-lat", type=float, default=37.6819)
    ap.add_argument("--detail-lon", type=float, default=-121.7680)
    ap.add_argument("--detail-m", type=float, default=1000.0)
    ap.add_argument("--dem-dir", default=DEMDIR,
                    help=f"directory of DEM tiles (default {DEMDIR})")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    demdir = os.path.join(here, args.dem_dir)
    files = sorted(glob.glob(os.path.join(demdir, "*.tif")))
    if not files:
        print(f"No .tif in {args.dem_dir}/ — run fetch_usgs_lidar.py first")
        return 1
    os.makedirs(os.path.join(here, OUTDIR), exist_ok=True)
    mos = build_vrt(files, os.path.join(demdir, VRT_NAME))
    vrt = mos.path

    print(f"{len(mos.sources)} tile(s) -> {mos.width:,} x {mos.height:,} px mosaic "
          f"at {mos.res:g} units/px; reprojecting street network for overlay...")
    streets = street_lines(os.path.join(here, SRC_GEOJSON), DEM_CRS)

    overview(vrt, streets, os.path.join(here, OUTDIR, "dem_overview.png"), args.res)
    detail(vrt, streets, os.path.join(here, OUTDIR, "dem_detail.png"),
           args.detail_lat, args.detail_lon, args.detail_m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
