"""Enrich the resampled centerline points with DEM cell references and elevation.

Adds, in place, to derived/segments_points_<spacing>.{parquet,csv}:

    easting/northing     UTM 10N (EPSG:26910), metres -- the frame every plot
                         and map in this project works in
    dem_x/dem_y          the same point in the DEM's own CRS (EPSG:6420,
                         California zone 3, US survey feet)
    cell_e/cell_n        integer 1 ftUS cell id; the DEM grid is aligned to
                         whole feet in EPSG:6420, so floor(dem_x), floor(dem_y)
                         identifies a cell independently of which tile it is in
    cell_center_dist_m   how far the point sits from that cell's center, metres
    same_cell_as_prev    True when the previous point on the segment shares the
                         cell -- at 0.15 m spacing on a 0.3 m grid this is still
                         common, and those pairs carry no new elevation. It ran
                         62.9% at the old 0.1 m spacing, which is what argued
                         for coarsening it
    dem_tile             source tile
    dem_res              that tile's native resolution in METRES -- comparable
                         across projections, and above mosaic_res_m wherever a
                         coarser collect was upsampled into the mosaic
    elev_m               bilinear interpolation (the better estimator), metres
    elev_cell_m          the raw cell value (nearest), metres
    elev_disc_cm         |bilinear - cell|; large values flag a discontinuity
                         such as a curb, wall, or bridge edge
    bearing_deg          local heading, for generating curb offsets later
    road_class/subclass/road_flags  joined from the segment attributes

The DEM
-------
Source is the USGS 3DEP Original Product Resolution tiles in dem_livermore/,
fetched and merged by fetch_usgs_lidar.py: 1 US survey foot, EPSG:6420 with a
NAVD88 (ftUS) vertical datum, AREA_OR_POINT=Point. Elevations are converted to
metres on read, so every column this writes -- and everything downstream --
stays in metres, on NAVD88, exactly as it was under the old 1 m product.

Two details of that DEM differ from the 1 m product and are handled here:

  * The tiles do not overlap. They butt up exactly on a 3000 ft grid, so a
    point within one pixel of a tile edge has no bilinear neighbourhood inside
    its own tile. Sampling therefore goes through a VRT, which pulls those
    neighbours from the adjoining tile -- the job the 1 m product's 12 m tile
    overlap used to do for free.

  * AREA_OR_POINT=Point rather than Area. This does NOT move the sample
    location: GDAL's geotransform is corner-based either way, and the flag only
    says the value is a point measurement rather than a cell average. The
    half-pixel shift to cell centres below is correct for both, and was checked
    against the 1 m product -- which comes from the same lidar collect -- at
    59,472 points: centre convention 1.05 cm RMS, node convention 1.26 cm.

Usage:
    python add_elevation.py
    python add_elevation.py --no-csv        # parquet only (much faster)
    python add_elevation.py --spacing 1     # a different point corpus
"""

import argparse
import glob
import json
import os
import xml.sax.saxutils as sx
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

from extract_centerline_latlon import (DEFAULT_CITY, SPACING, label,
                                       points_path)

PROJ_CRS = "EPSG:26910"          # NAD83 / UTM zone 10N -- the project's frame
DEM_CRS = "EPSG:6420"            # NAD83(2011) / California zone 3 (ftUS) -- the DEM's
VDATUM = "NAVD88"
FT_TO_M = 1200.0 / 3937.0        # US survey foot -> metre, exact by definition
# endpoints live beside the points, under derived/<city>/
from extract_centerline_latlon import endpoints_path
DEMDIR = "dem_livermore"
VRT_NAME = "_mosaic.vrt"

# Read the mosaic in square blocks with a small halo, so the bilinear stencil of
# every point in a block is inside the buffer. 2048 is a multiple of the tiles'
# 512 px internal blocking, which keeps the reads aligned.
BLOCK = 2048
HALO = 2

# Everything this script adds. Listed once so the reindex at the end and the
# drop at the start cannot drift apart -- see main().
DERIVED_COLS = ["easting", "northing", "dem_x", "dem_y", "cell_e", "cell_n",
                "cell_center_dist_m", "same_cell_as_prev", "bearing_deg",
                "elev_m", "elev_cell_m", "elev_disc_cm", "dem_tile", "dem_res",
                "road_class", "subclass", "road_flags"]


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


@dataclass
class Source:
    """One tile, and where it lands in the mosaic."""
    path: str
    name: str
    crs: object
    bounds: tuple                 # left, bottom, right, top -- in its OWN crs
    res: float                    # its own crs units per pixel
    res_m: float                  # the same, in metres -- the comparable number
    block: tuple = (512, 512)     # internal tiling, a read-planning hint for GDAL
    xoff: int = -1                # placement in mosaic pixels
    yoff: int = -1
    xsize: int = 0
    ysize: int = 0


@dataclass
class Mosaic:
    """A VRT and enough about its sources to say where a sample came from."""
    path: str
    width: int
    height: int
    res: float                    # target grid, in target CRS units per pixel
    res_m: float                  # the same, in metres
    crs: object
    transform: object
    nodata: float
    warped: bool = False          # True when sources had to be reprojected
    sources: list = field(default_factory=list)
    misalign: float = 0.0         # worst source offset from the target grid, px

    @property
    def mixed_res(self):
        return len({round(s.res_m, 9) for s in self.sources}) > 1

    @property
    def mixed_crs(self):
        return len({s.crs for s in self.sources}) > 1


def res_metres(crs, res):
    """A resolution in metres, whatever unit its CRS counts in.

    The whole point: California zone 3 counts in US survey feet, so a "1.0" tile
    there is 0.3048 m and is FINER than a "0.5" tile in a metre-based CRS.
    Ranking sources on the raw number would order them backwards.
    """
    factor = 1.0
    try:
        lu = crs.linear_units_factor        # (name, metres per unit)
        factor = float(lu[1])
    except Exception:                        # noqa: BLE001 -- geographic CRS, etc.
        factor = 1.0
    return res * factor


def _read_sources(files):
    """Open every tile once and collect what the mosaic needs."""
    srcs = []
    for f in files:
        with rasterio.open(f) as ds:
            if round(ds.transform.a, 9) != round(-ds.transform.e, 9):
                raise SystemExit(f"{os.path.basename(f)} has non-square pixels "
                                 f"({ds.transform.a} x {-ds.transform.e}); "
                                 "not supported")
            r = round(ds.transform.a, 9)
            srcs.append(Source(path=os.path.abspath(f), name=os.path.basename(f),
                               crs=ds.crs, bounds=tuple(ds.bounds), res=r,
                               res_m=res_metres(ds.crs, r),
                               block=ds.block_shapes[0]))
    return srcs


def _order(sources):
    """Coarsest first, so the FINEST source is last and wins any overlap.

    Both a VRT and gdal.Warp composite later sources over earlier ones, so
    ordering is the whole mechanism behind "prefer highest resolution". Ties
    fall back to filename, which keeps a single-resolution mosaic byte-stable.
    """
    return sorted(sources, key=lambda s: (-s.res_m, s.name))


def _target_grid(sources, resolution, target_crs):
    """Pick the CRS and pixel size the mosaic will be built on."""
    finest = min(sources, key=lambda s: s.res_m)
    crs = target_crs if target_crs is not None else finest.crs
    if isinstance(crs, str):
        crs = rasterio.crs.CRS.from_string(crs)

    if resolution == "highest":
        res_m = min(s.res_m for s in sources)
    elif resolution == "lowest":
        res_m = max(s.res_m for s in sources)
    else:
        # a number is given in TARGET CRS units; convert once for comparison
        res_m = res_metres(crs, float(resolution))
    # back into the target CRS's own units
    unit = res_metres(crs, 1.0) or 1.0
    return crs, res_m / unit, res_m


def build_vrt(files, path, resolution="highest", resampling="bilinear",
              target_crs=None):
    """Mosaic the tiles into a VRT. Returns a Mosaic.

    Two paths, picked automatically:

      SAME CRS -- a plain VRT, written here. Sources may still differ in
        resolution: each declares the source rectangle it reads and the
        destination rectangle it covers, and GDAL resamples on read. Needs
        nothing but rasterio, so the ordinary single-collect pipeline runs in
        the uv venv exactly as before.

      DIFFERENT CRS -- a warped VRT via gdal.Warp, which reprojects as well as
        mosaics. A plain VRT cannot do this and neither can gdalbuildvrt. This
        is the one path that needs the conda-forge environment (environment.yml)
        for the osgeo bindings; the error says so if they are missing.

    Sources are ordered COARSEST FIRST so the finest-resolution tile wins
    wherever collects overlap -- compared in metres, since a 1 ftUS tile
    (0.3048 m) is finer than a 0.5 m one despite the larger number.
    """
    sources = _order(_read_sources(files))
    crs, tres, tres_m = _target_grid(sources, resolution, target_crs)
    nodata = next((s for s in sources), None) and None
    with rasterio.open(sources[0].path) as ds:
        nodata = ds.nodata
        dtype = ds.dtypes[0]

    if len({s.crs for s in sources}) > 1:
        return _build_warped(sources, path, crs, tres, tres_m, resampling,
                             nodata, dtype)
    return _build_plain(sources, path, crs, tres, tres_m, resampling, nodata, dtype)


def _build_plain(sources, path, crs, tres, tres_m, resampling, nodata, dtype):
    """Same-CRS mosaic, written as VRT XML directly. No osgeo needed."""
    x0 = min(s.bounds[0] for s in sources)
    y1 = max(s.bounds[3] for s in sources)
    width = int(round((max(s.bounds[2] for s in sources) - x0) / tres))
    height = int(round((y1 - min(s.bounds[1] for s in sources)) / tres))

    gdal_dtype = {"float32": "Float32", "float64": "Float64",
                  "int16": "Int16", "int32": "Int32"}.get(str(dtype), "Float32")
    out = [f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">',
           f"  <SRS>{sx.escape(crs.to_wkt())}</SRS>",
           f"  <GeoTransform>{x0}, {tres}, 0.0, {y1}, 0.0, {-tres}</GeoTransform>",
           '  <Metadata><MDI key="AREA_OR_POINT">Point</MDI></Metadata>',
           f'  <VRTRasterBand dataType="{gdal_dtype}" band="1">',
           f"    <NoDataValue>{nodata}</NoDataValue>",
           "    <ColorInterp>Gray</ColorInterp>"]

    # Paths are relative to the VRT's own directory, NOT bare basenames: sources
    # need not live beside the VRT (a _warped/ subdirectory, say), and a bare
    # basename would silently resolve to a same-named file that does sit beside
    # it -- reading the wrong tile rather than failing.
    vrt_dir = os.path.dirname(os.path.abspath(path))
    misalign = 0.0
    for s in sources:
        fx = (s.bounds[0] - x0) / tres
        fy = (y1 - s.bounds[3]) / tres
        s.xoff, s.yoff = int(round(fx)), int(round(fy))
        misalign = max(misalign, abs(fx - s.xoff), abs(fy - s.yoff))
        srcw = int(round((s.bounds[2] - s.bounds[0]) / s.res))
        srch = int(round((s.bounds[3] - s.bounds[1]) / s.res))
        s.xsize = max(1, int(round(srcw * s.res / tres)))
        s.ysize = max(1, int(round(srch * s.res / tres)))
        rel = os.path.relpath(s.path, vrt_dir).replace(os.sep, "/")
        rs = f' resampling="{resampling}"' if (s.xsize != srcw or s.ysize != srch) else ""
        out += [
            f"    <SimpleSource{rs}>",
            f'      <SourceFilename relativeToVRT="1">{sx.escape(rel)}</SourceFilename>',
            "      <SourceBand>1</SourceBand>",
            f'      <SourceProperties RasterXSize="{srcw}" RasterYSize="{srch}" '
            f'DataType="{gdal_dtype}" BlockXSize="{s.block[1]}" '
            f'BlockYSize="{s.block[0]}"/>',
            f'      <SrcRect xOff="0" yOff="0" xSize="{srcw}" ySize="{srch}"/>',
            f'      <DstRect xOff="{s.xoff}" yOff="{s.yoff}" '
            f'xSize="{s.xsize}" ySize="{s.ysize}"/>',
            f"      <NODATA>{nodata}</NODATA>",
            "    </SimpleSource>",
        ]
    out += ["  </VRTRasterBand>", "</VRTDataset>", ""]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out))

    from rasterio.transform import from_origin
    return Mosaic(path=path, width=width, height=height, res=tres, res_m=tres_m,
                  crs=crs, transform=from_origin(x0, y1, tres, tres),
                  nodata=nodata, warped=False, sources=sources, misalign=misalign)


def _build_warped(sources, path, crs, tres, tres_m, resampling, nodata, dtype):
    """Cross-CRS mosaic, in three stages.

    gdal.Warp(format="VRT") honours only its FIRST source dataset -- it warns
    about this and returns a mosaic that is otherwise NoData. GDAL's documented
    remedy is to mosaic same-projection sources first and warp the result, so:

        tiles -> a plain VRT per (CRS, resolution) group
              -> gdal.Warp each group VRT onto the target grid (one source each)
              -> a plain VRT over the warped group VRTs

    Grouping on (CRS, resolution) rather than CRS alone keeps every group
    internally uniform, so ordering the groups coarsest-first reproduces the
    per-tile "finest wins" rule exactly instead of approximating it.
    """
    try:
        from osgeo import gdal
    except ImportError:
        raise SystemExit(
            f"These {len(sources)} tiles span "
            f"{len({s.crs for s in sources})} projections, which needs a "
            "reprojecting mosaic.\n"
            "A plain VRT cannot reproject and neither can gdalbuildvrt; this "
            "path uses gdal.Warp,\n"
            "which needs the osgeo bindings. GDAL has no Windows wheels on "
            "PyPI at any version,\n"
            "so it comes from conda-forge:\n\n"
            "    .conda/micromamba.exe create -y -p .conda/env -f environment.yml\n"
            "    .conda/micromamba.exe run -p .conda/env python add_elevation.py ...\n\n"
            "Or keep to one projection at a time and run per-AOI with --dem-dir."
        ) from None
    gdal.UseExceptions()

    stem = os.path.splitext(path)[0]
    groups = {}
    for src in sources:                      # sources arrive coarsest-first
        groups.setdefault((src.crs.to_wkt(), round(src.res_m, 9)), []).append(src)

    parts = []
    for i, ((_wkt, res_m), group) in enumerate(groups.items()):
        gcrs = group[0].crs
        gres = group[0].res
        part = f"{stem}.part{i}.vrt"
        # Stage 1: an ordinary same-CRS mosaic of this group, at its own grid.
        _build_plain(group, part, gcrs, gres, res_m, resampling, nodata, dtype)

        if gcrs == crs:
            parts.append(part)               # already on the target grid
            continue
        # Stage 2: reproject it. One source, which is all Warp supports here.
        # targetAlignedPixels snaps every group to the same whole-pixel grid, so
        # they compose without the sub-pixel registration drift you get from
        # letting each inherit its own arbitrary origin.
        warped = f"{stem}.part{i}.warped.vrt"
        gdal.Warp(warped, part, format="VRT", dstSRS=crs.to_wkt(),
                  xRes=tres, yRes=tres, targetAlignedPixels=True,
                  resampleAlg=resampling, srcNodata=nodata, dstNodata=nodata,
                  multithread=True)
        parts.append(warped)

    # Stage 3: one plain VRT over the parts, still coarsest-first.
    part_sources = _read_sources(parts)
    mos = _build_plain(part_sources, path, crs, tres, tres_m, resampling,
                       nodata, dtype)
    # Report the real tiles, not the intermediate parts: provenance is per tile.
    mos.sources = sources
    mos.warped = True
    return mos


def locate_sources(mos, x, y, inside):
    """Which source tile each point sits on. Returns (names, res_m) arrays.

    Tested against each source's bounds in ITS OWN CRS, which is exact: a tile
    is an axis-aligned rectangle there, whereas its reprojected footprint is a
    rotated quadrilateral whose bounding box would claim ground it does not
    cover. Points are transformed once per distinct source CRS, not per tile.

    Sources are visited in mosaic order (coarsest to finest), so the last writer
    -- the finest tile covering the point -- wins, matching what the mosaic
    itself composites.
    """
    names = np.full(len(x), "", dtype=object)
    res = np.full(len(x), np.nan)

    by_crs = {}
    for s in mos.sources:
        by_crs.setdefault(s.crs, []).append(s)

    for src_crs, group in by_crs.items():
        if src_crs == mos.crs:
            px, py = x, y
        else:
            tr = Transformer.from_crs(mos.crs, src_crs, always_xy=True)
            px, py = tr.transform(x, y)
        for s in group:            # group order preserves mosaic order
            l, b, r, t = s.bounds
            hit = inside & (px >= l) & (px < r) & (py >= b) & (py < t)
            names[hit] = s.name
            res[hit] = s.res_m
    return names, res


def sample_mosaic(vrt_path, x, y):
    """Bilinear + nearest elevation for every point, in the DEM's own units.

    Points are binned onto BLOCK-sized tiles of the mosaic and each block is
    read once with a HALO margin, so a point near a block edge -- or a source
    tile edge -- still has all four bilinear neighbours in the buffer.
    """
    elev = np.full(len(x), np.nan)
    cell = np.full(len(x), np.nan)

    with rasterio.open(vrt_path) as src:
        nodata = src.nodata
        W, H = src.width, src.height
        col, row = (~src.transform) * (x, y)
        # AREA convention: integer coordinates are pixel edges. Shift so integers
        # land on the sample locations, making floor/frac correct for bilinear.
        col_c = col - 0.5
        row_c = row - 0.5

        pix_c = np.floor(col).astype(np.int64)
        pix_r = np.floor(row).astype(np.int64)
        inside = (pix_c >= 0) & (pix_c < W) & (pix_r >= 0) & (pix_r < H)

        bc = np.where(inside, pix_c // BLOCK, -1)
        br = np.where(inside, pix_r // BLOCK, -1)
        keys = np.unique(np.stack([bc[inside], br[inside]], axis=1), axis=0)
        span = BLOCK + 2 * HALO

        for i, (kc, kr) in enumerate(keys, 1):
            sel = (bc == kc) & (br == kr)
            c_off = int(kc) * BLOCK - HALO
            r_off = int(kr) * BLOCK - HALO

            # Clamp the read to the mosaic and pad the rest, rather than a
            # boundless read: predictable cost, and no nested VRT.
            buf = np.full((span, span), nodata, dtype="float64")
            c0w, r0w = max(0, c_off), max(0, r_off)
            c1w, r1w = min(W, c_off + span), min(H, r_off + span)
            if c1w > c0w and r1w > r0w:
                data = src.read(1, window=Window(c0w, r0w, c1w - c0w, r1w - r0w))
                buf[r0w - r_off:r1w - r_off, c0w - c_off:c1w - c_off] = data

            lc = col_c[sel] - c_off
            lr = row_c[sel] - r_off
            c0 = np.floor(lc).astype(np.int32)
            r0 = np.floor(lr).astype(np.int32)
            fx = lc - c0
            fy = lr - r0

            acc = np.zeros(c0.size)
            bad = np.zeros(c0.size, dtype=bool)
            for dc, dr, wt in ((0, 0, (1-fx)*(1-fy)), (1, 0, fx*(1-fy)),
                               (0, 1, (1-fx)*fy), (1, 1, fx*fy)):
                v = buf[np.clip(r0+dr, 0, span-1), np.clip(c0+dc, 0, span-1)]
                bad |= (v == nodata)
                acc += wt * v
            elev[sel] = np.where(bad, np.nan, acc)

            # nearest = the cell actually containing the point
            nn = buf[np.clip(np.rint(lr).astype(np.int32), 0, span-1),
                     np.clip(np.rint(lc).astype(np.int32), 0, span-1)]
            cell[sel] = np.where(nn == nodata, np.nan, nn)

            if i % 25 == 0 or i == len(keys):
                print(f"  block {i}/{len(keys)}  ({int(sel.sum()):,} points)")

    return elev, cell, pix_c, pix_r, inside


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-csv", action="store_true", help="write parquet only")
    ap.add_argument("--city", default=DEFAULT_CITY,
                    help=f"city slug; reads derived/<city>/ (default {DEFAULT_CITY})")

    ap.add_argument("--spacing", type=float, default=SPACING,
                    help=f"which point corpus to enrich (default {SPACING:g} m)")
    ap.add_argument("--vrt-resolution", default="highest",
                    help="target grid when sources differ in resolution: "
                         "highest (default), lowest, or a number in CRS units")
    ap.add_argument("--vrt-resampling", default="bilinear",
                    help="how to resample sources onto the target grid "
                         "(nearest, bilinear, cubic, average; default bilinear)")
    ap.add_argument("--dem-dir", default=DEMDIR,
                    help=f"directory of DEM tiles to sample (default {DEMDIR}). "
                         "Point this at another AOI's tiles; the VRT mosaic is "
                         "regenerated from whatever is in there.")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    demdir = os.path.join(here, args.dem_dir)
    files = sorted(glob.glob(os.path.join(demdir, "*.tif")))
    if not files:
        print(f"No .tif in {args.dem_dir}/ -- run fetch_usgs_lidar.py first")
        return 1

    pq = os.path.join(here, points_path(args.spacing, "parquet", args.city))
    if not os.path.exists(pq):
        print(f"No point corpus at {pq}\n"
              f"run: python extract_centerline_latlon.py --spacing {args.spacing:g} "
              f"--slim --parquet --no-csv")
        return 1

    df = pd.read_parquet(pq)
    # Enriching "in place" has to survive being run twice. Drop anything this
    # script produces before regenerating it, or the road_class/subclass
    # merge below collides with the previous run's columns and the reindex at
    # the end raises KeyError.
    df = df.drop(columns=[c for c in DERIVED_COLS if c in df.columns])
    base_cols = list(df.columns)
    print(f"points: {len(df):,}   spacing: {args.spacing:g} m")

    mos = build_vrt(files, os.path.join(demdir, VRT_NAME),
                    resolution=args.vrt_resolution, resampling=args.vrt_resampling)
    vrt = mos.path
    kind = "warped VRT (reprojected)" if mos.warped else "VRT"
    print(f"mosaic: {len(mos.sources)} tiles -> {mos.width:,} x {mos.height:,} px "
          f"{kind} at {mos.res:g} units/px ({mos.res_m:.4f} m)")
    if mos.mixed_crs:
        crss = sorted({(s.crs.to_epsg() or s.crs.to_wkt()[:40]) for s in mos.sources},
                      key=str)
        print(f"  MIXED PROJECTIONS: {len(crss)} of them -> reprojected onto "
              f"{mos.crs.to_epsg() or 'the finest source CRS'}.")
    if mos.mixed_res:
        native = sorted({s.res_m for s in mos.sources})
        print(f"  MIXED RESOLUTION: sources at "
              f"{', '.join(f'{r:.4f} m' for r in native)}, resampled to "
              f"{mos.res_m:.4f} m with {args.vrt_resampling}.")
        print( "  Finest source wins any overlap. dem_res records each point's "
               "NATIVE resolution in")
        print( "  metres -- above mosaic_res means that sample sits on upsampled "
               "data. Collects also")
        print( "  differ in date and vertical reference, so a seam between them can "
               "look like terrain;")
        print( "  check dem_tile/dem_res before trusting a sag near a boundary.")
    if mos.misalign > 0.01:
        print(f"  note: worst source offset from the target grid is "
              f"{mos.misalign:.3f} px")

    # The project frame, unchanged: everything downstream reads these.
    tr = Transformer.from_crs("EPSG:4326", PROJ_CRS, always_xy=True)
    e, n = tr.transform(df.lon.values, df.lat.values)
    df["easting"] = np.round(e, 3)
    df["northing"] = np.round(n, 3)

    # The DEM frame, for sampling and for the cell ids.
    trd = Transformer.from_crs("EPSG:4326", DEM_CRS, always_xy=True)
    dx, dy = trd.transform(df.lon.values, df.lat.values)
    df["dem_x"] = np.round(dx, 3)
    df["dem_y"] = np.round(dy, 3)

    # Cell edges fall on whole feet in the DEM's CRS, so floor() is the cell id.
    df["cell_e"] = np.floor(dx).astype(np.int32)
    df["cell_n"] = np.floor(dy).astype(np.int32)
    df["cell_center_dist_m"] = np.round(
        np.hypot(dx - (df.cell_e + 0.5), dy - (df.cell_n + 0.5)) * FT_TO_M, 4)

    oid = df.OBJECTID.values
    same = np.r_[False, (df.cell_e.values[1:] == df.cell_e.values[:-1])
                 & (df.cell_n.values[1:] == df.cell_n.values[:-1])
                 & (oid[1:] == oid[:-1])]
    df["same_cell_as_prev"] = same

    df["bearing_deg"] = np.round(local_bearing(oid, e, n), 2).astype("float32")

    print("sampling DEM:")
    elev_ft, cell_ft, pix_c, pix_r, inside = sample_mosaic(vrt, dx, dy)

    # ftUS -> metres, here and only here: every column below is metres, so
    # nothing downstream has to know the DEM changed units.
    elev = elev_ft * FT_TO_M
    cellz = cell_ft * FT_TO_M
    df["elev_m"] = np.round(elev, 3).astype("float32")
    df["elev_cell_m"] = np.round(cellz, 3).astype("float32")
    df["elev_disc_cm"] = np.round(np.abs(elev - cellz) * 100, 2).astype("float32")

    tile_names, tile_res = locate_sources(mos, dx, dy, inside)
    df["dem_tile"] = tile_names
    df["dem_res"] = np.round(tile_res, 6).astype("float32")

    missing = int(np.isnan(elev).sum())
    print(f"  sampled {len(df) - missing:,} / {len(df):,} points"
          + (f"   ({missing:,} outside the DEM)" if missing else ""))

    attrs = pd.read_csv(os.path.join(here, endpoints_path(args.city)),
                        usecols=["OBJECTID", "road_class", "subclass",
                                 "road_flags"])
    df = df.merge(attrs, on="OBJECTID", how="left")

    df = df[base_cols + DERIVED_COLS]

    df.to_parquet(pq, index=False, compression="zstd")
    print(f"\nwrote {pq}  ({os.path.getsize(pq)/1e6:.1f} MB)")
    if not args.no_csv:
        csv = os.path.join(here, points_path(args.spacing, "csv", args.city))
        df.to_csv(csv, index=False)
        print(f"wrote {csv}  ({os.path.getsize(csv)/1e6:.1f} MB)")

    meta = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": int(len(df)),
        "point_spacing_m": float(args.spacing),
        "horizontal_crs": {"points_source": "EPSG:4326 (WGS84)",
                           "easting_northing": f"{PROJ_CRS} (NAD83 / UTM 10N), metres",
                           "dem_x_dem_y": f"{DEM_CRS} (NAD83(2011) / CA zone 3), US survey feet"},
        "vertical_datum": VDATUM,
        "elevation_units": "metres (converted from the DEM's US survey feet)",
        "dem": {
            "product": "USGS 3DEP OPR (Original Product Resolution) DEM",
            "project": "CA_AlamedaCounty_2021_B21",
            "grid": "1 US survey foot (0.3048006 m), AREA_OR_POINT=Point",
            "crs": f"{DEM_CRS} + NAVD88 height (ftUS), Geoid18",
            "tiles": len(files),
            "directory": args.dem_dir,
            "mosaic": f"{VRT_NAME} ({mos.width} x {mos.height} px), built per run",
            "mosaic_reprojected": bool(mos.warped),
            "mosaic_res": mos.res,
            "mosaic_res_m": mos.res_m,
            "mixed_resolution": bool(mos.mixed_res),
            "mixed_projections": bool(mos.mixed_crs),
            "native_resolutions_m": sorted({s.res_m for s in mos.sources}),
            "overlap_rule": "finest-resolution source wins",
            "nodata": -999999.0,
            "note": ("tiles do not overlap; sampled through the VRT so points at "
                     "a tile edge still get all four bilinear neighbours"),
        },
        "accuracy_notes": {
            "absolute_vertical": "USGS 3DEP QL1/QL2 spec RMSEz <= 10 cm (not independently verified)",
            "cross_check_vs_1m_product": ("1.05 cm RMS, +0.19 cm mean, over 59,472 points "
                                          "against the 1 m product from the same collect"),
            "relative_vertical_measured": "~1.2-2 cm RMS locally, measured on straight paved segments (1 m product)",
            "horizontal_registration_measured": "centerline vs road crown: mean offset -0.06 m, std 1.13 m",
            "gradient_guidance": ("compute grades over >=25 m baselines; adjacent samples are "
                                  "noise-dominated, and at 0.15 m spacing on a 0.3 m grid "
                                  "neighbouring samples are correlated as well"),
        },
        "columns": {
            "easting/northing": f"{PROJ_CRS}, metres -- the frame the plots use",
            "dem_x/dem_y": f"{DEM_CRS}, US survey feet -- the frame the DEM uses",
            "cell_e/cell_n": "SW corner of the containing 1 ftUS cell, DEM CRS",
            "elev_m": "bilinear interpolation of the 4 surrounding cell centers, metres",
            "elev_cell_m": "raw value of the containing cell, metres",
            "elev_disc_cm": ("|bilinear - cell|; >20 suggests a curb/wall/bridge edge. "
                             "On this 1 ft grid the bulk is much tighter than on the "
                             "1 m product (median 0.24 vs 1.5 cm), but above ~8 cm the "
                             "two agree closely -- that tail is real features, not grid "
                             "noise, so the threshold is unchanged."),
            "dem_res": ("native resolution of the source tile, in METRES -- "
                        "comparable across projections, unlike the raw CRS number "
                        "(1 ftUS = 0.3048 m is finer than 0.5 m). Equal to "
                        "mosaic_res_m unless collects of different resolutions were "
                        "mixed, in which case a larger value means that sample sits "
                        "on upsampled data."),
            "same_cell_as_prev": "previous point on the segment shares this cell",
            "bearing_deg": "local heading, degrees from north",
        },
    }
    mp = os.path.join(here, points_path(args.spacing, "meta.json", args.city))
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    print(f"wrote {mp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
