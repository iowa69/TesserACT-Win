// Machine-readable reporting: JSON, TSV, the terminal summary, and rebuilding
// an HTML report from a previously written JSON report.
//
// The JSON layout is a strict superset of fastp's: every key name and nesting
// level fastp emits is reproduced verbatim so existing pipelines and MultiQC
// keep parsing it, and everything scepter adds lives under a single top-level
// "scepter" object that fastp-aware parsers ignore.

#include "report.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <map>
#include <sstream>
#include <string>
#include <type_traits>
#include <vector>

#include "util.h"

namespace fp {
namespace {

// ---------------------------------------------------------------------------
// Small numeric / string helpers
// ---------------------------------------------------------------------------

inline double safeDiv(double a, double b) { return b > 0.0 ? a / b : 0.0; }
inline double ratio(uint64_t a, uint64_t b) { return b ? static_cast<double>(a) / static_cast<double>(b) : 0.0; }

std::string sfmt(const char* f, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, f);
    std::vsnprintf(buf, sizeof(buf), f, ap);
    va_end(ap);
    return std::string(buf);
}

std::string pctStr(double frac) { return sfmt("%.2f%%", frac * 100.0); }

// Number of terminal columns a UTF-8 string occupies (box drawing and arrows
// are multi-byte but single-column).
size_t displayWidth(const std::string& s) {
    size_t n = 0;
    for (unsigned char c : s) {
        if ((c & 0xC0) != 0x80) ++n;
    }
    return n;
}

std::string jsonEscape(const std::string& in) {
    std::string out;
    out.reserve(in.size() + 8);
    for (unsigned char c : in) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b";  break;
            case '\f': out += "\\f";  break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", static_cast<unsigned>(c));
                    out += buf;
                } else {
                    out += static_cast<char>(c);
                }
        }
    }
    return out;
}

// Locale-independent number text. Whole values are emitted as integers so that
// counts never appear as "1e+06" to downstream parsers.
std::string fmtDouble(double v) {
    if (!std::isfinite(v)) return "null";
    if (std::fabs(v) < 1e15 && v == std::floor(v)) {
        return std::to_string(static_cast<long long>(v));
    }
    char buf[48];
    std::snprintf(buf, sizeof(buf), "%.6g", v);
    std::string s(buf);
    for (char& c : s) {
        if (c == ',') c = '.';   // guard against an exotic LC_NUMERIC
    }
    return s;
}

// ---------------------------------------------------------------------------
// Minimal streaming JSON writer (4-space indent, no trailing commas)
// ---------------------------------------------------------------------------

class JsonWriter {
public:
    explicit JsonWriter(std::ostream& os) : os_(os) {}

    void beginObject() { pre(); os_ << '{'; push(); }
    void beginObject(const std::string& k) { pre(k); os_ << '{'; push(); }
    void endObject() { pop('}'); }
    void beginArray() { pre(); os_ << '['; push(); }
    void beginArray(const std::string& k) { pre(k); os_ << '['; push(); }
    void endArray() { pop(']'); }

    void s(const std::string& k, const std::string& v) { pre(k); writeString(v); }
    void sv(const std::string& v) { pre(); writeString(v); }
    void b(const std::string& k, bool v) { pre(k); os_ << (v ? "true" : "false"); }
    void nul(const std::string& k) { pre(k); os_ << "null"; }

    template <class T> void n(const std::string& k, T v) { pre(k); writeNum(v); }
    template <class T> void nv(T v) { pre(); writeNum(v); }

    // Numeric arrays are written on a single line: per-cycle curves would
    // otherwise dominate the file.
    template <class C> void arr(const std::string& k, const C& c) { pre(k); inlineArray(c); }
    template <class C> void arrv(const C& c) { pre(); inlineArray(c); }

private:
    void push() { empty_.push_back(true); }

    void pop(char close) {
        if (empty_.empty()) { os_ << close; return; }
        bool wasEmpty = empty_.back();
        empty_.pop_back();
        if (!wasEmpty) { os_ << '\n' << std::string(empty_.size() * 4, ' '); }
        os_ << close;
    }

    void pre() {
        if (empty_.empty()) return;
        if (!empty_.back()) os_ << ',';
        empty_.back() = false;
        os_ << '\n' << std::string(empty_.size() * 4, ' ');
    }

    void pre(const std::string& k) { pre(); writeString(k); os_ << ": "; }

    void writeString(const std::string& v) { os_ << '"' << jsonEscape(v) << '"'; }

    template <class C> void inlineArray(const C& c) {
        os_ << '[';
        bool first = true;
        for (const auto& x : c) {
            if (!first) os_ << ',';
            first = false;
            writeNum(x);
        }
        os_ << ']';
    }

    template <class T> void writeNum(T v) {
        if constexpr (std::is_same_v<T, bool>) {
            os_ << (v ? "true" : "false");
        } else if constexpr (std::is_integral_v<T>) {
            if constexpr (std::is_signed_v<T>) os_ << static_cast<long long>(v);
            else os_ << static_cast<unsigned long long>(v);
        } else {
            os_ << fmtDouble(static_cast<double>(v));
        }
    }

    std::ostream& os_;
    std::vector<bool> empty_;
};

// ---------------------------------------------------------------------------
// Shared derived values
// ---------------------------------------------------------------------------

const char* const kFilterKeys[] = {
    "pass", "failed_length", "failed_too_long", "failed_quality", "failed_n_base",
    "failed_average_quality", "failed_complexity", "failed_index", "duplicated"
};
static_assert(sizeof(kFilterKeys) / sizeof(kFilterKeys[0]) ==
                  static_cast<size_t>(FilterResult::kCount),
              "filter key table out of sync with FilterResult");

inline uint64_t filterCount(const RunStats& s, FilterResult r) {
    return s.filterCounts[static_cast<size_t>(r)];
}

struct Combined {
    uint64_t reads = 0, bases = 0, q20 = 0, q30 = 0, gc = 0;
    double len1 = 0, len2 = 0;
};

Combined combine(const ReadSetStats& a, const ReadSetStats& b, bool paired) {
    Combined c;
    c.reads = a.reads + (paired ? b.reads : 0);
    c.bases = a.bases + (paired ? b.bases : 0);
    c.q20 = a.q20Bases + (paired ? b.q20Bases : 0);
    c.q30 = a.q30Bases + (paired ? b.q30Bases : 0);
    c.gc = a.gcBases + (paired ? b.gcBases : 0);
    c.len1 = ratio(a.bases, a.reads);
    c.len2 = paired ? ratio(b.bases, b.reads) : 0.0;
    return c;
}

struct Digest {
    bool paired = false;
    uint64_t readsIn = 0, readsOut = 0, basesIn = 0, basesOut = 0;
    double passRate = 0;
    Combined before, after;
    double q20Before = 0, q30Before = 0, q20After = 0, q30After = 0;
    double gcBefore = 0, gcAfter = 0;
    uint64_t adapterReads = 0, adapterBases = 0;
    double adapterRate = 0;
    double dupRate = 0;
    double duration = 0;
};

Digest makeDigest(const RunStats& s, const Options& opt) {
    Digest d;
    d.paired = opt.isPaired();
    d.before = combine(s.before1, s.before2, d.paired);
    d.after = combine(s.after1, s.after2, d.paired);
    d.readsIn = s.readsIn ? s.readsIn : d.before.reads;
    d.readsOut = s.readsOut ? s.readsOut : d.after.reads;
    d.basesIn = s.basesIn ? s.basesIn : d.before.bases;
    d.basesOut = s.basesOut ? s.basesOut : d.after.bases;
    d.passRate = ratio(d.readsOut, d.readsIn);
    d.q20Before = ratio(d.before.q20, d.before.bases);
    d.q30Before = ratio(d.before.q30, d.before.bases);
    d.q20After = ratio(d.after.q20, d.after.bases);
    d.q30After = ratio(d.after.q30, d.after.bases);
    d.gcBefore = ratio(d.before.gc, d.before.bases);
    d.gcAfter = ratio(d.after.gc, d.after.bases);
    d.adapterReads = s.adapterTrimmedReads1 + s.adapterTrimmedReads2;
    d.adapterBases = s.adapterTrimmedBases1 + s.adapterTrimmedBases2;
    d.adapterRate = ratio(d.adapterReads, d.readsIn);
    d.dupRate = s.duplication.duplicationRate;
    d.duration = s.durationSeconds;
    return d;
}

struct Verdict {
    std::string status = "pass";
    std::vector<std::string> reasons;
};

void raise(Verdict& v, const char* level, const std::string& reason) {
    v.reasons.push_back(reason);
    if (std::string(level) == "fail") v.status = "fail";
    else if (v.status != "fail") v.status = "warn";
}

Verdict computeVerdict(const RunStats& s, const Options& opt) {
    Verdict v;
    Digest d = makeDigest(s, opt);

    if (d.readsIn == 0) {
        raise(v, "fail", "no reads were processed");
        return v;
    }
    if (d.passRate < 0.50) {
        raise(v, "fail", sfmt("only %.1f%% of reads passed filtering", d.passRate * 100.0));
    } else if (d.passRate < 0.80) {
        raise(v, "warn", sfmt("%.1f%% of reads were discarded by filtering",
                              (1.0 - d.passRate) * 100.0));
    }
    if (d.after.bases > 0) {
        if (d.q30After < 0.50) {
            raise(v, "fail", sfmt("Q30 rate after filtering is %.1f%%", d.q30After * 100.0));
        } else if (d.q30After < 0.75) {
            raise(v, "warn", sfmt("Q30 rate after filtering is %.1f%%", d.q30After * 100.0));
        }
    }
    if (d.dupRate > 0.80) {
        raise(v, "fail", sfmt("duplication rate is %.1f%%", d.dupRate * 100.0));
    } else if (d.dupRate > 0.40) {
        raise(v, "warn", sfmt("duplication rate is %.1f%%", d.dupRate * 100.0));
    }
    if (d.adapterRate > 0.40) {
        raise(v, "warn", sfmt("%.1f%% of reads needed adapter trimming", d.adapterRate * 100.0));
    }
    if (s.tiles1.usable() && !s.tiles1.outlierTiles.empty()) {
        raise(v, "warn", sfmt("%zu tile(s) deviate from the flow-cell mean quality",
                              s.tiles1.outlierTiles.size()));
    }
    if (s.errorModel.hasData() && std::fabs(s.errorModel.calibrationBias) > 3.0) {
        raise(v, "warn", sfmt("reported quality is off by %.1f Q units empirically",
                              s.errorModel.calibrationBias));
    }
    if (s.kmers.reliable && s.kmers.errorKmerFraction > 0.60) {
        raise(v, "warn", sfmt("%.1f%% of distinct k-mers look like sequencing errors",
                              s.kmers.errorKmerFraction * 100.0));
    }
    double nRate = ratio(d.before.reads ? s.before1.nBases + s.before2.nBases : 0, d.before.bases);
    if (nRate > 0.01) {
        raise(v, "warn", sfmt("%.2f%% of input bases are N", nRate * 100.0));
    }
    return v;
}

std::string sequencingDesc(const RunStats& s, const Options& opt) {
    if (opt.isPaired()) {
        return sfmt("paired end (%zu cycles + %zu cycles)", s.before1.cycles, s.before2.cycles);
    }
    return sfmt("single end (%zu cycles)", s.before1.cycles);
}

const char* runModeName(RunMode m) {
    switch (m) {
        case RunMode::QcOnly: return "qc_only";
        case RunMode::ReportOnly: return "report_only";
        case RunMode::Process: break;
    }
    return "process";
}

RunMode runModeFromName(const std::string& s) {
    if (s == "qc_only") return RunMode::QcOnly;
    if (s == "report_only") return RunMode::ReportOnly;
    return RunMode::Process;
}

const char* umiLocName(UmiLoc l) {
    switch (l) {
        case UmiLoc::Index1: return "index1";
        case UmiLoc::Index2: return "index2";
        case UmiLoc::Read1: return "read1";
        case UmiLoc::Read2: return "read2";
        case UmiLoc::PerIndex: return "per_index";
        case UmiLoc::PerRead: return "per_read";
        case UmiLoc::None: break;
    }
    return "none";
}

UmiLoc umiLocFromName(const std::string& s) {
    if (s == "index1") return UmiLoc::Index1;
    if (s == "index2") return UmiLoc::Index2;
    if (s == "read1") return UmiLoc::Read1;
    if (s == "read2") return UmiLoc::Read2;
    if (s == "per_index") return UmiLoc::PerIndex;
    if (s == "per_read") return UmiLoc::PerRead;
    return UmiLoc::None;
}

std::string joinList(const std::vector<std::string>& v, const char* sep) {
    std::string out;
    for (size_t i = 0; i < v.size(); ++i) {
        if (i) out += sep;
        out += v[i];
    }
    return out;
}

// ---------------------------------------------------------------------------
// The effective configuration, visited once and rendered three ways (JSON,
// TSV, and read back when rebuilding a report).
// ---------------------------------------------------------------------------

template <class OptT, class V>
void visitConfig(OptT& o, V& v) {
    v.s("in1", o.in1);
    v.s("in2", o.in2);
    v.s("out1", o.out1);
    v.s("out2", o.out2);
    v.s("unpaired1", o.unpaired1);
    v.s("unpaired2", o.unpaired2);
    v.s("failed_out", o.failedOut);
    v.s("overlapped_out", o.overlappedOut);
    v.s("out_dir", o.outDir);
    v.b("stdin", o.stdinInput);
    v.b("stdout", o.stdoutOutput);
    v.b("interleaved_in", o.interleavedIn);
    v.b("dont_overwrite", o.dontOverwrite);
    v.b("fix_mgi_id", o.fixMgiId);
    v.b("phred64", o.phred64);
    v.i("compression", o.compression);
    v.l("reads_to_process", o.readsToProcess);

    v.s("json", o.jsonFile);
    v.s("html", o.htmlFile);
    v.s("tsv", o.tsvFile);
    v.s("report_title", o.reportTitle);
    v.b("verbose", o.verbose);
    v.b("quiet", o.quiet);
    v.b("no_summary", o.noSummary);

    v.i("threads", o.threads);
    v.mode("mode", o.mode);
    v.b("dry_run", o.dryRun);
    v.b("explain", o.explain);
    v.s("preset", o.preset);

    v.b("adapter_enabled", o.adapter.enabled);
    v.s("adapter_sequence", o.adapter.sequence);
    v.s("adapter_sequence_r2", o.adapter.sequenceR2);
    v.s("adapter_fasta", o.adapter.fastaFile);
    v.b("adapter_detect_for_pe", o.adapter.detectForPE);
    v.b("adapter_allow_gap_overlap_trimming", o.adapter.allowGapOverlapTrimming);
    v.s("adapter_resolved_r1", o.adapter.resolvedR1);
    v.s("adapter_resolved_r2", o.adapter.resolvedR2);
    v.b("adapter_detected_r1", o.adapter.detectedR1);
    v.b("adapter_detected_r2", o.adapter.detectedR2);

    v.i("trim_front1", o.trim.front1);
    v.i("trim_tail1", o.trim.tail1);
    v.i("max_len1", o.trim.maxLen1);
    v.i("trim_front2", o.trim.front2);
    v.i("trim_tail2", o.trim.tail2);
    v.i("max_len2", o.trim.maxLen2);

    v.b("polyg_forced", o.polyG.forced);
    v.b("polyg_disabled", o.polyG.disabled);
    v.b("polyg_enabled", o.polyG.enabled);
    v.i("polyg_min_len", o.polyG.minLen);
    v.b("polyx_enabled", o.polyX.enabled);
    v.i("polyx_min_len", o.polyX.minLen);

    v.b("cut_front", o.cut.front);
    v.b("cut_tail", o.cut.tail);
    v.b("cut_right", o.cut.right);
    v.i("cut_window_size", o.cut.windowSize);
    v.i("cut_mean_quality", o.cut.meanQuality);
    v.i("cut_front_window_size", o.cut.frontWindow);
    v.i("cut_front_mean_quality", o.cut.frontQuality);
    v.i("cut_tail_window_size", o.cut.tailWindow);
    v.i("cut_tail_mean_quality", o.cut.tailQuality);
    v.i("cut_right_window_size", o.cut.rightWindow);
    v.i("cut_right_mean_quality", o.cut.rightQuality);

    v.b("quality_filtering_enabled", o.qualityFilter.enabled);
    v.i("qualified_quality_phred", o.qualityFilter.qualifiedPhred);
    v.i("unqualified_percent_limit", o.qualityFilter.unqualifiedPercentLimit);
    v.i("n_base_limit", o.qualityFilter.nBaseLimit);
    v.i("average_qual", o.qualityFilter.averageQual);

    v.b("length_filtering_enabled", o.lengthFilter.enabled);
    v.i("length_required", o.lengthFilter.required);
    v.i("length_limit", o.lengthFilter.limit);

    v.b("low_complexity_filter", o.complexityFilter.enabled);
    v.i("complexity_threshold", o.complexityFilter.threshold);

    v.b("filter_by_index", o.indexFilter.enabled);
    v.s("index1_file", o.indexFilter.file1);
    v.s("index2_file", o.indexFilter.file2);
    v.i("index_threshold", o.indexFilter.threshold);

    v.i("overlap_len_require", o.overlap.lenRequire);
    v.i("overlap_diff_limit", o.overlap.diffLimit);
    v.i("overlap_diff_percent_limit", o.overlap.diffPercentLimit);

    v.b("correction_enabled", o.correction.enabled);

    v.b("umi_enabled", o.umi.enabled);
    v.umiloc("umi_loc", o.umi.loc);
    v.i("umi_len", o.umi.len);
    v.s("umi_prefix", o.umi.prefix);
    v.i("umi_skip", o.umi.skip);
    v.s("umi_delim", o.umi.delim);

    v.b("dedup", o.duplication.dedup);
    v.b("dup_evaluate", o.duplication.evaluate);
    v.i("dup_accuracy_level", o.duplication.accuracyLevel);

    v.b("overrepresentation_analysis", o.overrep.enabled);
    v.i("overrepresentation_sampling", o.overrep.sampling);

    v.b("split_enabled", o.split.enabled);
    v.i("split_number", o.split.number);
    v.l("split_by_lines", o.split.byLines);
    v.i("split_digits", o.split.digits);

    v.b("merge_enabled", o.merge.enabled);
    v.s("merged_out", o.merge.out);
    v.b("include_unmerged", o.merge.includeUnmerged);

    v.b("deep_per_tile", o.deep.perTile);
    v.b("deep_kmer_spectrum", o.deep.kmerSpectrum);
    v.i("deep_kmer_k", o.deep.kmerK);
    v.b("deep_error_profile", o.deep.errorProfile);
    v.b("deep_quality_calibration", o.deep.qualityCalibration);
    v.b("deep_insert_size", o.deep.insertSize);
    v.b("deep_duplication_curve", o.deep.duplicationCurve);
    v.b("deep_contaminant_screen", o.deep.contaminantScreen);
    v.b("deep_positional_quality_dist", o.deep.positionalQualityDist);
    v.l("deep_genome_size", o.deep.genomeSize);
    v.b("deep_disable_all", o.deep.disableAll);

    v.l("sample_reads", o.sampling.reads);
    v.b("sample_head", o.sampling.headOnly);

    v.b("check_contamination", o.contam.enabled);
    v.s("contam_db", o.contam.dbPath);
    v.d("contam_min_fraction", o.contam.minFraction);

    // Derived; emitted for readability, ignored when reading back.
    v.roi("qual_offset", o.qualityOffset());
    v.roi("paired", o.isPaired() ? 1 : 0);
    v.ros("adapter_extra_sequences", joinList(o.adapter.extraSeqs, ","));
    v.ros("adapter_extra_names", joinList(o.adapter.extraNames, ","));
    v.ros("index_blacklist1", joinList(o.indexFilter.blacklist1, ","));
    v.ros("index_blacklist2", joinList(o.indexFilter.blacklist2, ","));
}

struct CfgToJson {
    JsonWriter& j;
    void b(const char* k, bool v) { j.b(k, v); }
    void i(const char* k, int v) { j.n(k, static_cast<long long>(v)); }
    void l(const char* k, long long v) { j.n(k, v); }
    void d(const char* k, double v) { j.n(k, v); }
    void s(const char* k, const std::string& v) { j.s(k, v); }
    void mode(const char* k, RunMode v) { j.s(k, runModeName(v)); }
    void umiloc(const char* k, UmiLoc v) { j.s(k, umiLocName(v)); }
    void roi(const char* k, long long v) { j.n(k, v); }
    void ros(const char* k, const std::string& v) { j.s(k, v); }
};

struct CfgToTsv {
    std::map<std::string, std::string>& m;
    void put(const char* k, const std::string& v) { m[std::string("config.") + k] = v; }
    void b(const char* k, bool v) { put(k, v ? "true" : "false"); }
    void i(const char* k, int v) { put(k, std::to_string(v)); }
    void l(const char* k, long long v) { put(k, std::to_string(v)); }
    void d(const char* k, double v) { put(k, fmtDouble(v)); }
    void s(const char* k, const std::string& v) { put(k, v); }
    void mode(const char* k, RunMode v) { put(k, runModeName(v)); }
    void umiloc(const char* k, UmiLoc v) { put(k, umiLocName(v)); }
    void roi(const char* k, long long v) { put(k, std::to_string(v)); }
    void ros(const char* k, const std::string& v) { put(k, v); }
};

// ---------------------------------------------------------------------------
// fastp-compatible per-read-set object
// ---------------------------------------------------------------------------

std::string kmerKey(size_t index, int k) {
    static const char* bases = "ACGT";
    std::string s(static_cast<size_t>(k), 'A');
    for (int i = k - 1; i >= 0; --i) {
        s[static_cast<size_t>(i)] = bases[index & 3u];
        index >>= 2;
    }
    return s;
}

void writeReadSetObject(JsonWriter& j, const std::string& key, const ReadSetStats& s,
                        const OverrepAnalyzer* overrep) {
    const size_t C = s.cycles;
    std::vector<double> mean(C, 0.0);
    std::vector<double> qa(C, 0.0), qc(C, 0.0), qg(C, 0.0), qt(C, 0.0);
    std::vector<double> ca(C, 0.0), cc(C, 0.0), cg(C, 0.0), ct(C, 0.0), cn(C, 0.0), cgc(C, 0.0);
    for (size_t c = 0; c < C; ++c) {
        mean[c] = s.cycleMeanQuality(c);
        qa[c] = s.cycleMeanQualityForBase(c, 0);
        qc[c] = s.cycleMeanQualityForBase(c, 1);
        qg[c] = s.cycleMeanQualityForBase(c, 2);
        qt[c] = s.cycleMeanQualityForBase(c, 3);
        ca[c] = s.cycleBaseFraction(c, 0);
        cc[c] = s.cycleBaseFraction(c, 1);
        cg[c] = s.cycleBaseFraction(c, 2);
        ct[c] = s.cycleBaseFraction(c, 3);
        cn[c] = s.cycleBaseFraction(c, 4);
        cgc[c] = s.cycleGcFraction(c);
    }

    j.beginObject(key);
    j.n("total_reads", s.reads);
    j.n("total_bases", s.bases);
    j.n("q20_bases", s.q20Bases);
    j.n("q30_bases", s.q30Bases);
    j.n("total_cycles", static_cast<unsigned long long>(C));

    // fastp's key order inside the curves: A, T, C, G (+ mean / N / GC).
    j.beginObject("quality_curves");
    j.arr("A", qa);
    j.arr("T", qt);
    j.arr("C", qc);
    j.arr("G", qg);
    j.arr("mean", mean);
    j.endObject();

    j.beginObject("content_curves");
    j.arr("A", ca);
    j.arr("T", ct);
    j.arr("C", cc);
    j.arr("G", cg);
    j.arr("N", cn);
    j.arr("GC", cgc);
    j.endObject();

    j.beginObject("kmer_count");
    for (size_t i = 0; i < s.kmerProfile.size(); ++i) {
        j.n(kmerKey(i, kStatKmerK), s.kmerProfile[i]);
    }
    j.endObject();

    j.beginObject("overrepresented_sequences");
    if (overrep && overrep->enabled) {
        for (const OverrepHit& h : overrep->hits) j.n(h.seq, h.count);
    }
    j.endObject();

    j.endObject();
}

void writeSummarySide(JsonWriter& j, const std::string& key, const Combined& c) {
    j.beginObject(key);
    j.n("total_reads", c.reads);
    j.n("total_bases", c.bases);
    j.n("q20_bases", c.q20);
    j.n("q30_bases", c.q30);
    j.n("q20_rate", ratio(c.q20, c.bases));
    j.n("q30_rate", ratio(c.q30, c.bases));
    j.n("read1_mean_length", static_cast<long long>(std::llround(c.len1)));
    j.n("read2_mean_length", static_cast<long long>(std::llround(c.len2)));
    j.n("gc_content", ratio(c.gc, c.bases));
    j.endObject();
}

void writeTileSection(JsonWriter& j, const TileAnalyzer& t) {
    j.beginObject("per_tile");
    j.b("usable", t.usable());
    j.n("tile_count", static_cast<unsigned long long>(t.tileCount()));
    j.n("global_mean_quality", t.globalMeanQuality);
    j.beginArray("outlier_tiles");
    for (const std::string& s : t.outlierTiles) j.sv(s);
    j.endArray();
    j.beginObject("tiles");
    for (const auto& kv : t.tiles_) {
        const TileStats& ts = kv.second;
        uint64_t qsum = 0, depth = 0;
        std::vector<double> byCycle(ts.qualSum.size(), 0.0);
        for (size_t c = 0; c < ts.qualSum.size(); ++c) {
            uint64_t d = c < ts.depth.size() ? ts.depth[c] : 0;
            byCycle[c] = d ? static_cast<double>(ts.qualSum[c]) / static_cast<double>(d) : 0.0;
            qsum += ts.qualSum[c];
            depth += d;
        }
        j.beginObject(kv.first);
        j.n("reads", ts.reads);
        j.n("mean_quality", ratio(qsum, depth));
        j.arr("quality_by_cycle", byCycle);
        j.endObject();
    }
    j.endObject();
    j.endObject();
}

void writeErrorModel(JsonWriter& j, const ErrorModel& e) {
    j.beginObject("error_model");
    j.b("has_data", e.hasData());
    j.n("pairs_examined", e.pairsExamined);
    j.n("bases_compared", e.basesCompared);
    j.n("mismatches", e.mismatches);
    j.n("error_rate", e.overallErrorRate);
    j.n("empirical_q", e.overallQ);
    j.n("calibration_bias", e.calibrationBias);

    j.beginArray("calibration");
    for (const auto& c : e.calibration) {
        int q = c.first;
        uint64_t obs = (q >= 0 && q < kQualBins) ? e.qTotal[static_cast<size_t>(q)] : 0;
        j.beginObject();
        j.n("reported_q", static_cast<long long>(q));
        j.n("observed_q", c.second);
        j.n("observations", obs);
        j.endObject();
    }
    j.endArray();

    static const char* kBases = "ACGT";
    j.beginObject("substitutions");
    for (size_t from = 0; from < 4; ++from) {
        j.beginObject(std::string(1, kBases[from]));
        for (size_t to = 0; to < 4; ++to) {
            if (from == to) continue;
            j.n(std::string(1, kBases[to]), e.substitutions[from][to]);
        }
        j.endObject();
    }
    j.endObject();

    std::vector<double> r1(e.cycleTotal1.size(), 0.0), r2(e.cycleTotal2.size(), 0.0);
    for (size_t c = 0; c < r1.size(); ++c) {
        uint64_t err = c < e.cycleErrors1.size() ? e.cycleErrors1[c] : 0;
        r1[c] = ratio(err, e.cycleTotal1[c]);
    }
    for (size_t c = 0; c < r2.size(); ++c) {
        uint64_t err = c < e.cycleErrors2.size() ? e.cycleErrors2[c] : 0;
        r2[c] = ratio(err, e.cycleTotal2[c]);
    }
    j.arr("cycle_error_rate_read1", r1);
    j.arr("cycle_error_rate_read2", r2);
    j.endObject();
}

void writeQualityDist(JsonWriter& j, const std::string& key, const ReadSetStats& s) {
    j.beginArray(key);
    for (const auto& hist : s.cycleQualHist) j.arrv(hist);
    j.endArray();
}

void writeOverrepList(JsonWriter& j, const std::string& key, const OverrepAnalyzer& o) {
    j.beginArray(key);
    for (const OverrepHit& h : o.hits) {
        j.beginObject();
        j.s("sequence", h.seq);
        j.n("count", h.count);
        j.n("percent", h.percent);
        if (h.source.empty()) j.nul("source");
        else j.s("source", h.source);
        j.arr("positions", h.positions);
        j.endObject();
    }
    j.endArray();
}

void writeContamination(JsonWriter& j, const ContamResult& c) {
    j.beginObject("contamination");
    j.b("enabled", c.attempted);
    j.b("ok", c.ok);
    if (!c.message.empty()) j.s("message", c.message);
    j.s("database", c.dbPath);
    j.n("k", static_cast<long long>(c.k));
    j.n("reads_screened", c.readsScreened);
    j.n("kmers_screened", c.kmersScreened);
    j.n("total_hits", c.totalHits);
    j.n("min_fraction", c.minFraction);
    j.n("noise_floor", c.noiseFloor);
    j.s("verdict", c.verdict);
    j.s("dominant_species", c.dominant);
    j.n("dominant_fraction", c.dominantFraction);
    j.s("top_contaminant", c.topContaminant);
    j.n("top_contaminant_fraction", c.topContaminantFraction);
    j.beginArray("species");
    for (const ContamSpeciesResult& s : c.species) {
        // Absent species are the majority of the panel and carry no information.
        if (!s.present && s.matchedKmers == 0) continue;
        j.beginObject();
        j.s("name", s.name);
        j.b("common_contaminant", s.commonContaminant);
        j.b("present", s.present);
        j.n("sketch_kmers", s.sketchSize);
        j.n("matched_kmers", s.matchedKmers);
        j.n("hits", s.hits);
        j.n("library_fraction", s.libraryFraction);
        j.n("relative_abundance", s.relativeAbundance);
        j.n("estimated_depth", s.estimatedDepth);
        j.n("genome_length", s.genomeLength);
        j.endObject();
    }
    j.endArray();
    j.endObject();
}

}  // namespace

// ---------------------------------------------------------------------------
// writeJsonReport
// ---------------------------------------------------------------------------

void writeJsonReport(const std::string& path, const RunStats& stats, const Options& opt) {
    if (path.empty()) return;
    std::ofstream out(path, std::ios::binary);
    if (!out) {
        std::cerr << util::Color::red() << "error" << util::Color::reset()
                  << ": cannot write JSON report to " << path << "\n";
        return;
    }

    const bool paired = opt.isPaired();
    const Digest d = makeDigest(stats, opt);
    const Verdict verdict = computeVerdict(stats, opt);

    JsonWriter j(out);
    j.beginObject();

    // ---- fastp-compatible section -----------------------------------------
    j.beginObject("summary");
    j.s("fastp_version", stats.version);
    j.s("sequencing", sequencingDesc(stats, opt));
    writeSummarySide(j, "before_filtering", d.before);
    writeSummarySide(j, "after_filtering", d.after);
    j.endObject();

    j.beginObject("filtering_result");
    j.n("passed_filter_reads", filterCount(stats, FilterResult::Pass));
    // fastp folds the average-quality rejects into low_quality_reads.
    j.n("low_quality_reads", filterCount(stats, FilterResult::FailedQuality) +
                                 filterCount(stats, FilterResult::FailedAverageQuality));
    j.n("too_many_N_reads", filterCount(stats, FilterResult::FailedNBase));
    j.n("too_short_reads", filterCount(stats, FilterResult::FailedLength));
    j.n("too_long_reads", filterCount(stats, FilterResult::FailedTooLong));
    j.endObject();

    j.beginObject("duplication");
    j.n("rate", stats.duplication.duplicationRate);
    j.arr("histogram", stats.duplication.levels);
    j.beginArray("mean_gc");
    j.endArray();
    j.endObject();

    if (paired) {
        j.beginObject("insert_size");
        j.n("peak", static_cast<long long>(stats.insertSize.mode));
        j.n("unknown", stats.insertSize.unknown);
        j.arr("histogram", stats.insertSize.histogram);
        j.endObject();
    }

    j.beginObject("adapter_cutting");
    j.n("adapter_trimmed_reads", d.adapterReads);
    j.n("adapter_trimmed_bases", d.adapterBases);
    j.s("read1_adapter_sequence", opt.adapter.resolvedR1);
    j.s("read2_adapter_sequence", opt.adapter.resolvedR2);
    j.beginObject("read1_adapter_counts");
    for (const auto& kv : stats.adapterCounts1) j.n(kv.first, kv.second);
    j.endObject();
    j.beginObject("read2_adapter_counts");
    for (const auto& kv : stats.adapterCounts2) j.n(kv.first, kv.second);
    j.endObject();
    j.endObject();

    if (opt.polyX.enabled) {
        j.beginObject("polyx_trimming");
        j.n("total_polyx_trimmed_reads", stats.polyXTrimmedReads);
        j.n("total_polyx_trimmed_bases", stats.polyXTrimmedBases);
        j.endObject();
    }
    if (opt.polyG.enabled) {
        j.beginObject("polyg_trimming");
        j.n("total_polyg_trimmed_reads", stats.polyGTrimmedReads);
        j.n("total_polyg_trimmed_bases", stats.polyGTrimmedBases);
        j.endObject();
    }

    writeReadSetObject(j, "read1_before_filtering", stats.before1,
                       stats.overrep1.enabled ? &stats.overrep1 : nullptr);
    if (paired) {
        writeReadSetObject(j, "read2_before_filtering", stats.before2,
                           stats.overrep2.enabled ? &stats.overrep2 : nullptr);
    }
    writeReadSetObject(j, "read1_after_filtering", stats.after1, nullptr);
    if (paired) {
        writeReadSetObject(j, "read2_after_filtering", stats.after2, nullptr);
    }
    if (opt.merge.enabled) {
        writeReadSetObject(j, "merged_and_filtered", stats.merged, nullptr);
    }

    j.s("command", stats.command);

    // ---- everything fastp does not report ---------------------------------
    j.beginObject("scepter");
    j.s("version", stats.version);
    j.s("started_at", stats.startedAt);
    j.n("duration_seconds", stats.durationSeconds);
    j.s("command", stats.command);

    j.beginObject("run");
    j.n("reads_in", d.readsIn);
    j.n("reads_out", d.readsOut);
    j.n("bases_in", d.basesIn);
    j.n("bases_out", d.basesOut);
    j.n("pass_rate", d.passRate);
    j.n("corrected_reads", stats.correctedReads);
    j.n("corrected_bases", stats.correctedBases);
    j.n("merged_pairs", stats.mergedPairs);
    j.n("polyg_trimmed_reads", stats.polyGTrimmedReads);
    j.n("polyg_trimmed_bases", stats.polyGTrimmedBases);
    j.n("polyx_trimmed_reads", stats.polyXTrimmedReads);
    j.n("polyx_trimmed_bases", stats.polyXTrimmedBases);
    j.n("adapter_trimmed_reads1", stats.adapterTrimmedReads1);
    j.n("adapter_trimmed_reads2", stats.adapterTrimmedReads2);
    j.n("adapter_trimmed_bases1", stats.adapterTrimmedBases1);
    j.n("adapter_trimmed_bases2", stats.adapterTrimmedBases2);
    j.arr("adapter_start_read1", stats.adapterStart1);
    j.arr("adapter_start_read2", stats.adapterStart2);
    j.endObject();

    j.beginObject("filtering_detail");
    for (size_t i = 0; i < static_cast<size_t>(FilterResult::kCount); ++i) {
        j.n(kFilterKeys[i], stats.filterCounts[i]);
    }
    j.endObject();

    j.beginObject("detection");
    j.s("sample_strategy", stats.detection.strategy);
    j.n("sample_reads_read1", stats.detection.sampleReads1);
    j.n("sample_reads_read2", stats.detection.sampleReads2);
    j.n("records_scanned", stats.detection.recordsScanned);
    j.b("exact_record_count", stats.detection.exactRecordCount);
    j.n("sample_seconds", stats.detection.seconds);
    j.n("sample_mean_quality", stats.detection.sampleMeanQuality);
    j.n("sample_gc_content", stats.detection.sampleGcFraction);
    j.s("instrument", stats.detection.instrument);
    j.s("platform", stats.detection.platform);
    j.b("tile_headers_usable", stats.detection.tileHeadersUsable);
    j.b("polyg_recommended", stats.detection.polyGRecommended);
    j.n("polyg_tail_fraction", stats.detection.polyGTailFraction);
    j.endObject();

    writeContamination(j, stats.contamination);

    writeTileSection(j, stats.tiles1);
    writeErrorModel(j, stats.errorModel);

    j.beginObject("kmer_spectrum");
    j.n("k", static_cast<long long>(stats.kmers.k()));
    j.n("scale", stats.kmers.scale());
    j.b("reliable", stats.kmers.reliable);
    j.n("peak_depth", static_cast<long long>(stats.kmers.peakDepth));
    j.n("estimated_genome_size", stats.kmers.estimatedGenomeSize);
    j.n("estimated_genome_size_core", stats.kmers.estimatedGenomeSizeCore);
    j.n("kmer_sampling_factor", static_cast<double>(stats.kmers.samplingFactor));
    j.n("estimated_depth", stats.kmers.estimatedDepth);
    j.n("error_kmer_fraction", stats.kmers.errorKmerFraction);
    j.n("repeat_fraction", stats.kmers.repeatFraction);
    j.n("distinct_sketch_kmers", stats.kmers.distinctSketchKmers);
    // Scaled back to the whole library: totalKmers is accumulated inside the
    // subsampled sketch, so emitting it raw understated the count by exactly
    // the sampling factor.
    j.n("total_kmers", static_cast<double>(stats.kmers.totalKmers) *
                           static_cast<double>(stats.kmers.samplingFactor));
    j.arr("histogram", stats.kmers.histogram);
    j.endObject();

    j.beginObject("duplication_detail");
    j.n("rate", stats.duplication.duplicationRate);
    j.n("estimated_library_size", stats.duplication.estimatedLibrarySize);
    j.n("total_observed", stats.duplication.totalObserved);
    j.n("unique_observed", stats.duplication.uniqueObserved);
    j.beginArray("levels");
    for (size_t i = 0; i < stats.duplication.levels.size(); ++i) {
        j.beginObject();
        const char* label = DuplicationAnalyzer::levelLabel(static_cast<int>(i));
        j.s("label", label ? label : std::to_string(i + 1));
        j.n("percent", stats.duplication.levels[i]);
        j.endObject();
    }
    j.endArray();
    j.endObject();

    j.beginObject("insert_size_detail");
    j.n("mode", static_cast<long long>(stats.insertSize.mode));
    j.n("mean", stats.insertSize.mean);
    j.n("median", stats.insertSize.median);
    j.n("stddev", stats.insertSize.stddev);
    j.n("p10", static_cast<long long>(stats.insertSize.p10));
    j.n("p90", static_cast<long long>(stats.insertSize.p90));
    j.n("counted", stats.insertSize.counted);
    j.n("unknown", stats.insertSize.unknown);
    j.n("max_size", static_cast<long long>(stats.insertSize.maxSize));
    j.arr("histogram", stats.insertSize.histogram);
    j.endObject();

    j.beginObject("quality_distribution");
    writeQualityDist(j, "read1", stats.before1);
    writeQualityDist(j, "read2", stats.before2);
    writeQualityDist(j, "read1_after", stats.after1);
    writeQualityDist(j, "read2_after", stats.after2);
    j.endObject();

    j.beginObject("complexity");
    j.arr("read1_histogram", stats.before1.complexityHist);
    j.arr("read2_histogram", stats.before2.complexityHist);
    j.endObject();

    j.beginObject("length_distribution");
    j.arr("read1_before", stats.before1.lengthHist);
    j.arr("read1_after", stats.after1.lengthHist);
    j.arr("read2_before", stats.before2.lengthHist);
    j.arr("read2_after", stats.after2.lengthHist);
    j.arr("merged", stats.merged.lengthHist);
    j.endObject();

    j.beginObject("gc_distribution");
    j.arr("read1_before", stats.before1.gcHist);
    j.arr("read2_before", stats.before2.gcHist);
    j.endObject();

    j.beginObject("mean_quality_distribution");
    j.arr("read1_before", stats.before1.meanQualHist);
    j.arr("read2_before", stats.before2.meanQualHist);
    j.endObject();

    j.beginObject("overrepresented");
    writeOverrepList(j, "read1", stats.overrep1);
    writeOverrepList(j, "read2", stats.overrep2);
    j.endObject();

    j.beginObject("verdict");
    j.s("status", verdict.status);
    j.beginArray("reasons");
    for (const std::string& r : verdict.reasons) j.sv(r);
    j.endArray();
    j.endObject();

    j.beginObject("config");
    CfgToJson cfg{j};
    visitConfig(opt, cfg);
    j.endObject();

    j.endObject();   // scepter
    j.endObject();   // root
    out << "\n";
}

// ---------------------------------------------------------------------------
// writeTsvReport
// ---------------------------------------------------------------------------

void writeTsvReport(const std::string& path, const RunStats& stats, const Options& opt) {
    if (path.empty()) return;
    std::ofstream out(path, std::ios::binary);
    if (!out) {
        std::cerr << util::Color::red() << "error" << util::Color::reset()
                  << ": cannot write TSV report to " << path << "\n";
        return;
    }

    const Digest d = makeDigest(stats, opt);
    const Verdict verdict = computeVerdict(stats, opt);
    std::map<std::string, std::string> m;

    auto putU = [&m](const std::string& k, uint64_t v) { m[k] = std::to_string(v); };
    auto putI = [&m](const std::string& k, long long v) { m[k] = std::to_string(v); };
    auto putD = [&m](const std::string& k, double v) { m[k] = fmtDouble(v); };
    auto putS = [&m](const std::string& k, const std::string& v) {
        std::string clean = v;
        for (char& c : clean) {
            if (c == '\t' || c == '\n' || c == '\r') c = ' ';
        }
        m[k] = clean;
    };

    putS("version", stats.version);
    putS("started_at", stats.startedAt);
    putS("command", stats.command);
    putS("sequencing", sequencingDesc(stats, opt));
    putS("mode", opt.isPaired() ? "paired_end" : "single_end");
    putD("duration_seconds", stats.durationSeconds);
    putD("reads_per_second", safeDiv(static_cast<double>(d.readsIn), stats.durationSeconds));
    putD("megabases_per_second",
         safeDiv(static_cast<double>(d.basesIn) / 1e6, stats.durationSeconds));

    putU("reads_in", d.readsIn);
    putU("reads_out", d.readsOut);
    putU("bases_in", d.basesIn);
    putU("bases_out", d.basesOut);
    putD("pass_rate", d.passRate);

    putU("before_filtering.total_reads", d.before.reads);
    putU("before_filtering.total_bases", d.before.bases);
    putU("before_filtering.q20_bases", d.before.q20);
    putU("before_filtering.q30_bases", d.before.q30);
    putD("before_filtering.q20_rate", d.q20Before);
    putD("before_filtering.q30_rate", d.q30Before);
    putD("before_filtering.gc_content", d.gcBefore);
    putI("before_filtering.read1_mean_length", std::llround(d.before.len1));
    putI("before_filtering.read2_mean_length", std::llround(d.before.len2));
    putI("before_filtering.read1_cycles", static_cast<long long>(stats.before1.cycles));
    putI("before_filtering.read2_cycles", static_cast<long long>(stats.before2.cycles));

    putU("after_filtering.total_reads", d.after.reads);
    putU("after_filtering.total_bases", d.after.bases);
    putU("after_filtering.q20_bases", d.after.q20);
    putU("after_filtering.q30_bases", d.after.q30);
    putD("after_filtering.q20_rate", d.q20After);
    putD("after_filtering.q30_rate", d.q30After);
    putD("after_filtering.gc_content", d.gcAfter);
    putI("after_filtering.read1_mean_length", std::llround(d.after.len1));
    putI("after_filtering.read2_mean_length", std::llround(d.after.len2));

    putU("filtering.passed_filter_reads", filterCount(stats, FilterResult::Pass));
    putU("filtering.low_quality_reads", filterCount(stats, FilterResult::FailedQuality) +
                                            filterCount(stats, FilterResult::FailedAverageQuality));
    putU("filtering.too_many_N_reads", filterCount(stats, FilterResult::FailedNBase));
    putU("filtering.too_short_reads", filterCount(stats, FilterResult::FailedLength));
    putU("filtering.too_long_reads", filterCount(stats, FilterResult::FailedTooLong));
    for (size_t i = 0; i < static_cast<size_t>(FilterResult::kCount); ++i) {
        putU(std::string("filtering_detail.") + kFilterKeys[i], stats.filterCounts[i]);
    }

    putU("adapter.trimmed_reads", d.adapterReads);
    putU("adapter.trimmed_bases", d.adapterBases);
    putU("adapter.trimmed_reads1", stats.adapterTrimmedReads1);
    putU("adapter.trimmed_reads2", stats.adapterTrimmedReads2);
    putU("adapter.trimmed_bases1", stats.adapterTrimmedBases1);
    putU("adapter.trimmed_bases2", stats.adapterTrimmedBases2);
    putD("adapter.trimmed_read_rate", d.adapterRate);
    putS("adapter.read1_sequence", opt.adapter.resolvedR1);
    putS("adapter.read2_sequence", opt.adapter.resolvedR2);

    putU("polyg.trimmed_reads", stats.polyGTrimmedReads);
    putU("polyg.trimmed_bases", stats.polyGTrimmedBases);
    putU("polyx.trimmed_reads", stats.polyXTrimmedReads);
    putU("polyx.trimmed_bases", stats.polyXTrimmedBases);
    putU("correction.corrected_reads", stats.correctedReads);
    putU("correction.corrected_bases", stats.correctedBases);
    putU("merge.merged_pairs", stats.mergedPairs);

    putD("duplication.rate", stats.duplication.duplicationRate);
    putD("duplication.estimated_library_size", stats.duplication.estimatedLibrarySize);
    putU("duplication.total_observed", stats.duplication.totalObserved);
    putU("duplication.unique_observed", stats.duplication.uniqueObserved);

    putI("insert_size.peak", stats.insertSize.mode);
    putD("insert_size.mean", stats.insertSize.mean);
    putD("insert_size.median", stats.insertSize.median);
    putD("insert_size.stddev", stats.insertSize.stddev);
    putI("insert_size.p10", stats.insertSize.p10);
    putI("insert_size.p90", stats.insertSize.p90);
    putU("insert_size.counted", stats.insertSize.counted);
    putU("insert_size.unknown", stats.insertSize.unknown);

    m["per_tile.usable"] = stats.tiles1.usable() ? "true" : "false";
    putU("per_tile.tile_count", stats.tiles1.tileCount());
    putD("per_tile.global_mean_quality", stats.tiles1.globalMeanQuality);
    putU("per_tile.outlier_tile_count", stats.tiles1.outlierTiles.size());
    putS("per_tile.outlier_tiles", joinList(stats.tiles1.outlierTiles, ","));

    m["error_model.has_data"] = stats.errorModel.hasData() ? "true" : "false";
    putU("error_model.pairs_examined", stats.errorModel.pairsExamined);
    putU("error_model.bases_compared", stats.errorModel.basesCompared);
    putU("error_model.mismatches", stats.errorModel.mismatches);
    putD("error_model.error_rate", stats.errorModel.overallErrorRate);
    putD("error_model.empirical_q", stats.errorModel.overallQ);
    putD("error_model.calibration_bias", stats.errorModel.calibrationBias);

    putI("kmer_spectrum.k", stats.kmers.k());
    putU("kmer_spectrum.scale", stats.kmers.scale());
    m["kmer_spectrum.reliable"] = stats.kmers.reliable ? "true" : "false";
    putI("kmer_spectrum.peak_depth", stats.kmers.peakDepth);
    putD("kmer_spectrum.estimated_genome_size", stats.kmers.estimatedGenomeSize);
    putD("kmer_spectrum.estimated_depth", stats.kmers.estimatedDepth);
    putD("kmer_spectrum.error_kmer_fraction", stats.kmers.errorKmerFraction);
    putD("kmer_spectrum.repeat_fraction", stats.kmers.repeatFraction);
    putU("kmer_spectrum.distinct_sketch_kmers", stats.kmers.distinctSketchKmers);
    putU("kmer_spectrum.total_kmers", stats.kmers.totalKmers);

    putU("overrepresented.read1_count", stats.overrep1.hits.size());
    putU("overrepresented.read2_count", stats.overrep2.hits.size());

    putS("detection.sample_strategy", stats.detection.strategy);
    putI("detection.sample_reads_read1", stats.detection.sampleReads1);
    putI("detection.sample_reads_read2", stats.detection.sampleReads2);
    putI("detection.records_scanned", stats.detection.recordsScanned);
    m["detection.exact_record_count"] = stats.detection.exactRecordCount ? "true" : "false";
    putD("detection.sample_seconds", stats.detection.seconds);
    putD("detection.sample_mean_quality", stats.detection.sampleMeanQuality);
    putD("detection.sample_gc_content", stats.detection.sampleGcFraction);
    putS("detection.instrument", stats.detection.instrument);
    putS("detection.platform", stats.detection.platform);

    {
        const ContamResult& c = stats.contamination;
        m["contamination.enabled"] = c.attempted ? "true" : "false";
        m["contamination.ok"] = c.ok ? "true" : "false";
        putS("contamination.message", c.message);
        putS("contamination.database", c.dbPath);
        putS("contamination.verdict", c.verdict);
        putS("contamination.dominant_species", c.dominant);
        putD("contamination.dominant_fraction", c.dominantFraction);
        putS("contamination.top_contaminant", c.topContaminant);
        putD("contamination.top_contaminant_fraction", c.topContaminantFraction);
        putU("contamination.reads_screened", c.readsScreened);
        putU("contamination.kmers_screened", c.kmersScreened);
        putU("contamination.total_hits", c.totalHits);
        putD("contamination.noise_floor", c.noiseFloor);
        size_t present = 0;
        for (const ContamSpeciesResult& s : c.species) {
            if (!s.present) continue;
            ++present;
            const std::string key = "contamination.species." + s.name + ".";
            putD(key + "relative_abundance", s.relativeAbundance);
            putD(key + "library_fraction", s.libraryFraction);
            putD(key + "estimated_depth", s.estimatedDepth);
            putU(key + "matched_kmers", s.matchedKmers);
            m[key + "common_contaminant"] = s.commonContaminant ? "true" : "false";
        }
        putU("contamination.species_present", present);
    }

    putS("verdict.status", verdict.status);
    putU("verdict.reason_count", verdict.reasons.size());
    putS("verdict.reasons", joinList(verdict.reasons, "; "));

    CfgToTsv cfg{m};
    visitConfig(opt, cfg);

    out << "#key\tvalue\n";
    for (const auto& kv : m) out << kv.first << '\t' << kv.second << '\n';
}

// ---------------------------------------------------------------------------
// printSummary
// ---------------------------------------------------------------------------

namespace {

constexpr size_t kBoxWidth = 76;
constexpr size_t kLabelWidth = 16;

void boxTop(std::ostream& os, const std::string& title) {
    const char* g = util::Color::gray();
    const char* r = util::Color::reset();
    std::string head = "╭─ " + title + " ";
    size_t w = displayWidth(head);
    os << g << "╭─ " << r << util::Color::bold() << title << r << g << ' ';
    for (size_t i = w; i + 1 < kBoxWidth; ++i) os << "─";
    os << "╮" << r << "\n";
}

void boxRule(std::ostream& os) {
    const char* g = util::Color::gray();
    os << g << "├";
    for (size_t i = 0; i + 2 < kBoxWidth; ++i) os << "─";
    os << "┤" << util::Color::reset() << "\n";
}

void boxBottom(std::ostream& os) {
    const char* g = util::Color::gray();
    os << g << "╰";
    for (size_t i = 0; i + 2 < kBoxWidth; ++i) os << "─";
    os << "╯" << util::Color::reset() << "\n";
}

void boxRow(std::ostream& os, const std::string& label, const std::string& value,
            const char* valueColor = "") {
    const char* g = util::Color::gray();
    const char* r = util::Color::reset();
    size_t lw = displayWidth(label);
    size_t pad = lw < kLabelWidth ? kLabelWidth - lw : 1;
    size_t used = 1 + lw + pad + displayWidth(value);
    size_t fill = used + 2 <= kBoxWidth ? kBoxWidth - 2 - used : 0;
    os << g << "│" << r << ' ' << util::Color::cyan() << label << r
       << std::string(pad, ' ') << valueColor << value << r
       << std::string(fill, ' ') << g << "│" << r << "\n";
}

const char* verdictColor(const std::string& status) {
    if (status == "fail") return util::Color::red();
    if (status == "warn") return util::Color::yellow();
    return util::Color::green();
}

std::string arrow(const std::string& a, const std::string& b) { return a + " → " + b; }

}  // namespace

void printSummary(const RunStats& stats, const Options& opt) {
    if (opt.quiet || opt.noSummary) return;

    const Digest d = makeDigest(stats, opt);
    const Verdict verdict = computeVerdict(stats, opt);
    std::ostringstream os;

    boxTop(os, "scepter " + (stats.version.empty() ? std::string("report") : stats.version));

    std::string in1 = opt.in1.empty() ? (opt.stdinInput ? std::string("<stdin>") : std::string("-"))
                                      : opt.in1;
    boxRow(os, "input", in1);
    if (!opt.in2.empty()) boxRow(os, "", opt.in2);
    boxRow(os, "mode", opt.isPaired() ? sfmt("PE  %zu + %zu cycles", stats.before1.cycles,
                                             stats.before2.cycles)
                                      : sfmt("SE  %zu cycles", stats.before1.cycles));

    boxRule(os);

    boxRow(os, "reads",
           arrow(util::commify(static_cast<long long>(d.readsIn)),
                 util::commify(static_cast<long long>(d.readsOut))) +
               sfmt("   (%.2f%% passed)", d.passRate * 100.0),
           d.passRate >= 0.8 ? util::Color::green() : util::Color::yellow());
    boxRow(os, "bases",
           arrow(util::humanCount(static_cast<double>(d.basesIn)),
                 util::humanCount(static_cast<double>(d.basesOut))));
    boxRow(os, "Q20", arrow(pctStr(d.q20Before), pctStr(d.q20After)),
           d.q20After >= d.q20Before ? util::Color::green() : util::Color::yellow());
    boxRow(os, "Q30", arrow(pctStr(d.q30Before), pctStr(d.q30After)),
           d.q30After >= d.q30Before ? util::Color::green() : util::Color::yellow());
    boxRow(os, "GC", arrow(pctStr(d.gcBefore), pctStr(d.gcAfter)));
    boxRow(os, "duplication", pctStr(d.dupRate),
           d.dupRate > 0.4 ? util::Color::yellow() : "");
    boxRow(os, "adapters", sfmt("%s reads trimmed (%s)", pctStr(d.adapterRate).c_str(),
                                util::humanCount(static_cast<double>(d.adapterBases)).c_str()));
    if (opt.isPaired() && stats.insertSize.counted > 0) {
        boxRow(os, "insert size", sfmt("peak %d bp   mean %.1f   sd %.1f", stats.insertSize.mode,
                                       stats.insertSize.mean, stats.insertSize.stddev));
    }
    if (stats.kmers.reliable && stats.kmers.estimatedGenomeSize > 0) {
        boxRow(os, "genome",
               sfmt("~%s bp at %.1fx depth",
                    util::humanCount(stats.kmers.estimatedGenomeSize).c_str(),
                    stats.kmers.estimatedDepth));
    }
    if (stats.contamination.attempted) {
        const ContamResult& c = stats.contamination;
        if (!c.ok) {
            boxRow(os, "contamination", "not screened: " + c.message, util::Color::yellow());
        } else {
            std::string line = c.verdict;
            if (!c.dominant.empty()) {
                line += sfmt("  %s %.1f%%", c.dominant.c_str(), c.dominantFraction * 100.0);
            }
            if (!c.topContaminant.empty()) {
                line += sfmt("  + %s %.1f%%", c.topContaminant.c_str(),
                             c.topContaminantFraction * 100.0);
            }
            const char* colour = c.verdict == "pure" ? util::Color::green()
                                                     : util::Color::yellow();
            boxRow(os, "contamination", line, colour);
        }
    }
    if (stats.errorModel.hasData()) {
        boxRow(os, "error rate", sfmt("%.4f%%  (empirical Q%.1f, bias %+.1f)",
                                      stats.errorModel.overallErrorRate * 100.0,
                                      stats.errorModel.overallQ,
                                      stats.errorModel.calibrationBias));
    }

    boxRule(os);

    boxRow(os, "verdict", util::toUpper(verdict.status), verdictColor(verdict.status));
    size_t shown = 0;
    for (const std::string& reason : verdict.reasons) {
        if (shown++ >= 3) {
            boxRow(os, "", sfmt("... and %zu more", verdict.reasons.size() - 3));
            break;
        }
        boxRow(os, "", "- " + reason, util::Color::gray());
    }

    boxRule(os);

    boxRow(os, "elapsed",
           sfmt("%s   %s reads/s   %.1f MB/s", util::humanTime(d.duration).c_str(),
                util::humanCount(safeDiv(static_cast<double>(d.readsIn), d.duration)).c_str(),
                safeDiv(static_cast<double>(d.basesIn) / 1e6, d.duration)));
    std::vector<std::string> reports;
    if (!opt.htmlFile.empty()) reports.push_back(opt.htmlFile);
    if (!opt.jsonFile.empty()) reports.push_back(opt.jsonFile);
    if (!opt.tsvFile.empty()) reports.push_back(opt.tsvFile);
    if (reports.empty()) {
        boxRow(os, "reports", "none written");
    } else {
        boxRow(os, "reports", reports[0]);
        for (size_t i = 1; i < reports.size(); ++i) boxRow(os, "", reports[i]);
    }

    boxBottom(os);
    std::cerr << os.str();
    std::cerr.flush();
}

// ---------------------------------------------------------------------------
// Recursive-descent JSON parser, used only to read reports this tool wrote
// ---------------------------------------------------------------------------

namespace {

struct JValue {
    enum class Type { Null, Bool, Number, String, Array, Object };
    Type type = Type::Null;
    bool boolean = false;
    double num = 0;
    std::string str;
    // std::vector supports an incomplete element type (C++17), which is what
    // lets the node hold its own children directly.
    std::vector<JValue> children;
    std::vector<std::string> keys;   // parallel to children when type == Object
};

class JsonParser {
public:
    explicit JsonParser(const std::string& text) : s_(text) {}

    bool parse(JValue& root, std::string& err) {
        skipWs();
        if (!parseValue(root, 0)) { err = err_; return false; }
        skipWs();
        if (p_ != s_.size()) { err = at("trailing content after top-level value"); return false; }
        return true;
    }

private:
    static constexpr int kMaxDepth = 64;

    bool fail(const std::string& msg) { if (err_.empty()) err_ = at(msg); return false; }

    std::string at(const std::string& msg) const {
        size_t line = 1, col = 1;
        for (size_t i = 0; i < p_ && i < s_.size(); ++i) {
            if (s_[i] == '\n') { ++line; col = 1; } else { ++col; }
        }
        return sfmt("line %zu column %zu: ", line, col) + msg;
    }

    void skipWs() {
        while (p_ < s_.size()) {
            char c = s_[p_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++p_;
            else break;
        }
    }

    bool literal(const char* lit) {
        size_t n = std::strlen(lit);
        if (s_.compare(p_, n, lit) != 0) return false;
        p_ += n;
        return true;
    }

    bool parseValue(JValue& v, int depth) {
        if (depth > kMaxDepth) return fail("nesting too deep");
        skipWs();
        if (p_ >= s_.size()) return fail("unexpected end of input");
        char c = s_[p_];
        switch (c) {
            case '{': return parseObject(v, depth);
            case '[': return parseArray(v, depth);
            case '"': {
                v.type = JValue::Type::String;
                return parseString(v.str);
            }
            case 't':
                if (!literal("true")) return fail("invalid literal");
                v.type = JValue::Type::Bool; v.boolean = true; v.num = 1;
                return true;
            case 'f':
                if (!literal("false")) return fail("invalid literal");
                v.type = JValue::Type::Bool; v.boolean = false; v.num = 0;
                return true;
            case 'n':
                if (!literal("null")) return fail("invalid literal");
                v.type = JValue::Type::Null;
                return true;
            default:
                return parseNumber(v);
        }
    }

    bool parseObject(JValue& v, int depth) {
        v.type = JValue::Type::Object;
        ++p_;   // '{'
        skipWs();
        if (p_ < s_.size() && s_[p_] == '}') { ++p_; return true; }
        while (true) {
            skipWs();
            if (p_ >= s_.size() || s_[p_] != '"') return fail("expected object key");
            std::string key;
            if (!parseString(key)) return false;
            skipWs();
            if (p_ >= s_.size() || s_[p_] != ':') return fail("expected ':'");
            ++p_;
            v.keys.push_back(key);
            v.children.emplace_back();
            if (!parseValue(v.children.back(), depth + 1)) return false;
            skipWs();
            if (p_ < s_.size() && s_[p_] == ',') { ++p_; continue; }
            if (p_ < s_.size() && s_[p_] == '}') { ++p_; return true; }
            return fail("expected ',' or '}'");
        }
    }

    bool parseArray(JValue& v, int depth) {
        v.type = JValue::Type::Array;
        ++p_;   // '['
        skipWs();
        if (p_ < s_.size() && s_[p_] == ']') { ++p_; return true; }
        while (true) {
            v.children.emplace_back();
            if (!parseValue(v.children.back(), depth + 1)) return false;
            skipWs();
            if (p_ < s_.size() && s_[p_] == ',') { ++p_; continue; }
            if (p_ < s_.size() && s_[p_] == ']') { ++p_; return true; }
            return fail("expected ',' or ']'");
        }
    }

    static void appendUtf8(std::string& out, unsigned cp) {
        if (cp < 0x80) {
            out += static_cast<char>(cp);
        } else if (cp < 0x800) {
            out += static_cast<char>(0xC0 | (cp >> 6));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else if (cp < 0x10000) {
            out += static_cast<char>(0xE0 | (cp >> 12));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        } else {
            out += static_cast<char>(0xF0 | (cp >> 18));
            out += static_cast<char>(0x80 | ((cp >> 12) & 0x3F));
            out += static_cast<char>(0x80 | ((cp >> 6) & 0x3F));
            out += static_cast<char>(0x80 | (cp & 0x3F));
        }
    }

    bool hex4(unsigned& out) {
        if (p_ + 4 > s_.size()) return fail("truncated \\u escape");
        out = 0;
        for (int i = 0; i < 4; ++i) {
            char c = s_[p_++];
            unsigned d;
            if (c >= '0' && c <= '9') d = static_cast<unsigned>(c - '0');
            else if (c >= 'a' && c <= 'f') d = static_cast<unsigned>(c - 'a' + 10);
            else if (c >= 'A' && c <= 'F') d = static_cast<unsigned>(c - 'A' + 10);
            else return fail("bad hex digit in \\u escape");
            out = (out << 4) | d;
        }
        return true;
    }

    bool parseString(std::string& out) {
        if (p_ >= s_.size() || s_[p_] != '"') return fail("expected string");
        ++p_;
        out.clear();
        while (p_ < s_.size()) {
            char c = s_[p_++];
            if (c == '"') return true;
            if (c != '\\') { out += c; continue; }
            if (p_ >= s_.size()) break;
            char e = s_[p_++];
            switch (e) {
                case '"':  out += '"';  break;
                case '\\': out += '\\'; break;
                case '/':  out += '/';  break;
                case 'b':  out += '\b'; break;
                case 'f':  out += '\f'; break;
                case 'n':  out += '\n'; break;
                case 'r':  out += '\r'; break;
                case 't':  out += '\t'; break;
                case 'u': {
                    unsigned cp = 0;
                    if (!hex4(cp)) return false;
                    if (cp >= 0xD800 && cp <= 0xDBFF && p_ + 1 < s_.size() &&
                        s_[p_] == '\\' && s_[p_ + 1] == 'u') {
                        p_ += 2;
                        unsigned lo = 0;
                        if (!hex4(lo)) return false;
                        if (lo >= 0xDC00 && lo <= 0xDFFF) {
                            cp = 0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00);
                        } else {
                            appendUtf8(out, cp);
                            cp = lo;
                        }
                    }
                    appendUtf8(out, cp);
                    break;
                }
                default: return fail("invalid escape sequence");
            }
        }
        return fail("unterminated string");
    }

    bool parseNumber(JValue& v) {
        size_t start = p_;
        if (p_ < s_.size() && (s_[p_] == '-' || s_[p_] == '+')) ++p_;
        while (p_ < s_.size()) {
            char c = s_[p_];
            if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' ||
                c == '+' || c == '-') {
                ++p_;
            } else {
                break;
            }
        }
        if (p_ == start) return fail("unexpected character");
        std::string tok = s_.substr(start, p_ - start);
        char* end = nullptr;
        double d = std::strtod(tok.c_str(), &end);
        if (end == tok.c_str()) { p_ = start; return fail("malformed number"); }
        v.type = JValue::Type::Number;
        v.num = d;
        v.boolean = d != 0;
        return true;
    }

    const std::string& s_;
    size_t p_ = 0;
    std::string err_;
};

const JValue* jfind(const JValue* o, const char* key) {
    if (!o || o->type != JValue::Type::Object) return nullptr;
    for (size_t i = 0; i < o->keys.size() && i < o->children.size(); ++i) {
        if (o->keys[i] == key) return &o->children[i];
    }
    return nullptr;
}

double jnum(const JValue* o, const char* key, double def = 0.0) {
    const JValue* v = jfind(o, key);
    if (!v || (v->type != JValue::Type::Number && v->type != JValue::Type::Bool)) return def;
    return v->num;
}

uint64_t ju64(const JValue* o, const char* key) {
    double d = jnum(o, key, 0.0);
    return d > 0 ? static_cast<uint64_t>(d + 0.5) : 0;
}

long long ji64(const JValue* o, const char* key, long long def = 0) {
    const JValue* v = jfind(o, key);
    if (!v || (v->type != JValue::Type::Number && v->type != JValue::Type::Bool)) return def;
    return static_cast<long long>(std::llround(v->num));
}

bool jbool(const JValue* o, const char* key, bool def = false) {
    const JValue* v = jfind(o, key);
    if (!v) return def;
    if (v->type == JValue::Type::Bool) return v->boolean;
    if (v->type == JValue::Type::Number) return v->num != 0;
    return def;
}

std::string jstr(const JValue* o, const char* key, const std::string& def = "") {
    const JValue* v = jfind(o, key);
    if (!v || v->type != JValue::Type::String) return def;
    return v->str;
}

template <class T>
std::vector<T> jNumVec(const JValue* o, const char* key) {
    std::vector<T> out;
    const JValue* v = jfind(o, key);
    if (!v || v->type != JValue::Type::Array) return out;
    out.reserve(v->children.size());
    for (const JValue& e : v->children) {
        if (e.type == JValue::Type::Number || e.type == JValue::Type::Bool) {
            out.push_back(static_cast<T>(e.num));
        } else {
            out.push_back(T{});
        }
    }
    return out;
}

std::vector<uint64_t> jU64Vec(const JValue* o, const char* key) {
    std::vector<uint64_t> out;
    const JValue* v = jfind(o, key);
    if (!v || v->type != JValue::Type::Array) return out;
    out.reserve(v->children.size());
    for (const JValue& e : v->children) {
        out.push_back(e.type == JValue::Type::Number && e.num > 0
                          ? static_cast<uint64_t>(e.num + 0.5)
                          : 0);
    }
    return out;
}

template <class Arr>
void jFillArray(const JValue* o, const char* key, Arr& dst) {
    const JValue* v = jfind(o, key);
    if (!v || v->type != JValue::Type::Array) return;
    for (size_t i = 0; i < v->children.size() && i < dst.size(); ++i) {
        const JValue& e = v->children[i];
        if (e.type == JValue::Type::Number) {
            dst[i] = static_cast<typename Arr::value_type>(e.num);
        }
    }
}

struct CfgFromJson {
    const JValue* cfg;
    void b(const char* k, bool& v) { v = jbool(cfg, k, v); }
    void i(const char* k, int& v) { v = static_cast<int>(ji64(cfg, k, v)); }
    void l(const char* k, long long& v) { v = ji64(cfg, k, v); }
    void d(const char* k, double& v) { v = jnum(cfg, k, v); }
    void s(const char* k, std::string& v) { v = jstr(cfg, k, v); }
    void mode(const char* k, RunMode& v) {
        const JValue* e = jfind(cfg, k);
        if (e && e->type == JValue::Type::String) v = runModeFromName(e->str);
    }
    void umiloc(const char* k, UmiLoc& v) {
        const JValue* e = jfind(cfg, k);
        if (e && e->type == JValue::Type::String) v = umiLocFromName(e->str);
    }
    void roi(const char*, long long) {}
    void ros(const char*, const std::string&) {}
};

// Per-cycle read depth implied by the length histogram: a read of length L
// contributes to every cycle below L.
std::vector<uint64_t> depthFromLengths(const std::vector<uint64_t>& lengthHist, size_t cycles) {
    std::vector<uint64_t> depth(cycles, 0);
    if (lengthHist.empty() || cycles == 0) return depth;
    uint64_t running = 0;
    for (size_t len = lengthHist.size(); len-- > 0;) {
        running += lengthHist[len];
        if (len > 0 && len - 1 < cycles) depth[len - 1] = running;
    }
    // Cycles beyond the histogram tail keep the deepest running total.
    for (size_t c = 0; c + 1 < cycles; ++c) {
        if (depth[c] == 0 && depth[c + 1] > 0) depth[c] = depth[c + 1];
    }
    return depth;
}

void rebuildReadSet(const JValue* obj, const JValue* qualDist, const std::vector<uint64_t>& lengths,
                    int qualOffset, ReadSetStats& s) {
    s.qualOffset = qualOffset;
    if (!obj) return;

    s.reads = ju64(obj, "total_reads");
    s.bases = ju64(obj, "total_bases");
    s.q20Bases = ju64(obj, "q20_bases");
    s.q30Bases = ju64(obj, "q30_bases");
    // Clamped exactly like the accumulator clamps it, so a corrupt report
    // cannot drive an unbounded allocation here.
    long long cycles = ji64(obj, "total_cycles", 0);
    if (cycles < 0) cycles = 0;
    if (cycles > kMaxCycles) cycles = kMaxCycles;
    s.cycles = static_cast<size_t>(cycles);
    const size_t C = s.cycles;

    s.lengthHist = lengths;
    if (!lengths.empty()) {
        for (size_t len = 0; len < lengths.size(); ++len) {
            if (lengths[len] == 0) continue;
            if (s.minLength == 0 || len < s.minLength) s.minLength = len;
            if (len > s.maxLength) s.maxLength = len;
        }
    }

    for (auto& b : s.baseCount) b.assign(C, 0);
    s.cycleDepth.assign(C, 0);
    s.cycleQualSum.assign(C, 0);
    s.cycleQ20.assign(C, 0);
    s.cycleQ30.assign(C, 0);
    s.cycleQualHist.clear();

    bool haveHist = false;
    if (qualDist && qualDist->type == JValue::Type::Array && !qualDist->children.empty()) {
        haveHist = true;
        s.positionalHist = true;
        s.cycleQualHist.assign(C, std::array<uint32_t, kQualBins>{});
        for (size_t c = 0; c < C && c < qualDist->children.size(); ++c) {
            const JValue& row = qualDist->children[c];
            if (row.type != JValue::Type::Array) continue;
            uint64_t depth = 0, qsum = 0, q20 = 0, q30 = 0;
            for (size_t q = 0; q < row.children.size() && q < kQualBins; ++q) {
                const JValue& e = row.children[q];
                if (e.type != JValue::Type::Number || e.num <= 0) continue;
                uint32_t cnt = static_cast<uint32_t>(e.num + 0.5);
                s.cycleQualHist[c][q] = cnt;
                depth += cnt;
                qsum += static_cast<uint64_t>(q) * cnt;
                if (q >= 20) q20 += cnt;
                if (q >= 30) q30 += cnt;
            }
            s.cycleDepth[c] = depth;
            s.cycleQualSum[c] = qsum;
            s.cycleQ20[c] = q20;
            s.cycleQ30[c] = q30;
        }
    } else {
        s.positionalHist = false;
        std::vector<uint64_t> depth = depthFromLengths(lengths, C);
        if (depth.empty() || (C > 0 && depth[0] == 0)) depth.assign(C, s.reads);
        s.cycleDepth = depth;
        std::vector<double> mean;
        const JValue* curves = jfind(obj, "quality_curves");
        if (curves) mean = jNumVec<double>(curves, "mean");
        for (size_t c = 0; c < C; ++c) {
            double m = c < mean.size() ? mean[c] : 0.0;
            s.cycleQualSum[c] = static_cast<uint64_t>(m * static_cast<double>(s.cycleDepth[c]) + 0.5);
        }
    }

    const JValue* content = jfind(obj, "content_curves");
    if (content) {
        std::vector<double> fa = jNumVec<double>(content, "A");
        std::vector<double> ft = jNumVec<double>(content, "T");
        std::vector<double> fc = jNumVec<double>(content, "C");
        std::vector<double> fg = jNumVec<double>(content, "G");
        std::vector<double> fn = jNumVec<double>(content, "N");
        auto at = [](const std::vector<double>& v, size_t i) { return i < v.size() ? v[i] : 0.0; };
        uint64_t gc = 0, ns = 0;
        for (size_t c = 0; c < C; ++c) {
            double depth = static_cast<double>(s.cycleDepth[c]);
            s.baseCount[0][c] = static_cast<uint64_t>(at(fa, c) * depth + 0.5);
            s.baseCount[1][c] = static_cast<uint64_t>(at(fc, c) * depth + 0.5);
            s.baseCount[2][c] = static_cast<uint64_t>(at(fg, c) * depth + 0.5);
            s.baseCount[3][c] = static_cast<uint64_t>(at(ft, c) * depth + 0.5);
            s.baseCount[4][c] = static_cast<uint64_t>(at(fn, c) * depth + 0.5);
            gc += s.baseCount[1][c] + s.baseCount[2][c];
            ns += s.baseCount[4][c];
        }
        s.gcBases = gc;
        s.nBases = ns;
    }

    // Per-base quality sums are rebuilt from the reported curves and the base
    // counts, so a re-rendered report keeps its per-base quality view.
    for (auto& v : s.cycleQualSumByBase) v.assign(C, 0);
    if (const JValue* curves = jfind(obj, "quality_curves")) {
        const std::vector<double> qa = jNumVec<double>(curves, "A");
        const std::vector<double> qc = jNumVec<double>(curves, "C");
        const std::vector<double> qg = jNumVec<double>(curves, "G");
        const std::vector<double> qt = jNumVec<double>(curves, "T");
        const std::vector<double>* byIndex[4] = {&qa, &qc, &qg, &qt};
        for (size_t b = 0; b < 4; ++b) {
            const std::vector<double>& v = *byIndex[b];
            for (size_t c = 0; c < C && c < v.size(); ++c) {
                s.cycleQualSumByBase[b][c] = static_cast<uint64_t>(
                    v[c] * static_cast<double>(s.baseCount[b][c]) + 0.5);
            }
        }
    }

    uint64_t qualTotal = 0;
    for (uint64_t q : s.cycleQualSum) qualTotal += q;
    s.qualSumTotal = qualTotal;
    if (haveHist) {
        uint64_t q20 = 0, q30 = 0;
        for (uint64_t v : s.cycleQ20) q20 += v;
        for (uint64_t v : s.cycleQ30) q30 += v;
        if (q20) s.q20Bases = q20;
        if (q30) s.q30Bases = q30;
    }

    const JValue* kmers = jfind(obj, "kmer_count");
    if (kmers && kmers->type == JValue::Type::Object) {
        size_t total = 1;
        for (int i = 0; i < kStatKmerK; ++i) total *= 4;
        s.kmerProfile.assign(total, 0);
        for (size_t i = 0; i < kmers->keys.size() && i < kmers->children.size(); ++i) {
            const std::string& key = kmers->keys[i];
            if (key.size() != static_cast<size_t>(kStatKmerK)) continue;
            size_t index = 0;
            bool ok = true;
            for (char ch : key) {
                size_t code;
                switch (ch) {
                    case 'A': code = 0; break;
                    case 'C': code = 1; break;
                    case 'G': code = 2; break;
                    case 'T': code = 3; break;
                    default: ok = false; code = 0; break;
                }
                if (!ok) break;
                index = index * 4 + code;
            }
            if (!ok || index >= total) continue;
            const JValue& e = kmers->children[i];
            if (e.type == JValue::Type::Number && e.num > 0) {
                s.kmerProfile[index] = static_cast<uint64_t>(e.num + 0.5);
            }
        }
    }

    s.gcPercent = ratio(s.gcBases, s.bases) * 100.0;
    s.q20Percent = ratio(s.q20Bases, s.bases) * 100.0;
    s.q30Percent = ratio(s.q30Bases, s.bases) * 100.0;
    s.meanLength = ratio(s.bases, s.reads);
    s.meanQuality = ratio(s.qualSumTotal, s.bases);
}

void rebuildOverrep(const JValue* arr, OverrepAnalyzer& o) {
    if (!arr || arr->type != JValue::Type::Array) return;
    o.enabled = true;
    for (const JValue& e : arr->children) {
        if (e.type != JValue::Type::Object) continue;
        OverrepHit h;
        h.seq = jstr(&e, "sequence");
        h.count = ju64(&e, "count");
        h.percent = jnum(&e, "percent");
        h.source = jstr(&e, "source");
        h.positions = jU64Vec(&e, "positions");
        o.hits.push_back(std::move(h));
    }
}

}  // namespace

bool reportFromJson(const std::string& jsonPath, const std::string& htmlPath, std::string& error) {
    error.clear();
    std::ifstream in(jsonPath, std::ios::binary);
    if (!in) {
        error = "cannot open " + jsonPath;
        return false;
    }
    std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    if (text.empty()) {
        error = jsonPath + ": file is empty";
        return false;
    }

    JValue root;
    JsonParser parser(text);
    std::string perr;
    if (!parser.parse(root, perr)) {
        error = jsonPath + ": " + perr;
        return false;
    }
    if (root.type != JValue::Type::Object) {
        error = jsonPath + ": top-level value is not an object";
        return false;
    }

    // Accept reports written under the old project name. Renaming the tool must not
    // orphan the reports it has already produced: those are measurements of libraries
    // that may no longer be available to re-measure, so a reader that rejects them turns
    // a cosmetic rename into data loss.
    const JValue* fplus = jfind(&root, "scepter");
    if (!fplus || fplus->type != JValue::Type::Object) fplus = jfind(&root, "fastplus");
    if (!fplus || fplus->type != JValue::Type::Object) {
        error = jsonPath + ": missing the \"scepter\" object (or the legacy \"fastplus\" "
                           "one); not a scepter report";
        return false;
    }

    Options opt;
    const JValue* cfg = jfind(fplus, "config");
    if (cfg && cfg->type == JValue::Type::Object) {
        CfgFromJson reader{cfg};
        visitConfig(opt, reader);
    }
    opt.jsonFile = jsonPath;
    opt.htmlFile = htmlPath;
    const int qualOffset = opt.qualityOffset();

    RunStats stats;
    stats.version = jstr(fplus, "version", jstr(jfind(&root, "summary"), "fastp_version"));
    stats.startedAt = jstr(fplus, "started_at");
    stats.command = jstr(fplus, "command", jstr(&root, "command"));
    stats.durationSeconds = jnum(fplus, "duration_seconds");

    const JValue* run = jfind(fplus, "run");
    if (run) {
        stats.readsIn = ju64(run, "reads_in");
        stats.readsOut = ju64(run, "reads_out");
        stats.basesIn = ju64(run, "bases_in");
        stats.basesOut = ju64(run, "bases_out");
        stats.correctedReads = ju64(run, "corrected_reads");
        stats.correctedBases = ju64(run, "corrected_bases");
        stats.mergedPairs = ju64(run, "merged_pairs");
        stats.polyGTrimmedReads = ju64(run, "polyg_trimmed_reads");
        stats.polyGTrimmedBases = ju64(run, "polyg_trimmed_bases");
        stats.polyXTrimmedReads = ju64(run, "polyx_trimmed_reads");
        stats.polyXTrimmedBases = ju64(run, "polyx_trimmed_bases");
        stats.adapterTrimmedReads1 = ju64(run, "adapter_trimmed_reads1");
        stats.adapterTrimmedReads2 = ju64(run, "adapter_trimmed_reads2");
        stats.adapterTrimmedBases1 = ju64(run, "adapter_trimmed_bases1");
        stats.adapterTrimmedBases2 = ju64(run, "adapter_trimmed_bases2");
        stats.adapterStart1 = jU64Vec(run, "adapter_start_read1");
        stats.adapterStart2 = jU64Vec(run, "adapter_start_read2");
    }

    const JValue* fdetail = jfind(fplus, "filtering_detail");
    if (fdetail) {
        for (size_t i = 0; i < static_cast<size_t>(FilterResult::kCount); ++i) {
            stats.filterCounts[i] = ju64(fdetail, kFilterKeys[i]);
        }
    } else if (const JValue* fr = jfind(&root, "filtering_result")) {
        stats.filterCounts[static_cast<size_t>(FilterResult::Pass)] =
            ju64(fr, "passed_filter_reads");
        stats.filterCounts[static_cast<size_t>(FilterResult::FailedQuality)] =
            ju64(fr, "low_quality_reads");
        stats.filterCounts[static_cast<size_t>(FilterResult::FailedNBase)] =
            ju64(fr, "too_many_N_reads");
        stats.filterCounts[static_cast<size_t>(FilterResult::FailedLength)] =
            ju64(fr, "too_short_reads");
        stats.filterCounts[static_cast<size_t>(FilterResult::FailedTooLong)] =
            ju64(fr, "too_long_reads");
    }

    if (const JValue* ac = jfind(&root, "adapter_cutting")) {
        if (opt.adapter.resolvedR1.empty()) {
            opt.adapter.resolvedR1 = jstr(ac, "read1_adapter_sequence");
        }
        if (opt.adapter.resolvedR2.empty()) {
            opt.adapter.resolvedR2 = jstr(ac, "read2_adapter_sequence");
        }
        for (int which = 0; which < 2; ++which) {
            const JValue* counts = jfind(ac, which == 0 ? "read1_adapter_counts"
                                                        : "read2_adapter_counts");
            if (!counts || counts->type != JValue::Type::Object) continue;
            auto& dst = which == 0 ? stats.adapterCounts1 : stats.adapterCounts2;
            for (size_t i = 0; i < counts->keys.size() && i < counts->children.size(); ++i) {
                const JValue& e = counts->children[i];
                if (e.type == JValue::Type::Number && e.num > 0) {
                    dst[counts->keys[i]] = static_cast<uint64_t>(e.num + 0.5);
                }
            }
        }
        if (stats.adapterTrimmedReads1 == 0 && stats.adapterTrimmedReads2 == 0) {
            stats.adapterTrimmedReads1 = ju64(ac, "adapter_trimmed_reads");
            stats.adapterTrimmedBases1 = ju64(ac, "adapter_trimmed_bases");
        }
    }

    // Duplication: the analyzer's table is private, so only the reported
    // results are restored; that is everything the HTML report consumes.
    if (const JValue* dd = jfind(fplus, "duplication_detail")) {
        stats.duplication.duplicationRate = jnum(dd, "rate");
        stats.duplication.estimatedLibrarySize = jnum(dd, "estimated_library_size");
        stats.duplication.totalObserved = ju64(dd, "total_observed");
        stats.duplication.uniqueObserved = ju64(dd, "unique_observed");
        const JValue* levels = jfind(dd, "levels");
        if (levels && levels->type == JValue::Type::Array) {
            for (size_t i = 0; i < levels->children.size() && i < stats.duplication.levels.size();
                 ++i) {
                stats.duplication.levels[i] = jnum(&levels->children[i], "percent");
            }
        }
    }
    if (const JValue* dup = jfind(&root, "duplication")) {
        if (stats.duplication.duplicationRate == 0) {
            stats.duplication.duplicationRate = jnum(dup, "rate");
        }
    }

    if (const JValue* is = jfind(fplus, "insert_size_detail")) {
        stats.insertSize.mode = static_cast<int>(ji64(is, "mode"));
        stats.insertSize.mean = jnum(is, "mean");
        stats.insertSize.median = jnum(is, "median");
        stats.insertSize.stddev = jnum(is, "stddev");
        stats.insertSize.p10 = static_cast<int>(ji64(is, "p10"));
        stats.insertSize.p90 = static_cast<int>(ji64(is, "p90"));
        stats.insertSize.counted = ju64(is, "counted");
        stats.insertSize.unknown = ju64(is, "unknown");
        stats.insertSize.maxSize = static_cast<int>(ji64(is, "max_size", 1000));
        stats.insertSize.histogram = jU64Vec(is, "histogram");
    }

    if (const JValue* pt = jfind(fplus, "per_tile")) {
        stats.tiles1.globalMeanQuality = jnum(pt, "global_mean_quality");
        const JValue* outliers = jfind(pt, "outlier_tiles");
        if (outliers && outliers->type == JValue::Type::Array) {
            for (const JValue& e : outliers->children) {
                if (e.type == JValue::Type::String) stats.tiles1.outlierTiles.push_back(e.str);
            }
        }
        const JValue* tiles = jfind(pt, "tiles");
        if (tiles && tiles->type == JValue::Type::Object && jbool(pt, "usable", true)) {
            for (size_t i = 0; i < tiles->keys.size() && i < tiles->children.size(); ++i) {
                const JValue& t = tiles->children[i];
                TileStats ts;
                ts.reads = ju64(&t, "reads");
                std::vector<double> curve = jNumVec<double>(&t, "quality_by_cycle");
                ts.qualSum.assign(curve.size(), 0);
                ts.depth.assign(curve.size(), 0);
                for (size_t c = 0; c < curve.size(); ++c) {
                    ts.depth[c] = ts.reads;
                    ts.qualSum[c] = static_cast<uint64_t>(curve[c] * static_cast<double>(ts.reads) + 0.5);
                }
                stats.tiles1.cycles = std::max(stats.tiles1.cycles, curve.size());
                stats.tiles1.tiles_[tiles->keys[i]] = std::move(ts);
            }
        }
    }

    if (const JValue* em = jfind(fplus, "error_model")) {
        ErrorModel& e = stats.errorModel;
        e.pairsExamined = ju64(em, "pairs_examined");
        e.basesCompared = ju64(em, "bases_compared");
        e.mismatches = ju64(em, "mismatches");
        e.overallErrorRate = jnum(em, "error_rate");
        e.overallQ = jnum(em, "empirical_q");
        e.calibrationBias = jnum(em, "calibration_bias");
        const JValue* cal = jfind(em, "calibration");
        if (cal && cal->type == JValue::Type::Array) {
            for (const JValue& c : cal->children) {
                int q = static_cast<int>(ji64(&c, "reported_q", -1));
                if (q < 0 || q >= kQualBins) continue;
                e.calibration.emplace_back(q, jnum(&c, "observed_q"));
                e.qTotal[static_cast<size_t>(q)] = ju64(&c, "observations");
            }
        }
        const JValue* subs = jfind(em, "substitutions");
        if (subs && subs->type == JValue::Type::Object) {
            const std::string bases = "ACGT";
            for (size_t i = 0; i < subs->keys.size() && i < subs->children.size(); ++i) {
                size_t from = bases.find(subs->keys[i]);
                if (from == std::string::npos) continue;
                const JValue& row = subs->children[i];
                for (size_t t = 0; t < 4; ++t) {
                    e.substitutions[from][t] = ju64(&row, std::string(1, bases[t]).c_str());
                }
            }
        }
        std::vector<double> r1 = jNumVec<double>(em, "cycle_error_rate_read1");
        std::vector<double> r2 = jNumVec<double>(em, "cycle_error_rate_read2");
        e.cycleTotal1.assign(r1.size(), 0);
        e.cycleErrors1.assign(r1.size(), 0);
        e.cycleTotal2.assign(r2.size(), 0);
        e.cycleErrors2.assign(r2.size(), 0);
        // Only the rate survives the round trip; a nominal denominator keeps the
        // per-cycle curve identical when it is recomputed.
        const uint64_t kNominal = 1000000;
        for (size_t c = 0; c < r1.size(); ++c) {
            e.cycleTotal1[c] = kNominal;
            e.cycleErrors1[c] = static_cast<uint64_t>(r1[c] * static_cast<double>(kNominal) + 0.5);
        }
        for (size_t c = 0; c < r2.size(); ++c) {
            e.cycleTotal2[c] = kNominal;
            e.cycleErrors2[c] = static_cast<uint64_t>(r2[c] * static_cast<double>(kNominal) + 0.5);
        }
    }

    if (const JValue* det = jfind(fplus, "detection")) {
        DetectionSummary& d = stats.detection;
        d.strategy = jstr(det, "sample_strategy", d.strategy);
        d.sampleReads1 = ji64(det, "sample_reads_read1");
        d.sampleReads2 = ji64(det, "sample_reads_read2");
        d.recordsScanned = ji64(det, "records_scanned");
        d.exactRecordCount = jbool(det, "exact_record_count");
        d.seconds = jnum(det, "sample_seconds");
        d.sampleMeanQuality = jnum(det, "sample_mean_quality");
        d.sampleGcFraction = jnum(det, "sample_gc_content");
        d.instrument = jstr(det, "instrument");
        d.platform = jstr(det, "platform");
        d.tileHeadersUsable = jbool(det, "tile_headers_usable");
        d.polyGRecommended = jbool(det, "polyg_recommended");
    }

    if (const JValue* cn = jfind(fplus, "contamination")) {
        ContamResult& c = stats.contamination;
        c.attempted = jbool(cn, "enabled");
        c.ok = jbool(cn, "ok");
        c.message = jstr(cn, "message");
        c.dbPath = jstr(cn, "database");
        c.k = static_cast<int>(ji64(cn, "k", 31));
        c.readsScreened = ju64(cn, "reads_screened");
        c.kmersScreened = ju64(cn, "kmers_screened");
        c.totalHits = ju64(cn, "total_hits");
        c.minFraction = jnum(cn, "min_fraction");
        c.noiseFloor = jnum(cn, "noise_floor");
        c.verdict = jstr(cn, "verdict");
        c.dominant = jstr(cn, "dominant_species");
        c.dominantFraction = jnum(cn, "dominant_fraction");
        c.topContaminant = jstr(cn, "top_contaminant");
        c.topContaminantFraction = jnum(cn, "top_contaminant_fraction");
        const JValue* list = jfind(cn, "species");
        if (list && list->type == JValue::Type::Array) {
            for (const JValue& e : list->children) {
                ContamSpeciesResult s;
                s.name = jstr(&e, "name");
                s.commonContaminant = jbool(&e, "common_contaminant");
                s.present = jbool(&e, "present");
                s.sketchSize = ju64(&e, "sketch_kmers");
                s.matchedKmers = ju64(&e, "matched_kmers");
                s.hits = ju64(&e, "hits");
                s.libraryFraction = jnum(&e, "library_fraction");
                s.relativeAbundance = jnum(&e, "relative_abundance");
                s.estimatedDepth = jnum(&e, "estimated_depth");
                s.genomeLength = ji64(&e, "genome_length");
                c.species.push_back(std::move(s));
            }
        }
    }

    if (const JValue* ks = jfind(fplus, "kmer_spectrum")) {
        int k = static_cast<int>(ji64(ks, "k", 21));
        uint64_t scale = ju64(ks, "scale");
        stats.kmers = KmerSpectrum(k, scale ? scale : 64);
        stats.kmers.reliable = jbool(ks, "reliable");
        stats.kmers.peakDepth = static_cast<int>(ji64(ks, "peak_depth"));
        stats.kmers.estimatedGenomeSize = jnum(ks, "estimated_genome_size");
        stats.kmers.estimatedDepth = jnum(ks, "estimated_depth");
        stats.kmers.errorKmerFraction = jnum(ks, "error_kmer_fraction");
        stats.kmers.repeatFraction = jnum(ks, "repeat_fraction");
        stats.kmers.distinctSketchKmers = ju64(ks, "distinct_sketch_kmers");
        stats.kmers.totalKmers = ju64(ks, "total_kmers");
        stats.kmers.histogram = jU64Vec(ks, "histogram");
    }

    const JValue* lengths = jfind(fplus, "length_distribution");
    const JValue* qdist = jfind(fplus, "quality_distribution");
    rebuildReadSet(jfind(&root, "read1_before_filtering"), jfind(qdist, "read1"),
                   jU64Vec(lengths, "read1_before"), qualOffset, stats.before1);
    rebuildReadSet(jfind(&root, "read2_before_filtering"), jfind(qdist, "read2"),
                   jU64Vec(lengths, "read2_before"), qualOffset, stats.before2);
    rebuildReadSet(jfind(&root, "read1_after_filtering"), jfind(qdist, "read1_after"),
                   jU64Vec(lengths, "read1_after"), qualOffset, stats.after1);
    rebuildReadSet(jfind(&root, "read2_after_filtering"), jfind(qdist, "read2_after"),
                   jU64Vec(lengths, "read2_after"), qualOffset, stats.after2);
    rebuildReadSet(jfind(&root, "merged_and_filtered"), nullptr,
                   jU64Vec(lengths, "merged"), qualOffset, stats.merged);

    if (const JValue* cx = jfind(fplus, "complexity")) {
        jFillArray(cx, "read1_histogram", stats.before1.complexityHist);
        jFillArray(cx, "read2_histogram", stats.before2.complexityHist);
    }
    if (const JValue* gc = jfind(fplus, "gc_distribution")) {
        jFillArray(gc, "read1_before", stats.before1.gcHist);
        jFillArray(gc, "read2_before", stats.before2.gcHist);
    }
    if (const JValue* mq = jfind(fplus, "mean_quality_distribution")) {
        jFillArray(mq, "read1_before", stats.before1.meanQualHist);
        jFillArray(mq, "read2_before", stats.before2.meanQualHist);
    }
    if (const JValue* ov = jfind(fplus, "overrepresented")) {
        rebuildOverrep(jfind(ov, "read1"), stats.overrep1);
        rebuildOverrep(jfind(ov, "read2"), stats.overrep2);
    }

    if (stats.readsIn == 0) stats.readsIn = stats.before1.reads + stats.before2.reads;
    if (stats.readsOut == 0) stats.readsOut = stats.after1.reads + stats.after2.reads;
    if (stats.basesIn == 0) stats.basesIn = stats.before1.bases + stats.before2.bases;
    if (stats.basesOut == 0) stats.basesOut = stats.after1.bases + stats.after2.bases;

    writeHtmlReport(htmlPath, stats, opt);
    return true;
}

}  // namespace fp
