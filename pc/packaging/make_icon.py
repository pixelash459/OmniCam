"""Render the OmniCam application icon for both apps (build-time, Pillow only).

One design, shared by the PC client and the iPhone app: a deep-blue gradient
tile holding a white camera lens, a cyan-to-blue pupil, and two orange
"broadcast" arcs with a live dot in the top-right (wireless webcam; orange is
the phone UI accent, blue the PC accent). Every frame is drawn at 4x and
reduced with LANCZOS; frames of 32 px and below drop the arcs and glint so
they stay legible in the taskbar and tray.

Outputs:
  packaging/omnicam.ico  multi-size 16..256 (exe resource + Qt)
  packaging/omnicam.png  256 px preview / Qt fallback
  ../../ios/Resources/Icon-*.png  iPhone icons (opaque, full-bleed square;
                                  iOS applies its own corner mask)
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

from PIL import Image, ImageChops, ImageDraw

HERE = Path(__file__).resolve().parent
ICO = HERE / "omnicam.ico"
PNG = HERE / "omnicam.png"
IOS_RES = HERE.parent.parent / "ios" / "Resources"

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
IOS_ICONS = {  # file -> pixel size (Info.plist CFBundleIconFiles)
    "Icon-60@2x.png": 120, "Icon-60@3x.png": 180,
    "Icon-Small-40@2x.png": 80, "Icon-Small-40@3x.png": 120,
    "Icon-Small@2x.png": 58, "Icon-Small@3x.png": 87,
}
SS = 4  # supersampling factor

RGBA = Tuple[int, int, int, int]

BG_TOP = (0x24, 0x4F, 0xD6, 255)       # gradient, top-left
BG_BOTTOM = (0x0B, 0x14, 0x38, 255)    # gradient, bottom-right
RIM = (0xFF, 0xFF, 0xFF, 40)           # faint edge so the tile reads on dark taskbars
LENS_RING = (0xF4, 0xF7, 0xFF, 255)
LENS_BARREL = (0x0A, 0x10, 0x26, 255)
LENS_INNER_RING = (0x2A, 0x3A, 0x6E, 255)
PUPIL_TOP = (0x5F, 0xE3, 0xFF, 255)
PUPIL_BOTTOM = (0x2F, 0x5B, 0xF0, 255)
GLINT = (0xFF, 0xFF, 0xFF, 235)
SIGNAL = (0xFF, 0x7A, 0x1A, 255)        # phone accent orange


def _gradient(size: int, top: RGBA, bottom: RGBA, diagonal: bool = True) -> Image.Image:
    """Square gradient from ``top`` to ``bottom``: top-left -> bottom-right when
    ``diagonal``, otherwise top -> bottom."""
    vert = Image.linear_gradient("L").resize((size, size), Image.Resampling.BILINEAR)
    if diagonal:
        horiz = vert.transpose(Image.Transpose.ROTATE_90).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        ramp = ImageChops.add(vert, horiz, scale=2.0)  # (x + y) / 2
    else:
        ramp = vert
    a = Image.new("RGBA", (size, size), top)
    b = Image.new("RGBA", (size, size), bottom)
    return Image.composite(b, a, ramp)


def _disc_mask(s: int, cx: float, cy: float, r: float) -> Image.Image:
    m = Image.new("L", (s, s), 0)
    ImageDraw.Draw(m).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    return m


def render(size: int, *, full_bleed: bool = False) -> Image.Image:
    """One anti-aliased RGBA frame. ``full_bleed`` = square, opaque (iOS)."""
    s = size * SS
    small = size <= 32

    # -- tile ---------------------------------------------------------------
    tile = _gradient(s, BG_TOP, BG_BOTTOM)
    if full_bleed:
        img = tile
    else:
        radius = s * 0.225  # Windows 11 style
        mask = Image.new("L", (s, s), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=radius, fill=255)
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        img.paste(tile, (0, 0), mask)
        rim_w = max(1, int(round(s / 128)))
        ImageDraw.Draw(img).rounded_rectangle((0, 0, s - 1, s - 1), radius=radius,
                                              outline=RIM, width=rim_w)
    d = ImageDraw.Draw(img)

    # -- lens: slightly below/left of centre to leave room for the arcs ------
    cx = s * (0.5 if small else 0.46)
    cy = s * (0.5 if small else 0.54)
    r_outer = s * (0.36 if small else 0.30)
    ring_w = s * (0.085 if small else 0.06)
    d.ellipse((cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer), fill=LENS_RING)
    r_barrel = r_outer - ring_w
    d.ellipse((cx - r_barrel, cy - r_barrel, cx + r_barrel, cy + r_barrel), fill=LENS_BARREL)
    if not small:
        r_in = r_barrel * 0.80
        d.ellipse((cx - r_in, cy - r_in, cx + r_in, cy + r_in),
                  outline=LENS_INNER_RING, width=max(1, int(s * 0.012)))

    # -- pupil: vertical cyan -> blue gradient disc --------------------------
    r_pupil = r_barrel * (0.62 if small else 0.56)
    side = int(2 * r_pupil) + 2
    pupil = _gradient(side, PUPIL_TOP, PUPIL_BOTTOM, diagonal=False)
    px, py = int(cx - r_pupil), int(cy - r_pupil)
    img.paste(pupil, (px, py), _disc_mask(s, cx, cy, r_pupil).crop((px, py, px + side, py + side)))
    d = ImageDraw.Draw(img)

    if not small:
        # glint
        gr = r_pupil * 0.28
        gx, gy = cx - r_pupil * 0.38, cy - r_pupil * 0.40
        d.ellipse((gx - gr, gy - gr, gx + gr, gy + gr), fill=GLINT)

        # broadcast: live dot + two arcs, top-right of the lens
        ox, oy = s * 0.775, s * 0.225
        dot = s * 0.042
        d.ellipse((ox - dot, oy - dot, ox + dot, oy + dot), fill=SIGNAL)
        arc_w = max(SS, int(s * 0.034))
        for rr in (s * 0.11, s * 0.185):
            # concentric quarter arcs opening toward the top-right corner
            # (PIL angles run clockwise from 3 o'clock)
            box = (ox - rr, oy - rr, ox + rr, oy + rr)
            d.arc(box, start=-80, end=-10, fill=SIGNAL, width=arc_w)

    out = img.resize((size, size), Image.Resampling.LANCZOS)
    if full_bleed:
        out = out.convert("RGB")  # iOS icons must be opaque
    return out


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
    if IOS_RES.is_dir():
        for name, px in IOS_ICONS.items():
            render(px, full_bleed=True).save(IOS_RES / name, format="PNG")
        print(f"wrote {len(IOS_ICONS)} iPhone icons in {IOS_RES}")


if __name__ == "__main__":
    main()
