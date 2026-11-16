# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
#
# PyInstaller recipe for TesserACT-Win: a one-folder, windowed build.
#
#     python packaging/make_build_files.py        (icon and version resource)
#     pyinstaller --noconfirm --clean packaging/tesseract_win.spec
#     python packaging/assemble_dist.py           (engines, example, texts, zip, checksums)
#
# One folder, not one file: a one-file build unpacks itself to the Temp folder at every
# start, which is slow, fails where Temp is locked down, and draws more antivirus alarms.
# Never compressed with UPX: packing makes the false alarms worse.
#
# The two engines are NOT listed here. PyInstaller would analyse and could alter them;
# assemble_dist.py copies them beside the program instead.

import os

from PyInstaller.building.api import COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis

PACKAGING = os.path.abspath(SPECPATH)                      # noqa: F821  (set by PyInstaller)
REPO = os.path.dirname(PACKAGING)
BUILD = os.path.join(PACKAGING, "build")
ICON = os.path.join(BUILD, "TesserACT-Win.ico")
VERSION_INFO = os.path.join(BUILD, "version_info.txt")

for needed in (ICON, VERSION_INFO):
    if not os.path.isfile(needed):
        raise SystemExit("%s is missing. Run: python packaging/make_build_files.py" % needed)

# Read-only data found at run time through appdirs.resource_dir() (sys._MEIPASS).
datas = []
assets = os.path.join(REPO, "tesseract_win", "assets")
for name in sorted(os.listdir(assets)):
    full = os.path.join(assets, name)
    if os.path.isfile(full) and not name.endswith((".py", ".pyc")):
        datas.append((full, "."))

binaries = []
hiddenimports = [
    "tesseract_win", "tesseract_win.gui",
    "tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.font",
]

# Drag-and-drop is optional: the program works without it, so a missing package must
# not stop the build. Only the Windows x64 part of tkdnd is kept.
try:
    from PyInstaller.utils.hooks import collect_all
    dnd_datas, dnd_binaries, dnd_hidden = collect_all("tkinterdnd2")

    def _wanted(entry):
        path = entry[0].replace("\\", "/").lower()
        if "/tkdnd/" not in path:
            return True
        return "/win-x64/" in path or "/win64/" in path

    datas += [e for e in dnd_datas if _wanted(e)]
    binaries += [e for e in dnd_binaries if _wanted(e)]
    hiddenimports += dnd_hidden
except Exception as exc:                                   # noqa: BLE001
    print("tkinterdnd2 not bundled (%s): drag-and-drop will be off" % exc)

a = Analysis(
    [os.path.join(PACKAGING, "entry.py")],
    pathex=[REPO],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Build-time and test-time tools must not end up in the program.
    excludes=["PIL", "pytest", "numpy", "setuptools", "pip", "wheel", "pydoc_data",
              "unittest", "doctest", "tkinter.test", "lib2to3", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TesserACT-Win",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # windowed: no console window, sys.stdout is None
    disable_windowed_traceback=False,
    icon=ICON,
    version=VERSION_INFO,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TesserACT-Win",
)
