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

# --- npm package: a literal version, padded to three components --------------
# npm rejects a two-component version outright, so package.json cannot simply
# copy git_roost.py. Compare against the padded form rather than exempting the
# file -- "exempt" would mean 0.1.0 could sit there forever after git_roost.py
# moved on. The padding branch is a no-op while git_roost.py carries three
# components (release-please's parser hard-fails on two, so it must), but it is
# what makes this check survive a future slip back to a two-component version.
case $VERSION in
  *.*.*) NPM_WANT=$VERSION ;;
  *.*)   NPM_WANT=$VERSION.0 ;;
  *)     NPM_WANT=$VERSION.0.0 ;;
esac
npm_version=$(sed -n 's/^[[:space:]]*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
              package.json | head -1)
report "package.json version" "$npm_version" "$NPM_WANT"

# --- Homebrew formula ---------------------------------------------------------
# The version appears in the formula EXACTLY ONCE, on the `version` line; the
# url interpolates it for both the tag and the filename. That shape is not
# cosmetic -- roost embedded the version twice on its url line, a bump rewrote
# only the first occurrence, and the release shipped a url whose tag said the
# new version while its filename still said the old one. So the url check here
# is structural: assert the interpolation is present, rather than parsing a
# literal that should not exist in the first place.
if [ -f packaging/git-roost.rb ]; then
  rb_version=$(sed -n 's/^[[:space:]]*version "\([^"]*\)".*/\1/p' packaging/git-roost.rb)
  report "git-roost.rb version" "$rb_version" "$VERSION"

  # The url must point at the sdist uploaded to the GitHub Release, not at
  # GitHub's archive/refs/tags/ URL. That URL is not a release asset, so GitHub
  # never counts `brew install` traffic in the repo's release download_count --
  # roost's entire Homebrew audience was invisible until this changed there.
  #
  # The filename normalises git-roost to git_roost (PEP 625, applied by
  # hatchling). Asserted here as well as written in the formula because a hyphen
  # there produces a URL that 404s at `brew install` time, which is exactly the
  # class of failure this script exists to catch before a tag goes out.
  if grep -qE '^[[:space:]]*url ".*/releases/download/v#\{version\}/git_roost-#\{version\}\.tar\.gz"' \
       packaging/git-roost.rb; then
    printf '  ok    %-24s release-asset sdist, interpolated\n' "git-roost.rb url"
  else
    echo "  DRIFT git-roost.rb url          want .../releases/download/v#{version}/git_roost-#{version}.tar.gz" >&2
    echo "        (archive/refs/tags/ is not a release asset, so brew installs go uncounted)" >&2
    fail=1
  fi

  # No other version literal may appear anywhere in the formula -- one copy, on
  # the `version` line, is the whole design, and a stale number in the curl hint
  # comment is how that design quietly rots. python@3.x interpreter pins and the
  # Apache-2.0 SPDX id are not release versions; exclude them.
  rb_literals=$(grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' packaging/git-roost.rb \
                | grep -vxE '3\.[0-9]+|2\.0' | sort -u | tr '\n' ' ') || rb_literals=""
  # The `|| rb_literals=""` matters: under `set -o pipefail` a formula with no
  # version literal at all would make grep exit 1 and kill this script with no
  # output -- the empty case has to reach report() and be named, not vanish.
  rb_literals=${rb_literals% }
  report "git-roost.rb literals" "$rb_literals" "$VERSION"

  # The version is only half the pin. Checking it alone would pass a formula
  # whose sha256 is a placeholder or a stale paste -- green here, broken at
  # `brew install`. Shape first, because it costs nothing and catches the
  # placeholder case offline.
  # Capture whatever is between the quotes, not just hex -- otherwise a
  # placeholder like REPLACE_ME captures as empty and reports "<missing>",
  # which sends you looking for an absent line rather than a wrong one.
  # Anchored to the start of the line rather than an unanchored '.*sha256':
  # the formula's header comment names the sha256 line when explaining what the
  # release workflow rewrites, and an unanchored match reads that prose instead
  # of the directive -- reporting a malformed digest for a formula whose digest
  # is fine.
  rb_sha=$(sed -n 's/^[[:space:]]*sha256 "\([^"]*\)".*/\1/p' packaging/git-roost.rb | head -1)
  if printf '%s' "$rb_sha" | grep -qE '^[0-9a-f]{64}$'; then
    printf '  ok    %-24s well-formed\n' "git-roost.rb sha256"
  else
    printf '  DRIFT %-24s not a 64-char hex digest: %s\n' \
      "git-roost.rb sha256" "${rb_sha:-<missing>}" >&2
    fail=1
  fi

  # Finally, report the in-repo digest against the published sdist. REPORT, not
  # gate: this whole block is informational and can never set fail. The 200 case
  # below explains why that is forced rather than lenient, and why the check the
  # script was actually written for still fails offline, above.
  #
  # It needs the network, so it degrades to an explicit skip -- never to a
  # silent pass. The reasons it can fail to fetch are not the same thing, and
  # saying which one happened matters. "No release asset yet" is the expected
  # state before the first tag: not a problem, but it does mean the sha256 is
  # UNVERIFIED and the release workflow must recompute it before the tap-push
  # job copies the formula. Collapsing that into one vague "could not fetch"
  # alongside "you are offline" is how an unverified hash reaches a user.
  rb_url=$(sed -n 's/^[[:space:]]*url "\([^"]*\)".*/\1/p' packaging/git-roost.rb | head -1)
  rb_url=${rb_url//'#{version}'/$VERSION}
  if [ -z "$rb_url" ]; then
    printf '  DRIFT %-24s no url line\n' "git-roost.rb url" >&2
    fail=1
  elif ! command -v curl >/dev/null 2>&1; then
    printf '  --    %-24s no curl; cannot verify %s\n' "git-roost.rb sha256" "$rb_url"
  else
    tmp=$(mktemp)
    code=$(curl -sL --max-time 60 -o "$tmp" -w '%{http_code}' "$rb_url" 2>/dev/null) || code=000
    case "$code" in
      200)
        # The asset exists. Compare -- but NEVER gate on the result, because a
        # mismatch here is the normal, structurally guaranteed state, not drift.
        #
        # The plain reason is causality: the digest of a release asset does not
        # exist on the commit the release is cut FROM. The asset is built from
        # that commit. So no value committed here could ever have matched, and
        # there was never a version of this assertion that could pass on a
        # pre-release commit.
        #
        # There is a second, independent reason, and it is worth naming so that
        # nobody "fixes" the first one and expects the gate back. The sdist ships
        # packaging/ (see [tool.hatch.build.targets.sdist] in pyproject.toml), so
        # git-roost.rb is INSIDE the tarball being hashed. Writing the asset's
        # true digest into the formula changes the tarball, which changes the
        # digest. "The formula's sha256 equals the hash of an sdist containing
        # that same sha256" is a hash fixed point: not findable. Dropping
        # packaging/ from the sdist would break the fixed point and still leave
        # the causality problem untouched. Neither is the kind of thing a build
        # should fail on.
        #
        # The re-run symptom that surfaced all this: a re-run of release.yml for
        # an already-published tag (retrying after npm or the tap failed on a
        # one-time-setup gap) fetched 200, compared stale-vs-real, exited 1, and
        # killed the build job before anything downstream could retry -- exactly
        # when a re-run matters most.
        #
        # Nothing is lost by not gating here, and this is the part worth being
        # clear about. The case this script was written for -- a formula left
        # behind on an OLDER release -- is caught by the `git-roost.rb version`
        # and `git-roost.rb literals` checks above, which compare against
        # __version__ directly, offline, and DO fail. Verified: a formula pinned
        # at 0.0.9 exits 1 on those two lines while this digest comparison is
        # reporting "matches". The digest never carried that signal.
        #
        # And the digest users actually install is not this one: the homebrew-tap
        # job in release.yml computes sha256sum over the local sdist, rewrites
        # the formula, and asserts the value it wrote before pushing to the tap.
        # That job is the authority; this line is a report.
        actual=$(shasum -a 256 "$tmp" | cut -d' ' -f1)
        if [ "$actual" = "$rb_sha" ]; then
          # Reachable, and good news when it happens: a post-release commit that
          # refreshed the digest for an already-published tag. Then -- and only
          # then -- the in-repo formula is accurate documentation of what shipped.
          printf '  ok    %-24s matches the published sdist\n' "git-roost.rb sha256"
        else
          printf '  --    %-24s differs from the published sdist, which is expected:\n' \
            "git-roost.rb sha256"
          printf '        expected: an asset digest cannot exist on the commit it is built\n'
          printf '        from, and the sdist ships this file, so it cannot match itself.\n'
          printf '        published %s\n' "$actual"
          printf '        in repo   %s\n' "$rb_sha"
          printf '        The homebrew-tap job computes and asserts the real one at push time.\n'
        fi
        ;;
      404)
        printf '  --    %-24s UNVERIFIED -- no release asset at\n' "git-roost.rb sha256"
        printf '        %s\n' "$rb_url"
        printf '        so the digest in the formula is a stand-in. The release workflow\n'
        printf '        must recompute it on the next tag, before the tap-push job runs.\n'
        ;;
      *)
        printf '  --    %-24s could not fetch (HTTP %s -- offline?) %s\n' \
          "git-roost.rb sha256" "$code" "$rb_url"
        ;;
    esac
    rm -f "$tmp"
  fi
else
  # A formula that exists and disagrees is worse than none. Do not "fix" an
  # absent one by writing a placeholder URL or a placeholder hash -- a formula
  # pinned to a URL that 404s, or to a sha256 that doesn't match, fails at
  # install time rather than here, which is the failure mode this whole script
  # exists to prevent.
  printf '  --    %-24s not written yet (no release tag to pin)\n' "packaging/git-roost.rb"
fi

if [ "$fail" -ne 0 ]; then
  cat >&2 <<EOF

Version drift. Every artifact above must say $VERSION.

  git-roost.1   .TH GIT-ROOST 1 "<date>" "git-roost $VERSION" "User Commands"
  package.json  "version": "$NPM_WANT"   (npm needs three components)

EOF
  exit 1
fi

echo "all version-bearing artifacts agree on $VERSION"
