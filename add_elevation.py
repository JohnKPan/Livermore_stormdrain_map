"""Enrich the 1 m centerline points with DEM cell references and elevation.

Adds, in place, to derived/segments_points_1m.{parquet,csv}:

    easting/northing     UTM 10N (EPSG:26910) -- the DEM's own CRS
    cell_e/cell_n        integer 1 m cell id; the DEM grid is aligned to whole
                         metres in UTM 10N, so floor(easting), floor(northing)
                         identifies a cell independently of which tile it is in
    cell_center_dist_m   how far the point sits from that cell's center
    same_cell_as_prev    True when the previous point on the segment shares the
                         cell -- at 1 m spacing on a diagonal street this
                         happens often, and those pairs carry no new elevation
    dem_tile             source tile
    elev_m               bilinear interpolation (the better estimator)
    elev_cell_m          the raw cell value (nearest)
    elev_disc_cm         |bilinear - cell|; large values flag a discontinuity
                         such as a curb, wall, or bridge edge
    bearing_deg          local heading, for generating curb offsets later
    FunctionalClass/RoadType  joined from the segment attributes

Usage:
    python add_elevation.py
    python add_elevation.py --no-csv        # parquet only (much faster)
"""

import argparse
import glob
import json
import os
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer

PROJ_CRS = "EPSG:26910"          # NAD83 / UTM zone 10N -- the DEM's CRS
VDATUM = "NAVD88"
POINTS_PQ = "derived/segments_points_1m.parquet"
POINTS_CSV = "derived/segments_points_1m.csv"
ENDPOINTS = "derived/segments_endpoints.csv"
DEMDIR = "dem"


def local_bearing(oid, e, n):
    """Heading in degrees from north, from the along-segment step."""
    idx = np.arange(len(oid))
    starts = np.r_[True, oid[1:] != oid[:-1]]
    lasts = np.r_[starts[1:], True]
    de = np.r_[np.diff(e), 0.0]
    dn = np.r_[np.diff(n), 0.0]
    # the final point of a segment has no forward step; reuse the previous one
    src = np.where(lasts, np.maximum(idx - 1, 0), idx)
    return (np.degrees(np.arctan2(de[src], dn[src])) + 360.0) % 360.0


def sample_tiles(files, e, n):
    """Bilinear + nearest elevation, one tile in memory at a time."""
    elev = np.full(len(e), np.nan)
    cell = np.full(len(e), np.nan)
    tile = np.full(len(e), "", dtype=object)

    for path in files:
        with rasterio.open(path) as src:
            b = src.bounds
            # stay 1 m inside so all four bilinear neighbours exist; the 12 m
            # tile overlap guarantees every point is interior to some tile
            todo = (np.isnan(elev)
                    & (e >= b.left + 1) & (e < b.right - 1)
                    & (n >= b.bottom + 1) & (n < b.top - 1))
            if not todo.any():
                continue
            arr = src.read(1)                      # float32, ~400 MB
            nodata = src.nodata
            inv = ~src.transform

            col, row = inv * (e[todo], n[todo])
            col = col - 0.5                        # AREA convention -> cell centers
            row = row - 0.5
            c0 = np.floor(col).astype(np.int32)
            r0 = np.floor(row).astype(np.int32)
            fx = (col - c0).astype(np.float64)
            fy = (row - r0).astype(np.float64)

            h, w = arr.shape
            acc = np.zeros(c0.size)
            wsum = np.zeros(c0.size)
            bad = np.zeros(c0.size, dtype=bool)
            for dc, dr, wt in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                               (0, 1, (1-fx)*fy), (1, 1, fx*fy)):
                v = arr[np.clip(r0+dr, 0, h-1), np.clip(c0+dc, 0, w-1)].astype(np.float64)
                bad |= (v == nodata)
                acc += wt * v
                wsum += wt
            bil = np.where(bad, np.nan, acc / wsum)

            # nearest = the cell actually containing the point
            nc = np.clip(np.rint(col).astype(np.int32), 0, w-1)
            nr = np.clip(np.rint(row).astype(np.int32), 0, h-1)
            nn = arr[nr, nc].astype(np.float64)
            nn[nn == nodata] = np.nan

            elev[todo] = bil
            cell[todo] = nn
            tile[todo] = os.path.basename(path)
            del arr
            print(f"  {os.path.basename(path)}: {int(todo.sum()):,} points")
    return elev, cell, tile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-csv", action="store_true", help="write parquet only")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    files = sorted(glob.glob(os.path.join(here, DEMDIR, "*.tif")))
    if not files:
        print(f"No .tif in {DEMDIR}/ -- run fetch_dem.py first")
        return 1

    df = pd.read_parquet(os.path.join(here, POINTS_PQ))
    base_cols = list(df.columns)
    print(f"points: {len(df):,}")

    tr = Transformer.from_crs("EPSG:4326", PROJ_CRS, always_xy=True)
    e, n = tr.transform(df.lon.values, df.lat.values)
    df["easting"] = np.round(e, 3)
    df["northing"] = np.round(n, 3)

    # Cell edges fall on whole metres in this CRS, so floor() is the cell id.
    df["cell_e"] = np.floor(e).astype(np.int32)
    df["cell_n"] = np.floor(n).astype(np.int32)
    df["cell_center_dist_m"] = np.round(
        np.hypot(e - (df.cell_e + 0.5), n - (df.cell_n + 0.5)), 4)

    oid = df.OBJECTID.values
    same = np.r_[False, (df.cell_e.values[1:] == df.cell_e.values[:-1])
                 & (df.cell_n.values[1:] == df.cell_n.values[:-1])
                 & (oid[1:] == oid[:-1])]
    df["same_cell_as_prev"] = same

    df["bearing_deg"] = np.round(local_bearing(oid, e, n), 2)

    print("sampling DEM:")
    elev, cell, tile = sample_tiles(files, e, n)
    df["elev_m"] = np.round(elev, 3)
    df["elev_cell_m"] = np.round(cell, 3)
    df["elev_disc_cm"] = np.round(np.abs(elev - cell) * 100, 2)
    df["dem_tile"] = tile

    attrs = pd.read_csv(os.path.join(here, ENDPOINTS),
                        usecols=["OBJECTID", "FunctionalClass", "RoadType"])
    df = df.merge(attrs, on="OBJECTID", how="left")

    order = base_cols + ["easting", "northing", "cell_e", "cell_n",
                         "cell_center_dist_m", "same_cell_as_prev", "bearing_deg",
                         "elev_m", "elev_cell_m", "elev_disc_cm", "dem_tile",
                         "FunctionalClass", "RoadType"]
    df = df[order]

    pq = os.path.join(here, POINTS_PQ)
    df.to_parquet(pq, index=False, compression="zstd")
    print(f"\nwrote {pq}  ({os.path.getsize(pq)/1e6:.1f} MB)")
    if not args.no_csv:
        csv = os.path.join(here, POINTS_CSV)
        df.to_csv(csv, index=False)
        print(f"wrote {csv}  ({os.path.getsize(csv)/1e6:.1f} MB)")

    meta = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": int(len(df)),
        "horizontal_crs": {"points_source": "EPSG:4326 (WGS84)",
                           "easting_northing": f"{PROJ_CRS} (NAD83 / UTM 10N)"},
        "vertical_datum": VDATUM,
        "elevation_units": "metres",
        "dem": {
            "product": "USGS 3DEP 1 m DEM",
            "project": "CA_AlamedaCounty_2021_B21",
            "grid": "1 m, AREA_OR_POINT=Area (values are cell areas, not nodes)",
            "tiles": [os.path.basename(f) for f in files],
            "nodata": -999999.0,
        },
        "accuracy_notes": {
            "absolute_vertical": "USGS 3DEP QL1/QL2 spec RMSEz <= 10 cm (not independently verified)",
            "relative_vertical_measured": "~1.2-2 cm RMS locally, measured on straight paved segments",
            "horizontal_registration_measured": "centerline vs road crown: mean offset -0.06 m, std 1.13 m",
            "gradient_guidance": "compute grades over >=25 m baselines; adjacent 1 m deltas are noise-dominated (SNR ~0.23)",
        },
        "columns": {
            "cell_e/cell_n": "SW corner of the containing 1 m cell, UTM 10N metres",
            "elev_m": "bilinear interpolation of the 4 surrounding cell centers",
            "elev_cell_m": "raw value of the containing cell",
            "elev_disc_cm": "|bilinear - cell|; >20 suggests a curb/wall/bridge edge",
            "same_cell_as_prev": "previous point on the segment shares this cell",
            "bearing_deg": "local heading, degrees from north",
        },
    }
    mp = os.path.join(here, "derived", "segments_points_1m.meta.json")
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    print(f"wrote {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
