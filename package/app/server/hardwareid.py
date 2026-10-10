# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
"""Active hardware identification for NiuFan.

The hwmon path in :mod:`fanhardware` only sees controllers that a kernel
driver has already registered.  This module adds the missing first step: read
the identifiers the machine itself exposes, probe x86 Super-I/O configuration
space read-only, and map a detected chip to the control interface available on
this system.

The direct Super-I/O backend is deliberately narrow.  Only register layouts
that are documented by the corresponding Linux driver are implemented, and
unknown or unsupported chips remain read-only.
"""

from __future__ import annotations

import ctypes
import errno
import glob
import os
import platform
from dataclasses import dataclass

try:
    import fcntl
except ImportError:  # pragma: no cover - only on non-POSIX test hosts
    fcntl = None


def read_text(path, limit=4096):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit).strip()
    except (OSError, ValueError):
        return None


def read_int(path):
    raw = read_text(path)
    if raw is None:
        return None
    try:
        return int(raw, 0)
    except ValueError:
        return None


def _glob_values(pattern, field, limit=256):
    rows = []
    for path in sorted(glob.glob(pattern))[:limit]:
        value = read_text(os.path.join(path, field))
        if value:
            rows.append({"path": path, field: value})
    return rows


def collect_bus_inventory():
    """Collect stable identifiers exposed by the running kernel."""
    inventory = {
        "dmi": {},
        "pci": [],
        "usb": [],
        "acpi": [],
        "i2c": [],
        "device_tree": [],
        "problems": [],
    }
    for key in ("board_vendor", "board_name", "product_name", "bios_vendor",
                "bios_version", "sys_vendor"):
        value = read_text("/sys/class/dmi/id/%s" % key)
        if value:
            inventory["dmi"][key] = value

    for base in sorted(glob.glob("/sys/bus/pci/devices/*")):
        entry = {"path": base}
        for key in ("vendor", "device", "subsystem_vendor",
                    "subsystem_device", "class", "modalias"):
            value = read_text(os.path.join(base, key))
            if value:
                entry[key] = value
        if len(entry) > 1:
            inventory["pci"].append(entry)

    for base in sorted(glob.glob("/sys/bus/usb/devices/*")):
        entry = {"path": base}
        for key in ("idVendor", "idProduct", "bDeviceClass", "modalias"):
            value = read_text(os.path.join(base, key))
            if value:
                entry[key] = value
        if len(entry) > 1:
            inventory["usb"].append(entry)

    for base in sorted(glob.glob("/sys/bus/acpi/devices/*")):
        entry = {"path": base}
        for key in ("hid", "uid", "status", "modalias"):
            value = read_text(os.path.join(base, key))
            if value:
                entry[key] = value
        if len(entry) > 1:
            inventory["acpi"].append(entry)

    for base in sorted(glob.glob("/sys/bus/i2c/devices/*")):
        entry = {"path": base}
        for key in ("name", "modalias"):
            value = read_text(os.path.join(base, key))
            if value:
                entry[key] = value
        if len(entry) > 1:
            inventory["i2c"].append(entry)

    root = "/sys/firmware/devicetree/base"
    if os.path.isdir(root):
        for base, _dirs, files in os.walk(root):
            if "compatible" not in files:
                continue
            value = read_text(os.path.join(base, "compatible"))
            if value:
                inventory["device_tree"].append({
                    "path": base,
                    "compatible": value.replace("\x00", " ").strip(),
                })
            if len(inventory["device_tree"]) >= 256:
                break
    return inventory


class PortAccessError(OSError):
    pass


class PortIO:
    """Small x86 port-I/O helper with ``ioperm`` and ``/dev/port`` fallbacks."""

    def __init__(self):
        self._fd = None
        self._libc = None
        self._inb = None
        self._outb = None
        self.base = None
        self.length = 0
        self.method = None

    @staticmethod
    def available_reason():
        if platform.machine().lower() not in ("x86_64", "amd64", "i386",
                                              "i686"):
            return "不是 x86/x86_64 架构"
        if os.name != "posix":
            return "当前系统没有 POSIX 端口 I/O"
        return None

    def open(self, base, length=2):
        reason = self.available_reason()
        if reason:
            raise PortAccessError(reason)
        if not (0 <= base <= 0xffff and length > 0 and base + length <= 0x10000):
            raise PortAccessError("非法 I/O 端口范围")

        try:
            libc = ctypes.CDLL(None, use_errno=True)
            ioperm = libc.ioperm
            ioperm.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int)
            ioperm.restype = ctypes.c_int
            if ioperm(base, length, 1) == 0:
                self._libc = libc
                self._inb = libc.inb
                self._outb = libc.outb
                self._inb.argtypes = (ctypes.c_ushort,)
                self._inb.restype = ctypes.c_ubyte
                self._outb.argtypes = (ctypes.c_ubyte, ctypes.c_ushort)
                self._outb.restype = None
                self.method = "ioperm"
        except (AttributeError, OSError):
            pass

        if self.method is None:
            try:
                self._fd = os.open("/dev/port", os.O_RDWR | os.O_CLOEXEC)
                self.method = "/dev/port"
            except OSError as exc:
                raise PortAccessError(
                    "无 CAP_SYS_RAWIO 或 /dev/port 权限：%s" % exc) from exc

        self.base = base
        self.length = length
        return self

    def inb(self, port):
        if self.method == "ioperm":
            return int(self._inb(port))
        os.lseek(self._fd, port, os.SEEK_SET)
        value = os.read(self._fd, 1)
        if len(value) != 1:
            raise PortAccessError("读取 /dev/port 0x%x 失败" % port)
        return value[0]

    def outb(self, port, value):
        if self.method == "ioperm":
            self._outb(int(value) & 0xff, port)
            return
        os.lseek(self._fd, port, os.SEEK_SET)
        written = os.write(self._fd, bytes((int(value) & 0xff,)))
        if written != 1:
            raise PortAccessError("写入 /dev/port 0x%x 失败" % port)

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self.method == "ioperm" and self._libc is not None:
            try:
                self._libc.ioperm(self.base, self.length, 0)
            except OSError:
                pass
        self.method = None

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()


@dataclass
class SuperIoIdentity:
    config_port: int
    vendor: str
    chip_id: int
    model: str
    profile_key: str
    driver: str
    ldn: int
    io_base: int | None
    enabled: bool | None
    revision: int | None = None
    customer_id: int | None = None
    raw_kind: str | None = None
    pwm_count: int = 0
    pwm_indices: tuple[int, ...] | None = None
    rpm16: bool = False
    force_id: int | None = None
    detection_error: str | None = None

    @property
    def device_type(self):
        return "superio-fan-controller"

    @property
    def control_interface(self):
        if self.raw_kind in ("nct6775", "it87", "it8613") and self.io_base:
            return "raw-superio"
        if self.driver:
            return "kernel-hwmon candidate"
        return "identify-only"

    def to_dict(self):
        return {
            "source": "superio",
            "device_type": self.device_type,
            "config_port": "0x%02x" % self.config_port,
            "vendor": self.vendor,
            "chip_id": "0x%04x" % self.chip_id,
            "model": self.model,
            "profile": self.profile_key,
            "driver": self.driver,
            "logical_device": "0x%02x" % self.ldn,
            "io_base": ("0x%x" % self.io_base) if self.io_base else None,
            "enabled": self.enabled,
            "revision": self.revision,
            "customer_id": ("0x%04x" % self.customer_id
                            if self.customer_id is not None else None),
            "raw_kind": self.raw_kind,
            "control_interface": self.control_interface,
            "pwm_count": self.pwm_count,
            "pwm_indices": list(self.pwm_indices or ()),
            "rpm16": self.rpm16,
            "force_id": ("0x%04x" % self.force_id) if self.force_id else None,
            "detection_error": self.detection_error,
        }


#: Linux driver IDs.  Keep these in sync with the probe tables in the
#: corresponding upstream source files, never with motherboard marketing copy.
NCT6775_IDS = {
    0xc450: ("NCT6106D", "nct6775", "nct6106", 3, False),
    0xd280: ("NCT6116D", "nct6775", "nct6116", 5, False),
    0xb470: ("NCT6775F", "nct6775", "nct6775", 3, False),
    0xc330: ("NCT6776D/F", "nct6775", "nct6776", 3, False),
    0xc560: ("NCT6779D", "nct6775", "nct6779", 5, True),
    0xc800: ("NCT6791D", "nct6775", "nct6791", 6, True),
    0xc910: ("NCT6792D", "nct6775", "nct6792", 6, True),
    0xd120: ("NCT6793D", "nct6775", "nct6793", 6, True),
    0xd350: ("NCT6795D", "nct6775", "nct6795", 6, True),
    0xd420: ("NCT6796D", "nct6775", "nct6796", 7, True),
    0xd450: ("NCT6797D", "nct6775", "nct6797", 7, True),
    0xd428: ("NCT6798D", "nct6775", "nct6798", 7, True),
    0xd800: ("NCT6796D-S/NCT6799D-R", "nct6775", "nct6799", 7, True),
}

NCT6683_IDS = {
    0xc730: ("NCT6683D", "nct6683", 8),
    0xd440: ("NCT6686D", "nct6683", 8),
    0xd590: ("NCT6687D", "nct6683", 8),
}

IT87_IDS = {
    0x8613: ("IT8613E", "it87", 5, True),
    0x8689: ("IT8689E", "it87", 6, True),
    0x8712: ("IT8712F", "it87", 3, False),
    0x8705: ("IT8705F", "it87", 3, False),
    0x8716: ("IT8716F", "it87", 3, False),
    0x8718: ("IT8718F", "it87", 3, False),
    0x8720: ("IT8720F", "it87", 3, False),
    0x8721: ("IT8721F", "it87", 3, True),
    0x8726: ("IT8726F", "it87", 3, False),
    0x8728: ("IT8728F", "it87", 3, True),
    0x8732: ("IT8732F", "it87", 4, True),
    0x8733: ("IT8792E/IT8795E", "it87", 3, True),
    0x8771: ("IT8771E", "it87", 3, True),
    0x8772: ("IT8772E", "it87", 3, True),
    0x8781: ("IT8781F", "it87", 3, False),
    0x8782: ("IT8782F", "it87", 3, False),
    0x8783: ("IT8783E/F", "it87", 3, False),
    0x8786: ("IT8786E", "it87", 3, True),
    0x8790: ("IT8790E", "it87", 3, True),
    0x8603: ("IT8603E", "it87", 3, True),
    0x8620: ("IT8620E", "it87", 6, True),
    0x8622: ("IT8622E", "it87", 5, True),
    0x8623: ("IT8623E", "it87", 3, True),
    0x8628: ("IT8628E", "it87", 6, True),
    0x8695: ("IT87952E", "it87", 3, True),
}

FINTEK_IDS = {
    0x0406: ("F71805F/FG", "f71805f", "f71805f"),
    0x0341: ("F71872F/FG or F71806F/FG", "f71805f", "f71805f"),
    0x0901: ("F71808E", "f71882fg", "f71882fg"),
    0x1001: ("F71808A", "f71882fg", "f71882fg"),
    0x0507: ("F71858FG", "f71882fg", "f71882fg"),
    0x0601: ("F71862FG", "f71882fg", "f71882fg"),
    0x1106: ("F71868A", "f71882fg", "f71882fg"),
    0x0814: ("F71869", "f71882fg", "f71882fg"),
    0x1007: ("F71869A", "f71882fg", "f71882fg"),
    0x0541: ("F71882FG", "f71882fg", "f71882fg"),
    0x0723: ("F71889FG", "f71882fg", "f71882fg"),
    0x0909: ("F71889ED", "f71882fg", "f71882fg"),
    0x1005: ("F71889A", "f71882fg", "f71882fg"),
    0x0581: ("F8000", "f71882fg", "f71882fg"),
    0x1210: ("F81768D", "f71882fg", "f71882fg"),
    0x0704: ("F81865F", "f71882fg", "f71882fg"),
    0x1010: ("F81866A", "f71882fg", "f71882fg"),
    0x0903: ("F71858AD", "f71882fg", "f71882fg"),
    0x1502: ("F81966", "f71882fg", "f71882fg"),
    0x1806: ("F81968", "f71882fg", "f71882fg"),
}

WINBOND_IDS = {
    0x8850: ("W83627EHF", "w83627ehf"),
    0x8860: ("W83627EHG", "w83627ehf"),
    0xa020: ("W83627DHG", "w83627ehf"),
    0xb070: ("W83627DHG-P", "w83627ehf"),
    0xa230: ("W83627UHG", "w83627ehf"),
    0xa510: ("W83667HG", "w83627ehf"),
    0xb350: ("W83667HG-B", "w83627ehf"),
}

WINBOND_LEGACY_IDS = {
    0x52: ("W83627HF", "w83627hf"),
    0x82: ("W83627THF", "w83627hf"),
    0x60: ("W83697HF", "w83627hf"),
    0x70: ("W83637HF", "w83627hf"),
    0x85: ("W83687THF", "w83627hf"),
}

SCH56XX_IDS = {
    0xc6: ("SCH5627", "sch5627", 4),
    0xc7: ("SCH5636", "sch5636", 0),
}


def _read_superio_word(io, config_port, reg):
    io.outb(config_port, reg)
    high = io.inb(config_port + 1)
    io.outb(config_port, reg + 1)
    low = io.inb(config_port + 1)
    return (high << 8) | low


def _select_ldn(io, config_port, ldn):
    io.outb(config_port, 0x07)
    io.outb(config_port + 1, ldn)


def _probe_nuvoton(io, config_port):
    io.outb(config_port, 0x87)
    io.outb(config_port, 0x87)
    old_ldn = None
    try:
        io.outb(config_port, 0x07)
        old_ldn = io.inb(config_port + 1)
        chip_id = _read_superio_word(io, config_port, 0x20)
        row = NCT6775_IDS.get(chip_id & 0xfff8)
        nct6683_row = NCT6683_IDS.get(chip_id & 0xfff0)
        if row is None and nct6683_row is None:
            return None
        if nct6683_row is not None:
            model, driver, pwm_count = nct6683_row
            raw_kind = None
            rpm16 = False
            profile_key = "nct6683"
        else:
            model, driver, _kind, pwm_count, rpm16 = row
            raw_kind = "nct6775" if rpm16 else None
            profile_key = "nct6775"
        _select_ldn(io, config_port, 0x0b)
        io.outb(config_port, 0x30)
        enabled_raw = io.inb(config_port + 1)
        io_base = _read_superio_word(io, config_port, 0x60) & 0xfff8
        io.outb(config_port, 0x22)
        revision = io.inb(config_port + 1)
        customer_id = None
        if nct6683_row is not None and io_base:
            customer_id = _read_nct6683_customer(io_base)
        return SuperIoIdentity(
            config_port=config_port,
            vendor="Nuvoton",
            chip_id=chip_id,
            model=model,
            profile_key=profile_key,
            driver=driver,
            ldn=0x0b,
            io_base=((io_base + 4) if nct6683_row is not None else
                     (io_base + 5)) if io_base else None,
            enabled=bool(enabled_raw & 0x01),
            revision=revision,
            customer_id=customer_id,
            raw_kind=raw_kind,
            pwm_count=pwm_count,
            rpm16=rpm16,
            force_id=chip_id,
        )
    finally:
        if old_ldn is not None:
            _select_ldn(io, config_port, old_ldn)
        io.outb(config_port, 0xaa)


def _read_nct6683_customer(io_base):
    """Read the NCT6683 customer ID through its EC page/index/data ports."""
    try:
        with PortIO().open((io_base & 0xfff8) + 4, 4) as io:
            return _nct6683_read16(io, 0x602)
    except (OSError, PortAccessError):
        return None


def _nct6683_read(io, register):
    io.outb(io.base, 0xff)
    io.outb(io.base, register >> 8)
    io.outb(io.base + 1, register & 0xff)
    return io.inb(io.base + 2)


def _nct6683_read16(io, register):
    return ((_nct6683_read(io, register) << 8) |
            _nct6683_read(io, register + 1))


def _probe_ite(io, config_port):
    io.outb(config_port, 0x87)
    io.outb(config_port, 0x01)
    io.outb(config_port, 0x55)
    io.outb(config_port, 0xaa if config_port == 0x4e else 0x55)
    old_ldn = None
    try:
        io.outb(config_port, 0x07)
        old_ldn = io.inb(config_port + 1)
        chip_id = _read_superio_word(io, config_port, 0x20)
        row = IT87_IDS.get(chip_id)
        if row is None:
            return None
        model, driver, pwm_count, newer = row
        pwm_indices = None
        if chip_id == 0x8613:
            _select_ldn(io, config_port, 0x07)
            io.outb(config_port, 0x27)
            reg27 = io.inb(config_port + 1)
            io.outb(config_port, 0x29)
            reg29 = io.inb(config_port + 1)
            io.outb(config_port, 0x2a)
            reg2a = io.inb(config_port + 1)
            skip_pwm = 0x01  # IT8613E does not expose PWM1.
            if reg27 & 0x08:
                skip_pwm |= 0x04
            if reg27 & 0x40:
                skip_pwm |= 0x10
            if reg29 & 0x02:
                skip_pwm |= 0x02
            if not (reg2a & 0x01) or (reg29 & 0x80):
                skip_pwm |= 0x08
            pwm_indices = tuple(
                index for index in range(pwm_count)
                if not skip_pwm & (1 << index))
        elif chip_id == 0x8689:
            _select_ldn(io, config_port, 0x07)
            io.outb(config_port, 0x25)
            reg25 = io.inb(config_port + 1)
            io.outb(config_port, 0x27)
            reg27 = io.inb(config_port + 1)
            io.outb(config_port, 0x28)
            reg28 = io.inb(config_port + 1)
            io.outb(config_port, 0x29)
            reg29 = io.inb(config_port + 1)
            skip_pwm = 0
            if reg25 & 0x40:
                skip_pwm |= 0x10
            if reg27 & 0x40:
                skip_pwm |= 0x04
            if reg28 & 0x04:
                skip_pwm |= 0x08
            if reg29 & 0x02:
                skip_pwm |= 0x02
            if not reg29 & 0x80:
                skip_pwm |= 0x20
            pwm_indices = tuple(
                index for index in range(pwm_count)
                if not skip_pwm & (1 << index))
        _select_ldn(io, config_port, 0x04)
        io.outb(config_port, 0x30)
        enabled_raw = io.inb(config_port + 1)
        io_base = _read_superio_word(io, config_port, 0x60) & 0xfff8
        io.outb(config_port, 0x22)
        revision = io.inb(config_port + 1) & 0x0f
        return SuperIoIdentity(
            config_port=config_port,
            vendor="ITE",
            chip_id=chip_id,
            model=model,
            profile_key="it87",
            driver=driver,
            ldn=0x04,
            io_base=io_base or None,
            enabled=bool(enabled_raw & 0x01),
            revision=revision,
            raw_kind="it8613" if chip_id == 0x8613 else "it87",
            pwm_count=pwm_count,
            pwm_indices=pwm_indices,
            rpm16=newer,
            force_id=chip_id,
        )
    finally:
        if old_ldn is not None:
            _select_ldn(io, config_port, old_ldn)
        io.outb(config_port, 0x02)
        io.outb(config_port + 1, 0x02)


def _probe_fintek(io, config_port):
    io.outb(config_port, 0x87)
    io.outb(config_port, 0x87)
    old_ldn = None
    try:
        io.outb(config_port, 0x07)
        old_ldn = io.inb(config_port + 1)
        manufacturer = _read_superio_word(io, config_port, 0x23)
        if manufacturer != 0x1934:
            return None
        chip_id = _read_superio_word(io, config_port, 0x20)
        row = FINTEK_IDS.get(chip_id)
        if row is None:
            return None
        model, driver, profile_key = row
        ldn = 0x02 if chip_id in (0x0507, 0x0903) else 0x04
        _select_ldn(io, config_port, ldn)
        io.outb(config_port, 0x30)
        enabled_raw = io.inb(config_port + 1)
        io_base = _read_superio_word(io, config_port, 0x60) & 0xfff8
        io.outb(config_port, 0x22)
        revision = io.inb(config_port + 1)
        return SuperIoIdentity(
            config_port=config_port,
            vendor="Fintek",
            chip_id=chip_id,
            model=model,
            profile_key=profile_key,
            driver=driver,
            ldn=ldn,
            io_base=io_base or None,
            enabled=bool(enabled_raw & 0x01),
            revision=revision,
            force_id=chip_id,
        )
    finally:
        if old_ldn is not None:
            _select_ldn(io, config_port, old_ldn)
        io.outb(config_port, 0xaa)


def _probe_winbond(io, config_port):
    io.outb(config_port, 0x87)
    io.outb(config_port, 0x87)
    old_ldn = None
    try:
        io.outb(config_port, 0x07)
        old_ldn = io.inb(config_port + 1)
        chip_id = _read_superio_word(io, config_port, 0x20)
        row = WINBOND_IDS.get(chip_id & 0xfff0)
        if row is None:
            return None
        model, driver = row
        _select_ldn(io, config_port, 0x0b)
        io.outb(config_port, 0x30)
        enabled_raw = io.inb(config_port + 1)
        io_base = _read_superio_word(io, config_port, 0x60) & 0xfff8
        io.outb(config_port, 0x22)
        revision = io.inb(config_port + 1)
        return SuperIoIdentity(
            config_port=config_port,
            vendor="Winbond/Nuvoton",
            chip_id=chip_id,
            model=model,
            profile_key="w83627ehf",
            driver=driver,
            ldn=0x0b,
            io_base=io_base or None,
            enabled=bool(enabled_raw & 0x01),
            revision=revision,
            force_id=chip_id,
        )
    finally:
        if old_ldn is not None:
            _select_ldn(io, config_port, old_ldn)
        io.outb(config_port, 0xaa)
        io.outb(config_port, 0x02)
        io.outb(config_port + 1, 0x02)


def _probe_winbond_legacy(io, config_port):
    """Probe the older W83627HF/THF/97HF/37HF/87THF 8-bit ID space."""
    io.outb(config_port, 0x87)
    io.outb(config_port, 0x87)
    old_ldn = None
    try:
        io.outb(config_port, 0x07)
        old_ldn = io.inb(config_port + 1)
        io.outb(config_port, 0x20)
        chip_id = io.inb(config_port + 1)
        row = WINBOND_LEGACY_IDS.get(chip_id)
        if row is None:
            return None
        model, driver = row
        _select_ldn(io, config_port, 0x0b)
        io.outb(config_port, 0x30)
        enabled_raw = io.inb(config_port + 1)
        io.outb(config_port, 0x60)
        high = io.inb(config_port + 1)
        io.outb(config_port, 0x61)
        low = io.inb(config_port + 1)
        io_base = ((high << 8) | low) & 0xfff8
        io.outb(config_port, 0x22)
        revision = io.inb(config_port + 1)
        return SuperIoIdentity(
            config_port=config_port,
            vendor="Winbond",
            chip_id=chip_id,
            model=model,
            profile_key="w83627hf",
            driver=driver,
            ldn=0x0b,
            io_base=io_base or None,
            enabled=bool(enabled_raw & 0x01),
            revision=revision,
            force_id=chip_id,
        )
    finally:
        if old_ldn is not None:
            _select_ldn(io, config_port, old_ldn)
        io.outb(config_port, 0xaa)
        io.outb(config_port, 0x02)
        io.outb(config_port + 1, 0x02)


def _probe_sch56xx(io, config_port):
    """Probe SCH5627/SCH5636 through the SMSC EC logical device."""
    io.outb(config_port, 0x55)
    old_ldn = None
    try:
        io.outb(config_port, 0x07)
        old_ldn = io.inb(config_port + 1)
        io.outb(config_port, 0x20)
        chip_id = io.inb(config_port + 1)
        row = SCH56XX_IDS.get(chip_id)
        if row is None:
            return None
        model, driver, pwm_count = row
        _select_ldn(io, config_port, 0x0c)
        io.outb(config_port, 0x30)
        enabled_raw = io.inb(config_port + 1)
        # Unlike most Super-I/O chips, SCH56xx stores address low byte first.
        io.outb(config_port, 0x66)
        low = io.inb(config_port + 1)
        io.outb(config_port, 0x67)
        high = io.inb(config_port + 1)
        io_base = low | (high << 8)
        return SuperIoIdentity(
            config_port=config_port,
            vendor="SMSC",
            chip_id=chip_id,
            model=model,
            profile_key="sch56xx",
            driver=driver,
            ldn=0x0c,
            io_base=io_base or None,
            enabled=bool(enabled_raw & 0x01),
            raw_kind=None,
            pwm_count=pwm_count,
            force_id=chip_id,
        )
    finally:
        if old_ldn is not None:
            _select_ldn(io, config_port, old_ldn)
        io.outb(config_port, 0xaa)


def scan_superio():
    """Read the IDs of supported Super-I/O fan controllers."""
    result = {"devices": [], "problems": []}
    reason = PortIO.available_reason()
    if reason:
        result["problems"].append(reason)
        return result

    for config_port in (0x2e, 0x4e):
        try:
            with PortIO().open(config_port, 2) as io:
                for probe in (_probe_nuvoton, _probe_ite, _probe_fintek,
                              _probe_winbond, _probe_winbond_legacy,
                              _probe_sch56xx):
                    try:
                        found = probe(io, config_port)
                    except (OSError, PortAccessError) as exc:
                        result["problems"].append(
                            "0x%02x %s 探测失败：%s"
                            % (config_port, probe.__name__, exc))
                        continue
                    if found is not None:
                        result["devices"].append(found)
                        break
        except (OSError, PortAccessError) as exc:
            result["problems"].append("SuperIO 0x%02x 不可访问：%s"
                                      % (config_port, exc))
    return result


def identify_hardware(include_superio=True):
    """Return the full evidence used by active detection and diagnostics."""
    inventory = collect_bus_inventory()
    inventory["superio"] = (
        scan_superio() if include_superio else {"devices": [], "problems": []})
    return inventory


def public_inventory(inventory):
    """Return a JSON-safe copy of an inventory snapshot."""
    if not inventory:
        return inventory
    public = {
        "dmi": dict(inventory.get("dmi") or {}),
        "pci": list(inventory.get("pci") or []),
        "usb": list(inventory.get("usb") or []),
        "acpi": list(inventory.get("acpi") or []),
        "i2c": list(inventory.get("i2c") or []),
        "device_tree": list(inventory.get("device_tree") or []),
        "problems": list(inventory.get("problems") or []),
    }
    superio = inventory.get("superio") or {}
    devices = []
    for item in superio.get("devices") or []:
        devices.append(item.to_dict() if hasattr(item, "to_dict") else item)
    public["superio"] = {
        "devices": devices,
        "problems": list(superio.get("problems") or []),
    }
    return public


def _best_superio(inventory):
    for item in inventory.get("superio", {}).get("devices") or []:
        if item.raw_kind in ("nct6775", "it87", "it8613") and item.io_base:
            return item
    for item in inventory.get("superio", {}).get("devices") or []:
        return item
    return None


def select_controller(inventory):
    """Pick the best controllable identity from an inventory snapshot."""
    item = _best_superio(inventory)
    if item is None:
        return None
    if item.raw_kind in ("nct6775", "it87", "it8613") and item.io_base:
        return item
    return item


class RawSuperIoController:
    """Direct Super-I/O control for NCT6779+ and IT87 family chips.

    The register layouts mirror the upstream Linux drivers.  Every access is
    serialised with a process lock file and every configuration register touched
    during setup is restored on :meth:`close`.
    """

    NCT_FAN_MODE = 0x102
    NCT_PWM = 0x109
    NCT_FAN = 0x4c0
    NCT_TEMP_SEL = 0x100
    NCT_BANK = 0x4e
    IT_PME = 0x04
    IT_CONFIG = 0x00
    IT_FAN_MAIN_CTRL = 0x13
    IT_FAN_CTL = 0x14
    IT_PWM = (0x15, 0x16, 0x17, 0x7f, 0xa7, 0xaf)
    IT8613_PWM = (0x15, 0x16, 0x17, 0x1e, 0x1f)
    IT_PWM_DUTY = (0x63, 0x6b, 0x73, 0x7b, 0xa3, 0xab)
    IT_FAN = (0x0d, 0x0e, 0x0f, 0x80, 0x82, 0x4c)
    IT_FAN_X = (0x18, 0x19, 0x1a, 0x81, 0x83, 0x4d)
    IT_FAN_DIV = 0x0b

    def __init__(self, identity, profile):
        self.identity = identity
        self.profile = profile
        self.kind = identity.raw_kind
        self.chip = identity.model
        self.io = None
        self._lock_path = "/tmp/niufan-hardware.lock"
        self._lock_file = None
        self._config = {}
        self._original = {}
        self._bank = None
        self._closed = False
        self._open()

    def _lock(self):
        if fcntl is None:
            raise PortAccessError("当前 Python 没有 fcntl，不能安全启用直接 I/O")
        try:
            self._lock_file = open(self._lock_path, "a+")
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self._lock_file:
                self._lock_file.close()
                self._lock_file = None
            raise PortAccessError("另一个硬件控制进程正在运行：%s" % exc) from exc

    def _open(self):
        self._lock()
        try:
            if self.kind == "nct6775":
                self._open_nct()
            elif self.kind in ("it87", "it8613"):
                self._open_it87()
            else:
                raise PortAccessError("%s 没有内置直接寄存器后端"
                                      % self.identity.model)
        except Exception:
            if self._lock_file is not None:
                try:
                    fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                    self._lock_file.close()
                except OSError:
                    pass
                self._lock_file = None
            raise

    def _open_nct(self):
        config = PortIO().open(self.identity.config_port, 2)
        try:
            config.outb(self.identity.config_port, 0x87)
            config.outb(self.identity.config_port, 0x87)
            config.outb(self.identity.config_port, 0x07)
            old_ldn = config.inb(self.identity.config_port + 1)
            _select_ldn(config, self.identity.config_port, 0x0b)
            config.outb(self.identity.config_port, 0x30)
            old_enable = config.inb(self.identity.config_port + 1)
            old_lock = None
            if self.identity.chip_id >= 0xc800:
                config.outb(self.identity.config_port, 0x28)
                old_lock = config.inb(self.identity.config_port + 1)
                if old_lock & 0x10:
                    config.outb(self.identity.config_port, 0x28)
                    config.outb(self.identity.config_port + 1,
                                old_lock & ~0x10)
            if not old_enable & 0x01:
                config.outb(self.identity.config_port, 0x30)
                config.outb(self.identity.config_port + 1, old_enable | 0x01)
            base = _read_superio_word(config, self.identity.config_port, 0x60)
            self._config = {
                "config_port": self.identity.config_port,
                "old_ldn": old_ldn,
                "old_enable": old_enable,
                "old_lock": old_lock,
            }
        finally:
            config.outb(self.identity.config_port, 0xaa)
            config.close()
        self.io = PortIO().open((base & 0xfff8) + 5, 2)
        self._bank = None

    def _open_it87(self):
        config = PortIO().open(self.identity.config_port, 2)
        try:
            config.outb(self.identity.config_port, 0x87)
            config.outb(self.identity.config_port, 0x01)
            config.outb(self.identity.config_port, 0x55)
            config.outb(self.identity.config_port,
                        0xaa if self.identity.config_port == 0x4e else 0x55)
            config.outb(self.identity.config_port, 0x07)
            old_ldn = config.inb(self.identity.config_port + 1)
            _select_ldn(config, self.identity.config_port, self.IT_PME)
            config.outb(self.identity.config_port, 0x30)
            old_enable = config.inb(self.identity.config_port + 1)
            if not old_enable & 0x01:
                config.outb(self.identity.config_port, 0x30)
                config.outb(self.identity.config_port + 1, old_enable | 0x01)
            base = _read_superio_word(config, self.identity.config_port, 0x60)
            self._config = {
                "config_port": self.identity.config_port,
                "old_ldn": old_ldn,
                "old_enable": old_enable,
            }
        finally:
            config.outb(self.identity.config_port, 0x02)
            config.outb(self.identity.config_port + 1, 0x02)
            config.close()
        # The Super-I/O base points at the ISA register block; the
        # Environmental Controller window starts at +5, matching it87.c's
        # IT87_EC_OFFSET.
        self.io = PortIO().open((base & 0xfff8) + 5, 2)
        self._bank = None

    def _nct_set_bank(self, register):
        bank = (register >> 8) & 0xff
        if self._bank != bank:
            self.io.outb(self.io.base, self.NCT_BANK)
            self.io.outb(self.io.base + 1, bank)
            self._bank = bank

    def _nct_read(self, register):
        self._nct_set_bank(register)
        self.io.outb(self.io.base, register & 0xff)
        return self.io.inb(self.io.base + 1)

    def _nct_write(self, register, value):
        self._nct_set_bank(register)
        self.io.outb(self.io.base, register & 0xff)
        self.io.outb(self.io.base + 1, value & 0xff)

    def _nct_read_word(self, register):
        return (self._nct_read(register) << 8) | self._nct_read(register + 1)

    def _it_read(self, register):
        self.io.outb(self.io.base, register)
        return self.io.inb(self.io.base + 1)

    def _it_write(self, register, value):
        self.io.outb(self.io.base, register)
        self.io.outb(self.io.base + 1, value & 0xff)

    def _it_slot(self, index):
        slot = int(index) - 1
        if slot < 0 or slot >= self.identity.pwm_count:
            raise OSError(errno.EINVAL, "PWM 通道不存在：%s" % index)
        return slot

    def _it_control_register(self, index):
        slot = self._it_slot(index)
        if self.kind == "it8613":
            return self.IT8613_PWM[slot]
        return self.IT_PWM[slot]

    def read_rpm(self, index):
        try:
            if self.kind == "nct6775":
                slot = int(index) - 1
                if slot < 0 or slot >= self.identity.pwm_count:
                    return None
                return self._nct_read_word(self.NCT_FAN + slot * 2) or None
            slot = self._it_slot(index)
            low = self._it_read(self.IT_FAN[slot])
            if self.identity.rpm16:
                raw = low | (self._it_read(self.IT_FAN_X[slot]) << 8)
                return (1350000 // (raw * 2)) if raw not in (0, 0xffff) else None
            div = 1 << ((self._it_read(self.IT_FAN_DIV) >> slot) & 0x07)
            return (1350000 // (low * div)) if low else None
        except (OSError, IndexError, ZeroDivisionError):
            return None

    def read_duty(self, index):
        if self.kind == "nct6775":
            slot = self._it_slot(index)
            return self._nct_read(self.NCT_PWM + slot * 0x100)
        slot = self._it_slot(index)
        if self.identity.rpm16:
            return self._it_read(self.IT_PWM_DUTY[slot])
        return (self._it_read(self.IT_PWM[slot]) & 0x7f) << 1

    def read_enable(self, index):
        if self.kind == "nct6775":
            slot = self._it_slot(index)
            mode = (self._nct_read(self.NCT_FAN_MODE + slot * 0x100) >> 4) & 0x07
            return 0 if mode == 0 and self.read_duty(index) == 255 else mode + 1
        control = self._it_read(self._it_control_register(index))
        return 2 if control & 0x80 else 1

    def read_mode(self, index):
        return 1

    def read_temp_sel(self, index):
        if self.kind != "nct6775":
            return None
        slot = self._it_slot(index)
        return self._nct_read(self.NCT_TEMP_SEL + slot * 0x100) & 0x07

    def read_temp_sel_label(self, index):
        selector = self.read_temp_sel(index)
        if selector is None:
            return None
        return "temp%d" % selector

    def write_duty(self, index, duty):
        value = max(0, min(255, int(duty)))
        if self.kind == "nct6775":
            slot = self._it_slot(index)
            self._nct_write(self.NCT_PWM + slot * 0x100, value)
            return
        slot = self._it_slot(index)
        if self.identity.rpm16:
            self._it_write(self.IT_PWM_DUTY[slot], value)
        else:
            self._it_write(self.IT_PWM[slot], value >> 1)

    def write_enable(self, index, enable):
        value = int(enable)
        if self.kind == "nct6775":
            mode = 0 if value == 1 else (4 if value == 5 else None)
            if mode is None:
                raise OSError(errno.EINVAL,
                              "NCT67xx 用户态后端只支持手动和 Smart Fan IV")
            slot = self._it_slot(index)
            register = self.NCT_FAN_MODE + slot * 0x100
            old = self._nct_read(register)
            self._nct_write(register, (old & 0x0f) | (mode << 4))
            return
        if value not in (1, 2):
            raise OSError(errno.EINVAL,
                          "IT87 用户态后端只支持手动和自动模式")
        slot = self._it_slot(index)
        register = self._it_control_register(index)
        control = self._it_read(register)
        if self.kind == "it8613":
            control = (control & ~0x80) if value == 1 else (control | 0x80)
            self._it_write(register, control)
            return
        if value == 1:
            if self.identity.rpm16:
                control &= ~0x80
                self._it_write(register, control)
            else:
                self._it_write(register, self.read_duty(index) >> 1)
            if slot < 3:
                self._it_write(
                    self.IT_FAN_MAIN_CTRL,
                    self._it_read(self.IT_FAN_MAIN_CTRL) | (1 << slot))
        else:
            if self.identity.rpm16:
                self._it_write(register, control | 0x80)
            else:
                self._it_write(register, 0x80)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.io:
            self.io.close()
        if self.kind == "nct6775":
            self._close_nct()
        elif self.kind in ("it87", "it8613"):
            self._close_it87()
        if self._lock_file:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
            except OSError:
                pass

    def _close_nct(self):
        state = self._config
        if not state:
            return
        try:
            with PortIO().open(state["config_port"], 2) as config:
                config.outb(state["config_port"], 0x87)
                config.outb(state["config_port"], 0x87)
                _select_ldn(config, state["config_port"], 0x0b)
                if state.get("old_lock") is not None:
                    config.outb(state["config_port"], 0x28)
                    config.outb(state["config_port"] + 1, state["old_lock"])
                config.outb(state["config_port"], 0x30)
                config.outb(state["config_port"] + 1, state["old_enable"])
                _select_ldn(config, state["config_port"], state["old_ldn"])
        except (OSError, PortAccessError):
            pass
        finally:
            try:
                with PortIO().open(state["config_port"], 2) as config:
                    config.outb(state["config_port"], 0xaa)
            except (OSError, PortAccessError):
                pass

    def _close_it87(self):
        state = self._config
        if not state:
            return
        try:
            with PortIO().open(state["config_port"], 2) as config:
                config.outb(state["config_port"], 0x87)
                config.outb(state["config_port"], 0x01)
                config.outb(state["config_port"], 0x55)
                config.outb(state["config_port"],
                            0xaa if state["config_port"] == 0x4e else 0x55)
                _select_ldn(config, state["config_port"], self.IT_PME)
                config.outb(state["config_port"], 0x30)
                config.outb(state["config_port"] + 1, state["old_enable"])
                _select_ldn(config, state["config_port"], state["old_ldn"])
                config.outb(state["config_port"], 0x02)
                config.outb(state["config_port"] + 1, 0x02)
        except (OSError, PortAccessError):
            pass
