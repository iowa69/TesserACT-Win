# scepter

**A from-scratch FASTQ preprocessor: everything `fastp` does, faster, plus the QC
analysis that `fastp` leaves out.**

`scepter` reads FASTQ (plain or gzipped, single-end, paired-end or interleaved),
trims adapters and low-quality tails, filters reads, merges overlapping pairs, and
writes a report. It accepts every `fastp` command-line flag and emits a JSON report
with the same key names, so it drops into an existing pipeline without touching the
surrounding code.

It also answers the questions `fastp` cannot: *which tile is bad*, *are the quality
scores honest*, *how big is the genome and how deep did I sequence it*, *how much
library complexity is left*, *what is that overrepresented sequence*.

Written in C++17. Dependencies: the standard library, zlib and pthreads. Nothing else.

---

## Table of contents

- [Why this exists](#why-this-exists)
- [scepter vs fastp](#scepter-vs-fastp)
- [Build and install](#build-and-install)
- [Quick start](#quick-start)
- [Presets](#presets)
- [Option reference](#option-reference)
- [Reports](#reports)
- [JSON schema](#json-schema)
- [Drop-in replacement for fastp](#drop-in-replacement-for-fastp)
- [Benchmarks](#benchmarks)
- [Tests](#tests)
- [License](#license)

---

## Why this exists

`fastp` is the de-facto standard FASTQ preprocessor, and for good reason: it is fast,
it does the right thing by default, and its JSON report is consumed by every workflow
manager in the field. Three things kept bothering us about it:

1. **QC stops at the surface.** You get per-cycle quality and base content. You do not
   get per-tile quality maps, an empirical check of whether the instrument's quality
   scores are calibrated, a per-cycle error profile, or a k-mer spectrum. Those are the
   analyses that tell you *why* a run is bad, not just *that* it is.
2. **The command line fights you.** Every invocation is `-i … -o … -I … -O …`, four
   paths for what is usually one obvious pairing. There is no `--out-dir`, no presets,
   and no way to see what the tool decided before it spends ten minutes deciding it.
3. **There is headroom.** The pipeline serialises gzip compression in the writer.
   Compressing inside the worker pool and appending independent gzip members in the
   writer removes that bottleneck.

`scepter` is a clean-room implementation that keeps everything worth keeping (the
algorithms, the flag names, the JSON keys) and fixes the three points above.

---

## scepter vs fastp

### Equivalent — the same job, the same flags, the same JSON keys

| Capability | fastp | scepter | Notes |
|---|:---:|:---:|---|
| Adapter trimming, SE and PE | yes | yes | auto-detection by read-through consensus |
| Paired-end overlap adapter trimming | yes | yes | `--detect_adapter_for_pe` |
| Extra adapters from FASTA | yes | yes | `--adapter_fasta` |
| Fixed trimming (`-f -t -b -F -T -B`) | yes | yes | |
| Poly-G / poly-X tail trimming | yes | yes | poly-G auto-enables on two-colour chemistry |
| Sliding-window cutting (`-5 -3 -r`) | yes | yes | per-mode window and quality overrides |
| Quality filtering (`-q -u -n -e`) | yes | yes | |
| Length filtering (`-l --length_limit`) | yes | yes | |
| Low-complexity filtering (`-y -Y`) | yes | yes | |
| Index/barcode blacklist filtering | yes | yes | |
| Overlap base correction (`-c`) | yes | yes | |
| Pair merging (`-m --merged_out`) | yes | yes | |
| UMI extraction (`-U --umi_loc …`) | yes | yes | all six locations |
| Duplication rate and FastQC-style levels | yes | yes | |
| Exact deduplication (`-D`) | yes | yes | |
| Overrepresented-sequence analysis (`-p`) | yes | yes | |
| Output splitting (`-s -S -d`) | yes | yes | |
| Insert-size distribution | yes | yes | |
| Interleaved I/O, stdin/stdout | yes | yes | |
| phred+64 input (`-6`) | yes | yes | rewritten as phred+33 on output, as fastp does |
| Unpaired / failed / overlapped output streams | yes | yes | |
| fastp-compatible JSON report | yes | yes | identical key names and nesting |
| Self-contained HTML report | yes | yes | see below for what scepter adds |

### Added by scepter

| Capability | What it gives you |
|---|---|
| **Per-tile quality** | Mean quality per flow-cell tile per cycle, parsed from Illumina headers, with automatic flagging of tiles more than 2 Q units below the global mean. Finds bubbles, smudges and edge effects. Requires Illumina-shaped read names; reports `usable: false` and is skipped otherwise. |
| **Empirical quality calibration** | Where R1 and R2 overlap, a disagreement is scored as an error in the lower-quality base. That yields the *observed* error rate for each *reported* Q value — a calibration curve and a single bias number. On the E. coli test set the instrument's scores run 2.6 Q optimistic. |
| **Per-cycle error profile** | Observed error rate per cycle for R1 and R2 separately, plus the full 4x4 substitution matrix. Distinguishes chemistry decay from a specific miscall bias. |
| **k-mer spectrum** | Scaled-MinHash k-mer sketch (bounded memory, unbiased sample). Produces the count histogram, an estimated genome size, an estimated sequencing depth, the error-k-mer fraction and the repeat fraction. |
| **Library-complexity projection** | Lander-Waterman style estimate of the number of distinct molecules in the library, so you can tell whether deeper sequencing would buy anything. |
| **Contaminant identification** | Overrepresented sequences are matched against a built-in adapter/primer/contaminant list, so a hit is named rather than just listed. |
| **Richer HTML report** | Quality quantile bands (10-90% and IQR) per cycle, per-base quality curves, per-tile heat maps, k-mer spectrum, calibration curve, substitution matrix, insert-size distribution, complexity distribution — all inline, no external assets, no network access. |
| **TSV output** (`--tsv`) | A flat `key<TAB>value` summary (217 rows) for `awk`/`cut`/spreadsheet consumption without a JSON parser. |
| **Presets** (`--preset`) | Twelve named starting configurations (`wgs-bacteria`, `rnaseq`, `amplicon`, `novaseq`, …) applied *before* your other flags, so anything you set explicitly still wins. |
| **Positional arguments and R2 auto-detection** | `scepter sample_R1.fq.gz` finds `sample_R2.fq.gz` on its own. `scepter a_1.fq.gz a_2.fq.gz` just works. |
| **`--out-dir`** | Auto-names the FASTQ outputs *and* the default reports inside one directory. |
| **`--dry-run` / `--explain`** | Print the fully resolved configuration — every default, every preset effect, every auto-detection — and either stop (`--dry-run`) or continue (`--explain`). |
| **Terminal summary** | A boxed end-of-run summary with a PASS/WARN/FAIL verdict and the reasons behind it. |
| **Report re-rendering** (`--report`) | Rebuild the HTML from a stored JSON report, without the FASTQ. |

### Honest differences

- `scepter` is a re-implementation, not a fork. Adapter detection, overlap analysis and
  duplication estimation use the same *ideas* as `fastp` but not the same code, so
  counts can differ by a handful of reads on borderline cases. On the E. coli test set
  the read counts and Q30 rates are identical (see [Benchmarks](#benchmarks)); the
  post-filter base count differs by 0.0014% (6,364 bases in 464 Mbp) because the
  adapter aligner breaks ambiguous read-through alignments differently.
- The deep analyses are cheap enough to disappear into the I/O wait: on the E. coli set
  the default run and `--no-deep` are the same speed within run-to-run noise (3.65 s vs
  3.66 s median of 5). `--no-deep` exists anyway, for fastp-equivalent QC.
- Long-read (ONT/PacBio) FASTQ round-trips byte-for-byte and is filtered correctly, but
  the defaults are tuned for short reads, and the *per-cycle* statistics (quality
  curves, base content) are capped at the first 1000 cycles. Totals, length and quality
  distributions are unaffected. `fastp` has the same design.

---

## Build and install

Requirements: a C++17 compiler (GCC 7+ / Clang 6+), zlib headers, GNU make.

```sh
git clone <repository-url>
cd scepter
make -j"$(nproc)"
./scepter --version
```

Build targets:

| Target | What it does |
|---|---|
| `make` | Portable optimised build (`-O3`). The default. |
| `make native` | Adds `-march=native -mtune=native`. Faster, not portable. |
| `make debug` | `-O0 -g3`, for a debugger. |
| `make asan` | AddressSanitizer + UBSan. |
| `make test` | Builds, then runs `tests/run_tests.sh`. |
| `make install` | Installs to `$PREFIX/bin` (default `/usr/local`). |
| `make uninstall` | Removes it again. |
| `make clean` | Deletes `build/` and the binary. |

```sh
sudo make install                 # -> /usr/local/bin/scepter
make install PREFIX="$HOME/.local"  # -> ~/.local/bin/scepter
```

The build is warning-free under `-Wall -Wextra`.

---

## Quick start

```sh
# QC report only; sample_R2.fq.gz is picked up automatically if it exists
scepter sample_R1.fq.gz

# Trim both mates, outputs and reports auto-named inside clean/
scepter sample_R1.fq.gz sample_R2.fq.gz --out-dir clean/

# Classic fastp-style invocation
scepter -i R1.fq.gz -o clean_1.fq.gz -I R2.fq.gz -O clean_2.fq.gz -w 8

# Settings tuned for feeding a de novo assembler
scepter R1.fq.gz R2.fq.gz --preset assembly --out-dir asm/

# See the resolved configuration (including expected coverage) without running
scepter R1.fq.gz --preset novaseq --genome-size 4.6m --dry-run

# Merge overlapping pairs into single reads
scepter R1.fq.gz R2.fq.gz --merge --merged_out merged.fq.gz

# Stream through a pipe
zcat R1.fq.gz | scepter --stdin --stdout -j qc.json -h qc.html > clean.fq

# Rebuild an HTML report from a stored JSON report
scepter --report old_run.json --html-out old_run.html
```

Every command above was executed against the test data while writing this file.

---

## Presets

A preset is applied **before** your other flags, so anything you set explicitly wins.

| Preset | What it sets up |
|---|---|
| `illumina-pe` | Illumina paired-end: adapter auto-detection for both mates |
| `illumina-se` | Illumina single-end: adapter auto-detection |
| `novaseq` | NovaSeq two-colour chemistry: forces poly-G trimming |
| `nextseq` | NextSeq two-colour chemistry: forces poly-G trimming |
| `bgi` / `mgi` | BGI/MGI adapters and MGI read-name fixing |
| `strict` | Aggressive quality and length filtering for sensitive analyses |
| `lenient` | Keeps almost everything; only obvious junk is removed |
| `amplicon` | Amplicon panels: long reads kept, no dedup, overrepresentation analysis on |
| `rnaseq` | RNA-seq: poly-X trimming, tolerant of short fragments |
| `wgs-bacteria` | Bacterial WGS: window trimming, overlap correction, k-mer spectrum |
| `assembly` | Tuned for feeding a de novo assembler: gentle 3' cut at Q10, no overlap correction |

```sh
scepter R1.fq.gz R2.fq.gz --preset wgs-bacteria --genome-size 4.6m --out-dir clean/
scepter R1.fq.gz R2.fq.gz --preset strict -l 75      # preset, then override -l
```

---

## Option reference

`scepter --help` prints the common options; `scepter --help-all` prints all 107.
Long options also accept `--opt=value`, and `-` and `_` are interchangeable in long
names (`--dry_run` == `--dry-run`).

### Input / output

| Option | Meaning |
|---|---|
| `-i, --in1 FILE` | Read 1 input (FASTQ, optionally gzipped) |
| `-o, --out1 FILE` | Read 1 output; a `.gz` suffix turns on compression |
| `-I, --in2 FILE` | Read 2 input for paired-end data |
| `-O, --out2 FILE` | Read 2 output |
| `--out-dir DIR` | Write auto-named outputs *and* default reports into `DIR` |
| `--unpaired1 FILE` | Read 1 of pairs whose mate failed filtering |
| `--unpaired2 FILE` | Read 2 of pairs whose mate failed filtering |
| `--overlapped_out FILE` | Overlapped region of merged pairs |
| `--failed_out FILE` | Reads that failed filtering, with the reason |
| `-m, --merge` | Merge overlapping pairs into single reads |
| `--merged_out FILE` | Destination for merged reads |
| `--include_unmerged` | Also write unmergeable pairs to the merged file |
| `-6, --phred64` | Input qualities are phred+64; output is rewritten as phred+33 |
| `-z, --compression INT` | gzip level 1 (fast) .. 9 (small), default 4 |
| `--stdin` | Read input from stdin |
| `--stdout` | Write interleaved output to stdout |
| `--interleaved_in` | Input holds both mates interleaved in one stream |
| `--reads_to_process INT` | Stop after this many reads/pairs (0 = all) |
| `--dont_overwrite` | Refuse to start if an output file already exists |
| `--fix_mgi_id` | Strip the `/1` `/2` suffix from MGI read names |

Positional arguments work too: `scepter R1.fq.gz [R2.fq.gz]`. Given only R1, scepter
looks for the matching R2 (`_R1`/`_R2`, `_1`/`_2`, `.1`/`.2`) and uses it if present.

### Adapter trimming

| Option | Meaning |
|---|---|
| `-A, --disable_adapter_trimming` | Turn adapter trimming off entirely |
| `-a, --adapter_sequence SEQ` | Read 1 adapter; `auto` detects it (default) |
| `--adapter_sequence_r2 SEQ` | Read 2 adapter; `auto` detects it |
| `--adapter_fasta FILE` | Extra adapters to trim, in FASTA format |
| `-2, --detect_adapter_for_pe` | Auto-detect adapters for paired-end input too |
| `--allow_gap_overlap_trimming` | Allow indels when overlapping the two mates |

### Global trimming

| Option | Meaning |
|---|---|
| `-f, --trim_front1 INT` | Bases to cut from the 5' end of read 1 |
| `-t, --trim_tail1 INT` | Bases to cut from the 3' end of read 1 |
| `-b, --max_len1 INT` | Truncate read 1 to this length (0 = no limit) |
| `-F, --trim_front2 INT` | As `--trim_front1` but for read 2 (default: same) |
| `-T, --trim_tail2 INT` | As `--trim_tail1` but for read 2 (default: same) |
| `-B, --max_len2 INT` | As `--max_len1` but for read 2 (default: same) |

### Poly-G / poly-X tail trimming

| Option | Meaning |
|---|---|
| `-g, --trim_poly_g` | Force poly-G trimming (automatic on NovaSeq/NextSeq) |
| `--poly_g_min_len INT` | Shortest poly-G run to trim, default 10 |
| `-G, --disable_trim_poly_g` | Never trim poly-G, even on two-colour data |
| `-x, --trim_poly_x` | Trim poly-A/T/C/G tails |
| `--poly_x_min_len INT` | Shortest poly-X run to trim, default 10 |

### Sliding-window quality cutting

| Option | Meaning |
|---|---|
| `-5, --cut_front` | Slide a window from the 5' end, cut while below the mean Q |
| `-3, --cut_tail` | Slide a window from the 3' end, cut while below the mean Q |
| `-r, --cut_right` | Scan 5'->3', drop the rest at the first bad window |
| `-W, --cut_window_size INT` | Window size for all cut modes, default 4 |
| `-M, --cut_mean_quality INT` | Mean quality a window must reach, default 20 |
| `--cut_front_window_size INT` | Window size just for `--cut_front` |
| `--cut_front_mean_quality INT` | Mean quality just for `--cut_front` |
| `--cut_tail_window_size INT` | Window size just for `--cut_tail` |
| `--cut_tail_mean_quality INT` | Mean quality just for `--cut_tail` |
| `--cut_right_window_size INT` | Window size just for `--cut_right` |
| `--cut_right_mean_quality INT` | Mean quality just for `--cut_right` |

### Quality filtering

| Option | Meaning |
|---|---|
| `-Q, --disable_quality_filtering` | Keep every read regardless of quality |
| `-q, --qualified_quality_phred INT` | A base at or above this Q counts as good, default 15 |
| `-u, --unqualified_percent_limit INT` | Max % of bad bases per read, default 40 |
| `-n, --n_base_limit INT` | Max N bases per read, default 5 |
| `-e, --average_qual INT` | Discard reads below this mean quality (0 = off) |

### Length and complexity filtering

| Option | Meaning |
|---|---|
| `-L, --disable_length_filtering` | Keep reads of any length |
| `-l, --length_required INT` | Discard reads shorter than this, default 15 |
| `--length_limit INT` | Discard reads longer than this (0 = no limit) |
| `-y, --low_complexity_filter` | Discard low-complexity reads |
| `-Y, --complexity_threshold INT` | Complexity % a read must reach, default 30 |

### Index (barcode) filtering

| Option | Meaning |
|---|---|
| `--filter_by_index1 FILE` | Blacklist of index1 barcodes, one per line |
| `--filter_by_index2 FILE` | Blacklist of index2 barcodes, one per line |
| `--filter_by_index_threshold INT` | Allowed mismatches against a blacklisted index |

### Paired-end overlap

| Option | Meaning |
|---|---|
| `-c, --correction` | Fix mismatched bases using the mate's overlap |
| `--overlap_len_require INT` | Shortest usable overlap, default 30 |
| `--overlap_diff_limit INT` | Max mismatched bases in the overlap, default 5 |
| `--overlap_diff_percent_limit INT` | Max % mismatch in the overlap, default 20 |

### UMI handling

| Option | Meaning |
|---|---|
| `-U, --umi` | Enable UMI extraction into the read name |
| `--umi_loc WHERE` | `index1`, `index2`, `read1`, `read2`, `per_index`, `per_read` |
| `--umi_len INT` | UMI length when it lives inside the read |
| `--umi_prefix STR` | String placed in front of the UMI in the name |
| `--umi_skip INT` | Bases to drop between the UMI and the insert |
| `--umi_delim STR` | Delimiter between name and UMI, default `:` |

### Duplication and overrepresentation

| Option | Meaning |
|---|---|
| `-D, --dedup` | Remove duplicated reads |
| `--dup_calc_accuracy INT` | Duplication accuracy 1..6 (higher = more memory) |
| `--dont_eval_duplication` | Skip the duplication rate estimate |
| `-p, --overrepresentation_analysis` | Find overrepresented sequences |
| `-P, --overrepresentation_sampling INT` | Sample 1 read in INT, default 20 |

### Output splitting

| Option | Meaning |
|---|---|
| `-s, --split INT` | Split output into INT files (2..999) |
| `-S, --split_by_lines INT` | Split output every INT lines (>= 1000) |
| `-d, --split_prefix_digits INT` | Zero-padding width of split file numbers |

Split files are named `0001.NAME`, `0002.NAME`, … next to the requested output path.
Roll-over happens on chunk boundaries, so file sizes are approximately, not exactly,
equal; the last file absorbs the remainder.

### Reporting

| Option | Meaning |
|---|---|
| `-j, --json FILE` | JSON report path, default `scepter.json` (inside `--out-dir` if set) |
| `-h, --html FILE` | HTML report path, default `scepter.html` (inside `--out-dir` if set) |
| `-R, --report_title STR` | Title shown at the top of the HTML report |
| `--tsv FILE` | Also write a flat TSV summary (scepter only) |
| `--report FILE` | Re-render an existing JSON report and exit |
| `--html-out FILE` | Destination for `--report` re-rendering |
| `--no-summary` | Suppress the end-of-run terminal summary |
| `--quiet` | Only print warnings and errors |
| `--no-color` | Disable ANSI colour in terminal output |

### Deep analysis (scepter only)

| Option | Meaning |
|---|---|
| `--genome-size N` | Expected genome size for coverage (e.g. `4.6m`) |
| `--kmer-size INT` | k for the k-mer spectrum, default 21 |
| `--no-deep` | Skip every extra analysis (fastp-equivalent QC) |
| `--no-tile` | Skip per-tile quality maps |
| `--no-kmer` | Skip the k-mer spectrum |
| `--no-error-profile` | Skip the per-cycle error profile |
| `--no-insert-size` | Skip insert-size estimation |
| `--no-dup-curve` | Skip the library complexity curve |
| `--no-contaminant-screen` | Skip the contaminant screen |

### Execution, modes and help

| Option | Meaning |
|---|---|
| `-w, --thread INT` / `--threads INT` | Worker threads, default auto (capped at 16) |
| `-V, --verbose` | Print progress and per-stage detail |
| `--preset NAME` | Start from a named configuration |
| `--qc-only` | Analyse only; write no FASTQ output |
| `--dry-run` | Print the resolved configuration and exit |
| `--explain` | Print the resolved configuration, then run |
| `-?, --help` | Show the common options |
| `--help-all` | Show every option, grouped by category |
| `--version` | Print the version and exit |

---

## Reports

Three report formats, all optional and all independent:

- **JSON** (`-j`, default `scepter.json`) — machine-readable, fastp-key-compatible.
- **HTML** (`-h`, default `scepter.html`) — a single self-contained file. All CSS,
  JavaScript and SVG are inline: no CDN, no web fonts, no network access at render
  time. Safe to open on an air-gapped machine or serve from a workflow report bundle.
- **TSV** (`--tsv`, off by default) — 217 flat `key<TAB>value` rows, sorted, under a
  `#key<TAB>value` header line. For pipelines that would rather not link a JSON parser:

  ```sh
  scepter R1.fq.gz R2.fq.gz --qc-only --tsv qc.tsv
  awk -F'\t' '$1 == "after_filtering.q30_rate" { print $2 }' qc.tsv
  ```

An existing JSON report can be re-rendered to HTML at any time:

```sh
scepter --report run.json --html-out run.html
```

---

## JSON schema

The report has two halves: **the fastp-compatible top level**, whose key names,
nesting and units match `fastp` exactly, and **the `scepter` namespace**, which holds
everything `fastp` has no equivalent for. Tools that read `fastp` JSON can read
`scepter` JSON unchanged and simply ignore the extra object.

### fastp-compatible top level

```
summary
  fastp_version                  string   the scepter version (key name kept for compatibility)
  sequencing                     string   e.g. "paired end (150 cycles + 150 cycles)"
  before_filtering               object   \ total_reads, total_bases, q20_bases, q30_bases,
  after_filtering                object   / q20_rate, q30_rate, read1_mean_length,
                                            read2_mean_length, gc_content
filtering_result
  passed_filter_reads            int
  low_quality_reads              int
  too_many_N_reads               int
  too_short_reads                int
  too_long_reads                 int
duplication
  rate                           float
  histogram                      array[16]   FastQC-style duplication levels
  mean_gc                        array
insert_size
  peak                           int
  unknown                        int         pairs with no established overlap
  histogram                      array[1001]
adapter_cutting
  adapter_trimmed_reads          int
  adapter_trimmed_bases          int
  read1_adapter_sequence         string
  read2_adapter_sequence         string
  read1_adapter_counts           object      trimmed sequence -> count
  read2_adapter_counts           object
read1_before_filtering           object   \
read2_before_filtering           object    |  see below
read1_after_filtering            object    |
read2_after_filtering            object   /
command                          string   the exact command line
```

Each `readN_{before,after}_filtering` object:

```
total_reads                      int
total_bases                      int
q20_bases                        int
q30_bases                        int
total_cycles                     int
quality_curves
  A, T, C, G                     array[cycles]  mean Phred at each cycle over ONLY the
                                                reads carrying that base at that cycle
  mean                           array[cycles]  overall mean Phred at each cycle
content_curves
  A, T, C, G, N, GC              array[cycles]  base fraction (0..1) at each cycle
kmer_count                       object   all 1024 5-mers -> count
overrepresented_sequences        object   sequence -> count (empty unless -p)
```

`quality_curves.A` is the mean quality of the reads that actually called an `A` at that
cycle, matching `fastp`'s definition; a cycle where a base never occurs reports `0`.

### The `scepter` namespace

```
scepter
  version                        string
  started_at                     string   "YYYY-MM-DD HH:MM:SS"
  duration_seconds               float
  command                        string
  run
    reads_in, reads_out          int
    bases_in, bases_out          int
    pass_rate                    float
    corrected_reads/_bases       int      overlap base correction (-c)
    merged_pairs                 int
    polyg_trimmed_reads/_bases   int
    polyx_trimmed_reads/_bases   int
    adapter_trimmed_reads1/2     int
    adapter_trimmed_bases1/2     int
    adapter_start_read1/2        array[cycles]  adapter-content curve
  filtering_detail               object   per-reason counts: pass, failed_length,
                                          failed_too_long, failed_quality, failed_n_base,
                                          failed_average_quality, failed_complexity,
                                          failed_index, duplicated
  per_tile
    usable                       bool     false when headers are not Illumina-shaped
    tile_count                   int
    global_mean_quality          float
    outlier_tiles                array    "lane:tile" keys > 2 Q below the global mean
    tiles                        object   "lane:tile" -> { reads, mean_quality,
                                                           quality_by_cycle[] }
  error_model
    has_data                     bool     true once > 1000 overlap bases were compared
    pairs_examined               int
    bases_compared               int
    mismatches                   int
    error_rate                   float    observed substitution rate in overlaps
    empirical_q                  float    that rate expressed as a Phred value
    calibration_bias             float    mean(observed Q - reported Q); negative means
                                          the instrument is optimistic
    calibration                  array    [reported Q, observed Q] pairs
    substitutions                array    4x4 matrix over ACGT, [from][to]
    cycle_error_rate_read1/2     array[cycles]
  kmer_spectrum
    k                            int
    scale                        int      MinHash scale: 1 k-mer in `scale` is kept
    reliable                     bool     false when the sketch is too small to trust
    peak_depth                   int      coverage mode above the error shoulder
    estimated_genome_size        float    bp
    estimated_depth              float    base depth, corrected for read subsampling
                                          and for the k-mer/base ratio L/(L-k+1)
    error_kmer_fraction          float    distinct k-mers seen only once or twice
    repeat_fraction              float    k-mer mass well above the main peak
    distinct_sketch_kmers        int
    total_kmers                  int
    histogram                    array    count -> number of distinct k-mers
  duplication_detail
    rate                         float
    estimated_library_size       float    distinct molecules projected in the library
    total_observed               int
    unique_observed              int
    levels                       array[16]
  insert_size_detail             mode, mean, median, stddev, p10, p90, counted,
                                 unknown, max_size, histogram
  quality_distribution           read1, read2, read1_after, read2_after:
                                 per-cycle Phred histograms (drives the quantile bands)
  complexity                     read1_histogram, read2_histogram
  length_distribution            read1_before/_after, read2_before/_after, merged
  gc_distribution                read1_before, read2_before
  mean_quality_distribution      read1_before, read2_before
  overrepresented                read1, read2: [{ seq, count, percent, positions[],
                                                  source }] where `source` names the
                                                matched contaminant when one is known
  verdict
    status                       string   "PASS" | "WARN" | "FAIL"
    reasons                      array    human-readable explanations
  config                         object   every resolved option (115 keys), including
                                          the values chosen by auto-detection
```

Reading a couple of fields:

```sh
scepter R1.fq.gz R2.fq.gz --qc-only -j qc.json
python3 -c 'import json; d=json.load(open("qc.json"));
print(d["summary"]["after_filtering"]["q30_rate"],
      d["scepter"]["kmer_spectrum"]["estimated_depth"],
      d["scepter"]["error_model"]["calibration_bias"])'
```

---

## Drop-in replacement for fastp

**Every `fastp` command-line flag is accepted**, including the deprecated aliases
(`--cut_by_quality5`, `--cut_by_quality3`, `--cut_by_quality_aggressive`,
`--discard_unmerged`), which are honoured with a deprecation note pointing at the
modern spelling. This was verified mechanically: all 84 long options in
`fastp 1.0.1 --help` are recognised by `scepter`.

Semantics are preserved as well: defaults, filter thresholds, trimming order, phred+64
conversion and the JSON key names all match. In practice:

```diff
- fastp -i R1.fq.gz -I R2.fq.gz -o c1.fq.gz -O c2.fq.gz -w 16 -j qc.json -h qc.html
+ scepter -i R1.fq.gz -I R2.fq.gz -o c1.fq.gz -O c2.fq.gz -w 16 -j qc.json -h qc.html
```

or, more simply, `ln -s scepter fastp`.

`scepter` adds flags that `fastp` does not have (`--out-dir`, `--preset`, `--tsv`,
`--dry-run`, `--explain`, `--genome-size`, `--kmer-size`, the `--no-*` deep-analysis
switches, and positional arguments). Nothing existing was renamed or repurposed.

`tests/compat_fastp.sh` runs both tools on the same input and asserts the reports agree:

```sh
FASTP=/path/to/fastp bash tests/compat_fastp.sh
```

---

## Benchmarks

Hardware: 24-core x86-64 Linux workstation, NVMe storage, GCC portable build (`make`,
no `-march=native`).

Data: `bench/reads/ecoli_*.fq.gz` — 1,547,200 read pairs (3,094,400 reads, 464.2 Mbp) of
2x150 bp Illumina data simulated with ART 2.5.8 from E. coli K-12 MG1655 at 100x fold
coverage; 307 MB gzipped input. Both tools run with `-w 16`, default settings,
gzip-compressed output, JSON and HTML reports. Five runs each, warm page cache. Absolute
times move by 10-20% with background load, so the ratio is the number to trust; an
A/B-interleaved re-measurement reproduced it (3.63 s vs 4.78 s median).

| Tool | Wall clock (5 runs, s) | Median | Throughput |
|---|---|---|---|
| **scepter 1.0.0** | 3.60, 3.60, 3.65, 3.67, 3.76 | **3.65 s** | 0.85 M reads/s, 127 MB bases/s |
| scepter 1.0.0 `--no-deep` | 3.56, 3.58, 3.66, 3.67, 3.67 | **3.66 s** | unchanged within noise |
| fastp 1.0.1 | 4.57, 4.63, 4.67, 4.75, 4.88 | **4.67 s** | 0.66 M reads/s |

**scepter is ~1.3x faster** while additionally computing the per-tile map, error model,
k-mer spectrum, calibration curve and complexity projection — which, as the `--no-deep`
row shows, cost nothing measurable here because they run inside the worker pool while
the process is waiting on gzip I/O.

Agreement on the same run:

| Metric | fastp 1.0.1 | scepter 1.0.0 |
|---|---|---|
| `before_filtering.total_reads` | 3,094,400 | 3,094,400 |
| `after_filtering.total_reads` | 3,094,400 | 3,094,400 |
| `before_filtering.q30_rate` | 0.906576 | 0.906576 |
| `after_filtering.q30_rate` | 0.906578 | 0.906578 |
| `before_filtering.gc_content` | 0.507869 | 0.507869 |
| `after_filtering.gc_content` | 0.507867 | 0.507864 |

Read counts are identical and Q30 rates agree to six decimal places. The 3 ppm
difference in post-filter GC comes from the adapter aligner resolving a handful of
ambiguous read-through alignments differently, which changes the last few trimmed bases.

What scepter additionally reported for the same run:

```
genome        ~4.59M bp at 96.9x depth
error rate    0.4467%  (empirical Q23.5, calibration bias -2.6)
insert size   peak 270 bp, mean 248.2, sd 22.4
verdict       PASS
```

Checked against ground truth:

| Estimate | scepter | Truth | Error |
|---|---|---|---|
| Genome size | 4,585,450 bp | 4,641,652 bp (E. coli K-12 MG1655) | -1.2% |
| Sequencing depth | 96.9x | 100x (ART `--fcov 100`; 464.2 Mbp / 4.64 Mb = 100.0x) | -3.1% |

Both come from a k-mer sketch of a 25% read subsample with no reference and no
alignment. The `-2.6` calibration bias means the reported Phred scores are 2.6 Q units
optimistic relative to the substitutions actually observed in read pair overlaps — a
known property of ART's built-in HiSeq 2500 empirical profile, and exactly the kind of
signal that is invisible in a `fastp` report.

Reproduce with:

```sh
cd projects/scepter
make -j"$(nproc)"
time ./scepter ../../bench/reads/ecoli_1.fq.gz ../../bench/reads/ecoli_2.fq.gz \
     --out-dir /tmp/fx -w 16
```

---

## Tests

```sh
make test                                   # 70 functional assertions
bash tests/run_tests.sh                     # the same, directly
FASTP=/path/to/fastp bash tests/compat_fastp.sh   # cross-check against real fastp
```

`tests/run_tests.sh` is self-contained: it generates every fixture into a temporary
directory (removed on exit), needs only bash, coreutils, gzip and python3, prints one
`PASS`/`FAIL` line per assertion, and exits non-zero if anything fails. It covers SE and
PE round-trips, paired-output lockstep, adapter trimming, each quality and length
filter, all three sliding-window modes, poly-G/poly-X trimming, pair merging, overlap
correction, deduplication, UMI extraction, output splitting, JSON and HTML report
validity, interleaved input, phred+64 conversion, `--out-dir` placement, gzip
round-tripping and exit codes.

`tests/compat_fastp.sh` runs both tools over a 200k-pair subset and asserts that
`total_reads` matches exactly and that `q30_rate` and `gc_content` agree to within
0.5%. It skips with a message when no `fastp` binary is available.

---

## License

MIT. See [LICENSE](LICENSE).
