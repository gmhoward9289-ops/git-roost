# Changelog

All notable changes to `git-roost` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Homebrew formula for v0.1, in
  [gmhoward9289-ops/tap](https://github.com/gmhoward9289-ops/homebrew-tap).

## [0.1] - 2026-07-31

Initial release.

### Added

- **`top` for git** — every repo and worktree on the machine in one table, most
  actionable first. Single file, stdlib only, Python 3.9+, macOS/Linux/Windows.
  Where [roost](https://github.com/gmhoward9289-ops/roost) answers *what are my
  Claude sessions doing*, `git-roost` answers *what is actually in the trees they
  are working in* — sessions report intent, git reports what happened, and only
  one of the two can be wrong.
- **Groups ordered by the cost of ignoring them**, not by size. `MID-OPERATION`
  comes first because a tree halted mid-rebase is indistinguishable from an idle
  one in every other column.
- **Drift compares against the branch upstream, falling back to `origin/HEAD`**
  when there is none. The fallback is load-bearing rather than cosmetic:
  `ccwork` branches are never pushed, so an upstream-only comparison reports
  every one of them as clean.
- **`--json` records** for downstream consumers, with staged and unstaged counts
  reported separately rather than summed. `parse_porcelain_v2` already sees the
  `XY` pair, and collapsing it discarded the index/worktree distinction that
  consumers render as `+N~M`. A partially staged file counts on both sides,
  because both are true.
- **Packaging and CI** — `pyproject` (hatchling), `.deb`, and a man page, with
  the version single-sourced from `__version__`. The distribution name is the
  bare `git-roost`, so unlike `roost` — which had to publish as `roost-top` —
  the dist, command, module and package names all agree.

### Security

Both of the following were guarantees the tool advertised and did not actually
hold. They were found by review before any tagged release, and are recorded here
because `--json` output and the read-only guarantee are the two things a
consumer is entitled to rely on.

- **Read-only enforcement is keyed on the full argument shape, not the
  subcommand.** Gating on the subcommand alone let three mutating forms through:
  `stash list` reports but `stash pop` mutates the working tree and
  `stash clear` destroys data; `config --get` reads but `config <key> <value>`
  writes `.git/config`; and `symbolic-ref --short REF` reads but
  `symbolic-ref HEAD REF` rewrites `HEAD`. `check_read_only()` is now split out
  so tests assert the policy directly, rather than inferring it from side
  effects they hope did not happen.
- **Positional arguments are capped**, closing a hole in the fix above: a
  leading safe flag laundered a write, because `symbolic-ref --short HEAD
  refs/heads/other` opens with an accepted `--short` and then rewrites `HEAD`.
  It leaves no working-tree or index change at all, so nothing downstream would
  have noticed. The read form takes one positional, the write form takes two,
  and the count is now enforced.
- **`--json` record keys are pinned** with an exact-set assertion. Adding a key
  is safe; a rename or removal would leave a subset check green while breaking
  consumers at runtime.

[Unreleased]: https://github.com/gmhoward9289-ops/git-roost/compare/v0.1...HEAD
[0.1]: https://github.com/gmhoward9289-ops/git-roost/releases/tag/v0.1
