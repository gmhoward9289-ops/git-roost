#!/usr/bin/env node
// npm's job here is delivery, not reimplementation. git-roost is one stdlib-only
// Python file and stays that way; this shim only finds a Python to run it with
// and gets out of the way.
//
// npm is here because it is the lowest-friction channel for someone who already
// has Node: `npx git-roost` tries the tool without committing to pipx or brew.
// Unlike roost -- which had to publish as roost-top because the bare name was
// held -- git-roost is git-roost on npm too, so nothing about this package needs
// to explain an alias.
//
// Windows matters more here than it does for leghorn. leghorn is curses and so
// needs the windows-curses *pip* dependency, which npm cannot deliver; that is
// why its package.json carries an "os" field excluding Windows. git-roost has no
// dependencies at all (see pyproject.toml) and supports Windows outright, so
// there is no "os" field and the Windows interpreter probe below is a real code
// path, not a courtesy.
//
// The bin name is exactly `git-roost`, which is also what makes `git roost`
// work: git dispatches an unknown subcommand to a `git-<name>` executable on
// PATH, and a global npm install puts one there (git-roost.cmd on Windows).
// Renaming the bin would silently drop that.
//
// There is deliberately no postinstall Python check. A failing postinstall would
// break `npm ci` in a project that merely lists git-roost as a devDependency; a
// missing interpreter is a run-time problem, so it is reported at run time.

'use strict';

const os = require('os');
const path = require('path');
const { spawn, spawnSync } = require('child_process');

const SCRIPT = path.join(__dirname, '..', 'git_roost.py');
const MIN = [3, 9]; // matches requires-python in pyproject.toml

// On Windows the py launcher goes first: it exists even when `python` resolves
// to the Microsoft Store app-execution alias, which is a stub that opens the
// Store and never runs anything. The probe below rejects that stub anyway
// (it prints no version), but trying py first avoids the detour.
const CANDIDATES =
  process.platform === 'win32'
    ? [['py', ['-3']], ['python', []], ['python3', []]]
    : [['python3', []], ['python', []]];

const PROBE = 'import sys; print("%d.%d" % sys.version_info[:2])';

function probe(cmd, pre) {
  const r = spawnSync(cmd, pre.concat(['-c', PROBE]), {
    encoding: 'utf8',
    windowsHide: true,
  });
  if (r.error || r.status !== 0) return null;
  const m = /^(\d+)\.(\d+)/.exec((r.stdout || '').trim());
  if (!m) return null;
  return [Number(m[1]), Number(m[2])];
}

function tooOld(v) {
  return v[0] < MIN[0] || (v[0] === MIN[0] && v[1] < MIN[1]);
}

let chosen = null;
const rejected = [];

for (const [cmd, pre] of CANDIDATES) {
  const v = probe(cmd, pre);
  if (!v) continue;
  if (tooOld(v)) {
    rejected.push(`${[cmd].concat(pre).join(' ')} is ${v[0]}.${v[1]}`);
    continue;
  }
  chosen = { cmd, pre };
  break;
}

// "found only 3.8" and "found nothing" are different problems with different
// fixes, so they get different sentences -- telling someone to install Python
// when they already have 3.8 sends them looking in the wrong place.
if (!chosen) {
  const names = CANDIDATES.map(([c, p]) => [c].concat(p).join(' ')).join(', ');
  process.stderr.write(
    rejected.length
      ? `git-roost needs Python ${MIN.join('.')} or newer; found only: ${rejected.join(', ')}\n`
      : `git-roost needs Python ${MIN.join('.')} or newer on PATH (tried: ${names})\n`
  );
  process.stderr.write('  https://www.python.org/downloads/\n');
  process.exit(127);
}

// The child owns the terminal. git-roost's watch loop redraws in place with
// \033[H\033[2J and catches KeyboardInterrupt itself, exiting 0 after the frame
// it is in. If this process died on Ctrl-C first, the shell would print its
// prompt while an orphaned child was still writing frames over it -- so signals
// are swallowed here and the child is left to quit on its own.
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  try {
    process.on(sig, () => {});
  } catch (e) {
    // not every signal exists on every platform; skip the ones that don't
  }
}

const child = spawn(chosen.cmd, chosen.pre.concat([SCRIPT], process.argv.slice(2)), {
  stdio: 'inherit',
  windowsHide: true,
});

child.on('error', (err) => {
  process.stderr.write(`git-roost: could not run ${chosen.cmd}: ${err.message}\n`);
  process.exit(127);
});

// Exit status is data here: `git-roost --json | ...` and scripted use both read
// it, so the child's code is passed through rather than collapsed to 0/1, and a
// signal death is reported the way a shell does (128 + signo).
child.on('exit', (code, signal) => {
  if (signal) {
    process.exit(128 + (os.constants.signals[signal] || 0));
  }
  process.exit(code === null ? 1 : code);
});
