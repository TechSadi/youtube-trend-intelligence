"""Render the trend report: matplotlib charts + email-safe HTML.

Deterministic. The *narrative* comes from a report spec the agent authors
(.tmp/report_spec.json); this tool only lays it out. That split is deliberate —
judgment is written once by the agent, presentation is reproducible code.

Email constraints that drive every choice here:
  - No JavaScript, so charts are PNGs, not Chart.js.
  - No external CSS and no <style> reliability, so every rule is inline.
  - Tables for layout, 600px wide — still the only thing that survives Outlook.
  - Light surface only; email clients do not honour prefers-color-scheme well.

Images are referenced as cid: for the real email. A parallel preview file using
plain paths is written alongside, since a browser cannot resolve cid:.

Palette: the dataviz reference instance, light column. Validated with
scripts/validate_palette.js — the 3-slot scatter palette passes --pairs all, the
5-slot line palette passes on the adjacent pairlist. Three light slots fall below
3:1 on the surface, so the relief rule applies and every series is direct-labeled.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import pathlib
import sys
import webbrowser

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import emit, fail, get_logger, read_json, tmp_path, write_json

log = get_logger(__name__)

# --- dataviz reference palette, light column -----------------------------
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#8a8985"
GRID = "#e6e5e1"

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
DIVERGE_POS = "#2a78d6"   # rising
DIVERGE_NEG = "#e34948"   # cooling
NEUTRAL = "#f0efec"       # diverging midpoint
SEQ_BLUE = "#2a78d6"

DPI = 200  # 2x for retina; the HTML sets an explicit display width

plt.rcParams.update({
    "font.family": ["DejaVu Sans"],
    "font.size": 9,
    "axes.facecolor": SURFACE,
    "figure.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.edgecolor": GRID,
    "axes.labelcolor": INK_2,
    "text.color": INK,
    "xtick.color": INK_2,
    "ytick.color": INK_2,
    "axes.spines.top": False,
    "axes.spines.right": False,
})


def _thousands(value, _pos=None) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return f"{value:.0f}"


def _save(fig, path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI, bbox_inches="tight", pad_inches=0.18)
    plt.close(fig)
    return path


# =========================================================================
# Charts
# =========================================================================

def chart_momentum(analysis: dict, path: pathlib.Path, *, top: int = 12) -> pathlib.Path:
    """Diverging bar: which technologies are accelerating, which are fading.

    Diverging data gets a diverging encoding — two poles, gray midpoint, one
    axis. Ranked by rate of change so the chart does not just restate volume.
    """
    rising = [r for r in analysis["by_momentum"] if r["momentum_pct"] > 0][: top // 2 + top % 2]
    cooling = [r for r in analysis["cooling"] if r["momentum_pct"] < 0][: top // 2]
    rows = sorted(rising + cooling, key=lambda r: r["momentum_pct"])
    if not rows:
        return _empty(path, "Not enough data for momentum")

    labels = [r["name"] for r in rows]
    values = [r["momentum_pct"] for r in rows]
    colors = [DIVERGE_POS if v > 0 else DIVERGE_NEG for v in values]

    fig, ax = plt.subplots(figsize=(7.2, 0.34 * len(rows) + 0.9))
    bars = ax.barh(labels, values, color=colors, height=0.62,
                   edgecolor=SURFACE, linewidth=2)  # 2px surface gap between bars

    span = max(abs(min(values)), abs(max(values))) or 1
    for bar, value, row in zip(bars, values, rows):
        offset = span * 0.03
        ax.text(value + (offset if value >= 0 else -offset),
                bar.get_y() + bar.get_height() / 2,
                f"{value:+.0f}%" + ("  new" if row.get("status") == "new" else ""),
                va="center", ha="left" if value >= 0 else "right",
                fontsize=8.5, color=INK_2)

    ax.axvline(0, color=MUTED, linewidth=1)
    ax.set_xlim(-span * 1.38, span * 1.38)
    ax.set_xlabel("Change in share of videos, recent half vs. prior half of window", fontsize=8.5)
    ax.set_xticks([])
    ax.spines["bottom"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0, labelsize=9)
    ax.set_axisbelow(True)
    return _save(fig, path)


def chart_signal_vs_hype(analysis: dict, path: pathlib.Path, *, top: int = 40) -> pathlib.Path:
    """Scatter: YouTube attention against real-world adoption.

    Three categories only — the palette's first three slots are the ones that
    validate on the all-pairs list, which is what a scatter needs. Every point
    is direct-labeled, which also satisfies the contrast relief rule.
    """
    rows = [r for r in analysis["technologies"] if "adoption_index" in r][:top]
    if not rows:
        return _empty(path, "Run the HN and GitHub fetchers to enable this chart")

    groups = {
        "Hype-leaning": (SERIES[1], [r for r in rows if r["verdict"] == "hype-leaning"]),
        "Aligned": (SERIES[0], [r for r in rows if r["verdict"] == "aligned"]),
        "Under-the-radar": (SERIES[2], [r for r in rows if r["verdict"] == "under-the-radar"]),
    }

    fig, ax = plt.subplots(figsize=(7.2, 5.6))
    ax.plot([0, 100], [0, 100], color=GRID, linewidth=1.4, linestyle=(0, (4, 3)), zorder=1)
    ax.text(97, 92, "parity", fontsize=8, color=MUTED, ha="right", style="italic")

    for label, (color, items) in groups.items():
        if not items:
            continue
        ax.scatter([r["attention_index"] for r in items], [r["adoption_index"] for r in items],
                   s=88, color=color, edgecolor=SURFACE, linewidth=2,  # 2px ring on overlap
                   label=f"{label} ({len(items)})", zorder=3)

    annotations = [
        ax.annotate(row["name"], (row["attention_index"], row["adoption_index"]),
                    textcoords="offset points", xytext=(0, 9), ha="center",
                    fontsize=7.8, color=INK_2, zorder=4)
        for row in rows
    ]
    _declutter_labels(fig, ax, annotations)

    ax.set_xlabel("YouTube attention index", fontsize=9)
    ax.set_ylabel("Adoption index  (HN discussion + new GitHub repos)", fontsize=9)
    ax.set_xlim(-6, 112)
    ax.set_ylim(-6, 112)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    legend = ax.legend(loc="upper left", frameon=True, fontsize=8.5, borderpad=0.7)
    legend.get_frame().set_edgecolor(GRID)
    legend.get_frame().set_facecolor("#ffffff")
    return _save(fig, path)


def chart_timeline(analysis: dict, path: pathlib.Path, *, series: int = 5) -> pathlib.Path:
    """Weekly mention counts for the leading technologies. Lines + end labels."""
    weeks = analysis.get("weeks", [])
    rows = analysis["technologies"][:series]
    if len(weeks) < 3 or not rows:
        return _empty(path, "Not enough weeks in the window to chart a trend")

    fig, ax = plt.subplots(figsize=(7.2, 3.9))
    end_labels = []
    for index, row in enumerate(rows):
        counts = [row["weekly_counts"].get(week, 0) for week in weeks]
        color = SERIES[index % len(SERIES)]
        ax.plot(range(len(weeks)), counts, color=color, linewidth=2,
                marker="o", markersize=4.5, markeredgecolor=SURFACE, markeredgewidth=1.4,
                label=row["name"], zorder=3)
        # Direct end label — identity never rests on colour alone.
        end_labels.append(ax.annotate(
            row["name"], (len(weeks) - 1, counts[-1]),
            textcoords="offset points", xytext=(7, 0), va="center",
            fontsize=8, color=INK_2))
    _declutter_labels(fig, ax, end_labels)

    ax.set_xticks(range(len(weeks)))
    ax.set_xticklabels([w.split("-W")[1] for w in weeks], fontsize=8)
    ax.set_xlabel("ISO week", fontsize=8.5)
    ax.set_ylabel("Videos mentioning", fontsize=8.5)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))  # video counts are whole numbers
    ax.set_xlim(-0.4, len(weeks) - 1 + len(weeks) * 0.26)
    ax.grid(True, axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    legend = ax.legend(loc="upper left", frameon=False, fontsize=8, ncol=min(len(rows), 3))
    for text in legend.get_texts():
        text.set_color(INK_2)
    return _save(fig, path)


def chart_reach(analysis: dict, path: pathlib.Path, *, top: int = 12) -> pathlib.Path:
    """Single-series bar: median views/day of videos covering each technology.

    One series, so one hue and no legend — the title names it. Sorted by value;
    every bar is direct-labeled.
    """
    rows = sorted(analysis["technologies"][:20],
                  key=lambda r: r["median_views_per_day"], reverse=True)[:top]
    if not rows:
        return _empty(path, "No data")

    rows.reverse()
    labels = [r["name"] for r in rows]
    values = [r["median_views_per_day"] for r in rows]

    fig, ax = plt.subplots(figsize=(7.2, 0.34 * len(rows) + 0.8))
    ax.barh(labels, values, color=SEQ_BLUE, height=0.62, edgecolor=SURFACE, linewidth=2)

    for index, value in enumerate(values):
        ax.text(value + max(values) * 0.015, index, _thousands(value),
                va="center", fontsize=8.5, color=INK_2)

    ax.set_xlabel("Median views per day across that technology's videos", fontsize=8.5)
    ax.xaxis.set_major_formatter(FuncFormatter(_thousands))
    ax.set_xlim(0, max(values) * 1.16)
    ax.grid(True, axis="x", color=GRID, linewidth=0.8)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0, labelsize=9)
    ax.set_axisbelow(True)
    return _save(fig, path)


def _declutter_labels(fig, ax, annotations, *, passes: int = 60) -> None:
    """Nudge point labels apart until they stop overlapping.

    Scatter labels collide constantly, and an unreadable label is worse than a
    slightly displaced one. Each pass measures real rendered boxes and pushes
    the lower of any overlapping pair further away, alternating sides so labels
    do not all drift in one direction.
    """
    renderer = fig.canvas.get_renderer()
    for _ in range(passes):
        fig.canvas.draw()
        boxes = [(a, a.get_window_extent(renderer=renderer)) for a in annotations]
        moved = False
        for i, (ann_a, box_a) in enumerate(boxes):
            for ann_b, box_b in boxes[i + 1:]:
                if not box_a.overlaps(box_b):
                    continue
                moved = True
                # Push the one already lower further down, the other further up.
                low, high = (ann_b, ann_a) if box_b.y0 < box_a.y0 else (ann_a, ann_b)
                lx, ly = low.get_position()
                hx, hy = high.get_position()
                low.set_position((lx, ly - 3.2))
                high.set_position((hx, hy + 3.2))
        if not moved:
            return


def _empty(path: pathlib.Path, message: str) -> pathlib.Path:
    fig, ax = plt.subplots(figsize=(7.2, 1.6))
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=10, color=MUTED)
    ax.axis("off")
    return _save(fig, path)


CHARTS = {
    "momentum": chart_momentum,
    "signal_vs_hype": chart_signal_vs_hype,
    "timeline": chart_timeline,
    "reach": chart_reach,
}


# =========================================================================
# HTML
# =========================================================================

E = html.escape
CONTENT_W = 600
IMG_W = 536


def _p(text: str, size: int = 15, color: str = "#3a3a38", top: int = 0) -> str:
    return (f'<p style="margin:{top}px 0 14px;font-size:{size}px;line-height:1.62;'
            f'color:{color};">{text}</p>')


def _rich(text: str) -> str:
    """Escape, then re-enable **bold** and `code` — a tiny, safe subset."""
    import re
    out = E(text)
    out = re.sub(r"\*\*(.+?)\*\*", r'<strong style="color:#0b0b0b;">\1</strong>', out)
    out = re.sub(r"`(.+?)`",
                 r'<span style="font-family:Consolas,Menlo,monospace;font-size:13px;'
                 r'background:#f1f0ed;padding:1px 5px;border-radius:3px;">\1</span>', out)
    return out


def _heading(text: str) -> str:
    return (f'<h2 style="margin:34px 0 10px;font-size:19px;line-height:1.3;color:#0b0b0b;'
            f'font-weight:700;letter-spacing:-0.2px;">{E(text)}</h2>')


def _caption(text: str) -> str:
    return (f'<p style="margin:6px 0 0;font-size:12.5px;line-height:1.5;color:#8a8985;'
            f'font-style:italic;">{_rich(text)}</p>')


def section_text(spec: dict, _ctx: dict) -> str:
    parts = []
    if spec.get("heading"):
        parts.append(_heading(spec["heading"]))
    for paragraph in _as_list(spec.get("body")):
        parts.append(_p(_rich(paragraph)))
    if spec.get("bullets"):
        items = "".join(
            f'<li style="margin:0 0 9px;font-size:15px;line-height:1.6;color:#3a3a38;">{_rich(b)}</li>'
            for b in spec["bullets"]
        )
        parts.append(f'<ul style="margin:0 0 14px;padding-left:22px;">{items}</ul>')
    return "".join(parts)


def section_chart(spec: dict, ctx: dict) -> str:
    name = spec["chart"]
    src = ctx["img_src"](name)
    parts = []
    if spec.get("heading"):
        parts.append(_heading(spec["heading"]))
    for paragraph in _as_list(spec.get("body")):
        parts.append(_p(_rich(paragraph)))
    parts.append(
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:6px 0 0;"><tr><td align="center" style="padding:10px 0;">'
        f'<img src="{src}" width="{IMG_W}" alt="{E(spec.get("alt", name))}" '
        f'style="display:block;width:100%;max-width:{IMG_W}px;height:auto;border:0;'
        f'border-radius:6px;" /></td></tr></table>'
    )
    if spec.get("caption"):
        parts.append(_caption(spec["caption"]))
    return "".join(parts)


def section_stats(spec: dict, _ctx: dict) -> str:
    cells = []
    for item in spec["items"]:
        cells.append(
            f'<td width="33%" align="center" style="padding:16px 8px;background:#f7f7f5;'
            f'border-radius:8px;">'
            f'<div style="font-size:26px;line-height:1.1;font-weight:700;color:#2a78d6;">{E(str(item["value"]))}</div>'
            f'<div style="margin-top:5px;font-size:11.5px;line-height:1.35;color:#6b6a66;'
            f'text-transform:uppercase;letter-spacing:0.6px;">{E(item["label"])}</div></td>'
        )
        cells.append('<td width="10" style="width:10px;">&nbsp;</td>')
    if cells:
        cells.pop()
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:18px 0 6px;"><tr>{"".join(cells)}</tr></table>')


def section_cards(spec: dict, _ctx: dict) -> str:
    parts = []
    if spec.get("heading"):
        parts.append(_heading(spec["heading"]))
    for paragraph in _as_list(spec.get("body")):
        parts.append(_p(_rich(paragraph)))
    for item in spec["items"]:
        accent = item.get("accent", "#2a78d6")
        badge = ""
        if item.get("badge"):
            badge = (f'<span style="display:inline-block;margin-left:8px;padding:2px 8px;'
                     f'background:{accent}1a;color:{accent};font-size:11px;font-weight:700;'
                     f'border-radius:10px;letter-spacing:0.3px;">{E(item["badge"])}</span>')
        title = E(item["title"])
        if item.get("url"):
            title = f'<a href="{E(item["url"])}" style="color:#0b0b0b;text-decoration:none;">{title}</a>'
        parts.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:0 0 12px;"><tr>'
            f'<td style="padding:14px 16px;background:#f9f9f7;border-left:3px solid {accent};'
            f'border-radius:0 6px 6px 0;">'
            f'<div style="font-size:15.5px;font-weight:700;color:#0b0b0b;line-height:1.35;">{title}{badge}</div>'
            + (f'<div style="margin-top:3px;font-size:12.5px;color:#8a8985;">{E(item["subtitle"])}</div>'
               if item.get("subtitle") else "")
            + (f'<div style="margin-top:7px;font-size:14px;line-height:1.55;color:#3a3a38;">{_rich(item["body"])}</div>'
               if item.get("body") else "")
            + '</td></tr></table>'
        )
    return "".join(parts)


def section_videos(spec: dict, ctx: dict) -> str:
    parts = []
    if spec.get("heading"):
        parts.append(_heading(spec["heading"]))
    for paragraph in _as_list(spec.get("body")):
        parts.append(_p(_rich(paragraph)))

    for item in spec["items"]:
        thumb_src = ctx["thumb_src"](item.get("video_id", ""))
        thumb_cell = ""
        if thumb_src:
            thumb_cell = (
                f'<td width="132" style="width:132px;padding-right:13px;vertical-align:top;">'
                f'<a href="{E(item.get("url", "#"))}">'
                f'<img src="{thumb_src}" width="132" alt="" '
                f'style="display:block;width:132px;height:auto;border:0;border-radius:5px;" />'
                f'</a></td>'
            )
        meta = " · ".join(filter(None, [item.get("channel"), item.get("stat")]))
        parts.append(
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:0 0 15px;"><tr>{thumb_cell}'
            f'<td style="vertical-align:top;">'
            f'<div style="font-size:14.5px;font-weight:600;line-height:1.4;">'
            f'<a href="{E(item.get("url", "#"))}" style="color:#0b0b0b;text-decoration:none;">{E(item["title"])}</a></div>'
            f'<div style="margin-top:4px;font-size:12.5px;color:#8a8985;">{E(meta)}</div>'
            + (f'<div style="margin-top:6px;font-size:13.5px;line-height:1.5;color:#3a3a38;">{_rich(item["note"])}</div>'
               if item.get("note") else "")
            + '</td></tr></table>'
        )
    return "".join(parts)


def section_table(spec: dict, _ctx: dict) -> str:
    parts = []
    if spec.get("heading"):
        parts.append(_heading(spec["heading"]))
    for paragraph in _as_list(spec.get("body")):
        parts.append(_p(_rich(paragraph)))

    head = "".join(
        f'<th align="{"left" if i == 0 else "right"}" style="padding:9px 10px;font-size:11.5px;'
        f'text-transform:uppercase;letter-spacing:0.5px;color:#6b6a66;border-bottom:2px solid #e6e5e1;'
        f'font-weight:700;">{E(col)}</th>'
        for i, col in enumerate(spec["columns"])
    )
    body = ""
    for r_i, row in enumerate(spec["rows"]):
        bg = "#ffffff" if r_i % 2 == 0 else "#fafaf8"
        cells = "".join(
            f'<td align="{"left" if i == 0 else "right"}" style="padding:9px 10px;font-size:13.5px;'
            f'color:{"#0b0b0b" if i == 0 else "#3a3a38"};border-bottom:1px solid #f0efec;'
            f'{"font-weight:600;" if i == 0 else ""}">{_rich(str(cell))}</td>'
            for i, cell in enumerate(row)
        )
        body += f'<tr style="background:{bg};">{cells}</tr>'

    parts.append(
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:8px 0 6px;border-collapse:collapse;">'
        f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
    )
    if spec.get("caption"):
        parts.append(_caption(spec["caption"]))
    return "".join(parts)


SECTIONS = {
    "text": section_text,
    "chart": section_chart,
    "stats": section_stats,
    "cards": section_cards,
    "videos": section_videos,
    "table": section_table,
}


def _as_list(value) -> list[str]:
    if not value:
        return []
    return value if isinstance(value, list) else [value]


def build_html(spec: dict, ctx: dict) -> str:
    body = "".join(SECTIONS[s["type"]](s, ctx) for s in spec["sections"] if s.get("type") in SECTIONS)
    preheader = E(spec.get("preheader", ""))
    generated = spec.get("date_label", dt.date.today().strftime("%d %B %Y"))

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="color-scheme" content="light only" />
<title>{E(spec.get('title', 'Tech Trend Report'))}</title></head>
<body style="margin:0;padding:0;background:#eeeeeb;-webkit-text-size-adjust:100%;">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">{preheader}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#eeeeeb;">
<tr><td align="center" style="padding:26px 12px;">

<table role="presentation" width="{CONTENT_W}" cellpadding="0" cellspacing="0" border="0"
 style="width:100%;max-width:{CONTENT_W}px;background:#ffffff;border-radius:12px;overflow:hidden;
 font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
 box-shadow:0 1px 3px rgba(0,0,0,0.07);">

<tr><td style="height:5px;background:#2a78d6;font-size:0;line-height:0;">&nbsp;</td></tr>

<tr><td style="padding:30px 32px 0;">
  <div style="font-size:11.5px;text-transform:uppercase;letter-spacing:1.3px;color:#8a8985;font-weight:700;">
    {E(spec.get('kicker', 'Tech Trend Intelligence'))}
  </div>
  <h1 style="margin:9px 0 6px;font-size:27px;line-height:1.22;color:#0b0b0b;font-weight:800;letter-spacing:-0.6px;">
    {E(spec.get('title', 'Tech Trend Report'))}
  </h1>
  <div style="font-size:13.5px;color:#8a8985;">{E(generated)}</div>
</td></tr>

<tr><td style="padding:4px 32px 34px;">{body}</td></tr>

<tr><td style="padding:20px 32px 26px;background:#fafaf8;border-top:1px solid #f0efec;">
  <div style="font-size:12px;line-height:1.6;color:#8a8985;">{_rich(spec.get('footer', ''))}</div>
</td></tr>

</table>
</td></tr></table>
</body></html>"""


# =========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec-file", default=None, help="Default .tmp/report_spec.json")
    parser.add_argument("--analysis-file", default=None, help="Default .tmp/analysis.json")
    parser.add_argument("--no-thumbnails", action="store_true", help="Skip downloading video thumbnails")
    parser.add_argument("--open", action="store_true", help="Open the preview in a browser")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    spec_path = pathlib.Path(args.spec_file) if args.spec_file else tmp_path("report_spec.json")
    if not spec_path.exists():
        fail(f"Report spec not found: {spec_path}",
             hint="The agent authors this file from .tmp/analysis.json before rendering.")
    spec = read_json(spec_path)

    analysis_path = pathlib.Path(args.analysis_file) if args.analysis_file else tmp_path("analysis.json")
    if not analysis_path.exists():
        fail(f"Analysis not found: {analysis_path}", hint="Run: python tools/analyze_trends.py")
    analysis = read_json(analysis_path)

    charts_dir = tmp_path("charts", ".keep").parent

    # --- charts ----------------------------------------------------------
    wanted = [s["chart"] for s in spec["sections"] if s.get("type") == "chart"]
    rendered: dict[str, pathlib.Path] = {}
    for name in wanted:
        if name in rendered:
            continue
        if name not in CHARTS:
            fail(f"Unknown chart: {name}", hint=f"Available: {', '.join(CHARTS)}")
        log.info("Rendering chart: %s", name)
        rendered[name] = CHARTS[name](analysis, charts_dir / f"{name}.png")

    # --- thumbnails -------------------------------------------------------
    thumbs: dict[str, pathlib.Path] = {}
    if not args.no_thumbnails:
        thumbs = _fetch_thumbnails(spec, analysis, charts_dir)

    # --- html (email uses cid:, preview uses file paths) ------------------
    email_ctx = {
        "img_src": lambda n: f"cid:chart_{n}",
        "thumb_src": lambda v: f"cid:thumb_{v}" if v in thumbs else "",
    }
    preview_ctx = {
        "img_src": lambda n: rendered[n].name,
        "thumb_src": lambda v: thumbs[v].name if v in thumbs else "",
    }

    out_path = pathlib.Path(args.output) if args.output else tmp_path("report.html")
    out_path.write_text(build_html(spec, email_ctx), encoding="utf-8")

    preview_path = charts_dir / "preview.html"
    preview_path.write_text(build_html(spec, preview_ctx), encoding="utf-8")

    inline = {f"chart_{n}": str(p) for n, p in rendered.items()}
    inline.update({f"thumb_{v}": str(p) for v, p in thumbs.items()})
    manifest = write_json(tmp_path("report_assets.json"), {
        "html_file": str(out_path),
        "preview_file": str(preview_path),
        "inline_images": inline,
        "subject": spec.get("subject", spec.get("title", "Tech Trend Report")),
    })

    if args.open:
        webbrowser.open(preview_path.resolve().as_uri())

    emit({
        "charts": list(rendered),
        "thumbnails": len(thumbs),
        "sections": len(spec["sections"]),
        "html_bytes": out_path.stat().st_size,
        "total_image_bytes": sum(pathlib.Path(p).stat().st_size for p in inline.values()),
        "html_file": str(out_path),
        "preview_file": str(preview_path),
        "assets_file": str(manifest),
    })


def _fetch_thumbnails(spec: dict, analysis: dict, charts_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    """Download thumbnails for featured videos so they can be inlined as CID parts.

    Hotlinking would work in some clients, but many block remote images by
    default — inlining means the email looks right before the reader clicks
    "display images".
    """
    import requests

    urls = {v["video_id"]: v.get("thumbnail", "")
            for v in analysis.get("top_videos_overall", []) if v.get("thumbnail")}
    for row in analysis.get("technologies", []):
        for v in row.get("top_videos", []):
            if v.get("thumbnail"):
                urls.setdefault(v["video_id"], v["thumbnail"])

    wanted = [item["video_id"]
              for section in spec["sections"] if section.get("type") == "videos"
              for item in section["items"] if item.get("video_id")]

    thumbs: dict[str, pathlib.Path] = {}
    for video_id in wanted:
        url = urls.get(video_id) or f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
        path = charts_dir / f"thumb_{video_id}.jpg"
        if path.exists():
            thumbs[video_id] = path
            continue
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            path.write_bytes(response.content)
            thumbs[video_id] = path
        except requests.RequestException as exc:
            log.warning("Thumbnail failed for %s: %s", video_id, str(exc)[:100])
    return thumbs


if __name__ == "__main__":
    main()
