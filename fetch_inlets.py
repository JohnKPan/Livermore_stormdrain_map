"""Fetch storm drain inlets from a Bay Area city's public ArcGIS service.

Livermore was the first city; bay_area_stormdrain_sources.csv found 32 more that
publish inlet points. The publishers agree on almost nothing -- field names, units
and which layer of a storm network counts as "the inlets" all differ -- so this
script keeps a small registry of vetted endpoints and maps each city's native
fields onto one canonical schema.

The canonical names are Livermore's, because plot_street_drains.py and
plot_street_bokeh.py already read them:

    AssetID  TypeDescription  TopOfGrate  InvertElevation1

A city supplies whichever it has; the rest come out blank. Every row also carries
a `source` column naming the city, so several cities can be concatenated into one
frame and still be told apart.

Usage:
    python fetch_inlets.py                       # livermore (the default)
    python fetch_inlets.py san_jose
    python fetch_inlets.py --list
    python fetch_inlets.py san_jose --all-fields --out derived/sj.csv
"""
import argparse
import csv
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "derived"

# Canonical schema, in output order. Livermore's names, kept because the plotting
# scripts already reference them -- renaming here would be a rename in four files.
CANONICAL = ["AssetID", "TypeDescription", "SubType", "TopOfGrate",
             "InvertElevation1", "Depth", "OperationalStatus", "YearInstalled"]

# --------------------------------------------------------------------------
# City registry.
#
#   url     a layer endpoint (.../FeatureServer/N or .../MapServer/N)
#   fields  native fields to request, in output order. Not "*": Livermore's
#           layer carries 54 columns and the original script deliberately took
#           15 of them. --all-fields overrides.
#   canon   canonical name -> native field. Anything unmapped comes out blank.
#   out     default output stem under derived/
#
# Endpoints and field names come from bay_area_stormdrain_sources.csv; re-run
# survey_bay_area_sources.py if a service moves.
# --------------------------------------------------------------------------
CITIES = {
    "livermore": {
        "label": "Livermore",
        "url": ("https://gisweb.cityoflivermore.net/arcgis/rest/services"
                "/WetUtilities/StormStructures/FeatureServer/2"),
        "fields": ["OBJECTID", "AssetID", "TypeDescription", "SubType", "GrateSize",
                   "TopOfGrate", "InvertElevation1", "Depth", "OutfallID",
                   "OperationalStatus", "YearInstalled", "HasGPSPoint",
                   "Location", "MaintenanceArea", "MapGrid"],
        # Layer 2 is "Inlet (active)", so the native names already are the
        # canonical ones and this map is an identity.
        "canon": {"AssetID": "AssetID", "TypeDescription": "TypeDescription",
                  "SubType": "SubType", "TopOfGrate": "TopOfGrate",
                  "InvertElevation1": "InvertElevation1", "Depth": "Depth",
                  "OperationalStatus": "OperationalStatus",
                  "YearInstalled": "YearInstalled"},
        # Legacy name: the readme's seven commands and both plot scripts point
        # at derived/storm_inlets.csv. Other cities get a suffixed file.
        "out": "storm_inlets",
    },
    "san_jose": {
        "label": "San Jose",
        "url": ("https://geo.sanjoseca.gov/server/rest/services"
                "/OPN/OPN_OpenDataService/MapServer/295"),
        "fields": ["FACILITYID", "INTID", "INLETTYPE", "RIMELEV", "DEMELEV",
                   "INVERTELEV", "SUMP", "OWNEDBY", "INSTALLYEAR", "SOURCEYEAR",
                   "PCDTYPE", "CSJUTILMAINTOWNER", "NOTES"],
        # RIMELEV, not DEMELEV, is the analogue of Livermore's surveyed
        # TopOfGrate. DEMELEV is sampled off a DEM -- it is better populated
        # (92% vs 76%) but it is modelled, and add_elevation.py already derives
        # that number itself. Conflating the two would hide which is which, so
        # DEMELEV rides along under its own name.
        "canon": {"AssetID": "FACILITYID", "TypeDescription": "INLETTYPE",
                  "TopOfGrate": "RIMELEV", "InvertElevation1": "INVERTELEV",
                  "YearInstalled": "INSTALLYEAR"},
        "out": "storm_inlets_san_jose",
        # Esri's Hub keeps a cached extract of this layer, reachable when the
        # origin is not. See --via-hub; it is a mirror, not a replica.
        "hub": "d36d012c31f14a6bbf80c131ccc3235a_295",
    },
    "pleasanton": {
        "label": "Pleasanton",
        # Pleasanton runs two servers. maps.cityofpleasantonca.gov is portal-
        # federated and its sd/ folder answers 499 Token Required; gisdata is a
        # second, open server carrying the same network. Not /arcgis/ and not
        # /server/, and the HTML Services Directory is disabled, so every URL
        # needs ?f=json.
        "url": ("https://gisdata.cityofpleasantonca.gov/arcgisdata/rest/services"
                "/ENGOSD/UtStormDrain/MapServer/1"),
        # The only city here whose layer is not already just inlets: 14,513
        # structures, of which 8,253 are inlets and 4,241 are manholes.
        "where": "TYPE='INLET'",
        "fields": ["OBJECTID", "CODE", "TYPE", "SP_FUNC", "DIAMETER", "MATERIAL",
                   "RIM_ELEV", "INV_OUT", "INV_IN", "INV_IN2", "DEPTH", "STATUS",
                   "OWNER", "STREET", "CROSS_ST", "TRACT_NO", "COMMENTS",
                   "LAND_USE"],
        # SP_FUNC, not TYPE, is the analogue of Livermore's TypeDescription:
        # after the filter TYPE is the constant "INLET", while SP_FUNC holds the
        # form -- DROP INLET, CATCH BASIN, CURB INLET. It is free text, 52
        # distinct values on 78.9% of rows, and needs normalising before it can
        # drive a legend.
        #
        # INV_OUT, not INV_IN: an inlet is a network head, so INV_IN is
        # populated on 8.3% of rows against INV_OUT's 87.4%.
        #
        # No YearInstalled and no SubType analogue; both come out blank.
        "canon": {"AssetID": "CODE", "TypeDescription": "SP_FUNC",
                  "TopOfGrate": "RIM_ELEV", "InvertElevation1": "INV_OUT",
                  "Depth": "DEPTH", "OperationalStatus": "STATUS"},
        "out": "storm_inlets_pleasanton",
    },
}

# geo.sanjoseca.gov throttles bursts by dropping the TLS handshake rather than
# returning 429, which surfaces as a reset or handshake timeout. Retry with
# backoff instead of failing the whole fetch.
RETRIES = 4
BACKOFF = 5

# A few city servers present certificates this client cannot chain. Verification
# is dropped only after a verified attempt fails, and the host is named when it
# happens, so an unexpected entry here is visible rather than silent.
_UNVERIFIED = ssl.create_default_context()
_UNVERIFIED.check_hostname = False
_UNVERIFIED.verify_mode = ssl.CERT_NONE
_insecure_hosts = set()


def get(url, timeout=120):
    """GET JSON, retrying transient network failures and TLS-chain refusals."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    host = urllib.parse.urlparse(url).netloc
    last = None
    for attempt in range(1, RETRIES + 1):
        ctx = _UNVERIFIED if host in _insecure_hosts else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.URLError as e:
            last = e
            if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
                if host not in _insecure_hosts:
                    print(f"  ! {host}: TLS verification failed, retrying unverified")
                    _insecure_hosts.add(host)
                continue
        except (TimeoutError, ConnectionError, json.JSONDecodeError) as e:
            last = e
        if attempt < RETRIES:
            wait = BACKOFF * attempt
            print(f"  ! {host}: {type(last).__name__}, "
                  f"retry {attempt}/{RETRIES - 1} in {wait}s")
            time.sleep(wait)
    raise SystemExit(f"giving up on {host}: {last}")


def describe(url):
    """Layer metadata. The OID field is read rather than assumed -- paging orders
    by it, and it is not always called OBJECTID."""
    d = get(url + "?f=json")
    if d.get("error"):
        raise SystemExit(f"server error: {d['error']}")
    adv = d.get("advancedQueryCapabilities") or {}
    return {
        "name": d.get("name"),
        "oid": d.get("objectIdField") or "OBJECTID",
        "page": min(d.get("maxRecordCount") or 1000, 2000),
        "paging": bool(adv.get("supportsPagination")),
        "fields": [f["name"] for f in (d.get("fields") or [])
                   if f.get("type") != "esriFieldTypeGeometry"],
        "domains": coded_domains(d.get("fields") or []),
    }


def coded_domains(fields):
    """{field: {code: label}} for every coded-value domain on the layer.

    Rows store the code, not the label: San Jose's INLETTYPE is "RH", and only
    the domain says that means Curb Inlet Right Hand. Livermore stores labels
    whose domain happens to map each to itself, so decoding is a no-op there.
    """
    out = {}
    for f in fields:
        dom = f.get("domain") or {}
        if dom.get("type") == "codedValue":
            out[f["name"]] = {str(c["code"]): c["name"] for c in dom.get("codedValues", [])}
    return out


def fetch_all(url, meta, out_fields, where="1=1"):
    """Page through every feature matching `where`, in WGS84.

    Prefers resultOffset. Servers that ignore it silently return page 1 forever,
    so the OID high-water mark is the fallback and also the loop guard.

    `where` is the city's own predicate, for publishers whose "inlet" layer is
    really a mixed structures layer -- Pleasanton files inlets, manholes and
    outfalls in one. It has to be AND-ed into both branches, not just the first:
    the OID fallback rewrites the clause each page, and dropping the predicate
    there would quietly widen the result set on exactly the servers least able
    to page.
    """
    fields = "*" if out_fields == "*" else ",".join(sorted(set(out_fields) | {meta["oid"]}))
    feats, offset, last_oid = [], 0, None
    while True:
        params = {"outFields": fields, "outSR": "4326", "f": "json",
                  "resultRecordCount": str(meta["page"]),
                  "orderByFields": meta["oid"]}
        if meta["paging"]:
            params["where"] = where
            params["resultOffset"] = str(offset)
        else:
            params["where"] = (where if last_oid is None
                               else f'({where}) AND {meta["oid"]} > {last_oid}')
        d = get(url + "/query?" + urllib.parse.urlencode(params))
        if d.get("error"):
            raise SystemExit(f"server error: {d['error']}")
        batch = d.get("features", [])
        if not batch:
            break
        oids = [f["attributes"].get(meta["oid"]) for f in batch]
        if last_oid is not None and oids and oids[-1] == last_oid:
            break                  # server ignored the window; stop rather than spin
        feats.extend(batch)
        last_oid = oids[-1] if oids else last_oid
        print(f"  fetched {len(feats)}")
        if not d.get("exceededTransferLimit") and len(batch) < meta["page"]:
            break
        offset += len(batch)
    return feats


HUB_DATASET = "https://hub.arcgis.com/api/v3/datasets/{ds}"
HUB_DOWNLOAD = ("https://hub.arcgis.com/api/v3/datasets/{ds}"
                "/downloads/data?format=geojson&spatialRefId=4326")


def fetch_hub(dataset):
    """Pull the layer from Esri's Hub cache instead of the origin server.

    For when the publisher's own host refuses us -- geo.sanjoseca.gov answers a
    burst of queries by dropping TLS handshakes at the firewall, and stays that
    way for a good while. Hub serves a periodically regenerated extract from
    Esri's infrastructure, so it survives that.

    It is a MIRROR, NOT A REPLICA. Expect a stale record count, and a schema
    that can differ from the live layer in both directions. Reported, not
    silently reconciled, so a fetch never looks authoritative when it is not.
    """
    print(f"  via Hub cache: dataset {dataset}")
    # Hub's dataset record carries the field definitions, domains included, which
    # the GeoJSON download itself does not.
    meta = get(HUB_DATASET.format(ds=dataset), timeout=120)
    domains = coded_domains(((meta.get("data") or {}).get("attributes") or {}).get("fields") or [])

    req = urllib.request.Request(HUB_DOWNLOAD.format(ds=dataset),
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=300) as r:
        gj = json.loads(r.read().decode("utf-8", "replace"))
    feats = []
    for ft in gj.get("features", []):
        g = ft.get("geometry") or {}
        c = g.get("coordinates") or [None, None]
        feats.append({"attributes": ft.get("properties") or {},
                      "geometry": {"x": c[0], "y": c[1]}})
    print(f"  fetched {len(feats)} (cached extract)")
    return feats, domains


def build_rows(feats, city, cfg, natives, domains):
    """Canonical columns first, then whatever else the city publishes.

    Coded values are decoded only on the canonical columns -- those exist to be
    comparable across cities, so "Curb Inlet" beats "RH". Native columns are
    passed through exactly as served.
    """
    canon = cfg["canon"]
    consumed = set(canon.values())
    extras = [f for f in natives if f not in consumed]
    cols = ["lon", "lat", "source"] + CANONICAL + extras
    rows = []
    for ft in feats:
        g = ft.get("geometry") or {}
        a = ft.get("attributes") or {}
        row = {"lon": g.get("x"), "lat": g.get("y"), "source": city}
        for name in CANONICAL:
            native = canon.get(name)
            v = a.get(native) if native else None
            codes = domains.get(native) if native else None
            if codes and v is not None:
                v = codes.get(str(v), v)
            row[name] = v
        for f in extras:
            row[f] = a.get(f)
        rows.append(row)
    return rows, cols


def report(rows, cfg):
    print(f"\ntotal: {len(rows)}")
    missing = sum(1 for r in rows if r["lat"] is None or r["lon"] is None)
    print(f"missing geometry: {missing}")

    for col in ("TypeDescription", "OperationalStatus"):
        if not cfg["canon"].get(col):
            continue
        print(f"\n{col} (from {cfg['canon'][col]}):")
        for k, n in Counter(r[col] for r in rows).most_common(15):
            print(f"  {n:>6}  {k}")

    for col in ("TopOfGrate", "InvertElevation1"):
        native = cfg["canon"].get(col)
        if not native:
            print(f"\n{col}: not published by this city")
            continue
        vals = [r[col] for r in rows if isinstance(r[col], (int, float))]
        print(f"\n{col} (from {native}): {len(vals)} / {len(rows)} populated", end="")
        print(f"   range {min(vals):.1f}..{max(vals):.1f}" if vals else "")

    pts = [(r["lon"], r["lat"]) for r in rows
           if r["lat"] is not None and r["lon"] is not None]
    if pts:
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        print(f"\nbbox: lon {min(lons):.5f}..{max(lons):.5f}  "
              f"lat {min(lats):.5f}..{max(lats):.5f}")


def fetch_city(key, cfg, args):
    """One city, from the origin or the Hub cache, to canonical rows."""
    print(f'{cfg["label"]}: {cfg["url"]}')
    if cfg.get("where"):
        print(f'  filter: {cfg["where"]}')

    if args.via_hub:
        if not cfg.get("hub"):
            raise SystemExit(f'no Hub dataset recorded for {key!r}')
        if cfg.get("where"):
            # The Hub download takes no predicate, so the cache would hand back
            # the whole mixed layer and every manhole in it would be written out
            # as an inlet. Refuse rather than emit a quietly wrong file.
            raise SystemExit(
                f'{key!r} needs the filter {cfg["where"]!r}, which the Hub '
                f'download cannot apply -- fetch from the origin instead')
        feats, domains = fetch_hub(cfg["hub"])
        # The origin is unreachable by definition here, so the schema comes from
        # the payload. The cache genuinely carries a different field set, so a
        # missing column is reported rather than treated as a broken service.
        available = sorted({k for ft in feats for k in ft["attributes"]})
        natives = available if args.all_fields else [f for f in cfg["fields"]
                                                     if f in available]
        missing = [f for f in cfg["fields"] if f not in available]
        if missing:
            print(f"  ! not in the cached extract, omitted: {', '.join(missing)}")
    else:
        meta = describe(cfg["url"])
        print(f'layer "{meta["name"]}"  oid={meta["oid"]}  page={meta["page"]}  '
              f'pagination={meta["paging"]}')

        natives = meta["fields"] if args.all_fields else cfg["fields"]
        unknown = [f for f in natives if f not in meta["fields"]]
        if unknown:
            # A renamed field would otherwise surface as a silently blank column.
            raise SystemExit(f"fields not on this layer (service changed?): {unknown}")

        domains = meta["domains"]
        feats = fetch_all(cfg["url"], meta, "*" if args.all_fields else natives,
                          cfg.get("where", "1=1"))
    return build_rows(feats, key, cfg, natives, domains)


def write_out(rows, cols, csv_path, want_geojson):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, restval="")
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {csv_path}  ({len(rows):,} rows, {len(cols)} cols)")

    if want_geojson:
        gj_path = csv_path.with_suffix(".geojson")
        gj = {"type": "FeatureCollection", "features": [
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
             "properties": {k: r.get(k) for k in cols if k not in ("lon", "lat")}}
            for r in rows if r["lat"] is not None and r["lon"] is not None]}
        with open(gj_path, "w", encoding="utf-8") as f:
            json.dump(gj, f)
        print(f"wrote {gj_path}")


def merge(per_city):
    """Concatenate every city into one table over the union of their columns.

    The canonical block is shared by construction, so only the extras differ.
    They are kept in registry order rather than sorted, so a city's own columns
    stay adjacent and the file reads as blocks rather than an interleave. A city
    that does not publish a column gets "" there, not a dropped row -- `source`
    is what tells the two apart downstream.
    """
    cols, seen = [], set()
    for _, (_, city_cols) in per_city.items():
        for c in city_cols:
            if c not in seen:
                seen.add(c)
                cols.append(c)
    rows = [r for _, (city_rows, _) in per_city.items() for r in city_rows]
    return rows, cols


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("city", nargs="?", default="livermore",
                    help="city key (default livermore); --list to see them")
    ap.add_argument("--require", metavar="CITY", action="append", default=[],
                    help="with --all: only these cities failing is fatal. A "
                         "pipeline building one city should not be stopped by "
                         "another publisher's server dropping a connection, but "
                         "it must still fail if the city it needs is missing. "
                         "Repeatable.")
    ap.add_argument("--all", action="store_true",
                    help="fetch every known city into one file "
                         "(default derived/storm_inlets_all.csv); rows carry a "
                         "`source` column, so filter downstream by AOI or city")
    ap.add_argument("--list", action="store_true", help="list known cities and exit")
    ap.add_argument("--out", help="output CSV path (default derived/<stem>.csv)")
    ap.add_argument("--all-fields", action="store_true",
                    help="request every field the layer publishes, not the curated set")
    ap.add_argument("--no-geojson", action="store_true",
                    help="skip the .geojson companion")
    ap.add_argument("--via-hub", action="store_true",
                    help="read Esri's cached extract instead of the origin server, "
                         "for when the publisher's host is refusing connections. "
                         "Cached: record count and schema may lag the live layer")
    args = ap.parse_args()

    if args.list:
        for key, c in CITIES.items():
            mapped = ", ".join(f"{k}<-{v}" for k, v in c["canon"].items())
            print(f'{key:<12} {c["label"]:<12} {c["url"]}')
            if c.get("where"):
                print(f'             filter {c["where"]}')
            print(f'             {mapped}')
        return

    OUT_DIR.mkdir(exist_ok=True)

    if not args.all:
        if args.city not in CITIES:
            raise SystemExit(f"unknown city {args.city!r}; known: {', '.join(CITIES)}")
        cfg = CITIES[args.city]
        rows, cols = fetch_city(args.city, cfg, args)
        report(rows, cfg)
        write_out(rows, cols, Path(args.out) if args.out
                  else OUT_DIR / f'{cfg["out"]}.csv', not args.no_geojson)
        return

    # --- every city, one file -------------------------------------------
    per_city, failed = {}, []
    for i, (key, cfg) in enumerate(CITIES.items(), 1):
        print(f'\n=== [{i}/{len(CITIES)}] {key} ===')
        try:
            per_city[key] = fetch_city(key, cfg, args)
        except SystemExit as e:
            # One publisher being down should not cost the other cities their
            # fetch. The failure is named here, named again at the end, and the
            # exit status is non-zero -- so a pipeline still stops, but by hand
            # you keep what you got.
            print(f'  ! {key} FAILED: {e}')
            failed.append(key)
            continue
        report(per_city[key][0], cfg)

    if not per_city:
        raise SystemExit("\nevery city failed; nothing written")

    rows, cols = merge(per_city)
    print('\n=== combined ===')
    for key, (city_rows, _) in per_city.items():
        print(f'  {key:<12} {len(city_rows):>7,}')
    print(f'  {"total":<12} {len(rows):>7,}  over {len(cols)} columns')
    write_out(rows, cols, Path(args.out) if args.out
              else OUT_DIR / "storm_inlets_all.csv", not args.no_geojson)

    if failed:
        # Written, but incomplete -- and the file cannot say so itself, since a
        # missing city is indistinguishable from a city with no inlets. So the
        # exit status has to carry it.
        missing = [c for c in args.require if c in failed] if args.require else failed
        note = (f'\nINCOMPLETE: {len(failed)} city(s) failed and are absent from '
                f'the file: {", ".join(failed)}')
        if not missing:
            # Something failed, but nothing the caller said it needed. A run
            # building San Jose should not die because Livermore's server
            # dropped a connection.
            print(note + f'\n  ...none of them required '
                         f'({", ".join(args.require)}), continuing.')
            return
        raise SystemExit(note + f'\n  REQUIRED and missing: {", ".join(missing)}')


if __name__ == "__main__":
    main()
