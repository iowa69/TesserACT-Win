#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""A quick end-to-end check of both engines, the same on Linux and on Windows.

    python engines/smoke.py run --engines build/engines/linux --out build/smoke-linux.json
    python engines/smoke.py run --engines build/engines/windows --out build/smoke-windows.json
    python engines/smoke.py compare build/smoke-linux.json build/smoke-windows.json

``run`` executes each engine's built-in self-test, then trims and assembles the bundled
example reads exactly as the program does (the standard trimming flags; the settings
profile of the Ubuntu ``tesseract`` command), inside a folder whose name has a space and
accented letters, and records a checksum of everything the engines wrote.

``compare`` requires the two records to agree: identical trimmed reads, identical
assembly files, identical answers from the self-tests wherever the platform does not
legitimately differ. It exits 1 and prints every difference otherwise.

Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

#: The nine variables the Ubuntu ``tesseract`` command sets (tesseract 1.6.2, "PROFILE").
PROFILE = {
    "TESSERACT_P2_LIBGUARD": "1", "TESSERACT_P2_CIRC": "verify", "TESSERACT_P2_SPIKEIN": "1",
    "TESSERACT_P2_ENDS": "1", "TESSERACT_P2_TIPS_LOWERCASE": "1", "TESSERACT_P2_SELF_QA": "1",
    "TESSERACT_P2_PROVENANCE": "1", "TESSERACT_DEEP_NORM": "30", "TESSERACT_DEEP_NORM_MATE": "both",
}
#: scepter flags behind the standard ticks (equal to ``--preset assembly``).
TRIM_FLAGS = ["--cut_tail", "--cut_tail_mean_quality", "10", "--qualified_quality_phred", "15",
              "--unqualified_percent_limit", "40", "--n_base_limit", "5", "--average_qual", "15",
              "--length_required", "50"]
#: Assembly outputs that must be the same on both platforms (when written).
ASSEMBLY_FILES = ["contigs.fasta", "scaffolds.fasta", "scaffolds.agp", "assembly_graph.gfa",
                  "replicons.tsv", "ends.tsv", "p2_edits.tsv", "p2_self_qa.tsv"]
#: Self-test answers that legitimately differ between platforms or machines.
SELFTEST_FREE = ("platform", "target", "sizeof_long", "exe_path", "work_dir", "argv_tail",
                 "zlib_version", "info_", "ts_compat selftest", "sizeof_stat_size", "sizeof_z_off_t")
WORK_NAME = "smoke run à côté"       # a space and accented letters, on purpose


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_gz_content(path: str) -> str:
    """Checksum of the decompressed reads: the gzip wrapper may differ by a header byte."""
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def exe(folder: str, name: str) -> str:
    return os.path.join(folder, name + (".exe" if os.name == "nt" else ""))


def clean_env(extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("TESSERACT_")}
    env.update(extra)
    env["NO_COLOR"] = "1"
    return env


def run_selftest(program: str, variable: str) -> dict:
    proc = subprocess.run([program], env=clean_env({variable: "1"}), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = proc.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    answers = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith(" "):
            key, value = line.split("=", 1)
            answers[key.strip()] = value.strip()
    return {"exit": proc.returncode, "answers": answers, "raw": text}


def run_logged(argv, cwd, env, log_path) -> int:
    with open(log_path, "ab") as log:
        return subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT).returncode


def cmd_run(args) -> int:
    engines = os.path.abspath(args.engines)
    asm, scepter = exe(engines, "tesseract-asm"), exe(engines, "scepter")
    sketch = os.path.join(engines, "om2detect.sketch")
    record = {"platform": sys.platform, "engines": engines, "threads": args.threads}
    ok = True

    record["versions"] = {}
    for name, program in (("tesseract-asm", asm), ("scepter", scepter)):
        proc = subprocess.run([program, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        lines = [line for line in proc.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        first = next((line.strip() for line in lines if "TesserACT" in line or "scepter" in line), "")
        record["versions"][name] = first
        print("%-14s %s" % (name, first))

    record["selftest"] = {"tesseract-asm": run_selftest(asm, "TS_COMPAT_SELFTEST"),
                          "scepter": run_selftest(scepter, "SCEPTER_PORT_SELFTEST")}
    for name, result in record["selftest"].items():
        print("self-test %-14s exit %d, %d answers" % (name, result["exit"], len(result["answers"])))
        if result["exit"] != 0:
            ok = False
            print(result["raw"])

    work = os.path.abspath(os.path.join(args.work, WORK_NAME))
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(os.path.join(work, "trimming"))
    example = os.path.join(work, "example")
    subprocess.run([sys.executable, os.path.join(REPO, "tools", "make_example.py"), example], check=True)
    r1 = os.path.join(example, "Example_isolate_R1.fastq.gz")
    r2 = os.path.join(example, "Example_isolate_R2.fastq.gz")
    record["input"] = {"R1": sha256_gz_content(r1), "R2": sha256_gz_content(r2)}

    started = time.monotonic()
    trim_dir = os.path.join(work, "trimming")
    t1, t2 = "Example_isolate_trimmed_R1.fastq.gz", "Example_isolate_trimmed_R2.fastq.gz"
    rc = run_logged([scepter, "-i", r1, "-I", r2, "-o", t1, "-O", t2, "-j", "trimming_report.json",
                     "-h", "trimming_report.html", "-R", "Example_isolate", "-w", "1",
                     "--no-color", "--no-summary", "--verbose", *TRIM_FLAGS],
                    trim_dir, clean_env({}), os.path.join(trim_dir, "trimming.log"))
    record["trim"] = {"exit": rc, "seconds": round(time.monotonic() - started, 1)}
    print("trimming exit %d (%.1f s)" % (rc, record["trim"]["seconds"]))
    if rc != 0:
        ok = False
    else:
        record["trim"]["R1"] = sha256_gz_content(os.path.join(trim_dir, t1))
        record["trim"]["R2"] = sha256_gz_content(os.path.join(trim_dir, t2))
        with open(os.path.join(trim_dir, "trimming_report.json"), encoding="utf-8") as handle:
            report = json.load(handle)
        summary = report.get("summary", {})
        record["trim"]["before_reads"] = summary.get("before_filtering", {}).get("total_reads")
        record["trim"]["after_reads"] = summary.get("after_filtering", {}).get("total_reads")
        record["trim"]["after_bases"] = summary.get("after_filtering", {}).get("total_bases")
        record["trim"]["adapter_trimmed_reads"] = report.get("adapter_cutting", {}).get("adapter_trimmed_reads")
        record["trim"]["filtering_result"] = report.get("filtering_result")

    if rc == 0:
        started = time.monotonic()
        extra = dict(PROFILE)
        if os.path.isfile(sketch):
            extra["TESSERACT_P2_DETECT_REPORT"] = "1"
            extra["TESSERACT_P2_DETECT_SKETCH"] = sketch
        rc = run_logged([asm, "-1", os.path.join("trimming", t1), "-2", os.path.join("trimming", t2),
                         "-o", "assembly", "-t", str(args.threads)],
                        work, clean_env(extra), os.path.join(work, "assembly.log"))
        record["assembly"] = {"exit": rc, "seconds": round(time.monotonic() - started, 1), "files": {}}
        print("assembly exit %d (%.1f s)" % (rc, record["assembly"]["seconds"]))
        if rc != 0:
            ok = False
            with open(os.path.join(work, "assembly.log"), "rb") as handle:
                print(handle.read()[-3000:].decode("utf-8", "replace"))
        else:
            out = os.path.join(work, "assembly")
            for name in ASSEMBLY_FILES:
                path = os.path.join(out, name)
                if os.path.isfile(path):
                    with open(path, "rb") as handle:
                        data = handle.read()
                    record["assembly"]["files"][name] = {"sha256": sha256_bytes(data), "bytes": len(data),
                                                         "crlf": data.count(b"\r\n")}
            with open(os.path.join(out, "contigs.fasta"), encoding="ascii") as handle:
                record["assembly"]["headers"] = [line.strip() for line in handle if line.startswith(">")]
            for header in record["assembly"]["headers"]:
                print("  " + header)
            record["assembly"]["written"] = sorted(os.listdir(out))

    record["ok"] = ok
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(record, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    print("smoke %s: %s" % ("passed" if ok else "FAILED", args.out))
    return 0 if ok else 1


def cmd_compare(args) -> int:
    with open(args.a, encoding="utf-8") as handle:
        a = json.load(handle)
    with open(args.b, encoding="utf-8") as handle:
        b = json.load(handle)
    problems = []

    def same(label, x, y):
        if x != y:
            problems.append("%s differs:\n    %s: %r\n    %s: %r" % (label, args.a, x, args.b, y))

    same("example reads R1", a["input"]["R1"], b["input"]["R1"])
    same("example reads R2", a["input"]["R2"], b["input"]["R2"])
    for key in ("R1", "R2", "before_reads", "after_reads", "after_bases", "adapter_trimmed_reads",
                "filtering_result"):
        same("trimming %s" % key, a.get("trim", {}).get(key), b.get("trim", {}).get(key))
    fa, fb = a.get("assembly", {}).get("files", {}), b.get("assembly", {}).get("files", {})
    same("assembly files written", sorted(fa), sorted(fb))
    for name in sorted(set(fa) & set(fb)):
        same("assembly %s" % name, fa[name], fb[name])
    same("contig headers", a.get("assembly", {}).get("headers"), b.get("assembly", {}).get("headers"))
    same("files in the assembly folder", a.get("assembly", {}).get("written"),
         b.get("assembly", {}).get("written"))
    for engine in ("tesseract-asm", "scepter"):
        sa = a["selftest"][engine]["answers"]
        sb = b["selftest"][engine]["answers"]
        # A probe that exists on one platform only (the Windows-specific ones) has nothing
        # to be compared with; its own pass or fail is already in that platform's exit code.
        for key in sorted(set(sa) & set(sb)):
            if key.startswith(SELFTEST_FREE):
                continue
            same("self-test %s %s" % (engine, key), sa.get(key), sb.get(key))
    for record, path in ((a, args.a), (b, args.b)):
        if not record.get("ok"):
            problems.append("%s records a failed run" % path)

    if problems:
        print("\n".join(problems))
        print("\n%d difference(s) between %s and %s" % (len(problems), a["platform"], b["platform"]))
        return 1
    print("identical: trimmed reads, %d assembly files, contig headers and self-test answers (%s vs %s)"
          % (len(fa), a["platform"], b["platform"]))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--engines", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--work", default=os.path.join(REPO, "build", "smoke"))
    run.add_argument("--threads", type=int, default=4)
    compare = sub.add_parser("compare")
    compare.add_argument("a")
    compare.add_argument("b")
    args = parser.parse_args(argv)
    return cmd_run(args) if args.command == "run" else cmd_compare(args)


if __name__ == "__main__":
    sys.exit(main())
