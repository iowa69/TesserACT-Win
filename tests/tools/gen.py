#!/usr/bin/env python3
"""Fixture generator and assembly checker for the TesserACT test suite.

Subcommands print a one-line summary and exit non-zero when a check fails.
"""
import gzip
import json
import random
import re
import sys

COMP = str.maketrans("ACGTNacgtn", "TGCANtgcan")


def rc(s):
    return s.translate(COMP)[::-1]


def opener(path):
    return gzip.open if path.endswith(".gz") else open


def write_fasta(path, records, width=70):
    with opener(path)(path, "wt") as fh:
        for name, seq in records:
            fh.write(">%s\n" % name)
            for i in range(0, len(seq), width):
                fh.write(seq[i:i + width] + "\n")


def read_fasta(path):
    recs, name, buf = [], None, []
    with opener(path)(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            if line[0] == ">":
                if name is not None:
                    recs.append((name, "".join(buf)))
                name, buf = line[1:], []
            else:
                buf.append(line)
    if name is not None:
        recs.append((name, "".join(buf)))
    return recs


def rand_seq(rng, n):
    return "".join(rng.choice("ACGT") for _ in range(n))


def cmd_genome(a):
    """genome OUT SEED LEN [LEN...] -- unrelated random sequences, one per record."""
    out, seed, lens = a[0], int(a[1]), [int(x) for x in a[2:]]
    rng = random.Random(seed)
    write_fasta(out, [("chr%d" % (i + 1), rand_seq(rng, L)) for i, L in enumerate(lens)])
    print("lengths=%s" % ",".join(str(L) for L in lens))


def cmd_repeat_genome(a):
    """repeat_genome OUT SEED UNIQUE REPEAT -- layout A R B R C with R identical."""
    out, seed, uniq, rep = a[0], int(a[1]), int(a[2]), int(a[3])
    rng = random.Random(seed)
    A, B, C, R = (rand_seq(rng, uniq), rand_seq(rng, uniq),
                  rand_seq(rng, uniq), rand_seq(rng, rep))
    write_fasta(out, [("chr1", A + R + B + R + C)])
    print("length=%d" % (3 * uniq + 2 * rep))


def cmd_circgenome(a):
    """circgenome OUT SEED EXT PHIX_HEADER LEN:COPIES [LEN:COPIES...] -- circular replicons: each random
    record is written COPIES times (depth), every record extended by its own first EXT bases so that
    the simulated fragments span the wrap; plus the phiX174 sequence of PHIX_HEADER (src/p2_phix.h,
    kPhiX174) the same way, unless PHIX_HEADER is '-'."""
    out, seed, ext, phix = a[0], int(a[1]), int(a[2]), a[3]
    rng = random.Random(seed)
    recs = []
    for i, spec in enumerate(a[4:]):
        L, n = (int(x) for x in spec.split(":"))
        s = rand_seq(rng, L)
        for c in range(n):
            recs.append(("rep%d_%d" % (i + 1, c + 1), s + s[:ext]))
    if phix != "-":
        txt = open(phix).read()
        body = txt.split("kPhiX174 =", 1)[1].split(";", 1)[0]
        s = "".join(re.findall(r'"([ACGT]+)"', body))
        recs.append(("phix", s + s[:ext]))
    write_fasta(out, recs)
    print("records=%d" % len(recs))


def mutate(rng, seq, err):
    if err <= 0:
        return seq
    out = list(seq)
    for i, b in enumerate(out):
        if rng.random() < err:
            out[i] = rng.choice([x for x in "ACGT" if x != b])
    return "".join(out)


def cmd_reads(a):
    """reads GENOME PREFIX COV READLEN INSERT SD ERR SEED MODE FORMAT gz|plain"""
    (gpath, prefix, cov, rlen, ins, sd, err, seed, mode, fmt, gz) = (
        a[0], a[1], float(a[2]), int(a[3]), float(a[4]), float(a[5]),
        float(a[6]), int(a[7]), a[8], a[9], a[10])
    rng = random.Random(seed)
    pairs = []
    minfrag = rlen + 20
    for _, seq in read_fasta(gpath):
        L = len(seq)
        want = int(cov * L / (2.0 * rlen))
        made, guard = 0, 0
        while made < want and guard < want * 40:
            guard += 1
            frag = int(rng.gauss(ins, sd))
            if frag < minfrag:
                continue
            # Fragment starts may hang off either end and are then clipped, so
            # the terminal bases get interior-like depth. Without this the first
            # and last few hundred bases are covered by almost no fragments and
            # contig ends are ragged for reasons that have nothing to do with
            # the assembler.
            s = rng.randint(-(frag - 1), L - 1)
            lo, hi = max(0, s), min(L, s + frag)
            if hi - lo < minfrag:
                continue
            f = seq[lo:hi]
            pairs.append((mutate(rng, f[:rlen], err), mutate(rng, rc(f[-rlen:]), err)))
            made += 1
    rng.shuffle(pairs)

    ext = ("fa" if fmt == "fasta" else "fq") + (".gz" if gz == "gz" else "")
    op = gzip.open if gz == "gz" else open

    def emit(fh, name, seq):
        if fmt == "fasta":
            fh.write(">%s\n%s\n" % (name, seq))
        else:
            fh.write("@%s\n%s\n+\n%s\n" % (name, seq, "I" * len(seq)))

    if mode == "paired":
        with op("%s_1.%s" % (prefix, ext), "wt") as f1, \
             op("%s_2.%s" % (prefix, ext), "wt") as f2:
            for i, (r1, r2) in enumerate(pairs):
                emit(f1, "r%d/1" % i, r1)
                emit(f2, "r%d/2" % i, r2)
    elif mode == "interleaved":
        with op("%s_12.%s" % (prefix, ext), "wt") as fh:
            for i, (r1, r2) in enumerate(pairs):
                emit(fh, "r%d/1" % i, r1)
                emit(fh, "r%d/2" % i, r2)
    else:
        with op("%s_s.%s" % (prefix, ext), "wt") as fh:
            for i, (r1, r2) in enumerate(pairs):
                emit(fh, "r%da" % i, r1)
                emit(fh, "r%db" % i, r2)
    print("pairs=%d" % len(pairs))


def cmd_tiny(a):
    """tiny OUT -- a handful of short reads, far too little to assemble."""
    rng = random.Random(99)
    with open(a[0], "wt") as fh:
        for i in range(3):
            s = rand_seq(rng, 60)
            fh.write("@t%d\n%s\n+\n%s\n" % (i, s, "I" * len(s)))
    print("reads=3")


def contigs_of(path):
    return [s for _, s in read_fasta(path)]


def cmd_stats(a):
    """stats CONTIGS -- shell-evalable summary."""
    lens = sorted((len(s) for s in contigs_of(a[0])), reverse=True)
    total = sum(lens)
    acc, n50 = 0, 0
    for L in lens:
        acc += L
        if acc * 2 >= total:
            n50 = L
            break
    print("n=%d total=%d largest=%d n50=%d" % (len(lens), total, lens[0] if lens else 0, n50))


def cmd_exact(a):
    """exact CONTIGS GENOME -- one contig per source record, identical up to rc."""
    seqs, src = contigs_of(a[0]), [s for _, s in read_fasta(a[1])]
    used = set()
    for g in src:
        for j, c in enumerate(seqs):
            if j not in used and (c == g or rc(c) == g):
                used.add(j)
                break
    print("contigs=%d sources=%d exact=%d" % (len(seqs), len(src), len(used)))
    if len(used) != len(src) or len(seqs) != len(src):
        sys.exit(1)


def pieces_of(seq):
    """Scaffold gaps are runs of N; each side of a gap is checked separately."""
    return [p for p in re.split("N+", seq) if p]


def cmd_substr(a):
    """substr CONTIGS GENOME -- every contig is a contiguous piece of the source.

    This is the misassembly check: a contig that joins the wrong flanks of a
    repeat is built entirely from real source k-mers but is not a substring of
    the source anywhere.
    """
    seqs = contigs_of(a[0])
    src = "".join(s for _, s in read_fasta(a[1]))
    both = src + "\x00" + rc(src)
    bad = sum(1 for c in seqs for p in pieces_of(c) if p not in both)
    gaps = sum(c.count("N") for c in seqs)
    print("contigs=%d off_reference=%d gap_bases=%d" % (len(seqs), bad, gaps))
    if bad:
        sys.exit(1)


def kmerset(seq, k):
    return set(seq[i:i + k] for i in range(len(seq) - k + 1))


def cmd_kmercheck(a):
    """kmercheck CONTIGS GENOME K -- no contig k-mer may be absent from the source."""
    k = int(a[2])
    src = "".join(s for _, s in read_fasta(a[1]))
    ref = kmerset(src, k) | kmerset(rc(src), k)
    tot = bad = 0
    for c in contigs_of(a[0]):
        for p in pieces_of(c):
            for i in range(len(p) - k + 1):
                tot += 1
                if p[i:i + k] not in ref:
                    bad += 1
    frac = 1.0 if tot == 0 else 1.0 - bad / float(tot)
    print("kmers=%d foreign=%d frac=%.6f" % (tot, bad, frac))
    if bad:
        sys.exit(1)


def cmd_identity(a):
    """identity CONTIGS GENOME MIN -- base identity of the best contig vs the source.

    The contig is anchored to the source with an exact 31-mer, then compared
    base by base along that diagonal; the denominator is the longer of the two,
    so missing or extra sequence counts against the score.
    """
    k = 31
    src = [s for _, s in read_fasta(a[1])][0]
    idx = {}
    for i in range(len(src) - k + 1):
        idx.setdefault(src[i:i + k], i)
    best = 0.0
    for c in contigs_of(a[0]):
        for cand in (c, rc(c)):
            off = None
            for i in range(len(cand) - k + 1):
                p = idx.get(cand[i:i + k])
                if p is not None:
                    off = p - i
                    break
            if off is None:
                continue
            m = sum(1 for i, b in enumerate(cand)
                    if 0 <= i + off < len(src) and src[i + off] == b)
            best = max(best, m / float(max(len(cand), len(src))))
    print("identity=%.5f" % best)
    if best < float(a[2]):
        sys.exit(1)


def canon_set(path):
    return sorted(min(s, rc(s)) for s in contigs_of(path))


def cmd_sameset(a):
    """sameset A B -- identical contig multisets, orientation-insensitive."""
    x, y = canon_set(a[0]), canon_set(a[1])
    print("a=%d b=%d equal=%s" % (len(x), len(y), x == y))
    if x != y:
        sys.exit(1)


def cmd_format(a):
    """format CONTIGS -- header syntax, stated length, longest-first ordering."""
    names, lens = [], []
    with open(a[0]) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith(">"):
                names.append(line[1:])
                lens.append(0)
            elif line:
                lens[-1] += len(line)
    # The replicon tag is appended, so the rank/length/coverage contract is unchanged and
    # still checked; only a suffix is newly permitted. The permitted tags are enumerated
    # rather than left open, so a malformed or unexpected one still fails:
    #   _chr | _unk | _plas | _plas_<n>, each optionally followed by _circular
    pat = re.compile(r"^NODE_(\d+)_length_(\d+)_cov_(\d+\.\d+)"
                     r"(?:_(?:chr|unk|plas(?:_\d+)?)(?:_circular)?)?$")
    bad_name = bad_len = bad_rank = 0
    for i, (nm, L) in enumerate(zip(names, lens)):
        m = pat.match(nm)
        if not m:
            bad_name += 1
            continue
        if int(m.group(2)) != L:
            bad_len += 1
        if int(m.group(1)) != i + 1:
            bad_rank += 1
    # Length descending WITHIN a replicon block, not across the whole file. The output is
    # ordered as a genome -- chromosome, then each plasmid molecule contiguous, then what
    # could not be called -- so a chromosomal fragment legitimately precedes a longer plasmid.
    # With no model every contig lands in one block and this reduces to the original global
    # check, which is the case this suite exercises; the per-block form is the real contract.
    tags = [re.search(r"_(?:chr|unk|plas(?:_\d+)?)(?:_circular)?$", nm) for nm in names]
    blocks = [m.group(0).replace("_circular", "") if m else "" for m in tags]
    unsorted = sum(1 for i in range(1, len(lens))
                   if blocks[i] == blocks[i - 1] and lens[i] > lens[i - 1])
    print("contigs=%d bad_header=%d bad_length=%d bad_rank=%d unsorted=%d"
          % (len(names), bad_name, bad_len, bad_rank, unsorted))
    if not names or bad_name or bad_len or bad_rank or unsorted:
        sys.exit(1)


def n50_of(lens):
    total = sum(lens)
    acc = 0
    for l in sorted(lens, reverse=True):
        acc += l
        if acc * 2 >= total:
            return l
    return 0


def contigstats_verdict(out, report_path=None):
    """(detail line, list of mismatches) for one output directory; see cmd_contigstats."""
    import os
    rep = json.load(open(report_path or os.path.join(out, "report.json")))["assembly"]
    recs = [s for _, s in read_fasta(os.path.join(out, "contigs.fasta"))]
    lens = [len(s) for s in recs]
    n_in_contigs = sum(s.upper().count("N") for s in recs)
    sp = os.path.join(out, "scaffolds.fasta")
    runs = [len(m.group(0)) for _, s in (read_fasta(sp) if os.path.exists(sp) else [])
            for m in re.finditer("[Nn]+", s)]
    agp = os.path.join(out, "scaffolds.agp")
    agp_n = 0
    if os.path.exists(agp):
        with open(agp) as fh:
            agp_n = sum(1 for l in fh if not l.startswith("#") and l.split("\t")[4:5] == ["N"])
    n50 = n50_of(lens)
    want = {"scaffold_gaps": len(runs), "contig_count": len(lens),
            "contig_total_length": sum(lens), "contig_n50": n50,
            "contig_largest": max(lens, default=0)}
    bad = [k for k, v in want.items() if rep.get(k) != v]
    notes = ["  %s: report says %s, the files say %s" % (k, rep.get(k), want[k]) for k in bad]
    if n_in_contigs:
        bad.append("N_in_contigs.fasta")
        notes.append("  contigs.fasta carries %d N bases" % n_in_contigs)
    if agp_n != len(runs):
        bad.append("agp_N_rows")
        notes.append("  scaffolds.agp has %d N rows for %d N runs" % (agp_n, len(runs)))
    detail = ("gaps=%d short_gaps=%d contigs=%d contig_n50=%d scaffold_n50=%d mismatched=%s"
              % (len(runs), sum(1 for r in runs if r < 10), len(lens), n50, rep.get("n50", 0),
                 ",".join(bad) if bad else "none"))
    return detail, bad, notes


def cmd_contigstats(a):
    """contigstats OUTDIR [REPORT_JSON] -- report.json describes the files actually written.

    The scaffold figures (n50, largest) are raised by asserting an order across a gap, which
    assembles no additional base; the contig figures are what QUAST and every published
    comparison report. Quoting the first as if it were the second has already misled a
    comparison in this project, so the invariant is pinned here against the files:
      contig_count / contig_total_length / contig_n50 / contig_largest == the records of
        contigs.fasta, taken as written. Since 1.3.0 that file is split at every N run and
        then trimmed, so it is never re-split here: the earlier form of this check split it
        at runs of 10+ N, found none, and so compared nothing (and failed every correct
        report of a gapped assembly, which is why no fixture had a gap).
      scaffold_gaps == the number of N runs of any length in scaffolds.fasta (0 without it);
      contigs.fasta carries no N, and scaffolds.agp has one N row per N run.
    The detail line prints gaps=, so a run that produced no gap is visible as gaps=0.
    """
    detail, bad, notes = contigstats_verdict(a[0], a[1] if len(a) > 1 else None)
    print(detail)
    if bad:
        for n in notes:
            print(n)
        sys.exit(1)


def cmd_contigstats_selftest(a):
    """contigstats_selftest DIR -- the contigstats check itself, on hand-written output dirs.

    Every case is a directory a run could write, with a report that either describes it
    (the check must pass) or is stale in the way report.json has been stale (it must fail):
    a 1-N gap counted by a >=10-N rule, a gap the polisher overwrote, a terminal trim that
    the totals still include, an N left in contigs.fasta, an AGP that disagrees.
    """
    import os
    rng = random.Random(4242)
    A, B, C = rand_seq(rng, 1200), rand_seq(rng, 900), rand_seq(rng, 700)

    def stats(lens, gaps):
        return {"assembly": {"contig_count": len(lens), "contig_total_length": sum(lens),
                             "contig_n50": n50_of(lens), "contig_largest": max(lens, default=0),
                             "scaffold_gaps": gaps, "n50": n50_of(lens)}}

    def agp_rows(scaffolds):
        rows = []
        for name, s in scaffolds:
            pos, part = 1, 1
            for m in re.finditer("[ACGT]+|N+", s):
                L = len(m.group(0))
                if m.group(0)[0] == "N":
                    rows.append("%s\t%d\t%d\t%d\tN\t%d\tscaffold\tyes\tpaired-ends"
                                % (name, pos, pos + L - 1, part, L))
                else:
                    rows.append("%s\t%d\t%d\t%d\tW\t%s_%d\t1\t%d\t+"
                                % (name, pos, pos + L - 1, part, name, part, L))
                pos += L
                part += 1
        return rows

    def case(name, contigs, scaffolds, report, agp=None):
        d = os.path.join(a[0], name)
        os.makedirs(d, exist_ok=True)
        write_fasta(os.path.join(d, "contigs.fasta"),
                    [("NODE_%d" % (i + 1), s) for i, s in enumerate(contigs)])
        if scaffolds is not None:
            recs = [("SCAF_%d" % (i + 1), s) for i, s in enumerate(scaffolds)]
            write_fasta(os.path.join(d, "scaffolds.fasta"), recs)
            with open(os.path.join(d, "scaffolds.agp"), "w") as fh:
                fh.write("##agp-version\t2.1\n")
                for r in (agp if agp is not None else agp_rows(recs)):
                    fh.write(r + "\n")
        json.dump(report, open(os.path.join(d, "report.json"), "w"))
        return d

    L = lambda *xs: [len(x) for x in xs]
    trimmed = C[:-30]
    cases = [
        # name, contigs.fasta, scaffolds.fasta, report, expected verdict (0 = consistent)
        ("no_gap", [A, B, C], None, stats(L(A, B, C), 0), 0),
        ("gap_12N", [A, B, C], [A + "N" * 12 + B, C], stats(L(A, B, C), 1), 0),
        ("gap_1N", [A, B, C], [A + "N" + B, C], stats(L(A, B, C), 1), 0),
        # The release report: pre-split scaffolds under a >=10-N rule, so the 1-N join is
        # one contig of len(A)+1+len(B) and no gap.
        ("gap_1N_stale_10N_rule", [A, B, C], [A + "N" + B, C],
         stats([len(A) + 1 + len(B), len(C)], 0), 1),
        # Contig figures right, but the short run not counted as a scaffold gap.
        ("gap_2N_stale_gap_count", [A, B, C], [A + "NN" + B, C], stats(L(A, B, C), 0), 1),
        # Polisher overwrote the gap with read bases: one record, no N run anywhere.
        ("polished_over_N", [A + "G" * 5 + B, C], [A + "G" * 5 + B, C],
         stats(L(A + "G" * 5 + B, C), 0), 0),
        ("polished_over_N_stale", [A + "G" * 5 + B, C], [A + "G" * 5 + B, C],
         stats(L(A, B, C), 1), 1),
        # Terminal-overlap trim removed 30 bp after the statistics were taken.
        ("trimmed", [A, B, trimmed], None, stats(L(A, B, trimmed), 0), 0),
        ("trimmed_stale", [A, B, trimmed], None, stats(L(A, B, C), 0), 1),
        ("N_in_contigs", [A + "N" * 12 + B, C], None, stats(L(A + "N" * 12 + B, C), 0), 1),
        ("agp_missing_N_row", [A, B, C], [A + "N" * 12 + B, C], stats(L(A, B, C), 1), 1),
    ]
    wrong = []
    for name, contigs, scaffolds, report, expect in cases:
        agp = None
        if name == "agp_missing_N_row":
            agp = [r for r in agp_rows([("SCAF_1", scaffolds[0]), ("SCAF_2", scaffolds[1])])
                   if "\tN\t" not in r]
        d = case(name, contigs, scaffolds, report, agp)
        _, bad, _ = contigstats_verdict(d)
        got = 1 if bad else 0
        if got != expect:
            wrong.append("%s(expected %s, got %s)" % (name, "fail" if expect else "pass",
                                                       "fail" if got else "pass"))
    print("cases=%d misjudged=%d%s" % (len(cases), len(wrong),
                                        (" " + ",".join(wrong)) if wrong else ""))
    if wrong:
        sys.exit(1)


def cmd_holereads(a):
    """holereads GENOME PREFIX COV READLEN INSERT SD SEED HOLE_LO HOLE_HI

    Error-free pairs as `reads ... paired fastq gz`, except that no READ overlaps
    [HOLE_LO, HOLE_HI): fragments may still span it, with both mates outside. A coverage
    dropout that pairs bridge is the one situation that makes the assembler write a scaffold
    gap, which none of the other fixtures produce.
    """
    gpath, prefix = a[0], a[1]
    cov, rlen, ins, sd = float(a[2]), int(a[3]), float(a[4]), float(a[5])
    seed, hlo, hhi = int(a[6]), int(a[7]), int(a[8])
    rng = random.Random(seed)
    pairs = []
    for _, seq in read_fasta(gpath):
        L = len(seq)
        want = int(cov * L / (2.0 * rlen))
        made = guard = 0
        while made < want and guard < want * 40:
            guard += 1
            frag = int(rng.gauss(ins, sd))
            if frag < rlen + 20:
                continue
            s = rng.randint(-(frag - 1), L - 1)
            lo, hi = max(0, s), min(L, s + frag)
            if hi - lo < rlen + 20:
                continue
            if any(x < hhi and y > hlo for x, y in ((lo, lo + rlen), (hi - rlen, hi))):
                continue
            f = seq[lo:hi]
            pairs.append((f[:rlen], rc(f[-rlen:])))
            made += 1
    rng.shuffle(pairs)
    with gzip.open(prefix + "_1.fq.gz", "wt") as f1, gzip.open(prefix + "_2.fq.gz", "wt") as f2:
        for i, (r1, r2) in enumerate(pairs):
            f1.write("@r%d/1\n%s\n+\n%s\n" % (i, r1, "I" * len(r1)))
            f2.write("@r%d/2\n%s\n+\n%s\n" % (i, r2, "I" * len(r2)))
    print("pairs=%d" % len(pairs))


def cmd_truncgz(a):
    """truncgz IN_FQ_GZ OUT KEEP_RECORDS -- a gzip stream cut after KEEP_RECORDS records.

    The deflate stream is flushed at a record boundary and then simply stops: no final
    block, no CRC/ISIZE trailer -- a download that died mid-file. `gzip -t` rejects it, and
    everything it does contain decompresses to whole FASTQ records.
    """
    import zlib
    with gzip.open(a[0], "rt") as fh:
        lines = fh.read().split("\n")
    keep = int(a[2]) * 4
    body = ("\n".join(lines[:keep]) + "\n").encode()
    z = zlib.compressobj(6, zlib.DEFLATED, 31)
    data = z.compress(body) + z.flush(zlib.Z_SYNC_FLUSH)
    with open(a[1], "wb") as out:
        out.write(data)
    print("records=%d bytes=%d" % (keep // 4, len(data)))


def cmd_dotnames(a):
    """dotnames IN_FQ OUT_FQ MATE -- rename '@rN/M' to '@SRR1.N.M' (fastq-dump --readids)."""
    with open(a[0]) as src, open(a[1], "w") as dst:
        for i, line in enumerate(src):
            if i % 4 == 0:
                line = re.sub(r"^@r(\d+)/\d", r"@SRR1.\1." + a[2], line)
            dst.write(line)


if __name__ == "__main__":
    table = {"genome": cmd_genome, "repeat_genome": cmd_repeat_genome, "circgenome": cmd_circgenome,
             "reads": cmd_reads,
             "tiny": cmd_tiny, "stats": cmd_stats, "exact": cmd_exact, "substr": cmd_substr,
             "kmercheck": cmd_kmercheck, "identity": cmd_identity, "sameset": cmd_sameset,
             "format": cmd_format, "contigstats": cmd_contigstats,
             "contigstats_selftest": cmd_contigstats_selftest, "holereads": cmd_holereads,
             "truncgz": cmd_truncgz, "dotnames": cmd_dotnames}
    table[sys.argv[1]](sys.argv[2:])
