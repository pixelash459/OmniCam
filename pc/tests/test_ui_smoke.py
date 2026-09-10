"""UI smoke tests: the main window constructs, pumps and closes offscreen, and
every handler / event kind of the redesigned window still drives the
``OmniCamApp`` contract.

Runs with QT_QPA_PLATFORM=offscreen; also verifies graceful degradation when
optional components (pyvirtualcam backend etc.) are missing -- a warning path,
never a crash.  Events are injected through ``OmniCamApp._emit`` exactly like
the network threads do, and drained by the window's 100 ms poll timer.
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
    """Never touch the real %APPDATA% settings from the UI tests."""
    folder = tempfile.mkdtemp(prefix="omnicam_ui_settings_")
    monkeypatch.setenv("OMNICAM_SETTINGS_DIR", folder)
    yield
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture
def win(qapp, monkeypatch):
    """A MainWindow whose network-facing app calls are recorded, not executed."""
    from PySide6.QtWidgets import QMessageBox

    from omnicam.ui import MainWindow, apply_theme

    apply_theme(qapp)
    warnings: list = []
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warnings.append(a)))
    w = MainWindow()
    calls: list = []

    def rec(name):
        def _f(*a, **k):
            calls.append((name, a, k))
            if name == "start_virtual_cam":
                return "obs"
            if name in ("push_session", "push_filter"):
                return dict(a[0]) if a else {}
            return None
        return _f

    for name in ("connect", "disconnect", "start_stream", "stop_stream", "push_session",
                 "set_camera", "set_torch", "set_zoom", "set_bitrate", "set_abr",
                 "start_virtual_cam", "stop_virtual_cam", "set_local_adjust", "push_filter",
                 "pin_device"):
        setattr(w._app, name, rec(name))
    w.calls = calls          # type: ignore[attr-defined]
    w.warnings = warnings    # type: ignore[attr-defined]
    w.show()
    _pump(qapp, 0.15)
    yield w
    w.close()
    _pump(qapp, 0.1)


def _names(win):
    return [c[0] for c in win.calls]


def _emit(win, qapp, kind, payload=None):
    win._app._emit(kind, payload or {})
    _pump(qapp, 0.15)  # poll timer is 100 ms


# ---------------------------------------------------------------------------
# original smoke tests (kept)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# theme / layout
# ---------------------------------------------------------------------------
def test_theme_applies_and_window_fits_1100x700(qapp, win):
    from omnicam import ui_theme

    assert qapp.styleSheet(), "stylesheet must be installed by apply_theme"
    assert ui_theme.build_stylesheet().count("url(") >= 3  # generated icons
    win.resize(1100, 700)
    _pump(qapp, 0.1)
    assert win.width() == 1100 and win.height() == 700
    assert win._video_label.width() > 600
    # the sidebar stays a fixed column and every card exists
    for attr in ("_device_list", "_saved_combo", "_res_combo", "_btn_front",
                 "_pf_look", "_la_brightness", "_btn_vcam_start", "_stats_strip"):
        assert getattr(win, attr) is not None


# ---------------------------------------------------------------------------
# connection card
# ---------------------------------------------------------------------------
def test_connect_disconnect_and_manual_ip(qapp, win):
    win._manual_ip.setText("192.168.1.55")
    win._on_add_manual_ip()
    assert ("pin_device", ("192.168.1.55",), {}) in win.calls
    assert "added 192.168.1.55" in win._status_msg.text()

    win._on_connect()
    assert ("connect", ("192.168.1.55",), {}) in win.calls

    win._manual_ip.setText("999.1.1")  # invalid -> warning, no connect
    n = _names(win).count("connect")
    win._on_connect()
    assert _names(win).count("connect") == n
    assert any("Not a valid IPv4" in str(a) for a in win.warnings)

    win._manual_ip.setText("bad")
    win._on_add_manual_ip()
    assert any("Type the phone's IPv4" in str(a) for a in win.warnings)

    win._on_disconnect()  # Disconnect == bye
    assert "disconnect" in _names(win)


def test_state_event_drives_button_enabling(qapp, win):
    assert not win._btn_disconnect.isEnabled() and win._btn_connect.isEnabled()
    _emit(win, qapp, "state", {"connected": True, "text": "connected to 10.0.0.2"})
    assert win._btn_disconnect.isEnabled() and not win._btn_connect.isEnabled()
    assert win._btn_start.isEnabled() and not win._btn_stop.isEnabled()
    assert win._btn_front.isEnabled() and win._btn_back.isEnabled()
    assert win._torch_check.isEnabled()  # default camera is back
    assert win._status_conn.text().startswith("connected")
    assert win._status_conn.property("connected") is True

    win._app._streaming = True
    _emit(win, qapp, "state", {"connected": True, "text": "connected"})
    assert not win._btn_start.isEnabled() and win._btn_stop.isEnabled()
    win._app._streaming = False

    _emit(win, qapp, "state", {"connected": False, "text": "disconnected"})
    assert not win._btn_disconnect.isEnabled() and win._btn_connect.isEnabled()
    assert not win._btn_front.isEnabled() and not win._torch_check.isEnabled()
    assert win._status_conn.text().startswith("disconnected")


def test_devices_list_labels_and_selection(qapp, win):
    assert win._device_list.count() == 1  # placeholder
    assert "Searching" in win._device_list.item(0).text()
    devices = [
        {"ip": "10.0.0.5", "name": "Ashu iPhone", "model": "iPhone15,2", "online": True,
         "streaming": True},
        {"ip": "10.0.0.9", "name": "", "model": "manual", "manual": True, "online": False},
    ]
    win._app.get_devices = lambda: devices
    _emit(win, qapp, "devices")
    texts = [win._device_list.item(i).text() for i in range(win._device_list.count())]
    assert texts[0] == "Ashu iPhone  [iPhone15,2]  10.0.0.5  (streaming)"
    assert texts[1] == "10.0.0.9  (manual)"
    win._select_device_ip("10.0.0.9")
    assert win._selected_device_ip() == "10.0.0.9"
    win._manual_ip.clear()
    win._on_connect()  # uses the selected device
    assert ("connect", ("10.0.0.9",), {}) in win.calls
    # unchanged snapshot must not rebuild (selection survives)
    win._refresh_devices()
    assert win._selected_device_ip() == "10.0.0.9"


# ---------------------------------------------------------------------------
# saved phones + preferences
# ---------------------------------------------------------------------------
def test_saved_phones_connect_forget_and_autoconnect_pref(qapp, win):
    settings = pytest.importorskip("omnicam.settings")
    settings.remember_phone("10.0.0.1", name="Old", device="iPhone12,1")
    time.sleep(0.01)
    settings.remember_phone("10.0.0.2", name="New", device="iPhone15,2")
    win._refresh_saved_phones()
    # live LAN beacons may add real phones on top; the combo mirrors known_phones()
    expected = [p["ip"] for p in settings.known_phones()]
    ips = [win._saved_combo.itemData(i) for i in range(win._saved_combo.count())]
    assert ips == expected
    assert ips.index("10.0.0.2") < ips.index("10.0.0.1")  # most recent first
    idx = ips.index("10.0.0.2")
    assert win._saved_combo.itemText(idx) == "New  10.0.0.2"
    assert win._btn_saved_connect.isEnabled() and win._btn_saved_forget.isEnabled()

    win._saved_combo.setCurrentIndex(idx)
    win._on_saved_connect()
    assert ("connect", ("10.0.0.2",), {}) in win.calls

    win._on_saved_forget()
    assert "10.0.0.2" not in [p["ip"] for p in settings.known_phones()]
    assert "10.0.0.2" not in [win._saved_combo.itemData(i)
                              for i in range(win._saved_combo.count())]
    assert "forgot 10.0.0.2" in win._status_msg.text()

    win._autoconnect_check.setChecked(False)
    assert settings.get_pref("autoconnect") is False
    win._autoconnect_check.setChecked(True)
    assert settings.get_pref("autoconnect") is True


def test_stream_prefs_and_geometry_persist_across_windows(qapp, monkeypatch):
    settings = pytest.importorskip("omnicam.settings")
    from PySide6.QtWidgets import QMessageBox

    from omnicam.ui import MainWindow

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    w1 = MainWindow()
    try:
        w1._app.push_session = lambda u: u
        w1.resize(1180, 760)
        w1.show()
        _pump(qapp, 0.1)
        w1._res_combo.setCurrentIndex(1)
        w1._fps_combo.setCurrentIndex(2)
        w1._kbps_combo.setCurrentIndex(4)
        _pump(qapp, 0.05)
        assert settings.get_pref("stream.res") == "1920x1080"
        assert settings.get_pref("stream.fps") == 15
        assert settings.get_pref("stream.kbps") == 6000
        w1._filters_section.set_expanded(True)
        assert settings.get_pref("ui.filters_open") is True
        size1 = (w1.width(), w1.height())  # offscreen screens may clamp the resize
        geo1 = bytes(w1.saveGeometry().toHex()).decode("ascii")
    finally:
        w1.close()
        _pump(qapp, 0.1)
    assert settings.get_pref("window.geometry") == geo1

    pushes: list = []
    w2 = MainWindow()
    try:
        w2._app.push_session = lambda u: pushes.append(u)
        w2.show()
        _pump(qapp, 0.4)
        assert (w2._res_combo.currentIndex(), w2._fps_combo.currentIndex(),
                w2._kbps_combo.currentIndex()) == (1, 2, 4)
        assert w2._filters_section.is_expanded()
        # the offscreen platform clamps widths to its virtual screen; the
        # height (and the round-tripped pref above) prove restoreGeometry ran
        assert w2.height() == size1[1]
        assert pushes == [], "restoring prefs must not push a session"
    finally:
        w2.close()
        _pump(qapp, 0.1)


def test_autoconnect_last_called_on_startup_when_enabled(qapp, monkeypatch):
    settings = pytest.importorskip("omnicam.settings")
    import omnicam.app as app_mod
    from PySide6.QtWidgets import QMessageBox

    from omnicam.ui import MainWindow

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    settings.set_pref("autoconnect", True)
    called: list = []
    if hasattr(app_mod.OmniCamApp, "autoconnect_last"):
        monkeypatch.setattr(app_mod.OmniCamApp, "autoconnect_last",
                            lambda self: called.append(True) or "10.0.0.7")
    w = MainWindow()
    try:
        w.show()
        _pump(qapp, 0.2)
        if hasattr(app_mod.OmniCamApp, "autoconnect_last"):
            assert called == [True]
            assert "autoconnecting to 10.0.0.7" in w._status_msg.text()
    finally:
        w.close()
        _pump(qapp, 0.1)

    settings.set_pref("autoconnect", False)
    called.clear()
    w = MainWindow()
    try:
        w.show()
        _pump(qapp, 0.2)
        assert called == []
    finally:
        w.close()
        _pump(qapp, 0.1)


# ---------------------------------------------------------------------------
# stream / session
# ---------------------------------------------------------------------------
def test_start_stop_stream_and_session_debounce(qapp, win):
    win._res_combo.setCurrentIndex(1)
    win._fps_combo.setCurrentIndex(1)
    win._kbps_combo.setCurrentIndex(2)
    assert "push_session" not in _names(win)  # debounced (250 ms)
    _pump(qapp, 0.4)
    pushes = [c for c in win.calls if c[0] == "push_session"]
    assert len(pushes) == 1
    assert pushes[0][1][0] == {"w": 1920, "h": 1080, "fps": 24, "kbps": 2000}

    win._on_start_stream()
    assert ("start_stream", (1920, 1080, 24, 2000), {}) in win.calls

    _emit(win, qapp, "started", {})
    assert not win._btn_start.isEnabled() and win._btn_stop.isEnabled()
    assert "streaming" in win._status_msg.text()

    win._on_stop_stream()
    assert "stop_stream" in _names(win)
    assert not win._btn_stop.isEnabled()
    assert win._status_msg.text() == "stream stopped"

    _emit(win, qapp, "stopped", {})
    assert win._status_msg.text() == "stream stopped by phone"
    _emit(win, qapp, "stopped", {"local": True})
    assert win._status_msg.text() == "stream stopped"


def test_started_with_manual_bitrate_disables_abr(qapp, win):
    win._abr_check.setChecked(False)
    assert ("set_bitrate", (3000,), {}) in win.calls  # abr off -> manual kbps
    win.calls.clear()
    _emit(win, qapp, "started", {})
    assert ("set_bitrate", (3000,), {}) in win.calls
    win._abr_check.setChecked(True)
    assert ("set_abr", (True,), {}) in win.calls


def test_session_sync_does_not_echo_and_respects_camera_caps(qapp, win):
    win._app._camera_caps = {"front": [1280, 720, 30], "back": [1920, 1080, 60]}
    state = {"w": 1920, "h": 1080, "fps": 24, "kbps": 2500, "abr": False, "torch": True,
             "zoom": 2.5, "camera": "back"}
    _emit(win, qapp, "session", {"state": state})
    _pump(qapp, 0.35)
    assert "push_session" not in _names(win)
    assert "set_torch" not in _names(win) and "set_zoom" not in _names(win)
    assert win._res_combo.currentIndex() == 1
    assert win._fps_combo.currentIndex() == 1
    assert win._kbps_combo.currentIndex() == 2  # nearest of 2500 -> 2000 or 3000 (2000 first)
    assert win._abr_check.isChecked() is False
    assert win._torch_check.isChecked() is True
    assert win._zoom_spin.value() == pytest.approx(2.5)
    assert win._cam_label.text() == "camera: back"

    # front camera caps: 1080p disabled and selection falls back to 720p
    win._app._camera = "front"
    _emit(win, qapp, "session", {"state": {"camera": "front"}})
    assert not win._res_combo.model().item(1).isEnabled()
    assert win._res_combo.currentIndex() == 0
    assert win._torch_check.isEnabled() is False


def test_welcome_event_variants(qapp, win):
    _emit(win, qapp, "state", {"connected": True, "text": "connected"})
    _emit(win, qapp, "welcome", {"device": "iPhone15,2", "ios": "17.5", "camera": "front"})
    assert win._status_msg.text() == "welcome from iPhone15,2 (iOS 17.5)"
    assert win._cam_label.text() == "camera: front"
    assert win._torch_check.isEnabled() is False
    assert win._btn_start.isEnabled()
    _emit(win, qapp, "welcome", {"device": "X", "ios": "1",
                                 "session": {"w": 1280, "h": 720, "fps": 15, "kbps": 500,
                                             "camera": "back"}})
    assert win._fps_combo.currentIndex() == 2 and win._kbps_combo.currentIndex() == 0
    assert win._cam_label.text() == "camera: back"


# ---------------------------------------------------------------------------
# camera
# ---------------------------------------------------------------------------
def test_camera_torch_zoom_handlers(qapp, win):
    _emit(win, qapp, "state", {"connected": True, "text": "connected"})
    win._torch_check.setChecked(True)
    assert ("set_torch", (True,), {}) in win.calls
    win._on_camera("front")
    assert ("set_camera", ("front",), {}) in win.calls
    assert "switching" in win._cam_label.text()
    assert win._torch_check.isChecked() is False and not win._torch_check.isEnabled()
    win._on_camera("back")
    assert win._torch_check.isEnabled()
    win._zoom_spin.setValue(3.0)
    assert ("set_zoom", (3.0,), {}) in win.calls
    _emit(win, qapp, "camera_ok", {"id": "front"})
    assert win._cam_label.text() == "camera: front"
    assert win._status_msg.text() == "camera switched to front"
    _emit(win, qapp, "torch_ok", {"on": True})
    assert win._status_msg.text() == "torch: on"
    _emit(win, qapp, "bitrate_ok", {"kbps": 1000, "auto": False})
    assert win._status_msg.text() == "bitrate: 1000 kbps (auto=False)"


# ---------------------------------------------------------------------------
# phone filters
# ---------------------------------------------------------------------------
def test_phone_filter_push_debounced_and_structured(qapp, win):
    win._pf_look.setCurrentIndex(win._pf_look.findText("sepia"))
    win._pf_beauty.setValue(40)
    win._pf_stylize.setCurrentIndex(win._pf_stylize.findText("twirl"))
    win._pf_amount.setValue(75)
    win._pf_mirror.setChecked(True)
    win._pf_rotate.setCurrentIndex(2)
    win._pf_zoom.setValue(1.5)
    win._pf_panx.setValue(-20)
    win._pf_pany.setValue(10)
    assert "push_filter" not in _names(win)  # coalesced (150 ms)
    _pump(qapp, 0.3)
    pushes = [c for c in win.calls if c[0] == "push_filter"]
    assert len(pushes) == 1
    assert pushes[0][1][0] == {
        "look": "sepia", "beauty": 0.4, "stylize": "twirl", "stylize_amount": 0.75,
        "geometry": {"mirror": True, "flipV": False, "rotate": 180, "zoom": 1.5,
                     "panX": -0.2, "panY": 0.1},
    }
    assert "filter pushed: look=sepia stylize=twirl" == win._status_msg.text()


def test_phone_filter_sync_from_phone_no_echo(qapp, win):
    state = {"look": "noir", "beauty": 0.6, "stylize": "pixelate", "stylize_amount": 0.25,
             "geometry": {"mirror": True, "flipV": True, "rotate": 270, "zoom": 2.0,
                          "panX": 0.5, "panY": -0.5}}
    _emit(win, qapp, "filter", {"state": state})
    _pump(qapp, 0.3)
    assert "push_filter" not in _names(win)
    assert win._pf_look.currentText() == "noir"
    assert win._pf_beauty.value() == 60
    assert win._pf_stylize.currentText() == "pixelate"
    assert win._pf_amount.value() == 25
    assert win._pf_mirror.isChecked() and win._pf_flipv.isChecked()
    assert win._pf_rotate.currentText() == "270"
    assert win._pf_zoom.value() == pytest.approx(2.0)
    assert win._pf_panx.value() == 50 and win._pf_pany.value() == -50
    assert win._status_msg.text() == "phone pushed filter state"
    # garbage is tolerated
    _emit(win, qapp, "filter", {"state": {"beauty": "nope"}})
    _emit(win, qapp, "filter_ok", {})


# ---------------------------------------------------------------------------
# local adjust (PC only)
# ---------------------------------------------------------------------------
def test_local_adjust_and_reset(qapp, win):
    win._la_brightness.setValue(20)
    win._la_contrast.setValue(-10)
    win._la_saturation.setValue(150)
    win._la_mirror.setChecked(True)
    win._la_flipv.setChecked(True)
    win._la_rotate.setCurrentIndex(1)
    last = [c for c in win.calls if c[0] == "set_local_adjust"][-1][2]
    assert last == {"brightness": 20, "contrast": -10, "saturation": 150,
                    "mirror": True, "flipV": True, "rotate": 90}
    n = _names(win).count("set_local_adjust")
    win._on_local_reset()
    assert _names(win).count("set_local_adjust") == n + 1  # exactly one push
    last = [c for c in win.calls if c[0] == "set_local_adjust"][-1][2]
    assert last == {"brightness": 0, "contrast": 0, "saturation": 100,
                    "mirror": False, "flipV": False, "rotate": 0}


# ---------------------------------------------------------------------------
# virtual camera
# ---------------------------------------------------------------------------
def test_virtual_cam_start_stop_and_error(qapp, win):
    from omnicam.virtualcam_out import VirtualCamError

    win._on_vcam_start()
    assert "start_virtual_cam" in _names(win)
    assert not win._btn_vcam_start.isEnabled() and win._btn_vcam_stop.isEnabled()
    assert win._vcam_backend.text() == "obs"
    assert "obs" in win._vcam_status.text()
    win._on_vcam_stop()
    assert "stop_virtual_cam" in _names(win)
    assert win._btn_vcam_start.isEnabled() and not win._btn_vcam_stop.isEnabled()
    assert win._vcam_backend.text() == "-"

    def boom():
        raise VirtualCamError("no OBS")
    win._app.start_virtual_cam = boom
    win._on_vcam_start()
    assert any("no OBS" in str(a) for a in win.warnings)
    assert win._btn_vcam_start.isEnabled()


# ---------------------------------------------------------------------------
# errors
# ---------------------------------------------------------------------------
def test_error_events(qapp, win):
    _emit(win, qapp, "error", {"code": "busy", "message": "busy"})
    assert "phone busy" in win._status_msg.text()
    n = len(win.warnings)
    _emit(win, qapp, "error", {"code": "nosuch", "message": "unknown t"})
    assert len(win.warnings) == n + 1
    assert "nosuch" in str(win.warnings[-1])
    _emit(win, qapp, "error", {"code": "badmsg", "message": "bad"})
    assert len(win.warnings) == n + 2
    _emit(win, qapp, "error", {"code": "sendfailed", "message": "down"})
    assert len(win.warnings) == n + 2  # other codes: status only
    assert win._status_msg.text() == "error: sendfailed down"


# ---------------------------------------------------------------------------
# preview + stats
# ---------------------------------------------------------------------------
def test_preview_renders_frame_and_stats_refresh(qapp, win):
    np = pytest.importorskip("numpy")
    frame = np.zeros((90, 160, 3), np.uint8)
    frame[..., 2] = 200
    win._app._streaming = True
    with win._app._preview_lock:
        win._app._preview = frame
        win._app._preview_seq += 1
    _pump(qapp, 0.15)
    assert win._video_label.has_frame()
    assert win._app.stats.snapshot()["fps_displayed"] >= 0.0
    # stopping clears the preview
    win._app._streaming = False
    with win._app._preview_lock:
        win._app._preview = None
    _emit(win, qapp, "stopped", {"local": True})
    assert not win._video_label.has_frame()

    real = win._app.get_stats_snapshot

    def fake_snapshot():
        snap = real()
        snap.update({"rtt_ms": 12.3, "drops": 3, "fps_decoded": 29.7, "bitrate_kbps": 2950.4,
                     "loss_pct": 0.25, "g2g_ms": 88.6, "fec_active": True,
                     "phone": {"fps": 30, "kbps": 2900, "enc_ms": 5.5, "loss_pct": 0.1,
                               "nacks": 2, "sent": 999}})
        return snap
    win._app.get_stats_snapshot = fake_snapshot
    _pump(qapp, 0.6)  # stats timer 500 ms
    assert win._status_rtt.text() == "RTT: 12.3 ms"
    assert win._stat_labels["rtt_ms"].text() == "12.3"
    assert win._stat_labels["drops"].text() == "3"
    assert win._stat_labels["phone_sent"].text() == "999"
    assert win._stat_labels["phone_enc_ms"].text() == "5.5"
    assert win._stat_labels["fec_active"].text() == "True"
    assert win._stats_strip.label("fps_decoded").text() == "29.7"
    assert win._stats_strip.label("bitrate_kbps").text() == "2950"
    assert win._stats_strip.label("loss_pct").text() == "0.25 %"
    assert win._stats_strip.label("rtt_ms").text() == "12.3 ms"
    assert win._stats_strip.label("g2g_ms").text() == "89 ms"
    assert win._stats_strip.label("drops").text() == "3"
    win._btn_stats_details.setChecked(True)
    assert win._stats_details.isVisible()
    win._btn_stats_details.setChecked(False)
    assert not win._stats_details.isVisible()
