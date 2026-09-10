"""Tests for omnicam.settings (persisted prefs + known phones)."""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from omnicam import settings


@pytest.fixture
def sdir(tmp_path, monkeypatch):
    """Point the settings module at a fresh temp directory."""
    d = tmp_path / "OmniCam"
    monkeypatch.setattr(settings, "SETTINGS_DIR", str(d))
    return d


def test_settings_dir_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SETTINGS_DIR", None)
    monkeypatch.setenv("OMNICAM_SETTINGS_DIR", str(tmp_path / "env"))
    assert settings.settings_dir() == tmp_path / "env"
    monkeypatch.delenv("OMNICAM_SETTINGS_DIR")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert settings.settings_dir() == tmp_path / "appdata" / "OmniCam"
    monkeypatch.delenv("APPDATA")
    assert settings.settings_dir().name == ".omnicam"
    monkeypatch.setattr(settings, "SETTINGS_DIR", str(tmp_path / "mod"))
    assert settings.settings_dir() == tmp_path / "mod"
    assert settings.settings_path() == tmp_path / "mod" / "settings.json"


def test_load_missing_and_corrupt(sdir):
    assert settings.load_settings() == {"version": 1, "phones": [], "prefs": {}}
    sdir.mkdir(parents=True)
    (sdir / "settings.json").write_text("{not json", encoding="utf-8")
    assert settings.load_settings()["phones"] == []
    assert settings.get_pref("x", 5) == 5
    assert settings.known_phones() == []


def test_value_survives_transient_replace_failure(sdir, monkeypatch):
    """Windows can briefly lock a just-written file (Defender/indexer). A
    failed os.replace must not make the running process forget the value."""
    calls = {"n": 0}
    real_replace = settings.os.replace

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("locked")
        return real_replace(src, dst)

    monkeypatch.setattr(settings.os, "replace", flaky_replace)
    monkeypatch.setattr(settings.time, "sleep", lambda *_: None)
    settings.set_pref("stream.res", "1920x1080")
    assert settings.get_pref("stream.res") == "1920x1080"
    assert calls["n"] == 3  # retried until it went through

    # Even when every attempt fails, the in-process value is kept.
    monkeypatch.setattr(settings.os, "replace",
                        lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
    settings.set_pref("stream.fps", 15)
    assert settings.get_pref("stream.fps") == 15
    assert settings.get_pref("stream.res") == "1920x1080"


def test_save_is_atomic_and_creates_dir(sdir):
    settings.save_settings({"prefs": {"a": 1}, "phones": []})
    assert (sdir / "settings.json").exists()
    leftovers = [p for p in os.listdir(sdir) if p.endswith(".tmp")]
    assert leftovers == []
    data = json.loads((sdir / "settings.json").read_text(encoding="utf-8"))
    assert data["prefs"] == {"a": 1}
    assert settings.load_settings()["prefs"]["a"] == 1


def test_prefs_roundtrip(sdir):
    assert settings.get_pref("autoconnect", True) is True
    settings.set_pref("autoconnect", False)
    settings.set_pref("stream", {"w": 1920, "h": 1080, "fps": 30, "kbps": 6000})
    settings.set_pref("geometry", [10, 20, 800, 600])
    assert settings.get_pref("autoconnect") is False
    assert settings.get_pref("stream")["w"] == 1920
    assert settings.get_pref("geometry") == [10, 20, 800, 600]
    # returned values are copies
    settings.get_pref("stream")["w"] = 1
    assert settings.get_pref("stream")["w"] == 1920


def test_remember_phone_upsert_order_and_cap(sdir):
    settings.remember_phone("10.0.0.50", name="iPhone7,1", device="12.5.8")
    time.sleep(0.01)
    settings.remember_phone("192.168.0.77", name="iPad")
    phones = settings.known_phones()
    assert [p["ip"] for p in phones] == ["192.168.0.77", "10.0.0.50"]
    assert phones[1]["name"] == "iPhone7,1"
    assert phones[1]["device"] == "12.5.8"
    assert isinstance(phones[0]["last_seen"], float)
    assert abs(phones[0]["last_seen"] - time.time()) < 5

    # upsert: bumps to front, empty name/device do not clobber stored values
    time.sleep(0.01)
    settings.remember_phone("10.0.0.50")
    phones = settings.known_phones()
    assert phones[0]["ip"] == "10.0.0.50"
    assert phones[0]["name"] == "iPhone7,1"
    assert phones[0]["device"] == "12.5.8"
    assert len(phones) == 2

    # non-empty values overwrite
    settings.remember_phone("10.0.0.50", name="Front iPhone")
    assert settings.known_phones()[0]["name"] == "Front iPhone"

    # blank ip ignored
    settings.remember_phone("   ")
    assert len(settings.known_phones()) == 2

    # cap at MAX_KNOWN_PHONES, most recent survive
    for i in range(30):
        settings.remember_phone(f"10.0.0.{i}", name=f"p{i}")
    phones = settings.known_phones()
    assert len(phones) == settings.MAX_KNOWN_PHONES == 20
    assert phones[0]["ip"] == "10.0.0.29"
    assert all(p["ip"] != "192.168.0.77" for p in phones)


def test_forget_phone(sdir):
    settings.remember_phone("1.2.3.4", name="a")
    settings.remember_phone("5.6.7.8", name="b")
    settings.forget_phone("1.2.3.4")
    assert [p["ip"] for p in settings.known_phones()] == ["5.6.7.8"]
    settings.forget_phone("9.9.9.9")  # absent: no error
    settings.forget_phone("")
    assert len(settings.known_phones()) == 1


def test_every_entry_has_exact_keys(sdir):
    settings.remember_phone("1.1.1.1")
    (p,) = settings.known_phones()
    assert set(p.keys()) == {"ip", "name", "last_seen", "device"}


def test_thread_safety(sdir):
    errors: list = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                settings.remember_phone(f"10.1.{n}.{i % 5}", name=f"w{n}")
                settings.set_pref(f"k{n}", i)
                settings.known_phones()
                settings.get_pref(f"k{n}")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    data = settings.load_settings()
    assert len(data["phones"]) <= settings.MAX_KNOWN_PHONES
    for n in range(6):
        assert settings.get_pref(f"k{n}") == 19


def test_app_remembers_phone_on_welcome_and_beacon(sdir):
    from omnicam.app import OmniCamApp

    app = OmniCamApp()
    try:
        app.control._target = ("10.0.0.50", 9923)
        app._on_message({"t": "welcome", "ver": 1, "app": "1.1.0", "device": "iPhone7,1",
                         "ios": "12.5.8", "camera": "back"})
        phones = app.known_phones()
        assert phones[0]["ip"] == "10.0.0.50"
        assert phones[0]["name"] == "iPhone7,1"
        assert phones[0]["device"] == "12.5.8"
        assert settings.get_pref("last_ip") == "10.0.0.50"

        app.beacons._handle_beacon(
            json.dumps({"magic": "OMNICAM1", "name": "QA iPhone", "model": "iPhone15,3",
                        "tcp_port": 9923}).encode(), "192.168.0.99")
        phones = app.known_phones()
        assert phones[0]["ip"] == "192.168.0.99"
        assert phones[0]["name"] == "QA iPhone"
        assert phones[0]["device"] == "iPhone15,3"

        # our own probe datagrams must never register as a phone
        app.beacons._handle_beacon(app.beacons.PROBE_PAYLOAD, "10.255.255.1")
        assert all(p["ip"] != "10.255.255.1" for p in app.known_phones())
        assert all(d["ip"] != "10.255.255.1" for d in app.get_devices())
    finally:
        app.control._target = None
        app.shutdown()


def test_autoconnect_last(sdir):
    from omnicam.app import OmniCamApp

    app = OmniCamApp()
    calls: list = []
    app.connect = lambda ip: calls.append(ip)  # type: ignore[method-assign]
    try:
        assert app.autoconnect_last() is None  # nothing remembered
        settings.remember_phone("192.168.0.5", name="old")
        time.sleep(0.01)
        settings.remember_phone("10.0.0.50", name="new")
        settings.set_pref("autoconnect", False)
        assert app.autoconnect_last() is None
        settings.set_pref("autoconnect", True)
        assert app.autoconnect_last() == "10.0.0.50"
        assert calls == ["10.0.0.50"]
        assert any(d["ip"] == "10.0.0.50" for d in app.get_devices())
    finally:
        app.shutdown()
