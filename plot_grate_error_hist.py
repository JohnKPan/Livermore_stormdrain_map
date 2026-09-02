"""Histogram of published grate elevation minus the lidar DEM, one panel per city.

Answers one question: how far is each city's surveyed TopOfGrate from the ground
the lidar says is there, once both are on NAVD88? A grate sits in the road
surface, so a correct reading lands on zero. Anything else is a defect -- in the
survey, in the geometry, or in the attribute join.

The error plotted is

    err_ft = (TopOfGrate + DATUM_SHIFT_M/FT_TO_M) - dem_ft

with TopOfGrate in the city's published feet on NGVD29 and dem_ft sampled from
the 1 ftUS 3DEP tiles in dem_<city>/, which are NAVD88. DATUM_SHIFT_M puts the
two on one datum -- see plot_street_drains.py for where that constant comes from.
Zeros are excluded: several publishers zero-fill a column they do not populate,
and 0 ft is an absence rather than a reading.

The dashed lines mark GRATE_TOL_M, the tolerance the plotters gate on. A value
outside them is dropped by gate_to_dem() and the inlet plots hollow on the DEM
profile instead, its position known and its elevation not.

SHOWING EVERY POINT
-------------------
Errors run from -1,642 to +440,608 ft while 97% of them sit inside +/-3 ft, so
the full range defeats a plain linear axis -- every real value lands in one bar.
Both axes are therefore transformed by default, and nothing is clipped or
dropped:

  x: signed log, t = sign(e) * log10(1 + |e|). Symmetric about zero, finite at
     zero (unlike log), and it keeps the sign, which matters because the tails
     are two different defects. Ticks are relabelled back to real feet, so the
     axis reads in feet even though the spacing is logarithmic.

  y: log counts. A bin holding 1 inlet out of 27,284 is invisible on a linear
     count axis -- which is exactly the bin you want to see when hunting a
     sentinel. Empty bins are not drawn, since log has no zero.

--scale linear and --xlim restore the older clipped view when you want to study
the core; --xlim then labels what fell off each end rather than letting clipped
values read as a spike at the edge.

DEM sampling goes through add_elevation.build_vrt/sample_mosaic -- the same code
the pipeline uses -- so these errors are the ones the plotters actually see, not
a reimplementation that might drift from it. That pass is slow (tens of minutes
across all five cities), so the sampled elevations are cached to CACHE and reused
until --refresh. --refresh honours --city, so one city can be redone without
paying for San Jose's 3,755 tiles again.

RENDERER: bokeh, not matplotlib, unlike the other plotters here. matplotlib's
savefig currently dies with a native fault (0xc06d007f) in this environment for
every output format. The computation below is renderer agnostic, so swapping
back is a change to panel()/render() alone.

Usage:
    python plot_grate_error_hist.py                      # full range, every point
    python plot_grate_error_hist.py --scale linear --xlim 50
    python plot_grate_error_hist.py --city san_jose
    python plot_grate_error_hist.py --refresh --city livermore
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd
from bokeh.layouts import column
from bokeh.models import (ColumnDataSource, FixedTicker, HoverTool, Label,
                          Span)
from bokeh.plotting import figure, output_file, save
from pyproj import Transformer

from add_elevation import VRT_NAME, build_vrt, sample_mosaic
from plot_street_drains import (DATUM_SHIFT_M, FT_TO_M, GRATE_TOL_M, INLETS,
                                resolve_inlets)

CACHE = "derived/grate_dem_error.csv"
OUTHTML = "derived/grate_error_hist.html"
# Registry order is arbitrary; this is biggest-first so the eye meets the panel
# carrying most of the corpus before the ones carrying little.
CITIES = ["san_jose", "fremont", "pleasanton", "livermore", "hayward"]
SHIFT_FT = DATUM_SHIFT_M / FT_TO_M
BAR, RULE, WARN = "#4a7fb5", "#333333", "#c0392b"
# Where to put labelled ticks on the signed-log axis, in real feet. Only those
# inside a panel's range are drawn.
TICKS_FT = [0, 1, 3, 10, 30, 100, 300, 1_000, 10_000, 100_000, 1_000_000]


def slog(e):
    """Signed log: sign(e) * log10(1 + |e|). Zero maps to zero, sign kept."""
    e = np.asarray(e, dtype=float)
    return np.sign(e) * np.log10(1.0 + np.abs(e))


def sample_city(city, lon, lat):
    """DEM elevation in ftUS under each point, via the pipeline's own mosaic."""
    demdir = f"dem_{city}"
    files = sorted(glob.glob(os.path.join(demdir, "*.tif")))
    if not files:
        print(f"  {city}: no tiles in {demdir}/ -- skipped")
        return np.full(len(lon), np.nan)
    mos = build_vrt(files, os.path.join(demdir, VRT_NAME))
    tr = Transformer.from_crs("EPSG:4326", mos.crs, always_xy=True)
    x, y = tr.transform(lon, lat)
    elev, _cell, _c, _r, _inside = sample_mosaic(mos.path, x, y)
    return elev


def build_cache(path, inlets_path, cities=None, existing=None):
    """Sample the DEM under every inlet once, and keep it.

    `cities` limits the sampling; rows for the others are carried over from
    `existing` when it is given, so a partial refresh does not lose them.
    """
    a = pd.read_csv(inlets_path, low_memory=False)
    want = cities or CITIES
    out = []
    if existing is not None:
        keep = existing[~existing.source.isin(want)]
        if not keep.empty:
            print(f"  keeping {len(keep):,} cached rows for "
                  f"{sorted(set(keep.source))}")
            out.append(keep)
    for city in want:
        d = a[a.source == city]
        if d.empty:
            continue
        ok = (d.lon.notna() & d.lat.notna()).to_numpy()
        print(f"  {city}: {len(d):,} inlets")
        dem = np.full(len(d), np.nan)
        if ok.any():
            dem[ok] = sample_city(city, d.lon.to_numpy()[ok], d.lat.to_numpy()[ok])
        out.append(pd.DataFrame({
            "source": city, "AssetID": d.AssetID.to_numpy(),
            "lon": d.lon.to_numpy(), "lat": d.lat.to_numpy(),
            "TopOfGrate": pd.to_numeric(d.TopOfGrate, errors="coerce").to_numpy(),
            "dem_ft": dem}))
    df = pd.concat(out, ignore_index=True)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(path, index=False)
    print(f"  wrote {path} ({len(df):,} rows)")
    return df


def errors(df):
    """Signed error in feet, zeros and unsampled points dropped."""
    v = pd.to_numeric(df.TopOfGrate, errors="coerce")
    keep = v.notna() & (v != 0) & df.dem_ft.notna()
    return (v[keep] + SHIFT_FT) - df.dem_ft[keep]


def axis_ticks(lo_ft, hi_ft, symlog):
    """Fixed ticks and their real-feet labels for the range actually shown."""
    if not symlog:
        return None, None
    vals = sorted({s * t for t in TICKS_FT for s in (-1, 1)})
    vals = [v for v in vals if lo_ft <= v <= hi_ft]
    pos = [float(slog(v)) for v in vals]
    lab = {p: (f"{v:+,.0f}" if v else "0") for p, v in zip(pos, vals)}
    return FixedTicker(ticks=pos), lab


def panel(city, e, args):
    """One city's histogram, or a placeholder when it publishes no elevations."""
    ylog = args.scale == "symlog" and not args.no_ylog
    p = figure(width=980, height=270,
               tools="pan,box_zoom,wheel_zoom,reset,save",
               y_axis_type="log" if ylog else "linear",
               y_axis_label="inlets (log)" if ylog else "inlets")
    if e.empty:
        # Hayward publishes no elevation column at all, so its panel would
        # otherwise be a silent empty box.
        p.title = f"{city} — no published grate elevations"
        p.add_layout(Label(x=0, y=1, text="nothing to plot",
                           text_align="center", text_color="#777"))
        return p

    symlog = args.scale == "symlog"
    if args.xlim:                              # clipped view
        lo_ft, hi_ft = -args.xlim, args.xlim
        vals = np.clip(e.to_numpy(), lo_ft, hi_ft)
        n_lo = int((e < lo_ft).sum())
        n_hi = int((e > hi_ft).sum())
    else:                                      # everything, nothing dropped
        lo_ft, hi_ft = float(e.min()), float(e.max())
        vals = e.to_numpy()
        n_lo = n_hi = 0

    t = slog(vals) if symlog else vals
    t_lo = float(slog(lo_ft)) if symlog else lo_ft
    t_hi = float(slog(hi_ft)) if symlog else hi_ft
    pad = 0.02 * (t_hi - t_lo or 1.0)
    edges = np.linspace(t_lo, t_hi, args.bins + 1)
    counts, _ = np.histogram(t, bins=edges)

    # Log y has no zero, so empty bins are simply not drawn.
    nz = counts > 0
    left_ft = np.where(np.abs(edges[:-1]) < 1e-12, 0.0,
                       np.sign(edges[:-1]) * (10.0 ** np.abs(edges[:-1]) - 1)) \
        if symlog else edges[:-1]
    right_ft = np.where(np.abs(edges[1:]) < 1e-12, 0.0,
                        np.sign(edges[1:]) * (10.0 ** np.abs(edges[1:]) - 1)) \
        if symlog else edges[1:]
    src = ColumnDataSource(dict(
        top=counts[nz], left=edges[:-1][nz], right=edges[1:][nz],
        lo_ft=left_ft[nz], hi_ft=right_ft[nz]))
    bottom = 0.5 if ylog else 0
    r = p.quad(top="top", bottom=bottom, left="left", right="right",
               source=src, fill_color=BAR, line_color=None)
    p.add_tools(HoverTool(renderers=[r], tooltips=[
        ("range", "@lo_ft{0,0.00} … @hi_ft{0,0.00} ft"),
        ("inlets", "@top{0,0}")]))

    p.x_range.start, p.x_range.end = t_lo - pad, t_hi + pad
    if ylog:
        p.y_range.start, p.y_range.end = 0.5, float(counts.max()) * 2.0
    ticker, labels = axis_ticks(lo_ft, hi_ft, symlog)
    if ticker is not None:
        p.xaxis.ticker = ticker
        p.xaxis.major_label_overrides = labels

    p.add_layout(Span(location=0, dimension="height", line_color=RULE,
                      line_width=1))
    tol = float(slog(args.tol_ft)) if symlog else args.tol_ft
    for s in (-1, 1):
        p.add_layout(Span(location=s * tol, dimension="height",
                          line_color=WARN, line_width=2, line_dash="dashed"))

    out = int((e.abs() > args.tol_ft).sum())
    p.title = (f"{city}   n={len(e):,}   median {e.median():+.2f} ft   "
               f"IQR {e.quantile(.25):+.2f}…{e.quantile(.75):+.2f}   "
               f"range {e.min():+,.1f} … {e.max():+,.1f} ft   "
               f"outside ±{args.tol_ft:g} ft: {out:,} "
               f"({100 * out / len(e):.2f}%)")
    ytxt = float(counts.max()) if not ylog else float(counts.max()) * 0.7
    if n_lo:
        p.add_layout(Label(x=t_lo, y=ytxt, text_font_size="9pt", text_color=WARN,
                           text=f"◄ {n_lo:,} below −{args.xlim:g} ft"))
    if n_hi:
        p.add_layout(Label(x=t_hi, y=ytxt, text_font_size="9pt", text_color=WARN,
                           text=f"{n_hi:,} above +{args.xlim:g} ft ►",
                           text_align="right"))
    return p


def render(df, cities, args):
    panels = [panel(c, errors(df[df.source == c]), args) for c in cities]
    panels[-1].xaxis.axis_label = (
        "published TopOfGrate − lidar DEM  (ft, NAVD88 after the "
        f"{DATUM_SHIFT_M:+.3f} m datum shift"
        + ("; signed-log spacing, labels are real feet)" if args.scale == "symlog"
           else ")"))
    output_file(args.out, title="Grate elevation error vs 3DEP lidar")
    save(column(*panels))
    print(f"wrote {args.out}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", action="append",
                    help="limit to one city; repeatable (default: all)")
    ap.add_argument("--tol-ft", type=float, default=GRATE_TOL_M / FT_TO_M,
                    help="tolerance line, feet (default the plotters' "
                         f"GRATE_TOL_M = {GRATE_TOL_M / FT_TO_M:.1f} ft)")
    ap.add_argument("--xlim", type=float, default=None,
                    help="clip to +/- this many feet. Default is no clipping: "
                         "every point is shown")
    ap.add_argument("--scale", choices=("symlog", "linear"), default="symlog",
                    help="x spacing. symlog (default) makes the full range "
                         "legible; linear is only useful with --xlim")
    ap.add_argument("--no-ylog", action="store_true",
                    help="linear count axis; single-inlet bins become invisible")
    ap.add_argument("--bins", type=int, default=160)
    ap.add_argument("--refresh", action="store_true",
                    help="re-sample the DEM even if the cache exists")
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--inlets", default=None,
                    help=f"inlet corpus (default {INLETS})")
    ap.add_argument("--out", default=OUTHTML)
    args = ap.parse_args()

    if args.scale == "linear" and not args.xlim:
        print("! --scale linear without --xlim: a handful of outliers span "
              "440,000 ft, so every real value will land in one bar")

    here = os.path.dirname(os.path.abspath(__file__))
    have = (pd.read_csv(args.cache, low_memory=False)
            if os.path.exists(args.cache) else None)
    if args.refresh or have is None:
        print("sampling the DEM under every inlet (slow; cached afterwards):")
        df = build_cache(args.cache, resolve_inlets(here, args.inlets),
                         cities=args.city, existing=have)
    else:
        df = have
        print(f"using cached {args.cache} ({len(df):,} rows); --refresh to redo")

    cities = [c for c in (args.city or CITIES) if c in set(df.source)]
    if not cities:
        raise SystemExit(f"no rows for {args.city}; cache holds "
                         f"{sorted(set(df.source))}")
    render(df, cities, args)


if __name__ == "__main__":
    main()
