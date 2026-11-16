// Run orchestration: a reader thread, a pool of workers, and an ordering
// writer thread. Output order always matches input order regardless of how
// chunks are scheduled.
#pragma once

#include <string>

#include "options.h"
#include "pipeline.h"
#include "stats.h"

namespace fp {

class Processor {
public:
    explicit Processor(Options& opt);
    ~Processor();

    // Runs the whole pipeline. Returns false and fills `error` on failure.
    bool run(std::string& error);

    const RunStats& stats() const { return stats_; }
    const DetectionResult& detection() const { return detection_; }

private:
    Options& opt_;
    RunStats stats_;
    DetectionResult detection_;
};

}  // namespace fp
