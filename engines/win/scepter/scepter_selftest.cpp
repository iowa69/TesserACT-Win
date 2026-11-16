// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// scepter_selftest.cpp -- the hidden self-test of the scepter port. Not upstream code.
//
//     SCEPTER_PORT_SELFTEST=1 scepter [arguments]
//
// prints one "key=value" line per probe, then "selftest=pass" or "selftest=FAIL" with
// the keys that failed, and exits 0 or 1. Nothing else of the program runs. Temporary
// files go to $SCEPTER_PORT_SELFTEST_DIR, else $TMPDIR, $TEMP, $TMP, else the current
// folder, and are removed again. Arguments are echoed (argN, and argN_hex) so a caller
// can prove that "*" is not expanded and that a non-ASCII argument arrives intact.
//
// This file is compiled WITH the force-include of scepter_port.h and the same flags as
// upstream code, on purpose: stat, mkdir, readlink, fopen, printf and the seek helper
// below are exactly what upstream gets on that platform (on Windows, the redirected
// ones). It includes no upstream header, so an upstream release cannot break it.
#include <cerrno>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>
#include <sys/stat.h>
#include <unistd.h>
#include <zlib.h>

namespace {

int g_failed = 0;
// Not a global object: on Linux the self-test starts from a constructor function, which
// may run before the global objects of this file are built.
std::string& failedKeys() {
    static std::string keys;
    return keys;
}

void emit(const char* key, const char* value, int pass) {
    std::printf("%s=%s\n", key, value);
    if (!pass) {
        ++g_failed;
        if (!failedKeys().empty()) failedKeys() += ',';
        failedKeys() += key;
    }
}
void emitNumber(const char* key, long long value, bool pass) {
    emit(key, std::to_string(value).c_str(), pass ? 1 : 0);
}
void emitBool(const char* key, bool value, bool expected) {
    emit(key, value ? "1" : "0", value == expected ? 1 : 0);
}

// The same test upstream uses to refuse an output that is the input (src/options.cpp).
// -1 when either file cannot be examined.
int sameFile(const std::string& a, const std::string& b) {
    struct stat sa, sb;
    if (::stat(a.c_str(), &sa) != 0 || ::stat(b.c_str(), &sb) != 0) return -1;
    return (sa.st_dev == sb.st_dev && sa.st_ino == sb.st_ino) ? 1 : 0;
}

long long sizeByStat(const std::string& path) {
    struct stat st;
    if (::stat(path.c_str(), &st) != 0) return -1;
    return static_cast<long long>(st.st_size);
}

bool writeBytes(const std::string& path, const char* mode, const std::string& bytes) {
    std::FILE* f = std::fopen(path.c_str(), mode);
    if (!f) return false;
    const bool ok = std::fwrite(bytes.data(), 1, bytes.size(), f) == bytes.size();
    return std::fclose(f) == 0 && ok;
}

bool readBytes(const std::string& path, const char* mode, std::string& out) {
    out.clear();
    std::FILE* f = std::fopen(path.c_str(), mode);
    if (!f) return false;
    char buf[256];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof(buf), f)) > 0) out.append(buf, n);
    std::fclose(f);
    return true;
}

std::string hexOf(const char* s) {
    static const char digits[] = "0123456789abcdef";
    std::string out;
    for (; *s; ++s) {
        out += digits[(static_cast<unsigned char>(*s) >> 4) & 15];
        out += digits[static_cast<unsigned char>(*s) & 15];
    }
    return out;
}

std::string baseDir() {
    for (const char* name : {"SCEPTER_PORT_SELFTEST_DIR", "TMPDIR", "TEMP", "TMP"}) {
        const char* v = std::getenv(name);
        if (v && *v) return v;
    }
    return ".";
}

std::string join(const std::string& dir, const std::string& name) {
    if (dir.empty()) return name;
    const char last = dir.back();
    return (last == '/' || last == '\\') ? dir + name : dir + "/" + name;
}

// ---- probes ------------------------------------------------------------------------

void probeTypes() {
#ifdef _WIN32
    emit("target", "windows", 1);
    const size_t wantLong = 4;        // LLP64: the reason for transformation T1
#else
    emit("target", "linux", 1);
    const size_t wantLong = 8;
#endif
    struct stat st;
    emitNumber("sizeof_long", static_cast<long long>(sizeof(long)), sizeof(long) == wantLong);
    emitNumber("sizeof_long_long", static_cast<long long>(sizeof(long long)), sizeof(long long) == 8);
    emitNumber("sizeof_size_t", static_cast<long long>(sizeof(size_t)), sizeof(size_t) == 8);
    emitNumber("sizeof_stat_size", static_cast<long long>(sizeof(st.st_size)), sizeof(st.st_size) == 8);
    // Not a verdict: zlib's own offset type only feeds the progress percentage.
    emitNumber("sizeof_z_off_t", static_cast<long long>(sizeof(z_off_t)), true);
    emit("zlib_version", zlibVersion(), std::strcmp(zlibVersion(), ZLIB_VERSION) == 0);
}

void probeOwnPath() {
    char buf[4096];
    const long long n = static_cast<long long>(::readlink("/proc/self/exe", buf, sizeof(buf) - 1));
    if (n <= 0) {
        emit("exe_path", "", 0);
        emit("exe_path_exists", "0", 0);
        return;
    }
    buf[n] = '\0';
    emit("exe_path", buf, 1);
    emitBool("exe_path_exists", sizeByStat(buf) > 0, true);
}

void probeSameFile(const std::string& dir) {
    const std::string a = join(dir, "first.txt"), b = join(dir, "second.txt");
    writeBytes(a, "wb", "one\n");
    writeBytes(b, "wb", "one\n");
    const int different = sameFile(a, b);
    emit("same_file_two_files", different < 0 ? "error" : different ? "1" : "0", different == 0);
    // Another spelling of the same file, so that equal text is not what is compared.
    const int self = sameFile(a, join(join(dir, "."), "first.txt"));
    emit("same_file_itself", self < 0 ? "error" : self ? "1" : "0", self == 1);
    std::remove(a.c_str());
    std::remove(b.c_str());
}

void probeBigFile(const std::string& dir) {
    const long long offset = (1LL << 32) + (2LL << 20);       // 4 GB + 2 MB
    const char marker[] = "SCEPTER!";
    const long long want = offset + static_cast<long long>(sizeof(marker) - 1);
    const std::string path = join(dir, "sparse.bin");
    const int made = scepter_port_make_big_file(path.c_str(), offset, marker);
    if (made != 0) {
        const char* why = made == 2 ? "unsupported: no sparse files in this folder"
                        : made == 3 ? "cannot write" : "cannot create";
        emit("big_file_size", why, 0);
        emit("big_file_seek", "0", 0);
        return;
    }
    emitNumber("big_file_size", sizeByStat(path), sizeByStat(path) == want);
    // The call transformation T1 puts into the plain-FASTQ sampler. A 32-bit offset
    // would land 4 GB short, in the hole, and read zeros.
    bool ok = false;
    if (std::FILE* f = std::fopen(path.c_str(), "rb")) {
        char got[sizeof(marker)] = {0};
        ok = scepter_fseek64(f, offset, SEEK_SET) == 0 &&
             std::fread(got, 1, sizeof(marker) - 1, f) == sizeof(marker) - 1 &&
             std::memcmp(got, marker, sizeof(marker) - 1) == 0;
        std::fclose(f);
    }
    emitBool("big_file_seek", ok, true);
    std::remove(path.c_str());
}

void probeTextMode(const std::string& dir) {
    // Mode "w" without "b": two bytes on Linux, three on Windows unless files are binary.
    const std::string p = join(dir, "text.txt");
    std::string got;
    writeBytes(p, "w", "a\n");
    readBytes(p, "rb", got);
    emitNumber("text_mode_write_bytes", static_cast<long long>(got.size()), got == "a\n");
    { std::ofstream out(p.c_str()); out << "a\n"; }
    readBytes(p, "rb", got);
    emitNumber("text_mode_stream_bytes", static_cast<long long>(got.size()), got == "a\n");
    // Reading: no "\r\n" to "\n", no end of file at byte 0x1A.
    const std::string raw("a\r\n\x1a" "b\n", 6);
    writeBytes(p, "wb", raw);
    readBytes(p, "r", got);
    emitNumber("text_mode_read_bytes", static_cast<long long>(got.size()), got == raw);
    std::remove(p.c_str());
}

void probeFormats() {
    // Exact binary halves: a correct printf rounds them to even, an old Windows one
    // rounds them up. The expected text is what glibc prints.
    struct Probe { const char* key; const char* format; double value; const char* expected; };
    static const Probe probes[] = {
        {"round_1f_0.25", "%.1f", 0.25, "0.2"},
        {"round_1f_0.75", "%.1f", 0.75, "0.8"},
        {"round_2f_0.125", "%.2f", 0.125, "0.12"},
        {"round_2f_0.375", "%.2f", 0.375, "0.38"},
        {"round_0f_0.5", "%.0f", 0.5, "0"},
        {"round_0f_2.5", "%.0f", 2.5, "2"},
        {"round_4f_97.65625", "%.4f", 97.65625, "97.6562"},
        {"round_6g_1234565", "%.6g", 1234565.0, "1.23456e+06"},
        {"round_6g_0.00001", "%.6g", 0.00001, "1e-05"},
        {"round_6g_third", "%.6g", 1.0 / 3.0, "0.333333"},
        {"round_17g_tenth", "%.17g", 0.1, "0.10000000000000001"},
    };
    char buf[64];
    for (const Probe& p : probes) {
        std::snprintf(buf, sizeof(buf), p.format, p.value);
        emit(p.key, buf, std::strcmp(buf, p.expected) == 0);
    }
    std::snprintf(buf, sizeof(buf), "%zu", static_cast<size_t>(-1));
    emit("format_zu", buf, std::strcmp(buf, "18446744073709551615") == 0);
    std::snprintf(buf, sizeof(buf), "%lld", LLONG_MIN);
    emit("format_lld", buf, std::strcmp(buf, "-9223372036854775808") == 0);
}

void probeArguments() {
    // The rules the Windows main applies to its arguments, on a fixed command line.
    struct Case { const char* given; const char* wanted; };
    static const Case cases[] = {
        {"C:\\tools\\scepter.exe", "C:\\tools\\scepter.exe"},          // argv[0]: untouched
        {"-i", "-i"},
        {"C:\\data\\a_R1.fq.gz", "C:/data/a_R1.fq.gz"},
        {"-IC:\\data\\a_R2.fq.gz", "-IC:/data/a_R2.fq.gz"},
        {"--out-dir=D:\\out", "--out-dir=D:/out"},
        {"--json", "--json"},
        {"rep\\r.json", "rep/r.json"},
        {"\\\\server\\share\\x.fq", "//server/share/x.fq"},
        {"\\\\?\\C:\\long\\x.fq", "\\\\?\\C:\\long\\x.fq"},        // taken literally by Windows
        {"-o\\\\?\\C:\\long\\o.fq", "-o\\\\?\\C:\\long\\o.fq"},
        {"-R", "-R"},
        {"run 7\\a", "run 7\\a"},                              // a title is not a path
        {"-Rrun 7\\b", "-Rrun 7\\b"},
        {"--report-title=x\\y", "--report-title=x\\y"},
        {"--UMI_PREFIX", "--UMI_PREFIX"},
        {"U\\", "U\\"},
        {"--umi-delim", "--umi-delim"},
        {"\\", "\\"},
        {"-AR", "-AR"},                                        // bundled: a switch, then -R TITLE
        {"t\\1", "t\\1"},
        {"-3Rt\\2", "-3Rt\\2"},
        {"-Ao", "-Ao"},                                        // bundled: a switch, then -o FILE
        {"d\\f", "d/f"},
        {"-AoD:\\o\\f", "-AoD:/o/f"},
        {"-o=d\\f", "-o=d/f"},
        {"-53", "-53"},                                        // switches only
        {"in\\x.fq", "in/x.fq"},
        {"-", "-"},                                            // standard input
        {"--", "--"},
        {"-R", "-R"},                                          // after "--": a file name
        {"dir\\file", "dir/file"},
    };
    const int n = static_cast<int>(sizeof(cases) / sizeof(cases[0]));
    std::vector<std::string> copies;
    for (const Case& c : cases) copies.push_back(c.given);
    std::vector<char*> argv;
    for (std::string& c : copies) argv.push_back(&c[0]);
    argv.push_back(nullptr);
    scepter_port_normalize_arguments(n, argv.data());
    int wrong = 0;
    for (int i = 0; i < n; ++i)
        if (copies[static_cast<size_t>(i)] != cases[i].wanted && wrong == 0) wrong = i + 1;
    if (wrong == 0) emit("argument_slashes", "1", 1);
    else emit("argument_slashes", ("wrong at " + std::to_string(wrong - 1) + ": " +
                                   copies[static_cast<size_t>(wrong - 1)]).c_str(), 0);
}

void probeUnicodeDir(const std::string& dir) {
    // A space, an accented letter and a non-Latin character, as UTF-8.
    const std::string leaf = "scepter port \xC3\xA9\xE6\x97\xA5";
    const std::string folder = join(dir, leaf);
    const std::string file = join(folder, "r\xC3\xA9" "ad me.txt");
    const std::string packed = join(folder, "reads \xC3\xA9.fq");
    const std::string text = "kept\n";
    std::string got;
    bool ok = ::mkdir(folder.c_str(), 0755) == 0;
    ok = ok && writeBytes(file, "wb", text) && readBytes(file, "rb", got) && got == text &&
         sizeByStat(file) == static_cast<long long>(text.size());
    emitBool("unicode_dir", ok, true);
    // Read files are opened by zlib, not by stdio (it reads plain files as they are):
    // the same folder through gzopen.
    bool gz = false;
    if (writeBytes(packed, "wb", text)) {
        if (gzFile in = gzopen(packed.c_str(), "rb")) {
            char buf[32];
            const int n = gzread(in, buf, sizeof(buf));
            gz = n == static_cast<int>(text.size()) && std::memcmp(buf, text.data(), text.size()) == 0;
            gzclose(in);
        }
    }
    emitBool("unicode_dir_zlib", gz, true);
    scepter_port_platform_probes(emit, dir.c_str(), leaf.c_str());
    std::remove(file.c_str());
    std::remove(packed.c_str());
    scepter_port_remove_dir(folder.c_str());
}

void probeLongPath(const std::string& dir) {
    // Not a verdict: beyond 260 characters Windows also needs LongPathsEnabled in the
    // registry, which is the PC's setting and not the program's.
    const std::string a = join(dir, std::string(120, 'a'));
    const std::string b = join(a, std::string(120, 'b'));
    const std::string f = join(b, std::string(60, 'c') + ".txt");
    std::string got;
    const bool ok = ::mkdir(a.c_str(), 0755) == 0 && ::mkdir(b.c_str(), 0755) == 0 &&
                    writeBytes(f, "wb", "x") && readBytes(f, "rb", got) && got == "x" &&
                    sizeByStat(f) == 1;
    emit("info_long_paths", ok ? "1" : "0", 1);
    std::remove(f.c_str());
    scepter_port_remove_dir(b.c_str());
    scepter_port_remove_dir(a.c_str());
}

}  // namespace

int scepter_port_selftest(int argc, char** argv) {
    g_failed = 0;
    failedKeys().clear();
    emit("selftest_format", "1", 1);
    probeTypes();
    probeOwnPath();
    probeFormats();
    probeArguments();

    const std::string dir = join(baseDir(), "scepter-selftest-" + std::to_string(scepter_port_process_id()));
    const bool haveDir = ::mkdir(dir.c_str(), 0700) == 0;
    emit("work_dir", dir.c_str(), haveDir ? 1 : 0);
    if (haveDir) {
        probeSameFile(dir);
        probeBigFile(dir);
        probeTextMode(dir);
        probeUnicodeDir(dir);
        probeLongPath(dir);
        emitBool("work_dir_removed", scepter_port_remove_dir(dir.c_str()) == 0, true);
    }

    emitNumber("args", argc > 0 ? argc - 1 : 0, true);
    for (int i = 1; i < argc; ++i) {
        const std::string key = "arg" + std::to_string(i);
        emit(key.c_str(), argv[i], 1);
        emit((key + "_hex").c_str(), hexOf(argv[i]).c_str(), 1);
    }

    if (g_failed == 0) std::printf("selftest=pass\n");
    else std::printf("selftest=FAIL %s\n", failedKeys().c_str());
    std::fflush(stdout);
    return g_failed == 0 ? 0 : 1;
}
