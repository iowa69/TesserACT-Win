// Small helpers: string manipulation, timing, filesystem, terminal colour.
#pragma once

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cstdio>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

namespace fp {
namespace util {

inline bool endsWith(const std::string& s, const std::string& suffix) {
    return s.size() >= suffix.size() &&
           s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
}

inline bool startsWith(const std::string& s, const std::string& prefix) {
    return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

inline std::string toLower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

inline std::string toUpper(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
    return s;
}

inline std::string trim(const std::string& s) {
    size_t b = s.find_first_not_of(" \t\r\n");
    if (b == std::string::npos) return "";
    size_t e = s.find_last_not_of(" \t\r\n");
    return s.substr(b, e - b + 1);
}

inline std::vector<std::string> split(const std::string& s, char sep) {
    std::vector<std::string> out;
    size_t start = 0;
    while (true) {
        size_t pos = s.find(sep, start);
        if (pos == std::string::npos) { out.push_back(s.substr(start)); break; }
        out.push_back(s.substr(start, pos - start));
        start = pos + 1;
    }
    return out;
}

inline bool fileExists(const std::string& path) {
    struct stat st;
    return ::stat(path.c_str(), &st) == 0;
}

inline long long fileSize(const std::string& path) {
    struct stat st;
    if (::stat(path.c_str(), &st) != 0) return -1;
    return static_cast<long long>(st.st_size);
}

inline bool isGzipPath(const std::string& path) {
    return endsWith(toLower(path), ".gz");
}

inline std::string basename(const std::string& path) {
    size_t pos = path.find_last_of('/');
    return pos == std::string::npos ? path : path.substr(pos + 1);
}

inline std::string dirname(const std::string& path) {
    size_t pos = path.find_last_of('/');
    return pos == std::string::npos ? "." : path.substr(0, pos);
}

// Strip .gz then the fastq extension, e.g. "sample_R1.fastq.gz" -> "sample_R1".
inline std::string stripFastqExt(std::string name) {
    if (endsWith(toLower(name), ".gz")) name = name.substr(0, name.size() - 3);
    std::string low = toLower(name);
    for (const char* ext : {".fastq", ".fq", ".fasta", ".fa"}) {
        if (endsWith(low, ext)) return name.substr(0, name.size() - std::string(ext).size());
    }
    return name;
}

// Create a directory path recursively. Returns true on success or if it exists.
bool makeDirs(const std::string& path);

// Directory holding the running binary, used to locate files shipped beside it.
std::string executableDir(const std::string& argv0);

// Human-readable byte and count formatting.
std::string humanBytes(double bytes);
std::string humanCount(double n);
std::string humanTime(double seconds);

// Comma-grouped integer, e.g. 1234567 -> "1,234,567".
std::string commify(long long v);

class Timer {
public:
    Timer() : start_(std::chrono::steady_clock::now()) {}
    void reset() { start_ = std::chrono::steady_clock::now(); }
    double elapsed() const {
        return std::chrono::duration<double>(std::chrono::steady_clock::now() - start_).count();
    }
private:
    std::chrono::steady_clock::time_point start_;
};

// ANSI colour, automatically disabled when stderr is not a TTY or NO_COLOR is set.
class Color {
public:
    static bool enabled();
    static void setEnabled(bool on);
    static const char* red();
    static const char* green();
    static const char* yellow();
    static const char* blue();
    static const char* cyan();
    static const char* magenta();
    static const char* gray();
    static const char* bold();
    static const char* reset();
};

std::string nowString();          // "2026-07-31 21:30:00"
std::string isoTimestamp();       // ISO-8601

}  // namespace util
}  // namespace fp
