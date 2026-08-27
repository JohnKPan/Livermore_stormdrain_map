"""Plot the elev_m profile of every street, one PNG per street name.

A street name usually covers several centerline segments, so the segments are
chained end-to-end into a single path before plotting. The chain starts at a
terminal of that street -- the westmost for a street running mostly E-W, the
northmost otherwise -- so repeated runs are directly comparable. Which end that
is appears on the x-axis label and in _index.csv, since it varies by street.

Individual samples are noise-dominated point-to-point (SNR ~0.23 against a
~2 cm noise floor), so each plot shows the raw profile faintly with a 25 m
rolling mean on top -- the 25 m baseline is where gradient becomes trustworthy.
--smooth changes that window; it is the one knob every profile in this project
shares, since plot_street_drains.py and plot_street_bokeh.py both build theirs
here.

The window is in METRES and is converted to samples using the point spacing
measured from the data (see sample_step). It used to be converted 1:1, which
silently assumed 1 m spacing -- at the current 0.1 m that would have made
--smooth 25 a 2.5 m window, and moved every sag count in the project with it.

Outputs:
    derived/profiles/<STREET>.png
    derived/profiles/_index.csv   one row per street, sortable by grade/length

Usage:
    python plot_street_profiles.py
    python plot_street_profiles.py --min-length 250     # only longer streets
    python plot_street_profiles.py --street "HOLMES ST" # just one
    python plot_street_profiles.py --smooth 10          # tighter rolling mean
"""

import argparse
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from extract_centerline_latlon import points_path

POINTS = points_path()
OUTDIR = "derived/profiles"
GAP_BREAK_M = 40.0        # larger jumps between segments are drawn as a break
SMOOTH_M = 25.0           # default rolling-mean window; --smooth overrides
# Where a profile or map marks a discontinuity. 20 cm on the old 1 m DEM; the
# 1 ft grid interpolates across a curb in a third of the distance, so the same
# physical step shows a smaller bilinear-vs-cell gap and 20 flagged only 140
# points corpus-wide -- none at all on AIRWAY BL, this project's worked example.
# 10 cm flags 823, still far above the 2.07 cm p99 noise floor.
DISC_CM = 10.0
NODE_TOL_M = 2.0          # segment endpoints this close share one junction node
OPPOSITE = {"W": "E", "N": "S"}


def safe_name(name, used):
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_") or "UNNAMED"
    s = s[:80]
    if s in used:
        used[s] += 1
        s = f"{s}__{used[s]}"
    else:
        used[s] = 0
    return s


def endpoint_nodes(segs):
    """Cluster segment endpoints into the junction nodes they share.

    Returns (pos, degree): node positions as an (N, 2) array, and how many
    segment ends meet at each. Degree 1 marks a terminal -- a true end of the
    street, rather than a joint with the next segment along.
    """
    eps = np.array([(s[1][i], s[2][i]) for s in segs for i in (0, -1)])
    node = np.full(len(eps), -1)
    pos, k = [], 0
    for i in range(len(eps)):
        if node[i] >= 0:
            continue
        same = (np.hypot(eps[:, 0] - eps[i, 0], eps[:, 1] - eps[i, 1]) < NODE_TOL_M)
        same &= node < 0
        node[same] = k
        pos.append(eps[same].mean(axis=0))
        k += 1
    return np.array(pos), np.bincount(node, minlength=k)


def path_origin(segs):
    """Where a street's path should start, and the compass label for that end.

    Candidates are the terminals of the segment graph, so the start always lies
    on the road. The corner of the bounding box does not: for a street that
    bends, its westmost easting and northmost northing belong to different
    places, and the nearest endpoint to that phantom corner can be a junction
    mid-street. Patterson Pass Rd started 540 m in and then jumped 3.5 km back.

    Which terminal wins follows the street's longer axis -- the westmost for one
    running mostly E-W, the northmost for the rest. A single rule like NW -> SE
    cannot work, because a street whose west end is also its south end has no
    NW end to start from.
    """
    all_e = np.concatenate([s[1] for s in segs])
    all_n = np.concatenate([s[2] for s in segs])
    pos, deg = endpoint_nodes(segs)
    cand = pos[deg == 1]
    if not len(cand):
        cand = pos                       # a closed loop has no terminal at all
    if np.ptp(all_e) >= np.ptp(all_n):
        return cand[cand[:, 0].argmin()], "W"
    return cand[cand[:, 1].argmax()], "N"


def chain_segments(segs):
    """Greedily order segments into one path, starting at one end of the street.

    segs: list of (oid, e, n, z, disc) arrays, each already ordered along its
    segment. Returns the concatenated arrays, the gap preceding each, and the
    compass label of the end the path starts from -- see path_origin.
    """
    start, origin = path_origin(segs)

    remaining = list(range(len(segs)))
    # Seed with whichever endpoint of any segment is closest to that end.
    best, best_d, best_flip = None, np.inf, False
    for i in remaining:
        _, e, n, _, _ = segs[i]
        for flip, (px, py) in ((False, (e[0], n[0])), (True, (e[-1], n[-1]))):
            d = np.hypot(px - start[0], py - start[1])
            if d < best_d:
                best, best_d, best_flip = i, d, flip

    order, flips, gaps = [best], [best_flip], [0.0]
    remaining.remove(best)
    _, e, n, _, _ = segs[best]
    cur = (e[0], n[0]) if best_flip else (e[-1], n[-1])

    while remaining:
        pick, pick_d, pick_flip = None, np.inf, False
        for i in remaining:
            _, e, n, _, _ = segs[i]
            d0 = np.hypot(e[0] - cur[0], n[0] - cur[1])
            d1 = np.hypot(e[-1] - cur[0], n[-1] - cur[1])
            if d0 < pick_d:
                pick, pick_d, pick_flip = i, d0, False
            if d1 < pick_d:
                pick, pick_d, pick_flip = i, d1, True
        order.append(pick)
        flips.append(pick_flip)
        gaps.append(pick_d)
        remaining.remove(pick)
        _, e, n, _, _ = segs[pick]
        cur = (e[0], n[0]) if pick_flip else (e[-1], n[-1])

    E, N, Z, D, RUN = [], [], [], [], []
    run = 0
    for k, (i, flip, gap) in enumerate(zip(order, flips, gaps)):
        _, e, n, z, disc = segs[i]
        if flip:
            e, n, z, disc = e[::-1], n[::-1], z[::-1], disc[::-1]
        if k and gap > GAP_BREAK_M:
            run += 1
        E.append(e)
        N.append(n)
        Z.append(z)
        D.append(disc)
        RUN.append(np.full(len(e), run))
    return (np.concatenate(E), np.concatenate(N), np.concatenate(Z),
            np.concatenate(D), np.concatenate(RUN), gaps, origin)


def sample_step(d, run):
    """Median along-path distance between consecutive samples, in metres.

    Taken from the data rather than from a constant, so the corpus spacing and
    the smoothing window can never drift apart. Steps that cross a run boundary
    are excluded -- those are the gaps between chained segments, not samples.
    """
    step = np.diff(d)
    same = np.asarray(run)[1:] == np.asarray(run)[:-1]
    step = step[same & (step > 0)]
    return float(np.median(step)) if step.size else 1.0


def build_profile(e, n, z, run, smooth_m=SMOOTH_M):
    """Cumulative along-path distance, with distance still accruing over gaps.

    smooth_m is the rolling-mean window in METRES, converted to samples by the
    spacing measured from the data. The mean is taken per run, so it never
    averages across a gap between segments.
    """
    d = np.r_[0.0, np.cumsum(np.hypot(np.diff(e), np.diff(n)))]
    s = pd.Series(z)
    w = max(3, int(round(smooth_m / sample_step(d, run))))
    smooth = (s.groupby(run)
               .transform(lambda g: g.rolling(w, center=True,
                                              min_periods=max(3, w // 3)).mean())
               .to_numpy())
    return d, smooth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-length", type=float, default=0.0,
                    help="skip streets shorter than this (m)")
    ap.add_argument("--street", default=None, help="render a single street name")
    ap.add_argument("--outdir", default=OUTDIR)
    ap.add_argument("--dpi", type=int, default=110)
    ap.add_argument("--smooth", type=float, default=SMOOTH_M,
                    help="rolling-mean window in metres, converted to samples "
                         f"using the corpus spacing (default {SMOOTH_M:g})")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    df = pd.read_parquet(os.path.join(here, POINTS),
                         columns=["OBJECTID", "FullStreetName", "dist_along_m",
                                  "easting", "northing", "elev_m", "elev_disc_cm"])
    df = df.dropna(subset=["elev_m"])
    if args.street:
        df = df[df.FullStreetName == args.street]
        if df.empty:
            print(f"No points for street {args.street!r}")
            return 1

    outdir = os.path.join(here, args.outdir)
    os.makedirs(outdir, exist_ok=True)

    fig, ax = plt.subplots(figsize=(11, 4.6), dpi=args.dpi)
    used, rows, skipped = {}, [], 0

    for name, grp in df.groupby("FullStreetName", sort=True):
        segs = []
        for oid, sg in grp.groupby("OBJECTID", sort=True):
            sg = sg.sort_values("dist_along_m")
            segs.append((oid, sg.easting.to_numpy(), sg.northing.to_numpy(),
                         sg.elev_m.to_numpy(), sg.elev_disc_cm.to_numpy()))
        e, n, z, disc, run, gaps, origin = chain_segments(segs)
        far = OPPOSITE[origin]
        d, smooth = build_profile(e, n, z, run, args.smooth)
        step = sample_step(d, run)

        if d[-1] < args.min_length:
            skipped += 1
            continue

        fname = safe_name(name, used) + ".png"
        # signed end-minus-start: positive means the street climbs going origin -> far
        change = z[-1] - z[0]
        grade = change / d[-1] * 100 if d[-1] > 0 else 0.0

        ax.clear()
        for r in np.unique(run):
            m = run == r
            ax.plot(d[m], z[m], color="#9fb3c8", lw=0.7,
                    label=f"raw {step:.3g} m" if r == 0 else None)
            ax.plot(d[m], smooth[m], color="#2e86c1", lw=1.9,
                    label=f"{args.smooth:g} m rolling mean" if r == 0 else None)
        ax.plot(d[0], z[0], "o", color="#1a9850", ms=7, zorder=5,
                label=f"{origin} start")
        ax.plot(d[-1], z[-1], "s", color="#d73027", ms=6, zorder=5,
                label=f"{far} end")

        bad = disc > DISC_CM
        nbad = int(bad.sum())
        if nbad:
            ax.plot(d[bad], z[bad], "x", color="#d73027", ms=5, mew=1.2,
                    ls="none", zorder=6,
                    label=f"discontinuity ({nbad})")

        ax.set_xlabel(f"distance along street from {origin} end (m)")
        ax.set_ylabel("elevation (m, NAVD88)")
        nseg = len(segs)
        nbreak = int(run.max())
        bits = [f"{d[-1]:,.0f} m", f"{nseg} segment{'s' if nseg != 1 else ''}"]
        if nbreak:
            bits.append(f"{nbreak} gap{'s' if nbreak != 1 else ''}")
        if nbad:
            bits.append(f"{nbad} point{'s' if nbad != 1 else ''} flagged")
        ax.set_title(f"{name}   —   " + " · ".join(bits) + "\n"
                     f"{origin} end {z[0]:.1f} m → {far} end {z[-1]:.1f} m   "
                     f"(net {change:+.2f} m, {grade:+.2f}% mean grade)",
                     fontsize=10)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="best")
        fig.tight_layout()
        fig.savefig(os.path.join(outdir, fname))

        rows.append({"FullStreetName": name, "file": fname, "n_segments": nseg,
                     "length_m": round(float(d[-1]), 1), "n_gaps": nbreak,
                     "origin": origin,
                     "elev_start_m": round(float(z[0]), 3),
                     "elev_end_m": round(float(z[-1]), 3),
                     "elev_min_m": round(float(np.nanmin(z)), 3),
                     "elev_max_m": round(float(np.nanmax(z)), 3),
                     "net_change_m": round(float(change), 3),
                     "mean_grade_pct": round(float(grade), 3),
                     "pts_flagged_disc": nbad})

    plt.close(fig)
    idx = pd.DataFrame(rows).sort_values("length_m", ascending=False)
    ipath = os.path.join(outdir, "_index.csv")
    idx.to_csv(ipath, index=False)
    print(f"wrote {len(rows):,} profiles to {outdir}/")
    if skipped:
        print(f"skipped {skipped:,} streets under {args.min_length:g} m")
    print(f"index: {ipath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
