#!/usr/bin/env python3
"""Stage a synthetic fleet of git repos for the git-roost demo recordings.

git-roost reads *real* repositories. Recording it against this machine would put
actual repo names, branch names and commit subjects into a public GIF, so the
demo gets its own throwaway fleet instead: nine invented repos plus two linked
worktrees, built from nothing under the system temp dir, with local bare repos
standing in for origins so "ahead" and "behind" are measured against a real
remote-tracking ref rather than faked.

Everything here is genuinely half-finished where it claims to be. git-roost
detects MID-OPERATION by looking for .git/rebase-merge and .git/MERGE_HEAD, so a
fixture that merely wrote those paths would be testing the fixture, not the tool
-- the rebase and the merge below are real conflicting operations that git
itself leaves stopped.

    python3 setup_fleet.py              # stage, print the root, exit
    python3 setup_fleet.py --live 60    # stage, then mutate for 60s (loop.tape)
    python3 setup_fleet.py --clean      # remove the fleet and exit

The fleet is left on disk when the script exits -- the recording needs it to
still be there. --clean removes it, and every run wipes the root before
rebuilding, so a stale fleet never accumulates.

Stdlib only, and no shell: the same 3.9+/Windows/macOS/Linux constraint the tool
itself carries, since the tapes are recorded on POSIX but the fixture is often
built and eyeballed on Windows.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The whole fleet lives under one root so cleanup is a single rmtree. /tmp on
# POSIX (which is what the tapes hardcode), %TEMP% on Windows.
ROOT = Path(os.environ.get("GIT_ROOST_DEMO_ROOT")
            or Path(tempfile.gettempdir()) / "git-roost-demo")

# The scan root handed to git-roost, and the bare "origins" it drifts against.
# They are siblings rather than nested: a bare repo has no .git entry so
# git-roost would not list it, but the scan would still walk every loose-object
# directory inside one, and there is no reason to pay for that.
FLEET = ROOT / "fleet"
ORIGINS = ROOT / "origins"

# Touched by the tapes at the moment recording starts, so --live's schedule is
# measured from the first visible frame rather than from process launch -- vhs
# spends an unpredictable second or two warming up the terminal.
#
# Deliberately a sibling of ROOT rather than a file inside it: build() rmtrees
# ROOT, and the tape fires `vhs loop.tape` right after backgrounding the stager,
# so a marker kept inside would be destroyed by a staging run still in progress
# and --live would then wait for a touch that already happened.
MARKER = ROOT.parent / (ROOT.name + "-go")

NOW = int(time.time())
HOUR = 3600
DAY = 86400

# A fictional outfit, deliberately. No name here corresponds to anything on the
# machine the GIF gets recorded on, which is the entire reason this file exists.
AUTHOR = "Fixture Bot"
EMAIL = "fixture@example.invalid"


# ------------------------------------------------------------------ git driver

def run(cwd, *args, check=True, when=None):
    """One git call in cwd. Returns the CompletedProcess.

    when= backdates both the author and the committer date. git-roost's ACTIVE
    bucket keys off the *committer* date (%ct), so setting only GIT_AUTHOR_DATE
    -- the usual reflex -- would leave every fixture commit stamped "now" and
    collapse ACTIVE and QUIET into a single indistinguishable group.
    """
    env = dict(os.environ)
    # A developer's own hooks, signing key, includeIf blocks and commit template
    # would all otherwise apply to the fixture, and each of them can break a
    # commit in a way that looks like a bug in this script.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if when is not None:
        stamp = "@%d +0000" % when
        env["GIT_AUTHOR_DATE"] = stamp
        env["GIT_COMMITTER_DATE"] = stamp
    proc = subprocess.run(
        ("git", "-C", str(cwd)) + tuple(str(a) for a in args),
        capture_output=True, text=True, env=env,
    )
    if check and proc.returncode != 0:
        raise RuntimeError("git %s failed in %s:\n%s%s"
                           % (" ".join(str(a) for a in args), cwd,
                              proc.stdout, proc.stderr))
    return proc


def rmtree(path):
    """rmtree that survives .git's read-only pack files on Windows."""
    def force(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    if not path.exists():
        return
    # onerror was deprecated in 3.12 in favour of onexc and the fixture has to
    # keep working on a bare 3.9, so pick the one this interpreter accepts.
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:
        shutil.rmtree(path, onerror=force)


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    # Explicit newline="\n": the tapes record on POSIX while the fixture is often
    # built on Windows, and a CRLF-vs-LF difference is enough to turn the two
    # deliberate one-line conflicts below into whole-file conflicts.
    with open(p, "w", newline="\n", encoding="utf-8") as fh:
        fh.write(text)
    return p


def configure(repo):
    run(repo, "config", "user.name", AUTHOR)
    run(repo, "config", "user.email", EMAIL)
    run(repo, "config", "commit.gpgsign", "false")
    run(repo, "config", "tag.gpgsign", "false")
    # Point hooks at a path that does not exist rather than trusting that the
    # machine has no global core.hooksPath. Someone else's pre-commit hook
    # running against a throwaway fixture is a bad afternoon.
    run(repo, "config", "core.hooksPath", str(ROOT / "no-hooks"))
    run(repo, "config", "core.autocrlf", "false")
    run(repo, "config", "gc.auto", "0")


def init_repo(path, bare=False):
    path.mkdir(parents=True, exist_ok=True)
    args = ["init", "-q"] + (["--bare"] if bare else [])
    run(path, *args)
    # Set the initial branch with symbolic-ref rather than `init -b`, which
    # needs git 2.28. git-roost runs on whatever git is already on the box, and
    # so should the thing that demonstrates it.
    run(path, "symbolic-ref", "HEAD", "refs/heads/main")
    if not bare:
        configure(path)
    return path


def commit(repo, subject, ago=0, files=None):
    for rel, text in (files or {}).items():
        write(repo, rel, text)
    run(repo, "add", "-A")
    run(repo, "commit", "-q", "-m", subject, when=NOW - ago)


def new_repo(name):
    """A working tree plus the bare origin it tracks."""
    origin = init_repo(ORIGINS / (name + ".git"), bare=True)
    work = init_repo(FLEET / name)
    run(work, "remote", "add", "origin", origin.as_posix())
    return work, origin


def publish(work):
    """Push main and record origin/HEAD.

    origin/HEAD is what git-roost's resolve_base() looks for when a branch has
    no upstream of its own -- which is every branch `git worktree add -b`
    creates. Without it the worktree rows show DRIFT '-' and file under QUIET no
    matter how far they have actually drifted, which is precisely the false-clean
    the tool exists to prevent. A fixture that skipped this would hide the bug
    class the tool was written for.
    """
    run(work, "push", "-q", "-u", "origin", "main")
    run(work, "remote", "set-head", "origin", "main")


# ---------------------------------------------------------------- the fleet

def build():
    rmtree(ROOT)
    FLEET.mkdir(parents=True)
    ORIGINS.mkdir(parents=True)

    build_tidewater()      # ACTIVE + a worktree stopped mid-rebase
    build_lantern()        # MID-OPERATION: merge halted on a conflict
    build_kestrel()        # DIVERGED
    build_driftwood()      # UNCOMMITTED + a stash
    build_pier_nine()      # UNPUSHED
    build_beacon()         # BEHIND
    build_quiet()          # QUIET x3, one of them with a dirty worktree


def build_tidewater():
    """Primary tree ACTIVE; a linked worktree stopped mid-rebase.

    This pair is the whole argument for the tool. The repo itself looks fine --
    clean, in sync, committed twenty minutes ago -- while a worktree hanging off
    it sits halted on a conflict nobody has looked at. `git status` run in the
    repo root says nothing at all about the second half of that.
    """
    work, _ = new_repo("tidewater-api")
    commit(work, "Add the tide table loader", ago=4 * DAY, files={
        "README.md": "# tidewater-api\n\nStation readings for the harbour.\n",
        # Every repo that hosts Claude Code worktrees carries this line, and it
        # is load-bearing here: without it the `git add -A` in the next commit
        # records the linked worktree as a gitlink, which then shows up forever
        # as one modified file and files the primary tree under UNCOMMITTED
        # instead of ACTIVE. Cost an hour the first time.
        ".gitignore": ".claude/worktrees/\n",
        "src/tides.py": "# tide sampling\nSAMPLE_INTERVAL_MINUTES = 30\n",
    })
    commit(work, "Cache the harbour station list", ago=2 * DAY, files={
        "src/stations.py": "STATIONS = ('pier-nine', 'salt-marsh', 'fogbank')\n",
    })
    publish(work)

    # Claude Code puts its worktrees at <repo>/.claude/worktrees/<slug>, which
    # is *inside* the repo -- finding a repo prunes the walk, so git-roost has
    # to descend there deliberately. The fixture covers it so a regression there
    # shows up as a missing row rather than as nothing at all.
    wt = work / ".claude" / "worktrees" / "rebase-pilot"
    run(work, "worktree", "add", "-q", "-b", "pilot/hourly-sampling", str(wt), "main")
    configure(wt)
    commit(wt, "Sample the pilot stations hourly", ago=3 * HOUR, files={
        "src/tides.py": "# tide sampling\nSAMPLE_INTERVAL_MINUTES = 60\n",
    })

    # Same line, different value, on main. That collision is what stops the
    # rebase; edits to different lines would merge cleanly and cost the row.
    commit(work, "Drop tide sampling to a quarter hour", ago=20 * 60, files={
        "src/tides.py": "# tide sampling\nSAMPLE_INTERVAL_MINUTES = 15\n",
    })
    run(work, "push", "-q", "origin", "main")

    run(wt, "rebase", "main", check=False)
    # If git managed to rebase cleanly the fixture is silently wrong and the
    # MID-OPERATION group is a row short, so fail loudly rather than record it.
    git_dir = Path(run(wt, "rev-parse", "--absolute-git-dir").stdout.strip())
    if not (git_dir / "rebase-merge").exists() and not (git_dir / "rebase-apply").exists():
        raise RuntimeError("rebase-pilot rebased cleanly; expected a conflict")


def build_lantern():
    """A merge stopped on an unresolved conflict, on the primary tree.

    The other half of MID-OPERATION: git-roost reads rebase state and merge
    state from different files, so one of each is the only way to know both
    paths still work.
    """
    work, _ = new_repo("lantern-ui")
    commit(work, "Ship the first status bar", ago=3 * DAY, files={
        "README.md": "# lantern-ui\n",
        "config.toml": 'palette = "midline"\n',
    })
    publish(work)

    run(work, "checkout", "-q", "-b", "topic/dusk-palette")
    commit(work, "Try the dusk palette on the status bar", ago=6 * HOUR, files={
        "config.toml": 'palette = "dusk"\n',
    })
    run(work, "checkout", "-q", "main")
    commit(work, "Pin the default palette to daybreak", ago=5 * HOUR, files={
        "config.toml": 'palette = "daybreak"\n',
    })
    run(work, "push", "-q", "origin", "main")

    run(work, "merge", "topic/dusk-palette", check=False)
    git_dir = Path(run(work, "rev-parse", "--absolute-git-dir").stdout.strip())
    if not (git_dir / "MERGE_HEAD").exists():
        raise RuntimeError("lantern-ui merged cleanly; expected a conflict")


def build_kestrel():
    """Ahead *and* behind -- the group that turns into a conflict later.

    Built by pushing a commit and then resetting past it: the remote-tracking
    ref keeps the pushed tip while the branch moves somewhere else, which is
    exactly the shape a force-push or a rebased upstream leaves behind. The
    counts come straight out of `status --branch`; nothing is simulated.
    """
    work, _ = new_repo("kestrel-daemon")
    commit(work, "Add the watchdog poll loop", ago=5 * DAY, files={
        "README.md": "# kestrel-daemon\n",
        "daemon.py": "POLL_SECONDS = 5\n",
    })
    publish(work)

    commit(work, "Retire the legacy poll loop", ago=2 * DAY, files={
        "daemon.py": "POLL_SECONDS = 5\nLEGACY = False\n",
    })
    run(work, "push", "-q", "origin", "main")
    run(work, "reset", "-q", "--hard", "HEAD~1")

    commit(work, "Add a jitter window to the watchdog", ago=9 * HOUR, files={
        "daemon.py": "POLL_SECONDS = 5\nJITTER_SECONDS = 2\n",
    })
    commit(work, "Log why the daemon last restarted", ago=7 * HOUR, files={
        "restart.py": "REASONS = ('signal', 'watchdog', 'config-reload')\n",
    })


def build_driftwood():
    """Uncommitted tracked work, untracked scratch, and a stash on the shelf."""
    work, _ = new_repo("driftwood-cli")
    commit(work, "Add the argument parser", ago=6 * DAY, files={
        "README.md": "# driftwood-cli\n",
        "cli.py": "COMMANDS = ('scan', 'render')\n",
        "flags.py": "FLAGS = ('--verbose',)\n",
    })
    publish(work)

    # Stash first, then re-dirty. `stash push` takes the working tree back to
    # HEAD, so doing these in the other order would leave the tree clean and
    # cost the UNCOMMITTED row this repo is here to provide.
    write(work, "cli.py", "COMMANDS = ('scan', 'render', 'format')\n")
    run(work, "stash", "push", "-q", "-m", "wip: half-finished --format flag")

    write(work, "cli.py", "COMMANDS = ('scan', 'render', 'watch')\n")
    write(work, "flags.py", "FLAGS = ('--verbose', '--watch')\n")
    write(work, "notes.local.md", "scratch, do not commit\n")
    write(work, "out.log", "run 41: ok\n")


def build_pier_nine():
    """Ahead only: three commits nobody has pushed."""
    work, _ = new_repo("pier-nine")
    commit(work, "Record the berth layout", ago=8 * DAY, files={
        "README.md": "# pier-nine\n",
        "berths.csv": "id,length\n1,12\n2,18\n",
    })
    publish(work)

    for i, (subject, ago) in enumerate((
            ("Add the tender berth to the layout", 5 * HOUR),
            ("Widen berth 2 for the survey boat", 4 * HOUR),
            ("Note the winter haul-out schedule", 3 * HOUR))):
        commit(work, subject, ago=ago, files={
            "berths.csv": "id,length\n1,12\n2,%d\n3,9\n" % (18 + i),
        })


def build_beacon():
    """Behind only: origin moved on and this tree has not caught up."""
    work, _ = new_repo("beacon-sim")
    commit(work, "Add the lamp duty-cycle model", ago=9 * DAY, files={
        "README.md": "# beacon-sim\n",
        "lamp.py": "DUTY_CYCLE = 0.4\n",
    })
    publish(work)

    for i in range(4):
        commit(work, "Tune the duty cycle, pass %d" % (i + 1), ago=(6 - i) * HOUR,
               files={"lamp.py": "DUTY_CYCLE = 0.%d\n" % (5 + i)})
    run(work, "push", "-q", "origin", "main")
    # The push left origin/main four ahead; walking the branch back is what
    # leaves this tree BEHIND without having to touch the remote again.
    run(work, "reset", "-q", "--hard", "HEAD~4")


def build_quiet():
    """Three trees that deserve to be collapsed -- plus one that does not.

    fogbank is the point of the group. The repo itself is quiet and in sync, so
    the collapsed QUIET line tells the truth about it, while a worktree hanging
    off it carries uncommitted work and earns its own row above. A per-repo tool
    can show one of those two facts at a time.
    """
    for name, subject, ago, extra in (
            ("salt-marsh-docs", "Rewrite the survey methodology page", 3 * DAY,
             {"docs/method.md": "# Method\n\nWade out at low water.\n"}),
            ("oyster-cache", "Expire cache entries by tide window", 6 * DAY,
             {"cache.py": "TTL_SECONDS = 900\n"}),
            ("fogbank", "Import the 1998 visibility log", 2 * DAY,
             {"visibility.csv": "date,metres\n1998-01-04,120\n"})):
        work, _ = new_repo(name)
        files = {"README.md": "# %s\n" % name}
        files.update(extra)
        commit(work, subject, ago=ago, files=files)
        publish(work)

    # ccwork-style worktrees live beside the repos at
    # <root>/.worktrees/<repo>/<slug> -- the other of the two layouts git-roost
    # knows about, and the one that depends on the default depth of 3. Covering
    # both is the only way a regression in either shows up here.
    fog = FLEET / "fogbank"
    wt = FLEET / ".worktrees" / "fogbank" / "field-notes"
    run(fog, "worktree", "add", "-q", "-b", "survey/field-notes", str(wt), "main")
    configure(wt)
    write(wt, "visibility.csv", "date,metres\n1998-01-04,120\n1998-01-05,40\n")
    write(wt, "field-notes.txt", "bank rolled in around 0600\n")


# -------------------------------------------------------------------- live

def live(seconds):
    """Walk one repo up through the groups while loop.tape records.

    The point of the watch view is that rows *move*, and a fleet that only ever
    holds still cannot show that. salt-marsh-docs starts collapsed under QUIET
    and climbs QUIET -> UNCOMMITTED -> UNPUSHED -> ACTIVE, one transition every
    eight seconds, while pier-nine picks up dirty files on the way past so a
    second row jumps groups too.
    """
    docs = FLEET / "salt-marsh-docs"
    pier = FLEET / "pier-nine"

    print("waiting for %s (the tapes touch it when recording starts)" % MARKER)
    deadline = time.time() + 120
    while not MARKER.exists() and time.time() < deadline:
        time.sleep(0.25)
    start = time.time()

    steps = (
        (5, "dirty the docs tree",
         lambda: write(docs, "docs/method.md",
                       "# Method\n\nWade out at low water, twice.\n")),
        (13, "commit it", lambda: commit(docs, "Sample the flats twice per tide")),
        (21, "push it", lambda: run(docs, "push", "-q", "origin", "main")),
        (29, "dirty pier-nine",
         lambda: write(pier, "berths.csv", "id,length\n1,14\n2,20\n3,9\n")),
    )
    for at, label, action in steps:
        wait = start + at - time.time()
        if wait > 0:
            time.sleep(wait)
        action()
        print("  t+%2ds  %s" % (at, label))

    remaining = start + seconds - time.time()
    if remaining > 0:
        time.sleep(remaining)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clean", action="store_true",
                    help="remove the staged fleet and exit")
    ap.add_argument("--live", nargs="?", const=60.0, type=float, metavar="SECS",
                    help="after staging, mutate the fleet for SECS so the watch "
                         "view has something to re-sort (default 60)")
    args = ap.parse_args(argv)

    if args.clean:
        rmtree(ROOT)
        if MARKER.exists():
            MARKER.unlink()
        print("removed", ROOT)
        return 0

    if shutil.which("git") is None:
        print("git is not on PATH", file=sys.stderr)
        return 1

    # Cleared before staging, not after: staging takes a few seconds and the
    # tape may well touch the marker inside that window. Clearing afterwards
    # would throw that touch away and hang --live for its full timeout.
    if args.live is not None and MARKER.exists():
        MARKER.unlink()

    build()
    print("fleet staged at", FLEET)
    print()
    print("  git-roost --root %s --all" % FLEET)
    print()
    print("left on disk deliberately -- the recording needs it to still be "
          "there. --clean removes it, and every run wipes the root first.")

    if args.live is not None:
        live(args.live)
    return 0


if __name__ == "__main__":
    sys.exit(main())
