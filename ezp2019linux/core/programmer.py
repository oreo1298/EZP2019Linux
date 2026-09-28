"""High level EZP2019+ operations: detect, read, erase, write, verify, blank check, auto.

Each public method follows the sequence the vendor software uses (see
docs/PROTOCOL.md): an optional presence check in its own USB session, then a
session that loads the chip parameters and performs the operation, always
finished with the "end" command.
"""

from __future__ import annotations

import time
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator

from . import protocol as P
from .chipdb import CLASS_24_EEPROM, CLASS_25_EEPROM, CLASS_SPI_FLASH, Chip, ChipDatabase
from .errors import NoChipError, OperationCancelled, ProgrammerError, UsbTransferError
from .transport import Connector, DeviceInfo, Transport, TraceFn, UsbConnector

ProgressFn = Callable[[str, int, int], None]   # stage, done, total (total 0 = unknown)
CancelFn = Callable[[], bool]

STAGE_DETECT = "Detecting"
STAGE_READ = "Reading"
STAGE_ERASE = "Erasing"
STAGE_WRITE = "Writing"
STAGE_VERIFY = "Verifying"
STAGE_BLANK = "Blank checking"

# Bytes per bulk request.  The vendor software moves one page per request;
# bigger requests keep the bus busy and are invisible to the device, which
# only ever sees 64-byte packets.
READ_REQUEST_BYTES = 4096
WRITE_REQUEST_BYTES = 4096
MAX_RECORDED_MISMATCHES = 65536


def round_up(value: int, step: int) -> int:
    step = max(1, step)
    return -(-value // step) * step


def used_length(data: bytes | bytearray | memoryview, limit: int | None = None) -> int:
    """Length of ``data`` without trailing 0xFF (erased) bytes."""
    view = memoryview(data)[:limit if limit is not None else len(data)]
    return len(bytes(view).rstrip(b"\xFF"))


def write_length(chip: Chip, buffer: bytes | bytearray, data_length: int) -> int:
    """How many bytes Write should program for the given buffer.

    Like the vendor tool the length is rounded up to whole pages.  For SPI
    flash trailing 0xFF bytes are skipped: an erased flash already holds them.
    """
    n = min(max(0, data_length), len(buffer), chip.size)
    if chip.chip_class == CLASS_SPI_FLASH:
        n = used_length(buffer, n)
    return min(round_up(n, chip.page_size or 1), chip.size)


def verify_length(chip: Chip, buffer: bytes | bytearray, data_length: int) -> int:
    """How many bytes Verify compares (the whole data range, 0xFF tail included)."""
    n = min(max(0, data_length), len(buffer), chip.size)
    return min(round_up(n, chip.page_size or 1), chip.size)


def crc32(data: bytes | bytearray | memoryview) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


@dataclass
class DetectResult:
    kind: int
    jedec_id: int = 0
    raw: bytes = b""
    matches: list[Chip] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return self.kind != P.DETECT_NONE

    @property
    def type_name(self) -> str | None:
        return P.DETECT_TYPES.get(self.kind)

    @property
    def id_label(self) -> str:
        return " ".join(f"{b:02X}" for b in self.jedec_id.to_bytes(3, "big"))

    @property
    def chip(self) -> Chip | None:
        return self.matches[0] if self.matches else None

    def describe(self) -> str:
        if not self.present:
            return "No chip detected."
        if self.kind == P.DETECT_SPI_FLASH:
            if self.matches:
                names = ", ".join(c.label for c in self.matches[:4])
                more = f" (+{len(self.matches) - 4} more)" if len(self.matches) > 4 else ""
                return f"SPI flash, ID {self.id_label}: {names}{more}"
            return f"SPI flash with unknown ID {self.id_label}"
        return {P.DETECT_24_EEPROM: "24xx I²C EEPROM (select the exact model)",
                P.DETECT_93_EEPROM: "93xx Microwire EEPROM (select the exact model)"}[self.kind]


@dataclass
class VerifyResult:
    length: int
    mismatches: int = 0
    first_mismatch: int | None = None
    offsets: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.mismatches == 0


@dataclass
class BlankResult:
    length: int
    first_data: int | None = None

    @property
    def blank(self) -> bool:
        return self.first_data is None


@dataclass
class AutoResult:
    erased: bool = False
    written: int = 0
    verify: VerifyResult | None = None

    @property
    def ok(self) -> bool:
        return self.verify is None or self.verify.ok


def erase_timeout(chip: Chip) -> float:
    """Seconds to wait for a chip erase.

    The vendor tool polls at most ``delay`` times (about 0.1 s per poll),
    which is shorter than the worst case erase time of big flashes; allow
    for the datasheet maximum as well.
    """
    return max(chip.delay * 0.1, 30.0 + 13.0 * chip.size / (1 << 20))


class Session:
    """One open connection to the programmer, as used by a single operation."""

    def __init__(self, transport: Transport, clock: int,
                 progress: ProgressFn | None = None, cancel: CancelFn | None = None):
        self.transport = transport
        self.packets = P.PacketBuilder(transport.variant.byteorder)
        self.clock = clock
        self._progress = progress
        self._cancel = cancel

    # -- helpers --------------------------------------------------------------

    def _check_cancel(self) -> None:
        if self._cancel is not None and self._cancel():
            raise OperationCancelled()

    def _report(self, stage: str, done: int, total: int) -> None:
        if self._progress is not None:
            self._progress(stage, done, total)

    def _command(self, packet: bytes, response: bool) -> bytes | None:
        reply = self.transport.command(packet, response)
        if response and (reply is None or len(reply) < 4):
            raise UsbTransferError("The programmer sent a short response.")
        return reply

    # -- protocol steps ---------------------------------------------------------

    def detect(self, chip: Chip | None) -> DetectResult:
        reply = self._command(self.packets.detect(chip, self.clock), True)
        kind = reply[0]
        jedec = int.from_bytes(reply[1:4], "big") if kind == P.DETECT_SPI_FLASH else 0
        return DetectResult(kind=kind, jedec_id=jedec, raw=bytes(reply))

    def setup(self, chip: Chip) -> None:
        self._command(self.packets.setup(chip, self.clock), True)

    def _prepare_stream(self, chip: Chip) -> None:
        if chip.uses_4byte_address:
            self._command(self.packets.spi(bytes([P.SPI_ENTER_4BYTE])), True)

    def _stream_in(self, chip: Chip, length: int, stage: str,
                   consume: Callable[[int, bytes], bool]) -> None:
        """Read ``length`` bytes from address 0, feeding chunks to ``consume``.

        ``consume(offset, data)`` returns False to stop early.
        """
        self._prepare_stream(chip)
        self._command(self.packets.start(0), True)
        # The vendor tool reads whole transfer chunks until it has ``length`` bytes.
        total = min(round_up(length, P.transfer_chunk(chip)), chip.size)
        request = _request_size(chip, READ_REQUEST_BYTES)
        done = 0
        self._report(stage, 0, length)
        while done < total:
            self._check_cancel()
            want = min(request, total - done)
            data = self.transport.read_data(want)
            if not data:
                raise UsbTransferError(f"The programmer stopped sending data at 0x{done:X}.")
            keep_going = consume(done, data)
            done += len(data)
            self._report(stage, min(done, length), length)
            if keep_going is False:
                return

    def read(self, chip: Chip, length: int | None = None) -> bytearray:
        length = chip.size if length is None else length
        out = bytearray(length)

        def consume(offset: int, data: bytes) -> bool:
            end = min(offset + len(data), length)
            out[offset:end] = data[:end - offset]
            return True

        self._stream_in(chip, length, STAGE_READ, consume)
        return out

    def verify(self, chip: Chip, expected: bytes | bytearray | memoryview) -> VerifyResult:
        expected = memoryview(expected)
        result = VerifyResult(length=len(expected))

        def consume(offset: int, data: bytes) -> bool:
            end = min(offset + len(data), len(expected))
            if end <= offset:
                return True
            want = expected[offset:end]
            got = data[:end - offset]
            if want != got:
                for i in range(end - offset):
                    if got[i] != want[i]:
                        result.mismatches += 1
                        if result.first_mismatch is None:
                            result.first_mismatch = offset + i
                        if len(result.offsets) < MAX_RECORDED_MISMATCHES:
                            result.offsets.append(offset + i)
            return True

        self._stream_in(chip, len(expected), STAGE_VERIFY, consume)
        return result

    def blank_check(self, chip: Chip) -> BlankResult:
        result = BlankResult(length=chip.size)

        def consume(offset: int, data: bytes) -> bool:
            stripped = data.lstrip(b"\xFF")
            if stripped:
                result.first_data = offset + len(data) - len(stripped)
                return False    # like the vendor tool: stop at the first data byte
            return True

        self._stream_in(chip, chip.size, STAGE_BLANK, consume)
        return result

    def write(self, chip: Chip, payload: bytes | bytearray | memoryview) -> None:
        payload = memoryview(payload)
        self._prepare_stream(chip)
        self._command(self.packets.start(0), False)
        request = _request_size(chip, WRITE_REQUEST_BYTES)
        total = len(payload)
        self._report(STAGE_WRITE, 0, total)
        for off in range(0, total, request):
            self._check_cancel()
            self.transport.write_data(payload[off:off + request])
            self._report(STAGE_WRITE, min(off + request, total), total)
        if chip.chip_class == CLASS_24_EEPROM:
            time.sleep(P.EEPROM24_WRITE_SETTLE_S)

    def erase(self, chip: Chip) -> None:
        if chip.chip_class == CLASS_SPI_FLASH:
            self._command(self.packets.erase(P.ERASE_CHIP), False)
            deadline = time.monotonic() + erase_timeout(chip)
            polls = 0
            self._report(STAGE_ERASE, 0, 0)
            while True:
                time.sleep(P.STATUS_POLL_INTERVAL_S)
                reply = self._command(self.packets.status(), True)
                polls += 1
                if not reply[0] & 0x01:
                    break
                if time.monotonic() > deadline:
                    raise ProgrammerError(
                        f"The chip erase did not finish within {erase_timeout(chip):.0f} s.")
                self._report(STAGE_ERASE, 0, 0)
            self._report(STAGE_ERASE, 1, 1)
            return

        arguments = P.erase_arguments(chip)
        step = chip.page_size or 1
        for i, argument in enumerate(arguments):
            self._check_cancel()
            self._command(self.packets.erase(argument), False)
            if chip.chip_class == CLASS_24_EEPROM or chip.chip_class == CLASS_25_EEPROM:
                self._report(STAGE_ERASE, min((i + 1) * step, chip.size), chip.size)
        self._report(STAGE_ERASE, 1, 1)


def _request_size(chip: Chip, preferred: int) -> int:
    chunk = P.transfer_chunk(chip)
    if chunk % 64:
        return chunk    # odd page size: one page per request, exactly like the vendor tool
    return max(chunk, preferred // chunk * chunk)


def pad_payload(chip: Chip, data: bytes | bytearray | memoryview, length: int) -> bytes:
    """``length`` bytes of data, padded with 0xFF to whole transfer chunks."""
    length = min(length, chip.size)
    total = min(round_up(length, P.transfer_chunk(chip)), chip.size)
    body = bytes(memoryview(data)[:min(length, len(data))])
    return body + b"\xFF" * (total - len(body))


class Programmer:
    """Entry point for all operations on an EZP2019+ (real or simulated)."""

    def __init__(self, connector: Connector | None = None, trace: TraceFn | None = None,
                 presence_check: bool = True):
        self.connector = connector or UsbConnector()
        self.trace = trace
        self.presence_check = presence_check

    # -- device discovery ---------------------------------------------------------

    def devices(self) -> list[DeviceInfo]:
        return self.connector.devices()

    def describe(self) -> DeviceInfo:
        return self.connector.describe()

    @contextmanager
    def session(self, clock: int = P.DEFAULT_CLOCK, progress: ProgressFn | None = None,
                cancel: CancelFn | None = None) -> Iterator[Session]:
        transport = self.connector.open()
        transport.trace = self.trace
        session = Session(transport, clock, progress, cancel)
        aborted = False
        try:
            yield session
        except BaseException:
            aborted = True
            raise
        finally:
            try:
                transport.command(session.packets.end(), False)
                if aborted:
                    # A cancelled or failed transfer can leave packets queued in the
                    # device; make sure the next session does not read them as replies.
                    transport.drain()
            except ProgrammerError:
                pass
            transport.close()

    # -- operations ---------------------------------------------------------------

    def detect(self, chip: Chip | None = None, clock: int = P.DEFAULT_CLOCK,
               db: ChipDatabase | None = None, retries: int = 1) -> DetectResult:
        """Identify the chip in the socket (retries once if nothing sensible came back)."""
        result = DetectResult(P.DETECT_NONE)
        for attempt in range(retries + 1):
            if attempt:
                time.sleep(0.05)
            with self.session(clock) as s:
                result = s.detect(chip)
            if db is not None and result.kind == P.DETECT_SPI_FLASH:
                result.matches = db.by_jedec_id(result.jedec_id)
            if result.present and (result.kind != P.DETECT_SPI_FLASH or result.matches):
                break
        return result

    def _require_chip(self, chip: Chip, clock: int) -> None:
        # The programmer cannot see 25xx EEPROMs, so the vendor tool skips this
        # check for them when reading and writing; we skip it for every operation.
        if not self.presence_check or chip.chip_class == CLASS_25_EEPROM:
            return
        if not self.detect(chip, clock).present:
            raise NoChipError()

    def read(self, chip: Chip, clock: int = P.DEFAULT_CLOCK, progress: ProgressFn | None = None,
             cancel: CancelFn | None = None) -> bytearray:
        self._require_chip(chip, clock)
        with self.session(clock, progress, cancel) as s:
            s.setup(chip)
            return s.read(chip)

    def erase(self, chip: Chip, clock: int = P.DEFAULT_CLOCK, progress: ProgressFn | None = None,
              cancel: CancelFn | None = None) -> None:
        self._require_chip(chip, clock)
        with self.session(clock, progress, cancel) as s:
            s.setup(chip)
            s.erase(chip)

    def write(self, chip: Chip, data: bytes | bytearray, length: int | None = None,
              clock: int = P.DEFAULT_CLOCK, progress: ProgressFn | None = None,
              cancel: CancelFn | None = None) -> int:
        """Program ``length`` bytes of ``data`` from address 0; returns bytes sent."""
        length = len(data) if length is None else length
        if length <= 0:
            raise ProgrammerError("Nothing to write: the buffer is empty.")
        payload = pad_payload(chip, data, length)
        self._require_chip(chip, clock)
        with self.session(clock, progress, cancel) as s:
            s.setup(chip)
            s.write(chip, payload)
        return len(payload)

    def verify(self, chip: Chip, data: bytes | bytearray, length: int | None = None,
               clock: int = P.DEFAULT_CLOCK, progress: ProgressFn | None = None,
               cancel: CancelFn | None = None) -> VerifyResult:
        expected = _expected(chip, data, length)
        self._require_chip(chip, clock)
        with self.session(clock, progress, cancel) as s:
            s.setup(chip)
            return s.verify(chip, expected)

    def blank_check(self, chip: Chip, clock: int = P.DEFAULT_CLOCK,
                    progress: ProgressFn | None = None,
                    cancel: CancelFn | None = None) -> BlankResult:
        self._require_chip(chip, clock)
        with self.session(clock, progress, cancel) as s:
            s.setup(chip)
            return s.blank_check(chip)

    def auto(self, chip: Chip, data: bytes | bytearray, write_len: int | None = None,
             verify_len: int | None = None, clock: int = P.DEFAULT_CLOCK,
             erase: bool = True, program: bool = True, verify: bool = True,
             progress: ProgressFn | None = None, cancel: CancelFn | None = None) -> AutoResult:
        """Erase, program and verify in one session, like the vendor "Auto" button."""
        write_len = len(data) if write_len is None else write_len
        if program and write_len <= 0:
            raise ProgrammerError("Nothing to write: the buffer is empty.")
        payload = pad_payload(chip, data, write_len) if program else b""
        expected = _expected(chip, data, verify_len if verify_len is not None else write_len)
        result = AutoResult()
        self._require_chip(chip, clock)
        with self.session(clock, progress, cancel) as s:
            s.setup(chip)
            if erase:
                s.erase(chip)
                result.erased = True
            if program:
                s.write(chip, payload)
                result.written = len(payload)
            if verify:
                time.sleep(P.AUTO_VERIFY_DELAY_S)
                result.verify = s.verify(chip, expected)
        return result


def _expected(chip: Chip, data: bytes | bytearray, length: int | None) -> bytes:
    length = min(len(data) if length is None else length, chip.size)
    body = bytes(memoryview(data)[:min(length, len(data))])
    return body + b"\xFF" * (length - len(body))
