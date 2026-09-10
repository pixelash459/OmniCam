"""XOR FEC recovery per PROTOCOL.md section 3.4.

Drop exactly one packet of a covered group, deliver the PT=100 FEC packet and
assert the reconstructed frame is byte-identical to the original.  A group
with two missing packets must NOT be reconstructed.
"""

from __future__ import annotations

from helpers import build_fec_packet, join_annexb
from rx_driver import DrivenReceiver

FEC_SSRC = 0x0FEC0FEC


def _nal(size: int, tag: int, nal_type: int = 0x41) -> bytes:
    return bytes([nal_type]) + bytes([tag]) * (size - 1)


def test_fec_recovers_single_missing_packet_identical_frame():
    """Group of 4 packets spans two frames; the missing packet is the FIRST
    packet of the second frame (an FU-A start fragment of a 1500 B NAL)."""
    rx = DrivenReceiver()
    p_a = _nal(800, 0x01)      # seq 100, frame ts 3000, marker
    p_b0 = _nal(750, 0x02)     # frame B NAL is split as two FU-A-ish packets:
    p_b1 = _nal(750, 0x03)     # seq 101 (missing) + seq 102 (marker), ts 6000
    p_c = _nal(200, 0x04)      # seq 103, frame ts 9000, marker

    rx.feed(100, 3000, p_a, marker=True)
    rx.feed(102, 6000, p_b1, marker=True)
    rx.feed(103, 9000, p_c, marker=True)

    fec = build_fec_packet(100, [p_a, p_b0, p_b1, p_c], ts=9000, ssrc=FEC_SSRC)
    rx.feed_wire(fec)
    rx.tick()

    got = {meta["rtp_ts"]: annexb for annexb, meta in rx.frames}
    assert set(got) == {3000, 6000, 9000}
    assert got[3000] == join_annexb([p_a])
    assert got[6000] == join_annexb([p_b0, p_b1])  # byte-identical rebuild
    assert got[9000] == join_annexb([p_c])
    assert rx.vr.get_stats()["fec_recovered"] == 1


def test_fec_recovers_missing_marker_packet_of_pending_frame():
    """Missing packet is the LAST packet of frame A (carrying its marker):
    frame A is still assembling and must be completed by the reconstruction."""
    rx = DrivenReceiver()
    p_a0 = _nal(400, 0x11)  # seq 300, frame ts 3000
    p_a1 = _nal(400, 0x12)  # seq 301, frame ts 3000, MARKER (the lost packet)
    p_b = _nal(300, 0x22)   # seq 302, frame ts 6000, marker
    p_c = _nal(250, 0x33)   # seq 303, frame ts 9000, marker

    rx.feed(300, 3000, p_a0)                 # frame A starts, no marker yet
    rx.feed(302, 6000, p_b, marker=True)     # gap at 301
    rx.feed(303, 9000, p_c, marker=True)

    fec = build_fec_packet(300, [p_a0, p_a1, p_b, p_c], ts=9000, ssrc=FEC_SSRC)
    rx.feed_wire(fec)
    rx.tick()

    got = {meta["rtp_ts"]: annexb for annexb, meta in rx.frames}
    assert set(got) == {3000, 6000, 9000}
    assert got[3000] == join_annexb([p_a0, p_a1])  # completed, identical
    assert got[6000] == join_annexb([p_b])
    assert got[9000] == join_annexb([p_c])
    assert rx.vr.get_stats()["fec_recovered"] == 1


def test_fec_recovers_mid_frame_missing_packet():
    """Single-frame group, middle packet missing: ts is shared, frame assembles
    once the reconstruction lands."""
    rx = DrivenReceiver()
    p0 = _nal(400, 0x51)  # seq 600, ts 3000
    p1 = _nal(400, 0x52)  # seq 601, ts 3000 (missing)
    p2 = _nal(400, 0x53)  # seq 602, ts 3000
    p3 = _nal(400, 0x54)  # seq 603, ts 3000, marker
    rx.feed(600, 3000, p0)
    rx.feed(602, 3000, p2)
    rx.feed(603, 3000, p3, marker=True)
    fec = build_fec_packet(600, [p0, p1, p2, p3], ts=3000, ssrc=FEC_SSRC)
    rx.feed_wire(fec)
    # the gap must age out before XOR recovery kicks in for a mid-frame packet
    rx.tick_for(0.16, step=0.008)
    assert len(rx.frames) == 1
    annexb, _ = rx.frames[0]
    assert annexb == join_annexb([p0, p1, p2, p3])
    assert rx.vr.get_stats()["fec_recovered"] == 1


def test_fec_two_missing_packets_not_reconstructed():
    rx = DrivenReceiver()
    p0 = _nal(100, 0xa1)  # seq 400, frame ts 3000
    p1 = _nal(100, 0xa2)  # seq 401, MISSING
    p2 = _nal(100, 0xa3)  # seq 402, MISSING
    p3 = _nal(100, 0xa4)  # seq 403, marker, frame ts 3000
    rx.feed(400, 3000, p0)
    rx.feed(403, 3000, p3, marker=True)
    fec = build_fec_packet(400, [p0, p1, p2, p3], ts=3000, ssrc=FEC_SSRC)
    rx.feed_wire(fec)
    rx.tick_for(0.12, step=0.01)  # well past any recovery window
    assert rx.vr.get_stats()["fec_recovered"] == 0
    assert rx.frames == []  # nothing was (or could be) reconstructed
    assert rx.vr.get_stats()["drops"] >= 1  # frame given up as loss


def test_fec_group_resolved_without_loss_is_discarded():
    """A complete group with its FEC packet must not duplicate anything."""
    rx = DrivenReceiver()
    p0 = _nal(60, 0xb1)
    p1 = _nal(60, 0xb2)
    rx.feed(500, 3000, p0)
    rx.feed(501, 3000, p1, marker=True)
    fec = build_fec_packet(500, [p0, p1, b"\x00" * 60, b"\x00" * 60],
                           ts=3000, ssrc=FEC_SSRC)
    rx.feed_wire(fec)
    rx.tick()
    assert len(rx.frames) == 1
    assert rx.frames[0][0] == join_annexb([p0, p1])
    assert rx.vr.get_stats()["fec_recovered"] == 0
