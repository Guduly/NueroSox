#include "neurosox/filters.hpp"

#include <algorithm>
#include <cmath>
#include <complex>

namespace neurosox {

namespace {
const double kPi = 3.14159265358979323846;

// Q of the k-th (1-based) conjugate pole pair of an order-N Butterworth prototype.
double butter_q(int order, int k) { return 1.0 / (2.0 * std::sin((2.0 * k - 1.0) * kPi / (2.0 * order))); }

Biquad rbj(bool lowpass, double fc, double fs, double q) {
  const double w0 = 2.0 * kPi * fc / fs;
  const double cw = std::cos(w0), alpha = std::sin(w0) / (2.0 * q);
  const double a0 = 1.0 + alpha;
  Biquad s;
  if (lowpass) {
    s.b0 = (1.0 - cw) / 2.0 / a0;
    s.b1 = (1.0 - cw) / a0;
    s.b2 = s.b0;
  } else {
    s.b0 = (1.0 + cw) / 2.0 / a0;
    s.b1 = -(1.0 + cw) / a0;
    s.b2 = s.b0;
  }
  s.a1 = -2.0 * cw / a0;
  s.a2 = (1.0 - alpha) / a0;
  return s;
}

Sos butter(bool lowpass, int order, double fc, double fs) {
  if (order < 2) order = 2;
  if (order % 2) ++order;  // even orders only (pairs of poles)
  fc = std::min(fc, 0.49 * fs);
  Sos sos;
  for (int k = 1; k <= order / 2; ++k) sos.push_back(rbj(lowpass, fc, fs, butter_q(order, k)));
  return sos;
}

double dc_gain(const Biquad& s) { return (s.b0 + s.b1 + s.b2) / (1.0 + s.a1 + s.a2); }
}  // namespace

Sos butter_lowpass(int order, double fc_hz, double fs_hz) { return butter(true, order, fc_hz, fs_hz); }
Sos butter_highpass(int order, double fc_hz, double fs_hz) { return butter(false, order, fc_hz, fs_hz); }

Sos butter_bandpass(double lo_hz, double hi_hz, double fs_hz, int hp_order, int lp_order) {
  Sos s = butter_highpass(hp_order, lo_hz, fs_hz);
  Sos l = butter_lowpass(lp_order, hi_hz, fs_hz);
  s.insert(s.end(), l.begin(), l.end());
  return s;
}

std::vector<double> sosfilt(const Sos& sos, const std::vector<double>& x, bool steady_state_init) {
  std::vector<double> y = x;
  if (y.empty()) return y;
  for (const Biquad& s : sos) {
    double z1 = 0.0, z2 = 0.0;
    if (steady_state_init) {
      const double v = y[0], g = dc_gain(s);
      z1 = (g - s.b0) * v;
      z2 = (s.b2 - s.a2 * g) * v;
    }
    for (double& v : y) {
      const double in = v;
      const double out = s.b0 * in + z1;
      z1 = s.b1 * in - s.a1 * out + z2;
      z2 = s.b2 * in - s.a2 * out;
      v = out;
    }
  }
  return y;
}

std::vector<double> sosfiltfilt(const Sos& sos, const std::vector<double>& x, size_t padlen) {
  const size_t n = x.size();
  if (n < 3) return x;
  padlen = std::min(padlen, n - 1);

  // Odd extension: 2*x[0] - x[padlen..1] ... x ... 2*x[n-1] - x[n-2..n-1-padlen]
  std::vector<double> ext;
  ext.reserve(n + 2 * padlen);
  for (size_t i = padlen; i >= 1; --i) ext.push_back(2.0 * x[0] - x[i]);
  ext.insert(ext.end(), x.begin(), x.end());
  for (size_t i = 1; i <= padlen; ++i) ext.push_back(2.0 * x[n - 1] - x[n - 1 - i]);

  std::vector<double> y = sosfilt(sos, ext, true);
  std::reverse(y.begin(), y.end());
  y = sosfilt(sos, y, true);
  std::reverse(y.begin(), y.end());
  return std::vector<double>(y.begin() + std::ptrdiff_t(padlen), y.begin() + std::ptrdiff_t(padlen + n));
}

double sos_magnitude(const Sos& sos, double f_hz, double fs_hz) {
  const double w = 2.0 * kPi * f_hz / fs_hz;
  const std::complex<double> z1 = std::polar(1.0, -w), z2 = std::polar(1.0, -2.0 * w);
  double mag = 1.0;
  for (const Biquad& s : sos) {
    const std::complex<double> num = s.b0 + s.b1 * z1 + s.b2 * z2;
    const std::complex<double> den = 1.0 + s.a1 * z1 + s.a2 * z2;
    mag *= std::abs(num / den);
  }
  return mag;
}

}  // namespace neurosox
