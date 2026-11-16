#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Make the small example read set that ships with the program.

    python3 tools/make_example.py [OUT_DIR]          (default: example/)

The "isolate" is made up: a random 180 kb circular chromosome and a random 5.2 kb
circular plasmid at about eight copies per chromosome. It is not a real organism and
must never be described as one. It exists so that a beginner can press "Try the
example" and watch the whole flow, offline, in about a minute, and so that the
automated build can run the finished program end to end.

The reads look like Illumina 2x150 reads: instrument-style names, a share of
low-quality 3' ends, a few short fragments that run into the adapter. So trimming
has something to do and the trimming report is worth opening.

Deterministic: the same files, byte for byte, on every machine and every run.
"""

from __future__ import annotations

import gzip
import os
import random
import sys

SEED = 20261116
CHROMOSOME = 180_000
PLASMID = 5_200
PLASMID_COPIES = 8
DEPTH = 45
READ_LEN = 150
INSERT_MEAN, INSERT_SD = 380, 45
ERROR_RATE = 0.002
SHORT_FRAGMENT_SHARE = 0.03          # fragments shorter than a read: adapter read-through
POOR_TAIL_SHARE = 0.18               # reads whose last bases are of low quality
ADAPTER_R1 = "AGATCGGAAGAGCACACGTCTGAACTCCAGTCACATCACGATCTCGTATGCCGTCTTCTGCTTG"
ADAPTER_R2 = "AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGTAGATCTCGGTGGTCGCCGTATCATT"
NAME = "Example_isolate"

_COMP = str.maketrans("ACGT", "TGCA")


def revcomp(seq: str) -> str:
    return seq.translate(_COMP)[::-1]


def random_dna(rng: random.Random, length: int) -> str:
    return "".join(rng.choice("ACGT") for _ in range(length))


def with_errors(rng: random.Random, seq: str) -> str:
    out = list(seq)
    for i, base in enumerate(out):
        if rng.random() < ERROR_RATE:
            out[i] = rng.choice([b for b in "ACGT" if b != base])
    return "".join(out)


def qualities(rng: random.Random, length: int) -> str:
    """Mostly Q37, with an occasional run of poor bases at the 3' end."""
    quals = ["F"] * length
    for i in range(length):
        if rng.random() < 0.02:
            quals[i] = ":"                     # Q25
    if rng.random() < POOR_TAIL_SHARE:
        tail = rng.randint(4, 35)
        for i in range(length - tail, length):
            quals[i] = rng.choice("#,,5")      # Q2 to Q20
    return "".join(quals)


def read_pair(rng: random.Random, replicon: str):
    """One fragment from a circular replicon, as (read 1, read 2)."""
    size = len(replicon)
    if rng.random() < SHORT_FRAGMENT_SHARE:
        frag_len = rng.randint(60, READ_LEN - 10)
    else:
        frag_len = max(READ_LEN + 20, int(rng.gauss(INSERT_MEAN, INSERT_SD)))
    start = rng.randrange(size)
    doubled = replicon + replicon[:frag_len]
    fragment = doubled[start:start + frag_len]
    if rng.random() < 0.5:
        fragment = revcomp(fragment)
    r1 = fragment[:READ_LEN]
    r2 = revcomp(fragment)[:READ_LEN]
    if frag_len < READ_LEN:                    # the read runs through the insert into the adapter
        r1 = (fragment + ADAPTER_R1)[:READ_LEN].ljust(READ_LEN, "G")
        r2 = (revcomp(fragment) + ADAPTER_R2)[:READ_LEN].ljust(READ_LEN, "G")
    return with_errors(rng, r1), with_errors(rng, r2)


def write_gz(path: str, lines) -> None:
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=6, mtime=0) as handle:
            for line in lines:
                handle.write(line.encode("ascii"))


def main(argv) -> int:
    out_dir = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "example")
    os.makedirs(out_dir, exist_ok=True)
    rng = random.Random(SEED)
    chromosome = random_dna(rng, CHROMOSOME)
    plasmid = random_dna(rng, PLASMID)

    pairs = []
    for replicon, depth in ((chromosome, DEPTH), (plasmid, DEPTH * PLASMID_COPIES)):
        for _ in range(int(depth * len(replicon) / (2 * READ_LEN))):
            pairs.append(read_pair(rng, replicon))
    rng.shuffle(pairs)

    r1_lines, r2_lines = [], []
    for i, (r1, r2) in enumerate(pairs):
        tile, x, y = 1101 + (i // 20000), 1000 + (i * 7) % 20000, 1000 + (i * 13) % 20000
        stem = "@M00001:1:000000000-EXMPL:1:%d:%d:%d" % (tile, x, y)
        r1_lines.append("%s 1:N:0:ATCACG\n%s\n+\n%s\n" % (stem, r1, qualities(rng, READ_LEN)))
        r2_lines.append("%s 2:N:0:ATCACG\n%s\n+\n%s\n" % (stem, r2, qualities(rng, READ_LEN)))

    r1_path = os.path.join(out_dir, NAME + "_R1.fastq.gz")
    r2_path = os.path.join(out_dir, NAME + "_R2.fastq.gz")
    write_gz(r1_path, r1_lines)
    write_gz(r2_path, r2_lines)
    with open(os.path.join(out_dir, "README.txt"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "Example reads for TesserACT-Win\n"
            "================================\n\n"
            "%s_R1.fastq.gz and %s_R2.fastq.gz are a small MADE-UP isolate: a random\n"
            "180 kb circular chromosome and a random 5.2 kb circular plasmid. It is not a real\n"
            "organism. It is here so that you can try the program in about a minute.\n\n"
            "In the program, choose \"Try the example\" on the Samples page, or add these two\n"
            "files yourself, then press Start.\n\n"
            "What to expect: two contigs, both marked _circular, about 185,000 bases in all.\n"
            % (NAME, NAME))
    sys.stdout.write("%d read pairs written to %s\n" % (len(pairs), out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
