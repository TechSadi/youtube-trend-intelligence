"""Discover YouTube channels in the research scope, seeded by a hand-curated list.

Seeds are resolved cheaply (1 unit each). Discovery uses search.list, which costs
100 units per query, so the number of queries is hard-capped by config and by
--max-searches. This is the only tool in the pipeline that spends real quota.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import emit, fail, get_env, get_logger, load_scope, read_json, tmp_path, write_json
from _youtube import build_client, channel_summary, execute, quota_spent, resolve_channels

log = get_logger(__name__)

DEFAULT_SCOPE = pathlib.Path(__file__).resolve().parent.parent / "config" / "scope.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope-file", default=str(DEFAULT_SCOPE), help="Research scope JSON")
    parser.add_argument("--max-searches", type=int, default=None,
                        help="Override scope cap. Each search costs 100 quota units.")
    parser.add_argument("--seeds-only", action="store_true",
                        help="Resolve seed channels and skip discovery entirely (costs ~1 unit per seed).")
    parser.add_argument("--output", default=None, help="Output path (default .tmp/channels.json)")
    args = parser.parse_args()

    scope = load_scope(args.scope_file)

    api_key = get_env("YOUTUBE_API_KEY", required=True)
    client = build_client(api_key)

    seeds = scope.get("seed_channels", [])
    min_subs = int(scope.get("min_channel_subscribers", 0))

    # --- Seeds -----------------------------------------------------------
    log.info("Resolving %d seed channels", len(seeds))
    seed_channels, unresolved = resolve_channels(client, seeds)
    channels = {c["id"]: channel_summary(c) for c in seed_channels}
    for cid in channels:
        channels[cid]["source"] = "seed"
    if unresolved:
        log.warning("%d seed refs did not resolve: %s", len(unresolved), ", ".join(unresolved))

    # --- Discovery -------------------------------------------------------
    discovered_ids: set[str] = set()
    queries_run: list[str] = []

    if not args.seeds_only:
        cap = args.max_searches if args.max_searches is not None else int(scope.get("max_discovery_searches", 0))
        queries = scope.get("discovery_queries", [])[:cap]
        per_query = int(scope.get("max_channels_per_query", 10))

        for query in queries:
            log.info("Discovery search (100 units): %s", query)
            data = execute(
                client.search().list(
                    part="snippet", q=query, type="video", order="relevance",
                    relevanceLanguage="en", maxResults=min(per_query * 2, 50),
                ),
                "search.list",
            )
            queries_run.append(query)
            for item in data.get("items", []):
                cid = item.get("snippet", {}).get("channelId")
                if cid and cid not in channels:
                    discovered_ids.add(cid)

        if discovered_ids:
            log.info("Resolving %d newly discovered channels", len(discovered_ids))
            found, _ = resolve_channels(client, sorted(discovered_ids))
            for channel in found:
                summary = channel_summary(channel)
                if summary["subscribers"] < min_subs:
                    continue
                if summary["channel_id"] in channels:
                    continue
                summary["source"] = "discovered"
                channels[summary["channel_id"]] = summary

    usable = [c for c in channels.values() if c["uploads_playlist"]]
    usable.sort(key=lambda c: c["subscribers"], reverse=True)

    if not usable:
        fail(
            "No usable channels resolved.",
            hint="Check the seed handles in config/scope.json — handles must include the leading @.",
        )

    out = write_json(pathlib.Path(args.output) if args.output else tmp_path("channels.json"), usable)

    emit({
        "channels": len(usable),
        "from_seeds": sum(1 for c in usable if c["source"] == "seed"),
        "discovered": sum(1 for c in usable if c["source"] == "discovered"),
        "unresolved_seeds": unresolved,
        "searches_run": len(queries_run),
        "quota_units_spent": quota_spent(),
        "output_file": str(out),
    })


if __name__ == "__main__":
    main()
