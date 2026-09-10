"""Low-latency decoding of the OmniCam RTP payloads via PyAV (FFmpeg).

- :class:`VideoDecoder` decodes complete Annex-B H.264 frames sequentially
  (hardware-encoded on the phone) and yields BGR24 numpy arrays.
"""

from __future__ import annotations

import logging
from typing import List

import numpy as np

import av

log = logging.getLogger("omnicam.decoder")

# PyAV >= 15 renamed ``av.error.AVError`` to ``av.FFmpegError``; support both.
FFmpegError: type = getattr(av, "FFmpegError", None) \
    or getattr(getattr(av, "error", None), "AVError", Exception)

__all__ = ["VideoDecoder"]


class VideoDecoder:
    """Sequential Annex-B H.264 frame decoder tuned for low latency.

    PyAV exposes decoder configuration through ``CodecContext.options`` (an
    AVDictionary applied when the codec opens on the first decode) plus the
    ``thread_count`` / ``thread_type`` properties, which is the cleanest
    supported way to get single-threaded low-delay decoding.
    """

    def __init__(self) -> None:
        self._ctx = av.CodecContext.create("h264", "r")
        try:
            # AV_CODEC_FLAG_LOW_DELAY + single decode thread; accepted as
            # AVOption strings validated by FFmpeg at open time.
            self._ctx.options = {"flags": "+low_delay", "threads": "1"}
        except Exception as exc:  # never fatal: decode still works
            log.debug("h264 low-delay options rejected (%s); using defaults", exc)
        try:
            self._ctx.thread_count = 1
            self._ctx.thread_type = "SLICE"
        except Exception as exc:
            log.debug("thread configuration rejected (%s)", exc)
        self._decoded_frames = 0

    @property
    def decoded_frames(self) -> int:
        """Total number of frames successfully decoded so far."""
        return self._decoded_frames

    def decode(self, annexb: bytes) -> List[np.ndarray]:
        """Decode one complete Annex-B frame; returns a list of BGR24 arrays
        (usually one, empty on transient/bitstream errors -- never raises)."""
        if not annexb:
            return []
        try:
            frames = self._ctx.decode(av.Packet(annexb))
        except FFmpegError as exc:
            # corrupted/no complete frame yet (e.g. join mid-GOP before IDR)
            log.debug("h264 decode error ignored: %s", exc)
            return []
        except (ValueError, MemoryError) as exc:
            log.warning("h264 packet rejected: %s", exc)
            return []
        out: List[np.ndarray] = []
        for frame in frames:
            try:
                out.append(frame.to_ndarray(format="bgr24"))
                self._decoded_frames += 1
            except (ValueError, MemoryError) as exc:
                log.debug("frame conversion failed: %s", exc)
        return out

    def close(self) -> None:
        """Flush the decoder; safe to call multiple times."""
        try:
            self._ctx.close()
        except Exception:
            pass
