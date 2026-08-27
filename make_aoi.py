#!/usr/bin/env python
"""Build buffered AOI polygons from US Census incorporated-place boundaries.

Buffering is done in UTM zone 10N (EPSG:32610), not Web Mercator - a Mercator
buffer at this latitude would be ~26% too large on the ground.

Usage:
    python make_aoi.py --place Livermore --miles 5
    python make_aoi.py --place Oakland --miles 5 --clip-county 06001
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.ops import transform as shp_transform

TIGER = ("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
         "/Places_CouSub_ConCity_SubMCD/MapServer/4/query")
COUNTY = ("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
          "/State_County/MapServer/1/query")

TO_UTM = Transformer.from_crs(4326, 32610, always_xy=True).transform
TO_WGS = Transformer.from_crs(32610, 4326, always_xy=True).transform
OUT = Path("aoi_polygons")


def query(url: str, where: str, out_fields: str = "*"):
    q = {"where": where, "outFields": out_fields, "f": "geojson",
         "outSR": "4326", "returnGeometry": "true"}
    d = json.load(urllib.request.urlopen(url + "?" + urllib.parse.urlencode(q),
                                         timeout=60))
    return d.get("features", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--place", required=True, help="incorporated place name")
    ap.add_argument("--state", default="06", help="state FIPS (06 = California)")
    ap.add_argument("--miles", type=float, default=5.0)
    ap.add_argument("--clip-county", default=None,
                    help="county GEOID to intersect with, e.g. 06001")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    # TIGER stores NAME as "Livermore city"; BASENAME holds the bare name.
    feats = query(TIGER, f"BASENAME='{args.place}' AND STATE='{args.state}'")
    if not feats:
        feats = query(TIGER, f"NAME='{args.place}' AND STATE='{args.state}'")
    if not feats:
        print(f"no incorporated place named {args.place!r} in state {args.state}")
        return 1
    # A name can match more than one place; take the largest by area.
    geoms = [shape(f["geometry"]) for f in feats]
    base = max(geoms, key=lambda g: g.area)
    props = feats[geoms.index(base)]["properties"]

    base_utm = shp_transform(TO_UTM, base)
    meters = args.miles * 1609.344
    buf_utm = base_utm.buffer(meters, quad_segs=16)
    buf = shp_transform(TO_WGS, buf_utm)

    rec = {"place": props.get("NAME"), "geoid": props.get("GEOID"),
           "buffer_miles": args.miles,
           "city_km2": round(base_utm.area / 1e6, 1),
           "buffered_km2": round(buf_utm.area / 1e6, 1)}

    if args.clip_county:
        cf = query(COUNTY, f"GEOID='{args.clip_county}'")
        if cf:
            county = shape(cf[0]["geometry"])
            clipped_utm = shp_transform(TO_UTM, buf.intersection(county))
            rec["clipped_km2"] = round(clipped_utm.area / 1e6, 1)
            rec["clip_county"] = cf[0]["properties"].get("NAME")
            buf = buf.intersection(county)

    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.place.lower().replace(' ', '_')}_{args.miles:g}mi"
    if args.clip_county:
        stem += "_clipped"
    p = args.out / f"{stem}.geojson"
    p.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": rec,
                      "geometry": mapping(buf)}]}, indent=1))

    b = buf.bounds
    print(f"{rec['place']} ({rec['geoid']})")
    print(f"  city area      : {rec['city_km2']:,} km2")
    print(f"  +{args.miles:g} mi buffer : {rec['buffered_km2']:,} km2")
    if "clipped_km2" in rec:
        print(f"  clipped to {rec['clip_county']}: {rec['clipped_km2']:,} km2")
    print(f"  bbox           : {b[0]:.4f} {b[1]:.4f} {b[2]:.4f} {b[3]:.4f}")
    print(f"  written        : {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
