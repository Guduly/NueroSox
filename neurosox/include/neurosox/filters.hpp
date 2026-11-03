// Butterworth biquad cascades and zero-phase filtering.
#pragma once

#include <cstddef>
#include <vector>

namespace neurosox {

struct Biquad {
  double b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0;  // a0 normalised to 1
};
using Sos = std::vector<Biquad>;

// Even-order Butterworth sections via bilinear transform with frequency pre-warping.
Sos butter_lowpass(int order, double fc_hz, double fs_hz);
Sos butter_highpass(int order, double fc_hz, double fs_hz);
// High-pass (hp_order) cascaded with low-pass (lp_order). hi is clamped below Nyquist.
Sos butter_bandpass(double lo_hz, double hi_hz, double fs_hz, int hp_order = 2, int lp_order = 4);

// Single forward pass (direct form II transposed). If steady_state_init, the filter state is
// initialised as if the input had been x[0] forever, which suppresses the start-up transient.
std::vector<double> sosfilt(const Sos& sos, const std::vector<double>& x, bool steady_state_init = true);

// Zero-phase forward-backward filtering with odd-symmetric edge padding.
std::vector<double> sosfiltfilt(const Sos& sos, const std::vector<double>& x, size_t padlen);

// |H(e^{j2*pi*f/fs})| of the cascade (used for verification).
double sos_magnitude(const Sos& sos, double f_hz, double fs_hz);

}  // namespace neurosox
