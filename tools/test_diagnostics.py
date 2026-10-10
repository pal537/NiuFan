#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
"""End-to-end checks for the diagnostic log export (2.2.5).

Starts the real daemon in its TCP test mode against a throw-away
``--appdest/--etc/--var`` triple and exercises the three new endpoints the way
the fnOS gateway would, including the failures:

    1. status with no controller   -> controllable count 0 + warning
    2. export                      -> report written under var/diagnostics
    3. download                    -> attachment with the right file name
    4. path traversal              -> 403, never a file
    5. no gateway identity         -> 403 (fail-closed)
    6. non-administrator           -> 403
    7. retention                   -> oldest reports are pruned
    8. configuration untouched     -> etc/config.json is never written

Standard library only; runs on Windows and on fnOS.

Usage:  python3 tools/test_diagnostics.py [--root .] [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: Identity headers the fnOS gateway injects.  Same shape the daemon reads.
ADMIN = {"X-Trim-Userid": "1000", "X-Trim-Username": "admin",
         "X-Trim-Isadmin": "1"}
USER = {"X-Trim-Userid": "1001", "X-Trim-Username": "user",
        "X-Trim-Isadmin": "0"}


class Check:
    def __init__(self, title):
        self.title = title
        self.problems = []

    def case(self, condition, message):
        if condition:
            print("  ok    %s" % message)
        else:
            print("  FAIL  %s" % message)
            self.problems.append(message)
        return bool(condition)

    def finish(self):
        print("%s: %s" % (self.title,
                          "全部通过" if not self.problems
                          else "%d 项不通过" % len(self.problems)))
        return 1 if self.problems else 0


# --------------------------------------------------------------------------- #
# plumbing
# --------------------------------------------------------------------------- #


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def call(port, path, method="GET", headers=None, payload=None):
    """One HTTP call; returns ``(status, headers, body)`` -- even for 4xx/5xx."""
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def wait_ready(port, process, deadline=30.0):
    end = time.time() + deadline
    while time.time() < end:
        if process.poll() is not None:
            return False
        try:
            status, _headers, _body = call(port, "/api/ping")
            if status == 200:
                return True
        except OSError:
            pass
        time.sleep(0.25)
    return False


def load_server_modules(root):
    path = os.path.join(root, "package", "app", "server")
    sys.path.insert(0, path)
    import fanconfig  # noqa: WPS433 - imported after sys.path is set up
    import fandiag
    import fanhardware
    import fancontrold
    return fanconfig, fandiag, fanhardware, fancontrold


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def check_pure_functions(fandiag, check):
    """The redaction helpers, without needing any hardware."""
    disk = fandiag.mask_disk_id(
        "/dev/disk/by-id/ata-WDC_WD40EFRX-68N32N0_WD-WCC7K1234567")
    check.case(disk and "WCC7K1234567" not in disk,
               "磁盘 by-id 名里的序列号被脱敏（%s）" % disk)
    check.case(fandiag.mask_mac("aa:bb:cc:dd:ee:ff") == "aa:bb:**:**:ee:ff",
               "MAC 只保留前后各两段")
    uuid_hash = fandiag.fingerprint("12345678-1234-1234-1234-123456789abc")
    check.case(uuid_hash and "123456789abc" not in uuid_hash,
               "UUID 只保留哈希（%s）" % uuid_hash)
    safe = fandiag.sanitise({
        "password": "hunter2",
        "jwt_secret": "abcdef123456",
        "api_token": "tok_live_123456",
        "enabled": True,
        "nested": {"private_key": "-----BEGIN"},
    })
    leaks = [key for key, value in safe.items()
             if isinstance(value, str) and value != "**已脱敏**"]
    check.case(safe["password"] == "**已脱敏**"
               and safe["jwt_secret"] == "**已脱敏**"
               and safe["api_token"] == "**已脱敏**"
               and safe["nested"]["private_key"] == "**已脱敏**",
               "口令 / 密钥 / token 字段一律脱敏")
    check.case(leaks == [], "非敏感字段不会被误删（漏出：%s）" % leaks)
    check.case(safe.get("enabled") is True, "非敏感字段原样保留：enabled=True")


def check_export(fandiag, check, port):
    status, _headers, body = call(port, "/api/diagnostics/status", headers=ADMIN)
    payload = json.loads(body.decode("utf-8"))
    check.case(status == 200 and payload.get("ok") is True,
               "GET /api/diagnostics/status 返回 200")
    check.case("controllable_fan_controller_count" in payload,
               "状态里带 controllable_fan_controller_count")
    check.case(isinstance(payload.get("detected_hwmon_names"), list),
               "状态里带 detected_hwmon_names 列表")
    check.case(payload.get("driver_mode") in ("software", "fnos", "unknown"),
               "状态里带驱动模式代码（%s）" % payload.get("driver_mode"))
    check.case(payload.get("driver_mode_label") in ("软件驱动", "飞牛驱动", "未识别"),
               "状态里带驱动模式显示名（%s）" % payload.get("driver_mode_label"))
    warnings = payload.get("warnings") or []
    check.case(bool(warnings), "无控制器时返回 warnings（%d 条）" % len(warnings))
    check.case(any("导出诊断日志" in item for item in warnings),
               "warning 引导用户导出诊断日志")
    check.case(any("root" in item or "权限" in item for item in warnings),
               "warning 说明权限 / 驱动 / 硬件等原因")
    check.case(payload.get("controllable_fan_controller_count") == 0,
               "本机没有可控制控制器，计数为 0（与真机相反的场景）")

    status, _headers, body = call(port, "/api/diagnostics/export",
                                  method="POST", headers=ADMIN,
                                  payload={"note": "自动化测试"})
    result = json.loads(body.decode("utf-8"))
    check.case(status == 200 and result.get("file"),
               "POST /api/diagnostics/export 生成了文件（%s）" % result.get("file"))
    name = result.get("file") or ""
    check.case(re.match(r"^niufan-diagnostic-\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?"
                        r"-[A-Za-z0-9._-]+-\d{8}-\d{6}(-\d+)?\.txt$",
                        name) is not None,
               "文件名符合 appname-diagnostic-version-device-yyyymmdd-HHMMSS.txt（%s）"
               % name)
    check.case(result.get("problems") is not None,
               "导出结果带回采集失败项数量（%s）" % result.get("problems"))

    status, headers, body = call(
        port, "/api/diagnostics/download?file=" + urllib.parse.quote(name),
        headers=ADMIN)
    text = body.decode("utf-8", "replace")
    disposition = headers.get("Content-Disposition", "")
    check.case(status == 200, "GET download 返回 200")
    check.case('attachment; filename="%s"' % name in disposition,
               "Content-Disposition 是 attachment 且带正确文件名")
    check.case("application/octet-stream" not in headers.get("Content-Type", ""),
               "日志以 text/plain 下发（%s）" % headers.get("Content-Type"))
    for section in ("1. 应用信息", "3. 主板", "6. 风扇控制器", "7. 内核模块",
                    "10. 采集失败项", "11. 运行日志"):
        check.case(section in text, "报告含「%s」小节" % section)
    check.case("主动采集数量" in text and "SuperIO" in text,
               "报告含主动硬件识别结果 / 探测状态")
    check.case("ACPI / SuperIO 资源冲突" in text,
               "报告含 ACPI / SuperIO 资源冲突检测")
    check.case("NiuFan 诊断日志" in text and result.get("file", "")[-4:] == ".txt",
               "报告是纯文本诊断日志")
    check.case("采集失败项" in text and ("不可用" in text or "读取失败" in text),
               "失败项被记录（命令缺失 / 权限不足）")
    check.case("info.log" in text and "合成 info.log 运行记录" in text,
               "报告包含 info.log 运行日志")
    return name


def check_traversal(check, port, name):
    attempts = [
        "../config.json",
        "..%2fconfig.json",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
        "/etc/passwd",
        "..\\config.json",
        "sub/dir/report.txt",
        "report.txt%00.png",
        "C:/Windows/win.ini",
        "niufan-diagnostic.txt/../../config.json",
    ]
    for attempt in attempts:
        status, _headers, _body = call(
            port, "/api/diagnostics/download?file=" + attempt, headers=ADMIN)
        check.case(status in (400, 403, 404),
                   "拒绝路径穿越 %r（HTTP %s）" % (attempt, status))
    status, _headers, _body = call(
        port, "/api/diagnostics/download?file="
              + urllib.parse.quote("niufan-diagnostic-9.9.9-20000101-000000.txt"),
        headers=ADMIN)
    check.case(status == 404, "不存在的合法文件名返回 404（HTTP %s）" % status)
    status, _headers, _body = call(
        port, "/api/diagnostics/download?file=" + urllib.parse.quote(name),
        headers={"X-Trim-Userid": "1002", "X-Trim-Username": "eve",
                 "X-Trim-Isadmin": "1"})
    check.case(status == 200, "另一个管理员仍可下载（HTTP %s）" % status)


def check_with_controller(check, workdir, modules):
    """The other half of the warning rule: a drivable controller means silence.

    A synthetic hwmon node stands in for the chip, because this machine has no
    fan controller (and no /sys at all).  Everything downstream -- the count the
    UI reads, and the report the developer receives -- is the real code.
    """
    _fanconfig, fandiag, fanhardware, fancontrold = modules
    hwmon = os.path.join(workdir, "fake-hwmon", "hwmon2")
    os.makedirs(hwmon, exist_ok=True)
    for name, value in (("name", "nct6779"), ("pwm1", "128"),
                        ("pwm1_enable", "1"), ("pwm1_mode", "1"),
                        ("fan1_input", "900"), ("temp1_input", "42000"),
                        ("temp1_label", "SYSTIN")):
        with open(os.path.join(hwmon, name), "w", encoding="utf-8") as handle:
            handle.write(value)

    profile = fanhardware.profile_for("nct6779")
    if profile is None or not profile.controllable:
        profile = next(item for item in fanhardware.PROFILES if item.controllable)
    saved = {name: getattr(fanhardware, name)
             for name in ("hwmon_nodes", "find_controller", "has_pwm_attribute",
                          "detect_other_controllers")}
    try:
        fanhardware.hwmon_nodes = lambda: [(hwmon, "nct6779")]
        fanhardware.find_controller = lambda *_args, **_kwargs: (
            hwmon, "nct6779", profile)
        fanhardware.has_pwm_attribute = lambda _path: True
        fanhardware.detect_other_controllers = lambda: []

        etc = os.path.join(workdir, "fake-etc")
        os.makedirs(etc, exist_ok=True)
        fake_var = os.path.join(workdir, "fake-var")
        os.makedirs(fake_var, exist_ok=True)
        with open(os.path.join(fake_var, "info.log"), "w",
                  encoding="utf-8") as handle:
            handle.write("合成 info.log 运行记录\n")
        controller = fancontrold.Controller(os.path.join(etc, "config.json"),
                                            lambda _message: None)
        controller.refresh_hardware()
        controller.load_config()
        store = fandiag.DiagnosticStore(
            os.path.join(fake_var, "diagnostics"), "niufan", "0.0.0",
            paths={"var": fake_var})
        status = controller.diagnostics_status(store)
        check.case(status["controllable_fan_controller_count"] == 1,
                   "识别到可控制控制器时计数为 1（%s）"
                   % status["controllable_fan_controller_count"])
        check.case(status["warnings"] == [],
                   "识别到可控制控制器时不给警告（%r）" % status["warnings"])
        check.case(status.get("driver_mode") == "fnos"
                   and status.get("driver_mode_label") == "飞牛驱动",
                   "hwmon 路径显示为飞牛驱动")
        check.case("nct6779" in status["detected_hwmon_names"],
                   "detected_hwmon_names 列出 hwmon 名称")

        result = controller.export_diagnostics(store, "合成控制器测试")
        text = open(store.resolve(result["file"]), encoding="utf-8").read()
        check.case("nct6779" in text and "pwm1" in text,
                   "报告写出控制器名称与 pwm 通道")
        check.case("可控制控制器数" in text and "可调速" in text,
                   "报告写明该控制器可调速")
        check.case("fan1_input=900" in text, "报告带转速读数")
        check.case("temp1_input=42.0 C" in text, "报告带温度读数")
    finally:
        for name, value in saved.items():
            setattr(fanhardware, name, value)


def check_authorisation(check, port, name):
    for path, method in (("/api/diagnostics/status", "GET"),
                         ("/api/diagnostics/export", "POST"),
                         ("/api/diagnostics/download?file="
                          + urllib.parse.quote(name), "GET")):
        status, _headers, _body = call(port, path, method=method, payload={}
                                       if method == "POST" else None)
        check.case(status == 403, "未带登录态访问 %s -> 403（HTTP %s）"
                   % (path.split("?")[0], status))
        status, _headers, _body = call(port, path, method=method, headers=USER,
                                       payload={} if method == "POST" else None)
        check.case(status == 403, "非管理员访问 %s -> 403（HTTP %s）"
                   % (path.split("?")[0], status))


def check_retention(check, port, workdir, keep=20):
    for _ in range(keep + 3):
        call(port, "/api/diagnostics/export", method="POST", headers=ADMIN)
    directory = os.path.join(workdir, "var", "diagnostics")
    files = [name for name in os.listdir(directory) if name.endswith(".txt")]
    check.case(len(files) <= keep,
               "超过 %d 份后自动清理旧报告（现有 %d 份）" % (keep, len(files)))
    check.case(all(not name.startswith(".") for name in os.listdir(directory)),
               "没有留下临时文件")


def check_config_untouched(check, workdir, before):
    path = os.path.join(workdir, "etc", "config.json")
    after = open(path, "rb").read() if os.path.isfile(path) else None
    check.case(after == before, "导出日志不改动 etc/config.json")
    check.case(os.path.isdir(os.path.join(workdir, "var", "diagnostics")),
               "报告写在 var/diagnostics 而不是 etc/")
    check.case(not os.path.exists(os.path.join(workdir, "etc", "diagnostics")),
               "etc/ 下没有诊断目录")


#: Runs inside node *before* the real app.js, so the module-level code finds the
#: DOM and network stubs it expects.  Everything is declared in one script so
#: the test below shares app.js's top-level scope.
FRONTEND_PRELUDE = r"""
const __calls = [];
const __created = [];
const __elements = new Map();
function __element(sel) {
  if (!__elements.has(sel)) {
    __elements.set(sel, {
      sel: sel, className: '', innerHTML: '', textContent: '', value: '',
      checked: false, disabled: false, dataset: {}, style: {},
      classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
      addEventListener() {}, removeEventListener() {}, appendChild() {},
      remove() {}, click() {}, replaceWith() {}, replaceChildren() {},
      querySelector() { return null; }, querySelectorAll() { return []; },
    });
  }
  return __elements.get(sel);
}
globalThis.document = {
  querySelector: __element,
  querySelectorAll: () => [],
  createElement(tag) {
    const node = __element('created:' + tag);
    node.tagName = tag;
    __created.push(node);
    return node;
  },
  createElementNS: () => __element('svg'),
  body: { appendChild() {} },
  addEventListener() {},
};
globalThis.window = { FANCONTROL_BASE: '/app/niufan/',
                      FANCONTROL_DONATE_QR: {}, confirm: () => false };
globalThis.URL.createObjectURL = () => 'blob:test';
globalThis.URL.revokeObjectURL = () => {};
globalThis.fetch = async (url) => {
  __calls.push(String(url));
  const body = String(url).indexOf('diagnostics/export') >= 0
    ? { ok: true, file: 'niufan-diagnostic-9.9.9-20260101-010203.txt',
        url: '/app/niufan/api/diagnostics/download?file=x' }
    : { ok: true, controllable_fan_controller_count: 0,
        detected_hwmon_names: [], warnings: [] };
  return { ok: true, status: 200, json: async () => body,
           blob: async () => 'BLOB' };
};
"""

#: Appended after app.js; asserts the banner and the export flow.
FRONTEND_TEST = r"""
(async () => {
  const out = { calls: __calls, created: __created };
  diagnostics = { warnings: ['未识别到任何可控制的风扇控制器。请点击下方「导出诊断日志」，并将日志反馈给开发者。',
                             '可能原因：权限不足 / 驱动未加载 / 硬件不支持。'] };
  renderDiagnosticsWarning();
  out.warnClass = $('#diag-warn').className;
  out.warnHtml = $('#diag-warn').innerHTML;

  diagnostics = { warnings: [] };
  renderDiagnosticsWarning();
  out.calmClass = $('#diag-warn').className;
  out.calmHtml = $('#diag-warn').innerHTML;

  hardware = { channels: [{ channel: 1 }], disks: [] };
  diagnostics = { warnings: [] };
  // The real element carries its label from index.html; the stub starts empty.
  $('#setup-diag').textContent = '导出诊断日志';
  await exportDiagnostics($('#setup-diag'));
  out.toast = $('#toast').textContent;
  out.setupLabel = $('#setup-diag').textContent;
  out.setupDisabled = $('#setup-diag').disabled;
  clearTimeout(toastTimer);
  process.stdout.write(JSON.stringify(out));
})();
"""


def check_frontend(check, root, workdir):
    """Render the banner and run the export flow through the real app.js."""
    node = shutil.which("node")
    if not node:
        print("  跳过  node 不可用，前端检查未执行")
        return
    source = open(os.path.join(root, "package", "app", "ui", "app.js"),
                  encoding="utf-8").read()
    # Drop the auto-start so the test owns the scope (boot() would race with it).
    lines = [line for line in source.splitlines() if line.strip() != "boot();"]
    harness = os.path.join(workdir, "frontend_check.js")
    with open(harness, "w", encoding="utf-8") as handle:
        handle.write(FRONTEND_PRELUDE + "\n" + "\n".join(lines) + "\n"
                     + FRONTEND_TEST)
    result = subprocess.run([node, harness], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=60)
    if result.returncode != 0:
        check.case(False, "node 运行 app.js 失败：%s"
                   % result.stderr.decode("utf-8", "replace").strip()[:400])
        return
    out = json.loads(result.stdout.decode("utf-8"))
    check.case(out["warnClass"] == "diag-warn",
               "无控制器时警告框显示（class=%s）" % out["warnClass"])
    check.case("未识别到任何可控制的风扇控制器" in out["warnHtml"]
               and "导出诊断日志" in out["warnHtml"],
               "警告文案与「导出诊断日志」按钮都在警告框内")
    check.case('id="diag-warn-export"' in out["warnHtml"],
               "警告框自己带一个导出按钮")
    check.case("hidden" in out["calmClass"] and out["calmHtml"] == "",
               "识别到控制器时不显示警告（class=%s）" % out["calmClass"])
    check.case(any("diagnostics/export" in url for url in out["calls"]),
               "点击导出先调 POST /api/diagnostics/export")
    check.case(any("diagnostics/download?file=niufan-diagnostic-9.9.9-20260101-010203.txt"
                   in url for url in out["calls"]),
               "再按返回的文件名下载（URL 已转义）")
    anchors = [item for item in out["created"] if item.get("tagName") == "a"]
    check.case(any(item.get("download") ==
                   "niufan-diagnostic-9.9.9-20260101-010203.txt"
                   for item in anchors),
               "下载用文件名清晰可辨（%s）"
               % (anchors[0].get("download") if anchors else "无 <a>"))
    check.case("诊断日志已生成" in (out["toast"] or ""),
               "导出成功后给出提示（%s）" % out["toast"])
    check.case(out["setupLabel"] == "导出诊断日志" and out["setupDisabled"] is False,
               "按钮在导出结束后恢复可用")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=ROOT)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--workdir", default=None)
    parser.add_argument("--keep", action="store_true",
                        help="保留临时目录（排查失败时用）")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    daemon = os.path.join(root, "package", "app", "server", "fancontrold.py")
    if not os.path.isfile(daemon):
        print("test_diagnostics: %s 不存在" % daemon, file=sys.stderr)
        return 2

    modules = load_server_modules(root)
    fandiag = modules[1]
    # Not ``mkdtemp``: on Windows it creates the directory with mode 0o700,
    # which becomes a DACL that a restricted token cannot create children in.
    workdir = args.workdir or os.path.join(
        tempfile.gettempdir(), "niufan-diag-%d-%d" % (os.getpid(), int(time.time())))
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    for name in ("etc", "var", "ui"):
        os.makedirs(os.path.join(workdir, name), exist_ok=True)
    with open(os.path.join(workdir, "var", "info.log"), "w",
              encoding="utf-8") as handle:
        handle.write("合成 info.log 运行记录\n")
    with open(os.path.join(workdir, "ui", "index.html"), "w",
              encoding="utf-8") as handle:
        handle.write("<h1>NiuFan</h1>")
    with open(os.path.join(workdir, "etc", "config.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"version": 1, "enabled": True, "setup_complete": True,
                   "sources": {"cpu": True, "hdd": True, "gpus": {}, "aux": {},
                               "hdd_devices": []},
                   "fans": [], "fingerprint": {}, "probe": {"at": None,
                                                            "results": []}},
                  handle)
    config_path = os.path.join(workdir, "etc", "config.json")

    port = args.port or free_port()
    log_path = os.path.join(workdir, "daemon.log")
    command = [sys.executable, "-X", "utf8", daemon, "run",
               "--appdest", os.path.join(workdir, "app"),
               "--etc", os.path.join(workdir, "etc"),
               "--var", os.path.join(workdir, "var"),
               "--ui", os.path.join(workdir, "ui"),
               "--host", "127.0.0.1", "--port", str(port), "--require-admin"]
    print("工作目录：%s" % workdir)
    print("启动：%s" % " ".join(command))
    with open(log_path, "wb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
    status = 0
    try:
        if not wait_ready(port, process):
            print("守护进程未就绪，日志如下：")
            print(open(log_path, encoding="utf-8", errors="replace").read())
            return 2
        # Snapshot after start-up: the daemon normalises its config once when it
        # loads, and the check is that *exporting* never writes it again.
        before = open(config_path, "rb").read()
        check = Check("诊断日志导出 / 端到端检查")
        check_pure_functions(fandiag, check)
        check_with_controller(check, workdir, modules)
        name = check_export(fandiag, check, port)
        check_traversal(check, port, name)
        check_authorisation(check, port, name)
        check_retention(check, port, workdir)
        check_config_untouched(check, workdir, before)
        check_frontend(check, root, workdir)
        status = check.finish()
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        if args.keep:
            print("保留工作目录：%s" % workdir)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    return status


if __name__ == "__main__":
    sys.exit(main())
