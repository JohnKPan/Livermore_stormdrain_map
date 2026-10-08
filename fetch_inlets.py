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
import datetime
import json
import re
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

# The canonical columns holding an elevation rather than a length or a label.
# They are the only ones a datum offset may touch: Foster City's rims and
# inverts are 100 ft off, but its depths are depths and are already right.
ELEVATION_COLS = ("TopOfGrate", "InvertElevation1")

# The datum the corpus is on. Livermore, Pleasanton, San Jose and Fremont all
# publish NGVD29, so plot_street_drains.py converts the whole file with one
# constant (DATUM_SHIFT_M = 0.794 m) at load time.
NGVD29_TO_NAVD88_FT = 2.605          # 0.794 m, plot_street_drains.DATUM_SHIFT_M

# Every city that publishes an elevation names its datum, and the name carries
# the offset. Values are feet ADDED to a published elevation to put it on the
# corpus datum -- NOT feet to NAVD88, which is a different number, because the
# plotter's global shift is still waiting downstream. NGVD29 is therefore 0.0:
# a city already on the corpus datum needs nothing here.
#
# Naming the datum rather than writing a bare offset is the point. An offset of
# zero and a missing key look identical in a registry and mean opposite things
# -- "measured, and it matches" against "nobody looked" -- and a city silently
# mis-shifted by 2.6 ft is invisible to the plotters' 20 ft grate gate. So
# `datum` is REQUIRED on any city mapping an elevation; see check_registry().
DATUMS = {
    "NGVD29":     0.0,
    "NAVD88":     -NGVD29_TO_NAVD88_FT,
    "NGVD29+100": -100.0,            # Foster City's local convention
}


def _to_float(v):
    """A number the publisher stored as text.

    San Ramon writes rim elevations as "486.44" and empty ones as " ", and
    Richmond files depths as "18". Anything unparseable becomes None rather
    than a string in a numeric column.
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    if not isinstance(v, str):
        return None
    try:
        return float(v.strip())
    except ValueError:
        return None


_LEAD_NUM = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


def _lead_float(v):
    """The number at the front of a packed string.

    Emeryville files an invert as `39.91 12" OUT` -- elevation, pipe size and
    direction in one text column. Four rows start with a minus sign, which is
    why the pattern allows one.
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    if not isinstance(v, str):
        return None
    m = _LEAD_NUM.match(v)
    return float(m.group(1)) if m else None


def _first_line(v):
    r"""The first line of a multi-line map label.

    Foster City publishes no type column; its only description of what a node
    is sits at the head of the label it draws on the map, "Curb Inlet\nDI No:
    4537". The break arrives from the service as the two characters backslash-n
    rather than a newline, so both spellings are cut.
    """
    if not isinstance(v, str):
        return v
    head = v.replace("\\n", "\n").split("\n")[0].strip()
    return head or None


_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def _year(v):
    """Calendar year out of an Esri date field (epoch milliseconds).

    Fairfield's InstallDate spans 1977..2007 on 95% of rows, which is the only
    install date anyone here publishes better than Livermore. Built by adding a
    timedelta rather than fromtimestamp(), which raises on pre-1970 dates.
    """
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        return (_EPOCH + datetime.timedelta(milliseconds=v)).year
    except (ValueError, OverflowError, OSError):
        return None


def _inches_to_feet(v):
    """Richmond's DEPTH/LENGTH/WIDTH are inches. Livermore's Depth is feet."""
    f = _to_float(v)
    return None if f is None else round(f / 12.0, 3)


def _negate(v):
    """Cupertino stores depth as a signed offset below the rim, so a 6 ft basin
    reads -6.0. Negated rather than abs()'d: the nine rows that were already
    positive come out negative, which shows the publisher's sign error instead
    of laundering it into a plausible number."""
    f = _to_float(v)
    return None if f is None else -f


CONVERTERS = {"float": _to_float, "lead_float": _lead_float,
              "first_line": _first_line, "year": _year,
              "inches_to_feet": _inches_to_feet, "negate": _negate}

# --------------------------------------------------------------------------
# City registry.
#
#   url     a layer endpoint (.../FeatureServer/N or .../MapServer/N)
#   fields  native fields to request, in output order. Not "*": Livermore's
#           layer carries 54 columns and the original script deliberately took
#           15 of them. --all-fields overrides.
#   canon   canonical name -> native field. Anything unmapped comes out blank.
#   where   optional predicate, for a publisher whose "inlet" layer is really a
#           mixed structures layer
#   convert optional {native field: CONVERTERS key}, for a column that has to be
#           read before it means anything -- a number stored as text, a year
#           inside a date, an elevation at the front of a packed string. Applied
#           to canonical columns only; a native column rides along as served.
#   sentinels optional numbers this city writes to mean "no reading" (-999 and
#           friends). Dropped to blank on every canonical numeric.
#   datum   the vertical datum the city publishes on, a key of DATUMS. Required
#           of every city mapping an elevation, and applied to elevations only,
#           never to depths. Each was measured against USGS 3DEP bare earth over
#           2,000 grates sampled the length of the layer's OID range -- the
#           numbers in the comments below are that measurement.
#   out     default output stem under derived/
#
# Endpoints and field names come from bay_area_stormdrain_sources.csv; re-run
# survey_bay_area_sources.py if a service moves. Population figures in the
# comments below are counted over the whole layer, non-null AND non-zero, and
# elevation columns were checked against USGS 3DEP bare earth (30 points per
# city, NAVD88 ft) -- a name proves nothing here, as Los Gatos publishes a
# column called TopOfGrate that is zero on 2,416 of its 2,422 inlets.
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
        # Measured NGVD29: median -2.428 ft from 3DEP bare earth over 2,000
        # of its 4,486 grates, 78.8% of them within a foot of that datum.
        "datum": "NGVD29",
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
        # Measured NGVD29: median -2.342 ft over 2,000 of its 27,284 grates,
        # 69.3% within a foot. The widest spread of the twelve (IQR 1.20), which
        # is the bridge-deck and right-tail population the 20 ft gate keeps.
        "datum": "NGVD29",
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
        # Measured NGVD29: median -2.744 ft over 2,000 of its 7,292 grates,
        # 73.3% within a foot.
        "datum": "NGVD29",
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
        # load_inlets() drops zeros outright and the plotters then gate a grate
        # against the DEM beneath it, so a zero draws no marker either way. If
        # Fremont ever populates them the mapping is already right.
        #
        # The 31 RIM_ELEV and 565 TC_ELEV values that ARE non-zero turn out to be
        # real: against the lidar they sit -0.05 and +1.06 ft from bare earth
        # (medians, datum-shifted), the second being about right for a top of
        # curb. The old absolute 300-900 ft plotter gate discarded all of them,
        # Fremont being a bayside city; the DEM-relative gate keeps them.
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
        # Measured NGVD29 on the only 31 grates it has: median -2.064 ft, 21 of
        # 31 within a foot. Thin, but it agrees with its neighbours and the
        # column is nearly empty anyway.
        "datum": "NGVD29",
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
    "richmond": {
        "label": "Richmond",
        # ArcGIS Online hosted, public, no token; layer 162 of a wide service.
        "url": ("https://services6.arcgis.com/il6vO1TutlF580Ku/arcgis/rest"
                "/services/Storm_Collection_Device/FeatureServer/162"),
        # 4,777 collection devices and every one of them is an inlet, so no
        # filter: SUBTYPE is 2 Curb Inlet 1,828, 0 Catch Basin 1,522, 1 Drop
        # Inlet 1,122, 5 Partial Pipe Culvert 302, 6 Other Culvert 2, 4
        # Potential 1. Not a manhole in the layer.
        "fields": ["OBJECTID", "ASSET_ID", "FACILITYID", "SUBTYPE",
                   "LIFE_CYCLE_STATUS", "RIM_ELEV", "DEM_ELEV_FT", "DEPTH",
                   "LENGTH", "WIDTH", "MATERIAL", "CONDITION", "OWNERSHIP",
                   "INSTALL_YEAR", "CONFIDENCE", "DRAIN_BASIN_NM", "NOTES"],
        # THE BEST GRATE DATA OF THE 34 SOURCES SURVEYED. RIM_ELEV is populated
        # on 4,654 of 4,777 (97.4%) and sits +0.317 ft from 3DEP bare earth
        # (median over 2,000 grates, IQR 0.28, MAD 0.13) -- NAVD88 already, and
        # on 94.5% of points individually, the cleanest agreement of the twelve.
        # Naming that datum is what makes the entry undo the corpus shift rather
        # than leave the rims 2.6 ft high.
        #
        # DEM_ELEV_FT is on every row and is sampled off a DEM: San Jose's
        # DEMELEV again, and it rides along natively for the same reason.
        #
        # DEPTH, LENGTH and WIDTH are numbers stored as text ("18", "142") and
        # they are inches, not feet -- the median device is 36 in deep, and a
        # foot reading would bury these basins 50 ft down. Only Depth is
        # canonical, so only it is converted; the other two stay as served.
        #
        # INSTALL_YEAR exists and is filled on 4 rows, so YearInstalled stays
        # blank. CONDITION is a Good/Fair/Bad grade and is NOT
        # OperationalStatus, per Hayward; LIFE_CYCLE_STATUS is the real one.
        "convert": {"DEPTH": "inches_to_feet"},
        "datum": "NAVD88",
        "canon": {"AssetID": "ASSET_ID", "TypeDescription": "SUBTYPE",
                  "TopOfGrate": "RIM_ELEV", "Depth": "DEPTH",
                  "OperationalStatus": "LIFE_CYCLE_STATUS"},
        "out": "storm_inlets_richmond",
    },
    "cupertino": {
        "label": "Cupertino",
        # The city's own server, and the one endpoint here under /cupgis/.
        "url": ("https://gis.cupertino.org/cupgis/rest/services/Public"
                "/AmazonData/MapServer/45"),
        # 6,296 structures of 16 kinds; these four are the inlets, 3,493 of them
        # -- Catch Basin 2,170, Area Drain 720, Drop Inlet 579, BubbleUp 24.
        # The filter drops Manhole 2,192, Clean Out 266, Outfall 185, Unknown
        # 77 and a tail of treatment devices. Inlet Culvert (19) and Thru Curb
        # Drain (3) are left out: both are pipe openings, not street inlets.
        "where": ("StructureType IN ('Catch Basin','Drop Inlet','Area Drain',"
                  "'BubbleUp')"),
        "fields": ["OBJECTID", "AssetID", "LegacyID", "StructureType", "Status",
                   "RimElev", "Depth", "CoverType", "OwnedBy", "MaintainedBy",
                   "Location", "AsbuiltDate", "DataSource", "IsCityStandard",
                   "HasStencil", "hasInspection"],
        # RimElev is on 2,487 of the 3,493 inlets (71.2%) and is NAVD88, not
        # NGVD29: median -0.185 ft against 3DEP over 2,000 grates, IQR 0.40, and
        # 1,727 of those points individually within a foot of NAVD88 against 170
        # of NGVD29.
        #
        # A 30-point check said NGVD29 and was wrong. Those 30 were the first
        # rows the server returned, which is OID order, which is digitising
        # order -- and they landed inside the 8.5% minority cluster that really
        # does sit 2.6 ft low. A city is not a subdivision; the sample has to
        # walk the whole OID range or it measures a neighbourhood.
        #
        # Depth is a SIGNED OFFSET BELOW THE RIM: 2,010 rows negative (3-9 ft
        # typical), 9 positive, 880 zero. Negated into the canonical column,
        # which is a positive depth in Livermore's usage. See _negate() for why
        # those 9 rows are left looking wrong.
        #
        # InstallDate is filled on 18 rows. AsbuiltDate is filled on 2,830
        # (81%) and is tempting, but an as-built is when the drawing was signed,
        # not when the basin went in, so YearInstalled stays blank and the date
        # rides along native.
        "convert": {"Depth": "negate"},
        "datum": "NAVD88",
        "canon": {"AssetID": "AssetID", "TypeDescription": "StructureType",
                  "TopOfGrate": "RimElev", "Depth": "Depth",
                  "OperationalStatus": "Status"},
        "out": "storm_inlets_cupertino",
    },
    "fairfield": {
        "label": "Fairfield",
        # ArcGIS Online hosted view of the city's 811 layer, public, no token.
        "url": ("https://services1.arcgis.com/A14KNJpxNyBTu19J/arcgis/rest"
                "/services/StormDrain811_view/FeatureServer/2"),
        # All 5,922 rows are inlets -- SDCB 5,717, SDDI 152, SDFI 53 -- so no
        # filter. Solano County, north-east of the other eight cities.
        "fields": ["OBJECTID", "FacilityID", "InletType", "RimElev",
                   "InvertElev", "InstallDate", "OwnedBy", "Street",
                   "CrossStreet", "MapGrid", "AccessMaterial", "GrateSize",
                   "Condition", "Notes"],
        # TWO SENTINELS, not one: RimElev is non-zero on 3,726 rows, of which 27
        # are -999 and 355 are -888. Dropping both leaves 3,344 real readings
        # (56.5%), measuring -2.230 ft against 3DEP over 2,000 grates
        # (IQR 0.76, 83.1% of points within a foot of NGVD29).
        # InvertElev (325 rows, 5.5%) uses the same codes.
        #
        # InstallDate is a genuine date field, 1977-11-08 to 2007-01-10, on
        # 5,636 rows (95%) -- better install coverage than any city here except
        # Hayward, and Hayward's is a year hidden in a Comments column. Read to
        # a year, since that is what YearInstalled holds elsewhere.
        #
        # Status is empty on every row and Activeflag is 1 on every row, so
        # OperationalStatus stays blank rather than being filled with a constant.
        "convert": {"InstallDate": "year"},
        "sentinels": (-999, -888),
        "datum": "NGVD29",
        "canon": {"AssetID": "FacilityID", "TypeDescription": "InletType",
                  "TopOfGrate": "RimElev", "InvertElevation1": "InvertElev",
                  "YearInstalled": "InstallDate"},
        "out": "storm_inlets_fairfield",
    },
    "foster_city": {
        "label": "Foster City",
        # ArcGIS Online hosted, public, no token.
        "url": ("https://services.arcgis.com/yq3FgOI44hYHAFVZ/arcgis/rest"
                "/services/Foster_City_StormNode/FeatureServer/0"),
        # "StormNode" sounds mixed and is not: all 5,039 rows are inlets, 4,841
        # curb inlets and 198 street drains. No manholes, so no filter.
        "fields": ["OBJECTID", "CB_ID", "SHORT", "CURBINLET", "DEPTH", "EL_OUT",
                   "CALC_DEPTH", "DATE_", "DIA_OUT", "EL_INV1", "DIA_INV1",
                   "MAINT_RESP", "CONF_DOC", "LOCATION"],
        # A LOCAL VERTICAL DATUM, AND A COLUMN NAMED FOR THE WRONG QUANTITY.
        # DEPTH is not a depth, it is the RIM ELEVATION: it tracks 3DEP bare
        # earth at +97.591 ft (median over 2,000 grates, IQR 0.94) in a city
        # that is flat and at sea level. That is NGVD29 + 100 ft, the usual
        # municipal dodge for a place where real elevations are 2-8 ft, and the
        # round hundred is the right reading of it -- the residual against
        # NGVD29+100 is +0.196 ft, about where a grate sits relative to bare
        # earth, and 1,631 of 2,000 points land within a foot of it. Not one
        # point in 2,000 votes for either of the other two datums.
        #
        # The rest follows from that: EL_OUT is the outgoing invert on the same
        # datum, and CALC_DEPTH is the actual depth -- DEPTH - EL_OUT ==
        # CALC_DEPTH on all 2,934 rows carrying the three, so it is derived and
        # it is a length, which is why no datum offset is allowed near it.
        #
        # There is no type column at all. SHORT is the two-line map label and
        # its first line is the only description of what a node is.
        #
        # DATE_ is a plain year on 3,713 rows (73.7%), one of which reads 199.
        #
        # Nine of the 3,069 rims land outside -5..20 ft once shifted: three
        # where the publisher put a DEPTH in the depth-named column after all
        # (10.2, 14.5, 1031.0), and five where rim and invert are the same
        # number. Left alone -- the plotters' DEM gate rejects all nine, and
        # patching them here would hide a data-entry pattern worth seeing.
        "convert": {"SHORT": "first_line"},
        "datum": "NGVD29+100",
        "canon": {"AssetID": "CB_ID", "TypeDescription": "SHORT",
                  "TopOfGrate": "DEPTH", "InvertElevation1": "EL_OUT",
                  "Depth": "CALC_DEPTH", "YearInstalled": "DATE_"},
        "out": "storm_inlets_foster_city",
    },
    "suisun_city": {
        "label": "Suisun City",
        # ArcGIS Online hosted, public, no token. Solano County, next to
        # Fairfield and sharing a plan set with it in places.
        "url": ("https://services6.arcgis.com/c3LFtvdbbWzVLXLS/arcgis/rest"
                "/services/Suisun_City_Storm_Drains/FeatureServer/1"),
        # 2,040 catch basins, already just inlets.
        "fields": ["OBJECTID", "Text_", "Elevation", "TCElevation",
                   "RimElevation", "SourceDoc", "SrcSubdivision", "SrcProject",
                   "SrcSheet", "Comments", "DateLastRev"],
        # THREE ELEVATION COLUMNS, and the one named RimElevation is the worst
        # of them: 161 rows (7.9%). Elevation carries 1,741 (85.3%) at -2.22 ft
        # from 3DEP, TCElevation 1,307 (64.1%) at -2.44 (IQR 0.38). Both are
        # NGVD29 and both land within half a foot of grade once shifted;
        # Elevation wins on coverage and is the rim, so TCElevation and
        # RimElevation ride along native and can be compared against it.
        #
        # Measured NGVD29: median -2.301 ft over all 1,741 of its grates,
        # IQR 1.13, 69.6% of points within a foot.
        #
        # NO ASSET ID EXISTS. The layer has OBJECTID and nothing else that
        # identifies a basin, so AssetID stays blank -- the only city here with
        # no identifier at all.
        #
        # Text_ is a map label pressed into service as a type: CB 1,812, DI 106,
        # CB_4 41, then FI, JB and two rows whose "type" is a number (one of
        # them, "29.42", is that row's own Elevation). It needs normalising
        # before it can drive a legend.
        "datum": "NGVD29",
        "canon": {"TypeDescription": "Text_", "TopOfGrate": "Elevation"},
        "out": "storm_inlets_suisun_city",
    },
    "belmont": {
        "label": "Belmont",
        # ArcGIS Online hosted, public, no token. An adopt-a-drain publication
        # rather than an asset layer, which is why the schema is so thin -- and
        # why it is surprising that it carries a usable elevation.
        "url": ("https://services2.arcgis.com/yj9NEYUuOce5iBjA/arcgis/rest"
                "/services/Adopt_A_Drain/FeatureServer/0"),
        "fields": ["OBJECTID", "STORMCB_ID", "DXF_LAYER", "Z_alt", "NoGrate",
                   "NoDump_Mar", "BikeSafe", "SITUS_STRE", "Location", "Owner",
                   "Adopted"],
        # Z_alt is on 837 of 1,008 (83.0%) and reads -3.085 ft against 3DEP
        # over all 837 (IQR 0.74) -- NGVD29, but the loosest fit of the twelve:
        # the residual is -0.480 ft and 197 points sit more than a foot from any
        # known datum, so the column runs a little below grade and a little
        # noisy. The name suggests a spare Z column
        # and it is in fact the only elevation the layer has; 247 rows sit above
        # 400 ft, which is right for a city climbing into the hills.
        #
        # DXF_LAYER is the CAD layer the points were digitised off and is the
        # constant "SDCB". It is the only type column, so it is mapped, and like
        # Pleasanton's SP_FUNC it needs normalising before a legend can use it.
        #
        # NoGrate flags 26 openings with no grate at all; kept native.
        "datum": "NGVD29",
        "canon": {"AssetID": "STORMCB_ID", "TypeDescription": "DXF_LAYER",
                  "TopOfGrate": "Z_alt"},
        "out": "storm_inlets_belmont",
    },
    "emeryville": {
        "label": "Emeryville",
        # ArcGIS Online hosted, public, no token. Smallest source in the
        # registry, 457 inlets, and the densest per acre.
        "url": ("https://services3.arcgis.com/ljOdqLVbHpS7dOJQ/arcgis/rest"
                "/services/Storm_Inlets_View/FeatureServer/19"),
        # TWO GENERATIONS OF SCHEMA IN ONE LAYER, and the modern half is empty.
        # RIMELEV, INVERTELEV, INVERT, HIGHELEV, CVTYPE, CONDITION, ACTIVEFLAG,
        # OWNEDBY, LOCDESC and INSTALLDATE are the Esri storm-water template and
        # not one of them has a value. Every reading lives in the legacy CAD
        # columns underneath: TC, INVERT1..5, CBNO, MHNO.
        "fields": ["OBJECTID", "FACILITYID", "CBNO", "MHNO", "TC", "TW", "RIM",
                   "INVERT1", "INVERT2", "INVERT3", "INVERT4", "VCOMMENTS"],
        # TC is a top of curb, not a top of grate, and it is the best thing
        # here: 423 of 457 (92.6%) at -2.594 ft from 3DEP across all of them,
        # IQR 0.35, MAD 0.17. That is NGVD29 to within 0.011 ft -- the closest
        # any city in this registry sits to its own datum -- and 391 of the 423
        # points agree individually. RIM, the column that would be the right
        # one, is filled on a single row.
        #
        # INVERT1 packs three facts into text -- `39.91 12" OUT` -- so the
        # leading number is parsed out. It is the FIRST listed invert, not
        # necessarily the outgoing one: 318 rows say OUT, 115 say IN, 11 say
        # neither ("TO MAIN"). Pleasanton could choose INV_OUT over INV_IN
        # because they were separate columns; here they are one, and INVERT2..4
        # ride along native so the direction stays inspectable.
        #
        # No type column exists in either generation, so TypeDescription is
        # blank. CBNO and MHNO duplicate each other and the facility id.
        "convert": {"INVERT1": "lead_float"},
        "datum": "NGVD29",
        "canon": {"AssetID": "FACILITYID", "TopOfGrate": "TC",
                  "InvertElevation1": "INVERT1"},
        "out": "storm_inlets_emeryville",
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


def check_registry():
    """Refuse to run on a registry that leaves a datum unstated.

    The failure this prevents is silent by construction: a city on NAVD88 with
    no `datum` gets shifted 2.605 ft by the plotters' global constant and lands
    every grate 2.6 ft high, which is well inside the 20 ft window the DEM gate
    judges a grate by. Cupertino spent a day in exactly that state. A missing
    key is therefore an error, not a default.
    """
    for key, cfg in CITIES.items():
        elev = [c for c in ELEVATION_COLS if cfg["canon"].get(c)]
        name = cfg.get("datum")
        if elev and not name:
            raise SystemExit(
                f"{key}: maps {', '.join(elev)} but declares no datum. Measure "
                f"it against 3DEP and add one of: {', '.join(DATUMS)}")
        if name and name not in DATUMS:
            raise SystemExit(f"{key}: unknown datum {name!r}; "
                             f"known: {', '.join(DATUMS)}")
        if name and not elev:
            raise SystemExit(f"{key}: declares datum {name!r} but maps no "
                             f"elevation for it to apply to")


def point(lon, lat):
    """(lon, lat) as floats, or None when the row carries no usable location.

    Every consumer of this corpus needs the point and nothing else will do: the
    AOI filter, the snap to a street profile, the DEM sample under the grate.
    A row without one is not an inlet, it is a record of an inlet, and it has
    no place in a file that exists to put drains on a map.

    Refused: null, blank, non-numeric, NaN, and exactly (0, 0) -- null island
    is the other way a service says "no geometry", 380 miles off Ghana.

    Out-of-range raises rather than returning None, because it is a different
    kind of failure: it means the server ignored outSR=4326 and answered in
    State Plane feet or Web Mercator metres. That is systematic, it would put
    every inlet in the wrong hemisphere, and the caller stops the run.
    """
    try:
        lon = float(lon)
        lat = float(lat)
    except (TypeError, ValueError):
        return None
    if lon != lon or lat != lat:                  # NaN
        return None
    if lon == 0 and lat == 0:
        return None
    if abs(lon) > 180 or abs(lat) > 90:
        raise ValueError(f"({lon}, {lat}) is not a longitude/latitude pair")
    return lon, lat


def require_points(rows, city):
    """Every row that survives this carries coordinates. Drops are named.

    Six of the 34 Bay Area sources publish rows with attributes and no
    geometry -- Marin County 25, Oakland 2, then one each in Hayward, Suisun
    City, Burlingame and Emeryville, 31 rows in 93,829. They are dropped rather
    than written with blank coordinates, and dropped loudly: a silent one would
    be indistinguishable from an inlet the AOI filter excluded.
    """
    kept, dropped = [], []
    for r in rows:
        try:
            pt = point(r.get("lon"), r.get("lat"))
        except ValueError as e:
            raise SystemExit(
                f"{city}: {e}. The service ignored outSR=4326, so these are "
                f"projected coordinates -- refusing to write them as lon/lat.")
        if pt is None:
            dropped.append(r)
        else:
            r["lon"], r["lat"] = pt
            kept.append(r)
    if dropped:
        ids = ", ".join(str(r.get("AssetID") or r.get("OBJECTID") or "?")
                        for r in dropped[:6])
        print(f"  ! dropped {len(dropped)} row(s) with no location published: "
              f"{ids}{' ...' if len(dropped) > 6 else ''}")
    if rows and not kept:
        raise SystemExit(f"{city}: no row carries a location; refusing to "
                         f"write a file of {len(rows)} unplaceable inlets")
    return kept


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

    Three more corrections apply to canonical columns only, in this order:

    `convert` first, because the rest cannot judge a value they cannot read --
    San Ramon's "486.44" is not a number until it is parsed, and Fairfield's
    -999 hides inside a date-shaped integer nowhere near it.

    `sentinels` next, so a publisher's "no reading" code becomes blank rather
    than a reading. It has to precede the datum shift: -999 + -100 is -1099,
    which matches nothing and would sail through as data.

    The datum offset last, and only on ELEVATION_COLS. Zero is left alone rather
    than shifted -- a zero elevation means "no reading" everywhere in this
    corpus, and moving it to -100.0 would invent one.
    """
    canon = cfg["canon"]
    convert = cfg.get("convert") or {}
    sentinels = set(cfg.get("sentinels") or ())
    datum = DATUMS[cfg["datum"]] if cfg.get("datum") else 0.0
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
            conv = convert.get(native) if native else None
            if conv:
                v = CONVERTERS[conv](v)
            codes = domains.get(native) if native else None
            if codes and v is not None:
                v = codes.get(str(v), v)
            if isinstance(v, str):
                v = v.strip() or None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if v in sentinels:
                    v = None
                elif datum and v != 0 and name in ELEVATION_COLS:
                    v = round(v + datum, 3)
            row[name] = v
        for f in extras:
            row[f] = a.get(f)
        rows.append(row)
    return require_points(rows, city), cols


def report(rows, cfg):
    # No "missing geometry" count: require_points() has already guaranteed
    # every row here has one, and said so on the way past if any went. The
    # bbox at the end of this function is what proves the coordinates landed
    # where the city is.
    print(f"\ntotal: {len(rows)}")

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

    if rows:
        lons = [r["lon"] for r in rows]
        lats = [r["lat"] for r in rows]
        print(f"\nbbox: lon {min(lons):.5f}..{max(lons):.5f}  "
              f"lat {min(lats):.5f}..{max(lats):.5f}")


def fetch_city(key, cfg, args):
    """One city, from the origin or the Hub cache, to canonical rows."""
    print(f'{cfg["label"]}: {cfg["url"]}')
    if cfg.get("where"):
        print(f'  filter: {cfg["where"]}')
    if cfg.get("convert"):
        print("  convert: " + ", ".join(f"{k} as {v}"
                                        for k, v in cfg["convert"].items()))
    if cfg.get("sentinels"):
        print("  sentinels dropped: "
              + ", ".join(str(s) for s in cfg["sentinels"]))
    if cfg.get("datum"):
        shift = DATUMS[cfg["datum"]]
        print(f'  datum: {cfg["datum"]}'
              + (f', elevations shifted {shift:+g} ft onto the corpus datum'
                 if shift else ' -- the corpus datum, no shift'))

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
            # Every row, unconditionally: require_points() left none without a
            # coordinate, so a filter here could only ever hide a bug.
            for r in rows]}
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
    # Filtered here too, not just on the fetch path: a file written before
    # require_points existed still holds its blank-coordinate rows, and reuse
    # is how they would outlive every rebuild.
    return require_points([{c: r.get(c, "") for c in cols} for r in rows], key), cols


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
    check_registry()

    if args.list:
        for key, c in CITIES.items():
            mapped = ", ".join(f"{k}<-{v}" for k, v in c["canon"].items())
            print(f'{key:<12} {c["label"]:<12} {c["url"]}')
            if c.get("where"):
                print(f'             filter {c["where"]}')
            if c.get("datum"):
                print(f'             datum {c["datum"]} '
                      f'({DATUMS[c["datum"]]:+g} ft)')
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
