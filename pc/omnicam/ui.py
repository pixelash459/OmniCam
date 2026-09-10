"""PySide6 user interface for OmniCam PC.

Layout: a scrollable LEFT sidebar of cards (Connection, Saved phones, Stream,
Camera, Phone Filters, Local Adjust, Virtual Camera); the video preview in the
CENTRE (30 fps QTimer, latest frame only) with a slim stats strip beneath it
and an expandable detail grid; a status line at the bottom.

Phone Filters mirrors PROTOCOL.md section 5: any change is pushed to the
phone automatically via ``{"t":"filter","state":...}`` and incoming phone-side
changes update the controls (bidirectional sync).  Local Adjust is PC-only.

Saved phones / preferences use ``omnicam.settings`` when available; the import
is defensive so the window works without that module (feature disabled).
"""

from __future__ import annotations

import logging
import signal
import sys
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from omnicam import __version__
from omnicam import ui_theme as T
from omnicam.app import OmniCamApp
from omnicam.tray import TrayIcon, load_app_icon
from omnicam.ui_theme import app_font, make_dark_palette  # noqa: F401 (re-export)
from omnicam.ui_widgets import FormGrid, PreviewWidget, Section, SliderRow, StatsStrip
from omnicam.virtualcam_out import VirtualCamError

try:  # optional: saved phones + preferences (another module owns this file)
    from omnicam import settings as _settings
except ImportError:  # pragma: no cover - depends on checkout state
    _settings = None  # type: ignore[assignment]

log = logging.getLogger("omnicam.ui")

# Exact value sets from PROTOCOL.md section 5
LOOKS = ["none", "mono", "noir", "chrome", "fade", "instant",
         "process", "transfer", "sepia", "invert", "false_color"]
STYLIZES = ["none", "pixelate", "crystallize", "hexagonal", "twirl",
            "bulge", "bump", "soft_blur", "zoom_blur"]

RESOLUTIONS: List[Tuple[int, int]] = [(1280, 720), (1920, 1080)]
FPSES = [30, 24, 15]
BITRATES_KBPS = [500, 1000, 2000, 3000, 6000]

SIDEBAR_WIDTH = 312

# preference keys (omnicam.settings)
PREF_AUTOCONNECT = "autoconnect"
PREF_RES = "stream.res"
PREF_FPS = "stream.fps"
PREF_KBPS = "stream.kbps"
PREF_GEOMETRY = "window.geometry"
PREF_FILTERS_OPEN = "ui.filters_open"
PREF_LOCAL_OPEN = "ui.local_open"
PREF_STATS_DETAILS = "ui.stats_details"
PREF_CLOSE_TO_TRAY = "ui.close_to_tray"


class MainWindow(QMainWindow):
    """OmniCam PC main window.

    Closing with the title-bar X hides the window into the system tray (pref
    ``ui.close_to_tray``, default on) so streaming and the virtual camera keep
    running; "Quit OmniCam" (tray menu or the status-bar Quit button) performs
    the full shutdown.  ``shutdown_finished`` fires once after that shutdown
    so ``main()`` can quit the application (``quitOnLastWindowClosed`` is off).
    """

    shutdown_finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"OmniCam PC {__version__}")
        self.setWindowIcon(load_app_icon())
        self.resize(1280, 800)
        self.setMinimumSize(1100, 700)

        self._quitting = False
        self._shutdown_done = False
        self._tray_hint_shown = False
        self._tray: Optional[TrayIcon] = None

        self._app = OmniCamApp()
        self._app.start()

        self._preview_seq = 0
        self._syncing_filters = False  # guard against push loops while syncing
        self._syncing_session = False
        self._filter_push_timer = QTimer(self)
        self._filter_push_timer.setSingleShot(True)
        self._filter_push_timer.setInterval(150)
        self._filter_push_timer.timeout.connect(self._flush_phone_filters)
        self._session_push_timer = QTimer(self)
        self._session_push_timer.setSingleShot(True)
        self._session_push_timer.setInterval(250)
        self._session_push_timer.timeout.connect(self._flush_session_controls)
        self._device_ips: Dict[int, str] = {}
        self._connect_ip: Optional[str] = None  # last IP we asked to connect to

        self._build_ui()
        self._build_tray()
        self._restore_prefs()
        self._build_timers()
        self._show_import_warnings()
        self._maybe_autoconnect()

    # ------------------------------------------------------------------
    # preferences (defensive: everything is optional)
    # ------------------------------------------------------------------
    @staticmethod
    def prefs_available() -> bool:
        """True when ``omnicam.settings`` imported."""
        return _settings is not None

    def _pref_get(self, key: str, default: Any = None) -> Any:
        if _settings is None:
            return default
        try:
            return _settings.get_pref(key, default)
        except Exception:
            log.exception("get_pref(%s) failed", key)
            return default

    def _pref_set(self, key: str, value: Any) -> None:
        if _settings is None:
            return
        try:
            _settings.set_pref(key, value)
        except Exception:
            log.exception("set_pref(%s) failed", key)

    def _known_phones(self) -> List[Dict[str, Any]]:
        try:
            if hasattr(self._app, "known_phones"):
                phones = self._app.known_phones()
            elif _settings is not None:
                phones = _settings.known_phones()
            else:
                return []
        except Exception:
            log.exception("known_phones failed")
            return []
        out: List[Dict[str, Any]] = []
        for p in phones or []:
            if isinstance(p, dict) and p.get("ip"):
                out.append(p)
        out.sort(key=lambda p: str(p.get("last_seen") or ""), reverse=True)
        return out

    def _remember_phone(self, ip: str, name: str = "", device: str = "") -> None:
        if _settings is None:
            return
        try:
            _settings.remember_phone(ip, name=name, device=device)
        except Exception:
            log.exception("remember_phone failed")
        self._refresh_saved_phones()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("root")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_sidebar(), 0)
        root.addWidget(self._build_center(), 1)
        self.setCentralWidget(central)

        self._status_conn = QLabel("disconnected")
        self._status_conn.setObjectName("connPill")
        self._status_conn.setProperty("connected", False)
        self._status_rtt = QLabel("RTT: -")
        self._status_rtt.setObjectName("muted")
        self._status_msg = QLabel("")
        self._status_ver = QLabel(f"OmniCam PC v{__version__} - protocol v1")
        self._status_ver.setObjectName("muted")
        bar = self.statusBar()
        bar.setSizeGripEnabled(True)
        bar.setContentsMargins(T.SPACE, 2, T.SPACE, 2)
        bar.addWidget(self._status_conn)
        bar.addWidget(self._status_rtt)
        bar.addWidget(self._status_msg, 1)
        bar.addPermanentWidget(self._status_ver)
        bar.addPermanentWidget(self._build_quit_button())

    def _build_quit_button(self) -> QToolButton:
        """Subtle text-only Quit in the status bar (X only hides to the tray)."""
        btn = QToolButton(self)
        btn.setObjectName("quitButton")
        btn.setText("Quit")
        btn.setAutoRaise(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip("Quit OmniCam completely (stops streaming and the virtual camera)")
        btn.setStyleSheet(
            f"QToolButton#quitButton {{ color: {T.TEXT_MUTED}; border: 1px solid transparent;"
            f" border-radius: {T.RADIUS}px; padding: 1px 8px; background: transparent; }}"
            f"QToolButton#quitButton:hover {{ color: {T.TEXT}; background-color: {T.HOVER};"
            f" border-color: {T.INPUT_BORDER}; }}"
            f"QToolButton#quitButton:pressed {{ background-color: {T.PRESSED}; }}")
        btn.clicked.connect(self.quit_app)
        self._btn_quit = btn
        return btn

    # -- system tray -------------------------------------------------------
    def _build_tray(self) -> None:
        """Create the tray icon; shown only when the platform offers a tray."""
        close_to_tray = bool(self._pref_get(PREF_CLOSE_TO_TRAY, True))
        tray = TrayIcon(self, close_to_tray=close_to_tray)
        tray.show_requested.connect(self.show_from_tray)
        tray.toggle_requested.connect(self.toggle_from_tray)
        tray.start_stream.connect(self._on_start_stream)
        tray.stop_stream.connect(self._on_stop_stream)
        tray.start_vcam.connect(self._on_vcam_start)
        tray.stop_vcam.connect(self._on_vcam_stop)
        tray.close_to_tray_changed.connect(self._on_close_to_tray_toggled)
        tray.quit_requested.connect(self.quit_app)
        tray.menu.aboutToShow.connect(self._update_tray_state)
        self._tray = tray
        self._update_tray_state()
        if TrayIcon.available():
            tray.show()

    def tray(self) -> Optional[TrayIcon]:
        """The tray controller (always created; visible only with a real tray)."""
        return self._tray

    def close_to_tray_enabled(self) -> bool:
        return bool(self._pref_get(PREF_CLOSE_TO_TRAY, True))

    def _on_close_to_tray_toggled(self, on: bool) -> None:
        self._pref_set(PREF_CLOSE_TO_TRAY, bool(on))

    def _tray_status_text(self) -> str:
        conn = self._status_conn.text()
        if self._app.streaming and "streaming" not in conn:
            conn += " · streaming"
        if self._app.vcam.running:
            conn += " · virtual camera on"
        msg = self._status_msg.text().strip()
        return f"OmniCam PC — {conn}" + (f"\n{msg}" if msg else "")

    def _update_tray_state(self) -> None:
        if self._tray is None:
            return
        try:
            self._tray.set_state(
                self._tray_status_text(),
                streaming=bool(self._app.streaming),
                can_start_stream=self._btn_start.isEnabled(),
                can_stop_stream=self._btn_stop.isEnabled(),
                can_start_vcam=self._btn_vcam_start.isEnabled(),
                can_stop_vcam=self._btn_vcam_stop.isEnabled(),
            )
        except RuntimeError:  # tray already destroyed during shutdown
            pass

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def toggle_from_tray(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_from_tray()

    def _build_sidebar(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setObjectName("sidebar")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFixedWidth(SIDEBAR_WIDTH)

        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(T.SPACE + 4, T.SPACE + 4, T.SPACE + 4, T.SPACE + 4)
        lay.setSpacing(T.SPACE + 2)

        lay.addWidget(self._section_connection())
        lay.addWidget(self._section_saved_phones())
        lay.addWidget(self._section_stream())
        lay.addWidget(self._section_camera())
        lay.addWidget(self._section_phone_filters())
        lay.addWidget(self._section_local_adjust())
        lay.addWidget(self._section_virtual_cam())
        lay.addStretch(1)
        scroll.setWidget(panel)
        return scroll

    # -- section: connection ---------------------------------------------
    def _section_connection(self) -> QWidget:
        sec = Section("Connection")
        hint = QLabel("Phones found via UDP 9920 beacons")
        hint.setObjectName("muted")
        sec.add(hint)
        self._device_list = QListWidget()
        self._device_list.setMinimumHeight(96)
        self._device_list.setMaximumHeight(132)
        self._device_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._device_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        sec.add(self._device_list)

        manual_row = QHBoxLayout()
        manual_row.setSpacing(T.SPACE)
        self._manual_ip = QLineEdit()
        self._manual_ip.setPlaceholderText("Manual IP, e.g. 192.168.1.20")
        self._manual_ip.setToolTip("Access points may block broadcast beacons; type the phone IP.")
        self._manual_ip.returnPressed.connect(self._on_add_manual_ip)
        manual_row.addWidget(self._manual_ip, 1)
        self._btn_add_ip = self._mk_button("Add", self._on_add_manual_ip, manual_row)
        sec.add_layout(manual_row)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(T.SPACE)
        self._btn_connect = self._mk_button("Connect", self._on_connect, btn_row, accent=True)
        self._btn_disconnect = self._mk_button("Disconnect", self._on_disconnect, btn_row)
        self._btn_disconnect.setEnabled(False)
        sec.add_layout(btn_row)

        self._autoconnect_check = QCheckBox("Autoconnect to last phone")
        self._autoconnect_check.toggled.connect(self._on_autoconnect_toggled)
        self._autoconnect_check.setEnabled(self.prefs_available())
        if not self.prefs_available():
            self._autoconnect_check.setToolTip("Requires omnicam.settings")
        sec.add(self._autoconnect_check)
        return sec

    # -- section: saved phones -------------------------------------------
    def _section_saved_phones(self) -> QWidget:
        sec = Section("Saved phones")
        self._saved_section = sec
        self._saved_combo = QComboBox()
        self._saved_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._saved_combo.setMinimumContentsLength(12)
        sec.add(self._saved_combo)
        row = QHBoxLayout()
        row.setSpacing(T.SPACE)
        self._btn_saved_connect = self._mk_button("Connect", self._on_saved_connect, row)
        self._btn_saved_forget = self._mk_button("Forget", self._on_saved_forget, row)
        sec.add_layout(row)
        self._saved_hint = QLabel("")
        self._saved_hint.setObjectName("muted")
        self._saved_hint.setWordWrap(True)
        sec.add(self._saved_hint)
        self._refresh_saved_phones()
        return sec

    # -- section: stream -------------------------------------------------
    def _section_stream(self) -> QWidget:
        sec = Section("Stream")
        grid = FormGrid()
        self._res_combo = self._mk_combo([f"{w} x {h}" for w, h in RESOLUTIONS], 0)
        self._fps_combo = self._mk_combo([str(f) for f in FPSES], 0)
        self._kbps_combo = self._mk_combo([f"{b} kbps" for b in BITRATES_KBPS], 3)
        grid.add_row("Resolution", self._res_combo)
        grid.add_row("Frame rate", self._fps_combo)
        grid.add_row("Bitrate", self._kbps_combo)
        self._res_combo.currentIndexChanged.connect(self._on_session_controls_changed)
        self._fps_combo.currentIndexChanged.connect(self._on_session_controls_changed)
        self._kbps_combo.currentIndexChanged.connect(self._on_session_controls_changed)
        self._abr_check = QCheckBox("Auto bitrate (phone-side ABR)")
        self._abr_check.setChecked(True)
        self._abr_check.toggled.connect(self._on_abr_toggled)
        grid.add_span(self._abr_check)
        sec.add_layout(grid)
        srow = QHBoxLayout()
        srow.setSpacing(T.SPACE)
        self._btn_start = self._mk_button("Start Stream", self._on_start_stream, srow, accent=True)
        self._btn_stop = self._mk_button("Stop Stream", self._on_stop_stream, srow)
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(False)
        sec.add_layout(srow)
        return sec

    # -- section: camera -------------------------------------------------
    def _section_camera(self) -> QWidget:
        sec = Section("Camera")
        crow = QHBoxLayout()
        crow.setSpacing(T.SPACE)
        self._btn_front = self._mk_button("Front", lambda: self._on_camera("front"), crow)
        self._btn_back = self._mk_button("Back", lambda: self._on_camera("back"), crow)
        self._btn_front.setEnabled(False)
        self._btn_back.setEnabled(False)
        sec.add_layout(crow)
        grid = FormGrid()
        self._torch_check = QCheckBox("Torch (back camera only)")
        self._torch_check.setEnabled(False)
        self._torch_check.toggled.connect(self._on_torch)
        grid.add_span(self._torch_check)
        self._zoom_spin = QDoubleSpinBox()
        self._zoom_spin.setRange(1.0, 8.0)
        self._zoom_spin.setSingleStep(0.1)
        self._zoom_spin.setDecimals(2)
        self._zoom_spin.setValue(1.0)
        self._zoom_spin.setSuffix(" x")
        self._zoom_spin.valueChanged.connect(self._on_zoom)
        grid.add_row("Zoom", self._zoom_spin)
        self._cam_label = QLabel("camera: -")
        self._cam_label.setObjectName("muted")
        grid.add_span(self._cam_label)
        sec.add_layout(grid)
        return sec

    # -- section: phone filters (PROTOCOL.md S5) --------------------------
    def _section_phone_filters(self) -> QWidget:
        sec = Section("Phone Filters", collapsible=True, expanded=False)
        self._filters_section = sec
        sec.toggled.connect(lambda on: self._pref_set(PREF_FILTERS_OPEN, bool(on)))
        note = QLabel("Mirrors the phone filter state; changes push to the phone "
                      "automatically and phone-side edits update these controls.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        sec.add(note)

        grid = FormGrid()
        self._pf_look = QComboBox()
        self._pf_look.addItems(LOOKS)
        grid.add_row("Look", self._pf_look)
        self._pf_beauty = self._mk_slider(grid, "Beauty", 0, 100, 0, None)
        self._pf_stylize = QComboBox()
        self._pf_stylize.addItems(STYLIZES)
        grid.add_row("Stylize", self._pf_stylize)
        self._pf_amount = self._mk_slider(grid, "Amount", 0, 100, 50, None)
        sec.add_layout(grid)

        geo_title = QLabel("Geometry")
        geo_title.setObjectName("fieldLabel")
        sec.add(geo_title)
        ggrid = FormGrid()
        grow = QHBoxLayout()
        grow.setSpacing(T.SPACE * 2)
        self._pf_mirror = QCheckBox("Mirror")
        self._pf_flipv = QCheckBox("Flip vertical")
        grow.addWidget(self._pf_mirror)
        grow.addWidget(self._pf_flipv)
        grow.addStretch(1)
        gwrap = QWidget()
        gwrap.setLayout(grow)
        ggrid.add_span(gwrap)
        self._pf_rotate = QComboBox()
        self._pf_rotate.addItems(["0", "90", "180", "270"])
        ggrid.add_row("Rotate", self._pf_rotate)
        self._pf_zoom = QDoubleSpinBox()
        self._pf_zoom.setRange(1.0, 8.0)
        self._pf_zoom.setSingleStep(0.1)
        self._pf_zoom.setDecimals(2)
        self._pf_zoom.setSuffix(" x")
        ggrid.add_row("Zoom", self._pf_zoom)
        self._pf_panx = self._mk_slider(ggrid, "Pan X", -100, 100, 0, None)
        self._pf_pany = self._mk_slider(ggrid, "Pan Y", -100, 100, 0, None)
        sec.add_layout(ggrid)

        # push-on-change wiring (all filter controls)
        for sig in (
            self._pf_look.currentIndexChanged,
            self._pf_beauty.valueChanged,
            self._pf_stylize.currentIndexChanged,
            self._pf_amount.valueChanged,
            self._pf_mirror.toggled,
            self._pf_flipv.toggled,
            self._pf_rotate.currentIndexChanged,
            self._pf_zoom.valueChanged,
            self._pf_panx.valueChanged,
            self._pf_pany.valueChanged,
        ):
            sig.connect(self._push_phone_filters)
        return sec

    # -- section: local adjust (PC only) ----------------------------------
    def _section_local_adjust(self) -> QWidget:
        sec = Section("Local Adjust", collapsible=True, expanded=False)
        self._local_section = sec
        sec.toggled.connect(lambda on: self._pref_set(PREF_LOCAL_OPEN, bool(on)))
        note = QLabel("PC-only, applied after decode to the preview and virtual camera.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        sec.add(note)
        grid = FormGrid()
        self._la_brightness = self._mk_slider(grid, "Brightness", -100, 100, 0, self._on_local_changed)
        self._la_contrast = self._mk_slider(grid, "Contrast", -100, 100, 0, self._on_local_changed)
        self._la_saturation = self._mk_slider(grid, "Saturation", 0, 200, 100, self._on_local_changed)
        la_row = QHBoxLayout()
        la_row.setSpacing(T.SPACE * 2)
        self._la_mirror = QCheckBox("Mirror")
        self._la_flipv = QCheckBox("Flip vertical")
        for cb in (self._la_mirror, self._la_flipv):
            cb.toggled.connect(self._on_local_changed)
            la_row.addWidget(cb)
        la_row.addStretch(1)
        la_wrap = QWidget()
        la_wrap.setLayout(la_row)
        grid.add_span(la_wrap)
        self._la_rotate = QComboBox()
        self._la_rotate.addItems(["0", "90", "180", "270"])
        self._la_rotate.currentIndexChanged.connect(self._on_local_changed)
        grid.add_row("Rotate", self._la_rotate)
        sec.add_layout(grid)
        self._btn_local_reset = self._mk_button("Reset local adjustments", self._on_local_reset,
                                                sec.layout_body())
        return sec

    # -- section: virtual camera -----------------------------------------
    def _section_virtual_cam(self) -> QWidget:
        sec = Section("Virtual Camera")
        src = QLabel("Source: this stream (decoded, locally adjusted)")
        src.setObjectName("muted")
        src.setWordWrap(True)
        sec.add(src)
        btns = QHBoxLayout()
        btns.setSpacing(T.SPACE)
        self._btn_vcam_start = self._mk_button("Start Virtual Camera", self._on_vcam_start, btns,
                                               accent=True)
        self._btn_vcam_stop = self._mk_button("Stop", self._on_vcam_stop, btns)
        self._btn_vcam_stop.setEnabled(False)
        sec.add_layout(btns)
        grid = FormGrid()
        self._vcam_backend = QLabel("-")
        self._vcam_res = QLabel("-")
        self._vcam_fps = QLabel("-")
        self._vcam_sent = QLabel("0")
        grid.add_row("Backend", self._vcam_backend)
        grid.add_row("Resolution", self._vcam_res)
        grid.add_row("FPS", self._vcam_fps)
        grid.add_row("Frames sent", self._vcam_sent)
        sec.add_layout(grid)
        self._vcam_status = QLabel("OBS Virtual Camera requires OBS Studio; "
                                   "Unity Capture is the optional fallback.")
        self._vcam_status.setObjectName("muted")
        self._vcam_status.setWordWrap(True)
        sec.add(self._vcam_status)
        return sec

    # -- centre: preview + stats -----------------------------------------
    def _build_center(self) -> QWidget:
        holder = QWidget(self)
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(T.SPACE + 4, T.SPACE + 4, T.SPACE + 4, T.SPACE + 4)
        lay.setSpacing(T.SPACE + 2)

        self._video_label = PreviewWidget()
        lay.addWidget(self._video_label, 1)

        self._stats_strip = StatsStrip((
            ("fps_decoded", "FPS"),
            ("bitrate_kbps", "kbps"),
            ("loss_pct", "Loss"),
            ("rtt_ms", "RTT"),
            ("g2g_ms", "Latency"),
            ("drops", "Dropped"),
        ))
        self._btn_stats_details = QPushButton("Details")
        self._btn_stats_details.setCheckable(True)
        self._btn_stats_details.setProperty("toggle", True)
        self._btn_stats_details.toggled.connect(self._on_stats_details_toggled)
        self._stats_strip.trailing.addWidget(self._btn_stats_details)
        lay.addWidget(self._stats_strip, 0)

        self._stats_details = self._build_stats_details()
        self._stats_details.setVisible(False)
        lay.addWidget(self._stats_details, 0)
        return holder

    def _build_stats_details(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("statsStrip")
        grid = QGridLayout(frame)
        grid.setContentsMargins(T.SPACE + 4, T.SPACE, T.SPACE + 4, T.SPACE)
        grid.setHorizontalSpacing(T.SPACE * 2)
        grid.setVerticalSpacing(4)
        self._stat_labels: Dict[str, QLabel] = {}

        pc_items = (
            ("fps_decoded", "Decoded FPS"),
            ("fps_displayed", "Displayed FPS"),
            ("bitrate_kbps", "Video bitrate (measured)"),
            ("loss_pct", "Packet loss %"),
            ("rtt_ms", "RTT (ping/pong) ms"),
            ("g2g_ms", "Glass-to-glass (est.) ms"),
            ("nacks_sent", "NACKs sent"),
            ("plis_sent", "PLIs sent"),
            ("drops", "Dropped frames"),
            ("late_frames", "Late frames"),
            ("dup_packets", "Duplicate packets"),
            ("jitter_ms", "Jitter (est.) ms"),
            ("fec_recovered", "FEC recovered pkts"),
            ("fec_active", "FEC active"),
        )
        phone_items = (
            ("phone_fps", "FPS"),
            ("phone_kbps", "Bitrate kbps"),
            ("phone_enc_ms", "Encode ms"),
            ("phone_loss_pct", "Loss %"),
            ("phone_nacks", "NACKs received"),
            ("phone_sent", "Packets sent"),
        )

        def header(text: str, row: int, col: int) -> None:
            h = QLabel(text)
            h.setObjectName("statKey")
            grid.addWidget(h, row, col, 1, 2)

        def add(key: str, title: str, row: int, col: int) -> None:
            k = QLabel(title)
            k.setObjectName("fieldLabel")
            v = QLabel("-")
            v.setObjectName("value")
            v.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(k, row, col)
            grid.addWidget(v, row, col + 1)
            self._stat_labels[key] = v

        header("Receiver (PC)", 0, 0)
        header("", 0, 2)
        header("Phone-reported (1 s)", 0, 4)
        half = (len(pc_items) + 1) // 2
        for i, (key, title) in enumerate(pc_items):
            col = 0 if i < half else 2
            add(key, title, 1 + (i % half), col)
        for i, (key, title) in enumerate(phone_items):
            add(key, title, 1 + i, 4)
        for c in (1, 3, 5):
            grid.setColumnMinimumWidth(c, 56)
        grid.setColumnStretch(6, 1)
        return frame

    # -- small widget helpers ------------------------------------------------
    @staticmethod
    def _mk_button(text: str, handler: Any, parent: Any, accent: bool = False) -> QPushButton:
        btn = QPushButton(text)
        if accent:
            btn.setProperty("accent", True)
        btn.clicked.connect(handler)
        parent.addWidget(btn)
        return btn

    @staticmethod
    def _mk_combo(items: List[str], default: int) -> QComboBox:
        combo = QComboBox()
        combo.addItems(items)
        combo.setCurrentIndex(default)
        return combo

    @staticmethod
    def _mk_slider(parent: Any, title: str, lo: int, hi: int, default: int,
                   handler: Any) -> QSlider:
        row = SliderRow(title, lo, hi, default)
        if handler is not None:
            row.slider.valueChanged.connect(handler)
        if isinstance(parent, FormGrid):
            parent.add_span(row)
        else:
            parent.addWidget(row)
        return row.slider

    # ------------------------------------------------------------------
    # preferences: restore / persist
    # ------------------------------------------------------------------
    def _restore_prefs(self) -> None:
        """Apply saved combos, section state and window geometry (all optional)."""
        was = self._syncing_session
        self._syncing_session = True
        try:
            res = self._pref_get(PREF_RES)
            if isinstance(res, str) and "x" in res:
                try:
                    w, h = (int(v) for v in res.lower().split("x"))
                    if (w, h) in RESOLUTIONS:
                        self._res_combo.setCurrentIndex(RESOLUTIONS.index((w, h)))
                except ValueError:
                    pass
            fps = self._pref_get(PREF_FPS)
            if isinstance(fps, int) and fps in FPSES:
                self._fps_combo.setCurrentIndex(FPSES.index(fps))
            kbps = self._pref_get(PREF_KBPS)
            if isinstance(kbps, int) and kbps in BITRATES_KBPS:
                self._kbps_combo.setCurrentIndex(BITRATES_KBPS.index(kbps))
        finally:
            self._syncing_session = was
        self._session_push_timer.stop()

        # default True matches OmniCamApp.autoconnect_last(); unavailable -> off
        self._autoconnect_check.blockSignals(True)
        self._autoconnect_check.setChecked(
            self.prefs_available() and bool(self._pref_get(PREF_AUTOCONNECT, True)))
        self._autoconnect_check.blockSignals(False)
        self._filters_section.set_expanded(bool(self._pref_get(PREF_FILTERS_OPEN, False)))
        self._local_section.set_expanded(bool(self._pref_get(PREF_LOCAL_OPEN, False)))
        self._btn_stats_details.setChecked(bool(self._pref_get(PREF_STATS_DETAILS, False)))

        geo = self._pref_get(PREF_GEOMETRY)
        if isinstance(geo, str) and geo:
            try:
                ba = QByteArray.fromHex(geo.encode("ascii"))
                if not ba.isEmpty():
                    self.restoreGeometry(ba)
            except Exception:
                log.exception("bad saved geometry (ignored)")

    def _save_stream_prefs(self) -> None:
        idx = self._res_combo.currentIndex()
        if 0 <= idx < len(RESOLUTIONS):
            w, h = RESOLUTIONS[idx]
            self._pref_set(PREF_RES, f"{w}x{h}")
        if 0 <= self._fps_combo.currentIndex() < len(FPSES):
            self._pref_set(PREF_FPS, FPSES[self._fps_combo.currentIndex()])
        if 0 <= self._kbps_combo.currentIndex() < len(BITRATES_KBPS):
            self._pref_set(PREF_KBPS, BITRATES_KBPS[self._kbps_combo.currentIndex()])

    def _save_geometry_pref(self) -> None:
        try:
            self._pref_set(PREF_GEOMETRY, bytes(self.saveGeometry().toHex()).decode("ascii"))
        except Exception:
            log.exception("saveGeometry failed")

    def _maybe_autoconnect(self) -> None:
        """Kick ``autoconnect_last`` once the event loop runs (if enabled)."""
        if not self._autoconnect_check.isChecked():
            return
        if not hasattr(self._app, "autoconnect_last"):
            return

        def go() -> None:
            try:
                res = self._app.autoconnect_last()
                if res:
                    self._set_status(f"autoconnecting to {res}")
                    self._connect_ip = str(res)
            except Exception:
                log.exception("autoconnect_last failed")

        QTimer.singleShot(0, go)

    # ------------------------------------------------------------------
    # timers
    # ------------------------------------------------------------------
    def _build_timers(self) -> None:
        self._render_timer = QTimer(self)
        self._render_timer.timeout.connect(self._render_frame)
        self._render_timer.start(33)  # ~30 fps preview, latest frame only

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_events)
        self._poll_timer.start(100)

        self._device_timer = QTimer(self)
        self._device_timer.timeout.connect(self._refresh_devices)
        self._device_timer.start(1000)
        self._refresh_devices()

        # the app remembers beacon phones itself; pick those up every 5 s
        self._saved_timer = QTimer(self)
        self._saved_timer.timeout.connect(self._refresh_saved_phones)
        self._saved_timer.start(5000)

        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start(500)

    def _show_import_warnings(self) -> None:
        """Non-fatal message boxes for missing optional dependencies."""
        errors = OmniCamApp.import_errors()
        if not errors:
            return
        text = "Some optional components failed to import:\n\n" + "\n".join(
            f"  - {pkg}: run `{cmd}`" for pkg, cmd in errors)
        if any(pkg.startswith("pyvirtualcam") for pkg, _ in errors):
            text += ("\n\nVirtual camera output additionally requires OBS Studio "
                     "(https://obsproject.com) or Unity Capture "
                     "(https://github.com/schellingb/UnityCapture).")
        QMessageBox.warning(self, "OmniCam PC - missing components", text)

    # ------------------------------------------------------------------
    # connection actions
    # ------------------------------------------------------------------
    def _on_connect(self) -> None:
        ip = self._manual_ip.text().strip() or self._selected_device_ip() or ""
        if not ip:
            typed, ok = QInputDialog.getText(self, "Manual IP", "Phone IP address:")
            if not ok or not typed.strip():
                return
            ip = typed.strip()
        parsed = self._parse_ipv4(ip)
        if parsed is None:
            QMessageBox.warning(self, "Manual IP", f"Not a valid IPv4 address:\n{ip}")
            return
        self._connect_to(parsed)

    def _connect_to(self, ip: str) -> None:
        """Pin, select and connect the control channel to ``ip``."""
        self._connect_ip = ip
        self._app.pin_device(ip)
        self._refresh_devices()
        self._select_device_ip(ip)
        self._app.connect(ip)

    def _on_add_manual_ip(self) -> None:
        ip = self._parse_ipv4(self._manual_ip.text())
        if ip is None:
            QMessageBox.warning(self, "Manual IP", "Type the phone's IPv4 address, then Add.")
            return
        self._app.pin_device(ip)
        self._refresh_devices()
        self._select_device_ip(ip)
        self._set_status(f"added {ip} — click Connect")

    @staticmethod
    def _parse_ipv4(text: str) -> Optional[str]:
        parts = text.strip().split(".")
        if len(parts) != 4:
            return None
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return None
        if any(n < 0 or n > 255 for n in nums):
            return None
        return ".".join(str(n) for n in nums)

    def _selected_device_ip(self) -> Optional[str]:
        item = self._device_list.currentItem()
        if item is None:
            return None
        ip = item.data(Qt.ItemDataRole.UserRole)
        return str(ip) if ip else None

    def _select_device_ip(self, ip: str) -> None:
        for i in range(self._device_list.count()):
            it = self._device_list.item(i)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) == ip:
                self._device_list.setCurrentItem(it)
                return

    def _on_disconnect(self) -> None:
        self._app.disconnect()  # sends bye

    # -- saved phones -----------------------------------------------------
    def _selected_saved_ip(self) -> Optional[str]:
        ip = self._saved_combo.currentData(Qt.ItemDataRole.UserRole)
        return str(ip) if ip else None

    def _refresh_saved_phones(self) -> None:
        phones = self._known_phones()
        current = self._selected_saved_ip()
        self._saved_combo.blockSignals(True)
        self._saved_combo.clear()
        for p in phones:
            ip = str(p.get("ip"))
            name = str(p.get("name") or p.get("device") or "").strip()
            label = f"{name}  {ip}" if name else ip
            self._saved_combo.addItem(label, ip)
        if current:
            idx = self._saved_combo.findData(current)
            if idx >= 0:
                self._saved_combo.setCurrentIndex(idx)
        self._saved_combo.blockSignals(False)
        have = bool(phones)
        self._saved_combo.setEnabled(have)
        self._btn_saved_connect.setEnabled(have)
        self._btn_saved_forget.setEnabled(have)
        if not self.prefs_available():
            self._saved_hint.setText("Saved phones need omnicam.settings (not installed).")
        elif not have:
            self._saved_hint.setText("Phones are remembered after the first successful connect.")
        else:
            self._saved_hint.setText("")
        self._saved_hint.setVisible(bool(self._saved_hint.text()))

    def _on_saved_connect(self) -> None:
        ip = self._selected_saved_ip()
        if not ip:
            return
        parsed = self._parse_ipv4(ip)
        if parsed is None:
            QMessageBox.warning(self, "Saved phone", f"Not a valid IPv4 address:\n{ip}")
            return
        self._connect_to(parsed)

    def _on_saved_forget(self) -> None:
        ip = self._selected_saved_ip()
        if not ip:
            return
        try:
            if hasattr(self._app, "forget_phone"):
                self._app.forget_phone(ip)
            elif _settings is not None:
                _settings.forget_phone(ip)
            else:
                return
        except Exception:
            log.exception("forget_phone failed")
        self._refresh_saved_phones()
        self._set_status(f"forgot {ip}")

    def _on_autoconnect_toggled(self, on: bool) -> None:
        self._pref_set(PREF_AUTOCONNECT, bool(on))

    # ------------------------------------------------------------------
    # stream / camera actions
    # ------------------------------------------------------------------
    def _on_start_stream(self) -> None:
        w, h = RESOLUTIONS[self._res_combo.currentIndex()]
        fps = FPSES[self._fps_combo.currentIndex()]
        kbps = BITRATES_KBPS[self._kbps_combo.currentIndex()]
        self._save_stream_prefs()
        self._app.start_stream(w, h, fps, kbps)

    def _on_stop_stream(self) -> None:
        self._app.stop_stream()
        connected = self.control_connected()
        self._btn_start.setEnabled(connected)
        self._btn_stop.setEnabled(False)
        self._set_status("stream stopped")

    def _on_camera(self, camera_id: str) -> None:
        self._app.set_camera(camera_id)
        self._cam_label.setText(f"camera: {camera_id} (switching...)")
        self._torch_check.setEnabled(camera_id == "back")
        if camera_id == "front":
            self._torch_check.setChecked(False)
        self._apply_camera_caps()

    def _on_torch(self, on: bool) -> None:
        if self._syncing_session:
            return
        self._app.set_torch(on)

    def _on_zoom(self, value: float) -> None:
        if self._syncing_session:
            return
        self._app.set_zoom(float(value))

    def _on_abr_toggled(self, auto: bool) -> None:
        if self._syncing_session:
            return
        if auto:
            self._app.set_abr(True)
        else:
            self._app.set_bitrate(BITRATES_KBPS[self._kbps_combo.currentIndex()])

    def _on_session_controls_changed(self, *_: Any) -> None:
        """Debounce encode size/fps/bitrate so 1080↔720 cannot tear both apps down."""
        if self._syncing_session:
            return
        self._save_stream_prefs()
        self._session_push_timer.start()

    def _flush_session_controls(self) -> None:
        if self._syncing_session:
            return
        idx = self._res_combo.currentIndex()
        if idx < 0 or idx >= len(RESOLUTIONS):
            return
        w, h = RESOLUTIONS[idx]
        fps = FPSES[max(0, self._fps_combo.currentIndex())]
        kbps = BITRATES_KBPS[max(0, self._kbps_combo.currentIndex())]
        self._app.push_session({"w": w, "h": h, "fps": fps, "kbps": kbps})

    # ------------------------------------------------------------------
    # virtual camera
    # ------------------------------------------------------------------
    def _on_vcam_start(self) -> None:
        try:
            backend = self._app.start_virtual_cam()
        except VirtualCamError as exc:
            QMessageBox.warning(self, "Virtual camera", str(exc))
            return
        self._btn_vcam_start.setEnabled(False)
        self._btn_vcam_stop.setEnabled(True)
        self._vcam_backend.setText(backend)
        self._vcam_status.setText(f"running on backend '{backend}'")
        self._update_tray_state()

    def _on_vcam_stop(self) -> None:
        self._app.stop_virtual_cam()
        self._btn_vcam_start.setEnabled(True)
        self._btn_vcam_stop.setEnabled(False)
        self._vcam_backend.setText("-")
        self._vcam_res.setText("-")
        self._vcam_fps.setText("-")
        self._update_tray_state()

    # ------------------------------------------------------------------
    # local adjust
    # ------------------------------------------------------------------
    def _on_local_changed(self, *_: Any) -> None:
        self._app.set_local_adjust(
            brightness=self._la_brightness.value(),
            contrast=self._la_contrast.value(),
            saturation=self._la_saturation.value(),
            mirror=self._la_mirror.isChecked(),
            flipV=self._la_flipv.isChecked(),
            rotate=int(self._la_rotate.currentText()),
        )

    def _on_local_reset(self) -> None:
        for slider, val in ((self._la_brightness, 0), (self._la_contrast, 0),
                            (self._la_saturation, 100)):
            slider.blockSignals(True)
            slider.setValue(val)
            slider.blockSignals(False)
        for cb, val in ((self._la_mirror, False), (self._la_flipv, False)):
            cb.blockSignals(True)
            cb.setChecked(val)
            cb.blockSignals(False)
        self._la_rotate.blockSignals(True)
        self._la_rotate.setCurrentIndex(0)
        self._la_rotate.blockSignals(False)
        self._on_local_changed()

    # ------------------------------------------------------------------
    # phone filters
    # ------------------------------------------------------------------
    def _push_phone_filters(self, *_: Any) -> None:
        """Coalesce slider drags — a flood of filter JSON stalled the phone TCP thread."""
        if self._syncing_filters:
            return
        self._filter_push_timer.start()

    def _flush_phone_filters(self) -> None:
        """Collect all filter controls and push the full state to the phone."""
        if self._syncing_filters:
            return
        updates: Dict[str, Any] = {
            "look": self._pf_look.currentText(),
            "beauty": self._pf_beauty.value() / 100.0,
            "stylize": self._pf_stylize.currentText(),
            "stylize_amount": self._pf_amount.value() / 100.0,
            "geometry": {
                "mirror": self._pf_mirror.isChecked(),
                "flipV": self._pf_flipv.isChecked(),
                "rotate": int(self._pf_rotate.currentText()),
                "zoom": round(float(self._pf_zoom.value()), 2),
                "panX": self._pf_panx.value() / 100.0,
                "panY": self._pf_pany.value() / 100.0,
            },
        }
        self._app.push_filter(updates)
        self._set_status(f"filter pushed: look={updates['look']} stylize={updates['stylize']}")

    def _sync_phone_filters(self, state: Dict[str, Any]) -> None:
        """Update filter controls from a phone-side state (no push)."""
        self._syncing_filters = True
        widgets = (self._pf_look, self._pf_beauty, self._pf_stylize, self._pf_amount,
                   self._pf_mirror, self._pf_flipv, self._pf_rotate, self._pf_zoom,
                   self._pf_panx, self._pf_pany)
        for w in widgets:
            w.blockSignals(True)
        try:
            look = str(state.get("look", "none"))
            idx = self._pf_look.findText(look)
            self._pf_look.setCurrentIndex(idx if idx >= 0 else 0)
            self._pf_beauty.setValue(int(round(float(state.get("beauty", 0.0)) * 100)))
            styl = str(state.get("stylize", "none"))
            idx = self._pf_stylize.findText(styl)
            self._pf_stylize.setCurrentIndex(idx if idx >= 0 else 0)
            self._pf_amount.setValue(int(round(float(state.get("stylize_amount", 0.5)) * 100)))
            geo = state.get("geometry", {}) or {}
            self._pf_mirror.setChecked(bool(geo.get("mirror", False)))
            self._pf_flipv.setChecked(bool(geo.get("flipV", False)))
            rot = str(int(geo.get("rotate", 0) or 0))
            idx = self._pf_rotate.findText(rot)
            self._pf_rotate.setCurrentIndex(idx if idx >= 0 else 0)
            self._pf_zoom.setValue(float(geo.get("zoom", 1.0)))
            self._pf_panx.setValue(int(round(float(geo.get("panX", 0.0)) * 100)))
            self._pf_pany.setValue(int(round(float(geo.get("panY", 0.0)) * 100)))
        except (TypeError, ValueError):
            log.exception("bad filter state from phone (ignored)")
        finally:
            for w in widgets:
                w.blockSignals(False)
            self._syncing_filters = False

    # ------------------------------------------------------------------
    # timers: render / poll / devices / stats
    # ------------------------------------------------------------------
    def _render_frame(self) -> None:
        seq, frame = self._app.get_preview_frame(self._preview_seq)
        if frame is None or frame.size == 0:
            if not self._app.streaming and self._video_label.has_frame():
                self._video_label.set_pixmap(None)
            return
        self._preview_seq = seq
        self._app.stats.tick_displayed()
        h, w = frame.shape[:2]
        img = QImage(frame.data, w, h, w * 3, QImage.Format.Format_BGR888)
        pm = QPixmap.fromImage(img)  # copies pixel data out of the numpy buffer
        self._video_label.set_pixmap(pm)  # scaled with KeepAspectRatio in paintEvent

    def _set_connected_pill(self, connected: bool, text: str) -> None:
        self._status_conn.setText(("connected" if connected else "disconnected") +
                                  (f" ({text})" if text else ""))
        self._status_conn.setProperty("connected", bool(connected))
        st = self._status_conn.style()
        st.unpolish(self._status_conn)
        st.polish(self._status_conn)
        self._update_tray_state()

    def _poll_events(self) -> None:
        events = self._app.drain_events()
        if events:
            self._handle_events(events)
            self._update_tray_state()  # button enabling may have changed

    def _handle_events(self, events: Any) -> None:
        for kind, payload in events:
            if kind == "state":
                connected = bool(payload.get("connected"))
                text = str(payload.get("text", ""))
                self._set_connected_pill(connected, text)
                self._btn_disconnect.setEnabled(connected)
                self._btn_connect.setEnabled(not connected)
                streaming = self._app.streaming
                self._btn_start.setEnabled(connected and not streaming)
                self._btn_stop.setEnabled(streaming)
                self._btn_front.setEnabled(connected)
                self._btn_back.setEnabled(connected)
                self._torch_check.setEnabled(connected and self._app.get_camera() == "back")
                if not connected:
                    self._video_label.set_placeholder("No stream",
                                                      "Connect a phone and press Start Stream")
            elif kind == "devices":
                self._refresh_devices()
            elif kind == "welcome":
                dev = payload.get("device", "?")
                ios = payload.get("ios", "?")
                camera = str(payload.get("camera", "back"))
                sess = payload.get("session")
                if isinstance(sess, dict):
                    self._sync_session_widgets(sess)
                else:
                    self._cam_label.setText(f"camera: {camera}")
                    self._torch_check.setEnabled(camera == "back")
                    self._apply_camera_caps()
                self._set_status(f"welcome from {dev} (iOS {ios})")
                self._btn_start.setEnabled(True)
                self._video_label.set_placeholder("Connected", "Press Start Stream")
                self._remember_connected_phone(str(dev))
            elif kind == "started":
                self._btn_start.setEnabled(False)
                self._btn_stop.setEnabled(True)
                if not self._abr_check.isChecked():
                    # manual bitrate: tell the phone to disable auto ABR
                    self._app.set_bitrate(BITRATES_KBPS[self._kbps_combo.currentIndex()])
                # Show the advertised media IP: the phone may stream to the
                # control-connection peer address instead of this rtp_host.
                host = self._app.rtp_host
                if host:
                    self._set_status(f"streaming (rtp_host={host}; phone prefers control peer IP)")
                else:
                    self._set_status("streaming")
                self._video_label.set_placeholder("Waiting for video", "")
            elif kind == "stopped":
                self._btn_start.setEnabled(self.control_connected())
                self._btn_stop.setEnabled(False)
                if payload.get("local"):
                    self._set_status("stream stopped")
                else:
                    self._set_status("stream stopped by phone")
                self._video_label.set_pixmap(None)
                self._video_label.set_placeholder("Stream stopped", "Press Start Stream")
            elif kind == "session":
                state = payload.get("state")
                if isinstance(state, dict):
                    self._sync_session_widgets(state)
            elif kind == "camera_ok":
                self._cam_label.setText(f"camera: {payload.get('id')}")
                self._set_status(f"camera switched to {payload.get('id')}")
            elif kind == "bitrate_ok":
                self._set_status(f"bitrate: {payload.get('kbps')} kbps "
                                 f"(auto={payload.get('auto')})")
            elif kind == "filter_ok":
                pass  # quiet confirmation
            elif kind == "torch_ok":
                self._set_status(f"torch: {'on' if payload.get('on') else 'off'}")
            elif kind == "filter":
                state = payload.get("state")
                if isinstance(state, dict):
                    self._sync_phone_filters(state)
                    self._set_status("phone pushed filter state")
            elif kind == "error":
                code = str(payload.get("code", ""))
                msg = str(payload.get("message", code))
                self._set_status(f"error: {code} {msg}")
                if code == "busy":
                    self._set_status("phone busy — close OmniCam on the other PC (Beast) first")
                elif code in ("nosuch", "badmsg"):
                    QMessageBox.warning(self, "Phone error",
                                        f"The phone reported an error: {code}\n{msg}")

    def _remember_connected_phone(self, device: str) -> None:
        """Persist the phone we just got ``welcome`` from (saved phones).

        Newer ``OmniCamApp`` builds remember phones themselves; then we only
        refresh the list.  Older builds get the UI-side fallback.
        """
        if hasattr(self._app, "known_phones"):
            self._refresh_saved_phones()
            return
        ip = self._connect_ip
        if not ip:
            target = getattr(self._app.control, "target_ip", None)
            ip = str(target) if target else None
        if not ip:
            return
        name = ""
        for dev in self._app.get_devices():
            if dev.get("ip") == ip and dev.get("name") and dev.get("model") != "manual":
                name = str(dev["name"])
                break
        self._remember_phone(ip, name=name, device=device)

    def control_connected(self) -> bool:
        """Expose control-channel state for button enabling."""
        return "connected" in self._app.connection_text or self._app.streaming

    def _sync_session_widgets(self, state: Dict[str, Any]) -> None:
        """Apply a phone session to combos/checkboxes without echoing push_session."""
        self._syncing_session = True
        widgets = (self._res_combo, self._fps_combo, self._kbps_combo,
                   self._abr_check, self._torch_check, self._zoom_spin)
        for w in widgets:
            w.blockSignals(True)
        try:
            if "w" in state and "h" in state:
                pair = (int(state["w"]), int(state["h"]))
                if pair in RESOLUTIONS:
                    self._res_combo.setCurrentIndex(RESOLUTIONS.index(pair))
            if "fps" in state:
                fps = int(state["fps"])
                if fps in FPSES:
                    self._fps_combo.setCurrentIndex(FPSES.index(fps))
            if "kbps" in state:
                kbps = int(state["kbps"])
                if kbps in BITRATES_KBPS:
                    self._kbps_combo.setCurrentIndex(BITRATES_KBPS.index(kbps))
                else:
                    nearest = min(range(len(BITRATES_KBPS)),
                                  key=lambda i: abs(BITRATES_KBPS[i] - kbps))
                    self._kbps_combo.setCurrentIndex(nearest)
            if "abr" in state:
                self._abr_check.setChecked(bool(state["abr"]))
            if "torch" in state:
                self._torch_check.setChecked(bool(state["torch"]))
            if "zoom" in state:
                self._zoom_spin.setValue(float(state["zoom"]))
            if "camera" in state:
                cam = str(state["camera"])
                self._cam_label.setText(f"camera: {cam}")
                connected = self.control_connected()
                self._torch_check.setEnabled(connected and cam == "back")
            self._apply_camera_caps()
        except (TypeError, ValueError):
            log.exception("bad session state from phone (ignored)")
        finally:
            for w in widgets:
                w.blockSignals(False)
            self._syncing_session = False

    def _apply_camera_caps(self) -> None:
        """Disable resolution/FPS entries the current camera cannot encode
        (from welcome max_front/max_back, PROTOCOL.md section 2.2)."""
        caps = self._app.get_camera_caps()
        cam = self._app.get_camera()
        row = caps.get(cam)
        if not row:
            row = [1280, 720, 30] if cam == "front" else [1920, 1080, 60]
        was = self._syncing_session
        self._syncing_session = True
        try:
            max_w = int(row[0])
            for i, (w, _h) in enumerate(RESOLUTIONS):
                item = self._res_combo.model().item(i)
                if item is not None:
                    item.setEnabled(w <= max_w)
            idx = self._res_combo.currentIndex()
            model_item = self._res_combo.model().item(idx)
            if model_item is not None and not model_item.isEnabled():
                for i in range(self._res_combo.count() - 1, -1, -1):
                    item = self._res_combo.model().item(i)
                    if item is None or item.isEnabled():
                        self._res_combo.setCurrentIndex(i)
                        break
            fps_max = int(row[2]) if len(row) > 2 else 30
            for i, f in enumerate(FPSES):
                item = self._fps_combo.model().item(i)
                if item is not None:
                    item.setEnabled(f <= fps_max)
            if self._fps_combo.currentIndex() >= 0:
                item = self._fps_combo.model().item(self._fps_combo.currentIndex())
                if item is not None and not item.isEnabled():
                    for i, f in enumerate(FPSES):
                        if f <= fps_max:
                            self._fps_combo.setCurrentIndex(i)
                            break
        finally:
            self._syncing_session = was

    def _refresh_devices(self) -> None:
        devices = self._app.get_devices()
        current = self._selected_device_ip()
        labels = []
        for dev in devices:
            ip = dev["ip"]
            if dev.get("manual") and (not dev.get("online") or dev.get("model") == "manual"):
                label = f"{ip}  (manual)"
            else:
                label = f"{dev['name']}  [{dev.get('model', '?')}]  {ip}"
            if dev.get("streaming"):
                label += "  (streaming)"
            labels.append((label, ip))
        # Rebuild only when the set of rows changed — clearing every 1 s made
        # the list (and selection) strobe on flaky laptop Wi-Fi.
        existing = []
        has_placeholder = False
        for i in range(self._device_list.count()):
            it = self._device_list.item(i)
            if it.data(Qt.ItemDataRole.UserRole) is None:
                has_placeholder = True
                continue
            existing.append((it.text(), it.data(Qt.ItemDataRole.UserRole)))
        if existing == labels and (labels or has_placeholder):
            return
        self._device_list.clear()
        if not labels:
            placeholder = QListWidgetItem("Searching for phones...")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self._device_list.addItem(placeholder)
            return
        for label, ip in labels:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, ip)
            item.setToolTip(label)
            self._device_list.addItem(item)
            if ip == current:
                self._device_list.setCurrentItem(item)

    @staticmethod
    def _fmt(val: Any, suffix: str = "", digits: int = 1) -> str:
        if val is None:
            return "-"
        if isinstance(val, bool):
            return "yes" if val else "no"
        if isinstance(val, float):
            return f"{val:.{digits}f}{suffix}"
        return f"{val}{suffix}"

    def _refresh_stats(self) -> None:
        snap = self._app.get_stats_snapshot()
        rtt = snap.get("rtt_ms")
        self._status_rtt.setText(f"RTT: {rtt:.1f} ms" if isinstance(rtt, (int, float)) else "RTT: -")
        keys = ("fps_decoded", "fps_displayed", "bitrate_kbps", "loss_pct", "rtt_ms",
                "g2g_ms", "nacks_sent", "plis_sent", "drops", "late_frames",
                "dup_packets", "jitter_ms", "fec_recovered", "fec_active")
        for key in keys:
            val = snap.get(key)
            self._stat_labels[key].setText("-" if val is None else str(val))
        phone = snap.get("phone", {}) or {}
        for key in ("fps", "kbps", "enc_ms", "loss_pct", "nacks", "sent"):
            val = phone.get(key)
            self._stat_labels[f"phone_{key}"].setText("-" if val is None else str(val))
        # slim strip
        self._stats_strip.set_value("fps_decoded", self._fmt(snap.get("fps_decoded")))
        kbps = snap.get("bitrate_kbps") or snap.get("kbps_measured")  # receiver fallback
        self._stats_strip.set_value("bitrate_kbps", self._fmt(kbps, digits=0))
        self._stats_strip.set_value("loss_pct", self._fmt(snap.get("loss_pct"), " %", 2))
        self._stats_strip.set_value("rtt_ms", self._fmt(snap.get("rtt_ms"), " ms"))
        self._stats_strip.set_value("g2g_ms", self._fmt(snap.get("g2g_ms"), " ms", 0))
        self._stats_strip.set_value("drops", self._fmt(snap.get("drops")))
        if self._app.vcam.running:
            dims = self._app.vcam.dims
            if dims:
                self._vcam_res.setText(f"{dims[0]} x {dims[1]}")
                self._vcam_fps.setText(str(dims[2]))
            self._vcam_sent.setText(str(self._app.vcam.frames_sent))

    def _on_stats_details_toggled(self, on: bool) -> None:
        self._stats_details.setVisible(bool(on))
        self._pref_set(PREF_STATS_DETAILS, bool(on))

    def _set_status(self, text: str) -> None:
        self._status_msg.setText(text)
        self._update_tray_state()

    # ------------------------------------------------------------------
    # close / hide-to-tray / quit
    # ------------------------------------------------------------------
    def _should_close_to_tray(self) -> bool:
        if self._quitting or self._tray is None or not self._tray.is_visible():
            return False
        app = QApplication.instance()
        if app is not None and getattr(app, "isSavingSession", lambda: False)():
            return False  # Windows log-off / shutdown: really close
        return self.close_to_tray_enabled()

    def closeEvent(self, event: Any) -> None:
        """X hides to the tray (when enabled); otherwise full graceful shutdown."""
        if self._should_close_to_tray():
            event.ignore()
            self.hide()
            if not self._tray_hint_shown and self._tray is not None:
                self._tray_hint_shown = True
                self._tray.show_message(
                    "OmniCam is still running",
                    "Streaming and the virtual camera keep working. "
                    "Right-click the tray icon to quit.", 4000)
            return
        self.shutdown()
        super().closeEvent(event)

    def shutdown(self) -> None:
        """Idempotent full teardown: geometry pref, app threads/sockets, tray icon."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._quitting = True
        self._save_geometry_pref()
        try:
            self._app.shutdown()
        except Exception:
            log.exception("shutdown failed")
        if self._tray is not None:
            self._tray.destroy()
            self._tray = None
        self.shutdown_finished.emit()

    def quit_app(self) -> None:
        """Permanent close: full shutdown, then quit the Qt application."""
        self._quitting = True
        self.close()  # closeEvent -> shutdown() (no-op if already done)
        self.shutdown()
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)


def apply_theme(app: QApplication) -> None:
    """Fusion + dark palette + style sheet (idempotent)."""
    app.setStyle("Fusion")
    app.setFont(app_font())
    app.setPalette(make_dark_palette())
    app.setStyleSheet(T.build_stylesheet())


def main() -> int:
    """Application entry point (python -m omnicam)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName("OmniCam PC")
    app.setQuitOnLastWindowClosed(False)  # hiding into the tray must not exit
    apply_theme(app)
    app.setWindowIcon(load_app_icon())
    win = MainWindow()
    win.setWindowIcon(load_app_icon())
    # Full close (Quit, or X with close-to-tray off) ends the process.
    win.shutdown_finished.connect(lambda: QTimer.singleShot(0, app.quit))
    # Session end / app.quit() from anywhere still tears everything down once.
    app.aboutToQuit.connect(win.shutdown)
    # Ctrl+C in a console: the window's timers keep the interpreter ticking so
    # Python delivers the signal; quit_app() then runs the normal shutdown.
    try:
        signal.signal(signal.SIGINT, lambda *_: QTimer.singleShot(0, win.quit_app))
    except (ValueError, OSError):  # pragma: no cover - not the main thread
        pass
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
