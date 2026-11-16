#!/usr/bin/env python3
"""Build the scepter clinical contamination sketch (data/contam_db.bin.gz).

One RefSeq reference genome per panel species is downloaded from
https://ftp.ncbi.nlm.nih.gov/genomes/all/..., reduced to canonical 31-mers, and
filtered down to the k-mers that are unique to a single species *within the
panel*.  K-mers seen in two or more panel members are dropped: that is what
makes a hit species-specific rather than merely "some Enterobacteriaceae".

Memory is bounded by doing everything inside a FracMinHash sketch: only k-mers
whose 64-bit hash is below 2**64/SCALE are ever considered.  Because the
threshold is the same for every species, a k-mer shared by two species is either
in both sketches or in neither, so the uniqueness filter is exact on the sketch.
Each species is then cut to the KEEP lowest hashes, and its own resulting
threshold is stored so the screen can scale an observed hit count back up.

Only the Python standard library is used (urllib, gzip, struct).  The downloaded
genomes are deleted when the build finishes.

    python3 tools/build_contam_db.py                 # writes data/contam_db.bin.gz
    python3 tools/build_contam_db.py --out X --keep 20000
"""

import argparse
import gzip
import os
import re
import shutil
import struct
import sys
import tempfile
import urllib.error
import urllib.request

BASE = "https://ftp.ncbi.nlm.nih.gov/genomes/all"
MAGIC = b"FPCONTM1"
K = 31
SCALE = 64          # keep 1 k-mer in SCALE for the uniqueness pass
KEEP = 20000        # k-mers retained per species in the shipped sketch

# (species, RefSeq assembly accession, common-contaminant flag).
# Accessions are pinned so a rebuild is reproducible; the directory name under
# the accession is discovered from the FTP index, since it carries the assembly
# name as well.
PANEL = [
    ("Staphylococcus aureus",        "GCF_000013425.1", False),
    ("Staphylococcus epidermidis",   "GCF_000007645.1", True),
    ("Escherichia coli",             "GCF_000005845.2", False),
    ("Klebsiella pneumoniae",        "GCF_000240185.1", False),
    ("Pseudomonas aeruginosa",       "GCF_000006765.1", False),
    ("Acinetobacter baumannii",      "GCF_000015425.1", False),
    ("Enterobacter cloacae",         "GCF_000025565.1", False),
    ("Proteus mirabilis",            "GCF_000069965.1", False),
    ("Salmonella enterica",          "GCF_000006945.2", False),
    ("Serratia marcescens",          "GCF_000513215.1", False),
    ("Streptococcus pneumoniae",     "GCF_000006885.1", False),
    ("Streptococcus pyogenes",       "GCF_000006785.2", False),
    ("Streptococcus agalactiae",     "GCF_000196055.1", False),
    ("Enterococcus faecalis",        "GCF_000007785.1", False),
    ("Enterococcus faecium",         "GCF_000250945.1", False),
    ("Haemophilus influenzae",       "GCF_000027305.1", False),
    ("Neisseria meningitidis",       "GCF_000008805.1", False),
    ("Neisseria gonorrhoeae",        "GCF_000006845.1", False),
    ("Listeria monocytogenes",       "GCF_000196035.1", False),
    ("Clostridioides difficile",     "GCF_000009205.2", False),
    ("Campylobacter jejuni",         "GCF_000009085.1", False),
    ("Cutibacterium acnes",          "GCF_000008345.1", True),
    ("Mycobacterium tuberculosis",   "GCF_000195955.2", False),
]

MASK = (1 << (2 * K)) - 1
TOPSHIFT = 2 * (K - 1)
CODE = [4] * 256
for _i, _c in enumerate("ACGT"):
    CODE[ord(_c)] = _i
    CODE[ord(_c.lower())] = _i

U64 = (1 << 64) - 1


def mix64(x):
    """splitmix64 finalizer; must match mix64() in src/contam.cpp exactly."""
    x = (x + 0x9E3779B97F4A7C15) & U64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & U64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & U64
    return x ^ (x >> 31)


def accession_url(acc):
    """Resolve GCF_000005845.2 -> .../GCF/000/005/845/GCF_000005845.2_ASM584v2/..."""
    body = acc.split("_", 1)[1].split(".")[0]
    parts = "/".join(body[i:i + 3] for i in (0, 3, 6))
    index = "%s/GCF/%s/" % (BASE, parts)
    with urllib.request.urlopen(index, timeout=60) as fh:
        html = fh.read().decode("utf-8", "replace")
    names = set(re.findall(r'href="(%s_[^"/]+)/"' % re.escape(acc), html))
    if not names:
        raise RuntimeError("no assembly directory for %s under %s" % (acc, index))
    name = sorted(names)[0]
    return "%s%s/%s_genomic.fna.gz" % (index, name, name)


def download(url, dest):
    with urllib.request.urlopen(url, timeout=300) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out, 1 << 20)


def sketch_genome(path, threshold):
    """Canonical 31-mers of the genome whose hash is below `threshold`.

    Returns (sketch_set, genome_length, distinct_kmers_estimate)."""
    kept = set()
    total = 0
    length = 0
    fwd = rev = 0
    run = 0
    with gzip.open(path, "rt") as fh:
        for line in fh:
            if line[0] == ">":
                fwd = rev = 0
                run = 0
                continue
            line = line.strip()
            length += len(line)
            for ch in line:
                b = CODE[ord(ch)] if ord(ch) < 256 else 4
                if b > 3:
                    run = 0
                    fwd = rev = 0
                    continue
                fwd = ((fwd << 2) | b) & MASK
                rev = (rev >> 2) | ((3 - b) << TOPSHIFT)
                run += 1
                if run < K:
                    continue
                total += 1
                h = mix64(fwd if fwd < rev else rev)
                if h < threshold:
                    kept.add(h)
    return kept, length, total


def varint(v):
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--out", default=os.path.join(here, "data", "contam_db.bin.gz"))
    ap.add_argument("--keep", type=int, default=KEEP,
                    help="k-mers retained per species (default %d)" % KEEP)
    ap.add_argument("--scale", type=int, default=SCALE,
                    help="FracMinHash scale for the uniqueness pass (default %d)" % SCALE)
    ap.add_argument("--cache", default=None,
                    help="directory to keep the downloaded genomes in "
                         "(default: a temporary directory that is deleted)")
    args = ap.parse_args()

    threshold = (1 << 64) // args.scale
    tmp = args.cache or tempfile.mkdtemp(prefix="fpcontam-")
    os.makedirs(tmp, exist_ok=True)
    keep_cache = args.cache is not None

    sketches = []
    try:
        for name, acc, contaminant in PANEL:
            fna = os.path.join(tmp, acc + ".fna.gz")
            if not os.path.exists(fna):
                url = accession_url(acc)
                sys.stderr.write("[get ] %-30s %s\n" % (name, url.rsplit("/", 1)[-1]))
                try:
                    download(url, fna)
                except (urllib.error.URLError, RuntimeError) as exc:
                    sys.stderr.write("error: %s: %s\n" % (name, exc))
                    return 1
            kmers, length, total = sketch_genome(fna, threshold)
            sys.stderr.write("[hash] %-30s %9d bp  %8d k-mers  %7d in sketch\n"
                             % (name, length, total, len(kmers)))
            sketches.append({"name": name, "contaminant": contaminant, "kmers": kmers,
                             "length": length, "total": total})
    finally:
        if not keep_cache:
            shutil.rmtree(tmp, ignore_errors=True)

    # Drop every k-mer that more than one panel species carries.
    seen = {}
    for i, s in enumerate(sketches):
        for h in s["kmers"]:
            seen[h] = -1 if h in seen else i
    for i, s in enumerate(sketches):
        s["unique"] = sorted(h for h in s["kmers"] if seen[h] == i)
        del s["kmers"]
    del seen

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with gzip.open(args.out, "wb", compresslevel=9) as out:
        out.write(MAGIC)
        out.write(struct.pack("<II", K, len(sketches)))
        for s in sketches:
            uniq = s["unique"]
            # Estimated counts scale the sketch back up to genome coordinates.
            unique_est = int(round(len(uniq) * args.scale))
            kept = uniq[:args.keep]
            # The species' own FracMinHash threshold after the cut: one past the
            # highest retained hash, or the global threshold when nothing was cut.
            max_hash = (kept[-1] + 1) if len(uniq) > args.keep else threshold
            kept = [h for h in kept if h != 0]
            name = s["name"].encode("utf-8")
            out.write(struct.pack("<H", len(name)))
            out.write(name)
            out.write(struct.pack("<B", 1 if s["contaminant"] else 0))
            out.write(struct.pack("<QQQQI", s["length"], s["total"], unique_est,
                                  max_hash, len(kept)))
            prev = 0
            blob = bytearray()
            for h in kept:
                blob += varint(h - prev)
                prev = h
            out.write(bytes(blob))
            sys.stderr.write("[keep] %-30s %7d unique (est %9d)  -> %6d stored, "
                             "1/%d of k-mer space\n"
                             % (s["name"], len(uniq), unique_est, len(kept),
                                (1 << 64) // max_hash if max_hash else 0))

    size = os.path.getsize(args.out)
    sys.stderr.write("wrote %s  (%.2f MB)\n" % (args.out, size / 1e6))
    if size > 5 * 1024 * 1024:
        sys.stderr.write("warning: database exceeds the 5 MB budget for the repo\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
