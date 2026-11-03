# `hail tui` demo, recorded with vhs (https://github.com/charmbracelet/vhs) on a made-up journal.
# From the repo root: vhs docs/demo/tui.tape   (needs uv, ttyd and ffmpeg on PATH)
Output docs/tui.gif
Require uv

Set Shell "powershell"
Set FontSize 16
Set Width 1400
Set Height 640
Set Padding 12
Set Theme "Catppuccin Mocha"
Set TypingSpeed 60ms

# throwaway home with a fictional journal; `hail` runs this checkout
Hide
Type "$d = Join-Path $env:TEMP 'hail-demo'; if (Test-Path $d) { Remove-Item -Recurse -Force $d }; $null = New-Item -ItemType Directory $d"
Enter
Type "$env:HAIL_HOME = Join-Path $d 'home'; $env:HAIL_CONFIG = Join-Path $d 'agents.toml'; uv run -q python docs/demo/seed.py"
Enter
Type "function hail { uv run -q hail @args }; Clear-Host"
Enter
Sleep 6s
Show

Type "hail tui"
Sleep 400ms
Enter
Sleep 4s

# move to a thread and open it
Type "j"
Sleep 700ms
Enter
Sleep 2.5s

# reply mode, then cancel (nothing is sent in the demo)
Type "r"
Sleep 600ms
Type "rerun it with -vvv"
Sleep 900ms
Escape
Sleep 1s

# folders filter the threads
Type "0"
Sleep 600ms
Type "j"
Sleep 900ms
Type "j"
Sleep 1.5s
Type "k"
Sleep 400ms
Type "k"
Sleep 600ms
Type "1"
Sleep 600ms

# close the thread: back to the logo
Escape
Sleep 3.5s
Type "q"
Sleep 600ms
