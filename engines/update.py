#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Bring a new upstream release of an engine into the project. One command per engine.

    python engines/update.py tesseract --from PATH_OR_TARBALL --version 1.7.0 --commit SHA [--dry-run]
    python engines/update.py scepter   --from PATH_OR_TARBALL --version 1.2.0 --commit SHA [--dry-run]

``--from`` is a folder or an archive (.tar.gz, .tgz, .tar, .zip) holding the release's
source. Nothing is ever downloaded. The command

  1. checks that the source really says it is the version given;
  2. replaces ``engines/<engine>/upstream`` with it (the old tree is kept in
     ``build/update/<engine>/upstream-before``);
  3. rewrites the pins in ``tesseract_win/versions.py`` (version, tag, commit and, for
     TesserACT, the sha256 of the bash ``tesseract`` script);
  4. stages a copy and applies the scripted Windows changes, which stop with the line
     they could not find when upstream changed something the port depends on;
  5. runs the other guards: the fingerprint of the ``tesseract`` script against the one
     the Python port matches, a Linux build of the changed source and, for TesserACT,
     the C-library functions that program imports;
  6. ends with a plain list of what a person must now look at.

With ``--dry-run`` all of this happens in a temporary copy of the project: the report
is the same and nothing in the project changes.

The command can be repeated. After a failed guard, or after it was stopped half way, the
same command finishes the work, and it still compares the new source with the tree it
replaced (kept in ``build/update/<engine>/upstream-before``), so the list of things to
look at stays complete.

Exit status: 0 when every guard passed (there may still be a list of things to look at),
1 when a guard failed, 2 when the command could not start. See ``engines/UPDATING.md``.

Standard library only.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

VERSIONS_FILE = "tesseract_win/versions.py"
FINGERPRINT_TOOL = "tests/parity/script_fingerprint.py"
FINGERPRINT_RECORD = "tests/parity/golden/SCRIPT_FINGERPRINT.json"

#: Never copied from a source folder, wherever it lies: version control, caches, objects.
IGNORED = (".git", "__pycache__", "*.o", "*.pyc")
#: Not copied when it lies at the top of the source: what a build leaves there. Deeper down
#: the same names are upstream's own files (a folder tests/.../build, say) and are kept.
IGNORED_TOP_FOLDERS = ("build",)
IGNORED_TOP_FILES = ("tesseract-asm", "tesseract-model", "scepter",
                     "tesseract-asm.exe", "tesseract-model.exe", "scepter.exe")
#: In build/update/<engine>/: the tree that was replaced, and what it was replaced by.
BEFORE = "upstream-before"
MEMO = "replaced.json"

ENGINES = {
    "tesseract": {
        "title": "TesserACT",
        "upstream": "engines/tesseract/upstream",
        "recipe": "engines/win/tesseract/recipe.py",
        "must_have": ("src/main.cpp", "src/version.h", "tesseract"),
        "version_file": "src/version.h",
        "version_pattern": r'kVersion\s*=\s*"([^"]+)"',
        "source_globs": ("src/*.cpp", "src/*.h"),
    },
    "scepter": {
        "title": "scepter",
        "upstream": "engines/scepter/upstream",
        "recipe": "engines/win/scepter/recipe.py",
        "must_have": ("src/main.cpp", "src/cli.h", "src/cli.cpp"),
        "version_file": "src/cli.h",
        "version_pattern": r'\bkScepterVersion\s*=\s*"([^"]+)"',
        "source_globs": ("src/*.cpp", "src/*.h"),
    },
}

#: Which part of the Python port follows which section of the bash ``tesseract`` script.
SECTION_ADVICE = {
    "head": "the top of the script (version, help text, defaults): check tesseract_win/settings_map.py "
            "and the texts that quote the help",
    "arguments": "how the command reads its options: check tesseract_win/settings_map.py (what the "
                 "pages offer) and the genome-size rules in tesseract_win/readcheck.py",
    "preflight": "the read check before assembly: update tesseract_win/readcheck.py",
    "assembly": "how the engine is started (the settings profile, the second attempt for thin data): "
                "update tesseract_win/settings_map.py and tesseract_win/postflight.py",
    "postflight": "the checks after assembly, SUMMARY.txt and assembly_stats.tsv: update "
                  "tesseract_win/postflight.py",
}
RECAPTURE = ("re-capture the goldens and record the new fingerprint as tests/parity/README.md says "
             "under \"A new upstream release\", until `python -m pytest -m parity` passes")


class Stop(Exception):
    """The command cannot go on; nothing more is changed."""


def say(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------------------
# The source that was given
# --------------------------------------------------------------------------------------

def _safe_members(names: Sequence[str]) -> None:
    for name in names:
        parts = Path(name.replace("\\", "/")).parts
        if name.startswith(("/", "\\")) or ".." in parts or (len(name) > 1 and name[1] == ":"):
            raise Stop("the archive holds a path that leaves its own folder (%s): it was not unpacked" % name)


def unpack(archive: Path, into: Path) -> Path:
    """Unpack a release archive into ``into``; return the folder that holds the source."""
    into.mkdir(parents=True)
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as handle:
                _safe_members(handle.namelist())
                handle.extractall(into)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as handle:
                _safe_members(handle.getnames())
                if hasattr(tarfile, "data_filter"):
                    handle.extractall(into, filter="data")
                else:                                           # older Python: refuse links, then unpack
                    if any(m.issym() or m.islnk() or m.isdev() for m in handle.getmembers()):
                        raise Stop("the archive holds links or device files: unpack it yourself and give the folder")
                    handle.extractall(into)
        else:
            raise Stop("%s is neither a folder nor an archive this command can read (.tar.gz, .tgz, .tar, .zip)"
                       % archive)
    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        raise Stop("cannot unpack %s: %s" % (archive, exc))
    return into


def find_root(folder: Path, must_have: Sequence[str]) -> Path:
    """The folder that holds the source: ``folder`` itself or the single folder inside it."""
    candidates = [folder] + sorted(p for p in folder.iterdir() if p.is_dir())
    for candidate in candidates[:12]:
        if all((candidate / rel).is_file() for rel in must_have):
            return candidate
    raise Stop("%s does not hold the source of this engine: %s not found" % (folder, ", ".join(must_have)))


def stated_version(tree: Path, engine: dict) -> str:
    path = tree / engine["version_file"]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise Stop("cannot read %s: %s" % (path, exc.strerror or exc))
    match = re.search(engine["version_pattern"], text)
    if not match:
        raise Stop("%s no longer states the version where it used to (%s): upstream moved it. Correct "
                   "the pattern in engines/update.py and in the engine's recipe." % (engine["title"],
                                                                                    engine["version_file"]))
    return match.group(1)


# --------------------------------------------------------------------------------------
# Comparing two trees
# --------------------------------------------------------------------------------------

def _ignored(name: str, at_top: bool = False, is_folder: bool = False) -> bool:
    if any(Path(name).match(pattern) for pattern in IGNORED):
        return True
    return at_top and name in (IGNORED_TOP_FOLDERS if is_folder else IGNORED_TOP_FILES)


def copy_ignore(source: Path):
    """The ``ignore`` of ``shutil.copytree`` that leaves out what :func:`tree_files` leaves out."""
    top = os.path.normpath(str(source))

    def ignore(folder: str, names: List[str]) -> List[str]:
        at_top = os.path.normpath(str(folder)) == top
        return [n for n in names if _ignored(n, at_top, os.path.isdir(os.path.join(folder, n)))]
    return ignore


def tree_files(root: Path) -> Dict[str, str]:
    """``{relative path: sha256}`` of a source tree, without what is never copied."""
    found: Dict[str, str] = {}
    if not root.is_dir():
        return found
    for folder, dirs, files in os.walk(root):
        at_top = Path(folder) == root
        dirs[:] = sorted(d for d in dirs if not _ignored(d, at_top, True))
        for name in sorted(files):
            if _ignored(name, at_top):
                continue
            path = Path(folder) / name
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(block)
            found[path.relative_to(root).as_posix()] = digest.hexdigest()
    return found


def tree_digest(files: Dict[str, str]) -> str:
    """One checksum for what :func:`tree_files` found."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(("%s\0%s\n" % (name, files[name])).encode("utf-8"))
    return digest.hexdigest()


def read_memo(work: Path) -> dict:
    """What an earlier run noted when it replaced the upstream tree ({} when there is no note)."""
    try:
        with open(work / MEMO, encoding="utf-8") as handle:
            memo = json.load(handle)
    except (OSError, ValueError):
        return {}
    return memo if isinstance(memo, dict) else {}


def tree_difference(old: Dict[str, str], new: Dict[str, str]) -> Tuple[List[str], List[str], List[str]]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(name for name in set(old) & set(new) if old[name] != new[name])
    return added, removed, changed


def _some(names: Sequence[str], limit: int = 8) -> str:
    shown = ", ".join(names[:limit])
    return shown + (" and %d more" % (len(names) - limit) if len(names) > limit else "")


# --------------------------------------------------------------------------------------
# The pins
# --------------------------------------------------------------------------------------

def read_pins(root: Path) -> Dict[str, str]:
    text = (root / VERSIONS_FILE).read_text(encoding="utf-8")
    return dict(re.findall(r'^([A-Z][A-Z0-9_]*)\s*=\s*"([^"]*)"', text, re.M))


def require_pins(root: Path, names: Sequence[str]) -> None:
    """Stop, before anything is changed, when versions.py does not set each name exactly once."""
    try:
        text = (root / VERSIONS_FILE).read_text(encoding="utf-8")
    except OSError as exc:
        raise Stop("cannot read %s: %s. Nothing was changed." % (VERSIONS_FILE, exc.strerror or exc))
    for name in names:
        found = len(re.findall(r'^%s\s*=\s*"[^"]*"' % re.escape(name), text, re.M))
        if found != 1:
            raise Stop("%s must set %s exactly once (found %d times): this command could not rewrite it. "
                       "Nothing was changed." % (VERSIONS_FILE, name, found))


def rewrite_pins(root: Path, wanted: Dict[str, str]) -> List[str]:
    """Set the given names in versions.py; return one sentence per value that changed."""
    path = root / VERSIONS_FILE
    with open(path, encoding="utf-8", newline="") as handle:
        text = handle.read()
    changes: List[str] = []
    for name, value in wanted.items():
        pattern = re.compile(r'^(%s\s*=\s*)"([^"]*)"' % re.escape(name), re.M)
        found = pattern.findall(text)
        if len(found) != 1:
            raise Stop("%s must set %s exactly once (found %d times): it was not rewritten"
                       % (VERSIONS_FILE, name, len(found)))
        if found[0][1] != value:
            changes.append("%s: %s -> %s" % (name, found[0][1] or "(empty)", value))
            text = pattern.sub(lambda m: '%s"%s"' % (m.group(1), value), text)
    if changes:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return changes


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------------------
# What changed in the source, for a person
# --------------------------------------------------------------------------------------

_INCLUDE = re.compile(r'^\s*#\s*include\s*<([^>]+)>', re.M)
_UNIX_PATHS = ('"/proc/', '"/dev/', '"/tmp/')
_SCEPTER_OPTION = re.compile(r"\{\s*'(?:\\0|[^'\\])'\s*,\s*\"([^\"]+)\"\s*,\s*Kind::(\w+)")
_ENV_NAME = re.compile(r'"((?:TESSERACT|SCEPTER)_[A-Z0-9_]+)"')
_FORK = re.compile(r"\b(fork|waitpid|execv|create_symlink)\s*\(|env_reject\.h")


def _read_sources(tree: Path, globs: Sequence[str]) -> Dict[str, str]:
    texts: Dict[str, str] = {}
    for pattern in globs:
        for path in sorted(tree.glob(pattern)):
            texts[path.relative_to(tree).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
    return texts


def source_notes(name: str, engine: dict, old: Path, new: Path) -> List[str]:
    """Things in the new source a person should look at, found by comparing with the old one."""
    notes: List[str] = []
    before, after = _read_sources(old, engine["source_globs"]), _read_sources(new, engine["source_globs"])
    if not before or not after:
        return notes
    old_headers = {h for text in before.values() for h in _INCLUDE.findall(text)}
    new_headers = sorted({h for text in after.values() for h in _INCLUDE.findall(text)} - old_headers)
    if new_headers:
        notes.append("the source includes system headers it did not include before (%s): check that each "
                     "exists for Windows (MinGW-w64) or is covered by the compatibility layer in "
                     "engines/win/%s/" % (", ".join("<%s>" % h for h in new_headers), name))
    for literal in _UNIX_PATHS:
        grown = sorted(rel for rel, text in after.items() if text.count(literal) > before.get(rel, "").count(literal))
        if grown:
            notes.append("new uses of a Linux-only path (%s...\") in %s: Windows has no such path; the port "
                         "needs a rule or a replacement for each" % (literal, _some(grown)))
    old_env = {e for text in before.values() for e in _ENV_NAME.findall(text)}
    new_env = {e for text in after.values() for e in _ENV_NAME.findall(text)}
    if new_env - old_env or old_env - new_env:
        parts = []
        if new_env - old_env:
            parts.append("new: " + _some(sorted(new_env - old_env)))
        if old_env - new_env:
            parts.append("gone: " + _some(sorted(old_env - new_env)))
        notes.append("the environment variables the engine reads changed (%s): the program starts the "
                     "engine with a fixed set (tesseract_win/settings_map.py); check that it is still right"
                     % "; ".join(parts))
    if name == "tesseract":
        old_tests = {p.stem for p in (old / "tests").glob("test_*.cpp")}
        new_tests = {p.stem for p in (new / "tests").glob("test_*.cpp")}
        for test in sorted(new_tests - old_tests):
            text = (new / "tests" / (test + ".cpp")).read_text(encoding="utf-8", errors="replace")
            if _FORK.search(text):
                notes.append("the new C++ test %s uses fork(), waitpid(), execv() or a symbolic link, which "
                             "Windows does not have: add it to engines/win/tesseract/tests-skip.txt with the "
                             "reason" % test)
            else:
                notes.append("there is a new C++ test, %s: it will be built and run on Windows too" % test)
        golden_old, golden_new = old / "tests" / "golden_130.md5", new / "tests" / "golden_130.md5"
        if golden_old.is_file() and golden_new.is_file() and golden_old.read_bytes() != golden_new.read_bytes():
            notes.append("upstream's own recorded checksums changed (tests/golden_130.md5): this release "
                         "assembles differently from the last one, so results will differ from those of the "
                         "previous TesserACT-Win; say so in the release notes")
    if name == "scepter":
        old_options = dict(_SCEPTER_OPTION.findall(before.get("src/cli.cpp", "")))
        new_options = dict(_SCEPTER_OPTION.findall(after.get("src/cli.cpp", "")))
        if old_options and new_options and old_options != new_options:
            parts = []
            added, removed = sorted(set(new_options) - set(old_options)), sorted(set(old_options) - set(new_options))
            retyped = sorted(o for o in set(old_options) & set(new_options) if old_options[o] != new_options[o])
            if added:
                parts.append("new: " + _some(["--" + o for o in added]))
            if removed:
                parts.append("gone: " + _some(["--" + o for o in removed]))
            if retyped:
                parts.append("now take or no longer take a value: " + _some(["--" + o for o in retyped]))
            notes.append("scepter's options changed (%s): check the command the program builds "
                         "(tesseract_win/settings_map.py, scepter_command) and run tests/test_settings_map.py, "
                         "which compares it with scepter's own 'assembly' preset" % "; ".join(parts))
    for licence in ("LICENSE", "NOTICE"):
        if (old / licence).is_file() and (new / licence).is_file() and \
                (old / licence).read_bytes() != (new / licence).read_bytes():
            notes.append("the %s file of %s changed: update the NOTICE file of this project and "
                         "packaging/ to match" % (licence, engine["title"]))
    return notes


# --------------------------------------------------------------------------------------
# The bash tesseract script
# --------------------------------------------------------------------------------------

_RULER = re.compile(r"^# -{20,} (\w+)\s*$")


def script_sections(text: str) -> Dict[str, List[str]]:
    """The script cut at its ``# ---- name`` rulers, as tests/parity/script_fingerprint.py cuts it."""
    sections: Dict[str, List[str]] = {}
    name = "head"
    for line in text.split("\n"):
        match = _RULER.match(line)
        if match:
            name = match.group(1)
        sections.setdefault(name, []).append(line)
    return sections


def _is_code(line: str) -> bool:
    return bool(line.strip()) and not line.lstrip().startswith("#")


def script_report(root: Path, new_script: Path, old_script: Optional[Path]) -> Tuple[str, str, List[str]]:
    """Compare the new script with the one the Python port matches.

    Returns (verdict, sentence, things to look at): verdict ``ok`` when it is the same
    script, ``look`` when it changed, ``failed`` when the comparison could not be made.
    """
    tool, record = root / FINGERPRINT_TOOL, root / FINGERPRINT_RECORD
    if not tool.is_file() or not record.is_file():
        return ("failed", "the fingerprint of the tesseract script could not be checked: %s or %s is missing"
                % (FINGERPRINT_TOOL, FINGERPRINT_RECORD), [])
    spec = importlib.util.spec_from_file_location("update_script_fingerprint", tool)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    now = json.loads(json.dumps(module.fingerprint(str(new_script))))
    with open(record, encoding="utf-8") as handle:
        recorded = json.load(handle)
    if now == recorded:
        return "ok", "the tesseract script is the one the Python port matches (version %s)" % now["version"], []

    todo: List[str] = []
    names = sorted(set(recorded["sections"]) | set(now["sections"]),
                   key=lambda n: (list(SECTION_ADVICE) + [n]).index(n))
    logic = [n for n in names if recorded["sections_code_only"].get(n) != now["sections_code_only"].get(n)]
    comments = [n for n in names if recorded["sections"].get(n) != now["sections"].get(n) and n not in logic]

    # The lines themselves, when the script the port matches is at hand to compare with.
    changed_lines: Dict[str, List[str]] = {}
    if old_script is not None and old_script.is_file() and sha256_file(old_script) == recorded["sha256"]:
        before = script_sections(old_script.read_text(encoding="utf-8", errors="surrogateescape"))
        after = script_sections(new_script.read_text(encoding="utf-8", errors="surrogateescape"))
        for name in names:
            diff = [line for line in difflib.unified_diff(before.get(name, []), after.get(name, []), n=0, lineterm="")
                    if line[:1] in "+-" and not line.startswith(("+++", "---"))]
            if diff:
                changed_lines[name] = diff
    def lines_of(name: str) -> str:
        lines = changed_lines.get(name, [])
        if not lines:
            return ""
        return ("\n      The lines that changed (- before, + now):\n%s%s"
                % ("\n".join("        " + line for line in lines[:40]),
                   "\n        ... and %d more" % (len(lines) - 40) if len(lines) > 40 else ""))

    to_port = False
    for name in logic:
        lines = changed_lines.get(name, [])
        if lines and all(not _is_code(line[1:]) or re.match(r"VERSION=", line[1:]) for line in lines):
            todo.append("the tesseract script changed in its %s section, but only in its version line: nothing "
                        "there for the Python port to follow.%s" % (name, lines_of(name)))
            continue
        to_port = True
        advice = SECTION_ADVICE.get(name, "a section this command does not know: read it, and decide which "
                                          "part of tesseract_win/ follows it")
        todo.append("the tesseract script changed in its %s section: %s.%s" % (name, advice, lines_of(name)))
    for name in comments:
        todo.append("the tesseract script changed in its %s section, but only in comments or blank lines: "
                    "nothing there for the Python port to follow.%s" % (name, lines_of(name)))
    literals = sorted(k for k in set(recorded["literals"]) | set(now["literals"])
                      if recorded["literals"].get(k) != now["literals"].get(k))
    for key in literals:
        to_port = True
        todo.append("a value the Python port copies from the script changed (%s):\n        before: %s\n        now:    %s"
                    % (key, json.dumps(recorded["literals"].get(key)), json.dumps(now["literals"].get(key))))
    todo.append("%s the parity tests fail until the evidence is that of the new script: %s"
                % ("when the port follows the script again," if to_port else "the port needs no change, but", RECAPTURE))
    where = ", ".join(["%s (logic)" % n for n in logic] + ["%s (comments only)" % n for n in comments]) or "its literals"
    return ("look", "the tesseract script is not the one the Python port matches: version %s -> %s, changed in %s"
            % (recorded.get("version"), now.get("version"), where), todo)


# --------------------------------------------------------------------------------------
# Staging, transformations, build
# --------------------------------------------------------------------------------------

def load_recipe(root: Path, name: str):
    for entry in (str(root / "engines"), str(root)):
        if entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)
    for module in [m for m in sys.modules if m == "buildlib" or m.startswith("tesseract_win")]:
        del sys.modules[module]                     # the pins were just rewritten: read them again
    importlib.invalidate_caches()
    path = root / ENGINES[name]["recipe"]
    spec = importlib.util.spec_from_file_location("update_recipe_" + name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def describe_stage(name: str, counts: dict) -> str:
    if name == "tesseract":
        llp = counts.get("llp64", {})
        rules = counts.get("rules", {})
        guards = counts.get("guards", {})
        return ("%d files rewritten so that every 'long' is 64 bits on Windows (%d types, %d functions, %d "
                "numbers, %d formats); %d exact-text rule(s) applied (%s); %d guard(s) found what they expect"
                % (llp.get("files", 0), llp.get("type", 0), llp.get("func", 0), llp.get("literal", 0),
                   llp.get("format", 0), len(rules), ", ".join(sorted(rules)) or "none", len(guards)))
    transformations = counts.get("transformations", {})
    parts = ["%s replaced %d of %d" % (ident, facts.get("replaced", 0), facts.get("expected", 0))
             for ident, facts in sorted(transformations.items())]
    return ("%s; the guard found no 32-bit 'long' left outside the %d reviewed line(s); %d source files, "
            "%d options that take a value" % ("; ".join(parts) or "no transformation", counts.get("reviewed_lines", 0),
                                              counts.get("files_scanned", 0) or counts.get("sources", 0),
                                              counts.get("value_options", 0)))


class Report:
    def __init__(self) -> None:
        self.done: List[str] = []
        self.checks: List[Tuple[str, str]] = []
        self.todo: List[str] = []

    def check(self, verdict: str, text: str) -> None:
        self.checks.append((verdict, text))
        say("  %-7s %s" % (verdict, text))

    @property
    def failed(self) -> bool:
        return any(verdict == "FAILED" for verdict, _ in self.checks)


def apply(root: Path, name: str, source: Path, version: str, commit: str, *, skip_build: bool, jobs: int) -> int:
    """Bring ``source`` in as the upstream tree of engine ``name`` inside the project at ``root``."""
    engine = ENGINES[name]
    title = engine["title"]
    upstream = root / engine["upstream"]
    work = root / "build" / "update" / name
    report = Report()

    stated = stated_version(source, engine)
    if stated != version:
        raise Stop("the source in %s says it is %s %s, but --version is %s. Give the version the source "
                   "states. Nothing was changed." % (source, title, stated, version))
    prefix = name.upper()
    wanted = {prefix + "_VERSION": version, prefix + "_COMMIT": commit}
    if name == "tesseract":
        wanted["TESSERACT_TAG"] = "v" + version
        wanted["TESSERACT_WRAPPER_SHA256"] = sha256_file(source / "tesseract")
    require_pins(root, list(wanted))
    pins = read_pins(root)
    same_pins = all(pins.get(key) == value for key, value in wanted.items())

    incoming = upstream.with_name("upstream.incoming")
    before = work / BEFORE
    if source == incoming or incoming in source.parents:
        raise Stop("%s is this command's own working copy, which it deletes first: give the folder or the "
                   "archive of the release. Nothing was changed." % source)
    new_files = tree_files(source)
    # Stopped earlier between moving the old tree aside and putting the new one in place:
    # the tree moved aside is the one being replaced.
    interrupted = not upstream.is_dir() and before.is_dir()
    old_files = tree_files(before if interrupted else upstream)
    added, removed, changed = tree_difference(old_files, new_files)
    same_tree = upstream.is_dir() and not (added or removed or changed)
    # The same command again, after it has already put this tree in place: the comparison
    # that tells a person what to look at is still the one with the tree it replaced.
    memo = read_memo(work)
    repeat = False
    if same_tree and before.is_dir() and memo.get("replaced_by") == tree_digest(new_files):
        added, removed, changed = tree_difference(tree_files(before), new_files)
        repeat = bool(added or removed or changed)
    compared = before if (repeat or not same_tree) else None      # the tree to compare the new one with

    version_before = (memo.get("version_before") if repeat else None) or pins.get(prefix + "_VERSION", "?")
    say("%s %s -> %s" % (title, version_before, version))
    say()
    say("What was done")
    old_script_copy: Optional[Path] = None
    if repeat:
        say("  the source is the tree already in %s: an earlier run of this command put it there. It is "
            "compared once more with the tree it replaced (%s): %d files added, %d removed, %d changed"
            % (engine["upstream"], before.relative_to(root).as_posix(), len(added), len(removed), len(changed)))
        old_script_copy = before / "tesseract" if name == "tesseract" else None
    elif same_tree:
        say("  the source is the tree already in %s: it was left as it is" % engine["upstream"])
        old_script_copy = upstream / "tesseract" if name == "tesseract" else None
    else:
        if interrupted:
            say("  %s is missing: an earlier run of this command was stopped while it replaced it. The tree "
                "it had moved aside (%s) is taken as the one being replaced"
                % (engine["upstream"], before.relative_to(root).as_posix()))
        # The new tree is copied completely before the old one is touched; the old one is
        # moved aside, never deleted, before the new one takes its place.
        try:
            if incoming.exists():
                shutil.rmtree(incoming)
            shutil.copytree(source, incoming, ignore=copy_ignore(source))
            work.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            shutil.rmtree(incoming, ignore_errors=True)
            raise Stop("cannot copy the new source to %s (%s). Nothing was changed." % (incoming, exc))
        try:
            with open(work / MEMO, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({"engine": name, "replaced_by": tree_digest(new_files),
                           "version_before": pins.get(prefix + "_VERSION", "")}, handle, indent=2, sort_keys=True)
                handle.write("\n")
            if upstream.exists():
                if before.exists():
                    shutil.rmtree(before)
                shutil.move(str(upstream), str(before))
            os.replace(incoming, upstream)
        except OSError as exc:
            raise Stop("could not put the new source in place (%s). The tree as it was is in %s or in %s, the "
                       "new one in %s. Run the same command again." % (exc, engine["upstream"],
                                                                      before.relative_to(root).as_posix(), incoming))
        say("  %s replaced: %d files added, %d removed, %d changed (the tree as it was: %s)"
            % (engine["upstream"], len(added), len(removed), len(changed),
               before.relative_to(root).as_posix()))
        old_script_copy = before / "tesseract" if name == "tesseract" else None
    if compared is not None:
        for label, names in (("added", added), ("removed", removed), ("changed", changed)):
            if names:
                say("      %-8s %s" % (label, _some(names, 10)))
    if same_pins:
        say("  the pins in %s already say %s %s, commit %s" % (VERSIONS_FILE, title, version, commit[:12]))
    else:
        for change in rewrite_pins(root, wanted):
            say("  %s  %s" % (VERSIONS_FILE, change))
    nothing_new = same_tree and same_pins and not repeat
    if nothing_new:
        say("  nothing changed: this is the release the project already uses. The guards are run all the same.")

    say()
    say("Guards")
    report.check("ok", "the source states the version given (%s)" % stated)

    # ---- staging and the scripted changes -------------------------------------------
    recipe = None
    staged = work / "stage"
    staged_ok = False
    try:
        recipe = load_recipe(root, name)
        buildlib = sys.modules["buildlib"]
    except Exception as exc:                                    # a broken recipe is a failed guard
        report.check("FAILED", "the build recipe %s cannot be loaded: %s" % (engine["recipe"], exc))
        buildlib = None
    if recipe is not None:
        try:
            counts = recipe.stage(upstream, staged, "linux")
            staged_ok = True
            report.check("ok", "staging and the scripted Windows changes: " + describe_stage(name, counts))
            if name == "tesseract" and not counts.get("llp64_pinned", True):
                report.todo.append(
                    "record what the 64-bit rewrite found in this release, so that the build stops if it ever "
                    "finds something else: add  \"%s\": %s  to LLP64_EXPECTED in engines/win/tesseract/recipe.py "
                    "(the release before: %s). A large change in these counts means new code that uses 'long': "
                    "read docs/superpowers/audits/engine-port.md, section 2"
                    % (version, json.dumps(counts["llp64"], sort_keys=True),
                       json.dumps(getattr(recipe, "LLP64_EXPECTED", {}).get(version_before),
                                  sort_keys=True)))
        except buildlib.BuildError as exc:
            report.check("FAILED", "staging and the scripted Windows changes stopped:\n%s"
                         % "\n".join("            " + line for line in str(exc).splitlines()))
            report.todo.append(
                "a line the Windows port depends on is no longer what it was (the message above names it). "
                "Read the new code at that place, decide whether Windows still needs the change, then correct "
                "the rule, the guard or its expected count in engines/win/%s/ and run this command again"
                % name)
        if name == "tesseract" and staged_ok:
            try:
                recipe.test_programs(staged, "windows")
            except buildlib.BuildError as exc:
                report.check("FAILED", "the list of C++ tests left out on Windows no longer fits: %s" % exc)
                report.todo.append("remove from engines/win/tesseract/tests-skip.txt the test programs that "
                                   "upstream no longer has")

    # ---- the bash script the Python port reproduces ---------------------------------
    if name == "tesseract":
        verdict, sentence, todo = script_report(root, upstream / "tesseract", old_script_copy)
        report.check({"ok": "ok", "look": "LOOK", "failed": "FAILED"}[verdict], sentence)
        report.todo.extend(todo)

    # ---- a Linux build of the changed source, and what it imports --------------------
    can_build = sys.platform.startswith("linux") and buildlib is not None
    if skip_build:
        report.check("skipped", "the Linux build of the changed source%s (--skip-build)"
                     % (" and its list of C-library imports" if name == "tesseract" else ""))
    elif not staged_ok:
        report.check("skipped", "the Linux build: the source could not be staged")
    elif not can_build:
        report.check("skipped", "the Linux build: this is not a Linux machine. CI does it after the push")
    else:
        try:
            tc = buildlib.detect_toolchain("linux")
            if shutil.which(tc.cxx) is None:
                report.check("skipped", "the Linux build: no compiler (%s) on this machine. CI does it after "
                                        "the push" % tc.cxx)
            else:
                facts = recipe.build(tc=tc, upstream=upstream, stage=staged, out_dir=work / "bin", jobs=jobs)
                report.check("ok", "the changed source builds on Linux with %s" % tc.version())
                if name == "tesseract":
                    new_imports = facts.get("new_libc_imports")
                    if new_imports is None:
                        report.check("skipped", "the list of C-library imports: 'nm' could not read the program")
                    elif new_imports:
                        report.check("LOOK", "the Linux program imports C-library functions it did not import "
                                             "before: %s" % ", ".join(new_imports))
                        report.todo.append(
                            "new C-library imports (%s). For each: is it there on Windows (MinGW-w64), or does "
                            "engines/win/tesseract/ts_compat.h have to supply it? If it is a math function (exp, "
                            "log, pow, sin ...), can its result reach contigs.fasta (audit engine-port.md, section "
                            "6.2)? Then add the name to engines/win/tesseract/libc_imports.txt"
                            % ", ".join(new_imports))
                    else:
                        report.check("ok", "the Linux program imports no C-library function that is not already "
                                           "in engines/win/tesseract/libc_imports.txt")
        except buildlib.BuildError as exc:
            report.check("FAILED", "the changed source does not build on Linux:\n%s"
                         % "\n".join("            " + line for line in str(exc).splitlines()[-25:]))
            report.todo.append("the Linux build of the changed source failed (above): a scripted change no "
                               "longer fits the new code, or the new code needs something the compatibility "
                               "header hides. Correct engines/win/%s/ and run this command again" % name)

    # ---- what else changed, found by comparing the two trees -------------------------
    if compared is not None:
        report.todo.extend(source_notes(name, engine, compared, upstream))
        if name == "tesseract" and "models/om2detect.sketch" in changed:
            report.todo.append("the organism detection file (models/om2detect.sketch) changed: it is shipped "
                               "beside the engines; nothing to do, but detected organisms may differ")

    say()
    say("What a person must look at now")
    if report.todo:
        for number, item in enumerate(report.todo, 1):
            say("  %d. %s" % (number, item))
    elif nothing_new:
        say("  Nothing. This is the release the project already uses and every guard passed.")
    else:
        say("  Nothing that this command can see. Read the release notes of %s %s all the same." % (title, version))
    say()
    if report.failed:
        say("RESULT: a guard failed. The new source and the pins are in place; the Windows changes do not "
            "fit them yet. Work through the list above, then run the same command again.")
        return 1
    say("RESULT: every guard passed%s." % ("" if not report.todo else "; the list above is still to be done"))
    if not nothing_new:
        say("Next: python -m pytest -q, then commit and push. CI builds both engines on Linux and on Windows, "
            "runs their own test suites and requires identical output on both (engines/UPDATING.md).")
    return 0


# --------------------------------------------------------------------------------------
# Dry run: the same, in a copy
# --------------------------------------------------------------------------------------

def dry_run(args, source: Path) -> int:
    """Copy what the update touches or reads to a temporary folder and run the update there."""
    engine = ENGINES[args.engine]
    with tempfile.TemporaryDirectory(prefix="engine-update-") as tmp:
        shadow = Path(tmp) / "project"
        shutil.copytree(REPO / "engines", shadow / "engines",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "upstream.incoming"))
        shutil.copytree(REPO / "tesseract_win", shadow / "tesseract_win",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "assets"))
        for rel in (FINGERPRINT_TOOL, FINGERPRINT_RECORD):
            if (REPO / rel).is_file():
                (shadow / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO / rel, shadow / rel)
        before = (tree_files(REPO / engine["upstream"]), sha256_file(REPO / VERSIONS_FILE))
        say("DRY RUN: everything below happens in a temporary copy of the project. Nothing here is changed.")
        say()
        command = [sys.executable, str(shadow / "engines" / "update.py"), args.engine, "--from", str(source),
                   "--version", args.version, "--commit", args.commit, "--jobs", str(args.jobs), "--in-copy"]
        if args.skip_build:
            command.append("--skip-build")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        status = subprocess.run(command, env=env).returncode
        after = (tree_files(REPO / engine["upstream"]), sha256_file(REPO / VERSIONS_FILE))
        say()
        if before != after:
            say("DRY RUN: WARNING, %s or %s changed while this ran. This command did not write to them; "
                "someone else did." % (engine["upstream"], VERSIONS_FILE))
        else:
            say("DRY RUN finished: %s and %s are exactly as they were. Run the command without --dry-run to "
                "make the change." % (engine["upstream"], VERSIONS_FILE))
        return status


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("engine", choices=sorted(ENGINES))
    parser.add_argument("--from", dest="source", required=True, metavar="PATH_OR_TARBALL",
                        help="folder or archive with the new source (nothing is downloaded)")
    parser.add_argument("--version", required=True, help="the version of the release, for example 1.7.0")
    parser.add_argument("--commit", required=True, help="the full commit of the release (40 characters)")
    parser.add_argument("--dry-run", action="store_true", help="work on a temporary copy; change nothing")
    parser.add_argument("--skip-build", action="store_true", help="do not build the changed source on Linux")
    parser.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--in-copy", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    args.commit = args.commit.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        sys.stderr.write("update: --commit must be the full commit, 40 characters of 0-9 and a-f (git rev-parse "
                         "TAG^{commit}). Nothing was changed.\n")
        return 2
    if not re.fullmatch(r"\d+(\.\d+){1,3}", args.version):
        sys.stderr.write("update: --version must look like 1.7.0. Nothing was changed.\n")
        return 2
    given = Path(args.source).expanduser()
    try:
        if not given.exists():
            raise Stop("%s does not exist. Nothing was changed." % given)
        with tempfile.TemporaryDirectory(prefix="engine-source-") as tmp:
            folder = given.resolve() if given.is_dir() else unpack(given.resolve(), Path(tmp) / "unpacked")
            source = find_root(folder, ENGINES[args.engine]["must_have"])
            if args.dry_run and not args.in_copy:
                return dry_run(args, source)
            return apply(REPO, args.engine, source, args.version, args.commit,
                         skip_build=args.skip_build, jobs=args.jobs)
    except Stop as exc:
        sys.stderr.write("update: %s\n" % exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
