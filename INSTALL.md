# OmniCam — Build & Install Guide

Target setup this guide is written for: **iPhone 6 Plus (iPhone7,1, arm64), iOS 12.5.8**, jailbroken with **Amethyst** (semi-untethered), **AppSync Unified** installed, **Sileo / Zebra / Filza** present, **Windows 11** PC. No Mac, no Apple developer account — every artifact here is an **unsigned/fake-signed (.ipa) sideload**.

---

## (a) Building the .ipa — two paths

### Path 1 — WSL + Theos (PRIMARY, fully offline from Apple)

One-time setup (PowerShell, admin not required):

```powershell
wsl --install -d Ubuntu     # once; reboot when asked, create a UNIX user
```

Then, from the repo root (`E:\Vibe ios apps\OmniCam`) in Windows Terminal:

```bash
wsl bash scripts/build-wsl.sh
```

What the script does:

1. Installs **Theos** if missing — fully user-space in `~/theos`, **no sudo / apt needed**: theos core + Procursus `ldid` static binary + the official Theos-for-Linux cross-clang (`L1ghtmann/llvm-project` iOSToolchain) + the `iPhoneOS12.4` SDK from `theos/sdks`. First run downloads ~1 GB once.
2. Builds with `make package FINALPACKAGE=1` from `ios/` — `TARGET = iphone:clang:12.4:12.0`, `ARCHS = arm64`, `THEOS_PACKAGE_FORMAT = ipa`. Theos **fake-signs the binary with `ldid -S` automatically**.
3. Copies the result to **`dist/OmniCam.ipa`** (the versioned original stays in `ios/packages/local.omnicam.app_1.1.0.ipa`).

Output artifact: `dist/OmniCam.ipa`.

### Path 2 — GitHub Actions (BACKUP)

Requires the repo to be on GitHub.

1. Push the repo to GitHub (`main`, or any tag).
2. Actions tab → **Build unsigned IPA** → *Run workflow* (or just push a tag — it triggers on `workflow_dispatch` and tag pushes).
3. Wait for the green run (`macos-14` + **Xcode 15.4** — the last Xcode able to target iOS 12 — + `xcodegen` + `ldid`), then download the artifact **`OmniCam-unsigned.ipa`** from the run summary page.

The recipe is exactly: `xcodegen generate` → `xcodebuild ... CODE_SIGNING_ALLOWED=NO CODE_SIGN_IDENTITY="" ARCHS=arm64 IPHONEOS_DEPLOYMENT_TARGET=12.0` → assemble `Payload/OmniCam.app` → `ldid -S` main binary + any embedded frameworks → `zip -r OmniCam.ipa Payload`.

> Note: `macos-14` is deprecated on GitHub Actions. Theos-in-WSL (Path 1) is the long-term build path; this workflow only exists as a backup.

---

## (b) Install on the phone (Filza web server — no USB, no trust prompt)

> **Skip the build:** a freshly compiled and verified `OmniCam.ipa` already ships in
> `dist/` of this repository — produced by the WSL+Theos pipeline (arm64, min iOS 12.0,
> `ldid -S` fake-signed, video-only v1.1.0). Jump straight to step (b) below.

Because **AppSync Unified** is installed, ipas install permanently: **no developer-mode trust prompt, no 7-day expiry**.

### Wireless route (recommended)

1. On the phone, open **Settings → Wi-Fi** and note the IP (tap the ⓘ next to the network), e.g. `192.168.1.42`.
2. On the phone, open **Filza** and enable its **web server** (toggle in Filza's bottom-bar gear / settings). Filza listens on port **2222**.
3. On the Windows PC, open a browser at `http://<phone-ip>:2222` (e.g. `http://192.168.1.42:2222`).
4. Use the Filza web UI to **upload `OmniCam.ipa`** (drop it into e.g. `/var/mobile/Documents/`).
5. On the phone, open Filza, navigate to the uploaded file, **tap `OmniCam.ipa` → Install**.
6. When it finishes, the **OmniCam icon appears on the home screen**. Done — no Settings → General → Profiles/Device Management step, no 7-day timer.

### USB route (alternative)

- **3uTools**: Apps → **Import & Install IPA** → pick `OmniCam.ipa`.
- **iMazing**: Apps → **Copy to Device** → pick the .ipa.
Both work the same way as Filza once AppSync is present; the file just travels over the cable instead of Wi-Fi.

---

## (c) CRITICAL notes — read before first launch

1. **The app only runs while jailbroken.** Amethyst is semi-untethered: after **every reboot**, code-signing enforcement comes back and OmniCam will crash instantly at launch. **Re-jailbreak first**: Safari → **`http://jbme.h4ck.kr`** → wait ~20 s until the jailbreak completes → then open OmniCam.
2. **Keep the ldid fake-signature.** AppSync accepts even fully unsigned binaries, but unsigned binaries are known to **crash at launch on iOS 12.4**. Always ship `ldid -S`-signed builds:
   - Path 1 (Theos) and Path 2 (workflow) already do this automatically.
   - Re-sign any existing .ipa/.app any time with: `bash scripts/fakesign.sh <OmniCam.ipa>`.
3. **Icon**: after reinstalling v1.1.1+, the icon appears on its own. If an older install still shows a blank icon, run once in a phone terminal (NewTerm/MTerminal, as root):
   `uicache -p "$(find /var/containers/Bundle/Application -name OmniCam.app -maxdepth 2)"` (or just `uicache -a`), then respring if needed.
4. **Allow the permission prompt on first run**: camera ("OmniCam streams the camera over Wi-Fi to your PC."). If you ever deny it: Settings → General → Reset → **Reset Location & Privacy**, then relaunch.
4. Streaming needs the phone and PC on the **same Wi-Fi network**. If your router has "AP/client isolation" enabled, discovery and streaming both fail (see troubleshooting).

---

## (d) First-run walkthrough

1. **Phone**: re-jailbreak if you rebooted (Safari → `jbme.h4ck.kr`), open **OmniCam**, tap **Allow** on the camera prompt. Leave it open in the foreground.
2. **PC**: open **OmniCam PC** (the Python client in `pc/`). The phone appears in the device list within a second or two (UDP beacon, port 9920). If it doesn't, enter the phone's IP manually.
3. Click **Connect**, then **Start Stream**. The preview window shows the phone camera (720p30 H.264 over RTP/UDP — glass-to-glass ≈ 60–110 ms).
4. Use OmniCam as a webcam in any app: select **OBS Virtual Camera** as the camera device (OmniCam PC feeds it via `pyvirtualcam`):
   - **Zoom**: Settings → Video → Camera → *OBS Virtual Camera*.
   - **Teams**: Settings → Devices → Camera → *OBS Virtual Camera*.
   - **OBS**: add a *Video Capture Device* source → *OmniCam Virtual Camera* is not needed — OmniCam PC feeds OBS directly if you prefer; either way works.
5. While streaming you can switch front/back camera, push filter/LUT state, set bitrate, or force a keyframe from the PC; the phone applies and mirrors the state (see `docs/PROTOCOL.md`).

Tip: `.cube` LUTs can be sent to the phone by any file route (Filza upload, iTunes File Sharing is enabled, or "Open in OmniCam" from the share sheet — the app declares the `cube` document type).

---

## (e) Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| OmniCam crashes instantly at launch | Not jailbroken (device rebooted) | Re-jailbreak: Safari → `jbme.h4ck.kr`, ~20 s, then launch the app |
| Still crashes after re-jailbreak | Binary lost its fake signature, or the ipa was rebuilt without `ldid -S` | `bash scripts/fakesign.sh OmniCam.ipa`, reinstall via Filza |
| "Unable to install" in Filza | Corrupt download, or a simulator/armv7 ipa | Re-upload the ipa; confirm it contains `Payload/OmniCam.app` with an `arm64` binary (unzip and check `lipo -info` on PC) |
| OmniCam icon never appears | AppSync Unified missing/disabled | Sileo → check **AppSync Unified** is installed, reinstall the ipa |
| Filza web page won't load from PC | Web server off, wrong IP, or router client isolation | Toggle Filza's web server; verify the IP in Settings → Wi-Fi; ping the phone; use the USB route instead |
| PC doesn't see the phone in OmniCam PC | AP isolation, or firewall blocking UDP 9920 | Enter the phone IP manually; allow UDP 9920–9921 + TCP 9923 through Windows Firewall |
| Connects but no video | Router blocks UDP or heavy loss | OmniCam's NACK/FEC handles moderate loss; check PC firewall for inbound UDP 9921 |
| No "OBS Virtual Camera" in Zoom/Teams | OmniCam PC not running, or app started before the virtual cam | Start OmniCam PC first, restart the meeting app afterwards |
| App runs in a small letterboxed window | Launch storyboard not picked up (rare, Theos builds only; iOS caches launch screens) | Reboot the phone once (then re-jailbreak); if it persists, install the GitHub-Actions-built ipa, whose launch storyboard is ibtool-compiled |
| Camera prompt never appeared, black preview | Permission denied earlier, or low-power camera kill | Settings → General → Reset → Reset Location & Privacy, relaunch; disable Low Power Mode |
| Phone Wi-Fi drops during long streams | Wi-Fi power management on A8 | Keep the phone on the charger; Settings → display auto-lock off while streaming |

---

## File map (packaging)

| File | Purpose |
|---|---|
| `ios/Makefile` | Theos app build (`TARGET = iphone:clang:12.4:12.0`, arm64, `THEOS_PACKAGE_FORMAT = ipa`) |
| `ios/control` | Package control — **required** by Theos' ipa format; `Package:`/`Version:` name the output ipa |
| `ios/Resources/Info.plist` | The single shared Info.plist (bundle id `local.omnicam.app`, camera usage string, `.cube` document type). Used by BOTH Theos and XcodeGen |
| `ios/Resources/LaunchScreen.storyboard` | Minimal blank black launch screen |
| `project.yml` | XcodeGen spec for the backup Xcode/CI build (rewrites `ios/Resources/Info.plist` deterministically on `xcodegen generate`) |
| `.github/workflows/build-ipa.yml` | macos-14 + Xcode 15.4 unsigned-ipa CI (backup path) |
| `scripts/fakesign.sh` | Re-`ldid -S` any .ipa/.app and re-zip |
| `scripts/build-wsl.sh` | One-command Theos build in WSL → `dist/OmniCam.ipa` |
