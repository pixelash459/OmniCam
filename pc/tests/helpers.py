"""Shared test helpers for the OmniCam PC test suite.

Pure-stdlib building blocks used by several test modules: RTP packet
builder, an RFC 6184 packetizer that mirrors the iOS sender behaviour
(PROTOCOL.md section 3.1), an RTCP feedback parser for byte-level NACK/PLI
checks and a recording fake socket.
"""

from __future__ import annotations

import struct
import time
from typing import Callable, Iterable, List, Optional, Tuple

START_CODE = b"\x00\x00\x00\x01"
MAX_VIDEO_PAYLOAD = 1200  # PROTOCOL.md section 0

DEFAULT_SSRC_VIDEO = 0x0A0B0C0D
DEFAULT_SSRC_FEC = 0x0FEC0FEC


# ---------------------------------------------------------------------------
# RTP
# ---------------------------------------------------------------------------

def build_rtp(seq: int, ts: int, payload: bytes, marker: bool = False,
              pt: int = 96, ssrc: int = DEFAULT_SSRC_VIDEO) -> bytes:
    """Standard 12-byte-header RTP datagram (V=2, P=0, X=0, CC=0)."""
    b0 = 0x80
    b1 = (0x80 if marker else 0x00) | (pt & 0x7F)
    return struct.pack("!BBHII", b0, b1, seq & 0xFFFF, ts & 0xFFFFFFFF,
                       ssrc & 0xFFFFFFFF) + payload


class RtpPacketizer:
    """Packetizes Annex-B NALUs exactly like the iOS sender (PROTOCOL.md 3.1):
    STAP-A (type 24) with SPS+PPS before every IDR, FU-A (type 28) for NALs
    above 1200 bytes, single NAL unit packets otherwise, marker bit on the
    last packet of every frame, +3000 per frame on the 90 kHz clock."""

    def __init__(self, ssrc: int = DEFAULT_SSRC_VIDEO, pt: int = 96,
                 seq: int = 100, ts: int = 9000) -> None:
        self.ssrc = ssrc
        self.pt = pt
        self.seq = seq & 0xFFFF
        self.ts = ts & 0xFFFFFFFF

    def packetize_frame(self, nals: List[bytes], is_idr: bool,
                        sps: Optional[bytes] = None,
                        pps: Optional[bytes] = None) -> List[bytes]:
        """Return the UDP datagrams (in order) for one access unit."""
        dgrams: List[bytes] = []

        def emit(payload: bytes, marker: bool = False) -> None:
            dgrams.append(build_rtp(self.seq, self.ts, payload,
                                    marker=marker, pt=self.pt, ssrc=self.ssrc))
            self.seq = (self.seq + 1) & 0xFFFF

        if is_idr and sps is not None and pps is not None:
            stap = bytes([(3 << 5) | 24])  # NRI=3, type 24
            for nal in (sps, pps):
                stap += len(nal).to_bytes(2, "big") + nal
            emit(stap)
        for nal in nals:
            if not nal:
                continue
            if len(nal) <= MAX_VIDEO_PAYLOAD:
                emit(nal)
            else:  # FU-A
                indicator = bytes([(3 << 5) | 28])
                ntype = nal[0] & 0x1F
                data = nal[1:]
                step = MAX_VIDEO_PAYLOAD - 2
                chunks = [data[i:i + step] for i in range(0, len(data), step)]
                for j, chunk in enumerate(chunks):
                    fu_header = (0x80 if j == 0 else 0x00) \
                        | (0x40 if j == len(chunks) - 1 else 0x00) | ntype
                    emit(indicator + bytes([fu_header]) + chunk)
        if dgrams:  # marker bit on the final packet of the frame
            last = bytearray(dgrams[-1])
            last[1] |= 0x80
            dgrams[-1] = bytes(last)
        self.ts = (self.ts + 3000) & 0xFFFFFFFF  # 90000 / 30 fps
        return dgrams


# ---------------------------------------------------------------------------
# Annex-B utilities
# ---------------------------------------------------------------------------

def split_annexb_nals(data: bytes) -> List[bytes]:
    """Split an Annex-B byte string into NALUs (start codes removed)."""
    out: List[bytes] = []
    for part in data.split(b"\x00\x00\x01")[1:]:
        if part.startswith(b"\x00"):  # 4-byte start code leaves one zero
            part = part[1:]
        if part:
            out.append(part)
    return out


def join_annexb(nals: Iterable[bytes]) -> bytes:
    return b"".join(START_CODE + n for n in nals)


def build_stap_a(nals: List[bytes], nri: int = 3) -> bytes:
    payload = bytes([(nri << 5) | 24])
    for nal in nals:
        payload += len(nal).to_bytes(2, "big") + nal
    return payload


def build_fu_a(nal: bytes, frag: int, count: int, nri: int = 3) -> bytes:
    """Build FU-A fragment ``frag`` (0-based) of ``count`` total for ``nal``."""
    indicator = bytes([(nri << 5) | 28])
    ntype = nal[0] & 0x1F
    data = nal[1:]
    step = MAX_VIDEO_PAYLOAD - 2
    chunks = [data[i:i + step] for i in range(0, len(data), step)]
    assert len(chunks) == count, f"nal splits into {len(chunks)} fragments, not {count}"
    fu = (0x80 if frag == 0 else 0x00) | (0x40 if frag == count - 1 else 0x00) | ntype
    return indicator + bytes([fu]) + chunks[frag]


# ---------------------------------------------------------------------------
# RTCP feedback parsing (RFC 4585) -- for byte-level verification
# ---------------------------------------------------------------------------

def parse_rtcp_feedback(data: bytes) -> dict:
    """Parse an RFC 4585 PSFB/RTCPFB packet into its fields by hand."""
    assert len(data) >= 12, "rtcp feedback packet too short"
    b0, pt, length = data[0], data[1], int.from_bytes(data[2:4], "big")
    parsed = {
        "version": b0 >> 6,
        "padding": bool(b0 & 0x20),
        "fmt": b0 & 0x1F,
        "pt": pt,
        "length": length,
        "sender_ssrc": int.from_bytes(data[4:8], "big"),
        "media_ssrc": int.from_bytes(data[8:12], "big"),
        "entries": [],
        "bytes": len(data),
    }
    words = length + 1  # length counts 32-bit words minus one
    assert parsed["bytes"] == words * 4, \
        f"length field {length} != {parsed['bytes']} bytes"
    fci = data[12:]
    if pt == 205 and parsed["fmt"] == 1:  # Generic NACK: PID+BLP pairs
        for i in range(0, len(fci) - 1, 4):
            pid = int.from_bytes(fci[i:i + 2], "big")
            blp = int.from_bytes(fci[i + 2:i + 4], "big")
            parsed["entries"].append((pid, blp))
    implied = []
    for pid, blp in parsed["entries"]:
        implied.append(pid)
        for bit in range(16):
            if blp & (1 << bit):
                implied.append((pid + 1 + bit) & 0xFFFF)
    parsed["implied_seqs"] = implied
    return parsed


def build_fec_packet(base_seq: int, payloads: List[bytes], ts: int,
                     seq: int = 1, ssrc: int = DEFAULT_SSRC_FEC) -> bytes:
    """XOR FEC packet per PROTOCOL.md section 3.4:
    [base_seq(16)][count(8)][flags(8)][XOR of payloads zero-padded]."""
    count = len(payloads)
    max_len = max(len(p) for p in payloads)
    acc = bytearray(max_len)
    for p in payloads:
        for i, b in enumerate(p):
            acc[i] ^= b
    body = base_seq.to_bytes(2, "big") + bytes([count, 0]) + bytes(acc)
    return build_rtp(seq, ts, body, marker=False, pt=100, ssrc=ssrc)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class FakeSock:
    """Stand-in for VideoReceiver's UDP socket: records sendto() calls."""

    def __init__(self) -> None:
        self.sent: List[Tuple[bytes, tuple]] = []

    def sendto(self, data: bytes, addr) -> None:
        self.sent.append((bytes(data), tuple(addr)))


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0,
               interval: float = 0.01, tick: Optional[Callable[[], None]] = None) -> bool:
    """Poll ``predicate`` until true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tick is not None:
            tick()
        if predicate():
            return True
        time.sleep(interval)
    return False
