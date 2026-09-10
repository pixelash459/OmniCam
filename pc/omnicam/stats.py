"""Thread-safe streaming statistics for the OmniCam PC receiver.

Collects decoded/displayed fps, measured video bitrate, packet loss, NACK/
drop counters and a glass-to-glass latency estimate:

    g2g ~ (wall-clock now - capture time of the newest displayed frame) + RTT/2

The capture time is estimated from the frame's RTP timestamp using a median
offset between the RTP 90 kHz clock and the PC wall clock (offset sampled at
first-packet arrival of every frame), and one-way network delay is
approximated with half of the control-channel RTT (ping/pong).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

WINDOW_S = 2.0          # rolling window for rates
OFFSET_MEDIAN_N = 32    # samples for the RTP<->wall-clock offset median


class Stats:
    """All counters guarded by one lock; ``snapshot()`` feeds the UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bytes: Deque[Tuple[float, int]] = deque()      # (mono, video bytes)
        self._decoded: Deque[float] = deque()                # mono timestamps
        self._displayed: Deque[float] = deque()
        self._loss: Deque[Tuple[float, int, int]] = deque()  # (mono, expected, lost)
        self._nacks_sent = 0
        self._plis_sent = 0
        self._drops = 0
        self._late_frames = 0
        self._fec_recovered = 0
        self._rtt_ms: Optional[float] = None
        self._phone: Dict[str, object] = {}
        # RTP timestamp unwrapping + offset estimation
        self._last_raw_ts: Optional[int] = None
        self._unwrapped_ms: Optional[float] = None
        self._offsets: Deque[float] = deque(maxlen=OFFSET_MEDIAN_N)
        self._displayed_sample: Optional[Tuple[float, float]] = None  # (rtp_unwrapped_ms, mono)

    # -- feed points -------------------------------------------------------
    def add_video_bytes(self, n: int) -> None:
        """Called per video RTP packet with its wire size in bytes."""
        now = time.monotonic()
        with self._lock:
            self._bytes.append((now, n))

    def add_frame(self, expected: int, received: int) -> None:
        """Called per assembled frame with its packet accounting."""
        now = time.monotonic()
        with self._lock:
            self._loss.append((now, expected, expected - received))
            while self._loss and now - self._loss[0][0] > 2 * WINDOW_S:
                self._loss.popleft()

    def count_nack(self, n: int = 1) -> None:
        """Increment the NACK counter."""
        with self._lock:
            self._nacks_sent += n

    def count_pli(self, n: int = 1) -> None:
        """Increment the PLI counter."""
        with self._lock:
            self._plis_sent += n

    def count_drop(self, n: int = 1) -> None:
        """Increment dropped/incomplete frame counter."""
        with self._lock:
            self._drops += n

    def count_late(self, n: int = 1) -> None:
        """Increment late-frame counter."""
        with self._lock:
            self._late_frames += n

    def count_fec(self, n: int = 1) -> None:
        """Increment FEC-recovered packet counter."""
        with self._lock:
            self._fec_recovered += n

    def tick_decoded(self) -> None:
        """Called once per decoded video frame."""
        now = time.monotonic()
        with self._lock:
            self._decoded.append(now)

    def tick_displayed(self) -> None:
        """Called once per rendered video frame (UI timer)."""
        now = time.monotonic()
        with self._lock:
            self._displayed.append(now)

    def mark_frame_displayed(self, rtp_ts: int, recv_epoch_ms: float) -> None:
        """Record the newest displayed frame for the g2g estimate.

        ``rtp_ts`` is the raw 32-bit RTP timestamp (90 kHz), ``recv_epoch_ms``
        the PC wall-clock (ms) at arrival of the frame's first packet.
        """
        now = time.time() * 1000.0
        with self._lock:
            if self._last_raw_ts is None:
                self._last_raw_ts = rtp_ts
                self._unwrapped_ms = recv_epoch_ms - rtp_ts * 1000.0 / 90000.0
            else:
                delta = (rtp_ts - self._last_raw_ts) & 0xFFFFFFFF
                if delta >= 0x80000000:
                    delta -= 0x100000000
                self._last_raw_ts = rtp_ts
                self._unwrapped_ms += delta * 1000.0 / 90000.0
            # capture-in-wall-clock sample: frame ts mapped to PC time at arrival
            self._offsets.append(recv_epoch_ms - self._unwrapped_ms)
            self._displayed_sample = (self._unwrapped_ms, time.monotonic())

    def set_rtt(self, rtt_ms: float) -> None:
        """Update the measured control-channel RTT."""
        with self._lock:
            self._rtt_ms = rtt_ms

    def set_phone(self, msg: Dict[str, object]) -> None:
        """Store the latest phone-side ``stats`` message."""
        with self._lock:
            self._phone = dict(msg)

    # -- snapshot ----------------------------------------------------------
    def _rate(self, stamps: Deque[float], now: float) -> float:
        while stamps and now - stamps[0] > WINDOW_S:
            stamps.popleft()
        if len(stamps) < 2:
            return 0.0
        span = max(now - stamps[0], 1e-3)
        return (len(stamps) - 1) / span

    def snapshot(self) -> Dict[str, object]:
        """Return all current metrics as a flat dict for the UI."""
        now = time.monotonic()
        with self._lock:
            while self._bytes and now - self._bytes[0][0] > WINDOW_S:
                self._bytes.popleft()
            bitrate_kbps = sum(b for _, b in self._bytes) * 8.0 / (WINDOW_S * 1000.0)
            exp = lost = 0
            for _, e, l in self._loss:
                exp += e
                lost += l
            loss_pct = (100.0 * lost / exp) if exp > 0 else 0.0
            offsets = sorted(self._offsets)
            median_offset = offsets[len(offsets) // 2] if offsets else None
            g2g_ms: Optional[float] = None
            if (median_offset is not None and self._displayed_sample is not None
                    and self._rtt_ms is not None):
                rtp_ms, _mono = self._displayed_sample
                capture_est = rtp_ms + median_offset  # wall-clock capture estimate
                g2g_ms = (time.time() * 1000.0 - capture_est) + self._rtt_ms / 2.0
                g2g_ms = min(max(g2g_ms, 0.0), 5000.0)
            return {
                "fps_decoded": round(self._rate(self._decoded, now), 1),
                "fps_displayed": round(self._rate(self._displayed, now), 1),
                "bitrate_kbps": round(bitrate_kbps, 1),
                "loss_pct": round(loss_pct, 2),
                "rtt_ms": self._rtt_ms,
                "nacks_sent": self._nacks_sent,
                "plis_sent": self._plis_sent,
                "drops": self._drops,
                "late_frames": self._late_frames,
                "fec_recovered": self._fec_recovered,
                "g2g_ms": g2g_ms,
                "phone": dict(self._phone),
            }

    def reset_stream(self) -> None:
        """Clear per-stream accumulators (new stream started)."""
        with self._lock:
            self._bytes.clear()
            self._loss.clear()
            self._last_raw_ts = None
            self._unwrapped_ms = None
            self._offsets.clear()
            self._displayed_sample = None
            self._phone = {}
