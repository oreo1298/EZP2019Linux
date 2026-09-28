"""A fast hex editor widget: only the visible rows are ever painted."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QGuiApplication, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QMenu

from .document import BufferDocument
from .theme import mono_font, theme

ROW = 16
_HEX_DIGITS = "0123456789abcdefABCDEF"


class HexView(QAbstractScrollArea):
    cursorMoved = Signal(int)
    selectionChanged = Signal(int, int)
    contextActionRequested = Signal(str)

    def __init__(self, doc: BufferDocument, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.cursor = 0
        self.nibble = 0
        self.pane = "hex"
        self.anchor: int | None = None
        self.sel_start = 0
        self.sel_end = 0
        self.read_only = False
        self._dragging = False
        self.setFrameShape(QAbstractScrollArea.NoFrame)
        self.setFocusPolicy(Qt.StrongFocus)
        self.viewport().setCursor(Qt.IBeamCursor)
        self.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.setFont(mono_font(10.5))
        self._metrics()
        self._scroll_timer = QTimer(self, interval=40)
        self._scroll_timer.timeout.connect(self._autoscroll)
        self._last_mouse = QPointF()

        doc.contentChanged.connect(self._on_content)
        doc.sizeChanged.connect(lambda _n: self._layout_changed())
        theme.changed.connect(lambda _p: self.viewport().update())
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.viewport().update())
        self.horizontalScrollBar().valueChanged.connect(lambda _v: self.viewport().update())

    # -- geometry ---------------------------------------------------------------------

    def _metrics(self) -> None:
        fm = QFontMetricsF(self.font())
        self.cw = fm.horizontalAdvance("0")
        self.ascent = fm.ascent()
        self.lh = round(fm.height() + 5)
        self.header_h = self.lh + 8
        self.margin = round(self.cw * 1.5)
        self.x_hex = self.margin + 10 * self.cw
        self.hex_w = ROW * 3 * self.cw + self.cw
        self.x_ascii = self.x_hex + self.hex_w + 1.5 * self.cw
        self.content_w = self.x_ascii + ROW * self.cw + self.margin

    def setFont(self, font) -> None:  # noqa: N802 - Qt API
        super().setFont(font)
        if hasattr(self, "doc"):
            self._metrics()
            self._layout_changed()

    def _hex_x(self, col: int) -> float:
        return self.x_hex + col * 3 * self.cw + (self.cw if col >= 8 else 0)

    def _ascii_x(self, col: int) -> float:
        return self.x_ascii + col * self.cw

    def rows(self) -> int:
        return -(-len(self.doc) // ROW)

    def visible_rows(self) -> int:
        return max(1, int((self.viewport().height() - self.header_h) // self.lh))

    def _layout_changed(self) -> None:
        vbar = self.verticalScrollBar()
        vbar.setRange(0, max(0, self.rows() - self.visible_rows()))
        vbar.setPageStep(self.visible_rows())
        vbar.setSingleStep(1)
        hbar = self.horizontalScrollBar()
        hbar.setRange(0, max(0, int(self.content_w - self.viewport().width())))
        hbar.setPageStep(self.viewport().width())
        self.cursor = min(self.cursor, max(0, len(self.doc) - 1))
        self.viewport().update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_changed()

    def _on_content(self, start: int, end: int) -> None:
        self.viewport().update()

    def minimumSizeHint(self):  # noqa: N802
        size = super().minimumSizeHint()
        size.setWidth(int(self.content_w * 0.6))
        return size

    # -- public API -------------------------------------------------------------------

    def set_read_only(self, value: bool) -> None:
        self.read_only = value
        self.viewport().setCursor(Qt.ArrowCursor if value else Qt.IBeamCursor)

    def selection(self) -> tuple[int, int]:
        return (self.sel_start, self.sel_end)

    def has_selection(self) -> bool:
        return self.sel_end > self.sel_start

    def set_selection(self, start: int, end: int) -> None:
        n = len(self.doc)
        start, end = max(0, min(start, n)), max(0, min(end, n))
        if (start, end) != (self.sel_start, self.sel_end):
            self.sel_start, self.sel_end = start, end
            self.selectionChanged.emit(start, end)
        self.viewport().update()

    def goto(self, offset: int, length: int = 0) -> None:
        if not len(self.doc):
            return
        offset = max(0, min(offset, len(self.doc) - 1))
        self._move(offset, extend=False)
        if length:
            self.anchor = offset
            self.set_selection(offset, offset + length)
        self.ensure_visible(offset, center=True)

    def ensure_visible(self, offset: int, center: bool = False) -> None:
        row = offset // ROW
        vbar = self.verticalScrollBar()
        top, vis = vbar.value(), self.visible_rows()
        if center and not (top <= row < top + vis):
            vbar.setValue(max(0, row - vis // 2))
        elif row < top:
            vbar.setValue(row)
        elif row >= top + vis:
            vbar.setValue(row - vis + 1)

    # -- painting -----------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        pal = theme.palette
        p = QPainter(self.viewport())
        p.setFont(self.font())
        vp = self.viewport().rect()
        p.fillRect(vp, QColor(pal.hex_bg))
        dx = -self.horizontalScrollBar().value()
        cw, lh, asc = self.cw, self.lh, self.ascent
        data = self.doc.data
        size = len(data)
        chip_size = self.doc.chip_size or size
        top = self.verticalScrollBar().value()
        vis = self.visible_rows() + 1
        focused = self.hasFocus()

        c_text = QColor(pal.hex_text)
        c_zero = QColor(pal.hex_zero)
        c_ff = QColor(pal.hex_ff)
        c_ascii = QColor(pal.hex_ascii)
        c_mod = QColor(pal.hex_modified)
        c_out = QColor(pal.hex_outside)
        c_off = QColor(pal.hex_offset)
        c_sel = QColor(pal.hex_selection)
        c_bad = QColor(pal.hex_mismatch_bg)
        c_stripe = QColor(pal.hex_stripe)
        c_cursor = QColor(pal.hex_cursor)
        has_marks = self.doc.has_marks()
        mismatches = self.doc.mismatches
        sel_a, sel_b = self.sel_start, self.sel_end
        ty = (lh - (asc + QFontMetricsF(self.font()).descent())) / 2 + asc

        for r in range(vis):
            row = top + r
            base = row * ROW
            if base >= size:
                break
            y = self.header_h + r * lh
            if row % 2:
                p.fillRect(QRectF(0, y, vp.width(), lh), c_stripe)
            chunk = data[base:base + ROW]
            n = len(chunk)

            # selection band
            if sel_b > sel_a and sel_a < base + n and sel_b > base:
                s = max(sel_a, base) - base
                e = min(sel_b, base + n) - base
                x1 = self._hex_x(s) + dx - cw * 0.5
                x2 = self._hex_x(e - 1) + dx + cw * 2.5
                p.fillRect(QRectF(x1, y + 1, x2 - x1, lh - 2), c_sel)
                p.fillRect(QRectF(self._ascii_x(s) + dx, y + 1, (e - s) * cw, lh - 2), c_sel)

            p.setPen(c_off)
            p.drawText(QPointF(self.margin + dx, y + ty), f"{base:08X}")

            for i in range(n):
                off = base + i
                b = chunk[i]
                hx = self._hex_x(i) + dx
                ax = self._ascii_x(i) + dx
                if mismatches and off in mismatches:
                    p.fillRect(QRectF(hx - cw * 0.5, y + 1, cw * 3, lh - 2), c_bad)
                    p.fillRect(QRectF(ax, y + 1, cw, lh - 2), c_bad)
                printable = 0x20 <= b < 0x7F
                if off >= chip_size:
                    color = text_color = c_out
                elif has_marks and self.doc.is_modified(off):
                    color = text_color = c_mod
                else:
                    color = c_ff if b == 0xFF else c_zero if b == 0x00 else c_text
                    text_color = c_ascii if printable else c_zero
                p.setPen(color)
                p.drawText(QPointF(hx, y + ty), f"{b:02X}")
                p.setPen(text_color)
                p.drawText(QPointF(ax, y + ty), chr(b) if printable else "·")

            # cursor
            if base <= self.cursor < base + n:
                i = self.cursor - base
                hx = self._hex_x(i) + dx
                ax = self._ascii_x(i) + dx
                pen = QPen(c_cursor, 1.6)
                p.setRenderHint(QPainter.Antialiasing, True)
                p.setBrush(Qt.NoBrush)
                active_hex = self.pane == "hex"
                box_h = QRectF(hx - cw * 0.35, y + 1.5, cw * 2.7, lh - 3)
                box_a = QRectF(ax - 1, y + 1.5, cw + 2, lh - 3)
                p.setPen(pen)
                if focused and not self.read_only:
                    p.drawRoundedRect(box_h if active_hex else box_a, 3, 3)
                    other = box_a if active_hex else box_h
                    p.drawLine(QPointF(other.left(), other.bottom()),
                               QPointF(other.right(), other.bottom()))
                    if active_hex:
                        nx = hx + self.nibble * cw
                        p.fillRect(QRectF(nx, y + lh - 4, cw, 2), c_cursor)
                else:
                    p.drawLine(QPointF(box_h.left(), box_h.bottom()),
                               QPointF(box_h.right(), box_h.bottom()))
                    p.drawLine(QPointF(box_a.left(), box_a.bottom()),
                               QPointF(box_a.right(), box_a.bottom()))
                p.setRenderHint(QPainter.Antialiasing, False)

        # header (drawn last so rows scroll underneath it)
        p.fillRect(QRectF(0, 0, vp.width(), self.header_h), QColor(pal.hex_bg))
        p.setPen(QColor(pal.hex_header))
        hy = (self.header_h - lh) / 2 + ty
        p.drawText(QPointF(self.margin + dx, hy), "Offset")
        cur_col = self.cursor % ROW if size else -1
        for i in range(ROW):
            p.setPen(c_cursor if i == cur_col else QColor(pal.hex_header))
            p.drawText(QPointF(self._hex_x(i) + dx, hy), f"{i:02X}")
        p.setPen(QColor(pal.hex_header))
        p.drawText(QPointF(self._ascii_x(0) + dx, hy), "Decoded text")
        p.setPen(QPen(QColor(pal.border), 1))
        p.drawLine(0, self.header_h - 1, vp.width(), self.header_h - 1)
        if not size:
            p.setPen(QColor(pal.text_faint))
            p.drawText(QRectF(vp).adjusted(0, self.header_h, 0, 0), Qt.AlignCenter,
                       "The buffer is empty — open a file or read a chip.")
        p.end()

    # -- hit testing ------------------------------------------------------------------------

    def _hit(self, pos: QPointF) -> tuple[int, str, int]:
        x = pos.x() + self.horizontalScrollBar().value()
        y = pos.y()
        row = self.verticalScrollBar().value() + int((y - self.header_h) // self.lh)
        row = max(0, min(row, self.rows() - 1))
        cw = self.cw
        if x >= self.x_ascii - cw * 0.5:
            col = int((x - self.x_ascii) // cw)
            pane, nib = "ascii", 0
        else:
            rel = x - self.x_hex
            if rel < 8 * 3 * cw:
                col = int(rel // (3 * cw))
                inner = rel - col * 3 * cw
            else:
                rel -= 8 * 3 * cw + cw
                col = 8 + int(rel // (3 * cw))
                inner = rel - (col - 8) * 3 * cw
            pane = "hex"
            nib = 1 if inner >= cw else 0
        col = max(0, min(col, ROW - 1))
        offset = min(row * ROW + col, max(0, len(self.doc) - 1))
        return offset, pane, nib

    # -- mouse ------------------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not len(self.doc):
            return
        if event.button() == Qt.RightButton:
            return super().mousePressEvent(event)
        offset, pane, nib = self._hit(event.position())
        self.pane = pane
        self.nibble = nib if pane == "hex" else 0
        extend = bool(event.modifiers() & Qt.ShiftModifier)
        self._move(offset, extend=extend)
        self._dragging = True
        self._last_mouse = event.position()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            return
        self._last_mouse = event.position()
        y = event.position().y()
        if y < self.header_h or y > self.viewport().height():
            self._scroll_timer.start()
        else:
            self._scroll_timer.stop()
        offset, _pane, _nib = self._hit(event.position())
        self._move(offset, extend=True, keep_nibble=True)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._dragging = False
        self._scroll_timer.stop()

    def _autoscroll(self) -> None:
        vbar = self.verticalScrollBar()
        if self._last_mouse.y() < self.header_h:
            vbar.setValue(vbar.value() - 2)
        else:
            vbar.setValue(vbar.value() + 2)
        offset, _p, _n = self._hit(self._last_mouse)
        self._move(offset, extend=True, keep_nibble=True, scroll=False)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        has_sel = self.has_selection()
        for key, label, enabled in (
                ("copy_hex", "Copy as hex", has_sel), ("copy_text", "Copy as text", has_sel),
                ("paste_hex", "Paste hex over", not self.read_only), (None, None, None),
                ("select_all", "Select all", True), ("fill", "Fill selection…", not self.read_only),
                (None, None, None), ("goto", "Go to address…", True),
                ("find", "Find…", True)):
            if key is None:
                menu.addSeparator()
                continue
            act = menu.addAction(label)
            act.setEnabled(bool(enabled))
            act.setData(key)
        chosen = menu.exec(event.globalPos())
        if chosen is not None:
            key = chosen.data()
            if key == "copy_hex":
                self.copy(as_text=False)
            elif key == "copy_text":
                self.copy(as_text=True)
            elif key == "paste_hex":
                self.paste_hex()
            elif key == "select_all":
                self.select_all()
            else:
                self.contextActionRequested.emit(key)

    # -- keyboard -----------------------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802
        n = len(self.doc)
        if not n:
            return super().keyPressEvent(event)
        key = event.key()
        mods = event.modifiers()
        shift = bool(mods & Qt.ShiftModifier)
        ctrl = bool(mods & Qt.ControlModifier)
        vis = self.visible_rows()
        moves = {
            Qt.Key_Left: -1, Qt.Key_Right: 1, Qt.Key_Up: -ROW, Qt.Key_Down: ROW,
            Qt.Key_PageUp: -ROW * vis, Qt.Key_PageDown: ROW * vis,
        }
        if event.matches(QKeySequence.SelectAll):
            self.select_all()
            return
        if event.matches(QKeySequence.Copy):
            self.copy(as_text=self.pane == "ascii")
            return
        if event.matches(QKeySequence.Paste):
            self.paste_hex()
            return
        if key in moves:
            if key == Qt.Key_Left and self.pane == "hex" and self.nibble == 1 and not shift:
                self.nibble = 0
                self.viewport().update()
                return
            self._move(self.cursor + moves[key], extend=shift)
            return
        if key == Qt.Key_Home:
            self._move(0 if ctrl else self.cursor - self.cursor % ROW, extend=shift)
            return
        if key == Qt.Key_End:
            self._move(n - 1 if ctrl else min(n - 1, self.cursor - self.cursor % ROW + ROW - 1),
                       extend=shift)
            return
        if key in (Qt.Key_Tab, Qt.Key_Backtab):
            self.pane = "ascii" if self.pane == "hex" else "hex"
            self.nibble = 0
            self.viewport().update()
            return
        if key == Qt.Key_Escape:
            self.anchor = None
            self.set_selection(self.cursor, self.cursor)
            return
        text = event.text()
        if text and not ctrl and not self.read_only:
            if self.pane == "hex" and text in _HEX_DIGITS:
                self._type_nibble(int(text, 16))
                return
            if self.pane == "ascii" and len(text) == 1 and 0x20 <= ord(text) < 0x7F:
                self.doc.write(self.cursor, bytes([ord(text)]))
                self._move(self.cursor + 1, extend=False)
                return
        super().keyPressEvent(event)

    def focusNextPrevChild(self, _next: bool) -> bool:  # noqa: N802 - keep Tab for panes
        return False

    def _type_nibble(self, value: int) -> None:
        old = self.doc.data[self.cursor]
        if self.nibble == 0:
            new = (value << 4) | (old & 0x0F)
            self.doc.write(self.cursor, bytes([new]))
            self.nibble = 1
            self.viewport().update()
        else:
            new = (old & 0xF0) | value
            self.doc.write(self.cursor, bytes([new]))
            self.nibble = 0
            self._move(self.cursor + 1, extend=False)

    def _move(self, offset: int, extend: bool, keep_nibble: bool = False,
              scroll: bool = True) -> None:
        n = len(self.doc)
        offset = max(0, min(offset, n - 1))
        if extend:
            if self.anchor is None:
                self.anchor = self.cursor
            a, b = sorted((self.anchor, offset))
            self.set_selection(a, b + 1)
        else:
            self.anchor = offset
            self.set_selection(offset, offset)
        if offset != self.cursor:
            if not keep_nibble:
                self.nibble = 0
            self.cursor = offset
            self.cursorMoved.emit(offset)
        if scroll:
            self.ensure_visible(offset)
        self.viewport().update()

    # -- clipboard ------------------------------------------------------------------------------

    def select_all(self) -> None:
        self.anchor = 0
        self.set_selection(0, len(self.doc))

    def copy(self, as_text: bool = False) -> None:
        a, b = self.selection()
        if b <= a:
            a, b = self.cursor, self.cursor + 1
        chunk = bytes(self.doc.data[a:min(b, a + (16 << 20))])
        if as_text:
            text = "".join(chr(c) if 0x20 <= c < 0x7F else "." for c in chunk)
        else:
            text = chunk.hex(" ").upper()
        QGuiApplication.clipboard().setText(text)

    def paste_hex(self) -> None:
        if self.read_only:
            return
        text = QGuiApplication.clipboard().text()
        cleaned = "".join(ch for ch in text if ch in _HEX_DIGITS)
        if not cleaned or len(cleaned) % 2:
            return
        payload = bytes.fromhex(cleaned)
        payload = payload[:max(0, len(self.doc) - self.cursor)]
        self.doc.write(self.cursor, payload)
        self.goto(self.cursor, len(payload))
