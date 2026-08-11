"""Janela principal: um QWebEngineView servindo o frontend local."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineScript
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow

from .bridge import Bridge


def _web_dir() -> Path:
    """Locate `ui/web/` both from source and from a frozen PyInstaller bundle.

    `Path(__file__).parent` does not work once frozen: `__file__` points
    into the bundle's synthetic module layout, not a real directory on
    disk. PyInstaller instead extracts (`onedir`) or unpacks (`onefile`,
    not used here — see `packaging/notemcp.spec`) `datas` under
    `sys._MEIPASS`, preserving the `notemcp/ui/web` path configured there.
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "notemcp" / "ui" / "web"
    return Path(__file__).parent / "web"


WEB_DIR = _web_dir()

DISPLAY_NAME = "Lapidary"
"""What the user reads: window title, menu entry, brand row.

Deliberately different from the application name passed to Qt below, which
stays `notemcp`. That string becomes the window's WM_CLASS, and the desktop
entry matches on it via `StartupWMClass` — change one without the other and
the running window stops associating with its pinned launcher, which shows up
as two separate icons in the dock. A display name that differs from the
window-class identifier is ordinary; keeping the identifier stable is what
makes pinning keep working across a rename.
"""


def _qwebchannel_js() -> str:
    """Read `qwebchannel.js` out of Qt's resource system.

    The PySide6 wheel ships this file as a compiled Qt resource rather than
    as something on disk, so there is no path to load it from. The resource
    is only registered once `QtWebChannel` has been imported, which is why
    the read happens inside this function instead of at module level.
    """
    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.ReadOnly):
        raise RuntimeError("could not find qwebchannel.js in Qt's resources")
    try:
        return bytes(f.readAll()).decode("utf-8")
    finally:
        f.close()


class NotePage(QWebEnginePage):
    """Open external links in the system browser.

    Without this, clicking the published page's link would load Notion *inside*
    the app's own window, and the user would lose the interface with no obvious
    way back.
    """

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if nav_type == QWebEnginePage.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class MainWindow(QMainWindow):
    def __init__(self, bridge: Bridge | None = None):
        """Build the window and wire the QWebChannel bridge.

        `bridge` is injectable so tests can supply one backed by a temporary
        store instead of the user's real queue.

        The `qwebchannel` script is injected at `DocumentCreation` rather than
        loaded from disk or run later: `app.js` calls into the channel as soon
        as it executes, so the shim has to already exist by then.
        """
        super().__init__()
        self.setWindowTitle(DISPLAY_NAME)
        self.resize(1280, 820)

        self.view = QWebEngineView(self)
        self.page = NotePage(self.view)
        self.view.setPage(self.page)
        self.setCentralWidget(self.view)

        script = QWebEngineScript()
        script.setName("qwebchannel")
        script.setSourceCode(_qwebchannel_js())
        script.setInjectionPoint(QWebEngineScript.DocumentCreation)
        script.setWorldId(QWebEngineScript.MainWorld)
        script.setRunsOnSubFrames(False)
        self.page.scripts().insert(script)

        self.bridge = bridge or Bridge()
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("bridge", self.bridge)
        self.page.setWebChannel(self.channel)

        self.view.load(QUrl.fromLocalFile(str(WEB_DIR / "index.html")))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("notemcp")
    window = MainWindow()
    window.show()

    # Opt-in only, read once at startup: lets `packaging/build.py`'s smoke
    # test prove the packaged executable actually boots the window (not
    # just the interpreter) by exiting cleanly on its own instead of being
    # killed after an arbitrary timeout. Unset in normal use — nothing
    # here changes ordinary interactive behaviour.
    quit_after_ms = os.environ.get("NOTEMCP_UI_AUTOQUIT_MS")
    if quit_after_ms:
        QTimer.singleShot(int(quit_after_ms), app.quit)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
