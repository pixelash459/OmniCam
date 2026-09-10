"""OmniCam PC - Windows receiver for the OmniCam iOS streaming app.

Implements OmniCam Protocol v1 (see docs/PROTOCOL.md): UDP discovery on 9920,
RTP/UDP H.264 video on 9921 (NACK/PLI + optional FEC), TCP JSON control on
9923, low-latency preview, and mirroring into a system virtual webcam (OBS
Virtual Camera).  Video-only by design — see docs/PROTOCOL.md section 4.
"""

__version__ = "1.1.12"
__app_name__ = "OmniCam PC"
