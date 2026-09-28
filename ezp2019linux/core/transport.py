"""USB transport for the EZP2019+ (pyusb / libusb-1.0).

A transport is opened for one operation "session" and closed afterwards,
exactly like the vendor software does.  The simulator in ``simulator.py``
implements the same interface.
"""

from __future__ import annotations

import array
import errno
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as P
from .errors import (BackendError, NotConnectedError, PermissionDeniedError,
                     UsbTransferError)

TraceFn = Callable[[str], None]


@dataclass
class DeviceInfo:
    """Static description of a connected programmer."""

    vendor_id: int
    product_id: int
    bus: int | None = None
    address: int | None = None
    port_path: str = ""
    manufacturer: str | None = None
    product: str | None = None
    serial: str | None = None
    bcd_device: int = 0
    variant: P.Variant | None = None
    simulated: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def model(self) -> str:
        return "EZP2019+ (simulated)" if self.simulated else "EZP2019+"

    @property
    def usb_id(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"

    @property
    def firmware(self) -> str:
        v = self.bcd_device
        return f"{v >> 8:x}.{(v >> 4) & 0xF:x}{v & 0xF:x}"

    @property
    def location(self) -> str:
        if self.simulated:
            return "virtual"
        loc = f"bus {self.bus:03d} device {self.address:03d}" if self.bus is not None else ""
        if self.port_path:
            loc += f" (port {self.port_path})"
        return loc

    @property
    def key(self) -> tuple:
        return (self.vendor_id, self.product_id, self.bus, self.port_path, self.simulated)


def hexdump(data: bytes, limit: int = 64) -> str:
    shown = bytes(data[:limit]).hex(" ")
    return shown + (f" … (+{len(data) - limit} bytes)" if len(data) > limit else "")


class Transport(ABC):
    """One open connection to a programmer."""

    info: DeviceInfo
    variant: P.Variant

    def __init__(self) -> None:
        self.trace: TraceFn | None = None

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def _write_ep(self, ep: int, data: bytes) -> None: ...

    @abstractmethod
    def _read_ep(self, ep: int, size: int) -> bytes: ...

    def command(self, packet: bytes, response: bool) -> bytes | None:
        """Send a 64-byte command; optionally wait 50 ms and read the reply."""
        if len(packet) != P.PACKET_SIZE:
            raise ValueError("command packets are 64 bytes")
        if self.trace:
            self.trace(f"CMD  > {hexdump(packet[:32])}")
        self._write_ep(self.variant.ep_cmd_out, packet)
        if not response:
            return None
        time.sleep(P.RESPONSE_DELAY_S)
        reply = self._read_ep(self.variant.ep_in, P.PACKET_SIZE)
        if self.trace:
            self.trace(f"RESP < {hexdump(reply[:32])}")
        return reply

    def read_data(self, size: int) -> bytes:
        data = self._read_ep(self.variant.ep_in, size)
        if self.trace:
            self.trace(f"DATA < {len(data)} bytes")
        return data

    def write_data(self, data: bytes) -> None:
        if self.trace:
            self.trace(f"DATA > {len(data)} bytes")
        self._write_ep(self.variant.ep_data_out, data)


class Connector(ABC):
    """Finds programmers and opens transports to them."""

    @abstractmethod
    def devices(self) -> list[DeviceInfo]: ...

    @abstractmethod
    def open(self) -> Transport: ...

    def describe(self) -> DeviceInfo:
        """Open the first programmer just long enough to read its strings."""
        transport = self.open()
        try:
            return transport.info
        finally:
            transport.close()


# --------------------------------------------------------------------------
# libusb implementation


def _import_usb():
    try:
        import usb.core
        import usb.util
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise BackendError("The 'pyusb' Python package is not installed "
                           "(sudo pacman -S python-pyusb).") from exc
    return usb


def _usb_errno(exc) -> int | None:
    return getattr(exc, "errno", None) or getattr(exc, "backend_error_code", None)


class UsbConnector(Connector):
    def __init__(self, product_ids: tuple[int, ...] = P.PRODUCT_IDS):
        self.product_ids = product_ids

    def _find(self):
        usb = _import_usb()
        try:
            return list(usb.core.find(
                find_all=True, idVendor=P.VENDOR_ID,
                custom_match=lambda d: d.idProduct in self.product_ids) or [])
        except usb.core.NoBackendError as exc:
            raise BackendError() from exc

    @staticmethod
    def _base_info(dev) -> DeviceInfo:
        ports = getattr(dev, "port_numbers", None) or ()
        return DeviceInfo(
            vendor_id=dev.idVendor, product_id=dev.idProduct, bus=dev.bus,
            address=dev.address, port_path=".".join(str(p) for p in ports),
            bcd_device=dev.bcdDevice)

    def devices(self) -> list[DeviceInfo]:
        return [self._base_info(d) for d in self._find()]

    def open(self) -> "UsbTransport":
        devices = self._find()
        if not devices:
            raise NotConnectedError()
        return UsbTransport(devices[0], self._base_info(devices[0]))


class UsbTransport(Transport):
    def __init__(self, dev, info: DeviceInfo):
        super().__init__()
        self._usb = _import_usb()
        self.dev = dev
        self.info = info
        self._interface = 0
        self._claimed = False
        self._rx = array.array("B")
        try:
            self._open()
        except Exception:
            self.close()
            raise

    def _string(self, index: int) -> str | None:
        if not index:
            return None
        try:
            return self._usb.util.get_string(self.dev, index, 0x0409)
        except (ValueError, self._usb.core.USBError):
            return None

    def _open(self) -> None:
        usb = self._usb
        dev = self.dev
        try:
            try:
                cfg = dev.get_active_configuration()
            except usb.core.USBError as exc:
                if _usb_errno(exc) in (errno.EACCES, errno.EPERM):
                    raise
                dev.set_configuration()
                cfg = dev.get_active_configuration()
            intf = cfg[(0, 0)]
            self._interface = intf.bInterfaceNumber
            in_eps = {ep.bEndpointAddress for ep in intf
                      if usb.util.endpoint_direction(ep.bEndpointAddress)
                      == usb.util.ENDPOINT_IN}

            # The vendor software reads both strings every time it opens the device.
            self.info.manufacturer = self._string(dev.iManufacturer)
            self.info.product = self._string(dev.iProduct)
            self.info.serial = self._string(dev.iSerialNumber)
            self.variant = P.variant_for(self.info.product, in_eps)
            self.info.variant = self.variant

            try:
                if dev.is_kernel_driver_active(self._interface):
                    dev.detach_kernel_driver(self._interface)
            except (NotImplementedError, usb.core.USBError):
                pass
            usb.util.claim_interface(dev, self._interface)
            self._claimed = True
        except usb.core.USBError as exc:
            code = _usb_errno(exc)
            if code in (errno.EACCES, errno.EPERM):
                raise PermissionDeniedError() from exc
            if code in (errno.ENODEV, errno.ENOENT):
                raise NotConnectedError() from exc
            if code == errno.EBUSY:
                raise UsbTransferError(
                    "The programmer is in use by another program.") from exc
            raise UsbTransferError(f"Could not open the programmer: {exc}") from exc

    def close(self) -> None:
        usb = self._usb
        if self._claimed:
            try:
                usb.util.release_interface(self.dev, self._interface)
            except usb.core.USBError:
                pass
            self._claimed = False
        try:
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError:
            pass

    def _fail(self, what: str, ep: int, exc) -> None:
        usb = self._usb
        code = _usb_errno(exc)
        if code == errno.EPIPE:
            try:
                self.dev.clear_halt(ep)
            except usb.core.USBError:
                pass
        if code in (errno.ENODEV, errno.ENOENT):
            raise NotConnectedError("The programmer was disconnected.") from exc
        if isinstance(exc, usb.core.USBTimeoutError) or code == errno.ETIMEDOUT:
            raise UsbTransferError(f"USB {what} timed out (endpoint 0x{ep:02X}).") from exc
        raise UsbTransferError(f"USB {what} failed on endpoint 0x{ep:02X}: {exc}") from exc

    def _write_ep(self, ep: int, data: bytes) -> None:
        try:
            written = self.dev.write(ep, data, P.USB_TIMEOUT_MS)
        except self._usb.core.USBError as exc:
            self._fail("write", ep, exc)
        if written != len(data):
            raise UsbTransferError(f"Short USB write ({written} of {len(data)} bytes).")

    def _read_ep(self, ep: int, size: int) -> bytes:
        # pyusb reads into an array.array in place; reuse one per request size.
        if len(self._rx) != size:
            self._rx = array.array("B", bytes(size))
        try:
            n = self.dev.read(ep, self._rx, P.USB_TIMEOUT_MS)
        except self._usb.core.USBError as exc:
            self._fail("read", ep, exc)
        return memoryview(self._rx)[:n].tobytes()
