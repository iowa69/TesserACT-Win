#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Executable form of the wrapper-port specification (audits/wrapper-port.md).

NOT the Windows port. It exists to prove that the written specification is complete: it is
written from the specification, section by section, and parity_check.py shows that it
reproduces the captured output of the real `tesseract` 1.6.2 script on every golden case.
It keeps the script's message texts byte for byte so that the comparison can be exact.

Linux-only parts (free disk through statvfs, free memory through /proc/meminfo) are marked;
everything else is plain standard-library Python that also runs on Windows.

Deliberate deviations from the script (each is listed in the specification):
  * an option given without its value is a usage error (the script loops forever);
  * --genome-size must be a positive decimal number (the script accepts anything);
  * --organism is refused (the Windows app never uses organism models);
  * numbers are always printed with '.', whatever the locale.
"""
import gzip
import itertools
import os
import re
import shutil
import subprocess
import sys
import zlib

SCRIPT_VERSION = "1.6.2"
SCRIPT_SHA256 = "dfa075b96ca0cb1cfc8eabbf06c3bfab9e22257bfdb9eb648d9d7656d0086d4a"
ORGANISM_MB = {"kpneumoniae": "5.5", "ecoli": "5.1", "ecloacae": "5.0", "abaumannii": "4.0",
               "paeruginosa": "6.6", "saureus": "2.8", "efaecium": "3.0", "senterica": "4.9"}
PROFILE = [("TESSERACT_P2_LIBGUARD", "1"), ("TESSERACT_P2_CIRC", "verify"), ("TESSERACT_P2_SPIKEIN", "1"),
           ("TESSERACT_P2_ENDS", "1"), ("TESSERACT_P2_TIPS_LOWERCASE", "1"), ("TESSERACT_P2_SELF_QA", "1"),
           ("TESSERACT_P2_PROVENANCE", "1"), ("TESSERACT_DEEP_NORM", "30"), ("TESSERACT_DEEP_NORM_MATE", "both")]
SAMPLE_ROWS = 200000
GZ_ERRORS = (EOFError, gzip.BadGzipFile, zlib.error, OSError)


class Exit(Exception):
    def __init__(self, code):
        self.code = code


def b(x):
    return x if isinstance(x, bytes) else os.fsencode(str(x))


class Report:
    """stdout plus an optional log file, like `say ... | tee -a "$LOG"` and pf()."""

    def __init__(self):
        self.log = None
        self.fails = self.warns = 0

    def say(self, text, log=True):
        line = b(text) + b"\n"
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
        if log and self.log:
            with open(self.log, "ab") as fh:
                fh.write(line)

    def pf(self, verdict, name, msg):
        self.say(b"  %-6s %-22s %s" % (b(verdict), b(name), b(msg)))
        if verdict == "FAIL":
            self.fails += 1
        elif verdict == "WARN":
            self.warns += 1


def die(msg):
    sys.stderr.write("tesseract: %s\n" % msg)
    raise Exit(2)


# --------------------------------------------------------------------------- input files
def is_gzip(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def gzip_complete(path):
    """`gzip -t`: every member inflates, CRC32 and ISIZE agree, nothing but zero bytes follows."""
    try:
        with gzip.open(path, "rb") as g:
            while g.read(1 << 20):
                pass
        return True
    except GZ_ERRORS:
        return False


def rows(path):
    """`reader FILE | paste - - - -`: four lines per row, TAB-joined; a short last row is padded."""
    try:
        fh = gzip.open(path, "rb") if is_gzip(path) else open(path, "rb")
    except OSError:
        return
    buf = []
    with fh:
        try:
            for line in fh:                       # binary mode: split at LF only
                buf.append(line[:-1] if line.endswith(b"\n") else line)
                if len(buf) == 4:
                    yield b"\t".join(buf)
                    buf = []
        except GZ_ERRORS:
            pass
    if buf:
        yield b"\t".join(buf + [b""] * (4 - len(buf)))


def nm(h):
    s = h[1:] if h.startswith(b"@") else h
    m = re.search(rb"[ \t]", s)
    if m:
        s = s[:m.start()]
    if s.endswith((b"/1", b"/2")):
        s = s[:-2]
    return s


def qcode(c):
    return c if 33 <= c < 127 else 0              # outside '!'..'~' the awk table has no entry: 0


def read_stats(r1, r2):
    n = bad = badlen = badrec = mism = bases = maxlen = 0
    s = sumlen = adapt = polyg = nn = short1 = short2 = 0
    qmin, qmax, qmin2, qmax2 = 999, 0, 999, 0
    firstm = None
    for a, c in itertools.zip_longest(rows(r1), rows(r2), fillvalue=b""):
        f = (a + b"\t" + c).split(b"\t")           # what awk -F'\t' sees
        nf = len(f)
        f += [b""] * (8 - nf)
        n += 1
        if nf == 4 or (nf > 4 and f[4] == b"" and f[5] == b""):
            short2 += 1
            continue
        if f[0] == b"" and nf <= 5:
            short1 += 1
            continue
        if (nf < 8 or f[0][:1] != b"@" or f[4][:1] != b"@" or f[2][:1] != b"+" or f[6][:1] != b"+"):
            bad += 1
            badrec = badrec or n
            continue
        if len(f[1]) != len(f[3]) or len(f[5]) != len(f[7]):
            badlen += 1
            badrec = badrec or n
        n1, n2 = nm(f[0]), nm(f[4])
        if n1 != n2:
            mism += 1
            if firstm is None:
                firstm = n1                        # the script builds "A vs B" but prints only A
        l1, l2 = len(f[1]), len(f[5])
        bases += l1 + l2
        maxlen = max(maxlen, l1, l2)
        if n <= SAMPLE_ROWS:
            s += 1
            sumlen += l1 + l2
            if b"AGATCGGAAGAGC" in f[1] or b"AGATCGGAAGAGC" in f[5]:
                adapt += 1
            if f[1].endswith(b"GGGGGGGGGG") or f[5].endswith(b"GGGGGGGGGG"):
                polyg += 1
            for ch in f[3][::7]:
                q = qcode(ch)
                qmin, qmax = min(qmin, q), max(qmax, q)
            for ch in f[7][::7]:
                q = qcode(ch)
                qmin2, qmax2 = min(qmin2, q), max(qmax2, q)
            nn += f[1].count(b"N") + f[5].count(b"N")
    return dict(pairs=n, bad=bad, badlen=badlen, badrec=badrec, mism=mism, bases=bases, maxlen=maxlen,
                meanlen="%.1f" % (sumlen / (2 * s) if s else 0),
                adapt=float("%.4f" % (adapt / s if s else 0)),      # rounded BEFORE the threshold test
                polyg=float("%.4f" % (polyg / s if s else 0)),
                nfrac=float("%.5f" % (nn / sumlen if sumlen else 0)),
                qmin=qmin, qmax=qmax, qmin2=qmin2, qmax2=qmax2, short1=short1, short2=short2,
                firstm=b"-" if firstm is None else firstm)


def preflight_reads(rep, r1, r2, gsize):
    """Lines 165-240 of the script. Returns the genome size string the retry will use, or None."""
    for f in (r1, r2):
        if not os.access(f, os.R_OK):
            rep.pf("FAIL", "file", "cannot read '%s'" % f)
            continue
        try:
            size = os.path.getsize(f)
        except OSError:
            size = 0
        if size == 0:
            rep.pf("FAIL", "file", "'%s' is empty" % f)
            continue
        if is_gzip(f):
            if gzip_complete(f):
                rep.pf("OK", "gzip", "'%s' is complete" % os.path.basename(f))
            else:
                rep.pf("FAIL", "gzip", "'%s' is truncated or corrupt (gzip -t failed)" % f)
    try:
        if os.path.samefile(r1, r2):
            rep.pf("FAIL", "pairing", "-1 and -2 are the same file")
    except OSError:
        pass
    if rep.fails:
        return None
    st = read_stats(r1, r2)
    if st["pairs"] == 0:
        rep.pf("FAIL", "format", "no FASTQ records read")
        return None
    if st["bad"] == 0:
        rep.pf("OK", "format", "%d read pairs, every record well-formed" % st["pairs"])
    else:
        rep.pf("FAIL", "format", "%d malformed records (first at pair %d); is this FASTQ?" % (st["bad"], st["badrec"]))
    if st["badlen"]:
        rep.pf("FAIL", "format", "%d records whose sequence and quality lengths differ" % st["badlen"])
    if st["short1"]:
        rep.pf("FAIL", "pairing", "the -1 file has %d fewer reads than the -2 file (truncated, or not the mate file)" % st["short1"])
    if st["short2"]:
        rep.pf("FAIL", "pairing", "the -2 file has %d fewer reads than the -1 file (truncated, or not the mate file)" % st["short2"])
    if st["mism"] == 0:
        rep.pf("OK", "pairing", "mate names agree on every pair")
    else:
        rep.pf("FAIL", "pairing", b"%d pairs whose mate names differ (first: %s); the files are not in the same order, or one is truncated"
               % (st["mism"], st["firstm"]))
    qok = True
    for side, lo, hi in (("-1", st["qmin"], st["qmax"]), ("-2", st["qmin2"], st["qmax2"])):
        if lo < 33:
            rep.pf("FAIL", "quality", "%s file: quality characters below '!' (code %d): not FASTQ Phred+33" % (side, lo))
            qok = False
        elif lo >= 64 and hi > 74:
            rep.pf("FAIL", "quality", "%s file: qualities look Phred+64 (codes %d-%d); convert to Phred+33 first (e.g. seqtk seq -VQ64)" % (side, lo, hi))
            qok = False
    if qok:
        rep.pf("OK", "quality", "Phred+33 (codes %d-%d / %d-%d)" % (st["qmin"], st["qmax"], st["qmin2"], st["qmax2"]))
    rep.pf("OK", "read length", "max %d bp, mean %s bp (%s)" % (
        st["maxlen"], st["meanlen"],
        "long-read MiSeq-type library: 2x250/2x300" if st["maxlen"] >= 240 else "short-read library: 2x150 or shorter"))
    gs = gsize or "5"
    depth = int("%.0f" % (st["bases"] / (float(gs) * 1e6)))
    note = "for a %s Mb genome%s" % (gs, "" if gsize else " (assumed; set --genome-size for a better estimate)")
    if depth < 15:
        rep.pf("WARN", "depth", "about %dx %s: too thin for a reliable assembly; expect many breaks" % (depth, note))
    elif depth < 30:
        rep.pf("WARN", "depth", "about %dx %s: low; dropouts are likely" % (depth, note))
    elif depth > 400:
        rep.pf("WARN", "depth", "about %dx %s: very deep; runtime and memory grow with depth, results do not improve beyond ~150x" % (depth, note))
    else:
        rep.pf("OK", "depth", "about %dx %s" % (depth, note))
    a = st["adapt"]
    if a > 0.05:
        rep.pf("WARN", "adapters", "%.1f%% of read pairs carry Illumina adapter sequence: trim them first (fastp, cutadapt)" % (a * 100))
    else:
        rep.pf("OK", "adapters", "%.2f%% of read pairs with adapter read-through" % (a * 100))
    a = st["polyg"]
    if a > 0.02:
        rep.pf("WARN", "poly-G", "%.1f%% of read pairs end in a poly-G run (two-colour chemistry): consider fastp --trim_poly_g" % (a * 100))
    else:
        rep.pf("OK", "poly-G", "%.2f%% of read pairs end in a poly-G run" % (a * 100))
    a = st["nfrac"]
    if a > 0.05:
        rep.pf("WARN", "N bases", "%.1f%% of bases are N" % (a * 100))
    return gs


def preflight_machine(rep, r1, r2, out):
    """Lines 241-250 (run only). Linux implementation of the two machine checks."""
    try:
        with open(r1, "rb"), open(r2, "rb"):
            size = os.path.getsize(r1) + os.path.getsize(r2)
    except OSError:
        return          # the script's arithmetic fails here and both checks are skipped
    v = os.statvfs(out)
    avail_kb = -(-(v.f_bavail * v.f_frsize) // 1024)
    need_kb = size * 3 // 1024 + 2097152
    if avail_kb >= need_kb:
        rep.pf("OK", "disk", "%d GB free in %s" % (avail_kb // 1048576, out))
    else:
        rep.pf("FAIL", "disk", "only %d GB free in %s; need about %d GB" % (avail_kb // 1048576, out, need_kb // 1048576))
    mem_kb = 0
    try:
        for line in open("/proc/meminfo"):
            if "MemAvailable" in line:
                mem_kb = int(line.split()[1])
    except OSError:
        pass
    if mem_kb > 0:
        if mem_kb >= 4194304:
            rep.pf("OK", "memory", "%d GB available" % (mem_kb // 1048576))
        else:
            rep.pf("WARN", "memory", "only %d GB available; a bacterial assembly needs 2-6 GB" % (mem_kb // 1048576))


# ------------------------------------------------------------------------------ postflight
def nonempty(path):
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def contig_stats(path):
    n = tot = dup = empty = badch = 0
    lens, seen = [], set()
    name, cur, started = b"", 0, False
    bad_re = re.compile(rb"[^ACGTNacgtn]")

    def close():
        nonlocal n, tot, empty
        if name != b"":
            if cur == 0:
                empty += 1
            lens.append(cur)
            tot += cur
            n += 1
    with open(path, "rb") as fh:
        for line in fh:
            if line.endswith(b"\n"):
                line = line[:-1]
            if line.startswith(b">"):
                close()
                name = re.match(rb"[^ \t]*", line).group(0)[1:]    # awk $1 (blank = space or TAB only), minus '>'
                if name in seen:
                    dup += 1
                seen.add(name)
                cur = 0
                continue
            if bad_re.search(line):
                badch += 1
            cur += len(line)
    close()
    lens.sort(reverse=True)
    big = lens[0] if lens else 0
    n50 = n90 = l50 = l90 = acc = 0
    for i, ln in enumerate(lens, 1):
        acc += ln
        if not n50 and acc >= tot * 0.5:
            n50, l50 = ln, i
        if not n90 and acc >= tot * 0.9:
            n90, l90 = ln, i
    return dict(n=n, tot=tot, dup=dup, empty=empty, badch=badch, big=big, n50=n50, n90=n90, l50=l50, l90=l90)


def first(pattern, text):
    m = re.search(pattern, text)
    return m.group(1) if m else b""


def tsv_value(path, key):
    vals = []
    with open(path, "rb") as fh:
        for line in fh:
            f = line.rstrip(b"\n").split(b"\t")
            if f[0] == key:
                vals.append(f[1] if len(f) > 1 else b"")
    return b"\n".join(vals)


def detect_organism(report_path):
    with open(report_path, "rb") as fh:
        t = fh.read().replace(b"\n", b"").replace(b" ", b"")
    i = t.find(b'"organism_detect":{')
    if i < 0:
        return ""
    call = first(rb'"call":"([a-z]*)"', t[i:]).decode()
    return call if call in ORGANISM_MB else ""


def check_dir(rep, d, rc, gsize, detected, extra):
    rep.say("[vanilla assembly] %s" % d)
    if rc != 0:
        rep.pf("FAIL", "exit status", "%d (see %s.log)" % (rc, d))
        return
    rep.pf("OK", "exit status", "0")
    for f in ("contigs.fasta", "report.json", "assembly_graph.gfa"):
        if not nonempty(os.path.join(d, f)):
            if f == "assembly_graph.gfa" and "--no-gfa" in extra:
                continue
            rep.pf("FAIL", "file", "%s missing or empty" % f)
    sc = os.path.join(d, "scaffolds.fasta")
    if nonempty(sc):
        with open(sc, "rb") as fh:
            nrec = sum(1 for line in fh if b">" in line)
        rep.pf("OK", "scaffolds.fasta", "%d records (scaffolds with N gaps; scaffolds.agp lists them)" % nrec)
    else:
        rep.pf("OK", "scaffolds.fasta", "not written: every scaffold join was closed with sequence, contigs.fasta is complete")
    contigs = os.path.join(d, "contigs.fasta")
    if not nonempty(contigs):
        return
    c = contig_stats(contigs)
    if c["dup"] == 0 and c["empty"] == 0 and c["badch"] == 0:
        rep.pf("OK", "contigs.fasta", "%(n)d contigs, %(tot)d bp; largest %(big)d; N50 %(n50)d (L50 %(l50)d); N90 %(n90)d (L90 %(l90)d)" % c)
    else:
        rep.pf("FAIL", "contigs.fasta", "malformed: %(dup)d duplicate names, %(empty)d empty records, %(badch)d lines with non-ACGTN characters" % c)
    stats = [("contigs", c["n"]), ("total_bp", c["tot"]), ("largest", c["big"]), ("N50", c["n50"]),
             ("L50", c["l50"]), ("N90", c["n90"]), ("L90", c["l90"])]
    gexp = gsize or ORGANISM_MB.get(detected, "")
    if gexp and c["tot"] > 0:
        r = c["tot"] / (float(gexp) * 1e6)
        if r < 0.8 or r > 1.3:
            rep.pf("WARN", "size", "total %.2f Mb vs ~%s Mb expected: contamination, a mixed sample, or the wrong genome size" % (c["tot"] / 1e6, gexp))
    try:
        with open(os.path.join(d, "report.json"), "rb") as fh:
            js = fh.read().replace(b"\n", b"")
    except OSError:
        js = b""
    if b'"version"' not in js:
        rep.pf("FAIL", "report.json", "unreadable")
    q = os.path.join(d, "p2_self_qa.tsv")
    if nonempty(q):
        comp, qv = tsv_value(q, b"completeness"), tsv_value(q, b"qv_read0")
        alarm, lost = tsv_value(q, b"lost_replicon_alarm"), tsv_value(q, b"missing_bp_est_gt10x")
        if alarm == b"1":
            rep.pf("WARN", "self-check", b"k-mer completeness %s, QV %s; about %s kb of sequence seen in the reads at >10x genome depth is not in the assembly (a small high-copy plasmid, rRNA or IS copies); see p2_self_qa.missing_hi.tsv"
                   % (comp, qv, b("%.1f" % (float(lost or 0) / 1000))))
        else:
            rep.pf("OK", "self-check", b"k-mer completeness %s, QV %s; no high-copy sequence lost" % (comp, qv))
    ncirc = first(rb'"written_circular": *([0-9]*)', js)
    nlin = first(rb'"written_linear": *([0-9]*)', js)
    if ncirc:
        rep.pf("OK", "circles", b"%s replicons written circular (read-verified join), %s circle candidates failed verification and were written linear" % (ncirc, nlin or b"0"))
    spike = first(rb'"spikein": *([0-9]*)', js)
    if int(spike or 0) > 0:
        rep.pf("WARN", "spike-in", b"%s PhiX record(s) labelled _spikein: remove them before submission" % spike)
    guard = b"\n".join(re.findall(rb'"r2": *\{[^}]*"verdict": *"([a-z]*)"', js))
    if guard and guard != b"pass":
        rep.pf("WARN", "library", b"pair orientation check '%s': not a standard FR library; pair-based joins were disabled" % guard)
    rep.pf("OK", "map-back", "skipped: install bwa or minimap2 to measure how many reads map back")
    with open(os.path.join(d, "assembly_stats.tsv"), "wb") as fh:
        fh.write(b"metric\tvalue\n" + b"".join(b("%s\t%d\n" % kv) for kv in stats))


# ------------------------------------------------------------------------------------ main
def find_sketch(here):
    s = os.environ.get("TESSERACT_P2_DETECT_SKETCH", "")
    if s:
        return s
    model_dir = os.environ.get("TESSERACT_MODEL_DIR") or os.path.join(os.environ.get("HOME", ""), ".tesseract/models")
    for c in (here + "/../share/tesseract/om2detect.sketch", here + "/models/om2detect.sketch",
              here + "/om2detect.sketch", model_dir + "/om2detect.sketch"):
        if nonempty(c):
            return c
    return ""


def run_engine(asm, envs, r1, r2, out, th, extra, kmers=None):
    cmd = [asm, "-1", r1, "-2", r2, "-o", out, "-t", th] + (["-k", kmers] if kmers else []) + extra
    with open(os.path.join(out, "assembly.log"), "wb") as log:
        return subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, env=envs).returncode


def main(argv):
    here = os.environ.get("MODEL_HERE") or os.path.dirname(os.path.abspath(__file__))
    asm = os.environ.get("TESSERACT_ASM", "")
    cmd = "run"
    if argv and argv[0] in ("version", "--version", "-v"):
        print("TesserACT %s" % SCRIPT_VERSION)
        return 0
    if argv and argv[0] in ("check", "run"):
        cmd = argv.pop(0)
    r1 = r2 = out = org = gsize = ""
    th = str(os.cpu_count() or 4)
    force = keepgoing = False
    extra = []
    takes = {"-1": "r1", "--read1": "r1", "-2": "r2", "--read2": "r2", "-o": "out", "--out": "out",
             "-t": "th", "--threads": "th", "--organism": "org", "-organism": "org", "--genome-size": "gsize"}
    opt = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in takes:
            if i + 1 >= len(argv):
                die("option %s needs a value" % a)                  # deliberate fix: the script hangs
            opt[takes[a]] = argv[i + 1]
            i += 2
        elif a == "--force":
            force = True
            i += 1
        elif a == "--keep-going":
            keepgoing = True
            i += 1
        elif a == "--":
            extra += argv[i + 1:]
            break
        else:
            extra.append(a)
            i += 1
    r1, r2, out = opt.get("r1", ""), opt.get("r2", ""), opt.get("out", "")
    th, org, gsize = opt.get("th", th), opt.get("org", ""), opt.get("gsize", "")
    if not (r1 and r2):
        die("give both read files: -1 R1.fq.gz -2 R2.fq.gz")
    if cmd != "check" and not out:
        die("give an output directory: -o OUTDIR")
    if not (re.fullmatch(r"[0-9]+", th) and int(th) >= 1):
        die("-t needs a positive integer")
    if org:
        die("organism models are not available in this build")       # deliberate: never used on Windows
    if gsize and not (re.fullmatch(r"[0-9]*\.?[0-9]+", gsize) and float(gsize) > 0):
        die("--genome-size needs a positive number of megabases")   # deliberate fix
    if not (asm and os.access(asm, os.X_OK)):
        die("the assembler engine is missing from this installation (reinstall, or set TESSERACT_ASM)")
    sketch = find_sketch(here)

    rep = Report()
    if cmd == "run":
        os.makedirs(out, exist_ok=True)
        if os.path.exists(os.path.join(out, "contigs.fasta")):
            die("%s already holds an assembly; choose a new output directory" % out)
        rep.log = os.path.join(out, "preflight.txt")
        open(rep.log, "wb").close()
    rep.say("TesserACT %s preflight: %s + %s" % (SCRIPT_VERSION, r1, r2))
    gs = preflight_reads(rep, r1, r2, gsize)
    if cmd == "run":
        preflight_machine(rep, r1, r2, out)
    last = "preflight: %d failed, %d warnings" % (rep.fails, rep.warns)
    rep.say(last)
    if cmd == "check":
        return 0 if rep.fails == 0 else 1
    if rep.fails and not force:
        sys.stderr.write("Stopping before assembly. Fix the failures above, or rerun with --force.\n")
        return 1

    envs = dict(os.environ)
    profile = PROFILE + ([("TESSERACT_P2_DETECT_REPORT", "1"), ("TESSERACT_P2_DETECT_SKETCH", sketch)] if sketch else [])
    for k, v in profile:
        if not os.environ.get(k):                 # a non-empty value set by the user wins
            envs[k] = v
    rep.say("assembling into %s (vanilla) ..." % out, log=False)
    rc = run_engine(asm, envs, r1, r2, out, th, extra)
    retried = False
    if rc == 0 and not any(x in ("-k", "--kmers") for x in extra):
        got = 0
        try:
            with open(os.path.join(out, "contigs.fasta"), "rb") as fh:
                for line in fh:
                    if not line.startswith(b">"):
                        got += len(line[:-1] if line.endswith(b"\n") else line)
        except OSError:
            pass
        want = int(float(gs or "5") * 1e6 * 0.2)   # gs is unset (-> 5) when the read statistics never ran
        kdepth = 99.0
        try:
            with open(os.path.join(out, "assembly.log"), "rb") as fh:
                hits = re.findall(rb"k-mer depth *([0-9.]*)x", fh.read())
            if hits and hits[-1]:
                kdepth = float(hits[-1])
        except (OSError, ValueError):
            pass
        if got < want and kdepth < 2:
            rep.say("the assembly holds only %d bp: retrying with a short k ladder (21,33,55) for thin data ..." % got, log=False)
            keep = os.path.join(out, "attempt1_default_k")
            os.makedirs(keep, exist_ok=True)
            for name in sorted(os.listdir(out)):
                if name not in ("preflight.txt", "attempt1_default_k") and not name.startswith("."):
                    shutil.move(os.path.join(out, name), os.path.join(keep, name))
            rc = run_engine(asm, envs, r1, r2, out, th, extra, kmers="21,33,55")
            retried = True
    detected = ""
    rj = os.path.join(out, "report.json")
    if rc == 0 and nonempty(rj):
        detected = detect_organism(rj)

    summary = os.path.join(out, "SUMMARY.txt")
    open(summary, "wb").close()
    rep.log = summary
    rep.fails = rep.warns = 0
    engine = ""
    try:
        ver = subprocess.run([asm, "--version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
        engine = next((ln for ln in ver.decode("latin-1").split("\n") if "TesserACT" in ln), "")
    except OSError:
        pass
    rep.say("TesserACT %s run summary" % SCRIPT_VERSION)
    rep.say("  reads    %s + %s" % (r1, r2))
    rep.say("  version  tesseract %s, engine %s" % (SCRIPT_VERSION, engine))
    rep.say("  settings standard (read-verified circles, library check, PhiX label, small-plasmid recovery, self-check)")
    rep.say("  preflight %s" % last)
    check_dir(rep, out, rc, gsize, detected, extra)
    if retried:
        rep.pf("WARN", "thin data", "the default k ladder assembled almost nothing; this result uses k = 21,33,55 (the first attempt is in attempt1_default_k/). Expect a fragmented assembly: sequence more deeply if you can")
    if detected:
        rep.say("  hint     the reads look like %s, which has an organism model. To use it (your choice): tesseract models download %s, then rerun with --organism %s" % (detected, detected, detected))
    rep.say("")
    rep.say("postflight: %d failed, %d warnings (details above; this text is in %s)" % (rep.fails, rep.warns, summary))
    return 0 if rep.fails == 0 or keepgoing else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exit as e:
        sys.exit(e.code)
