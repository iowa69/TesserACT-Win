#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""The engines' built-in self-tests, and their comparison between Linux and Windows.

    python engines/selftest.py run --engines DIR --out FILE
    python engines/selftest.py compare LINUX_FILE WINDOWS_FILE

Each engine answers a fixed list of questions about the platform it was built for when
it is started with ``TS_COMPAT_SELFTEST=1`` (``tesseract-asm``) or
``SCEPTER_PORT_SELFTEST=1`` (``scepter``): how numbers are rounded when printed, whether
two different files are told apart, whether a file over 4 GB has its real size, whether
a written line end is one byte, whether a folder with accented letters works.

``run`` asks both engines, writes their answers to FILE and exits 1 if either engine
says a probe failed. ``compare`` requires the answers of two platforms to be the same,
except those listed in ``PLATFORM_ANSWERS``, each with the reason why it may differ. It
prints every other answer that differs and exits 1.

Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple

#: engine -> (program name, the variable that starts its self-test)
ENGINES = {
    "tesseract-asm": ("tesseract-asm", "TS_COMPAT_SELFTEST"),
    "scepter": ("scepter", "SCEPTER_PORT_SELFTEST"),
}

#: Answers that may differ between Linux and Windows, or that only one of them gives:
#: (engine, answer name or pattern, reason). Every other answer must be the same.
PLATFORM_ANSWERS: Tuple[Tuple[str, str, str], ...] = (
    ("tesseract-asm", "platform", "names the platform the program was built for"),
    ("tesseract-asm", "sizeof_long", "8 bytes on Linux and 4 on Windows; the source is rewritten to "
                                     "'long long' for that reason, and sizeof_long_long is compared"),
    ("tesseract-asm", "argv_tail", "the command line as the system hands it over"),
    ("tesseract-asm", "thread_stack_4m", "on Linux the deep walk is skipped when the machine gives a "
                                         "thread less than 6 MiB of stack; on Windows it must pass"),
    ("scepter", "target", "names the platform the program was built for"),
    ("scepter", "sizeof_long", "8 bytes on Linux and 4 on Windows: the reason for transformation T1"),
    ("scepter", "sizeof_z_off_t", "zlib's own offset type, 8 on Linux and 4 on Windows; it only feeds "
                                  "the progress percentage"),
    ("scepter", "zlib_version", "each toolchain brings its zlib; the decompressed reads are compared, "
                                "never the gzip bytes"),
    ("scepter", "exe_path", "where the program is on this machine"),
    ("scepter", "work_dir", "a temporary folder named after the process number"),
    ("scepter", "info_*", "information about the PC, not a verdict (long paths are a Windows setting)"),
    ("scepter", "active_code_page", "Windows only: the UTF-8 code page asked for by the manifest"),
    ("scepter", "unicode_dir_wide", "Windows only: the folder really carries the intended name"),
    ("scepter", "error_mode", "Windows only: no crash dialog can block a run"),
    ("scepter", "default_file_mode", "Windows only: files are opened without line-end translation"),
    ("scepter", "stdin_mode", "Windows only: the standard streams are binary"),
    ("scepter", "stdout_mode", "Windows only: the standard streams are binary"),
    ("scepter", "stderr_mode", "Windows only: the standard streams are binary"),
    ("scepter", "wildcard_expansion", "Windows only: a * in an argument is not expanded"),
)


def say(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def platform_reason(engine: str, key: str) -> Optional[str]:
    """Why this answer may differ between platforms, or None when it must be the same."""
    for owner, pattern, reason in PLATFORM_ANSWERS:
        if owner == engine and fnmatch.fnmatchcase(key, pattern):
            return reason
    return None


# --------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------

def ask(program: str, variable: str) -> dict:
    """Start one engine in self-test mode; return its exit status, answers and messages."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("TESSERACT_")
           and k.upper() not in ("TS_COMPAT_SELFTEST", "SCEPTER_PORT_SELFTEST")}
    env[variable] = "1"
    env["NO_COLOR"] = "1"
    with tempfile.TemporaryDirectory(prefix="engine-selftest-") as work:
        try:
            done = subprocess.run([program], cwd=work, env=env, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
            status, out, err = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as exc:
            status, out, err = 124, exc.stdout or b"", (exc.stderr or b"") + b"\nno answer after 15 minutes\n"
        except OSError as exc:
            status, out, err = 127, b"", ("cannot be started: %s\n" % (exc.strerror or exc)).encode()
    answers: List[Tuple[str, str]] = []
    other: List[str] = []
    for line in out.decode("utf-8", "replace").replace("\r\n", "\n").split("\n"):
        if "=" in line and not line.startswith((" ", "\t", "#", "[")):
            key, value = line.split("=", 1)
            answers.append((key.strip(), value.strip()))
        elif line.strip():
            other.append(line.rstrip())
    notes = [line for line in err.decode("utf-8", "replace").replace("\r\n", "\n").split("\n") if line.strip()]
    # What a program printed that is no answer (a crash message, the help of a program that
    # has no self-test) is kept as well: it is all there is to explain a failure.
    notes += ["(printed) " + line for line in other[-30:]]
    return {"exit": status, "answers": answers, "notes": notes}


def failure(result: dict) -> str:
    """Why this self-test counts as failed ("" when it passed)."""
    verdict = dict(result["answers"]).get("selftest", "")
    if result["exit"] != 0:
        return "ended with status %d" % result["exit"]
    if not verdict.startswith("pass"):
        return "did not end with selftest=pass (%s)" % (verdict or "no verdict: is this an engine "
                                                        "built by engines/build.py?")
    return ""


def render(results: Dict[str, dict]) -> str:
    lines = ["# Answers of the engines' built-in self-tests (engines/selftest.py run).",
             "# One [engine] block each; 'name=answer' lines; '#' lines are what the engine said besides.",
             "format=1", "host_platform=%s" % sys.platform]
    for engine, result in results.items():
        lines.append("")
        lines.append("[%s]" % engine)
        lines.append("exit=%d" % result["exit"])
        lines += ["%s=%s" % pair for pair in result["answers"]]
        lines += ["# " + note for note in result["notes"]]
    return "\n".join(lines) + "\n"


def cmd_run(args) -> int:
    folder = os.path.abspath(args.engines)
    out = os.path.abspath(args.out)
    if os.path.isfile(out):             # answers of an earlier run must not pass for these
        os.remove(out)
    suffix = ".exe" if os.name == "nt" else ""
    results: Dict[str, dict] = {}
    failed: List[str] = []
    for engine, (name, variable) in ENGINES.items():
        program = os.path.join(folder, name + suffix)
        if not os.path.isfile(program):
            results[engine] = {"exit": 127, "answers": [], "notes": ["%s is missing" % program]}
        else:
            results[engine] = ask(program, variable)
        why = failure(results[engine])
        say("%-14s %d answers, %s" % (engine, len(results[engine]["answers"]), why or "passed"))
        if why:
            failed.append(engine)
            for key, value in results[engine]["answers"]:
                say("    %s=%s" % (key, value))
            for note in results[engine]["notes"]:
                say("    # " + note)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render(results))
    if failed:
        say("FAILED: the self-test of %s did not pass. The answers are in %s." % (" and ".join(failed), args.out))
        return 1
    say("both self-tests passed: %s" % args.out)
    return 0


# --------------------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------------------

def parse(path: str) -> Dict[str, Dict[str, str]]:
    """A file written by ``run`` as ``{engine: {name: answer}}`` (``exit`` is one of the names)."""
    engines: Dict[str, Dict[str, str]] = {}
    current: Optional[Dict[str, str]] = None
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            if not line.strip() or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = engines.setdefault(line[1:-1], {})
            elif current is not None and "=" in line:
                key, value = line.split("=", 1)
                current[key] = value
    return engines


def compare_answers(a: Dict[str, Dict[str, str]], b: Dict[str, Dict[str, str]],
                    name_a: str = "A", name_b: str = "B") -> Tuple[List[str], int, int]:
    """(differences as sentences, answers compared, answers left out as platform-specific)."""
    problems: List[str] = []
    compared = left_out = 0
    for engine in ENGINES:
        if engine not in a or engine not in b:
            problems.append("%s: no answers in %s" % (engine, name_a if engine not in a else name_b))
            continue
        for label, answers in ((name_a, a[engine]), (name_b, b[engine])):
            if answers.get("exit") != "0" or not answers.get("selftest", "").startswith("pass"):
                problems.append("%s: the self-test recorded in %s did not pass (exit %s, selftest=%s)"
                                % (engine, label, answers.get("exit", "?"), answers.get("selftest", "none")))
        for key in sorted(set(a[engine]) | set(b[engine])):
            if platform_reason(engine, key) is not None:
                left_out += 1
                continue
            x, y = a[engine].get(key), b[engine].get(key)
            if x == y:
                compared += 1
            elif x is None or y is None:
                problems.append("%s: %s is answered only in %s (%s=%s)"
                                % (engine, key, name_b if x is None else name_a, key, y if x is None else x))
            else:
                problems.append("%s: %s differs\n    %s: %s\n    %s: %s" % (engine, key, name_a, x, name_b, y))
    return problems, compared, left_out


def cmd_compare(args) -> int:
    try:
        a, b = parse(args.linux_file), parse(args.windows_file)
    except OSError as exc:
        sys.stderr.write("selftest: cannot read %s: %s\n" % (exc.filename, exc.strerror))
        return 1
    except ValueError as exc:           # not UTF-8: not a file that "run" wrote
        sys.stderr.write("selftest: %s or %s is not a file written by 'selftest.py run' (%s)\n"
                         % (args.linux_file, args.windows_file, exc))
        return 1
    problems, compared, left_out = compare_answers(a, b, args.linux_file, args.windows_file)
    if problems:
        say("\n".join(problems))
        say("\n%d difference(s). An answer that may rightly differ between Linux and Windows belongs in "
            "PLATFORM_ANSWERS of engines/selftest.py, with the reason." % len(problems))
        return 1
    say("the self-tests agree: %d answers identical, %d left out because they describe the platform"
        % (compared, left_out))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run both self-tests and write the answers")
    run.add_argument("--engines", required=True, help="folder with tesseract-asm and scepter")
    run.add_argument("--out", required=True, help="file to write the answers to")
    compare = sub.add_parser("compare", help="require the answers of two platforms to agree")
    compare.add_argument("linux_file")
    compare.add_argument("windows_file")
    args = parser.parse_args(argv)
    return cmd_run(args) if args.command == "run" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
