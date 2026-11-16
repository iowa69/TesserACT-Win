#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Fingerprint of an upstream `tesseract` wrapper script, for detecting that it changed.

    python3 script_fingerprint.py PATH/tesseract            print the fingerprint (JSON)
    python3 script_fingerprint.py PATH/tesseract RECORDED   compare with a recorded fingerprint;
                                                            exit 1 and name what changed

The fingerprint holds the sha256 of the whole file, the sha256 of each section (the script
marks them with `# ---- name` rulers: head, arguments, preflight, assembly, postflight), the
same section hashes with comment-only and blank lines removed (so a comment edit can be told
from a logic edit), and the literals a port must mirror, pulled out with regular expressions.
"""
import hashlib
import json
import re
import sys


def sha(text):
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def fingerprint(path):
    raw = open(path, "rb").read()
    text = raw.decode("utf-8", "surrogateescape")
    sections, name, buf = {}, "head", []
    for line in text.split("\n"):
        m = re.match(r"^# -{20,} (\w+)\s*$", line)
        if m:
            sections[name] = "\n".join(buf)
            name, buf = m.group(1), []
        buf.append(line)
    sections[name] = "\n".join(buf)

    def code(s):
        return "\n".join(l for l in s.split("\n") if l.strip() and not l.lstrip().startswith("#"))

    def grab(pattern, flags=0):
        return sorted(set(re.findall(pattern, text, flags)))
    prof = re.search(r"PROFILE=\((.*?)\)", text, re.S)
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "lines": text.count("\n"),
        "version": (re.search(r'^VERSION="([^"]*)"', text, re.M) or [None, None])[1],
        "sections": {k: sha(v) for k, v in sections.items()},
        "sections_code_only": {k: sha(code(v)) for k, v in sections.items()},
        "literals": {
            "profile": prof.group(1).split() if prof else None,
            "profile_added_with_sketch": grab(r"PROFILE\+=\((.*?)\)"),
            "organism_mb": grab(r"(\w+)\) echo ([0-9.]+)"),
            "sample_rows": grab(r"n <= (\d+)"),
            "adapter": grab(r'index\(\$2, "([ACGT]+)"\)'),
            "polyg": grab(r"\$2 ~ /(G+)\$/"),
            "quality_step": grab(r"i \+= (\d+)"),
            "thresholds_awk": grab(r"exit !\((.*?)\) \}"),
            "thresholds_shell": grab(r'\[ "\$\{?\w+(?::-\d+)?\}?" -(?:lt|ge|gt|le|eq) "?\$?\w+"? \]'),
            "retry_ladder": grab(r"-k (\d+(?:,\d+)+)"),
            "retry_want": grab(r"g \* 1e6 \* ([0-9.]+)"),
            "disk_need": grab(r"need_kb=(.*)"),
            "printf_formats": grab(r'printf ?\(? ?"([^"]*%[^"]*)"'),
            "check_names": grab(r'pf (?:OK|WARN|FAIL) "([^"]+)"'),
            "engine_outputs_read": grab(r'\$(?:d|OUT)/([A-Za-z0-9_.]+)'),
            "report_json_keys": grab(r"grep -o '(\"[a-z_0-9]+\")"),
        },
    }


def main():
    fp = json.loads(json.dumps(fingerprint(sys.argv[1])))      # tuples -> lists, as in a recorded file
    if len(sys.argv) < 3:
        json.dump(fp, sys.stdout, indent=1, sort_keys=True)
        print()
        return 0
    old = json.load(open(sys.argv[2]))
    if old == fp:
        print("unchanged: %s is the script this port matches (version %s)" % (sys.argv[1], fp["version"]))
        return 0
    print("CHANGED: version %s -> %s, sha256 %s -> %s" % (old["version"], fp["version"], old["sha256"][:12], fp["sha256"][:12]))
    for k in sorted(set(old["sections"]) | set(fp["sections"])):
        a, c = old["sections"].get(k), fp["sections"].get(k)
        if a != c:
            logic = old["sections_code_only"].get(k) != fp["sections_code_only"].get(k)
            print("  section %-10s %s" % (k, "LOGIC changed" if logic else "comments/blank lines only"))
    for k in sorted(set(old["literals"]) | set(fp["literals"])):
        if old["literals"].get(k) != fp["literals"].get(k):
            print("  literal %-26s %s -> %s" % (k, old["literals"].get(k), fp["literals"].get(k)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
