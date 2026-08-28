"""Map one street against the USGS DEM grid.

Top panel  : the whole street over hillshaded terrain, discontinuities in red,
             with boxes showing where the detail panels zoom.
Lower panels: native 1 ftUS cells drawn as actual squares, the cell containing
             each centerline point outlined, points on top, flagged points red.

Individual 1 ft cells are sub-pixel at street scale, which is why the detail
panels exist -- they are the only place the grid is actually visible.

This is the one plot in the project that works in the DEM's own frame rather
than UTM 10N: its whole subject is the DEM grid, and the grid is only square,
axis-aligned and whole-numbered in EPSG:6420. Axes are therefore ftUS, while
--window and every elevation stay in metres like the rest of the project.

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

from add_elevation import DEMDIR, FT_TO_M, VRT_NAME, build_vrt
from extract_centerline_latlon import DEFAULT_CITY, SPACING, points_path
from plot_street_profiles import DISC_CM


def read_dem(vrt, bounds):
    """Read a window of the DEM mosaic. Bounds are DEM CRS feet, values metres.

    Reading through the VRT rather than looping tiles means a window straddling
    a tile edge comes back whole -- the OPR tiles do not overlap.
    """
    west, south, east, north = bounds
    with rasterio.open(vrt) as src:
        win = from_bounds(west, south, east, north, transform=src.transform)
        arr = src.read(1, window=win, boundless=True,
                       fill_value=src.nodata).astype("float64")
    arr[arr == -999999.0] = np.nan
    return arr * FT_TO_M


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--street", default="AIRWAY BL")
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
    vrt = build_vrt(files, os.path.join(demdir, VRT_NAME)).path

    df = pd.read_parquet(os.path.join(here, points_path(args.spacing, "parquet", args.city)))
    st = df[df.display_name == args.street].copy()
    if st.empty:
        print(f"No points for {args.street!r}")
        return 1

    # Everything below is in DEM CRS feet; --window arrives in metres.
    win_ft = args.window / FT_TO_M
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

    pad = 60.0 / FT_TO_M
    ob = (st.dem_x.min()-pad, st.dem_y.min()-pad,
          st.dem_x.max()+pad, st.dem_y.max()+pad)
    dem = read_dem(vrt, (np.floor(ob[0]), np.floor(ob[1]),
                         np.ceil(ob[2]), np.ceil(ob[3])))
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
    # elevations are metres, pixels are feet -- give shade() the real cell size
    # or the hillshade exaggerates by a factor of 3.28.
    rgb = ls.shade(filled, cmap=plt.get_cmap("gist_earth"), blend_mode="soft",
                   vert_exag=4, dx=FT_TO_M, dy=FT_TO_M)
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
                 f"{st.OBJECTID.nunique()} segments\nover 1 ft DEM hillshade",
                 fontsize=10.5, loc="left")
    ax.set_xlabel("CA zone 3 easting (ftUS)")
    ax.set_ylabel("CA zone 3 northing (ftUS)")
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
        sub = read_dem(vrt, (w, s, e, nn))

        # every DEM cell drawn as a real square, edges visible
        xs = np.arange(w, w + sub.shape[1] + 1)
        ys = np.arange(s, s + sub.shape[0] + 1)
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
        axd.set_xlabel("easting (ftUS)")
        axd.tick_params(axis="x", labelrotation=45, labelsize=8)
        if i == 0 or tall:
            axd.set_ylabel("northing (ftUS)")
        axd.ticklabel_format(style="plain")
        fig.colorbar(mesh, ax=axd, shrink=0.85, label="elevation (m)")

    fig.suptitle(f"{args.street} — centerline points on the USGS 3DEP 1 ft grid\n"
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
