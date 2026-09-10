"""Control channel (TCP 9923) and discovery (UDP 9920) against fake phones.

Runs a real fake-phone TCP server serving the PROTOCOL.md section 2.2
messages (with ``started`` carrying ONLY ssrc_video/ssrc_fec/fec), asserts
the ``hello`` the PC sends is correct and audio-free, and exercises the UDP
beacon device list.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from helpers import wait_until

from omnicam.app import OmniCamApp
from omnicam.net import BEACON_MAGIC, BeaconListener, ControlClient

WELCOME = {
    "t": "welcome", "ver": 1, "app": "1.1.0", "device": "iPhone14,2",
    "ios": "17.5.1", "camera": "back",
    "max_front": [1280, 720, 30], "max_back": [1920, 1080, 60],
    "filter": {"look": "mono", "adjust": {"brightness": 0.1}},
}
STARTED_EXACT = {"t": "started", "ssrc_video": 305419896, "ssrc_fec": 22, "fec": False}


class FakePhone:
    """Minimal phone-side TCP server on 127.0.0.1:9923."""

    def __init__(self) -> None:
        self.received: list = []          # parsed JSON messages
        self.hello_line: bytes = b""
        self.connected = threading.Event()
        self.got_bye = threading.Event()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 9923))
        self._srv.listen(1)
        self._srv.settimeout(0.2)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._serve_conn(conn)

    def _serve_conn(self, conn: socket.socket) -> None:
        self.connected.set()
        conn.settimeout(0.2)
        buf = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                msg = json.loads(line.decode("utf-8"))
                with self._lock:
                    self.received.append(msg)
                if msg.get("t") == "hello":
                    self.hello_line = line
                    self._send(conn, WELCOME)
                elif msg.get("t") == "ping":
                    self._send(conn, {"t": "pong", "ts": msg.get("ts")})
                elif msg.get("t") == "start":
                    self._send(conn, STARTED_EXACT)
                    self._send(conn, {"t": "camera_ok", "id": "back"})
                    self._send(conn, {"t": "stats", "fps": 29.8, "kbps": 2950,
                                      "enc_ms": 9.2, "loss_pct": 0.4,
                                      "nacks": 12, "sent": 34510})
                elif msg.get("t") == "bye":
                    self.got_bye.set()
                    self._send(conn, {"t": "stopped"})
                    conn.close()
                    return
        try:
            conn.close()
        except OSError:
            pass

    @staticmethod
    def _send(conn: socket.socket, obj: dict) -> None:
        try:
            conn.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError:
            pass  # client already gone (e.g. PC disconnected first)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


@pytest.fixture()
def phone():
    srv = FakePhone()
    yield srv
    srv.stop()


def test_hello_welcome_started_and_events(phone: FakePhone):
    msgs: list = []
    states: list = []
    cc = ControlClient(on_message=msgs.append, on_state=lambda c, t: states.append((c, t)))
    try:
        cc.connect_to("127.0.0.1", 9923)
        assert wait_until(phone.connected.is_set, timeout=3.0)
        assert wait_until(lambda: phone.received and phone.received[0]["t"] == "hello",
                          timeout=3.0)
        hello = phone.received[0]
        assert hello == {"t": "hello", "name": socket.gethostname(), "ver": 1}
        assert b"audio" not in phone.hello_line  # video-only protocol: no audio anywhere

        # welcome parsed by the client
        assert wait_until(lambda: any(m.get("t") == "welcome" for m in msgs), timeout=3.0)
        assert wait_until(lambda: cc.is_connected, timeout=3.0)

        # start -> started (exactly ssrc_video + ssrc_fec + fec), camera_ok, stats
        assert cc.send_start("127.0.0.1", {"port": 9921, "w": 1280, "h": 720,
                                           "fps": 30, "kbps": 3000, "keyint": 60})
        assert wait_until(lambda: any(m.get("t") == "started" for m in msgs), timeout=3.0)
        started = next(m for m in msgs if m.get("t") == "started")
        assert set(started) == {"t", "ssrc_video", "ssrc_fec", "fec"}  # ONLY these fields
        assert started["ssrc_video"] == 305419896 and started["fec"] is False
        assert wait_until(lambda: any(m.get("t") == "camera_ok" for m in msgs), timeout=3.0)
        assert wait_until(lambda: any(m.get("t") == "stats" for m in msgs), timeout=3.0)

        # ping/pong RTT measurement
        assert cc.send_ping()
        assert wait_until(lambda: cc.rtt_ms is not None, timeout=3.0)
        assert cc.rtt_ms >= 0.0

        # every PC->phone message type goes over the wire verbatim
        cc.send_stop()
        cc.send_camera("front")
        cc.send_bitrate(2500)
        cc.send_abr(False)
        cc.send_idr()
        cc.send_torch(True)
        cc.send_zoom(2.0)
        cc.send_filter({"look": "noir"})
        cc.send_rr(0.4, 3.0, 4321, 29.7)
        assert wait_until(lambda: {m.get("t") for m in phone.received} >= {
            "stop", "camera", "bitrate", "abr", "idr", "torch", "zoom",
            "filter", "rr", "ping"}, timeout=3.0)
        rr = next(m for m in phone.received if m.get("t") == "rr")
        assert rr == {"t": "rr", "loss_pct": 0.4, "jitter_ms": 3.0,
                      "max_seq": 4321, "fps_decoded": 29.7}
        by_id = next(m for m in phone.received if m.get("t") == "start")
        assert by_id["rtp_host"] == "127.0.0.1"
        assert by_id["video"] == {"port": 9921, "w": 1280, "h": 720, "fps": 30,
                                  "kbps": 3000, "keyint": 60}
        assert "audio" not in json.dumps(by_id)

        cc.disconnect(send_bye=True)
        assert wait_until(phone.got_bye.is_set, timeout=3.0)
    finally:
        cc.shutdown()


def test_app_layer_parses_started_without_audio_fields(phone: FakePhone):
    """OmniCamApp tolerates a ``started`` that carries ONLY
    ssrc_video + ssrc_fec + fec (no audio/port-9922 fields anywhere)."""
    app = OmniCamApp()
    try:
        app._on_message(dict(WELCOME))
        assert app.get_camera_caps() == {"front": [1280, 720, 30],
                                         "back": [1920, 1080, 60]}
        assert app.get_camera() == "back"
        assert app.get_filter_state()["look"] == "mono"

        started = dict(STARTED_EXACT)
        app._on_message(started)
        assert app.streaming is True
        assert app._fec_active is False
        events = app.drain_events()  # UI event queue must not explode either
        assert isinstance(events, list)
        snap = app.get_stats_snapshot()
        assert snap["fec_active"] is False

        app._on_message({"t": "camera_ok", "id": "front"})
        assert app.get_camera() == "front"
        app._on_message({"t": "stats", "fps": 30.0, "kbps": 3000})
        assert app.get_stats_snapshot()["phone"]["t"] == "stats"
        app._on_message({"t": "pong", "ts": 1})  # unknown ts: ignored, no crash
        app._on_message({"t": "stopped"})
        assert app.streaming is False
    finally:
        app.shutdown()


def test_beacon_discovery_builds_device_list():
    devices_seen: list = []
    listener = BeaconListener(on_devices=devices_seen.append)
    try:
        listener.start()
        beacon = {
            "magic": BEACON_MAGIC, "ver": 1, "name": "QA iPhone",
            "model": "iPhone15,3", "tcp_port": 9923, "streaming": False,
            "app": "1.1.0",
        }
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            line = (json.dumps(beacon) + "\n").encode("utf-8")
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                tx.sendto(line, ("127.0.0.1", 9920))
                if wait_until(
                        lambda: any(d["ip"] == "127.0.0.1" for d in listener.snapshot()),
                        timeout=0.25):
                    break
            dev = next(d for d in listener.snapshot() if d["ip"] == "127.0.0.1")
            assert dev["name"] == "QA iPhone"
            assert dev["model"] == "iPhone15,3"
            assert dev["tcp_port"] == 9923
            assert dev["streaming"] is False
            assert dev["online"] is True
            assert devices_seen, "on_devices callback must fire on discovery"
        finally:
            tx.close()
        # a device that stops beaconing is dropped (other LAN phones may remain)
        assert wait_until(
            lambda: not any(d["ip"] == "127.0.0.1" for d in listener.snapshot()),
            timeout=12.0, interval=0.2)
    finally:
        listener.stop()


def test_manual_pin_stays_in_device_list_without_beacons():
    listener = BeaconListener()
    listener.pin("192.168.1.42")
    snap = listener.snapshot()
    assert len(snap) == 1
    assert snap[0]["ip"] == "192.168.1.42"
    assert snap[0]["manual"] is True
    listener.pin("192.168.1.42")  # idempotent
    assert len(listener.snapshot()) == 1


def test_pause_feedback_clears_rtcp_target():
    from omnicam.net import VideoReceiver
    rx = VideoReceiver()
    rx._feedback_addr = ("192.168.1.9", 9921)
    rx._media_ssrc = 1
    rx.pause_feedback()
    assert rx._feedback_addr is None
    assert rx._media_ssrc is None
