"""GUI entry point."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication

from .. import APP_ID, APP_NAME, __version__
from ..core.chipdb import ChipDatabase
from ..core.programmer import Programmer
from ..core.simulator import SimConnector, Timing, VirtualProgrammer
from ..core.transport import UsbConnector
from .main_window import MainWindow
from .theme import theme

ICON_PATH = Path(__file__).resolve().parent.parent / "data" / "ezp2019linux.svg"


def app_icon() -> QIcon:
    themed = QIcon.fromTheme(APP_ID)
    if not themed.isNull():
        return themed
    return QIcon(str(ICON_PATH))


def make_simulator(db: ChipDatabase, spec: str) -> VirtualProgrammer:
    # Paced roughly like the real hardware so the demo feels realistic.
    device = VirtualProgrammer(timing=Timing(read_bytes_per_s=950_000,
                                             write_bytes_per_s=260_000, erase_polls=30))
    if spec and spec.lower() != "empty":
        chips = db.find(spec)
        if chips:
            device.insert(chips[0], pattern="random")
    return device


def run_gui(simulator: bool = False, sim_chip: str = "W25Q64", debug: bool = False,
            argv: list[str] | None = None) -> int:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QApplication.setApplicationName(APP_ID)
    QApplication.setApplicationDisplayName(APP_NAME)
    QApplication.setApplicationVersion(__version__)
    QApplication.setOrganizationName(APP_ID)
    QGuiApplication.setDesktopFileName(APP_ID)
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setWindowIcon(app_icon())

    settings = QSettings(APP_ID, APP_ID)
    mode = os.environ.get("EZP2019LINUX_THEME") or str(settings.value("theme", "system"))
    theme.apply(app, mode)
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(
            lambda _s: theme.apply(app) if theme.mode == "system" else None)

    db = ChipDatabase.load()
    device = make_simulator(db, sim_chip) if simulator else None
    connector = SimConnector(device) if device is not None else UsbConnector()
    trace = (lambda line: print(f"[usb] {line}", file=sys.stderr)) if debug else None
    programmer = Programmer(connector, trace=trace)

    window = MainWindow(programmer, db, device)
    window.show()
    return app.exec()
