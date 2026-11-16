"""Merge several finished city corpora into one overview, with the cities in
between drawn but not loadable.

San Francisco and South San Francisco do not touch. Their limits are 3.3 km
apart and Daly City, Colma and Brisbane sit in the space. Brisbane publishes
its catch basins and is built as a full city here; Daly City and Colma do not
and cannot be, so the map still has a hole in it. Daly City secures the
Utilities folder on its own ArcGIS Server, and Colma's layer -- 404 nodes, all
of them carrying an elevation -- went private between the survey and this
build. The choice this script makes is to draw their STREETS in a muted grey
that is not tappable and says why, rather than leave blank ground that reads as
"no drains here". The drains are there; the cities just do not publish them.

Two things make the merge more than a concatenation.

The corpora OVERLAP. Each city's streets were cut against its buffered AOI,
which runs about two miles past the line, so hundreds of segments sit in more
than one set with identical geometry -- mostly in Daly City, where the haloes
meet. Drawn twice they would stack two different elevation sources on one road.
They are de-duplicated here on an endpoint key, and the copy that survives is
the one from the city the segment actually lies in.

Street NAMES collide too. 26 names have a page in both corpora. 25 are
homonyms -- San Francisco's Irving Street and South San Francisco's are 12 km
apart -- and one, Junipero Serra Boulevard, is genuinely one road crossing the
county line, with 165 shared vertices. All 26 are qualified with their city
rather than merged: two pages that each cover their own stretch tell the truth,
where one page spanning both would claim a continuity the elevation data does
not have (the two stretches were sampled off different DEMs, in different
CRSs).

Nothing is re-sampled and no DEM is re-warped. Each corpus was already built
against its own collect in its own projection; this only merges the outputs.

Usage:
    python merge_cities.py
    python merge_cities.py --cities san_francisco south_san_francisco \
        --gap daly_city colma brisbane --name bay_north
"""

import argparse
import json
import os
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd
from shapely.geometry import LineString, shape
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
HDR = {"User-Agent": "Mozilla/5.0"}

# Census TIGERweb Incorporated Places. The REAL limits, not the 2-mile AOI the
# streets were cut with -- the whole point here is to know which city a segment
# is actually in. BASENAME, not NAME: NAME carries the suffix ("Colma town").
TIGERWEB = ("https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
            "/Places_CouSub_ConCity_SubMCD/MapServer/4")
LIMITS_DIR = os.path.join(HERE, "city_geojson", "_real")

DEFAULT_CITIES = ["san_francisco", "south_san_francisco", "brisbane",
                  "san_bruno", "burlingame"]
# The corridor between the built cities. Greying them all under one caption
# would be a lie: Daly City genuinely publishes nothing, but San Bruno, San
# Mateo and Belmont publish perfectly good inlet layers that simply have not
# been built yet, and Millbrae publishes a catalogue with the coordinates
# withheld. Each says why it is grey, in its own words.
DEFAULT_GAP = ["daly_city", "colma", "millbrae", "hillsborough"]

GAP_REASONS = {
    "daly_city": "publishes no inlet data (its Utilities folder is secured)",
    "colma": "withdrew its inlet layer from public access",
    "hillsborough": "publishes no GIS at all",
    "millbrae": "catalogues 990 inlets but withholds their coordinates",
    # Kept for when the map reaches further south again. San Carlos was built
    # and then dropped from this run; its corpus is still on disk.
    "san_mateo": "publishes 6,171 inlets - not yet built",
    "belmont": "publishes 1,008 inlets - not yet built",
    "san_carlos": "publishes 2,133 inlets - built, not in this run",
}
GAP_FALLBACK = "no inlet data in this build"
DEFAULT_NAME = "SF_and_North_Peninsula"
SMOOTH = "10m"

# The census name for a slug, where title-casing the slug would not give it.
CENSUS_NAME = {
    "san_francisco": "San Francisco",
    "south_san_francisco": "South San Francisco",
    "daly_city": "Daly City",
    "colma": "Colma",
    "brisbane": "Brisbane",
    "burlingame": "Burlingame",
    "san_carlos": "San Carlos",
    "san_bruno": "San Bruno",
    "millbrae": "Millbrae",
    "hillsborough": "Hillsborough",
    "san_mateo": "San Mateo",
    "belmont": "Belmont",
}

# What the live corpora contain, so the gap is drawn from the same vocabulary.
# Overture's road subtype also carries footway, path, steps, sidewalk and
# crosswalk; the built corpora hold none of them, and a grey mesh of footpaths
# would swamp the streets it is meant to sit behind.
ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary", "tertiary",
                "residential", "living_street", "unclassified", "service"}

# Endpoint rounding for the duplicate test. 6 decimal places is about 0.1 m
# here. Two cuts of the SAME Overture release carry byte-identical geometry,
# so this is an exactness test with a tolerance, not a fuzzy match.
KEY = 6


def get(url, timeout=120):
    req = urllib.request.Request(url, headers=HDR)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def real_limits(slug):
    """A city's actual boundary polygon, cached under city_geojson/_real/."""
    os.makedirs(LIMITS_DIR, exist_ok=True)
    path = os.path.join(LIMITS_DIR, slug + ".geojson")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            gj = json.load(fh)
    else:
        base = CENSUS_NAME.get(slug, slug.replace("_", " ").title())
        q = {"where": "BASENAME='%s' AND STATE='06'" % base,
             "outFields": "NAME,GEOID", "returnGeometry": "true",
             "outSR": 4326, "f": "geojson"}
        gj = get(TIGERWEB + "/query?" + urllib.parse.urlencode(q))
        if not gj.get("features"):
            raise SystemExit("TIGERweb has no incorporated place '%s'" % base)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(gj, fh)
        print("    cached limits -> %s" % os.path.relpath(path, HERE))
    return unary_union([shape(f["geometry"]) for f in gj["features"]])


def corpus_paths(slug, smooth):
    return (os.path.join(HERE, "derived", slug, "segments_vertices.csv"),
            os.path.join(HERE, "Stormdrain_map", slug, "streets_" + smooth))


def load_corpus(slug, smooth):
    """A city's vertices, narrowed to the segments that actually have a page."""
    vpath, pages = corpus_paths(slug, smooth)
    for p in (vpath, os.path.join(pages, "_index.csv")):
        if not os.path.exists(p):
            raise SystemExit("%s: missing %s" % (slug, os.path.relpath(p, HERE)))
    v = pd.read_csv(vpath)
    idx = pd.read_csv(os.path.join(pages, "_index.csv"))
    oid2page = {int(o): pg for pg, segs in zip(idx["page"], idx["segments"])
                for o in str(segs).split()}
    v["page"] = v.OBJECTID.map(oid2page)
    orphan = int(v.page.isna().sum())
    v = v[v.page.notna()].copy()
    v["city"] = slug
    idx["city"] = slug
    print("  %-22s %6d pages  %7d vertices  (%d vertices with no page)"
          % (slug, len(idx), len(v), orphan))
    return v, idx


def endpoint_keys(v):
    """One key per segment, from its first and last vertex."""
    v = v.sort_values(["OBJECTID", "vertex_index"], kind="stable")
    g = v.groupby("OBJECTID", sort=False)
    a, b = g.head(1).set_index("OBJECTID"), g.tail(1).set_index("OBJECTID")
    return (a.lat.round(KEY).astype(str) + "," + a.lon.round(KEY).astype(str)
            + "|" + b.lat.round(KEY).astype(str) + "," + b.lon.round(KEY).astype(str))


def main():
    ap = argparse.ArgumentParser(
        description="merge finished city corpora into one overview")
    ap.add_argument("--cities", nargs="+", default=DEFAULT_CITIES,
                    metavar="SLUG", help="corpora to merge, primary first")
    ap.add_argument("--gap", nargs="+", default=DEFAULT_GAP, metavar="SLUG",
                    help="cities drawn but not loadable (default: %s)"
                         % " ".join(DEFAULT_GAP))
    ap.add_argument("--name", default=DEFAULT_NAME,
                    help="slug for the merged corpus (default %s)" % DEFAULT_NAME)
    ap.add_argument("--smooth", default=SMOOTH,
                    help="which page corpus to merge (default %s)" % SMOOTH)
    ap.add_argument("--no-gap", action="store_true",
                    help="skip the in-between cities entirely")
    args = ap.parse_args()

    out_derived = os.path.join(HERE, "derived", args.name)
    out_pages = os.path.join(HERE, "Stormdrain_map", args.name,
                             "streets_" + args.smooth)
    os.makedirs(out_derived, exist_ok=True)
    os.makedirs(out_pages, exist_ok=True)

    # ------------------------------------------------ the live corpora
    print("reading corpora:")
    verts, idxs = [], []
    for slug in args.cities:
        v, i = load_corpus(slug, args.smooth)
        verts.append(v)
        idxs.append(i)

    print()
    print("city limits:")
    lim = {}
    for slug in args.cities + ([] if args.no_gap else args.gap):
        lim[slug] = real_limits(slug)
        print("  %-22s bounds %s"
              % (slug, [round(q, 4) for q in lim[slug].bounds]))

    # ------------------------------------------ each corpus to its own city
    # Every corpus reaches about two miles past its city, so three of them
    # overlap heavily -- Brisbane's buffered AOI alone pulled in 588 km of
    # street for a city of 4,600, half of whose points fell outside its own DEM
    # tiles. A page is kept by the corpus whose city holds MOST of it. A page
    # that belongs to a city nobody built (Daly City, San Bruno) is kept from
    # the primary, which is the only corpus that will claim it twice.
    #
    # Without this the merge duplicates at the page level rather than the
    # segment level: South San Francisco and Brisbane would each contribute a
    # "Hillside Boulevard", built off different DEMs, and the name-collision
    # pass would dress the pair up as two honest homonyms.
    print()
    print("assigning pages to the city that holds most of each:")
    from shapely.geometry import Point
    prep_all = {s: prep(p) for s, p in lim.items()}
    homes, keeps = [], []
    for v, i, slug in zip(verts, idxs, args.cities):
        pts = [Point(x, y) for x, y in zip(v.lon.to_numpy(), v.lat.to_numpy())]
        tree = STRtree(pts)
        home = np.full(len(v), "", dtype=object)
        for nm, pp in prep_all.items():
            home[tree.query(lim[nm], predicate="covers")] = nm
        v = v.assign(home=home)
        maj = (v.groupby(["page", "home"]).size().rename("n").reset_index()
               .sort_values("n", ascending=False).drop_duplicates("page")
               .set_index("page")["home"])
        i = i.assign(home=i.page.map(maj).fillna(""))
        mine = i.home == slug
        # a page in nobody's built city: the primary keeps it
        orphan = (~i.home.isin(args.cities)) & (slug == args.cities[0])
        keep = mine | orphan
        print("  %-22s keeps %4d of %4d  (%d its own, %d in no built city, "
              "%d handed to another corpus)"
              % (slug, int(keep.sum()), len(i), int(mine.sum()),
                 int(orphan.sum()), int((~keep).sum())))
        homes.append(home)
        keeps.append(set(i.loc[keep, "page"]))
        idxs[args.cities.index(slug)] = i[keep].drop(columns=["home"]).copy()
    verts = [v[v.page.isin(k)].copy()
             for v, k in zip(verts, keeps)]

    # ------------------------------------------- de-duplicate the haloes
    # A segment in two corpora belongs to the city it physically lies in. Where
    # it lies in neither (both haloes, e.g. Daly City) the primary wins, which
    # keeps the choice deterministic instead of depending on argument order.
    print()
    print("de-duplicating the overlapping haloes:")
    keyed = []
    for v, slug in zip(verts, args.cities):
        k = endpoint_keys(v).rename("key")
        keyed.append(k)
        v["key"] = v.OBJECTID.map(k)

    seen, drop_per_city = {}, {s: 0 for s in args.cities}
    for slug, k in zip(args.cities, keyed):
        for oid, key in k.items():
            seen.setdefault(key, []).append((slug, oid))
    contested = {k: v for k, v in seen.items() if len(v) > 1}
    print("  %d segment(s) present in more than one corpus" % len(contested))

    # whose copy survives, per key
    winner = {}
    prepped = {s: prep(p) for s, p in lim.items() if s in args.cities}
    for key, owners in contested.items():
        slug0, oid0 = owners[0]
        v0 = verts[args.cities.index(slug0)]
        pt = v0.loc[v0.OBJECTID == oid0, ["lon", "lat"]].iloc[0]
        from shapely.geometry import Point
        p = Point(float(pt.lon), float(pt.lat))
        home = next((s for s in args.cities if prepped[s].covers(p)), None)
        winner[key] = home if home and any(s == home for s, _ in owners) \
            else args.cities[0]
    for slug, k in zip(args.cities, keyed):
        for key in k.values:
            if key in winner and winner[key] != slug:
                drop_per_city[slug] += 1
    for slug in args.cities:
        print("     %-22s drops %4d segment(s) to another corpus"
              % (slug, drop_per_city[slug]))

    kept = []
    for v, slug in zip(verts, args.cities):
        mask = ~v.key.isin(winner) | (v.key.map(winner).fillna(slug) == slug)
        kept.append(v[mask].copy())

    # ------------------------------------------------ qualify colliding names
    counts = {}
    for i in idxs:
        for nm in set(i.display_name):
            counts[nm] = counts.get(nm, 0) + 1
    collide = {nm for nm, n in counts.items() if n > 1}
    print()
    print("  %d street name(s) appear in more than one corpus; qualifying with "
          "the city" % len(collide))

    label = {s: CENSUS_NAME.get(s, s.replace("_", " ").title())
             for s in args.cities}

    def qualify(col, slug):
        return col.where(~col.isin(collide), col + " (" + label[slug] + ")")

    # ------------------------------------------- re-key and write the merge
    merged_v, merged_i, off = [], [], 0
    for v, i, slug in zip(kept, idxs, args.cities):
        span = int(max(v.OBJECTID.max(), i_max(i))) + 1
        v = v.copy()
        i = i.copy()
        v["OBJECTID"] = v.OBJECTID + off
        i["segments"] = i.segments.map(
            lambda s: " ".join(str(int(o) + off) for o in str(s).split()))
        v["display_name"] = qualify(v.display_name, slug)
        i["display_name"] = qualify(i.display_name, slug)
        # The page key the overview joins on is `page`; qualify it the same way
        # so two cities' "Irving Street 1 of 2" stay apart.
        i["page"] = np.where(i.display_name.str.endswith(")"),
                             i.page.astype(str) + " (" + label[slug] + ")",
                             i.page.astype(str))
        v["page"] = np.where(v.display_name.str.endswith(")"),
                             v.page.astype(str) + " (" + label[slug] + ")",
                             v.page.astype(str))
        # The overview turns `file` into an iframe src by prefixing the hop
        # from index.html to the pages dir. Point each row back at the city
        # corpus it was built in, so 542 MB of pages are not copied.
        i["file"] = ("../../%s/streets_%s/" % (slug, args.smooth)) + i.file.astype(str)
        merged_v.append(v)
        merged_i.append(i)
        off += span

    V = pd.concat(merged_v, ignore_index=True)
    I = pd.concat(merged_i, ignore_index=True)
    V = V.drop(columns=["key"])

    vout = os.path.join(out_derived, "segments_vertices.csv")
    iout = os.path.join(out_pages, "_index.csv")
    V.to_csv(vout, index=False)
    I.to_csv(iout, index=False)
    print()
    print("merged corpus: %d pages, %d segments, %d vertices"
          % (len(I), V.OBJECTID.nunique(), len(V)))
    print("  -> %s" % os.path.relpath(vout, HERE))
    print("  -> %s" % os.path.relpath(iout, HERE))

    # --------------------------------------------------------- the gap
    if args.no_gap:
        return
    print()
    print("the in-between cities (drawn, not loadable):")
    live_keys = set()
    for v, slug in zip(kept, args.cities):
        live_keys |= set(endpoint_keys(v).values)

    gap_rows, oid = [], int(V.OBJECTID.max()) + 1_000_000
    for slug in args.gap:
        src = os.path.join(HERE, "streets", "overture", slug + ".geojson")
        if not os.path.exists(src):
            print("  %-22s no street file, skipped" % slug)
            continue
        with open(src, encoding="utf-8") as fh:
            gj = json.load(fh)
        poly = prep(lim[slug])
        n_in = n_dup = 0
        for f in gj["features"]:
            p = f["properties"]
            if p.get("class") not in ROAD_CLASSES:
                continue
            coords = f["geometry"]["coordinates"]
            if f["geometry"]["type"] != "LineString" or len(coords) < 2:
                continue
            ls = LineString(coords)
            if not poly.intersects(ls):
                continue
            # majority of the line inside the real limits, not merely touching
            if not poly.covers(ls.interpolate(0.5, normalized=True)):
                continue
            n_in += 1
            key = ("%s,%s|%s,%s"
                   % (round(coords[0][1], KEY), round(coords[0][0], KEY),
                      round(coords[-1][1], KEY), round(coords[-1][0], KEY)))
            if key in live_keys:
                n_dup += 1
                continue
            for vi, (lon, lat) in enumerate(coords):
                gap_rows.append((oid, slug,
                                 GAP_REASONS.get(slug, GAP_FALLBACK),
                                 p.get("class") or "unclassified",
                                 vi, lat, lon))
            oid += 1
        print("  %-22s %5d road(s) inside the limits, %d already in a corpus, "
              "%d kept" % (slug, n_in, n_dup, n_in - n_dup))

    G = pd.DataFrame(gap_rows, columns=["OBJECTID", "city", "reason",
                                        "road_class", "vertex_index",
                                        "lat", "lon"])
    gout = os.path.join(out_derived, "gap_vertices.csv")
    G.to_csv(gout, index=False)
    print()
    print("gap: %d segments, %d vertices" % (G.OBJECTID.nunique(), len(G)))
    print("  -> %s" % os.path.relpath(gout, HERE))


def i_max(idx):
    """The largest OBJECTID any page in this index claims."""
    return max(int(o) for s in idx.segments for o in str(s).split())


if __name__ == "__main__":
    main()
