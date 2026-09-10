"""Render the OmniCam PC application icon (build-time, Pillow only).

Draws a flat, modern glyph that matches the PySide6 theme in
``omnicam/ui_theme.py``: a dark rounded square (#1e1e22) holding a camera
lens in the accent blue (#3d8bfd).  Every frame is drawn at 4x and reduced
with LANCZOS so edges are anti-aliased at all sizes; small frames get their
own geometry tweaks (thicker ring, no highlight) so they stay legible.

Outputs (next to this script):
  omnicam.ico  multi-size 16/20/24/32/48/64/128/256 (exe resource + Qt)
  omnicam.png  256 px preview / Qt fallback
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ICO = HERE / "omnicam.ico"
PNG = HERE / "omnicam.png"

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
SS = 4  # supersampling factor

# Palette (mirrors ui_theme.py)
BG = (0x1E, 0x1E, 0x22, 255)
BG_EDGE = (0x36, 0x36, 0x3E, 255)      # 1px rim so the tile reads on dark taskbars
LENS_DARK = (0x14, 0x14, 0x17, 255)    # inside of the barrel
ACCENT = (0x3D, 0x8B, 0xFD, 255)
ACCENT_DEEP = (0x2F, 0x74, 0xD8, 255)
HIGHLIGHT = (0xE8, 0xF1, 0xFF, 230)

RGBA = Tuple[int, int, int, int]


def _circle(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float,
            fill: RGBA | None = None, outline: RGBA | None = None,
            width: float = 0) -> None:
    box = (cx - r, cy - r, cx + r, cy + r)
    d.ellipse(box, fill=fill, outline=outline, width=int(round(width)) if outline else 0)


def render(size: int) -> Image.Image:
    """Return one anti-aliased RGBA frame of ``size`` x ``size`` pixels."""
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # -- tile: rounded square, radius ~22 % (Windows 11 style) --------------
    radius = s * 0.22
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=BG_EDGE)
    rim = max(1.0, size / 32.0) * SS   # ~1 device px rim
    d.rounded_rectangle((rim, rim, s - 1 - rim, s - 1 - rim),
                        radius=radius - rim, fill=BG)

    cx = cy = s / 2.0
    small = size <= 24

    # -- lens barrel: accent ring ------------------------------------------
    r_outer = s * (0.36 if small else 0.34)
    ring_w = max(1.5 * SS, s * (0.11 if small else 0.085))
    _circle(d, cx, cy, r_outer, fill=ACCENT)
    _circle(d, cx, cy, r_outer - ring_w, fill=LENS_DARK)

    # -- pupil: filled accent disc with a deeper lower half ------------------
    r_pupil = s * (0.15 if small else 0.15)
    _circle(d, cx, cy, r_pupil, fill=ACCENT_DEEP)
    _circle(d, cx, cy - r_pupil * 0.12, r_pupil * 0.88, fill=ACCENT)

    # -- glint (skipped at tiny sizes: it would just be noise) ---------------
    if not small:
        gr = s * 0.045
        _circle(d, cx - r_pupil * 0.42, cy - r_pupil * 0.45, gr, fill=HIGHLIGHT)

    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    frames = {sz: render(sz) for sz in SIZES}
    base = frames[256]
    base.save(PNG, format="PNG")
    base.save(
        ICO,
        format="ICO",
        sizes=[(sz, sz) for sz in SIZES],
        append_images=[frames[sz] for sz in SIZES if sz != 256],
    )
    print(f"wrote {ICO} ({ICO.stat().st_size} bytes) and {PNG.name}")


if __name__ == "__main__":
    main()
