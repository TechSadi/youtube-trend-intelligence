"""Harvest recent videos from a channel list via the cheap uploads-playlist path.

Per channel: playlistItems.list on the uploads playlist (1 unit per 50 videos),
then videos.list for statistics (1 unit per 50 ids). No search.list here, so
harvesting thousands of videos costs tens of units rather than thousands.

Paging stops as soon as a page runs past the lookback window — uploads playlists
are newest-first, so there is no reason to walk a channel's whole history.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import emit, fail, get_env, get_logger, load_scope, read_json, tmp_path, write_json
from _youtube import build_client, chunked, execute, quota_spent

log = get_logger(__name__)

DEFAULT_SCOPE = pathlib.Path(__file__).resolve().parent.parent / "config" / "scope.json"

ISO_DURATION = re.compile(
    r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


def parse_duration(value: str) -> int:
    """ISO-8601 duration -> seconds. Used to drop Shorts and trailers."""
    match = ISO_DURATION.match(value or "")
    if not match:
        return 0
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channels-file", default=None, help="Default .tmp/channels.json")
    parser.add_argument("--scope-file", default=str(DEFAULT_SCOPE))
    parser.add_argument("--lookback-days", type=int, default=None, help="Override scope value")
    parser.add_argument("--max-per-channel", type=int, default=None, help="Override scope value")
    parser.add_argument("--limit-channels", type=int, default=None, help="Smoke-test switch: only N channels")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    scope = load_scope(args.scope_file)
    channels_path = pathlib.Path(args.channels_file) if args.channels_file else tmp_path("channels.json")
    if not channels_path.exists():
        fail(f"Channels file not found: {channels_path}",
             hint="Run: python tools/youtube_discover_channels.py")
    channels = read_json(channels_path)
    if args.limit_channels:
        channels = channels[: args.limit_channels]

    lookback = args.lookback_days or int(scope.get("lookback_days", 60))
    max_per_channel = args.max_per_channel or int(scope.get("max_videos_per_channel", 40))
    min_duration = int(scope.get("min_video_duration_seconds", 0))

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback)
    log.info("Harvesting videos published after %s from %d channels", cutoff.date(), len(channels))

    api_key = get_env("YOUTUBE_API_KEY", required=True)
    client = build_client(api_key)

    # --- Collect candidate ids from each uploads playlist ----------------
    candidates: dict[str, dict] = {}
    skipped_channels: list[str] = []
    for channel in channels:
        playlist = channel.get("uploads_playlist")
        if not playlist:
            continue

        collected, page_token, stop = 0, None, False
        while not stop and collected < max_per_channel:
            data = execute(
                client.playlistItems().list(
                    part="contentDetails", playlistId=playlist,
                    maxResults=min(50, max_per_channel - collected), pageToken=page_token,
                ),
                "playlistItems.list",
                # A discovered channel may be deleted or have no uploads by the
                # time we walk it. Skip it rather than losing the whole harvest.
                soft_errors=("playlistNotFound", "playlistItemsNotAccessible"),
            )
            if data is None:
                skipped_channels.append(channel["title"])
                break
            items = data.get("items", [])
            if not items:
                break

            for item in items:
                details = item.get("contentDetails", {})
                published = details.get("videoPublishedAt")
                if not published:
                    continue
                if parse_ts(published) < cutoff:
                    # Uploads are newest-first, so everything after this is older too.
                    stop = True
                    break
                candidates[details["videoId"]] = {
                    "video_id": details["videoId"],
                    "channel_id": channel["channel_id"],
                    "channel_title": channel["title"],
                    "channel_subscribers": channel["subscribers"],
                }
                collected += 1

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        log.info("  %-32s %3d videos in window", channel["title"][:32], collected)

    if not candidates:
        fail(
            f"No videos published in the last {lookback} days across {len(channels)} channels.",
            hint="Widen lookback_days in config/scope.json, or check that the channel list is right.",
        )

    # --- Hydrate with statistics (1 unit per 50) -------------------------
    log.info("Fetching statistics for %d videos", len(candidates))
    videos = []
    now = dt.datetime.now(dt.timezone.utc)

    for batch in chunked(list(candidates), 50):
        data = execute(
            client.videos().list(part="snippet,statistics,contentDetails", id=",".join(batch), maxResults=50),
            "videos.list",
            soft_errors=("videoNotFound",),
        )
        if data is None:
            continue
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            duration = parse_duration(item.get("contentDetails", {}).get("duration", ""))
            if duration < min_duration:
                continue  # Shorts and clips distort engagement metrics

            published = parse_ts(snippet.get("publishedAt"))
            age_days = max((now - published).total_seconds() / 86400, 0.5)
            views = int(stats.get("viewCount", 0) or 0)
            likes = int(stats.get("likeCount", 0) or 0)
            comments = int(stats.get("commentCount", 0) or 0)

            base = candidates[item["id"]]
            videos.append({
                **base,
                "title": snippet.get("title", ""),
                "description": (snippet.get("description") or "")[:2000],
                "tags": snippet.get("tags", [])[:30],
                "published_at": snippet.get("publishedAt"),
                "age_days": round(age_days, 2),
                "duration_seconds": duration,
                "views": views,
                "likes": likes,
                "comments": comments,
                "views_per_day": round(views / age_days, 1),
                "engagement_rate": round((likes + comments) / views, 5) if views else 0.0,
                "thumbnail": (snippet.get("thumbnails", {}).get("medium", {}) or {}).get("url", ""),
                "url": f"https://www.youtube.com/watch?v={item['id']}",
            })

    videos.sort(key=lambda v: v["views_per_day"], reverse=True)
    out = write_json(pathlib.Path(args.output) if args.output else tmp_path("videos.json"), videos)

    emit({
        "videos": len(videos),
        "channels_covered": len({v["channel_id"] for v in videos}),
        "window_days": lookback,
        "dropped_short_videos": len(candidates) - len(videos),
        "skipped_channels": skipped_channels,
        "quota_units_spent": quota_spent(),
        "output_file": str(out),
    })


if __name__ == "__main__":
    main()
