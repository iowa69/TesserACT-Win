// Every knob the tool exposes. Mirrors fastp's option set exactly, then adds
// the analysis and usability options that fastp does not have.
#pragma once

#include <string>
#include <vector>

namespace fp {

enum class UmiLoc { None, Index1, Index2, Read1, Read2, PerIndex, PerRead };

struct AdapterOptions {
    bool enabled = true;
    std::string sequence = "auto";
    std::string sequenceR2 = "auto";
    std::string fastaFile;
    bool detectForPE = false;
    bool allowGapOverlapTrimming = false;

    // Resolved during startup.
    std::string resolvedR1;
    std::string resolvedR2;
    bool detectedR1 = false;
    bool detectedR2 = false;
    std::vector<std::string> extraSeqs;
    std::vector<std::string> extraNames;
};

struct TrimOptions {
    int front1 = 0, tail1 = 0, maxLen1 = 0;
    int front2 = -1, tail2 = -1, maxLen2 = -1;  // -1 means "follow read1"
    int resolvedFront2() const { return front2 < 0 ? front1 : front2; }
    int resolvedTail2()  const { return tail2  < 0 ? tail1  : tail2; }
    int resolvedMaxLen2() const { return maxLen2 < 0 ? maxLen1 : maxLen2; }
};

struct PolyGOptions {
    bool forced = false;      // -g
    bool disabled = false;    // -G
    bool enabled = false;     // resolved: forced, or auto-enabled for NextSeq/NovaSeq
    int minLen = 10;
};

struct PolyXOptions {
    bool enabled = false;
    int minLen = 10;
};

struct CutOptions {
    bool front = false, tail = false, right = false;
    int windowSize = 4, meanQuality = 20;
    int frontWindow = -1, frontQuality = -1;
    int tailWindow  = -1, tailQuality  = -1;
    int rightWindow = -1, rightQuality = -1;

    int fw() const { return frontWindow < 0 ? windowSize : frontWindow; }
    int fq() const { return frontQuality < 0 ? meanQuality : frontQuality; }
    int tw() const { return tailWindow  < 0 ? windowSize : tailWindow; }
    int tq() const { return tailQuality < 0 ? meanQuality : tailQuality; }
    int rw() const { return rightWindow < 0 ? windowSize : rightWindow; }
    int rq() const { return rightQuality < 0 ? meanQuality : rightQuality; }
    bool any() const { return front || tail || right; }
};

struct QualityFilterOptions {
    bool enabled = true;
    int qualifiedPhred = 15;
    int unqualifiedPercentLimit = 40;
    int nBaseLimit = 5;
    int averageQual = 0;
};

struct LengthFilterOptions {
    bool enabled = true;
    int required = 15;
    int limit = 0;    // 0 = no upper bound
};

struct ComplexityFilterOptions {
    bool enabled = false;
    int threshold = 30;
};

struct IndexFilterOptions {
    bool enabled = false;
    std::string file1, file2;
    int threshold = 0;
    std::vector<std::string> blacklist1, blacklist2;
};

struct OverlapOptions {
    int lenRequire = 30;
    int diffLimit = 5;
    int diffPercentLimit = 20;
};

struct CorrectionOptions {
    bool enabled = false;
};

struct UmiOptions {
    bool enabled = false;
    UmiLoc loc = UmiLoc::None;
    int len = 0;
    std::string prefix;
    int skip = 0;
    std::string delim = ":";
};

struct DuplicationOptions {
    bool dedup = false;
    bool evaluate = true;
    int accuracyLevel = 0;   // 0 = auto (1 without dedup, 3 with)
    int resolvedAccuracy() const {
        if (accuracyLevel >= 1 && accuracyLevel <= 6) return accuracyLevel;
        return dedup ? 3 : 1;
    }
};

struct OverrepOptions {
    bool enabled = false;
    int sampling = 20;
};

struct SplitOptions {
    bool enabled = false;
    int number = 0;         // -s
    long long byLines = 0;  // -S
    int digits = 4;
    bool byFileNumber() const { return number > 0; }
};

struct MergeOptions {
    bool enabled = false;
    std::string out;
    bool includeUnmerged = false;
};

// ---------------------------------------------------------------------------
// Analyses that fastp does not perform. All are on by default because they are
// cheap relative to I/O; each can be disabled individually.
// ---------------------------------------------------------------------------
struct DeepOptions {
    bool perTile = true;             // per-tile quality and error maps
    bool kmerSpectrum = true;        // k-mer spectrum + genome size estimate
    // Resolved at startup from the estimated input size: the sketch needs a
    // certain depth to place the error/genome valley, but nothing above that.
    int kmerSamplingFactor = 1;
    int  kmerK = 21;
    bool errorProfile = true;        // per-cycle error rate from PE overlaps
    bool qualityCalibration = true;  // reported Q vs empirically observed Q
    bool insertSize = true;
    bool duplicationCurve = true;    // duplication histogram + library complexity
    bool contaminantScreen = true;   // built-in adapter/primer/rRNA screen
    bool positionalQualityDist = true;  // full per-cycle quality histogram
    long long genomeSize = 0;        // if set, report estimated coverage
    bool disableAll = false;
};

// How the startup sample used for auto-detection (and for the contamination
// screen) is drawn from the input.
struct SamplingOptions {
    long long reads = 200000;   // --sample-reads
    bool headOnly = false;      // --sample-head: only the head of the file
};

// Opt-in clinical contamination screen against the shipped species sketch.
struct ContamOptions {
    bool enabled = false;       // --check-contamination
    std::string dbPath;         // --contam-db, empty = look for the shipped DB
    double minFraction = 0.01;  // --contam-min-fraction, reporting floor
};

enum class RunMode { Process, QcOnly, ReportOnly };

struct Options {
    // ---- I/O -------------------------------------------------------------
    std::string in1, in2;
    std::string out1, out2;
    std::string unpaired1, unpaired2;
    std::string failedOut;
    std::string overlappedOut;
    std::string outDir;              // convenience: auto-name outputs here

    bool stdinInput = false;
    bool stdoutOutput = false;
    bool interleavedIn = false;
    bool dontOverwrite = false;
    bool fixMgiId = false;
    bool phred64 = false;
    int  compression = 4;
    long long readsToProcess = 0;

    // ---- reporting -------------------------------------------------------
    std::string jsonFile;
    std::string htmlFile;
    std::string tsvFile;             // machine-friendly flat summary
    std::string reportTitle = "scepter report";
    bool verbose = false;
    bool quiet = false;
    bool noSummary = false;

    // ---- execution -------------------------------------------------------
    int threads = 0;                 // 0 = auto-detect
    std::string commandLine;         // verbatim argv, echoed into the reports
    RunMode mode = RunMode::Process;
    bool dryRun = false;
    bool explain = false;
    std::string preset;

    // ---- stages ----------------------------------------------------------
    AdapterOptions adapter;
    TrimOptions trim;
    PolyGOptions polyG;
    PolyXOptions polyX;
    CutOptions cut;
    QualityFilterOptions qualityFilter;
    LengthFilterOptions lengthFilter;
    ComplexityFilterOptions complexityFilter;
    IndexFilterOptions indexFilter;
    OverlapOptions overlap;
    CorrectionOptions correction;
    UmiOptions umi;
    DuplicationOptions duplication;
    OverrepOptions overrep;
    SplitOptions split;
    MergeOptions merge;
    DeepOptions deep;
    SamplingOptions sampling;
    ContamOptions contam;

    // Directory the binary was started from, used to find the shipped
    // contamination database next to it.
    std::string programDir;

    // ---- derived ---------------------------------------------------------
    bool isPaired() const { return !in2.empty() || interleavedIn; }
    bool writesOutput() const {
        return mode == RunMode::Process &&
               (!out1.empty() || !out2.empty() || stdoutOutput || !merge.out.empty());
    }

    // Fill in defaults that depend on other options (output names, thread count,
    // adapter resolution inputs, phred offset). Returns false and fills `error`
    // when the combination is invalid.
    bool finalize(std::string& error);

    // Apply a named preset before user flags are applied.
    bool applyPreset(const std::string& name, std::string& error);

    // Human-readable description of the active configuration.
    std::string describe() const;

    int qualityOffset() const { return phred64 ? 64 : 33; }
};

// Names of all presets, for help output.
std::vector<std::pair<std::string, std::string>> presetList();

}  // namespace fp
