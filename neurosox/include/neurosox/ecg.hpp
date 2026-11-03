// ECG: QRS detection (Pan-Tompkins style) and per-window feature extraction.
#pragma once

#include <deque>
#include <vector>

#include "neurosox/common.hpp"
#include "neurosox/features.hpp"
#include "neurosox/segment.hpp"

namespace neurosox {

struct EcgDetection {
  std::vector<double> filt;       // 0.5-40 Hz zero-phase signal (mean removed)
  std::vector<size_t> r_idx;      // R-peak sample indices (time sorted)
  std::vector<uint8_t> r_clean;   // no invalid samples around the beat
};

// Detect R peaks in a whole segment. Polarity independent (uses |filtered ECG|).
EcgDetection detect_qrs(const Segment& seg, const Params& p);

class EcgProcessor {
 public:
  explicit EcgProcessor(const Params& p) : p_(p) {}
  // ws_s / we_s: window bounds in seconds relative to the stream reference.
  EcgFeatures process(const Segment& seg, double ws_s, double we_s);

 private:
  struct Interval {
    double t_end_s;
    double rr_ms;
    bool nn;
  };
  Params p_;
  bool has_prev_ = false;
  double prev_t_ = 0.0;
  bool prev_clean_ = false;
  bool have_first_ws_ = false;
  double first_ws_ = 0.0;
  std::deque<Interval> rolling_;
};

}  // namespace neurosox
