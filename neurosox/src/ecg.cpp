#include "neurosox/ecg.hpp"

#include <algorithm>
#include <cmath>
#include <numeric>

#include "neurosox/filters.hpp"

namespace neurosox {

namespace {

// Local maxima of v, then keep the highest peaks that are at least `dist` samples apart.
std::vector<size_t> peaks_with_distance(const std::vector<double>& v, size_t dist, double min_height) {
  std::vector<size_t> cand;
  for (size_t i = 1; i + 1 < v.size(); ++i)
    if (v[i] > v[i - 1] && v[i] >= v[i + 1] && v[i] > min_height) cand.push_back(i);
  std::vector<size_t> order(cand.size());
  std::iota(order.begin(), order.end(), size_t(0));
  std::sort(order.begin(), order.end(), [&](size_t a, size_t b) { return v[cand[a]] > v[cand[b]]; });
  std::vector<uint8_t> keep(cand.size(), 1);
  for (size_t oi : order) {
    if (!keep[oi]) continue;
    for (long j = long(oi) - 1; j >= 0 && cand[oi] - cand[size_t(j)] < dist; --j) keep[size_t(j)] = 0;
    for (size_t j = oi + 1; j < cand.size() && cand[j] - cand[oi] < dist; ++j) keep[j] = 0;
  }
  std::vector<size_t> out;
  for (size_t i = 0; i < cand.size(); ++i)
    if (keep[i]) out.push_back(cand[i]);
  return out;
}

double max_abs_in(const std::vector<double>& v, size_t a, size_t b) {  // inclusive, clamped
  if (v.empty()) return 0.0;
  b = std::min(b, v.size() - 1);
  double m = 0.0;
  for (size_t i = a; i <= b; ++i) m = std::max(m, std::fabs(v[i]));
  return m;
}

}  // namespace

EcgDetection detect_qrs(const Segment& seg, const Params& p) {
  EcgDetection out;
  const size_t n = seg.size();
  const double fs = seg.fs;
  if (fs <= 0 || n < size_t(4 * fs)) return out;

  const std::vector<double> xs = prepare_signal(seg, false);
  const size_t pad = size_t(std::lround(1.5 * fs));
  out.filt = sosfiltfilt(butter_bandpass(p.ecg_lo_hz, p.ecg_hi_hz, fs, 2, 4), xs, pad);
  const std::vector<double> q = sosfiltfilt(butter_bandpass(p.qrs_lo_hz, p.qrs_hi_hz, fs, 2, 2), xs, pad);

  // Five-point derivative (zero phase), squaring, moving-window integration.
  std::vector<double> d(n, 0.0);
  for (size_t i = 2; i + 2 < n; ++i) d[i] = (-q[i + 2] - 2 * q[i + 1] + 2 * q[i - 1] + q[i - 2]) * fs / 8.0;
  std::vector<double> sq(n);
  for (size_t i = 0; i < n; ++i) sq[i] = d[i] * d[i];
  size_t L = size_t(std::lround(p.mwi_s * fs));
  if (L % 2 == 0) ++L;
  std::vector<double> cs(n + 1, 0.0), mwi(n, 0.0);
  for (size_t i = 0; i < n; ++i) cs[i + 1] = cs[i] + sq[i];
  const size_t half = L / 2;
  for (size_t i = 0; i < n; ++i) {
    const size_t a = i > half ? i - half : 0, b = std::min(n, i + half + 1);
    mwi[i] = (cs[b] - cs[a]) / double(L);
  }

  const size_t refr = size_t(std::lround(p.refractory_s * fs));
  const std::vector<size_t> cand = peaks_with_distance(mwi, refr, 0.0);
  if (cand.empty()) return out;

  // Robust threshold initialisation: per-second maxima (signal level) and a low quantile (noise level).
  std::vector<double> sec_max;
  const size_t sec = size_t(std::lround(fs));
  for (size_t a = 0; a + sec <= n; a += sec) sec_max.push_back(*std::max_element(mwi.begin() + long(a), mwi.begin() + long(a + sec)));
  double spki = median_of(sec_max);
  double npki = quantile_of(mwi, 0.25);
  if (spki <= npki) spki = npki * 2.0 + 1e-12;

  const size_t slope_half = size_t(std::lround(0.075 * fs));
  auto slope_at = [&](size_t i) { return max_abs_in(d, i > slope_half ? i - slope_half : 0, i + slope_half); };

  std::vector<size_t> qrs;
  std::vector<double> qrs_slope;
  std::vector<size_t> rej;
  auto accept = [&](size_t idx, double weight) {
    qrs.push_back(idx);
    qrs_slope.push_back(slope_at(idx));
    spki = weight * mwi[idx] + (1.0 - weight) * spki;
    rej.erase(std::remove_if(rej.begin(), rej.end(), [&](size_t r) { return r <= idx; }), rej.end());
  };

  for (size_t pk : cand) {
    double thr1 = npki + 0.25 * (spki - npki);
    // Search-back when an interval looks too long compared with the recent average.
    if (qrs.size() >= 3) {
      const size_t m = std::min<size_t>(8, qrs.size() - 1);
      const double rr_avg = double(qrs.back() - qrs[qrs.size() - 1 - m]) / double(m);
      if (double(pk - qrs.back()) > 1.66 * rr_avg) {
        const double thr2 = 0.5 * thr1;
        long best = -1;
        for (size_t r : rej)
          if (r > qrs.back() + refr && r + refr < pk && mwi[r] > thr2 && (best < 0 || mwi[r] > mwi[size_t(best)])) best = long(r);
        if (best >= 0) accept(size_t(best), 0.25);
      }
    }
    thr1 = npki + 0.25 * (spki - npki);
    const double v = mwi[pk];
    if (v > thr1) {
      const bool t_wave = !qrs.empty() && (pk - qrs.back()) < size_t(std::lround(0.36 * fs)) && slope_at(pk) < 0.5 * qrs_slope.back();
      if (t_wave) {
        npki = 0.125 * v + 0.875 * npki;
      } else {
        accept(pk, 0.125);
      }
    } else {
      npki = 0.125 * v + 0.875 * npki;
      rej.push_back(pk);
    }
  }

  // Refine each detection to the |filtered ECG| maximum around the integrator peak.
  const size_t search = size_t(std::lround(0.075 * fs));
  std::vector<size_t> refined;
  for (size_t pk : qrs) {
    const size_t a = pk > search ? pk - search : 0, b = std::min(n - 1, pk + search);
    size_t best = a;
    for (size_t i = a; i <= b; ++i)
      if (std::fabs(out.filt[i]) > std::fabs(out.filt[best])) best = i;
    refined.push_back(best);
  }
  std::sort(refined.begin(), refined.end());
  std::vector<size_t> r;
  for (size_t i : refined) {
    if (!r.empty() && i - r.back() < refr) {
      if (std::fabs(out.filt[i]) > std::fabs(out.filt[r.back()])) r.back() = i;
    } else {
      r.push_back(i);
    }
  }
  out.r_idx = r;

  const BadIndex bad(seg);
  const long guard = long(std::lround(0.1 * fs));
  for (size_t i : out.r_idx) out.r_clean.push_back(bad.any_bad(long(i) - guard, long(i) + guard) ? 0 : 1);
  return out;
}

EcgFeatures EcgProcessor::process(const Segment& seg, double ws_s, double we_s) {
  EcgFeatures f;
  if (!have_first_ws_) {
    have_first_ws_ = true;
    first_ws_ = ws_s;
  }
  f.has_data = seg.present && seg.size() > 0;
  if (!f.has_data) return f;

  const BadIndex bad(seg);
  const size_t nwin = seg.w1 > seg.w0 ? seg.w1 - seg.w0 : 0;
  for (size_t i = seg.w0; i < seg.w1; ++i) {
    const uint8_t s = seg.st[i];
    if (s & kStMissing) ++f.missing;
    else if (s & (kStLeadOff | kStAfeSat)) ++f.flagged;
    else if (s & kStClipped) ++f.clipped;
    else if (s & kStFlat) ++f.flat;
  }
  const size_t bad_in_window = bad.count_bad(seg.w0, seg.w1);
  f.usable_pct = nwin ? 100.0 * double(nwin - bad_in_window) / double(nwin) : 0.0;

  const EcgDetection det = detect_qrs(seg, p_);

  // Retain beats that belong to this window (exactly-once across overlapping passes).
  std::vector<size_t> kept;
  const double lower = has_prev_ ? std::max(prev_t_ + p_.refractory_s, ws_s - 0.5) : ws_s;
  for (size_t k = 0; k < det.r_idx.size(); ++k) {
    const double t = seg.time(double(det.r_idx[k]));
    const bool lo_ok = has_prev_ ? (t > lower) : (t >= lower);
    if (lo_ok && t < we_s) kept.push_back(k);
  }
  f.n_beats = int(kept.size());

  // Intervals: each interval is attributed to the window containing its ending beat.
  std::vector<Interval> ivs;
  std::vector<uint8_t> plausible_clean;
  double tprev = prev_t_;
  bool cprev = prev_clean_, have_prev = has_prev_;
  for (size_t k : kept) {
    const double t = seg.time(double(det.r_idx[k]));
    const bool clean = det.r_clean[k] != 0;
    f.beat_t_s.push_back(t);
    f.beat_clean.push_back(clean ? 1 : 0);
    f.r_peaks_ms.push_back((t - ws_s) * 1000.0);
    if (have_prev) {
      const double rr = t - tprev;
      const bool pc = rr >= p_.rr_min_s && rr <= p_.rr_max_s && clean && cprev;
      ivs.push_back({t, rr * 1000.0, false});
      plausible_clean.push_back(pc ? 1 : 0);
    }
    tprev = t;
    cprev = clean;
    have_prev = true;
  }
  // Ectopic / irregular rejection against the local median of plausible, clean intervals.
  for (size_t j = 0; j < ivs.size(); ++j) {
    if (!plausible_clean[j]) continue;
    std::vector<double> nb;
    const size_t a = j >= 5 ? j - 5 : 0, b = std::min(ivs.size(), j + 6);
    for (size_t k = a; k < b; ++k)
      if (plausible_clean[k]) nb.push_back(ivs[k].rr_ms);
    bool nn = true;
    if (nb.size() >= 3) {
      const double ref = median_of(nb);
      nn = std::fabs(ivs[j].rr_ms - ref) <= p_.rr_tol * ref;
    }
    ivs[j].nn = nn;
  }
  for (const Interval& iv : ivs) {
    f.rr_intervals_ms.push_back(iv.rr_ms);
    if (iv.nn) f.nn_intervals_ms.push_back(iv.rr_ms);
  }
  if (!ivs.empty()) f.valid_beats_pct = 100.0 * double(f.nn_intervals_ms.size()) / double(ivs.size());
  if (f.nn_intervals_ms.size() >= 3) f.heart_rate_bpm = 60000.0 / mean_of(f.nn_intervals_ms);

  // QRS template agreement of clean beats = morphology/noise quality.
  double corr = 0.0;
  {
    const size_t hw = size_t(std::lround(0.1 * seg.fs));
    std::vector<std::vector<double>> beats;
    for (size_t k : kept) {
      const size_t r = det.r_idx[k];
      if (!det.r_clean[k] || r < hw || r + hw >= det.filt.size()) continue;
      beats.emplace_back(det.filt.begin() + long(r - hw), det.filt.begin() + long(r + hw + 1));
    }
    if (beats.size() >= 3) {
      std::vector<double> tmpl(2 * hw + 1);
      for (size_t i = 0; i < tmpl.size(); ++i) {
        std::vector<double> col;
        for (const auto& b : beats) col.push_back(b[i]);
        tmpl[i] = median_of(col);
      }
      double acc = 0.0;
      for (const auto& b : beats) acc += std::max(0.0, pearson(b, tmpl));
      corr = acc / double(beats.size());
    }
  }
  f.signal_quality = (f.usable_pct / 100.0) * corr;
  f.valid = f.usable_pct / 100.0 >= p_.ecg_min_usable && f.signal_quality >= p_.min_quality &&
            int(f.nn_intervals_ms.size()) >= p_.min_beats;

  // Rolling store for short-term HRV. Windows that fail validity never contribute NN intervals.
  for (const Interval& iv : ivs) rolling_.push_back({iv.t_end_s, iv.rr_ms, iv.nn && f.valid});
  while (!rolling_.empty() && rolling_.front().t_end_s <= we_s - p_.hrv_window_s) rolling_.pop_front();

  std::vector<double> nn_all, diffs;
  for (size_t i = 0; i < rolling_.size(); ++i) {
    if (!rolling_[i].nn) continue;
    nn_all.push_back(rolling_[i].rr_ms);
    if (i > 0 && rolling_[i - 1].nn) diffs.push_back(rolling_[i].rr_ms - rolling_[i - 1].rr_ms);
  }
  f.hrv.n_nn = int(nn_all.size());
  double nn_time_ms = 0.0;
  for (double v : nn_all) nn_time_ms += v;
  f.hrv.coverage_pct = std::min(100.0, 100.0 * nn_time_ms / (p_.hrv_window_s * 1000.0));
  const bool span_ok = (we_s - first_ws_) >= p_.hrv_window_s - 1e-6;
  if (f.valid && span_ok && f.hrv.coverage_pct >= 100.0 * p_.hrv_min_coverage &&
      f.hrv.n_nn >= p_.hrv_min_nn && diffs.size() >= 20) {
    f.hrv.sdnn_ms = stdev_sample(nn_all);
    double acc = 0.0;
    for (double dv : diffs) acc += dv * dv;
    f.hrv.rmssd_ms = std::sqrt(acc / double(diffs.size()));
  }

  if (!kept.empty()) {
    const size_t k = kept.back();
    has_prev_ = true;
    prev_t_ = seg.time(double(det.r_idx[k]));
    prev_clean_ = det.r_clean[k] != 0;
  }
  return f;
}

}  // namespace neurosox
