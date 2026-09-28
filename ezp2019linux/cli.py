"""Command line interface.

Without a command the graphical interface starts; every programmer
operation is also available as a sub-command for scripting.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__
from .core import fileio
from .core import protocol as P
from .core.chipdb import TYPE_LABELS, Chip, ChipDatabase, format_size
from .core.errors import ProgrammerError
from .core.programmer import (DetectResult, Programmer, verify_length, write_length)
from .core.simulator import SimConnector, Timing, VirtualProgrammer
from .core.transport import UsbConnector

SPINNER = "|/-\\"
CLOCK_CHOICES = {"12mhz": 0, "6mhz": 1, "3mhz": 2, "1.5mhz": 3, "750khz": 4, "375khz": 5}


def parse_clock(text: str) -> int:
    key = text.lower().replace(" ", "")
    if key in CLOCK_CHOICES:
        return CLOCK_CHOICES[key]
    if key.isdigit() and 0 <= int(key) <= 5:
        return int(key)
    raise argparse.ArgumentTypeError(
        f"unknown clock {text!r}; choose from {', '.join(P.CLOCK_LABELS)}")


class Progress:
    """A single-line progress display on stderr."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet or not sys.stderr.isatty()
        self.stage = None
        self.t0 = time.monotonic()
        self.last = 0.0

    def __call__(self, stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        if stage != self.stage:
            self.finish()
            self.stage, self.t0 = stage, now
        if self.quiet or (now - self.last < 0.1 and done < total):
            return
        self.last = now
        elapsed = now - self.t0
        if total:
            pct = 100.0 * done / total
            width = 30
            fill = int(width * done / total)
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            line = (f"\r{stage:<14} [{'#' * fill}{'.' * (width - fill)}] {pct:5.1f}%  "
                    f"{rate / 1024:7.1f} KB/s  ETA {eta:4.0f}s")
        else:
            spin = SPINNER[int(elapsed * 4) % 4]
            line = f"\r{stage:<14} {spin}  {elapsed:5.1f}s"
        sys.stderr.write(line)
        sys.stderr.flush()

    def finish(self) -> None:
        if self.stage and not self.quiet:
            sys.stderr.write(f"\r{self.stage:<14} done in {time.monotonic() - self.t0:.1f}s"
                             + " " * 40 + "\n")
        self.stage = None


def build_programmer(args, db: ChipDatabase) -> Programmer:
    trace = (lambda line: print(f"[usb] {line}", file=sys.stderr)) if args.debug else None
    if args.simulator:
        device = VirtualProgrammer(timing=Timing(erase_polls=5))
        spec = args.sim_chip
        if spec.lower() != "empty":
            chips = db.find(spec)
            if not chips:
                raise SystemExit(f"simulator: unknown chip {spec!r}")
            device.insert(chips[0], pattern="random")
        connector = SimConnector(device)
    else:
        connector = UsbConnector()
    return Programmer(connector, trace=trace, presence_check=not args.no_check)


def resolve_chip(args, db: ChipDatabase, prog: Programmer) -> Chip:
    spec = getattr(args, "chip", None)
    if spec is None or spec.lower() == "auto":
        result = prog.detect(clock=args.clock, db=db)
        if result.chip is None:
            raise SystemExit(f"Could not identify the chip: {result.describe()}\n"
                             "Select it with --chip (see 'ezp2019linux chips').")
        print(f"Detected {result.chip.label} (ID {result.id_label})", file=sys.stderr)
        if len(result.matches) > 1:
            others = ", ".join(c.label for c in result.matches[1:])
            print(f"  also matches: {others}", file=sys.stderr)
        return result.chip
    matches = db.find(spec)
    if not matches:
        hints = ", ".join(f"{c.manufacturer}:{c.name}" for c in db.search(spec, limit=8))
        raise SystemExit(f"Unknown chip {spec!r}." + (f" Did you mean: {hints}?" if hints else ""))

    def params(c: Chip):
        return (c.chip_class, c.size, c.page_size, c.algorithm, c.delay, c.voltage)

    if len({params(c) for c in matches}) > 1:
        options = "\n  ".join(f"{c.type}:{c.manufacturer}:{c.name}" for c in matches[:20])
        raise SystemExit(f"{spec!r} is ambiguous; use one of:\n  {options}")
    return matches[0]


def describe_chip(chip: Chip) -> str:
    return (f"{chip.label} — {chip.type_label}, {format_size(chip.size)}, page {chip.page_size} B, "
            f"{chip.supply_label}{' (1.8 V adapter)' if chip.is_1v8 else ''}, {chip.algorithm_label}"
            + (f", ID {chip.jedec_id_label}" if chip.chip_id else ""))


def load_file(path: Path, chip: Chip, force: bool) -> bytes:
    image = fileio.load_image(path)
    data = bytes(image.data)
    if len(data) > chip.size:
        if not force:
            raise SystemExit(f"{path} is {len(data)} bytes, larger than the {chip.size}-byte "
                             "chip. Use --force to write only the first part.")
        data = data[:chip.size]
    return data


# -- commands -----------------------------------------------------------------


def cmd_list(args, db, prog) -> int:
    devices = prog.devices()
    if not devices:
        print("No EZP2019+ programmer found.")
        return 1
    for d in devices:
        print(f"{d.model}  {d.usb_id}  {d.location}  firmware {d.firmware}")
    return 0


def cmd_info(args, db, prog) -> int:
    info = prog.describe()
    rows = [("Model", info.model), ("USB ID", info.usb_id), ("Location", info.location),
            ("Manufacturer", info.manufacturer or "?"), ("Product", info.product or "?"),
            ("Serial", info.serial or "—"), ("Firmware", info.firmware),
            ("Interface", info.variant.description if info.variant else "?")]
    for key, value in rows:
        print(f"{key:<13} {value}")
    return 0


def cmd_detect(args, db, prog) -> int:
    result: DetectResult = prog.detect(clock=args.clock, db=db)
    print(result.describe())
    for chip in result.matches:
        print("  " + describe_chip(chip))
    return 0 if result.present else 1


def cmd_chips(args, db, prog) -> int:
    chips = db.search(args.query or "", chip_type=args.type)
    for c in chips:
        ident = c.jedec_id_label if c.chip_id else ""
        print(f"{c.type:<10} {c.manufacturer:<12} {c.name:<24} {format_size(c.size):>7} "
              f"{c.page_size:>4} B  {c.supply_label:<6} {ident}")
    print(f"{len(chips)} chip(s)", file=sys.stderr)
    return 0


def cmd_read(args, db, prog) -> int:
    chip = resolve_chip(args, db, prog)
    print(describe_chip(chip), file=sys.stderr)
    progress = Progress(args.quiet)
    data = prog.read(chip, clock=args.clock, progress=progress)
    progress.finish()
    fmt = fileio.save_image(args.file, data)
    from .core.programmer import crc32
    print(f"Read {len(data)} bytes to {args.file} ({fmt}), CRC32 {crc32(data):08X}")
    return 0


def _auto(args, db, prog, erase: bool, verify: bool) -> int:
    chip = resolve_chip(args, db, prog)
    print(describe_chip(chip), file=sys.stderr)
    data = load_file(Path(args.file), chip, args.force)
    wlen = write_length(chip, data, len(data))
    vlen = verify_length(chip, data, len(data))
    if wlen == 0:
        raise SystemExit("The file contains no data to write (all bytes are 0xFF).")
    if chip.is_spi_flash and not erase:
        print("Note: SPI flash must be erased before writing (use --erase).", file=sys.stderr)
    progress = Progress(args.quiet)
    result = prog.auto(chip, data, write_len=wlen, verify_len=vlen, clock=args.clock,
                       erase=erase, program=True, verify=verify, progress=progress)
    progress.finish()
    print(f"Wrote {result.written} bytes.")
    if result.verify is not None:
        return report_verify(result.verify)
    return 0


def report_verify(result) -> int:
    if result.ok:
        print(f"Verify OK ({result.length} bytes).")
        return 0
    print(f"Verify FAILED: {result.mismatches} byte(s) differ, first at "
          f"0x{result.first_mismatch:08X}.")
    return 2


def cmd_write(args, db, prog) -> int:
    return _auto(args, db, prog, erase=args.erase, verify=args.verify)


def cmd_auto(args, db, prog) -> int:
    return _auto(args, db, prog, erase=not args.no_erase, verify=not args.no_verify)


def cmd_erase(args, db, prog) -> int:
    chip = resolve_chip(args, db, prog)
    print(describe_chip(chip), file=sys.stderr)
    if not args.yes and sys.stdin.isatty():
        answer = input(f"Erase the entire {chip.label}? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return 1
    progress = Progress(args.quiet)
    prog.erase(chip, clock=args.clock, progress=progress)
    progress.finish()
    print("Erase complete.")
    return 0


def cmd_verify(args, db, prog) -> int:
    chip = resolve_chip(args, db, prog)
    data = load_file(Path(args.file), chip, args.force)
    progress = Progress(args.quiet)
    result = prog.verify(chip, data, length=verify_length(chip, data, len(data)),
                         clock=args.clock, progress=progress)
    progress.finish()
    return report_verify(result)


def cmd_blank(args, db, prog) -> int:
    chip = resolve_chip(args, db, prog)
    progress = Progress(args.quiet)
    result = prog.blank_check(chip, clock=args.clock, progress=progress)
    progress.finish()
    if result.blank:
        print("The chip is blank.")
        return 0
    print(f"The chip is not blank: data at 0x{result.first_data:08X}.")
    return 2


def cmd_install_udev(args, db, prog) -> int:
    from .core.system import install_udev_rule
    ok, message = install_udev_rule()
    print(message)
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ezp2019linux",
        description="Read, write, erase and verify EEPROM/flash chips with an EZP2019+ "
                    "USB programmer. Run without a command to open the graphical interface.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--simulator", action="store_true",
                    help="use a virtual programmer instead of real hardware")
    ap.add_argument("--sim-chip", metavar="CHIP", default="W25Q64",
                    help="chip in the virtual programmer's socket (default W25Q64, "
                         "'empty' for none)")
    ap.add_argument("--debug", action="store_true", help="print every USB packet")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    def op(name, help_text, func, file_arg=None, chip_required=False):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("-c", "--chip", required=chip_required,
                       help="chip name, e.g. W25Q64, WINBOND:W25Q64, 24C02 "
                            "(default: auto-detect SPI flash)")
        p.add_argument("-s", "--speed", dest="clock", type=parse_clock,
                       default=P.DEFAULT_CLOCK, help="SPI clock (default 6MHz)")
        p.add_argument("--no-check", action="store_true",
                       help="skip the chip presence check")
        p.add_argument("-q", "--quiet", action="store_true", help="no progress display")
        if file_arg:
            p.add_argument("file", help=file_arg)
        p.set_defaults(func=func)
        return p

    sub.add_parser("gui", help="open the graphical interface (default)")
    p = sub.add_parser("list", help="list connected programmers")
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("info", help="show programmer details")
    p.set_defaults(func=cmd_info)
    p = op("detect", "identify the chip in the socket", cmd_detect)
    p = sub.add_parser("chips", help="search the chip database")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("-t", "--type", choices=list(TYPE_LABELS))
    p.set_defaults(func=cmd_chips)
    op("read", "read the chip into a file", cmd_read, "output file (.bin/.hex/.srec)")
    p = op("write", "write a file to the chip", cmd_write, "input file")
    p.add_argument("-e", "--erase", action="store_true", help="erase first")
    p.add_argument("-v", "--verify", action="store_true", help="verify afterwards")
    p.add_argument("-f", "--force", action="store_true",
                   help="truncate files larger than the chip")
    p = op("auto", "erase, write and verify (like the Auto button)", cmd_auto, "input file")
    p.add_argument("--no-erase", action="store_true")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("-f", "--force", action="store_true",
                   help="truncate files larger than the chip")
    p = op("erase", "erase the chip", cmd_erase)
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p = op("verify", "compare the chip with a file", cmd_verify, "file to compare")
    p.add_argument("-f", "--force", action="store_true",
                   help="compare only the first chip-size bytes of larger files")
    op("blank", "check that the chip is erased", cmd_blank)
    p = sub.add_parser("install-udev", help="install the udev rule for non-root access")
    p.set_defaults(func=cmd_install_udev)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if os.environ.get("EZP2019LINUX_SIMULATOR"):
        args.simulator = True
    if args.command in (None, "gui"):
        from .gui.app import run_gui
        return run_gui(simulator=args.simulator, sim_chip=args.sim_chip, debug=args.debug)
    for attr, default in (("clock", P.DEFAULT_CLOCK), ("no_check", False)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    db = ChipDatabase.load()
    try:
        prog = build_programmer(args, db)
        return args.func(args, db, prog)
    except ProgrammerError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except fileio.ImageFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
