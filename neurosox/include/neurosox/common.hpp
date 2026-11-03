// NeuroSox data pipeline - shared types, tunable parameters and small helpers.
#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace neurosox {

constexpr const char* kSchema = "neurosox.features.v1";

enum class Channel : uint8_t { ECG = 0, PPG = 1 };
constexpr int kNumChannels = 2;
const char* channel_name(Channel c);  // "ECG_1" / "PPG_1"

// Per-sample status bits. A sample is usable only when its status is 0.
enum SampleStatus : uint8_t {
  kStMissing = 1,   // counter gap / dropped or corrupted frame / outside stored data
  kStLeadOff = 2,   // frame flagged lead-off (ECG) / no optical contact (PPG)
  kStAfeSat = 4,    // frame flagged AFE saturation
  kStClipped = 8,   // sample at ADC code limit
  kStFlat = 16      // stuck / flat-line run
};

// All thresholds in one place. Values are prototype starting points, NOT clinical limits.
struct Params {
  // windowing / timing
  double window_s = 30.0;
  double context_lead_s = 2.0;    // history kept ahead of a window so filters/detectors see context
  double context_look_s = 2.0;    // look-ahead past the window end
  double stall_s = 5.0;           // emit a window even if one channel stalls this long
  double max_gap_s = 3600.0;      // larger counter jumps are rejected as implausible
  double ts_tolerance_ms = 10.0;  // frame timestamp vs counter-predicted time
  double flat_run_s = 0.4;        // identical-sample run treated as a stuck signal

  // ECG
  double ecg_lo_hz = 0.5, ecg_hi_hz = 40.0;
  double qrs_lo_hz = 5.0, qrs_hi_hz = 15.0;  // Pan-Tompkins detection band
  double mwi_s = 0.15;                       // moving-window integration width
  double refractory_s = 0.2;
  double rr_min_s = 0.3, rr_max_s = 2.0;     // physiologic plausibility (30..200 bpm)
  double rr_tol = 0.20;                      // |RR - local median| / median limit
  double hrv_window_s = 300.0;
  double hrv_min_coverage = 0.8;             // NN time / window time
  int hrv_min_nn = 30;

  // PPG
  double ppg_lo_hz = 0.5, ppg_hi_hz = 8.0;
  double ppg_min_interval_s = 0.3;
  double ppg_interval_tol = 0.25;
  double pat_min_s = 0.05, pat_max_s = 0.7;

  // window validity
  double ecg_min_usable = 0.8;
  double ppg_min_usable = 0.7;
  double min_quality = 0.5;
  int min_beats = 5;
};

// Acquisition context that arrives in CONFIG frames (key=value text).
struct StreamConfig {
  std::string subject_id = "UNKNOWN";
  std::string session_id = "UNKNOWN";
  std::string foot_side = "unknown";
  std::string activity = "unknown";
  int adc_bits = 22;
  bool adc_signed = false;
  bool ppg_invert = false;                     // optical front end delivers inverted pulses
  std::map<std::string, std::string> extra;    // other AFE settings, echoed into output

  void apply(const std::string& key, const std::string& value);
  int64_t adc_min() const;
  int64_t adc_max() const;
};

// ---- small numeric helpers (all tolerate empty input) ----
double mean_of(const std::vector<double>& v);
double median_of(std::vector<double> v);
double quantile_of(std::vector<double> v, double q);  // linear interpolation, q in [0,1]
double stdev_sample(const std::vector<double>& v);    // ddof = 1
double pearson(const std::vector<double>& a, const std::vector<double>& b);

// Days-from-civil based UTC formatting: "2026-10-31T20:00:00Z" (fraction only if non-zero).
std::string format_utc(int64_t unix_us);

// Integer ceil division for possibly-negative numerators (den > 0).
int64_t ceil_div(int64_t num, int64_t den);
int64_t floor_div(int64_t num, int64_t den);

}  // namespace neurosox
