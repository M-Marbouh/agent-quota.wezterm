# Contributing

Contributions are welcome.

## Development Setup

On a Unix-like development host, clone the repository and run the credential-free checks first:

```bash
git clone https://github.com/M-Marbouh/agent-quota.wezterm.git
cd agent-quota.wezterm
python3 -m py_compile codex-limits.py tmux-quota.py
python3 -m unittest discover -s tests -v
python3 tmux-quota.py --tmux-command
python3 tmux-quota.py --tmux-command --unicode
```

The `--tmux-command` smoke checks only print a command fragment; they do not call provider CLIs or read account data. The test suite uses fixtures and isolated tmux servers.

For WezTerm development, load the checkout from your `~/.wezterm.lua` with `dofile(...)`:

```lua
local quota = dofile(os.getenv("HOME") .. "/path/to/agent-quota.wezterm/plugin/init.lua")
quota.apply_to_config(config)
```

For normal usage, prefer the published plugin install in the README.

For standalone tmux development or use, the tmux host needs Python 3, tmux, and the provider CLI(s) being displayed. Add the generated command to `status-right`; ASCII is the default, and `--unicode` opts into icons and Unicode bars. The [README](README.md#standalone-tmux) shows a reload-safe append example and explains integration with themes that replace `status-right`.

## Project Files

- `plugin/init.lua`: main WezTerm plugin logic
- `codex-limits.py`: bundled Codex helper
- `tmux-quota.py`: optional standalone tmux status command
- `README.md`: user-facing documentation

## Checks

Run the compile and unit-test commands in Development Setup before opening a pull request. Running `python3 codex-limits.py` directly starts the installed Codex app-server and can make authenticated usage reads, even without an interactive Codex session. Keep this as an intentional manual check using an already-authenticated development account, and distinguish it from fixture-only checks. Never publish tokens, credentials, or account-specific cache data.

For changes to the WezTerm UI, reload WezTerm and manually check:

- neither Claude nor Codex running
- Claude only
- Codex only
- both running
- multiple WezTerm windows sharing the cache

For tmux rendering changes, use the isolated-server tests and add a status sample or screenshot to the pull request. Test both default ASCII and opt-in `--unicode`; Unicode mode uses standard glyphs and does not need a Nerd Font. Tmux runs the command on the tmux host, so remote/headless installations need the provider CLI and existing authentication on that host. Tmux tests use an isolated server and clean it up when complete.

## Style

- Lua uses 2-space indentation
- Python uses 4 spaces
- use `snake_case` for local variables and helper functions
- keep fetching logic separate from rendering logic

## Pull Requests

- keep each pull request scoped to one concern
- include a short summary of behavior changes
- include manual test notes
- include a screenshot or status-bar sample when UI output changes

## Security

Do not commit tokens, credential files, or private machine-specific data. Runtime credentials come from:

- `~/.claude/.credentials.json`
- `~/.codex/auth.json`
