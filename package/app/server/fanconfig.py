# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
"""Configuration schema, validation and the fan curve engine."""

from __future__ import annotations

import copy
import json
import os
import tempfile

CONFIG_VERSION = 1

#: Fixed source keys. GPUs are not listed here: each physical GPU becomes its
#: own source named ``gpu:<pci-id>`` so that a multi-GPU machine gets one card
#: per GPU instead of a single "gpu" toggle plus a redundant device list.
SOURCE_KEYS = ("cpu", "hdd")
GPU_PREFIX = "gpu:"
#: Motherboard, DIMM and other extra sensors from fanhardware.list_aux_sensors().
#: Unlike GPUs these are not filtered against the sensors present right now: a
#: key left behind by a sensor that has gone away simply never matches anything,
#: so it is inert rather than wrong.
AUX_PREFIX = "aux:"
MODES = ("curve", "manual", "auto")
DRIVER_PREFERENCES = ("auto", "fnos", "software")

#: Fallback curve: quiet when cool, full speed well before the CPU is unhappy.
DEFAULT_CPU_POINTS = [[30, 20], [45, 35], [60, 60], [75, 100]]

#: Disk driven curve: disks dislike sustained temperatures above ~45 C.
DEFAULT_HDD_POINTS = [[30, 20], [40, 30], [48, 60], [55, 100]]

DEFAULT_CONFIG = {
    "version": CONFIG_VERSION,
    # A missing key means "existing install, do not nag with the wizard";
    # a fresh install writes it explicitly as False.
    "setup_complete": True,
    "driver_preference": "auto",
    "enabled": True,
    "interval": 3,
    "hdd_interval": 60,
    "hysteresis": 3,
    "fail_safe_duty": 255,
    "sources": {
        "cpu": True,
        "gpus": {},
        "aux": {},
        "hdd": True,
        "hdd_devices": [],

    },
    "fans": [],
    #: The hardware this calibration was made against.  When it no longer
    #: matches, the machine is not the one that was calibrated and the wizard
    #: is re-opened rather than silently keeping numbers from other hardware.
    "fingerprint": {},
    #: Every probed channel's result, not only the managed ones.  The detection
    #: table reads this back, so a calibration made once still shows after a
    #: restart, an upgrade, or simply reloading the page.
    "probe": {"at": None, "results": []},
}


# --------------------------------------------------------------------------- #
# curve maths
# --------------------------------------------------------------------------- #


def interpolate(points, temperature):
    """Linear interpolation of a ``[[temp, percent], ...]`` curve."""
    if not points:
        return None
    ordered = sorted(([float(p[0]), float(p[1])] for p in points), key=lambda p: p[0])
    if temperature <= ordered[0][0]:
        return ordered[0][1]
    if temperature >= ordered[-1][0]:
        return ordered[-1][1]
    for (t0, p0), (t1, p1) in zip(ordered, ordered[1:]):
        if t0 <= temperature <= t1:
            if t1 == t0:
                return p1
            return p0 + (temperature - t0) * (p1 - p0) / (t1 - t0)
    return ordered[-1][1]


def duty_from_percent(percent, min_duty=0, max_duty=255):
    """Convert a curve percentage into a 0-255 PWM duty, honouring the limits."""
    duty = int(round(float(percent) * 255.0 / 100.0))
    return max(int(min_duty), min(int(max_duty), duty))


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #


def _clean_points(raw, fallback):
    points = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                temp = float(item[0])
                percent = float(item[1])
            except (TypeError, ValueError):
                continue
            points.append([max(0.0, min(120.0, temp)), max(0.0, min(100.0, percent))])
    points.sort(key=lambda p: p[0])
    if len(points) < 2:
        return copy.deepcopy(fallback)
    return points


def _clean_calibration(raw):
    """Validate a stored RPM calibration record, or drop it."""
    if not isinstance(raw, dict):
        return None
    record = {}
    for key in ("duty_low", "rpm_low", "duty_high", "rpm_high",
                "rpm_min", "rpm_max"):
        try:
            record[key] = int(raw.get(key))
        except (TypeError, ValueError):
            return None
    record["responsive"] = bool(raw.get("responsive"))
    record["stops"] = bool(raw.get("stops"))
    try:
        record["at"] = int(raw.get("at"))
    except (TypeError, ValueError):
        record["at"] = None
    return record


def calibration_as_result(channel, calibration):
    """Reshape a stored calibration into a probe result.

    Without this the detection table is empty every time the daemon restarts or
    the app is upgraded, which makes a perfectly intact calibration look as if
    it had been lost.
    """
    if not calibration:
        return None
    return {
        "channel": channel,
        "rpm_high": calibration.get("rpm_high"),
        "rpm_low": calibration.get("rpm_low"),
        "duty_high": calibration.get("duty_high"),
        "duty_low": calibration.get("duty_low"),
        "rpm_min": calibration.get("rpm_min"),
        "rpm_max": calibration.get("rpm_max"),
        "detected": bool(calibration.get("rpm_max")),
        "responsive": bool(calibration.get("responsive")),
        "stops": bool(calibration.get("stops")),
        "pwm_mode": None,
        "pwm_writable": None,
        "pwm_readback": None,
        "enable_writable": None,
        "hints": [],
        "error": None,
        "stored": True,
        "at": calibration.get("at"),
    }


def _clean_sources(raw, gpu_keys=None):
    """Normalise one fan's source list.

    ``gpu_keys`` is the list of ``gpu:<id>`` keys present on this machine.  The
    legacy ``"gpu"`` alias is expanded into them so configs written before each
    GPU became its own source keep working.
    """
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raw = ["cpu"]

    picked = []
    for item in raw:
        key = str(item)
        if key in SOURCE_KEYS:
            if key not in picked:
                picked.append(key)
        elif key.startswith(GPU_PREFIX):
            if key not in picked:
                picked.append(key)
        elif key.startswith(AUX_PREFIX):
            if key not in picked:
                picked.append(key)
        elif key == "gpu":
            if gpu_keys is None:
                if "gpu" not in picked:
                    picked.append("gpu")
            else:
                for gpu in gpu_keys:
                    if gpu not in picked:
                        picked.append(gpu)
    return picked or ["cpu"]


def normalise(config, channels=None, gpu_keys=None):
    """Return a validated deep copy of ``config``, filling in every default."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if isinstance(config, dict):
        for key, value in config.items():
            if key in ("fans", "sources"):
                continue
            if key in cfg:
                cfg[key] = value

    sources = config.get("sources") if isinstance(config, dict) else None
    if isinstance(sources, dict):
        for key in ("cpu", "hdd"):
            if key in sources:
                cfg["sources"][key] = bool(sources[key])
        devices = sources.get("hdd_devices")
        if isinstance(devices, (list, tuple)):
            cfg["sources"]["hdd_devices"] = [str(d) for d in devices]

        # Per-GPU switches.  Accepts the new {"gpus": {id: bool}} shape and
        # migrates the old {"gpu": bool, "gpu_devices": [...]} pair.
        gpus = {}
        raw_gpus = sources.get("gpus")
        if isinstance(raw_gpus, dict):
            for key, value in raw_gpus.items():
                gpus[str(key)] = bool(value)
        elif gpu_keys:
            legacy_on = bool(sources.get("gpu"))
            legacy = sources.get("gpu_devices")
            wanted = [str(d) for d in legacy] if isinstance(legacy, (list, tuple)) and legacy \
                else list(gpu_keys)
            for key in gpu_keys:
                gpus[key] = bool(legacy_on and key in wanted)
        if gpu_keys is not None:
            # drop entries for GPUs that are no longer present
            gpus = {k: v for k, v in gpus.items() if k in gpu_keys}
        cfg["sources"]["gpus"] = gpus

        # Extra sensors (motherboard, DIMMs, ACPI zones, ...): one switch each,
        # off by default because most machines have far more inputs than
        # anything actually wired to them.
        aux = {}
        raw_aux = sources.get("aux")
        if isinstance(raw_aux, dict):
            for key, value in raw_aux.items():
                key = str(key)
                if key.startswith(AUX_PREFIX):
                    aux[key] = bool(value)
        cfg["sources"]["aux"] = aux

    known = {channel.index for channel in channels} if channels else None
    fans = []
    raw_fans = config.get("fans") if isinstance(config, dict) else None
    for entry in raw_fans if isinstance(raw_fans, (list, tuple)) else []:
        if not isinstance(entry, dict):
            continue
        try:
            channel = int(entry.get("channel"))
        except (TypeError, ValueError):
            continue
        if known is not None and channel not in known:
            continue  # header no longer exists on this machine

        mode = entry.get("mode")
        if mode not in MODES:
            mode = "curve"

        min_duty = _clamp_int(entry.get("min_duty"), 0, 255, 0)
        max_duty = _clamp_int(entry.get("max_duty"), 0, 255, 255)
        if min_duty > max_duty:
            min_duty, max_duty = max_duty, min_duty

        is_cpu = "cpu" in _clean_sources(entry.get("source"), gpu_keys)
        fans.append(
            {
                "channel": channel,
                "name": str(entry.get("name") or ("fan%d" % channel))[:32],
                "mode": mode,
                "source": _clean_sources(entry.get("source"), gpu_keys),
                "points": _clean_points(
                    entry.get("points"),
                    DEFAULT_CPU_POINTS if is_cpu else DEFAULT_HDD_POINTS,
                ),
                "min_duty": min_duty,
                "max_duty": max_duty,
                "manual_duty": _clamp_int(entry.get("manual_duty"), 0, 255, 128),
                "calibration": _clean_calibration(entry.get("calibration")),
            }
        )
    fans.sort(key=lambda fan: fan["channel"])
    cfg["fans"] = fans

    cfg["version"] = CONFIG_VERSION
    cfg["setup_complete"] = bool(cfg.get("setup_complete", True))
    preference = str(cfg.get("driver_preference") or "auto")
    cfg["driver_preference"] = (
        preference if preference in DRIVER_PREFERENCES else "auto")
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["interval"] = _clamp_int(cfg.get("interval"), 1, 60, 3)
    cfg["hdd_interval"] = _clamp_int(cfg.get("hdd_interval"), 10, 3600, 60)
    cfg["hysteresis"] = _clamp_int(cfg.get("hysteresis"), 0, 20, 3)
    cfg["fail_safe_duty"] = _clamp_int(cfg.get("fail_safe_duty"), 0, 255, 255)
    cfg["fingerprint"] = _clean_fingerprint(cfg.get("fingerprint"))
    cfg["probe"] = _clean_probe(cfg.get("probe"))
    return cfg


def _clean_fingerprint(raw):
    """Keep the hardware fingerprint as ``{kind: [item, ...]}``."""
    clean = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, (list, tuple)):
                items = [str(item) for item in value if str(item)]
                if items:
                    clean[str(key)] = items
    return clean


def _clean_probe(raw):
    """Keep only the fields the detection table reads back."""
    stored = {"at": None, "results": []}
    if not isinstance(raw, dict):
        return stored
    try:
        stored["at"] = float(raw.get("at"))
    except (TypeError, ValueError):
        stored["at"] = None

    def whole(entry, key):
        try:
            value = entry.get(key)
            return None if value is None else int(round(float(value)))
        except (TypeError, ValueError):
            return None

    results = []
    for entry in raw.get("results") or []:
        if not isinstance(entry, dict):
            continue
        try:
            channel = int(entry.get("channel"))
        except (TypeError, ValueError):
            continue
        results.append({
            "channel": channel,
            "rpm_high": whole(entry, "rpm_high"),
            "rpm_low": whole(entry, "rpm_low"),
            "duty_high": whole(entry, "duty_high"),
            "duty_low": whole(entry, "duty_low"),
            "rpm_min": whole(entry, "rpm_min"),
            "rpm_max": whole(entry, "rpm_max"),
            "responsive": bool(entry.get("responsive")),
            "stops": bool(entry.get("stops")),
            "detected": bool(entry.get("detected")),
        })
    results.sort(key=lambda item: item["channel"])
    stored["results"] = results
    return stored


def _clamp_int(value, low, high, fallback):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


# --------------------------------------------------------------------------- #
# default configuration derived from the detected hardware
# --------------------------------------------------------------------------- #


#: Temperature source labels that mean "this header watches the CPU".
CPU_TEMP_TOKENS = ("peci", "cputin", "cpu", "tctl", "tdie")


def default_fan_for_channel(channel):
    """Sensible defaults for one header, guessed from the BIOS configuration.

    The BIOS already tells us which temperature each header was configured
    against (``pwmN_temp_sel``): a PECI-backed source means the header is
    watching the CPU, anything else is a chassis header.  Chassis headers cool
    the disks first but fall back to CPU temperature, so a missing disk sensor
    can never leave one stuck at full speed.
    """
    label = (getattr(channel, "temp_sel_label", None) or "").strip().lower()
    cpu_header = any(token in label for token in CPU_TEMP_TOKENS)
    if cpu_header:
        return {
            "channel": channel.index,
            "name": "CPU_FAN",
            "mode": "curve",
            "source": ["cpu"],
            "points": copy.deepcopy(DEFAULT_CPU_POINTS),
            "min_duty": 60,
            "max_duty": 255,
            "manual_duty": 128,
        }
    return {
        "channel": channel.index,
        "name": "FAN%d (机箱)" % channel.index,
        "mode": "curve",
        "source": ["hdd", "cpu"],
        "points": copy.deepcopy(DEFAULT_HDD_POINTS),
        "min_duty": 0,
        "max_duty": 255,
        "manual_duty": 128,
    }


def build_default_config(channels, setup_complete=False, only_present=True,
                         gpu_keys=None):
    """Starting point for the fans found on this machine.

    ``only_present`` skips headers that have never reported a tach signal; the
    first-run wizard turns it off so the user sees every real channel.
    """
    fans = []
    for channel in channels:
        if only_present and not channel.present:
            continue
        fans.append(default_fan_for_channel(channel))
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["fans"] = fans
    cfg["setup_complete"] = bool(setup_complete)
    return normalise(cfg, channels, gpu_keys)


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def load(path, channels=None, gpu_keys=None):
    """Read the config from ``path``; a missing or broken file yields defaults."""
    try:
        with open(path, "r") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    return normalise(raw, channels, gpu_keys)


def save(path, config):
    """Atomically persist the config, creating the directory when needed."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", dir=directory, prefix=".config-", suffix=".json", delete=False
    )
    try:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, path)
    return path
