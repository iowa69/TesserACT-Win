#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Deterministic read fixtures for the `tesseract` wrapper parity tests.

    python3 make_fixtures.py [OUTDIR]          (default: ./fixtures next to this file)

Everything is derived from fixed seeds; gzip members are written with mtime=0 and no
file name, so two runs give byte-identical files (given the same zlib build -- the
MANIFEST therefore also records the sha256 of the *decompressed* content, which does
not depend on zlib).

The genome and read simulators are the ones of the project's own generator
(tests/tools/gen.py: cmd_genome, cmd_reads, mutate) reproduced call for call, so
`gen.py genome g.fa 7 300000` + `gen.py reads g.fa r 60 150 350 30 0.005 4242 paired
fastq gz` decompress to exactly fixtures/good/good_[12].fq.gz.

Fixture groups
  required   the 16 sets asked for by the port plan
  extra      small sets that pin one quirk each (sampling limit, rounding edges,
             header forms, blank lines, gzip trailers ...)
"""
import gzip
import hashlib
import io
import os
import random
import sys

COMP = str.maketrans("ACGTNacgtn", "TGCANtgcan")
AD1 = "AGATCGGAAGAGCACACGTCTGAACTCCAGTCA"      # TruSeq read-1 read-through
AD2 = "AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGT"      # TruSeq read-2 read-through
ADAPTER13 = "AGATCGGAAGAGC"                     # what the script searches for


def rc(s):
    return s.translate(COMP)[::-1]


def rand_seq(rng, n):
    return "".join(rng.choice("ACGT") for _ in range(n))


def mutate(rng, seq, err):
    if err <= 0:
        return seq
    out = list(seq)
    for i, b in enumerate(out):
        if rng.random() < err:
            out[i] = rng.choice([x for x in "ACGT" if x != b])
    return "".join(out)


def simulate(genome, cov, rlen, ins, sd, err, seed):
    """tests/tools/gen.py cmd_reads, paired mode: list of (read1, read2)."""
    rng = random.Random(seed)
    pairs = []
    minfrag = rlen + 20
    L = len(genome)
    want = int(cov * L / (2.0 * rlen))
    made, guard = 0, 0
    while made < want and guard < want * 40:
        guard += 1
        frag = int(rng.gauss(ins, sd))
        if frag < minfrag:
            continue
        s = rng.randint(-(frag - 1), L - 1)
        lo, hi = max(0, s), min(L, s + frag)
        if hi - lo < minfrag:
            continue
        f = genome[lo:hi]
        pairs.append((mutate(rng, f[:rlen], err), mutate(rng, rc(f[-rlen:]), err)))
        made += 1
    rng.shuffle(pairs)
    return pairs


# ----------------------------------------------------------------------------- writers
def rec(name, seq, qual=None):
    return (name, seq, "I" * len(seq) if qual is None else qual)


def fastq(records, eol="\n"):
    return "".join("@%s%s%s%s+%s%s%s" % (n, eol, s, eol, eol, q, eol) for n, s, q in records).encode("latin-1")


def gz(data, level=6):
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=level) as g:
        g.write(data)
    return buf.getvalue()


def mates(pairs, n1="r%d/1", n2="r%d/2"):
    r1 = [rec(n1 % i, a) for i, (a, b) in enumerate(pairs)]
    r2 = [rec(n2 % i, b) for i, (a, b) in enumerate(pairs)]
    return r1, r2


class Out:
    def __init__(self, root):
        self.root = root
        self.rows = []

    def put(self, group, fixture, fname, data, note=""):
        d = os.path.join(self.root, fixture)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fname), "wb") as fh:
            fh.write(data)
        plain = data
        if data[:2] == b"\x1f\x8b":
            try:
                plain = gzip.decompress(data)
            except Exception:                       # deliberately broken gzip
                plain = None
        self.rows.append((group, fixture, fname, len(data), hashlib.sha256(data).hexdigest(),
                          hashlib.sha256(plain).hexdigest() if plain is not None else "-",
                          plain.count(b"\n") if plain is not None else -1, note))

    def manifest(self):
        with open(os.path.join(self.root, "MANIFEST.tsv"), "w", newline="\n") as fh:
            fh.write("group\tfixture\tfile\tbytes\tsha256_file\tsha256_decompressed\tlines\tnote\n")
            for r in self.rows:
                fh.write("\t".join(str(x) for x in r) + "\n")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "fixtures")
    os.makedirs(root, exist_ok=True)
    o = Out(root)

    # ---- genome: gen.py genome g.fa 7 300000
    genome = rand_seq(random.Random(7), 300000)
    with open(os.path.join(root, "genome.fa"), "w", newline="\n") as fh:
        fh.write(">chr1\n")
        for i in range(0, len(genome), 70):
            fh.write(genome[i:i + 70] + "\n")

    # ================================================================= required
    # 1. good: 300 kb, ~60x, 2x150, 0.5 % substitutions
    good = simulate(genome, 60, 150, 350, 30, 0.005, 4242)
    g1, g2 = mates(good)
    o.put("required", "good", "good_1.fq.gz", gz(fastq(g1)), "60000 pairs 2x150, ~60x of 300 kb")
    o.put("required", "good", "good_2.fq.gz", gz(fastq(g2)))

    # 2. thin: ~8x
    thin = simulate(genome, 8, 150, 350, 30, 0.005, 4243)
    t1, t2 = mates(thin)
    o.put("required", "thin", "thin_1.fq.gz", gz(fastq(t1)), "8000 pairs 2x150, ~8x of 300 kb")
    o.put("required", "thin", "thin_2.fq.gz", gz(fastq(t2)))

    # base for the cheap check-only sets: the first 20,000 pairs of `good`
    base = good[:20000]
    b1, b2 = mates(base)
    b1gz, b2gz = gz(fastq(b1)), gz(fastq(b2))

    # 3. truncated gzip: the last 1000 bytes of R2 are missing
    o.put("required", "trunc_gz", "trunc_1.fq.gz", b1gz, "20000 pairs; R2 lacks its last 1000 bytes")
    o.put("required", "trunc_gz", "trunc_2.fq.gz", b2gz[:-1000])

    # 4. R2 has 37 fewer reads than R1
    o.put("required", "r2_short", "r2short_1.fq.gz", b1gz, "R2 lacks its last 37 records")
    o.put("required", "r2_short", "r2short_2.fq.gz", gz(fastq(b2[:-37])))

    # 5. R1 has 5 fewer reads than R2
    o.put("required", "r1_short", "r1short_1.fq.gz", gz(fastq(b1[:-5])), "R1 lacks its last 5 records")
    o.put("required", "r1_short", "r1short_2.fq.gz", b2gz)

    # 6. mates in a different order: R2 shuffled
    sh = list(b2)
    random.Random(606).shuffle(sh)
    o.put("required", "mate_order", "order_1.fq.gz", b1gz, "R2 records shuffled (seed 606)")
    o.put("required", "mate_order", "order_2.fq.gz", gz(fastq(sh)))

    # 7. Phred+64: Q40 'h' with a Q26/Q2 tail ('Z' = 90, 'B' = 66)
    def q64(n):
        return "h" * (n - 10) + "ZZZZZBBBBB"
    o.put("required", "phred64", "phred64_1.fq.gz", gz(fastq([(n, s, q64(len(s))) for n, s, _ in b1])),
          "qualities h...hZZZZZBBBBB (codes 66-104)")
    o.put("required", "phred64", "phred64_2.fq.gz", gz(fastq([(n, s, q64(len(s))) for n, s, _ in b2])))

    # 8. adapter-rich: ~60x, 8 % of pairs are short fragments read through into the adapter
    ad = simulate(genome, 60, 150, 350, 30, 0.005, 4244)
    rng = random.Random(808)
    out_ad = []
    nad = 0
    for a, b in ad:
        if rng.random() < 0.08:
            f = rng.randint(60, 137)                 # fragment shorter than the read
            frag = a[:f]
            fill = 150 - f
            a2 = (frag + AD1 + rand_seq(rng, 150))[:150]
            b2_ = (rc(frag) + AD2 + rand_seq(rng, 150))[:150]
            assert ADAPTER13 in a2 and fill >= 13
            out_ad.append((a2, b2_))
            nad += 1
        else:
            out_ad.append((a, b))
    a1, a2r = mates(out_ad)
    o.put("required", "adapter", "adapter_1.fq.gz", gz(fastq(a1)),
          "60000 pairs, %d read through into TruSeq adapters" % nad)
    o.put("required", "adapter", "adapter_2.fq.gz", gz(fastq(a2r)))

    # 9. poly-G tails: 4 % of pairs, R2 ends in 15-40 G (two-colour no-signal)
    rng = random.Random(909)
    pg = []
    npg = 0
    for a, b in base:
        if rng.random() < 0.04:
            k = rng.randint(15, 40)
            b = b[:150 - k] + "G" * k
            npg += 1
        pg.append((a, b))
    p1, p2 = mates(pg)
    o.put("required", "polyg", "polyg_1.fq.gz", gz(fastq(p1)), "%d of 20000 pairs with a poly-G R2 tail" % npg)
    o.put("required", "polyg", "polyg_2.fq.gz", gz(fastq(p2)))

    # 10. N bases: every base N with probability 0.07
    rng = random.Random(1010)

    def nify(s):
        return "".join("N" if rng.random() < 0.07 else c for c in s)
    nb = [(nify(a), nify(b)) for a, b in base]
    n1, n2 = mates(nb)
    o.put("required", "nbases", "nbases_1.fq.gz", gz(fastq(n1)), "7 % of bases N")
    o.put("required", "nbases", "nbases_2.fq.gz", gz(fastq(n2)))

    # 11. uncompressed input
    o.put("required", "plain", "plain_1.fq", fastq(b1), "the 20000-pair base set, not compressed")
    o.put("required", "plain", "plain_2.fq", fastq(b2))

    # 12. the same file for -1 and -2 (a second, byte-identical copy is NOT the same file)
    o.put("required", "same_file", "same_1.fq.gz", b1gz, "give this file as -1 and as -2")
    o.put("required", "same_file", "copy_of_same_1.fq.gz", b1gz, "identical bytes, different inode")
    sd = os.path.join(root, "same_file")
    for nm, fn in (("hardlink_to_same_1.fq.gz", os.link), ("symlink_to_same_1.fq.gz", os.symlink)):
        p = os.path.join(sd, nm)
        if os.path.lexists(p):
            os.remove(p)
        try:                                        # not every file system has both
            fn("same_1.fq.gz" if fn is os.symlink else os.path.join(sd, "same_1.fq.gz"), p)
        except OSError:
            pass

    # 13. CRLF line endings (first 2000 pairs, uncompressed)
    c1, c2 = b1[:2000], b2[:2000]
    o.put("required", "crlf", "crlf_1.fq", fastq(c1, "\r\n"), "2000 pairs, CRLF, names r<i>/1 r<i>/2")
    o.put("required", "crlf", "crlf_2.fq", fastq(c2, "\r\n"))

    # 14. an empty file as R2
    o.put("required", "empty", "empty_1.fq.gz", gz(fastq(c1)), "R2 is a 0-byte file")
    o.put("required", "empty", "empty_2.fq.gz", b"")

    # 15. non-FASTQ text
    words = ("lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor "
             "incididunt ut labore et dolore magna aliqua").split()
    rng = random.Random(1515)

    def prose(nlines):
        return "".join(" ".join(rng.choice(words) for _ in range(rng.randint(6, 12))) + "\n"
                       for _ in range(nlines)).encode()
    o.put("required", "nonfastq", "text_1.txt", prose(40), "40 lines of prose")
    o.put("required", "nonfastq", "text_2.txt", prose(40))

    # 16. gzip of two concatenated members
    h = 10000
    o.put("required", "two_member", "twomember_1.fq.gz", gz(fastq(b1[:h])) + gz(fastq(b1[h:])),
          "two gzip members, 10000 records each")
    o.put("required", "two_member", "twomember_2.fq.gz", gz(fastq(b2[:h])) + gz(fastq(b2[h:])))

    # ==================================================================== extra
    s1, s2 = b1[:2000], b2[:2000]              # a small 2000-pair set
    s1gz, s2gz = gz(fastq(s1)), gz(fastq(s2))

    # sample_limit: 200,050 pairs of 40 bp; pairs 200,001.. are 60 bp, carry adapters, poly-G,
    # N and Phred+64 qualities -> must change maxlen/bases only, never the sampled statistics.
    rng = random.Random(2020)
    L1, L2 = [], []
    for i in range(200050):
        if i < 200000:
            L1.append(rec("s%d/1" % i, rand_seq(rng, 40)))
            L2.append(rec("s%d/2" % i, rand_seq(rng, 40)))
        else:
            a = rand_seq(rng, 20) + ADAPTER13 + "N" * 17 + "G" * 10
            L1.append(("s%d/1" % i, a, "h" * 60))
            L2.append(("s%d/2" % i, a, "h" * 60))
    o.put("extra", "sample_limit", "limit_1.fq.gz", gz(fastq(L1)),
          "200050 pairs; only the last 50 carry adapter/poly-G/N/Phred+64 and are 60 bp")
    o.put("extra", "sample_limit", "limit_2.fq.gz", gz(fastq(L2)))
    # same, but pair 200,001 has a mate-name mismatch and pair 200,002 is malformed:
    # format and name checks cover every pair.
    M2 = list(L2)
    M2[200000] = ("OTHER/2",) + M2[200000][1:]
    body = fastq(M2[:200001]) + b"this record\nis not\nfastq\nat all\n" + fastq(M2[200002:])
    o.put("extra", "sample_limit_late_errors", "late_1.fq.gz", gz(fastq(L1)),
          "as sample_limit; pair 200001 mate name differs, pair 200002 malformed")
    o.put("extra", "sample_limit_late_errors", "late_2.fq.gz", gz(body))

    # threshold_edge: 40,000 pairs of 30 bp. adapters 2001/40000 = 0.050025 -> "%.4f" 0.0500 -> OK;
    # poly-G 801/40000 = 0.020025 -> 0.0200 -> OK; N 120001/2400000 = 0.0500004 -> "%.5f" 0.05000 -> no WARN.
    rng = random.Random(3030)
    E1, E2 = [], []
    nleft = 120001
    for i in range(40000):
        a, b = rand_seq(rng, 30), rand_seq(rng, 30)
        if a.endswith("G" * 10) or b.endswith("G" * 10) or ADAPTER13 in a or ADAPTER13 in b:
            a, b = "ACGT" * 7 + "AC", "TGCA" * 7 + "TG"
        if i < 2001:
            a = a[:10] + ADAPTER13 + "ACGTACT"
        elif i < 2001 + 801:
            b = b[:20] + "G" * 10
        elif nleft >= 30:
            a = "N" * 30
            nleft -= 30
        elif nleft > 0:
            a = "N" * nleft + a[nleft:]
            nleft = 0
        E1.append(rec("e%d/1" % i, a))
        E2.append(rec("e%d/2" % i, b))
    o.put("extra", "threshold_edge", "edge_1.fq.gz", gz(fastq(E1)),
          "adapters 0.050025, poly-G 0.020025, N 0.0500004: all just above the limit, all pass")
    o.put("extra", "threshold_edge", "edge_2.fq.gz", gz(fastq(E2)))
    # threshold_over: 2003/40000 = 0.050075 -> 0.0501 WARN; 803/40000 = 0.020075 -> 0.0201 WARN;
    # N 120013/2400000 = 0.050005417 -> 0.05001 WARN
    rng = random.Random(3031)
    E1, E2 = [], []
    nleft = 120013
    for i in range(40000):
        a, b = rand_seq(rng, 30), rand_seq(rng, 30)
        if a.endswith("G" * 10) or b.endswith("G" * 10) or ADAPTER13 in a or ADAPTER13 in b:
            a, b = "ACGT" * 7 + "AC", "TGCA" * 7 + "TG"
        if i < 2003:
            a = a[:10] + ADAPTER13 + "ACGTACT"
        elif i < 2003 + 803:
            b = b[:20] + "G" * 10
        elif nleft >= 30:
            a = "N" * 30
            nleft -= 30
        elif nleft > 0:
            a = "N" * nleft + a[nleft:]
            nleft = 0
        E1.append(rec("e%d/1" % i, a))
        E2.append(rec("e%d/2" % i, b))
    o.put("extra", "threshold_over", "over_1.fq.gz", gz(fastq(E1)),
          "adapters 0.050075, poly-G 0.020075, N 0.0500054: all WARN")
    o.put("extra", "threshold_over", "over_2.fq.gz", gz(fastq(E2)))

    # header forms
    il1 = [rec("M01:7:000-FC:1:1101:%d:%d 1:N:0:ACGTACGT" % (1000 + i, 2000 + i), s) for i, (n, s, q) in enumerate(s1)]
    il2 = [rec("M01:7:000-FC:1:1101:%d:%d 2:N:0:ACGTACGT" % (1000 + i, 2000 + i), s) for i, (n, s, q) in enumerate(s2)]
    o.put("extra", "space_header", "space_1.fq.gz", gz(fastq(il1)), "Illumina 1.8 names with a comment after a space")
    o.put("extra", "space_header", "space_2.fq.gz", gz(fastq(il2)))
    tb1 = [rec("r%d\tBC:Z:ACGT" % i, s) for i, (n, s, q) in enumerate(s1)]
    tb2 = [rec("r%d\tBC:Z:ACGT" % i, s) for i, (n, s, q) in enumerate(s2)]
    o.put("extra", "tab_header", "tab_1.fq.gz", gz(fastq(tb1)), "names followed by a TAB and a tag, both files")
    o.put("extra", "tab_header", "tab_2.fq.gz", gz(fastq(tb2)))
    o.put("extra", "tab_header_r2_only", "tabr2_1.fq.gz", s1gz, "only R2 names carry a TAB")
    o.put("extra", "tab_header_r2_only", "tabr2_2.fq.gz", gz(fastq(tb2)))
    d1 = [rec("SRR1.%d.1" % i, s) for i, (n, s, q) in enumerate(s1)]
    d2 = [rec("SRR1.%d.2" % i, s) for i, (n, s, q) in enumerate(s2)]
    o.put("extra", "dot_names", "dot_1.fq.gz", gz(fastq(d1)), "fastq-dump --readids names SRR1.<n>.1 / .2")
    o.put("extra", "dot_names", "dot_2.fq.gz", gz(fastq(d2)))

    # blank lines
    o.put("extra", "blank_tail_both", "blank_1.fq", fastq(s1) + b"\n", "one extra empty line at the end of both files")
    o.put("extra", "blank_tail_both", "blank_2.fq", fastq(s2) + b"\n")
    o.put("extra", "blank_tail_r2", "blankr2_1.fq", fastq(s1), "one extra empty line at the end of R2 only")
    o.put("extra", "blank_tail_r2", "blankr2_2.fq", fastq(s2) + b"\n")
    o.put("extra", "blank_tail_r1", "blankr1_1.fq", fastq(s1) + b"\n", "one extra empty line at the end of R1 only")
    o.put("extra", "blank_tail_r1", "blankr1_2.fq", fastq(s2))
    o.put("extra", "blank_middle", "blankmid_1.fq", fastq(s1[:1000]) + b"\n" + fastq(s1[1000:]),
          "an empty line between records 1000 and 1001 of R1")
    o.put("extra", "blank_middle", "blankmid_2.fq", fastq(s2))
    o.put("extra", "no_final_newline", "nonl_1.fq", fastq(s1)[:-1], "the last line of both files has no newline")
    o.put("extra", "no_final_newline", "nonl_2.fq", fastq(s2)[:-1])
    o.put("extra", "r2_midrecord", "mid_1.fq", fastq(s1), "R2 stops after the sequence line of its last record")
    o.put("extra", "r2_midrecord", "mid_2.fq", b"\n".join(fastq(s2).split(b"\n")[:-3]) + b"\n")

    # CRLF variants
    o.put("extra", "crlf_space_names", "crlfsp_1.fq", fastq(il1, "\r\n"), "CRLF with Illumina 1.8 names (150 bp)")
    o.put("extra", "crlf_space_names", "crlfsp_2.fq", fastq(il2, "\r\n"))
    k1 = [rec(n, s[:147]) for n, s, q in il1]
    k2 = [rec(n, s[:147]) for n, s, q in il2]
    o.put("extra", "crlf_len147", "crlf147_1.fq", fastq(k1, "\r\n"),
          "CRLF, 147 bp reads: the CR is at a sampled quality position (148 = 1 + 7*21)")
    o.put("extra", "crlf_len147", "crlf147_2.fq", fastq(k2, "\r\n"))
    o.put("extra", "crlf_r2_only", "crlfr2_1.fq", fastq(il1), "LF in R1, CRLF in R2")
    o.put("extra", "crlf_r2_only", "crlfr2_2.fq", fastq(il2, "\r\n"))

    # gzip container variants
    o.put("extra", "gz_trailing_garbage", "garbage_1.fq.gz", s1gz + b"GARBAGE\n", "valid gzip followed by 8 non-zero bytes")
    o.put("extra", "gz_trailing_garbage", "garbage_2.fq.gz", s2gz + b"GARBAGE\n")
    o.put("extra", "gz_trailing_zeros", "zeros_1.fq.gz", s1gz + b"\0" * 512, "valid gzip followed by 512 zero bytes")
    o.put("extra", "gz_trailing_zeros", "zeros_2.fq.gz", s2gz + b"\0" * 512)
    o.put("extra", "gz_of_nothing", "gzempty_1.fq.gz", gz(b""), "valid gzip members that decompress to 0 bytes")
    o.put("extra", "gz_of_nothing", "gzempty_2.fq.gz", gz(b""))
    o.put("extra", "magic_not_extension", "lies_1.fq.gz", fastq(s1), "plain text named .fq.gz + gzip named .fq")
    o.put("extra", "magic_not_extension", "lies_2.fq", s2gz)
    o.put("extra", "gz_crc_error", "crc_1.fq.gz", s1gz, "R2: one bit flipped in the stored CRC32")
    bad = bytearray(s2gz)
    bad[-8] ^= 1
    o.put("extra", "gz_crc_error", "crc_2.fq.gz", bytes(bad))
    o.put("extra", "both_empty", "e_1.fq", b"", "two 0-byte files")
    o.put("extra", "both_empty", "e_2.fq", b"")

    # record-level problems
    bl = list(s1)
    for i in (10, 20, 30):
        n, s, q = bl[i]
        bl[i] = (n, s, q[:-3])
    o.put("extra", "badlen", "badlen_1.fq.gz", gz(fastq(bl)), "3 R1 records whose quality is 3 shorter than the sequence")
    o.put("extra", "badlen", "badlen_2.fq.gz", s2gz)
    # a length-mismatch record (11) BEFORE the first malformed record (51): "first at pair" says 11
    bb = list(s1)
    n_, s_, q_ = bb[10]
    bb[10] = (n_, s_, q_[:-1])
    body = fastq(bb[:50]) + b"r50/1 header without at-sign\n" + fastq(bb[50:51]).split(b"\n", 1)[1] + fastq(bb[51:])
    o.put("extra", "badlen_before_bad", "bb_1.fq.gz", gz(body), "record 11: quality 1 short; record 51: header lacks '@'")
    o.put("extra", "badlen_before_bad", "bb_2.fq.gz", s2gz)
    o.put("extra", "fasta_input", "fa_1.fa", "".join(">%s\n%s\n" % (n, s) for n, s, q in s1).encode(), "FASTA, not FASTQ")
    o.put("extra", "fasta_input", "fa_2.fa", "".join(">%s\n%s\n" % (n, s) for n, s, q in s2).encode())

    # quality edge cases
    o.put("extra", "qual_all_J", "qJ_1.fq.gz", gz(fastq([(n, s, "J" * len(s)) for n, s, q in s1])), "all qualities 'J' (74): passes")
    o.put("extra", "qual_all_J", "qJ_2.fq.gz", gz(fastq([(n, s, "J" * len(s)) for n, s, q in s2])))
    o.put("extra", "qual_all_K", "qK_1.fq.gz", gz(fastq([(n, s, "K" * len(s)) for n, s, q in s1])), "all qualities 'K' (75): called Phred+64")
    o.put("extra", "qual_all_K", "qK_2.fq.gz", gz(fastq([(n, s, "K" * len(s)) for n, s, q in s2])))
    # a Phred+64 character only at positions the 1-in-7 sampling never looks at (2..7)
    o.put("extra", "qual_unsampled", "qun_1.fq.gz", gz(fastq([(n, s, "I" + "hhhhhh" + "I" * (len(s) - 7)) for n, s, q in s1])),
          "'h' at quality positions 2-7 only: never sampled")
    o.put("extra", "qual_unsampled", "qun_2.fq.gz", gz(fastq([(n, s, "I" + "hhhhhh" + "I" * (len(s) - 7)) for n, s, q in s2])))
    o.put("extra", "qual_space", "qsp_1.fq.gz", gz(fastq([(n, s, " " + "I" * (len(s) - 1)) for n, s, q in s1])),
          "a space (code 32) as first quality character of R1")
    o.put("extra", "qual_space", "qsp_2.fq.gz", s2gz)
    o.put("extra", "qual_binned", "qbin_1.fq.gz", gz(fastq([(n, s, ("#,:F" * 40)[:len(s)]) for n, s, q in s1])), "NovaSeq-style binned # , : F")
    o.put("extra", "qual_binned", "qbin_2.fq.gz", gz(fastq([(n, s, ("F:,#" * 40)[:len(s)]) for n, s, q in s2])))

    # read-length branch: max >= 240 selects the MiSeq text even when the mean is short
    rng = random.Random(4040)
    v1, v2 = [], []
    for i in range(2000):
        l1 = 239 if i != 776 else 240
        v1.append(rec("v%d/1" % i, rand_seq(rng, 100 if i % 2 else l1)))
        v2.append(rec("v%d/2" % i, rand_seq(rng, 100)))
    o.put("extra", "len_one_240", "len240_1.fq.gz", gz(fastq(v1)), "one 240 bp read among 100/239 bp reads")
    o.put("extra", "len_one_240", "len240_2.fq.gz", gz(fastq(v2)))
    v1[776] = rec("v776/1", v1[776][1][:239])
    o.put("extra", "len_max_239", "len239_1.fq.gz", gz(fastq(v1)), "the same with that read cut to 239 bp")
    o.put("extra", "len_max_239", "len239_2.fq.gz", gz(fastq(v2)))

    # lower-case and IUPAC bases are not looked at by the preflight; only upper-case N is counted
    lw = [rec(n, s[:50].lower() + "n" * 20 + "RYKM" + s[74:]) for n, s, q in s1]
    o.put("extra", "lowercase_iupac", "lower_1.fq.gz", gz(fastq(lw)), "lower-case bases, 20 lower-case n, IUPAC codes")
    o.put("extra", "lowercase_iupac", "lower_2.fq.gz", s2gz)

    # depth rounding: exactly 29 bases, to be run with --genome-size 0.000002 (14.5 -> "14")
    o.put("extra", "tiny29", "tiny_1.fq", fastq([rec("t0/1", "ACGTACGTACGTACG")]), "15 + 14 = 29 bases")
    o.put("extra", "tiny29", "tiny_2.fq", fastq([rec("t0/2", "ACGTACGTACGTAC")]))

    # Windows-flavoured text encodings
    o.put("extra", "utf8_bom", "bom_1.fq", b"\xef\xbb\xbf" + fastq(s1), "UTF-8 byte-order mark in front of both files")
    o.put("extra", "utf8_bom", "bom_2.fq", b"\xef\xbb\xbf" + fastq(s2))
    o.put("extra", "utf16le", "utf16_1.fq", b"\xff\xfe" + fastq(s1[:200], "\r\n").decode("latin-1").encode("utf-16-le"),
          "UTF-16LE with BOM and CRLF, as PowerShell 5 `>` writes text (200 pairs)")
    o.put("extra", "utf16le", "utf16_2.fq", b"\xff\xfe" + fastq(s2[:200], "\r\n").decode("latin-1").encode("utf-16-le"))
    o.put("extra", "cr_only", "cr_1.fq", fastq(s1, "\r"), "CR-only line ends (classic Mac)")
    o.put("extra", "cr_only", "cr_2.fq", fastq(s2, "\r"))

    # a directory and file names with spaces (the script quotes every path)
    o.put("extra", "dir with space", "my reads_1.fq.gz", s1gz, "spaces in the directory and file names")
    o.put("extra", "dir with space", "my reads_2.fq.gz", s2gz)

    # very_thin: ~0.3x (300 pairs). The 8x set does NOT trigger the short-ladder retry with the
    # 1.6.2 engine (its log says "k-mer depth 6.9x"); this one leaves the default ladder with
    # 0 contigs and "k-mer depth 0.0x", which is the retry signature.
    vt = simulate(genome, 0.3, 150, 350, 30, 0.005, 4245)
    w1, w2 = mates(vt)
    o.put("extra", "very_thin", "verythin_1.fq.gz", gz(fastq(w1)), "300 pairs, ~0.3x: triggers the k=21,33,55 retry")
    o.put("extra", "very_thin", "verythin_2.fq.gz", gz(fastq(w2)))

    o.manifest()
    print("fixtures=%d files=%d root=%s" % (len(set(r[1] for r in o.rows)), len(o.rows), root))


if __name__ == "__main__":
    main()
