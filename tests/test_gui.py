import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import ezp2019linux.gui.main_window as mw  # noqa: E402
from ezp2019linux.core import fileio  # noqa: E402
from ezp2019linux.core.chipdb import ChipDatabase  # noqa: E402
from ezp2019linux.core.programmer import Programmer  # noqa: E402
from ezp2019linux.core.simulator import SimConnector, Timing, VirtualProgrammer  # noqa: E402
from ezp2019linux.gui.theme import theme  # noqa: E402


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    theme.apply(application, "dark")
    return application


@pytest.fixture(autouse=True)
def no_modal(monkeypatch):
    monkeypatch.setattr(mw, "ask_yes_no", lambda *a, **k: True)
    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))


@pytest.fixture(scope="module")
def db():
    return ChipDatabase.load(custom_path=None)


def pump(seconds=0.05):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def wait_idle(win, timeout=30):
    end = time.monotonic() + timeout
    while win.runner.busy:
        assert time.monotonic() < end, "operation did not finish"
        QCoreApplication.processEvents()
        time.sleep(0.005)
    pump(0.05)


@pytest.fixture
def setup(app, db):
    device = VirtualProgrammer(timing=Timing(erase_polls=2))
    chip = db.find("WINBOND:W25Q16")[0]
    device.insert(chip, pattern="random")
    win = mw.MainWindow(Programmer(SimConnector(device)), db, device)
    win.show()
    pump(0.1)
    win.select_chip(chip)
    yield win, device, chip
    win.doc.dirty = False
    win.close()


def logs(win):
    return "\n".join(m for _t, _l, m in win._log_entries)


def test_device_detected(setup):
    win, device, chip = setup
    win._poll_device()
    assert win.device is not None and win.device.simulated
    assert "connected" in win.conn_text.text().lower()


def test_read(setup):
    win, device, chip = setup
    win.read_chip()
    wait_idle(win)
    assert bytes(win.doc.data) == bytes(device.memory)
    assert win.doc.data_length == chip.size
    assert "Read complete" in logs(win)


def test_edit_write_verify(setup):
    win, device, chip = setup
    win.read_chip()
    wait_idle(win)
    win.hex.setFocus()
    win.hex.goto(0x10)
    for ch in "AB":
        win.hex.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, 0, Qt.NoModifier, ch))
    assert win.doc.data[0x10] == 0xAB
    assert win.doc.is_modified(0x10)

    # erase + write + verify through Auto
    win.auto_program()
    wait_idle(win)
    assert device.memory[0x10] == 0xAB
    assert bytes(device.memory) == bytes(win.doc.data)
    assert "Auto complete" in logs(win)
    assert device.violations == []


def test_verify_mismatch_highlight(setup):
    win, device, chip = setup
    win.read_chip()
    wait_idle(win)
    device.memory[0x2345] ^= 0x5A
    win.verify_chip()
    wait_idle(win)
    assert 0x2345 in win.doc.mismatches
    assert win.hex.cursor == 0x2345
    assert "Verify failed" in logs(win)


def test_detect_selects_chip(setup, db):
    win, device, chip = setup
    device.insert(db.find("WINBOND:W25Q64")[0])
    win.detect_chip()
    wait_idle(win)
    assert win.chip.name == "W25Q64"


def test_detect_ambiguous_id_asks(setup, db, monkeypatch):
    win, device, chip = setup
    shared = [c for c in db.by_jedec_id(0xC22018)]
    assert len(shared) > 1
    device.insert(shared[0])
    picked = {}

    class FakePicker:
        def __init__(self, parent, db, *a, chips=None, **k):
            picked["chips"] = chips
            self.selected = chips[2]

        def exec(self):
            return True

    monkeypatch.setattr(mw, "ChipPickerDialog", FakePicker)
    win.detect_chip()
    wait_idle(win)
    assert picked["chips"] == shared
    assert win.chip == shared[2]


def test_detect_eeprom_family(setup, db):
    win, device, chip = setup
    device.insert(db.find("MICROCHIP:24C02")[0])
    win.detect_chip()
    wait_idle(win)
    assert win.type_seg.value() == "24_EEPROM"


def test_cancel_read(setup):
    win, device, chip = setup
    device.timing = Timing(read_bytes_per_s=200_000)
    before = bytes(win.doc.data)
    win.read_chip()
    pump(0.3)
    win.cancel_operation()
    wait_idle(win)
    assert bytes(win.doc.data) == before
    assert "cancelled" in logs(win).lower()
    device.timing = Timing()
    win.blank_check()
    wait_idle(win)
    assert device.violations == []


def test_open_file_and_write_eeprom(setup, db, tmp_path):
    win, device, chip = setup
    eeprom = db.find("MICROCHIP:24C02")[0]
    device.insert(eeprom)
    win.select_chip(eeprom)
    payload = bytes(range(256))
    path = tmp_path / "eeprom.hex"
    fileio.save_image(path, payload)
    win.open_file(str(path))
    assert bytes(win.doc.data[:256]) == payload
    win.write_chip()
    wait_idle(win)
    assert bytes(device.memory) == payload
    win.verify_chip()
    wait_idle(win)
    assert not win.doc.mismatches
    assert device.violations == []


def test_fill_and_find(setup):
    win, device, chip = setup
    win.doc.write(0x100, b"\xDE\xAD\xBE\xEF")
    win.find_mode.setCurrentText("Hex")
    win.find_edit.setText("de ad be ef")
    win.hex.goto(0)
    win.find_next(True)
    assert win.hex.cursor == 0x100
    assert win.hex.selection() == (0x100, 0x104)


def test_theme_switch(setup):
    win, device, chip = setup
    win.set_theme("light")
    assert theme.palette.name == "light"
    win.set_theme("dark")
    assert theme.palette.name == "dark"


def test_disconnect_reported(setup):
    win, device, chip = setup
    win._poll_device()
    device.connected = False
    win._poll_device()
    assert win.device is None
    assert "disconnected" in logs(win).lower()
    device.connected = True
    win._poll_device()
    assert win.device is not None
