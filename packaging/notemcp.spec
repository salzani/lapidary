# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the notemcp desktop executable.

Run through `packaging/build.py`, never invoked with `pyinstaller` directly
by hand — the wrapper validates the environment first and prints where the
artifact ends up. See `packaging/build.py` and the "Building a desktop
executable" section of the top-level README for the full story.

Decisions that are fixed, not up for reinterpretation per platform:

- **`onedir`, never `onefile`.** PySide6 + QtWebEngine bundles a full
  Chromium (the installed wheel is ~650 MB). `onefile` re-extracts that to a
  temp directory on *every* launch — tens of seconds of startup and a class
  of sandbox failures specific to Chromium running out of a tmpfs. `onedir`
  extracts once, at build time, onto a real directory the app then runs
  from directly.
- **No cross-compilation.** PyInstaller freezes the interpreter and native
  extensions it is currently running under; it cannot target a different
  OS. This spec is expected to run once on Linux (producing a Linux
  binary) and once on Windows (producing a `.exe`) — see the CI matrix in
  `.github/workflows/build.yml`, which is the actual answer for anyone who
  only has one of the two platforms available.
- **UPX is left off.** UPX-compressing Qt/QtWebEngine's shared libraries is
  a well-known source of binaries that fail to load at runtime. The disk
  savings are not worth reintroducing that failure mode.

Anything that differs between Linux and Windows is isolated below behind
`sys.platform` checks, not spread across the file.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

REPO_ROOT = Path(SPECPATH).resolve().parent
SRC_DIR = REPO_ROOT / "src"
WEB_DIR = SRC_DIR / "notemcp" / "ui" / "web"

if not WEB_DIR.is_dir():
    raise SystemExit(f"packaging/notemcp.spec: expected {WEB_DIR} to exist — is the repo layout unchanged?")

APP_NAME = "notemcp"

# The frontend is data, not code: `ui/app.py::_web_dir()` reads it back out
# of `sys._MEIPASS` at exactly this relative path, so the destination here
# and that function have to agree.
datas = [(str(WEB_DIR), str(Path("notemcp") / "ui" / "web"))]

hiddenimports = []

# google-genai resolves a good deal of its surface lazily, so PyInstaller's
# static analysis alone does not find every submodule the provider reaches
# at runtime. `collect_submodules` walks the installed package instead.
#
# This is deliberately a hard failure rather than a best-effort `try/pass`.
# Gemini is the only provider left: a bundle that ships without it does not
# degrade, it has nothing to format with — and it would still pass the
# `--version` and GUI smoke tests, build cleanly, and only fail in the
# user's hands. Refusing to produce that bundle is the whole point.
try:
    hiddenimports += collect_submodules("google.genai")
except Exception as exc:
    raise SystemExit(
        f"packaging/notemcp.spec: could not collect the google-genai package "
        f"({type(exc).__name__}: {exc}).\n"
        "A bundle without google-genai has no provider at all and must not be produced. "
        'Install it first:\n  .venv/bin/python -m pip install -e ".[ui,build]"'
    ) from exc

# `collect_submodules` succeeding is not the same as it having found
# anything useful — it returns a list, and a short list is still a list.
for _required in ("google.genai", "google.genai.types"):
    if _required not in hiddenimports:
        raise SystemExit(
            "packaging/notemcp.spec: collect_submodules('google.genai') did not include "
            f"{_required!r}. `collect_submodules` succeeding is not proof it found "
            "everything — a short list here means the bundle would ship with a broken "
            "Gemini provider. Re-check the google-genai install in this environment "
            "before building."
        )

# OpenSSL, pinned to the interpreter being frozen (ESTADO.md §5).
#
# PyInstaller collects `_ssl.cpython-*.so` from the virtualenv but resolves
# its `libcrypto.so.3`/`libssl.so.3` against the *system* loader path. On a
# conda interpreter those are different builds: the system copy exported up
# to OPENSSL_3.0.3 while conda's `_ssl` required OPENSSL_3.3.0, so every
# `import ssl` inside the bundle died.
#
# That failure was invisible for a long time. Nothing imports `ssl`
# directly — it is reached deep inside `google.genai` and `httpx` — so the
# provider discovery caught the resulting exception and reported a plausible
# but wrong reason ("SDK not installed"), while both smoke tests passed.
#
# Taking the pair from `sys.base_prefix/lib` ships the exact libraries the
# frozen `_ssl` was built against.
openssl_binaries = []
if sys.platform.startswith("linux"):
    _interpreter_lib = Path(sys.base_prefix) / "lib"
    for _soname in ("libcrypto.so.3", "libssl.so.3"):
        _candidate = _interpreter_lib / _soname
        if _candidate.is_file():
            openssl_binaries.append((str(_candidate), "."))

block_cipher = None

a = Analysis(
    [str(REPO_ROOT / "packaging" / "entry_point.py")],
    pathex=[str(SRC_DIR)],
    binaries=openssl_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Keep the console on Linux and macOS, drop it on Windows.
#
# On Linux a console-mode build is what makes `--version` and `--doctor`
# usable from a terminal, and the desktop entry launches without a terminal
# anyway, so nothing is lost. On Windows a console build opens a black
# window behind the GUI every time the app starts from the Start Menu,
# which is why the flag flips there.
console = not sys.platform.startswith("win")

icon = None
if sys.platform.startswith("win"):
    # Only Windows reads an icon from inside the executable. Linux takes it
    # from the `.desktop` entry's `Icon=` key instead (see
    # `packaging/desktop_entry.py`), so there is nothing to embed here.
    #
    # The `.ico` is generated by `packaging/icons.py` and may legitimately
    # not exist yet on a first build, hence the existence check rather than
    # a hard failure.
    _ico = REPO_ROOT / "packaging" / "assets" / "notemcp.ico"
    icon = str(_ico) if _ico.is_file() else None

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=console,
    icon=icon,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
