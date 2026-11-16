"""Map one street against the USGS DEM grid.

Top panel  : the whole street over hillshaded terrain, discontinuities in red,
             with boxes showing where the detail panels zoom.
Lower panels: native DEM cells drawn as actual squares, the cell containing
             each centerline point outlined, points on top, flagged points red.

Individual cells are sub-pixel at street scale, which is why the detail panels
exist -- they are the only place the grid is actually visible.

This is the one plot in the project that works in the DEM's own frame rather
than UTM 10N: its whole subject is the DEM grid, and the grid is only square
and axis-aligned in the DEM's own CRS. Axes are therefore in that CRS's unit --
US survey feet for the county collects, metres for CA_SanFrancisco_B23 -- while
--window and every elevation stay in metres like the rest of the project.

Units are read from the mosaic, never assumed. See add_elevation.vertical_unit_m.

Usage:
    python map_street_cells.py                          # AIRWAY BL
    python map_street_cells.py --street "S VASCO RD"
    python map_street_cells.py --window 30 --disc-threshold 5
"""

import argparse
import glob
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from matplotlib.colors import LightSource, Normalize
from matplotlib.patches import Rectangle
from rasterio.windows import from_bounds

from add_elevation import (DEMDIR, VRT_NAME, build_vrt, horizontal_crs,
                           res_metres, snap_unit, unit_abbr, vertical_unit_m)
from extract_centerline_latlon import DEFAULT_CITY, SPACING, points_path
from plot_street_profiles import DISC_CM


def read_dem(vrt, bounds, v_unit):
    """Read a window of the DEM mosaic. Bounds are DEM CRS units, values metres.

    Reading through the VRT rather than looping tiles means a window straddling
    a tile edge comes back whole -- the OPR tiles do not overlap.

    `v_unit` is metres per vertical unit, from the mosaic. It was a foot
    constant here
    for as long as every collect counted feet; San Francisco's counts metres,
    and the constant would have shrunk its hills to a third.
    """
    west, south, east, north = bounds
    with rasterio.open(vrt) as src:
        nodata = src.nodata
        win = from_bounds(west, south, east, north, transform=src.transform)
        arr = src.read(1, window=win, boundless=True,
                       fill_value=nodata).astype("float64")
    if nodata is not None:
        arr[arr == nodata] = np.nan
    return arr * v_unit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--street", default="AIRWAY BL")
    # Pre-existing break: main() has read args.city since the corpus moved under
    # derived/<city>/, but the option was never declared, so every invocation
    # died with AttributeError before reaching the plot.
    ap.add_argument("--city", default=DEFAULT_CITY,
                    help=f"city slug; reads derived/<city>/ (default {DEFAULT_CITY})")
    # 12 m, not the 40 m the 1 m DEM used: the panel's whole job is to make
    # individual cells visible, and 40 m of 1 ft cells is 131 across -- 17,000
    # squares whose white edges swamp the panel. 12 m gives 39 across, which is
    # what 40 m gave on the 1 m grid.
    ap.add_argument("--window", type=float, default=12.0,
                    help="detail panel side length in metres")
    ap.add_argument("--disc-threshold", type=float, default=DISC_CM)
    ap.add_argument("--spacing", type=float, default=SPACING,
                    help=f"which point corpus to read (default {SPACING:g} m)")
    ap.add_argument("--dem-dir", default=DEMDIR,
                    help=f"directory of DEM tiles (default {DEMDIR})")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    demdir = os.path.join(here, args.dem_dir)
    files = sorted(glob.glob(os.path.join(demdir, "*.tif")))
    if not files:
        print(f"No .tif in {args.dem_dir}/ -- run fetch_usgs_lidar.py first")
        return 1
    mos = build_vrt(files, os.path.join(demdir, VRT_NAME))
    vrt = mos.path
    # The DEM's own units, read from the mosaic rather than assumed.
    dem_crs = horizontal_crs(mos.crs)
    h_unit = snap_unit(res_metres(dem_crs, 1.0) or 1.0)
    v_unit = vertical_unit_m(mos.crs)
    u = unit_abbr(h_unit)
    epsg = dem_crs.to_epsg()
    frame = f"EPSG:{epsg}" if epsg else "DEM CRS"

    df = pd.read_parquet(os.path.join(here, points_path(args.spacing, "parquet", args.city)))
    st = df[df.display_name == args.street].copy()
    if st.empty:
        print(f"No points for {args.street!r}")
        return 1

    # Everything below is in DEM CRS units; --window arrives in metres.
    win_ft = args.window / h_unit
    flagged = st[st.elev_disc_cm > args.disc_threshold]
    # group flagged points into clusters to choose detail windows
    centers = []
    if not flagged.empty:
        f = flagged.sort_values(["OBJECTID", "dist_along_m"])
        cl = (f.OBJECTID.diff().ne(0) | f.dist_along_m.diff().gt(5)).cumsum()
        for _, g in f.groupby(cl):
            centers.append((g.dem_x.mean(), g.dem_y.mean(), len(g)))
    if not centers:
        centers = [(st.dem_x.median(), st.dem_y.median(), 0)]
    # merge clusters closer together than one window, otherwise the detail
    # panels show the same ground twice
    merged = []
    for cx, cy, cn in sorted(centers, key=lambda c: (-c[2], c[1])):
        for j, (mx, my, mn) in enumerate(merged):
            if np.hypot(cx-mx, cy-my) < win_ft:
                tot = mn + cn
                merged[j] = ((mx*mn + cx*cn)/tot, (my*mn + cy*cn)/tot, tot)
                break
        else:
            merged.append((cx, cy, cn))
    centers = merged[:3]

    pad = 60.0 / h_unit
    ob = (st.dem_x.min()-pad, st.dem_y.min()-pad,
          st.dem_x.max()+pad, st.dem_y.max()+pad)
    dem = read_dem(vrt, (np.floor(ob[0]), np.floor(ob[1]),
                         np.ceil(ob[2]), np.ceil(ob[3])), v_unit)
    oext = [np.floor(ob[0]), np.ceil(ob[2]), np.floor(ob[1]), np.ceil(ob[3])]

    ndet = len(centers)
    # A long thin street forced into a wide panel collapses to a sliver once the
    # axes are equal-aspect, so stand the overview up beside the detail panels.
    tall = (ob[3]-ob[1]) > 1.6*(ob[2]-ob[0])
    if tall:
        fig = plt.figure(figsize=(13.5, max(9.0, 5.0*ndet)), dpi=115)
        gs = fig.add_gridspec(ndet, 2, width_ratios=[1, 1.35],
                              wspace=0.24, hspace=0.30)
        ax = fig.add_subplot(gs[:, 0])
        det_axes = [gs[i, 1] for i in range(ndet)]
    else:
        fig = plt.figure(figsize=(6.2*max(ndet, 2), 11), dpi=115)
        # generous hspace: the overview's legend hangs below its axes, and at
        # 0.30 it lands on top of the detail panel's title.
        gs = fig.add_gridspec(2, ndet, height_ratios=[1.15, 1],
                              hspace=0.45, wspace=0.20)
        ax = fig.add_subplot(gs[0, :])
        det_axes = [gs[1, i] for i in range(ndet)]
    ls = LightSource(azdeg=315, altdeg=45)
    fin = np.isfinite(dem)
    filled = np.where(fin, dem, np.nanmedian(dem[fin]))
    # elevations are metres and pixels are DEM units -- give shade() the real
    # cell size in metres, or a foot grid exaggerates the relief by 3.28x.
    px_m = mos.res_m
    rgb = ls.shade(filled, cmap=plt.get_cmap("gist_earth"), blend_mode="soft",
                   vert_exag=4, dx=px_m, dy=px_m)
    ax.imshow(np.dstack([rgb[..., :3], fin.astype(float)]), extent=oext,
              origin="upper", interpolation="bilinear")
    ax.plot(st.dem_x, st.dem_y, ".", color="#1f4e79", ms=1.4,
            label=f"{args.spacing:g} m centerline points ({len(st):,})")
    if not flagged.empty:
        ax.plot(flagged.dem_x, flagged.dem_y, "o", color="#e00000", ms=6,
                mec="white", mew=0.8, zorder=6,
                label=f"discontinuity > {args.disc_threshold:g} cm ({len(flagged)})")
    for i, (cx, cy, n) in enumerate(centers):
        half = win_ft/2
        ax.add_patch(Rectangle((cx-half, cy-half), win_ft, win_ft,
                               fill=False, ec="#e00000", lw=1.6, zorder=7))
        ax.annotate(chr(66+i), (cx+half, cy+half), color="#e00000",
                    fontsize=13, fontweight="bold",
                    xytext=(4, 2), textcoords="offset points", zorder=8)
    ax.set_title(f"A.  {args.street} — {len(st):,} points, "
                 f"{st.OBJECTID.nunique()} segments\nover {mos.res:g} {u} "
                 f"DEM hillshade",
                 fontsize=10.5, loc="left")
    ax.set_xlabel(f"{frame} easting ({u})")
    ax.set_ylabel(f"{frame} northing ({u})")
    ax.ticklabel_format(style="plain")
    ax.tick_params(axis="x", labelrotation=45, labelsize=8)
    # legend below the axes: on a narrow street it would otherwise cover the road
    ax.legend(fontsize=8, loc="upper center", bbox_to_anchor=(0.5, -0.13),
              framealpha=0.95)
    ax.set_aspect("equal")
    ax.set_xlim(oext[0], oext[1])
    ax.set_ylim(oext[2], oext[3])

    # ---------- detail panels ----------
    for i, (cx, cy, n) in enumerate(centers):
        axd = fig.add_subplot(det_axes[i])
        half = win_ft/2
        w, s = np.floor(cx-half), np.floor(cy-half)
        e, nn = w+win_ft, s+win_ft
        sub = read_dem(vrt, (w, s, e, nn), v_unit)

        # every DEM cell drawn as a real square, edges visible. The step is the
        # mosaic's pixel size in CRS units -- 1.0 on a 1 ftUS collect, 0.25 on
        # San Francisco's metre grid. Using 1 there would draw every cell four
        # times too wide and the panel would not line up with the points.
        xs = w + np.arange(sub.shape[1] + 1) * mos.res
        ys = s + np.arange(sub.shape[0] + 1) * mos.res
        norm = Normalize(np.nanpercentile(sub, 2), np.nanpercentile(sub, 98))
        mesh = axd.pcolormesh(xs, ys, sub[::-1], cmap="viridis", norm=norm,
                              edgecolors="#ffffff", linewidth=0.25)

        inw = st[(st.dem_x >= w) & (st.dem_x < e)
                 & (st.dem_y >= s) & (st.dem_y < nn)]
        # outline the cell each point falls in
        for ce, cn in inw[["cell_e", "cell_n"]].drop_duplicates().itertuples(index=False):
            axd.add_patch(Rectangle((ce, cn), 1, 1, fill=False,
                                    ec="#ff8c00", lw=1.3, zorder=4))
        axd.plot(inw.dem_x, inw.dem_y, "o", color="#111111", ms=3.2,
                 mec="white", mew=0.6, zorder=5)
        fl = inw[inw.elev_disc_cm > args.disc_threshold]
        if not fl.empty:
            axd.plot(fl.dem_x, fl.dem_y, "o", color="#e00000", ms=6.5,
                     mec="white", mew=0.9, zorder=6)

        axd.set_xlim(w, e)
        axd.set_ylim(s, nn)
        axd.set_aspect("equal")
        axd.set_title(f"{chr(66+i)}.  {args.window:g} m window — "
                      f"{len(inw)} points, {len(fl)} flagged\n"
                      f"relief {np.nanmax(sub)-np.nanmin(sub):.2f} m across the window",
                      fontsize=9.5, loc="left")
        axd.set_xlabel(f"easting ({u})")
        axd.tick_params(axis="x", labelrotation=45, labelsize=8)
        if i == 0 or tall:
            axd.set_ylabel(f"northing ({u})")
        axd.ticklabel_format(style="plain")
        fig.colorbar(mesh, ax=axd, shrink=0.85, label="elevation (m)")

    fig.suptitle(f"{args.street} — centerline points on the USGS 3DEP "
                 f"{mos.res:g} {u} grid\n"
                 "orange outlines: the DEM cell containing each point   ·   "
                 "red: elev_disc_cm above threshold",
                 fontsize=12.5, y=0.985)
    out = args.out or os.path.join(
        here, "derived",
        "map_cells_" + re.sub(r"[^A-Za-z0-9]+", "_", args.street).strip("_") + ".png")
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {out}")
    print(f"  {len(st):,} points, {len(flagged)} flagged, {len(centers)} detail window(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
