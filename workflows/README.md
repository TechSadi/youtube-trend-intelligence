# Workflows — the instruction layer

Plain-language SOPs. Each file briefs the agent the way you would brief a
teammate: the goal, what it needs, which tools to run, what comes out, and what
to do when something breaks.

- One workflow per file, named for the job: `analyze_channel.md`, `weekly_report.md`.
- Start from `_TEMPLATE.md`.
- Workflows are **living documents**. When a run teaches you something — a rate
  limit, a quirk, a better sequence — write it into the workflow's *Lessons
  learned* section so it is never rediscovered.
- Do not overwrite an existing workflow without asking first. These are
  instructions to be refined, not scratch files.

## The workflows

| File | Job |
|---|---|
| [`youtube_trend_report.md`](youtube_trend_report.md) | Harvest YouTube + Hacker News + GitHub, analyze, email a designed HTML report |
| [`scheduled_run.md`](scheduled_run.md) | Run any workflow unattended: orchestrator, preflight, GitHub Actions, run logs |
| [`send_email_report.md`](send_email_report.md) | Reusable Gmail delivery for any workflow whose deliverable is an email |
