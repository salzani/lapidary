"""UI preferences that survive between runs: provider, destination, parent page.

Stored in `~/.config/notemcp/` rather than in `.env`, deliberately. `.env` is
configuration the user edits by hand; this file is preference the application
writes on its own. Merging the two would mean the app rewriting a file that
belongs to the user, reformatting and dropping their comments in the process.
"""

from __future__ import annotations

import json
from pathlib import Path

STATE_DIR = Path.home() / ".config" / "notemcp"
STATE_FILE = STATE_DIR / "state.json"


def load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save(**values) -> None:
    """Merge `values` into the stored state and write it back.

    Write failures are swallowed on purpose: this file holds preferences, not
    data. A read-only home directory or a full disk should cost the user their
    remembered dropdown selection, never the note they are in the middle of
    publishing.
    """
    state = load()
    state.update(values)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass


def _is_valid_parent_page(raw: object) -> bool:
    """Shape exato de `parent_page`: `{id, title, path:[{id,title}, ...]}`."""
    if not isinstance(raw, dict):
        return False
    if not isinstance(raw.get("id"), str) or not raw["id"]:
        return False
    if not isinstance(raw.get("title"), str):
        return False
    path = raw.get("path")
    if not isinstance(path, list):
        return False
    for item in path:
        if not isinstance(item, dict):
            return False
        if not isinstance(item.get("id"), str) or not isinstance(item.get("title"), str):
            return False
    return True


def load_parent_page() -> tuple[dict | None, bool]:
    """Load the parent page chosen in a previous session.

    Returns `(parent_page, malformed)` — two deliberately distinct ways of
    saying "there is no chosen page":

    - key absent, the ordinary case (never chosen, or explicitly cleared) →
      `(None, False)`: fall back to the root silently;
    - key present but structurally invalid (hand-edited, written by a future
      version, corrupted) → `(None, True)`: still falls back to the root, but
      the caller is expected to surface a warning.

    The distinction exists because falling back to the root without a word,
    when a choice *was* recorded, is precisely the failure this must not have:
    the note would land somewhere the user never picked and nothing on screen
    would say so.
    """
    raw = load().get("parent_page")
    if raw is None:
        return None, False
    if not _is_valid_parent_page(raw):
        return None, True
    return raw, False


def save_parent_page(parent_page: dict | None) -> None:
    """Store the chosen parent page, or clear it with `None`."""
    save(parent_page=parent_page)
