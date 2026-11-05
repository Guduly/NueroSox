// afe4950_i2c.cpp - skeleton for reading the AFE4950 FIFO from the SK-AM62B over I2C
// and streaming samples to the host over UDP.
//
// Everything marked TODO must come from the AFE4950 datasheet / EVM user guide
// (gated by TI). No register address or value below is verified; none were invented
// beyond obvious placeholders.
//
// Build:  g++ -std=c++17 -O2 -Wall -Wextra afe4950_i2c.cpp -o afe4950_i2c
// Run:    ./afe4950_i2c /dev/i2c-1 192.168.1.10

#include <arpa/inet.h>
#include <fcntl.h>
#include <linux/i2c-dev.h>
#include <linux/i2c.h>
#include <netinet/in.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

// ---------------- TODO: fill in from the datasheet ----------------
constexpr uint8_t  AFE_ADDR          = 0x00;  // 7-bit I2C address. Confirm with `i2cdetect -y <bus>`.
constexpr unsigned N_PHASES          = 1;     // samples per FIFO cycle (ECG + enabled PPG phases)
constexpr unsigned BYTES_PER_SAMPLE  = 3;     // 22-bit value in 3 bytes (confirm packing/alignment)
constexpr unsigned FIFO_DEPTH        = 256;   // samples
constexpr uint8_t  REG_FIFO_COUNT    = 0x00;  // TODO: register holding number of samples in FIFO
constexpr uint8_t  REG_FIFO_DATA     = 0x00;  // TODO: FIFO read register (check auto-increment behaviour)
// Register init table: {register address, bytes to write}. TODO: reset, timing, LED currents,
// ECG gain/filters, FIFO enable, interface select, etc.
const std::vector<std::pair<uint8_t, std::vector<uint8_t>>> kInitTable = {
    // {0x00, {0x00, 0x00, 0x00}},
};
constexpr size_t MAX_CHUNK = 63;  // bytes per I2C read (multiple of 3); lower it if the adapter complains
// -------------------------------------------------------------------

class I2cBus {
 public:
  explicit I2cBus(const std::string& path) : fd_(::open(path.c_str(), O_RDWR)) {
    if (fd_ < 0) std::cerr << "open " << path << ": " << std::strerror(errno) << "\n";
  }
  ~I2cBus() { if (fd_ >= 0) ::close(fd_); }
  I2cBus(const I2cBus&) = delete;
  I2cBus& operator=(const I2cBus&) = delete;
  bool ok() const { return fd_ >= 0; }

  // One combined transaction: optional write, then optional read with a repeated start.
  bool xfer(uint8_t addr, const uint8_t* w, uint16_t wlen, uint8_t* r, uint16_t rlen) {
    i2c_msg msgs[2];
    unsigned n = 0;
    if (wlen) {
      msgs[n].addr = addr; msgs[n].flags = 0; msgs[n].len = wlen;
      msgs[n].buf = const_cast<uint8_t*>(w); ++n;
    }
    if (rlen) {
      msgs[n].addr = addr; msgs[n].flags = I2C_M_RD; msgs[n].len = rlen;
      msgs[n].buf = r; ++n;
    }
    i2c_rdwr_ioctl_data d{msgs, n};
    return ::ioctl(fd_, I2C_RDWR, &d) == static_cast<int>(n);
  }

 private:
  int fd_;
};

class Afe4950 {
 public:
  Afe4950(I2cBus& bus, uint8_t addr) : bus_(bus), addr_(addr) {}

  bool regWrite(uint8_t reg, const std::vector<uint8_t>& data) {
    std::vector<uint8_t> buf;
    buf.push_back(reg);
    buf.insert(buf.end(), data.begin(), data.end());
    return bus_.xfer(addr_, buf.data(), static_cast<uint16_t>(buf.size()), nullptr, 0);
  }
  bool regRead(uint8_t reg, uint8_t* out, uint16_t len) {
    return bus_.xfer(addr_, &reg, 1, out, len);
  }

  bool configure() {
    for (const auto& e : kInitTable)
      if (!regWrite(e.first, e.second)) return false;
    return true;
  }
  bool fifoCount(unsigned& n) {
    uint8_t v = 0;
    if (!regRead(REG_FIFO_COUNT, &v, 1)) return false;  // TODO: width/mask per datasheet
    n = v;
    return true;
  }
  bool readFifoBytes(std::vector<uint8_t>& out, size_t nbytes) {
    out.resize(nbytes);
    size_t off = 0;
    while (off < nbytes) {
      uint16_t chunk = static_cast<uint16_t>(std::min(MAX_CHUNK, nbytes - off));
      if (!regRead(REG_FIFO_DATA, out.data() + off, chunk)) return false;
      off += chunk;
    }
    return true;
  }

 private:
  I2cBus& bus_;
  uint8_t addr_;
};

// 22-bit two's complement stored right-aligned in 3 bytes (TODO: confirm alignment/flag bits).
static inline int32_t decode22(const uint8_t* p) {
  uint32_t v = (uint32_t(p[0]) << 16) | (uint32_t(p[1]) << 8) | p[2];
  v &= 0x3FFFFFu;
  if (v & 0x200000u) v |= 0xFFC00000u;
  return static_cast<int32_t>(v);
}

class UdpOut {
 public:
  UdpOut(const char* ip, uint16_t port) : s_(::socket(AF_INET, SOCK_DGRAM, 0)) {
    std::memset(&dst_, 0, sizeof dst_);
    dst_.sin_family = AF_INET;
    dst_.sin_port = htons(port);
    ::inet_pton(AF_INET, ip, &dst_.sin_addr);
  }
  ~UdpOut() { if (s_ >= 0) ::close(s_); }
  UdpOut(const UdpOut&) = delete;
  UdpOut& operator=(const UdpOut&) = delete;
  void send(const void* p, size_t n) {
    ::sendto(s_, p, n, 0, reinterpret_cast<const sockaddr*>(&dst_), sizeof dst_);
  }

 private:
  int s_;
  sockaddr_in dst_;
};

struct __attribute__((packed)) PacketHeader {
  uint32_t seq;        // increments per packet: the receiver detects loss from gaps
  uint64_t t_ns;       // CLOCK_MONOTONIC time at read
  uint16_t n_samples;  // total int32 samples that follow
  uint16_t n_phases;
  uint16_t flags;      // bit0 = FIFO overflow suspected, bit1 = I2C read error
};

static uint64_t now_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch()).count();
}

int main(int argc, char** argv) {
  const std::string bus_path = argc > 1 ? argv[1] : "/dev/i2c-1";
  const char* host = argc > 2 ? argv[2] : "192.168.1.10";

  I2cBus bus(bus_path);
  if (!bus.ok()) return 1;
  Afe4950 afe(bus, AFE_ADDR);
  if (!afe.configure()) { std::cerr << "AFE configure failed\n"; return 1; }
  UdpOut out(host, 5005);

  uint32_t seq = 0;
  std::vector<uint8_t> raw;
  std::vector<uint8_t> packet;

  while (true) {
    // TODO: block on the AFE data-ready GPIO (libgpiod edge event) instead of polling.
    unsigned n = 0;
    if (!afe.fifoCount(n)) { std::this_thread::sleep_for(std::chrono::milliseconds(1)); continue; }
    if (n < N_PHASES) { std::this_thread::sleep_for(std::chrono::milliseconds(1)); continue; }

    uint16_t flags = (n >= FIFO_DEPTH) ? 1 : 0;   // FIFO full: samples may have been dropped
    n -= n % N_PHASES;                            // read whole cycles only, keeps phases aligned
    const uint64_t t = now_ns();
    if (!afe.readFifoBytes(raw, size_t(n) * BYTES_PER_SAMPLE)) flags |= 2;

    PacketHeader h{seq++, t, static_cast<uint16_t>(n), static_cast<uint16_t>(N_PHASES), flags};
    packet.assign(reinterpret_cast<uint8_t*>(&h), reinterpret_cast<uint8_t*>(&h) + sizeof h);
    if (!(flags & 2)) {
      for (unsigned i = 0; i < n; ++i) {          // sample i belongs to phase (i % N_PHASES)
        int32_t v = decode22(&raw[size_t(i) * BYTES_PER_SAMPLE]);
        const uint8_t* b = reinterpret_cast<const uint8_t*>(&v);
        packet.insert(packet.end(), b, b + sizeof v);
      }
    }
    out.send(packet.data(), packet.size());
  }
}
