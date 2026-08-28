"""Fetch Overture street segments and split them by the city_geojson/ boundaries.

Source: Overture Maps, transportation theme, segment type, read straight off the
        public S3 bucket as Parquet
        s3://overturemaps-us-west-2/release/<release>/theme=transportation
                                                     /type=segment

Reads : city_geojson/*.geojson   written by fetch_city_boundaries.py
Writes: streets/overture/<slug>.geojson   one FeatureCollection per city
        streets/overture/_index.csv       per-city totals
        streets/overture/_classes.csv     per-city, per-road-class totals

Why this and not another city portal: Livermore's centerline (step 0) is the
gold standard for Livermore and exists for nowhere else. Overture is one schema
over all 101 cities at once, which is what a regional comparison needs. It is
not a replacement -- Overture is OSM-derived road centrelines, so it carries no
FunctionalClass, no RoadType, and no city asset IDs. It carries `class`, which
is a different taxonomy with the same job.

Reading it. The transportation theme is 72 GB in 128 Parquet files, so it is
never downloaded whole. Every row carries a `bbox` struct, and Overture writes
the files in spatial order, so a bbox predicate prunes almost every row group
before a byte of geometry is read: the nine-county window is ~1.2 M segments out
of the world's ~400 M. Rows then stream in batches and are routed to cities as
they arrive, and every city's file is written as the batches go past, so peak
memory is a couple of batches rather than either the region or the output.

Assigning a segment to a city. A segment belongs to every city whose polygon it
touches -- a street on a city line lands in both files, which is the honest
answer for a boundary street and lets either city's study see it. The test is:

  1. City bbox against segment bbox, vectorised over the whole batch. Cheap,
     and it throws away almost everything.
  2. Any vertex inside the polygon, by even-odd ray cast (holes handled, winding
     irrelevant -- and Livermore genuinely has two holes).
  3. For survivors with no vertex inside, an exact edge-crossing test, but only
     for segments whose bbox lands on a cell the boundary passes through. That
     is what catches a segment cutting a corner of a city without putting a
     vertex in it. Without step 3 those would be dropped silently.

--clip additionally cuts the geometry at the boundary, so a boundary street is
split and each city's file holds only its own half. Off by default: clipping
mutates geometry and breaks the one-to-one tie back to an Overture segment id,
which is usually the more valuable property. Segments wholly inside a city are
never touched by --clip either way.

Naming a freeway. `name` is not where a motorway's identity lives, so the
`route` property carries its designation -- "I 580" -- from the routes
column. Read names alone and 18% of mainline motorway looks anonymous, I-680
included; read routes too and every mainline segment in the Livermore window
is identifiable. Ramps stay anonymous either way, which is correct: a ramp is
not a route. See flatten_routes().

`display_name` is the one to read: names.primary where there is one, the route
designation where there is not. `name` and `route` are both kept beside it
rather than being collapsed into it, because the difference matters -- a
consumer grouping streets into pages wants to know whether it is holding a
street name or a route number. _index.csv counts all three: `named`, `routed`
and `display_named`.

Usage:
    python fetch_overture_streets.py
    python fetch_overture_streets.py --dry-run
    python fetch_overture_streets.py --cities livermore san_jose --clip
    python fetch_overture_streets.py --classes motorway trunk primary secondary
    python fetch_overture_streets.py --release 2026-08-19.0 --all-subtypes
"""
import argparse
import csv
import json
import re
import struct
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.fs as pafs

ROOT = Path(__file__).resolve().parent
DEFAULT_CITY_DIR = "city_geojson"
DEFAULT_OUT = "streets/overture"

BUCKET = "overturemaps-us-west-2"
REGION = "us-west-2"
LISTING = ("https://%s.s3.amazonaws.com/?list-type=2&delimiter=/&prefix=release/"
           % BUCKET)

# RECOVERED COMMENT (see the recovery note in readme.txt): pinned fallback for
# when the bucket listing cannot be read.
FALLBACK_RELEASE = "2026-08-19.0"

# RECOVERED COMMENT: only these columns are read. `bbox` is what makes the
# predicate pushdown work, and it is cheap next to `geometry`.
#
# `routes` is here because a freeway's identity is not in `names` -- see
# flatten_routes(). It is the only one of the schema's 21 columns added since
# the recovery; the other ten omissions (connectors, speed_limits,
# access_restrictions, destinations, level_rules, prohibited_transitions,
# rail_flags, subclass_rules, sources, version) stay omitted.
COLUMNS = ["id", "names", "class", "subtype", "subclass", "road_flags",
           "road_surface", "width_rules", "routes", "geometry", "bbox"]

# The road network this project keeps, for --roads-only. The eight classified
# classes carry the streets a drainage study is about; `service` is 35% of the
# corpus at 3% named -- driveways and parking aisles -- and only its alleys are
# real back-lanes worth having, so it is admitted by subclass alone.
#
# `link` is excluded throughout. A link is a ramp, and a ramp is not a street:
# it has no name (0.3% of motorway links), no route, and no counterpart in the
# portal centerline beyond a local asset label. Excluding it is also what
# lifts the classified network from 93% to 97.7-99.9% named, because ramps
# were the whole of the gap.
ROAD_CLASSES = ["motorway", "trunk", "primary", "secondary", "tertiary",
                "residential", "living_street", "unclassified"]
SERVICE_KEEP = "alley"

# RECOVERED COMMENT: rows per streamed batch. Large enough that the vectorised
# bbox test pays for itself, small enough that a batch and its geometries fit
# in memory alongside the next one being read ahead.
BATCH = 50000

# RECOVERED COMMENT: readahead. pyarrow prefetches this many batches and
# fragments while the current one is being processed, which keeps the S3 reads
# overlapped with the point-in-polygon work rather than serialised behind it.
# Both are deliberately small: the default readahead queues enough of a 72 GB
# dataset to exhaust memory on a machine that is also holding a hundred city
# boundaries and their grids.
BATCH_READAHEAD = 2
FRAGMENT_READAHEAD = 1

# RECOVERED COMMENT: grid cell for the boundary index, in degrees. ~500 m at
# this latitude.
BOUNDARY_CELL = 0.005

# RECOVERED COMMENT: cap on the point-by-edge boolean in points_in_rings, in
# elements. A city with 20k boundary edges and a batch with 200k vertices would
# otherwise allocate a 4-billion-element array.
PIP_CHUNK = 4000000

# RECOVERED COMMENT: metres per degree, for the equirectangular length. Good to
# a fraction of a percent over a region this size, and length here is a summary
# figure rather than a survey.
M_PER_DEG_LAT = 110540.0
M_PER_DEG_LON = 111320.0


# RECOVERED COMMENT: Overture publishes a new release monthly and the newest is
# the default, so a re-run picks up whatever is current rather than silently
# pinning itself to whatever existed when this was written. The pinned release
# is only a fallback for when the listing cannot be read; the release actually
# used is recorded in every output file's metadata either way.
def latest_release():
    """Newest release under the bucket's release/ prefix."""
    try:
        req = urllib.request.Request(LISTING, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            xml = r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as e:
        print("  ! cannot list releases (%s); using pinned %s"
              % (e, FALLBACK_RELEASE))
        return FALLBACK_RELEASE
    found = sorted(re.findall("<Prefix>release/([^<]+)/</Prefix>", xml))
    if not found:
        print("  ! no releases listed; using pinned %s" % FALLBACK_RELEASE)
        return FALLBACK_RELEASE
    return found[-1]


# RECOVERED COMMENT: WKB is parsed here rather than through shapely so the
# script depends only on numpy and pyarrow. The geometry is read straight out
# of the buffer with np.frombuffer, which is a view rather than a copy, and at
# 1.2 M segments that difference is the run.
def wkb_lines(buf):
    """WKB bytes -> list of (n, 2) float64 arrays.

    Overture segments are 2D LineStrings; MultiLineString is accepted because
    nothing in the format forbids it. Anything else is an error rather than a
    silent drop -- a whole geometry type vanishing from a city file is exactly
    the kind of thing that goes unnoticed for months.
    """
    endian = buf[0]
    if endian not in (0, 1):
        raise ValueError("bad WKB byte order: %r" % endian)
    fmt = "<" if endian == 1 else ">"
    dt = "<f8" if endian == 1 else ">f8"
    gtype = struct.unpack_from(fmt + "I", buf, 1)[0]
    # RECOVERED COMMENT: the high bits flag Z/M/SRID; 1000+ is the ISO form of
    # the same thing. Either way the coordinates are not the 2D pairs assumed.
    if gtype & 0xE0000000 or gtype > 1000:
        raise ValueError("WKB carries Z/M/SRID (type %d); expected 2D" % gtype)
    if gtype == 2:
        n = struct.unpack_from(fmt + "I", buf, 5)[0]
        return [np.frombuffer(buf, dtype=dt, count=2 * n, offset=9).reshape(n, 2)]
    if gtype == 5:
        nparts = struct.unpack_from(fmt + "I", buf, 5)[0]
        parts, off = [], 9
        for _ in range(nparts):
            sub_end = buf[off]
            sfmt = "<" if sub_end == 1 else ">"
            sdt = "<f8" if sub_end == 1 else ">f8"
            n = struct.unpack_from(sfmt + "I", buf, off + 5)[0]
            parts.append(np.frombuffer(buf, dtype=sdt, count=2 * n,
                                       offset=off + 9).reshape(n, 2))
            off += 9 + 16 * n
        return parts
    raise ValueError("unexpected WKB geometry type %d (want 2 or 5)" % gtype)


def length_m(pts):
    """Ground length of a lon/lat polyline, equirectangular."""
    if len(pts) < 2:
        return 0.0
    lat = np.radians(pts[:, 1])
    dx = np.diff(pts[:, 0]) * M_PER_DEG_LON * np.cos(0.5 * (lat[:-1] + lat[1:]))
    dy = np.diff(pts[:, 1]) * M_PER_DEG_LAT
    return float(np.hypot(dx, dy).sum())


# RECOVERED COMMENT: the vectorised twin of fetch_city_boundaries.py's
# contains(). Same even-odd test, but every point in a batch is answered at
# once against every edge of a ring, which is what makes a hundred cities by a
# million segments finish.
def points_in_rings(pts, rings):
    """Even-odd ray cast, vectorised over points.

    Crossings are counted per ring and the parities XORed, which is the same
    number as counting them all at once and gives holes for free without
    knowing which ring is a hole. Points are chunked so the point-by-edge
    boolean never exceeds PIP_CHUNK elements.
    """
    n = len(pts)
    inside = np.zeros(n, dtype=bool)
    if n == 0:
        return inside
    px_all, py_all = pts[:, 0], pts[:, 1]
    for ring in rings:
        x1, y1 = ring[:-1, 0], ring[:-1, 1]
        x2, y2 = ring[1:, 0], ring[1:, 1]
        m = len(x1)
        if m == 0:
            continue
        step = max(1, PIP_CHUNK // m)
        for s in range(0, n, step):
            px = px_all[s:s + step, None]
            py = py_all[s:s + step, None]
            straddles = (y1 > py) != (y2 > py)
            # RECOVERED COMMENT: a horizontal edge divides by zero here. The
            # result is inf/nan, which fails the comparison below, which is the
            # right answer -- a horizontal edge is never crossed by a
            # horizontal ray.
            with np.errstate(divide="ignore", invalid="ignore"):
                xint = (x2 - x1) * (py - y1) / (y2 - y1) + x1
            crossings = np.count_nonzero(straddles & (px < xint), axis=1)
            inside[s:s + step] ^= (crossings & 1).astype(bool)
    return inside


# RECOVERED COMMENT: the boundary index. Built once per city, consulted once
# per candidate segment.
def boundary_cells(rings, cell=BOUNDARY_CELL):
    """Set of grid cells the polygon's own edges pass through.

    A segment whose bbox misses every one of these cells cannot cross the
    boundary, so its containment is settled by a single vertex test. Cells are
    marked from each edge's bounding box rather than by walking the edge: TIGER
    edges are metres long, so the two agree almost everywhere, and where they
    do not the answer is a few extra exact tests, never a missed crossing.
    """
    cells = set()
    for ring in rings:
        x1, y1 = ring[:-1, 0], ring[:-1, 1]
        x2, y2 = ring[1:, 0], ring[1:, 1]
        i0 = np.floor(np.minimum(x1, x2) / cell).astype(np.int64)
        i1 = np.floor(np.maximum(x1, x2) / cell).astype(np.int64)
        j0 = np.floor(np.minimum(y1, y2) / cell).astype(np.int64)
        j1 = np.floor(np.maximum(y1, y2) / cell).astype(np.int64)
        for a, b, c, d in zip(i0, i1, j0, j1):
            for i in range(a, b + 1):
                for j in range(c, d + 1):
                    cells.add((i, j))
    return cells


def near_boundary(bbox, cells, cell=BOUNDARY_CELL):
    """Does this segment bbox touch a cell the boundary passes through?"""
    xmin, ymin, xmax, ymax = bbox
    for i in range(int(np.floor(xmin / cell)), int(np.floor(xmax / cell)) + 1):
        for j in range(int(np.floor(ymin / cell)), int(np.floor(ymax / cell)) + 1):
            if (i, j) in cells:
                return True
    return False


# RECOVERED COMMENT: exact segment-versus-ring intersection, used only on the
# handful of segments the grid could not settle.
def crossing_params(p, q, rings):
    """Sorted t in (0, 1) where segment p->q crosses any ring edge."""
    rx, ry = q[0] - p[0], q[1] - p[1]
    ts = []
    for ring in rings:
        e1 = ring[:-1]
        s = ring[1:] - e1
        denom = rx * s[:, 1] - ry * s[:, 0]
        wx = e1[:, 0] - p[0]
        wy = e1[:, 1] - p[1]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (wx * s[:, 1] - wy * s[:, 0]) / denom
            u = (wx * ry - wy * rx) / denom
        ok = (denom != 0) & (t > 0) & (t < 1) & (u >= 0) & (u <= 1)
        if ok.any():
            ts.append(t[ok])
    if not ts:
        return np.empty(0)
    return np.unique(np.concatenate(ts))


def crosses(pts, rings):
    """Does this polyline cross the polygon boundary anywhere?"""
    for i in range(len(pts) - 1):
        if len(crossing_params(pts[i], pts[i + 1], rings)):
            return True
    return False


def clip_to_rings(pts, rings):
    """Split a polyline at the boundary; keep the runs that fall inside.

    Every crossing becomes a vertex, which cuts the line into runs that are each
    wholly in or wholly out. A run is classified by the midpoint of its longest
    edge -- a point that cannot sit on the boundary the way an endpoint does.
    """
    runs, cur = [], [pts[0]]
    for i in range(len(pts) - 1):
        p, q = pts[i], pts[i + 1]
        for t in crossing_params(p, q, rings):
            x = p + t * (q - p)
            cur.append(x)
            runs.append(np.array(cur))
            cur = [x]
        cur.append(q)
    runs.append(np.array(cur))

    kept = []
    for run in runs:
        if len(run) < 2:
            continue
        seg = np.argmax(np.hypot(*np.diff(run, axis=0).T))
        mid = 0.5 * (run[seg] + run[seg + 1])
        if points_in_rings(mid[None, :], rings)[0]:
            kept.append(run)
    return kept


# RECOVERED COMMENT: one City is built per boundary file and kept for the whole
# run. The rings, the bbox and the boundary grid are all computed once, because
# every batch asks the same questions of them.
class City:
    """One boundary file, ready to be asked about a batch of segments."""

    def __init__(self, path):
        with open(path, encoding="utf-8") as f:
            gj = json.load(f)
        feats = gj.get("features") or []
        if len(feats) != 1:
            raise SystemExit("%s: expected 1 feature, found %d"
                             % (path.name, len(feats)))
        ft = feats[0]
        self.props = ft.get("properties") or {}
        self.slug = self.props.get("slug") or path.stem
        self.name = self.props.get("basename") or self.slug
        self.geoid = self.props.get("geoid", "")
        self.county = self.props.get("county", "")
        g = ft["geometry"]
        if g["type"] == "Polygon":
            raw = g["coordinates"]
        elif g["type"] == "MultiPolygon":
            raw = [r for part in g["coordinates"] for r in part]
        else:
            raise SystemExit("%s: geometry is %s, want Polygon/MultiPolygon"
                             % (path.name, g["type"]))
        self.rings = [np.asarray(r, dtype=np.float64)[:, :2] for r in raw]
        allpts = np.concatenate(self.rings)
        self.xmin, self.ymin = allpts.min(axis=0)
        self.xmax, self.ymax = allpts.max(axis=0)
        self.cells = boundary_cells(self.rings)
        # RECOVERED COMMENT: running totals, filled in as the batches go past.
        self.fh = None
        self.n = 0
        self.length = 0.0
        self.named = 0
        self.routed = 0
        self.display_named = 0
        self.classes = Counter()
        self.class_len = defaultdict(float)


def load_cities(city_dir, wanted):
    paths = sorted(p for p in city_dir.glob("*.geojson"))
    if not paths:
        raise SystemExit("no *.geojson in %s -- run fetch_city_boundaries.py first"
                         % city_dir)
    cities = []
    for p in paths:
        if wanted and p.stem not in wanted:
            continue
        cities.append(City(p))
    if wanted:
        missing = sorted(set(wanted) - {c.slug for c in cities})
        if missing:
            raise SystemExit("no boundary file for: " + ", ".join(missing))
    return cities


# RECOVERED COMMENT: anonymous S3 -- the Overture bucket is public and requires
# no credentials, and asking for none avoids picking up whatever AWS profile
# happens to be configured on the machine.
def open_dataset(release):
    s3 = pafs.S3FileSystem(anonymous=True, region=REGION)
    path = "%s/release/%s/theme=transportation/type=segment" % (BUCKET, release)
    try:
        return ds.dataset(path, filesystem=s3, format="parquet")
    except OSError as e:
        raise SystemExit("cannot open %s: %s" % (path, e)) from None


def build_filter(bbox, subtypes, classes, roads_only=False):
    xmin, ymin, xmax, ymax = bbox
    # RECOVERED COMMENT: bbox overlap, not containment -- a segment crossing
    # into the window counts, and its own bbox is what Overture indexes on.
    f = ((pc.field("bbox", "xmin") < xmax) & (pc.field("bbox", "xmax") > xmin)
         & (pc.field("bbox", "ymin") < ymax) & (pc.field("bbox", "ymax") > ymin))
    if subtypes:
        f = f & pc.field("subtype").isin(subtypes)
    if roads_only:
        # A null subclass is the common case and must survive the != test:
        # in Arrow, null != "link" is null, which a filter reads as false, so
        # the whole road network would be dropped without the is_null() arm.
        sub = pc.field("subclass")
        not_link = sub.is_null() | (sub != "link")
        f = f & ((pc.field("class").isin(ROAD_CLASSES) & not_link)
                 | ((pc.field("class") == "service") & (sub == SERVICE_KEEP)))
    if classes:
        f = f & pc.field("class").isin(classes)
    return f


def feature(seg_id, name, route, cls, subtype, subclass, flags, surface, width,
            city, release, coords, glength):
    geom = ({"type": "LineString", "coordinates": coords[0]} if len(coords) == 1
            else {"type": "MultiLineString", "coordinates": coords})
    return {
        "type": "Feature",
        "properties": {
            "id": seg_id, "name": name, "route": route,
            "display_name": name or route, "class": cls,
            "subtype": subtype,
            "subclass": subclass, "flags": flags, "surface": surface,
            "width_m": width,
            "city": city.slug, "city_geoid": city.geoid, "county": city.county,
            "length_m": round(glength, 1),
            "source": "overture:" + release,
        },
        "geometry": geom,
    }


def flatten_flags(rf):
    """road_flags is a list of {values, between} -- the linear-referencing form.
    Flattened to the set of flags present anywhere on the segment, because
    nothing downstream reads a partial-length bridge."""
    if not rf:
        return None
    out = sorted({v for e in rf for v in (e.get("values") or [])})
    return ",".join(out) or None


def first_surface(rs):
    if not rs:
        return None
    return (rs[0] or {}).get("value")


def first_width(wr):
    """Published width in metres that applies to the WHOLE segment, or None.

    Overture has the column but almost nothing in it: 6,628 of the region's
    1.9 M road segments, 0.34%. It is passed through anyway so
    build_street_polygons.py can prefer a real number to its class default on
    the rare segment that has one, and so the coverage stays visible rather
    than being assumed away.

    Rules carrying `between` describe one stretch of the segment -- a bridge
    deck narrowing, say -- and are skipped, because the consumer applies this
    number to the entire geometry. Taking a ranged width would silently widen
    or narrow the whole street to match one span of it.
    """
    for rule in wr or []:
        if not rule or rule.get("between") is not None:
            continue
        v = rule.get("value")
        if v is not None:
            return v
    return None


def flatten_routes(rt):
    """The route designations a segment carries -- "I 580" -- or None.

    A freeway's identity is not in `names`. OSM keeps the route number in `ref`
    and the proper name in `name`, and Overture follows it: I-680 through
    Livermore carries no name at all, only routes=[{network: US:I, ref: 680}],
    while I-580 carries both "Arthur H. Breed Junior Freeway" and ref 580.
    Reading names alone leaves 18% of mainline motorway looking anonymous, and
    the portal centerline this project compares against calls the same road
    I580 EB -- a ref, not a name.

    Entries with no `ref` are dropped: those are the scenic-byway and
    historic-trail overlays (US:CA:Scenic, US:NHT) that decorate a road rather
    than identify it. Entries carrying `between` are dropped for the reason
    first_width() drops them -- they describe one stretch of the segment, and
    this value is read as applying to the whole of it.

    Ramps are not rescued by this. A link has neither name nor ref (5% of
    motorway links carry either), because a ramp is not a route; the portal's
    AIRWAY OFF I580 EB is a local asset label with no global counterpart.
    """
    if not rt:
        return None
    out = []
    for e in rt:
        if not e or e.get("between") is not None:
            continue
        ref = e.get("ref")
        if not ref:
            continue
        # US:I -> I, US:CA -> CA, US:CA:CR -> CR. The leading namespace is
        # Overture's way of scoping the network to a country and state; what
        # goes on a sign, and what the portal centerline calls the same road,
        # is the last component alone.
        net = (e.get("network") or "").rsplit(":", 1)[-1]
        out.append("%s %s" % (net, ref) if net else ref)
    return ",".join(dict.fromkeys(out)) or None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--city-dir", default=DEFAULT_CITY_DIR,
                    help="boundaries from fetch_city_boundaries.py (default %s/)"
                         % DEFAULT_CITY_DIR)
    ap.add_argument("--outdir", default=DEFAULT_OUT,
                    help="output directory (default %s/)" % DEFAULT_OUT)
    ap.add_argument("--cities", nargs="+", metavar="SLUG",
                    help="restrict to these city slugs (default: every file)")
    ap.add_argument("--release", help="Overture release (default: newest)")
    ap.add_argument("--classes", nargs="+", metavar="CLASS",
                    help="keep only these road classes, e.g. motorway primary")
    ap.add_argument("--all-subtypes", action="store_true",
                    help="keep rail and water segments too, not just subtype=road")
    ap.add_argument("--roads-only", action="store_true",
                    help="keep only the drivable street network: the eight "
                         "classified classes plus service alleys, ramps "
                         "(subclass=link) excluded. Cuts the corpus ~64%%")
    ap.add_argument("--clip", action="store_true",
                    help="cut geometry at the city boundary instead of keeping whole segments (mutates geometry; see the docstring)")
    # RECOVERED COMMENT: --batch trades memory against per-batch overhead.
    ap.add_argument("--batch", type=int, default=BATCH,
                    help="rows per streamed batch (default %d)" % BATCH)
    ap.add_argument("--dry-run", action="store_true",
                    help="report the release, window and row count, write nothing")
    args = ap.parse_args()

    city_dir = ROOT / args.city_dir
    cities = load_cities(city_dir, set(args.cities) if args.cities else None)
    print("%d boundary file(s) from %s" % (len(cities), city_dir))

    bbox = (min(c.xmin for c in cities), min(c.ymin for c in cities),
            max(c.xmax for c in cities), max(c.ymax for c in cities))
    print("window: lon %.5f..%.5f  lat %.5f..%.5f" % (bbox[0], bbox[2],
                                                      bbox[1], bbox[3]))

    release = args.release or latest_release()
    print("release: " + release)
    dataset = open_dataset(release)
    subtypes = None if args.all_subtypes else ["road"]
    filt = build_filter(bbox, subtypes, args.classes, args.roads_only)
    print("filter: subtype=%s  class=%s"
          % ("any" if subtypes is None else ",".join(subtypes),
             "any" if not args.classes else ",".join(args.classes)))

    if args.dry_run:
        t = time.time()
        n = dataset.count_rows(filter=filt)
        print("\n%s rows match (%.1fs) -- --dry-run, nothing written"
              % ("{:,}".format(n), time.time() - t))
        for c in sorted(cities, key=lambda c: c.slug):
            print("  %-24s bbox lon %.4f..%.4f lat %.4f..%.4f  %d boundary cell(s)"
                  % (c.slug, c.xmin, c.xmax, c.ymin, c.ymax, len(c.cells)))
        return

    out_dir = ROOT / args.outdir
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"release": release, "theme": "transportation", "type": "segment",
            "subtype": subtypes, "classes": args.classes, "clipped": args.clip,
            "roads_only": args.roads_only,
            "fetched": time.strftime("%Y-%m-%d")}
    for c in cities:
        c.fh = open(out_dir / (c.slug + ".geojson"), "w", encoding="utf-8")
        c.fh.write('{"type":"FeatureCollection","crs":{"type":"name","properties":{"name":"EPSG:4326"}},"metadata":'
                   # RECOVERED COMMENT: written by hand rather than by json.dump
                   # so features can be streamed out one at a time.
                   + json.dumps({**meta, "city": c.props})
                   + ',"features":[\n')

    read = matched = 0
    t0 = time.time()
    try:
        for batch in dataset.to_batches(filter=filt, columns=COLUMNS,
                                        batch_size=args.batch,
                                        batch_readahead=BATCH_READAHEAD,
                                        fragment_readahead=FRAGMENT_READAHEAD):
            if batch.num_rows == 0:
                continue
            read += batch.num_rows

            bb = batch.column("bbox")
            sx0 = bb.field("xmin").to_numpy(zero_copy_only=False)
            sx1 = bb.field("xmax").to_numpy(zero_copy_only=False)
            sy0 = bb.field("ymin").to_numpy(zero_copy_only=False)
            sy1 = bb.field("ymax").to_numpy(zero_copy_only=False)

            ids = batch.column("id").to_pylist()
            names = batch.column("names").field("primary").to_pylist()
            classes = batch.column("class").to_pylist()
            subtys = batch.column("subtype").to_pylist()
            subcls = batch.column("subclass").to_pylist()
            rflags = batch.column("road_flags").to_pylist()
            rsurf = batch.column("road_surface").to_pylist()
            rwidth = batch.column("width_rules").to_pylist()
            rroutes = batch.column("routes").to_pylist()
            wkbs = batch.column("geometry").to_pylist()
            # RECOVERED COMMENT: geometry is parsed lazily and cached per row,
            # because most rows in a batch are candidates for no city at all
            # and parsing them would be the bulk of the work.
            geoms = [None] * batch.num_rows
            # RECOVERED COMMENT: which rows landed somewhere, for the totals.
            hit_any = np.zeros(batch.num_rows, dtype=bool)
            for c in cities:
                cand = np.nonzero((sx0 < c.xmax) & (sx1 > c.xmin)
                                  & (sy0 < c.ymax) & (sy1 > c.ymin))[0]
                if cand.size == 0:
                    continue
                for k in cand:
                    if geoms[k] is None:
                        geoms[k] = wkb_lines(wkbs[k])
                # RECOVERED COMMENT: every vertex of every candidate, tested in
                # one call, then folded back to per-segment answers by `owner`.
                parts = [p for k in cand for p in geoms[k]]
                owner = np.repeat(np.arange(len(cand)),
                                  [sum(len(p) for p in geoms[k]) for k in cand])
                inside = points_in_rings(np.concatenate(parts), c.rings)
                any_in = np.zeros(len(cand), dtype=bool)
                np.logical_or.at(any_in, owner, inside)

                for pos, k in enumerate(cand):
                    if not any_in[pos]:
                        # RECOVERED COMMENT: no vertex inside. It can still cut
                        # a corner of the city, but only if its bbox touches a
                        # cell the boundary runs through -- step 3.
                        if not near_boundary((sx0[k], sy0[k], sx1[k], sy1[k]),
                                             c.cells):
                            continue
                        if not any(crosses(p, c.rings) for p in geoms[k]):
                            continue
                    # RECOVERED COMMENT: --clip cuts at the boundary.
                    parts_out = geoms[k]
                    if args.clip:
                        touches = near_boundary((sx0[k], sy0[k], sx1[k], sy1[k]),
                                                c.cells)
                        if touches:
                            parts_out = [q for p in parts_out
                                         for q in clip_to_rings(p, c.rings)]
                            if not parts_out:
                                continue
                    # RECOVERED COMMENT: 7 decimals is ~1 cm.
                    glen = sum(length_m(p) for p in parts_out)
                    coords = [[[round(x, 7), round(y, 7)] for x, y in p]
                              for p in parts_out]
                    route = flatten_routes(rroutes[k])
                    ft = feature(ids[k], names[k], route, classes[k], subtys[k],
                                 subcls[k], flatten_flags(rflags[k]),
                                 first_surface(rsurf[k]), first_width(rwidth[k]),
                                 c, release, coords, glen)
                    c.fh.write((",\n" if c.n else "") + json.dumps(ft))
                    c.n += 1
                    c.length += glen
                    c.named += 1 if names[k] else 0
                    c.routed += 1 if route else 0
                    c.display_named += 1 if (names[k] or route) else 0
                    c.classes[classes[k]] += 1
                    c.class_len[classes[k]] += glen
                    hit_any[k] = True

            matched += int(hit_any.sum())
            print("  read {:,}  assigned {:,}  ({:.0f}s)".format(
                read, matched, time.time() - t0))
            sys.stdout.flush()
    finally:
        for c in cities:
            if c.fh:
                c.fh.write("\n]}\n")
                c.fh.close()

    print("\nread {:,} segment(s) in the window; {:,} fell in a city ({:,} outside every boundary)"
          .format(read, matched, read - matched))

    rows = []
    for c in sorted(cities, key=lambda c: (c.county, c.name)):
        path = out_dir / (c.slug + ".geojson")
        rows.append({
            "slug": c.slug, "geoid": c.geoid, "name": c.name, "county": c.county,
            "segments": c.n, "length_km": round(c.length / 1000.0, 2),
            "named": c.named, "unnamed": c.n - c.named, "routed": c.routed,
            "display_named": c.display_named,
            "classes": len(c.classes),
            "top_class": c.classes.most_common(1)[0][0] if c.classes else "",
            "bytes": path.stat().st_size, "file": path.name,
        })

    idx = out_dir / "_index.csv"
    with open(idx, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    cls_path = out_dir / "_classes.csv"
    with open(cls_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["slug", "name", "county", "class", "segments", "length_km"])
        for c in sorted(cities, key=lambda c: (c.county, c.name)):
            for cls, n in c.classes.most_common():
                w.writerow([c.slug, c.name, c.county, cls, n,
                            round(c.class_len[cls] / 1000.0, 2)])

    print("wrote %d file(s) to %s  (%.1f MB)" % (
        len(rows), out_dir, sum(r["bytes"] for r in rows) / 1e6))
    print("wrote %s\nwrote %s" % (idx, cls_path))

    empty = [r["slug"] for r in rows if r["segments"] == 0]
    if empty:
        print("\n! no segments for: " + ", ".join(empty))

    print("\ntotal: {:,} assignment(s), {:,.0f} km".format(
        sum(r["segments"] for r in rows), sum(r["length_km"] for r in rows)))
    dup = sum(r["segments"] for r in rows) - matched
    print("%s segment(s) landed in more than one city (boundary streets)"
          % "{:,}".format(dup))
    print("\nlargest:")
    for r in sorted(rows, key=lambda r: -r["segments"])[:10]:
        print("  %-24s %7d seg  %9.1f km  %s"
              % (r["slug"], r["segments"], r["length_km"], r["county"]))


if __name__ == "__main__":
    main()
