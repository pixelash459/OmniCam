"""1.3.0: queuing-delay feedback, remote focus, LUT list, phone power stats."""

from __future__ import annotations

from omnicam.app import OmniCamApp
from omnicam.net import ControlClient, VideoReceiver


def _capture_sends(client: ControlClient) -> list:
    sent: list = []
    client._send = lambda obj, **_: sent.append(obj) or True  # type: ignore[assignment]
    return sent


# ---------------------------------------------------------------------------
# queuing delay (GCC-style delay signal)
# ---------------------------------------------------------------------------
def _feed(rx: VideoReceiver, frames: int, t0: float, ts0: int, extra_delay_s: float = 0.0):
    t, ts = t0, ts0
    with rx._stat_lock:
        for _ in range(frames):
            rx._note_frame_delay(ts & 0xFFFFFFFF, t + extra_delay_s)
            t += 1 / 30.0
            ts += 3000
    return t, ts


def test_queue_delay_zero_on_a_steady_link():
    rx = VideoReceiver()
    _feed(rx, 60, 100.0, 12345)
    assert rx.get_queue_delay_ms() < 1.0


def test_queue_delay_tracks_a_filling_buffer():
    rx = VideoReceiver()
    t, ts = _feed(rx, 60, 100.0, 12345)
    _feed(rx, 20, t, ts, extra_delay_s=0.120)  # AP queue adds 120 ms
    q = rx.get_queue_delay_ms()
    assert 100.0 < q <= 121.0
    assert rx.get_stats()["qdelay_ms"] == q


def test_queue_delay_survives_rtp_timestamp_wrap_and_resets():
    rx = VideoReceiver()
    t, ts = _feed(rx, 30, 5.0, 0xFFFFFFFF - 3000 * 10)  # wraps mid-run
    assert rx.get_queue_delay_ms() < 1.0
    rx.reset()
    assert rx.get_queue_delay_ms() == 0.0


# ---------------------------------------------------------------------------
# control messages
# ---------------------------------------------------------------------------
def test_rr_carries_qdelay_only_when_given():
    c = ControlClient(on_message=lambda m: None, on_state=lambda *a: None)
    sent = _capture_sends(c)
    c.send_rr(1.0, 2.0, 77, 29.9)
    c.send_rr(1.0, 2.0, 78, 29.9, qdelay_ms=42.26)
    assert "qdelay_ms" not in sent[0]
    assert sent[1]["qdelay_ms"] == 42.3


def test_focus_messages_are_clamped():
    c = ControlClient(on_message=lambda m: None, on_state=lambda *a: None)
    sent = _capture_sends(c)
    c.send_focus(1.7, -0.2, lock=True)
    c.send_focus_auto()
    assert sent == [{"t": "focus", "x": 1.0, "y": 0.0, "lock": True},
                    {"t": "focus", "auto": True}]


def test_app_gates_focus_on_phone_caps_and_tracks_luts():
    app = OmniCamApp()
    sent = _capture_sends(app.control)
    assert app.focus_at(0.5, 0.5) is False  # old phone: nothing sent
    assert sent == []

    app._on_message({"t": "welcome", "caps": ["focus", "luts"], "luts": ["Teal.cube"]})
    assert app.phone_supports("focus")
    assert app.get_phone_luts() == ["Teal.cube"]
    assert app.focus_at(0.25, 0.75, lock=False) is True
    assert sent[-1] == {"t": "focus", "x": 0.25, "y": 0.75, "lock": False}

    app.drain_events()
    app._on_message({"t": "luts", "names": ["A.cube", "B.cube"]})
    app._on_message({"t": "focus_ok", "auto": False, "locked": True})
    kinds = [k for k, _ in app.drain_events()]
    assert kinds == ["luts", "focus_ok"]
    assert app.get_phone_luts() == ["A.cube", "B.cube"]

    app._on_message({"t": "welcome"})  # a 1.2.x phone advertises nothing
    assert not app.phone_supports("focus")


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def test_phone_power_text():
    from omnicam.ui import MainWindow

    assert MainWindow.phone_power_text({}) == ("-", "-", False)
    assert MainWindow.phone_power_text({"batt": 0.83, "charging": True, "thermal": "nominal"}) \
        == ("83% charging", "83%+", False)
    assert MainWindow.phone_power_text({"batt": -1, "thermal": "serious"}) == ("-", "hot", True)
