"""PySide6 user interface for OmniCam PC.

Layout: LEFT devices + stream controls; CENTER video preview (30 fps QTimer,
latest-frame only); RIGHT tabs (Virtual Cam / Local Adjust / Phone Filters /
Stats); status bar with connection state, RTT and version.

Phone Filters mirrors PROTOCOL.md section 5: any change is pushed to the
phone automatically via ``{"t":"filter","state":...}`` and incoming phone-side
changes update the controls (bidirectional sync; full fine-tuning lives on
the phone UI).
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPalette, QImage, QPixmap, QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from omnicam import __version__
from omnicam.app import OmniCamApp
from omnicam.virtualcam_out import VirtualCamError

log = logging.getLogger("omnicam.ui")

# Exact value sets from PROTOCOL.md section 5
LOOKS = ["none", "mono", "noir", "chrome", "fade", "instant",
         "process", "transfer", "sepia", "invert", "false_color"]
STYLIZES = ["none", "pixelate", "crystallize", "hexagonal", "twirl",
            "bulge", "bump", "soft_blur", "zoom_blur"]

RESOLUTIONS: List[Tuple[int, int]] = [(1280, 720), (1920, 1080)]
FPSES = [30, 24, 15]
BITRATES_KBPS = [500, 1000, 2000, 3000, 6000]


def make_dark_palette() -> QPalette:
    """Build the standard dark Fusion palette."""
    p = QPalette()
    window = QColor(45, 45, 48)
    base = QColor(30, 30, 32)
    text = QColor(220, 220, 220)
    disabled = QColor(120, 120, 120)
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, window)
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(60, 60, 64))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, window)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 80, 80))
    p.setColor(QPalette.ColorRole.Highlight, QColor(0, 120, 215))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)
    return p


class MainWindow(QMainWindow):
    """OmniCam PC main window."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"OmniCam PC {__version__}")
        self.resize(1280, 760)

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

        self._build_ui()
        self._build_timers()
        self._show_import_warnings()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        root = QHBoxLayout(central)
        root.addWidget(self._build_left_panel(), 0)
        root.addWidget(self._build_center(), 1)
        root.addWidget(self._build_right_tabs(), 0)
        self.setCentralWidget(central)

        self._status_conn = QLabel("disconnected")
        self._status_rtt = QLabel("RTT: -")
        self._status_msg = QLabel("")
        self._status_ver = QLabel(f"OmniCam PC v{__version__} - protocol v1")
        bar = self.statusBar()
        bar.addWidget(self._status_conn)
        bar.addWidget(self._status_rtt)
        bar.addWidget(self._status_msg, 1)
        bar.addPermanentWidget(self._status_ver)

    def _build_left_panel(self) -> QWidget:
        panel = QWidget(self)
        lay = QVBoxLayout(panel)

        # -- Devices -----------------------------------------------------
        dev_box = QGroupBox("Devices (UDP 9920 beacons)")
        dev_lay = QVBoxLayout(dev_box)
        self._device_list = self._mk_list(dev_lay)
        manual_row = QHBoxLayout()
        self._manual_ip = QLineEdit()
        self._manual_ip.setPlaceholderText("manual IP (AP may block broadcast)")
        self._manual_ip.returnPressed.connect(self._on_add_manual_ip)
        manual_row.addWidget(self._manual_ip, 1)
        self._mk_button("Add", self._on_add_manual_ip, manual_row)
        dev_lay.addLayout(manual_row)
        btn_row = QHBoxLayout()
        self._btn_connect = self._mk_button("Connect", self._on_connect, btn_row)
        self._btn_disconnect = self._mk_button("Disconnect", self._on_disconnect, btn_row)
        self._btn_disconnect.setEnabled(False)
        dev_lay.addLayout(btn_row)
        lay.addWidget(dev_box)

        # -- Stream ------------------------------------------------------
        stream_box = QGroupBox("Stream")
        form = QFormLayout(stream_box)
        self._res_combo = self._mk_combo(form, "Resolution", [f"{w} x {h}" for w, h in RESOLUTIONS], 0)
        self._fps_combo = self._mk_combo(form, "FPS", [str(f) for f in FPSES], 0)
        self._kbps_combo = self._mk_combo(form, "Bitrate", [f"{b} kbps" for b in BITRATES_KBPS], 3)
        self._res_combo.currentIndexChanged.connect(self._on_session_controls_changed)
        self._fps_combo.currentIndexChanged.connect(self._on_session_controls_changed)
        self._kbps_combo.currentIndexChanged.connect(self._on_session_controls_changed)
        self._abr_check = QCheckBox("Auto ABR (phone-side)")
        self._abr_check.setChecked(True)
        self._abr_check.toggled.connect(self._on_abr_toggled)
        form.addRow("", self._abr_check)
        srow = QHBoxLayout()
        self._btn_start = self._mk_button("Start Stream", self._on_start_stream, srow)
        self._btn_stop = self._mk_button("Stop Stream", self._on_stop_stream, srow)
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(False)
        form.addRow("", srow)
        lay.addWidget(stream_box)

        # -- Camera ------------------------------------------------------
        cam_box = QGroupBox("Camera")
        cam_lay = QVBoxLayout(cam_box)
        crow = QHBoxLayout()
        self._btn_front = self._mk_button("Front", lambda: self._on_camera("front"), crow)
        self._btn_back = self._mk_button("Back", lambda: self._on_camera("back"), crow)
        self._btn_front.setEnabled(False)
        self._btn_back.setEnabled(False)
        cam_lay.addLayout(crow)
        self._torch_check = QCheckBox("Torch (back camera only)")
        self._torch_check.setEnabled(False)
        self._torch_check.toggled.connect(self._on_torch)
        cam_lay.addWidget(self._torch_check)
        zoom_row = QHBoxLayout()
        zoom_row.addWidget(QLabel("Zoom:"))
        self._zoom_spin = QDoubleSpinBox()
        self._zoom_spin.setRange(1.0, 8.0)
        self._zoom_spin.setSingleStep(0.1)
        self._zoom_spin.setDecimals(2)
        self._zoom_spin.setValue(1.0)
        self._zoom_spin.valueChanged.connect(self._on_zoom)
        zoom_row.addWidget(self._zoom_spin, 1)
        cam_lay.addLayout(zoom_row)
        self._cam_label = QLabel("camera: -")
        cam_lay.addWidget(self._cam_label)
        lay.addWidget(cam_box)

        lay.addStretch(1)
        return panel

    def _build_center(self) -> QWidget:
        holder = QWidget(self)
        holder.setStyleSheet("background-color: #101012;")
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(4, 4, 4, 4)
        self._video_label = QLabel("no stream")
        self._video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video_label.setStyleSheet("color: #808088; background-color: #101012;")
        lay.addWidget(self._video_label, 1)
        return holder

    def _build_right_tabs(self) -> QWidget:
        tabs = QTabWidget()
        tabs.addTab(self._tab_virtual_cam(), "Virtual Cam")
        tabs.addTab(self._tab_local_adjust(), "Local Adjust")
        tabs.addTab(self._tab_phone_filters(), "Phone Filters")
        tabs.addTab(self._tab_stats(), "Stats")
        tabs.setMinimumWidth(330)
        tabs.setMaximumWidth(400)
        return tabs

    # -- tab: virtual cam ------------------------------------------------
    def _tab_virtual_cam(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        src = QLabel("Source: this stream (decoded, locally adjusted)")
        src.setWordWrap(True)
        lay.addWidget(src)
        btns = QHBoxLayout()
        self._btn_vcam_start = self._mk_button("Start Virtual Camera", self._on_vcam_start, btns)
        self._btn_vcam_stop = self._mk_button("Stop", self._on_vcam_stop, btns)
        self._btn_vcam_stop.setEnabled(False)
        lay.addLayout(btns)
        form = QFormLayout()
        self._vcam_backend = QLabel("-")
        self._vcam_res = QLabel("-")
        self._vcam_fps = QLabel("-")
        self._vcam_sent = QLabel("0")
        form.addRow("Backend:", self._vcam_backend)
        form.addRow("Resolution:", self._vcam_res)
        form.addRow("FPS:", self._vcam_fps)
        form.addRow("Frames sent:", self._vcam_sent)
        lay.addLayout(form)
        self._vcam_status = QLabel("OBS Virtual Camera requires OBS Studio; "
                                   "Unity Capture is the optional fallback.")
        self._vcam_status.setWordWrap(True)
        lay.addWidget(self._vcam_status)
        lay.addStretch(1)
        return w

    # -- tab: local adjust -------------------------------------------------
    def _tab_local_adjust(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        note = QLabel("PC-only (after decode). Phone filters are shared.")
        note.setWordWrap(True)
        lay.addWidget(note)

        self._la_brightness = self._mk_slider(lay, "Brightness", -100, 100, 0, self._on_local_changed)
        self._la_contrast = self._mk_slider(lay, "Contrast", -100, 100, 0, self._on_local_changed)
        self._la_saturation = self._mk_slider(lay, "Saturation", 0, 200, 100, self._on_local_changed)
        la_row = QHBoxLayout()
        self._la_mirror = QCheckBox("Mirror")
        self._la_flipv = QCheckBox("Flip V")
        for cb in (self._la_mirror, self._la_flipv):
            cb.toggled.connect(self._on_local_changed)
            la_row.addWidget(cb)
        lay.addLayout(la_row)
        rot_row = QHBoxLayout()
        rot_row.addWidget(QLabel("Rotate:"))
        self._la_rotate = QComboBox()
        self._la_rotate.addItems(["0", "90", "180", "270"])
        self._la_rotate.currentIndexChanged.connect(self._on_local_changed)
        rot_row.addWidget(self._la_rotate, 1)
        lay.addLayout(rot_row)
        self._mk_button("Reset local adjustments", self._on_local_reset, lay)
        lay.addStretch(1)
        return w

    # -- tab: phone filters ------------------------------------------------
    def _tab_phone_filters(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        note = QLabel("Compact mirror of the phone filter state (PROTOCOL.md S5). "
                      "Changes push to the phone automatically; phone-side changes "
                      "update these controls. Full fine-tuning lives on the phone UI.")
        note.setWordWrap(True)
        lay.addWidget(note)

        form = QFormLayout()
        self._pf_look = QComboBox()
        self._pf_look.addItems(LOOKS)
        form.addRow("Look:", self._pf_look)
        self._pf_beauty = self._mk_slider(form, "Beauty", 0, 100, 0, None)
        self._pf_stylize = QComboBox()
        self._pf_stylize.addItems(STYLIZES)
        form.addRow("Stylize:", self._pf_stylize)
        self._pf_amount = self._mk_slider(form, "Amount", 0, 100, 50, None)
        lay.addLayout(form)

        geo = QGroupBox("Geometry")
        gform = QFormLayout(geo)
        grow1 = QHBoxLayout()
        self._pf_mirror = QCheckBox("Mirror")
        self._pf_flipv = QCheckBox("Flip V")
        grow1.addWidget(self._pf_mirror)
        grow1.addWidget(self._pf_flipv)
        gform.addRow("", grow1)
        grow2 = QHBoxLayout()
        grow2.addWidget(QLabel("Rotate:"))
        self._pf_rotate = QComboBox()
        self._pf_rotate.addItems(["0", "90", "180", "270"])
        grow2.addWidget(self._pf_rotate, 1)
        gform.addRow("", grow2)
        self._pf_zoom = QDoubleSpinBox()
        self._pf_zoom.setRange(1.0, 8.0)
        self._pf_zoom.setSingleStep(0.1)
        self._pf_zoom.setDecimals(2)
        gform.addRow("Zoom:", self._pf_zoom)
        self._pf_panx = self._mk_slider(gform, "Pan X", -100, 100, 0, None)
        self._pf_pany = self._mk_slider(gform, "Pan Y", -100, 100, 0, None)
        lay.addWidget(geo)

        # push-on-change wiring (all filter controls)
        for widget, sig in (
            (self._pf_look, self._pf_look.currentIndexChanged),
            (self._pf_beauty, self._pf_beauty.valueChanged),
            (self._pf_stylize, self._pf_stylize.currentIndexChanged),
            (self._pf_amount, self._pf_amount.valueChanged),
            (self._pf_mirror, self._pf_mirror.toggled),
            (self._pf_flipv, self._pf_flipv.toggled),
            (self._pf_rotate, self._pf_rotate.currentIndexChanged),
            (self._pf_zoom, self._pf_zoom.valueChanged),
            (self._pf_panx, self._pf_panx.valueChanged),
            (self._pf_pany, self._pf_pany.valueChanged),
        ):
            sig.connect(self._push_phone_filters)

        lay.addStretch(1)
        return w

    # -- tab: stats ----------------------------------------------------------
    def _tab_stats(self) -> QWidget:
        w = QWidget(self)
        lay = QVBoxLayout(w)
        self._stat_labels: Dict[str, QLabel] = {}
        form = QFormLayout()
        for key, title in (
            ("fps_decoded", "Decoded FPS"),
            ("fps_displayed", "Displayed FPS"),
            ("bitrate_kbps", "Video bitrate (measured)"),
            ("loss_pct", "Packet loss"),
            ("rtt_ms", "RTT (ping/pong)"),
            ("g2g_ms", "Glass-to-glass (est.)"),
            ("nacks_sent", "NACKs sent"),
            ("plis_sent", "PLIs sent"),
            ("drops", "Dropped frames"),
            ("late_frames", "Late frames"),
            ("dup_packets", "Duplicate packets"),
            ("jitter_ms", "Jitter (est.)"),
            ("fec_recovered", "FEC recovered pkts"),
            ("fec_active", "FEC active"),
        ):
            lbl = QLabel("-")
            self._stat_labels[key] = lbl
            form.addRow(f"{title}:", lbl)
        lay.addLayout(form)
        phone_box = QGroupBox("Phone-reported stats (1 s)")
        pform = QFormLayout(phone_box)
        for key, title in (
            ("fps", "FPS"),
            ("kbps", "Bitrate"),
            ("enc_ms", "Encode ms"),
            ("loss_pct", "Loss %"),
            ("nacks", "NACKs received"),
            ("sent", "Packets sent"),
        ):
            lbl = QLabel("-")
            self._stat_labels[f"phone_{key}"] = lbl
            pform.addRow(f"{title}:", lbl)
        lay.addWidget(phone_box)
        lay.addStretch(1)
        return w

    # -- small widget helpers ------------------------------------------------
    @staticmethod
    def _mk_list(parent: QVBoxLayout) -> QListWidget:
        lst = QListWidget()
        lst.setMinimumHeight(140)
        parent.addWidget(lst, 1)
        return lst

    @staticmethod
    def _mk_button(text: str, handler: Any, parent: Any) -> QPushButton:
        btn = QPushButton(text)
        btn.clicked.connect(handler)
        parent.addWidget(btn)
        return btn

    @staticmethod
    def _mk_combo(form: QFormLayout, title: str, items: List[str], default: int) -> QComboBox:
        combo = QComboBox()
        combo.addItems(items)
        combo.setCurrentIndex(default)
        form.addRow(title + ":", combo)
        return combo

    @staticmethod
    def _mk_slider(parent: Any, title: str, lo: int, hi: int, default: int,
                   handler: Any) -> QSlider:
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(default)
        row = QHBoxLayout()
        label = QLabel(f"{title}: {default}")
        val = QLabel(str(default))
        row.addWidget(label, 0)
        row.addWidget(slider, 1)
        row.addWidget(val, 0)
        slider.valueChanged.connect(lambda v: val.setText(str(v)))
        if handler is not None:
            slider.valueChanged.connect(handler)
        if isinstance(parent, QVBoxLayout):
            wrap: Any = QWidget()
            wrap.setLayout(row)
            parent.addWidget(wrap)
        else:
            wrap2: Any = QWidget()
            wrap2.setLayout(row)
            parent.addRow("", wrap2)
        return slider

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
    # left panel actions
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
        self._app.pin_device(parsed)
        self._refresh_devices()
        self._select_device_ip(parsed)
        self._app.connect(parsed)

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
        self._app.disconnect()

    def _on_start_stream(self) -> None:
        w, h = RESOLUTIONS[self._res_combo.currentIndex()]
        fps = FPSES[self._fps_combo.currentIndex()]
        kbps = BITRATES_KBPS[self._kbps_combo.currentIndex()]
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
    # virtual cam tab
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

    def _on_vcam_stop(self) -> None:
        self._app.stop_virtual_cam()
        self._btn_vcam_start.setEnabled(True)
        self._btn_vcam_stop.setEnabled(False)
        self._vcam_backend.setText("-")
        self._vcam_res.setText("-")
        self._vcam_fps.setText("-")

    # ------------------------------------------------------------------
    # local adjust tab
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
    # phone filters tab
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
            return
        self._preview_seq = seq
        self._app.stats.tick_displayed()
        h, w = frame.shape[:2]
        img = QImage(frame.data, w, h, w * 3, QImage.Format.Format_BGR888)
        pm = QPixmap.fromImage(img)  # copies pixel data out of the numpy buffer
        self._video_label.setPixmap(pm.scaled(
            self._video_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation))

    def _poll_events(self) -> None:
        for kind, payload in self._app.drain_events():
            if kind == "state":
                connected = bool(payload.get("connected"))
                text = str(payload.get("text", ""))
                self._status_conn.setText(("connected" if connected else "disconnected") +
                                          (f" ({text})" if text else ""))
                self._btn_disconnect.setEnabled(connected)
                self._btn_connect.setEnabled(not connected)
                streaming = self._app.streaming
                self._btn_start.setEnabled(connected and not streaming)
                self._btn_stop.setEnabled(streaming)
                self._btn_front.setEnabled(connected)
                self._btn_back.setEnabled(connected)
                self._torch_check.setEnabled(connected and self._app.get_camera() == "back")
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
            elif kind == "stopped":
                self._btn_start.setEnabled(self.control_connected())
                self._btn_stop.setEnabled(False)
                if payload.get("local"):
                    self._set_status("stream stopped")
                else:
                    self._set_status("stream stopped by phone")
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
        for i in range(self._device_list.count()):
            it = self._device_list.item(i)
            existing.append((it.text(), it.data(Qt.ItemDataRole.UserRole)))
        if existing == labels:
            return
        self._device_list.clear()
        if not labels:
            self._device_list.addItem(QListWidgetItem("searching for phones..."))
            return
        for label, ip in labels:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, ip)
            self._device_list.addItem(item)
            if ip == current:
                self._device_list.setCurrentItem(item)

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
        if self._app.vcam.running:
            dims = self._app.vcam.dims
            if dims:
                self._vcam_res.setText(f"{dims[0]} x {dims[1]}")
                self._vcam_fps.setText(str(dims[2]))
            self._vcam_sent.setText(str(self._app.vcam.frames_sent))

    def _set_status(self, text: str) -> None:
        self._status_msg.setText(text)

    # ------------------------------------------------------------------
    def closeEvent(self, event: Any) -> None:
        """Graceful shutdown of every thread and socket."""
        try:
            self._app.shutdown()
        except Exception:
            log.exception("shutdown failed")
        super().closeEvent(event)


def main() -> int:
    """Application entry point (python -m omnicam)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(make_dark_palette())
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
