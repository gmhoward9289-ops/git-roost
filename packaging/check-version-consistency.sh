#!/usr/bin/env bash
# Assert every version-bearing artifact agrees with __version__ in git_roost.py.
#
# Ported from roost, where the absence of this check cost a release: the v0.3
# bump touched only README.md and roost.py, so the man page still said
# "roost 0.2" and the Homebrew formula still pointed at the v0.2 tarball. The
# formula case cannot report itself -- Homebrew derives `version` from the
# tarball URL, so a stale formula checks the stale number against the stale
# tarball and passes, and `brew install` ships the old release with every check
# green. Starting git-roost with the check already in place is cheaper than
# discovering the same thing at v0.3 again.
#
# Artifacts that *derive* the version (build-deb.sh seds it, hatch reads it via
# [tool.hatch.version]) cannot drift and are not listed. Only the ones that
# embed it as literal text need checking.

set -euo pipefail
cd "$(dirname "$0")/.." || exit 2

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' git_roost.py)
if [ -z "$VERSION" ]; then
  echo "FATAL: could not read __version__ from git_roost.py" >&2
  exit 2
fi

echo "git_roost.py __version__ = $VERSION"
fail=0

report() { # <artifact> <found> <want>
  if [ "$2" = "$3" ]; then
    printf '  ok    %-24s %s\n' "$1" "$2"
  else
    printf '  DRIFT %-24s found %-10s want %s\n' "$1" "${2:-<unparseable>}" "$3" >&2
    fail=1
  fi
}

# --- man page: .TH GIT-ROOST 1 "<date>" "git-roost <version>" "..." ----------
man_version=$(sed -n '1s/.*"git-roost \([^"]*\)".*/\1/p' git-roost.1)
report "git-roost.1 .TH header" "$man_version" "$VERSION"

# --- CLI ---------------------------------------------------------------------
cli_version=$(python3 git_roost.py --version 2>&1 | sed -n 's/^git-roost \(.*\)$/\1/p')
report "git-roost --version" "$cli_version" "$VERSION"

# --- pyproject must stay dynamic --------------------------------------------
# A literal version = "..." here would be a second copy to drift.
if grep -qE '^\s*version\s*=\s*"' pyproject.toml; then
  echo "  DRIFT pyproject.toml           has a literal version=; it must stay dynamic" >&2
  echo "        (keep [tool.hatch.version] path = \"git_roost.py\" as the only source)" >&2
  fail=1
else
  printf '  ok    %-24s dynamic (from git_roost.py)\n' "pyproject.toml"
fi

# --- Homebrew formula, once it exists ----------------------------------------
# The repo has no remote yet, so there is no tarball URL to pin and no formula.
# Absence is fine; a formula that exists and disagrees is not. Do not "fix" this
# by writing a placeholder URL -- a formula pinned to a URL that 404s fails at
# install time, not here.
if [ -f packaging/git-roost.rb ]; then
  rb_version=$(sed -n 's#.*url "https://github.com/[^"]*/archive/refs/tags/v\([^"]*\)\.tar\.gz".*#\1#p' \
               packaging/git-roost.rb)
  report "git-roost.rb url tag" "$rb_version" "$VERSION"
  rb_hint=$(sed -n 's#.*curl -sL https://github.com/[^ ]*/archive/refs/tags/v\([0-9][^ ]*\)\.tar\.gz.*#\1#p' \
            packaging/git-roost.rb | head -1)
  report "git-roost.rb curl comment" "$rb_hint" "$VERSION"
else
  printf '  --    %-24s not written yet (no remote; see README)\n' "packaging/git-roost.rb"
fi

if [ "$fail" -ne 0 ]; then
  cat >&2 <<EOF

Version drift. Every artifact above must say $VERSION.

  git-roost.1   .TH GIT-ROOST 1 "<date>" "git-roost $VERSION" "User Commands"

EOF
  exit 1
fi

echo "all version-bearing artifacts agree on $VERSION"
