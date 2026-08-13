"""PyInstaller entry point for the notemcp desktop executable.

Lives in `packaging/`, not `src/notemcp/`, on purpose: it is packaging
glue, not application code. `notemcp.spec` points `Analysis` directly at
this file.

Supports `--version` and `--doctor` in addition to launching the GUI.

`--version` exists so the packaged binary can be smoke-tested (see
`packaging/build.py`) without needing a working display or a QtWebEngine
boot — printing a version string and exiting 0 is enough to prove the frozen
interpreter, its bundled stdlib, and the `notemcp` package inside the bundle
are all wired together correctly. It does not prove the GUI itself renders;
`build.py` also runs a GUI-boot smoke test for that under
`QT_QPA_PLATFORM=offscreen`.

`--doctor` reports what the bundle can actually reach: which `.env` it
resolved, whether the Gemini SDK imports, and what provider discovery
returned. Both flags are handled **before importing Qt**, deliberately: a
diagnostic that needs QtWebEngine to start cannot diagnose a QtWebEngine
that will not start.
"""

from __future__ import annotations

import sys


def main() -> int:
    if "--doctor" in sys.argv[1:]:
        from notemcp.doctor import main as run_doctor

        return run_doctor(sys.argv[1:])

    if "--version" in sys.argv[1:]:
        from importlib.metadata import PackageNotFoundError, version

        try:
            print(f"notemcp {version('notemcp')}")
        except PackageNotFoundError:
            print("notemcp (version unknown — package metadata missing from the bundle)")
        return 0

    from notemcp.ui.app import main as run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
