# Tools — the execution layer

Deterministic Python scripts. No reasoning here: a tool takes explicit inputs,
does one job, and returns a parseable result.

## Contract

Every tool must:

1. **Be runnable from the project root** — `python tools/<name>.py --arg value`
2. **Parse args with `argparse`** and provide `--help`. No interactive prompts.
3. **Print one JSON object to stdout** via `emit()` / `fail()` from `_common.py`.
   Logs and progress go to stderr so stdout stays machine-readable.
4. **Exit 0 on success, non-zero on failure.**
5. **Read secrets only from `.env`/the environment** via `get_env(..., required=True)`.
   Never hardcode a key, never accept one as a CLI arg. A deploy target can only
   inject environment variables, and `load_env()` lets real env vars win over the
   file — so the same tool works locally and on a runner with no change.
6. **Write intermediates to `.tmp/`** via `tmp_path()`. Final deliverables go to
   cloud services (Sheets, Slides, Drive).
7. **Fail with a hint** — `fail(msg, hint=...)` should say what to do next.
8. **Be runnable unattended.** No prompts, no browser, no "press any key". If a
   tool needs an interactive grant, it also needs a non-interactive path, or it
   cannot be part of a scheduled run.

## Skeleton

```python
"""One line: what this tool does."""
import argparse, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import emit, fail, get_env, get_logger, tmp_path, write_json

log = get_logger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    api_key = get_env("YOUTUBE_API_KEY", required=True)
    ...
    out = write_json(tmp_path("videos.json"), results)
    emit({"count": len(results), "output_file": str(out)})


if __name__ == "__main__":
    main()
```

## Naming

`<verb>_<object>.py` — `fetch_channel_videos.py`, `export_to_sheet.py`.
A leading underscore means a shared module, not a runnable tool: `_common.py`,
`_youtube.py`, `_signals.py`, `_spec_schema.py`, `_spec_template.py`.

## The orchestration tools

Three tools are about running the others, not about doing work:

| Tool | Job |
|---|---|
| `run_pipeline.py` | Runs the full sequence, classifies each step critical or optional, writes a run log to `.tmp/runs/` |
| `preflight.py` | Validates every credential with free or 1-unit calls, before anything is spent |
| `author_report_spec.py` | The one non-deterministic step — Claude writes the narrative, validated against `_spec_schema.py`, falling back to `_spec_template.py` |

When you add a step to a pipeline, add it to `run_pipeline.py`'s step list and
decide there whether its failure should stop the run. A step whose failure
should not stop the run needs a `note` saying what is lost — that text is what
lands in the run log and, eventually, in front of a confused human.

## Configuration vs. secrets

Secrets live in `.env`. *Configuration* — what is tracked, how far back, how
many — lives in `config/`, loaded with `load_scope()`, which applies `SCOPE_*`
environment overrides on top of the file. That is what lets a scheduler change a
run's window without committing a file change. Precedence is CLI flag > env >
file.

## Rate limits and quotas

When a tool hits an API limit, fix the tool (batch endpoint, backoff, caching)
and record the constraint in the workflow that calls it — so the next run does
not rediscover it.
