#!/usr/bin/env python3
"""Probe and capture AFE4950EVM USB CDC traffic on Linux (Python stdlib only).

The EVM must have a valid register configuration for ECG/PPG and FIFO timing;
this script does not invent one. Captured FIFO words are raw 24-bit values.
"""

import argparse
import json
import os
import select
import termios
import time
import tty

HEADER = b"\x00\x00\x00\x02"


class SerialPort:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.settings = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)

    def close(self):
        termios.tcsetattr(self.fd, termios.TCSANOW, self.settings)
        os.close(self.fd)

    def send(self, data):
        while data:
            _, writable, _ = select.select([], [self.fd], [], 2)
            if not writable:
                raise TimeoutError(f"Timed out writing to {self.path}")
            data = data[os.write(self.fd, data):]

    def read(self, wait):
        readable, _, _ = select.select([self.fd], [], [], wait)
        if not readable:
            return b""
        try:
            return os.read(self.fd, 4096)
        except BlockingIOError:
            return b""

    def query(self, command, wait=1.0):
        termios.tcflush(self.fd, termios.TCIFLUSH)
        self.send(command)
        data = bytearray()
        until = time.monotonic() + wait
        while time.monotonic() < until:
            data.extend(self.read(until - time.monotonic()))
            if b"\n" in data:
                break
        return bytes(data).strip()


def parse_frames(buffer):
    """Return complete FIFO packets; keep a partial packet for the next read."""
    packets = []
    while True:
        start = buffer.find(HEADER)
        if start < 0:
            return packets, buffer[-3:]
        buffer = buffer[start:]
        if len(buffer) < 8:
            return packets, buffer
        pointer = int.from_bytes(buffer[4:7], "little")
        if buffer[7] != 3:
            buffer = buffer[1:]
            continue
        word_count = (pointer + 1) & 0xff
        size = 8 + word_count * 4
        if len(buffer) < size:
            return packets, buffer
        words = []
        valid = True
        for offset in range(8, size, 4):
            if buffer[offset + 3] != 5:
                valid = False
                break
            words.append(int.from_bytes(buffer[offset:offset + 3], "little"))
        if valid:
            packets.append({"fifo_pointer": pointer, "raw_words": words})
            buffer = buffer[size:]
        else:
            buffer = buffer[1:]


def load_profile(path, control):
    """Apply an exported Bio-Sensing .cfg in its original register order."""
    entries = []
    with open(path, encoding="ascii") as source:
        for number, raw in enumerate(source, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            kind, separator, register = line.partition("|")
            if not separator or kind not in ("AFE4950", "AFE4950_PPM"):
                raise ValueError(f"Invalid profile line {number}: {line}")
            fields = register.split()
            if len(fields) != 2:
                raise ValueError(f"Invalid profile line {number}: {line}")
            address, value = (int(field, 0) for field in fields)
            if not 0 <= address <= 0xff or not 0 <= value <= 0xffffff:
                raise ValueError(f"Out-of-range register on profile line {number}")
            entries.append((kind, address, value))
    if not entries:
        raise ValueError("The profile contains no register settings")

    page = None
    for index, (kind, address, value) in enumerate(entries, 1):
        if address == 0x01:
            page = value
        elif page != (1 if kind == "AFE4950_PPM" else 0):
            raise ValueError(f"Profile page mismatch at register write {index}")
        # The final '1' selects the AFE on this EVM. No newline is required.
        command = f"C:REG_W{address:02X}{value:06X}1".encode("ascii")
        response = control.query(command)
        if response != b"SUCCESS":
            raise RuntimeError(f"Register write {index}/{len(entries)} failed: {response!r}")
        if index % 50 == 0 or index == len(entries):
            print(f"Applied {index}/{len(entries)} register settings")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ports", nargs=2, default=["/dev/ttyACM0", "/dev/ttyACM1"])
    parser.add_argument("--capture", action="store_true", help="start raw FIFO capture")
    parser.add_argument("--profile", help="AFE4950_Settings.cfg exported by TI Bio-Sensing")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--output", default="afe4950_raw.jsonl")
    args = parser.parse_args()

    ports = [SerialPort(path) for path in args.ports]
    try:
        roles = {}
        for port in ports:
            reply = port.query(b"C:CDC?")
            print(f"{port.path}: {reply.decode('ascii', 'replace') or '(no reply)'}")
            roles[reply] = port
        control, capture = roles.get(b"REG_EDIT"), roles.get(b"CAPTURE")
        if not control or not capture:
            raise RuntimeError("Could not identify both ports. Check the EVM firmware and USB cable.")
        print("Firmware:", control.query(b"FIRMWARE_VER?").decode("ascii", "replace"))
        print("Device:", control.query(b"DEVICE?").decode("ascii", "replace"))
        if args.profile:
            load_profile(args.profile, control)
        if not args.capture:
            print("USB command interface is working. Use --capture to test configured FIFO data.")
            return

        # The firmware reads ASCII hex: finite flag (00=infinite) and 32-bit count.
        command = b"C:CAP_START" + b"00" + b"00000000"
        buffer = b""
        received = 0
        deadline = time.monotonic() + args.seconds
        capture.send(command)
        try:
            with open(args.output, "w", encoding="utf-8") as out:
                while time.monotonic() < deadline:
                    chunk = capture.read(min(0.5, deadline - time.monotonic()))
                    if not chunk:
                        continue
                    packets, buffer = parse_frames(buffer + chunk)
                    for packet in packets:
                        packet["host_time"] = time.time()
                        out.write(json.dumps(packet) + "\n")
                        received += len(packet["raw_words"])
        finally:
            capture.send(b"C:CAP_STOP")
        print(f"Saved {received} raw FIFO words to {args.output}")
        if received == 0:
            print("No FIFO packets. The AFE may need its register/timing profile loaded first.")
    finally:
        for port in ports:
            port.close()


if __name__ == "__main__":
    main()
