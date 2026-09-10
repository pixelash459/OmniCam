"""OmniCam PC orchestration: wires control, receivers, decoders and outputs.

Pure Python (no Qt) so the UI layer stays thin: network callbacks run on
worker threads and publish UI events onto a thread-safe deque that the UI
drains on a timer.  Owns the decode threads and the virtual-camera feed, and
tears everything down on shutdown.
"""

from __future__ import annotations

import copy
import logging
import queue
import socket
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from omnicam import __version__
from omnicam.net import (
    VIDEO_PORT,
    BeaconListener,
    ControlClient,
    VideoReceiver,
    default_filter_state,
    merge_filter_state,
)
from omnicam.stats import Stats
from omnicam.virtualcam_out import VirtualCamOut

try:  # av (PyAV) is only mandatory once a stream starts
    from omnicam.decoder import VideoDecoder
    _AV_ERROR: Optional[BaseException] = None
except BaseException as exc:  # pragma: no cover - depends on environment
    VideoDecoder = None  # type: ignore[assignment]
    _AV_ERROR = exc

log = logging.getLogger("omnicam.app")


def get_local_ip(target: str) -> str:
    """Best-effort local LAN IP used to reach ``target`` (for ``rtp_host``).

    UDP-connect trick: ``connect()`` a UDP socket to the phone and read the
    chosen local address back with ``getsockname()`` — the OS then picks the
    interface on the actual route to the phone, which stays correct on
    multi-homed hosts (VPN/Tailscale/RustDesk, WSL/Docker/Hyper-V adapters,
    multiple NICs).  Falls back to ``127.0.0.1`` if the trick fails.

    Note: the phone treats ``rtp_host`` as advisory only and prefers the
    control-connection peer address (PROTOCOL.md section 2.1).
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        ip = s.getsockname()[0]
        if ip and ip != "0.0.0.0":
            return ip
    except OSError:
        pass
    finally:
        s.close()
    return "127.0.0.1"


def apply_local_adjust(frame: np.ndarray, p: Dict[str, Any]) -> np.ndarray:
    """Apply PC-local (CPU) adjustments to a BGR frame before preview and
    virtual-cam output.  These are NOT part of the phone filter state.

    ``p`` keys: brightness (-100..100), contrast (-100..100),
    saturation (0..200), mirror, flipV (bool), rotate (0|90|180|270).
    """
    brightness = p.get("brightness", 0)
    contrast = p.get("contrast", 0)
    saturation = p.get("saturation", 100)
    out: np.ndarray = frame
    if brightness or contrast or saturation != 100:
        a = out.astype(np.float32)
        if contrast:
            a = (a - 128.0) * (1.0 + contrast / 100.0) + 128.0
        if brightness:
            a = a + brightness * 1.27
        if saturation != 100:
            gray = a.mean(axis=2, keepdims=True)
            a = gray + (a - gray) * (saturation / 100.0)
        out = np.clip(a, 0.0, 255.0).astype(np.uint8)
    if p.get("flipV"):
        out = out[::-1]
    if p.get("mirror"):
        out = out[:, ::-1]
    rotate = int(p.get("rotate", 0)) // 90
    if rotate % 4:
        out = np.rot90(out, k=rotate % 4)
    return np.ascontiguousarray(out)


class OmniCamApp:
    """Headless controller behind the UI; one instance per process."""

    def __init__(self) -> None:
        self.stats = Stats()
        self.control = ControlClient(on_message=self._on_message, on_state=self._on_state,
                                     on_rtt=self._on_rtt)
        self.beacons = BeaconListener()
        self.video_rx = VideoReceiver()
        self.vcam = VirtualCamOut()

        self._events: Deque[Tuple[str, Dict[str, Any]]] = deque()
        self._events_lock = threading.Lock()

        self._stop_evt = threading.Event()
        self._threads: List[threading.Thread] = []

        self._vq: "queue.Queue[Tuple[bytes, Dict[str, Any]]]" = queue.Queue(maxsize=4)

        self._vdec: Optional[Any] = None   # VideoDecoder
        self._streaming = False
        self._stream_lock = threading.Lock()
        self._stream_cfg: Dict[str, int] = {"w": 1280, "h": 720, "fps": 30, "kbps": 3000}
        self._fec_active = False

        self._filter_state: Dict[str, Any] = default_filter_state()
        self._camera = "back"
        self._conn_text = "disconnected"
        self._camera_caps: Dict[str, List[int]] = {}
        self._rtp_host: Optional[str] = None   # media IP advertised in start (advisory)

        self._local_lock = threading.Lock()
        self._local_adjust: Dict[str, Any] = {"brightness": 0, "contrast": 0, "saturation": 100,
                                              "mirror": False, "flipV": False, "rotate": 0}

        self._preview_lock = threading.Lock()
        self._preview: Optional[np.ndarray] = None
        self._preview_seq = 0
        self._last_video_rx_mono = 0.0
        self._last_decode_mono = 0.0

        self.video_rx.set_frame_callback(self._on_video_frame)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start discovery, receivers and the control timer (idempotent)."""
        if self._threads:
            return
        self.beacons.start()
        self.video_rx.start()
        self._stop_evt.clear()
        for name, target in (("control-timer", self._timer_loop),
                             ("video-decode", self._video_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        """Graceful, complete teardown: bye, sockets, threads, devices."""
        try:
            if self._streaming:
                self.control.send_stop()
        except Exception:
            pass
        self._stop_evt.set()
        try:
            self.control.shutdown()
        except Exception:
            pass
        for t in self._threads:
            t.join(timeout=1.5)
        self._threads.clear()
        for closer in (self.beacons.stop, self.video_rx.stop,
                       self.vcam.stop):
            try:
                closer()
            except Exception:
                log.exception("shutdown step failed (ignored)")
        self._teardown_decoders()
        log.info("omnicam app shut down")

    # ------------------------------------------------------------------
    # UI API
    # ------------------------------------------------------------------
    @staticmethod
    def import_errors() -> List[Tuple[str, str]]:
        """Optional dependencies that failed to import: (package, install cmd)."""
        out = []
        if _AV_ERROR is not None:
            out.append(("av (PyAV)", "pip install av"))
        try:
            import pyvirtualcam  # noqa: F401
        except BaseException:
            out.append(("pyvirtualcam", "pip install pyvirtualcam"))
        return out

    def drain_events(self) -> List[Tuple[str, Dict[str, Any]]]:
        """Pop all pending UI events (thread-safe; UI timer calls this)."""
        with self._events_lock:
            out = list(self._events)
            self._events.clear()
        return out

    def get_devices(self) -> List[Dict[str, Any]]:
        """Beacon devices plus any IPs the user pinned manually."""
        return self.beacons.snapshot()

    def pin_device(self, ip: str) -> None:
        """Keep a typed IP in the device list even without UDP beacons."""
        self.beacons.pin(ip)

    def get_preview_frame(self, last_seq: int) -> Tuple[int, Optional[np.ndarray]]:
        """Latest locally-adjusted frame; returns (seq, frame) where frame is
        ``None`` when nothing newer than ``last_seq`` exists."""
        with self._preview_lock:
            if self._preview_seq == last_seq:
                return last_seq, None
            return self._preview_seq, self._preview

    def get_stats_snapshot(self) -> Dict[str, Any]:
        """Merged PC + receiver + phone statistics for the UI."""
        snap = self.stats.snapshot()
        snap.update(self.video_rx.get_stats())
        snap["fec_active"] = self._fec_active
        rtt = self.control.rtt_smooth_ms
        if rtt is not None:
            snap["rtt_ms"] = round(rtt, 1)
        return snap

    def get_filter_state(self) -> Dict[str, Any]:
        """Deep copy of the last known phone filter state (S5 schema)."""
        with self._local_lock:
            return copy.deepcopy(self._filter_state)

    def get_camera(self) -> str:
        """Currently selected phone camera ('front'/'back')."""
        return self._camera

    def get_camera_caps(self) -> Dict[str, List[int]]:
        """Camera caps from ``welcome``: id -> [max_w, max_h, max_fps]."""
        return dict(self._camera_caps)

    @property
    def streaming(self) -> bool:
        """True between phone ``started`` and ``stopped``."""
        return self._streaming

    @property
    def connection_text(self) -> str:
        """Human-readable control channel state for the status bar."""
        return self._conn_text

    @property
    def version(self) -> str:
        """Application version string."""
        return __version__

    @property
    def rtp_host(self) -> Optional[str]:
        """Local IP advertised as ``rtp_host`` in the last ``start`` (advisory:
        the phone prefers the control TCP peer address for media)."""
        return self._rtp_host

    def connect(self, ip: str) -> None:
        """Connect the control channel (and keep reconnecting) to ``ip``."""
        ip = ip.strip()
        if not ip:
            return
        self._emit("state", {"connected": False, "text": f"connecting to {ip}..."})
        self.control.connect_to(ip)

    def disconnect(self) -> None:
        """Send ``bye`` and stop the control channel."""
        if self._streaming:
            self.stop_stream()
        self.control.disconnect(send_bye=True)
        self._emit("state", {"connected": False, "text": "disconnected"})

    def start_stream(self, w: int, h: int, fps: int, kbps: int) -> bool:
        """Send ``start`` with the negotiated stream parameters."""
        ip = self.control.target_ip
        if ip is None:
            self._emit("error", {"code": "notconnected", "message": "not connected"})
            return False
        if _AV_ERROR is not None:
            self._emit("error", {"code": "noav",
                                 "message": f"PyAV is not installed ({_AV_ERROR}); run: pip install av"})
            return False
        self.stats.reset_stream()
        self.video_rx.reset()
        self._stream_cfg = {"w": int(w), "h": int(h), "fps": int(fps), "kbps": int(kbps)}
        self.video_rx.set_fps(int(fps))
        self._vdec = VideoDecoder()
        self._last_video_rx_mono = 0.0
        rtp_host = get_local_ip(ip)
        self._rtp_host = rtp_host
        log.info("start: media destination rtp_host=%s (phone %s, advisory; "
                 "phone prefers the control TCP peer address)", rtp_host, ip)
        video = {"port": VIDEO_PORT, "w": int(w), "h": int(h), "fps": int(fps),
                 "kbps": int(kbps), "keyint": 60}
        ok = self.control.send_start(rtp_host, video)
        if not ok:
            self._emit("error", {"code": "sendfailed", "message": "start: control channel down"})
        return ok

    def stop_stream(self) -> None:
        """Send ``stop`` and tear the decode/output pipeline down.

        TCP stays up (Disconnect is ``bye``). Media feedback must stop immediately
        or the phone keeps handling NACK/PLI and the HUD glitches.
        """
        was = False
        with self._stream_lock:
            was = self._streaming
            self._streaming = False
        if was:
            self.control.send_stop()
        self.video_rx.pause_feedback()
        self._teardown_stream()
        if was:
            self._emit("stopped", {"local": True})

    def set_camera(self, camera_id: str) -> None:
        """Switch the phone camera ('front' or 'back')."""
        if camera_id in ("front", "back"):
            self._camera = camera_id
            self.control.send_camera(camera_id)

    def set_torch(self, on: bool) -> None:
        """Toggle the phone torch."""
        self.control.send_torch(on)

    def set_bitrate(self, kbps: int) -> None:
        """Manual bitrate override (disables phone-side ABR)."""
        self.control.send_bitrate(int(kbps))

    def set_abr(self, auto: bool) -> None:
        """Enable/disable phone-side adaptive bitrate."""
        self.control.send_abr(bool(auto))

    def request_idr(self) -> None:
        """Force a keyframe on the phone."""
        self.control.send_idr()

    def start_virtual_cam(self) -> str:
        """Open OBS Virtual Camera (Unity Capture fallback) at stream size."""
        cfg = self._stream_cfg
        return self.vcam.start(cfg["w"], cfg["h"], cfg["fps"])

    def stop_virtual_cam(self) -> None:
        """Close the virtual camera."""
        self.vcam.stop()

    def set_local_adjust(self, **params: Any) -> None:
        """Update PC-local adjustments (brightness/contrast/saturation/mirror/
        flipV/rotate); applied before preview and virtual cam."""
        with self._local_lock:
            self._local_adjust.update(params)

    def get_local_adjust(self) -> Dict[str, Any]:
        """Copy of the current PC-local adjustment parameters."""
        with self._local_lock:
            return dict(self._local_adjust)

    def push_filter(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Merge ``updates`` into the filter state, push ``{"t":"filter"}``
        and return the resulting full state (PROTOCOL.md S5)."""
        with self._local_lock:
            state = merge_filter_state(self._filter_state, updates)
            self._filter_state = state
        self.control.send_filter(state)
        return copy.deepcopy(state)

    def apply_remote_filter(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Install a filter state received from the phone (welcome/filter)."""
        with self._local_lock:
            self._filter_state = merge_filter_state(state, {})
            return copy.deepcopy(self._filter_state)

    # ------------------------------------------------------------------
    # network callbacks (worker threads)
    # ------------------------------------------------------------------
    def _emit(self, kind: str, payload: Dict[str, Any]) -> None:
        with self._events_lock:
            self._events.append((kind, payload))
            while len(self._events) > 256:
                self._events.popleft()

    def _on_rtt(self, rtt_ms: float) -> None:
        self.stats.set_rtt(rtt_ms)

    def _on_state(self, connected: bool, text: str) -> None:
        self._conn_text = text
        if not connected:
            self.video_rx.pause_feedback()
            with self._stream_lock:
                was = self._streaming
            if was:
                self._teardown_stream()
        self._emit("state", {"connected": connected, "text": text})

    def _on_message(self, msg: Dict[str, Any]) -> None:
        """Route a phone->PC control message (PROTOCOL.md section 2.2)."""
        kind = msg.get("t")
        if kind == "welcome":
            state = msg.get("filter")
            if isinstance(state, dict):
                self.apply_remote_filter(state)
            self._camera = str(msg.get("camera", self._camera))
            front = msg.get("max_front")
            back = msg.get("max_back")
            if isinstance(front, list) and isinstance(back, list):
                try:
                    self._camera_caps = {"front": [int(v) for v in front[:3]],
                                         "back": [int(v) for v in back[:3]]}
                except (TypeError, ValueError):
                    self._camera_caps = {}
            self._emit("welcome", dict(msg))
        elif kind == "started":
            self._apply_started(msg)
        elif kind == "stopped":
            self.video_rx.pause_feedback()
            self._teardown_stream()
            self._emit("stopped", {})
        elif kind == "camera_ok":
            self._camera = str(msg.get("id", self._camera))
            self._emit("camera_ok", dict(msg))
        elif kind == "bitrate_ok":
            self._emit("bitrate_ok", dict(msg))
        elif kind == "filter_ok":
            self._emit("filter_ok", {})
        elif kind == "torch_ok":
            self._emit("torch_ok", dict(msg))
        elif kind == "stats":
            self.stats.set_phone(msg)
        elif kind == "pong":
            pass  # RTT handled inside ControlClient
        elif kind == "error":
            log.warning("phone error: %r", msg)
            self._emit("error", dict(msg))
        elif kind == "filter":
            state = msg.get("state")
            if isinstance(state, dict):
                self.apply_remote_filter(state)
                self._emit("filter", {"state": self.get_filter_state()})
        else:
            log.debug("unknown control message: %r", kind)

    def _apply_started(self, msg: Dict[str, Any]) -> None:
        """Configure receivers/decoders from the ``started`` message."""
        ip = self.control.target_ip
        ssrc_video = int(msg.get("ssrc_video", 0))
        self._fec_active = bool(msg.get("fec", False))
        fps = self._stream_cfg["fps"]
        self.video_rx.set_session(ssrc_video, ip or "255.255.255.255", fps)
        self._streaming = True
        # PLI right away so the first IDR arrives promptly
        self.video_rx.force_pli("start")
        self._emit("started", {"ssrc_video": ssrc_video,
                               "ssrc_fec": int(msg.get("ssrc_fec", 0)),
                               "fec": self._fec_active})

    def _teardown_stream(self) -> None:
        """Stop decoding/outputs after stop/disconnect/error."""
        with self._stream_lock:
            self._streaming = False
        self._teardown_decoders()
        with self._preview_lock:
            self._preview = None

    def _teardown_decoders(self) -> None:
        vdec, self._vdec = self._vdec, None
        if vdec is not None:
            try:
                vdec.close()
            except Exception:
                pass

    def _on_video_frame(self, annexb: bytes, meta: Dict[str, Any]) -> None:
        """Frame assembly callback (video-proc thread): queue for decode."""
        self._last_video_rx_mono = time.monotonic()
        self.stats.add_frame(meta.get("expected", 1), meta.get("received", 1))
        if meta.get("late"):
            self.stats.count_late()
        try:
            self._vq.put_nowait((annexb, meta))
        except queue.Full:
            # decoder is behind: drop the oldest frame and resync via PLI
            try:
                self._vq.get_nowait()
                self._vq.put_nowait((annexb, meta))
            except (queue.Empty, queue.Full):
                pass
            self.stats.count_drop()
            self.video_rx.force_pli("queue backlog")

    # ------------------------------------------------------------------
    # worker threads
    # ------------------------------------------------------------------
    def _timer_loop(self) -> None:
        """500 ms control timer: ping, receiver reports, decode-stall PLI."""
        while not self._stop_evt.wait(0.5):
            if not self.control.is_connected:
                continue
            self.control.send_ping()
            if not self._streaming:
                continue
            loss = self.video_rx.get_loss_pct()
            jitter = self.video_rx.get_stats().get("jitter_ms", 0.0)
            fps_decoded = float(self.stats.snapshot().get("fps_decoded", 0.0))
            self.control.send_rr(loss, jitter, self.video_rx.get_max_seq(), fps_decoded)
            now = time.monotonic()
            if self._last_video_rx_mono and now - self._last_video_rx_mono > 0.5:
                self.video_rx.force_pli("decode stall")

    def _video_loop(self) -> None:
        """Decode Annex-B frames, apply local adjustments, feed preview+vcam."""
        while not self._stop_evt.is_set():
            try:
                annexb, meta = self._vq.get(timeout=0.25)
            except queue.Empty:
                continue
            dec = self._vdec
            if dec is None:
                continue
            frames = dec.decode(annexb)
            if not frames:
                continue
            self._last_decode_mono = time.monotonic()
            with self._local_lock:
                params = dict(self._local_adjust)
            for frame in frames:
                self.stats.tick_decoded()
                try:
                    processed = apply_local_adjust(frame, params)
                except Exception:
                    log.exception("local adjustment failed (using raw frame)")
                    processed = frame
                with self._preview_lock:
                    self._preview = processed
                    self._preview_seq += 1
                if self.vcam.running:
                    self.vcam.submit(processed)
                del frame
