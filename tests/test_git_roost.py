"""Tests for git-roost.

Written against stdlib unittest so `python -m unittest` works with nothing
installed; pytest collects them unchanged in CI.

The bar for a test here: it must be able to fail. Several of these encode
mistakes made while writing the tool -- untracked-only trees being counted as
uncommitted work, worktrees of one repo being reported as separate repos, and
the ahead/behind fallback that silently calls unpushed branches clean.

The read-only tests are the important ones. This tool runs unattended in a watch
loop across every repo on the machine; the guarantee that it cannot write is the
only thing making that safe, so it is asserted rather than assumed.
"""

import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("git_roost", str(ROOT / "git_roost.py"))
git_roost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(git_roost)


HAVE_GIT = bool(__import__("shutil").which("git"))
needs_git = unittest.skipUnless(HAVE_GIT, "git is not installed")


def run(cwd, *args):
    subprocess.run(
        ("git",) + args, cwd=str(cwd), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def make_repo(path, commit=True):
    """A real repo, because the parsing this tool does is git's output format."""
    path.mkdir(parents=True, exist_ok=True)
    run(path, "init", "-b", "main")
    run(path, "config", "user.name", "Test")
    run(path, "config", "user.email", "test@example.com")
    if commit:
        (path / "README.md").write_text("hello\n")
        run(path, "add", "README.md")
        run(path, "commit", "-m", "initial")
    return path


class TestReadOnlyGuard(unittest.TestCase):
    """The tool must not be able to mutate a repo, even by future accident."""

    def test_write_subcommands_raise(self):
        for verb in ("commit", "checkout", "push", "reset", "clean", "rebase",
                     "merge", "add", "rm", "gc", "fetch", "pull", "switch"):
            with self.assertRaises(git_roost.NotReadOnly, msg=verb):
                git_roost.git(".", verb, "--whatever")

    def test_empty_args_raise(self):
        with self.assertRaises(git_roost.NotReadOnly):
            git_roost.git(".")

    def test_whitelist_contains_no_writing_verbs(self):
        # A reviewer adding a subcommand should have to think about this.
        forbidden = {"commit", "checkout", "push", "fetch", "pull", "reset",
                     "clean", "merge", "rebase", "add", "rm", "gc", "prune",
                     "switch", "restore", "cherry-pick", "revert", "apply"}
        self.assertEqual(forbidden & set(git_roost.READ_ONLY), set())

    def test_dangerous_forms_of_permitted_subcommands_are_refused(self):
        # The subcommand alone does not settle it. Every pair below shares its
        # first word with a legitimate read-only call, and each of these three
        # slipped through a subcommand-level allowlist: stash pop mutates the
        # working tree, stash clear destroys data, config <k> <v> writes
        # .git/config, and symbolic-ref with a value rewrites HEAD.
        refused = [
            ("stash", "pop"),
            ("stash", "clear"),
            ("stash", "drop"),
            ("stash", "push"),
            ("stash",),
            ("config", "user.email", "evil@example.com"),
            ("config", "--unset", "user.email"),
            ("config",),
            ("symbolic-ref", "HEAD", "refs/heads/other"),
            ("remote", "add", "evil", "https://example.com"),
            ("remote", "remove", "origin"),
        ]
        for args in refused:
            with self.assertRaises(git_roost.NotReadOnly, msg=" ".join(args)):
                git_roost.git("/tmp", *args)

    def test_a_leading_safe_flag_does_not_launder_a_write(self):
        # Checking the first argument alone is not enough either. Each of these
        # opens with a token the allowlist accepts and then writes anyway:
        # `symbolic-ref --short HEAD <ref>` rewrites HEAD -- verified doing
        # exactly that against a scratch repo -- and a config scope flag shifts
        # the key and value one position right, past a fixed-position check.
        for args in [
            ("symbolic-ref", "--short", "HEAD", "refs/heads/other"),
            ("config", "--local", "core.hooksPath", "/tmp"),
            ("config", "--global", "user.email", "evil@example.com"),
            ("config", "--get", "core.hooksPath", "/tmp"),
        ]:
            with self.assertRaises(git_roost.NotReadOnly, msg=" ".join(args)):
                git_roost.git("/tmp", *args)

    @needs_git
    def test_symbolic_ref_write_form_is_refused_in_practice(self):
        # The regression this encodes: the write form left HEAD pointing at a
        # different branch with no working-tree change at all, so nothing
        # downstream would have noticed.
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            before = subprocess.run(
                ("git", "symbolic-ref", "HEAD"), cwd=str(repo),
                capture_output=True, text=True).stdout.strip()
            with self.assertRaises(git_roost.NotReadOnly):
                git_roost.git(repo, "symbolic-ref", "--short", "HEAD",
                              "refs/heads/other")
            after = subprocess.run(
                ("git", "symbolic-ref", "HEAD"), cwd=str(repo),
                capture_output=True, text=True).stdout.strip()
            self.assertEqual(before, after)

    def test_safe_forms_of_those_subcommands_are_permitted(self):
        for args in [
            ("stash", "list"),
            ("config", "--get", "user.email"),
            ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"),
            ("remote",),
            ("rev-parse", "--show-toplevel"),
            ("log", "-1"),
        ]:
            git_roost.check_read_only(args)  # must not raise

    @needs_git
    def test_reading_a_repo_leaves_it_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            before = subprocess.run(
                ("git", "status", "--porcelain=v2", "--branch"), cwd=str(repo),
                capture_output=True, text=True).stdout
            head_before = subprocess.run(
                ("git", "rev-parse", "HEAD"), cwd=str(repo),
                capture_output=True, text=True).stdout

            git_roost.tree_state(repo)

            after = subprocess.run(
                ("git", "status", "--porcelain=v2", "--branch"), cwd=str(repo),
                capture_output=True, text=True).stdout
            head_after = subprocess.run(
                ("git", "rev-parse", "HEAD"), cwd=str(repo),
                capture_output=True, text=True).stdout
            self.assertEqual(before, after)
            self.assertEqual(head_before, head_after)


class TestPorcelainParse(unittest.TestCase):
    def test_branch_and_counts(self):
        text = "\n".join((
            "# branch.oid abc123",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +2 -3",
            "1 .M N... 100644 100644 100644 aaa bbb file_one.py",
            "2 R. N... 100644 100644 100644 ccc ddd R100 new.py\told.py",
            "? scratch.txt",
            "? other.txt",
        ))
        out = git_roost.parse_porcelain_v2(text)
        self.assertEqual(out["branch"], "main")
        self.assertFalse(out["detached"])
        self.assertEqual(out["upstream"], "origin/main")
        self.assertEqual((out["ahead"], out["behind"]), (2, 3))
        self.assertEqual(out["tracked"], 2)
        self.assertEqual(out["untracked"], 2)
        self.assertEqual(out["conflicts"], 0)

    def test_detached_head(self):
        out = git_roost.parse_porcelain_v2("# branch.head (detached)")
        self.assertTrue(out["detached"])
        self.assertEqual(out["branch"], "")

    def test_unmerged_paths_count_as_conflicts_and_as_tracked(self):
        text = "\n".join((
            "# branch.head main",
            "u UU N... 100644 100644 100644 100644 a b c conflicted.py",
        ))
        out = git_roost.parse_porcelain_v2(text)
        self.assertEqual(out["conflicts"], 1)
        self.assertEqual(out["tracked"], 1)

    def test_staged_and_unstaged_split_and_a_file_can_be_both(self):
        text = "\n".join((
            "# branch.head main",
            "1 M. N... 100644 100644 100644 aaa bbb staged_only.py",
            "1 .M N... 100644 100644 100644 aaa bbb unstaged_only.py",
            "1 MM N... 100644 100644 100644 aaa bbb partially_staged.py",
            "2 R. N... 100644 100644 100644 ccc ddd R100 new.py\told.py",
        ))
        out = git_roost.parse_porcelain_v2(text)
        self.assertEqual(out["tracked"], 4)
        self.assertEqual(out["staged"], 3)     # staged_only + partial + rename
        self.assertEqual(out["unstaged"], 2)   # unstaged_only + partial

    def test_no_upstream_leaves_ahead_behind_unknown(self):
        # Not zero -- unknown. Zero would render as "=", claiming it is in sync.
        out = git_roost.parse_porcelain_v2("# branch.head feat/x")
        self.assertIsNone(out["ahead"])
        self.assertIsNone(out["behind"])

    def test_none_input_is_survivable(self):
        out = git_roost.parse_porcelain_v2(None)
        self.assertEqual(out["tracked"], 0)


class TestFormatting(unittest.TestCase):
    def test_work_untracked_only_is_marked_not_counted_as_dirty(self):
        st = {"tracked": 0, "untracked": 4}
        self.assertEqual(git_roost.work(st), "+4?")

    def test_work_clean(self):
        self.assertEqual(git_roost.work({"tracked": 0, "untracked": 0}), "clean")

    def test_work_both(self):
        self.assertEqual(git_roost.work({"tracked": 3, "untracked": 2}), "3+2?")

    def test_drift_states(self):
        base = {"base": "origin/main"}
        self.assertEqual(git_roost.drift(dict(base, ahead=0, behind=0)), "=")
        self.assertEqual(git_roost.drift(dict(base, ahead=2, behind=0)), "^2")
        self.assertEqual(git_roost.drift(dict(base, ahead=0, behind=3)), "v3")
        self.assertEqual(git_roost.drift(dict(base, ahead=2, behind=3)), "^2v3")

    def test_drift_unknown_is_a_dash_not_a_zero(self):
        self.assertEqual(git_roost.drift({"base": "", "ahead": None}), "-")
        self.assertEqual(git_roost.drift({"base": "origin/main", "ahead": None}), "-")

    def test_dur(self):
        self.assertEqual(git_roost.dur(30), "30s")
        self.assertEqual(git_roost.dur(90), "1m")
        self.assertEqual(git_roost.dur(7200), "2h")
        self.assertEqual(git_roost.dur(90000), "1d")
        self.assertEqual(git_roost.dur(None), "-")

    def test_ascii_safe_replaces_unrenderable(self):
        self.assertEqual(git_roost.ascii_safe("a—b"), "a?b")


def state(**kw):
    base = {
        "operation": "", "ahead": 0, "behind": 0, "tracked": 0,
        "untracked": 0, "last_ts": None, "repo": "r", "tree": "t",
    }
    base.update(kw)
    return base


class TestBuckets(unittest.TestCase):
    def test_order_is_by_cost_of_ignoring(self):
        now = 1000000.0
        cases = [
            (state(operation="rebase"), "MID-OPERATION"),
            (state(ahead=1, behind=2), "DIVERGED"),
            (state(tracked=3), "UNCOMMITTED"),
            (state(ahead=2), "UNPUSHED"),
            (state(behind=2), "BEHIND"),
            (state(last_ts=now - 60), "ACTIVE"),
            (state(last_ts=now - 99999), "QUIET"),
        ]
        for st, label in cases:
            self.assertEqual(git_roost.bucket(st, now)[1], label)

    def test_mid_operation_outranks_diverged(self):
        now = 1000000.0
        stuck = git_roost.bucket(state(operation="merge", ahead=1, behind=1), now)
        div = git_roost.bucket(state(ahead=1, behind=1), now)
        self.assertLess(stuck[0], div[0])

    def test_untracked_only_is_not_uncommitted(self):
        # 12 permanent scratch files should not sit in the alarming group.
        now = 1000000.0
        self.assertNotEqual(
            git_roost.bucket(state(untracked=12, last_ts=now - 99999), now)[1],
            "UNCOMMITTED",
        )


class TestNeedsAttention(unittest.TestCase):
    """The threshold --check gates on: exactly the three costliest groups."""

    def test_the_three_costliest_groups_need_attention(self):
        now = 1000000.0
        cases = [
            state(operation="rebase"),
            state(ahead=1, behind=2),
            state(tracked=3),
        ]
        for st in cases:
            self.assertTrue(git_roost.needs_attention(st))

    def test_unpushed_behind_active_and_quiet_do_not(self):
        now = 1000000.0
        cases = [
            state(ahead=2),
            state(behind=2),
            state(last_ts=now - 60),
            state(last_ts=now - 99999),
        ]
        for st in cases:
            self.assertFalse(git_roost.needs_attention(st))


class TestDiscover(unittest.TestCase):
    def test_finds_repos_and_prunes_vendored_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha" / ".git").mkdir(parents=True)
            (root / "beta" / ".git").mkdir(parents=True)
            # A vendored repo inside node_modules is not a tree you work in.
            (root / "alpha" / "node_modules" / "dep" / ".git").mkdir(parents=True)

            found = {p.name for p in git_roost.discover([root], depth=3)}
            self.assertEqual(found, {"alpha", "beta"})

    def test_finds_worktrees_nested_inside_a_repo(self):
        # Claude Code puts worktrees at <repo>/.claude/worktrees/<slug>. Finding
        # the repo prunes the walk, so these are invisible unless descended into.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha" / ".git").mkdir(parents=True)
            (root / "alpha" / ".claude" / "worktrees" / "slug").mkdir(parents=True)
            (root / "alpha" / ".claude" / "worktrees" / "slug" / ".git").touch()

            found = {p.name for p in git_roost.discover([root], depth=3)}
            self.assertIn("slug", found)

    def test_finds_worktrees_beside_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha" / ".git").mkdir(parents=True)
            wt = root / ".worktrees" / "alpha" / "feat-x"
            wt.mkdir(parents=True)
            (wt / ".git").touch()

            found = {p.name for p in git_roost.discover([root], depth=3)}
            self.assertIn("feat-x", found)

    def test_missing_root_is_not_an_error(self):
        self.assertEqual(git_roost.discover([Path("/nonexistent-xyz")]), [])


class TestTreeState(unittest.TestCase):
    @needs_git
    def test_clean_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            st = git_roost.tree_state(repo)
            self.assertEqual(st["branch"], "main")
            self.assertEqual(st["tracked"], 0)
            self.assertEqual(st["untracked"], 0)
            self.assertEqual(st["tree"], "(primary)")
            self.assertEqual(st["repo"], "r")
            self.assertTrue(st["last_subject"])

    @needs_git
    def test_untracked_and_modified_are_counted_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "README.md").write_text("changed\n")
            (repo / "scratch.txt").write_text("x\n")
            st = git_roost.tree_state(repo)
            self.assertEqual(st["tracked"], 1)
            self.assertEqual(st["untracked"], 1)

    @needs_git
    def test_worktrees_share_the_repo_identity_of_their_parent(self):
        # A repo and its worktrees are one project. Reporting them as separate
        # repos double-counts every commit in the feed.
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            wt = Path(tmp) / "wt"
            run(repo, "worktree", "add", "-b", "feat/x", str(wt))

            a = git_roost.tree_state(repo)
            b = git_roost.tree_state(wt)
            self.assertEqual(a["common_dir"], b["common_dir"])
            self.assertEqual(a["repo"], b["repo"])
            self.assertEqual(b["tree"], "wt")
            self.assertEqual(b["branch"], "feat/x")

    @needs_git
    def test_detached_head_reports_a_sha_not_a_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            head = subprocess.run(
                ("git", "rev-parse", "HEAD"), cwd=str(repo),
                capture_output=True, text=True).stdout.strip()
            run(repo, "checkout", "--detach", head)
            st = git_roost.tree_state(repo)
            self.assertTrue(st["detached"])
            self.assertTrue(head.startswith(st["branch"]))

    @needs_git
    def test_merge_in_progress_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "f.txt").write_text("base\n")
            run(repo, "add", "f.txt")
            run(repo, "commit", "-m", "base")

            run(repo, "checkout", "-b", "side")
            (repo / "f.txt").write_text("side\n")
            run(repo, "add", "f.txt")
            run(repo, "commit", "-m", "side")

            run(repo, "checkout", "main")
            (repo / "f.txt").write_text("main\n")
            run(repo, "add", "f.txt")
            run(repo, "commit", "-m", "main")

            # Expected to conflict; that is the state under test.
            subprocess.run(("git", "merge", "side"), cwd=str(repo),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            st = git_roost.tree_state(repo)
            self.assertEqual(st["operation"], "merge")
            self.assertGreaterEqual(st["conflicts"], 1)
            self.assertEqual(git_roost.bucket(st)[1], "MID-OPERATION")

    @needs_git
    def test_drift_is_found_when_the_only_remote_is_not_called_origin(self):
        # ~/GitHub/blog and its worktrees have exactly one remote, named
        # "deploy". An origin-only lookup rendered a tree nine commits behind as
        # "-" and filed it under QUIET.
        with tempfile.TemporaryDirectory() as tmp:
            upstream = make_repo(Path(tmp) / "up")
            clone = Path(tmp) / "clone"
            subprocess.run(
                ("git", "clone", "--origin", "deploy", str(upstream), str(clone)),
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            run(clone, "config", "user.name", "Test")
            run(clone, "config", "user.email", "test@example.com")
            # Move the remote ahead, then drop the local upstream link so the
            # only route to a baseline is the remote-name fallback.
            (upstream / "next.txt").write_text("x\n")
            run(upstream, "add", "next.txt")
            run(upstream, "commit", "-m", "second")
            run(clone, "fetch", "deploy")
            run(clone, "branch", "--unset-upstream")

            st = git_roost.tree_state(clone)
            self.assertTrue(st["base"], "no baseline found for a non-origin remote")
            self.assertEqual(st["behind"], 1)
            self.assertEqual(git_roost.bucket(st)[1], "BEHIND")

    @needs_git
    def test_two_remotes_without_origin_head_render_unknown_not_a_guess(self):
        # A confident wrong baseline is worse than admitting we do not know.
        with tempfile.TemporaryDirectory() as tmp:
            upstream = make_repo(Path(tmp) / "up")
            other = make_repo(Path(tmp) / "other")
            clone = Path(tmp) / "clone"
            subprocess.run(
                ("git", "clone", "--origin", "deploy", str(upstream), str(clone)),
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            run(clone, "remote", "add", "backup", str(other))
            run(clone, "branch", "--unset-upstream")

            st = git_roost.tree_state(clone)
            self.assertEqual(st["base"], "")
            self.assertEqual(git_roost.drift(st), "-")

    @needs_git
    def test_not_a_repo_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(git_roost.tree_state(Path(tmp)))


class TestRender(unittest.TestCase):
    def test_empty_scan_says_so_rather_than_crashing(self):
        self.assertEqual(git_roost.render([]), ["no git repositories found"])

    def test_clip_to_height_keeps_header_and_summary(self):
        lines = ["HEAD"] + ["r%d" % i for i in range(20)] + ["SUM"]
        out = git_roost.clip_to_height(lines, 6, focus=1)
        self.assertEqual(len(out), 6)
        self.assertEqual(out[0], "HEAD")
        self.assertEqual(out[-1], "SUM")
        self.assertTrue(any("more below" in ln for ln in out))

    def test_clip_to_height_is_a_noop_when_the_frame_fits(self):
        lines = ["a", "b", "c"]
        self.assertEqual(git_roost.clip_to_height(lines, 10, 0), lines)

    def test_render_pending_keeps_discover_order(self):
        paths = [Path("/tmp/alpha"), Path("/tmp/beta")]
        empty = "\n".join(git_roost.render_pending(paths, [None, None], width=120))
        self.assertIn("alpha", empty)
        self.assertIn("...", empty)
        filled = git_roost.render_pending(
            paths, [None, renderable(repo="beta", tree="(primary)", last_ts=1)],
            width=120)
        text = "\n".join(filled)
        self.assertLess(text.find("alpha"), text.find("beta"))
        self.assertIn("0 of 2", empty)
        self.assertTrue(any("1 of 2" in ln for ln in filled))

    def test_watch_height_does_not_dump_the_whole_fleet(self):
        rows = [renderable(repo="r%02d" % i, last_ts=1) for i in range(40)]
        out = git_roost.render(rows, width=200, expand_quiet=True, height=12, cursor=0)
        self.assertLessEqual(len(out), 12)
        self.assertTrue(any("40 tree(s)" in ln for ln in out))

    def test_cursor_row_is_marked(self):
        rows = [renderable(repo="aa", last_ts=1), renderable(repo="bb", last_ts=1)]
        out = "\n".join(git_roost.render(rows, width=200, expand_quiet=True, cursor=1))
        self.assertIn("> ", out)

    def test_empty_fleet_names_the_root_and_the_next_command(self):
        missing = Path("/no-such-git-roost-root")
        lines = git_roost.empty_fleet_lines([missing], depth=3)
        text = "\n".join(lines)
        self.assertEqual(lines[0], "no git repositories found")
        self.assertIn(str(missing), text)
        self.assertIn("does not exist", text)
        self.assertIn("Point git-roost at a folder of checkouts", text)
        self.assertIn("--root", text)
        self.assertIn("GIT_ROOST_ROOT", text)

    def test_empty_result_distinguishes_a_repo_filter_miss(self):
        class Args:
            root = [str(Path("/tmp"))]
            depth = 3
            repo = ["nope"]
        self.assertEqual(
            git_roost.empty_result_lines(Args()),
            ["no repo matches: nope"],
        )

    @needs_git
    def test_table_contains_every_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_repo(root / "alpha")
            make_repo(root / "beta")
            states = git_roost.collect(git_roost.discover([root]))
            out = "\n".join(git_roost.render(states, width=200, expand_quiet=True))
            self.assertIn("alpha", out)
            self.assertIn("beta", out)
            self.assertIn("2 tree(s) across 2 repo(s)", out)


def trees(text):
    """The tree records out of a --json capture, whichever shape it is.

    The default is the {schema, version, trees} envelope; --legacy-json is
    still the bare list. Tests that only care about the records go through
    this; the envelope itself has its own dedicated assertions.
    """
    data = json.loads(text)
    return data["trees"] if isinstance(data, dict) else data


def renderable(**kw):
    """A state complete enough for render(), which needs more than bucket() does."""
    base = dict(state(), branch="main", detached=False, base="origin/main",
                stashes=0, last_subject="s", conflicts=0, common_dir="/c/%s" % kw.get("repo", "r"),
                path="/p/%s" % kw.get("tree", "t"))
    base.update(kw)
    base["common_dir"] = "/c/%s" % base["repo"]
    return base


class TestWatchResize(unittest.TestCase):
    """A window resize is relayout + repaint of the cached scan, never git.

    The watch loop scans on its own clock (next_watch_event's deadline) and
    treats a size change as its own event. These pin the two halves of that:
    the resize event fires without waiting out the interval, and the repaint
    path can run with subprocess disabled entirely.
    """

    class NoKeys:
        enabled = False

        def wait(self, timeout):
            # No sleeping in tests: pretend the slice elapsed keylessly.
            return None

    def test_resize_is_reported_before_the_scan_tick(self):
        sizes = iter([os.terminal_size((100, 30))])
        event = git_roost.next_watch_event(
            self.NoKeys(), deadline=time.time() + 3600,
            size=os.terminal_size((160, 24)),
            get_size=lambda fallback: next(sizes))
        self.assertEqual(event, ("resize", os.terminal_size((100, 30))))

    def test_tick_fires_when_the_scan_interval_has_elapsed(self):
        event = git_roost.next_watch_event(
            self.NoKeys(), deadline=time.time() - 1,
            size=os.terminal_size((160, 24)),
            get_size=lambda fallback: os.terminal_size((160, 24)))
        self.assertEqual(event, ("tick", None))

    def test_key_wins_over_an_unchanged_size(self):
        class OneKey:
            enabled = True

            def wait(self, timeout):
                return "j"

        event = git_roost.next_watch_event(
            OneKey(), deadline=time.time() + 3600,
            size=os.terminal_size((160, 24)),
            get_size=lambda fallback: os.terminal_size((160, 24)))
        self.assertEqual(event, ("key", "j"))

    def test_settle_resize_answers_with_the_final_shape(self):
        # Windows Terminal streams intermediate sizes during a drag; the
        # settle loop must ride it out and answer with the final shape so
        # the caller lands the last paint at the settled size.
        sizes = iter([os.terminal_size((150, 24)), os.terminal_size((120, 20)),
                      os.terminal_size((120, 20))])
        settled = git_roost.settle_resize(
            os.terminal_size((160, 24)),
            get_size=lambda fallback: next(sizes), sleep=lambda s: None)
        self.assertEqual(settled, os.terminal_size((120, 20)))

    def test_drag_repaints_live_while_the_size_keeps_changing(self):
        # The old settle-then-paint policy left the screen jumbled for the
        # whole drag. Now a moving drag calls repaint() mid-flight with the
        # current size, before the size ever settles.
        burst = [os.terminal_size((150 - 5 * i, 24)) for i in range(4)]
        burst += [os.terminal_size((120, 20)), os.terminal_size((120, 20))]
        sizes = iter(burst)
        now = [0.0]

        def sleep(s):
            now[0] += s

        paints = []
        settled = git_roost.settle_resize(
            os.terminal_size((160, 24)),
            repaint=lambda cur: paints.append((cur, now[0])),
            get_size=lambda fallback: next(sizes),
            sleep=sleep, clock=lambda: now[0])
        self.assertEqual(settled, os.terminal_size((120, 20)))
        # At least one paint happened while the drag was still moving, at an
        # intermediate size -- not just the final settled one.
        self.assertTrue(paints)
        self.assertNotEqual(paints[0][0], os.terminal_size((120, 20)))

    def test_drag_repaints_are_throttled(self):
        # A drag emits a size roughly every RESIZE_SETTLE; painting each one
        # would chase the mouse. Consecutive mid-drag paints must be at
        # least RESIZE_REPAINT apart, and a drag that outlives
        # RESIZE_SETTLE_MAX must still hand control back to the loop.
        def endless(fallback):
            endless.n += 1
            return os.terminal_size((200 + endless.n, 24))
        endless.n = 0
        now = [0.0]

        def sleep(s):
            now[0] += s

        paints = []
        git_roost.settle_resize(
            os.terminal_size((160, 24)),
            repaint=lambda cur: paints.append(now[0]),
            get_size=endless, sleep=sleep, clock=lambda: now[0])
        # It returned (RESIZE_SETTLE_MAX cap) even though the size never
        # settled, and it painted along the way.
        self.assertTrue(paints)
        for earlier, later in zip(paints, paints[1:]):
            self.assertGreaterEqual(later - earlier,
                                    git_roost.RESIZE_REPAINT)
        self.assertLessEqual(now[0],
                             git_roost.RESIZE_SETTLE_MAX
                             + git_roost.RESIZE_SETTLE)

    def test_drag_repaint_runs_no_subprocess(self):
        # Mid-drag paints are the same cached-relayout path as the settled
        # paint: never git, never gh, even while the burst is still moving.
        burst = [os.terminal_size((150, 30)), os.terminal_size((110, 26)),
                 os.terminal_size((100, 24)), os.terminal_size((100, 24))]
        sizes = iter(burst)
        now = [0.0]

        def sleep(s):
            now[0] += s

        states = [renderable(repo="alpha", last_ts=1)]
        view = {"sort": "recent", "filter": "all", "quiet": True,
                "log": False, "log_limit": 25, "cursor": 0, "detail": None,
                "github": False, "note": None, "page": 10}

        class Args:
            json = False
            legacy_json = False
            github = False
            watch = 3.0

        def boom(*a, **kw):
            raise AssertionError("drag repaint ran a subprocess")

        paints = []

        def repaint(cur):
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.draw_watch_frame(
                    True, cur.columns, max(8, cur.lines), view, Args(),
                    False, states, set())
            paints.append(buf.getvalue())

        with mock.patch.object(git_roost.subprocess, "run", boom), \
                mock.patch.object(git_roost.subprocess, "Popen", boom):
            settled = git_roost.settle_resize(
                os.terminal_size((160, 24)), repaint=repaint,
                get_size=lambda fallback: next(sizes),
                sleep=sleep, clock=lambda: now[0])
        self.assertEqual(settled, os.terminal_size((100, 24)))
        self.assertTrue(paints)
        self.assertTrue(all(paints))

    def test_resize_repaint_runs_no_subprocess(self):
        # The point of the scan/repaint split: rendering a frame at a new
        # width from already-scanned states must never reach for git (or gh).
        # Disable subprocess at the module level and repaint at three widths,
        # including one narrow enough to trigger column shedding.
        states = [renderable(repo="alpha", last_ts=1),
                  renderable(tracked=3, repo="beta", last_ts=1)]
        view = {"sort": "recent", "filter": "all", "quiet": True,
                "log": False, "log_limit": 25, "cursor": 0, "detail": None,
                "github": False, "note": None, "page": 10}

        class Args:
            json = False
            legacy_json = False
            github = False
            watch = 3.0

        def boom(*a, **kw):
            raise AssertionError("repaint ran a subprocess")

        with mock.patch.object(git_roost.subprocess, "run", boom), \
                mock.patch.object(git_roost.subprocess, "Popen", boom):
            for width, height in ((160, 40), (100, 30), (46, 12)):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    shown = git_roost.draw_watch_frame(
                        True, width, height, view, Args(), False, states,
                        set())
                self.assertTrue(buf.getvalue())
                self.assertEqual(len(shown), 2)


class TestTtyFrameSizeChange(unittest.TestCase):
    """The in-place no-flicker overwrite is only sound at an unchanged size.

    Across a size change the terminal has rewrapped the previous frame at the
    new width, so overwriting row-by-row misaligns: fragments of old wrapped
    lines and rows outside the freshly painted region survive. write_tty_frame
    must erase the display once on any shape change (drag repaints and the
    settled paint both funnel through it), and must NOT erase on a same-size
    paint -- the full-screen erase every frame is exactly what made watch mode
    flash before the overwrite path existed.
    """

    def setUp(self):
        # Each test starts as watch mode does: no previously painted frame.
        git_roost._LAST_TTY_SIZE[0] = None

    def paint(self, width, height, lines):
        buf = io.StringIO()
        with redirect_stdout(buf):
            git_roost.write_tty_frame(width, height, lines)
        return buf.getvalue()

    def test_size_change_emits_a_full_clear(self):
        self.paint(160, 24, ["a", "b"])
        out = self.paint(120, 20, ["a", "b"])
        self.assertIn("\033[2J", out)

    def test_same_size_repaint_stays_flicker_free(self):
        self.paint(160, 24, ["a", "b"])
        out = self.paint(160, 24, ["c", "d"])
        self.assertNotIn("\033[2J", out,
                         "a same-size repaint erased the display; that "
                         "full-buffer flash is what the overwrite path "
                         "exists to prevent")

    def test_first_paint_of_a_session_starts_from_a_clear(self):
        # The alternate-screen entry resets the tracker, so the first frame
        # never trusts leftovers from a previous session's shape.
        out = self.paint(160, 24, ["a"])
        self.assertIn("\033[2J", out)

    def test_shrink_leaves_no_stale_trailing_rows(self):
        # Paint tall, then shrink. The clear must come before any row is
        # written, and no cursor address in the new frame may reach past the
        # new height -- together those leave nothing of the tall frame alive.
        self.paint(100, 30, ["row %d" % i for i in range(30)])
        out = self.paint(100, 10, ["row %d" % i for i in range(5)])
        clear_at = out.find("\033[2J")
        self.assertNotEqual(clear_at, -1)
        first_row = out.find("\033[1;1H")
        self.assertNotEqual(first_row, -1)
        self.assertLess(clear_at, first_row)
        addressed = [int(m) for m in
                     re.findall(r"\033\[(\d+);1H", out)]
        self.assertEqual(max(addressed), 10)
        # Every row of the shorter frame is written (blank rows padded and
        # cleared), so nothing below row 5 relies on the old frame either.
        self.assertEqual(sorted(addressed), list(range(1, 11)))

    def test_drag_and_settled_paints_share_the_clearing_path(self):
        # A drag burst then the settled size: the first paint of each new
        # shape clears, and a repeat of the settled shape does not.
        outs = [self.paint(w, h, ["x"]) for w, h in
                ((150, 24), (130, 22), (120, 20), (120, 20))]
        self.assertIn("\033[2J", outs[0])
        self.assertIn("\033[2J", outs[1])
        self.assertIn("\033[2J", outs[2])
        self.assertNotIn("\033[2J", outs[3])


class TestSortModes(unittest.TestCase):
    """Sorting cycles within a group, never across one.

    The group order is the argument the tool makes -- cost of ignoring, not
    recency or size. A sort mode that let an ACTIVE tree float above a
    MID-OPERATION one would quietly answer a different question, so every mode
    is asserted to keep the bucket as its first key.
    """

    def test_every_mode_keeps_group_order_first(self):
        rows = [
            state(operation="rebase", repo="zulu", last_ts=1),
            state(tracked=9, repo="alpha", last_ts=500),
            state(last_ts=400, repo="mike"),
        ]
        for mode in git_roost.SORT_MODES:
            ordered = sorted(rows, key=lambda st: git_roost.sort_key(st, mode))
            self.assertEqual(
                git_roost.bucket(ordered[0])[1], "MID-OPERATION",
                "sort mode %r floated something above MID-OPERATION" % mode)

    def test_recent_orders_by_last_commit_within_a_group(self):
        older = state(tracked=1, repo="a", last_ts=100)
        newer = state(tracked=1, repo="z", last_ts=900)
        ordered = sorted([older, newer], key=lambda st: git_roost.sort_key(st, "recent"))
        self.assertEqual([s["repo"] for s in ordered], ["z", "a"])

    def test_repo_orders_alphabetically_within_a_group(self):
        older = state(tracked=1, repo="a", last_ts=100)
        newer = state(tracked=1, repo="z", last_ts=900)
        ordered = sorted([older, newer], key=lambda st: git_roost.sort_key(st, "repo"))
        self.assertEqual([s["repo"] for s in ordered], ["a", "z"])

    def test_work_puts_the_biggest_pile_first(self):
        small = state(tracked=1, repo="a", last_ts=900)
        big = state(tracked=8, repo="z", last_ts=100)
        ordered = sorted([small, big], key=lambda st: git_roost.sort_key(st, "work"))
        self.assertEqual([s["repo"] for s in ordered], ["z", "a"])

    def test_default_mode_matches_the_old_two_argument_behaviour(self):
        # sort_key() gained a parameter; the default has to be what 0.1 did, or
        # the one-shot table silently reorders for everyone who never pressed a
        # key.
        rows = [state(tracked=1, repo="a", last_ts=100),
                state(tracked=1, repo="z", last_ts=900)]
        self.assertEqual(
            [git_roost.sort_key(s) for s in rows],
            [git_roost.sort_key(s, "recent") for s in rows])


class TestFilters(unittest.TestCase):
    def test_dirty_counts_untracked_too(self):
        # Untracked never gets its own bucket -- see work() -- but a filter
        # asking "what have I not saved" that hid a tree with 12 untracked
        # files would be answering the wrong question.
        self.assertTrue(git_roost.passes_filter(state(untracked=3), "dirty"))
        self.assertTrue(git_roost.passes_filter(state(tracked=1), "dirty"))
        self.assertFalse(git_roost.passes_filter(state(), "dirty"))

    def test_stuck_is_operations_only(self):
        self.assertTrue(git_roost.passes_filter(state(operation="merge"), "stuck"))
        self.assertFalse(git_roost.passes_filter(state(tracked=5), "stuck"))

    def test_all_passes_everything(self):
        for st in (state(), state(tracked=2), state(operation="rebase")):
            self.assertTrue(git_roost.passes_filter(st, "all"))

    def test_filtered_summary_reports_both_numbers(self):
        # A filtered count printed alone reads as the whole fleet, which is the
        # one impression this tool must never leave.
        rows = [renderable(operation="rebase", repo="a", last_ts=1),
                renderable(repo="b", last_ts=1), renderable(repo="c", last_ts=1)]
        out = "\n".join(git_roost.render(rows, width=200, filt="stuck"))
        self.assertIn("1 of 3 tree(s)", out)
        self.assertIn("[filter: mid-operation]", out)

    def test_filtered_summary_keeps_fleet_counts_fleet_wide(self):
        # Only the tree count is filtered. Recomputing "N with uncommitted work"
        # over the filtered subset leaves a number that is still plausible but
        # now means "of the ones you are looking at" -- the same misreading the
        # "N of M" wording exists to prevent.
        rows = [renderable(operation="rebase", tracked=1, repo="a", last_ts=1),
                renderable(tracked=4, repo="b", last_ts=1),
                renderable(repo="c", last_ts=1)]
        out = "%sn".replace("%s", chr(92)).join(
            git_roost.render(rows, width=200, filt="stuck"))
        self.assertIn("1 of 3 tree(s) across 3 repo(s)", out)
        self.assertIn("2 with uncommitted work", out)

    def test_filter_matching_nothing_says_so(self):
        out = git_roost.render([renderable(repo="a", last_ts=1)], width=200, filt="stuck")
        self.assertEqual(out, ["no tree matches filter: mid-operation"])

    def test_unfiltered_summary_is_unchanged(self):
        rows = [renderable(repo="a", last_ts=1), renderable(repo="b", last_ts=1)]
        out = "\n".join(git_roost.render(rows, width=200, expand_quiet=True))
        self.assertIn("2 tree(s) across 2 repo(s)", out)
        self.assertNotIn("[filter:", out)


class TestWatchKeys(unittest.TestCase):
    """The keymap, and the promise that none of it reaches the one-shot path."""

    def test_keymap_has_no_duplicate_bindings(self):
        keys = [k for k, _ in git_roost.KEYMAP]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_documented_key_is_handled(self):
        # The map is what the `?` overlay prints. A key documented but not wired
        # would look like a broken feature rather than a missing one.
        documented = {k for k, _ in git_roost.KEYMAP}
        self.assertEqual(
            documented, {"?", "r", "s", "f", "a", "l", "g", "j", "k",
                         "pgup", "home", "y", "enter", "q"})

    def test_every_nav_name_a_reader_can_emit_is_documented_or_handled(self):
        # Keys.wait folds platform-specific arrow/page/home sequences into
        # symbolic names. Every name either binds in apply_key or it is a
        # dead map entry -- exercise each one and require no crash and a
        # plausible cursor.
        names = set(git_roost.WIN_NAV_KEYS.values()) | set(
            git_roost.ANSI_NAV_KEYS.values())
        self.assertEqual(names, {"up", "down", "home", "end", "pgup", "pgdn"})
        shown = [renderable(repo=str(i)) for i in range(30)]
        for name in names:
            view = {"sort": "recent", "filter": "all", "quiet": False,
                    "cursor": 5, "page": 10}
            self.assertIsNone(git_roost.apply_key(view, name, shown=shown))
            self.assertTrue(0 <= view["cursor"] < len(shown))

    def test_help_overlay_lists_every_key(self):
        out = "\n".join(git_roost.help_lines())
        for key, _ in git_roost.KEYMAP:
            self.assertIn(key, out)

    def test_s_cycles_sort_and_wraps(self):
        view = {"sort": git_roost.SORT_MODES[0], "filter": "all", "quiet": False}
        seen = []
        for _ in range(len(git_roost.SORT_MODES)):
            git_roost.apply_key(view, "s")
            seen.append(view["sort"])
        # Every mode is reachable, and the cycle closes rather than dead-ending
        # on the last one.
        self.assertEqual(set(seen), set(git_roost.SORT_MODES))
        self.assertEqual(view["sort"], git_roost.SORT_MODES[0])

    def test_f_cycles_filter_and_wraps(self):
        view = {"sort": "recent", "filter": git_roost.FILTER_MODES[0], "quiet": False}
        for _ in range(len(git_roost.FILTER_MODES)):
            git_roost.apply_key(view, "f")
        self.assertEqual(view["filter"], git_roost.FILTER_MODES[0])

    def test_a_toggles_quiet_both_ways(self):
        view = {"sort": "recent", "filter": "all", "quiet": False}
        git_roost.apply_key(view, "a")
        self.assertTrue(view["quiet"])
        git_roost.apply_key(view, "a")
        self.assertFalse(view["quiet"])

    def test_q_quits_and_question_mark_opens_help(self):
        view = {"sort": "recent", "filter": "all", "quiet": False}
        self.assertEqual(git_roost.apply_key(view, "q"), "quit")
        self.assertEqual(git_roost.apply_key(view, "?"), "help")

    def test_keys_are_case_insensitive(self):
        lower = {"sort": "recent", "filter": "all", "quiet": False}
        upper = {"sort": "recent", "filter": "all", "quiet": False}
        for key in ("s", "f", "a"):
            git_roost.apply_key(lower, key)
            git_roost.apply_key(upper, key.upper())
        self.assertEqual(lower, upper)
        self.assertEqual(git_roost.apply_key(upper, "Q"), "quit")

    def test_r_requests_a_rescan_and_unbound_keys_change_nothing(self):
        # Every other key repaints the cached fleet; 'r' is the one that may
        # reach for git, and it must say so via its return value or the loop
        # would serve it the same cache. Neither touches the view dict.
        for key in ("r", "R"):
            view = {"sort": "repo", "filter": "dirty", "quiet": True}
            self.assertEqual(git_roost.apply_key(view, key), "rescan")
            self.assertEqual(view, {"sort": "repo", "filter": "dirty", "quiet": True})
        for key in ("x", "5", " "):
            view = {"sort": "repo", "filter": "dirty", "quiet": True}
            self.assertIsNone(git_roost.apply_key(view, key))
            self.assertEqual(view, {"sort": "repo", "filter": "dirty", "quiet": True})

    def test_every_view_a_key_can_reach_renders(self):
        # Cycling into a combination that crashes render() would only show up
        # in someone's live watch loop, which is the worst place to find it.
        rows = [renderable(operation="rebase", repo="a", last_ts=1),
                renderable(tracked=2, repo="b", last_ts=1),
                renderable(repo="c", last_ts=1)]
        for sort_mode in git_roost.SORT_MODES:
            for filt in git_roost.FILTER_MODES:
                for quiet in (True, False):
                    out = git_roost.render(rows, width=200, expand_quiet=quiet,
                                           sort_mode=sort_mode, filt=filt)
                    self.assertTrue(out and isinstance(out[0], str))

    def test_reader_is_inert_when_stdin_is_not_a_tty(self):
        # `git-roost -w < /dev/null`, or any test harness. It must degrade to
        # the timer-only redraw rather than raising -- and it must never put a
        # terminal it does not own into cbreak.
        with git_roost.Keys(stream=io.StringIO()) as keys:
            self.assertFalse(keys.enabled)
            start = time.time()
            self.assertIsNone(keys.wait(0.01))
            self.assertGreaterEqual(time.time() - start, 0.005)

    def test_reader_survives_a_closed_stream(self):
        buf = io.StringIO()
        buf.close()
        with git_roost.Keys(stream=buf) as keys:
            self.assertFalse(keys.enabled)

    def test_status_line_names_the_active_view(self):
        line = git_roost.status_line("repo", "dirty", False, 3.0)
        self.assertIn("sort:repo", line)
        self.assertIn("filter:uncommitted", line)
        self.assertIn("quiet:collapsed", line)

    def test_status_line_shows_a_loading_spinner(self):
        line = git_roost.status_line(
            "recent", "all", False, 3.0, loading="scanning 3/9 |")
        self.assertIn("scanning 3/9", line)

    def test_status_line_carries_the_version(self):
        # leghorn's #18, ported: mid-watch is where "which version is this
        # box running" gets asked, and --version is not reachable from there.
        line = git_roost.status_line("recent", "all", False, 3.0)
        self.assertIn("v" + git_roost.__version__, line)

    def test_status_line_shows_a_note_only_when_given(self):
        with_note = git_roost.status_line(
            "recent", "all", False, 3.0, note="copied: /tmp/x")
        without = git_roost.status_line("recent", "all", False, 3.0)
        self.assertIn("copied: /tmp/x", with_note)
        self.assertNotIn("copied", without)

    def test_help_explains_the_display_not_just_the_keys(self):
        out = "\n".join(git_roost.help_lines())
        for label in ("MID-OPERATION", "DIVERGED", "UNCOMMITTED", "UNPUSHED",
                      "BEHIND", "ACTIVE", "QUIET", "WORK", "DRIFT", "STASH"):
            self.assertIn(label, out)

    @needs_git
    def test_collect_progress_reaches_the_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "a")
            make_repo(Path(tmp) / "b")
            hits = []

            def cb(done, total, states):
                hits.append((done, total, len([s for s in states if s])))

            out = git_roost.collect(
                git_roost.discover([Path(tmp)]), on_progress=cb)
            self.assertEqual(len(out), 2)
            self.assertEqual(hits[-1][0], hits[-1][1])
            self.assertEqual(hits[-1][2], 2)
            self.assertGreaterEqual(len(hits), 2)

    def test_body_accepts_a_live_view(self):
        # Regression: body() gained its `view` parameter in one edit and the
        # watch loop started passing it in another. The suite stayed green
        # because every test called the two-argument form, so the only thing
        # that ever exercised the third argument was running the tool.
        class Args:
            json = False
            log = None
            all = False
            root = None
            depth = git_roost.DEFAULT_DEPTH
            sort = git_roost.SORT_MODES[0]
            filter = git_roost.FILTER_MODES[0]
            github = False
            repo = None

        with tempfile.TemporaryDirectory() as tmp:
            args = Args()
            args.root = [tmp]
            view = {"sort": "repo", "filter": "dirty", "quiet": True}
            lines, states = git_roost.body(args, 200, view)
            self.assertEqual(states, [])
            self.assertEqual(lines[0], "no git repositories found")
            self.assertTrue(any(tmp in line for line in lines))
            self.assertTrue(any("--root" in line for line in lines))

    def test_body_without_a_view_renders_the_one_shot_table(self):
        # The contract behind `git-roost | less` and `git-roost --json | jq`:
        # passing no view must produce exactly what the flags alone produce.
        class Args:
            json = False
            log = None
            all = False
            root = None
            depth = git_roost.DEFAULT_DEPTH
            sort = git_roost.SORT_MODES[0]
            filter = git_roost.FILTER_MODES[0]
            github = False
            repo = None

        with tempfile.TemporaryDirectory() as tmp:
            args = Args()
            args.root = [tmp]
            lines, states = git_roost.body(args, 200)
            self.assertEqual(states, [])
            self.assertEqual(lines[0], "no git repositories found")
            self.assertTrue(any(tmp in line for line in lines))
            self.assertTrue(any("Point git-roost at a folder of checkouts" in line for line in lines))


class TestCursorAndDetail(unittest.TestCase):
    """j/k/enter: the row cursor and its detail view.

    apply_key() needs the currently shown rows to keep the cursor sane, since
    sort/filter/quiet can shrink or reorder the set out from under it between
    keypresses.
    """

    def test_j_moves_down_and_stops_at_the_last_row(self):
        shown = [renderable(repo="a"), renderable(repo="b"), renderable(repo="c")]
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 0}
        for _ in range(5):  # more presses than rows, on purpose
            git_roost.apply_key(view, "j", shown=shown)
        self.assertEqual(view["cursor"], len(shown) - 1)

    def test_k_moves_up_and_stops_at_zero(self):
        # The bug this guards against: a naive decrement with no floor sends
        # the cursor negative, which then indexes shown[-1] on `enter` --
        # silently opening the wrong tree instead of doing nothing.
        shown = [renderable(repo="a"), renderable(repo="b")]
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 0}
        git_roost.apply_key(view, "k", shown=shown)
        self.assertEqual(view["cursor"], 0)

    def test_j_and_k_with_no_shown_rows_does_not_move_or_crash(self):
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 0}
        git_roost.apply_key(view, "j", shown=[])
        git_roost.apply_key(view, "k", shown=None)
        self.assertEqual(view["cursor"], 0)

    def test_arrow_names_alias_j_and_k(self):
        shown = [renderable(repo="a"), renderable(repo="b"), renderable(repo="c")]
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 0}
        git_roost.apply_key(view, "down", shown=shown)
        git_roost.apply_key(view, "down", shown=shown)
        self.assertEqual(view["cursor"], 2)
        git_roost.apply_key(view, "up", shown=shown)
        self.assertEqual(view["cursor"], 1)

    def test_page_keys_move_by_the_viewport_and_clamp(self):
        shown = [renderable(repo=str(i)) for i in range(25)]
        view = {"sort": "recent", "filter": "all", "quiet": False,
                "cursor": 0, "page": 10}
        git_roost.apply_key(view, "pgdn", shown=shown)
        self.assertEqual(view["cursor"], 10)
        git_roost.apply_key(view, "pgdn", shown=shown)
        git_roost.apply_key(view, "pgdn", shown=shown)  # past the end
        self.assertEqual(view["cursor"], 24)
        git_roost.apply_key(view, "pgup", shown=shown)
        self.assertEqual(view["cursor"], 14)
        for _ in range(3):  # past the top
            git_roost.apply_key(view, "pgup", shown=shown)
        self.assertEqual(view["cursor"], 0)

    def test_home_and_end_jump_to_the_edges(self):
        shown = [renderable(repo=str(i)) for i in range(7)]
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 3}
        git_roost.apply_key(view, "end", shown=shown)
        self.assertEqual(view["cursor"], 6)
        git_roost.apply_key(view, "home", shown=shown)
        self.assertEqual(view["cursor"], 0)

    def test_g_toggles_the_github_column_when_gh_exists(self):
        view = {"sort": "recent", "filter": "all", "quiet": False,
                "github": False}
        with mock.patch.object(git_roost, "GH_PATH", "/usr/bin/gh"):
            git_roost.apply_key(view, "g")
            self.assertTrue(view["github"])
            git_roost.apply_key(view, "G")
            self.assertFalse(view["github"])

    def test_g_without_gh_refuses_with_a_note_instead_of_a_blank_column(self):
        view = {"sort": "recent", "filter": "all", "quiet": False,
                "github": False}
        with mock.patch.object(git_roost, "GH_PATH", None):
            git_roost.apply_key(view, "g")
        self.assertFalse(view["github"])
        self.assertIn("gh", view["note"])

    def test_y_yanks_the_highlighted_path(self):
        shown = [renderable(repo="a"), renderable(repo="b")]
        shown[1]["path"] = "/tmp/somewhere/b"
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 1}
        with mock.patch.object(git_roost, "to_clipboard",
                               return_value=True) as clip:
            git_roost.apply_key(view, "y", shown=shown)
        clip.assert_called_once_with("/tmp/somewhere/b")
        self.assertIn("/tmp/somewhere/b", view["note"])

    def test_y_reports_a_missing_clipboard_helper(self):
        shown = [renderable(repo="a")]
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 0}
        with mock.patch.object(git_roost, "to_clipboard", return_value=False):
            git_roost.apply_key(view, "y", shown=shown)
        self.assertIn("clipboard", view["note"])

    def test_y_with_no_shown_rows_does_nothing(self):
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 0}
        with mock.patch.object(git_roost, "to_clipboard") as clip:
            git_roost.apply_key(view, "y", shown=[])
        clip.assert_not_called()

    def test_view_shaping_keys_leave_a_note(self):
        # The transient feedback line: a handled key must say what it did,
        # because the status line's own words change too subtly to confirm
        # the press registered.
        view = {"sort": "recent", "filter": "all", "quiet": False, "log": False}
        git_roost.apply_key(view, "s")
        self.assertEqual(view["note"], "sort: repo")
        git_roost.apply_key(view, "f")
        self.assertEqual(view["note"], "filter: uncommitted")
        git_roost.apply_key(view, "a")
        self.assertEqual(view["note"], "quiet: shown")
        git_roost.apply_key(view, "l")
        self.assertEqual(view["note"], "view: commit feed")

    def test_changing_sort_resets_the_cursor(self):
        # A cursor left at row 4 after a sort that now has only 2 rows on
        # screen would either be silently clamped somewhere else or, worse,
        # index past the end. Resetting on any view-shaping key sidesteps
        # both.
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 3}
        git_roost.apply_key(view, "s", shown=[renderable()])
        self.assertEqual(view["cursor"], 0)

    def test_enter_opens_the_row_under_the_cursor(self):
        shown = [renderable(repo="a"), renderable(repo="b"), renderable(repo="c")]
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 1}
        git_roost.apply_key(view, "enter", shown=shown)
        self.assertEqual(view["detail"]["repo"], "b")

    def test_enter_with_no_shown_rows_opens_nothing(self):
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 0}
        git_roost.apply_key(view, "enter", shown=[])
        self.assertNotIn("detail", view)

    def test_enter_clamps_a_cursor_left_over_from_a_wider_frame(self):
        view = {"sort": "recent", "filter": "all", "quiet": False, "cursor": 9}
        shown = [renderable(repo="only")]
        git_roost.apply_key(view, "\r", shown=shown)
        self.assertEqual(view["detail"]["repo"], "only")

    @needs_git
    def test_detail_view_shows_full_stash_list_for_a_tree_with_stashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "f.txt").write_text("one\n")
            run(repo, "add", "f.txt")
            run(repo, "stash", "push", "-m", "wip one")
            (repo / "f.txt").write_text("two\n")
            run(repo, "add", "f.txt")
            run(repo, "stash", "push", "-m", "wip two")

            st = git_roost.tree_state(repo)
            out = "\n".join(git_roost.detail_lines(st))
            self.assertIn("wip one", out)
            self.assertIn("wip two", out)
            self.assertNotIn("(none)", out)

    @needs_git
    def test_detail_view_says_none_for_a_tree_with_no_stashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            st = git_roost.tree_state(repo)
            out = "\n".join(git_roost.detail_lines(st))
            self.assertIn("(none)", out)

    @needs_git
    def test_detail_view_includes_last_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            st = git_roost.tree_state(repo)
            out = "\n".join(git_roost.detail_lines(st))
            self.assertIn("initial", out)

    @needs_git
    def test_detail_view_shows_operation_in_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            (repo / "f.txt").write_text("base\n")
            run(repo, "add", "f.txt")
            run(repo, "commit", "-m", "base")
            run(repo, "checkout", "-b", "side")
            (repo / "f.txt").write_text("side\n")
            run(repo, "add", "f.txt")
            run(repo, "commit", "-m", "side")
            run(repo, "checkout", "main")
            (repo / "f.txt").write_text("main\n")
            run(repo, "add", "f.txt")
            run(repo, "commit", "-m", "main")
            subprocess.run(("git", "merge", "side"), cwd=str(repo),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

            st = git_roost.tree_state(repo)
            out = "\n".join(git_roost.detail_lines(st))
            self.assertIn("merge in progress", out)


class TestChangeHighlighting(unittest.TestCase):
    """Frame-to-frame change marking: what a top-style tool owes a watcher."""

    def test_frame_signature_changes_when_bucket_changes(self):
        clean = renderable(repo="a", tracked=0)
        dirty = renderable(repo="a", tracked=3)
        self.assertNotEqual(git_roost.frame_signature(clean), git_roost.frame_signature(dirty))

    def test_frame_signature_is_stable_for_an_identical_state(self):
        a = renderable(repo="a", tracked=1, untracked=2, ahead=1, behind=0, base="origin/main")
        b = renderable(repo="a", tracked=1, untracked=2, ahead=1, behind=0, base="origin/main")
        self.assertEqual(git_roost.frame_signature(a), git_roost.frame_signature(b))

    def test_frame_signature_ignores_last_ts_alone(self):
        # LAST ticks every second in watch mode. If the signature included it,
        # every row would show as "changed" on every redraw, which defeats the
        # entire point of a change marker.
        a = renderable(repo="a", last_ts=1000)
        b = renderable(repo="a", last_ts=2000)
        self.assertEqual(git_roost.frame_signature(a), git_roost.frame_signature(b))

    def test_render_marks_a_changed_row_and_not_an_unchanged_one(self):
        # tracked=1 keeps both rows out of the collapsed QUIET group, which
        # would otherwise fold them into one summary line with no per-row
        # marker to assert on.
        unchanged = renderable(repo="a", path="/p/a", tracked=1)
        row_changed = renderable(repo="b", path="/p/b", tracked=1)
        rows = [unchanged, row_changed]
        out = git_roost.render(rows, width=200, changed={"/p/b"})
        # The marker is a fixed two-char prefix ("* " or "  "); the REPO cell
        # right after it starts immediately with the repo name. COLOR is off
        # in the test process (no terminal), so there are no escapes to strip.
        a_line = next(l for l in out if l[2:].startswith("a"))
        b_line = next(l for l in out if l[2:].startswith("b"))
        self.assertTrue(a_line.startswith("  "))
        self.assertTrue(b_line.startswith("* "))

    def test_render_without_changed_marks_nothing(self):
        rows = [renderable(repo="a", path="/p/a")]
        out = "\n".join(git_roost.render(rows, width=200))
        self.assertNotIn("*", out)


class TestLogToggle(unittest.TestCase):
    """`l`: flip watch mode between the fleet table and the commit feed."""

    def test_l_toggles_log_both_ways(self):
        view = {"sort": "recent", "filter": "all", "quiet": False, "log": False, "cursor": 0}
        git_roost.apply_key(view, "l")
        self.assertTrue(view["log"])
        git_roost.apply_key(view, "l")
        self.assertFalse(view["log"])

    @needs_git
    def test_render_view_shows_the_feed_when_log_is_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            states = git_roost.collect(git_roost.discover([Path(tmp)]))
            view = {"sort": "recent", "filter": "all", "quiet": False,
                    "log": True, "log_limit": 25, "cursor": 0, "detail": None}
            out = "\n".join(git_roost.render_view(states, 200, view))
            self.assertIn("initial", out)
            self.assertIn("AGE", out)  # render_log's header, not render()'s

    @needs_git
    def test_render_view_shows_the_table_when_log_is_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            states = git_roost.collect(git_roost.discover([Path(tmp)]))
            view = {"sort": "recent", "filter": "all", "quiet": False,
                    "log": False, "log_limit": 25, "cursor": 0, "detail": None}
            out = "\n".join(git_roost.render_view(states, 200, view))
            self.assertIn("WORK", out)  # render()'s header, not render_log's

    def test_render_view_shows_detail_when_set(self):
        st = renderable(repo="a", path="/p/a")
        view = {"sort": "recent", "filter": "all", "quiet": False,
                "log": False, "log_limit": 25, "cursor": 0, "detail": st}
        out = "\n".join(git_roost.render_view([st], 200, view))
        self.assertIn("LAST 5 COMMITS", out)


class TestCli(unittest.TestCase):
    def test_version_exits_clean(self):
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                git_roost.main(["--version"])
        self.assertEqual(cm.exception.code, 0)

    def test_print_completion_emits_a_script_and_exits_clean(self):
        for shell, marker in (("bash", "complete -F _git_roost git-roost"),
                              ("zsh", "#compdef git-roost"),
                              ("powershell", "Register-ArgumentCompleter")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = git_roost.main(["--print-completion", shell])
            self.assertEqual(rc, 0)
            self.assertIn(marker, buf.getvalue())

    def test_print_completion_rejects_an_unknown_shell(self):
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                git_roost.main(["--print-completion", "fish"])

    def test_completion_words_track_the_parser(self):
        # The whole point of deriving the word list: a flag added to the
        # parser appears in every script without anyone remembering to.
        words = git_roost._completion_flag_words()
        self.assertIn("--print-completion", words)
        self.assertIn("--github", words)
        for shell in ("bash", "powershell"):
            script = git_roost.print_completion(shell)
            for w in words:
                self.assertIn(w, script)

    def test_completion_value_flags_all_exist_on_the_parser(self):
        # A value flag renamed on the parser but not here would silently stop
        # suppressing flag suggestions after it.
        words = set(git_roost._completion_flag_words())
        for flag in git_roost._COMPLETION_VALUE_FLAGS:
            self.assertIn(flag, words)

    def test_completion_never_runs_a_scan(self):
        # Completion must work on a box with no repos and no scan roots --
        # it is parser metadata, not a fleet question.
        with mock.patch.object(git_roost, "scan",
                               side_effect=AssertionError("scanned")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = git_roost.main(["--print-completion", "bash"])
            self.assertEqual(rc, 0)

    def test_runs_with_zero_repositories(self):
        # The contract with packaging: --help and a bare run must work on a box
        # that has no repos at all.
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = git_roost.main(["--root", tmp])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("no git repositories found", out)
            self.assertIn(tmp, out)
            self.assertIn("GIT_ROOST_ROOT", out)

    @needs_git
    def test_root_flag_with_no_path_means_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            prev = os.getcwd()
            buf = io.StringIO()
            try:
                os.chdir(tmp)
                with redirect_stdout(buf):
                    rc = git_roost.main(["--root", "--json"])
            finally:
                os.chdir(prev)
            self.assertEqual(rc, 0)
            data = trees(buf.getvalue())
            self.assertEqual([r["repo"] for r in data], ["alpha"])

    @needs_git
    def test_json_is_valid_and_machine_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--json"])
            data = trees(buf.getvalue())
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["repo"], "alpha")

    @needs_git
    def test_json_envelope_names_its_schema_and_version(self):
        # Same convention as every sibling: a consumer can tell which shape
        # it got before indexing into it, and which git-roost produced it.
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--json"])
            data = json.loads(buf.getvalue())
            self.assertEqual(set(data), {"schema", "version", "trees"})
            self.assertEqual(data["schema"], git_roost.JSON_SCHEMA)
            self.assertEqual(data["version"], git_roost.__version__)
            self.assertIsInstance(data["trees"], list)

    @needs_git
    def test_legacy_json_is_the_bare_list(self):
        # The escape hatch for consumers written against <= 0.5: the exact
        # old shape, not a differently-decorated new one.
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--json", "--legacy-json"])
            data = json.loads(buf.getvalue())
            self.assertIsInstance(data, list)
            self.assertEqual(data[0]["repo"], "alpha")

    @needs_git
    def test_check_json_wraps_and_honours_legacy_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            dirty = make_repo(Path(tmp) / "dirty")
            (dirty / "README.md").write_text("changed\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--check", "--json"])
            data = json.loads(buf.getvalue())
            self.assertEqual(data["schema"], git_roost.JSON_SCHEMA)
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--check", "--json",
                                "--legacy-json"])
            self.assertIsInstance(json.loads(buf.getvalue()), list)

    @needs_git
    def test_json_record_keys_are_a_stable_contract(self):
        # henhouse (leghorn's data layer) consumes `git-roost --json`. Adding
        # a key is safe; renaming or removing one breaks a consumer this suite
        # cannot see -- and it is also what JSON_SCHEMA's version suffix is
        # for. If you are changing the shape deliberately, bump the schema,
        # update this set and henhouse's gather_git() together. assertEqual,
        # not a subset check: a subset check passes when a key is deleted,
        # which is exactly the case that breaks the consumer.
        EXPECTED = {
            "ahead", "base", "behind", "branch", "common_dir", "conflicts",
            "detached", "last_author", "last_hash", "last_subject", "last_ts",
            "operation", "path", "repo", "staged", "stashes", "toplevel",
            "tracked", "tree", "unstaged", "untracked",
        }
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--json"])
            data = trees(buf.getvalue())
            self.assertEqual(set(data[0]), EXPECTED)

    def test_sort_and_filter_flags_reach_the_one_shot_render(self):
        # SORT_MODES/FILTER_MODES were only ever reachable from watch-mode keys.
        # These flags are the scriptable path onto the same machinery.
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = git_roost.main(["--root", tmp, "--filter", "dirty"])
            self.assertEqual(rc, 0)
            self.assertIn("no tree matches filter: uncommitted", buf.getvalue())

    def test_sort_and_filter_reject_unknown_values(self):
        with self.assertRaises(SystemExit):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                git_roost.main(["--sort", "bogus"])

    def test_watch_without_vt_falls_back_to_once_instead_of_spamming(self):
        # Non-VT Windows consoles used to append a full frame every interval
        # (the "spamming my cmd" bug). Watch must refuse and render once.
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            # Patch *after* redirect so isatty lands on the capture stream,
            # not the original stdout that redirect replaces.
            with redirect_stdout(out), redirect_stderr(err), \
                 mock.patch.object(sys.stdout, "isatty", return_value=True), \
                 mock.patch.object(git_roost, "enable_windows_ansi",
                                   return_value=False):
                rc = git_roost.main(["--root", tmp, "-w", "0.01"])
            self.assertEqual(rc, 0)
            self.assertIn("no VT console", err.getvalue())
            self.assertIn("no git repositories found", out.getvalue())
            self.assertNotIn("\n---", out.getvalue())

    def test_draw_watch_frame_refuses_a_non_vt_console(self):
        args = argparse.Namespace(watch=3.0, json=False, github=False)
        view = {"sort": "recent", "filter": "all", "quiet": False,
                "log": False, "cursor": 0, "detail": None}
        with self.assertRaises(RuntimeError):
            git_roost.draw_watch_frame(
                False, 80, 24, view, args, False, [], {})


class TestExitCode(unittest.TestCase):
    def _state(self, operation="", ahead=0, behind=0, tracked=0):
        return {
            "operation": operation, "ahead": ahead, "behind": behind,
            "tracked": tracked, "last_ts": None,
        }

    def test_none_is_always_zero(self):
        self.assertEqual(git_roost.exit_code([self._state(operation="rebase")], "none"), 0)

    def test_stuck_only_fires_on_mid_operation(self):
        self.assertEqual(git_roost.exit_code([self._state(ahead=1, behind=1)], "stuck"), 0)
        self.assertEqual(git_roost.exit_code([self._state(operation="rebase")], "stuck"), 1)

    def test_diverged_fires_on_stuck_or_diverged_but_not_merely_dirty(self):
        self.assertEqual(git_roost.exit_code([self._state(tracked=3)], "diverged"), 0)
        self.assertEqual(git_roost.exit_code([self._state(ahead=1, behind=1)], "diverged"), 1)
        self.assertEqual(git_roost.exit_code([self._state(operation="merge")], "diverged"), 1)

    def test_dirty_fires_on_uncommitted_work_too(self):
        self.assertEqual(git_roost.exit_code([self._state(tracked=3)], "dirty"), 1)
        self.assertEqual(git_roost.exit_code([self._state()], "dirty"), 0)

    def test_checked_against_the_whole_fleet_not_a_filtered_view(self):
        # A hook asking --fail-on stuck must not get a false 0 just because the
        # caller also passed --filter for a human to read at the same time.
        fleet = [self._state(), self._state(operation="rebase")]
        self.assertEqual(git_roost.exit_code(fleet, "stuck"), 1)


HAVE_GH = bool(__import__("shutil").which("gh"))
needs_gh = unittest.skipUnless(HAVE_GH, "gh is not installed")


class TestGhReadOnlyGuard(unittest.TestCase):
    """gh's own read-only wrapper. Same spirit as TestReadOnlyGuard, adapted to
    gh's shape: writes live on separate subcommands rather than behind flags on
    a read one, so the allowlist only needs (command, subcommand) plus the same
    fail-closed positional cap check_read_only uses.
    """

    def test_write_subcommands_raise(self):
        for args in [
            ("pr", "merge"), ("pr", "close"), ("pr", "edit"),
            ("pr", "ready"), ("pr", "review"), ("pr", "comment"),
            ("pr", "create"), ("pr", "reopen"),
        ]:
            with self.assertRaises(git_roost.NotReadOnly, msg=" ".join(args)):
                git_roost.check_gh_read_only(args)

    def test_non_pr_commands_raise(self):
        for args in [("issue", "list"), ("repo", "delete"), ("api", "graphql"),
                     ("run", "rerun"), ("workflow", "run")]:
            with self.assertRaises(git_roost.NotReadOnly, msg=" ".join(args)):
                git_roost.check_gh_read_only(args)

    def test_empty_args_raise(self):
        with self.assertRaises(git_roost.NotReadOnly):
            git_roost.check_gh_read_only(())

    def test_safe_forms_are_permitted(self):
        for args in [
            ("pr", "view", "--json=number,state"),
            ("pr", "list", "--json=number"),
            ("pr", "status"),
            ("pr", "view"),
        ]:
            git_roost.check_gh_read_only(args)  # must not raise

    def test_a_bare_positional_argument_is_refused(self):
        # The positional cap is a fail-closed backstop, same shape as git's:
        # a value that should have arrived as --flag=value instead looks like
        # an unexamined extra argument, and this refuses it rather than
        # guessing it is still safe.
        with self.assertRaises(git_roost.NotReadOnly):
            git_roost.check_gh_read_only(("pr", "view", "some-branch"))

    def test_gh_call_checks_before_touching_the_subprocess(self):
        # A refused call must never reach subprocess.run, whether or not gh is
        # even installed on this machine.
        with self.assertRaises(git_roost.NotReadOnly):
            git_roost.gh_call("/tmp", "pr", "merge")


class FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


class TestGhCall(unittest.TestCase):
    """gh_call()'s best-effort contract: never raise, never block the scan."""

    def test_returns_none_when_gh_is_not_on_path(self):
        with unittest.mock.patch.object(git_roost, "GH_PATH", None):
            self.assertIsNone(git_roost.gh_call("/tmp", "pr", "view"))

    def test_returns_none_on_timeout(self):
        with unittest.mock.patch.object(git_roost, "GH_PATH", "gh"), \
             unittest.mock.patch.object(
                 git_roost.subprocess, "run",
                 side_effect=git_roost.subprocess.TimeoutExpired("gh", 8)):
            self.assertIsNone(git_roost.gh_call("/tmp", "pr", "view"))

    def test_returns_none_on_oserror(self):
        # gh vanishing between shutil.which() and the call, or a bad cwd.
        with unittest.mock.patch.object(git_roost, "GH_PATH", "gh"), \
             unittest.mock.patch.object(
                 git_roost.subprocess, "run", side_effect=OSError("boom")):
            self.assertIsNone(git_roost.gh_call("/tmp", "pr", "view"))

    def test_returns_none_on_nonzero_exit(self):
        # No PR for this branch, no GitHub remote, not authenticated -- gh
        # reports all of these as a plain nonzero exit, not an exception.
        with unittest.mock.patch.object(git_roost, "GH_PATH", "gh"), \
             unittest.mock.patch.object(
                 git_roost.subprocess, "run",
                 return_value=FakeCompleted(returncode=1)):
            self.assertIsNone(git_roost.gh_call("/tmp", "pr", "view"))

    def test_returns_stdout_on_success(self):
        with unittest.mock.patch.object(git_roost, "GH_PATH", "gh"), \
             unittest.mock.patch.object(
                 git_roost.subprocess, "run",
                 return_value=FakeCompleted(stdout='{"number": 1}')):
            self.assertEqual(git_roost.gh_call("/tmp", "pr", "view"), '{"number": 1}')

    @needs_gh
    def test_gh_call_against_a_non_repo_directory_degrades_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(git_roost.gh_call(tmp, "pr", "view", "--json=number"))


PR_JSON = (
    '{"number": 42, "state": "OPEN", "isDraft": false, '
    '"reviewDecision": "APPROVED", "statusCheckRollup": %s}'
)


class TestGithubFacts(unittest.TestCase):
    """Parsing/rollup logic against canned `gh pr view --json` output, so this
    does not need a real `gh` or network access to run.
    """

    def _facts(self, checks_json, gh_stdout=None):
        stdout = gh_stdout if gh_stdout is not None else PR_JSON % checks_json
        with unittest.mock.patch.object(git_roost, "gh_call", return_value=stdout):
            return git_roost.github_facts("/tmp")

    def test_no_pr_is_none(self):
        with unittest.mock.patch.object(git_roost, "gh_call", return_value=None):
            self.assertIsNone(git_roost.github_facts("/tmp"))

    def test_invalid_json_is_none(self):
        with unittest.mock.patch.object(git_roost, "gh_call", return_value="not json"):
            self.assertIsNone(git_roost.github_facts("/tmp"))

    def test_basic_fields(self):
        facts = self._facts("[]")
        self.assertEqual(facts["pr_number"], 42)
        self.assertEqual(facts["pr_state"], "open")
        self.assertFalse(facts["pr_draft"])
        self.assertEqual(facts["pr_review"], "APPROVED")
        self.assertIsNone(facts["pr_ci"])  # no checks reported at all

    def test_draft_flag(self):
        stdout = ('{"number": 1, "state": "OPEN", "isDraft": true, '
                  '"reviewDecision": null, "statusCheckRollup": []}')
        facts = self._facts(None, gh_stdout=stdout)
        self.assertTrue(facts["pr_draft"])
        self.assertIsNone(facts["pr_review"])

    def test_all_checks_succeeded_is_success(self):
        checks = ('[{"status": "COMPLETED", "conclusion": "SUCCESS"}, '
                  '{"status": "COMPLETED", "conclusion": "SUCCESS"}]')
        self.assertEqual(self._facts(checks)["pr_ci"], "success")

    def test_any_failure_wins_over_a_success(self):
        checks = ('[{"status": "COMPLETED", "conclusion": "SUCCESS"}, '
                  '{"status": "COMPLETED", "conclusion": "FAILURE"}]')
        self.assertEqual(self._facts(checks)["pr_ci"], "failure")

    def test_in_progress_check_is_pending(self):
        checks = '[{"status": "IN_PROGRESS", "conclusion": null}]'
        self.assertEqual(self._facts(checks)["pr_ci"], "pending")

    def test_pending_does_not_hide_a_failure(self):
        # A run still queued must not mask a check that already failed.
        checks = ('[{"status": "IN_PROGRESS", "conclusion": null}, '
                  '{"status": "COMPLETED", "conclusion": "FAILURE"}]')
        self.assertEqual(self._facts(checks)["pr_ci"], "failure")

    def test_external_status_context_uses_state_not_conclusion(self):
        # Commit statuses (as opposed to check-runs) carry "state", not
        # "status"/"conclusion" -- both shapes appear in the same rollup.
        checks = '[{"state": "FAILURE"}]'
        self.assertEqual(self._facts(checks)["pr_ci"], "failure")


class TestGithubColumn(unittest.TestCase):
    def test_no_pr_is_blank(self):
        self.assertEqual(git_roost.github_cell({"pr_number": None}), "")

    def test_draft(self):
        cell = git_roost.github_cell({"pr_number": 7, "pr_draft": True})
        self.assertEqual(cell, "#7 draft")

    def test_success_marker(self):
        cell = git_roost.github_cell(
            {"pr_number": 7, "pr_draft": False, "pr_ci": "success"})
        self.assertEqual(cell, "#7+")

    def test_failure_marker(self):
        cell = git_roost.github_cell(
            {"pr_number": 7, "pr_draft": False, "pr_ci": "failure"})
        self.assertEqual(cell, "#7x")

    def test_pending_marker(self):
        cell = git_roost.github_cell(
            {"pr_number": 7, "pr_draft": False, "pr_ci": "pending"})
        self.assertEqual(cell, "#7~")

    def test_no_ci_data_is_just_the_number(self):
        cell = git_roost.github_cell(
            {"pr_number": 7, "pr_draft": False, "pr_ci": None})
        self.assertEqual(cell, "#7")

    def test_cell_text_is_pure_ascii(self):
        # See github_cell()'s docstring: this column deliberately avoids the
        # console-mangling problem ascii_safe() exists for elsewhere.
        for cell in ("#7 draft", "#7+", "#7x", "#7~", "#7"):
            self.assertTrue(all(32 <= ord(ch) < 127 for ch in cell))


class TestGithubFlagGating(unittest.TestCase):
    """--github must be opt-in: the column and the JSON keys only appear when
    it is actually passed, and the tool must not need a real `gh` to prove it.
    """

    def test_column_absent_by_default(self):
        rows = [renderable(repo="a", last_ts=1)]
        out = "\n".join(git_roost.render(rows, width=200, expand_quiet=True))
        self.assertNotIn("PR", out.split("\n")[0].split())

    def test_column_present_when_requested(self):
        rows = [renderable(repo="a", last_ts=1, pr_number=None, pr_draft=False,
                           pr_ci=None)]
        out = "\n".join(git_roost.render(rows, width=200, expand_quiet=True, github=True))
        self.assertIn("PR", out.split("\n")[0].split())

    def test_json_keys_absent_without_github_flag(self):
        with unittest.mock.patch.object(git_roost, "GH_PATH", None), \
             tempfile.TemporaryDirectory() as tmp:
            if HAVE_GIT:
                make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--json"])
            data = trees(buf.getvalue())
            if data:
                for key in git_roost.GITHUB_KEYS:
                    self.assertNotIn(key, data[0])

    @needs_git
    def test_json_keys_present_and_null_when_gh_is_missing(self):
        # --github still adds the keys even when gh itself is not on the box --
        # present-but-null, not present-only-when-gh-succeeds, is what keeps
        # this a stable shape for a consumer to depend on.
        with unittest.mock.patch.object(git_roost, "GH_PATH", None), \
             tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--json", "--github"])
            data = trees(buf.getvalue())
            for key in git_roost.GITHUB_KEYS:
                self.assertIn(key, data[0])
            self.assertIsNone(data[0]["pr_number"])

    @needs_git
    def test_json_keys_reflect_a_fetched_pr(self):
        with unittest.mock.patch.object(git_roost, "GH_PATH", "gh"), \
             unittest.mock.patch.object(
                 git_roost, "gh_call", return_value=PR_JSON % "[]"), \
             tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--json", "--github"])
            data = trees(buf.getvalue())
            self.assertEqual(data[0]["pr_number"], 42)
            self.assertEqual(data[0]["pr_review"], "APPROVED")

    def test_apply_github_facts_defaults_are_stable(self):
        states = [{"path": "/p/a"}, {"path": "/p/b"}]
        git_roost.apply_github_facts(states, {"/p/a": {"pr_number": 5}})
        self.assertEqual(states[0]["pr_number"], 5)
        self.assertIsNone(states[1]["pr_number"])
        self.assertFalse(states[1]["pr_draft"])

    def test_github_facts_map_is_empty_without_gh(self):
        with unittest.mock.patch.object(git_roost, "GH_PATH", None):
            self.assertEqual(git_roost.github_facts_map([{"path": "/p/a"}]), {})


class TestRootEnvVar(unittest.TestCase):
    def test_git_roost_root_overrides_the_default(self):
        roots = git_roost.default_roots(
            env=str(Path("/tmp/one")) + os.pathsep + str(Path("/tmp/two")),
        )
        self.assertEqual([p.name for p in roots], ["one", "two"])

    def test_default_roots_are_the_current_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            proj = Path(tmp) / "proj"
            home.mkdir()
            proj.mkdir()
            (home / "dev").mkdir()
            roots = git_roost.default_roots(home=home, cwd=proj, env="")
            self.assertEqual(list(roots), [proj])

    def test_default_roots_use_cwd_even_when_home_has_dev(self):
        # Well-known ~/dev folders are no longer scanned by a bare run.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "dev").mkdir()
            roots = git_roost.default_roots(home=home, cwd=home, env="")
            self.assertEqual(list(roots), [home])

    def test_empty_fleet_with_no_roots_points_at_cwd_and_env(self):
        text = "\n".join(git_roost.empty_fleet_lines(()))
        self.assertIn("current directory", text)
        self.assertIn("GIT_ROOST_ROOT", text)
        self.assertIn("--root", text)

    def test_missing_root_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "nope")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = git_roost.main(["--root", missing])
            self.assertEqual(rc, 1)
            self.assertIn("does not exist", err.getvalue())
            self.assertIn(missing, err.getvalue())

    def test_root_that_is_a_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-a-dir"
            path.write_text("x\n", encoding="utf-8")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = git_roost.main(["--root", str(path)])
            self.assertEqual(rc, 1)
            self.assertIn("not a directory", err.getvalue())

    @needs_git
    def test_bare_run_finds_a_repo_in_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            proj = Path(tmp) / "proj"
            home.mkdir()
            make_repo(proj / "alpha")
            roots = git_roost.default_roots(home=home, cwd=proj, env="")
            found = {p.name for p in git_roost.discover(roots, depth=3)}
            self.assertEqual(found, {"alpha"})

    @needs_git
    def test_repo_flag_scopes_to_a_substring_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            make_repo(Path(tmp) / "beta")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--repo", "alp", "--json"])
            data = trees(buf.getvalue())
            self.assertEqual([r["repo"] for r in data], ["alpha"])

    @needs_git
    def test_repo_flag_is_case_insensitive_and_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            make_repo(Path(tmp) / "beta")
            make_repo(Path(tmp) / "gamma")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--repo", "ALPHA", "--repo", "beta", "--json"])
            data = trees(buf.getvalue())
            self.assertEqual(sorted(r["repo"] for r in data), ["alpha", "beta"])

    @needs_git
    def test_filter_flag_scopes_json_the_same_as_the_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = make_repo(Path(tmp) / "clean")
            dirty = make_repo(Path(tmp) / "dirty")
            (dirty / "scratch.txt").write_text("x\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--filter", "dirty", "--json"])
            data = trees(buf.getvalue())
            self.assertEqual([r["repo"] for r in data], ["dirty"])

    @needs_git
    def test_check_exits_zero_on_a_clean_fleet(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = git_roost.main(["--root", tmp, "--check"])
            self.assertEqual(rc, 0)
            self.assertIn("clean", buf.getvalue())

    @needs_git
    def test_check_exits_nonzero_when_a_tree_has_uncommitted_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "alpha")
            # An untracked file alone is not "uncommitted work" -- see
            # test_untracked_only_is_not_uncommitted -- so modify a tracked one.
            (repo / "README.md").write_text("changed\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = git_roost.main(["--root", tmp, "--check"])
            self.assertEqual(rc, 1)
            self.assertIn("alpha", buf.getvalue())

    @needs_git
    def test_check_ignores_unpushed_and_behind(self):
        # UNPUSHED/BEHIND are safe states to start work in -- see
        # needs_attention(). A repo with no remote at all is permanently
        # ahead-of-nothing and must not trip the gate.
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = git_roost.main(["--root", tmp, "--check"])
            self.assertEqual(rc, 0)

    @needs_git
    def test_check_json_emits_only_offending_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            clean = make_repo(Path(tmp) / "clean")
            dirty = make_repo(Path(tmp) / "dirty")
            (dirty / "README.md").write_text("changed\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = git_roost.main(["--root", tmp, "--check", "--json"])
            self.assertEqual(rc, 1)
            data = trees(buf.getvalue())
            self.assertEqual([r["repo"] for r in data], ["dirty"])

    @needs_git
    def test_log_feed_does_not_repeat_a_commit_per_worktree(self):
        # A repo and its worktrees share history. Without dedup the busy repos
        # here show every commit five or six times.
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r")
            run(repo, "worktree", "add", "-b", "feat/x", str(Path(tmp) / "wt"))
            states = git_roost.collect(git_roost.discover([Path(tmp)]))
            self.assertEqual(len(states), 2)
            out = "\n".join(git_roost.render_log(states, 25, width=200))
            self.assertEqual(out.count("initial"), 1)


class TestDesignLanguage(unittest.TestCase):
    """Conformance to the leghorn design language: colors by role, not rank.

    Red is failure and only MID-OPERATION earns it; magenta is divergence
    (DIVERGED and BEHIND are positions against a base, not emergencies);
    yellow is attention; blue is reserved for identity, so no bucket may take
    it. COLOR is off in the test process, so these patch it on where the
    escapes themselves are the assertion.
    """

    def test_bucket_colors_map_roles_not_rank(self):
        self.assertEqual(git_roost.BUCKET_COLORS[0], git_roost.RED)
        self.assertEqual(git_roost.BUCKET_COLORS[1], git_roost.MAGENTA)
        self.assertEqual(git_roost.BUCKET_COLORS[4], git_roost.MAGENTA)
        self.assertEqual(git_roost.BUCKET_COLORS[5], git_roost.GREEN)
        # Only genuine failure is red, and identity blue is nobody's bucket.
        reds = [v for v in git_roost.BUCKET_COLORS.values() if v == git_roost.RED]
        self.assertEqual(reds, [git_roost.RED])
        self.assertNotIn(git_roost.BLUE, git_roost.BUCKET_COLORS.values())
        self.assertNotIn(git_roost.BRIGHT_BLUE, git_roost.BUCKET_COLORS.values())

    def test_repo_cell_renders_in_the_identity_color(self):
        rows = [renderable(repo="wings", tracked=1)]
        with mock.patch.object(git_roost, "COLOR", True):
            out = "\n".join(git_roost.render(rows, width=200))
        self.assertIn(git_roost.IDENT + git_roost.BOLD + "wings", out)

    def test_blue_for_terminal_requests_index_12_on_256_color(self):
        self.assertEqual(
            git_roost.blue_for_terminal({"TERM": "xterm-256color"}),
            git_roost.BRIGHT_BLUE)
        self.assertEqual(
            git_roost.blue_for_terminal({"WT_SESSION": "abc"}),
            git_roost.BRIGHT_BLUE)
        self.assertEqual(
            git_roost.blue_for_terminal({"COLORTERM": "truecolor"}),
            git_roost.BRIGHT_BLUE)
        # A terminal that claims nothing gets the plain blue it can render.
        self.assertEqual(git_roost.blue_for_terminal({"TERM": "vt100"}),
                         git_roost.BLUE)
        self.assertEqual(git_roost.blue_for_terminal({}), git_roost.BLUE)

    def test_cursor_row_is_the_one_painted_background(self):
        rows = [renderable(repo="aa", tracked=1)]
        with mock.patch.object(git_roost, "COLOR", True):
            out = "\n".join(git_roost.render(rows, width=200, cursor=0))
        self.assertIn(git_roost.SELECTED, out)

    def test_paint_over_rearms_after_an_inner_reset(self):
        # A colored cell's RESET inside a selected row must not cut the
        # selection bar short -- the paint is re-armed after every inner reset.
        with mock.patch.object(git_roost, "COLOR", True):
            inner = git_roost.c("wings", git_roost.BLUE)
            out = git_roost.paint_over("a " + inner + " b", git_roost.SELECTED)
        self.assertTrue(out.startswith(git_roost.SELECTED))
        self.assertIn(git_roost.RESET + git_roost.SELECTED, out)
        self.assertTrue(out.endswith(git_roost.RESET))

    def test_changed_row_flashes_bold_not_magenta(self):
        # Magenta now means divergence; the change flash is liveness, carried
        # by bold plus the colorless * marker.
        rows = [renderable(repo="aa", path="/p/a", tracked=1)]
        with mock.patch.object(git_roost, "COLOR", True):
            out = git_roost.render(rows, width=200, changed={"/p/a"})
        row = next(l for l in out if "* " in l)
        self.assertTrue(row.startswith(git_roost.BOLD))
        self.assertNotIn(git_roost.MAGENTA, row)
        self.assertNotIn(git_roost.YELLOW, row)

    def test_truncation_notices_take_the_attention_color(self):
        lines = ["HEAD"] + ["r%d" % i for i in range(20)] + ["SUM"]
        with mock.patch.object(git_roost, "COLOR", True):
            out = git_roost.clip_to_height(lines, 6, focus=10)
        notices = [l for l in out if "more above" in l or "more below" in l]
        self.assertTrue(notices)
        for notice in notices:
            self.assertIn(git_roost.YELLOW, notice)
            self.assertNotIn(git_roost.DIM, notice)

    def test_detail_stash_overflow_notice_is_attention_colored(self):
        st = renderable(repo="a", tree="t", stashes=1)
        patch = "\n".join("line %d" % i for i in range(40))

        def fake_git(path, *args):
            if args[:2] == ("stash", "list"):
                return "stash@{0}: WIP on main"
            if args[:2] == ("stash", "show"):
                return patch
            return ""

        with mock.patch.object(git_roost, "git", fake_git), \
                mock.patch.object(git_roost, "COLOR", True):
            out = git_roost.detail_lines(st)
        notice = next(l for l in out if "more line(s)" in l)
        self.assertIn(git_roost.YELLOW, notice)


class TestAuthorDates(unittest.TestCase):
    """Ages come from the author date. A rebase rewrites every committer
    date, and a tool whose top bucket is literally "stuck mid-rebase" must
    not call a freshly rebased tree freshly worked-in."""

    AUTHORED = 1577836800  # 2020-01-01T00:00:00Z

    def commit_with_split_dates(self, repo):
        (repo / "f.txt").write_text("x\n")
        run(repo, "add", "f.txt")
        env = dict(
            os.environ,
            GIT_AUTHOR_DATE="2020-01-01T00:00:00 +0000",
            GIT_COMMITTER_DATE="2021-06-01T00:00:00 +0000",
        )
        subprocess.run(
            ("git", "commit", "-m", "split dates"), cwd=str(repo), env=env,
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @needs_git
    def test_tree_state_last_ts_is_the_author_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r", commit=False)
            self.commit_with_split_dates(repo)
            st = git_roost.tree_state(repo)
            self.assertEqual(st["last_ts"], self.AUTHORED)

    @needs_git
    def test_commit_feed_ages_use_the_author_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp) / "r", commit=False)
            self.commit_with_split_dates(repo)
            st = git_roost.tree_state(repo)
            rows = git_roost.commits_for(st, 5)
            self.assertEqual(rows[0]["ts"], self.AUTHORED)


class TestStatusLineShedding(unittest.TestCase):
    """The status line sheds chips in a designed order under width pressure;
    the clock and the quit/help hints survive every tier."""

    def line(self, width=None, **kw):
        return git_roost.status_line("recent", "all", False, 3.0,
                                     width=width, **kw)

    def test_no_width_never_sheds(self):
        line = self.line()
        for bit in ("view:table", "sort:recent", "filter:all",
                    "quiet:collapsed", "3s", "v" + git_roost.__version__):
            self.assertIn(bit, line)
        self.assertIn("[?] keys", line)
        self.assertIn("q quit", line)

    def test_a_width_that_fits_sheds_nothing(self):
        full = self.line()
        self.assertEqual(self.line(width=len(full)), full)

    def test_interval_is_shed_first(self):
        line = self.line(width=len(self.line()) - 1)
        self.assertNotIn("3s", line)
        self.assertIn("quiet:collapsed", line)
        self.assertIn("sort:recent", line)

    def test_version_outlives_every_mode_chip(self):
        # Wide enough for version + clock + hint only: every mode chip is
        # shed before the version, which is last on the shed list.
        width = (len("git-roost v%s" % git_roost.__version__) + 2 + 8 + 3
                 + len("[?] keys  q quit"))
        line = self.line(width=width)
        self.assertIn("v" + git_roost.__version__, line)
        for gone in ("view:", "sort:", "filter:", "quiet:"):
            self.assertNotIn(gone, line)

    def test_clock_and_hints_survive_the_last_tier(self):
        line = self.line(width=40)
        self.assertNotIn("v" + git_roost.__version__, line)
        self.assertRegex(line, r"\d\d:\d\d:\d\d")
        self.assertIn("[?] keys", line)
        self.assertIn("q quit", line)
        self.assertLessEqual(len(line), 40)


class TestNarrowRender(unittest.TestCase):
    """Degradation is designed, not overflowed: test at 40 columns, not 80."""

    def rows(self):
        # UNPUSHED and QUIET keep every count line short: a dirty row would
        # append "| N with uncommitted work" to the summary, which is a
        # different (footer) budget than the columns under test here.
        return [
            renderable(repo="wings", ahead=1, stashes=2),
            renderable(repo="roost", last_ts=1),
        ]

    def test_40_columns_sheds_the_stated_columns_and_never_overflows(self):
        out = git_roost.render(self.rows(), width=40, expand_quiet=True)
        header = out[0]
        for gone in ("STASH", "BRANCH", "TREE", "DRIFT"):
            self.assertNotIn(gone, header)
        for kept in ("REPO", "WORK", "LAST", "SUBJECT"):
            self.assertIn(kept, header)
        # Nothing writes into the last column. COLOR is off in the test
        # process, so len() is the visible length.
        for line in out:
            self.assertLessEqual(len(line), 40, repr(line))

    def test_intermediate_width_sheds_in_order_keeping_drift(self):
        # 55 columns is enough to keep DRIFT, the last column on the shed
        # list -- so STASH, BRANCH and TREE must go before it does.
        header = git_roost.render(self.rows(), width=55, expand_quiet=True)[0]
        self.assertIn("DRIFT", header)
        for gone in ("STASH", "BRANCH", "TREE"):
            self.assertNotIn(gone, header)

    def test_a_wide_render_keeps_every_column(self):
        header = git_roost.render(self.rows(), width=200, expand_quiet=True)[0]
        for h in ("REPO", "TREE", "BRANCH", "WORK", "DRIFT", "STASH", "LAST",
                  "SUBJECT"):
            self.assertIn(h, header)


class TestUnicodeDialect(unittest.TestCase):
    """The Unicode glyph tier: probed once at startup, watch-mode only.

    Two dialects, one vocabulary -- the semantics of every slot are identical,
    only the codepoints differ, and a frame must never mix them. Pipe-safe
    output (--once, --json, anything non-interactive) is always the ASCII
    dialect: every other test in this file runs on the module default and
    asserts those exact bytes, which is itself the byte-identity guarantee.
    """

    def setUp(self):
        self._color, self._ident = git_roost.COLOR, git_roost.IDENT

    def tearDown(self):
        # The dialect is module state; a test that switched it must not leak
        # Unicode into the ASCII assertions the rest of the file makes. The
        # main()-driving tests below also resolve COLOR/IDENT for a fake
        # TTY; those are module state too.
        git_roost.set_dialect(False)
        git_roost.COLOR, git_roost.IDENT = self._color, self._ident

    @staticmethod
    def stream(tty=True, encoding="utf-8"):
        s = mock.Mock()
        s.isatty.return_value = tty
        s.encoding = encoding
        return s

    # ------------------------------------------------------------- the probe

    def test_probe_accepts_an_interactive_utf8_stdout_on_posix(self):
        # A POSIX terminal reports its real locale encoding, so utf-8 on a
        # TTY is the honest signal there -- in every spelling Python uses.
        for enc in ("utf-8", "UTF-8", "utf8", "utf_8"):
            self.assertTrue(git_roost.unicode_capable(
                stream=self.stream(encoding=enc), env={}, platform="linux"),
                enc)
        self.assertTrue(git_roost.unicode_capable(
            stream=self.stream(), env={}, platform="darwin"))

    def test_probe_rejects_a_non_utf8_posix_terminal(self):
        self.assertFalse(git_roost.unicode_capable(
            stream=self.stream(encoding="latin-1"), env={}, platform="linux"))

    def test_probe_rejects_a_windows_console_outside_windows_terminal(self):
        # Since PEP 528 every Windows console stream reports utf-8 no matter
        # what code page the window is on, so the encoding cannot tell a
        # legacy conhost (boxes for arrows) from Windows Terminal. Without
        # WT_SESSION the answer on Windows is ASCII, utf-8 or not.
        self.assertFalse(git_roost.unicode_capable(
            stream=self.stream(encoding="utf-8"), env={}, platform="win32"))

    def test_probe_accepts_windows_terminal(self):
        # Windows Terminal always sets WT_SESSION and always renders the tier.
        self.assertTrue(git_roost.unicode_capable(
            stream=self.stream(encoding="utf-8"),
            env={"WT_SESSION": "3c1a"}, platform="win32"))
        # ...but it still has to be a UTF-8 stream, WT_SESSION or not.
        self.assertFalse(git_roost.unicode_capable(
            stream=self.stream(encoding="cp1252"),
            env={"WT_SESSION": "3c1a"}, platform="win32"))

    def test_probe_rejects_a_pipe_even_when_utf8(self):
        for platform in ("linux", "win32"):
            self.assertFalse(git_roost.unicode_capable(
                stream=self.stream(tty=False), env={"WT_SESSION": "3c1a"},
                platform=platform), platform)

    def test_probe_env_override_forces_ascii(self):
        self.assertFalse(git_roost.unicode_capable(
            stream=self.stream(), env={"GIT_ROOST_ASCII": "1"},
            platform="linux"))
        self.assertFalse(git_roost.unicode_capable(
            stream=self.stream(),
            env={"GIT_ROOST_ASCII": "1", "WT_SESSION": "3c1a"},
            platform="win32"))

    def test_probe_defaults_to_the_running_platform(self):
        # No `platform` argument means sys.platform -- the production call.
        with mock.patch.object(git_roost.sys, "platform", "win32"):
            self.assertFalse(git_roost.unicode_capable(
                stream=self.stream(), env={}))
            self.assertTrue(git_roost.unicode_capable(
                stream=self.stream(), env={"WT_SESSION": "x"}))
        with mock.patch.object(git_roost.sys, "platform", "linux"):
            self.assertTrue(git_roost.unicode_capable(
                stream=self.stream(), env={}))

    def test_non_watch_main_always_resets_to_ascii(self):
        # A leaked Unicode table from an earlier watch session in the same
        # process must not reach one-shot output: main() sets the dialect on
        # every path, and non-watch paths set it to ASCII unconditionally.
        git_roost.set_dialect(True)
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                git_roost.main(["--once", "--root", tmp])
        self.assertIs(git_roost.G, git_roost.ASCII_GLYPHS)
        self.assertTrue(all(ord(ch) < 128 for ch in buf.getvalue()))

    def test_check_on_an_interactive_terminal_stays_ascii(self):
        # --check is a scripted gate, and a hook runs it on whatever terminal
        # the shell has. It used to inherit --watch's default and pass the
        # dialect gate, so a diverged tree printed `↑1↓1` into a hook's log.
        # Force every probe to say yes; the gate must still say ASCII.
        git_roost.set_dialect(True)
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()), \
                 mock.patch.object(sys.stdout, "isatty", return_value=True), \
                 mock.patch.object(git_roost, "enable_windows_ansi",
                                   return_value=True), \
                 mock.patch.object(git_roost, "unicode_capable",
                                   return_value=True):
                rc = git_roost.main(["--check", "--root", tmp])
        self.assertEqual(rc, 0)
        self.assertIs(git_roost.G, git_roost.ASCII_GLYPHS)
        self.assertIn("clean", buf.getvalue())
        self.assertTrue(all(ord(ch) < 128 for ch in buf.getvalue()))

    def test_watch_on_a_capable_terminal_picks_unicode(self):
        # The positive half of the gate, so the --check case above is proved
        # to be the gate saying no rather than the probe never saying yes.
        # The gate runs before the loop; the loop's first act is scan(), so
        # a fake scan records the dialect and bails out via the loop's own
        # Ctrl-C exit path -- no subprocess, no terminal.
        seen = []

        def fake_scan(args, on_progress=None):
            seen.append(git_roost.G)
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()), \
                 redirect_stderr(io.StringIO()), \
                 mock.patch.object(sys.stdout, "isatty", return_value=True), \
                 mock.patch.object(git_roost, "enable_windows_ansi",
                                   return_value=True), \
                 mock.patch.object(git_roost, "unicode_capable",
                                   return_value=True), \
                 mock.patch.object(git_roost, "scan", fake_scan):
                git_roost.main(["-w", "1", "--root", tmp])
        self.assertEqual(seen, [git_roost.UNICODE_GLYPHS])

    @needs_git
    def test_oneshot_output_carries_no_unicode_even_from_a_utf8_stdout(self):
        # The dialect is watch-only: even on a box whose stdout would pass the
        # probe, a piped/one-shot render is byte-identical ASCII.
        git_roost.set_dialect(True)
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "r")
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                git_roost.main(["--once", "--root", tmp, "--no-color"])
            out = buf.getvalue()
        self.assertIn("r", out)
        self.assertTrue(all(ord(ch) < 128 for ch in out), repr(out))

    # ------------------------------------------------------ glyphs per dialect

    def test_drift_cell_in_both_dialects(self):
        st = {"base": "origin/main", "ahead": 2, "behind": 3}
        self.assertEqual(git_roost.drift(st), "^2v3")
        git_roost.set_dialect(True)
        self.assertEqual(git_roost.drift(st), "↑2↓3")  # up-2 down-3
        # `=` and `-` are already both dialects' spelling.
        self.assertEqual(git_roost.drift(
            {"base": "origin/main", "ahead": 0, "behind": 0}), "=")
        self.assertEqual(git_roost.drift({"base": "", "ahead": None}), "-")

    def test_github_cell_in_both_dialects(self):
        cases = {"success": ("+", "✓"), "failure": ("x", "✗"),
                 "pending": ("~", "○")}
        for ci, (a_mark, u_mark) in cases.items():
            s = {"pr_number": 7, "pr_draft": False, "pr_ci": ci}
            git_roost.set_dialect(False)
            self.assertEqual(git_roost.github_cell(s), "#7" + a_mark)
            git_roost.set_dialect(True)
            self.assertEqual(git_roost.github_cell(s), "#7" + u_mark)

    def test_work_cell_stays_ascii_in_the_unicode_dialect(self):
        # The WORK cell is the pipe-safe git vocabulary shared across the
        # flock -- identical in both dialects by design.
        git_roost.set_dialect(True)
        cell = git_roost.work(state(tracked=12, untracked=3))
        self.assertEqual(cell, "12+3?")

    def test_truncation_marker_in_both_dialects(self):
        lines = ["head"] + ["row %d" % i for i in range(30)] + ["summary"]
        out = git_roost.clip_to_height(lines, 6, focus=1)
        self.assertIn("... ", "\n".join(out))
        git_roost.set_dialect(True)
        out = git_roost.clip_to_height(lines, 6, focus=1)
        joined = "\n".join(out)
        self.assertIn("… ", joined)          # ellipsis + N more
        self.assertIn("more below  (j)", joined)  # still key-named
        self.assertNotIn("...", joined)           # no mixed frames

    def test_elide_uses_the_dialect_mark(self):
        self.assertEqual(git_roost.elide("abcdefgh", 6), "abc...")
        self.assertEqual(git_roost.elide("abc", 6), "abc")
        git_roost.set_dialect(True)
        self.assertEqual(git_roost.elide("abcdefgh", 6), "abcde…")

    def test_elide_name_keeps_both_ends_in_unicode(self):
        name = "wings/standardization-a1b2"
        self.assertEqual(git_roost.elide_name(name, 12), name[:12])
        git_roost.set_dialect(True)
        out = git_roost.elide_name(name, 12)
        self.assertEqual(len(out), 12)
        self.assertTrue(out.startswith("wings/"))
        self.assertTrue(out.endswith("a1b2"))
        self.assertIn("…", out)
        # A fitting name is untouched in both dialects.
        self.assertEqual(git_roost.elide_name("short", 12), "short")

    def test_ascii_safe_strips_data_but_not_dialect_glyphs(self):
        # Commit subjects stay stripped in both dialects -- prose is unvetted
        # -- but the active table's own glyphs must survive.
        git_roost.set_dialect(True)
        self.assertEqual(git_roost.ascii_safe("a—b"), "a?b")
        marks = "↑2↓3 … ✓✗○"
        self.assertEqual(git_roost.ascii_safe(marks), marks)
        git_roost.set_dialect(False)
        self.assertEqual(git_roost.ascii_safe("↑…"), "??")

    # -------------------------------------------------------------- the frame

    def test_overlay_frame_is_a_noop_in_ascii(self):
        lines = ["KEYS", "", "  q   quit"]
        self.assertEqual(git_roost.overlay_frame(lines, "help", 80, None), lines)

    def test_detail_overlay_is_framed_in_unicode(self):
        git_roost.set_dialect(True)
        st = renderable(repo="wings", tree="t")
        with mock.patch.object(git_roost, "git", return_value=None):
            out = git_roost.detail_lines(st, width=60, height=20)
        self.assertTrue(out[0].startswith("╭─ DETAIL ─"))
        self.assertTrue(out[0].endswith("╮"))
        self.assertTrue(out[-1].startswith("╰"))
        self.assertTrue(out[-1].endswith("╯"))
        for line in out[1:-1]:
            self.assertTrue(line.startswith("│ "), repr(line))
            self.assertTrue(line.endswith(" │"), repr(line))
        # Every row paints the same visible width -- a ragged right border is
        # the classic len()-vs-visible-length bug.
        widths = {git_roost.visible_len(line) for line in out}
        self.assertEqual(len(widths), 1, widths)

    def test_detail_overlay_is_flat_in_ascii(self):
        st = renderable(repo="wings", tree="t")
        with mock.patch.object(git_roost, "git", return_value=None):
            out = git_roost.detail_lines(st, width=60, height=20)
        self.assertTrue(all(ord(ch) < 128 for ch in "\n".join(out)))
        self.assertNotIn("╭", out[0])

    def test_help_overlay_is_framed_in_unicode_and_unmixed(self):
        git_roost.set_dialect(True)
        out = git_roost.overlay_frame(git_roost.help_lines(), "help", 80, 40)
        self.assertTrue(out[0].startswith("╭─ HELP ─"))
        joined = "\n".join(out)
        # The DRIFT legend speaks the dialect on screen -- describing `^2`
        # under a table that renders an up-arrow would be a mixed frame.
        self.assertIn("↑2", joined)
        self.assertNotIn("^2", joined)

    def test_help_legend_is_ascii_in_ascii(self):
        out = "\n".join(git_roost.help_lines())
        self.assertIn("^2", out)
        self.assertTrue(all(ord(ch) < 128 for ch in out))

    def test_frame_respects_the_height_budget(self):
        git_roost.set_dialect(True)
        lines = ["title"] + ["row %d" % i for i in range(50)] + ["foot"]
        out = git_roost.overlay_frame(lines, "detail", 60, 12)
        self.assertLessEqual(len(out), 12)

    def test_frame_never_writes_into_the_last_column(self):
        git_roost.set_dialect(True)
        out = git_roost.overlay_frame(["x" * 200], "detail", 60, 10)
        for line in out:
            self.assertLessEqual(git_roost.visible_len(line), 59, repr(line))


if __name__ == "__main__":
    unittest.main()
