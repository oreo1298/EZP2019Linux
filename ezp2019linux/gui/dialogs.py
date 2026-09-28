"""Dialogs: fill, chip picker, chip database editor and about."""

from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt,
                            QRegularExpression)
from PySide6.QtGui import QFont, QRegularExpressionValidator
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QComboBox,
                               QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QRadioButton, QSpinBox, QTableView,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..core.chipdb import (ALGORITHM_LABELS, TYPE_CLASSES, TYPE_LABELS, VOLTAGE_LABELS, Chip,
                           ChipDatabase, format_size, parse_dat)
from . import icons
from .theme import theme
from .widgets import Card, form_row, scaled_font

HEX_RE = QRegularExpression(r"^(0[xX])?[0-9A-Fa-f]{0,8}[hH]?$")


def parse_address(text: str) -> int | None:
    t = text.strip().lower().replace("_", "")
    if not t:
        return None
    if t.startswith("0x"):
        t = t[2:]
    if t.endswith("h"):
        t = t[:-1]
    try:
        return int(t, 16)
    except ValueError:
        return None


def primary(button: QPushButton) -> QPushButton:
    button.setProperty("variant", "primary")
    return button


# -- Fill -------------------------------------------------------------------------------------


class FillDialog(QDialog):
    """Fill a buffer range with a byte, a 16-bit word or random data."""

    def __init__(self, parent: QWidget, buffer_size: int, start: int = 0, end: int | None = None):
        super().__init__(parent)
        self.setWindowTitle("Fill buffer")
        self.buffer_size = buffer_size
        end = buffer_size - 1 if end is None else end
        digits = max(4, len(f"{max(0, buffer_size - 1):X}"))

        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        data = Card("Data")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.mode = QButtonGroup(self)
        self.r_byte = QRadioButton("Constant byte (8 bits)")
        self.r_word = QRadioButton("Constant word (16 bits)")
        self.r_rand = QRadioButton("Random data")
        self.r_byte.setChecked(True)
        for i, r in enumerate((self.r_byte, self.r_word, self.r_rand)):
            self.mode.addButton(r, i)
        self.byte_edit = QLineEdit("FF")
        self.byte_edit.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{1,2}")))
        self.word_edit = QLineEdit("FFFF")
        self.word_edit.setValidator(QRegularExpressionValidator(QRegularExpression("[0-9A-Fa-f]{1,4}")))
        for e in (self.byte_edit, self.word_edit):
            e.setMaximumWidth(90)
        grid.addWidget(self.r_byte, 0, 0)
        grid.addWidget(self.byte_edit, 0, 1)
        grid.addWidget(self.r_word, 1, 0)
        grid.addWidget(self.word_edit, 1, 1)
        grid.addWidget(self.r_rand, 2, 0)
        data.add_layout(grid)
        lay.addWidget(data)

        rng = Card("Range")
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        self.start_edit = QLineEdit(f"{start:0{digits}X}")
        self.end_edit = QLineEdit(f"{max(start, end):0{digits}X}")
        for e in (self.start_edit, self.end_edit):
            e.setValidator(QRegularExpressionValidator(HEX_RE))
        form_row(form, "Start address (hex)", self.start_edit, muted=False)
        form_row(form, "End address (hex, inclusive)", self.end_edit, muted=False)
        rng.add_layout(form)
        lay.addWidget(rng)

        self.error = QLabel()
        self.error.setStyleSheet(f"color: {theme.palette.danger};")
        self.error.hide()
        lay.addWidget(self.error)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        primary(buttons.button(QDialogButtonBox.Ok)).setText("Fill")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.result_range: tuple[int, int] = (0, 0)

    def _accept(self) -> None:
        start = parse_address(self.start_edit.text())
        end = parse_address(self.end_edit.text())
        if start is None or end is None or end < start or end >= self.buffer_size:
            self.error.setText(f"Enter a range inside the buffer (0 – {self.buffer_size - 1:X}).")
            self.error.show()
            return
        self.result_range = (start, end + 1)
        self.accept()

    def pattern(self, length: int) -> bytes:
        if self.r_rand.isChecked():
            return random.randbytes(length)
        if self.r_word.isChecked():
            word = int(self.word_edit.text() or "0", 16).to_bytes(2, "big")
            return (word * (length // 2 + 1))[:length]
        return bytes([int(self.byte_edit.text() or "0", 16)]) * length

    def describe(self) -> str:
        if self.r_rand.isChecked():
            return "random data"
        if self.r_word.isChecked():
            return f"word 0x{int(self.word_edit.text() or '0', 16):04X}"
        return f"byte 0x{int(self.byte_edit.text() or '0', 16):02X}"


# -- Chip table model ---------------------------------------------------------------------------


class ChipTableModel(QAbstractTableModel):
    HEADERS = ("Type", "Manufacturer", "Model", "Size", "Page", "Voltage", "Chip ID")

    def __init__(self, chips: list[Chip]):
        super().__init__()
        self.chips = chips
        self._bold = QFont()
        self._bold.setBold(True)

    def set_chips(self, chips: list[Chip]) -> None:
        self.beginResetModel()
        self.chips = chips
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.chips)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        c = self.chips[index.row()]
        col = index.column()
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return (TYPE_LABELS.get(c.type, c.type), c.manufacturer, c.name,
                    format_size(c.size), f"{c.page_size} B", c.supply_label,
                    c.jedec_id_label)[col]
        if role == Qt.UserRole:          # sort keys
            return (c.type, c.manufacturer.lower(), c.name.lower(), c.size, c.page_size,
                    c.supply_label, c.chip_id)[col]
        if role == Qt.UserRole + 1:      # search text
            return f"{c.name} {c.manufacturer} {c.type} {c.chip_id:06x}".lower()
        if role == Qt.FontRole and col == 2:
            return self._bold
        if role == Qt.TextAlignmentRole and col in (3, 4):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None


class _ChipFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.terms: list[str] = []
        self.chip_type: str | None = None
        self.setSortRole(Qt.UserRole)

    def set_query(self, text: str, chip_type: str | None) -> None:
        self.terms = text.lower().split()
        self.chip_type = chip_type
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent) -> bool:  # noqa: N802
        model: ChipTableModel = self.sourceModel()
        chip = model.chips[row]
        if self.chip_type and chip.type != self.chip_type:
            return False
        hay = model.data(model.index(row, 0), Qt.UserRole + 1)
        return all(t in hay for t in self.terms)


def _setup_table(view: QTableView) -> None:
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setSelectionMode(QAbstractItemView.SingleSelection)
    view.setEditTriggers(QAbstractItemView.NoEditTriggers)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.verticalHeader().hide()
    view.verticalHeader().setDefaultSectionSize(30)
    view.setSortingEnabled(True)
    header = view.horizontalHeader()
    header.setHighlightSections(False)
    header.setSectionResizeMode(QHeaderView.ResizeToContents)
    header.setSectionResizeMode(2, QHeaderView.Stretch)


class ChipPickerDialog(QDialog):
    """Search the whole chip list (the vendor tool's "Find" window)."""

    def __init__(self, parent: QWidget, db: ChipDatabase, query: str = "",
                 chips: list[Chip] | None = None, title: str = "Find chip",
                 message: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 560)
        self.selected: Chip | None = None

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        if message:
            msg = QLabel(message)
            msg.setWordWrap(True)
            lay.addWidget(msg)
        bar = QHBoxLayout()
        self.search = QLineEdit(query)
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("Model, manufacturer or JEDEC ID — e.g. W25Q64, 24C02, EF4017")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icons.icon("search", theme.palette.text_muted),
                              QLineEdit.LeadingPosition)
        self.type_box = QComboBox()
        self.type_box.addItem("All types", None)
        for t in db.types():
            self.type_box.addItem(TYPE_LABELS.get(t, t), t)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.type_box)
        lay.addLayout(bar)

        self.model = ChipTableModel(chips if chips is not None else db.chips)
        self.proxy = _ChipFilter()
        self.proxy.setSourceModel(self.model)
        self.view = QTableView()
        self.view.setModel(self.proxy)
        _setup_table(self.view)
        if chips is not None:
            self.view.setSortingEnabled(False)
        lay.addWidget(self.view, 1)

        foot = QHBoxLayout()
        self.count = QLabel()
        self.count.setObjectName("Muted")
        foot.addWidget(self.count, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = primary(buttons.button(QDialogButtonBox.Ok))
        self.ok.setText("Select")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        foot.addWidget(buttons)
        lay.addLayout(foot)

        self.search.textChanged.connect(self._refilter)
        self.type_box.currentIndexChanged.connect(self._refilter)
        self.view.doubleClicked.connect(lambda _i: self._accept())
        self.view.selectionModel().selectionChanged.connect(
            lambda *_: self.ok.setEnabled(bool(self.view.selectionModel().selectedRows())))
        self._refilter()
        self.search.setFocus()

    def _refilter(self) -> None:
        self.proxy.set_query(self.search.text(), self.type_box.currentData())
        n = self.proxy.rowCount()
        self.count.setText(f"{n} chip{'s' if n != 1 else ''}")
        if n:
            self.view.selectRow(0)
        self.ok.setEnabled(n > 0)

    def _accept(self) -> None:
        rows = self.view.selectionModel().selectedRows()
        if not rows:
            return
        src = self.proxy.mapToSource(rows[0])
        self.selected = self.model.chips[src.row()]
        self.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Down, Qt.Key_Up) and self.search.hasFocus():
            self.view.setFocus()
            self.view.keyPressEvent(event)
            return
        super().keyPressEvent(event)


# -- Chip database editor ----------------------------------------------------------------------

_SIZES = [64 << i for i in range(21)]           # 64 B … 64 MB, like the vendor editor
_PAGES = [1 << i for i in range(10)]            # 1 … 512


class ChipEditorDialog(QDialog):
    """Create and manage custom chip definitions (the vendor tool's chip editor)."""

    def __init__(self, parent: QWidget, db: ChipDatabase):
        super().__init__(parent)
        self.setWindowTitle("Chip database")
        self.resize(1040, 620)
        self.db = db
        self.custom: list[Chip] = db.custom_chips()
        self.changed = False
        self._editing: int | None = None

        lay = QHBoxLayout(self)
        lay.setSpacing(14)

        left = QVBoxLayout()
        intro = QLabel(f"<b>{len(db) - len(self.custom)}</b> built-in chips. Chips you add here "
                       "are stored in your profile and appear in every chip list.")
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        left.addWidget(intro)
        self.model = ChipTableModel(self.custom)
        self.view = QTableView()
        self.view.setModel(self.model)
        _setup_table(self.view)
        self.view.setSortingEnabled(False)
        left.addWidget(self.view, 1)
        self.empty = QLabel("No custom chips yet.\nFill in the form and click “Add chip”, or "
                            "import a .Dat database.", self.view.viewport())
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignCenter)
        self.model.modelReset.connect(self._update_empty)
        row = QHBoxLayout()
        self.b_import = QPushButton(theme.icon("import"), "Import .Dat…")
        self.b_export = QPushButton(theme.icon("export"), "Export .Dat…")
        self.b_delete = QPushButton(theme.icon("trash"), "Delete")
        self.b_delete.setProperty("variant", "danger")
        row.addWidget(self.b_import)
        row.addWidget(self.b_export)
        row.addStretch(1)
        row.addWidget(self.b_delete)
        left.addLayout(row)
        lay.addLayout(left, 3)

        card = Card("Chip definition", "chip")
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        self.f_type = QComboBox()
        for t in TYPE_CLASSES:
            self.f_type.addItem(TYPE_LABELS[t], t)
        self.f_vendor = QComboBox()
        self.f_vendor.setEditable(True)
        self.f_vendor.lineEdit().setPlaceholderText("e.g. WINBOND")
        self.f_name = QLineEdit()
        self.f_name.setPlaceholderText("e.g. W25Q64JV")
        self.f_id = QLineEdit()
        self.f_id.setPlaceholderText("JEDEC ID, e.g. EF4017 (SPI flash only)")
        self.f_id.setValidator(QRegularExpressionValidator(HEX_RE))
        self.f_size = QComboBox()
        for s in _SIZES:
            self.f_size.addItem(format_size(s), s)
        self.f_page = QComboBox()
        for s in _PAGES:
            self.f_page.addItem(f"{s} B", s)
        self.f_algo = QComboBox()
        self.f_volt = QComboBox()
        for i, v in enumerate(VOLTAGE_LABELS):
            self.f_volt.addItem(v, i)
        self.f_delay = QSpinBox()
        self.f_delay.setRange(0, 65535)
        self.f_delay.setValue(1000)
        self.f_delay.setToolTip("SPI flash: erase timeout in 50 ms status polls.\n"
                                "EEPROMs: write/erase delay passed to the programmer.")
        for text, field in (("Type", self.f_type), ("Manufacturer", self.f_vendor),
                            ("Model", self.f_name), ("Chip ID", self.f_id),
                            ("Capacity", self.f_size), ("Page size", self.f_page),
                            ("Algorithm", self.f_algo), ("Supply voltage", self.f_volt),
                            ("Delay", self.f_delay)):
            form_row(form, text, field, muted=False)
        card.add_layout(form)
        self.b_template = QPushButton(theme.icon("copy"), "Copy settings from a built-in chip…")
        self.b_template.setProperty("variant", "ghost")
        card.add(self.b_template)
        card.body().addStretch(1)
        self.form_error = QLabel()
        self.form_error.setStyleSheet(f"color: {theme.palette.danger};")
        self.form_error.setWordWrap(True)
        self.form_error.hide()
        card.add(self.form_error)
        brow = QHBoxLayout()
        self.b_new = QPushButton(theme.icon("plus"), "New")
        self.b_save = primary(QPushButton(theme.icon("check", "accent_text"), "Add chip"))
        brow.addWidget(self.b_new)
        brow.addStretch(1)
        brow.addWidget(self.b_save)
        card.add_layout(brow)
        right = QVBoxLayout()
        right.addWidget(card, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        right.addWidget(close, 0, Qt.AlignRight)
        lay.addLayout(right, 2)

        self.f_type.currentIndexChanged.connect(self._type_changed)
        self.b_new.clicked.connect(self._new)
        self.b_save.clicked.connect(self._save)
        self.b_delete.clicked.connect(self._delete)
        self.b_import.clicked.connect(self._import)
        self.b_export.clicked.connect(self._export)
        self.b_template.clicked.connect(self._template)
        self.view.selectionModel().selectionChanged.connect(self._selected)
        self._type_changed()
        self._new()
        self._update_empty()

    _DEFAULTS = {  # capacity, page size, delay, voltage for a new chip of each family
        "SPI_FLASH": (4 << 20, 256, 1000, 0),
        "24_EEPROM": (256, 16, 4000, 0),
        "93_EEPROM": (128, 16, 2000, 2),
        "25_EEPROM": (8192, 32, 4000, 2),
    }

    def _apply_defaults(self) -> None:
        size, page, delay, volt = self._DEFAULTS[self.f_type.currentData()]
        self.f_size.setCurrentIndex(max(0, self.f_size.findData(size)))
        self.f_page.setCurrentIndex(max(0, self.f_page.findData(page)))
        self.f_delay.setValue(delay)
        self.f_volt.setCurrentIndex(max(0, self.f_volt.findData(volt)))

    def _type_changed(self) -> None:
        chip_type = self.f_type.currentData()
        cls = TYPE_CLASSES[chip_type]
        if self._editing is None and not getattr(self, "_filling", False):
            self._apply_defaults()
        self.f_algo.clear()
        for value, label in ALGORITHM_LABELS[cls].items():
            self.f_algo.addItem(label, value)
        vendors = self.db.manufacturers(chip_type)
        current = self.f_vendor.currentText()
        self.f_vendor.clear()
        self.f_vendor.addItems(vendors)
        self.f_vendor.setEditText(current)
        self.f_id.setEnabled(cls == 0)

    def _fill_form(self, chip: Chip) -> None:
        self._filling = True
        self.f_type.setCurrentIndex(max(0, self.f_type.findData(chip.type)))
        self._type_changed()
        self._filling = False
        self.f_vendor.setEditText(chip.manufacturer)
        self.f_name.setText(chip.name)
        self.f_id.setText(f"{chip.chip_id:06X}" if chip.chip_id else "")
        i = self.f_size.findData(chip.size)
        if i < 0:
            self.f_size.addItem(f"{chip.size} B", chip.size)
            i = self.f_size.count() - 1
        self.f_size.setCurrentIndex(i)
        i = self.f_page.findData(chip.page_size)
        if i < 0:
            self.f_page.addItem(f"{chip.page_size} B", chip.page_size)
            i = self.f_page.count() - 1
        self.f_page.setCurrentIndex(i)
        i = self.f_algo.findData(chip.algorithm)
        if i < 0:
            self.f_algo.addItem(f"0x{chip.algorithm:02X}", chip.algorithm)
            i = self.f_algo.count() - 1
        self.f_algo.setCurrentIndex(i)
        self.f_volt.setCurrentIndex(max(0, self.f_volt.findData(chip.voltage)))
        self.f_delay.setValue(chip.delay)

    def _new(self) -> None:
        self._editing = None
        self.view.clearSelection()
        self.f_name.clear()
        self.f_id.clear()
        self.b_save.setText("Add chip")
        self.form_error.hide()

    def _selected(self) -> None:
        rows = self.view.selectionModel().selectedRows()
        if not rows:
            return
        self._editing = rows[0].row()
        self._fill_form(self.custom[self._editing])
        self.b_save.setText("Save changes")

    def _template(self) -> None:
        dlg = ChipPickerDialog(self, self.db, title="Copy settings from")
        if dlg.exec() and dlg.selected:
            base = dlg.selected
            self._fill_form(base)
            self.f_name.setText(base.name + "-custom")
            self._editing = None
            self.view.clearSelection()
            self.b_save.setText("Add chip")

    def _build(self) -> Chip | None:
        chip_type = self.f_type.currentData()
        vendor = self.f_vendor.currentText().strip().upper()
        name = self.f_name.text().strip()
        problems = []
        if not vendor:
            problems.append("Enter a manufacturer.")
        if not name:
            problems.append("Enter a model name.")
        if "," in vendor + name:
            problems.append("Names cannot contain commas.")
        chip_id = 0
        if TYPE_CLASSES[chip_type] == 0:
            parsed = parse_address(self.f_id.text()) if self.f_id.text().strip() else 0
            if parsed is None:
                problems.append("The chip ID must be hexadecimal.")
            chip_id = parsed or 0
        if len(f"{chip_type},{vendor},{name}".encode("gb18030")) > 47:
            problems.append("The combined name is too long (47 bytes maximum).")
        key = (chip_type, vendor, name)
        for i, other in enumerate(self.custom):
            if other.key == key and i != self._editing:
                problems.append("A custom chip with this name already exists.")
        if problems:
            self.form_error.setText(" ".join(problems))
            self.form_error.show()
            return None
        self.form_error.hide()
        cls = TYPE_CLASSES[chip_type]
        return Chip(type=chip_type, manufacturer=vendor, name=name, chip_id=chip_id,
                    size=self.f_size.currentData(), page_size=self.f_page.currentData(),
                    chip_class=cls, algorithm=self.f_algo.currentData() or 0,
                    delay=self.f_delay.value(), voltage=self.f_volt.currentData() or 0,
                    eeprom_page=1 if cls == 0 else 4, eeprom_size=0 if cls == 0 else 2,
                    custom=True)

    def _update_empty(self) -> None:
        self.empty.setVisible(not self.custom)
        self.empty.resize(self.view.viewport().size())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_empty()

    def _persist(self) -> None:
        self.db.set_custom_chips(self.custom)
        try:
            self.db.save_custom()
        except OSError as exc:
            QMessageBox.warning(self, "Chip database", f"Could not save custom chips:\n{exc}")
        self.model.set_chips(self.custom)
        self.changed = True

    def _save(self) -> None:
        chip = self._build()
        if chip is None:
            return
        if self._editing is None:
            self.custom.append(chip)
            row = len(self.custom) - 1
        else:
            self.custom[self._editing] = chip
            row = self._editing
        self._persist()
        self.view.selectRow(row)

    def _delete(self) -> None:
        rows = self.view.selectionModel().selectedRows()
        if not rows:
            return
        chip = self.custom[rows[0].row()]
        if QMessageBox.question(self, "Delete chip", f"Delete {chip.label}?") != QMessageBox.Yes:
            return
        del self.custom[rows[0].row()]
        self._persist()
        self._new()

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import chip database", "",
                                              "EZP chip database (*.Dat *.dat);;All files (*)")
        if not path:
            return
        try:
            chips = parse_dat(Path(path).read_bytes(), custom=True)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Import", str(exc))
            return
        known = {c.key for c in self.db}
        new = [c for c in chips if c.key not in known]
        self.custom.extend(new)
        self._persist()
        QMessageBox.information(self, "Import", f"Imported {len(new)} new chip(s); "
                                f"{len(chips) - len(new)} were already known.")

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export chip database", "EZP2019+.Dat",
                                              "EZP chip database (*.Dat)")
        if not path:
            return
        only_custom = QMessageBox.question(
            self, "Export", "Export only your custom chips?\n\n"
            "Choose No to export the complete list (usable by the Windows software).") \
            == QMessageBox.Yes
        try:
            Path(path).write_bytes(self.db.export_dat(include_builtin=not only_custom))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Export", str(exc))


# -- About --------------------------------------------------------------------------------------


class AboutDialog(QDialog):
    def __init__(self, parent: QWidget, device_text: str = ""):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QApplication.windowIcon().pixmap(56, 56))
        head.addWidget(logo, 0, Qt.AlignTop)
        text = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("Title")
        title.setFont(scaled_font(title, 1.6, bold=True))
        version = QLabel(f"Version {__version__} for Linux")
        version.setObjectName("Muted")
        text.addWidget(title)
        text.addWidget(version)
        head.addLayout(text, 1)
        lay.addLayout(head)
        body = QLabel(
            "Reads, writes, erases and verifies 25-series SPI flash and 24/25/93-series "
            "EEPROMs with the EZP2019 and EZP2019+ USB programmers.<br><br>"
            "The USB protocol and chip list come from the vendor's EZP2019+ v2.0 Windows "
            "software; see <i>docs/PROTOCOL.md</i>. This is an independent project, not "
            "affiliated with the hardware vendor.<br><br>"
            "Released under the MIT license.")
        body.setWordWrap(True)
        body.setTextFormat(Qt.RichText)
        lay.addWidget(body)
        if device_text:
            dev = QLabel(device_text)
            dev.setObjectName("Banner")
            dev.setWordWrap(True)
            dev.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(dev)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        lay.addWidget(buttons)


def ask_yes_no(parent: QWidget, title: str, text: str, yes: str = "Continue",
               danger: bool = False, informative: str = "") -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    ok = box.addButton(yes, QMessageBox.AcceptRole)
    ok.setProperty("variant", "danger" if danger else "primary")
    box.addButton("Cancel", QMessageBox.RejectRole)
    box.setDefaultButton(ok)
    box.exec()
    return box.clickedButton() is ok
