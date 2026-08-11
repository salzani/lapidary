"""Destination configuration: `NOTION_DESTINATION` and the `require_notion`
guard. Also `_resolve_env_file`'s ordering — the `.env` lookup a packaged
build depends on (see the "Building a desktop executable" section of the
README)."""

import sys

import pytest

from notemcp import config
from notemcp.config import Settings, _resolve_env_file, load_settings
from notemcp.models import DestinationKind


def test_require_notion_page_needs_parent_page_id_specifically():
    """`NOTION_DATA_SOURCE_ID` alone is not enough for the "loose page"
    destination — that shortcut only applies to "database" (risk #2)."""
    settings = Settings(notion_token="t", notion_data_source_id="ds-1")
    with pytest.raises(RuntimeError, match="NOTION_PARENT_PAGE_ID"):
        settings.require_notion(DestinationKind.PAGE)


def test_require_notion_page_passes_with_parent_page_id():
    """Must not raise."""
    settings = Settings(notion_token="t", notion_parent_page_id="parent-1")
    settings.require_notion(DestinationKind.PAGE)


def test_require_notion_without_kind_keeps_the_general_check():
    """Without an explicit kind, `data_source_id` alone is enough (it is
    sufficient for "database"); an empty settings object must still fail on
    the missing parent page id."""
    settings = Settings(notion_token="t", notion_data_source_id="ds-1")
    settings.require_notion()

    settings_empty = Settings(notion_token="t")
    with pytest.raises(RuntimeError, match="NOTION_PARENT_PAGE_ID"):
        settings_empty.require_notion()


def test_require_notion_always_needs_the_token():
    settings = Settings(notion_parent_page_id="parent-1")
    with pytest.raises(RuntimeError, match="NOTION_TOKEN"):
        settings.require_notion(DestinationKind.PAGE)


def test_destination_kind_reads_database_by_default():
    assert Settings().destination_kind() == DestinationKind.DATABASE


def test_destination_kind_reads_page():
    assert Settings(notion_destination="page").destination_kind() == DestinationKind.PAGE


def test_destination_kind_falls_back_to_database_on_unknown_value():
    assert Settings(notion_destination="xpto").destination_kind() == DestinationKind.DATABASE


def test_default_llm_provider_agrees_with_the_registrys_default_model():
    """`config.py`'s default `llm_provider` and
    `registry.DEFAULT_GEMINI_MODEL` are two independent literals on
    purpose (see both docstrings for why `config` does not import `llm`) —
    this test is the coupling that keeps them from drifting apart instead
    of an import."""
    from notemcp.llm import registry

    assert Settings().llm_provider == f"gemini:{registry.DEFAULT_GEMINI_MODEL}"


# --- _resolve_env_file: the four-step lookup a packaged build depends on ---
#
# Every test below isolates itself from the real filesystem (tmp_path,
# monkeypatch'd home directory, no chdir into the real repo) — the same
# discipline `tests/conftest.py::isolate_settings_from_dotenv` documents for
# `Settings` itself (bug 7 in ESTADO.md): a test whose outcome depends on
# what happens to exist on the machine running it is not a test.


@pytest.fixture(autouse=True)
def _isolated_env_resolution(tmp_path, monkeypatch):
    """Point every candidate `_resolve_env_file` checks at a scratch
    directory, and make sure `NOTEMCP_ENV` starts unset. Individual tests
    then create only the file(s) they want to exist."""
    monkeypatch.delenv("NOTEMCP_ENV", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("notemcp.config.Path.home", staticmethod(lambda: fake_home))


def test_resolve_env_file_returns_none_when_nothing_exists():
    assert _resolve_env_file() is None


def test_resolve_env_file_finds_the_cwd_dotenv(tmp_path):
    """The development path: `.env` in the current working directory."""
    (tmp_path / ".env").write_text("NOTION_TOKEN=from-cwd\n")
    assert _resolve_env_file() == str(tmp_path / ".env")


def test_resolve_env_file_finds_the_config_dir_dotenv(tmp_path, monkeypatch):
    """`~/.config/notemcp/.env` — the same directory `ui/state.py` already
    uses for persisted preferences."""
    fake_home = tmp_path / "home"
    config_dir = fake_home / ".config" / "notemcp"
    config_dir.mkdir(parents=True)
    (config_dir / ".env").write_text("NOTION_TOKEN=from-config-dir\n")
    assert _resolve_env_file() == str(config_dir / ".env")


def test_resolve_env_file_prefers_config_dir_over_cwd(tmp_path):
    """Order matters: `~/.config/notemcp/.env` (step 3) beats a `.env` in
    the CWD (step 4) when both exist."""
    (tmp_path / ".env").write_text("NOTION_TOKEN=from-cwd\n")
    config_dir = tmp_path / "home" / ".config" / "notemcp"
    config_dir.mkdir(parents=True)
    (config_dir / ".env").write_text("NOTION_TOKEN=from-config-dir\n")
    assert _resolve_env_file() == str(config_dir / ".env")


def test_resolve_env_file_prefers_notemcp_env_over_everything(tmp_path, monkeypatch):
    """`NOTEMCP_ENV` (step 1) wins even with a CWD `.env` and a config-dir
    `.env` both present."""
    (tmp_path / ".env").write_text("NOTION_TOKEN=from-cwd\n")
    config_dir = tmp_path / "home" / ".config" / "notemcp"
    config_dir.mkdir(parents=True)
    (config_dir / ".env").write_text("NOTION_TOKEN=from-config-dir\n")

    override = tmp_path / "custom.env"
    override.write_text("NOTION_TOKEN=from-override\n")
    monkeypatch.setenv("NOTEMCP_ENV", str(override))

    assert _resolve_env_file() == str(override)


def test_resolve_env_file_ignores_notemcp_env_pointing_nowhere(tmp_path, monkeypatch):
    """A `NOTEMCP_ENV` set to a path that doesn't exist falls through to the
    next candidate instead of resolving to a file that isn't there."""
    (tmp_path / ".env").write_text("NOTION_TOKEN=from-cwd\n")
    monkeypatch.setenv("NOTEMCP_ENV", str(tmp_path / "does-not-exist.env"))

    assert _resolve_env_file() == str(tmp_path / ".env")


def test_resolve_env_file_finds_the_frozen_executable_dotenv(tmp_path, monkeypatch):
    """The packaged-app case: `.env` next to a frozen PyInstaller
    executable, detected via `sys.frozen`/`sys.executable`."""
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    fake_exe = bundle_dir / "notemcp"
    fake_exe.write_text("")
    (bundle_dir / ".env").write_text("NOTION_TOKEN=from-bundle\n")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    assert _resolve_env_file() == str(bundle_dir / ".env")


def test_resolve_env_file_prefers_frozen_bundle_over_config_dir_and_cwd(tmp_path, monkeypatch):
    """Order matters: the frozen-bundle `.env` (step 2) beats both
    `~/.config/notemcp/.env` (step 3) and the CWD `.env` (step 4)."""
    (tmp_path / ".env").write_text("NOTION_TOKEN=from-cwd\n")
    config_dir = tmp_path / "home" / ".config" / "notemcp"
    config_dir.mkdir(parents=True)
    (config_dir / ".env").write_text("NOTION_TOKEN=from-config-dir\n")

    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    fake_exe = bundle_dir / "notemcp"
    fake_exe.write_text("")
    (bundle_dir / ".env").write_text("NOTION_TOKEN=from-bundle\n")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    assert _resolve_env_file() == str(bundle_dir / ".env")


def test_load_settings_actually_reads_values_from_the_resolved_file(tmp_path, monkeypatch):
    """Not just path resolution: `load_settings()` has to read the file it
    resolves to and populate `Settings` from it — otherwise this whole
    mechanism could resolve the "right" path and still boot without a
    token, which is exactly the failure this feature exists to prevent."""
    override = tmp_path / "custom.env"
    override.write_text("NOTION_TOKEN=secret-from-file\n")
    monkeypatch.setenv("NOTEMCP_ENV", str(override))
    monkeypatch.delenv("NOTION_TOKEN", raising=False)

    settings = load_settings()

    assert settings.notion_token == "secret-from-file"


def test_bundle_inside_a_checkout_finds_the_repository_env(tmp_path, monkeypatch):
    """A pinned build must read the .env the developer already edited.

    `packaging/build.py` writes to `<checkout>/dist/<name>/`, so the frozen
    executable can derive its own checkout. Without this the app silently
    ignores the .env sitting at the project root and demands a second copy —
    two files holding the same token, drifting apart unnoticed.
    """
    checkout = tmp_path / "notion-mcp"
    exe = checkout / "dist" / "notemcp" / "notemcp"
    exe.parent.mkdir(parents=True)
    (checkout / ".env").write_text("NOTION_TOKEN=from-checkout\n", encoding="utf-8")

    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(exe))
    monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    assert config._checkout_root_of_bundle() == checkout
    assert config._resolve_env_file() == str(checkout / ".env")


def test_bundle_outside_a_checkout_does_not_borrow_a_stranger_env(tmp_path, monkeypatch):
    """A bundle copied elsewhere must not adopt whatever .env is two levels up.

    The checkout is only inferred when the layout actually matches
    `<root>/dist/<name>/`; anything else returns None instead of guessing.
    """
    exe = tmp_path / "opt" / "notemcp" / "notemcp"
    exe.parent.mkdir(parents=True)
    (tmp_path / ".env").write_text("NOTION_TOKEN=not-mine\n", encoding="utf-8")

    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(exe))

    assert config._checkout_root_of_bundle() is None
