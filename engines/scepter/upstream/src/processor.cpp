#include "processor.h"

#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

#include "cli.h"
#include "contam.h"
#include "fastqio.h"
#include "util.h"

namespace fp {

namespace {

constexpr size_t kChunkReads = 8192;

using ChunkPtr = std::shared_ptr<Chunk>;

// Bounded blocking queue. The bound is what keeps memory flat: the reader
// cannot run arbitrarily far ahead of the workers.
class ChunkQueue {
public:
    explicit ChunkQueue(size_t cap) : cap_(cap) {}

    void push(ChunkPtr c) {
        std::unique_lock<std::mutex> lock(mu_);
        notFull_.wait(lock, [&] { return q_.size() < cap_ || closed_; });
        if (closed_) return;
        q_.push_back(std::move(c));
        lock.unlock();
        notEmpty_.notify_one();
    }

    bool pop(ChunkPtr& out) {
        std::unique_lock<std::mutex> lock(mu_);
        notEmpty_.wait(lock, [&] { return !q_.empty() || closed_; });
        if (q_.empty()) return false;
        out = std::move(q_.front());
        q_.pop_front();
        lock.unlock();
        notFull_.notify_one();
        return true;
    }

    void close() {
        {
            std::lock_guard<std::mutex> lock(mu_);
            closed_ = true;
        }
        notEmpty_.notify_all();
        notFull_.notify_all();
    }

private:
    std::mutex mu_;
    std::condition_variable notEmpty_, notFull_;
    std::deque<ChunkPtr> q_;
    size_t cap_;
    bool closed_ = false;
};

// Recycles Chunk objects so their Read strings and output buffers keep their
// capacity for the whole run. Without this the reader spends most of its time
// in malloc.
class ChunkPool {
public:
    explicit ChunkPool(size_t n) {
        free_.reserve(n);
        for (size_t i = 0; i < n; ++i) free_.push_back(std::make_shared<Chunk>());
    }

    ChunkPtr acquire() {
        std::unique_lock<std::mutex> lock(mu_);
        cv_.wait(lock, [&] { return !free_.empty(); });
        ChunkPtr c = std::move(free_.back());
        free_.pop_back();
        return c;
    }

    void release(ChunkPtr c) {
        if (!c) return;
        std::lock_guard<std::mutex> lock(mu_);
        free_.push_back(std::move(c));
        cv_.notify_one();
    }

private:
    std::mutex mu_;
    std::condition_variable cv_;
    std::vector<ChunkPtr> free_;
};

size_t countRecords(const std::string& fastqText) {
    size_t lines = 0;
    for (char c : fastqText) if (c == '\n') ++lines;
    return lines / 4;
}

// `bytes` must be a position in the compressed stream so that it is on the same
// scale as `totalBytes` (the on-disk size).
void printProgress(long long reads, long long bytes, long long totalBytes, double elapsed) {
    double pct = totalBytes > 0 ? 100.0 * static_cast<double>(bytes) / static_cast<double>(totalBytes) : 0;
    if (pct > 100.0) pct = 100.0;
    if (pct < 0.0) pct = 0.0;
    double rps = elapsed > 0 ? static_cast<double>(reads) / elapsed : 0;
    char bar[32];
    int filled = totalBytes > 0 ? static_cast<int>(pct / 100.0 * 24) : 0;
    for (int i = 0; i < 24; ++i) bar[i] = i < filled ? '=' : ' ';
    bar[24] = 0;
    if (totalBytes > 0) {
        std::fprintf(stderr, "\r%s  [%s] %5.1f%%  %s reads  %s reads/s%s   ",
                     util::Color::gray(), bar, pct,
                     util::humanCount(static_cast<double>(reads)).c_str(),
                     util::humanCount(rps).c_str(), util::Color::reset());
    } else {
        std::fprintf(stderr, "\r%s  %s reads  %s reads/s%s   ",
                     util::Color::gray(),
                     util::humanCount(static_cast<double>(reads)).c_str(),
                     util::humanCount(rps).c_str(), util::Color::reset());
    }
    std::fflush(stderr);
}

}  // namespace

Processor::Processor(Options& opt) : opt_(opt) {}
Processor::~Processor() = default;

bool Processor::run(std::string& error) {
    util::Timer timer;
    stats_.startedAt = util::nowString();
    stats_.version = kScepterVersion;

    // ---- auto-detection -------------------------------------------------
    detection_ = detectInputProperties(opt_);

    stats_.detection.strategy = detection_.sampleStrategy;
    stats_.detection.sampleReads1 = static_cast<long long>(detection_.sampleReads1);
    stats_.detection.sampleReads2 = static_cast<long long>(detection_.sampleReads2);
    stats_.detection.recordsScanned = detection_.sampleScanned;
    stats_.detection.exactRecordCount = detection_.sampleExactCount;
    stats_.detection.seconds = detection_.sampleSeconds;
    stats_.detection.sampleMeanQuality = detection_.sampleMeanQuality;
    stats_.detection.sampleGcFraction = detection_.sampleGcFraction;
    stats_.detection.instrument = detection_.instrument;
    stats_.detection.platform = detection_.platform;
    stats_.detection.tileHeadersUsable = detection_.tileHeadersUsable;
    stats_.detection.polyGRecommended = detection_.polyGRecommended;
    stats_.detection.polyGTailFraction = detection_.polyGTailFraction;

    if (opt_.verbose && !opt_.quiet) {
        std::fprintf(stderr, "%s[detect]%s %s sample: %zu + %zu reads in %.2fs\n",
                     util::Color::cyan(), util::Color::reset(),
                     detection_.sampleStrategy.c_str(), detection_.sampleReads1,
                     detection_.sampleReads2, detection_.sampleSeconds);
    }

    // ---- contamination screen -------------------------------------------
    if (opt_.contam.enabled) {
        double libraryBases = 0;
        const long long records = detection_.estimatedReads;
        auto meanLen = [](const std::vector<Read>& v) {
            if (v.empty()) return 0.0;
            uint64_t b = 0;
            for (const Read& r : v) b += r.seq.size();
            return static_cast<double>(b) / static_cast<double>(v.size());
        };
        if (records > 0) {
            libraryBases = static_cast<double>(records) *
                           (meanLen(detection_.sample1) + meanLen(detection_.sample2));
        }
        stats_.contamination = screenContamination(opt_, detection_.sample1, detection_.sample2,
                                                   libraryBases);
        // The sample is the largest thing detection allocates; the screen was
        // its only remaining consumer.
        detection_.sample1.clear();
        detection_.sample1.shrink_to_fit();
        detection_.sample2.clear();
        detection_.sample2.shrink_to_fit();

        if (!opt_.quiet) {
            const ContamResult& c = stats_.contamination;
            if (!c.ok) {
                std::fprintf(stderr, "%swarning:%s contamination screen skipped: %s\n",
                             util::Color::yellow(), util::Color::reset(), c.message.c_str());
            } else {
                std::fprintf(stderr, "%s[contam]%s %s%s%s\n", util::Color::cyan(),
                             util::Color::reset(), c.verdict.c_str(),
                             c.dominant.empty() ? "" : " - ", c.dominant.c_str());
            }
        }
    }

    if (detection_.phred64Detected && !opt_.phred64) {
        std::fprintf(stderr, "%s[detect]%s input looks like phred64; enabling --phred64\n",
                     util::Color::yellow(), util::Color::reset());
        opt_.phred64 = true;
    }
    if (!opt_.polyG.forced && !opt_.polyG.disabled) {
        opt_.polyG.enabled = detection_.polyGRecommended;
        if (opt_.polyG.enabled && !opt_.quiet) {
            std::fprintf(stderr, "%s[detect]%s %s detected; enabling polyG trimming\n",
                         util::Color::cyan(), util::Color::reset(),
                         detection_.instrument.empty() ? "two-colour chemistry"
                                                       : detection_.instrument.c_str());
        }
    }

    // Adapter resolution: an explicit --adapter_sequence always wins.
    if (opt_.adapter.enabled) {
        if (opt_.adapter.sequence != "auto" && !opt_.adapter.sequence.empty()) {
            opt_.adapter.resolvedR1 = util::toUpper(opt_.adapter.sequence);
        } else if (!opt_.isPaired() || opt_.adapter.detectForPE) {
            opt_.adapter.resolvedR1 = detection_.adapter1;
            opt_.adapter.detectedR1 = detection_.adapter1Detected;
        }
        if (opt_.adapter.sequenceR2 != "auto" && !opt_.adapter.sequenceR2.empty()) {
            opt_.adapter.resolvedR2 = util::toUpper(opt_.adapter.sequenceR2);
        } else if (opt_.isPaired() && opt_.adapter.detectForPE) {
            opt_.adapter.resolvedR2 = detection_.adapter2;
            opt_.adapter.detectedR2 = detection_.adapter2Detected;
        } else if (opt_.isPaired() && !opt_.adapter.resolvedR1.empty() &&
                   opt_.adapter.sequenceR2 == "auto") {
            opt_.adapter.resolvedR2 = opt_.adapter.resolvedR1;
        }
        if (!opt_.quiet) {
            if (opt_.adapter.detectedR1 && !opt_.adapter.resolvedR1.empty()) {
                std::fprintf(stderr, "%s[detect]%s read1 adapter: %s\n", util::Color::cyan(),
                             util::Color::reset(), opt_.adapter.resolvedR1.c_str());
            }
            if (opt_.adapter.detectedR2 && !opt_.adapter.resolvedR2.empty()) {
                std::fprintf(stderr, "%s[detect]%s read2 adapter: %s\n", util::Color::cyan(),
                             util::Color::reset(), opt_.adapter.resolvedR2.c_str());
            }
        }
    }

    // The k-mer sketch needs enough depth to separate the error shoulder from
    // the coverage peak, but sampling beyond that only costs time. Target a
    // fixed volume of sampled sequence rather than a fixed fraction, so shallow
    // libraries are sampled in full and deep ones are thinned.
    if (opt_.deep.kmerSpectrum) {
        constexpr double kTargetSampledBases = 1.5e8;
        const double readLen = detection_.readLength1 > 0 ? detection_.readLength1 : 150;
        double estBases = static_cast<double>(detection_.estimatedReads) * readLen;
        if (opt_.isPaired()) estBases *= 2.0;
        int factor = 1;
        if (estBases > kTargetSampledBases) {
            factor = static_cast<int>(estBases / kTargetSampledBases + 0.5);
        }
        if (factor < 1) factor = 1;
        if (factor > 8) factor = 8;
        opt_.deep.kmerSamplingFactor = factor;
        if (opt_.verbose) {
            std::fprintf(stderr, "%s[detect]%s k-mer sketch sampling 1-in-%d\n",
                         util::Color::cyan(), util::Color::reset(), factor);
        }
    }

    // ---- outputs --------------------------------------------------------
    OutputFile out1(opt_.stdoutOutput ? "-" : opt_.out1, opt_.compression, &opt_.split);
    OutputFile out2(opt_.stdoutOutput ? "" : opt_.out2, opt_.compression, &opt_.split);
    OutputFile outMerged(opt_.merge.enabled ? opt_.merge.out : "", opt_.compression, &opt_.split);
    OutputFile outUnpaired1(opt_.unpaired1, opt_.compression, nullptr);
    OutputFile outUnpaired2(opt_.unpaired2 == opt_.unpaired1 ? "" : opt_.unpaired2,
                            opt_.compression, nullptr);
    OutputFile outFailed(opt_.failedOut, opt_.compression, nullptr);
    OutputFile outOverlapped(opt_.overlappedOut, opt_.compression, nullptr);

    for (OutputFile* f : {&out1, &out2, &outMerged, &outUnpaired1, &outUnpaired2,
                          &outFailed, &outOverlapped}) {
        if (f->active() && !f->ok()) { error = f->error(); return false; }
    }

    if (opt_.split.enabled) {
        long long est = detection_.estimatedReads > 0 ? detection_.estimatedReads : 0;
        if (opt_.split.byFileNumber() && est > 0) {
            long long per = (est + opt_.split.number - 1) / opt_.split.number;
            out1.setSplitTarget(per);
            out2.setSplitTarget(per);
            outMerged.setSplitTarget(per);
        } else if (opt_.split.byLines > 0) {
            long long per = opt_.split.byLines / 4;
            out1.setSplitTarget(per);
            out2.setSplitTarget(per);
            outMerged.setSplitTarget(per);
        }
    }

    const bool precompress = out1.allowsPrecompression() &&
                             (!out2.active() || out2.allowsPrecompression()) &&
                             (!outMerged.active() || outMerged.allowsPrecompression());

    // ---- input ----------------------------------------------------------
    ChunkSource source(opt_.in1, opt_.in2, opt_.interleavedIn, opt_.stdinInput, kChunkReads);
    if (!source.ok()) { error = source.error(); return false; }
    source.setLimit(opt_.readsToProcess);

    const int nWorkers = std::max(1, opt_.threads);
    ChunkQueue inQ(static_cast<size_t>(nWorkers) * 3 + 4);
    ChunkQueue outQ(static_cast<size_t>(nWorkers) * 3 + 4);

    std::unique_ptr<SharedDedup> dedup;
    if (opt_.duplication.dedup) dedup = std::make_unique<SharedDedup>(8);

    std::atomic<long long> readsSeen{0};
    std::atomic<bool> failed{false};
    std::string readerError;

    // Reader ---------------------------------------------------------------
    ChunkPool pool(static_cast<size_t>(nWorkers) * 4 + 12);

    std::thread reader([&] {
        while (true) {
            ChunkPtr c = pool.acquire();
            if (!source.next(*c)) { pool.release(std::move(c)); break; }
            readsSeen.fetch_add(static_cast<long long>(c->size()), std::memory_order_relaxed);
            inQ.push(std::move(c));
        }
        if (!source.ok()) { readerError = source.error(); failed.store(true); }
        inQ.close();
    });

    // Workers --------------------------------------------------------------
    std::vector<std::unique_ptr<Worker>> workers;
    workers.reserve(static_cast<size_t>(nWorkers));
    for (int i = 0; i < nWorkers; ++i) {
        workers.push_back(std::make_unique<Worker>(opt_));
        if (dedup) workers.back()->setDedup(dedup.get());
    }

    std::atomic<int> activeWorkers{nWorkers};
    std::vector<std::thread> workerThreads;
    workerThreads.reserve(static_cast<size_t>(nWorkers));
    for (int i = 0; i < nWorkers; ++i) {
        workerThreads.emplace_back([&, i] {
            Worker& w = *workers[static_cast<size_t>(i)];
            GzipMemberCompressor comp(opt_.compression);
            ChunkPtr c;
            while (inQ.pop(c)) {
                w.process(*c);
                if (precompress) {
                    // Deflate here so compression scales with the worker pool
                    // instead of serialising in the writer.
                    auto pack = [&](std::string& buf) {
                        if (buf.empty()) return;
                        std::string packed;
                        packed.reserve(buf.size() / 3 + 64);
                        comp.compress(buf, packed);
                        buf.swap(packed);
                    };
                    pack(c->out1);
                    pack(c->out2);
                    pack(c->outMerged);
                    c->compressed = true;
                }
                outQ.push(std::move(c));
            }
            if (activeWorkers.fetch_sub(1) == 1) outQ.close();
        });
    }

    // Writer ---------------------------------------------------------------
    std::thread writer([&] {
        std::map<uint64_t, ChunkPtr> pending;
        uint64_t wantIndex = 0;
        ChunkPtr c;
        long long chunksWritten = 0;

        auto flush = [&](ChunkPtr& ch) {
            if (ch->compressed) {
                out1.appendPrepared(ch->out1, 0);
                out2.appendPrepared(ch->out2, 0);
                outMerged.appendPrepared(ch->outMerged, 0);
            } else {
                out1.appendRaw(ch->out1, countRecords(ch->out1));
                out2.appendRaw(ch->out2, countRecords(ch->out2));
                outMerged.appendRaw(ch->outMerged, countRecords(ch->outMerged));
            }
            outUnpaired1.appendRaw(ch->outUnpaired1, countRecords(ch->outUnpaired1));
            outUnpaired2.appendRaw(ch->outUnpaired2, countRecords(ch->outUnpaired2));
            outFailed.appendRaw(ch->outFailed, countRecords(ch->outFailed));
            outOverlapped.appendRaw(ch->outOverlapped, countRecords(ch->outOverlapped));
            ++chunksWritten;
        };

        while (outQ.pop(c)) {
            pending.emplace(c->index, std::move(c));
            auto it = pending.find(wantIndex);
            while (it != pending.end()) {
                flush(it->second);
                pool.release(std::move(it->second));
                pending.erase(it);
                ++wantIndex;
                it = pending.find(wantIndex);
            }
            if (!opt_.quiet && (chunksWritten % 16) == 0) {
                printProgress(readsSeen.load(std::memory_order_relaxed), source.compressedBytes(),
                              source.totalBytes(), timer.elapsed());
            }
        }
        // Any stragglers left after the queue closed.
        for (auto& kv : pending) flush(kv.second);
    });

    reader.join();
    for (auto& t : workerThreads) t.join();
    writer.join();

    if (!opt_.quiet) {
        printProgress(readsSeen.load(), source.compressedBytes(), source.totalBytes(), timer.elapsed());
        std::fprintf(stderr, "\r%*s\r", 78, "");
    }

    for (OutputFile* f : {&out1, &out2, &outMerged, &outUnpaired1, &outUnpaired2,
                          &outFailed, &outOverlapped}) {
        f->close();
        if (f->active() && !f->ok()) { error = f->error(); return false; }
    }

    if (failed.load()) { error = readerError; return false; }

    // ---- aggregate ------------------------------------------------------
    for (auto& w : workers) stats_.mergeWorker(w->stats);
    stats_.durationSeconds = timer.elapsed();
    stats_.finalize();
    return true;
}

}  // namespace fp
