#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""win_rules.py -- the exact-text source rules of the TesserACT port, and its guards.

A RULE replaces one exact piece of upstream text by another. It names the file, the text, the
replacement and the number of places it must match. Any other number stops the build: upstream
changed that line and a person has to look. A rule that is already applied is recognised, so the
rules can be run again on a transformed tree without changing it.

A GUARD changes nothing. It counts an exact piece of text in a set of files and stops the build when
the count is not the expected one. The guards watch the places where the compatibility layer
(ts_compat.h, ts_compat.cpp) relies on how upstream spells something, and the Linux-only paths that
would do nothing on Windows without anyone noticing.

The replacement macros are defined in ts_compat.h. On Linux they expand to exactly the text the
rule replaced, so the transformed tree compiles to the same program there.

As a module:  apply(root) -> dict of what each rule and guard found; raises RuleError.
As a script:  win_rules.py STAGED_TREE
"""
import glob
import os
import sys


class RuleError(Exception):
    """A rule or a guard did not find what it expects."""


#: (name, file, exact text, replacement, expected number of matches)
RULES = [
    # The provenance block of report.json reads the command line from /proc/self/cmdline ...
    ("R1", "src/p2_provenance.cpp",
     'std::ifstream f("/proc/self/cmdline", std::ios::binary);',
     'TS_COMPAT_CMDLINE_STREAM(f);', 1),
    # ... and hashes the running program through /proc/self/exe.
    ("R2", "src/p2_provenance.cpp",
     'digestFile("binary", "/proc/self/exe", false)',
     'digestFile("binary", TS_COMPAT_SELF_EXE, false)', 1),
]

#: Every engine source and header except the developer fork batch, which is not built for Windows.
ENGINE = ("src/*.cpp", "src/*.h", "!src/dev_fork_batch.cpp")

#: (name, file patterns ("!" excludes), exact text, expected total, what it protects)
GUARDS = [
    ("G1", ("src/util.cpp",), "long long currentMemoryBytes() {", 1,
     "ts_compat.h renames this function in util.cpp and ts_compat.cpp supplies the Windows one"),
    ("G2", ("src/util.cpp",), "long long totalMemoryBytes() {", 1,
     "ts_compat.h renames this function in util.cpp and ts_compat.cpp supplies the Windows one"),
    ("G3", ("src/util.cpp",), "long long peakMemoryBytes() {", 1,
     "ts_compat.h renames this function in util.cpp and ts_compat.cpp supplies the Windows one"),
    ("G4", ENGINE, '"/proc/', 1,
     "the only /proc path left after R1 and R2 is /proc/self/statm in util.cpp, which Windows replaces"),
    ("G5", ENGINE, "/dev/", 5,
     "/dev/null appears only in the mapper command lines of mappolish.cpp, which Windows does not run"),
    ("G6", ENGINE, "popen(", 1,
     "the only pipe is the mapper of --map-polish; ts_compat.h makes it fail with a note about --map-polish"),
    ("G7", ENGINE, "pclose(", 1,
     "the only pipe is the mapper of --map-polish; ts_compat.h makes it fail with a note about --map-polish"),
    ("G8", ENGINE, "freopen(", 0,
     "ts_compat.h remaps freopen for the test programs only; an engine that calls it needs the body in ts_compat.cpp"),
    ("G9", ("src/util.cpp",), "MemoryBytes(", 3,
     "util.cpp must only DEFINE the three memory queries: a call from inside util.cpp would reach the renamed "
     "POSIX copies, which return 0 on Windows and switch the --max-memory guard off without a word"),
    ("G10", ENGINE, "rename(", 0,
     "rename() onto an existing file fails on Windows; the engine has no such call outside the developer fork batch"),
]


def _read(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _files(root, patterns):
    keep, drop = set(), set()
    # Only the pattern is a pattern: a "[" in the path of the tree itself must not start a character set.
    base = glob.escape(root)
    for pattern in patterns:
        target = drop if pattern.startswith("!") else keep
        target.update(glob.glob(os.path.join(base, pattern.lstrip("!"))))
    return sorted(keep - drop)


def apply(root):
    """Apply every rule to the tree at ``root`` and check every guard.

    Returns ``{"rules": {name: "applied" | "already applied"}, "guards": {name: count}}``.
    """
    root = str(root)
    report = {"rules": {}, "guards": {}}
    for name, path, old, new, want in RULES:
        full = os.path.join(root, path)
        if not os.path.isfile(full):
            raise RuleError("rule %s: %s is missing -- upstream moved it; review the rule" % (name, path))
        text = _read(full)
        got = text.count(old)
        if got == 0 and text.count(new) == want:
            report["rules"][name] = "already applied"
            continue
        if got != want:
            raise RuleError("rule %s: %s: expected %d occurrence(s) of %r, found %d -- upstream changed "
                            "this line; review the rule in win_rules.py" % (name, path, want, old, got))
        _write(full, text.replace(old, new))
        report["rules"][name] = "applied"
    for name, patterns, text, want, why in GUARDS:
        files = _files(root, patterns)
        if not files:
            raise RuleError("guard %s: no file matches %s -- upstream moved it" % (name, ", ".join(patterns)))
        found = [(os.path.relpath(f, root), _read(f).count(text)) for f in files]
        got = sum(n for _, n in found)
        if got != want:
            where = ", ".join("%s: %d" % (f.replace(os.sep, "/"), n) for f, n in found if n) or "nowhere"
            raise RuleError("guard %s: expected %d occurrence(s) of %r, found %d (%s) -- %s. Check the new "
                            "code against the Windows port, then correct the count in win_rules.py"
                            % (name, want, text, got, where, why))
        report["guards"][name] = got
    return report


def main(argv):
    if len(argv) != 1:
        sys.exit(__doc__.strip().splitlines()[-1])
    try:
        report = apply(argv[0])
    except RuleError as exc:
        sys.exit("win_rules: %s" % exc)
    for name, state in sorted(report["rules"].items()):
        print("rule %s: %s" % (name, state))
    for name, count in sorted(report["guards"].items()):
        print("guard %s: %d" % (name, count))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
