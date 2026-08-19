# The demo recordings

The GIFs are recorded against a **synthetic fleet**, not against the machine
doing the recording. git-roost reads real repositories, so an unstaged recording
would put actual repo names, branch names and commit subjects into a public GIF.
`setup_fleet.py` builds nine invented repos and two linked worktrees under the
system temp dir instead, with local bare repos standing in for origins so
ahead/behind is measured against a real remote-tracking ref.

Staged data, real reads: git-roost itself runs unmodified and learns everything
from `git status --porcelain=v2` and the `.git` directory, exactly as it does
against a live box.

## Regenerating

Needs [vhs](https://github.com/charmbracelet/vhs) and ffmpeg, and a POSIX
terminal — vhs has no Windows build, so this step happens on Linux or macOS.
The fixture script itself is cross-platform and can be built and inspected
anywhere.

```bash
cd demo

python3 setup_fleet.py          # stage the fleet
vhs hero.tape                   # -> git-roost-demo.gif

python3 setup_fleet.py --live 45 &   # stage, then mutate on a schedule
vhs loop.tape                        # -> git-roost-loop.gif
wait

python3 setup_fleet.py --clean  # remove /tmp/git-roost-demo
```

`bin/git-roost` puts the working copy on `$PATH`, so the tapes record the tree
you are about to commit rather than whatever version is installed.

## What the fleet contains

Every group the table can show is exercised, because a fixture that skipped one
would let a regression in that branch of `bucket()` pass unnoticed:

| tree | group | how |
| --- | --- | --- |
| `tidewater-api/rebase-pilot` | MID-OPERATION | a real `git rebase` stopped on a conflict, in a `.claude/worktrees/` worktree |
| `lantern-ui` | MID-OPERATION | a real `git merge` stopped on a conflict |
| `kestrel-daemon` | DIVERGED | pushed, reset past the pushed tip, then recommitted — `^2v1` |
| `fogbank/field-notes` | UNCOMMITTED | a `.worktrees/` worktree with edits while its repo is quiet |
| `driftwood-cli` | UNCOMMITTED | tracked edits, untracked scratch, and one stash |
| `pier-nine` | UNPUSHED | three commits never pushed |
| `beacon-sim` | BEHIND | pushed four commits, then walked the branch back |
| `tidewater-api` | ACTIVE | clean, in sync, committed 20 minutes ago |
| `salt-marsh-docs`, `oyster-cache`, `fogbank` | QUIET | clean, in sync, last touched days ago |

The rebase and the merge are genuinely half-finished. git-roost detects those
from `.git/rebase-merge` and `.git/MERGE_HEAD`, so writing those paths by hand
would test the fixture rather than the tool — the script runs the conflicting
operations for real and raises if either one happens to succeed.

`fogbank` is the pair worth watching: the repo is collapsed under QUIET and
telling the truth about itself, while a worktree hanging off it carries
uncommitted work and gets its own row above. A per-repo tool shows one of those
two facts at a time.

## Where it leaves things

Everything lives under one root — `/tmp/git-roost-demo` on POSIX, `%TEMP%\git-roost-demo`
on Windows, overridable with `GIT_ROOST_DEMO_ROOT`. Inside it:

- `fleet/` — the scan root you hand to `--root`
- `origins/` — the bare repos, deliberately a sibling so the scan never walks them

And beside the root, `/tmp/git-roost-demo-go` — the marker the loop tape touches to
start `--live`'s schedule. It sits outside the root because staging wipes the
root, and the tape may touch it while staging is still running.

The fleet is **left on disk** when the script exits; the recording needs it to
still be there. Every run wipes the root before rebuilding, so a stale fleet
never accumulates, and `--clean` removes it outright.
