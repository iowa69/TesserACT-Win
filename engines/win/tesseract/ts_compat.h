// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// ts_compat.h -- compatibility header of the TesserACT Windows port.
//
// Force-included (g++ -include ts_compat.h) into every translation unit of the engine and of its
// tests, on BOTH targets, so that one transformed source tree builds everywhere:
//
//   Linux    the header is inert. It defines only the two macros that the scripted rules R1 and R2
//            (win_rules.py) put into src/p2_provenance.cpp, and each expands to exactly the text
//            the rule replaced. Nothing is included, nothing is remapped.
//   Windows  (MinGW-w64, UCRT) the POSIX names the engine uses and the Windows C runtime lacks or
//            gets wrong are supplied here; their bodies are in ts_compat.cpp.
//
// Rules for the Windows half:
//   * Remap a name with a macro ONLY if the engine never uses that identifier for something of its
//     own. Checked for 1.6.2: stat, environ, localtime_r, open_memstream, fclose, popen, pclose
//     (and freopen, which only the tests call).
//     Never remap unlink (UnitigGraph::unlink), getline (std::getline), open, close, read, write.
//   * Never include <windows.h> or <winsock2.h> from here: they define `near`, `far`, `min` and
//     `max` as macros and the engine has variables named `near`. Win32 calls live in ts_compat.cpp.
//   * Every system header that mentions a remapped name is included BEFORE the macros below.
#pragma once

#ifndef _WIN32

// ---- Linux and every other POSIX system: the upstream text, nothing else ------------------------
#define TS_COMPAT_SELF_EXE "/proc/self/exe"
#define TS_COMPAT_CMDLINE_STREAM(var) std::ifstream var("/proc/self/cmdline", std::ios::binary)

#else  // _WIN32

// ---- feature macros (also given on the command line by recipe.py; these are the fallback) -------
#ifndef __USE_MINGW_ANSI_STDIO
#  define __USE_MINGW_ANSI_STDIO 1      // mingw-w64's own printf/scanf: %zu, %lld, glibc-like %.4f rounding
#endif
#ifndef _FILE_OFFSET_BITS
#  define _FILE_OFFSET_BITS 64          // 64-bit off_t, fseeko, ftello
#endif

#include <sys/types.h>
#include <sys/stat.h>
#include <direct.h>
#include <errno.h>
#include <fcntl.h>
#include <io.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <climits>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

// ---- 1. stat(): real file identity, 64-bit size, trailing separator tolerated -------------------
// The C runtime's stat reports st_ino == 0 for every file, so the engine's "same file given twice"
// test (src/main.cpp sameFile, src/seqio.cpp) would refuse every paired run. The engine only ever
// writes `struct stat x;` and `::stat(path, &x)`, so one macro covers the type and the function.
struct ts_compat_stat {
    unsigned long long st_dev;      // volume serial number
    unsigned long long st_ino;      // file identifier of the volume (never 0 for an existing file)
    unsigned int st_mode;           // S_IFREG or S_IFDIR plus permission bits, as <sys/stat.h> spells them
    int st_nlink;
    long long st_size;              // 64 bits: a FASTQ of 2 GiB or more is still found
    time_t st_atime;
    time_t st_mtime;
    time_t st_ctime;
};
int ts_compat_stat(const char* path, struct ts_compat_stat* out);
#ifdef stat
#  undef stat                       // mingw-w64 makes it a macro (_stat64) under _FILE_OFFSET_BITS=64
#endif
#define stat ts_compat_stat

// ---- 2. environ: `::environ` must stay an expression (UCRT's macro breaks under `::`) -----------
extern "C" char** ts_compat_environ(void);
#ifdef environ
#  undef environ
#endif
#define environ ts_compat_environ()

// ---- 3. localtime_r ------------------------------------------------------------------------------
struct tm* ts_compat_localtime_r(const time_t* t, struct tm* out);
#ifdef localtime_r
#  undef localtime_r
#endif
#define localtime_r ts_compat_localtime_r

// ---- 4. open_memstream ----------------------------------------------------------------------------
// No Windows counterpart. Emulated with a temporary file; the buffer is produced when the stream is
// closed, so fclose() goes through ts_compat_fclose(). Returning nullptr instead would be enough for
// the engine (src/assembler.cpp) but tests/test_deep_norm.cpp dereferences the result.
std::FILE* ts_compat_open_memstream(char** buf, size_t* len);
int ts_compat_fclose(std::FILE* f);
namespace std { using ::ts_compat_fclose; }     // `std::fclose(f)` still resolves after the macro
#define open_memstream ts_compat_open_memstream
#ifdef fclose
#  undef fclose
#endif
#define fclose ts_compat_fclose

// ---- 5. the provenance block (rules R1 and R2 of win_rules.py) -----------------------------------
namespace ts_compat {
std::string selfExePath();                      // full path of the running program
std::string procSelfCmdline();                  // argv joined by NUL bytes, as /proc/self/cmdline holds it
}
#define TS_COMPAT_SELF_EXE (::ts_compat::selfExePath())
#define TS_COMPAT_CMDLINE_STREAM(var) std::istringstream var(::ts_compat::procSelfCmdline(), std::ios::binary)

// ---- 6. the three memory queries of src/util.cpp ---------------------------------------------------
// They read /proc and call sysconf/getrusage. src/util.cpp alone is compiled with -DTS_COMPAT_UTIL_TU,
// which renames ITS copies out of the way; ts_compat.cpp defines the real
// ts::util::{current,total,peak}MemoryBytes(). recipe.py checks at staging that the three upstream
// definitions are still spelled this way.
#ifdef TS_COMPAT_UTIL_TU
#  define currentMemoryBytes currentMemoryBytes_posix_unused
#  define totalMemoryBytes totalMemoryBytes_posix_unused
#  define peakMemoryBytes peakMemoryBytes_posix_unused
#endif

// ---- 7. POSIX functions MinGW-w64 does not have (bodies in ts_compat.cpp) -------------------------
int mkdir(const char* path, int mode);          // a C++ overload beside the runtime's one-argument mkdir
char* realpath(const char* path, char* resolved);
extern "C" ssize_t getline(char** line, size_t* cap, FILE* stream);
long sysconf(int name);                         // only the two names below
#ifndef _SC_PAGESIZE
#  define _SC_PAGESIZE 30
#endif
#ifndef _SC_PHYS_PAGES
#  define _SC_PHYS_PAGES 85
#endif
// src/p2_provenance.cpp and the realpath above use char[PATH_MAX]; the runtime's 260 is too small
// for a long UTF-8 path. 4096 is the Linux value.
#ifdef PATH_MAX
#  undef PATH_MAX
#endif
#define PATH_MAX 4096

// ---- 8. --map-polish -------------------------------------------------------------------------------
// It runs bwa or bowtie2 through a POSIX shell command line. Not available on Windows: popen() says
// so on stderr and fails, the engine prints its own "alignment polishing skipped" and carries on.
std::FILE* ts_compat_popen(const char* command, const char* mode);
int ts_compat_pclose(std::FILE* stream);
#ifdef popen
#  undef popen
#endif
#ifdef pclose
#  undef pclose
#endif
#define popen ts_compat_popen
#define pclose ts_compat_pclose

// ---- 9. used by the upstream test programs only (bodies in ts_compat_tests.cpp) --------------------
int setenv(const char* name, const char* value, int overwrite);
int unsetenv(const char* name);
char* mkdtemp(char* templ);
// tests/test_v3_emit_post.cpp silences stderr with freopen("/dev/null", ...); on Windows that is NUL.
// The engine does not call freopen (guard G8 of win_rules.py); if it ever does, linking fails here.
std::FILE* ts_compat_freopen(const char* path, const char* mode, std::FILE* stream);
namespace std { using ::ts_compat_freopen; }
#ifdef freopen
#  undef freopen
#endif
#define freopen ts_compat_freopen

#endif  // _WIN32
