#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
"""Release checks for the NiuFan .fpk, used by build-fpk.sh.

Four sub-commands; each one exits non-zero on the first problem it finds:

    versions   manifest.version, the daemon's VERSION, the top CHANGELOG entry
               and the README badge all say the same thing
    source     the packaging tree is complete, clean and still carries the
               identifiers that must never change (appname and friends)
    stamp      normalise the modes inside a built fpk.  fnpack may write
               0666/0777 for everything, which is not installable, so the
               modes are rewritten and the manifest checksum re-stamped
    sourcepkg  build <appname>-<version>-source.tar.gz/.zip from a whitelist,
               so the source package and the tree can never drift apart
    fpk        inspect a built fpk: version, checksum, modes, no symlinks

Standard library only: this also has to run on the fnOS box, which has no pip.
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: Identifiers that must survive every rename.  Changing any of these makes
#: fnOS treat the package as a different application, which loses the config
#: directory, the gateway path and the upgrade path.
IDENTITY = {
    "appname": "niufan",
    "desktop_applaunchname": "niufan.Application",
    "display_name": "风扇控制",
}
UI_CONFIG_IDENTITY = {
    "gatewayPrefix": "/app/niufan",
    "gatewaySocket": "app.sock",
}

SCRIPTS_0755 = ("cmd/", "wizard/")
REQUIRED = (
    "manifest",
    "ICON.PNG",
    "ICON_256.PNG",
    "cmd/main",
    "cmd/install_init",
    "cmd/install_callback",
    "cmd/upgrade_init",
    "cmd/upgrade_callback",
    "cmd/uninstall_init",
    "cmd/uninstall_callback",
    "config/privilege",
    "config/resource",
    "app/ui/config",
    "app/ui/index.html",
    "app/ui/app.js",
    "app/ui/style.css",
    "app/ui/donate.json",
    "app/ui/donate-qr.js",
    "app/server/hardwareid.py",
)
JUNK_DIRS = ("__pycache__", "node_modules", "dist", "build", ".git",
             ".compat-work")
JUNK_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej", ".swp", ".tmp", ".fpk")
JUNK_NAMES = (".DS_Store", "Thumbs.db", "package-lock.json", "pnpm-lock.yaml")
QR_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
QR_IMAGE_MARKERS = ("wechat", "alipay", "微信", "支付宝")
#: What goes into a source package.  A whitelist and not "tar the directory":
#: dist/, 交付/, __pycache__ and anything a future tool drops into the tree stay
#: out by construction (《通用开发规范》2.2.1).
SOURCE_ENTRIES = (".gitattributes", ".gitignore", "CHANGELOG.md", "LICENSE",
                  "README.en.md", "README.md", "build-fpk.sh", "docs",
                  "package", "tools")
PLACEHOLDERS = ("{display_name}", "{port}", "{url-path}", "{appname}",
                "{desktop_uidir}", "your-name", "demoapp")
TEXT_SUFFIXES = (".py", ".js", ".css", ".html", ".json", ".sh", ".md", ".txt",
                 ".cfg", ".ini", "")
FORBIDDEN = (
    (re.compile(r"/vol\d+/"), "NAS 绝对路径 /volN/"),
    (re.compile("备份-PC"), "NAS 归档路径 备份-PC"),
    (re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
     "内网 IP"),
    (re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*[\"'][^\"'\s]{6,}[\"']"),
     "疑似口令 / token"),
)


class Check:
    """Collects problems so one run reports everything, then exits non-zero."""

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
        print("%s: %s" % (self.title, "所有检查通过" if not self.problems
                          else "%d 项不通过" % len(self.problems)))
        return 1 if self.problems else 0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_manifest(path):
    """Return {key: value} plus the raw lines, keeping the field order."""
    entries = []
    for line in read_text(path).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            entries.append((None, line))
            continue
        key, value = line.split("=", 1)
        entries.append((key.strip(), value.strip()))
    fields = {key: value for key, value in entries if key}
    return fields, entries


def is_qr_image(name):
    lowered = name.lower()
    return (lowered.endswith(QR_IMAGE_SUFFIXES)
            and any(marker in lowered for marker in QR_IMAGE_MARKERS))


def daemon_version(path):
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', read_text(path), re.M)
    return match.group(1).replace("--", "-") if match else None


def changelog_version(path):
    match = re.search(r"^##\s+\[([^\]]+)\]", read_text(path), re.M)
    return match.group(1) if match else None


def readme_badge_version(path):
    match = re.search(
        r"badge/version-([0-9]+\.[0-9]+\.[0-9]+(?:--[0-9A-Za-z.]+)?)"
        r"-orange\.svg",
        read_text(path))
    return match.group(1).replace("--", "-") if match else None


def md5_file(path):
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def wanted_mode(name, is_dir):
    if is_dir:
        return 0o755
    for prefix in SCRIPTS_0755:
        if name == prefix.rstrip("/") or name.startswith(prefix):
            return 0o755
    return 0o644


def normalise_tar(members, get_data, writer):
    """Rewrite every member of a tar with the mode policy above."""
    for member in members:
        member.mode = wanted_mode(member.name, member.isdir())
        if member.isreg():
            payload = get_data(member)
            member.size = len(payload)
            writer.addfile(member, io.BytesIO(payload))
        else:
            writer.addfile(member)


def tar_bytes(members, get_data):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.GNU_FORMAT) as out:
        normalise_tar(members, get_data, out)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# versions
# --------------------------------------------------------------------------- #


def cmd_versions(args):
    check = Check("版本号三处交叉校验")
    manifest_path = os.path.join(args.root, "package", "manifest")
    fields, _ = read_manifest(manifest_path)

    found = {
        "package/manifest 的 version": fields.get("version"),
        "fancontrold.py 的 VERSION": daemon_version(
            os.path.join(args.root, "package", "app", "server", "fancontrold.py")),
        "CHANGELOG.md 顶层条目": changelog_version(
            os.path.join(args.root, "CHANGELOG.md")),
        "README.md 顶部徽章": readme_badge_version(
            os.path.join(args.root, "README.md")),
    }
    for label in sorted(found):
        print("  %-28s = %s" % (label, found[label]))

    values = set(found.values())
    if len(values) == 1 and None not in values:
        check.ok("四处（manifest / 代码常量 / CHANGELOG / README 徽章）一致：%s"
                 % values.pop())
    else:
        check.bad("版本号不一致：%s" % found)
    return check.finish()


# --------------------------------------------------------------------------- #
# source tree
# --------------------------------------------------------------------------- #


def cmd_source(args):
    root = os.path.join(args.root, "package")
    check = Check("打包源码树检查")

    for rel in REQUIRED:
        check.case(os.path.isfile(os.path.join(root, rel)), "存在 %s" % rel)

    manifest, _ = read_manifest(os.path.join(root, "manifest"))
    for key, value in sorted(IDENTITY.items()):
        check.case(manifest.get(key) == value,
                   "%s 未被改动（%s）" % (key, manifest.get(key)))

    # fnpack parses the manifest configparser-style, where ``;`` starts an
    # inline comment: a value containing it is silently cut in half and the
    # tail is re-emitted as a comment line.  Tested against fnpack 1.2.3 --
    # a desc holding ``style="color:red;"`` lost its last 137 characters.
    offenders = sorted(key for key, value in manifest.items() if ";" in value)
    check.case(not offenders,
               "manifest 的值里没有 ';'（fnpack 会当作行内注释截断）：%s"
               % (offenders or "干净"))

    # app/ui/config: the entry key, the icon template and the gateway path all
    # have to line up with the manifest, or the desktop icon 404s.
    config_path = os.path.join(root, "app", "ui", "config")
    try:
        ui = json.loads(read_text(config_path))
        entries = ui.get(".url", {})
        check.case(len(entries) == 1, "app/ui/config 只有一个入口")
        name = next(iter(entries), "")
        entry = entries.get(name, {})
        check.case(name == manifest.get("desktop_applaunchname"),
                   "入口键名与 desktop_applaunchname 一致（%s）" % name)
        appname = manifest.get("appname") or ""
        check.case(name.startswith(appname),
                   "入口键名以 appname 开头")
        check.case(entry.get("title") == IDENTITY["display_name"],
                   "入口 title = %s" % entry.get("title"))
        for key, value in sorted(UI_CONFIG_IDENTITY.items()):
            check.case(entry.get(key) == value,
                       "%s = %s" % (key, entry.get(key)))
        icon = entry.get("icon", "")
        check.case("{0}" in icon, "图标模板带 {0} 占位（%s）" % icon)
        for size in ("64", "256"):
            icon_file = icon.replace("{0}", size)
            check.case(os.path.isfile(os.path.join(root, "app", "ui", icon_file)),
                       "图标文件存在：app/ui/%s" % icon_file)
        check.case(entry.get("type") == "iframe", "入口 type = iframe")
    except (OSError, ValueError) as exc:
        check.bad("app/ui/config 无法解析：%s" % exc)

    # Nothing that belongs to this machine or to the previous packaging tool.
    junk, symlinks, forbidden, placeholders, qr_images = [], [], [], [], []
    for base, dirs, files in os.walk(root):
        for name in list(dirs):
            if name in JUNK_DIRS:
                junk.append(os.path.relpath(os.path.join(base, name), root))
            if os.path.islink(os.path.join(base, name)):
                symlinks.append(os.path.relpath(os.path.join(base, name), root))
        for name in files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root)
            if os.path.islink(full):
                symlinks.append(rel)
            if name in JUNK_NAMES or name.endswith(JUNK_SUFFIXES):
                junk.append(rel)
                continue
            if is_qr_image(name):
                qr_images.append(rel)
            if not (name.endswith(TEXT_SUFFIXES) or "." not in name):
                continue
            try:
                text = read_text(full)
            except (OSError, UnicodeDecodeError):
                continue
            for hit in PLACEHOLDERS:
                if hit in text:
                    placeholders.append("%s: %s" % (rel, hit))
            for pattern, label in FORBIDDEN:
                if pattern.search(text):
                    forbidden.append("%s: %s" % (rel, label))

    check.case(not junk, "无 __pycache__ / *.pyc / 临时文件（%s）" % (junk or "干净"))
    check.case(not symlinks, "包内无软链接（%s）" % (symlinks or "干净"))
    check.case(not placeholders,
               "无脚手架占位符（%s）" % (placeholders or "干净"))
    check.case(not forbidden,
               "无主机专属路径 / 内网 IP / 口令（%s）" % (forbidden or "干净"))
    check.case(not qr_images,
               "打包树中没有独立收款码图片（%s）" % (qr_images or "干净"))
    return check.finish()


# --------------------------------------------------------------------------- #
# stamp / inspect
# --------------------------------------------------------------------------- #


def collect_source(root):
    """``[(relative-name, absolute-path)]`` for the source package, sorted.

    Driven by the :data:`SOURCE_ENTRIES` whitelist rather than by walking the
    whole tree: whatever else ends up in the workspace later (build output, the
    delivery directory, editor scratch files) can never leak into a package
    that is handed to strangers (《通用开发规范》2.2.1).
    """
    items = []
    for entry in SOURCE_ENTRIES:
        path = os.path.join(root, entry)
        if os.path.islink(path) or not os.path.exists(path):
            continue
        if os.path.isfile(path):
            items.append((entry, path))
            continue
        for base, dirs, files in os.walk(path):
            dirs[:] = sorted(
                name for name in dirs
                if name not in JUNK_DIRS
                and not os.path.islink(os.path.join(base, name)))
            for name in sorted(files):
                full = os.path.join(base, name)
                if os.path.islink(full) or name in JUNK_NAMES:
                    continue
                if name.endswith(JUNK_SUFFIXES):
                    continue
                items.append((os.path.relpath(full, root).replace(os.sep, "/"),
                              full))
    items.sort()
    return items


def source_package_names(root):
    """``(appname, version, [target, ...])`` for this tree."""
    fields, _entries = read_manifest(os.path.join(root, "package", "manifest"))
    appname = fields.get("appname")
    version = fields.get("version")
    if not appname or not version:
        raise ValueError("package/manifest 里没有 appname/version")
    return appname, version


def write_source_packages(root, outdir):
    """Build ``<appname>-<version>-source.tar.gz`` and ``.zip``.

    The archive root is ``<appname>/``, so unpacking yields exactly the expanded
    source tree the delivery rules ask for next to the package.  Both files are
    written to a temporary name and moved into place: the archive must never be
    produced *inside* the tree it is reading (9.4), and a half-written package
    must never be mistaken for a finished one.
    """
    appname, version = source_package_names(root)
    items = collect_source(root)
    if not items:
        raise ValueError("源码白名单没有匹配到任何文件")
    os.makedirs(outdir, exist_ok=True)

    written = []
    for suffix in (".tar.gz", ".zip"):
        target = os.path.join(outdir,
                              "%s-%s-source%s" % (appname, version, suffix))
        handle = tempfile.NamedTemporaryFile(dir=outdir, prefix=".source-",
                                             suffix=suffix, delete=False)
        handle.close()
        try:
            if suffix == ".zip":
                with zipfile.ZipFile(handle.name, "w",
                                     compression=zipfile.ZIP_DEFLATED) as out:
                    for arcname, path in items:
                        with open(path, "rb") as source:
                            out.writestr("%s/%s" % (appname, arcname),
                                         source.read())
            else:
                with tarfile.open(handle.name, "w:gz",
                                  format=tarfile.GNU_FORMAT) as out:
                    for arcname, path in items:
                        info = out.gettarinfo(
                            path, arcname="%s/%s" % (appname, arcname))
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        with open(path, "rb") as source:
                            out.addfile(info, source)
            os.replace(handle.name, target)
        except BaseException:
            if os.path.exists(handle.name):
                os.unlink(handle.name)
            raise
        written.append(target)
    return written


def cmd_sourcepkg(args):
    check = Check("源码包生成")
    try:
        appname, version = source_package_names(args.root)
        paths = write_source_packages(args.root, args.outdir)
    except (OSError, ValueError) as exc:
        check.bad("生成失败：%s" % exc)
        return check.finish()
    for path in paths:
        check.ok("%s（%d 字节）" % (os.path.basename(path),
                                   os.path.getsize(path)))

    # Reverse check, same idea as the fpk one: read the version back out of the
    # package and compare it with the file name and the manifest.
    with tarfile.open(paths[0]) as archive:
        names = archive.getnames()
        inside = "%s/package/manifest" % appname
        check.case(inside in names, "包内有 %s" % inside)
        text = archive.extractfile(inside).read().decode("utf-8") if \
            inside in names else ""
        match = re.search(r"(?m)^\s*version\s*=\s*(.*?)\s*$", text)
        check.case(match is not None and match.group(1) == version,
                   "包内 manifest.version = %s（文件名里的版本 %s）"
                   % (match.group(1) if match else "?", version))
        leaked = [name for name in names
                  if "__pycache__" in name
                  or name.endswith((".pyc", ".pyo", ".fpk"))
                  or name.startswith("%s/dist/" % appname)
                  or name.startswith("%s/交付/" % appname)]
        check.case(not leaked,
                   "包内没有 dist/ 交付/ __pycache__ *.pyc *.fpk（%s）"
                   % (leaked[:3] or "干净"))
        qr_images = [name for name in names if is_qr_image(name)]
        check.case(not qr_images,
                   "源码包内没有独立收款码图片（%s）"
                   % (qr_images[:3] or "干净"))
    return check.finish()


# --------------------------------------------------------------------------- #
# stamp / inspect
# --------------------------------------------------------------------------- #


def stamp(source, target):
    """Rewrite the fpk with normalised modes and a matching manifest checksum."""
    with tarfile.open(source, "r:gz") as outer:
        members = outer.getmembers()
        blobs = {}
        for member in members:
            if member.isreg():
                handle = outer.extractfile(member)
                blobs[member.name] = handle.read() if handle else b""

    if "app.tgz" in blobs:
        with tarfile.open(fileobj=io.BytesIO(blobs["app.tgz"]), mode="r:gz") as app:
            app_members = app.getmembers()
            app_data = {}
            for member in app_members:
                if member.isreg():
                    handle = app.extractfile(member)
                    app_data[member.name] = handle.read() if handle else b""
            blobs["app.tgz"] = tar_bytes(
                app_members, lambda m: app_data.get(m.name, b""))

    if "manifest" in blobs:
        manifest = blobs["manifest"].decode("utf-8")
        if "app.tgz" in blobs:
            digest = hashlib.md5(blobs["app.tgz"]).hexdigest()
            manifest = re.sub(r"(?m)^(checksum\s*=\s*).*$",
                              lambda m: m.group(1) + digest, manifest)
        blobs["manifest"] = manifest.encode("utf-8")

    with tarfile.open(source, "r:gz") as outer:
        members = outer.getmembers()
        with tarfile.open(target, "w:gz", format=tarfile.GNU_FORMAT) as out:
            for member in members:
                member.mode = wanted_mode(member.name, member.isdir())
                if member.name in blobs:
                    payload = blobs[member.name]
                    member.size = len(payload)
                    out.addfile(member, io.BytesIO(payload))
                else:
                    out.addfile(member)
    return target


def cmd_stamp(args):
    stamp(args.fpk, args.output or args.fpk)
    print("stamp: %s 权限已归一（cmd/* 755，其余 644，目录 755），"
          "manifest checksum 已按新的 app.tgz 重算" % (args.output or args.fpk))
    return 0


def cmd_fpk(args):
    check = Check("成品 fpk 检查")
    with tarfile.open(args.fpk, "r:gz") as outer:
        members = {m.name: m for m in outer.getmembers()}
        blobs = {}
        for name, member in members.items():
            if member.isreg():
                handle = outer.extractfile(member)
                blobs[name] = handle.read() if handle else b""

        check.case("manifest" in members, "包含 manifest")
        check.case("app.tgz" in members, "包含 app.tgz")
        symlinks = [m.name for m in members.values() if m.issym() or m.islnk()]
        check.case(not symlinks, "外层无软链接（%s）" % (symlinks or "干净"))

        bad_modes = ["%s=%o" % (m.name, m.mode) for m in members.values()
                     if m.isreg() and m.mode != wanted_mode(m.name, False)]
        check.case(not bad_modes, "外层文件权限正确（%s）"
                   % (bad_modes or "cmd/* 755，其余 644"))

        fields = {}
        manifest_text = ""
        if "manifest" in blobs:
            manifest_text = blobs["manifest"].decode("utf-8")
            fields = dict(re.findall(r"(?m)^\s*([a-z_]+)\s*=\s*(.*?)\s*$",
                                     manifest_text))
        check.case(not [line for line in manifest_text.splitlines()
                        if line.startswith(";")],
                   "manifest 没被 fnpack 的行内注释规则截断")
        for key, value in sorted(IDENTITY.items()):
            check.case(fields.get(key) == value,
                       "manifest.%s = %s" % (key, fields.get(key)))
        if args.version:
            check.case(fields.get("version") == args.version,
                       "manifest.version = %s" % fields.get("version"))
        if "app.tgz" in blobs:
            digest = hashlib.md5(blobs["app.tgz"]).hexdigest()
            check.case(fields.get("checksum") == digest,
                       "checksum == md5(app.tgz)（%s）" % digest)

        with tarfile.open(fileobj=io.BytesIO(blobs.get("app.tgz", b"")),
                          mode="r:gz") as app:
            inner = app.getmembers()
            names = {m.name for m in inner}
            inner_links = [m.name for m in inner if m.issym() or m.islnk()]
            inner_modes = ["%s=%o" % (m.name, m.mode) for m in inner
                           if m.isreg() and m.mode != 0o644]
            check.case(not inner_links, "app.tgz 内无软链接（%s）" % (inner_links or "干净"))
            check.case(not inner_modes, "app.tgz 内文件 644（%s）"
                       % (inner_modes or "干净"))
            check.case("ui/donate.json" in names and "ui/index.html" in names,
                       "app.tgz 含 ui/donate.json 与 ui/index.html")

            def read_inner(name):
                member = app.getmember(name)
                handle = app.extractfile(member)
                return handle.read().decode("utf-8") if handle else ""

            donate = read_inner("ui/donate.json") if "ui/donate.json" in names else ""
            page = read_inner("ui/index.html") if "ui/index.html" in names else ""
            app_js = read_inner("ui/app.js") if "ui/app.js" in names else ""
            style = read_inner("ui/style.css") if "ui/style.css" in names else ""
            check.case("QQ群：818299505" in donate and
                       "https://github.com/LiuFudi/NiuFan" in donate,
                       "打赏文案含 Github 链接与 QQ 群号")
            check.case("<h1>NiuFan</h1>" in page, "主界面标题为 NiuFan")
            check.case("donateFeedback" in app_js and ".donate-accent" in style,
                       "反馈行的渲染与红色样式已随包")
    return check.finish()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=ROOT, help="项目根目录（默认自动识别）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("versions", help="版本号交叉校验")
    sub.add_parser("source", help="打包源码树检查")

    pkg_parser = sub.add_parser("sourcepkg", help="生成源码包（tar.gz + zip）")
    pkg_parser.add_argument("outdir", help="写到哪个目录（通常是 dist/）")

    stamp_parser = sub.add_parser("stamp", help="归一成品 fpk 的权限位")
    stamp_parser.add_argument("fpk")
    stamp_parser.add_argument("--output", help="写到这里（默认原地重写）")

    fpk_parser = sub.add_parser("fpk", help="检查成品 fpk")
    fpk_parser.add_argument("fpk")
    fpk_parser.add_argument("--version", help="期望的版本号")

    args = parser.parse_args(argv)
    if not os.path.isdir(os.path.join(args.root, "package")):
        print("release_checks: %s 下没有 package/ 目录" % args.root, file=sys.stderr)
        return 2
    return {"versions": cmd_versions, "source": cmd_source,
            "sourcepkg": cmd_sourcepkg, "stamp": cmd_stamp,
            "fpk": cmd_fpk}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
