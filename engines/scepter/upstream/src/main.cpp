#include <cstdio>
#include <string>

#include "cli.h"
#include "options.h"
#include "processor.h"
#include "report.h"
#include "util.h"

int main(int argc, char** argv) {
    using namespace fp;

    Options opt;
    std::string error;

    for (int i = 0; i < argc; ++i) {
        if (i) opt.commandLine += ' ';
        opt.commandLine += argv[i];
    }
    if (argc > 0 && argv[0]) opt.programDir = util::executableDir(argv[0]);

    int rc = parseCommandLine(argc, argv, opt, error);
    if (rc == 1) return 0;
    if (rc < 0) {
        std::fprintf(stderr, "%serror:%s %s\n", util::Color::red(), util::Color::reset(),
                     error.c_str());
        std::fprintf(stderr, "Run '%s --help' for usage.\n", argv[0]);
        return 1;
    }

    if (opt.mode == RunMode::ReportOnly) {
        if (!reportFromJson(opt.jsonFile, opt.htmlFile, error)) {
            std::fprintf(stderr, "%serror:%s %s\n", util::Color::red(), util::Color::reset(),
                         error.c_str());
            return 1;
        }
        std::fprintf(stderr, "Wrote %s\n", opt.htmlFile.c_str());
        return 0;
    }

    if (!opt.finalize(error)) {
        std::fprintf(stderr, "%serror:%s %s\n", util::Color::red(), util::Color::reset(),
                     error.c_str());
        return 1;
    }

    if (opt.dryRun || opt.explain) {
        std::fputs(opt.describe().c_str(), stdout);
        if (opt.dryRun) return 0;
    }

    Processor proc(opt);
    if (!proc.run(error)) {
        std::fprintf(stderr, "%serror:%s %s\n", util::Color::red(), util::Color::reset(),
                     error.c_str());
        return 1;
    }

    RunStats stats = proc.stats();
    stats.command = opt.commandLine;

    if (!opt.jsonFile.empty()) writeJsonReport(opt.jsonFile, stats, opt);
    if (!opt.htmlFile.empty()) writeHtmlReport(opt.htmlFile, stats, opt);
    if (!opt.tsvFile.empty()) writeTsvReport(opt.tsvFile, stats, opt);

    printSummary(stats, opt);
    return 0;
}
