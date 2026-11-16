// Statistics collected over a read set, plus the deeper analyses that fastp
// does not perform: per-tile quality, empirical quality calibration, per-cycle
// error profiles, k-mer spectrum / genome size, and library complexity.
//
// Every accumulator is per-worker and combined with merge() at the end, so no
// locking happens on the hot path.
#pragma once

#include <array>
#include <cstdint>
#include <map>
#include <string>
#include <unordered_map>
#include <vector>

#include "common.h"

namespace fp {

// ---------------------------------------------------------------------------
// Per-read-set statistics (one instance per stream, per phase: before/after)
// ---------------------------------------------------------------------------
class ReadSetStats {
public:
    explicit ReadSetStats(int qualOffset = 33, bool positionalHist = true);

    void add(const Read& r);
    void merge(const ReadSetStats& other);
    void finalize();

    // --- totals ---
    uint64_t reads = 0;
    uint64_t bases = 0;
    uint64_t q20Bases = 0;
    uint64_t q30Bases = 0;
    uint64_t q40Bases = 0;
    uint64_t nBases = 0;
    uint64_t gcBases = 0;
    uint64_t qualSumTotal = 0;

    size_t cycles = 0;          // longest read seen
    size_t minLength = 0;
    size_t maxLength = 0;

    // --- per cycle (index 0 == first base) ---
    // baseCount[b][cycle] with b in {A,C,G,T,N}
    std::array<std::vector<uint64_t>, 5> baseCount;
    std::vector<uint64_t> cycleDepth;    // reads covering this cycle
    std::vector<uint64_t> cycleQualSum;
    // Same sums split by the base called at that cycle, so a per-base quality
    // curve can be reported (fastp's quality_curves A/T/C/G).
    std::array<std::vector<uint64_t>, 5> cycleQualSumByBase;
    std::vector<uint64_t> cycleQ20;
    std::vector<uint64_t> cycleQ30;

    // Full per-cycle quality histogram (enables box plots, not just means).
    bool positionalHist = true;
    std::vector<std::array<uint32_t, kQualBins>> cycleQualHist;

    // --- distributions ---
    std::vector<uint64_t> lengthHist;              // index = read length
    std::array<uint64_t, 101> gcHist{};            // percent GC, 0..100
    std::array<uint64_t, kQualBins> meanQualHist{};// per-read mean quality
    std::array<uint64_t, 101> complexityHist{};    // percent complexity

    // 5-mer composition over the whole set (1024 entries).
    std::vector<uint64_t> kmerProfile;

    // --- derived, filled by finalize() ---
    double gcPercent = 0;
    double q20Percent = 0;
    double q30Percent = 0;
    double meanLength = 0;
    double meanQuality = 0;
    double duplicationHint = 0;

    int qualOffset = 33;

    // Per-cycle helpers used by the reporters.
    double cycleMeanQuality(size_t c) const;
    // Mean quality at `cycle` over only the reads carrying `base` there.
    double cycleMeanQualityForBase(size_t cycle, int base) const;   // base index 0..4
    double cycleBaseFraction(size_t c, int base) const;   // base index 0..4
    double cycleGcFraction(size_t c) const;
    double cycleQ20Fraction(size_t c) const;
    // Quality quantile at a cycle, q in [0,1]. Requires positionalHist.
    int cycleQualQuantile(size_t c, double q) const;

private:
    void grow(size_t n);
};

// ---------------------------------------------------------------------------
// Per-tile quality, parsed from Illumina read headers
// ---------------------------------------------------------------------------
struct TileStats {
    std::vector<uint64_t> qualSum;   // per cycle
    std::vector<uint64_t> depth;     // per cycle
    uint64_t reads = 0;
    uint64_t errors = 0;             // from overlap analysis, when available
    uint64_t errorOpportunities = 0;
};

class TileAnalyzer {
public:
    // Returns false if the header is not in a recognised Illumina layout, in
    // which case per-tile analysis is silently skipped for the run.
    static bool parseTile(const std::string& header, std::string& lane, int& tile);

    void add(const std::string& header, const Read& r, int qualOffset);
    void addError(const std::string& header, uint64_t errors, uint64_t opportunities);
    void merge(const TileAnalyzer& other);
    void finalize();

    bool usable() const { return usable_; }
    size_t tileCount() const { return tiles_.size(); }

    // tileKey -> stats, key formatted as "lane:tile"
    std::map<std::string, TileStats> tiles_;
    size_t cycles = 0;

    // Filled by finalize(): tiles whose mean quality deviates from the global
    // mean by more than 2 Q units, which usually indicates a flow-cell defect.
    std::vector<std::string> outlierTiles;
    double globalMeanQuality = 0;

private:
    bool usable_ = true;
    bool checked_ = false;
};

// ---------------------------------------------------------------------------
// Empirical error model, derived from the overlap of properly paired reads.
//
// Where R1 and R2 overlap and agree almost everywhere, a disagreement is
// treated as an error in the lower-quality base. That yields an observed error
// rate per reported quality value (a calibration curve) and per cycle.
// ---------------------------------------------------------------------------
class ErrorModel {
public:
    ErrorModel();

    // Record one overlap comparison.
    void observe(const Read& r1, const Read& r2, const OverlapResult& ov, int qualOffset);
    void merge(const ErrorModel& other);
    void finalize();

    // Reported quality -> (observed errors, opportunities)
    std::array<uint64_t, kQualBins> qErrors{};
    std::array<uint64_t, kQualBins> qTotal{};

    // Per cycle (R1 and R2 tracked separately).
    std::vector<uint64_t> cycleErrors1, cycleTotal1;
    std::vector<uint64_t> cycleErrors2, cycleTotal2;

    // 4x4 substitution matrix, indexed [from][to] over ACGT.
    std::array<std::array<uint64_t, 4>, 4> substitutions{};

    uint64_t pairsExamined = 0;
    uint64_t basesCompared = 0;
    uint64_t mismatches = 0;

    // Derived.
    double overallErrorRate = 0;
    double overallQ = 0;
    // Calibration: for each reported Q with enough data, the observed Q.
    std::vector<std::pair<int, double>> calibration;
    double calibrationBias = 0;   // mean(observed Q - reported Q)

    bool hasData() const { return basesCompared > 1000; }
};

// ---------------------------------------------------------------------------
// k-mer spectrum via a scaled MinHash sketch.
//
// Only k-mers whose 64-bit hash is below (2^64 / scale) are kept, so memory is
// bounded regardless of input size while the retained set stays an unbiased
// sample of the full k-mer population. From the resulting count histogram we
// estimate genome size, sequencing depth, and the error k-mer fraction.
// ---------------------------------------------------------------------------
class KmerSpectrum {
public:
    explicit KmerSpectrum(int k = 21, uint64_t scale = 64, int samplingFactor = 1);

    void add(const Read& r);
    void merge(const KmerSpectrum& other);
    void finalize();

    int k() const { return k_; }
    uint64_t scale() const { return scale_; }

    // count -> number of distinct k-mers with that count (index 0 unused)
    std::vector<uint64_t> histogram;

    uint64_t distinctSketchKmers = 0;
    uint64_t totalKmers = 0;          // all k-mers seen, not just the sketch

    // Only 1 read in `samplingFactor` reaches the sketch, and a read of length
    // L yields L-k+1 k-mers rather than L bases. finalize() undoes both so that
    // estimatedDepth is a base depth comparable to an aligner's coverage.
    int samplingFactor = 1;
    size_t readLength = 0;            // longest read fed to the sketch

    // Derived by finalize().
    int peakDepth = 0;                // coverage mode above the error shoulder
    size_t massFrom = 0;              // count at which genomic mass integration began
    double estimatedGenomeSize = 0;
    // Same estimate restricted to single-copy sequence: comparable to a
    // reference assembly's length, where a high-copy plasmid appears once.
    double estimatedGenomeSizeCore = 0;
    double estimatedDepth = 0;
    double errorKmerFraction = 0;     // fraction of distinct k-mers at count 1-2
    double repeatFraction = 0;        // k-mers well above the main peak
    bool reliable = false;

private:
    void maybeDownscale();

    int k_;
    uint64_t scale_;
    uint64_t threshold_;
    std::unordered_map<uint64_t, uint32_t> counts_;
    static constexpr size_t kMaxEntries = 6u << 20;   // ~6M before downscaling
};

// ---------------------------------------------------------------------------
// Duplication level estimate and library complexity
// ---------------------------------------------------------------------------
class DuplicationAnalyzer {
public:
    // `level` 1..6 selects the table size (1G..24G in fastp terms); we use a
    // far smaller table and compensate statistically.
    explicit DuplicationAnalyzer(int level = 1, bool enabled = true);

    // Returns true when this read/pair is a duplicate of one already seen.
    bool observe(const Read& r1, const Read* r2);
    void merge(const DuplicationAnalyzer& other);
    void finalize();

    double duplicationRate = 0;
    // FastQC-style duplication levels: 1,2,3..9,>10,>50,>100,>500,>1k,>5k,>10k
    std::array<double, 16> levels{};
    static const char* levelLabel(int i);

    // Lander-Waterman style projection of unique molecules in the library.
    double estimatedLibrarySize = 0;
    uint64_t totalObserved = 0;
    uint64_t uniqueObserved = 0;
    bool enabled = true;

private:
    uint64_t keyFor(const Read& r1, const Read* r2) const;

    size_t bits_;
    bool mergedAny_ = false;   // aggregate adopts the first worker's table shape
    std::vector<uint32_t> table_;    // open-addressed count-min style buckets
    std::vector<uint64_t> keys_;
};

// ---------------------------------------------------------------------------
// Overrepresented sequences with positional profiles
// ---------------------------------------------------------------------------
struct OverrepHit {
    std::string seq;
    uint64_t count = 0;
    double percent = 0;
    std::vector<uint64_t> positions;   // per-cycle occurrence profile
    std::string source;                // matched contaminant name, if any
};

class OverrepAnalyzer {
public:
    explicit OverrepAnalyzer(int sampling = 20, bool enabled = false);

    void add(const Read& r, uint64_t readIndex);
    void merge(const OverrepAnalyzer& other);
    void finalize(uint64_t totalReads);

    std::vector<OverrepHit> hits;
    bool enabled = false;
    int sampling = 20;
    uint64_t sampled = 0;

private:
    std::unordered_map<std::string, uint64_t> counts_;
    std::unordered_map<std::string, std::vector<uint64_t>> positions_;
    size_t cycles_ = 0;
};

// Look up a sequence against the built-in adapter / primer / contaminant list.
// Returns an empty string when nothing matches.
std::string identifyContaminant(const std::string& seq);

// ---------------------------------------------------------------------------
// Insert size distribution
// ---------------------------------------------------------------------------
class InsertSizeAnalyzer {
public:
    explicit InsertSizeAnalyzer(int maxSize = 1000);

    void observe(int insertSize);
    void merge(const InsertSizeAnalyzer& other);
    void finalize();

    std::vector<uint64_t> histogram;
    uint64_t counted = 0;
    uint64_t unknown = 0;      // pairs where no overlap could be established
    int mode = 0;
    double mean = 0;
    double median = 0;
    double stddev = 0;
    int p10 = 0, p90 = 0;
    int maxSize = 1000;
};

// ---------------------------------------------------------------------------
// What the startup sampler looked at, and what it concluded. Reported so the
// cost of sampling and the basis of every auto-detection is auditable.
// ---------------------------------------------------------------------------
struct DetectionSummary {
    std::string strategy = "head";
    long long sampleReads1 = 0;
    long long sampleReads2 = 0;
    long long recordsScanned = 0;
    bool exactRecordCount = false;
    double seconds = 0;
    double sampleMeanQuality = 0;
    double sampleGcFraction = 0;
    std::string instrument;
    std::string platform;
    bool tileHeadersUsable = false;
    bool polyGRecommended = false;
    double polyGTailFraction = 0;
};

// ---------------------------------------------------------------------------
// Contamination screen result (see contam.h for the analysis itself)
// ---------------------------------------------------------------------------
struct ContamSpeciesResult {
    std::string name;
    bool commonContaminant = false;
    uint64_t sketchSize = 0;
    uint64_t matchedKmers = 0;    // distinct sketch k-mers hit at least once
    uint64_t hits = 0;            // read k-mer hits, with multiplicity
    double libraryFraction = 0;   // estimated share of the whole library
    double relativeAbundance = 0; // share among the panel species that were called
    double estimatedDepth = 0;    // estimated sequencing depth of this species
    long long genomeLength = 0;
    bool present = false;
};

struct ContamResult {
    bool attempted = false;
    bool ok = false;
    std::string message;          // why the screen did not run, when !ok
    std::string dbPath;
    int k = 31;
    uint64_t readsScreened = 0;
    uint64_t kmersScreened = 0;
    uint64_t totalHits = 0;
    double minFraction = 0.01;
    double noiseFloor = 0;
    std::string verdict;          // pure | minor contamination | mixed | none
    std::string dominant;
    double dominantFraction = 0;  // relative abundance of the dominant species
    std::string topContaminant;
    double topContaminantFraction = 0;
    std::vector<ContamSpeciesResult> species;   // descending by relative abundance
};

// ---------------------------------------------------------------------------
// Aggregate of everything, one per run
// ---------------------------------------------------------------------------
struct RunStats {
    DetectionSummary detection;
    ContamResult contamination;

    ReadSetStats before1, before2;
    ReadSetStats after1, after2;
    ReadSetStats merged;

    TileAnalyzer tiles1, tiles2;
    ErrorModel errorModel;
    KmerSpectrum kmers;
    DuplicationAnalyzer duplication;
    OverrepAnalyzer overrep1, overrep2;
    InsertSizeAnalyzer insertSize;

    // Filter accounting.
    std::array<uint64_t, static_cast<size_t>(FilterResult::kCount)> filterCounts{};

    uint64_t readsIn = 0;
    uint64_t readsOut = 0;
    uint64_t basesIn = 0;
    uint64_t basesOut = 0;

    uint64_t adapterTrimmedReads1 = 0;
    uint64_t adapterTrimmedReads2 = 0;
    uint64_t adapterTrimmedBases1 = 0;
    uint64_t adapterTrimmedBases2 = 0;
    std::map<std::string, uint64_t> adapterCounts1;
    std::map<std::string, uint64_t> adapterCounts2;

    uint64_t polyGTrimmedReads = 0;
    uint64_t polyGTrimmedBases = 0;
    uint64_t polyXTrimmedReads = 0;
    uint64_t polyXTrimmedBases = 0;

    uint64_t correctedReads = 0;
    uint64_t correctedBases = 0;
    uint64_t mergedPairs = 0;

    // Per-cycle adapter content, for the adapter-content curve.
    std::vector<uint64_t> adapterStart1, adapterStart2;

    double durationSeconds = 0;
    std::string command;
    std::string version;
    std::string startedAt;

    void mergeWorker(const RunStats& w);
    void finalize();
};

}  // namespace fp
