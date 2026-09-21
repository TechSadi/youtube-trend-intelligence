"""Turn .tmp/runs/latest.json into a Markdown summary.

Written for the GitHub Actions job summary, but it prints to stdout so it is
equally useful locally — `python scripts/summarize_run.py` after any run.

Deliberately dependency-free and defensive: this runs with `if: always()`, so
its whole job is to explain a failure, and it must not add one of its own.
"""

from __future__ import annotations

import json
import pathlib
import sys

# Standalone by design — it does not import _common, so it repeats the UTF-8
# guard. Report prose is full of em-dashes and a cp1252 console would either
# garble them or raise mid-summary.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

DEFAULT_LOG = pathlib.Path(".tmp/runs/latest.json")


def main() -> None:
    path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOG

    if not path.exists():
        print("## Trend report — no run log")
        print()
        print(f"`{path}` does not exist. The pipeline died before it could write one; "
              "check the step logs above.")
        return

    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print("## Trend report — unreadable run log")
        print()
        print(f"`{path}`: {type(exc).__name__}: {exc}")
        return

    print(f"## Trend report — {'succeeded' if run.get('ok') else 'FAILED'}")
    print()
    print(f"- Duration: **{run.get('seconds', '?')}s**")
    print(f"- YouTube quota: **{run.get('youtube_quota_units', 0)}** of 10,000")

    if run.get("scope_env_overrides"):
        print(f"- Scope overrides: `{run['scope_env_overrides']}`")

    for step in run.get("steps", []):
        if step["step"] == "author_spec" and step.get("ok"):
            result = step.get("result", {})
            source = result.get("source")
            line = f"- Narrative: **{source}**"
            if result.get("usage", {}).get("estimated_cost_usd") is not None:
                line += f" (~${result['usage']['estimated_cost_usd']:.3f})"
            if result.get("degraded"):
                line += f" — fell back: {result['degraded']}"
            print(line)
            if result.get("subject"):
                print(f"- Subject: {result['subject']}")

    if run.get("failed_step"):
        print(f"- Failed at: **`{run['failed_step']}`**")

    for degradation in run.get("degradations", []):
        print(f"- Degraded: `{degradation['step']}` — {degradation.get('impact') or degradation.get('error')}")

    print()
    print("| Step | Result | Seconds |")
    print("|---|---|---|")
    for step in run.get("steps", []):
        print(f"| `{step['step']}` | {'ok' if step.get('ok') else '**FAILED**'} | {step.get('seconds', '?')} |")

    failed = [s for s in run.get("steps", []) if not s.get("ok") and s.get("critical")]
    if failed:
        step = failed[0]
        print()
        print("### Failure detail")
        print()
        print(f"**{step['step']}**: {step.get('error')}")
        if step.get("hint"):
            print()
            print(f"> {step['hint']}")
        if step.get("stderr_tail"):
            print()
            print("```")
            for line in step["stderr_tail"]:
                print(line)
            print("```")


if __name__ == "__main__":
    main()
