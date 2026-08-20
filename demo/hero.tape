Output git-roost-demo.gif

Set Shell "bash"
Set FontSize 13
Set Width 1400
Set Height 680
Set Padding 14

# Watch-mode tour of the current binary (cursor, detail, keymap, log).
# A one-shot-then-watch tape used to end after the first command; keep
# this on a single -w process so vhs records the whole thing.
Hide
Type "export PATH=$PWD/bin:$PATH FLEET=/tmp/git-roost-demo/fleet && clear"
Enter
Sleep 500ms
Show

Type@120ms "git-roost -w 2 --root $FLEET"
Sleep 600ms
Enter
Sleep 4s

# `>` marks the cursor. j walks it; enter opens that tree's detail.
Type "j"
Sleep 700ms
Type "j"
Sleep 1s
Enter
Sleep 5s

Type "x"
Sleep 800ms

Type "?"
Sleep 4s

Type "x"
Sleep 800ms

Type "l"
Sleep 5s

Type "q"
Sleep 1s
