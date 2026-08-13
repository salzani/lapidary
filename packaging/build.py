"""Single, cross-platform entry point for building the notemcp desktop executable.

Usage::

    .venv/bin/python packaging/build.py

Detects the current OS, validates the environment (Python 3.11+, PySide6,
PyInstaller — installed via the `ui` and `build` extras respectively — and
`google-genai`, a first-class dependency since Gemini became the only
provider), runs PyInstaller against `packaging/notemcp.spec`, then
smoke-tests the artifact it produced before declaring success: `--version`,
a real offscreen GUI boot, and finally `--doctor` against the built
executable — the one of the three that actually reaches `google.genai`, and
that fails the build outright (not just "could not verify") on a broken
verdict. See `src/notemcp/doctor.py` for what it checks.

There is no `--target-os` flag and there will not be one: PyInstaller
freezes the interpreter and native extensions it is *currently running
under* — it cannot cross-compile. This script only ever builds for the
platform it runs on. To get both a Linux and a Windows artifact, run it
once on each — that is exactly what the matrix in
`.github/workflows/build.yml` does, and is the answer for anyone who only
has one of the two platforms available locally.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGING_DIR = REPO_ROOT / "packaging"
SPEC_FILE = PACKAGING_DIR / "notemcp.spec"
DIST_DIR = REPO_ROOT / "dist"
BUILD_DIR = REPO_ROOT / "build"
APP_NAME = "notemcp"

MIN_PYTHON = (3, 11)

GUI_AUTOQUIT_MS = 5000
"""How long the GUI smoke test lets the window live before it quits itself."""

GUI_SMOKE_TIMEOUT_S = 30
"""Hard ceiling on the GUI smoke test, well above `GUI_AUTOQUIT_MS`."""


class BuildError(RuntimeError):
    """Raised with a message that already says what to run next."""


def _check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        raise BuildError(
            f"notemcp requires Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+, this interpreter "
            f"({sys.executable}) is {platform.python_version()}.\n"
            "Recreate the virtualenv with a newer interpreter, then rerun this script, e.g.:\n"
            "  python3.13 -m venv .venv\n"
            '  .venv/bin/python -m pip install -e ".[ui,build]"\n'
            "  .venv/bin/python packaging/build.py"
        )


def _check_module(module: str, install_hint: str) -> None:
    try:
        __import__(module)
    except ImportError as exc:
        raise BuildError(
            f"'{module}' is not importable from this interpreter ({sys.executable}).\n"
            f"Install it with:\n  {install_hint}"
        ) from exc


def _check_system_tools() -> None:
    """Verify the non-Python tools PyInstaller shells out to.

    PyInstaller hard-requires `objdump` on Linux to walk shared-library
    dependencies. It is absent from minimal images — plain Docker bases, CI
    runners without a build toolchain, and trimmed desktop installs — and the
    error it raises names the tool but not the package or the command, so the
    failure arrives after a long analysis phase and points nowhere useful.

    Checking up front turns a late, cryptic stop into an immediate one that
    says exactly what to run. `ldd` being present is not a substitute:
    PyInstaller checks for `objdump` specifically.
    """
    if sys.platform != "linux":
        return
    if shutil.which("objdump"):
        return
    raise BuildError(
        "'objdump' was not found on PATH, and PyInstaller requires it on Linux to "
        "resolve shared-library dependencies.\n"
        "It ships in the 'binutils' package. Install it with whichever applies:\n"
        "  sudo apt install binutils          # Debian, Ubuntu\n"
        "  sudo dnf install binutils          # Fedora, RHEL\n"
        "  sudo pacman -S binutils            # Arch\n"
        "Then rerun this script. Having `ldd` alone is not enough."
    )


def _check_environment() -> None:
    _check_python_version()
    _check_module("PySide6", '.venv/bin/python -m pip install -e ".[ui]"')
    _check_module("PyInstaller", '.venv/bin/python -m pip install -e ".[build]"')
    # google-genai is not an optional extra any more: Gemini is the only
    # provider, so a bundle that cannot import it has no provider at all.
    # Catching that here costs a second; catching it after the build costs a
    # full PyInstaller run, and catching it after shipping costs a user.
    _check_module("google.genai", '.venv/bin/python -m pip install -e ".[ui,build]"')
    _check_system_tools()
    if not SPEC_FILE.is_file():
        raise BuildError(
            f"Spec file not found at {SPEC_FILE} — is the packaging/ directory intact?"
        )


def _run_pyinstaller() -> None:
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_FILE),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(BUILD_DIR),
    ]
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise BuildError(
            f"PyInstaller exited with code {result.returncode}. Scroll up for its own "
            "diagnostics — this wrapper does not swallow or reinterpret them."
        )


def _executable_path() -> Path:
    bundle_dir = DIST_DIR / APP_NAME
    exe_name = f"{APP_NAME}.exe" if sys.platform.startswith("win") else APP_NAME
    return bundle_dir / exe_name


def _dir_size_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _smoke_test_version_flag(exe: Path) -> None:
    """Prove the frozen interpreter and the `notemcp` package inside it boot.

    Does not touch Qt/QtWebEngine at all — `entry_point.py --version`
    short-circuits before importing `notemcp.ui.app`. This alone does not
    prove the GUI renders; `_smoke_test_gui_boot` below covers that. Neither
    one imports `ssl` — which is exactly why both passed on the build
    broken by the OpenSSL/libcrypto bug in ESTADO.md §5. `_smoke_test_doctor`
    is the test that actually reaches `google.genai`/`httpx`.
    """
    print(f"Smoke test 1/3 — {exe.name} --version")

    try:
        result = subprocess.run(
            [str(exe), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            "The built executable did not exit within 30s on '--version'. A build that "
            "produces files but whose executable never starts is worse than no build at "
            "all — investigate before distributing it."
        ) from exc

    if result.returncode != 0:
        raise BuildError(
            f"The built executable exited with code {result.returncode} on '--version'.\n"
            f"stdout: {result.stdout!r}\n"
            f"stderr: {result.stderr!r}"
        )

    if "notemcp" not in result.stdout:
        raise BuildError(
            "The built executable ran but printed unexpected output on '--version': "
            f"{result.stdout!r}"
        )

    print(f"  -> exit 0, stdout: {result.stdout.strip()!r}")


def _smoke_test_gui_boot(exe: Path) -> bool:
    """Best-effort: actually boot the QWebEngineView window and let it quit itself.

    Runs with `QT_QPA_PLATFORM=offscreen` (no real display needed) and sets
    `NOTEMCP_UI_AUTOQUIT_MS` so the app calls `QApplication.quit()` on its
    own after `GUI_AUTOQUIT_MS` — a clean exit 0 is real evidence the window
    was constructed and the event loop ran, not just an inference from a
    timeout.

    Returns True only if that clean exit was observed. Returns False, never
    raises, when it cannot be verified — a GPU-less/driver-less CI runner or
    dev machine is a real possibility (see ESTADO.md) and the caller must
    report "could not verify", not claim success.
    """
    print(f"Smoke test 2/3 — {exe.name} (GUI boot, offscreen, autoquit after {GUI_AUTOQUIT_MS}ms)")

    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    env["NOTEMCP_UI_AUTOQUIT_MS"] = str(GUI_AUTOQUIT_MS)

    try:
        result = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=GUI_SMOKE_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "")[-2000:]
        stderr = (exc.stderr or "")[-2000:]
        print(
            f"  -> did not exit within {GUI_SMOKE_TIMEOUT_S}s even with autoquit armed; "
            f"killed. Could not verify.\n"
            f"     stdout: {stdout!r}\n"
            f"     stderr: {stderr!r}"
        )
        return False

    if result.returncode != 0:
        print(
            f"  -> exited with code {result.returncode} instead of 0. Could not verify.\n"
            f"     stdout: {result.stdout[-2000:]!r}\n"
            f"     stderr: {result.stderr[-2000:]!r}"
        )
        return False

    print("  -> exited 0 after autoquit — window was constructed and the event loop ran.")
    return True


def _smoke_test_doctor(exe: Path) -> None:
    """Run `--doctor` inside the frozen executable and fail the build on a
    broken verdict.

    This is the smoke test the other two are not: `--version` never touches
    Qt, and the GUI boot never calls the model, so a bundle that cannot
    import `google.genai`/`httpx` (the OpenSSL/libcrypto mismatch in
    ESTADO.md §5 — both providers import `ssl` deep and late) sailed through
    both, built, and shipped. `doctor.py` reaches the SDK directly.

    Exit 2 (`doctor.NO_KEY`) passes here on purpose: this machine building
    the bundle does not necessarily have a `GEMINI_API_KEY`, and that is not
    this script's problem to solve. Exit 1 (`doctor.BROKEN`) — SDK missing
    or broken, or discovery raised — fails the build hard, with the full
    doctor report in the error so there is no second step to reproduce it.
    """
    print(f"Smoke test 3/3 — {exe.name} --doctor")

    try:
        result = subprocess.run(
            [str(exe), "--doctor"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            "The built executable did not exit within 30s on '--doctor'. A build that "
            "produces files but whose executable never starts is worse than no build at "
            "all — investigate before distributing it."
        ) from exc

    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if result.returncode == 1:
        raise BuildError(
            "`--doctor` reported this build as broken (exit 1) — see the report printed "
            "above for which section failed. A build that cannot reach its only provider "
            "must not be shipped."
        )

    print(f"  -> exit {result.returncode} ({'ok' if result.returncode == 0 else 'no key, acceptable here'})")


def build() -> Path:
    print(
        f"notemcp packaging — {platform.system()} {platform.release()}, "
        f"Python {platform.python_version()}"
    )

    _check_environment()
    DIST_DIR.mkdir(exist_ok=True)
    _run_pyinstaller()

    exe = _executable_path()
    if not exe.is_file():
        raise BuildError(f"PyInstaller reported success but {exe} does not exist.")

    _smoke_test_version_flag(exe)
    gui_verified = _smoke_test_gui_boot(exe)
    _smoke_test_doctor(exe)

    bundle_dir = exe.parent
    size = _human_size(_dir_size_bytes(bundle_dir))

    print()
    print("Build finished.")
    print(f"  Executable         : {exe}")
    print(f"  Bundle directory   : {bundle_dir}  ({size})")
    print(f"  GUI boot smoke test: {'PASSED' if gui_verified else 'COULD NOT BE VERIFIED on this machine'}")
    if not gui_verified:
        print(
            "  This does not necessarily mean the build is broken — it means this\n"
            "  environment could not confirm it (see ESTADO.md for known limitations\n"
            "  of the machine this was last built on). Run the executable manually\n"
            "  before distributing it if you cannot trust this result."
        )

    print()
    _print_env_guidance(bundle_dir)
    print()
    print(
        "  NOTION_WRITER=mcp is not supported from a packaged build — it shells out to "
        "`npx`, which is not bundled. Use NOTION_WRITER=api (the default)."
    )

    return exe


def _print_env_guidance(bundle_dir: Path) -> None:
    """Tell the user where to put `.env` before they run the bundle.

    A packaged app is normally launched from a desktop entry or from its own
    directory, so the repository's `.env` is not on the search path and the
    app starts with no credentials. That looks like a broken build rather
    than missing setup, so say it here — while the person still has the
    build output in front of them — instead of letting them discover it from
    an error later.
    """
    user_config = Path.home() / ".config" / "notemcp" / ".env"
    repo_env = REPO_ROOT / ".env"

    if repo_env.is_file():
        print(f"  Config: the executable reads {repo_env} directly.")
        print("  Nothing to copy — it finds the checkout it was built in.")
        print(f"  Move or delete this checkout and it falls back to {user_config}.")
        return

    print("  Before running it, make sure a .env exists somewhere it will be found.")
    print(f"  Simplest while you keep this checkout: {repo_env}")
    print(f"  Independent of the checkout: {user_config}")
    print()
    print("  Either needs at least:")
    print("    NOTION_TOKEN=secret_...")
    print("    NOTION_PARENT_PAGE_ID=...")
    print()
    print(f"  A .env in {bundle_dir} also works, but is lost on the")
    print("  next build — that directory is deleted and recreated each time.")


def main() -> int:
    try:
        build()
    except BuildError as exc:
        print(f"\nBuild failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
