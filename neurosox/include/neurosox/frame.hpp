// NeuroSox raw-stream framing.
//
// Binary layout (all integers little-endian):
//   off size field
//    0   2  magic            'N','S'
//    2   1  version          1
//    3   1  frame_type       1 = DATA (int32 ADC counts), 2 = CONFIG (UTF-8 "key=value" lines)
//    4   1  channel          0 = ECG_1, 1 = PPG_1 (ignored for CONFIG)
//    5   1  flags            bit0 lead-off / no contact, bit1 AFE saturation
//    6   2  sample_rate_hz   (DATA)
//    8   8  start_counter    monotonic sample counter of first sample (DATA)
//   16   8  acq_timestamp_us UTC microseconds at acquisition of first sample (CONFIG: effective time)
//   24   4  payload_len     bytes
//   28   N  payload         DATA: payload_len/4 x int32 ; CONFIG: text
//   28+N 2  crc16           CRC-16/CCITT-FALSE over bytes [0, 28+N)
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace neurosox {

constexpr uint8_t kMagic0 = 'N';
constexpr uint8_t kMagic1 = 'S';
constexpr uint8_t kFrameVersion = 1;
constexpr size_t kHeaderSize = 28;
constexpr uint32_t kMaxPayloadBytes = 1u << 22;  // 4 MiB sanity bound

constexpr uint8_t kFlagLeadOff = 0x01;
constexpr uint8_t kFlagSaturation = 0x02;

enum class FrameType : uint8_t { Data = 1, Config = 2 };

struct Frame {
  FrameType type = FrameType::Data;
  uint8_t channel = 0;
  uint8_t flags = 0;
  uint16_t sample_rate_hz = 0;
  uint64_t start_counter = 0;
  uint64_t acq_timestamp_us = 0;
  std::vector<int32_t> samples;  // DATA
  std::string text;              // CONFIG
};

uint16_t crc16_ccitt(const uint8_t* data, size_t n);
std::vector<uint8_t> encode_frame(const Frame& f);

struct DecoderStats {
  uint64_t frames_ok = 0;
  uint64_t crc_errors = 0;
  uint64_t header_errors = 0;
  uint64_t bytes_skipped = 0;  // bytes discarded while hunting for the next valid frame
};

// Incremental decoder: feed arbitrary byte chunks, receive complete CRC-checked frames.
// After corruption it resynchronises on the next valid magic + header + CRC.
class StreamDecoder {
 public:
  std::vector<Frame> push(const uint8_t* data, size_t n);
  const DecoderStats& stats() const { return stats_; }
  size_t buffered_bytes() const { return buf_.size() - pos_; }

 private:
  std::vector<uint8_t> buf_;
  size_t pos_ = 0;
  DecoderStats stats_;
};

}  // namespace neurosox
