"""Reorder buffer, frame assembly and stale-frame eviction of VideoReceiver.

Feeds a mock-socket receiver out-of-order packets with a deliberate gap and
asserts: NACK only after the reorder wait, frame emission only at the marker
bit, incomplete stale frames dropped, sequence wrap 65535->0 and duplicate
(retransmitted) sequence dedupe.
"""

from __future__ import annotations

import time

from helpers import join_annexb, parse_rtcp_feedback
from rx_driver import DrivenReceiver


def test_frame_assembles_only_at_marker_bit():
    rx = DrivenReceiver()
    nals = [b"\x65" + b"A" * 50, b"\x41" + b"B" * 30, b"\x41" + b"C" * 20]
    rx.feed(100, 3000, nals[0])
    rx.feed(101, 3000, nals[1])  # marker packet not yet arrived
    rx.tick()
    assert rx.frames == []
    rx.feed(102, 3000, nals[2], marker=True)
    rx.tick()
    assert len(rx.frames) == 1
    annexb, meta = rx.frames[0]
    assert annexb == join_annexb(nals)
    assert meta["rtp_ts"] == 3000
    assert meta["first_seq"] == 100 and meta["last_seq"] == 102
    assert meta["received"] == 3 and meta["expected"] == 3
    assert meta["late"] is False


def test_out_of_order_with_gap_nacks_then_recovers():
    rx = DrivenReceiver()
    s0 = b"\x65" + b"\x01" * 100
    s1 = b"\x41" + b"\x02" * 100
    s2 = b"\x41" + b"\x03" * 100
    s3 = b"\x41" + b"\x04" * 100
    # 1. out of order: first and third/fourth packets arrive, second is missing
    rx.feed(100, 3000, s0)
    rx.feed(102, 3000, s2)
    rx.feed(103, 3000, s3, marker=True)
    rx.tick()
    assert rx.frames == []                    # nothing may be emitted yet
    assert rx.rtcp_packets(205) == []         # inside the 8 ms reorder wait
    # 2. after the reorder wait a NACK for seq 101 is emitted
    time.sleep(0.012)
    rx.tick()
    nacks = rx.rtcp_packets(205)
    assert len(nacks) == 1
    p = parse_rtcp_feedback(nacks[0])
    assert p["fmt"] == 1 and p["pt"] == 205 and p["implied_seqs"] == [101]
    # 3. the retransmission completes the frame in original order
    rx.feed(101, 3000, s1)
    rx.tick()
    assert len(rx.frames) == 1
    annexb, meta = rx.frames[0]
    assert annexb == join_annexb([s0, s1, s2, s3])
    assert meta["received"] == 4 and meta["expected"] == 4


def test_incomplete_stale_frame_is_dropped():
    rx = DrivenReceiver()
    rx.feed(200, 9000, b"\x41" + b"\x05" * 40)
    rx.feed(201, 9000, b"\x41" + b"\x06" * 40)  # marker packet never arrives
    rx.tick()
    assert rx.frames == []
    stats0 = rx.vr.get_stats()
    # 3 frame periods at 30 fps = 100 ms -> frame must be dropped
    rx.tick_for(0.14, step=0.01)
    stats1 = rx.vr.get_stats()
    assert stats1["drops"] == stats0["drops"] + 1
    assert rx.frames == []  # never emitted
    assert rx.vr.get_loss_pct() > 0.0


def test_sequence_wrap_65535_to_0():
    rx = DrivenReceiver()
    n1, n2, n3 = (b"\x41" + b"a" * 20, b"\x41" + b"b" * 20, b"\x41" + b"c" * 20)
    rx.feed(65534, 12000, n1)
    rx.feed(65535, 12000, n2)
    rx.feed(0, 12000, n3, marker=True)
    rx.tick()
    assert len(rx.frames) == 1
    annexb, meta = rx.frames[0]
    assert annexb == join_annexb([n1, n2, n3])
    assert meta["first_seq"] == 65534 and meta["last_seq"] == 0


def test_gap_crossing_seq_wrap_is_nacked():
    """A loss whose gap spans 65535 -> 0 must still be detected and NACKed."""
    rx = DrivenReceiver()
    a0 = b"\x41" + b"\x01" * 20
    a1 = b"\x41" + b"\x02" * 20  # seq 65535, the marker of frame A -- "lost"
    b0 = b"\x41" + b"\x03" * 20  # seq 0, frame B (ts 6000), complete in one
    rx.feed(65534, 3000, a0)
    rx.feed(0, 6000, b0, marker=True)  # gap 65535 crosses the wrap
    time.sleep(0.012)
    rx.tick()
    nacks = rx.rtcp_packets(205)
    assert nacks, "wrap-crossing gap must produce a NACK"
    assert parse_rtcp_feedback(nacks[0])["implied_seqs"] == [65535]
    # the retransmission completes both frames in order
    rx.feed(65535, 3000, a1, marker=True)
    rx.tick()
    assert [m["rtp_ts"] for _, m in rx.frames] == [3000, 6000]
    assert rx.frames[0][0] == join_annexb([a0, a1])
    assert rx.frames[1][0] == join_annexb([b0])


def test_frame_that_lost_its_start_is_dropped_not_emitted():
    """When a give-up swallows a frame's first packet (previous frame's marker
    already seen), the truncated frame must be dropped, never emitted."""
    rx = DrivenReceiver()
    a0 = b"\x41" + b"\x11" * 20
    a1 = b"\x41" + b"\x12" * 20
    rx.feed(300, 3000, a0)
    rx.feed(301, 3000, a1, marker=True)  # frame A complete, marker seq 301
    rx.tick()
    assert len(rx.frames) == 1
    b1 = b"\x41" + b"\x22" * 20  # seq 303: frame B's second + marker packet
    rx.feed(303, 6000, b1, marker=True)  # frame B's start (302) is "lost"
    rx.tick_for(0.15, step=0.01)  # past the give-up window
    # frame B (ts 6000) must NOT be emitted headless
    assert all(meta["rtp_ts"] != 6000 for _, meta in rx.frames)
    assert rx.vr.get_stats()["drops"] >= 1
    assert rx.vr.get_loss_pct() > 0.0
    # and the pipeline keeps working for the next intact frame
    rx.feed(304, 9000, b"\x41" + b"\x33" * 20, marker=True)
    rx.tick()
    assert [m["rtp_ts"] for _, m in rx.frames] == [3000, 9000]


def test_lost_marker_of_open_frame_does_not_poison_next_frame():
    """Frame N's MARKER packet is lost: N is dropped as stale, but the next
    intact frame N+1 must still assemble and be emitted normally."""
    rx = DrivenReceiver()
    rx.feed(400, 3000, b"\x41" + b"\x01" * 20)          # frame N's only arrived pkt
    # seq 401 (frame N's marker) is "lost"
    rx.feed(402, 6000, b"\x41" + b"\x02" * 20, marker=True)  # frame N+1
    rx.tick()
    assert rx.frames == []  # nothing complete yet
    rx.tick_for(0.15, step=0.01)  # give-up window passes
    got = {meta["rtp_ts"] for _, meta in rx.frames}
    assert got == {6000}  # N+1 intact and emitted; N never emitted
    assert rx.vr.get_stats()["drops"] >= 1  # N given up as loss


def test_duplicate_retransmitted_seq_is_deduped():
    rx = DrivenReceiver()
    n1 = b"\x41" + b"\x0a" * 20
    n2 = b"\x41" + b"\x0b" * 20
    rx.feed(300, 15000, n1)
    rx.feed(300, 15000, n1)  # exact retransmission
    rx.feed(301, 15000, n2, marker=True)
    rx.feed(301, 15000, n2)  # late duplicate after completion
    rx.tick()
    assert len(rx.frames) == 1
    annexb, _ = rx.frames[0]
    assert annexb == join_annexb([n1, n2])  # no duplicated NAL content
    assert rx.vr.get_stats()["dup_packets"] == 2


def test_buffered_out_of_order_delivers_in_order():
    """Reordered arrival (marker packet third, last packet last) still
    assembles the original frame exactly once, in order."""
    rx = DrivenReceiver()
    nals = [b"\x41" + bytes([i]) * 30 for i in range(1, 5)]
    rx.feed(100, 18000, nals[0])
    rx.feed(103, 18000, nals[3], marker=True)  # far out of order
    rx.feed(102, 18000, nals[2])
    rx.feed(101, 18000, nals[1])
    rx.tick()
    assert len(rx.frames) == 1
    annexb, _ = rx.frames[0]
    assert annexb == join_annexb(nals)
