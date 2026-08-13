"""Linux desktop-menu integration: the `.desktop` entry and its icon theme entries.

`XDG_DATA_HOME` is respected (falling back to `~/.local/share`, per the
XDG Base Directory spec) for both the `applications/` directory the
launcher lives in and the `icons/hicolor/` theme tree the icon sizes are
installed into.

Content generation (`desktop_entry_content`) and path resolution are kept
as pure functions, separate from `install`/`uninstall`'s filesystem and
subprocess side effects, specifically so they can be unit tested without
touching the real filesystem or spawning processes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

APP_ID = "notemcp"
"""The stable identifier, not the product name.

It names the `.desktop` file, every installed icon file, and the
`StartupWMClass` the running window is matched by — all three have to agree
with the application name `ui/app.py` hands to Qt. Renaming the product does
not rename this; see `DISPLAY_NAME` below.
"""

DISPLAY_NAME = "Lapidary"
"""What the menu shows. Matches `ui/app.py::DISPLAY_NAME`."""

ICON_SIZES: tuple[int, ...] = (16, 32, 48, 64, 128, 256)


def xdg_data_home() -> Path:
    override = os.environ.get("XDG_DATA_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share"


def applications_dir() -> Path:
    return xdg_data_home() / "applications"


def desktop_file_path() -> Path:
    return applications_dir() / f"{APP_ID}.desktop"


def icon_dir(size: int) -> Path:
    return xdg_data_home() / "icons" / "hicolor" / f"{size}x{size}" / "apps"


def icon_path(size: int) -> Path:
    return icon_dir(size) / f"{APP_ID}.png"


def desktop_entry_content(exec_path: Path) -> str:
    """Build the `.desktop` file text.

    `exec_path` must be absolute: a desktop launcher does not start the
    process from any predictable working directory (the same reasoning
    `config.py::_resolve_env_file` documents for `.env`), so a relative
    `Exec=` would resolve against whatever happened to be current.

    `StartupWMClass={APP_ID}` is required, not cosmetic:
    `ui/app.py` calls `app.setApplicationName("notemcp")`, which Qt uses as
    the window's WM_CLASS. Without this line GNOME cannot associate the
    running window with the pinned launcher, and shows two separate icons
    in the dock — pinning the app would stop working as intended.
    """
    if not exec_path.is_absolute():
        raise ValueError(f"exec_path must be absolute, got {exec_path}")

    fields = {
        "Type": "Application",
        "Name": DISPLAY_NAME,
        "Comment": "Turn a raw note into a formatted Notion page",
        "Exec": f'"{exec_path}"',
        "Icon": APP_ID,
        "Terminal": "false",
        "Categories": "Utility;TextEditor;",
        "StartupWMClass": APP_ID,
    }
    lines = ["[Desktop Entry]"] + [f"{key}={value}" for key, value in fields.items()]
    return "\n".join(lines) + "\n"


@dataclass
class InstallResult:
    desktop_file: Path
    icon_files: list[Path] = field(default_factory=list)
    cache_refreshed: list[str] = field(default_factory=list)
    validated: bool | None = None
    validation_output: str = ""


def _refresh_caches(apps_dir: Path) -> list[str]:
    """Run the desktop/icon cache refreshers if present, ignoring failure.

    These are optimizations (they make a just-installed entry show up
    immediately instead of after the next cache rebuild), not
    requirements — a failure here must never abort the install.
    """
    refreshed: list[str] = []

    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(
                ["update-desktop-database", str(apps_dir)],
                capture_output=True,
                check=False,
            )
            refreshed.append("update-desktop-database")
        except OSError:
            pass

    if shutil.which("gtk-update-icon-cache"):
        theme_dir = xdg_data_home() / "icons" / "hicolor"
        try:
            subprocess.run(
                ["gtk-update-icon-cache", "-f", "-t", str(theme_dir)],
                capture_output=True,
                check=False,
            )
            refreshed.append("gtk-update-icon-cache")
        except OSError:
            pass

    return refreshed


def _validate(desktop_file: Path) -> tuple[bool | None, str]:
    if not shutil.which("desktop-file-validate"):
        return None, ""
    proc = subprocess.run(
        ["desktop-file-validate", str(desktop_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def install(exec_path: Path, icon_sources: dict[int, Path]) -> InstallResult:
    """Write the `.desktop` entry, install the icon sizes, refresh caches.

    `icon_sources` is the `{size: png_path}` map `icons.write_pngs` returns.
    """
    exec_path = exec_path.resolve()

    apps_dir = applications_dir()
    apps_dir.mkdir(parents=True, exist_ok=True)
    target = desktop_file_path()
    target.write_text(desktop_entry_content(exec_path), encoding="utf-8")

    installed_icons: list[Path] = []
    for size, source in sorted(icon_sources.items()):
        if size not in ICON_SIZES:
            continue
        dest = icon_path(size)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
        installed_icons.append(dest)

    result = InstallResult(desktop_file=target, icon_files=installed_icons)
    result.cache_refreshed = _refresh_caches(apps_dir)
    result.validated, result.validation_output = _validate(target)
    return result


def uninstall() -> list[Path]:
    """Remove the `.desktop` entry and its installed icon sizes.

    Returns the paths actually removed. Never touches `.env` or the notes
    database — those live under `~/.config/notemcp/` and
    `~/.local/share/notemcp/`, this function only ever looks under
    `applications/` and `icons/hicolor/`.
    """
    removed: list[Path] = []

    target = desktop_file_path()
    if target.is_file():
        target.unlink()
        removed.append(target)

    for size in ICON_SIZES:
        path = icon_path(size)
        if path.is_file():
            path.unlink()
            removed.append(path)

    apps_dir = applications_dir()
    if apps_dir.is_dir():
        _refresh_caches(apps_dir)

    return removed
