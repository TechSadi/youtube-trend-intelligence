"""The report spec contract — one definition, three consumers.

A "report spec" is the narrative layer of the trend report: headline, prose,
which charts appear, what the recommendations say. `render_report.py` turns it
into HTML but never invents a word of it.

Three things need to agree on its shape:
  - author_report_spec.py  asks Claude to produce one (this schema is sent as
                           the structured-output format, so the model cannot
                           return a shape the renderer chokes on)
  - _spec_template.py      builds one deterministically when there is no API key
  - render_report.py       consumes it

Keeping the schema here means a new section type is added in one place. The
JSON Schema is deliberately flat: every section is one object with `type` plus
the union of all section fields, because a strict `anyOf` discriminated union
is poorly supported and buys nothing here — `validate_spec()` enforces the
per-type requirements afterwards, with better error messages than a schema
validator would give.

Two constraints the API's structured-output mode does not accept, and which
therefore live in `validate_spec()` rather than the schema: array length bounds
(`minItems`/`maxItems`) and numeric bounds. Every object node must carry
`additionalProperties: false` or the request is rejected.
"""

from __future__ import annotations

from typing import Any

# Chart ids that render_report.CHARTS knows how to draw. Kept as a literal
# rather than imported, so this module stays free of matplotlib.
CHART_IDS = ["momentum", "signal_vs_hype", "timeline", "reach"]

SECTION_TYPES = ["text", "chart", "stats", "cards", "videos", "table"]

# Editorial bounds. Fewer than four sections is a note, not a report; more than
# nine is the length the reader already pushed back on.
MIN_SECTIONS, MAX_SECTIONS = 4, 9

# Fields each section type requires beyond "type".
REQUIRED_BY_TYPE = {
    "text": [],            # heading/body/bullets all optional, but see validate_spec
    "chart": ["chart"],
    "stats": ["items"],
    "cards": ["items"],
    "videos": ["items"],
    "table": ["columns", "rows"],
}

JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["subject", "preheader", "kicker", "title", "date_label", "footer", "sections"],
    "properties": {
        "subject": {
            "type": "string",
            "description": "Email subject line. Lead with the finding, not the word 'report'. Max ~70 chars.",
        },
        "preheader": {
            "type": "string",
            "description": "Inbox preview text shown after the subject. One or two sentences carrying a concrete number.",
        },
        "kicker": {"type": "string", "description": "Small uppercase eyebrow above the title."},
        "title": {
            "type": "string",
            "description": "The headline claim of this edition, e.g. 'Reliability is the new frontier'. Not a generic label.",
        },
        "date_label": {"type": "string", "description": "Window and corpus size, e.g. '23 Jul - 21 Sep 2026 · 1,847 videos, 62 channels'."},
        "footer": {
            "type": "string",
            "description": "Method note explaining how momentum and the adoption index are computed, and what the report does not claim. Supports <br> and **bold**.",
        },
        "sections": {
            "type": "array",
            # No minItems/maxItems: the structured-outputs API rejects array
            # constraints. Section-count bounds are enforced in validate_spec().
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "enum": SECTION_TYPES},
                    "heading": {"type": "string"},
                    "body": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Paragraphs. Supports **bold** and `code`.",
                    },
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "caption": {"type": "string", "description": "Small italic note under a chart or table."},
                    "alt": {"type": "string", "description": "Alt text, chart sections only."},
                    "chart": {
                        "type": "string",
                        "enum": CHART_IDS,
                        "description": "Chart sections only. Which pre-built chart to draw.",
                    },
                    "columns": {"type": "array", "items": {"type": "string"}, "description": "Table sections only."},
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                        "description": "Table sections only. Every cell a string, numbers pre-formatted.",
                    },
                    "items": {
                        "type": "array",
                        "description": "stats: value+label. cards: title/badge/accent/subtitle/body. videos: video_id/title/url/channel/stat/note.",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "value": {"type": "string", "description": "stats: the big number, pre-formatted (e.g. '+187%')."},
                                "label": {"type": "string", "description": "stats: short uppercase caption under the value."},
                                "title": {"type": "string", "description": "cards/videos: the headline of the item."},
                                "badge": {"type": "string", "description": "cards: short uppercase tag, e.g. 'HIGHEST CONVICTION'."},
                                "accent": {"type": "string", "description": "cards: hex colour. Use #2a78d6, #1baf7a, #eda100 or #e34948."},
                                "subtitle": {"type": "string", "description": "cards/videos: the supporting numbers."},
                                "body": {"type": "string", "description": "cards: two or three sentences of substance."},
                                "note": {"type": "string", "description": "videos: why this video matters."},
                                "video_id": {"type": "string", "description": "videos: YouTube id, used to fetch the thumbnail."},
                                "url": {"type": "string", "description": "cards/videos: link target."},
                                "channel": {"type": "string", "description": "videos: channel title."},
                                "stat": {"type": "string", "description": "videos: e.g. '412K views · 8.2K/day'."},
                            },
                        },
                    },
                },
            },
        },
    },
}


def validate_spec(spec: Any) -> list[str]:
    """Return a list of problems. Empty list means the spec is renderable.

    This is the gate between an LLM-authored spec and the renderer. It checks
    the things that would otherwise surface as a KeyError deep inside a section
    builder, plus the editorial minimums (a report with no prose is not a
    report).
    """
    problems: list[str] = []

    if not isinstance(spec, dict):
        return [f"spec must be an object, got {type(spec).__name__}"]

    for key in ("subject", "title", "sections"):
        if not spec.get(key):
            problems.append(f"missing required top-level key: {key}")

    sections = spec.get("sections")
    if not isinstance(sections, list) or not sections:
        problems.append("sections must be a non-empty array")
        return problems

    if not MIN_SECTIONS <= len(sections) <= MAX_SECTIONS:
        problems.append(f"{len(sections)} sections; expected between {MIN_SECTIONS} and {MAX_SECTIONS}")

    prose_chars = 0
    for i, section in enumerate(sections):
        where = f"sections[{i}]"
        if not isinstance(section, dict):
            problems.append(f"{where} must be an object")
            continue

        stype = section.get("type")
        if stype not in SECTION_TYPES:
            problems.append(f"{where}.type={stype!r} is not one of {SECTION_TYPES}")
            continue

        for key in REQUIRED_BY_TYPE[stype]:
            if not section.get(key):
                problems.append(f"{where} is type {stype!r} and needs a non-empty {key!r}")

        if stype == "chart" and section.get("chart") not in CHART_IDS:
            problems.append(f"{where}.chart={section.get('chart')!r} is not one of {CHART_IDS}")

        if stype == "table":
            width = len(section.get("columns") or [])
            for r, row in enumerate(section.get("rows") or []):
                if len(row) != width:
                    problems.append(f"{where}.rows[{r}] has {len(row)} cells, columns has {width}")

        if stype == "stats":
            for j, item in enumerate(section.get("items") or []):
                if not item.get("value") or not item.get("label"):
                    problems.append(f"{where}.items[{j}] needs both value and label")

        if stype == "cards":
            for j, item in enumerate(section.get("items") or []):
                if not item.get("title"):
                    problems.append(f"{where}.items[{j}] needs a title")

        if stype == "videos":
            for j, item in enumerate(section.get("items") or []):
                if not item.get("title"):
                    problems.append(f"{where}.items[{j}] needs a title")

        for paragraph in section.get("body") or []:
            prose_chars += len(paragraph)

    if not any(s.get("type") == "text" for s in sections if isinstance(s, dict)):
        problems.append("spec has no 'text' section — a report of charts alone says nothing")

    if prose_chars < 400:
        problems.append(f"only {prose_chars} characters of prose across all sections; "
                        "the narrative is the point of the report")

    return problems
