"""DEPRECATED -- use `python fetch_inlets.py livermore` instead.

Superseded in full. fetch_inlets.py fetches the same layer with the same
canonical field names, and adds what this script never had: coded-domain
decoding, an OID high-water-mark fallback for servers that ignore resultOffset,
retry with backoff for the resets this host throws under load, and a `source`
column so several cities can share one file. Nothing in the pipeline calls this
any more; it is kept only so the original single-city fetch stays readable.

Fetch all storm drain inlets from Livermore's public ArcGIS FeatureServer.

Layer 2 = Inlet (active). Outputs lat/lon in WGS84 to match the street
centerline data already in derived/.
"""
import csv
import json
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

BASE = ("https://gisweb.cityoflivermore.net/arcgis/rest/services"
        "/WetUtilities/StormStructures/FeatureServer/2/query")

FIELDS = [
    "OBJECTID", "AssetID", "TypeDescription", "SubType", "GrateSize",
    "TopOfGrate", "InvertElevation1", "Depth", "OutfallID",
    "OperationalStatus", "YearInstalled", "HasGPSPoint",
    "Location", "MaintenanceArea", "MapGrid",
]

OUT_DIR = Path(__file__).resolve().parent / "derived"


def fetch(offset, count=2000):
    params = {
        "where": "1=1",
        "outFields": ",".join(FIELDS),
        "outSR": "4326",
        "f": "json",
        "resultOffset": str(offset),
        "resultRecordCount": str(count),
        "orderByFields": "OBJECTID",
    }
    url = BASE + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    features = []
    offset = 0
    while True:
        d = fetch(offset)
        if d.get("error"):
            raise SystemExit(f"server error: {d['error']}")
        batch = d.get("features", [])
        if not batch:
            break
        features.extend(batch)
        print(f"  fetched {len(features)}")
        if not d.get("exceededTransferLimit"):
            break
        offset += len(batch)

    rows = []
    for ft in features:
        g = ft.get("geometry") or {}
        a = ft["attributes"]
        rows.append({
            "lon": g.get("x"),
            "lat": g.get("y"),
            **{f: a.get(f) for f in FIELDS},
        })

    print(f"\ntotal: {len(rows)}")
    missing = sum(1 for r in rows if r["lat"] is None or r["lon"] is None)
    print(f"missing geometry: {missing}")

    print("\nTypeDescription:")
    for k, n in Counter(r["TypeDescription"] for r in rows).most_common():
        print(f"  {n:>6}  {k}")
    print("\nOperationalStatus:")
    for k, n in Counter(r["OperationalStatus"] for r in rows).most_common():
        print(f"  {n:>6}  {k}")
    print("\nSubType -> TypeDescription:")
    for k, n in sorted(Counter(
            (r["SubType"], r["TypeDescription"]) for r in rows).items()):
        print(f"  {k[0]} -> {k[1]}  ({n})")

    with_elev = sum(1 for r in rows if r["TopOfGrate"] is not None)
    with_inv = sum(1 for r in rows if r["InvertElevation1"] is not None)
    print(f"\nTopOfGrate populated:       {with_elev} / {len(rows)}")
    print(f"InvertElevation1 populated: {with_inv} / {len(rows)}")

    lats = [r["lat"] for r in rows if r["lat"] is not None]
    lons = [r["lon"] for r in rows if r["lon"] is not None]
    print(f"\nbbox: lon {min(lons):.5f}..{max(lons):.5f}  "
          f"lat {min(lats):.5f}..{max(lats):.5f}")

    OUT_DIR.mkdir(exist_ok=True)

    csv_path = OUT_DIR / "storm_inlets.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["lon", "lat"] + FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {csv_path}")

    gj = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": {f: r[f] for f in FIELDS},
            }
            for r in rows if r["lat"] is not None and r["lon"] is not None
        ],
    }
    gj_path = OUT_DIR / "storm_inlets.geojson"
    with open(gj_path, "w", encoding="utf-8") as f:
        json.dump(gj, f)
    print(f"wrote {gj_path}")


if __name__ == "__main__":
    main()
