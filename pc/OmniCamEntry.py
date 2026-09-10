"""PyInstaller launcher: builds into a standalone OmniCam.exe (see build-exe.bat)."""

import sys

from omnicam.ui import main


if __name__ == "__main__":
    sys.exit(main())
