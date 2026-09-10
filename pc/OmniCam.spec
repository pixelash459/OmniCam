# -*- mode: python ; coding: utf-8 -*-
# OmniCam PC — onedir (not onefile). Onefile dumps ~80 MB into %TEMP% on every
# launch, which is slow on laptops and fights Defender. Ship onedir via Inno.

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

datas, binaries, hiddenimports = [], [], []

d, b, h = collect_all("pyvirtualcam")
datas += d
binaries += b
hiddenimports += h

binaries += collect_dynamic_libs("av")
hiddenimports += [
    "av",
    "numpy",
    "omnicam",
    "omnicam.ui",
    "omnicam.net",
    "omnicam.app",
    "omnicam.decoder",
    "omnicam.virtualcam_out",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
]

a = Analysis(
    ["OmniCamEntry.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "PyQt5", "pytest", "unittest",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
        "PySide6.QtQuick3D", "PySide6.QtQml", "PySide6.QtQuick",
        "PySide6.QtMultimedia", "PySide6.QtPdf", "PySide6.QtCharts",
        "PySide6.QtDataVisualization", "PySide6.QtBluetooth",
        "PySide6.QtSensors", "PySide6.QtPositioning", "PySide6.QtLocation",
        "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects",
        "PySide6.QtScxml", "PySide6.QtSerialBus", "PySide6.QtSerialPort",
        "PySide6.QtNfc", "PySide6.QtWebView", "PySide6.QtWebSockets",
        "PySide6.QtWebChannel", "PySide6.QtGraphs", "PySide6.QtHelp",
        "PySide6.QtHttpServer", "PySide6.QtDesigner", "PySide6.QtTest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OmniCam",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon="packaging\\omnicam.ico",
    version="packaging\\file_version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OmniCam",
)
