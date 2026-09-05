# Homebrew formula for git-roost.
#
# This is the master copy; it is consumed by copying it to Formula/git-roost.rb
# in the tap repo (gmhoward9289-ops/homebrew-tap), which is what `brew install`
# reads. It lives here so the formula is versioned alongside the code it builds.
#
# homebrew-core is not an option yet -- it requires notability thresholds
# (stars/forks/watchers) that this project has not met.
#
# The url points at the sdist tarball uploaded to the GitHub Release, not
# GitHub's auto-generated archive/refs/tags/ URL. That URL isn't a release
# asset at all, so GitHub doesn't count `brew install` downloads in the repo's
# release download_count the way it does for the .deb/.whl assets -- roost's
# Homebrew traffic was invisible to any download tracking until this changed
# there (see roost's packaging/roost.rb), and leghorn followed. The secondary
# benefit is that the tarball Homebrew builds is byte-identical to the one PyPI
# serves, rather than a separately-generated archive of the same tag.
#
# The filename is git_roost-<version>.tar.gz, with an underscore, even though
# the distribution is named git-roost: PEP 625 makes hatchling normalise the
# name in the sdist filename. Do not "correct" it to a hyphen -- that URL 404s.
#
# The `x-release-please-version` marker on the `version` line is load-bearing:
# release-please's generic updater rewrites a version only inside such an
# annotation, so without it this file would be listed in the release config and
# silently left on the previous release. The homebrew-tap job in the release
# workflow then rewrites the `version` and `sha256` lines with computed
# literals, resolves the url's interpolation, and asserts the result matches the
# asset it just uploaded -- so both lines must keep their `  version "..."` /
# `  sha256 "..."` shape.
#
# The version appears in this file EXACTLY ONCE, on the `version` line below,
# and the url interpolates it for both the tag and the filename. roost learned
# this the hard way: with the version written twice on the url line, a bump
# rewrote only the first occurrence and shipped a url whose tag said the new
# release while its filename still said the old one. One occurrence means
# there is nothing to half-update -- and because the same rot can hide in a
# comment, check-version-consistency.sh rejects any version literal anywhere
# in this file other than the one on the `version` line. That is why the curl
# hint below is written versionless.
#
# After tagging a release, refresh the checksum with (versionless on purpose --
# fill in the tag you just cut):
#   curl -sL https://github.com/gmhoward9289-ops/git-roost/releases/download/v<version>/git_roost-<version>.tar.gz | shasum -a 256
#
# Both the version above and this checksum are verified by
# packaging/check-version-consistency.sh, which fetches the sdist and compares
# digests. That check exists because a stale formula cannot report itself:
# `version`, url and sha256 all move together, so a formula left on an old
# release checks the old version against the old tarball, agrees with itself,
# and passes -- while `brew install` quietly ships the previous release.
class GitRoost < Formula
  include Language::Python::Shebang

  desc "top for git: every repo and worktree on the box, most actionable first"
  homepage "https://github.com/gmhoward9289-ops/git-roost"
  version "0.10.0" # x-release-please-version
  url "https://github.com/gmhoward9289-ops/git-roost/releases/download/v#{version}/git_roost-#{version}.tar.gz"
  # STALE ON PURPOSE, AND KNOWN WRONG FOR THE URL ABOVE. This digest is the one
  # for the old archive/refs/tags/ tarball; no release carrying an sdist asset
  # exists yet, so there is nothing to hash. A digest cannot be invented, and a
  # placeholder string would fail the shape gate in
  # check-version-consistency.sh with a message about a malformed hash rather
  # than about the missing release -- so the last real hex stays here as a
  # well-formed stand-in. The release workflow must recompute this against the
  # sdist asset on the next tag, before the tap-push job runs; the checker
  # reports the unverifiable state explicitly rather than passing it silently.
  sha256 "0ad6b09bcb645b60a24684613f0733b05b336aefb2fee33ec6ca32e7449f521c"
  license "Apache-2.0"

  depends_on "python@3.13"
  # git is a runtime dependency in a way it is not for roost: with no git on
  # PATH every column is empty and the tool has nothing to say. Homebrew does
  # not install it (macOS ships one, and Linuxbrew users have their own), so
  # this is declared rather than depended on -- see the caveat below.

  def install
    # Installed as `git-roost`, without the .py suffix: the shebang and the
    # executable bit are what make it a command. This also makes git resolve
    # `git roost` to it, since git looks for a `git-<x>` on PATH.
    bin.install "git_roost.py" => "git-roost"
    # The shipped shebang is `/usr/bin/env python3`, which would resolve to
    # whatever python happens to be first on PATH -- including a virtualenv the
    # user activated for something else. Pin it to the formula's interpreter.
    rewrite_shebang detected_python_shebang(use_python_from_path: false), bin/"git-roost"
    man1.install "git-roost.1"
  end

  def caveats
    <<~EOS
      git-roost reads local git state and needs `git` on PATH.

      After install, run `git-roost`. It opens the TUI and scans the current
      directory. Override with --root DIR or GIT_ROOST_ROOT. A missing root is
      an error. Use --once for a one-shot table.

      It is read-only by construction: every git invocation is checked against an
      allowlist of plumbing that cannot mutate a tree, an index, or a ref.
    EOS
  end

  test do
    assert_match "git-roost #{version}", shell_output("#{bin}/git-roost --version")
    # --once renders a frame and exits. Point it at testpath, which is empty, so
    # the test does not depend on the build machine having any repos -- it still
    # has to produce the empty-state line rather than fail or print nothing.
    # Matching that line specifically, because a loose pattern here would pass
    # on almost any output including an error message.
    assert_match(/no git repository/i,
                 shell_output("#{bin}/git-roost --once --no-color --root #{testpath}"))
  end
end
