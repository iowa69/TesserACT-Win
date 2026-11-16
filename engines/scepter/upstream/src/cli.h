// Command-line parsing and help output.
#pragma once

#include <string>

#include "options.h"

namespace fp {

constexpr const char* kScepterVersion = "1.1.0";

// Returns: 0 = parsed OK and should run, 1 = handled (help/version printed), -1 = error.
int parseCommandLine(int argc, char** argv, Options& opt, std::string& error);

void printUsage(bool full);
void printVersion();

}  // namespace fp
