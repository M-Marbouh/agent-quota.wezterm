# Agent Quota for WezTerm

![WezTerm plugin](https://img.shields.io/badge/WezTerm-plugin-blue)

[Listed on Awesome WezTerm](https://github.com/michaelbrusegard/awesome-wezterm#ai)

A WezTerm plugin that shows Claude and Codex quota usage directly in the status bar, with opt-in OpenCode Go usage on Unix hosts.
An optional standalone tmux status command provides the same account view on the machine running tmux, including remote and headless tmux servers.

**Works with [CCB](https://github.com/M-Marbouh/claude_code_bridge).** See the [optional CCB setup](#ccb-integration) for the required status-bar configuration.

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
- Optional `compact` and `minimal` layouts plus inactive-provider hiding in WezTerm, matching the tmux dense layouts
- Opt-in OpenCode Go usage in WezTerm on Unix hosts, bridged through the same tmux helper and cache

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
- For OpenCode Go in WezTerm only: a Unix host with `python3` and this repo's `tmux-quota.py` helper (see [Layouts, inactive providers and OpenCode Go](#layouts-inactive-providers-and-opencode-go))

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

Keep `tmux-quota.py` and `codex-limits.py` together in the checkout. Add the following to `~/.tmux.conf`, replacing the path with your clone location. It preserves existing `status-right` content and avoids duplicate appends on reload. The example enables `--unicode` icons and block bars on a readable dark background; omit that flag for the default ASCII output. This is the default full layout: no `--layout` flag is needed, and the [compact layout](#compact-layout) is opt-in:

```tmux
set-option -g status-style 'bg=#1e1e2e,fg=#cdd6f4'
set-option -g status-right-length 180
if-shell 'case "$(tmux -S "#{socket_path}" show-option -gqv status-right)" in *tmux-quota.py*) exit 0;; *) exit 1;; esac' '' 'set-option -ag status-right "  #(python3 /absolute/path/to/agent-quota.wezterm/tmux-quota.py --unicode)"'
```

Replace `/absolute/path/to` with the actual clone location. Start `tmux` to load the configuration. If the tmux server is already running, apply the changes with `tmux source-file ~/.tmux.conf`.

The duplicate check reads the global option from the same tmux server. Checking `#{status-right}` instead can read a theme's session override and append duplicate global widgets when reloading inside CCB.

When upgrading an existing setup, update the checkout first, then edit the existing helper command to add `--unicode`, `--layout`, or the new flags; the duplicate check intentionally keeps an existing command. Keep one helper invocation in `status-right`.

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

Tmux uses its native `#[...]` styles. The default ASCII bars remain available for minimal terminal fonts by omitting `--unicode`; the Unicode mode uses standard glyphs and does not require a Nerd Font. Each provider is fetched and cached independently; inactive CLIs show `not running` (or disappear with [`--hide-inactive`](#hiding-inactive-providers)), temporary failures show a stale marker when safe cached data remains, and data past a reset is discarded until a fresh read succeeds.

#### Compact layout

The full layout above is the default and needs no flag. Three opt-in layouts compress the same data when a status line is too narrow, and all of them keep the provider names and every state marker:

```text
⚡ Claude: 5h ▁ 2% 4h53m · 7d 2%  │  ✦ Codex: 5h ▄ 47% 30m · 7d 20%  │  ◈ OpenCode Go: 5h ▁ 0% 4h50m · 7d 1% · mo 0%
```

`--layout compact` replaces the eight-cell bars with a one-cell gauge for each short window and keeps the reset countdown for those windows and for any window at 80% or above. A window counts as short when its label starts with an hour or minute duration such as `5h` or `45m`, so Codex's `1h` window and Claude's `7d` or `7d opus` windows each get the right treatment. The ASCII form stays entirely ASCII: `C`, `X`, and `O` identifiers, `|` between providers, `/` between a provider's windows, `...` for waiting data, and no gauge:

```text
C Claude: 5h 2% 4h53m / 7d 2%  |  X Codex: 5h 47% 30m / 7d 20%  |  O OpenCode Go: 5h 0% 4h50m / 7d 1% / mo 0%
```

`--layout minimal` reduces each provider to one percentage: the highest usage across that provider's windows, with a red `!` added when any of its windows is limited:

```text
⚡ Claude: 2%  │  ✦ Codex: 47%  │  ◈ OpenCode Go: 1%
C Claude: 2%  |  X Codex: 47%  |  O OpenCode Go: 1%
```

To opt in, add the flag to the helper command you already have, in place, rather than adding a second helper. `--layout auto` renders full, compact, and minimal in that order and keeps the first that fits the cells given by `--max-width`; the helper fragment then reads:

```tmux
#(python3 /absolute/path/to/agent-quota.wezterm/tmux-quota.py --unicode --layout auto --max-width #{e|-:#{client_width},60})
```

`--max-width` is the number of cells reserved for the quota text, so subtract your other status widgets before passing it; the `#{e|-:#{client_width},60}` form subtracts 60 cells for the clock and the rest of the line, and tmux expands it per attached client. `--layout auto` without `--max-width` renders the compact layout, and `--max-width` is ignored by the explicit layouts. At a typical 212-column client the three layouts need roughly 220 cells for full, 117 for compact, and 52 for minimal.

The layouts keep every state marker distinct: an inactive CLI shows only a dim icon and name, `…` (or `...` in ASCII) means waiting for data, `?` means unavailable, `~` marks stale data directly after the name (`Claude:~`), and a limited window keeps a red percentage with a red `!`.

**Updating a running server.** Editing the guarded append line in `~/.tmux.conf` and reloading does not change the invocation already stored in `status-right`, because the guard skips the append while a `tmux-quota.py` command is present. Update the live option once; this keeps every other status widget in place:

```sh
tmux set -g status-right "$(tmux show -gv status-right | sed 's|tmux-quota.py --unicode|tmux-quota.py --unicode --layout compact|')"
```

Run it once only; a second run adds the flag again. Check the result with `tmux show -gv status-right` before relying on it, and keep the matching edit in `~/.tmux.conf` so the next server starts in the same state. Alternatively, skip the live update: the edited configuration takes effect the next time a tmux server starts, after your current sessions have ended. For CCB, the `@ccb_status_right_extra` value is read live, so changing it in the sourced file and reloading does take effect; edit that value instead of the `status-right` option.

#### Hiding inactive providers

`--hide-inactive` omits providers whose CLI is not running, in every layout and in both Unicode and ASCII output, and leaves the separators correct so nothing is left dangling:

```text
✦ Codex: 5h ▇ 78% 3h57m · 7d 34%  │  ◈ OpenCode Go: 5h ▁ 0% 3h50m · 7d 1% · mo 0%
```

This detects running tools on the tmux host, not connected accounts: Claude counts as running only while a `claude` process runs in a terminal, Codex only while `codex` runs, and OpenCode Go whenever a `pi` or `opencode` TUI is open, even when that session is not using OpenCode Go. A hidden provider is also never fetched, so no credential file is read and no key command runs for it. A provider that *is* running but has no usable data still renders its state (`no key`, `syncing`, `unavailable`, `no plan`, or `~` on stale data). When every selected provider is inactive the widget renders nothing at all, which keeps the status line clean. Check what would be hidden by running the helper once with the flag before adding it to your configuration; `python3 tmux-quota.py --tmux-command --hide-inactive` prints the fragment for you.

#### Machine-readable output

`--json` prints exactly one JSON object instead of the tmux status line, for machine consumers such as the WezTerm plugin:

```sh
python3 /absolute/path/to/agent-quota.wezterm/tmux-quota.py --json --providers claude,codex,opencode-go
```

The object is `{"version":1,"providers":[...]}`, one entry per selected provider in the usual order, each with `name`, `state` (one of `ok`, `not_running`, `syncing`, `no_plan`, `no_key`, `rate_limited`, `unavailable`, `error`), `stale`, and `windows` (`label`, `percent`, `reset_at` or `null`, `limited`). It goes through the same fetches, cache, backoff and key resolution as the status line, never contains credentials, error strings or tmux style codes, and ignores `--layout` and `--hide-inactive`. A runtime failure still prints the documented shape with `"state": "error"` and exits 0, so a consumer can always parse the output.

Other tmux themes may replace `status-right` after this command is added. Use the theme's supported extension point, preserving its existing content. A reload guard must check the same option scope the theme changes: global or per-session. Increase `status-right-length` if the bar is clipped; the needed width depends on your other widgets and terminal size.

### CCB integration

[CCB](https://github.com/M-Marbouh/claude_code_bridge) is optional. Use a CCB build with the documented [`@ccb_status_right_extra` extension](https://github.com/M-Marbouh/claude_code_bridge#tmux-status-right-extension) and the tmux theme lifecycle fixes. Older installed builds can ignore this option; updating a source checkout alone does not update the installed theme scripts.

When upgrading from an older CCB build, exit its launchers before installing the update so the old scripts can restore their saved theme state. Start a fresh launcher afterward; replacing script files does not migrate an already-active legacy theme to the new lifecycle.

Keep the standalone tmux configuration above for ordinary sessions. Add this separately in your own `~/.tmux.conf`, outside CCB's installer-managed block, replacing the path with your clone location:

```tmux
set-option -g @ccb_status_right_extra "  #(python3 /absolute/path/to/agent-quota.wezterm/tmux-quota.py --unicode)"
```

Reload with `tmux source-file ~/.tmux.conf`. A compatible active CCB theme reads the value live and displays the quotas at the end of CCB's status line. CCB's default `slim` layout is one row (mode badge and git info, project folder, live CCB request activity) with no clock, so the widgets have the right side to themselves; the two-row `classic` layout places the same value after its clock. A per-session value overrides the global value. Assigning the option again on reload keeps one widget; if you already use this extension for other widgets, add the quota command once to that existing value. CCB restores the ordinary status when its launcher exits. Narrow terminals can still clip the combined status line.

To include OpenCode Go, keep one helper invocation and list every provider in it; failures stay isolated per provider inside that one item:

```tmux
set-option -g @ccb_status_right_extra "  #(python3 /absolute/path/to/agent-quota.wezterm/tmux-quota.py --unicode --providers claude,codex,opencode-go)"
```

The OpenCode Go key is found the same way as in the standalone setup (see [OpenCode Go](#opencode-go)). For CCB's one-row bar, the [compact layout](#compact-layout) and [hiding inactive providers](#hiding-inactive-providers) sections describe the optional flags that keep the item short.

CCB sets `status-right` per session while its theme is active, so the standalone `set-option -ag status-right` forms below are masked inside a CCB session; use `@ccb_status_right_extra` there instead, as above.

For paths with spaces, use the fragment printed by `python3 tmux-quota.py --tmux-command --unicode` inside the double-quoted option value, following the quoting guidance above. Literal percent signs in this option, including any in the command path, must be doubled as `%%` because CCB applies tmux time formatting. Percentages printed by the quota helper need no changes. Native `#[...]` styles and `#(...)` commands are supported. The `--layout` and `--max-width` flags of the standalone command work here unchanged; add them to the value as the [compact layout](#compact-layout) section describes.

Do not edit CCB's installed theme scripts: its installer replaces them. If an older personal integration appends the quota through a hook or directly inside the theme, remove that extra append when switching to this option so the quota appears once. Check ordinary tmux, CCB activation, config reloads, and CCB exit after upgrading. This plugin uses the public extension and does not modify CCB's private restore state.

### OpenCode Go

OpenCode Go usage is opt-in in both integrations: the tmux helper shows it only with `--providers`, and the WezTerm plugin only with `opencode_go.enabled = true` on a Unix host. In tmux, select it with `--providers`, which takes a comma-separated list drawn from `claude`, `codex`, and `opencode-go`:

```sh
python3 /absolute/path/to/agent-quota.wezterm/tmux-quota.py --unicode --providers opencode-go
```

The default invocation is unchanged (`claude,codex`). Providers that are not selected are not fetched and their credentials are not read. OpenCode Go shows three windows: `5h` (rolling), `7d` (weekly), and `mo` (monthly), each with its percentage, bar, and reset countdown. A window the service reports as rate limited keeps its percentage in the high/red colour with a `limited` marker. `not running`, `no key`, `no Go plan`, `syncing`, `unavailable`, and `stale` states match the other providers.

One status item renders all three providers, so a Go outage or a missing key cannot disturb the Claude and Codex part of the line. This reload-safe form appends the helper exactly once and uses the default full layout; switch to `--layout compact` as described in the [compact layout](#compact-layout) section when the full line does not fit:

```tmux
if-shell 'case "$(tmux -S "#{socket_path}" show-option -gqv status-right)" in *tmux-quota.py*) exit 0;; *) exit 1;; esac' '' 'set-option -ag status-right "  #(python3 /absolute/path/to/agent-quota.wezterm/tmux-quota.py --unicode --providers claude,codex,opencode-go --opencode-key-command \"your-secret-tool get opencode-go\")"'
```

Upgrading from the two-widget setup: the reload guard skips the append when `status-right` already contains a `tmux-quota.py` invocation, so adding this block to an existing configuration will not replace the line you already have. Delete the old standalone OpenCode Go widget and edit the existing helper command to the form above instead of adding a second one, using the one-time live update or a fresh server described in the [compact layout](#compact-layout) section; if you use CCB, make the same edit to the value of `@ccb_status_right_extra` and reload with `tmux source-file ~/.tmux.conf`. Inside a CCB session this `status-right` append is masked by CCB's per-session override, so inside CCB put the same single invocation in `@ccb_status_right_extra`, as the [CCB integration](#ccb-integration) section shows.

`--opencode-key-command` takes a command; the helper splits it into arguments and runs it without a shell, so pipelines, globbing, redirections, and `$VAR` expansion do not work. Keep the command in quotes, as above, so tmux and the status-line shell pass it as one argument. `python3 tmux-quota.py --tmux-command … --opencode-key-command 'your-secret-tool get opencode-go'` prints the same fragment with the quoting applied for you. The command runs only when the key is actually needed, meaning OpenCode Go is selected and running, the cached Go data is stale, and no refresh is already in flight. The command must print exactly one line, the key itself, optionally newline-terminated; leading or trailing whitespace, extra lines, and non-printable bytes are rejected. A rejected or failed lookup shows `no key` for OpenCode Go only and makes no network request; Claude and Codex keep rendering from their own caches.

Set `OPENCODE_API_KEY` instead when you prefer the environment. A non-blank variable wins over the command, and an empty or whitespace-only variable counts as unset, so the command above still works when an empty variable is exported:

```sh
export OPENCODE_API_KEY="$(your-secret-tool get opencode-go)"
tmux new-session   # or restart the server
```

Export it before the server starts, because an already-running server keeps the environment it was started with; exporting the variable in a shell afterwards does not reach that server's status commands. On tmux 3.4, `tmux set-environment -g OPENCODE_API_KEY …` also reaches a running server's status `#()` jobs, which was checked with an attached client; the value stays in the server environment until `set-environment -gu OPENCODE_API_KEY`. The environment variable is visible to every pane and program that server starts, which is why the command option exists. Neither form keeps the key on the command line, and the helper writes no credential file of its own; it stores only usage data in its cache.

With neither an environment key nor a key command, the helper falls back to the login OpenCode already saved: it reads the `opencode-go` entry of `~/.local/share/opencode/auth.json`, or of `$XDG_DATA_HOME/opencode/auth.json` when that variable is set to an absolute path. So logging in to OpenCode Go with an **API key** is enough for the widget to work; other login types are not used. The order is fixed: a non-blank `OPENCODE_API_KEY` wins over `--opencode-key-command`, which wins over the saved login, and each source is consulted only when the ones before it supply nothing. A configured command that fails reports `no key` rather than falling back to the file. The saved login counts only for an entry of type `api` whose key is one printable line; a missing, unreadable, oversized or non-JSON file, a missing entry, any other login type and an invalid key all mean `no key` with no network request. Only that one entry is read, and the key is never cached, printed or logged.

The widget appears while any interactive Pi or OpenCode process is running on the tmux host, which is the same process check used for Claude and Codex. That only means one of those tools is open: it does not prove that the running Pi session is using OpenCode Go, and a Go key from a different workspace still reports that workspace's meters. A workspace without a Go subscription returns `no Go plan` rather than an error; rate limiting holds a longer retry delay and shows `syncing` when no safe cached meters are left.

The usage endpoint used here is undocumented upstream and may change without notice; a changed response shape is reported as `unavailable` rather than as fabricated numbers. Three providers need more room than two, so raise `status-right-length` (260 is enough to show all three next to a short clock) and check the line on your narrowest terminal.

## Configuration

For the WezTerm plugin, pass options to `apply_to_config(config, opts)`:

```lua
quota.apply_to_config(config, {
  poll_interval_secs = 120,
  position = "left",
  dashboard_key = { key = "u", mods = "CTRL|SHIFT" },
  compact = false,
  layout = nil,          -- nil (legacy full line), "full", "compact" or "minimal"
  hide_inactive = false, -- omit providers whose CLI is not running
  opencode_go = {
    enabled = false,     -- opt-in; Unix only
    key_command = nil,   -- optional command that prints the Go key
  },
  icons = {
    claude = "⚡",
    codex  = "✦",
    opencode_go = "◈",
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
- `layout`: `nil` keeps the legacy full line; `"full"` is the same rendering with OpenCode Go included when enabled; `"compact"` and `"minimal"` follow the tmux dense layouts. Default: `nil`
- `hide_inactive`: omit providers whose CLI is not running; when every provider is hidden the status is empty. Default: `false`
- `opencode_go.enabled`: show OpenCode Go from the tmux helper. Default: `false`
- `opencode_go.key_command`: optional command whose first output line is the Go key, forwarded to the helper. Default: `nil`
- `icons.claude`: Claude prefix icon. Default: `⚡` (override to taste, e.g. `▲`)
- `icons.codex`: Codex prefix icon. Default: `✦` (override to taste, e.g. `◆`)
- `icons.opencode_go`: OpenCode Go prefix icon. Default: `◈`
- `icons.week`: separator between usage windows
- `bars.enabled`: show compact percentage bars
- `bars.width`: number of bar cells
- `bars.full` / `bars.empty`: glyphs used for the bar
- `codex_script`: absolute path to `codex-limits.py`, overriding auto-discovery. Default: `nil` (auto-detect)

### Layouts, inactive providers and OpenCode Go

```lua
quota.apply_to_config(config, {
  layout = "compact",
  hide_inactive = true,
  opencode_go = {
    enabled = true,
    -- key_command = "your-secret-tool get opencode-go",
  },
})
```

`layout = "compact"` and `layout = "minimal"` mirror the tmux dense layouts: an `icon Name:` head, a one-cell gauge and countdown for short windows such as `5h`, percentage only for long windows with the countdown kept when a window is at `80%` or more or rate limited, and the highest percentage plus a red `!` in the minimal layout. `~` after the colon marks stale data, an inactive CLI keeps only its dim name, and the other states read `…`, `?`, `no key`, `no plan` or `ready`. Windows are joined with ` · ` and providers with a dim `│`. `layout = nil` and `layout = "full"` keep the bar-and-countdown rendering above; `compact = true` still means "hide the countdowns" in those layouts. There is no `auto` layout in WezTerm.

`hide_inactive = true` drops providers whose CLI is not running, so a closed Claude or Codex session disappears instead of showing `not running`; separators stay clean and the line is empty when nothing is active. OpenCode Go counts as inactive when the helper reports `not_running`.

`opencode_go.enabled = true` adds the Go provider to the WezTerm status bar. The plugin shells out to this repo's `tmux-quota.py --json --providers opencode-go` at most once every 15 seconds and reuses the tmux cache, so tmux and WezTerm report the same numbers without separate network traffic. Expired windows stop counting between helper runs. Find the helper with `WEZTERM_AGENT_QUOTA_TMUX_HELPER=/absolute/path/to/tmux-quota.py` if auto-discovery misses it. A malformed or failed helper run shows `?` for Go and leaves Claude and Codex untouched.

This bridge is Unix-only: the helper and its bridge are a Python process pair, so on native Windows `opencode_go.enabled = true` renders `not supported on Windows` and never starts a process. The key comes from the same precedence documented for tmux: a non-blank `OPENCODE_API_KEY` in the WezTerm environment, then `opencode_go.key_command`, then the OpenCode **API-key login** saved in OpenCode's `auth.json`. The Claude and Codex credential paths are untouched.

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
- OpenCode Go (when enabled on a Unix host) comes from `tmux-quota.py --json`, so it shares the tmux cache and key resolution instead of adding a second credential path

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
- OpenCode Go shows `?`: run `python3 tmux-quota.py --json --providers opencode-go` to see the helper's own state; `no key` means no environment key, key command or saved OpenCode API-key login was usable.
- OpenCode Go shows `not supported on Windows`: the Go bridge is Unix-only; the provider's other states still work on Windows.
- Codex helper is missing: ensure the full plugin repo was installed, not just `plugin/init.lua` by itself.

### tmux

- `not running`: run the relevant provider CLI on the tmux host, not only on the SSH client. Codex process detection requires an interactive terminal; Claude's Chrome native host does not count as Claude Code.
- `unavailable` or `syncing`: confirm the CLI is installed and its existing authentication is usable on the tmux host. The helper does not sign in or refresh credentials. `--tmux-command` can verify the generated command without fetching account data.
- The status turns green or disappears after a theme/CCB change: that theme may have replaced `status-style` or `status-right`. Reapply a readable dark style and integrate the helper into the theme's own right-side format.
- The bar is clipped: increase `status-right-length` and/or shorten neighboring widgets; available width depends on the attached terminal.
- A value looks stale or does not update immediately: tmux refreshes the status command on its configured interval. Successful usage reads are cached for a minute; failures use a retry delay, and expired windows are not kept past their reset.

## Credit

Originally based on [wezterm-quota-limit](https://github.com/EdenGibson/wezterm-quota-limit) by EdenGibson. This fork significantly extends the original Claude-only plugin with Codex support, shared cross-window caching, process-aware status states, bundled helper discovery, compact usage bars, and additional reset/error handling.
