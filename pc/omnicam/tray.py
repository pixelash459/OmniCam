"""System-tray support for OmniCam PC.

``TrayIcon`` wraps :class:`QSystemTrayIcon` with the OmniCam context menu
(show / stream / virtual camera / close-to-tray / quit) and exposes plain Qt
signals so :class:`omnicam.ui.MainWindow` stays the only place with app logic.

Icons come from ``omnicam.app_icon`` (``app_icon()`` / ``tray_icon(streaming)``,
owned by another module) when that import succeeds; otherwise we fall back to
``pc/packaging/omnicam.ico`` next to the source tree / PyInstaller bundle, and
finally to the platform's stock computer icon.  Nothing here needs extra data
files to be bundled.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

log = logging.getLogger("omnicam.tray")

try:  # optional: multi-size icon helper (another module owns this file)
    from omnicam import app_icon as _app_icon
except ImportError:  # pragma: no cover - depends on checkout state
    _app_icon = None  # type: ignore[assignment]

_ICO_CANDIDATES = ("packaging/omnicam.ico", "omnicam.ico")


def _fallback_ico_path() -> Optional[str]:
    """``omnicam.ico`` from the source tree or a PyInstaller onedir bundle."""
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
        roots.append(os.path.dirname(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    roots.append(os.path.dirname(here))  # pc/
    for root in roots:
        for rel in _ICO_CANDIDATES:
            path = os.path.join(root, rel)
            if os.path.isfile(path):
                return path
    return None


def _stock_icon() -> QIcon:
    app = QApplication.instance()
    if app is not None:
        try:
            return app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        except Exception:  # pragma: no cover - defensive
            log.exception("standardIcon failed")
    return QIcon()


def load_app_icon() -> QIcon:
    """Window/application icon (never raises, may be a stock icon)."""
    if _app_icon is not None:
        try:
            icon = _app_icon.app_icon()
            if icon is not None and not icon.isNull():
                return icon
        except Exception:
            log.exception("app_icon.app_icon() failed; using fallback")
    path = _fallback_ico_path()
    if path:
        icon = QIcon(path)
        if not icon.isNull():
            return icon
    return _stock_icon()


def load_tray_icon(streaming: bool) -> QIcon:
    """Tray icon; the helper adds a green dot while streaming."""
    if _app_icon is not None:
        try:
            icon = _app_icon.tray_icon(bool(streaming))
            if icon is not None and not icon.isNull():
                return icon
        except Exception:
            log.exception("app_icon.tray_icon() failed; using fallback")
    return load_app_icon()


class TrayIcon(QObject):
    """OmniCam tray icon + context menu.

    The owner connects the signals; ``set_state`` refreshes the tooltip, the
    stream/vcam action enabling and the (streaming) icon variant.
    """

    show_requested = Signal()
    toggle_requested = Signal()
    start_stream = Signal()
    stop_stream = Signal()
    start_vcam = Signal()
    stop_vcam = Signal()
    close_to_tray_changed = Signal(bool)
    quit_requested = Signal()

    def __init__(self, parent: Optional[QObject] = None, close_to_tray: bool = True) -> None:
        super().__init__(parent)
        self._streaming = False
        self._icon = QSystemTrayIcon(self)
        self._icon.setIcon(load_tray_icon(False))
        self._icon.setToolTip("OmniCam PC")
        self._icon.activated.connect(self._on_activated)

        self._menu = QMenu()
        self.act_show = QAction("Show OmniCam", self._menu)
        self.act_show.triggered.connect(self.show_requested)
        self._menu.addAction(self.act_show)
        self._menu.setDefaultAction(self.act_show)
        self._menu.addSeparator()
        self.act_start_stream = QAction("Start Stream", self._menu)
        self.act_start_stream.triggered.connect(self.start_stream)
        self.act_stop_stream = QAction("Stop Stream", self._menu)
        self.act_stop_stream.triggered.connect(self.stop_stream)
        self._menu.addAction(self.act_start_stream)
        self._menu.addAction(self.act_stop_stream)
        self.act_start_vcam = QAction("Start Virtual Camera", self._menu)
        self.act_start_vcam.triggered.connect(self.start_vcam)
        self.act_stop_vcam = QAction("Stop Virtual Camera", self._menu)
        self.act_stop_vcam.triggered.connect(self.stop_vcam)
        self._menu.addAction(self.act_start_vcam)
        self._menu.addAction(self.act_stop_vcam)
        self._menu.addSeparator()
        self.act_close_to_tray = QAction("Close to tray on X", self._menu)
        self.act_close_to_tray.setCheckable(True)
        self.act_close_to_tray.setChecked(bool(close_to_tray))
        self.act_close_to_tray.toggled.connect(
            lambda on: self.close_to_tray_changed.emit(bool(on)))
        self._menu.addAction(self.act_close_to_tray)
        self._menu.addSeparator()
        self.act_quit = QAction("Quit OmniCam", self._menu)
        self.act_quit.triggered.connect(self.quit_requested)
        self._menu.addAction(self.act_quit)
        self._icon.setContextMenu(self._menu)

    # -- lifecycle ------------------------------------------------------------
    @staticmethod
    def available() -> bool:
        return bool(QSystemTrayIcon.isSystemTrayAvailable())

    @property
    def qt_icon(self) -> QSystemTrayIcon:
        return self._icon

    @property
    def menu(self) -> QMenu:
        return self._menu

    def show(self) -> None:
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def is_visible(self) -> bool:
        return bool(self._icon.isVisible())

    def destroy(self) -> None:
        """Hide and drop the native icon so no ghost stays in the tray."""
        try:
            self._icon.hide()
            self._icon.setContextMenu(None)
            self._menu.deleteLater()
            self._icon.deleteLater()
        except RuntimeError:  # already deleted by Qt
            pass

    def show_message(self, title: str, body: str, msecs: int = 4000) -> None:
        try:
            self._icon.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, msecs)
        except Exception:  # pragma: no cover - platform quirks
            log.exception("tray showMessage failed")

    # -- state ------------------------------------------------------------------
    def set_state(self, tooltip: str, *, streaming: bool, can_start_stream: bool,
                  can_stop_stream: bool, can_start_vcam: bool, can_stop_vcam: bool) -> None:
        self._icon.setToolTip(tooltip)
        self.act_start_stream.setEnabled(bool(can_start_stream))
        self.act_stop_stream.setEnabled(bool(can_stop_stream))
        self.act_start_vcam.setEnabled(bool(can_start_vcam))
        self.act_stop_vcam.setEnabled(bool(can_stop_vcam))
        if bool(streaming) != self._streaming:
            self._streaming = bool(streaming)
            self._icon.setIcon(load_tray_icon(self._streaming))

    def tooltip(self) -> str:
        return self._icon.toolTip()

    def set_close_to_tray(self, on: bool) -> None:
        self.act_close_to_tray.setChecked(bool(on))

    def close_to_tray(self) -> bool:
        return self.act_close_to_tray.isChecked()

    # -- events -----------------------------------------------------------------
    def _on_activated(self, reason: Any) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.toggle_requested.emit()
