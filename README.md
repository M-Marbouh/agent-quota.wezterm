# Agent Quota for WezTerm

![WezTerm plugin](https://img.shields.io/badge/WezTerm-plugin-blue)

[Listed on Awesome WezTerm](https://github.com/michaelbrusegard/awesome-wezterm#ai)

A WezTerm plugin that shows Claude and Codex quota usage directly in the status bar.
An optional standalone tmux status command provides the same account view on the machine running tmux, including remote and headless tmux servers.

It displays live usage windows returned by each provider, reset countdowns, process-aware `not running` states, compact percentage bars, and a shared cache so multiple WezTerm windows do not all refresh the same data independently.

![Agent Quota status bar sample](assets/status-sample.svg)

## Features

- Claude 5-hour and 7-day utilization
- Codex utilization windows returned by the account
- Reset countdowns for both providers
- Compact 8-cell percentage bars
- Process-aware `not running` status for Claude and Codex
- Shared per-user cache in `/tmp` across WezTerm instances
- Bundled Codex helper auto-discovery with no manual script-path setup
- Optional Claude usage dashboard shortcut
- Configurable status-bar side, icons, polling interval, and bar glyphs
- Optional tmux integration with an independent cache and no WezTerm dependency

Example output:

```text
Claude: 5h ███░░░░░ 42% (2h31m)  ▪ 7d █░░░░░░░ 18% (4d12h)  |  Codex: 5h ███████░ 88% (2h10m)  ▪ 7d ███░░░░░ 32% (1d4h)
```

## Requirements

Choose the requirements for the integration you use; the standalone tmux command does not require WezTerm.

### WezTerm plugin

- [WezTerm](https://wezterm.org/)
- Python 3 (`python3` on Linux, or `python`/`python3` on Windows)
- `curl`; Linux also needs `pgrep`, `ps`, `mkdir`, `rmdir`, and GNU `stat`; Windows uses bundled `tasklist` for process detection
- [Claude Code](https://code.claude.com/docs/en/setup) and/or [Codex CLI](https://github.com/openai/codex) installed and authenticated for the provider(s) you want to display

### Standalone tmux command

- A Unix-like host with tmux, Python 3, and `ps`
- Git for the clone instructions below
- [Claude Code](https://code.claude.com/docs/en/setup) and/or [Codex CLI](https://github.com/openai/codex) installed and authenticated on that same host for the provider(s) you want to display
- Network access when usage data needs refreshing
- For Unicode output, a UTF-8 terminal and fonts containing the displayed glyphs; ASCII output is available without those glyphs

Tmux checks for provider processes and reads credentials on the tmux host. Claude usage uses the existing `~/.claude/.credentials.json`; Codex usage uses the installed `codex` CLI and its existing authentication. The helper does not sign in or refresh credentials. If you use only one provider, the other shows `not running`.

On Debian/Ubuntu, missing WezTerm integration utilities can be installed with `sudo apt install python3 curl procps coreutils`. For the standalone tmux setup, use `sudo apt install git python3 tmux procps` if these tools are missing.

## Installation

Choose the [WezTerm plugin](#wezterm-plugin-installation) or [standalone tmux](#standalone-tmux) instructions.

### WezTerm plugin installation

Install it with WezTerm's plugin loader:

```lua
local wezterm = require("wezterm")
local config = wezterm.config_builder()

local quota = wezterm.plugin.require("https://github.com/M-Marbouh/agent-quota.wezterm")

quota.apply_to_config(config)

return config
```

Reload WezTerm with `CTRL+SHIFT+R`, or restart WezTerm fully.

No extra Python path configuration is required. The plugin resolves its bundled `codex-limits.py` helper automatically.

### Standalone tmux

This integration is optional and works without WezTerm or CCB. Install the helper on the machine where the tmux server runs. For a remote or headless server, the provider CLIs, their existing authentication, Python, and this repository all belong on that host; the SSH client only displays the host's tmux status line. The helper looks for `codex` in common locations, including `~/.local/bin`, if tmux has a small `PATH`.

Clone the repository on the tmux host:

```sh
git clone https://github.com/M-Marbouh/agent-quota.wezterm.git
cd agent-quota.wezterm
```

Keep `tmux-quota.py` and `codex-limits.py` together in the checkout. Add the following to `~/.tmux.conf`, replacing the path with your clone location. It preserves existing `status-right` content and avoids duplicate appends on reload. The example enables `--unicode` icons and block bars on a readable dark background; omit that flag for the default ASCII output:

```tmux
set-option -g status-style 'bg=#1e1e2e,fg=#cdd6f4'
set-option -g status-right-length 180
if-shell -F '#{m:*tmux-quota.py*,#{status-right}}' '' 'set-option -ag status-right "  #(python3 /absolute/path/to/agent-quota.wezterm/tmux-quota.py --unicode)"'
```

Replace `/absolute/path/to` with the actual clone location. Start `tmux` to load the configuration. If the tmux server is already running, apply the changes with `tmux source-file ~/.tmux.conf`.

When upgrading an existing setup, update the checkout first, then edit the existing helper command to add `--unicode`; the duplicate check intentionally keeps an existing command. Keep one helper invocation in `status-right`.

For an installation path containing spaces, run this from the cloned repository to print a shell-quoted command fragment:

```sh
python3 tmux-quota.py --tmux-command --unicode
```

The command prints a shell fragment; it does not escape the surrounding `tmux.conf` string. Use the fragment in the reload-safe configuration above. A single append with a spaced path has this form:

```tmux
set-option -ag status-right "  #(python3 '/absolute/path with spaces/agent-quota.wezterm/tmux-quota.py' --unicode)"
```

This form supports spaces in the installation path. Keep quote characters out of the path because they need additional escaping for `tmux.conf`. The `--tmux-command` option prints only the shell command fragment; it does not contact either provider.

`set-option -ag` preserves the current status content. The default `status-right-length` of 40 is too short for both providers, so the example raises it to 180 characters; increase it further if your existing status content is long. Tmux clips status content on narrow terminals, so both providers may not fit when the attached terminal is narrow. Tmux refreshes the command on its normal `status-interval` (15 seconds by default); account requests use a separate per-user cache and are limited to once per minute on success, with persisted retry delays after failures. The cache lives in `~/.cache/wezterm-quota-limit-tmux` with private directory and file permissions. It does not read or overwrite the WezTerm cache in `/tmp`.

Default ASCII/minimal-font output (omit `--unicode`):

```text
C Claude: 5h [###-----] 42% (2h31m)  |  X Codex: 5h [#######-] 88% (2h10m)  7d [###-----] 32% (1d4h)
```

Unicode status-bar sample (filled bars use green below 50%, gold from 50% to 79%, and red from 80%; empty cells and reset countdowns use subdued, readable colors):

```text
⚡ Claude: 5h ███░░░░░ 42% (2h31m) · 7d █░░░░░░░ 18% (4d12h) · ✦ Codex: 5h ███████░ 88% (2h10m) · 7d ███░░░░░ 32% (1d4h)
```

Tmux uses its native `#[...]` styles. The default ASCII bars remain available for minimal terminal fonts by omitting `--unicode`; the Unicode mode uses standard glyphs and does not require a Nerd Font. Each provider is fetched and cached independently; inactive CLIs show `not running`, temporary failures show a stale marker when safe cached data remains, and data past a reset is discarded until a fresh read succeeds.

Other tmux themes or CCB status scripts may replace `status-right` after this command is added. In that case, put the helper fragment into the theme's own `status-right` definition, preserving its existing content and using the same duplicate check when the config can be reloaded. Increase `status-right-length` if the bar is clipped; the needed width depends on your other widgets and terminal size.

## Configuration

For the WezTerm plugin, pass options to `apply_to_config(config, opts)`:

```lua
quota.apply_to_config(config, {
  poll_interval_secs = 120,
  position = "left",
  dashboard_key = { key = "u", mods = "CTRL|SHIFT" },
  compact = false,
  icons = {
    claude = "⚡",
    codex  = "✦",
    week   = "▪",
  },
  bars = {
    enabled = true,
    width = 8,
    full = "█",
    empty = "░",
  },
  -- codex_script = "/absolute/path/to/codex-limits.py",
})
```

Options:

- `poll_interval_secs`: refresh interval for successful reads. Default: `60`
- `position`: `"left"` or `"right"`. Default: `"right"`
- `dashboard_key`: opens the Claude usage dashboard. Default: `CTRL+SHIFT+U`
- `compact`: hide reset countdowns to shrink the status-bar footprint. Default: `false`
- `icons.claude`: Claude prefix icon. Default: `⚡` (override to taste, e.g. `▲`)
- `icons.codex`: Codex prefix icon. Default: `✦` (override to taste, e.g. `◆`)
- `icons.week`: separator between Codex usage windows
- `bars.enabled`: show compact percentage bars
- `bars.width`: number of bar cells
- `bars.full` / `bars.empty`: glyphs used for the bar
- `codex_script`: absolute path to `codex-limits.py`, overriding auto-discovery. Default: `nil` (auto-detect)

### Compact mode

Set `compact = true` to drop the reset countdowns from the status bar. Usage percentages and bars are kept, only the trailing `(2h31m)` style reset timers are hidden. This is useful on narrow terminals or when the status bar shares space with other widgets.

```lua
quota.apply_to_config(config, { compact = true })
```

### Codex script path

The plugin resolves its bundled `codex-limits.py` helper automatically. If auto-discovery fails in a custom setup, point `codex_script` at the helper directly:

```lua
quota.apply_to_config(config, {
  codex_script = "/absolute/path/to/codex-limits.py",
})
```

The path is resolved lazily on first use, so an incorrect value only affects the Codex side and never blocks Claude display.

## Windows

The plugin runs on native Windows WezTerm. Platform differences are handled automatically:

- Codex process detection uses `tasklist`; Claude process detection inspects command lines so the Chrome native host can be excluded.
- The shared cache is written to the user temp directory (`%TEMP%`) instead of `/tmp`.
- Python is invoked as `python` when `python3` is not on `PATH`.

### Process detection

Claude and Codex fetching is gated on the corresponding CLI actually running, so process detection matters. Both tools install as npm shims but launch a bundled **native** binary as a child process. Claude's Chrome native host is ignored so it does not count as Claude Code:

- Claude Code runs as `claude.exe`
- Codex runs as `codex.exe` (the `node.exe` launcher spawns the native binary from its `vendor` directory)

The image name stays the same on both x64 and arm64, so `IMAGENAME eq claude.exe` / `codex.exe` matches on either architecture. You can confirm a tool is detectable while it is running with:

```powershell
tasklist /FI "IMAGENAME eq claude.exe"
tasklist /FI "IMAGENAME eq codex.exe"
```

If either command shows no process while the CLI is open, the status bar will show `not running` for that side.

## How It Works

Claude:

- reads the OAuth token from `~/.claude/.credentials.json`
- scopes `accessToken` and `expiresAt` to the `claudeAiOauth` block, avoiding unrelated expiry fields such as `discoveryState.expiresAt`
- calls the Anthropic OAuth usage endpoint
- preserves stale data and backs off on repeated errors
- stops trusting stale data once a reported reset boundary has already passed, and briefly shows `syncing...` until fresh data arrives

Codex:

- runs the bundled `codex-limits.py`
- the helper starts `codex app-server --listen stdio://`
- reads `account/rateLimits/read`
- displays each returned usage window using its actual duration, such as `5h` or `7d`
- omits windows that are not returned and shows them again automatically if Codex reintroduces them
- displays the reset countdown from the returned `resetsAt` timestamp

Shared cache:

- Claude and Codex each write a per-user JSON cache file in `/tmp`
- a short lock directory prevents all WezTerm instances from refreshing at once
- other windows reuse the same cached result until it expires

Status display:

- shows actual usage only when the corresponding tool is running
- colors usage as green under `50%`, yellow from `50%` to `79%`, and red at `80%` and above
- renders compact 8-cell bars by default

tmux:

- runs `tmux-quota.py` from `status-right`, so it does not depend on WezTerm or an active pane being a Claude pane
- detects Claude and Codex processes across the tmux host, including detached sessions
- invokes the existing `codex-limits.py` helper and reads Claude's OAuth token only while Claude is running
- stores independent per-provider cache files under `~/.cache/wezterm-quota-limit-tmux`
- renders provider window durations and reset times using tmux styles, with ASCII bars by default and Unicode icons/block bars with `--unicode`

## Compatibility

- Targets: Linux and Windows desktop sessions running WezTerm; tmux integration targets Unix-like tmux hosts.
- Claude credentials are read from `~/.claude/.credentials.json`.
- Codex usage is read through `codex app-server --listen stdio://`, so the installed Codex CLI must support app-server rate-limit reads.
- Platform-specific tools differ between the WezTerm and tmux integrations; see [Requirements](#requirements).

## Known Limitations

- The plugin does not refresh Claude or Codex authentication itself; it waits for the corresponding CLI to keep credentials valid.
- Codex displays `not running` unless an interactive Codex process is attached to a terminal. Quota data may still be fetchable in the background, but the visible status remains process-aware.
- Claude usage calls are intentionally cached and retried with backoff to avoid unnecessary API pressure.
- Tmux reads the installed Claude OAuth access token without refreshing it; after expiry, Claude Code must refresh credentials before usage returns.
- macOS is not currently a tested release target.

## Troubleshooting

### WezTerm

- Claude shows `not running`: confirm Claude Code is running; on Linux, `pgrep -a -x claude` can help distinguish it from the `--chrome-native-host` process.
- Codex shows `not running`: keep an interactive Codex process running in a WezTerm pane; the WezTerm integration uses pane process information.
- Codex helper fails in a GUI `PATH` environment: run `python3 codex-limits.py` directly; the helper also checks common `nvm` locations.
- Codex helper path resolution fails in a custom environment: set `WEZTERM_AGENT_QUOTA_CODEX_HELPER=/absolute/path/to/codex-limits.py` before launching WezTerm.
- Cached WezTerm data looks stale: inspect `/tmp/wezterm-quota-limit-"$USER"-*.json` and reload WezTerm; this cache is separate from tmux's cache.
- Codex helper is missing: ensure the full plugin repo was installed, not just `plugin/init.lua` by itself.

### tmux

- `not running`: run the relevant provider CLI on the tmux host, not only on the SSH client. Codex process detection requires an interactive terminal; Claude's Chrome native host does not count as Claude Code.
- `unavailable` or `syncing`: confirm the CLI is installed and its existing authentication is usable on the tmux host. The helper does not sign in or refresh credentials. `--tmux-command` can verify the generated command without fetching account data.
- The status turns green or disappears after a theme/CCB change: that theme may have replaced `status-style` or `status-right`. Reapply a readable dark style and integrate the helper into the theme's own right-side format.
- The bar is clipped: increase `status-right-length` and/or shorten neighboring widgets; available width depends on the attached terminal.
- A value looks stale or does not update immediately: tmux refreshes the status command on its configured interval. Successful usage reads are cached for a minute; failures use a retry delay, and expired windows are not kept past their reset.

## Credit

Originally based on [wezterm-quota-limit](https://github.com/EdenGibson/wezterm-quota-limit) by EdenGibson. This fork significantly extends the original Claude-only plugin with Codex support, shared cross-window caching, process-aware status states, bundled helper discovery, compact usage bars, and additional reset/error handling.
