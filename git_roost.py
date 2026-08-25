#!/usr/bin/env python3
"""git-roost -- top for git, across every repo and worktree on the box.

roost answers "what are my Claude sessions doing". git-roost answers the other
half: what the trees they are working in actually contain. Sessions report what
they intend to do; git reports what happened, and only one of those two can be
wrong.

Single-repo TUIs already exist and are good -- lazygit, gitui, tig. None of them
answer a question about thirty trees at once, which is exactly the question you
have when a dozen agents are working in parallel: who is diverged, who has
uncommitted work nobody has looked at, who is stuck mid-rebase.

    git-roost                # TUI; current directory is the scan root
    git-roost --root ~/dev   # scan a specific tree of checkouts
    git-roost -1             # render once and exit
    git-roost --log          # commit feed across every repo, newest first
    git-roost --all          # expand the QUIET group
    git-roost --json         # records, for piping somewhere else
    git-roost --repo wings --filter dirty   # scope to one repo, one view
    git-roost --check        # exit 1 if anything needs a human first; for hooks
    git-roost --github       # add a PR/CI column, via `gh` (opt-in, network)

Watch mode (the default on a TTY) takes keys -- `?` for the map, `r` refresh,
`s` sort, `f` filter, `a` quiet, `l` toggles the table for the commit feed,
`j`/`k` move a row cursor, `enter` opens a detail view for the highlighted
tree, `q` quit. `--once` / pipes / `--json` take no keys and touch no terminal
settings, which is what keeps them safe to script.

One file, no dependencies, Python 3.9+, macOS/Linux/Windows -- the same
constraints as roost, for the same reason: it has to run on whatever Python is
already on the box, including a bare system 3.9 on macOS.

Read-only by construction. Every git invocation goes through git(), which
refuses anything outside READ_ONLY -- so the tool cannot mutate a tree, an index
or a ref even if a future edit tries to. The allowlist is keyed on the
subcommand *and* its first argument, because `stash list` and `stash pop` are
not the same kind of thing. tests/test_git_roost.py asserts that policy directly.

`--github` is a second, separate opt-in: it shells out to `gh` for PR and CI
state, which is a network call with its own latency and trust profile, so it
never runs unless asked. Those calls go through gh_call(), a read-only wrapper
in the same spirit as git() but keyed to gh's own danger surface -- see the
"github (gh)" section below.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Raw single-key reads in watch mode. curses would have been the obvious choice
# -- leghorn and legbar both use it -- but curses is not in the Windows stdlib,
# and git-roost claims Windows in its classifiers. So the two halves are
# imported conditionally and the watch loop uses whichever it got; a box with
# neither simply keeps the old timer-only redraw.
try:
    import msvcrt  # Windows
except ImportError:
    msvcrt = None
try:
    import select
    import termios
    import tty  # POSIX
except ImportError:
    termios = None

# release-please rewrites the line between these markers. It only touches a
# version inside an annotation, so without them the release PR bumps
# package.json, leaves this one behind, and goes green -- the exact silent
# drift the version-consistency check exists to catch after the fact.
#
# Three components, not two: release-please parses strict semver and throws
# on "0.1", and npm rejects it outright. The historical v0.1 tag stands.
# x-release-please-start-version
__version__ = "0.6.0"
# x-release-please-end

# How deep to look for a repo below a root. Worktrees live at
# <root>/.worktrees/<repo>/<slug>, which is depth 3, so 3 is the floor and the
# default. Deeper costs a directory walk and finds mostly vendored junk.
DEFAULT_DEPTH = 3

# Watch-mode redraw interval when a bare `git-roost` opens the TUI.
DEFAULT_WATCH = 3.0

# Never descend into these. A node_modules with its own .git is not a repo you
# are working in, and walking one costs more than the whole rest of the scan.
PRUNE = frozenset((
    "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "build", "dist", "target", ".next", ".cargo", "Library",
    ".Trash", "vendor", ".terraform",
    "AppData", "Application Data",
))

# Worktrees can live inside the repo as well as beside it: ccwork puts them at
# <root>/.worktrees/, but Claude Code's own put them at <repo>/.claude/worktrees/.
# Finding a repo prunes the walk, so these two have to be descended explicitly or
# the second kind is invisible.
NESTED_WORKTREE_DIRS = (".worktrees", Path(".claude") / "worktrees")

GIT_TIMEOUT = float(os.environ.get("GIT_ROOST_TIMEOUT") or 5)
GIT_WORKERS = int(os.environ.get("GIT_ROOST_WORKERS") or 12)

# gh hits the network, so it gets its own, longer timeout and its own, smaller
# worker cap -- same GIT_ROOST_* naming convention as the two above. A slow or
# rate-limited `gh` must never be allowed to starve the local git scan, which
# is why this is a separate pool rather than sharing GIT_WORKERS.
GH_TIMEOUT = float(os.environ.get("GIT_ROOST_GH_TIMEOUT") or 8)
GH_WORKERS = int(os.environ.get("GIT_ROOST_GH_WORKERS") or 4)

# Resolved once at import, not on every call: `gh` either exists on this box or
# it doesn't, and --github degrades to a blank column / null JSON keys when it
# is missing rather than erroring partway through a scan.
GH_PATH = shutil.which("gh")

# A commit this recent means someone is working in the tree right now.
ACTIVE_SECS = 3600

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

COLOR = False


def c(text, *codes):
    if not COLOR or not codes:
        return text
    return "".join(codes) + text + RESET


def ascii_safe(s):
    """Drop characters the console cannot render.

    Commit subjects are free-form prose and routinely carry em dashes and smart
    quotes; the Windows console codepage turns those into replacement blobs
    mid-table.
    """
    if not s:
        return ""
    return "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in s)


def enable_windows_ansi():
    """Turn on VT processing so escapes render instead of printing literally."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        k = ctypes.windll.kernel32
        handle = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_vt = 0x0004
        if mode.value & enable_vt:
            return True
        return bool(k.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:
        return False


def dur(secs):
    """Age, at one significant unit. Same vocabulary as roost's."""
    if secs is None:
        return "-"
    secs = int(secs)
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm" % (secs // 60)
    if secs < 86400:
        return "%dh" % (secs // 3600)
    return "%dd" % (secs // 86400)


# ---------------------------------------------------------------- git plumbing

# What this tool is allowed to run: subcommand -> None, or a rule pairing the
# permitted first argument with the most positional arguments a read-only form
# of that subcommand can take. None means the subcommand cannot write whatever
# its arguments.
#
# Checking the subcommand alone is not enough: `stash list` reports but `stash
# pop` mutates the working tree and `stash clear` destroys data; `config --get`
# reads but `config user.email x` writes .git/config.
#
# Checking the first argument alone is *also* not enough, which is why the
# positional cap exists. A leading flag does not make the rest of the line safe:
#
#     git symbolic-ref --short HEAD refs/heads/other
#
# passes a first-argument check on "--short" and rewrites HEAD -- verified doing
# exactly that. The read form takes one positional (the ref to resolve); the
# write form takes two (the ref, and what to point it at). Counting them is what
# separates the two, since git accepts the flag in either. The same shape is why
# `config --local core.hooksPath /tmp` has to be refused: a scope flag shifts the
# key and value one position right, and hooksPath is per-repo, not per-worktree,
# so it would reach every worktree and every session in them.
#
# "" is the entry for a bare invocation with no arguments at all.
READ_ONLY = {
    "rev-parse": None,
    "rev-list": None,
    "log": None,
    "status": None,
    "stash": (frozenset(("list", "show")), 2),
    "config": (frozenset(("--get", "--get-all", "--list")), 1),
    "symbolic-ref": (frozenset(("--short",)), 1),
    "remote": (frozenset(("",)), 0),
}


class NotReadOnly(RuntimeError):
    """Raised when a caller asks git() for anything that could write."""


def check_read_only(args):
    """Raise unless args is a form that cannot modify a repository.

    Split out from git() so the test suite can assert the policy directly rather
    than by observing side effects it hopes do not happen.
    """
    if not args:
        raise NotReadOnly("git-roost refuses an empty git invocation")
    sub = args[0]
    if sub not in READ_ONLY:
        raise NotReadOnly("git-roost refuses non-read-only subcommand: %r" % sub)
    rule = READ_ONLY[sub]
    if rule is None:
        return
    allowed, max_positional = rule

    first = args[1] if len(args) > 1 else ""
    if first not in allowed:
        raise NotReadOnly(
            "git-roost refuses %r: only %s are read-only"
            % (" ".join(args[:2]), ", ".join(sorted(a or "(no args)" for a in allowed)))
        )

    # Fails closed: an unrecognised flag that takes a value would make its value
    # look positional and push the count over, refusing a call rather than
    # letting an unexamined form through.
    positional = [a for a in args[1:] if not a.startswith("-")]
    if len(positional) > max_positional:
        raise NotReadOnly(
            "git-roost refuses %r: %d positional argument(s), read-only %s takes "
            "at most %d" % (" ".join(args), len(positional), sub, max_positional)
        )


def git(dirpath, *args):
    """One read-only git call. None on any failure -- git state is best-effort.

    A tree can vanish, be mid-rebase, or belong to another user between the scan
    and the call. None of that is worth an exception: the row just shows what it
    could learn and dashes for the rest.
    """
    check_read_only(args)
    kwargs = dict(capture_output=True, text=True, timeout=GIT_TIMEOUT)
    if sys.platform == "win32":
        # Avoid flashing a console per git.exe spawn; on a fleet of ~80 trees
        # that allocation is a measurable slice of the one-shot wait.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(
            ("git", "-C", str(dirpath)) + tuple(args),
            **kwargs,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n")


def is_repo(path):
    """A .git entry -- directory for a normal clone, file for a worktree."""
    return (path / ".git").exists()


def discover(roots, depth=DEFAULT_DEPTH):
    """Every working tree at or below the roots, deduped, in stable order."""
    found = []
    seen = set()

    def add(path):
        key = str(path)
        if key not in seen:
            seen.add(key)
            found.append(path)

    def walk(path, remaining):
        try:
            if not path.is_dir():
                return
        except OSError:
            return
        if is_repo(path):
            add(path)
            # Stop here, except for the two places a worktree hides inside its
            # own repo. Descending a whole repo finds only vendored copies.
            for nested in NESTED_WORKTREE_DIRS:
                sub = path / nested
                if sub.is_dir():
                    for child in sorted_dirs(sub):
                        walk(child, 1)
            return
        if remaining <= 0:
            return
        for child in sorted_dirs(path):
            walk(child, remaining - 1)

    for root in roots:
        walk(Path(root).expanduser(), depth)
    return found


def sorted_dirs(path):
    try:
        entries = sorted(path.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [p for p in entries if p.is_dir() and p.name not in PRUNE]


def operation_in_progress(git_dir):
    """Whatever multi-step git operation this tree is stuck in the middle of.

    This is the single most useful thing on the board: a tree halted mid-rebase
    looks identical to an idle one in every other column, and it is the state
    that most needs a human.
    """
    if not git_dir:
        return ""
    d = Path(git_dir)
    for name, label in (
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
    ):
        if (d / name).exists():
            return label
    return ""


def parse_porcelain_v2(text):
    """Branch, upstream, ahead/behind and file counts out of one status call.

    `status --porcelain=v2 --branch` carries all of it, which is why it is worth
    parsing a slightly awkward format: the obvious alternative is four separate
    git invocations, and at ~30 trees a redraw that is 90 extra process spawns.
    """
    out = {
        "branch": "", "detached": False, "upstream": "",
        "ahead": None, "behind": None,
        "tracked": 0, "untracked": 0, "conflicts": 0,
        "staged": 0, "unstaged": 0,
    }
    if text is None:
        return out
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            head = line[14:].strip()
            if head == "(detached)":
                out["detached"] = True
            else:
                out["branch"] = head
        elif line.startswith("# branch.oid "):
            out["oid"] = line[13:].strip()
        elif line.startswith("# branch.upstream "):
            out["upstream"] = line[18:].strip()
        elif line.startswith("# branch.ab "):
            # "+1 -4" -- signs are decoration, the counts are what matter.
            parts = line[12:].split()
            if len(parts) == 2:
                try:
                    out["ahead"] = abs(int(parts[0]))
                    out["behind"] = abs(int(parts[1]))
                except ValueError:
                    pass
        elif line.startswith("? "):
            out["untracked"] += 1
        elif line.startswith("u "):
            # Unmerged. These are live conflicts, not merely modified files.
            out["conflicts"] += 1
            out["tracked"] += 1
        elif line[:2] in ("1 ", "2 "):
            out["tracked"] += 1
            # The XY pair: X is the index side, Y the working-tree side. One
            # file can count on both -- partially staged is both true things.
            xy = line[2:4]
            if xy[0:1] not in (".", ""):
                out["staged"] += 1
            if xy[1:2] not in (".", ""):
                out["unstaged"] += 1
    return out


# Stashes and remote refs live in the common dir, so every worktree of a repo
# gives the same answer. Asking once per tree instead of once per repo was 16
# redundant subprocess spawns here. Reset per scan -- a cache that outlived a
# redraw would quietly show stale counts in watch mode.
_repo_cache = {}
_repo_lock = threading.Lock()


def repo_facts(path, common_dir):
    """Repo-level facts, computed once per common dir and shared by its trees."""
    key = common_dir or str(path)
    with _repo_lock:
        hit = _repo_cache.get(key)
    if hit is not None:
        return hit

    facts = {"stashes": 0, "base": ""}
    stashes = git(path, "stash", "list")
    if stashes:
        facts["stashes"] = len([ln for ln in stashes.splitlines() if ln])
    facts["base"] = resolve_base(path)

    with _repo_lock:
        _repo_cache.setdefault(key, facts)
    return facts


def resolve_base(path):
    """The remote branch to measure drift against when there is no upstream.

    "origin" is a convention, not a guarantee. ~/GitHub/blog and its worktrees
    have exactly one remote and it is called "deploy": an origin-only lookup
    finds nothing, renders "-", and files a tree that is nine commits behind
    under QUIET. That is the 2026-07-30 false-clean one level down -- not
    "upstream-only" that time, but "origin-only" this time.

    Stops at a single remote deliberately. With two remotes, guessing which one
    is authoritative is worse than admitting we do not know: a confident wrong
    baseline is the failure this whole chain exists to avoid.
    """
    head = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head

    remotes = (git(path, "remote") or "").split()
    if len(remotes) != 1:
        return ""
    remote = remotes[0]

    head = git(path, "symbolic-ref", "--short", "refs/remotes/%s/HEAD" % remote)
    if head:
        return head
    for name in ("main", "master"):
        ref = "%s/%s" % (remote, name)
        if git(path, "rev-parse", "--verify", "--quiet",
               "refs/remotes/%s" % ref) is not None:
            return ref
    return ""


def tree_state(path):
    """Everything one working tree can tell us, in as few calls as possible."""
    paths = git(path, "rev-parse", "--show-toplevel", "--absolute-git-dir",
                "--path-format=absolute", "--git-common-dir")
    lines = paths.splitlines() if paths else []
    if len(lines) >= 3:
        toplevel, git_dir, common = lines[0], lines[1], lines[2]
    else:
        # --path-format landed in git 2.31. Rather than report a readable repo
        # as unreadable on an older git, pay for the extra calls on that path.
        toplevel = git(path, "rev-parse", "--show-toplevel")
        if not toplevel:
            return None
        git_dir = git(path, "rev-parse", "--absolute-git-dir") or ""
        common = git(path, "rev-parse", "--git-common-dir") or git_dir
        if common and not os.path.isabs(common):
            common = os.path.join(toplevel, common)

    # The common dir is shared by a repo and all of its worktrees, so it is the
    # only stable identity for "these trees are the same project". The basename
    # of its parent is the repo name; ".git" itself is not a useful label.
    repo_root = Path(common).parent if common else Path(toplevel)

    status = parse_porcelain_v2(git(path, "status", "--porcelain=v2", "--branch"))
    facts = repo_facts(path, common)

    st = {
        "path": str(path),
        "toplevel": toplevel,
        "common_dir": common,
        "repo": repo_root.name or toplevel,
        "tree": "(primary)" if Path(toplevel) == repo_root else Path(toplevel).name,
        "branch": status["branch"],
        "detached": status["detached"],
        "base": status["upstream"],
        "ahead": status["ahead"],
        "behind": status["behind"],
        "tracked": status["tracked"],
        "staged": status["staged"],
        "unstaged": status["unstaged"],
        "untracked": status["untracked"],
        "conflicts": status["conflicts"],
        "stashes": facts["stashes"],
        "operation": operation_in_progress(git_dir),
        "last_ts": None,
        "last_hash": "",
        "last_author": "",
        "last_subject": "",
    }

    if st["detached"]:
        # The sha is the only honest label, and saying so matters -- a commit
        # made here lands on no branch at all.
        st["branch"] = git(path, "rev-parse", "--short", "HEAD") or "?"

    # status --branch already gave ahead/behind when the branch has an upstream.
    # When it has none, fall back to the repo's default remote branch: ccwork
    # branches are never pushed, so an upstream-only check calls them all clean
    # -- on 2026-07-30 that reported all 8 drifted counting-chicken-wings
    # worktrees as current.
    if not st["base"] and facts["base"]:
        st["base"] = facts["base"]
        counts = git(path, "rev-list", "--left-right", "--count",
                     "HEAD...%s" % st["base"])
        if counts:
            parts = counts.split()
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                st["ahead"], st["behind"] = int(parts[0]), int(parts[1])

    last = git(path, "log", "-1", "--format=%ct%x00%h%x00%an%x00%s")
    if last:
        bits = last.split("\0")
        if len(bits) == 4 and bits[0].isdigit():
            st["last_ts"] = int(bits[0])
            st["last_hash"] = bits[1]
            st["last_author"] = bits[2]
            st["last_subject"] = bits[3]

    return st


SPIN = "|/-\\"


def collect(paths, on_progress=None):
    # Repo-level facts are memoized within a scan and must not survive it: in
    # watch mode a stale cache would keep showing a stash that was just popped.
    #
    # Completion order is not path order: as_completed fires whenever a worker
    # finishes, which is what lets a spinner / partial table paint before the
    # slowest tree. Results are stored by original index so --json stays stable.
    with _repo_lock:
        _repo_cache.clear()
    if not paths:
        return []
    ordered = [None] * len(paths)
    with ThreadPoolExecutor(max_workers=min(GIT_WORKERS, len(paths))) as pool:
        fmap = {pool.submit(tree_state, p): i for i, p in enumerate(paths)}
        finished = 0
        if on_progress:
            on_progress(0, len(paths), ordered)
        for fut in as_completed(fmap):
            ordered[fmap[fut]] = fut.result()
            finished += 1
            if on_progress:
                on_progress(finished, len(paths), ordered)
    return [s for s in ordered if s]


# ------------------------------------------------------------------- grouping

def bucket(st, now=None):
    """Which attention group a tree belongs in, most actionable first.

    Ordered by what it costs to ignore, not by how interesting it looks. A tree
    stuck mid-rebase is blocking someone right now; a diverged one will cost a
    conflict later; uncommitted work is merely unsaved. Everything in sync and
    quiet is noise until it is not.
    """
    now = now or time.time()
    if st["operation"]:
        return 0, "MID-OPERATION"
    ahead, behind = st.get("ahead") or 0, st.get("behind") or 0
    if ahead and behind:
        return 1, "DIVERGED"
    if st["tracked"]:
        return 2, "UNCOMMITTED"
    if ahead:
        return 3, "UNPUSHED"
    if behind:
        return 4, "BEHIND"
    if st["last_ts"] and now - st["last_ts"] < ACTIVE_SECS:
        return 5, "ACTIVE"
    return 6, "QUIET"


BUCKET_COLORS = {0: RED, 1: RED, 2: YELLOW, 3: YELLOW, 4: BLUE, 5: GREEN, 6: DIM}


def work(st):
    """Uncommitted work, compactly. Untracked is flagged but never alarming.

    Untracked files are mostly scratch output -- one tree here carries 12 of
    them permanently -- so they get a marker, not a bucket.
    """
    tracked, untracked = st["tracked"], st["untracked"]
    if not tracked and not untracked:
        return "clean"
    out = str(tracked) if tracked else ""
    if untracked:
        out += "+%d?" % untracked
    return out


def drift(st):
    """Position against the base branch: '=', '^2', 'v3', '^2v3', or '-'."""
    if not st["base"] or st["ahead"] is None:
        return "-"
    ahead, behind = st["ahead"], st["behind"]
    if not ahead and not behind:
        return "="
    return ("^%d" % ahead if ahead else "") + ("v%d" % behind if behind else "")


# Sort cycles *within* a group, never across one. The group order is the whole
# argument the tool makes -- cost of ignoring, not size or recency -- so a sort
# that let ACTIVE float above MID-OPERATION would be answering a different
# question than the one the table exists to answer.
SORT_MODES = ("recent", "repo", "work")

# Filters are subtractive views of the same table, not different tables.
FILTER_MODES = ("all", "dirty", "stuck")
FILTER_LABELS = {"all": "all", "dirty": "uncommitted", "stuck": "mid-operation"}


def sort_key(st, mode="recent"):
    order, _ = bucket(st)
    if mode == "repo":
        return (order, st["repo"], st["tree"], -(st["last_ts"] or 0))
    if mode == "work":
        # Most uncommitted work first: the biggest pile is the one most likely
        # to be lost. Untracked counts, but only after tracked -- see work().
        return (order, -st["tracked"], -st["untracked"], st["repo"], st["tree"])
    # Default. Within a group, the tree touched most recently is the one being
    # worked in.
    return (order, -(st["last_ts"] or 0), st["repo"], st["tree"])


def passes_filter(st, filt):
    if filt == "dirty":
        return bool(st["tracked"] or st["untracked"])
    if filt == "stuck":
        return bool(st["operation"])
    return True


# The three groups where a human (or an agent about to start work) needs to
# look before touching the tree: stuck mid-operation, diverged from its base,
# or carrying uncommitted work. UNPUSHED/BEHIND/ACTIVE/QUIET are all states a
# fresh session can safely start in -- they cost nothing to walk into, which
# is exactly the line `--check` exists to draw for scripts and hooks.
CHECK_THRESHOLD = 2


def needs_attention(st):
    order, _ = bucket(st)
    return order <= CHECK_THRESHOLD


# -------------------------------------------------------------------- render

COLUMNS = (
    ("REPO", lambda s: s["repo"]),
    ("TREE", lambda s: s["tree"]),
    ("BRANCH", lambda s: ("@" + s["branch"]) if s["detached"] else s["branch"]),
    ("WORK", work),
    ("DRIFT", drift),
    ("STASH", lambda s: str(s["stashes"]) if s["stashes"] else ""),
    ("LAST", lambda s: dur(time.time() - s["last_ts"]) if s["last_ts"] else "-"),
)


def visible_rows(states, expand_quiet=False, sort_mode="recent", filt="all"):
    """Sorted, filtered rows with QUIET collapsed away -- exactly what render()
    draws as its table.

    Watch mode's cursor walks this same list, kept as its own function so `j`/
    `k` land on a row that is actually on screen instead of drifting out of
    sync with render()'s own filtering the next time one of the two changes.
    """
    if filt != "all":
        states = [s for s in states if passes_filter(s, filt)]
    rows = sorted(states, key=lambda st: sort_key(st, sort_mode))
    return [s for s in rows if bucket(s)[1] != "QUIET" or expand_quiet]


def default_roots(home=None, cwd=None, env=None):
    """Where a bare `git-roost` looks, computed at scan time.

    1. GIT_ROOST_ROOT, if set (os.pathsep-separated, same convention as PATH).
    2. Otherwise the current directory.

    `home` is accepted for call-site compatibility with older tests; a bare
    run no longer walks well-known folders under $HOME.
    """
    _ = home
    if env is None:
        env = os.environ.get("GIT_ROOST_ROOT")
    if env:
        return tuple(Path(p).expanduser() for p in env.split(os.pathsep) if p)
    if cwd is None:
        try:
            cwd = Path.cwd()
        except OSError:
            return ()
    else:
        cwd = Path(cwd)
    return (cwd,)


def resolved_roots(args, home=None, cwd=None):
    """The directories this invocation will walk. --root wins; else default_roots()."""
    if getattr(args, "root", None):
        return [Path(r).expanduser() for r in args.root]
    return list(default_roots(home=home, cwd=cwd))


def root_problems(roots):
    """(path, reason) for each root that is missing or not a directory.

    A bare run treats the current directory as the root and must fail loudly
    when a configured root is gone -- silent skip used to look like an empty
    fleet.
    """
    problems = []
    for raw in roots:
        path = Path(raw).expanduser()
        try:
            exists = path.exists()
        except OSError as exc:
            problems.append((path, str(exc)))
            continue
        if not exists:
            problems.append((path, "does not exist"))
        elif not path.is_dir():
            problems.append((path, "not a directory"))
    return problems


def empty_fleet_lines(roots, depth=DEFAULT_DEPTH, home=None, cwd=None):
    """What a first run prints when the scan found nothing.

    Name what was searched, and the ways to point the next run at a folder of
    checkouts. Missing roots are reported by root_problems() before a scan;
    this path is the "root exists, no repos within depth" case.
    """
    _ = home, cwd
    lines = ["no git repositories found", ""]
    if roots:
        lines.append("looked under (depth %d):" % depth)
        for raw in roots:
            path = Path(raw).expanduser()
            if not path.exists():
                note = "does not exist"
            elif not path.is_dir():
                note = "not a directory"
            else:
                note = "exists, no git repo within depth"
            lines.append("  %s  (%s)" % (path, note))
        lines.append("")
        lines.append("Point git-roost at a folder of checkouts:")
    else:
        lines.append("No scan root was resolved.")
        lines.append("A bare git-roost uses the current directory;")
        lines.append("pass --root or set GIT_ROOST_ROOT to choose another.")
        lines.append("")
        lines.append("Point git-roost at a folder of checkouts:")
    lines.extend([
        "",
        "  git-roost --root ~/dev              # one tree of checkouts",
        "  git-roost --root .                  # this directory, still depth %d" % depth,
        "  git-roost --root ~/dev --root ~/src # repeatable",
        "",
        "Daily default (%s-separated, same convention as PATH):" % os.pathsep,
        "  GIT_ROOST_ROOT=~/dev git-roost",
    ])
    return lines


def empty_result_lines(args):
    """Empty scan vs empty --repo filter: two different next steps."""
    if getattr(args, "repo", None):
        return ["no repo matches: %s" % ", ".join(args.repo)]
    return empty_fleet_lines(resolved_roots(args), getattr(args, "depth", DEFAULT_DEPTH))


def clip_to_height(lines, height, focus=0):
    """Fit a rendered frame into a terminal that is shorter than the table.

    Watch mode is a TUI, not a dump: 85 trees must not push the status line
    and `?` off the top of the screen. Keep the header and the summary, and
    slide the middle so `focus` (the cursor row's line) stays in view.
    One-shot renders pass height=None and are not clipped -- `git-roost |
    less` still gets the whole list.
    """
    if height is None or height <= 0 or len(lines) <= height:
        return list(lines)
    if height == 1:
        return [lines[0]]
    header = lines[0]
    footer = lines[-1]
    inner = lines[1:-1]
    inner_h = height - 2
    if inner_h <= 0:
        return [header, footer][:height]
    if len(inner) <= inner_h:
        return [header] + inner + [footer]
    focus_inner = max(0, min(int(focus) - 1, len(inner) - 1))
    start = focus_inner - inner_h // 2
    start = max(0, min(start, len(inner) - inner_h))
    window = inner[start:start + inner_h]
    above = start
    below = len(inner) - start - len(window)
    if above and window:
        window[0] = c("  ... %d more above  (k)" % above, DIM)
    if below and window:
        window[-1] = c("  ... %d more below  (j)" % below, DIM)
    return [header] + window + [footer]


def write_tty_frame(width, height, lines):
    """Overwrite the visible screen in place.

    `\033[2J` (erase display) is what made watch mode flash: the whole
    buffer went black between paints. Home the cursor, write each row, and
    clear to end-of-line so a shorter replacement does not leave junk. No
    newlines -- a newline on the last row would scroll.
    """
    rows = list(lines[:height])
    while len(rows) < height:
        rows.append("")
    sys.stdout.write("\033[H")
    for i, row in enumerate(rows):
        sys.stdout.write("\033[%d;1H%s\033[K" % (i + 1, row))
    sys.stdout.flush()


def render_pending(paths, ordered, width=160, height=None):
    """Stable loading table: one slot per discovered path, never regrouped.

    The grouped render jumps as trees finish (a row appears under UNCOMMITTED,
    then the next paint shuffles it). This keeps discover order so cells fill
    in place until collect() is done.
    """
    lines = [c("  REPO                      TREE                      WORK   DRIFT  SUBJECT", BOLD)]
    for path, st in zip(paths, ordered):
        name = Path(path).name
        if st:
            subject = ascii_safe(st.get("last_subject") or "")
            if len(subject) > 40:
                subject = subject[:37] + "..."
            lines.append("  %s  %s  %s  %s  %s" % (
                st["repo"][:24].ljust(24),
                st["tree"][:24].ljust(24),
                work(st).ljust(5),
                drift(st).ljust(5),
                subject,
            ))
        else:
            lines.append(c("  %s  %s" % (name[:24].ljust(24), "..."), DIM))
    done = sum(1 for s in ordered if s)
    lines.append("")
    lines.append("%d of %d tree(s) scanned" % (done, len(paths)))
    return clip_to_height(lines, height, 0)


def render(states, width=160, expand_quiet=False, sort_mode="recent", filt="all",
           changed=None, github=False, cursor=None, height=None):
    """`changed` is a set of tree paths whose bucket, WORK or DRIFT differ from
    the previous watch-mode frame -- see frame_signature(). None outside watch
    mode, where there is no previous frame to compare against. `github` adds
    the opt-in PR/CI column -- see GITHUB_COLUMN. `cursor` is the highlighted
    row index in the shown list (watch mode). `height` clips the frame to a
    terminal, keeping that row in view.
    """
    if not states:
        return clip_to_height(["no git repositories found"], height, 0)

    fleet = states
    total = len(states)
    if filt != "all":
        states = [s for s in states if passes_filter(s, filt)]
        if not states:
            return clip_to_height(
                ["no tree matches filter: %s" % FILTER_LABELS[filt]], height, 0)

    rows = sorted(states, key=lambda st: sort_key(st, sort_mode))
    shown = [s for s in rows if bucket(s)[1] != "QUIET" or expand_quiet]
    quiet = [s for s in rows if bucket(s)[1] == "QUIET" and not expand_quiet]

    # The PR/CI column is opt-in: appended rather than baked into COLUMNS, so
    # the default table is byte-for-byte what it always was for anyone who
    # never passes --github.
    columns = COLUMNS + (GITHUB_COLUMN,) if github else COLUMNS

    cells = [[fn(s) for _, fn in columns] for s in shown]
    headers = [h for h, _ in columns]
    widths = [
        max([len(headers[i])] + [row[i] and len(row[i]) or 0 for row in cells] or [0])
        for i in range(len(columns))
    ]

    used = sum(widths) + 2 * len(widths) + 2
    subject_w = max(20, width - used - 2)

    lines = []
    lines.append(c("  " + "  ".join(
        headers[i].ljust(widths[i]) for i in range(len(columns))
    ) + "  " + "SUBJECT", BOLD))

    current = None
    focus_line = 0
    row_i = 0
    for st, row in zip(shown, cells):
        order, label = bucket(st)
        if label != current:
            current = label
            lines.append(c(label, BUCKET_COLORS.get(order, ""), BOLD))
        subject = ascii_safe(st["last_subject"])
        if st["operation"]:
            # Overwrite the subject: what it is stuck doing beats what it last
            # did, and an unresolved conflict count is the actionable part.
            subject = "** %s in progress" % st["operation"]
            if st["conflicts"]:
                subject += ", %d conflict(s)" % st["conflicts"]
            subject += " **"
        if len(subject) > subject_w:
            subject = subject[: subject_w - 3] + "..."
        body = "  ".join(row[i].ljust(widths[i]) for i in range(len(columns)))
        is_changed = bool(changed) and st["path"] in changed
        is_cursor = cursor is not None and row_i == cursor
        if is_cursor:
            focus_line = len(lines)
            marker = "> "
        elif is_changed:
            marker = "* "
        else:
            marker = "  "
        line = marker + body + "  " + subject
        if is_cursor:
            line = c(line, BOLD, CYAN)
        elif is_changed:
            line = c(line, MAGENTA, BOLD)
        lines.append(line)
        row_i += 1

    if quiet:
        names = " . ".join(sorted({"%s/%s" % (s["repo"], s["tree"]) for s in quiet}))
        lines.append("")
        lines.append(c("QUIET (%d)  " % len(quiet) + names, DIM))

    lines.append("")
    # Every count after the tree count describes the whole fleet, filtered or
    # not. Recomputing them over the filtered subset makes "2 mid-operation"
    # mean "2 of the ones you are currently looking at", which is the same
    # misreading the "N of M" wording above exists to prevent -- and it is worse
    # here, because the number stays plausible while quietly changing meaning.
    repos = len({s["common_dir"] for s in fleet})
    dirty = sum(1 for s in fleet if s["tracked"])
    stuck = sum(1 for s in fleet if s["operation"])
    if len(states) == total:
        summary = "%d tree(s) across %d repo(s)" % (total, repos)
    else:
        # Say both numbers. A filtered count printed alone reads as the whole
        # fleet, which is exactly the wrong impression for a tool whose job is
        # telling you what you have not looked at.
        summary = "%d of %d tree(s) across %d repo(s)  [filter: %s]" % (
            len(states), total, repos, FILTER_LABELS[filt])
    if dirty:
        summary += "  |  %d with uncommitted work" % dirty
    if stuck:
        summary += "  |  %d mid-operation" % stuck
    lines.append(summary)
    return clip_to_height(lines, height, focus_line)


# ---------------------------------------------------------------- commit feed

def commits_for(st, limit):
    out = git(st["path"], "log", "-%d" % limit, "--format=%ct%x00%h%x00%an%x00%s")
    if not out:
        return []
    rows = []
    for line in out.splitlines():
        bits = line.split("\0")
        if len(bits) != 4 or not bits[0].isdigit():
            continue
        rows.append({
            "ts": int(bits[0]),
            "hash": bits[1],
            "author": bits[2],
            "subject": bits[3],
            "repo": st["repo"],
            "tree": st["tree"],
            "branch": st["branch"],
        })
    return rows


def render_log(states, limit, width=160, height=None):
    """One merged feed, newest first.

    Deduped by (common dir, sha): a repo and its worktrees share history, so
    every commit would otherwise appear once per tree -- here that is five or six
    times for the busy repos.
    """
    by_repo = {}
    for st in states:
        by_repo.setdefault(st["common_dir"] or st["toplevel"], []).append(st)

    work_items = []
    for trees in by_repo.values():
        for st in trees:
            work_items.append(st)

    with ThreadPoolExecutor(max_workers=min(GIT_WORKERS, max(1, len(work_items)))) as pool:
        batches = list(pool.map(lambda s: commits_for(s, limit), work_items))

    seen = set()
    merged = []
    for st, batch in zip(work_items, batches):
        repo_key = st["common_dir"] or st["toplevel"]
        for row in batch:
            key = (repo_key, row["hash"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)

    merged.sort(key=lambda r: -r["ts"])
    merged = merged[:limit]
    if not merged:
        return clip_to_height(["no commits found"], height, 0)

    now = time.time()
    w_repo = max([4] + [len(r["repo"]) for r in merged])
    w_branch = max([6] + [len(r["branch"]) for r in merged])
    w_author = max([6] + [len(ascii_safe(r["author"])) for r in merged])
    used = 5 + w_repo + w_branch + w_author + 9 + 10
    subject_w = max(20, width - used)

    lines = [c("  ".join((
        "AGE".ljust(4), "REPO".ljust(w_repo), "BRANCH".ljust(w_branch),
        "SHA".ljust(7), "AUTHOR".ljust(w_author), "SUBJECT",
    )), BOLD)]
    for r in merged:
        subject = ascii_safe(r["subject"])
        if len(subject) > subject_w:
            subject = subject[: subject_w - 3] + "..."
        lines.append("  ".join((
            dur(now - r["ts"]).ljust(4),
            r["repo"].ljust(w_repo),
            r["branch"].ljust(w_branch),
            r["hash"].ljust(7),
            ascii_safe(r["author"]).ljust(w_author),
            subject,
        )))
    lines.append("")
    lines.append("%d commit(s) across %d repo(s)" % (len(merged), len({r["repo"] for r in merged})))
    return clip_to_height(lines, height, 0)


def frame_signature(st):
    """What "changed since last frame" means: bucket, WORK and DRIFT.

    Not the whole state dict -- LAST ticks every second and would mark every
    row changed on every redraw, which is the opposite of a "what moved"
    signal.
    """
    return (bucket(st)[1], work(st), drift(st))


def detail_lines(st, width=160, height=None):
    """Everything one tree has that the table has no room for: the whole
    stash list rather than a count, what it is stuck doing, and its last five
    commits.

    Read-only, same as everywhere else -- `stash list` and `stash show -p` are
    both on the READ_ONLY allowlist, and commits come from the already-allowed
    `commits_for()`.
    """
    lines = [c("%s/%s" % (st["repo"], st["tree"]), BOLD)]
    lines.append(("@" + st["branch"]) if st["detached"] else (st["branch"] or "-"))
    lines.append(st["path"])
    lines.append("")

    if st["operation"]:
        op_line = "** %s in progress **" % st["operation"]
        if st["conflicts"]:
            op_line += "  (%d conflict(s))" % st["conflicts"]
        lines.append(c(op_line, RED, BOLD))
        lines.append("")

    lines.append(c("STASH", BOLD))
    stash_out = git(st["path"], "stash", "list")
    entries = [ln for ln in (stash_out or "").splitlines() if ln]
    if not entries:
        lines.append("  (none)")
    else:
        for entry in entries:
            lines.append("  " + ascii_safe(entry))
        # The most recent stash is the one someone is most likely to come back
        # for, so it is the one worth a diffstat rather than just a subject
        # line. `-p` is capped at 30 lines -- enough to see what is in it
        # without dumping a whole patch into a table-shaped screen.
        patch = git(st["path"], "stash", "show", "-p", "stash@{0}")
        if patch:
            diff_lines = patch.splitlines()
            lines.append("")
            lines.append(c("  stash@{0}:", DIM))
            for dl in diff_lines[:30]:
                lines.append("  " + ascii_safe(dl))
            if len(diff_lines) > 30:
                lines.append("  ... %d more line(s)" % (len(diff_lines) - 30))
    lines.append("")

    lines.append(c("LAST 5 COMMITS", BOLD))
    commits = commits_for(st, 5)
    if not commits:
        lines.append("  (no commits)")
    else:
        now = time.time()
        for r in commits:
            lines.append("  %s  %s  %s" % (
                dur(now - r["ts"]).ljust(4), r["hash"].ljust(7),
                ascii_safe(r["subject"])))
    lines.append("")
    lines.append(c("any other key returns to the table", DIM))
    return clip_to_height(lines, height, 0)


# ---------------------------------------------------------------- github (gh)

# What this tool is allowed to run through `gh`: command -> (permitted
# subcommands, most positional arguments a read-only form can take).
#
# gh does not have git's flag-laundering problem -- `symbolic-ref --short HEAD
# X` rewrites HEAD despite opening with a read flag, but no flag turns
# `gh pr view` into a write. Every gh operation that mutates a PR (merge,
# close, edit, ready, review, comment) is a separate subcommand, not a flag on
# list/view/status. So the allowlist only needs (command, subcommand); the
# positional cap is still here as the same fail-closed backstop check_read_only
# uses -- a future call site that starts interpolating a bare argument where a
# flag value belongs gets refused rather than silently let through.
GH_READ_ONLY = {
    "pr": (frozenset(("view", "list", "status")), 0),
}


def check_gh_read_only(args):
    """Raise unless args is a form of `gh` that cannot modify anything.

    Split out from gh_call() for the same reason check_read_only() is split
    from git(): so the test suite can assert the policy directly, and because
    the guarantee that this tool cannot write matters more than any one call
    site remembering to respect it.
    """
    if not args:
        raise NotReadOnly("git-roost refuses an empty gh invocation")
    cmd = args[0]
    if cmd not in GH_READ_ONLY:
        raise NotReadOnly("git-roost refuses non-read-only gh command: %r" % cmd)
    allowed, max_positional = GH_READ_ONLY[cmd]
    sub = args[1] if len(args) > 1 else ""
    if sub not in allowed:
        raise NotReadOnly(
            "git-roost refuses %r: only %s are read-only"
            % (" ".join(args[:2]), ", ".join(sorted(allowed)))
        )
    # Flag values must use --flag=value form (as every call site here does),
    # exactly like git()'s "--porcelain=v2": a space-separated value would
    # look positional and could push this over the cap for a legitimate call.
    positional = [a for a in args[2:] if not a.startswith("-")]
    if len(positional) > max_positional:
        raise NotReadOnly(
            "git-roost refuses %r: %d positional argument(s), read-only gh %s "
            "%s takes at most %d"
            % (" ".join(args), len(positional), cmd, sub, max_positional)
        )


def gh_call(dirpath, *args):
    """One read-only `gh` call, cwd-scoped to a tree. None on any failure.

    Same best-effort contract as git(): no `gh` on PATH, no auth, no GitHub
    remote for this tree, a rate limit, or a slow network are all just "we
    don't know" for this one tree, never a reason to fail the whole scan. gh
    has no -C equivalent, so the working directory does the scoping git() gets
    from -C -- this is why every caller passes a path, same shape as git().
    """
    check_gh_read_only(args)
    if GH_PATH is None:
        return None
    try:
        out = subprocess.run(
            (GH_PATH,) + tuple(args),
            cwd=str(dirpath),
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


# The five facts kept per tree. Present in a JSON record only when --github was
# passed -- see apply_github_facts() -- which keeps the default `--json`
# contract untouched for every consumer that never asks for GitHub data.
GITHUB_KEYS = ("pr_number", "pr_state", "pr_draft", "pr_review", "pr_ci")


def github_facts(path):
    """PR + CI facts for one tree's current branch, or None.

    One `gh pr view` call gets everything this tool wants: the PR number,
    whether it is draft, the review decision, and the full check-run rollup
    for HEAD, all in one round trip -- run from inside the tree so gh resolves
    "the PR for this branch" itself, the same way git() relies on -C to scope
    a call instead of asking the caller to already know the answer.

    None whenever there is no open PR for the branch, no GitHub remote, `gh`
    is missing or unauthenticated, or the call fails for any other reason --
    it renders as a blank column, not an error.
    """
    out = gh_call(
        path, "pr", "view",
        "--json=number,state,isDraft,reviewDecision,statusCheckRollup",
    )
    if not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None

    # statusCheckRollup mixes GitHub Actions check-runs (status/conclusion)
    # with external commit statuses (state instead) -- both need reading, or
    # a repo whose CI is a status API integration would always show blank.
    # Any run still in progress makes the whole PR "pending"; any failure
    # makes it "failure" even if something else already succeeded; otherwise
    # a rollup that reported anything at all is "success".
    ci = None
    checks = data.get("statusCheckRollup") or []
    if checks:
        failed = pending = seen = False
        for chk in checks:
            status = (chk.get("status") or "").upper()
            conclusion = (chk.get("conclusion") or chk.get("state") or "").upper()
            if status and status != "COMPLETED":
                pending = True
                continue
            seen = True
            if conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT", "ERROR"):
                failed = True
        if failed:
            ci = "failure"
        elif pending:
            ci = "pending"
        elif seen:
            ci = "success"

    return {
        "pr_number": data.get("number"),
        "pr_state": (data.get("state") or "").lower() or None,
        "pr_draft": bool(data.get("isDraft")),
        "pr_review": data.get("reviewDecision") or None,
        "pr_ci": ci,
    }


def github_facts_map(states):
    """{tree path: facts} for every tree, fetched fresh, fanned out like collect().

    Its own ThreadPoolExecutor and its own GH_WORKERS cap -- sharing the git
    pool would let a slow `gh` call hold a worker a local git scan needed, the
    exact starvation this tool exists to avoid inflicting on itself.
    """
    if not states or GH_PATH is None:
        return {}
    with ThreadPoolExecutor(max_workers=min(GH_WORKERS, len(states))) as pool:
        facts = list(pool.map(lambda s: github_facts(Path(s["path"])), states))
    return {s["path"]: (f or {}) for s, f in zip(states, facts)}


def apply_github_facts(states, facts_map):
    """Merge PR/CI facts onto states in place, defaulting absent ones to None.

    Always sets all five keys once called, even for a tree facts_map has
    nothing for (no PR, gh missing, the call failed) -- that is what makes the
    keys a stable, always-present-when-enabled shape rather than sometimes
    there and sometimes not depending on what gh happened to return.
    """
    for st in states:
        gh = facts_map.get(st["path"]) or {}
        st["pr_number"] = gh.get("pr_number")
        st["pr_state"] = gh.get("pr_state")
        st["pr_draft"] = gh.get("pr_draft", False)
        st["pr_review"] = gh.get("pr_review")
        st["pr_ci"] = gh.get("pr_ci")
    return states


def github_cell(s):
    """PR column: '#123', '#123+' success, '#123x' failure, '#123~' pending,
    '#123 draft', or blank when there is no open PR.

    ASCII on purpose, not the checkmark/cross this might otherwise reach for --
    ascii_safe() exists a few hundred lines up because the Windows console
    codepage mangles exactly that kind of glyph mid-table, and this column
    should not need its own escape hatch from the same problem.
    """
    n = s.get("pr_number")
    if not n:
        return ""
    if s.get("pr_draft"):
        return "#%d draft" % n
    mark = {"success": "+", "failure": "x", "pending": "~"}.get(s.get("pr_ci"), "")
    return "#%d%s" % (n, mark)


# Appended to COLUMNS only when --github is passed -- see render()'s `github`
# parameter. Kept separate from COLUMNS itself so the default table layout is
# untouched for the far more common case of nobody asking for network calls.
GITHUB_COLUMN = ("PR", github_cell)


# ------------------------------------------------------------------- watch keys

KEYMAP = (
    ("?", "this map"),
    ("r", "refresh now"),
    ("s", "sort: recent / repo / work (within a group, never across)"),
    ("f", "filter: all / uncommitted / mid-operation"),
    ("a", "expand or collapse QUIET"),
    ("l", "toggle table / commit feed"),
    ("j", "move the cursor down (scrolls the viewport)"),
    ("k", "move the cursor up (scrolls the viewport)"),
    ("enter", "open a detail view for the highlighted tree"),
    ("q", "quit"),
)


class Keys:
    """Single-key reads in watch mode, without curses.

    curses is the obvious tool and the one leghorn and legbar reach for, but it
    is not in the Windows stdlib and git-roost claims Windows support. It also
    takes over the screen, which would mean two rendering paths for one table.
    So this reads raw keys instead: cbreak plus select on POSIX, msvcrt on
    Windows, and a plain sleep anywhere else -- a box with neither, or a stdout
    that is a pipe rather than a terminal, degrades to exactly the timer-only
    redraw that shipped in 0.1.

    Deliberately not used by the one-shot render. That path touches no terminal
    settings at all, which is what makes `git-roost | less` and `git-roost
    --json | jq` safe.
    """

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdin
        self.enabled = False
        self._fd = None
        self._saved = None

    def __enter__(self):
        try:
            if not self.stream.isatty():
                return self
        except (AttributeError, ValueError):
            # A stdin that has been replaced or closed -- under a test harness,
            # or `git-roost -w < /dev/null`. Not an error, just no keys.
            return self
        if msvcrt is not None:
            self.enabled = True
        elif termios is not None:
            try:
                self._fd = self.stream.fileno()
                self._saved = termios.tcgetattr(self._fd)
                # setcbreak, not setraw: it leaves ISIG alone, so Ctrl-C still
                # raises KeyboardInterrupt and the existing exit path holds.
                tty.setcbreak(self._fd)
                self.enabled = True
            except Exception:
                self._saved = None
        return self

    def __exit__(self, *exc):
        # Restoring matters more than it looks. cbreak leaves the terminal with
        # no line discipline, so an exception path that skipped this would hand
        # the user back a shell that does not echo what they type.
        if self._saved is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass
        return False

    def wait(self, timeout):
        """Wait up to timeout seconds for a key. Returns the key, or None."""
        if not self.enabled:
            time.sleep(timeout)
            return None
        if msvcrt is not None:
            deadline = time.time() + timeout
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        # Function and arrow keys arrive as a prefix plus a
                        # scan code. Swallow the second half, or an arrow key
                        # reads as whatever letter shares its code -- Up is
                        # 'H', which is nothing here today but would silently
                        # become a hotkey the moment one is added.
                        msvcrt.getwch()
                        continue
                    return ch
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                time.sleep(min(0.05, remaining))
        r, _, _ = select.select([self._fd], [], [], timeout)
        if not r:
            return None
        try:
            return os.read(self._fd, 1).decode("utf-8", "replace")
        except OSError:
            return None


def help_lines():
    width = max(len(k) for k, _ in KEYMAP)
    out = [c("KEYS", BOLD), ""]
    out += ["  %s   %s" % (c(k.ljust(width), BOLD), desc) for k, desc in KEYMAP]
    out += ["", c("any other key returns to the table", DIM)]
    return out


def apply_key(view, key, shown=None):
    """Fold one keypress into the live view. Returns "quit", "help" or None.

    Split out of the watch loop so the keymap can be tested without a terminal.
    Every other path in this file is testable headlessly and this one was not,
    which is how a key ends up bound to nothing and nobody notices.

    `shown` is the row list the table currently has on screen (from
    visible_rows()) -- j/k/enter need it to know how far the cursor can move
    and which tree "enter" opens. It is None outside watch mode and for keys
    that do not touch the cursor, so every other call site is unaffected.

    Exiting a detail view is not handled here: the watch loop clears
    view["detail"] on the next keypress before apply_key is even called,
    the same way it already handles the help overlay -- one overlay-dismissal
    path, not two.
    """
    if key in ("q", "Q"):
        return "quit"
    if key == "?":
        return "help"
    if key in ("s", "S"):
        view["sort"] = SORT_MODES[
            (SORT_MODES.index(view["sort"]) + 1) % len(SORT_MODES)]
        view["cursor"] = 0
    elif key in ("f", "F"):
        view["filter"] = FILTER_MODES[
            (FILTER_MODES.index(view["filter"]) + 1) % len(FILTER_MODES)]
        view["cursor"] = 0
    elif key in ("a", "A"):
        view["quiet"] = not view["quiet"]
        view["cursor"] = 0
    elif key in ("l", "L"):
        view["log"] = not view["log"]
        view["cursor"] = 0
    elif key in ("j", "J"):
        # Sort, filter or the quiet toggle can shrink the shown set out from
        # under a cursor sitting near the bottom -- clamp every time rather
        # than trusting the value left over from a wider frame.
        if shown:
            last = len(shown) - 1
            view["cursor"] = min(max(0, view.get("cursor", 0)) + 1, last)
    elif key in ("k", "K"):
        if shown:
            last = len(shown) - 1
            view["cursor"] = max(0, min(view.get("cursor", 0), last) - 1)
    elif key in ("\r", "\n", "enter"):
        if shown:
            cursor = max(0, min(view.get("cursor", 0), len(shown) - 1))
            view["cursor"] = cursor
            view["detail"] = shown[cursor]
    # 'r' -- and any unbound key -- falls through to an immediate redraw, which
    # is what refresh means here: the scan happens on the next line of the loop,
    # not behind a cache.
    return None


def status_line(sort_mode, filt, expand_quiet, interval, view_mode="table",
                loading=None):
    """The one line that says what view you are looking at.

    Without it a filtered table is indistinguishable from a fleet that happens
    to be quiet, which is the same failure the summary line guards against.
    `view_mode` is "table", "log" or "detail" -- `l` and `enter` change what is
    on screen independently of sort/filter/quiet, so it needs saying too.
    `loading` is the watch-mode spinner ("scanning 12/92 |") while collect()
    is still in flight -- None once the frame is complete.
    """
    bits = [
        time.strftime("git-roost  %H:%M:%S"),
        "view:%s" % view_mode,
        "sort:%s" % sort_mode,
        "filter:%s" % FILTER_LABELS[filt],
        "quiet:%s" % ("shown" if expand_quiet else "collapsed"),
        "%gs" % interval,
    ]
    if loading:
        bits.append(loading)
    return c("  ".join(bits), DIM) + c("   [?] keys", BOLD)


# ------------------------------------------------------------------------ cli

def oneshot_scan_progress(done, total, spin_i):
    """One stderr line for a bare `git-roost` -- not used in -w.

    Starts as `scanning 92 tree(s)...` and ticks `12/92` plus a spinner
    until the collect finishes, then restores the same wording with a newline
    so the table that follows is what people already expect.
    """
    if done <= 0 or done >= total:
        msg = "scanning %d tree(s)..." % total
    else:
        msg = "scanning %d/%d tree(s)... %s" % (done, total, SPIN[spin_i % 4])
    sys.stderr.write("\r" + msg.ljust(44))
    sys.stderr.flush()


def rewrite_status(width, view, args, loading):
    """Update only the status line so a refresh does not redraw the table."""
    view_mode = "detail" if view.get("detail") is not None else (
        "log" if view.get("log") else "table")
    line = status_line(
        view["sort"], view["filter"], view["quiet"], args.watch,
        view_mode, loading=loading)
    sys.stdout.write("\033[1;1H%s\033[K" % line)
    sys.stdout.flush()


def draw_watch_frame(ansi, width, height, view, args, helping, states,
                     changed, loading=None, pending=None):
    """One alternate-screen paint. Returns the row list j/k walk."""
    body_h = max(4, height - 1)
    if getattr(args, "json", False):
        out = [json.dumps(states, indent=2, sort_keys=True)]
        shown = []
    elif helping:
        out = clip_to_height(help_lines(), body_h, 0)
        shown = visible_rows(states, view["quiet"], view["sort"], view["filter"])
    elif pending is not None:
        paths, ordered = pending
        out = render_pending(paths, ordered, width, height=body_h)
        shown = []
    elif not states:
        if loading:
            out = clip_to_height([loading], body_h, 0)
        else:
            out = clip_to_height(empty_result_lines(args), body_h, 0)
        shown = []
    else:
        out = render_view(states, width, view, changed,
                          getattr(args, "github", False), height=body_h)
        shown = visible_rows(states, view["quiet"], view["sort"], view["filter"])
    view_mode = "detail" if view.get("detail") is not None else (
        "log" if view.get("log") else "table")
    frame = [status_line(
        view["sort"], view["filter"], view["quiet"], args.watch,
        view_mode, loading=loading)] + list(out)
    # Watch only runs when ansi is on (see main). The old "print another
    # separator + frame" else branch is what flooded a non-VT cmd every 3s.
    if not ansi:
        raise RuntimeError("draw_watch_frame requires a VT-capable console")
    write_tty_frame(width, height, frame)
    return shown


def scan(args, on_progress=None):
    roots = resolved_roots(args)
    paths = discover(roots, args.depth)
    oneshot = (
        bool(paths)
        and not getattr(args, "json", False)
        and not getattr(args, "watch", None)
        and sys.stderr.isatty()
    )
    spin = {"n": 0, "last": 0.0}

    def tick(done, total, ordered):
        if on_progress:
            on_progress(done, total, ordered, paths)
        if not oneshot:
            return
        now = time.time()
        if done < total and now - spin["last"] < 0.08:
            return
        spin["last"] = now
        oneshot_scan_progress(done, total, spin["n"])
        spin["n"] += 1

    if oneshot:
        oneshot_scan_progress(0, len(paths), 0)
    cb = tick if (oneshot or on_progress) else None
    states = collect(paths, on_progress=cb)
    if oneshot:
        oneshot_scan_progress(len(paths), len(paths), 0)
        sys.stderr.write("\n")
        sys.stderr.flush()
    if args.repo:
        # A substring match, not exact: "--repo roost" finding both roost and
        # git-roost is a feature here, not ambiguity -- there is no id to match
        # exactly against, and the fleet is small enough that a loose match
        # costs nothing to skim past.
        needles = [r.lower() for r in args.repo]
        states = [s for s in states if any(n in s["repo"].lower() for n in needles)]
    return states


def body(args, width, view=None, changed=None, github_map=None):
    """One frame plus the states it was built from. `view` is the live watch
    state; None means the flags alone.

    The one-shot path passes None and renders exactly what 0.1 rendered, plus
    whatever --repo/--sort/--filter narrowed or ordered the scan -- those are
    static flags, not watch state, so they apply identically to the piped
    path. That is the contract behind `git-roost | less` and `git-roost --json
    | jq`: keys move the watch view and nothing else. States come back
    alongside the lines so --fail-on/--check can inspect the fleet without a
    second scan.

    `changed` is forwarded to render() for the table view only -- see
    frame_signature(). The watch loop computes it from frame to frame; the
    one-shot path never has a previous frame to compare against.

    `github_map` is the watch loop's cache hook: pass a pre-fetched {path:
    facts} map to skip a fresh `gh` round trip this frame, or leave it None to
    fetch fresh (which is exactly right for the one-shot path -- it only ever
    calls this once, so there is no cache to reuse). getattr() guards
    args.github because the inline Args stubs a couple of tests use predate
    this flag and do not set it.
    """
    states = scan(args)
    if getattr(args, "github", False):
        if github_map is None:
            github_map = github_facts_map(states)
        apply_github_facts(states, github_map)
    show_github = getattr(args, "github", False)
    if args.json:
        filt = view["filter"] if view is not None else args.filter
        if filt != "all":
            states = [s for s in states if passes_filter(s, filt)]
        return [json.dumps(states, indent=2, sort_keys=True)], states
    if not states:
        # First-run / wrong-root: say where we looked. An empty --log feed
        # of nothing is the same situation as an empty table.
        return empty_result_lines(args), states
    if view is None:
        if args.log is not None:
            return render_log(states, args.log, width), states
        return render(states, width, expand_quiet=args.all,
                      sort_mode=args.sort, filt=args.filter,
                      github=show_github), states
    return render_view(states, width, view, changed, show_github), states


def render_view(states, width, view, changed=None, github=False, height=None):
    """Render one already-scanned watch-mode frame from the live view state.

    Split out of body() so the watch loop can reuse a single scan for both the
    rendered frame and the cursor's row list, instead of scanning twice a
    redraw. `height` is the body budget after the status line -- None means
    do not clip (the one-shot path).
    """
    if view.get("detail") is not None:
        target = view["detail"]
        # Watch mode rescans every interval; show the freshest state for that
        # tree rather than freezing the frame it was opened on. If the tree
        # has vanished (worktree removed mid-session), fall back to what was
        # last known about it rather than crashing.
        current = next((s for s in states if s["path"] == target["path"]), None)
        return detail_lines(current or target, width, height=height)
    if view.get("log"):
        limit = view.get("log_limit") or 25
        return render_log(states, limit, width, height=height)
    return render(states, width, expand_quiet=view["quiet"],
                  sort_mode=view["sort"], filt=view["filter"],
                  changed=changed, github=github,
                  cursor=view.get("cursor"), height=height)


def exit_code(states, fail_on):
    """0 unless --fail-on names a condition present in the (unfiltered) fleet.

    Checked against the whole fleet, not a --filter view, for the same reason
    the summary line always says both counts: a hook that asked "is anything
    stuck" should not get a false "no" because someone also passed --filter
    dirty for a human to read at the same time.
    """
    if not fail_on or fail_on == "none":
        return 0
    for st in states:
        order, _ = bucket(st)
        if fail_on == "stuck" and order == 0:
            return 1
        if fail_on == "diverged" and order <= 1:
            return 1
        if fail_on == "dirty" and order <= 2:
            return 1
    return 0


def build_parser():
    """The CLI, as a function so completion can enumerate the real flag set.

    Anything hand-listed in a completion script drifts the first time a flag
    is added; deriving the words from the parser makes drift impossible.
    """
    ap = argparse.ArgumentParser(
        prog="git-roost",
        description=(
            "top for git -- every repo and worktree, most actionable first.\n\n"
            "Bare run opens the TUI and scans the current directory. Pass\n"
            "--root DIR or set GIT_ROOST_ROOT to choose another tree. A root\n"
            "that does not exist is an error. Use --once (or pipe stdout) for\n"
            "a one-shot table."
        ),
        epilog=("watch-mode keys:  " + "  ".join(
            "%s %s" % (k, d.split(":")[0].split("(")[0].strip())
            for k, d in KEYMAP)),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version="git-roost %s" % __version__)
    ap.add_argument("-w", "--watch", nargs="?", const=DEFAULT_WATCH, type=float,
                    default=DEFAULT_WATCH, metavar="SECS",
                    help="redraw every SECS seconds (default %.0f; this is the "
                         "default mode on a TTY); takes keys, see below"
                         % DEFAULT_WATCH)
    ap.add_argument("-1", "--once", action="store_true",
                    help="render once and exit (overrides --watch; also used "
                         "when stdout is not a TTY)")
    ap.add_argument("--log", nargs="?", const=25, type=int, metavar="N",
                    help="commit feed across every repo, newest first (default 25)")
    ap.add_argument("-a", "--all", action="store_true", help="expand the QUIET group")
    ap.add_argument("--root", action="append", nargs="?", const=".", metavar="DIR",
                    help="where to look for repos (repeatable; omit DIR to "
                         "mean the current directory. Default with no --root: "
                         "$GIT_ROOST_ROOT if set, else the current directory. "
                         "Missing roots are an error)")
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH, metavar="N",
                    help="how deep to search below each root (default %d)" % DEFAULT_DEPTH)
    ap.add_argument("--repo", action="append", metavar="NAME",
                    help="only repos whose name contains NAME (repeatable, "
                         "case-insensitive)")
    ap.add_argument("--sort", choices=SORT_MODES, default=SORT_MODES[0],
                    help="sort within each group (default %s)" % SORT_MODES[0])
    ap.add_argument("--filter", choices=FILTER_MODES, default="all", metavar="MODE",
                    help="show only this view: %s (default all)" % ", ".join(FILTER_MODES))
    ap.add_argument("--check", action="store_true",
                    help="print nothing but a summary and exit 1 if any tree is "
                         "mid-operation, diverged, or has uncommitted work; 0 "
                         "otherwise. For scripts and hooks, not the table.")
    ap.add_argument("--fail-on", choices=("none", "dirty", "diverged", "stuck"),
                    default="none", metavar="COND",
                    help="like --check, but keeps the normal render and only "
                         "changes the exit code -- pick which of stuck "
                         "(mid-operation), diverged (also ahead+behind a base) or "
                         "dirty (also uncommitted work) trips it. Checked against "
                         "the whole fleet, not --filter. Default none (always 0).")
    ap.add_argument("--json", action="store_true", help="emit records as JSON")
    ap.add_argument("--no-color", action="store_true", help="disable colour output")
    ap.add_argument("--github", action="store_true",
                    help="add a PR/CI column via `gh` (opt-in: network calls, "
                         "needs gh on PATH and authenticated; silently omitted "
                         "otherwise)")
    ap.add_argument("--github-interval", type=float, default=30.0, metavar="SECS",
                    help="in watch mode, refresh PR/CI data at most every SECS "
                         "seconds rather than every redraw (default 30); no "
                         "effect outside watch mode or without --github")
    ap.add_argument("--print-completion", choices=("bash", "zsh", "powershell"),
                    metavar="SHELL",
                    help="print a completion script for SHELL (bash, zsh, "
                         "powershell) and exit")
    return ap


def _completion_flag_words():
    words = []
    for action in build_parser()._actions:
        if action.option_strings:
            words.extend(action.option_strings)
    return sorted(set(words), key=lambda s: (len(s), s))


# The flags that consume a value, kept in one place so the three scripts agree
# on when the *next* word is a value rather than a flag. Same approach as
# roost's: no argcomplete dependency, just the parser's own word list plus
# this map of what each value can be ("" means free-form: no suggestions).
_COMPLETION_VALUE_FLAGS = {
    "-w": "", "--watch": "", "--log": "", "--root": "", "--depth": "",
    "--repo": "", "--github-interval": "",
    "--sort": " ".join(SORT_MODES),
    "--filter": " ".join(FILTER_MODES),
    "--fail-on": "none dirty diverged stuck",
    "--print-completion": "bash zsh powershell",
}


def print_completion(shell):
    flags = " ".join(_completion_flag_words())
    value_flags = "|".join(sorted(_COMPLETION_VALUE_FLAGS))
    choice_cases = "\n".join(
        '        %s)\n            COMPREPLY=( $(compgen -W "%s" -- "$cur") )\n'
        '            return 0\n            ;;' % (flag, choices)
        for flag, choices in sorted(_COMPLETION_VALUE_FLAGS.items()) if choices)
    if shell == "bash":
        return """# bash completion for git-roost. Install to
# /usr/share/bash-completion/completions/git-roost, or eval:
#   eval "$(git-roost --print-completion bash)"
_git_roost() {
    local cur prev
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    local opts="%s"
    case "$prev" in
%s
        %s)
            return 0
            ;;
    esac
    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
    fi
}
complete -F _git_roost git-roost
""" % (flags, choice_cases, value_flags)
    if shell == "zsh":
        return """#compdef git-roost
# zsh completion for git-roost. Install to
# /usr/share/zsh/vendor-completions/_git-roost, or eval:
#   source <(git-roost --print-completion zsh)
_arguments -S \\
  '--version[print the version and exit]' \\
  '(-w --watch)'{-w,--watch}'[redraw every SECS seconds]:seconds:' \\
  '(-1 --once)'{-1,--once}'[render once and exit]' \\
  '--log[commit feed across every repo, newest first]:count:' \\
  '(-a --all)'{-a,--all}'[expand the QUIET group]' \\
  '*--root[where to look for repos]:directory:_files -/' \\
  '--depth[how deep to search below each root]:depth:' \\
  '*--repo[only repos whose name contains NAME]:name:' \\
  '--sort[sort within each group]:mode:(%s)' \\
  '--filter[show only this view]:mode:(%s)' \\
  '--check[exit 1 if any tree needs a human first]' \\
  '--fail-on[exit 1 on this condition]:condition:(none dirty diverged stuck)' \\
  '--json[emit records as JSON]' \\
  '--no-color[disable colour output]' \\
  '--github[add a PR/CI column via gh]' \\
  '--github-interval[PR/CI refresh interval in watch mode]:seconds:' \\
  '--print-completion[print a shell completion script]:shell:(bash zsh powershell)'
""" % (" ".join(SORT_MODES), " ".join(FILTER_MODES))
    if shell == "powershell":
        ps_flags = ", ".join("'%s'" % f for f in _completion_flag_words())
        ps_value_flags = ", ".join("'%s'" % f for f in sorted(_COMPLETION_VALUE_FLAGS))
        ps_choices = "; ".join(
            "'%s' = @(%s)" % (flag, ", ".join("'%s'" % w for w in choices.split()))
            for flag, choices in sorted(_COMPLETION_VALUE_FLAGS.items()) if choices)
        return """# PowerShell completion for git-roost. Add to your profile:
#   . (git-roost --print-completion powershell | Out-String | Invoke-Expression)
Register-ArgumentCompleter -Native -CommandName git-roost -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)
    $flags = @(%s)
    $valueFlags = @(%s)
    $choices = @{%s}
    $prev = $commandAst.CommandElements[
        [Math]::Max(0, $commandAst.CommandElements.Count - 2)].ToString()
    if ($valueFlags -contains $prev) {
        if ($choices.ContainsKey($prev)) {
            $choices[$prev] | Where-Object { $_ -like "$wordToComplete*" } |
                ForEach-Object {
                    [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
                }
        }
        return
    }
    $flags | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object {
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterName', $_)
    }
}
""" % (ps_flags, ps_value_flags, ps_choices)
    raise ValueError("unknown shell %r" % shell)


def main(argv=None):
    global COLOR

    ap = build_parser()
    args = ap.parse_args(argv)

    if args.print_completion:
        sys.stdout.write(print_completion(args.print_completion))
        return 0

    # TUI is the default on a real terminal. Scripts, pipes, --once and --json
    # stay one-shot so `git-roost | less` and the test harness never hang in
    # the watch loop. Clearing watch keeps scan()'s oneshot progress check
    # honest -- it keys off args.watch being unset.
    if args.once or args.json or not sys.stdout.isatty():
        args.watch = None

    # Escape sequences need a real terminal that understands them. This gates
    # the watch loop's cursor-home and erase as well as colour: on a Windows
    # console without VT processing those two are ignored rather than obeyed, so
    # every frame would append below the last and the screen pages away instead
    # of redrawing. That is exactly the "spamming my cmd" failure -- so watch
    # refuses to run without VT and falls back to one shot, rather than looping
    # a dump. Piping to a file has the same problem in a different form.
    ansi = sys.stdout.isatty() and enable_windows_ansi()
    if args.watch and not ansi:
        sys.stderr.write(
            "git-roost: no VT console (cannot redraw in place); "
            "rendering once. Use Windows Terminal, or pass --once.\n")
        args.watch = None

    # NO_COLOR is the community convention (https://no-color.org) and costs
    # nothing to honour. It suppresses colour only -- a user who wants plain
    # output still wants watch mode to redraw in place.
    COLOR = ansi and not args.no_color and not os.environ.get("NO_COLOR")
    if args.json:
        COLOR = False

    problems = root_problems(resolved_roots(args))
    if problems:
        for path, reason in problems:
            sys.stderr.write("git-roost: root %s: %s\n" % (path, reason))
        return 1

    if args.check:
        # A scripted gate, not a view of the table: watch mode and --filter
        # don't apply, because the question is binary and the exit code is the
        # answer. --repo/--root still scope the scan -- a hook checking one
        # tree before dispatching an agent into it should not have to reason
        # about the whole fleet.
        states = scan(args)
        offenders = [s for s in states if needs_attention(s)]
        if args.json:
            print(json.dumps(offenders, indent=2, sort_keys=True))
        elif not offenders:
            print("clean: no tree is mid-operation, diverged, or has uncommitted work")
        else:
            width = shutil.get_terminal_size((160, 24)).columns
            print("\n".join(render(offenders, width, filt="all")))
        return 1 if offenders else 0

    if not args.watch:
        width = shutil.get_terminal_size((160, 24)).columns
        lines, states = body(args, width)
        print("\n".join(lines))
        return exit_code(states, args.fail_on)

    # Live view state, separate from args: the flags are the starting position
    # and the keys move from there. --all seeds the quiet toggle, --sort/
    # --filter seed the sort/filter, and --log seeds the table/feed toggle, so
    # `-a -w` opens expanded and `a` still collapses it, and `--log -w` opens
    # on the feed and `l` still flips back.
    view = {
        "sort": args.sort, "filter": args.filter, "quiet": args.all,
        "log": args.log is not None, "log_limit": args.log if args.log is not None else 25,
        "cursor": 0, "detail": None,
    }
    helping = False
    last_states = []
    # Previous frame's (bucket, WORK, DRIFT) per tree path, for the "what
    # moved" highlight. Empty on the first frame, so nothing is marked changed
    # before there is anything to compare against.
    prev_sig = {}

    # GitHub data is cached across redraws and refreshed on its own, longer
    # interval (--github-interval, default 30s) rather than every redraw
    # (default 3s). The local git scan is fast and local; `gh` is neither --
    # it is a network call subject to GitHub's own rate limits, so re-fetching
    # it every frame would be both slow and a good way to get throttled for no
    # benefit, since a PR's review state rarely changes inside a 3s window.
    github_map = {}
    last_gh_fetch = 0.0

    try:
        if ansi:
            # Alternate screen + hidden cursor: a 85-row dump must not become
            # scrollback the operator is stuck at the bottom of, with [? keys]
            # sitting above the fold.
            sys.stdout.write("\033[?1049h\033[?25l")
            sys.stdout.flush()
        with Keys() as keys:
            shown = []
            while True:
                size = shutil.get_terminal_size((160, 24))
                width = size.columns
                height = max(8, size.lines)
                spin_n = [0]
                last_paint = [0.0]

                def on_progress(done, total, ordered, paths):
                    now = time.time()
                    if done < total and now - last_paint[0] < 0.12:
                        return
                    last_paint[0] = now
                    loading = "scanning %d/%d %s" % (
                        done, total, SPIN[spin_n[0] % 4])
                    spin_n[0] += 1
                    if last_states:
                        # Keep the last grouped table; only the status line
                        # ticks. Redrawing the body every worker finish is
                        # what flashed and jumped.
                        if ansi:
                            rewrite_status(width, view, args, loading)
                        return
                    draw_watch_frame(
                        ansi, width, height, view, args, helping, [],
                        {}, loading=loading, pending=(paths, ordered))

                states = scan(
                    args, on_progress=None if args.json else on_progress)
                last_states = states

                if args.json:
                    shown = draw_watch_frame(
                        ansi, width, height, view, args, helping, states, {})
                else:
                    if args.github:
                        now = time.time()
                        due = now - last_gh_fetch >= args.github_interval or not github_map
                        if due:
                            github_map = github_facts_map(states)
                            last_gh_fetch = now
                        apply_github_facts(states, github_map)

                    cur_sig = {s["path"]: frame_signature(s) for s in states}
                    changed = {p for p, sig in cur_sig.items()
                               if p in prev_sig and prev_sig[p] != sig}
                    prev_sig = cur_sig
                    shown = draw_watch_frame(
                        ansi, width, height, view, args, helping, states,
                        changed)

                # The help overlay waits for a key rather than timing out under
                # the reader. A keymap that vanished after 3s would be gone at
                # exactly the moment someone was reading it.
                key = keys.wait(3600 if helping and keys.enabled else args.watch)
                if key is None:
                    continue
                if helping:
                    helping = False
                    continue
                # Detail is its own overlay, dismissed by any key -- same
                # pattern as help, one step above so apply_key never sees the
                # keypress that closes it.
                if view.get("detail") is not None:
                    view["detail"] = None
                    continue
                action = apply_key(view, key, shown=shown)
                if action == "quit":
                    break
                helping = action == "help"
    except KeyboardInterrupt:
        pass
    finally:
        if ansi:
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()
    return exit_code(last_states, args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
