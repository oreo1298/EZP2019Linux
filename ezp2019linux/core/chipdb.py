"""Chip database: the EZP2019+ chip list plus user-defined chips.

The vendor software keeps its chip list in ``EZP2019+.Dat``, a flat array of
68-byte records (see ``docs/PROTOCOL.md``).  We ship the same data converted
to JSON and can import/export the binary format so databases can be shared
with the Windows tool.
"""

from __future__ import annotations

import json
import os
import struct
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable, Iterator

RECORD_SIZE = 68
_RECORD = struct.Struct("<48sIIHBBHHHBB")
assert _RECORD.size == RECORD_SIZE

CLASS_SPI_FLASH = 0
CLASS_24_EEPROM = 1
CLASS_93_EEPROM = 2
CLASS_25_EEPROM = 3

# Type string used in the database -> chip class byte sent to the programmer.
TYPE_CLASSES = {
    "SPI_FLASH": CLASS_SPI_FLASH,
    "24_EEPROM": CLASS_24_EEPROM,
    "93_EEPROM": CLASS_93_EEPROM,
    "25_EEPROM": CLASS_25_EEPROM,
}
CLASS_TYPES = {v: k for k, v in TYPE_CLASSES.items()}

TYPE_LABELS = {
    "SPI_FLASH": "SPI Flash (25xx)",
    "24_EEPROM": "I²C EEPROM (24xx)",
    "93_EEPROM": "Microwire EEPROM (93xx)",
    "25_EEPROM": "SPI EEPROM (25xx)",
}

VOLTAGE_LABELS = ("3.3 V", "1.8 V", "5.0 V")

# Algorithm byte meaning per chip class (labels from the vendor chip editor).
ALGORITHM_LABELS = {
    CLASS_SPI_FLASH: {0: "Standard SPI flash", 1: "SST SPI flash"},
    CLASS_24_EEPROM: {0: "< 4 KB (8-bit address)", 1: "≥ 4 KB (16-bit address)"},
    CLASS_93_EEPROM: {
        0x07: "93C46 ×8", 0x08: "93C57 ×8", 0x09: "93C66 ×8",
        0x0A: "93C76 ×8", 0x0B: "93C86 ×8",
        0x57: "93C46 ×16", 0x58: "93C57 ×16", 0x59: "93C66 ×16",
        0x5A: "93C76 ×16", 0x5B: "93C86 ×16",
    },
    CLASS_25_EEPROM: {0: "< 512 B", 1: "512 B – 64 KB", 2: "> 64 KB"},
}

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BUNDLED_DB = _DATA_DIR / "chips.json"


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "ezp2019linux"


def custom_db_path() -> Path:
    return user_config_dir() / "custom_chips.json"


def format_size(size: int) -> str:
    """Human readable size using binary units (as chip vendors do)."""
    if size >= 1 << 20 and size % (1 << 20) == 0:
        return f"{size >> 20} MB"
    if size >= 1 << 10 and size % (1 << 10) == 0:
        return f"{size >> 10} KB"
    return f"{size} B"


def _clean_name(text: str) -> str:
    return text.replace("（", "(").replace("）", ")").strip()


@dataclass(frozen=True)
class Chip:
    """One chip definition, mirroring a 68-byte ``.Dat`` record."""

    type: str
    manufacturer: str
    name: str
    chip_id: int
    size: int
    page_size: int
    chip_class: int
    algorithm: int = 0
    delay: int = 0
    voltage: int = 0
    extend: int = 0
    eeprom_size: int = 0
    eeprom_page: int = 0
    custom: bool = False

    # -- presentation -------------------------------------------------------

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.type, self.manufacturer, self.name)

    @property
    def label(self) -> str:
        return f"{self.manufacturer} {self.name}"

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)

    @property
    def voltage_label(self) -> str:
        if 0 <= self.voltage < len(VOLTAGE_LABELS):
            return VOLTAGE_LABELS[self.voltage]
        return f"#{self.voltage}"

    @property
    def algorithm_label(self) -> str:
        labels = ALGORITHM_LABELS.get(self.chip_class, {})
        return labels.get(self.algorithm, f"0x{self.algorithm:02X}")

    @property
    def size_label(self) -> str:
        return format_size(self.size)

    @property
    def jedec_id_label(self) -> str:
        if not self.chip_id:
            return "—"
        return " ".join(f"{b:02X}" for b in self.chip_id.to_bytes(3, "big")) \
            if self.chip_id <= 0xFFFFFF else f"0x{self.chip_id:08X}"

    # -- behaviour ----------------------------------------------------------

    @property
    def is_spi_flash(self) -> bool:
        return self.chip_class == CLASS_SPI_FLASH

    @property
    def is_eeprom(self) -> bool:
        return self.chip_class != CLASS_SPI_FLASH

    @property
    def detectable(self) -> bool:
        """True if the programmer can identify this exact model by ID."""
        return self.chip_class == CLASS_SPI_FLASH and self.chip_id != 0

    @property
    def uses_4byte_address(self) -> bool:
        return self.size > 0x1000000

    @property
    def uses_clock(self) -> bool:
        """The SPI clock setting only applies to SPI flash and 25xx EEPROMs."""
        return self.chip_class in (CLASS_SPI_FLASH, CLASS_25_EEPROM)

    # -- serialisation ------------------------------------------------------

    def to_record(self) -> bytes:
        name = f"{self.type},{self.manufacturer},{self.name}".encode("gb18030")
        if len(name) > 47:
            raise ValueError(f"chip name too long for .Dat record: {name!r}")
        return _RECORD.pack(
            name, self.chip_id & 0xFFFFFFFF, self.size & 0xFFFFFFFF,
            self.page_size & 0xFFFF, self.chip_class & 0xFF, self.algorithm & 0xFF,
            self.delay & 0xFFFF, self.extend & 0xFFFF, self.eeprom_size & 0xFFFF,
            self.eeprom_page & 0xFF, self.voltage & 0xFF,
        )

    @classmethod
    def from_record(cls, raw: bytes, custom: bool = False) -> "Chip | None":
        (name, chip_id, size, page, chip_class, algorithm, delay, extend,
         eeprom_size, eeprom_page, voltage) = _RECORD.unpack(raw)
        text = name.split(b"\0", 1)[0].decode("gb18030", errors="replace")
        parts = [_clean_name(p) for p in text.split(",")]
        if len(parts) != 3 or not all(parts):
            return None
        return cls(parts[0], parts[1], parts[2], chip_id, size, page, chip_class,
                   algorithm, delay, voltage, extend, eeprom_size, eeprom_page, custom)

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("custom")
        d["chip_id"] = f"0x{self.chip_id:06X}"
        return d

    @classmethod
    def from_json(cls, d: dict, custom: bool = False) -> "Chip":
        d = dict(d)
        chip_id = d.pop("chip_id", 0)
        if isinstance(chip_id, str):
            chip_id = int(chip_id, 0)
        d.pop("custom", None)
        chip_type = d["type"]
        if "chip_class" not in d:
            d["chip_class"] = TYPE_CLASSES.get(chip_type, 0)
        return cls(chip_id=chip_id, custom=custom, **d)

    def with_changes(self, **changes) -> "Chip":
        return replace(self, **changes)


def read_json(path: Path, custom: bool = False) -> list[Chip]:
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return [Chip.from_json(c, custom=custom) for c in doc.get("chips", [])]


def write_json(path: Path, chips: Iterable[Chip], description: str = "") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(c.to_json(), ensure_ascii=False, separators=(",", ":"))
             for c in chips]
    header = json.dumps({"format": 1, "description": description},
                        ensure_ascii=False)[:-1]
    body = ",\n  ".join(lines)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(f'{header}, "chips": [\n  {body}\n]}}\n')
    os.replace(tmp, path)


def parse_dat(data: bytes, custom: bool = False) -> list[Chip]:
    if len(data) % RECORD_SIZE:
        raise ValueError("not an EZP2019+ chip database: size is not a multiple "
                         f"of {RECORD_SIZE} bytes")
    chips = []
    for off in range(0, len(data), RECORD_SIZE):
        chip = Chip.from_record(data[off:off + RECORD_SIZE], custom=custom)
        if chip is not None:
            chips.append(chip)
    return chips


def build_dat(chips: Iterable[Chip]) -> bytes:
    """Serialise chips to the vendor ``.Dat`` format (with its empty terminator)."""
    out = bytearray()
    for chip in chips:
        out += chip.to_record()
    out += bytes(RECORD_SIZE)
    return bytes(out)


_TYPE_ORDER = ("SPI_FLASH", "24_EEPROM", "93_EEPROM", "25_EEPROM")


class ChipDatabase:
    """Searchable chip list: bundled chips followed by the user's own chips."""

    def __init__(self, chips: Iterable[Chip] = (), custom_path: Path | None = None):
        self._builtin: list[Chip] = [c for c in chips if not c.custom]
        self._custom: list[Chip] = [c for c in chips if c.custom]
        self.custom_path = custom_path
        self._rebuild()

    @classmethod
    def load(cls, bundled: Path = BUNDLED_DB,
             custom_path: Path | None = None) -> "ChipDatabase":
        chips = read_json(bundled) if Path(bundled).exists() else []
        custom_path = custom_path if custom_path is not None else custom_db_path()
        custom: list[Chip] = []
        if custom_path and Path(custom_path).exists():
            try:
                custom = read_json(custom_path, custom=True)
            except (OSError, ValueError, KeyError, TypeError):
                custom = []
        return cls([*chips, *custom], custom_path=custom_path)

    def _rebuild(self) -> None:
        self._all = [*self._builtin, *self._custom]
        self._by_key = {}
        for chip in self._all:
            self._by_key.setdefault(chip.key, chip)

    # -- container protocol -------------------------------------------------

    def __len__(self) -> int:
        return len(self._all)

    def __iter__(self) -> Iterator[Chip]:
        return iter(self._all)

    @property
    def chips(self) -> list[Chip]:
        return list(self._all)

    # -- browsing -----------------------------------------------------------

    def types(self) -> list[str]:
        seen = []
        for chip in self._all:
            if chip.type not in seen:
                seen.append(chip.type)
        known = [t for t in _TYPE_ORDER if t in seen]
        return known + [t for t in seen if t not in known]

    def manufacturers(self, chip_type: str) -> list[str]:
        seen: list[str] = []
        for chip in self._all:
            if chip.type == chip_type and chip.manufacturer not in seen:
                seen.append(chip.manufacturer)
        return seen

    def models(self, chip_type: str, manufacturer: str) -> list[Chip]:
        return [c for c in self._all
                if c.type == chip_type and c.manufacturer == manufacturer]

    def get(self, chip_type: str, manufacturer: str, name: str) -> Chip | None:
        return self._by_key.get((chip_type, manufacturer, name))

    # -- lookup -------------------------------------------------------------

    def by_jedec_id(self, jedec_id: int) -> list[Chip]:
        """All SPI flash definitions matching a 24-bit JEDEC ID, in database order."""
        return [c for c in self._all
                if c.chip_class == CLASS_SPI_FLASH and c.chip_id == jedec_id and jedec_id]

    def find(self, spec: str) -> list[Chip]:
        """Resolve a user supplied chip spec.

        Accepts ``NAME``, ``MANUFACTURER:NAME`` or ``TYPE:MANUFACTURER:NAME``
        (case insensitive).  Exact name matches win over prefix matches.
        """
        parts = [p.strip().lower() for p in spec.split(":")]
        if len(parts) == 3:
            t, m, n = parts
            return [c for c in self._all if c.type.lower() == t
                    and c.manufacturer.lower() == m and c.name.lower() == n]
        if len(parts) == 2:
            m, n = parts
            pool = [c for c in self._all if c.manufacturer.lower() == m]
        else:
            n = parts[0]
            pool = self._all
        exact = [c for c in pool if c.name.lower() == n]
        if exact:
            return exact
        return [c for c in pool if c.name.lower().startswith(n)]

    def search(self, text: str, chip_type: str | None = None,
               limit: int | None = None) -> list[Chip]:
        """Substring search over name, manufacturer and JEDEC ID."""
        terms = text.lower().split()
        results = []
        for chip in self._all:
            if chip_type and chip.type != chip_type:
                continue
            hay = " ".join((chip.name, chip.manufacturer, chip.type,
                            f"{chip.chip_id:06x}" if chip.chip_id else "")).lower()
            if all(t in hay for t in terms):
                results.append(chip)
                if limit and len(results) >= limit:
                    break
        # Names that start with the query first, then the rest in DB order.
        if terms:
            first = terms[0]
            results.sort(key=lambda c: 0 if c.name.lower().startswith(first) else 1)
        return results

    # -- user chips ----------------------------------------------------------

    def custom_chips(self) -> list[Chip]:
        return list(self._custom)

    def set_custom_chips(self, chips: Iterable[Chip]) -> None:
        self._custom = [replace(c, custom=True) for c in chips]
        self._rebuild()

    def save_custom(self, path: Path | None = None) -> None:
        path = path or self.custom_path or custom_db_path()
        write_json(Path(path), self._custom, "ezp2019linux user chip definitions")

    def export_dat(self, include_builtin: bool = True) -> bytes:
        chips = self._all if include_builtin else self._custom
        return build_dat(chips)
