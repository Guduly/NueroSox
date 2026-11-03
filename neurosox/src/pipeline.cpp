#include "neurosox/pipeline.hpp"

#include <algorithm>
#include <cmath>

namespace neurosox {

Pipeline::Pipeline(const Params& p, Sink sink) : p_(p), sink_(std::move(sink)), ecg_(p), ppg_(p) {
  cfg_hist_.emplace_back(0, StreamConfig{});
}

void Pipeline::push_bytes(const uint8_t* data, size_t n) {
  for (const Frame& f : decoder_.push(data, n)) push_frame(f);
  stats_.decoder = decoder_.stats();
}

void Pipeline::push_frame(const Frame& f) {
  if (f.type == FrameType::Config) {
    ++stats_.config_frames;
    ingest_config(f);
  } else {
    ++stats_.data_frames;
    ingest_data(f);
    try_emit(false);
  }
}

void Pipeline::finish() {
  stats_.decoder = decoder_.stats();
  try_emit(true);
}

void Pipeline::ingest_config(const Frame& f) {
  // Effective time: the frame timestamp, or "now" (latest data seen) when it is zero.
  int64_t eff = int64_t(f.acq_timestamp_us);
  if (eff == 0 && have_t0_) {
    double latest = 0.0;
    for (const Chan& c : ch_)
      if (c.seen) latest = std::max(latest, c.end_time());
    eff = t0_us_ + int64_t(std::llround(latest * 1e6));
  }
  StreamConfig* target;
  if (eff <= cfg_hist_.back().first) {
    target = &cfg_hist_.back().second;
  } else {
    cfg_hist_.emplace_back(eff, cfg_hist_.back().second);
    target = &cfg_hist_.back().second;
  }
  size_t a = 0;
  while (a < f.text.size()) {
    size_t b = f.text.find('\n', a);
    if (b == std::string::npos) b = f.text.size();
    const std::string line = f.text.substr(a, b - a);
    const size_t eq = line.find('=');
    if (eq != std::string::npos) target->apply(line.substr(0, eq), line.substr(eq + 1));
    a = b + 1;
  }
}

void Pipeline::ingest_data(const Frame& f) {
  const int ci = f.channel;
  if (ci < 0 || ci >= kNumChannels || f.samples.empty() || f.sample_rate_hz == 0) return;
  Chan& c = ch_[ci];

  if (!have_t0_) {
    have_t0_ = true;
    t0_us_ = int64_t(f.acq_timestamp_us);
  }
  if (!c.seen) {
    c.seen = true;
    c.fs = f.sample_rate_hz;
    c.c0 = c.next_counter = f.start_counter;
    c.t0_s = double(int64_t(f.acq_timestamp_us) - t0_us_) / 1e6;
    c.base_idx = 0;
  }
  if (f.sample_rate_hz != c.fs) {
    ++stats_.rate_mismatch_frames;
    return;
  }
  if (f.start_counter < c.next_counter) {
    ++stats_.duplicate_or_reordered_frames;
    return;
  }
  const uint64_t gap = f.start_counter - c.next_counter;
  if (double(gap) > p_.max_gap_s * c.fs) {
    ++stats_.implausible_gap_frames;
    return;
  }

  // Timestamp consistency: acquisition timestamp must agree with the counter-predicted time.
  const double predicted = c.t0_s + double(int64_t(f.start_counter - c.c0)) / c.fs;
  const double actual = double(int64_t(f.acq_timestamp_us) - t0_us_) / 1e6;
  const int64_t idx0 = int64_t(f.start_counter - c.c0);
  if (std::fabs(actual - predicted) * 1000.0 > p_.ts_tolerance_ms) {
    ++stats_.timestamp_mismatch_frames;
    c.bad_ts.emplace_back(idx0, idx0 + int64_t(f.samples.size()));
  }

  // Dropped samples become explicit "missing" samples, keeping the time base intact.
  if (gap > 0) {
    c.x.insert(c.x.end(), size_t(gap), 0.0);
    c.st.insert(c.st.end(), size_t(gap), uint8_t(kStMissing));
    stats_.missing_samples_filled[ci] += gap;
  }
  uint8_t fl = 0;
  if (f.flags & kFlagLeadOff) fl |= kStLeadOff;
  if (f.flags & kFlagSaturation) fl |= kStAfeSat;
  for (int32_t s : f.samples) {
    c.x.push_back(double(s));
    c.st.push_back(fl);
  }
  c.next_counter = f.start_counter + f.samples.size();
}

const StreamConfig& Pipeline::config_at(int64_t abs_us) const {
  const StreamConfig* cur = &cfg_hist_.front().second;
  for (const auto& e : cfg_hist_)
    if (e.first <= abs_us) cur = &e.second;
  return *cur;
}

bool Pipeline::config_stable_in(int64_t ws_us, int64_t we_us) const {
  for (size_t j = 1; j < cfg_hist_.size(); ++j) {
    const int64_t t = cfg_hist_[j].first;
    if (t <= ws_us || t >= we_us) continue;
    const StreamConfig &a = cfg_hist_[j - 1].second, &b = cfg_hist_[j].second;
    if (a.activity != b.activity || a.foot_side != b.foot_side || a.subject_id != b.subject_id ||
        a.session_id != b.session_id)
      return false;
  }
  return true;
}

Segment Pipeline::build_segment(int ci, double ws_s, double we_s, const StreamConfig& cfg, bool* ts_ok) const {
  const Chan& c = ch_[ci];
  Segment s;
  *ts_ok = true;
  if (!c.seen) return s;
  const double fs = c.fs;
  s.fs = fs;
  s.invert = (ci == int(Channel::PPG)) && cfg.ppg_invert;

  const auto idx_of = [&](double t) { return int64_t(std::ceil((t - c.t0_s) * fs - 1e-6)); };
  const int64_t i0 = idx_of(ws_s - p_.context_lead_s), i1 = idx_of(we_s + p_.context_look_s);
  const int64_t iw0 = idx_of(ws_s), iw1 = idx_of(we_s);
  const int64_t have_end = c.base_idx + int64_t(c.x.size());

  s.t0_s = c.t0_s + double(i0) / fs;
  s.x.assign(size_t(i1 - i0), 0.0);
  s.st.assign(size_t(i1 - i0), uint8_t(kStMissing));
  for (int64_t i = i0; i < i1; ++i) {
    if (i < std::max<int64_t>(0, c.base_idx) || i >= have_end) continue;
    s.x[size_t(i - i0)] = c.x[size_t(i - c.base_idx)];
    s.st[size_t(i - i0)] = c.st[size_t(i - c.base_idx)];
  }
  s.w0 = size_t(std::max<int64_t>(0, iw0 - i0));
  s.w1 = size_t(std::max<int64_t>(0, iw1 - i0));
  s.present = c.start_time() < we_s && c.end_time() > ws_s;
  for (const auto& r : c.bad_ts)
    if (r.first < iw1 && r.second > iw0) *ts_ok = false;
  return s;
}

void Pipeline::try_emit(bool flush) {
  if (!have_t0_) return;
  for (;;) {
    const double W = p_.window_s;
    const double ws = double(next_window_) * W, we = ws + W;

    bool any = false, all_ready = true;
    double max_end = -1e300;
    for (const Chan& c : ch_) {
      if (!c.seen) continue;
      any = true;
      max_end = std::max(max_end, c.end_time());
      if (c.end_time() < we + p_.context_look_s - 1e-9) all_ready = false;
    }
    if (!any) return;
    bool ready;
    if (flush) {
      ready = max_end >= we - 1e-9;  // at least one channel fully covers the window
    } else {
      ready = all_ready || max_end >= we + p_.context_look_s + p_.stall_s;
    }
    if (!ready) return;

    const int64_t ws_us = t0_us_ + int64_t(std::llround(ws * 1e6));
    const int64_t we_us = t0_us_ + int64_t(std::llround(we * 1e6));
    const StreamConfig& cfg = config_at(ws_us);

    FeatureRecord rec;
    rec.window_index = next_window_;
    rec.window_start_us = ws_us;
    rec.window_duration_s = W;
    rec.config = cfg;
    rec.ecg_fs = ch_[0].seen ? ch_[0].fs : 0;
    rec.ppg_fs = ch_[1].seen ? ch_[1].fs : 0;
    rec.activity_stable = config_stable_in(ws_us, we_us);

    bool ts_e = true, ts_p = true;
    Segment se = build_segment(0, ws, we, cfg, &ts_e);
    Segment sp = build_segment(1, ws, we, cfg, &ts_p);
    rec.timestamps_valid = ts_e && ts_p;
    if (se.present) screen_segment(se, p_, cfg.adc_min(), cfg.adc_max());
    if (sp.present) screen_segment(sp, p_, cfg.adc_min(), cfg.adc_max());

    // ECG is always processed first so its beat state advances even when the PPG channel is absent.
    rec.ecg = ecg_.process(se, ws, we);
    rec.ppg = ppg_.process(sp, ws, we, &rec.ecg, rec.timestamps_valid);

    sink_(rec);
    ++stats_.windows_emitted;
    ++next_window_;

    // Release samples that no later window can need.
    const double keep_from = we - p_.context_lead_s;
    for (Chan& c : ch_) {
      if (!c.seen) continue;
      const int64_t drop_to = int64_t(std::floor((keep_from - c.t0_s) * c.fs)) - 2;
      if (drop_to > c.base_idx) {
        const size_t n = size_t(std::min<int64_t>(drop_to - c.base_idx, int64_t(c.x.size())));
        c.x.erase(c.x.begin(), c.x.begin() + std::ptrdiff_t(n));
        c.st.erase(c.st.begin(), c.st.begin() + std::ptrdiff_t(n));
        c.base_idx += int64_t(n);
        c.bad_ts.erase(std::remove_if(c.bad_ts.begin(), c.bad_ts.end(),
                                      [&](const std::pair<int64_t, int64_t>& r) { return r.second <= c.base_idx; }),
                       c.bad_ts.end());
      }
    }
  }
}

}  // namespace neurosox
