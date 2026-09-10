"""End-to-end video path: simulated iPhone -> real UDP -> VideoReceiver ->
VideoDecoder.

Encodes 45 frames of a moving 1280x720 test pattern with PyAV/libx264
(baseline profile, no B-frames), packetizes per RFC 6184 exactly like the
iOS app (STAP-A with SPS+PPS before each IDR, FU-A above 1200 bytes, marker
bit, +3000 ticks/frame on the 90 kHz clock), streams over UDP 127.0.0.1:9921
into a real VideoReceiver, deliberately drops 2-3 packets, and the fake phone
answers NACKs from its retransmit cache (PROTOCOL.md 3.2).
"""

from __future__ import annotations

import socket
import threading
import time
from fractions import Fraction
from typing import Dict, List, Tuple

import numpy as np
import pytest

import av

from helpers import RtpPacketizer, parse_rtcp_feedback, split_annexb_nals

from omnicam.decoder import VideoDecoder
from omnicam.net import VideoReceiver

MEDIA_SSRC = 0x0A0B0C0D
N_FRAMES = 45
WIDTH, HEIGHT, FPS = 1280, 720, 30

# ---------------------------------------------------------------------------
# H.264 encoding (PyAV).  Primary path: libx264 inside the av wheel.
# ---------------------------------------------------------------------------


def _pick_encoder() -> str:
    if "libx264" in av.codecs_available:
        return "libx264"
    for name in sorted(av.codecs_available):  # fallback: any H.264 encoder
        if "264" not in name:
            continue
        try:
            av.codec.Codec(name, "w")
            return name
        except Exception:
            continue
    pytest.fail("no H.264 encoder available in the av wheel")


def _make_pattern(i: int, w: int = WIDTH, h: int = HEIGHT) -> np.ndarray:
    """1280x720 BGR test pattern with a moving, ever-changing element."""
    img = np.empty((h, w, 3), dtype=np.uint8)
    img[:] = (40, 60, 80)
    box = 400
    x = (i * 23) % (w - box)
    y = 160
    val = 100 + (i * 3) % 150  # monotonic brightness ramp over 45 frames
    yy, xx = np.mgrid[y:y + box, x:x + box]
    tex = ((xx // 8 + yy // 8 + i) % 4) * 12
    img[y:y + box, x:x + box, 0] = np.clip(val + tex, 0, 255)
    img[y:y + box, x:x + box, 1] = np.clip(val // 2 + tex, 0, 255)
    img[y:y + box, x:x + box, 2] = np.clip(val // 3 + tex, 0, 255)
    return img


def _encode_stream() -> List[Tuple[int, bytes]]:
    """Returns [(rtp_ts, annexb_bytes)] for N_FRAMES, in order."""
    enc = av.CodecContext.create(_pick_encoder(), "w")
    enc.width = WIDTH
    enc.height = HEIGHT
    enc.pix_fmt = "yuv420p"
    enc.time_base = Fraction(1, 90000)
    enc.gop_size = 15  # IDR every 15 frames (keyint 60 scaled to 45-frame test)
    enc.options = {"profile": "baseline", "tune": "zerolatency",
                   "preset": "ultrafast", "crf": "26"}
    enc.open()
    out: List[Tuple[int, bytes]] = []
    for i in range(N_FRAMES):
        vf = av.VideoFrame.from_ndarray(_make_pattern(i), format="bgr24")
        vf = vf.reformat(format="yuv420p")
        vf.pts = i * 3000
        for pkt in enc.encode(vf):
            out.append((int(pkt.pts) if pkt.pts is not None else i * 3000, bytes(pkt)))
    for pkt in enc.encode(None):
        pts = pkt.pts if pkt.pts is not None else out[-1][0] + 3000 if out else 0
        out.append((int(pts), bytes(pkt)))
    assert len(out) >= N_FRAMES, f"encoder produced only {len(out)} frames"
    return out


# ---------------------------------------------------------------------------
# Packetization (mirrors the iOS sender, PROTOCOL.md 3.1)
# ---------------------------------------------------------------------------

def _packetize(frames: List[Tuple[int, bytes]]) -> Tuple[List[List[bytes]], Dict[int, List[int]]]:
    """Returns (per-frame datagram lists, {frame_index: [seq, ...]})."""
    pk = RtpPacketizer(ssrc=MEDIA_SSRC)
    sps = pps = None
    frame_dgrams: List[List[bytes]] = []
    frame_seqs: Dict[int, List[int]] = {}
    for idx, (ts, annexb) in enumerate(frames):
        nals = split_annexb_nals(annexb)
        assert nals, f"frame {idx} has no NALs"
        is_idr = any((n[0] & 0x1F) == 5 for n in nals)
        if is_idr and sps is None:
            for n in nals:
                if (n[0] & 0x1F) == 7:
                    sps = n
                elif (n[0] & 0x1F) == 8:
                    pps = n
            assert sps is not None and pps is not None, "keyframe without SPS/PPS"
        body = [n for n in nals if (n[0] & 0x1F) not in (7, 8)]
        pk.ts = ts  # encoder pts already runs on the 90 kHz clock
        dgrams = pk.packetize_frame(body, is_idr=is_idr,
                                    sps=sps if is_idr else None,
                                    pps=pps if is_idr else None)
        assert dgrams
        # marker bit must sit on the very last datagram of the frame
        assert dgrams[-1][1] & 0x80
        for d in dgrams[:-1]:
            assert not (d[1] & 0x80)
        frame_dgrams.append(dgrams)
        frame_seqs[idx] = [int.from_bytes(d[2:4], "big") for d in dgrams]
    return frame_dgrams, frame_seqs


# ---------------------------------------------------------------------------
# Fake phone: RTP sender with retransmit cache + NACK responder (3.2/3.3)
# ---------------------------------------------------------------------------

class FakePhoneRtp:
    def __init__(self, skip_first: set, drop_permanent: set) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.002)
        self.port = self.sock.getsockname()[1]
        self.cache: Dict[int, bytes] = {}
        self.skip_first = set(skip_first)
        self.drop_permanent = set(drop_permanent)
        self.nack_seqs: List[int] = []
        self.retransmits = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._nack_loop, daemon=True)
        self._thread.start()

    def send_frame(self, dgrams: List[bytes], sent_once: set) -> None:
        for wire in dgrams:
            seq = int.from_bytes(wire[2:4], "big")
            with self._lock:
                self.cache[seq] = wire
            if seq in self.drop_permanent:
                continue
            if seq in self.skip_first and seq not in sent_once:
                sent_once.add(seq)
                continue  # "lost": only recoverable via NACK retransmit
            self.sock.sendto(wire, ("127.0.0.1", 9921))

    def _nack_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            # RTCP: payload type is the full second byte; Generic NACK = 205/FMT 1
            if len(data) < 12 or data[1] != 205 or (data[0] & 0x1F) != 1:
                continue  # e.g. a PLI (PT 206): nothing to retransmit
            p = parse_rtcp_feedback(data)
            with self._lock:
                self.nack_seqs.extend(p["implied_seqs"])
                for seq in p["implied_seqs"]:
                    wire = self.cache.get(seq)
                    if wire is not None and seq not in self.drop_permanent:
                        self.sock.sendto(wire, addr)
                        self.retransmits += 1

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self.sock.close()


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------

def test_e2e_video_stream_decode_nack_recovery():
    enc_path = _pick_encoder()
    print(f"\nH.264 encode path: {enc_path} (in-wheel)")
    frames = _encode_stream()
    frame_dgrams, frame_seqs = _packetize(frames)

    # loss plan: first frame with >=2 packets after 15 -> 1 packet lost once,
    # recovered via NACK; frames 43 + 44 -> one packet each permanently lost
    # (drives the loss stats after the give-up window)
    idx_rec = next(i for i in range(16, N_FRAMES) if len(frame_seqs[i]) >= 2)
    recovered_seq = frame_seqs[idx_rec][1]
    lost_a = frame_seqs[N_FRAMES - 2][len(frame_seqs[N_FRAMES - 2]) // 2]
    lost_b = frame_seqs[N_FRAMES - 1][0]

    vr = VideoReceiver(fps=FPS)
    received: List[Tuple[bytes, dict]] = []
    rx_lock = threading.Lock()

    def on_frame(annexb: bytes, meta: dict) -> None:
        with rx_lock:
            received.append((annexb, meta))

    vr.set_frame_callback(on_frame)
    phone = FakePhoneRtp(skip_first={recovered_seq},
                         drop_permanent={lost_a, lost_b})
    vr.start()
    try:
        vr.set_session(MEDIA_SSRC, "127.0.0.1", FPS)
        # feedback targets the fake phone's NACK socket (localhost test harness)
        vr._feedback_addr = ("127.0.0.1", phone.port)

        sent_once: set = set()
        for dgrams in frame_dgrams:
            phone.send_frame(dgrams, sent_once)
            time.sleep(1.0 / FPS)  # ~30 fps pacing so 3-frame windows apply

        # wait until the stream settles: retransmits happened and no new frames
        deadline = time.monotonic() + 6.0
        last_count, last_retx = -1, -1
        while time.monotonic() < deadline:
            time.sleep(0.2)
            with rx_lock:
                n = len(received)
            retx = phone.retransmits
            if n == last_count and retx == last_retx and n >= N_FRAMES - 2:
                break
            last_count, last_retx = n, retx

        stats = vr.get_stats()
        with rx_lock:
            n_frames_rx = len(received)
        print(f"frames received: {n_frames_rx}, nacks: {stats['nacks_sent']}, "
              f"retransmits: {phone.retransmits}, dups: {stats['dup_packets']}, "
              f"drops: {stats['drops']}, loss: {vr.get_loss_pct():.2f}%")

        assert n_frames_rx >= N_FRAMES - 2, "receiver lost too many frames"
        assert stats["nacks_sent"] > 0, "deliberate loss must trigger NACKs"
        assert phone.retransmits > 0, "fake phone must have answered NACKs"
        assert recovered_seq in phone.nack_seqs
        assert vr.get_loss_pct() > 0.0, "permanent loss must show in loss stats"
    finally:
        vr.stop()
        phone.stop()

    # -- decode with the real VideoDecoder ---------------------------------
    dec = VideoDecoder()
    decoded: List[np.ndarray] = []
    with rx_lock:
        items = list(received)
    for annexb, _meta in items:
        decoded.extend(dec.decode(annexb))
    dec.close()

    print(f"decoded frames: {len(decoded)} (decoder reports {dec.decoded_frames})")
    assert len(decoded) >= 40, f"only {len(decoded)} frames decoded"
    assert dec.decoded_frames == len(decoded)
    first = decoded[0]
    assert first.shape == (HEIGHT, WIDTH, 3), f"bad dimensions {first.shape}"

    # content must vary over time (moving element -> brightness changes)
    means = [f.mean(axis=(0, 1)) for f in decoded]
    lum = [float(np.mean(m)) for m in means]
    assert max(lum) - min(lum) > 2.0, "frame content does not vary"
    k = min(20, len(lum) - 1)
    assert lum[k] > lum[0] + 0.5, "moving element did not brighten over time"
    assert lum[-1] > lum[0], "brightness must not return to the first value"
