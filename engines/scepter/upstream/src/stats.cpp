#include "stats.h"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <utility>

#include "util.h"

namespace fp {

namespace {

// ---------------------------------------------------------------------------
// Small shared helpers
// ---------------------------------------------------------------------------

// ACGT -> 0..3, everything else (including N) -> 4.
struct BaseTable {
    uint8_t v[256];
    constexpr BaseTable() : v() {
        for (int i = 0; i < 256; ++i) v[i] = 4;
        v[static_cast<int>('A')] = 0; v[static_cast<int>('a')] = 0;
        v[static_cast<int>('C')] = 1; v[static_cast<int>('c')] = 1;
        v[static_cast<int>('G')] = 2; v[static_cast<int>('g')] = 2;
        v[static_cast<int>('T')] = 3; v[static_cast<int>('t')] = 3;
        v[static_cast<int>('U')] = 3; v[static_cast<int>('u')] = 3;
    }
};
constexpr BaseTable kBaseTable{};

inline uint8_t baseCode(char c) {
    return kBaseTable.v[static_cast<unsigned char>(c)];
}

// splitmix64 finalizer: a bijection with good avalanche, so a hash value can
// stand in for the key it was derived from without introducing collisions.
inline uint64_t mix64(uint64_t x) {
    x += 0x9E3779B97F4A7C15ull;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBull;
    return x ^ (x >> 31);
}

inline void addVec(std::vector<uint64_t>& dst, const std::vector<uint64_t>& src) {
    if (dst.size() < src.size()) dst.resize(src.size(), 0);
    for (size_t i = 0; i < src.size(); ++i) dst[i] += src[i];
}

inline void growVec(std::vector<uint64_t>& v, size_t n) {
    if (v.size() < n) v.resize(n, 0);
}

inline int clampQual(int q) {
    if (q < 0) return 0;
    if (q >= kQualBins) return kQualBins - 1;
    return q;
}

inline int clampPercent(double p) {
    long v = std::lround(p);
    if (v < 0) return 0;
    if (v > 100) return 100;
    return static_cast<int>(v);
}

bool parsePositiveInt(const std::string& s, int& out) {
    if (s.empty() || s.size() > 9) return false;
    int v = 0;
    for (char c : s) {
        if (c < '0' || c > '9') return false;
        v = v * 10 + (c - '0');
    }
    out = v;
    return true;
}

// Read lengths are unbounded (long reads), so the length histogram gets its own
// much larger cap than the per-cycle statistics.
constexpr size_t kMaxLenHist = 1u << 20;
constexpr uint32_t kStatKmerCount = 1u << (2 * kStatKmerK);
constexpr uint32_t kStatKmerMask = kStatKmerCount - 1;

}  // namespace

// ---------------------------------------------------------------------------
// ReadSetStats
// ---------------------------------------------------------------------------

ReadSetStats::ReadSetStats(int qualOffsetIn, bool positionalHistIn)
    : positionalHist(positionalHistIn), qualOffset(qualOffsetIn) {
    kmerProfile.assign(kStatKmerCount, 0);
}

void ReadSetStats::grow(size_t n) {
    if (n > static_cast<size_t>(kMaxCycles)) n = kMaxCycles;
    if (cycleDepth.size() >= n) return;
    for (auto& v : baseCount) v.resize(n, 0);
    for (auto& v : cycleQualSumByBase) v.resize(n, 0);
    cycleDepth.resize(n, 0);
    cycleQualSum.resize(n, 0);
    cycleQ20.resize(n, 0);
    cycleQ30.resize(n, 0);
    if (positionalHist) cycleQualHist.resize(n, std::array<uint32_t, kQualBins>{});
}

void ReadSetStats::add(const Read& r) {
    const size_t len = r.seq.size();
    const size_t qlen = std::min(len, r.qual.size());

    if (reads == 0) {
        minLength = len;
        maxLength = len;
    } else {
        if (len < minLength) minLength = len;
        if (len > maxLength) maxLength = len;
    }
    reads++;
    bases += len;

    const size_t li = std::min(len, kMaxLenHist);
    if (lengthHist.size() <= li) lengthHist.resize(li + 1, 0);
    lengthHist[li]++;

    if (len == 0) return;

    const size_t n = std::min(len, static_cast<size_t>(kMaxCycles));
    if (cycleDepth.size() < n) grow(n);
    if (n > cycles) cycles = n;

    uint64_t gc = 0;
    uint64_t qsum = 0;
    uint64_t transitions = 0;
    uint32_t kmer = 0;
    int kmerRun = 0;

    for (size_t i = 0; i < len; ++i) {
        const uint8_t b = baseCode(r.seq[i]);
        int q = 0;
        if (i < qlen) {
            q = clampQual(static_cast<int>(static_cast<unsigned char>(r.qual[i])) - qualOffset);
        }

        qsum += static_cast<uint64_t>(q);
        if (b == 1 || b == 2) gc++;
        else if (b == 4) nBases++;
        if (q >= 20) {
            q20Bases++;
            if (q >= 30) {
                q30Bases++;
                if (q >= 40) q40Bases++;
            }
        }
        if (i > 0 && r.seq[i] != r.seq[i - 1]) transitions++;

        if (i < n) {
            baseCount[b][i]++;
            cycleDepth[i]++;
            cycleQualSum[i] += static_cast<uint64_t>(q);
            cycleQualSumByBase[b][i] += static_cast<uint64_t>(q);
            if (q >= 20) {
                cycleQ20[i]++;
                if (q >= 30) cycleQ30[i]++;
            }
            if (positionalHist) cycleQualHist[i][q]++;
        }

        if (b < 4) {
            kmer = ((kmer << 2) | b) & kStatKmerMask;
            if (++kmerRun >= kStatKmerK) kmerProfile[kmer]++;
        } else {
            kmerRun = 0;
            kmer = 0;
        }
    }

    gcBases += gc;
    qualSumTotal += qsum;

    gcHist[clampPercent(100.0 * static_cast<double>(gc) / static_cast<double>(len))]++;
    meanQualHist[clampQual(static_cast<int>(
        std::lround(static_cast<double>(qsum) / static_cast<double>(len))))]++;
    if (len >= 2) {
        complexityHist[clampPercent(100.0 * static_cast<double>(transitions) /
                                    static_cast<double>(len - 1))]++;
    }
}

void ReadSetStats::merge(const ReadSetStats& other) {
    if (other.reads == 0) return;

    if (reads == 0) {
        minLength = other.minLength;
        maxLength = other.maxLength;
    } else {
        minLength = std::min(minLength, other.minLength);
        maxLength = std::max(maxLength, other.maxLength);
    }

    reads += other.reads;
    bases += other.bases;
    q20Bases += other.q20Bases;
    q30Bases += other.q30Bases;
    q40Bases += other.q40Bases;
    nBases += other.nBases;
    gcBases += other.gcBases;
    qualSumTotal += other.qualSumTotal;

    if (other.cycles > cycles) cycles = other.cycles;
    if (cycleDepth.size() < other.cycleDepth.size()) grow(other.cycleDepth.size());

    for (int b = 0; b < 5; ++b) {
        addVec(baseCount[b], other.baseCount[b]);
        addVec(cycleQualSumByBase[b], other.cycleQualSumByBase[b]);
    }
    addVec(cycleDepth, other.cycleDepth);
    addVec(cycleQualSum, other.cycleQualSum);
    addVec(cycleQ20, other.cycleQ20);
    addVec(cycleQ30, other.cycleQ30);

    if (positionalHist && !other.cycleQualHist.empty()) {
        if (cycleQualHist.size() < other.cycleQualHist.size()) {
            cycleQualHist.resize(other.cycleQualHist.size(), std::array<uint32_t, kQualBins>{});
        }
        for (size_t c = 0; c < other.cycleQualHist.size(); ++c) {
            for (int q = 0; q < kQualBins; ++q) cycleQualHist[c][q] += other.cycleQualHist[c][q];
        }
    }

    if (lengthHist.size() < other.lengthHist.size()) lengthHist.resize(other.lengthHist.size(), 0);
    for (size_t i = 0; i < other.lengthHist.size(); ++i) lengthHist[i] += other.lengthHist[i];

    for (size_t i = 0; i < gcHist.size(); ++i) gcHist[i] += other.gcHist[i];
    for (size_t i = 0; i < meanQualHist.size(); ++i) meanQualHist[i] += other.meanQualHist[i];
    for (size_t i = 0; i < complexityHist.size(); ++i) complexityHist[i] += other.complexityHist[i];

    if (kmerProfile.size() < other.kmerProfile.size()) kmerProfile.resize(other.kmerProfile.size(), 0);
    for (size_t i = 0; i < other.kmerProfile.size(); ++i) kmerProfile[i] += other.kmerProfile[i];
}

void ReadSetStats::finalize() {
    gcPercent = bases ? 100.0 * static_cast<double>(gcBases) / static_cast<double>(bases) : 0.0;
    q20Percent = bases ? 100.0 * static_cast<double>(q20Bases) / static_cast<double>(bases) : 0.0;
    q30Percent = bases ? 100.0 * static_cast<double>(q30Bases) / static_cast<double>(bases) : 0.0;
    meanLength = reads ? static_cast<double>(bases) / static_cast<double>(reads) : 0.0;
    meanQuality = bases ? static_cast<double>(qualSumTotal) / static_cast<double>(bases) : 0.0;

    // Heuristic redundancy signal: total-variation distance between the observed
    // 5-mer profile and the profile expected from the base composition alone.
    // Highly duplicated or low-complexity libraries deviate strongly. The
    // authoritative figure comes from DuplicationAnalyzer.
    duplicationHint = 0.0;
    uint64_t kmerTotal = 0;
    for (uint64_t v : kmerProfile) kmerTotal += v;
    uint64_t acgt[4] = {0, 0, 0, 0};
    for (int b = 0; b < 4; ++b) {
        for (uint64_t v : baseCount[b]) acgt[b] += v;
    }
    const uint64_t acgtTotal = acgt[0] + acgt[1] + acgt[2] + acgt[3];
    if (kmerTotal > 0 && acgtTotal > 0 && kmerProfile.size() == kStatKmerCount) {
        double p[4];
        for (int b = 0; b < 4; ++b) {
            p[b] = static_cast<double>(acgt[b]) / static_cast<double>(acgtTotal);
        }
        double tv = 0.0;
        for (uint32_t i = 0; i < kStatKmerCount; ++i) {
            double expected = 1.0;
            for (int pos = 0; pos < kStatKmerK; ++pos) {
                expected *= p[(i >> (2 * pos)) & 3u];
            }
            const double observed = static_cast<double>(kmerProfile[i]) /
                                    static_cast<double>(kmerTotal);
            tv += std::fabs(observed - expected);
        }
        duplicationHint = 0.5 * tv;
    }
}

double ReadSetStats::cycleMeanQuality(size_t c) const {
    if (c >= cycleDepth.size() || cycleDepth[c] == 0) return 0.0;
    return static_cast<double>(cycleQualSum[c]) / static_cast<double>(cycleDepth[c]);
}

double ReadSetStats::cycleMeanQualityForBase(size_t cycle, int base) const {
    if (base < 0 || base > 4) return 0.0;
    const size_t b = static_cast<size_t>(base);
    if (cycle >= cycleQualSumByBase[b].size() || cycle >= baseCount[b].size()) return 0.0;
    const uint64_t n = baseCount[b][cycle];
    if (n == 0) return 0.0;
    return static_cast<double>(cycleQualSumByBase[b][cycle]) / static_cast<double>(n);
}

double ReadSetStats::cycleBaseFraction(size_t c, int base) const {
    if (base < 0 || base > 4) return 0.0;
    if (c >= cycleDepth.size() || cycleDepth[c] == 0) return 0.0;
    if (c >= baseCount[base].size()) return 0.0;
    return static_cast<double>(baseCount[base][c]) / static_cast<double>(cycleDepth[c]);
}

double ReadSetStats::cycleGcFraction(size_t c) const {
    if (c >= cycleDepth.size() || cycleDepth[c] == 0) return 0.0;
    if (c >= baseCount[1].size() || c >= baseCount[2].size()) return 0.0;
    const uint64_t gc = baseCount[1][c] + baseCount[2][c];
    return static_cast<double>(gc) / static_cast<double>(cycleDepth[c]);
}

double ReadSetStats::cycleQ20Fraction(size_t c) const {
    if (c >= cycleDepth.size() || cycleDepth[c] == 0) return 0.0;
    return static_cast<double>(cycleQ20[c]) / static_cast<double>(cycleDepth[c]);
}

int ReadSetStats::cycleQualQuantile(size_t c, double q) const {
    if (!positionalHist || c >= cycleQualHist.size()) return -1;
    const std::array<uint32_t, kQualBins>& h = cycleQualHist[c];

    uint64_t total = 0;
    for (int b = 0; b < kQualBins; ++b) total += h[b];
    if (total == 0) return -1;

    if (q < 0.0) q = 0.0;
    if (q > 1.0) q = 1.0;

    // Linear interpolation between the two order statistics that bracket the
    // requested rank (the numpy/R type-7 definition).
    const double pos = q * static_cast<double>(total - 1);
    const uint64_t lo = static_cast<uint64_t>(std::floor(pos));
    const uint64_t hi = static_cast<uint64_t>(std::ceil(pos));

    int vlo = -1, vhi = -1;
    uint64_t cum = 0;
    for (int b = 0; b < kQualBins; ++b) {
        cum += h[b];
        if (vlo < 0 && cum > lo) vlo = b;
        if (cum > hi) { vhi = b; break; }
    }
    if (vlo < 0) vlo = kQualBins - 1;
    if (vhi < 0) vhi = vlo;

    const double frac = pos - static_cast<double>(lo);
    return static_cast<int>(std::lround(vlo + frac * (vhi - vlo)));
}

// ---------------------------------------------------------------------------
// TileAnalyzer
// ---------------------------------------------------------------------------

bool TileAnalyzer::parseTile(const std::string& header, std::string& lane, int& tile) {
    const size_t space = header.find_first_of(" \t");
    const std::string head = (space == std::string::npos) ? header : header.substr(0, space);
    if (head.empty()) return false;

    const std::vector<std::string> f = util::split(head, ':');
    // Casava >=1.8: instrument:run:flowcell:lane:tile:x:y
    if (f.size() >= 7) {
        if (!parsePositiveInt(f[4], tile)) return false;
        lane = f[3];
        return true;
    }
    // Casava 1.4: machine:lane:tile:x:y
    if (f.size() == 5) {
        if (!parsePositiveInt(f[2], tile)) return false;
        lane = f[1];
        return true;
    }
    return false;
}

void TileAnalyzer::add(const std::string& header, const Read& r, int qualOffset) {
    if (!usable_) return;

    std::string lane;
    int tile = 0;
    if (!parseTile(header, lane, tile)) {
        // One unparsable header disables per-tile analysis for the whole run:
        // partial coverage would produce misleading outlier calls.
        usable_ = false;
        tiles_.clear();
        return;
    }
    checked_ = true;

    TileStats& t = tiles_[lane + ":" + std::to_string(tile)];
    t.reads++;

    const size_t n = std::min(std::min(r.seq.size(), r.qual.size()),
                              static_cast<size_t>(kMaxCycles));
    if (n == 0) return;
    growVec(t.qualSum, n);
    growVec(t.depth, n);
    for (size_t i = 0; i < n; ++i) {
        const int q = clampQual(static_cast<int>(static_cast<unsigned char>(r.qual[i])) - qualOffset);
        t.qualSum[i] += static_cast<uint64_t>(q);
        t.depth[i]++;
    }
    if (n > cycles) cycles = n;
}

void TileAnalyzer::addError(const std::string& header, uint64_t errors, uint64_t opportunities) {
    if (!usable_) return;
    std::string lane;
    int tile = 0;
    if (!parseTile(header, lane, tile)) {
        usable_ = false;
        tiles_.clear();
        return;
    }
    checked_ = true;
    TileStats& t = tiles_[lane + ":" + std::to_string(tile)];
    t.errors += errors;
    t.errorOpportunities += opportunities;
}

void TileAnalyzer::merge(const TileAnalyzer& other) {
    if (!other.usable_) {
        usable_ = false;
        tiles_.clear();
        return;
    }
    if (!usable_) return;

    checked_ = checked_ || other.checked_;
    if (other.cycles > cycles) cycles = other.cycles;

    for (const auto& kv : other.tiles_) {
        TileStats& t = tiles_[kv.first];
        addVec(t.qualSum, kv.second.qualSum);
        addVec(t.depth, kv.second.depth);
        t.reads += kv.second.reads;
        t.errors += kv.second.errors;
        t.errorOpportunities += kv.second.errorOpportunities;
    }
}

void TileAnalyzer::finalize() {
    outlierTiles.clear();
    globalMeanQuality = 0.0;
    if (!usable_ || !checked_ || tiles_.empty()) return;

    uint64_t sum = 0, depth = 0;
    for (const auto& kv : tiles_) {
        for (uint64_t v : kv.second.qualSum) sum += v;
        for (uint64_t v : kv.second.depth) depth += v;
    }
    if (depth == 0) return;
    globalMeanQuality = static_cast<double>(sum) / static_cast<double>(depth);

    for (const auto& kv : tiles_) {
        uint64_t ts = 0, td = 0;
        for (uint64_t v : kv.second.qualSum) ts += v;
        for (uint64_t v : kv.second.depth) td += v;
        if (td == 0) continue;
        const double mean = static_cast<double>(ts) / static_cast<double>(td);
        if (std::fabs(mean - globalMeanQuality) > 2.0) outlierTiles.push_back(kv.first);
    }
}

// ---------------------------------------------------------------------------
// ErrorModel
// ---------------------------------------------------------------------------

ErrorModel::ErrorModel() = default;

void ErrorModel::observe(const Read& r1, const Read& r2, const OverlapResult& ov, int qualOffset) {
    if (!ov.overlapped || ov.overlapLen <= 0) return;

    // A pair is only usable as a truth set when the two mates essentially agree;
    // beyond that, the disagreements are more likely real variation or a bad
    // alignment than sequencing error.
    if (ov.diff > 5) return;
    if (static_cast<double>(ov.diff) > 0.20 * static_cast<double>(ov.overlapLen)) return;

    const size_t len1 = std::min(r1.seq.size(), r1.qual.size());
    const size_t len2 = std::min(r2.seq.size(), r2.qual.size());
    if (len1 == 0 || len2 == 0) return;

    // offset >= 0: the reverse complement of R2 starts `offset` bases into R1.
    // offset  < 0: adapter read-through, R1 starts `-offset` bases into rc(R2).
    const size_t start1 = ov.offset >= 0 ? static_cast<size_t>(ov.offset) : 0;
    const size_t startRc = ov.offset >= 0 ? 0 : static_cast<size_t>(-ov.offset);

    size_t compared = 0;
    for (int i = 0; i < ov.overlapLen; ++i) {
        const size_t p1 = start1 + static_cast<size_t>(i);
        const size_t prc = startRc + static_cast<size_t>(i);
        if (p1 >= len1 || prc >= len2) break;
        const size_t p2 = len2 - 1 - prc;

        const uint8_t b1 = baseCode(r1.seq[p1]);
        const uint8_t b2 = baseCode(r2.seq[p2]);
        if (b1 > 3 || b2 > 3) continue;
        const uint8_t b2fwd = static_cast<uint8_t>(3 - b2);   // R2 base in R1 orientation

        const int q1 = clampQual(static_cast<int>(static_cast<unsigned char>(r1.qual[p1])) - qualOffset);
        const int q2 = clampQual(static_cast<int>(static_cast<unsigned char>(r2.qual[p2])) - qualOffset);

        qTotal[q1]++;
        qTotal[q2]++;
        growVec(cycleTotal1, p1 + 1);
        growVec(cycleErrors1, p1 + 1);
        growVec(cycleTotal2, p2 + 1);
        growVec(cycleErrors2, p2 + 1);
        cycleTotal1[p1]++;
        cycleTotal2[p2]++;
        compared++;

        if (b1 == b2fwd) continue;

        mismatches++;
        // The lower-quality base is taken to be the erroneous one; the other is
        // treated as the truth for the substitution matrix. On a tie there is
        // no evidence either way, so splitting the blame is the only unbiased
        // choice -- always charging read 1 left read 2's per-cycle error rate
        // identically zero, and ties are the common case on binned-quality
        // NovaSeq data where only a handful of distinct values exist.
        if (q1 < q2 || (q1 == q2 && ((p1 ^ p2) & 1) == 0)) {
            qErrors[q1]++;
            cycleErrors1[p1]++;
            substitutions[b2fwd][b1]++;
        } else {
            qErrors[q2]++;
            cycleErrors2[p2]++;
            substitutions[b1][b2fwd]++;
        }
    }

    if (compared > 0) {
        pairsExamined++;
        basesCompared += compared;
    }
}

void ErrorModel::merge(const ErrorModel& other) {
    for (int q = 0; q < kQualBins; ++q) {
        qErrors[q] += other.qErrors[q];
        qTotal[q] += other.qTotal[q];
    }
    addVec(cycleErrors1, other.cycleErrors1);
    addVec(cycleTotal1, other.cycleTotal1);
    addVec(cycleErrors2, other.cycleErrors2);
    addVec(cycleTotal2, other.cycleTotal2);
    for (int a = 0; a < 4; ++a) {
        for (int b = 0; b < 4; ++b) substitutions[a][b] += other.substitutions[a][b];
    }
    pairsExamined += other.pairsExamined;
    basesCompared += other.basesCompared;
    mismatches += other.mismatches;
}

void ErrorModel::finalize() {
    calibration.clear();
    overallErrorRate = 0.0;
    overallQ = 0.0;
    calibrationBias = 0.0;

    if (basesCompared == 0) return;
    // Every compared position contributes two base calls, one from each mate,
    // and a mismatch means one of the two is wrong. The denominator is
    // therefore twice the number of positions -- which is what qTotal already
    // counts, so dividing by the positions alone made this headline figure
    // exactly double the per-quality calibration table beside it.
    overallErrorRate =
        static_cast<double>(mismatches) / (2.0 * static_cast<double>(basesCompared));
    overallQ = overallErrorRate > 0.0 ? -10.0 * std::log10(overallErrorRate) : 60.0;

    double biasSum = 0.0;
    double biasWeight = 0.0;
    for (int q = 0; q < kQualBins; ++q) {
        if (qTotal[q] < 1000) continue;   // below this the observed rate is noise
        double observed;
        if (qErrors[q] == 0) {
            observed = 60.0;
        } else {
            const double rate = static_cast<double>(qErrors[q]) / static_cast<double>(qTotal[q]);
            observed = -10.0 * std::log10(rate);
            if (observed > 60.0) observed = 60.0;
        }
        calibration.emplace_back(q, observed);
        biasSum += (observed - q) * static_cast<double>(qTotal[q]);
        biasWeight += static_cast<double>(qTotal[q]);
    }
    if (biasWeight > 0.0) calibrationBias = biasSum / biasWeight;
}

// ---------------------------------------------------------------------------
// KmerSpectrum
// ---------------------------------------------------------------------------

KmerSpectrum::KmerSpectrum(int k, uint64_t scale, int samplingFactorIn)
    : samplingFactor(samplingFactorIn < 1 ? 1 : samplingFactorIn), k_(k), scale_(scale) {
    // 2 bits per base must fit in 64, and a rolling reverse complement needs
    // k >= 2 to be meaningful.
    if (k_ < 2) k_ = 2;
    if (k_ > 31) k_ = 31;
    if (scale_ < 1) scale_ = 1;
    threshold_ = UINT64_MAX / scale_;
}

void KmerSpectrum::add(const Read& r) {
    const size_t len = r.seq.size();
    if (len > readLength) readLength = len;
    if (len < static_cast<size_t>(k_)) return;

    const uint64_t mask = (1ull << (2 * k_)) - 1;
    const int topShift = 2 * (k_ - 1);
    uint64_t fwd = 0, rev = 0;
    int run = 0;

    for (size_t i = 0; i < len; ++i) {
        const uint8_t b = baseCode(r.seq[i]);
        if (b > 3) {   // an N breaks the window; nothing spanning it is countable
            run = 0;
            fwd = 0;
            rev = 0;
            continue;
        }
        fwd = ((fwd << 2) | b) & mask;
        rev = (rev >> 2) | (static_cast<uint64_t>(3 - b) << topShift);
        if (++run < k_) continue;

        totalKmers++;
        const uint64_t canonical = fwd < rev ? fwd : rev;
        const uint64_t h = mix64(canonical);
        if (h >= threshold_) continue;

        uint32_t& c = counts_[h];
        if (c < UINT32_MAX) c++;
        if (counts_.size() > kMaxEntries) maybeDownscale();
    }
}

void KmerSpectrum::maybeDownscale() {
    scale_ *= 2;
    threshold_ = UINT64_MAX / scale_;
    for (auto it = counts_.begin(); it != counts_.end();) {
        if (it->first >= threshold_) it = counts_.erase(it);
        else ++it;
    }
}

void KmerSpectrum::merge(const KmerSpectrum& other) {
    // The aggregate sketch is default-constructed before the workers' k is
    // known, so an empty destination adopts the incoming parameters instead of
    // rejecting every count as incompatible.
    if (counts_.empty() && totalKmers == 0) {
        k_ = other.k_;
        scale_ = other.scale_;
        threshold_ = other.threshold_;
    }
    if (other.samplingFactor > samplingFactor) samplingFactor = other.samplingFactor;
    if (other.readLength > readLength) readLength = other.readLength;

    totalKmers += other.totalKmers;
    if (other.k_ != k_) return;   // incompatible sketches, keep counts as-is

    if (other.scale_ > scale_) {
        scale_ = other.scale_;
        threshold_ = UINT64_MAX / scale_;
        for (auto it = counts_.begin(); it != counts_.end();) {
            if (it->first >= threshold_) it = counts_.erase(it);
            else ++it;
        }
    }
    for (const auto& kv : other.counts_) {
        if (kv.first >= threshold_) continue;
        uint32_t& c = counts_[kv.first];
        const uint64_t sum = static_cast<uint64_t>(c) + kv.second;
        c = sum > UINT32_MAX ? UINT32_MAX : static_cast<uint32_t>(sum);
    }
    while (counts_.size() > kMaxEntries) maybeDownscale();
}

void KmerSpectrum::finalize() {
    constexpr size_t kMaxCount = 10000;
    distinctSketchKmers = counts_.size();
    histogram.assign(kMaxCount + 1, 0);
    for (const auto& kv : counts_) {
        histogram[std::min<size_t>(kv.second, kMaxCount)]++;
    }

    // The peak search starts at 3: counts of 1-2 are dominated by k-mers created
    // by sequencing errors, whose shoulder is usually taller than the true
    // coverage peak and would otherwise win the argmax.
    uint64_t bestMass = 0;
    peakDepth = 0;
    for (size_t c = 3; c <= kMaxCount; ++c) {
        const uint64_t mass = histogram[c] * static_cast<uint64_t>(c);
        if (mass > bestMass) {
            bestMass = mass;
            peakDepth = static_cast<int>(c);
        }
    }

    // The histogram is integer-binned, so the mode is only located to the
    // nearest whole depth. Since genome size divides by it, that rounding alone
    // moves the answer by up to 1/peak -- several percent at the depths bacterial
    // isolates are usually sequenced to, and it shows up as an error that swings
    // with depth rather than shrinking. Fitting a parabola through the mass
    // curve at the winning bin and its two neighbours recovers the sub-bin
    // position of the true maximum. Measured against simulated Poisson and
    // negative-binomial spectra with and without repeat families, this halves
    // the worst-case error (-6.3% -> -3.3%) and cuts RMS error by ~39%.
    //
    // Note the peak of c*h(c) sits near lambda+1 rather than lambda, so a small
    // negative bias remains. Subtracting the offset was tried and rejected: it
    // overcorrects once coverage is overdispersed or repeats are present, which
    // is the normal case, and it flips the error positive without shrinking it.
    double peakRefined = static_cast<double>(peakDepth);
    if (peakDepth > 3 && static_cast<size_t>(peakDepth) < kMaxCount) {
        const size_t p = static_cast<size_t>(peakDepth);
        const double lo = static_cast<double>(histogram[p - 1]) * static_cast<double>(p - 1);
        const double mid = static_cast<double>(histogram[p]) * static_cast<double>(p);
        const double hi = static_cast<double>(histogram[p + 1]) * static_cast<double>(p + 1);
        const double denom = lo - 2.0 * mid + hi;
        // Only trust the fit when the three points really do bracket a maximum.
        if (denom < 0.0) {
            const double shift = 0.5 * (lo - hi) / denom;
            if (shift > -1.0 && shift < 1.0) peakRefined += shift;
        }
    }

    // Integrating from a fixed floor of 3 either keeps error k-mers (deep data,
    // where the error shoulder reaches well past 3) or discards real genomic
    // ones (shallow or uneven data, where the coverage peak sits low). The
    // minimum between the error shoulder and the coverage peak is where the two
    // components actually cross, so start there.
    size_t valley = 1;
    uint64_t lowest = ~static_cast<uint64_t>(0);
    for (size_t c = 1; c < static_cast<size_t>(peakDepth); ++c) {
        if (histogram[c] < lowest) { lowest = histogram[c]; valley = c; }
    }
    if (valley < 1) valley = 1;
    // Never eat into the peak itself; half the peak depth is already extreme.
    if (peakDepth > 0 && valley > static_cast<size_t>(peakDepth) / 2 + 1) {
        valley = static_cast<size_t>(peakDepth) / 2 + 1;
    }
    massFrom = valley;

    double totalMass = 0.0;
    double repeatMass = 0.0;
    const size_t repeatFrom = peakDepth > 0 ? static_cast<size_t>(2 * peakDepth) + 1 : kMaxCount + 1;
    for (size_t c = valley; c <= kMaxCount; ++c) {
        const double mass = static_cast<double>(histogram[c]) * static_cast<double>(c);
        totalMass += mass;
        if (c >= repeatFrom) repeatMass += mass;
    }

    // peakDepth is the k-mer depth of a 1-in-samplingFactor subsample. Scaling
    // by L/(L-k+1) converts k-mer depth to base depth; genome size needs no
    // such correction because both numerator and denominator carry it.
    estimatedDepth = peakRefined * static_cast<double>(samplingFactor);
    if (readLength > static_cast<size_t>(k_)) {
        estimatedDepth *= static_cast<double>(readLength) /
                          static_cast<double>(readLength - static_cast<size_t>(k_) + 1);
    }
    estimatedGenomeSize =
        peakRefined > 0.0 ? totalMass / peakRefined * static_cast<double>(scale_) : 0.0;

    // Dividing the whole mass by the single-copy peak counts every copy of
    // everything, so a 20-copy plasmid enters as 20 genome's-worth of sequence.
    // That is the right answer for total DNA per cell and the wrong one for the
    // number users actually compare against -- a reference assembly, which
    // lists that plasmid once. The two differ by a factor of two or more on
    // real isolates, so report both instead of silently choosing: the core
    // figure integrates only up to twice the peak, which keeps single-copy
    // sequence and drops high-copy elements.
    double coreMass = 0.0;
    for (size_t c = valley; c < repeatFrom && c <= kMaxCount; ++c) {
        coreMass += static_cast<double>(histogram[c]) * static_cast<double>(c);
    }
    estimatedGenomeSizeCore =
        peakRefined > 0.0 ? coreMass / peakRefined * static_cast<double>(scale_) : 0.0;
    errorKmerFraction = distinctSketchKmers > 0
                            ? static_cast<double>(histogram[1] + histogram[2]) /
                                  static_cast<double>(distinctSketchKmers)
                            : 0.0;
    repeatFraction = totalMass > 0.0 ? repeatMass / totalMass : 0.0;
    // A spectrum dominated by multi-copy mass cannot yield a trustworthy
    // single-copy genome size: the two estimates above diverge, and which one
    // is meaningful depends on biology the spectrum cannot see.
    reliable = distinctSketchKmers > 5000 && peakDepth >= 5 && repeatFraction < 0.15;
}

// ---------------------------------------------------------------------------
// DuplicationAnalyzer
// ---------------------------------------------------------------------------

namespace {
// Table size per accuracy level, in bits.
constexpr size_t kDupBits[6] = {20, 22, 24, 26, 27, 28};

uint64_t packPrefix(const std::string& s) {
    const size_t n = std::min<size_t>(32, s.size());
    uint64_t v = 0;
    for (size_t i = 0; i < n; ++i) {
        const uint8_t b = baseCode(s[i]);
        v = (v << 2) | (b & 3u);
    }
    // Fold the length in so short reads do not alias with zero-padded longer ones.
    return v ^ (static_cast<uint64_t>(n) << 60);
}
}  // namespace

DuplicationAnalyzer::DuplicationAnalyzer(int level, bool on) : enabled(on) {
    if (level < 1) level = 1;
    if (level > 6) level = 6;
    bits_ = kDupBits[level - 1];
    if (enabled) {
        table_.assign(static_cast<size_t>(1) << bits_, 0);
        keys_.assign(static_cast<size_t>(1) << bits_, 0);
    }
}

uint64_t DuplicationAnalyzer::keyFor(const Read& r1, const Read* r2) const {
    uint64_t key = packPrefix(r1.seq);
    if (r2 && !r2->seq.empty()) key ^= mix64(packPrefix(r2->seq));
    return mix64(key);
}

bool DuplicationAnalyzer::observe(const Read& r1, const Read* r2) {
    if (!enabled || table_.empty()) return false;
    totalObserved++;

    const uint64_t key = keyFor(r1, r2);
    const size_t idx = static_cast<size_t>(key >> (64 - bits_));

    if (table_[idx] != 0 && keys_[idx] == key) {
        if (table_[idx] < UINT32_MAX) table_[idx]++;
        return true;
    }
    // A bucket holding a different key is overwritten and counted as a first
    // sighting: the standard bounded-memory approximation.
    keys_[idx] = key;
    table_[idx] = 1;
    uniqueObserved++;
    return false;
}

void DuplicationAnalyzer::merge(const DuplicationAnalyzer& other) {
    totalObserved += other.totalObserved;
    uniqueObserved += other.uniqueObserved;
    if (!other.enabled) return;
    // Adopt the workers' table geometry. The aggregate is default-constructed
    // at accuracy level 1, but --dedup raises the workers to level 3, so the
    // size check below rejected every merge and the reported rate fell with
    // every extra thread -- 19.75% at one thread, 8.34% at four, for the same
    // data. KmerSpectrum::merge adopts its parameters the same way.
    if (bits_ != other.bits_ || table_.size() != other.table_.size() || !enabled) {
        // The aggregate never observes anything itself, so before the first
        // merge its table is allocated but empty of content -- checking for an
        // *unallocated* table was not enough, and the geometry was never
        // adopted. Take the first worker's shape wholesale; later workers then
        // match and merge normally, including the cross-worker uniqueObserved
        // correction that makes the rate independent of thread count.
        if (!mergedAny_) {
            enabled = true;
            bits_ = other.bits_;
            table_ = other.table_;
            keys_ = other.keys_;
            mergedAny_ = true;
            return;
        }
        return;   // genuinely incompatible geometries: keep what we have
    }
    mergedAny_ = true;

    for (size_t i = 0; i < table_.size(); ++i) {
        const uint32_t oc = other.table_[i];
        if (oc == 0) continue;
        if (table_[i] == 0) {
            table_[i] = oc;
            keys_[i] = other.keys_[i];
        } else if (keys_[i] == other.keys_[i]) {
            const uint64_t sum = static_cast<uint64_t>(table_[i]) + oc;
            table_[i] = sum > UINT32_MAX ? UINT32_MAX : static_cast<uint32_t>(sum);
            // The other worker's first sighting was in fact a duplicate of ours.
            if (uniqueObserved > 0) uniqueObserved--;
        } else if (oc > table_[i]) {
            table_[i] = oc;
            keys_[i] = other.keys_[i];
        }
    }
}

void DuplicationAnalyzer::finalize() {
    levels.fill(0.0);
    duplicationRate = 0.0;
    estimatedLibrarySize = 0.0;
    if (!enabled || totalObserved == 0) return;

    duplicationRate = 1.0 - static_cast<double>(uniqueObserved) / static_cast<double>(totalObserved);
    if (duplicationRate < 0.0) duplicationRate = 0.0;

    std::array<uint64_t, 16> mass{};
    mass.fill(0);
    uint64_t tableMass = 0;
    for (uint32_t c : table_) {
        if (c == 0) continue;
        size_t bin;
        if (c <= 9) bin = c - 1;
        else if (c < 50) bin = 9;
        else if (c < 100) bin = 10;
        else if (c < 500) bin = 11;
        else if (c < 1000) bin = 12;
        else if (c < 5000) bin = 13;
        else if (c < 10000) bin = 14;
        else bin = 15;
        mass[bin] += c;
        tableMass += c;
    }
    // Normalised against the read mass actually retained in the table, so the
    // columns still sum to 100% after collision overwrites.
    const double denom = tableMass > 0 ? static_cast<double>(tableMass)
                                       : static_cast<double>(totalObserved);
    if (denom > 0.0) {
        for (size_t i = 0; i < levels.size(); ++i) {
            levels[i] = 100.0 * static_cast<double>(mass[i]) / denom;
        }
    }

    // Lander-Waterman / Picard: solve C/L = 1 - exp(-N/L) for the library size L.
    const double N = static_cast<double>(totalObserved);
    const double C = static_cast<double>(uniqueObserved);
    if (C > 0.0 && C < N) {
        auto f = [&](double L) { return C / L - 1.0 + std::exp(-N / L); };
        double lo = C;                    // f(C) = exp(-N/C) > 0
        double hi = C * 2.0;
        int guard = 0;
        while (f(hi) > 0.0 && guard++ < 200) hi *= 2.0;
        for (int i = 0; i < 100; ++i) {
            const double mid = 0.5 * (lo + hi);
            if (f(mid) > 0.0) lo = mid;
            else hi = mid;
        }
        estimatedLibrarySize = 0.5 * (lo + hi);
    }
    // C >= N means no duplicates were seen at all: the library size is simply
    // not determinable from this data, and 0 signals that to the reporters.
}

const char* DuplicationAnalyzer::levelLabel(int i) {
    static const char* kLabels[16] = {"1",  "2",  "3",   "4",    "5",   "6",
                                      "7",  "8",  "9",   ">10",  ">50", ">100",
                                      ">500", ">1k", ">5k", ">10k"};
    if (i < 0 || i >= 16) return "";
    return kLabels[i];
}

// ---------------------------------------------------------------------------
// OverrepAnalyzer
// ---------------------------------------------------------------------------

namespace {
constexpr size_t kOverrepLens[4] = {10, 20, 40, 100};
constexpr size_t kOverrepMaxKeys = 1000000;
}  // namespace

OverrepAnalyzer::OverrepAnalyzer(int samplingIn, bool on)
    : enabled(on), sampling(samplingIn < 1 ? 1 : samplingIn) {}

void OverrepAnalyzer::add(const Read& r, uint64_t readIndex) {
    if (!enabled) return;
    const uint64_t step = sampling < 1 ? 1 : static_cast<uint64_t>(sampling);
    if (readIndex % step != 0) return;

    const size_t len = r.seq.size();
    if (len == 0) return;
    sampled++;
    if (len > cycles_) cycles_ = std::min(len, static_cast<size_t>(kMaxCycles));

    // Prefix count of non-ACGT bases, so substrings containing an N are skipped
    // in O(1) instead of being rescanned.
    std::vector<uint32_t> nPrefix(len + 1, 0);
    for (size_t i = 0; i < len; ++i) {
        nPrefix[i + 1] = nPrefix[i] + (baseCode(r.seq[i]) > 3 ? 1u : 0u);
    }

    std::string key;
    for (size_t sl : kOverrepLens) {
        if (sl > len) continue;
        for (size_t pos = 0; pos + sl <= len; ++pos) {
            if (nPrefix[pos + sl] != nPrefix[pos]) continue;
            key.assign(r.seq, pos, sl);

            auto it = counts_.find(key);
            if (it == counts_.end()) {
                if (counts_.size() >= kOverrepMaxKeys) continue;   // memory bound
                it = counts_.emplace(key, 0).first;
            }
            it->second++;

            if (pos < static_cast<size_t>(kMaxCycles)) {
                std::vector<uint64_t>& profile = positions_[key];
                if (profile.size() <= pos) profile.resize(pos + 1, 0);
                profile[pos]++;
            }
        }
    }
}

void OverrepAnalyzer::merge(const OverrepAnalyzer& other) {
    // The aggregate this merges into is default-constructed, so it must adopt
    // the workers' configuration. Without this it stays disabled and
    // finalize() discards everything the workers collected: -p reported an
    // empty list however overrepresented the library was.
    if (other.enabled) {
        enabled = true;
        sampling = other.sampling;
    }
    sampled += other.sampled;
    if (other.cycles_ > cycles_) cycles_ = other.cycles_;
    for (const auto& kv : other.counts_) counts_[kv.first] += kv.second;
    for (const auto& kv : other.positions_) addVec(positions_[kv.first], kv.second);
}

void OverrepAnalyzer::finalize(uint64_t totalReads) {
    hits.clear();
    if (!enabled || counts_.empty()) return;

    const uint64_t minCount = std::max<uint64_t>(
        20, static_cast<uint64_t>(std::ceil(0.001 * static_cast<double>(sampled))));

    std::vector<OverrepHit> kept;
    for (const auto& kv : counts_) {
        if (kv.second < minCount) continue;
        OverrepHit h;
        h.seq = kv.first;
        h.count = kv.second;
        kept.push_back(std::move(h));
    }
    if (kept.empty()) return;

    std::sort(kept.begin(), kept.end(), [](const OverrepHit& a, const OverrepHit& b) {
        if (a.count != b.count) return a.count > b.count;
        if (a.seq.size() != b.seq.size()) return a.seq.size() > b.seq.size();
        return a.seq < b.seq;
    });

    // A short sequence that occurs about as often as a longer one containing it
    // carries no extra information, so only the longer form is reported.
    std::vector<bool> drop(kept.size(), false);
    for (size_t i = 0; i < kept.size(); ++i) {
        if (drop[i]) continue;
        for (size_t j = 0; j < kept.size(); ++j) {
            if (i == j || drop[j]) continue;
            if (kept[j].seq.size() <= kept[i].seq.size()) continue;
            const double diff = std::fabs(static_cast<double>(kept[i].count) -
                                          static_cast<double>(kept[j].count));
            if (diff <= 0.10 * static_cast<double>(kept[j].count) &&
                kept[j].seq.find(kept[i].seq) != std::string::npos) {
                drop[i] = true;
                break;
            }
        }
    }

    const double denom = totalReads > 0 ? static_cast<double>(totalReads)
                                        : static_cast<double>(sampled);
    for (size_t i = 0; i < kept.size() && hits.size() < 100; ++i) {
        if (drop[i]) continue;
        OverrepHit h = std::move(kept[i]);
        auto it = positions_.find(h.seq);
        if (it != positions_.end()) h.positions = it->second;
        if (cycles_ > 0 && h.positions.size() < cycles_) h.positions.resize(cycles_, 0);
        // Counts come from a 1-in-`sampling` subset, so scale back up to the run.
        if (denom > 0.0) {
            h.percent = 100.0 * static_cast<double>(h.count) *
                        static_cast<double>(sampling) / denom;
            if (h.percent > 100.0) h.percent = 100.0;
        }
        h.source = identifyContaminant(h.seq);
        hits.push_back(std::move(h));
    }
}

// ---------------------------------------------------------------------------
// Built-in contaminant list
// ---------------------------------------------------------------------------

std::string identifyContaminant(const std::string& seq) {
    static const std::pair<const char*, const char*> kKnown[] = {
        {"AGATCGGAAGAGCACACGTCTGAACTCCAGTCA", "Illumina TruSeq Adapter Read 1"},
        {"AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGT", "Illumina TruSeq Adapter Read 2"},
        {"AGATCGGAAGAGC", "Illumina TruSeq Adapter (common stem)"},
        {"CTGTCTCTTATACACATCT", "Nextera Transposase Sequence"},
        {"TGGAATTCTCGGGTGCCAAGG", "Illumina Small RNA 3' Adapter"},
        {"GATCGTCGGACTGTAGAACTCTGAAC", "Illumina Small RNA 5' Adapter"},
        {"AATGATACGGCGACCACCGAGATCTACAC", "Illumina TruSeq Universal Adapter"},
        {"CAAGCAGAAGACGGCATACGAGAT", "Illumina PCR Primer Index 2"},
        {"AAGTCGGAGGCCAAGCGGTCTTAGGAAGACAA", "BGI/MGI Adapter Read 1"},
        {"AAGTCGGATCGTAGCCATGTCGTTCTGTGAGCCAAGGAGTTG", "BGI/MGI Adapter Read 2"},
        {"ACACTCTTTCCCTACACGACGCTCTTCCGATCT", "Illumina Sequencing Primer Read 1"},
        {"GTGACTGGAGTTCAGACGTGTGCTCTTCCGATCT", "Illumina Sequencing Primer Read 2"},
        {"GAGTTTTATCGCTTCCATGACGCAGAAGTTAACACTTTCGG", "PhiX174 control (genome 5' end)"},
        {"TCTTTCCCTACACGACGCTCTTCCGATCT", "Illumina Single End Adapter 1"},
        {"CGGTCTCGGCATTCCTGCTGAACCGCTCTTCCGATCT", "Illumina Multiplexing Adapter"},
        {"GTTCAGAGTTCTACAGTCCGACGATC", "Illumina Small RNA RT Primer"},
        {"ATCTCGTATGCCGTCTTCTGCTTG", "Illumina Adapter Index Tail"},
    };

    if (seq.empty()) return "";
    const std::string q = util::toUpper(seq);
    constexpr size_t kMinShared = 12;

    // Poly-X: a homopolymer (allowing an occasional miscall) of at least 12 bp.
    if (q.size() >= kMinShared) {
        size_t best = 1, run = 1;
        char bestBase = q[0];
        for (size_t i = 1; i < q.size(); ++i) {
            if (q[i] == q[i - 1]) {
                run++;
                if (run > best) { best = run; bestBase = q[i]; }
            } else {
                run = 1;
            }
        }
        if (best >= kMinShared && static_cast<double>(best) >= 0.9 * static_cast<double>(q.size())) {
            switch (bestBase) {
                case 'A': return "PolyA";
                case 'C': return "PolyC";
                case 'G': return "PolyG (no-signal artefact, 2-colour chemistry)";
                case 'T': return "PolyT";
                case 'N': return "PolyN";
                default: break;
            }
        }
    }

    for (const auto& entry : kKnown) {
        const std::string known(entry.first);
        if (known.size() >= kMinShared && q.find(known) != std::string::npos) return entry.second;
        if (q.size() >= kMinShared && known.find(q) != std::string::npos) return entry.second;
    }
    return "";
}

// ---------------------------------------------------------------------------
// InsertSizeAnalyzer
// ---------------------------------------------------------------------------

InsertSizeAnalyzer::InsertSizeAnalyzer(int maxSizeIn) : maxSize(maxSizeIn < 1 ? 1 : maxSizeIn) {
    histogram.assign(static_cast<size_t>(maxSize) + 1, 0);
}

void InsertSizeAnalyzer::observe(int insertSize) {
    if (insertSize <= 0) {
        unknown++;
        return;
    }
    // Everything past the configured maximum lands in the last bin.
    size_t idx = static_cast<size_t>(insertSize);
    if (idx > static_cast<size_t>(maxSize)) idx = static_cast<size_t>(maxSize);
    if (histogram.size() <= idx) histogram.resize(idx + 1, 0);
    histogram[idx]++;
    counted++;
}

void InsertSizeAnalyzer::merge(const InsertSizeAnalyzer& other) {
    if (other.maxSize > maxSize) maxSize = other.maxSize;
    if (histogram.size() < other.histogram.size()) histogram.resize(other.histogram.size(), 0);
    for (size_t i = 0; i < other.histogram.size(); ++i) histogram[i] += other.histogram[i];
    counted += other.counted;
    unknown += other.unknown;
}

void InsertSizeAnalyzer::finalize() {
    mode = 0;
    mean = 0.0;
    median = 0.0;
    stddev = 0.0;
    p10 = 0;
    p90 = 0;

    uint64_t total = 0;
    uint64_t best = 0;
    double sum = 0.0;
    for (size_t i = 1; i < histogram.size(); ++i) {
        total += histogram[i];
        sum += static_cast<double>(histogram[i]) * static_cast<double>(i);
        if (histogram[i] > best) {
            best = histogram[i];
            mode = static_cast<int>(i);
        }
    }
    if (total == 0) return;

    mean = sum / static_cast<double>(total);

    double var = 0.0;
    for (size_t i = 1; i < histogram.size(); ++i) {
        const double d = static_cast<double>(i) - mean;
        var += static_cast<double>(histogram[i]) * d * d;
    }
    stddev = std::sqrt(var / static_cast<double>(total));

    auto quantile = [&](double q) {
        const double target = q * static_cast<double>(total);
        uint64_t cum = 0;
        for (size_t i = 1; i < histogram.size(); ++i) {
            const uint64_t n = histogram[i];
            if (n == 0) continue;
            if (static_cast<double>(cum + n) >= target) {
                const double frac = (target - static_cast<double>(cum)) / static_cast<double>(n);
                return static_cast<double>(i) + frac - 0.5;
            }
            cum += n;
        }
        return static_cast<double>(histogram.size() - 1);
    };

    median = quantile(0.5);
    p10 = static_cast<int>(std::lround(quantile(0.10)));
    p90 = static_cast<int>(std::lround(quantile(0.90)));
    if (p10 < 0) p10 = 0;
    if (p90 < 0) p90 = 0;
}

// ---------------------------------------------------------------------------
// RunStats
// ---------------------------------------------------------------------------

void RunStats::mergeWorker(const RunStats& w) {
    before1.merge(w.before1);
    before2.merge(w.before2);
    after1.merge(w.after1);
    after2.merge(w.after2);
    merged.merge(w.merged);

    tiles1.merge(w.tiles1);
    tiles2.merge(w.tiles2);
    errorModel.merge(w.errorModel);
    kmers.merge(w.kmers);
    duplication.merge(w.duplication);
    overrep1.merge(w.overrep1);
    overrep2.merge(w.overrep2);
    insertSize.merge(w.insertSize);

    for (size_t i = 0; i < filterCounts.size(); ++i) filterCounts[i] += w.filterCounts[i];

    readsIn += w.readsIn;
    readsOut += w.readsOut;
    basesIn += w.basesIn;
    basesOut += w.basesOut;

    adapterTrimmedReads1 += w.adapterTrimmedReads1;
    adapterTrimmedReads2 += w.adapterTrimmedReads2;
    adapterTrimmedBases1 += w.adapterTrimmedBases1;
    adapterTrimmedBases2 += w.adapterTrimmedBases2;
    for (const auto& kv : w.adapterCounts1) adapterCounts1[kv.first] += kv.second;
    for (const auto& kv : w.adapterCounts2) adapterCounts2[kv.first] += kv.second;

    polyGTrimmedReads += w.polyGTrimmedReads;
    polyGTrimmedBases += w.polyGTrimmedBases;
    polyXTrimmedReads += w.polyXTrimmedReads;
    polyXTrimmedBases += w.polyXTrimmedBases;

    correctedReads += w.correctedReads;
    correctedBases += w.correctedBases;
    mergedPairs += w.mergedPairs;

    addVec(adapterStart1, w.adapterStart1);
    addVec(adapterStart2, w.adapterStart2);
}

void RunStats::finalize() {
    before1.finalize();
    before2.finalize();
    after1.finalize();
    after2.finalize();
    merged.finalize();

    tiles1.finalize();
    tiles2.finalize();
    errorModel.finalize();
    kmers.finalize();
    duplication.finalize();
    overrep1.finalize(before1.reads ? before1.reads : readsIn);
    overrep2.finalize(before2.reads ? before2.reads : readsIn);
    insertSize.finalize();
}

}  // namespace fp
