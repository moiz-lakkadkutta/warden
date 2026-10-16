# Warden README hero: doctor -> run --demo -> console, offline, no credentials.
#
# Render from the repo root (needs `brew install vhs`, or the charmbracelet/vhs-action in CI):
#   uv sync && vhs docs/demo/hero.tape        # writes docs/demo/hero.gif (~0.4 MB)
#
# The hidden setup puts the repo's `warden` on PATH and moves to an empty temp dir, so doctor
# sees no .env (the ✗ lines are real: the demo needs no Nebius, Sandboxes or Tavily key) and the
# demo run lands in a throwaway runs/. Everything typed after `Show` is what a judge would type.
# The demo replays the bundled #201 run on the fake sandbox: its $ figures are that recording's,
# not a live spend. Typed text stays ASCII: VHS drops characters like the arrow when typing.

Output docs/demo/hero.gif

Set Shell bash
Set FontSize 16
Set Width 1400
Set Height 720
Set Padding 24
Set Framerate 12
Set TypingSpeed 40ms
Set Theme "GitHub Dark"

Hide
Type 'export PATH="$PWD/.venv/bin:$PATH" PS1="\[\e[2m\]\$\[\e[0m\] " && unset NEBIUS_API_KEY NEBIUS_PROJECT_ID CONTREE_BASE_URL TAVILY_API_KEY && cd "$(mktemp -d)" && clear'
Enter
Sleep 500ms
Show

Type "# 1. what does Warden need? (no network call, no keys in this shell)"
Enter
Type "warden doctor"
Sleep 300ms
Enter
Sleep 5s

Hide
Type "clear"
Enter
Show
Type "# 2. the whole hunt -> plan -> fix -> verify -> PR loop on a fake sandbox"
Enter
Type "warden run --demo"
Sleep 300ms
Enter
Sleep 7s

Hide
Type "clear"
Enter
Show
Type "# 3. the run's timeline, branch tree and evidence table as one HTML page"
Enter
Type "warden console"
Sleep 300ms
Enter
Sleep 4s
Type "# the same console, live on a Nebius Serverless AI endpoint:"
Enter
Type "# https://port8080-cs9x5ndsvxz54x7.tunnel.applications.eu-west2.nebius.cloud/"
Enter
Sleep 4s
