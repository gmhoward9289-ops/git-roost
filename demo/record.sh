#!/usr/bin/env bash
# Record the README GIFs. Run from WSL/Linux:  demo/record.sh
# Copies the working tree onto ext4 so Windows CRLF cannot break shebangs.
set -euo pipefail

src="$(cd "$(dirname "$0")/.." && pwd)"
rec=/tmp/git-roost-rec

rm -rf "$rec"
mkdir -p "$rec/demo/bin"
python3 - <<PY
from pathlib import Path
src = Path("$src")
rec = Path("$rec")
files = {
    src / "git_roost.py": rec / "git_roost.py",
    src / "demo" / "bin" / "git-roost": rec / "demo" / "bin" / "git-roost",
    src / "demo" / "hero.tape": rec / "demo" / "hero.tape",
    src / "demo" / "loop.tape": rec / "demo" / "loop.tape",
    src / "demo" / "setup_fleet.py": rec / "demo" / "setup_fleet.py",
}
for a, b in files.items():
    b.write_bytes(a.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
    print("copied", b)
PY
chmod +x "$rec/demo/bin/git-roost"

cd "$rec/demo"
"$rec/demo/bin/git-roost" --version

python3 setup_fleet.py
echo "=== recording hero ==="
vhs hero.tape
echo "=== recording loop ==="
python3 setup_fleet.py --live 45 &
live=$!
# Give staging a head start so the fleet exists before vhs types the command.
sleep 3
vhs loop.tape
wait "$live" || true

python3 - <<PY
from pathlib import Path
src = Path("$src") / "demo"
rec = Path("$rec") / "demo"
for name in ("git-roost-demo.gif", "git-roost-loop.gif"):
    data = (rec / name).read_bytes()
    (src / name).write_bytes(data)
    print("wrote", src / name, "bytes", len(data))
PY

python3 setup_fleet.py --clean
echo "done"
