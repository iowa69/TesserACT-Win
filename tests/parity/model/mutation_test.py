#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Do the goldens catch the mistakes a port is likely to make?

    python3 mutation_test.py

Each entry rewrites one line of the specification model into a plausible "natural" variant
(what a programmer would write without the specification) and runs parity_check.py --noreal
on it. A mutation that no golden case catches would mean a quirk is specified but not pinned.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MUTATIONS = [
    ("fraction compared unrounded", 'adapt=float("%.4f" % (adapt / s if s else 0)),', "adapt=(adapt / s if s else 0),"),
    ("depth rounded half-up", 'depth = int("%.0f" % (st["bases"] / (float(gs) * 1e6)))',
     'depth = int(st["bases"] / (float(gs) * 1e6) + 0.5)'),
    ("no 200,000-row sampling limit", "if n <= SAMPLE_ROWS:", "if True:"),
    ("max length from the sample only", "        maxlen = max(maxlen, l1, l2)\n        if n <= SAMPLE_ROWS:",
     "        if n <= SAMPLE_ROWS:\n            maxlen = max(maxlen, l1, l2)"),
    ("CR stripped from line ends", 'buf.append(line[:-1] if line.endswith(b"\\n") else line)', 'buf.append(line.rstrip(b"\\r\\n"))'),
    ("every quality character examined", "for ch in f[3][::7]:", "for ch in f[3]:"),
    ("both names in the mismatch message", 'firstm = n1                        # the script builds "A vs B" but prints only A',
     'firstm = n1 + b" vs " + n2'),
    ("lower-case n counted", 'nn += f[1].count(b"N") + f[5].count(b"N")', 'nn += f[1].upper().count(b"N") + f[5].upper().count(b"N")'),
    ("Phred+64 rule with >= 74", "elif lo >= 64 and hi > 74:", "elif lo >= 64 and hi >= 74:"),
    ("same file decided by path text", "if os.path.samefile(r1, r2):", "if os.path.abspath(r1) == os.path.abspath(r2):"),
    ("trailing bytes after gzip ignored", "            while g.read(1 << 20):\n                pass\n        return True\n    except GZ_ERRORS:\n        return False",
     "            while g.read(1 << 20):\n                pass\n        return True\n    except gzip.BadGzipFile as e:\n        return 'Not a gzipped file' in str(e)\n    except GZ_ERRORS:\n        return False"),
    ("blank trailing line ignored", "    if buf:\n        yield b\"\\t\".join(buf + [b\"\"] * (4 - len(buf)))",
     "    if buf and buf != [b\"\"]:\n        yield b\"\\t\".join(buf + [b\"\"] * (4 - len(buf)))"),
    ("kb rounded half-up", 'b("%.1f" % (float(lost or 0) / 1000))', "b(str(int(float(lost or 0) / 100 + 0.5) / 10))"),
    ("N50 with > instead of >=", "if not n50 and acc >= tot * 0.5:", "if not n50 and acc > tot * 0.5:"),
    ("size limits inclusive", "if r < 0.8 or r > 1.3:", "if r <= 0.8 or r >= 1.3:"),
    ("contig name = whole header line", 'name = re.match(rb"[^ \\t]*", line).group(0)[1:]', "name = line[1:]"),
    ("retry threshold from --genome-size", 'want = int(float(gs or "5") * 1e6 * 0.2)', 'want = int(float(gsize or "5") * 1e6 * 0.2)'),
    ("retry when k-mer depth <= 2", "if got < want and kdepth < 2:", "if got < want and kdepth <= 2:"),
    ("first k-mer depth line used", "kdepth = float(hits[-1])", "kdepth = float(hits[0])"),
    ("retry -k appended after user arguments", '+ (["-k", kmers] if kmers else []) + extra', '+ extra + (["-k", kmers] if kmers else [])'),
    ("empty user variable wins", "if not os.environ.get(k):", "if k not in os.environ:"),
    ("--keep-going needs --force", "return 0 if rep.fails == 0 or keepgoing else 1", "return 0 if rep.fails == 0 or (keepgoing and force) else 1"),
    ("preflight failures kept in final count", "    rep.fails = rep.warns = 0\n    engine", "    rep.warns = 0\n    engine"),
    ("disk/memory skipped after a failed read check", '    if cmd == "run":\n        preflight_machine(rep, r1, r2, out)',
     '    if cmd == "run" and not rep.fails:\n        preflight_machine(rep, r1, r2, out)'),
    ("statistics still run after a gzip failure", "    if rep.fails:\n        return None\n    st = read_stats(r1, r2)", "    st = read_stats(r1, r2)"),
]


def main():
    src = open(os.path.join(HERE, "tesseract_model.py")).read()
    missed = 0
    for title, old, new in MUTATIONS:
        if old not in src:
            print("STALE  %-45s (the model no longer contains this line)" % title)
            missed += 1
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=HERE) as fh:
            fh.write(src.replace(old, new, 1))
        p = subprocess.run([sys.executable, os.path.join(HERE, "parity_check.py"), "--noreal", "--model", fh.name],
                           stdout=subprocess.PIPE, universal_newlines=True)
        os.unlink(fh.name)
        caught = [l.split()[1] for l in p.stdout.split("\n") if l.startswith("FAIL")]
        if not caught:
            missed += 1
        print("%-6s %-45s %d case(s): %s" % ("caught" if caught else "MISSED", title, len(caught), " ".join(caught[:4]) + (" ..." if len(caught) > 4 else "")))
    print("%d mutations, %d not caught" % (len(MUTATIONS), missed))
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
