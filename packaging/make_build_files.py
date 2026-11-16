#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Make the files PyInstaller needs that are generated, not stored: the icon and the
Windows version resource. Both go to packaging/build/.

    python packaging/make_build_files.py

Needs Pillow (a build-time tool only; the program itself does not use it).
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from tesseract_win import branding, versions  # noqa: E402

OUT = os.path.join(HERE, "build")
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def version_tuple(text: str):
    parts = [int(p) for p in text.split(".")[:4] if p.isdigit()]
    return tuple(parts + [0] * (4 - len(parts)))


def write_version_info(path: str) -> None:
    v = version_tuple(versions.APP_VERSION)
    text = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=%(v)r, prodvers=%(v)r, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', %(vendor)r),
      StringStruct('FileDescription', %(desc)r),
      StringStruct('FileVersion', %(ver)r),
      StringStruct('InternalName', %(name)r),
      StringStruct('LegalCopyright', %(copyright)r),
      StringStruct('OriginalFilename', %(exe)r),
      StringStruct('ProductName', %(name)r),
      StringStruct('ProductVersion', %(ver)r),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""" % {"v": v, "vendor": branding.VENDOR, "desc": "%s: %s" % (branding.APP_NAME, branding.APP_TAGLINE),
       "ver": versions.APP_VERSION, "name": branding.APP_NAME, "copyright": branding.COPYRIGHT,
       "exe": branding.APP_NAME + ".exe"}
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_icon(path: str) -> None:
    from tesseract_win.gui import icons
    largest = icons.make_icon(256)
    images = [icons.make_icon(size) for size in ICON_SIZES]
    largest.save(path, format="ICO", sizes=[(s, s) for s in ICON_SIZES], append_images=images[:-1])


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    write_version_info(os.path.join(OUT, "version_info.txt"))
    write_icon(os.path.join(OUT, "TesserACT-Win.ico"))
    sys.stdout.write("wrote %s\n" % OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
