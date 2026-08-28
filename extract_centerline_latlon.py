"""Extract lat/long for Livermore street centerline segments at a chosen resolution.

Input : streets/Street_Centerline_-_Public.geojson (EPSG:4326, LineString), as
        written by fetch_livermore_street_centerlines.py. Override with --src.

Default run writes three files to derived/:
    segments_endpoints.csv    - 1 row per centerline feature
    segments_vertices.csv     - 1 row per shape vertex (native resolution)
    segments_points_0p1m.csv  - 1 row per point resampled every 0.1 m

The resample interval is in the filename, so corpora at different spacings sit
side by side rather than overwriting each other. points_path() is the one place
that name is built; every consumer imports it rather than hardcoding a path.

Spacing is 0.1 m to suit the 1 ft (0.3048 m) OPR DEM the elevations come from.
That is ~3x finer than the DEM grid, so neighbouring samples are correlated --
it oversamples deliberately, to place points precisely along the centerline
rather than to extract detail the DEM does not hold.

At 0.1 m this writes ~6.7 M points, which is a ~600 MB CSV. The pipeline runs
--slim --parquet --no-csv, which streams that CSV, converts it, and removes it.

Usage:
    python extract_centerline_latlon.py                   # endpoints + vertices + 0.1 m
    python extract_centerline_latlon.py --slim --parquet --no-csv   # the pipeline
    python extract_centerline_latlon.py --spacing 1       # coarser resample
    python extract_centerline_latlon.py --points-only

--slim --parquet is the combination add_elevation.py expects: it reads the
parquet, and joins FunctionalClass/RoadType back on OBJECTID from the endpoints
file, so carrying those columns per point here would only duplicate them.
"""

import argparse
import csv
import json
import math
import os

# One source of truth for where the centerline lands: the script that writes it.
from fetch_livermore_street_centerlines import DEFAULT_OUT as SRC

OUTDIR = "derived"
R = 6371008.8  # mean earth radius, m

# The city is the unit of work. Every derived file lands under derived/<city>/,
# so one city's run never collides with another's, and "region-wide" is a loop
# over slugs rather than a second code path. Livermore is the default because
# it is the city with a published centerline to check the rest against.
DEFAULT_CITY = "livermore"

# Default resample interval, metres. Matched to the 1 ft OPR DEM -- see the
# module docstring. Consumers import this so the whole pipeline moves together.
SPACING = 0.1

# One normalised column set, whatever the source. The portal centerline and an
# Overture city file carry the same facts under different names:
#
#     portal            Overture           here
#     OBJECTID          (none)             OBJECTID
#     FullStreetName    display_name       display_name
#     FunctionalClass   class              road_class
#     RoadType          subclass           subclass
#
# `road_class`, not `class`: `class` is a Python keyword, and every consumer
# reaches for these by attribute (df.road_class), which a keyword forbids.
#
# The portal's StreetName/StreetType/PrefixDir/SuffixDir/GlobalID are dropped.
# They were written to the endpoints and vertices files and read by nothing.
ATTRS = ["OBJECTID", "display_name", "road_class", "subclass"]
SLIM_ATTRS = ["OBJECTID", "display_name"]

PORTAL_ATTRS = {"display_name": "FullStreetName",
                "road_class": "FunctionalClass", "subclass": "RoadType"}
OVERTURE_ATTRS = {"display_name": "display_name",
                  "road_class": "class", "subclass": "subclass"}


def dist_m(a, b):
    """Equirectangular distance in metres; exact enough over a city-block span."""
    p = math.pi / 180
    x = (b[0] - a[0]) * p * math.cos((a[1] + b[1]) / 2 * p)
    y = (b[1] - a[1]) * p
    return R * math.hypot(x, y)


def cumulative(coords):
    d = [0.0]
    for i in range(1, len(coords)):
        d.append(d[-1] + dist_m(coords[i - 1], coords[i]))
    return d


def interpolate(coords, cum, target):
    """Point at `target` metres along the line."""
    if target <= 0:
        return coords[0]
    if target >= cum[-1]:
        return coords[-1]
    i = 1
    while cum[i] < target:
        i += 1
    span = cum[i] - cum[i - 1]
    t = 0.0 if span == 0 else (target - cum[i - 1]) / span
    return (coords[i - 1][0] + t * (coords[i][0] - coords[i - 1][0]),
            coords[i - 1][1] + t * (coords[i][1] - coords[i - 1][1]))


def label(spacing):
    """Filename suffix: 10m, 1m, 0p5m, 0p1m."""
    s = f"{spacing:g}".replace(".", "p")
    return f"{s}m"


def detect_schema(props):
    """Which publisher wrote this feature, from the names it uses."""
    if "FullStreetName" in props:
        return "portal"
    if "class" in props and ("display_name" in props or "name" in props):
        return "overture"
    raise SystemExit(
        "unrecognised centerline schema: expected the portal's FullStreetName "
        "or Overture's class/display_name, found " + ", ".join(sorted(props)))


def attrs_of(props, schema, seq):
    """One feature's ATTRS row, normalised.

    Overture has no stable per-segment id -- its `id` is a UUID minted fresh
    each release -- so OBJECTID is the sequence number of the feature in the
    file. That is enough: nothing keys a page off it, it only has to separate
    segments within a street so they can be chained end to end, and every
    derived file is rebuilt from a single run.
    """
    m = PORTAL_ATTRS if schema == "portal" else OVERTURE_ATTRS
    oid = props.get("OBJECTID") if schema == "portal" else seq
    return [oid] + [props.get(m[k]) for k in ATTRS[1:]]


def city_dir(city=DEFAULT_CITY):
    """Where one city's derived files live."""
    return os.path.join(OUTDIR, city)


def points_path(spacing=SPACING, ext="parquet", city=DEFAULT_CITY):
    """Where the resampled points for a given spacing live.

    One source of truth for the name, imported by add_elevation.py and every
    plotter, so changing SPACING or the city moves the whole pipeline in step.
    """
    return os.path.join(city_dir(city), f"segments_points_{label(spacing)}.{ext}")


def endpoints_path(city=DEFAULT_CITY):
    return os.path.join(city_dir(city), "segments_endpoints.csv")


def vertices_path(city=DEFAULT_CITY):
    return os.path.join(city_dir(city), "segments_vertices.csv")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=SRC,
                    help=f"input centerline GeoJSON (default {SRC})")
    ap.add_argument("--city", default=DEFAULT_CITY,
                    help=f"city slug; outputs land in derived/<city>/ "
                         f"(default {DEFAULT_CITY})")
    ap.add_argument("--spacing", type=float, default=SPACING,
                    help=f"resample interval in metres (default {SPACING:g})")
    ap.add_argument("--slim", action="store_true",
                    help="keep only OBJECTID + display_name; join the rest on OBJECTID")
    ap.add_argument("--parquet", action="store_true",
                    help="also write the resampled points as .parquet")
    ap.add_argument("--points-only", action="store_true",
                    help="skip the endpoints and vertices files")
    ap.add_argument("--no-csv", action="store_true",
                    help="delete the points CSV once --parquet has converted it; "
                         "at 0.1 m spacing that CSV is ~600 MB and nothing reads it")
    args = ap.parse_args()
    if args.no_csv and not args.parquet:
        raise SystemExit("--no-csv only makes sense with --parquet")

    here = os.path.dirname(os.path.abspath(__file__))
    src = os.path.join(here, args.src)
    if not os.path.exists(src):
        raise SystemExit(
            f"no centerline GeoJSON at {src}\n"
            "run fetch_livermore_street_centerlines.py first, or pass --src")
    with open(src, encoding="utf-8-sig") as fh:
        features = json.load(fh)["features"]

    schema = detect_schema(features[0]["properties"]) if features else "portal"
    print(f"{len(features):,} features, {schema} schema -> derived/{args.city}/")

    outdir = os.path.join(here, city_dir(args.city))
    os.makedirs(outdir, exist_ok=True)

    attr_cols = SLIM_ATTRS if args.slim else ATTRS
    pts_path = os.path.join(here, points_path(args.spacing, "csv", args.city))

    f_pts = open(pts_path, "w", newline="", encoding="utf-8")
    w_pts = csv.writer(f_pts)
    w_pts.writerow(attr_cols + ["point_index", "lat", "lon", "dist_along_m"])

    w_end = w_vtx = None
    if not args.points_only:
        f_end = open(os.path.join(here, endpoints_path(args.city)), "w",
                     newline="", encoding="utf-8")
        f_vtx = open(os.path.join(here, vertices_path(args.city)), "w",
                     newline="", encoding="utf-8")
        w_end = csv.writer(f_end)
        w_vtx = csv.writer(f_vtx)
        w_end.writerow(ATTRS + ["start_lat", "start_lon", "end_lat", "end_lon",
                                "mid_lat", "mid_lon", "length_m", "n_vertices", "bearing_deg"])
        w_vtx.writerow(ATTRS + ["vertex_index", "lat", "lon", "dist_along_m"])

    n_pts = 0
    seq = 0
    for feat in features:
        geom = feat["geometry"]
        if not geom or geom["type"] != "LineString":
            continue
        seq += 1
        coords = geom["coordinates"]
        props = feat["properties"]
        full = attrs_of(props, schema, seq)
        slim = full[:len(attr_cols)]

        cum = cumulative(coords)
        length = cum[-1]

        if w_end is not None:
            mid = interpolate(coords, cum, length / 2)
            p = math.pi / 180
            dx = (coords[-1][0] - coords[0][0]) * math.cos((coords[0][1] + coords[-1][1]) / 2 * p)
            dy = coords[-1][1] - coords[0][1]
            bearing = (math.degrees(math.atan2(dx, dy)) + 360) % 360
            w_end.writerow(full + [
                f"{coords[0][1]:.7f}", f"{coords[0][0]:.7f}",
                f"{coords[-1][1]:.7f}", f"{coords[-1][0]:.7f}",
                f"{mid[1]:.7f}", f"{mid[0]:.7f}",
                f"{length:.2f}", len(coords), f"{bearing:.1f}",
            ])
            for i, c in enumerate(coords):
                w_vtx.writerow(full + [i, f"{c[1]:.7f}", f"{c[0]:.7f}", f"{cum[i]:.2f}"])

        # resampled every `spacing` m, always including both ends
        n = max(1, int(round(length / args.spacing)))
        for i in range(n + 1):
            d = length * i / n
            pt = interpolate(coords, cum, d)
            w_pts.writerow(slim + [i, f"{pt[1]:.7f}", f"{pt[0]:.7f}", f"{d:.2f}"])
        n_pts += n + 1

    f_pts.close()
    if w_end is not None:
        f_end.close()
        f_vtx.close()

    print(f"{n_pts:,} points at {args.spacing:g} m -> {pts_path}")

    if args.parquet:
        import pandas as pd
        pq_path = pts_path[:-4] + ".parquet"
        df = pd.read_csv(pts_path)
        df.to_parquet(pq_path, index=False, compression="zstd")
        print(f"{pq_path}  ({os.path.getsize(pq_path) / 1e6:.1f} MB "
              f"vs {os.path.getsize(pts_path) / 1e6:.1f} MB csv)")
        if args.no_csv:
            os.remove(pts_path)
            print(f"removed {pts_path}")


if __name__ == "__main__":
    main()
