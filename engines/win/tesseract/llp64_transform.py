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
    labs/atol/strtol/strtoul/lround/lrint/ldiv/stol/stoul/... when called
                         -> llabs/atoll/strtoll/strtoull/llround/llrint/lldiv/stoll/stoull/...
    LONG_MAX LONG_MIN ULONG_MAX ldiv_t
                         -> LLONG_MAX LLONG_MIN ULLONG_MAX lldiv_t   (counted with the functions)
    123L / 123UL / 0x1fL -> 123LL / 123ULL / 0x1fLL  (integer literals only: 1.0L and 1e-3L are
                                                     long doubles and are left alone)
  string literals
    %ld %lu %li %lx %lX %lo (with any flags, width, precision) -> %lld ...  (printf and scanf formats;
                                                     "%%ld" is a percent sign and the letters "ld")

Not rewritten, because nothing lexical can decide them: a function of the list above used without
being called (`&std::strtol`), and `ftell`/`fseek`, whose offsets stay 32 bits on Windows.

As a module:  transform(text) -> (new_text, [n_type, n_func, n_literal, n_format])
              raises ValueError on a raw string literal, which the tokenizer does not understand.
As a script:  llp64_transform.py [--check] FILE...
              prints one line per changed file; with --check nothing is written and the exit
              status is 1 if anything would change.
"""
import re
import sys

#: Rewritten where they are called.
FUNCS = {
    "labs": "llabs", "atol": "atoll", "strtol": "strtoll", "strtoul": "strtoull",
    "lround": "llround", "lrint": "llrint", "stol": "stoll", "stoul": "stoull",
    "lroundf": "llroundf", "lrintf": "llrintf", "lroundl": "llroundl", "lrintl": "llrintl",
    "wcstol": "wcstoll", "wcstoul": "wcstoull", "ldiv": "lldiv",
}
#: Rewritten wherever they stand: the limits of `long` (2^31 on Windows) and the result of ldiv().
NAMES = {"LONG_MAX": "LLONG_MAX", "LONG_MIN": "LLONG_MIN", "ULONG_MAX": "ULLONG_MAX", "ldiv_t": "lldiv_t"}

# Comments, literals and numbers. A number is a token of its own so that the digit separators of
# 1'000'000 are not taken for the quotes of a character literal (which would hide the code between
# them). A raw string is recognised only to be refused.
TOKEN = re.compile(
    r'(?P<lc>//[^\n]*)'
    r'|(?P<bc>/\*.*?\*/)'
    r'|(?P<raw>(?<!\w)(?:u8|[uUL])?R"[^\s()\\"]{0,16}\()'
    r'|(?P<str>"(?:\\.|[^"\\\n])*")'
    r"|(?P<chr>'(?:\\.|[^'\\\n])+')"
    r"|(?P<num>(?<![\w.])\.?\d(?:[eEpP][-+]|'\w|[\w.])*)",
    re.S)

RE_LONG = re.compile(r'(?<!\w)long\b')
RE_AFTER_LONG = re.compile(r'\s+(?:long|double)\b')         # `long long`, `long double`: leave alone
RE_BEFORE_LONG = re.compile(r'(?<!\w)long\s+$')              # the second word of `long long`
RE_FUNC = re.compile(r'\b(' + "|".join(FUNCS) + r')\b(?=\s*\()')
RE_NAME = re.compile(r'\b(' + "|".join(NAMES) + r')\b')
# An integer literal with the suffix of `long`: 5L, 5UL, 5LU, in any base and either case.
RE_INT_L = re.compile(r"(?:0[xX][0-9a-fA-F']+|0[bB][01']+|\d[\d']*)(?:[uU]?(?P<a>[lL])|(?P<b>[lL])[uU])")
# "%%" first, so that the second percent sign of "%%ld" does not start a conversion.
RE_FMT = re.compile(r'%%|(%[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?)l(?!l)([diouxX])')


def _tokens(text):
    """(masked, string_spans, number_spans). ``masked`` has the length of ``text`` with every comment
    and the inside of every literal blanked (line ends kept), so a pattern can look across a comment
    but never into one. The spans are the (start, end) of the string literals and of the numbers."""
    out = []
    strings = []
    numbers = []
    pos = 0
    for m in TOKEN.finditer(text):
        out.append(text[pos:m.start()])
        body = m.group(0)
        kind = m.lastgroup
        if kind == "raw":
            line = text.count("\n", 0, m.start()) + 1
            raise ValueError("raw string literal found at line %d: extend the tokenizer before transforming" % line)
        if kind in ("str", "chr"):
            quote = body[0]
            out.append(quote + re.sub(r'[^\n]', ' ', body[1:-1]) + quote)
            if kind == "str":
                strings.append((m.start(), m.end()))
        elif kind == "num":
            out.append(body)
            numbers.append((m.start(), m.end()))
        else:
            out.append(re.sub(r'[^\n]', ' ', body))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out), strings, numbers


def _mask(text):
    """(masked, string_spans): see ``_tokens``."""
    masked, strings, _ = _tokens(text)
    return masked, strings


def transform(text):
    """Return ``(new_text, [types, functions, literals, formats])``."""
    masked, strings, numbers = _tokens(text)
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
    for m in RE_NAME.finditer(masked):
        counts[1] += 1
        edits.append((m.start(1), m.end(1), NAMES[m.group(1)]))
    for start, end in numbers:
        m = RE_INT_L.fullmatch(text, start, end)
        if m:
            group = "a" if m.group("a") else "b"
            counts[2] += 1
            edits.append((m.start(group), m.end(group), "LL"))
    for start, end in strings:
        for m in RE_FMT.finditer(text, start, end):
            if m.group(1) is None:          # "%%"
                continue
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
