#!/usr/bin/env python3
"""Snapshot San Francisco's inlet layer from both of its sources, and diff runs.

Why this exists
---------------
SFGIS publishes the city's stormwater inlets twice. DataSF's Socrata dataset
jtgq-b7c5 is the original; an ArcGIS Online feature layer, titled "... (from
DataSF, pulled daily)", is a copy pushed every morning around 3:20 AM PT.
bay_area_stormdrain_sources.csv records the ArcGIS one, because that is the
kind of endpoint fetch_inlets.py speaks.

Measured 2026-11-15, both served 23,907 rows -- and the mirror held only 20,603
distinct assets. 3,304 records were duplicated (exactly twice, byte-identical in
all eleven attributes AND the geometry) and 3,304 DIFFERENT upstream records
were absent. Upstream has 23,907 distinct puc_maximo_asset_id, no duplicates at
all. The row counts match because a loader paging an unordered source stops when
it has taken as many rows as the source claims; what it took is another matter.

That shape -- duplicate OID offsets clustered at 2,725..2,743, duplicates dense
below OID 12,000 and absent above 18,000 -- is unstable pagination: page the
source without a deterministic sort, rows move between pages, some are read
twice and others never. If that is the mechanism, the 3,304 it loses should be
a different 3,304 tomorrow. That is an inference from the signature, not a
measurement, and this script exists to turn it into one.

What it writes
--------------
    derived/sf_inlet_snapshots/<utc>_agol.csv        every row the mirror serves
    derived/sf_inlet_snapshots/<utc>_datasf.csv      every row DataSF serves
    derived/sf_inlet_snapshots/<utc>_meta.json       counts, service edit dates,
                                                     and the duplicated/missing
                                                     id lists in full

The id lists go in the metadata deliberately. Knowing that 3,304 went missing
again says nothing; knowing WHICH 3,304 is the whole question, and a count
cannot be un-aggregated later.

Both sources are captured in the same run. A change in the mirror means nothing
unless you can show upstream did not change underneath it.

Usage:
    python snapshot_sf_inlets.py              # snapshot, then diff vs the last
    python snapshot_sf_inlets.py --compare    # diff the two most recent, no fetch
    python snapshot_sf_inlets.py --list
"""
import argparse
import csv
import datetime
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "derived", "sf_inlet_snapshots")

AGOL = ("https://services.arcgis.com/Zs2aNLFN00jrS4gG/arcgis/rest/services/"
        "Stormwater_inlets_drains_and_catch_basins_from_DataSF_pulled_daily_"
        "/FeatureServer/0")
DATASF = "https://data.sfgov.org/resource/jtgq-b7c5.json"
UA = {"User-Agent": "Mozilla/5.0"}

# The field both sources key on. puc_maximo_asset_id and dpw_node_id are in
# one-to-one correspondence (20,603 of each in the mirror, 23,907 in DataSF),
# so either would do; the Maximo id is the one a person can look up.
KEY = "puc_maximo_asset_id"

# Socrata attaches a dozen ":@computed_region_*" columns -- the boundary
# polygons each point happens to fall in, recomputed by the platform. They are
# not the publisher's data and they are not in the mirror, so they are dropped
# rather than stored and diffed every day.
SOCRATA_NOISE = ":@computed_region_"


def fetch(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def agol_rows():
    """Every row of the mirror, with the geometry flattened to lon/lat.

    Paged on ObjectId with an explicit orderByFields -- the failure this script
    is watching for is what happens when a client does NOT do that.
    """
    meta = fetch(f"{AGOL}?f=json")
    fields = [f["name"] for f in meta["fields"]]
    page = min(int(meta.get("maxRecordCount") or 2000), 2000)
    rows, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "where": "1=1", "outFields": "*", "returnGeometry": "true",
            "outSR": "4326", "f": "json", "resultOffset": offset,
            "resultRecordCount": page, "orderByFields": meta["objectIdField"]})
        d = fetch(f"{AGOL}/query?{q}")
        if d.get("error"):
            sys.exit(f"ArcGIS error: {d['error']}")
        batch = d.get("features", [])
        for f in batch:
            r = dict(f["attributes"])
            g = f.get("geometry") or {}
            r["lon"], r["lat"] = g.get("x"), g.get("y")
            rows.append(r)
        print(f"  agol: {len(rows):,}")
        if len(batch) < page:
            break
        offset += page
    return rows, fields + ["lon", "lat"], meta


def datasf_rows():
    """Every row of the DataSF original, location flattened to lon/lat.

    Ordered by :id, Socrata's own row identity, which is what makes offset
    paging here reproducible.
    """
    rows, offset = [], 0
    while True:
        q = urllib.parse.urlencode({"$limit": 50000, "$offset": offset,
                                    "$order": ":id"})
        batch = fetch(f"{DATASF}?{q}")
        for r in batch:
            r = {k: v for k, v in r.items() if not k.startswith(SOCRATA_NOISE)}
            loc = r.pop("location", None) or {}
            c = loc.get("coordinates") or [None, None]
            r["lon"], r["lat"] = c[0], c[1]
            rows.append(r)
        print(f"  datasf: {len(rows):,}")
        if len(batch) < 50000:
            break
        offset += 50000
    cols = []
    for r in rows:                       # union, first-seen order
        for k in r:
            if k not in cols:
                cols.append(k)
    return rows, cols


def duplicates(rows, key=KEY):
    """Ids appearing more than once, and whether the copies are identical.

    "Identical" ignores ObjectId, which is assigned by the server on load and
    is the one field a duplicate pair is guaranteed to differ in.
    """
    by = {}
    for r in rows:
        by.setdefault(r[key], []).append(r)
    dup = {k: v for k, v in by.items() if len(v) > 1}
    cols = [c for c in (rows[0] if rows else {}) if c != "ObjectId"]
    byte_same = sum(1 for v in dup.values()
                    if len({tuple(r.get(c) for c in cols) for r in v}) == 1)
    return sorted(dup), byte_same, {k: len(v) for k, v in dup.items()}


def write_csv(path, rows, cols):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return os.path.getsize(path)


def snapshots():
    if not os.path.isdir(OUT_DIR):
        return []
    stamps = sorted({f.split("_")[0] for f in os.listdir(OUT_DIR)
                     if f.endswith("_meta.json")})
    return stamps


def load_meta(stamp):
    with open(os.path.join(OUT_DIR, f"{stamp}_meta.json"), encoding="utf-8") as fh:
        return json.load(fh)


def take():
    """One snapshot of both sources. Returns its stamp."""
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"snapshot {stamp}")

    arows, acols, ameta = agol_rows()
    drows, dcols = datasf_rows()

    adup, abyte, asizes = duplicates(arows)
    ddup, dbyte, _ = duplicates(drows)
    aids = {r[KEY] for r in arows}
    dids = {r[KEY] for r in drows}

    asize = write_csv(os.path.join(OUT_DIR, f"{stamp}_agol.csv"), arows, acols)
    dsize = write_csv(os.path.join(OUT_DIR, f"{stamp}_datasf.csv"), drows, dcols)

    def when(ms):
        if not ms:
            return None
        return datetime.datetime.fromtimestamp(
            ms / 1000, datetime.timezone.utc).isoformat()

    edit = ameta.get("editingInfo", {})
    meta = {
        "taken_utc": stamp,
        "agol": {
            "url": AGOL,
            "rows": len(arows),
            "distinct_ids": len(aids),
            "duplicated_ids": len(adup),
            "duplicate_groups_byte_identical": abyte,
            "group_sizes": sorted(set(asizes.values())),
            # The service's own stamps: which generation of the daily push this
            # snapshot caught. Two snapshots sharing a lastEditDate are the SAME
            # push and must not be read as two refreshes that happened to agree.
            "last_edit": when(edit.get("lastEditDate")),
            "data_last_edit": when(edit.get("dataLastEditDate")),
            "row_data_loaded_at": sorted({when(r.get("data_loaded_at"))
                                          for r in arows}),
            "csv_bytes": asize,
        },
        "datasf": {
            "url": DATASF,
            "rows": len(drows),
            "distinct_ids": len(dids),
            "duplicated_ids": len(ddup),
            "row_data_loaded_at": sorted({r.get("data_loaded_at")
                                          for r in drows}),
            "csv_bytes": dsize,
        },
        "comparison": {
            "in_datasf_missing_from_agol": sorted(dids - aids),
            "in_agol_not_in_datasf": sorted(aids - dids),
            "duplicated_in_agol": adup,
        },
    }
    with open(os.path.join(OUT_DIR, f"{stamp}_meta.json"), "w",
              encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)

    print(f"\n  agol   {len(arows):,} rows, {len(aids):,} distinct, "
          f"{len(adup):,} duplicated ({abyte:,} byte-identical)")
    print(f"  datasf {len(drows):,} rows, {len(dids):,} distinct, "
          f"{len(ddup):,} duplicated")
    print(f"  missing from the mirror: {len(dids - aids):,}")
    print(f"  wrote {stamp}_agol.csv ({asize/1e6:.1f} MB), "
          f"{stamp}_datasf.csv ({dsize/1e6:.1f} MB), {stamp}_meta.json")
    return stamp


def compare(a, b):
    """What changed between two snapshots. `a` is the older."""
    ma, mb = load_meta(a), load_meta(b)
    print(f"\n{'=' * 70}\n{a}  ->  {b}\n{'=' * 70}")

    ea, eb = ma["agol"]["last_edit"], mb["agol"]["last_edit"]
    print(f"  service lastEditDate: {ea}  ->  {eb}")
    if ea == eb:
        print("  SAME PUSH -- the mirror has not reloaded between these two "
              "snapshots, so any difference below is a fetch artefact, not a "
              "refresh. Wait for the next push (~3:20 AM PT) and re-run.")

    for src in ("agol", "datasf"):
        x, y = ma[src], mb[src]
        print(f"\n  {src}: rows {x['rows']:,} -> {y['rows']:,}   "
              f"distinct {x['distinct_ids']:,} -> {y['distinct_ids']:,}   "
              f"duplicated {x['duplicated_ids']:,} -> {y['duplicated_ids']:,}")

    for label, key in (("missing from the mirror", "in_datasf_missing_from_agol"),
                       ("duplicated in the mirror", "duplicated_in_agol")):
        sa = set(ma["comparison"][key])
        sb = set(mb["comparison"][key])
        both = sa & sb
        pct = 100.0 * len(both) / max(1, min(len(sa), len(sb)))
        print(f"\n  {label}: {len(sa):,} -> {len(sb):,}")
        print(f"    in both snapshots : {len(both):,}  ({pct:.1f}% of the smaller set)")
        print(f"    only in {a}: {len(sa - sb):,}")
        print(f"    only in {b}: {len(sb - sa):,}")
        if sa and sb:
            if both == sa == sb:
                print("    -> IDENTICAL set. The loss is deterministic: the same "
                      "drains are missing every day, and a one-off patch would "
                      "hold. (It is still 13.8% of the city.)")
            elif not both:
                print("    -> DISJOINT sets. The loss is random per refresh, "
                      "exactly as unstable pagination predicts.")
            else:
                print("    -> PARTIAL overlap: some of the loss is stable, some "
                      "rotates. Neither a patch nor a retry fixes it.")
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--compare", action="store_true",
                    help="diff the two most recent snapshots without fetching")
    ap.add_argument("--list", action="store_true", help="list snapshots taken")
    args = ap.parse_args()

    if args.list:
        for s in snapshots():
            m = load_meta(s)
            print(f"  {s}  agol {m['agol']['rows']:,} rows / "
                  f"{m['agol']['distinct_ids']:,} distinct   "
                  f"push {m['agol']['last_edit']}")
        return 0

    have = snapshots()
    if not args.compare:
        stamp = take()
        have = have + [stamp]
    if len(have) < 2:
        print("\nonly one snapshot so far -- run again after the next daily "
              "push to compare.")
        return 0
    compare(have[-2], have[-1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
