# OmniCam

> This was built for **personal use** on one setup (a jailbroken iPhone 6 Plus
> on iOS 12.5.8 + a Windows PC). It is **100% vibe-coded** — written with AI
> coding agents, not a product. Use it at your own risk; there is no promised
> support. Pull requests are welcome.

Turn a **jailbroken iPhone** into a wireless webcam for **Windows**. Hardware
H.264 from the phone, RTP over Wi-Fi, a dark desktop app with a live preview,
and an OBS Virtual Camera feed for Zoom / Teams / Discord.

Tested on **iPhone 6 Plus (iOS 12.5.8, Amethyst, AppSync Unified)** and
**Windows 10/11**. Other jailbroken iOS 12 devices may work; nothing else is
claimed.

## Requirements

| Side | Need |
|---|---|
| Phone | Jailbroken iPhone (iOS 12.x), **AppSync Unified**, **Filza**. Re-jailbreak after every reboot. |
| PC | Windows 10/11 x64. [OBS Studio](https://obsproject.com) once, for the virtual camera driver. |
| Network | Phone and PC on the **same LAN**. Guest Wi-Fi / AP isolation often blocks discovery. |

No Apple developer account. The `.ipa` is `ldid` fake-signed for AppSync.

## End-user install (Releases)

Binaries for each version live on the [Releases](https://github.com/pixelash459/OmniCam/releases) page:
`OmniCam.ipa` and `OmniCam-PC-<ver>-Setup.exe` (current: **1.2.2**).

1. **Phone** — open Filza’s web server (port 2222), upload the IPA from
   `http://<phone-ip>:2222`, tap **Install**. Or copy via 3uTools / iMazing.
2. **PC** — run the Setup.exe. Leave **Allow OmniCam through Windows Firewall**
   checked. Install OBS Studio and click **Start Virtual Camera** once if the
   driver is missing.
3. Jailbreak the phone if you just rebooted (Safari → `http://jbme.h4ck.kr`),
   then open **OmniCam** and allow the camera.
4. Open **OmniCam PC**. The phone should appear automatically. If not, type
   its IP and **Connect**. Tick **Autoconnect to last phone** if you want that
   next time.
5. **Start Stream**, then **Start Virtual Camera**. In Zoom/Teams pick
   **OBS Virtual Camera**.

Closing the window with **X** hides to the **system tray**; streaming keeps
running. Right-click the tray icon → **Quit OmniCam** to fully exit.

A copy of the IPA also sits in `dist/OmniCam.ipa` in this repo.

## Features

- Hardware VideoToolbox H.264, RTP/UDP with NACK + PLI and loss-based ABR.
  Glass-to-glass was about **60–110 ms** on the author’s Wi-Fi (not a
  guarantee).
- Front/back camera, torch, zoom, resolution/fps/bitrate from either end.
- GPU filters on the phone (looks, adjust, beauty, stylize, geometry, `.cube`
  LUTs, overlay) synced both ways; extra local adjust on the PC.
- LAN discovery (UDP 9920 beacons + probes), saved-phone list, autoconnect.
- Close-to-tray, redesigned dark UI, stats strip (fps, kbps, loss, RTT).
- Video only — use a separate mic in the meeting app.

## Troubleshooting

| Symptom | What to try |
|---|---|
| Phone never appears | Same Wi-Fi; app in the foreground. Windows often marks home Wi-Fi as **Public** — the installer firewall rule covers all profiles; reinstall 1.2.0+ or allow UDP **9920–9921** inbound for `OmniCam.exe`. Typing the IP always works (TCP **9923** outbound). |
| Connects, no video | Allow inbound UDP **9921**. Disable AP isolation. |
| Skin looks blue / clothes look orange in Zoom | That was BGR vs RGB into OBS. Use PC **1.2.1+**. |
| “Closed” but still in the tray | Intended. Quit from the tray or the status-bar **Quit** button. |
| App crashes on launch | Not jailbroken after reboot. Re-jailbreak, then open OmniCam. |
| Colours / preview fine, Zoom wrong | Start OmniCam’s virtual camera *after* OBS has registered the device; restart Zoom. |

Ports: **9920** UDP discovery, **9921** UDP video, **9923** TCP control.

## Build from source

- **iOS** — [INSTALL.md](INSTALL.md): `wsl bash scripts/build-wsl.sh` (Theos).
  Optional backup: Actions → **Build unsigned IPA** (manual).
- **PC** — [pc/README.md](pc/README.md): venv + `pip install -r requirements.txt`
  + `run.bat`. Installer: `pc\build-exe.bat` (needs Inno Setup 6).

Tests: `cd pc` then `.venv\Scripts\python.exe -m pytest tests -q`.

## Layout

| Path | What |
|---|---|
| `ios/` | Objective-C app, iOS 12.0, Theos |
| `pc/` | Windows receiver (Python 3.11+, tested on 3.13) |
| `docs/PROTOCOL.md` | Wire protocol |
| `docs/DECISIONS.md` | Device constraints and research notes |
| `INSTALL.md` | IPA build + Filza install |
| `.github/workflows/pc-ci.yml` | Windows tests + installer artifact |
| `.github/workflows/build-ipa.yml` | Manual unsigned IPA (Xcode 15.4) |

## Versioning

Each git tag `vX.Y.Z` should ship **both** artifacts on GitHub Releases.
The maintainer builds the IPA with Theos on a real device and the Setup.exe
locally; CI on Windows runs the test suite and can produce an installer
artifact. CI does not overwrite Releases.

## Changelog (high level)

- **1.2.2** — Close-to-tray, tray menu, new app icon.
- **1.2.1** — Virtual camera declared as BGR (fixes swapped red/blue in OBS consumers).
- **1.2.0** — LAN discovery (Public-network firewall + probes), saved phones, redesigned PC UI; iOS filter-panel sliders and NSNull save crash.
- **1.1.12** — TCP line-buffer bug that replayed control messages (stream glitches).
- **1.1.11** — Receiver follows the SSRC from `started`.
- **1.1.10** — Live 720↔1080 retarget without tearing the encode graph.
- **1.1.9** — Bidirectional session state (camera / res / fps / kbps / ABR / torch / zoom).

## License

[MIT](LICENSE).

## Acknowledgements

[Theos](https://theos.dev), [PySide6](https://doc.qt.io/qtforpython-6/),
[PyAV](https://github.com/PyAV-Org/PyAV), [pyvirtualcam](https://github.com/letmaik/pyvirtualcam),
[OBS Studio](https://obsproject.com).
