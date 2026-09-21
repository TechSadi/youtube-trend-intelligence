# <Workflow name>

**Objective**
What this produces and why it matters. One or two sentences.

**When to use this**
The trigger — a request phrasing, a schedule, an upstream event.

---

## Inputs

| Input | Required | Description | Example |
|---|---|---|---|
| `channel_url` | yes | Channel to analyze | `https://youtube.com/@example` |
| `date_range` | no | Defaults to last 90 days | `2026-01-01..2026-03-31` |

If a required input is missing, ask for it. Do not guess.

## Credentials

Which `.env` keys this needs (e.g. `YOUTUBE_API_KEY`). If one is missing, stop
and tell the user which key to add — do not work around it.

## Steps

1. **<Step name>** — run `python tools/<tool>.py --arg value`
   - Produces: `.tmp/<file>.json`
   - Check: what a good result looks like
2. **<Step name>** — ...
3. **Deliver** — write the final output to the cloud (Sheet / Slides / Drive)
   and return the link.

## Output

Where the deliverable lives and what it contains. Local files in `.tmp/` are
intermediates only.

## Edge cases

| Situation | What to do |
|---|---|
| Channel has no videos in range | Report it, do not produce an empty deliverable |
| API quota exceeded | Stop, report remaining quota, do not retry in a loop |
| Tool exits non-zero | Read the `hint` field, fix the tool, retest, then update this workflow |

## Lessons learned

*Append as you go — dated notes on rate limits, timing, API quirks.*

- YYYY-MM-DD — <what broke, what fixed it>
