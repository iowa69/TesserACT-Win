// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// ts_selftest.cpp -- the hidden self-test of the port, on BOTH targets.
//
//   TS_COMPAT_SELFTEST=1 tesseract-asm [any arguments]
//
// prints one "key=value" line per probe on stdout and exits 0 when every probe passed, 1 otherwise
// (the reason of each failure goes to stderr). It runs before main(). The folder it works in is the
// first one set of TS_COMPAT_SELFTEST_DIR, TMPDIR, TEMP, TMP, else the current folder.
//
// This file is compiled like an engine source: the compatibility header is force-included and the
// probes use the very names the engine uses (struct stat, ::stat, fopen, std::ofstream, snprintf,
// ::environ, localtime_r, realpath, ts::util::*). On Linux they therefore test glibc and the
// upstream code, on Windows the compatibility layer, and the two outputs can be compared line by
// line. Lines that differ by design: platform, sizeof_long (8 on Linux, 4 on Windows).
// thread_stack_4m is "skipped" on a Linux system whose threads get less than 6 MiB of stack.
#include "ts_selftest.h"

#include <sys/stat.h>
#include <unistd.h>
#include <zlib.h>
#ifdef __linux__
#  include <pthread.h>
#endif

#include <chrono>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <iterator>
#include <string>
#include <thread>
#include <vector>

#include "util.h"

namespace ts_compat {

namespace {

bool gAllPassed = true;

void probe(const char* key, const std::string& value, bool pass, const std::string& detail = std::string()) {
    std::printf("%s=%s\n", key, value.c_str());
    if (!pass) {
        gAllPassed = false;
        std::fprintf(stderr, "ts_compat selftest: FAILED %s=%s%s%s\n", key, value.c_str(),
                     detail.empty() ? "" : " -- ", detail.c_str());
    }
}

void probeBool(const char* key, bool value, const std::string& detail = std::string()) {
    probe(key, value ? "1" : "0", value, detail);
}

std::string num(long long v) {
    char b[32];
    std::snprintf(b, sizeof b, "%lld", v);
    return b;
}

// The engine's own test (src/main.cpp sameFile, src/seqio.cpp), through the same names.
// 1 same, 0 different, -1 when one of the two cannot be examined.
int sameFile(const std::string& a, const std::string& b) {
    struct stat sa, sb;
    if (::stat(a.c_str(), &sa) != 0 || ::stat(b.c_str(), &sb) != 0) return -1;
    return (sa.st_dev == sb.st_dev && sa.st_ino == sb.st_ino) ? 1 : 0;
}

bool writeWith(const std::string& path, const char* mode, const char* text) {
    std::FILE* f = std::fopen(path.c_str(), mode);
    if (!f) return false;
    const bool ok = std::fputs(text, f) >= 0;
    return std::fclose(f) == 0 && ok;
}

// Every byte that is really in the file, read in binary.
bool slurp(const std::string& path, std::string& out) {
    out.clear();
    std::FILE* f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    char buf[4096];
    size_t n = 0;
    while ((n = std::fread(buf, 1, sizeof buf, f)) > 0) out.append(buf, n);
    std::fclose(f);
    return true;
}

long long bytesIn(const std::string& path) {
    std::string all;
    return slurp(path, all) ? static_cast<long long>(all.size()) : -1;
}

// On Windows a virus scanner or the search indexer may hold a file that was just written, and a
// folder cannot go while a file in it is still being deleted. Try for two seconds before giving up.
template <class Remove>
void removeForSure(const std::string& path, Remove removeOnce) {
    for (int attempt = 0; attempt < 40; ++attempt) {
        if (removeOnce(path.c_str()) == 0 || !ts::util::fileExists(path)) return;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}
void removeFile(const std::string& path) { removeForSure(path, [](const char* p) { return std::remove(p); }); }
void removeDir(const std::string& path) { removeForSure(path, [](const char* p) { return static_cast<int>(::rmdir(p)); }); }

std::string format1(const char* fmt, double value) {
    char b[64];
    std::snprintf(b, sizeof b, fmt, value);
    return b;
}

void probeFormat(const char* key, const char* fmt, double value, const char* want) {
    volatile double v = value;                   // formatted at run time, by the C library
    const std::string got = format1(fmt, v);
    probe(key, got, got == want, std::string("expected ") + want);
}

std::string workBase() {
    for (const char* name : {"TS_COMPAT_SELFTEST_DIR", "TMPDIR", "TEMP", "TMP"}) {
        const char* v = std::getenv(name);
        if (v && *v) return v;
    }
    return ".";
}

std::vector<std::string> commandLine() {
    std::vector<std::string> out;
    TS_COMPAT_CMDLINE_STREAM(f);                 // the statement rule R1 leaves in src/p2_provenance.cpp
    if (!f) return out;
    const std::string all((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    size_t at = 0;
    while (at < all.size()) {
        const size_t z = all.find('\0', at);
        const size_t e = z == std::string::npos ? all.size() : z;
        out.push_back(all.substr(at, e - at));
        at = e + 1;
    }
    return out;
}

void probeTypes() {
    probe("platform", platformName(), true);
#ifdef _WIN32
    const size_t wantLong = 4;
#else
    const size_t wantLong = 8;
#endif
    probe("sizeof_long", num(static_cast<long long>(sizeof(long))), sizeof(long) == wantLong);
    probe("sizeof_long_long", num(static_cast<long long>(sizeof(long long))), sizeof(long long) == 8);
}

void probeFormats() {
    probeFormat("printf_4f_24.86445", "%.4f", 24.86445, "24.8645");
    probeFormat("printf_4f_0.00005", "%.4f", 0.00005, "0.0001");
    probeFormat("printf_0f_0.5", "%.0f", 0.5, "0");          // exact ties: glibc rounds to even
    probeFormat("printf_0f_1.5", "%.0f", 1.5, "2");
    probeFormat("printf_0f_2.5", "%.0f", 2.5, "2");
    probeFormat("printf_4f_0.03125", "%.4f", 0.03125, "0.0312");
    probeFormat("printf_4f_0.09375", "%.4f", 0.09375, "0.0938");
    char b[96];
    volatile size_t big = static_cast<size_t>(-1);
    volatile long long low = LLONG_MIN;
    std::snprintf(b, sizeof b, "%zu,%lld", static_cast<size_t>(big), static_cast<long long>(low));
    probe("printf_zu_lld", b, std::strcmp(b, "18446744073709551615,-9223372036854775808") == 0);
}

void probeMemory() {
    const long long before = ts::util::currentMemoryBytes();
    // 64 MiB, every page written: resident memory must follow, or the --max-memory guard is blind.
    const size_t size = static_cast<size_t>(64) << 20;
    char* volatile block = static_cast<char*>(std::malloc(size));
    unsigned long long sum = 0;
    if (block) {
        for (size_t i = 0; i < size; i += 1024) block[i] = static_cast<char>((i >> 10) | 1);
        for (size_t i = 0; i < size; i += 1024) sum += static_cast<unsigned char>(block[i]);
    }
    const long long during = ts::util::currentMemoryBytes();
    const long long peak = ts::util::peakMemoryBytes();
    const long long total = ts::util::totalMemoryBytes();
    std::free(block);
    std::fprintf(stderr, "ts_compat selftest: memory before=%lld during=%lld peak=%lld total=%lld bytes\n",
                 before, during, peak, total);
    probeBool("memory_current_gt0", before > 0 && during > 0);
    probeBool("memory_peak_gt0", peak > 0);
    probeBool("memory_total_gt0", total > 0);
    probeBool("memory_current_follows_alloc", sum != 0 && during - before >= (static_cast<long long>(48) << 20));
    probeBool("memory_peak_ge_current", peak >= during / 2);
    probeBool("memory_total_ge_peak", total >= peak);
}

void probeProcess() {
    const std::string exe = TS_COMPAT_SELF_EXE;  // the expression rule R2 leaves in src/p2_provenance.cpp
    struct stat st;
    const bool found = !exe.empty() && ::stat(exe.c_str(), &st) == 0;
    probeBool("self_exe_exists", found && S_ISREG(st.st_mode), exe);
    probeBool("self_exe_size_gt0", found && st.st_size > 0);
    const std::vector<std::string> args = commandLine();
    probeBool("cmdline_argv0_nonempty", !args.empty() && !args[0].empty());
    std::string tail;
    for (size_t i = 1; i < args.size(); ++i) {
        if (i > 1) tail += '|';
        tail += args[i];
    }
    probe("argv_tail", tail, true);              // run with a quoted "*" argument to see wildcards stay
    bool inEnviron = false;
    for (char** e = ::environ; e && *e; ++e) {
        if (std::strcmp(*e, "TS_COMPAT_SELFTEST=1") == 0) inEnviron = true;
    }
    probeBool("environ_walk", inEnviron);
    std::time_t tt = static_cast<std::time_t>(86400) * 366;
    std::tm tmv{};
    probeBool("localtime_r", localtime_r(&tt, &tmv) != nullptr && tmv.tm_year >= 70 && tmv.tm_year <= 71);
}

// ---- the stack of a worker thread ------------------------------------------------------------------
// The gap filler (Walker::walk, src/gapfill.cpp) recurses once per base, about 4,100 levels deep, on
// std::thread workers. Linux gives a thread 8 MiB. The Windows build asks the linker for 16 MiB
// (-Wl,--stack,16777216), which is also what a new thread gets; without it a thread has 2 MiB and
// such a walk ends the program with exception 0xC00000FD and no message. One level below uses a
// little more than 1 KiB, so 4,096 levels need well over 4 MiB. Called through a volatile pointer
// so that the compiler can neither inline the levels nor turn them into a loop.
unsigned long long burnStack(int depth, unsigned long long seed);
unsigned long long (*volatile gBurnStack)(int, unsigned long long) = burnStack;

unsigned long long burnStack(int depth, unsigned long long seed) {
    volatile unsigned char pad[1024];
    for (size_t i = 0; i < sizeof pad; i += 64) pad[i] = static_cast<unsigned char>(seed >> (i & 31));
    const unsigned long long below =
        depth > 1 ? gBurnStack(depth - 1, seed * 6364136223846793005ULL + 1442695040888963407ULL) : seed;
    return below + pad[((seed >> 33) % (sizeof pad / 64)) * 64];      // read after the call: no tail call
}

// How much stack the calling thread has; 0 when that cannot be told.
size_t threadStackBytes() {
#ifdef __linux__
    pthread_attr_t attr;
    if (pthread_getattr_np(pthread_self(), &attr) != 0) return 0;
    void* base = nullptr;
    size_t size = 0;
    if (pthread_attr_getstack(&attr, &base, &size) != 0) size = 0;
    pthread_attr_destroy(&attr);
    return size;
#else
    return 0;
#endif
}

void probeThreadStack() {
    const int levels = 4096;
    bool walked = false;
    unsigned long long sum = 0;
    size_t have = 0;
    std::fprintf(stderr, "ts_compat selftest: walking %d levels (over 4 MiB) of stack on a worker thread; if the "
                         "program ends here, threads do not get the stack the gap filler needs\n", levels);
    std::fflush(stdout);                         // what was printed so far survives a stack overflow
    std::fflush(stderr);
    std::thread worker([&]() {
        have = threadStackBytes();
#ifdef _WIN32
        const bool roomy = true;                 // the very thing to prove: the 16 MiB reach every thread
#else
        const bool roomy = have >= (static_cast<size_t>(6) << 20);
#endif
        if (!roomy) return;
        sum = gBurnStack(levels, 0x9E3779B97F4A7C15ULL);
        walked = true;
    });
    worker.join();
    if (!walked) {
        std::fprintf(stderr, "ts_compat selftest: thread stack walk skipped, a thread has %llu bytes of stack here\n",
                     static_cast<unsigned long long>(have));
    }
    probe("thread_stack_4m", walked ? "1" : "skipped", walked ? sum != 0 : true);
}

void probeFiles(const std::string& work) {
    // ---- the same-file test of main.cpp and seqio.cpp
    const std::string a = work + "/a.txt", b = work + "/b.txt";
    writeWith(a, "w", "first\n");
    writeWith(b, "w", "first\n");
    probe("same_file_two_files", num(sameFile(a, b)), sameFile(a, b) == 0,
          "two different files are reported as one: every paired run would be refused");
    probe("same_file_itself", num(sameFile(a, a)), sameFile(a, a) == 1);
    probe("same_file_other_spelling", num(sameFile(a, work + "/./a.txt")), sameFile(a, work + "/./a.txt") == 1);
    probe("same_file_missing", num(sameFile(a, work + "/absent.txt")), sameFile(a, work + "/absent.txt") == -1);

    // ---- a file larger than 4 GiB keeps its size
    const std::string sparse = work + "/sparse.bin";
    const long long big = (static_cast<long long>(1) << 32) + 4097;
    long long seen = -1;
    if (makeSparseFile(sparse.c_str(), big)) {
        struct stat st;
        if (::stat(sparse.c_str(), &st) == 0) seen = static_cast<long long>(st.st_size);
    } else {
        std::fprintf(stderr, "ts_compat selftest: cannot create a sparse file in %s\n", work.c_str());
    }
    probe("stat_size_over_4g", num(seen), seen == big);
    probeBool("file_exists_over_4g", ts::util::fileExists(sparse));
    removeFile(sparse);

    // ---- text mode must not exist: "a\n" is two bytes, through C stdio and through C++ streams
    const std::string t1 = work + "/mode_w.txt", t2 = work + "/mode_ofstream.txt";
    writeWith(t1, "w", "a\n");
    probe("fopen_w_newline_bytes", num(bytesIn(t1)), bytesIn(t1) == 2);
    {
        std::ofstream out(t2);
        out << "a\n";
    }
    probe("ofstream_newline_bytes", num(bytesIn(t2)), bytesIn(t2) == 2);
    {
        // ... and a reader must see the bytes that are in the file ("\r\n" stays, 0x1A is no end of file)
        const std::string t3 = work + "/mode_read.txt";
        writeWith(t3, "wb", "x\r\n\x1Ay\n");
        std::ifstream in(t3);
        const std::string got((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
        probe("ifstream_raw_bytes", num(static_cast<long long>(got.size())), got == "x\r\n\x1Ay\n");
        std::string viaR;
        if (std::FILE* f = std::fopen(t3.c_str(), "r")) {
            int c = 0;
            while ((c = std::fgetc(f)) != EOF) viaR.push_back(static_cast<char>(c));
            std::fclose(f);
        }
        probe("fopen_r_raw_bytes", num(static_cast<long long>(viaR.size())), viaR == "x\r\n\x1Ay\n");
        removeFile(t3);
    }
    {
        // the FASTA header of every contig is written with fprintf("%.4f")
        const std::string t4 = work + "/cov.txt";
        volatile double cov = 24.86445;
        if (std::FILE* f = std::fopen(t4.c_str(), "wb")) {
            std::fprintf(f, ">NODE_1_length_%zu_cov_%.4f\n", static_cast<size_t>(1234), static_cast<double>(cov));
            std::fclose(f);
        }
        std::string line;
        slurp(t4, line);
        if (!line.empty() && line.back() == '\n') line.pop_back();
        probe("fprintf_fasta_header", line, line == ">NODE_1_length_1234_cov_24.8645");
        removeFile(t4);
    }

    // ---- a folder with a space and non-ASCII characters (e-acute, a CJK character), as UTF-8
    const std::string uni = work + "/sp ace \xC3\xA9 \xE6\xBC\xA2";
    probeBool("unicode_dir_create", ts::util::makeDirs(uni) && ts::util::fileExists(uni));
    const std::string uf = uni + "/r\xC3\xA9sum\xC3\xA9 1.txt";
    std::string back;
    probeBool("unicode_fopen_roundtrip", writeWith(uf, "w", "hello\n") && slurp(uf, back) && back == "hello\n");
    {
        std::ifstream in(uf);
        const std::string got((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
        probeBool("unicode_ifstream_roundtrip", got == "hello\n");
    }
    {
        const std::string gz = uni + "/reads \xC3\xA9.fq.gz";
        const char text[] = "@r1\nACGT\n+\nIIII\n";
        bool ok = false;
        if (gzFile out = gzopen(gz.c_str(), "wb")) {
            ok = gzwrite(out, text, static_cast<unsigned>(sizeof text - 1)) == static_cast<int>(sizeof text - 1);
            ok = gzclose(out) == Z_OK && ok;
        }
        char buf[64] = {0};
        int got = -1;
        if (gzFile in = gzopen(gz.c_str(), "rb")) {
            got = gzread(in, buf, sizeof buf - 1);
            gzclose(in);
        }
        probeBool("unicode_gzopen_roundtrip", ok && got == static_cast<int>(sizeof text - 1) && std::strcmp(buf, text) == 0);
        removeFile(gz);
    }
    {
        char resolved[PATH_MAX];
        const char* r = ::realpath(uf.c_str(), resolved);
        probeBool("unicode_realpath", r != nullptr && sameFile(r, uf) == 1, r ? r : "(null)");
        char missing[PATH_MAX];
        probeBool("realpath_missing_fails", ::realpath((uni + "/absent").c_str(), missing) == nullptr);
    }
    probeBool("stat_trailing_slash", ts::util::fileExists(uni + "/"));
    const std::string nested = work + "/n1/n2/n3";
    probeBool("makedirs_nested", ts::util::makeDirs(nested) && ts::util::fileExists(nested));

    // ---- clean up; whatever is left behind is a failure of its own
    for (const std::string& f : {uf, a, b, t1, t2}) removeFile(f);
    for (const std::string& d : {uni, nested, work + "/n1/n2", work + "/n1", work}) removeDir(d);
    probeBool("cleanup", !ts::util::fileExists(work));
}

}  // namespace

int selfTest() {
    gAllPassed = true;
    probeTypes();
    probeFormats();
    probeMemory();
    probeProcess();
    probeThreadStack();
    const std::string base = workBase();
    const std::string work = base + "/ts-compat-selftest-" + num(static_cast<long long>(getpid())) + "-" +
                             num(static_cast<long long>(std::time(nullptr)));
    const bool haveWork = ts::util::makeDirs(work) && ts::util::fileExists(work);
    probeBool("work_dir", haveWork, work);
    if (haveWork) probeFiles(work);
    probe("selftest", gAllPassed ? "pass" : "fail", true);
    return gAllPassed ? 0 : 1;
}

}  // namespace ts_compat
