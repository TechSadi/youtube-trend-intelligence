"""Shared YouTube Data API v3 helpers: client, quota accounting, retries.

Quota is the binding constraint on this API, not rate. The default project
allowance is 10,000 units/day and resets at midnight Pacific. Costs that matter:

    search.list         100 units   <- expensive, use only for discovery
    channels.list         1 unit    (up to 50 ids per call)
    playlistItems.list    1 unit    (up to 50 items per page)
    videos.list           1 unit    (up to 50 ids per call)

So the cheap way to harvest a channel is: channels.list -> uploads playlist ->
playlistItems.list -> videos.list. That path costs ~3 units per 50 videos, while
a single search.list costs 100. Every call here goes through `_track()` so tools
can report exactly what a run spent.
"""

from __future__ import annotations

import random
import time
from typing import Any, Iterator

from _common import fail, get_logger

log = get_logger(__name__)

# Published quota costs, used for the estimate tools report back.
QUOTA_COSTS = {
    "search.list": 100,
    "channels.list": 1,
    "playlistItems.list": 1,
    "videos.list": 1,
    "videoCategories.list": 1,
}

_spent = 0


def quota_spent() -> int:
    """Units consumed so far in this process."""
    return _spent


def _track(endpoint: str) -> None:
    global _spent
    _spent += QUOTA_COSTS.get(endpoint, 1)


def build_client(api_key: str):
    """YouTube Data API client. cache_discovery=False avoids a noisy warning."""
    from googleapiclient.discovery import build

    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def execute(request, endpoint: str, *, retries: int = 3,
            soft_errors: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """Run an API request with quota tracking and backoff on transient errors.

    Quota exhaustion is terminal — we stop rather than retry, because retrying a
    quotaExceeded error just burns time until midnight Pacific.

    `soft_errors` names API reasons that are expected for *one* resource and
    should not abort a batch — a deleted channel's uploads playlist, say. Those
    return None so the caller can skip that item and keep going.
    """
    from googleapiclient.errors import HttpError

    for attempt in range(retries):
        try:
            result = request.execute()
            _track(endpoint)
            return result
        except HttpError as exc:
            status = exc.resp.status
            reason = _reason(exc)

            if reason in soft_errors:
                _track(endpoint)
                log.warning("%s: %s — skipping this resource", endpoint, reason)
                return None

            if reason in ("quotaExceeded", "dailyLimitExceeded"):
                fail(
                    f"YouTube API daily quota exhausted (spent ~{_spent} units this run).",
                    hint=(
                        "Quota resets at midnight Pacific. Reduce "
                        "max_discovery_searches in config/scope.json (each search "
                        "costs 100 units), or request more quota in the Cloud Console."
                    ),
                )

            if reason in ("keyInvalid", "badRequest") and status == 400:
                fail(
                    f"YouTube API rejected the request ({reason}): {exc}",
                    hint="Check YOUTUBE_API_KEY in .env and that YouTube Data API v3 is enabled.",
                )

            if status in (403, 429, 500, 503) and attempt < retries - 1:
                delay = (2 ** attempt) + random.uniform(0, 1)
                log.warning("%s -> HTTP %s (%s); retrying in %.1fs", endpoint, status, reason, delay)
                time.sleep(delay)
                continue

            fail(
                f"YouTube API call {endpoint} failed: {exc}",
                hint="Check the API key, that the quota is not exhausted, and that the resource IDs are valid.",
            )

    fail(f"YouTube API call {endpoint} failed after {retries} attempts.")
    return {}  # unreachable; keeps type checkers happy


def _reason(exc) -> str:
    """Pull the machine-readable reason out of an HttpError."""
    try:
        errors = exc.error_details
        if errors and isinstance(errors, list):
            return errors[0].get("reason", "")
    except Exception:
        pass
    try:
        import json

        body = json.loads(exc.content.decode("utf-8"))
        return body["error"]["errors"][0].get("reason", "")
    except Exception:
        return ""


def chunked(items: list, size: int = 50) -> Iterator[list]:
    """Split a list into API-sized batches. The API caps id lists at 50."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


def resolve_channels(client, refs: list[str]) -> tuple[list[dict], list[str]]:
    """Resolve a mix of @handles and channel IDs to channel resources.

    Channel IDs batch 50 per call (1 unit). Handles do not batch — forHandle
    takes one at a time — so a handle-heavy seed list costs 1 unit each.
    Returns (resolved, unresolved_refs).
    """
    part = "id,snippet,statistics,contentDetails"
    handles = [r for r in refs if r.startswith("@")]
    ids = [r for r in refs if not r.startswith("@")]

    resolved: list[dict] = []
    unresolved: list[str] = []

    for batch in chunked(ids, 50):
        data = execute(client.channels().list(part=part, id=",".join(batch), maxResults=50), "channels.list")
        found = data.get("items", [])
        resolved.extend(found)
        found_ids = {c["id"] for c in found}
        unresolved.extend(i for i in batch if i not in found_ids)

    for handle in handles:
        data = execute(client.channels().list(part=part, forHandle=handle), "channels.list")
        items = data.get("items", [])
        if items:
            resolved.extend(items)
        else:
            unresolved.append(handle)
            log.warning("Could not resolve handle %s", handle)

    return resolved, unresolved


def channel_summary(channel: dict) -> dict[str, Any]:
    """Flatten a channel resource down to the fields the pipeline uses."""
    stats = channel.get("statistics", {})
    snippet = channel.get("snippet", {})
    return {
        "channel_id": channel["id"],
        "title": snippet.get("title", ""),
        "handle": snippet.get("customUrl", ""),
        "description": (snippet.get("description") or "")[:500],
        "country": snippet.get("country", ""),
        "published_at": snippet.get("publishedAt", ""),
        "subscribers": int(stats.get("subscriberCount", 0) or 0),
        "video_count": int(stats.get("videoCount", 0) or 0),
        "view_count": int(stats.get("viewCount", 0) or 0),
        "uploads_playlist": channel.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads", ""),
    }
