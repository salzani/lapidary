"""Windows Start Menu shortcut creation.

Drives `WScript.Shell`'s `CreateShortcut` through a small PowerShell
snippet instead of using `pywin32` — PowerShell ships with every supported
Windows version, so this adds no new dependency to the `build` extra.

This module's Windows-specific calls (`install`/`uninstall`) have never
run against a real Windows machine — this project's only development
machine is Linux (see ESTADO.md). `_powershell_script` and the path
functions are pure/path-only and are covered by tests that run anywhere;
`install`/`uninstall` are not.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

APP_NAME = "notemcp"


def start_menu_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError(
            "APPDATA is not set in the environment — cannot locate the Start Menu Programs folder."
        )

    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def shortcut_path() -> Path:
    return start_menu_dir() / f"{APP_NAME}.lnk"


def _powershell_script(target: Path, icon: Path, shortcut: Path) -> str:
    """Build the PowerShell snippet that creates the `.lnk`.

    Kept as its own function, separate from the `subprocess.run` call in
    `install`, so the generated script text can be asserted on directly.
    """
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "$WshShell = New-Object -ComObject WScript.Shell\n"
        f'$Shortcut = $WshShell.CreateShortcut("{shortcut}")\n'
        f'$Shortcut.TargetPath = "{target}"\n'
        f'$Shortcut.WorkingDirectory = "{target.parent}"\n'
        f'$Shortcut.IconLocation = "{icon}"\n'
        '$Shortcut.Description = "notemcp"\n'
        "$Shortcut.Save()\n"
    )


def install(exe_path: Path, icon_path: Path) -> Path:
    """Create the Start Menu shortcut. Requires `exe_path` to be absolute
    for the same reason `desktop_entry.desktop_entry_content` requires it:
    a launcher does not start from a predictable working directory.
    """
    if not exe_path.is_absolute():
        raise ValueError(f"exe_path must be absolute, got {exe_path!r}")

    target = shortcut_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    script = _powershell_script(exe_path.resolve(), icon_path, target)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
    )

    return target


def uninstall() -> bool:
    """Remove the Start Menu shortcut, if present. Returns whether it existed."""
    target = shortcut_path()
    if target.is_file():
        target.unlink()
        return True
    return False
