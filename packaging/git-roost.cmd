@echo off
setlocal

rem Windows portable wrapper for the winget package. This is the batch sibling
rem of bin/git-roost.js: same job (find a Python, run git_roost.py with it,
rem stay out of the way), different runtime, because winget has nothing like
rem npm's postinstall or brew's `depends_on` -- a "zip"/"portable" winget
rem package is just this file plus git_roost.py dropped on disk and symlinked
rem onto PATH, so whatever finds Python has to be this script.
rem
rem It deliberately skips the JS wrapper's version probe (rejecting a found-
rem but-too-old interpreter with a specific message). Doing that reliably in
rem batch means shelling out to the interpreter anyway just to ask its version,
rem which is most of the cost of just running the real script and letting a
rem genuinely too-old Python fail on its own -- git_roost.py is stdlib-only, so
rem that failure is a normal Python traceback, not a silent hang. The trade is
rem a worse message on the rare too-old-Python path in exchange for not
rem duplicating interpreter-selection logic in two languages.
rem
rem Order matches bin/git-roost.js's Windows branch: `py -3` first, because it
rem exists even when `python` resolves to the Microsoft Store app-execution
rem alias -- a stub that opens the Store and exits 9009 rather than running
rem anything. That stub is *why* `python`/`python3` are tried after, not
rem before: if `py` is genuinely absent (rare but real on a minimal box) they
rem are still worth a shot.

set "HERE=%~dp0"
set "SCRIPT=%HERE%git_roost.py"

where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
  py -3 "%SCRIPT%" %*
  exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if %ERRORLEVEL% equ 0 (
  python "%SCRIPT%" %*
  exit /b %ERRORLEVEL%
)

where python3 >nul 2>nul
if %ERRORLEVEL% equ 0 (
  python3 "%SCRIPT%" %*
  exit /b %ERRORLEVEL%
)

echo git-roost needs Python 3.9 or newer on PATH (tried: py -3, python, python3) 1>&2
echo   https://www.python.org/downloads/ 1>&2
exit /b 127
