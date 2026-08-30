"""City-wide index map over the per-street pages from plot_street_bokeh.py.

Draws every centerline of one city on an Esri Gray Canvas map, coloured by functional
class or by the street's sag count -- a radio button on the page switches
between the two, and --color-by picks which one it opens on. Tapping a street
-- or picking one from the search box -- loads that street's existing
profile+map page into an iframe directly below, so the whole corpus written by
`plot_street_bokeh.py --all` becomes browsable from one entry point.

Nothing here regenerates a street page: the pages are consumed as-is, keyed by
the `file` column of the index that `--all` writes. By default the overview
lands at Stormdrain_map/index.html and the pages in Stormdrain_map/streets/,
and the iframe srcs are relative paths worked out from those two locations.

--pages-alt may be repeated, adding further corpora built at different --smooth
windows. With more than one corpus the page grows a selector that switches
between them. Historically this took a SECOND corpus only, and a
checkbox that switches the whole page between the two: which page the iframe
loads, the sag counts in the tooltips, and the sag colouring itself. Sags come
from the smoothed profile, so a shorter window finds more of them -- the two
sets genuinely disagree, and the checkbox is how they are compared.

Usage:
    python plot_street_bokeh.py --all        # once, to build the pages
    python plot_city_overview.py             # then this
    python plot_city_overview.py --color-by sags    # open on the sag colouring
    python plot_city_overview.py --opens-at "10 m"  # open on that window
    python plot_city_overview.py --pages-alt Stormdrain_map/streets_10m
    python plot_city_overview.py \
        --pages     Stormdrain_map/streets_25m --label     "25 m" \
        --pages-alt Stormdrain_map/streets_10m --alt-label "10 m" \
        --pages-alt Stormdrain_map/streets_5m  --alt-label "5 m"
"""

import argparse
import csv
import os

import numpy as np
import pandas as pd
import xyzservices.providers as xyz
from bokeh.document import Document
from bokeh.events import DocumentReady
from bokeh.layouts import column, row
from bokeh.models import (AutocompleteInput, CDSView,
                          ColumnDataSource, CustomJS, Div, HoverTool,
                          IndexFilter, Legend, LegendItem,
                          RadioButtonGroup, Range1d, TapTool, WheelZoomTool)
from bokeh.plotting import figure, output_file, save
from pyproj import Transformer

from extract_centerline_latlon import DEFAULT_CITY, vertices_path
from plot_points_map import CLASS_COLORS, DRAW_ORDER

VERTS = vertices_path()
PAGES = "Stormdrain_map/streets_25m"
PAGES_ALT = "Stormdrain_map/streets_10m"
OUT = "Stormdrain_map/index.html"
INDEX = "_index.csv"
UNCLASSIFIED = "(unknown)"
MAP_W, MAP_H = 1240, 700
# FRAME_H is the embedded street page's own height, measured: read + pick Divs,
# then plot_street_bokeh.py's PROF_H and MAP_H panels with their titles and
# axes. Short of it and the iframe grows an inner scrollbar, which is what a
# 1,020 px frame was doing around a 1,305 px page. Raise this with PROF_H.
FRAME_W, FRAME_H = 1240, 1320
HIT_W = 12                # invisible fat line under each class, for hit testing

# Set from --city in main(). They were hardcoded to Livermore, so every
# Pleasanton page was captioned "Livermore street centerline".
TITLE = ""
TITLE_SAG = ""
NAME = ""


def city_name(here, slug):
    """A city's display name, from the boundary index that already carries it.

    san_jose -> "San Jose", which slug.title() would render "San_Jose". Falls
    back to the slug if the index is missing or does not list it.
    """
    path = os.path.join(here, "city_geojson", "_index.csv")
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("slug") == slug:
                    return row.get("name") or slug
    except OSError:
        pass
    return slug.replace("_", " ").title()

# Arterials and freeways read as the skeleton of the city; the Local mesh is
# the background it sits on. Ramps stay thin so interchanges do not blob.
CLASS_WIDTH = {
    "Local": 1.1,
    "Minor Collector": 1.6,
    "Major Collector": 2.0,
    "Minor Arterial": 2.2,
    "Principal Arterial": 2.6,
    "Other Freeway or Expressway": 3.0,
    "Interstate": 3.4,
    "Ramp": 1.5,
    UNCLASSIFIED: 1.0,
}

# The alternate colouring: sags per street, binned rather than ramped. 1,391 of
# the 1,728 streets have no sag at all and the tail runs to 29, so a linear
# scale would spend its whole range on a handful of streets. Zero gets a grey
# that reads as background; the ramp starts at one.
#
# (lower bound, label, colour, minimum line width) -- the upper bound of each
# bin is the next entry's lower bound. Width is a floor, not a value: a street
# keeps its class width if that is already fatter, so the arterial skeleton
# survives while a Local street with sags stops being a 1 px line.
SAG_BINS = [
    (0,  "no sag", "#aeb8c2", 0.0),
    (1,  "1",      "#fcc44e", 1.8),
    (2,  "2",      "#f99331", 2.2),
    (3,  "3–4",    "#ef6420", 2.6),
    (5,  "5–9",    "#cf2d16", 3.0),
    (10, "10+",    "#8c0308", 3.4),
]




def sag_bin(n):
    """Index of the SAG_BINS entry a sag count falls in."""
    for i in range(len(SAG_BINS) - 1, -1, -1):
        if n >= SAG_BINS[i][0]:
            return i
    return 0


def load(here, args, variants):
    """Per-OBJECTID polylines in Web Mercator, joined to every page index.

    Fills each variant's "idx" in place and returns the vertices, narrowed to
    the streets that have a page in EVERY corpus -- the checkbox switches a
    loaded street between them, so one that exists in only one set would break
    on the toggle rather than at load.
    """
    v = pd.read_csv(os.path.join(here, args.verts or vertices_path(args.city)),
                    usecols=["OBJECTID", "display_name", "road_class",
                             "vertex_index", "lat", "lon"])
    # Anything unrecognised joins the unclassified bucket rather than vanishing:
    # only the classes in DRAW_ORDER get drawn.
    v["road_class"] = v.road_class.where(
        v.road_class.isin(CLASS_COLORS), UNCLASSIFIED)
    v = v.sort_values(["OBJECTID", "vertex_index"], kind="stable")

    # One transform over the whole file, then slice: per-group transforms would
    # pay pyproj's setup cost 4,867 times.
    to3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    v["mx"], v["my"] = to3857.transform(v.lon.to_numpy(), v.lat.to_numpy())

    # The page, not the street, is the unit here. A street whose runs are
    # disconnected gets a page each -- THIRD ST has four, 4.5 km apart -- so a
    # name no longer identifies a page, and a bbox drawn per name would span
    # the whole town. Each page declares the OBJECTIDs it covers, so a segment
    # maps straight to its own page.
    for var in variants:
        idx = pd.read_csv(os.path.join(here, var["pages"], INDEX))
        if "page" not in idx.columns or "segments" not in idx.columns:
            raise SystemExit(
                f"{var['pages']}/{INDEX} predates the per-run pages: rebuild it "
                "with plot_street_bokeh.py --all")
        var["idx"] = idx.set_index("page")
        var["oid2page"] = {int(o): pg
                           for pg, segs in zip(idx["page"], idx["segments"])
                           for o in str(segs).split()}
    v["page"] = v.OBJECTID.map(variants[0]["oid2page"])
    have = set.intersection(*[set(var["idx"].index) for var in variants])
    orphan = int(v.page.isna().sum())
    if orphan:
        print(f"  {orphan:,} vertices belong to no page, dropped")
        v = v[v.page.notna()]
    missing = sorted(set(v.page) - have)
    if missing:
        print(f"  {len(missing)} page(s) missing from a corpus, dropped: "
              f"{', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}")
        v = v[v.page.isin(have)]
    return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default=DEFAULT_CITY,
                    help="city slug; sets the default --verts")
    ap.add_argument("--verts", default=None,
                    help=f"vertices CSV (default {VERTS})")
    ap.add_argument("--pages", default=PAGES,
                    help="directory holding the per-street pages and _index.csv")
    ap.add_argument("--pages-alt", action="append", default=None,
                    help="another corpus built at a different --smooth "
                         "window; repeatable. Two or more corpora add the "
                         f"selector that switches between them (e.g. {PAGES_ALT})")
    ap.add_argument("--label", default="25 m",
                    help="how --pages is named on the page")
    # default=None, not a string: argparse's append action would try to
    # .append() onto the default and blow up before main() ever runs.
    ap.add_argument("--alt-label", default=None, action="append",
                    help="how --pages-alt is named on the page; repeat once "
                         "per --pages-alt, in the same order")
    ap.add_argument("--out", default=OUT,
                    help="path for the overview page itself")
    ap.add_argument("--opens-at", default=None, metavar="LABEL",
                    help="which smoothing window the page opens on, named by "
                         "its label (e.g. \"10 m\"). Independent of the order "
                         "the corpora are listed in; defaults to --pages")
    ap.add_argument("--color-by", "--colour-by", dest="color_by",
                    choices=("class", "sags"), default="class",
                    help="colouring the page opens on; both are always built "
                         "and switchable on the page itself")
    args = ap.parse_args()
    sag_first = args.color_by == "sags"

    # Captions name the city being drawn. Module globals rather than threaded
    # arguments because the figure builders below already read them by name.
    global TITLE, TITLE_SAG, NAME
    NAME = city_name(os.path.dirname(os.path.abspath(__file__)), args.city)
    TITLE = (f"{NAME} street centerline — tap a street to load its profile "
             f"and drainage map below")
    TITLE_SAG = (f"{NAME} streets by sag count — tap a street to load its "
                 f"profile and drainage map below")

    # Primary first: it is the one the page opens on, and the one whose inlet
    # counts and lengths are used -- neither depends on the smoothing window.
    variants = [dict(pages=args.pages, label=args.label)]
    for i, alt in enumerate(args.pages_alt or []):
        # Fall back to the directory name when a label is missing, rather than
        # failing: an unlabelled corpus is still usable, just less well named.
        labs = args.alt_label or []
        lab = labs[i] if i < len(labs) else os.path.basename(alt.rstrip("/\\"))
        variants.append(dict(pages=alt, label=lab))

    # Which variant the page OPENS on -- deliberately not tied to the order the
    # corpora are listed in. The selector shows them in the given order, which
    # for 25 / 10 / 5 reads as a scale; the page can still open on any of them.
    # Matched by label rather than index so the caller says "10 m", not "1".
    dvi = 0
    if args.opens_at is not None:
        have = [var["label"] for var in variants]
        if args.opens_at not in have:
            ap.error(f"--opens-at {args.opens_at!r} matches no corpus; "
                     f"labels are {have}")
        dvi = have.index(args.opens_at)

    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, args.out)
    # The overview no longer sits beside the pages it opens, so every iframe src
    # needs a relative hop from wherever this page lands to wherever they do.
    # Derived rather than hardcoded, so --out and --pages stay free to move.
    # Forward slashes: this ends up in a URL, not a filesystem path.
    for var in variants:
        rel = os.path.relpath(os.path.join(here, var["pages"]),
                              os.path.dirname(out)).replace(os.sep, "/")
        var["pfx"] = "" if rel in ("", ".") else rel + "/"
    v = load(here, args, variants)

    # ---------------- geometry, one entry per OBJECTID ----------------
    # Not per street: a name spans several disjoint segments, and folding them
    # into one polyline would need NaN separators. Each entry carries its own
    # street name, so tapping any segment still opens the right page.
    rows = {c: dict(xs=[], ys=[], name=[], cls=[]) for c in CLASS_COLORS}
    bbox = {}
    for (oid, nm, cls), g in v.groupby(
            ["OBJECTID", "page", "road_class"], sort=False):
        mx, my = g.mx.to_numpy(), g.my.to_numpy()
        r = rows.setdefault(cls, dict(xs=[], ys=[], name=[], cls=[]))
        r["xs"].append(mx)
        r["ys"].append(my)
        r["name"].append(nm)
        r["cls"].append(cls)
        b = bbox.get(nm)
        lo = (mx.min(), my.min(), mx.max(), my.max())
        bbox[nm] = lo if b is None else (min(b[0], lo[0]), min(b[1], lo[1]),
                                         max(b[2], lo[2]), max(b[3], lo[3]))

    # ---------------- map ----------------
    xs_all = np.concatenate([a for r in rows.values() for a in r["xs"]])
    ys_all = np.concatenate([a for r in rows.values() for a in r["ys"]])
    padx = 0.02*(xs_all.max() - xs_all.min())
    pady = 0.02*(ys_all.max() - ys_all.min())
    mp = figure(width=MAP_W, height=MAP_H, match_aspect=True,
                tools="pan,wheel_zoom,box_zoom,reset",
                x_range=Range1d(xs_all.min()-padx, xs_all.max()+padx),
                y_range=Range1d(ys_all.min()-pady, ys_all.max()+pady),
                x_axis_type="mercator", y_axis_type="mercator",
                title=TITLE_SAG if sag_first else TITLE)
    # Esri.WorldGrayCanvas, not CartoDB.Positron. CARTO began requiring an API
    # key for its raster basemaps around 2026-08-25/26 and now serves
    # unauthenticated requests as a valid PNG watermarked "API KEY REQUIRED" --
    # HTTP 200, no error, so the page just quietly renders a spoiled map. These
    # tiles are fetched by the BROWSER on every page open, so every existing
    # page was affected the moment CARTO flipped it, build date irrelevant.
    # Gray Canvas is key-free and plays the same muted-backdrop role.
    mp.add_tile(xyz.Esri.WorldGrayCanvas)
    # City scale means a lot of zooming; make the wheel do it without a toolbar
    # trip. The per-street pages leave this off, where panning matters more.
    mp.toolbar.active_scroll = mp.select_one(WheelZoomTool)

    tips = [("street", "@name"), ("class", "@cls"),
            ("inlets", "@n_inlets"), ("sags", "@n_sags (@n_unserved unserved)"),
            ("street length", "@length_m{0,0} m")]
    srcs, hits, cls_lines, cls_items, cls_w = [], [], [], [], []
    # Which segments of which class source land in which sag bin, per variant.
    # Indices, not geometry: every sag colouring draws the very same sources
    # through a filter, so the coordinates are serialised into the page once
    # however many corpora are being compared.
    by_bin = [[{} for _ in SAG_BINS] for _ in variants]
    bin_names = [[set() for _ in SAG_BINS] for _ in variants]   # legend counts
    for cls in DRAW_ORDER:                      # Local first, Interstate last
        r = rows.get(cls)
        if not r or not r["xs"]:
            continue
        stats = [var["idx"].reindex(r["name"]) for var in variants]
        # Inlets and length are upstream of the smoothing -- an inlet snaps to
        # the centerline and a street is as long as it is -- so they come from
        # the primary and stay put. Only the sag columns move with the checkbox.
        data = dict(xs=r["xs"], ys=r["ys"], name=r["name"], cls=r["cls"],
                    n_inlets=stats[0].n_inlets.to_numpy(),
                    length_m=stats[0].length_m.to_numpy(),
                    n_sags=stats[dvi].n_sags.to_numpy(),
                    n_unserved=stats[dvi].n_unserved.to_numpy())
        # Tooltips name fixed columns, so the toggle copies the variant it wants
        # into n_sags/n_unserved. Both sets have to be on the source to do that.
        for vi, s in enumerate(stats):
            data[f"n_sags_v{vi}"] = s.n_sags.to_numpy()
            data[f"n_unserved_v{vi}"] = s.n_unserved.to_numpy()
        src = ColumnDataSource(data)
        # Two renderers on ONE source: the visible line, and a fat transparent
        # one to catch the cursor -- a 1 px Local street is otherwise unclickable.
        # Both go in the same legend item so a legend click hides the hit target
        # too, otherwise a hidden class would still be tappable.
        w0 = CLASS_WIDTH.get(cls, 1.2)
        line = mp.multi_line("xs", "ys", source=src, line_color=CLASS_COLORS[cls],
                             line_width=w0, visible=not sag_first)
        hit = mp.multi_line("xs", "ys", source=src, line_width=HIT_W,
                            line_alpha=0.0)
        cls_items.append(LegendItem(label=f"{cls} ({len(r['xs']):,})",
                                    renderers=[line, hit]))
        srcs.append(src)
        hits.append(hit)
        cls_lines.append(line)
        cls_w.append(w0)

        si = len(srcs) - 1
        for vi, s in enumerate(stats):
            for k, n in enumerate(s.n_sags.to_numpy()):
                b = sag_bin(int(n))
                by_bin[vi][b].setdefault(si, []).append(k)
                bin_names[vi][b].add(r["name"][k])
    mp.add_tools(HoverTool(renderers=hits, tooltips=tips, line_policy="nearest"))

    # Sag colouring: one renderer per (class source, bin), bins outermost so the
    # worst streets draw over the quiet ones. Splitting by class as well as bin
    # is what lets a line keep its class width -- and it costs nothing, since
    # each renderer is just a filtered view of a source that already exists.
    # Hover, tap and the search box hang off the class renderers, so they behave
    # identically in either colouring.
    # One such set per variant, since the counts differ between them; SAG_VAR
    # keeps a flat parallel list of which variant each renderer belongs to, so
    # the JS can flip visibility without nesting.
    sag_lines, sag_var, sag_legs = [], [], []
    for vi, var in enumerate(variants):
        shown = sag_first and vi == dvi
        sag_items = []
        for (_, label, colour, wmin), members, nms in zip(SAG_BINS, by_bin[vi],
                                                          bin_names[vi]):
            rs = [mp.multi_line("xs", "ys", source=srcs[si], visible=shown,
                                view=CDSView(filter=IndexFilter(ks)),
                                line_color=colour,
                                line_width=max(cls_w[si], wmin))
                  for si, ks in sorted(members.items())]
            if not rs:
                continue
            # Streets, not segments as the class legend counts: the colour is a
            # per-street number, and one street is many segments.
            sag_items.append(LegendItem(label=f"{label} ({len(nms):,} streets)",
                                        renderers=rs))
            sag_lines.extend(rs)
            sag_var.extend([vi]*len(rs))
        leg = Legend(items=sag_items, visible=shown,
                     title="sags on street" if len(variants) == 1
                     else f"sags on street — {var['label']}")
        mp.add_layout(leg)
        sag_legs.append(leg)

    # Added last so it draws over both colourings. Zooming to a street is not
    # much use if you cannot tell which of the lines in view it is. In neither
    # legend: this is not a class or a bin, and it must not be hideable.
    hi = ColumnDataSource(dict(xs=[], ys=[]))
    # Blue in both colourings, so one renderer does it: the highlight means the
    # same thing either way, and a colour that changes with the mode reads as
    # though it were encoding something. Blue is the choice because red -- the
    # obvious highlight -- vanishes into the top of the sag ramp, which is
    # itself red. Nothing here flips with the mode any more.
    mp.multi_line("xs", "ys", source=hi, line_color="#0a84ff",
                  line_width=5, line_alpha=0.9, line_cap="round")

    # One legend per colouring -- per variant, for the sag ones -- all parked at
    # the same spot inside the plot; only ever one is visible, so they never
    # collide. Explicit rather than legend_label=, because that folds everything
    # into a single legend.
    leg_cls = Legend(items=cls_items, visible=not sag_first)
    mp.add_layout(leg_cls)
    mp.legend.click_policy = "hide"
    mp.legend.label_text_font_size = "8pt"
    mp.legend.background_fill_alpha = 0.85

    # ---------------- widgets ----------------
    names = sorted(bbox)
    search = AutocompleteInput(
        completions=names, search_strategy="includes", case_sensitive=False,
        min_characters=2, width=380, placeholder=f"search {len(names):,} pages")
    colour = RadioButtonGroup(labels=["road class", "sags per street"],
                              active=int(sag_first), width=260)
    # A radio group, not a checkbox: there can be any number of corpora now, and
    # a radio's `active` IS the variant index, which is what both JS callbacks
    # want. The JS below treats a null widget as "always the primary", so a
    # single-corpus page needs no selector at all.
    smooth = None
    if len(variants) > 1:
        smooth = RadioButtonGroup(
            labels=[f"{var['label']} smoothing" for var in variants],
            active=dvi, width=max(320, 150 * len(variants)))

    labels = [var["label"] for var in variants]
    titles_sag = [TITLE_SAG if len(variants) == 1
                  else f"{TITLE_SAG} ({lab} smoothing)" for lab in labels]
    # The figure was built with the bare TITLE_SAG, which named no window at
    # all until the reader touched the selector. Name it from the start.
    if sag_first:
        mp.title.text = titles_sag[dvi]

    # Shared by the colouring switch and the smoothing checkbox: both change
    # which renderers are on, and each has to honour the other's state.
    VIS_JS = """
        const sag = colour.active === 1;
        const v = SM ? SM.active : 0;
        // Legends hide by flipping renderer.visible, so switching also clears
        // whatever the other mode had hidden -- deliberate: the legends filter
        // on different keys and cannot be kept in step. The hit targets come
        // back either way, or a class hidden here would stay untappable over
        // there.
        for (const r of CLS) { r.visible = !sag; }
        for (let i = 0; i < SAG.length; i++) { SAG[i].visible = sag && SAGV[i] === v; }
        for (const r of HIT) { r.visible = true; }
        legC.visible = !sag;
        for (let k = 0; k < LEGS.length; k++) { LEGS[k].visible = sag && k === v; }
        title.text = sag ? TS[v] : T;
    """
    vis_args = dict(CLS=cls_lines, SAG=sag_lines, SAGV=sag_var, HIT=hits,
                    legC=leg_cls, LEGS=sag_legs, title=mp.title, T=TITLE,
                    TS=titles_sag, colour=colour, SM=smooth)
    colour.js_on_change("active", CustomJS(args=vis_args, code=VIS_JS))

    others = [lab for i, lab in enumerate(labels) if i != dvi]
    opens = ("" if len(variants) == 1 else
             f" &middot; pages open at {labels[dvi]} smoothing; "
             f"switch to {', '.join(others)} above the map")
    pick = Div(text="<i>tap a street on the map, or search above</i>" + opens,
               width=MAP_W,
               styles={"font-family": "monospace", "font-size": "13px",
                       "padding": "4px 0", "min-height": "20px"})
    frame = Div(text=f"<div style='width:{FRAME_W}px;height:{FRAME_H}px;"
                     "display:flex;align-items:center;justify-content:center;"
                     "background:#eef1f4;color:#667;font:13px monospace;"
                     "border:1px solid #d5dade'>no street selected</div>",
                width=FRAME_W, height=FRAME_H, disable_math=True)

    # name -> everything the page needs about a street. href/sags/unserved are
    # one entry per variant, indexed by the checkbox; inlets and length do not
    # move with the smoothing window, so they stay scalars.
    prim = variants[0]["idx"]
    meta = {nm: dict(href=[var["pfx"] + str(var["idx"].at[nm, "file"])
                           for var in variants],
                     bbox=[float(q) for q in bbox[nm]],
                     sags=[int(var["idx"].at[nm, "n_sags"]) for var in variants],
                     unserved=[int(var["idx"].at[nm, "n_unserved"])
                               for var in variants],
                     inlets=int(prim.at[nm, "n_inlets"]),
                     length=float(prim.at[nm, "length_m"]))
            for nm in names}

    # Which street is loaded, so the smoothing checkbox can reload the same one
    # into the other corpus. A CDS rather than a JS global: every callback is a
    # separate function body, and this is the only state they share.
    state = ColumnDataSource(dict(nm=[""]))

    # Shared tail: every entry point resolves a street NAME, then this loads it.
    # Rewriting .text rebuilds the iframe, which is what navigates it -- Bokeh
    # renders widgets into a shadow root, so getElementById cannot reach the
    # existing element to poke its src.
    LOAD_JS = """
        const M = META[nm];
        if (M === undefined) { return; }
        // Named vi, not v: VIS_JS declares its own v and the two get
        // concatenated into one function body on the checkbox callback.
        const vi = SM ? SM.active : 0;
        // Repaint the highlight from the geometry already in the browser --
        // the class sources hold it, so META does not have to carry a second
        // copy of every coordinate.
        const hx = [], hy = [];
        for (const s of S) {
            const N = s.data.name;
            for (let k = 0; k < N.length; k++) {
                if (N[k] === nm) { hx.push(s.data.xs[k]); hy.push(s.data.ys[k]); }
            }
        }
        hi.data = {xs: hx, ys: hy};
        hi.change.emit();
        state.data.nm = [nm];
        pick.text = "<b>" + nm + "</b> &middot; " + M.inlets + " inlets &middot; "
                  + M.sags[vi] + " sags (" + M.unserved[vi] + " unserved) &middot; "
                  + M.length.toFixed(0) + " m &middot; " + LAB[vi]
                  + " smoothing &middot; <span style='color:#777'>"
                  + M.href[vi] + "</span>";
        frame.text = "<iframe src='" + M.href[vi] + "' width='%d' height='%d'"
                   + " style='border:1px solid #d5dade' loading='lazy'></iframe>";
        if (decodeURIComponent((window.location.hash || "").slice(1)) !== nm) {
            history.replaceState(null, "", "#" + encodeURIComponent(nm));
        }
    """ % (FRAME_W, FRAME_H)

    ZOOM_JS = """
        // Pad the street's own bbox, then grow the short side to the map's
        // aspect -- match_aspect only corrects a range Bokeh itself set, and a
        // range assigned from JS would otherwise squash the basemap.
        let [x0, y0, x1, y1] = M.bbox;
        const px = Math.max(0.25*(x1 - x0), 120), py = Math.max(0.25*(y1 - y0), 120);
        x0 -= px; x1 += px; y0 -= py; y1 += py;
        const ar = %f, bw = x1 - x0, bh = y1 - y0;
        if (bw/bh < ar) { const c = (x0 + x1)/2, r = bh*ar/2; x0 = c - r; x1 = c + r; }
        else            { const c = (y0 + y1)/2, r = bw/ar/2; y0 = c - r; y1 = c + r; }
        xr.start = x0; xr.end = x1; yr.start = y0; yr.end = y1;
    """ % (MAP_W/MAP_H)

    cb_args = dict(META=meta, pick=pick, frame=frame, S=srcs, hi=hi,
                   xr=mp.x_range, yr=mp.y_range, SM=smooth, LAB=labels,
                   state=state)

    tap = CustomJS(args=cb_args, code="""
        // One TapTool covers every class, so find which source took the hit.
        // Tapping empty space clears them all; leave the last pick loaded.
        let nm = null;
        for (const s of S) {
            const sel = s.selected.indices;
            if (sel && sel.length) { nm = s.data.name[sel[0]]; break; }
        }
        if (nm === null) { return; }
    """ + LOAD_JS)
    tt = TapTool(renderers=hits, callback=tap)
    mp.add_tools(tt)
    # active_tap stays null when the toolbar came from an explicit tools=
    # string, and clicks never reach the tool. Bind it.
    mp.toolbar.active_tap = tt

    search.js_on_change("value", CustomJS(args=cb_args, code="""
        const nm = cb_obj.value;
    """ + LOAD_JS + ZOOM_JS))

    ready = CustomJS(args=cb_args, code="""
        // Restore a bookmarked street. The hash holds the name, not the file,
        // so a page renamed by a rebuild still resolves.
        const nm = decodeURIComponent((window.location.hash || "").slice(1));
        if (!nm) { return; }
    """ + LOAD_JS + ZOOM_JS)

    if smooth is not None:
        # Everything the window touches, in one callback: which renderers are
        # drawn, the counts behind the tooltips, and the page in the iframe.
        # Nothing re-zooms -- the view is where the reader put it, and only the
        # smoothing changed.
        smooth.js_on_change("active", CustomJS(
            args={**vis_args, **cb_args}, code=VIS_JS + """
            // Tooltips name fixed columns, so move the variant under them.
            for (const s of S) {
                s.data.n_sags = s.data["n_sags_v" + v];
                s.data.n_unserved = s.data["n_unserved_v" + v];
                s.change.emit();
            }
            const nm = state.data.nm[0];
            if (!nm) { return; }
        """ + LOAD_JS))

    os.makedirs(os.path.dirname(out), exist_ok=True)
    doc = Document()
    # The checkbox goes between the map and the iframe, not in the header strip:
    # it changes which page loads below it, so it belongs next to that page
    # rather than beside the search box it has nothing to do with.
    body = [row(search, colour), pick, mp]
    if smooth is not None:
        body.append(smooth)
    body.append(frame)
    doc.add_root(column(*body))
    doc.js_on_event(DocumentReady, ready)
    output_file(out, title=f"{NAME} stormdrain — street index", mode="cdn")
    save(doc)
    n_seg = sum(len(s.data["xs"]) for s in srcs)
    print(f"{n_seg:,} segments over {len(names):,} pages -> {out}  "
          f"({os.path.getsize(out)/1e6:.1f} MB)")
    for var in variants:
        sub = var["idx"].reindex(names)
        print(f"  {var['label']:>6} smoothing: {int(sub.n_sags.sum()):,} sags, "
              f"{int(sub.n_unserved.sum()):,} unserved   ({var['pages']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
