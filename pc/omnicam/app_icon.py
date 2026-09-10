"""Application icon for the window title bar, taskbar and system tray.

The icon is rendered at build time by ``pc/packaging/make_icon.py`` into
``omnicam.ico`` / ``omnicam.png``.  From source we read it out of
``pc/packaging``; in the PyInstaller onedir build it is shipped as a data
file next to the exe (``--add-data``), so we look in ``sys._MEIPASS`` and the
executable's folder.  If neither exists (stripped build, odd launcher) a
matching icon is painted at runtime so callers never receive a null
``QIcon``.

Icons are created lazily and cached at module level: a ``QIcon`` may only be
built once a ``QGuiApplication`` exists, so nothing here runs at import time.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap

# Mirrors ui_theme.py (kept literal so this module has no UI imports).
_BG = "#1e1e22"
_BG_EDGE = "#36363e"
_LENS_DARK = "#141417"
_ACCENT = "#3d8bfd"
_ACCENT_DEEP = "#2f74d8"
_LIVE = "#22c55e"

_FILENAMES = ("omnicam.ico", "omnicam.png")

_app_icon: Optional[QIcon] = None
_tray_icons: Dict[bool, QIcon] = {}


def _candidate_dirs() -> list:
    dirs = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(meipass)
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
    else:
        pkg_dir = os.path.dirname(os.path.abspath(__file__))
        dirs.append(os.path.join(os.path.dirname(pkg_dir), "packaging"))
    return dirs


def icon_path() -> Optional[str]:
    """Absolute path of ``omnicam.ico`` (or ``.png``), or ``None`` if missing.

    Frozen: ``sys._MEIPASS`` then next to ``sys.executable``.
    Source: ``pc/packaging``.
    """
    for folder in _candidate_dirs():
        for name in _FILENAMES:
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                return p
    return None


# -- runtime fallback --------------------------------------------------------
def _paint_fallback(size: int) -> QPixmap:
    """Rounded dark square with the accent lens, same design as the .ico."""
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)

    s = float(size)
    radius = s * 0.22
    p.setBrush(QColor(_BG_EDGE))
    p.drawRoundedRect(QRectF(0, 0, s, s), radius, radius)
    rim = max(1.0, s / 32.0)
    p.setBrush(QColor(_BG))
    p.drawRoundedRect(QRectF(rim, rim, s - 2 * rim, s - 2 * rim),
                      radius - rim, radius - rim)

    cx = cy = s / 2.0
    small = size <= 24
    r_outer = s * (0.36 if small else 0.34)
    ring_w = max(1.5, s * (0.11 if small else 0.085))
    p.setBrush(QColor(_ACCENT))
    p.drawEllipse(QRectF(cx - r_outer, cy - r_outer, 2 * r_outer, 2 * r_outer))
    r_in = r_outer - ring_w
    p.setBrush(QColor(_LENS_DARK))
    p.drawEllipse(QRectF(cx - r_in, cy - r_in, 2 * r_in, 2 * r_in))
    r_pupil = s * 0.15
    p.setBrush(QColor(_ACCENT_DEEP))
    p.drawEllipse(QRectF(cx - r_pupil, cy - r_pupil, 2 * r_pupil, 2 * r_pupil))
    r2 = r_pupil * 0.88
    p.setBrush(QColor(_ACCENT))
    p.drawEllipse(QRectF(cx - r2, cy - r_pupil * 0.12 - r2, 2 * r2, 2 * r2))
    p.end()
    return QPixmap.fromImage(img)


def _fallback_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 32, 64, 256):
        icon.addPixmap(_paint_fallback(size))
    return icon


def app_icon() -> QIcon:
    """The OmniCam application icon (never null). Cached after first call."""
    global _app_icon
    if _app_icon is not None:
        return _app_icon
    icon: Optional[QIcon] = None
    path = icon_path()
    if path:
        candidate = QIcon(path)
        if not candidate.isNull() and candidate.availableSizes():
            icon = candidate
    if icon is None:
        icon = _fallback_icon()
    _app_icon = icon
    return icon


# -- tray variant ------------------------------------------------------------
def _with_live_dot(pm: QPixmap) -> QPixmap:
    """Overlay a green dot with a 1 px dark outline in the bottom-right."""
    out = QPixmap(pm)
    size = out.width()
    d = max(6.0, size * 0.40)          # dot diameter
    margin = 0.5
    x = size - d - margin
    y = size - d - margin
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(QColor(_BG))
    pen.setWidthF(1.0)
    p.setPen(pen)
    p.setBrush(QColor(_LIVE))
    p.drawEllipse(QRectF(x, y, d, d))
    p.end()
    return out


def tray_icon(streaming: bool = False) -> QIcon:
    """Tray icon; ``streaming=True`` adds a green "live" dot. Cached."""
    key = bool(streaming)
    cached = _tray_icons.get(key)
    if cached is not None:
        return cached
    base = app_icon()
    if not key:
        _tray_icons[key] = base
        return base
    icon = QIcon()
    for size in (16, 24, 32):
        pm = base.pixmap(size, size)
        if pm.isNull():
            pm = _paint_fallback(size)
        icon.addPixmap(_with_live_dot(pm))
    _tray_icons[key] = icon
    return icon
