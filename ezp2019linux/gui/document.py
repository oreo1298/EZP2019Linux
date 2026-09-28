"""The data buffer shown in the hex editor and moved to/from the chip."""

from __future__ import annotations

import bisect

from PySide6.QtCore import QObject, Signal

from ..core.programmer import crc32


class BufferDocument(QObject):
    """A byte buffer plus the bookkeeping the UI needs.

    ``data_length`` is how much of the buffer holds meaningful data (the file
    length, the chip size after a read, or the end of the last edit); like
    the vendor tool, Write and Verify use it to decide how much to move.
    """

    contentChanged = Signal(int, int)   # start, end (exclusive); (0, -1) = everything
    sizeChanged = Signal(int)
    stateChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.data = bytearray()
        self.data_length = 0
        self.chip_size = 0
        self.path: str | None = None
        self.source = ""
        self.dirty = False
        self._marks: list[list[int]] = []     # sorted, merged [start, end) edit ranges
        self.mismatches: set[int] = set()

    # -- whole-buffer changes ---------------------------------------------------

    def load(self, data: bytes | bytearray, source: str, path: str | None = None,
             data_length: int | None = None) -> None:
        self.data = bytearray(data)
        self.data_length = len(self.data) if data_length is None else data_length
        if len(self.data) < self.chip_size:
            self.data.extend(b"\xFF" * (self.chip_size - len(self.data)))
        self.path = path
        self.source = source
        self.dirty = False
        self._marks = []
        self.mismatches = set()
        self.sizeChanged.emit(len(self.data))
        self.contentChanged.emit(0, -1)
        self.stateChanged.emit()

    def clear(self) -> None:
        self.load(b"\xFF" * self.chip_size, source="", data_length=0)

    def set_chip_size(self, size: int) -> None:
        """Resize the buffer for a newly selected chip, keeping loaded data."""
        self.chip_size = size
        wanted = max(size, self.data_length)
        if wanted > len(self.data):
            self.data.extend(b"\xFF" * (wanted - len(self.data)))
        elif wanted < len(self.data):
            del self.data[wanted:]
            self._marks = [[s, min(e, wanted)] for s, e in self._marks if s < wanted]
            self.mismatches = {o for o in self.mismatches if o < wanted}
        self.sizeChanged.emit(len(self.data))
        self.contentChanged.emit(0, -1)
        self.stateChanged.emit()

    # -- edits ----------------------------------------------------------------------

    def _mark(self, start: int, end: int) -> None:
        marks = self._marks
        i = bisect.bisect_left(marks, [start, start])
        if i > 0 and marks[i - 1][1] >= start:
            i -= 1
            start = marks[i][0]
        j = i
        while j < len(marks) and marks[j][0] <= end:
            end = max(end, marks[j][1])
            j += 1
        marks[i:j] = [[start, end]]

    def is_modified(self, offset: int) -> bool:
        marks = self._marks
        i = bisect.bisect_right(marks, [offset, float("inf")]) - 1
        return i >= 0 and marks[i][0] <= offset < marks[i][1]

    def has_marks(self) -> bool:
        return bool(self._marks)

    def write(self, offset: int, payload: bytes | bytearray) -> None:
        if not payload:
            return
        end = offset + len(payload)
        if end > len(self.data):
            self.data.extend(b"\xFF" * (end - len(self.data)))
            self.sizeChanged.emit(len(self.data))
        self.data[offset:end] = payload
        self._mark(offset, end)
        if self.mismatches:
            self.mismatches = {o for o in self.mismatches if not offset <= o < end}
        self.data_length = max(self.data_length, end)
        self.dirty = True
        self.contentChanged.emit(offset, end)
        self.stateChanged.emit()

    def set_mismatches(self, offsets) -> None:
        self.mismatches = set(offsets)
        self.contentChanged.emit(0, -1)

    def clear_marks(self) -> None:
        self._marks = []
        self.mismatches = set()
        self.contentChanged.emit(0, -1)

    def mark_saved(self, path: str) -> None:
        self.path = path
        self.dirty = False
        self.stateChanged.emit()

    # -- queries ----------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.data)

    def crc32(self) -> int:
        return crc32(self.data)
