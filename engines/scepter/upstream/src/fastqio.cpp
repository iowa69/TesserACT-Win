#include "fastqio.h"

#include <algorithm>
#include <cstring>
#include <utility>

#include "util.h"

namespace fp {

namespace {
constexpr size_t kReadBuf = 4u << 20;   // 4 MB input block
constexpr size_t kGzWindow = 15 + 16;   // deflate window + gzip wrapper
}  // namespace

const char* complementTable() {
    static const char* table = [] {
        static char t[256];
        for (int i = 0; i < 256; ++i) t[i] = 'N';
        const char* from = "ACGTacgtUuRYSWKMBVDHryswkmbvdhNn";
        const char* to   = "TGCAtgcaAaYRSWMKVBHDyrswmkvbhdNn";
        for (size_t i = 0; from[i]; ++i) t[static_cast<unsigned char>(from[i])] = to[i];
        return t;
    }();
    return table;
}

void Read::reverseComplement() {
    const char* comp = complementTable();
    size_t n = seq.size();
    for (size_t i = 0; i < n / 2; ++i) {
        char a = seq[i], b = seq[n - 1 - i];
        seq[i] = comp[static_cast<unsigned char>(b)];
        seq[n - 1 - i] = comp[static_cast<unsigned char>(a)];
    }
    if (n % 2) seq[n / 2] = comp[static_cast<unsigned char>(seq[n / 2])];
    std::reverse(qual.begin(), qual.end());
}

const char* filterResultName(FilterResult r) {
    switch (r) {
        case FilterResult::Pass:                 return "passed";
        case FilterResult::FailedLength:         return "too_short";
        case FilterResult::FailedTooLong:        return "too_long";
        case FilterResult::FailedQuality:        return "low_quality";
        case FilterResult::FailedNBase:          return "too_many_N";
        case FilterResult::FailedAverageQuality: return "low_mean_quality";
        case FilterResult::FailedComplexity:     return "low_complexity";
        case FilterResult::FailedIndex:          return "filtered_by_index";
        case FilterResult::Duplicated:           return "duplicated";
        default:                                 return "unknown";
    }
}

// ---------------------------------------------------------------------------
// FastqInput
// ---------------------------------------------------------------------------

FastqInput::FastqInput(const std::string& path, bool fromStdin)
    : path_(path), stdin_(fromStdin) {
    buf_.resize(kReadBuf);
    if (fromStdin) {
        gzipped_ = true;   // gzdopen transparently handles uncompressed input
        gz_ = gzdopen(fileno(stdin), "rb");
        if (!gz_) { error_ = "cannot read from stdin"; return; }
        gzbuffer(gz_, 1u << 20);
        ok_ = true;
        return;
    }
    if (!util::fileExists(path)) {
        error_ = "file not found: " + path;
        return;
    }
    // zlib reads plain files transparently, so a single path covers both.
    gz_ = gzopen(path.c_str(), "rb");
    if (!gz_) { error_ = "cannot open " + path; return; }
    gzbuffer(gz_, 1u << 20);
    gzipped_ = true;
    ok_ = true;
}

FastqInput::~FastqInput() {
    if (gz_) gzclose(gz_);
    if (fh_) std::fclose(fh_);
}

bool FastqInput::fill() {
    if (eof_) return false;
    // Preserve the unconsumed tail (a partial line) at the front of the buffer.
    size_t remain = bufLen_ - bufPos_;
    if (remain > 0 && bufPos_ > 0) std::memmove(buf_.data(), buf_.data() + bufPos_, remain);
    bufPos_ = 0;
    bufLen_ = remain;

    if (bufLen_ == buf_.size()) buf_.resize(buf_.size() * 2);  // pathological long line

    int n = gzread(gz_, buf_.data() + bufLen_, static_cast<unsigned>(buf_.size() - bufLen_));
    if (n < 0) {
        int err = 0;
        const char* msg = gzerror(gz_, &err);
        error_ = std::string("read error on ") + path_ + ": " + (msg ? msg : "unknown");
        eof_ = true;
        return false;
    }
    const z_off_t off = gzoffset(gz_);
    if (off >= 0) compressedOffset_.store(static_cast<long long>(off), std::memory_order_relaxed);
    if (n == 0) { eof_ = true; return bufLen_ > bufPos_; }
    bufLen_ += static_cast<size_t>(n);
    bytesRead_ += n;
    return true;
}

bool FastqInput::nextLine(std::string& line) {
    line.clear();
    while (true) {
        const char* base = buf_.data();
        const void* nl = std::memchr(base + bufPos_, '\n', bufLen_ - bufPos_);
        if (nl) {
            size_t pos = static_cast<const char*>(nl) - base;
            size_t len = pos - bufPos_;
            if (len > 0 && base[pos - 1] == '\r') --len;
            line.append(base + bufPos_, len);
            bufPos_ = pos + 1;
            return true;
        }
        // No newline in the buffer: keep the tail and pull more data.
        if (eof_) {
            if (bufPos_ < bufLen_) {
                size_t len = bufLen_ - bufPos_;
                if (len > 0 && base[bufLen_ - 1] == '\r') --len;
                line.append(base + bufPos_, len);
                bufPos_ = bufLen_;
                return !line.empty();
            }
            return false;
        }
        size_t remain = bufLen_ - bufPos_;
        if (remain > 0) {
            line.append(base + bufPos_, remain);
            bufPos_ = bufLen_;
        }
        if (!fill()) {
            return !line.empty();
        }
    }
}

bool FastqInput::next(Read& out) {
    if (!ok_) return false;
    // Skip blank lines between records.
    do {
        if (!nextLine(header_)) return false;
    } while (header_.empty());

    if (header_[0] != '@') {
        error_ = path_ + ": malformed record " + std::to_string(recordNo_ + 1) +
                 " (expected '@', got '" + header_.substr(0, 1) + "')";
        ok_ = false;
        return false;
    }
    out.name.assign(header_, 1, std::string::npos);
    if (!nextLine(out.seq)) { error_ = path_ + ": truncated record (missing sequence)"; ok_ = false; return false; }
    if (!nextLine(out.strand)) { error_ = path_ + ": truncated record (missing '+' line)"; ok_ = false; return false; }
    if (!nextLine(out.qual)) { error_ = path_ + ": truncated record (missing quality)"; ok_ = false; return false; }

    if (out.seq.size() != out.qual.size()) {
        error_ = path_ + ": record '" + out.name + "' has sequence length " +
                 std::to_string(out.seq.size()) + " but quality length " +
                 std::to_string(out.qual.size());
        ok_ = false;
        return false;
    }
    ++recordNo_;
    return true;
}

// ---------------------------------------------------------------------------
// ChunkSource
// ---------------------------------------------------------------------------

ChunkSource::ChunkSource(const std::string& in1, const std::string& in2,
                         bool interleaved, bool fromStdin, size_t chunkReads)
    : interleaved_(interleaved), chunkReads_(chunkReads) {
    a_ = std::make_unique<FastqInput>(in1, fromStdin);
    if (!a_->ok()) { error_ = a_->error(); return; }
    totalBytes_ = fromStdin ? 0 : util::fileSize(in1);
    if (!in2.empty()) {
        b_ = std::make_unique<FastqInput>(in2, false);
        if (!b_->ok()) { error_ = b_->error(); return; }
        paired_ = true;
        long long s2 = util::fileSize(in2);
        if (s2 > 0 && totalBytes_ > 0) totalBytes_ += s2;
    } else if (interleaved) {
        paired_ = true;
    }
    ok_ = true;
}

long long ChunkSource::bytesRead() const {
    long long n = a_ ? a_->bytesRead() : 0;
    if (b_) n += b_->bytesRead();
    return n;
}

long long ChunkSource::compressedBytes() const {
    long long n = a_ ? a_->compressedOffset() : 0;
    if (b_) n += b_->compressedOffset();
    return n;
}

bool ChunkSource::next(Chunk& chunk) {
    if (!ok_) return false;
    chunk.clearOutputs();
    chunk.index = index_;
    chunk.last = false;
    chunk.n = 0;
    chunk.isPaired = paired_;

    size_t want = chunkReads_;
    if (limit_ > 0) {
        long long left = limit_ - produced_;
        if (left <= 0) return false;
        if (static_cast<long long>(want) > left) want = static_cast<size_t>(left);
    }

    // Grow only; the Read objects already present keep their string capacity.
    if (chunk.r1.size() < want) chunk.r1.resize(want);
    if (paired_ && chunk.r2.size() < want) chunk.r2.resize(want);

    size_t got = 0;
    while (got < want) {
        if (!a_->next(chunk.r1[got])) break;
        if (paired_) {
            bool ok2 = interleaved_ ? a_->next(chunk.r2[got]) : b_->next(chunk.r2[got]);
            if (!ok2) {
                if (interleaved_) {
                    error_ = "interleaved input ended on an unpaired read: " + chunk.r1[got].name;
                    ok_ = false;
                }
                break;
            }
        }
        ++got;
    }
    chunk.n = got;

    if (!a_->ok() && !a_->error().empty()) { error_ = a_->error(); ok_ = false; }
    if (b_ && !b_->ok() && !b_->error().empty()) { error_ = b_->error(); ok_ = false; }

    if (got == 0) return false;
    produced_ += static_cast<long long>(got);
    ++index_;
    return true;
}

// ---------------------------------------------------------------------------
// GzipMemberCompressor
// ---------------------------------------------------------------------------

GzipMemberCompressor::GzipMemberCompressor(int level) : level_(level) {
    if (level_ < 1) level_ = 1;
    if (level_ > 9) level_ = 9;
    scratch_.resize(256u << 10);
}

GzipMemberCompressor::~GzipMemberCompressor() {
    if (inited_) deflateEnd(&zs_);
}

void GzipMemberCompressor::compress(const std::string& in, std::string& out) {
    if (in.empty()) return;
    if (!inited_) {
        std::memset(&zs_, 0, sizeof(zs_));
        if (deflateInit2(&zs_, level_, Z_DEFLATED, static_cast<int>(kGzWindow), 8,
                         Z_DEFAULT_STRATEGY) != Z_OK) {
            return;
        }
        inited_ = true;
    } else {
        // Reset so each call emits a complete, standalone gzip member.
        deflateReset(&zs_);
    }
    zs_.next_in = reinterpret_cast<Bytef*>(const_cast<char*>(in.data()));
    zs_.avail_in = static_cast<uInt>(in.size());
    int ret = Z_OK;
    do {
        zs_.next_out = reinterpret_cast<Bytef*>(scratch_.data());
        zs_.avail_out = static_cast<uInt>(scratch_.size());
        ret = deflate(&zs_, Z_FINISH);
        size_t produced = scratch_.size() - zs_.avail_out;
        if (produced) out.append(scratch_.data(), produced);
    } while (ret == Z_OK);
}

// ---------------------------------------------------------------------------
// OutputFile
// ---------------------------------------------------------------------------

OutputFile::OutputFile(const std::string& path, int compressionLevel, const SplitOptions* split)
    : path_(path), level_(compressionLevel), split_(split) {
    if (path.empty()) return;
    active_ = true;
    toStdout_ = (path == "-");
    gzipped_ = !toStdout_ && util::isGzipPath(path);
    splitting_ = (split_ && split_->enabled && !toStdout_);
    if (gzipped_) comp_ = std::make_unique<GzipMemberCompressor>(level_);
    if (!openCurrent()) ok_ = false;
}

OutputFile::~OutputFile() { close(); }

std::string OutputFile::splitPathFor(int index) const {
    std::string dir = util::dirname(path_);
    std::string base = util::basename(path_);
    std::string num = std::to_string(index);
    int digits = split_ ? split_->digits : 4;
    while (static_cast<int>(num.size()) < digits) num.insert(num.begin(), '0');
    std::string name = num + "." + base;
    return dir == "." && path_.find('/') == std::string::npos ? name : dir + "/" + name;
}

bool OutputFile::openCurrent() {
    if (!active_) return true;
    if (toStdout_) { fh_ = stdout; writtenPaths_.push_back("<stdout>"); return true; }
    std::string target = splitting_ ? splitPathFor(splitIndex_) : path_;
    std::string dir = util::dirname(target);
    if (!dir.empty() && dir != ".") util::makeDirs(dir);
    fh_ = std::fopen(target.c_str(), "wb");
    if (!fh_) { error_ = "cannot write to " + target; return false; }
    // A large stdio buffer keeps write() syscalls out of the hot path.
    std::setvbuf(fh_, nullptr, _IOFBF, 1u << 20);
    writtenPaths_.push_back(target);
    return true;
}

void OutputFile::rollIfNeeded(size_t incoming) {
    if (!splitting_ || splitTarget_ <= 0) return;
    if (splitRecords_ == 0) return;                      // never roll on an empty file
    if (splitRecords_ + static_cast<long long>(incoming) <= splitTarget_) return;
    if (split_->byFileNumber() && splitIndex_ >= split_->number) return;  // last file absorbs the rest
    if (fh_ && fh_ != stdout) std::fclose(fh_);
    fh_ = nullptr;
    ++splitIndex_;
    splitRecords_ = 0;
    if (!openCurrent()) ok_ = false;
}

void OutputFile::appendPrepared(const std::string& data, size_t records) {
    if (!active_ || !ok_ || data.empty()) { totalRecords_ += records; splitRecords_ += records; return; }
    rollIfNeeded(records);
    if (!fh_) return;
    if (std::fwrite(data.data(), 1, data.size(), fh_) != data.size()) {
        error_ = "write failed on " + path_;
        ok_ = false;
        return;
    }
    totalBytes_ += static_cast<long long>(data.size());
    totalRecords_ += records;
    splitRecords_ += records;
}

void OutputFile::appendRaw(const std::string& data, size_t records) {
    if (!active_ || !ok_ || data.empty()) { totalRecords_ += records; splitRecords_ += records; return; }
    if (!gzipped_) { appendPrepared(data, records); return; }
    rollIfNeeded(records);
    if (!fh_) return;
    std::string packed;
    packed.reserve(data.size() / 3 + 64);
    comp_->compress(data, packed);
    if (std::fwrite(packed.data(), 1, packed.size(), fh_) != packed.size()) {
        error_ = "write failed on " + path_;
        ok_ = false;
        return;
    }
    totalBytes_ += static_cast<long long>(packed.size());
    totalRecords_ += records;
    splitRecords_ += records;
}

void OutputFile::close() {
    if (fh_ && fh_ != stdout) std::fclose(fh_);
    if (fh_ == stdout) std::fflush(stdout);
    fh_ = nullptr;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

long long estimateRecordCount(const std::string& path, double* avgRecordBytes) {
    long long fsize = util::fileSize(path);
    if (fsize <= 0) return 0;

    FastqInput in(path);
    if (!in.ok()) return 0;

    Read r;
    long long uncompressed = 0, n = 0;
    const long long kSample = 20000;
    while (n < kSample && in.next(r)) {
        uncompressed += static_cast<long long>(r.name.size() + r.seq.size() +
                                               r.strand.size() + r.qual.size() + 6);
        ++n;
    }
    if (n == 0) return 0;
    double avg = static_cast<double>(uncompressed) / static_cast<double>(n);
    if (avgRecordBytes) *avgRecordBytes = avg;

    if (!util::isGzipPath(path)) {
        return static_cast<long long>(static_cast<double>(fsize) / avg);
    }
    // For gzip, infer the ratio from what we consumed versus what we produced.
    long long compressedConsumed = in.bytesRead();
    if (compressedConsumed <= 0) return 0;
    double ratio = static_cast<double>(uncompressed) / static_cast<double>(compressedConsumed);
    if (ratio < 1.0) ratio = 3.5;   // gzread reports post-inflate bytes on some builds
    return static_cast<long long>(static_cast<double>(fsize) * ratio / avg);
}

std::vector<Read> peekReads(const std::string& path, size_t n, bool fromStdin) {
    std::vector<Read> out;
    if (fromStdin) return out;   // cannot rewind stdin
    FastqInput in(path);
    if (!in.ok()) return out;
    out.reserve(n);
    Read r;
    while (out.size() < n && in.next(r)) out.push_back(r);
    return out;
}

// ---------------------------------------------------------------------------
// Whole-file sampling
// ---------------------------------------------------------------------------

const char* SampleInfo::strategyName() const {
    switch (strategy) {
        case Strategy::WholeFile:  return "whole_file";
        case Strategy::Stratified: return "stratified";
        case Strategy::Reservoir:  return "reservoir";
        case Strategy::Head:       break;
    }
    return "head";
}

namespace {

// Bytes a stratum must be able to look at before it gives up on resynchronising.
constexpr size_t kResyncWindow = 512u << 10;
constexpr size_t kSamplerBuf = 1u << 20;

// splitmix64. Seeded from a constant so that two runs over the same file draw
// the same sample and a report is reproducible.
inline uint64_t splitmix64(uint64_t& state) {
    uint64_t z = (state += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
}

void shuffleDeterministic(std::vector<Read>& v) {
    uint64_t rng = 0x243F6A8885A308D3ull;
    for (size_t i = v.size(); i > 1; --i) {
        size_t j = static_cast<size_t>(splitmix64(rng) % i);
        if (j != i - 1) std::swap(v[j], v[i - 1]);
    }
}

// Buffered reader over a plain (uncompressed) FASTQ that can seek to an
// arbitrary byte offset. gzip cannot be handled here; see sampleReads().
class PlainSampler {
public:
    explicit PlainSampler(const std::string& path) {
        fh_ = std::fopen(path.c_str(), "rb");
        buf_.resize(kSamplerBuf);
    }
    ~PlainSampler() { if (fh_) std::fclose(fh_); }
    PlainSampler(const PlainSampler&) = delete;
    PlainSampler& operator=(const PlainSampler&) = delete;

    bool ok() const { return fh_ != nullptr; }

    // Jump to `offset` and advance to the next valid record boundary.
    bool seekAndResync(long long offset) {
        if (std::fseek(fh_, static_cast<long>(offset), SEEK_SET) != 0) return false;
        pos_ = len_ = 0;
        eof_ = false;
        atStart_ = (offset == 0);
        if (!want(kResyncWindow)) return false;
        size_t rel = findRecordStart(buf_.data() + pos_, len_ - pos_, atStart_);
        if (rel == std::string::npos) return false;
        pos_ += rel;
        return true;
    }

    bool next(Read& out) {
        if (!nextLine(header_) || header_.empty() || header_[0] != '@') return false;
        out.name.assign(header_, 1, std::string::npos);
        if (!nextLine(out.seq) || !nextLine(out.strand) || !nextLine(out.qual)) return false;
        return out.seq.size() == out.qual.size();
    }

private:
    // Make at least `n` bytes available from pos_ (fewer at end of file).
    bool want(size_t n) {
        while (len_ - pos_ < n && !eof_) {
            if (pos_ > 0) {
                std::memmove(buf_.data(), buf_.data() + pos_, len_ - pos_);
                len_ -= pos_;
                pos_ = 0;
            }
            if (len_ == buf_.size()) buf_.resize(buf_.size() * 2);
            size_t got = std::fread(buf_.data() + len_, 1, buf_.size() - len_, fh_);
            if (got == 0) { eof_ = true; break; }
            len_ += got;
        }
        return len_ > pos_;
    }

    bool nextLine(std::string& out) {
        out.clear();
        while (true) {
            const char* base = buf_.data();
            const void* nl = std::memchr(base + pos_, '\n', len_ - pos_);
            if (nl) {
                size_t at = static_cast<size_t>(static_cast<const char*>(nl) - base);
                size_t n = at - pos_;
                if (n > 0 && base[at - 1] == '\r') --n;
                out.append(base + pos_, n);
                pos_ = at + 1;
                return true;
            }
            if (eof_) {
                if (pos_ < len_) {
                    size_t n = len_ - pos_;
                    if (n > 0 && base[len_ - 1] == '\r') --n;
                    out.append(base + pos_, n);
                    pos_ = len_;
                    return !out.empty();
                }
                return false;
            }
            size_t remain = len_ - pos_;
            if (remain > 0) {
                out.append(base + pos_, remain);
                pos_ = len_;
            }
            if (!want(kSamplerBuf / 2)) return !out.empty();
        }
    }

    std::FILE* fh_ = nullptr;
    std::vector<char> buf_;
    size_t pos_ = 0, len_ = 0;
    bool eof_ = false;
    bool atStart_ = true;
    std::string header_;
};

std::vector<Read> stratifiedSample(const std::string& path, size_t target, long long fileBytes,
                                   SampleInfo& info) {
    std::vector<Read> out;
    PlainSampler probe(path);
    if (!probe.ok()) return out;

    // Average record size, so a file that is already smaller than the target is
    // read straight through instead of being seeked around.
    Read r;
    long long probeBytes = 0, probeRecords = 0;
    if (probe.seekAndResync(0)) {
        while (probeRecords < 256 && probe.next(r)) {
            probeBytes += static_cast<long long>(r.name.size() + r.seq.size() + r.strand.size() +
                                                 r.qual.size() + 6);
            ++probeRecords;
        }
    }
    if (probeRecords == 0) return out;
    const double avg = static_cast<double>(probeBytes) / static_cast<double>(probeRecords);
    const long long estRecords = static_cast<long long>(static_cast<double>(fileBytes) / avg);

    PlainSampler in(path);
    if (!in.ok()) return out;

    if (estRecords <= static_cast<long long>(target)) {
        info.strategy = SampleInfo::Strategy::WholeFile;
        info.strata = 1;
        if (!in.seekAndResync(0)) return out;
        out.reserve(static_cast<size_t>(estRecords) + 16);
        while (out.size() < target && in.next(r)) out.push_back(r);
        info.scanned = static_cast<long long>(out.size());
        info.exactCount = out.size() < target;
        return out;
    }

    info.strategy = SampleInfo::Strategy::Stratified;
    info.strata = kSampleStrata;
    const size_t perStratum = (target + kSampleStrata - 1) / kSampleStrata;
    out.reserve(target);
    long long scanned = 0;
    for (int s = 0; s < kSampleStrata && out.size() < target; ++s) {
        const long long offset =
            static_cast<long long>(static_cast<double>(fileBytes) * s / kSampleStrata);
        if (!in.seekAndResync(offset)) continue;
        for (size_t i = 0; i < perStratum && out.size() < target; ++i) {
            if (!in.next(r)) break;
            out.push_back(r);
            ++scanned;
        }
    }
    info.scanned = scanned;
    return out;
}

std::vector<Read> reservoirSample(const std::string& path, size_t target, SampleInfo& info) {
    std::vector<Read> out;
    FastqInput in(path);
    if (!in.ok()) return out;

    info.strategy = SampleInfo::Strategy::Reservoir;

    // Fill the reservoir by parsing straight into its slots: on a file only a
    // few times larger than the sample that removes one whole copy per record,
    // which is most of the sampler's cost above the unavoidable inflate.
    out.resize(target);
    uint64_t seen = 0;
    while (seen < target) {
        if (!in.next(out[static_cast<size_t>(seen)])) break;
        ++seen;
    }
    out.resize(static_cast<size_t>(seen));

    if (seen == target) {
        Read r;
        uint64_t rng = 0x9E3779B97F4A7C15ull;
        while (in.next(r)) {
            ++seen;
            // Algorithm R: record `seen` replaces a uniformly chosen slot with
            // probability target/seen, which leaves every record of the file
            // equally likely to survive.
            const uint64_t j = splitmix64(rng) % seen;
            // Swapping rather than assigning hands the displaced buffers back to
            // `r`, so the scratch record never reallocates.
            if (j < target) std::swap(out[static_cast<size_t>(j)], r);
        }
    }
    info.scanned = static_cast<long long>(seen);
    info.exactCount = in.error().empty();
    if (seen <= target) info.strategy = SampleInfo::Strategy::WholeFile;
    return out;
}

}  // namespace

size_t findRecordStart(const char* data, size_t len, bool atFileStart) {
    size_t p = 0;
    if (!atFileStart) {
        const void* nl = std::memchr(data, '\n', len);
        if (!nl) return std::string::npos;
        p = static_cast<size_t>(static_cast<const char*>(nl) - data) + 1;
    }
    while (p < len) {
        size_t begin[4] = {0, 0, 0, 0}, end[4] = {0, 0, 0, 0};
        size_t q = p;
        bool complete = true;
        for (int i = 0; i < 4; ++i) {
            const void* nl = std::memchr(data + q, '\n', len - q);
            if (!nl) { complete = false; break; }
            const size_t at = static_cast<size_t>(static_cast<const char*>(nl) - data);
            begin[i] = q;
            end[i] = (at > q && data[at - 1] == '\r') ? at - 1 : at;
            q = at + 1;
        }
        if (!complete) return std::string::npos;

        const size_t seqLen = end[1] - begin[1];
        const size_t qualLen = end[3] - begin[3];
        if (data[begin[0]] == '@' && seqLen > 0 && seqLen == qualLen &&
            end[2] > begin[2] && data[begin[2]] == '+') {
            return begin[0];
        }
        p = begin[1];   // that '@' was a quality line; try the next line start
    }
    return std::string::npos;
}

std::vector<Read> sampleReads(const std::string& path, size_t target, bool fromStdin,
                              SampleInfo* info) {
    util::Timer timer;
    SampleInfo local;
    std::vector<Read> out;

    if (target == 0 || fromStdin) {
        // stdin is a one-shot stream; the caller falls back to head sampling.
        if (info) { local.seconds = timer.elapsed(); *info = local; }
        return out;
    }

    const long long size = util::fileSize(path);
    if (size <= 0) {
        if (info) { local.seconds = timer.elapsed(); *info = local; }
        return out;
    }

    if (util::isGzipPath(path)) {
        out = reservoirSample(path, target, local);
    } else {
        out = stratifiedSample(path, target, size, local);
    }

    // Detection stages cap themselves at a prefix of the sample, so the sample
    // is shuffled to keep every prefix representative of the whole file.
    shuffleDeterministic(out);

    local.records = out.size();
    local.seconds = timer.elapsed();
    if (info) *info = local;
    return out;
}

}  // namespace fp
