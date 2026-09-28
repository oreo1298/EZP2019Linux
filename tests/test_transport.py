"""The libusb transport, exercised against a fake pyusb device."""

import errno

import pytest

usb = pytest.importorskip("usb")
import usb.core  # noqa: E402
import usb.util  # noqa: E402

from ezp2019linux.core import protocol as P  # noqa: E402
from ezp2019linux.core.errors import (NotConnectedError, PermissionDeniedError,  # noqa: E402
                                      UsbTransferError)
from ezp2019linux.core.transport import DeviceInfo, UsbConnector, UsbTransport  # noqa: E402


class FakeEndpoint:
    def __init__(self, address):
        self.bEndpointAddress = address


class FakeInterface(list):
    bInterfaceNumber = 0


class FakeConfig:
    def __init__(self, endpoints):
        self.intf = FakeInterface(FakeEndpoint(a) for a in endpoints)

    def __getitem__(self, key):
        assert key == (0, 0)
        return self.intf


class FakeDev:
    idVendor = P.VENDOR_ID
    idProduct = 0x310B
    bus = 3
    address = 7
    port_numbers = (1, 4)
    bcdDevice = 0x0201
    iManufacturer, iProduct, iSerialNumber = 1, 2, 0

    def __init__(self, product="WinUSBComm", endpoints=(0x82, 0x01, 0x02), config_error=None):
        self.strings = {1: P.MANUFACTURER_STRING, 2: product}
        self.endpoints = endpoints
        self.config_error = config_error
        self.writes = []
        self.in_queue = []
        self.read_error = None

    def get_active_configuration(self):
        if self.config_error:
            raise self.config_error
        return FakeConfig(self.endpoints)

    def set_configuration(self):
        pass

    def is_kernel_driver_active(self, n):
        return False

    def write(self, ep, data, timeout):
        self.writes.append((ep, bytes(data)))
        return len(data)

    def read(self, ep, buf, timeout):
        assert ep in self.endpoints
        if self.read_error:
            raise self.read_error
        if not self.in_queue:
            raise usb.core.USBTimeoutError("timeout", -7, errno.ETIMEDOUT)
        chunk = self.in_queue.pop(0)[:len(buf)]
        buf[:len(chunk)] = __import__("array").array("B", chunk)
        return len(chunk)

    def clear_halt(self, ep):
        pass


@pytest.fixture(autouse=True)
def fake_util(monkeypatch):
    calls = {"claimed": [], "released": [], "disposed": 0}
    monkeypatch.setattr(usb.util, "get_string",
                        lambda dev, index, langid=None: dev.strings.get(index))
    monkeypatch.setattr(usb.util, "claim_interface",
                        lambda dev, n: calls["claimed"].append(n))
    monkeypatch.setattr(usb.util, "release_interface",
                        lambda dev, n: calls["released"].append(n))

    def dispose(dev):
        calls["disposed"] += 1

    monkeypatch.setattr(usb.util, "dispose_resources", dispose)
    monkeypatch.setattr(P, "RESPONSE_DELAY_S", 0)
    return calls


def open_transport(dev):
    return UsbTransport(dev, DeviceInfo(dev.idVendor, dev.idProduct))


def test_variant_b_big_endian(fake_util):
    dev = FakeDev("WinUSBComm", (0x82, 0x01, 0x02))
    t = open_transport(dev)
    assert t.variant is P.VARIANT_B
    assert t.info.manufacturer == P.MANUFACTURER_STRING
    assert fake_util["claimed"] == [0]
    dev.in_queue.append(bytes(64))
    reply = t.command(P.PacketBuilder("big").status(), True)
    assert reply == bytes(64)
    assert dev.writes[0][0] == 0x02            # command endpoint
    t.write_data(b"\x00" * 64)
    assert dev.writes[1][0] == 0x01            # data endpoint
    t.close()
    assert fake_util["released"] == [0] and fake_util["disposed"] == 1


def test_variant_a_little_endian():
    dev = FakeDev("WinUSBComm Device", (0x85, 0x05, 0x04))
    t = open_transport(dev)
    assert t.variant is P.VARIANT_A
    t.command(bytes(64), False)
    assert dev.writes[0][0] == 0x04


def test_variant_from_endpoints_when_string_missing():
    dev = FakeDev(None, (0x82, 0x01, 0x02))
    dev.strings = {}
    assert open_transport(dev).variant is P.VARIANT_B


def test_read_data_exact_length():
    dev = FakeDev()
    t = open_transport(dev)
    dev.in_queue.append(bytes(range(256)) * 16)
    assert t.read_data(4096) == bytes(range(256)) * 16


def test_permission_error():
    err = usb.core.USBError("Access denied", -3, errno.EACCES)
    with pytest.raises(PermissionDeniedError):
        open_transport(FakeDev(config_error=err))


def test_timeout_maps_to_transfer_error():
    dev = FakeDev()
    t = open_transport(dev)
    with pytest.raises(UsbTransferError, match="timed out"):
        t.read_data(64)


def test_disconnect_maps_to_not_connected():
    dev = FakeDev()
    t = open_transport(dev)
    dev.read_error = usb.core.USBError("No such device", -4, errno.ENODEV)
    with pytest.raises(NotConnectedError):
        t.read_data(64)


def test_drain():
    dev = FakeDev()
    t = open_transport(dev)
    dev.in_queue += [bytes(64), bytes(64)]
    assert t.drain() == 128
    assert t.drain() == 0


def test_connector_filters_product_ids(monkeypatch):
    found = [FakeDev()]
    monkeypatch.setattr(usb.core, "find", lambda **kw: [d for d in found
                                                         if kw["custom_match"](d)])
    connector = UsbConnector()
    [info] = connector.devices()
    assert info.usb_id == "1fc8:310b" and info.port_path == "1.4"
    assert info.firmware == "2.01"
    found[0].idProduct = 0x1234
    assert connector.devices() == []
    with pytest.raises(NotConnectedError):
        connector.open()
