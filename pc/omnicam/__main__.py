"""Module entry point: ``python -m omnicam`` launches the OmniCam PC app."""

import sys

from omnicam.ui import main


if __name__ == "__main__":
    sys.exit(main())
