"""UI smoke test: the main window constructs and pumps cleanly offscreen.

Runs with QT_QPA_PLATFORM=offscreen; also verifies graceful degradation when
optional components (pyvirtualcam backend etc.) are missing -- a warning path,
never a crash.
"""

from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")


def _pump(app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def test_main_window_constructs_pumps_and_closes():
    from PySide6.QtWidgets import QApplication

    from omnicam.ui import MainWindow, make_dark_palette

    app = QApplication.instance() or QApplication([])
    app.setPalette(make_dark_palette())  # main() applies this before the window
    win = MainWindow()
    try:
        win.show()
        _pump(app, 0.4)  # let the 30 fps render / poll / stats timers fire
        assert "OmniCam" in win.windowTitle()
        assert win.centralWidget() is not None
        assert win._render_timer.isActive()
        assert win._poll_timer.isActive()
    finally:
        win.close()
        _pump(app, 0.1)
    # shutdown must have stopped the app threads/sockets without exceptions
    assert win._app.streaming is False
    assert win._app._threads == []


def test_main_window_missing_optional_deps_still_constructs(monkeypatch):
    """Graceful degradation: with av/pyvirtualcam 'missing', the window must
    still construct (a warning is shown, not an exception)."""
    from PySide6.QtWidgets import QApplication, QMessageBox

    import omnicam.app as app_mod
    from omnicam.ui import MainWindow

    warnings: list = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warnings.append(a)))
    monkeypatch.setattr(app_mod.OmniCamApp, "import_errors",
                        staticmethod(lambda: [("pyvirtualcam", "pip install pyvirtualcam")]))

    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    try:
        _pump(app, 0.2)
        assert warnings, "missing-component warning path must run"
        assert any("pyvirtualcam" in str(a) for a in warnings)
    finally:
        win.close()
        _pump(app, 0.1)
