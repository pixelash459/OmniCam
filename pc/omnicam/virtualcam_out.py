"""Virtual webcam output via pyvirtualcam.

Tries the ``obs`` backend (OBS Virtual Camera, installed with OBS Studio)
first and falls back to ``unitycapture``.  Frames are consumed with
latest-wins semantics: the sender thread always picks up the most recent
BGR frame and never queues stale ones.  If the frame geometry changes
(live 720<->1080, local rotation) the frame is scaled into the size the
device was opened with; the device itself is never reopened mid-session.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Tuple

import numpy as np

log = logging.getLogger("omnicam.vcam")

INSTALL_HINT = (
    "No usable virtual camera backend was found. Install OBS Studio "
    "(https://obsproject.com - provides the 'OBS Virtual Camera' driver, "
    "click Start Virtual Camera once inside OBS if prompted) and restart "
    "this app plus any consumer app (Zoom/Teams/OBS...). Optional fallback: "
    "Unity Capture (https://github.com/schellingb/UnityCapture)."
)

BACKENDS = ("obs", "unitycapture")


class VirtualCamError(RuntimeError):
    """Raised when no virtual camera backend can be opened."""


def _fit_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    """Return ``frame`` (BGR HxWx3) scaled to fit ``width``x``height``,
    aspect preserved, centred on black. Uses PyAV's swscale (already a
    dependency) and falls back to nearest-neighbour numpy indexing."""
    h, w = frame.shape[:2]
    if (w, h) == (width, height):
        return frame
    scale = min(width / float(w), height / float(h))
    nw = max(2, int(round(w * scale)) // 2 * 2)
    nh = max(2, int(round(h * scale)) // 2 * 2)
    try:
        import av  # PyAV, in-process swscale
        vf = av.VideoFrame.from_ndarray(np.ascontiguousarray(frame), format="bgr24")
        scaled = vf.reformat(width=nw, height=nh, format="bgr24").to_ndarray()
    except Exception:
        ys = (np.arange(nh) * (h / float(nh))).astype(np.intp)
        xs = (np.arange(nw) * (w / float(nw))).astype(np.intp)
        scaled = frame[ys][:, xs]
    if (nw, nh) == (width, height):
        return scaled
    out = np.zeros((height, width, 3), dtype=frame.dtype)
    y0 = (height - nh) // 2
    x0 = (width - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = scaled
    return out


class VirtualCamOut:
    """Thread-safe latest-frame virtual camera writer.

    ``submit()`` never blocks and never queues: only the newest frame is
    kept, the sender thread (paced by pyvirtualcam's
    ``sleep_until_next_frame``) sends it to the DirectShow virtual device.
    """

    def __init__(self, on_status: Optional[Callable[[str], None]] = None) -> None:
        self._on_status = on_status
        self._cam = None                      # pyvirtualcam.Camera
        self._backend: Optional[str] = None
        self._dims: Optional[Tuple[int, int, int]] = None
        self._lock = threading.Lock()
        self._newest: Optional[np.ndarray] = None
        self._frame_evt = threading.Event()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fps = 30.0
        self._sent = 0

    # -- properties --------------------------------------------------------
    @property
    def running(self) -> bool:
        """True while the virtual camera is open."""
        return self._cam is not None

    @property
    def backend(self) -> Optional[str]:
        """Active pyvirtualcam backend name ('obs' or 'unitycapture')."""
        return self._backend

    @property
    def dims(self) -> Optional[Tuple[int, int, int]]:
        """Open camera dimensions as (width, height, fps)."""
        return self._dims

    @property
    def frames_sent(self) -> int:
        """Total frames pushed to the virtual device."""
        return self._sent

    # -- lifecycle ---------------------------------------------------------
    def start(self, width: int, height: int, fps: float) -> str:
        """Open the virtual camera; returns the backend name used.

        Raises :class:`VirtualCamError` with an actionable message when
        pyvirtualcam is missing or no backend is installed.
        """
        if self._cam is not None:
            self.stop()
        try:
            import pyvirtualcam  # deferred: user-friendly failure message
        except Exception as exc:
            raise VirtualCamError(
                f"pyvirtualcam is not installed ({exc}). Run: "
                "pip install pyvirtualcam  -  and " + INSTALL_HINT
            ) from exc
        errors = []
        for backend in BACKENDS:
            try:
                cam = pyvirtualcam.Camera(width=int(width), height=int(height),
                                          fps=float(fps), backend=backend)
            except Exception as exc:
                errors.append(f"{backend}: {exc}")
                continue
            self._cam = cam
            self._backend = backend
            self._fps = float(fps)
            self._dims = (int(width), int(height), int(fps))
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._run, name="vcam-sender", daemon=True)
            self._thread.start()
            info = ""
            try:
                info = str(cam.device)
            except Exception:
                pass
            log.info("virtual camera started: backend=%s %dx%d@%d %s",
                     backend, width, height, int(fps), info)
            self._status(f"virtual camera: {backend} {width}x{height}@{int(fps)}")
            return backend
        raise VirtualCamError(
            "Could not open a virtual camera. Tried " + ", ".join(BACKENDS) +
            ". " + " | ".join(errors) + "  " + INSTALL_HINT)

    def stop(self) -> None:
        """Stop the sender thread and close the virtual camera."""
        self._stop_evt.set()
        self._frame_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        cam, self._cam = self._cam, None
        self._backend = None
        self._dims = None
        if cam is not None:
            try:
                cam.close()
            except Exception:
                pass
        self._status("virtual camera: stopped")

    # -- frame feed --------------------------------------------------------
    def submit(self, frame_bgr: np.ndarray) -> None:
        """Hand the newest frame to the sender (non-blocking, latest-wins)."""
        if self._cam is None or frame_bgr is None or frame_bgr.ndim != 3:
            return
        with self._lock:
            self._newest = frame_bgr
        self._frame_evt.set()

    # -- internals ---------------------------------------------------------
    def _run(self) -> None:
        """Sender loop paced by the virtual camera's target fps."""
        while not self._stop_evt.is_set():
            if not self._frame_evt.wait(0.2):
                continue
            self._frame_evt.clear()
            with self._lock:
                frame = self._newest
                self._newest = None
            if frame is None:
                continue
            cam = self._cam
            if cam is None:
                break
            h, w = frame.shape[:2]
            if (w, h) != (cam.width, cam.height):
                # Stream geometry changed (live 720<->1080, rotation). The device
                # keeps the size it was opened with - consumers (Zoom/OBS/Teams)
                # negotiated that format and a reopen mid-call breaks them - so
                # letterbox/scale the frame into the opened size instead.
                frame = _fit_frame(frame, cam.width, cam.height)
            try:
                self._cam.send(np.ascontiguousarray(frame))
                self._cam.sleep_until_next_frame()
                self._sent += 1
            except Exception as exc:
                log.exception("virtual camera send failed")
                self._status(f"virtual camera error: {exc}")
                self.stop()
                return

    def _status(self, text: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(text)
            except Exception:
                log.exception("on_status callback failed")
