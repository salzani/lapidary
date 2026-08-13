"""Deciding what to do about `~/.config/notemcp/.env` for a packaged install.

Same file `config.py::_resolve_env_file` checks third, after `NOTEMCP_ENV`
and the frozen-executable directory — it is the recommended location for a
desktop install because it does not depend on the unpredictable working
directory a launcher starts the process from.

The decision (`decide`) is a pure function of three booleans, kept
separate from the side-effecting `apply` so the three cases can be unit
tested without touching any real file — `.env` holds a Notion token, and a
test must never read or write the developer's actual one (the same
reasoning behind `tests/conftest.py::isolate_settings_from_dotenv`).
"""

from __future__ import annotations

import shutil
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

REQUIRED_LINES = ("NOTION_TOKEN=", "NOTION_PARENT_PAGE_ID=")

_TOKEN_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR
"""0600 — the file holds a Notion token, so it is owner-read/write only."""


class EnvAction(Enum):
    ALREADY_OK = "already_ok"
    COPIED_FROM_REPO = "copied_from_repo"
    MISSING = "missing"


@dataclass(frozen=True)
class EnvDecision:
    action: EnvAction
    target: Path
    source: Path | None = None


def target_env_path(home: Path | None = None) -> Path:
    """The recommended `.env` location for a packaged install.

    Accepts an explicit `home` so tests can point this at a temp directory
    instead of monkeypatching `Path.home()` globally.
    """
    base = home if home is not None else Path.home()
    return base / ".config" / "notemcp" / ".env"


def decide(
    *,
    target_env: Path,
    repo_env: Path,
    target_exists: bool,
    repo_exists: bool,
) -> EnvDecision:
    """Decide what `install.py` should do about `.env`.

    Existence is passed in explicitly, rather than checked here, so this
    function has no filesystem access at all and can be tested with paths
    that do not exist anywhere.
    """
    if target_exists:
        return EnvDecision(EnvAction.ALREADY_OK, target_env)
    if repo_exists:
        return EnvDecision(EnvAction.COPIED_FROM_REPO, target_env, repo_env)
    return EnvDecision(EnvAction.MISSING, target_env)


def apply(decision: EnvDecision) -> None:
    """Perform the filesystem side effect `decide` chose, if any.

    Only `COPIED_FROM_REPO` does anything — `ALREADY_OK` and `MISSING` are
    report-only outcomes. Never touches any `.env` other than
    `decision.target`, and never touches `decision.source` beyond reading
    it to copy.
    """
    if decision.action is not EnvAction.COPIED_FROM_REPO:
        return
    assert decision.source is not None
    decision.target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(decision.source, decision.target)
    decision.target.chmod(_TOKEN_FILE_MODE)


def guidance_message(target: Path) -> str:
    """What to print when there is no `.env` anywhere to use or copy.

    Deliberately does not create a template file — `.env.example` was
    removed from this repository on purpose (see README), so this prints
    the two required lines instead of writing them to disk.
    """
    lines = "\n".join(f"  {line}" for line in REQUIRED_LINES)
    return (
        f"No .env found at {target}, and none in the repository to copy.\n"
        f"Create one there with at least:\n\n{lines}\n"
    )
