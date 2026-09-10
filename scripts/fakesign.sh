#!/usr/bin/env bash
# fakesign.sh — ad-hoc pseudo-sign an .app/.ipa with ldid for AppSync Unified
# sideloading (no Apple dev account / no provisioning involved).
#
# Usage:  fakesign.sh <Payload dir | .app dir | .ipa> [output.ipa]
#   .ipa          -> fake-signed <input>-fakesigned.ipa (or [output.ipa])
#   .app dir      -> fake-signed <name>.ipa next to the .app
#   Payload dir   -> fake-signed OmniCam.ipa (or [output.ipa])
#
# Signs every embedded framework/dylib plus the main binary with `ldid -S`
# (ad-hoc, no entitlements). AppSync Unified accepts even unsigned binaries,
# but ldid -S avoids the known iOS 12.4 launch-crash with unsigned binaries.
#
# Requires: ldid, zip, unzip. (macOS: brew install ldid; WSL: bundled with Theos)

set -euo pipefail

usage() { echo "Usage: fakesign.sh <Payload dir or .ipa> [output.ipa]" >&2; exit 2; }

[ $# -ge 1 ] || usage
INPUT=$1
[ -e "$INPUT" ] || { echo "fakesign.sh: no such file or directory: $INPUT" >&2; exit 1; }

for tool in ldid zip unzip; do
	command -v "$tool" >/dev/null 2>&1 || { echo "fakesign.sh: '$tool' not found in PATH" >&2; exit 1; }
done

WORK=""
cleanup() { [ -n "$WORK" ] && rm -rf "$WORK"; }
trap cleanup EXIT

# Resolve a directory that contains either Payload/*.app or a single .app.
case "$INPUT" in
*.ipa)
	[ $# -le 2 ] || usage
	WORK=$(mktemp -d)
	echo "==> Extracting $(basename "$INPUT")"
	unzip -qq "$INPUT" -d "$WORK"
	OUT=${2:-"${INPUT%.*}-fakesigned.ipa"}
	;;
*.app)
	[ $# -le 2 ] || usage
	APP_DIR=$(cd "$INPUT" && pwd)
	WORK=$(mktemp -d)
	mkdir -p "$WORK/Payload"
	cp -R "$APP_DIR" "$WORK/Payload/$(basename "$APP_DIR")"
	OUT=${2:-"$(basename "$APP_DIR" .app).ipa"}
	;;
*)
	# Assume an extracted Payload directory (containing *.app) or an .app path
	# given without extension.
	[ $# -le 2 ] || usage
	if [ -d "$INPUT/Payload" ]; then
		WORK=$(cd "$INPUT" && pwd)
	else
		APP_DIR=$(cd "$INPUT" && pwd)
		WORK=$(mktemp -d)
		mkdir -p "$WORK/Payload"
		cp -R "$APP_DIR" "$WORK/Payload/$(basename "$APP_DIR")"
	fi
	OUT=${2:-"OmniCam.ipa"}
	;;
esac

APP=$(find "$WORK/Payload" -maxdepth 1 -name '*.app' -type d | head -n 1)
[ -n "$APP" ] || { echo "fakesign.sh: no .app found under Payload/" >&2; exit 1; }
PLIST="$APP/Info.plist"
[ -f "$PLIST" ] || { echo "fakesign.sh: missing $PLIST" >&2; exit 1; }

# Read CFBundleExecutable (PlistBuddy on macOS, grep fallback elsewhere).
EXE=""
if [ -x /usr/libexec/PlistBuddy ]; then
	EXE=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$PLIST" 2>/dev/null || true)
fi
if [ -z "$EXE" ]; then
	EXE=$(grep -A1 '<key>CFBundleExecutable</key>' "$PLIST" | grep -o '<string>[^<]*</string>' | head -n1 | sed 's/<\/*string>//g')
fi
[ -n "$EXE" ] || EXE=$(basename "$APP" .app)
[ -f "$APP/$EXE" ] || { echo "fakesign.sh: main binary not found: $APP/$EXE" >&2; exit 1; }

echo "==> Fake-signing embedded frameworks/dylibs (if any)"
if [ -d "$APP/Frameworks" ]; then
	for fw in "$APP"/Frameworks/*.framework; do
		[ -e "$fw" ] || continue
		echo "    ldid -S $fw/$(basename "${fw%.*}")"
		ldid -S "$fw/$(basename "${fw%.*}")"
	done
	for dy in "$APP"/Frameworks/*.dylib; do
		[ -e "$dy" ] || continue
		echo "    ldid -S $dy"
		ldid -S "$dy"
	done
fi

echo "==> Fake-signing main binary: $EXE"
ldid -S "$APP/$EXE"

echo "==> Packing $OUT"
ABS_OUT=$([ "${OUT:0:1}" = "/" ] && echo "$OUT" || echo "$(pwd)/$OUT")
(cd "$WORK" && zip -qry "$ABS_OUT" Payload)

echo "==> Done: $OUT"
