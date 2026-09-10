#!/usr/bin/env bash
set -euo pipefail
export THEOS="${THEOS:-$HOME/theos}"
export PATH="$THEOS/bin:$THEOS/toolchain/linux/iphone/bin:$PATH"
APP="$HOME/omni-ios/.theos/obj/OmniCam.app"
DEST="/mnt/e/Vibe ios apps/OmniCam/dist"
OUT="$DEST/OmniCam.ipa"
VER_OUT="$DEST/local.omnicam.app_1.1.10.ipa"
echo "packing $APP -> $OUT"
python3 - <<'PY'
import plistlib
p = plistlib.load(open("/home/ashut/omni-ios/.theos/obj/OmniCam.app/Info.plist","rb"))
print("version", p.get("CFBundleShortVersionString"), p.get("CFBundleVersion"))
PY
mkdir -p "$DEST"
bash "/mnt/e/Vibe ios apps/OmniCam/scripts/fakesign.sh" "$APP" "$OUT"
cp -f "$OUT" "$VER_OUT"
ls -l "$OUT" "$VER_OUT"
