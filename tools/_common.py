"""Shared helpers for WAT tools.

Every tool in tools/ imports from here so that env loading, paths, logging and
output format stay identical across the toolbox. Import it like this, so tools
work whether they are run as `python tools/foo.py` or imported:

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    from _common import emit, fail, get_env, tmp_path
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = PROJECT_ROOT / ".tmp"
ENV_FILE = PROJECT_ROOT / ".env"


# A Windows console defaults to cp1252, which cannot encode the em-dashes and
# arrows this pipeline's prose is full of. Interactively that only garbles a
# character; piped into an orchestrator or a CI log it raises UnicodeEncodeError
# and takes the whole step down. Force UTF-8 on both streams at import, before
# any tool writes a byte.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # already wrapped, or not a TextIO
        pass


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

def load_env(path: Path = ENV_FILE) -> None:
    """Load .env into os.environ. Existing env vars win, so callers can override.

    Uses python-dotenv when installed and falls back to a minimal parser so a
    tool never breaks just because a dependency is missing.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get_env(key: str, default: str | None = None, *, required: bool = False) -> str | None:
    """Read a config value from .env / the environment."""
    load_env()
    value = os.environ.get(key, default)
    if required and not value:
        fail(
            f"Missing required environment variable: {key}",
            hint=f"Add {key}= to {ENV_FILE.name} (see .env.example).",
        )
    return value


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def env_flag(key: str, default: bool = False) -> bool:
    """Read a boolean from the environment. Accepts 1/true/yes/on, any case."""
    raw = (get_env(key) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Scope configuration
# --------------------------------------------------------------------------
# config/scope.json is the source of truth for *what* is tracked. On a scheduled
# run there is no one to edit it, and a deploy target (GitHub Actions) can only
# inject environment variables — so every knob worth changing per-run has an env
# override. File value is the default; env wins; a CLI flag still wins over both.

SCOPE_ENV_OVERRIDES = {
    "lookback_days": ("SCOPE_LOOKBACK_DAYS", int),
    "max_videos_per_channel": ("SCOPE_MAX_VIDEOS_PER_CHANNEL", int),
    "min_video_duration_seconds": ("SCOPE_MIN_VIDEO_DURATION", int),
    "max_discovery_searches": ("SCOPE_MAX_DISCOVERY_SEARCHES", int),
    "max_channels_per_query": ("SCOPE_MAX_CHANNELS_PER_QUERY", int),
    "min_channel_subscribers": ("SCOPE_MIN_CHANNEL_SUBSCRIBERS", int),
    "transcript_top_n": ("SCOPE_TRANSCRIPT_TOP_N", int),
    "recipient": ("REPORT_RECIPIENT", str),
}

DEFAULT_SCOPE_FILE = PROJECT_ROOT / "config" / "scope.json"


def load_scope(path: Path | str | None = None) -> dict[str, Any]:
    """Load config/scope.json and apply environment overrides.

    Returns the merged scope. The keys that were overridden are recorded under
    `_env_overrides` so a run log can show what the schedule changed.
    """
    scope_path = Path(path) if path else DEFAULT_SCOPE_FILE
    if not scope_path.exists():
        fail(f"Scope file not found: {scope_path}",
             hint="Create config/scope.json (see the repo default).")

    scope = read_json(scope_path)
    applied: dict[str, Any] = {}

    for key, (env_key, cast) in SCOPE_ENV_OVERRIDES.items():
        raw = get_env(env_key)
        if raw is None or raw == "":
            continue
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            fail(f"{env_key}={raw!r} is not a valid {cast.__name__}.",
                 hint=f"Set {env_key} to a {cast.__name__} value, or unset it to use config/scope.json.")
        scope[key] = value
        applied[key] = value

    scope["_env_overrides"] = applied
    return scope


def tmp_path(*parts: str) -> Path:
    """Path inside .tmp/, with parent directories created. Contents are disposable."""
    path = TMP_DIR.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Output contract
# --------------------------------------------------------------------------
# Tools speak JSON on stdout so the agent can parse a result instead of
# guessing at prose. Logs and progress go to stderr.

def emit(data: dict[str, Any], *, exit_code: int = 0) -> None:
    """Print a success envelope to stdout and exit."""
    print(json.dumps({"ok": True, **data}, indent=2, ensure_ascii=False, default=str))
    sys.exit(exit_code)


def fail(message: str, *, hint: str | None = None, exit_code: int = 1) -> None:
    """Print an error envelope to stdout and exit non-zero.

    The hint should tell the agent what to do next, not just what went wrong.
    """
    payload: dict[str, Any] = {"ok": False, "error": message}
    if hint:
        payload["hint"] = hint
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    sys.exit(exit_code)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def get_logger(name: str) -> logging.Logger:
    """Logger writing to stderr, so stdout stays pure JSON."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    return logger
