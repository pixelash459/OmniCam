"""PyTest configuration: make ``omnicam`` importable from the repo layout."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

PC_DIR = Path(__file__).resolve().parent.parent
if str(PC_DIR) not in sys.path:
    sys.path.insert(0, str(PC_DIR))

# Never let tests touch the user's real %APPDATA%\OmniCam\settings.json.
os.environ.setdefault("OMNICAM_SETTINGS_DIR",
                      tempfile.mkdtemp(prefix="omnicam-test-settings-"))


@pytest.fixture(autouse=True)
def _no_autoconnect_to_real_phones():
    """A real phone on the LAN is remembered from its beacons; with the
    default autoconnect=True a test window would then connect to it and its
    session would overwrite the widgets under test.  Tests that exercise
    autoconnect set the pref explicitly."""
    from omnicam import settings

    settings.set_pref("autoconnect", False)
    yield
