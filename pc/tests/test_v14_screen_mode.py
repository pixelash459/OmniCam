"""1.4.0: handcam screen mode (session keys, caps gating) and shadow lift."""

from __future__ import annotations

from omnicam.app import OmniCamApp
from omnicam.net import default_filter_state, merge_session_state


def _capture_sends(app: OmniCamApp) -> list:
    sent: list = []
    app.control._send = lambda obj, **_: sent.append(obj) or True  # type: ignore[assignment]
    return sent


def test_session_screen_keys_default_and_clamp():
    state = merge_session_state(None, {})
    assert state["screen"] is False and state["screen_ev"] == 0.0
    state = merge_session_state(state, {"screen": 1, "screen_ev": 7.3})
    assert state["screen"] is True and state["screen_ev"] == 2.0
    state = merge_session_state(state, {"screen_ev": "-0.66"})
    assert state["screen_ev"] == -0.7
    assert state["screen"] is True  # absent keys stay


def test_shadows_in_default_filter_state():
    assert default_filter_state()["adjust"]["shadows"] == 0.0


def test_screen_mode_needs_phone_cap():
    app = OmniCamApp()
    sent = _capture_sends(app)
    assert app.set_screen_mode(True) is False
    assert sent == []

    app._on_message({"t": "welcome", "caps": ["focus", "screen_mode"]})
    assert app.set_screen_mode(True, -0.5) is True
    msg = sent[-1]
    assert msg["t"] == "session"
    assert msg["state"]["screen"] is True
    assert msg["state"]["screen_ev"] == -0.5
    assert app.get_session()["screen"] is True


def test_screen_exposure_text():
    from omnicam.ui import MainWindow

    assert MainWindow.screen_exposure_text({}) == ""
    assert MainWindow.screen_exposure_text({"exp_ms": 4.0, "iso": 29, "hi": 0.82}) \
        == "1/250 s, ISO 29, screen 82%"
