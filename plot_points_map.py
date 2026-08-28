"""Plot the resampled centerline points on an interactive Plotly map.

Reads the resampled point corpus (parquet by default, or any --points file --
.csv is read as CSV), joins road_class from derived/segments_endpoints.csv
on OBJECTID, and writes a self-contained HTML map coloured by road class.

At the 0.1 m default that corpus is ~6.7 M points, which is far more than a
browser will draw comfortably: use --every to thin it.

Usage:
    python plot_points_map.py --every 100         # ~67k points, a sane default map
    python plot_points_map.py                     # every point (slow, huge HTML)
    python plot_points_map.py --hover             # per-point street-name hover
    python plot_points_map.py --offline           # inline plotly.js, no CDN
"""

import argparse
import math
import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go

from extract_centerline_latlon import (DEFAULT_CITY, SPACING, label,
                                       endpoints_path, points_path)

POINTS = points_path()
ENDPOINTS = endpoints_path()

# Ordered so the busy classes draw on top of the Local mesh.
# Two taxonomies, because two sources. The portal centerline classifies by
# FHWA function (Local, Major Collector, ...); Overture uses the OSM road
# hierarchy (residential, tertiary, ...). They are not translatable above the
# residential/Local level -- Livermore calls 94% of Overture's `secondary` a
# Principal Arterial, which is a notch above the standard correspondence -- so
# rather than map one onto the other, both are coloured, and a corpus renders
# in whichever vocabulary it actually uses.
#
# Colours run cool-to-hot up each hierarchy, and the two hierarchies are shaded
# alike at equivalent levels so the maps read the same way side by side.
CLASS_COLORS = {
    # portal, FHWA functional class
    "Local": "#7f8c9b",
    "Minor Collector": "#4c9f70",
    "Major Collector": "#2e86c1",
    "Minor Arterial": "#e08e45",
    "Principal Arterial": "#e4572e",
    "Other Freeway or Expressway": "#a24bcf",
    "Interstate": "#111827",
    "Ramp": "#c9a227",
    # Overture, OSM road hierarchy
    "service": "#a8b2bd",
    "residential": "#7f8c9b",
    "living_street": "#8fa38b",
    "unclassified": "#4c9f70",
    "tertiary": "#2e86c1",
    "secondary": "#e08e45",
    "primary": "#e4572e",
    "trunk": "#a24bcf",
    "motorway": "#111827",
    # anything unrecognised, and anything unclassified by its source. Named
    # "(unknown)" rather than "(unclassified)" so it cannot be confused with
    # Overture's `unclassified`, which is a real class and not a fallback.
    "(unknown)": "#b9c2cc",
}
DRAW_ORDER = list(CLASS_COLORS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default=DEFAULT_CITY,
                    help="city slug; sets the default --points")
    ap.add_argument("--points", default=None)
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
    pts_path = os.path.join(here, args.points
                            or points_path(SPACING, "parquet", args.city))

    cols = ["OBJECTID", "display_name", "lat", "lon"]
    if pts_path.lower().endswith(".csv"):
        df = pd.read_csv(pts_path, usecols=cols)
    else:
        df = pd.read_parquet(pts_path, columns=cols)
    if args.every > 1:
        df = df.iloc[::args.every].copy()

    cls = pd.read_csv(os.path.join(here, endpoints_path(args.city)), usecols=["OBJECTID", "road_class"])
    df = df.merge(cls, on="OBJECTID", how="left")
    df["road_class"] = df["road_class"].fillna("(unknown)")

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
        sub = df[df.road_class == name]
        if sub.empty:
            continue
        kw = {}
        if args.hover:
            kw["customdata"] = sub.display_name.to_numpy()
            kw["hovertemplate"] = "%{customdata}<br>%{lat:.6f}, %{lon:.6f}<extra></extra>"
        else:
            kw["hovertemplate"] = "%{lat:.6f}, %{lon:.6f}<extra>" + name + "</extra>"
        fig.add_trace(go.Scattermap(
            # float64 numpy -> plotly 6 serialises as base64 typed arrays, far
            # smaller than JSON text and lossless at the corpus spacing.
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
               f"({'every ' + str(args.every) + 'th of ' if args.every > 1 else ''}"
               f"{SPACING:g} m spacing)"),
        legend=dict(itemsizing="constant", bgcolor="rgba(255,255,255,0.85)",
                    bordercolor="#ccc", borderwidth=1, x=0.01, y=0.99),
        height=860,
    )

    suffix = "" if args.every == 1 else f"_every{args.every}"
    out = args.out or os.path.join(
        here, "derived", f"map_points_{label(SPACING)}{suffix}.html")
    fig.write_html(out, include_plotlyjs=True if args.offline else "cdn",
                   full_html=True)
    print(f"{len(df):,} points -> {out}  ({os.path.getsize(out) / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
