// Core data types shared by every stage of the pipeline.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace fp {

constexpr int kMaxCycles = 1000;   // per-cycle statistics are capped here
constexpr int kQualBins = 64;      // phred33 quality values 0..63
constexpr int kStatKmerK = 5;      // k for the 5-mer composition profile

// A single FASTQ record. Trimming rewrites seq/qual in place; `name` keeps the
// original header (without the leading '@') so UMI and tile parsing can use it.
struct Read {
    std::string name;
    std::string seq;
    std::string strand;  // the '+' line, preserved verbatim
    std::string qual;

    Read() = default;
    Read(std::string n, std::string s, std::string st, std::string q)
        : name(std::move(n)), seq(std::move(s)), strand(std::move(st)), qual(std::move(q)) {}

    size_t length() const { return seq.size(); }
    bool empty() const { return seq.empty(); }

    // Drop `n` bases from the 5' end.
    void trimFront(size_t n) {
        if (n == 0) return;
        if (n >= seq.size()) { seq.clear(); qual.clear(); return; }
        seq.erase(0, n);
        qual.erase(0, n);
    }

    // Keep only the first `len` bases.
    void truncate(size_t len) {
        if (len >= seq.size()) return;
        seq.resize(len);
        qual.resize(len);
    }

    // Reverse complement in place (quality is reversed to match).
    void reverseComplement();

    std::string toString() const {
        std::string out;
        out.reserve(name.size() + seq.size() * 2 + strand.size() + 8);
        out += '@'; out += name; out += '\n';
        out += seq;  out += '\n';
        out += strand.empty() ? "+" : strand; out += '\n';
        out += qual; out += '\n';
        return out;
    }
    void appendTo(std::string& out) const {
        out += '@'; out += name; out += '\n';
        out += seq;  out += '\n';
        out += strand.empty() ? "+" : strand; out += '\n';
        out += qual; out += '\n';
    }
};

// Why a read was discarded. Order matters: reported in this order.
enum class FilterResult : uint8_t {
    Pass = 0,
    FailedLength,
    FailedTooLong,
    FailedQuality,      // unqualified base percentage
    FailedNBase,
    FailedAverageQuality,
    FailedComplexity,
    FailedIndex,
    Duplicated,
    kCount
};

const char* filterResultName(FilterResult r);

// One unit of work moving through the pipeline. Chunks carry a monotonically
// increasing index so the writer can restore input order.
// Chunks are recycled through a pool rather than reallocated, so `r1`/`r2` stay
// at their high-water size and `n` records how many entries are live. Reusing
// the Read objects keeps their string buffers, which removes essentially all
// allocation from the reader's hot path.
struct Chunk {
    uint64_t index = 0;
    std::vector<Read> r1;
    std::vector<Read> r2;   // empty for single-end
    size_t n = 0;
    bool isPaired = false;
    bool last = false;

    // Filled by workers, consumed by the writer. When `compressed` is set the
    // worker has already deflated each buffer into a standalone gzip member and
    // the writer only has to append the bytes.
    std::string out1, out2;
    std::string outMerged;
    std::string outUnpaired1, outUnpaired2;
    std::string outFailed;
    std::string outOverlapped;
    bool compressed = false;

    size_t size() const { return n; }
    bool paired() const { return isPaired; }

    void clearOutputs() {
        out1.clear(); out2.clear(); outMerged.clear();
        outUnpaired1.clear(); outUnpaired2.clear();
        outFailed.clear(); outOverlapped.clear();
    }
};

// Result of comparing R1 against the reverse complement of R2.
struct OverlapResult {
    bool overlapped = false;
    int offset = 0;      // >=0: R2 starts `offset` bases into R1 (normal insert)
                         // <0 : adapter read-through, R1 extends past the insert
    int overlapLen = 0;
    int diff = 0;        // mismatched bases inside the overlap
    bool hasGap = false;
};

}  // namespace fp
