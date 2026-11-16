// The per-read transformation pipeline: UMI handling, trimming, adapter
// removal, overlap analysis, base correction, filtering and merging.
//
// A Worker owns all per-thread state (statistics accumulators and scratch
// buffers) so chunks can be processed with no synchronisation.
#pragma once

#include <mutex>
#include <string>
#include <unordered_set>
#include <vector>

#include "common.h"
#include "options.h"
#include "stats.h"

namespace fp {

// ---------------------------------------------------------------------------
// Overlap analysis
//
// `offset` is the position within read1's coordinate frame where the reverse
// complement of read2 begins. It may be negative when read1 runs past the end
// of the insert (adapter read-through). The insert length is always
// `offset + read2.length()`, which is what makes adapter trimming, base
// correction and merging fall out of a single computation.
// ---------------------------------------------------------------------------
OverlapResult analyzeOverlap(const Read& r1, const Read& r2, const OverlapOptions& opt,
                             bool allowGap);

// Insert length implied by an overlap result (0 when not overlapped).
inline int insertSizeOf(const OverlapResult& ov, const Read& r2) {
    return ov.overlapped ? ov.offset + static_cast<int>(r2.length()) : 0;
}

// ---------------------------------------------------------------------------
// Individual transformations. Each returns the number of bases removed or
// changed so the caller can keep statistics.
// ---------------------------------------------------------------------------

// Trim a 3' run of `base` (or the dominant tail base when `base` is 0),
// tolerating occasional mismatches. Returns bases trimmed.
size_t trimPolyTail(Read& r, char base, int minLen);

// Sliding-window quality trimming. Returns bases trimmed.
size_t cutFront(Read& r, int window, int meanQual, int qualOffset);
size_t cutTail(Read& r, int window, int meanQual, int qualOffset);
size_t cutRight(Read& r, int window, int meanQual, int qualOffset);

// Find where `adapter` begins in `r`, requiring the match to run to the 3' end.
// Returns the trim position, or -1 when no adapter is found.
int findAdapter(const Read& r, const std::string& adapter, int minMatch = 4);

// Mean quality of a read.
double meanQuality(const Read& r, int qualOffset);

// Percentage of positions where base[i] != base[i+1].
double sequenceComplexity(const Read& r);

// Hamming distance, stopping once `maxDiff` is exceeded.
int hammingDistance(const std::string& a, const std::string& b, int maxDiff);

// ---------------------------------------------------------------------------
// Exact, cross-thread deduplication.
//
// The per-worker DuplicationAnalyzer estimates the duplication *rate* cheaply,
// but dropping reads requires a view of every read seen so far. Sharding the
// key space keeps lock contention low enough that this stays off the critical
// path for the common case where --dedup is not requested.
// ---------------------------------------------------------------------------
class SharedDedup {
public:
    explicit SharedDedup(size_t shardBits = 8);

    // Returns true when this key has been seen before.
    bool seen(uint64_t key);
    size_t uniqueCount() const;

private:
    struct Shard {
        mutable std::mutex mu;
        std::unordered_set<uint64_t> keys;
    };
    // Declared before `shards_` so it is available to size the vector in the
    // member initialiser list; a mutex is neither copyable nor movable, so the
    // vector cannot be resized after construction.
    size_t shardMask_;
    std::vector<Shard> shards_;
};

uint64_t dedupKey(const Read& r1, const Read* r2);

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------
class Worker {
public:
    explicit Worker(const Options& opt);

    // Transforms `chunk` in place and fills its output buffers.
    void process(Chunk& chunk);

    void setDedup(SharedDedup* d) { dedup_ = d; }

    RunStats stats;

private:
    void processPair(Read& r1, Read& r2, Chunk& chunk, uint64_t readIndex);
    void processSingle(Read& r1, Chunk& chunk, uint64_t readIndex);

    // Shared stages. `isRead2` selects the read-2 variants of the options.
    void applyUmi(Read& r1, Read* r2);
    void applyFixedTrim(Read& r, bool isRead2);
    size_t applyPolyG(Read& r);
    size_t applyPolyX(Read& r);
    // Returns false when fastp would discard the read outright.
    bool applyQualityCut(Read& r);
    size_t applyAdapterBySequence(Read& r, bool isRead2);
    void applyExtraAdapters(Read& r, bool isRead2);
    FilterResult filterRead(const Read& r);
    bool indexFiltered(const std::string& name) const;

    // PE-only stages.
    void trimByOverlap(Read& r1, Read& r2, const OverlapResult& ov);
    void correctByOverlap(Read& r1, Read& r2, const OverlapResult& ov);
    bool mergePair(const Read& r1, const Read& r2, const OverlapResult& ov, Read& out);

    void recordAdapter(const std::string& seq, bool isRead2, size_t trimPos);
    void emit(std::string& dest, const Read& r);

    const Options& opt_;
    bool rebasePhred64_;
    int qualOffset_;
    std::string scratch_;
    SharedDedup* dedup_ = nullptr;
};

// ---------------------------------------------------------------------------
// Auto-detection performed once at startup, before the worker pool starts.
// ---------------------------------------------------------------------------
struct DetectionResult {
    std::string adapter1;
    std::string adapter2;
    bool adapter1Detected = false;
    bool adapter2Detected = false;
    bool polyGRecommended = false;   // NextSeq/NovaSeq two-colour chemistry
    double polyGTailFraction = 0;    // observed, reported but not acted on
    bool phred64Detected = false;
    std::string instrument;
    std::string platform;
    int readLength1 = 0;
    int readLength2 = 0;
    long long estimatedReads = 0;
    bool tileHeadersUsable = false;

    // How the startup sample was drawn, for the reports and --verbose.
    std::string sampleStrategy = "head";
    size_t sampleReads1 = 0;
    size_t sampleReads2 = 0;
    long long sampleScanned = 0;
    bool sampleExactCount = false;
    double sampleSeconds = 0;
    double sampleMeanQuality = 0;
    double sampleGcFraction = 0;

    // Retained only when --check-contamination needs them; cleared afterwards.
    std::vector<Read> sample1, sample2;
};

// Samples the input files to infer adapters, platform and layout. The sample
// spans the whole file unless --sample-head is set or the input is a stream.
DetectionResult detectInputProperties(const Options& opt);

// Known adapters, tried before de novo detection because they cover almost all
// real data and are far more reliable than inference from a sample.
struct KnownAdapter {
    const char* name;
    const char* seq;
};
const std::vector<KnownAdapter>& knownAdapters();

// De novo 3' adapter inference from sampled reads.
std::string detectAdapterDeNovo(const std::vector<Read>& reads);

}  // namespace fp
