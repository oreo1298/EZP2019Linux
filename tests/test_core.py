import random

import pytest

from ezp2019linux.core import protocol as P
from ezp2019linux.core.chipdb import (CLASS_24_EEPROM, CLASS_25_EEPROM, CLASS_93_EEPROM,
                                      CLASS_SPI_FLASH, Chip, ChipDatabase, build_dat,
                                      parse_dat)
from ezp2019linux.core.errors import NoChipError, OperationCancelled
from ezp2019linux.core.programmer import (Programmer, used_length, verify_length,
                                          write_length)
from ezp2019linux.core.simulator import SimConnector, Timing, VirtualProgrammer


@pytest.fixture(scope="module")
def db():
    return ChipDatabase.load(custom_path=None)


def chip(db, spec):
    matches = db.find(spec)
    assert matches, spec
    return matches[0]


def make(chip_obj, variant=P.VARIANT_A, contents=None):
    dev = VirtualProgrammer(variant=variant, timing=Timing(erase_polls=3))
    dev.insert(chip_obj, contents=contents)
    return dev, Programmer(SimConnector(dev))


def random_bytes(n, seed=1):
    return random.Random(seed).randbytes(n)


# -- chip database ---------------------------------------------------------------


def test_database_loads(db):
    assert len(db) > 800
    assert db.types() == ["SPI_FLASH", "24_EEPROM", "93_EEPROM", "25_EEPROM"]
    w25q64 = chip(db, "W25Q64")
    assert w25q64.chip_id == 0xEF4017
    assert w25q64.size == 8 << 20 and w25q64.page_size == 256
    assert w25q64.chip_class == CLASS_SPI_FLASH
    assert [c.name for c in db.by_jedec_id(0xEF4017)] == ["W25Q64"]
    assert chip(db, "WINBOND:W25Q128").size == 16 << 20


def test_dat_roundtrip(db):
    raw = build_dat(db.chips)
    assert len(raw) % 68 == 0
    again = parse_dat(raw)
    assert [c.key for c in again] == [c.key for c in db.chips]
    assert again[0] == db.chips[0]


def test_search(db):
    names = [c.name for c in db.search("24c02")]
    assert names and all("24C02" in n.upper() for n in names)
    assert db.search("ef4017")[0].name == "W25Q64"


# -- packets ----------------------------------------------------------------------


def test_setup_packet_byte_order(db):
    w = chip(db, "W25Q64")
    le = P.PacketBuilder("little").setup(w, 2)
    be = P.PacketBuilder("big").setup(w, 2)
    assert le[1] == be[1] == P.CMD_SETUP
    assert le[4:6] == (256).to_bytes(2, "little") and be[4:6] == (256).to_bytes(2, "big")
    assert le[8:12] == (8 << 20).to_bytes(4, "little")
    assert be[12:16] == (0xEF4017).to_bytes(4, "big")
    assert le[16] == 2 and le[28] == w.voltage


def test_setup_clock_byte_per_class(db):
    pk = P.PacketBuilder("little")
    e24 = chip(db, "MICROCHIP:24C02")
    e25 = chip(db, "COMMON:25010")
    assert pk.setup(e24, 3)[16] == e24.voltage
    assert pk.setup(e25, 3)[16] == (3 << 4) + e25.voltage


def test_variant_selection():
    assert P.variant_for("WinUSBComm Device", {0x85}) is P.VARIANT_A
    assert P.variant_for("WinUSBComm", {0x82}) is P.VARIANT_B
    assert P.variant_for(None, {0x82}) is P.VARIANT_B
    assert P.variant_for("WinUSBComm Device", {0x82}) is P.VARIANT_B


# -- operations against the simulator ------------------------------------------


@pytest.mark.parametrize("variant", [P.VARIANT_A, P.VARIANT_B])
@pytest.mark.parametrize("spec", ["W25Q16", "MICROCHIP:24C02", "ANACHIP:93C46",
                                  "COMMON:25640", "SST25VF512A"])
def test_full_cycle(db, variant, spec):
    c = chip(db, spec)
    original = random_bytes(c.size, seed=2)
    dev, prog = make(c, variant, contents=original)

    assert bytes(prog.read(c)) == original

    prog.erase(c)
    assert prog.blank_check(c).blank

    image = random_bytes(c.size, seed=3)
    prog.write(c, image)
    assert bytes(dev.memory) == image
    result = prog.verify(c, image)
    assert result.ok and result.length == c.size

    assert not prog.blank_check(c).blank
    assert dev.violations == []


def test_detect(db):
    w = chip(db, "W25Q64")
    dev, prog = make(w)
    result = prog.detect(db=db)
    assert result.kind == P.DETECT_SPI_FLASH
    assert result.jedec_id == 0xEF4017
    assert result.chip.name == "W25Q64"

    dev.insert(chip(db, "MICROCHIP:24C02"))
    assert prog.detect(db=db).type_name == "24_EEPROM"
    dev.insert(chip(db, "ANACHIP:93C46"))
    assert prog.detect(db=db).type_name == "93_EEPROM"
    dev.insert(None)
    assert not prog.detect(db=db).present
    assert dev.violations == []


def test_no_chip_error(db):
    w = chip(db, "W25Q64")
    dev, prog = make(w)
    dev.insert(None)
    with pytest.raises(NoChipError):
        prog.read(w)


def test_verify_reports_mismatches(db):
    c = chip(db, "W25Q16")
    image = random_bytes(c.size, seed=5)
    dev, prog = make(c, contents=image)
    bad = bytearray(image)
    bad[0x1234] ^= 0xFF
    bad[0x100000] ^= 0x01
    result = prog.verify(c, bad)
    assert not result.ok
    assert result.mismatches == 2
    assert result.first_mismatch == 0x1234
    assert result.offsets == [0x1234, 0x100000]


def test_flash_write_without_erase_fails_verify(db):
    c = chip(db, "W25Q16")
    dev, prog = make(c, contents=b"\x00" * c.size)
    image = random_bytes(c.size, seed=6)
    prog.write(c, image)
    assert not prog.verify(c, image).ok


def test_auto(db):
    c = chip(db, "W25Q32")
    dev, prog = make(c, contents=random_bytes(c.size, seed=7))
    image = bytearray(b"\xFF" * c.size)
    image[:100000] = random_bytes(100000, seed=8)
    n = write_length(c, image, len(image))
    assert n == 100096          # trailing 0xFF skipped, rounded up to a page
    result = prog.auto(c, image, write_len=n, verify_len=verify_length(c, image, len(image)))
    assert result.ok and result.erased and result.written == n
    assert result.verify.length == c.size
    assert bytes(dev.memory) == bytes(image)
    assert dev.violations == []


def test_large_chip_uses_4byte_mode(db):
    c = chip(db, "W25Q256")
    assert c.uses_4byte_address
    contents = bytearray(b"\xFF" * c.size)
    contents[0x1000000:0x1000010] = b"above-16-mebibyte"[:16]
    dev, prog = make(c, contents=contents)
    data = prog.read(c)
    assert data[0x1000000:0x1000010] == contents[0x1000000:0x1000010]
    assert P.CMD_SPI in dev.commands
    assert dev.violations == []


def test_partial_verify_stops_early(db):
    c = chip(db, "W25Q64")
    dev, prog = make(c)
    image = b"\xFF" * 4096
    assert prog.verify(c, image).ok
    assert dev.violations == []


def test_cancel(db):
    c = chip(db, "W25Q16")
    dev, prog = make(c)
    calls = []

    def cancel():
        calls.append(1)
        return len(calls) > 3

    with pytest.raises(OperationCancelled):
        prog.read(c, cancel=cancel)
    # The session was closed properly and the device accepts new work.
    assert prog.blank_check(c).blank
    assert dev.violations == []


def test_lengths(db):
    c = chip(db, "W25Q16")
    buf = bytearray(b"\xFF" * c.size)
    assert write_length(c, buf, len(buf)) == 0
    buf[10] = 0
    assert write_length(c, buf, len(buf)) == 256
    assert verify_length(c, buf, 1000) == 1024
    assert used_length(buf) == 11
    e = chip(db, "MICROCHIP:24C02")
    ebuf = bytearray(b"\xFF" * e.size)
    assert write_length(e, ebuf, len(ebuf)) == e.size      # EEPROM: whole range


def test_eeprom_erase_pages(db):
    c = chip(db, "MICROCHIP:24C02")
    dev, prog = make(c, contents=b"\x00" * c.size)
    prog.erase(c)
    assert bytes(dev.memory) == b"\xFF" * c.size
    assert dev.commands.count(P.CMD_ERASE) == c.size // c.page_size
    assert dev.violations == []


def test_trace(db):
    c = chip(db, "W25Q16")
    lines = []
    dev = VirtualProgrammer()
    dev.insert(c)
    prog = Programmer(SimConnector(dev), trace=lines.append)
    prog.detect()
    assert any(line.startswith("CMD") for line in lines)


def test_pad_payload_uses_buffer_for_chunk_tail(db):
    from ezp2019linux.core.programmer import pad_payload
    c = chip(db, "MICROCHIP:24C02")        # 16-byte pages, 64-byte transfer chunks
    buf = bytes(range(256))
    assert pad_payload(c, buf, 20) == buf[:64]
    assert pad_payload(c, buf[:20], 20) == buf[:20] + b"\xFF" * 44
