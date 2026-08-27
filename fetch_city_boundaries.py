"""Fetch a boundary polygon for every Bay Area city, one GeoJSON per city.

Source: Census TIGERweb, the Bureau's own ArcGIS mirror of TIGER/Line
        https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb
        Incorporated Places for the cities, Counties for the region filter.

Writes: city_geojson/<slug>.geojson   one Polygon/MultiPolygon feature each
        city_geojson/_index.csv       one row per city -- what was written

One publisher for all 101 cities, deliberately. Every city also draws its own
limits on its own portal, and fetch_inlets.py already documents where that
leads: 33 publishers, 33 schemas, 33 field-name maps. Boundaries have a single
national source that is authoritative for all of them, so this script carries a
registry of nine county codes rather than a registry of a hundred endpoints.

The trade is vintage, not accuracy: TIGER reflects annexations as of its
January 1 snapshot, so a parcel annexed last spring sits outside its city here
until the next vintage. --vintage picks the snapshot.

Which places count as "Bay Area cities":

  - Incorporated places only -- the 101 cities and towns. Census Designated
    Places are unincorporated communities with no city government and no storm
    drain department; --include-cdp adds them anyway, since a study of runoff
    may care about places a study of jurisdictions does not.
  - The nine-county region: Alameda, Contra Costa, Marin, Napa, San Francisco,
    San Mateo, Santa Clara, Solano, Sonoma. --counties narrows it.

Places carry no county field -- a place may straddle a county line, so the
Bureau does not assign one. Each place is therefore located by its INTPTLAT/
INTPTLON, the Bureau's internal point, which is guaranteed to lie inside the
polygon (unlike a centroid, which for a crescent-shaped city need not). A
straddling city lands in the county holding that point and is fetched whole,
so the polygon is never clipped to the county.

Slugs match fetch_inlets.py's city keys -- livermore, san_jose -- so a city's
boundary, inlets and streets can be found under one name across the fetches.

--buffer-miles dilates every polygon outward before writing, for catching what
drains INTO a city from outside it. Read the caveats on BUFFER below before
using the result as a city limit -- a buffered file is no longer one.

Usage:
    python fetch_city_boundaries.py
    python fetch_city_boundaries.py --list
    python fetch_city_boundaries.py --buffer-miles 2
    python fetch_city_boundaries.py --counties alameda santa_clara
    python fetch_city_boundaries.py --include-cdp --vintage census2020
"""
import argparse
import csv
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = "city_geojson"

TIGERWEB = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
PLACES_SVC = TIGERWEB + "/Places_CouSub_ConCity_SubMCD/MapServer"
COUNTY_SVC = TIGERWEB + "/State_County/MapServer"

# RECOVERED COMMENT (see the recovery note in readme.txt): TIGERweb keeps one
# layer id per vintage, so a vintage is a pair of layer numbers rather than a
# query parameter. `current` is the Bureau's own default and is what the rest
# of this project lines up with.
VINTAGES = {
    "current": {"places": 4, "counties": 1, "label": "January 1, 2025"},
    "acs2024": {"places": 18, "counties": 37, "label": "ACS 2024"},
    "census2020": {"places": 25, "counties": 55, "label": "2020 Census"},
}
DEFAULT_VINTAGE = "current"

CA = "06"

# RECOVERED COMMENT: county FIPS, not GEOID -- the places layer is queried by
# STATE and PLACE and the county layer by STATE and COUNTY, so the three-digit
# code is what both sides of the filter want.
COUNTIES = {
    "alameda": "001", "contra_costa": "013", "marin": "041", "napa": "055",
    "san_francisco": "075", "san_mateo": "081", "santa_clara": "085",
    "solano": "095", "sonoma": "097",
}

# RECOVERED COMMENT: legal/statistical area description -- what a place calls
# itself. Livermore is a city, Danville a town.
LSADC = {"25": "city", "43": "town", "21": "borough", "47": "village"}

# RECOVERED COMMENT: AREALAND/AREAWATER are the Bureau's own measurements in
# square metres, and are the only independent check on a polygon's area, which
# is what _index.csv compares the buffered area against.
PLACE_FIELDS = [
    "GEOID", "NAME", "BASENAME", "STATE", "PLACE", "LSADC", "FUNCSTAT",
    "AREALAND", "AREAWATER", "INTPTLAT", "INTPTLON",
]

# RECOVERED COMMENT: places per geometry request. This is about the request
# staying inside the server's patience, not about the record cap.
CHUNK = 25


def get(url, timeout=180):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise SystemExit("TIGERweb unreachable: " + str(e)) from None
    # RECOVERED COMMENT: an ArcGIS error arrives as HTTP 200 with an "error"
    # key, so it has to be checked for or a failed query looks like an empty
    # county.
    if isinstance(d, dict) and d.get("error"):
        raise SystemExit("server error: " + str(d["error"]))
    return d


def query(service, layer, params):
    return get("%s/%d/query?%s" % (service, layer, urllib.parse.urlencode(params)))


def slugify(name):
    """BASENAME -> the key fetch_inlets.py would use. 'St. Helena' -> st_helena."""
    return re.sub("[^a-z0-9]+", "_", name.lower()).strip("_")


def rings_of(geom):
    """Every ring of a Polygon or MultiPolygon, flat.

    Ring role -- outer versus hole -- is deliberately discarded: the containment
    test below is even-odd, which gets holes right from the rings alone and does
    not depend on winding order. TIGER's winding is not guaranteed.
    """
    if geom["type"] == "Polygon":
        return geom["coordinates"]
    return [ring for part in geom["coordinates"] for ring in part]


def contains(x, y, rings):
    """Even-odd ray cast. Only ever asked about ~500 points here, so plain
    Python is fine; fetch_overture_streets.py carries the vectorised twin."""
    inside = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i][0], ring[i][1]
            xj, yj = ring[j][0], ring[j][1]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def fetch_counties(layer, keys):
    """The region filter: one polygon per requested county."""
    codes = ",".join("'%s'" % COUNTIES[k] for k in keys)
    d = query(COUNTY_SVC, layer, {"where": "STATE='%s' AND COUNTY IN (%s)"
                                  % (CA, codes),
                                  "outFields": "GEOID,NAME,BASENAME,COUNTY", "outSR": "4326",
                                  "f": "geojson", "returnGeometry": "true"})
    by_fips = {}
    for ft in d.get("features", []):
        p = ft["properties"]
        by_fips[p["COUNTY"]] = {
            "key": next(k for k, v in COUNTIES.items() if v == p["COUNTY"]),
            "geoid": p["GEOID"], "name": p["NAME"],
            "rings": rings_of(ft["geometry"]),
        }
    missing = [k for k in keys if COUNTIES[k] not in by_fips]
    if missing:
        raise SystemExit("county not returned by TIGERweb: " + ", ".join(missing))
    return [by_fips[COUNTIES[k]] for k in keys]


def select_places(layer, counties):
    """Every California place, located by internal point, kept if it lands in
    one of the requested counties.

    Attributes only: 483 rows without geometry is one small request, and it
    keeps the expensive geometry fetch to the hundred cities actually wanted.
    """
    d = query(PLACES_SVC, layer, {
        "where": "STATE='%s'" % CA, "outFields": ",".join(PLACE_FIELDS),
        "returnGeometry": "false", "f": "json"})
    feats = d.get("features", [])
    print("  %d places statewide" % len(feats))

    picked = []
    for ft in feats:
        a = ft["attributes"]
        try:
            x, y = float(a["INTPTLON"]), float(a["INTPTLAT"])
        except (TypeError, ValueError):
            print("  ! %s: no internal point, skipped" % a.get("BASENAME"))
            continue
        for co in counties:
            if contains(x, y, co["rings"]):
                picked.append((a, co))
                break
    return picked


def fetch_geometry(layer, place_codes):
    """Polygons for the selected places, chunked."""
    out = {}
    for i in range(0, len(place_codes), CHUNK):
        chunk = place_codes[i:i + CHUNK]
        codes = ",".join("'%s'" % c for c in chunk)
        d = query(PLACES_SVC, layer, {"where": "STATE='%s' AND PLACE IN (%s)"
                                      % (CA, codes),
                                      "outFields": ",".join(PLACE_FIELDS), "outSR": "4326",
                                      "f": "geojson", "returnGeometry": "true"})
        for ft in d.get("features", []):
            out[ft["properties"]["PLACE"]] = ft
        print("  geometry %d/%d" % (min(i + CHUNK, len(place_codes)),
                                    len(place_codes)))
    return out


def bbox_of(rings):
    xs = [c[0] for r in rings for c in r]
    ys = [c[1] for r in rings for c in r]
    return min(xs), min(ys), max(xs), max(ys)


# RECOVERED COMMENT -- the BUFFER note the docstring points at. --buffer-miles
# dilates each city outward by a fixed distance, and what comes back is NOT a
# city limit:
#
#   - Neighbours overlap, so the polygons no longer partition the region and a
#     point cannot be assigned to a city by testing them in turn.
#   - Detail is lost. A dilation of a couple of miles swallows every notch
#     narrower than that, fills holes, and merges the detached parts of a
#     multipart city into one blob. _index.csv records rings before and after
#     so the damage is visible.
#   - Area stops meaning anything; the summary prints the ratio for that
#     reason.
#
# It is here because runoff does not respect a city line: a study of what
# drains INTO a city wants the ground uphill of it.
#
# The dilation is done in UTM zone 10N, not Web Mercator -- a Mercator buffer
# at this latitude would be about 26% too large on the ground.
M_PER_MILE = 1609.344
UTM10N = "EPSG:32610"


def buffer_geometry(geom, miles):
    """Dilate one polygon outward by `miles`. Returns (geojson_geom, area_km2).

    shapely and pyproj are imported here rather than at module scope so the
    default path stays pure stdlib, which is what requirements.txt claims.
    """
    from pyproj import Transformer
    from shapely.geometry import mapping, shape
    from shapely.ops import transform as sh_transform

    fwd = Transformer.from_crs("EPSG:4326", UTM10N, always_xy=True).transform
    inv = Transformer.from_crs(UTM10N, "EPSG:4326", always_xy=True).transform

    metric = sh_transform(fwd, shape(geom))
    if miles:
        metric = metric.buffer(miles * M_PER_MILE)
    # RECOVERED COMMENT: TIGER polygons are not always valid, and buffer() can
    # hand back an invalid result from an invalid input. buffer(0) repairs it.
    if not metric.is_valid:
        metric = metric.buffer(0)
    return mapping(sh_transform(inv, metric)), metric.area / 1e6


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="output directory (default %s/)" % DEFAULT_OUT)
    ap.add_argument("--counties", nargs="+", metavar="KEY", default=list(COUNTIES),
                    help="restrict to these counties (default: all nine)")
    ap.add_argument("--vintage", choices=sorted(VINTAGES), default=DEFAULT_VINTAGE,
                    help="TIGER snapshot (default %s)" % DEFAULT_VINTAGE)
    ap.add_argument("--include-cdp", action="store_true",
                    help="also fetch Census Designated Places -- unincorporated communities, which have no city government")
    # RECOVERED COMMENT: read the BUFFER note above before using this.
    ap.add_argument("--buffer-miles", type=float, default=0.0, metavar="MI",
                    help="dilate every polygon outward by MI miles. Neighbours then overlap and the result is no longer a city limit -- see the BUFFER note in this file")
    # RECOVERED COMMENT: --list costs one attribute query and no geometry.
    ap.add_argument("--list", action="store_true",
                    help="list what would be written, fetch no geometry")
    args = ap.parse_args()

    if args.buffer_miles < 0:
        raise SystemExit("--buffer-miles cannot be negative; this only dilates")

    unknown = [c for c in args.counties if c not in COUNTIES]
    if unknown:
        raise SystemExit("unknown county %s; known: %s"
                         % (unknown, ", ".join(COUNTIES)))

    vint = VINTAGES[args.vintage]
    print("TIGERweb, %s vintage" % vint["label"])

    counties = fetch_counties(vint["counties"], args.counties)
    print("counties: " + ", ".join(c["name"] for c in counties))

    layers = [("incorporated", vint["places"])]
    if args.include_cdp:
        layers.append(("cdp", vint["places"] + 1))

    picked = []
    for kind, layer in layers:
        print("\n%s places (layer %d):" % (kind, layer))
        for attrs, co in select_places(layer, counties):
            picked.append((kind, layer, attrs, co))
        print("  %d in the region" % sum(1 for p in picked if p[0] == kind))

    # RECOVERED COMMENT: two places can slug to the same name only if the
    # Bureau lists them twice; refuse rather than write one over the other.
    slugs = {}
    for kind, layer, a, co in picked:
        slugs.setdefault(slugify(a["BASENAME"]), []).append(a["GEOID"])
    dupes = {s: g for s, g in slugs.items() if len(g) > 1}
    if dupes:
        raise SystemExit("slug collision, refusing to overwrite: %s" % dupes)

    picked.sort(key=lambda p: (p[3]["key"], p[2]["BASENAME"]))

    if args.list:
        print()
        for kind, layer, a, co in picked:
            print("  %-24s %s  %-8s %s"
                  % (slugify(a["BASENAME"]), a["GEOID"],
                     LSADC.get(a["LSADC"], a["LSADC"]), co["name"]))
        print("\n%d place(s); --list, nothing written" % len(picked))
        return

    geoms = {}
    for kind, layer in layers:
        codes = [a["PLACE"] for k, _, a, _ in picked if k == kind]
        if not codes:
            continue
        print("\nfetching %d %s polygons:" % (len(codes), kind))
        geoms[kind] = fetch_geometry(layer, codes)

    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for kind, layer, a, co in picked:
        ft = geoms.get(kind, {}).get(a["PLACE"])
        if ft is None:
            print("  ! %s: no geometry returned, skipped" % a["BASENAME"])
            continue
        slug = slugify(a["BASENAME"])
        geom = ft["geometry"]
        poly_km2 = None
        rings_before = len(rings_of(geom))
        if args.buffer_miles:
            geom, poly_km2 = buffer_geometry(geom, args.buffer_miles)
        rings = rings_of(geom)
        xmin, ymin, xmax, ymax = bbox_of(rings)
        props = {
            "slug": slug, "geoid": a["GEOID"], "name": a["NAME"],
            "basename": a["BASENAME"], "lsad": LSADC.get(a["LSADC"], a["LSADC"]),
            "kind": kind, "county": co["name"], "county_geoid": co["geoid"],
            "county_key": co["key"], "state": a["STATE"], "place": a["PLACE"],
            "arealand_m2": a["AREALAND"], "areawater_m2": a["AREAWATER"],
            "intptlat": a["INTPTLAT"], "intptlon": a["INTPTLON"],
            "vintage": args.vintage, "source": "tigerweb",
            # RECOVERED COMMENT: recorded on every feature, buffered or not, so
            # a file can never be mistaken for a city limit downstream.
            "buffer_miles": args.buffer_miles,
        }
        gj = {
            "type": "FeatureCollection",
            "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
            "features": [{"type": "Feature", "properties": props,
                          "geometry": geom}],
        }
        path = out_dir / (slug + ".geojson")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(gj, f)
        rows.append({
            "slug": slug, "geoid": a["GEOID"], "name": a["BASENAME"],
            "lsad": props["lsad"], "kind": kind, "county": co["name"],
            # RECOVERED COMMENT: the Bureau's own land area against the
            # polygon's. Unbuffered they agree; buffered, the gap is the point.
            "arealand_km2": round(float(a["AREALAND"]) / 1e6, 3),
            "buffer_mi": args.buffer_miles,
            "poly_km2": "" if poly_km2 is None else round(poly_km2, 3),
            "lon_min": round(xmin, 6), "lat_min": round(ymin, 6),
            "lon_max": round(xmax, 6), "lat_max": round(ymax, 6),
            "rings": len(rings), "rings_before": rings_before,
            "vertices": sum(len(r) for r in rings),
            "bytes": path.stat().st_size, "file": path.name,
        })

    if not rows:
        raise SystemExit("no geometry written -- nothing matched")

    idx = out_dir / "_index.csv"
    with open(idx, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print("\nwrote %d file(s) to %s  (%.1f MB)" % (
        len(rows), out_dir, sum(r["bytes"] for r in rows) / 1e6))
    print("wrote " + str(idx))

    by_county = {}
    for r in rows:
        by_county.setdefault(r["county"], []).append(r)
    print()
    for name in sorted(by_county):
        rs = by_county[name]
        print("  %-22s %3d  %8.1f km2" % (
            name, len(rs), sum(r["arealand_km2"] for r in rs)))
    if args.buffer_miles:
        land = sum(r["arealand_km2"] for r in rows)
        poly = sum(r["poly_km2"] for r in rows)
        holes = sum(r["rings"] - 1 for r in rows)
        holes0 = sum(r["rings_before"] - 1 for r in rows)
        print("\nBUFFERED by %g mile(s) -- these are NOT city limits." %
              args.buffer_miles)
        print("  area %.0f -> %.0f km2 (x%.1f), summed per city and so counting" % (
            land, poly, poly / land))
        print("  every overlap once per city it belongs to.")
        print("  %d of %d hole(s) survive the dilation." % (holes, holes0))

    print("\nvertices: {:,}".format(sum(r["vertices"] for r in rows)))
    xs = [r["lon_min"] for r in rows] + [r["lon_max"] for r in rows]
    ys = [r["lat_min"] for r in rows] + [r["lat_max"] for r in rows]
    print("bbox: lon %.5f..%.5f  lat %.5f..%.5f" % (
        min(xs), max(xs), min(ys), max(ys)))


if __name__ == "__main__":
    main()
