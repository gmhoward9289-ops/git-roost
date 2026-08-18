# Homebrew formula for git-roost.
#
# This is the master copy; it is consumed by copying it to Formula/git-roost.rb
# in the tap repo (gmhoward9289-ops/homebrew-tap), which is what `brew install`
# reads. It lives here so the formula is versioned alongside the code it builds.
#
# homebrew-core is not an option yet -- it requires notability thresholds
# (stars/forks/watchers) that this project has not met.
#
# After tagging a release, refresh the checksum with:
#   curl -sL https://github.com/gmhoward9289-ops/git-roost/archive/refs/tags/v0.1.tar.gz | shasum -a 256
#
# Both the tag above and this checksum are verified by
# packaging/check-version-consistency.sh, which fetches the tarball and compares
# digests. That check exists because a stale formula cannot report itself:
# Homebrew derives `version` from this URL, so a formula left on an old tag
# checks the old version against the old tarball, agrees with itself, and passes
# -- while `brew install` quietly ships the previous release.
class GitRoost < Formula
  include Language::Python::Shebang

  desc "top for git: every repo and worktree on the box, most actionable first"
  homepage "https://github.com/gmhoward9289-ops/git-roost"
  url "https://github.com/gmhoward9289-ops/git-roost/archive/refs/tags/v0.1.tar.gz"
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
