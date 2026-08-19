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

    git-roost                # one table, most actionable first
    git-roost -w             # redraw every 3s (the top view)
    git-roost --log          # commit feed across every repo, newest first
    git-roost --all          # expand the QUIET group
    git-roost --json         # records, for piping somewhere else

Watch mode takes keys -- `?` for the map, `r` refresh, `s` sort, `f` filter, `a`
quiet, `l` toggles the table for the commit feed, `j`/`k` move a row cursor,
`enter` opens a detail view for the highlighted tree, `q` quit. The default
one-shot render takes none and touches no terminal settings at all, which is
what keeps it safe to pipe.

One file, no dependencies, Python 3.9+, macOS/Linux/Windows -- the same
constraints as roost, for the same reason: it has to run on whatever Python is
already on the box, including a bare system 3.9 on macOS.

Read-only by construction. Every git invocation goes through git(), which
refuses anything outside READ_ONLY -- so the tool cannot mutate a tree, an index
or a ref even if a future edit tries to. The allowlist is keyed on the
subcommand *and* its first argument, because `stash list` and `stash pop` are
not the same kind of thing. tests/test_git_roost.py asserts that policy directly.
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
from concurrent.futures import ThreadPoolExecutor
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
__version__ = "0.1.0"
# x-release-please-end

HOME = Path.home()

# ~/GitHub is where every repo on this machine lives. Overridable because that
# is a fact about one box, not about git -- GIT_ROOST_ROOT for the daily
# default (os.pathsep-separated for more than one root, matching PATH), --root
# for a one-off override.
_env_roots = os.environ.get("GIT_ROOST_ROOT")
if _env_roots:
    DEFAULT_ROOTS = tuple(Path(p).expanduser() for p in _env_roots.split(os.pathsep) if p)
else:
    DEFAULT_ROOTS = (HOME / "GitHub",)

# How deep to look for a repo below a root. Worktrees live at
# <root>/.worktrees/<repo>/<slug>, which is depth 3, so 3 is the floor and the
# default. Deeper costs a directory walk and finds mostly vendored junk.
DEFAULT_DEPTH = 3

# Never descend into these. A node_modules with its own .git is not a repo you
# are working in, and walking one costs more than the whole rest of the scan.
PRUNE = frozenset((
    "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "build", "dist", "target", ".next", ".cargo", "Library",
    ".Trash", "vendor", ".terraform",
))

# Worktrees can live inside the repo as well as beside it: ccwork puts them at
# <root>/.worktrees/, but Claude Code's own put them at <repo>/.claude/worktrees/.
# Finding a repo prunes the walk, so these two have to be descended explicitly or
# the second kind is invisible.
NESTED_WORKTREE_DIRS = (".worktrees", Path(".claude") / "worktrees")

GIT_TIMEOUT = float(os.environ.get("GIT_ROOST_TIMEOUT") or 5)
GIT_WORKERS = int(os.environ.get("GIT_ROOST_WORKERS") or 12)

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
    try:
        out = subprocess.run(
            ("git", "-C", str(dirpath)) + tuple(args),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
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


def collect(paths):
    # Repo-level facts are memoized within a scan and must not survive it: in
    # watch mode a stale cache would keep showing a stash that was just popped.
    with _repo_lock:
        _repo_cache.clear()
    if not paths:
        return []
    with ThreadPoolExecutor(max_workers=min(GIT_WORKERS, len(paths))) as pool:
        states = list(pool.map(tree_state, paths))
    return [s for s in states if s]


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


def render(states, width=160, expand_quiet=False, sort_mode="recent", filt="all",
           changed=None):
    """`changed` is a set of tree paths whose bucket, WORK or DRIFT differ from
    the previous watch-mode frame -- see frame_signature(). None outside watch
    mode, where there is no previous frame to compare against.
    """
    if not states:
        return ["no git repositories found"]

    fleet = states
    total = len(states)
    if filt != "all":
        states = [s for s in states if passes_filter(s, filt)]
        if not states:
            return ["no tree matches filter: %s" % FILTER_LABELS[filt]]

    rows = sorted(states, key=lambda st: sort_key(st, sort_mode))
    shown = [s for s in rows if bucket(s)[1] != "QUIET" or expand_quiet]
    quiet = [s for s in rows if bucket(s)[1] == "QUIET" and not expand_quiet]

    cells = [[fn(s) for _, fn in COLUMNS] for s in shown]
    headers = [h for h, _ in COLUMNS]
    widths = [
        max([len(headers[i])] + [row[i] and len(row[i]) or 0 for row in cells] or [0])
        for i in range(len(COLUMNS))
    ]

    used = sum(widths) + 2 * len(widths) + 2
    subject_w = max(20, width - used - 2)

    lines = []
    lines.append(c("  " + "  ".join(
        headers[i].ljust(widths[i]) for i in range(len(COLUMNS))
    ) + "  " + "SUBJECT", BOLD))

    current = None
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
        body = "  ".join(row[i].ljust(widths[i]) for i in range(len(COLUMNS)))
        is_changed = bool(changed) and st["path"] in changed
        marker = "* " if is_changed else "  "
        line = marker + body + "  " + subject
        if is_changed:
            line = c(line, MAGENTA, BOLD)
        lines.append(line)

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
    return lines


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


def render_log(states, limit, width=160):
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
        return ["no commits found"]

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
    return lines


def frame_signature(st):
    """What "changed since last frame" means: bucket, WORK and DRIFT.

    Not the whole state dict -- LAST ticks every second and would mark every
    row changed on every redraw, which is the opposite of a "what moved"
    signal.
    """
    return (bucket(st)[1], work(st), drift(st))


def detail_lines(st, width=160):
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
    return lines


# ------------------------------------------------------------------- watch keys

KEYMAP = (
    ("?", "this map"),
    ("r", "refresh now"),
    ("s", "sort: recent / repo / work (within a group, never across)"),
    ("f", "filter: all / uncommitted / mid-operation"),
    ("a", "expand or collapse QUIET"),
    ("l", "toggle table / commit feed"),
    ("j", "move the cursor down"),
    ("k", "move the cursor up"),
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


def status_line(sort_mode, filt, expand_quiet, interval, view_mode="table"):
    """The one line that says what view you are looking at.

    Without it a filtered table is indistinguishable from a fleet that happens
    to be quiet, which is the same failure the summary line guards against.
    `view_mode` is "table", "log" or "detail" -- `l` and `enter` change what is
    on screen independently of sort/filter/quiet, so it needs saying too.
    """
    bits = [
        time.strftime("git-roost  %H:%M:%S"),
        "view:%s" % view_mode,
        "sort:%s" % sort_mode,
        "filter:%s" % FILTER_LABELS[filt],
        "quiet:%s" % ("shown" if expand_quiet else "collapsed"),
        "%gs" % interval,
    ]
    return c("  ".join(bits), DIM) + c("   [?] keys", BOLD)


# ------------------------------------------------------------------------ cli

def scan(args):
    roots = [Path(r).expanduser() for r in args.root] if args.root else list(DEFAULT_ROOTS)
    return collect(discover(roots, args.depth))


def body(args, width, view=None, changed=None):
    """One frame plus the states it was built from. `view` is the live watch
    state; None means the flags alone.

    The one-shot path passes None and renders exactly what 0.1 rendered, plus
    whatever --sort/--filter were given -- those are static flags, not watch
    state, so they apply identically to the piped path. That is the contract
    behind `git-roost | less` and `git-roost --json | jq`: keys move the watch
    view and nothing else. States come back alongside the lines so --fail-on
    can inspect the fleet without a second scan.

    `changed` is forwarded to render() for the table view only -- see
    frame_signature(). The watch loop computes it from frame to frame; the
    one-shot path never has a previous frame to compare against.
    """
    states = scan(args)
    if args.json:
        return [json.dumps(states, indent=2, sort_keys=True)], states
    if view is None:
        if args.log is not None:
            return render_log(states, args.log, width), states
        return render(states, width, expand_quiet=args.all,
                      sort_mode=args.sort, filt=args.filter), states
    return render_view(states, width, view, changed), states


def render_view(states, width, view, changed=None):
    """Render one already-scanned watch-mode frame from the live view state.

    Split out of body() so the watch loop can reuse a single scan for both the
    rendered frame and the cursor's row list, instead of scanning twice a
    redraw.
    """
    if view.get("detail") is not None:
        target = view["detail"]
        # Watch mode rescans every interval; show the freshest state for that
        # tree rather than freezing the frame it was opened on. If the tree
        # has vanished (worktree removed mid-session), fall back to what was
        # last known about it rather than crashing.
        current = next((s for s in states if s["path"] == target["path"]), None)
        return detail_lines(current or target, width)
    if view.get("log"):
        limit = view.get("log_limit") or 25
        return render_log(states, limit, width)
    return render(states, width, expand_quiet=view["quiet"],
                  sort_mode=view["sort"], filt=view["filter"], changed=changed)


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


def main(argv=None):
    global COLOR

    ap = argparse.ArgumentParser(
        prog="git-roost",
        description="top for git -- every repo and worktree, most actionable first.",
        epilog=("watch-mode keys:  " + "  ".join(
            "%s %s" % (k, d.split(":")[0].split("(")[0].strip())
            for k, d in KEYMAP)),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version="git-roost %s" % __version__)
    ap.add_argument("-w", "--watch", nargs="?", const=3.0, type=float, metavar="SECS",
                    help="redraw every SECS seconds (default 3); takes keys, see below")
    ap.add_argument("-1", "--once", action="store_true",
                    help="render once and exit (the default; overrides --watch)")
    ap.add_argument("--log", nargs="?", const=25, type=int, metavar="N",
                    help="commit feed across every repo, newest first (default 25)")
    ap.add_argument("-a", "--all", action="store_true", help="expand the QUIET group")
    ap.add_argument("--root", action="append", metavar="DIR",
                    help="where to look for repos (repeatable; default $GIT_ROOST_ROOT or ~/GitHub)")
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH, metavar="N",
                    help="how deep to search below each root (default %d)" % DEFAULT_DEPTH)
    ap.add_argument("--sort", choices=SORT_MODES, default=SORT_MODES[0],
                    help="sort within each group (default %s)" % SORT_MODES[0])
    ap.add_argument("--filter", choices=FILTER_MODES, default=FILTER_MODES[0],
                    help="subtractive view of the table (default %s)" % FILTER_MODES[0])
    ap.add_argument("--fail-on", choices=("none", "dirty", "diverged", "stuck"),
                    default="none", metavar="COND",
                    help="exit 1 if the fleet has a tree matching COND: stuck "
                         "(mid-operation), diverged (mid-operation or ahead+behind "
                         "a base), dirty (also uncommitted work). Checked against "
                         "the whole fleet, not --filter. Default none (always 0).")
    ap.add_argument("--json", action="store_true", help="emit records as JSON")
    ap.add_argument("--no-color", action="store_true", help="disable colour output")
    args = ap.parse_args(argv)

    # Escape sequences need a real terminal that understands them. This gates
    # the watch loop's cursor-home and erase as well as colour: on a Windows
    # console without VT processing those two are ignored rather than obeyed, so
    # every frame appends below the last and the screen pages away instead of
    # redrawing. Piping to a file has the same problem in a different form.
    ansi = sys.stdout.isatty() and enable_windows_ansi()

    # NO_COLOR is the community convention (https://no-color.org) and costs
    # nothing to honour. It suppresses colour only -- a user who wants plain
    # output still wants watch mode to redraw in place.
    COLOR = ansi and not args.no_color and not os.environ.get("NO_COLOR")
    if args.json:
        COLOR = False

    if args.once or not args.watch:
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

    try:
        with Keys() as keys:
            while True:
                width = shutil.get_terminal_size((160, 24)).columns
                states = scan(args)
                last_states = states

                if args.json:
                    out = [json.dumps(states, indent=2, sort_keys=True)]
                    shown = []
                else:
                    cur_sig = {s["path"]: frame_signature(s) for s in states}
                    changed = {p for p, sig in cur_sig.items()
                               if p in prev_sig and prev_sig[p] != sig}
                    prev_sig = cur_sig
                    out = help_lines() if helping else render_view(states, width, view, changed)
                    shown = visible_rows(states, view["quiet"], view["sort"], view["filter"])

                if ansi:
                    sys.stdout.write("\033[H\033[2J")
                else:
                    # No VT support: a rule beats escape codes printed literally.
                    sys.stdout.write("\n" + "-" * min(width, 78) + "\n")
                view_mode = "detail" if view.get("detail") is not None else (
                    "log" if view.get("log") else "table")
                sys.stdout.write(status_line(
                    view["sort"], view["filter"], view["quiet"], args.watch,
                    view_mode) + "\n\n")
                sys.stdout.write("\n".join(out) + "\n")
                sys.stdout.flush()

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
    return exit_code(last_states, args.fail_on)


if __name__ == "__main__":
    sys.exit(main())
