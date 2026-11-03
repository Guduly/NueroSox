// PPG: pulse detection (max-upslope + tangent-intersection foot) and per-window features.
#pragma once

#include <vector>

#include "neurosox/common.hpp"
#include "neurosox/features.hpp"
#include "neurosox/segment.hpp"

namespace neurosox {

struct PpgPulse {
  double foot_i = 0;      // tangent-intersection foot, fractional sample index in segment
  size_t trough_i = 0;    // pre-upstroke minimum (band-passed signal)
  size_t peak_i = 0;      // systolic peak
  double amp = 0;         // foot-to-peak amplitude on the 8 Hz smoothed raw signal (counts)
  double rise_ms = 0;     // foot -> peak
  double width_ms = -1;   // full width at half amplitude (-1 when not measurable)
  bool clean = false;     // no invalid samples around the pulse
};

struct PpgDetection {
  std::vector<double> bp;   // 0.5-8 Hz zero-phase, polarity corrected
  std::vector<PpgPulse> pulses;
};

PpgDetection detect_pulses(const Segment& seg, const Params& p);

class PpgProcessor {
 public:
  explicit PpgProcessor(const Params& p) : p_(p) {}
  // `ecg` may be null; PAT needs a valid ECG window, a valid PPG window and consistent timestamps.
  PpgFeatures process(const Segment& seg, double ws_s, double we_s, const EcgFeatures* ecg, bool timestamps_valid);

 private:
  Params p_;
  bool has_prev_ = false;
  double prev_foot_t_ = 0.0;
  bool prev_valid_ = false;
};

}  // namespace neurosox
