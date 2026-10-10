#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LiuFudi
#
# This file is part of niufan, licensed under the GNU General Public
# License version 3 or (at your option) any later version.
# See the LICENSE file for the full text.
# Build the NiuFan .fpk and stamp the version into the artifact name.
#
# `fnpack build` always emits "<appname>.fpk" inside the source tree and offers
# no way to override the name, which makes two builds impossible to tell apart.
# This wrapper reads the version out of package/manifest, checks it against the
# other places that carry it, builds, normalises the modes inside the fpk
# (fnpack may write 0666/0777 for everything) and verifies the
# result before moving it to
# dist/<appname>-<version>-<arch>-<system>.fpk  (e.g. niufan-2.2.5-x86-fn.fpk).
# It also builds the matching source package and drops every artifact of
# another version, so the workspace only ever holds the current release
# (《通用开发规范》3.1).
#
# Every check lives in tools/release_checks.py.  Any failure exits non-zero and
# leaves no artifact behind.
#
# Requires `fnpack`, which ships with fnOS at /usr/local/bin/fnpack.
#
# Usage:  ./build-fpk.sh [source-dir]      (default: ./package)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="${1:-$HERE/package}"
SOURCE="$(cd "$SOURCE" && pwd)"
DIST="$HERE/dist"
CHECKS="$HERE/tools/release_checks.py"

MANIFEST="$SOURCE/manifest"
[ -f "$MANIFEST" ] || { echo "build-fpk: no manifest in $SOURCE" >&2; exit 1; }

PYTHON="$(command -v python3 || command -v python || echo /usr/bin/python3)"
command -v "$PYTHON" >/dev/null 2>&1 || {
    echo "build-fpk: python3 not found (tools/release_checks.py needs it)" >&2
    exit 1
}
[ -f "$CHECKS" ] || { echo "build-fpk: $CHECKS is missing" >&2; exit 1; }

command -v fnpack >/dev/null 2>&1 || {
    echo "build-fpk: fnpack not found. It ships with fnOS at /usr/local/bin/fnpack." >&2
    exit 1
}

manifest_value() {
    sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$MANIFEST" \
        | head -n1 | tr -d '\r' | sed 's/[[:space:]]*$//'
}

APPNAME="$(manifest_value appname)"
VERSION="$(manifest_value version)"
ARCH="$(manifest_value platform)"
[ -n "$ARCH" ] || ARCH="x86"
#: fnOS packages carry the "fn" system tag; override with SYSTEM=... if needed.
SYSTEM="${SYSTEM:-fn}"
if [ -z "$APPNAME" ] || [ -z "$VERSION" ]; then
    echo "build-fpk: appname/version missing from $MANIFEST" >&2
    exit 1
fi

# fnpack rejects the literal test suffix "-rN"; it only accepts x.y.z or
# x.y.z-<integer>.  Official releases keep x.y.z unchanged; local -rN builds
# are mapped to the equivalent numeric suffix before packaging.
FNPACK_VERSION="$VERSION"
if [[ "$VERSION" =~ ^([0-9]+\.[0-9]+\.[0-9]+)-r([0-9]*)$ ]]; then
    FNPACK_VERSION="${BASH_REMATCH[1]}-${BASH_REMATCH[2]:-1}"
    echo "build-fpk: fnpack 版本映射 $VERSION -> $FNPACK_VERSION"
fi

# 1. The version is written in several places (manifest, the daemon constant,
#    the CHANGELOG, the README badge) and they must agree: a mismatch means the
#    artifact would report a version the app center never sees.  Hard failure.
"$PYTHON" "$CHECKS" --root "$HERE" versions || exit 1

# 2. Stale bytecode and leftovers would end up inside the fpk; it happens
#    easily, because any `python3 -m py_compile` recreates __pycache__.
find "$SOURCE" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$SOURCE" -name '*.py[co]' -delete 2>/dev/null || true
find "$SOURCE" -name '*.fpk' -delete 2>/dev/null || true

# 3. The donation QR codes ship inlined as data URIs.  Regenerate the module
#    from the external source directory when it is available; otherwise keep
#    the committed generated module so a clean checkout can still build.
GENERATOR="$HERE/tools/make_donate_qr.py"
if [ -f "$GENERATOR" ]; then
    QR_DIR="${NIUFAN_QR_DIR:-/vol1/1000/软件开发/收款码}"
    "$PYTHON" "$GENERATOR" "$HERE" "$QR_DIR"
fi
[ -f "$SOURCE/app/ui/donate-qr.js" ] || {
    echo "build-fpk: app/ui/donate-qr.js is missing - run tools/make_donate_qr.py" >&2
    exit 1
}

# 4. Packaging tree: required files, the identifiers that must never move, no
#    symlinks, no scaffold placeholders, no host paths or credentials.
"$PYTHON" "$CHECKS" --root "$HERE" source || exit 1

mkdir -p "$DIST"
ARTIFACT="$DIST/${APPNAME}-${VERSION}-${ARCH}-${SYSTEM}.fpk"

# Drop the legacy unversioned file and any rebuild of this same version.  The
# final cleanup below removes artifacts from every other version.
rm -f "$SOURCE/${APPNAME}.fpk" "$ARTIFACT"

echo "build-fpk: building $APPNAME $VERSION"
BUILD_ROOT="$(mktemp -d)"
trap 'rm -rf "$BUILD_ROOT"' EXIT
BUILD_SOURCE="$BUILD_ROOT/package"
mkdir -p "$BUILD_SOURCE"
cp -a "$SOURCE/." "$BUILD_SOURCE/"
if [ "$FNPACK_VERSION" != "$VERSION" ]; then
    sed -i "s/^version[[:space:]]*=.*/version               = $FNPACK_VERSION/" \
        "$BUILD_SOURCE/manifest"
fi
( cd "$BUILD_SOURCE" && fnpack build --directory . )

# `fnpack build` reports success even when it produced nothing.
BUILT="$BUILD_SOURCE/${APPNAME}.fpk"
[ -f "$BUILT" ] || { echo "build-fpk: fnpack produced no $BUILT" >&2; exit 1; }

# 5. Normalise the modes and re-stamp the manifest checksum.
"$PYTHON" "$CHECKS" stamp "$BUILT" || exit 1
mv -f "$BUILT" "$ARTIFACT"

# 6. Inspect what is actually inside the artifact: version, display name,
#    identity fields, checksum, modes, symlinks, and the shipped UI strings.
"$PYTHON" "$CHECKS" --root "$HERE" fpk "$ARTIFACT" --version "$FNPACK_VERSION" || exit 1

# 7. The source package is produced here, from the same tree, so the two can
#    never drift apart (《飞牛 fpk 开发规范》9.4).
"$PYTHON" "$CHECKS" --root "$HERE" sourcepkg "$DIST" || exit 1

# 8. Only the current version stays in the workspace: the unversioned leftover
#    and every artifact of another version are removed *after* this build has
#    produced and verified its own (《通用开发规范》3.1).
KEEP="${APPNAME}-${VERSION}-${ARCH}-${SYSTEM}.fpk ${APPNAME}-${VERSION}-source.tar.gz ${APPNAME}-${VERSION}-source.zip SHA256SUMS"
for file in "$DIST"/*; do
    [ -e "$file" ] || continue
    case " $KEEP " in
        *" $(basename "$file") "*) ;;
        *) echo "build-fpk: 清理旧产物 $(basename "$file")"
           rm -f "$file" ;;
    esac
done

( cd "$DIST" && sha256sum \
    "${APPNAME}-${VERSION}-${ARCH}-${SYSTEM}.fpk" \
    "${APPNAME}-${VERSION}-source.tar.gz" \
    "${APPNAME}-${VERSION}-source.zip" > SHA256SUMS )
chmod 644 "$ARTIFACT" \
    "$DIST/${APPNAME}-${VERSION}-source.tar.gz" \
    "$DIST/${APPNAME}-${VERSION}-source.zip" \
    "$DIST/SHA256SUMS"

echo "build-fpk: $ARTIFACT"
sha256sum "$ARTIFACT" 2>/dev/null || true
ls -la "$DIST"
