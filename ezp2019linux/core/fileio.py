"""Loading and saving buffer images.

Supports the formats the vendor software handles: raw binary (``.bin``,
``.rom`` and anything else), Intel HEX (``.hex``), ASUS BIOS capsules
(``.cap``, whose 2 KiB header is skipped) and ``.eep`` files that may be
Intel HEX, Motorola S-record or binary.  S-records (``.srec``, ``.s19``, ...)
can be read and written as well.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MAX_IMAGE_SIZE = 64 * 1024 * 1024       # the vendor tool's buffer size
CAP_HEADER_SIZE = 0x800

FMT_BIN = "bin"
FMT_IHEX = "ihex"
FMT_SREC = "srec"
FMT_CAP = "cap"

_SREC_EXT = {".srec", ".s19", ".s28", ".s37", ".mot", ".mhx"}
_IHEX_EXT = {".hex", ".ihex", ".ihx"}

OPEN_FILTERS = ("All supported (*.bin *.rom *.hex *.ihex *.cap *.eep *.srec *.s19 *.s28 *.s37 *.mot);;"
                "Binary (*.bin *.rom);;Intel HEX (*.hex *.ihex);;ASUS BIOS capsule (*.cap);;"
                "EEPROM file (*.eep);;Motorola S-record (*.srec *.s19 *.s28 *.s37 *.mot);;"
                "All files (*)")
SAVE_FILTERS = ("Binary (*.bin);;Intel HEX (*.hex);;Motorola S-record (*.srec);;"
                "BIOS ROM (*.rom);;All files (*)")


class ImageFormatError(ValueError):
    pass


@dataclass
class LoadedImage:
    data: bytearray
    fmt: str
    base_address: int = 0
    path: str = ""

    @property
    def length(self) -> int:
        return len(self.data)

    @property
    def format_label(self) -> str:
        return {FMT_BIN: "binary", FMT_IHEX: "Intel HEX", FMT_SREC: "Motorola S-record",
                FMT_CAP: "ASUS capsule"}[self.fmt]


def detect_format(path: str | os.PathLike, head: bytes) -> str:
    ext = Path(path).suffix.lower()
    if ext in _IHEX_EXT:
        return FMT_IHEX
    if ext in _SREC_EXT:
        return FMT_SREC
    if ext == ".cap":
        return FMT_CAP
    if ext == ".eep":
        first = head.lstrip()[:1]
        if first == b":":
            return FMT_IHEX
        if first in (b"S", b"s"):
            return FMT_SREC
    return FMT_BIN


def _place(image: bytearray, address: int, payload: bytes, lineno: int) -> None:
    end = address + len(payload)
    if end > MAX_IMAGE_SIZE:
        raise ImageFormatError(f"line {lineno}: address 0x{address:X} is beyond the "
                               f"{MAX_IMAGE_SIZE >> 20} MB buffer")
    if end > len(image):
        image.extend(b"\xFF" * (end - len(image)))
    image[address:end] = payload


def parse_ihex(text: str) -> tuple[bytearray, int]:
    image = bytearray()
    base = 0
    lowest = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ImageFormatError(f"line {lineno}: not an Intel HEX record")
        try:
            rec = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise ImageFormatError(f"line {lineno}: invalid hex digits") from exc
        if len(rec) < 5 or len(rec) != rec[0] + 5:
            raise ImageFormatError(f"line {lineno}: bad record length")
        if sum(rec) & 0xFF:
            raise ImageFormatError(f"line {lineno}: checksum error")
        addr, rtype, payload = (rec[1] << 8) | rec[2], rec[3], rec[4:4 + rec[0]]
        if rtype == 0x00:
            address = base + addr
            lowest = address if lowest is None else min(lowest, address)
            _place(image, address, payload, lineno)
        elif rtype == 0x01:
            break
        elif rtype == 0x02:
            base = int.from_bytes(payload, "big") << 4
        elif rtype == 0x04:
            base = int.from_bytes(payload, "big") << 16
        elif rtype in (0x03, 0x05):
            continue
        else:
            raise ImageFormatError(f"line {lineno}: unknown record type {rtype:02X}")
    return image, lowest or 0


def parse_srec(text: str) -> tuple[bytearray, int]:
    image = bytearray()
    lowest = None
    addr_len = {"1": 2, "2": 3, "3": 4}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if len(line) < 4 or line[0] not in "Ss":
            raise ImageFormatError(f"line {lineno}: not an S-record")
        kind = line[1]
        try:
            rec = bytes.fromhex(line[2:])
        except ValueError as exc:
            raise ImageFormatError(f"line {lineno}: invalid hex digits") from exc
        if not rec or len(rec) != rec[0] + 1:
            raise ImageFormatError(f"line {lineno}: bad record length")
        if (sum(rec) & 0xFF) != 0xFF:
            raise ImageFormatError(f"line {lineno}: checksum error")
        if kind in addr_len:
            n = addr_len[kind]
            address = int.from_bytes(rec[1:1 + n], "big")
            payload = rec[1 + n:-1]
            lowest = address if lowest is None else min(lowest, address)
            _place(image, address, payload, lineno)
        elif kind in "0456789":
            continue
        else:
            raise ImageFormatError(f"line {lineno}: unknown record type S{kind}")
    return image, lowest or 0


def load_image(path: str | os.PathLike) -> LoadedImage:
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as fh:
        head = fh.read(256)
    fmt = detect_format(path, head)
    if fmt in (FMT_IHEX, FMT_SREC):
        if size > 4 * MAX_IMAGE_SIZE:
            raise ImageFormatError("File is too big for the buffer.")
        text = path.read_text(encoding="ascii", errors="replace")
        data, base = parse_ihex(text) if fmt == FMT_IHEX else parse_srec(text)
        return LoadedImage(data, fmt, base, str(path))
    if fmt == FMT_CAP:
        if size <= CAP_HEADER_SIZE:
            raise ImageFormatError("The capsule file is too small to contain a BIOS image.")
        size -= CAP_HEADER_SIZE
    if size > MAX_IMAGE_SIZE:
        raise ImageFormatError("File is too big for the buffer.")
    with open(path, "rb") as fh:
        if fmt == FMT_CAP:
            fh.seek(CAP_HEADER_SIZE)
        data = bytearray(fh.read())
    return LoadedImage(data, fmt, 0, str(path))


def iter_ihex(data: bytes | bytearray | memoryview, record_len: int = 16):
    """Intel HEX lines for ``data`` placed at address 0."""
    view = memoryview(data)
    upper = -1
    for off in range(0, len(view), record_len):
        if off >> 16 != upper:
            upper = off >> 16
            rec = bytes([2, 0, 0, 4]) + upper.to_bytes(2, "big")
            yield ":" + (rec + bytes([-sum(rec) & 0xFF])).hex().upper()
        chunk = bytes(view[off:off + record_len])
        rec = bytes([len(chunk)]) + (off & 0xFFFF).to_bytes(2, "big") + b"\x00" + chunk
        yield ":" + (rec + bytes([-sum(rec) & 0xFF])).hex().upper()
    yield ":00000001FF"


def iter_srec(data: bytes | bytearray | memoryview, record_len: int = 32,
              header: bytes = b"ezp2019linux"):
    """Motorola S-record lines for ``data`` placed at address 0."""
    view = memoryview(data)
    size = len(view)
    if size <= 0x10000:
        kind, alen, term = "1", 2, "9"
    elif size <= 0x1000000:
        kind, alen, term = "2", 3, "8"
    else:
        kind, alen, term = "3", 4, "7"

    def record(t: str, address: int, payload: bytes, n: int) -> str:
        body = bytes([n + len(payload) + 1]) + address.to_bytes(n, "big") + payload
        return f"S{t}" + (body + bytes([~sum(body) & 0xFF])).hex().upper()

    yield record("0", 0, header, 2)
    for off in range(0, size, record_len):
        yield record(kind, off, bytes(view[off:off + record_len]), alen)
    yield record(term, 0, b"", alen)


def format_ihex(data: bytes | bytearray | memoryview) -> str:
    return "\n".join(iter_ihex(data)) + "\n"


def format_srec(data: bytes | bytearray | memoryview) -> str:
    return "\n".join(iter_srec(data)) + "\n"


def save_image(path: str | os.PathLike, data: bytes | bytearray | memoryview,
               fmt: str | None = None) -> str:
    """Write ``data`` to ``path``; the format follows the extension unless given."""
    path = Path(path)
    if fmt is None:
        ext = path.suffix.lower()
        fmt = FMT_IHEX if ext in _IHEX_EXT else FMT_SREC if ext in _SREC_EXT else FMT_BIN
    tmp = path.with_name(path.name + ".tmp")
    try:
        if fmt in (FMT_IHEX, FMT_SREC):
            lines = iter_ihex(data) if fmt == FMT_IHEX else iter_srec(data)
            with open(tmp, "w", encoding="ascii", newline="\n") as fh:
                for line in lines:
                    fh.write(line)
                    fh.write("\n")
        else:
            with open(tmp, "wb") as fh:
                fh.write(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return fmt
