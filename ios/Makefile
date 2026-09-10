# OmniCam — Theos application build (arm64, iOS 12.0+, .ipa package)
#
# Conventions verified against theos/theos@master and theos/templates@master
# (ios/application_modern):
#   * TARGET = <platform>:<compiler>:<sdk version>:<deployment target>
#     (iPhoneOS12.4.sdk ships in theos/sdks)
#   * Template variable order: TARGET/etc. -> include common.mk ->
#     APPLICATION_NAME + <name>_* variables -> include application.mk
#   * Bundle id lives in Resources/Info.plist (CFBundleIdentifier); Theos has
#     no separate bundle-id variable for applications.
#   * Resources/ is bundled automatically; Resources/Info.plist is moved to
#     the .app root by theos/makefiles/instance/shared/bundle.mk.
#   * THEOS_PACKAGE_FORMAT=ipa REQUIRES a "control" file next to this Makefile
#     (its Package:/Version: fields name the produced packages/<...>.ipa).
#
# Build:  make package FINALPACKAGE=1      (see scripts/build-wsl.sh)
# Output: packages/local.omnicam.app_1.1.1.ipa  (ldid fake-signed by Theos)

TARGET = iphone:clang:12.4:12.0
ARCHS = arm64
THEOS_PACKAGE_FORMAT = ipa
PACKAGE_FORMAT = ipa            # this Theos snapshot reads the un-prefixed name

INSTALL_TARGET_PROCESSES = OmniCam

include $(THEOS)/makefiles/common.mk

APPLICATION_NAME = OmniCam

OmniCam_FILES = src/main.m \
	src/OCAppDelegate.m \
	src/OCViewController.m \
	src/OCCaptureEngine.m \
	src/OCFilterPipeline.m \
	src/OCLutLoader.m \
	src/OCEncoder.m \
	src/OCPacker.m \
	src/OCNetManager.m \
	src/OCFilterState.m

OmniCam_FRAMEWORKS = UIKit Foundation AVFoundation CoreMedia CoreVideo VideoToolbox CoreImage Metal MetalKit OpenGLES

OmniCam_CFLAGS = -fobjc-arc -Wno-deprecated-declarations

# Resources/Info.plist + Resources/LaunchScreen.storyboard.
# (Default RESOURCE_DIRS is "Resources" when the dir exists; set explicitly.)
OmniCam_RESOURCE_DIRS = Resources

include $(THEOS_MAKE_PATH)/application.mk
