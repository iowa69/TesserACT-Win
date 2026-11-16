// FASTQ input and output, including transparent gzip and split-file handling.
#pragma once

#include <atomic>
#include <cstdio>
#include <memory>
#include <string>
#include <vector>

#include <zlib.h>

#include "common.h"
#include "options.h"

namespace fp {

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

// Buffered reader over a plain, gzipped, or stdin FASTQ stream. Records are
// parsed by scanning a large block for newlines rather than per-line calls.
class FastqInput {
public:
    explicit FastqInput(const std::string& path, bool fromStdin = false);
    ~FastqInput();

    FastqInput(const FastqInput&) = delete;
    FastqInput& operator=(const FastqInput&) = delete;

    bool ok() const { return ok_; }
    const std::string& error() const { return error_; }
    const std::string& path() const { return path_; }

    // Reads the next record. Returns false at end of stream.
    bool next(Read& out);

    // Bytes handed back by the decompressor so far (throughput reporting).
    long long bytesRead() const { return bytesRead_; }

    // Position in the *compressed* stream, i.e. comparable to the file size.
    // Sampled by the reader thread and published here, because the progress
    // display polls it from the writer thread.
    long long compressedOffset() const { return compressedOffset_.load(std::memory_order_relaxed); }

private:
    bool fill();                 // refill buf_ from the stream
    bool nextLine(std::string& line);

    std::string path_;
    bool gzipped_ = false;
    bool stdin_ = false;
    bool ok_ = false;
    bool eof_ = false;
    std::string error_;

    gzFile gz_ = nullptr;
    FILE* fh_ = nullptr;

    std::vector<char> buf_;
    size_t bufPos_ = 0;
    size_t bufLen_ = 0;
    long long bytesRead_ = 0;
    std::atomic<long long> compressedOffset_{0};
    long long recordNo_ = 0;
    std::string header_;   // reused across records to avoid reallocating
};

// Yields Chunks from either a single stream, a pair of streams, or one
// interleaved stream. Not thread-safe; intended to be driven by one thread.
class ChunkSource {
public:
    ChunkSource(const std::string& in1, const std::string& in2,
                bool interleaved, bool fromStdin, size_t chunkReads);

    bool ok() const { return ok_; }
    const std::string& error() const { return error_; }

    // Fills `chunk`. Returns false when the input is exhausted.
    bool next(Chunk& chunk);

    long long bytesRead() const;
    // Compressed bytes consumed across every input; comparable to totalBytes().
    long long compressedBytes() const;
    long long totalBytes() const { return totalBytes_; }

    // Stop after this many reads/pairs (0 = unlimited).
    void setLimit(long long limit) { limit_ = limit; }

private:
    std::unique_ptr<FastqInput> a_, b_;
    bool interleaved_ = false;
    bool paired_ = false;
    size_t chunkReads_;
    uint64_t index_ = 0;
    long long produced_ = 0;
    long long limit_ = 0;
    long long totalBytes_ = 0;
    bool ok_ = false;
    std::string error_;
};

// ---------------------------------------------------------------------------
// Output
// ---------------------------------------------------------------------------

// Compresses a buffer into a self-contained gzip member. Concatenated members
// form a valid gzip file (RFC 1952), which is what lets workers compress in
// parallel while the writer only has to append bytes in order.
class GzipMemberCompressor {
public:
    explicit GzipMemberCompressor(int level);
    ~GzipMemberCompressor();
    GzipMemberCompressor(const GzipMemberCompressor&) = delete;
    GzipMemberCompressor& operator=(const GzipMemberCompressor&) = delete;

    // Appends the compressed form of `in` to `out`.
    void compress(const std::string& in, std::string& out);

private:
    z_stream zs_{};
    bool inited_ = false;
    int level_;
    std::vector<char> scratch_;
};

// A single output destination, optionally gzipped, optionally split across
// numbered files. Only the writer thread touches this.
class OutputFile {
public:
    // `path` may be empty (the sink is then a no-op) or "-" for stdout.
    OutputFile(const std::string& path, int compressionLevel, const SplitOptions* split);
    ~OutputFile();

    OutputFile(const OutputFile&) = delete;
    OutputFile& operator=(const OutputFile&) = delete;

    bool active() const { return active_; }
    bool ok() const { return ok_; }
    const std::string& error() const { return error_; }

    // Append already-compressed gzip member bytes (or raw bytes when the file
    // is not gzipped). `records` is used for split accounting.
    void appendPrepared(const std::string& data, size_t records);

    // Append uncompressed text; compresses here when the target is gzipped.
    void appendRaw(const std::string& data, size_t records);

    // True when the caller may pre-compress. False in split mode, where the
    // target file is not known until the writer sees the chunk.
    bool allowsPrecompression() const { return gzipped_ && !splitting_; }
    bool gzipped() const { return gzipped_; }

    void close();

    // Split mode: how many records go into each file before rolling over.
    void setSplitTarget(long long recordsPerFile) { splitTarget_ = recordsPerFile; }

    const std::vector<std::string>& writtenPaths() const { return writtenPaths_; }
    long long recordsWritten() const { return totalRecords_; }
    long long bytesWritten() const { return totalBytes_; }

private:
    bool openCurrent();
    void rollIfNeeded(size_t incoming);
    std::string splitPathFor(int index) const;

    std::string path_;
    bool active_ = false;
    bool ok_ = true;
    bool gzipped_ = false;
    bool toStdout_ = false;
    bool splitting_ = false;
    int level_;
    std::string error_;

    FILE* fh_ = nullptr;
    std::unique_ptr<GzipMemberCompressor> comp_;

    const SplitOptions* split_ = nullptr;
    int splitIndex_ = 1;
    long long splitTarget_ = 0;
    long long splitRecords_ = 0;

    long long totalRecords_ = 0;
    long long totalBytes_ = 0;
    std::vector<std::string> writtenPaths_;
};

// Estimate the number of records in a FASTQ file from its size, by sampling the
// first records. Used to plan `--split` output and to size progress bars.
long long estimateRecordCount(const std::string& path, double* avgRecordBytes = nullptr);

// Peek at the first `n` records without consuming a stream, for auto-detection
// (adapter, phred offset, instrument type). Returns however many it could read.
std::vector<Read> peekReads(const std::string& path, size_t n, bool fromStdin = false);

// ---------------------------------------------------------------------------
// Whole-file sampling for auto-detection
// ---------------------------------------------------------------------------

// How a sample was obtained. Reported so the cost of sampling is visible and
// so the tests can assert which path ran.
struct SampleInfo {
    enum class Strategy { Head, WholeFile, Stratified, Reservoir };
    Strategy strategy = Strategy::Head;
    size_t records = 0;        // records returned
    long long scanned = 0;     // records the sampler looked at
    bool exactCount = false;   // true when `scanned` is the file's real record count
    int strata = 0;            // strata used (Stratified only)
    double seconds = 0;
    const char* strategyName() const;
};

// Strata used by the stratified path, and the record-boundary resynchronisation
// it depends on. Exposed for testing.
constexpr int kSampleStrata = 200;

// Offset of the first byte of a record whose complete 4-line shape validates, or
// std::string::npos. A quality line can also begin with '@', so a candidate is
// only accepted when the third line starts with '+' and the sequence and quality
// lines have equal, non-zero length.
size_t findRecordStart(const char* data, size_t len, bool atFileStart);

// Draw `target` records from across the whole file instead of only its head,
// so adapter content, quality, instrument and poly-G decisions are not taken
// from a single flow-cell corner.
//
// Plain files are seekable in O(1), so they are sampled at kSampleStrata evenly
// spaced byte offsets, each resynchronised to the next valid record.
//
// gzip files are not: zlib has to inflate from the start of the stream to reach
// any offset, so seeking to N strata costs N partial inflate passes (measured at
// 24 s for 200 strata on a 34 MB file whose full inflate takes 0.23 s). They are
// therefore sampled with a reservoir (Algorithm R) over one streaming pass,
// which costs a single inflate and returns a uniform sample of the whole file.
//
// stdin cannot be rewound at all; this returns an empty vector for it and the
// caller falls back to head sampling.
//
// The returned records are deterministically shuffled, so any prefix of the
// sample is itself representative of the whole file.
std::vector<Read> sampleReads(const std::string& path, size_t target,
                              bool fromStdin = false, SampleInfo* info = nullptr);

}  // namespace fp
