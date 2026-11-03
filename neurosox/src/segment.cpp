#include "neurosox/segment.hpp"

#include <algorithm>
#include <cmath>

namespace neurosox {

void screen_segment(Segment& s, const Params& p, int64_t adc_min, int64_t adc_max) {
  const size_t n = s.size();
  for (size_t i = 0; i < n; ++i) {
    if (s.st[i] & kStMissing) continue;
    const double v = s.x[i];
    if (v <= double(adc_min) || v >= double(adc_max)) s.st[i] |= kStClipped;
  }
  const size_t min_run = std::max<size_t>(3, size_t(std::lround(p.flat_run_s * s.fs)));
  size_t i = 0;
  while (i < n) {
    if (s.st[i] & kStMissing) { ++i; continue; }
    size_t j = i + 1;
    while (j < n && !(s.st[j] & kStMissing) && s.x[j] == s.x[i]) ++j;
    if (j - i >= min_run)
      for (size_t k = i; k < j; ++k) s.st[k] |= kStFlat;
    i = j;
  }
}

std::vector<double> prepare_signal(const Segment& s, bool allow_invert, double* dc_out) {
  const size_t n = s.size();
  std::vector<double> y(n, 0.0);
  const double sign = (allow_invert && s.invert) ? -1.0 : 1.0;

  long first = -1, last = -1;
  double sum = 0.0;
  size_t cnt = 0;
  for (size_t i = 0; i < n; ++i) {
    if (!s.ok(i)) continue;
    if (first < 0) first = long(i);
    last = long(i);
    sum += s.x[i];
    ++cnt;
  }
  const double mean = cnt ? sum / double(cnt) : 0.0;
  if (dc_out) *dc_out = mean;
  if (cnt == 0) return y;

  size_t prev_valid = size_t(first);
  y[prev_valid] = s.x[prev_valid];
  for (size_t i = 0; i < n; ++i) {
    if (s.ok(i)) {
      y[i] = s.x[i];
      continue;
    }
    if (long(i) < first) { y[i] = s.x[size_t(first)]; continue; }
    if (long(i) > last) { y[i] = s.x[size_t(last)]; continue; }
  }
  // Interior gaps: linear interpolation between neighbouring valid samples.
  size_t i = size_t(first);
  while (i <= size_t(last)) {
    if (s.ok(i)) { prev_valid = i; ++i; continue; }
    size_t j = i;
    while (j <= size_t(last) && !s.ok(j)) ++j;  // j is next valid (exists because i <= last)
    const double a = s.x[prev_valid], b = s.x[j];
    for (size_t k = i; k < j; ++k) y[k] = a + (b - a) * double(k - prev_valid) / double(j - prev_valid);
    i = j;
  }
  for (double& v : y) v = sign * (v - mean);
  return y;
}

BadIndex::BadIndex(const Segment& s) : cum_(s.size() + 1, 0) {
  for (size_t i = 0; i < s.size(); ++i) cum_[i + 1] = cum_[i] + (s.ok(i) ? 0u : 1u);
}

bool BadIndex::any_bad(long a, long b) const {
  const long n = long(cum_.size()) - 1;
  if (n <= 0) return true;
  a = std::max(0L, a);
  b = std::min(n - 1, b);
  if (b < a) return false;
  return cum_[size_t(b) + 1] - cum_[size_t(a)] > 0;
}

size_t BadIndex::count_bad(size_t a, size_t b) const {
  const size_t n = cum_.size() - 1;
  a = std::min(a, n);
  b = std::min(b, n);
  if (b <= a) return 0;
  return cum_[b] - cum_[a];
}

}  // namespace neurosox
