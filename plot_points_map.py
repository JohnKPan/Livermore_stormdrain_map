"""Plot the resampled centerline points on an interactive Plotly map.

Reads derived/segments_points_1m.csv (or any --points file), joins
FunctionalClass from derived/segments_endpoints.csv on OBJECTID, and writes a
self-contained HTML map coloured by road class.

Usage:
    python plot_points_map.py                     # all 667k points
    python plot_points_map.py --every 10          # every 10th point (~67k)
    python plot_points_map.py --hover             # per-point street-name hover
    python plot_points_map.py --offline           # inline plotly.js, no CDN
"""

import argparse
import math
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go

POINTS = "derived/segments_points_1m.csv"
ENDPOINTS = "derived/segments_endpoints.csv"

# Ordered so the busy classes draw on top of the Local mesh.
CLASS_COLORS = {
    "Local": "#7f8c9b",
    "Minor Collector": "#4c9f70",
    "Major Collector": "#2e86c1",
    "Minor Arterial": "#e08e45",
    "Principal Arterial": "#e4572e",
    "Other Freeway or Expressway": "#a24bcf",
    "Interstate": "#111827",
    "Ramp": "#c9a227",
    "(unclassified)": "#b9c2cc",
}
DRAW_ORDER = list(CLASS_COLORS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--points", default=POINTS)
    ap.add_argument("--every", type=int, default=1,
                    help="plot every Nth point (1 = all)")
    ap.add_argument("--size", type=float, default=2.6, help="marker size in px")
    ap.add_argument("--style", default="carto-positron",
                    help="basemap: carto-positron, open-street-map, carto-darkmatter, "
                         "satellite, white-bg (all token-free)")
    ap.add_argument("--zoom", type=float, default=None,
                    help="override the auto-fit zoom")
    ap.add_argument("--hover", action="store_true",
                    help="attach street name to each point (large file)")
    ap.add_argument("--offline", action="store_true",
                    help="inline plotly.js instead of loading from CDN")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    pts_path = os.path.join(here, args.points)

    df = pd.read_csv(pts_path, usecols=["OBJECTID", "FullStreetName", "lat", "lon"])
    if args.every > 1:
        df = df.iloc[::args.every].copy()

    cls = pd.read_csv(os.path.join(here, ENDPOINTS), usecols=["OBJECTID", "FunctionalClass"])
    df = df.merge(cls, on="OBJECTID", how="left")
    df["FunctionalClass"] = df["FunctionalClass"].fillna("(unclassified)")

    lat0, lat1 = df.lat.min(), df.lat.max()
    lon0, lon1 = df.lon.min(), df.lon.max()

    # Web-mercator fit: 360 deg spans 256*2^z px. Take the tighter of the two
    # axes, with the lat span stretched by 1/cos(lat) for mercator, then pad.
    view_w, view_h = 1400.0, 800.0
    lat_mid = (lat0 + lat1) / 2
    span_x = max(lon1 - lon0, 1e-9)
    span_y = max(lat1 - lat0, 1e-9) / math.cos(math.radians(lat_mid))
    zoom = args.zoom if args.zoom is not None else min(
        math.log2(view_w * 360 / (256 * span_x)),
        math.log2(view_h * 360 / (256 * span_y)),
    ) - 0.6  # padding so the extent isn't flush against the frame

    fig = go.Figure()
    for name in DRAW_ORDER:
        sub = df[df.FunctionalClass == name]
        if sub.empty:
            continue
        kw = {}
        if args.hover:
            kw["customdata"] = sub.FullStreetName.to_numpy()
            kw["hovertemplate"] = "%{customdata}<br>%{lat:.6f}, %{lon:.6f}<extra></extra>"
        else:
            kw["hovertemplate"] = "%{lat:.6f}, %{lon:.6f}<extra>" + name + "</extra>"
        fig.add_trace(go.Scattermap(
            # float64 numpy -> plotly 6 serialises as base64 typed arrays, far
            # smaller than JSON text and lossless at 1 m precision.
            lat=sub.lat.to_numpy(dtype=np.float64),
            lon=sub.lon.to_numpy(dtype=np.float64),
            mode="markers",
            marker=dict(size=args.size, color=CLASS_COLORS[name]),
            name=f"{name} ({len(sub):,})",
            **kw,
        ))

    fig.update_layout(
        map=dict(
            style=args.style,
            center=dict(lat=(lat0 + lat1) / 2, lon=(lon0 + lon1) / 2),
            zoom=zoom,
        ),
        margin=dict(l=0, r=0, t=44, b=0),
        title=(f"Livermore street centerline — {len(df):,} points "
               f"({'every ' + str(args.every) + 'th of ' if args.every > 1 else ''}1 m spacing)"),
        legend=dict(itemsizing="constant", bgcolor="rgba(255,255,255,0.85)",
                    bordercolor="#ccc", borderwidth=1, x=0.01, y=0.99),
        height=860,
    )

    suffix = "" if args.every == 1 else f"_every{args.every}"
    out = args.out or os.path.join(here, "derived", f"map_points_1m{suffix}.html")
    fig.write_html(out, include_plotlyjs=True if args.offline else "cdn",
                   full_html=True)
    print(f"{len(df):,} points -> {out}  ({os.path.getsize(out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
