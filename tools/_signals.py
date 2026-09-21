"""Shared helpers for the non-YouTube signal sources (Hacker News, GitHub).

Both fetchers work from the ranked technology list that analyze_trends.py
produces on its YouTube-only first pass, so they only spend requests on
technologies that actually showed up in the videos.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import fail, read_json

TAXONOMY = pathlib.Path(__file__).resolve().parent.parent / "config" / "tech_taxonomy.json"


def load_taxonomy(path: pathlib.Path | str = TAXONOMY) -> list[dict]:
    data = read_json(pathlib.Path(path))
    return data["technologies"]


def search_term(tech: dict) -> str:
    """The string to search external sources for.

    Very short canonical names ('Go', 'C#') are ambiguous even inside technical
    corpora, so prefer an unambiguous alias like 'golang' when one exists.
    """
    if tech.get("search_query"):
        return tech["search_query"]
    name = tech["name"]
    if len(name) <= 2:
        aliases = [a for a in tech.get("aliases", []) if len(a) > len(name)]
        if aliases:
            return max(aliases, key=len)
    return name


def select_technologies(analysis_file: pathlib.Path, top: int, taxonomy_file: pathlib.Path) -> list[dict]:
    """Top-N technologies from a YouTube-only analysis pass, joined to taxonomy."""
    if not analysis_file.exists():
        fail(
            f"Analysis file not found: {analysis_file}",
            hint="Run the YouTube-only pass first: python tools/analyze_trends.py",
        )
    analysis = read_json(analysis_file)
    by_id = {t["id"]: t for t in load_taxonomy(taxonomy_file)}

    selected = []
    for row in analysis.get("technologies", [])[:top]:
        tech = by_id.get(row["id"])
        if tech:
            selected.append({**tech, "youtube_rank": len(selected) + 1})
    return selected
