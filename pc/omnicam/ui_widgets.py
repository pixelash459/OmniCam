"""Small reusable widgets for the OmniCam PC window (no business logic).

* :class:`Section` -- titled card with an optional collapse toggle.
* :class:`FormGrid` -- two-column label/field grid on the 8 px grid.
* :class:`SliderRow` -- label + slider + live value readout.
* :class:`PreviewWidget` -- aspect-preserving video surface with placeholder.
* :class:`StatsStrip` -- slim key/value strip under the preview.
"""

from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from omnicam import ui_theme as T

LABEL_COL_WIDTH = 68  # shared label column so forms and slider rows line up


class Section(QFrame):
    """Card with a 13 px title row and a body; optionally collapsible."""

    toggled = Signal(bool)  # True when expanded

    def __init__(self, title: str, collapsible: bool = False,
                 expanded: bool = True, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._collapsible = collapsible
        outer = QVBoxLayout(self)
        outer.setContentsMargins(T.SPACE + 4, T.SPACE + 2, T.SPACE + 4, T.SPACE + 4)
        outer.setSpacing(T.SPACE)

        self._header = QToolButton(self)
        self._header.setObjectName("sectionHeader")
        self._header.setText(title)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor if collapsible
                               else Qt.CursorShape.ArrowCursor)
        self._header.setCheckable(collapsible)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(0)
        header_row.addWidget(self._header, 0, Qt.AlignmentFlag.AlignLeft)
        header_row.addStretch(1)
        outer.addLayout(header_row)

        self.body = QWidget(self)
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(T.SPACE)
        outer.addWidget(self.body)

        if collapsible:
            self._header.toggled.connect(self._on_toggled)
            self._header.setChecked(expanded)
            self._on_toggled(expanded)
        else:
            self._header.setArrowType(Qt.ArrowType.NoArrow)

    def _on_toggled(self, expanded: bool) -> None:
        self.body.setVisible(expanded)
        self._header.setArrowType(Qt.ArrowType.DownArrow if expanded
                                  else Qt.ArrowType.RightArrow)
        self.toggled.emit(expanded)

    def is_expanded(self) -> bool:
        """True when the body is visible."""
        return not self._collapsible or self._header.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        """Programmatically expand/collapse (no-op when not collapsible)."""
        if self._collapsible:
            self._header.setChecked(expanded)

    def layout_body(self) -> QVBoxLayout:
        """Vertical layout of the card body."""
        return self._body_layout

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        """Add a widget to the body."""
        self._body_layout.addWidget(widget, stretch)

    def add_layout(self, layout: object) -> None:
        """Add a nested layout to the body."""
        self._body_layout.addLayout(layout)  # type: ignore[arg-type]


class FormGrid(QGridLayout):
    """Two-column grid: muted label on the left, field(s) on the right."""

    def __init__(self) -> None:
        super().__init__()
        self.setContentsMargins(0, 0, 0, 0)
        self.setHorizontalSpacing(T.SPACE + 4)
        self.setVerticalSpacing(T.SPACE)
        self.setColumnMinimumWidth(0, LABEL_COL_WIDTH)
        self.setColumnStretch(1, 1)
        self._rows = 0

    def add_row(self, label: str, field: QWidget) -> QLabel:
        """Append a labelled field; returns the label widget."""
        lbl = QLabel(label)
        lbl.setObjectName("fieldLabel")
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.addWidget(lbl, self._rows, 0)
        self.addWidget(field, self._rows, 1)
        self._rows += 1
        return lbl

    def add_span(self, widget: QWidget) -> None:
        """Append a widget spanning both columns."""
        self.addWidget(widget, self._rows, 0, 1, 2)
        self._rows += 1


class SliderRow(QWidget):
    """``Title  [=====o====]  42`` -- exposes the inner :class:`QSlider`."""

    def __init__(self, title: str, lo: int, hi: int, default: int,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(T.SPACE + 4)
        self.label = QLabel(title)
        self.label.setObjectName("fieldLabel")
        self.label.setFixedWidth(LABEL_COL_WIDTH)
        self.label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(lo, hi)
        self.slider.setValue(default)
        self.value = QLabel(str(default))
        self.value.setObjectName("value")
        self.value.setMinimumWidth(30)
        self.value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.label)
        row.addWidget(self.slider, 1)
        row.addWidget(self.value)
        self.slider.valueChanged.connect(lambda v: self.value.setText(str(v)))


class PreviewWidget(QWidget):
    """Video surface: dark rounded panel, frame scaled to keep aspect."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pixmap: Optional[QPixmap] = None
        self._placeholder = "No stream"
        self._hint = "Connect a phone and press Start Stream"
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 180)

    def sizeHint(self) -> QSize:  # noqa: D401 - Qt override
        return QSize(960, 540)

    def set_pixmap(self, pm: Optional[QPixmap]) -> None:
        """Show ``pm`` (or the placeholder when ``None``)."""
        self._pixmap = pm
        self.update()

    def set_placeholder(self, title: str, hint: str = "") -> None:
        """Text shown while no frame is available."""
        self._placeholder = title
        self._hint = hint
        if self._pixmap is None:
            self.update()

    def has_frame(self) -> bool:
        """True when a frame is currently displayed."""
        return self._pixmap is not None

    def paintEvent(self, _event: object) -> None:  # noqa: D401 - Qt override
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(T.PREVIEW_BG))
        p.drawRoundedRect(rect, T.RADIUS, T.RADIUS)
        if self._pixmap is None or self._pixmap.isNull():
            p.setPen(QColor(T.TEXT_MUTED))
            f = p.font()
            f.setPixelSize(T.FONT_TITLE_PX + 2)
            f.setWeight(QFont.Weight.DemiBold)
            p.setFont(f)
            title_rect = QRect(rect.left(), rect.center().y() - 24, rect.width(), 22)
            p.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, self._placeholder)
            if self._hint:
                f.setPixelSize(T.FONT_PX)
                f.setWeight(QFont.Weight.Normal)
                p.setFont(f)
                p.setPen(QColor(T.TEXT_DISABLED))
                hint_rect = QRect(rect.left(), rect.center().y() + 2, rect.width(), 20)
                p.drawText(hint_rect, Qt.AlignmentFlag.AlignCenter, self._hint)
            p.end()
            return
        target = rect.adjusted(2, 2, -2, -2)
        scaled = self._pixmap.scaled(target.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.FastTransformation)
        x = target.left() + (target.width() - scaled.width()) // 2
        y = target.top() + (target.height() - scaled.height()) // 2
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.drawPixmap(x, y, scaled)
        p.end()


class StatsStrip(QFrame):
    """Horizontal strip of ``key`` / ``value`` pairs (``set_value`` to update)."""

    def __init__(self, items: Iterable[Tuple[str, str]],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("statsStrip")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(T.SPACE + 4, T.SPACE - 2, T.SPACE + 4, T.SPACE - 2)
        lay.setSpacing(T.SPACE * 3)
        self._values: Dict[str, QLabel] = {}
        for key, title in items:
            cell = QVBoxLayout()
            cell.setContentsMargins(0, 0, 0, 0)
            cell.setSpacing(0)
            k = QLabel(title)
            k.setObjectName("statKey")
            v = QLabel("-")
            v.setObjectName("statValue")
            cell.addWidget(k)
            cell.addWidget(v)
            lay.addLayout(cell)
            self._values[key] = v
        lay.addStretch(1)
        self.trailing = QHBoxLayout()
        self.trailing.setSpacing(T.SPACE)
        lay.addLayout(self.trailing)

    def set_value(self, key: str, text: str) -> None:
        """Update one cell."""
        lbl = self._values.get(key)
        if lbl is not None:
            lbl.setText(text)

    def label(self, key: str) -> Optional[QLabel]:
        """Value label for ``key`` (or ``None``)."""
        return self._values.get(key)
