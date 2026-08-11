"""Execution off the UI thread.

A local LLM call takes 10-40s, and the Notion write retries with backoff.
Either one on the UI thread freezes the entire window — including repaint,
so not even the spinner would turn.

`QRunnable` + `QThreadPool` instead of `QThread` because each operation is a
single, independent job: there is no state to keep alive between calls, and
the pool manages the lifecycle on its own.
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _TaskSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self._fn = fn
        self.signals = _TaskSignals()

    def run(self) -> None:
        """Run `self._fn` and emit `finished`/`failed`.

        Catches every exception on purpose: this runs on a worker thread, a
        boundary nothing may escape across silently — an uncaught exception
        here would simply vanish instead of reaching the UI.
        """
        try:
            result = self._fn()
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        else:
            self.signals.finished.emit(result)


_active: set[Task] = set()
"""Strong references keeping in-flight `Task`s (and their `_TaskSignals`)
alive.

Containment for the silent failure where `QThreadPool` takes ownership of
the `QRunnable` only on the C++ side, while nothing holds the Python object
or its parentless `_TaskSignals`. Without this strong reference, the garbage
collector can reclaim them before the job finishes, and the signal simply
never arrives — no error, no traceback, the UI just waits forever (bug 8).
"""


def run_async(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None],
    on_error: Callable[[str], None],
) -> None:
    task = Task(fn)
    _active.add(task)

    def release(*_):
        _active.discard(task)

    task.signals.finished.connect(on_done)
    task.signals.failed.connect(on_error)
    task.signals.finished.connect(release)
    task.signals.failed.connect(release)
    QThreadPool.globalInstance().start(task)
