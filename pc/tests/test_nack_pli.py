"""Byte-level verification of the RTCP feedback the receiver builds.

NACK (RFC 4585 Generic NACK, PT=205 FMT=1, PID+BLP entries) and PLI
(PT=206 FMT=1) packets are parsed back by hand and every header field is
asserted, plus the re-NACK throttle and the 16-entries-per-packet cap.
"""

from __future__ import annotations

import time

from helpers import parse_rtcp_feedback
from rx_driver import FEEDBACK_PORT, DrivenReceiver

from omnicam.net import build_nack, build_pli

SENDER = 0x1BADB002
MEDIA = 0x50110102


# ---------------------------------------------------------------------------
# build_nack / build_pli byte level
# ---------------------------------------------------------------------------

def test_nack_single_entry_with_blp_bits():
    data = build_nack(SENDER, MEDIA, [100, 101, 103])
    p = parse_rtcp_feedback(data)
    assert p["version"] == 2 and p["padding"] is False
    assert p["fmt"] == 1 and p["pt"] == 205
    assert p["sender_ssrc"] == SENDER
    assert p["media_ssrc"] == MEDIA
    assert p["length"] == 3  # fixed 2 words + 1 FCI word
    assert p["entries"] == [(100, 0b0101)]  # bit0 -> 101, bit2 -> 103
    assert p["implied_seqs"] == [100, 101, 103]


def test_nack_multiple_entries():
    data = build_nack(SENDER, MEDIA, [10, 500])
    p = parse_rtcp_feedback(data)
    assert p["length"] == 4
    assert p["entries"] == [(10, 0), (500, 0)]


def test_nack_chain_breaks_at_distance_17():
    # 100 and 118 are 18 apart -> must be separate FCI entries
    data = build_nack(SENDER, MEDIA, [100, 118])
    p = parse_rtcp_feedback(data)
    assert p["entries"] == [(100, 0), (118, 0)]


def test_nack_max_blp_run_16():
    # 17 consecutive lost seqs fit in ONE entry: PID covers the first and
    # BLP bits 0..15 the remaining 16
    data = build_nack(SENDER, MEDIA, list(range(200, 217)))  # 17 consecutive
    p = parse_rtcp_feedback(data)
    assert p["entries"] == [(200, 0xFFFF)]
    assert p["implied_seqs"] == list(range(200, 217))
    # an 18th consecutive seq needs a second entry
    p2 = parse_rtcp_feedback(build_nack(SENDER, MEDIA, list(range(200, 218))))
    assert p2["entries"] == [(200, 0xFFFF), (217, 0)]


def test_nack_dedupes_and_caps_at_16_entries():
    seqs = [1000 + 20 * i for i in range(20)]  # spread out: one entry each
    data = build_nack(SENDER, MEDIA, seqs + seqs)  # duplicates removed
    p = parse_rtcp_feedback(data)
    assert len(p["entries"]) == 16  # hard cap
    assert p["length"] == 2 + 16
    pids = [pid for pid, _ in p["entries"]]
    assert pids == [1000 + 20 * i for i in range(16)]


def test_nack_entries_across_seq_wrap():
    data = build_nack(SENDER, MEDIA, [65530, 65535, 0, 4])
    p = parse_rtcp_feedback(data)
    assert set(p["implied_seqs"]) == {0, 4, 65530, 65535}
    # the wrap arithmetic must land 65535 on BLP bit 4 of the 65530 entry
    assert p["entries"] == [(0, 0x0008), (65530, 0x0010)]


def test_pli_exact_bytes():
    data = build_pli(SENDER, MEDIA)
    assert len(data) == 12
    assert data[0] == 0x81  # V=2, P=0, FMT=1
    assert data[1] == 206   # PT=PLI
    assert int.from_bytes(data[2:4], "big") == 2
    assert int.from_bytes(data[4:8], "big") == SENDER
    assert int.from_bytes(data[8:12], "big") == MEDIA
    p = parse_rtcp_feedback(data)
    assert p["fmt"] == 1 and p["pt"] == 206 and p["entries"] == []


# ---------------------------------------------------------------------------
# Receiver-level NACK behaviour
# ---------------------------------------------------------------------------

def _drive_gap(rx: DrivenReceiver, missing: list, first: int = 100):
    """Deliver the packets around a set of missing sequence numbers:
    everything before/after each missing seq arrives, the missing ones don't,
    plus one later packet so every gap becomes discoverable."""
    seq = first
    for m in missing:
        while seq < m:
            rx.feed(seq, 3000, b"P" * 40)
            seq += 1
        seq = m + 1
    rx.feed(seq, 6000, b"Q" * 40, marker=True)  # tail past the last gap


def test_receiver_nacks_gap_after_reorder_wait():
    rx = DrivenReceiver()
    _drive_gap(rx, missing=[101])
    assert rx.rtcp_packets(205) == []  # nothing before the ~8 ms reorder wait
    time.sleep(0.012)
    rx.tick()
    nacks = rx.rtcp_packets(205)
    assert len(nacks) == 1
    p = parse_rtcp_feedback(nacks[0])
    assert p["fmt"] == 1 and p["pt"] == 205
    assert p["media_ssrc"] == 0x50110102
    assert p["implied_seqs"] == [101]
    assert rx.vr.get_stats()["nacks_sent"] == 1


def test_receiver_renack_throttled_to_15_ms():
    rx = DrivenReceiver()
    _drive_gap(rx, missing=[101])
    time.sleep(0.012)
    rx.tick()
    assert len(rx.rtcp_packets(205)) == 1
    rx.tick()  # immediate re-tick: within the 15 ms throttle -> nothing
    assert len(rx.rtcp_packets(205)) == 1
    time.sleep(0.016)
    rx.tick()
    assert len(rx.rtcp_packets(205)) == 2  # re-NACK allowed now
    same_seqs = (parse_rtcp_feedback(rx.rtcp_packets(205)[0])["implied_seqs"]
                 == parse_rtcp_feedback(rx.rtcp_packets(205)[1])["implied_seqs"])
    assert same_seqs


def test_receiver_nack_cap_16_entries_per_packet():
    """Many simultaneous spread gaps: every emitted feedback packet carries at
    most 16 PID/BLP entries and only implies actually-missing seqs."""
    rx = DrivenReceiver()
    rx.feed(100, 3000, b"P" * 30, marker=True)  # anchor: seeds the seq pointer
    missing = set()
    seq = 100
    for _ in range(40):  # 40 separate gaps of 16 missing seqs each
        missing.update(range(seq + 1, seq + 17))
        seq += 17
        rx.feed(seq, 3000, b"P" * 30, marker=True)  # the packet closing the gap
        seq += 1
    time.sleep(0.012)
    rx.tick()
    nacks = rx.rtcp_packets(205)
    assert nacks, "gaps must produce NACKs"
    for pkt in nacks:
        p = parse_rtcp_feedback(pkt)
        assert len(p["entries"]) <= 16
        assert len(p["implied_seqs"]) <= 16 * 16
        assert set(p["implied_seqs"]) <= missing
    assert rx.vr.get_stats()["nacks_sent"] == len(nacks)


def test_receiver_never_nacks_pt100_fec():
    """PT=100 FEC packets live in their own seq space: receiving them must
    never create media gaps or NACK feedback (only PT 96 gaps are NACKed)."""
    rx = DrivenReceiver()
    from helpers import build_fec_packet
    rx.feed(100, 3000, b"A" * 40, marker=True)
    # a FEC packet covering 100..103 while 101-103 are simply not there yet:
    # no media gap exists, so no NACK may be sent
    rx.feed_wire(build_fec_packet(100, [b"A" * 40, b"F1", b"F2", b"F3"], 3000))
    time.sleep(0.012)
    rx.tick()
    assert rx.rtcp_packets(205) == []
    assert rx.vr.get_stats()["nacks_sent"] == 0
    # ...and a genuine PT=96 gap is still NACKed afterwards
    rx.feed(103, 3000, b"B" * 40, marker=True)
    time.sleep(0.012)
    rx.tick()
    nacks = rx.rtcp_packets(205)
    assert nacks and parse_rtcp_feedback(nacks[-1])["implied_seqs"] == [101, 102]


def test_receiver_pli_on_start_and_rate_limit():
    rx = DrivenReceiver()
    rx.vr.force_pli("start")
    plis = rx.rtcp_packets(206)
    assert len(plis) == 1
    p = parse_rtcp_feedback(plis[0])
    assert p["pt"] == 206 and p["fmt"] == 1 and p["length"] == 2
    assert p["media_ssrc"] == 0x50110102
    rx.vr.force_pli("start")  # inside the 0.3 s rate limit -> suppressed
    assert len(rx.rtcp_packets(206)) == 1
