"""Single command to pin notemcp to the desktop menu / taskbar and back out.

Usage::

    .venv/bin/python packaging/install.py                 # build + install
    .venv/bin/python packaging/install.py --skip-build     # reuse dist/notemcp
    .venv/bin/python packaging/install.py --uninstall       # remove the entry

End to end, a plain install:

1. builds the executable (reuses `packaging/build.py::build()` — the
   PyInstaller invocation and its smoke tests are not duplicated here),
2. generates the app icons,
3. installs the platform's menu entry (a `.desktop` file on Linux, a Start
   Menu `.lnk` on Windows),
4. checks `~/.config/notemcp/.env` and either confirms it, copies the
   repo's, or prints exactly what to create,
5. prints a summary of what was installed and where.

`--uninstall` removes the menu entry and the generated icons. It never
touches `.env` or the notes database — those are the user's data, not
build output.

Sibling modules in this directory (`build.py`, `icons.py`,
`desktop_entry.py`, `env_setup.py`, `windows_shortcut.py`) are loaded by
file path via `importlib`, not by package/module name. `packaging/` is
deliberately not turned into an importable package (no `__init__.py`):
`import packaging` and `import build` would each collide with an
unrelated, commonly-installed PyPI package of the same name
(`packaging`, `build`) if this directory ever ended up on `sys.path`
under its own name.
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGING_DIR = REPO_ROOT / "packaging"
ASSETS_DIR = PACKAGING_DIR / "assets"
ICONS_DIR = ASSETS_DIR / "icons"
ICO_PATH = ASSETS_DIR / "notemcp.ico"


def _load_sibling(filename: str) -> ModuleType:
    """Import a sibling file in `packaging/` by path, under a unique name.

    See the module docstring for why this isn't a plain `import`.
    """
    module_name = f"notemcp_packaging_{Path(filename).stem}"
    spec = importlib.util.spec_from_file_location(module_name, PACKAGING_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


build = _load_sibling("build.py")
icons = _load_sibling("icons.py")
desktop_entry = _load_sibling("desktop_entry.py")
env_setup = _load_sibling("env_setup.py")
windows_shortcut = _load_sibling("windows_shortcut.py")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install or remove the notemcp desktop menu entry.",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="Reuse the existing dist/notemcp build instead of rebuilding.",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help=(
            "Remove the desktop menu entry and generated icons. Never touches .env or the notes database."
        ),
    )
    return parser.parse_args(argv)


def _get_or_build_executable(skip_build: bool) -> Path | None:
    if not skip_build:
        try:
            return build.build()
        except build.BuildError as exc:
            print(f"\nInstall aborted — build failed: {exc}", file=sys.stderr)
            return None

    exe = build._executable_path()
    if not exe.is_file():
        print(
            f"error: --skip-build given but {exe} does not exist.\n"
            "Run 'packaging/install.py' without --skip-build, or run "
            "'packaging/build.py' first.",
            file=sys.stderr,
        )
        return None
    print(f"Skipping build, reusing existing executable at {exe}")
    return exe


def _generate_icons() -> tuple[dict[int, Path], Path]:
    print("-- Icons --")
    png_sources = icons.write_pngs(ICONS_DIR)
    ico_path = icons.write_ico(ICO_PATH)
    print(f"Generated {len(png_sources)} PNG sizes in {ICONS_DIR}")
    print(f"Generated {ico_path}")
    return png_sources, ico_path


def _install_menu_entry(exe: Path, png_sources: dict[int, Path], ico_path: Path) -> None:
    print()
    print("-- Menu entry --")
    if sys.platform.startswith("linux"):
        result = desktop_entry.install(exe, png_sources)
        print(f"Wrote {result.desktop_file}")
        for icon_file in result.icon_files:
            print(f"Installed icon: {icon_file}")
        if result.cache_refreshed:
            print(f"Refreshed caches: {', '.join(result.cache_refreshed)}")
        else:
            print(
                "No desktop/icon cache tools found on PATH — skipped "
                "(that's fine, they're just a display-refresh optimization)."
            )
        if result.validated is True:
            print("desktop-file-validate: OK")
        elif result.validated is False:
            print(f"desktop-file-validate: FAILED\n{result.validation_output}")
        else:
            print("desktop-file-validate not found on PATH — skipped.")
    elif sys.platform.startswith("win"):
        shortcut = windows_shortcut.install(exe, ico_path)
        print(f"Wrote {shortcut}")
    else:
        print(f"No desktop-menu integration implemented for {sys.platform!r} — skipped.")


def _handle_env() -> None:
    print()
    print("-- .env --")
    target = env_setup.target_env_path()
    repo_env = REPO_ROOT / ".env"
    decision = env_setup.decide(
        target_env=target,
        repo_env=repo_env,
        target_exists=target.is_file(),
        repo_exists=repo_env.is_file(),
    )
    if decision.action is env_setup.EnvAction.ALREADY_OK:
        print(f"{target} already exists — the packaged app will find it there.")
    elif decision.action is env_setup.EnvAction.COPIED_FROM_REPO:
        env_setup.apply(decision)
        print(f"Copied {repo_env} -> {target} (permissions set to 600 — it holds a token).")
    else:
        print(env_setup.guidance_message(target))


def _do_install(skip_build: bool) -> int:
    print("== notemcp desktop install ==")
    print()

    exe = _get_or_build_executable(skip_build)
    if exe is None:
        return 1

    print()
    png_sources, ico_path = _generate_icons()
    _install_menu_entry(exe, png_sources, ico_path)
    _handle_env()

    print()
    print("== Summary ==")
    print(f"Executable    : {exe}")
    print(f"Icons         : {ICONS_DIR} (+ {ICO_PATH})")
    if sys.platform.startswith("linux"):
        print(f"Menu entry    : {desktop_entry.desktop_file_path()}")
    elif sys.platform.startswith("win"):
        print(f"Start Menu    : {windows_shortcut.shortcut_path()}")
    print()
    print(
        "Note: the menu entry points at the executable inside this checkout's dist/ "
        "directory. Moving or deleting this checkout breaks it — rerun this script "
        "(or 'packaging/install.py --skip-build' after a fresh 'packaging/build.py') "
        "from the new location to fix it."
    )

    return 0


def _do_uninstall() -> int:
    print("== notemcp desktop uninstall ==")
    print()

    if sys.platform.startswith("linux"):
        removed = desktop_entry.uninstall()
        if removed:
            for path in removed:
                print(f"Removed {path}")
        else:
            print("Nothing to remove (no .desktop entry or icons found).")
    elif sys.platform.startswith("win"):
        if windows_shortcut.uninstall():
            print(f"Removed {windows_shortcut.shortcut_path()}")
        else:
            print("Nothing to remove (no Start Menu shortcut found).")
    else:
        print(f"No desktop-menu integration implemented for {sys.platform!r} — nothing to uninstall.")

    if ASSETS_DIR.is_dir():
        shutil.rmtree(ASSETS_DIR)
        print(f"Removed generated icon assets at {ASSETS_DIR}")

    print()
    print(
        "Your .env and note database were left untouched — those are your data, "
        "not build artifacts."
    )

    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.uninstall:
        return _do_uninstall()
    return _do_install(skip_build=args.skip_build)


if __name__ == "__main__":
    raise SystemExit(main())
