"""Fetch transcripts for the highest-signal videos. Free — no API key, no quota.

Transcripts are where the real signal lives: a title says a technology was
mentioned, a transcript says what people actually think about it.

IMPORTANT (verified 2026-09-21): YouTube blocks datacenter IPs — AWS, GCP,
Azure, Vercel, Colab, and GitHub Actions runners. This works from a home or
residential connection and fails from a cloud runner with
RequestBlocked/IpBlocked.

Set TRANSCRIPT_PROXY_* to route through residential proxies and the step works
from anywhere:

    Webshare  TRANSCRIPT_PROXY_PROVIDER=webshare
              TRANSCRIPT_PROXY_USERNAME / TRANSCRIPT_PROXY_PASSWORD
    Any other TRANSCRIPT_PROXY_PROVIDER=generic
              TRANSCRIPT_PROXY_HTTP_URL / TRANSCRIPT_PROXY_HTTPS_URL

Without a proxy on a blocked host the tool still exits cleanly when the cache
has anything in it — the per-video cache is what lets a scheduled run keep
accumulating transcripts across weeks instead of needing them all at once.

Results are cached per video under .tmp/transcripts/, so re-runs cost nothing
and a partial run can always be resumed.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import emit, fail, get_env, get_logger, read_json, tmp_path, write_json

log = get_logger(__name__)

# Per-video outcomes that are normal and expected — never fatal to a batch.
SKIPPABLE = ("TranscriptsDisabled", "NoTranscriptFound", "VideoUnavailable",
             "VideoUnplayable", "AgeRestricted", "InvalidVideoId", "NotTranslatable")
# Outcomes that mean every subsequent request will fail too — stop early.
BLOCKING = ("RequestBlocked", "IpBlocked", "PoTokenRequired")


def build_proxy_config():
    """Residential proxy config from the environment, or None for a direct connection.

    Returns (config, description). The description goes into the emitted envelope
    so a run log says whether transcripts went direct or through a proxy — which
    is the first thing worth knowing when the fetch count collapses.
    """
    provider = (get_env("TRANSCRIPT_PROXY_PROVIDER") or "").strip().lower()
    if not provider or provider == "none":
        return None, "direct"

    if provider == "webshare":
        username = get_env("TRANSCRIPT_PROXY_USERNAME")
        password = get_env("TRANSCRIPT_PROXY_PASSWORD")
        if not (username and password):
            fail("TRANSCRIPT_PROXY_PROVIDER=webshare needs credentials.",
                 hint="Set TRANSCRIPT_PROXY_USERNAME and TRANSCRIPT_PROXY_PASSWORD, "
                      "or unset TRANSCRIPT_PROXY_PROVIDER to connect directly.")
        from youtube_transcript_api.proxies import WebshareProxyConfig
        return WebshareProxyConfig(proxy_username=username, proxy_password=password), "webshare"

    if provider == "generic":
        http_url = get_env("TRANSCRIPT_PROXY_HTTP_URL")
        https_url = get_env("TRANSCRIPT_PROXY_HTTPS_URL") or http_url
        if not (http_url or https_url):
            fail("TRANSCRIPT_PROXY_PROVIDER=generic needs a proxy URL.",
                 hint="Set TRANSCRIPT_PROXY_HTTP_URL (and optionally TRANSCRIPT_PROXY_HTTPS_URL).")
        from youtube_transcript_api.proxies import GenericProxyConfig
        return GenericProxyConfig(http_url=http_url, https_url=https_url), "generic"

    fail(f"Unknown TRANSCRIPT_PROXY_PROVIDER: {provider!r}",
         hint="Use 'webshare', 'generic', or unset it for a direct connection.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--videos-file", default=None, help="Default .tmp/videos.json")
    parser.add_argument("--top", type=int, default=120, help="How many videos to transcribe")
    parser.add_argument("--rank-by", default="views_per_day",
                        choices=["views_per_day", "views", "engagement_rate"])
    parser.add_argument("--max-chars", type=int, default=24000, help="Truncate each transcript")
    parser.add_argument("--languages", default="en", help="Comma-separated preference order")
    parser.add_argument("--min-delay", type=float, default=0.6, help="Throttle floor, seconds")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    from youtube_transcript_api import YouTubeTranscriptApi

    videos_path = pathlib.Path(args.videos_file) if args.videos_file else tmp_path("videos.json")
    if not videos_path.exists():
        fail(f"Videos file not found: {videos_path}", hint="Run: python tools/youtube_fetch_videos.py")

    videos = read_json(videos_path)
    videos.sort(key=lambda v: v.get(args.rank_by, 0), reverse=True)
    targets = videos[: args.top]
    languages = [lang.strip() for lang in args.languages.split(",") if lang.strip()]

    cache_dir = tmp_path("transcripts", ".keep").parent
    proxy_config, transport = build_proxy_config()
    if proxy_config:
        log.info("Routing transcript requests through the %s proxy.", transport)
    api = YouTubeTranscriptApi(proxy_config=proxy_config)

    results: list[dict] = []
    counts = {"fetched": 0, "cached": 0, "skipped": 0}
    skip_reasons: dict[str, int] = {}
    blocked_at: str | None = None

    for index, video in enumerate(targets, 1):
        vid = video["video_id"]
        cache_file = cache_dir / f"{vid}.json"

        if not args.no_cache and cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if cached.get("text"):
                    results.append({**_meta(video), **cached})
                    counts["cached"] += 1
                    continue
                skip_reasons[cached.get("error", "unknown")] = skip_reasons.get(cached.get("error", "unknown"), 0) + 1
                counts["skipped"] += 1
                continue
            except (json.JSONDecodeError, OSError):
                pass  # Corrupt cache entry — just refetch.

        try:
            fetched = api.fetch(vid, languages=languages)
            text = " ".join(snippet.text.replace("\n", " ") for snippet in fetched)
            text = " ".join(text.split())[: args.max_chars]
            payload = {"text": text, "language": getattr(fetched, "language_code", languages[0]),
                       "chars": len(text)}
            cache_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            results.append({**_meta(video), **payload})
            counts["fetched"] += 1
            if index % 20 == 0:
                log.info("  %d/%d transcripts", index, len(targets))

        except Exception as exc:
            name = type(exc).__name__

            if name in BLOCKING:
                blocked_at = name
                log.error("YouTube is blocking transcript requests (%s). Stopping early.", name)
                break

            if name in SKIPPABLE or "CouldNotRetrieve" in name:
                skip_reasons[name] = skip_reasons.get(name, 0) + 1
                counts["skipped"] += 1
                cache_file.write_text(json.dumps({"error": name}), encoding="utf-8")
            else:
                skip_reasons[name] = skip_reasons.get(name, 0) + 1
                counts["skipped"] += 1
                log.warning("%s on %s: %s", name, vid, str(exc)[:160])

        time.sleep(args.min_delay + random.uniform(0, 0.4))

    if blocked_at and not results:
        fail(
            f"YouTube blocked every transcript request ({blocked_at}).",
            hint=("This host's IP is blocked and the cache is empty, so there is nothing to "
                  "return. On a cloud runner (including GitHub Actions) this is expected: set "
                  "TRANSCRIPT_PROXY_PROVIDER=webshare with TRANSCRIPT_PROXY_USERNAME/PASSWORD. "
                  "On a residential connection, wait an hour — the block is a cooldown measured "
                  "in hours — and retry with a higher --min-delay."),
        )

    out = write_json(pathlib.Path(args.output) if args.output else tmp_path("transcripts.json"), results)

    emit({
        "transcripts": len(results),
        "attempted": len(targets),
        **counts,
        "skip_reasons": skip_reasons,
        "blocked": blocked_at,
        "transport": transport,
        "total_chars": sum(r.get("chars", 0) for r in results),
        "output_file": str(out),
    })


def _meta(video: dict) -> dict:
    return {
        "video_id": video["video_id"],
        "title": video.get("title", ""),
        "channel_title": video.get("channel_title", ""),
        "published_at": video.get("published_at", ""),
        "views": video.get("views", 0),
    }


if __name__ == "__main__":
    main()
