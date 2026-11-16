#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Proof that the Windows engines write the same results as the Linux engines.

    python engines/equivalence.py make-corpus DIR
    python engines/equivalence.py fetch-real DIR
    python engines/equivalence.py run DIR --engines DIR --out JSON
    python engines/equivalence.py compare A.json B.json

``make-corpus`` writes a fixed set of small synthetic read sets (tests/tools/gen.py and
tools/make_example.py): the same reads, byte for byte, on every machine and every run.

``fetch-real`` adds one real public isolate, downloaded from the European Nucleotide
Archive over HTTPS and checked against the checksums the archive publishes.

``run`` does to every set what the program does to a sample: trims it with the standard
ticks (one worker thread, so that every figure of the report is reproducible), then
assembles the trimmed reads and, separately, the raw reads with the settings profile of
the Ubuntu ``tesseract`` command, including its second attempt for thin data. It records
a checksum of every output file that does not depend on the run: the decompressed
trimmed reads, the trimming report without its run-specific fields, and the assembly
files listed in ``DETERMINISTIC_FILES``. The record is written whole at the end; a record
of an earlier run at the same place is removed first, so a run that is stopped leaves
none. Each run works in a folder of its own unless ``--work`` names one.

``compare`` requires two such records (one made on Linux, one on Windows) to agree in
every checksum. It prints every difference and exits 1, or says how many files were
compared and exits 0.

Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import http.client
import json
import os
import random
import shutil
import subprocess
import sys
import time
import urllib.request
import zlib
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

GEN = os.path.join(REPO, "tests", "tools", "gen.py")
MAKE_EXAMPLE = os.path.join(REPO, "tools", "make_example.py")
PHIX_SOURCE = os.path.join(REPO, "engines", "tesseract", "upstream", "src", "p2_phix.h")
MANIFEST = "corpus.json"
FORMAT = 1

# --------------------------------------------------------------------------------------
# What the program passes to the engines (tesseract_win/settings_map.py; the tests check
# that these stay equal to it)
# --------------------------------------------------------------------------------------

#: scepter flags behind the standard ticks of the trimming page.
TRIM_FLAGS = ["--no-color", "--no-summary", "--verbose",
              "--cut_tail", "--cut_tail_mean_quality", "10",
              "--qualified_quality_phred", "15", "--unqualified_percent_limit", "40",
              "--n_base_limit", "5", "--average_qual", "15", "--length_required", "50"]
#: The nine variables the Ubuntu ``tesseract`` command sets (tesseract 1.6.2, "PROFILE").
PROFILE = {
    "TESSERACT_P2_LIBGUARD": "1", "TESSERACT_P2_CIRC": "verify", "TESSERACT_P2_SPIKEIN": "1",
    "TESSERACT_P2_ENDS": "1", "TESSERACT_P2_TIPS_LOWERCASE": "1", "TESSERACT_P2_SELF_QA": "1",
    "TESSERACT_P2_PROVENANCE": "1", "TESSERACT_DEEP_NORM": "30", "TESSERACT_DEEP_NORM_MATE": "both",
}
#: The second attempt of the ``tesseract`` command for thin data, and where the first one is kept.
RETRY_KMERS = "21,33,55"
FIRST_ATTEMPT_DIR = "attempt1_default_k"

#: Assembly files that hold nothing of the run that made them (engine audit, section 6.3):
#: no time, no path, no thread count. These must be the same bytes on Linux and Windows.
#: ``report.json``, ``report.html`` and the log carry times and paths and are not compared.
DETERMINISTIC_FILES = ("contigs.fasta", "scaffolds.fasta", "scaffolds.agp", "assembly_graph.gfa",
                       "replicons.tsv", "ends.tsv", "p2_edits.tsv", "p2_self_qa.*")
#: Fields of the trimming report that differ between any two runs, with the reason
#: (scepter audit, section A7). Everything else is reproducible at one worker thread.
REPORT_RUN_SPECIFIC = {
    "command": "the command line, with the folder of the program and of the reads",
    "scepter.command": "the same command line",
    "scepter.started_at": "the time of the run",
    "scepter.duration_seconds": "how long the run took",
    "scepter.detection.sample_seconds": "how long the adapter detection took",
    "scepter.config.in1": "where the reads are on this machine",
    "scepter.config.in2": "where the reads are on this machine",
}

# --------------------------------------------------------------------------------------
# The real isolate. Chosen on 2026-11-16 from the file report of the European Nucleotide
# Archive:
#   https://www.ebi.ac.uk/ena/portal/api/filereport?accession=SRR30504922&result=read_run
#       &fields=fastq_ftp,fastq_md5,fastq_bytes,scientific_name,library_layout,...
# which answered: Campylobacter jejuni (taxon 197), PAIRED, WGS, GENOMIC, Illumina MiSeq,
# 194,829 read pairs, 95,253,164 bases (2 x 250; about 55 times a 1.7 Mb genome), study
# PRJNA239251, sample SAMN43446785, public since 2024-09-02. Every value below is copied
# from that answer; nothing here is made up. 64 MB to download.
# --------------------------------------------------------------------------------------
REAL_ISOLATE = {
    "accession": "SRR30504922",
    "organism": "Campylobacter jejuni",
    "read_pairs": 194829,
    "bases": 95253164,
    "files": [
        {"mate": "R1",
         "url": "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR305/022/SRR30504922/SRR30504922_1.fastq.gz",
         "md5": "b60b4b2b3b75ab257ac0f5cfd6a666ac", "bytes": 28769754},
        {"mate": "R2",
         "url": "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR305/022/SRR30504922/SRR30504922_2.fastq.gz",
         "md5": "7d2a242fa320c56cb60897b73578111b", "bytes": 35731091},
    ],
}
REAL_SET = "real_" + REAL_ISOLATE["accession"]


def say(text: str = "") -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


class Problem(Exception):
    """Something that stops the command, said in words."""


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reads_digest(path: str) -> Tuple[str, int]:
    """sha256 of the decompressed reads and the number of records.

    The decompressed data, because zlib stamps the operating system into every gzip
    header: the ``.gz`` bytes differ between Linux and Windows by design.
    """
    digest, lines = hashlib.sha256(), 0
    with gzip.open(path, "rb") as handle:           # reads every member of the file
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
            lines += block.count(b"\n")
    return digest.hexdigest(), lines // 4


def write_gz(path: str, data: bytes) -> None:
    """A gzip file with nothing of this machine in it: no time, no name, LF line ends."""
    data = data.replace(b"\r\n", b"\n")
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0) as handle:
            handle.write(data)


def write_json(path: str, data: dict) -> None:
    """Write ``path`` whole or not at all: an interrupted write leaves no half record."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    partial = str(path) + ".part"
    with open(partial, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
        handle.write("\n")
    os.replace(partial, path)


def read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise Problem("cannot read %s: %s" % (path, exc.strerror or exc))
    except ValueError as exc:
        raise Problem("%s is not a readable record (%s). Make it again." % (path, exc))
    if not isinstance(data, dict) or not isinstance(data.get("sets", {}), dict):
        raise Problem("%s is not a readable record (it does not hold what this script writes). Make it again."
                      % path)
    return data


def exe(folder: str, name: str) -> str:
    return os.path.join(folder, name + (".exe" if os.name == "nt" else ""))


def remove_tree(path: str) -> None:
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------------------
# make-corpus
# --------------------------------------------------------------------------------------

def _gen(*args: str) -> str:
    """Run one sub-command of tests/tools/gen.py; return what it printed."""
    done = subprocess.run([sys.executable, GEN, *[str(a) for a in args]], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True)
    if done.returncode != 0:
        raise Problem("the read generator failed (gen.py %s):\n%s" % (" ".join(str(a) for a in args[:1]),
                                                                       done.stdout))
    return done.stdout.strip()


def _slurp(path: str) -> bytes:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as handle:
        return handle.read()


def _pack(tmp_prefix: str, out1: str, out2: str) -> None:
    """Turn the pair gen.py wrote (plain or gzipped) into our reproducible gzip files."""
    for mate, out in (("1", out1), ("2", out2)):
        source = next((p for p in ("%s_%s.fq" % (tmp_prefix, mate), "%s_%s.fq.gz" % (tmp_prefix, mate))
                       if os.path.isfile(p)), None)
        if source is None:
            raise Problem("the read generator wrote no %s_%s.fq" % (tmp_prefix, mate))
        write_gz(out, _slurp(source))
        os.remove(source)


def _plain_reads(tmp: str, genome_args: Sequence[str], reads_args: Sequence[str], out1: str, out2: str) -> None:
    genome = os.path.join(tmp, "genome.fa")
    _gen(*genome_args[:1], genome, *genome_args[1:])
    prefix = os.path.join(tmp, "reads")
    _gen("reads", genome, prefix, *reads_args, "paired", "fastq", "plain")
    _pack(prefix, out1, out2)


def _set_plain200k(tmp: str, out1: str, out2: str) -> None:
    _plain_reads(tmp, ["genome", "101", "200000"], ["60", "150", "350", "40", "0.005", "201"], out1, out2)


def _set_circular_phix(tmp: str, out1: str, out2: str) -> None:
    if not os.path.isfile(PHIX_SOURCE):
        raise Problem("the PhiX sequence is read from %s, which is missing" % PHIX_SOURCE)
    _plain_reads(tmp, ["circgenome", "102", "600", PHIX_SOURCE, "150000:1", "6000:12", "3000:30"],
                 ["40", "150", "350", "40", "0.002", "202"], out1, out2)


def _set_repeat_dropout(tmp: str, out1: str, out2: str) -> None:
    genome = os.path.join(tmp, "genome.fa")
    _gen("repeat_genome", genome, "103", "40000", "1200")
    prefix = os.path.join(tmp, "reads")
    # No read touches bases 60,000 to 60,260; the 600-base fragments still span them, so the
    # assembler orders the two sides and writes a gap between them (scaffolds.fasta, scaffolds.agp).
    _gen("holereads", genome, prefix, "50", "150", "600", "40", "203", "60000", "60260")
    _pack(prefix, out1, out2)


def _set_replicons_250(tmp: str, out1: str, out2: str) -> None:
    _plain_reads(tmp, ["genome", "104", "120000", "60000", "20000"],
                 ["25", "250", "500", "50", "0.01", "204"], out1, out2)


def _set_low_depth(tmp: str, out1: str, out2: str) -> None:
    # Five reads deep: many short contigs, joined across gaps where read pairs allow.
    _plain_reads(tmp, ["genome", "105", "150000"], ["5", "150", "350", "40", "0.005", "205"], out1, out2)


def _set_very_thin(tmp: str, out1: str, out2: str) -> None:
    # 300 read pairs for 300 kb. The default k sizes assemble nothing and the log says
    # "k-mer depth 0.0x", which is what makes the tesseract command try again with k = 21,33,55.
    _plain_reads(tmp, ["genome", "107", "300000"], ["0.3", "150", "350", "30", "0.005", "207"], out1, out2)


_COMPLEMENT = bytes.maketrans(b"ACGTN", b"TGCAN")
_ADAPTER_R1 = b"AGATCGGAAGAGCACACGTCTGAACTCCAGTCA"
_ADAPTER_R2 = b"AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGT"


def _records(data: bytes) -> Iterator[bytes]:
    lines = data.replace(b"\r\n", b"\n").split(b"\n")
    for i in range(0, len(lines) - 3, 4):
        yield lines[i + 1]


def _set_novaseq(tmp: str, out1: str, out2: str) -> None:
    """Reads named as a NovaSeq names them, with work for a trimmer in them.

    scepter reads the instrument from the read names and switches poly-G trimming on by
    itself for two-colour instruments. Quality falls towards the 3' end, some inserts are
    shorter than a read (adapter, then G to the end), some reads end in G because the
    signal ran out, a few carry N, a few are hopeless, a few are duplicates.
    """
    genome = os.path.join(tmp, "genome.fa")
    _gen("genome", genome, "106", "150000")
    prefix = os.path.join(tmp, "reads")
    _gen("reads", genome, prefix, "40", "150", "320", "60", "0.003", "206", "paired", "fastq", "plain")
    length = 150
    rng = random.Random(306)
    decay = {slope: bytes(33 + max(2, int(36 - k * slope)) for k in range(length))
             for slope in (0.5, 1.0, 2.0, 4.0)}

    def qualities() -> bytes:
        good = int(rng.triangular(60, length, length))
        return (b"F" * good + decay[rng.choice((0.5, 1.0, 2.0, 4.0))])[:length]

    parts1: List[bytes] = []
    parts2: List[bytes] = []
    written = 0

    def emit(seq1: bytes, qual1: bytes, seq2: bytes, qual2: bytes) -> None:
        nonlocal written
        x, y = 1000 + rng.randrange(30000), 1000 + rng.randrange(30000)
        stem = b"@A01234:77:HXXXXXXXX:%d:%d:%d:%d" % (1 + written % 4, 1101 + (written // 97) % 78, x, y)
        tail = b":N:0:ACGTACGT+TGCATGCA\n"
        parts1.append(stem + b" 1" + tail + seq1 + b"\n+\n" + qual1 + b"\n")
        parts2.append(stem + b" 2" + tail + seq2 + b"\n+\n" + qual2 + b"\n")
        written += 1

    raw1, raw2 = _slurp(prefix + "_1.fq"), _slurp(prefix + "_2.fq")
    for seq1, seq2 in zip(_records(raw1), _records(raw2)):
        seq1, seq2 = seq1[:length], seq2[:length]
        qual1, qual2 = qualities(), qualities()
        kind = rng.random()
        if kind < 0.10:                       # the insert is shorter than the read
            insert = seq1[:rng.randint(40, 140)]
            filler = b"G" * length
            seq1 = (insert + _ADAPTER_R1 + filler)[:length]
            seq2 = (insert.translate(_COMPLEMENT)[::-1] + _ADAPTER_R2 + filler)[:length]
        elif kind < 0.15:                     # the signal ran out: a tail of G
            keep = rng.randint(60, 130)
            seq2 = seq2[:keep] + b"G" * (length - keep)
        elif kind < 0.17:                     # a few N
            at = rng.randrange(len(seq1) - 3)
            seq1 = seq1[:at] + b"NN" + seq1[at + 2:]
        elif kind < 0.19:                     # a hopeless read 2
            qual2 = b"#" * len(seq2)
        qual1, qual2 = qual1[:len(seq1)], qual2[:len(seq2)]
        emit(seq1, qual1, seq2, qual2)
        if rng.random() < 0.03:               # a duplicate
            emit(seq1, qual1, seq2, qual2)
    write_gz(out1, b"".join(parts1))
    write_gz(out2, b"".join(parts2))
    os.remove(prefix + "_1.fq")
    os.remove(prefix + "_2.fq")


def _set_example(tmp: str, out1: str, out2: str) -> None:
    done = subprocess.run([sys.executable, MAKE_EXAMPLE, tmp], stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True)
    if done.returncode != 0:
        raise Problem("tools/make_example.py failed:\n%s" % done.stdout)
    shutil.move(os.path.join(tmp, "Example_isolate_R1.fastq.gz"), out1)
    shutil.move(os.path.join(tmp, "Example_isolate_R2.fastq.gz"), out2)


#: name -> (maker, file stem, what it is). "run" takes the sets in alphabetical order.
SYNTHETIC_SETS = {
    "plain200k": (_set_plain200k, "plain200k",
                  "one 200 kb chromosome, 60 times deep, 0.5% errors (engine audit, set d1)"),
    "circular_phix": (_set_circular_phix, "circular_phix",
                      "a circular 150 kb chromosome, a 6 kb plasmid at 12 copies, a 3 kb plasmid at 30 "
                      "copies, and PhiX (engine audit, set d2)"),
    "repeat_dropout": (_set_repeat_dropout, "repeat_dropout",
                       "a genome with one sequence repeated twice and 260 bases that no read covers "
                       "but read pairs span (engine audit, set d3)"),
    "replicons_250": (_set_replicons_250, "replicons_250",
                      "three replicons, 2 x 250 reads, 25 times deep, 1% errors (engine audit, set d4)"),
    "low_depth": (_set_low_depth, "low_depth",
                  "150 kb only five reads deep: many short contigs and scaffold gaps"),
    "very_thin": (_set_very_thin, "very_thin",
                  "300 kb at a third of a read deep: the first attempt assembles nothing and the "
                  "second attempt with k = 21,33,55 is made"),
    "novaseq": (_set_novaseq, "novaseq",
                "150 kb with NovaSeq read names, falling quality, adapters and G tails: poly-G "
                "trimming switches itself on"),
    "example": (_set_example, "Example_isolate",
                "the example that ships with the program (tools/make_example.py)"),
}


def load_manifest(corpus: str) -> dict:
    path = os.path.join(corpus, MANIFEST)
    if not os.path.isfile(path):
        raise Problem("%s holds no read sets (no %s). Run: python engines/equivalence.py make-corpus %s"
                      % (corpus, MANIFEST, corpus))
    manifest = read_json(path)
    if manifest.get("format") != FORMAT or not isinstance(manifest.get("sets"), dict):
        raise Problem("%s was written by another version of this script. Make the read sets again." % path)
    return manifest


def _describe(corpus: str, name: str, rel1: str, rel2: str, kind: str, about: str) -> dict:
    sha1, pairs1 = reads_digest(os.path.join(corpus, rel1))
    sha2, pairs2 = reads_digest(os.path.join(corpus, rel2))
    if pairs1 != pairs2 or pairs1 == 0:
        raise Problem("read set %s is not a pair: %d reads in R1, %d in R2" % (name, pairs1, pairs2))
    return {"kind": kind, "about": about, "R1": rel1, "R2": rel2, "pairs": pairs1,
            "sha256": {"R1": sha1, "R2": sha2},
            "gz_bytes": {"R1": os.path.getsize(os.path.join(corpus, rel1)),
                         "R2": os.path.getsize(os.path.join(corpus, rel2))}}


def cmd_make_corpus(args) -> int:
    corpus = os.path.abspath(args.dir)
    wanted = _chosen(args.sets, SYNTHETIC_SETS)
    os.makedirs(corpus, exist_ok=True)
    sets: Dict[str, dict] = {}
    try:                                         # a real isolate fetched earlier stays
        for name, entry in load_manifest(corpus)["sets"].items():
            if entry.get("kind") == "real" and all(os.path.isfile(os.path.join(corpus, entry[m]))
                                                   for m in ("R1", "R2")):
                sets[name] = entry
    except Problem:
        pass
    total = 0
    for name in wanted:
        maker, stem, about = SYNTHETIC_SETS[name]
        folder = os.path.join(corpus, name)
        tmp = os.path.join(corpus, ".making-" + name)
        remove_tree(folder)
        remove_tree(tmp)
        os.makedirs(folder)
        os.makedirs(tmp)
        rel1, rel2 = "%s/%s_R1.fastq.gz" % (name, stem), "%s/%s_R2.fastq.gz" % (name, stem)
        try:
            maker(tmp, os.path.join(corpus, rel1), os.path.join(corpus, rel2))
        finally:
            remove_tree(tmp)
        sets[name] = _describe(corpus, name, rel1, rel2, "synthetic", about)
        size = sum(sets[name]["gz_bytes"].values())
        total += size
        say("%-16s %7d read pairs  %5.1f MB  %s" % (name, sets[name]["pairs"], size / 1e6,
                                                    sets[name]["sha256"]["R1"][:16]))
    write_json(os.path.join(corpus, MANIFEST), {"format": FORMAT, "sets": sets})
    say("%d read sets in %s (%.1f MB)" % (len(wanted), corpus, total / 1e6))
    return 0


def _chosen(text: Optional[str], known: Iterable[str]) -> List[str]:
    known = list(known)
    if not text:
        return known
    names = [n.strip() for n in text.split(",") if n.strip()]
    unknown = [n for n in names if n not in known]
    if unknown:
        raise Problem("no read set is called %s. The sets are: %s" % (", ".join(unknown), ", ".join(known)))
    return names


# --------------------------------------------------------------------------------------
# fetch-real
# --------------------------------------------------------------------------------------

def md5_file(path: str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, target: str, size: int, checksum: str, attempts: int = 4) -> None:
    if not url.startswith("https://"):
        raise Problem("refusing to download %s: only HTTPS is used" % url)
    partial = target + ".part"
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "equivalence-corpus"})
            with urllib.request.urlopen(request, timeout=120) as response, open(partial, "wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
            got = os.path.getsize(partial)
            if got != size:
                last = "the download holds %d bytes, the archive says %d" % (got, size)
            elif md5_file(partial) != checksum:
                last = "the checksum of the download is not the one the archive publishes"
            else:
                os.replace(partial, target)
                return
        except (OSError, ValueError, http.client.HTTPException) as exc:     # also a transfer cut short
            last = str(exc) or type(exc).__name__
        say("  attempt %d of %d failed: %s" % (attempt, attempts, last))
        time.sleep(5 * attempt)
    if os.path.exists(partial):
        os.remove(partial)
    raise Problem("could not download %s (%s). Nothing was added to the read sets. Check the "
                  "network and run the command again." % (url, last))


def cmd_fetch_real(args) -> int:
    corpus = os.path.abspath(args.dir)
    os.makedirs(os.path.join(corpus, REAL_SET), exist_ok=True)
    try:
        manifest = load_manifest(corpus)
    except Problem:
        manifest = {"format": FORMAT, "sets": {}}
    rel = {}
    for item in REAL_ISOLATE["files"]:
        rel[item["mate"]] = "%s/%s_%s.fastq.gz" % (REAL_SET, REAL_ISOLATE["accession"], item["mate"])
        target = os.path.join(corpus, rel[item["mate"]])
        if os.path.isfile(target) and os.path.getsize(target) == item["bytes"] and md5_file(target) == item["md5"]:
            say("%s is already here and its checksum is right" % rel[item["mate"]])
            continue
        say("downloading %s (%.0f MB) ..." % (item["url"], item["bytes"] / 1e6))
        _download(item["url"], target, item["bytes"], item["md5"])
        say("  checksum as published by the archive: %s" % item["md5"])
    about = ("%s, run %s of the European Nucleotide Archive: %d read pairs"
             % (REAL_ISOLATE["organism"], REAL_ISOLATE["accession"], REAL_ISOLATE["read_pairs"]))
    entry = _describe(corpus, REAL_SET, rel["R1"], rel["R2"], "real", about)
    if entry["pairs"] != REAL_ISOLATE["read_pairs"]:
        raise Problem("%s holds %d read pairs but the archive reports %d"
                      % (REAL_SET, entry["pairs"], REAL_ISOLATE["read_pairs"]))
    manifest["sets"][REAL_SET] = entry
    write_json(os.path.join(corpus, MANIFEST), manifest)
    say("%s added: %d read pairs" % (REAL_SET, entry["pairs"]))
    return 0


# --------------------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------------------

def clean_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The environment the program gives an engine: no TESSERACT_* of ours, no self-test."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("TESSERACT_")
           and k.upper() not in ("TS_COMPAT_SELFTEST", "SCEPTER_PORT_SELFTEST", "NO_COLOR")}
    env.update(extra or {})
    env["NO_COLOR"] = "1"
    return env


def run_logged(argv: Sequence[str], cwd: str, env: Dict[str, str], log_path: str) -> int:
    with open(log_path, "wb") as log:
        try:
            return subprocess.run(list(argv), cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT).returncode
        except OSError as exc:
            log.write(("could not start %s: %s\n" % (argv[0], exc)).encode("utf-8", "replace"))
            return 127


def _canonical(value) -> str:
    return sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                        .encode("ascii"))


def report_digest(report: dict) -> dict:
    """Checksums of a trimming report with its run-specific fields taken out.

    One checksum for the whole report and one per section, so that a difference can be
    named. ``missing`` lists run-specific fields that were not there to take out.
    """
    cleaned = json.loads(json.dumps(report))
    missing = []
    for dotted in sorted(REPORT_RUN_SPECIFIC):
        node, keys = cleaned, dotted.split(".")
        for key in keys[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and keys[-1] in node:
            del node[keys[-1]]
        else:
            missing.append(dotted)
    sections = {}
    for key, value in cleaned.items():
        if key == "scepter" and isinstance(value, dict):
            for inner, inner_value in value.items():
                sections["scepter." + inner] = _canonical(inner_value)
        else:
            sections[key] = _canonical(value)
    return {"sha256": _canonical(cleaned), "sections": sections, "missing": missing}


def _file_facts(path: str) -> dict:
    with open(path, "rb") as handle:
        data = handle.read()
    return {"sha256": sha256_bytes(data), "bytes": len(data), "crlf": data.count(b"\r\n")}


def _is_deterministic(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in DETERMINISTIC_FILES)


def _listing(folder: str) -> List[str]:
    found = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            found.append(os.path.relpath(os.path.join(root, name), folder).replace(os.sep, "/"))
    return sorted(found)


def _contig_headers(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    with open(path, "rb") as handle:
        return [line.strip().decode("ascii", "replace") for line in handle if line.startswith(b">")]


def _retry_needed(out_dir: str, exit_code: int) -> Tuple[bool, str]:
    """The rule of the tesseract command for its second attempt (tesseract_win.postflight)."""
    from tesseract_win import postflight
    decision = postflight.thin_retry_needed(out_dir, exit_code=exit_code, engine_args=(), genome_size_mb=None)
    return bool(decision), decision.message


def _keep_first_attempt(out_dir: str) -> None:
    from tesseract_win import postflight
    postflight.keep_first_attempt(out_dir)


def assemble(asm: str, sketch: str, read1: str, read2: str, work: str, threads: int) -> dict:
    """One assembly as the program runs it; returns what was written, by checksum."""
    remove_tree(work)
    out = os.path.join(work, "assembly")
    os.makedirs(out)
    extra = dict(PROFILE)
    if os.path.isfile(sketch):
        extra["TESSERACT_P2_DETECT_REPORT"] = "1"
        extra["TESSERACT_P2_DETECT_SKETCH"] = sketch
    env = clean_env(extra)
    base = [asm, "-1", read1, "-2", read2, "-o", "assembly", "-t", str(threads), "--no-polish"]
    log = os.path.join(out, "assembly.log")
    started = time.monotonic()
    record: dict = {"exit": run_logged(base, work, env, log), "retried": False}
    retry, message = _retry_needed(out, record["exit"])
    if retry:
        say("      " + message)
        _keep_first_attempt(out)
        record["retried"] = True
        record["first_exit"] = record["exit"]
        record["exit"] = run_logged(base + ["-k", RETRY_KMERS], work, env, log)
    record["seconds"] = round(time.monotonic() - started, 1)
    record["written"] = _listing(out)
    record["files"] = {name: _file_facts(os.path.join(out, name)) for name in record["written"]
                       if _is_deterministic(name.rsplit("/", 1)[-1])}
    headers = _contig_headers(os.path.join(out, "contigs.fasta"))
    record["contigs"] = len(headers)
    record["first_headers"] = headers[:12]
    if record["exit"] != 0:
        with open(log, "rb") as handle:
            record["log_tail"] = handle.read()[-3000:].decode("utf-8", "replace").replace("\r\n", "\n")
    return record


def trim(scepter: str, read1: str, read2: str, folder: str, sample: str) -> dict:
    """Trim one pair with the standard ticks at one worker thread."""
    remove_tree(folder)
    os.makedirs(folder)
    out1, out2 = "%s_trimmed_R1.fastq.gz" % sample, "%s_trimmed_R2.fastq.gz" % sample
    argv = [scepter, "-i", read1, "-I", read2, "-o", out1, "-O", out2, "-j", "trimming_report.json",
            "-h", "trimming_report.html", "-R", sample, "-w", "1", *TRIM_FLAGS]
    started = time.monotonic()
    record: dict = {"exit": run_logged(argv, folder, clean_env(), os.path.join(folder, "trimming.log"))}
    record["seconds"] = round(time.monotonic() - started, 1)
    record["out"] = (os.path.join(folder, out1), os.path.join(folder, out2))
    with open(os.path.join(folder, "trimming.log"), "rb") as handle:
        log = handle.read().decode("utf-8", "replace")
    if record["exit"] != 0:
        record["log_tail"] = log[-3000:].replace("\r\n", "\n")
        return record
    try:
        sha1, pairs1 = reads_digest(record["out"][0])
        sha2, pairs2 = reads_digest(record["out"][1])
        with open(os.path.join(folder, "trimming_report.json"), encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError, EOFError) as exc:
        record["exit"] = 1
        record["log_tail"] = "scepter ended with status 0 but its output cannot be read: %s" % exc
        return record
    summary = report.get("summary", {})
    record.update({
        "reads": {"R1": sha1, "R2": sha2},
        "pairs": {"R1": pairs1, "R2": pairs2},
        "report": report_digest(report),
        "before_reads": summary.get("before_filtering", {}).get("total_reads"),
        "after_reads": summary.get("after_filtering", {}).get("total_reads"),
        "after_bases": summary.get("after_filtering", {}).get("total_bases"),
        "adapter_trimmed_reads": report.get("adapter_cutting", {}).get("adapter_trimmed_reads"),
        "polyg_trimmed_reads": report.get("scepter", {}).get("run", {}).get("polyg_trimmed_reads"),
        "html_written": os.path.getsize(os.path.join(folder, "trimming_report.html")) > 0
        if os.path.isfile(os.path.join(folder, "trimming_report.html")) else False,
    })
    return record


def _version_line(program: str, word: str) -> str:
    try:
        done = subprocess.run([program, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=clean_env())
    except OSError as exc:
        raise Problem("%s cannot be started (%s). Build the engines first: python engines/build.py"
                      % (program, exc.strerror or exc))
    lines = [line.strip() for line in done.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    return next((line for line in lines if word in line), lines[0] if lines else "")


def cmd_run(args) -> int:
    corpus = os.path.abspath(args.dir)
    engines = os.path.abspath(args.engines)
    # A record left by an earlier run must not pass for the result of this one, should this
    # one be stopped or fail before it writes its own.
    for stale in (args.out, str(args.out) + ".part"):
        if os.path.isfile(stale):
            os.remove(stale)
    asm, scepter = exe(engines, "tesseract-asm"), exe(engines, "scepter")
    sketch = os.path.join(engines, "om2detect.sketch")
    for program in (asm, scepter):
        if not os.path.isfile(program):
            raise Problem("%s is missing. Build the engines first: python engines/build.py" % program)
    manifest = load_manifest(corpus)
    names = _chosen(args.sets, manifest["sets"])
    threads = max(1, args.threads or os.cpu_count() or 1)
    # A folder of this run's own unless one is given: two runs at the same time (two engine
    # folders being compared, say) must never read each other's files.
    own_work = os.path.join(REPO, "build", "equivalence-work", "run-%d" % os.getpid())
    work = os.path.abspath(args.work or own_work)
    record = {
        "format": FORMAT, "platform": sys.platform, "threads": threads,
        "versions": {"tesseract-asm": _version_line(asm, "TesserACT"), "scepter": _version_line(scepter, "scepter")},
        "detect_sketch": _file_facts(sketch)["sha256"] if os.path.isfile(sketch) else None,
        "sets": {},
    }
    say("tesseract-asm  %s" % record["versions"]["tesseract-asm"])
    say("scepter        %s" % record["versions"]["scepter"])
    say("%d read sets, %d threads for the assembler" % (len(names), threads))
    ok = True
    for name in names:
        entry = manifest["sets"][name]
        # normpath: the manifest writes "/" on every platform, the engines get the native spelling
        read1 = os.path.normpath(os.path.join(corpus, entry["R1"]))
        read2 = os.path.normpath(os.path.join(corpus, entry["R2"]))
        folder = os.path.join(work, name)
        say("%s (%d read pairs)" % (name, entry["pairs"]))
        result: dict = {"input": {}, "problems": []}
        record["sets"][name] = result
        for mate, path in (("R1", read1), ("R2", read2)):
            if not os.path.isfile(path):
                result["problems"].append("%s is missing" % entry[mate])
                continue
            try:
                result["input"][mate] = reads_digest(path)[0]
            except (OSError, EOFError, zlib.error) as exc:      # cut short, or not gzip at all
                result["problems"].append("%s cannot be read (%s): make the read sets again" % (entry[mate], exc))
                continue
            if result["input"][mate] != entry["sha256"][mate]:
                result["problems"].append("%s is not the file the read sets were made with" % entry[mate])
        if result["problems"]:
            ok = False
            say("  FAILED: " + "; ".join(result["problems"]))
            continue

        trimmed = trim(scepter, read1, read2, os.path.join(folder, "trimming"), name)
        outputs = trimmed.pop("out")
        result["trim"] = trimmed
        if trimmed["exit"] != 0:
            ok = False
            result["problems"].append("trimming ended with status %d" % trimmed["exit"])
            say("  FAILED: trimming ended with status %d\n%s" % (trimmed["exit"], trimmed.get("log_tail", "")))
        else:
            say("  trimmed       %d of %d reads kept, %s poly-G tails cut  (%.1f s)"
                % (trimmed["after_reads"] or 0, trimmed["before_reads"] or 0,
                   trimmed["polyg_trimmed_reads"], trimmed["seconds"]))
        result["assembly"] = {}
        for what, pair in (("trimmed", outputs), ("raw", (read1, read2))):
            if what == "trimmed" and trimmed["exit"] != 0:
                continue
            done = assemble(asm, sketch, pair[0], pair[1], os.path.join(folder, what), threads)
            result["assembly"][what] = done
            if done["exit"] != 0:
                ok = False
                result["problems"].append("the assembly of the %s reads ended with status %d" % (what, done["exit"]))
                say("  FAILED: the assembly of the %s reads ended with status %d\n%s"
                    % (what, done["exit"], done.get("log_tail", "")))
            else:
                say("  %-7s reads  %d contigs, %d files recorded%s  (%.1f s)"
                    % (what, done["contigs"], len(done["files"]),
                       ", second attempt with k = %s" % RETRY_KMERS if done["retried"] else "", done["seconds"]))
        if not args.keep:
            remove_tree(folder)
    if not args.keep and not args.work:
        remove_tree(work)
        try:
            os.rmdir(os.path.dirname(work))         # build/equivalence-work, when no other run is using it
        except OSError:
            pass
    record["ok"] = ok
    write_json(args.out, record)
    say("%s: %s" % ("recorded" if ok else "FAILED (see above); partial record", args.out))
    return 0 if ok else 1


# --------------------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------------------

def compare_records(a: dict, b: dict, name_a: str = "A", name_b: str = "B") -> Tuple[List[str], int]:
    """Every difference between two records, as sentences, and the number of files compared."""
    problems: List[str] = []
    compared = 0

    def differ(what: str, x, y) -> bool:
        if x == y:
            return False
        problems.append("%s differs:\n    %s: %s\n    %s: %s" % (what, name_a, x, name_b, y))
        return True

    for record, label in ((a, name_a), (b, name_b)):
        if record.get("format") != FORMAT:
            problems.append("%s was not written by this version of equivalence.py" % label)
        if not record.get("ok"):
            problems.append("%s records a run in which an engine failed" % label)
    differ("the version of tesseract-asm", a.get("versions", {}).get("tesseract-asm"),
           b.get("versions", {}).get("tesseract-asm"))
    differ("the version of scepter", a.get("versions", {}).get("scepter"), b.get("versions", {}).get("scepter"))
    differ("the organism detection file (om2detect.sketch)", a.get("detect_sketch"), b.get("detect_sketch"))
    sets_a, sets_b = a.get("sets", {}), b.get("sets", {})
    for name in sorted(set(sets_a) ^ set(sets_b)):
        problems.append("read set %s is only in %s" % (name, name_a if name in sets_a else name_b))
    if not (set(sets_a) & set(sets_b)):
        problems.append("the two records have no read set in common: nothing was compared")

    for name in sorted(set(sets_a) & set(sets_b)):
        sa, sb = sets_a[name], sets_b[name]
        for mate in ("R1", "R2"):
            differ("%s: the %s reads that went in (the two runs did not use the same read files)" % (name, mate),
                   sa.get("input", {}).get(mate), sb.get("input", {}).get(mate))
        ta, tb = sa.get("trim", {}), sb.get("trim", {})
        differ("%s: exit status of trimming" % name, ta.get("exit"), tb.get("exit"))
        for mate in ("R1", "R2"):
            if not differ("%s: trimmed %s reads (decompressed)" % (name, mate),
                          ta.get("reads", {}).get(mate), tb.get("reads", {}).get(mate)):
                compared += 1 if ta.get("reads", {}).get(mate) else 0
        ra, rb = ta.get("report", {}), tb.get("report", {})
        if ra.get("sha256") != rb.get("sha256"):
            sections_a, sections_b = ra.get("sections", {}), rb.get("sections", {})
            where = sorted(k for k in set(sections_a) | set(sections_b) if sections_a.get(k) != sections_b.get(k))
            problems.append("%s: the trimming report differs in: %s" % (name, ", ".join(where) or "its structure"))
            for key in ("before_reads", "after_reads", "after_bases", "adapter_trimmed_reads", "polyg_trimmed_reads"):
                differ("%s: trimming report, %s" % (name, key), ta.get(key), tb.get(key))
        elif ra.get("sha256"):
            compared += 1
        differ("%s: run-specific fields missing from the trimming report" % name, ra.get("missing"), rb.get("missing"))
        differ("%s: whether the trimming report in HTML was written" % name, ta.get("html_written"),
               tb.get("html_written"))

        for what in sorted(set(sa.get("assembly", {})) | set(sb.get("assembly", {}))):
            xa, xb = sa.get("assembly", {}).get(what, {}), sb.get("assembly", {}).get(what, {})
            label = "%s, %s reads" % (name, what)
            differ("%s: exit status of the assembler" % label, xa.get("exit"), xb.get("exit"))
            differ("%s: whether the second attempt for thin data was made" % label,
                   xa.get("retried"), xb.get("retried"))
            differ("%s: the files in the assembly folder" % label, xa.get("written"), xb.get("written"))
            fa, fb = xa.get("files", {}), xb.get("files", {})
            for path in sorted(set(fa) | set(fb)):
                if path not in fa or path not in fb:
                    problems.append("%s: %s was written only by %s" % (label, path, name_a if path in fa else name_b))
                elif fa[path]["sha256"] != fb[path]["sha256"]:
                    hint = ""
                    if fa[path].get("crlf") != fb[path].get("crlf"):
                        hint = " (one of them has Windows line ends: %d against %d)" % (fa[path]["crlf"], fb[path]["crlf"])
                    problems.append("%s: %s differs%s:\n    %s: %s, %d bytes\n    %s: %s, %d bytes"
                                    % (label, path, hint, name_a, fa[path]["sha256"], fa[path]["bytes"],
                                       name_b, fb[path]["sha256"], fb[path]["bytes"]))
                else:
                    compared += 1
            if xa.get("first_headers") != xb.get("first_headers"):
                problems.append("%s: the first contigs differ:\n    %s: %s\n    %s: %s"
                                % (label, name_a, "; ".join(xa.get("first_headers", [])[:4]),
                                   name_b, "; ".join(xb.get("first_headers", [])[:4])))
    if compared == 0 and not problems:
        problems.append("the two records hold no checksum to compare: nothing was compared")
    return problems, compared


def cmd_compare(args) -> int:
    a, b = read_json(args.a), read_json(args.b)
    problems, compared = compare_records(a, b, args.a, args.b)
    if problems:
        say("\n".join(problems))
        say("\n%d difference(s) between %s (%s) and %s (%s). %d file(s) were identical."
            % (len(problems), args.a, a.get("platform"), args.b, b.get("platform"), compared))
        return 1
    sets = len(set(a["sets"]) & set(b["sets"]))
    say("identical: %d files compared in %d read sets (trimmed reads, trimming reports, assembly files); "
        "%s against %s" % (compared, sets, a.get("platform"), b.get("platform")))
    return 0


# --------------------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("make-corpus", help="write the synthetic read sets")
    make.add_argument("dir")
    make.add_argument("--sets", default=None, help="only these sets, separated by commas")
    fetch = sub.add_parser("fetch-real", help="add one real public isolate (downloads 64 MB)")
    fetch.add_argument("dir")
    run = sub.add_parser("run", help="trim and assemble every set; record the checksums")
    run.add_argument("dir")
    run.add_argument("--engines", required=True, help="folder with tesseract-asm, scepter and om2detect.sketch")
    run.add_argument("--out", required=True, help="the record to write (JSON)")
    run.add_argument("--threads", type=int, default=0, help="assembler threads (default: every core)")
    run.add_argument("--work", default=None, help="working folder (default: one of this run's own under "
                                                  "build/equivalence-work, removed afterwards)")
    run.add_argument("--sets", default=None, help="only these sets, separated by commas")
    run.add_argument("--keep", action="store_true", help="keep what the engines wrote")
    compare = sub.add_parser("compare", help="require two records to agree")
    compare.add_argument("a")
    compare.add_argument("b")
    args = parser.parse_args(argv)
    try:
        return {"make-corpus": cmd_make_corpus, "fetch-real": cmd_fetch_real, "run": cmd_run,
                "compare": cmd_compare}[args.command](args)
    except Problem as exc:
        sys.stderr.write("equivalence: %s\n" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
