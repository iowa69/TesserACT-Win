#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Scripted, re-runnable source transformations and guards for the scepter port.

    transform.py TREE            rewrite a staged copy of scepter in place (idempotent)
    transform.py --check TREE    change nothing; report what would happen

``TREE`` is a copy of the scepter source (the folder that holds ``src/``). Only a copy
staged by ``recipe.py`` is ever rewritten (it carries ``port/README.txt``); the pristine
``engines/scepter/upstream`` can be examined with ``--check`` and is refused otherwise.

Nothing here is a patch. Every change is a rule that names the file, the exact text and
the number of places it must be found in; anything else stops with a message that says
what was found, so a new upstream release cannot be half-transformed without anyone
noticing. Running the rules on a tree they were already applied to changes nothing.

Transformations
    T1  src/fastqio.cpp, the seek of the plain-FASTQ sampler.
        ``long`` is 32 bits on Windows, so the original cast sends offsets from 2 GB
        to the wrong place. ``scepter_fseek64`` comes from the force-included
        ``scepter_port.h`` and is ``fseeko`` on Linux: the same call as before.

Guards (run on the tree as it is after the transformations)
    G1  Windows ``long`` is 32 bits. Every plain ``long`` (not ``long long``), every
        function that takes or returns one (fseek, ftell, strtol, lround ...), every
        ``off_t``, zlib's own ``long`` (``z_off_t``, ``uLong``, gzseek/gztell/gzoffset,
        ``total_in``/``total_out``), ``LONG_MAX`` and its kin, every integer literal
        with an L or UL suffix and every ``%ld``-style format must be one of the
        reviewed lines below. A new one stops the build and shows the line, so a
        person looks at it before it reaches a Windows build.
    G2  Every system header the source includes must also be included by
        ``scepter_port.h``, before that header redirects names such as ``stat``.
    G3  The options whose value is free text are the ones the Windows ``main`` leaves
        alone when it turns ``\\`` into ``/`` in arguments, and the short options that
        take a value are the ones it knows (they end a bundle such as ``-AR title``).
    G4  The version in ``src/cli.h`` is found exactly once (it is returned).
    G5  Nothing escapes G1 to G3: ``src/`` holds only the ``*.cpp`` and ``*.h`` files the
        guards read (no sub-folder, no other kind of file), and every ``#include "..."``
        names one of them.
    G6  Upstream's ``Makefile`` still builds the way ``recipe.py`` does (same sources,
        language level, libraries): the lines in ``UPSTREAM_BUILD``, each exactly once,
        and no other assignment to the variables they set.

Standard library only.
"""

from __future__ import annotations

import posixpath
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

__all__ = ["TransformError", "Transform", "Reviewed", "TRANSFORMS", "REVIEWED", "TEXT_OPTIONS",
           "KNOWN_METAVARS", "UPSTREAM_BUILD", "STAGE_MARKER", "apply", "lex", "upstream_version",
           "system_headers"]


class TransformError(Exception):
    """The tree is not what the rules expect. The message lists every problem found."""


class Transform(NamedTuple):
    ident: str
    path: str          # relative to the tree, forward slashes
    old: str           # exact text in the pristine source
    new: str           # exact text after the transformation
    count: int         # how many times it must be found
    why: str


class Reviewed(NamedTuple):
    path: str
    line: str          # the whole source line, stripped of surrounding blanks
    count: int         # how many lines of the file read exactly like this
    why: str


TRANSFORMS: Tuple[Transform, ...] = (
    Transform(
        ident="T1-fseek64",
        path="src/fastqio.cpp",
        old="std::fseek(fh_, static_cast<long>(offset), SEEK_SET)",
        new="scepter_fseek64(fh_, offset, SEEK_SET)",
        count=1,
        why="a 32-bit long cannot hold a byte offset in a plain FASTQ file over 2 GB",
    ),
)

#: Lines that trip guard G1 and were read by a person: safe when ``long`` is 32 bits.
REVIEWED: Tuple[Reviewed, ...] = (
    Reviewed("src/stats.cpp", "long v = std::lround(p);", 1,
             "p is a percentage, 0 to 100"),
    Reviewed("src/stats.cpp",
             "std::lround(static_cast<double>(qsum) / static_cast<double>(len))))]++;", 1,
             "a mean base quality, at most 63"),
    Reviewed("src/stats.cpp",
             "return static_cast<int>(std::lround(vlo + frac * (vhi - vlo)));", 1,
             "a quality quantile, at most 63"),
    Reviewed("src/stats.cpp", "p10 = static_cast<int>(std::lround(quantile(0.10)));", 1,
             "an insert size, at most 1000"),
    Reviewed("src/stats.cpp", "p90 = static_cast<int>(std::lround(quantile(0.90)));", 1,
             "an insert size, at most 1000"),
    Reviewed("src/fastqio.cpp", "const z_off_t off = gzoffset(gz_);", 1,
             "feeds only the progress percentage, and a negative value is ignored; above 2 GB "
             "of compressed input the percentage is wrong on Windows, nothing else (audit A3)"),
)

#: Options whose value is free text (metavar STR in the table of src/cli.cpp), as
#: (short letter or "", long name). scepter_port.cpp keeps the same list.
TEXT_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("R", "report_title"),
    ("", "umi_prefix"),
    ("", "umi_delim"),
)
#: Every metavar the option table uses in the reviewed version. A new kind of value has
#: to be looked at: may it hold a backslash that is not a path separator?
KNOWN_METAVARS = frozenset({"FILE", "DIR", "SEQ", "INT", "STR", "WHERE", "N", "F", "NAME"})

#: The file the recipe puts into a staged copy. Without it a tree is taken to be someone's
#: original source, and nothing is written to it.
STAGE_MARKER = "port/README.txt"
SOURCE_DIR = "src"
SOURCE_GLOBS = ("src/*.cpp", "src/*.h")
VERSION_FILE = "src/cli.h"
OPTION_TABLE_FILE = "src/cli.cpp"
MAKEFILE = "Makefile"
#: The lines of upstream's Makefile that say what is compiled and how. recipe.py builds
#: the same way (every src/*.cpp, C++17, -pthread, zlib) with buildlib's flags; when one
#: of these lines changes, a person has to compare the two again. Runs of blanks do not count.
UPSTREAM_BUILD: Tuple[str, ...] = (
    "SRCDIR := src",
    "SOURCES := $(wildcard $(SRCDIR)/*.cpp)",
    "CXXSTD := -std=c++17",
    "WARN := -Wall -Wextra -Wno-unused-parameter",
    "OPT ?= -O3",
    "CXXFLAGS += $(CXXSTD) $(WARN) $(OPT) -pthread -MMD -MP",
    "LDFLAGS += -pthread",
    "LDLIBS += -lz",
)

# ---------------------------------------------------------------------------------------
# A small C++ lexer: enough to tell code from comments and from the inside of literals
# (including raw strings, which hold the CSS and JavaScript of the HTML report).
# ---------------------------------------------------------------------------------------

_RAW_PREFIX = re.compile(r'(?:u8|u|U|L)?R"([^ ()\\\t\v\f\n]{0,16})\(')
_STR_PREFIX = re.compile(r'(?:u8|u|U|L)?"')
_CHR_PREFIX = re.compile(r"(?:u8|u|U|L)?'")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"\.?[0-9](?:[eEpP][+-]|'[0-9A-Za-z_]|[0-9A-Za-z_.])*")


def lex(text: str) -> Tuple[List[str], List[Tuple[int, str]]]:
    """Split C++ source into code and string literals.

    Returns ``(code_lines, strings)``. ``code_lines`` is the source line by line with
    comments removed and the inside of string and character literals emptied (``""``,
    ``''``). ``strings`` is a list of ``(line number, content)`` for every string
    literal, with a multi-line raw string reported once per line it spans.
    """
    code: List[str] = []
    strings: List[Tuple[int, str]] = []
    line = 1
    i, n = 0, len(text)

    def emit(piece: str) -> None:
        nonlocal line
        code.append(piece)
        line += piece.count("\n")

    def emit_string(content: str, start_line: int) -> None:
        for offset, part in enumerate(content.split("\n")):
            strings.append((start_line + offset, part))

    while i < n:
        ch = text[i]
        if ch == "/" and text.startswith("//", i):
            k = i
            while True:
                k = text.find("\n", k)
                if k < 0:
                    k = n
                    break
                last = text[k - 2] if text[k - 1] == "\r" else text[k - 1]
                if last != "\\":
                    break
                emit("\n")                               # a backslash continues the comment
                k += 1
            i = k                                        # the line end itself is code
            continue
        if ch == "/" and text.startswith("/*", i):
            k = text.find("*/", i + 2)
            k = n if k < 0 else k + 2
            emit("\n" * text.count("\n", i, k) or " ")
            i = k
            continue
        m = _RAW_PREFIX.match(text, i)
        if m:
            closing = ")" + m.group(1) + '"'
            k = text.find(closing, m.end())
            if k < 0:
                raise TransformError("unterminated raw string starting on line %d" % line)
            content = text[m.end():k]
            emit_string(content, line)
            emit('""' + "\n" * content.count("\n"))
            i = k + len(closing)
            continue
        m = _STR_PREFIX.match(text, i) or _CHR_PREFIX.match(text, i)
        if m:
            quote = text[m.end() - 1]
            k = m.end()
            while k < n and text[k] != quote:
                if text[k] == "\n":
                    raise TransformError("unterminated literal on line %d" % line)
                k += 2 if text[k] == "\\" else 1
            if quote == '"':
                emit_string(text[m.end():k], line)
            emit(quote + quote)
            i = k + 1
            continue
        m = _IDENT.match(text, i)
        if m:
            emit(m.group(0))
            i = m.end()
            continue
        m = _NUMBER.match(text, i)
        if m:                                           # keeps 1'000'000 from opening a literal
            emit(m.group(0))
            i = m.end()
            continue
        emit(ch)
        i += 1
    return "".join(code).split("\n"), strings


# ---------------------------------------------------------------------------------------
# Guard G1: everything that depends on the width of `long`.
# ---------------------------------------------------------------------------------------

_LONG_LONG = re.compile(r"\blong\s+long\b")
_RULES_CODE = (
    ("a plain long", re.compile(r"\blong\b")),
    # The name alone, call or not: `&std::strtol` hands the same function to someone else.
    ("a call that takes or returns long",
     re.compile(r"\b(?:fseeko?|ftello?|lseek|strtou?l|wcstou?l|atol|labs|ldiv|stou?l|"
                r"lround[fl]?|lrint[fl]?)\b")),
    ("off_t (32 bits on Windows)", re.compile(r"\boff_t\b")),
    ("zlib's long (z_off_t and uLong are 32 bits on Windows)",
     re.compile(r"\b(?:z_off_t|uLongf?|gzseek|gztell|gzoffset|total_in|total_out)\b")),
    ("a limit of long", re.compile(r"\bU?LONG_(?:MAX|MIN)\b")),
    ("a literal with an L or UL suffix",
     re.compile(r"(?<![A-Za-z0-9_.])(?:0[xX][0-9a-fA-F']+|0[bB][01']+|[0-9][0-9'.]*)"
                r"(?:[uU][lL]|[lL][uU]?)(?![A-Za-z0-9_])")),
)
_RULE_FORMAT = ("a %ld-style format",
                re.compile(r"%[-+ #0']*(?:\d+|\*)?(?:\.(?:\d+|\*))?l(?!l)[diouxX]"))


def _long_sites(code_lines: Sequence[str], strings: Iterable[Tuple[int, str]]) -> Dict[int, List[str]]:
    """Line number -> the reasons guard G1 objects to that line."""
    found: Dict[int, List[str]] = {}
    for number, code in enumerate(code_lines, 1):
        code = _LONG_LONG.sub(" ", code)
        for reason, pattern in _RULES_CODE:
            if pattern.search(code):
                found.setdefault(number, []).append(reason)
    for number, content in strings:
        if _RULE_FORMAT[1].search(content):
            reasons = found.setdefault(number, [])
            if _RULE_FORMAT[0] not in reasons:
                reasons.append(_RULE_FORMAT[0])
    return found


# ---------------------------------------------------------------------------------------
# Guards G2 to G4.
# ---------------------------------------------------------------------------------------

_INCLUDE = re.compile(r"^\s*#\s*include\s*<([^>]+)>", re.M)
_INCLUDE_QUOTED = re.compile(r'^\s*#\s*include\s*""')        # as lex() leaves it: the name emptied
_MAKE_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::=|\+=|\?=|=)")
_OPTION = re.compile(r"\{\s*'(\\0|[^'\\])'\s*,\s*\"([^\"]+)\"\s*,\s*Kind::Value\s*,\s*\"([^\"]*)\"")
_VERSION = re.compile(r'\bkScepterVersion\s*=\s*"([^"]+)"')


def system_headers(text: str) -> List[str]:
    """Names inside ``#include <...>``, in order, for C++ source or for scepter_port.h."""
    code_lines, _ = lex(text)
    return _INCLUDE.findall("\n".join(code_lines))


def upstream_version(tree: Path) -> str:
    """The version string compiled into scepter (``kScepterVersion`` in src/cli.h)."""
    path = tree / VERSION_FILE
    if not path.is_file():
        raise TransformError("%s: not found, cannot read the scepter version" % VERSION_FILE)
    found = _VERSION.findall(_read(path))
    if len(found) != 1:
        raise TransformError("%s: expected exactly 1 kScepterVersion = \"...\", found %d"
                             % (VERSION_FILE, len(found)))
    return found[0]


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _sources(tree: Path) -> List[Path]:
    found: List[Path] = []
    for pattern in SOURCE_GLOBS:
        found.extend(tree.glob(pattern))
    return sorted(found)


_PORT_TEXT_OPTIONS = re.compile(r"kTextOptions\[\]\s*=\s*\{([^}]*)\}")
_PORT_TEXT_SHORT = re.compile(r"kTextShort\s*=\s*'(.)'")
_PORT_VALUE_SHORTS = re.compile(r'kValueShorts\[\]\s*=\s*"([^"]*)"')


def _port_value_shorts(port_source: Path) -> str:
    """The short options scepter_port.cpp takes to carry a value, sorted."""
    found = _PORT_VALUE_SHORTS.findall(_read(port_source))
    if len(found) != 1:
        raise TransformError("%s: kValueShorts must be defined once" % port_source.name)
    return "".join(sorted(found[0]))


def _makefile_problems(tree: Path) -> List[str]:
    """Guard G6: upstream's Makefile still builds what, and how, recipe.py builds."""
    path = tree / MAKEFILE
    if not path.is_file():
        return ["G6: %s is missing: how does upstream build now? Compare with recipe.py" % MAKEFILE]
    lines = [" ".join(line.split()) for line in _read(path).split("\n") if line[:1] not in ("\t", "#")]
    problems = []
    for wanted in UPSTREAM_BUILD:
        if lines.count(wanted) != 1:
            problems.append("G6: %s must hold `%s` exactly once, found %d: upstream changed how it "
                            "builds; compare with recipe.py, then update UPSTREAM_BUILD"
                            % (MAKEFILE, wanted, lines.count(wanted)))
    watched = {_MAKE_ASSIGNMENT.match(wanted).group(1) for wanted in UPSTREAM_BUILD} | {"CPPFLAGS", "LIBS"}
    for line in lines:
        found = _MAKE_ASSIGNMENT.match(line)
        if found and found.group(1) in watched and line not in UPSTREAM_BUILD:
            problems.append("G6: %s now also says `%s`: compare with recipe.py, then update "
                            "UPSTREAM_BUILD" % (MAKEFILE, line))
    return problems


def _port_text_options(port_source: Path) -> List[Tuple[str, str]]:
    """The free-text options scepter_port.cpp leaves alone, in the form of TEXT_OPTIONS."""
    text = _read(port_source)
    lists, shorts = _PORT_TEXT_OPTIONS.findall(text), _PORT_TEXT_SHORT.findall(text)
    if len(lists) != 1 or len(shorts) != 1:
        raise TransformError("%s: kTextOptions and kTextShort must each be defined once"
                             % port_source.name)
    names = re.findall(r'"([^"]+)"', lists[0])
    with_short = {name: short for short, name in TEXT_OPTIONS if short}
    return sorted((shorts[0] if with_short.get(name) == shorts[0] else "", name) for name in names)


def apply(tree: Path, *, port_header: Optional[Path] = None, port_source: Optional[Path] = None,
          check: bool = False) -> dict:
    """Run every transformation and guard on ``tree``; return the counts.

    ``port_header`` and ``port_source`` are the ``scepter_port.h`` and ``scepter_port.cpp``
    the tree will be compiled with (guards G2 and G3); a guard that needs a file that is
    not given checks what it can. With ``check`` nothing is written. Raises
    :class:`TransformError` listing every problem; in that case nothing was written.
    """
    tree = Path(tree)
    if not check and not (tree / STAGE_MARKER).is_file():
        raise TransformError("%s is not a staged copy (no %s): it will not be rewritten. Stage it "
                             "with recipe.py, or use --check" % (tree, STAGE_MARKER))
    problems: List[str] = []
    sources = _sources(tree)
    if not sources:
        raise TransformError("%s: no scepter source found (expected %s)" % (tree, ", ".join(SOURCE_GLOBS)))
    texts: Dict[str, str] = {p.relative_to(tree).as_posix(): _read(p) for p in sources}
    changed: Dict[str, str] = {}
    counts: Dict[str, dict] = {}

    # ---- transformations ----------------------------------------------------------
    for rule in TRANSFORMS:
        if rule.path not in texts:
            problems.append("%s: %s is missing" % (rule.ident, rule.path))
            continue
        text = texts[rule.path]
        before, after = text.count(rule.old), text.count(rule.new)
        if before == rule.count and after == 0:
            texts[rule.path] = changed[rule.path] = text.replace(rule.old, rule.new)
            replaced = before
        elif before == 0 and after == rule.count:
            replaced = 0                                   # already applied
        else:
            problems.append(
                "%s: %s must hold `%s` exactly %d time(s) (or, once transformed, `%s`); "
                "found %d and %d. Upstream changed this code: read it and update the rule (%s)"
                % (rule.ident, rule.path, rule.old, rule.count, rule.new, before, after, rule.why))
            continue
        counts[rule.ident] = {"file": rule.path, "expected": rule.count, "replaced": replaced,
                              "present": texts[rule.path].count(rule.new)}

    # ---- G1: the width of long ----------------------------------------------------
    reviewed_hits = {entry: 0 for entry in REVIEWED}
    lookup = {(entry.path, entry.line): entry for entry in REVIEWED}
    used_headers: Dict[str, str] = {}
    for rel, text in texts.items():
        try:
            code_lines, strings = lex(text)
        except TransformError as exc:
            problems.append("%s: %s" % (rel, exc))
            continue
        raw_lines = text.split("\n")
        for number, reasons in sorted(_long_sites(code_lines, strings).items()):
            stripped = raw_lines[number - 1].strip()
            entry = lookup.get((rel, stripped))
            if entry is not None:
                reviewed_hits[entry] += 1
            else:
                problems.append("G1: %s:%d: %s: %s" % (rel, number, ", ".join(reasons), stripped))
        for header in _INCLUDE.findall("\n".join(code_lines)):
            used_headers.setdefault(header, rel)
        named: Dict[int, str] = {}
        for number, content in strings:
            named.setdefault(number, content)
        for number, code in enumerate(code_lines, 1):
            if _INCLUDE_QUOTED.match(code):
                target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), named.get(number, "")))
                if target not in texts:
                    problems.append("G5: %s:%d includes \"%s\", which is not one of the files the "
                                    "guards read (%s): a `long` in it would not be seen"
                                    % (rel, number, named.get(number, ""), ", ".join(SOURCE_GLOBS)))
    for entry, hits in reviewed_hits.items():
        if hits != entry.count:
            problems.append("G1: reviewed line `%s` of %s must be found %d time(s), found %d: "
                            "upstream changed it, review the new code and update REVIEWED"
                            % (entry.line, entry.path, entry.count, hits))

    # ---- G2: headers seen before the name redirections ------------------------------
    if port_header is not None:
        covered = set(system_headers(_read(Path(port_header))))
        for header in sorted(set(used_headers) - covered):
            problems.append("G2: %s includes <%s>, which %s does not include before its "
                            "redirections: add it there" % (used_headers[header], header,
                                                            Path(port_header).name))

    # ---- G3: free-text options ------------------------------------------------------
    table = _OPTION.findall(texts.get(OPTION_TABLE_FILE, ""))
    if len(table) < 20:
        problems.append("G3: %s: the option table was not recognised (%d value options found)"
                        % (OPTION_TABLE_FILE, len(table)))
    else:
        metavars = {meta for _, _, meta in table}
        for meta in sorted(metavars - KNOWN_METAVARS):
            problems.append("G3: %s: new kind of option value %r: decide whether the Windows main "
                            "may turn its backslashes into slashes, then add it to KNOWN_METAVARS"
                            % (OPTION_TABLE_FILE, meta))
        text_options = sorted(("" if short == "\\0" else short, name)
                              for short, name, meta in table if meta == "STR")
        if text_options != sorted(TEXT_OPTIONS):
            problems.append("G3: %s: the free-text options are now %r, the port knows %r: update "
                            "TEXT_OPTIONS here and kTextOptions in scepter_port.cpp"
                            % (OPTION_TABLE_FILE, text_options, sorted(TEXT_OPTIONS)))
    if port_source is not None:
        try:
            in_port = _port_text_options(Path(port_source))
            if in_port != sorted(TEXT_OPTIONS):
                problems.append("G3: %s leaves %r alone but TEXT_OPTIONS says %r: make them agree"
                                % (Path(port_source).name, in_port, sorted(TEXT_OPTIONS)))
            value_shorts = "".join(sorted(short for short, _name, _meta in table if short != "\\0"))
            if len(table) >= 20 and _port_value_shorts(Path(port_source)) != value_shorts:
                problems.append("G3: %s: the short options that take a value are now \"%s\", %s knows "
                                "\"%s\": update kValueShorts there"
                                % (OPTION_TABLE_FILE, value_shorts, Path(port_source).name,
                                   _port_value_shorts(Path(port_source))))
        except TransformError as exc:
            problems.append("G3: %s" % exc)

    # ---- G5: nothing in src/ that the guards above did not read -----------------------
    for path in sorted((tree / SOURCE_DIR).iterdir()):
        rel = path.relative_to(tree).as_posix()
        if path.is_dir():
            problems.append("G5: %s/ is a folder inside %s/: neither the guards nor the build look "
                            "into it. Read what upstream put there, then extend SOURCE_GLOBS and "
                            "recipe.py" % (rel, SOURCE_DIR))
        elif rel not in texts:
            problems.append("G5: %s is not one of the files the guards read (%s): a `long` in it "
                            "would not be seen. Read it, then extend SOURCE_GLOBS"
                            % (rel, ", ".join(SOURCE_GLOBS)))

    # ---- G6: upstream's own build ------------------------------------------------------
    problems.extend(_makefile_problems(tree))

    # ---- G4: the version ------------------------------------------------------------
    version = ""
    try:
        version = upstream_version(tree)
    except TransformError as exc:
        problems.append("G4: %s" % exc)

    if problems:
        raise TransformError("scepter source is not what the port expects (%d problem(s)):\n  %s"
                             % (len(problems), "\n  ".join(problems)))
    if not check:
        for rel, text in changed.items():
            _write(tree / rel, text)
    return {
        "version": version,
        "transformations": counts,
        "replaced": sum(c["replaced"] for c in counts.values()),
        "files_scanned": len(texts),
        "reviewed_lines": sum(reviewed_hits.values()),
        "system_headers": len(used_headers),
        "value_options": len(table),
        "written": [] if check else sorted(changed),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check = bool(args) and args[0] == "--check"
    if check:
        args = args[1:]
    if len(args) != 1:
        sys.stderr.write(__doc__)
        return 2
    tree = Path(args[0])
    port = tree / "port"
    if not (port / "scepter_port.h").is_file():           # a tree that was not staged yet
        port = Path(__file__).resolve().parent
    try:
        counts = apply(tree, port_header=port / "scepter_port.h",
                       port_source=port / "scepter_port.cpp", check=check)
    except TransformError as exc:
        sys.stderr.write("%s\n" % exc)
        return 1
    sys.stdout.write("transform%s: scepter %s, %d replacement(s) %s, %d file(s) scanned, "
                     "%d reviewed line(s), 0 problems\n"
                     % (" --check" if check else "", counts["version"], counts["replaced"],
                        "needed" if check else "written", counts["files_scanned"],
                        counts["reviewed_lines"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
