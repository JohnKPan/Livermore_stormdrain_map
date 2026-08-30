"""Render quick-look PNGs of any folder of DEM tiles.

A raw elevation ramp shows almost nothing on urban terrain -- a whole city sits
inside a ~100 m range, so everything reads as one flat wash. Hillshading is what
makes the surface legible, so both views blend a terrain colour ramp with shaded
relief.

Two views:
    <prefix>_overview.png  the whole mosaic decimated to --res, streets over it
    <prefix>_detail.png    a small window at native resolution, where the road
                           crown and gutter lines become visible

NOTHING here is specific to a collect or a city. The CRS, the pixel size and the
linear unit are read from the rasters themselves, so a metric collect and a
1 ftUS one both come out right -- an earlier version multiplied every elevation
by 0.3048 and labelled the axes "CA zone 3", which was silently wrong for any
DEM but Livermore's. The street overlay is optional and skipped when absent.

Usage:
    python preview_dem.py --dem-dir dem_pleasanton --city pleasanton
    python preview_dem.py --dem-dir dem_san_jose --res 20
    python preview_dem.py --dem-dir ./some_tiles --no-streets
    python preview_dem.py --dem-dir dem_livermore --detail-lat 37.6819 \
        --detail-lon -121.7680 --detail-m 1000
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

from add_elevation import DEMDIR, VRT_NAME, build_vrt

OUTDIR = "derived"
# Where fetch_overture_streets.py writes, so --city alone finds the overlay.
STREETS_DIR = "streets/overture"


def mosaic_crs(vrt):
    """(crs, metres-per-unit, unit name) read from the mosaic, not assumed.

    A collect in US survey feet reports 0.3048; a metric one reports 1.0. Every
    elevation and every distance below is scaled by this, which is the whole
    reason this function exists.
    """
    with rasterio.open(vrt) as src:
        crs = src.crs
    if crs is None:
        return None, 1.0, "units"
    try:
        name, factor = crs.linear_units_factor
    except Exception:                                          # noqa: BLE001
        return crs, 1.0, "units"
    return crs, float(factor), name


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
    """Centerline vertices reprojected into the DEM CRS, as (x, y) segments.

    Handles MultiLineString as well as LineString: Overture carries both, and
    the portal-only version of this silently dropped every multi-part road.
    """
    with open(path, encoding="utf-8-sig") as fh:
        feats = json.load(fh)["features"]
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    out = []
    for f in feats:
        g = f.get("geometry")
        if not g:
            continue
        if g["type"] == "LineString":
            parts = [g["coordinates"]]
        elif g["type"] == "MultiLineString":
            parts = g["coordinates"]
        else:
            continue
        for co in parts:
            if len(co) < 2:
                continue
            x, y = tr.transform([c[0] for c in co], [c[1] for c in co])
            out.append((x, y))
    return out


def resolve_streets(here, args):
    """The centerline GeoJSON to overlay, or None.

    --streets wins; otherwise --city looks in streets/overture/. Absent is not
    an error: a preview of a bare tile folder is still useful.
    """
    if args.no_streets:
        return None
    if args.streets:
        p = os.path.join(here, args.streets)
        if not os.path.exists(p):
            raise SystemExit(f"--streets not found: {p}")
        return p
    if args.city:
        p = os.path.join(here, STREETS_DIR, f"{args.city}.geojson")
        if os.path.exists(p):
            return p
        print(f"  no centerline at {STREETS_DIR}/{args.city}.geojson, "
              f"drawing the DEM alone")
    return None


def overview(vrt, streets, out_png, res_m, mpu, unit, title):
    """Decimated read of the whole mosaic -- at native resolution it is GBs.

    Nearest, not average: NoData is a large negative sentinel, and averaging it
    into the edge cells would swamp the colour ramp instead of staying masked.
    """
    with rasterio.open(vrt) as src:
        px = abs(src.transform.a)                # pixel size, CRS units
        step = max(1, int(round(res_m / (px * mpu))))
        oh, ow = max(1, src.height // step), max(1, src.width // step)
        elev = src.read(1, out_shape=(oh, ow),
                        resampling=Resampling.nearest).astype("float64")
        nodata = src.nodata
        left, top = src.transform.c, src.transform.f

    if nodata is not None:
        elev[elev == nodata] = np.nan
    elev *= mpu                                  # CRS vertical units -> metres
    h, w = elev.shape
    cell = px * step                             # in CRS units
    extent = [left, left + w * cell, top - h * cell, top]
    shown_m = cell * mpu

    fig, ax = plt.subplots(figsize=(13, 11), dpi=130)
    ax.imshow(shaded(elev, dx=shown_m, dy=shown_m),
              extent=extent, origin="upper", interpolation="bilinear")
    for x, y in streets or []:
        ax.plot(x, y, color="#0b3d91", lw=0.35, alpha=0.75,
                solid_capstyle="round")
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_title(f"{title}, shown at {shown_m:.1f} m"
                 + ("\nstreet centerlines in blue" if streets else ""),
                 fontsize=11)
    ax.set_xlabel(f"easting ({unit})")
    ax.set_ylabel(f"northing ({unit})")
    ax.ticklabel_format(style="plain")
    fig.colorbar(plt.cm.ScalarMappable(
        norm=plt.Normalize(np.nanpercentile(elev, 1), np.nanpercentile(elev, 99)),
        cmap="terrain"), ax=ax, shrink=0.7, label="elevation (m)")
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)
    print(f"  {out_png}  ({w}x{h} @ {shown_m:.1f} m)")


def detail(vrt, streets, out_png, crs, mpu, unit, lat, lon, side_m):
    """Native-resolution window, so the road surface itself is visible."""
    with rasterio.open(vrt) as src:
        if lat is None or lon is None:
            # Centre of the mosaic, so a bare tile folder needs no coordinates.
            cx = src.transform.c + src.width * src.transform.a / 2
            cy = src.transform.f + src.height * src.transform.e / 2
            back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            lon, lat = back.transform(cx, cy)
        else:
            cx, cy = Transformer.from_crs(
                "EPSG:4326", crs, always_xy=True).transform(lon, lat)
        half = (side_m / mpu) / 2                # side_m is metres, bounds are CRS units
        bounds = (cx - half, cy - half, cx + half, cy + half)
        win = from_bounds(*bounds, transform=src.transform)
        elev = src.read(1, window=win, boundless=True,
                        fill_value=src.nodata if src.nodata is not None else 0
                        ).astype("float64")
        if src.nodata is not None:
            elev[elev == src.nodata] = np.nan
        px = abs(src.transform.a)
    elev *= mpu
    if not np.isfinite(elev).any():
        print("  detail window falls outside the mosaic; skipped")
        return

    extent = [bounds[0], bounds[2], bounds[1], bounds[3]]
    fig, ax = plt.subplots(figsize=(11, 10), dpi=130)
    # Low vertical exaggeration: the crown-to-gutter drop is only a few cm.
    ax.imshow(shaded(elev, vert_exag=12.0, dx=px * mpu, dy=px * mpu),
              extent=extent, origin="upper", interpolation="nearest")
    for x, y in streets or []:
        ax.plot(x, y, color="#d81b60", lw=1.1, alpha=0.9)
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_title(f"Native {px * mpu:.3g} m detail — {side_m:g} m window at "
                 f"{lat:.4f}, {lon:.4f}\n"
                 f"road crown and gutter lines visible in the relief",
                 fontsize=11)
    ax.set_xlabel(f"easting ({unit})")
    ax.set_ylabel(f"northing ({unit})")
    ax.ticklabel_format(style="plain")
    fig.tight_layout()
    fig.savefig(out_png)
    plt.close(fig)
    v = elev[np.isfinite(elev)]
    print(f"  {out_png}  ({elev.shape[1]}x{elev.shape[0]} @ {px * mpu:.3g} m, "
          f"{v.min():.1f}-{v.max():.1f} m)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dem-dir", default=DEMDIR,
                    help=f"directory of DEM tiles (default {DEMDIR})")
    ap.add_argument("--res", type=float, default=10.0,
                    help="overview resolution in METRES (default 10)")
    ap.add_argument("--city", default=None,
                    help=f"overlay {STREETS_DIR}/<city>.geojson if it exists")
    ap.add_argument("--streets", default=None,
                    help="explicit centerline GeoJSON to overlay")
    ap.add_argument("--no-streets", action="store_true",
                    help="draw the DEM alone")
    ap.add_argument("--detail-lat", type=float, default=None,
                    help="detail window centre (default: centre of the mosaic)")
    ap.add_argument("--detail-lon", type=float, default=None)
    ap.add_argument("--detail-m", type=float, default=1000.0,
                    help="detail window side in metres (default 1000)")
    ap.add_argument("--out-prefix", default=None,
                    help="output stem (default: the DEM folder's name), so two "
                         "collects do not overwrite each other's PNGs")
    ap.add_argument("--outdir", default=OUTDIR)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    demdir = os.path.join(here, args.dem_dir)
    files = sorted(glob.glob(os.path.join(demdir, "*.tif")))
    if not files:
        raise SystemExit(f"no .tif in {args.dem_dir}/ -- "
                         f"run fetch_usgs_lidar.py --out {args.dem_dir} first")
    outdir = os.path.join(here, args.outdir)
    os.makedirs(outdir, exist_ok=True)
    prefix = args.out_prefix or os.path.basename(demdir.rstrip("/\\")) or "dem"

    mos = build_vrt(files, os.path.join(demdir, VRT_NAME))
    vrt = mos.path
    crs, mpu, unit = mosaic_crs(vrt)
    # The collect name is in the tile filenames; no need to hardcode one.
    stem = os.path.basename(files[0])
    title = f"{len(files)} tile(s) from {args.dem_dir}"
    if "_OPR_" in stem:
        title = stem.split("_OPR_", 1)[1].rsplit("_", 1)[0]

    print(f"{len(mos.sources)} tile(s) -> {mos.width:,} x {mos.height:,} px "
          f"at {mos.res:g} {unit}/px ({mos.res * mpu:.3g} m); "
          f"CRS {crs.to_string() if crs else 'unknown'}")

    spath = resolve_streets(here, args)
    streets = street_lines(spath, crs) if (spath and crs) else None
    if streets:
        print(f"  {len(streets):,} centerline part(s) from {os.path.basename(spath)}")

    overview(vrt, streets, os.path.join(outdir, f"{prefix}_overview.png"),
             args.res, mpu, unit, title)
    detail(vrt, streets, os.path.join(outdir, f"{prefix}_detail.png"),
           crs, mpu, unit, args.detail_lat, args.detail_lon, args.detail_m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
