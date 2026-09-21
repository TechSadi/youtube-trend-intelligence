"""Check every credential and dependency before a run spends anything.

Written for deployment. The failure mode this exists to prevent is a scheduled
run that harvests two thousand videos, burns its quota, renders a report, and
then dies on the send because an app password was rotated three weeks ago.

Each check is cheap and non-destructive:

    YouTube      channels.list on a known id — 1 quota unit of 10,000
    GitHub       GET /rate_limit — free, not rate limited
    Anthropic    models.retrieve — free
    Gmail SMTP   connect and authenticate, then disconnect without sending
    Config       the JSON files parse and contain what the tools expect
    Imports      every third-party package the pipeline needs

Exit 0 when everything critical passes (warnings are fine), 1 otherwise.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from _common import PROJECT_ROOT, get_env, get_logger, load_scope, read_json

log = get_logger(__name__)

# A channel that has existed for two decades, used as a liveness probe.
PROBE_CHANNEL = "UC_x5XG1OV2P6uZZ5FSM9Ttw"  # Google Developers

REQUIRED_IMPORTS = {
    "requests": "requests",
    "googleapiclient": "google-api-python-client",
    "youtube_transcript_api": "youtube-transcript-api",
    "matplotlib": "matplotlib",
    "dotenv": "python-dotenv",
}
OPTIONAL_IMPORTS = {"anthropic": "anthropic"}


class Report:
    """Collects check results so one bad credential does not hide the next."""

    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, name: str, ok: bool, detail: str, *, critical: bool = True,
            hint: str | None = None) -> None:
        self.checks.append({"check": name, "ok": ok, "critical": critical,
                            "detail": detail, **({"hint": hint} if hint and not ok else {})})
        mark = "PASS" if ok else ("FAIL" if critical else "WARN")
        log.info("%-5s %-14s %s", mark, name, detail)

    @property
    def failed(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"] and c["critical"]]

    @property
    def warnings(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"] and not c["critical"]]


def check_imports(report: Report) -> None:
    import importlib.util
    missing = [pkg for mod, pkg in REQUIRED_IMPORTS.items()
               if not importlib.util.find_spec(mod)]
    report.add("imports", not missing,
               "all required packages importable" if not missing else f"missing: {', '.join(missing)}",
               hint="pip install -r requirements.txt")

    absent = [pkg for mod, pkg in OPTIONAL_IMPORTS.items() if not importlib.util.find_spec(mod)]
    if absent:
        report.add("imports_llm", False, f"missing: {', '.join(absent)}", critical=False,
                   hint="pip install anthropic — without it the narrative uses the template")


def check_config(report: Report) -> None:
    try:
        scope = load_scope()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        report.add("config_scope", False, f"{type(exc).__name__}: {exc}",
                   hint="config/scope.json is not valid JSON")
        return

    seeds = scope.get("seed_channels") or []
    report.add("config_scope", bool(seeds),
               f"{len(seeds)} seed channels, lookback {scope.get('lookback_days')}d"
               + (f", env overrides: {list(scope['_env_overrides'])}" if scope.get("_env_overrides") else ""),
               hint="Add seed_channels to config/scope.json")

    taxonomy_path = PROJECT_ROOT / "config" / "tech_taxonomy.json"
    try:
        taxonomy = read_json(taxonomy_path)
        count = len(taxonomy.get("technologies", taxonomy) if isinstance(taxonomy, dict) else taxonomy)
        report.add("config_taxonomy", count > 0, f"{count} technologies tracked")
    except Exception as exc:  # noqa: BLE001
        report.add("config_taxonomy", False, f"{type(exc).__name__}: {exc}",
                   hint=f"Could not read {taxonomy_path}")


def check_youtube(report: Report) -> None:
    key = get_env("YOUTUBE_API_KEY")
    if not key:
        report.add("youtube", False, "YOUTUBE_API_KEY not set",
                   hint="Cloud Console > enable YouTube Data API v3 > create an API key")
        return
    try:
        from googleapiclient.discovery import build
        client = build("youtube", "v3", developerKey=key, cache_discovery=False)
        response = client.channels().list(part="snippet", id=PROBE_CHANNEL).execute()
        title = response["items"][0]["snippet"]["title"]
        report.add("youtube", True, f"key valid, probe resolved '{title}' (1 quota unit)")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        hint = "Check the key and that YouTube Data API v3 is enabled on that project."
        if "quotaExceeded" in message:
            hint = "Quota is exhausted for today. It resets at midnight Pacific."
        report.add("youtube", False, f"{type(exc).__name__}: {message[:200]}", hint=hint)


def check_github(report: Report) -> None:
    token = get_env("GITHUB_TOKEN")
    if not token:
        report.add("github", False, "GITHUB_TOKEN not set — search limited to 10 req/min",
                   critical=False,
                   hint="Create a fine-grained PAT with public read access. The run degrades without it.")
        return
    try:
        import requests
        response = requests.get("https://api.github.com/rate_limit", timeout=20,
                                headers={"Authorization": f"Bearer {token}",
                                         "Accept": "application/vnd.github+json"})
        if response.status_code == 401:
            report.add("github", False, "token rejected (401)", critical=False,
                       hint="The PAT is expired or revoked. Create a new one.")
            return
        response.raise_for_status()
        search = response.json()["resources"]["search"]
        report.add("github", True, f"authenticated, search {search['remaining']}/{search['limit']} remaining")
    except Exception as exc:  # noqa: BLE001
        report.add("github", False, f"{type(exc).__name__}: {exc}", critical=False)


def check_anthropic(report: Report) -> None:
    key = get_env("ANTHROPIC_API_KEY")
    model = get_env("ANTHROPIC_MODEL") or "claude-opus-5"
    if not key:
        report.add("anthropic", False, "ANTHROPIC_API_KEY not set — narrative uses the template",
                   critical=False,
                   hint="console.anthropic.com > API keys. Roughly $0.20 per weekly run.")
        return
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        info = client.models.retrieve(model)
        report.add("anthropic", True, f"key valid, model '{info.id}' reachable")
    except Exception as exc:  # noqa: BLE001
        report.add("anthropic", False, f"{type(exc).__name__}: {str(exc)[:200]}", critical=False,
                   hint=f"Check the key and that '{model}' is a valid model id. Falls back to the template.")


def check_email(report: Report) -> None:
    recipient = get_env("REPORT_RECIPIENT")
    report.add("recipient", bool(recipient), recipient or "REPORT_RECIPIENT not set",
               hint="Set REPORT_RECIPIENT in .env, or always pass --to.")

    password = get_env("GMAIL_APP_PASSWORD")
    address = get_env("GMAIL_ADDRESS") or recipient

    if not password:
        oauth_ready = (PROJECT_ROOT / "credentials.json").exists() and (PROJECT_ROOT / "token.json").exists()
        report.add("gmail", False,
                   "GMAIL_APP_PASSWORD not set" + (" (OAuth files present — interactive only)" if oauth_ready else ""),
                   hint="Scheduled runs need SMTP: myaccount.google.com/apppasswords (2-Step Verification "
                        "required), then set GMAIL_APP_PASSWORD. OAuth cannot refresh unattended.")
        return

    if not address:
        report.add("gmail", False, "GMAIL_APP_PASSWORD set but no sending address",
                   hint="Set GMAIL_ADDRESS (or REPORT_RECIPIENT) to the account that owns the app password.")
        return

    try:
        import smtplib
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(address, password)
        report.add("gmail", True, f"SMTP authenticated as {address} (nothing sent)")
    except Exception as exc:  # noqa: BLE001
        report.add("gmail", False, f"{type(exc).__name__}: {str(exc)[:200]}",
                   hint="App passwords are 16 characters and must belong to GMAIL_ADDRESS. "
                        "Spaces in the value are fine; quotes are not.")


CHECKS = {
    "imports": check_imports,
    "config": check_config,
    "youtube": check_youtube,
    "github": check_github,
    "anthropic": check_anthropic,
    "email": check_email,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", default=[], choices=list(CHECKS),
                        help="Run just these checks. Repeatable.")
    parser.add_argument("--offline", action="store_true",
                        help="Skip every check that makes a network call")
    args = parser.parse_args()

    selected = args.only or list(CHECKS)
    if args.offline:
        selected = [name for name in selected if name in ("imports", "config")]

    report = Report()
    for name in selected:
        CHECKS[name](report)

    # emit()/fail() would exit before the summary, so the envelope is built here.
    import json
    ok = not report.failed
    payload = {
        "ok": ok,
        "checks": report.checks,
        "failed": [c["check"] for c in report.failed],
        "warnings": [c["check"] for c in report.warnings],
    }
    if not ok:
        payload["error"] = f"{len(report.failed)} critical check(s) failed: " + \
                           ", ".join(c["check"] for c in report.failed)
        payload["hint"] = "Fix the failing checks above, then re-run preflight before scheduling."
    elif report.warnings:
        payload["note"] = ("Run will proceed but degrade: "
                           + ", ".join(c["check"] for c in report.warnings))

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
