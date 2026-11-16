// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IOWA-BioTech - Giovanni Lorenzin
//
// scepter_port.cpp -- the operating-system half of the native Windows port of scepter.
// Not upstream code. Compiled WITHOUT the force-include of scepter_port.h (it includes
// the header itself, with SCEPTER_PORT_IMPL set so that no name is redirected here).
//
// Windows: the functions scepter_port.h redirects upstream to, and the real main():
// it prepares the process (no crash dialogs, binary streams, console), runs the hidden
// self-test when asked, turns backslashes in path arguments into slashes, and only then
// calls upstream's main (renamed scepter_main by the header).
// Linux: nothing of that. Upstream's main stays main; this file adds only the start of
// the self-test, from a constructor that does nothing unless SCEPTER_PORT_SELFTEST=1.
#define SCEPTER_PORT_IMPL
#include "scepter_port.h"

namespace {

bool selftestRequested() {
    const char* e = std::getenv("SCEPTER_PORT_SELFTEST");
    return e && e[0] == '1' && e[1] == '\0';
}

// ---- arguments (used by the Windows main; compiled everywhere so the self-test can
//      check the rules on Linux too) ---------------------------------------------------
// Upstream splits paths on '/' only (output folders, --out-dir, split files), so a path
// typed the Windows way would not have its folder created. Every '\' in an argument
// becomes '/', which Windows accepts everywhere, except:
//   * the value of an option that holds free text (kTextOptions; transform.py, guard G3,
//     checks this list against upstream's option table at every build);
//   * an argument that holds a \\?\ or \\.\ path, which Windows takes literally.
// The program's own name (argv[0]) is left as it is.
const char* const kTextOptions[] = {"report_title", "umi_prefix", "umi_delim"};
const char kTextShort = 'R';   // -R TITLE
// Every short option that takes a value (guard G3 checks this list too). Upstream lets
// short options be bundled ("-AR title", "-3oOUT"): the first of these letters in an
// argument ends the switches, and what follows it, or else the next argument, is its value.
const char kValueShorts[] = "iIoOzaftbFTBWMqunelYPjhRsSdw";

// Long names are matched the way upstream does: case ignored, '-' the same as '_'.
bool isTextOption(const std::string& name) {
    std::string norm;
    for (char c : name)
        norm += (c == '-') ? '_' : static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    for (const char* known : kTextOptions)
        if (norm == known) return true;
    return false;
}

void slashes(char* s) {
    if (std::strstr(s, "\\\\?\\") || std::strstr(s, "\\\\.\\")) return;
    for (; *s; ++s) if (*s == '\\') *s = '/';
}

}  // namespace

void scepter_port_normalize_arguments(int argc, char** argv) {
    bool valueIsText = false, optionsEnded = false;
    for (int i = 1; i < argc; ++i) {
        char* a = argv[i];
        if (!a) continue;
        if (valueIsText) { valueIsText = false; continue; }
        if (!optionsEnded && a[0] == '-' && a[1] == '-') {
            if (a[2] == '\0') { optionsEnded = true; continue; }
            char* eq = std::strchr(a, '=');
            const std::string name = eq ? std::string(a + 2, eq) : std::string(a + 2);
            if (isTextOption(name)) { valueIsText = (eq == nullptr); continue; }
            if (eq) slashes(eq + 1);
            continue;
        }
        if (!optionsEnded && a[0] == '-' && a[1] != '\0') {
            for (char* c = a + 1; *c; ++c) {
                if (!std::strchr(kValueShorts, *c)) continue;          // a switch
                if (*c == kTextShort) valueIsText = (c[1] == '\0');    // "-R" then the title, or "-Rtitle"
                else if (c[1] != '\0') slashes(c + 1);                 // "-oOUT"; else the next argument
                break;
            }
            continue;
        }
        slashes(a);
    }
}

#ifdef _WIN32
// =========================================================================== Windows
#define WIN32_LEAN_AND_MEAN
#ifndef NOMINMAX
#  define NOMINMAX
#endif
#include <windows.h>
#include <winioctl.h>
#include <cerrno>
#include <cwchar>

#ifndef ENABLE_VIRTUAL_TERMINAL_PROCESSING
#  define ENABLE_VIRTUAL_TERMINAL_PROCESSING 0x0004
#endif

// The MinGW start-up code expands * and ? in arguments unless this is 0. A sample
// name is never a pattern: the arguments must arrive exactly as the caller wrote them.
extern "C" {
int _dowildcard = 0;
}

namespace {

// The code page the C runtime uses for narrow file names: UTF-8 when the manifest's
// activeCodePage is honoured (Windows 10 1903 and later), the system one before that.
UINT fileCodePage() { return AreFileApisANSI() ? CP_ACP : CP_OEMCP; }

bool widen(const std::string& s, UINT codePage, std::wstring& out) {
    out.clear();
    if (s.empty()) return true;
    const int n = MultiByteToWideChar(codePage, 0, s.data(), static_cast<int>(s.size()), nullptr, 0);
    if (n <= 0) return false;
    out.resize(static_cast<size_t>(n));
    return MultiByteToWideChar(codePage, 0, s.data(), static_cast<int>(s.size()), &out[0], n) == n;
}

// False when a character has no place in the code page: the result would name another file.
bool narrow(const std::wstring& w, std::string& out) {
    out.clear();
    if (w.empty()) return true;
    const UINT cp = fileCodePage();
    // UTF-8 allows neither the flag nor the "a character was lost" answer.
    const bool utf8 = (cp == CP_ACP ? GetACP() : cp == CP_OEMCP ? GetOEMCP() : cp) == CP_UTF8;
    const DWORD flags = utf8 ? 0 : WC_NO_BEST_FIT_CHARS;
    BOOL lossy = FALSE;
    const int n = WideCharToMultiByte(cp, flags, w.data(), static_cast<int>(w.size()), nullptr, 0,
                                      nullptr, utf8 ? nullptr : &lossy);
    if (n <= 0 || lossy) return false;
    out.resize(static_cast<size_t>(n));
    return WideCharToMultiByte(cp, flags, w.data(), static_cast<int>(w.size()), &out[0], n,
                               nullptr, utf8 ? nullptr : &lossy) == n && !lossy;
}

bool isSep(char c) { return c == '/' || c == '\\'; }

// Upstream asks about "dir/" while it creates folders. Keep the separator only where
// it is the whole meaning of the path: "/", "C:/" and the root of a \\?\ path.
std::string trimSlashes(const char* path) {
    std::string p(path ? path : "");
    while (p.size() > 1 && isSep(p.back())) {
        if (p.size() == 3 && p[1] == ':') break;
        if (p.size() == 7 && p.compare(0, 4, "\\\\?\\") == 0 && p[5] == ':') break;
        p.pop_back();
    }
    return p;
}

// "//server" and "//server/share" (either separator): they can be neither tested nor made.
bool isNetworkRoot(const std::string& p) {
    if (p.size() < 3 || !isSep(p[0]) || !isSep(p[1]) || isSep(p[2]) || p[2] == '?' || p[2] == '.')
        return false;
    int parts = 0;
    bool inPart = false;
    for (size_t i = 2; i < p.size(); ++i) {
        if (isSep(p[i])) { inPart = false; continue; }
        if (!inPart) { inPart = true; ++parts; }
    }
    return parts >= 1 && parts <= 2;
}

// An identity for a file whose volume gives none: a hash of its full path, upper-cased
// the way Windows compares names, so that two different paths never look like one file.
unsigned long long pathIdentity(const std::wstring& w) {
    std::wstring full(32768, L'\0');
    const DWORD n = GetFullPathNameW(w.c_str(), static_cast<DWORD>(full.size()), &full[0], nullptr);
    if (n > 0 && n < full.size()) full.resize(n); else full = w;
    std::wstring upper(full.size(), L'\0');
    if (!full.empty() &&
        LCMapStringW(LOCALE_INVARIANT, LCMAP_UPPERCASE, full.data(), static_cast<int>(full.size()),
                     &upper[0], static_cast<int>(upper.size())) == static_cast<int>(full.size()))
        full.swap(upper);
    unsigned long long h = 1469598103934665603ull;
    for (wchar_t c : full) {
        if (c == L'\\') c = L'/';
        h = (h ^ static_cast<unsigned long long>(c)) * 1099511628211ull;
    }
    return h | 1ull;
}

unsigned int modeOf(DWORD attributes) {
    unsigned int mode = (attributes & FILE_ATTRIBUTE_DIRECTORY) ? (S_IFDIR | 0111u) : S_IFREG;
    mode |= (attributes & FILE_ATTRIBUTE_READONLY) ? 0444u : 0666u;
    return mode;
}

}  // namespace

// stat(): 64-bit size and a real file identity. The C runtime's own stat reports
// st_ino = 0 for every file, so upstream's "same device and inode" test would call any
// two existing files on one drive the same file, and its default st_size is 32 bits.
int scepter_stat(const char* path, struct scepter_stat* out) {
    std::wstring w;
    if (!path || !out || !*path || !widen(trimSlashes(path), fileCodePage(), w)) {
        errno = ENOENT;
        return -1;
    }
    out->st_dev = 0;
    out->st_ino = 0;
    out->st_mode = 0;
    out->st_size = 0;
    // No access is asked for, so a file another program holds open can still be examined.
    HANDLE h = CreateFileW(w.c_str(), 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                           nullptr, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, nullptr);
    if (h != INVALID_HANDLE_VALUE) {
        const DWORD type = GetFileType(h);
        BY_HANDLE_FILE_INFORMATION bi;
        if (type == FILE_TYPE_DISK && GetFileInformationByHandle(h, &bi)) {
            CloseHandle(h);
            out->st_mode = modeOf(bi.dwFileAttributes);
            if (!(bi.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
                out->st_size = static_cast<long long>(
                    (static_cast<unsigned long long>(bi.nFileSizeHigh) << 32) | bi.nFileSizeLow);
            out->st_dev = bi.dwVolumeSerialNumber;
            out->st_ino = (static_cast<unsigned long long>(bi.nFileIndexHigh) << 32) | bi.nFileIndexLow;
            if (out->st_ino == 0) {            // some network and FAT volumes
                out->st_dev = 0;
                out->st_ino = pathIdentity(w);
            }
            return 0;
        }
        CloseHandle(h);
        if (type == FILE_TYPE_CHAR || type == FILE_TYPE_PIPE) {      // NUL, CON, a pipe
            out->st_mode = S_IFCHR | 0666u;
            out->st_ino = pathIdentity(w);
            return 0;
        }
    } else {
        const DWORD err = GetLastError();
        if (err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND || err == ERROR_INVALID_NAME) {
            errno = ENOENT;
            return -1;
        }
    }
    // It exists but cannot be opened (no permission, a locked system file).
    WIN32_FILE_ATTRIBUTE_DATA fad;
    if (!GetFileAttributesExW(w.c_str(), GetFileExInfoStandard, &fad)) {
        errno = ENOENT;
        return -1;
    }
    out->st_mode = modeOf(fad.dwFileAttributes);
    if (!(fad.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
        out->st_size = static_cast<long long>(
            (static_cast<unsigned long long>(fad.nFileSizeHigh) << 32) | fad.nFileSizeLow);
    out->st_ino = pathIdentity(w);
    return 0;
}

// mkdir(path, mode): MinGW's takes one argument. The mode has no meaning on Windows.
int scepter_mkdir(const char* path, int) {
    const std::string p = trimSlashes(path);
    std::wstring w;
    if (p.empty() || !widen(p, fileCodePage(), w)) {
        errno = ENOENT;
        return -1;
    }
    if (CreateDirectoryW(w.c_str(), nullptr)) return 0;
    const DWORD err = GetLastError();
    // Upstream creates a path one component at a time and stops at the first failure.
    // The server and share of a network path cannot be created: let the walk go on.
    if (isNetworkRoot(p)) return 0;
    errno = (err == ERROR_ALREADY_EXISTS || err == ERROR_FILE_EXISTS) ? EEXIST
          : (err == ERROR_PATH_NOT_FOUND || err == ERROR_FILE_NOT_FOUND) ? ENOENT : EACCES;
    return -1;
}

// readlink("/proc/self/exe"): upstream only ever asks where the running program is, to
// find files shipped beside it. Answered with forward slashes because upstream splits
// paths on '/'. Anything else, or a path this code page cannot spell, is "not a link".
long long scepter_readlink(const char* path, char* buf, size_t cap) {
    if (!path || !buf || cap == 0 || std::strcmp(path, "/proc/self/exe") != 0) {
        errno = EINVAL;
        return -1;
    }
    std::wstring w(1024, L'\0');
    for (;;) {
        const DWORD n = GetModuleFileNameW(nullptr, &w[0], static_cast<DWORD>(w.size()));
        if (n == 0) { errno = ENOENT; return -1; }
        if (n < w.size()) { w.resize(n); break; }
        if (w.size() >= 65536) { errno = ENAMETOOLONG; return -1; }
        w.assign(w.size() * 2, L'\0');
    }
    if (w.compare(0, 8, L"\\\\?\\UNC\\") == 0) w = L"\\\\" + w.substr(8);
    else if (w.compare(0, 4, L"\\\\?\\") == 0) w = w.substr(4);
    std::string s;
    if (!narrow(w, s) || s.size() > cap) { errno = ENAMETOOLONG; return -1; }
    for (char& c : s) if (c == '\\') c = '/';
    std::memcpy(buf, s.data(), s.size());
    return static_cast<long long>(s.size());
}

// isatty(): true only for a real console. The runtime's _isatty also says yes for NUL.
int scepter_isatty(int fd) {
    if (fd < 0 || fd > 2) return 0;
    HANDLE h = GetStdHandle(fd == 0 ? STD_INPUT_HANDLE : fd == 1 ? STD_OUTPUT_HANDLE : STD_ERROR_HANDLE);
    DWORD mode = 0;
    return (h != nullptr && h != INVALID_HANDLE_VALUE && GetConsoleMode(h, &mode)) ? 1 : 0;
}

int scepter_fileno(std::FILE* f) { return _fileno(f); }

struct tm* scepter_localtime_r(const time_t* t, struct tm* out) {
    return (t && out && localtime_s(out, t) == 0) ? out : nullptr;
}
struct tm* scepter_gmtime_r(const time_t* t, struct tm* out) {
    return (t && out && gmtime_s(out, t) == 0) ? out : nullptr;
}

namespace {

// ---- the console ---------------------------------------------------------------------
struct ConsoleState {
    UINT outputCp = 0;
    HANDLE handle[2] = {nullptr, nullptr};
    DWORD mode[2] = {0, 0};
    bool changed[2] = {false, false};
} g_console;

void restoreConsole() {
    for (int i = 0; i < 2; ++i)
        if (g_console.changed[i]) SetConsoleMode(g_console.handle[i], g_console.mode[i]);
    if (g_console.outputCp) SetConsoleOutputCP(g_console.outputCp);
}

// Attached to a real console: UTF-8 for the summary box and escape sequences for colour.
// Both are put back at exit. When stderr is a pipe or a file (how the app runs it)
// nothing is touched.
void setupConsole() {
    bool console = false, colour = true;
    const DWORD which[2] = {STD_OUTPUT_HANDLE, STD_ERROR_HANDLE};
    for (int i = 0; i < 2; ++i) {
        HANDLE h = GetStdHandle(which[i]);
        DWORD mode = 0;
        if (h == nullptr || h == INVALID_HANDLE_VALUE || !GetConsoleMode(h, &mode)) continue;
        console = true;
        if (mode & ENABLE_VIRTUAL_TERMINAL_PROCESSING) continue;
        if (SetConsoleMode(h, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING)) {
            g_console.handle[i] = h;
            g_console.mode[i] = mode;
            g_console.changed[i] = true;
        } else {
            colour = false;
        }
    }
    if (!console) return;
    const UINT cp = GetConsoleOutputCP();
    if (cp != CP_UTF8 && SetConsoleOutputCP(CP_UTF8)) g_console.outputCp = cp;
    std::atexit(restoreConsole);
    // A console that cannot interpret escape sequences would show them as text.
    if (!colour) _putenv_s("NO_COLOR", "1");
}

std::string hexOf(unsigned int v) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "0x%x", v);
    return buf;
}

}  // namespace

// ---- helpers of the self-test ------------------------------------------------------
int scepter_port_make_big_file(const char* path, long long offset, const char* marker) {
    std::wstring w;
    if (!path || !marker || !widen(path, fileCodePage(), w)) return 1;
    HANDLE h = CreateFileW(w.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return 1;
    int rc = 0;
    DWORD got = 0;
    LARGE_INTEGER where;
    where.QuadPart = offset;
    // Without the sparse mark Windows would fill the whole gap with zeros on disk.
    if (!DeviceIoControl(h, FSCTL_SET_SPARSE, nullptr, 0, nullptr, 0, &got, nullptr)) rc = 2;
    else if (!SetFilePointerEx(h, where, nullptr, FILE_BEGIN) ||
             !WriteFile(h, marker, static_cast<DWORD>(std::strlen(marker)), &got, nullptr) ||
             got != std::strlen(marker)) rc = 3;
    CloseHandle(h);
    if (rc != 0) DeleteFileW(w.c_str());
    return rc;
}

int scepter_port_remove_dir(const char* path) {
    std::wstring w;
    if (!path || !widen(trimSlashes(path), fileCodePage(), w)) return -1;
    return RemoveDirectoryW(w.c_str()) ? 0 : -1;
}

long long scepter_port_process_id() { return static_cast<long long>(GetCurrentProcessId()); }

void scepter_port_platform_probes(scepter_port_probe_fn emit, const char* parent, const char* leaf_utf8) {
    // The manifest's UTF-8 code page is what makes any path spellable in narrow calls.
    const UINT acp = GetACP();
    emit("active_code_page", std::to_string(acp).c_str(), acp == CP_UTF8);
    // The folder made through the narrow calls must carry the intended name, not the
    // same bytes read in another code page: look for it with the wide API.
    std::wstring wparent, wleaf;
    bool found = false;
    if (parent && leaf_utf8 && widen(parent, fileCodePage(), wparent) &&
        MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, leaf_utf8, -1, nullptr, 0) > 0 &&
        widen(leaf_utf8, CP_UTF8, wleaf)) {
        const DWORD attr = GetFileAttributesW((wparent + L"\\" + wleaf).c_str());
        found = attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY);
    }
    emit("unicode_dir_wide", found ? "1" : "0", found);

    const UINT em = GetErrorMode();
    emit("error_mode", hexOf(em).c_str(),
         (em & (SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)) ==
             (SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX));

    int fmode = 0;
    _get_fmode(&fmode);
    emit("default_file_mode", fmode == _O_BINARY ? "binary" : "text", fmode == _O_BINARY);
    std::FILE* const streams[3] = {stdin, stdout, stderr};
    const char* const keys[3] = {"stdin_mode", "stdout_mode", "stderr_mode"};
    for (int i = 0; i < 3; ++i) {
        const int fd = _fileno(streams[i]);
        if (fd < 0) { emit(keys[i], "absent", 1); continue; }
        std::fflush(streams[i]);
        const int previous = _setmode(fd, _O_BINARY);
        emit(keys[i], previous == _O_BINARY ? "binary" : previous == -1 ? "unknown" : "text",
             previous == _O_BINARY);
    }
    emit("wildcard_expansion", _dowildcard ? "on" : "off", _dowildcard == 0);
}

// ---- the real main ---------------------------------------------------------------------
int main(int argc, char** argv) {
    // A hidden child process must never wait on a dialog: not for a crash, not for a
    // missing drive, not for abort() (how running out of memory ends: exit code 3).
    SetErrorMode(GetErrorMode() | SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    _set_abort_behavior(0, _WRITE_ABORT_MSG | _CALL_REPORTFAULT);
    _set_error_mode(_OUT_TO_STDERR);
    // Binary everywhere. Text mode would turn "\n" into "\r\n" and stop reading at byte
    // 0x1A: fatal for gzip on stdin, wrong for FASTQ on --stdout, and it would make the
    // messages the app reads differ from Linux. Files opened without "b" follow _fmode.
    _set_fmode(_O_BINARY);
    std::FILE* const streams[3] = {stdin, stdout, stderr};
    for (std::FILE* f : streams) {
        const int fd = _fileno(f);            // negative when the process has no such handle
        if (fd >= 0) _setmode(fd, _O_BINARY);
    }
    setupConsole();
    if (selftestRequested()) {
        const int rc = scepter_port_selftest(argc, argv);
        std::fflush(stdout);
        return rc;
    }
    scepter_port_normalize_arguments(argc, argv);
    return scepter_main(argc, argv);
}

#else
// ============================================================================= Linux
#include <unistd.h>

int scepter_port_make_big_file(const char* path, long long offset, const char* marker) {
    if (!path || !marker) return 1;
    // A file system without sparse files would write the whole gap to the disk (more than
    // 4 GB): try with 32 MB first and look at what the file really occupies.
    {
        const long long trial = 32 * 1024 * 1024;
        std::FILE* t = std::fopen(path, "wb");
        if (!t) return 1;
        const bool wrote = scepter_fseek64(t, trial, SEEK_SET) == 0 && std::fwrite("x", 1, 1, t) == 1;
        const bool closed = std::fclose(t) == 0;
        struct stat st;
        const bool sparse = wrote && closed && ::stat(path, &st) == 0 &&
                            static_cast<long long>(st.st_blocks) * 512 < trial / 2;
        std::remove(path);
        if (!wrote || !closed) return 3;
        if (!sparse) return 2;
    }
    std::FILE* f = std::fopen(path, "wb");
    if (!f) return 1;
    const size_t n = std::strlen(marker);
    int rc = 0;
    if (scepter_fseek64(f, offset, SEEK_SET) != 0 || std::fwrite(marker, 1, n, f) != n) rc = 3;
    if (std::fclose(f) != 0) rc = 3;
    if (rc != 0) std::remove(path);
    return rc;
}

int scepter_port_remove_dir(const char* path) { return path ? ::rmdir(path) : -1; }

long long scepter_port_process_id() { return static_cast<long long>(::getpid()); }

void scepter_port_platform_probes(scepter_port_probe_fn, const char*, const char*) {}

namespace {
// Before main, so that the self-test runs before anything else and upstream's main
// needs no change. Does nothing unless SCEPTER_PORT_SELFTEST=1.
__attribute__((constructor)) void scepterPortEarly() {
    if (!selftestRequested()) return;
    std::vector<std::string> args;
    if (std::FILE* f = std::fopen("/proc/self/cmdline", "rb")) {
        std::string cur;
        for (int c; (c = std::fgetc(f)) != EOF;) {
            if (c == '\0') { args.push_back(cur); cur.clear(); } else cur += static_cast<char>(c);
        }
        if (!cur.empty()) args.push_back(cur);
        std::fclose(f);
    }
    if (args.empty()) args.push_back("scepter");
    std::vector<char*> argv;
    for (std::string& a : args) argv.push_back(&a[0]);
    argv.push_back(nullptr);
    const int rc = scepter_port_selftest(static_cast<int>(args.size()), argv.data());
    std::fflush(stdout);
    std::fflush(stderr);
    std::_Exit(rc);
}
}  // namespace

#endif
