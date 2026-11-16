#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Proof that the ported scepter behaves exactly like the reference build.

    python3 engines/win/scepter/prove.py all
    python3 engines/win/scepter/prove.py suites  --staged BIN --stage DIR
    python3 engines/win/scepter/prove.py compare --staged BIN --reference BIN

``suites``   runs upstream's own test scripts (tests/run_tests.sh, tests/check_flags.sh,
             taken from the staged tree) against the staged program. The program is
             copied beside a copy of ``data/`` first, because some checks find the data
             file from the program's own folder.
``compare``  makes two synthetic read sets with tests/tools/gen.py (one with MiSeq-style
             read names, one with NovaSeq-style names so that poly-G trimming switches
             itself on), trims each with both programs using the flags the app passes,
             and requires: identical decompressed reads and an identical JSON report
             apart from the run-specific fields at ``-w 1``; identical reads at ``-w 8``.
             Then one set again as plain FASTQ with a small detection sample, so that
             the sampler seeks: that seek is the one line the port changes (T1), and
             gzipped input never reaches it.
             The ``.gz`` bytes are not compared: zlib stamps the operating system in
             every gzip header, so they differ between Linux and Windows by design.

Defaults: staged ``build/engines/staged-linux/scepter``, reference
``build/engines/reference-linux/scepter``, staged tree ``build/stage/linux/scepter``,
logs in ``build/logs/scepter``. Exit status 0 only when everything asked for passed.

Standard library only. Small on purpose: a 200 kb genome, about 27 000 read pairs per
set, one program run at a time.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
EXE = ".exe" if os.name == "nt" else ""
GEN = REPO / "tests" / "tools" / "gen.py"

#: The flags the app passes for the standard ticks (audit section B3).
STANDARD_FLAGS = [
    "--no-color", "--no-summary", "--verbose",
    "--cut_tail", "--cut_tail_mean_quality", "10",
    "--qualified_quality_phred", "15", "--unqualified_percent_limit", "40", "--n_base_limit", "5",
    "--average_qual", "15", "--length_required", "50",
]
#: JSON leaves that differ between any two runs of the same program (audit section A7).
RUN_SPECIFIC = {
    "command", "scepter.command", "scepter.started_at", "scepter.duration_seconds",
    "scepter.detection.sample_seconds",
}
ADAPTER_R1 = "AGATCGGAAGAGCACACGTCTGAACTCCAGTCA"
ADAPTER_R2 = "AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGT"
READ_LENGTH = 150
COMPLEMENT = str.maketrans("ACGTN", "TGCAN")


class Log:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.handle = open(path, "w", encoding="utf-8", newline="\n")
        self.failed: List[str] = []

    def say(self, text: str = "") -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        self.handle.write(text + "\n")
        self.handle.flush()

    def check(self, ok: bool, what: str) -> bool:
        self.say("  %s  %s" % ("ok  " if ok else "FAIL", what))
        if not ok:
            self.failed.append(what)
        return ok

    def close(self) -> None:
        self.handle.close()


# ---------------------------------------------------------------------------------------
# upstream suites
# ---------------------------------------------------------------------------------------

def run_suites(staged: Path, stage_tree: Path, work: Path, logs: Path, log: Log) -> None:
    bash = shutil.which("bash")
    if bash is None:
        log.check(False, "bash is needed to run the upstream test scripts")
        return
    root = work / "suite"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copy2(staged, root / ("scepter" + EXE))
    shutil.copytree(stage_tree / "data", root / "data")
    shutil.copytree(stage_tree / "tests", root / "tests")
    tmp = work / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TMPDIR=str(tmp))
    env.pop("SCEPTER", None)
    env.pop("SCEPTER_PORT_SELFTEST", None)

    log.say("upstream suites against %s" % staged)
    runs = (
        ("run_tests.sh", [bash, "tests/run_tests.sh"], root,
         re.compile(r"total (\d+)\s+passed (\d+)\s+failed (\d+)")),
        ("check_flags.sh", [bash, str(root / "tests" / "check_flags.sh"), str(root / ("scepter" + EXE))],
         work / "flags", re.compile(r"(\d+) passed, (\d+) failed")),
    )
    for name, cmd, cwd, pattern in runs:
        cwd.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, errors="replace", timeout=3600)
        out_path = logs / ("upstream-%s.log" % name.replace(".sh", ""))
        out_path.write_text(proc.stdout, encoding="utf-8")
        found = pattern.findall(proc.stdout)
        numbers = [int(n) for n in found[-1]] if found else []
        passed, failed = (numbers[-2], numbers[-1]) if numbers else (0, -1)
        log.check(proc.returncode == 0 and failed == 0 and passed > 0,
                  "%s: %d passed, %d failed, exit %d (%s)"
                  % (name, passed, failed, proc.returncode, out_path.relative_to(REPO)
                     if REPO in out_path.parents else out_path))


# ---------------------------------------------------------------------------------------
# synthetic read sets
# ---------------------------------------------------------------------------------------

def _records(path: Path) -> Iterator[Tuple[str, str, str]]:
    with gzip.open(path, "rt", encoding="ascii", newline="\n") as handle:
        while True:
            name = handle.readline().rstrip("\n")
            if not name:
                return
            seq = handle.readline().rstrip("\n")
            handle.readline()
            qual = handle.readline().rstrip("\n")
            yield name[1:], seq, qual


def _qualities(rng: random.Random, decay: Dict[float, str]) -> str:
    """Good bases, then a tail that decays at one of a few speeds."""
    good = int(rng.triangular(60, READ_LENGTH, READ_LENGTH))
    return ("F" * good + decay[rng.choice((0.5, 1.0, 2.0, 4.0))])[:READ_LENGTH]


def decorate(src1: Path, src2: Path, dst1: Path, dst2: Path, style: str, seed: int) -> int:
    """Turn gen.py's clean pairs into something a trimmer has work to do on.

    Illumina read names of the given instrument style, quality that decays towards the
    3' end, short inserts that read through into the adapter, poly-G tails, a few N
    bases, some hopeless reads and some duplicates. Deterministic for a given seed.
    """
    rng = random.Random(seed)
    decay = {slope: "".join(chr(33 + max(2, int(36 - k * slope))) for k in range(READ_LENGTH))
             for slope in (0.5, 1.0, 2.0, 4.0)}
    written = 0
    with gzip.open(dst1, "wt", encoding="ascii", newline="\n", compresslevel=4) as out1, \
            gzip.open(dst2, "wt", encoding="ascii", newline="\n", compresslevel=4) as out2:

        def emit(seq1: str, qual1: str, seq2: str, qual2: str) -> None:
            nonlocal written
            x, y = 1000 + rng.randrange(30000), 1000 + rng.randrange(30000)
            if style == "novaseq":
                stem = "A01234:77:HXXXXXXXX:%d:%d:%d:%d" % (1 + written % 4, 1101 + (written // 97) % 78, x, y)
                tail = ":N:0:ACGTACGT+TGCATGCA"
            else:
                stem = "M01234:55:000000000-ABCDE:1:%d:%d:%d" % (1101 + (written // 97) % 14, x, y)
                tail = ":N:0:1"
            out1.write("@%s 1%s\n%s\n+\n%s\n" % (stem, tail, seq1, qual1))
            out2.write("@%s 2%s\n%s\n+\n%s\n" % (stem, tail, seq2, qual2))
            written += 1

        for (_n1, seq1, _q1), (_n2, seq2, _q2) in zip(_records(src1), _records(src2)):
            seq1, seq2 = seq1[:READ_LENGTH], seq2[:READ_LENGTH]
            qual1, qual2 = _qualities(rng, decay), _qualities(rng, decay)
            kind = rng.random()
            if kind < 0.10:                       # insert shorter than the read
                insert = seq1[:rng.randint(40, 140)]
                filler = "G" * READ_LENGTH if style == "novaseq" else \
                    "".join(rng.choice("ACGT") for _ in range(READ_LENGTH))
                seq1 = (insert + ADAPTER_R1 + filler)[:READ_LENGTH]
                seq2 = (insert.translate(COMPLEMENT)[::-1] + ADAPTER_R2 + filler)[:READ_LENGTH]
            elif kind < 0.15:                     # the signal ran out: a tail of G
                keep = rng.randint(60, 130)
                seq2 = seq2[:keep] + "G" * (READ_LENGTH - keep)
            elif kind < 0.17:                     # a few N
                at = rng.randrange(len(seq1) - 3)
                seq1 = seq1[:at] + "NN" + seq1[at + 2:]
            elif kind < 0.19:                     # a hopeless read 2
                qual2 = "#" * len(seq2)
            qual1, qual2 = qual1[:len(seq1)], qual2[:len(seq2)]
            emit(seq1, qual1, seq2, qual2)
            if rng.random() < 0.03:               # an optical or PCR duplicate
                emit(seq1, qual1, seq2, qual2)
    return written


def make_read_sets(work: Path, log: Log) -> Dict[str, Tuple[Path, Path, int]]:
    data = work / "data"
    data.mkdir(parents=True, exist_ok=True)
    genome = data / "genome.fa"
    subprocess.run([sys.executable, str(GEN), "genome", str(genome), "11", "200000"], check=True,
                   stdout=subprocess.DEVNULL)
    sets: Dict[str, Tuple[Path, Path, int]] = {}
    for name, seed in (("miseq", 21), ("novaseq", 22)):
        prefix = data / ("raw_" + name)
        subprocess.run([sys.executable, str(GEN), "reads", str(genome), str(prefix), "40", str(READ_LENGTH),
                        "320", "60", "0.003", str(seed), "paired", "fastq", "gz"], check=True,
                       stdout=subprocess.DEVNULL)
        raw1, raw2 = Path(str(prefix) + "_1.fq.gz"), Path(str(prefix) + "_2.fq.gz")
        r1, r2 = data / ("%s_R1.fastq.gz" % name), data / ("%s_R2.fastq.gz" % name)
        pairs = decorate(raw1, raw2, r1, r2, name, seed)
        raw1.unlink()
        raw2.unlink()
        sets[name] = (r1, r2, pairs)
        log.say("read set %-8s %d pairs, %s + %s bytes" % (name, pairs, r1.stat().st_size, r2.stat().st_size))
    return sets


# ---------------------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------------------

def reads_digest(path: Path) -> Tuple[str, int, int]:
    """sha256 of the decompressed reads, number of records, number of bases."""
    digest, lines, bases = hashlib.sha256(), 0, 0
    with gzip.open(path, "rb") as handle:           # reads every member of a multi-member file
        for line in handle:
            digest.update(line)
            if lines % 4 == 1:
                bases += len(line) - 1
            lines += 1
    return digest.hexdigest(), lines // 4, bases


def leaves(node, prefix: str = "") -> Iterator[Tuple[str, object]]:
    if isinstance(node, dict):
        for key in node:
            yield from leaves(node[key], prefix + "." + key if prefix else key)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from leaves(item, "%s[%d]" % (prefix, index))
    else:
        yield prefix, node


def trim(program: Path, r1: Path, r2: Path, out: Path, sample: str, threads: int,
         extra: Sequence[str] = ()) -> dict:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    cmd = [str(program), "-i", str(r1), "-I", str(r2),
           "-o", "%s_trimmed_R1.fastq.gz" % sample, "-O", "%s_trimmed_R2.fastq.gz" % sample,
           "-j", "trimming_report.json", "-h", "trimming_report.html", "-R", sample,
           "-w", str(threads)] + STANDARD_FLAGS + list(extra)
    env = dict(os.environ)
    env.pop("SCEPTER_PORT_SELFTEST", None)
    proc = subprocess.run(cmd, cwd=str(out), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=3600)
    result = {"exit": proc.returncode, "stdout": proc.stdout,
              "stderr": proc.stderr.decode("utf-8", "replace"), "out": out}
    if proc.returncode == 0:
        result["r1"] = reads_digest(out / ("%s_trimmed_R1.fastq.gz" % sample))
        result["r2"] = reads_digest(out / ("%s_trimmed_R2.fastq.gz" % sample))
        with open(out / "trimming_report.json", encoding="utf-8") as handle:
            result["json"] = json.load(handle)
        result["html_bytes"] = (out / "trimming_report.html").stat().st_size
    return result


def compare(staged: Path, reference: Path, work: Path, logs: Path, log: Log) -> None:
    log.say("staged     %s\nreference  %s" % (staged, reference))
    sets = make_read_sets(work, log)
    summary: Dict[str, dict] = {}
    for name, (r1, r2, pairs) in sets.items():
        raw_bases = reads_digest(r1)[2] + reads_digest(r2)[2]
        log.say("%s, -w 1" % name)
        a = trim(staged, r1, r2, work / "run" / name / "staged-w1", name, 1)
        b = trim(reference, r1, r2, work / "run" / name / "reference-w1", name, 1)
        if not log.check(a["exit"] == 0 and b["exit"] == 0,
                         "both programs exit 0 (staged %d, reference %d)" % (a["exit"], b["exit"])):
            log.say(a["stderr"][-2000:] + b["stderr"][-2000:])
            continue
        log.check(a["stdout"] == b"" and b["stdout"] == b"", "nothing on standard output")
        alarming = [line for line in a["stderr"].replace("\r", "\n").split("\n")
                    if re.search(r"contam|warning|error|database", line, re.I)]
        log.check(not alarming, "the staged program, run without its contaminant database, prints "
                                "nothing about it%s" % ("" if not alarming else ": " + alarming[0]))
        polyg = "enabling polyG trimming" in a["stderr"]
        trimmed_g = a["json"]["scepter"]["run"]["polyg_trimmed_reads"]
        if name == "novaseq":
            log.check(polyg and trimmed_g > 0, "poly-G trimming switched itself on (%d reads trimmed)" % trimmed_g)
        else:
            log.check(not polyg and trimmed_g == 0, "poly-G trimming stayed off")
        kept = a["r1"][1]
        after_bases = a["r1"][2] + a["r2"][2]
        log.check(0 < kept < pairs and after_bases < raw_bases,
                  "trimming did real work: %d of %d pairs kept, %d of %d bases" % (kept, pairs, after_bases, raw_bases))
        log.check(a["r1"] == b["r1"] and a["r2"] == b["r2"],
                  "decompressed trimmed reads identical (R1 %s, R2 %s)" % (a["r1"][0][:16], a["r2"][0][:16]))
        la, lb = dict(leaves(a["json"])), dict(leaves(b["json"]))
        differing = sorted(key for key in set(la) | set(lb)
                           if la.get(key, "<absent>") != lb.get(key, "<absent>") and key not in RUN_SPECIFIC)
        log.check(not differing, "JSON report identical apart from %d run-specific fields (%d leaves compared)%s"
                  % (len(RUN_SPECIFIC), len(la), "" if not differing else ": differs in " + ", ".join(differing[:8])))
        log.check(all(key in la for key in RUN_SPECIFIC), "every run-specific field is present in the report")
        log.check(la.get("summary.after_filtering.total_reads") == 2 * kept and a["html_bytes"] > 0,
                  "report totals agree with the reads written and the HTML report exists")
        w1 = (a["r1"], a["r2"])
        shutil.rmtree(work / "run" / name)

        log.say("%s, -w 8" % name)
        a = trim(staged, r1, r2, work / "run" / name / "staged-w8", name, 8)
        b = trim(reference, r1, r2, work / "run" / name / "reference-w8", name, 8)
        if log.check(a["exit"] == 0 and b["exit"] == 0, "both programs exit 0"):
            log.check(a["r1"] == b["r1"] and a["r2"] == b["r2"], "decompressed trimmed reads identical at 8 workers")
            log.check((a["r1"], a["r2"]) == w1, "and identical to the reads written with 1 worker")
        shutil.rmtree(work / "run" / name)
        summary[name] = {"pairs_in": pairs, "pairs_out": kept, "bases_in": raw_bases, "bases_out": after_bases,
                         "reads_sha256": {"R1": w1[0][0], "R2": w1[1][0]}, "polyg_trimmed_reads": trimmed_g}
    if "miseq" in sets:
        summary["plain_sampler"] = compare_plain(staged, reference, work, sets["miseq"], log)
    with open(logs / "compare-linux.json", "w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")


def compare_plain(staged: Path, reference: Path, work: Path, read_set: Tuple[Path, Path, int], log: Log) -> dict:
    """The seek of the plain-FASTQ sampler (transformation T1), on both programs.

    Uncompressed input in a folder whose name has a space, an accent, brackets and
    non-Latin characters, with a detection sample much smaller than the file, so the
    sampler jumps to 64 places in it instead of reading it from the start.
    """
    folder = work / "data" / "plain \u00e9 [1] \u65e5\u672c"
    folder.mkdir(parents=True, exist_ok=True)
    plain = []
    for source, name in zip(read_set[:2], ("p R1.fastq", "p R2.fastq")):
        with gzip.open(source, "rb") as src, open(folder / name, "wb") as dst:
            shutil.copyfileobj(src, dst)
        plain.append(folder / name)
    log.say("miseq as plain FASTQ, sample of 2000 reads, -w 1")
    extra = ["--sample-reads", "2000"]
    a = trim(staged, plain[0], plain[1], work / "run" / "plain" / "staged", "plain", 1, extra)
    b = trim(reference, plain[0], plain[1], work / "run" / "plain" / "reference", "plain", 1, extra)
    result: dict = {}
    if log.check(a["exit"] == 0 and b["exit"] == 0,
                 "both programs exit 0 (staged %d, reference %d)" % (a["exit"], b["exit"])):
        strategies = [r["json"]["scepter"]["detection"].get("sample_strategy") for r in (a, b)]
        log.check(strategies == ["stratified", "stratified"] and "stratified sample" in a["stderr"],
                  "the sampler seeked in both (strategy %s)" % "/".join(str(x) for x in strategies))
        log.check(a["r1"] == b["r1"] and a["r2"] == b["r2"],
                  "decompressed trimmed reads identical (R1 %s, R2 %s)" % (a["r1"][0][:16], a["r2"][0][:16]))
        la, lb = dict(leaves(a["json"])), dict(leaves(b["json"]))
        differing = sorted(key for key in set(la) | set(lb)
                           if la.get(key, "<absent>") != lb.get(key, "<absent>") and key not in RUN_SPECIFIC)
        log.check(not differing, "JSON report identical apart from the run-specific fields, the detection "
                                 "numbers included%s" % ("" if not differing else ": differs in " + ", ".join(differing[:8])))
        result = {"pairs_out": a["r1"][1], "reads_sha256": {"R1": a["r1"][0], "R2": a["r2"][0]},
                  "records_scanned": la.get("scepter.detection.records_scanned")}
    else:
        log.say(a["stderr"][-2000:] + b["stderr"][-2000:])
    shutil.rmtree(work / "run" / "plain", ignore_errors=True)
    shutil.rmtree(folder, ignore_errors=True)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("what", choices=("suites", "compare", "all"))
    parser.add_argument("--staged", default=str(REPO / "build" / "engines" / "staged-linux" / ("scepter" + EXE)))
    parser.add_argument("--reference", default=str(REPO / "build" / "engines" / "reference-linux" / ("scepter" + EXE)))
    parser.add_argument("--stage", default=str(REPO / "build" / "stage" / "linux" / "scepter"),
                        help="the staged source tree (for its tests/ and data/)")
    parser.add_argument("--logs", default=str(REPO / "build" / "logs" / "scepter"))
    parser.add_argument("--work", default=None, help="scratch folder (default: a temporary one, removed afterwards)")
    args = parser.parse_args(argv)

    staged, reference = Path(args.staged).resolve(), Path(args.reference).resolve()
    logs = Path(args.logs).resolve()
    work = Path(args.work).resolve() if args.work else Path(tempfile.mkdtemp(prefix="scepter-prove-"))
    work.mkdir(parents=True, exist_ok=True)
    log = Log(logs / ("prove-%s.log" % args.what))
    try:
        if not staged.is_file():
            log.check(False, "staged program not found: %s" % staged)
        elif args.what in ("suites", "all"):
            run_suites(staged, Path(args.stage).resolve(), work, logs, log)
        if staged.is_file() and args.what in ("compare", "all"):
            if not reference.is_file():
                log.check(False, "reference program not found: %s" % reference)
            else:
                compare(staged, reference, work, logs, log)
        log.say("RESULT: %s" % ("PASS" if not log.failed else "FAIL (%d)" % len(log.failed)))
        return 0 if not log.failed else 1
    finally:
        log.close()
        if not args.work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
