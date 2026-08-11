"""Shared pytest fixtures for the test suite.

Two autouse fixtures, both isolating a piece of the developer's real
machine state so no test's outcome depends on who ran it or what they
already had configured: `Settings` from the real `.env` (bug 7 in
ESTADO.md), and `ui/state.py`'s `~/.config/notemcp/state.json` from the
real one (Fase 3, R7 of the Gemini-only migration plan — the same class of
leak, found because it had already happened: the real state.json ended up
holding fixture ids like `"root"`/`"faculdade"`/`"new-1"` from
`test_browser.py`/`test_tree_cache.py`).
"""

import pytest

from notemcp.config import Settings
from notemcp.ui import state


@pytest.fixture(autouse=True)
def isolate_settings_from_dotenv(monkeypatch):
    """Prevent tests from reading the developer's real `.env`.

    `Settings` loads `.env` from the current working directory, so a test
    that builds `Settings(notion_token="t")` would silently inherit the
    real keys — and pass or fail depending on who ran it. No test touches
    the network, but the result used to vary by machine.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for var in (
        "NOTION_TOKEN",
        "NOTION_PARENT_PAGE_ID",
        "NOTION_DATA_SOURCE_ID",
        "NOTION_WRITER",
        "NOTION_DESTINATION",
        "LLM_PROVIDER",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def isolate_state_json(tmp_path, monkeypatch):
    """Prevent tests from reading or overwriting the user's real
    `~/.config/notemcp/state.json`.

    Every `Bridge()` construction touches `ui/state.py` in `__init__`
    (loading the persisted provider/destination/parent-page) and on most
    actions (saving them back). Without this, a test that builds a `Bridge`
    without explicitly monkeypatching `state.STATE_FILE` itself — most of
    them, in `tests/test_ui.py` — silently reads and overwrites the real
    file, which is exactly how fixture ids like `"root"`/`"faculdade"`/
    `"new-1"` ended up persisted as this machine's actual chosen parent
    page. Individual tests that need a specific temp path of their own
    (several already do, to assert roundtrips) simply re-patch these same
    attributes afterwards — `monkeypatch` composes fine either way.
    """
    fake_dir = tmp_path / "notemcp-state"
    monkeypatch.setattr(state, "STATE_DIR", fake_dir)
    monkeypatch.setattr(state, "STATE_FILE", fake_dir / "state.json")
