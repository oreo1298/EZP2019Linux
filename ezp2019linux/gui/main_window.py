"""The main window."""

from __future__ import annotations

import html
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QCompleter, QFileDialog,
                               QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QToolBar, QToolButton, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..core import fileio
from ..core import protocol as P
from ..core.chipdb import TYPE_LABELS, Chip, ChipDatabase, format_size
from ..core.errors import (BackendError, NoChipError, NotConnectedError, OperationCancelled,
                           PermissionDeniedError, ProgrammerError)
from ..core.programmer import (DetectResult, Programmer, VerifyResult, round_up,
                               used_length, verify_length, write_length)
from ..core.simulator import VirtualProgrammer
from ..core.system import install_udev_rule
from ..core.transport import DeviceInfo
from . import icons
from .dialogs import (AboutDialog, ChipEditorDialog, ChipPickerDialog, FillDialog,
                      ask_yes_no, parse_address)
from .document import BufferDocument
from .hexview import HexView
from .theme import mono_font, theme
from .widgets import (Card, KeyValueGrid, SegmentedControl, StatTile, StatusDot, Toast,
                      form_row, scaled_font)
from .worker import OperationRunner

MAX_LOG_ENTRIES = 3000
TYPE_SHORT = {"SPI_FLASH": "Flash", "24_EEPROM": "24xx", "93_EEPROM": "93xx",
              "25_EEPROM": "25xx"}


def human_bytes(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.2f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KB"
    return f"{n} B"


def mmss(seconds: float) -> str:
    seconds = max(0, int(seconds + 0.5))
    return f"{seconds // 60}:{seconds % 60:02d}"


class MainWindow(QMainWindow):
    traceLine = Signal(str)

    def __init__(self, programmer: Programmer, db: ChipDatabase,
                 simulator: VirtualProgrammer | None = None):
        super().__init__()
        self.prog = programmer
        self.db = db
        self.sim = simulator
        self.settings = QSettings("ezp2019linux", "ezp2019linux")
        self.doc = BufferDocument()
        self.runner = OperationRunner(self)
        self.chip: Chip | None = None
        self.device: DeviceInfo | None = None
        self.device_error: str | None = None
        self._device_key = object()
        self._log_entries: list[tuple[str, str, str]] = []
        self._stage = ""
        self._stage_t0 = 0.0
        self._icon_actions: list[tuple[QAction, str, str]] = []
        self._chip_names: dict[str, Chip] = {}

        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.resize(1360, 860)
        self.setMinimumSize(980, 640)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_central()
        self._build_statusbar()
        self.toast = Toast(self.centralWidget())

        self.runner.progress.connect(self._on_progress)
        self.runner.finished.connect(self._on_finished)
        self.doc.stateChanged.connect(self._update_buffer_info)
        self.doc.contentChanged.connect(lambda *_: self._buffer_info_timer.start())
        self.traceLine.connect(self._on_trace)
        theme.changed.connect(self._retheme)

        self._buffer_info_timer = QTimer(self, singleShot=True, interval=250)
        self._buffer_info_timer.timeout.connect(self._update_buffer_info)
        self._poll_timer = QTimer(self, interval=1500)
        self._poll_timer.timeout.connect(self._poll_device)

        self._populate_types()
        self._restore_settings()
        self._retheme(theme.palette)
        self._update_actions()
        self._update_buffer_info()
        self.log(f"{APP_NAME} {__version__} ready — {len(self.db)} chips in the database.")
        if self.sim is not None:
            self.log("Demo mode: using a virtual programmer. Start without --simulator to use "
                     "real hardware.", "warning")
        self._poll_timer.start()
        QTimer.singleShot(0, self._poll_device)
        self.hex.setFocus()

    # ------------------------------------------------------------------ construction

    def _action(self, text: str, icon: str | None, slot, shortcut=None, tip: str = "",
                checkable: bool = False) -> QAction:
        act = QAction(text, self)
        if shortcut:
            seqs = shortcut if isinstance(shortcut, (list, tuple)) else [shortcut]
            act.setShortcuts([QKeySequence(s) for s in seqs])
        tooltip = tip or text.replace("…", "")
        if shortcut:
            first = shortcut[0] if isinstance(shortcut, (list, tuple)) else shortcut
            tooltip += f"  ({QKeySequence(first).toString(QKeySequence.NativeText)})"
        act.setToolTip(tooltip)
        act.setStatusTip(tip or text)
        act.setCheckable(checkable)
        if slot is not None:
            act.triggered.connect(slot)
        if icon:
            self._icon_actions.append((act, icon, "text"))
        self.addAction(act)
        return act

    def _build_actions(self) -> None:
        a = self._action
        self.act_new = a("New buffer", None, self.new_buffer, "Ctrl+N",
                         "Clear the buffer (fill it with 0xFF)")
        self.act_open = a("Open…", "open", self.open_file, "Ctrl+O",
                          "Load a .bin, .hex, .rom, .cap, .eep or S-record file into the buffer")
        self.act_save = a("Save…", "save", self.save_file, ["Ctrl+S", "Ctrl+Shift+S"],
                          "Save the buffer to a file")
        self.act_quit = a("Quit", None, self.close, "Ctrl+Q")
        self.act_detect = a("Detect", "detect", self.detect_chip, "Ctrl+D",
                            "Identify the chip in the socket")
        self.act_read = a("Read", "read", self.read_chip, ["F5", "Ctrl+R"],
                          "Read the chip into the buffer")
        self.act_erase = a("Erase", "erase", self.erase_chip, "F6", "Erase the whole chip")
        self.act_write = a("Write", "write", self.write_chip, "F7",
                           "Program the buffer into the chip")
        self.act_verify = a("Verify", "verify", self.verify_chip, "F8",
                            "Compare the chip with the buffer")
        self.act_blank = a("Blank", "blank", self.blank_check, "F4",
                           "Check that the chip is erased")
        self.act_auto = a("Auto", "auto", self.auto_program, "F9",
                          "Erase, write and verify in one go (steps set in Options)")
        self.act_cancel = a("Cancel", "cancel", self.cancel_operation, "Esc",
                            "Stop the running operation")
        self.act_cancel.setEnabled(False)
        self.act_fill = a("Fill…", "fill", self.fill_buffer, "Ctrl+L",
                          "Fill part of the buffer with a byte, word or random data")
        self.act_goto = a("Go to address…", "goto", self.focus_goto, "Ctrl+G")
        self.act_find = a("Find…", "search", self.focus_find, "Ctrl+F",
                          "Search the buffer for hex bytes or text")
        self.act_find_next = a("Find next", None, lambda: self.find_next(True), "F3")
        self.act_find_prev = a("Find previous", None, lambda: self.find_next(False), "Shift+F3")
        self.act_select_all = a("Select all", None, lambda: self.hex.select_all(), None)
        self.act_find_chip = a("Find chip…", "search", self.browse_chips, "Ctrl+K",
                               "Search the chip database")
        self.act_chipdb = a("Chip database…", "database", self.edit_chip_db, None,
                            "Add your own chips, import or export .Dat databases")
        self.act_about = a("About", "info", self.show_about, None)
        self.act_install_udev = a("Install USB permissions (udev rule)…", "shield",
                                  lambda: self.install_udev(), None,
                                  "Allow your user to access the programmer without root")
        self.act_refresh = a("Rescan for programmer", "refresh", self.rescan_device, None)
        self.act_trace = a("Log USB traffic", None, None, None,
                           "Show every command packet in the activity log", checkable=True)
        self.act_trace.toggled.connect(self._set_trace)
        self.act_presence = a("Check for a chip before operations", None, None, None,
                              "Run a detect before read/write/erase/verify, like the vendor "
                              "software", checkable=True)
        self.act_presence.setChecked(True)
        self.act_presence.toggled.connect(lambda on: setattr(self.prog, "presence_check", on))
        self.act_theme = a("Toggle dark/light", "moon", self.toggle_theme, "Ctrl+Shift+T")

        self.theme_group = QActionGroup(self)
        self.theme_actions = {}
        for mode, label in (("system", "Follow system"), ("dark", "Dark"), ("light", "Light")):
            act = QAction(label, self, checkable=True)
            act.triggered.connect(lambda _c=False, m=mode: self.set_theme(m))
            self.theme_group.addAction(act)
            self.theme_actions[mode] = act

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction(self.act_new)
        m.addAction(self.act_open)
        self.recent_menu = m.addMenu("Open recent")
        m.addAction(self.act_save)
        m.addSeparator()
        m.addAction(self.act_quit)

        m = mb.addMenu("&Buffer")
        for act in (self.act_fill, self.act_goto, self.act_find, self.act_find_next,
                    self.act_find_prev, self.act_select_all):
            m.addAction(act)

        m = mb.addMenu("&Operation")
        for act in (self.act_auto, None, self.act_detect, self.act_read, self.act_erase,
                    self.act_write, self.act_verify, self.act_blank, None, self.act_cancel):
            m.addSeparator() if act is None else m.addAction(act)

        m = mb.addMenu("&Device")
        for act in (self.act_find_chip, self.act_chipdb, None, self.act_refresh,
                    self.act_install_udev, None, self.act_presence, self.act_trace):
            m.addSeparator() if act is None else m.addAction(act)

        m = mb.addMenu("&View")
        tm = m.addMenu("Theme")
        for act in self.theme_actions.values():
            tm.addAction(act)
        m.addAction(self.act_theme)

        m = mb.addMenu("&Help")
        m.addAction(self.act_about)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("MainToolbar")
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setIconSize(QSize(22, 22))
        tb.setContextMenuPolicy(Qt.PreventContextMenu)
        self.addToolBar(Qt.TopToolBarArea, tb)
        self.toolbar = tb

        brand = QWidget()
        bl = QHBoxLayout(brand)
        bl.setContentsMargins(4, 0, 14, 0)
        bl.setSpacing(10)
        self.brand_icon = QLabel()
        text = QVBoxLayout()
        text.setSpacing(0)
        name = QLabel("EZP2019+")
        name.setFont(scaled_font(name, 1.25, bold=True))
        sub = QLabel("Programmer for Linux")
        sub.setObjectName("Muted")
        sub.setFont(scaled_font(sub, 0.85))
        text.addWidget(name)
        text.addWidget(sub)
        bl.addWidget(self.brand_icon)
        bl.addLayout(text)
        tb.addWidget(brand)
        tb.addSeparator()
        for act in (self.act_open, self.act_save):
            tb.addAction(act)
        tb.addSeparator()
        for act in (self.act_detect, self.act_read, self.act_blank, self.act_erase,
                    self.act_write, self.act_verify, self.act_auto):
            tb.addAction(act)
        tb.addSeparator()
        for act in (self.act_fill, self.act_chipdb):
            tb.addAction(act)
        auto_btn = tb.widgetForAction(self.act_auto)
        auto_btn.setProperty("accent", True)
        auto_btn.setMinimumWidth(62)
        for act in tb.actions():
            w = tb.widgetForAction(act)
            if isinstance(w, QToolButton):
                w.setMinimumWidth(max(w.minimumWidth(), 58))
                w.setCursor(Qt.PointingHandCursor)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.conn_pill = QFrame()
        self.conn_pill.setObjectName("Inset")
        pl = QHBoxLayout(self.conn_pill)
        pl.setContentsMargins(8, 4, 12, 4)
        pl.setSpacing(4)
        self.conn_dot = StatusDot(8)
        self.conn_text = QLabel("Searching…")
        pl.addWidget(self.conn_dot)
        pl.addWidget(self.conn_text)
        tb.addWidget(self.conn_pill)

        self.theme_btn = QToolButton()
        self.theme_btn.setDefaultAction(self.act_theme)
        self.theme_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.theme_btn.setObjectName("Flat")
        tb.addWidget(self.theme_btn)
        self.about_btn = QToolButton()
        self.about_btn.setDefaultAction(self.act_about)
        self.about_btn.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.about_btn.setObjectName("Flat")
        tb.addWidget(self.about_btn)

    def _build_central(self) -> None:
        page = QWidget()
        page.setObjectName("Page")
        outer = QHBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 10)
        outer.setSpacing(14)

        # sidebar
        side = QWidget()
        side.setObjectName("Page")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 4, 0)
        sl.setSpacing(14)
        sl.addWidget(self._build_programmer_card())
        sl.addWidget(self._build_chip_card())
        sl.addWidget(self._build_options_card())
        sl.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(348)
        scroll.viewport().setObjectName("Page")
        outer.addWidget(scroll)

        # main column
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self._build_buffer_card())
        self.splitter.addWidget(self._build_log_card())
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([560, 170])
        outer.addWidget(self.splitter, 1)
        self.setCentralWidget(page)

    def _build_programmer_card(self) -> Card:
        card = Card("Programmer", "usb")
        refresh = QToolButton()
        refresh.setDefaultAction(self.act_refresh)
        refresh.setObjectName("Flat")
        refresh.setToolButtonStyle(Qt.ToolButtonIconOnly)
        refresh.setIconSize(QSize(16, 16))
        card.header.addWidget(refresh)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.dev_dot = StatusDot(10)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.dev_name = QLabel("EZP2019+")
        self.dev_name.setFont(scaled_font(self.dev_name, 1.15, bold=True))
        self.dev_state = QLabel("Looking for the programmer…")
        self.dev_state.setObjectName("Muted")
        col.addWidget(self.dev_name)
        col.addWidget(self.dev_state)
        row.addWidget(self.dev_dot, 0, Qt.AlignTop)
        row.addLayout(col, 1)
        card.add_layout(row)

        self.dev_grid = KeyValueGrid()
        self.dev_grid.add_row("fw", "Firmware")
        self.dev_grid.add_row("usb", "USB ID")
        self.dev_grid.add_row("iface", "Interface")
        card.add(self.dev_grid)

        self.dev_banner = QLabel()
        self.dev_banner.setObjectName("Banner")
        self.dev_banner.setWordWrap(True)
        self.dev_banner.hide()
        card.add(self.dev_banner)
        self.fix_perm_btn = QPushButton("Fix USB permissions…")
        self.fix_perm_btn.setProperty("variant", "primary")
        self.fix_perm_btn.clicked.connect(lambda: self.install_udev())
        self.fix_perm_btn.hide()
        card.add(self.fix_perm_btn)

        if self.sim is not None:
            sim_row = QHBoxLayout()
            self.sim_chip_btn = QPushButton("Change chip in socket…")
            self.sim_chip_btn.setProperty("variant", "ghost")
            self.sim_chip_btn.clicked.connect(self.change_sim_chip)
            self.sim_plug_btn = QPushButton("Unplug")
            self.sim_plug_btn.setProperty("variant", "ghost")
            self.sim_plug_btn.clicked.connect(self.toggle_sim_plug)
            sim_row.addWidget(self.sim_chip_btn)
            sim_row.addStretch(1)
            sim_row.addWidget(self.sim_plug_btn)
            card.add_layout(sim_row)
        return card

    def _build_chip_card(self) -> Card:
        card = Card("Chip", "chip")
        browse = QToolButton()
        browse.setDefaultAction(self.act_find_chip)
        browse.setObjectName("Flat")
        browse.setToolButtonStyle(Qt.ToolButtonIconOnly)
        browse.setIconSize(QSize(16, 16))
        card.header.addWidget(browse)

        self.chip_search = QLineEdit()
        self.chip_search.setObjectName("Search")
        self.chip_search.setPlaceholderText("Search chips (Ctrl+K)")
        self.chip_search.setClearButtonEnabled(True)
        self._search_action = self.chip_search.addAction(
            theme.icon("search", "text_muted"), QLineEdit.LeadingPosition)
        self.chip_completer = QCompleter(self)
        self.chip_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.chip_completer.setFilterMode(Qt.MatchContains)
        self.chip_completer.setMaxVisibleItems(12)
        self.chip_completer.activated[str].connect(self._completer_chosen)
        self.chip_search.setCompleter(self.chip_completer)
        self.chip_search.returnPressed.connect(self._search_entered)
        card.add(self.chip_search)

        self.type_seg = SegmentedControl()
        self.type_seg.changed.connect(lambda _v: self._type_changed())
        card.add(self.type_seg)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.vendor_box = QComboBox()
        self.model_box = QComboBox()
        for box in (self.vendor_box, self.model_box):
            box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(10)
            box.setMaxVisibleItems(20)
        form_row(form, "Maker", self.vendor_box)
        form_row(form, "Model", self.model_box)
        card.add_layout(form)
        self.vendor_box.currentIndexChanged.connect(self._vendor_changed)
        self.model_box.currentIndexChanged.connect(self._model_changed)

        self.detect_btn = QPushButton("Detect chip")
        self.detect_btn.setProperty("variant", "primary")
        self.detect_btn.clicked.connect(self.detect_chip)
        self.detect_btn.setCursor(Qt.PointingHandCursor)
        card.add(self.detect_btn)

        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.t_size = StatTile("Capacity")
        self.t_page = StatTile("Page")
        self.t_volt = StatTile("Voltage")
        for tile in (self.t_size, self.t_page, self.t_volt):
            tiles.addWidget(tile, 1)
        card.add_layout(tiles)
        self.chip_details = KeyValueGrid()
        self.chip_details.add_row("id", "Chip ID")
        self.chip_details.add_row("family", "Family")
        self.chip_details.add_row("algo", "Algorithm")
        card.add(self.chip_details)
        self.chip_badge = QLabel("Requires the 1.8 V adapter")
        self.chip_badge.setObjectName("Badge")
        self.chip_badge.hide()
        card.add(self.chip_badge)
        return card

    def _build_options_card(self) -> Card:
        card = Card("Options", "clock")
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        self.clock_box = QComboBox()
        for label in P.CLOCK_LABELS:
            self.clock_box.addItem(label)
        self.clock_box.setCurrentIndex(P.DEFAULT_CLOCK)
        self.clock_box.setToolTip("SPI clock for 25-series flash and EEPROMs. Lower it if "
                                  "reads are unreliable (long wires, clips).")
        form_row(form, "SPI clock", self.clock_box)
        card.add_layout(form)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self._muted("Auto"))
        row.addSpacing(18)
        self.chk_erase = QCheckBox("Erase")
        self.chk_program = QCheckBox("Write")
        self.chk_verify = QCheckBox("Verify")
        for c in (self.chk_erase, self.chk_program, self.chk_verify):
            c.setChecked(True)
            row.addWidget(c)
        row.addStretch(1)
        card.add_layout(row)
        return card

    def _muted(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("Muted")
        return lbl

    def _build_buffer_card(self) -> Card:
        card = Card("Buffer", "chip")
        self.buffer_icon_card = card
        self.buffer_title = QLabel()
        self.buffer_title.setObjectName("Muted")
        card.header.insertWidget(2, self.buffer_title)
        self.goto_edit = QLineEdit()
        self.goto_edit.setPlaceholderText("Go to 0x…")
        self.goto_edit.setFixedWidth(118)
        self.goto_edit.returnPressed.connect(self._goto_entered)
        self.find_edit = QLineEdit()
        self.find_edit.setObjectName("Search")
        self.find_edit.setPlaceholderText("Find hex or text")
        self.find_edit.setFixedWidth(210)
        self.find_edit.setClearButtonEnabled(True)
        self._find_action = self.find_edit.addAction(theme.icon("search", "text_muted"),
                                                     QLineEdit.LeadingPosition)
        self.find_edit.returnPressed.connect(lambda: self.find_next(True))
        self.find_mode = QComboBox()
        self.find_mode.addItems(["Hex", "Text"])
        self.find_mode.setFixedWidth(80)
        card.header.addWidget(self.goto_edit)
        card.header.addWidget(self.find_edit)
        card.header.addWidget(self.find_mode)
        card.body().setContentsMargins(1, 12, 1, 1)
        card.header.setContentsMargins(15, 0, 15, 0)

        self.hex = HexView(self.doc)
        self.hex.cursorMoved.connect(lambda _o: self._update_cursor_info())
        self.hex.selectionChanged.connect(lambda *_: self._update_cursor_info())
        self.hex.contextActionRequested.connect(self._hex_context)
        card.add(self.hex, 1)

        foot = QHBoxLayout()
        foot.setContentsMargins(15, 6, 15, 10)
        self.cursor_info = QLabel()
        self.cursor_info.setObjectName("Muted")
        self.cursor_info.setFont(mono_font(9.5))
        self.data_info = QLabel()
        self.data_info.setObjectName("Muted")
        self.data_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        foot.addWidget(self.cursor_info, 1)
        foot.addWidget(self.data_info)
        card.add_layout(foot)
        return card

    def _build_log_card(self) -> Card:
        card = Card("Activity", "log")
        clear = QPushButton("Clear")
        clear.setProperty("variant", "ghost")
        clear.clicked.connect(self._clear_log)
        card.header.addWidget(clear)
        card.body().setContentsMargins(1, 10, 1, 1)
        card.header.setContentsMargins(15, 0, 9, 0)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("Log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(MAX_LOG_ENTRIES)
        self.log_view.setFont(mono_font(9.5))
        card.add(self.log_view, 1)
        return card

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(10, 2, 10, 2)
        lay.setSpacing(12)
        self.op_dot = StatusDot(8)
        self.op_label = QLabel("Ready")
        self.op_label.setMinimumWidth(130)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(280)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.hide()
        self.op_detail = QLabel()
        self.op_detail.setObjectName("Muted")
        self.op_detail.setFont(mono_font(9.5))
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setProperty("variant", "danger")
        self.cancel_btn.clicked.connect(self.cancel_operation)
        self.cancel_btn.hide()
        lay.addWidget(self.op_dot)
        lay.addWidget(self.op_label)
        lay.addWidget(self.progress)
        lay.addWidget(self.op_detail)
        lay.addWidget(self.cancel_btn)
        lay.addStretch(1)
        sb.addWidget(box, 1)
        self.last_op = QLabel()
        self.last_op.setObjectName("Muted")
        sb.addPermanentWidget(self.last_op)

    # ------------------------------------------------------------------ theming

    def _retheme(self, pal) -> None:
        for act, name, token in self._icon_actions:
            act.setIcon(icons.icon(name, getattr(pal, token), pal.text_faint))
        self.act_theme.setIcon(icons.icon("sun" if pal.dark else "moon", pal.text, pal.text_faint))
        self.act_auto.setIcon(icons.icon("auto", pal.accent_text, pal.text_faint))
        self.brand_icon.setPixmap(icons.pixmap("chip", pal.accent, 30))
        self._search_action.setIcon(icons.icon("search", pal.text_muted))
        self._find_action.setIcon(icons.icon("search", pal.text_muted))
        auto_btn = self.toolbar.widgetForAction(self.act_auto)
        auto_btn.style().unpolish(auto_btn)
        auto_btn.style().polish(auto_btn)
        for mode, act in self.theme_actions.items():
            act.setChecked(mode == theme.mode)
        self._refresh_device_view()
        self._set_op_state(self.runner.busy)
        self._rerender_log()

    def set_theme(self, mode: str) -> None:
        theme.apply(QApplication.instance(), mode)
        self.settings.setValue("theme", mode)

    def toggle_theme(self) -> None:
        self.set_theme("light" if theme.palette.dark else "dark")

    # ------------------------------------------------------------------ logging

    def log(self, message: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        self._log_entries.append((stamp, level, message))
        if len(self._log_entries) > MAX_LOG_ENTRIES:
            del self._log_entries[:len(self._log_entries) - MAX_LOG_ENTRIES]
        self.log_view.appendHtml(self._log_html(stamp, level, message))
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _log_html(self, stamp: str, level: str, message: str) -> str:
        pal = theme.palette
        color = {"info": pal.accent, "success": pal.success, "warning": pal.warning,
                 "error": pal.danger, "usb": pal.text_faint}.get(level, pal.text_muted)
        text_color = pal.text_faint if level == "usb" else pal.text
        glyph = {"success": "✓", "warning": "!", "error": "✕", "usb": "·"}.get(level, "•")
        return (f'<span style="color:{pal.text_faint}">{stamp}</span>&nbsp;&nbsp;'
                f'<span style="color:{color}"><b>{glyph}</b></span>&nbsp;&nbsp;'
                f'<span style="color:{text_color}">{html.escape(message)}</span>')

    def _rerender_log(self) -> None:
        if not hasattr(self, "log_view"):
            return
        self.log_view.clear()
        for entry in self._log_entries[-MAX_LOG_ENTRIES:]:
            self.log_view.appendHtml(self._log_html(*entry))

    def _clear_log(self) -> None:
        self._log_entries.clear()
        self.log_view.clear()

    def _set_trace(self, on: bool) -> None:
        self.prog.trace = (lambda line: self.traceLine.emit(line)) if on else None

    def _on_trace(self, line: str) -> None:
        if not line.startswith("DATA"):
            self.log(line, "usb")

    # ------------------------------------------------------------------ chip selection

    def _populate_types(self) -> None:
        current = self.type_seg.value()
        self.type_seg.clear()
        for t in self.db.types():
            self.type_seg.add(TYPE_SHORT.get(t, t), t, TYPE_LABELS.get(t, t))
        self.type_seg.set_value(current if current else "SPI_FLASH")
        names = []
        self._chip_names = {}
        for chip in self.db:
            text = f"{chip.name}  ·  {chip.manufacturer}  ·  {TYPE_LABELS.get(chip.type, chip.type)}"
            if text not in self._chip_names:
                self._chip_names[text] = chip
                names.append(text)
        self.chip_completer.setModel(QStringListModel(names, self.chip_completer))

    def _type_changed(self) -> None:
        chip_type = self.type_seg.value()
        current = self.vendor_box.currentText()
        self.vendor_box.blockSignals(True)
        self.vendor_box.clear()
        vendors = self.db.manufacturers(chip_type) if chip_type else []
        self.vendor_box.addItems(vendors)
        if current in vendors:
            self.vendor_box.setCurrentIndex(vendors.index(current))
        self.vendor_box.blockSignals(False)
        self._vendor_changed()

    def _vendor_changed(self) -> None:
        chip_type = self.type_seg.value()
        vendor = self.vendor_box.currentText()
        self.model_box.blockSignals(True)
        self.model_box.clear()
        for chip in self.db.models(chip_type, vendor):
            self.model_box.addItem(chip.name, chip)
        self.model_box.blockSignals(False)
        self._model_changed()

    def _model_changed(self) -> None:
        chip = self.model_box.currentData()
        if chip is not None:
            self._apply_chip(chip)

    def select_chip(self, chip: Chip) -> None:
        """Select ``chip`` in the three combo boxes and apply it."""
        for box in (self.vendor_box, self.model_box):
            box.blockSignals(True)
        self.type_seg.set_value(chip.type)
        self.vendor_box.clear()
        self.vendor_box.addItems(self.db.manufacturers(chip.type))
        self.vendor_box.setCurrentText(chip.manufacturer)
        self.model_box.clear()
        for j, c in enumerate(self.db.models(chip.type, chip.manufacturer)):
            self.model_box.addItem(c.name, c)
            if c.key == chip.key:
                self.model_box.setCurrentIndex(j)
        for box in (self.vendor_box, self.model_box):
            box.blockSignals(False)
        self._apply_chip(chip)

    def _apply_chip(self, chip: Chip) -> None:
        changed = self.chip is None or chip.key != self.chip.key
        self.chip = chip
        if changed and self.doc.source != "file":
            # A dump of another chip only makes sense up to this chip's size; files keep
            # their full length so an oversized image is still noticed before writing.
            self.doc.data_length = min(self.doc.data_length, chip.size)
        self.t_size.set(format_size(chip.size), f"{chip.size:,} bytes (0x{chip.size:X})")
        self.t_page.set(f"{chip.page_size} B")
        self.t_volt.set(chip.supply_label,
                        "Use the 1.8 V adapter" if chip.is_1v8 else f"Programmer supply "
                        f"{chip.voltage_label}")
        self.chip_details.set("id", chip.jedec_id_label if chip.chip_id else "—",
                              "JEDEC ID used by Detect" if chip.chip_id else
                              "This family cannot be identified automatically")
        self.chip_details.set("family", TYPE_LABELS.get(chip.type, chip.type))
        self.chip_details.set("algo", chip.algorithm_label)
        self.chip_badge.setVisible(chip.is_1v8)
        self.clock_box.setEnabled(chip.uses_clock)
        self.doc.set_chip_size(chip.size)
        self.settings.setValue("chip", "|".join(chip.key))
        if changed:
            self._update_title()
        self._update_actions()
        self._update_buffer_info()

    def _completer_chosen(self, text: str) -> None:
        chip = self._chip_names.get(text)
        if chip is not None:
            self.select_chip(chip)
            QTimer.singleShot(0, self.chip_search.clear)
            self.log(f"Selected {chip.label}.")

    def _search_entered(self) -> None:
        text = self.chip_search.text().strip()
        if not text:
            return
        if text in self._chip_names:
            return
        matches = self.db.find(text)
        if len(matches) == 1:
            self.select_chip(matches[0])
            self.chip_search.clear()
            return
        self.browse_chips(text)

    def browse_chips(self, query: str | bool = "") -> None:
        dlg = ChipPickerDialog(self, self.db, query if isinstance(query, str) else "")
        if dlg.exec() and dlg.selected is not None:
            self.select_chip(dlg.selected)
            self.chip_search.clear()
            self.log(f"Selected {dlg.selected.label}.")

    def clock(self) -> int:
        return max(0, self.clock_box.currentIndex())

    # ------------------------------------------------------------------ device status

    def _poll_device(self) -> None:
        if self.runner.busy:
            return
        try:
            devices = self.prog.devices()
        except BackendError as exc:
            if self.device_error != "backend":
                self.device_error = "backend"
                self.device = None
                self.log(str(exc), "error")
                self._refresh_device_view()
            return
        key = devices[0].key if devices else None
        if key == self._device_key:
            return
        self._device_key = key
        if not devices:
            if self.device is not None:
                self.log("Programmer disconnected.", "warning")
            self.device = None
            self.device_error = None
            self._refresh_device_view()
            self._update_actions()
            return
        self._describe_device()

    def _describe_device(self) -> None:
        try:
            info = self.prog.describe()
        except PermissionDeniedError as exc:
            self.device = None
            self.device_error = "permission"
            self.log(str(exc), "error")
        except ProgrammerError as exc:
            self.device = None
            self.device_error = "busy"
            self.log(f"Found a programmer but could not open it: {exc}", "error")
        else:
            self.device = info
            self.device_error = None
            self.log(f"Programmer connected — {info.model}, firmware {info.firmware}, "
                     f"{info.variant.name if info.variant else 'unknown interface'}.",
                     "success")
        self._refresh_device_view()
        self._update_actions()

    def rescan_device(self) -> None:
        self._device_key = object()
        self._poll_device()

    def _refresh_device_view(self) -> None:
        if not hasattr(self, "dev_dot"):
            return
        pal = theme.palette
        info = self.device
        self.fix_perm_btn.setVisible(self.device_error == "permission")
        if info is not None:
            color, state = pal.success, ("Connected" if not info.simulated
                                         else "Connected · virtual (demo)")
            self.dev_name.setText(info.model)
            self.dev_grid.set("fw", info.firmware)
            self.dev_grid.set("usb", info.usb_id, info.location)
            if info.variant:
                order = "LE" if info.variant.byteorder == "little" else "BE"
                self.dev_grid.set("iface", f"{info.variant.name} ({order})",
                                  info.variant.description)
            self.dev_banner.hide()
            pill = f"{info.model.split(' ')[0]} connected"
        else:
            self.dev_name.setText("EZP2019+")
            for k in ("fw", "usb", "iface"):
                self.dev_grid.set(k, "—")
            if self.device_error == "permission":
                color, state = pal.warning, "No permission to open the device"
                self.dev_banner.setText("The programmer is plugged in but your user cannot "
                                        "access it. Install the udev rule to fix this.")
                self.dev_banner.show()
                pill = "Permission needed"
            elif self.device_error == "backend":
                color, state = pal.danger, "libusb is not available"
                self.dev_banner.setText("Install libusb and pyusb: sudo pacman -S libusb "
                                        "python-pyusb")
                self.dev_banner.show()
                pill = "libusb missing"
            elif self.device_error == "busy":
                color, state = pal.warning, "Device busy or not responding"
                self.dev_banner.setText("Close other programs using the programmer, then "
                                        "replug it.")
                self.dev_banner.show()
                pill = "Device busy"
            else:
                color, state = pal.text_faint, "Not connected — plug in the programmer"
                self.dev_banner.hide()
                pill = "Not connected"
        self.dev_dot.set_color(color)
        self.dev_state.setText(state)
        self.conn_dot.set_color(color, halo=False)
        self.conn_text.setText(pill)

    def install_udev(self, confirm: bool = True) -> None:
        if confirm and not ask_yes_no(self, "USB permissions",
                          "Install a udev rule so that your user can use the EZP2019+ "
                          "without root?",
                          informative="You will be asked for your password. The rule is "
                                      "written to /etc/udev/rules.d/70-ezp2019linux.rules.",
                          yes="Install"):
            return
        ok, message = install_udev_rule(graphical=True)
        self.log(message, "success" if ok else "error")
        if ok:
            QMessageBox.information(self, "USB permissions", message)
            QTimer.singleShot(1500, self.rescan_device)
        else:
            QMessageBox.warning(self, "USB permissions", message)

    # demo mode controls
    def change_sim_chip(self) -> None:
        dlg = ChipPickerDialog(self, self.db, title="Chip in the virtual socket")
        if dlg.exec() and dlg.selected is not None:
            self.sim.insert(dlg.selected, pattern="random")
            self.log(f"Virtual socket now holds {dlg.selected.label} (random contents).")

    def toggle_sim_plug(self) -> None:
        self.sim.connected = not self.sim.connected
        self.sim_plug_btn.setText("Unplug" if self.sim.connected else "Plug in")
        self.rescan_device()

    # ------------------------------------------------------------------ operations

    def _require_chip(self) -> Chip | None:
        if self.chip is None:
            QMessageBox.information(self, APP_NAME, "Select a chip first (or use Detect).")
            return None
        return self.chip

    def _start(self, title: str, job, on_success) -> None:
        if self.runner.busy:
            return
        self.hex.set_read_only(True)
        self._stage = ""
        self.runner.start(title, job, on_success, self._on_error)
        self._set_op_state(True, f"{title}…")

    def _set_op_state(self, busy: bool, text: str | None = None) -> None:
        pal = theme.palette
        if text is not None:
            self.op_label.setText(text)
        self.op_dot.set_color(pal.accent if busy else pal.success, halo=busy)
        self.progress.setVisible(busy)
        self.cancel_btn.setVisible(busy)
        self.act_cancel.setEnabled(busy)
        if not busy:
            self.op_detail.setText("")
        self._update_actions()

    def _update_actions(self) -> None:
        if not hasattr(self, "detect_btn"):
            return
        busy = self.runner.busy
        has_chip = self.chip is not None
        for act in (self.act_read, self.act_erase, self.act_write, self.act_verify,
                    self.act_blank, self.act_auto):
            act.setEnabled(not busy and has_chip)
        self.act_detect.setEnabled(not busy)
        self.detect_btn.setEnabled(not busy)
        for act in (self.act_open, self.act_new, self.act_fill, self.act_chipdb):
            act.setEnabled(not busy)
        for w in (self.type_seg, self.vendor_box, self.model_box, self.chip_search,
                  self.clock_box, self.chk_erase, self.chk_program, self.chk_verify):
            w.setEnabled(not busy)
        if not busy and self.chip is not None:
            self.clock_box.setEnabled(self.chip.uses_clock)

    def _on_progress(self, stage: str, done: int, total: int) -> None:
        now = time.monotonic()
        if stage != self._stage:
            self._stage = stage
            self._stage_t0 = now
            self.op_label.setText(f"{stage}…")
        elapsed = now - self._stage_t0
        if total > 0:
            self.progress.setRange(0, 1000)
            self.progress.setValue(int(1000 * done / total))
            rate = done / elapsed if elapsed > 0.2 else 0
            eta = (total - done) / rate if rate > 0 else 0
            detail = f"{100 * done / total:5.1f}%"
            if total >= 4096 and rate > 0:
                detail += f"   {rate / 1024:,.0f} KB/s   ETA {mmss(eta)}"
            self.op_detail.setText(detail)
        else:
            self.progress.setRange(0, 0)
            self.op_detail.setText(f"{mmss(elapsed)} elapsed")

    def _on_finished(self, title: str) -> None:
        elapsed = time.monotonic() - self.runner.started_at
        self.hex.set_read_only(False)
        self.progress.setRange(0, 1000)
        self._set_op_state(False, "Ready")
        self.last_op.setText(f"Last: {title} · {mmss(elapsed)}")
        QTimer.singleShot(200, self._poll_device)

    def _on_error(self, exc: BaseException) -> None:
        if isinstance(exc, OperationCancelled):
            self.log("Operation cancelled.", "warning")
            self.toast.show_message("Operation cancelled", "warning")
            return
        if isinstance(exc, NoChipError):
            self.log(str(exc), "error")
            QMessageBox.warning(self, "No chip detected", str(exc))
            return
        if isinstance(exc, PermissionDeniedError):
            self.device_error = "permission"
            self._refresh_device_view()
            self.log(str(exc), "error")
            if ask_yes_no(self, "USB permissions", str(exc), yes="Install udev rule"):
                self.install_udev(confirm=False)
            return
        if isinstance(exc, NotConnectedError):
            self.log(str(exc), "error")
            QMessageBox.warning(self, "Programmer not connected",
                                f"{exc}\n\nConnect the EZP2019+ and try again.")
            self.rescan_device()
            return
        if isinstance(exc, ProgrammerError):
            self.log(str(exc), "error")
            QMessageBox.critical(self, "Operation failed", str(exc))
            return
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.log(f"Unexpected error: {exc!r}", "error")
        box = QMessageBox(QMessageBox.Critical, "Unexpected error", str(exc) or repr(exc),
                          parent=self)
        box.setDetailedText(detail)
        box.exec()

    def cancel_operation(self) -> None:
        if self.runner.busy:
            self.runner.cancel()
            self.op_label.setText("Cancelling…")

    # -- detect

    def detect_chip(self) -> None:
        chip, clock, db = self.chip, self.clock(), self.db
        self._start("Detect", lambda p, c: self.prog.detect(chip, clock, db), self._detect_done)

    def _detect_done(self, result: DetectResult) -> None:
        if not result.present:
            self.log("No chip detected. Check the chip's orientation in the socket and try "
                     "again.", "error")
            QMessageBox.information(self, "Detect",
                                    "No chip detected.\n\nPlace the chip in the socket (pin 1 "
                                    "towards the lever) and try again.")
            return
        if result.kind == P.DETECT_SPI_FLASH:
            if not result.matches:
                self.log(f"Unknown chip ID {result.id_label}. Select a compatible model "
                         "manually.", "warning")
                QMessageBox.information(
                    self, "Detect", f"Unknown chip ID = {result.id_label}\n\nPlease choose a "
                    "compatible model manually (same capacity and page size).")
                return
            chip = result.matches[0]
            if len(result.matches) > 1:
                dlg = ChipPickerDialog(
                    self, self.db, chips=result.matches, title="Choose the exact model",
                    message=f"{len(result.matches)} chips share the ID {result.id_label}. "
                            "Pick the part number printed on your chip.")
                if dlg.exec() and dlg.selected is not None:
                    chip = dlg.selected
            self.select_chip(chip)
            self.log(f"Detected {chip.label} (ID {result.id_label}, {chip.size_label}).",
                     "success")
            self.toast.show_message(f"Detected {chip.label}")
            return
        family = result.type_name
        if family and self.type_seg.value() != family:
            self.type_seg.set_value(family)
            self._type_changed()
        label = TYPE_LABELS.get(family, family)
        self.log(f"Chip family detected: {label}. Select the exact model.", "warning")
        QMessageBox.information(self, "Detect",
                                f"Chip family detected: {label}.\n\nThe programmer cannot tell "
                                "the exact model; please select it from the list.")

    # -- read

    def read_chip(self) -> None:
        chip = self._require_chip()
        if chip is None:
            return
        if self.doc.dirty and not ask_yes_no(self, "Read chip",
                                             "The buffer has unsaved changes that will be "
                                             "replaced by the chip contents.", yes="Read"):
            return
        clock = self.clock()
        self._start("Read", lambda p, c: self.prog.read(chip, clock, p, c),
                    lambda data: self._read_done(chip, data))

    def _read_done(self, chip: Chip, data: bytearray) -> None:
        self.doc.load(data, source="chip", data_length=len(data))
        self._update_title()
        crc = self.doc.crc32()
        used = used_length(data)
        self.log(f"Read complete — {chip.label}, {human_bytes(len(data))}, CRC32 {crc:08X}.",
                 "success")
        if used == 0:
            self.log("The chip is empty (every byte is 0xFF).", "info")
            self.toast.show_message("Read complete — the chip is empty", "info")
        else:
            self.toast.show_message(f"Read complete · CRC32 {crc:08X}")

    # -- erase

    def erase_chip(self) -> None:
        chip = self._require_chip()
        if chip is None:
            return
        if not ask_yes_no(self, "Erase chip", f"Erase the entire {chip.label}?",
                          informative="All data on the chip will be lost.", yes="Erase",
                          danger=True):
            return
        clock = self.clock()
        self._start("Erase", lambda p, c: self.prog.erase(chip, clock, p, c),
                    lambda _r: self._simple_done("Erase complete.", "Chip erased"))

    def _simple_done(self, message: str, toast: str) -> None:
        self.log(message, "success")
        self.toast.show_message(toast)

    # -- write / verify / auto helpers

    def _data_ranges(self, chip: Chip, action: str, need_write: bool = True,
                     need_verify: bool = True) -> tuple[int, int] | None:
        data = self.doc.data
        length = self.doc.data_length
        if not (need_write or need_verify):
            return 0, 0
        if length > chip.size:
            if not ask_yes_no(self, action,
                              f"The buffer holds {human_bytes(length)} of data but the "
                              f"{chip.label} holds {human_bytes(chip.size)}.",
                              informative="Continue with only the first "
                                          f"{human_bytes(chip.size)}?"):
                return None
        wlen = write_length(chip, data, length)
        vlen = verify_length(chip, data, length)
        if (need_verify and vlen == 0) or (need_write and wlen == 0):
            QMessageBox.information(self, action, "The buffer is empty. Open a file or read "
                                                  "a chip first.")
            return None
        return wlen, vlen

    def write_chip(self) -> None:
        chip = self._require_chip()
        if chip is None:
            return
        ranges = self._data_ranges(chip, "Write")
        if ranges is None:
            return
        wlen, _vlen = ranges
        if chip.is_spi_flash:
            self.log("SPI flash only changes bits from 1 to 0 when written; erase it first "
                     "(or use Auto).", "info")
        data = bytes(self.doc.data[:self._stream_len(chip, wlen)])
        clock = self.clock()
        self._start("Write", lambda p, c: self.prog.write(chip, data, wlen, clock, p, c),
                    lambda n: self._simple_done(f"Write complete — {human_bytes(n)} "
                                                f"programmed.", "Write complete"))

    @staticmethod
    def _stream_len(chip: Chip, length: int) -> int:
        """Buffer bytes a write of ``length`` bytes sends (whole transfer chunks)."""
        return min(round_up(length, P.transfer_chunk(chip)), chip.size)

    def verify_chip(self) -> None:
        chip = self._require_chip()
        if chip is None:
            return
        ranges = self._data_ranges(chip, "Verify", need_write=False)
        if ranges is None:
            return
        _wlen, vlen = ranges
        data = bytes(self.doc.data[:vlen])
        clock = self.clock()
        self._start("Verify", lambda p, c: self.prog.verify(chip, data, vlen, clock, p, c),
                    self._verify_done)

    def _verify_done(self, result: VerifyResult, title: str = "Verify") -> bool:
        if result.ok:
            self.doc.set_mismatches([])
            self.log(f"{title}: chip matches the buffer ({human_bytes(result.length)}).",
                     "success")
            return True
        self.doc.set_mismatches(result.offsets)
        self.hex.goto(result.first_mismatch)
        self.log(f"{title} failed: {result.mismatches:,} byte(s) differ; first at "
                 f"0x{result.first_mismatch:08X}. Differences are highlighted in red.", "error")
        QMessageBox.warning(self, f"{title} failed",
                            f"Verify error at address 0x{result.first_mismatch:X}.\n\n"
                            f"{result.mismatches:,} byte(s) differ from the buffer.")
        return False

    def blank_check(self) -> None:
        chip = self._require_chip()
        if chip is None:
            return
        clock = self.clock()
        self._start("Blank check", lambda p, c: self.prog.blank_check(chip, clock, p, c),
                    self._blank_done)

    def _blank_done(self, result) -> None:
        if result.blank:
            self._simple_done("Blank check: the chip is empty.", "The chip is blank")
        else:
            self.log(f"Blank check: the chip is not empty (data at 0x{result.first_data:08X}).",
                     "warning")
            QMessageBox.information(self, "Blank check",
                                    f"The chip is not empty.\n\nFirst programmed byte at "
                                    f"0x{result.first_data:X}.")

    def auto_program(self) -> None:
        chip = self._require_chip()
        if chip is None:
            return
        erase, program, verify = (self.chk_erase.isChecked(), self.chk_program.isChecked(),
                                  self.chk_verify.isChecked())
        if not (erase or program or verify):
            QMessageBox.information(self, "Auto", "Tick at least one step under Options.")
            return
        ranges = self._data_ranges(chip, "Auto", need_write=program, need_verify=verify)
        if ranges is None:
            return
        wlen, vlen = ranges
        if erase and not ask_yes_no(self, "Auto", f"Erase and reprogram the {chip.label}?",
                                    informative="Steps: " + ", ".join(
                                        s for s, on in (("erase", erase), ("write", program),
                                                        ("verify", verify)) if on) + ".",
                                    yes="Start", danger=True):
            return
        data = bytes(self.doc.data[:max(self._stream_len(chip, wlen), vlen)])
        clock = self.clock()
        self._start("Auto", lambda p, c: self.prog.auto(
            chip, data, write_len=wlen, verify_len=vlen, clock=clock, erase=erase,
            program=program, verify=verify, progress=p, cancel=c), self._auto_done)

    def _auto_done(self, result) -> None:
        if result.verify is not None and not self._verify_done(result.verify, "Auto"):
            self.toast.show_message("Auto failed — verify error", "error")
            return
        steps = []
        if result.erased:
            steps.append("erased")
        if result.written:
            steps.append(f"wrote {human_bytes(result.written)}")
        if result.verify is not None:
            steps.append("verified")
        self._simple_done("Auto complete: " + ", ".join(steps) + ".", "Auto complete")

    # ------------------------------------------------------------------ files & buffer

    def _last_dir(self) -> str:
        return str(self.settings.value("last_dir", str(Path.home())))

    def open_file(self, path: str | bool | None = None) -> None:
        if not isinstance(path, str) or not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open image", self._last_dir(),
                                                  fileio.OPEN_FILTERS)
            if not path:
                return
        if self.doc.dirty and not ask_yes_no(self, "Open file", "Discard unsaved changes in "
                                             "the buffer?", yes="Discard"):
            return
        try:
            image = fileio.load_image(path)
        except (OSError, fileio.ImageFormatError) as exc:
            self.log(f"Could not open {path}: {exc}", "error")
            QMessageBox.warning(self, "Open file", f"Could not open the file:\n{exc}")
            return
        self.doc.load(image.data, source="file", path=path)
        self.settings.setValue("last_dir", str(Path(path).parent))
        self._add_recent(path)
        self._update_title()
        name = Path(path).name
        self.log(f"Opened {name} — {image.format_label}, {human_bytes(image.length)}, "
                 f"CRC32 {self.doc.crc32():08X}.", "success")
        if self.chip is not None and image.length > self.chip.size:
            self.log(f"The file is larger than the {self.chip.label} "
                     f"({human_bytes(self.chip.size)}); extra bytes are shown dimmed.",
                     "warning")
        self.hex.goto(0)

    def save_file(self) -> None:
        if not len(self.doc):
            QMessageBox.information(self, "Save", "The buffer is empty.")
            return
        base = Path(self.doc.path).stem if self.doc.path else (
            self.chip.name.replace("/", "_") if self.chip else "buffer")
        path, _ = QFileDialog.getSaveFileName(self, "Save buffer",
                                              str(Path(self._last_dir()) / f"{base}.bin"),
                                              fileio.SAVE_FILTERS)
        if not path:
            return
        try:
            fmt = fileio.save_image(path, self.doc.data)
        except OSError as exc:
            QMessageBox.warning(self, "Save", f"Could not save the file:\n{exc}")
            return
        self.doc.mark_saved(path)
        self.settings.setValue("last_dir", str(Path(path).parent))
        self._add_recent(path)
        self._update_title()
        self.log(f"Saved {human_bytes(len(self.doc))} to {Path(path).name} ({fmt}).", "success")
        self.toast.show_message(f"Saved {Path(path).name}")

    def new_buffer(self) -> None:
        if self.doc.dirty and not ask_yes_no(self, "New buffer", "Discard unsaved changes?",
                                             yes="Discard"):
            return
        self.doc.clear()
        self._update_title()
        self.log("Buffer cleared.")

    def fill_buffer(self) -> None:
        if not len(self.doc):
            return
        a, b = self.hex.selection()
        start, end = (a, b - 1) if b > a else (0, len(self.doc) - 1)
        dlg = FillDialog(self, len(self.doc), start, end)
        if not dlg.exec():
            return
        s, e = dlg.result_range
        self.doc.write(s, dlg.pattern(e - s))
        self._update_title()
        self.log(f"Filled 0x{s:X}–0x{e - 1:X} ({human_bytes(e - s)}) with {dlg.describe()}.")

    def _add_recent(self, path: str) -> None:
        recent = [p for p in self.settings.value("recent", [], type=list) if p != path]
        recent.insert(0, path)
        self.settings.setValue("recent", recent[:10])
        self._rebuild_recent()

    def _rebuild_recent(self) -> None:
        self.recent_menu.clear()
        recent = self.settings.value("recent", [], type=list)
        for p in recent:
            act = self.recent_menu.addAction(Path(p).name)
            act.setToolTip(p)
            act.triggered.connect(lambda _c=False, path=p: self.open_file(path))
        self.recent_menu.setEnabled(bool(recent))

    def _update_title(self) -> None:
        name = Path(self.doc.path).name if self.doc.path else (
            "chip contents" if self.doc.source == "chip" else "untitled")
        dirty = " •" if self.doc.dirty else ""
        self.setWindowTitle(f"{name}{dirty} — {APP_NAME}")

    def _update_buffer_info(self) -> None:
        if not hasattr(self, "data_info"):
            return
        doc = self.doc
        src = {"chip": "read from chip", "file": Path(doc.path).name if doc.path else "file"}.get(
            doc.source, "empty buffer" if not doc.data_length else "edited")
        chip = f"{self.chip.label} · " if self.chip else ""
        self.buffer_title.setText(f"·  {chip}{src}")
        crc = doc.crc32() if len(doc) <= (64 << 20) else 0
        self.data_info.setText(f"Data {human_bytes(doc.data_length)} of "
                               f"{human_bytes(len(doc))}   ·   CRC32 {crc:08X}")
        self._update_title()
        self._update_cursor_info()

    def _update_cursor_info(self) -> None:
        if not len(self.doc):
            self.cursor_info.setText("")
            return
        off = self.hex.cursor
        value = self.doc.data[off] if off < len(self.doc) else 0
        text = f"Offset 0x{off:08X} ({off:,})   Value 0x{value:02X} ({value})"
        a, b = self.hex.selection()
        if b > a:
            text += f"   Selected {b - a:,} bytes (0x{a:X}–0x{b - 1:X})"
        self.cursor_info.setText(text)

    # -- go to / find

    def focus_goto(self) -> None:
        self.goto_edit.setFocus()
        self.goto_edit.selectAll()

    def focus_find(self) -> None:
        self.find_edit.setFocus()
        self.find_edit.selectAll()

    def _goto_entered(self) -> None:
        addr = parse_address(self.goto_edit.text())
        if addr is None or addr >= len(self.doc):
            self.log(f"Address out of range: {self.goto_edit.text()!r}.", "warning")
            return
        self.hex.goto(addr)
        self.hex.setFocus()

    def _find_pattern(self) -> bytes | None:
        text = self.find_edit.text()
        if not text:
            return None
        if self.find_mode.currentText() == "Hex":
            cleaned = text.replace(" ", "").replace("0x", "").replace(",", "")
            try:
                return bytes.fromhex(cleaned)
            except ValueError:
                self.log("Enter hex bytes such as 'EF 40 17', or switch the search to Text.",
                         "warning")
                return None
        return text.encode("latin-1", errors="replace")

    def find_next(self, forward: bool = True) -> None:
        pattern = self._find_pattern()
        if not pattern:
            self.focus_find()
            return
        data = self.doc.data
        start = self.hex.cursor
        if forward:
            pos = data.find(pattern, start + 1)
            if pos < 0:
                pos = data.find(pattern, 0)
        else:
            pos = data.rfind(pattern, 0, max(0, start))
            if pos < 0:
                pos = data.rfind(pattern)
        if pos < 0:
            self.log(f"Not found: {self.find_edit.text()!r}.", "warning")
            self.toast.show_message("No match in the buffer", "warning", 2000)
            return
        self.hex.goto(pos, len(pattern))

    def _hex_context(self, key: str) -> None:
        {"fill": self.fill_buffer, "goto": self.focus_goto, "find": self.focus_find}[key]()

    # ------------------------------------------------------------------ misc

    def edit_chip_db(self) -> None:
        dlg = ChipEditorDialog(self, self.db)
        dlg.exec()
        if dlg.changed:
            current = self.chip
            self._populate_types()
            if current is not None:
                chip = self.db.get(*current.key) or current
                self.select_chip(chip)
            self.log(f"Chip database updated: {len(self.db.custom_chips())} custom chip(s).")

    def show_about(self) -> None:
        text = ""
        if self.device is not None:
            d = self.device
            text = (f"<b>{d.model}</b> · firmware {d.firmware} · {d.usb_id}<br>"
                    f"{d.variant.description if d.variant else ''}")
        AboutDialog(self, text).exec()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls() and not self.runner.busy:
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        urls = [u for u in event.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.open_file(urls[0].toLocalFile())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast._place()

    # ------------------------------------------------------------------ settings

    def _restore_settings(self) -> None:
        s = self.settings
        geometry = s.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        sizes = s.value("splitter")
        if sizes:
            try:
                self.splitter.setSizes([int(x) for x in sizes])
            except (TypeError, ValueError):
                pass
        self.clock_box.setCurrentIndex(int(s.value("clock", P.DEFAULT_CLOCK)))
        for key, box in (("auto_erase", self.chk_erase), ("auto_program", self.chk_program),
                         ("auto_verify", self.chk_verify)):
            box.setChecked(s.value(key, True, type=bool))
        self.act_presence.setChecked(s.value("presence_check", True, type=bool))
        chip = None
        saved = s.value("chip", "")
        if saved and saved.count("|") == 2:
            chip = self.db.get(*saved.split("|"))
        if chip is None:
            found = self.db.find("WINBOND:W25Q64")
            chip = found[0] if found else next(iter(self.db), None)
        if chip is not None:
            self.select_chip(chip)
        self._rebuild_recent()

    def _save_settings(self) -> None:
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("splitter", self.splitter.sizes())
        s.setValue("clock", self.clock_box.currentIndex())
        s.setValue("auto_erase", self.chk_erase.isChecked())
        s.setValue("auto_program", self.chk_program.isChecked())
        s.setValue("auto_verify", self.chk_verify.isChecked())
        s.setValue("presence_check", self.act_presence.isChecked())

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.runner.busy:
            if not ask_yes_no(self, "Quit", f"{self.runner.name} is still running. Cancel it "
                              "and quit?", yes="Cancel and quit", danger=True):
                event.ignore()
                return
            self.runner.cancel()
            self.runner.wait(15000)
        if self.doc.dirty and not ask_yes_no(self, "Quit", "The buffer has unsaved changes. "
                                             "Quit anyway?", yes="Quit"):
            event.ignore()
            return
        self._poll_timer.stop()
        self._save_settings()
        event.accept()
