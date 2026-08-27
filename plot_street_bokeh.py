"""Interactive linked profile + map per street, using Bokeh.

Two panels sharing one ColumnDataSource over the chained path points:

  top    elevation profile (x = chainage from the street's W or N end)
  bottom plan view on Esri Gray Canvas tiles, in Web Mercator

Because both panels are built from the SAME ordered array of path points, the
hovered index is a shared key -- hovering either panel moves a crosshair on the
other with no spatial search. Box-select on either panel highlights the same
stretch on both, which comes free from the shared source.

Everything is CustomJS, so the output is a standalone HTML file: no bokeh
server, no build step. Tiles are fetched from CARTO when the page opens, so
viewing needs a network connection.

Given a Maps Embed API key, tapping an inlet also loads its Street View pano
into a pane beside the map instead of only offering a link out.

Usage:
    python plot_street_bokeh.py --street "CONCANNON BL"
    python plot_street_bokeh.py --random 20 --seed 3
    python plot_street_bokeh.py --all --sv-key AIza...
    python plot_street_bokeh.py --all --smooth 10 --outdir Stormdrain_map/streets_10m
"""

import argparse
import os
import re

import numpy as np
import pandas as pd
import xyzservices.providers as xyz
from bokeh.layouts import column, row
from bokeh.models import (Arrow, BoxSelectTool, ColumnDataSource, CustomJS,
                          Div, HoverTool, LabelSet, Range1d, Span, TapTool,
                          VeeHead)
from bokeh.plotting import figure, output_file, save
from pyproj import Transformer

from plot_street_drains import (DATUM_SHIFT_M, DEFAULT_STYLE, STYLE,
                                load_inlets, prepare)
from extract_centerline_latlon import points_path
from plot_street_profiles import SMOOTH_M, safe_name, sample_step

POINTS = points_path()
INLETS = "derived/storm_inlets.csv"
OUTDIR = "Stormdrain_map/streets"
# The profile is the panel the page is read for, and at 320 px a Livermore
# street -- tens of metres of relief over kilometres -- was drawn nearly flat.
# Height only: PANEL_W is pinned by the overview's iframe, which embeds these
# pages at FRAME_W and would gain a horizontal scrollbar if this grew.
PROF_H, MAP_H, PANEL_W = 480, 560, 1180
PROF_ARROW_FRAC = 0.16    # arrow length as a fraction of the visible y-range
MAP_ARROW_FRAC = 0.10
SV_W = 440                # embedded Street View panel, when a key is supplied


def bearing(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing 1 -> 2, degrees clockwise from north."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dl = np.radians(np.asarray(lon2) - np.asarray(lon1))
    y = np.sin(dl)*np.cos(p2)
    x = np.cos(p1)*np.sin(p2) - np.sin(p1)*np.cos(p2)*np.cos(dl)
    return (np.degrees(np.arctan2(y, x)) + 360.0) % 360.0


def draw_stride(d, run, draw_spacing):
    """Indices to embed for DRAWING, thinned to about `draw_spacing` metres.

    Analysis is untouched -- see the module note. The first and last points are
    always kept so the line still spans the street, and a spacing of 0 (or a
    corpus already coarser than the target) keeps everything.
    """
    if not draw_spacing or draw_spacing <= 0:
        return np.arange(len(d))
    stride = int(round(draw_spacing / sample_step(d, run)))
    if stride <= 1:
        return np.arange(len(d))
    keep = np.arange(0, len(d), stride)
    if keep[-1] != len(d) - 1:
        keep = np.r_[keep, len(d) - 1]
    return keep


def build(street, st, inlets, args, outdir, used):
    p = prepare(st, inlets, args)
    d, z, sm = p["d"], p["z"], p["smooth"]
    e, n, near = p["e"], p["n"], p["near"]

    # With a key the map shares its row with a live Street View iframe, so it
    # gives up that width; without one it keeps the full panel as before. A
    # street with no inlets has nothing to tap, so it keeps the full width too
    # rather than reserving space for a pane that can never fill.
    embed_sv = bool(args.sv_key) and not near.empty
    map_w = PANEL_W - SV_W - 10 if embed_sv else PANEL_W

    to3857 = Transformer.from_crs("EPSG:26910", "EPSG:3857", always_xy=True)
    to4326 = Transformer.from_crs("EPSG:26910", "EPSG:4326", always_xy=True)
    mx, my = to3857.transform(e, n)
    lon, lat = to4326.transform(e, n)

    # Thin ONLY what gets drawn. lat/lon/d/z stay full-resolution locals below,
    # because the inlet snapping indexes them with near.path_idx.
    k = draw_stride(d, p["run"], args.draw_spacing)
    p["drawn"] = k
    src = ColumnDataSource(dict(d=d[k], z=z[k], sm=sm[k], mx=mx[k], my=my[k],
                                lon=lon[k], lat=lat[k]))
    cur = ColumnDataSource(dict(d=[d[0]], z=[z[0]], mx=[mx[0]], my=[my[0]]))

    # ---------------- profile ----------------
    # Explicit ranges, not autoscale: the arrow tails are recomputed from the
    # visible range, so the initial range has to be known here in Python too.
    ylo, yhi = float(np.nanmin(z)), float(np.nanmax(z))
    if not near.empty:
        for col in ("grate_m", "invert_m"):
            v = near[col].to_numpy(dtype=float)
            v = v[np.isfinite(v)]
            if v.size:
                ylo, yhi = min(ylo, v.min()), max(yhi, v.max())
    ypad = max(0.06*(yhi - ylo), 0.15)
    prof = figure(width=PANEL_W, height=PROF_H, tools="pan,wheel_zoom,box_zoom,reset",
                  x_axis_label=f"distance along street from {p['origin']} end (m)",
                  y_axis_label="elevation (m, NAVD88)",
                  x_range=Range1d(-0.02*float(d[-1]), 1.02*float(d[-1])),
                  y_range=Range1d(ylo - ypad, yhi + ypad),
                  # The smoothing window is in the title because two sets of
                  # these pages now exist side by side, and the sag markers
                  # below differ between them -- see prepare().
                  title=f"{street} — elevation profile "
                        f"({len(near)} inlets within {args.max_offset:g} m, "
                        f"{args.smooth:g} m smoothing)")
    prof.line("d", "z", source=src, line_color="#b6c4d2", line_width=1)
    line_sm = prof.line("d", "sm", source=src, line_color="#33475b",
                        line_width=2)
    prof.scatter("d", "z", source=cur, size=11, marker="circle",
                 fill_color="#e00000", line_color="white", line_width=1.5)
    vline = Span(location=float(d[0]), dimension="height",
                 line_color="#e00000", line_dash="dashed", line_width=1.2)
    prof.add_layout(vline)
    prof.add_tools(BoxSelectTool())

    # ---------------- map ----------------
    pad = float(np.clip(args.pad_frac*max(mx.max()-mx.min(), my.max()-my.min()),
                        args.pad_min_m, args.pad_m))
    mp = figure(width=map_w, height=MAP_H, match_aspect=True,
                tools="pan,wheel_zoom,box_zoom,reset",
                x_range=Range1d(mx.min()-pad, mx.max()+pad),
                y_range=Range1d(my.min()-pad, my.max()+pad),
                x_axis_type="mercator", y_axis_type="mercator",
                title=f"{street} — plan view (hover either panel to link)")
    # Esri.WorldGrayCanvas, not CartoDB.Positron. CARTO began requiring an API
    # key for its raster basemaps around 2026-08-25/26 and now serves
    # unauthenticated requests as a valid PNG watermarked "API KEY REQUIRED" --
    # HTTP 200, no error, so the page just quietly renders a spoiled map. These
    # tiles are fetched by the BROWSER on every page open, so every existing
    # page was affected the moment CARTO flipped it, build date irrelevant.
    # Gray Canvas is key-free and plays the same muted-backdrop role.
    mp.add_tile(xyz.Esri.WorldGrayCanvas)
    mp.line("mx", "my", source=src, line_color="#12263a", line_width=2)
    # Fat transparent line as the hover target. A scatter target fails here:
    # at 3 m spacing the markers are sub-pixel on screen, so one hover lands on
    # a dozen of them and the tooltip becomes a stack of near-identical rows.
    map_hit = mp.line("mx", "my", source=src, line_width=14, line_alpha=0.0)
    mp.scatter("mx", "my", source=cur, size=13, marker="circle",
               fill_color="#e00000", line_color="white", line_width=1.5)
    mp.add_tools(BoxSelectTool())

    # ---------------- inlets on both panels ----------------
    inlet_srcs, tap_prof, tap_map = [], [], []
    if not near.empty:
        ix, iy = to3857.transform(near.x.to_numpy(), near.y.to_numpy())
        # Street View opens looking from the roadway towards the inlet, so the
        # pano lands on the curb rather than facing off down the street. The
        # camera sits on the centerline, hence the bearing from the snapped
        # path point out to the inlet's own position. Not "head" -- that name
        # collides with DataFrame.head on the groupby subsets below.
        pi = near.path_idx.to_numpy()
        near = near.assign(sv_head=bearing(lat[pi], lon[pi],
                                           near.lat.to_numpy(),
                                           near.lon.to_numpy()))
        for t, g in near.groupby("type"):
            sty = STYLE.get(t, DEFAULT_STYLE)
            m = near.type.to_numpy() == t
            gy = np.where(np.isfinite(g.grate_m.to_numpy()),
                          g.grate_m.to_numpy(), g.dem_m.to_numpy())
            isrc = ColumnDataSource(dict(
                ch=g.chainage.to_numpy(), gy=gy,
                mx=ix[m], my=iy[m], num=g.num.to_numpy(),
                asset=g.AssetID.astype(str).to_numpy(),
                iv=g.invert_m.to_numpy(),
                off=g.offset_m.to_numpy(),
                lat=g.lat.to_numpy(), lon=g.lon.to_numpy(),
                hd=g["sv_head"].to_numpy(),
                lbl=[str(int(v)) for v in g.num.to_numpy()],
                typ=[t]*int(m.sum())))
            mk = {"o": "circle", "s": "square", "D": "diamond",
                  "P": "plus", "^": "triangle", "X": "x",
                  "v": "inverted_triangle"}.get(sty["marker"], "circle")
            # nonselection_alpha=1: tapping an inlet selects it, and Bokeh's
            # default is to fade everything unselected. Here the tap is only a
            # way to pin details, so the other inlets must stay legible.
            r1 = prof.scatter("ch", "gy", source=isrc, marker=mk, size=9,
                              fill_color=sty["color"], line_color="white",
                              nonselection_alpha=1.0, legend_label=t)
            r2 = mp.scatter("mx", "my", source=isrc, marker=mk, size=11,
                            fill_color=sty["color"], line_color="white",
                            nonselection_alpha=1.0, legend_label=t)
            inlet_srcs.append(isrc)
            tap_prof.append(r1)
            tap_map.append(r2)
            # lat/lon here is the inlet's own surveyed position, not the
            # centerline point it snapped to -- they differ by @off metres.
            tips = [("inlet", "#@num  @asset"), ("type", "@typ"),
                    ("chainage", "@ch{0.0} m"), ("grate", "@gy{0.00} m"),
                    ("invert", "@iv{0.00} m"), ("offset", "@off{0.0} m"),
                    ("lat, lon", "@lat{0.000000}, @lon{0.000000}")]
            # Inlet markers sit on the profile line, so this hover and the
            # path hover both fire and their boxes would stack -- the taller
            # inlet box ends up with its middle rows hidden. Flanking the
            # cursor (markers right, path left) keeps both fully readable.
            prof.add_tools(HoverTool(renderers=[r1], tooltips=tips,
                                     attachment="right"))
            mp.add_tools(HoverTool(renderers=[r2], tooltips=tips,
                                   attachment="right"))
            for f, xf, yf, dx, dy in ((prof, "ch", "gy", 0, 9),
                                      (mp, "mx", "my", 7, 6)):
                f.add_layout(LabelSet(x=xf, y=yf, text="lbl", source=isrc,
                                      x_offset=dx, y_offset=dy,
                                      text_font_size="8pt",
                                      text_color="#111111",
                                      background_fill_color="white",
                                      background_fill_alpha=0.55))

    # ---------------- sags ----------------
    for items, colour, tag in ((p["marked"], "#b8002e", "sag (served)"),
                               (p["unserved"], "#c76a00", "sag (no inlet)")):
        if not items:
            continue
        ch = [it.get("chainage", it.get("sag_chainage")) for it in items]
        el = [it.get("elev", it.get("sag_elev")) for it in items]
        sx, sy = to3857.transform(
            *zip(*[(float(np.interp(c, d, e)), float(np.interp(c, d, n)))
                   for c in ch]))
        # Arrow tails live in data space, so their on-screen length would grow
        # with zoom. A callback on each range resets them to a fixed fraction of
        # what is visible, which keeps the arrow a constant size on screen.
        pl0 = PROF_ARROW_FRAC * (prof.y_range.end - prof.y_range.start)
        ml0 = MAP_ARROW_FRAC * (mp.y_range.end - mp.y_range.start)
        snum = [it["sag_num"] for it in items]
        prom = [it["prominence"] for it in items]
        ssrc = ColumnDataSource(dict(
            ch=ch, el=el, mx=list(sx), my=list(sy), lab=[tag]*len(ch),
            snum=snum, prom=prom,
            pys=[v - pl0 for v in el],
            mxs=[v + 0.9*ml0 for v in sx], mys=[v + ml0 for v in sy]))
        sr1 = prof.scatter("ch", "el", source=ssrc, marker="inverted_triangle",
                           size=13, fill_color=colour, line_color="white",
                           legend_label=tag)
        sr2 = mp.scatter("mx", "my", source=ssrc, marker="inverted_triangle",
                         size=14, fill_color=colour, line_color="white",
                         legend_label=tag)
        stips = [("sag", "@snum"), ("status", "@lab"),
                 ("chainage", "@ch{0.0} m"), ("prominence", "@prom{0.00} m")]
        prof.add_tools(HoverTool(renderers=[sr1], tooltips=stips,
                                 attachment="right"))
        mp.add_tools(HoverTool(renderers=[sr2], tooltips=stips,
                               attachment="right"))
        prof.add_layout(Arrow(
            x_start="ch", y_start="pys", x_end="ch", y_end="el", source=ssrc,
            line_color=colour, line_width=2,
            end=VeeHead(size=11, fill_color=colour, line_color=colour)))
        mp.add_layout(Arrow(
            x_start="mxs", y_start="mys", x_end="mx", y_end="my", source=ssrc,
            line_color=colour, line_width=2,
            end=VeeHead(size=11, fill_color=colour, line_color=colour)))
        pcb = CustomJS(args=dict(s=ssrc, r=prof.y_range, f=PROF_ARROW_FRAC),
                       code="""
            const L = f*(r.end - r.start), D = s.data;
            for (let i = 0; i < D.el.length; i++) { D.pys[i] = D.el[i] - L; }
            s.change.emit();
        """)
        mcb = CustomJS(args=dict(s=ssrc, r=mp.y_range, f=MAP_ARROW_FRAC), code="""
            const L = f*(r.end - r.start), D = s.data;
            for (let i = 0; i < D.my.length; i++) {
                D.mxs[i] = D.mx[i] + 0.9*L; D.mys[i] = D.my[i] + L;
            }
            s.change.emit();
        """)
        for prop in ("start", "end"):
            prof.y_range.js_on_change(prop, pcb)
            mp.y_range.js_on_change(prop, mcb)

    for f in (prof, mp):
        if not f.legend:          # streets with no inlets and no sags
            continue
        f.legend.click_policy = "hide"
        f.legend.label_text_font_size = "8pt"
        f.legend.background_fill_alpha = 0.85

    # ---------------- the link ----------------
    read = Div(text="<i>hover either panel</i>", width=PANEL_W,
               styles={"font-family": "monospace", "font-size": "13px",
                       "padding": "4px 0"})
    LINK_JS = """
        // Point glyphs report hits in .indices; LINE glyphs use .line_indices.
        // The profile hovers a line, the map hovers a scatter, so accept both.
        const ix = cb_data.index;
        let i = -1;
        if (ix.indices && ix.indices.length) { i = ix.indices[0]; }
        else if (ix.line_indices && ix.line_indices.length) { i = ix.line_indices[0]; }
        if (i < 0) { return; }
        const H = SRC.data;
        cur.data = {d:[H.d[i]], z:[H.z[i]], mx:[H.mx[i]], my:[H.my[i]]};
        cur.change.emit();
        vline.location = H.d[i];
        read.text = "<b>" + H.d[i].toFixed(0) + " m</b> along  |  elev <b>"
                  + H.z[i].toFixed(2) + " m</b>  |  smoothed " + H.sm[i].toFixed(2)
                  + " m  |  <b>" + H.lat[i].toFixed(6) + ", " + H.lon[i].toFixed(6) + "</b>";
    """
    cb_prof = CustomJS(args=dict(SRC=src, cur=cur, vline=vline, read=read),
                       code=LINK_JS)
    cb_map = CustomJS(args=dict(SRC=src, cur=cur, vline=vline, read=read),
                      code=LINK_JS)
    path_tips = [("chainage", "@d{0.0} m"), ("elev", "@z{0.00} m"),
                 (f"smoothed ({args.smooth:g} m)", "@sm{0.00} m"),
                 ("lat, lon", "@lat{0.000000}, @lon{0.000000}")]
    # vline mode on a scatter returns every point under the cursor, which
    # stacks five near-identical rows once zoomed in. Hovering the line with
    # line_policy="nearest" resolves to exactly one.
    prof.add_tools(HoverTool(renderers=[line_sm], tooltips=path_tips,
                             line_policy="nearest", mode="vline",
                             attachment="left", callback=cb_prof))
    mp.add_tools(HoverTool(renderers=[map_hit], tooltips=path_tips,
                           line_policy="nearest", attachment="left",
                           callback=cb_map))

    # ---------------- tap an inlet -> pinned panel with a Street View link ---
    # A link inside a hover tooltip is unreachable: the tooltip tracks the
    # cursor and is torn down on mouseleave, so it cannot be moused into. The
    # panel below is an ordinary Div in the layout, so its anchor behaves like
    # any other link -- and it must be its OWN Div, because `read` is rewritten
    # on every hover and moving towards a link crosses the plots.
    # Two points up on the hover readout above it: this line is read carefully
    # -- asset ID, elevations, and the Street View link -- while the hover line
    # is glanced at and replaced on every mouse move.
    pick = Div(text="<i>tap an inlet marker for its Street View link</i>",
               width=PANEL_W,
               styles={"font-family": "monospace", "font-size": "15px",
                       "padding": "4px 0", "min-height": "22px"})

    # ---------------- embedded Street View ----------------
    # Maps Embed API: a plain iframe, free and unmetered, but it needs a key.
    # The iframe is only written on the first tap, so a page that is opened and
    # never clicked costs one nothing and loads no Google resources at all.
    sv = None
    if embed_sv:
        # Explicit px, not 100%: the Div host does not pass its height down, so
        # a percentage placeholder collapses to a one-line box in the corner.
        sv = Div(text=f"<div style='width:{SV_W}px;height:{MAP_H - 4}px;"
                      "display:flex;align-items:center;justify-content:center;"
                      "background:#eef1f4;color:#667;font:13px monospace;"
                      "border:1px solid #d5dade'>tap an inlet</div>",
                 width=SV_W, height=MAP_H, disable_math=True)

    if inlet_srcs:
        TAP_JS = """
            // One TapTool per figure covers every inlet type, so find which
            // source actually took the hit. Tapping empty space clears them
            // all and we return, leaving the last pick pinned.
            let D = null, i = -1;
            for (const s of S) {
                const sel = s.selected.indices;
                if (sel && sel.length) { D = s.data; i = sel[0]; break; }
            }
            if (D === null) { return; }
            const la = D.lat[i], lo = D.lon[i];
            const url = "https://www.google.com/maps/@?api=1"
                      + "&map_action=pano&viewpoint=" + la.toFixed(6) + ","
                      + lo.toFixed(6) + "&heading=" + D.hd[i].toFixed(0);
            const iv = isFinite(D.iv[i]) ? D.iv[i].toFixed(2) + " m" : "n/a";
            pick.text =
                "<b>inlet #" + D.num[i] + "</b> &middot; " + D.asset[i]
              + " &middot; " + D.typ[i] + " &middot; " + D.ch[i].toFixed(1)
              + " m along &middot; grate " + D.gy[i].toFixed(2)
              + " m &middot; invert " + iv + " &middot; " + D.off[i].toFixed(1)
              + " m off centerline<br>" + la.toFixed(6) + ", " + lo.toFixed(6)
              + " &nbsp;&rarr;&nbsp; <a href='" + url + "' target='_blank'"
              + " rel='noopener'>open Street View</a>"
              + " <span style='color:#777'>(facing the inlet from the road)"
              + "</span>";
        """
        tap_args = dict(S=inlet_srcs, pick=pick)
        if sv is not None:
            tap_args["sv"] = sv
            # Rewriting .text rebuilds the iframe, which is what reloads the
            # pano -- mutating a src attribute in place is not an option here,
            # since Bokeh renders widgets into a shadow root that getElementById
            # cannot reach.
            TAP_JS += """
            sv.text =
                "<iframe width='%d' height='%d' style='border:0' loading='lazy'"
              + " allowfullscreen referrerpolicy='no-referrer-when-downgrade'"
              + " src='https://www.google.com/maps/embed/v1/streetview?key=%s"
              + "&location=" + la.toFixed(6) + "," + lo.toFixed(6)
              + "&heading=" + D.hd[i].toFixed(0) + "&pitch=0&fov=90'></iframe>";
            """ % (SV_W, MAP_H - 4, args.sv_key)
        for f, rs in ((prof, tap_prof), (mp, tap_map)):
            tt = TapTool(renderers=rs, callback=CustomJS(
                args=tap_args, code=TAP_JS))
            f.add_tools(tt)
            # active_tap="auto" leaves the gesture unbound on these figures --
            # the toolbar was built from an explicit tools= string, so the tap
            # slot stays null and clicks never reach the tool. Bind it.
            f.toolbar.active_tap = tt

    out = os.path.join(outdir, safe_name(street, used) + ".html")
    output_file(out, title=f"{street} — profile + map", mode="cdn")
    plan = row(mp, sv) if sv is not None else mp
    save(column(*([read, pick] if inlet_srcs else [read]), prof, plan))
    return out, p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--street", default="CONCANNON BL")
    ap.add_argument("--all", action="store_true",
                    help="every street in the network")
    ap.add_argument("--random", type=int, default=0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--outdir", default=OUTDIR)
    ap.add_argument("--max-offset", type=float, default=30.0)
    # Moves both the drawn profile and the sags, so a second window means a
    # second output directory rather than an overwrite of the first.
    # Default 0 -- every point is embedded. The pages are ~4x heavier for it
    # (0.31 GB vs 83 MB across the corpus) and the extra detail is below one
    # screen pixel, but the raw samples stay inspectable in the page, which is
    # what this project wants. Pass a spacing in metres to thin the DRAWN line;
    # sags, inlets and the rolling mean are computed on every point regardless.
    ap.add_argument("--draw-spacing", type=float, default=0.0,
                    help="thin the embedded polyline to roughly this spacing "
                         "(m) for DRAWING only; sags, inlets and the rolling "
                         "mean are still computed on every point. "
                         "0 = keep all points (default)")
    ap.add_argument("--smooth", type=float, default=SMOOTH_M,
                    help=f"rolling-mean window (m) (default {SMOOTH_M:g})")
    ap.add_argument("--sag-prom", type=float, default=0.20)
    ap.add_argument("--sag-sep", type=float, default=10.0)
    ap.add_argument("--sag-window", type=float, default=60.0)
    ap.add_argument("--sag-edge", type=float, default=10.0)
    ap.add_argument("--sag-tol", type=float, default=0.20)
    ap.add_argument("--pad-m", type=float, default=250.0)
    ap.add_argument("--pad-min-m", type=float, default=60.0)
    ap.add_argument("--pad-frac", type=float, default=0.6)
    ap.add_argument("--no-datum-shift", dest="datum_shift", action="store_false")
    # Maps Embed API key. Without one the pages behave as before: a link out to
    # Street View rather than a panel beside the map. The key is baked into
    # every page it generates, so restrict it to the Maps Embed API in the
    # Cloud console -- HTTP referrer restrictions cannot cover file:// pages.
    ap.add_argument("--sv-key", default=os.environ.get("GOOGLE_MAPS_EMBED_KEY"),
                    help="Google Maps Embed API key; enables the inline "
                         "Street View panel (env: GOOGLE_MAPS_EMBED_KEY)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    outdir = os.path.join(here, args.outdir)
    os.makedirs(outdir, exist_ok=True)

    inlets = load_inlets(os.path.join(here, INLETS),
                         DATUM_SHIFT_M if args.datum_shift else 0.0)
    tr = Transformer.from_crs("EPSG:4326", "EPSG:26910", always_xy=True)
    inlets["x"], inlets["y"] = tr.transform(inlets.lon.values, inlets.lat.values)

    df = pd.read_parquet(os.path.join(here, POINTS))
    if args.all:
        names = sorted(df.FullStreetName.unique())
    elif args.random:
        cand = []
        for nm in sorted(df.FullStreetName.unique()):
            sub = df[df.FullStreetName == nm]
            bx = (inlets.x.between(sub.easting.min()-30, sub.easting.max()+30)
                  & inlets.y.between(sub.northing.min()-30, sub.northing.max()+30))
            if bx.sum() >= 3:
                cand.append(nm)
        rng = np.random.default_rng(args.seed)
        names = list(rng.choice(cand, size=min(args.random, len(cand)),
                                replace=False))
    else:
        names = [args.street]

    used, rows, failed = {}, [], 0
    for i, nm in enumerate(names, 1):
        st = df[df.FullStreetName == nm]
        if st.empty:
            print(f"  no points for {nm!r}")
            continue
        try:
            out, p = build(nm, st, inlets, args, outdir, used)
        except Exception as exc:                                # noqa: BLE001
            failed += 1
            print(f"  FAILED {nm!r}: {type(exc).__name__}: {exc}", flush=True)
            continue
        rows.append(dict(FullStreetName=nm, file=os.path.basename(out),
                         n_points=len(st), n_inlets=len(p["near"]),
                         n_sags=len(p["sags"]), n_served=len(p["marked"]),
                         n_unserved=len(p["unserved"]),
                         length_m=round(float(p["d"][-1]), 1),
                         kb=round(os.path.getsize(out)/1e3, 1)))
        if args.all:
            if i % 100 == 0:
                print(f"  {i}/{len(names)} processed", flush=True)
        else:
            print(f"wrote {os.path.basename(out):<28} "
                  f"{len(st):>7,}->{len(p['drawn']):>6,} pts  "
                  f"{len(p['near']):>3} inlets  "
                  f"{len(p['marked'])} sag  {len(p['unserved'])} unserved  "
                  f"{os.path.getsize(out)/1e3:>6.0f} KB")
    if args.all:
        idx = pd.DataFrame(rows).sort_values("n_inlets", ascending=False)
        ipath = os.path.join(outdir, "_index.csv")
        idx.to_csv(ipath, index=False)
        print(f"wrote {len(rows):,} pages to {outdir}/  "
              f"({idx.kb.sum()/1e3:.0f} MB total)")
        print(f"  {int((idx.n_inlets > 0).sum()):,} with inlets, "
              f"{int(idx.n_sags.sum()):,} sags, "
              f"{int(idx.n_unserved.sum()):,} unserved")
        if failed:
            print(f"  FAILED {failed}")
        print(f"index: {ipath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
