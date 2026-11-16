#include "pipeline.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <mutex>
#include <unordered_set>

#include "fastqio.h"
#include "util.h"

namespace fp {

const char* complementTable();   // defined in fastqio.cpp

namespace {


inline char complementOf(char c) { return complementTable()[static_cast<unsigned char>(c)]; }

void rebaseToPhred33(std::string& qual) {
    for (char& c : qual) {
        int q = static_cast<int>(static_cast<unsigned char>(c)) - 64;
        if (q < 0) q = 0;
        c = static_cast<char>(q + 33);
    }
}

inline bool isACGT(char c) {
    return c == 'A' || c == 'C' || c == 'G' || c == 'T';
}

// Reverse complement into a caller-owned buffer to keep the hot path free of
// allocations.
void reverseComplementInto(const std::string& src, std::string& dst) {
    size_t n = src.size();
    dst.resize(n);
    const char* comp = complementTable();
    for (size_t i = 0; i < n; ++i) {
        dst[i] = comp[static_cast<unsigned char>(src[n - 1 - i])];
    }
}

}  // namespace

// ---------------------------------------------------------------------------
// Basic measurements
// ---------------------------------------------------------------------------

int hammingDistance(const std::string& a, const std::string& b, int maxDiff) {
    size_t n = std::min(a.size(), b.size());
    int diff = static_cast<int>(a.size() > b.size() ? a.size() - b.size() : b.size() - a.size());
    if (diff > maxDiff) return diff;
    for (size_t i = 0; i < n; ++i) {
        if (a[i] != b[i] && ++diff > maxDiff) return diff;
    }
    return diff;
}

double meanQuality(const Read& r, int qualOffset) {
    if (r.qual.empty()) return 0;
    uint64_t sum = 0;
    for (char c : r.qual) sum += static_cast<uint64_t>(c - qualOffset);
    return static_cast<double>(sum) / static_cast<double>(r.qual.size());
}

double sequenceComplexity(const Read& r) {
    if (r.seq.size() < 2) return 100.0;
    size_t diff = 0;
    for (size_t i = 0; i + 1 < r.seq.size(); ++i) {
        if (r.seq[i] != r.seq[i + 1]) ++diff;
    }
    return 100.0 * static_cast<double>(diff) / static_cast<double>(r.seq.size() - 1);
}

// ---------------------------------------------------------------------------
// Overlap analysis
// ---------------------------------------------------------------------------

OverlapResult analyzeOverlap(const Read& r1, const Read& r2, const OverlapOptions& opt,
                             bool allowGap) {
    OverlapResult best;
    const int len1 = static_cast<int>(r1.length());
    const int len2 = static_cast<int>(r2.length());
    if (len1 < opt.lenRequire || len2 < opt.lenRequire) return best;

    thread_local std::string rc2;
    reverseComplementInto(r2.seq, rc2);

    int bestDiff = INT32_MAX;
    int bestS = 0, bestLen = 0;

    // `s` is where rc2 starts inside read1's frame; the insert length is
    // s + len2 for every s, which is what later stages rely on.
    const int sMax = len1 - opt.lenRequire;
    const int sMin = opt.lenRequire - len2;

    // fastp's alignment rule, reproduced deliberately.
    //
    // It takes the FIRST acceptable offset rather than the best-scoring one,
    // and it sweeps s from 0 upward (longest overlap first) before trying
    // negative s. Searching for the minimum-mismatch alignment instead lets a
    // short spurious exact match beat the long, slightly-mismatched true one --
    // on a repetitive insert that fabricates sequence when the pair is merged.
    //
    // The diff limit is also only enforced over the first `kCompleteCompare`
    // bases. Beyond that, mismatches are counted but no longer disqualify: real
    // read-through overlaps are long and their tails are the lowest-quality
    // part of both reads, so a whole-overlap limit rejects exactly the
    // alignments adapter trimming exists to find.
    constexpr int kCompleteCompare = 50;
    auto tryOffset = [&](int s) {
        const int start1 = s > 0 ? s : 0;
        const int start2 = s < 0 ? -s : 0;
        const int overlapLen = std::min(len1 - start1, len2 - start2);
        if (overlapLen < opt.lenRequire) return false;

        const int maxDiff = std::min(opt.diffLimit, overlapLen * opt.diffPercentLimit / 100);
        int diff = 0;
        int i = 0;
        const char* a = r1.seq.data() + start1;
        const char* b = rc2.data() + start2;
        for (i = 0; i < overlapLen; ++i) {
            if (a[i] != b[i]) {
                ++diff;
                if (diff > maxDiff && i < kCompleteCompare) break;
            }
        }
        if (diff <= maxDiff || (diff > maxDiff && i > kCompleteCompare)) {
            bestDiff = diff;
            bestS = s;
            bestLen = overlapLen;
            return true;
        }
        return false;
    };

    bool found = false;
    for (int s = 0; s <= sMax && !found; ++s) found = tryOffset(s);
    if (!found) {
        for (int s = -1; s >= sMin && !found; --s) found = tryOffset(s);
    }

    if (found && bestLen > 0) {
        best.overlapped = true;
        best.offset = bestS;
        best.overlapLen = bestLen;
        best.diff = bestDiff;
        return best;
    }

    if (!allowGap) return best;

    // Opt-in single-gap search: only run when the ungapped scan failed, and
    // only around the offset with the fewest mismatches, since a full gapped
    // search over every offset would dominate runtime.
    int fallbackS = 0, fallbackDiff = INT32_MAX, fallbackLen = 0;
    for (int s = sMax; s >= sMin; --s) {
        const int start1 = s > 0 ? s : 0;
        const int start2 = s < 0 ? -s : 0;
        const int overlapLen = std::min(len1 - start1, len2 - start2);
        if (overlapLen < opt.lenRequire) continue;
        int diff = 0;
        for (int i = 0; i < overlapLen; ++i) {
            if (r1.seq[start1 + i] != rc2[start2 + i]) ++diff;
        }
        if (diff < fallbackDiff) { fallbackDiff = diff; fallbackS = s; fallbackLen = overlapLen; }
    }
    if (fallbackLen == 0) return best;

    const int start1 = fallbackS > 0 ? fallbackS : 0;
    const int start2 = fallbackS < 0 ? -fallbackS : 0;
    const int maxDiff = std::min(opt.diffLimit, fallbackLen * opt.diffPercentLimit / 100);

    for (int g = opt.lenRequire / 2; g < fallbackLen - 1; ++g) {
        for (int dir = 0; dir < 2; ++dir) {
            int diff = 0;
            int i = 0;
            for (; i < g; ++i) {
                if (r1.seq[start1 + i] != rc2[start2 + i]) ++diff;
            }
            if (diff > maxDiff) continue;
            int o1 = start1 + g + (dir == 0 ? 1 : 0);
            int o2 = start2 + g + (dir == 1 ? 1 : 0);
            int remain = std::min(len1 - o1, static_cast<int>(rc2.size()) - o2);
            for (int j = 0; j < remain; ++j) {
                if (r1.seq[o1 + j] != rc2[o2 + j] && ++diff > maxDiff) break;
            }
            if (diff <= maxDiff) {
                best.overlapped = true;
                best.offset = fallbackS;
                best.overlapLen = fallbackLen;
                best.diff = diff;
                best.hasGap = true;
                return best;
            }
        }
    }
    return best;
}

// ---------------------------------------------------------------------------
// Trimming primitives
// ---------------------------------------------------------------------------

size_t trimPolyTail(Read& r, char base, int minLen) {
    const int rlen = static_cast<int>(r.length());
    if (rlen < minLen) return 0;

    // A poly-X tail is allowed roughly one mismatch per eight bases so that a
    // sequencing error in the middle of the run does not hide it.
    constexpr int kOneMismatchPer = 8;
    constexpr int kMaxMismatch = 5;

    int mismatch = 0;
    int firstPos = rlen;
    int i = 0;
    for (; i < rlen; ++i) {
        char c = r.seq[rlen - i - 1];
        if (c != base) {
            ++mismatch;
        } else {
            firstPos = rlen - i - 1;
        }
        int allowed = (i + 1) / kOneMismatchPer;
        if (mismatch > kMaxMismatch || (mismatch > allowed && i >= minLen - 1)) break;
    }
    if (i >= minLen && firstPos < rlen) {
        size_t removed = r.length() - static_cast<size_t>(firstPos);
        r.truncate(static_cast<size_t>(firstPos));
        return removed;
    }
    return 0;
}

// The three quality-cut modes follow fastp's Filter::trimAndCut exactly,
// including the parts that look like off-by-ones and are not:
//
//   cut_front  stops at the FIRST passing window and keeps from its LAST base
//              (s = s + w - 1), then skips leading Ns.
//   cut_tail   scans backwards to the LAST passing window and keeps up to its
//              FIRST base (t = t - w + 1), then skips trailing Ns.
//   cut_right  scans forward for the first FAILING window and then walks
//              through the good bases at its head before truncating.
//
// Each returns the number of bases removed, or kDiscardRead when the read is
// too short for a single window -- fastp drops the read outright there rather
// than emitting a stub.
constexpr size_t kDiscardRead = static_cast<size_t>(-1);

size_t cutFront(Read& r, int window, int meanQual, int qualOffset) {
    const int len = static_cast<int>(r.length());
    if (window <= 0) return 0;
    if (len - window <= 0) return kDiscardRead;
    const int need = meanQual * window;

    int sum = 0;
    for (int i = 0; i < window - 1; ++i) sum += r.qual[i] - qualOffset;

    int s = 0;
    for (s = 0; s + window < len; ++s) {
        sum += r.qual[s + window - 1] - qualOffset;
        if (s > 0) sum -= r.qual[s - 1] - qualOffset;
        if (sum >= need) break;
    }
    // Keep from the last base of the passing window, not its first.
    if (s > 0) s = s + window - 1;
    while (s < len && r.seq[static_cast<size_t>(s)] == 'N') ++s;
    if (s <= 0) return 0;
    if (s >= len) { const size_t n = r.length(); r.seq.clear(); r.qual.clear(); return n; }
    r.trimFront(static_cast<size_t>(s));
    return static_cast<size_t>(s);
}

size_t cutTail(Read& r, int window, int meanQual, int qualOffset) {
    const int len = static_cast<int>(r.length());
    if (window <= 0) return 0;
    if (len - window <= 0) return kDiscardRead;
    const int need = meanQual * window;

    int sum = 0;
    for (int i = 0; i < window - 1; ++i) sum += r.qual[len - 1 - i] - qualOffset;

    int t = len - 1;
    for (t = len - 1; t - window >= 0; --t) {
        sum += r.qual[t - window + 1] - qualOffset;
        if (t < len - 1) sum -= r.qual[t + 1] - qualOffset;
        if (sum >= need) break;
    }
    // Keep up to the first base of the passing window, not its last.
    if (t < len - 1) t = t - window + 1;
    while (t >= 0 && r.seq[static_cast<size_t>(t)] == 'N') --t;
    const int keep = t + 1;
    if (keep >= len) return 0;
    const size_t removed = r.length() - static_cast<size_t>(std::max(0, keep));
    r.truncate(static_cast<size_t>(std::max(0, keep)));
    return removed;
}

size_t cutRight(Read& r, int window, int meanQual, int qualOffset) {
    const int len = static_cast<int>(r.length());
    if (window <= 0) return 0;
    if (len - window <= 0) return kDiscardRead;
    const int need = meanQual * window;

    int sum = 0;
    for (int i = 0; i < window - 1; ++i) sum += r.qual[i] - qualOffset;

    bool foundLowQualWindow = false;
    int s = 0;
    for (s = 0; s + window < len; ++s) {
        sum += r.qual[s + window - 1] - qualOffset;
        if (s > 0) sum -= r.qual[s - 1] - qualOffset;
        if (sum < need) { foundLowQualWindow = true; break; }
    }
    if (!foundLowQualWindow) return 0;
    // The failing window may still open with good bases; keep them.
    while (s < len - 1 && r.qual[static_cast<size_t>(s)] - qualOffset >= meanQual) ++s;
    const size_t removed = r.length() - static_cast<size_t>(s);
    r.truncate(static_cast<size_t>(s));
    return removed;
}

int findAdapter(const Read& r, const std::string& adapter, int minMatch) {
    const int rlen = static_cast<int>(r.length());
    const int alen = static_cast<int>(adapter.size());
    if (rlen < minMatch || alen == 0) return -1;

    // Adapter read-through always runs to the 3' end, so only alignments that
    // reach the end of the read are considered.
    for (int pos = 0; pos <= rlen - minMatch; ++pos) {
        const int cmpLen = std::min(rlen - pos, alen);
        const int allowed = cmpLen / 8;
        int mismatch = 0;
        bool matched = true;
        for (int i = 0; i < cmpLen; ++i) {
            if (adapter[i] != r.seq[pos + i]) {
                if (++mismatch > allowed) { matched = false; break; }
            }
        }
        if (matched) return pos;
    }
    return -1;
}

// ---------------------------------------------------------------------------
// Shared, exact deduplication
// ---------------------------------------------------------------------------

SharedDedup::SharedDedup(size_t shardBits)
    : shardMask_((size_t{1} << shardBits) - 1), shards_(shardMask_ + 1) {}

bool SharedDedup::seen(uint64_t key) {
    Shard& s = shards_[key & shardMask_];
    std::lock_guard<std::mutex> lock(s.mu);
    return !s.keys.insert(key).second;
}

size_t SharedDedup::uniqueCount() const {
    size_t n = 0;
    for (const auto& s : shards_) n += s.keys.size();
    return n;
}

namespace {

uint64_t encodePrefix(const std::string& seq, size_t n) {
    uint64_t v = 0;
    size_t lim = std::min(n, seq.size());
    for (size_t i = 0; i < lim; ++i) {
        uint64_t code;
        switch (seq[i]) {
            case 'A': code = 0; break;
            case 'C': code = 1; break;
            case 'G': code = 2; break;
            case 'T': code = 3; break;
            default:  code = 0; break;
        }
        v = (v << 2) | code;
    }
    return v;
}

uint64_t mix64(uint64_t x) {
    x += 0x9e3779b97f4a7c15ULL;
    x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
    x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
    return x ^ (x >> 31);
}

}  // namespace

uint64_t dedupKey(const Read& r1, const Read* r2) {
    uint64_t k = mix64(encodePrefix(r1.seq, 32)) ^ (static_cast<uint64_t>(r1.length()) << 1);
    if (r2) k ^= mix64(encodePrefix(r2->seq, 32) + 0x1234567ULL);
    return mix64(k);
}

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

Worker::Worker(const Options& opt)
    : stats(),
      opt_(opt),
      // Phred+64 input is rebased to phred+33 on arrival, so every later stage
      // and every output file speak the one encoding, exactly as fastp does.
      rebasePhred64_(opt.qualityOffset() == 64),
      qualOffset_(33) {
    stats.before1 = ReadSetStats(qualOffset_, opt.deep.positionalQualityDist);
    stats.before2 = ReadSetStats(qualOffset_, opt.deep.positionalQualityDist);
    stats.after1 = ReadSetStats(qualOffset_, opt.deep.positionalQualityDist);
    stats.after2 = ReadSetStats(qualOffset_, opt.deep.positionalQualityDist);
    stats.merged = ReadSetStats(qualOffset_, false);
    stats.kmers = KmerSpectrum(opt.deep.kmerK, 64, opt.deep.kmerSamplingFactor);
    stats.duplication = DuplicationAnalyzer(opt.duplication.resolvedAccuracy(),
                                            opt.duplication.evaluate);
    stats.overrep1 = OverrepAnalyzer(opt.overrep.sampling, opt.overrep.enabled);
    stats.overrep2 = OverrepAnalyzer(opt.overrep.sampling, opt.overrep.enabled);
}

void Worker::emit(std::string& dest, const Read& r) { r.appendTo(dest); }

void Worker::applyFixedTrim(Read& r, bool isRead2) {
    const int front = isRead2 ? opt_.trim.resolvedFront2() : opt_.trim.front1;
    const int tail  = isRead2 ? opt_.trim.resolvedTail2()  : opt_.trim.tail1;
    if (front > 0) r.trimFront(static_cast<size_t>(front));
    if (tail > 0 && r.length() > static_cast<size_t>(tail)) {
        r.truncate(r.length() - static_cast<size_t>(tail));
    } else if (tail > 0) {
        r.seq.clear();
        r.qual.clear();
    }
    // --max_len is deliberately NOT applied here. fastp applies it once, after
    // polyX, and applying it early changes what the later stages see: a read
    // truncated to max_len first can leave a poly-G run too short to meet
    // --poly_g_min_len, so the run survives and the final read is longer than
    // fastp's.
}

size_t Worker::applyPolyG(Read& r) {
    if (!opt_.polyG.enabled) return 0;
    return trimPolyTail(r, 'G', opt_.polyG.minLen);
}

size_t Worker::applyPolyX(Read& r) {
    if (!opt_.polyX.enabled) return 0;
    size_t best = 0;
    char bestBase = 0;
    for (char b : {'A', 'C', 'G', 'T'}) {
        Read probe = r;
        size_t n = trimPolyTail(probe, b, opt_.polyX.minLen);
        if (n > best) { best = n; bestBase = b; }
    }
    if (best > 0) return trimPolyTail(r, bestBase, opt_.polyX.minLen);
    return 0;
}

bool Worker::applyQualityCut(Read& r) {
    if (!opt_.cut.any()) return true;
    if (opt_.cut.front) {
        if (cutFront(r, opt_.cut.fw(), opt_.cut.fq(), qualOffset_) == kDiscardRead) return false;
    }
    // cut_right subsumes cut_tail: running both would trim twice. fastp guards
    // the tail pass with !enabledRight, so match that.
    if (opt_.cut.tail && !opt_.cut.right) {
        if (cutTail(r, opt_.cut.tw(), opt_.cut.tq(), qualOffset_) == kDiscardRead) return false;
    }
    if (opt_.cut.right) {
        if (cutRight(r, opt_.cut.rw(), opt_.cut.rq(), qualOffset_) == kDiscardRead) return false;
    }
    return true;
}

void Worker::recordAdapter(const std::string& seq, bool isRead2, size_t trimPos) {
    if (isRead2) {
        ++stats.adapterTrimmedReads2;
        stats.adapterTrimmedBases2 += seq.size();
        if (seq.size() <= 64) ++stats.adapterCounts2[seq];
        if (trimPos < stats.adapterStart2.size()) ++stats.adapterStart2[trimPos];
    } else {
        ++stats.adapterTrimmedReads1;
        stats.adapterTrimmedBases1 += seq.size();
        if (seq.size() <= 64) ++stats.adapterCounts1[seq];
        if (trimPos < stats.adapterStart1.size()) ++stats.adapterStart1[trimPos];
    }
}

size_t Worker::applyAdapterBySequence(Read& r, bool isRead2) {
    const std::string& ad = isRead2 ? opt_.adapter.resolvedR2 : opt_.adapter.resolvedR1;
    if (ad.empty()) return 0;
    int pos = findAdapter(r, ad);
    if (pos < 0) return 0;
    std::string removed = r.seq.substr(static_cast<size_t>(pos));
    r.truncate(static_cast<size_t>(pos));
    recordAdapter(removed, isRead2, static_cast<size_t>(pos));
    return removed.size();
}

void Worker::applyExtraAdapters(Read& r, bool isRead2) {
    for (const std::string& ad : opt_.adapter.extraSeqs) {
        if (ad.empty() || r.empty()) continue;
        int pos = findAdapter(r, ad);
        if (pos >= 0) {
            std::string removed = r.seq.substr(static_cast<size_t>(pos));
            r.truncate(static_cast<size_t>(pos));
            recordAdapter(removed, isRead2, static_cast<size_t>(pos));
        }
    }
}

void Worker::trimByOverlap(Read& r1, Read& r2, const OverlapResult& ov) {
    if (!ov.overlapped) return;
    const int insert = insertSizeOf(ov, r2);
    if (insert <= 0) return;
    // Bases beyond the insert are adapter read-through.
    if (insert < static_cast<int>(r1.length())) {
        std::string removed = r1.seq.substr(static_cast<size_t>(insert));
        r1.truncate(static_cast<size_t>(insert));
        recordAdapter(removed, false, static_cast<size_t>(insert));
    }
    if (insert < static_cast<int>(r2.length())) {
        std::string removed = r2.seq.substr(static_cast<size_t>(insert));
        r2.truncate(static_cast<size_t>(insert));
        recordAdapter(removed, true, static_cast<size_t>(insert));
    }
}

void Worker::correctByOverlap(Read& r1, Read& r2, const OverlapResult& ov) {
    if (!ov.overlapped || ov.diff == 0 || ov.hasGap) return;

    const int start1 = ov.offset > 0 ? ov.offset : 0;
    const int start2 = ov.offset < 0 ? -ov.offset : 0;
    const int len2 = static_cast<int>(r2.length());
    int corrected = 0;

    for (int i = 0; i < ov.overlapLen; ++i) {
        const int p1 = start1 + i;
        const int p2 = len2 - 1 - (start2 + i);   // position in the original r2
        if (p1 >= static_cast<int>(r1.length()) || p2 < 0 || p2 >= len2) continue;

        const char b1 = r1.seq[p1];
        const char b2c = complementOf(r2.seq[p2]);
        if (b1 == b2c) continue;

        const int q1 = r1.qual[p1] - qualOffset_;
        const int q2 = r2.qual[p2] - qualOffset_;
        // Only correct when one base is clearly more trustworthy, otherwise the
        // disagreement is left in place for downstream tools to judge.
        if (q1 >= 30 && q2 <= 15) {
            r2.seq[p2] = complementOf(b1);
            r2.qual[p2] = r1.qual[p1];
            ++corrected;
        } else if (q2 >= 30 && q1 <= 15) {
            r1.seq[p1] = b2c;
            r1.qual[p1] = r2.qual[p2];
            ++corrected;
        }
    }
    if (corrected) {
        ++stats.correctedReads;
        stats.correctedBases += static_cast<uint64_t>(corrected);
    }
}

bool Worker::mergePair(const Read& r1, const Read& r2, const OverlapResult& ov, Read& out) {
    if (!ov.overlapped) return false;
    const int insert = insertSizeOf(ov, r2);
    if (insert <= 0) return false;

    thread_local std::string rc2seq, rc2qual;
    reverseComplementInto(r2.seq, rc2seq);
    rc2qual.assign(r2.qual.rbegin(), r2.qual.rend());

    const int len1 = static_cast<int>(r1.length());
    out.seq.assign(static_cast<size_t>(insert), 'N');
    out.qual.assign(static_cast<size_t>(insert), static_cast<char>(qualOffset_));

    // rc2 occupies [offset, offset + len2) in the insert's coordinate frame.
    for (int i = 0; i < insert; ++i) {
        const int i1 = i;                 // read1 covers the insert from its start
        const int i2 = i - ov.offset;     // index into rc2
        const bool has1 = i1 >= 0 && i1 < len1;
        const bool has2 = i2 >= 0 && i2 < static_cast<int>(rc2seq.size());

        if (has1 && has2) {
            const char a = r1.seq[i1], b = rc2seq[i2];
            const char qa = r1.qual[i1], qb = rc2qual[i2];
            if (a == b) {
                out.seq[i] = a;
                // Agreement raises confidence, capped at Q41 to stay in range.
                int q = std::min(41, (qa - qualOffset_) + (qb - qualOffset_) / 2);
                out.qual[i] = static_cast<char>(q + qualOffset_);
            } else if (qa >= qb) {
                out.seq[i] = a;
                out.qual[i] = static_cast<char>(std::max(2, (qa - qualOffset_) - (qb - qualOffset_)) + qualOffset_);
            } else {
                out.seq[i] = b;
                out.qual[i] = static_cast<char>(std::max(2, (qb - qualOffset_) - (qa - qualOffset_)) + qualOffset_);
            }
        } else if (has1) {
            out.seq[i] = r1.seq[i1];
            out.qual[i] = r1.qual[i1];
        } else if (has2) {
            out.seq[i] = rc2seq[i2];
            out.qual[i] = rc2qual[i2];
        }
    }
    out.name = r1.name;
    out.strand = "+";
    return true;
}

bool Worker::indexFiltered(const std::string& name) const {
    if (!opt_.indexFilter.enabled) return false;
    size_t sp = name.find(' ');
    if (sp == std::string::npos) return false;
    // Illumina comment field: "1:N:0:INDEX1+INDEX2"
    std::string comment = name.substr(sp + 1);
    size_t last = comment.find_last_of(':');
    if (last == std::string::npos) return false;
    std::string idx = comment.substr(last + 1);
    std::string i1 = idx, i2;
    size_t plus = idx.find('+');
    if (plus != std::string::npos) { i1 = idx.substr(0, plus); i2 = idx.substr(plus + 1); }

    const int thr = opt_.indexFilter.threshold;
    for (const std::string& b : opt_.indexFilter.blacklist1) {
        if (hammingDistance(i1, b, thr) <= thr) return true;
    }
    if (!i2.empty()) {
        for (const std::string& b : opt_.indexFilter.blacklist2) {
            if (hammingDistance(i2, b, thr) <= thr) return true;
        }
    }
    return false;
}

FilterResult Worker::filterRead(const Read& r) {
    // Check order follows fastp: unqualified fraction, then average quality,
    // then N count, then length, then complexity. Every order rejects the same
    // reads, but the one that fires first is the category the read is reported
    // under, so a different order silently relabels the JSON breakdown -- a
    // short all-Q2 read is "low quality" to fastp and would be "too short" if
    // length were tested first.
    if (opt_.qualityFilter.enabled) {
        int nCount = 0, unqualified = 0;
        uint64_t qsum = 0;
        const int qualifiedAt = opt_.qualityFilter.qualifiedPhred;
        for (size_t i = 0; i < r.seq.size(); ++i) {
            if (r.seq[i] == 'N') ++nCount;
            int q = r.qual[i] - qualOffset_;
            if (q < qualifiedAt) ++unqualified;
            qsum += static_cast<uint64_t>(q);
        }
        if (!r.seq.empty()) {
            if (unqualified * 100 > static_cast<int>(r.seq.size()) * opt_.qualityFilter.unqualifiedPercentLimit) {
                return FilterResult::FailedQuality;
            }
            if (opt_.qualityFilter.averageQual > 0) {
                double avg = static_cast<double>(qsum) / static_cast<double>(r.seq.size());
                if (avg < opt_.qualityFilter.averageQual) return FilterResult::FailedAverageQuality;
            }
        }
        if (nCount > opt_.qualityFilter.nBaseLimit) return FilterResult::FailedNBase;
    }

    if (opt_.lengthFilter.enabled) {
        if (r.length() < static_cast<size_t>(opt_.lengthFilter.required)) return FilterResult::FailedLength;
        if (opt_.lengthFilter.limit > 0 && r.length() > static_cast<size_t>(opt_.lengthFilter.limit)) {
            return FilterResult::FailedTooLong;
        }
    } else if (r.empty()) {
        return FilterResult::FailedLength;
    }

    if (opt_.complexityFilter.enabled) {
        // A read of one base has no dinucleotide to score, so complexity is
        // undefined; fastp rejects it rather than letting it through.
        if (r.length() <= 1) return FilterResult::FailedComplexity;
        if (sequenceComplexity(r) < opt_.complexityFilter.threshold) return FilterResult::FailedComplexity;
    }
    if (indexFiltered(r.name)) return FilterResult::FailedIndex;
    return FilterResult::Pass;
}

void Worker::applyUmi(Read& r1, Read* r2) {
    if (!opt_.umi.enabled) return;
    std::string umi;

    auto indexFromName = [](const std::string& name, int which) -> std::string {
        size_t sp = name.find(' ');
        if (sp == std::string::npos) return "";
        std::string comment = name.substr(sp + 1);
        size_t last = comment.find_last_of(':');
        if (last == std::string::npos) return "";
        std::string idx = comment.substr(last + 1);
        size_t plus = idx.find('+');
        if (which == 1) return plus == std::string::npos ? idx : idx.substr(0, plus);
        return plus == std::string::npos ? "" : idx.substr(plus + 1);
    };

    auto takeFromRead = [&](Read& r) -> std::string {
        int n = opt_.umi.len;
        if (n <= 0 || r.length() < static_cast<size_t>(n)) return "";
        std::string u = r.seq.substr(0, static_cast<size_t>(n));
        r.trimFront(static_cast<size_t>(n));
        if (opt_.umi.skip > 0) r.trimFront(static_cast<size_t>(opt_.umi.skip));
        return u;
    };

    switch (opt_.umi.loc) {
        case UmiLoc::Index1: umi = indexFromName(r1.name, 1); break;
        case UmiLoc::Index2: umi = indexFromName(r1.name, 2); break;
        case UmiLoc::PerIndex: {
            std::string a = indexFromName(r1.name, 1), b = indexFromName(r1.name, 2);
            umi = b.empty() ? a : a + "_" + b;
            break;
        }
        case UmiLoc::Read1: umi = takeFromRead(r1); break;
        case UmiLoc::Read2: if (r2) umi = takeFromRead(*r2); break;
        case UmiLoc::PerRead: {
            std::string a = takeFromRead(r1);
            std::string b = r2 ? takeFromRead(*r2) : std::string();
            umi = b.empty() ? a : a + "_" + b;
            break;
        }
        default: return;
    }
    if (umi.empty()) return;

    std::string tag = opt_.umi.prefix.empty() ? umi : opt_.umi.prefix + "_" + umi;
    auto tagName = [&](std::string& name) {
        size_t sp = name.find(' ');
        if (sp == std::string::npos) {
            name += opt_.umi.delim + tag;
        } else {
            name = name.substr(0, sp) + opt_.umi.delim + tag + name.substr(sp);
        }
    };
    tagName(r1.name);
    if (r2) tagName(r2->name);
}

void Worker::processPair(Read& r1, Read& r2, Chunk& chunk, uint64_t readIndex) {
    stats.readsIn += 2;
    stats.basesIn += r1.length() + r2.length();

    stats.before1.add(r1);
    stats.before2.add(r2);
    if (opt_.deep.perTile) {
        stats.tiles1.add(r1.name, r1, qualOffset_);
        stats.tiles2.add(r2.name, r2, qualOffset_);
    }
    if (opt_.overrep.enabled) {
        stats.overrep1.add(r1, readIndex);
        stats.overrep2.add(r2, readIndex);
    }
    if (opt_.deep.kmerSpectrum &&
        (readIndex % static_cast<uint64_t>(opt_.deep.kmerSamplingFactor)) == 0) {
        stats.kmers.add(r1);
        stats.kmers.add(r2);
    }

    if (opt_.fixMgiId) {
        auto fix = [](std::string& n) {
            if (n.size() > 2 && n[n.size() - 2] == '/' && (n.back() == '1' || n.back() == '2')) {
                n.resize(n.size() - 2);
            }
        };
        fix(r1.name);
        fix(r2.name);
    }

    applyUmi(r1, &r2);
    applyFixedTrim(r1, false);
    applyFixedTrim(r2, true);

    // fastp fuses the fixed trim and the quality cut into one stage that runs
    // before polyG and adapter trimming. Running the cut later instead lets
    // adapter matching see bases the cut would already have removed, so reads
    // get counted as adapter-trimmed that fastp never touches.
    const bool keep1 = applyQualityCut(r1);
    const bool keep2 = applyQualityCut(r2);
    if (!keep1 || !keep2) {
        // fastp discards a read whose length cannot hold one cut window, and a
        // paired read goes with its mate.
        r1.seq.clear(); r1.qual.clear();
        r2.seq.clear(); r2.qual.clear();
    }

    size_t pg = applyPolyG(r1) + applyPolyG(r2);
    if (pg) { ++stats.polyGTrimmedReads; stats.polyGTrimmedBases += pg; }

    OverlapResult ov;
    const bool needOverlap = opt_.adapter.enabled || opt_.correction.enabled ||
                             opt_.merge.enabled || opt_.deep.insertSize ||
                             opt_.deep.errorProfile || !opt_.overlappedOut.empty();
    if (needOverlap && !r1.empty() && !r2.empty()) {
        ov = analyzeOverlap(r1, r2, opt_.overlap, opt_.adapter.allowGapOverlapTrimming);
    }

    if (opt_.deep.insertSize) {
        if (ov.overlapped) stats.insertSize.observe(insertSizeOf(ov, r2));
        else ++stats.insertSize.unknown;
    }
    if (opt_.deep.errorProfile && ov.overlapped && !ov.hasGap) {
        stats.errorModel.observe(r1, r2, ov, qualOffset_);
    }
    if (!opt_.overlappedOut.empty() && ov.overlapped && ov.diff == 0) {
        const int start1 = ov.offset > 0 ? ov.offset : 0;
        Read piece;
        piece.name = r1.name;
        piece.strand = "+";
        piece.seq = r1.seq.substr(static_cast<size_t>(start1), static_cast<size_t>(ov.overlapLen));
        piece.qual = r1.qual.substr(static_cast<size_t>(start1), static_cast<size_t>(ov.overlapLen));
        emit(chunk.outOverlapped, piece);
    }

    // Correction has to happen before any trimming: `ov` was computed against
    // the reads as they are now, and adapter trimming shortens r2, after which
    // the offsets in `ov` no longer describe it. Correcting afterwards mapped
    // r1 positions onto the wrong r2 bases and wrote the complement of an
    // unrelated base over good data.
    if (opt_.correction.enabled) correctByOverlap(r1, r2, ov);

    if (opt_.adapter.enabled) {
        if (ov.overlapped) {
            trimByOverlap(r1, r2, ov);
        } else {
            applyAdapterBySequence(r1, false);
            applyAdapterBySequence(r2, true);
        }
        if (!opt_.adapter.extraSeqs.empty()) {
            applyExtraAdapters(r1, false);
            applyExtraAdapters(r2, true);
        }
    }

    size_t px = applyPolyX(r1) + applyPolyX(r2);
    if (px) { ++stats.polyXTrimmedReads; stats.polyXTrimmedBases += px; }

    if (opt_.trim.maxLen1 > 0 && r1.length() > static_cast<size_t>(opt_.trim.maxLen1)) {
        r1.truncate(static_cast<size_t>(opt_.trim.maxLen1));
    }
    if (opt_.trim.resolvedMaxLen2() > 0 &&
        r2.length() > static_cast<size_t>(opt_.trim.resolvedMaxLen2())) {
        r2.truncate(static_cast<size_t>(opt_.trim.resolvedMaxLen2()));
    }

    FilterResult f1 = filterRead(r1);
    FilterResult f2 = filterRead(r2);

    bool duplicate = false;
    if (opt_.duplication.evaluate || opt_.duplication.dedup) {
        const bool dup = stats.duplication.observe(r1, &r2);
        if (opt_.duplication.dedup) {
            duplicate = dedup_ ? dedup_->seen(dedupKey(r1, &r2)) : dup;
        }
    }
    if (duplicate) {
        ++stats.filterCounts[static_cast<size_t>(FilterResult::Duplicated)];
        if (!opt_.failedOut.empty()) { emit(chunk.outFailed, r1); emit(chunk.outFailed, r2); }
        return;
    }

    // A pair is kept or dropped together, so both mates are reported under the
    // same outcome -- the failing one when they disagree. Counting each mate
    // under its own result made a discarded pair read as one passed and one
    // failed, which does not describe anything that was written.
    const FilterResult pairResult =
        (f1 != FilterResult::Pass) ? f1 : f2;
    stats.filterCounts[static_cast<size_t>(pairResult)] += 2;

    const bool p1 = (f1 == FilterResult::Pass);
    const bool p2 = (f2 == FilterResult::Pass);

    if (opt_.merge.enabled) {
        if (p1 && p2) {
            Read m;
            if (mergePair(r1, r2, ov, m) && m.length() >= static_cast<size_t>(opt_.lengthFilter.required)) {
                ++stats.mergedPairs;
                stats.merged.add(m);
                stats.readsOut += 1;
                stats.basesOut += m.length();
                emit(chunk.outMerged, m);
                return;
            }
        }
        if (opt_.merge.includeUnmerged) {
            if (p1) { stats.after1.add(r1); emit(chunk.outMerged, r1); stats.readsOut++; stats.basesOut += r1.length(); }
            if (p2) { stats.after2.add(r2); emit(chunk.outMerged, r2); stats.readsOut++; stats.basesOut += r2.length(); }
            return;
        }
    }

    if (p1 && p2) {
        stats.after1.add(r1);
        stats.after2.add(r2);
        stats.readsOut += 2;
        stats.basesOut += r1.length() + r2.length();
        emit(chunk.out1, r1);
        emit(chunk.out2, r2);
    } else if (p1) {
        // The surviving mate is only kept if there is somewhere to put it.
        // Counting it in the after-filtering statistics when no --unpaired
        // output is configured described a read that was never written.
        // fastp also creates no unpaired writer at all unless --unpaired1 is
        // given, so --unpaired2 alone does not rescue read 1.
        if (!opt_.unpaired1.empty()) {
            stats.after1.add(r1);
            stats.readsOut += 1;
            stats.basesOut += r1.length();
            emit(chunk.outUnpaired1, r1);
        }
        if (!opt_.failedOut.empty()) emit(chunk.outFailed, r2);
    } else if (p2) {
        if (!opt_.unpaired1.empty()) {
            stats.after2.add(r2);
            stats.readsOut += 1;
            stats.basesOut += r2.length();
            if (!opt_.unpaired2.empty()) emit(chunk.outUnpaired2, r2);
            else emit(chunk.outUnpaired1, r2);
        }
        if (!opt_.failedOut.empty()) emit(chunk.outFailed, r1);
    } else if (!opt_.failedOut.empty()) {
        emit(chunk.outFailed, r1);
        emit(chunk.outFailed, r2);
    }
}

void Worker::processSingle(Read& r1, Chunk& chunk, uint64_t readIndex) {
    ++stats.readsIn;
    stats.basesIn += r1.length();
    stats.before1.add(r1);
    if (opt_.deep.perTile) stats.tiles1.add(r1.name, r1, qualOffset_);
    if (opt_.overrep.enabled) stats.overrep1.add(r1, readIndex);
    if (opt_.deep.kmerSpectrum &&
        (readIndex % static_cast<uint64_t>(opt_.deep.kmerSamplingFactor)) == 0) {
        stats.kmers.add(r1);
    }

    if (opt_.fixMgiId && r1.name.size() > 2 &&
        r1.name[r1.name.size() - 2] == '/' &&
        (r1.name.back() == '1' || r1.name.back() == '2')) {
        r1.name.resize(r1.name.size() - 2);
    }

    applyUmi(r1, nullptr);
    applyFixedTrim(r1, false);

    // Same stage order as fastp: the quality cut belongs with the fixed trim,
    // ahead of polyG and adapter trimming.
    if (!applyQualityCut(r1)) { r1.seq.clear(); r1.qual.clear(); }

    size_t pg = applyPolyG(r1);
    if (pg) { ++stats.polyGTrimmedReads; stats.polyGTrimmedBases += pg; }

    if (opt_.adapter.enabled) {
        applyAdapterBySequence(r1, false);
        if (!opt_.adapter.extraSeqs.empty()) applyExtraAdapters(r1, false);
    }

    size_t px = applyPolyX(r1);
    if (px) { ++stats.polyXTrimmedReads; stats.polyXTrimmedBases += px; }

    if (opt_.trim.maxLen1 > 0 && r1.length() > static_cast<size_t>(opt_.trim.maxLen1)) {
        r1.truncate(static_cast<size_t>(opt_.trim.maxLen1));
    }

    FilterResult f = filterRead(r1);

    bool duplicate = false;
    if (opt_.duplication.evaluate || opt_.duplication.dedup) {
        const bool dup = stats.duplication.observe(r1, nullptr);
        if (opt_.duplication.dedup) {
            duplicate = dedup_ ? dedup_->seen(dedupKey(r1, nullptr)) : dup;
        }
    }
    if (duplicate) {
        ++stats.filterCounts[static_cast<size_t>(FilterResult::Duplicated)];
        if (!opt_.failedOut.empty()) emit(chunk.outFailed, r1);
        return;
    }

    ++stats.filterCounts[static_cast<size_t>(f)];
    if (f == FilterResult::Pass) {
        stats.after1.add(r1);
        ++stats.readsOut;
        stats.basesOut += r1.length();
        emit(chunk.out1, r1);
    } else if (!opt_.failedOut.empty()) {
        emit(chunk.outFailed, r1);
    }
}

void Worker::process(Chunk& chunk) {
    chunk.clearOutputs();
    // A stable global read index keeps sampling decisions reproducible
    // regardless of how chunks are distributed across threads.
    const uint64_t base = chunk.index * 100000ULL;
    const size_t n = chunk.size();
    if (rebasePhred64_) {
        for (size_t i = 0; i < n; ++i) {
            rebaseToPhred33(chunk.r1[i].qual);
            if (chunk.paired()) rebaseToPhred33(chunk.r2[i].qual);
        }
    }
    if (chunk.paired()) {
        for (size_t i = 0; i < n; ++i) processPair(chunk.r1[i], chunk.r2[i], chunk, base + i);
    } else {
        for (size_t i = 0; i < n; ++i) processSingle(chunk.r1[i], chunk, base + i);
    }
}

}  // namespace fp
