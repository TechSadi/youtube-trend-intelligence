"""Turn raw videos, transcripts and external signals into ranked technology trends.

Deterministic — no LLM. This tool produces the *numbers*; the agent reads them
and writes the narrative. Keeping that boundary sharp is what makes the report
reproducible instead of a fresh interpretation every run.

Run it twice:
  1. YouTube only                  -> ranked technologies, used to pick lookups
  2. with --hn-file / --github-file -> merges the cross-source signals in

Momentum compares the recent half of the window against the prior half, using
*share of videos* rather than raw counts, so it stays meaningful even when
upload volume differs between the halves. That also means momentum works on the
very first run, with no historical baseline.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import re
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import emit, fail, get_logger, read_json, tmp_path, write_json

log = get_logger(__name__)

CONFIG = pathlib.Path(__file__).resolve().parent.parent / "config"

# Where a mention appears says a lot about how central it is to the video.
WEIGHT_TITLE = 3.0
WEIGHT_TAGS = 2.0
WEIGHT_DESCRIPTION = 1.0
WEIGHT_TRANSCRIPT = 0.5


def build_pattern(alias: str) -> re.Pattern:
    r"""Word-boundary regex that survives '+', '#' and '.' in technology names.

    \b is useless next to punctuation (c++, c#, .net), so boundaries are
    explicit lookarounds, dropped on an edge that is already punctuation.
    """
    escaped = re.escape(alias)
    boundary = r"A-Za-z0-9_+#"
    prefix = rf"(?<![{boundary}])" if alias[0].isalnum() else ""
    suffix = rf"(?![{boundary}])" if alias[-1].isalnum() else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


def compile_taxonomy(taxonomy: dict) -> list[dict]:
    """Attach compiled patterns to each technology entry.

    'aliases'          always count.
    'context_aliases'  only count when the document also holds a context word,
                       which is what keeps 'go', 'bun' and 'cursor' from
                       matching ordinary English.
    'requires_context' promotes every alias to context-gated.
    """
    compiled = []
    for tech in taxonomy["technologies"]:
        gated = list(tech.get("context_aliases", []))
        plain = list(tech.get("aliases", []))
        if tech.get("requires_context"):
            gated += plain
            plain = []
        compiled.append({
            "id": tech["id"],
            "name": tech["name"],
            "category": tech["category"],
            "plain": [build_pattern(a) for a in plain],
            "gated": [build_pattern(a) for a in gated],
        })
    return compiled


def count_in(text: str, patterns: list[re.Pattern]) -> int:
    return sum(len(p.findall(text)) for p in patterns) if text else 0


URL_RE = re.compile(r"https?://\S+|www\.\S+")


def strip_urls(text: str) -> str:
    """Remove links before matching.

    Video descriptions are mostly boilerplate: sponsor links, Discord invites,
    a github.com repo link on every single upload. A URL containing a product
    name is not the video talking about that product, and counting it put Git
    near the top of the rankings purely on link furniture.
    """
    return URL_RE.sub(" ", text)


def build_context_pattern(words: list[str]) -> re.Pattern:
    r"""One alternation matching any context word on a word boundary.

    Substring matching is not good enough here: 'script' appears inside
    'description', which would flip the context gate on for almost every video
    and let 'go', 'bun' and 'cursor' match ordinary English.
    """
    return re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--videos-file", default=None)
    parser.add_argument("--transcripts-file", default=None)
    parser.add_argument("--hn-file", default=None, help="Optional, from fetch_hn_signals.py")
    parser.add_argument("--github-file", default=None, help="Optional, from fetch_github_signals.py")
    parser.add_argument("--taxonomy-file", default=str(CONFIG / "tech_taxonomy.json"))
    parser.add_argument("--min-videos", type=int, default=3,
                        help="Drop technologies mentioned in fewer videos than this")
    parser.add_argument("--timeline-weeks", type=int, default=9)
    parser.add_argument("--no-snapshot", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    videos_path = pathlib.Path(args.videos_file) if args.videos_file else tmp_path("videos.json")
    if not videos_path.exists():
        fail(f"Videos file not found: {videos_path}", hint="Run: python tools/youtube_fetch_videos.py")
    videos = read_json(videos_path)
    if not videos:
        fail("Videos file is empty.", hint="Widen lookback_days in config/scope.json and refetch.")

    transcripts_path = pathlib.Path(args.transcripts_file) if args.transcripts_file else tmp_path("transcripts.json")
    transcripts = {}
    if transcripts_path.exists():
        transcripts = {t["video_id"]: t.get("text", "") for t in read_json(transcripts_path)}
    log.info("Analyzing %d videos (%d with transcripts)", len(videos), len(transcripts))

    taxonomy = read_json(pathlib.Path(args.taxonomy_file))
    context_pattern = build_context_pattern(taxonomy.get("_context_words", []))
    technologies = compile_taxonomy(taxonomy)

    # --- Window halves, for momentum -------------------------------------
    timestamps = [dt.datetime.fromisoformat(v["published_at"].replace("Z", "+00:00")) for v in videos]
    window_start, window_end = min(timestamps), max(timestamps)
    midpoint = window_start + (window_end - window_start) / 2

    recent_total = sum(1 for t in timestamps if t >= midpoint)
    prior_total = sum(1 for t in timestamps if t < midpoint)
    log.info("Window %s .. %s (split at %s: %d recent / %d prior)",
             window_start.date(), window_end.date(), midpoint.date(), recent_total, prior_total)

    # --- Per-video matching ----------------------------------------------
    hits: dict[str, dict] = defaultdict(lambda: {
        "videos": [], "score": 0.0, "channels": set(), "recent": 0, "prior": 0, "weekly": Counter(),
    })
    per_video_techs: list[set[str]] = []

    for video, published in zip(videos, timestamps):
        title = video.get("title", "")
        description = strip_urls(video.get("description", ""))
        tags = " ".join(video.get("tags", []))
        transcript = transcripts.get(video["video_id"], "")

        haystack = f"{title} {description} {tags} {transcript}"
        has_context = bool(context_pattern.search(haystack))

        matched: set[str] = set()
        for tech in technologies:
            plain = tech["plain"]
            # Ambiguous aliases ('go', 'spark', 'cursor', 'bun') are only
            # trusted in the title and tags, where naming a technology is a
            # deliberate topic signal. In a corpus that is 100% software
            # videos the context-word gate is always satisfied, so on its own
            # it filters nothing — prose position is the real discriminator.
            gated = tech["gated"] if has_context else []
            if not (plain or gated):
                continue

            title_hits = count_in(title, plain + gated)
            tag_hits = count_in(tags, plain + gated)
            desc_hits = count_in(description, plain)
            # Transcript repetition is capped: a tutorial saying "Python" 200
            # times is not 200x the signal of one that says it twice.
            transcript_hits = min(count_in(transcript, plain), 12)

            if not (title_hits or tag_hits or desc_hits or transcript_hits):
                continue

            score = (title_hits * WEIGHT_TITLE + tag_hits * WEIGHT_TAGS
                     + desc_hits * WEIGHT_DESCRIPTION + transcript_hits * WEIGHT_TRANSCRIPT)

            entry = hits[tech["id"]]
            entry["score"] += score
            entry["videos"].append(video)
            entry["channels"].add(video["channel_id"])
            entry["weekly"][published.strftime("%Y-W%W")] += 1
            if published >= midpoint:
                entry["recent"] += 1
            else:
                entry["prior"] += 1
            matched.add(tech["id"])

        per_video_techs.append(matched)

    # --- Aggregate ---------------------------------------------------------
    by_id = {t["id"]: t for t in technologies}
    hn = read_json(pathlib.Path(args.hn_file)) if args.hn_file and pathlib.Path(args.hn_file).exists() else {}
    gh = read_json(pathlib.Path(args.github_file)) if args.github_file and pathlib.Path(args.github_file).exists() else {}

    rows = []
    for tech_id, entry in hits.items():
        matched_videos = entry["videos"]
        if len(matched_videos) < args.min_videos:
            continue

        recent_share = entry["recent"] / recent_total if recent_total else 0.0
        prior_share = entry["prior"] / prior_total if prior_total else 0.0

        if prior_share > 0:
            momentum = (recent_share - prior_share) / prior_share * 100
            status = "new" if entry["prior"] <= 1 and entry["recent"] >= 3 else "tracked"
        else:
            momentum = 100.0 if entry["recent"] else 0.0
            status = "new"
        momentum = max(min(momentum, 300.0), -100.0)  # keep one outlier from owning the chart

        views = [v["views"] for v in matched_videos]
        vpd = [v["views_per_day"] for v in matched_videos]
        engagement = [v["engagement_rate"] for v in matched_videos if v["views"] > 1000]

        row = {
            "id": tech_id,
            "name": by_id[tech_id]["name"],
            "category": by_id[tech_id]["category"],
            "mention_score": round(entry["score"], 1),
            "video_count": len(matched_videos),
            "channel_count": len(entry["channels"]),
            "recent_videos": entry["recent"],
            "prior_videos": entry["prior"],
            "momentum_pct": round(momentum, 1),
            "status": status,
            "total_views": sum(views),
            "median_views": int(statistics.median(views)),
            "median_views_per_day": round(statistics.median(vpd), 1),
            "avg_engagement_rate": round(statistics.mean(engagement), 5) if engagement else 0.0,
            "weekly_counts": dict(sorted(entry["weekly"].items())),
            "top_videos": [
                {k: v[k] for k in ("video_id", "title", "channel_title", "views",
                                   "views_per_day", "url", "thumbnail", "published_at")}
                for v in sorted(matched_videos, key=lambda x: x["views_per_day"], reverse=True)[:5]
            ],
        }

        if tech_id in hn:
            row["hn"] = {k: hn[tech_id][k] for k in
                         ("story_count", "total_points", "total_comments", "discussion_score", "top_stories")}
        if tech_id in gh:
            row["github"] = {k: gh[tech_id][k] for k in ("new_repos", "new_repo_stars", "top_new_repos")}

        rows.append(row)

    rows.sort(key=lambda r: r["mention_score"], reverse=True)

    # --- Attention vs adoption, for the signal-vs-hype chart ---------------
    # Both sides are heavy-tailed: GitHub reports 600k+ new repos mentioning
    # "python" against a few hundred for a young tool, and HN points behave the
    # same way. Raw counts would let one row own the whole chart, so each
    # component is log-compressed and independently min-max normalised, then
    # averaged. The result is a *rank-like* comparison, which is all the
    # hype-vs-signal question needs.
    scored = [r for r in rows if "hn" in r or "github" in r]
    if scored:
        def norm(values: list[float]) -> list[float]:
            low, high = min(values), max(values)
            span = high - low
            return [(v - low) / span * 100 if span else 50.0 for v in values]

        attention = norm([math.log10(1 + r["mention_score"]) for r in rows])
        for row, value in zip(rows, attention):
            row["attention_index"] = round(value, 1)

        hn_part = norm([math.log10(1 + r.get("hn", {}).get("discussion_score", 0)) for r in scored])
        gh_part = norm([math.log10(1 + r.get("github", {}).get("new_repos", 0)) for r in scored])

        for row, hn_value, gh_value in zip(scored, hn_part, gh_part):
            row["adoption_index"] = round((hn_value + gh_value) / 2, 1)
            row["hn_index"] = round(hn_value, 1)
            row["github_index"] = round(gh_value, 1)
            gap = row["attention_index"] - row["adoption_index"]
            row["signal_gap"] = round(gap, 1)
            row["verdict"] = ("hype-leaning" if gap > 20 else
                              "under-the-radar" if gap < -20 else "aligned")

    # --- Co-occurrence -----------------------------------------------------
    ranked_ids = {r["id"] for r in rows}
    pairs: Counter = Counter()
    for matched in per_video_techs:
        ranked = sorted(t for t in matched if t in ranked_ids)
        for i, a in enumerate(ranked):
            for b in ranked[i + 1:]:
                pairs[(a, b)] += 1

    all_weeks = sorted({w for r in rows for w in r["weekly_counts"]})[-args.timeline_weeks:]

    analysis = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "window": {
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
            "midpoint": midpoint.isoformat(),
            "days": (window_end - window_start).days,
        },
        "corpus": {
            "videos": len(videos),
            "channels": len({v["channel_id"] for v in videos}),
            "transcripts": len(transcripts),
            "total_views": sum(v["views"] for v in videos),
            "recent_half_videos": recent_total,
            "prior_half_videos": prior_total,
        },
        "sources_merged": {"hackernews": bool(hn), "github": bool(gh)},
        "weeks": all_weeks,
        "technologies": rows,
        "by_momentum": sorted(rows, key=lambda r: r["momentum_pct"], reverse=True)[:15],
        "cooling": sorted(rows, key=lambda r: r["momentum_pct"])[:10],
        "top_pairs": [
            {"a": by_id[a]["name"], "b": by_id[b]["name"], "count": c}
            for (a, b), c in pairs.most_common(15)
        ],
        "top_videos_overall": sorted(videos, key=lambda v: v["views_per_day"], reverse=True)[:15],
        "category_totals": _category_totals(rows),
    }

    out = write_json(pathlib.Path(args.output) if args.output else tmp_path("analysis.json"), analysis)

    if not args.no_snapshot:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        write_json(tmp_path("history", f"{stamp}.json"), {
            "generated_at": analysis["generated_at"],
            "corpus": analysis["corpus"],
            "technologies": [
                {k: r[k] for k in ("id", "name", "mention_score", "video_count", "momentum_pct")}
                for r in rows
            ],
        })

    emit({
        "technologies_ranked": len(rows),
        "videos_analyzed": len(videos),
        "transcripts_used": len(transcripts),
        "window_days": analysis["window"]["days"],
        "sources_merged": analysis["sources_merged"],
        "top_10": [f"{r['name']} ({r['video_count']}v, {r['momentum_pct']:+.0f}%)" for r in rows[:10]],
        "output_file": str(out),
    })




def _category_totals(rows: list[dict]) -> list[dict]:
    totals: dict[str, dict] = defaultdict(lambda: {"score": 0.0, "videos": 0})
    for row in rows:
        totals[row["category"]]["score"] += row["mention_score"]
        totals[row["category"]]["videos"] += row["video_count"]
    return sorted(
        [{"category": k, "score": round(v["score"], 1), "videos": v["videos"]} for k, v in totals.items()],
        key=lambda x: x["score"], reverse=True,
    )


if __name__ == "__main__":
    main()
