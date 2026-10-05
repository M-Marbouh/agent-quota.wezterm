# Contributing

Contributions are welcome.

## Development Setup

Clone the repo locally and load it from your `~/.wezterm.lua` with `dofile(...)` while developing:

```lua
local quota = dofile(os.getenv("HOME") .. "/path/to/agent-quota.wezterm/plugin/init.lua")
quota.apply_to_config(config)
```

For normal usage, prefer the published plugin install in the README.

## Project Files

- `plugin/init.lua`: main WezTerm plugin logic
- `codex-limits.py`: bundled Codex helper
- `tmux-quota.py`: optional standalone tmux status command
- `README.md`: user-facing documentation

## Checks

Run these before opening a pull request:

```bash
python3 -m py_compile codex-limits.py
python3 -m py_compile tmux-quota.py
python3 -m unittest discover -s tests -v
python3 codex-limits.py
ls -l /tmp/wezterm-quota-limit-"$USER"-*.json
```

Also reload WezTerm and manually test:

- neither Claude nor Codex running
- Claude only
- Codex only
- both running
- multiple WezTerm windows sharing the cache

The tmux command can be used on its own from any terminal, including a remote SSH session attached to a tmux server. Add it with `set-option -ag status-right ...` to append to the current right-side status. Tmux itself must run on the machine with Python 3 and the provider CLI installations. Tmux tests use an isolated server and clean it up when complete.

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
