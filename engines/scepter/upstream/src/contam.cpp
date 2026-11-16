#include "contam.h"

#include <algorithm>
#include <cmath>
#include <cstring>

#include <zlib.h>

#include "util.h"

namespace fp {

namespace {

// A species must light up this many distinct sketch k-mers before it is
// believed at all: a handful of hits is what a relative outside the panel
// produces, and 50 of 20000 is 0.25% of a species budget.
constexpr uint64_t kMinMatchedKmers = 50;
// Below this the sample simply does not contain enough panel signal to say
// anything, whatever the ratios between species look like.
constexpr uint64_t kMinTotalHits = 200;
constexpr double kMinDominantLibraryFraction = 0.10;
constexpr double kPureDominant = 0.95;
constexpr double kPureOther = 0.01;
constexpr double kMinorDominant = 0.80;
// Reads screened; more than this adds precision the verdict does not need.
constexpr size_t kMaxScreenReads = 200000;

constexpr char kMagic[8] = {'F', 'P', 'C', 'O', 'N', 'T', 'M', '1'};

inline uint64_t mix64(uint64_t x) {
    x += 0x9E3779B97F4A7C15ull;
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ull;
    x = (x ^ (x >> 27)) * 0x94D049BB133111EBull;
    return x ^ (x >> 31);
}

inline uint8_t baseCode(char c) {
    switch (c) {
        case 'A': case 'a': return 0;
        case 'C': case 'c': return 1;
        case 'G': case 'g': return 2;
        case 'T': case 't': return 3;
        default: return 4;
    }
}

// ---- little-endian readers over a gzip stream ------------------------------

class GzReader {
public:
    explicit GzReader(gzFile g) : g_(g) {}

    bool bytes(void* dst, size_t n) {
        if (failed_) return false;
        size_t done = 0;
        while (done < n) {
            const int got = gzread(g_, static_cast<char*>(dst) + done,
                                   static_cast<unsigned>(n - done));
            if (got <= 0) { failed_ = true; return false; }
            done += static_cast<size_t>(got);
        }
        return true;
    }

    bool u8(uint8_t& v) { return bytes(&v, 1); }

    bool u16(uint16_t& v) {
        uint8_t b[2];
        if (!bytes(b, 2)) return false;
        v = static_cast<uint16_t>(b[0] | (b[1] << 8));
        return true;
    }

    bool u32(uint32_t& v) {
        uint8_t b[4];
        if (!bytes(b, 4)) return false;
        v = static_cast<uint32_t>(b[0]) | (static_cast<uint32_t>(b[1]) << 8) |
            (static_cast<uint32_t>(b[2]) << 16) | (static_cast<uint32_t>(b[3]) << 24);
        return true;
    }

    bool u64(uint64_t& v) {
        uint8_t b[8];
        if (!bytes(b, 8)) return false;
        v = 0;
        for (int i = 7; i >= 0; --i) v = (v << 8) | b[static_cast<size_t>(i)];
        return true;
    }

    // LEB128, used for the delta-encoded hash lists.
    bool varint(uint64_t& v) {
        v = 0;
        int shift = 0;
        while (shift < 70) {
            uint8_t b = 0;
            if (!u8(b)) return false;
            v |= static_cast<uint64_t>(b & 0x7F) << shift;
            if ((b & 0x80) == 0) return true;
            shift += 7;
        }
        failed_ = true;
        return false;
    }

    bool failed() const { return failed_; }

private:
    gzFile g_;
    bool failed_ = false;
};

size_t nextPowerOfTwo(size_t n) {
    size_t p = 1;
    while (p < n) p <<= 1;
    return p;
}

}  // namespace

const std::vector<PanelEntry>& contamPanel() {
    // '*' in the task's species list marks the usual skin/reagent passengers;
    // they are screened for like everything else but flagged in the report so a
    // small amount of them is not read as a mixed culture.
    static const std::vector<PanelEntry> kPanel = {
        {"Staphylococcus aureus", false},
        {"Staphylococcus epidermidis", true},
        {"Escherichia coli", false},
        {"Klebsiella pneumoniae", false},
        {"Pseudomonas aeruginosa", false},
        {"Acinetobacter baumannii", false},
        {"Enterobacter cloacae", false},
        {"Proteus mirabilis", false},
        {"Salmonella enterica", false},
        {"Serratia marcescens", false},
        {"Streptococcus pneumoniae", false},
        {"Streptococcus pyogenes", false},
        {"Streptococcus agalactiae", false},
        {"Enterococcus faecalis", false},
        {"Enterococcus faecium", false},
        {"Haemophilus influenzae", false},
        {"Neisseria meningitidis", false},
        {"Neisseria gonorrhoeae", false},
        {"Listeria monocytogenes", false},
        {"Clostridioides difficile", false},
        {"Campylobacter jejuni", false},
        {"Cutibacterium acnes", true},
        {"Mycobacterium tuberculosis", false},
    };
    return kPanel;
}

// ---------------------------------------------------------------------------
// ContamDb
// ---------------------------------------------------------------------------

bool ContamDb::load(const std::string& path, std::string& error) {
    loaded_ = false;
    species_.clear();
    keys_.clear();
    slots_.clear();

    gzFile g = gzopen(path.c_str(), "rb");
    if (!g) {
        error = "cannot open contamination database " + path;
        return false;
    }
    gzbuffer(g, 1u << 20);
    GzReader in(g);

    char magic[8] = {0};
    uint32_t k = 0, nSpecies = 0;
    if (!in.bytes(magic, 8) || std::memcmp(magic, kMagic, 8) != 0) {
        gzclose(g);
        error = path + ": not a scepter contamination database";
        return false;
    }
    if (!in.u32(k) || !in.u32(nSpecies) || nSpecies == 0 || nSpecies > 4096) {
        gzclose(g);
        error = path + ": corrupt header";
        return false;
    }
    k_ = static_cast<int>(k);
    if (k_ < 8 || k_ > 31) {
        gzclose(g);
        error = path + ": unsupported k=" + std::to_string(k_);
        return false;
    }

    std::vector<std::vector<uint64_t>> hashes(nSpecies);
    size_t total = 0;
    for (uint32_t s = 0; s < nSpecies; ++s) {
        ContamSketch sk;
        uint16_t nameLen = 0;
        uint8_t flags = 0;
        uint64_t genomeLength = 0;
        if (!in.u16(nameLen) || nameLen == 0 || nameLen > 512) { break; }
        std::string name(nameLen, '\0');
        if (!in.bytes(&name[0], nameLen)) break;
        if (!in.u8(flags) || !in.u64(genomeLength) || !in.u64(sk.genomeKmers) ||
            !in.u64(sk.uniqueKmers) || !in.u64(sk.maxHash)) {
            break;
        }
        uint32_t count = 0;
        if (!in.u32(count) || count > (1u << 24)) break;
        sk.name = name;
        sk.commonContaminant = (flags & 1u) != 0;
        sk.genomeLength = static_cast<long long>(genomeLength);
        sk.count = count;

        hashes[s].reserve(count);
        uint64_t prev = 0;
        bool ok = true;
        for (uint32_t i = 0; i < count; ++i) {
            uint64_t delta = 0;
            if (!in.varint(delta)) { ok = false; break; }
            prev += delta;
            if (prev != 0) hashes[s].push_back(prev);   // 0 is the empty-slot sentinel
        }
        if (!ok) break;
        total += hashes[s].size();
        species_.push_back(std::move(sk));
    }
    gzclose(g);

    if (species_.size() != nSpecies || total == 0) {
        error = path + ": truncated or corrupt species table";
        species_.clear();
        return false;
    }

    // Open addressing at <= 50% load: one cache miss per lookup, no chasing.
    const size_t slots = nextPowerOfTwo(total * 2 + 1);
    keys_.assign(slots, kEmpty);
    slots_.assign(slots, 0);
    mask_ = slots - 1;
    for (size_t s = 0; s < species_.size(); ++s) {
        for (uint64_t h : hashes[s]) {
            size_t i = static_cast<size_t>(h) & mask_;
            while (keys_[i] != kEmpty && keys_[i] != h) i = (i + 1) & mask_;
            keys_[i] = h;
            slots_[i] = static_cast<uint16_t>(s);
        }
        species_[s].count = static_cast<uint32_t>(hashes[s].size());
    }

    path_ = path;
    loaded_ = true;
    return true;
}

std::string findContamDb(const std::string& programDir) {
    std::vector<std::string> candidates;
    if (!programDir.empty()) {
        candidates.push_back(programDir + "/data/contam_db.bin.gz");
        candidates.push_back(programDir + "/../data/contam_db.bin.gz");
        candidates.push_back(programDir + "/../share/scepter/contam_db.bin.gz");
    }
    candidates.push_back("data/contam_db.bin.gz");
    candidates.push_back("/usr/local/share/scepter/contam_db.bin.gz");
    candidates.push_back("/usr/share/scepter/contam_db.bin.gz");
    for (const std::string& c : candidates) {
        if (util::fileExists(c)) return c;
    }
    return "";
}

// ---------------------------------------------------------------------------
// screenContamination
// ---------------------------------------------------------------------------

namespace {

void countRead(const Read& r, const ContamDb& db, int k, std::vector<uint64_t>& hits,
               std::vector<uint64_t>& distinct, std::vector<bool>& seenSlot, uint64_t& kmers) {
    const size_t len = r.seq.size();
    if (len < static_cast<size_t>(k)) return;
    const uint64_t mask = (1ull << (2 * k)) - 1;
    const int topShift = 2 * (k - 1);
    uint64_t fwd = 0, rev = 0;
    int run = 0;
    for (size_t i = 0; i < len; ++i) {
        const uint8_t b = baseCode(r.seq[i]);
        if (b > 3) { run = 0; fwd = 0; rev = 0; continue; }
        fwd = ((fwd << 2) | b) & mask;
        rev = (rev >> 2) | (static_cast<uint64_t>(3 - b) << topShift);
        if (++run < k) continue;
        ++kmers;
        const uint64_t canonical = fwd < rev ? fwd : rev;
        const size_t slot = db.find(mix64(canonical));
        if (slot == ContamDb::kNoSlot) continue;
        const size_t s = db.speciesAt(slot);
        ++hits[s];
        if (!seenSlot[slot]) { seenSlot[slot] = true; ++distinct[s]; }
    }
}

}  // namespace

ContamResult screenContamination(const Options& opt, const std::vector<Read>& sample1,
                                 const std::vector<Read>& sample2, double libraryBases) {
    ContamResult res;
    res.attempted = true;
    res.minFraction = opt.contam.minFraction;

    std::string dbPath = opt.contam.dbPath;
    if (dbPath.empty()) dbPath = findContamDb(opt.programDir);
    if (dbPath.empty()) {
        res.message =
            "no contamination database found; build one with tools/build_contam_db.py "
            "or pass --contam-db PATH";
        return res;
    }

    ContamDb db;
    std::string error;
    if (!db.load(dbPath, error)) {
        res.message = error;
        return res;
    }
    res.dbPath = dbPath;
    res.k = db.k();

    const size_t nSpecies = db.species().size();
    std::vector<uint64_t> hits(nSpecies, 0), distinct(nSpecies, 0);
    std::vector<bool> seenSlot(db.tableSize(), false);

    uint64_t kmers = 0, reads = 0;
    for (const std::vector<Read>* side : {&sample1, &sample2}) {
        for (const Read& r : *side) {
            if (reads >= kMaxScreenReads) break;
            countRead(r, db, db.k(), hits, distinct, seenSlot, kmers);
            ++reads;
        }
    }
    res.readsScreened = reads;
    res.kmersScreened = kmers;
    if (kmers == 0) {
        // Distinguish "nothing to read" from "read plenty, none of it usable":
        // a library of all-N or very short reads yields no k-mers at all, and
        // blaming the input stream for that is actively misleading.
        res.message = reads == 0
                          ? "no reads were available to sample; the screen needs a seekable input"
                          : "no k-mers could be taken from the sampled reads; they may be shorter "
                            "than the screening k, or masked";
        return res;
    }

    uint64_t totalHits = 0;
    for (uint64_t h : hits) totalHits += h;
    res.totalHits = totalHits;

    // Turn hits into an absolute share of the library. If species s makes up a
    // fraction phi of the reads, the number of its k-mers that can land in the
    // sketch at all is (all k-mers) * phi * (unique fraction of its genome) *
    // (FracMinHash fraction the sketch was cut at), so phi inverts directly.
    std::vector<double> phi(nSpecies, 0.0);
    double phiSum = 0.0;
    for (size_t s = 0; s < nSpecies; ++s) {
        const ContamSketch& sk = db.species()[s];
        if (hits[s] == 0 || kmers == 0 || sk.genomeKmers == 0 || sk.uniqueKmers == 0 ||
            sk.maxHash == 0) {
            continue;
        }
        const double uniqueFrac =
            static_cast<double>(sk.uniqueKmers) / static_cast<double>(sk.genomeKmers);
        const double keptFrac = static_cast<double>(sk.maxHash) / 18446744073709551616.0;
        const double denom = static_cast<double>(kmers) * uniqueFrac * keptFrac;
        if (denom <= 0) continue;
        phi[s] = static_cast<double>(hits[s]) / denom;
        phiSum += phi[s];
    }

    // The noise floor is what the per-species budget can actually resolve: a
    // species needs kMinMatchedKmers of its stored k-mers before it counts, so
    // anything under that share of the budget is indistinguishable from noise.
    uint32_t budget = 1;
    for (const ContamSketch& sk : db.species()) budget = std::max(budget, sk.count);
    res.noiseFloor = std::max(opt.contam.minFraction,
                              static_cast<double>(kMinMatchedKmers) / static_cast<double>(budget));

    res.species.resize(nSpecies);
    for (size_t s = 0; s < nSpecies; ++s) {
        const ContamSketch& sk = db.species()[s];
        ContamSpeciesResult& r = res.species[s];
        r.name = sk.name;
        r.commonContaminant = sk.commonContaminant;
        r.sketchSize = sk.count;
        r.hits = hits[s];
        r.genomeLength = sk.genomeLength;
        r.matchedKmers = distinct[s];
        r.libraryFraction = phi[s];
        r.relativeAbundance = phiSum > 0 ? phi[s] / phiSum : 0.0;
        if (libraryBases > 0 && sk.genomeLength > 0) {
            r.estimatedDepth = phi[s] * libraryBases / static_cast<double>(sk.genomeLength);
        }
        r.present = r.matchedKmers >= kMinMatchedKmers && r.relativeAbundance >= res.noiseFloor;
    }

    std::sort(res.species.begin(), res.species.end(),
              [](const ContamSpeciesResult& a, const ContamSpeciesResult& b) {
                  return a.relativeAbundance > b.relativeAbundance;
              });

    const ContamSpeciesResult* top = nullptr;
    for (const ContamSpeciesResult& r : res.species) {
        if (r.present) { top = &r; break; }
    }

    res.ok = true;
    if (totalHits < kMinTotalHits || top == nullptr ||
        top->libraryFraction < kMinDominantLibraryFraction) {
        res.verdict = "no panel species detected";
        return res;
    }

    res.dominant = top->name;
    res.dominantFraction = top->relativeAbundance;
    for (const ContamSpeciesResult& r : res.species) {
        if (&r == top || !r.present) continue;
        res.topContaminant = r.name;
        res.topContaminantFraction = r.relativeAbundance;
        break;
    }

    bool othersClean = true;
    for (const ContamSpeciesResult& r : res.species) {
        if (&r == top) continue;
        if (r.present && r.relativeAbundance >= kPureOther) { othersClean = false; break; }
    }

    if (top->relativeAbundance >= kPureDominant && othersClean) res.verdict = "pure";
    else if (top->relativeAbundance >= kMinorDominant) res.verdict = "minor contamination";
    else res.verdict = "mixed";
    return res;
}

}  // namespace fp
