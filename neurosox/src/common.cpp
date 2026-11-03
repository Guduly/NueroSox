#include "neurosox/common.hpp"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <numeric>

namespace neurosox {

const char* channel_name(Channel c) { return c == Channel::ECG ? "ECG_1" : "PPG_1"; }

static std::string trim(const std::string& s) {
  size_t a = 0, b = s.size();
  while (a < b && (s[a] == ' ' || s[a] == '\t' || s[a] == '\r')) ++a;
  while (b > a && (s[b - 1] == ' ' || s[b - 1] == '\t' || s[b - 1] == '\r')) --b;
  return s.substr(a, b - a);
}

void StreamConfig::apply(const std::string& key_in, const std::string& value_in) {
  const std::string key = trim(key_in), value = trim(value_in);
  if (key.empty()) return;
  if (key == "subject_id") {
    subject_id = value;
  } else if (key == "session_id") {
    session_id = value;
  } else if (key == "foot_side") {
    foot_side = value;
  } else if (key == "activity") {
    activity = value;
  } else if (key == "adc_bits") {
    int b = std::atoi(value.c_str());
    if (b >= 8 && b <= 31) adc_bits = b;
  } else if (key == "adc_signed") {
    adc_signed = (value == "1" || value == "true");
  } else if (key == "ppg_invert") {
    ppg_invert = (value == "1" || value == "true");
  } else {
    extra[key] = value;
  }
}

int64_t StreamConfig::adc_min() const {
  return adc_signed ? -(int64_t(1) << (adc_bits - 1)) : 0;
}
int64_t StreamConfig::adc_max() const {
  return adc_signed ? (int64_t(1) << (adc_bits - 1)) - 1 : (int64_t(1) << adc_bits) - 1;
}

double mean_of(const std::vector<double>& v) {
  if (v.empty()) return 0.0;
  return std::accumulate(v.begin(), v.end(), 0.0) / double(v.size());
}

double quantile_of(std::vector<double> v, double q) {
  if (v.empty()) return 0.0;
  std::sort(v.begin(), v.end());
  q = std::min(1.0, std::max(0.0, q));
  const double pos = q * double(v.size() - 1);
  const size_t lo = size_t(std::floor(pos));
  const size_t hi = std::min(v.size() - 1, lo + 1);
  return v[lo] + (pos - double(lo)) * (v[hi] - v[lo]);
}

double median_of(std::vector<double> v) { return quantile_of(std::move(v), 0.5); }

double stdev_sample(const std::vector<double>& v) {
  if (v.size() < 2) return 0.0;
  const double m = mean_of(v);
  double acc = 0.0;
  for (double x : v) acc += (x - m) * (x - m);
  return std::sqrt(acc / double(v.size() - 1));
}

double pearson(const std::vector<double>& a, const std::vector<double>& b) {
  const size_t n = std::min(a.size(), b.size());
  if (n < 2) return 0.0;
  double ma = 0, mb = 0;
  for (size_t i = 0; i < n; ++i) { ma += a[i]; mb += b[i]; }
  ma /= double(n); mb /= double(n);
  double sab = 0, saa = 0, sbb = 0;
  for (size_t i = 0; i < n; ++i) {
    const double da = a[i] - ma, db = b[i] - mb;
    sab += da * db; saa += da * da; sbb += db * db;
  }
  if (saa <= 0.0 || sbb <= 0.0) return 0.0;
  return sab / std::sqrt(saa * sbb);
}

int64_t floor_div(int64_t num, int64_t den) {
  int64_t q = num / den;
  if ((num % den != 0) && ((num < 0) != (den < 0))) --q;
  return q;
}
int64_t ceil_div(int64_t num, int64_t den) { return -floor_div(-num, den); }

// Howard Hinnant's civil-from-days algorithm.
static void civil_from_days(int64_t z, int& y, unsigned& m, unsigned& d) {
  z += 719468;
  const int64_t era = (z >= 0 ? z : z - 146096) / 146097;
  const unsigned doe = unsigned(z - era * 146097);
  const unsigned yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
  const int64_t yy = int64_t(yoe) + era * 400;
  const unsigned doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
  const unsigned mp = (5 * doy + 2) / 153;
  d = doy - (153 * mp + 2) / 5 + 1;
  m = mp < 10 ? mp + 3 : mp - 9;
  y = int(yy + (m <= 2));
}

std::string format_utc(int64_t unix_us) {
  const int64_t secs = floor_div(unix_us, 1000000);
  const int64_t frac = unix_us - secs * 1000000;
  const int64_t days = floor_div(secs, 86400);
  const int64_t sod = secs - days * 86400;
  int y; unsigned m, d;
  civil_from_days(days, y, m, d);
  char buf[64];
  std::snprintf(buf, sizeof buf, "%04d-%02u-%02uT%02d:%02d:%02d", y, m, d, int(sod / 3600),
                int((sod % 3600) / 60), int(sod % 60));
  std::string out = buf;
  if (frac != 0) {
    char fb[16];
    std::snprintf(fb, sizeof fb, ".%06lld", static_cast<long long>(frac));
    std::string f = fb;
    while (!f.empty() && f.back() == '0') f.pop_back();
    out += f;
  }
  return out + "Z";
}

}  // namespace neurosox
