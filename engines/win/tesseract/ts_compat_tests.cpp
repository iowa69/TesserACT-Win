// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// ts_compat_tests.cpp -- what only the upstream TEST programs need on Windows: setenv, unsetenv,
// mkdtemp, freopen("/dev/null") and a \tmp folder. Linked into the test programs, never into
// tesseract-asm. Empty on every other platform.
#include "ts_compat.h"

#ifdef _WIN32

#undef environ
#undef freopen

#include <process.h>

#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

// The C runtime cannot store an empty value: _putenv_s(name, "") REMOVES the variable. The tests set
// empty values on purpose (an empty TESSERACT_* value is an error the engine must report), so a
// one-character value is stored and then shortened in place. getenv() and the environ walk of
// src/envflags.cpp both read that table, so both see "NAME=".
int setenv(const char* name, const char* value, int overwrite) {
    if (!name || !*name || std::strchr(name, '=') || !value) { errno = EINVAL; return -1; }
    if (!overwrite && std::getenv(name)) return 0;
    if (*value) return _putenv_s(name, value) == 0 ? 0 : -1;
    if (_putenv_s(name, "x") != 0) return -1;
    const size_t n = std::strlen(name);
    for (char** e = _environ; e && *e; ++e) {
        if (_strnicmp(*e, name, n) == 0 && (*e)[n] == '=') {
            (*e)[n + 1] = '\0';
            return 0;
        }
    }
    errno = ENOENT;
    return -1;
}

int unsetenv(const char* name) {
    if (!name || !*name || std::strchr(name, '=')) { errno = EINVAL; return -1; }
    return _putenv_s(name, "") == 0 ? 0 : -1;
}

// The tests pass literal templates such as "/tmp/tesseract-XXXXXX": on Windows that is \tmp on the
// current drive, which is created here when it is missing. The template cannot grow.
char* mkdtemp(char* templ) {
    const size_t n = templ ? std::strlen(templ) : 0;
    if (n < 6 || std::strcmp(templ + n - 6, "XXXXXX") != 0) { errno = EINVAL; return nullptr; }
    static const char alphabet[] = "abcdefghijklmnopqrstuvwxyz0123456789";
    static std::atomic<unsigned long long> counter{0};
    unsigned long long seed = (static_cast<unsigned long long>(std::time(nullptr)) << 20) ^
                              (static_cast<unsigned long long>(_getpid()) << 8) ^
                              static_cast<unsigned long long>(std::clock()) ^ (counter.fetch_add(1) * 0x9E3779B97F4A7C15ULL);
    bool madeTmp = false;
    for (int attempt = 0; attempt < 1000; ++attempt) {
        seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
        unsigned long long v = seed >> 16;
        for (int i = 0; i < 6; ++i) { templ[n - 6 + i] = alphabet[v % 36]; v /= 36; }
        if (_mkdir(templ) == 0) return templ;
        if (errno == EEXIST) continue;
        if (errno == ENOENT && !madeTmp && std::strncmp(templ, "/tmp/", 5) == 0) {
            madeTmp = true;
            _mkdir("/tmp");
            continue;
        }
        break;
    }
    std::memcpy(templ + n - 6, "XXXXXX", 6);
    return nullptr;
}

std::FILE* ts_compat_freopen(const char* path, const char* mode, std::FILE* stream) {
    if (path && std::strcmp(path, "/dev/null") == 0) path = "NUL";
    return std::freopen(path, mode, stream);
}

// Several tests use mkstemp() and plain file names under the literal "/tmp", which on Windows is
// \tmp on the current drive. Make sure it exists before main() runs.
namespace {
struct TmpFolder {
    TmpFolder() { _mkdir("/tmp"); }
} tmpFolder;
}  // namespace

#endif  // _WIN32
