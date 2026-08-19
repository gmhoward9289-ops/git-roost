Output git-roost-loop.gif

Set Shell "bash"
Set FontSize 13
Set Width 1400
Set Height 560
Set Padding 14

# Touching the marker is what starts setup_fleet.py --live's schedule, so the
# transitions land on camera instead of during vhs's warm-up.
Hide
Type "export PATH=$PWD/bin:$PATH && touch /tmp/git-roost-demo-go && clear"
Enter
Sleep 500ms
Show

Type@120ms "git-roost -w 2 --root /tmp/git-roost-demo/fleet"
Sleep 600ms
Enter

# t+5s  salt-marsh-docs goes dirty and climbs out of the collapsed QUIET line
Sleep 10s

# expand QUIET so the tree it just left is visible too
Type "a"
Sleep 12s

# t+21s it gets pushed and lands in ACTIVE; t+29s pier-nine goes dirty
Sleep 14s

Type "q"
Sleep 1s
