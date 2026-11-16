// Optional clinical contamination screen.
//
// Answers "is this isolate pure, and if not, what else is in it?" for a fixed
// panel of clinically relevant bacteria. The panel is shipped as a compact
// FracMinHash sketch of canonical 31-mers that are unique to a single species
// *within the panel*; k-mers shared by two or more panel members are dropped at
// build time, which is what makes a hit specific rather than merely similar.
//
// The screen runs on the same whole-file sample the auto-detector already drew
// (see sampleReads), so it costs no extra I/O.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "common.h"
#include "options.h"
#include "stats.h"

namespace fp {

// One species sketch as stored in the database.
struct ContamSketch {
    std::string name;
    bool commonContaminant = false;
    long long genomeLength = 0;
    uint64_t genomeKmers = 0;    // distinct canonical k-mers in the genome (estimated)
    uint64_t uniqueKmers = 0;    // of those, the ones unique within the panel (estimated)
    uint64_t maxHash = 0;        // FracMinHash threshold this sketch was cut at
    uint32_t count = 0;          // k-mers actually stored
};

// The shipped panel, loaded into a flat open-addressed hash -> species table.
class ContamDb {
public:
    bool load(const std::string& path, std::string& error);

    bool loaded() const { return loaded_; }
    int k() const { return k_; }
    const std::vector<ContamSketch>& species() const { return species_; }
    const std::string& path() const { return path_; }

    static constexpr size_t kNoSlot = static_cast<size_t>(-1);

    // Table slot holding `hash`, or kNoSlot. The caller keeps the slot so that
    // "distinct k-mers matched" can be counted exactly, without a second hash.
    size_t find(uint64_t hash) const {
        if (keys_.empty()) return kNoSlot;
        size_t i = static_cast<size_t>(hash) & mask_;
        while (true) {
            const uint64_t key = keys_[i];
            if (key == kEmpty) return kNoSlot;
            if (key == hash) return i;
            i = (i + 1) & mask_;
        }
    }

    // Every stored hash belongs to exactly one species by construction.
    uint16_t speciesAt(size_t slot) const { return slots_[slot]; }
    size_t tableSize() const { return keys_.size(); }

private:
    static constexpr uint64_t kEmpty = 0;   // hash 0 is dropped at load time

    bool loaded_ = false;
    int k_ = 31;
    std::string path_;
    std::vector<ContamSketch> species_;
    std::vector<uint64_t> keys_;
    std::vector<uint16_t> slots_;
    size_t mask_ = 0;
};

// Where the shipped database lives, searched relative to the running binary and
// then in the usual install locations. Empty when nothing was found.
std::string findContamDb(const std::string& programDir);

// Run the screen over a sample of reads. `libraryBases` is the estimated total
// base count of the whole library, used to turn a fraction into a depth; pass 0
// when it is unknown.
ContamResult screenContamination(const Options& opt, const std::vector<Read>& sample1,
                                 const std::vector<Read>& sample2, double libraryBases);

// The panel as compiled in, so --help and the README can list it even when the
// database file is missing.
struct PanelEntry {
    const char* name;
    bool commonContaminant;
};
const std::vector<PanelEntry>& contamPanel();

}  // namespace fp
