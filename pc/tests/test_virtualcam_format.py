"""Regression: the virtual camera must be opened as BGR.

The decoder yields BGR24 frames.  pyvirtualcam defaults to RGB, so opening
without ``fmt=PixelFormat.BGR`` swapped red and blue for every application
reading the virtual camera (blue skin, orange clothes) while the in-app
preview looked correct.
"""
import enum
import sys
import types

import numpy as np

from omnicam.virtualcam_out import VirtualCamOut


class _PixelFormat(enum.Enum):
    RGB = "rgb"
    BGR = "bgr"


class _FakeCamera:
    opened = []

    def __init__(self, width, height, fps, fmt=_PixelFormat.RGB, backend=None):
        self.width, self.height, self.fps, self.fmt, self.backend = width, height, fps, fmt, backend
        self.device = "fake"
        self.frames = []
        _FakeCamera.opened.append(self)

    def send(self, frame):
        self.frames.append(frame)

    def sleep_until_next_frame(self):
        pass

    def close(self):
        pass


def test_camera_opened_as_bgr(monkeypatch):
    fake = types.ModuleType("pyvirtualcam")
    fake.PixelFormat = _PixelFormat
    fake.Camera = _FakeCamera
    monkeypatch.setitem(sys.modules, "pyvirtualcam", fake)
    _FakeCamera.opened.clear()

    out = VirtualCamOut()
    try:
        out.start(640, 360, 30)
        assert _FakeCamera.opened, "camera must be opened"
        assert _FakeCamera.opened[0].fmt is _PixelFormat.BGR
        out.submit(np.zeros((360, 640, 3), dtype=np.uint8))
    finally:
        out.stop()
