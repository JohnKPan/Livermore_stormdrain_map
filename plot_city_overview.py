"""City-wide index map over the per-street pages from plot_street_bokeh.py.

Draws every Livermore centerline on one CartoDB map, coloured by functional
class. Tapping a street -- or picking one from the search box -- loads that
street's existing profile+map page into an iframe directly below, so the whole
corpus written by `plot_street_bokeh.py --all` becomes browsable from one entry
point.

Nothing here regenerates a street page: the pages are consumed as-is, keyed by
the `file` column of the index that `--all` writes. By default the overview
lands at Stormdrain_map/index.html and the pages in Stormdrain_map/streets/,
and the iframe srcs are relative paths worked out from those two locations.

Usage:
    python plot_street_bokeh.py --all        # once, to build the pages
    python plot_city_overview.py             # then this
"""

import argparse
import os

import numpy as np
import pandas as pd
import xyzservices.providers as xyz
from bokeh.document import Document
from bokeh.events import DocumentReady
from bokeh.layouts import column
from bokeh.models import (AutocompleteInput, ColumnDataSource, CustomJS, Div,
                          HoverTool, Range1d, TapTool, WheelZoomTool)
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
    args = ap.parse_args()

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
                title="Livermore street centerline — tap a street to load its "
                      "profile and drainage map below")
    mp.add_tile(xyz.CartoDB.Positron)
    # City scale means a lot of zooming; make the wheel do it without a toolbar
    # trip. The per-street pages leave this off, where panning matters more.
    mp.toolbar.active_scroll = mp.select_one(WheelZoomTool)

    tips = [("street", "@name"), ("class", "@cls"),
            ("inlets", "@n_inlets"), ("sags", "@n_sags (@n_unserved unserved)"),
            ("street length", "@length_m{0,0} m")]
    srcs, hits = [], []
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
        label = f"{cls} ({len(r['xs']):,})"
        # Two renderers on ONE source: the visible line, and a fat transparent
        # one to catch the cursor -- a 1 px Local street is otherwise unclickable.
        # Both carry the same legend_label so a legend click hides the hit
        # target too, otherwise a hidden class would still be tappable.
        mp.multi_line("xs", "ys", source=src, line_color=CLASS_COLORS[cls],
                      line_width=CLASS_WIDTH.get(cls, 1.2), legend_label=label)
        hit = mp.multi_line("xs", "ys", source=src, line_width=HIT_W,
                            line_alpha=0.0, legend_label=label)
        srcs.append(src)
        hits.append(hit)
    mp.add_tools(HoverTool(renderers=hits, tooltips=tips, line_policy="nearest"))

    # Added last so it draws over every class. Zooming to a street is not much
    # use if you cannot tell which of the lines in view it is. No legend_label:
    # this is not a class, and it must not be hideable.
    hi = ColumnDataSource(dict(xs=[], ys=[]))
    mp.multi_line("xs", "ys", source=hi, line_color="#e00000", line_width=5,
                  line_alpha=0.8, line_cap="round")

    mp.legend.click_policy = "hide"
    mp.legend.label_text_font_size = "8pt"
    mp.legend.background_fill_alpha = 0.85

    # ---------------- widgets ----------------
    names = sorted(bbox)
    search = AutocompleteInput(
        completions=names, search_strategy="includes", case_sensitive=False,
        min_characters=2, width=380, placeholder=f"search {len(names):,} streets")
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
    doc.add_root(column(search, pick, mp, frame))
    doc.js_on_event(DocumentReady, ready)
    output_file(out, title="Livermore stormdrain — street index", mode="cdn")
    save(doc)
    n_seg = sum(len(s.data["xs"]) for s in srcs)
    print(f"{n_seg:,} segments over {len(names):,} streets -> {out}  "
          f"({os.path.getsize(out)/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
