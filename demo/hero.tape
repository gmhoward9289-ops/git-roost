Output git-roost-demo.gif

Set Shell "bash"
Set FontSize 13
Set Width 1400
Set Height 620
Set Padding 14

Hide
Type "export PATH=$PWD/bin:$PATH FLEET=/tmp/git-roost-demo/fleet && clear"
Enter
Sleep 500ms
Show

Type@120ms "git-roost --root $FLEET"
Sleep 600ms
Enter
Sleep 8s

Hide
Type "clear"
Enter
Sleep 300ms
Show

Type@120ms "git-roost --root $FLEET --all"
Sleep 600ms
Enter
Sleep 7s

Hide
Type "clear"
Enter
Sleep 300ms
Show

Type@120ms "git-roost --root $FLEET --log 12"
Sleep 600ms
Enter
Sleep 6s
