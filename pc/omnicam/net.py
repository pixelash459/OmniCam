"""OmniCam protocol networking (docs/PROTOCOL.md v1).

Plain threads + queues, no asyncio.

Components
----------
- :class:`BeaconListener`  UDP 9920 discovery beacons (magic ``OMNICAM1``).
- :class:`ControlClient`   TCP 9923 newline-delimited JSON with auto-reconnect;
                           every PC->phone message (hello/start/stop/camera/
                           bitrate/abr/idr/filter/torch/zoom/session/rr/ping/bye)
                           and parsing of every phone->PC message
                           (welcome/started/stopped/camera_ok/bitrate_ok/
                           filter_ok/torch_ok/session/stats/pong/error).
- :class:`VideoReceiver`   UDP 9921; RTP PT=96 H.264 (RFC 6184) + PT=100 FEC;
                           RFC 4585 NACK (PT=205 FMT=1) and PLI (PT=206 FMT=1)
                           feedback; frame assembly by RTP timestamp; XOR FEC
                           recovery (groups of 4).

All constants match PROTOCOL.md section 0 exactly.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import socket
import struct
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("omnicam.net")

# ---------------------------------------------------------------------------
# Constants (PROTOCOL.md section 0)
# ---------------------------------------------------------------------------

BEACON_PORT = 9920       # UDP, phone -> broadcast
VIDEO_PORT = 9921        # UDP, phone -> PC (RTP video + RTCP feedback out)
CONTROL_PORT = 9923      # TCP, PC connects -> phone listens

PT_VIDEO = 96            # RTP payload type: H.264
PT_FEC = 100             # RTP payload type: optional XOR FEC

RTCP_PT_NACK = 205       # Generic NACK, FMT=1
RTCP_PT_PLI = 206        # PLI, FMT=1

RTP_CLOCK_VIDEO = 90000  # Hz

BEACON_MAGIC = "OMNICAM1"
BEACON_OFFLINE_S = 10.0  # offline after 10 s without a beacon (laptop Wi-Fi drops broadcasts)

MAX_VIDEO_PAYLOAD = 1200  # informational (sender side)

START_CODE = b"\x00\x00\x00\x01"

DEVICE_OFFLINE_S = BEACON_OFFLINE_S


def default_filter_state() -> Dict[str, Any]:
    """Return a deep copy of the default phone filter state (PROTOCOL.md S5)."""
    return {
        "look": "none",
        "adjust": {
            "brightness": 0.0,   # -1..1
            "contrast": 0.0,     # -1..1
            "saturation": 1.0,   # 0..2
            "temperature": 0.0,  # -1..1
            "vibrance": 0.0,     # 0..1
            "gamma": 1.0,        # 0.2..3
            "sharpness": 0.0,    # 0..1
            "vignette": 0.0,     # 0..1
        },
        "beauty": 0.0,           # 0..1
        "stylize": "none",
        "stylize_amount": 0.5,   # 0..1
        "geometry": {
            "mirror": False,
            "flipV": False,
            "rotate": 0,         # 0|90|180|270
            "zoom": 1.0,
            "panX": 0.0,
            "panY": 0.0,
            "aspect": "native",
        },
        "lut": None,
        "overlay": {"text": "", "show_timecode": False},
    }


SESSION_KEYS = ("camera", "w", "h", "fps", "kbps", "abr", "torch", "zoom")


def default_session_state() -> Dict[str, Any]:
    """Return a copy of the default shared session (PROTOCOL.md S2.3)."""
    return {
        "camera": "back",
        "w": 1280,
        "h": 720,
        "fps": 30,
        "kbps": 3000,
        "abr": True,
        "torch": False,
        "zoom": 1.0,
    }


def merge_session_state(base: Optional[Dict[str, Any]], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge known session keys from ``updates`` into ``base``; absent keys stay."""
    state = default_session_state()
    if base:
        for key in SESSION_KEYS:
            if key in base:
                state[key] = copy.deepcopy(base[key])
    for key in SESSION_KEYS:
        if key not in updates:
            continue
        val = updates[key]
        try:
            if key == "camera":
                cam = str(val)
                if cam in ("front", "back"):
                    state[key] = cam
            elif key in ("w", "h", "fps", "kbps"):
                state[key] = int(val)
            elif key in ("abr", "torch"):
                state[key] = bool(val)
            elif key == "zoom":
                state[key] = max(1.0, min(8.0, float(val)))
        except (TypeError, ValueError):
            continue
    return state


def merge_filter_state(base: Optional[Dict[str, Any]], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge ``updates`` into ``base`` (shallow per section, deep for adjust/
    geometry/overlay) and return a new state dict suitable for
    ``{"t":"filter","state":...}``."""
    state = default_filter_state()
    if base:
        for key in ("look", "beauty", "stylize", "stylize_amount", "lut"):
            if key in base:
                state[key] = copy.deepcopy(base[key])
        for key in ("adjust", "geometry", "overlay"):
            if isinstance(base.get(key), dict):
                state[key].update(copy.deepcopy(base[key]))
    for key, val in updates.items():
        if key in ("adjust", "geometry", "overlay") and isinstance(val, dict):
            state[key].update(copy.deepcopy(val))
        else:
            state[key] = copy.deepcopy(val)
    return state


# ---------------------------------------------------------------------------
# RTP helpers
# ---------------------------------------------------------------------------

class RtpPacket:
    """Parsed fixed-header RTP packet (V=2, no CSRC, optional padding)."""

    __slots__ = ("marker", "pt", "seq", "ts", "ssrc", "payload")

    def __init__(self, marker: bool, pt: int, seq: int, ts: int, ssrc: int, payload: bytes) -> None:
        self.marker = marker
        self.pt = pt
        self.seq = seq & 0xFFFF
        self.ts = ts & 0xFFFFFFFF
        self.ssrc = ssrc & 0xFFFFFFFF
        self.payload = payload

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"RtpPacket(pt={self.pt} seq={self.seq} ts={self.ts} m={int(self.marker)} len={len(self.payload)})"


def parse_rtp(data: bytes) -> Optional[RtpPacket]:
    """Parse a standard 12-byte-header RTP datagram; ``None`` if malformed."""
    if len(data) < 12:
        return None
    b0 = data[0]
    if (b0 >> 6) != 2:  # version
        return None
    padding = bool(b0 & 0x20)
    cc = b0 & 0x0F
    b1 = data[1]
    marker = bool(b1 & 0x80)
    pt = b1 & 0x7F
    seq = int.from_bytes(data[2:4], "big")
    ts = int.from_bytes(data[4:8], "big")
    ssrc = int.from_bytes(data[8:12], "big")
    offset = 12 + 4 * cc
    if len(data) <= offset:
        return None
    payload = data[offset:]
    if padding:
        pad_len = payload[-1] if payload else 0
        if 0 < pad_len <= len(payload):
            payload = payload[:-pad_len]
        else:
            return None
    return RtpPacket(marker, pt, seq, ts, ssrc, payload)


def build_rtcp_feedback(fmt: int, pt: int, sender_ssrc: int, media_ssrc: int,
                        fci: bytes = b"") -> bytes:
    """Build an RFC 4585 transport-layer feedback packet.

    Layout: ``V=2 P=0 FMT | PT | length | sender SSRC | media SSRC | FCI``.
    The length field counts 32-bit words minus one (PLI -> len=2, NACK with N
    entries -> len=N+2).
    """
    words = 3 + len(fci) // 4
    return struct.pack("!BBHII", 0x80 | (fmt & 0x1F), pt, words - 1,
                       sender_ssrc & 0xFFFFFFFF, media_ssrc & 0xFFFFFFFF) + fci


def build_nack(sender_ssrc: int, media_ssrc: int, seqs: List[int]) -> bytes:
    """Build a Generic NACK (PT=205 FMT=1) with at most 16 PID/BLP entries.

    ``BLP`` bit i set means packet ``PID + 1 + i`` is also lost.  Consecutive
    lost seqs chain into a single entry, so the 16-entry cap allows up to 256
    seqs per packet; excess entries are left for the next re-NACK.
    """
    seqs = sorted(set(s & 0xFFFF for s in seqs))
    fci = bytearray()
    cur: Optional[int] = None
    blp = 0
    entries = 0
    for s in seqs:
        if cur is None:
            cur = s
            blp = 0
            continue
        d = (s - cur) & 0xFFFF
        if 1 <= d <= 16:
            blp |= 1 << (d - 1)
        else:
            if entries >= 16:
                break  # entry cap reached; the re-NACK covers the rest
            fci += struct.pack("!HH", cur, blp)
            entries += 1
            cur, blp = s, 0
    if cur is not None and entries < 16:
        fci += struct.pack("!HH", cur, blp)
    return build_rtcp_feedback(1, RTCP_PT_NACK, sender_ssrc, media_ssrc, bytes(fci))


def build_pli(sender_ssrc: int, media_ssrc: int) -> bytes:
    """Build a PLI (PT=206 FMT=1, len=2) requesting an IDR."""
    return build_rtcp_feedback(1, RTCP_PT_PLI, sender_ssrc, media_ssrc)


def depacketize_h264(payloads: List[bytes]) -> bytes:
    """Depacketize an ordered list of RFC 6184 RTP payloads into one Annex-B
    frame (start codes ``00 00 00 01``).

    Supports Single NAL Unit (types 1..23), STAP-A (type 24, 16-bit big-endian
    length prefixes) and FU-A (type 28 with S/E bits).  Malformed input is
    skipped, never raises.
    """
    nalus: List[bytes] = []
    fu_buf: Optional[bytearray] = None
    for p in payloads:
        if not p:
            continue
        nal_type = p[0] & 0x1F
        if 1 <= nal_type <= 23:
            fu_buf = None  # a complete NAL interrupts any fragmented one
            nalus.append(p)
        elif nal_type == 24:  # STAP-A
            fu_buf = None
            i = 1
            while i + 2 <= len(p):
                n = int.from_bytes(p[i:i + 2], "big")
                i += 2
                if n == 0 or i + n > len(p):
                    break  # malformed STAP-A: keep what we have
                nalus.append(p[i:i + n])
                i += n
        elif nal_type == 28:  # FU-A
            if len(p) < 2:
                continue
            fu_header = p[1]
            start = bool(fu_header & 0x80)
            end = bool(fu_header & 0x40)
            if start:
                if fu_buf is not None:
                    pass  # previous fragment sequence was incomplete: discard
                fu_buf = bytearray()
                fu_buf.append((p[0] & 0xE0) | (fu_header & 0x1F))
                fu_buf += p[2:]
                if end:
                    nalus.append(bytes(fu_buf))
                    fu_buf = None
            else:
                if fu_buf is None:
                    continue  # continuation without start: drop
                fu_buf += p[2:]
                if end:
                    nalus.append(bytes(fu_buf))
                    fu_buf = None
        else:
            # types 25..29+ (MTAP/FU-B) not used by the sender
            continue
    if not nalus:
        return b""
    return b"".join(START_CODE + n for n in nalus)


# ---------------------------------------------------------------------------
# BeaconListener (UDP 9920)
# ---------------------------------------------------------------------------

class BeaconListener:
    """Listens for OmniCam UDP discovery beacons on ``0.0.0.0:9920``.

    Beacons are single-line JSON + ``\\n`` with ``magic == "OMNICAM1"`` sent
    every second by the phone.  Devices are deduped by source IP and considered
    offline after 3.5 s without a beacon.
    """

    def __init__(self, on_devices: Optional[Callable[[List[Dict[str, Any]]], None]] = None) -> None:
        self._on_devices = on_devices
        self._devices: Dict[str, Dict[str, Any]] = {}
        self._pinned: set[str] = set()
        self._signature = ""
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Bind UDP 9920 and start the receive thread (idempotent)."""
        if self._thread is not None:
            return
        self._stop_evt.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 18)
        except OSError:
            pass
        self._sock.bind(("0.0.0.0", BEACON_PORT))
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._run, name="beacon-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Close the socket and join the receive thread."""
        self._stop_evt.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # -- access ------------------------------------------------------------
    def pin(self, ip: str) -> None:
        """Keep ``ip`` in the device list even when UDP beacons never arrive."""
        ip = ip.strip()
        if not ip:
            return
        now = time.monotonic()
        with self._lock:
            self._pinned.add(ip)
            old = self._devices.get(ip)
            if old is None:
                self._devices[ip] = {
                    "ip": ip,
                    "name": ip,
                    "model": "manual",
                    "tcp_port": CONTROL_PORT,
                    "streaming": False,
                    "app": "",
                    "last_seen": now,
                    "manual": True,
                }
            else:
                old["manual"] = True
        self._emit()

    def snapshot(self) -> List[Dict[str, Any]]:
        """Online beacons plus pinned manual IPs, sorted by name."""
        now = time.monotonic()
        out = []
        with self._lock:
            pinned = set(self._pinned)
            for dev in self._devices.values():
                ip = str(dev.get("ip", ""))
                online = now - float(dev.get("last_seen", 0.0)) <= DEVICE_OFFLINE_S
                if not online and ip not in pinned:
                    continue
                row = {k: v for k, v in dev.items() if k != "last_seen"}
                row["online"] = online
                row["manual"] = bool(dev.get("manual") or ip in pinned)
                out.append(row)
        out.sort(key=lambda d: d.get("name", "").lower())
        return out

    # -- internals ---------------------------------------------------------
    def _run(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._stop_evt.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                self._prune_offline()
                continue
            except OSError:
                break
            self._handle_beacon(data, addr[0])
        log.debug("beacon listener exited")

    def _handle_beacon(self, data: bytes, ip: str) -> None:
        """Parse one beacon datagram; malformed input is logged and dropped."""
        try:
            text = data.decode("utf-8", errors="replace").strip()
            msg = json.loads(text)
            if not isinstance(msg, dict) or msg.get("magic") != BEACON_MAGIC:
                return
        except (ValueError, UnicodeDecodeError):
            log.debug("malformed beacon from %s", ip)
            return
        dev = {
            "ip": ip,
            "name": str(msg.get("name", ip)),
            "model": str(msg.get("model", "")),
            "tcp_port": int(msg.get("tcp_port", CONTROL_PORT)),
            "streaming": bool(msg.get("streaming", False)),
            "app": str(msg.get("app", "")),
            "last_seen": time.monotonic(),
        }
        changed = False
        with self._lock:
            old = self._devices.get(ip)
            if old is None:
                changed = True
                log.info("device discovered: %s (%s) at %s", dev["name"], dev["model"], ip)
            else:
                for key in ("name", "model", "tcp_port", "streaming", "app"):
                    if old.get(key) != dev[key]:
                        changed = True
            if ip in self._pinned or (old and old.get("manual")):
                dev["manual"] = True
            self._devices[ip] = dev
        if changed:
            self._emit()

    def _prune_offline(self) -> None:
        now = time.monotonic()
        with self._lock:
            dead = [ip for ip, d in self._devices.items()
                    if now - d["last_seen"] > DEVICE_OFFLINE_S and ip not in self._pinned]
            for ip in dead:
                del self._devices[ip]
                log.info("device offline: %s", ip)
        if dead:
            self._emit()

    def _emit(self) -> None:
        snap = self.snapshot()
        sig = json.dumps(snap, sort_keys=True)
        with self._lock:
            if sig == self._signature:
                return
            self._signature = sig
        if self._on_devices is not None:
            try:
                self._on_devices(snap)
            except Exception:  # callback must never kill the thread
                log.exception("on_devices callback failed")


# ---------------------------------------------------------------------------
# ControlClient (TCP 9923)
# ---------------------------------------------------------------------------

class ControlClient:
    """TCP control channel to the phone (phone listens on 9923).

    Newline-delimited UTF-8 JSON, max 64 KiB per message.  Auto-reconnects
    every 3 s while a target address is set.  Measures RTT from
    ``ping``/``pong`` exchanges.
    """

    RECONNECT_INTERVAL_S = 3.0
    BUSY_BACKOFF_S = 15.0
    MAX_MESSAGE = 64 * 1024

    def __init__(
        self,
        on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_state: Optional[Callable[[bool, str], None]] = None,
        on_rtt: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._on_message = on_message
        self._on_state = on_state
        self._on_rtt = on_rtt

        self._target: Optional[Tuple[str, int]] = None
        self._sock: Optional[socket.socket] = None
        self._sock_lock = threading.Lock()
        self._connected = False
        self._stop_evt = threading.Event()
        self._worker: Optional[threading.Thread] = None

        self._rtt_ms: Optional[float] = None
        self._rtt_smooth: Optional[float] = None
        self._pending_pings: Dict[int, float] = {}  # ts payload -> monotonic
        self._ping_lock = threading.Lock()
        self._got_busy = False

    # -- lifecycle ---------------------------------------------------------
    def connect_to(self, ip: str, port: int = CONTROL_PORT) -> None:
        """Start (or retarget) the persistent connection to ``ip:port``."""
        self._target = (ip, port)
        if self._worker is None or not self._worker.is_alive():
            self._stop_evt.clear()
            self._worker = threading.Thread(target=self._run, name="control-client", daemon=True)
            self._worker.start()

    def disconnect(self, send_bye: bool = True) -> None:
        """Stop reconnecting and close the current connection."""
        if send_bye:
            self.send_bye()
        self._target = None
        self._stop_evt.set()
        self._close_socket()
        if self._worker is not None:
            self._worker.join(timeout=3.0)
            self._worker = None
        self._set_connected(False, "disconnected")

    def shutdown(self) -> None:
        """Final teardown at application exit."""
        self.disconnect(send_bye=True)

    @property
    def is_connected(self) -> bool:
        """True while the TCP control channel is established."""
        return self._connected

    @property
    def target_ip(self) -> Optional[str]:
        """Configured phone IP, if any."""
        return self._target[0] if self._target else None

    @property
    def rtt_ms(self) -> Optional[float]:
        """Last measured round-trip time in milliseconds (ping/pong)."""
        return self._rtt_ms

    @property
    def rtt_smooth_ms(self) -> Optional[float]:
        """Exponentially smoothed RTT in milliseconds."""
        return self._rtt_smooth

    # -- PC -> phone messages (PROTOCOL.md section 2.1) --------------------
    def send_hello(self) -> bool:
        """``hello`` with the PC hostname; must be sent right after connect."""
        return self._send({"t": "hello", "name": socket.gethostname(), "ver": 1},
                          require_connected=False)

    def send_start(self, rtp_host: str, video: Dict[str, Any]) -> bool:
        """``start``: rtp_host + video{port,w,h,fps,kbps,keyint}."""
        return self._send({"t": "start", "rtp_host": rtp_host, "video": video})

    def send_stop(self) -> bool:
        """``stop`` the current stream."""
        return self._send({"t": "stop"})

    def send_camera(self, camera_id: str) -> bool:
        """``camera`` with id ``front`` or ``back``."""
        return self._send({"t": "camera", "id": camera_id})

    def send_bitrate(self, kbps: int) -> bool:
        """``bitrate`` manual override (disables auto ABR until abr auto=true)."""
        return self._send({"t": "bitrate", "kbps": int(kbps)})

    def send_abr(self, auto: bool) -> bool:
        """``abr`` enable/disable phone-side adaptive bitrate."""
        return self._send({"t": "abr", "auto": bool(auto)})

    def send_idr(self) -> bool:
        """``idr`` force a keyframe on the next encoded frame."""
        return self._send({"t": "idr"})

    def send_filter(self, state: Dict[str, Any]) -> bool:
        """``filter`` push a full filter state (schema in PROTOCOL.md S5)."""
        return self._send({"t": "filter", "state": state})

    def send_torch(self, on: bool) -> bool:
        """``torch`` toggle the rear flashlight."""
        return self._send({"t": "torch", "on": bool(on)})

    def send_zoom(self, x: float) -> bool:
        """``zoom`` digital zoom factor 1.0..max (clamped by the phone)."""
        return self._send({"t": "zoom", "x": float(x)})

    def send_session(self, state: Dict[str, Any]) -> bool:
        """``session`` shared capture/encode state (PROTOCOL.md S2.3)."""
        return self._send({"t": "session", "state": state})

    def send_rr(self, loss_pct: float, jitter_ms: float, max_seq: int, fps_decoded: float) -> bool:
        """``rr`` receiver report every 500 ms while streaming (drives ABR)."""
        return self._send({"t": "rr", "loss_pct": round(float(loss_pct), 2),
                           "jitter_ms": round(float(jitter_ms), 2),
                           "max_seq": int(max_seq), "fps_decoded": round(float(fps_decoded), 2)})

    def send_ping(self) -> bool:
        """``ping`` with ms-epoch payload; the phone echoes it unchanged."""
        ts = int(time.time() * 1000)
        with self._ping_lock:
            self._pending_pings[ts] = time.monotonic()
            # prune stale entries (>= 10 s old)
            now = time.monotonic()
            for old in [k for k, t0 in self._pending_pings.items() if now - t0 > 10.0]:
                del self._pending_pings[old]
        return self._send({"t": "ping", "ts": ts})

    def send_bye(self) -> bool:
        """``bye`` politely release the phone's single-client slot."""
        return self._send({"t": "bye"})

    # -- internals ---------------------------------------------------------
    def _send(self, obj: Dict[str, Any], *, require_connected: bool = True) -> bool:
        """Serialize + send one JSON line; returns False when not connected."""
        sock = self._sock
        if sock is None or (require_connected and not self._connected):
            return False
        try:
            data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
            if len(data) > self.MAX_MESSAGE:
                log.error("control message too large (%d B), dropped", len(data))
                return False
            with self._sock_lock:
                sock.sendall(data)
            return True
        except OSError as exc:
            log.debug("control send failed: %s", exc)
            return False

    def _run(self) -> None:
        """Connect/recv loop. Connected is latched only after ``welcome``.

        A second PC (or this laptop while Beast still holds the slot) used to
        flip the UI connected→disconnected on every attempt: TCP succeeded,
        we advertised connected, the phone replied ``busy`` and closed.
        """
        while not self._stop_evt.is_set():
            target = self._target
            if target is None:
                self._stop_evt.wait(0.2)
                continue
            ip, port = target
            try:
                sock = socket.create_connection((ip, port), timeout=3.0)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.settimeout(None)
            except OSError as exc:
                log.debug("control connect to %s:%d failed: %s", ip, port, exc)
                self._set_connected(False, f"cannot reach {ip}:{port}")
                if self._stop_evt.wait(self.RECONNECT_INTERVAL_S):
                    break
                continue
            with self._sock_lock:
                self._sock = sock
            self._got_busy = False
            self.send_hello()
            try:
                self._recv_loop(sock)
            finally:
                self._close_socket(sock)
                wait = self.BUSY_BACKOFF_S if self._got_busy else self.RECONNECT_INTERVAL_S
                if self._got_busy:
                    self._set_connected(False, "phone busy — another PC is connected")
                elif self._connected:
                    self._set_connected(False, "disconnected")
                if self._stop_evt.wait(wait):
                    break
        log.debug("control client exited")

    def _recv_loop(self, sock: socket.socket) -> None:
        """Receive and dispatch newline-delimited JSON messages."""
        buf = b""
        while not self._stop_evt.is_set():
            try:
                chunk = sock.recv(8192)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                if len(line) > self.MAX_MESSAGE:
                    log.warning("oversized control message dropped (%d B)", len(line))
                    continue
                self._dispatch(line)

    def _dispatch(self, line: bytes) -> None:
        """Parse one JSON control message and route it."""
        try:
            msg = json.loads(line.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            log.warning("malformed control message: %r", line[:120])
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
            log.warning("control message without 't': %r", msg)
            return
        if msg["t"] == "pong":
            self._handle_pong(msg)
        if msg["t"] == "welcome" and self._target:
            self._set_connected(True, f"connected to {self._target[0]}")
        if msg["t"] == "error" and str(msg.get("code", "")) == "busy":
            self._got_busy = True
            log.info("phone busy (another control client holds the slot)")
            self._close_socket()
        if self._on_message is not None:
            try:
                self._on_message(msg)
            except Exception:
                log.exception("on_message callback failed for %r", msg.get("t"))

    def _handle_pong(self, msg: Dict[str, Any]) -> None:
        """Measure RTT from an echoed ping timestamp."""
        ts = msg.get("ts")
        with self._ping_lock:
            t0 = self._pending_pings.pop(ts, None) if isinstance(ts, int) else None
        if t0 is None:
            return
        rtt = (time.monotonic() - t0) * 1000.0
        self._rtt_ms = rtt
        self._rtt_smooth = rtt if self._rtt_smooth is None else (0.8 * self._rtt_smooth + 0.2 * rtt)
        if self._on_rtt is not None:
            try:
                self._on_rtt(rtt)
            except Exception:
                log.exception("on_rtt callback failed")

    def _close_socket(self, sock: Optional[socket.socket] = None) -> None:
        with self._sock_lock:
            s = sock if sock is not None else self._sock
            if sock is None:
                self._sock = None
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    def _set_connected(self, state: bool, text: str) -> None:
        self._connected = state
        if self._on_state is not None:
            try:
                self._on_state(state, text)
            except Exception:
                log.exception("on_state callback failed")


# ---------------------------------------------------------------------------
# VideoReceiver (UDP 9921)
# ---------------------------------------------------------------------------

class _FrameBuf:
    """Assembly state for one video frame (all packets share an RTP ts)."""

    __slots__ = ("ts", "first_seq", "last_seq", "marker", "packets", "recovered",
                 "first_recv_mono", "first_recv_epoch_ms")

    def __init__(self, seq: int, ts: int, recv_mono: float, recv_epoch_ms: float) -> None:
        self.ts = ts
        self.first_seq = seq
        self.last_seq = seq
        self.marker = False
        self.packets: Dict[int, Tuple[bytes, bool]] = {}  # seq -> (payload, recovered)
        self.recovered = 0
        self.first_recv_mono = recv_mono
        self.first_recv_epoch_ms = recv_epoch_ms

    @property
    def expected(self) -> int:
        """Packet count first_seq..last_seq inclusive."""
        return ((self.last_seq - self.first_seq) & 0xFFFF) + 1

    @property
    def complete(self) -> bool:
        """True when the marker packet is present and every seq in range arrived."""
        return self.marker and len(self.packets) == self.expected


class VideoReceiver:
    """RTP H.264 video receiver with NACK/PLI feedback and FEC recovery.

    Implements PROTOCOL.md sections 3.1-3.5:
    - reorder buffer keyed by seq, NACK after ~8 ms, 15 ms per-seq re-NACK
      throttle, at most 16 entries per NACK packet;
    - PLI on start, on sustained loss > 10 % and on decoder stall > 500 ms
      (via :meth:`force_pli`);
    - frame assembly by RTP timestamp with the marker bit, incomplete frames
      older than 3 frame periods are dropped;
    - optional PT=100 XOR FEC recovery (groups of ``count`` packets, exactly
      one missing -> reconstruct, then depacketize).
    """

    REORDER_WAIT_S = 0.008      # gap must persist ~8 ms before a NACK
    RENACK_INTERVAL_S = 0.015   # never re-NACK the same seq within 15 ms
    MAX_NACK_ENTRIES = 16
    PLI_MIN_INTERVAL_S = 0.3    # rate limit for forced PLIs
    PLI_LOSS_INTERVAL_S = 1.0   # sustained-loss PLI cadence
    FEC_WINDOW = 2048           # dedupe / recent-packet map size

    def __init__(self, fps: float = 30.0) -> None:
        self._fps = fps
        self._sender_ssrc = random.getrandbits(32)
        self._media_ssrc: Optional[int] = None
        self._feedback_addr: Optional[Tuple[str, int]] = None

        self._sock: Optional[socket.socket] = None
        self._rxq: "deque[Tuple[RtpPacket, float]]" = deque()
        self._rxq_lock = threading.Condition()
        self._stop_evt = threading.Event()
        self._recv_thread: Optional[threading.Thread] = None
        self._proc_thread: Optional[threading.Thread] = None

        self._on_frame: Optional[Callable[[bytes, Dict[str, Any]], None]] = None
        self._on_pli: Optional[Callable[[str], None]] = None

        # reorder / assembly state (owned by the process thread)
        self._next_seq: Optional[int] = None
        self._buffer: Dict[int, Tuple[bytes, int, bool, float]] = {}  # seq -> (payload, ts, marker, recv_mono)
        self._gap_first: Dict[int, float] = {}   # missing seq -> first seen monotonic
        self._last_nack: Dict[int, float] = {}   # missing seq -> last nack monotonic
        self._frames: Dict[int, _FrameBuf] = {}  # RTP ts -> frame
        self._recent: Dict[int, Tuple[bytes, int, bool]] = {}  # seq -> (payload, ts, marker) incl. recovered
        self._fec_groups: Dict[int, Dict[str, Any]] = {}       # base_seq -> group
        self._last_frame_period_hint: Optional[float] = None
        self._headless_ts: set = set()                # RTP ts of frames that lost their start

        # feedback + stats (guarded by _stat_lock)
        self._stat_lock = threading.Lock()
        self._nacks_sent = 0
        self._plis_sent = 0
        self._drops = 0
        self._late_frames = 0
        self._fec_recovered = 0
        self._dup_packets = 0
        self._loss_win: deque[Tuple[float, int, int]] = deque()  # (mono, expected, lost)
        self._last_pli_mono = 0.0
        self._jitter_ms = 0.0
        self._max_seq = 0
        self._bytes_win: deque[Tuple[float, int]] = deque()
        self._max_seq_lock = threading.Lock()
        self._last_arrival: Optional[float] = None
        self._last_ts: Optional[int] = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Bind UDP 9921 and start receive + processing threads (idempotent)."""
        if self._recv_thread is not None:
            return
        self._stop_evt.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Exclusive bind: SO_REUSEADDR on Windows lets a second OmniCam (or Beast
        # leftover) steal unicast RTP and looks like the stream is strobing.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        except OSError:
            pass
        self._sock.bind(("0.0.0.0", VIDEO_PORT))
        self._recv_thread = threading.Thread(target=self._recv_loop, name="video-recv", daemon=True)
        self._proc_thread = threading.Thread(target=self._process_loop, name="video-proc", daemon=True)
        self._recv_thread.start()
        self._proc_thread.start()

    def stop(self) -> None:
        """Close the socket and join both threads."""
        self._stop_evt.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        with self._rxq_lock:
            self._rxq.clear()
            self._rxq_lock.notify_all()
        for t in (self._recv_thread, self._proc_thread):
            if t is not None:
                t.join(timeout=2.0)
        self._recv_thread = self._proc_thread = None

    # -- configuration -----------------------------------------------------
    def set_session(self, media_ssrc: int, phone_ip: str, fps: float) -> None:
        """Apply ``started`` info: media SSRC for feedback, feedback target and
        the negotiated fps; resets all sequence/frame state."""
        self._media_ssrc = media_ssrc & 0xFFFFFFFF
        self._feedback_addr = (phone_ip, VIDEO_PORT)
        self._fps = max(1.0, float(fps))
        self.reset()

    def reset(self) -> None:
        """Clear sequence/frame/FEC state (new stream or stream restart)."""
        self._next_seq = None
        self._buffer.clear()
        self._gap_first.clear()
        self._last_nack.clear()
        self._frames.clear()
        self._recent.clear()
        self._fec_groups.clear()
        self._loss_win.clear()
        self._last_arrival = None
        self._last_ts = None
        self._headless_ts.clear()
        with self._stat_lock:
            self._loss_win.clear()

    def pause_feedback(self) -> None:
        """Stop NACK/PLI toward the phone (call on Stop Stream; keep UDP bound)."""
        self._feedback_addr = None
        self._media_ssrc = None
        self.reset()

    def set_frame_callback(self, cb: Callable[[bytes, Dict[str, Any]], None]) -> None:
        """Register ``cb(annexb_bytes, meta)`` called per complete frame."""
        self._on_frame = cb

    def set_pli_callback(self, cb: Callable[[str], None]) -> None:
        """Register ``cb(reason)`` called whenever a PLI is transmitted."""
        self._on_pli = cb

    def set_fps(self, fps: float) -> None:
        """Update the negotiated fps (used for frame-timeout windows)."""
        self._fps = max(1.0, float(fps))

    def force_pli(self, reason: str = "stall") -> None:
        """Send a rate-limited PLI (e.g. decode stall > 500 ms)."""
        now = time.monotonic()
        with self._stat_lock:
            if now - self._last_pli_mono < self.PLI_MIN_INTERVAL_S:
                return
            self._last_pli_mono = now
        self._send_rtcp(build_pli(self._sender_ssrc, self._media_ssrc or 0))
        log.info("PLI sent (%s)", reason)

    # -- stats -------------------------------------------------------------
    def get_loss_pct(self) -> float:
        """Rolling ~2 s packet loss percentage (0.0 when no data)."""
        now = time.monotonic()
        expected = lost = 0
        with self._stat_lock:
            while self._loss_win and now - self._loss_win[0][0] > 2.0:
                self._loss_win.popleft()
            for _, e, l in self._loss_win:
                expected += e
                lost += l
        if expected <= 0:
            return 0.0
        return min(100.0, 100.0 * lost / expected)

    def get_stats(self) -> Dict[str, Any]:
        """Snapshot of receiver counters for the UI/rr reports."""
        now = time.monotonic()
        with self._stat_lock:
            while self._bytes_win and now - self._bytes_win[0][0] > 2.0:
                self._bytes_win.popleft()
            kbps = sum(b for _, b in self._bytes_win) * 8.0 / 2000.0
            return {
                "nacks_sent": self._nacks_sent,
                "plis_sent": self._plis_sent,
                "drops": self._drops,
                "late_frames": self._late_frames,
                "fec_recovered": self._fec_recovered,
                "dup_packets": self._dup_packets,
                "jitter_ms": round(self._jitter_ms, 2),
                "kbps_measured": round(kbps, 1),
            }

    def get_max_seq(self) -> int:
        """Highest video RTP sequence number received (for ``rr``)."""
        with self._max_seq_lock:
            return self._max_seq

    # -- receive thread ----------------------------------------------------
    def _recv_loop(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._stop_evt.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except (socket.timeout, OSError):
                break
            pkt = parse_rtp(data)
            if pkt is None:
                continue
            if pkt.pt not in (PT_VIDEO, PT_FEC):
                continue
            with self._rxq_lock:
                if len(self._rxq) < 4096:
                    self._rxq.append((pkt, time.monotonic()))
                    self._rxq_lock.notify()
                # else: overflow -> drop (stats unchanged; loss will trigger PLI)
        log.debug("video recv loop exited")

    # -- processing thread -------------------------------------------------
    def _process_loop(self) -> None:
        """Consume packets in order, run NACK/FEC/timeout maintenance."""
        while not self._stop_evt.is_set():
            item = None
            with self._rxq_lock:
                if not self._rxq:
                    self._rxq_lock.wait(0.004)
                if self._rxq:
                    item = self._rxq.popleft()
            now = time.monotonic()
            if item is not None:
                pkt, recv_mono = item
                try:
                    if pkt.pt == PT_VIDEO:
                        self._handle_media(pkt, recv_mono, now)
                    else:
                        self._handle_fec(pkt, now)
                except Exception:
                    log.exception("video packet processing error (ignored)")
            try:
                self._maintenance(now)
            except Exception:
                log.exception("video maintenance error (ignored)")
        log.debug("video process loop exited")

    # seq arithmetic helper
    @staticmethod
    def _seq_diff(a: int, b: int) -> int:
        """Signed distance a-b in seq16 space."""
        d = (a - b) & 0xFFFF
        return d - 0x10000 if d >= 0x8000 else d

    @staticmethod
    def _seq_span(a: int, b: int) -> List[int]:
        """The seqs [a, b) in seq16 order (wrap-safe; empty when a == b)."""
        d = (b - a) & 0xFFFF
        return [(a + i) & 0xFFFF for i in range(d)]

    def _handle_media(self, pkt: RtpPacket, recv_mono: float, now: float) -> None:
        """Classify/dedupe one PT=96 packet and feed the reorder buffer."""
        seq, ts, marker, payload = pkt.seq, pkt.ts, pkt.marker, pkt.payload
        with self._max_seq_lock:
            if seq > self._max_seq or self._max_seq - seq > 0x8000:
                self._max_seq = seq
        with self._stat_lock:
            self._bytes_win.append((now, len(payload) + 12))
            # RFC 3550-style arrival jitter estimate (ms), wrap/reset tolerant
            if self._last_arrival is not None and self._last_ts is not None:
                ts_delta = ((pkt.ts - self._last_ts) & 0xFFFFFFFF) / float(RTP_CLOCK_VIDEO)
                d = abs((now - self._last_arrival) - ts_delta)
                if d < 1.0:  # ignore reorder/wrap outliers
                    self._jitter_ms += (d * 1000.0 - self._jitter_ms) / 16.0
            self._last_arrival = now
            self._last_ts = pkt.ts

        if seq in self._recent or seq in self._buffer:
            with self._stat_lock:
                self._dup_packets += 1
            return  # retransmission dedupe

        if self._next_seq is None:
            self._next_seq = seq

        diff = self._seq_diff(seq, self._next_seq)
        if diff > 2048:
            # huge jump: sender restarted its sequence space -> resync
            log.warning("video seq jumped by %d, resyncing at %d", diff, seq)
            self._gap_first.clear()
            self._last_nack.clear()
            self._next_seq = seq
            diff = 0
        if diff >= 0:
            entry = (payload, ts, marker, recv_mono)
            if diff == 0:
                self._deliver(seq, entry, now)
                self._drain_buffer(now)
            else:
                self._buffer[seq] = entry
                # record gap members next_seq..seq-1 (wrap-safe)
                for miss in self._seq_span(self._next_seq, seq):
                    if miss not in self._gap_first and miss not in self._buffer:
                        self._gap_first[miss] = now
        else:
            # late retransmission: may still complete a pending frame
            frame = self._frames.get(ts)
            if frame is not None:
                d_first = self._seq_diff(seq, frame.first_seq)
                d_last = self._seq_diff(frame.last_seq, seq)
                if 0 <= d_first and 0 <= d_last and seq not in frame.packets:
                    frame.packets[seq] = (payload, False)
                    self._try_emit(frame, now)
            self._prune_recent()

    def _deliver(self, seq: int, entry: Tuple[bytes, int, bool, float], now: float) -> None:
        """Deliver the in-order packet into frame assembly."""
        payload, ts, marker, recv_mono = entry
        self._recent[seq] = (payload, ts, marker)
        if len(self._recent) > self.FEC_WINDOW:
            self._prune_recent()
        self._gap_first.pop(seq, None)
        self._next_seq = (seq + 1) & 0xFFFF
        if ts in self._headless_ts:
            return  # this frame lost its leading packets: never assembled
        epoch_ms = time.time() * 1000.0
        frame = self._frames.get(ts)
        if frame is None:
            frame = _FrameBuf(seq, ts, recv_mono, epoch_ms)
            self._frames[ts] = frame
        if seq not in frame.packets:
            frame.packets[seq] = (payload, False)
        if self._seq_diff(seq, frame.first_seq) < 0:
            frame.first_seq = seq
        if self._seq_diff(seq, frame.last_seq) > 0:
            frame.last_seq = seq
        if marker:
            frame.marker = True
        self._try_emit(frame, now)

    def _drain_buffer(self, now: float) -> None:
        """Deliver consecutive buffered packets after the in-order one."""
        while self._next_seq in self._buffer:
            entry = self._buffer.pop(self._next_seq)
            self._deliver(self._next_seq, entry, now)

    def _try_emit(self, frame: _FrameBuf, now: float) -> None:
        """Emit the frame when complete; count loss; feed the consumer."""
        expected = frame.expected
        received = len(frame.packets)
        if frame.marker and received >= expected:
            self._frames.pop(frame.ts, None)
            payloads = [frame.packets[(frame.first_seq + i) & 0xFFFF][0]
                        for i in range(expected)
                        if (frame.first_seq + i) & 0xFFFF in frame.packets]
            annexb = depacketize_h264(payloads)
            loss = expected - received
            with self._stat_lock:
                self._loss_win.append((now, expected, loss))
            late = (now - frame.first_recv_mono) > (3.0 / self._fps)
            if late:
                with self._stat_lock:
                    self._late_frames += 1
            if annexb and self._on_frame is not None:
                meta = {
                    "rtp_ts": frame.ts,
                    "first_seq": frame.first_seq,
                    "last_seq": frame.last_seq,
                    "received": received,
                    "expected": expected,
                    "fec_recovered": frame.recovered,
                    "late": late,
                    "recv_epoch_ms": frame.first_recv_epoch_ms,
                }
                try:
                    self._on_frame(annexb, meta)
                except Exception:
                    log.exception("on_frame callback failed")
        elif len(self._frames) > 64:
            self._drop_stale_frames(now, force=True)

    def _drop_stale_frames(self, now: float, force: bool = False) -> None:
        """Drop incomplete frames older than 3 frame periods."""
        period = self._last_frame_period_hint or (1.0 / self._fps)
        stale_ts = []
        for ts, frame in self._frames.items():
            age = now - frame.first_recv_mono
            if age > 3.0 * period or force:
                stale_ts.append(ts)
        for ts in stale_ts:
            frame = self._frames.pop(ts)
            expected = frame.expected
            received = len(frame.packets)
            # a frame without its marker packet is missing at least its tail
            lost = expected - received + (0 if frame.marker else 1)
            with self._stat_lock:
                self._drops += 1
                self._loss_win.append((now, expected, max(lost, 1)))
            log.debug("dropped incomplete frame ts=%u (%d/%d pkts)", ts, received, expected)

    def _maintenance(self, now: float) -> None:
        """NACK generation, gap give-up, stale frames, FEC, sustained-loss PLI."""
        period = 1.0 / self._fps
        self._last_frame_period_hint = period

        # 1. give up on gaps older than 3 frame periods
        giveup = 3.0 * period
        expired = [s for s, t0 in self._gap_first.items() if now - t0 > giveup]
        if expired:
            for s in expired:
                del self._gap_first[s]
                self._last_nack.pop(s, None)
            self._advance_next_seq(now)

        # 2. NACK gaps after the reorder wait
        due: List[int] = []
        for s, t0 in self._gap_first.items():
            if now - t0 < self.REORDER_WAIT_S:
                continue
            if now - self._last_nack.get(s, 0.0) < self.RENACK_INTERVAL_S:
                continue
            due.append(s)
        if due and self._feedback_addr is not None:
            due.sort(key=lambda s: self._seq_diff(s, self._next_seq if self._next_seq is not None else 0))
            chunk = due[:self.MAX_NACK_ENTRIES]
            self._send_rtcp(build_nack(self._sender_ssrc, self._media_ssrc or 0, chunk))
            stamp = time.monotonic()
            for s in chunk:
                self._last_nack[s] = stamp
            with self._stat_lock:
                self._nacks_sent += 1

        # 3. drop incomplete frames past their deadline
        if self._frames:
            self._drop_stale_frames(now)

        # 4. FEC recovery attempts
        if self._fec_groups:
            self._fec_try_recover(now)

        # 5. sustained loss > 10 % -> PLI (at most once per second)
        if self.get_loss_pct() > 10.0:
            with self._stat_lock:
                last = self._last_pli_mono
            if now - last > self.PLI_LOSS_INTERVAL_S:
                self.force_pli("sustained loss")

    def _advance_next_seq(self, now: float) -> None:
        """Skip permanently-lost seqs so the reorder pointer moves forward."""
        if self._next_seq is None or not self._buffer:
            return
        keys = sorted(self._buffer.keys(), key=lambda s: self._seq_diff(s, self._next_seq))
        nxt = keys[0]
        lost = self._seq_diff(nxt, self._next_seq)
        if lost > 0:
            lost_seqs = self._seq_span(self._next_seq, nxt)
            for miss in lost_seqs:
                self._gap_first.pop(miss, None)
                self._last_nack.pop(miss, None)
            with self._stat_lock:
                self._loss_win.append((now, lost, lost))
            log.debug("gave up on %d lost packet(s) before seq %d", lost, nxt)
            # The frame starting at ``nxt`` is "headless" when the lost seq
            # immediately before it (nxt-1) does not simply continue a known
            # open frame (marker still pending, last_seq == nxt-2): otherwise
            # nxt-1 belonged to the same never-completed frame as nxt and this
            # frame must never be emitted (only its loss is reported).
            continues = any(
                not f.marker and self._seq_diff((nxt - 1) & 0xFFFF, f.last_seq) == 1
                for f in self._frames.values())
            headless = not continues
            entry = self._buffer.pop(nxt)
            ts = entry[1]
            if headless:
                # poison the ts before delivery so no packet of this frame
                # (nxt included) is ever assembled into a frame
                self._headless_ts.add(ts)
                if len(self._headless_ts) > 512:
                    self._headless_ts.clear()
                with self._stat_lock:
                    self._drops += 1
                log.debug("dropped headless frame ts=%u (start seq lost)", ts)
            self._next_seq = nxt
            self._deliver(nxt, entry, now)
            self._drain_buffer(now)

    # -- FEC (PROTOCOL.md section 3.4) -------------------------------------
    def _handle_fec(self, pkt: RtpPacket, now: float) -> None:
        """Parse a PT=100 FEC packet covering a group of video packets."""
        p = pkt.payload
        if len(p) < 4:
            return
        base_seq = int.from_bytes(p[0:2], "big")
        count = p[2]
        # p[3] = flags (sender-defined, unused)
        xor_payload = p[4:]
        if not 2 <= count <= 8:
            return
        self._fec_groups[base_seq] = {
            "count": count,
            "xor": xor_payload,
            "ts": pkt.ts,
            "arrival": now,
        }
        # keep the group map bounded
        if len(self._fec_groups) > 256:
            oldest = sorted(self._fec_groups, key=lambda b: self._fec_groups[b]["arrival"])
            for b in oldest[:128]:
                del self._fec_groups[b]
        self._fec_try_recover(now)

    def _fec_try_recover(self, now: float) -> None:
        """XOR-reconstruct groups with exactly one missing packet.

        Recovery is deterministic when the FEC packet and all-but-one covered
        payloads are known, so it runs immediately; a genuine retransmission
        arriving later is absorbed by the seq dedupe in ``_handle_media``.
        Waiting for the reorder/give-up windows here would be too late: the
        pending frame is dropped as stale at the same deadline.
        """
        for base_seq in list(self._fec_groups.keys()):
            grp = self._fec_groups[base_seq]
            covered = [(base_seq + i) & 0xFFFF for i in range(grp["count"])]
            known = [s for s in covered if s in self._recent or s in self._buffer]
            missing = [s for s in covered if s not in known]
            if not missing:
                del self._fec_groups[base_seq]  # group fully resolved
                continue
            if len(missing) != 1 or len(known) != len(covered) - 1:
                continue  # two or more missing: XOR cannot reconstruct
            self._fec_reconstruct(base_seq, grp, missing[0], now)

    def _fec_reconstruct(self, base_seq: int, grp: Dict[str, Any], miss: int, now: float) -> None:
        """Rebuild one missing payload from the FEC XOR and its 3 neighbours."""
        count = grp["count"]
        covered = [(base_seq + i) & 0xFFFF for i in range(count)]

        def payload_of(s: int) -> Optional[bytes]:
            ent = self._recent.get(s)
            if ent is not None:
                return ent[0]
            ent = self._buffer.get(s)
            if ent is not None:
                return ent[0]
            return None

        known = [(s, payload_of(s)) for s in covered if s != miss]
        if any(p is None for _, p in known):
            return
        max_len = max([len(grp["xor"])] + [len(p) for _, p in known])  # type: ignore[arg-type]
        acc = bytearray(grp["xor"].ljust(max_len, b"\x00"))
        for _, p in known:
            for i, byte in enumerate(p):
                acc[i] ^= byte
        # The sender zero-pads every covered payload to the group maximum
        # before XOR (PROTOCOL.md 3.4), so the reconstruction carries that
        # padding on the right.  The wire format has no per-packet length
        # field, so trim the pad to recover the original datagram size
        # (H.264 payloads effectively never end in 0x00 bytes).
        payload = bytes(acc).rstrip(b"\x00")

        # RTP ts / marker inference for the recovered packet
        prev = self._recent.get((miss - 1) & 0xFFFF) or self._buffer.get((miss - 1) & 0xFFFF)
        nxt = self._recent.get((miss + 1) & 0xFFFF) or self._buffer.get((miss + 1) & 0xFFFF)
        prev_ts = prev[1] if prev else None
        nxt_ts = nxt[1] if nxt else None
        marker = False
        if miss == covered[-1]:
            # FEC RTP ts == ts of the last covered packet
            ts = grp["ts"]
            if nxt_ts is not None and nxt_ts != ts:
                marker = True  # frame boundary right after this packet
        elif prev_ts is not None and nxt_ts is not None and prev_ts != nxt_ts:
            # frame boundary between prev and miss: the recovered packet is
            # either the prev frame's last (marker) packet or the next frame's
            # first one.  If the prev frame is still assembling, only this
            # packet can complete it -> it is that frame's marker packet;
            # otherwise the prev frame already emitted -> this packet starts
            # the next frame.
            pframe = self._frames.get(prev_ts)
            if pframe is not None and not pframe.complete:
                ts, marker = prev_ts, True
            else:
                ts, marker = nxt_ts, False
        elif prev_ts is not None:
            ts = prev_ts
        elif nxt_ts is not None:
            ts = nxt_ts
        else:
            ts = grp["ts"]

        entry = (payload, ts, marker, now)
        self._recent[miss] = (payload, ts, marker)
        with self._stat_lock:
            self._fec_recovered += 1
        self._fec_groups.pop(base_seq, None)
        log.debug("FEC recovered seq=%d (base=%d)", miss, base_seq)

        if self._next_seq is None:
            self._next_seq = miss
        diff = self._seq_diff(miss, self._next_seq)
        if diff >= 0:
            if diff == 0:
                self._deliver(miss, entry, now)
                self._drain_buffer(now)
            else:
                self._buffer[miss] = entry
                for m2 in range(self._next_seq, miss):
                    self._gap_first.setdefault(m2, now)
        else:
            frame = self._frames.get(ts)
            if frame is not None and miss not in frame.packets:
                frame.packets[miss] = (payload, True)
                frame.recovered += 1
                self._try_emit(frame, now)

    def _prune_recent(self) -> None:
        """Bound the recent-packet map used for dedupe and FEC."""
        if len(self._recent) > self.FEC_WINDOW:
            for s in list(self._recent.keys())[:len(self._recent) - self.FEC_WINDOW]:
                del self._recent[s]

    def _send_rtcp(self, data: bytes) -> None:
        """Send an RTCP feedback datagram to the phone's RTP port."""
        addr = self._feedback_addr
        sock = self._sock
        if addr is None or sock is None:
            return
        try:
            sock.sendto(data, addr)
            # RTCP packets carry the payload type in the full second byte
            # (no marker bit, unlike RTP).
            if data[1] == RTCP_PT_PLI:
                with self._stat_lock:
                    self._plis_sent += 1
                if self._on_pli is not None:
                    self._on_pli("pli")
        except OSError as exc:
            log.debug("rtcp send failed: %s", exc)
