#include "neurosox/ppg.hpp"

#include <algorithm>
#include <cmath>
#include <numeric>

#include "neurosox/filters.hpp"

namespace neurosox {

PpgDetection detect_pulses(const Segment& seg, const Params& p) {
  PpgDetection out;
  const size_t n = seg.size();
  const double fs = seg.fs;
  if (fs <= 0 || n < size_t(4 * fs)) return out;

  const std::vector<double> xs = prepare_signal(seg, true);  // polarity corrected, mean removed
  const size_t pad = size_t(std::lround(1.5 * fs));
  out.bp = sosfiltfilt(butter_bandpass(p.ppg_lo_hz, p.ppg_hi_hz, fs, 2, 4), xs, pad);
  const std::vector<double> sm = sosfiltfilt(butter_lowpass(4, p.ppg_hi_hz, fs), xs, pad);
  const std::vector<double>& bp = out.bp;

  std::vector<double> d(n, 0.0);
  for (size_t i = 1; i + 1 < n; ++i) d[i] = (bp[i + 1] - bp[i - 1]) * fs / 2.0;

  // Upstroke candidates: local maxima of the slope, >= min interval apart, tallest first.
  const size_t dist = size_t(std::lround(p.ppg_min_interval_s * fs));
  std::vector<size_t> cand;
  for (size_t i = 1; i + 1 < n; ++i)
    if (d[i] > 0 && d[i] > d[i - 1] && d[i] >= d[i + 1]) cand.push_back(i);
  std::vector<size_t> order(cand.size());
  std::iota(order.begin(), order.end(), size_t(0));
  std::sort(order.begin(), order.end(), [&](size_t a, size_t b) { return d[cand[a]] > d[cand[b]]; });
  std::vector<uint8_t> keep(cand.size(), 1);
  for (size_t oi : order) {
    if (!keep[oi]) continue;
    for (long j = long(oi) - 1; j >= 0 && cand[oi] - cand[size_t(j)] < dist; --j) keep[size_t(j)] = 0;
    for (size_t j = oi + 1; j < cand.size() && cand[j] - cand[oi] < dist; ++j) keep[j] = 0;
  }
  // Scale = median of per-second maximum slope; the dicrotic upstroke is far weaker than the systolic one.
  std::vector<double> sec_max;
  const size_t sec = size_t(std::lround(fs));
  for (size_t a = 0; a + sec <= n; a += sec) sec_max.push_back(*std::max_element(d.begin() + long(a), d.begin() + long(a + sec)));
  const double thr = 0.5 * median_of(sec_max);
  std::vector<size_t> ups;
  for (size_t i = 0; i < cand.size(); ++i)
    if (keep[i] && d[cand[i]] >= thr && d[cand[i]] > 0) ups.push_back(cand[i]);
  if (ups.empty()) return out;

  struct Tmp { size_t m, trough, peak; };
  std::vector<Tmp> tmp;
  for (size_t k = 0; k < ups.size(); ++k) {
    const size_t m = ups[k];
    const double prev_gap = k > 0 ? double(m - ups[k - 1]) / fs : 1e9;
    const double next_gap = k + 1 < ups.size() ? double(ups[k + 1] - m) / fs : 1e9;
    const size_t tl = size_t(std::lround(std::min(0.4, 0.7 * prev_gap) * fs));
    const size_t pl = size_t(std::lround(std::min(0.5, 0.6 * next_gap) * fs));
    if (tl < 3 || pl < 3) continue;
    const size_t a = m > tl ? m - tl : 0;
    size_t tr = a;
    for (size_t i = a; i <= m; ++i)
      if (bp[i] < bp[tr]) tr = i;
    const size_t b = std::min(n - 1, m + pl);
    size_t pk = m;
    for (size_t i = m; i <= b; ++i)
      if (bp[i] > bp[pk]) pk = i;
    if (pk <= m || tr >= m) continue;
    tmp.push_back({m, tr, pk});
  }

  const BadIndex bad(seg);
  for (size_t k = 0; k < tmp.size(); ++k) {
    const Tmp& t = tmp[k];
    PpgPulse pu;
    pu.trough_i = t.trough;
    pu.peak_i = t.peak;
    const double slope = d[t.m] / fs;  // per sample
    double foot = double(t.m) - (bp[t.m] - bp[t.trough]) / slope;
    foot = std::min(double(t.m), std::max(double(t.trough), foot));
    pu.foot_i = foot;
    pu.rise_ms = (double(t.peak) - foot) / fs * 1000.0;

    // Baseline-corrected amplitude on the smoothed raw signal (robust to slow drift).
    double base = sm[t.trough];
    if (k + 1 < tmp.size() && tmp[k + 1].trough > t.trough) {
      const double f = double(t.peak - t.trough) / double(tmp[k + 1].trough - t.trough);
      base = sm[t.trough] + (sm[tmp[k + 1].trough] - sm[t.trough]) * f;
    }
    pu.amp = sm[t.peak] - base;

    // Full width at half amplitude on the band-passed signal.
    const double amp_bp = bp[t.peak] - bp[t.trough];
    const double level = bp[t.trough] + 0.5 * amp_bp;
    long ri = long(t.peak);
    while (ri > long(t.trough) && bp[size_t(ri)] > level) --ri;
    size_t fi = t.peak;
    const size_t lim = k + 1 < tmp.size() ? tmp[k + 1].trough : n - 1;
    while (fi < lim && bp[fi] > level) ++fi;
    if (ri >= long(t.trough) && fi < lim && amp_bp > 0) {
      const double rx = double(ri) + (level - bp[size_t(ri)]) / (bp[size_t(ri) + 1] - bp[size_t(ri)] + 1e-12);
      const double fx = double(fi - 1) + (bp[fi - 1] - level) / (bp[fi - 1] - bp[fi] + 1e-12);
      pu.width_ms = (fx - rx) / fs * 1000.0;
    }
    const long g0 = long(std::lround(0.05 * fs)), g1 = long(std::lround(0.3 * fs));
    pu.clean = !bad.any_bad(long(t.trough) - g0, long(t.peak) + g1);
    out.pulses.push_back(pu);
  }
  return out;
}

PpgFeatures PpgProcessor::process(const Segment& seg, double ws_s, double we_s, const EcgFeatures* ecg,
                                  bool timestamps_valid) {
  PpgFeatures f;
  f.has_data = seg.present && seg.size() > 0;
  if (!f.has_data) return f;

  const BadIndex bad(seg);
  const size_t nwin = seg.w1 > seg.w0 ? seg.w1 - seg.w0 : 0;
  double dc_sum = 0.0;
  size_t dc_n = 0;
  for (size_t i = seg.w0; i < seg.w1; ++i) {
    const uint8_t s = seg.st[i];
    if (s & kStMissing) ++f.missing;
    else if (s & (kStLeadOff | kStAfeSat)) ++f.flagged;
    else if (s & kStClipped) ++f.clipped;
    else if (s & kStFlat) ++f.flat;
    if (s == 0) { dc_sum += seg.x[i]; ++dc_n; }
  }
  f.usable_pct = nwin ? 100.0 * double(nwin - bad.count_bad(seg.w0, seg.w1)) / double(nwin) : 0.0;
  if (dc_n) f.dc_level = dc_sum / double(dc_n);

  const PpgDetection det = detect_pulses(seg, p_);

  // Amplitude plausibility reference from clean pulses of the whole segment.
  std::vector<double> clean_amps;
  for (const PpgPulse& pu : det.pulses)
    if (pu.clean && pu.amp > 0) clean_amps.push_back(pu.amp);
  const double amp_ref = median_of(clean_amps);
  auto is_valid = [&](const PpgPulse& pu) {
    return pu.clean && pu.amp > 0 && pu.amp >= 0.4 * amp_ref && pu.amp <= 2.5 * amp_ref;
  };

  // Pulses that belong to this window (exactly-once across overlapping passes).
  std::vector<size_t> kept;
  const double lower = has_prev_ ? std::max(prev_foot_t_ + 0.25, ws_s - 0.5) : ws_s;
  for (size_t k = 0; k < det.pulses.size(); ++k) {
    const double t = seg.time(det.pulses[k].foot_i);
    const bool lo_ok = has_prev_ ? (t > lower) : (t >= lower);
    if (lo_ok && t < we_s) kept.push_back(k);
  }
  f.n_pulses = int(kept.size());

  std::vector<double> ivs_ms;
  std::vector<uint8_t> iv_plaus;
  bool have_prev = has_prev_, prev_ok = prev_valid_;
  double tprev = prev_foot_t_;
  std::vector<size_t> valid_k;
  for (size_t k : kept) {
    const PpgPulse& pu = det.pulses[k];
    const double t = seg.time(pu.foot_i);
    const bool ok = is_valid(pu);
    if (have_prev) {
      const double iv = t - tprev;
      ivs_ms.push_back(iv * 1000.0);
      iv_plaus.push_back((ok && prev_ok && iv >= p_.ppg_min_interval_s && iv <= p_.rr_max_s) ? 1 : 0);
    }
    if (ok) valid_k.push_back(k);
    tprev = t;
    prev_ok = ok;
    have_prev = true;
  }
  f.n_valid_pulses = int(valid_k.size());

  std::vector<double> good_iv;
  for (size_t j = 0; j < ivs_ms.size(); ++j) {
    if (!iv_plaus[j]) continue;
    std::vector<double> nb;
    const size_t a = j >= 5 ? j - 5 : 0, b = std::min(ivs_ms.size(), j + 6);
    for (size_t q = a; q < b; ++q)
      if (iv_plaus[q]) nb.push_back(ivs_ms[q]);
    bool good = true;
    if (nb.size() >= 3) {
      const double ref = median_of(nb);
      good = std::fabs(ivs_ms[j] - ref) <= p_.ppg_interval_tol * ref;
    }
    if (good) good_iv.push_back(ivs_ms[j]);
  }
  f.pulse_intervals_ms = good_iv;
  if (good_iv.size() >= 3) f.pulse_rate_bpm = 60000.0 / mean_of(good_iv);

  std::vector<double> rise, width;
  for (size_t k : valid_k) {
    const PpgPulse& pu = det.pulses[k];
    f.pulse_foot_ms.push_back((seg.time(pu.foot_i) - ws_s) * 1000.0);
    f.pulse_amplitudes.push_back(pu.amp);
    f.pulse_rise_times_ms.push_back(pu.rise_ms);
    rise.push_back(pu.rise_ms);
    if (pu.width_ms > 0) width.push_back(pu.width_ms);
  }
  if (!f.pulse_amplitudes.empty()) {
    f.amplitude_median = median_of(f.pulse_amplitudes);
    f.rise_time_median_ms = median_of(rise);
    if (f.dc_level && *f.dc_level > 0) f.perfusion_index_pct = 100.0 * *f.amplitude_median / *f.dc_level;
  }
  if (!width.empty()) f.width_median_ms = median_of(width);

  // Template agreement of valid pulses.
  double corr = 0.0;
  {
    const size_t pre = size_t(std::lround(0.1 * seg.fs)), len = size_t(std::lround(0.7 * seg.fs));
    std::vector<std::vector<double>> shapes;
    for (size_t k : valid_k) {
      const size_t fi = size_t(std::lround(det.pulses[k].foot_i));
      if (fi < pre || fi + len >= det.bp.size()) continue;
      shapes.emplace_back(det.bp.begin() + long(fi - pre), det.bp.begin() + long(fi - pre + len));
    }
    if (shapes.size() >= 3) {
      std::vector<double> tmpl(len);
      for (size_t i = 0; i < len; ++i) {
        std::vector<double> col;
        for (const auto& s : shapes) col.push_back(s[i]);
        tmpl[i] = median_of(col);
      }
      double acc = 0.0;
      for (const auto& s : shapes) acc += std::max(0.0, pearson(s, tmpl));
      corr = acc / double(shapes.size());
    }
  }
  f.signal_quality = (f.usable_pct / 100.0) * corr;
  f.valid = f.usable_pct / 100.0 >= p_.ppg_min_usable && f.signal_quality >= p_.min_quality &&
            f.n_valid_pulses >= p_.min_beats;

  // PAT: first valid foot in [R + pat_min, R + pat_max] for each clean ECG beat; each foot used once.
  if (ecg && ecg->valid && f.valid && timestamps_valid) {
    std::vector<double> feet;
    for (const PpgPulse& pu : det.pulses)
      if (is_valid(pu)) feet.push_back(seg.time(pu.foot_i));
    size_t fp = 0;
    for (size_t b = 0; b < ecg->beat_t_s.size(); ++b) {
      if (!ecg->beat_clean[b]) continue;
      const double tr = ecg->beat_t_s[b];
      while (fp < feet.size() && feet[fp] < tr + p_.pat_min_s) ++fp;
      if (fp < feet.size() && feet[fp] <= tr + p_.pat_max_s) {
        f.pat_ms.push_back((feet[fp] - tr) * 1000.0);
        ++fp;
      }
    }
    if (f.pat_ms.size() >= 3) f.pat_median_ms = median_of(f.pat_ms);
  }

  if (!kept.empty()) {
    const size_t k = kept.back();
    has_prev_ = true;
    prev_foot_t_ = seg.time(det.pulses[k].foot_i);
    prev_valid_ = is_valid(det.pulses[k]);
  }
  return f;
}

}  // namespace neurosox
