"""Run the whole trend-report pipeline end to end, unattended.

This is the entry point a scheduler calls. It runs each tool in order, decides
what a failure means, and writes a structured run log to .tmp/runs/ that says
exactly what happened — which is the only thing you get to look at when a
Tuesday-morning cron run goes wrong.

The step list is the same sequence the workflow documents, with one difference
that matters: **steps are classified critical or optional.** A critical step
failing ends the run with a non-zero exit. An optional step failing is recorded
as a degradation and the run continues, because a report missing its Hacker News
cross-check is worth far more than no report at all.

    critical   discover -> videos -> analyze -> author -> render -> send
    optional   transcripts, hackernews, github

Transcripts are optional by design, not by accident. YouTube blocks datacenter
IPs, so on a cloud runner that step fails every time; the analysis falls back to
titles, tags and descriptions, and the run log records that the corpus was
shallower than usual.

Exit codes:
    0  ran to completion (possibly degraded — check `degradations` in the log)
    1  a critical step failed
    2  bad invocation (missing credentials, nothing to do)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import (PROJECT_ROOT, get_env, get_logger, load_scope,
                     tmp_path, write_json)

log = get_logger(__name__)

TOOLS = pathlib.Path(__file__).resolve().parent


class Step:
    """One tool invocation and what its failure means to the run."""

    def __init__(self, name: str, script: str, args: list[str], *,
                 critical: bool = True, note: str = ""):
        self.name = name
        self.script = script
        self.args = args
        self.critical = critical
        self.note = note      # what is lost when an optional step fails

    @property
    def argv(self) -> list[str]:
        return [sys.executable, str(TOOLS / self.script), *self.args]


def run_step(step: Step, *, timeout: int) -> dict:
    """Execute one step. Returns a record; never raises for a tool-level failure."""
    log.info("--> %s", step.name)
    started = time.monotonic()

    # PYTHONUNBUFFERED keeps stderr progress interleaved correctly in a CI log;
    # PYTHONIOENCODING covers the child's streams the way _common covers ours.
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

    try:
        proc = subprocess.run(step.argv, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=PROJECT_ROOT, env=env)
    except subprocess.TimeoutExpired:
        log.error("    %s timed out after %ss", step.name, timeout)
        return {"step": step.name, "ok": False, "critical": step.critical,
                "error": f"timed out after {timeout}s", "seconds": round(time.monotonic() - started, 1)}

    elapsed = round(time.monotonic() - started, 1)

    # Every tool prints one JSON envelope on stdout. If it did not, something
    # crashed before emit() — keep the tail of stderr, that is the traceback.
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        tail = (proc.stderr or "").strip().splitlines()[-12:]
        log.error("    %s produced no JSON envelope (exit %s)", step.name, proc.returncode)
        return {"step": step.name, "ok": False, "critical": step.critical,
                "error": f"no JSON on stdout (exit {proc.returncode})",
                "stderr_tail": tail, "seconds": elapsed}

    record = {"step": step.name, "ok": bool(envelope.get("ok")), "critical": step.critical,
              "seconds": elapsed, "result": envelope}
    if not record["ok"]:
        record["error"] = envelope.get("error")
        record["hint"] = envelope.get("hint")
        log.error("    %s failed: %s", step.name, envelope.get("error"))
    else:
        log.info("    %s ok (%ss)", step.name, elapsed)
    return record


def build_steps(args, scope: dict) -> list[Step]:
    """The pipeline, in order. Optional steps are the cross-checks and transcripts."""
    transcript_top = args.transcript_top or int(scope.get("transcript_top_n", 30))

    discover_args = ["--seeds-only"] if args.seeds_only else []
    analyze_merged = ["--hn-file", str(tmp_path("hn_signals.json")),
                      "--github-file", str(tmp_path("github_signals.json"))]

    author_args = []
    if args.template_only:
        author_args.append("--template-only")

    render_args = ["--no-thumbnails"] if args.no_thumbnails else []

    send_args = ["--dry-run"] if args.dry_run else []
    if args.to:
        send_args += ["--to", args.to]

    steps = [
        Step("discover_channels", "youtube_discover_channels.py", discover_args),
        Step("fetch_videos", "youtube_fetch_videos.py", []),
        Step("fetch_transcripts", "youtube_fetch_transcripts.py",
             ["--top", str(transcript_top)],
             critical=False,
             note="analysis falls back to titles, tags and descriptions only"),
        # First pass ranks the technologies so the cross-checks know what to look up.
        Step("analyze_youtube_only", "analyze_trends.py", ["--no-snapshot"]),
        Step("fetch_hn_signals", "fetch_hn_signals.py", ["--top", str(args.signal_top)],
             critical=False, note="no Hacker News discussion in the adoption index"),
        Step("fetch_github_signals", "fetch_github_signals.py", ["--top", str(args.signal_top)],
             critical=False, note="no GitHub repo creation in the adoption index"),
        # Second pass merges whichever cross-checks succeeded, and writes the
        # history snapshot that makes week-over-week comparison possible.
        Step("analyze_with_signals", "analyze_trends.py", analyze_merged),
        Step("author_spec", "author_report_spec.py", author_args),
        Step("render_report", "render_report.py", render_args),
    ]

    if not args.no_send:
        steps.append(Step("send_email", "send_gmail.py", send_args))

    return steps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seeds-only", action="store_true",
                        help="Skip channel discovery searches (saves ~800 quota units). "
                             "Right for most scheduled runs — seeds rarely change week to week.")
    parser.add_argument("--transcript-top", type=int, default=None,
                        help="Videos to transcribe. Default from scope. ~30 is the realistic ceiling.")
    parser.add_argument("--signal-top", type=int, default=30,
                        help="Technologies to cross-check against HN and GitHub")
    parser.add_argument("--template-only", action="store_true",
                        help="Skip the Anthropic API and use the deterministic narrative")
    parser.add_argument("--no-thumbnails", action="store_true",
                        help="Smaller email; skips video thumbnail downloads")
    parser.add_argument("--to", default=None, help="Override REPORT_RECIPIENT")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build the message and write the .eml, but do not send")
    parser.add_argument("--no-send", action="store_true",
                        help="Stop after rendering. Leaves .tmp/report.html for inspection.")
    parser.add_argument("--step-timeout", type=int, default=1800,
                        help="Per-step timeout in seconds (default 1800)")
    parser.add_argument("--skip", action="append", default=[],
                        help="Step name to skip. Repeatable.")
    args = parser.parse_args()

    started_at = dt.datetime.now(dt.timezone.utc)
    scope = load_scope()

    # Fail before spending any quota if the run cannot possibly finish.
    if not get_env("YOUTUBE_API_KEY"):
        print(json.dumps({"ok": False, "error": "YOUTUBE_API_KEY is not set.",
                          "hint": "Add it to .env, or set it as a secret in the deploy environment. "
                                  "Run: python tools/preflight.py"}, indent=2))
        sys.exit(2)
    if not args.no_send and not (args.to or get_env("REPORT_RECIPIENT")):
        print(json.dumps({"ok": False, "error": "No recipient for the finished report.",
                          "hint": "Set REPORT_RECIPIENT, pass --to, or pass --no-send."}, indent=2))
        sys.exit(2)

    steps = [s for s in build_steps(args, scope) if s.name not in args.skip]

    records: list[dict] = []
    degradations: list[dict] = []
    failed: dict | None = None

    for step in steps:
        record = run_step(step, timeout=args.step_timeout)
        records.append(record)

        if record["ok"]:
            continue
        if step.critical:
            failed = record
            log.error("Critical step %s failed — stopping.", step.name)
            break
        degradations.append({"step": step.name, "error": record.get("error"), "impact": step.note})
        log.warning("Optional step %s failed — continuing. Impact: %s", step.name, step.note)

    # A degraded success is still worth recording in detail: it is how you find
    # out the transcript cache has been empty for three weeks.
    finished_at = dt.datetime.now(dt.timezone.utc)
    quota = sum(r["result"].get("quota_units_spent", 0)
                for r in records if r["ok"] and isinstance(r.get("result"), dict))

    run_log = {
        "ok": failed is None,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "seconds": round((finished_at - started_at).total_seconds(), 1),
        "invocation": vars(args),
        "scope_env_overrides": scope.get("_env_overrides", {}),
        "youtube_quota_units": quota,
        "steps": records,
        "degradations": degradations,
        "failed_step": failed["step"] if failed else None,
    }

    stamp = started_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    log_path = write_json(tmp_path("runs", f"{stamp}.json"), run_log)
    write_json(tmp_path("runs", "latest.json"), run_log)

    summary = {
        "ok": failed is None,
        "seconds": run_log["seconds"],
        "youtube_quota_units": quota,
        "steps_run": len(records),
        "degradations": degradations,
        "run_log": str(log_path),
    }

    # A dry run's envelope carries `dry_run: true` and no `sent` key. Collapsing
    # the two into one truthy field would put "sent: true" in the log of a run
    # that delivered nothing — keep them distinct.
    send = next((r for r in records if r["step"] == "send_email" and r["ok"]), None)
    if send:
        envelope = send["result"]
        if envelope.get("dry_run"):
            summary["delivery"] = "dry-run"
            summary["eml_file"] = envelope.get("eml_file")
            summary["unresolved_cids"] = envelope.get("unresolved_cids")
        else:
            summary["delivery"] = "sent" if envelope.get("sent") else "unknown"
            summary["message_id"] = envelope.get("message_id")
        summary["recipients"] = envelope.get("to")
    elif args.no_send:
        summary["delivery"] = "skipped (--no-send)"

    author = next((r for r in records if r["step"] == "author_spec" and r["ok"]), None)
    if author:
        summary["narrative_source"] = author["result"].get("source")
        summary["subject"] = author["result"].get("subject")

    if failed:
        summary["error"] = f"{failed['step']}: {failed.get('error')}"
        summary["hint"] = failed.get("hint")
        if failed.get("stderr_tail"):
            summary["stderr_tail"] = failed["stderr_tail"]

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    sys.exit(0 if failed is None else 1)


if __name__ == "__main__":
    main()
