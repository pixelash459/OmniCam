"""Live 720<->1080 retarget: the phone starts a new encoder with a NEW SSRC and
announces it in a fresh ``started``. Packets from the old SSRC that are still
in flight must not re-latch the reorder buffer (that froze video for ~50 % of
switches when the new random sequence space happened to sort "before" the old
one and every new packet was discarded as a late retransmission).
"""

from __future__ import annotations

from rx_driver import DrivenReceiver

OLD = 0x50110102
NEW = 0x0A0B0C0D
IDR = b"\x65" + b"\x01" * 40
P = b"\x41" + b"\x02" * 40


def test_old_ssrc_packets_ignored_after_started():
    rx = DrivenReceiver(media_ssrc=OLD)
    rx.feed(1000, 3000, IDR, marker=True, ssrc=OLD)
    assert len(rx.frames) == 1

    # phone retargets: new SSRC, sequence space below the old one
    rx.vr.set_session(NEW, "127.0.0.1", 30.0)
    rx.media_ssrc = NEW

    # stragglers from the old encoder arrive after the reset
    rx.feed(1001, 6000, P, marker=True, ssrc=OLD)
    rx.feed(1002, 9000, P, marker=True, ssrc=OLD)
    assert len(rx.frames) == 1  # ignored, and they must not latch _next_seq

    rx.feed(37, 90000, IDR, marker=True, ssrc=NEW)
    rx.feed(38, 93000, P, marker=True, ssrc=NEW)
    assert len(rx.frames) == 3
    assert rx.frames[-1][1]["first_seq"] == 38
    assert rx.vr._foreign_packets == 2


def test_new_ssrc_seq_below_old_is_not_dropped_as_late():
    rx = DrivenReceiver(media_ssrc=OLD)
    for s in range(30000, 30003):
        rx.feed(s, 3000 * s, IDR, marker=True, ssrc=OLD)
    assert len(rx.frames) == 3
    rx.vr.set_session(NEW, "127.0.0.1", 30.0)
    rx.media_ssrc = NEW
    rx.feed(30003, 3000 * 30003, P, marker=True, ssrc=OLD)  # straggler
    # new stream picks a seq far "behind" the old one
    rx.feed(5, 1000, IDR, marker=True, ssrc=NEW)
    rx.feed(6, 4000, P, marker=True, ssrc=NEW)
    assert len(rx.frames) == 5


def test_before_started_any_ssrc_is_accepted():
    rx = DrivenReceiver(media_ssrc=OLD)
    rx.vr.pause_feedback()  # no announced SSRC (Stop Stream)
    rx.feed(10, 1000, IDR, marker=True, ssrc=NEW)
    assert len(rx.frames) == 1


def test_fec_from_other_ssrc_ignored():
    from helpers import build_fec_packet

    rx = DrivenReceiver(media_ssrc=OLD, fec_ssrc=0x77)
    rx.feed(100, 3000, IDR, ssrc=OLD)
    rx.feed(102, 3000, P, marker=True, ssrc=OLD)
    fec_wrong = build_fec_packet(100, [IDR, P, P], ts=3000, ssrc=0x78)
    rx.feed_wire(fec_wrong)
    assert rx.vr._foreign_packets == 1
    assert rx.frames == []  # nothing recovered from a foreign FEC packet
