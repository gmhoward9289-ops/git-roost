#!/usr/bin/env bash
# One command that answers "does every channel actually serve the version
# git-roost claims to be, and if not, what exactly is left to do?"
#
# STRICTLY READ-ONLY. It fetches from registries and reads local files; it
# publishes nothing, installs nothing, and runs no command that can mutate a
# tree, an index, or a ref -- every GitHub call here is a GET. That is not
# incidental politeness: it mirrors git-roost's own central design guarantee
# (every git call goes through an allowlist of non-mutating plumbing; see git()
# in git_roost.py and the CI job that enforces it), and a diagnostic that could
# change the thing it diagnoses would be the one tool in this repo allowed to
# lie about it.
#
# Ported from leghorn's copy by way of roost's, which exists because roost's npm
# channel sat frozen at 0.6.1 while PyPI served 0.8.0 -- two releases where
# `npm i -g roost-top` handed people old software. The publish job had been
# failing with a 404-on-PUT (npm's way of saying unauthorized) since v0.7.0, and
# because release.yml lets each publish job fail alone so the rest still land,
# the red job scrolled away unread. Job status is not the same fact as "the
# registry serves it", so this asks the registries.
#
# Two adaptations to git-roost's facts, both load-bearing:
#
# 1. No name split. roost had to publish as roost-top because PyPI and npm both
#    hold "roost"; here the distribution, command, module, repo, formula and
#    .deb are all the bare `git-roost` (see the comment at the top of
#    pyproject.toml). Nothing to translate, so nothing to get wrong.
#
# 2. A fourth state: TODO. The siblings are binary underneath their PASS/PENDING
#    labels -- anything not PASS sets fail=1 -- because by the time they were
#    written every channel had published at least once, so "behind" was the only
#    way to be not-PASS. git-roost has never published to PyPI or npm at all,
#    and its only release (v0.1) was cut by hand, so on a first run most channels
#    are legitimately absent. Reusing PENDING for that would print a wall of
#    near-identical lines on every run and train the reader to skim past the one
#    that is real -- exactly the cry-wolf failure the PASS/PENDING split was
#    invented to avoid. So:
#
#      PASS    the channel serves $VERSION
#      TODO    the channel has never been established -- nothing is wrong yet,
#              there is just a one-time setup nobody has done. Never fails.
#      PENDING the channel is established, is behind, and the release is recent
#              enough that propagation is a sufficient explanation. Never fails.
#      FAIL    the channel is established and disagrees with a release old
#              enough that propagation is not an excuse. This is the only state
#              that exits non-zero, so the daily scheduled run stays quiet until
#              something is genuinely broken.
#
# The PENDING window is measured from the GitHub release's publishedAt, not from
# the wall clock, because "it only just went out" is a defence only for a
# release that actually did only just go out.
set -u

OWNER=gmhoward9289-ops
REPO=$OWNER/git-roost
DIST=git-roost                  # dist == command == module == repo == formula
TAP=$OWNER/homebrew-tap
WINGET_ID=$OWNER.git-roost
WINGET_PKGS=microsoft/winget-pkgs
ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Minutes after a release during which a lagging channel is PENDING, not FAIL.
# 60 is generous on purpose: PyPI and npm serve in seconds, but the tap push is
# a second workflow writing to a second repo, and a queued runner can eat half
# an hour before it even starts.
GRACE_MIN=${PUBLISH_DOCTOR_GRACE_MIN:-60}

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/git_roost.py")
if [ -z "$VERSION" ]; then
  echo "FATAL: could not read __version__ from git_roost.py" >&2
  exit 2
fi

fails=0
pendings=0
todos=0

say()  { printf '  %-8s %-12s %s\n' "$1" "$2" "$3"; }
pass() { say PASS "$1" "$2"; }
skip() { say "--" "$1" "$2"; }
todo() { say TODO "$1" "$2"; todos=$((todos + 1)); }
pend() { say PENDING "$1" "$2"; pendings=$((pendings + 1)); }
fail() { say FAIL "$1" "$2"; fails=$((fails + 1)); }

# A channel that is established but behind: PENDING inside the propagation
# window, FAIL outside it. Every "registry has X, want Y" case routes through
# here rather than deciding for itself, so the window is defined in one place.
lagging() { # <channel> <message>
  if [ "$fresh" = 1 ]; then
    pend "$1" "$2 [$why_fresh]"
  else
    fail "$1" "$2"
  fi
}

sha256_of() { # <file>
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

echo "git-roost publish doctor -- version $VERSION (read-only)"
echo

# --- how old is the release? -------------------------------------------------
# Everything downstream needs this, so resolve it first. No release for this
# version means nothing downstream could have propagated yet, and fresh=1 makes
# the run report PENDING rather than FAIL -- the right answer while a bump is
# still in flight.
published=$(gh release view "v$VERSION" --repo "$REPO" --json publishedAt \
              --jq '.publishedAt' 2>/dev/null)
if [ -n "${published:-}" ]; then
  age_min=$(python3 -c '
import datetime, sys
t = datetime.datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ")
t = t.replace(tzinfo=datetime.timezone.utc)
print(int((datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() // 60))
' "$published" 2>/dev/null)
  : "${age_min:=99999}"
  if [ "$age_min" -lt "$GRACE_MIN" ]; then fresh=1; else fresh=0; fi
  why_fresh="release is ${age_min}m old, inside the ${GRACE_MIN}m window"
else
  age_min=0
  fresh=1
  # Not the same reason as "published recently", and saying so matters: this is
  # the mid-bump state, where the version file has moved and nothing has been
  # released under it yet. Reporting it as "inside the 60m window" would send
  # the reader looking for a release that does not exist.
  why_fresh="no v$VERSION release yet, so nothing downstream could have caught up"
fi

# --- man page ----------------------------------------------------------------
# Local text: no network, no propagation, so this can never legitimately be
# PENDING. It either agrees or it is drift. Checked here as well as in
# check-version-consistency.sh because that script only runs in CI on a branch,
# and the failure it was written for -- roost shipping a v0.3 whose man page
# still said 0.2 -- is only visible after the release, which is when this runs.
man_version=$(sed -n '1s/.*"git-roost \([^"]*\)".*/\1/p' "$ROOT/git-roost.1")
if [ "${man_version:-}" = "$VERSION" ]; then
  pass "man page" "git-roost.1 .TH says $man_version"
else
  fail "man page" "git-roost.1 .TH says ${man_version:-<unparseable>}, want $VERSION"
fi

# --- git tag -----------------------------------------------------------------
# Asked of the remote, not of the local clone: a tag that exists only here is a
# tag nobody can install from. `gh api` on a refs endpoint is a GET.
tags=$(gh api "repos/$REPO/git/refs/tags" --jq '.[].ref' 2>/dev/null | sed 's#refs/tags/##')
latest_tag=$(printf '%s\n' "$tags" | grep -v '^$' | sed 's/^v//' | sort -V | tail -1)
if printf '%s\n' "$tags" | grep -qx "v$VERSION"; then
  pass "git tag" "v$VERSION is on the remote"
elif [ -z "${latest_tag:-}" ]; then
  todo "git tag" "no tags on $REPO yet -- nothing has ever been released"
elif [ "$(printf '%s\n%s\n' "$latest_tag" "$VERSION" | sort -V | tail -1)" = "$VERSION" ]; then
  # The version file is ahead of the newest tag: a bump landed and the tag has
  # not been cut. Normal between releases, so PENDING regardless of the window
  # -- there is no release whose age could be measured.
  pend "git tag" "newest tag is v$latest_tag, git_roost.py says $VERSION -- tag not cut yet"
else
  # The newest tag is *ahead* of __version__. A real disagreement: the repo
  # claims to be older than something already published under its name.
  fail "git tag" "remote has v$latest_tag but git_roost.py says $VERSION -- the version file is behind the tag"
fi

# --- GitHub release ----------------------------------------------------------
assets=$(gh release view "v$VERSION" --repo "$REPO" --json assets --jq '.assets[].name' 2>/dev/null)
if [ -z "${published:-}" ]; then
  if [ -z "${latest_tag:-}" ]; then
    todo "gh release" "no releases on $REPO yet"
  else
    pend "gh release" "no release for v$VERSION -- cut it, or let release automation cut it"
  fi
elif printf '%s\n' "$assets" | grep -q 'whl$'; then
  pass "gh release" "v$VERSION with $(printf '%s\n' "$assets" | grep -c .) assets"
else
  # v0.1 was cut by hand and carries no assets at all. That is a channel not yet
  # established rather than one that broke -- the wheel and sdist start
  # appearing when release automation builds them -- so TODO, and it stays quiet
  # until then.
  todo "gh release" "v$VERSION exists but carries no wheel; a hand-cut release has no build artifacts to attach"
fi

# --- PyPI --------------------------------------------------------------------
pypi=$(curl -sf "https://pypi.org/pypi/$DIST/json" 2>/dev/null)
if [ -z "$pypi" ]; then
  todo pypi "nothing published as $DIST -- create the pending publisher at https://pypi.org/manage/account/publishing/ (project $DIST, repo $REPO, workflow release.yml, environment pypi)"
else
  pypi_ver=$(printf '%s' "$pypi" | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null)
  if [ "${pypi_ver:-}" = "$VERSION" ]; then
    # The distribution is `git-roost` but PyPI normalises filenames to
    # `git_roost-*`. Worth asserting rather than assuming: a hand-renamed
    # artifact installs fine from a URL and fails from the index, and the index
    # is the only place anyone actually installs from.
    names=$(printf '%s' "$pypi" | python3 -c '
import json, sys
print(" ".join(f["filename"] for f in json.load(sys.stdin)["urls"]))' 2>/dev/null)
    want_whl="git_roost-$VERSION-py3-none-any.whl"
    want_sdist="git_roost-$VERSION.tar.gz"
    case " $names " in
      *" $want_whl "*) ;;
      *) fail pypi "serves $pypi_ver but no $want_whl among: ${names:-<none>}" ;;
    esac
    case " $names " in
      *" $want_sdist "*) ;;
      *) fail pypi "serves $pypi_ver but no $want_sdist among: ${names:-<none>}" ;;
    esac
    pass pypi "pipx install $DIST ($pypi_ver)"
  else
    lagging pypi "registry has ${pypi_ver:-<unparseable>}, want $VERSION -- rerun the release's pypi job"
  fi
fi

# --- npm ---------------------------------------------------------------------
# The npm channel is package.json + bin/git-roost.js in this repo. Checked
# separately from the registry because "not wired up here" and "wired up but
# never published" are different jobs to do, and a doctor that collapses them
# sends you to the wrong one.
if [ ! -f "$ROOT/package.json" ]; then
  todo npm "no package.json in the repo -- the npm channel does not exist yet"
else
  npm_ver=$(curl -sf "https://registry.npmjs.org/$DIST" 2>/dev/null \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["dist-tags"]["latest"])' 2>/dev/null)
  if [ -z "${npm_ver:-}" ]; then
    todo npm "package.json exists but nothing is published as $DIST -- the first publish is manual (npm publish, browser auth), then register the Trusted Publisher at https://www.npmjs.com/package/$DIST/access"
  elif [ "$npm_ver" = "$VERSION" ] || [ "$npm_ver" = "$VERSION.0" ]; then
    # npm demands three-part semver, so a two-part version like 0.1 is published
    # as 0.1.0. Both spellings are the same release; treating them as a mismatch
    # would FAIL every run for the life of a two-part version.
    pass npm "npm i -g $DIST ($npm_ver)"
  else
    lagging npm "registry has $npm_ver, want $VERSION -- rerun the release's npm job"
  fi
fi

# --- Homebrew tap ------------------------------------------------------------
# packaging/git-roost.rb is the master copy; Formula/git-roost.rb in the tap is
# what `brew install` actually reads. Both are checked, because they fail
# differently: the master copy going stale is a repo bug, the tap not receiving
# it is a plumbing bug (a lapsed TAP_PUSH_TOKEN), and the fix differs.
tap_rb=$(curl -sf "https://raw.githubusercontent.com/$TAP/master/Formula/$DIST.rb" 2>/dev/null)
if [ -z "$tap_rb" ]; then
  todo brew "no Formula/$DIST.rb in $TAP -- copy packaging/$DIST.rb there, or set TAP_PUSH_TOKEN so release does it"
else
  # Read the version from the `version` line first, and only fall back to
  # parsing it out of the url. Two reasons, both learned elsewhere: leghorn's
  # copy of this check hardcoded one url shape and reported a stale tap on every
  # release for weeks; and git-roost's own formula now carries the version
  # exactly once on the `version` line, interpolating it into the url (roost
  # shipped .../download/v0.6.0/roost_top-0.5.0.tar.gz because the version was
  # written twice and a bump rewrote only one). An interpolated url has no
  # literal version to parse at all, so parsing it is the fallback, not the
  # primary -- and the interpolation has to be expanded before anything can be
  # fetched from it.
  tap_ver=$(printf '%s\n' "$tap_rb" | sed -n 's/^[[:space:]]*version "\([^"]*\)".*/\1/p' | head -1)
  tap_url=$(printf '%s\n' "$tap_rb" | sed -n 's/^[[:space:]]*url "\([^"]*\)".*/\1/p' | head -1)
  if [ -z "$tap_ver" ]; then
    tap_ver=$(printf '%s\n' "$tap_url" | sed -n 's#.*/v\([0-9][^/]*\)\.tar\.gz$#\1#p')
  fi
  tap_url=${tap_url//'#{version}'/$tap_ver}
  if [ "${tap_ver:-}" != "$VERSION" ]; then
    lagging brew "tap formula pins ${tap_ver:-<unparseable>}, want $VERSION -- rerun the release's tap job, or copy packaging/$DIST.rb by hand"
  else
    # The tag is only half the pin. A formula whose sha256 is a stale paste
    # passes every version check and then fails at `brew install` -- the exact
    # failure this script exists to catch early. So hash what the URL actually
    # serves. Needs the network, so it degrades to an explicit skip, never to a
    # silent pass.
    tmp=$(mktemp)
    if curl -sfL --max-time 60 "$tap_url" -o "$tmp" 2>/dev/null; then
      got=$(sha256_of "$tmp")
      want=$(printf '%s\n' "$tap_rb" | sed -n 's/^[[:space:]]*sha256 "\([^"]*\)".*/\1/p' | head -1)
      if [ "$got" = "$want" ]; then
        pass brew "brew install $OWNER/tap/$DIST ($tap_ver, sha256 verified)"
      else
        # Never PENDING: a digest does not propagate. It either is the tarball's
        # or it is wrong.
        fail brew "tap sha256 $want does not match the tarball at $tap_url ($got)"
      fi
    else
      # Not a pass and not a failure: the formula points at an sdist release
      # asset, and no release carrying one exists yet. Say "unverified" out
      # loud -- reporting it as a pass is how an unmatched digest ships.
      skip brew "tap pins $tap_ver but $tap_url could not be fetched -- sha256 UNVERIFIED (no such release asset yet, or offline)"
    fi
    rm -f "$tmp"

    # And does the master copy still agree with what the tap serves? If it does
    # not, the next release quietly overwrites the tap with the older pin.
    if [ -f "$ROOT/packaging/$DIST.rb" ]; then
      own_ver=$(sed -n 's/^[[:space:]]*version "\([^"]*\)".*/\1/p' \
                "$ROOT/packaging/$DIST.rb" | head -1)
      if [ "${own_ver:-}" != "$VERSION" ]; then
        fail brew "packaging/$DIST.rb (the master copy) pins ${own_ver:-<unparseable>}, want $VERSION"
      fi
    fi
  fi
fi

# --- winget ------------------------------------------------------------------
# Unlike every channel above, there is no single URL to ask "what version do
# you serve" -- winget-pkgs is a manifest tree in a repo this project does not
# own, and there is no registry API, only the repo's own directory layout:
# manifests/<first-letter-of-publisher>/<Publisher>/<Package>/<version>/. So
# this checks for the directory itself via the GitHub API (a GET, same as
# every other check here) rather than fetching a file whose exact name would
# have to be guessed.
winget_prefix=$(printf '%s' "$WINGET_ID" | cut -c1 | tr '[:upper:]' '[:lower:]')
winget_path="manifests/$winget_prefix/$OWNER/git-roost"
winget_versions=$(gh api "repos/$WINGET_PKGS/contents/$winget_path" \
                     --jq '.[].name' 2>/dev/null)
if [ -z "$winget_versions" ]; then
  todo winget "no $WINGET_ID in $WINGET_PKGS yet -- run 'wingetcreate new' by hand once (see the winget job's comment in release.yml for the exact fields), then WINGET_PAT takes over for every version after"
else
  winget_ver=$(printf '%s\n' "$winget_versions" | sort -V | tail -1)
  if [ "$winget_ver" = "$VERSION" ]; then
    pass winget "winget install $WINGET_ID ($winget_ver)"
  else
    lagging winget "winget-pkgs has $winget_ver, want $VERSION -- rerun the release's winget job, or check the PR it opened against $WINGET_PKGS"
  fi
fi

# --- repo secrets the automation depends on ----------------------------------
# Listing secrets needs admin, and GITHUB_TOKEN does not have it -- in CI this
# can only ever report a false negative, which is how leghorn's first scheduled
# run went red while every channel was live. A missing secret already announces
# itself as a failed publish job, so CI skips this block and the local run keeps
# it.
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  skip secret "needs admin to list; run this locally to check secrets"
else
  # PyPI and npm need no secret at all (trusted publishing), so this list is
  # exactly the credentials whose absence make a publish job fail alone.
  secrets=$(gh secret list --repo "$REPO" 2>/dev/null)
  if printf '%s\n' "$secrets" | grep -q '^TAP_PUSH_TOKEN'; then
    pass secret "TAP_PUSH_TOKEN"
  else
    todo secret "TAP_PUSH_TOKEN not set -- gh secret set TAP_PUSH_TOKEN --repo $REPO (needed once release automation pushes to the tap)"
  fi
  if printf '%s\n' "$secrets" | grep -q '^WINGET_PAT'; then
    pass secret "WINGET_PAT"
  else
    todo secret "WINGET_PAT not set -- gh secret set WINGET_PAT --repo $REPO (a classic PAT with public_repo scope; needed once the first manifest exists in $WINGET_PKGS)"
  fi
fi

# --- verdict -----------------------------------------------------------------
echo
printf 'pending %d  todo %d  fail %d\n' "$pendings" "$todos" "$fails"
if [ "$fails" -ne 0 ]; then
  echo
  echo "FAIL means a published channel disagrees with $VERSION and propagation is"
  echo "not the explanation. Rerun the failed release jobs with:"
  echo "  gh run rerun \$(gh run list --repo $REPO --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId') --failed --repo $REPO"
  exit 1
fi
if [ "$todos" -ne 0 ]; then
  echo "TODO items are one-time setups nobody has done yet, not breakage."
fi
echo "no channel disagrees with $VERSION"
