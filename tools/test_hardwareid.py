#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
"""Unit checks for active hardware identification."""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SERVER = os.path.join(ROOT, "package", "app", "server")
sys.path.insert(0, SERVER)

import hardwareid  # noqa: E402
import fanconfig  # noqa: E402
import fanhardware  # noqa: E402
import fancontrold  # noqa: E402


class Check:
    def __init__(self):
        self.problems = []

    def case(self, condition, message):
        print(("  ok    " if condition else "  FAIL  ") + message)
        if not condition:
            self.problems.append(message)

    def finish(self):
        print("主动硬件识别检查: %s" % (
            "全部通过" if not self.problems
            else "%d 项不通过" % len(self.problems)))
        return 1 if self.problems else 0


class FakePortIO:
    """A minimal NCT6796D Super-I/O model, including config registers."""

    opened = []
    registers = {
        0x20: 0xd4,
        0x21: 0x20,
        0x22: 0x11,
        0x30: 0x01,
        0x60: 0x02,
        0x61: 0x90,
    }

    def __init__(self):
        self.base = None
        self.selected = None
        self.data_port = None
        self.pending = None
        self.hwm_pending = None
        self.bank = 0
        self.mode = None
        self.hwm = {
            (1, 0x09): 128,
            (4, 0xc0): 0x04,
            (4, 0xc1): 0xd0,
            (0, 0x63): 128,
            (0, 0x0d): 0xd0,
            (0, 0x18): 0x04,
        }

    @staticmethod
    def available_reason():
        return None

    def open(self, base, length=2):
        type(self).opened.append(base)
        self.base = base
        self.data_port = base + 1
        self.mode = "config" if base in (0x2e, 0x4e) else "hwm"
        return self

    def outb(self, port, value):
        if self.mode == "config":
            if port == self.data_port:
                if self.selected == 0x07:
                    self.selected = value
                else:
                    self.registers[self.selected] = value
            elif port == self.base:
                self.selected = value
        elif port == self.base:
            self.hwm_pending = value
        elif port == self.data_port:
            if self.hwm_pending == 0x4e:
                self.bank = value
            else:
                self.hwm[(self.bank, self.hwm_pending)] = value

    def inb(self, port):
        if self.mode == "config" and port == self.data_port:
            return self.registers.get(self.selected, 0xff)
        if self.mode == "hwm" and port == self.data_port:
            return self.hwm.get((self.bank, self.hwm_pending), 0)
        return 0xff

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()


class FakeIT8613PortIO(FakePortIO):
    """A minimal IT8613E Super-I/O model."""

    registers = {
        0x20: 0x86,
        0x21: 0x13,
        0x22: 0x10,
        0x27: 0x00,
        0x29: 0x00,
        0x2a: 0x01,
        0x30: 0x01,
        0x60: 0x0a,
        0x61: 0x00,
    }

    def __init__(self):
        super().__init__()
        self.hwm.update({
            (0, 0x16): 0x80,
            (0, 0x6b): 128,
            (0, 0x0e): 0xd0,
            (0, 0x19): 0x04,
        })


class FakeIT8689PortIO(FakePortIO):
    """A minimal IT8689E Super-I/O model with all six PWM pins enabled."""

    registers = {
        0x20: 0x86,
        0x21: 0x89,
        0x22: 0x10,
        0x25: 0x00,
        0x27: 0x00,
        0x28: 0x00,
        0x29: 0x80,
        0x30: 0x01,
        0x60: 0x0a,
        0x61: 0x00,
    }


class FakeF71805FPortIO(FakePortIO):
    registers = {
        0x20: 0x04,
        0x21: 0x06,
        0x23: 0x19,
        0x24: 0x34,
        0x30: 0x01,
        0x60: 0x0a,
        0x61: 0x00,
    }


class FakeF81968PortIO(FakePortIO):
    registers = {
        0x20: 0x18,
        0x21: 0x06,
        0x23: 0x19,
        0x24: 0x34,
        0x30: 0x01,
        0x60: 0x0a,
        0x61: 0x00,
    }


class FakeLegacyWinbondPortIO(FakePortIO):
    registers = {
        0x20: 0x52,
        0x30: 0x01,
        0x60: 0x02,
        0x61: 0x00,
    }


class FakeSCH5627PortIO(FakePortIO):
    registers = {
        0x20: 0xc6,
        0x30: 0x01,
        0x66: 0x00,
        0x67: 0x0a,
    }


def scan_with(fake):
    original = hardwareid.PortIO
    try:
        hardwareid.PortIO = fake
        return hardwareid.scan_superio()
    finally:
        hardwareid.PortIO = original


def main():
    check = Check()
    original = hardwareid.PortIO
    try:
        hardwareid.PortIO = FakePortIO
        result = hardwareid.scan_superio()
    finally:
        hardwareid.PortIO = original

    devices = result.get("devices") or []
    check.case(bool(devices), "SuperIO 探测得到芯片（%d 个端口响应）"
               % len(devices))
    if devices:
        item = devices[0]
        check.case(item.model == "NCT6796D",
                   "设备 ID 0xd420 映射到 NCT6796D（%s）" % item.model)
        check.case(item.profile_key == "nct6775",
                   "映射到 nct6775 驱动家族")
        check.case(item.raw_kind == "nct6775",
                   "NCT6796D 标记为可进入直接寄存器后端")
        check.case(item.io_base == 0x295,
                   "HWM 基址按内核驱动规则加 5（%s）"
                   % ("0x%x" % item.io_base if item.io_base else None))
        payload = item.to_dict()
        check.case(payload["chip_id"] == "0xd420",
                   "诊断输出保留原始设备 ID")
        check.case(payload["device_type"] == "superio-fan-controller"
                   and payload["control_interface"] == "raw-superio",
                   "设备 ID 输出设备类型与控制接口")
    else:
        check.case(False, "SuperIO 探测得到一个芯片")
        check.case(False, "设备 ID 0xd420 映射到 NCT6796D")
        item = None

    inventory = {
        "dmi": {"board_vendor": "Test"},
        "pci": [],
        "usb": [],
        "acpi": [],
        "i2c": [],
        "device_tree": [],
        "problems": [],
        "superio": result,
    }
    public = hardwareid.public_inventory(inventory)
    try:
        encoded = json.dumps(public, ensure_ascii=False)
        check.case("NCT6796D" in encoded, "主动识别结果可安全序列化为 JSON")
    except (TypeError, ValueError) as exc:
        check.case(False, "主动识别结果可安全序列化为 JSON（%s）" % exc)

    bus = hardwareid.collect_bus_inventory()
    for key in ("dmi", "pci", "usb", "acpi", "i2c", "device_tree",
                "problems"):
        check.case(key in bus, "总线清单包含 %s" % key)

    check.case(
        fanconfig.normalise({"driver_preference": "software"})
        .get("driver_preference") == "software",
        "驱动偏好可持久化到配置")

    class FakeDmesg:
        returncode = 0
        stdout = (
            b"ACPI: resource it87 [io  0x0290-0x0297] conflicts with ACPI region\n"
            b"it87: Found IT8613E chip at 0x290\n"
        )

    original_run = fanhardware.subprocess.run
    try:
        fanhardware.subprocess.run = lambda *args, **kwargs: FakeDmesg()
        conflicts = fanhardware.kernel_conflict_lines()
        check.case(len(conflicts) == 1 and "conflicts" in conflicts[0],
                   "内核日志能提取 ACPI / SuperIO 资源冲突")
    finally:
        fanhardware.subprocess.run = original_run

    if item is not None:
        profile = fanhardware.profile_by_key(item.profile_key)
        original = hardwareid.PortIO
        try:
            hardwareid.PortIO = FakePortIO
            FakePortIO.opened.clear()
            raw = hardwareid.RawSuperIoController(item, profile)
            check.case(0x295 in FakePortIO.opened,
                       "NCT 直接后端访问 EC 偏移后的端口 0x295")
            check.case(raw.read_duty(1) == 128,
                       "直接后端能读取 PWM 占空比")
            raw.write_enable(1, profile.manual)
            check.case(raw.read_enable(1) == profile.manual,
                       "直接后端能进入手动模式")
            raw.write_duty(1, 200)
            check.case(raw.read_duty(1) == 200,
                       "直接后端能写入 PWM 占空比")
            check.case(raw.read_rpm(1) == 1232,
                       "直接后端能读取转速寄存器")
            raw.close()
            check.case(True, "直接后端可以关闭并恢复配置寄存器")
        except (OSError, TypeError) as exc:
            check.case(False, "直接后端控制流程：%s" % exc)
        finally:
            hardwareid.PortIO = original

        saved_modules = (
            fanhardware.hwmon_controller,
            fanhardware.refresh_hardware_identity,
            fanhardware.kernel_conflict_lines,
            fanhardware.load_module,
            fanhardware.find_controller,
        )
        calls = []
        try:
            fanhardware.hwmon_controller = lambda: (None, None, None)
            fanhardware.refresh_hardware_identity = lambda _log=None: None
            fanhardware.kernel_conflict_lines = lambda: [
                "ACPI: resource conflict with it87"]
            fanhardware.find_controller = lambda *_args, **_kwargs: (
                None, None, None)

            def fake_load(name, log=None, options=()):
                calls.append((name, tuple(options)))
                return name == "it87" and options == (
                    "ignore_resource_conflict=1",)

            fanhardware.load_module = fake_load
            fanhardware.ensure_modules()
            check.case(("it87", ()) in calls
                       and ("it87", ("ignore_resource_conflict=1",)) in calls,
                       "ACPI 冲突时自动重试 it87 ignore_resource_conflict=1")
        finally:
            (fanhardware.hwmon_controller,
             fanhardware.refresh_hardware_identity,
             fanhardware.kernel_conflict_lines,
             fanhardware.load_module,
             fanhardware.find_controller) = saved_modules

        it_profile = fanhardware.profile_by_key("it87")
        it_identity = hardwareid.SuperIoIdentity(
            config_port=0x2e, vendor="ITE", chip_id=0x8786,
            model="IT8786E", profile_key="it87", driver="it87", ldn=0x04,
            io_base=0x290, enabled=True, revision=1, raw_kind="it87",
            pwm_count=3, rpm16=True, force_id=0x8786)
        original = hardwareid.PortIO
        try:
            hardwareid.PortIO = FakePortIO
            FakePortIO.opened.clear()
            raw = hardwareid.RawSuperIoController(it_identity, it_profile)
            check.case(0x295 in FakePortIO.opened,
                       "IT87 直接后端访问 EC 偏移后的端口 0x295")
            check.case(raw.read_duty(1) == 128,
                       "IT87 直接后端能读取 PWM 占空比")
            raw.write_enable(1, it_profile.manual)
            raw.write_duty(1, 180)
            check.case(raw.read_enable(1) == it_profile.manual
                       and raw.read_duty(1) == 180,
                       "IT87 直接后端能写入手动模式与占空比")
            check.case(raw.read_rpm(1) == 547,
                       "IT87 直接后端能读取 16 位转速")
            raw.close()
        except (OSError, TypeError) as exc:
            check.case(False, "IT87 直接后端控制流程：%s" % exc)
        finally:
            hardwareid.PortIO = original

        nct6683_profile = fanhardware.profile_by_key("nct6683")
        nct6683_identity = hardwareid.SuperIoIdentity(
            config_port=0x2e, vendor="Nuvoton", chip_id=0xd590,
            model="NCT6687D", profile_key="nct6683", driver="nct6683",
            ldn=0x0b, io_base=0x0a04, enabled=True, customer_id=0x0805,
            raw_kind=None, pwm_count=8, rpm16=False)
        source = fanhardware.RawControllerSource(
            nct6683_identity, nct6683_profile)
        check.case(fanhardware.fan_channels(nct6683_profile, source) == [],
                   "NCT6683 仅识别不控制时不枚举 raw PWM 通道")

        original = hardwareid.PortIO
        try:
            hardwareid.PortIO = FakeIT8613PortIO
            it8613_result = hardwareid.scan_superio()
        finally:
            hardwareid.PortIO = original
        it8613_devices = it8613_result.get("devices") or []
        check.case(bool(it8613_devices),
                   "IT8613E SuperIO 探测得到芯片")
        if it8613_devices:
            it8613 = it8613_devices[0]
            check.case(it8613.model == "IT8613E"
                       and it8613.chip_id == 0x8613,
                       "设备 ID 0x8613 映射到 IT8613E")
            check.case(it8613.profile_key == "it87"
                       and it8613.raw_kind == "it8613",
                       "IT8613E 映射到 IT87 家族和直接后端")
            check.case(it8613.pwm_indices == (1, 2, 3, 4),
                       "IT8613E 按 GPIO 复用隐藏 PWM1（%s）"
                       % (it8613.pwm_indices,))
        else:
            check.case(False, "IT8613E 设备 ID 0x8613 映射到 IT8613E")
            it8613 = None

        if it8613 is not None:
            original = hardwareid.PortIO
            try:
                hardwareid.PortIO = FakeIT8613PortIO
                FakePortIO.opened.clear()
                raw = hardwareid.RawSuperIoController(
                    it8613, fanhardware.profile_by_key("it87"))
                check.case(0xa05 in FakePortIO.opened,
                           "IT8613E 直接后端访问 EC 偏移后的端口 0xA05")
                check.case(raw.read_duty(2) == 128,
                           "IT8613E 直接后端能读取 PWM2 占空比")
                raw.write_enable(2, 1)
                raw.write_duty(2, 180)
                check.case(raw.read_enable(2) == 1
                           and raw.read_duty(2) == 180,
                           "IT8613E 直接后端能写入手动模式与占空比")
                check.case(raw.read_rpm(2) == 547,
                           "IT8613E 直接后端能读取 16 位转速")
                raw.write_enable(2, 2)
                check.case(raw.read_enable(2) == 2,
                           "IT8613E 直接后端能切换自动模式")
                raw.close()
                source = fanhardware.RawControllerSource(
                    it8613, fanhardware.profile_by_key("it87"))
                channels = fanhardware.fan_channels(
                    fanhardware.profile_by_key("it87"), source)
                check.case([channel.index for channel in channels]
                           == [2, 3, 4, 5],
                           "IT8613E 只暴露 PWM2-5 四个硬件通道")
            except (OSError, TypeError, IndexError) as exc:
                check.case(False, "IT8613E 直接后端控制流程：%s" % exc)
            finally:
                hardwareid.PortIO = original

        it8689_result = scan_with(FakeIT8689PortIO)
        it8689_devices = it8689_result.get("devices") or []
        check.case(bool(it8689_devices) and it8689_devices[0].model == "IT8689E",
                   "设备 ID 0x8689 映射到 IT8689E")
        if it8689_devices:
            check.case(it8689_devices[0].profile_key == "it87"
                       and it8689_devices[0].raw_kind == "it87"
                       and it8689_devices[0].pwm_indices
                       == (0, 1, 2, 3, 4, 5),
                       "IT8689E 暴露 6 路可映射 PWM")

        f71805f_result = scan_with(FakeF71805FPortIO)
        f71805f_devices = f71805f_result.get("devices") or []
        check.case(bool(f71805f_devices)
                   and f71805f_devices[0].model == "F71805F/FG"
                   and f71805f_devices[0].profile_key == "f71805f",
                   "设备 ID 0x0406 映射到 F71805F 驱动家族")

        f81968_result = scan_with(FakeF81968PortIO)
        f81968_devices = f81968_result.get("devices") or []
        check.case(bool(f81968_devices)
                   and f81968_devices[0].model == "F81968"
                   and f81968_devices[0].profile_key == "f71882fg",
                   "设备 ID 0x1806 映射到 F81968 / f71882fg")

        winbond_result = scan_with(FakeLegacyWinbondPortIO)
        winbond_devices = winbond_result.get("devices") or []
        check.case(bool(winbond_devices)
                   and winbond_devices[0].model == "W83627HF"
                   and winbond_devices[0].profile_key == "w83627hf",
                   "8 位设备 ID 0x52 映射到 W83627HF")

        sch_result = scan_with(FakeSCH5627PortIO)
        sch_devices = sch_result.get("devices") or []
        check.case(bool(sch_devices)
                   and sch_devices[0].model == "SCH5627"
                   and sch_devices[0].profile_key == "sch56xx",
                   "设备 ID 0xc6 映射到 SCH5627")
        check.case(fanhardware.profile_for("f81866a").key == "f71882fg"
                   and fanhardware.profile_for("w83637hf").key == "w83627hf"
                   and fanhardware.profile_for("it8613").key == "it87",
                   "补齐的 hwmon 名称前缀能命中对应驱动家族")

        saved_hwmon = fanhardware.hwmon_controller
        saved_inventory = fanhardware._HARDWARE_INVENTORY
        saved_active = fanhardware._ACTIVE_CONTROLLER
        try:
            hardwareid.PortIO = FakePortIO
            fanhardware.hwmon_controller = lambda: (None, None, None)
            fanhardware._HARDWARE_INVENTORY = None
            fanhardware._ACTIVE_CONTROLLER = None
            source, model, detected = fanhardware.find_controller()
            channels = fanhardware.fan_channels(detected, source)
            check.case(isinstance(source, fanhardware.RawControllerSource),
                       "没有 hwmon 时 find_controller 选择主动后端")
            check.case(model == "NCT6796D" and len(channels) == 7,
                       "主动后端暴露 NCT6796D 的 7 路 PWM 通道")

            fanhardware._HARDWARE_INVENTORY = None
            fanhardware._ACTIVE_CONTROLLER = None
            fnos_path, _fnos_name, _fnos_profile = fanhardware.find_controller(
                "fnos")
            software_path, _software_name, _software_profile = (
                fanhardware.find_controller("software"))
            check.case(
                fnos_path is None
                and isinstance(software_path, fanhardware.RawControllerSource),
                "驱动模式选择会强制区分飞牛驱动与软件驱动")
        finally:
            hardwareid.PortIO = original
            fanhardware.hwmon_controller = saved_hwmon
            fanhardware._HARDWARE_INVENTORY = saved_inventory
            fanhardware._ACTIVE_CONTROLLER = saved_active

        saved_modules = fanhardware.ensure_modules
        saved_inventory = fanhardware._HARDWARE_INVENTORY
        saved_active = fanhardware._ACTIVE_CONTROLLER
        try:
            hardwareid.PortIO = FakePortIO
            fanhardware.ensure_modules = lambda *_args, **_kwargs: []
            fanhardware.hwmon_controller = lambda: (None, None, None)
            fanhardware._HARDWARE_INVENTORY = None
            fanhardware._ACTIVE_CONTROLLER = None
            controller = fancontrold.Controller("/tmp/niufan-r2-test.json",
                                                lambda _message: None)
            controller.refresh_hardware()
            check.case(controller.driver_mode == "software"
                       and controller.driver_mode_label == "软件驱动",
                       "主动后端驱动时界面模式标记为软件驱动")
        finally:
            hardwareid.PortIO = original
            fanhardware.ensure_modules = saved_modules
            fanhardware.hwmon_controller = saved_hwmon
            fanhardware._HARDWARE_INVENTORY = saved_inventory
            fanhardware._ACTIVE_CONTROLLER = saved_active

        class FakeDutyChannel:
            index = 2
            profile = fanhardware.profile_by_key("it87")
            controllable = True

            def __init__(self):
                self._duty = 25
                self.writes = []

            @property
            def enable(self):
                return 1

            @property
            def duty(self):
                return self._duty

            def write_enable(self, _value):
                pass

            def write_duty(self, value):
                self.writes.append(value)
                self._duty = value

        logs = []
        controller = fancontrold.Controller(
            "/tmp/niufan-r2-test.json", logs.append)
        channel = FakeDutyChannel()
        controller.applied[2] = "manual"
        controller.written[2] = 180
        controller.mode_at[2] = fancontrold.time.time()
        controller._apply_duty(channel, 180)
        check.case(channel.writes == [180]
                   and any("may be overriding" in line for line in logs),
                   "PWM 读回被外部改写时会重写并写入日志")
    return check.finish()


if __name__ == "__main__":
    sys.exit(main())
