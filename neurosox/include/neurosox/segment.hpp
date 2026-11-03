// A contiguous stretch of one channel (window plus lead/look-ahead context) handed to the DSP stages.
#pragma once

#include <cstdint>
#include <vector>

#include "neurosox/common.hpp"

namespace neurosox {

struct Segment {
  double fs = 0.0;
  double t0_s = 0.0;          // time of x[0], seconds relative to the stream reference
  std::vector<double> x;      // raw ADC counts (never polarity-corrected)
  std::vector<uint8_t> st;    // SampleStatus bits per sample
  size_t w0 = 0, w1 = 0;      // [w0, w1) is the analysis window inside the segment
  bool invert = false;        // PPG polarity: process -x
  bool present = false;       // channel delivered any data

  size_t size() const { return x.size(); }
  double time(double i) const { return t0_s + i / fs; }
  bool ok(size_t i) const { return st[i] == 0; }
};

// Mark clipped (at ADC code limits) and flat-line samples in-place.
void screen_segment(Segment& s, const Params& p, int64_t adc_min, int64_t adc_max);

// Replace invalid samples by linear interpolation (edges hold the nearest valid value),
// optionally negate, and subtract the mean of the valid samples. `dc_out` receives that mean.
std::vector<double> prepare_signal(const Segment& s, bool allow_invert, double* dc_out = nullptr);

// Prefix counts of invalid samples for O(1) "any invalid in [a,b]" queries.
class BadIndex {
 public:
  explicit BadIndex(const Segment& s);
  bool any_bad(long a, long b) const;  // inclusive, indices clamped to the segment
  size_t count_bad(size_t a, size_t b) const;  // half-open
 private:
  std::vector<uint32_t> cum_;
};

}  // namespace neurosox
