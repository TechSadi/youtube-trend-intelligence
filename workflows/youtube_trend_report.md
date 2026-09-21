# YouTube Tech-Trend Intelligence Report

**Objective**
Harvest recent videos from the software-development and AI/ML corner of YouTube,
cross-check what they are pushing against Hacker News discussion and GitHub
adoption, and deliver a designed HTML email that says what is rising, what is
cooling, what is hype, and what is worth learning next.

**When to use this**
"Send me the trend report", "what's trending in dev/AI right now", or on a
schedule (weekly is the natural cadence — the window is 60 days, so weekly runs
overlap and the momentum numbers stay stable).

**Two ways to run it.** Unattended, one command:

    python tools/run_pipeline.py --seeds-only

That is what the scheduler calls, and what `workflows/scheduled_run.md`
documents. The step-by-step below is for interactive work — developing a
change, debugging a bad report, or when you want to read the numbers yourself
before anything is sent. The sequences are identical; the orchestrator just
decides for itself what a failure means.

---

## Inputs

| Input | Required | Description | Default |
|---|---|---|---|
| `config/scope.json` | yes | Seed channels, discovery queries, window, recipient | in repo |
| `config/tech_taxonomy.json` | yes | ~130 technologies with aliases and context gates | in repo |
| `lookback_days` | no | Analysis window | 60 |
| `transcript_top_n` | no | How many videos get transcribed | 120 |

To change *what* is tracked, edit the config files — not the tools.

## Credentials

| Key | Needed for | If missing |
|---|---|---|
| `YOUTUBE_API_KEY` | discovery + video harvest | Stop. Cloud Console → enable YouTube Data API v3 → create API key. |
| `GITHUB_TOKEN` | adoption signal | Degrades: unauthenticated search is 10 req/min, so the run is slow and partial. |
| `GMAIL_APP_PASSWORD` | sending | Stop. See `send_email_report.md`. Required for unattended runs — OAuth cannot refresh without a browser. |
| `REPORT_RECIPIENT` | sending | Pass `--to` instead. |
| `ANTHROPIC_API_KEY` | the narrative | Degrades: `author_report_spec.py` falls back to a deterministic template. Correct numbers, same sentences every week. |

Check all of them before a run, without spending quota:

    python tools/preflight.py

## Quota budget

YouTube Data API v3 gives 10,000 units/day, resetting midnight Pacific.

| Step | Cost |
|---|---|
| `search.list` (discovery) | **100 units each** — capped by `max_discovery_searches` |
| `channels.list` | 1 unit per call (50 ids, or 1 handle) |
| `playlistItems.list` | 1 unit per 50 videos |
| `videos.list` | 1 unit per 50 videos |

Default config ≈ **800–1,000 units**, under 10% of the daily allowance. Every
tool reports `quota_units_spent`; sum them if you run repeatedly in a day.

Use `--seeds-only` on discovery to skip all search cost on reruns — the seed
list rarely needs re-discovery more than monthly.

## Steps

1. **Discover channels** — `python tools/youtube_discover_channels.py`
   - Produces: `.tmp/channels.json`
   - Check: `channels` ≈ 30–70, `unresolved_seeds` empty. A handle that fails to
     resolve has usually been renamed — fix it in `config/scope.json`.
   - Rerun cheaply with `--seeds-only`.

2. **Harvest videos** — `python tools/youtube_fetch_videos.py`
   - Produces: `.tmp/videos.json`
   - Check: several hundred to a few thousand videos, `quota_units_spent` under ~150.
   - Smoke-test first with `--limit-channels 3 --max-per-channel 5`.

3. **Fetch transcripts** — `python tools/youtube_fetch_transcripts.py --top 120`
   - Produces: `.tmp/transcripts.json`, cached per video in `.tmp/transcripts/`
   - Check: `blocked` is `null`. Some `skipped` is normal — plenty of channels
     disable captions.
   - Free, no quota. Slowest step (~2–4 min for 120 videos) because it throttles.

4. **Analyze, YouTube only** — `python tools/analyze_trends.py`
   - Produces: `.tmp/analysis.json`
   - Check: scan `top_10` for nonsense. A generic word matching as a technology
     means the taxonomy needs a `context_aliases` entry.

5. **Cross-check** — run both:
   - `python tools/fetch_hn_signals.py --top 30`
   - `python tools/fetch_github_signals.py --top 30`
   - Produce: `.tmp/hn_signals.json`, `.tmp/github_signals.json`

6. **Re-analyze with signals merged** —
   `python tools/analyze_trends.py --hn-file .tmp/hn_signals.json --github-file .tmp/github_signals.json`
   - Check: `sources_merged` shows both `true`; rows now carry `verdict`.

7. **Author the report spec** — `python tools/author_report_spec.py`
   - Produces: `.tmp/report_spec.json`
   - Claude reads a compacted evidence payload and writes the narrative:
     headline, prose, which charts, the recommendations. With no
     `ANTHROPIC_API_KEY` it falls back to `_spec_template.py`.
   - Check: `source` is `claude` (not `template`) if you expected the API path,
     and `body_words` is in the 500-700 range.
   - The spec is validated against `tools/_spec_schema.py` before it is written,
     so `render_report.py` can never receive a shape it cannot lay out. A model
     that breaks the contract gets one retry with the validator's complaints fed
     back, then the template takes over.
   - Use `--require-llm` when testing the API path — it turns the silent
     fallback into a hard failure.

   *The agent can still write this file by hand* when a particular edition needs
   human judgment. Just write `.tmp/report_spec.json` and skip this step.

8. **Render** — `python tools/render_report.py --open`
   - Produces: `.tmp/report.html` (cid: refs), `.tmp/charts/preview.html`
     (browser-viewable), `.tmp/report_assets.json`
   - Check: open the preview, confirm no label collisions and no overflow.

9. **Preview the email** — `python tools/send_gmail.py --dry-run`
   - Check: `unresolved_cids` is empty and the MIME tree is
     `multipart/alternative → text/plain + multipart/related → text/html + images`.

10. **Send** — `python tools/send_gmail.py`
    - Confirm with the user before this step when running interactively. It is
      not reversible. A scheduled run has standing approval by virtue of being
      scheduled — that is what `run_pipeline.py` assumes.

## Output

An HTML email in the recipient's inbox, with inline charts. All local files in
`.tmp/` are intermediates and can be deleted; `.tmp/history/<date>.json`
snapshots are the exception worth keeping — they are what makes true
week-over-week comparison possible on later runs.

## Edge cases

| Situation | What to do |
|---|---|
| `quotaExceeded` | Stop. Resets midnight Pacific. Rerun with `--seeds-only` to skip the 100-unit searches. |
| Transcripts all blocked | The IP is blocked. Works from residential connections; fails on cloud VMs. Raise `--min-delay`, or configure `WebshareProxyConfig`. |
| A seed handle won't resolve | The channel was renamed or deleted. Update `config/scope.json`. |
| A common English word ranks as a technology | Move its alias into `context_aliases` in the taxonomy, or add `requires_context`. |
| No videos in the window | Widen `lookback_days`. Do not send an empty report. |
| Gmail 403 on send | The Gmail API is not enabled on that Cloud project, or `token.json` predates the `gmail.send` scope. Delete `token.json` and re-authorize. |
| Message over 25 MB | Drop thumbnails (`--no-thumbnails`) or lower `DPI` in `render_report.py`. |
| Report arrives with template prose week after week | `ANTHROPIC_API_KEY` is missing or rejected. Check `narrative_source` in the run log and run `python tools/preflight.py --only anthropic`. |
| Scheduled run red, no report | Read `.tmp/runs/latest.json` (downloadable as a workflow artifact), or `python scripts/summarize_run.py`. `failed_step` names the tool and `hint` says what to do. |
| Everything degrades at once on a runner | Almost always a missing secret. `preflight.py` runs first in CI precisely to catch this before quota is spent. |

## Lessons learned

- **2026-09-21** — `search.list` costs 100 units against a 10,000/day budget, so
  only ~100 searches/day exist. Harvesting through `playlistItems.list` on each
  channel's uploads playlist costs ~1 unit per 50 videos instead. The pipeline is
  built around that difference; do not "simplify" it back to search.
- **2026-09-21** — YouTube blocks datacenter IPs for transcripts (AWS, GCP,
  Azure, Vercel, Colab). Verified working from this local Windows machine.
  **Moving this pipeline to a cloud VM will break step 3** and require
  residential proxies.
- **2026-09-21** — Context-gating alias matching must use word boundaries, not
  substrings: `"script"` is inside `"description"`, which switched the context
  gate on for essentially every video and let `go` match "here we go again".
  Caught by a synthetic trap-phrase test before any real data was pulled.
- **2026-09-21** — **Transcripts rate-limit at roughly 35 requests even from a
  residential IP.** A 150-video run got `IpBlocked` after ~36 fetches, and the
  block persisted through a retry at `--min-delay 6.0` minutes later, so it is
  a cooldown measured in hours, not seconds. Practical approach: treat ~30
  transcripts per session as the budget, let the per-video cache accumulate
  across days, and rank by `views_per_day` so the ones you do get are the most
  influential videos. Do not raise `--top` expecting more — raise it expecting
  the cache to fill in over several runs.
- **2026-09-21** — **Video descriptions are mostly link furniture.** Counting
  raw description text put Git at 148 videos, almost entirely from `github.com`
  URLs and "check out my GitHub" boilerplate. Two fixes: strip URLs before
  matching, and treat ambiguous aliases (`github`, `go`, `spark`, `cursor`) as
  title/tags-only signals. Git fell to a believable 52. **The context-word gate
  alone does nothing in this corpus** — every video is a software video, so the
  gate is always satisfied; *position in the document* is the real
  discriminator.
- **2026-09-21** — A discovered channel can have a dead uploads playlist
  (`playlistNotFound`) and killed the entire 102-channel harvest. `execute()`
  now takes `soft_errors` so a single bad resource is skipped and reported in
  `skipped_channels` instead of aborting the run.
- **2026-09-21** — Considered YouTube MCP servers and rejected them: they wrap
  the same two APIs but stream results through the model's context one call at a
  time. A Python tool returning one JSON file is cheaper and repeatable.
- **2026-09-21** — Momentum compares the recent half of the window to the prior
  half using *share* of videos, not raw counts. This makes the first run
  meaningful with no history, and stays honest when upload volume changes.
- **2026-09-21** — The pipeline could not be scheduled because step 7 was the
  agent writing prose in-session. `author_report_spec.py` closes that gap with
  the Anthropic API, and `_spec_schema.py` makes the handoff safe: the spec is
  validated before `render_report.py` sees it, so a hallucinated chart id or a
  ragged table row degrades to the template instead of crashing the run. The
  general shape — **let the model supply judgment, never structure** — is worth
  reusing for any other step that needs authoring.
- **2026-09-21** — Classifying steps *critical* vs *optional* is what makes
  unattended runs worth having. All three cross-checks (transcripts, HN, GitHub)
  are optional: a report missing its Hacker News column still tells you what to
  learn, and a run that hard-fails on it tells you nothing. Degradations go in
  the run log so a quietly shallower report is still visible.
- **2026-09-21** — `momentum_pct` is clamped at +300 by `analyze_trends.py`, and
  several technologies routinely sit exactly on the clamp. Rendering that as a
  precise "+300%" implies precision the number does not have, and picking a
  headline among tied rows by dictionary order is arbitrary. The template now
  prints ">+300%" and breaks ties by breadth (channels, then videos).
