"""Fetch the Livermore street centerline from the city's public ArcGIS service.

Layer 1 of Street_Centerline_-_Public, written as GeoJSON in WGS84 -- the input
extract_centerline_latlon.py reads. Discovered from the open data portal:

    https://gisopendata.livermoreca.gov/datasets/08e97ae0e0ee43cea3b376ef0bbc9884_1
    -> services7.arcgis.com/BJisQXdgVScP0JMy/.../Street_Centerline_-_Public/FeatureServer/1

The service speaks GeoJSON natively (f=geojson), so no coordinate wrangling is
needed here beyond asking for outSR=4326; its own storage is EPSG:6420.

Usage:
    python fetch_livermore_street_centerlines.py
    python fetch_livermore_street_centerlines.py --out somewhere/else.geojson
"""
import argparse
import json
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

BASE = ("https://services7.arcgis.com/BJisQXdgVScP0JMy/arcgis/rest/services"
        "/Street_Centerline_-_Public/FeatureServer/1/query")

# extract_centerline_latlon.py defaults to this same path. The portal's export
# carried a long numeric suffix; nothing depends on it, so it is dropped here.
DEFAULT_OUT = "streets/Street_Centerline_-_Public.geojson"

# The layer's advertised maxRecordCount. Asking for more is silently capped, so
# requesting exactly this makes the paging loop predictable.
PAGE = 2000


def fetch(offset, count=PAGE):
    params = {
        "where": "1=1",
        "outFields": "*",
        "outSR": "4326",
        "f": "geojson",
        "resultOffset": str(offset),
        "resultRecordCount": str(count),
        "orderByFields": "OBJECTID",
    }
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"output path (default {DEFAULT_OUT})")
    args = ap.parse_args()

    features, offset = [], 0
    while True:
        d = fetch(offset)
        # f=geojson still reports failures in ArcGIS's own error envelope.
        if isinstance(d, dict) and d.get("error"):
            raise SystemExit(f"server error: {d['error']}")
        batch = d.get("features", [])
        if not batch:
            break
        features.extend(batch)
        print(f"  fetched {len(features)}")
        # Newer servers move this flag under `properties`; older ones leave it
        # at the top level. Check both, or paging stops after the first page.
        more = (d.get("properties", {}).get("exceededTransferLimit")
                or d.get("exceededTransferLimit"))
        if not more:
            break
        offset += len(batch)

    print(f"\ntotal: {len(features)}")

    kinds = Counter(f["geometry"]["type"] for f in features if f.get("geometry"))
    null_geom = sum(1 for f in features if not f.get("geometry"))
    print(f"geometry: {dict(kinds)}  null: {null_geom}")
    # extract_centerline_latlon.py skips anything that is not a LineString, so a
    # multi-part polyline would vanish silently rather than error. Say so here.
    stray = sum(n for k, n in kinds.items() if k != "LineString")
    if stray or null_geom:
        print(f"  WARNING: {stray + null_geom} feature(s) are not LineString --"
              " extract_centerline_latlon.py will skip these")

    print("\nFunctionalClass:")
    for k, n in Counter(f["properties"].get("FunctionalClass")
                        for f in features).most_common():
        print(f"  {n:>6}  {k}")

    verts = sum(len(f["geometry"]["coordinates"])
                for f in features if f.get("geometry"))
    print(f"\nvertices: {verts:,}")

    xs = [c[0] for f in features if f.get("geometry")
          for c in f["geometry"]["coordinates"]]
    ys = [c[1] for f in features if f.get("geometry")
          for c in f["geometry"]["coordinates"]]
    print(f"bbox: lon {min(xs):.5f}..{max(xs):.5f}  "
          f"lat {min(ys):.5f}..{max(ys):.5f}")

    # "EPSG:4326" rather than the CRS84 urn purely to match the portal export
    # this replaces. Nothing reads it -- extract_centerline_latlon.py only
    # touches ["features"] -- but a drop-in replacement should look the part.
    gj = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": features,
    }
    out = Path(__file__).resolve().parent / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(gj, f)
    print(f"\nwrote {out}  ({out.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
