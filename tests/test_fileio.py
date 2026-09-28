import random

import pytest

from ezp2019linux.core import fileio


def data(n, seed=0):
    return random.Random(seed).randbytes(n)


@pytest.mark.parametrize("ext,fmt", [(".hex", fileio.FMT_IHEX), (".srec", fileio.FMT_SREC),
                                     (".bin", fileio.FMT_BIN), (".rom", fileio.FMT_BIN)])
@pytest.mark.parametrize("size", [100, 0x10000, 0x10010, 0x1000010])
def test_roundtrip(tmp_path, ext, fmt, size):
    if size > 0x10000 and fmt == fileio.FMT_BIN:
        size = 0x10010
    payload = data(size, size)
    path = tmp_path / f"image{ext}"
    assert fileio.save_image(path, payload) == fmt
    loaded = fileio.load_image(path)
    assert loaded.fmt == fmt
    assert bytes(loaded.data) == payload


def test_cap_header_is_skipped(tmp_path):
    body = data(4096, 1)
    path = tmp_path / "bios.cap"
    path.write_bytes(b"\xAA" * fileio.CAP_HEADER_SIZE + body)
    loaded = fileio.load_image(path)
    assert loaded.fmt == fileio.FMT_CAP and bytes(loaded.data) == body


@pytest.mark.parametrize("writer,fmt", [(fileio.format_ihex, fileio.FMT_IHEX),
                                        (fileio.format_srec, fileio.FMT_SREC)])
def test_eep_autodetect(tmp_path, writer, fmt):
    payload = data(512, 2)
    path = tmp_path / "eeprom.eep"
    path.write_text(writer(payload))
    loaded = fileio.load_image(path)
    assert loaded.fmt == fmt and bytes(loaded.data) == payload


def test_eep_binary(tmp_path):
    payload = b"\x00\x01binary"
    path = tmp_path / "eeprom.eep"
    path.write_bytes(payload)
    assert bytes(fileio.load_image(path).data) == payload


def test_ihex_gaps_and_segments(tmp_path):
    text = "\n".join([
        ":020000040001F9",          # upper address 0x0001xxxx
        ":0400100001020304E2",      # 4 bytes at 0x10010
        ":00000001FF",
    ])
    image, base = fileio.parse_ihex(text)
    assert base == 0x10010
    assert len(image) == 0x10014
    assert image[0x10010:0x10014] == b"\x01\x02\x03\x04"
    assert image[:0x10010] == b"\xFF" * 0x10010


def test_ihex_checksum_error():
    with pytest.raises(fileio.ImageFormatError, match="checksum"):
        fileio.parse_ihex(":0400100001020304E3")


def test_srec_checksum_error():
    good = fileio.format_srec(b"\x01\x02").splitlines()[1]
    bad = good[:-2] + ("00" if good[-2:] != "00" else "01")
    with pytest.raises(fileio.ImageFormatError, match="checksum"):
        fileio.parse_srec(bad)
