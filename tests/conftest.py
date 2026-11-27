"""Fixtures shared by the UI tests (Qt is imported lazily: non-UI tests run without it)."""

from __future__ import annotations

import gc

import pytest


@pytest.fixture(scope="session")
def _app():
    import ui_support  # noqa: F401 - sets the offscreen / no-3D environment first
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def qapp(_app):
    """Destroy every widget a test created while the QApplication is still alive:
    widgets left to Python's garbage collector at interpreter exit can segfault."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid

    yield _app
    for w in QApplication.topLevelWidgets():
        if not isValid(w):  # already deleted by the test (closed with its parent)
            continue
        if w.parent() is not None:
            # popups (menus, combo box lists) go with their parent; deleting them on
            # their own first crashes with pyqtgraph, which keeps references to its menus
            continue
        runner = getattr(w, "runner", None)
        if runner is not None and runner.running:  # a failed test left a run going
            runner.stop()
            runner.wait(10000)
        if hasattr(w, "confirm_discard"):
            w.confirm_discard = lambda: True  # never block on "save changes?"
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _app.processEvents()
    gc.collect()


@pytest.fixture
def settings(tmp_path):
    """Throw-away QSettings, so tests never touch the user's preferences."""
    from PySide6.QtCore import QSettings

    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
