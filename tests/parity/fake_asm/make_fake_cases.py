#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
"""Canned engine output directories for the postflight parity cases (deterministic).

    python3 make_fake_cases.py [OUTDIR]        (default: ./cases next to this file)

Each directory is what tesseract-asm could leave in -o OUT. report.json follows the layout
of the real 1.6.2 report (top-level keys one per line, the whole "p2" object on one line,
`"key": value` spacing) but carries only the keys the wrapper reads plus a few neighbours.
"""
import os
import random
import sys


def seq(rng, n):
    return "".join(rng.choice("ACGT") for _ in range(n))


def fasta(records, width=60, eol="\n"):
    out = []
    for name, s in records:
        out.append(">" + name + eol)
        for i in range(0, len(s), width):
            out.append(s[i:i + width] + eol)
    return "".join(out)


def report(call="none", circ=0, lin=0, spike=0, verdict="pass", detect=True, version=True, r3=True, eol="\n"):
    det = ""
    if detect:
        det = (', "organism_detect": {"mode": "report_only", "ran": true, "sketch_path": "/opt/tess share/om2detect.sketch", '
               '"sketch_md5": "43f0d121ca5ab647653cb1a7158bc503", "models": [{"name": "ecoli", "core": 3000, "core_hit": 2950, '
               '"score": 0.9833, "hold_list": "x (md5 0)"}, {"name": "senterica", "core": 3100, "core_hit": 97, "score": 0.0313}], '
               '"best": "ecoli", "best_score": 0.9833, "second": "senterica", "second_score": 0.0313, "accept_threshold": 0.85, '
               '"mixture_threshold": 0.1, "accepted": %s, "call": "%s", "mixture": false, "model_applied": false}'
               % ("true" if call != "none" else "false", call))
    r3s = ""
    if r3:
        r3s = ('"r3": {"enabled": 1, "mode": 2, "candidates": %d, "verified": %d, "written_circular": %d, "written_linear": %d, '
               '"closed": 0, "mode_name": "verify", "circles": []}, ' % (circ + lin, circ, circ, lin))
    p2 = ('    "p2": {"enabled": 1, "counters": {"self_qa_run": 1, "self_qa_alarm": 0}, '
          '"r2": {"enabled": 1, "evaluated": 1, "fired": %d, "pairs": 300, "outward_frac": 0, "fr_fit": 0.99, '
          '"verdict": "%s", "max_outward_frac": 0.5, "min_fr_fit": 0.25}, %s'
          '"f5": {"enabled": 1, "screened": 1, "spikein": %d, "spikein_bp": %d, "spikein_circular": 0, '
          '"set": "phiX174 NC_001422.1", "records": []}, '
          '"self_qa": {"ran": true, "verdict": "no lost-replicon alarm"}%s},'
          % (0 if verdict == "pass" else 1, verdict, r3s, spike, spike * 5386, det))
    lines = ["{", '    "tool": "TesserACT",']
    if version:
        lines.append('    "version": "1.6.2",')
    lines += ['    "mode": "standard",', p2, '    "assembly": {', '        "contigs": 3', "    }", "}"]
    return eol.join(lines) + eol


def self_qa(completeness="0.99871", qv="47.13", alarm=0, lost=0, eol="\n"):
    rows = [("k", 31), ("sample", 8), ("completeness", completeness), ("qv_read0", qv), ("qv_solid", "48.02"),
            ("missing_bp_est_3-10x", 12), ("solid_gt10x", 40), ("missing_gt10x", 5),
            ("missing_bp_est_gt10x", lost), ("asm_bases_ge_minlen", 10000), ("lost_replicon_alarm", alarm)]
    return "".join("%s\t%s%s" % (k, v, eol) for k, v in rows)


LOG_TAIL = ("[1/7] loading reads\n      600 reads, 90,000 bases, max length 150, paired  [mode: standard]\n"
            "\n  contigs      %d\n  total length %s bp\n  k-mer depth  %sx (k=%d)\n  elapsed      0.2s   peak memory 100.00 MB\n")
GFA = "H\tVN:Z:1.0\nS\t1\tACGT\n"


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "cases")
    rng = random.Random(20261116)
    c5000, c3000, c2000 = seq(rng, 5000), seq(rng, 3000), seq(rng, 2000)
    ok_contigs = [("NODE_1_length_5000_cov_23.50", c5000), ("NODE_2_length_3000_cov_22.10", c3000),
                  ("NODE_3_length_2000_cov_24.80", c2000[:1900] + c2000[1900:].lower())]

    def put(case, files):
        d = os.path.join(root, case)
        os.makedirs(d, exist_ok=True)
        for name, data in files.items():
            with open(os.path.join(d, name), "wb") as fh:
                fh.write(data if isinstance(data, bytes) else data.encode())

    def std(**over):
        f = {"contigs.fasta": fasta(ok_contigs), "report.json": report(), "assembly_graph.gfa": GFA,
             "p2_self_qa.tsv": self_qa(), "_stderr.txt": LOG_TAIL % (3, "10,000", "23.9", 99)}
        f.update(over)
        return {k: v for k, v in f.items() if v is not None}

    # 1. everything in order: 3 contigs 5000/3000/2000, the last with a lower-case tail
    put("ok", std())

    # 2. N50/N90 with ties, unsorted file order, a blank line inside a record, N and lower case
    recs = [("c_a", seq(rng, 1000)), ("c_b", seq(rng, 2500)), ("c_c", seq(rng, 1400) + "N" * 100),
            ("c_d", seq(rng, 2500).lower()), ("c_e", seq(rng, 2500))]
    txt = fasta(recs, 70)
    cut = txt.index("\n", txt.index(">c_b") + 200) + 1
    put("n50_ties", std(**{"contigs.fasta": txt[:cut] + "\n" + txt[cut:]}))

    # 3. malformed: duplicate first-word name, an empty record, IUPAC code, trailing blank
    mal = (">c1 first copy\n" + seq(rng, 100) + "\n>c1 second copy\n" + seq(rng, 80) + "\n>c3\n>c4\n"
           + seq(rng, 30) + "RY" + seq(rng, 28) + "\n" + seq(rng, 59) + " \n" + seq(rng, 60) + "\n")
    put("malformed", std(**{"contigs.fasta": mal}))

    # 4. odd headers: sequence before the first header, '>' alone, '> name' (name = empty word)
    odd = (seq(rng, 40) + "\n>\n" + seq(rng, 50) + "\n>c2 real\n" + seq(rng, 60) + "\n> spaced name\n"
           + seq(rng, 70) + "\n>c9\n" + seq(rng, 80) + "\n")
    put("odd_headers", std(**{"contigs.fasta": odd}))

    # 5. the `ok` output with CRLF line ends, as a text-mode Windows build of the engine would write
    put("crlf_outputs", std(**{"contigs.fasta": fasta(ok_contigs, eol="\r\n"),
                              "report.json": report(circ=1, spike=1, verdict="outward", eol="\r\n"),
                              "p2_self_qa.tsv": self_qa(alarm=1, lost=2250, eol="\r\n")}))

    # 6. self-check alarm; 2250 bp -> "%.1f" of 2.25 -> 2.2 (ties to even), not 2.3
    put("alarm", std(**{"p2_self_qa.tsv": self_qa("0.98765", "41.27", 1, 2250)}))

    # 7. circles, spike-in, library guard
    put("spike_guard_circ", std(**{"report.json": report(circ=2, lin=1, spike=2, verdict="outward")}))

    # 8. organism detected
    put("detect_ecoli", std(**{"report.json": report(call="ecoli")}))

    # 9. engine failure
    put("rc_fail", std(_rc="3\n", **{"_stderr.txt": "tesseract-asm: error: out of memory\n"}))

    # 10. report.json and the graph missing
    put("missing_report_gfa", std(**{"report.json": None, "assembly_graph.gfa": None}))

    # 11. graph missing (to be run with and without --no-gfa)
    put("no_gfa", std(**{"assembly_graph.gfa": None}))

    # 12. empty contigs.fasta, no "k-mer depth" line in the log
    put("empty_contigs", std(**{"contigs.fasta": "", "_stderr.txt": "[1/7] loading reads\nno contigs\n"}))

    # 13. scaffolds present
    sc = fasta([("SCAF_1", c5000 + "N" * 25 + c3000), ("SCAF_2", c2000)])
    put("scaffolds", std(**{"scaffolds.fasta": sc, "scaffolds.agp": "##agp-version\t2.1\n"}))

    # 14. minimal report: only "version"; no self-check table
    put("minimal_report", std(**{"report.json": '{"version": "1.6.2"}\n', "p2_self_qa.tsv": None}))

    # 15. report without "version"
    put("no_version", std(**{"report.json": report(version=False)}))

    # ---- retry pairs: <case> is the default-ladder attempt, <case>.retry the -k 21,33,55 one
    first0 = std(**{"contigs.fasta": "", "_stderr.txt": LOG_TAIL % (0, "0", "0.0", 99)})
    retry = std(**{"_stderr.txt": LOG_TAIL % (3, "10,000", "6.1", 55)})
    put("retry", first0)
    put("retry.retry", retry)
    put("retry_rcfail", first0)
    put("retry_rcfail.retry", std(_rc="1\n", **{"_stderr.txt": "tesseract-asm: error\n"}))
    small = [("NODE_1_length_500_cov_1.00", seq(rng, 500))]
    put("kdepth_1.9", std(**{"contigs.fasta": fasta(small),
                            "_stderr.txt": "  k-mer depth  7.5x (k=77)\n" + LOG_TAIL % (1, "500", "1.9", 99)}))
    put("kdepth_1.9.retry", retry)
    put("kdepth_2.0", std(**{"contigs.fasta": fasta(small),
                            "_stderr.txt": "  k-mer depth  0.0x (k=77)\n" + LOG_TAIL % (1, "500", "2.0", 99)}))
    put("kdepth_2.0.retry", retry)
    put("rcfail_signature", std(_rc="1\n", **{"contigs.fasta": "", "_stderr.txt": LOG_TAIL % (0, "0", "0.0", 99)}))
    put("rcfail_signature.retry", retry)
    print("cases written to %s" % root)


if __name__ == "__main__":
    main()
