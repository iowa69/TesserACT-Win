#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""llp64_transform.py -- make every engine-owned `long` 64 bits on LLP64 (Windows).

On Windows `long` is 32 bits; on Linux it is 64. This rewrite makes the integer conversion rules of
the transformed tree on Windows equal to those of the pristine tree on Linux, for every `long` the
engine owns, including code a future release adds. On Linux it changes no behaviour (`long` and
`long long` are both 64 bits there), which the Linux build of the transformed tree proves.

Mechanical, re-runnable, idempotent. Applied to C++ sources (src/*.cpp, src/*.h, tests/*.cpp,
tests/*.h). Never run it on the compatibility layer itself: that code needs the real Win32 LONG.

  code (outside comments and string/char literals)
    long                 -> long long               (not `long long`, not `long double`; this also
                                                     turns `unsigned long` into `unsigned long long`)
    labs/atol/strtol/strtoul/lround/lrint/stol/stoul/...
                         -> llabs/atoll/strtoll/strtoull/llround/llrint/stoll/stoull/...
    123L / 123UL / 0x1fL -> 123LL / 123ULL / 0x1fLL  (1.0L, a long double, is left alone)
  string literals
    %ld %lu %li %lx %lX %lo (with any flags, width, precision) -> %lld ...  (printf and scanf formats)

As a module:  transform(text) -> (new_text, [n_type, n_func, n_literal, n_format])
              raises ValueError on a raw string literal, which the tokenizer does not understand.
As a script:  llp64_transform.py [--check] FILE...
              prints one line per changed file; with --check nothing is written and the exit
              status is 1 if anything would change.
"""
import re
import sys

FUNCS = {
    "labs": "llabs", "atol": "atoll", "strtol": "strtoll", "strtoul": "strtoull",
    "lround": "llround", "lrint": "llrint", "stol": "stoll", "stoul": "stoull",
    "lroundf": "llroundf", "lrintf": "llrintf", "wcstol": "wcstoll", "wcstoul": "wcstoull",
}

# comments and literals (raw strings are refused below)
TOKEN = re.compile(
    r'(?P<lc>//[^\n]*)'
    r'|(?P<bc>/\*.*?\*/)'
    r'|(?P<str>"(?:\\.|[^"\\\n])*")'
    r"|(?P<chr>'(?:\\.|[^'\\\n])+')",
    re.S)
RAW_STRING = re.compile(r'\bR"[^\n"]*\(')

RE_LONG = re.compile(r'(?<!\w)long\b')
RE_AFTER_LONG = re.compile(r'\s+(?:long|double)\b')         # `long long`, `long double`: leave alone
RE_BEFORE_LONG = re.compile(r'(?<!\w)long\s+$')              # the second word of `long long`
RE_FUNC = re.compile(r'\b(' + "|".join(FUNCS) + r')\b(?=\s*\()')
_NUMBER = r'(?:0[xX][0-9a-fA-F\']+|0[bB][01\']+|\d[\d\']*)'
RE_LIT = re.compile(r'(?<![\w.])(' + _NUMBER + r')([uU]?)([lL])(?![lL\w])')
RE_LIT2 = re.compile(r'(?<![\w.])(' + _NUMBER + r')([lL])([uU])(?![lL\w])')
RE_FMT = re.compile(r'(%[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?)l(?!l)([diouxX])')


def _mask(text):
    """(masked, string_spans): ``masked`` has the length of ``text`` with every comment and the inside
    of every literal blanked (line ends kept), so a pattern can look across a comment but never
    into one; ``string_spans`` are the (start, end) of the string literals."""
    out = []
    strings = []
    pos = 0
    for m in TOKEN.finditer(text):
        out.append(text[pos:m.start()])
        body = m.group(0)
        if m.lastgroup in ("str", "chr"):
            quote = body[0]
            out.append(quote + re.sub(r'[^\n]', ' ', body[1:-1]) + quote)
            if m.lastgroup == "str":
                strings.append((m.start(), m.end()))
        else:
            out.append(re.sub(r'[^\n]', ' ', body))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), strings


def transform(text):
    """Return ``(new_text, [types, functions, literals, formats])``."""
    if RAW_STRING.search(text):
        raise ValueError("raw string literal found: extend the tokenizer before transforming")
    masked, strings = _mask(text)
    counts = [0, 0, 0, 0]
    edits = []          # (start, end, replacement) on the ORIGINAL text; never overlapping

    for m in RE_LONG.finditer(masked):
        if RE_AFTER_LONG.match(masked, m.end()):
            continue
        if RE_BEFORE_LONG.search(masked, max(0, m.start() - 200), m.start()):
            continue
        counts[0] += 1
        edits.append((m.end(), m.end(), " long"))
    for m in RE_FUNC.finditer(masked):
        counts[1] += 1
        edits.append((m.start(1), m.end(1), FUNCS[m.group(1)]))
    for m in RE_LIT.finditer(masked):
        counts[2] += 1
        edits.append((m.start(3), m.end(3), "LL"))
    for m in RE_LIT2.finditer(masked):
        counts[2] += 1
        edits.append((m.start(2), m.end(2), "LL"))
    for start, end in strings:
        for m in RE_FMT.finditer(text, start, end):
            counts[3] += 1
            edits.append((m.end(1), m.end(1) + 1, "ll"))

    edits.sort()
    out = []
    pos = 0
    for start, end, replacement in edits:
        if start < pos:
            raise ValueError("overlapping rewrites near offset %d" % start)
        out.append(text[pos:start])
        out.append(replacement)
        pos = end
    out.append(text[pos:])
    return "".join(out), counts


def main(argv):
    check = False
    if argv and argv[0] == "--check":
        check, argv = True, argv[1:]
    changed = 0
    for path in argv:
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        try:
            new, counts = transform(text)
        except ValueError as exc:
            sys.exit("%s: %s" % (path, exc))
        if new != text:
            changed += 1
            print("%s\ttype=%d func=%d literal=%d format=%d" % (path, *counts))
            if not check:
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(new)
    return 1 if (check and changed) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
