#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Compare freshly captured goldens with the recorded ones.

    python3 compare_goldens.py [NEW [OLD]]        (default: out/golden-new against golden)

For every case of NEW (what ./run_golden.sh just captured from the script) the exit status,
stdout, preflight.txt, SUMMARY.txt, assembly_stats.tsv and the list of files left in the
output folder are compared with OLD, after masking what depends on the machine or the run:
the free-disk and free-memory messages and the engine's own version line. stderr is compared
without the diagnostics of the shell and its tools, whose wording differs between systems.

No difference means the recorded goldens are what the script prints today. After a new
upstream release the differences are the list of what the port has to follow.
Exit status 1 when a case differs or NEW holds no case.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MACHINE = re.compile(rb"^(  (?:OK|FAIL|WARN) +(?:disk|memory) +).*$", re.M)
ENGINE = re.compile(rb"^(  version  tesseract [0-9.]+, engine ).*$", re.M)
NOISE = re.compile(rb"^(?:\S*tesseract: line \d+: .*|awk: .*|head: .*|cat: .*|gzip: .*|timeout: .*)\n", re.M)
FILES = ("exit_status.txt", "stdout.txt", "stderr.txt", "preflight.txt", "SUMMARY.txt", "assembly_stats.tsv",
         "out_tree.txt")


def read(path, name):
    try:
        with open(os.path.join(path, name), "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    data = ENGINE.sub(rb"\1<masked>", MACHINE.sub(rb"\1<masked>", data))
    return NOISE.sub(b"", data) if name == "stderr.txt" else data


def main():
    new = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(HERE, "out", "golden-new")
    old = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.join(HERE, "golden")
    cases = sorted(os.path.relpath(root, new) for root, _dirs, files in os.walk(new) if "cmd.txt" in files)
    same = differ = 0
    for case in cases:
        if not os.path.isdir(os.path.join(old, case)):
            differ += 1
            print("NEW     %s" % case)
            continue
        changed = [name for name in FILES if read(os.path.join(new, case), name) != read(os.path.join(old, case), name)]
        if changed:
            differ += 1
            print("DIFFERS %-45s %s" % (case, ", ".join(changed)))
        else:
            same += 1
    print("%d cases compared with %s: %d identical, %d differ" % (len(cases), old, same, differ))
    return 1 if differ or not cases else 0


if __name__ == "__main__":
    sys.exit(main())
