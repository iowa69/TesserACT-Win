#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Run the specification model over every golden case and compare with what the real script did.

    python3 parity_check.py [--noreal] [--golden DIR] [--model IMPLEMENTATION.py] [case-substring ...]

For each golden/<fixture>/<invocation>/cmd.txt the same arguments and environment are given to
model/tesseract_model.py (in its own working directory, so the real outputs are not touched),
and exit status, stdout, stderr, preflight.txt, SUMMARY.txt, assembly_stats.tsv, the list of
files left in the output directory and (stub engine) the recorded engine argv/environment are
compared. Masked before comparison: the free-disk and free-memory messages, shell/tool
diagnostics on stderr that only bash can produce, and the absolute paths of the engine and of
the detection sketch in the recorded engine environment.

The goldens were captured in another folder, so the paths of cmd.txt are mapped to this
repository: the script's own directory to engines/tesseract/upstream (where the sketch is), the
engine to build/engines/linux/tesseract-asm (or $TESSERACT_ASM_REAL), the stand-in engine and
its canned folders to fake_asm/. Everything is run in out/model_run, which git ignores.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(W))
MODEL = os.path.join(HERE, "tesseract_model.py")
RUNDIR = os.path.join(W, "out", "model_run")
UPSTREAM = os.path.join(REPO, "engines", "tesseract", "upstream")
ENGINE = os.environ.get("TESSERACT_ASM_REAL") or os.path.join(REPO, "build", "engines", "linux", "tesseract-asm")
FAKE = os.path.join(W, "fake_asm", "fake_asm.sh")

# Cases where the model is MEANT to differ (specification, "fix deliberately"), or that it does not model.
EXPECTED = {
    "_args/option_without_value_HANGS": "script loops forever; port: usage error",
    "_args/help": "usage text not modelled", "_args/no_arguments": "usage text not modelled",
    "_args/models_help": "models sub-command not modelled",
    "_args/unknown_organism": "organism models not in the Windows app",
    "tiny29/check_gs_0": "invalid genome size: port rejects it", "tiny29/check_gs_abc": "invalid genome size: port rejects it",
    "tiny29/check_gs_0x10": "invalid genome size: port rejects it", "tiny29/check_gs_5,5": "invalid genome size: port rejects it",
    "tiny29/check_gs_0.3x": "invalid genome size: port rejects it", "tiny29/check_gs_-1": "invalid genome size: port rejects it",
    "tiny29/check_gs_1e-6": "exponent notation: port rejects it",
}

MASK = re.compile(rb"^(  (?:OK|FAIL|WARN) +(?:disk|memory) +).*$", re.M)
ENV_PATHS = re.compile(rb"^(TESSERACT_ASM=|TESSERACT_P2_DETECT_SKETCH=(?=/.*/models/om2detect\.sketch$)).*$", re.M)
NOISE = re.compile(rb"^(?:\S*tesseract: line \d+: .*|awk: .*|head: .*|cat: .*|gzip: .*)\n", re.M)


def norm(data, stderr=False):
    data = MASK.sub(rb"\1<masked>", data)
    return NOISE.sub(b"", data) if stderr else data


def read(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def tree(d):
    out = []
    for root, dirs, files in os.walk(d):
        for n in dirs:
            out.append(os.path.relpath(os.path.join(root, n), d) + "/")
        for n in files:
            out.append(os.path.relpath(os.path.join(root, n), d))
    return sorted(out)


def main():
    args = sys.argv[1:]
    noreal = "--noreal" in args
    golden = os.path.join(W, "golden")
    if "--golden" in args:
        golden = os.path.abspath(args[args.index("--golden") + 1])
        args.remove(args[args.index("--golden") + 1])
    model = MODEL
    if "--model" in args:                      # e.g. the real port, or a mutated copy of the model
        model = os.path.abspath(args[args.index("--model") + 1])
        args.remove(args[args.index("--model") + 1])
    subs = [a for a in args if not a.startswith("--")]
    os.makedirs(RUNDIR, exist_ok=True)
    for link in ("fixtures",):
        p = os.path.join(RUNDIR, link)
        if not os.path.islink(p):
            os.symlink(os.path.join(W, link), p)
    cases = sorted(os.path.relpath(os.path.dirname(os.path.join(r, f)), golden)
                   for r, _, fs in os.walk(golden) for f in fs if f == "cmd.txt")
    npass = nfail = nskip = 0
    for case in cases:
        if subs and not any(s in case for s in subs):
            continue
        g = os.path.join(golden, case)
        words = shlex.split(open(os.path.join(g, "cmd.txt")).read().split("\n")[0])
        k = next(i for i, w in enumerate(words) if w.endswith("/tesseract") and "=" not in w)
        env = dict(w.split("=", 1) for w in words[words.index("-i") + 1:k])
        argv = words[k + 1:]
        real_engine = env.get("TESSERACT_ASM", "").endswith("tesseract-asm")
        # cmd.txt names the folder the goldens were captured in; map it to this repository
        nosketch = "/_nosketch/" in words[k]
        if real_engine:
            env["TESSERACT_ASM"] = ENGINE
        elif env.get("TESSERACT_ASM", "").endswith("fake_asm.sh"):
            env["TESSERACT_ASM"] = FAKE
        if "FAKE_SRC" in env:
            env["FAKE_SRC"] = os.path.join(W, "fake_asm", "cases", os.path.basename(env["FAKE_SRC"]))
        toolbin = os.path.join(W, "toolbin", "native")
        env["PATH"] = toolbin if os.path.isdir(toolbin) else "/usr/bin:/bin"
        if nosketch:
            env["HOME"] = os.path.join(RUNDIR, "_nosketch", "home")
            os.makedirs(env["HOME"], exist_ok=True)
        out = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-o", "--out")), None)
        if case in EXPECTED:
            nskip += 1
            print("SKIP  %-45s %s" % (case, EXPECTED[case]))
            continue
        if noreal and real_engine and out and "run" in case.split("/")[1]:
            nskip += 1
            continue
        env["MODEL_HERE"] = os.path.join(RUNDIR, "_nosketch", "bin") if nosketch else UPSTREAM
        calls = os.path.join(RUNDIR, "calls", case)
        if "FAKE_REC" in env:
            shutil.rmtree(calls, ignore_errors=True)
            os.makedirs(calls)
            env["FAKE_REC"] = calls
        if out and "run_again" not in case:
            shutil.rmtree(os.path.join(RUNDIR, out), ignore_errors=True)
        p = subprocess.run([sys.executable, model] + argv, cwd=RUNDIR, env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        diffs = []
        if str(p.returncode) != open(os.path.join(g, "exit_status.txt")).read().strip():
            diffs.append("exit status %d" % p.returncode)
        if norm(p.stdout) != norm(read(os.path.join(g, "stdout.txt")) or b""):
            diffs.append("stdout")
        if norm(p.stderr, True) != norm(read(os.path.join(g, "stderr.txt")) or b"", True):
            diffs.append("stderr")
        if out and "run_again" not in case:
            for f in ("preflight.txt", "SUMMARY.txt", "assembly_stats.tsv"):
                a, e = read(os.path.join(RUNDIR, out, f)), read(os.path.join(g, f))
                if (a is None) != (e is None) or (a is not None and norm(a) != norm(e)):
                    diffs.append(f)
            exp = read(os.path.join(g, "out_tree.txt"))
            if exp is not None:
                want = sorted(x for x in exp.decode().split("\n") if x)
                if tree(os.path.join(RUNDIR, out)) != [w[2:] if w.startswith("./") else w for w in want]:
                    diffs.append("output tree")
        if "FAKE_REC" in env:
            gc = os.path.join(g, "calls")
            for f in sorted(set(os.listdir(gc)) | set(os.listdir(calls))):
                a, e = read(os.path.join(calls, f)), read(os.path.join(gc, f))
                if f.endswith(".env") and a is not None and e is not None:
                    a, e = ENV_PATHS.sub(rb"\1<masked>", a), ENV_PATHS.sub(rb"\1<masked>", e)
                if a != e:
                    diffs.append("calls/" + f)
        if diffs:
            nfail += 1
            print("FAIL  %-45s %s" % (case, ", ".join(diffs)))
        else:
            npass += 1
    print("model vs real script: %d identical, %d differ, %d skipped (expected deviations%s)"
          % (npass, nfail, nskip, " and real-engine runs" if noreal else ""))
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
