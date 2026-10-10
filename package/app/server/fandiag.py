#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
"""Diagnostic hardware log export for NiuFan.

Collects what the application can see about the machine it runs on -- board,
CPU, GPUs, fan controllers, current sensor readings, kernel modules -- and
writes it to a single plain-text file under ``$TRIM_PKGVAR/diagnostics/``.

Everything here is best effort.  A missing tool, a file that only root can
read, a driver that is not loaded: each one is recorded in the report rather
than raised, because the whole point of the report is to be collectable
*exactly* when the machine is in a strange state.  Only a failure to write the
file itself is an error.

Deliberately not collected: any credential, any user file or media-library
path, and any full serial number, UUID or MAC address.  Identifiers are
shortened or hashed (``fingerprint``), and the configuration goes through
``sanitise`` first.
"""

from __future__ import annotations

import glob
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fanhardware  # noqa: E402

#: Format of a stored report:
#: appname-diagnostic-version-device-yyyymmdd-HHMMSS.txt
NAME_TEMPLATE = "%s-diagnostic-%s-%s-%s.txt"
#: What a download is allowed to ask for.  Anything else is refused before a
#: path is even built, so ``..`` and absolute paths never reach the filesystem.
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.txt")

#: Config keys whose value is never written to the report.
SENSITIVE_HINTS = ("password", "passwd", "secret", "token", "api_key",
                   "apikey", "jwt", "credential", "private_key")

#: Retention: a handful of reports is plenty, and they should not pile up.
KEEP_REPORTS = 20
MAX_AGE_DAYS = 7

#: hwmon drivers that mean "this is a GPU fan / disk bay / CPU sensor".
MODULE_GREP = ("nct|it87|f718|w836|coretemp|k10temp|zenpower|nvidia|amdgpu|"
               "radeon|nouveau|i915|xe|drivetemp|nvme|smartctl")
#: Modules whose presence (or absence) explains a missing controller.
CANDIDATE_MODULES = ("nct6775", "nct6683", "it87", "f71882fg", "w83627ehf",
                     "drivetemp", "coretemp", "k10temp")

#: hwmon drivers that report CPU temperature rather than chassis fans.
CPU_TEMP_DRIVERS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "peci")


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def fingerprint(value, length=12):
    """A short, stable hash of an identifier: comparable, but not usable."""
    text = (value or "").strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return "sha256:%s(len=%d)" % (digest[:length], len(text))


def mask_identifier(value, keep=3):
    """Keep the ends of an identifier and blank the middle (``WDC***567``)."""
    text = (value or "").strip()
    if not text:
        return None
    if len(text) <= keep * 2:
        return "*" * len(text)
    return "%s***%s" % (text[:keep], text[-keep:])


def mask_mac(value):
    """``aa:bb:cc:dd:ee:ff`` becomes ``aa:bb:**:**:ee:ff``."""
    parts = [part for part in re.split(r"[:-]", (value or "").strip()) if part]
    if len(parts) != 6:
        return mask_identifier(value)
    return ":".join(parts[:2] + ["**", "**"] + parts[4:])


def mask_disk_id(value):
    """Keep the model half of a ``/dev/disk/by-id`` name, hash the serial half."""
    text = (value or "").strip()
    if not text:
        return None
    name = os.path.basename(text)
    for prefix in ("ata-", "scsi-", "sata-", "nvme-", "mmc-", "usb-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    head, _sep, tail = name.partition("_")
    if not tail:
        return mask_identifier(name, 6)
    model = head.replace("_", " ").strip() or "unknown"
    return "%s · serial %s" % (model, fingerprint(tail))


def read_file(path, errors=None, limit=8192):
    """Read a system file.  A refusal is recorded, never raised.

    ``None`` means "absent"; an entry in ``errors`` means "there was something
    there and we could not read it", which is what a permissions problem
    produces and is a completely different diagnosis.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit).strip()
    except (FileNotFoundError, ValueError):
        return None
    except OSError as exc:
        if errors is not None:
            errors.append("%s 读取失败：%s" % (path, exc))
        return None


def run_command(parts, errors=None, timeout=6.0):
    """Run a read-only helper command and return its stdout, or ``None``.

    The reason a command is unavailable is recorded: "命令不存在" and "权限不足"
    lead to very different advice, and the report has to say which one it was.
    """
    label = " ".join(parts)
    exe = shutil.which(parts[0])
    if exe is None:
        if errors is not None:
            errors.append("%s 不可用：命令不存在" % parts[0])
        return None
    try:
        proc = subprocess.run([exe] + list(parts[1:]), capture_output=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        if errors is not None:
            errors.append("%s 执行失败：%s" % (label, exc))
        return None
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if proc.returncode != 0 and not out:
        reason = err or "无输出"
        lowered = reason.lower()
        if "permission" in lowered or "denied" in lowered:
            reason = "权限不足（%s）" % reason
        if errors is not None:
            errors.append("%s 退出码 %s：%s" % (label, proc.returncode, reason))
        return None
    return out


def _section(title):
    return "\n\n" + "=" * 72 + "\n" + title + "\n" + "=" * 72 + "\n"


def _display_width(text):
    """Columns a label occupies in a terminal (CJK glyphs are twice as wide)."""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def _pad(label, width=18):
    return label + " " * max(1, width - _display_width(label))


def _field(label, value, width=18):
    if value is None or value == "":
        value = "（无法读取）"
    return "%s: %s\n" % (_pad(label, width), value)


def _lines(prefix, text, width=18):
    if not text:
        return "%s: （无）\n" % _pad(prefix, width)
    body = "\n".join("  " + line for line in text.splitlines())
    return "%s:\n%s\n" % (_pad(prefix, width).rstrip(), body)


def _numeric_suffix(path):
    match = re.search(r"(\d+)", os.path.basename(path))
    return int(match.group(1)) if match else 0


def _milli_celsius(value):
    try:
        return "%.1f C" % (int(value) / 1000.0)
    except (TypeError, ValueError):
        return str(value)


def _describe_file(path):
    if not path:
        return None
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return "%s（不存在）" % path
    except OSError as exc:
        return "%s（无法访问：%s）" % (path, exc)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
    return "%s（%d 字节，最后修改 %s）" % (path, stat.st_size, stamp)


def _filtered(text, wanted):
    if not text:
        return None
    kept = [line.strip() for line in text.splitlines()
            if any(token in line for token in wanted)]
    return "\n".join(kept) if kept else None


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #


def _app_info(ctx, out, errors):
    paths = ctx.get("paths") or {}
    out.append(_section("1. 应用信息 / application"))
    out.append(_field("应用", "%s %s" % (ctx.get("app_name"),
                                         ctx.get("version"))))
    out.append(_field("应用标题", ctx.get("app_title") or "NiuFan"))
    out.append(_field("导出时间", time.strftime("%Y-%m-%d %H:%M:%S %z")))
    out.append(_field("导出原因", ctx.get("note") or "用户从「硬件检测」页导出"))
    if hasattr(os, "getuid"):
        uid = os.getuid()
        out.append(_field("运行身份",
                          "uid=%d euid=%d%s" % (uid, os.geteuid(),
                                                "（root）" if uid == 0 else "")))
    else:
        out.append(_field("运行身份", "非 POSIX 平台（%s）" % os.name))
    out.append(_field("Python", "%s (%s)" % (sys.version.split()[0],
                                             platform.platform())))
    out.append(_field("解释器", sys.executable))
    out.append(_field("进程 PID", os.getpid()))
    out.append(_field("程序目录", paths.get("appdest")))
    out.append(_field("配置目录", paths.get("etc")))
    out.append(_field("数据目录", paths.get("var")))
    out.append(_field("诊断目录", paths.get("diagnostics")))
    out.append(_field("配置文件", _describe_file(paths.get("config"))))
    env_names = ("TRIM_APPDEST", "TRIM_PKGETC", "TRIM_PKGVAR", "TRIM_APPNAME",
                 "TRIM_APPVER", "TRIM_SYS_ARCH", "TRIM_SYS_VERSION",
                 "TRIM_TEMP_LOGFILE")
    found = []
    for name in env_names:
        value = os.environ.get(name)
        if value:
            found.append("%s=%s" % (name, value))
    known = set(item.split("=", 1)[0] for item in found)
    missing = [name for name in env_names if name not in known]
    out.append(_lines("环境变量", "\n".join(found)))
    if missing:
        out.append(_field("未导出变量",
                          "%s（应用中心不向守护进程导出，属正常现象）"
                          % ", ".join(missing)))


def _os_release():
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        text = read_file(path, None)
        if not text:
            continue
        fields = {}
        for line in text.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                fields[key.strip()] = value.strip().strip('"')
        pretty = fields.get("PRETTY_NAME") or fields.get("NAME")
        if pretty:
            return "%s（%s）" % (pretty, fields.get("VERSION_ID", ""))
    return None


def _meminfo():
    text = read_file("/proc/meminfo", None)
    if not text:
        return None
    wanted = {}
    for line in text.splitlines():
        for key in ("MemTotal", "MemFree", "MemAvailable", "SwapTotal"):
            if line.startswith(key + ":"):
                wanted[key] = line.split(":", 1)[1].strip()
    if not wanted:
        return None
    return ", ".join("%s %s" % (key, wanted[key]) for key in wanted)


def _system_section(ctx, out, errors):
    out.append(_section("2. 系统信息 / system"))
    out.append(_field("主机名", platform.node() or None))
    out.append(_field("架构", platform.machine() or None))
    out.append(_field("逻辑核心", os.cpu_count()))
    out.append(_field("Python 平台", platform.platform()))
    version_text = read_file("/proc/version", errors)
    if version_text:
        out.append(_field("内核 version", version_text.replace("\n", " ")))
    out.append(_field("发行版", _os_release()))
    out.append(_field("启动时长", run_command(["uptime", "-p"], None)))
    out.append(_field("内存", _meminfo()))
    out.append(_field("当前时间", time.strftime("%Y-%m-%d %H:%M:%S %z (%Z)")))
    out.append(_lines("uname -a", run_command(["uname", "-a"], errors)))


def _board_section(out, errors):
    out.append(_section("3. 主板 / BIOS / board"))
    fields = (
        ("board_vendor", "主板厂商"),
        ("board_name", "主板型号"),
        ("board_version", "主板版本"),
        ("product_name", "整机型号"),
        ("product_family", "整机系列"),
        ("sys_vendor", "整机厂商"),
        ("chassis_vendor", "机箱厂商"),
        ("bios_vendor", "BIOS 厂商"),
        ("bios_version", "BIOS 版本"),
        ("bios_date", "BIOS 日期"),
    )
    found_any = False
    for name, label in fields:
        value = read_file("/sys/class/dmi/id/%s" % name, errors)
        if value is not None:
            found_any = True
        out.append(_field(label, value))
    out.append(_field("机箱类型",
                      read_file("/sys/class/dmi/id/chassis_type", errors)))
    out.append(_field("产品 UUID（哈希）",
                      fingerprint(read_file("/sys/class/dmi/id/product_uuid",
                                            None))))
    if not found_any:
        out.append(_lines("  说明", "/sys/class/dmi/id 不可读：非 x86 平台、"
                                    "容器内，或权限不足。"))
    out.append(_lines("dmidecode 主板",
                      _filtered(run_command(["dmidecode", "-t", "2"], errors),
                                ("Manufacturer", "Product Name", "Version",
                                 "Serial Number"))))


def _cpu_temps():
    lines = []
    for path, name in fanhardware.hwmon_nodes():
        if name not in CPU_TEMP_DRIVERS:
            continue
        for entry in sorted(glob.glob(os.path.join(path, "temp*_input")),
                            key=_numeric_suffix):
            value = fanhardware.read_int(entry)
            if value is None:
                continue
            lines.append("%s %s = %s" % (name, os.path.basename(entry),
                                         _milli_celsius(value)))
    return "\n".join(lines) if lines else None


def _cpu_section(ctx, out, errors):
    out.append(_section("4. CPU"))
    cpuinfo = read_file("/proc/cpuinfo", errors, limit=262144)
    if cpuinfo:
        blocks = [block for block in cpuinfo.split("\n\n") if block.strip()]
        out.append(_field("逻辑处理器", len(blocks)))
        keys = ("model name", "vendor_id", "cpu family", "model", "stepping",
                "cpu MHz", "cache size", "cpu cores", "siblings", "Hardware",
                "Model", "flags")
        seen = {}
        for line in blocks[0].splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip()
            if key not in keys:
                continue
            keep = value.strip()
            if key == "flags":
                keep = " ".join(keep.split()[:24]) + " ..."
            seen[key] = keep
        for key, value in seen.items():
            out.append(_field(key, value))
    else:
        out.append(_field("/proc/cpuinfo", None))
    out.append(_lines("lscpu", run_command(["lscpu"], errors)))
    out.append(_lines("CPU 温度（hwmon）", _cpu_temps()))


def _gpu_section(ctx, out, errors):
    out.append(_section("5. GPU"))
    lspci = run_command(["lspci"], errors)
    if lspci:
        tokens = ("vga", "3d controller", "display controller")
        kept = [line.strip() for line in lspci.splitlines()
                if any(token in line.lower() for token in tokens)]
        out.append(_lines("lspci（显示设备）", "\n".join(kept) if kept else None))
    out.append(_lines("nvidia-smi -L", run_command(["nvidia-smi", "-L"], None)))
    hardware = ctx.get("hardware") or {}
    gpus = hardware.get("gpus") or []
    if gpus:
        out.append(_lines("应用识别到的显卡",
                          "\n".join("%s: %s（%s）"
                                    % (gpu.get("id"), gpu.get("label"),
                                       gpu.get("driver"))
                                    for gpu in gpus)))
    else:
        out.append(_field("应用识别到的显卡", "无"))


def _hwmon_lines(path):
    """Everything a developer needs to see about one hwmon node."""
    name = fanhardware.read_text(os.path.join(path, "name")) or "（无 name）"
    profile = fanhardware.profile_for(name)
    driver = None
    link = os.path.join(path, "device", "driver")
    if os.path.islink(link):
        driver = os.path.basename(os.path.realpath(link))
    if not driver:
        uevent = read_file(os.path.join(path, "device", "uevent"), None)
        if uevent:
            for line in uevent.splitlines():
                if line.startswith("DRIVER="):
                    driver = line.split("=", 1)[1].strip()
    device = os.path.realpath(os.path.join(path, "device"))

    lines = ["%s: name=%s" % (os.path.basename(path), name)]
    lines.append("  路径      : %s" % path)
    lines.append("  设备      : %s" % device)
    lines.append("  驱动模块  : %s" % (driver or "（无法确定）"))
    if profile is not None:
        lines.append("  已知型号  : %s → 驱动家族 %s（%s，%s）"
                     % (name, profile.key, profile.label,
                        "可调速" if profile.controllable else "仅监控"))
    elif fanhardware.has_pwm_attribute(path):
        lines.append("  已知型号  : 未收录，按通用 PWM 约定处理")
    else:
        lines.append("  已知型号  : 未收录，且没有 pwm* 属性")

    pwms = [entry for entry in glob.glob(os.path.join(path, "pwm*"))
            if re.fullmatch(r"pwm\d+", os.path.basename(entry))]
    if pwms:
        for entry in sorted(pwms, key=_numeric_suffix):
            index = _numeric_suffix(entry)
            duty = fanhardware.read_int(entry)
            enable = fanhardware.read_int(
                os.path.join(path, "pwm%d_enable" % index))
            mode = fanhardware.read_int(os.path.join(path, "pwm%d_mode" % index))
            rpm = fanhardware.read_int(os.path.join(path, "fan%d_input" % index))
            lines.append("  pwm%-3d    : 占空比=%s enable=%s mode=%s 转速=%s "
                         "本进程可写=%s"
                         % (index, duty, enable, mode, rpm,
                            os.access(entry, os.W_OK)))
    else:
        lines.append("  pwm 通道  : 无")

    temps = sorted(glob.glob(os.path.join(path, "temp*_input")),
                   key=_numeric_suffix)
    if temps:
        readings = []
        for entry in temps:
            value = fanhardware.read_int(entry)
            if value is None:
                continue
            label = fanhardware.read_text(entry.replace("_input", "_label")) or ""
            readings.append("%s=%s%s" % (os.path.basename(entry),
                                         _milli_celsius(value),
                                         "(%s)" % label if label else ""))
        lines.append("  温度输入  : %s" % (", ".join(readings) or "无"))

    fans = sorted(glob.glob(os.path.join(path, "fan*_input")),
                  key=_numeric_suffix)
    if fans:
        readings = ["%s=%s" % (os.path.basename(entry),
                               fanhardware.read_int(entry)) for entry in fans]
        lines.append("  转速输入  : %s" % ", ".join(readings))
    return lines


def _hwmon_section(ctx, out, errors):
    out.append(_section("6. 风扇控制器（hwmon）/ fan controllers"))
    nodes = fanhardware.hwmon_nodes()
    active_path, active_name, profile = fanhardware.find_controller()
    out.append(_field("hwmon 节点数", len(nodes)))
    out.append(_field("当前控制器",
                      "%s (%s)" % (active_name, active_path) if active_path
                      else "未识别"))
    out.append(_field("控制器家族",
                      "%s（%s）" % (profile.key,
                                    "可调速" if profile.controllable else "仅监控")
                      if profile is not None else "无"))
    out.append(_field("可控制控制器数",
                      ctx.get("controllable_fan_controller_count")))
    out.append(_field("可控制 PWM 通道数", len(ctx.get("channels") or [])))
    status = ctx.get("status") or {}
    out.append(_field("驱动模式", status.get("driver_mode_label") or "未识别"))
    identity = ctx.get("active_identity")
    if identity:
        out.append(_lines("主动识别控制器",
                          "\n".join("%s: %s" % (key, value)
                                    for key, value in identity.items()
                                    if value not in (None, "", []))))

    inventory = (ctx.get("hardware") or {}).get("hardware_inventory") or {}
    if inventory:
        out.append(_lines(
            "主动采集数量",
            "DMI=%d PCI=%d USB=%d ACPI=%d I2C=%d 设备树=%d"
            % (len(inventory.get("dmi") or {}),
               len(inventory.get("pci") or []),
               len(inventory.get("usb") or []),
               len(inventory.get("acpi") or []),
               len(inventory.get("i2c") or []),
               len(inventory.get("device_tree") or []))))
    superio = (inventory.get("superio") or {})
    devices = superio.get("devices") or []
    out.append(_field("SuperIO 探测", "%d 个已识别型号" % len(devices)))
    if devices:
        rows = []
        for item in devices:
            if hasattr(item, "to_dict"):
                item = item.to_dict()
            rows.append(
                "%s %s chip=%s config=%s I/O=%s enabled=%s profile=%s "
                "类型=%s 接口=%s"
                % (item.get("vendor"), item.get("model"),
                   item.get("chip_id"), item.get("config_port"),
                   item.get("io_base"), item.get("enabled"),
                   item.get("profile"), item.get("device_type"),
                   item.get("control_interface")))
        out.append(_lines("SuperIO 主动探测", "\n".join(rows)))
    problems = superio.get("problems") or []
    if problems:
        out.append(_lines("SuperIO 探测问题", "\n".join(problems)))
    conflicts = fanhardware.kernel_conflict_status()
    if not conflicts.get("available"):
        conflict_text = "无法读取 dmesg / 内核日志"
    elif conflicts.get("lines"):
        conflict_text = "%d 条（来源：%s）" % (
            len(conflicts["lines"]), conflicts.get("source") or "未知")
    else:
        conflict_text = "未发现（来源：%s）" % (
            conflicts.get("source") or "未知")
    out.append(_field("ACPI / SuperIO 资源冲突", conflict_text))
    if conflicts.get("lines"):
        out.append(_lines("内核资源冲突记录",
                          "\n".join(conflicts["lines"])))

    if not nodes:
        out.append(_lines("  说明", "/sys/class/hwmon 为空或不可读：可能内核未"
                                    "提供 hwmon，或运行在容器中。"))
    for path, _name in nodes:
        out.append("\n" + "\n".join(_hwmon_lines(path)) + "\n")
    unsupported = ctx.get("unsupported") or []
    if unsupported:
        out.append(_lines("未被使用的 PWM 节点",
                          "\n".join("%s（%s）"
                                    % (item.get("name"), item.get("hwmon"))
                                    for item in unsupported)))
    channels = ctx.get("channels") or []
    if channels:
        out.append(_lines("应用管理的通道",
                          "\n".join("CH%s: 转速=%s 占空比=%s enable=%s "
                                    "可调速=%s BIOS 绑定温度源=%s 模式=%s"
                                    % (item.get("channel"), item.get("rpm"),
                                       item.get("duty"), item.get("enable"),
                                       item.get("controllable"),
                                       item.get("temp_sel_label")
                                       or item.get("temp_sel"),
                                       item.get("mode"))
                                    for item in channels)))


def _modules_section(ctx, out, errors):
    out.append(_section("7. 内核模块 / kernel modules"))
    pattern = re.compile(MODULE_GREP, re.I)
    lsmod = run_command(["lsmod"], errors)
    if lsmod:
        rows = [line for line in lsmod.splitlines()[1:] if line.split()]
        kept = [line for line in rows if pattern.search(line.split()[0])]
        out.append(_lines("已加载（相关）", "\n".join(kept) if kept else None))
        out.append(_field("已加载模块总数", len(rows)))
    else:
        modules = read_file("/proc/modules", errors, limit=262144)
        if modules:
            names = [line.split()[0] for line in modules.splitlines()
                     if line.split()]
            out.append(_lines("已加载（相关）",
                              "\n".join(name for name in names
                                        if pattern.search(name))))
            out.append(_field("已加载模块总数", len(names)))
        else:
            out.append(_field("模块列表", None))
    present = [name for name in CANDIDATE_MODULES
               if os.path.isdir("/sys/module/%s" % name)]
    absent = [name for name in CANDIDATE_MODULES if name not in present]
    out.append(_field("候选模块（在）", ", ".join(present) or "无"))
    out.append(_field("候选模块（不在）", ", ".join(absent) or "无"))
    out.append(_lines("modprobe",
                      run_command(["modprobe", "--version"], None)))


def _sensors_section(ctx, out, errors):
    out.append(_section("8. 传感器读数 / sensor readings"))
    out.append(_lines("sensors", run_command(["sensors", "-A"], errors)))
    status = ctx.get("status") or {}
    sources = status.get("source_list") or []
    if sources:
        out.append(_lines("应用温度源",
                          "\n".join("%s (%s): %s C [%s]%s"
                                    % (item.get("label"), item.get("key"),
                                       item.get("temperature"),
                                       "已启用" if item.get("enabled")
                                       else "未参与调速",
                                       " · " + str(item.get("detail"))
                                       if item.get("detail") else "")
                                    for item in sources)))
    fans = status.get("fans") or []
    if fans:
        out.append(_lines("应用风扇状态",
                          "\n".join("%s (CH%s): 模式=%s 生效=%s 转速=%s "
                                    "占空比=%s 温度=%s"
                                    % (item.get("name"), item.get("channel"),
                                       item.get("mode"),
                                       item.get("effective_mode"),
                                       item.get("rpm"), item.get("duty"),
                                       item.get("temperature"))
                                    for item in fans)))
    else:
        out.append(_field("应用风扇状态", "没有正在管理的风扇通道"))


def _compact(value):
    if value is None:
        return "无"
    if isinstance(value, dict):
        if not value:
            return "无"
        return " ".join("%s=%s" % (key, val) for key, val in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value) if value else "无"
    return str(value)


def _json(value):
    import json
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _config_section(ctx, out, errors):
    out.append(_section("9. 应用配置摘要（已脱敏）/ configuration"))
    config = ctx.get("config")
    if not isinstance(config, dict):
        out.append(_field("配置", "未加载"))
        return
    safe = sanitise(config)
    out.append(_field("配置版本", safe.get("version")))
    out.append(_field("驱动偏好", safe.get("driver_preference")))
    out.append(_field("调速总开关", safe.get("enabled")))
    out.append(_field("首次标定完成", safe.get("setup_complete")))
    out.append(_field("轮询 / 硬盘间隔",
                      "%s s / %s s" % (safe.get("interval"),
                                       safe.get("hdd_interval"))))
    out.append(_field("温度迟滞", safe.get("hysteresis")))
    out.append(_field("失效保护占空比", safe.get("fail_safe_duty")))
    sources = safe.get("sources") or {}
    out.append(_field("温度源开关",
                      "cpu=%s hdd=%s 显卡=%s 其它=%s"
                      % (sources.get("cpu"), sources.get("hdd"),
                         _compact(sources.get("gpus")),
                         _compact(sources.get("aux")))))
    out.append(_field("所选硬盘", _compact(sources.get("hdd_devices"))))
    out.append(_field("硬件指纹", _compact(safe.get("fingerprint"))))
    fans = safe.get("fans") or []
    if fans:
        out.append(_lines("风扇配置",
                          "\n".join("CH%s %s: 模式=%s 温度源=%s 最低=%s 最高=%s "
                                    "固定=%s 标定=%s"
                                    % (fan.get("channel"), fan.get("name"),
                                       fan.get("mode"),
                                       _compact(fan.get("source")),
                                       fan.get("min_duty"), fan.get("max_duty"),
                                       fan.get("manual_duty"),
                                       _compact(fan.get("calibration")))
                                    for fan in fans)))
    else:
        out.append(_field("风扇配置", "无（还没有标定任何通道）"))
    out.append(_lines("完整配置（脱敏 JSON）", _json(safe)))


def _error_section(out, errors):
    out.append(_section("10. 采集失败项 / collection problems"))
    if errors:
        out.append("".join("  - %s\n" % item for item in errors))
        out.append("\n")
        out.append(_lines("  说明",
                          "以上项目采集失败，不代表其它部分不可用。常见原因：\n"
                          "  1. 运行身份权限不足（读 /sys、执行 dmidecode）；\n"
                          "  2. 命令未安装（lspci / sensors / nvidia-smi）。"))
    else:
        out.append("  无：所有采集项都成功了。\n")


def collect_runtime_logs(paths, max_bytes=1024 * 1024):
    """Read the application's runtime logs for one diagnostic export."""
    var = (paths or {}).get("var") or ""
    candidates = [
        ("info.log", os.path.join(var, "info.log")),
        ("install.log", os.path.join(var, "install.log")),
        ("upgrade.log", os.path.join(var, "upgrade.log")),
    ]
    temp_log = os.environ.get("TRIM_TEMP_LOGFILE")
    if temp_log:
        candidates.append(("TRIM_TEMP_LOGFILE", temp_log))

    logs = []
    missing = []
    for name, path in candidates:
        if not path or not os.path.isfile(path):
            missing.append(name)
            continue
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                truncated = size > max_bytes
                if truncated:
                    handle.seek(-max_bytes, os.SEEK_END)
                else:
                    handle.seek(0)
                content = handle.read(max_bytes).decode(
                    "utf-8", "replace")
        except OSError as exc:
            missing.append("%s（%s）" % (name, exc))
            continue
        logs.append({
            "name": name,
            "path": path,
            "content": content.strip(),
            "truncated": truncated,
        })
    return logs, missing


def _runtime_logs_section(ctx, out, _errors):
    out.append(_section("11. 运行日志 / runtime logs"))
    logs = ctx.get("runtime_logs") or []
    missing = ctx.get("runtime_logs_missing") or []
    if not logs:
        out.append(_field("运行日志", "未找到可读取的 info.log / 安装日志"))
    for item in logs:
        out.append(_field("日志文件", "%s（%s）"
                          % (item.get("name"), item.get("path"))))
        if item.get("truncated"):
            out.append(_field("  说明", "文件较大，仅保留前 1 MiB"))
        out.append("\n" + (item.get("content") or "（空文件）") + "\n")
    if missing:
        out.append(_field("未找到的日志", "、".join(missing)))


# --------------------------------------------------------------------------- #
# sanitising
# --------------------------------------------------------------------------- #


def sanitise(value, key=""):
    """Copy ``value`` with credentials and full identifiers taken out."""
    if any(hint in key.lower() for hint in SENSITIVE_HINTS):
        return "**已脱敏**"
    if isinstance(value, dict):
        return {str(k): sanitise(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitise(item, key) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("/dev/disk/by-id/"):
            return mask_disk_id(text)
        if re.fullmatch(r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}", text):
            return mask_mac(text)
        return value
    return value


def disk_summary(sensor):
    """Model and a masked identity for one disk: never the serial number."""
    device = getattr(sensor, "device", None)
    model = None
    if device:
        model = fanhardware.read_text("/sys/block/%s/device/model" % device)
    return {
        "device": device,
        "model": model,
        "identity": mask_disk_id(getattr(sensor, "id", None)),
        "driver": getattr(sensor, "driver", None),
    }


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def build_report(ctx):
    """Return ``(text, errors)``.  Always a usable report, never an exception."""
    errors = []
    out = ["NiuFan 诊断日志 / diagnostic report\n",
           "由「硬件检测」页的「导出诊断日志」生成，用于排查风扇控制器识别问题。\n",
           "只含硬件信息、运行日志与配置摘要；不含口令、密钥、用户文件、完整序列号。\n"]
    steps = (_app_info, _system_section, _board_section, _cpu_section,
             _gpu_section, _hwmon_section, _modules_section, _sensors_section,
             _config_section)
    for step in steps:
        try:
            if step is _board_section:
                step(out, errors)
            else:
                step(ctx, out, errors)
        except Exception as exc:  # noqa: BLE001 - a section must not kill the report
            errors.append("%s 采集异常：%r" % (step.__name__, exc))
    try:
        _error_section(out, errors)
    except Exception as exc:  # noqa: BLE001
        out.append("\n采集失败项：%r\n" % (exc,))
    try:
        _runtime_logs_section(ctx, out, errors)
    except Exception as exc:  # noqa: BLE001
        out.append("\n运行日志：%r\n" % (exc,))
    return "".join(out), errors


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #


class DiagnosticStore:
    """Owns ``$TRIM_PKGVAR/diagnostics``: create, resolve, prune."""

    def __init__(self, directory, app_name, version, log=None, paths=None):
        self.directory = directory
        self.app_name = app_name
        self.version = version
        self.log = log or (lambda _message: None)
        self._paths = dict(paths or {})
        self._paths["diagnostics"] = directory

    def paths(self):
        """The runtime paths as the report should print them."""
        return dict(self._paths)

    @staticmethod
    def device_slug():
        host = platform.node() or "device"
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", host).strip("-._")
        return (slug or "device")[:32]

    # -- writing ----------------------------------------------------------- #

    def _ensure_directory(self):
        """Create the spool directory, 0700 wherever the platform honours it.

        ``makedirs(mode=...)`` is deliberately not used for the mode: on
        Windows ``mode`` becomes a DACL, and a process running under a
        restricted token can then be refused access to the directory it has
        just created -- an export would fail for a reason that has nothing to
        do with the report.  The mode is applied as a separate best-effort
        step instead; the reports themselves are always created 0600.
        """
        os.makedirs(self.directory, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError as exc:
            self.log("cannot tighten %s: %s" % (self.directory, exc))

    def create(self, text):
        """Write one report and return its file name.  Raises OSError on failure."""
        self._ensure_directory()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = NAME_TEMPLATE % (
            self.app_name, self.version, self.device_slug(), stamp)
        counter = 2
        while os.path.exists(os.path.join(self.directory, name)):
            name = NAME_TEMPLATE % (
                self.app_name, self.version, self.device_slug(),
                "%s-%d" % (stamp, counter))
            counter += 1
        target = os.path.join(self.directory, name)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.directory, prefix=".diag-",
            suffix=".txt", delete=False)
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        self.prune()
        return name

    # -- reading ----------------------------------------------------------- #

    def resolve(self, name):
        """Absolute path of a stored report, or ``None`` when the name is not ours.

        ``..``, absolute paths, subdirectories and anything that is not a
        ``*.txt`` written by this class are refused before a path is built;
        the ``realpath`` comparison then also covers a symlink planted inside
        the directory.
        """
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            return None
        root = os.path.realpath(self.directory)
        target = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(target) != root:
            return None
        if not os.path.isfile(target):
            return None
        return target

    def listing(self):
        """``[(name, bytes, mtime)]``, newest first."""
        items = []
        try:
            entries = os.listdir(self.directory)
        except OSError:
            return items
        for name in entries:
            if not NAME_PATTERN.fullmatch(name):
                continue
            path = os.path.join(self.directory, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            items.append((name, stat.st_size, stat.st_mtime))
        items.sort(key=lambda item: item[2], reverse=True)
        return items

    def count(self):
        return len(self.listing())

    # -- housekeeping ------------------------------------------------------ #

    def prune(self, keep=KEEP_REPORTS, max_age_days=MAX_AGE_DAYS):
        """Keep the newest ``keep`` reports and nothing older than the age cap."""
        cutoff = time.time() - max_age_days * 86400
        removed = []
        for index, (name, _size, mtime) in enumerate(self.listing()):
            if index < keep and mtime >= cutoff:
                continue
            try:
                os.unlink(os.path.join(self.directory, name))
                removed.append(name)
            except OSError as exc:
                self.log("cannot remove old diagnostic %s: %s" % (name, exc))
        if removed:
            self.log("pruned %d old diagnostic report(s)" % len(removed))
        return removed
