"""Plot the elev_m profile of every street, one PNG per street name.

A street name usually covers several centerline segments, so the segments are
chained end-to-end into a single path before plotting. The chain always starts
at whichever terminal endpoint lies closest to the northwest corner of that
street's bounding box, so every profile reads NW -> SE and repeated runs are
directly comparable.

Because 1 m samples are noise-dominated point-to-point (SNR ~0.23 against a
~2 cm noise floor), each plot shows the raw profile faintly with a 25 m rolling
mean on top -- the 25 m baseline is where gradient becomes trustworthy.

Outputs:
    derived/profiles/<STREET>.png
    derived/profiles/_index.csv   one row per street, sortable by grade/length

Usage:
    python plot_street_profiles.py
    python plot_street_profiles.py --min-length 250     # only longer streets
    python plot_street_profiles.py --street "HOLMES ST" # just one
"""

import argparse
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

POINTS = "derived/segments_points_1m.parquet"
OUTDIR = "derived/profiles"
GAP_BREAK_M = 40.0        # larger jumps between segments are drawn as a break
SMOOTH_M = 25.0


def safe_name(name, used):
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_") or "UNNAMED"
    s = s[:80]
    if s in used:
        used[s] += 1
        s = f"{s}__{used[s]}"
    else:
        used[s] = 0
    return s


def chain_segments(segs):
    """Greedily order segments into one path starting nearest the NW corner.

    segs: list of (oid, e, n, z, disc) arrays, each already ordered along its
    segment. Returns the concatenated arrays plus the gap preceding each.
    """
    all_e = np.concatenate([s[1] for s in segs])
    all_n = np.concatenate([s[2] for s in segs])
    nw = np.array([all_e.min(), all_n.max()])          # NW corner of the bbox

    remaining = list(range(len(segs)))
    # Seed with whichever endpoint of any segment is closest to the NW corner.
    best, best_d, best_flip = None, np.inf, False
    for i in remaining:
        _, e, n, _, _ = segs[i]
        for flip, (px, py) in ((False, (e[0], n[0])), (True, (e[-1], n[-1]))):
            d = np.hypot(px - nw[0], py - nw[1])
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
            np.concatenate(D), np.concatenate(RUN), gaps)


def build_profile(e, n, z, run):
    """Cumulative along-path distance, with distance still accruing over gaps."""
    d = np.r_[0.0, np.cumsum(np.hypot(np.diff(e), np.diff(n)))]
    s = pd.Series(z)
    w = max(3, int(round(SMOOTH_M)))
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
        e, n, z, disc, run, gaps = chain_segments(segs)
        d, smooth = build_profile(e, n, z, run)

        if d[-1] < args.min_length:
            skipped += 1
            continue

        fname = safe_name(name, used) + ".png"
        # signed end-minus-start: positive means the street climbs going NW -> SE
        change = z[-1] - z[0]
        grade = change / d[-1] * 100 if d[-1] > 0 else 0.0

        ax.clear()
        for r in np.unique(run):
            m = run == r
            ax.plot(d[m], z[m], color="#9fb3c8", lw=0.7,
                    label="raw 1 m" if r == 0 else None)
            ax.plot(d[m], smooth[m], color="#2e86c1", lw=1.9,
                    label=f"{SMOOTH_M:g} m rolling mean" if r == 0 else None)
        ax.plot(d[0], z[0], "o", color="#1a9850", ms=7, zorder=5, label="NW start")
        ax.plot(d[-1], z[-1], "s", color="#d73027", ms=6, zorder=5, label="end")

        bad = disc > 20
        nbad = int(bad.sum())
        if nbad:
            ax.plot(d[bad], z[bad], "x", color="#d73027", ms=5, mew=1.2,
                    ls="none", zorder=6,
                    label=f"discontinuity ({nbad})")

        ax.set_xlabel("distance along street from NW end (m)")
        ax.set_ylabel("elevation (m, NAVD88)")
        nseg = len(segs)
        nbreak = int(run.max())
        bits = [f"{d[-1]:,.0f} m", f"{nseg} segment{'s' if nseg != 1 else ''}"]
        if nbreak:
            bits.append(f"{nbreak} gap{'s' if nbreak != 1 else ''}")
        if nbad:
            bits.append(f"{nbad} point{'s' if nbad != 1 else ''} flagged")
        ax.set_title(f"{name}   —   " + " · ".join(bits) + "\n"
                     f"NW end {z[0]:.1f} m → SE end {z[-1]:.1f} m   "
                     f"(net {change:+.2f} m, {grade:+.2f}% mean grade)",
                     fontsize=10)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="best")
        fig.tight_layout()
        fig.savefig(os.path.join(outdir, fname))

        rows.append({"FullStreetName": name, "file": fname, "n_segments": nseg,
                     "length_m": round(float(d[-1]), 1), "n_gaps": nbreak,
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
