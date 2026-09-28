"""Host integration helpers: udev rule installation and permission checks."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

UDEV_RULE_NAME = "70-ezp2019linux.rules"
UDEV_RULE_PATH = Path("/etc/udev/rules.d") / UDEV_RULE_NAME
PACKAGED_RULE_PATH = Path("/usr/lib/udev/rules.d") / UDEV_RULE_NAME

UDEV_RULE = """\
# EZP2019 / EZP2019+ USB programmer (installed by ezp2019linux).
# "uaccess" gives the user logged in at the local seat access to the device.
SUBSYSTEM=="usb", ATTRS{idVendor}=="1fc8", ATTRS{idProduct}=="310b", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="1fc8", ATTRS{idProduct}=="310c", MODE="0660", TAG+="uaccess"
"""


def udev_rule_installed() -> bool:
    return UDEV_RULE_PATH.exists() or PACKAGED_RULE_PATH.exists()


def install_udev_rule(graphical: bool = False) -> tuple[bool, str]:
    """Install the udev rule and reload udev, elevating with pkexec or sudo.

    Returns ``(success, message)``.
    """
    script = (
        f"install -Dm644 /dev/stdin {UDEV_RULE_PATH} && "
        "udevadm control --reload-rules && "
        "udevadm trigger --subsystem-match=usb --attr-match=idVendor=1fc8"
    )
    if os.geteuid() == 0:
        cmd = ["sh", "-c", script]
    elif graphical and shutil.which("pkexec"):
        cmd = ["pkexec", "sh", "-c", script]
    elif shutil.which("sudo"):
        cmd = ["sudo", "sh", "-c", script]
    elif shutil.which("pkexec"):
        cmd = ["pkexec", "sh", "-c", script]
    else:
        return False, ("Neither pkexec nor sudo is available. Copy the rule below to "
                       f"{UDEV_RULE_PATH} as root:\n\n{UDEV_RULE}")
    try:
        proc = subprocess.run(cmd, input=UDEV_RULE, text=True, capture_output=True,
                              timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Could not run {cmd[0]}: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return False, f"Installing the udev rule failed ({detail or proc.returncode})."
    return True, (f"Installed {UDEV_RULE_PATH}. Unplug and replug the programmer "
                  "if it is connected.")
