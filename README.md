# git-roost

`top` for git — every repo and worktree on the box, in one table, most
actionable first.

[roost](https://github.com/gmhoward9289-ops/roost) answers *what are my Claude
sessions doing*. `git-roost` answers the other half: *what is actually in the
trees they are working in*. Sessions report intent; git reports what happened,
and only one of those two can be wrong.

One file, no dependencies, Python 3.9+, macOS/Linux/Windows.

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
```

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

## Tests

```bash
python -m unittest discover -s tests
```

Stdlib `unittest`, so it runs with nothing installed; pytest collects it
unchanged.

## License

MIT
