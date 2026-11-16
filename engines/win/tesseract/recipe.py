# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Build recipe of the TesserACT engine (``tesseract-asm``) for Linux and for Windows.

Nothing under ``engines/tesseract/upstream`` is edited. The Windows port is

  * a compatibility layer in added files: ``ts_compat.h`` (force-included into every source),
    ``ts_compat.cpp``, ``ts_selftest.cpp``, ``dev_fork_batch_win.cpp``, ``include/sys/*.h``;
  * two scripted transformations of a staged copy: ``llp64_transform.py`` (every engine-owned
    ``long`` becomes ``long long``) and ``win_rules.py`` (exact-text rules and guards);
  * a manifest (UTF-8 code page, long paths) and a version resource, written here at build time.

The same transformed tree is built for both targets with ``buildlib.common_cxxflags`` and
``buildlib.common_ldflags``. On Linux the compatibility header is inert, so a Linux build of the
staged tree that still equals the reference binary proves the port changed nothing there.

Used by ``engines/build.py`` (``stage``, ``build``). The upstream suites are served by
``build`` (it leaves ``tesseract-asm`` and ``tesseract-model`` in the staged tree, where
``tests/run_tests.sh`` looks for them) and by ``build_tests`` / ``run_tests`` for the C++ test
programs, also from the command line:

    python3 engines/win/tesseract/recipe.py unit-tests --target linux --jobs 4 --run
    python3 engines/win/tesseract/recipe.py check-imports build/engines/linux/tesseract-asm

Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
for _entry in (str(REPO / "engines"), str(REPO)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import buildlib  # noqa: E402
from tesseract_win import branding, versions  # noqa: E402

NAME = "tesseract"
BINARY = "tesseract-asm"
#: The model builder. Not shipped; ``tests/run_tests.sh`` needs it beside the engine.
MODEL_BINARY = "tesseract-model"

#: Sources of the compatibility layer linked into every program, on both targets.
COMPAT_SOURCES = ("ts_compat.cpp", "ts_selftest.cpp")
#: Windows only: takes the place of ``src/dev_fork_batch.cpp`` (fork, signals, /proc).
FORK_BATCH_UPSTREAM = "dev_fork_batch.cpp"
FORK_BATCH_WINDOWS = "dev_fork_batch_win.cpp"
#: Linked into the upstream test programs only (setenv, unsetenv, mkdtemp for Windows).
TEST_COMPAT_SOURCES = ("ts_compat_tests.cpp",)
#: Programs of the upstream C++ suite that are not built for Windows, one per line with the reason.
TESTS_SKIP = HERE / "tests-skip.txt"

#: What the LLP64 rewrite is applied to.
TRANSFORM_GLOBS = ("src/*.cpp", "src/*.h", "tests/*.cpp", "tests/*.h")
#: What the LLP64 rewrite must find in a pinned release: files changed, then rewrites by kind.
#: A different count for a release listed here stops the build. A release that is not listed is
#: transformed and its counts are reported, to be added here when that release is pinned.
LLP64_EXPECTED = {
    "1.6.2": {"files": 38, "type": 280, "func": 38, "literal": 8, "format": 15},
}

#: C-library symbols a Linux build of the staged tree is known to import (see the file's header).
LIBC_IMPORTS = HERE / "libc_imports.txt"

LOG_DIR = REPO / "build" / "logs" / "tesseract"


def _load(name: str):
    """Import a sibling script as a module (this folder is not a package)."""
    spec = importlib.util.spec_from_file_location("tesseract_recipe_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


llp64_transform = _load("llp64_transform")
win_rules = _load("win_rules")


# --------------------------------------------------------------------------------------------- staging

def upstream_version(tree: Path) -> str:
    """The version the source itself states (``kVersion`` in ``src/version.h``)."""
    header = Path(tree) / "src" / "version.h"
    try:
        text = header.read_text(encoding="utf-8")
    except OSError as exc:
        raise buildlib.BuildError("cannot read %s: %s" % (header, exc))
    match = re.search(r'kVersion\s*=\s*"([^"]+)"', text)
    if not match:
        raise buildlib.BuildError("no kVersion in %s -- upstream moved the version; review recipe.py" % header)
    return match.group(1)


def transform(tree: Path) -> dict:
    """Apply every scripted transformation to the tree at ``tree``, in place.

    Idempotent: a second call changes nothing and reports zero rewrites. Raises
    ``buildlib.BuildError`` when a rule or a guard does not find exactly what it expects.
    """
    tree = Path(tree)
    totals = {"files": 0, "type": 0, "func": 0, "literal": 0, "format": 0}
    seen = 0
    for pattern in TRANSFORM_GLOBS:
        for path in sorted(tree.glob(pattern)):
            seen += 1
            try:
                with open(path, encoding="utf-8", newline="") as handle:
                    text = handle.read()
                new, counts = llp64_transform.transform(text)
            except (OSError, ValueError) as exc:
                raise buildlib.BuildError("LLP64 rewrite: %s: %s" % (path.relative_to(tree).as_posix(), exc))
            if new == text:
                continue
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(new)
            totals["files"] += 1
            for key, n in zip(("type", "func", "literal", "format"), counts):
                totals[key] += n
    if seen == 0:
        raise buildlib.BuildError("LLP64 rewrite: no source file under %s -- upstream moved src/ or tests/" % tree)
    try:
        found = win_rules.apply(tree)
    except win_rules.RuleError as exc:
        raise buildlib.BuildError(str(exc))
    return {"llp64": totals, "rules": found["rules"], "guards": found["guards"]}


def stage(upstream: Path, stage: Path, target: str) -> dict:
    """Make ``stage`` a fresh copy of ``upstream`` and apply every scripted transformation.

    The staged tree is the same for both targets. Returns the counts of what was rewritten.
    Fails loudly (``buildlib.BuildError``) when upstream no longer matches what a rule expects.
    """
    if target not in buildlib.TARGETS:
        raise buildlib.BuildError("unknown target %r (linux or windows)" % target)
    upstream, stage = Path(upstream), Path(stage)
    if not (upstream / "src" / "main.cpp").is_file():
        raise buildlib.BuildError("%s does not hold the TesserACT source (no src/main.cpp)" % upstream)
    buildlib.stage_tree(upstream, stage)
    version = upstream_version(stage)
    counts = transform(stage)
    expected = LLP64_EXPECTED.get(version)
    counts["version"] = version
    counts["llp64_pinned"] = expected is not None
    if expected is not None and counts["llp64"] != expected:
        raise buildlib.BuildError(
            "LLP64 rewrite of TesserACT %s changed %s but %s is pinned in recipe.py (LLP64_EXPECTED) -- the "
            "source or llp64_transform.py is not what was audited" % (version, counts["llp64"], expected))
    return counts


_stage = stage      # build() has a parameter of the same name


# -------------------------------------------------------------------------------------------- compiling

def object_dir(stage: Path) -> Path:
    """Where the objects of a staged tree go: beside it, so the tree itself stays source only."""
    stage = Path(stage)
    return stage.parent / (stage.name + "-obj")


def cxxflags(tc: buildlib.Toolchain) -> List[str]:
    """Compiler flags of every engine source: the shared ones plus the force-included header."""
    flags = buildlib.common_cxxflags(tc) + ["-Werror=format", "-include", str(HERE / "ts_compat.h")]
    if tc.target == "windows":
        flags += ["-I" + str(HERE / "include"),          # sys/wait.h, sys/resource.h
                  "-D_FILE_OFFSET_BITS=64",
                  "-D__USE_MINGW_ANSI_STDIO=1",          # FASTA headers carry printf("%.4f")
                  "-D_WIN32_WINNT=0x0A00"]               # Windows 10
    return flags


def ldflags_for(tc: buildlib.Toolchain) -> List[str]:
    """Linker flags: the shared ones and, for Windows, symbols stripped while linking and no link
    time in the program's header, so that the same source and compiler give the same bytes (and
    the same sha256 in BUILD_INFO.json). A separate ``strip`` would stamp the time back in."""
    flags = buildlib.common_ldflags(tc)
    if tc.target == "windows":
        flags += ["-s", "-Wl,--no-insert-timestamp"]
    return flags


def git_commit(version: str) -> str:
    """What ``report.json`` records as the commit the engine was built from."""
    if version == versions.TESSERACT_VERSION and versions.TESSERACT_COMMIT:
        return versions.TESSERACT_COMMIT[:12]
    return "unknown"


def _version_numbers(version: str) -> List[int]:
    numbers = [int(n) for n in re.findall(r"\d+", version)[:4]]
    return (numbers + [0, 0, 0, 0])[:4]


MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity type="win32" name="{vendor}.TesserACT.{name}" version="{dotted}" processorArchitecture="amd64"/>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security>
      <requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
      </requestedPrivileges>
    </security>
  </trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">
    <application>
      <supportedOS Id="{{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}}"/>
    </application>
  </compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8</activeCodePage>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
    </windowsSettings>
  </application>
</assembly>
"""

# Numbers instead of the names of <winver.h>, so the script needs no include path.
# 24 is RT_MANIFEST and 1 is the identifier Windows reads the manifest of a program from.
RESOURCE = """// Generated by engines/win/tesseract/recipe.py. Do not edit.
1 24 "{name}.manifest"

1 VERSIONINFO
FILEVERSION {commas}
PRODUCTVERSION {commas}
FILEFLAGSMASK 0x3fL
FILEFLAGS 0x0L
FILEOS 0x40004L
FILETYPE 0x1L
FILESUBTYPE 0x0L
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "040904b0"
    BEGIN
      VALUE "CompanyName", "{vendor}\\0"
      VALUE "FileDescription", "{description}\\0"
      VALUE "FileVersion", "{version}\\0"
      VALUE "InternalName", "{name}\\0"
      VALUE "LegalCopyright", "Copyright (c) 2026 The TesserACT authors. MIT licence.\\0"
      VALUE "OriginalFilename", "{name}.exe\\0"
      VALUE "ProductName", "TesserACT\\0"
      VALUE "ProductVersion", "{version}\\0"
      VALUE "Comments", "Windows build made by {app} from the TesserACT {version} source (commit {commit}).\\0"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x409, 1200
  END
END
"""


def write_resource(folder: Path, name: str, description: str, version: str) -> Path:
    """Write ``<name>.manifest`` and ``<name>.rc`` (manifest plus version information) to ``folder``."""
    folder.mkdir(parents=True, exist_ok=True)
    numbers = _version_numbers(version)
    fields = {
        "name": name, "description": description, "version": version,
        "vendor": branding.VENDOR, "app": branding.APP_NAME, "commit": git_commit(version),
        "dotted": ".".join(str(n) for n in numbers), "commas": ",".join(str(n) for n in numbers),
    }
    for value in fields.values():
        if '"' in value or "\\" in value or not value.isascii():
            raise buildlib.BuildError("cannot put %r into the version resource" % value)
    for suffix, template in ((".manifest", MANIFEST), (".rc", RESOURCE)):
        with open(folder / (name + suffix), "w", encoding="ascii", newline="\n") as handle:
            handle.write(template.format(**fields))
    return folder / (name + ".rc")


def _reset_log(log: Path) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("")


def _warnings(log: Path) -> Dict[str, List[str]]:
    """Compiler warnings in a build log, split into those of this folder's files and upstream's."""
    own: List[str] = []
    upstream: List[str] = []
    here = HERE.as_posix()
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        if "warning:" not in line or line.startswith("$ "):
            continue
        where = line.split("warning:", 1)[0].replace("\\", "/")
        (own if here in where else upstream).append(line.strip())
    return {"own": own, "upstream": upstream}


def _compile_all(tc: buildlib.Toolchain, stage: Path, version: str, jobs: int, log: Path) -> Dict[str, List[Path]]:
    """Compile the staged engine and the compatibility layer. Returns the object lists by role."""
    src = stage / "src"
    sources = sorted(src.glob("*.cpp"))
    names = {p.name for p in sources}
    for needed in ("main.cpp", "model_main.cpp", "util.cpp", "p2_provenance.cpp", FORK_BATCH_UPSTREAM):
        if needed not in names:
            raise buildlib.BuildError("src/%s is missing -- upstream moved it; review recipe.py" % needed)
    if tc.target == "windows":
        sources = [p for p in sources if p.name != FORK_BATCH_UPSTREAM]
    objdir = object_dir(stage)
    if objdir.exists():
        shutil.rmtree(objdir)
    flags = cxxflags(tc)
    per_file = {"p2_provenance.cpp": ['-DTS_GIT_COMMIT="%s"' % git_commit(version)]}
    if tc.target == "windows":
        per_file["util.cpp"] = ["-DTS_COMPAT_UTIL_TU"]       # see ts_compat.h, section 6
    engine = buildlib.compile_objects(tc, sources, objdir / "src", flags, jobs=jobs,
                                      per_file_flags=per_file, log=log)
    # The layer itself: with the engine's headers in reach, and no warning allowed.
    own_flags = flags + ["-I" + str(src), "-Werror"]
    own_sources = [HERE / name for name in COMPAT_SOURCES + TEST_COMPAT_SOURCES]
    if tc.target == "windows":
        own_sources.append(HERE / FORK_BATCH_WINDOWS)
    own = buildlib.compile_objects(tc, own_sources, objdir / "compat", own_flags, jobs=jobs, log=log)
    by_name = {o.stem: o for o in own}
    compat = [by_name[Path(name).stem] for name in COMPAT_SOURCES]
    if tc.target == "windows":
        compat.append(by_name[Path(FORK_BATCH_WINDOWS).stem])
    return {
        "library": [o for o in engine if o.stem not in ("main", "model_main")],
        "main": [o for o in engine if o.stem == "main"],
        "model_main": [o for o in engine if o.stem == "model_main"],
        "compat": compat,
        "test_compat": [by_name[Path(name).stem] for name in TEST_COMPAT_SOURCES],
    }


def _resource_object(tc: buildlib.Toolchain, stage: Path, name: str, description: str, version: str,
                     log: Path) -> List[Path]:
    if tc.target != "windows":
        return []
    folder = object_dir(stage) / "res"
    rc = write_resource(folder, name, description, version)
    return [buildlib.compile_resource(tc, rc, folder, log=log)]


def _check_windows_program(tc: buildlib.Toolchain, exe: Path) -> dict:
    """Facts of a linked Windows program that must hold; raises when one does not."""
    header = buildlib.run([tc.objdump, "-p", str(exe)])
    match = re.search(r"SizeOfStackReserve\s+([0-9a-fA-F]+)", header)
    reserve = int(match.group(1), 16) if match else 0
    if reserve < 16 * 1024 * 1024:
        raise buildlib.BuildError("%s reserves a stack of %d bytes; the gap filler needs 16 MiB" % (exe.name, reserve))
    sections = buildlib.run([tc.objdump, "-h", str(exe)])
    if not re.search(r"\s\.rsrc\s", sections):
        raise buildlib.BuildError("%s has no resource section: the UTF-8 manifest is missing" % exe.name)
    return {"stack_reserve": reserve, "has_resources": True}


def new_libc_imports(program: Path, nm: str = "nm") -> Optional[List[str]]:
    """C-library symbols a Linux program imports that ``libc_imports.txt`` does not list.

    A new name after an upstream release is the earliest sign of a new POSIX or libm dependency
    for the Windows port to cover. ``None`` when ``nm`` cannot read the program.
    """
    try:
        out = subprocess.run([nm, "-D", "--undefined-only", str(program)], capture_output=True, text=True,
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    with open(LIBC_IMPORTS, encoding="utf-8") as handle:
        known = {line.strip() for line in handle if line.strip() and not line.startswith("#")}
    found = set()
    for line in out.splitlines():
        parts = line.split()
        name = parts[-1].split("@", 1)[0] if parts else ""
        if name and not name.startswith("_Z"):          # _Z...: C++ symbols of libstdc++, not the C library
            found.add(name)
    return sorted(found - known)


def build(tc: buildlib.Toolchain, upstream: Path, stage: Path, out_dir: Path, jobs: int) -> dict:
    """Stage, compile and link ``out_dir/tesseract-asm`` (``.exe`` for Windows).

    Also leaves ``tesseract-asm`` and ``tesseract-model`` in the staged tree, where the upstream
    end-to-end suite (``tests/run_tests.sh``) expects them. Returns facts for BUILD_INFO.json.
    """
    upstream, stage, out_dir = Path(upstream), Path(stage), Path(out_dir)
    log = LOG_DIR / ("build-%s.log" % tc.target)
    _reset_log(log)
    counts = _stage(upstream, stage, tc.target)
    version = counts["version"]
    if version != versions.TESSERACT_VERSION:
        raise buildlib.BuildError(
            "the source in %s is TesserACT %s but tesseract_win/versions.py pins %s -- bring the new "
            "release in with engines/update.py" % (upstream, version, versions.TESSERACT_VERSION))
    objects = _compile_all(tc, stage, version, jobs, log)
    ldflags = ldflags_for(tc)
    libs = ["-lz"]

    resource = _resource_object(tc, stage, BINARY, "TesserACT assembler", version, log)
    program = out_dir / (BINARY + tc.exe)
    buildlib.link(tc, objects["main"] + objects["library"] + objects["compat"] + resource,
                  program, ldflags, libs, log=log)
    facts = {
        "version": version,
        "git_commit": git_commit(version),
        "upstream_tree_sha256": buildlib.tree_sha256(upstream),
        "transformations": counts,
        "cxxflags": [f.replace(str(REPO) + os.sep, "") for f in cxxflags(tc)],
        "ldflags": [f.replace(str(REPO) + os.sep, "") for f in ldflags + libs],
        "log": log.relative_to(REPO).as_posix(),
    }
    if tc.target == "windows":
        facts.update(_check_windows_program(tc, program))
    elif os.name != "nt":
        facts["new_libc_imports"] = new_libc_imports(program)      # informative; see libc_imports.txt
    shutil.copy2(program, stage / program.name)

    # The model builder: the upstream end-to-end suite builds a model with it. Never shipped.
    model_resource = _resource_object(tc, stage, MODEL_BINARY, "TesserACT model builder", version, log)
    buildlib.link(tc, objects["model_main"] + objects["library"] + objects["compat"] + model_resource,
                  stage / (MODEL_BINARY + tc.exe), ldflags, libs, log=log)

    warnings = _warnings(log)
    facts["warnings"] = {"own": len(warnings["own"]), "upstream": len(warnings["upstream"])}
    if warnings["own"]:
        raise buildlib.BuildError("the compatibility layer compiles with warnings:\n" + "\n".join(warnings["own"][:20]))
    return facts


# ------------------------------------------------------------------------- the upstream C++ test programs

def read_skip_list(path: Optional[Path] = None) -> Dict[str, str]:
    """``tests-skip.txt``: program name, then the reason. Lines starting with ``#`` are comments."""
    skip: Dict[str, str] = {}
    with open(path or TESTS_SKIP, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, _, reason = line.partition(" ")
            skip[name] = reason.strip()
    return skip


def test_programs(stage: Path, target: str) -> Dict[str, List[str]]:
    """Names of the upstream C++ test programs to build for ``target`` and of those left out.

    ``test_dev_fork_batch`` is a helper of a Python test, not a test, on every platform
    (upstream's own Makefile leaves it out of ``componenttest``).
    """
    names = sorted(p.stem for p in (Path(stage) / "tests").glob("test_*.cpp"))
    skip = set(read_skip_list()) if target == "windows" else {"test_dev_fork_batch"}
    unknown = sorted(set(read_skip_list()) - set(names))
    if unknown:
        raise buildlib.BuildError("tests-skip.txt names programs upstream does not have: %s" % ", ".join(unknown))
    return {"build": [n for n in names if n not in skip], "skip": [n for n in names if n in skip]}


def build_tests(tc: buildlib.Toolchain, stage: Path, jobs: int = 2, *, names: Optional[Sequence[str]] = None,
                keep: bool = True) -> dict:
    """Build the upstream C++ test programs against the objects ``build`` left behind.

    Each program is ``<stage>/build/<name>`` (``.exe`` for Windows), as upstream's Makefile puts
    them. ``keep=False`` deletes each program once it has linked (a link check on a small disk).
    Returns ``{"built": [...], "failed": {name: last lines of the compiler}, "skipped": [...]}``.
    """
    stage = Path(stage)
    objdir = object_dir(stage)
    version = upstream_version(stage)
    log = LOG_DIR / ("tests-%s.log" % tc.target)
    _reset_log(log)

    def objs(folder: str, stems: Sequence[str] = ()) -> List[Path]:
        found = sorted((objdir / folder).glob("*.o"))
        return [o for o in found if not stems or o.stem in stems]

    library = [o for o in objs("src") if o.stem not in ("main", "model_main")]
    compat = objs("compat")                      # the layer, the test shims and (Windows) the fork-batch stub
    if not library or not compat:
        raise buildlib.BuildError("no objects in %s -- build the engine first (engines/build.py)" % objdir)
    resource = _resource_object(tc, stage, "tesseract-test", "TesserACT test program", version, log)
    plan = test_programs(stage, tc.target)
    wanted = [n for n in plan["build"] if names is None or n in names]
    out_dir = stage / "build"
    out_dir.mkdir(parents=True, exist_ok=True)
    flags = cxxflags(tc) + ["-I" + str(stage / "src")]
    tail = [str(o) for o in library + compat + resource] + ldflags_for(tc) + ["-lz"]

    import concurrent.futures

    def one(name: str):
        program = out_dir / (name + tc.exe)
        try:
            buildlib.run([tc.cxx, *flags, str(stage / "tests" / (name + ".cpp")), *tail, "-o", str(program)], log=log)
        except buildlib.BuildError as exc:
            return name, str(exc)
        if not keep:
            program.unlink()
        return name, ""

    built: List[str] = []
    failed: Dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for name, error in pool.map(one, wanted):
            if error:
                failed[name] = error[-1500:]
            else:
                built.append(name)
    return {"built": built, "failed": failed, "skipped": plan["skip"], "log": str(log)}


def run_tests(tc: buildlib.Toolchain, stage: Path, names: Sequence[str], *, timeout: int = 1800) -> Dict[str, int]:
    """Run built test programs with every ``TESSERACT_*`` variable removed. Returns exit statuses."""
    stage = Path(stage)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TESSERACT_")}
    log = LOG_DIR / ("tests-run-%s.log" % tc.target)
    _reset_log(log)
    statuses: Dict[str, int] = {}
    for name in names:
        program = stage / "build" / (name + tc.exe)
        try:
            proc = subprocess.run([str(program)], cwd=str(stage), env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=timeout)
            status, output = proc.returncode, proc.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired as exc:
            status, output = 124, (exc.stdout or b"").decode("utf-8", "replace") + "\n[timed out]"
        except OSError as exc:
            status, output = 127, str(exc)
        statuses[name] = status
        with open(log, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("== %s (exit %d)\n%s\n" % (name, status, output))
    return statuses


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="TesserACT recipe: stage the source or build the upstream C++ tests.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_stage = sub.add_parser("stage", help="stage and transform the upstream source, then stop")
    p_stage.add_argument("--target", choices=buildlib.TARGETS, default="windows" if os.name == "nt" else "linux")
    p_tests = sub.add_parser("unit-tests", help="build (and run) the upstream C++ test programs of a built tree")
    p_tests.add_argument("--target", choices=buildlib.TARGETS, default="windows" if os.name == "nt" else "linux")
    p_tests.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    p_tests.add_argument("--cxx", default=None)
    p_tests.add_argument("--run", action="store_true", help="run each program after building it")
    p_tests.add_argument("--discard", action="store_true", help="delete each program once it has linked")
    p_imports = sub.add_parser("check-imports", help="list C-library imports of a Linux build that are new")
    p_imports.add_argument("program")
    args = parser.parse_args(argv)
    if args.command == "check-imports":
        new = new_libc_imports(Path(args.program))
        if new is None:
            sys.stderr.write("failed: nm cannot read %s\n" % args.program)
            return 1
        buildlib.say("new C-library imports (review for the Windows port): %s" % ", ".join(new) if new
                     else "no new C-library imports")
        return 1 if new else 0
    staged = REPO / "build" / "stage" / args.target / NAME
    try:
        if args.command == "stage":
            counts = stage(REPO / "engines" / NAME / "upstream", staged, args.target)
            buildlib.say("staged %s: %s" % (staged, counts))
            return 0
        tc = buildlib.detect_toolchain(args.target, args.cxx)
        result = build_tests(tc, staged, args.jobs, keep=not args.discard)
        buildlib.say("built %d, failed %d, skipped %d (%s)" % (len(result["built"]), len(result["failed"]),
                                                               len(result["skipped"]), ", ".join(result["skipped"])))
        for name, error in sorted(result["failed"].items()):
            buildlib.say("FAILED TO BUILD %s\n%s" % (name, error))
        bad = len(result["failed"])
        if args.run and not args.discard:
            statuses = run_tests(tc, staged, result["built"])
            for name, status in sorted(statuses.items()):
                if status != 0:
                    buildlib.say("FAILED %s (exit %d)" % (name, status))
                    bad += 1
            buildlib.say("ran %d, passed %d" % (len(statuses), sum(1 for s in statuses.values() if s == 0)))
        return 1 if bad else 0
    except buildlib.BuildError as exc:
        sys.stderr.write("failed: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
