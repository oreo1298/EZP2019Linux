# EZP2019+ USB protocol

This document describes the USB protocol spoken by the EZP2019 / EZP2019+
programmer, as recovered from the vendor's Windows software
(`EZP2019+.exe`, "EZP2019+ Version 2.0", © 2019–2025 东莞东门电子). Every
packet layout, timing and sequence below was taken from the disassembly of
that program; `ezp2019linux` reproduces it exactly unless noted.

## Device identification

| Field            | Value                                   |
|------------------|-----------------------------------------|
| VID:PID          | `1fc8:310b` (some units: `1fc8:310c`)   |
| iManufacturer    | `www.zhifengsoft.com`                   |
| iProduct         | `WinUSBComm Device` **or** `WinUSBComm` |
| USB speed        | Full speed (12 Mbit/s), 64-byte packets |

Two firmware variants exist. The Windows software tells them apart by the
product string and uses different endpoints **and a different byte order**
for multi-byte packet fields:

| iProduct            | Bulk IN | Data OUT | Command OUT | Byte order    |
|---------------------|---------|----------|-------------|---------------|
| `WinUSBComm Device` | `0x85`  | `0x05`   | `0x04`      | little-endian |
| `WinUSBComm`        | `0x82`  | `0x01`   | `0x02`      | big-endian    |

`ezp2019linux` picks the variant from the product string and cross-checks it
against the endpoint descriptors. The interface number is taken from the
first interface of the active configuration. The Windows software reads the
manufacturer and product strings every time it opens the device.

## Transport

* **Command**: 64 bytes written to the command OUT endpoint (5 s timeout).
  When a response is expected the host waits 50 ms and then reads 64 bytes
  from the bulk IN endpoint (5 s timeout).
* **Data in**: chip contents are streamed on the bulk IN endpoint. The
  Windows software reads `max(page_size, 64)` bytes per request.
* **Data out**: data to program is streamed on the data OUT endpoint in
  `max(page_size, 64)`-byte chunks.

## Command packets

All packets are 64 bytes; unused bytes are zero. `u16`/`u32` fields use the
byte order of the firmware variant (see above).

### Chip parameter block (commands `0x09` and `0x07`)

| Offset | Size | Content                                              |
|--------|------|------------------------------------------------------|
| 0      | u8   | `0x00`                                               |
| 1      | u8   | command (`0x09` detect, `0x07` setup)                |
| 2      | u8   | chip class (0 = 25 SPI flash, 1 = 24 EEPROM, 2 = 93 EEPROM, 3 = 25 EEPROM) |
| 3      | u8   | algorithm (see chip database)                        |
| 4      | u16  | page size                                            |
| 6      | u16  | delay / erase timeout                                |
| 8      | u32  | chip size in bytes                                   |
| 12     | u32  | chip ID (JEDEC ID for SPI flash)                     |
| 16     | u8   | clock / voltage byte (see below)                     |
| 28     | u8   | supply voltage (0 = 3.3 V, 1 = 1.8 V, 2 = 5.0 V)     |

Byte 16:

* detect (`0x09`): the SPI clock index.
* setup (`0x07`), SPI flash: the SPI clock index.
* setup, 25 EEPROM: `(clock_index << 4) + voltage`.
* setup, 24/93 EEPROM: the voltage.

Clock index: 0 = 12 MHz, 1 = 6 MHz, 2 = 3 MHz, 3 = 1.5 MHz, 4 = 750 kHz,
5 = 375 kHz.

### `0x09` detect

Chip parameter block (the currently selected chip). Response:

| Byte | Meaning                                                            |
|------|--------------------------------------------------------------------|
| 0    | 0 = nothing found, 1 = SPI flash, 2 = 24 EEPROM, 3 = 93 EEPROM     |
| 1..3 | JEDEC ID (manufacturer, memory type, capacity) when byte 0 is 1    |

Only SPI flash can be identified by model. For 24/93 EEPROMs only the family
is reported.

### `0x07` setup

Chip parameter block. The response is read and ignored.

### `0x05` start transfer

| Offset | Size | Content          |
|--------|------|------------------|
| 1      | u8   | `0x05`           |
| 8      | u32  | start address    |

For read, verify and blank check the 64-byte response is read and ignored,
then the host reads the chip contents from the bulk IN endpoint. For write
no response is read; the host streams data to the data OUT endpoint.

### `0x02` erase

| Offset | Size | Content                  |
|--------|------|--------------------------|
| 0      | u8   | `0x01`                   |
| 1      | u8   | `0x02`                   |
| 26     | u16  | argument                 |

No response. The argument depends on the chip class:

* SPI flash: `0x8000` (chip erase), then poll `0x0A` status every 50 ms
  until bit 0 of the response is clear, for at most `delay` polls.
* 24 and 25 EEPROM: the page address; one command is sent per page, for
  every page of the chip.
* 93 EEPROM: the chip's `delay` value, sent once.

### `0x0A` status

Byte 0 = `0x00`, byte 1 = `0x0A`. Response byte 0, bit 0 = busy.

### `0x0B` raw SPI

| Offset | Size | Content                 |
|--------|------|-------------------------|
| 1      | u8   | `0x0B`                  |
| 2      | u8   | `0x03`                  |
| 3      | u8   | number of bytes         |
| 4..    |      | bytes to clock out      |

A response is read. The Windows software uses it to send `0xB7` (enter
4-byte address mode) before read, write, verify and blank check on chips
larger than 16 MiB.

### `0x08` end session

Byte 0 = `0x01`, byte 1 = `0x08`. No response. Sent before the device is
closed after every operation.

## Operation sequences

Every operation opens the device, runs the steps below and then sends
`0x08` and closes the device. "Detect first" is a separate session (open,
`0x09`, `0x08`, close); if its byte 0 is 0 the operation aborts with "No
chips detected".

| Operation   | Detect first    | Session                                                         |
|-------------|-----------------|-----------------------------------------------------------------|
| Read        | not for 25 EEPROM | setup, [`0xB7`], start(0) + response, read *size* bytes      |
| Write       | not for 25 EEPROM | setup, [`0xB7`], start(0), write *n* bytes, 100 ms pause for 24 EEPROM |
| Erase       | yes             | setup, erase (see above)                                        |
| Verify      | yes             | setup, [`0xB7`], start(0) + response, read and compare *n* bytes |
| Blank check | yes             | setup, [`0xB7`], start(0) + response, read *size* bytes, all `0xFF` |
| Auto        | yes             | setup, erase, write, 350 ms pause, verify (all in one session)  |

`[0xB7]` is only sent for chips larger than 16 MiB. `ezp2019linux` also skips
the presence check for 25 EEPROMs on erase, verify, blank check and auto,
because the programmer cannot detect that family.

## Chip database (`EZP2019+.Dat`)

A flat array of 68-byte little-endian records:

| Offset | Size     | Field                                                  |
|--------|----------|--------------------------------------------------------|
| 0x00   | char[48] | `"TYPE,MANUFACTURER,MODEL"` (GBK, NUL-terminated)      |
| 0x30   | u32      | chip ID (JEDEC ID for SPI flash, 0 otherwise)          |
| 0x34   | u32      | size in bytes                                          |
| 0x38   | u16      | page size                                              |
| 0x3A   | u8       | class (0 = 25 flash, 1 = 24 EEPROM, 2 = 93 EEPROM, 3 = 25 EEPROM) |
| 0x3B   | u8       | algorithm                                              |
| 0x3C   | u16      | delay / erase timeout                                  |
| 0x3E   | u16      | "extend" (unused, 0)                                   |
| 0x40   | u16      | EEPROM size (chip editor field, not sent to the device) |
| 0x42   | u8       | EEPROM page (chip editor field, not sent to the device) |
| 0x43   | u8       | voltage (0 = 3.3 V, 1 = 1.8 V, 2 = 5.0 V)              |

Algorithm values:

| Class       | Values                                                              |
|-------------|---------------------------------------------------------------------|
| 25 flash    | 0 = standard SPI flash, 1 = SST SPI flash (SST25VF style)           |
| 24 EEPROM   | 0 = below 4 KiB (8-bit word address), 1 = 4 KiB and larger (16-bit) |
| 93 EEPROM   | 7..11 = 93C46/57/66/76/86 in 8-bit mode, `0x57`..`0x5B` = the same in 16-bit mode |
| 25 EEPROM   | 0 = below 512 B, 1 = 512 B to 64 KiB, 2 = above 64 KiB              |

The Windows software rejects a `.Dat` whose size is not a multiple of 68.
`ezp2019linux` ships the same data converted to JSON
(`ezp2019linux/data/chips.json`) and can import and export `.Dat` files.
