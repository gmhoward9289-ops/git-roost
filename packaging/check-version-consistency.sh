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
# by writing a placeholder URL or a placeholder hash -- a formula pinned to a URL
# that 404s, or to a sha256 that doesn't match, fails at install time rather than
# here, which is the failure mode this whole script exists to prevent.
if [ -f packaging/git-roost.rb ]; then
  rb_version=$(sed -n 's#.*url "https://github.com/[^"]*/archive/refs/tags/v\([^"]*\)\.tar\.gz".*#\1#p' \
               packaging/git-roost.rb)
  report "git-roost.rb url tag" "$rb_version" "$VERSION"
  rb_hint=$(sed -n 's#.*curl -sL https://github.com/[^ ]*/archive/refs/tags/v\([0-9][^ ]*\)\.tar\.gz.*#\1#p' \
            packaging/git-roost.rb | head -1)
  report "git-roost.rb curl comment" "$rb_hint" "$VERSION"

  # The tag is only half the pin. Checking the URL alone would pass a formula
  # whose sha256 is a placeholder or a stale paste -- green here, broken at
  # `brew install`. Shape first, because it costs nothing and catches the
  # placeholder case offline.
  # Capture whatever is between the quotes, not just hex -- otherwise a
  # placeholder like REPLACE_ME captures as empty and reports "<missing>",
  # which sends you looking for an absent line rather than a wrong one.
  rb_sha=$(sed -n 's/.*sha256 "\([^"]*\)".*/\1/p' packaging/git-roost.rb | head -1)
  if printf '%s' "$rb_sha" | grep -qE '^[0-9a-f]{64}$'; then
    printf '  ok    %-24s well-formed\n' "git-roost.rb sha256"
  else
    printf '  DRIFT %-24s not a 64-char hex digest: %s\n' \
      "git-roost.rb sha256" "${rb_sha:-<missing>}" >&2
    fail=1
  fi

  # Then the real check: does that digest actually match the tarball? This is
  # the one that would have caught roost shipping a stale formula. Needs the
  # network, so it degrades to an explicit skip -- never to a silent pass.
  rb_url=$(sed -n 's/.*url "\([^"]*\)".*/\1/p' packaging/git-roost.rb | head -1)
  if [ -n "$rb_url" ] && command -v curl >/dev/null 2>&1; then
    tmp=$(mktemp)
    if curl -sfL --max-time 60 "$rb_url" -o "$tmp" 2>/dev/null; then
      actual=$(shasum -a 256 "$tmp" | cut -d' ' -f1)
      report "git-roost.rb sha256 vs tarball" "$actual" "$rb_sha"
    else
      printf '  --    %-24s could not fetch %s (offline, or the tag does not exist)\n' \
        "git-roost.rb tarball" "$rb_url"
    fi
    rm -f "$tmp"
  fi
else
  # The remote exists; what is missing is a tag to hash. See the note in README.
  printf '  --    %-24s not written yet (no release tag to pin)\n' "packaging/git-roost.rb"
fi

if [ "$fail" -ne 0 ]; then
  cat >&2 <<EOF

Version drift. Every artifact above must say $VERSION.

  git-roost.1   .TH GIT-ROOST 1 "<date>" "git-roost $VERSION" "User Commands"

EOF
  exit 1
fi

echo "all version-bearing artifacts agree on $VERSION"
