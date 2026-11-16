// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// ts_compat.cpp -- bodies for ts_compat.h, linked into the engine and into every test program.
//
//   Windows  the POSIX replacements, the three memory queries and the start-up settings.
//   Linux    nothing but the hook of the hidden self-test (TS_COMPAT_SELFTEST=1) and one helper
//            for it, so that the same self-test runs on both targets and its output can be compared.
//
// This file must stay outside src/: tests/test_envflags_retrofit.cpp scans src/ for raw getenv().
#include "ts_compat.h"
#include "ts_selftest.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

// =====================================================================================================
#ifdef _WIN32
// =====================================================================================================

// From here on this file needs the real names.
#undef stat
#undef environ
#undef localtime_r
#undef open_memstream
#undef fclose
#undef popen
#undef pclose

#ifndef WIN32_LEAN_AND_MEAN
#  define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#  define NOMINMAX
#endif
#ifndef PSAPI_VERSION
#  define PSAPI_VERSION 2       // GetProcessMemoryInfo comes from kernel32.dll: no psapi.dll import
#endif
#include <windows.h>
#include <psapi.h>
#include <winioctl.h>

#include <sys/resource.h>       // the stub in include/sys

#include <atomic>
#include <map>
#include <mutex>
#include <string>
#include <vector>

// ---- start-up defaults read by the C runtime before main() -------------------------------------------
// crtexe.c copies _fmode into the runtime (the default translation mode of every fopen() and fstream
// without 'b' or 't') and passes _dowildcard to the argv parser. Defining them here replaces the
// library's own definitions: binary files as on Linux, and no wildcard expansion of the arguments
// (a read file named "a*.fastq" stays one argument, as it does when a Linux shell quotes it).
#ifdef _fmode
#  undef _fmode
#endif
extern "C" {
int _fmode = _O_BINARY;
int _dowildcard = 0;
}

namespace {

const size_t kWidePathMax = 32768;      // the longest path Windows accepts, in UTF-16 units

// Narrow strings are in the process's active code page: UTF-8 through the manifest on Windows 10
// 1903 and later, the PC's own code page before that. CP_ACP follows it, exactly as the runtime's
// own narrow file functions (fopen, open, _stat) do, so every path is read the same way everywhere.
std::wstring widen(const char* s) {
    if (!s || !*s) return std::wstring();
    const int n = MultiByteToWideChar(CP_ACP, 0, s, -1, nullptr, 0);
    if (n <= 1) return std::wstring();
    std::wstring w(static_cast<size_t>(n), L'\0');
    if (MultiByteToWideChar(CP_ACP, 0, s, -1, &w[0], n) <= 0) return std::wstring();
    w.resize(static_cast<size_t>(n) - 1);
    return w;
}

std::string narrow(const wchar_t* w) {
    if (!w || !*w) return std::string();
    const int n = WideCharToMultiByte(CP_ACP, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 1) return std::string();
    std::string s(static_cast<size_t>(n), '\0');
    if (WideCharToMultiByte(CP_ACP, 0, w, -1, &s[0], n, nullptr, nullptr) <= 0) return std::string();
    s.resize(static_cast<size_t>(n) - 1);
    return s;
}

bool isSeparator(wchar_t c) { return c == L'/' || c == L'\\'; }

time_t fileTimeToUnix(const FILETIME& ft) {
    ULARGE_INTEGER u;
    u.LowPart = ft.dwLowDateTime;
    u.HighPart = ft.dwHighDateTime;
    return static_cast<time_t>(static_cast<long long>(u.QuadPart / 10000000ULL) - 11644473600LL);
}

unsigned long long fnv1a(const void* data, size_t n) {
    const unsigned char* p = static_cast<const unsigned char*>(data);
    unsigned long long h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; ++i) { h ^= p[i]; h *= 1099511628211ULL; }
    return h;
}

int errnoFromWin32(DWORD err) {
    switch (err) {
        case ERROR_FILE_NOT_FOUND:
        case ERROR_PATH_NOT_FOUND:
        case ERROR_INVALID_NAME:
        case ERROR_INVALID_DRIVE:
        case ERROR_BAD_NETPATH:
        case ERROR_BAD_NET_NAME:
        case ERROR_BAD_PATHNAME:
        case ERROR_NOT_READY:
            return ENOENT;
        case ERROR_ACCESS_DENIED:
        case ERROR_SHARING_VIOLATION:
        case ERROR_LOCK_VIOLATION:
            return EACCES;
        case ERROR_FILENAME_EXCED_RANGE:
            return ENAMETOOLONG;
        case ERROR_NOT_ENOUGH_MEMORY:
        case ERROR_OUTOFMEMORY:
            return ENOMEM;
        default:
            return EIO;
    }
}

void fillStat(struct ts_compat_stat* out, DWORD attrs, DWORD sizeHigh, DWORD sizeLow,
              const FILETIME& access, const FILETIME& write, const FILETIME& create) {
    if (attrs & FILE_ATTRIBUTE_DIRECTORY) out->st_mode = _S_IFDIR | 0755;
    else out->st_mode = _S_IFREG | ((attrs & FILE_ATTRIBUTE_READONLY) ? 0444 : 0644);
    out->st_nlink = 1;
    out->st_size = static_cast<long long>((static_cast<unsigned long long>(sizeHigh) << 32) | sizeLow);
    out->st_atime = fileTimeToUnix(access);
    out->st_mtime = fileTimeToUnix(write);
    out->st_ctime = fileTimeToUnix(create);
}

// Identity of last resort, for a file system that reports no file identifier (some network shares
// and virtual-machine shared folders): a hash of the final, case-folded path. Two spellings of one
// file still agree; two different files still differ, which is what keeps a paired run possible.
unsigned long long pathIdentity(HANDLE h, const std::wstring& given) {
    std::vector<wchar_t> buf(kWidePathMax);
    std::wstring full;
    if (h != INVALID_HANDLE_VALUE) {
        const DWORD n = GetFinalPathNameByHandleW(h, buf.data(), static_cast<DWORD>(buf.size()),
                                                  FILE_NAME_NORMALIZED | VOLUME_NAME_GUID);
        if (n > 0 && n < buf.size()) full.assign(buf.data(), n);
    }
    if (full.empty()) {
        const DWORD n = GetFullPathNameW(given.c_str(), static_cast<DWORD>(buf.size()), buf.data(), nullptr);
        if (n > 0 && n < buf.size()) full.assign(buf.data(), n);
        else full = given;
    }
    std::wstring lower(full.size(), L'\0');
    const int m = full.empty() ? 0
        : LCMapStringEx(LOCALE_NAME_INVARIANT, LCMAP_LOWERCASE, full.c_str(), static_cast<int>(full.size()),
                        &lower[0], static_cast<int>(lower.size()), nullptr, nullptr, 0);
    if (m <= 0) lower = full;
    const unsigned long long hash = fnv1a(lower.data(), lower.size() * sizeof(wchar_t));
    return hash ? hash : 1;
}

void quietInvalidParameter(const wchar_t*, const wchar_t*, const wchar_t*, unsigned int, uintptr_t) {
    // The runtime function that was handed the bad argument now returns its error code, as its
    // POSIX counterpart does, instead of ending the program with a crash report.
}

void binaryMode(std::FILE* stream) {
    const int fd = _fileno(stream);
    if (fd >= 0) _setmode(fd, _O_BINARY);       // a program started without that handle has fd -2
}

// Process-wide defaults the engine assumes. Priority 101: before every ordinary static initialiser.
__attribute__((constructor(101))) void tsCompatStartup() {
    _set_invalid_parameter_handler(quietInvalidParameter);
    // No modal box, ever: the engine runs unattended under a window program. A crash ends the
    // process with its exception code; abort() (src/envflags.cpp) exits with status 3.
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    _set_abort_behavior(0, _WRITE_ABORT_MSG | _CALL_REPORTFAULT);
    _set_error_mode(_OUT_TO_STDERR);
    // Every fopen() and fstream without 'b' is binary (scaffolds.agp, replicons.tsv, p2_edits.tsv, ...)
    // and so is the log: no "\r\n" anywhere. _fmode above already says so; this is the documented call.
    _set_fmode(_O_BINARY);
    binaryMode(stdin);
    binaryMode(stdout);
    binaryMode(stderr);
    // src/main.cpp looks for bundled models under $HOME/.tesseract/models.
    const char* home = std::getenv("HOME");
    if (!home || !*home) {
        const char* profile = std::getenv("USERPROFILE");
        if (profile && *profile) _putenv_s("HOME", profile);
    }
}

}  // namespace

// ---- stat ---------------------------------------------------------------------------------------------
int ts_compat_stat(const char* path, struct ts_compat_stat* out) {
    if (!path || !out) { errno = EFAULT; return -1; }
    std::wstring w = widen(path);
    if (w.empty()) { errno = ENOENT; return -1; }
    // "dir/" and "dir\" stat like "dir": util::makeDirs asks with the trailing separator. A root
    // ("C:\", "\") keeps its own.
    while (w.size() > 1 && isSeparator(w.back()) && w[w.size() - 2] != L':') w.pop_back();
    std::memset(out, 0, sizeof *out);

    WIN32_FILE_ATTRIBUTE_DATA fad;
    // No access beyond reading attributes, every kind of sharing: this opens a file another program
    // has locked, and (FILE_FLAG_BACKUP_SEMANTICS) a directory. Reparse points are followed, as stat() does.
    HANDLE h = CreateFileW(w.c_str(), FILE_READ_ATTRIBUTES, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, nullptr);
    BY_HANDLE_FILE_INFORMATION bi;
    if (h == INVALID_HANDLE_VALUE || !GetFileInformationByHandle(h, &bi)) {
        const DWORD err = GetLastError();
        if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
        // A few system files and devices cannot be opened at all but still have attributes.
        if (GetFileAttributesExW(w.c_str(), GetFileExInfoStandard, &fad)) {
            fillStat(out, fad.dwFileAttributes, fad.nFileSizeHigh, fad.nFileSizeLow,
                     fad.ftLastAccessTime, fad.ftLastWriteTime, fad.ftCreationTime);
            out->st_ino = pathIdentity(INVALID_HANDLE_VALUE, w);
            return 0;
        }
        errno = errnoFromWin32(err);
        return -1;
    }
    fillStat(out, bi.dwFileAttributes, bi.nFileSizeHigh, bi.nFileSizeLow,
             bi.ftLastAccessTime, bi.ftLastWriteTime, bi.ftCreationTime);
    out->st_nlink = static_cast<int>(bi.nNumberOfLinks ? bi.nNumberOfLinks : 1);
    out->st_dev = bi.dwVolumeSerialNumber;
    out->st_ino = (static_cast<unsigned long long>(bi.nFileIndexHigh) << 32) | bi.nFileIndexLow;
    // ReFS (a Windows 11 "Dev Drive", for one) identifies a file with 128 bits and its 64-bit index
    // is not unique. Ask for the full identifier; fold it only when the upper half is in use.
    FILE_ID_INFO idi;
    if (GetFileInformationByHandleEx(h, FileIdInfo, &idi, sizeof idi)) {
        unsigned long long lo = 0, hi = 0;
        std::memcpy(&lo, idi.FileId.Identifier, 8);
        std::memcpy(&hi, idi.FileId.Identifier + 8, 8);
        if (lo != 0 || hi != 0) {
            out->st_dev = idi.VolumeSerialNumber;
            out->st_ino = hi == 0 ? lo : fnv1a(idi.FileId.Identifier, sizeof idi.FileId.Identifier);
        }
    }
    if (out->st_ino == 0) out->st_ino = pathIdentity(h, w);
    CloseHandle(h);
    return 0;
}

// ---- environ, localtime_r -----------------------------------------------------------------------------
extern "C" char** ts_compat_environ(void) { return _environ; }

struct tm* ts_compat_localtime_r(const time_t* t, struct tm* out) {
    if (!t || !out) return nullptr;
    return localtime_s(out, t) == 0 ? out : nullptr;
}

// ---- open_memstream ------------------------------------------------------------------------------------
namespace {
struct MemStream { char** buf; size_t* len; };
std::mutex gMemMutex;
std::map<std::FILE*, MemStream> gMemStreams;
std::atomic<int> gMemOpen{0};
}  // namespace

std::FILE* ts_compat_open_memstream(char** buf, size_t* len) {
    if (!buf || !len) { errno = EINVAL; return nullptr; }
    std::FILE* f = std::tmpfile();          // UCRT: deleted when closed, in the user's temp directory
    if (!f) return nullptr;
    *buf = nullptr;
    *len = 0;
    std::lock_guard<std::mutex> lock(gMemMutex);
    gMemStreams[f] = MemStream{buf, len};
    gMemOpen.fetch_add(1);
    return f;
}

int ts_compat_fclose(std::FILE* f) {
    if (gMemOpen.load() != 0) {             // one atomic load on the ordinary path
        MemStream m{nullptr, nullptr};
        {
            std::lock_guard<std::mutex> lock(gMemMutex);
            const auto it = gMemStreams.find(f);
            if (it != gMemStreams.end()) { m = it->second; gMemStreams.erase(it); gMemOpen.fetch_sub(1); }
        }
        if (m.buf) {
            std::fflush(f);
            const long long n = _ftelli64(f);
            std::rewind(f);
            const size_t want = n > 0 ? static_cast<size_t>(n) : 0;
            char* p = static_cast<char*>(std::malloc(want + 1));
            const size_t got = (p && want) ? std::fread(p, 1, want, f) : 0;
            if (p) p[got] = '\0';
            *m.buf = p;
            *m.len = got;
        }
    }
    return std::fclose(f);
}

// ---- the provenance block ------------------------------------------------------------------------------
namespace ts_compat {

std::string selfExePath() {
    std::vector<wchar_t> buf(kWidePathMax);
    const DWORD n = GetModuleFileNameW(nullptr, buf.data(), static_cast<DWORD>(buf.size()));
    if (n == 0 || n >= buf.size()) return std::string();
    buf[n] = L'\0';
    return narrow(buf.data());
}

// The runtime's own argv: what main() received, in the active code page, never wildcard-expanded.
std::string procSelfCmdline() {
    std::string out;
    const int argc = __argc;
    char** argv = __argv;
    for (int i = 0; argv && i < argc && argv[i]; ++i) {
        out += argv[i];
        out.push_back('\0');
    }
    return out;
}

const char* platformName() { return "windows"; }

bool makeSparseFile(const char* path, long long size) {
    const std::wstring w = widen(path);
    if (w.empty()) return false;
    HANDLE h = CreateFileW(w.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return false;
    DWORD returned = 0;
    LARGE_INTEGER end;
    end.QuadPart = size;
    // Sparse first: extending an ordinary file would really allocate (and later zero) all of it.
    const bool ok = DeviceIoControl(h, FSCTL_SET_SPARSE, nullptr, 0, nullptr, 0, &returned, nullptr) &&
                    SetFilePointerEx(h, end, nullptr, FILE_BEGIN) && SetEndOfFile(h);
    CloseHandle(h);
    if (!ok) DeleteFileW(w.c_str());
    return ok;
}

}  // namespace ts_compat

// ---- mkdir(path, mode) ----------------------------------------------------------------------------------
// util::makeDirs creates the parents of a path itself, but only where it finds '/'. A path written
// with backslashes reaches this function whole, so missing parents are created here.
int mkdir(const char* path, int) {
    const std::wstring w = widen(path);
    if (w.empty()) { errno = ENOENT; return -1; }
    if (_wmkdir(w.c_str()) == 0) return 0;
    if (errno != ENOENT) return -1;
    for (size_t i = 1; i < w.size(); ++i) {
        if (!isSeparator(w[i]) || isSeparator(w[i - 1]) || w[i - 1] == L':') continue;
        const std::wstring parent = w.substr(0, i);
        if (GetFileAttributesW(parent.c_str()) != INVALID_FILE_ATTRIBUTES) continue;
        _wmkdir(parent.c_str());        // a prefix that is not a directory (\\server) fails; the last call decides
    }
    return _wmkdir(w.c_str());
}

// ---- realpath ---------------------------------------------------------------------------------------------
char* realpath(const char* path, char* resolved) {
    struct ts_compat_stat st;
    if (ts_compat_stat(path, &st) != 0) return nullptr;        // POSIX realpath fails on a missing file
    const std::wstring w = widen(path);
    std::vector<wchar_t> buf(kWidePathMax);
    const DWORD n = GetFullPathNameW(w.c_str(), static_cast<DWORD>(buf.size()), buf.data(), nullptr);
    if (n == 0 || n >= buf.size()) { errno = ENAMETOOLONG; return nullptr; }
    const std::string s = narrow(buf.data());
    if (s.empty()) { errno = EILSEQ; return nullptr; }
    if (!resolved) {
        resolved = static_cast<char*>(std::malloc(s.size() + 1));
        if (!resolved) { errno = ENOMEM; return nullptr; }
    } else if (s.size() >= PATH_MAX) {
        errno = ENAMETOOLONG;
        return nullptr;
    }
    std::memcpy(resolved, s.c_str(), s.size() + 1);
    return resolved;
}

// ---- --map-polish -------------------------------------------------------------------------------------------
std::FILE* ts_compat_popen(const char*, const char*) {
    std::fprintf(stderr, "      note: --map-polish is not available in the Windows build of TesserACT: it needs\n"
                         "            bwa or bowtie2 and a POSIX shell. The assembly continues without it.\n");
    errno = ENOSYS;
    return nullptr;
}

int ts_compat_pclose(std::FILE*) { errno = ECHILD; return -1; }

// ---- getline -------------------------------------------------------------------------------------------------
extern "C" ssize_t getline(char** line, size_t* cap, FILE* stream) {
    if (!line || !cap || !stream) { errno = EINVAL; return -1; }
    if (!*line) *cap = 0;
    size_t n = 0;
    for (;;) {
        const int c = std::fgetc(stream);
        if (c == EOF) break;
        if (n + 2 > *cap) {
            const size_t want = *cap ? *cap * 2 : 256;
            char* p = static_cast<char*>(std::realloc(*line, want));
            if (!p) { errno = ENOMEM; return -1; }
            *line = p;
            *cap = want;
        }
        (*line)[n++] = static_cast<char>(c);
        if (c == '\n') break;
    }
    if (n == 0) return -1;
    (*line)[n] = '\0';
    return static_cast<ssize_t>(n);
}

// ---- sysconf, getrusage: what the renamed POSIX bodies of src/util.cpp still call ----------------------------
long sysconf(int name) {
    SYSTEM_INFO si;
    GetSystemInfo(&si);
    if (name == _SC_PAGESIZE) return static_cast<long>(si.dwPageSize);
    if (name == _SC_PHYS_PAGES) {
        MEMORYSTATUSEX ms;
        ms.dwLength = sizeof ms;
        if (!GlobalMemoryStatusEx(&ms) || si.dwPageSize == 0) return -1;
        return static_cast<long>(ms.ullTotalPhys / si.dwPageSize);      // 2^31 pages of 4 KiB are 8 TiB
    }
    errno = EINVAL;
    return -1;
}

int getrusage(int, struct rusage* out) {
    if (!out) { errno = EFAULT; return -1; }
    std::memset(out, 0, sizeof *out);
    PROCESS_MEMORY_COUNTERS pmc;
    std::memset(&pmc, 0, sizeof pmc);
    pmc.cb = sizeof pmc;
    if (!GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof pmc)) return -1;
    out->ru_maxrss = static_cast<long>(pmc.PeakWorkingSetSize / 1024);      // kilobytes, as on Linux
    return 0;
}

// ---- the three memory queries (declared in src/util.h) ---------------------------------------------------------
// The --max-memory guard (src/counter.cpp) compares currentMemoryBytes() with the budget, and the
// default budget is 80 % of totalMemoryBytes(). A zero here would switch the guard off silently.
namespace ts { namespace util {

long long currentMemoryBytes() {
    PROCESS_MEMORY_COUNTERS pmc;
    std::memset(&pmc, 0, sizeof pmc);
    pmc.cb = sizeof pmc;
    return GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof pmc) ? static_cast<long long>(pmc.WorkingSetSize) : 0;
}

long long peakMemoryBytes() {
    PROCESS_MEMORY_COUNTERS pmc;
    std::memset(&pmc, 0, sizeof pmc);
    pmc.cb = sizeof pmc;
    return GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof pmc) ? static_cast<long long>(pmc.PeakWorkingSetSize) : 0;
}

long long totalMemoryBytes() {
    MEMORYSTATUSEX ms;
    std::memset(&ms, 0, sizeof ms);
    ms.dwLength = sizeof ms;
    return GlobalMemoryStatusEx(&ms) ? static_cast<long long>(ms.ullTotalPhys) : 0;
}

} }  // namespace ts::util

// =====================================================================================================
#else   // not Windows
// =====================================================================================================
#include <fcntl.h>
#include <unistd.h>

namespace ts_compat {

const char* platformName() { return "linux"; }

bool makeSparseFile(const char* path, long long size) {
    const int fd = ::open(path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return false;
    const bool ok = ::ftruncate(fd, static_cast<off_t>(size)) == 0;
    ::close(fd);
    if (!ok) ::unlink(path);
    return ok;
}

}  // namespace ts_compat

#endif

// =====================================================================================================
// Both targets: the hidden self-test. With TS_COMPAT_SELFTEST=1 the program prints the probes and
// exits before main() is reached; without it this costs one getenv().
// =====================================================================================================
namespace {

struct SelfTestHook {
    SelfTestHook() {
        const char* wanted = std::getenv("TS_COMPAT_SELFTEST");
        if (!wanted || std::strcmp(wanted, "1") != 0) return;
        const int status = ts_compat::selfTest();
        std::fflush(stdout);
        std::fflush(stderr);
        std::_Exit(status);
    }
} selfTestHook;

}  // namespace
