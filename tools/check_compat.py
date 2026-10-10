#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
"""Prove that a rename/relabel release still reads what older versions wrote.

The 2.0.1 release only changes what users read: the display name is 「风扇控制」
outside the app, ``NiuFan`` inside it, the donation dialog gained a feedback
line.  Nothing that identifies the application may move, and every file an
older version left behind has to keep working:

  * configuration written by 1.10.x and 2.0.0 (before the detection wizard
    existed, with disk selections stored as ``/dev/sdX`` and GPUs stored as a
    single ``gpu`` flag) still loads;
  * a configuration left by the pre-2.0.0 ``fn-fancontrol`` package is picked
    up once, and an existing configuration is never overwritten;
  * the configuration lives in ``etc/`` and no script deletes it on uninstall
    (fnOS runs the uninstall flow before installing an upgrade);
  * the new front end still renders an old ``donate.json``;
  * the identifiers that make fnOS treat this as the same application are
    pinned.

Run it from the project root:  python tools/check_compat.py
"""

import argparse
import importlib.util
import json
import locale
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile

#: Importing the daemon's modules would otherwise leave __pycache__ inside the
#: packaging tree, which the release checks (rightly) refuse to pack.
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: What 1.10.x/2.0.0 called these keys.  They must survive this release
#: unchanged, or an existing install loses part of its configuration.
LEGACY_CONFIG_KEYS = ("version", "enabled", "interval", "hdd_interval",
                      "hysteresis", "fail_safe_duty", "sources", "fans")
LEGACY_FAN_KEYS = ("channel", "name", "mode", "source", "points",
                   "min_duty", "max_duty", "manual_duty")
LEGACY_SOURCE_KEYS = ("cpu", "gpu", "gpu_devices", "hdd", "hdd_devices")

#: Frozen at 2.0.0: the sentence the donation dialog has always shown.
LEGACY_DONATE_NOTE = "打赏完全自愿，不影响任何功能，也不会改变软件的任何行为。"

IDENTITY = {
    "appname": "niufan",
    "desktop_applaunchname": "niufan.Application",
    "display_name": "风扇控制",
}

#: Set in main(): True when the platform's default text encoding is UTF-8, as
#: it is on fnOS.  A Windows box with a legacy code page cannot even decode a
#: UTF-8 config file with the plain open() the daemon uses, so the non-ASCII
#: parts of the fixture are skipped there and reported as such.
UTF8_LOCALE = True
UI_IDENTITY = {
    "gatewayPrefix": "/app/niufan",
    "gatewaySocket": "app.sock",
}


class Check:
    def __init__(self, title):
        self.title = title
        self.problems = []

    def ok(self, message):
        print("  ok    %s" % message)

    def bad(self, message):
        print("  FAIL  %s" % message)
        self.problems.append(message)

    def case(self, condition, message):
        self.ok(message) if condition else self.bad(message)
        return bool(condition)

    def finish(self):
        print("%s: %s" % (self.title, "通过" if not self.problems
                          else "%d 项不通过" % len(self.problems)))
        return 1 if self.problems else 0


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# the three states a configuration can be in
# --------------------------------------------------------------------------- #


def config_v1_10():
    """A configuration as 1.10.x wrote it: no wizard flag, no fingerprint."""
    chassis = "机箱风扇" if UTF8_LOCALE else "Chassis Fan"
    return {
        "version": 1,
        "enabled": True,
        "interval": 5,
        "hdd_interval": 120,
        "hysteresis": 4,
        "fail_safe_duty": 200,
        "sources": {
            "cpu": True,
            "gpu": False,
            "gpu_devices": [],
            "hdd": True,
            "hdd_devices": ["/dev/sda", "disk-WDC-ABC"],
        },
        "fans": [
            {"channel": 2, "name": "CPU_FAN", "mode": "curve", "source": ["cpu"],
             "points": [[30, 20], [50, 40], [70, 80]],
             "min_duty": 60, "max_duty": 240, "manual_duty": 128},
            {"channel": 3, "name": chassis, "mode": "manual", "source": ["hdd"],
             "points": [[40, 30], [55, 100]],
             "min_duty": 80, "max_duty": 255, "manual_duty": 90},
        ],
    }


def config_v2_0_0():
    """A 2.0.0 configuration: wizard flag, per-GPU map, calibration, probe."""
    return {
        "version": 1,
        "setup_complete": True,
        "enabled": False,
        "interval": 8,
        "hdd_interval": 300,
        "hysteresis": 2,
        "fail_safe_duty": 255,
        "sources": {
            "cpu": True,
            "gpus": {"gpu:0000:01:00.0": True},
            "aux": {"aux:nct6798:temp3": False},
            "hdd": True,
            "hdd_devices": ["ata-WDC_WD40EFRX-68N32N0_WD-WCC7K1234567"],
        },
        "fans": [
            {"channel": 2, "name": "CPU_FAN", "mode": "curve", "source": ["cpu"],
             "points": [[35, 25], [50, 45], [65, 70], [80, 100]],
             "min_duty": 60, "max_duty": 255, "manual_duty": 128,
             "calibration": {"duty_low": 30, "rpm_low": 800,
                             "duty_high": 255, "rpm_high": 2100,
                             "rpm_min": 0, "rpm_max": 2100,
                             "responsive": True, "stops": False,
                             "at": 1789000000}},
        ],
        "fingerprint": {"cpu": ["Intel CC150"], "channels": ["CH2", "CH3"]},
        "probe": {"at": "2026-09-20T10:00:00", "results": []},
    }


def main(argv=None):
    global UTF8_LOCALE
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=ROOT)
    parser.add_argument("--workdir",
                        help="临时目录（默认在系统临时目录下新建；"
                             "某些受限环境里需要显式指定）")
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root)
    encoding = (locale.getpreferredencoding(False) or "").lower()
    UTF8_LOCALE = encoding.replace("_", "-").startswith("utf")
    if not UTF8_LOCALE:
        print("注意：本机默认文本编码是 %s，不是 UTF-8。非 ASCII 的风扇名用 ASCII 代替，"
              "其余用例照跑；在 fnOS 上（UTF-8）或用 python -X utf8 运行会覆盖中文名。"
              % encoding)

    server = os.path.join(root, "package", "app", "server")
    sys.path.insert(0, server)
    fanconfig = load_module("fanconfig", os.path.join(server, "fanconfig.py"))

    # fancontrold defines its server classes at import time, and Windows has no
    # socketserver.UnixStreamServer.  This stub only exists so the module can be
    # imported here; nothing in these checks starts a server.
    if not hasattr(socketserver, "UnixStreamServer"):
        socketserver.UnixStreamServer = socketserver.TCPServer
    fancontrold = load_module("fancontrold",
                              os.path.join(server, "fancontrold.py"))
    fandiag = load_module("fandiag", os.path.join(server, "fandiag.py"))

    status = 0
    workdir, temporary = pick_workdir(args.workdir, root)
    print("工作目录：%s%s" % (workdir, "" if temporary else "（保留，未删除）"))
    try:
        status |= check_legacy_config(fanconfig, workdir)
        status |= check_config_location(fancontrold, workdir)
        status |= check_legacy_dir_migration(fancontrold, workdir)
        status |= check_upgrade_scripts(root)
        status |= check_frontend(root, workdir)
        status |= check_identity(root)
        status |= check_config_written_by_new_version(fanconfig, workdir)
        status |= check_diagnostics_policy(root, workdir, fancontrold,
                                           fanconfig, fandiag)
    finally:
        if temporary:
            shutil.rmtree(workdir, ignore_errors=True)

    print("")
    print("兼容性检查：%s" % ("全部通过" if status == 0 else "有 %d 组不通过" % status))
    return 1 if status else 0


def pick_workdir(explicit, root):
    """A directory this script may write in.

    The system temp directory is tried first (and used when it works, which is
    the normal case).  Some hardened Windows sandboxes hand out a %TEMP% that
    can be created in but not written to, so a directory inside the project is
    used as a fallback; either way it is removed again at the end.
    """
    if explicit:
        path = os.path.abspath(explicit)
        os.makedirs(path, exist_ok=True)
        return path, False

    path = tempfile.mkdtemp(prefix="niufan-compat-")
    try:
        probe = os.path.join(path, "probe")
        os.makedirs(probe, exist_ok=True)
        os.rmdir(probe)
        return path, True
    except OSError:
        shutil.rmtree(path, ignore_errors=True)
        fallback = os.path.join(root, ".compat-work")
        shutil.rmtree(fallback, ignore_errors=True)
        os.makedirs(fallback, exist_ok=True)
        return fallback, True


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def check_legacy_config(fanconfig, workdir):
    """A 1.10.x configuration loads unchanged: curves, duties, disk choices."""
    check = Check("① 1.10.x 的旧配置能被新版本读取")
    path = os.path.join(workdir, "v110", "config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old = config_v1_10()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(old, handle, ensure_ascii=False)

    loaded = fanconfig.load(path)
    check.case(isinstance(loaded, dict), "旧配置能解析（没有抛错）")
    if not isinstance(loaded, dict):
        return check.finish()

    missing = [key for key in LEGACY_CONFIG_KEYS if key not in loaded]
    check.case(not missing, "顶层字段一个不少：%s" % (missing or "全部在"))
    check.case(loaded["interval"] == 5 and loaded["hdd_interval"] == 120,
               "轮询间隔保留（interval=%s, hdd_interval=%s）"
               % (loaded["interval"], loaded["hdd_interval"]))
    check.case(loaded["hysteresis"] == 4 and loaded["fail_safe_duty"] == 200,
               "迟滞与失效转速保留（%s / %s）"
               % (loaded["hysteresis"], loaded["fail_safe_duty"]))
    check.case(loaded["sources"]["hdd_devices"] == old["sources"]["hdd_devices"],
               "旧硬盘选择（/dev/sda 与稳定 id 混用）原样读出")
    check.case("setup_complete" in loaded,
               "补上 setup_complete（老装机不会被向导拦住）")
    check.case(loaded["setup_complete"] is True,
               "缺 setup_complete 的旧配置按「已完成」处理")

    fans = {fan["channel"]: fan for fan in loaded.get("fans", [])}
    check.case(sorted(fans) == [2, 3], "两路风扇都在（channel=%s）" % sorted(fans))
    for channel, original in [(2, old["fans"][0]), (3, old["fans"][1])]:
        fan = fans.get(channel)
        if not fan:
            check.bad("channel %d 丢了" % channel)
            continue
        gone = [key for key in LEGACY_FAN_KEYS if key not in fan]
        check.case(not gone, "channel %d 字段一个不少：%s"
                   % (channel, gone or "全部在"))
        check.case(fan["points"] == original["points"],
                   "channel %d 曲线逐点一致（%s）" % (channel, fan["points"]))
        check.case((fan["min_duty"], fan["max_duty"], fan["manual_duty"]) ==
                   (original["min_duty"], original["max_duty"],
                    original["manual_duty"]),
                   "channel %d 的最低/最高/固定转速一致" % channel)
        check.case(fan["name"] == original["name"] and fan["mode"] == original["mode"],
                   "channel %d 的名字与模式一致（%s / %s）"
                   % (channel, fan["name"], fan["mode"]))
    check.case("calibration" in fans.get(2, {}),
               "老配置没有 calibration 时补空值（不改行为）")

    # 1.10.x stored one GPU switch for every card: {"gpu": bool,
    # "gpu_devices": [...]}.  It has to keep working (or at least keep
    # meaning "off") instead of vanishing.
    legacy_gpu = dict(old)
    legacy_gpu["sources"] = dict(old["sources"])
    legacy_gpu["sources"]["gpu"] = True
    legacy_gpu["sources"]["gpu_devices"] = ["gpu:0000:01:00.0"]
    keys = ["gpu:0000:01:00.0", "gpu:0000:02:00.0"]
    migrated = fanconfig.normalise(legacy_gpu, None, keys)
    check.case(migrated["sources"]["gpus"] == {"gpu:0000:01:00.0": True,
                                               "gpu:0000:02:00.0": False},
               "旧的单个显卡开关迁移为每卡开关：%s" % migrated["sources"]["gpus"])
    return check.finish()


def check_config_written_by_new_version(fanconfig, workdir):
    """Round trip: what 2.0.1 writes still holds every 1.x/2.0.0 field."""
    check = Check("② 新版本写回的配置对老版本仍然可读")
    path = os.path.join(workdir, "roundtrip", "config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    old = config_v2_0_0()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(old, handle, ensure_ascii=False)

    before = open(path, "rb").read()
    loaded = fanconfig.load(path)          # only reads
    check.case(open(path, "rb").read() == before,
               "读取配置不会改动文件（回退时老版本看到的还是它写的那份）")
    fanconfig.save(path, loaded)           # what an upgrade rewrites
    again = fanconfig.load(path)

    gone = [key for key in LEGACY_CONFIG_KEYS if key not in again]
    check.case(not gone, "重写后顶层字段仍在：%s" % (gone or "全部在"))
    # gpu/gpu_devices are the one deliberately migrated pair: 1.10.x wrote them
    # and 2.0.0 replaced them with the per-GPU "gpus" map.  Everything else the
    # fixture contains must still be there under the same name.
    kept = [key for key in old["sources"]
            if key not in ("gpu", "gpu_devices")]
    gone = [key for key in kept if key not in again["sources"]]
    check.case(not gone, "重写后 sources 字段仍在：%s" % (gone or "全部在"))
    fan = (again["fans"] or [{}])[0]
    gone = [key for key in LEGACY_FAN_KEYS if key not in fan]
    check.case(not gone, "重写后风扇字段仍在：%s" % (gone or "全部在"))
    check.case(fan.get("calibration", {}).get("rpm_high") == 2100,
               "标定结果（rpm_high=2100）保留")
    check.case(again["enabled"] is False, "总开关状态保留（enabled=false）")
    check.case(again["sources"]["gpus"] == old["sources"]["gpus"],
               "每块显卡的开关保留")
    check.case(again["sources"]["hdd_devices"] == old["sources"]["hdd_devices"],
               "按稳定 id 记录的硬盘选择保留")
    check.case(again["fingerprint"] == old["fingerprint"],
               "硬件指纹保留（不会误判为换机器而要求重标定）")
    return check.finish()


def check_config_location(fancontrold, workdir):
    """The configuration stays in etc/, never in var/."""
    check = Check("③ 配置放在 etc/，卸载不动用户数据")
    appdest = os.path.join(workdir, "appdest")
    os.makedirs(appdest, exist_ok=True)

    class Args:
        appdest = None
        etc = None
        ui = None
        socket = None

    saved = {key: os.environ.get(key)
             for key in ("TRIM_APPDEST", "TRIM_PKGETC", "TRIM_SERVICE_PORT")}
    try:
        os.environ["TRIM_APPDEST"] = appdest
        os.environ.pop("TRIM_PKGETC", None)
        paths = fancontrold.resolve_paths(Args())
        expected = os.path.join(appdest, "etc", "config.json")
        check.case(paths["config"] == expected,
                   "默认配置路径 = %s" % paths["config"])
        check.case("/var/" not in paths["config"].replace(os.sep, "/"),
                   "配置不在 var/ 下")

        etc = os.path.join(workdir, "pkgetc")
        os.environ["TRIM_PKGETC"] = etc
        paths = fancontrold.resolve_paths(Args())
        check.case(paths["config"] == os.path.join(etc, "config.json"),
                   "TRIM_PKGETC 生效：%s" % paths["config"])
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return check.finish()


def check_legacy_dir_migration(fancontrold, workdir):
    """The pre-2.0.0 @appconf/fn-fancontrol/config.json is still picked up."""
    check = Check("④ 2.0.0 改名前的 fn-fancontrol 配置能迁过来")
    check.case(fancontrold.APP_NAME == "niufan",
               "APP_NAME 仍是 niufan（路径与网关不变）")
    check.case(fancontrold.LEGACY_APP_NAME == "fn-fancontrol",
               "迁移来源仍是 fn-fancontrol")

    confdir = os.path.join(workdir, "conf")
    old_dir = os.path.join(confdir, "fn-fancontrol")
    new_dir = os.path.join(confdir, "niufan")
    os.makedirs(old_dir, exist_ok=True)
    old_payload = json.dumps(config_v1_10(), ensure_ascii=False)
    with open(os.path.join(old_dir, "config.json"), "w", encoding="utf-8") as handle:
        handle.write(old_payload)

    target = os.path.join(new_dir, "config.json")
    controller = fancontrold.Controller(target, lambda message: None)
    moved = controller._migrate_legacy_config()
    check.case(moved is True, "目标不存在时执行迁移")
    check.case(os.path.isfile(target), "迁移后目标配置存在")
    if os.path.isfile(target):
        check.case(open(target, encoding="utf-8").read() == old_payload,
                   "迁移内容与旧文件逐字节一致")

    # ... and it never overwrites a configuration that is already there.
    with open(target, "w", encoding="utf-8") as handle:
        handle.write('{"enabled": false, "fans": []}')
    again = controller._migrate_legacy_config()
    check.case(again is False, "目标已存在时不迁移")
    check.case(open(target, encoding="utf-8").read() ==
               '{"enabled": false, "fans": []}',
               "已有配置没有被覆盖")
    return check.finish()


def check_upgrade_scripts(root):
    """Uninstall keeps user data; upgrade takes a copy first (static check)."""
    check = Check("⑤ 升级/卸载脚本不会丢用户数据")
    cmd = os.path.join(root, "package", "cmd")
    uninstall_init = open(os.path.join(cmd, "uninstall_init"), encoding="utf-8").read()
    uninstall = open(os.path.join(cmd, "uninstall_callback"), encoding="utf-8").read()
    upgrade_init = open(os.path.join(cmd, "upgrade_init"), encoding="utf-8").read()
    upgrade = open(os.path.join(cmd, "upgrade_callback"), encoding="utf-8").read()

    removals = re.findall(r"(?m)^\s*(?:rm|find)\s+.*$", uninstall + uninstall_init)
    dangerous = [line.strip() for line in removals
                 if "PKGETC" in line or "config.json" in line
                 or re.search(r"\.\./etc", line)]
    check.case(not dangerous, "卸载流程里没有删 etc/ 或 config.json 的命令：%s"
               % (dangerous or "干净"))
    check.case("rm -f \"${APPDEST}/app.sock\" \"${PKGVAR}/app.pid\"" in uninstall,
               "卸载只清运行期残留（app.sock / app.pid）")
    check.case("User data (TRIM_PKGETC/TRIM_PKGVAR) is deliberately preserved"
               in uninstall, "卸载脚本写明保留用户数据")
    check.case("cp -f \"${PKGETC}/config.json\" \"${PKGVAR}/config.backup.json\""
               in upgrade_init, "升级前把 config.json 备份到 var/")
    check.case("if [ ! -r \"${PKGETC}/config.json\" ] && [ -r \"${PKGVAR}/config.backup.json\" ]"
               in upgrade, "升级后仅在配置缺失时恢复备份")
    check.case("init-config" in upgrade,
               "升级后跑 init-config（配置存在时不会重置）")
    install_callback = open(os.path.join(cmd, "install_callback"),
                            encoding="utf-8").read()
    check.case("An existing configuration always wins" in install_callback,
               "安装脚本写明「已存在的配置优先」")
    return check.finish()


def check_frontend(root, workdir):
    """New UI + old donate.json still renders the sentence users already know.

    app.js is JavaScript, so this runs the real functions in node instead of
    re-implementing them here.
    """
    check = Check("⑥ 新前端仍能渲染旧版 donate.json")
    node = shutil.which("node")
    if not node:
        print("  跳过  node 不可用（fnOS 上没有 node 时正常），"
              "改用静态断言：")
        app_js = open(os.path.join(root, "package", "app", "ui", "app.js"),
                      encoding="utf-8").read()
        check.case("function esc(value)" in app_js, "app.js 里仍有 esc()")
        check.case("Array.isArray(parts)" in app_js,
                   "donateFeedback() 对缺失的 feedback 字段返回空串")
        check.case("data-driver-mode" in app_js
                   and "driver-mode" in app_js,
                   "检测页包含飞牛 / 软件驱动切换入口")
        check.case("PROJECT_URL" in app_js and "QQ_GROUP" in app_js
                   and "3pin" in app_js and "DC" in app_js,
                   "失败提示包含 Git / QQ 和 3pin / DC 排查建议")
        return check.finish()

    ui = os.path.join(root, "package", "app", "ui")
    old_path = os.path.join(workdir, "donate-old.json")
    with open(old_path, "w", encoding="utf-8") as handle:
        json.dump({"title": "支持作者", "message": "niufan 是免费开源软件。",
                   "qrcodes": [], "links": [],
                   "note": LEGACY_DONATE_NOTE}, handle, ensure_ascii=False)

    harness = os.path.join(workdir, "donate_check.js")
    with open(harness, "w", encoding="utf-8") as handle:
        handle.write(
            "const fs = require('fs');\n"
            "const src = fs.readFileSync(process.argv[2], 'utf8');\n"
            "eval(src.match(/function esc[\\s\\S]*?\\n}/)[0]);\n"
            "eval(src.match(/function donateFeedback[\\s\\S]*?\\n}/)[0]);\n"
            "const out = {};\n"
            "for (const [key, path] of [['old', process.argv[3]],"
            " ['now', process.argv[4]]]) {\n"
            "  const cfg = JSON.parse(fs.readFileSync(path, 'utf8'));\n"
            "  out[key] = { note: cfg.note, html: donateFeedback(cfg.feedback) };\n"
            "}\n"
            "process.stdout.write(JSON.stringify(out));\n")

    result = subprocess.run([node, harness, os.path.join(ui, "app.js"), old_path,
                             os.path.join(ui, "donate.json")],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        check.bad("node 渲染失败：%s" % result.stderr.decode("utf-8", "replace").strip())
        return check.finish()

    rendered = json.loads(result.stdout.decode("utf-8"))
    check.case(rendered["old"]["html"] == "",
               "旧 donate.json（没有 feedback 字段）不渲染反馈行 —— 弹窗与旧版一致")
    check.case(rendered["old"]["note"] == LEGACY_DONATE_NOTE,
               "旧文案原句仍能读出")
    check.case(rendered["now"]["note"] == LEGACY_DONATE_NOTE,
               "原来那句自愿说明一字未改")
    html = rendered["now"]["html"]
    check.case('href="https://github.com/LiuFudi/NiuFan"' in html
               and ">Github</a>" in html, "Github 是指向仓库的超链接")
    check.case("QQ群：818299505" in html, "含 QQ 群号")
    check.case(html.count('class="donate-accent"') == 2,
               "链接与 QQ 群号都带红色样式类（%s 处）"
               % html.count('class="donate-accent"'))
    check.case("如果遇到BUG，欢迎前往" in html and "反馈或者添加" in html,
               "整句与要求逐字一致")
    return check.finish()


def check_identity(root):
    """The identifiers fnOS uses to recognise the application are pinned."""
    check = Check("⑦ 数据层标识冻结")
    manifest = {}
    for line in open(os.path.join(root, "package", "manifest"),
                     encoding="utf-8").read().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            manifest[key.strip()] = value.strip()
    for key, value in sorted(IDENTITY.items()):
        check.case(manifest.get(key) == value,
                   "manifest.%s = %s" % (key, manifest.get(key)))

    ui = json.loads(open(os.path.join(root, "package", "app", "ui", "config"),
                         encoding="utf-8").read())[".url"]
    entry_name = next(iter(ui))
    entry = ui[entry_name]
    check.case(entry_name == manifest.get("desktop_applaunchname"),
               "桌面入口键名 = %s" % entry_name)
    for key, value in sorted(UI_IDENTITY.items()):
        check.case(entry.get(key) == value, "入口 %s = %s" % (key, entry.get(key)))
    check.case(entry.get("url") == entry.get("gatewayPrefix"),
               "入口 url 与 gatewayPrefix 一致（%s）" % entry.get("url"))

    main_sh = open(os.path.join(root, "package", "cmd", "main"),
                   encoding="utf-8").read()
    for name in ("TRIM_APPDEST", "TRIM_PKGETC", "TRIM_PKGVAR"):
        check.case(name in main_sh, "cmd/main 仍使用 %s（变量名未改）" % name)
    check.case('APPNAME="niufan"' in main_sh,
               "cmd/main 的 APPNAME 仍是 niufan")

    config = open(os.path.join(root, "package", "app", "ui", "donate.json"),
                  encoding="utf-8").read()
    check.case("niufan" not in json.loads(config)["message"],
               "打赏文案里的软件名已改为 NiuFan")
    return check.finish()


def check_diagnostics_policy(root, workdir, fancontrold, fanconfig, fandiag):
    """⑧ 诊断日志（2.2.5）：路径、鉴权、脱敏与数据层都不越界。"""
    check = Check("⑧ 诊断日志不碰数据层、不新开免鉴权入口")
    server = os.path.join(root, "package", "app", "server")
    package = os.path.dirname(os.path.dirname(server))

    # 1. Where the reports live: var/, never etc/ (which holds the user config).
    class Args:  # minimal stand-in for the parsed command line
        pass

    args = Args()
    args.appdest = os.path.join(workdir, "app")
    args.etc = os.path.join(workdir, "etc")
    args.var = os.path.join(workdir, "var")
    paths = fancontrold.resolve_paths(args)
    check.case(paths["diagnostics"] == os.path.join(workdir, "var", "diagnostics"),
               "诊断目录在 TRIM_PKGVAR 下（%s）" % paths["diagnostics"])
    check.case(not paths["diagnostics"].startswith(paths["etc"] + os.sep),
               "诊断文件不写进 etc/")
    check.case(paths["config"] == os.path.join(workdir, "etc", "config.json"),
               "配置文件路径未变（etc/config.json）")

    # 2. A download may only name a report this app wrote, in this directory.
    store = fandiag.DiagnosticStore(paths["diagnostics"], "niufan", "0.0.0")
    name = store.create("probe")
    check.case(store.resolve(name) is not None, "自己生成的报告能解析")
    for bad in ("../config.json", "..%2Fconfig.json", "/etc/passwd",
                "..\\config.json", "a/../../etc/passwd", "report.txt/../x.txt",
                "", ".hidden.txt", "x.txt.exe"):
        check.case(store.resolve(bad) is None, "拒绝非法文件名 %r" % bad)

    # 3. Routes: under /api/ (the authenticated gateway path), with the
    #    fail-closed check, and served as an attachment.
    daemon = open(os.path.join(server, "fancontrold.py"),
                  encoding="utf-8").read()
    diagnostics_source = open(os.path.join(server, "fandiag.py"),
                              encoding="utf-8").read()
    for route in ("/api/diagnostics/status", "/api/diagnostics/export",
                  "/api/diagnostics/download"):
        check.case(route in daemon, "接口 %s 存在" % route)
    check.case("def authorise_diagnostics" in daemon
               and daemon.count("self.authorise_diagnostics()") == 3,
               "三个诊断接口都走 authorise_diagnostics（fail-closed）")
    check.case('attachment; filename=' in daemon,
               "下载响应带 Content-Disposition: attachment")
    check.case("TRIM_PKGVAR" in daemon and "TRIM_APPDEST" in daemon,
               "守护进程仍从 TRIM_* 变量取路径，未硬编码")
    check.case("collect_runtime_logs" in diagnostics_source
               and "info.log" in diagnostics_source
               and "runtime_logs" in daemon,
               "诊断导出包含 info.log 与运行日志章节")

    # 4. Uninstall / upgrade must not touch the reports (a reinstall keeps the
    #    user's configuration, and a report is no different from any other
    #    runtime artefact the user may still want to send us).
    for script in ("uninstall_callback", "upgrade_init", "upgrade_callback",
                   "install_callback"):
        text = open(os.path.join(package, "cmd", script),
                    encoding="utf-8").read()
        check.case("diagnostics" not in text,
                   "cmd/%s 不会删诊断日志" % script)
    uninstall = open(os.path.join(package, "cmd", "uninstall_callback"),
                     encoding="utf-8").read()
    check.case("config.json" not in uninstall.replace("config.backup", ""),
               "卸载脚本仍不删 config.json")
    upgrade = open(os.path.join(package, "cmd", "upgrade_callback"),
                   encoding="utf-8").read()
    check.case("cp -f" in upgrade, "升级仍按需恢复配置")

    # 5. The configuration schema is stable and the new driver preference has
    #    a single documented default, so older configs still read cleanly.
    schema = set(fanconfig.DEFAULT_CONFIG)
    check.case(schema == {"version", "setup_complete", "driver_preference",
                          "enabled", "interval",
                          "hdd_interval", "hysteresis", "fail_safe_duty",
                          "sources", "fans", "fingerprint", "probe"},
               "配置顶层字段一个没多也一个没少（%s）" % sorted(schema))
    check.case(fanconfig.CONFIG_VERSION == 1,
               "CONFIG_VERSION 仍为 1")

    # 6. The report itself carries no credential and no full serial number.
    disk = fandiag.mask_disk_id(
        "/dev/disk/by-id/ata-WDC_WD40EFRX-68N32N0_WD-WCC7K1234567")
    check.case(disk is not None and "WCC7K1234567" not in disk,
               "报告里的硬盘标识已脱敏（%s）" % disk)
    masked = fandiag.sanitise({"password": "hunter2", "token": "abcd123456",
                               "media_path": "/vol1/1000/photo"})
    check.case(masked["password"] == "**已脱敏**"
               and masked["token"] == "**已脱敏**",
               "口令 / token 字段在报告里被抹掉")
    return check.finish()


if __name__ == "__main__":
    sys.exit(main())
