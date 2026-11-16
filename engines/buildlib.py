# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Small build driver shared by the engine recipes.

Each engine has a recipe (``engines/win/<engine>/recipe.py``) that stages a copy of
its pristine upstream source, applies its scripted transformations and then calls
the helpers here to compile and link. Nothing here knows about a particular engine.

Three places can build:
  * Linux, target ``linux``:    the same transformed tree, to prove the port changes nothing there;
  * Linux, target ``windows``:  a MinGW-w64 UCRT cross-compiler (compile and link only);
  * Windows, target ``windows``: g++ from MSYS2 UCRT64 on a CI runner (the release build).

Standard library only.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

REPO = Path(__file__).resolve().parent.parent
TARGETS = ("linux", "windows")
#: Where a locally cross-built zlib for Windows lives (include/ and lib/), if any.
XDEPS_WIN = REPO / "build" / "xdeps" / "win"
#: Name of the MinGW-w64 cross-compiler, and conda environments where it may live.
CROSS_CXX_NAME = "x86_64-w64-mingw32-g++"


def _default_cross_cxx() -> str:
    """The cross-compiler on PATH, or in a conda environment named ``tesswin-x``."""
    found = shutil.which(CROSS_CXX_NAME)
    if found:
        return found
    roots = [os.environ.get("CONDA_ROOT", ""), os.path.expanduser("~/miniconda3"),
             os.path.expanduser("~/miniforge3"), os.path.expanduser("~/anaconda3")]
    for root in roots:
        candidate = os.path.join(root, "envs", "tesswin-x", "bin", CROSS_CXX_NAME)
        if root and os.path.exists(candidate):
            return candidate
    return CROSS_CXX_NAME


#: Kept for callers that want to know where the cross toolchain is.
DEFAULT_CROSS_CXX = _default_cross_cxx()


class BuildError(Exception):
    pass


@dataclass
class Toolchain:
    target: str                      # "linux" or "windows"
    cxx: str
    windres: str = ""                # windows only
    strip: str = "strip"
    objdump: str = "objdump"
    include_dirs: List[str] = field(default_factory=list)   # extra -I (zlib for the cross build)
    lib_dirs: List[str] = field(default_factory=list)       # extra -L
    cross: bool = False              # building for Windows on a non-Windows host

    @property
    def exe(self) -> str:
        return ".exe" if self.target == "windows" else ""

    def version(self) -> str:
        try:
            out = subprocess.run([self.cxx, "--version"], capture_output=True, text=True, check=True)
            return out.stdout.splitlines()[0].strip()
        except (OSError, subprocess.CalledProcessError, IndexError):
            return "unknown"


def detect_toolchain(target: str, cxx: Optional[str] = None) -> Toolchain:
    """Choose the compiler for ``target``. ``cxx`` or ``$CXX`` overrides the default."""
    if target not in TARGETS:
        raise BuildError("unknown target %r (linux or windows)" % target)
    cxx = cxx or os.environ.get("CXX") or ""
    if target == "linux":
        return Toolchain(target="linux", cxx=cxx or "g++")
    if os.name == "nt":                                   # native MSYS2 / MinGW g++
        return Toolchain(target="windows", cxx=cxx or "g++", windres="windres",
                         strip="strip", objdump="objdump")
    cxx = cxx or os.environ.get("TESSERACT_WIN_CROSS_CXX") or _default_cross_cxx()
    if shutil.which(cxx) is None and not os.path.exists(cxx):
        raise BuildError("no Windows cross-compiler found (looked for %s)" % cxx)
    prefix = cxx[:-len("g++")] if cxx.endswith("g++") else cxx.rsplit("-", 1)[0] + "-"
    tc = Toolchain(target="windows", cxx=cxx, windres=prefix + "windres", strip=prefix + "strip",
                   objdump=prefix + "objdump", cross=True)
    if (XDEPS_WIN / "lib" / "libz.a").exists():
        tc.include_dirs.append(str(XDEPS_WIN / "include"))
        tc.lib_dirs.append(str(XDEPS_WIN / "lib"))
    return tc


def common_cxxflags(tc: Toolchain) -> List[str]:
    """Flags every engine is built with, on both targets, so the two builds stay comparable.

    Baseline x86-64 only: the programs must start on any 64-bit PC.
    """
    flags = ["-std=c++17", "-O3", "-march=x86-64", "-mtune=generic", "-ffp-contract=off",
             "-pthread", "-Wall", "-Wextra", "-Wno-unused-parameter"]
    flags += ["-I" + d for d in tc.include_dirs]
    return flags


def common_ldflags(tc: Toolchain) -> List[str]:
    flags = ["-pthread"] + ["-L" + d for d in tc.lib_dirs]
    if tc.target == "windows":
        # One self-contained .exe; a 16 MB stack because the gap filler recurses deeply.
        flags += ["-static", "-Wl,--stack,16777216"]
    return flags


def run(cmd: Sequence[str], *, cwd: Optional[Path] = None, log: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None) -> str:
    """Run a command; raise :class:`BuildError` with its output if it fails."""
    proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("$ %s\n%s\n" % (" ".join(str(c) for c in cmd), proc.stdout))
    if proc.returncode != 0:
        raise BuildError("command failed (%d): %s\n%s" % (proc.returncode, " ".join(str(c) for c in cmd),
                                                           proc.stdout[-4000:]))
    return proc.stdout


def stage_tree(upstream: Path, stage: Path, *, ignore: Iterable[str] = ("build", ".git")) -> None:
    """Make ``stage`` a fresh copy of ``upstream``. The upstream tree is never written to."""
    if stage.exists():
        shutil.rmtree(stage)
    stage.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(upstream, stage, ignore=shutil.ignore_patterns(*ignore))


def compile_objects(tc: Toolchain, sources: Sequence[Path], objdir: Path, flags: Sequence[str], *,
                    jobs: int = 2, per_file_flags: Optional[Dict[str, Sequence[str]]] = None,
                    log: Optional[Path] = None) -> List[Path]:
    """Compile every source to ``objdir`` in parallel; return the object files in source order.

    ``per_file_flags`` maps a source file name (``util.cpp``) to extra flags for that file.
    Any warning is kept in ``log``; the caller decides whether warnings matter.
    """
    objdir.mkdir(parents=True, exist_ok=True)
    per_file_flags = per_file_flags or {}

    def one(src: Path) -> Path:
        obj = objdir / (src.stem + ".o")
        cmd = [tc.cxx, *flags, *per_file_flags.get(src.name, ()), "-c", str(src), "-o", str(obj)]
        run(cmd, log=log)
        return obj

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return list(pool.map(one, sources))


def compile_resource(tc: Toolchain, rc: Path, objdir: Path, *, include_dirs: Sequence[Path] = (),
                     log: Optional[Path] = None) -> Path:
    """Compile a Windows resource script (manifest, version information) to an object file."""
    objdir.mkdir(parents=True, exist_ok=True)
    obj = objdir / (rc.stem + "_res.o")
    cmd = [tc.windres, "-O", "coff", "-i", str(rc), "-o", str(obj)]
    for d in include_dirs:
        cmd += ["-I", str(d)]
    run(cmd, cwd=rc.parent, log=log)
    return obj


def link(tc: Toolchain, objects: Sequence[Path], out: Path, ldflags: Sequence[str], libs: Sequence[str], *,
         log: Optional[Path] = None) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".new")
    run([tc.cxx, *[str(o) for o in objects], *ldflags, *libs, "-o", str(tmp)], log=log)
    os.replace(tmp, out)
    return out


def strip_binary(tc: Toolchain, path: Path) -> None:
    run([tc.strip, str(path)])


def dll_imports(tc: Toolchain, exe: Path) -> List[str]:
    """DLLs a Windows program needs at start-up (lower case). Empty list for other targets."""
    if tc.target != "windows":
        return []
    out = run([tc.objdump, "-p", str(exe)])
    names = [line.split("DLL Name:", 1)[1].strip().lower() for line in out.splitlines() if "DLL Name:" in line]
    return sorted(set(names))


#: A statically linked engine may import only DLLs that are part of Windows itself.
def is_system_dll(name: str) -> bool:
    name = name.lower()
    return name.startswith(("api-ms-win-", "ext-ms-win-")) or name in {
        "kernel32.dll", "ucrtbase.dll", "psapi.dll", "advapi32.dll", "user32.dll", "shell32.dll",
        "ntdll.dll", "bcrypt.dll", "ws2_32.dll", "msvcrt.dll", "ole32.dll", "shlwapi.dll",
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    """One checksum for a whole source tree (relative paths and contents, sorted)."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(sha256(path)))
    return digest.hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")


def say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()
