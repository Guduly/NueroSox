// Feature records produced per analysis window (schema neurosox.features.v1).
#pragma once

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include "neurosox/common.hpp"

namespace neurosox {

struct HrvFeatures {
  std::optional<double> sdnn_ms, rmssd_ms;
  int n_nn = 0;              // NN intervals in the rolling window
  double coverage_pct = 0;   // NN time / rolling-window time
};

struct EcgFeatures {
  bool has_data = false;
  bool valid = false;
  std::optional<double> heart_rate_bpm;
  std::vector<double> rr_intervals_ms;  // every interval whose ending beat is in this window
  std::vector<double> nn_intervals_ms;  // the subset classified normal-to-normal
  std::vector<double> r_peaks_ms;       // R-peak times, ms from window start
  std::optional<double> valid_beats_pct;
  HrvFeatures hrv;
  double signal_quality = 0.0;          // 0..1
  double usable_pct = 0.0;              // % of window samples that passed screening
  int n_beats = 0;
  // sample screening counts inside the window
  int missing = 0, flagged = 0, clipped = 0, flat = 0;
  // internal, for ECG/PPG alignment (seconds relative to stream reference)
  std::vector<double> beat_t_s;
  std::vector<uint8_t> beat_clean;
};

struct PpgFeatures {
  bool has_data = false;
  bool valid = false;
  std::optional<double> pulse_rate_bpm;
  std::vector<double> pulse_foot_ms;        // valid pulse feet, ms from window start
  std::vector<double> pulse_intervals_ms;   // valid foot-to-foot intervals
  std::vector<double> pulse_amplitudes;     // ADC counts, foot-to-peak
  std::vector<double> pulse_rise_times_ms;  // foot -> systolic peak
  std::optional<double> amplitude_median;   // "AC" level, ADC counts
  std::optional<double> dc_level;           // mean raw level, ADC counts
  std::optional<double> perfusion_index_pct;
  std::optional<double> rise_time_median_ms;
  std::optional<double> width_median_ms;    // width at half amplitude
  std::vector<double> pat_ms;               // per matched beat
  std::optional<double> pat_median_ms;
  double signal_quality = 0.0;
  double usable_pct = 0.0;
  int n_pulses = 0;        // detected in window
  int n_valid_pulses = 0;
  int missing = 0, flagged = 0, clipped = 0, flat = 0;
};

struct FeatureRecord {
  int64_t window_index = 0;
  int64_t window_start_us = 0;
  double window_duration_s = 30.0;
  StreamConfig config;
  int ecg_fs = 0, ppg_fs = 0;
  EcgFeatures ecg;
  PpgFeatures ppg;
  bool timestamps_valid = true;
  bool activity_stable = true;
};

std::string to_json(const FeatureRecord& r);

}  // namespace neurosox
