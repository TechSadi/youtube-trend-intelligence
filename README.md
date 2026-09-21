# YouTube Analysis — WAT framework

Workflows describe the job, the agent orchestrates, tools execute.
Agent-facing rules live in [CLAUDE.md](CLAUDE.md).

```
workflows/   Markdown SOPs — objective, inputs, tool sequence, edge cases
tools/       Python scripts — deterministic execution, JSON in / JSON out
scripts/     Operational helpers (run summaries) — not part of the pipeline
config/      What is tracked: seed channels, discovery queries, taxonomy
.github/     Scheduled deployment
.tmp/        Disposable intermediates (gitignored)
.env         Secrets — the only place they live (gitignored)
```

The pipeline runs two ways: **interactively**, where the agent reads a workflow
and calls tools one at a time, and **unattended**, where a scheduler calls one
orchestrator. Both run the same tools in the same order.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # then fill in your keys

python tools/preflight.py       # verify every credential before spending anything
```

`preflight.py` checks each key with a free or 1-unit call and tells you what is
missing, what will degrade, and what to do about it. Run it again after rotating
anything.

### Keys

| Key | Required | Without it |
|---|---|---|
| `YOUTUBE_API_KEY` | yes | Nothing runs |
| `GMAIL_APP_PASSWORD` + `REPORT_RECIPIENT` | yes | Nothing is delivered |
| `ANTHROPIC_API_KEY` | no | The narrative falls back to a deterministic template |
| `GITHUB_TOKEN` | no | GitHub search drops to 10 req/min; the adoption signal is partial |
| `TRANSCRIPT_PROXY_*` | no | Transcripts fail on cloud hosts (they work from a home connection) |

Scheduled runs need the **SMTP** send path, not OAuth — an OAuth grant cannot
refresh without a browser. See [`workflows/send_email_report.md`](workflows/send_email_report.md).

## Running a job

Unattended, one command:

```bash
python tools/run_pipeline.py --seeds-only          # the real thing
python tools/run_pipeline.py --dry-run             # builds the email, sends nothing
python tools/run_pipeline.py --no-send             # stops at .tmp/report.html
```

Interactively, ask the agent in plain language — it reads the matching workflow
and calls the tools that workflow names. To run one tool directly:

```bash
python tools/<name>.py --help
```

Every tool prints a JSON envelope — `{"ok": true, ...}` or
`{"ok": false, "error": ..., "hint": ...}`.

## Scheduling

[`.github/workflows/trend-report.yml`](.github/workflows/trend-report.yml) runs
it Mondays at 13:00 UTC, plus on demand with toggles for dry-run,
template-only and full discovery. Set the keys above as repository secrets —
except `GITHUB_TOKEN`, which GitHub reserves and injects automatically.

Every run writes `.tmp/runs/<timestamp>.json`: per-step status, duration, quota
spent, and any degradations. `python scripts/summarize_run.py` renders it.

Full setup, caching and failure handling: [`workflows/scheduled_run.md`](workflows/scheduled_run.md).

## Where judgment lives

One step in this pipeline is not deterministic: writing the report's narrative.
[`tools/author_report_spec.py`](tools/author_report_spec.py) sends the analysis
to Claude and gets back a report spec, validated against
[`tools/_spec_schema.py`](tools/_spec_schema.py) before the renderer sees it.

**The model supplies judgment, never structure.** A spec that breaks the
contract — an invented chart, a ragged table row, too little prose — gets one
retry with the validator's complaints, then falls back to the deterministic
template. The run does not fail because a sentence came out wrong.

## Workflows

- [`youtube_trend_report.md`](workflows/youtube_trend_report.md) — harvest YouTube +
  Hacker News + GitHub, analyze trends, email a designed HTML report.
- [`scheduled_run.md`](workflows/scheduled_run.md) — unattended runs, deployment, run logs.
- [`send_email_report.md`](workflows/send_email_report.md) — reusable Gmail delivery.

## Adding capability

1. Write the SOP first — copy `workflows/_TEMPLATE.md`.
2. Check `tools/` for something that already does the step.
3. Only then write a new tool, following the contract in [tools/README.md](tools/README.md).

To change *what* is tracked, edit `config/` — not the tools. Every knob worth
changing per-run also has a `SCOPE_*` environment override, so a scheduler can
tune a run without a commit.

Deliverables belong in the cloud. Anything in `.tmp/` can be deleted at any time,
with two exceptions worth keeping: `.tmp/transcripts/` (a cache that fills in
over weeks, because YouTube rate-limits it) and `.tmp/history/` (what makes
week-over-week comparison possible).
