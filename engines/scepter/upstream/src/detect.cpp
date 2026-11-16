// Startup auto-detection: adapter sequences, instrument/platform, quality
// encoding and read geometry.
//
// The sample is drawn from across the whole file (see sampleReads), not from its
// head: on a real flow cell the head of a FASTQ is one tile in one corner, so
// adapter content, quality and the two-colour/poly-G decision taken from it are
// not representative of the run. --sample-head restores the old behaviour.
#include <algorithm>
#include <cmath>
#include <map>
#include <thread>
#include <unordered_map>

#include "fastqio.h"
#include "pipeline.h"
#include "util.h"

namespace fp {

namespace {
constexpr size_t kAdapterSampleReads = 50000;
// Header layout is uniform within a run, so a few hundred reads settle the
// instrument and tile questions; a vote guards against the odd malformed name.
constexpr size_t kHeaderVoteReads = 1000;
constexpr int kSeedLen = 12;
// 4^12 seeds indexed directly; a flat table costs 67 MB transiently but turns
// seed counting into an array increment instead of a hash lookup.
constexpr size_t kSeedSpace = 1u << (2 * kSeedLen);
}  // namespace

const std::vector<KnownAdapter>& knownAdapters() {
    static const std::vector<KnownAdapter> kList = {
        {"Illumina TruSeq Adapter Read 1",  "AGATCGGAAGAGCACACGTCTGAACTCCAGTCA"},
        {"Illumina TruSeq Adapter Read 2",  "AGATCGGAAGAGCGTCGTGTAGGGAAAGAGTGT"},
        {"Illumina Universal Adapter",      "AGATCGGAAGAGC"},
        {"Nextera Transposase Sequence",    "CTGTCTCTTATACACATCT"},
        {"Illumina Small RNA 3' Adapter",   "TGGAATTCTCGGGTGCCAAGG"},
        {"Illumina Small RNA 5' Adapter",   "GATCGTCGGACTGTAGAACTCTGAAC"},
        {"MGI/BGI Adapter Read 1",          "AAGTCGGAGGCCAAGCGGTCTTAGGAAGACAA"},
        {"MGI/BGI Adapter Read 2",          "AAGTCGGATCGTAGCCATGTCGTTCTGTGAGCCAAGGAGTTG"},
        {"Illumina Multiplexing Adapter",   "GATCGGAAGAGCACACGTCT"},
        {"PolyA Tail",                      "AAAAAAAAAAAAAAAAAAAA"},
        {"PolyG Tail (two-colour)",         "GGGGGGGGGGGGGGGGGGGG"},
    };
    return kList;
}

namespace {

// Number of sampled reads containing `probe` starting at any position such that
// the match runs to the end of the read (the signature of adapter read-through).
// A cheap exact search on the probe's head screens out almost every read before
// the tolerant check runs.
size_t countReadThrough(const std::vector<Read>& reads, const std::string& probe,
                        size_t limit) {
    const std::string head = probe.substr(0, std::min<size_t>(probe.size(), 12));
    size_t hits = 0;
    const size_t n = std::min(limit, reads.size());
    for (size_t i = 0; i < n; ++i) {
        if (reads[i].seq.find(head) == std::string::npos) continue;
        Read tmp;
        tmp.seq = reads[i].seq;
        tmp.qual = reads[i].qual;
        if (findAdapter(tmp, probe, 8) >= 0) ++hits;
    }
    return hits;
}

uint64_t encodeSeed(const char* s, int k, bool& ok) {
    uint64_t v = 0;
    for (int i = 0; i < k; ++i) {
        uint64_t c;
        switch (s[i]) {
            case 'A': c = 0; break;
            case 'C': c = 1; break;
            case 'G': c = 2; break;
            case 'T': c = 3; break;
            default: ok = false; return 0;
        }
        v = (v << 2) | c;
    }
    ok = true;
    return v;
}

std::string decodeSeed(uint64_t v, int k) {
    static const char kBases[] = "ACGT";
    std::string s(static_cast<size_t>(k), 'A');
    for (int i = k - 1; i >= 0; --i) {
        s[static_cast<size_t>(i)] = kBases[v & 3];
        v >>= 2;
    }
    return s;
}

}  // namespace

std::string detectAdapterDeNovo(const std::vector<Read>& reads) {
    const size_t nReads = std::min(reads.size(), kAdapterSampleReads);
    if (nReads < 1000) return "";

    // Direct-indexed seed counts: one array increment per position.
    std::vector<uint32_t> seedCount(kSeedSpace, 0);
    size_t totalSeeds = 0, distinctSeeds = 0;

    for (size_t i = 0; i < nReads; ++i) {
        const std::string& s = reads[i].seq;
        const int len = static_cast<int>(s.size());
        if (len < kSeedLen + 4) continue;
        for (int p = 0; p + kSeedLen <= len - 2; ++p) {
            bool ok = false;
            uint64_t key = encodeSeed(s.data() + p, kSeedLen, ok);
            if (!ok) continue;
            if (seedCount[key]++ == 0) ++distinctSeeds;
            ++totalSeeds;
        }
    }
    if (totalSeeds == 0 || distinctSeeds == 0) return "";

    uint64_t bestKey = 0;
    uint32_t bestCount = 0;
    for (size_t key = 0; key < kSeedSpace; ++key) {
        if (seedCount[key] > bestCount) { bestCount = seedCount[key]; bestKey = key; }
    }

    // Require the seed to sit far above the background rate and to appear in a
    // meaningful share of reads; otherwise this is just a repetitive genome.
    const double background = static_cast<double>(totalSeeds) / static_cast<double>(distinctSeeds);
    if (bestCount < 50 || static_cast<double>(bestCount) < 20.0 * background) return "";
    if (static_cast<double>(bestCount) < 0.005 * static_cast<double>(nReads)) return "";

    std::string candidate = decodeSeed(bestKey, kSeedLen);

    // Track where the seed occurs once, then extend against that shrinking list
    // instead of rescanning every read on every round.
    struct Occ { uint32_t read; uint32_t pos; };
    std::vector<Occ> occ;
    occ.reserve(bestCount);
    for (size_t i = 0; i < nReads; ++i) {
        size_t p = reads[i].seq.find(candidate);
        if (p != std::string::npos) {
            occ.push_back({static_cast<uint32_t>(i), static_cast<uint32_t>(p)});
        }
    }
    if (occ.size() < 20) return "";

    auto voteAndExtend = [&](bool rightward, double minFraction) {
        std::array<size_t, 4> votes{};
        size_t support = 0;
        const size_t curLen = candidate.size();
        for (const Occ& o : occ) {
            const std::string& s = reads[o.read].seq;
            size_t idx;
            if (rightward) {
                idx = o.pos + curLen;
                if (idx >= s.size()) continue;
            } else {
                if (o.pos == 0) continue;
                idx = o.pos - 1;
            }
            int c = -1;
            switch (s[idx]) {
                case 'A': c = 0; break;
                case 'C': c = 1; break;
                case 'G': c = 2; break;
                case 'T': c = 3; break;
                default: break;
            }
            if (c < 0) continue;
            ++votes[static_cast<size_t>(c)];
            ++support;
        }
        if (support < 20) return false;
        size_t bi = 0;
        for (size_t i = 1; i < 4; ++i) if (votes[i] > votes[bi]) bi = i;
        if (static_cast<double>(votes[bi]) < minFraction * static_cast<double>(support)) return false;

        const char chosen = "ACGT"[bi];
        std::vector<Occ> kept;
        kept.reserve(occ.size());
        for (const Occ& o : occ) {
            const std::string& s = reads[o.read].seq;
            if (rightward) {
                size_t idx = o.pos + curLen;
                if (idx < s.size() && s[idx] == chosen) kept.push_back(o);
            } else {
                if (o.pos > 0 && s[o.pos - 1] == chosen) {
                    kept.push_back({o.read, o.pos - 1});
                }
            }
        }
        if (kept.size() < 20) return false;
        occ.swap(kept);
        if (rightward) candidate += chosen;
        else candidate.insert(candidate.begin(), chosen);
        return true;
    };

    for (int round = 0; round < 90 && voteAndExtend(true, 0.75); ++round) {}
    // Genomic context has no consensus, so the left extension stops on its own
    // once the true adapter start is reached.
    for (int round = 0; round < 30 && voteAndExtend(false, 0.85); ++round) {}

    if (candidate.size() < 16) return "";

    // A homopolymer is a poly-X artefact, not an adapter; poly-G/X trimming
    // handles those and reporting one as an adapter would be misleading.
    bool homopolymer = true;
    for (char c : candidate) if (c != candidate[0]) { homopolymer = false; break; }
    if (homopolymer) return "";

    return candidate;
}

namespace {

std::string detectFromKnownList(const std::vector<Read>& reads, std::string& name) {
    if (reads.empty()) return "";
    const double minFraction = 0.001;   // 0.1% of reads
    std::string best;
    size_t bestHits = 0;
    for (const KnownAdapter& ka : knownAdapters()) {
        std::string probe(ka.seq);
        if (probe.size() > 20) probe = probe.substr(0, 20);
        // Skip the homopolymer entries here: they are poly-X, handled elsewhere.
        bool homo = true;
        for (char c : probe) if (c != probe[0]) { homo = false; break; }
        if (homo) continue;

        size_t hits = countReadThrough(reads, probe, kAdapterSampleReads);
        const size_t scanned = std::min(reads.size(), kAdapterSampleReads);
        if (hits > bestHits && static_cast<double>(hits) >= minFraction * static_cast<double>(scanned)) {
            bestHits = hits;
            best = ka.seq;
            name = ka.name;
        }
    }
    return best;
}

std::string instrumentOf(const std::string& header) {
    size_t colon = header.find(':');
    std::string first = colon == std::string::npos ? header : header.substr(0, colon);
    size_t sp = first.find(' ');
    if (sp != std::string::npos) first = first.substr(0, sp);
    if (!first.empty() && first[0] == '@') first.erase(0, 1);
    return first;
}

// Two-colour chemistry (NextSeq, NovaSeq, iSeq) encodes G as "no signal", so a
// dark cycle at the end of a read reads out as a run of G.
bool isTwoColourInstrument(const std::string& inst) {
    static const char* kPrefixes[] = {"NS", "NB", "VH", "A0", "LH", "FS", "NDX"};
    std::string up = util::toUpper(inst);
    for (const char* p : kPrefixes) {
        if (util::startsWith(up, p)) return true;
    }
    return false;
}

double polyGTailFraction(const std::vector<Read>& reads, int minLen) {
    if (reads.empty()) return 0;
    size_t hits = 0;
    for (const Read& r : reads) {
        if (static_cast<int>(r.seq.size()) < minLen) continue;
        int run = 0;
        for (int i = static_cast<int>(r.seq.size()) - 1; i >= 0; --i) {
            if (r.seq[static_cast<size_t>(i)] == 'G') ++run; else break;
        }
        if (run >= minLen) ++hits;
    }
    return static_cast<double>(hits) / static_cast<double>(reads.size());
}

// Most common instrument token in the sample, so one malformed header cannot
// decide the platform for the whole run.
std::string votedInstrument(const std::vector<Read>& reads) {
    std::unordered_map<std::string, size_t> votes;
    const size_t n = std::min(reads.size(), kHeaderVoteReads);
    for (size_t i = 0; i < n; ++i) {
        std::string inst = instrumentOf(reads[i].name);
        if (!inst.empty()) ++votes[inst];
    }
    std::string best;
    size_t bestCount = 0;
    for (const auto& kv : votes) {
        if (kv.second > bestCount) { bestCount = kv.second; best = kv.first; }
    }
    return best;
}

// Per-tile analysis is only worth enabling when essentially every header in the
// run carries a parseable tile, not just the first one.
bool tileHeadersUsable(const std::vector<Read>& reads) {
    const size_t n = std::min(reads.size(), kHeaderVoteReads);
    if (n == 0) return false;
    size_t ok = 0;
    std::string lane;
    int tile = 0;
    for (size_t i = 0; i < n; ++i) {
        if (TileAnalyzer::parseTile(reads[i].name, lane, tile)) ++ok;
    }
    return static_cast<double>(ok) >= 0.9 * static_cast<double>(n);
}

std::vector<Read> drawSample(const std::string& path, bool fromStdin, const Options& opt,
                             SampleInfo& info) {
    const size_t want = opt.sampling.reads > 0 ? static_cast<size_t>(opt.sampling.reads) : 200000;
    if (opt.sampling.headOnly || fromStdin) {
        util::Timer timer;
        std::vector<Read> v = peekReads(path, want, fromStdin);
        info = SampleInfo{};
        info.records = v.size();
        info.scanned = static_cast<long long>(v.size());
        info.seconds = timer.elapsed();
        return v;
    }
    return sampleReads(path, want, false, &info);
}

}  // namespace

DetectionResult detectInputProperties(const Options& opt) {
    DetectionResult res;

    std::vector<Read> s1, s2;
    SampleInfo i1, i2;
    // Sampling both mates concurrently halves startup on paired input.
    std::thread peek2;
    if (!opt.in2.empty()) {
        peek2 = std::thread([&] { s2 = drawSample(opt.in2, false, opt, i2); });
    }
    s1 = drawSample(opt.in1, opt.stdinInput, opt, i1);
    if (peek2.joinable()) peek2.join();

    res.sampleStrategy = i1.strategyName();
    res.sampleReads1 = i1.records;
    res.sampleReads2 = i2.records;
    res.sampleSeconds = std::max(i1.seconds, i2.seconds);
    res.sampleScanned = i1.scanned;
    res.sampleExactCount = i1.exactCount;

    if (s1.empty()) return res;

    for (const Read& r : s1) res.readLength1 = std::max(res.readLength1, static_cast<int>(r.seq.size()));
    for (const Read& r : s2) res.readLength2 = std::max(res.readLength2, static_cast<int>(r.seq.size()));

    res.instrument = votedInstrument(s1);
    res.platform = isTwoColourInstrument(res.instrument) ? "Illumina (two-colour)" : "Illumina / generic";

    // Phred-64 shows quality characters above 'J' and never below ';'.
    int minQ = 127, maxQ = 0;
    size_t inspected = 0;
    for (const Read& r : s1) {
        for (char c : r.qual) {
            minQ = std::min(minQ, static_cast<int>(static_cast<unsigned char>(c)));
            maxQ = std::max(maxQ, static_cast<int>(static_cast<unsigned char>(c)));
        }
        if (++inspected > 10000) break;
    }
    // Phred+64 encodings start at ASCII 64 and in practice never go below 'B'
    // (59 with the Illumina 1.5 'B' tail), so a high floor is the usual signal.
    // On its own it is not enough: a Phred+33 file whose calls are all Q26 and
    // above -- recalibrated, consensus or HiFi-style data -- has exactly the
    // same floor, and rebasing it by 31 drives every read below the quality
    // filter and empties the output. Phred+64 also cannot produce characters
    // below 64 except in that narrow 'B' tail, so requiring the low band to be
    // either absent or confined to 'B' separates the two.
    const bool highFloor = (minQ >= 59 && maxQ > 74);
    bool lowBandIsBTail = true;
    if (highFloor && minQ < 64) {
        // Anything in [59, 64) must be the 'B' (66-64=2) read-segment marker;
        // a genuine Phred+33 file spreads its low band across the range.
        size_t inLowBand = 0, atB = 0, seen = 0;
        for (const Read& r : s1) {
            for (char c : r.qual) {
                const int v = static_cast<int>(static_cast<unsigned char>(c));
                if (v < 64) { ++inLowBand; if (v == 'B') ++atB; }
            }
            if (++seen > 10000) break;
        }
        lowBandIsBTail = inLowBand == 0 || atB * 10 >= inLowBand * 9;
    }
    res.phred64Detected = highFloor && lowBandIsBTail;

    // Mean quality and GC of the sample itself: these say what the detector
    // actually looked at, which is the difference between head and whole-file
    // sampling on a run whose quality drifts across the flow cell.
    {
        const int offset = res.phred64Detected ? 64 : 33;
        uint64_t bases = 0, gc = 0, qsum = 0;
        for (const Read& r : s1) {
            bases += r.seq.size();
            for (char c : r.seq) if (c == 'G' || c == 'C' || c == 'g' || c == 'c') ++gc;
            for (char c : r.qual) {
                int q = static_cast<int>(static_cast<unsigned char>(c)) - offset;
                if (q > 0) qsum += static_cast<uint64_t>(q);
            }
        }
        if (bases > 0) {
            res.sampleMeanQuality = static_cast<double>(qsum) / static_cast<double>(bases);
            res.sampleGcFraction = static_cast<double>(gc) / static_cast<double>(bases);
        }
    }

    res.tileHeadersUsable = tileHeadersUsable(s1);

    // Poly-G trimming is only ever correct on two-colour chemistry, where G is
    // the absence of signal rather than a base call. On a four-colour
    // instrument a poly-G run is real sequence, and trimming it silently
    // deletes data. Enabling this because the data merely *looks* G-rich was
    // wrong for exactly that reason -- and it diverged from fastp, which keys
    // the default purely off the instrument. The observed fraction is still
    // measured and reported, so a surprising value is visible without acting
    // on it; -g forces trimming on regardless.
    res.polyGTailFraction = polyGTailFraction(s1, 10);
    res.polyGRecommended = isTwoColourInstrument(res.instrument);

    // Adapters: prefer a hit against the known list, since inference from a
    // sample is far less reliable than a match to a real adapter. The costly
    // de novo search only runs when the result will actually be used for
    // trimming -- paired-end data without --detect_adapter_for_pe is trimmed by
    // overlap analysis, which needs no adapter sequence at all.
    const bool deNovoWanted = !opt.isPaired() || opt.adapter.detectForPE;
    std::string name1, name2;

    std::thread detect2;
    if (!s2.empty()) {
        detect2 = std::thread([&] {
            res.adapter2 = detectFromKnownList(s2, name2);
            if (res.adapter2.empty() && deNovoWanted) res.adapter2 = detectAdapterDeNovo(s2);
            res.adapter2Detected = !res.adapter2.empty();
        });
    }
    res.adapter1 = detectFromKnownList(s1, name1);
    if (res.adapter1.empty() && deNovoWanted) res.adapter1 = detectAdapterDeNovo(s1);
    res.adapter1Detected = !res.adapter1.empty();
    if (detect2.joinable()) detect2.join();

    // A full sampling pass counted every record, so --split sizing and the
    // progress bar get the true number instead of an extrapolation from the head.
    if (i1.exactCount && i1.scanned > 0) {
        res.estimatedReads = i1.scanned;
    } else if (!opt.stdinInput) {
        res.estimatedReads = estimateRecordCount(opt.in1);
    }

    if (opt.contam.enabled) {
        res.sample1 = std::move(s1);
        res.sample2 = std::move(s2);
    }
    return res;
}

}  // namespace fp
