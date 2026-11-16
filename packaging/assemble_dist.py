#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Turn PyInstaller's output into the folder users unzip, then zip and hash it.

    python packaging/assemble_dist.py [--engines DIR] [--dist DIR] [--release DIR]

Adds beside TesserACT-Win.exe: engines\\ (both programs and the detection sketch),
example\\ (made by tools/make_example.py), README.txt, LICENSE.txt, NOTICE.txt and
SMARTSCREEN.txt. Writes release/TesserACT-Win-<version>-win64-portable.zip, whose single
top folder is "TesserACT-Win", and release/SHA256SUMS.txt.

The zip is written with fixed timestamps and sorted names, so the same input gives the
same zip.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from tesseract_win import branding, versions  # noqa: E402

TOP = branding.APP_NAME
ENGINE_FILES = ("tesseract-asm.exe", "scepter.exe", "om2detect.sketch")
TEXTS = (
    (os.path.join(HERE, "README.txt"), "README.txt"),
    (os.path.join(REPO, "LICENSE"), "LICENSE.txt"),
    (os.path.join(REPO, "NOTICE"), "NOTICE.txt"),
    (os.path.join(HERE, "SMARTSCREEN.txt"), "SMARTSCREEN.txt"),
)
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def crlf_copy(src: str, dst: str) -> None:
    """Text files for Windows users: CRLF line ends, so Notepad on old builds shows them right."""
    with open(src, "r", encoding="utf-8") as handle:
        text = handle.read()
    with open(dst, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--engines", default=os.path.join(REPO, "build", "engines", "windows"))
    parser.add_argument("--dist", default=os.path.join(REPO, "dist", TOP))
    parser.add_argument("--release", default=os.path.join(REPO, "release"))
    args = parser.parse_args(argv)

    exe = os.path.join(args.dist, TOP + ".exe")
    if not os.path.isfile(exe):
        sys.stderr.write("%s not found: run PyInstaller first\n" % exe)
        return 1

    engines_out = os.path.join(args.dist, "engines")
    os.makedirs(engines_out, exist_ok=True)
    for name in ENGINE_FILES:
        src = os.path.join(args.engines, name)
        if not os.path.isfile(src):
            sys.stderr.write("engine file missing: %s\n" % src)
            return 1
        shutil.copy2(src, os.path.join(engines_out, name))

    example_out = os.path.join(args.dist, "example")
    if os.path.isdir(example_out):
        shutil.rmtree(example_out)
    subprocess.run([sys.executable, os.path.join(REPO, "tools", "make_example.py"), example_out], check=True)

    for src, name in TEXTS:
        crlf_copy(src, os.path.join(args.dist, name))

    os.makedirs(args.release, exist_ok=True)
    zip_name = "%s-%s-win64-portable.zip" % (TOP, versions.APP_VERSION)
    zip_path = os.path.join(args.release, zip_name)
    files = []
    for root, dirs, names in os.walk(args.dist):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, args.dist).replace(os.sep, "/")
            files.append((full, "%s/%s" % (TOP, rel)))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for full, arcname in files:
            info = zipfile.ZipInfo(arcname, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with open(full, "rb") as handle:
                archive.writestr(info, handle.read(), compresslevel=9)

    with open(os.path.join(args.release, "SHA256SUMS.txt"), "w", encoding="ascii", newline="\n") as handle:
        handle.write("%s  %s\n" % (sha256(zip_path), zip_name))
        for name in ENGINE_FILES[:2]:
            handle.write("%s  %s/engines/%s\n" % (sha256(os.path.join(engines_out, name)), TOP, name))
        handle.write("%s  %s/%s.exe\n" % (sha256(exe), TOP, TOP))

    size_mb = os.path.getsize(zip_path) / (1024 * 1024)
    sys.stdout.write("%s  (%.1f MB, %d files)\n" % (zip_path, size_mb, len(files)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
