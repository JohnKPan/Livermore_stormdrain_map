"""City-wide index map over the per-street pages from plot_street_bokeh.py.

Draws every Livermore centerline on one CartoDB map, coloured by functional
class or by the street's sag count -- a radio button on the page switches
between the two, and --color-by picks which one it opens on. Tapping a street
-- or picking one from the search box -- loads that street's existing
profile+map page into an iframe directly below, so the whole corpus written by
`plot_street_bokeh.py --all` becomes browsable from one entry point.

Nothing here regenerates a street page: the pages are consumed as-is, keyed by
the `file` column of the index that `--all` writes. By default the overview
lands at Stormdrain_map/index.html and the pages in Stormdrain_map/streets/,
and the iframe srcs are relative paths worked out from those two locations.

Usage:
    python plot_street_bokeh.py --all        # once, to build the pages
    python plot_city_overview.py             # then this
    python plot_city_overview.py --color-by sags    # open on the sag colouring
"""

import argparse
import os

import numpy as np
import pandas as pd
import xyzservices.providers as xyz
from bokeh.document import Document
from bokeh.events import DocumentReady
from bokeh.layouts import column, row
from bokeh.models import (AutocompleteInput, CDSView, ColumnDataSource,
                          CustomJS, Div, HoverTool, IndexFilter, Legend,
                          LegendItem, RadioButtonGroup, Range1d, TapTool,
                          WheelZoomTool)
from bokeh.plotting import figure, output_file, save
from pyproj import Transformer

from plot_points_map import CLASS_COLORS, DRAW_ORDER

VERTS = "derived/segments_vertices.csv"
PAGES = "Stormdrain_map/streets"
OUT = "Stormdrain_map/index.html"
INDEX = "_index.csv"
UNCLASSIFIED = "(unclassified)"
MAP_W, MAP_H = 1240, 700
FRAME_W, FRAME_H = 1240, 1020
HIT_W = 12                # invisible fat line under each class, for hit testing

TITLE = ("Livermore street centerline — tap a street to load its profile and "
         "drainage map below")
TITLE_SAG = ("Livermore streets by sag count — tap a street to load its "
             "profile and drainage map below")

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


def load(here, args):
    """Per-OBJECTID polylines in Web Mercator, joined to the page index."""
    v = pd.read_csv(os.path.join(here, args.verts),
                    usecols=["OBJECTID", "FullStreetName", "FunctionalClass",
                             "vertex_index", "lat", "lon"])
    # Anything unrecognised joins the unclassified bucket rather than vanishing:
    # only the classes in DRAW_ORDER get drawn.
    v["FunctionalClass"] = v.FunctionalClass.where(
        v.FunctionalClass.isin(CLASS_COLORS), UNCLASSIFIED)
    v = v.sort_values(["OBJECTID", "vertex_index"], kind="stable")

    # One transform over the whole file, then slice: per-group transforms would
    # pay pyproj's setup cost 4,867 times.
    to3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    v["mx"], v["my"] = to3857.transform(v.lon.to_numpy(), v.lat.to_numpy())

    idx = pd.read_csv(os.path.join(here, args.pages, INDEX))
    have = set(idx.FullStreetName)
    missing = sorted(set(v.FullStreetName) - have)
    if missing:
        print(f"  {len(missing)} street(s) have no page, dropped: "
              f"{', '.join(missing[:5])}{' ...' if len(missing) > 5 else ''}")
        v = v[v.FullStreetName.isin(have)]
    return v, idx.set_index("FullStreetName")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verts", default=VERTS)
    ap.add_argument("--pages", default=PAGES,
                    help="directory holding the per-street pages and _index.csv")
    ap.add_argument("--out", default=OUT,
                    help="path for the overview page itself")
    ap.add_argument("--color-by", "--colour-by", dest="color_by",
                    choices=("class", "sags"), default="class",
                    help="colouring the page opens on; both are always built "
                         "and switchable on the page itself")
    args = ap.parse_args()
    sag_first = args.color_by == "sags"

    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, args.out)
    # The overview no longer sits beside the pages it opens, so every iframe src
    # needs a relative hop from wherever this page lands to wherever they do.
    # Derived rather than hardcoded, so --out and --pages stay free to move.
    # Forward slashes: this ends up in a URL, not a filesystem path.
    rel = os.path.relpath(os.path.join(here, args.pages),
                          os.path.dirname(out)).replace(os.sep, "/")
    pfx = "" if rel in ("", ".") else rel + "/"
    v, idx = load(here, args)

    # ---------------- geometry, one entry per OBJECTID ----------------
    # Not per street: a name spans several disjoint segments, and folding them
    # into one polyline would need NaN separators. Each entry carries its own
    # street name, so tapping any segment still opens the right page.
    rows = {c: dict(xs=[], ys=[], name=[], cls=[]) for c in CLASS_COLORS}
    bbox = {}
    for (oid, nm, cls), g in v.groupby(
            ["OBJECTID", "FullStreetName", "FunctionalClass"], sort=False):
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
    mp.add_tile(xyz.CartoDB.Positron)
    # City scale means a lot of zooming; make the wheel do it without a toolbar
    # trip. The per-street pages leave this off, where panning matters more.
    mp.toolbar.active_scroll = mp.select_one(WheelZoomTool)

    tips = [("street", "@name"), ("class", "@cls"),
            ("inlets", "@n_inlets"), ("sags", "@n_sags (@n_unserved unserved)"),
            ("street length", "@length_m{0,0} m")]
    srcs, hits, cls_lines, cls_items, cls_w = [], [], [], [], []
    # Which segments of which class source land in which sag bin. Indices, not
    # geometry: the sag colouring draws the very same sources through a filter,
    # so the coordinates are serialised into the page once rather than twice.
    by_bin = [{} for _ in SAG_BINS]
    bin_names = [set() for _ in SAG_BINS]       # for the legend counts
    for cls in DRAW_ORDER:                      # Local first, Interstate last
        r = rows.get(cls)
        if not r or not r["xs"]:
            continue
        stats = idx.reindex(r["name"])
        src = ColumnDataSource(dict(
            xs=r["xs"], ys=r["ys"], name=r["name"], cls=r["cls"],
            n_inlets=stats.n_inlets.to_numpy(),
            n_sags=stats.n_sags.to_numpy(),
            n_unserved=stats.n_unserved.to_numpy(),
            length_m=stats.length_m.to_numpy()))
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
        for k, n in enumerate(stats.n_sags.to_numpy()):
            b = sag_bin(int(n))
            by_bin[b].setdefault(si, []).append(k)
            bin_names[b].add(r["name"][k])
    mp.add_tools(HoverTool(renderers=hits, tooltips=tips, line_policy="nearest"))

    # Sag colouring: one renderer per (class source, bin), bins outermost so the
    # worst streets draw over the quiet ones. Splitting by class as well as bin
    # is what lets a line keep its class width -- and it costs nothing, since
    # each renderer is just a filtered view of a source that already exists.
    # Hover, tap and the search box hang off the class renderers, so they behave
    # identically in either colouring.
    sag_lines, sag_items = [], []
    for (_, label, colour, wmin), members, nms in zip(SAG_BINS, by_bin, bin_names):
        rs = [mp.multi_line("xs", "ys", source=srcs[si], visible=sag_first,
                            view=CDSView(filter=IndexFilter(ks)),
                            line_color=colour, line_width=max(cls_w[si], wmin))
              for si, ks in sorted(members.items())]
        if not rs:
            continue
        # Streets, not segments as the class legend counts: the colour is a
        # per-street number, and one street is many segments.
        sag_items.append(LegendItem(label=f"{label} ({len(nms):,} streets)",
                                    renderers=rs))
        sag_lines.extend(rs)

    # Added last so it draws over both colourings. Zooming to a street is not
    # much use if you cannot tell which of the lines in view it is. In neither
    # legend: this is not a class or a bin, and it must not be hideable.
    hi = ColumnDataSource(dict(xs=[], ys=[]))
    # Two renderers over one source: red disappears into the top of the sag
    # ramp, which is itself red, so that colouring gets a blue highlight. Same
    # visibility flip as everything else rather than a colour swap in JS.
    hi_cls = mp.multi_line("xs", "ys", source=hi, line_color="#e00000",
                           line_width=5, line_alpha=0.8, line_cap="round",
                           visible=not sag_first)
    hi_sag = mp.multi_line("xs", "ys", source=hi, line_color="#0a84ff",
                           line_width=5, line_alpha=0.9, line_cap="round",
                           visible=sag_first)

    # One legend per colouring, both parked at the same spot inside the plot;
    # only ever one is visible, so they never collide. Explicit rather than
    # legend_label= because that folds everything into a single legend.
    leg_cls = Legend(items=cls_items, visible=not sag_first)
    leg_sag = Legend(items=sag_items, visible=sag_first, title="sags on street")
    for leg in (leg_cls, leg_sag):
        mp.add_layout(leg)
    mp.legend.click_policy = "hide"
    mp.legend.label_text_font_size = "8pt"
    mp.legend.background_fill_alpha = 0.85

    # ---------------- widgets ----------------
    names = sorted(bbox)
    search = AutocompleteInput(
        completions=names, search_strategy="includes", case_sensitive=False,
        min_characters=2, width=380, placeholder=f"search {len(names):,} streets")
    colour = RadioButtonGroup(labels=["road class", "sags per street"],
                              active=int(sag_first), width=260)
    colour.js_on_change("active", CustomJS(
        args=dict(CLS=cls_lines, SAG=sag_lines, HIT=hits, hiC=hi_cls,
                  hiS=hi_sag, legC=leg_cls, legS=leg_sag, title=mp.title,
                  T=TITLE, TS=TITLE_SAG),
        code="""
        const sag = cb_obj.active === 1;
        // Both legends hide by flipping renderer.visible, so switching also
        // clears whatever the other mode had hidden -- deliberate: the two
        // legends filter on different keys and cannot be kept in step. The hit
        // targets come back either way, or a class hidden here would stay
        // untappable over there.
        for (const r of CLS) { r.visible = !sag; }
        for (const r of SAG) { r.visible = sag; }
        for (const r of HIT) { r.visible = true; }
        hiC.visible = !sag;
        hiS.visible = sag;
        legC.visible = !sag;
        legS.visible = sag;
        title.text = sag ? TS : T;
    """))
    pick = Div(text="<i>tap a street on the map, or search above</i>",
               width=MAP_W,
               styles={"font-family": "monospace", "font-size": "13px",
                       "padding": "4px 0", "min-height": "20px"})
    frame = Div(text=f"<div style='width:{FRAME_W}px;height:{FRAME_H}px;"
                     "display:flex;align-items:center;justify-content:center;"
                     "background:#eef1f4;color:#667;font:13px monospace;"
                     "border:1px solid #d5dade'>no street selected</div>",
                width=FRAME_W, height=FRAME_H, disable_math=True)

    # name -> [href, x0, y0, x1, y1, inlets, sags, unserved, length]
    meta = {nm: [pfx + str(idx.at[nm, "file"]), *[float(q) for q in bbox[nm]],
                 int(idx.at[nm, "n_inlets"]), int(idx.at[nm, "n_sags"]),
                 int(idx.at[nm, "n_unserved"]), float(idx.at[nm, "length_m"])]
            for nm in names}

    # Shared tail: every entry point resolves a street NAME, then this loads it.
    # Rewriting .text rebuilds the iframe, which is what navigates it -- Bokeh
    # renders widgets into a shadow root, so getElementById cannot reach the
    # existing element to poke its src.
    LOAD_JS = """
        const M = META[nm];
        if (M === undefined) { return; }
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
        pick.text = "<b>" + nm + "</b> &middot; " + M[5] + " inlets &middot; "
                  + M[6] + " sags (" + M[7] + " unserved) &middot; "
                  + M[8].toFixed(0) + " m &middot; <span style='color:#777'>"
                  + M[0] + "</span>";
        frame.text = "<iframe src='" + M[0] + "' width='%d' height='%d'"
                   + " style='border:1px solid #d5dade' loading='lazy'></iframe>";
        if (decodeURIComponent((window.location.hash || "").slice(1)) !== nm) {
            history.replaceState(null, "", "#" + encodeURIComponent(nm));
        }
    """ % (FRAME_W, FRAME_H)

    ZOOM_JS = """
        // Pad the street's own bbox, then grow the short side to the map's
        // aspect -- match_aspect only corrects a range Bokeh itself set, and a
        // range assigned from JS would otherwise squash the basemap.
        let [x0, y0, x1, y1] = [M[1], M[2], M[3], M[4]];
        const px = Math.max(0.25*(x1 - x0), 120), py = Math.max(0.25*(y1 - y0), 120);
        x0 -= px; x1 += px; y0 -= py; y1 += py;
        const ar = %f, bw = x1 - x0, bh = y1 - y0;
        if (bw/bh < ar) { const c = (x0 + x1)/2, r = bh*ar/2; x0 = c - r; x1 = c + r; }
        else            { const c = (y0 + y1)/2, r = bw/ar/2; y0 = c - r; y1 = c + r; }
        xr.start = x0; xr.end = x1; yr.start = y0; yr.end = y1;
    """ % (MAP_W/MAP_H)

    cb_args = dict(META=meta, pick=pick, frame=frame, S=srcs, hi=hi,
                   xr=mp.x_range, yr=mp.y_range)

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

    os.makedirs(os.path.dirname(out), exist_ok=True)
    doc = Document()
    doc.add_root(column(row(search, colour), pick, mp, frame))
    doc.js_on_event(DocumentReady, ready)
    output_file(out, title="Livermore stormdrain — street index", mode="cdn")
    save(doc)
    n_seg = sum(len(s.data["xs"]) for s in srcs)
    print(f"{n_seg:,} segments over {len(names):,} streets -> {out}  "
          f"({os.path.getsize(out)/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
