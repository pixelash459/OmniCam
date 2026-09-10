#!/usr/bin/env bash
# Pack a Theos-built OmniCam.app into dist/OmniCam.ipa (ldid fake-signed).
# Used by scripts/build-wsl.sh after `make package`. Safe to run standalone
# when BUILD_DIR is a symlink at $HOME/omni-ios (Theos cannot handle spaces).
set -euo pipefail
export THEOS="${THEOS:-$HOME/theos}"
export PATH="$THEOS/bin:$THEOS/toolchain/linux/iphone/bin:$PATH"

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(dirname "$SCRIPT_DIR")

if [ -d "$HOME/omni-ios/.theos/obj/OmniCam.app" ]; then
  APP="$HOME/omni-ios/.theos/obj/OmniCam.app"
elif [ -d "$REPO_ROOT/ios/.theos/obj/OmniCam.app" ]; then
  APP="$REPO_ROOT/ios/.theos/obj/OmniCam.app"
else
  echo "pack-ipa.sh: OmniCam.app not found (run scripts/build-wsl.sh first)" >&2
  exit 1
fi

DEST="$REPO_ROOT/dist"
OUT="$DEST/OmniCam.ipa"
VER=$(python3 - "$APP/Info.plist" <<'PY'
import plistlib, sys
p = plistlib.load(open(sys.argv[1], "rb"))
print(p.get("CFBundleShortVersionString") or "dev")
PY
)
VER_OUT="$DEST/local.omnicam.app_${VER}.ipa"
echo "packing $APP -> $OUT (v$VER)"
mkdir -p "$DEST"
bash "$SCRIPT_DIR/fakesign.sh" "$APP" "$OUT"
cp -f "$OUT" "$VER_OUT"
ls -l "$OUT" "$VER_OUT"
