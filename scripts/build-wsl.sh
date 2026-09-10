#!/usr/bin/env bash
# build-wsl.sh — primary build path: Theos inside WSL, fully offline from Apple.
#
# Windows usage (from the repo root, e.g. E:\Vibe ios apps\OmniCam):
#
#     wsl bash scripts/build-wsl.sh
#
# WSL maps the current Windows directory to /mnt/<drive>/..., so the repo is
# found at /mnt/e/"Vibe ios apps"/OmniCam — this script resolves its own
# location, so spaces in the path are handled.
#
# First run installs Theos entirely in user space (NO sudo / apt needed):
#   theos core  — github.com/theos/theos (+ vendor submodules)
#   ldid        — static binary from ProcursusTeam/ldid (fake-signer)
#   toolchain   — L1ghtmann/llvm-project iOSToolchain (the official Theos-for-
#                 Linux cross-clang + Mach-O linker, per bin/install-theos)
#   iOS SDK     — iPhoneOS12.4.sdk tarball from github.com/theos/sdks releases
# Only requirements inside WSL: git, curl, tar, xz, zip, perl, rsync (standard
# on Ubuntu; the official installer's apt list also needs none beyond these).
#
# Output: dist/OmniCam.ipa (plus the versioned copy from ios/packages/).

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(dirname "$SCRIPT_DIR")

echo "==> Repo root: $REPO_ROOT"

# --- 1. Theos (user-space, no sudo) ------------------------------------------
THEOS_DIR="${THEOS:-$HOME/theos}"
if [ ! -f "$THEOS_DIR/makefiles/common.mk" ]; then
	echo "==> Theos not found — installing to $THEOS_DIR (user-space, no sudo)..."
	git clone --depth=1 https://github.com/theos/theos.git "$THEOS_DIR"
	git -C "$THEOS_DIR" submodule update --init --depth=1 \
		vendor/include vendor/lib vendor/logos vendor/nic vendor/templates vendor/dm.pl
	mkdir -p "$THEOS_DIR/bin" "$THEOS_DIR/sdks" "$THEOS_DIR/toolchain"
	echo "==> Installing ldid (static)..."
	curl -fsSL -o "$THEOS_DIR/bin/ldid" \
		https://github.com/ProcursusTeam/ldid/releases/download/v2.1.5-procursus7/ldid_linux_x86_64
	chmod +x "$THEOS_DIR/bin/ldid"
	echo "==> Downloading iOS cross-toolchain (~1 GB, one time)..."
	curl -sL -o /tmp/ios-tc.tar.xz \
		https://github.com/L1ghtmann/llvm-project/releases/latest/download/iOSToolchain-x86_64.tar.xz
	tar -xJf /tmp/ios-tc.tar.xz -C "$THEOS_DIR/toolchain/"
	rm -f /tmp/ios-tc.tar.xz
	[ -x "$THEOS_DIR/toolchain/linux/iphone/bin/clang" ] || {
		echo "build-wsl.sh: toolchain extraction failed (no linux/iphone/bin/clang)" >&2; exit 1; }
	echo "==> Downloading iPhoneOS12.4 SDK..."
	SDK_URL=$(curl -s https://api.github.com/repos/theos/sdks/releases/latest \
		| grep download_url | sed 's/.*: "\(.*\)"/\1/' | grep 'iPhoneOS12\.4' | head -n 1)
	[ -n "$SDK_URL" ] || { echo "build-wsl.sh: iPhoneOS12.4 SDK not found in theos/sdks latest release" >&2; exit 1; }
	curl -sL "$SDK_URL" | tar -xJ -C "$THEOS_DIR/sdks"
	grep -q "export THEOS=" ~/.bashrc || echo "export THEOS=$THEOS_DIR" >> ~/.bashrc
fi
export THEOS="$THEOS_DIR"
export PATH="$THEOS/bin:$THEOS/toolchain/linux/iphone/bin:$PATH"
echo "==> THEOS=$THEOS"

# --- 2. Build the .ipa (Theos fake-signs with ldid automatically) -----------
# Theos refuses project paths containing spaces (e.g. "/mnt/e/Vibe ios apps/…"),
# so build through a space-free symlink inside the WSL filesystem when needed.
BUILD_DIR="$REPO_ROOT/ios"
case "$REPO_ROOT" in
	*" "*)
		ln -sfn "$REPO_ROOT/ios" "$HOME/omni-ios"
		BUILD_DIR="$HOME/omni-ios"
		;;
esac
cd "$BUILD_DIR"
# Keep build artifacts off the versioned tree except packages/.
make clean >/dev/null 2>&1 || true
make package FINALPACKAGE=1

# --- 3. Collect the product into dist/ --------------------------------------
mkdir -p "$REPO_ROOT/dist"
IPA=$(ls -t packages/*.ipa 2>/dev/null | head -n 1)
[ -n "$IPA" ] || { echo "build-wsl.sh: no .ipa produced" >&2; exit 1; }
cp -v "$IPA" "$REPO_ROOT/dist/"
cp -v "$IPA" "$REPO_ROOT/dist/OmniCam.ipa"

echo ""
echo "==> Built: $REPO_ROOT/dist/OmniCam.ipa"
echo "    Install it via Filza (see INSTALL.md)."
echo "    Tip: building on /mnt/* (DrvFS) is slow; for faster builds clone the"
echo "    repo inside the WSL filesystem (~/src) and sync back."
