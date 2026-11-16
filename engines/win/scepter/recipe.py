# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Build recipe for scepter, the read trimmer.

``engines/build.py`` calls :func:`build`. Nothing under ``engines/scepter/upstream`` is
ever written to: the source is copied to a stage folder and the Windows support is
added there by script.

What is added to the staged copy (the same for both targets, so one tree is built and
tested on Linux and shipped for Windows):

* ``port/scepter_port.h``        force-included into every upstream source file;
* ``port/scepter_port.cpp``      the Windows side of the redirections and the real ``main``;
* ``port/scepter_selftest.cpp``  the hidden self-test (``SCEPTER_PORT_SELFTEST=1``);
* ``port/scepter.rc``, ``port/scepter.manifest``   generated here: UTF-8 code page,
  long-path awareness and the version information of ``scepter.exe``;
* the scripted transformations and guards of ``transform.py``.

Standard library only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
_ENGINES = HERE.parent.parent
if str(_ENGINES) not in sys.path:
    sys.path.insert(0, str(_ENGINES))
import buildlib  # noqa: E402

if str(buildlib.REPO) not in sys.path:
    sys.path.insert(0, str(buildlib.REPO))
from tesseract_win import branding, versions  # noqa: E402

__all__ = ["NAME", "BINARY", "stage", "build", "transform", "manifest_text", "resource_text",
           "pe_resources", "windows_identity", "windows_resources", "PORT_DIR", "PORT_SOURCES", "LOG_DIR"]

NAME = "scepter"
BINARY = "scepter"

#: Files copied unchanged into the folder added to the staged tree.
PORT_SOURCES = ("scepter_port.h", "scepter_port.cpp", "scepter_selftest.cpp")
MARKER_TEXT = (
    "This folder is not part of scepter. It was added by engines/win/scepter/recipe.py of\n"
    "TesserACT-Win: the compatibility layer of the native Windows build, its self-test,\n"
    "and the generated resource files. Edit the originals in engines/win/scepter/, not\n"
    "these copies.\n"
)
FILE_DESCRIPTION = "scepter read trimmer"
LOG_DIR = buildlib.REPO / "build" / "logs" / NAME

RT_VERSION, RT_MANIFEST = 16, 24
#: Windows Vista, 7, 8, 8.1 and 10/11, as in the default manifest of MinGW toolchains.
SUPPORTED_OS = (
    "e2011457-1546-43c5-a5fe-008deee3d3f0",
    "35138b9a-5d96-4fbd-8e2d-a2440225f93a",
    "4a2f28e3-53b9-4441-ba9c-d69d4a4a6e38",
    "1f676c76-80e1-4239-95bb-83d0f6d0da78",
    "8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a",
)


def _load_transform():
    """transform.py, loaded by path under its own name (both recipes share one process)."""
    name = "recipe_scepter_transform"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / "transform.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


transform_rules = _load_transform()
#: The folder added to the staged tree and the file that marks it as ours. Only stage()
#: writes that file; transform() and transform.py rewrite a tree only when it is there.
PORT_DIR, PORT_MARKER = transform_rules.STAGE_MARKER.split("/")


# ---------------------------------------------------------------------------------------
# Generated Windows resources
# ---------------------------------------------------------------------------------------

def _version_quad(version: str) -> Tuple[int, int, int, int]:
    """'1.1.0' -> (1, 1, 0, 0). Anything after the numbers (a suffix) is ignored."""
    numbers = [int(n) for n in re.findall(r"\d+", version.split("-")[0].split("+")[0])][:4]
    if not numbers:
        raise buildlib.BuildError("cannot read a version number from %r" % version)
    quad = (numbers + [0, 0, 0, 0])[:4]
    if any(n > 65535 for n in quad):
        raise buildlib.BuildError("version %r does not fit a Windows version resource" % version)
    return quad[0], quad[1], quad[2], quad[3]


def manifest_text(version: str) -> str:
    """The application manifest of scepter.exe.

    ``activeCodePage`` UTF-8 makes ``argv`` and every file name the C runtime sees UTF-8
    on Windows 10 1903 and later, so a path with any character works without touching
    upstream's file handling. ``longPathAware`` lifts the 260-character limit where the
    PC allows it. The ``compatibility`` block is the one of the default manifest that
    MinGW toolchains link in and that this one replaces (Windows Vista to 10; 11 uses
    the same identifier as 10): without it Windows would treat the program as written
    for Vista. No comment inside: this text is read by Windows itself when the program
    starts, and a manifest it rejects stops the program.
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">',
        '  <assemblyIdentity type="win32" name="%s.scepter" version="%d.%d.%d.%d"/>'
        % ((branding.VENDOR,) + _version_quad(version)),
        '  <description>%s</description>' % FILE_DESCRIPTION,
        '  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">',
        '    <security>',
        '      <requestedPrivileges>',
        '        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>',
        '      </requestedPrivileges>',
        '    </security>',
        '  </trustInfo>',
        '  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1">',
        '    <application>',
    ]
    lines += ['      <supportedOS Id="{%s}"/>' % guid for guid in SUPPORTED_OS]
    lines += [
        '    </application>',
        '  </compatibility>',
        '  <application xmlns="urn:schemas-microsoft-com:asm.v3">',
        '    <windowsSettings>',
        '      <activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">UTF-8</activeCodePage>',
        '      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>',
        '    </windowsSettings>',
        '  </application>',
        '</assembly>',
        '',
    ]
    return "\n".join(lines)


def windows_identity(version: str) -> Dict[str, str]:
    """The strings of the version resource (what Explorer shows under Properties, Details)."""
    return {
        "CompanyName": branding.VENDOR,
        "FileDescription": FILE_DESCRIPTION,
        "FileVersion": version,
        "InternalName": BINARY,
        "LegalCopyright": "Copyright (c) 2026 The scepter authors. MIT licence. "
                          "Windows build by %s." % branding.VENDOR,
        "OriginalFilename": BINARY + ".exe",
        "ProductName": "scepter",
        "ProductVersion": version,
    }


def resource_text(version: str) -> str:
    """The resource script: the manifest and the version information. ASCII only."""
    quad = "%d,%d,%d,%d" % _version_quad(version)
    strings = windows_identity(version)
    for key, value in strings.items():
        if not value.isascii() or '"' in value or "\\" in value:
            raise buildlib.BuildError("version resource: %s=%r must be plain ASCII" % (key, value))
    lines = [
        "// Generated by engines/win/scepter/recipe.py. Do not edit.",
        "// US English on purpose: it is the language under which the linker lets this",
        "// manifest replace the default one that MinGW toolchains add by themselves.",
        "LANGUAGE 0x09, 0x01",
        "",
        '1 %d "scepter.manifest"' % RT_MANIFEST,
        "",
        "1 VERSIONINFO",
        "FILEVERSION %s" % quad,
        "PRODUCTVERSION %s" % quad,
        "FILEFLAGSMASK 0x3f",
        "FILEFLAGS 0x0",
        "FILEOS 0x40004",
        "FILETYPE 0x1",
        "FILESUBTYPE 0x0",
        "BEGIN",
        '  BLOCK "StringFileInfo"',
        "  BEGIN",
        '    BLOCK "040904b0"',
        "    BEGIN",
    ]
    lines += ['      VALUE "%s", "%s"' % (key, strings[key]) for key in sorted(strings)]
    lines += [
        "    END",
        "  END",
        '  BLOCK "VarFileInfo"',
        "  BEGIN",
        '    VALUE "Translation", 0x409, 1200',
        "  END",
        "END",
        "",
    ]
    return "\n".join(lines)


def _write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# ---------------------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------------------

def transform(tree: Path) -> dict:
    """Add the port files to ``tree`` and run every scripted transformation and guard.

    ``tree`` is a copy made by :func:`stage`, which is the only thing that marks a tree
    as one (``port/README.txt``): any other tree, the pinned upstream folder above all,
    is refused before a byte of it is written. Running this a second time on the same
    tree changes nothing. Raises ``buildlib.BuildError`` with every problem found;
    returns the counts.
    """
    tree = Path(tree)
    if not (tree / PORT_DIR / PORT_MARKER).is_file():
        raise buildlib.BuildError(
            "%s is not a staged copy (no %s/%s): it will not be rewritten. Use stage(), which "
            "copies the source first" % (tree, PORT_DIR, PORT_MARKER))
    try:
        version = transform_rules.upstream_version(tree)
    except transform_rules.TransformError as exc:
        raise buildlib.BuildError(str(exc)) from None
    if version != versions.SCEPTER_VERSION:
        raise buildlib.BuildError(
            "the source is scepter %s but tesseract_win/versions.py pins SCEPTER_VERSION = %s. "
            "Bring the two in line (engines/update.py) before building."
            % (version, versions.SCEPTER_VERSION))

    port = tree / PORT_DIR
    for name in PORT_SOURCES:
        shutil.copyfile(HERE / name, port / name)
    _write_text(port / "scepter.manifest", manifest_text(version))
    _write_text(port / "scepter.rc", resource_text(version))

    try:
        counts = transform_rules.apply(tree, port_header=port / "scepter_port.h",
                                       port_source=port / "scepter_port.cpp")
    except transform_rules.TransformError as exc:
        raise buildlib.BuildError(str(exc)) from None
    counts["port_files"] = sorted(p.name for p in port.iterdir())
    return counts


def stage(upstream: Path, stage: Path, target: str) -> dict:
    """Make ``stage`` a fresh copy of ``upstream`` with the port applied; return the counts.

    The staged tree does not depend on ``target``: the same tree is compiled for Linux
    and for Windows. ``upstream`` is read only (checked by checksum before and after).
    """
    if target not in buildlib.TARGETS:
        raise buildlib.BuildError("unknown target %r (linux or windows)" % (target,))
    upstream, stage = Path(upstream).resolve(), Path(stage).resolve()
    if not (upstream / "src").is_dir():
        raise buildlib.BuildError("no scepter source in %s" % upstream)
    if stage == upstream or upstream in stage.parents or stage in upstream.parents:
        raise buildlib.BuildError("the stage folder %s must be outside the source %s" % (stage, upstream))
    if (upstream / PORT_DIR).exists():
        raise buildlib.BuildError(
            "the scepter source now has its own %s/ folder: choose another PORT_DIR in %s"
            % (PORT_DIR, Path(__file__).name))
    # Staging replaces the stage folder. Only an earlier stage (complete: it carries the
    # marker; or cut short: it holds nothing the source does not) may be deleted for that.
    if stage.exists() and not (stage / PORT_DIR / PORT_MARKER).is_file():
        if not stage.is_dir():
            raise buildlib.BuildError("the stage folder %s is a file: it will not be replaced" % stage)
        ours = {p.name for p in upstream.iterdir()} | {PORT_DIR}
        strangers = sorted(p.name for p in stage.iterdir() if p.name not in ours)
        if strangers:
            raise buildlib.BuildError(
                "%s is not an earlier stage of scepter (it holds %s%s): it will not be deleted. "
                "Choose another stage folder, or remove this one yourself"
                % (stage, ", ".join(strangers[:3]), " ..." if len(strangers) > 3 else ""))
    before = buildlib.tree_sha256(upstream)
    buildlib.stage_tree(upstream, stage)
    (stage / PORT_DIR).mkdir()
    _write_text(stage / PORT_DIR / PORT_MARKER, MARKER_TEXT)
    counts = transform(stage)
    if buildlib.tree_sha256(upstream) != before:
        raise buildlib.BuildError("the upstream source %s changed while it was staged" % upstream)
    counts["target"] = target
    counts["upstream_sha256"] = before
    counts["staged_sha256"] = buildlib.tree_sha256(stage)
    return counts


_stage = stage      # build() has a parameter of the same name


# ---------------------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------------------

def _target_cxxflags(tc: buildlib.Toolchain) -> List[str]:
    if tc.target != "windows":
        return []
    # MinGW's own printf (rounding that does not depend on the Windows version) and the
    # Windows 10 API level, whatever the toolchain's defaults are.
    # Not -D_FILE_OFFSET_BITS=64: it would change zlib's z_off_t in the header without
    # changing the prebuilt library.
    return ["-D__USE_MINGW_ANSI_STDIO=1", "-D_WIN32_WINNT=0x0A00"]


def _per_file_flags(tc: buildlib.Toolchain) -> Dict[str, List[str]]:
    if tc.target != "windows":
        return {}
    # report_html.cpp marks its own helper with __attribute__((format(printf, ...))).
    # For a MinGW compiler that word means the old Microsoft rules, which do not know
    # %zu, so every call is reported although the helper prints through MinGW's printf,
    # which does. The Linux build of the same tree checks the same format strings.
    return {"report_html.cpp": ["-Wno-format"]}


def _target_ldflags(tc: buildlib.Toolchain) -> List[str]:
    if tc.target != "windows":
        return []
    # No symbol table, and no link time in the header: the same source and compiler give
    # the same scepter.exe, byte for byte. Stripping at link time, not with strip
    # afterwards, because strip would write the current time back into the header.
    return ["-s", "-Wl,--no-insert-timestamp"]


def _count_warnings(log: Path, stage_dir: Path) -> Dict[str, int]:
    """Compiler warnings in the build log, split into upstream source and port files."""
    counts = {"upstream": 0, "port": 0, "other": 0}
    port = (stage_dir / PORT_DIR).as_posix() + "/"
    source = (stage_dir / "src").as_posix() + "/"
    if not log.is_file():
        return counts
    with open(log, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "warning:" not in line or line.startswith("$ "):
                continue
            where = line.replace("\\", "/")
            if where.startswith(port) or (" " + port) in where:
                counts["port"] += 1
            elif where.startswith(source) or (" " + source) in where:
                counts["upstream"] += 1
            else:
                counts["other"] += 1
    return counts


def build(tc: buildlib.Toolchain, upstream: Path, stage: Path, out_dir: Path, jobs: int) -> dict:
    """Stage, compile and link ``out_dir/scepter`` (``scepter.exe`` for Windows).

    The program is linked beside the staged tree and checked there (no warning from the
    port files; for Windows, our manifest and version strings inside it and no DLL that
    is not part of Windows). Only a program that passed is put into ``out_dir``, in one
    step: a build that fails or is interrupted leaves the previous one as it was.
    Returns the facts recorded in ``BUILD_INFO.json``.
    """
    stage, out_dir = Path(stage).resolve(), Path(out_dir).resolve()
    counts = _stage(upstream, stage, tc.target)
    log = LOG_DIR / ("build-%s.log" % tc.target)
    log.parent.mkdir(parents=True, exist_ok=True)
    _write_text(log, "scepter %s for %s with %s\n" % (counts["version"], tc.target, tc.version()))

    port = stage / PORT_DIR
    objdir = stage.parent / (stage.name + ".obj")          # beside the staged tree, not in it
    if objdir.exists():
        shutil.rmtree(objdir)
    cxxflags = buildlib.common_cxxflags(tc) + _target_cxxflags(tc)
    with_header = cxxflags + ["-include", str(port / "scepter_port.h")]
    sources = sorted((stage / "src").glob("*.cpp"))
    if not sources:
        raise buildlib.BuildError("no source files in %s" % (stage / "src"))

    objects = buildlib.compile_objects(tc, sources, objdir / "src", with_header, jobs=jobs,
                                       per_file_flags=_per_file_flags(tc), log=log)
    # The self-test sees the platform exactly as upstream code does; the port file is
    # the one place that must not have its names redirected.
    objects += buildlib.compile_objects(tc, [port / "scepter_selftest.cpp"], objdir / "port",
                                        with_header, jobs=1, log=log)
    objects += buildlib.compile_objects(tc, [port / "scepter_port.cpp"], objdir / "port",
                                        cxxflags, jobs=1, log=log)
    if tc.target == "windows":
        objects.append(buildlib.compile_resource(tc, port / "scepter.rc", objdir / "port", log=log))

    ldflags = buildlib.common_ldflags(tc) + _target_ldflags(tc)
    libs = ["-lz"]
    linked = buildlib.link(tc, objects, objdir / (BINARY + tc.exe), ldflags, libs, log=log)

    warnings = _count_warnings(log, stage)
    if warnings["port"]:
        raise buildlib.BuildError("the port files of scepter compile with %d warning(s): see %s"
                                  % (warnings["port"], log))
    resources = None
    if tc.target == "windows":
        resources = _check_windows_resources(linked, counts["version"])
        foreign = [name for name in buildlib.dll_imports(tc, linked) if not buildlib.is_system_dll(name)]
        if foreign:
            raise buildlib.BuildError("%s needs DLLs that are not part of Windows: %s"
                                      % (linked.name, ", ".join(foreign)))
    binary = out_dir / linked.name
    out_dir.mkdir(parents=True, exist_ok=True)
    arriving = binary.with_name(binary.name + ".new")
    try:
        shutil.copy2(linked, arriving)
        os.replace(arriving, binary)
    except OSError as exc:
        raise buildlib.BuildError("cannot put %s in place (%s); is that program running?"
                                  % (binary, exc)) from None

    facts = {
        "version": counts["version"],
        "upstream_sha256": counts["upstream_sha256"],
        "staged_sha256": counts["staged_sha256"],
        "transformations": counts["transformations"],
        "reviewed_lines": counts["reviewed_lines"],
        "sources": len(sources),
        "cxxflags": [f.replace(str(stage), "<stage>") for f in with_header],
        "per_file_cxxflags": _per_file_flags(tc),
        "ldflags": ldflags + libs,
        "warnings": warnings,
        "log": log.relative_to(buildlib.REPO).as_posix() if buildlib.REPO in log.parents else str(log),
    }
    if resources is not None:
        facts["windows_resources"] = resources
    buildlib.say("  scepter %s: %d sources, %d transformation(s), warnings: %d upstream, %d port, %d other"
                 % (counts["version"], len(sources), len(counts["transformations"]),
                    warnings["upstream"], warnings["port"], warnings["other"]))
    return facts


# ---------------------------------------------------------------------------------------
# Reading the resources back out of the linked program
# ---------------------------------------------------------------------------------------

def pe_resources(exe: Path) -> Dict[Tuple[object, object, object], bytes]:
    """Every resource of a Windows program as ``{(type, name, language): bytes}``.

    Enough of the PE format to check, on any platform and without running the program,
    that the manifest and the version information really are inside it.
    """
    data = Path(exe).read_bytes()
    try:
        if data[:2] != b"MZ":
            raise ValueError("no MZ header")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("no PE header")
        n_sections, = struct.unpack_from("<H", data, pe + 6)
        optional_size, = struct.unpack_from("<H", data, pe + 20)
        optional = pe + 24
        magic, = struct.unpack_from("<H", data, optional)
        directories = optional + (112 if magic == 0x20B else 96)
        rsrc_rva, rsrc_size = struct.unpack_from("<II", data, directories + 2 * 8)
        if rsrc_rva == 0 or rsrc_size == 0:
            return {}
        sections = []
        for i in range(n_sections):
            base = optional + optional_size + 40 * i
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, base + 8)
            sections.append((vaddr, max(vsize, rawsize), rawptr))

        def offset_of(rva: int) -> int:
            for vaddr, size, rawptr in sections:
                if vaddr <= rva < vaddr + size:
                    return rawptr + (rva - vaddr)
            raise ValueError("address 0x%x is in no section" % rva)

        root = offset_of(rsrc_rva)
        found: Dict[Tuple[object, object, object], bytes] = {}

        def name_of(value: int) -> object:
            if value & 0x80000000:
                at = root + (value & 0x7FFFFFFF)
                length, = struct.unpack_from("<H", data, at)
                return data[at + 2:at + 2 + 2 * length].decode("utf-16-le")
            return value

        def walk(at: int, path: Tuple[object, ...]) -> None:
            named, by_id = struct.unpack_from("<HH", data, at + 12)
            for i in range(named + by_id):
                name, where = struct.unpack_from("<II", data, at + 16 + 8 * i)
                key = path + (name_of(name),)
                if where & 0x80000000:
                    if len(key) >= 3:
                        raise ValueError("resource tree deeper than type/name/language")
                    walk(root + (where & 0x7FFFFFFF), key)
                else:
                    rva, size = struct.unpack_from("<II", data, root + where)
                    start = offset_of(rva)
                    found[(key + (None, None, None))[:3]] = data[start:start + size]

        walk(root, ())
        return found
    except (struct.error, ValueError, IndexError) as exc:
        raise buildlib.BuildError("%s: cannot read its resources (%s)" % (exe, exc)) from None


def _version_strings(blob: bytes) -> Dict[str, str]:
    """The name/value strings inside a VS_VERSIONINFO resource."""
    def node(at: int) -> Tuple[str, int, int, int]:
        length, value_length, _kind = struct.unpack_from("<HHH", blob, at)
        end = at + 6
        while blob[end:end + 2] not in (b"\0\0", b""):
            end += 2
        key = blob[at + 6:end].decode("utf-16-le")
        return key, value_length, (end + 2 + 3) & ~3, at + max(length, 8)

    strings: Dict[str, str] = {}
    _key, value_length, at, end = node(0)
    at = (at + value_length + 3) & ~3
    while at + 6 <= min(end, len(blob)):
        key, _vlen, child, child_end = node(at)
        if key == "StringFileInfo":
            table = child
            while table + 6 <= child_end:
                _lang, _v, entry, table_end = node(table)
                while entry + 6 <= table_end:
                    name, _n, value_at, entry_end = node(entry)
                    strings[name] = blob[value_at:entry_end].decode("utf-16-le", "replace").split("\0")[0]
                    entry = (entry_end + 3) & ~3
                table = (table_end + 3) & ~3
        at = (child_end + 3) & ~3
    return strings


def _check_windows_resources(exe: Path, version: str) -> dict:
    """Fail unless the linked program carries exactly our manifest and version strings."""
    resources = pe_resources(exe)
    manifests = [(key, blob) for key, blob in resources.items() if key[0] == RT_MANIFEST]
    wanted = manifest_text(version).encode("utf-8")
    if len(manifests) != 1 or manifests[0][0][1] != 1 or manifests[0][1] != wanted:
        raise buildlib.BuildError(
            "%s must carry exactly one manifest, ours, as resource 1; found %s. A toolchain's "
            "default manifest may have been linked beside it or instead of it."
            % (exe.name, ", ".join("%r (%d bytes)" % (k, len(b)) for k, b in manifests) or "none"))
    version_blobs = [blob for key, blob in resources.items() if key[0] == RT_VERSION]
    if len(version_blobs) != 1:
        raise buildlib.BuildError("%s: expected 1 version resource, found %d" % (exe.name, len(version_blobs)))
    strings = _version_strings(version_blobs[0])
    for key, value in windows_identity(version).items():
        if strings.get(key) != value:
            raise buildlib.BuildError("%s: version resource has %s=%r, expected %r"
                                      % (exe.name, key, strings.get(key), value))
    return {
        "manifest_sha256": hashlib.sha256(manifests[0][1]).hexdigest(),
        "manifest_language": manifests[0][0][2],
        "version_strings": strings,
    }


def windows_resources(exe: Path, version: Optional[str] = None) -> dict:
    """Check a built ``scepter.exe`` and return what it carries (used by the tests)."""
    return _check_windows_resources(Path(exe), version or versions.SCEPTER_VERSION)
