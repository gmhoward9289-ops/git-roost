#!/bin/sh
# Build the Windows portable zip winget installs. Usage: packaging/build-windows-zip.sh [version]
#
# Mirrors build-deb.sh's shape: one architecture-independent bundle, no build
# step. The zip holds exactly two files -- packaging/git-roost.cmd (the batch
# sibling of bin/git-roost.js, since winget has no npm-postinstall or
# brew-depends_on equivalent to lean on) and git_roost.py itself -- so a
# winget "zip"/"portable" install is just those two files dropped on disk and
# git-roost.cmd symlinked onto PATH as PortableCommandAlias.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=${1:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/git_roost.py")}
[ -n "$VERSION" ] || { echo "could not determine version" >&2; exit 1; }

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT

cp "$ROOT/packaging/git-roost.cmd" "$ROOT/git_roost.py" "$BUILD/"

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/git-roost-${VERSION}-windows.zip"
rm -f "$OUT"
(cd "$BUILD" && zip -X "$OUT" git-roost.cmd git_roost.py > /dev/null)
echo "dist/git-roost-${VERSION}-windows.zip"
