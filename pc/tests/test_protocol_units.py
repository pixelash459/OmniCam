"""Unit tests for the protocol primitives in omnicam.net.

Covers the RFC 6184 depacketizer (single NALU / STAP-A / FU-A, exact Annex-B
output), RTP parse/build round-trips including the 16-bit sequence wrap, and
NAL sizes at the 1200-byte single-packet boundary.
"""

from __future__ import annotations

import os
import struct

from helpers import (
    DEFAULT_SSRC_VIDEO,
    START_CODE,
    build_fu_a,
    build_rtp,
    build_stap_a,
    join_annexb,
)

from omnicam.net import RtpPacket, build_rtcp_feedback, depacketize_h264, parse_rtp

NRI3_TYPE5 = bytes([0x65])  # IDR slice NAL header byte (type 5, NRI 3)


def _nal(n: int, header: bytes = NRI3_TYPE5, seed: int = 1) -> bytes:
    return header + bytes([(seed + i) % 256 for i in range(n - 1)])


# ---------------------------------------------------------------------------
# RTP parser / builder
# ---------------------------------------------------------------------------

def test_parse_rtp_roundtrip_all_fields():
    wire = build_rtp(seq=513, ts=288000, payload=b"\x01\x02\x03",
                     marker=True, pt=96, ssrc=0xDEADBEEF)
    pkt = parse_rtp(wire)
    assert isinstance(pkt, RtpPacket)
    assert pkt.marker is True
    assert pkt.pt == 96
    assert pkt.seq == 513
    assert pkt.ts == 288000
    assert pkt.ssrc == 0xDEADBEEF
    assert pkt.payload == b"\x01\x02\x03"


def test_parse_rtp_sequence_wrap_65535_to_0():
    hi = parse_rtp(build_rtp(seq=65535, ts=1, payload=b"x"))
    lo = parse_rtp(build_rtp(seq=0, ts=3001, payload=b"x"))
    assert hi.seq == 65535
    assert lo.seq == 0


def test_parse_rtp_rejects_malformed():
    assert parse_rtp(b"") is None
    assert parse_rtp(b"\x00" * 12) is None            # version 0
    assert parse_rtp(build_rtp(1, 1, b"")[:11]) is None  # truncated header
    pkt = parse_rtp(build_rtp(1, 1, b"payload"))
    assert pkt is not None and pkt.payload == b"payload"


def test_rtp_packet_seq_and_ts_masks():
    pkt = RtpPacket(True, 96, 0x12345, -3, 0x1FFFFFFFF, b"")
    assert pkt.seq == 0x2345
    assert pkt.ts == 0xFFFFFFFD
    assert pkt.ssrc == 0xFFFFFFFF


# ---------------------------------------------------------------------------
# RFC 6184 depacketizer
# ---------------------------------------------------------------------------

def test_depacketize_single_nalu():
    nal = _nal(50)
    out = depacketize_h264([nal])
    assert out == START_CODE + nal


def test_depacketize_two_single_nalus_in_order():
    n1, n2 = _nal(30, seed=1), _nal(40, seed=2)
    out = depacketize_h264([n1, n2])
    assert out == join_annexb([n1, n2])
    assert out.startswith(b"\x00\x00\x00\x01" + NRI3_TYPE5)


def test_depacketize_stap_a_sps_pps():
    sps = bytes([0x67]) + b"\x11" * 14   # type 7
    pps = bytes([0x68]) + b"\x22" * 5    # type 8
    stap = build_stap_a([sps, pps])
    assert stap[0] & 0x1F == 24
    out = depacketize_h264([stap])
    assert out == START_CODE + sps + START_CODE + pps


def test_depacketize_stap_a_rejects_truncated():
    sps = bytes([0x67]) + b"\x11" * 14
    stap = build_stap_a([sps])
    malformed = stap[:-3]  # cut into the length-prefixed NAL
    out = depacketize_h264([malformed])
    assert out == b""


def test_depacketize_fu_a_three_fragments():
    nal = _nal(2500)
    frags = [build_fu_a(nal, i, 3) for i in range(3)]
    # S bit only on the first, E bit only on the last
    assert frags[0][1] & 0x80 and not (frags[1][1] & 0x80) and not (frags[2][1] & 0x80)
    assert not (frags[0][1] & 0x40) and not (frags[1][1] & 0x40) and frags[2][1] & 0x40
    for f in frags:
        assert f[0] & 0x1F == 28
        assert (f[0] >> 5) == (nal[0] >> 5)  # NRI copied to the indicator
    out = depacketize_h264(frags)
    assert out == START_CODE + nal
    assert len(out) == 4 + 2500


def test_depacketize_fu_a_single_fragment():
    nal = _nal(60)
    # tiny NAL fragmented as one FU-A unit with S and E both set
    indicator = bytes([(3 << 5) | 28])
    frag = indicator + bytes([0xC0 | (nal[0] & 0x1F)]) + nal[1:]
    assert depacketize_h264([frag]) == START_CODE + nal


def test_depacketize_fu_a_interleaved_with_complete_nal():
    nal = _nal(2500)
    frags = [build_fu_a(nal, i, 3) for i in range(3)]
    other = _nal(20, seed=9)
    out = depacketize_h264(frags[:1] + [other] + frags[1:])
    # the complete NAL interrupts the fragment: only its header byte survives
    # of the aborted FU, then the other NAL; continuation fragments are dropped
    assert START_CODE + other in out


def test_depacketize_fu_a_missing_start_drops_fragments():
    nal = _nal(2500)
    mid = build_fu_a(nal, 1, 3)
    assert depacketize_h264([mid]) == b""


def test_depacketize_nal_exactly_1200_is_single_packet():
    nal = _nal(1200)
    out = depacketize_h264([nal])
    assert out == START_CODE + nal
    assert len(out) == 1204


def test_depacketize_nal_1201_requires_fu_a():
    nal = _nal(1201)
    # 1200 bytes of FU-A data split as 1198 + 2
    f0 = build_fu_a(nal, 0, 2)
    f1 = build_fu_a(nal, 1, 2)
    assert len(f0) == 2 + 1198  # indicator + FU header + payload
    assert len(f1) == 2 + 2
    out = depacketize_h264([f0, f1])
    assert out == START_CODE + nal


def test_depacketize_empty_and_garbage_payloads():
    assert depacketize_h264([]) == b""
    assert depacketize_h264([b""]) == b""
    # unknown types (e.g. 25 MTAP) are skipped, not fatal
    assert depacketize_h264([bytes([0x60]) + b"junk"]) == b""


def test_depacketize_full_idr_access_unit():
    """SPS+PPS STAP-A followed by a fragmented IDR slice -> exact Annex-B."""
    sps = bytes([0x67]) + b"\x42" * 12
    pps = bytes([0x68]) + b"\x43" * 4
    idr = _nal(2600)
    payloads = [build_stap_a([sps, pps]),
                build_fu_a(idr, 0, 3), build_fu_a(idr, 1, 3), build_fu_a(idr, 2, 3)]
    out = depacketize_h264(payloads)
    assert out == START_CODE + sps + START_CODE + pps + START_CODE + idr
