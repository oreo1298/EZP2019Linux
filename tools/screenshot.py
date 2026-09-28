#!/usr/bin/env python3
"""Render the GUI offscreen with the virtual programmer and save screenshots.

Usage: tools/screenshot.py OUTPUT_DIR [--theme dark|light]
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp(prefix="ezp-shot-"))

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ezp2019linux.core.chipdb import ChipDatabase  # noqa: E402
from ezp2019linux.core.programmer import Programmer  # noqa: E402
from ezp2019linux.core.simulator import SimConnector, Timing, VirtualProgrammer  # noqa: E402
from ezp2019linux.gui.main_window import MainWindow  # noqa: E402
from ezp2019linux.gui.theme import theme  # noqa: E402


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


def wait_idle(win: MainWindow, timeout: float = 60) -> None:
    end = time.monotonic() + timeout
    while win.runner.busy and time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    pump(0.3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--theme", default="dark")
    ap.add_argument("--size", default="1440x900")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    w, h = (int(v) for v in args.size.split("x"))

    import ezp2019linux.gui.main_window as mw
    mw.ask_yes_no = lambda *a, **k: True
    from PySide6.QtWidgets import QMessageBox
    for name in ("information", "warning", "critical", "question"):
        setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))

    app = QApplication([])
    theme.apply(app, args.theme)
    db = ChipDatabase.load(custom_path=None)
    device = VirtualProgrammer(timing=Timing(erase_polls=3))
    chip = db.find("WINBOND:W25Q64")[0]
    image = bytearray(os.urandom(0x2000)) + bytearray(b"\xFF" * (chip.size - 0x2000))
    image[0x40:0x70] = b"EZP2019+ Programmer for Linux - demo firmware  "
    device.insert(chip, contents=bytes(image))
    win = MainWindow(Programmer(SimConnector(device)), db, device)
    win.resize(w, h)
    win.show()
    pump(0.6)

    win.detect_chip()
    wait_idle(win)
    win.read_chip()
    wait_idle(win)
    win.toast.hide()
    win.hex.goto(0x40, 0x2F)
    pump(0.3)
    win.grab().save(str(args.out / f"main-{args.theme}.png"))

    # a verify failure with highlighted differences
    win.doc.write(0x80, b"\x00\x11\x22\x33")
    win.hex.set_selection(0, 0)
    device.memory[0x1000] ^= 0xFF
    win.verify_chip()
    wait_idle(win)
    win.toast.hide()
    win.hex.goto(0x80)
    pump(0.3)
    win.grab().save(str(args.out / f"verify-{args.theme}.png"))

    # progress state mid-operation
    device.timing = Timing(read_bytes_per_s=400_000)
    win.read_chip()
    pump(1.5)
    win.grab().save(str(args.out / f"progress-{args.theme}.png"))
    win.cancel_operation()
    wait_idle(win)
    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
