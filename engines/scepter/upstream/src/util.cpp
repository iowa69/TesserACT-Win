#include "util.h"

#include <cmath>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <sstream>

namespace fp {
namespace util {

bool makeDirs(const std::string& path) {
    if (path.empty() || path == ".") return true;
    if (fileExists(path)) return true;
    std::string acc;
    for (size_t i = 0; i < path.size(); ++i) {
        acc += path[i];
        if (path[i] == '/' || i + 1 == path.size()) {
            if (acc == "/" || acc.empty()) continue;
            if (!fileExists(acc) && ::mkdir(acc.c_str(), 0755) != 0) return false;
        }
    }
    return fileExists(path);
}

std::string executableDir(const std::string& argv0) {
    // /proc is authoritative and survives an argv[0] that carries no path, which
    // is the usual case when the tool is found on PATH.
    char buf[4096];
    ssize_t n = ::readlink("/proc/self/exe", buf, sizeof(buf) - 1);
    if (n > 0) {
        buf[n] = '\0';
        return dirname(std::string(buf));
    }
    if (argv0.find('/') != std::string::npos) return dirname(argv0);
    return ".";
}

std::string humanBytes(double bytes) {
    static const char* units[] = {"B", "KB", "MB", "GB", "TB"};
    int u = 0;
    while (bytes >= 1024.0 && u < 4) { bytes /= 1024.0; ++u; }
    char buf[64];
    std::snprintf(buf, sizeof(buf), u == 0 ? "%.0f %s" : "%.2f %s", bytes, units[u]);
    return buf;
}

std::string humanCount(double n) {
    static const char* units[] = {"", "K", "M", "G", "T"};
    int u = 0;
    while (n >= 1000.0 && u < 4) { n /= 1000.0; ++u; }
    char buf[64];
    std::snprintf(buf, sizeof(buf), u == 0 ? "%.0f%s" : "%.2f%s", n, units[u]);
    return buf;
}

std::string humanTime(double seconds) {
    char buf[64];
    if (seconds < 60) {
        std::snprintf(buf, sizeof(buf), "%.2fs", seconds);
    } else if (seconds < 3600) {
        std::snprintf(buf, sizeof(buf), "%dm%02ds", static_cast<int>(seconds) / 60,
                      static_cast<int>(seconds) % 60);
    } else {
        int s = static_cast<int>(seconds);
        std::snprintf(buf, sizeof(buf), "%dh%02dm%02ds", s / 3600, (s % 3600) / 60, s % 60);
    }
    return buf;
}

std::string commify(long long v) {
    bool neg = v < 0;
    unsigned long long x = neg ? static_cast<unsigned long long>(-(v + 1)) + 1
                               : static_cast<unsigned long long>(v);
    std::string digits = std::to_string(x);
    std::string out;
    int count = 0;
    for (int i = static_cast<int>(digits.size()) - 1; i >= 0; --i) {
        out += digits[i];
        if (++count % 3 == 0 && i > 0) out += ',';
    }
    if (neg) out += '-';
    std::reverse(out.begin(), out.end());
    return out;
}

namespace {
bool detectColor() {
    if (const char* nc = std::getenv("NO_COLOR"); nc && *nc) return false;
    if (const char* t = std::getenv("TERM"); t && std::strcmp(t, "dumb") == 0) return false;
    return ::isatty(2) != 0;
}
bool g_colorInit = false;
bool g_color = false;
}  // namespace

bool Color::enabled() {
    if (!g_colorInit) { g_color = detectColor(); g_colorInit = true; }
    return g_color;
}
void Color::setEnabled(bool on) { g_color = on; g_colorInit = true; }

const char* Color::red()     { return enabled() ? "\033[31m" : ""; }
const char* Color::green()   { return enabled() ? "\033[32m" : ""; }
const char* Color::yellow()  { return enabled() ? "\033[33m" : ""; }
const char* Color::blue()    { return enabled() ? "\033[34m" : ""; }
const char* Color::magenta() { return enabled() ? "\033[35m" : ""; }
const char* Color::cyan()    { return enabled() ? "\033[36m" : ""; }
const char* Color::gray()    { return enabled() ? "\033[90m" : ""; }
const char* Color::bold()    { return enabled() ? "\033[1m"  : ""; }
const char* Color::reset()   { return enabled() ? "\033[0m"  : ""; }

std::string nowString() {
    std::time_t t = std::time(nullptr);
    std::tm tmv{};
    localtime_r(&t, &tmv);
    char buf[64];
    std::strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", &tmv);
    return buf;
}

std::string isoTimestamp() {
    std::time_t t = std::time(nullptr);
    std::tm tmv{};
    gmtime_r(&t, &tmv);
    char buf[64];
    std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", &tmv);
    return buf;
}

}  // namespace util
}  // namespace fp
