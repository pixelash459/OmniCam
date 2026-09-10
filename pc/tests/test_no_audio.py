"""Regression guard: audio was removed project-wide (video-only protocol).

No AudioReceiver/AudioDecoder classes, no port 9922, no sounddevice import,
and the ``start`` control message carries video only.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

PC_OMNICAM = Path(__file__).resolve().parent.parent / "omnicam"


def _sources() -> dict:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(PC_OMNICAM.glob("*.py"))}


@pytest.mark.parametrize("module", ["omnicam.net", "omnicam.decoder", "omnicam.app"])
def test_no_audio_classes(module):
    mod = importlib.import_module(module)
    assert not hasattr(mod, "AudioReceiver"), f"{module} still defines AudioReceiver"
    assert not hasattr(mod, "AudioDecoder"), f"{module} still defines AudioDecoder"


def test_no_port_9922_anywhere():
    for name, text in _sources().items():
        assert "9922" not in text, f"port 9922 found in {name}"


def test_no_sounddevice_import():
    for name, text in _sources().items():
        assert "sounddevice" not in text, f"sounddevice referenced in {name}"
    # and importing the package must not pull it in transitively
    for module in ("omnicam", "omnicam.net", "omnicam.decoder", "omnicam.app",
                   "omnicam.stats", "omnicam.virtualcam_out"):
        importlib.import_module(module)
    assert "sounddevice" not in sys.modules


def test_no_asc_hex_or_audio_in_protocol_messages():
    """The wire messages must be video-only: no asc_hex / 'audio' keys."""
    for name, text in _sources().items():
        assert "asc_hex" not in text, f"asc_hex found in {name}"


def test_start_message_is_video_only():
    """``start`` carries rtp_host + video dict only -- no audio key."""
    from omnicam.net import ControlClient

    cc = ControlClient()
    captured = {}

    def fake_send(obj):
        captured["msg"] = obj
        return True

    cc._send = fake_send  # type: ignore[method-assign]
    assert cc.send_start("192.168.1.50", {"port": 9921, "w": 1280, "h": 720,
                                          "fps": 30, "kbps": 3000, "keyint": 60})
    msg = captured["msg"]
    assert set(msg) == {"t", "rtp_host", "video"}
    assert "audio" not in msg
    assert "audio" not in msg["video"]
    assert msg["video"]["port"] == 9921
