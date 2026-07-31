#!/bin/sh
# Build a .deb for git-roost. Usage: packaging/build-deb.sh [version]
#
# Deliberately a plain dpkg-deb tree rather than a debian/ source package:
# git-roost is one architecture-independent script with no build step and no
# dependencies beyond python3 itself, so debhelper would add ceremony and no
# correctness. Mirrors roost's packaging, which is proven.
#
# Installed as `git-roost`; git also picks it up as the subcommand `git roost`,
# since git resolves `git <x>` to a `git-<x>` on PATH. That is free and is why
# the hyphenated name is worth keeping on disk even though the module uses an
# underscore.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
VERSION=${1:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/git_roost.py")}
[ -n "$VERSION" ] || { echo "could not determine version" >&2; exit 1; }

BUILD=$(mktemp -d)
trap 'rm -rf "$BUILD"' EXIT
PKG="$BUILD/git-roost_${VERSION}_all"

mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$PKG/usr/share/man/man1" \
         "$PKG/usr/share/doc/git-roost"

# Installed without the .py suffix: the shebang and the executable bit are what
# make it a command.
install -m 0755 "$ROOT/git_roost.py" "$PKG/usr/bin/git-roost"
gzip -9nc "$ROOT/git-roost.1" > "$PKG/usr/share/man/man1/git-roost.1.gz"
chmod 0644 "$PKG/usr/share/man/man1/git-roost.1.gz"
install -m 0644 "$ROOT/LICENSE" "$PKG/usr/share/doc/git-roost/copyright"

# git is a hard runtime dependency here in a way it is not for roost: with no
# git on PATH every column is empty and the tool has nothing to say.
cat > "$PKG/DEBIAN/control" <<EOF
Package: git-roost
Version: $VERSION
Section: vcs
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), git
Maintainer: George M. Howard <dev@swamplink.com>
Description: top for git across every repo and worktree
 Shows every git working tree on the machine in one table -- uncommitted work,
 drift against upstream, stash count, and how long since the last commit --
 ordered so the trees that need attention come first.
 .
 Read-only by construction: every git invocation is checked against an
 allowlist of plumbing that cannot mutate a tree, an index, or a ref. It
 sends nothing anywhere.
EOF

dpkg-deb --build --root-owner-group "$PKG" > /dev/null
mkdir -p "$ROOT/dist"
mv "$BUILD/git-roost_${VERSION}_all.deb" "$ROOT/dist/"
echo "dist/git-roost_${VERSION}_all.deb"
