"""Persistent PC-side settings (JSON in ``%APPDATA%\\OmniCam\\settings.json``).

Two things live here:

- ``phones``: the "last known phones" list -- every phone we ever received a
  beacon from or completed a control handshake with.  UDP discovery is
  unreliable on Windows (firewall profile mismatch, APs dropping broadcasts,
  virtual adapters), so the UI offers these as connect targets even when no
  beacon arrives.
- ``prefs``: arbitrary UI preferences (last resolution/fps/kbps, window
  geometry, last connected ip, autoconnect flag, ...).

All public functions are thread-safe (one module lock) and never raise on a
corrupt/missing file: a broken ``settings.json`` is treated as empty.  Writes
are atomic (temp file + ``os.replace``).

The settings directory can be overridden (tests) by setting the module
variable :data:`SETTINGS_DIR` or the environment variable
``OMNICAM_SETTINGS_DIR``.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("omnicam.settings")

SETTINGS_FILE = "settings.json"
MAX_KNOWN_PHONES = 20

#: Override for the settings directory (``None`` = use env var / %APPDATA%).
SETTINGS_DIR: Optional[str] = None

_lock = threading.RLock()


# ---------------------------------------------------------------------------
# location
# ---------------------------------------------------------------------------

def settings_dir() -> Path:
    """Directory holding ``settings.json``.

    Resolution order: :data:`SETTINGS_DIR` module variable, ``OMNICAM_SETTINGS_DIR``
    env var, ``%APPDATA%\\OmniCam``, ``~/.omnicam``.
    """
    if SETTINGS_DIR:
        return Path(SETTINGS_DIR)
    env = os.environ.get("OMNICAM_SETTINGS_DIR")
    if env:
        return Path(env)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "OmniCam"
    return Path.home() / ".omnicam"


def settings_path() -> Path:
    """Full path of the settings file."""
    return settings_dir() / SETTINGS_FILE


# ---------------------------------------------------------------------------
# raw load / save
# ---------------------------------------------------------------------------

def _empty() -> Dict[str, Any]:
    return {"version": 1, "phones": [], "prefs": {}}


def _normalize(d: Any) -> Dict[str, Any]:
    out = _empty()
    if not isinstance(d, dict):
        return out
    out.update({k: v for k, v in d.items() if k not in ("phones", "prefs")})
    phones = d.get("phones")
    if isinstance(phones, list):
        out["phones"] = [p for p in phones if isinstance(p, dict) and p.get("ip")]
    prefs = d.get("prefs")
    if isinstance(prefs, dict):
        out["prefs"] = dict(prefs)
    return out


def load_settings() -> Dict[str, Any]:
    """Read and return the whole settings dict (empty defaults on any error)."""
    path = settings_path()
    with _lock:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return _empty()
        except (OSError, ValueError) as exc:
            log.warning("settings file %s unreadable (%s); using defaults", path, exc)
            return _empty()
    return _normalize(data)


def save_settings(d: Dict[str, Any]) -> None:
    """Atomically write ``d`` to the settings file (temp file + ``os.replace``)."""
    path = settings_path()
    payload = json.dumps(_normalize(d), indent=2, sort_keys=True)
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".settings-", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            log.warning("could not save settings to %s: %s", path, exc)


# ---------------------------------------------------------------------------
# known phones
# ---------------------------------------------------------------------------

def _clean_phone(p: Dict[str, Any]) -> Dict[str, Any]:
    try:
        last_seen = float(p.get("last_seen", 0.0))
    except (TypeError, ValueError):
        last_seen = 0.0
    return {
        "ip": str(p.get("ip", "")).strip(),
        "name": str(p.get("name", "") or ""),
        "last_seen": last_seen,
        "device": str(p.get("device", "") or ""),
    }


def known_phones() -> List[Dict[str, Any]]:
    """Remembered phones, most recently seen first.

    Each entry: ``{"ip": str, "name": str, "last_seen": float epoch, "device": str}``.
    """
    with _lock:
        phones = [_clean_phone(p) for p in load_settings()["phones"]]
    phones = [p for p in phones if p["ip"]]
    phones.sort(key=lambda p: p["last_seen"], reverse=True)
    return phones[:MAX_KNOWN_PHONES]


def remember_phone(ip: str, name: str = "", device: str = "") -> None:
    """Upsert ``ip`` in the known-phones list and stamp ``last_seen`` = now.

    Empty ``name``/``device`` never overwrite a previously stored value.  The
    list is kept most-recent first and capped at :data:`MAX_KNOWN_PHONES`.
    """
    ip = (ip or "").strip()
    if not ip:
        return
    now = time.time()
    with _lock:
        data = load_settings()
        phones = [_clean_phone(p) for p in data["phones"]]
        entry: Optional[Dict[str, Any]] = None
        rest: List[Dict[str, Any]] = []
        for p in phones:
            if p["ip"] == ip and entry is None:
                entry = p
            elif p["ip"] and p["ip"] != ip:
                rest.append(p)
        if entry is None:
            entry = {"ip": ip, "name": "", "last_seen": 0.0, "device": ""}
        if name:
            entry["name"] = str(name)
        if device:
            entry["device"] = str(device)
        entry["last_seen"] = now
        rest.sort(key=lambda p: p["last_seen"], reverse=True)
        data["phones"] = ([entry] + rest)[:MAX_KNOWN_PHONES]
        save_settings(data)


def forget_phone(ip: str) -> None:
    """Remove ``ip`` from the known-phones list (no-op when absent)."""
    ip = (ip or "").strip()
    if not ip:
        return
    with _lock:
        data = load_settings()
        before = len(data["phones"])
        data["phones"] = [p for p in data["phones"] if str(p.get("ip", "")).strip() != ip]
        if len(data["phones"]) != before:
            save_settings(data)


# ---------------------------------------------------------------------------
# arbitrary prefs
# ---------------------------------------------------------------------------

def get_pref(key: str, default: Any = None) -> Any:
    """Return pref ``key`` (deep copy) or ``default`` when unset."""
    with _lock:
        prefs = load_settings()["prefs"]
    if key not in prefs:
        return default
    return copy.deepcopy(prefs[key])


def set_pref(key: str, value: Any) -> None:
    """Persist pref ``key`` = ``value`` (must be JSON-serialisable)."""
    with _lock:
        data = load_settings()
        data["prefs"][key] = copy.deepcopy(value)
        save_settings(data)


__all__ = [
    "SETTINGS_DIR",
    "MAX_KNOWN_PHONES",
    "settings_dir",
    "settings_path",
    "load_settings",
    "save_settings",
    "known_phones",
    "remember_phone",
    "forget_phone",
    "get_pref",
    "set_pref",
]
