// Report writers. All output is self-contained: the HTML embeds its own CSS and
// JavaScript so a report can be opened or emailed without any network access.
#pragma once

#include <string>

#include "options.h"
#include "stats.h"

namespace fp {

void writeJsonReport(const std::string& path, const RunStats& stats, const Options& opt);
void writeHtmlReport(const std::string& path, const RunStats& stats, const Options& opt);
void writeTsvReport(const std::string& path, const RunStats& stats, const Options& opt);

// Human-readable run summary printed to stderr when the run finishes.
void printSummary(const RunStats& stats, const Options& opt);

// Rebuild an HTML report from a previously written JSON report.
bool reportFromJson(const std::string& jsonPath, const std::string& htmlPath,
                    std::string& error);

}  // namespace fp
