// Self-contained HTML report writer.
//
// The document embeds its own CSS, JavaScript and SVG: no external stylesheet,
// script, font or image is referenced, so a report opens correctly from a
// file:// URL and survives being emailed as a single attachment. Charts are
// emitted as inline SVG built here in C++; the only JavaScript is a small
// tooltip / legend-toggle / theme helper.
#include "report.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <limits>
#include <map>
#include <string>
#include <utility>
#include <vector>

#include "common.h"
#include "options.h"
#include "stats.h"
#include "util.h"

namespace fp {
namespace {

// ---------------------------------------------------------------------------
// Small formatting helpers
// ---------------------------------------------------------------------------

#if defined(__GNUC__)
#define FP_PRINTF_FMT(a, b) __attribute__((format(printf, a, b)))
#else
#define FP_PRINTF_FMT(a, b)
#endif

std::string f(const char* fmt, ...) FP_PRINTF_FMT(1, 2);

std::string f(const char* fmt, ...) {
    char buf[512];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    if (n < 0) return std::string();
    if (static_cast<size_t>(n) < sizeof(buf)) return std::string(buf, static_cast<size_t>(n));
    std::string out(static_cast<size_t>(n) + 1, '\0');
    va_start(ap, fmt);
    vsnprintf(&out[0], out.size(), fmt, ap);
    va_end(ap);
    out.resize(static_cast<size_t>(n));
    return out;
}

const double kNaN = std::numeric_limits<double>::quiet_NaN();

bool finite(double v) { return std::isfinite(v); }

// Every string that reaches the document passes through here.
std::string htmlEscape(const std::string& s) {
    std::string o;
    o.reserve(s.size() + 8);
    for (char c : s) {
        unsigned char u = static_cast<unsigned char>(c);
        switch (c) {
            case '&': o += "&amp;"; break;
            case '<': o += "&lt;"; break;
            case '>': o += "&gt;"; break;
            case '"': o += "&quot;"; break;
            case '\'': o += "&#39;"; break;
            default:
                if (u < 0x20 && c != '\n' && c != '\t') o += ' ';
                else o += c;
        }
    }
    return o;
}

// Tooltip payloads are split on "||" by the client, so a literal "||" inside a
// value would create a spurious line; sequences and tile names cannot contain
// it, but user-supplied text can.
std::string tipEscape(const std::string& s) {
    std::string o = s;
    for (size_t i = 0; i + 1 < o.size();) {
        if (o[i] == '|' && o[i + 1] == '|') { o[i] = '/'; o[i + 1] = '/'; i += 2; }
        else ++i;
    }
    return htmlEscape(o);
}

std::string groupDigits(std::string digits) {
    std::string out;
    int c = 0;
    for (size_t i = digits.size(); i-- > 0;) {
        out += digits[i];
        if (++c % 3 == 0 && i > 0) out += ',';
    }
    std::reverse(out.begin(), out.end());
    return out;
}

// Thousands-separated fixed-point.
std::string fmtNum(double v, int dp = 0) {
    if (!finite(v)) return "n/a";
    bool neg = v < 0;
    std::string s = f("%.*f", dp, std::fabs(v));
    std::string ip = s, fp2;
    size_t dot = s.find('.');
    if (dot != std::string::npos) { ip = s.substr(0, dot); fp2 = s.substr(dot); }
    std::string out = groupDigits(ip) + fp2;
    return neg && out.find_first_not_of("0.,") != std::string::npos ? "-" + out : out;
}

std::string fmtCount(double v) { return fmtNum(v, 0); }

// Percentages are quoted to 2 dp throughout, from a 0..1 fraction.
std::string fmtPct(double frac, int dp = 2) {
    if (!finite(frac)) return "n/a";
    return fmtNum(frac * 100.0, dp) + "%";
}

double safeDiv(double a, double b) { return (b > 0 || b < 0) ? a / b : 0.0; }

std::string pctOf(double part, double whole, int dp = 2) {
    if (!(whole > 0)) return "n/a";
    return fmtPct(part / whole, dp);
}

std::string fmtBases(double bp) {
    if (!finite(bp)) return "n/a";
    double a = std::fabs(bp);
    if (a >= 1e9) return fmtNum(bp / 1e9, 2) + " Gbp";
    if (a >= 1e6) return fmtNum(bp / 1e6, 2) + " Mbp";
    if (a >= 1e3) return fmtNum(bp / 1e3, 2) + " kbp";
    return fmtNum(bp, 0) + " bp";
}

std::string fmtDuration(double seconds) {
    if (!finite(seconds) || seconds < 0) return "n/a";
    if (seconds < 1) return f("%.0f ms", seconds * 1000.0);
    if (seconds < 60) return f("%.2f s", seconds);
    long long t = static_cast<long long>(seconds + 0.5);
    long long h = t / 3600, m = (t % 3600) / 60, s = t % 60;
    if (h > 0) return f("%lldh %lldm %llds", h, m, s);
    return f("%lldm %llds", m, s);
}

std::string compactNum(double v) {
    double a = std::fabs(v);
    if (!finite(v)) return "n/a";
    if (a >= 1e9) return f("%.3gG", v / 1e9);
    if (a >= 1e6) return f("%.3gM", v / 1e6);
    if (a >= 1e3) return f("%.3gk", v / 1e3);
    if (a == 0) return "0";
    return f("%.4g", v);
}

// Coordinates: one decimal keeps the document small without visible artefacts.
std::string co(double v) {
    if (!finite(v)) return "0";
    return f("%.1f", v);
}

// ---------------------------------------------------------------------------
// Axis helpers
// ---------------------------------------------------------------------------

double niceStep(double range, int target) {
    if (!(range > 0) || target <= 0) return 1.0;
    double raw = range / target;
    double mag = std::pow(10.0, std::floor(std::log10(raw)));
    double n = raw / mag;
    double step;
    if (n <= 1.0) step = 1.0;
    else if (n <= 2.0) step = 2.0;
    else if (n <= 5.0) step = 5.0;
    else step = 10.0;
    return step * mag;
}

struct Axis {
    double lo = 0, hi = 1;
    bool log = false;

    double map(double v, double p0, double p1) const {
        double t;
        if (log) {
            double l0 = std::log10(std::max(lo, 1e-300));
            double l1 = std::log10(std::max(hi, 1e-300));
            double lv = std::log10(std::max(v, 1e-300));
            t = (l1 > l0) ? (lv - l0) / (l1 - l0) : 0.0;
        } else {
            t = (hi > lo) ? (v - lo) / (hi - lo) : 0.0;
        }
        if (t < -4) t = -4;
        if (t > 5) t = 5;
        return p0 + t * (p1 - p0);
    }

    std::vector<double> ticks(int target) const {
        std::vector<double> t;
        if (log) {
            if (!(hi > lo) || lo <= 0) return t;
            int e0 = static_cast<int>(std::floor(std::log10(lo)));
            int e1 = static_cast<int>(std::ceil(std::log10(hi)));
            std::vector<double> mult;
            if (e1 - e0 <= 3) mult = {1.0, 2.0, 5.0};
            else if (e1 - e0 <= 8) mult = {1.0};
            else mult = {1.0};
            int stride = (e1 - e0 > 8) ? 2 : 1;
            for (int e = e0; e <= e1; e += stride) {
                for (double m : mult) {
                    double v = m * std::pow(10.0, e);
                    if (v >= lo * 0.9999 && v <= hi * 1.0001) t.push_back(v);
                }
            }
            return t;
        }
        if (!(hi > lo)) { t.push_back(lo); return t; }
        double step = niceStep(hi - lo, target);
        double start = std::ceil(lo / step - 1e-9) * step;
        for (double v = start; v <= hi + step * 1e-6 && t.size() < 40; v += step) {
            t.push_back(std::fabs(v) < step * 1e-9 ? 0.0 : v);
        }
        return t;
    }
};

std::string tickText(double v, double step, bool logScale) {
    if (logScale) return compactNum(v);
    if (std::fabs(v) >= 1e4) return compactNum(v);
    int dp = 0;
    if (step > 0 && step < 1) dp = std::min(4, static_cast<int>(std::ceil(-std::log10(step)) + 0.001));
    std::string s = f("%.*f", dp, v);
    if (s == "-0") s = "0";
    return s;
}

// ---------------------------------------------------------------------------
// Chart primitives
// ---------------------------------------------------------------------------

struct Series {
    std::string name;
    std::string color = "var(--c1)";
    std::vector<double> x;      // empty -> 1..y.size()
    std::vector<double> y;      // NaN breaks the line
    std::vector<double> lo, hi; // optional shaded band
    bool band = false;
    bool line = true;
    bool dashed = false;
    bool markers = false;
    double fillOpacity = 0.16;
    bool inLegend = true;
};

struct ChartOpts {
    int width = 800;
    int height = 320;
    bool logX = false;
    bool logY = false;
    bool qualityZones = false;   // FastQC-style green/amber/red backdrop
    bool identity = false;       // y = x reference line
    double yMin = kNaN;
    double yMax = kNaN;
    double xMin = kNaN;
    double xMax = kNaN;
    int tipDp = 2;
    std::string unit;            // appended to y values in tooltips
    std::string xTipName = "x";
    int xTipDp = 0;
    std::string xUnit;
    std::vector<std::pair<double, std::string>> vlines;
    int minWidthPx = 560;
};

std::string naBlock(const std::string& msg) {
    return "<p class=\"na\">" + htmlEscape(msg) + "</p>\n";
}

double seriesX(const Series& s, size_t i) {
    return s.x.empty() ? static_cast<double>(i + 1) : s.x[i];
}

size_t seriesLen(const Series& s) {
    size_t n = std::max(s.y.size(), std::max(s.lo.size(), s.hi.size()));
    if (!s.x.empty()) n = std::min(n, s.x.size());
    return n;
}

std::string escAttr(const std::string& s) { return htmlEscape(s); }

// The workhorse. Line / band / marker series on shared linear or log axes.
std::string svgLineChart(const std::string& id, const std::string& title,
                         const std::string& xLabel, const std::string& yLabel,
                         const std::vector<Series>& series, const ChartOpts& o) {
    // --- bounds -------------------------------------------------------------
    double xmin = 1e308, xmax = -1e308, ymin = 1e308, ymax = -1e308;
    double yminPos = 1e308, xminPos = 1e308;
    size_t points = 0;
    for (const Series& s : series) {
        size_t n = seriesLen(s);
        for (size_t i = 0; i < n; ++i) {
            double x = seriesX(s, i);
            if (!finite(x)) continue;
            double vals[3] = {kNaN, kNaN, kNaN};
            if (i < s.y.size()) vals[0] = s.y[i];
            if (s.band && i < s.lo.size()) vals[1] = s.lo[i];
            if (s.band && i < s.hi.size()) vals[2] = s.hi[i];
            bool any = false;
            for (double v : vals) {
                if (!finite(v)) continue;
                any = true;
                ymin = std::min(ymin, v);
                ymax = std::max(ymax, v);
                if (v > 0) yminPos = std::min(yminPos, v);
            }
            if (!any) continue;
            ++points;
            xmin = std::min(xmin, x);
            xmax = std::max(xmax, x);
            if (x > 0) xminPos = std::min(xminPos, x);
        }
    }
    if (points == 0) return naBlock("No data available for this chart.");

    Axis ax, ay;
    ax.log = o.logX;
    ay.log = o.logY;

    if (ax.log) {
        double lo = (xminPos < 1e308) ? xminPos : 1.0;
        double hi = std::max(xmax, lo * 10.0);
        ax.lo = std::pow(10.0, std::floor(std::log10(lo)));
        ax.hi = std::pow(10.0, std::ceil(std::log10(hi)));
    } else {
        ax.lo = finite(o.xMin) ? o.xMin : xmin;
        ax.hi = finite(o.xMax) ? o.xMax : xmax;
        if (!(ax.hi > ax.lo)) { ax.hi = ax.lo + 1.0; }
    }

    if (ay.log) {
        double lo = (yminPos < 1e308) ? yminPos : 1.0;
        double hi = std::max(ymax, lo * 10.0);
        ay.lo = std::pow(10.0, std::floor(std::log10(lo)));
        ay.hi = std::pow(10.0, std::ceil(std::log10(hi)));
    } else {
        double lo = finite(o.yMin) ? o.yMin : ymin;
        double hi = finite(o.yMax) ? o.yMax : ymax;
        if (!finite(o.yMin) || !finite(o.yMax)) {
            double pad = (hi - lo) * 0.08;
            if (!(pad > 0)) pad = (std::fabs(hi) > 0 ? std::fabs(hi) * 0.1 : 1.0);
            if (!finite(o.yMin)) { lo -= pad; if (ymin >= 0 && lo < 0) lo = 0; }
            if (!finite(o.yMax)) hi += pad;
        }
        if (!(hi > lo)) hi = lo + 1.0;
        ay.lo = lo;
        ay.hi = hi;
    }

    // --- layout -------------------------------------------------------------
    int legendRows = 0;
    std::vector<const Series*> legend;
    for (const Series& s : series) if (s.inLegend && !s.name.empty()) legend.push_back(&s);
    const int perRow = 4;
    if (legend.size() > 1) legendRows = static_cast<int>((legend.size() + perRow - 1) / perRow);

    const double ml = 68, mr = 20;
    const double mt = 30 + legendRows * 18.0;
    const double mb = 50;
    const double W = o.width, H = o.height + legendRows * 18.0;
    const double x0 = ml, x1 = W - mr, y0 = mt, y1 = H - mb;

    std::string s;
    s.reserve(8192);
    s += "<svg class=\"chart\" id=\"" + escAttr(id) + "\" viewBox=\"0 0 " + co(W) + " " + co(H) +
         "\" role=\"img\" aria-label=\"" + escAttr(title) + "\">\n";
    s += "<text class=\"ct\" x=\"8\" y=\"18\">" + htmlEscape(title) + "</text>\n";

    // Plot background, plus FastQC-style quality zones when asked for.
    s += "<rect class=\"plotbg\" x=\"" + co(x0) + "\" y=\"" + co(y0) + "\" width=\"" + co(x1 - x0) +
         "\" height=\"" + co(y1 - y0) + "\"/>\n";
    if (o.qualityZones && !ay.log) {
        struct Zone { double lo, hi; const char* var; };
        const Zone zones[3] = {{ay.lo, 20.0, "var(--zone-r)"},
                               {20.0, 28.0, "var(--zone-a)"},
                               {28.0, ay.hi, "var(--zone-g)"}};
        for (const Zone& z : zones) {
            double zl = std::max(z.lo, ay.lo), zh = std::min(z.hi, ay.hi);
            if (!(zh > zl)) continue;
            double py1 = ay.map(zh, y1, y0), py2 = ay.map(zl, y1, y0);
            s += "<rect x=\"" + co(x0) + "\" y=\"" + co(py1) + "\" width=\"" + co(x1 - x0) +
                 "\" height=\"" + co(py2 - py1) + "\" fill=\"" + z.var + "\"/>\n";
        }
    }

    // --- grid and ticks -----------------------------------------------------
    std::vector<double> yt = ay.ticks(6);
    double ystep = yt.size() > 1 ? yt[1] - yt[0] : 1.0;
    for (double v : yt) {
        double py = ay.map(v, y1, y0);
        if (py < y0 - 0.5 || py > y1 + 0.5) continue;
        s += "<line class=\"grid\" x1=\"" + co(x0) + "\" y1=\"" + co(py) + "\" x2=\"" + co(x1) +
             "\" y2=\"" + co(py) + "\"/>\n";
        s += "<text class=\"tk\" x=\"" + co(x0 - 8) + "\" y=\"" + co(py + 3.5) +
             "\" text-anchor=\"end\">" + htmlEscape(tickText(v, ystep, ay.log)) + "</text>\n";
    }
    std::vector<double> xt = ax.ticks(8);
    double xstep = xt.size() > 1 ? xt[1] - xt[0] : 1.0;
    for (double v : xt) {
        double px = ax.map(v, x0, x1);
        if (px < x0 - 0.5 || px > x1 + 0.5) continue;
        s += "<line class=\"grid\" x1=\"" + co(px) + "\" y1=\"" + co(y0) + "\" x2=\"" + co(px) +
             "\" y2=\"" + co(y1) + "\"/>\n";
        s += "<text class=\"tk\" x=\"" + co(px) + "\" y=\"" + co(y1 + 16) +
             "\" text-anchor=\"middle\">" + htmlEscape(tickText(v, xstep, ax.log)) + "</text>\n";
    }
    s += "<line class=\"axis\" x1=\"" + co(x0) + "\" y1=\"" + co(y1) + "\" x2=\"" + co(x1) +
         "\" y2=\"" + co(y1) + "\"/>\n";
    s += "<line class=\"axis\" x1=\"" + co(x0) + "\" y1=\"" + co(y0) + "\" x2=\"" + co(x0) +
         "\" y2=\"" + co(y1) + "\"/>\n";

    s += "<text class=\"axl\" x=\"" + co((x0 + x1) / 2) + "\" y=\"" + co(H - 10) +
         "\" text-anchor=\"middle\">" + htmlEscape(xLabel) + "</text>\n";
    double ycen = (y0 + y1) / 2;
    s += "<text class=\"axl\" transform=\"rotate(-90 14 " + co(ycen) + ")\" x=\"14\" y=\"" +
         co(ycen) + "\" text-anchor=\"middle\">" + htmlEscape(yLabel) + "</text>\n";

    if (o.identity) {
        double lo = std::max(ax.lo, ay.lo), hi = std::min(ax.hi, ay.hi);
        if (hi > lo) {
            s += "<line class=\"ident\" x1=\"" + co(ax.map(lo, x0, x1)) + "\" y1=\"" +
                 co(ay.map(lo, y1, y0)) + "\" x2=\"" + co(ax.map(hi, x0, x1)) + "\" y2=\"" +
                 co(ay.map(hi, y1, y0)) + "\"/>\n";
        }
    }
    for (const auto& vl : o.vlines) {
        if (vl.first < ax.lo || vl.first > ax.hi) continue;
        double px = ax.map(vl.first, x0, x1);
        s += "<line class=\"vmark\" x1=\"" + co(px) + "\" y1=\"" + co(y0) + "\" x2=\"" + co(px) +
             "\" y2=\"" + co(y1) + "\"/>\n";
        s += "<text class=\"vmt\" x=\"" + co(px + 4) + "\" y=\"" + co(y0 + 12) + "\">" +
             htmlEscape(vl.second) + "</text>\n";
    }

    // --- series -------------------------------------------------------------
    for (size_t si = 0; si < series.size(); ++si) {
        const Series& sr = series[si];
        size_t n = seriesLen(sr);
        s += "<g class=\"sr\" data-s=\"" + f("%zu", si) + "\">\n";
        if (sr.band) {
            size_t i = 0;
            while (i < n) {
                while (i < n && !(i < sr.lo.size() && i < sr.hi.size() &&
                                  finite(sr.lo[i]) && finite(sr.hi[i]))) ++i;
                size_t start = i;
                while (i < n && i < sr.lo.size() && i < sr.hi.size() &&
                       finite(sr.lo[i]) && finite(sr.hi[i])) ++i;
                if (i - start < 1) continue;
                std::string up, down;
                for (size_t k = start; k < i; ++k) {
                    double px = ax.map(seriesX(sr, k), x0, x1);
                    up += (k == start ? "M" : "L") + co(px) + " " + co(ay.map(sr.hi[k], y1, y0)) + " ";
                }
                for (size_t k = i; k-- > start;) {
                    double px = ax.map(seriesX(sr, k), x0, x1);
                    down += "L" + co(px) + " " + co(ay.map(sr.lo[k], y1, y0)) + " ";
                    if (k == start) break;
                }
                s += "<path d=\"" + up + down + "Z\" fill=\"" + sr.color + "\" fill-opacity=\"" +
                     f("%.2f", sr.fillOpacity) + "\" stroke=\"none\"/>\n";
            }
        }
        if (sr.line && !sr.y.empty()) {
            std::string d;
            bool pen = false;
            for (size_t i = 0; i < n && i < sr.y.size(); ++i) {
                double v = sr.y[i], x = seriesX(sr, i);
                if (!finite(v) || !finite(x) || (ay.log && v <= 0)) { pen = false; continue; }
                d += (pen ? "L" : "M") + co(ax.map(x, x0, x1)) + " " + co(ay.map(v, y1, y0)) + " ";
                pen = true;
            }
            if (!d.empty()) {
                s += "<path class=\"ln\" d=\"" + d + "\" stroke=\"" + sr.color + "\" fill=\"none\"";
                if (sr.dashed) s += " stroke-dasharray=\"5 3\"";
                s += "/>\n";
            }
        }
        if (sr.markers) {
            for (size_t i = 0; i < n && i < sr.y.size(); ++i) {
                double v = sr.y[i], x = seriesX(sr, i);
                if (!finite(v) || !finite(x) || (ay.log && v <= 0)) continue;
                s += "<circle class=\"pt\" cx=\"" + co(ax.map(x, x0, x1)) + "\" cy=\"" +
                     co(ay.map(v, y1, y0)) + "\" r=\"3\" fill=\"" + sr.color + "\"/>\n";
            }
        }
        s += "</g>\n";
    }

    // --- hover bands --------------------------------------------------------
    std::vector<double> xs;
    for (const Series& sr : series) {
        size_t n = seriesLen(sr);
        for (size_t i = 0; i < n; ++i) {
            double x = seriesX(sr, i);
            if (finite(x)) xs.push_back(x);
        }
    }
    std::sort(xs.begin(), xs.end());
    xs.erase(std::unique(xs.begin(), xs.end()), xs.end());
    if (xs.size() > 400) {
        size_t stride = (xs.size() + 399) / 400;
        std::vector<double> thin;
        for (size_t i = 0; i < xs.size(); i += stride) thin.push_back(xs[i]);
        xs.swap(thin);
    }
    if (!xs.empty()) {
        std::vector<std::vector<std::pair<double, size_t>>> index(series.size());
        for (size_t si = 0; si < series.size(); ++si) {
            size_t n = seriesLen(series[si]);
            index[si].reserve(n);
            for (size_t i = 0; i < n; ++i) index[si].push_back({seriesX(series[si], i), i});
            std::sort(index[si].begin(), index[si].end());
        }
        s += "<g class=\"hbs\">\n";
        for (size_t i = 0; i < xs.size(); ++i) {
            double px = ax.map(xs[i], x0, x1);
            double left = (i == 0) ? x0 : (ax.map(xs[i - 1], x0, x1) + px) / 2;
            double right = (i + 1 == xs.size()) ? x1 : (ax.map(xs[i + 1], x0, x1) + px) / 2;
            if (right <= left) continue;
            std::string tip = tipEscape(o.xTipName) + " " + htmlEscape(f("%.*f", o.xTipDp, xs[i])) +
                              tipEscape(o.xUnit);
            bool anyVal = false;
            for (size_t si = 0; si < series.size(); ++si) {
                const Series& sr = series[si];
                auto it = std::lower_bound(index[si].begin(), index[si].end(),
                                           std::make_pair(xs[i] - 1e-9, size_t(0)));
                if (it == index[si].end() || std::fabs(it->first - xs[i]) > 1e-6) continue;
                size_t k = it->second;
                std::string line;
                if (sr.band && k < sr.lo.size() && k < sr.hi.size() &&
                    finite(sr.lo[k]) && finite(sr.hi[k])) {
                    line = tipEscape(sr.name) + ": " + htmlEscape(fmtNum(sr.lo[k], o.tipDp)) +
                           " &#8211; " + htmlEscape(fmtNum(sr.hi[k], o.tipDp)) + tipEscape(o.unit);
                } else if (k < sr.y.size() && finite(sr.y[k])) {
                    line = tipEscape(sr.name) + ": " + htmlEscape(fmtNum(sr.y[k], o.tipDp)) +
                           tipEscape(o.unit);
                }
                if (line.empty()) continue;
                tip += "||" + line;
                anyVal = true;
            }
            if (!anyVal) continue;
            s += "<rect class=\"hb\" x=\"" + co(left) + "\" y=\"" + co(y0) + "\" width=\"" +
                 co(right - left) + "\" height=\"" + co(y1 - y0) + "\" data-tip=\"" + tip + "\"/>\n";
        }
        s += "</g>\n";
    }

    // --- legend -------------------------------------------------------------
    for (size_t li = 0; li < legend.size(); ++li) {
        if (legend.size() < 2) break;
        size_t si = 0;
        for (size_t k = 0; k < series.size(); ++k) if (&series[k] == legend[li]) { si = k; break; }
        double lx = 10 + (li % perRow) * ((W - 20) / perRow);
        double ly = 34 + static_cast<double>(li / perRow) * 18.0;
        const Series& ls = *legend[li];
        // The swatch mirrors how the series is drawn, so a dashed "after" line is
        // distinguishable from the solid "before" line of the same colour.
        std::string swatch;
        if (ls.band || !ls.line) {
            swatch = "<rect x=\"" + co(lx) + "\" y=\"" + co(ly - 8) + "\" width=\"14\" height=\"11\" rx=\"2\" fill=\"" +
                     ls.color + "\" fill-opacity=\"" + f("%.2f", ls.band ? 0.45 : 1.0) + "\"/>";
        }
        if (ls.line) {
            swatch += "<line x1=\"" + co(lx) + "\" y1=\"" + co(ly - 2.5) + "\" x2=\"" + co(lx + 14) +
                      "\" y2=\"" + co(ly - 2.5) + "\" stroke=\"" + ls.color + "\" stroke-width=\"2.6\"" +
                      (ls.dashed ? " stroke-dasharray=\"4 2.5\"" : "") + "/>";
        }
        if (ls.markers) {
            swatch += "<circle cx=\"" + co(lx + 7) + "\" cy=\"" + co(ly - 2.5) + "\" r=\"2.8\" fill=\"" +
                      ls.color + "\"/>";
        }
        s += "<g class=\"lg\" data-chart=\"" + escAttr(id) + "\" data-s=\"" + f("%zu", si) + "\">" +
             swatch + "<text x=\"" + co(lx + 19) + "\" y=\"" + co(ly + 1) + "\">" +
             htmlEscape(ls.name) + "</text></g>\n";
    }

    s += "</svg>\n";
    return "<div class=\"scroll\"><div class=\"cw\" style=\"min-width:" + f("%d", o.minWidthPx) +
           "px\">" + s + "</div></div>\n";
}

struct BarOpts {
    int width = 800;
    int height = 320;
    std::string color = "var(--c1)";
    std::string unit;
    int tipDp = 2;
    bool rotateLabels = false;
    int minWidthPx = 560;
    double yMax = kNaN;
};

std::string svgBarChart(const std::string& title, const std::string& xLabel,
                        const std::string& yLabel, const std::vector<std::string>& labels,
                        const std::vector<double>& values, const BarOpts& o) {
    size_t n = std::min(labels.size(), values.size());
    if (n == 0) return naBlock("No data available for this chart.");
    double ymax = finite(o.yMax) ? o.yMax : 0.0;
    bool any = false;
    for (size_t i = 0; i < n; ++i) {
        if (!finite(values[i])) continue;
        any = true;
        ymax = std::max(ymax, values[i]);
    }
    if (!any) return naBlock("No data available for this chart.");
    if (!(ymax > 0)) ymax = 1;

    Axis ay;
    ay.lo = 0;
    ay.hi = ymax * 1.08;

    const double ml = 68, mr = 20, mt = 30, mb = o.rotateLabels ? 76 : 50;
    const double W = o.width, H = o.height;
    const double x0 = ml, x1 = W - mr, y0 = mt, y1 = H - mb;
    const double slot = (x1 - x0) / static_cast<double>(n);
    const double bw = std::max(1.0, slot * 0.72);

    std::string s;
    s += "<svg class=\"chart\" viewBox=\"0 0 " + co(W) + " " + co(H) + "\" role=\"img\" aria-label=\"" +
         escAttr(title) + "\">\n";
    s += "<text class=\"ct\" x=\"8\" y=\"18\">" + htmlEscape(title) + "</text>\n";
    s += "<rect class=\"plotbg\" x=\"" + co(x0) + "\" y=\"" + co(y0) + "\" width=\"" + co(x1 - x0) +
         "\" height=\"" + co(y1 - y0) + "\"/>\n";

    std::vector<double> yt = ay.ticks(6);
    double ystep = yt.size() > 1 ? yt[1] - yt[0] : 1.0;
    for (double v : yt) {
        double py = ay.map(v, y1, y0);
        s += "<line class=\"grid\" x1=\"" + co(x0) + "\" y1=\"" + co(py) + "\" x2=\"" + co(x1) +
             "\" y2=\"" + co(py) + "\"/>\n";
        s += "<text class=\"tk\" x=\"" + co(x0 - 8) + "\" y=\"" + co(py + 3.5) +
             "\" text-anchor=\"end\">" + htmlEscape(tickText(v, ystep, false)) + "</text>\n";
    }
    for (size_t i = 0; i < n; ++i) {
        double v = values[i];
        double bx = x0 + slot * static_cast<double>(i) + (slot - bw) / 2;
        if (finite(v) && v > 0) {
            double by = ay.map(v, y1, y0);
            s += "<rect class=\"bar\" x=\"" + co(bx) + "\" y=\"" + co(by) + "\" width=\"" + co(bw) +
                 "\" height=\"" + co(std::max(0.0, y1 - by)) + "\" fill=\"" + o.color +
                 "\" data-tip=\"" + tipEscape(labels[i]) + "||" +
                 htmlEscape(fmtNum(v, o.tipDp)) + tipEscape(o.unit) + "\"/>\n";
        }
        double lx = bx + bw / 2;
        if (o.rotateLabels) {
            s += "<text class=\"tk\" transform=\"rotate(-45 " + co(lx) + " " + co(y1 + 14) +
                 ")\" x=\"" + co(lx) + "\" y=\"" + co(y1 + 14) + "\" text-anchor=\"end\">" +
                 htmlEscape(labels[i]) + "</text>\n";
        } else if (n <= 40 || i % ((n / 30) + 1) == 0) {
            s += "<text class=\"tk\" x=\"" + co(lx) + "\" y=\"" + co(y1 + 16) +
                 "\" text-anchor=\"middle\">" + htmlEscape(labels[i]) + "</text>\n";
        }
    }
    s += "<line class=\"axis\" x1=\"" + co(x0) + "\" y1=\"" + co(y1) + "\" x2=\"" + co(x1) +
         "\" y2=\"" + co(y1) + "\"/>\n";
    s += "<line class=\"axis\" x1=\"" + co(x0) + "\" y1=\"" + co(y0) + "\" x2=\"" + co(x0) +
         "\" y2=\"" + co(y1) + "\"/>\n";
    s += "<text class=\"axl\" x=\"" + co((x0 + x1) / 2) + "\" y=\"" + co(H - 8) +
         "\" text-anchor=\"middle\">" + htmlEscape(xLabel) + "</text>\n";
    double ycen = (y0 + y1) / 2;
    s += "<text class=\"axl\" transform=\"rotate(-90 14 " + co(ycen) + ")\" x=\"14\" y=\"" +
         co(ycen) + "\" text-anchor=\"middle\">" + htmlEscape(yLabel) + "</text>\n";
    s += "</svg>\n";
    return "<div class=\"scroll\"><div class=\"cw\" style=\"min-width:" + f("%d", o.minWidthPx) +
           "px\">" + s + "</div></div>\n";
}

struct Segment {
    std::string label;
    double value = 0;
    std::string color;
};

std::string svgStackedBar(const std::vector<Segment>& segs, const std::string& title) {
    double total = 0;
    for (const Segment& sg : segs) total += std::max(0.0, sg.value);
    if (!(total > 0)) return naBlock("No reads were counted, so there is nothing to break down.");
    const double W = 800, H = 74;
    const double x0 = 8, x1 = W - 8, y0 = 26, y1 = 58;
    std::string s;
    s += "<svg class=\"chart\" viewBox=\"0 0 " + co(W) + " " + co(H) + "\" role=\"img\" aria-label=\"" +
         escAttr(title) + "\">\n";
    s += "<text class=\"ct\" x=\"8\" y=\"16\">" + htmlEscape(title) + "</text>\n";
    double cx = x0;
    for (const Segment& sg : segs) {
        if (!(sg.value > 0)) continue;
        double w = (x1 - x0) * (sg.value / total);
        s += "<rect x=\"" + co(cx) + "\" y=\"" + co(y0) + "\" width=\"" + co(std::max(w, 0.6)) +
             "\" height=\"" + co(y1 - y0) + "\" fill=\"" + sg.color + "\" class=\"seg\" data-tip=\"" +
             tipEscape(sg.label) + "||" + htmlEscape(fmtCount(sg.value)) + " reads||" +
             htmlEscape(pctOf(sg.value, total)) + " of total\"/>\n";
        cx += w;
    }
    s += "</svg>\n";
    return "<div class=\"scroll\"><div class=\"cw\" style=\"min-width:420px\">" + s + "</div></div>\n";
}

std::string sparkline(const std::vector<uint64_t>& v, int w = 90, int h = 20) {
    if (v.empty()) return "<span class=\"dim\">&mdash;</span>";
    double mx = 0;
    for (uint64_t u : v) mx = std::max(mx, static_cast<double>(u));
    if (!(mx > 0)) return "<span class=\"dim\">&mdash;</span>";
    std::string d;
    for (size_t i = 0; i < v.size(); ++i) {
        double x = (v.size() > 1) ? (static_cast<double>(i) / static_cast<double>(v.size() - 1)) * (w - 2) + 1 : 1;
        double y = h - 1 - (static_cast<double>(v[i]) / mx) * (h - 2);
        d += (i == 0 ? "M" : "L") + co(x) + " " + co(y) + " ";
    }
    return "<svg class=\"spark\" viewBox=\"0 0 " + f("%d", w) + " " + f("%d", h) +
           "\" width=\"" + f("%d", w) + "\" height=\"" + f("%d", h) +
           "\"><path d=\"" + d + "\" fill=\"none\" stroke=\"var(--c1)\" stroke-width=\"1.4\"/></svg>";
}

// Diverging blue/red built on alpha so the neutral midpoint is the card colour
// in both the light and the dark theme.
std::string divergingFill(double v, double vmax) {
    if (!finite(v) || !(vmax > 0)) return "transparent";
    double t = std::max(-1.0, std::min(1.0, v / vmax));
    double a = std::fabs(t) * 0.92;
    if (t < 0) return f("rgba(46,104,158,%.3f)", a);
    return f("rgba(176,69,63,%.3f)", a);
}

std::string sequentialFill(double t) {
    if (!finite(t)) return "transparent";
    t = std::max(0.0, std::min(1.0, t));
    return f("rgba(47,111,159,%.3f)", 0.06 + t * 0.82);
}

struct HeatOpts {
    double cellW = 6, cellH = 14;
    double vmax = 3.0;         // symmetric clamp for the diverging scale
    std::string valueName = "value";
    std::string valueUnit;
    std::string auxName;
    std::string auxUnit;
    std::string rowName = "row";
    std::string colName = "column";
    std::string xLabel, yLabel;
};

std::string svgHeatmap(const std::string& title, const std::vector<std::string>& rowLabels,
                       const std::vector<std::string>& colLabels, const std::vector<double>& values,
                       const std::vector<double>& aux, const HeatOpts& o) {
    size_t rows = rowLabels.size(), cols = colLabels.size();
    if (rows == 0 || cols == 0 || values.size() < rows * cols) {
        return naBlock("No data available for this heat map.");
    }
    const double ml = 92, mr = 24, mt = 32, mb = 54;
    const double W = ml + static_cast<double>(cols) * o.cellW + mr;
    const double H = mt + static_cast<double>(rows) * o.cellH + mb;
    std::string s;
    s.reserve(rows * cols * 96 + 2048);
    s += "<svg class=\"chart\" viewBox=\"0 0 " + co(W) + " " + co(H) + "\" role=\"img\" aria-label=\"" +
         escAttr(title) + "\">\n";
    s += "<text class=\"ct\" x=\"8\" y=\"18\">" + htmlEscape(title) + "</text>\n";

    size_t rowStride = rows <= 40 ? 1 : (rows / 34) + 1;
    const std::string cellGeom = "\" width=\"" + co(o.cellW + 0.4) + "\" height=\"" +
                                 co(o.cellH + 0.4) + "\" fill=\"";
    for (size_t r = 0; r < rows; ++r) {
        double ry = mt + static_cast<double>(r) * o.cellH;
        if (r % rowStride == 0) {
            s += "<text class=\"tk\" x=\"" + co(ml - 8) + "\" y=\"" + co(ry + o.cellH / 2 + 3.5) +
                 "\" text-anchor=\"end\">" + htmlEscape(rowLabels[r]) + "</text>\n";
        }
        // Rows are translated so each cell only carries its x offset.
        s += "<g transform=\"translate(0 " + co(ry) + ")\">";
        std::string rowTip = tipEscape(o.rowName) + " " + tipEscape(rowLabels[r]) + " &middot; " +
                             tipEscape(o.colName) + " ";
        for (size_t c = 0; c < cols; ++c) {
            double v = values[r * cols + c];
            double cx = ml + static_cast<double>(c) * o.cellW;
            std::string fill = finite(v) ? divergingFill(v, o.vmax) : "var(--grid)";
            std::string tip = rowTip + tipEscape(colLabels[c]);
            if (!aux.empty() && r * cols + c < aux.size() && finite(aux[r * cols + c])) {
                tip += "||" + tipEscape(o.auxName) + ": " +
                       htmlEscape(fmtNum(aux[r * cols + c], 2)) + tipEscape(o.auxUnit);
            }
            tip += "||" + tipEscape(o.valueName) + ": " +
                   (finite(v) ? htmlEscape(f("%+.2f", v)) : std::string("n/a")) + tipEscape(o.valueUnit);
            s += "<rect class=\"hm\" x=\"" + co(cx) + cellGeom + fill + "\" data-tip=\"" + tip + "\"/>";
        }
        s += "</g>\n";
    }
    size_t colStride = cols <= 16 ? 1 : (cols / 14) + 1;
    for (size_t c = 0; c < cols; c += colStride) {
        double cx = ml + (static_cast<double>(c) + 0.5) * o.cellW;
        s += "<text class=\"tk\" x=\"" + co(cx) + "\" y=\"" + co(mt + static_cast<double>(rows) * o.cellH + 15) +
             "\" text-anchor=\"middle\">" + htmlEscape(colLabels[c]) + "</text>\n";
    }
    s += "<text class=\"axl\" x=\"" + co(ml + (W - ml - mr) / 2) + "\" y=\"" + co(H - 8) +
         "\" text-anchor=\"middle\">" + htmlEscape(o.xLabel) + "</text>\n";
    double ycen = mt + static_cast<double>(rows) * o.cellH / 2;
    s += "<text class=\"axl\" transform=\"rotate(-90 14 " + co(ycen) + ")\" x=\"14\" y=\"" + co(ycen) +
         "\" text-anchor=\"middle\">" + htmlEscape(o.yLabel) + "</text>\n";
    s += "</svg>\n";
    int minW = static_cast<int>(std::min(1400.0, std::max(560.0, W)));
    return "<div class=\"scroll\"><div class=\"cw\" style=\"min-width:" + f("%d", minW) + "px\">" + s +
           "</div></div>\n";
}

std::string heatLegend(double vmax, const std::string& unit) {
    std::string s = "<div class=\"hlgd\"><span class=\"dim\">" +
                    htmlEscape(f("%+.1f", -vmax) + unit) + "</span>";
    const int steps = 21;
    for (int i = 0; i < steps; ++i) {
        double t = -1.0 + 2.0 * i / (steps - 1);
        s += "<i style=\"background:" + divergingFill(t * vmax, vmax) + "\"></i>";
    }
    s += "<span class=\"dim\">" + htmlEscape(f("%+.1f", vmax) + unit) + "</span></div>\n";
    return s;
}

std::string svgScatter(const std::string& id, const std::string& title, const std::string& xLabel,
                       const std::string& yLabel, const std::vector<double>& xs,
                       const std::vector<double>& ys, const ChartOpts& base) {
    Series pts;
    pts.name = "observed";
    pts.color = "var(--c1)";
    pts.x = xs;
    pts.y = ys;
    pts.markers = true;
    pts.line = true;
    pts.inLegend = false;
    ChartOpts o = base;
    o.identity = true;
    return svgLineChart(id, title, xLabel, yLabel, {pts}, o);
}

// ---------------------------------------------------------------------------
// HTML building blocks
// ---------------------------------------------------------------------------

std::string card(const std::string& title, const std::string& body, const std::string& note = "") {
    std::string s = "<div class=\"card\">";
    if (!title.empty()) s += "<h3>" + htmlEscape(title) + "</h3>";
    if (!note.empty()) s += "<p class=\"note\">" + note + "</p>";
    s += body;
    s += "</div>\n";
    return s;
}

std::string metric(const std::string& label, const std::string& value, const std::string& sub = "") {
    std::string s = "<div class=\"metric\"><div class=\"ml\">" + htmlEscape(label) +
                    "</div><div class=\"mv\">" + htmlEscape(value) + "</div>";
    if (!sub.empty()) s += "<div class=\"ms\">" + htmlEscape(sub) + "</div>";
    s += "</div>";
    return s;
}

// Cells are emitted verbatim: callers escape their own text (some cells hold
// inline SVG sparklines or bars).
std::string table(const std::vector<std::string>& headers,
                  const std::vector<std::vector<std::string>>& rows, size_t rightFrom = 1) {
    if (rows.empty()) return naBlock("No rows to show.");
    std::string s = "<div class=\"scroll\"><table>\n<thead><tr>";
    for (size_t i = 0; i < headers.size(); ++i) {
        s += std::string("<th") + (i >= rightFrom ? " class=\"r\"" : "") + ">" +
             htmlEscape(headers[i]) + "</th>";
    }
    s += "</tr></thead>\n<tbody>\n";
    for (const auto& r : rows) {
        s += "<tr>";
        for (size_t i = 0; i < r.size(); ++i) {
            s += std::string("<td") + (i >= rightFrom ? " class=\"r\"" : "") + ">" + r[i] + "</td>";
        }
        s += "</tr>\n";
    }
    s += "</tbody></table></div>\n";
    return s;
}

std::string inlineBar(double frac, const std::string& color = "var(--c1)") {
    double p = std::max(0.0, std::min(1.0, frac)) * 100.0;
    return "<span class=\"ibar\"><i style=\"width:" + f("%.2f", p) + "%;background:" + color +
           "\"></i></span>";
}

std::string calloutBox(const std::string& kind, const std::string& text) {
    return "<div class=\"callout " + kind + "\">" + text + "</div>\n";
}

// ---------------------------------------------------------------------------
// Stats access helpers (every one of these guards against empty vectors)
// ---------------------------------------------------------------------------

size_t qualCycles(const ReadSetStats& s) {
    size_t n = s.cycles;
    n = std::min(n, s.cycleDepth.size());
    n = std::min(n, s.cycleQualSum.size());
    return std::min<size_t>(n, kMaxCycles);
}

size_t baseCycles(const ReadSetStats& s) {
    size_t n = std::min<size_t>(s.cycles, kMaxCycles);
    n = std::min(n, s.cycleDepth.size());
    for (int b = 0; b < 5; ++b) n = std::min(n, s.baseCount[static_cast<size_t>(b)].size());
    return n;
}

bool hasQuantiles(const ReadSetStats& s) {
    return s.positionalHist && qualCycles(s) > 0 && s.cycleQualHist.size() >= qualCycles(s);
}

std::vector<double> cycleQualitySeries(const ReadSetStats& s) {
    std::vector<double> v;
    size_t n = qualCycles(s);
    v.reserve(n);
    for (size_t c = 0; c < n; ++c) {
        v.push_back(s.cycleDepth[c] > 0 ? s.cycleMeanQuality(c) : kNaN);
    }
    return v;
}

std::vector<double> cycleAxis(size_t n) {
    std::vector<double> x;
    x.reserve(n);
    for (size_t i = 0; i < n; ++i) x.push_back(static_cast<double>(i + 1));
    return x;
}

struct Combined {
    uint64_t reads = 0, bases = 0, q20 = 0, q30 = 0, gc = 0, nb = 0;
    double meanLen() const { return safeDiv(static_cast<double>(bases), static_cast<double>(reads)); }
    double q20f() const { return safeDiv(static_cast<double>(q20), static_cast<double>(bases)); }
    double q30f() const { return safeDiv(static_cast<double>(q30), static_cast<double>(bases)); }
    double gcf() const { return safeDiv(static_cast<double>(gc), static_cast<double>(bases)); }
    double nf() const { return safeDiv(static_cast<double>(nb), static_cast<double>(bases)); }
};

Combined combine(const ReadSetStats& a, const ReadSetStats& b) {
    Combined c;
    c.reads = a.reads + b.reads;
    c.bases = a.bases + b.bases;
    c.q20 = a.q20Bases + b.q20Bases;
    c.q30 = a.q30Bases + b.q30Bases;
    c.gc = a.gcBases + b.gcBases;
    c.nb = a.nBases + b.nBases;
    return c;
}

const char* kBaseName[5] = {"A", "C", "G", "T", "N"};
const char* kBaseColor[5] = {"var(--c3)", "var(--c1)", "var(--c7)", "var(--c5)", "var(--c6)"};

// ---------------------------------------------------------------------------
// Verdict
// ---------------------------------------------------------------------------

struct Issue {
    int sev = 0;      // 2 = fail, 1 = warn
    double weight = 0;
    std::string text;
};

struct Verdict {
    int level = 0;    // 0 pass, 1 warn, 2 fail
    std::vector<std::string> reasons;
};

Verdict computeVerdict(const RunStats& st, const Options& opt) {
    std::vector<Issue> issues;
    auto push = [&issues](int sev, double weight, const std::string& text) {
        issues.push_back(Issue{sev, weight, text});
    };

    Combined before = combine(st.before1, st.before2);
    Combined after = combine(st.after1, st.after2);
    const Combined& qc = after.bases > 0 ? after : before;
    const char* phase = after.bases > 0 ? "after filtering" : "in the input";

    if (qc.bases > 0) {
        double q30 = qc.q30f() * 100.0;
        if (q30 < 70.0) push(2, 100 - q30, f("Q30 is only %.2f%% %s (fail below 70%%)", q30, phase));
        else if (q30 < 80.0) push(1, 100 - q30, f("Q30 is %.2f%% %s (warn below 80%%)", q30, phase));
        double nf = qc.nf() * 100.0;
        if (nf > 5.0) push(2, nf, f("N content is %.2f%% (fail above 5%%)", nf));
        else if (nf > 1.0) push(1, nf, f("N content is %.2f%% (warn above 1%%)", nf));
    } else {
        push(1, 50, "No bases were counted, so quality could not be assessed");
    }

    if (st.duplication.enabled) {
        double dup = st.duplication.duplicationRate * 100.0;
        if (dup > 1.5 && dup <= 100.0) {
            if (dup > 40.0) push(2, dup, f("Duplication rate is %.2f%% (fail above 40%%)", dup));
            else if (dup > 20.0) push(1, dup, f("Duplication rate is %.2f%% (warn above 20%%)", dup));
        }
    }

    double adapterReads = static_cast<double>(st.adapterTrimmedReads1 + st.adapterTrimmedReads2);
    double inReads = static_cast<double>(st.readsIn > 0 ? st.readsIn : before.reads);
    if (inReads > 0) {
        double ad = safeDiv(adapterReads, inReads) * 100.0;
        if (ad > 20.0) push(2, ad, f("Adapter sequence found in %.2f%% of reads (fail above 20%%)", ad));
        else if (ad > 5.0) push(1, ad, f("Adapter sequence found in %.2f%% of reads (warn above 5%%)", ad));
    }

    if (st.readsIn > 0 && opt.mode == RunMode::Process) {
        double keep = safeDiv(static_cast<double>(st.readsOut), static_cast<double>(st.readsIn)) * 100.0;
        if (keep < 50.0) push(2, 100 - keep, f("Only %.2f%% of reads passed filtering (fail below 50%%)", keep));
        else if (keep < 80.0) push(1, 100 - keep, f("%.2f%% of reads passed filtering (warn below 80%%)", keep));
    }

    if (st.errorModel.hasData()) {
        double er = st.errorModel.overallErrorRate * 100.0;
        if (er > 1.0) push(2, er * 10, f("Empirical error rate is %.3f%% (fail above 1%%)", er));
        else if (er > 0.5) push(1, er * 10, f("Empirical error rate is %.3f%% (warn above 0.5%%)", er));
        if (st.errorModel.calibrationBias < -3.0) {
            push(1, -st.errorModel.calibrationBias,
                 f("Quality scores are optimistic by %.1f Q on average", -st.errorModel.calibrationBias));
        }
    }

    if (st.tiles1.usable() && st.tiles1.tileCount() > 1 && !st.tiles1.outlierTiles.empty()) {
        double frac = safeDiv(static_cast<double>(st.tiles1.outlierTiles.size()),
                              static_cast<double>(st.tiles1.tileCount()));
        std::string msg = f("%zu of %zu tiles deviate from the flow-cell mean quality",
                            st.tiles1.outlierTiles.size(), st.tiles1.tileCount());
        if (frac > 0.25) push(2, frac * 100, msg + " (fail above 25% of tiles)");
        else push(1, frac * 100, msg);
    }

    std::stable_sort(issues.begin(), issues.end(), [](const Issue& a, const Issue& b) {
        if (a.sev != b.sev) return a.sev > b.sev;
        return a.weight > b.weight;
    });

    Verdict v;
    for (const Issue& i : issues) v.level = std::max(v.level, i.sev);
    for (size_t i = 0; i < issues.size() && i < 3; ++i) v.reasons.push_back(issues[i].text);
    if (v.reasons.empty()) {
        v.reasons.push_back("Quality, duplication, adapter content and yield are all within thresholds");
    }
    return v;
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------

std::string sectionSummary(const RunStats& st, const Options& opt) {
    Combined before = combine(st.before1, st.before2);
    Combined after = combine(st.after1, st.after2);
    bool hasAfter = after.reads > 0 || st.readsOut > 0;

    double readsIn = static_cast<double>(st.readsIn > 0 ? st.readsIn : before.reads);
    double readsOut = static_cast<double>(st.readsOut > 0 ? st.readsOut : after.reads);
    double basesIn = static_cast<double>(st.basesIn > 0 ? st.basesIn : before.bases);
    double basesOut = static_cast<double>(st.basesOut > 0 ? st.basesOut : after.bases);

    std::string g = "<div class=\"grid\">";
    g += metric("Reads out", hasAfter ? fmtCount(readsOut) : "\xE2\x80\x94",
                readsIn > 0 ? fmtCount(readsIn) + " in \xC2\xB7 " + pctOf(readsOut, readsIn) + " passing"
                            : "no reads counted");
    g += metric("Bases out", hasAfter ? fmtBases(basesOut) : "\xE2\x80\x94",
                basesIn > 0 ? fmtBases(basesIn) + " in \xC2\xB7 " + pctOf(basesOut, basesIn) + " retained"
                            : "no bases counted");
    g += metric("Q20", hasAfter ? fmtPct(after.q20f()) : fmtPct(before.q20f()),
                "before " + fmtPct(before.q20f()) + (hasAfter ? " \xE2\x86\x92 after " + fmtPct(after.q20f()) : ""));
    g += metric("Q30", hasAfter ? fmtPct(after.q30f()) : fmtPct(before.q30f()),
                "before " + fmtPct(before.q30f()) + (hasAfter ? " \xE2\x86\x92 after " + fmtPct(after.q30f()) : ""));
    g += metric("GC content", hasAfter ? fmtPct(after.gcf()) : fmtPct(before.gcf()),
                "before " + fmtPct(before.gcf()) + (hasAfter ? " \xE2\x86\x92 after " + fmtPct(after.gcf()) : ""));
    g += metric("Mean read length",
                hasAfter ? fmtNum(after.meanLen(), 1) + " bp" : fmtNum(before.meanLen(), 1) + " bp",
                "before " + fmtNum(before.meanLen(), 1) + " bp" +
                    (hasAfter ? " \xE2\x86\x92 after " + fmtNum(after.meanLen(), 1) + " bp" : ""));
    g += metric("N content", fmtPct(before.nf()),
                fmtCount(static_cast<double>(before.nb)) + " ambiguous bases in the input");

    if (st.duplication.enabled) {
        g += metric("Duplication rate", fmtPct(st.duplication.duplicationRate),
                    st.duplication.estimatedLibrarySize > 0
                        ? "est. library " + fmtCount(st.duplication.estimatedLibrarySize) + " molecules"
                        : "estimated from the read-pair key table");
    } else {
        g += metric("Duplication rate", "\xE2\x80\x94", "duplication evaluation disabled");
    }

    double adReads = static_cast<double>(st.adapterTrimmedReads1 + st.adapterTrimmedReads2);
    double adBases = static_cast<double>(st.adapterTrimmedBases1 + st.adapterTrimmedBases2);
    g += metric("Adapter-trimmed reads", readsIn > 0 ? pctOf(adReads, readsIn) : "n/a",
                fmtCount(adReads) + " reads \xC2\xB7 " + fmtBases(adBases) + " removed");

    if (st.kmers.reliable && st.kmers.estimatedGenomeSize > 0) {
        std::string sub = "k = " + fmtNum(st.kmers.k(), 0) + " \xC2\xB7 peak depth " +
                          fmtNum(st.kmers.peakDepth, 0) + "x";
        if (opt.deep.genomeSize > 0) {
            double ratio = safeDiv(st.kmers.estimatedGenomeSize, static_cast<double>(opt.deep.genomeSize));
            sub = "expected " + fmtBases(static_cast<double>(opt.deep.genomeSize)) + " \xC2\xB7 " +
                  fmtNum(ratio, 2) + "x of expected";
        }
        g += metric("Estimated genome size", fmtBases(st.kmers.estimatedGenomeSize), sub);
        g += metric("Estimated depth", fmtNum(st.kmers.estimatedDepth, 1) + "x",
                    "error k-mers " + fmtPct(st.kmers.errorKmerFraction) + " \xC2\xB7 repeats " +
                        fmtPct(st.kmers.repeatFraction));
    } else if (opt.deep.kmerSpectrum) {
        g += metric("Estimated genome size", "\xE2\x80\x94", "no reliable coverage peak found");
    }

    if (st.errorModel.hasData()) {
        g += metric("Empirical error rate", fmtPct(st.errorModel.overallErrorRate, 4),
                    fmtCount(static_cast<double>(st.errorModel.mismatches)) + " mismatches in " +
                        fmtCount(static_cast<double>(st.errorModel.basesCompared)) + " compared bases");
        std::string bias;
        if (st.errorModel.calibrationBias < -0.05) {
            bias = f("optimistic by %.2f Q", -st.errorModel.calibrationBias);
        } else if (st.errorModel.calibrationBias > 0.05) {
            bias = f("conservative by %.2f Q", st.errorModel.calibrationBias);
        } else {
            bias = "well calibrated";
        }
        g += metric("Empirical quality", fmtNum(st.errorModel.overallQ, 2) + " Q",
                    "reported scores are " + bias);
    }

    if (st.insertSize.counted > 0) {
        double unknownFrac = safeDiv(static_cast<double>(st.insertSize.unknown),
                                     static_cast<double>(st.insertSize.counted + st.insertSize.unknown));
        g += metric("Insert size (mode)", fmtNum(st.insertSize.mode, 0) + " bp",
                    "median " + fmtNum(st.insertSize.median, 0) + " bp \xC2\xB7 " +
                        fmtPct(unknownFrac) + " pairs unresolved");
    }

    if (st.mergedPairs > 0) {
        g += metric("Merged pairs", fmtCount(static_cast<double>(st.mergedPairs)),
                    readsIn > 0 ? pctOf(static_cast<double>(st.mergedPairs) * 2, readsIn) + " of reads" : "");
    }
    if (st.correctedBases > 0) {
        g += metric("Corrected bases", fmtCount(static_cast<double>(st.correctedBases)),
                    fmtCount(static_cast<double>(st.correctedReads)) + " reads touched by overlap correction");
    }
    if (st.polyGTrimmedReads > 0 || st.polyXTrimmedReads > 0) {
        g += metric("polyG / polyX trimmed",
                    fmtCount(static_cast<double>(st.polyGTrimmedReads)) + " / " +
                        fmtCount(static_cast<double>(st.polyXTrimmedReads)),
                    fmtBases(static_cast<double>(st.polyGTrimmedBases + st.polyXTrimmedBases)) + " removed");
    }
    g += "</div>\n";

    if (!hasAfter) {
        g += calloutBox("info",
                        "No post-filtering statistics are present: this looks like a QC-only run, so "
                        "&ldquo;after&rdquo; values are shown as &mdash;.");
    }
    return g;
}

std::string sectionFiltering(const RunStats& st) {
    const size_t n = static_cast<size_t>(FilterResult::kCount);
    double total = 0;
    for (size_t i = 0; i < n; ++i) total += static_cast<double>(st.filterCounts[i]);
    if (!(total > 0)) {
        return naBlock("No filtering counts were recorded, so no breakdown is available. This is "
                       "expected when the run only produced statistics.");
    }
    const char* colors[8] = {"var(--c3)", "var(--c1)", "var(--c2)", "var(--c5)",
                             "var(--c4)", "var(--c7)", "var(--c8)", "var(--c6)"};
    std::vector<std::vector<std::string>> rows;
    std::vector<Segment> segs;
    for (size_t i = 0; i < n; ++i) {
        double v = static_cast<double>(st.filterCounts[i]);
        const char* nm = filterResultName(static_cast<FilterResult>(i));
        std::string name = nm ? nm : "unknown";
        const char* col = colors[i % 8];
        if (v > 0) segs.push_back(Segment{name, v, col});
        rows.push_back({htmlEscape(name), htmlEscape(fmtCount(v)), htmlEscape(pctOf(v, total)),
                        inlineBar(safeDiv(v, total), col)});
    }
    std::string body = svgStackedBar(segs, "Reads by filtering outcome");
    body += table({"Outcome", "Reads", "% of input", ""}, rows, 1);
    return body;
}

std::string sectionQuality(const RunStats& st) {
    bool hasR2 = st.before2.reads > 0;
    std::vector<Series> series;
    struct Src { const ReadSetStats* s; const char* name; const char* color; bool dashed; bool use; };
    const Src srcs[4] = {
        {&st.before1, "R1 before", "var(--c1)", false, qualCycles(st.before1) > 0},
        {&st.after1, "R1 after", "var(--c1)", true, qualCycles(st.after1) > 0},
        {&st.before2, "R2 before", "var(--c2)", false, hasR2 && qualCycles(st.before2) > 0},
        {&st.after2, "R2 after", "var(--c2)", true, hasR2 && qualCycles(st.after2) > 0},
    };
    for (const Src& sc : srcs) {
        if (!sc.use) continue;
        Series se;
        se.name = sc.name;
        se.color = sc.color;
        se.dashed = sc.dashed;
        se.y = cycleQualitySeries(*sc.s);
        se.x = cycleAxis(se.y.size());
        series.push_back(se);
    }
    if (series.empty()) {
        return naBlock("No per-cycle quality data was collected.");
    }
    ChartOpts o;
    o.yMin = 0;
    o.yMax = 42;
    o.qualityZones = true;
    o.unit = " Q";
    o.xTipName = "Cycle";
    o.tipDp = 2;
    std::string body = card("", svgLineChart("ch-qual", "Mean quality by cycle",
                                             "cycle (1-based position in read)",
                                             "mean Phred quality (Q)", series, o),
                            "Background bands mark the FastQC quality zones: Q&ge;28 good, "
                            "Q20&ndash;28 acceptable, Q&lt;20 poor. Click a legend entry to hide a series.");

    struct Q { const ReadSetStats* s; const char* label; };
    const Q qs[2] = {{&st.before1, "read 1"}, {&st.before2, "read 2"}};

    for (size_t i = 0; i < 2; ++i) {
        if (i == 1 && !hasR2) continue;
        const ReadSetStats& s = *qs[i].s;
        size_t n = std::min(qualCycles(s), baseCycles(s));
        if (n == 0) continue;
        std::vector<Series> byBase;
        for (int b = 0; b < 4; ++b) {
            Series se;
            se.name = kBaseName[b];
            se.color = kBaseColor[b];
            se.y.reserve(n);
            for (size_t c = 0; c < n; ++c) {
                se.y.push_back(s.baseCount[static_cast<size_t>(b)][c] > 0
                                   ? s.cycleMeanQualityForBase(c, b)
                                   : kNaN);
            }
            se.x = cycleAxis(n);
            byBase.push_back(se);
        }
        ChartOpts bq;
        bq.yMin = 0;
        bq.yMax = 42;
        bq.qualityZones = true;
        bq.unit = " Q";
        bq.xTipName = "Cycle";
        bq.tipDp = 2;
        bq.height = 280;
        body += card("", svgLineChart(f("ch-qbase%zu", i),
                                      std::string("Mean quality by cycle and base \xE2\x80\x94 ") +
                                          qs[i].label + " (before filtering)",
                                      "cycle (1-based position in read)",
                                      "mean Phred quality (Q)", byBase, bq),
                     "Each curve averages only the reads that called that base at that cycle. "
                     "Large gaps between the curves point at a base-specific calling bias.");
    }

    for (size_t i = 0; i < 2; ++i) {
        if (i == 1 && !hasR2) continue;
        const ReadSetStats& s = *qs[i].s;
        if (!hasQuantiles(s)) {
            body += card(std::string("Quality distribution by cycle \xE2\x80\x94 ") + qs[i].label,
                         naBlock("Per-cycle quality quantiles are not available: the full positional "
                                 "quality histogram was disabled for this run."));
            continue;
        }
        size_t n = qualCycles(s);
        std::vector<double> x = cycleAxis(n), p10(n), p25(n), p50(n), p75(n), p90(n);
        // cycleQualQuantile() reports "no data" as -1; that must become a gap in
        // the band rather than a quality of -1.
        auto quant = [&s](size_t c, double q) {
            int v = s.cycleQualQuantile(c, q);
            return v < 0 ? kNaN : static_cast<double>(v);
        };
        for (size_t c = 0; c < n; ++c) {
            if (s.cycleDepth[c] == 0) {
                p10[c] = p25[c] = p50[c] = p75[c] = p90[c] = kNaN;
                continue;
            }
            p10[c] = quant(c, 0.10);
            p25[c] = quant(c, 0.25);
            p50[c] = quant(c, 0.50);
            p75[c] = quant(c, 0.75);
            p90[c] = quant(c, 0.90);
        }
        Series outer;
        outer.name = "10\xE2\x80\x93" "90%";
        outer.color = "var(--c1)";
        outer.band = true;
        outer.line = false;
        outer.fillOpacity = 0.14;
        outer.x = x; outer.lo = p10; outer.hi = p90;
        Series inner = outer;
        inner.name = "25\xE2\x80\x93" "75% (IQR)";
        inner.fillOpacity = 0.34;
        inner.lo = p25; inner.hi = p75;
        Series med;
        med.name = "median";
        med.color = "var(--c5)";
        med.x = x; med.y = p50;

        ChartOpts bo;
        bo.yMin = 0;
        bo.yMax = 42;
        bo.qualityZones = true;
        bo.unit = " Q";
        bo.xTipName = "Cycle";
        bo.height = 300;
        body += card("", svgLineChart(f("ch-band%zu", i),
                                      std::string("Quality distribution by cycle \xE2\x80\x94 ") + qs[i].label +
                                          " (before filtering)",
                                      "cycle (1-based position in read)", "Phred quality (Q)",
                                      {outer, inner, med}, bo),
                     "Shaded areas are the 10&ndash;90% and inter-quartile ranges of the per-cycle "
                     "quality distribution; the line is the median.");
    }
    return body;
}

std::string sectionBaseContent(const RunStats& st) {
    struct Panel { const ReadSetStats* s; const char* title; };
    std::vector<Panel> panels;
    panels.push_back({&st.before1, "Read 1 \xE2\x80\x94 before filtering"});
    if (baseCycles(st.after1) > 0) panels.push_back({&st.after1, "Read 1 \xE2\x80\x94 after filtering"});
    if (st.before2.reads > 0) panels.push_back({&st.before2, "Read 2 \xE2\x80\x94 before filtering"});
    if (st.before2.reads > 0 && baseCycles(st.after2) > 0)
        panels.push_back({&st.after2, "Read 2 \xE2\x80\x94 after filtering"});

    std::string body;
    size_t drawn = 0;
    for (size_t p = 0; p < panels.size(); ++p) {
        const ReadSetStats& s = *panels[p].s;
        size_t n = baseCycles(s);
        if (n == 0) continue;
        std::vector<Series> series;
        for (int b = 0; b < 5; ++b) {
            Series se;
            se.name = kBaseName[b];
            se.color = kBaseColor[b];
            se.y.reserve(n);
            for (size_t c = 0; c < n; ++c) {
                se.y.push_back(s.cycleDepth[c] > 0 ? s.cycleBaseFraction(c, b) * 100.0 : kNaN);
            }
            se.x = cycleAxis(n);
            series.push_back(se);
        }
        ChartOpts o;
        o.yMin = 0;
        o.unit = "%";
        o.xTipName = "Cycle";
        o.height = 280;
        body += card("", svgLineChart(f("ch-base%zu", p),
                                      std::string("Base composition by cycle \xE2\x80\x94 ") + panels[p].title,
                                      "cycle (1-based position in read)", "fraction of bases (%)",
                                      series, o));
        ++drawn;
    }
    if (drawn == 0) return naBlock("No per-cycle base composition was collected.");
    return body;
}

std::string sectionGc(const RunStats& st) {
    std::array<uint64_t, 101> before{}, after{};
    bool anyAfter = false;
    for (size_t i = 0; i < 101; ++i) {
        before[i] = st.before1.gcHist[i] + st.before2.gcHist[i];
        after[i] = st.after1.gcHist[i] + st.after2.gcHist[i];
        if (after[i] > 0) anyAfter = true;
    }
    double total = 0, mean = 0;
    for (size_t i = 0; i < 101; ++i) total += static_cast<double>(before[i]);
    if (!(total > 0)) return naBlock("No GC distribution was collected (no reads were counted).");
    for (size_t i = 0; i < 101; ++i) mean += static_cast<double>(i) * static_cast<double>(before[i]);
    mean /= total;
    double var = 0;
    for (size_t i = 0; i < 101; ++i) {
        double d = static_cast<double>(i) - mean;
        var += d * d * static_cast<double>(before[i]);
    }
    var /= total;
    double sd = std::sqrt(std::max(var, 1e-9));

    std::vector<double> x = cycleAxis(101);
    for (size_t i = 0; i < 101; ++i) x[i] = static_cast<double>(i);

    Series obs;
    obs.name = "observed (before)";
    obs.color = "var(--c1)";
    obs.x = x;
    Series obsA;
    obsA.name = "observed (after)";
    obsA.color = "var(--c3)";
    obsA.x = x;
    Series fit;
    fit.name = "fitted normal";
    fit.color = "var(--c5)";
    fit.dashed = true;
    fit.x = x;

    double devSum = 0;
    for (size_t i = 0; i < 101; ++i) {
        double o = static_cast<double>(before[i]);
        double d = (static_cast<double>(i) - mean) / sd;
        double e = total * std::exp(-0.5 * d * d) / (sd * std::sqrt(2 * 3.14159265358979323846));
        obs.y.push_back(o);
        obsA.y.push_back(static_cast<double>(after[i]));
        fit.y.push_back(e);
        devSum += std::fabs(o - e);
    }
    double devPct = safeDiv(devSum, 2.0 * total) * 100.0;

    std::vector<Series> series{obs};
    if (anyAfter) series.push_back(obsA);
    series.push_back(fit);

    ChartOpts o;
    o.yMin = 0;
    o.xTipName = "GC";
    o.xUnit = "%";
    o.unit = " reads";
    o.tipDp = 0;
    std::string body = svgLineChart("ch-gc", "GC content distribution", "GC content of a read (%)",
                                    "reads", series, o);
    std::string kind = devPct > 30 ? "bad" : (devPct > 15 ? "warn" : "ok");
    body += calloutBox(kind,
                       "Fitted normal: mean <b>" + htmlEscape(fmtNum(mean, 2)) + "%</b> GC, sd <b>" +
                           htmlEscape(fmtNum(sd, 2)) + "</b>. Deviation of the observed distribution "
                           "from that theoretical curve is <b>" + htmlEscape(fmtNum(devPct, 2)) +
                           "%</b> of reads" +
                           (devPct > 30 ? " &mdash; a bimodal or shifted GC profile usually means "
                                          "contamination or a mixed library."
                                        : (devPct > 15 ? " &mdash; mildly non-normal."
                                                       : " &mdash; consistent with a single clean library.")));
    return body;
}

std::string sectionLength(const RunStats& st) {
    size_t n = std::max(std::max(st.before1.lengthHist.size(), st.before2.lengthHist.size()),
                        std::max(st.after1.lengthHist.size(), st.after2.lengthHist.size()));
    if (n == 0) return naBlock("No read-length distribution was collected.");
    std::vector<double> x, yb, ya;
    x.reserve(n); yb.reserve(n); ya.reserve(n);
    bool anyBefore = false, anyAfter = false;
    for (size_t i = 0; i < n; ++i) {
        double b = 0, a = 0;
        if (i < st.before1.lengthHist.size()) b += static_cast<double>(st.before1.lengthHist[i]);
        if (i < st.before2.lengthHist.size()) b += static_cast<double>(st.before2.lengthHist[i]);
        if (i < st.after1.lengthHist.size()) a += static_cast<double>(st.after1.lengthHist[i]);
        if (i < st.after2.lengthHist.size()) a += static_cast<double>(st.after2.lengthHist[i]);
        x.push_back(static_cast<double>(i));
        yb.push_back(b);
        ya.push_back(a);
        if (b > 0) anyBefore = true;
        if (a > 0) anyAfter = true;
    }
    if (!anyBefore && !anyAfter) return naBlock("No read-length distribution was collected.");

    std::vector<Series> series;
    if (anyBefore) {
        Series s;
        s.name = "before filtering";
        s.color = "var(--c1)";
        s.x = x; s.y = yb;
        s.band = true; s.lo.assign(n, 0.0); s.hi = yb; s.fillOpacity = 0.14;
        series.push_back(s);
    }
    if (anyAfter) {
        Series s;
        s.name = "after filtering";
        s.color = "var(--c3)";
        s.x = x; s.y = ya;
        s.band = true; s.lo.assign(n, 0.0); s.hi = ya; s.fillOpacity = 0.14;
        series.push_back(s);
    }
    ChartOpts o;
    o.yMin = 0;
    o.xTipName = "Length";
    o.xUnit = " bp";
    o.unit = " reads";
    o.tipDp = 0;
    return svgLineChart("ch-len", "Read length distribution", "read length (bp)", "reads", series, o);
}

std::string sectionQualDist(const RunStats& st) {
    std::vector<double> x, yb, ya;
    bool anyB = false, anyA = false;
    for (size_t q = 0; q < kQualBins; ++q) {
        double b = static_cast<double>(st.before1.meanQualHist[q] + st.before2.meanQualHist[q]);
        double a = static_cast<double>(st.after1.meanQualHist[q] + st.after2.meanQualHist[q]);
        x.push_back(static_cast<double>(q));
        yb.push_back(b);
        ya.push_back(a);
        if (b > 0) anyB = true;
        if (a > 0) anyA = true;
    }
    if (!anyB && !anyA) return naBlock("No per-read mean-quality distribution was collected.");
    std::vector<Series> series;
    if (anyB) {
        Series s;
        s.name = "before filtering";
        s.color = "var(--c1)";
        s.x = x; s.y = yb; s.band = true; s.lo.assign(x.size(), 0.0); s.hi = yb;
        series.push_back(s);
    }
    if (anyA) {
        Series s;
        s.name = "after filtering";
        s.color = "var(--c3)";
        s.x = x; s.y = ya; s.band = true; s.lo.assign(x.size(), 0.0); s.hi = ya;
        series.push_back(s);
    }
    ChartOpts o;
    o.yMin = 0;
    o.xTipName = "Mean Q";
    o.unit = " reads";
    o.tipDp = 0;
    return svgLineChart("ch-qdist", "Distribution of per-read mean quality",
                        "mean Phred quality of a read (Q)", "reads", series, o);
}

std::string tileHeatmapFor(const TileAnalyzer& ta, const std::string& label) {
    if (!ta.usable() || ta.tileCount() <= 1) return std::string();
    size_t cycles = std::min<size_t>(ta.cycles, kMaxCycles);
    if (cycles == 0) return naBlock("Tiles were parsed but no per-cycle quality was accumulated.");

    std::vector<std::string> tileKeys;
    tileKeys.reserve(ta.tiles_.size());
    for (const auto& kv : ta.tiles_) tileKeys.push_back(kv.first);
    bool truncated = false;
    if (tileKeys.size() > 240) { tileKeys.resize(240); truncated = true; }

    // Cycles are binned into columns so a 384-tile flow cell still produces a
    // document of sane size: every cell carries its own tooltip payload.
    const size_t kCellBudget = 6000;
    size_t maxCols = std::max<size_t>(24, kCellBudget / std::max<size_t>(1, tileKeys.size()));
    size_t cols = std::min(cycles, std::min<size_t>(120, maxCols));
    if (cols == 0) return naBlock("No cycles to plot.");
    std::vector<size_t> binStart(cols + 1);
    for (size_t i = 0; i <= cols; ++i) {
        binStart[i] = static_cast<size_t>(static_cast<double>(i) * static_cast<double>(cycles) /
                                          static_cast<double>(cols));
    }
    binStart[cols] = cycles;

    // Global (flow-cell) mean per bin, from every tile including any dropped rows.
    std::vector<double> globalMean(cols, kNaN);
    for (size_t b = 0; b < cols; ++b) {
        double sum = 0, dep = 0;
        for (const auto& kv : ta.tiles_) {
            const TileStats& t = kv.second;
            for (size_t c = binStart[b]; c < binStart[b + 1]; ++c) {
                if (c < t.qualSum.size() && c < t.depth.size()) {
                    sum += static_cast<double>(t.qualSum[c]);
                    dep += static_cast<double>(t.depth[c]);
                }
            }
        }
        globalMean[b] = dep > 0 ? sum / dep : kNaN;
    }

    std::vector<double> delta(tileKeys.size() * cols, kNaN);
    std::vector<double> absQ(tileKeys.size() * cols, kNaN);
    double vmax = 0.5;
    for (size_t r = 0; r < tileKeys.size(); ++r) {
        auto it = ta.tiles_.find(tileKeys[r]);
        if (it == ta.tiles_.end()) continue;
        const TileStats& t = it->second;
        for (size_t b = 0; b < cols; ++b) {
            double sum = 0, dep = 0;
            for (size_t c = binStart[b]; c < binStart[b + 1]; ++c) {
                if (c < t.qualSum.size() && c < t.depth.size()) {
                    sum += static_cast<double>(t.qualSum[c]);
                    dep += static_cast<double>(t.depth[c]);
                }
            }
            if (!(dep > 0) || !finite(globalMean[b])) continue;
            double q = sum / dep;
            absQ[r * cols + b] = q;
            delta[r * cols + b] = q - globalMean[b];
            vmax = std::max(vmax, std::fabs(q - globalMean[b]));
        }
    }
    vmax = std::min(vmax, 6.0);

    std::vector<std::string> colLabels(cols);
    for (size_t b = 0; b < cols; ++b) {
        size_t lo = binStart[b] + 1, hi = binStart[b + 1];
        colLabels[b] = (hi > lo) ? f("%zu-%zu", lo, hi) : f("%zu", lo);
    }

    HeatOpts ho;
    ho.cellH = tileKeys.size() <= 24 ? 17.0 : (tileKeys.size() <= 64 ? 11.0 : 7.0);
    ho.cellW = std::max(5.0, std::min(14.0, 900.0 / static_cast<double>(cols)));
    ho.vmax = vmax;
    ho.valueName = "deviation";
    ho.valueUnit = " Q";
    ho.auxName = "tile mean";
    ho.auxUnit = " Q";
    ho.rowName = "tile";
    ho.colName = "cycle";
    ho.xLabel = "cycle (1-based position in read)";
    ho.yLabel = "flow-cell tile (lane:tile)";

    std::string body = svgHeatmap("Per-tile quality deviation \xE2\x80\x94 " + label + " (tile mean Q minus "
                                  "the flow-cell mean at the same cycle)",
                                  tileKeys, colLabels, delta, absQ, ho);
    body += heatLegend(vmax, " Q");
    if (truncated) {
        body += calloutBox("info", "Only the first 240 tiles are drawn; the run contains " +
                                       htmlEscape(fmtNum(static_cast<double>(ta.tileCount()), 0)) + ".");
    }
    return body;
}

std::string sectionTiles(const RunStats& st) {
    bool r1 = st.tiles1.usable() && st.tiles1.tileCount() > 1;
    bool r2 = st.tiles2.usable() && st.tiles2.tileCount() > 1;
    if (!r1 && !r2) {
        return naBlock("Per-tile analysis is not available: it requires Illumina-style read headers "
                       "(instrument:run:flowcell:lane:tile:x:y) and more than one tile, or it was "
                       "disabled for this run.");
    }
    std::string body;
    if (r1) {
        body += "<div class=\"grid stat3\">";
        body += metric("Tiles (read 1)", fmtNum(static_cast<double>(st.tiles1.tileCount()), 0), "parsed from read headers");
        body += metric("Flow-cell mean quality", fmtNum(st.tiles1.globalMeanQuality, 2) + " Q", "across all tiles and cycles");
        body += metric("Outlier tiles", fmtNum(static_cast<double>(st.tiles1.outlierTiles.size()), 0),
                       "deviating by more than 2 Q");
        body += "</div>\n";
        if (!st.tiles1.outlierTiles.empty()) {
            std::string list;
            for (size_t i = 0; i < st.tiles1.outlierTiles.size(); ++i) {
                if (i) list += ", ";
                if (i >= 40) { list += f("and %zu more", st.tiles1.outlierTiles.size() - i); break; }
                list += "<code>" + htmlEscape(st.tiles1.outlierTiles[i]) + "</code>";
            }
            body += calloutBox("warn", "<b>Outlier tiles (read 1):</b> " + list +
                                           ". Tiles that deviate from the flow-cell mean usually "
                                           "indicate a local defect \xE2\x80\x94 bubbles, debris or "
                                           "an out-of-focus region \xE2\x80\x94 and their reads can "
                                           "be filtered out separately.");
        } else {
            body += calloutBox("ok", "No read 1 tile deviates from the flow-cell mean by more than 2 Q.");
        }
        body += tileHeatmapFor(st.tiles1, "read 1");
    }
    if (r2) {
        if (!st.tiles2.outlierTiles.empty()) {
            std::string list;
            for (size_t i = 0; i < st.tiles2.outlierTiles.size(); ++i) {
                if (i) list += ", ";
                if (i >= 40) { list += f("and %zu more", st.tiles2.outlierTiles.size() - i); break; }
                list += "<code>" + htmlEscape(st.tiles2.outlierTiles[i]) + "</code>";
            }
            body += calloutBox("warn", "<b>Outlier tiles (read 2):</b> " + list + ".");
        }
        body += tileHeatmapFor(st.tiles2, "read 2");
    }
    return body;
}

std::string sectionCalibration(const RunStats& st) {
    const ErrorModel& em = st.errorModel;
    if (!em.hasData()) {
        return naBlock("Quality calibration needs overlapping paired-end reads: fewer than 1,000 "
                       "bases could be compared, so no empirical error model was built.");
    }
    std::string body;
    if (em.calibration.empty()) {
        body += naBlock("No reported-quality bin collected enough observations to be calibrated.");
    } else {
        std::vector<double> xs, ys;
        for (const auto& p : em.calibration) {
            xs.push_back(static_cast<double>(p.first));
            ys.push_back(p.second);
        }
        ChartOpts o;
        o.xTipName = "Reported Q";
        o.unit = " Q";
        o.height = 340;
        body += svgScatter("ch-cal", "Reported vs observed quality", "reported Phred quality (Q)",
                           "empirically observed quality (Q)", xs, ys, o);
    }

    std::string words;
    double b = em.calibrationBias;
    if (b < -0.1) {
        words = f("Quality scores are optimistic by %.2f Q on average \xE2\x80\x94 the real error rate is "
                  "higher than the base caller claims.", -b);
    } else if (b > 0.1) {
        words = f("Quality scores are conservative by %.2f Q on average \xE2\x80\x94 the real error rate is "
                  "lower than the base caller claims.", b);
    } else {
        words = f("Quality scores are well calibrated (mean deviation %.2f Q).", b);
    }
    body += calloutBox(b < -2.0 ? "warn" : (b > 2.0 ? "info" : "ok"),
                       htmlEscape(words) + " Overall empirical quality is <b>" +
                           htmlEscape(fmtNum(em.overallQ, 2)) + " Q</b> (error rate " +
                           htmlEscape(fmtPct(em.overallErrorRate, 4)) + ") over " +
                           htmlEscape(fmtCount(static_cast<double>(em.pairsExamined))) +
                           " overlapping pairs.");

    if (!em.calibration.empty()) {
        std::vector<std::vector<std::string>> rows;
        for (const auto& p : em.calibration) {
            size_t q = static_cast<size_t>(p.first);
            double tot = q < kQualBins ? static_cast<double>(em.qTotal[q]) : 0.0;
            double err = q < kQualBins ? static_cast<double>(em.qErrors[q]) : 0.0;
            rows.push_back({htmlEscape(f("Q%d", p.first)), htmlEscape(fmtNum(p.second, 2)),
                            htmlEscape(f("%+.2f", p.second - static_cast<double>(p.first))),
                            htmlEscape(fmtCount(err)), htmlEscape(fmtCount(tot)),
                            htmlEscape(pctOf(err, tot, 4))});
        }
        body += card("Calibration table",
                     table({"Reported", "Observed Q", "Delta", "Errors", "Bases", "Error rate"}, rows, 1));
    }

    // Substitution matrix as a small heat table.
    double subTotal = 0;
    for (size_t i = 0; i < 4; ++i)
        for (size_t j = 0; j < 4; ++j) subTotal += static_cast<double>(em.substitutions[i][j]);
    if (subTotal > 0) {
        std::string t = "<div class=\"scroll\"><table class=\"matrix\"><thead><tr><th>from \\ to</th>";
        for (int j = 0; j < 4; ++j) t += "<th class=\"r\">" + std::string(kBaseName[j]) + "</th>";
        t += "</tr></thead><tbody>";
        double mx = 0;
        for (size_t i = 0; i < 4; ++i)
            for (size_t j = 0; j < 4; ++j)
                if (i != j) mx = std::max(mx, static_cast<double>(em.substitutions[i][j]));
        for (size_t i = 0; i < 4; ++i) {
            t += "<tr><th>" + std::string(kBaseName[i]) + "</th>";
            for (size_t j = 0; j < 4; ++j) {
                double v = static_cast<double>(em.substitutions[i][j]);
                double frac = safeDiv(v, subTotal);
                std::string bg = (i == j) ? "transparent" : sequentialFill(safeDiv(v, mx));
                t += "<td class=\"r\" style=\"background:" + bg + "\" data-tip=\"" +
                     std::string(kBaseName[i]) + " &#8594; " + kBaseName[j] + "||" +
                     htmlEscape(fmtCount(v)) + " events||" + htmlEscape(fmtPct(frac)) +
                     " of substitutions\">" + (i == j ? "<span class=\"dim\">&mdash;</span>"
                                                      : htmlEscape(fmtPct(frac))) +
                     "</td>";
            }
            t += "</tr>";
        }
        t += "</tbody></table></div>\n";
        body += card("Substitution matrix",
                     t + "<p class=\"note\">Percentages are of all " +
                         htmlEscape(fmtCount(subTotal)) +
                         " substitution events observed in read overlaps. A strong asymmetry (for "
                         "example G&rarr;T) points at oxidative damage rather than base-calling noise.</p>");
    }
    return body;
}

std::string sectionErrorRate(const RunStats& st, bool hasR2) {
    const ErrorModel& em = st.errorModel;
    if (!em.hasData() || (em.cycleTotal1.empty() && em.cycleTotal2.empty())) {
        return naBlock("Per-cycle error rates need overlapping paired-end reads; none were available "
                       "for this run.");
    }
    std::vector<Series> series;
    struct Src { const std::vector<uint64_t>* err; const std::vector<uint64_t>* tot; const char* name; const char* color; };
    std::vector<Src> srcs;
    srcs.push_back({&em.cycleErrors1, &em.cycleTotal1, "read 1", "var(--c1)"});
    if (hasR2) srcs.push_back({&em.cycleErrors2, &em.cycleTotal2, "read 2", "var(--c2)"});
    for (const Src& s : srcs) {
        size_t n = std::min(s.err->size(), s.tot->size());
        if (n == 0) continue;
        Series se;
        se.name = s.name;
        se.color = s.color;
        se.markers = false;
        for (size_t c = 0; c < n; ++c) {
            double tot = static_cast<double>((*s.tot)[c]);
            double err = static_cast<double>((*s.err)[c]);
            // A cycle with no observed error cannot be placed on a log axis; the
            // line is broken there rather than clamped to a fake floor.
            se.y.push_back((tot > 0 && err > 0) ? (err / tot) * 100.0 : kNaN);
        }
        se.x = cycleAxis(se.y.size());
        series.push_back(se);
    }
    if (series.empty()) return naBlock("No per-cycle error counts were recorded.");
    ChartOpts o;
    o.logY = true;
    o.unit = "%";
    o.tipDp = 4;
    o.xTipName = "Cycle";
    return svgLineChart("ch-err", "Empirical error rate by cycle (log scale)",
                        "cycle (1-based position in read)", "error rate (%, log scale)", series, o);
}

std::string sectionKmers(const RunStats& st, const Options& opt) {
    const KmerSpectrum& ks = st.kmers;
    if (ks.histogram.size() < 2 || ks.totalKmers == 0) {
        return naBlock("No k-mer spectrum was collected: the analysis was disabled, or the input was "
                       "too short to produce k-mers.");
    }
    size_t last = 0;
    for (size_t i = 1; i < ks.histogram.size(); ++i) if (ks.histogram[i] > 0) last = i;
    if (last == 0) return naBlock("The k-mer count histogram is empty.");
    size_t cap = std::max<size_t>(200, static_cast<size_t>(std::max(1, ks.peakDepth) * 8));
    last = std::min(last, cap);

    Series se;
    se.name = "distinct k-mers";
    se.color = "var(--c1)";
    for (size_t i = 1; i <= last; ++i) {
        se.x.push_back(static_cast<double>(i));
        se.y.push_back(static_cast<double>(ks.histogram[i]));
    }
    ChartOpts o;
    o.logX = true;
    o.logY = true;
    o.tipDp = 0;
    o.unit = " k-mers";
    o.xTipName = "Count";
    if (ks.peakDepth > 0) {
        o.vlines.push_back({static_cast<double>(ks.peakDepth), f("peak %dx", ks.peakDepth)});
    }
    std::string body = svgLineChart("ch-kmer", "k-mer count spectrum (log-log)",
                                    "k-mer multiplicity (times a k-mer was seen)",
                                    "distinct k-mers in the sketch", {se}, o);

    std::string g = "<div class=\"grid\">";
    g += metric("k", fmtNum(ks.k(), 0), "sketch scale 1/" + fmtNum(static_cast<double>(ks.scale()), 0));
    g += metric("Total k-mers", fmtCount(static_cast<double>(ks.totalKmers)),
                fmtCount(static_cast<double>(ks.distinctSketchKmers)) + " distinct in the sketch");
    g += metric("Peak depth", ks.peakDepth > 0 ? fmtNum(ks.peakDepth, 0) + "x" : "\xE2\x80\x94",
                "coverage mode above the error shoulder");
    g += metric("Estimated genome size",
                ks.estimatedGenomeSize > 0 ? fmtBases(ks.estimatedGenomeSize) : "\xE2\x80\x94",
                ks.reliable ? "from the sketch spectrum" : "not reliable");
    g += metric("Estimated depth",
                ks.estimatedDepth > 0 ? fmtNum(ks.estimatedDepth, 2) + "x" : "\xE2\x80\x94",
                "mean coverage of the estimated genome");
    g += metric("Error k-mers", fmtPct(ks.errorKmerFraction),
                "distinct k-mers seen only once or twice");
    g += metric("Repeat fraction", fmtPct(ks.repeatFraction), "k-mers well above the main peak");
    g += "</div>\n";
    body += g;

    if (!ks.reliable) {
        body += calloutBox("warn",
                           "No clear coverage peak was found, so the genome size and depth estimates "
                           "above are not reliable. This is normal for very low coverage, "
                           "amplicon panels, or highly heterogeneous input.");
    }
    if (opt.deep.genomeSize > 0) {
        double expected = static_cast<double>(opt.deep.genomeSize);
        double ratio = safeDiv(ks.estimatedGenomeSize, expected);
        std::string kind = (ratio > 0.8 && ratio < 1.25) ? "ok" : "warn";
        body += calloutBox(kind, "Expected genome size <b>" + htmlEscape(fmtBases(expected)) +
                                     "</b>; the k-mer estimate is <b>" +
                                     htmlEscape(fmtBases(ks.estimatedGenomeSize)) + "</b> (" +
                                     htmlEscape(fmtNum(ratio, 2)) +
                                     "x of expected). Expected depth from the delivered bases is <b>" +
                                     htmlEscape(fmtNum(safeDiv(static_cast<double>(st.basesOut > 0 ? st.basesOut : st.basesIn), expected), 2)) +
                                     "x</b>.");
    }
    return body;
}

std::string sectionDuplication(const RunStats& st) {
    const DuplicationAnalyzer& d = st.duplication;
    if (!d.enabled) {
        return naBlock("Duplication analysis was disabled for this run.");
    }
    double sum = 0;
    for (double v : d.levels) sum += v;
    if (!(sum > 0)) {
        return naBlock("No duplication levels were recorded (too few reads were observed).");
    }
    std::vector<std::string> labels;
    std::vector<double> values;
    for (int i = 0; i < 16; ++i) {
        const char* lb = DuplicationAnalyzer::levelLabel(i);
        labels.push_back(lb ? lb : f("%d", i + 1));
        values.push_back(d.levels[static_cast<size_t>(i)]);   // already a percentage
    }
    BarOpts bo;
    bo.color = "var(--c1)";
    bo.unit = "%";
    bo.tipDp = 2;
    bo.rotateLabels = true;
    std::string body = svgBarChart("Sequence duplication levels", "duplication level (times a sequence was seen)",
                                   "% of reads", labels, values, bo);

    std::string g = "<div class=\"grid\">";
    g += metric("Duplication rate", fmtPct(d.duplicationRate), "fraction of reads that are duplicates");
    g += metric("Observed reads/pairs", fmtCount(static_cast<double>(d.totalObserved)),
                fmtCount(static_cast<double>(d.uniqueObserved)) + " unique keys");
    g += metric("Estimated library size",
                d.estimatedLibrarySize > 0 ? fmtCount(d.estimatedLibrarySize) : "\xE2\x80\x94",
                "unique molecules projected from the duplicate rate");
    g += "</div>\n";
    return body + g;
}

std::string sectionInsert(const RunStats& st) {
    const InsertSizeAnalyzer& is = st.insertSize;
    if (is.counted == 0 || is.histogram.empty()) {
        return naBlock("No insert sizes could be measured: this requires paired-end reads whose mates "
                       "overlap, and none were resolved.");
    }
    size_t last = 0;
    for (size_t i = 0; i < is.histogram.size(); ++i) if (is.histogram[i] > 0) last = i;
    Series se;
    se.name = "read pairs";
    se.color = "var(--c1)";
    se.band = true;
    se.fillOpacity = 0.16;
    for (size_t i = 0; i <= last; ++i) {
        se.x.push_back(static_cast<double>(i));
        se.y.push_back(static_cast<double>(is.histogram[i]));
        se.lo.push_back(0.0);
        se.hi.push_back(static_cast<double>(is.histogram[i]));
    }
    ChartOpts o;
    o.yMin = 0;
    o.tipDp = 0;
    o.unit = " pairs";
    o.xTipName = "Insert";
    o.xUnit = " bp";
    if (is.mode > 0) o.vlines.push_back({static_cast<double>(is.mode), f("mode %d bp", is.mode)});
    if (is.p10 > 0) o.vlines.push_back({static_cast<double>(is.p10), f("p10 %d", is.p10)});
    if (is.p90 > 0) o.vlines.push_back({static_cast<double>(is.p90), f("p90 %d", is.p90)});
    std::string body = svgLineChart("ch-ins", "Insert size distribution", "insert size (bp)",
                                    "read pairs", {se}, o);

    double unknownFrac = safeDiv(static_cast<double>(is.unknown),
                                 static_cast<double>(is.counted + is.unknown));
    std::string g = "<div class=\"grid\">";
    g += metric("Mode", fmtNum(is.mode, 0) + " bp", "most common insert size");
    g += metric("Mean", fmtNum(is.mean, 1) + " bp", "sd " + fmtNum(is.stddev, 1) + " bp");
    g += metric("Median", fmtNum(is.median, 1) + " bp",
                "p10 " + fmtNum(is.p10, 0) + " bp \xC2\xB7 p90 " + fmtNum(is.p90, 0) + " bp");
    g += metric("Pairs measured", fmtCount(static_cast<double>(is.counted)),
                fmtPct(unknownFrac) + " of pairs could not be resolved");
    g += "</div>\n";
    body += g;
    if (last >= is.histogram.size() - 1 && is.maxSize > 0 &&
        static_cast<int>(last) >= is.maxSize) {
        body += calloutBox("info", "The histogram is capped at " +
                                       htmlEscape(fmtNum(is.maxSize, 0)) +
                                       " bp; longer inserts are accumulated in the final bin.");
    }
    if (unknownFrac > 0.5) {
        body += calloutBox("warn", "More than half of the pairs had no detectable overlap, so the "
                                   "insert-size distribution is biased towards short fragments.");
    }
    return body;
}

std::string adapterTable(const std::map<std::string, uint64_t>& counts, double denom,
                         const std::string& label) {
    if (counts.empty()) {
        return card(label, naBlock("No adapter sequences were detected or trimmed for this read."));
    }
    std::vector<std::pair<std::string, uint64_t>> v(counts.begin(), counts.end());
    std::sort(v.begin(), v.end(), [](const std::pair<std::string, uint64_t>& a,
                                     const std::pair<std::string, uint64_t>& b) {
        if (a.second != b.second) return a.second > b.second;
        return a.first < b.first;
    });
    double total = 0;
    for (const auto& p : v) total += static_cast<double>(p.second);
    std::vector<std::vector<std::string>> rows;
    size_t shown = std::min<size_t>(v.size(), 25);
    for (size_t i = 0; i < shown; ++i) {
        double c = static_cast<double>(v[i].second);
        rows.push_back({"<code class=\"seq\">" + htmlEscape(v[i].first) + "</code>",
                        htmlEscape(fmtCount(c)),
                        htmlEscape(denom > 0 ? pctOf(c, denom) : "n/a"),
                        inlineBar(safeDiv(c, total > 0 ? total : 1.0))});
    }
    std::string note;
    if (v.size() > shown) note = "<p class=\"note\">Showing the 25 most frequent of " +
                                 htmlEscape(fmtNum(static_cast<double>(v.size()), 0)) + " sequences.</p>";
    return card(label, table({"Sequence", "Occurrences", "% of reads", ""}, rows, 1) + note);
}

std::string sectionAdapters(const RunStats& st, const Options& opt, bool hasR2) {
    std::string body;
    std::string g = "<div class=\"grid\">";
    double reads1 = static_cast<double>(st.before1.reads);
    double reads2 = static_cast<double>(st.before2.reads);
    g += metric("Reads trimmed (R1)", fmtCount(static_cast<double>(st.adapterTrimmedReads1)),
                reads1 > 0 ? pctOf(static_cast<double>(st.adapterTrimmedReads1), reads1) + " of read 1" : "");
    g += metric("Bases removed (R1)", fmtBases(static_cast<double>(st.adapterTrimmedBases1)), "");
    if (hasR2) {
        g += metric("Reads trimmed (R2)", fmtCount(static_cast<double>(st.adapterTrimmedReads2)),
                    reads2 > 0 ? pctOf(static_cast<double>(st.adapterTrimmedReads2), reads2) + " of read 2" : "");
        g += metric("Bases removed (R2)", fmtBases(static_cast<double>(st.adapterTrimmedBases2)), "");
    }
    g += "</div>\n";
    body += g;

    if (!opt.adapter.enabled) {
        body += calloutBox("info", "Adapter trimming was disabled for this run; the counts above "
                                   "reflect detection only.");
    }
    std::string res1 = opt.adapter.resolvedR1.empty() ? opt.adapter.sequence : opt.adapter.resolvedR1;
    std::string res2 = opt.adapter.resolvedR2.empty() ? opt.adapter.sequenceR2 : opt.adapter.resolvedR2;
    std::string cfg = "<p class=\"note\">Read 1 adapter: <code>" + htmlEscape(res1) + "</code>" +
                      (opt.adapter.detectedR1 ? " (auto-detected)" : "");
    if (hasR2) {
        cfg += " &middot; read 2 adapter: <code>" + htmlEscape(res2) + "</code>" +
               (opt.adapter.detectedR2 ? " (auto-detected)" : "");
    }
    cfg += "</p>\n";
    body += cfg;

    body += adapterTable(st.adapterCounts1, reads1, "Adapter sequences \xE2\x80\x94 read 1");
    if (hasR2) body += adapterTable(st.adapterCounts2, reads2, "Adapter sequences \xE2\x80\x94 read 2");

    // Cumulative adapter-content curve, in the FastQC sense.
    std::vector<Series> series;
    struct Src { const std::vector<uint64_t>* v; double reads; const char* name; const char* color; bool use; };
    const Src srcs[2] = {{&st.adapterStart1, reads1, "read 1", "var(--c1)", !st.adapterStart1.empty()},
                         {&st.adapterStart2, reads2, "read 2", "var(--c2)", hasR2 && !st.adapterStart2.empty()}};
    for (const Src& s : srcs) {
        if (!s.use || !(s.reads > 0)) continue;
        Series se;
        se.name = s.name;
        se.color = s.color;
        double cum = 0;
        for (size_t c = 0; c < s.v->size() && c < static_cast<size_t>(kMaxCycles); ++c) {
            cum += static_cast<double>((*s.v)[c]);
            se.y.push_back(safeDiv(cum, s.reads) * 100.0);
        }
        se.x = cycleAxis(se.y.size());
        if (!se.y.empty()) series.push_back(se);
    }
    if (series.empty()) {
        body += card("Adapter content by cycle",
                     naBlock("No per-cycle adapter positions were recorded."));
    } else {
        ChartOpts o;
        o.yMin = 0;
        o.unit = "%";
        o.xTipName = "Cycle";
        o.height = 280;
        body += card("", svgLineChart("ch-adapt", "Adapter content by cycle (cumulative)",
                                      "cycle (1-based position in read)",
                                      "reads with adapter starting at or before this cycle (%)",
                                      series, o));
    }
    return body;
}

std::string overrepTable(const OverrepAnalyzer& oa, const std::string& label) {
    if (!oa.enabled) {
        return card(label, naBlock("Overrepresented-sequence analysis was not enabled for this run "
                                   "(it is off by default because it costs an extra pass over the "
                                   "sampled reads)."));
    }
    if (oa.hits.empty()) {
        return card(label, naBlock("No sequence exceeded the overrepresentation threshold."));
    }
    std::vector<std::vector<std::string>> rows;
    size_t shown = std::min<size_t>(oa.hits.size(), 30);
    for (size_t i = 0; i < shown; ++i) {
        const OverrepHit& h = oa.hits[i];
        std::string seq = h.seq;
        std::string disp = seq.size() > 60 ? seq.substr(0, 57) + "..." : seq;
        rows.push_back({"<code class=\"seq\" title=\"" + htmlEscape(seq) + "\">" + htmlEscape(disp) +
                            "</code>",
                        htmlEscape(fmtCount(static_cast<double>(h.count))),
                        htmlEscape(fmtPct(h.percent / 100.0)),   // OverrepHit::percent is 0..100
                        h.source.empty() ? "<span class=\"dim\">unknown</span>" : htmlEscape(h.source),
                        sparkline(h.positions)});
    }
    std::string note = "<p class=\"note\">Sampling 1 read in " +
                       htmlEscape(fmtNum(oa.sampling, 0)) + "; " +
                       htmlEscape(fmtCount(static_cast<double>(oa.sampled))) +
                       " reads sampled. The sparkline shows where in the read each sequence occurs "
                       "(first cycle on the left).</p>";
    return card(label, table({"Sequence", "Count", "% of reads", "Identified source", "Position profile"},
                             rows, 1) + note);
}

std::string sectionOverrep(const RunStats& st, bool hasR2) {
    std::string body = overrepTable(st.overrep1, "Overrepresented sequences \xE2\x80\x94 read 1");
    if (hasR2) body += overrepTable(st.overrep2, "Overrepresented sequences \xE2\x80\x94 read 2");
    return body;
}

// The 5-mer profile is indexed with the first base most significant and
// A=0, C=1, G=2, T=3, matching the accumulator in stats.cpp.
std::string decodeKmer(size_t index, int k) {
    std::string s(static_cast<size_t>(k), 'A');
    const char* bases = "ACGT";
    for (int i = k - 1; i >= 0; --i) {
        s[static_cast<size_t>(i)] = bases[index & 3u];
        index >>= 2;
    }
    return s;
}

std::string sectionKmerComposition(const RunStats& st) {
    const size_t want = 1u << (2 * kStatKmerK);
    std::vector<double> prof(want, 0.0);
    double total = 0;
    bool any = false;
    for (const ReadSetStats* s : {&st.before1, &st.before2}) {
        if (s->kmerProfile.size() < want) continue;
        any = true;
        for (size_t i = 0; i < want; ++i) {
            prof[i] += static_cast<double>(s->kmerProfile[i]);
            total += static_cast<double>(s->kmerProfile[i]);
        }
    }
    if (!any || !(total > 0)) {
        return naBlock("No 5-mer composition profile was collected.");
    }
    double expected = total / static_cast<double>(want);
    std::vector<std::pair<double, size_t>> order;
    order.reserve(want);
    for (size_t i = 0; i < want; ++i) order.push_back({safeDiv(prof[i], expected), i});
    std::sort(order.begin(), order.end(),
              [](const std::pair<double, size_t>& a, const std::pair<double, size_t>& b) {
                  return a.first > b.first;
              });

    size_t shown = std::min<size_t>(20, order.size());
    std::vector<std::string> labels;
    std::vector<double> values;
    std::vector<std::vector<std::string>> rows;
    double maxEnr = order.empty() ? 1.0 : std::max(1.0, order[0].first);
    for (size_t i = 0; i < shown; ++i) {
        size_t idx = order[i].second;
        std::string km = decodeKmer(idx, kStatKmerK);
        labels.push_back(km);
        values.push_back(order[i].first);
        rows.push_back({"<code>" + htmlEscape(km) + "</code>",
                        htmlEscape(fmtCount(prof[idx])),
                        htmlEscape(fmtPct(safeDiv(prof[idx], total), 3)),
                        htmlEscape(fmtNum(order[i].first, 2) + "x"),
                        inlineBar(safeDiv(order[i].first, maxEnr), "var(--c2)")});
    }
    BarOpts bo;
    bo.color = "var(--c2)";
    bo.unit = "x expected";
    bo.rotateLabels = true;
    bo.tipDp = 2;
    std::string body = svgBarChart("Top 20 enriched 5-mers", "5-mer",
                                   "observed / expected frequency", labels, values, bo);
    body += "<p class=\"note\">Expected frequency under a uniform model is 1 / " +
            htmlEscape(fmtNum(static_cast<double>(want), 0)) + " = " +
            htmlEscape(fmtPct(1.0 / static_cast<double>(want), 3)) + " (" +
            htmlEscape(fmtCount(expected)) + " occurrences each, from " +
            htmlEscape(fmtCount(total)) + " counted 5-mers). Strong enrichment of a low-complexity "
            "k-mer such as GGGGG points at polyG artefacts; enrichment of a structured k-mer usually "
            "reflects adapters or primer bias.</p>\n";
    body += table({"5-mer", "Count", "Observed", "Enrichment", ""}, rows, 1);
    return body;
}

std::string sectionContamination(const RunStats& st) {
    const ContamResult& c = st.contamination;
    if (!c.attempted) return "";
    if (!c.ok) {
        return calloutBox("warn", "The contamination screen did not run: " +
                                      htmlEscape(c.message) + ".");
    }

    const bool pure = c.verdict == "pure";
    const bool none = c.verdict == "no panel species detected";
    const char* kind = pure ? "ok" : (none ? "info" : "warn");

    std::string head;
    if (none) {
        head = "<b>No panel species detected.</b> Only " + fmtCount(static_cast<double>(c.totalHits)) +
               " of " + fmtCount(static_cast<double>(c.kmersScreened)) +
               " screened k-mers matched anything in the panel, which is what an organism "
               "outside the 23-species panel looks like. This is not evidence that the "
               "library is clean, only that nothing on the panel is in it.";
    } else {
        head = "<b>" + htmlEscape(util::toUpper(c.verdict)) + "</b> &mdash; " +
               htmlEscape(c.dominant) + " at " + fmtPct(c.dominantFraction) +
               " of the k-mers assigned to the panel";
        if (!c.topContaminant.empty()) {
            head += ", top other species " + htmlEscape(c.topContaminant) + " at " +
                    fmtPct(c.topContaminantFraction);
        }
        head += ".";
    }
    std::string body = calloutBox(kind, head);

    body += "<div class=\"grid\">";
    body += metric("Verdict", c.verdict);
    body += metric("Dominant species", c.dominant.empty() ? std::string("none") : c.dominant,
                   c.dominant.empty() ? "" : fmtPct(c.dominantFraction) + " of assigned k-mers");
    body += metric("Reads screened", fmtCount(static_cast<double>(c.readsScreened)),
                   fmtCount(static_cast<double>(c.kmersScreened)) + " k-mers (k=" +
                       std::to_string(c.k) + ")");
    body += metric("Reporting floor", fmtPct(c.noiseFloor),
                   "species below this are not called");
    body += "</div>";

    std::vector<std::string> labels;
    std::vector<double> values;
    std::vector<std::vector<std::string>> rows;
    for (const ContamSpeciesResult& s : c.species) {
        if (!s.present && s.matchedKmers < 10) continue;
        if (labels.size() < 12 && s.relativeAbundance > 0) {
            labels.push_back(s.name);
            values.push_back(s.relativeAbundance * 100.0);
        }
        std::string name = htmlEscape(s.name);
        if (s.commonContaminant) {
            name += " <span class=\"note\">(common skin / reagent contaminant)</span>";
        }
        rows.push_back({
            name,
            s.present ? "<b>yes</b>" : "no",
            inlineBar(s.relativeAbundance) + " " + fmtPct(s.relativeAbundance),
            fmtPct(s.libraryFraction),
            s.estimatedDepth > 0 ? fmtNum(s.estimatedDepth, 1) + "&times;" : "&mdash;",
            fmtCount(static_cast<double>(s.matchedKmers)) + " / " +
                fmtCount(static_cast<double>(s.sketchSize)),
            fmtCount(static_cast<double>(s.hits)),
        });
    }

    if (!labels.empty()) {
        BarOpts o;
        o.rotateLabels = true;
        o.unit = "%";
        o.height = 340;
        o.color = "var(--c3)";
        body += card("Species abundance",
                     svgBarChart("Relative abundance among panel species", "", "% of assigned k-mers",
                                 labels, values, o));
    }

    if (!rows.empty()) {
        body += card("Per-species detail",
                     table({"Species", "Called", "Relative abundance", "Library fraction",
                            "Est. depth", "Sketch k-mers matched", "Hits"},
                           rows),
                     "Relative abundance is the share among panel species; library fraction is the "
                     "estimated share of the whole library, scaled back up from the FracMinHash "
                     "sketch. Species with fewer than 10 matched k-mers are omitted.");
    }

    body += "<p class=\"note\">Panel k-mers are canonical 31-mers unique to a single species "
            "within the panel, so a hit is specific rather than merely similar. Species outside "
            "the panel cannot be named, and a close off-panel relative contributes no signal "
            "rather than a wrong call. Database: <code>" + htmlEscape(c.dbPath) + "</code>.</p>\n";
    return body;
}

std::string sectionConfig(const Options& opt) {
    std::string body = "<details class=\"cfg\"><summary>Effective configuration</summary><pre>";
    body += htmlEscape(opt.describe());
    body += "</pre></details>\n";
    return body;
}

// ---------------------------------------------------------------------------
// Document assembly
// ---------------------------------------------------------------------------

struct SectionEntry {
    std::string id, title, html;
};

const char* kCss = R"CSS(
*,*::before,*::after{box-sizing:border-box}
:root{
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans",sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,"Liberation Mono",monospace;
  --bg:#f3f5f8; --card:#ffffff; --fg:#1b2430; --muted:#5f6d7c; --border:#dfe5ec; --grid:#eaeff4;
  --plot:rgba(127,140,155,.05); --hover:rgba(110,135,160,.16); --code:#f2f5f8;
  --accent:#2f6f9f;
  --c1:#2f6f9f; --c2:#c07038; --c3:#4a8b5c; --c4:#8a5aa6; --c5:#b04a4a; --c6:#7d8894;
  --c7:#a8862c; --c8:#3a8b93;
  --ok:#3d7a50; --warn:#8f6519; --bad:#a1413f;
  --okbg:rgba(61,122,80,.10); --warnbg:rgba(143,101,25,.12); --badbg:rgba(161,65,63,.10);
  --infobg:rgba(47,111,159,.09);
  --zone-g:rgba(74,139,92,.11); --zone-a:rgba(168,134,44,.12); --zone-r:rgba(176,74,74,.11);
  --shadow:0 1px 2px rgba(16,24,32,.05),0 2px 10px rgba(16,24,32,.04);
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    --bg:#11151a; --card:#191f26; --fg:#e2e8ef; --muted:#95a3b2; --border:#2a333d; --grid:#242e38;
    --plot:rgba(160,180,200,.04); --hover:rgba(150,175,200,.18); --code:#141a20;
    --accent:#6fb0e0;
    --c1:#6fb0e0; --c2:#e0975f; --c3:#78c08c; --c4:#c08ad0; --c5:#e07f7f; --c6:#9aa6b3;
    --c7:#d6bf60; --c8:#5fc0c8;
    --ok:#7bc496; --warn:#dcbb63; --bad:#e58c8a;
    --okbg:rgba(123,196,150,.12); --warnbg:rgba(220,187,99,.13); --badbg:rgba(229,140,138,.12);
    --infobg:rgba(111,176,224,.12);
    --zone-g:rgba(120,192,140,.10); --zone-a:rgba(214,191,96,.10); --zone-r:rgba(224,127,127,.10);
    --shadow:0 1px 2px rgba(0,0,0,.35),0 2px 10px rgba(0,0,0,.25);
  }
}
:root[data-theme="dark"]{
  --bg:#11151a; --card:#191f26; --fg:#e2e8ef; --muted:#95a3b2; --border:#2a333d; --grid:#242e38;
  --plot:rgba(160,180,200,.04); --hover:rgba(150,175,200,.18); --code:#141a20;
  --accent:#6fb0e0;
  --c1:#6fb0e0; --c2:#e0975f; --c3:#78c08c; --c4:#c08ad0; --c5:#e07f7f; --c6:#9aa6b3;
  --c7:#d6bf60; --c8:#5fc0c8;
  --ok:#7bc496; --warn:#dcbb63; --bad:#e58c8a;
  --okbg:rgba(123,196,150,.12); --warnbg:rgba(220,187,99,.13); --badbg:rgba(229,140,138,.12);
  --infobg:rgba(111,176,224,.12);
  --zone-g:rgba(120,192,140,.10); --zone-a:rgba(214,191,96,.10); --zone-r:rgba(224,127,127,.10);
  --shadow:0 1px 2px rgba(0,0,0,.35),0 2px 10px rgba(0,0,0,.25);
}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--font);font-size:14px;
  line-height:1.55;overflow-x:hidden}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
code{font-family:var(--mono);font-size:.92em;background:var(--code);padding:1px 5px;border-radius:4px;
  border:1px solid var(--border)}
pre{font-family:var(--mono);font-size:12.5px;background:var(--code);border:1px solid var(--border);
  border-radius:8px;padding:14px;overflow-x:auto;margin:0;white-space:pre-wrap;word-break:break-word}
.wrap{max-width:1360px;margin:0 auto;padding:22px}
header.top{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:22px 24px;
  box-shadow:var(--shadow);margin-bottom:20px}
.brand{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.brand h1{margin:0;font-size:24px;letter-spacing:-.4px}
.brand .ver{color:var(--muted);font-family:var(--mono);font-size:12.5px}
.brand .rt{color:var(--muted);font-size:15px}
.themebtn{margin-left:auto;background:transparent;color:var(--fg);border:1px solid var(--border);
  border-radius:8px;padding:6px 12px;font:inherit;font-size:12.5px;cursor:pointer}
.themebtn:hover{border-color:var(--accent);color:var(--accent)}
dl.meta{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;margin:16px 0 0;font-size:13px}
dl.meta dt{color:var(--muted)}
dl.meta dd{margin:0;font-family:var(--mono);font-size:12.5px;word-break:break-all}
.verdict{margin-top:18px;border-radius:12px;padding:14px 16px;border:1px solid var(--border);
  display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.verdict .badge{font-weight:700;letter-spacing:.10em;font-size:13px;padding:6px 14px;border-radius:8px;
  white-space:nowrap}
.verdict.pass{background:var(--okbg);border-color:var(--ok)}
.verdict.pass .badge{background:var(--ok);color:var(--card)}
.verdict.warn{background:var(--warnbg);border-color:var(--warn)}
.verdict.warn .badge{background:var(--warn);color:var(--card)}
.verdict.fail{background:var(--badbg);border-color:var(--bad)}
.verdict.fail .badge{background:var(--bad);color:var(--card)}
.verdict ul{margin:0;padding-left:20px}
.verdict li{margin:2px 0}
.layout{display:grid;grid-template-columns:220px minmax(0,1fr);gap:22px;align-items:start}
nav.toc{position:sticky;top:22px;background:var(--card);border:1px solid var(--border);border-radius:12px;
  padding:14px;box-shadow:var(--shadow);max-height:calc(100vh - 44px);overflow-y:auto}
nav.toc h2{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);margin:0 0 8px}
nav.toc ol{list-style:none;margin:0;padding:0;counter-reset:s}
nav.toc li{counter-increment:s;margin:1px 0}
nav.toc a{display:block;padding:5px 8px;border-radius:7px;color:var(--fg);font-size:12.5px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
nav.toc a::before{content:counter(s) ". ";color:var(--muted)}
nav.toc a:hover{background:var(--hover);text-decoration:none}
nav.toc a.active{background:var(--hover);color:var(--accent);font-weight:600}
main{min-width:0}
section.sec{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px 22px;
  margin-bottom:20px;box-shadow:var(--shadow);scroll-margin-top:16px}
section.sec>h2{margin:0 0 14px;font-size:17px;letter-spacing:-.2px;
  padding-bottom:10px;border-bottom:1px solid var(--border)}
.card{border:1px solid var(--border);border-radius:11px;padding:14px;margin:14px 0;background:var(--card)}
.card h3{margin:0 0 10px;font-size:14px;color:var(--fg)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(212px,1fr));gap:12px;margin:4px 0 14px}
.metric{border:1px solid var(--border);border-radius:11px;padding:12px 14px;background:var(--card)}
.metric .ml{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
.metric .mv{font-size:21px;font-weight:650;letter-spacing:-.4px;margin:3px 0 2px;
  font-variant-numeric:tabular-nums}
.metric .ms{font-size:12px;color:var(--muted)}
.scroll{overflow-x:auto;overflow-y:hidden;max-width:100%;-webkit-overflow-scrolling:touch}
.cw{width:100%}
svg.chart{width:100%;height:auto;display:block;overflow:visible}
svg.chart text{font-family:var(--font);font-size:11px;fill:var(--muted)}
svg.chart text.ct{font-size:13px;font-weight:600;fill:var(--fg)}
svg.chart text.axl{font-size:11.5px;fill:var(--muted)}
svg.chart text.vmt{font-size:10.5px;fill:var(--muted)}
svg.chart .grid{stroke:var(--grid);stroke-width:1}
svg.chart .axis{stroke:var(--border);stroke-width:1}
svg.chart .plotbg{fill:var(--plot)}
svg.chart .ln{stroke-width:1.7;stroke-linejoin:round;stroke-linecap:round}
svg.chart .ident{stroke:var(--muted);stroke-width:1;stroke-dasharray:4 4;opacity:.8}
svg.chart .vmark{stroke:var(--c5);stroke-width:1.2;stroke-dasharray:3 3;opacity:.85}
svg.chart .hb{fill:transparent}
svg.chart .hb:hover{fill:var(--hover)}
svg.chart .bar:hover,svg.chart .seg:hover,svg.chart .hm:hover{opacity:.75}
svg.chart .lg{cursor:pointer}
svg.chart .lg text{fill:var(--fg);font-size:11.5px}
svg.chart .lg.off{opacity:.35}
svg.spark{vertical-align:middle}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:420px}
th,td{padding:7px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:middle}
th{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600;
  white-space:nowrap}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:hover{background:var(--hover)}
table.matrix{min-width:340px;width:auto}
table.matrix th{text-align:right}
table.matrix td{font-variant-numeric:tabular-nums}
.ibar{display:inline-block;width:110px;height:8px;border-radius:4px;background:var(--grid);overflow:hidden;
  vertical-align:middle}
.ibar i{display:block;height:100%;border-radius:4px}
.na{color:var(--muted);font-style:italic;background:var(--plot);border:1px dashed var(--border);
  border-radius:9px;padding:11px 13px;margin:8px 0}
.note{color:var(--muted);font-size:12.5px;margin:6px 0 10px}
.dim{color:var(--muted)}
.seq{word-break:break-all;font-size:12px}
.callout{border:1px solid var(--border);border-left-width:4px;border-radius:9px;padding:11px 14px;
  margin:12px 0;font-size:13px}
.callout.ok{background:var(--okbg);border-left-color:var(--ok)}
.callout.warn{background:var(--warnbg);border-left-color:var(--warn)}
.callout.bad{background:var(--badbg);border-left-color:var(--bad)}
.callout.info{background:var(--infobg);border-left-color:var(--accent)}
.hlgd{display:flex;align-items:center;gap:2px;margin:6px 0 2px;font-size:11.5px}
.hlgd i{display:inline-block;width:14px;height:11px}
.hlgd span:first-child{margin-right:6px}
.hlgd span:last-child{margin-left:6px}
details.cfg summary{cursor:pointer;font-weight:600;padding:6px 0}
.tip{position:fixed;z-index:99;pointer-events:none;background:var(--card);color:var(--fg);
  border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:12px;
  box-shadow:var(--shadow);max-width:280px;line-height:1.4}
.tip .tth{font-weight:650;margin-bottom:2px}
footer{color:var(--muted);font-size:12px;text-align:center;padding:8px 0 26px}
@media (max-width:900px){
  .wrap{padding:14px}
  .layout{grid-template-columns:minmax(0,1fr)}
  nav.toc{position:sticky;top:0;z-index:5;max-height:none;border-radius:10px}
  nav.toc ol{display:flex;gap:6px;overflow-x:auto;padding-bottom:4px}
  nav.toc a{white-space:nowrap}
  section.sec{padding:16px 14px}
  .grid{grid-template-columns:repeat(auto-fill,minmax(160px,1fr))}
}
@media print{
  nav.toc,.themebtn{display:none}
  body{background:#fff}
  section.sec{break-inside:avoid;box-shadow:none}
}
)CSS";

const char* kJs = R"JS(
(function(){
  var root=document.documentElement;
  try{var st=localStorage.getItem('fp-theme'); if(st==='dark'||st==='light') root.setAttribute('data-theme',st);}catch(e){}
  var btn=document.getElementById('themebtn');
  if(btn){btn.addEventListener('click',function(){
    var cur=root.getAttribute('data-theme');
    if(!cur){cur=(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches)?'dark':'light';}
    var next=(cur==='dark')?'light':'dark';
    root.setAttribute('data-theme',next);
    try{localStorage.setItem('fp-theme',next);}catch(e){}
  });}

  var tip=document.createElement('div');
  tip.className='tip'; tip.style.display='none';
  document.body.appendChild(tip);
  function place(e){
    var x=e.clientX+15, y=e.clientY+15;
    var r=tip.getBoundingClientRect();
    if(x+r.width>window.innerWidth-8) x=Math.max(4,e.clientX-r.width-15);
    if(y+r.height>window.innerHeight-8) y=Math.max(4,e.clientY-r.height-15);
    tip.style.left=x+'px'; tip.style.top=y+'px';
  }
  function target(e){ return (e.target&&e.target.closest)?e.target.closest('[data-tip]'):null; }
  function show(el,e){
    while(tip.firstChild) tip.removeChild(tip.firstChild);
    var parts=(el.getAttribute('data-tip')||'').split('||');
    for(var i=0;i<parts.length;i++){
      var d=document.createElement('div');
      if(i===0) d.className='tth';
      d.innerHTML=parts[i];
      tip.appendChild(d);
    }
    tip.style.display='block'; place(e);
  }
  document.addEventListener('mousemove',function(e){
    var el=target(e);
    if(el){ if(tip.dataset.k!==el.getAttribute('data-tip')){tip.dataset.k=el.getAttribute('data-tip'); show(el,e);} else place(e); }
    else if(tip.style.display!=='none'){ tip.style.display='none'; tip.dataset.k=''; }
  },{passive:true});
  document.addEventListener('mouseleave',function(){ tip.style.display='none'; tip.dataset.k=''; });

  document.addEventListener('click',function(e){
    var lg=(e.target&&e.target.closest)?e.target.closest('.lg'):null;
    if(!lg) return;
    var svg=document.getElementById(lg.getAttribute('data-chart'));
    if(!svg) return;
    var g=svg.querySelector('.sr[data-s="'+lg.getAttribute('data-s')+'"]');
    if(!g) return;
    var hidden=(g.style.display==='none');
    g.style.display=hidden?'':'none';
    if(hidden) lg.classList.remove('off'); else lg.classList.add('off');
  });

  var links=[].slice.call(document.querySelectorAll('nav.toc a'));
  var secs=links.map(function(a){return document.getElementById(a.getAttribute('href').slice(1));});
  function spy(){
    var best=-1, bestTop=-1e9;
    for(var i=0;i<secs.length;i++){
      if(!secs[i]) continue;
      var t=secs[i].getBoundingClientRect().top-90;
      if(t<=0 && t>bestTop){bestTop=t; best=i;}
    }
    for(var j=0;j<links.length;j++){ if(j===best) links[j].classList.add('active'); else links[j].classList.remove('active'); }
  }
  window.addEventListener('scroll',spy,{passive:true});
  spy();
})();
)JS";

}  // namespace

void writeHtmlReport(const std::string& path, const RunStats& stats, const Options& opt) {
    const bool hasR2 = stats.before2.reads > 0 || (opt.isPaired() && stats.after2.reads > 0);

    std::vector<SectionEntry> secs;
    auto add = [&secs](const char* id, const char* title, std::string html) {
        if (html.empty()) return;
        secs.push_back(SectionEntry{id, title, std::move(html)});
    };

    add("summary", "Summary", sectionSummary(stats, opt));
    add("contamination", "Contamination screen", sectionContamination(stats));
    add("filtering", "Filtering breakdown", sectionFiltering(stats));
    add("quality", "Per-cycle quality", sectionQuality(stats));
    add("basecontent", "Per-cycle base content", sectionBaseContent(stats));
    add("gc", "GC content distribution", sectionGc(stats));
    add("length", "Read length distribution", sectionLength(stats));
    add("qualdist", "Quality score distribution", sectionQualDist(stats));
    add("tiles", "Per-tile quality", sectionTiles(stats));
    add("calibration", "Quality calibration", sectionCalibration(stats));
    add("errorrate", "Per-cycle error rate", sectionErrorRate(stats, hasR2));
    add("kmers", "k-mer spectrum", sectionKmers(stats, opt));
    add("duplication", "Duplication", sectionDuplication(stats));
    add("insert", "Insert size", sectionInsert(stats));
    add("adapters", "Adapters", sectionAdapters(stats, opt, hasR2));
    add("overrep", "Overrepresented sequences", sectionOverrep(stats, hasR2));
    add("kmercomp", "5-mer composition", sectionKmerComposition(stats));
    add("config", "Effective configuration", sectionConfig(opt));

    Verdict v = computeVerdict(stats, opt);
    const char* vclass = v.level == 2 ? "fail" : (v.level == 1 ? "warn" : "pass");
    const char* vtext = v.level == 2 ? "FAIL" : (v.level == 1 ? "WARN" : "PASS");

    std::string version = stats.version.empty() ? std::string("unknown version") : stats.version;
    std::string title = opt.reportTitle.empty() ? std::string("scepter report") : opt.reportTitle;

    std::string out;
    out.reserve(1u << 19);
    out += "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n";
    out += "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n";
    out += "<meta name=\"generator\" content=\"scepter " + htmlEscape(version) + "\">\n";
    out += "<title>" + htmlEscape(title) + "</title>\n<style>";
    out += kCss;
    out += "</style>\n</head>\n<body>\n<div class=\"wrap\">\n";

    // --- header -------------------------------------------------------------
    out += "<header class=\"top\" id=\"top\">\n<div class=\"brand\"><h1>scepter</h1>"
           "<span class=\"ver\">" + htmlEscape(version) + "</span>"
           "<span class=\"rt\">" + htmlEscape(title) + "</span>"
           "<button class=\"themebtn\" id=\"themebtn\" type=\"button\">Toggle theme</button></div>\n";
    out += "<dl class=\"meta\">\n";
    auto meta = [&out](const char* k, const std::string& val) {
        if (val.empty()) return;
        out += "<dt>" + std::string(k) + "</dt><dd>" + htmlEscape(val) + "</dd>\n";
    };
    meta("Command", stats.command);
    meta("Started", stats.startedAt);
    out += "<dt>Duration</dt><dd>" + htmlEscape(fmtDuration(stats.durationSeconds)) + "</dd>\n";
    meta("Input read 1", opt.in1.empty() ? (opt.stdinInput ? std::string("<stdin>") : std::string()) : opt.in1);
    meta("Input read 2", opt.in2);
    meta("Output read 1", opt.out1.empty() ? (opt.stdoutOutput ? std::string("<stdout>") : std::string()) : opt.out1);
    meta("Output read 2", opt.out2);
    // The layout line reports the configured run; the read 2 panels below follow
    // the data, so a paired run that produced no R2 reads still renders cleanly.
    out += "<dt>Layout</dt><dd>" + std::string(opt.isPaired() ? "paired-end" : "single-end") + "</dd>\n";
    out += "</dl>\n";

    out += "<div class=\"verdict " + std::string(vclass) + "\"><span class=\"badge\">" + vtext +
           "</span><div><b>Top findings</b><ul>";
    for (const std::string& r : v.reasons) out += "<li>" + htmlEscape(r) + "</li>";
    out += "</ul></div></div>\n</header>\n";

    // --- body ---------------------------------------------------------------
    out += "<div class=\"layout\">\n<nav class=\"toc\"><h2>Sections</h2><ol>\n";
    for (const SectionEntry& s : secs) {
        out += "<li><a href=\"#" + htmlEscape(s.id) + "\">" + htmlEscape(s.title) + "</a></li>\n";
    }
    out += "</ol></nav>\n<main>\n";
    for (const SectionEntry& s : secs) {
        out += "<section class=\"sec\" id=\"" + htmlEscape(s.id) + "\"><h2>" + htmlEscape(s.title) +
               "</h2>\n" + s.html + "</section>\n";
    }
    out += "</main>\n</div>\n";

    out += "<footer>Generated by scepter " + htmlEscape(version);
    if (!stats.startedAt.empty()) out += " &middot; run started " + htmlEscape(stats.startedAt);
    out += " &middot; this file is fully self-contained</footer>\n";
    out += "</div>\n<script>";
    out += kJs;
    out += "</script>\n</body>\n</html>\n";

    std::ofstream os(path.c_str(), std::ios::out | std::ios::binary | std::ios::trunc);
    if (!os) {
        std::fprintf(stderr, "scepter: cannot open HTML report '%s' for writing\n", path.c_str());
        return;
    }
    os.write(out.data(), static_cast<std::streamsize>(out.size()));
    if (!os) {
        std::fprintf(stderr, "scepter: failed while writing HTML report '%s'\n", path.c_str());
    }
}

}  // namespace fp
