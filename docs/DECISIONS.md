# OmniCam — Research Decisions (verified, 2026-09)

Target device: **iPhone 6 Plus (iPhone7,1, A8, arm64), iOS 12.5.8**, jailbroken with **Amethyst** (semi-untethered; re-jailbreak after every reboot via Safari `http://jbme.h4ck.kr`). **AppSync Unified** installed → permanent unsigned .ipa installs via Filza. Paired **Windows 11** PC. No Mac, no dev account.

## Hard constraints (verified)

1. **No simultaneous front+back capture** — `AVCaptureMultiCamSession` needs iOS 13+ AND A12+; on A8 the camera HAL powers one camera at a time (Apple WWDC 2019-249; historic SO evidence: dual sessions alternate/freeze). → Ship **single-session instant switch** (both inputs pre-created, begin/commitConfiguration swap, ~150-300 ms gap), never attempt dual sessions.
2. **Front cam max 720p@30** (fixed focus, no torch); **back cam 1080p@30/60** (OIS). Safe sustained target: **720p30**, 1080p30 optional.
3. **No HEVC HW encode on A8** → H.264 Main via `VTCompressionSession` (HW), NV12 (`420v`) buffers end-to-end.
4. **Xcode 16+/26 cannot target iOS 12** (min iOS 15). Last capable: **Xcode 15.4** (macos-14 CI image). → **Primary build path: Theos in WSL** (Windows officially supported via WSL; theos/sdks ships iPhoneOS12.4.sdk; `THEOS_PACKAGE_FORMAT=ipa` produces a fake-signed .ipa via ldid directly).
5. **Objective-C, no third-party deps** — Swift 5 works on iOS 12.2+ but couples us to Xcode ≤15.4 and weakens Theos. AppSync installs unsigned ipas, but community reports launch crashes on iOS 12.4 for unsigned binaries → **always `ldid -S` fake-sign**.
6. **No special entitlements** needed for camera/network. Info.plist needs `NSCameraUsageDescription`; no scene manifest (pre-iOS13 pattern); minimal launch storyboard included for safety. Privacy manifests are App-Store-only concerns — irrelevant for Filza sideloads.
7. App works **only while jailbroken** (code-signing enforcement returns after reboot) — document the re-jailbreak ritual.

## Streaming design (beats DroidCam/iVCam)

- DroidCam = MJPEG over TCP (bandwidth hog, TCP head-of-line blocking on Wi-Fi loss, ~150-300 ms). iVCam = H.264 over proprietary TCP (~100-200 ms, USB recommended).
- OmniCam = **hardware H.264 → RTP/UDP (RFC 6184) → NACK retransmit + PLI (RFC 4585) → optional XOR FEC → loss-based ABR (GCC-style)**. TCP used only for control JSON. UDP broadcast discovery + manual-IP fallback. Full spec: [PROTOCOL.md](PROTOCOL.md).
- Latency budget ≈ **60-110 ms** glass-to-glass.
- Audio: intentionally NOT implemented — removed in v1.1; OmniCam is video-only, consumer apps use their own PC microphones.

## PC receiver (Windows 11)

- **Python 3.11**: custom RFC 6184 depacketizer + reorder/NACK (adapted from aiortc's approach), **PyAV** in-process H.264 decode, **pyvirtualcam → OBS Virtual Camera** (user-mode DirectShow filter, no driver signing; fallback backend: Unity Capture), **PySide6** UI.
- PC-side local adjustments (brightness/contrast/mirror/rotate) before virtual-cam send — parity with DroidCam's client.

## Filter pipeline (all CIFilters verified present on iOS 12; no CIBilateral — use high-pass recipe)

Order: geometry (crop/zoom/rotate/mirror) → adjust (CIColorControls, CITemperatureAndTint, CIVibrance, CIGammaAdjust, CIUnsharpMask, CIVignette) → look (7 CIPhotoEffect*, sepia, invert, false color) → LUT (CIColorCube, 32³ RGBA float premultiplied, blue-fastest) → stylize (CIPixellate, CICrystallize, CIHexagonalPixellate, CITwirl/Bulge/BumpDistortion, soft CIGaussianBlur r≤10; CIZoomBlur/CIGloom risky at 1080p) → beauty (high-pass: `rgb − blur(r=8) + 0.5`, exposure-mask, CIBlendWithMask; YUCIHighPassSkinSmoothing recipe — 60 fps on iPhone 5s at preview res) → overlay (cached CIImage composite). Metal-backed `CIContext` (iOS 9+), render into encoder's CVPixelBuffer pool. Budget: 1-3 single-pass filters at 720p30 comfortable on A8.

## Build & install

1. **WSL + Theos** (primary): `bash -c "$(curl -fsSL https://raw.githubusercontent.com/theos/theos/master/bin/install-theos)"` → `make package FINALPACKAGE=1` → `packages/OmniCam.ipa` (auto ldid-fake-signed).
2. **GitHub Actions** (backup): macos-14 runner + Xcode 15.4 (preinstalled) + xcodegen + `xcodebuild CODE_SIGNING_ALLOWED=NO` + `brew install ldid && ldid -S` → zip `Payload/` → artifact .ipa.
3. **Install**: Filza web server `http://<phone-ip>:2222` → upload .ipa → tap → Install (no trust prompt, no 7-day timer, AppSync). Re-jailbreak after every reboot before launching.
