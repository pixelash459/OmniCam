"""Render OmniCam.ico (multi-size) from the iOS RGB icon PNG."""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
src = ROOT / "ios" / "Resources" / "Icon-60@3x.png"
out = Path(__file__).resolve().parent / "omnicam.ico"
img = Image.open(src).convert("RGBA")
sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
img.save(out, format="ICO", sizes=sizes)
print(f"wrote {out} ({out.stat().st_size} bytes)")
