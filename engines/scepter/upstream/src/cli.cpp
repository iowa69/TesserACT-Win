#include "cli.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <string>
#include <vector>

#include <zlib.h>

#include "util.h"

namespace fp {

namespace {

enum class Kind { Flag, Value };

struct OptSpec {
    char sh;              // '\0' when there is no short form
    const char* lg;
    Kind kind;
    const char* meta;     // value placeholder, nullptr for switches
    const char* cat;
    const char* help;
    bool common;          // shown in the short help
    bool deprecated;      // accepted, ignored, hidden from help
};

constexpr const char* kCatIO      = "Input / output";
constexpr const char* kCatAdapter = "Adapter trimming";
constexpr const char* kCatTrim    = "Global trimming";
constexpr const char* kCatPoly    = "Poly-G / poly-X tail trimming";
constexpr const char* kCatCut     = "Sliding-window quality cutting";
constexpr const char* kCatQual    = "Quality filtering";
constexpr const char* kCatLen     = "Length filtering";
constexpr const char* kCatCplx    = "Low-complexity filtering";
constexpr const char* kCatIndex   = "Index (barcode) filtering";
constexpr const char* kCatPair    = "Paired-end overlap";
constexpr const char* kCatUmi     = "UMI handling";
constexpr const char* kCatDup     = "Duplication";
constexpr const char* kCatOver    = "Overrepresented sequences";
constexpr const char* kCatReport  = "Reporting";
constexpr const char* kCatSplit   = "Output splitting";
constexpr const char* kCatDeep    = "Deep analysis";
constexpr const char* kCatContam  = "Contamination screening";
constexpr const char* kCatExec    = "Execution";
constexpr const char* kCatHelp    = "Presets, modes and help";

const char* const kCategoryOrder[] = {
    kCatIO,   kCatAdapter, kCatTrim,  kCatPoly,  kCatCut,   kCatQual,
    kCatLen,  kCatCplx,    kCatIndex, kCatPair,  kCatUmi,   kCatDup,
    kCatOver, kCatReport,  kCatSplit, kCatDeep,  kCatContam, kCatExec, kCatHelp,
};

const OptSpec kOpts[] = {
    // ---- input / output --------------------------------------------------
    {'i', "in1", Kind::Value, "FILE", kCatIO, "read 1 input (FASTQ, optionally gzipped)", true, false},
    {'o', "out1", Kind::Value, "FILE", kCatIO, "read 1 output; .gz suffix turns on compression", true, false},
    {'I', "in2", Kind::Value, "FILE", kCatIO, "read 2 input for paired-end data", true, false},
    {'O', "out2", Kind::Value, "FILE", kCatIO, "read 2 output", true, false},
    {'\0', "out-dir", Kind::Value, "DIR", kCatIO, "write auto-named outputs into DIR", true, false},
    {'\0', "unpaired1", Kind::Value, "FILE", kCatIO, "read 1 of pairs whose mate failed filtering", false, false},
    {'\0', "unpaired2", Kind::Value, "FILE", kCatIO, "read 2 of pairs whose mate failed filtering", false, false},
    {'\0', "overlapped_out", Kind::Value, "FILE", kCatIO, "overlapped region of merged pairs", false, false},
    {'\0', "failed_out", Kind::Value, "FILE", kCatIO, "reads that failed filtering, with the reason", false, false},
    {'m', "merge", Kind::Flag, nullptr, kCatIO, "merge overlapping pairs into single reads", false, false},
    {'\0', "merged_out", Kind::Value, "FILE", kCatIO, "destination for merged reads", false, false},
    {'\0', "include_unmerged", Kind::Flag, nullptr, kCatIO, "also write unmergeable pairs to the merged file", false, false},
    {'6', "phred64", Kind::Flag, nullptr, kCatIO, "input qualities are phred+64 instead of phred+33", false, false},
    {'z', "compression", Kind::Value, "INT", kCatIO, "gzip level 1 (fast) .. 9 (small), default 4", true, false},
    {'\0', "stdin", Kind::Flag, nullptr, kCatIO, "read input from stdin", false, false},
    {'\0', "stdout", Kind::Flag, nullptr, kCatIO, "write interleaved output to stdout", false, false},
    {'\0', "interleaved_in", Kind::Flag, nullptr, kCatIO, "input holds both mates interleaved in one stream", false, false},
    {'\0', "reads_to_process", Kind::Value, "INT", kCatIO, "stop after this many reads/pairs (0 = all)", false, false},
    {'\0', "dont_overwrite", Kind::Flag, nullptr, kCatIO, "refuse to start if an output file already exists", false, false},
    {'\0', "fix_mgi_id", Kind::Flag, nullptr, kCatIO, "strip the /1 /2 suffix from MGI read names", false, false},

    // ---- adapter ---------------------------------------------------------
    {'A', "disable_adapter_trimming", Kind::Flag, nullptr, kCatAdapter, "turn adapter trimming off entirely", true, false},
    {'a', "adapter_sequence", Kind::Value, "SEQ", kCatAdapter, "read 1 adapter; 'auto' detects it (default)", true, false},
    {'\0', "adapter_sequence_r2", Kind::Value, "SEQ", kCatAdapter, "read 2 adapter; 'auto' detects it", false, false},
    {'\0', "adapter_fasta", Kind::Value, "FILE", kCatAdapter, "extra adapters to trim, in FASTA format", false, false},
    {'2', "detect_adapter_for_pe", Kind::Flag, nullptr, kCatAdapter, "auto-detect adapters for paired-end input too", true, false},
    {'\0', "allow_gap_overlap_trimming", Kind::Flag, nullptr, kCatAdapter, "allow indels when overlapping the two mates", false, false},

    // ---- global trimming -------------------------------------------------
    {'f', "trim_front1", Kind::Value, "INT", kCatTrim, "bases to cut from the 5' end of read 1", true, false},
    {'t', "trim_tail1", Kind::Value, "INT", kCatTrim, "bases to cut from the 3' end of read 1", true, false},
    {'b', "max_len1", Kind::Value, "INT", kCatTrim, "truncate read 1 to this length (0 = no limit)", false, false},
    {'F', "trim_front2", Kind::Value, "INT", kCatTrim, "as --trim_front1 but for read 2 (default: same)", false, false},
    {'T', "trim_tail2", Kind::Value, "INT", kCatTrim, "as --trim_tail1 but for read 2 (default: same)", false, false},
    {'B', "max_len2", Kind::Value, "INT", kCatTrim, "as --max_len1 but for read 2 (default: same)", false, false},

    // ---- poly tails ------------------------------------------------------
    {'g', "trim_poly_g", Kind::Flag, nullptr, kCatPoly, "force poly-G trimming (auto for NovaSeq/NextSeq)", true, false},
    {'\0', "poly_g_min_len", Kind::Value, "INT", kCatPoly, "shortest poly-G run to trim, default 10", false, false},
    {'G', "disable_trim_poly_g", Kind::Flag, nullptr, kCatPoly, "never trim poly-G, even on two-colour data", false, false},
    {'x', "trim_poly_x", Kind::Flag, nullptr, kCatPoly, "trim poly-A/T/C/G tails", true, false},
    {'\0', "poly_x_min_len", Kind::Value, "INT", kCatPoly, "shortest poly-X run to trim, default 10", false, false},

    // ---- sliding window cutting ------------------------------------------
    {'5', "cut_front", Kind::Flag, nullptr, kCatCut, "slide a window from the 5' end, cut while below Q", true, false},
    {'3', "cut_tail", Kind::Flag, nullptr, kCatCut, "slide a window from the 3' end, cut while below Q", true, false},
    {'r', "cut_right", Kind::Flag, nullptr, kCatCut, "scan 5'->3', drop the rest at the first bad window", true, false},
    {'W', "cut_window_size", Kind::Value, "INT", kCatCut, "window size for all cut modes, default 4", false, false},
    {'M', "cut_mean_quality", Kind::Value, "INT", kCatCut, "mean quality a window must reach, default 20", true, false},
    {'\0', "cut_front_window_size", Kind::Value, "INT", kCatCut, "window size just for --cut_front", false, false},
    {'\0', "cut_front_mean_quality", Kind::Value, "INT", kCatCut, "mean quality just for --cut_front", false, false},
    {'\0', "cut_tail_window_size", Kind::Value, "INT", kCatCut, "window size just for --cut_tail", false, false},
    {'\0', "cut_tail_mean_quality", Kind::Value, "INT", kCatCut, "mean quality just for --cut_tail", false, false},
    {'\0', "cut_right_window_size", Kind::Value, "INT", kCatCut, "window size just for --cut_right", false, false},
    {'\0', "cut_right_mean_quality", Kind::Value, "INT", kCatCut, "mean quality just for --cut_right", false, false},

    // ---- quality filtering -----------------------------------------------
    {'Q', "disable_quality_filtering", Kind::Flag, nullptr, kCatQual, "keep every read regardless of quality", false, false},
    {'q', "qualified_quality_phred", Kind::Value, "INT", kCatQual, "a base at or above this Q counts as good, default 15", true, false},
    {'u', "unqualified_percent_limit", Kind::Value, "INT", kCatQual, "max % of bad bases per read, default 40", true, false},
    {'n', "n_base_limit", Kind::Value, "INT", kCatQual, "max N bases per read, default 5", false, false},
    {'e', "average_qual", Kind::Value, "INT", kCatQual, "discard reads below this mean quality (0 = off)", true, false},

    // ---- length filtering ------------------------------------------------
    {'L', "disable_length_filtering", Kind::Flag, nullptr, kCatLen, "keep reads of any length", false, false},
    {'l', "length_required", Kind::Value, "INT", kCatLen, "discard reads shorter than this, default 15", true, false},
    {'\0', "length_limit", Kind::Value, "INT", kCatLen, "discard reads longer than this (0 = no limit)", false, false},

    // ---- complexity ------------------------------------------------------
    {'y', "low_complexity_filter", Kind::Flag, nullptr, kCatCplx, "discard low-complexity reads", false, false},
    {'Y', "complexity_threshold", Kind::Value, "INT", kCatCplx, "complexity % a read must reach, default 30", false, false},

    // ---- index filtering -------------------------------------------------
    {'\0', "filter_by_index1", Kind::Value, "FILE", kCatIndex, "blacklist of index1 barcodes, one per line", false, false},
    {'\0', "filter_by_index2", Kind::Value, "FILE", kCatIndex, "blacklist of index2 barcodes, one per line", false, false},
    {'\0', "filter_by_index_threshold", Kind::Value, "INT", kCatIndex, "allowed mismatches against a blacklisted index", false, false},

    // ---- paired-end overlap ----------------------------------------------
    {'c', "correction", Kind::Flag, nullptr, kCatPair, "fix mismatched bases using the mate's overlap", true, false},
    {'\0', "overlap_len_require", Kind::Value, "INT", kCatPair, "shortest usable overlap, default 30", false, false},
    {'\0', "overlap_diff_limit", Kind::Value, "INT", kCatPair, "max mismatched bases in the overlap, default 5", false, false},
    {'\0', "overlap_diff_percent_limit", Kind::Value, "INT", kCatPair, "max % mismatch in the overlap, default 20", false, false},

    // ---- UMI -------------------------------------------------------------
    {'U', "umi", Kind::Flag, nullptr, kCatUmi, "enable UMI extraction into the read name", false, false},
    {'\0', "umi_loc", Kind::Value, "WHERE", kCatUmi, "index1|index2|read1|read2|per_index|per_read", false, false},
    {'\0', "umi_len", Kind::Value, "INT", kCatUmi, "UMI length when it lives inside the read", false, false},
    {'\0', "umi_prefix", Kind::Value, "STR", kCatUmi, "string placed in front of the UMI in the name", false, false},
    {'\0', "umi_skip", Kind::Value, "INT", kCatUmi, "bases to drop between the UMI and the insert", false, false},
    {'\0', "umi_delim", Kind::Value, "STR", kCatUmi, "delimiter between name and UMI, default ':'", false, false},

    // ---- duplication -----------------------------------------------------
    {'D', "dedup", Kind::Flag, nullptr, kCatDup, "remove duplicated reads", true, false},
    {'\0', "dup_calc_accuracy", Kind::Value, "INT", kCatDup, "duplication accuracy 1..6 (more = more memory)", false, false},
    {'\0', "dont_eval_duplication", Kind::Flag, nullptr, kCatDup, "skip the duplication rate estimate", false, false},

    // ---- overrepresentation ----------------------------------------------
    {'p', "overrepresentation_analysis", Kind::Flag, nullptr, kCatOver, "find overrepresented sequences", true, false},
    {'P', "overrepresentation_sampling", Kind::Value, "INT", kCatOver, "sample 1 read in INT, default 20", false, false},

    // ---- reporting -------------------------------------------------------
    {'j', "json", Kind::Value, "FILE", kCatReport, "JSON report path, default scepter.json", true, false},
    {'h', "html", Kind::Value, "FILE", kCatReport, "HTML report path, default scepter.html", true, false},
    {'R', "report_title", Kind::Value, "STR", kCatReport, "title shown at the top of the HTML report", true, false},
    {'\0', "tsv", Kind::Value, "FILE", kCatReport, "also write a flat TSV summary (scepter only)", false, false},
    {'\0', "report", Kind::Value, "FILE", kCatReport, "re-render an existing JSON report and exit", false, false},
    {'\0', "html-out", Kind::Value, "FILE", kCatReport, "destination for --report re-rendering", false, false},
    {'\0', "no-summary", Kind::Flag, nullptr, kCatReport, "suppress the end-of-run terminal summary", false, false},
    {'\0', "quiet", Kind::Flag, nullptr, kCatReport, "only print warnings and errors", true, false},
    {'\0', "no-color", Kind::Flag, nullptr, kCatReport, "disable ANSI colour in terminal output", false, false},

    // ---- splitting -------------------------------------------------------
    {'s', "split", Kind::Value, "INT", kCatSplit, "split output into INT files (2..999)", true, false},
    {'S', "split_by_lines", Kind::Value, "INT", kCatSplit, "split output every INT lines (>= 1000)", false, false},
    {'d', "split_prefix_digits", Kind::Value, "INT", kCatSplit, "zero-padding width of split file numbers", false, false},

    // ---- deep analysis ---------------------------------------------------
    {'\0', "genome-size", Kind::Value, "N", kCatDeep, "expected genome size for coverage (e.g. 4.6m)", true, false},
    {'\0', "kmer-size", Kind::Value, "INT", kCatDeep, "k for the k-mer spectrum, default 21", false, false},
    {'\0', "no-deep", Kind::Flag, nullptr, kCatDeep, "skip every extra analysis (fastp-equivalent QC)", true, false},
    {'\0', "no-tile", Kind::Flag, nullptr, kCatDeep, "skip per-tile quality maps", false, false},
    {'\0', "no-kmer", Kind::Flag, nullptr, kCatDeep, "skip the k-mer spectrum", false, false},
    {'\0', "no-error-profile", Kind::Flag, nullptr, kCatDeep, "skip the per-cycle error profile", false, false},
    {'\0', "no-insert-size", Kind::Flag, nullptr, kCatDeep, "skip insert-size estimation", false, false},
    {'\0', "no-dup-curve", Kind::Flag, nullptr, kCatDeep, "skip the library complexity curve", false, false},
    {'\0', "no-contaminant-screen", Kind::Flag, nullptr, kCatDeep, "skip the contaminant screen", false, false},
    {'\0', "sample-reads", Kind::Value, "N", kCatDeep, "reads sampled for auto-detection, default 200000", false, false},
    {'\0', "sample-head", Kind::Flag, nullptr, kCatDeep, "sample only the head of the file (old, faster)", false, false},

    // ---- contamination screening ----------------------------------------
    {'\0', "check-contamination", Kind::Flag, nullptr, kCatContam, "screen the library against the 23-species panel", true, false},
    {'\0', "contam-db", Kind::Value, "FILE", kCatContam, "species sketch to use instead of the shipped one", false, false},
    {'\0', "contam-min-fraction", Kind::Value, "F", kCatContam, "reporting floor for a species, default 0.01", false, false},

    // ---- execution -------------------------------------------------------
    {'w', "thread", Kind::Value, "INT", kCatExec, "worker threads (alias --threads), default auto", true, false},
    {'\0', "threads", Kind::Value, "INT", kCatExec, "alias of --thread", false, false},
    {'V', "verbose", Kind::Flag, nullptr, kCatExec, "print progress and per-stage detail", true, false},

    // ---- presets, modes, help --------------------------------------------
    {'\0', "preset", Kind::Value, "NAME", kCatHelp, "start from a named configuration (see PRESETS)", true, false},
    {'\0', "qc-only", Kind::Flag, nullptr, kCatHelp, "analyse only; write no FASTQ output", true, false},
    {'\0', "dry-run", Kind::Flag, nullptr, kCatHelp, "print the resolved configuration and exit", true, false},
    {'\0', "explain", Kind::Flag, nullptr, kCatHelp, "print the resolved configuration, then run", true, false},
    {'?', "help", Kind::Flag, nullptr, kCatHelp, "show this help", true, false},
    {'\0', "help-all", Kind::Flag, nullptr, kCatHelp, "show every option, grouped by category", true, false},
    {'\0', "version", Kind::Flag, nullptr, kCatHelp, "print the version and exit", true, false},

    // ---- deprecated fastp spellings, accepted and ignored ----------------
    {'\0', "cut_by_quality5", Kind::Flag, nullptr, kCatCut, "", false, true},
    {'\0', "cut_by_quality3", Kind::Flag, nullptr, kCatCut, "", false, true},
    {'\0', "cut_by_quality_aggressive", Kind::Flag, nullptr, kCatCut, "", false, true},
    {'\0', "discard_unmerged", Kind::Flag, nullptr, kCatIO, "", false, true},
};

struct ParseState {
    bool help = false;
    bool helpAll = false;
    bool version = false;
};

// '-' and '_' are interchangeable in long names, so --dry_run and --split-by-lines
// both work without adding table entries.
std::string normalizeName(std::string s) {
    s = util::toLower(std::move(s));
    std::replace(s.begin(), s.end(), '-', '_');
    return s;
}

const OptSpec* findLong(const std::string& name) {
    for (const auto& o : kOpts) {
        if (name == o.lg) return &o;
    }
    std::string norm = normalizeName(name);
    for (const auto& o : kOpts) {
        if (norm == normalizeName(o.lg)) return &o;
    }
    return nullptr;
}

const OptSpec* findShort(char c) {
    for (const auto& o : kOpts) {
        if (o.sh != '\0' && o.sh == c) return &o;
    }
    return nullptr;
}

size_t editDistance(const std::string& a, const std::string& b) {
    std::vector<size_t> prev(b.size() + 1), cur(b.size() + 1);
    for (size_t j = 0; j <= b.size(); ++j) prev[j] = j;
    for (size_t i = 1; i <= a.size(); ++i) {
        cur[0] = i;
        for (size_t j = 1; j <= b.size(); ++j) {
            size_t cost = (a[i - 1] == b[j - 1]) ? 0u : 1u;
            cur[j] = std::min(std::min(prev[j] + 1, cur[j - 1] + 1), prev[j - 1] + cost);
        }
        prev.swap(cur);
    }
    return prev[b.size()];
}

std::string unknownLongMessage(const std::string& name) {
    std::string norm = normalizeName(name);
    std::string best;
    size_t bestDist = std::numeric_limits<size_t>::max();
    for (const auto& o : kOpts) {
        if (o.deprecated) continue;
        size_t d = editDistance(norm, normalizeName(o.lg));
        if (d < bestDist) {
            bestDist = d;
            best = o.lg;
        }
    }
    std::string msg = "unknown option '--" + name + "'";
    size_t tolerance = std::max<size_t>(2, name.size() / 3);
    if (!best.empty() && bestDist <= tolerance) {
        msg += "; did you mean '--" + best + "'?";
    } else {
        msg += ". Run 'scepter --help-all' to list every option.";
    }
    return msg;
}

std::string unknownShortMessage(char c) {
    std::string msg = "unknown option '-";
    msg += c;
    msg += "'";
    char flipped = std::isalpha(static_cast<unsigned char>(c))
                       ? (std::islower(static_cast<unsigned char>(c))
                              ? static_cast<char>(std::toupper(static_cast<unsigned char>(c)))
                              : static_cast<char>(std::tolower(static_cast<unsigned char>(c))))
                       : c;
    const OptSpec* alt = (flipped != c) ? findShort(flipped) : nullptr;
    if (alt) {
        msg += "; did you mean '-";
        msg += flipped;
        msg += "' (--";
        msg += alt->lg;
        msg += ")?";
    } else {
        msg += ". Run 'scepter --help-all' to list every option.";
    }
    return msg;
}

bool toLongLong(const std::string& s, long long& out) {
    if (s.empty()) return false;
    char* end = nullptr;
    long long v = std::strtoll(s.c_str(), &end, 10);
    if (end == s.c_str() || end == nullptr || *end != '\0') return false;
    out = v;
    return true;
}

// Accepts a bare number or one with a k/m/g suffix, e.g. "4.6m" -> 4600000.
bool parseSize(const std::string& s, long long& out) {
    if (s.empty()) return false;
    char* end = nullptr;
    double v = std::strtod(s.c_str(), &end);
    if (end == s.c_str() || v < 0) return false;
    std::string suffix = util::toLower(util::trim(std::string(end)));
    double mul = 1.0;
    if (suffix.empty() || suffix == "b" || suffix == "bp") mul = 1.0;
    else if (suffix == "k" || suffix == "kb") mul = 1e3;
    else if (suffix == "m" || suffix == "mb") mul = 1e6;
    else if (suffix == "g" || suffix == "gb") mul = 1e9;
    else return false;
    out = static_cast<long long>(v * mul + 0.5);
    return true;
}

// -1 unrecognised, 0 false, 1 true.
int parseBoolWord(const std::string& s) {
    std::string v = util::toLower(s);
    if (v == "1" || v == "true" || v == "yes" || v == "on") return 1;
    if (v == "0" || v == "false" || v == "no" || v == "off") return 0;
    return -1;
}

bool parseUmiLoc(const std::string& s, UmiLoc& out) {
    std::string v = normalizeName(s);
    if (v == "none") out = UmiLoc::None;
    else if (v == "index1") out = UmiLoc::Index1;
    else if (v == "index2") out = UmiLoc::Index2;
    else if (v == "read1") out = UmiLoc::Read1;
    else if (v == "read2") out = UmiLoc::Read2;
    else if (v == "per_index") out = UmiLoc::PerIndex;
    else if (v == "per_read") out = UmiLoc::PerRead;
    else return false;
    return true;
}

void deprecationNote(const std::string& name, const char* modern) {
    std::fprintf(stderr, "%snote:%s --%s is deprecated and ignored; use %s instead.\n",
                 util::Color::yellow(), util::Color::reset(), name.c_str(), modern);
}

bool applyOption(const OptSpec& spec, const std::string& val, Options& opt, ParseState& st,
                 std::string& error) {
    const std::string n = spec.lg;

    auto badInt = [&](const char* what) {
        error = "option --" + n + " expects " + what + ", got '" + val + "'";
        return false;
    };
    auto intVal = [&](int lo, int hi, int& dst) -> bool {
        long long v = 0;
        if (!toLongLong(val, v)) return badInt("an integer");
        if (v < lo || v > hi) {
            error = "option --" + n + " must be between " + std::to_string(lo) + " and " +
                    std::to_string(hi) + " (got " + val + ")";
            return false;
        }
        dst = static_cast<int>(v);
        return true;
    };
    auto longVal = [&](long long lo, long long hi, long long& dst) -> bool {
        long long v = 0;
        if (!toLongLong(val, v)) return badInt("an integer");
        if (v < lo || v > hi) {
            error = "option --" + n + " must be between " + std::to_string(lo) + " and " +
                    std::to_string(hi) + " (got " + val + ")";
            return false;
        }
        dst = v;
        return true;
    };

    constexpr int kBig = 1000000000;

    // ---- input / output --------------------------------------------------
    if (n == "in1") { opt.in1 = val; return true; }
    if (n == "out1") { opt.out1 = val; return true; }
    if (n == "in2") { opt.in2 = val; return true; }
    if (n == "out2") { opt.out2 = val; return true; }
    if (n == "out-dir") { opt.outDir = val; return true; }
    if (n == "unpaired1") { opt.unpaired1 = val; return true; }
    if (n == "unpaired2") { opt.unpaired2 = val; return true; }
    if (n == "overlapped_out") { opt.overlappedOut = val; return true; }
    if (n == "failed_out") { opt.failedOut = val; return true; }
    if (n == "merge") { opt.merge.enabled = true; return true; }
    if (n == "merged_out") { opt.merge.out = val; opt.merge.enabled = true; return true; }
    if (n == "include_unmerged") { opt.merge.includeUnmerged = true; return true; }
    if (n == "phred64") { opt.phred64 = true; return true; }
    if (n == "compression") return intVal(1, 9, opt.compression);
    if (n == "stdin") { opt.stdinInput = true; return true; }
    if (n == "stdout") { opt.stdoutOutput = true; return true; }
    if (n == "interleaved_in") { opt.interleavedIn = true; return true; }
    if (n == "reads_to_process") return longVal(0, std::numeric_limits<long long>::max() / 2, opt.readsToProcess);
    if (n == "dont_overwrite") { opt.dontOverwrite = true; return true; }
    if (n == "fix_mgi_id") { opt.fixMgiId = true; return true; }

    // ---- adapter ---------------------------------------------------------
    if (n == "disable_adapter_trimming") { opt.adapter.enabled = false; return true; }
    if (n == "adapter_sequence") { opt.adapter.sequence = val; return true; }
    if (n == "adapter_sequence_r2") { opt.adapter.sequenceR2 = val; return true; }
    if (n == "adapter_fasta") { opt.adapter.fastaFile = val; return true; }
    if (n == "detect_adapter_for_pe") { opt.adapter.detectForPE = true; return true; }
    if (n == "allow_gap_overlap_trimming") { opt.adapter.allowGapOverlapTrimming = true; return true; }

    // ---- global trimming -------------------------------------------------
    if (n == "trim_front1") return intVal(0, kBig, opt.trim.front1);
    if (n == "trim_tail1") return intVal(0, kBig, opt.trim.tail1);
    if (n == "max_len1") return intVal(0, kBig, opt.trim.maxLen1);
    if (n == "trim_front2") return intVal(0, kBig, opt.trim.front2);
    if (n == "trim_tail2") return intVal(0, kBig, opt.trim.tail2);
    if (n == "max_len2") return intVal(0, kBig, opt.trim.maxLen2);

    // ---- duplication -----------------------------------------------------
    if (n == "dedup") { opt.duplication.dedup = true; return true; }
    if (n == "dup_calc_accuracy") return intVal(1, 6, opt.duplication.accuracyLevel);
    if (n == "dont_eval_duplication") { opt.duplication.evaluate = false; return true; }

    // ---- poly tails ------------------------------------------------------
    if (n == "trim_poly_g") { opt.polyG.forced = true; return true; }
    if (n == "poly_g_min_len") return intVal(1, 1000, opt.polyG.minLen);
    if (n == "disable_trim_poly_g") { opt.polyG.disabled = true; return true; }
    if (n == "trim_poly_x") { opt.polyX.enabled = true; return true; }
    if (n == "poly_x_min_len") return intVal(1, 1000, opt.polyX.minLen);

    // ---- window cutting --------------------------------------------------
    if (n == "cut_front") { opt.cut.front = true; return true; }
    if (n == "cut_tail") { opt.cut.tail = true; return true; }
    if (n == "cut_right") { opt.cut.right = true; return true; }
    if (n == "cut_window_size") return intVal(1, 1000, opt.cut.windowSize);
    if (n == "cut_mean_quality") return intVal(1, 36, opt.cut.meanQuality);
    if (n == "cut_front_window_size") return intVal(1, 1000, opt.cut.frontWindow);
    if (n == "cut_front_mean_quality") return intVal(1, 36, opt.cut.frontQuality);
    if (n == "cut_tail_window_size") return intVal(1, 1000, opt.cut.tailWindow);
    if (n == "cut_tail_mean_quality") return intVal(1, 36, opt.cut.tailQuality);
    if (n == "cut_right_window_size") return intVal(1, 1000, opt.cut.rightWindow);
    if (n == "cut_right_mean_quality") return intVal(1, 36, opt.cut.rightQuality);

    // ---- quality filtering -----------------------------------------------
    if (n == "disable_quality_filtering") { opt.qualityFilter.enabled = false; return true; }
    if (n == "qualified_quality_phred") return intVal(0, 60, opt.qualityFilter.qualifiedPhred);
    if (n == "unqualified_percent_limit") return intVal(0, 100, opt.qualityFilter.unqualifiedPercentLimit);
    if (n == "n_base_limit") return intVal(0, kBig, opt.qualityFilter.nBaseLimit);
    if (n == "average_qual") return intVal(0, 60, opt.qualityFilter.averageQual);

    // ---- length filtering ------------------------------------------------
    if (n == "disable_length_filtering") { opt.lengthFilter.enabled = false; return true; }
    if (n == "length_required") return intVal(0, kBig, opt.lengthFilter.required);
    if (n == "length_limit") return intVal(0, kBig, opt.lengthFilter.limit);

    // ---- complexity ------------------------------------------------------
    if (n == "low_complexity_filter") { opt.complexityFilter.enabled = true; return true; }
    if (n == "complexity_threshold") {
        opt.complexityFilter.enabled = true;
        return intVal(0, 100, opt.complexityFilter.threshold);
    }

    // ---- index filtering -------------------------------------------------
    if (n == "filter_by_index1") {
        opt.indexFilter.file1 = val;
        opt.indexFilter.enabled = true;
        return true;
    }
    if (n == "filter_by_index2") {
        opt.indexFilter.file2 = val;
        opt.indexFilter.enabled = true;
        return true;
    }
    if (n == "filter_by_index_threshold") return intVal(0, 100, opt.indexFilter.threshold);

    // ---- paired-end overlap ----------------------------------------------
    if (n == "correction") { opt.correction.enabled = true; return true; }
    if (n == "overlap_len_require") return intVal(1, kBig, opt.overlap.lenRequire);
    if (n == "overlap_diff_limit") return intVal(0, kBig, opt.overlap.diffLimit);
    if (n == "overlap_diff_percent_limit") return intVal(0, 100, opt.overlap.diffPercentLimit);

    // ---- UMI -------------------------------------------------------------
    if (n == "umi") { opt.umi.enabled = true; return true; }
    if (n == "umi_loc") {
        if (!parseUmiLoc(val, opt.umi.loc)) {
            error = "unknown --umi_loc '" + val +
                    "'; expected index1, index2, read1, read2, per_index or per_read";
            return false;
        }
        return true;
    }
    if (n == "umi_len") return intVal(0, 100, opt.umi.len);
    if (n == "umi_prefix") { opt.umi.prefix = val; return true; }
    if (n == "umi_skip") return intVal(0, 1000, opt.umi.skip);
    if (n == "umi_delim") { opt.umi.delim = val; return true; }

    // ---- overrepresentation ----------------------------------------------
    if (n == "overrepresentation_analysis") { opt.overrep.enabled = true; return true; }
    if (n == "overrepresentation_sampling") return intVal(1, 10000, opt.overrep.sampling);

    // ---- reporting -------------------------------------------------------
    if (n == "json") { opt.jsonFile = val; return true; }
    if (n == "html") { opt.htmlFile = val; return true; }
    if (n == "report_title") { opt.reportTitle = val; return true; }
    if (n == "tsv") { opt.tsvFile = val; return true; }
    if (n == "report") {
        opt.jsonFile = val;
        opt.mode = RunMode::ReportOnly;
        return true;
    }
    if (n == "html-out") { opt.htmlFile = val; return true; }
    if (n == "no-summary") { opt.noSummary = true; return true; }
    if (n == "quiet") { opt.quiet = true; return true; }
    if (n == "no-color") { util::Color::setEnabled(false); return true; }

    // ---- splitting -------------------------------------------------------
    if (n == "split") {
        if (!intVal(2, 999, opt.split.number)) return false;
        opt.split.enabled = true;
        return true;
    }
    if (n == "split_by_lines") {
        long long v = 0;
        if (!toLongLong(val, v)) return badInt("an integer");
        if (v < 1000) {
            error = "option --split_by_lines must be at least 1000 (got " + val + ")";
            return false;
        }
        opt.split.byLines = v;
        opt.split.enabled = true;
        return true;
    }
    if (n == "split_prefix_digits") return intVal(0, 10, opt.split.digits);

    // ---- deep analysis ---------------------------------------------------
    if (n == "genome-size") {
        if (!parseSize(val, opt.deep.genomeSize)) {
            error = "option --genome-size expects a size such as 4.6m, 3g or 4600000 (got '" +
                    val + "')";
            return false;
        }
        return true;
    }
    if (n == "kmer-size") return intVal(5, 31, opt.deep.kmerK);
    if (n == "no-deep") { opt.deep.disableAll = true; return true; }
    if (n == "no-tile") { opt.deep.perTile = false; return true; }
    if (n == "no-kmer") { opt.deep.kmerSpectrum = false; return true; }
    if (n == "no-error-profile") { opt.deep.errorProfile = false; return true; }
    if (n == "no-insert-size") { opt.deep.insertSize = false; return true; }
    if (n == "no-dup-curve") { opt.deep.duplicationCurve = false; return true; }
    if (n == "no-contaminant-screen") { opt.deep.contaminantScreen = false; return true; }
    if (n == "sample-reads") return longVal(1000, 100000000LL, opt.sampling.reads);
    if (n == "sample-head") { opt.sampling.headOnly = true; return true; }

    // ---- contamination screening -----------------------------------------
    if (n == "check-contamination") { opt.contam.enabled = true; return true; }
    if (n == "contam-db") { opt.contam.dbPath = val; opt.contam.enabled = true; return true; }
    if (n == "contam-min-fraction") {
        char* end = nullptr;
        double v = std::strtod(val.c_str(), &end);
        if (end == val.c_str() || end == nullptr || *end != '\0' || v < 0.0 || v > 1.0) {
            error = "option --contam-min-fraction expects a fraction between 0 and 1 (got '" +
                    val + "')";
            return false;
        }
        opt.contam.minFraction = v;
        return true;
    }

    // ---- execution -------------------------------------------------------
    if (n == "thread" || n == "threads") return intVal(1, 1024, opt.threads);
    if (n == "verbose") { opt.verbose = true; return true; }

    // ---- presets, modes, help --------------------------------------------
    if (n == "preset") return true;  // already applied in the pre-scan
    if (n == "qc-only") { opt.mode = RunMode::QcOnly; return true; }
    if (n == "dry-run") { opt.dryRun = true; return true; }
    if (n == "explain") { opt.explain = true; return true; }
    if (n == "help") { st.help = true; return true; }
    if (n == "help-all") { st.helpAll = true; return true; }
    if (n == "version") { st.version = true; return true; }

    // ---- deprecated ------------------------------------------------------
    if (n == "cut_by_quality5") { deprecationNote(n, "--cut_front (-5)"); return true; }
    if (n == "cut_by_quality3") { deprecationNote(n, "--cut_tail (-3)"); return true; }
    if (n == "cut_by_quality_aggressive") { deprecationNote(n, "--cut_right (-r)"); return true; }
    if (n == "discard_unmerged") {
        deprecationNote(n, "the default merge behaviour (--include_unmerged keeps them)");
        return true;
    }

    error = "option --" + n + " is recognised but not wired up";  // unreachable
    return false;
}

// Candidate read-2 filenames for a read-1 path, most specific first.
std::vector<std::string> mateCandidates(const std::string& path) {
    struct Pattern { const char* from; const char* to; };
    static const Pattern kPatterns[] = {
        {"_R1", "_R2"}, {".R1.", ".R2."}, {"_1.", "_2."},
    };
    std::vector<std::string> out;
    for (const auto& p : kPatterns) {
        const std::string from = p.from;
        size_t pos = path.rfind(from);
        if (pos == std::string::npos) continue;
        std::string cand = path;
        cand.replace(pos, from.size(), p.to);
        if (cand != path) out.push_back(cand);
    }
    return out;
}

// ---------------------------------------------------------------------------
// help rendering
// ---------------------------------------------------------------------------

std::string plainOptText(const OptSpec& o) {
    std::string s;
    if (o.sh != '\0') {
        s += '-';
        s += o.sh;
        s += ", ";
    } else {
        s += "    ";
    }
    s += "--";
    s += o.lg;
    if (o.kind == Kind::Value && o.meta) {
        s += ' ';
        s += o.meta;
    }
    return s;
}

// Widest option text among the lines that will actually be printed, so the
// description column sits just clear of the longest name.
int describeColumn(bool full) {
    size_t widest = 0;
    for (const auto& o : kOpts) {
        if (o.deprecated) continue;
        if (!full && !o.common) continue;
        widest = std::max(widest, plainOptText(o).size());
    }
    return static_cast<int>(widest) + 2;
}

void printOptLine(std::FILE* out, const OptSpec& o, int column) {
    std::string colored;
    if (o.sh != '\0') {
        colored += util::Color::green();
        colored += '-';
        colored += o.sh;
        colored += util::Color::reset();
        colored += ", ";
    } else {
        colored += "    ";
    }
    colored += util::Color::green();
    colored += "--";
    colored += o.lg;
    colored += util::Color::reset();
    if (o.kind == Kind::Value && o.meta) {
        colored += ' ';
        colored += util::Color::yellow();
        colored += o.meta;
        colored += util::Color::reset();
    }
    int pad = column - static_cast<int>(plainOptText(o).size());
    if (pad < 1) pad = 1;
    std::fprintf(out, "  %s%*s%s\n", colored.c_str(), pad, "", o.help);
}

void printHeading(std::FILE* out, const char* text) {
    std::fprintf(out, "\n%s%s%s%s\n", util::Color::bold(), util::Color::cyan(), text,
                 util::Color::reset());
}

void printExample(std::FILE* out, const char* command, const char* what) {
    std::fprintf(out, "  %s%s%s\n      %s%s%s\n", util::Color::green(), command,
                 util::Color::reset(), util::Color::gray(), what, util::Color::reset());
}

void printPresets(std::FILE* out) {
    for (const auto& p : presetList()) {
        int pad = 16 - static_cast<int>(p.first.size());
        if (pad < 1) pad = 1;
        std::fprintf(out, "  %s%s%s%*s%s\n", util::Color::magenta(), p.first.c_str(),
                     util::Color::reset(), pad, "", p.second.c_str());
    }
}

void printBanner(std::FILE* out) {
    std::fprintf(out, "%s%sscepter%s %s - fast FASTQ preprocessing with deep QC%s\n",
                 util::Color::bold(), util::Color::cyan(), util::Color::reset(),
                 kScepterVersion, util::Color::reset());
    std::fprintf(out, "%sa drop-in replacement for fastp: every fastp flag works unchanged%s\n",
                 util::Color::gray(), util::Color::reset());
}

}  // namespace

// ---------------------------------------------------------------------------

void printVersion() {
    std::fprintf(stdout, "scepter %s\n", kScepterVersion);
    std::fprintf(stdout, "drop-in replacement for fastp; C++17, zlib %s\n", ZLIB_VERSION);
}

void printUsage(bool full) {
    // Help goes to stdout so it can be piped; only colourise when that is a terminal.
    if (!::isatty(1)) util::Color::setEnabled(false);
    std::FILE* out = stdout;

    printBanner(out);

    printHeading(out, "USAGE");
    std::fprintf(out, "  scepter %sREAD1%s [%sREAD2%s] [options]\n", util::Color::yellow(),
                 util::Color::reset(), util::Color::yellow(), util::Color::reset());
    std::fprintf(out, "  scepter -i %sIN1%s -o %sOUT1%s [-I %sIN2%s -O %sOUT2%s] [options]\n",
                 util::Color::yellow(), util::Color::reset(), util::Color::yellow(),
                 util::Color::reset(), util::Color::yellow(), util::Color::reset(),
                 util::Color::yellow(), util::Color::reset());

    if (!full) {
        printHeading(out, "QUICK START");
        printExample(out, "scepter sample_R1.fq.gz",
                     "QC report only; sample_R2.fq.gz is picked up automatically if present");
        printExample(out, "scepter sample_R1.fq.gz sample_R2.fq.gz --out-dir clean/",
                     "trim both mates, outputs auto-named inside clean/");
        printExample(out, "scepter -i R1.fq.gz -o c1.fq.gz -I R2.fq.gz -O c2.fq.gz -w 8",
                     "classic fastp-style invocation, 8 threads");
        printExample(out, "scepter R1.fq.gz R2.fq.gz --preset assembly --out-dir asm/",
                     "settings tuned for feeding a de novo assembler");
        printExample(out, "scepter R1.fq.gz --preset novaseq --genome-size 4.6m --dry-run",
                     "show the resolved configuration (including coverage) without running");

        printHeading(out, "PRESETS");
        std::fprintf(out, "  %sapplied before your other flags, so anything you set explicitly wins%s\n",
                     util::Color::gray(), util::Color::reset());
        printPresets(out);
    }

    const int column = describeColumn(full);
    for (const char* cat : kCategoryOrder) {
        bool any = false;
        for (const auto& o : kOpts) {
            if (o.deprecated || std::strcmp(o.cat, cat) != 0) continue;
            if (!full && !o.common) continue;
            any = true;
            break;
        }
        if (!any) continue;
        printHeading(out, cat);
        for (const auto& o : kOpts) {
            if (o.deprecated || std::strcmp(o.cat, cat) != 0) continue;
            if (!full && !o.common) continue;
            printOptLine(out, o, column);
        }
    }

    std::fprintf(out, "\n");
    if (!full) {
        std::fprintf(out, "%sThat is the short list. 'scepter --help-all' shows all %zu options.%s\n",
                     util::Color::gray(), sizeof(kOpts) / sizeof(kOpts[0]) - 4,
                     util::Color::reset());
    } else {
        std::fprintf(out,
                     "%sLong options also accept --opt=value, and '-' and '_' are "
                     "interchangeable in long names.%s\n",
                     util::Color::gray(), util::Color::reset());
    }
}

// ---------------------------------------------------------------------------

int parseCommandLine(int argc, char** argv, Options& opt, std::string& error) {
    error.clear();

    std::vector<std::string> args;
    for (int i = 1; i < argc; ++i) args.emplace_back(argv[i]);

    if (args.empty()) {
        printUsage(false);
        return 1;
    }

    // Colour must be settled before anything is printed, and the preset must be
    // applied before the flags that are meant to override it.
    for (const std::string& a : args) {
        if (a == "--no-color" || a == "--no_color") util::Color::setEnabled(false);
    }
    for (size_t i = 0; i < args.size(); ++i) {
        const std::string& a = args[i];
        std::string name;
        bool hasValue = false;
        if (util::startsWith(a, "--preset=") || util::startsWith(a, "--preset:")) {
            name = a.substr(9);
            hasValue = true;
        } else if (a == "--preset") {
            if (i + 1 >= args.size()) {
                error = "--preset needs a name; run 'scepter --help' to see the list";
                return -1;
            }
            name = args[i + 1];
            hasValue = true;
        }
        if (!hasValue) continue;
        if (!opt.applyPreset(name, error)) return -1;
    }

    ParseState st;
    std::vector<std::string> positional;
    bool optionsEnded = false;

    for (size_t i = 0; i < args.size() && !st.help && !st.helpAll && !st.version; ++i) {
        const std::string a = args[i];

        if (!optionsEnded && a == "--") {
            optionsEnded = true;
            continue;
        }
        if (optionsEnded || a.size() < 2 || a[0] != '-') {
            positional.push_back(a);
            continue;
        }

        if (util::startsWith(a, "--")) {
            const std::string body = a.substr(2);
            std::string name = body, value;
            bool inlineValue = false;
            size_t eq = body.find('=');
            if (eq != std::string::npos) {
                name = body.substr(0, eq);
                value = body.substr(eq + 1);
                inlineValue = true;
            }
            const OptSpec* spec = findLong(name);
            if (!spec) {
                error = unknownLongMessage(name);
                return -1;
            }
            if (spec->kind == Kind::Value) {
                if (!inlineValue) {
                    if (i + 1 >= args.size()) {
                        error = "option --" + name + " needs a value";
                        return -1;
                    }
                    value = args[++i];
                }
            } else if (inlineValue) {
                int b = parseBoolWord(value);
                if (b < 0) {
                    error = "option --" + name + " is a switch; '" + value +
                            "' is not one of true/false/on/off";
                    return -1;
                }
                if (b == 0) continue;
                value.clear();
            }
            if (!applyOption(*spec, value, opt, st, error)) return -1;
            continue;
        }

        // Bundled short options: -53, -q20, -q 20, -w8.
        for (size_t c = 1; c < a.size(); ++c) {
            const OptSpec* spec = findShort(a[c]);
            if (!spec) {
                error = unknownShortMessage(a[c]);
                return -1;
            }
            if (spec->kind == Kind::Value) {
                std::string value;
                if (c + 1 < a.size()) {
                    value = a.substr(c + 1);
                    if (!value.empty() && value[0] == '=') value.erase(0, 1);
                } else if (i + 1 < args.size()) {
                    value = args[++i];
                } else if (a[c] == 'h') {
                    // fastp spells --html as -h; a trailing bare -h is a request for help.
                    st.help = true;
                    break;
                } else {
                    error = std::string("option -") + a[c] + " (--" + spec->lg + ") needs a value";
                    return -1;
                }
                if (!applyOption(*spec, value, opt, st, error)) return -1;
                break;  // the rest of the token was the value
            }
            if (!applyOption(*spec, std::string(), opt, st, error)) return -1;
        }
    }

    if (st.version) {
        printVersion();
        return 1;
    }
    if (st.help || st.helpAll) {
        printUsage(st.helpAll);
        return 1;
    }

    for (const std::string& p : positional) {
        if (p == "-") {
            opt.stdinInput = true;
            continue;
        }
        if (opt.in1.empty()) {
            opt.in1 = p;
        } else if (opt.in2.empty()) {
            // Left to finalize() to reject when --interleaved_in is also set, so the
            // user gets the specific conflict rather than "extra argument".
            opt.in2 = p;
        } else {
            error = "unexpected extra argument '" + p +
                    "'; scepter takes at most two input files";
            return -1;
        }
    }

    if (opt.in2.empty() && !opt.in1.empty() && !opt.interleavedIn && !opt.stdinInput) {
        for (const std::string& cand : mateCandidates(opt.in1)) {
            if (!util::fileExists(cand)) continue;
            opt.in2 = cand;
            if (!opt.quiet) {
                std::fprintf(stderr, "%sauto-detected read 2:%s %s\n", util::Color::cyan(),
                             util::Color::reset(), cand.c_str());
            }
            break;
        }
    }

    if (opt.mode == RunMode::ReportOnly) {
        if (opt.jsonFile.empty()) {
            error = "--report needs the JSON file to regenerate the report from";
            return -1;
        }
        if (!util::fileExists(opt.jsonFile)) {
            error = "--report file '" + opt.jsonFile + "' does not exist";
            return -1;
        }
        // finalize() deliberately invents no report names in this mode, but the
        // re-render still needs a destination.
        if (opt.htmlFile.empty()) {
            std::string base = opt.jsonFile;
            if (util::endsWith(util::toLower(base), ".json")) base.resize(base.size() - 5);
            opt.htmlFile = base + ".html";
        }
    } else if (opt.in1.empty() && !opt.stdinInput) {
        error = "no input given. Pass a FASTQ file (scepter reads_R1.fq.gz), "
                "use -i/--in1, or read a stream with --stdin.";
        return -1;
    } else {
        const std::pair<const char*, const std::string*> inputs[] = {
            {"-i/--in1", &opt.in1}, {"-I/--in2", &opt.in2}};
        for (const auto& in : inputs) {
            if (in.second->empty() || *in.second == "-") continue;
            if (!util::fileExists(*in.second)) {
                error = std::string("input file '") + *in.second + "' (" + in.first +
                        ") does not exist";
                return -1;
            }
        }
    }

    // finalize() is idempotent, so the caller re-running it is harmless. Printing
    // the description for --dry-run/--explain is left to the caller.
    if (!opt.finalize(error)) return -1;
    return 0;
}

}  // namespace fp
