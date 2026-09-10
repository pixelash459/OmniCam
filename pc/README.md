# OmniCam PC (Windows 11 receiver)

Python 3.11+ receiver for the **OmniCam** iOS app (tested on 3.13). Discovers
the phone on the LAN (UDP beacons), streams hardware-encoded **H.264 over
RTP/UDP** (with NACK retransmit + PLI + optional XOR FEC), decodes it
low-latency in-process (PyAV/FFmpeg), shows a preview, and mirrors it into
**OBS Virtual Camera** (system webcam) so Zoom/Teams/Meet see it as a webcam.
Video-only — audio is intentionally not implemented (v1.1); use your own
microphone in the consumer app.

## Ready-made installer (no Python needed)

Download **`OmniCam-PC-*-Setup.exe`** from the [GitHub Releases page](https://github.com/pixelash459/OmniCam/releases) (or build it locally with `pc\build-exe.bat`):

1. Keep **Allow OmniCam through Windows Firewall** checked (UDP 9920–9921 so the phone can be discovered).
2. Finish → desktop / Start menu shortcut **OmniCam PC**.
3. Optional but required for Zoom/Teams webcam: install [OBS Studio](https://obsproject.com) once and click **Start Virtual Camera** in OBS.
4. SmartScreen may show *More info → Run anyway* (the installer is unsigned).

This is an **onedir** install under `Program Files\OmniCam` — it does **not** unpack to `%TEMP%` on every launch (that old single-file `.exe` is what made laptops feel broken). Rebuild after code changes with `pc\build-exe.bat`.

## 0. Fresh laptop setup (from a clone of this repo)

```bat
git clone https://github.com/pixelash459/OmniCam.git
cd OmniCam\pc
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
run.bat
```

That's the whole install — the client runs from source, there is no build
step. Also install **OBS Studio** once (section 3) for webcam output, and
allow Python through Windows Firewall when prompted (section 4). Optional
self-check that everything is wired correctly:

```bat
.venv\Scripts\python -m pytest tests\ -q
```

Ports (OmniCam Protocol v1, see `../docs/PROTOCOL.md`):

| Port | Protocol | Direction | Purpose |
|---|---|---|---|
| 9920 | UDP | phone -> broadcast | discovery beacons (`OMNICAM1`, 1/s) |
| 9921 | UDP | phone -> PC | video RTP (PT 96) + FEC (PT 100); NACK/PLI feedback out |
| 9923 | TCP | PC -> phone | JSON control (newline-delimited) |

## 1. Install Python

Skip if already installed. Download from <https://www.python.org/downloads/>
(3.11 or newer — the project is tested on 3.13). During setup tick
**"Add python.exe to PATH"**. Verify:

```bat
python --version
```

## 2. Create the virtualenv and install dependencies

Open *Command Prompt* in this folder (`pc\`) and run:

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Pinned packages: `PySide6` (UI), `av` (PyAV, H.264 decode),
`pyvirtualcam` (virtual webcam), `numpy` (frame processing).

## 3. Install virtual camera

### OBS Virtual Camera (REQUIRED for webcam output)

Install **OBS Studio**: <https://obsproject.com>. It registers the
**"OBS Virtual Camera"** DirectShow filter that `pyvirtualcam` drives via the
`obs` backend. OBS itself never needs to be running for other apps to use the
camera, but if the device does not appear, start OBS once and click
**Start Virtual Camera** (right side, Controls dock), then restart the consumer
app (Zoom/Teams/...). This is the default backend used by OmniCam PC.

### Unity Capture (optional fallback)

<https://github.com/schellingb/UnityCapture> - build/install the filter and
run `install.bat` from its `UnityCaptureFilter` folder. OmniCam PC
automatically falls back to the `unitycapture` backend when OBS is missing.

## 4. Windows Firewall

On first launch Windows asks to allow Python on private networks - click
**Allow**. OmniCam PC *receives* on UDP 9920/9921 and *connects out* to
TCP 9923, so inbound UDP must be allowed. To add the rules manually
(administrator Command Prompt):

```bat
netsh advfirewall firewall add rule name="OmniCam PC UDP In" dir=in action=allow protocol=UDP localport=9920-9921 profile=any
netsh advfirewall firewall add rule name="OmniCam PC TCP Out" dir=out action=allow protocol=TCP localport=9923 profile=any
```

## 5. Run

```bat
run.bat
```

(or `.venv\Scripts\python -m omnicam`). Then:

1. Start OmniCam on the iPhone (jailbroken device; re-jailbreak after every
   reboot first). The phone appears in the **Devices** list within ~1 s.
2. Select it (or type its IP under manual IP - guest/AP-isolated Wi-Fi
   networks block broadcast) and click **Connect**.
3. Pick resolution/FPS/bitrate (defaults: 1280x720, 30 fps, 3000 kbps) and
   click **Start Stream**.
4. Optional: **Start Virtual Camera** (mirrors the stream as a webcam).

## Troubleshooting

- **No device found** — guest Wi-Fi / AP isolation blocks broadcast, **or**
  Windows classified the LAN as **Public** and blocked UDP 9920. Type the
  iPhone IP (Settings → Wi-Fi → ⓘ) and Connect. Reinstall 1.2.0+ so the
  firewall rule uses `profile=any`. Phone app must be in the foreground.
- **Colours swapped in Zoom/Teams (blue skin)** — fixed in 1.2.1 (BGR vs RGB
  into OBS). Update the PC app.
- **Closing X does not quit** — the app hides to the tray. Right-click the
  tray icon → Quit OmniCam, or use the status-bar Quit button.
- **Connect fails / keeps retrying** - check firewall rules above, verify the
  phone shows OmniCam running; only one PC client at a time (a second
  connection gets `{"t":"error","code":"busy"}`).
- **Webcam not visible in Zoom/Teams/OBS** - install OBS Studio (see above),
  then restart the consumer app so it re-enumerates cameras. Try selecting
  "OBS Virtual Camera" explicitly in the app's device list. Unity Capture's
  device is called "Unity Video Capture".
- **High latency / stutter** - prefer **5 GHz** Wi-Fi for both devices, keep
  the PC off VPN, lower the bitrate combo (e.g. 2000 kbps), or drop to 720p.
  The phone adapts bitrate automatically (Auto ABR) when loss is reported via
  `rr` every 500 ms.
- **Green/gray blocks after joining** - the decoder waits for the next IDR
  (keyframe every `keyint=60` frames, SPS/PPS re-sent per IDR; the receiver
  also sends PLI on start, on >10 % sustained loss and on decode stalls).
- **App works only while jailbroken** - after a reboot, re-jailbreak via
  Safari `http://jbme.h4ck.kr` before launching OmniCam (see
  `../docs/DECISIONS.md`).

## Layout

```
pc\
  requirements.txt      pinned dependencies
  run.bat               launcher (uses .venv when present)
  omnicam\
    __main__.py         python -m omnicam entry
    net.py              beacons, TCP control, video receiver, NACK/PLI/FEC
    decoder.py          PyAV H.264 decoder
    virtualcam_out.py   pyvirtualcam wrapper (OBS -> Unity Capture fallback)
    stats.py            fps/bitrate/loss/RTT/glass-to-glass estimation
    app.py              orchestration + decode threads
    ui.py               PySide6 dark UI
  tests\                pytest suite (protocol, UI smoke, tray, settings)
  OmniCam.spec          PyInstaller onedir spec
  build-exe.bat         icon + freeze + Inno Setup
```

Tests: `.venv\Scripts\python -m pytest tests\ -q` (needs `pytest`,
`pip install pytest`).
