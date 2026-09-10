"""Manual driver for VideoReceiver without real sockets/threads.

Lets tests feed RTP packets synchronously (``_handle_media`` / ``_handle_fec``)
and run maintenance ticks, while NACK/PLI output is captured by a FakeSock.
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from helpers import FakeSock, build_rtp

from omnicam.net import VideoReceiver

FEEDBACK_PORT = 56565  # fake phone feedback port (receiver sends RTCP here)


class DrivenReceiver:
    """VideoReceiver driven manually: no bind, no threads, deterministic."""

    def __init__(self, fps: float = 30.0, media_ssrc: int = 0x50110102) -> None:
        self.vr = VideoReceiver(fps=fps)
        self.sock = FakeSock()
        self.vr._sock = self.sock
        self.vr.set_session(media_ssrc, "127.0.0.1", fps)
        self.vr._feedback_addr = ("127.0.0.1", FEEDBACK_PORT)
        self.frames: List[Tuple[bytes, dict]] = []
        self.vr.set_frame_callback(self._on_frame)

    def _on_frame(self, annexb: bytes, meta: dict) -> None:
        self.frames.append((annexb, meta))

    # -- feeding -----------------------------------------------------------
    def feed(self, seq: int, ts: int, payload: bytes, marker: bool = False,
             pt: int = 96, ssrc: int = 0x11223344, at: Optional[float] = None) -> None:
        wire = build_rtp(seq, ts, payload, marker=marker, pt=pt, ssrc=ssrc)
        self.feed_wire(wire, at=at)

    def feed_wire(self, wire: bytes, at: Optional[float] = None) -> None:
        from omnicam.net import parse_rtp
        pkt = parse_rtp(wire)
        assert pkt is not None
        now = at if at is not None else time.monotonic()
        if pkt.pt == 96:
            self.vr._handle_media(pkt, now, now)
        else:
            self.vr._handle_fec(pkt, now)

    def tick(self) -> None:
        self.vr._maintenance(time.monotonic())

    def tick_for(self, seconds: float, step: float = 0.004) -> None:
        """Run maintenance every ``step`` seconds of wall time."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.tick()
            time.sleep(step)

    # -- inspection --------------------------------------------------------
    def rtcp_packets(self, pt: Optional[int] = None) -> List[bytes]:
        # RTCP carries its payload type in the FULL second byte (no marker bit)
        return [d for d, _ in self.sock.sent if pt is None or d[1] == pt]
