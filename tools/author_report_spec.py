"""Author the report narrative from the analysis — the step that used to need a human.

Everything else in this pipeline is deterministic. This step is judgment: read
the numbers, decide what the story is, say it in ~600 words. On an interactive
run the agent did it in-session, which is exactly why the pipeline could not be
scheduled.

Two paths, and the tool never dies on this step:

    ANTHROPIC_API_KEY set   -> Claude reads a compacted evidence payload and
                               returns a spec through structured outputs, so the
                               shape is guaranteed and only the judgment varies
    no key, or the call fails -> _spec_template.py builds a spec from the same
                               data with fixed prose

Either way the result goes through validate_spec() before it is written, so
render_report.py never receives a spec it cannot lay out. A model that invents a
chart id or forgets a table cell gets one retry with the validator's complaints
fed back to it, then the template takes over.

The evidence payload is compacted on purpose: analysis.json is ~400 KB, most of
it weekly counts and thumbnail URLs. Sending it whole would cost more and read
worse than sending the 30 rows that carry the story.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import emit, fail, get_env, get_logger, read_json, tmp_path, write_json
from _spec_schema import JSON_SCHEMA, validate_spec
from _spec_template import build_spec as build_template_spec

log = get_logger(__name__)

DEFAULT_MODEL = "claude-opus-5"

# Opus 5, USD per million tokens. Only used for the cost line in the envelope —
# wrong prices here cost nothing but an inaccurate log entry.
PRICE_IN, PRICE_OUT = 5.00, 25.00

SYSTEM = """You are the analyst behind a weekly tech-trend intelligence email. \
The reader is a working software engineer who reads it to decide what to learn \
next, and who has said plainly that earlier editions were too long and too \
focused on industry hype.

Editorial rules, in priority order:

1. ~600 words of prose across the whole report. This is a hard target, not a \
floor. Six or seven sections.
2. Every claim carries a number from the evidence, and every number you write \
must appear in the evidence. Never estimate, extrapolate or round into a \
different story. If the evidence does not support a claim, drop the claim.
3. Prefer what changes a decision — what to learn, what to drop, where creator \
attention diverges from real adoption — over describing the landscape.
4. The attention-vs-adoption gap is the most valuable thing in the data. It is \
the one signal the reader cannot get anywhere else. Lead with it or near it.
5. Distrust anything ranked by views or popularity: that metric surfaces hype by \
construction. Do not build a section around most-viewed videos.
6. Frontier-model news — new model launches, "the end of programmers" takes — is \
noise to this reader. He sees it everywhere already. Skip it.
7. Breadth beats magnitude. A technology up 300% across two videos on one channel \
is one creator's content calendar. Say so rather than leading with it.
8. Write plainly. No "in today's fast-paced landscape", no exclamation marks, no \
hedging into meaninglessness. A flat week is a finding — say it is flat.

Formatting: **bold** and `code` work inside body text. The title is the claim \
this edition makes, not a label — "Reliability is the new frontier", never \
"Weekly Trend Report"."""

USER_TEMPLATE = """Here is this week's analysis. Write the report spec.

Available charts (use the `chart` section type; each may be used at most once):
  - `momentum` — diverging bar, which technologies are accelerating vs fading
  - `signal_vs_hype` — scatter, YouTube attention against HN/GitHub adoption
  - `timeline` — multi-series line over the last weeks
  - `reach` — ranked by views. Ranked by popularity, so it usually fails rule 5. \
Use it only if you have a specific reason.

Section types: `text` (heading/body/bullets), `chart`, `stats` (three big \
numbers), `cards` (recommendations), `table` (columns/rows), `videos` (needs \
video_id from the evidence).

EVIDENCE
--------
{evidence}
"""


def compact_evidence(analysis: dict, *, top: int = 30) -> dict:
    """Reduce analysis.json to the rows that carry the story.

    Drops weekly_counts, thumbnails and per-video detail. Keeps the fields a
    narrative can actually cite, plus the derived lists the template also uses.
    """
    keep = ("name", "category", "video_count", "channel_count", "recent_videos",
            "prior_videos", "momentum_pct", "status", "median_views_per_day")
    derived = ("attention_index", "adoption_index", "signal_gap", "verdict")

    def row(r: dict) -> dict:
        out = {k: r[k] for k in keep if k in r}
        out.update({k: r[k] for k in derived if k in r})
        if "hn" in r:
            out["hn_stories"] = r["hn"]["story_count"]
            out["hn_points"] = r["hn"]["total_points"]
        if "github" in r:
            out["github_new_repos"] = r["github"]["new_repos"]
        return out

    rows = analysis.get("technologies", [])
    ranked = [row(r) for r in rows[:top]]

    hype = sorted((r for r in rows if r.get("verdict") == "hype-leaning"),
                  key=lambda r: -r.get("signal_gap", 0))[:8]
    radar = sorted((r for r in rows if r.get("verdict") == "under-the-radar"),
                   key=lambda r: r.get("signal_gap", 0))[:8]

    return {
        "window": analysis["window"],
        "corpus": analysis["corpus"],
        "sources_merged": analysis.get("sources_merged", {}),
        "note": ("momentum_pct compares share of videos in the recent half of the window against the "
                 "prior half. signal_gap = attention_index - adoption_index; positive means more "
                 "YouTube airtime than real-world use. status 'new' means it rose from a base of "
                 "one or two videos."),
        "technologies_by_mention": ranked,
        "rising": [row(r) for r in analysis.get("by_momentum", [])[:12]],
        "cooling": [row(r) for r in analysis.get("cooling", [])[:10]],
        "over_covered": [row(r) for r in hype],
        "under_covered": [row(r) for r in radar],
        "category_totals": analysis.get("category_totals", [])[:10],
        "co_occurrence": analysis.get("top_pairs", [])[:10],
    }


def call_claude(evidence: dict, *, model: str, max_tokens: int, effort: str) -> tuple[dict, dict]:
    """Ask Claude for a spec. Returns (spec, usage). Raises on API failure.

    Structured outputs guarantee the response parses and matches the schema, so
    the only failure mode left is editorial — caught by validate_spec().
    """
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": USER_TEMPLATE.format(
        evidence=json.dumps(evidence, indent=2, ensure_ascii=False))}]

    def ask(msgs: list[dict]):
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM,
            messages=msgs,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": JSON_SCHEMA}},
        )

    response = ask(messages)
    text = next(b.text for b in response.content if b.type == "text")
    spec = json.loads(text)

    problems = validate_spec(spec)
    usage = {"input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens,
             "attempts": 1}

    if problems:
        # One retry with the complaints fed back. The model gets its own output
        # plus what was wrong with it, which fixes editorial misses (too little
        # prose, a table with a ragged row) that a schema cannot express.
        log.warning("Spec failed validation, retrying: %s", "; ".join(problems[:5]))
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": (
                "That spec did not pass validation:\n"
                + "\n".join(f"  - {p}" for p in problems)
                + "\n\nReturn the corrected spec in full. Keep everything that was fine.")},
        ]
        response = ask(messages)
        text = next(b.text for b in response.content if b.type == "text")
        spec = json.loads(text)
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        usage["attempts"] = 2

    usage["estimated_cost_usd"] = round(
        usage["input_tokens"] / 1e6 * PRICE_IN + usage["output_tokens"] / 1e6 * PRICE_OUT, 4)
    return spec, usage


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-file", default=None, help="Default .tmp/analysis.json")
    parser.add_argument("--output", default=None, help="Default .tmp/report_spec.json")
    parser.add_argument("--model", default=None, help=f"Default {DEFAULT_MODEL} or ANTHROPIC_MODEL")
    parser.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"],
                        help="Thinking depth. Default high.")
    parser.add_argument("--max-tokens", type=int, default=16000)
    parser.add_argument("--top", type=int, default=30, help="Technologies included in the evidence payload")
    parser.add_argument("--template-only", action="store_true",
                        help="Skip the API entirely and use the deterministic template")
    parser.add_argument("--require-llm", action="store_true",
                        help="Fail instead of falling back to the template. Use when testing the API path.")
    args = parser.parse_args()

    analysis_path = pathlib.Path(args.analysis_file) if args.analysis_file else tmp_path("analysis.json")
    if not analysis_path.exists():
        fail(f"Analysis not found: {analysis_path}", hint="Run: python tools/analyze_trends.py")
    analysis = read_json(analysis_path)

    if not analysis.get("technologies"):
        fail("Analysis contains no ranked technologies.",
             hint="Widen lookback_days, or lower --min-videos on analyze_trends.py. "
                  "Do not send an empty report.")

    api_key = get_env("ANTHROPIC_API_KEY")
    model = args.model or get_env("ANTHROPIC_MODEL") or DEFAULT_MODEL
    effort = args.effort or get_env("ANTHROPIC_EFFORT") or "high"

    source, usage, degraded = "template", {}, None

    if args.template_only:
        spec = build_template_spec(analysis)
    elif not api_key:
        if args.require_llm:
            fail("ANTHROPIC_API_KEY is not set and --require-llm was passed.",
                 hint="Add ANTHROPIC_API_KEY= to .env, or drop --require-llm to use the template.")
        log.warning("No ANTHROPIC_API_KEY — using the deterministic template.")
        degraded = "ANTHROPIC_API_KEY not set"
        spec = build_template_spec(analysis)
    else:
        evidence = compact_evidence(analysis, top=args.top)
        try:
            spec, usage = call_claude(evidence, model=model, max_tokens=args.max_tokens, effort=effort)
            source = "claude"
        except Exception as exc:  # noqa: BLE001 — any API failure degrades to the template
            if args.require_llm:
                fail(f"Claude call failed: {type(exc).__name__}: {exc}",
                     hint="Check ANTHROPIC_API_KEY and the model id. Drop --require-llm to fall back.")
            log.warning("Claude call failed (%s), falling back to template: %s", type(exc).__name__, exc)
            degraded = f"{type(exc).__name__}: {exc}"
            spec = build_template_spec(analysis)

    problems = validate_spec(spec)
    if problems and source == "claude":
        # Retry already happened inside call_claude; a still-invalid spec means
        # the template is the safer thing to render.
        if args.require_llm:
            fail("Claude returned a spec that failed validation twice: " + "; ".join(problems),
                 hint="Inspect the evidence payload, or relax the editorial rules in SYSTEM.")
        log.warning("Claude spec still invalid after retry, falling back to template.")
        degraded = "spec failed validation after retry: " + "; ".join(problems[:3])
        source, spec = "template", build_template_spec(analysis)
        problems = validate_spec(spec)

    if problems:
        fail("Report spec failed validation: " + "; ".join(problems),
             hint="This is a bug in _spec_template.py — the deterministic path must always validate.")

    out = write_json(pathlib.Path(args.output) if args.output else tmp_path("report_spec.json"), spec)

    words = sum(len(p.split()) for s in spec["sections"] for p in (s.get("body") or []))
    result = {
        "source": source,
        "model": model if source == "claude" else None,
        "sections": len(spec["sections"]),
        "section_types": [s["type"] for s in spec["sections"]],
        "charts": [s["chart"] for s in spec["sections"] if s["type"] == "chart"],
        "body_words": words,
        "title": spec["title"],
        "subject": spec["subject"],
        "output_file": str(out),
    }
    if usage:
        result["usage"] = usage
    if degraded:
        result["degraded"] = degraded
    emit(result)


if __name__ == "__main__":
    main()
