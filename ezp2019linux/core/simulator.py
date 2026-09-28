"""A virtual EZP2019+ for demos and tests.

``VirtualProgrammer`` emulates the programmer firmware at the packet level:
it decodes command packets with the firmware variant's byte order, keeps a
chip "in the socket" and streams its contents like the real device.  It also
records protocol violations (commands out of order, reads past the end of a
stream, wrong responses requested, ...) so tests can assert the host code
talks to it exactly like the vendor software does.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field

from . import protocol as P
from .chipdb import (CLASS_24_EEPROM, CLASS_25_EEPROM, CLASS_93_EEPROM,
                     CLASS_SPI_FLASH, Chip)
from .errors import NotConnectedError, UsbTransferError
from .transport import Connector, DeviceInfo, Transport


@dataclass
class SetupParams:
    command: int
    chip_class: int
    algorithm: int
    page_size: int
    delay: int
    size: int
    chip_id: int
    clock_byte: int
    voltage: int

    @classmethod
    def decode(cls, pkt: bytes, byteorder: str) -> "SetupParams":
        u16 = lambda o: int.from_bytes(pkt[o:o + 2], byteorder)  # noqa: E731
        u32 = lambda o: int.from_bytes(pkt[o:o + 4], byteorder)  # noqa: E731
        return cls(pkt[1], pkt[2], pkt[3], u16(4), u16(6), u32(8), u32(12), pkt[16], pkt[28])


@dataclass
class Timing:
    """Optional pacing so the demo mode feels like real hardware."""

    read_bytes_per_s: float = 0.0     # 0 = as fast as possible
    write_bytes_per_s: float = 0.0
    erase_polls: int = 3              # status polls a chip erase stays busy


@dataclass
class VirtualProgrammer:
    variant: P.Variant = P.VARIANT_A
    socket: Chip | None = None
    memory: bytearray = field(default_factory=bytearray)
    timing: Timing = field(default_factory=Timing)
    connected: bool = True
    bcd_device: int = 0x0200

    def __post_init__(self) -> None:
        self.lock = threading.RLock()
        self.violations: list[str] = []
        self.commands: list[int] = []
        self.sessions = 0
        self._reset()
        if self.socket is not None and not self.memory:
            self.insert(self.socket)

    # -- test/demo controls -------------------------------------------------

    def insert(self, chip: Chip | None, contents: bytes | None = None,
               pattern: str = "blank") -> None:
        """Put a chip in the socket (``None`` empties it)."""
        with self.lock:
            self.socket = chip
            if chip is None:
                self.memory = bytearray()
                return
            if contents is not None:
                data = bytearray(contents[:chip.size])
                data += b"\xFF" * (chip.size - len(data))
            elif pattern == "random":
                data = bytearray(random.Random(chip.size).randbytes(chip.size))
            else:
                data = bytearray(b"\xFF" * chip.size)
            self.memory = data

    @property
    def byteorder(self) -> str:
        return self.variant.byteorder

    def _violation(self, text: str) -> None:
        self.violations.append(text)

    def _reset(self) -> None:
        self.config: SetupParams | None = None
        self.stream_addr: int | None = None
        self.stream_dir: str | None = None     # "in" or "out"
        self.stream_end = 0
        self.four_byte = False
        self.busy = 0

    # -- firmware -----------------------------------------------------------

    def _detect_reply(self) -> bytes:
        reply = bytearray(P.PACKET_SIZE)
        chip = self.socket
        if chip is None:
            return bytes(reply)
        if chip.chip_class == CLASS_SPI_FLASH:
            reply[0] = P.DETECT_SPI_FLASH
            reply[1:4] = (chip.chip_id & 0xFFFFFF).to_bytes(3, "big")
        elif chip.chip_class == CLASS_24_EEPROM:
            reply[0] = P.DETECT_24_EEPROM
        elif chip.chip_class == CLASS_93_EEPROM:
            reply[0] = P.DETECT_93_EEPROM
        return bytes(reply)

    def _address(self, addr: int) -> int:
        # A chip larger than 16 MiB still in 3-byte mode only sees 24 address bits.
        if self.config and self.config.size > 0x1000000 and not self.four_byte:
            return addr & 0xFFFFFF
        return addr

    def handle(self, pkt: bytes, want_response: bool) -> bytes | None:
        with self.lock:
            cmd = pkt[1]
            self.commands.append(cmd)
            if self.stream_dir == "out" and cmd != P.CMD_END:
                self.stream_dir = None
            expects = {P.CMD_DETECT: True, P.CMD_SETUP: True, P.CMD_STATUS: True,
                       P.CMD_SPI: True, P.CMD_ERASE: False, P.CMD_END: False}
            if cmd in expects and expects[cmd] != want_response:
                self._violation(f"command 0x{cmd:02X}: response "
                                f"{'read' if want_response else 'not read'}")

            if cmd == P.CMD_DETECT:
                return self._detect_reply()

            if cmd == P.CMD_SETUP:
                if pkt[0] != 0:
                    self._violation("setup: byte 0 must be 0")
                self.config = SetupParams.decode(pkt, self.byteorder)
                self._check_setup(self.config)
                self.four_byte = False
                return bytes(pkt[:4]) + bytes(P.PACKET_SIZE - 4)

            if cmd == P.CMD_SPI:
                n = pkt[3]
                payload = bytes(pkt[4:4 + n])
                if pkt[2] != 3:
                    self._violation("raw SPI: byte 2 must be 3")
                if payload == bytes([P.SPI_ENTER_4BYTE]):
                    self.four_byte = True
                reply = bytearray(P.PACKET_SIZE)
                if payload[:1] == b"\x9F" and self.socket is not None:
                    reply[4:7] = (self.socket.chip_id & 0xFFFFFF).to_bytes(3, "big")
                return bytes(reply)

            if cmd == P.CMD_START:
                if self.config is None:
                    self._violation("start before setup")
                    return bytes(P.PACKET_SIZE) if want_response else None
                addr = int.from_bytes(pkt[8:12], self.byteorder)
                if self.config.size > 0x1000000 and not self.four_byte:
                    self._violation("start on a >16 MiB chip without 4-byte mode")
                self.stream_addr = addr
                self.stream_end = self.config.size
                self.stream_dir = "in" if want_response else "out"
                return bytes(P.PACKET_SIZE) if want_response else None

            if cmd == P.CMD_ERASE:
                if pkt[0] != 1:
                    self._violation("erase: byte 0 must be 1")
                self._erase(int.from_bytes(pkt[26:28], self.byteorder))
                return None

            if cmd == P.CMD_STATUS:
                reply = bytearray(P.PACKET_SIZE)
                if self.busy > 0:
                    self.busy -= 1
                    reply[0] = 0x01
                    if self.busy == 0:
                        self._finish_chip_erase()
                return bytes(reply)

            if cmd == P.CMD_END:
                if pkt[0] != 1:
                    self._violation("end: byte 0 must be 1")
                self._reset()
                return None

            self._violation(f"unknown command 0x{cmd:02X}")
            return bytes(P.PACKET_SIZE) if want_response else None

    def _check_setup(self, cfg: SetupParams) -> None:
        chip = self.socket
        if chip is None:
            return
        if cfg.chip_class == chip.chip_class and cfg.size != chip.size:
            # Wrong chip selected by the user; the real hardware just misbehaves.
            return
        if cfg.chip_class == CLASS_SPI_FLASH:
            if cfg.clock_byte > 5:
                self._violation(f"setup: bad SPI clock index {cfg.clock_byte}")
        elif cfg.chip_class == CLASS_25_EEPROM:
            if (cfg.clock_byte & 0x0F) != cfg.voltage:
                self._violation("setup: 25xx clock byte must carry the voltage")
        elif cfg.clock_byte != cfg.voltage:
            self._violation("setup: 24/93 clock byte must be the voltage")

    def _erase(self, argument: int) -> None:
        cfg = self.config
        if cfg is None:
            self._violation("erase before setup")
            return
        if cfg.chip_class == CLASS_SPI_FLASH:
            if argument != P.ERASE_CHIP:
                self._violation(f"SPI erase argument 0x{argument:04X}")
            self.busy = max(1, self.timing.erase_polls)
        elif cfg.chip_class in (CLASS_24_EEPROM, CLASS_25_EEPROM):
            page = cfg.page_size or 1
            if argument % page:
                self._violation(f"EEPROM erase at unaligned address 0x{argument:X}")
            # The command only carries 16 address bits; larger chips repeat.
            for base in range(argument, len(self.memory), 0x10000):
                self.memory[base:base + page] = b"\xFF" * len(self.memory[base:base + page])
        elif cfg.chip_class == CLASS_93_EEPROM:
            if argument != cfg.delay:
                self._violation("93xx erase argument must be the chip delay")
            self.memory[:] = b"\xFF" * len(self.memory)

    def _finish_chip_erase(self) -> None:
        self.memory[:] = b"\xFF" * len(self.memory)

    def read_in(self, size: int) -> bytes:
        with self.lock:
            if self.stream_dir != "in" or self.stream_addr is None:
                self._violation("bulk IN read with no read stream")
                raise UsbTransferError("USB read timed out (simulated).")
            remaining = self.stream_end - self.stream_addr
            if remaining <= 0:
                self._violation("read past the end of the chip")
                raise UsbTransferError("USB read timed out (simulated).")
            n = min(size, remaining)
            start = self._address(self.stream_addr)
            data = bytes(self.memory[start:start + n])
            data += b"\xFF" * (n - len(data))
            self.stream_addr += n
        self._pace(n, self.timing.read_bytes_per_s)
        return data

    def write_out(self, data: bytes) -> None:
        with self.lock:
            if self.stream_dir != "out" or self.stream_addr is None:
                self._violation("bulk OUT write with no write stream")
                raise UsbTransferError("USB write timed out (simulated).")
            if len(data) % 64 and self.stream_addr + len(data) < self.stream_end:
                self._violation(f"write chunk of {len(data)} bytes is not packet aligned")
            if self.stream_addr + len(data) > self.stream_end:
                self._violation("write past the end of the chip")
            start = self._address(self.stream_addr)
            end = min(start + len(data), len(self.memory))
            chunk = data[:end - start]
            if self.config and self.config.chip_class == CLASS_SPI_FLASH and chunk:
                # NOR flash: programming can only clear bits.
                merged = (int.from_bytes(self.memory[start:end], "little")
                          & int.from_bytes(chunk, "little"))
                self.memory[start:end] = merged.to_bytes(len(chunk), "little")
            else:
                self.memory[start:end] = chunk
            self.stream_addr += len(data)
        self._pace(len(data), self.timing.write_bytes_per_s)

    @staticmethod
    def _pace(n: int, rate: float) -> None:
        if rate > 0:
            time.sleep(n / rate)


class SimTransport(Transport):
    def __init__(self, device: VirtualProgrammer):
        super().__init__()
        if not device.connected:
            raise NotConnectedError()
        self.device = device
        self.variant = device.variant
        self.info = SimConnector.info_for(device)
        device.sessions += 1

    def close(self) -> None:
        pass

    def command(self, packet: bytes, response: bool) -> bytes | None:
        if len(packet) != P.PACKET_SIZE:
            raise ValueError("command packets are 64 bytes")
        if not self.device.connected:
            raise NotConnectedError("The programmer was disconnected.")
        if self.trace:
            from .transport import hexdump
            self.trace(f"CMD  > {hexdump(packet[:32])}")
        reply = self.device.handle(packet, response)
        if response:
            time.sleep(0.002)
            if self.trace and reply is not None:
                from .transport import hexdump
                self.trace(f"RESP < {hexdump(reply[:32])}")
            return reply
        return None

    def _write_ep(self, ep: int, data: bytes) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    def _read_ep(self, ep: int, size: int) -> bytes:  # pragma: no cover - unused
        raise NotImplementedError

    def read_data(self, size: int) -> bytes:
        if not self.device.connected:
            raise NotConnectedError("The programmer was disconnected.")
        return self.device.read_in(size)

    def write_data(self, data: bytes) -> None:
        if not self.device.connected:
            raise NotConnectedError("The programmer was disconnected.")
        self.device.write_out(bytes(data))


class SimConnector(Connector):
    def __init__(self, device: VirtualProgrammer | None = None):
        self.device = device or VirtualProgrammer()

    @staticmethod
    def info_for(device: VirtualProgrammer) -> DeviceInfo:
        return DeviceInfo(
            vendor_id=P.VENDOR_ID, product_id=P.PRODUCT_IDS[0],
            manufacturer=P.MANUFACTURER_STRING, product=device.variant.name,
            serial=None, bcd_device=device.bcd_device, variant=device.variant,
            simulated=True)

    def devices(self) -> list[DeviceInfo]:
        return [self.info_for(self.device)] if self.device.connected else []

    def open(self) -> SimTransport:
        return SimTransport(self.device)
