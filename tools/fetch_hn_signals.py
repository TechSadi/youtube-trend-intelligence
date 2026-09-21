"""Measure Hacker News discussion volume per technology — the practitioner cross-check.

YouTube tells you what educators are teaching; HN tells you what working engineers
are actually arguing about. A technology that is loud on YouTube and silent on HN
is usually content-driven hype rather than adoption.

Uses the free Algolia-backed HN API: no key, ~10k requests/hour, so a 25-term run
is trivially within limits.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import emit, get_logger, tmp_path, write_json
from _signals import TAXONOMY, search_term, select_technologies

log = get_logger(__name__)

ENDPOINT = "https://hn.algolia.com/api/v1/search"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-file", default=None, help="Default .tmp/analysis.json")
    parser.add_argument("--taxonomy-file", default=str(TAXONOMY))
    parser.add_argument("--top", type=int, default=30, help="How many technologies to check")
    parser.add_argument("--days", type=int, default=90, help="Lookback window")
    parser.add_argument("--hits", type=int, default=60, help="Stories to sample per technology")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    analysis_file = pathlib.Path(args.analysis_file) if args.analysis_file else tmp_path("analysis.json")
    technologies = select_technologies(analysis_file, args.top, pathlib.Path(args.taxonomy_file))

    since = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).timestamp())
    session = requests.Session()
    session.headers["User-Agent"] = "wat-youtube-trend-report/1.0"

    signals = {}
    failures = []

    for tech in technologies:
        term = search_term(tech)
        try:
            response = session.get(
                ENDPOINT,
                params={
                    "query": term,
                    "tags": "story",
                    "numericFilters": f"created_at_i>{since}",
                    "hitsPerPage": args.hits,
                },
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("HN lookup failed for %s: %s", term, str(exc)[:120])
            failures.append(tech["id"])
            continue

        hits = data.get("hits", [])
        points = sum(int(h.get("points") or 0) for h in hits)
        comments = sum(int(h.get("num_comments") or 0) for h in hits)

        top_stories = sorted(hits, key=lambda h: int(h.get("points") or 0), reverse=True)[:3]
        signals[tech["id"]] = {
            "id": tech["id"],
            "name": tech["name"],
            "query": term,
            "story_count": int(data.get("nbHits", len(hits))),
            "sampled": len(hits),
            "total_points": points,
            "total_comments": comments,
            "avg_points": round(points / len(hits), 1) if hits else 0.0,
            "discussion_score": points + comments,
            "top_stories": [
                {
                    "title": h.get("title", ""),
                    "points": int(h.get("points") or 0),
                    "comments": int(h.get("num_comments") or 0),
                    "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                }
                for h in top_stories
            ],
        }
        log.info("  %-24s %4d stories  %5d pts", tech["name"][:24], signals[tech["id"]]["story_count"], points)
        time.sleep(0.15)

    out = write_json(pathlib.Path(args.output) if args.output else tmp_path("hn_signals.json"), signals)
    emit({
        "technologies": len(signals),
        "window_days": args.days,
        "failures": failures,
        "output_file": str(out),
    })


if __name__ == "__main__":
    main()
