// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// scepter_port.h -- compatibility header of the native Windows port of scepter.
// Not upstream code. Force-included (g++ -include port/scepter_port.h) into every
// scepter translation unit and into scepter_selftest.cpp; never into scepter_port.cpp
// (that file defines SCEPTER_PORT_IMPL first).
//
// What it does:
//   * On every platform: defines scepter_fseek64(), which the scripted transformation
//     T1 (transform.py) puts in place of the one seek that went through a `long`.
//   * On Windows only: names that MinGW-w64 lacks or gives another meaning are
//     redirected by macro to scepter_* functions implemented in scepter_port.cpp, and
//     main() is renamed so that the real main() there can prepare the process first.
// On Linux the header is inert apart from scepter_fseek64(): no name is redirected.
//
// Rules for this file:
//   * Every system header the scepter source includes is included in section 1, while
//     the names still mean what the platform says. transform.py (guard G2) stops the
//     build when upstream starts using a header that is missing here.
//   * Never include <windows.h> here: it defines macros (min, max, near, ERROR ...)
//     that would rewrite upstream code. Windows calls live in scepter_port.cpp only.
#ifndef SCEPTER_PORT_H
#define SCEPTER_PORT_H
#ifdef __cplusplus

#ifdef _WIN32
// MinGW's own printf family: correctly rounded and identical on every Windows
// version, where the system one rounds exact halves differently before Windows 10
// 2004. Also passed as -D by the recipe; set here so the header is enough by itself.
#  ifndef __USE_MINGW_ANSI_STDIO
#    define __USE_MINGW_ANSI_STDIO 1
#  endif
#endif

// ---- 1. every system header upstream uses, before any name is redirected ----------
#include <algorithm>
#include <array>
#include <atomic>
#include <cctype>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdarg>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <deque>
#include <fstream>
#include <iostream>
#include <iterator>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <type_traits>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#ifdef _WIN32
#  include <direct.h>
#  include <fcntl.h>
#  include <io.h>
#  include <process.h>
#endif
#include <zlib.h>

// ---- 2. a seek that takes a 64-bit offset on every platform (transformation T1) ----
inline int scepter_fseek64(std::FILE* f, long long offset, int whence) {
#ifdef _WIN32
    return _fseeki64(f, offset, whence);
#else
    return fseeko(f, static_cast<off_t>(offset), whence);
#endif
}

// ---- 3. the hidden self-test (scepter_selftest.cpp), started from scepter_port.cpp --
// Runs when the environment variable SCEPTER_PORT_SELFTEST is exactly "1".
int scepter_port_selftest(int argc, char** argv);
// What the Windows main does to its arguments before upstream reads them: '\' becomes
// '/' in paths (scepter_port.cpp says where not). In place; argv[0] is not touched.
void scepter_port_normalize_arguments(int argc, char** argv);
// Helpers of the self-test that need the operating system (scepter_port.cpp).
// make_big_file: a new sparse file with `marker` at `offset`; 0 on success, 1 cannot
// create, 2 the file system has no sparse files, 3 cannot write.
int scepter_port_make_big_file(const char* path, long long offset, const char* marker);
int scepter_port_remove_dir(const char* path);
long long scepter_port_process_id();
typedef void (*scepter_port_probe_fn)(const char* key, const char* value, int pass);
// Probes only one platform has. `parent`/`leaf_utf8` name a folder the self-test made
// through the narrow file calls; `leaf_utf8` is UTF-8 whatever the code page is.
void scepter_port_platform_probes(scepter_port_probe_fn emit, const char* parent,
                                  const char* leaf_utf8);

#ifdef _WIN32
// ---- 4. Windows: replacement declarations ---------------------------------------------
// A struct and a function may share a name in C++, which is what lets one macro redirect
// both `struct stat` and `::stat()`.
struct scepter_stat {
    unsigned long long st_dev;   // volume serial number
    unsigned long long st_ino;   // file index: a real identity, where the C runtime gives 0
    unsigned int st_mode;        // S_IFREG / S_IFDIR / S_IFCHR and permission bits
    long long st_size;           // 64 bits whatever the C runtime's default is
};
int scepter_stat(const char* path, struct scepter_stat* out);
int scepter_mkdir(const char* path, int mode);
long long scepter_readlink(const char* path, char* buf, size_t cap);
int scepter_isatty(int fd);
int scepter_fileno(std::FILE* f);
struct tm* scepter_localtime_r(const time_t* t, struct tm* out);
struct tm* scepter_gmtime_r(const time_t* t, struct tm* out);
int scepter_main(int argc, char** argv);

#  ifndef SCEPTER_PORT_IMPL
// ---- 5. Windows: the redirections ------------------------------------------------------
#    undef stat
#    undef mkdir
#    undef readlink
#    undef isatty
#    undef fileno
#    undef localtime_r
#    undef gmtime_r
#    define stat scepter_stat
#    define mkdir scepter_mkdir
#    define readlink scepter_readlink
#    define isatty scepter_isatty
#    define fileno scepter_fileno
#    define localtime_r scepter_localtime_r
#    define gmtime_r scepter_gmtime_r
#    define main scepter_main
#  endif  // SCEPTER_PORT_IMPL
#endif  // _WIN32

#endif  // __cplusplus
#endif  // SCEPTER_PORT_H
