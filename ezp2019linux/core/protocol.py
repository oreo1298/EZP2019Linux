"""EZP2019+ wire protocol: constants and command packet builders.

Everything here mirrors the vendor Windows software; see docs/PROTOCOL.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from .chipdb import (CLASS_24_EEPROM, CLASS_25_EEPROM, CLASS_93_EEPROM,
                     CLASS_SPI_FLASH, Chip)

VENDOR_ID = 0x1FC8
PRODUCT_IDS = (0x310B, 0x310C)
MANUFACTURER_STRING = "www.zhifengsoft.com"

PACKET_SIZE = 64
USB_TIMEOUT_MS = 5000
RESPONSE_DELAY_S = 0.050          # host waits 50 ms before reading a response
STATUS_POLL_INTERVAL_S = 0.050    # erase status polling interval
EEPROM24_WRITE_SETTLE_S = 0.100   # pause after streaming data to a 24xx EEPROM
AUTO_VERIFY_DELAY_S = 0.350       # pause between program and verify in "Auto"

CMD_ERASE = 0x02
CMD_START = 0x05
CMD_SETUP = 0x07
CMD_END = 0x08
CMD_DETECT = 0x09
CMD_STATUS = 0x0A
CMD_SPI = 0x0B

ERASE_CHIP = 0x8000               # erase argument for SPI flash: whole chip
SPI_ENTER_4BYTE = 0xB7

# Detect response, byte 0.
DETECT_NONE = 0
DETECT_SPI_FLASH = 1
DETECT_24_EEPROM = 2
DETECT_93_EEPROM = 3

DETECT_TYPES = {
    DETECT_SPI_FLASH: "SPI_FLASH",
    DETECT_24_EEPROM: "24_EEPROM",
    DETECT_93_EEPROM: "93_EEPROM",
}

# SPI clock choices offered by the vendor software (index is sent to the device).
CLOCK_LABELS = ("12 MHz", "6 MHz", "3 MHz", "1.5 MHz", "750 kHz", "375 kHz")
DEFAULT_CLOCK = 1


@dataclass(frozen=True)
class Variant:
    """Endpoint layout and byte order of a firmware variant."""

    name: str
    ep_in: int
    ep_data_out: int
    ep_cmd_out: int
    byteorder: str   # "little" or "big"

    @property
    def description(self) -> str:
        return (f"{self.name} (IN 0x{self.ep_in:02X}, data OUT 0x{self.ep_data_out:02X}, "
                f"cmd OUT 0x{self.ep_cmd_out:02X}, {self.byteorder}-endian)")


# iProduct "WinUSBComm Device"
VARIANT_A = Variant("WinUSBComm Device", 0x85, 0x05, 0x04, "little")
# iProduct "WinUSBComm"
VARIANT_B = Variant("WinUSBComm", 0x82, 0x01, 0x02, "big")
VARIANTS = (VARIANT_A, VARIANT_B)


def variant_for(product: str | None, in_endpoints: set[int] | None = None) -> Variant:
    """Pick the firmware variant the same way the vendor software does.

    The product string decides; the endpoint list is used when the string is
    unavailable or disagrees with the endpoints the device actually has.
    """
    by_name = None
    if product is not None:
        if product == VARIANT_A.name:
            by_name = VARIANT_A
        elif product == VARIANT_B.name:
            by_name = VARIANT_B
    if in_endpoints:
        if by_name is not None and by_name.ep_in in in_endpoints:
            return by_name
        for variant in VARIANTS:
            if variant.ep_in in in_endpoints:
                return variant
    return by_name or VARIANT_A


class PacketBuilder:
    """Builds 64-byte command packets for one firmware variant."""

    def __init__(self, byteorder: str = "little"):
        if byteorder not in ("little", "big"):
            raise ValueError(byteorder)
        self.byteorder = byteorder

    def _put16(self, pkt: bytearray, off: int, value: int) -> None:
        pkt[off:off + 2] = (value & 0xFFFF).to_bytes(2, self.byteorder)

    def _put32(self, pkt: bytearray, off: int, value: int) -> None:
        pkt[off:off + 4] = (value & 0xFFFFFFFF).to_bytes(4, self.byteorder)

    def _chip_block(self, cmd: int, chip: Chip | None) -> bytearray:
        pkt = bytearray(PACKET_SIZE)
        pkt[1] = cmd
        if chip is not None:
            pkt[2] = chip.chip_class & 0xFF
            pkt[3] = chip.algorithm & 0xFF
            self._put16(pkt, 4, chip.page_size)
            self._put16(pkt, 6, chip.delay)
            self._put32(pkt, 8, chip.size)
            self._put32(pkt, 12, chip.chip_id)
            pkt[28] = chip.voltage & 0xFF
        return pkt

    def detect(self, chip: Chip | None, clock: int) -> bytes:
        pkt = self._chip_block(CMD_DETECT, chip)
        pkt[16] = clock & 0xFF
        return bytes(pkt)

    def setup(self, chip: Chip, clock: int) -> bytes:
        pkt = self._chip_block(CMD_SETUP, chip)
        if chip.chip_class == CLASS_SPI_FLASH:
            pkt[16] = clock & 0xFF
        elif chip.chip_class == CLASS_25_EEPROM:
            pkt[16] = ((clock << 4) + chip.voltage) & 0xFF
        else:
            pkt[16] = chip.voltage & 0xFF
        return bytes(pkt)

    def start(self, address: int) -> bytes:
        pkt = bytearray(PACKET_SIZE)
        pkt[1] = CMD_START
        self._put32(pkt, 8, address)
        return bytes(pkt)

    def erase(self, argument: int) -> bytes:
        pkt = bytearray(PACKET_SIZE)
        pkt[0] = 0x01
        pkt[1] = CMD_ERASE
        self._put16(pkt, 26, argument)
        return bytes(pkt)

    @staticmethod
    def status() -> bytes:
        pkt = bytearray(PACKET_SIZE)
        pkt[1] = CMD_STATUS
        return bytes(pkt)

    @staticmethod
    def spi(data: bytes) -> bytes:
        if len(data) > PACKET_SIZE - 4:
            raise ValueError("raw SPI transfer too long")
        pkt = bytearray(PACKET_SIZE)
        pkt[1] = CMD_SPI
        pkt[2] = 0x03
        pkt[3] = len(data)
        pkt[4:4 + len(data)] = data
        return bytes(pkt)

    @staticmethod
    def end() -> bytes:
        pkt = bytearray(PACKET_SIZE)
        pkt[0] = 0x01
        pkt[1] = CMD_END
        return bytes(pkt)


def erase_arguments(chip: Chip) -> list[int]:
    """Erase command argument(s) for a chip, in the order they are sent."""
    if chip.chip_class == CLASS_SPI_FLASH:
        return [ERASE_CHIP]
    if chip.chip_class == CLASS_93_EEPROM:
        return [chip.delay]
    if chip.chip_class in (CLASS_24_EEPROM, CLASS_25_EEPROM):
        step = chip.page_size or 1
        return list(range(0, chip.size, step))
    return []


def transfer_chunk(chip: Chip) -> int:
    """Chunk size the vendor software uses for bulk transfers."""
    return max(chip.page_size, 64)
