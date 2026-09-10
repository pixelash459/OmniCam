# OmniCam Protocol v1 — iOS ⇄ Windows (canonical spec)

Both implementations (iOS app in `ios/`, PC client in `pc/`) MUST follow this document exactly.
All multi-byte integers are **big-endian** (network order). All sockets are on the same Wi-Fi LAN.

## 0. Constants

| Constant | Value |
|---|---|
| Beacon port (UDP, phone → broadcast) | `9920` |
| Video RTP port (UDP, phone → PC) | `9921` |
| Control TCP port (PC connects → phone listens) | `9923` |
| RTP payload type — video (H.264) | `96` |
| RTP payload type — FEC (optional) | `100` |
| Max video RTP payload (excl. RTP header) | `1200` bytes |
| RTP clock — video | `90000` Hz |
| Beacon magic | `"OMNICAM1"` |

## 1. Discovery (UDP beacon)

Phone broadcasts a single-line JSON + `\n` to `255.255.255.255:9920` **every 1 second** (always, app in foreground or streaming):

```json
{"magic":"OMNICAM1","ver":1,"name":"<iPhone computer name>","model":"iPhone7,1","tcp_port":9923,"streaming":false,"app":"1.1.0"}
```

PC listens on `0.0.0.0:9920`, dedupes by source IP, refreshes a device list (device considered offline after 3.5 s without beacon). Manual IP entry is the fallback (APs may block broadcast).

## 2. Control channel (TCP, phone listens on 9923)

- Newline-delimited UTF-8 JSON. Max message 64 KiB. One client at a time; a new `hello` on a second connection is refused with `{"t":"error","code":"busy"}`.
- PC connects, then immediately sends `hello`. Phone replies `welcome`. Phone is source of truth for filter state; PC may push state.

### 2.1 Messages PC → Phone

```jsonc
{"t":"hello","name":"<pc hostname>","ver":1}
{"t":"start","rtp_host":"192.168.1.50",
 "video":{"port":9921,"w":1280,"h":720,"fps":30,"kbps":3000,"keyint":60}}
{"t":"stop"}
{"t":"camera","id":"front"}            // or "back"
{"t":"bitrate","kbps":2500}            // manual override; also disables auto ABR until {"t":"abr","auto":true}
{"t":"abr","auto":true}
{"t":"idr"}                            // force keyframe on next frame
{"t":"filter","state":{ ... }}         // full filter state, schema in §5
{"t":"torch","on":true}
{"t":"zoom","x":2.0}                   // digital zoom 1.0..max (clamped by phone)
{"t":"rr","loss_pct":0.4,"jitter_ms":3,"max_seq":12345,"fps_decoded":29.7}  // every 500 ms while streaming; drives ABR
{"t":"ping","ts":1234567890123}        // ms epoch; phone echoes unchanged
{"t":"bye"}
```

### 2.2 Messages Phone → PC

```jsonc
{"t":"welcome","ver":1,"app":"1.1.0","device":"iPhone7,1","ios":"12.5.8","camera":"back",
 "max_front":[1280,720,30],"max_back":[1920,1080,60],"filter":{ ...current state §5... }}
{"t":"started","ssrc_video":<u32>,"ssrc_fec":<u32>,"fec":false}
{"t":"stopped"}
{"t":"camera_ok","id":"front"}         // after switch completes (~150-300 ms black gap is normal on A8)
{"t":"bitrate_ok","kbps":2500,"auto":false}
{"t":"filter_ok"}
{"t":"torch_ok","on":true}
{"t":"stats","fps":29.8,"kbps":2950,"enc_ms":9.2,"loss_pct":0.4,"nacks":12,"sent":34510}
{"t":"pong","ts":1234567890123}
{"t":"error","code":"busy|badmsg|nosuch"}
```

`stats` is sent by the phone every 1 s while streaming. `enc_ms` = last frame encode time.

## 3. Video transport (RTP/UDP)

Standard 12-byte RTP header: `V=2,P=0,X=0,CC=0, M, PT=96, seq(16), ts(32, 90 kHz), ssrc(32)`.
Timestamp advances by `90000/fps` per frame; all packets of one frame share a timestamp.

### 3.1 H.264 packetization (RFC 6184, packetization-mode = 1)

- Sender receives AVCC NALUs (4-byte length prefixes) from `VTCompressionSession` → converts to Annex-B (`00 00 00 01` start codes) internally; RTP carries **NALUs, never start codes**.
- NAL ≤ 1200 B → **Single NAL Unit packet** (payload = NAL).
- NAL > 1200 B → **FU-A** (type 28): indicator byte = `(NRI<<5)|28`, FU header = `(S<<7)|(E<<6)|type`. All fragments share ts, consecutive seq, marker bit on the last.
- On every **IDR**: the first packet(s) of the access unit are one **STAP-A** (type 24) containing SPS then PPS (`16-bit length + NAL`, NRI = max of contained NALs). Marker bit on final packet of the frame (IDR or not).
- SPS/PPS are re-sent with every IDR so a joining/recovering PC decodes immediately.

### 3.2 Sender retransmit cache

Ring buffer of full UDP datagrams indexed by seq, ≥ 2 s worth (~2048 entries). On NACK, re-send the original datagram bytes unchanged (receiver dedupes by seq).

### 3.3 Receiver feedback (PC → phone, to phone_ip:9921)

**NACK** (RFC 4585 Generic NACK, PT=205, FMT=1): `V=2,P=0,FMT=1,PT=205,len`, sender SSRC = PC's own random, media SSRC = `ssrc_video`, then ≥1 entries of `PID(16)+BLP(16)` (BLP bit i = packet PID+1+i lost). Send after a reorder wait of ~8 ms per gap; never re-NACK the same seq within 15 ms; ≤ 16 entries per packet. Only for PT 96 (never FEC).

**PLI** (RFC 4585, PT=206, FMT=1, len=2): same layout, requests an IDR. Send on: connect/start, sustained loss >10 %, or decode stall >500 ms.

### 3.4 FEC (optional, default OFF; both ends implement)

RTP stream with PT=100, its **own SSRC (`ssrc_fec`) and own seq16 counter**. Covers groups of 4 consecutive video packets (by seq). Payload: `[base_seq(16)][count(8)][flags(8)][XOR of group payloads zero-padded to max length]`. RTP ts = ts of last covered packet. Phone enables FEC when reported loss >2 % for 2 s and disables when 0 % for 5 s (no control message needed; PC detects via `fec` in `started` + PT 100 packets). Receiver recovery: if exactly one packet of a covered group is missing, reconstruct via XOR; then attempt depacketization.

### 3.5 Receiver pipeline (PC)

1. Recv → classify by PT (96 media / 100 FEC / 205+206 are outgoing only).
2. Reorder buffer keyed by seq (window 3 frames). Gap + 8 ms elapsed → NACK.
3. Frame assembly: buffer NALU fragments per ts; frame is complete at marker bit. Incomplete frames older than 3 frame periods are dropped.
4. Depacketize (single NALU / FU-A / STAP-A) → Annex-B → decoder.

## 4. Audio

Audio: intentionally NOT implemented — OmniCam is video-only; use your PC microphone in the consumer app. (Removed in v1.1; ports/PTs `9922` and `97` are reserved-nothing and MUST NOT be used.)

## 5. Filter state schema (phone applies; PC mirrors)

```jsonc
{
 "look": "none|mono|noir|chrome|fade|instant|process|transfer|sepia|invert|false_color",
 "adjust": {"brightness":0.0,   // -1..1, def 0
            "contrast":0.0,     // -1..1, def 0
            "saturation":1.0,   // 0..2,  def 1
            "temperature":0.0,  // -1..1, def 0 (cool..warm)
            "vibrance":0.0,     // 0..1,  def 0
            "gamma":1.0,        // 0.2..3,def 1
            "sharpness":0.0,    // 0..1,  def 0
            "vignette":0.0},    // 0..1,  def 0
 "beauty":0.0,                 // 0..1 skin smoothing
 "stylize":"none|pixelate|crystallize|hexagonal|twirl|bulge|bump|soft_blur|zoom_blur",
 "stylize_amount":0.5,         // 0..1
 "geometry":{"mirror":false,"flipV":false,"rotate":0,"zoom":1.0,"panX":0.0,"panY":0.0,"aspect":"native"},
 "lut":null,                   // filename of imported .cube, or null
 "overlay":{"text":"","show_timecode":false}
}
```

`rotate` ∈ {0,90,180,270}. `aspect` ∈ {native, "16:9", "4:3"}. Defaults shown; absent keys = default.
Phone pushes `{"t":"filter","state":...}` → PC (and vice versa) whenever state changes; both UIs stay in sync.

**PC additionally applies local (CPU) adjustments** to the decoded frame before virtual-cam output, with its own separate controls (brightness/contrast/saturation/mirror/rotate) — these are NOT part of the phone filter state.

## 6. Adaptive bitrate (phone-side, driven by `rr`)

Only while `auto` (default true; `{"t":"bitrate"}` sets auto=false).
- loss < 2 % → `bitrate ×= 1.05` per rr interval (cap = configured max)
- 2 % ≤ loss ≤ 10 % → hold
- loss > 10 % → `bitrate ×= (1 − 0.5·loss)`, floor 500 kbps; also send IDR after sustained loss
Defaults: 720p30 → 3000 kbps, 1080p30 → 6000 kbps.

## 7. VTCompressionSession settings (phone, verified against iOS 12.4 SDK)

`RequireHardwareAcceleratedVideoEncoder=true`, `RealTime=true`, `AllowFrameReordering=false`,
`ProfileLevel=AVC/H264 Main AutoLevel`, `AverageBitRate=<kbps*1000>`, `DataRateLimits=@[(kbps*1000/8), @(1)]`,
`MaxKeyFrameInterval=keyint (default 60)`, `ExpectedFrameRate=fps`, `MaxFrameDelayCount=1`.
Output pixels `kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange`; SPS/PPS from `CMVideoFormatDescriptionGetParameterSetAtIndex`.

## 8. Latency budget (target)

capture ~16 ms + filter 2-6 ms + encode ~10-15 ms + LAN <5 ms + jitter/reorder 8-15 ms + decode 3-8 ms + render/virtualcam <5 ms ≈ **60-110 ms glass-to-glass** (DroidCam Wi-Fi: 150-300 ms).

## 9. Security

None in v1 (trusted home LAN, closed 2-party system). Do not port-forward. PIN auth is a possible v1.2 extension.
