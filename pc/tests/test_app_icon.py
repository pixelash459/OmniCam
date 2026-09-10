"""``omnicam.app_icon``: the window/taskbar/tray icon resolves from source and
from a simulated PyInstaller layout, is never null, and the streaming tray
variant is visibly different from the idle one.  Runs offscreen."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

PC_DIR = Path(__file__).resolve().parent.parent
ICO = PC_DIR / "packaging" / "omnicam.ico"


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def fresh_icon_module():
    """Reset the module-level caches so each test builds its own icons."""
    from omnicam import app_icon

    app_icon._app_icon = None
    app_icon._tray_icons.clear()
    yield app_icon
    app_icon._app_icon = None
    app_icon._tray_icons.clear()


def _image_bytes(icon, size: int) -> bytes:
    img = icon.pixmap(size, size).toImage()
    assert not img.isNull()
    return bytes(img.constBits())


def test_icon_path_resolves_from_source(fresh_icon_module):
    assert ICO.exists(), "run packaging/make_icon.py first"
    p = fresh_icon_module.icon_path()
    assert p is not None
    assert Path(p).resolve() == ICO.resolve()


def test_icon_path_frozen_uses_meipass(fresh_icon_module, monkeypatch, tmp_path):
    bundle = tmp_path / "_internal"
    bundle.mkdir()
    shutil.copy(ICO, bundle / "omnicam.ico")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr("sys.executable", str(tmp_path / "nowhere" / "OmniCam.exe"))
    p = fresh_icon_module.icon_path()
    assert p is not None
    assert Path(p) == bundle / "omnicam.ico"


def test_icon_path_frozen_falls_back_next_to_exe(fresh_icon_module, monkeypatch, tmp_path):
    exe_dir = tmp_path / "OmniCam"
    exe_dir.mkdir()
    shutil.copy(ICO, exe_dir / "omnicam.ico")
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path / "empty"), raising=False)
    monkeypatch.setattr("sys.executable", str(exe_dir / "OmniCam.exe"))
    assert fresh_icon_module.icon_path() == str(exe_dir / "omnicam.ico")


def test_icon_path_none_when_missing(fresh_icon_module, monkeypatch, tmp_path):
    monkeypatch.setattr("sys.frozen", True, raising=False)
    monkeypatch.setattr("sys._MEIPASS", str(tmp_path / "a"), raising=False)
    monkeypatch.setattr("sys.executable", str(tmp_path / "b" / "OmniCam.exe"))
    assert fresh_icon_module.icon_path() is None


def test_app_icon_not_null_and_cached(qapp, fresh_icon_module):
    icon = fresh_icon_module.app_icon()
    assert not icon.isNull()
    sizes = icon.availableSizes()
    assert sizes, "multi-size .ico should expose frames"
    assert any(s.width() >= 256 for s in sizes)
    assert fresh_icon_module.app_icon() is icon


def test_app_icon_fallback_when_file_missing(qapp, fresh_icon_module, monkeypatch):
    monkeypatch.setattr(fresh_icon_module, "icon_path", lambda: None)
    icon = fresh_icon_module.app_icon()
    assert not icon.isNull()
    widths = sorted(s.width() for s in icon.availableSizes())
    assert widths == [16, 32, 64, 256]
    # Something was actually painted (not a transparent square).
    img = icon.pixmap(32, 32).toImage()
    assert any(img.pixelColor(x, 16).alpha() > 0 for x in range(32))


def test_tray_icon_streaming_differs(qapp, fresh_icon_module):
    idle = fresh_icon_module.tray_icon(False)
    live = fresh_icon_module.tray_icon(True)
    assert not idle.isNull() and not live.isNull()
    assert _image_bytes(idle, 32) != _image_bytes(live, 32)
    # Bottom-right corner turns green-ish on the live variant only.
    live_px = live.pixmap(32, 32).toImage().pixelColor(27, 27)
    idle_px = idle.pixmap(32, 32).toImage().pixelColor(27, 27)
    assert live_px.green() > live_px.red() and live_px.green() > live_px.blue()
    assert idle_px != live_px
    # Both variants cached.
    assert fresh_icon_module.tray_icon(True) is live
    assert fresh_icon_module.tray_icon(False) is idle
