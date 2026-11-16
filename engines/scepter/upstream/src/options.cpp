#include "options.h"

#include <algorithm>
#include <fstream>
#include <sstream>
#include <thread>

#include "util.h"

namespace fp {

namespace {

constexpr const char* kMgiAdapterR1 = "AAGTCGGAGGCCAAGCGGTCTTAGGAAGACAA";
constexpr const char* kMgiAdapterR2 = "AAGTCGGATCGTAGCCATGTCGTTCTGTGAGCCAAGGAGTTG";

bool inRange(std::string& error, const char* flag, long long v, long long lo, long long hi) {
    if (v >= lo && v <= hi) return true;
    std::ostringstream os;
    os << flag << " must be between " << lo << " and " << hi << " (got " << v << ")";
    error = os.str();
    return false;
}

bool atLeast(std::string& error, const char* flag, long long v, long long lo) {
    if (v >= lo) return true;
    std::ostringstream os;
    os << flag << " must be at least " << lo << " (got " << v << ")";
    error = os.str();
    return false;
}

// Catches "./x.fq" vs "x.fq" and symlinks, which plain string comparison misses.
bool sameFile(const std::string& a, const std::string& b) {
    if (a.empty() || b.empty() || a == "-" || b == "-") return false;
    if (a == b) return true;
    struct stat sa, sb;
    if (::stat(a.c_str(), &sa) != 0 || ::stat(b.c_str(), &sb) != 0) return false;
    return sa.st_dev == sb.st_dev && sa.st_ino == sb.st_ino;
}

std::string joinPath(const std::string& dir, const std::string& file) {
    if (dir.empty()) return file;
    if (dir.back() == '/') return dir + file;
    return dir + "/" + file;
}

const char* umiLocName(UmiLoc loc) {
    switch (loc) {
        case UmiLoc::Index1:   return "index1";
        case UmiLoc::Index2:   return "index2";
        case UmiLoc::Read1:    return "read1";
        case UmiLoc::Read2:    return "read2";
        case UmiLoc::PerIndex: return "per_index";
        case UmiLoc::PerRead:  return "per_read";
        case UmiLoc::None:     break;
    }
    return "none";
}

bool loadBarcodeList(const std::string& path, std::vector<std::string>& out, std::string& error) {
    std::ifstream in(path);
    if (!in) {
        error = "cannot open index list file '" + path + "'";
        return false;
    }
    std::string line;
    while (std::getline(in, line)) {
        std::string t = util::trim(line);
        if (t.empty() || t[0] == '#') continue;
        out.push_back(util::toUpper(t));
    }
    if (out.empty()) {
        error = "index list file '" + path + "' contains no barcodes";
        return false;
    }
    return true;
}

bool loadAdapterFasta(const std::string& path, std::vector<std::string>& seqs,
                      std::vector<std::string>& names, std::string& error) {
    std::ifstream in(path);
    if (!in) {
        error = "cannot open adapter FASTA '" + path + "'";
        return false;
    }
    std::string line, name, seq;
    auto flush = [&]() {
        std::string s = util::toUpper(util::trim(seq));
        seq.clear();
        if (s.empty()) return;
        seqs.push_back(s);
        names.push_back(name.empty() ? ("adapter" + std::to_string(seqs.size())) : name);
    };
    while (std::getline(in, line)) {
        std::string t = util::trim(line);
        if (t.empty()) continue;
        if (t[0] == '>') {
            flush();
            name = util::trim(t.substr(1));
        } else {
            seq += t;
        }
    }
    flush();
    if (seqs.empty()) {
        error = "adapter FASTA '" + path + "' contains no sequences";
        return false;
    }
    // The trimmer matches whole adapters; anything shorter than this is noise.
    for (size_t i = 0; i < seqs.size(); ++i) {
        if (seqs[i].size() < 6) {
            error = "adapter sequence '" + names[i] + "' in " + path +
                    " is shorter than 6 bases; that would match at random";
            return false;
        }
    }
    return true;
}

// Section accumulator used by describe().
struct Section {
    std::string title;
    std::vector<std::pair<std::string, std::string>> rows;
    void add(std::string key, std::string value) {
        rows.emplace_back(std::move(key), std::move(value));
    }
    void add(std::string key, long long value) {
        rows.emplace_back(std::move(key), std::to_string(value));
    }
};

std::string onOff(bool b) { return b ? "on" : "off"; }

}  // namespace

// ---------------------------------------------------------------------------
// finalize
// ---------------------------------------------------------------------------

bool Options::finalize(std::string& error) {
    error.clear();

    if (threads <= 0) {
        unsigned hc = std::thread::hardware_concurrency();
        if (hc == 0) hc = 4;
        threads = static_cast<int>(std::min<unsigned>(16u, hc));
    }

    // ---- ranges ----------------------------------------------------------
    if (!inRange(error, "--compression (-z)", compression, 1, 9)) return false;
    if (!inRange(error, "--qualified_quality_phred (-q)", qualityFilter.qualifiedPhred, 0, 60))
        return false;
    if (!inRange(error, "--unqualified_percent_limit (-u)",
                 qualityFilter.unqualifiedPercentLimit, 0, 100))
        return false;
    if (!inRange(error, "--complexity_threshold (-Y)", complexityFilter.threshold, 0, 100))
        return false;

    struct WindowSpec { const char* flag; int value; bool optional; };
    const WindowSpec windows[] = {
        {"--cut_window_size (-W)", cut.windowSize, false},
        {"--cut_front_window_size", cut.frontWindow, true},
        {"--cut_tail_window_size", cut.tailWindow, true},
        {"--cut_right_window_size", cut.rightWindow, true},
    };
    for (const auto& w : windows) {
        if (w.optional && w.value == -1) continue;
        if (!inRange(error, w.flag, w.value, 1, 1000)) return false;
    }

    const WindowSpec quals[] = {
        {"--cut_mean_quality (-M)", cut.meanQuality, false},
        {"--cut_front_mean_quality", cut.frontQuality, true},
        {"--cut_tail_mean_quality", cut.tailQuality, true},
        {"--cut_right_mean_quality", cut.rightQuality, true},
    };
    for (const auto& q : quals) {
        if (q.optional && q.value == -1) continue;
        if (!inRange(error, q.flag, q.value, 1, 36)) return false;
    }

    if (split.number != 0 && !inRange(error, "--split (-s)", split.number, 2, 999)) return false;
    if (split.byLines != 0 &&
        !atLeast(error, "--split_by_lines (-S)", split.byLines, 1000))
        return false;
    if (!inRange(error, "--split_prefix_digits (-d)", split.digits, 0, 10)) return false;
    if (!inRange(error, "--overrepresentation_sampling (-P)", overrep.sampling, 1, 10000))
        return false;
    if (!inRange(error, "--umi_len", umi.len, 0, 100)) return false;

    // ---- mutually exclusive combinations ---------------------------------
    if (split.number > 0 && split.byLines > 0) {
        error = "--split (-s) and --split_by_lines (-S) cannot be used together; "
                "pick either a file count or a line count";
        return false;
    }
    if (stdoutOutput && split.enabled) {
        error = "--stdout cannot be combined with --split/--split_by_lines; "
                "split output needs real files to write to";
        return false;
    }
    if (merge.enabled && merge.out.empty() && !stdoutOutput) {
        error = "--merge needs somewhere to put the merged reads; "
                "add --merged_out FILE or --stdout";
        return false;
    }
    if (interleavedIn && !in2.empty()) {
        error = "--interleaved_in reads both mates from a single stream, "
                "so a second input file (-I/--in2 '" + in2 + "') cannot be given";
        return false;
    }
    if (umi.enabled && umi.loc == UmiLoc::None) {
        error = "--umi (-U) requires --umi_loc "
                "(index1, index2, read1, read2, per_index or per_read)";
        return false;
    }
    if (umi.enabled && (umi.loc == UmiLoc::Read1 || umi.loc == UmiLoc::Read2 ||
                        umi.loc == UmiLoc::PerRead) &&
        umi.len == 0) {
        error = std::string("--umi_loc ") + umiLocName(umi.loc) +
                " takes the UMI from the read itself, so --umi_len must be > 0";
        return false;
    }

    // ---- output naming ---------------------------------------------------
    if (!outDir.empty()) {
        if (!util::makeDirs(outDir)) {
            error = "cannot create output directory '" + outDir + "'";
            return false;
        }
        if (mode == RunMode::Process && out1.empty() && out2.empty()) {
            if (!in1.empty()) out1 = joinPath(outDir, util::basename(in1));
            if (!in2.empty()) out2 = joinPath(outDir, util::basename(in2));
        }
    }

    // A single input with no destination at all is a report-only run: outputs
    // stay empty on purpose, nothing is invented.

    if (sameFile(out1, in1)) {
        error = "output file '" + out1 + "' is the same file as the input '" + in1 +
                "'; choose a different --out-dir or -o";
        return false;
    }
    if (sameFile(out2, in2)) {
        error = "output file '" + out2 + "' is the same file as the input '" + in2 +
                "'; choose a different --out-dir or -O";
        return false;
    }

    if (mode == RunMode::QcOnly) {
        out1.clear();
        out2.clear();
        unpaired1.clear();
        unpaired2.clear();
        merge.out.clear();
        merge.enabled = false;
    }

    // ---- reports ---------------------------------------------------------
    // Only the auto-chosen names follow --out-dir; an explicit -j/-h/--tsv path
    // is taken exactly as given.
    if (mode != RunMode::ReportOnly) {
        auto defaultReport = [this](const char* name) {
            return outDir.empty() ? std::string(name) : joinPath(outDir, name);
        };
        if (jsonFile.empty()) jsonFile = defaultReport("scepter.json");
        if (htmlFile.empty()) htmlFile = defaultReport("scepter.html");
    }

    if (dontOverwrite) {
        const std::string* targets[] = {&out1,     &out2,      &unpaired1, &unpaired2,
                                        &failedOut, &overlappedOut, &merge.out,
                                        &jsonFile, &htmlFile, &tsvFile};
        for (const std::string* t : targets) {
            if (t->empty() || *t == "-") continue;
            if (util::fileExists(*t)) {
                error = "--dont_overwrite is set and '" + *t + "' already exists";
                return false;
            }
        }
    }

    // ---- stage resolution ------------------------------------------------
    if (polyG.forced) {
        polyG.enabled = true;
    } else if (polyG.disabled) {
        polyG.enabled = false;
    }
    // Otherwise polyG.enabled is left for instrument auto-detection.

    if (deep.disableAll) {
        deep.perTile = false;
        deep.kmerSpectrum = false;
        deep.errorProfile = false;
        deep.qualityCalibration = false;
        deep.insertSize = false;
        deep.duplicationCurve = false;
        deep.contaminantScreen = false;
        deep.positionalQualityDist = false;
    }

    // ---- auxiliary input files -------------------------------------------
    adapter.extraSeqs.clear();
    adapter.extraNames.clear();
    if (!adapter.fastaFile.empty() &&
        !loadAdapterFasta(adapter.fastaFile, adapter.extraSeqs, adapter.extraNames, error))
        return false;

    indexFilter.blacklist1.clear();
    indexFilter.blacklist2.clear();
    if (!indexFilter.file1.empty()) {
        if (!loadBarcodeList(indexFilter.file1, indexFilter.blacklist1, error)) return false;
        indexFilter.enabled = true;
    }
    if (!indexFilter.file2.empty()) {
        if (!loadBarcodeList(indexFilter.file2, indexFilter.blacklist2, error)) return false;
        indexFilter.enabled = true;
    }

    return true;
}

// ---------------------------------------------------------------------------
// presets
// ---------------------------------------------------------------------------

std::vector<std::pair<std::string, std::string>> presetList() {
    return {
        {"illumina-pe",  "Illumina paired-end: adapter auto-detection for both mates"},
        {"illumina-se",  "Illumina single-end: adapter auto-detection (the default)"},
        {"novaseq",      "NovaSeq two-colour chemistry: force poly-G trimming"},
        {"nextseq",      "Alias of novaseq: two-colour chemistry, force poly-G"},
        {"bgi",          "BGI/MGI: MGI adapters and MGI read-name fixing"},
        {"mgi",          "Alias of bgi"},
        {"strict",       "Aggressive quality/length filtering for sensitive analyses"},
        {"lenient",      "Keep almost everything; only obvious junk is removed"},
        {"amplicon",     "Amplicon panels: long reads kept, no dedup, overrep analysis"},
        {"rnaseq",       "RNA-seq: poly-X trimming, short-fragment tolerant"},
        {"wgs-bacteria", "Bacterial WGS: window trimming, overlap correction, k-mer spectrum"},
        {"assembly",     "Tuned for feeding a de novo assembler"},
    };
}

bool Options::applyPreset(const std::string& name, std::string& error) {
    std::string key = util::toLower(util::trim(name));
    std::replace(key.begin(), key.end(), '_', '-');

    auto unknown = [&]() {
        std::ostringstream os;
        os << "unknown preset '" << name << "'. Valid presets are:";
        for (const auto& p : presetList()) os << "\n    " << p.first << "  -  " << p.second;
        error = os.str();
        return false;
    };

    if (key == "illumina-pe" || key == "illumina-se") {
        adapter.enabled = true;
        adapter.sequence = "auto";
        adapter.sequenceR2 = "auto";
        adapter.detectForPE = (key == "illumina-pe");
    } else if (key == "novaseq" || key == "nextseq") {
        polyG.forced = true;
        polyG.enabled = true;
        polyG.disabled = false;
        polyG.minLen = 10;
    } else if (key == "bgi" || key == "mgi") {
        fixMgiId = true;
        adapter.enabled = true;
        adapter.sequence = kMgiAdapterR1;
        adapter.sequenceR2 = kMgiAdapterR2;
    } else if (key == "strict") {
        qualityFilter.enabled = true;
        qualityFilter.qualifiedPhred = 20;
        qualityFilter.unqualifiedPercentLimit = 20;
        qualityFilter.nBaseLimit = 0;
        qualityFilter.averageQual = 20;
        lengthFilter.enabled = true;
        lengthFilter.required = 50;
        cut.right = true;
        cut.meanQuality = 20;
        complexityFilter.enabled = true;
    } else if (key == "lenient") {
        qualityFilter.qualifiedPhred = 10;
        qualityFilter.unqualifiedPercentLimit = 60;
        qualityFilter.nBaseLimit = 10;
        // Was 20, which is *above* the default of 15: a preset promising to
        // keep almost everything was discarding reads the default keeps.
        lengthFilter.required = 15;
    } else if (key == "amplicon") {
        adapter.enabled = true;
        lengthFilter.required = 100;
        cut.tail = true;
        complexityFilter.enabled = false;
        duplication.dedup = false;
        overrep.enabled = true;
    } else if (key == "rnaseq") {
        polyX.enabled = true;
        lengthFilter.required = 25;
        overrep.enabled = true;
    } else if (key == "wgs-bacteria") {
        // Bacterial WGS is overwhelmingly done to assemble, so this preset
        // matches `assembly` on the settings that were measured against closed
        // reference genomes. It used to run cut_right at Q20 with overlap
        // correction, which the table below `assembly` shows costs about 22% of
        // NGA50 on exactly this organism -- a preset should not ship the
        // configuration its neighbour documents as harmful.
        cut.tail = true;
        cut.tailQuality = 10;
        lengthFilter.required = 50;
        correction.enabled = false;
        duplication.dedup = false;
        deep.kmerSpectrum = true;
    } else if (key == "assembly") {
        // Deliberately gentle. This preset used to run cut_right at Q20 and
        // enable overlap correction, and both cost the assembler real
        // contiguity -- measured on Klebsiella isolates that have a complete
        // closed reference for the same DNA, assembling with everything else
        // held fixed:
        //
        //     cut_right Q20 + correction   NGA50 132,059   (what this was)
        //     cut_tail  Q10 + correction         149,192
        //     cut_tail  Q10, no correction       160,991
        //     fastp's own defaults               160,991
        //
        // cut_right truncates a read at the first sliding window that dips
        // below the threshold, which at Q20 discarded 13-24% of all bases
        // depending on the run. Overlap correction then rewrites disagreeing
        // bases in the overlap toward the higher-quality mate, which pre-empts
        // the assembler's own error model with a decision it cannot revisit.
        //
        // The lesson is the same one the k-mer abundance cutoff taught: an
        // assembler removes errors by graph topology, using evidence pooled
        // across every read covering a locus. A trimmer deciding per-read, in
        // advance, can only take coverage away from that. Filter for what is
        // unambiguously junk and leave the rest.
        cut.tail = true;
        cut.tailQuality = 10;
        lengthFilter.required = 50;
        correction.enabled = false;
        duplication.dedup = false;
        deep.kmerSpectrum = true;
        qualityFilter.averageQual = 15;
    } else {
        return unknown();
    }

    preset = key;
    return true;
}

// ---------------------------------------------------------------------------
// describe
// ---------------------------------------------------------------------------

std::string Options::describe() const {
    std::vector<Section> sections;

    // ---- Input / output --------------------------------------------------
    {
        Section s{"Input / output", {}};
        if (stdinInput) s.add("read 1 in", "<stdin>");
        else if (!in1.empty()) s.add("read 1 in", in1);
        if (interleavedIn) s.add("layout", "interleaved paired-end");
        else if (!in2.empty()) s.add("read 2 in", in2);
        else if (!stdinInput) s.add("layout", "single-end");

        if (stdoutOutput) s.add("read 1 out", "<stdout>");
        else if (!out1.empty()) s.add("read 1 out", out1);
        if (!out2.empty()) s.add("read 2 out", out2);
        if (!outDir.empty()) s.add("output directory", outDir);
        if (!unpaired1.empty()) s.add("unpaired 1 out", unpaired1);
        if (!unpaired2.empty()) s.add("unpaired 2 out", unpaired2);
        if (!failedOut.empty()) s.add("failed reads out", failedOut);
        if (!overlappedOut.empty()) s.add("overlapped out", overlappedOut);
        if (mode == RunMode::Process && out1.empty() && !stdoutOutput && merge.out.empty())
            s.add("mode", "report only (no FASTQ written)");

        if (!out1.empty() || !out2.empty() || stdoutOutput || !merge.out.empty())
            s.add("gzip level", compression);
        if (phred64) s.add("input encoding", "phred+64");
        if (readsToProcess > 0) s.add("reads to process", util::commify(readsToProcess));
        if (dontOverwrite) s.add("overwrite existing", "no");
        if (fixMgiId) s.add("fix MGI read names", "yes");
        if (split.enabled) {
            if (split.byFileNumber())
                s.add("split output", std::to_string(split.number) + " files");
            else
                s.add("split output", util::commify(split.byLines) + " lines per file");
            s.add("split name digits", split.digits);
        }
        sections.push_back(std::move(s));
    }

    // ---- Adapter ---------------------------------------------------------
    {
        Section s{"Adapter", {}};
        if (!adapter.enabled) {
            s.add("adapter trimming", "disabled");
        } else {
            s.add("read 1 adapter",
                  adapter.sequence == "auto" ? "auto-detect" : adapter.sequence);
            if (isPaired())
                s.add("read 2 adapter",
                      adapter.sequenceR2 == "auto" ? "auto-detect" : adapter.sequenceR2);
            if (adapter.detectForPE) s.add("PE overlap detection", "on");
            if (!adapter.fastaFile.empty()) {
                s.add("adapter FASTA", adapter.fastaFile);
                if (!adapter.extraSeqs.empty())
                    s.add("extra adapters", std::to_string(adapter.extraSeqs.size()) + " loaded");
            }
            if (adapter.allowGapOverlapTrimming) s.add("allow gaps in overlap", "yes");
        }
        sections.push_back(std::move(s));
    }

    // ---- Trimming --------------------------------------------------------
    {
        Section s{"Trimming", {}};
        if (trim.front1 > 0) s.add("trim front (R1)", trim.front1);
        if (trim.tail1 > 0) s.add("trim tail (R1)", trim.tail1);
        if (trim.maxLen1 > 0) s.add("max length (R1)", trim.maxLen1);
        if (isPaired()) {
            if (trim.resolvedFront2() > 0) s.add("trim front (R2)", trim.resolvedFront2());
            if (trim.resolvedTail2() > 0) s.add("trim tail (R2)", trim.resolvedTail2());
            if (trim.resolvedMaxLen2() > 0) s.add("max length (R2)", trim.resolvedMaxLen2());
        }
        if (polyG.disabled) {
            s.add("poly-G trimming", "disabled");
        } else if (polyG.forced || polyG.enabled) {
            s.add("poly-G trimming",
                  std::string(polyG.forced ? "forced" : "auto") + ", min run " +
                      std::to_string(polyG.minLen));
        } else {
            s.add("poly-G trimming", "auto (depends on instrument)");
        }
        if (polyX.enabled) s.add("poly-X trimming", "min run " + std::to_string(polyX.minLen));
        if (cut.front)
            s.add("cut front (5')", "window " + std::to_string(cut.fw()) + ", mean Q >= " +
                                        std::to_string(cut.fq()));
        if (cut.tail)
            s.add("cut tail (3')", "window " + std::to_string(cut.tw()) + ", mean Q >= " +
                                       std::to_string(cut.tq()));
        if (cut.right)
            s.add("cut right", "window " + std::to_string(cut.rw()) + ", mean Q >= " +
                                   std::to_string(cut.rq()));
        sections.push_back(std::move(s));
    }

    // ---- Filtering -------------------------------------------------------
    {
        Section s{"Filtering", {}};
        if (!qualityFilter.enabled) {
            s.add("quality filter", "disabled");
        } else {
            s.add("qualified base Q", qualityFilter.qualifiedPhred);
            s.add("max unqualified %", qualityFilter.unqualifiedPercentLimit);
            s.add("max N bases", qualityFilter.nBaseLimit);
            if (qualityFilter.averageQual > 0)
                s.add("min average Q", qualityFilter.averageQual);
        }
        if (!lengthFilter.enabled) {
            s.add("length filter", "disabled");
        } else {
            s.add("min length", lengthFilter.required);
            if (lengthFilter.limit > 0) s.add("max length", lengthFilter.limit);
        }
        if (complexityFilter.enabled)
            s.add("low-complexity filter", "threshold " + std::to_string(complexityFilter.threshold) + "%");
        if (indexFilter.enabled) {
            if (!indexFilter.file1.empty())
                s.add("index 1 blacklist",
                      indexFilter.file1 + " (" + std::to_string(indexFilter.blacklist1.size()) + ")");
            if (!indexFilter.file2.empty())
                s.add("index 2 blacklist",
                      indexFilter.file2 + " (" + std::to_string(indexFilter.blacklist2.size()) + ")");
            s.add("index mismatches", indexFilter.threshold);
        }
        if (duplication.dedup) s.add("deduplication", "on, accuracy " + std::to_string(duplication.resolvedAccuracy()));
        if (!duplication.evaluate) s.add("duplication estimate", "skipped");
        sections.push_back(std::move(s));
    }

    // ---- Paired-end ------------------------------------------------------
    if (isPaired()) {
        Section s{"Paired-end", {}};
        s.add("overlap min length", overlap.lenRequire);
        s.add("overlap max diff", std::to_string(overlap.diffLimit) + " bases / " +
                                      std::to_string(overlap.diffPercentLimit) + "%");
        if (correction.enabled) s.add("overlap correction", "on");
        if (merge.enabled) {
            s.add("merge overlapping", merge.out.empty() ? std::string("<stdout>") : merge.out);
            s.add("include unmerged", onOff(merge.includeUnmerged));
        }
        sections.push_back(std::move(s));
    }

    // ---- Analysis --------------------------------------------------------
    {
        Section s{"Analysis", {}};
        if (overrep.enabled)
            s.add("overrepresented seqs", "on, sampling 1/" + std::to_string(overrep.sampling));
        if (umi.enabled) {
            s.add("UMI location", umiLocName(umi.loc));
            if (umi.len > 0) s.add("UMI length", umi.len);
            if (umi.skip > 0) s.add("UMI skip bases", umi.skip);
            if (!umi.prefix.empty()) s.add("UMI prefix", umi.prefix);
            s.add("UMI delimiter", umi.delim);
        }
        if (deep.disableAll) {
            s.add("deep analysis", "all disabled");
        } else {
            std::string off;
            auto note = [&off](bool on, const char* label) {
                if (on) return;
                if (!off.empty()) off += ", ";
                off += label;
            };
            note(deep.perTile, "per-tile");
            note(deep.kmerSpectrum, "k-mer spectrum");
            note(deep.errorProfile, "error profile");
            note(deep.qualityCalibration, "quality calibration");
            note(deep.insertSize, "insert size");
            note(deep.duplicationCurve, "duplication curve");
            note(deep.contaminantScreen, "contaminant screen");
            note(deep.positionalQualityDist, "positional quality");
            s.add("deep analysis", off.empty() ? "all enabled" : "all except " + off);
            if (deep.kmerSpectrum) s.add("k-mer size", deep.kmerK);
        }
        if (deep.genomeSize > 0)
            s.add("expected genome size", util::commify(deep.genomeSize) + " bp");
        s.add("detection sample", util::commify(sampling.reads) + " reads" +
                                      (sampling.headOnly ? " (head of file)" : " (whole file)"));
        if (contam.enabled) {
            s.add("contamination screen", "on");
            s.add("contamination floor", std::to_string(contam.minFraction));
            if (!contam.dbPath.empty()) s.add("contamination DB", contam.dbPath);
        }
        sections.push_back(std::move(s));
    }

    // ---- Execution -------------------------------------------------------
    {
        Section s{"Execution", {}};
        s.add("threads", threads);
        switch (mode) {
            case RunMode::Process:    s.add("run mode", "process"); break;
            case RunMode::QcOnly:     s.add("run mode", "QC only (no reads written)"); break;
            case RunMode::ReportOnly: s.add("run mode", "regenerate report from JSON"); break;
        }
        if (!preset.empty()) s.add("preset", preset);
        if (!jsonFile.empty()) s.add("JSON report", jsonFile);
        if (!htmlFile.empty()) s.add("HTML report", htmlFile);
        if (!tsvFile.empty()) s.add("TSV summary", tsvFile);
        if (reportTitle != "scepter report") s.add("report title", reportTitle);
        if (verbose) s.add("verbose", "on");
        if (quiet) s.add("quiet", "on");
        if (noSummary) s.add("terminal summary", "off");
        sections.push_back(std::move(s));
    }

    // ---- render ----------------------------------------------------------
    const size_t keyWidth = 24;
    std::ostringstream os;
    os << "Effective configuration\n";
    for (const auto& s : sections) {
        if (s.rows.empty()) continue;
        os << "\n" << s.title << "\n";
        for (const auto& r : s.rows) {
            os << "  " << r.first;
            if (r.first.size() < keyWidth) os << std::string(keyWidth - r.first.size(), ' ');
            os << "  " << r.second << "\n";
        }
    }
    return os.str();
}

}  // namespace fp
