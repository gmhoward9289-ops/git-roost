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

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
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


def renderable(**kw):
    """A state complete enough for render(), which needs more than bucket() does."""
    base = dict(state(), branch="main", detached=False, base="origin/main",
                stashes=0, last_subject="s", conflicts=0, common_dir="/c/%s" % kw.get("repo", "r"),
                path="/p/%s" % kw.get("tree", "t"))
    base.update(kw)
    base["common_dir"] = "/c/%s" % base["repo"]
    return base


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
        self.assertEqual(documented, {"?", "r", "s", "f", "a", "q"})

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

    def test_r_and_unbound_keys_change_nothing(self):
        # 'r' is a redraw, which the loop does anyway on falling through. The
        # test exists so a future edit cannot quietly give it a side effect.
        for key in ("r", "R", "x", "5", " "):
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
            repo = None
            filter = "all"

        with tempfile.TemporaryDirectory() as tmp:
            args = Args()
            args.root = [tmp]
            view = {"sort": "repo", "filter": "dirty", "quiet": True}
            self.assertEqual(git_roost.body(args, 200, view),
                             ["no git repositories found"])

    def test_body_without_a_view_renders_the_one_shot_table(self):
        # The contract behind `git-roost | less` and `git-roost --json | jq`:
        # passing no view must produce exactly what the flags alone produce.
        class Args:
            json = False
            log = None
            all = False
            root = None
            depth = git_roost.DEFAULT_DEPTH
            repo = None
            filter = "all"

        with tempfile.TemporaryDirectory() as tmp:
            args = Args()
            args.root = [tmp]
            self.assertEqual(git_roost.body(args, 200), ["no git repositories found"])


class TestCli(unittest.TestCase):
    def test_version_exits_clean(self):
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                git_roost.main(["--version"])
        self.assertEqual(cm.exception.code, 0)

    def test_runs_with_zero_repositories(self):
        # The contract with packaging: --help and a bare run must work on a box
        # that has no repos at all.
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = git_roost.main(["--root", tmp])
            self.assertEqual(rc, 0)
            self.assertIn("no git repositories found", buf.getvalue())

    @needs_git
    def test_json_is_valid_and_machine_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--json"])
            data = json.loads(buf.getvalue())
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["repo"], "alpha")

    @needs_git
    def test_json_record_keys_are_a_stable_contract(self):
        # ccboard (leghorn's data layer) consumes `git-roost --json`. Adding a
        # key is safe; renaming or removing one breaks a consumer this suite
        # cannot see. If you are changing the shape deliberately, update this
        # set and ccboard's gather_git() together. assertEqual, not a subset
        # check: a subset check passes when a key is deleted, which is exactly
        # the case that breaks the consumer.
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
            data = json.loads(buf.getvalue())
            self.assertEqual(set(data[0]), EXPECTED)

    @needs_git
    def test_repo_flag_scopes_to_a_substring_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_repo(Path(tmp) / "alpha")
            make_repo(Path(tmp) / "beta")
            buf = io.StringIO()
            with redirect_stdout(buf):
                git_roost.main(["--root", tmp, "--repo", "alp", "--json"])
            data = json.loads(buf.getvalue())
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
            data = json.loads(buf.getvalue())
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
            data = json.loads(buf.getvalue())
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
            data = json.loads(buf.getvalue())
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


if __name__ == "__main__":
    unittest.main()
