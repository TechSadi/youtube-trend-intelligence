"""Deterministic report spec — the fallback when no LLM authors the narrative.

This is the descendant of the hand-written spec builder from the first run, with
one structural difference that matters for unattended use: **nothing is keyed by
technology id**. The original named `evals`, `copilot`, `mcp` and friends
directly, which is fine when a human is looking at the data and wrong the first
week one of them drops out of the ranking. Everything here is selected by
position in the data — biggest riser with real breadth, biggest faller, widest
attention/adoption gaps.

The prose is fixed and the numbers move. That is the deal: it cannot notice a
story, but it also cannot hallucinate one, and it runs for free. Editorial rules
follow the standing feedback on these reports — around 600 words, sections that
change a decision, and nothing ranked by views, because view counts surface hype
by construction.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

ACCENTS = {"blue": "#2a78d6", "green": "#1baf7a", "amber": "#eda100", "red": "#e34948"}

# A riser with two videos across one channel is noise. These floors are what
# separates "a trend" from "one creator's content calendar".
MIN_VIDEOS_FOR_HEADLINE = 6
MIN_CHANNELS_FOR_HEADLINE = 4


# analyze_trends.py clamps momentum to this, so one technology rising from a
# base of zero cannot own the chart. A row sitting exactly on the clamp has not
# "grown 300%" — it grew by at least that, and the report should not imply a
# precision the number does not have.
MOMENTUM_CLAMP = 300.0


def _fmt_pct(value: float) -> str:
    """For prose, where there is room to be explicit about the clamp."""
    if value >= MOMENTUM_CLAMP:
        return f"over +{MOMENTUM_CLAMP:.0f}%"
    return f"{value:+.0f}%"


def _fmt_pct_short(value: float) -> str:
    """For stat tiles and table cells, where 26px type has no room for words."""
    if value >= MOMENTUM_CLAMP:
        return f">+{MOMENTUM_CLAMP:.0f}%"
    return f"{value:+.0f}%"


def _rows(analysis: dict) -> list[dict]:
    return [r for r in analysis.get("technologies", []) if r.get("video_count")]


def _risers(analysis: dict) -> list[dict]:
    """Accelerating technologies with enough breadth to be believable.

    Sorted by momentum, then by breadth. The tiebreak is load-bearing: several
    rows routinely pin to the clamp, and picking the headline among them by
    dictionary order would be arbitrary. Breadth is the honest tiebreak — it is
    what distinguishes a field-wide shift from one channel's upload schedule.
    """
    return sorted(
        (r for r in _rows(analysis)
         if r["momentum_pct"] > 0
         and r["video_count"] >= MIN_VIDEOS_FOR_HEADLINE
         and r["channel_count"] >= MIN_CHANNELS_FOR_HEADLINE),
        key=lambda r: (r["momentum_pct"], r["channel_count"], r["video_count"]), reverse=True,
    )


def _fallers(analysis: dict) -> list[dict]:
    return sorted(
        (r for r in _rows(analysis) if r["momentum_pct"] < 0 and r["video_count"] >= MIN_VIDEOS_FOR_HEADLINE),
        key=lambda r: r["momentum_pct"],
    )


def _hype(analysis: dict) -> list[dict]:
    return sorted((r for r in _rows(analysis) if r.get("verdict") == "hype-leaning"),
                  key=lambda r: -r.get("signal_gap", 0))


def _radar(analysis: dict) -> list[dict]:
    return sorted((r for r in _rows(analysis) if r.get("verdict") == "under-the-radar"),
                  key=lambda r: r.get("signal_gap", 0))


def _names(rows: list[dict], limit: int) -> str:
    return ", ".join(f"{r['name']} ({r.get('signal_gap', 0):+.0f})" for r in rows[:limit])


def build_spec(analysis: dict) -> dict[str, Any]:
    """Build a renderable report spec from an analysis, using no judgment at all."""
    corpus = analysis["corpus"]
    window = analysis["window"]

    start = dt.date.fromisoformat(window["start"][:10])
    end = dt.date.fromisoformat(window["end"][:10])
    midpoint = dt.date.fromisoformat(window["midpoint"][:10])

    risers, fallers = _risers(analysis), _fallers(analysis)
    hype, radar = _hype(analysis), _radar(analysis)
    cross_checked = bool(hype or radar)

    lead = risers[0] if risers else None
    second = risers[1] if len(risers) > 1 else None
    drop = fallers[0] if fallers else None

    title = (f"{lead['name']} is accelerating" if lead
             else "A quiet window across dev and AI")
    subject = (f"Dev & AI Trends — {end:%d %b}: {lead['name']} {_fmt_pct_short(lead['momentum_pct'])}"
               if lead else f"Dev & AI Trends — {end:%d %b}")

    preheader_bits = [f"{corpus['videos']:,} videos across {corpus['channels']} channels."]
    if lead:
        preheader_bits.append(f"{lead['name']} {_fmt_pct_short(lead['momentum_pct'])}.")
    if cross_checked:
        preheader_bits.append("Where creator attention diverges from real adoption.")

    spec: dict[str, Any] = {
        "subject": subject,
        "preheader": " ".join(preheader_bits),
        "kicker": "Software & AI/ML · Trend Intelligence",
        "title": title,
        "date_label": (f"{start:%d %b} – {end:%d %b %Y}  ·  "
                       f"{corpus['videos']:,} videos, {corpus['channels']} channels"),
        "footer": (
            "**Method:** momentum compares each technology's *share* of videos in the recent half of the "
            f"window (from {midpoint:%d %b}) against the prior half, so changes in upload volume do not "
            "distort it. The adoption index log-compresses Hacker News discussion and 90-day new-repo "
            "counts — a rank-like comparison, not an absolute measure. This tracks **creator attention**, "
            "corrected against two external sources; it is not an industry survey.<br><br>"
            "Sources: YouTube Data API v3, Hacker News, GitHub. Generated automatically by the WAT "
            "pipeline in your Youtube Analysis project."
        ),
        "sections": [],
    }
    sections = spec["sections"]

    # --- headline numbers -------------------------------------------------
    stats = []
    if lead:
        stats.append({"value": _fmt_pct_short(lead["momentum_pct"]), "label": lead["name"]})
    if second:
        stats.append({"value": _fmt_pct_short(second["momentum_pct"]), "label": second["name"]})
    if drop:
        stats.append({"value": _fmt_pct_short(drop["momentum_pct"]), "label": drop["name"]})
    elif hype:
        stats.append({"value": f"{hype[0]['signal_gap']:+.0f}", "label": f"{hype[0]['name']} hype gap"})
    if stats:
        sections.append({"type": "stats", "items": stats})

    # --- what changed -----------------------------------------------------
    if lead:
        paragraphs = [
            f"**{lead['name']} went from {lead['prior_videos']} videos to {lead['recent_videos']}** "
            f"({_fmt_pct(lead['momentum_pct'])}) across {lead['channel_count']} separate channels — "
            "breadth, which is what separates a real shift from one creator's content calendar."
        ]
        if second:
            paragraphs[0] += (f" {second['name']} moved the same way "
                              f"({_fmt_pct(second['momentum_pct'])}, {second['channel_count']} channels).")
        if drop:
            paragraphs.append(
                f"The mirror image: **{drop['name']} fell {abs(drop['momentum_pct']):.0f}%** across "
                f"{drop['video_count']} videos. Falling coverage is not the same as falling use — mature "
                "technologies stop generating new explainers — but it is where a year-old learning plan "
                "has quietly drifted."
            )
        sections.append({"type": "text", "heading": "What actually changed", "body": paragraphs})
    else:
        sections.append({"type": "text", "heading": "A flat window", "body": [
            f"No technology cleared the breadth floor ({MIN_VIDEOS_FOR_HEADLINE} videos across "
            f"{MIN_CHANNELS_FOR_HEADLINE} channels) with positive momentum this window. That usually means "
            "a short window or a holiday lull rather than a stalled field — the chart below still shows "
            "relative movement.",
        ]})

    sections.append({
        "type": "chart", "chart": "momentum",
        "heading": "Rising and fading",
        "caption": ("Change in share of videos, recent half against prior half. Bars marked *new* jumped "
                    "from a base of one or two videos — real movement, small sample."),
        "alt": "Diverging bar chart of technology momentum",
    })

    # --- attention vs adoption, the part worth reading --------------------
    if cross_checked:
        sections.append({
            "type": "chart", "chart": "signal_vs_hype",
            "heading": "What YouTube teaches vs. what the industry uses",
            "body": ["Horizontal: attention in this YouTube corpus. Vertical: activity on Hacker News and in "
                     "newly created GitHub repos. Below the line means more airtime than use."],
            "alt": "Scatter plot of YouTube attention against real-world adoption",
        })

        gap_body = []
        if hype:
            gap_body.append(
                "**Over-covered:** " + _names(hype, 4) + ". Content that reliably earns views is not the "
                "same as a skill that is gaining value, and the gap between the two is stable enough to "
                "plan around."
            )
        if radar:
            gap_body.append(
                "**Under-covered:** " + _names(radar, 5) + ". The load-bearing technologies nobody can make "
                "a viral video about. **Calibrating what to learn against YouTube's attention means "
                "systematically under-investing in exactly these.**"
            )
        if gap_body:
            sections.append({"type": "text", "body": gap_body})

    # --- what to do about it ---------------------------------------------
    cards = []
    if lead:
        cards.append({
            "title": lead["name"], "badge": "RISING", "accent": ACCENTS["blue"],
            "subtitle": f"{lead['prior_videos']} → {lead['recent_videos']} videos · {lead['channel_count']} channels",
            "body": "The steepest broad-based riser in the window. Breadth across channels means the shift is "
                    "in the field, not in one creator's calendar.",
        })
    if radar:
        pick = radar[0]
        cards.append({
            "title": pick["name"], "badge": "UNDERRATED", "accent": ACCENTS["amber"],
            "subtitle": f"attention {pick.get('attention_index', 0):.0f} vs adoption {pick.get('adoption_index', 0):.0f}",
            "body": "Widest gap in the dataset between how much it is used and how little it is taught. Low "
                    "coverage here is an opportunity, not a signal of irrelevance.",
        })
    if hype:
        pick = hype[0]
        cards.append({
            "title": f"Rebalance away from {pick['name']}", "badge": "REALLOCATE", "accent": ACCENTS["red"],
            "subtitle": f"largest hype gap in the dataset ({pick['signal_gap']:+.0f})",
            "body": "Heavily covered, thinly used. Keep enough to stay conversant; the marginal hour is better "
                    "spent on something from the under-covered list.",
        })
    if cards:
        sections.append({"type": "cards", "heading": "What to learn next", "items": cards})

    # --- cooling table ----------------------------------------------------
    if fallers:
        sections.append({
            "type": "table", "heading": "Cooling fastest",
            "columns": ["Technology", "Videos", "Change"],
            "rows": [[r["name"], str(r["video_count"]), _fmt_pct_short(r["momentum_pct"])] for r in fallers[:5]],
            "caption": "Not necessarily dying — several are simply mature enough that no one makes "
                       "introductory videos any more.",
        })

    return spec
