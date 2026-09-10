"""Design tokens, palette and Qt style sheet for the OmniCam PC window.

One accent colour, neutral dark greys (never pure black), an 8 px spacing
grid, 12/13 px Segoe UI typography and 6 px rounded controls.  Styling is
kept here so ``ui.py`` only contains layout and behaviour.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Dict

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QPen

log = logging.getLogger("omnicam.ui_theme")

# -- colour tokens --------------------------------------------------------
BG = "#1e1e22"          # window background
SIDEBAR = "#232328"     # left column
CARD = "#2a2a30"        # section cards
CARD_BORDER = "#36363e"
INPUT = "#1a1a1e"       # line edits / combos / lists
INPUT_BORDER = "#3d3d46"
HOVER = "#33333a"
PRESSED = "#3a3a42"
TEXT = "#e8e8ec"
TEXT_MUTED = "#9a9aa4"
TEXT_DISABLED = "#62626c"
ACCENT = "#3d8bfd"
ACCENT_HOVER = "#5a9dfd"
ACCENT_PRESSED = "#2f74d8"
ACCENT_TEXT = "#ffffff"
PREVIEW_BG = "#141417"
OK = "#4cc38a"
WARN = "#f0b429"

SPACE = 8               # base spacing grid
RADIUS = 6
FONT_FAMILY = "Segoe UI"
FONT_PX = 12
FONT_TITLE_PX = 13


def app_font() -> QFont:
    """Default application font (12 px Segoe UI, falls back gracefully)."""
    f = QFont(FONT_FAMILY)
    f.setPixelSize(FONT_PX)
    return f


def make_dark_palette() -> QPalette:
    """Fusion palette matching the style sheet (keeps native widgets in tune)."""
    p = QPalette()
    window = QColor(BG)
    base = QColor(INPUT)
    text = QColor(TEXT)
    disabled = QColor(TEXT_DISABLED)
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(CARD))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(CARD))
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(TEXT_MUTED))
    p.setColor(QPalette.ColorRole.Button, QColor(CARD))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 96, 96))
    p.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(ACCENT_TEXT))
    p.setColor(QPalette.ColorRole.Link, QColor(ACCENT))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText,
                 QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return p


_STYLESHEET_TEMPLATE = f"""
* {{
    font-family: "{FONT_FAMILY}";
    font-size: {FONT_PX}px;
    color: {TEXT};
}}
QMainWindow, QWidget#root {{
    background-color: {BG};
}}
QToolTip {{
    background-color: {CARD};
    color: {TEXT};
    border: 1px solid {CARD_BORDER};
    padding: 4px 6px;
}}

/* -- sidebar / cards ------------------------------------------------- */
QScrollArea#sidebar, QScrollArea#sidebar > QWidget > QWidget {{
    background-color: {SIDEBAR};
    border: none;
}}
QFrame#card {{
    background-color: {CARD};
    border: 1px solid {CARD_BORDER};
    border-radius: {RADIUS}px;
}}
QToolButton#sectionHeader {{
    background: transparent;
    border: none;
    padding: 0px;
    text-align: left;
    font-size: {FONT_TITLE_PX}px;
    font-weight: 600;
    color: {TEXT};
}}
QToolButton#sectionHeader:hover {{
    color: {ACCENT_HOVER};
}}
QLabel#muted {{
    color: {TEXT_MUTED};
}}
QLabel#fieldLabel {{
    color: {TEXT_MUTED};
}}
QLabel#value {{
    color: {TEXT};
}}

/* -- buttons ----------------------------------------------------------- */
QPushButton {{
    background-color: {HOVER};
    border: 1px solid {INPUT_BORDER};
    border-radius: {RADIUS}px;
    padding: 5px 12px;
    min-height: 18px;
}}
QPushButton:hover {{
    background-color: {PRESSED};
    border-color: #4a4a54;
}}
QPushButton:pressed {{
    background-color: {INPUT};
}}
QPushButton:disabled {{
    color: {TEXT_DISABLED};
    background-color: {CARD};
    border-color: {CARD_BORDER};
}}
QPushButton[accent="true"] {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: {ACCENT_TEXT};
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background-color: {ACCENT_HOVER};
    border-color: {ACCENT_HOVER};
}}
QPushButton[accent="true"]:pressed {{
    background-color: {ACCENT_PRESSED};
}}
QPushButton[accent="true"]:disabled {{
    background-color: {CARD};
    border-color: {CARD_BORDER};
    color: {TEXT_DISABLED};
}}
QPushButton[toggle="true"] {{
    padding: 3px 10px;
}}
QPushButton[toggle="true"]:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: {ACCENT_TEXT};
}}

/* -- inputs ------------------------------------------------------------ */
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox {{
    background-color: {INPUT};
    border: 1px solid {INPUT_BORDER};
    border-radius: {RADIUS}px;
    padding: 4px 8px;
    min-height: 20px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QComboBox:disabled, QDoubleSpinBox:disabled {{
    color: {TEXT_DISABLED};
    border-color: {CARD_BORDER};
}}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: url("%(arrow_down)s");
    width: 10px;
    height: 10px;
    margin-right: 6px;
}}
QComboBox::down-arrow:disabled {{
    image: url("%(arrow_down_dis)s");
}}
QComboBox QAbstractItemView {{
    background-color: {CARD};
    border: 1px solid {CARD_BORDER};
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_TEXT};
    outline: none;
    padding: 4px;
}}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 18px;
    border: none;
    background: transparent;
}}
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {HOVER};
}}
QDoubleSpinBox::up-arrow {{
    image: url("%(arrow_up)s");
    width: 8px;
    height: 8px;
}}
QDoubleSpinBox::down-arrow {{
    image: url("%(arrow_down)s");
    width: 8px;
    height: 8px;
}}

QCheckBox {{
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {INPUT_BORDER};
    border-radius: 4px;
    background-color: {INPUT};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    image: url("%(check)s");
}}
QCheckBox::indicator:disabled {{
    border-color: {CARD_BORDER};
    background-color: {CARD};
}}
QCheckBox:disabled {{
    color: {TEXT_DISABLED};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {INPUT_BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
    background: {TEXT};
    border: 1px solid {INPUT_BORDER};
}}
QSlider::handle:horizontal:hover {{
    background: #ffffff;
}}
QSlider:disabled::sub-page:horizontal {{
    background: {CARD_BORDER};
}}

QListWidget {{
    background-color: {INPUT};
    border: 1px solid {INPUT_BORDER};
    border-radius: {RADIUS}px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 5px 6px;
    border-radius: 4px;
}}
QListWidget::item:hover {{
    background-color: {HOVER};
}}
QListWidget::item:selected {{
    background-color: {ACCENT};
    color: {ACCENT_TEXT};
}}

/* -- scrollbars -------------------------------------------------------- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {INPUT_BORDER};
    border-radius: 3px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: #50505a;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
    height: 0px;
}}

/* -- preview / stats / status ------------------------------------------ */
QFrame#statsStrip {{
    background-color: {CARD};
    border: 1px solid {CARD_BORDER};
    border-radius: {RADIUS}px;
}}
QLabel#statKey {{
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QLabel#statValue {{
    color: {TEXT};
    font-size: {FONT_TITLE_PX}px;
    font-weight: 600;
}}
QStatusBar {{
    background-color: {SIDEBAR};
    border-top: 1px solid {CARD_BORDER};
    color: {TEXT_MUTED};
}}
QStatusBar::item {{
    border: none;
}}
QLabel#connPill {{
    border-radius: 9px;
    padding: 1px 8px;
    background-color: {HOVER};
    color: {TEXT};
}}
QLabel#connPill[connected="true"] {{
    background-color: {OK};
    color: #10251a;
    font-weight: 600;
}}
QLabel#separator {{
    color: {CARD_BORDER};
}}
QMessageBox, QInputDialog {{
    background-color: {BG};
}}
"""


# -- generated indicator icons (QSS ``image:`` needs a file; no resources) --
def _paint_icon(path: Path, kind: str, colour: str, size: int) -> None:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    c = QColor(colour)
    if kind == "check":
        pen = QPen(c, max(1.6, size / 7.0))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        s = size
        path_pts = [QPointF(s * 0.22, s * 0.52), QPointF(s * 0.42, s * 0.72),
                    QPointF(s * 0.78, s * 0.30)]
        p.drawPolyline(path_pts)
    else:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        s = size
        if kind == "down":
            pts = [QPointF(s * 0.15, s * 0.32), QPointF(s * 0.85, s * 0.32),
                   QPointF(s * 0.5, s * 0.72)]
        elif kind == "up":
            pts = [QPointF(s * 0.15, s * 0.68), QPointF(s * 0.85, s * 0.68),
                   QPointF(s * 0.5, s * 0.28)]
        else:  # right
            pts = [QPointF(s * 0.32, s * 0.15), QPointF(s * 0.32, s * 0.85),
                   QPointF(s * 0.72, s * 0.5)]
        p.drawPolygon(pts)
    p.end()
    img.save(str(path), "PNG")


def icon_dir() -> Path:
    """Per-user cache folder for generated indicator PNGs."""
    return Path(tempfile.gettempdir()) / "omnicam_ui_icons"


def build_stylesheet() -> str:
    """Return the QSS with generated icon paths filled in.

    Must be called after a ``QGuiApplication`` exists (QPainter needs one).
    Icon generation failures fall back to ``image: none`` (widgets still work).
    """
    icons = {
        "arrow_down": ("down", TEXT_MUTED, 16),
        "arrow_down_dis": ("down", TEXT_DISABLED, 16),
        "arrow_up": ("up", TEXT_MUTED, 16),
        "check": ("check", ACCENT_TEXT, 16),
    }
    paths: Dict[str, str] = {}
    try:
        folder = icon_dir()
        folder.mkdir(parents=True, exist_ok=True)
        for key, (kind, colour, size) in icons.items():
            file = folder / f"{key}_{colour.lstrip('#')}.png"
            if not file.exists():
                _paint_icon(file, kind, colour, size)
            paths[key] = file.as_posix()
    except Exception:  # pragma: no cover - filesystem/painter oddities
        log.exception("icon generation failed; using plain indicators")
        return _STYLESHEET_TEMPLATE.replace('image: url("%(arrow_down)s");', "image: none;") \
            .replace('image: url("%(arrow_down_dis)s");', "image: none;") \
            .replace('image: url("%(arrow_up)s");', "image: none;") \
            .replace('image: url("%(check)s");', "")
    return _STYLESHEET_TEMPLATE % paths


# Backwards-compatible name (no icons); prefer :func:`build_stylesheet`.
STYLESHEET = _STYLESHEET_TEMPLATE % {
    "arrow_down": "", "arrow_down_dis": "", "arrow_up": "", "check": ""}
