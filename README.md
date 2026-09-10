# OmniCam

Turn your **jailbroken iPhone** into a wireless webcam for your **Windows PC** — designed to beat DroidCam and iVCam on latency, image quality, and control. Built specifically for an **iPhone 6 Plus (iOS 12.5.8, Amethyst jailbreak, AppSync Unified)**.

## What it does

- **Hardware-encoded H.264** (VideoToolbox, NV12 end-to-end) streamed over **RTP/UDP** with NACK retransmission, keyframe-on-loss recovery, and loss-based **adaptive bitrate** → **~60–110 ms glass-to-glass** on Wi-Fi (DroidCam: ~150–300 ms; it uses MJPEG-over-TCP which chokes on Wi-Fi loss).
- **Front + back camera** with near-instant switching (~150–300 ms). *Hardware truth:* the A8 physically cannot power both cameras at once (Apple-documented) — any app claiming otherwise on a 6 Plus is lying. OmniCam pre-arms both inputs and swaps faster than the competition can react.
- **Full filter suite** running in real time on the GPU: looks (Mono, Noir, Chrome, Fade, Instant, Process, Transfer, Sepia, Invert, False Color), adjustments (brightness/contrast/saturation/temperature/vibrance/gamma/sharpen/vignette), **skin-smoothing beauty filter**, stylize (pixelate, crystallize, hexagonal, twirl, bulge, bump, soft blur, zoom blur), mirror/flip/rotate/zoom/pan/crop, **.cube LUT import** (Filza "Open in OmniCam" or document picker), text + timecode overlays. Filters sync **bidirectionally** between phone and PC.
- **Filters sync bidirectionally** between phone and PC.
- **PC side**: discovery via UDP beacons, live preview, stats (fps/bitrate/loss/RTT/latency), local CPU adjustments, and a system-wide **virtual webcam** (OBS Virtual Camera, Unity Capture fallback) usable in Zoom, Teams, OBS, Discord, Chrome. Video-only — audio is intentionally not implemented; use your own microphone in the consumer app.

## Layout

| Path | What |
|---|---|
| `ios/` | The iPhone app (Objective-C, no third-party deps, iOS 12.0) — Theos Makefile + `Resources/` |
| `pc/` | The Windows receiver (Python 3.11: PySide6, PyAV, pyvirtualcam) |
| `docs/PROTOCOL.md` | The wire protocol both ends implement (ports, RTP layout, JSON messages, filter schema) |
| `docs/DECISIONS.md` | Verified research decisions & device constraints |
| `scripts/`, `project.yml`, `.github/workflows/` | Build tooling |
| `INSTALL.md` | **Start here** — build the .ipa, install via Filza, first run |

## Quick start (short version)

1. **A tested .ipa is already built**: `dist/OmniCam.ipa` (arm64, iOS 12.0, ldid fake-signed). To rebuild after changes: [WSL + Theos](INSTALL.md) (offline, recommended): `wsl bash scripts/build-wsl.sh`; or push to GitHub and let the included Action build it.
2. **Install** — Filza web server (`http://<phone-ip>:2222`) → upload the .ipa → tap → Install. No certificate, no 7-day timer (AppSync Unified).
3. **PC** — `cd pc && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt` and install [OBS Studio](https://obsproject.com) (for the virtual camera). Run `run.bat`.
4. **Stream** — open OmniCam on the phone, OmniCam PC on the desktop, click the device → Connect → Start Stream, then pick "OBS Virtual Camera" in Zoom/Teams/OBS.

> ⚠️ The app only launches **while jailbroken** — after every reboot, re-jailbreak first (Safari → `jbme.h4ck.kr`, ~20 s), then open OmniCam.

See [INSTALL.md](INSTALL.md) for the complete, step-by-step guide and troubleshooting.

## Documentation map

| Doc | Contents |
|---|---|
| [INSTALL.md](INSTALL.md) | Build the .ipa (WSL+Theos or GitHub Actions), Filza install, first run, troubleshooting |
| [pc/README.md](pc/README.md) | **Laptop setup from a fresh clone**, firewall, virtual camera, troubleshooting |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | The exact wire protocol both ends implement |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Verified research decisions and device constraints |

`dist/OmniCam.ipa` is the current compiled, tested build — re-buildable any
time with `wsl bash scripts/build-wsl.sh`, or on GitHub via the included
Actions workflow (manual trigger).
