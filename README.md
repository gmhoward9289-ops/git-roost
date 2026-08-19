# git-roost

[![ci](https://github.com/gmhoward9289-ops/git-roost/actions/workflows/ci.yml/badge.svg)](https://github.com/gmhoward9289-ops/git-roost/actions/workflows/ci.yml)
[![pypi](https://img.shields.io/pypi/v/git-roost)](https://pypi.org/project/git-roost/)
[![npm](https://img.shields.io/npm/v/git-roost)](https://www.npmjs.com/package/git-roost)
[![winget](https://img.shields.io/winget/v/gmhoward9289-ops.git-roost)](https://github.com/microsoft/winget-pkgs/tree/master/manifests/g/gmhoward9289-ops/git-roost)
[![discussions](https://img.shields.io/github/discussions/gmhoward9289-ops/git-roost)](https://github.com/gmhoward9289-ops/git-roost/discussions)
[![license](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

`top` for git — every repo and worktree on the box, in one table, most
actionable first.

[roost](https://github.com/gmhoward9289-ops/roost) answers *what are my Claude
sessions doing*. `git-roost` answers the other half: *what is actually in the
trees they are working in*. Sessions report intent; git reports what happened,
and only one of those two can be wrong.

One file, no dependencies, Python 3.9+, macOS/Linux/Windows.

![git-roost's table re-sorting as a synthetic fleet drifts, goes uncommitted, and gets pushed](demo/git-roost-demo.gif)

The short ambient loop below is the same program, watching quietly:

![git-roost's watch mode, idling until a tree goes dirty and climbs out of QUIET](demo/git-roost-loop.gif)

Recorded against a synthetic fixture, not a real machine — see
[`demo/`](demo/) for how. Text fallback, same shape:

```
  REPO                    TREE                      BRANCH                       WORK   DRIFT  STASH  LAST  SUBJECT
MID-OPERATION
  copilot-money-mcp       fork-merge-and-repack     fork/merge-and-repack        17     ^1            1h    ** merge in progress, 3 conflict(s) **
DIVERGED
  counting-chicken-wings  docs-roadmap-refresh      docs/roadmap-refresh         clean  ^1v1   1      1h    Re-verify the roadmap against the tree
  llm-security-rules      pipeline-e2e              pipeline-e2e                 clean  ^4v8          3h    Add launch post draft
UNCOMMITTED
  llm-security-rules      fix-action-injection      fix/action-injection         2+1?   =             1h    fix: script injection via unquoted input
UNPUSHED
  copilot-money-mcp       fork-merge-and-repack     fork/merge-and-repack        clean  ^1            1h    test: registry-derived tool-count check
BEHIND
  roost                   (primary)                 main                         clean  v2            1h    Bump version to 0.3 (#3)
ACTIVE
  shunt-ai-power          (primary)                 main                         clean  =             3m    docs+fix: wall-power wording

QUIET (8)  blog/chicken-fest . copilot-money-mcp/(primary) . repo-security-ci/(primary) . ...

27 tree(s) across 11 repo(s)  |  2 with uncommitted work  |  1 mid-operation
```

## Install

Questions, ideas, or your own fleet's screenshot go in
[Discussions](https://github.com/gmhoward9289-ops/git-roost/discussions) — bugs go in Issues.

The distribution, the command, the module and the repo are all the bare
`git-roost` — pick whichever channel is already on the box.

```bash
pipx install git-roost          # or: pip install --user git-roost
npm install -g git-roost        # if Node is what you have
brew install gmhoward9289-ops/tap/git-roost
winget install gmhoward9289-ops.git-roost   # Windows; still needs Python on PATH
```

Or just take the file. It is one script with no dependencies, so `curl` and
`chmod +x` is a complete install:

```bash
curl -fsSLO https://raw.githubusercontent.com/gmhoward9289-ops/git-roost/main/git_roost.py
chmod +x git_roost.py && ./git_roost.py
```

Installed under any of those names, `git roost` works too: git dispatches an
unknown subcommand to a `git-<name>` on PATH.

The man page installs to `<prefix>/share/man/man1`. A system or Homebrew install
puts that on the default MANPATH; a venv or pipx install does not, so `man
git-roost` there needs `MANPATH` help.

## Why

`lazygit`, `gitui` and `tig` are all excellent and all single-repo. They answer
"what is happening in this repo". They do not answer "which of my thirty trees
is diverged, which has uncommitted work nobody has looked at, and which one is
stuck half way through a rebase" — which is the question you have the moment
more than one thing is working in parallel.

## Usage

```bash
git-roost                # one table, most actionable first
git-roost -1             # render once and exit (the default; --once)
git-roost -w             # redraw every 3s (the top view)
git-roost --log          # commit feed across every repo, newest first
git-roost --all          # expand the QUIET group
git-roost --json         # records, for piping somewhere else
git-roost --root ~/src   # look somewhere other than ~/GitHub (repeatable)
git-roost --repo wings --sort work --filter dirty   # scope, sort and filter together
git-roost --check                      # no table -- exit 1 if any tree needs a human first
git-roost --fail-on stuck              # like --check, but keeps the normal table
git-roost --github                     # add a PR/CI column, via `gh` (opt-in: network calls)
```

Watch mode takes keys:

| Key | Action |
|---|---|
| `?` | the keymap |
| `r` | refresh now |
| `s` | sort: recent / repo / work |
| `f` | filter: all / uncommitted / mid-operation |
| `a` | expand or collapse `QUIET` |
| `l` | toggle the fleet table and the commit feed, without restarting |
| `j` / `k` | move the row cursor down / up |
| `enter` | open a detail view for the highlighted tree |
| `q` | quit |

Sort cycles *within* a group and never across one. The group order is the whole
argument this tool makes — cost of ignoring, not recency or size — so a sort
that let an `ACTIVE` tree float above a `MID-OPERATION` one would be quietly
answering a different question. Changing sort, filter or the quiet toggle
resets the row cursor rather than leaving it pointing at whatever row happens
to land underneath it.

`l` used to be a restart: `--log` decided table-or-feed once, at launch. Now
it just seeds the initial view — `git-roost --log -w` opens on the feed, and
`git-roost -w` opens on the table — and `l` flips between the two live,
without losing the scan already in flight.

`j`/`k` move a highlighted row through whatever the table is currently
showing — same sort, same filter, same QUIET collapse everyone else sees.
`enter` opens a detail screen for that tree: its whole stash list rather than
just a count, a diffstat of the most recent stash, what it's stuck doing if
anything, and its last five commits. Any other key returns to the table, the
same way dismissing the `?` overlay does — one dismissal convention, not two.

Two consecutive frames of an idle fleet used to be indistinguishable from each
other, which is a strange thing for a tool named after `top`. Now a row whose
group, `WORK` or `DRIFT` changed since the last redraw is marked with a
leading `*`, so watching quietly still tells you when something moved.

`--repo NAME` (repeatable, case-insensitive substring), `--sort` and `--filter`
put the `f`/`s` keys' view on the command line, so `--json` and one-shot
renders can be scoped without a terminal — `git-roost --repo wings --filter
dirty --json` is one repo's uncommitted trees, nothing else. Both also seed
`-w`'s starting view, so `-w --sort work --filter dirty` opens watch mode
already positioned there and the keys still cycle from it.

Keys need a terminal. Piped, redirected, or on a box with neither `termios` nor
`msvcrt`, watch mode degrades to the plain timer redraw rather than failing, and
the default one-shot render touches no terminal settings at all — which is what
keeps `git-roost | less` and `git-roost --json | jq` safe.

Two exit-code flags, for two different hook shapes:

- **`--check`** is a different shape entirely from the table: no `--filter`,
  just pass/fail. `0` means every tree is at worst `UNPUSHED` or `BEHIND`; `1`
  means at least one is `MID-OPERATION`, `DIVERGED`, or has `UNCOMMITTED` work
  — and those offending trees print (or `--json` them), so the caller knows
  which, without reading a full table. `--root`/`--repo` still scope the scan.

  ```bash
  git-roost --repo counting-chicken-wings --check || echo "not clean, look first"
  ```

- **`--fail-on {stuck,diverged,dirty}`** is `--check` with the threshold made
  a choice, and without replacing the table: it prints the normal render (or
  the normal `--json`) and only changes the exit code, checked against the
  *whole fleet* (never the `--filter` view — a hook asking "is anything
  stuck" should not get a false 0 just because a human also filtered the
  table they're looking at). `stuck` is mid-operation only; `diverged` adds
  ahead-and-behind; `dirty` (equivalent to `--check`'s fixed threshold) adds
  uncommitted work on top of that.

  ```bash
  git-roost -w --fail-on stuck   # a human's normal view, that also exits 1
  ```

### `--github`

Opt-in, not on by default. The local scan is disk-only and fast; PR and CI
state is a network call to GitHub through the `gh` CLI, with a meaningfully
different latency and trust profile, so it never runs unless asked.

```bash
git-roost --github                     # one-shot table with a PR column
git-roost -w --github                  # watch mode; PR/CI refreshed every 30s
git-roost -w --github --github-interval 10   # refresh PR/CI more often
git-roost --json --github              # pr_number/pr_state/pr_draft/pr_review/pr_ci per tree
```

Each tree's current branch is looked up with a single `gh pr view` call, run
from inside that tree so `gh` resolves "the PR for this branch" itself. It
degrades the same way `git()` does: no `gh` on `PATH`, no auth, no GitHub
remote, no open PR, a rate limit, or a slow network are all just "we don't
know" for that one tree — a blank `PR` column and, in `--json`, `null` values
rather than a missing key or a crashed scan. The calls go through `gh_call()`,
a wrapper as careful as `git()`'s: an allowlist keyed on `gh`'s subcommand
(`pr view` / `pr list` / `pr status` only — nothing that could merge, close,
edit, or comment), checked before the subprocess ever runs.

In watch mode, PR/CI data is cached and refreshed on its own interval
(`--github-interval`, default 30s) rather than on every redraw (default 3s):
the local git scan is cheap enough to run every frame, but `gh` is a
rate-limited network call and a PR's review state rarely changes inside a 3s
window.

`--json` only gains the five `pr_*` keys when `--github` was passed — the
default JSON shape is unchanged for any consumer that never asks for GitHub
data.

## Reading the table

Groups are ordered by what it costs to ignore them, not by how interesting they
look.

| Group | Meaning |
|---|---|
| `MID-OPERATION` | Stuck part-way through a rebase, merge, cherry-pick, revert or bisect. This is the one that most needs a human, and it looks identical to an idle tree in every other column. |
| `DIVERGED` | Ahead *and* behind. Someone is going to resolve a conflict later. |
| `UNCOMMITTED` | Tracked changes sitting unsaved. |
| `UNPUSHED` | Commits that exist only on this machine. |
| `BEHIND` | Someone else moved on without you. |
| `ACTIVE` | Committed within the hour, otherwise clean. |
| `QUIET` | Collapsed to one line. `--all` expands it. |

Columns:

- **WORK** — `clean`, `3` tracked changes, `+2?` untracked, `3+2?` both.
  Untracked files get a marker but never a group of their own: they are mostly
  scratch output, and one tree here carries a dozen permanently.
- **DRIFT** — `=` in sync, `^2` ahead, `v3` behind, `^2v3` diverged, `-` unknown.
  `-` means *unknown*, never *in sync*.
- **STASH** — stash entries, blank when there are none.
- **LAST** — age of the last commit.
- **PR** — only with `--github`. `#123` open, `#123+` CI green, `#123x` CI red,
  `#123~` CI still running, `#123 draft`, blank when there is no open PR.

### Finding the baseline

Drift needs something to measure against. The chain is:

1. the branch's own upstream, when it has one;
2. `origin/HEAD`;
3. if the repo has exactly **one** remote, that remote's `HEAD`, then its
   `main` or `master`;
4. otherwise `-`.

Steps 1 and 2 are the ones that matter most and are also the easiest to get
wrong in opposite directions. Branches that are never pushed have no upstream,
so an upstream-only check calls every one of them clean. And `origin` is a
convention, not a guarantee — one repo here has a single remote named `deploy`,
and an origin-only lookup filed a tree that was nine commits behind under QUIET.

Step 3 stops at a single remote on purpose. With two remotes there is no way to
know which is authoritative, and a confident wrong baseline is worse than `-`.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `GIT_ROOST_ROOT` | `~/GitHub` | where to look for repos, `os.pathsep`-separated for more than one (same convention as `PATH`) — `--root` overrides it for one call |
| `GIT_ROOST_TIMEOUT` | `5` | seconds any single git call may take before it is abandoned |
| `GIT_ROOST_WORKERS` | `12` | how many trees are scanned in parallel |
| `GIT_ROOST_GH_TIMEOUT` | `8` | seconds any single `gh` call may take before it is abandoned (only with `--github`) |
| `GIT_ROOST_GH_WORKERS` | `4` | how many `gh` calls run in parallel (only with `--github`; its own, smaller pool so a slow `gh` never starves the git scan) |

No dotfile or config file, deliberately — every setting here is an env var or a
flag, matching roost, leghorn and legbar. The timeout and worker count are both
about how hard to push a slow disk rather than about git. The `GH` pair is
separate on purpose: `gh` is a network call with its own, longer timeout and
its own, smaller worker cap, and both are irrelevant unless `--github` is
passed.

The timeout is a ceiling on one call, not on the scan. A tree behind a stalled
network mount is dropped rather than allowed to hold the whole table hostage —
in a watch loop, one unreachable repo would otherwise stop every other repo from
redrawing.

## Read-only by construction

Every git invocation goes through one function that refuses anything outside an
allowlist of read-only plumbing. It cannot mutate a tree, an index or a ref —
not because it happens not to, but because the call raises.

The allowlist is keyed on `(subcommand, first argument)`, not the subcommand
alone, because the subcommand alone does not settle it: `stash list` reports but
`stash pop` mutates and `stash clear` destroys; `config --get` reads but
`config <key> <value>` writes; `symbolic-ref --short REF` reads but
`symbolic-ref HEAD REF` rewrites `HEAD`. A subcommand-level allowlist admits all
of those writes, and an earlier version of this file did.

The test suite asserts the policy directly, asserts each of those dangerous
forms is refused, and asserts that reading a repo leaves its status and `HEAD`
byte-identical.

This matters because the tool is meant to sit in a watch loop over every repo on
the machine, unattended.

## Performance

A full scan is parallel across trees, and repo-level facts (stashes, remote
refs) are computed once per repo rather than once per worktree.

Measured on 27 trees across 11 repos: **~0.68s** per redraw, comfortably inside
the default 3s watch interval. Tunable with `GIT_ROOST_WORKERS` and
`GIT_ROOST_TIMEOUT`.

## The family

Four tools, one shape: single file, no dependencies, stdlib `unittest`, and a
table ordered by what it costs to ignore.

- **[roost](https://github.com/gmhoward9289-ops/roost)** — `top` for Claude
  Code: per-session context burn, models, and the subagents a session spawned.
- **git-roost** — this one. The other half of the same question: not what the
  sessions say they are doing, but what their trees actually contain.
- **[leghorn](https://github.com/gmhoward9289-ops/leghorn)** — sessions joined
  to worktrees and real git state, CI, and a commit feed.
- **[legbar](https://github.com/gmhoward9289-ops/legbar)** — both lanes on one
  screen, over one discovery layer.

`git-roost` is the one that needs no Claude Code, no sessions and no
`~/.claude` at all. It reads git and nothing else, which is why it is the one
worth running on a box that has never seen an agent.

## Tests

```bash
python -m unittest discover -s tests
```

Stdlib `unittest`, so it runs with nothing installed; pytest collects it
unchanged.

## License

Apache-2.0
