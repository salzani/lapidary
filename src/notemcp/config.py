"""Configuration loaded from .env / environment variables."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import PARENT_PAGE_ID_REQUIRED_MESSAGE, DestinationKind


def _checkout_root_of_bundle() -> Path | None:
    """Return the repository root when the bundle still sits inside a checkout.

    `packaging/build.py` writes to `<checkout>/dist/<name>/`, so from a frozen
    executable the checkout is two directories above it. Recovering that path
    lets someone who cloned the repository, built, and pinned the app keep a
    single `.env` at the project root — the file they already edited — instead
    of maintaining a second copy elsewhere. Two files holding the same
    credentials drift, and the drift is silent: the app keeps running on the
    stale one.

    Returns `None` unless the layout actually matches, so a bundle that was
    copied somewhere else never picks up an unrelated `.env` that happens to
    sit two levels up.
    """
    exe_dir = Path(sys.executable).parent
    if exe_dir.parent.name != "dist":
        return None
    return exe_dir.parent.parent


def _env_search_paths() -> list[Path]:
    """The candidate `.env` locations, in the order they are tried.

    Kept separate from `_resolve_env_file` so an error message can show the
    user exactly where the file was looked for. "NOTION_TOKEN not set" is
    useless advice to someone whose `.env` is right there but in a directory
    the process never searched — which is the normal situation for a packaged
    build, since a desktop launcher does not start the app from the source
    tree.
    """
    paths: list[Path] = []

    override = os.environ.get("NOTEMCP_ENV")
    if override:
        paths.append(Path(override))

    if getattr(sys, "frozen", False):
        paths.append(Path(sys.executable).parent / ".env")
        checkout = _checkout_root_of_bundle()
        if checkout is not None:
            paths.append(checkout / ".env")

    paths.append(Path.home() / ".config" / "notemcp" / ".env")
    paths.append(Path.cwd() / ".env")

    unique: list[Path] = []
    for path in paths:
        resolved = path.expanduser()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _missing_token_message() -> str:
    """Build the "no token" error, naming every path that was searched."""
    searched = "\n".join(f"  {i}. {p}" for i, p in enumerate(_env_search_paths(), 1))
    return (
        "NOTION_TOKEN não configurado.\n\n"
        f"Procurei um arquivo .env nestes lugares, nesta ordem:\n{searched}\n\n"
        "Crie um .env em um deles com pelo menos:\n"
        "  NOTION_TOKEN=secret_...\n"
        "  NOTION_PARENT_PAGE_ID=...\n\n"
        "Para uma instalação empacotada, o lugar recomendado é "
        f"{Path.home() / '.config' / 'notemcp' / '.env'} — ele não depende "
        "do diretório de onde o aplicativo foi aberto.\n"
        "Alternativamente, aponte a variável NOTEMCP_ENV para o arquivo."
    )


def _resolve_env_file() -> str | None:
    """Resolve which `.env` file `Settings` should load.

    The first candidate that exists on disk wins, in this order:

    1. The path in the `NOTEMCP_ENV` environment variable, if set and it
       exists — an explicit override for anyone who wants a non-default
       location.
    2. `.env` next to the executable, when running from a frozen
       PyInstaller `onedir` bundle (detected via `sys.frozen` /
       `sys.executable`). A desktop shortcut starts the process with an
       unpredictable working directory (`/`, `C:\\Windows\\System32`, the
       home directory) — resolving relative to the CWD would silently boot
       without a token and fail in a confusing way later.
    3. `~/.config/notemcp/.env` — the same directory `ui/state.py` already
       uses for persisted preferences, so a packaged install has one
       conventional place to keep credentials.
    4. `.env` in the current working directory — the development path
       (`python -m notemcp.cli` from a repo checkout), preserved exactly
       as it worked before this function existed.

    Returns `None` — pydantic-settings' "no dotenv file" behaviour — when
    none of the four exist. That is not an error by itself: a machine that
    sets real environment variables directly (CI, a container) has nothing
    to load here. `Settings.require_notion` is what actually enforces the
    presence of credentials.
    """
    for candidate in _env_search_paths():
        if candidate.is_file():
            return str(candidate)
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_resolve_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    notion_token: str = ""
    notion_parent_page_id: str = ""
    notion_data_source_id: str = ""
    """ID of the *data source* (not the database). See notion/base.py for the
    data-source vs. database distinction the Notion API introduced on
    2025-09-03 (bug 1 in ESTADO.md)."""
    notion_writer: str = "api"
    notion_destination: str = "database"
    """Default destination: "database" (a row in the "Notas" database) or
    "page" (a child page of notion_parent_page_id). Symmetric to
    `notion_writer`."""

    llm_provider: str = "gemini:gemini-3.6-flash"
    """Kept as a plain string literal rather than importing
    `llm.registry.DEFAULT_GEMINI_MODEL` — `config` staying free of `llm`
    imports keeps `Settings()` cheap to construct (no `dataclasses`/
    `google.genai` machinery pulled in just to read a config default).
    `tests/test_config.py` asserts this literal and
    `registry.DEFAULT_GEMINI_MODEL` agree, which is the coupling that
    matters here — a test, not an import."""
    llm_timeout: float = 180.0

    gemini_api_key: str = ""

    notes_db: str = ""
    """Empty means the default path (~/.local/share/notemcp/notes.db). See
    store/db.py."""

    def require_notion(self, kind: DestinationKind | None = None) -> None:
        """Validate the minimum configuration needed to publish to `kind`.

        `kind=None` validates only the requirement common to both
        destinations (used at UI boot, before the user has chosen one).
        The "loose page" destination specifically requires
        `NOTION_PARENT_PAGE_ID` — `NOTION_DATA_SOURCE_ID` alone is not
        enough, since that shortcut only applies to "database" (risk 2).
        """
        if not self.notion_token:
            raise RuntimeError(_missing_token_message())

        if kind is DestinationKind.PAGE:
            if not self.notion_parent_page_id:
                raise RuntimeError(PARENT_PAGE_ID_REQUIRED_MESSAGE)
            return

        if not self.notion_parent_page_id and not self.notion_data_source_id:
            raise RuntimeError(
                "Defina NOTION_PARENT_PAGE_ID (para criar a database) "
                "ou NOTION_DATA_SOURCE_ID (para usar uma existente)"
            )

    def destination_kind(self) -> DestinationKind:
        """Return `notion_destination` already converted, silently falling
        back to DATABASE on an unrecognized value — the same rule
        `NoteRecord` uses (`DestinationKind.coerce`, consolidation B2).
        Normalizes (`strip`/`lower`) BEFORE coercing — behavior preserved:
        this was the only one of the three former implementations that
        normalized."""
        return DestinationKind.coerce(self.notion_destination.strip().lower())


def load_settings() -> Settings:
    """Build `Settings`, resolving the `.env` file fresh on every call.

    `Settings.model_config["env_file"]` is only computed once, at import
    time, so it cannot see a `NOTEMCP_ENV` set (or a `.env` created) after
    the module was first loaded. Passing `_env_file` explicitly re-runs
    `_resolve_env_file()` for this call, which is what the CLI and the UI
    bridge — the two real entry points — actually use.
    """
    return Settings(_env_file=_resolve_env_file())
