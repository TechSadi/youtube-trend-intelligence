"""Measure GitHub adoption velocity per technology — the "is anyone building with it" check.

Two numbers per technology:
  - new_repos     how many repos mentioning it were created in the window
  - new_repo_stars stars earned by the top of those new repos

New-repo activity is a much harder adoption signal than total stars, which mostly
reflect history. A tool that is genuinely spreading shows up in things people
started building recently.

GitHub's search endpoints are rate-limited far more tightly than the rest of the
API (30 req/min authenticated), so this throttles and backs off on 403.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys
import time

import requests

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import emit, get_env, get_logger, tmp_path, write_json
from _signals import TAXONOMY, search_term, select_technologies

log = get_logger(__name__)

SEARCH_URL = "https://api.github.com/search/repositories"


def search_repos(session: requests.Session, query: str, *, per_page: int = 10) -> dict | None:
    """One search call, with backoff when the search rate limit bites."""
    for attempt in range(4):
        response = session.get(
            SEARCH_URL,
            params={"q": query, "sort": "stars", "order": "desc", "per_page": per_page},
            timeout=25,
        )
        if response.status_code == 200:
            return response.json()

        if response.status_code in (403, 429):
            remaining = response.headers.get("X-RateLimit-Remaining")
            retry_after = response.headers.get("Retry-After")
            wait = int(retry_after) if retry_after and retry_after.isdigit() else 20 * (attempt + 1)
            log.warning("GitHub rate limited (remaining=%s); waiting %ss", remaining, wait)
            time.sleep(wait)
            continue

        if response.status_code == 422:
            log.warning("GitHub rejected query %r", query)
            return None

        log.warning("GitHub returned HTTP %s for %r", response.status_code, query)
        return None
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-file", default=None, help="Default .tmp/analysis.json")
    parser.add_argument("--taxonomy-file", default=str(TAXONOMY))
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    token = get_env("GITHUB_TOKEN")
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "wat-youtube-trend-report/1.0",
    })
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        log.warning("No GITHUB_TOKEN set — unauthenticated search allows only 10 req/min. "
                    "Expect this to be slow or partial.")

    analysis_file = pathlib.Path(args.analysis_file) if args.analysis_file else tmp_path("analysis.json")
    technologies = select_technologies(analysis_file, args.top, pathlib.Path(args.taxonomy_file))

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).date().isoformat()
    throttle = 2.2 if token else 7.0

    signals = {}
    failures = []

    for tech in technologies:
        term = search_term(tech)
        data = search_repos(session, f'"{term}" created:>{since}')
        if data is None:
            failures.append(tech["id"])
            continue

        items = data.get("items", [])
        signals[tech["id"]] = {
            "id": tech["id"],
            "name": tech["name"],
            "query": term,
            "new_repos": int(data.get("total_count", 0)),
            "new_repo_stars": sum(int(r.get("stargazers_count") or 0) for r in items),
            "top_new_repos": [
                {
                    "name": r.get("full_name", ""),
                    "stars": int(r.get("stargazers_count") or 0),
                    "description": (r.get("description") or "")[:160],
                    "url": r.get("html_url", ""),
                    "language": r.get("language") or "",
                }
                for r in items[:3]
            ],
        }
        log.info("  %-24s %6d new repos  %6d stars", tech["name"][:24],
                 signals[tech["id"]]["new_repos"], signals[tech["id"]]["new_repo_stars"])
        time.sleep(throttle)

    out = write_json(pathlib.Path(args.output) if args.output else tmp_path("github_signals.json"), signals)
    emit({
        "technologies": len(signals),
        "window_days": args.days,
        "authenticated": bool(token),
        "failures": failures,
        "output_file": str(out),
    })


if __name__ == "__main__":
    main()
