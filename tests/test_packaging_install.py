"""Tests for the desktop-install layer (`packaging/`).

`packaging/` is deliberately not an importable Python package (see
`packaging/install.py`'s module docstring for why) — sibling modules are
loaded here the same way `install.py` loads them, by file path via
`importlib`, under names that cannot collide with anything on `sys.path`.

Nothing here touches the real user filesystem: every path exercised is
under `tmp_path`, and `XDG_DATA_HOME`/`APPDATA`/`Path.home()` are
monkeypatched before any function that would otherwise resolve against the
real home directory is called. No test spawns PyInstaller or `powershell`.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGING_DIR = REPO_ROOT / "packaging"


def _load(filename: str):
    module_name = f"test_load_{Path(filename).stem}"
    spec = importlib.util.spec_from_file_location(module_name, PACKAGING_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


icons = _load("icons.py")
desktop_entry = _load("desktop_entry.py")
env_setup = _load("env_setup.py")
windows_shortcut = _load("windows_shortcut.py")
install = _load("install.py")


# --------------------------------------------------------------- desktop_entry


def test_desktop_entry_content_rejects_relative_exec_path():
    with pytest.raises(ValueError):
        desktop_entry.desktop_entry_content(Path("dist/notemcp/notemcp"))


def test_desktop_entry_content_has_the_required_fields():
    content = desktop_entry.desktop_entry_content(Path("/opt/notemcp/dist/notemcp/notemcp"))

    assert content.startswith("[Desktop Entry]\n")
    assert "Type=Application" in content
    assert "Name=Lapidary" in content, "the menu entry shows the product name"
    assert "Icon=notemcp" in content, (
        "the icon name is the stable APP_ID, not the display name — it has to keep "
        "matching the installed icon filenames"
    )
    assert 'Exec="/opt/notemcp/dist/notemcp/notemcp"' in content
    assert "Icon=notemcp" in content
    assert "Terminal=false" in content
    assert "Categories=Utility;TextEditor;" in content
    # The GNOME pinning fix — without this, a running window is not
    # associated with the pinned launcher (see the docstring for why).
    assert "StartupWMClass=notemcp" in content


def test_xdg_data_home_respects_the_environment_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "custom-data"))
    assert desktop_entry.xdg_data_home() == tmp_path / "custom-data"


def test_xdg_data_home_falls_back_to_local_share(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert desktop_entry.xdg_data_home() == tmp_path / ".local" / "share"


def test_icon_path_uses_the_hicolor_theme_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert desktop_entry.icon_path(48) == tmp_path / "icons" / "hicolor" / "48x48" / "apps" / "notemcp.png"


def test_desktop_entry_install_and_uninstall_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    exe = tmp_path / "dist" / "notemcp" / "notemcp"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"fake-executable")

    icon_sources = {}
    for size in (16, 32):
        source = tmp_path / f"source-{size}.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        icon_sources[size] = source

    result = desktop_entry.install(exe, icon_sources)

    assert result.desktop_file == tmp_path / "applications" / "notemcp.desktop"
    assert result.desktop_file.is_file()
    content = result.desktop_file.read_text(encoding="utf-8")
    assert f'Exec="{exe.resolve()}"' in content
    assert "StartupWMClass=notemcp" in content

    assert len(result.icon_files) == 2
    for path in result.icon_files:
        assert path.is_file()

    # desktop-file-validate is present on this dev machine; if it's ever
    # absent (e.g. a minimal CI image), `validated` is None rather than
    # False — a generator bug is the only thing that should produce False.
    assert result.validated is not False, result.validation_output

    removed = desktop_entry.uninstall()
    assert result.desktop_file in removed
    assert not result.desktop_file.is_file()
    for path in result.icon_files:
        assert path not in removed or not path.is_file()
    for path in result.icon_files:
        assert not path.is_file()


# --------------------------------------------------------------------- icons


def test_render_icon_has_the_requested_size():
    image = icons.render_icon(32)
    assert image.width() == 32
    assert image.height() == 32


def test_render_png_bytes_is_a_real_png():
    data = icons.render_png_bytes(16)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_write_pngs_writes_one_file_per_size(tmp_path):
    written = icons.write_pngs(tmp_path, sizes=(16, 32, 64))
    assert set(written) == {16, 32, 64}
    for size, path in written.items():
        assert path == tmp_path / f"{size}.png"
        assert path.is_file()
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_build_ico_bytes_has_a_valid_icondir_header():
    sizes = (16, 32, 48)
    data = icons.build_ico_bytes(sizes=sizes)

    reserved, image_type, count = struct.unpack_from("<HHH", data, 0)
    assert reserved == 0
    assert image_type == 1  # ICO, not CUR
    assert count == len(sizes)

    offset = 6
    total_frame_bytes = 0
    for _ in sizes:
        width, height, color_count, reserved2, planes, bit_count, size_bytes, image_offset = (
            struct.unpack_from("<BBBBHHII", data, offset)
        )
        assert color_count == 0
        assert reserved2 == 0
        assert planes == 1
        assert bit_count == 32
        assert data[image_offset : image_offset + 8] == b"\x89PNG\r\n\x1a\n"
        total_frame_bytes += size_bytes
        offset += 16

    assert len(data) == 6 + len(sizes) * 16 + total_frame_bytes


def test_write_ico_writes_a_file(tmp_path):
    target = tmp_path / "notemcp.ico"
    result = icons.write_ico(target, sizes=(16, 32))
    assert result == target
    assert target.is_file()
    assert target.read_bytes()[:4] == struct.pack("<HHH", 0, 1, 2)[:4]


# ------------------------------------------------------------------ env_setup


def test_target_env_path_uses_the_given_home(tmp_path):
    assert env_setup.target_env_path(tmp_path) == tmp_path / ".config" / "notemcp" / ".env"


def test_decide_already_ok_when_target_exists(tmp_path):
    decision = env_setup.decide(
        target_env=tmp_path / "target" / ".env",
        repo_env=tmp_path / "repo" / ".env",
        target_exists=True,
        repo_exists=True,
    )
    assert decision.action is env_setup.EnvAction.ALREADY_OK
    assert decision.source is None


def test_decide_copies_from_repo_when_target_missing_but_repo_has_one(tmp_path):
    decision = env_setup.decide(
        target_env=tmp_path / "target" / ".env",
        repo_env=tmp_path / "repo" / ".env",
        target_exists=False,
        repo_exists=True,
    )
    assert decision.action is env_setup.EnvAction.COPIED_FROM_REPO
    assert decision.source == tmp_path / "repo" / ".env"


def test_decide_missing_when_neither_exists(tmp_path):
    decision = env_setup.decide(
        target_env=tmp_path / "target" / ".env",
        repo_env=tmp_path / "repo" / ".env",
        target_exists=False,
        repo_exists=False,
    )
    assert decision.action is env_setup.EnvAction.MISSING


def test_apply_copies_and_chmods_600(tmp_path):
    repo_env = tmp_path / "repo" / ".env"
    repo_env.parent.mkdir(parents=True)
    repo_env.write_text("NOTION_TOKEN=secret_x\n")
    target = tmp_path / "config" / "notemcp" / ".env"

    decision = env_setup.EnvDecision(env_setup.EnvAction.COPIED_FROM_REPO, target, repo_env)
    env_setup.apply(decision)

    assert target.read_text() == "NOTION_TOKEN=secret_x\n"
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600


def test_apply_is_a_no_op_for_already_ok_and_missing(tmp_path):
    target = tmp_path / ".env"
    for action in (env_setup.EnvAction.ALREADY_OK, env_setup.EnvAction.MISSING):
        env_setup.apply(env_setup.EnvDecision(action, target))
        assert not target.exists()


def test_guidance_message_names_the_required_lines(tmp_path):
    target = tmp_path / ".config" / "notemcp" / ".env"
    message = env_setup.guidance_message(target)
    assert str(target) in message
    assert "NOTION_TOKEN=" in message
    assert "NOTION_PARENT_PAGE_ID=" in message


# ------------------------------------------------------------- windows_shortcut


def test_start_menu_dir_requires_appdata(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(RuntimeError):
        windows_shortcut.start_menu_dir()


def test_start_menu_dir_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert (
        windows_shortcut.start_menu_dir()
        == tmp_path / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    )


def test_shortcut_path_is_notemcp_lnk(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert windows_shortcut.shortcut_path().name == "notemcp.lnk"


def test_powershell_script_references_target_and_icon(tmp_path):
    target = tmp_path / "dist" / "notemcp" / "notemcp.exe"
    icon = tmp_path / "assets" / "notemcp.ico"
    shortcut = tmp_path / "Programs" / "notemcp.lnk"

    script = windows_shortcut._powershell_script(target, icon, shortcut)

    assert "New-Object -ComObject WScript.Shell" in script
    assert f'$Shortcut.TargetPath = "{target}"' in script
    assert f'$Shortcut.IconLocation = "{icon}"' in script
    assert f'CreateShortcut("{shortcut}")' in script
    assert "$Shortcut.Save()" in script


def test_windows_install_rejects_relative_exe_path(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    with pytest.raises(ValueError):
        windows_shortcut.install(Path("notemcp.exe"), tmp_path / "notemcp.ico")


# ----------------------------------------------------------------------- build.py
#
# `build` is the same module `install.py` itself loads by path (`install.build`,
# bound at import time above) — no separate `_load` call needed, and it is
# what keeps this file from loading `build.py` twice under two different
# module names.


def test_check_module_accepts_google_genai_since_it_is_a_first_class_dependency():
    """H5/R5: `google-genai` moved from an optional extra to a hard
    dependency — a build environment that has PySide6/PyInstaller but not
    `google.genai` must be caught here, before PyInstaller ever runs."""
    build = install.build
    build._check_module("google.genai", "pip install ...")  # must not raise


def _fake_executable(tmp_path: Path, exit_code: int, stdout: str = "notemcp doctor fake output") -> Path:
    exe = tmp_path / "fake-notemcp"
    exe.write_text(f"#!/bin/sh\necho '{stdout}'\nexit {exit_code}\n")
    exe.chmod(0o755)
    return exe


@pytest.mark.parametrize("exit_code", [0, 2])
def test_smoke_test_doctor_passes_on_ok_or_no_key(tmp_path, exit_code, capsys):
    """H3/H5: exit 0 (`doctor.OK`) and exit 2 (`doctor.NO_KEY` — this
    machine legitimately has no API key) must both let the build succeed."""
    build = install.build
    exe = _fake_executable(tmp_path, exit_code)

    build._smoke_test_doctor(exe)  # must not raise

    assert "fake output" in capsys.readouterr().out


def test_smoke_test_doctor_fails_the_build_on_a_broken_verdict(tmp_path):
    """H3/H5: exit 1 (`doctor.BROKEN` — SDK missing/broken, or discovery
    raised) must fail the build outright. This is the check that would have
    caught the OpenSSL/libcrypto bug (ESTADO.md §5) instead of shipping it —
    the other two smoke tests (`--version`, GUI boot) both pass on that
    broken build, since neither one imports `ssl`."""
    build = install.build
    exe = _fake_executable(tmp_path, 1, stdout="4. Gemini SDK\n   [FAIL] import google.genai: OSError")

    with pytest.raises(build.BuildError, match="broken"):
        build._smoke_test_doctor(exe)


# ------------------------------------------------------------------- install.py


def test_parse_args_defaults():
    args = install._parse_args([])
    assert args.skip_build is False
    assert args.uninstall is False


def test_parse_args_flags():
    args = install._parse_args(["--skip-build"])
    assert args.skip_build is True

    args = install._parse_args(["--uninstall"])
    assert args.uninstall is True


def test_install_and_uninstall_round_trip_writes_only_under_tmp_path(monkeypatch, tmp_path, capsys):
    """Full `install.main()` orchestration, entirely inside `tmp_path`.

    Stubs `build._executable_path()` so no PyInstaller run is needed, and
    points every home/XDG lookup at `tmp_path` so nothing under the real
    `~/.local/share`, `~/.config`, or this repo's own `packaging/assets/`
    is ever touched.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("XDG_DATA_HOME", str(fake_home / ".local" / "share"))
    # Point at a repo root with no .env of its own — this project's real
    # .env holds a live Notion token, and the round trip below must never
    # read it, even to copy it into a throwaway tmp_path.
    monkeypatch.setattr(install, "REPO_ROOT", tmp_path / "fake-repo-with-no-env")

    fake_exe = tmp_path / "dist" / "notemcp" / "notemcp"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"fake-executable")
    monkeypatch.setattr(install.build, "_executable_path", lambda: fake_exe)

    fake_assets = tmp_path / "packaging-assets"
    monkeypatch.setattr(install, "ASSETS_DIR", fake_assets)
    monkeypatch.setattr(install, "ICONS_DIR", fake_assets / "icons")
    monkeypatch.setattr(install, "ICO_PATH", fake_assets / "notemcp.ico")

    exit_code = install.main(["--skip-build"])
    assert exit_code == 0

    desktop_file = fake_home / ".local" / "share" / "applications" / "notemcp.desktop"
    assert desktop_file.is_file()
    assert (fake_assets / "icons").is_dir()
    env_target = fake_home / ".config" / "notemcp" / ".env"
    # No repo .env was made visible to this run and none exists at the
    # target — guidance should have been printed, nothing written.
    assert not env_target.exists()

    out = capsys.readouterr().out
    assert "notemcp desktop install" in out

    exit_code = install.main(["--uninstall"])
    assert exit_code == 0
    assert not desktop_file.is_file()
    assert not fake_assets.exists()
    # Never touched, per the explicit constraint on --uninstall.
    assert not env_target.exists()
