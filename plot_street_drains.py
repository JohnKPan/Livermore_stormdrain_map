"""Street elevation profile with storm drain inlets, plus a plan-view map.

Top panel : DEM elevation along the street (W -> E, or N -> S for a street that
            runs mostly north-south), with every storm inlet
            within --max-offset of the centerline placed at its chainage.
            Inlets with a surveyed TopOfGrate are drawn at that elevation, with
            a stem down to InvertElevation1 showing pipe depth. Inlets without
            survey elevations are drawn hollow on the DEM profile -- position
            known, elevation unknown.
Bottom    : plan view in lat/lon (GPS), centerline points and inlets by type.

DATUM: the inlet table stores elevations in FEET on what is almost certainly
NGVD29, while the DEM is metres NAVD88. Comparing 4,469 clean inlets against
the DEM gives a median offset of -0.794 m (std 0.400), matching the ~+0.8 m
NGVD29->NAVD88 shift for this area. That offset is applied by default so the
two sources are on one datum; use --no-datum-shift to see the raw values.

Usage:
    python plot_street_drains.py --street "A ST"
    python plot_street_drains.py --street "AIRWAY BL" --max-offset 25
    python plot_street_drains.py --all --min-drains 5
    python plot_street_drains.py --street "A ST" --smooth 10
"""

import argparse
import json
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd
from pyproj import Transformer

from extract_centerline_latlon import DEFAULT_CITY, SPACING, points_path
from plot_street_profiles import (SMOOTH_M, chain_segments, build_profile,
                                  merge_components, safe_name, sample_step,
                                  segs_of, split_components)

# resolved per --city in main(); this is only the default shown in --help
POINTS = points_path()
# One file for every city fetch_inlets.py knows, filtered to the city's AOI at
# load time. The pre-merge per-city file is still read if the merged one has not
# been built, so an old checkout keeps working.
INLETS = "derived/storm_inlets_all.csv"
INLETS_LEGACY = "derived/storm_inlets.csv"
# fetch_city_boundaries.py writes one polygon per city under this name. A
# buffered AOI from make_aoi.py can be passed with --aoi instead, which is the
# point of buffering: it catches inlets that drain INTO the city from outside
# its limits, and those are exactly the ones a source filter would miss.
AOI_DIR = "city_geojson"
OUTDIR = "derived/drains"
FT_TO_M = 0.3048
DATUM_SHIFT_M = 0.794          # measured NGVD29 -> NAVD88 offset, see module docstring
# OSM's tile usage policy requires a User-Agent identifying the application.
# contextily's default is "contextily-<random hex>", which is blocked outright.
TILE_UA = ("PengWeather-Stormdrain-Study/1.0 "
           "(Livermore CA storm drain research; contextily)")
GRATE_MIN_FT, GRATE_MAX_FT = 300.0, 900.0   # Livermore spans roughly 390-790 ft

# Standardised chart scales. Vertical is fixed so 0.20 m (the sag threshold)
# always renders ~10 px while the 2 cm DEM noise stays sub-pixel. Horizontal is
# quantised to a 1-2-5 ladder so charts fall into a few comparable classes
# instead of 1,728 unique scales.
V_MPP_DEFAULT = 0.02
H_LADDER = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0)
PROF_W_MIN_PX, PROF_W_MAX_PX = 420.0, 1400.0
PROF_H_MIN_PX, PROF_H_MAX_PX = 200.0, 900.0
MAP_LADDER = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0)
MAP_W_MIN_PX, MAP_W_MAX_PX = 420.0, 1200.0
MAP_H_MIN_PX, MAP_H_MAX_PX = 320.0, 1000.0

STYLE = {
    "Curb Inlet":    dict(marker="o", color="#2e86c1"),
    "Grated Inlet":  dict(marker="s", color="#e08e45"),
    "Slotted Drain": dict(marker="D", color="#7b3294"),
    "Trench Drain":  dict(marker="P", color="#1a9850"),
    "Standard":      dict(marker="^", color="#c9a227"),
    "Unknown":       dict(marker="X", color="#888888"),
}
DEFAULT_STYLE = dict(marker="v", color="#555555")


def load_aoi(path):
    """The AOI polygon as one shapely geometry, prepared for repeated hits."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    with open(path, encoding="utf-8") as f:
        gj = json.load(f)
    geoms = ([shape(ft["geometry"]) for ft in gj["features"]]
             if gj.get("type") == "FeatureCollection"
             else [shape(gj.get("geometry", gj))])
    geoms = [g for g in geoms if not g.is_empty]
    if not geoms:
        raise SystemExit(f"no usable geometry in {path}")
    return unary_union(geoms)


def clip_to_aoi(d, aoi):
    """Keep the inlets inside the AOI.

    A bbox pre-filter first: the polygon test is per-point and the corpus is
    every city at once, so rejecting the ~90% that are not even in the bounding
    box before touching shapely is most of the runtime.
    """
    from shapely import points as _points, contains as _contains

    x0, y0, x1, y1 = aoi.bounds
    near = (d.lon.between(x0, x1) & d.lat.between(y0, y1)).to_numpy()
    keep = np.zeros(len(d), dtype=bool)
    if near.any():
        sub = d.loc[near, ["lon", "lat"]].to_numpy()
        keep[near] = _contains(aoi, _points(sub))
    return d.loc[keep].reset_index(drop=True)


def load_inlets(path, shift, aoi=None):
    # low_memory=False: the merged corpus has columns a city fills and its
    # neighbours leave blank, so chunked type inference would give one column
    # different dtypes depending on where the chunk boundary landed.
    d = pd.read_csv(path, low_memory=False)
    before, srcs = len(d), None
    if aoi is not None:
        srcs = d.source.value_counts().to_dict() if "source" in d.columns else None
        d = clip_to_aoi(d, aoi)
        kept = d.source.value_counts().to_dict() if "source" in d.columns else None
        print(f"  inlets: {before:,} -> {len(d):,} within the AOI"
              + (f"  {kept}" if kept else ""))
        if srcs and not kept:
            raise SystemExit(
                f"no inlets fall inside the AOI. The corpus covers "
                f"{', '.join(srcs)} -- is this the right city?")
    g = d.TopOfGrate.where((d.TopOfGrate >= GRATE_MIN_FT) & (d.TopOfGrate <= GRATE_MAX_FT))
    iv = d.InvertElevation1.where((d.InvertElevation1 >= GRATE_MIN_FT - 50)
                                  & (d.InvertElevation1 <= GRATE_MAX_FT))
    d["grate_m"] = g * FT_TO_M + shift
    d["invert_m"] = iv * FT_TO_M + shift
    d["type"] = d.TypeDescription.fillna("Unknown")
    return d


def resolve_inlets(here, explicit=None):
    """The merged corpus if it has been built, else the pre-merge Livermore file."""
    if explicit:
        return os.path.join(here, explicit)
    merged = os.path.join(here, INLETS)
    if os.path.exists(merged):
        return merged
    legacy = os.path.join(here, INLETS_LEGACY)
    if os.path.exists(legacy):
        print(f"  ! {INLETS} not built, falling back to {INLETS_LEGACY}"
              f" -- run: python fetch_inlets.py --all")
        return legacy
    raise SystemExit(f"no inlet corpus: run `python fetch_inlets.py --all` "
                     f"to write {INLETS}")


def resolve_aoi(here, city, explicit=None, disabled=False):
    """The AOI polygon path for a city, or None when filtering is off."""
    if disabled:
        return None
    path = os.path.join(here, explicit) if explicit else \
        os.path.join(here, AOI_DIR, f"{city}.geojson")
    if not os.path.exists(path):
        raise SystemExit(
            f"AOI not found: {path}\n"
            f"Run `python fetch_city_boundaries.py` to write {AOI_DIR}/, pass "
            f"--aoi with your own polygon, or --no-aoi to skip the filter.")
    return path


def snap(inlets, e, n, dist, max_offset):
    """Attach each nearby inlet to its closest point on the chained path."""
    keep = ((inlets.x > e.min()-max_offset) & (inlets.x < e.max()+max_offset)
            & (inlets.y > n.min()-max_offset) & (inlets.y < n.max()+max_offset))
    sub = inlets[keep].copy()
    if sub.empty:
        return sub.assign(chainage=[], offset_m=[], dem_m=[])
    # Running argmin over chunks of the path. The whole inlet x point matrix
    # would be 9.3 GB for UNNAMED (119 scattered segments, so the bbox filter
    # above keeps 97% of the city's inlets); this caps it at inlets x CHUNK.
    sx, sy = sub.x.to_numpy(), sub.y.to_numpy()
    best = np.full(len(sub), np.inf)
    idx = np.zeros(len(sub), dtype=np.int64)
    CHUNK = 8192
    for a in range(0, len(e), CHUNK):
        ee, nn = e[a:a + CHUNK], n[a:a + CHUNK]
        d2 = (sx[:, None] - ee[None, :])**2 + (sy[:, None] - nn[None, :])**2
        j = d2.argmin(axis=1)
        dmin = d2[np.arange(len(sub)), j]
        hit = dmin < best
        best[hit] = dmin[hit]
        idx[hit] = a + j[hit]

    sub["offset_m"] = np.sqrt(best)
    sub["chainage"] = dist[idx]
    sub["path_idx"] = idx
    return sub[sub.offset_m <= max_offset].sort_values("chainage")


def find_sags(d, z, run, min_prom, min_sep, edge_m=10.0):
    """Interior local minima of the profile -- the sags where water collects.

    Prominence is measured as the smaller of the two climbs needed to escape the
    dip, which is what separates a real ponding point from noise on a slope.
    Endpoints are excluded: a street that simply runs downhill to its end drains
    onto the next street, it does not pond.
    """
    out = []
    for r in np.unique(run):
        m = np.flatnonzero(run == r)
        if m.size < 3:
            continue
        dd, zz = d[m], z[m]
        ok = np.isfinite(zz)
        if ok.sum() < 3:
            continue
        dd, zz, idx = dd[ok], zz[ok], m[ok]
        for i in range(1, len(zz)-1):
            if not (zz[i] <= zz[i-1] and zz[i] < zz[i+1]):
                continue
            if dd[i]-dd[0] < edge_m or dd[-1]-dd[i] < edge_m:
                continue          # too close to an end to be a true sag
            # climb required to escape left and right before finding lower ground
            left = zz[:i]
            right = zz[i+1:]
            lo_l = np.flatnonzero(left < zz[i])
            lo_r = np.flatnonzero(right < zz[i])
            l_seg = left[lo_l[-1]+1:] if lo_l.size else left
            r_seg = right[:lo_r[0]] if lo_r.size else right
            if l_seg.size == 0 or r_seg.size == 0:
                continue
            prom = min(l_seg.max(), r_seg.max()) - zz[i]
            if prom >= min_prom:
                out.append((dd[i], zz[i], prom, idx[i]))
    out.sort(key=lambda t: -t[2])
    kept = []
    for cand in out:                       # greedy, keep the most prominent
        if all(abs(cand[0]-k[0]) >= min_sep for k in kept):
            kept.append(cand)
    return sorted(kept, key=lambda t: t[0])


def street_parts(st):
    """A street's physically connected runs, ready for prepare().

    One entry for an ordinary street; more where a divided road gives a
    carriageway each way, or where the name covers roads in different parts of
    town. Chaining across those produces a profile that doubles back or counts
    kilometres of open country as chainage -- see split_components().
    """
    return merge_components(split_components(segs_of(st)))


def prepare(st, inlets, args, segs=None):
    """Chain the street and attach inlets -- cheap, so a batch can filter on the
    inlet count before paying for a figure and its map tiles.

    `segs` is one run from street_parts(); without it the whole street is
    chained as a single path, which is only right when it has one run.
    """
    if segs is None:
        segs = segs_of(st)
    e, n, z, disc, run, _, origin = chain_segments(segs)
    d, smooth = build_profile(e, n, z, run, args.smooth)
    near = snap(inlets, e, n, d, args.max_offset)
    if not near.empty:
        near = near.assign(dem_m=z[near.path_idx.to_numpy()])
        # sorted by chainage in snap(), so numbers run 1..N from the start end
        # and read left-to-right on the profile as well as along the map
        near = near.assign(num=np.arange(1, len(near) + 1))

    # Sags come from the smoothed profile: at 1 m the elevation noise (~2 cm)
    # would manufacture minima everywhere. --smooth therefore moves the sag
    # count as well as the drawn line -- a shorter window keeps shallower dips.
    sags = find_sags(d, smooth, run, args.sag_prom, args.sag_sep, args.sag_edge)
    marked, unserved = [], []
    to4326 = Transformer.from_crs("EPSG:26910", "EPSG:4326", always_xy=True)
    # find_sags returns chainage order, so this numbers the surviving sags
    # 1..N from the start end -- independent of how many inlets exist
    for k, (chain, elev, prom, pidx) in enumerate(sags, 1):
        row = None
        if not near.empty:
            cand = near[(near.chainage - chain).abs() <= args.sag_window]
            if not cand.empty:
                # Proximity along the street is not enough: an inlet 30 m away
                # can sit well above the low point and would never receive its
                # ponding. Require it within roughly a curb height of the sag,
                # smoothed vs smoothed so 2 cm DEM noise does not decide it.
                lift = np.abs(smooth[cand.path_idx.to_numpy()] - elev)
                cand = cand[np.isfinite(lift) & (lift <= args.sag_tol)]
                if not cand.empty:
                    pick = cand.iloc[(cand.chainage - chain).abs().argmin()]
                    if not any(pick.num == m["num"] for m in marked):
                        row = pick

        if row is not None:
            marked.append(dict(sag_num=k, num=int(row.num),
                               chainage=float(row.chainage),
                               sag_chainage=float(chain), sag_elev=float(elev),
                               prominence=float(prom), lon=float(row.lon),
                               lat=float(row.lat),
                               elev=float(row.grate_m) if pd.notna(row.grate_m)
                               else float(row.dem_m)))
            continue

        # No qualifying inlet. Record the sag anyway, with whichever inlet on
        # the street is closest along the chainage -- ignoring both the window
        # and the height tolerance, so the gap itself is visible.
        slon, slat = to4326.transform(float(e[pidx]), float(n[pidx]))
        rec = dict(sag_num=k, sag_chainage=float(chain), sag_elev=float(elev),
                   prominence=float(prom), lon=slon, lat=slat,
                   near_num=None, gap_m=None, lift_m=None,
                   near_lon=None, near_lat=None, near_asset=None)
        if not near.empty:
            nb = near.iloc[(near.chainage - chain).abs().argmin()]
            rec.update(near_num=int(nb.num),
                       gap_m=float(abs(nb.chainage - chain)),
                       lift_m=float(smooth[int(nb.path_idx)] - elev),
                       near_lon=float(nb.lon), near_lat=float(nb.lat),
                       near_asset=str(nb.AssetID))
        unserved.append(rec)

    return dict(e=e, n=n, z=z, run=run, d=d, smooth=smooth, near=near,
                sags=sags, marked=marked, unserved=unserved, origin=origin)


def render(street, st, prep, args, outdir, used):
    e, n, z, run = prep["e"], prep["n"], prep["z"], prep["run"]
    d, smooth, near = prep["d"], prep["smooth"], prep["near"]
    marked, unserved = prep["marked"], prep["unserved"]
    origin = prep["origin"]

    tr = Transformer.from_crs("EPSG:26910", "EPSG:4326", always_xy=True)
    slon, slat = tr.transform(e, n)
    to3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    mx, my = to3857.transform(slon, slat)

    # Web Mercator metres are inflated by 1/cos(lat) -- ~1.264 here -- so a
    # literal 500 would only be ~396 m on the ground.
    # Work out the map extent in GROUND metres. Web Mercator inflates by
    # 1/cos(lat) (~1.264 here), so everything user-facing is converted back.
    cosl = np.cos(np.radians(float(np.mean(slat))))
    gx = max((mx.max()-mx.min()) * cosl, 1.0)
    gy = max((my.max()-my.min()) * cosl, 1.0)
    # Padding proportional to the street: a fixed 500 m swamps a 60 m cul-de-sac,
    # leaving it 1% of the frame, while being too tight on a 5 km arterial.
    pad_g = float(np.clip(args.pad_frac * max(gx, gy), args.pad_min_m, args.pad_m))
    ext_x, ext_y = gx + 2*pad_g, gy + 2*pad_g
    pad = pad_g / cosl                       # back to Mercator for the limits
    span_x, span_y = ext_x, ext_y

    # ---- standardised scales -------------------------------------------------
    # The panel is sized FROM the scale rather than the scale falling out of a
    # fixed panel, so every chart reads at the same metres-per-pixel and two
    # streets are directly comparable.
    ylo, yhi = float(np.nanmin(z)), float(np.nanmax(z))
    if not near.empty:                       # grate/invert markers extend the y range
        for col in ("grate_m", "invert_m"):
            v = near[col].to_numpy(dtype=float)
            v = v[np.isfinite(v)]
            if v.size:
                ylo, yhi = min(ylo, v.min()), max(yhi, v.max())
    dat_y = max(yhi - ylo, 0.05)
    length = float(d[-1])

    h_mpp = next((t for t in H_LADDER if length/t <= PROF_W_MAX_PX), H_LADDER[-1])
    prof_w = float(np.clip(length/h_mpp, PROF_W_MIN_PX, PROF_W_MAX_PX))
    want_h = dat_y*1.06/args.v_mpp
    prof_h = float(np.clip(want_h, PROF_H_MIN_PX, PROF_H_MAX_PX))
    # only a street taller than PROF_H_MAX_PX*v_mpp gets compressed
    v_mpp = args.v_mpp if want_h <= PROF_H_MAX_PX else dat_y*1.06/prof_h
    y_span = prof_h * v_mpp
    y_mid = (ylo + yhi)/2.0
    ylim = (y_mid - y_span/2.0, y_mid + y_span/2.0)
    xlim = (-0.02*length, 1.02*length)

    # Map gets its own quantised ground-metres-per-pixel, so maps are comparable
    # to each other the same way the profiles are. Equal aspect falls out for
    # free because both axes use the one scale.
    m_mpp = next((t for t in MAP_LADDER
                  if ext_x/t <= MAP_W_MAX_PX and ext_y/t <= MAP_H_MAX_PX),
                 MAP_LADDER[-1])
    map_w = float(np.clip(ext_x/m_mpp, MAP_W_MIN_PX, MAP_W_MAX_PX))
    map_h = float(np.clip(ext_y/m_mpp, MAP_H_MIN_PX, MAP_H_MAX_PX))

    DPI = 115.0
    L, R, T, B, GAP = 0.95, 2.85, 0.95, 0.75, 1.15      # inches
    body_w = max(prof_w, map_w)/DPI
    fig_w = L + body_w + R
    fig_h = T + prof_h/DPI + GAP + map_h/DPI + B
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=DPI)
    ax = fig.add_axes([L/fig_w,
                       (B + map_h/DPI + GAP)/fig_h,
                       (prof_w/DPI)/fig_w, (prof_h/DPI)/fig_h])
    axm = fig.add_axes([L/fig_w, B/fig_h,
                        (map_w/DPI)/fig_w, (map_h/DPI)/fig_h])
    tall = False

    # ---------------- profile ----------------
    # The corpus spacing is measured, not assumed -- it moved from 1 m to 0.1 m
    # when the DEM went to 1 ft, and a hardcoded label went stale silently.
    step = sample_step(d, run)
    for r in np.unique(run):
        m = run == r
        ax.plot(d[m], z[m], color="#b6c4d2", lw=0.7,
                label=f"DEM, raw {step:.3g} m" if r == 0 else None)
        ax.plot(d[m], smooth[m], color="#33475b", lw=1.8,
                label=f"DEM, {args.smooth:g} m mean" if r == 0 else None)

    for t, g in near.groupby("type"):
        sty = STYLE.get(t, DEFAULT_STYLE)
        surveyed = g.dropna(subset=["grate_m"])
        unsurveyed = g[g.grate_m.isna()]
        if not surveyed.empty:
            # stem from grate down to pipe invert
            for _, row in surveyed.dropna(subset=["invert_m"]).iterrows():
                ax.plot([row.chainage, row.chainage], [row.grate_m, row.invert_m],
                        color=sty["color"], lw=0.9, alpha=0.55, zorder=3)
            ax.plot(surveyed.chainage, surveyed.grate_m, sty["marker"],
                    color=sty["color"], ms=7, mec="white", mew=0.8, ls="none",
                    zorder=5, label=f"{t} — grate ({len(surveyed)})")
            iv = surveyed.dropna(subset=["invert_m"])
            if not iv.empty:
                ax.plot(iv.chainage, iv.invert_m, "_", color=sty["color"],
                        ms=9, mew=1.6, ls="none", zorder=5,
                        label=f"{t} — invert ({len(iv)})")
        if not unsurveyed.empty:
            ax.plot(unsurveyed.chainage, unsurveyed.dem_m, sty["marker"],
                    mfc="none", mec=sty["color"], mew=1.3, ms=7, ls="none",
                    zorder=4, label=f"{t} — no survey elev ({len(unsurveyed)})")

    if not near.empty and len(near) <= args.max_labels:
        for _, row in near.iterrows():
            yv = row.grate_m if pd.notna(row.grate_m) else row.dem_m
            ax.annotate(str(int(row.num)), (row.chainage, yv),
                        xytext=(0, 9), textcoords="offset points",
                        ha="center", fontsize=6.5, color="#111111", zorder=7,
                        path_effects=[pe.withStroke(linewidth=2.0,
                                                    foreground="white")])

    for k, us in enumerate(unserved):
        lab = f"sag {us['sag_num']}: NO inlet"
        if us["near_num"] is not None:
            lab = (f"sag {us['sag_num']}: nearest #{us['near_num']}\n"
                   f"{us['gap_m']:.0f} m away, {us['lift_m']:+.2f} m")
        ax.annotate(lab, xy=(us["sag_chainage"], us["sag_elev"]),
                    xytext=(0, -52 - 30*(k % 2)), textcoords="offset points",
                    ha="center", va="top", fontsize=7.5, fontweight="bold",
                    color="#c76a00", zorder=9,
                    arrowprops=dict(arrowstyle="-|>", lw=1.8, color="#c76a00",
                                    shrinkA=1, shrinkB=4),
                    path_effects=[pe.withStroke(linewidth=2.5,
                                                foreground="white")])
        # dashed link to that nearest inlet so the gap reads at a glance
        if us["near_num"] is not None:
            nb = near[near.num == us["near_num"]].iloc[0]
            ny = nb.grate_m if pd.notna(nb.grate_m) else nb.dem_m
            ax.plot([us["sag_chainage"], nb.chainage], [us["sag_elev"], ny],
                    ls=":", lw=1.2, color="#c76a00", alpha=0.85, zorder=4)

    for k, mk in enumerate(marked):
        ax.annotate(f"sag {mk['sag_num']} → #{mk['num']}",
                    xy=(mk["chainage"], mk["elev"]),
                    xytext=(0, -46 - 28*(k % 3)), textcoords="offset points",
                    ha="center", va="top", fontsize=8, fontweight="bold",
                    color="#b8002e", zorder=9,
                    arrowprops=dict(arrowstyle="-|>", lw=1.8, color="#b8002e",
                                    shrinkA=1, shrinkB=6),
                    path_effects=[pe.withStroke(linewidth=2.5,
                                                foreground="white")])

    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_xlabel(f"distance along street from {origin} end (m)")
    ax.set_ylabel("elevation (m, NAVD88)")
    shift_note = (f"grate/invert shifted {DATUM_SHIFT_M:+.3f} m to NAVD88"
                  if args.datum_shift else "raw inlet elevations, NO datum shift")
    scale_note = (f"{h_mpp:g} m/px H  |  {v_mpp:.3f} m/px V  |  "
                  f"{h_mpp/v_mpp:.0f}x vertical exaggeration"
                  + ("   [V COMPRESSED]" if v_mpp > args.v_mpp*1.001 else ""))
    ax.set_title(f"{street} — DEM profile with storm drain inlets   "
                 f"({len(near)} inlets within {args.max_offset:g} m)\n"
                 f"{shift_note}\n{scale_note}",
                 fontsize=10.5)
    ax.grid(alpha=0.3)
    # Legends go outside the axes; savefig(bbox_inches="tight") grows the canvas
    # to fit them, so nothing is clipped and nothing covers the data.
    h, l = ax.get_legend_handles_labels()
    # Annotations cannot act as legend handles, so the sag arrows need proxies.
    for col, style, txt, cnt in (("#b8002e", "-", "sag served by inlet", len(marked)),
                                 ("#c76a00", ":", "sag, no qualifying inlet", len(unserved))):
        if cnt:
            h.append(Line2D([], [], color=col, lw=1.8, ls=style, marker=">",
                            markersize=6))
            l.append(f"{txt} ({cnt})")
    if h:
        if tall:
            # profile occupies the left column; nothing sits beneath it
            ax.legend(h, l, fontsize=7.5, ncol=min(4, len(h)), loc="upper center",
                      bbox_to_anchor=(0.5, -0.09), frameon=True, framealpha=0.95)
        else:
            # stacked: below the profile would land on the map's title, so send
            # it right alongside the map legend
            ax.legend(h, l, fontsize=7.5, loc="upper left",
                      bbox_to_anchor=(1.02, 1.0), frameon=True, framealpha=0.95,
                      borderaxespad=0.0)

    # ---------------- plan map over real tiles ----------------
    # Drawn in Web Mercator so tiles land unwarped and sharp; the ticks are then
    # relabelled back to lat/lon so the axes still read as GPS coordinates.
    axm.plot(mx, my, ".", color="#12263a", ms=1.8, zorder=4,
             label=f"{step:.3g} m centerline points ({len(st):,})")
    for t, g in near.groupby("type"):
        sty = STYLE.get(t, DEFAULT_STYLE)
        gx, gy = to3857.transform(g.lon.to_numpy(), g.lat.to_numpy())
        axm.plot(gx, gy, sty["marker"], color=sty["color"], ms=9,
                 mec="white", mew=1.0, ls="none", zorder=5,
                 label=f"{t} ({len(g)})")
        if len(near) <= args.max_labels:
            for (px, py, num) in zip(gx, gy, g.num.to_numpy()):
                axm.annotate(str(int(num)), (px, py), xytext=(7, 5),
                             textcoords="offset points", fontsize=6.5,
                             color="#111111", zorder=6,
                             path_effects=[pe.withStroke(linewidth=2.0,
                                                         foreground="white")])

    # Labels past this point are drawn leftward so they cannot run under the
    # right-hand legend. set_xlim has not been applied yet, so use the data.
    mid_x = mx.min() + 0.6*(mx.max() - mx.min())
    for us in unserved:
        sx_, sy_ = to3857.transform(us["lon"], us["lat"])
        lab = (f"sag {us['sag_num']}: NO inlet" if us["near_num"] is None
               else f"sag {us['sag_num']}: nearest #{us['near_num']} "
                    f"({us['gap_m']:.0f} m)")
        right = sx_ > mid_x
        axm.annotate(lab, xy=(sx_, sy_),
                     xytext=(-34 if right else 34, -34),
                     ha="right" if right else "left",
                     textcoords="offset points", fontsize=7.5,
                     fontweight="bold", color="#c76a00", zorder=9,
                     arrowprops=dict(arrowstyle="-|>", lw=1.8, color="#c76a00",
                                     shrinkA=1, shrinkB=5),
                     path_effects=[pe.withStroke(linewidth=2.5,
                                                 foreground="white")])
        if us["near_num"] is not None:
            nx_, ny_ = to3857.transform(us["near_lon"], us["near_lat"])
            axm.plot([sx_, nx_], [sy_, ny_], ls=":", lw=1.3, color="#c76a00",
                     alpha=0.9, zorder=4)

    for mk in marked:
        ax_, ay_ = to3857.transform(mk["lon"], mk["lat"])
        right = ax_ > mid_x
        axm.annotate(f"sag {mk['sag_num']} → #{mk['num']}", xy=(ax_, ay_),
                     xytext=(-34 if right else 34, 30),
                     ha="right" if right else "left",
                     textcoords="offset points",
                     fontsize=8, fontweight="bold", color="#b8002e", zorder=9,
                     arrowprops=dict(arrowstyle="-|>", lw=1.8, color="#b8002e",
                                     shrinkA=1, shrinkB=7),
                     path_effects=[pe.withStroke(linewidth=2.5,
                                                 foreground="white")])

    axm.set_xlim(mx.min()-pad, mx.max()+pad)
    axm.set_ylim(my.min()-pad, my.max()+pad)
    axm.set_aspect("equal")

    basemap = "none"
    try:
        import contextily as cx
        provider = (getattr(cx.providers.CartoDB, args.basemap)
                    if hasattr(cx.providers.CartoDB, args.basemap)
                    else cx.providers.OpenStreetMap.Mapnik)
        if args.zoom > 0:
            zoom = args.zoom
        else:
            # Pick the zoom whose tiles span the extent about 3 across, then
            # clamp: too low is blurry on short streets, too high multiplies
            # tile fetches across a 1,700-street batch for no visible gain.
            span = max(axm.get_xlim()[1]-axm.get_xlim()[0],
                       axm.get_ylim()[1]-axm.get_ylim()[0])
            zoom = int(np.clip(round(np.log2(3*40075016.686/max(span, 1.0))),
                               12, 18))
        cx.add_basemap(axm, source=provider, crs="EPSG:3857",
                       attribution_size=6, zoom=zoom,
                       headers={"User-Agent": TILE_UA})
        basemap = f"{args.basemap} z{zoom}"
    except Exception as exc:                                   # noqa: BLE001
        print(f"  basemap unavailable ({type(exc).__name__}: {exc}); "
              f"drawing without tiles")

    # relabel Web Mercator ticks as lat/lon
    back = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    xt = np.linspace(*axm.get_xlim(), 4 if tall else 6)
    yt = np.linspace(*axm.get_ylim(), 8 if tall else 6)
    axm.set_xticks(xt)
    axm.set_yticks(yt)
    axm.set_xticklabels([f"{back.transform(v, my.mean())[0]:.4f}" for v in xt],
                        fontsize=7.5, rotation=45, ha="right")
    axm.set_yticklabels([f"{back.transform(mx.mean(), v)[1]:.4f}" for v in yt],
                        fontsize=8)
    axm.set_xlabel("longitude (WGS84)")
    axm.set_ylabel("latitude (WGS84)")
    axm.set_title(f"{street} — {m_mpp:g} m/px  |  {pad_g:.0f} m context  |  "
                  f"{basemap} basemap, "
                  f"storm drain inlets by type", fontsize=11)
    hm, lm = axm.get_legend_handles_labels()
    if hm:
        axm.legend(hm, lm, fontsize=8, loc="upper left",
                   bbox_to_anchor=(1.02, 1.0), frameon=True, framealpha=0.95,
                   borderaxespad=0.0)

    out = os.path.join(outdir, safe_name(street, used) + ".png")
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out, near


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--street", default="A ST")
    ap.add_argument("--all", action="store_true", help="every street with drains")
    ap.add_argument("--min-drains", type=int, default=1)
    ap.add_argument("--max-offset", type=float, default=30.0,
                    help="max centerline distance for an inlet to belong (m)")
    ap.add_argument("--no-datum-shift", dest="datum_shift", action="store_false")
    ap.add_argument("--basemap", default="osm",
                    help="tile source. Default 'osm' (OpenStreetMap). The "
                         "CartoDB styles -- Voyager, Positron, DarkMatter -- "
                         "now watermark every tile 'API KEY REQUIRED' unless "
                         "you have a key configured, and they return HTTP 200 "
                         "while doing it, so the failure is silent.")
    ap.add_argument("--outdir", default=OUTDIR)
    ap.add_argument("--city", default=DEFAULT_CITY,
                    help="city slug; reads derived/<city>/ and city_geojson/<city>.geojson")
    ap.add_argument("--inlets", default=None,
                    help=f"inlet CSV (default {INLETS}, or {INLETS_LEGACY})")
    ap.add_argument("--aoi", default=None,
                    help=f"AOI polygon to clip inlets to "
                         f"(default {AOI_DIR}/<city>.geojson)")
    ap.add_argument("--no-aoi", dest="aoi_filter", action="store_false",
                    help="skip the AOI clip and use every inlet in the file")
    ap.add_argument("--smooth", type=float, default=SMOOTH_M,
                    help="rolling-mean window (m) behind both the drawn "
                         f"profile and sag detection (default {SMOOTH_M:g})")
    ap.add_argument("--sag-prom", type=float, default=0.20,
                    help="min prominence (m) for a profile dip to count as a sag")
    ap.add_argument("--sag-sep", type=float, default=10.0,
                    help="min separation (m) between reported sags")
    ap.add_argument("--sag-window", type=float, default=60.0,
                    help="max chainage distance from a sag to its inlet")
    ap.add_argument("--sag-edge", type=float, default=10.0,
                    help="a minimum must be this far (m) from either end of a "
                         "run to count as a sag rather than a drain-off point")
    ap.add_argument("--sag-tol", type=float, default=0.20,
                    help="max height (m) an inlet may sit above the sag and "
                         "still be treated as serving it (~one curb height)")
    ap.add_argument("--unserved-only", action="store_true",
                    help="render only streets holding a sag with no qualifying inlet")
    ap.add_argument("--random", type=int, default=0,
                    help="render N randomly chosen streets that have inlets")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--v-mpp", type=float, default=V_MPP_DEFAULT,
                    help="vertical scale in metres of elevation per pixel "
                         "(default 0.02 = 50 px per metre)")
    ap.add_argument("--pad-m", type=float, default=250.0,
                    help="MAXIMUM ground metres of map context around the street")
    ap.add_argument("--pad-min-m", type=float, default=60.0,
                    help="minimum ground metres of map context")
    ap.add_argument("--pad-frac", type=float, default=0.6,
                    help="context as a fraction of the street's own extent")
    ap.add_argument("--max-labels", type=int, default=400,
                    help="skip inlet numbering above this count")
    ap.add_argument("--zoom", type=int, default=0,
                    help="fixed tile zoom; 0 = derive from extent, clamped 12-18")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(here, args.outdir)
    os.makedirs(outdir, exist_ok=True)

    # Neighbouring streets share tiles, so a cache turns a full-city batch from
    # tens of thousands of requests into a few thousand.
    try:
        import contextily as cx
        cache = os.path.join(here, ".tilecache")
        os.makedirs(cache, exist_ok=True)
        cx.set_cache_dir(cache)
    except Exception:                                          # noqa: BLE001
        pass

    shift = DATUM_SHIFT_M if args.datum_shift else 0.0
    aoi_path = resolve_aoi(here, args.city, args.aoi, not args.aoi_filter)
    inlets = load_inlets(resolve_inlets(here, args.inlets), shift,
                         load_aoi(aoi_path) if aoi_path else None)
    tr = Transformer.from_crs("EPSG:4326", "EPSG:26910", always_xy=True)
    inlets["x"], inlets["y"] = tr.transform(inlets.lon.values, inlets.lat.values)

    df = pd.read_parquet(os.path.join(here, points_path(SPACING, "parquet", args.city)))
# Segments with no display_name get no page: a page is identified by its
# street, and Overture leaves 8% of the kept network nameless -- unnamed stubs,
# alleys and junction connectors that no amount of fetching will name. They stay
# in the point corpus, where they still drain, and the overview draws them; they
# just have nothing to be a page of. The portal never raised this because it
# labels its nameless roads with the literal string UNNAMED.
    if args.unserved_only:
        # prepare() is cheap; scan every street and keep those holding a sag
        # with no qualifying inlet
        names = []
        for nm in sorted(df.display_name.dropna().unique()):
            sub = df[df.display_name == nm]
            if any(prepare(sub, inlets, args, segs=sg)["unserved"]
                   for sg in street_parts(sub)):
                names.append(nm)
        print(f"{len(names)} streets hold at least one unserved sag")
    elif args.all:
        names = sorted(df.display_name.dropna().unique())
    elif args.random:
        # sample only from streets that actually carry inlets, otherwise most
        # draws land on the 648 streets with none and show no arrows at all
        with_inlets = set(
            df.loc[df.display_name.notna(), "display_name"].unique())
        cand = []
        for nm in sorted(with_inlets):
            sub = df[df.display_name == nm]
            bx = (inlets.x.between(sub.easting.min()-args.max_offset,
                                   sub.easting.max()+args.max_offset)
                  & inlets.y.between(sub.northing.min()-args.max_offset,
                                     sub.northing.max()+args.max_offset))
            if bx.sum() >= 3:
                cand.append(nm)
        rng = np.random.default_rng(args.seed)
        names = list(rng.choice(cand, size=min(args.random, len(cand)),
                                replace=False))
        print(f"random sample of {len(names)} from {len(cand)} streets "
              f"with inlets (seed {args.seed}):")
        for nm in names:
            print(f"  {nm}")
    else:
        names = [args.street]

    made, skipped, failed = 0, 0, 0
    used = {}
    rows = []
    for i, name in enumerate(names, 1):
        st = df[df.display_name == name]
        if st.empty:
            print(f"No points for {name!r}")
            continue
        parts = street_parts(st)
        for pi, segs in enumerate(parts, 1):
            label = name if len(parts) == 1 else (
                "%s %d of %d" % (name, pi, len(parts)))
            try:
                prep = prepare(st, inlets, args, segs=segs)
                near = prep["near"]
                if args.all and len(near) < args.min_drains:
                    skipped += 1
                    continue
                out, near = render(label, st, prep, args, outdir, used)
            except Exception as exc:                           # noqa: BLE001
                failed += 1
                print(f"  FAILED {label!r}: {type(exc).__name__}: {exc}")
                continue
            made += 1
            rows.append({
                "display_name": name, "part": pi, "n_parts": len(parts),
                "page": label,
                "segments": " ".join(str(x[0]) for x in segs),
                "file": os.path.basename(out),
                "n_points": sum(len(x[1]) for x in segs),
                "n_inlets": len(near),
                "n_grate_elev": int(near.grate_m.notna().sum()) if len(near) else 0,
                "n_invert_elev": int(near.invert_m.notna().sum()) if len(near) else 0,
                "median_offset_m": round(float(near.offset_m.median()), 1) if len(near) else None})
        if args.all and i % 100 == 0:
            print(f"  {i}/{len(names)} processed, {made} written", flush=True)
        if not args.all:
            print(f"wrote {out}")
            mk = prep["marked"]
            print(f"  sags detected: {len(prep['sags'])}, "
                  f"{len(mk)} with an inlet within {args.sag_window:g} m")
            for m in mk:
                print(f"    sag at {m['sag_chainage']:.0f} m "
                      f"(prominence {m['prominence']:.2f} m) -> inlet #{m['num']}")
            print(f"  {len(st):,} centerline points, {len(near)} inlets within "
                  f"{args.max_offset:g} m")
            if not near.empty:
                for t, g in near.groupby("type"):
                    print(f"    {t:<14} {len(g):3d}  "
                          f"({g.grate_m.notna().sum()} with grate elev, "
                          f"{g.invert_m.notna().sum()} with invert)")
                print(f"  offset from centerline: median {near.offset_m.median():.1f} m, "
                      f"max {near.offset_m.max():.1f} m")
    if args.all:
        idx = pd.DataFrame(rows).sort_values("n_inlets", ascending=False)
        ipath = os.path.join(outdir, "_index.csv")
        idx.to_csv(ipath, index=False)
        print(f"wrote {made} street figures to {outdir}/")
        print(f"  {int((idx.n_inlets > 0).sum())} streets with inlets, "
              f"{int((idx.n_inlets == 0).sum())} without")
        print(f"  {int(idx.n_inlets.sum())} inlets attributed")
        if skipped:
            print(f"  skipped {skipped} below --min-drains {args.min_drains}")
        if failed:
            print(f"  FAILED {failed}")
        print(f"index: {ipath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
