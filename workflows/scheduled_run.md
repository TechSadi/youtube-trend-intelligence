# Scheduled & Deployed Runs

**Objective**
Run a workflow end to end with nobody watching, on a schedule, and know what
happened afterwards.

**When to use this**
Setting the pipeline up on a schedule, changing the cadence, rotating a
credential, or working out why Monday's report never arrived.

---

## The one command

```bash
python tools/run_pipeline.py --seeds-only
```

Everything else here is about where that runs and what to do when it does not.

`run_pipeline.py` executes the same sequence as
[`youtube_trend_report.md`](youtube_trend_report.md), with one addition that
only matters unattended: each step is **critical** or **optional**.

| | Steps | On failure |
|---|---|---|
| Critical | discover, videos, analyze ×2, author, render, send | Run stops, exit 1 |
| Optional | transcripts, Hacker News, GitHub | Recorded as a degradation, run continues |

That split is the whole design. A report missing its Hacker News cross-check
still tells you what to learn next; a run that hard-fails on it tells you
nothing.

### Useful flags

| Flag | Why |
|---|---|
| `--seeds-only` | Skip discovery searches. Saves ~800 of 10,000 daily quota units. Right for almost every scheduled run — seed channels do not change weekly. |
| `--dry-run` | Build the message, write the `.eml`, send nothing. |
| `--no-send` | Stop after rendering. Leaves `.tmp/report.html`. |
| `--template-only` | Skip the Anthropic API. Free, deterministic prose. |
| `--skip <step>` | Repeatable. Reuses whatever is already in `.tmp/`, which makes iterating on the last two steps fast. |

## Before the first scheduled run

```bash
python tools/preflight.py
```

Checks every credential without spending anything meaningful: a 1-unit YouTube
probe, a free GitHub rate-limit call, a free Anthropic model lookup, and an SMTP
login that disconnects without sending. Exit 0 means the run can complete.
Warnings mean it will complete in a degraded form, and name which.

Run it again after rotating any credential.

## Deploying to GitHub Actions

[`.github/workflows/trend-report.yml`](../.github/workflows/trend-report.yml)
runs Mondays at 13:00 UTC, and on demand via **Actions → Weekly trend report →
Run workflow** (with toggles for dry run, template-only and full discovery).

### Secrets to set

Settings → Secrets and variables → Actions:

| Secret | Required | Notes |
|---|---|---|
| `YOUTUBE_API_KEY` | yes | |
| `GMAIL_APP_PASSWORD` | yes | App password, not the account password |
| `REPORT_RECIPIENT` | yes | |
| `GMAIL_ADDRESS` | if sender ≠ recipient | |
| `ANTHROPIC_API_KEY` | no | Without it every report uses template prose |
| `TRANSCRIPT_PROXY_*` | no | See the transcripts section below |

**Do not create a `GITHUB_TOKEN` secret — GitHub rejects the name.** The
`GITHUB_` prefix is reserved. The workflow uses the token Actions injects
automatically, which is already authenticated and gets the 30 req/min search
rate rather than the anonymous 10.

### What the runner keeps between runs

`.tmp/` is destroyed with the runner, so the workflow caches the two directories
that are not disposable:

- **`.tmp/transcripts/`** — YouTube rate-limits transcripts at roughly 30 per
  session, so the corpus is designed to fill in over many weeks. Losing this
  cache every run means permanently shallow analysis.
- **`.tmp/history/`** — the per-run snapshots that make week-over-week
  comparison possible at all.

The cache key carries the run id (a key must be unique to write a new entry) and
`restore-keys: trend-state-` falls back to the most recent previous cache.

### Transcripts on a cloud runner

They will fail, and that is expected. YouTube blocks datacenter IPs — AWS, GCP,
Azure, Vercel, Colab, and Actions runners. The step is optional, so the run
continues and the analysis falls back to titles, tags and descriptions. The run
log records the degradation.

To fix it properly, add a residential proxy:

```
TRANSCRIPT_PROXY_PROVIDER=webshare
TRANSCRIPT_PROXY_USERNAME=...
TRANSCRIPT_PROXY_PASSWORD=...
```

Worth knowing before paying for one: transcripts add depth to the analysis, not
breadth. The technology ranking is driven mostly by titles and tags. If the
reports read well without it, it is not worth the subscription.

## After a run

Every run writes `.tmp/runs/<timestamp>.json` and `.tmp/runs/latest.json`:
per-step status, duration, the tool's own JSON envelope, quota spent, and any
degradations. In CI both are uploaded as an artifact alongside the rendered
report, kept 30 days.

```bash
python scripts/summarize_run.py          # readable Markdown summary
```

In Actions this is the job summary, so a failed run explains itself on the run
page without downloading anything.

**Failure reporting is log-only by choice.** The job goes red, GitHub emails the
repository owner, and the artifact holds the detail. Nothing else is wired up —
adding a failure email would mean the alerting path shares a credential with the
thing most likely to have broken.

## Cadence

Weekly fits the data. The window is 60 days, so consecutive runs overlap by
~85% and momentum stays stable instead of swinging on small samples. Daily runs
would mostly re-report the same numbers and spend quota doing it.

GitHub cron is best-effort — queued runs can start 10-30 minutes late — and
**scheduled workflows are disabled automatically after 60 days with no
repository activity.** A quiet repo silently stops reporting; if the emails
stop, check that before anything else.

## Edge cases

| Situation | What to do |
|---|---|
| Report never arrived, job is green | Check `delivery` in the run log. `dry-run` means a toggle was left on. |
| Job red at `preflight` | A secret is missing or expired. The check name and hint say which. Nothing was spent. |
| Job red at `send_email` | Usually a rotated app password. `preflight.py --only email` confirms in seconds. |
| `quotaExceeded` | Another run already spent the day's units. Resets midnight Pacific. Confirm `--seeds-only` is set. |
| Narrative is `template` when a key is set | The API call failed and degraded silently. `degraded` in the `author_spec` envelope holds the exception. |
| Every step degraded | Almost always missing secrets. `preflight.py` runs first in CI to catch exactly this. |
| Schedule silently stopped | 60 days of repository inactivity disables it. Push any commit and re-enable in the Actions tab. |

## Lessons learned

- **2026-09-21** — GitHub reserves the `GITHUB_` secret prefix, so the obvious
  setup step ("add your PAT as `GITHUB_TOKEN`") is impossible. The automatic
  token is authenticated and works for the public-repo search this pipeline
  does, so the cleanest answer is to use it and document why.
- **2026-09-21** — A cache key that does not change never writes a new entry.
  Keying on `github.run_id` with a `restore-keys` prefix is what makes a
  read-modify-write cache actually accumulate across runs.
- **2026-09-21** — `preflight.py` runs *before* the pipeline in CI, not after a
  failure. The expensive failure mode is harvesting 2,000 videos, burning quota,
  rendering a report, and then dying on the send because an app password was
  rotated. All of that is avoidable with four cheap checks up front.
- **2026-09-21** — Tool stdout must be forced to UTF-8. This pipeline's prose is
  full of em-dashes; on a Windows console (cp1252) an unguarded `print` raises
  `UnicodeEncodeError` and takes the step down. `_common.py` reconfigures both
  streams at import, and `run_pipeline.py` sets `PYTHONIOENCODING` for children.
- **2026-09-21** — A run summary that collapses "sent" and "dry run" into one
  truthy field will eventually claim a report was delivered when it was not.
  Delivery state is reported as an explicit string.
