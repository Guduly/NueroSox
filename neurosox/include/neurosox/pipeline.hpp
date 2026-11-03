// Streaming pipeline: bytes -> frames -> per-channel sample buffers -> 30 s windows -> feature records.
#pragma once

#include <cstdint>
#include <functional>
#include <vector>

#include "neurosox/common.hpp"
#include "neurosox/ecg.hpp"
#include "neurosox/features.hpp"
#include "neurosox/frame.hpp"
#include "neurosox/ppg.hpp"

namespace neurosox {

struct PipelineStats {
  DecoderStats decoder;
  uint64_t data_frames = 0, config_frames = 0;
  uint64_t duplicate_or_reordered_frames = 0;  // counter at/before the expected next sample
  uint64_t rate_mismatch_frames = 0;           // sample rate differs from the channel's first frame
  uint64_t implausible_gap_frames = 0;         // counter jump larger than max_gap_s
  uint64_t timestamp_mismatch_frames = 0;
  uint64_t missing_samples_filled[kNumChannels] = {0, 0};
  uint64_t windows_emitted = 0;
};

class Pipeline {
 public:
  using Sink = std::function<void(const FeatureRecord&)>;
  Pipeline(const Params& p, Sink sink);

  void push_bytes(const uint8_t* data, size_t n);
  void push_frame(const Frame& f);
  // End of stream: emits every window that is completely covered by received data.
  void finish();

  const PipelineStats& stats() const { return stats_; }

 private:
  struct Chan {
    bool seen = false;
    int fs = 0;
    uint64_t c0 = 0, next_counter = 0;
    double t0_s = 0.0;                     // time of sample counter c0 relative to the stream reference
    int64_t base_idx = 0;                  // sample index (counter - c0) of x[0]
    std::vector<double> x;
    std::vector<uint8_t> st;
    std::vector<std::pair<int64_t, int64_t>> bad_ts;  // [a,b) sample ranges with inconsistent timestamps
    double start_time() const { return t0_s; }
    double end_time() const { return t0_s + double(int64_t(next_counter - c0)) / fs; }
  };

  void ingest_data(const Frame& f);
  void ingest_config(const Frame& f);
  void try_emit(bool flush);
  Segment build_segment(int ch, double ws_s, double we_s, const StreamConfig& cfg, bool* ts_ok) const;
  const StreamConfig& config_at(int64_t abs_us) const;
  bool config_stable_in(int64_t ws_us, int64_t we_us) const;

  Params p_;
  Sink sink_;
  StreamDecoder decoder_;
  PipelineStats stats_;
  EcgProcessor ecg_;
  PpgProcessor ppg_;

  bool have_t0_ = false;
  int64_t t0_us_ = 0;
  Chan ch_[kNumChannels];
  int64_t next_window_ = 0;
  std::vector<std::pair<int64_t, StreamConfig>> cfg_hist_;
};

}  // namespace neurosox
