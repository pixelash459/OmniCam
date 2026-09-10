"""System-tray behaviour of the main window (offscreen).

X with ``ui.close_to_tray`` on hides the window and leaves the app running;
Quit (tray menu / status-bar button) performs exactly one full shutdown; the
pref is persisted from the checkable tray action; the tray tooltip follows the
status line.  The offscreen platform has no real tray, so
``QSystemTrayIcon.isSystemTrayAvailable`` is monkeypatched where needed.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")


def _pump(app, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    folder = tempfile.mkdtemp(prefix="omnicam_tray_settings_")
    monkeypatch.setenv("OMNICAM_SETTINGS_DIR", folder)
    yield
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def tray_available(monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    # offscreen: isVisible() on a shown tray icon is platform dependent; be explicit
    monkeypatch.setattr(QSystemTrayIcon, "isVisible", lambda self: True)
    monkeypatch.setattr(QSystemTrayIcon, "showMessage", lambda self, *a, **k: None)
    yield


@pytest.fixture
def win(qapp, monkeypatch, tray_available):
    """MainWindow with a (fake) tray and a spied ``OmniCamApp.shutdown``."""
    from PySide6.QtWidgets import QMessageBox

    from omnicam.ui import MainWindow, apply_theme

    apply_theme(qapp)
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    w = MainWindow()
    real_shutdown = w._app.shutdown
    shutdown_calls: list = []

    def spy():
        shutdown_calls.append(True)
        real_shutdown()

    w._app.shutdown = spy
    w.shutdown_calls = shutdown_calls  # type: ignore[attr-defined]
    for name in ("start_stream", "stop_stream", "start_virtual_cam", "stop_virtual_cam"):
        setattr(w._app, name, lambda *a, **k: "obs")
    w.show()
    _pump(qapp, 0.15)
    yield w
    # never leave a tray icon (or app threads) alive after a test
    if not w._shutdown_done:
        w.quit_app()
    _pump(qapp, 0.1)


def test_tray_is_created_and_shown_when_available(qapp, win):
    tray = win.tray()
    assert tray is not None and tray.is_visible()
    labels = [a.text() for a in tray.menu.actions() if not a.isSeparator()]
    assert labels == ["Show OmniCam", "Start Stream", "Stop Stream", "Start Virtual Camera",
                      "Stop Virtual Camera", "Close to tray on X", "Quit OmniCam"]
    assert tray.menu.defaultAction() is tray.act_show
    assert tray.act_close_to_tray.isChecked() is True  # default pref
    assert not win.windowIcon().isNull()
    assert win._btn_quit.text() == "Quit"


def test_close_with_pref_on_hides_to_tray_without_shutdown(qapp, win):
    assert win.isVisible()
    win.close()
    _pump(qapp, 0.1)
    assert not win.isVisible()
    assert win.shutdown_calls == []
    assert not win._shutdown_done
    assert win._render_timer.isActive()  # still alive, just hidden
    assert win.tray() is not None and win._tray_hint_shown is True
    # tray left-click brings it back (showNormal/raise/activate)
    win.toggle_from_tray()
    _pump(qapp, 0.05)
    assert win.isVisible()
    win.toggle_from_tray()
    _pump(qapp, 0.05)
    assert not win.isVisible()
    win.show_from_tray()
    assert win.isVisible()


def test_quit_action_shuts_down_once_and_closes(qapp, win):
    finished: list = []
    win.shutdown_finished.connect(lambda: finished.append(True))
    tray = win.tray()
    tray.act_quit.trigger()
    _pump(qapp, 0.15)
    assert win.shutdown_calls == [True]
    assert not win.isVisible()
    assert win._shutdown_done and win._quitting
    assert win.tray() is None  # tray icon destroyed: no ghost icon
    assert finished == [True]
    assert win._app._threads == []
    # idempotent: further close/shutdown never re-runs the app teardown
    win.close()
    win.shutdown()
    assert win.shutdown_calls == [True]
    assert finished == [True]


def test_quit_button_in_status_bar_quits(qapp, win):
    win._btn_quit.click()
    _pump(qapp, 0.1)
    assert win.shutdown_calls == [True]
    assert not win.isVisible() and win._shutdown_done


def test_close_with_pref_off_does_full_shutdown(qapp, win):
    settings = pytest.importorskip("omnicam.settings")
    settings.set_pref("ui.close_to_tray", False)
    win.close()
    _pump(qapp, 0.1)
    assert win.shutdown_calls == [True]
    assert not win.isVisible() and win._shutdown_done
    assert win.tray() is None


def test_close_without_tray_does_full_shutdown(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox, QSystemTrayIcon

    from omnicam.ui import MainWindow

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    w = MainWindow()
    try:
        w.show()
        _pump(qapp, 0.1)
        assert w.tray() is not None and not w.tray().is_visible()
        w.close()
        _pump(qapp, 0.1)
        assert w._shutdown_done and w._app._threads == []
    finally:
        if not w._shutdown_done:
            w.quit_app()
        _pump(qapp, 0.1)


def test_close_to_tray_action_persists_pref(qapp, win):
    settings = pytest.importorskip("omnicam.settings")
    act = win.tray().act_close_to_tray
    act.setChecked(False)
    assert settings.get_pref("ui.close_to_tray") is False
    assert win.close_to_tray_enabled() is False
    act.setChecked(True)
    assert settings.get_pref("ui.close_to_tray") is True
    act.trigger()  # user click toggles it
    assert settings.get_pref("ui.close_to_tray") is False

    # the pref is honoured by a fresh window (one app at a time: UDP 9921 bind)
    from omnicam.ui import MainWindow

    win.quit_app()
    _pump(qapp, 0.1)
    w2 = MainWindow()
    try:
        assert w2.tray().act_close_to_tray.isChecked() is False
    finally:
        w2.quit_app()
        _pump(qapp, 0.1)


def test_tray_tooltip_follows_status_and_actions_follow_state(qapp, win):
    tray = win.tray()
    assert tray.tooltip().startswith("OmniCam PC — disconnected")
    win._set_status("added 10.0.0.9 — click Connect")
    assert "added 10.0.0.9" in tray.tooltip()
    assert not tray.act_start_stream.isEnabled() and not tray.act_stop_stream.isEnabled()
    assert tray.act_start_vcam.isEnabled() and not tray.act_stop_vcam.isEnabled()

    win._app._emit("state", {"connected": True, "text": "connected to 10.0.0.9"})
    _pump(qapp, 0.2)
    assert tray.tooltip().startswith("OmniCam PC — connected (connected to 10.0.0.9)")
    assert tray.act_start_stream.isEnabled() and not tray.act_stop_stream.isEnabled()

    win._app._streaming = True
    win._app._emit("started", {})
    _pump(qapp, 0.2)
    assert "streaming" in tray.tooltip()
    assert not tray.act_start_stream.isEnabled() and tray.act_stop_stream.isEnabled()
    tray.act_stop_stream.trigger()  # same slot as the button
    assert win._status_msg.text() == "stream stopped"
    win._app._streaming = False

    tray.act_start_vcam.trigger()
    assert not win._btn_vcam_start.isEnabled() and win._btn_vcam_stop.isEnabled()
    assert not tray.act_start_vcam.isEnabled() and tray.act_stop_vcam.isEnabled()
    tray.act_stop_vcam.trigger()
    assert tray.act_start_vcam.isEnabled() and not tray.act_stop_vcam.isEnabled()


def test_icon_helpers_never_raise(qapp):
    from omnicam import tray as tray_mod

    assert not tray_mod.load_app_icon().isNull()
    assert not tray_mod.load_tray_icon(True).isNull()
    assert not tray_mod.load_tray_icon(False).isNull()
