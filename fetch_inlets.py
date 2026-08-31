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
    "fremont": {
        "label": "Fremont",
        # ArcGIS Online hosted, public, no token. The item (351af28e, owner
        # fenvserv) carries no description and NODE_TYPE has no coded-value
        # domain, so the codes look undocumented -- but the layer ships its own
        # dictionary in GISO_LABEL, one readable name per code.
        "url": ("https://services2.arcgis.com/AVso4yDITKsybTJg/arcgis/rest"
                "/services/COF_Storm_Structs/FeatureServer/0"),
        # 30,524 structures of 27 kinds; these five are the inlets, 15,192 of
        # them -- CI Curb inlet 10,152, DI Drainage inlet 2,587, CB Catch basin
        # 2,159, INL Inlet 148, FI Field inlet 145. Everything else is network
        # or hydrography: MH Manhole 9,339, UNK Unknown 1,541, END End of main,
        # AD Area drain, ST Stream, OUTL Outlet, CH Channel, J Junction, CV
        # Culvert, CR Creek, JB Junction box, OUTF Outfall, DH Ditch, HW
        # Headwall, LK Lake, LG Lagoon, RP RipRap. Two look like inlets and are
        # not: GB is "Grade break" and INF is "Inflow", both pipe-network nodes.
        "where": "NODE_TYPE IN ('CI','DI','CB','INL','FI')",
        "fields": ["OBJECTID", "STORMN_KEY", "NODE_TYPE", "GISO_LABEL",
                   "RIM_ELEV", "TC_ELEV", "ELEV_LOPIP", "ELEV_BTM", "BOX_DEPTH",
                   "OWNER", "F_CITY", "COMMENTS", "SRC", "SRC_DATE"],
        # ELEVATIONS ARE EFFECTIVELY ABSENT, and the survey CSV hides it: it
        # reports RIM_ELEV/TC_ELEV/ELEV_LOPIP as 30,524 populated because it
        # counts non-null, and the columns are ZERO-FILLED. Measured over the
        # 15,192 inlets: RIM_ELEV is non-zero on 31, TC_ELEV on 565, ELEV_LOPIP
        # on none at all. They are mapped anyway rather than dropped -- the
        # plotters already gate on GRATE_MIN_FT/GRATE_MAX_FT (300-900 ft), so a
        # zero falls outside and becomes NaN, which draws no marker. If Fremont
        # ever populates them the mapping is already right.
        #
        # So this city is location-only: it snaps to profiles and drives sag and
        # unserved-sag analysis off the DEM exactly like the others, but its
        # pages carry no grate or invert marks.
        #
        # GISO_LABEL, not NODE_TYPE, for TypeDescription: the point of that
        # column is to be comparable across cities, and "Catch basin" beats
        # "CB". The raw code goes to SubType. No OperationalStatus or
        # YearInstalled analogue; both come out blank.
        "canon": {"AssetID": "STORMN_KEY", "TypeDescription": "GISO_LABEL",
                  "SubType": "NODE_TYPE", "TopOfGrate": "RIM_ELEV",
                  "InvertElevation1": "ELEV_LOPIP", "Depth": "BOX_DEPTH"},
        "out": "storm_inlets_fremont",
    },
    "hayward": {
        "label": "Hayward",
        # ArcGIS Online hosted, public, no token. The only city here whose layer
        # is ALREADY just inlets -- 4,567 of them, no manholes or network nodes
        # mixed in -- so it needs no `where` at all.
        "url": ("https://services1.arcgis.com/WTXhkvI9mSg0lzhr/arcgis/rest"
                "/services/COH_Storm_Drain_Inlets/FeatureServer/0"),
        "fields": ["FID", "ID", "InletType", "Grate_Cond", "No_Dumpi_1",
                   "Comments"],
        # Seven fields in the whole layer, and NO elevation columns exist at
        # all -- not zero-filled like Fremont's, simply absent. So TopOfGrate,
        # InvertElevation1 and Depth stay unmapped and come out blank, which is
        # the honest rendering: nothing here could be mistaken for a measurement.
        # Location-only, like Fremont.
        #
        # Comments is misnamed and is really the install year: all 4,567 values
        # are years, 9 distinct (1955 x2,668, 1960 x554, 1972 x332, 2000 x301,
        # 1957, 1956, 1958, 2015, 1975), none of them anything else. It maps to
        # YearInstalled, which no other city outside Livermore fills.
        #
        # Grate_Cond is deliberately NOT mapped to OperationalStatus. It is a
        # condition grade -- F 1,898 / G 1,491 / P 1,175 Fair, Good, Poor, plus
        # two strays -- and OperationalStatus is about whether an asset is in
        # service. Livermore fills the latter; folding a condition into it would
        # quietly corrupt the one thing the canonical columns exist for, which
        # is comparing like with like across cities. It passes through as a
        # native column instead, preserved and correctly named.
        "canon": {"AssetID": "ID", "TypeDescription": "InletType",
                  "YearInstalled": "Comments"},
        "out": "storm_inlets_hayward",
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

    Canonical strings are also whitespace-stripped, and only canonical ones.
    Hayward publishes InletType as both "COMBO" (4,070 rows) and "COMBO " with a
    trailing space (28), plus "GRATE"/"GRATE " -- 35 rows that would read as
    separate categories in any legend or groupby. The server's own groupBy hides
    it, reporting the clean values, so it only shows up once the rows are in
    hand. Extras keep the "exactly as served" contract above; a padded native
    value is the publisher's business, but a padded CANONICAL one defeats the
    cross-city comparison those columns exist for. A value that is only
    whitespace becomes None rather than "".
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
            if isinstance(v, str):
                v = v.strip() or None
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


def city_columns(cfg, all_fields=False):
    """The columns build_rows() produces for a city, without fetching it.

    Same construction as build_rows: the canonical block, then whatever native
    fields the registry keeps that no canonical name already consumed. Lets a
    reused city be reassembled from an existing file in exactly the shape a
    fresh fetch would have written.
    """
    if all_fields:
        return None                      # unknown without asking the server
    consumed = set(cfg["canon"].values())
    extras = [f for f in cfg["fields"] if f not in consumed]
    return ["lon", "lat", "source"] + CANONICAL + extras


def read_existing(path):
    """{city: [row, ...]} from a merged file written by an earlier run."""
    if not path.exists():
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.setdefault(row.get("source"), []).append(row)
    return out


def reuse_city(existing, key, cfg, all_fields=False):
    """That city's rows from the existing file, in its own column order.

    Returns None when the file has nothing for it, or when --all-fields makes
    the column set unknowable without asking the server -- either way the
    caller falls through to fetching.
    """
    rows = existing.get(key)
    cols = city_columns(cfg, all_fields)
    if not rows or cols is None:
        return None
    have = set(rows[0])
    if not set(cols) <= have:
        # The registry gained a field since the file was written, so the file
        # cannot answer for this city any more.
        return None
    return [{c: r.get(c, "") for c in cols} for r in rows], cols


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
    ap.add_argument("--refresh", metavar="CITY", action="append", default=[],
                    help="refetch this city even though the output file already "
                         "has it. Repeatable; --refresh-all does every city.")
    ap.add_argument("--refresh-all", dest="refresh_all", action="store_true",
                    help="refetch every city, ignoring what the file holds.")
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
    # A city already in the merged file is reused rather than refetched. The
    # publishers are third-party servers of wildly differing robustness --
    # Livermore's municipal box resets connections under load, where the two
    # Esri-hosted ones never flinch -- so refetching two cities to rebuild one
    # is slow and a failure mode for no gain. --refresh CITY overrides, and
    # --refresh-all ignores the file entirely.
    out_path = Path(args.out) if args.out else OUT_DIR / "storm_inlets_all.csv"
    existing = {} if args.refresh_all else read_existing(out_path)
    per_city, failed, reused = {}, [], []
    for i, (key, cfg) in enumerate(CITIES.items(), 1):
        print(f'\n=== [{i}/{len(CITIES)}] {key} ===')
        if key not in args.refresh:
            keep = reuse_city(existing, key, cfg, args.all_fields)
            if keep:
                print(f'  {len(keep[0]):,} row(s) already in {out_path.name}, '
                      f'not refetching (--refresh {key} to force)')
                per_city[key] = keep
                reused.append(key)
                continue
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
    if reused:
        print(f'  reused, not refetched: {", ".join(reused)}')
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
