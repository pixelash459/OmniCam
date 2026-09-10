# Contributing

OmniCam is a personal project that was made public. PRs are welcome if they keep the
phone and PC in sync and do not break the protocol.

## PC client

```bat
cd pc
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt pytest
.venv\Scripts\python -m pytest tests -q
```

The UI tests need Qt. They set `QT_QPA_PLATFORM=offscreen` themselves where
needed. Do not point tests at a real `%APPDATA%\OmniCam\settings.json`
(`conftest.py` isolates that).

Building the Windows installer (optional): install [Inno Setup 6](https://jrsoftware.org/isinfo.php),
then `pc\build-exe.bat` (also `pip install -r requirements-build.txt`).

## iPhone app

Theos in WSL is the supported path (`wsl bash scripts/build-wsl.sh`). See
[INSTALL.md](INSTALL.md). The GitHub Action **Build unsigned IPA** is a
manual backup (`workflow_dispatch`) using Xcode 15.4.

Keep `ios/Resources/Info.plist` as literals (no `$(PRODUCT_…)` macros) — Theos
and XcodeGen share that file.

## Protocol

Both ends speak [docs/PROTOCOL.md](docs/PROTOCOL.md). If you add a control
message, a session field, or a filter key, update that file in the same PR and
keep the iOS and Python parsers matching.

## Style

- Python 3.11+, type hints where it helps, no new runtime dependencies unless
  they are unavoidable.
- Objective-C, ARC, iOS 12.0, no third-party iOS libraries.
- Do not commit secrets, crash logs, or machine-specific paths.
