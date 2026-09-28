"""Exception hierarchy shared by the core, CLI and GUI."""


class ProgrammerError(Exception):
    """Base class for all programmer related failures."""


class NotConnectedError(ProgrammerError):
    def __init__(self, message: str = "EZP2019+ programmer not connected."):
        super().__init__(message)


class PermissionDeniedError(ProgrammerError):
    def __init__(self, message: str | None = None):
        super().__init__(message or (
            "Permission denied while opening the EZP2019+ programmer. Install the "
            "udev rule (run 'ezp2019linux install-udev' or see the README), then "
            "unplug and replug the programmer."))


class BackendError(ProgrammerError):
    def __init__(self, message: str | None = None):
        super().__init__(message or (
            "libusb was not found. Install it with 'sudo pacman -S libusb'."))


class UsbTransferError(ProgrammerError):
    """A USB transfer failed or timed out."""


class NoChipError(ProgrammerError):
    def __init__(self, message: str = ("No chip detected. Check that the chip is "
                                       "seated correctly and try again.")):
        super().__init__(message)


class OperationCancelled(ProgrammerError):
    def __init__(self, message: str = "Operation cancelled."):
        super().__init__(message)
