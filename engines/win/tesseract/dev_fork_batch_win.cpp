// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// dev_fork_batch_win.cpp -- takes the place of src/dev_fork_batch.cpp in the Windows object list.
//
// The developer fork batch (TESSERACT_DEV_FORK_BATCH) is fork(), sigaction(), prctl(), wait4() and
// /proc code with no Windows counterpart and no role for a user. The class keeps its upstream
// header, so a change to the interface upstream breaks this file at compile time, not silently.
// The upstream .cpp stays in the staged tree: the test source scan (tests/env_scan.h) reads it.
#include "dev_fork_batch.h"

#include "assembler.h"
#include "envflags.h"

namespace ts { namespace dev {

bool ForkBatch::prepare(const AssemblyOptions&, std::string& error) {
    if (!env::text("TESSERACT_DEV_FORK_BATCH")) return true;
    enabled_ = true;
    error = "TESSERACT_DEV_FORK_BATCH (developer fork batch) is not available in the Windows build";
    return false;
}

ForkBatch::Entry ForkBatch::enter(AssemblyOptions&, const UnitigGraph&, const SequenceStore&, double,
                                  std::string&) {
    return Entry::Disabled;
}

bool ForkBatch::finishChild(double, std::string&) { return true; }
bool ForkBatch::writeBatch(const std::string&, std::string&) const { return true; }
bool ForkBatch::writeChild(const std::string&, double, std::string&) const { return true; }
ForkBatch::~ForkBatch() {}

} }  // namespace ts::dev
