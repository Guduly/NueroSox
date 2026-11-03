#include "neurosox/frame.hpp"

#include <cstring>

namespace neurosox {

namespace {

void put16(std::vector<uint8_t>& b, uint16_t v) {
  b.push_back(uint8_t(v & 0xFF));
  b.push_back(uint8_t(v >> 8));
}
void put32(std::vector<uint8_t>& b, uint32_t v) {
  for (int i = 0; i < 4; ++i) b.push_back(uint8_t((v >> (8 * i)) & 0xFF));
}
void put64(std::vector<uint8_t>& b, uint64_t v) {
  for (int i = 0; i < 8; ++i) b.push_back(uint8_t((v >> (8 * i)) & 0xFF));
}
uint16_t get16(const uint8_t* p) { return uint16_t(p[0] | (p[1] << 8)); }
uint32_t get32(const uint8_t* p) {
  return uint32_t(p[0]) | (uint32_t(p[1]) << 8) | (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24);
}
uint64_t get64(const uint8_t* p) {
  uint64_t v = 0;
  for (int i = 7; i >= 0; --i) v = (v << 8) | p[i];
  return v;
}

}  // namespace

uint16_t crc16_ccitt(const uint8_t* data, size_t n) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < n; ++i) {
    crc ^= uint16_t(data[i]) << 8;
    for (int b = 0; b < 8; ++b) crc = (crc & 0x8000) ? uint16_t((crc << 1) ^ 0x1021) : uint16_t(crc << 1);
  }
  return crc;
}

std::vector<uint8_t> encode_frame(const Frame& f) {
  std::vector<uint8_t> out;
  const bool is_data = f.type == FrameType::Data;
  const uint32_t payload = is_data ? uint32_t(f.samples.size() * 4) : uint32_t(f.text.size());
  out.reserve(kHeaderSize + payload + 2);
  out.push_back(kMagic0);
  out.push_back(kMagic1);
  out.push_back(kFrameVersion);
  out.push_back(uint8_t(f.type));
  out.push_back(f.channel);
  out.push_back(f.flags);
  put16(out, f.sample_rate_hz);
  put64(out, f.start_counter);
  put64(out, f.acq_timestamp_us);
  put32(out, payload);
  if (is_data) {
    for (int32_t s : f.samples) put32(out, uint32_t(s));
  } else {
    out.insert(out.end(), f.text.begin(), f.text.end());
  }
  put16(out, crc16_ccitt(out.data(), out.size()));
  return out;
}

std::vector<Frame> StreamDecoder::push(const uint8_t* data, size_t n) {
  // Compact consumed bytes occasionally so the buffer cannot grow without bound.
  if (pos_ > (1u << 16) && pos_ * 2 > buf_.size()) {
    buf_.erase(buf_.begin(), buf_.begin() + std::ptrdiff_t(pos_));
    pos_ = 0;
  }
  buf_.insert(buf_.end(), data, data + n);

  std::vector<Frame> frames;
  for (;;) {
    const size_t avail = buf_.size() - pos_;
    if (avail == 0) break;
    const uint8_t* p = buf_.data() + pos_;

    // Hunt for magic.
    if (p[0] != kMagic0) {
      ++pos_;
      ++stats_.bytes_skipped;
      continue;
    }
    if (avail < 2) break;  // could be the first byte of the magic
    if (p[1] != kMagic1) {
      ++pos_;
      ++stats_.bytes_skipped;
      continue;
    }
    if (avail < kHeaderSize) break;

    const uint8_t version = p[2], type = p[3], channel = p[4];
    const uint32_t plen = get32(p + 24);
    bool header_ok = version == kFrameVersion && plen <= kMaxPayloadBytes &&
                     (type == uint8_t(FrameType::Data) || type == uint8_t(FrameType::Config));
    if (header_ok && type == uint8_t(FrameType::Data)) {
      header_ok = (plen % 4 == 0) && channel < 2 && get16(p + 6) > 0;
    }
    if (!header_ok) {
      ++stats_.header_errors;
      ++pos_;
      ++stats_.bytes_skipped;
      continue;
    }
    const size_t total = kHeaderSize + plen + 2;
    if (avail < total) break;  // wait for the rest of the frame

    const uint16_t want = get16(p + kHeaderSize + plen);
    if (crc16_ccitt(p, kHeaderSize + plen) != want) {
      ++stats_.crc_errors;
      ++pos_;
      ++stats_.bytes_skipped;
      continue;
    }

    Frame f;
    f.type = FrameType(type);
    f.channel = channel;
    f.flags = p[5];
    f.sample_rate_hz = get16(p + 6);
    f.start_counter = get64(p + 8);
    f.acq_timestamp_us = get64(p + 16);
    if (f.type == FrameType::Data) {
      f.samples.resize(plen / 4);
      for (size_t i = 0; i < f.samples.size(); ++i)
        f.samples[i] = int32_t(get32(p + kHeaderSize + 4 * i));
    } else {
      f.text.assign(reinterpret_cast<const char*>(p + kHeaderSize), plen);
    }
    frames.push_back(std::move(f));
    ++stats_.frames_ok;
    pos_ += total;
  }
  return frames;
}

}  // namespace neurosox
