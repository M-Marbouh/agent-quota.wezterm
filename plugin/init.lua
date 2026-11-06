local wezterm = require("wezterm")

local M = {}

local is_windows = type(wezterm.target_triple) == "string"
  and wezterm.target_triple:find("windows") ~= nil

-- Config defaults
local config = {
  poll_interval_secs = 60,
  position = "right", -- "left" or "right"
  dashboard_key = { key = "u", mods = "CTRL|SHIFT" }, -- keybind to open dashboard
  layout = nil,       -- nil keeps the legacy full line; "full" | "compact" | "minimal"
  hide_inactive = false, -- omit providers whose CLI is not running
  opencode_go = { enabled = false, key_command = nil },
  icons = {
    claude = "⚡", -- override to taste, e.g. "▲"
    codex  = "✦", -- override to taste, e.g. "◆"
    opencode_go = "◈",
    week   = "▪",
    bolt   = "⚡", -- legacy alias
  },
  bars = {
    enabled = true,
    width = 8,
    full = "█",
    empty = "░",
  },
  codex_script = nil, -- explicit path to codex-limits.py, overrides auto-detection
  compact = false,    -- hide reset countdowns to save space
}

-- Cached usage data
local cached_data = nil
local last_fetch_time = 0
local consecutive_errors = 0
local last_error = nil
local handler_registered = false
local cached_token = nil

-- In-memory fast-path: avoid all I/O when data was checked very recently
local FETCH_GATE_SECS = 1         -- min seconds between full fetch cycles
local PROCESS_CHECK_TTL = 5       -- cache is_*_running() results for this long

-- Claude process-state cache
local claude_running_cached = nil
local claude_running_checked_at = 0

-- Codex process-state cache
local codex_running_cached = nil
local codex_running_checked_at = 0

-- In-memory fast-path data (last returned result + timestamp)
local claude_last_result = nil
local claude_last_result_at = 0
local codex_last_result = nil
local codex_last_result_at = 0

-- ANSI escape helpers (bypass wezterm.format to avoid nightly deserialization bugs)
local ESC = "\x1b["
local RESET = ESC .. "0m"

local function hex_to_fg(hex)
  local r = tonumber(hex:sub(2, 3), 16)
  local g = tonumber(hex:sub(4, 5), 16)
  local b = tonumber(hex:sub(6, 7), 16)
  return ESC .. "38;2;" .. r .. ";" .. g .. ";" .. b .. "m"
end

-- Color thresholds (Tokyo Night palette)
local function usage_color_esc(pct)
  if pct >= 80 then
    return hex_to_fg("#f7768e") -- red
  elseif pct >= 50 then
    return hex_to_fg("#e0af68") -- yellow
  else
    return hex_to_fg("#9ece6a") -- green
  end
end

local DIM = hex_to_fg("#565f89")
local BRIGHT = hex_to_fg("#c0caf5")
local STALE = hex_to_fg("#e0af68")
local RED = hex_to_fg("#f7768e")

-- Deep merge: t2 values override t1, recurses into nested tables
local function deep_merge(t1, t2)
  local result = {}
  for k, v in pairs(t1) do
    result[k] = v
  end
  for k, v in pairs(t2) do
    if type(v) == "table" and type(result[k]) == "table" then
      result[k] = deep_merge(result[k], v)
    else
      result[k] = v
    end
  end
  return result
end

local function current_file_path()
  local dbg = rawget(_G, "debug")
  if type(dbg) ~= "table" or type(dbg.getinfo) ~= "function" then
    return nil
  end

  -- Walk the call stack: WezTerm's plugin loader may not set level-1 source
  -- to a file path, so scan upward until we find a level whose source ends in
  -- init.lua (i.e. this file).
  for level = 1, 20 do
    local info = dbg.getinfo(level, "S")
    if not info then
      break
    end
    local source = info.source
    if type(source) == "string" and source:sub(1, 1) == "@" then
      local path = source:sub(2)
      if path:find("[/\\]init%.lua$") or path:find("^init%.lua$") then
        return path
      end
    end
  end
  return nil
end

local function dirname(path)
  if not path or path == "" then
    return nil
  end
  return path:match("^(.*)[/\\][^/\\]+$") or "."
end

local function file_exists(path)
  if not path or path == "" then
    return false
  end
  local f = io.open(path, "r")
  if not f then
    return false
  end
  f:close()
  return true
end

local function resolve_codex_script()
  local home = os.getenv("HOME") or os.getenv("USERPROFILE") or ""
  local plugin_dir = dirname(current_file_path())
  local candidates = {}

  local env_path = os.getenv("WEZTERM_AGENT_QUOTA_CODEX_HELPER")
  if env_path and env_path ~= "" then
    candidates[#candidates + 1] = env_path
  end

  if plugin_dir then
    local repo_root = dirname(plugin_dir)
    if repo_root then
      candidates[#candidates + 1] = repo_root .. "/codex-limits.py"
      candidates[#candidates + 1] = repo_root .. "\\codex-limits.py"
    end
    candidates[#candidates + 1] = plugin_dir .. "/codex-limits.py"
    candidates[#candidates + 1] = plugin_dir .. "\\codex-limits.py"
  end

  if home ~= "" then
    if is_windows then
      local appdata = os.getenv("LOCALAPPDATA") or (home .. "\\AppData\\Local")
      candidates[#candidates + 1] = appdata .. "\\wezterm\\codex-limits.py"
      candidates[#candidates + 1] = home .. "\\dev\\Plugins\\agent-quota.wezterm\\codex-limits.py"
    else
      candidates[#candidates + 1] = home .. "/dev/Plugins/wezterm-quota-limit/codex-limits.py"
      candidates[#candidates + 1] = home .. "/dev/Plugins/agent-quota.wezterm/codex-limits.py"
      candidates[#candidates + 1] = home .. "/.local/share/wezterm/codex-limits.py"
    end
  end

  for _, candidate in ipairs(candidates) do
    if file_exists(candidate) then
      return candidate
    end
  end

  if is_windows then
    local appdata = os.getenv("LOCALAPPDATA") or (home ~= "" and (home .. "\\AppData\\Local") or "")
    if appdata ~= "" then
      local plugins_dir = appdata .. "\\wezterm\\plugins"
      local ok, stdout = wezterm.run_child_process({
        "powershell", "-NoProfile", "-Command",
        "Get-ChildItem -Path '"
          .. plugins_dir:gsub("'", "''")
          .. "' -Recurse -Depth 5 -Filter 'codex-limits.py' -ErrorAction SilentlyContinue"
          .. " | Select-Object -ExpandProperty FullName",
      })
      if ok and stdout and stdout ~= "" then
        local selected = nil
        for line in stdout:gmatch("[^\r\n]+") do
          if not selected then
            selected = line
          end
          if line:find("agent%-quota%.wezterm", 1, false) then
            selected = line
            break
          end
        end
        if selected and file_exists(selected) then
          return selected
        end
      end
    end
  elseif home ~= "" then
    local plugins_dir = home .. "/.local/share/wezterm/plugins"
    local ok, stdout = wezterm.run_child_process({
      "find",
      plugins_dir,
      "-maxdepth",
      "5",
      "-type",
      "f",
      "-name",
      "codex-limits.py",
    })

    if ok and stdout and stdout ~= "" then
      local selected = nil
      for line in stdout:gmatch("[^\r\n]+") do
        if not selected then
          selected = line
        end
        if line:find("agent%-quota%.wezterm", 1, false) then
          selected = line
          break
        end
      end

      if selected and file_exists(selected) then
        return selected
      end
    end
  end

  return candidates[1] or "codex-limits.py"
end

local function usage_bar_esc(pct)
  if not config.bars or not config.bars.enabled then
    return nil
  end

  local width = tonumber(config.bars.width) or 6
  if width < 1 then
    return nil
  end

  local full = config.bars.full or "█"
  local empty = config.bars.empty or "░"
  local normalized = tonumber(pct) or 0
  if normalized < 0 then
    normalized = 0
  elseif normalized > 100 then
    normalized = 100
  end

  local filled = math.floor((normalized / 100) * width + 0.5)
  if normalized >= 100 then
    filled = width
  elseif normalized > 0 and filled == 0 then
    filled = 1
  elseif normalized <= 0 then
    filled = 0
  end

  return usage_color_esc(normalized) .. string.rep(full, filled)
    .. DIM .. string.rep(empty, width - filled)
end

local function cache_prefix()
  local user = os.getenv("USER") or os.getenv("USERNAME") or "user"
  user = user:gsub("[^%w_.-]", "_")
  if is_windows then
    local tmp = os.getenv("TEMP") or os.getenv("TMP")
      or (os.getenv("USERPROFILE") or "C:\\Users\\Default") .. "\\AppData\\Local\\Temp"
    return tmp .. "\\wezterm-quota-limit-" .. user
  end
  return "/tmp/wezterm-quota-limit-" .. user
end

local SHARED_CACHE_PREFIX = cache_prefix()
local CLAUDE_CACHE_PATH = SHARED_CACHE_PREFIX .. "-claude.json"
local CLAUDE_LOCK_DIR = SHARED_CACHE_PREFIX .. "-claude.lock"
local CODEX_CACHE_PATH = SHARED_CACHE_PREFIX .. "-codex.json"
local CODEX_LOCK_DIR = SHARED_CACHE_PREFIX .. "-codex.lock"
local LOCK_TIMEOUT_SECS = 30
local INVALID_STALE_RETRY_SECS = 15
local CLAUDE_TRANSIENT_RETRY_SECS = 30
local CLAUDE_RATE_LIMIT_RETRY_SECS = 60
local CLAUDE_FAST_POLL_SECS = 15
local CLAUDE_HIGH_USAGE_POLL_SECS = 30
local CLAUDE_FAST_POLL_WINDOW_SECS = 10 * 60
local CLAUDE_HIGH_UTILIZATION_PCT = 90

local function json_escape(str)
  local replacements = {
    ['"'] = '\\"',
    ["\\"] = "\\\\",
    ["\b"] = "\\b",
    ["\f"] = "\\f",
    ["\n"] = "\\n",
    ["\r"] = "\\r",
    ["\t"] = "\\t",
  }

  return (str:gsub('[%z\1-\31\\"]', function(c)
    return replacements[c] or string.format("\\u%04x", c:byte())
  end))
end

local function table_is_array(value)
  local max = 0
  local count = 0

  for k, _ in pairs(value) do
    if type(k) ~= "number" or k < 1 or k ~= math.floor(k) then
      return false, 0
    end
    if k > max then
      max = k
    end
    count = count + 1
  end

  return max == count, max
end

local function json_encode_value(value)
  local value_type = type(value)

  if value == nil then
    return "null"
  end

  if value_type == "string" then
    return '"' .. json_escape(value) .. '"'
  end

  if value_type == "number" then
    if value ~= value or value == math.huge or value == -math.huge then
      return "null"
    end
    return tostring(value)
  end

  if value_type == "boolean" then
    return value and "true" or "false"
  end

  if value_type == "table" then
    local is_array, length = table_is_array(value)
    local parts = {}

    if is_array then
      for i = 1, length do
        parts[#parts + 1] = json_encode_value(value[i])
      end
      return "[" .. table.concat(parts, ",") .. "]"
    end

    local keys = {}
    for k, _ in pairs(value) do
      keys[#keys + 1] = tostring(k)
    end
    table.sort(keys)

    for _, key in ipairs(keys) do
      parts[#parts + 1] = '"' .. json_escape(key) .. '":' .. json_encode_value(value[key])
    end
    return "{" .. table.concat(parts, ",") .. "}"
  end

  return '"' .. json_escape(tostring(value)) .. '"'
end

local function read_json_file(path)
  local f = io.open(path, "r")
  if not f then
    return nil
  end

  local raw = f:read("*a")
  f:close()

  if not raw or raw == "" then
    return nil
  end

  local ok, data = pcall(wezterm.json_parse, raw)
  if not ok or type(data) ~= "table" then
    return nil
  end

  return data
end

local function write_json_file(path, value)
  local tmp_path = string.format("%s.tmp.%d.%d", path, os.time(), math.floor((os.clock() or 0) * 1000000))
  local f = io.open(tmp_path, "w")
  if not f then
    return nil, "open failed"
  end

  local ok, encoded = pcall(json_encode_value, value)
  if not ok then
    f:close()
    os.remove(tmp_path)
    return nil, encoded
  end

  f:write(encoded)
  f:close()

  -- Windows: os.rename fails if destination exists; remove it first
  if is_windows then
    os.remove(path)
  end

  local renamed, err = os.rename(tmp_path, path)
  if not renamed then
    os.remove(tmp_path)
    return nil, err or "rename failed"
  end

  return true
end

local function read_shared_cache(path)
  local entry = read_json_file(path)
  if type(entry) ~= "table" or type(entry.data) ~= "table" then
    return nil
  end
  return entry
end

local function days_from_civil(year, month, day)
  if month <= 2 then
    year = year - 1
  end

  local era = math.floor(year / 400)
  local yoe = year - (era * 400)
  local mp = month + (month > 2 and -3 or 9)
  local doy = math.floor((153 * mp + 2) / 5) + day - 1
  local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
  return era * 146097 + doe - 719468
end

local function unix_from_iso_utc(reset_str)
  if type(reset_str) ~= "string" or reset_str == "" then
    return nil
  end

  local year, month, day, hour, min, sec =
    reset_str:match("(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)")
  if not year then
    return nil
  end

  local sign, offset_hour, offset_min = reset_str:match("([+-])(%d%d):?(%d%d)$")
  local offset_secs = 0
  if sign and offset_hour and offset_min then
    offset_secs = tonumber(offset_hour) * 3600 + tonumber(offset_min) * 60
    if sign == "-" then
      offset_secs = -offset_secs
    end
  elseif not reset_str:match("[Zz]$") then
    return nil
  end

  local epoch =
    days_from_civil(tonumber(year), tonumber(month), tonumber(day)) * 86400
    + tonumber(hour) * 3600
    + tonumber(min) * 60
    + tonumber(sec)

  return epoch - offset_secs
end

local function earliest_reset_boundary_ts(data)
  if type(data) ~= "table" or data.not_running or data.error or data.syncing then
    return nil
  end

  local candidates = {}

  local five_reset = data.five_hour and unix_from_iso_utc(data.five_hour.resets_at) or nil
  local seven_reset = data.seven_day and unix_from_iso_utc(data.seven_day.resets_at) or nil
  local primary_reset = tonumber(data.primary_reset_at)
  local secondary_reset = tonumber(data.secondary_reset_at)

  if five_reset then
    candidates[#candidates + 1] = five_reset
  end
  if seven_reset then
    candidates[#candidates + 1] = seven_reset
  end
  if primary_reset then
    candidates[#candidates + 1] = primary_reset
  end
  if secondary_reset then
    candidates[#candidates + 1] = secondary_reset
  end

  if #candidates == 0 then
    return nil
  end

  table.sort(candidates)
  return candidates[1]
end

local function data_crossed_reset_boundary(data, now)
  local reset_at = earliest_reset_boundary_ts(data)
  return reset_at ~= nil and now >= reset_at
end

local function seconds_until_reset_boundary(data, now)
  local reset_at = earliest_reset_boundary_ts(data)
  if not reset_at then
    return nil
  end
  return reset_at - (now or os.time())
end

local function claude_fast_poll_secs(data, now)
  if type(data) ~= "table" or data.not_running or data.syncing or data.error then
    return nil
  end

  local remaining = seconds_until_reset_boundary(data, now)
  if remaining ~= nil and remaining > 0 and remaining <= CLAUDE_FAST_POLL_WINDOW_SECS then
    return CLAUDE_FAST_POLL_SECS
  end

  local five_pct = data.five_hour and tonumber(data.five_hour.utilization) or 0
  local seven_pct = data.seven_day and tonumber(data.seven_day.utilization) or 0
  if math.max(five_pct, seven_pct) >= CLAUDE_HIGH_UTILIZATION_PCT then
    return CLAUDE_HIGH_USAGE_POLL_SECS
  end

  return nil
end

local function is_rate_limited_error(err)
  return type(err) == "string" and err:match("^rate limited") ~= nil
end

local function shared_cache_is_fresh(entry, now)
  if type(entry) ~= "table" or type(entry.data) ~= "table" then
    return false
  end

  local next_refresh_at = tonumber(entry.next_refresh_at)
  if is_rate_limited_error(entry.data.error) then
    local written_at = tonumber(entry.written_at) or now
    next_refresh_at = math.min(next_refresh_at or (written_at + CLAUDE_RATE_LIMIT_RETRY_SECS), written_at + CLAUDE_RATE_LIMIT_RETRY_SECS)
  end
  return next_refresh_at ~= nil
    and now < next_refresh_at
    and not data_crossed_reset_boundary(entry.data, now)
end

local function interval_for_errors(error_count)
  if not error_count or error_count <= 0 then
    return config.poll_interval_secs
  end

  return math.min(120 * (2 ^ (error_count - 1)), 1800)
end

local function build_cache_entry(data, error_count, last_err, now, retry_secs)
  return {
    written_at = now,
    next_refresh_at = now + (retry_secs or interval_for_errors(error_count)),
    error_count = error_count,
    last_error = last_err,
    data = data,
  }
end

local function lock_age_secs(lock_dir)
  if is_windows then
    local ok, stdout = wezterm.run_child_process({
      "powershell", "-NoProfile", "-Command",
      "[int64]([DateTimeOffset]::new((Get-Item -LiteralPath '"
        .. lock_dir:gsub("'", "''")
        .. "' -ErrorAction Stop).LastWriteTimeUtc)).ToUnixTimeSeconds()",
    })
    if not ok or not stdout then
      return nil
    end
    local mtime = tonumber(stdout:match("(%d+)"))
    if not mtime then
      return nil
    end
    return os.time() - mtime
  end

  local ok, stdout = wezterm.run_child_process({ "stat", "-c", "%Y", lock_dir })
  if not ok or not stdout then
    return nil
  end

  local mtime = tonumber(stdout:match("(%d+)"))
  if not mtime then
    return nil
  end

  return os.time() - mtime
end

local function acquire_lock(lock_dir)
  local mkdir_cmd = is_windows and { "cmd", "/c", "mkdir", lock_dir } or { "mkdir", lock_dir }
  local rmdir_cmd = is_windows and { "cmd", "/c", "rmdir", lock_dir } or { "rmdir", lock_dir }

  local ok = wezterm.run_child_process(mkdir_cmd)
  if ok then
    return true
  end

  local age = lock_age_secs(lock_dir)
  if age and age > LOCK_TIMEOUT_SECS then
    wezterm.run_child_process(rmdir_cmd)
    return wezterm.run_child_process(mkdir_cmd)
  end

  return false
end

local function release_lock(lock_dir)
  local rmdir_cmd = is_windows and { "cmd", "/c", "rmdir", lock_dir } or { "rmdir", lock_dir }
  wezterm.run_child_process(rmdir_cmd)
end

local function cacheable_data(data, now)
  if type(data) ~= "table" or data.not_running or data.syncing then
    return nil
  end
  if now and data_crossed_reset_boundary(data, now) then
    return nil
  end
  return data
end

local function transient_refresh_entry(previous_data, previous_errors, last_err, now, raw_previous)
  if data_crossed_reset_boundary(raw_previous, now) then
    return build_cache_entry(
      { syncing = true },
      previous_errors + 1,
      last_err,
      now,
      INVALID_STALE_RETRY_SECS
    )
  end

  return build_cache_entry(previous_data or { error = last_err }, previous_errors + 1, last_err, now)
end

local function claude_success_retry_secs(data, now)
  local normal_retry = tonumber(config.poll_interval_secs) or 60
  local fast_retry = claude_fast_poll_secs(data, now)
  if fast_retry then
    return math.min(normal_retry, fast_retry)
  end
  return normal_retry
end

local function claude_transient_refresh_entry(previous_data, previous_errors, last_err, now, raw_previous)
  if type(raw_previous) == "table" and (raw_previous.syncing or data_crossed_reset_boundary(raw_previous, now)) then
    return build_cache_entry(
      { syncing = true },
      previous_errors + 1,
      last_err,
      now,
      INVALID_STALE_RETRY_SECS
    )
  end

  local retry_secs = CLAUDE_TRANSIENT_RETRY_SECS
  local fast_retry = claude_fast_poll_secs(previous_data, now)
  if fast_retry then
    retry_secs = math.min(retry_secs, fast_retry)
  end

  return build_cache_entry(previous_data or { error = last_err }, previous_errors + 1, last_err, now, retry_secs)
end

-- ============================================================
-- CODEX STATE
-- ============================================================
local codex_cached    = nil
local codex_last_fetch = 0
local codex_errors    = 0
local codex_last_error = nil

local function sync_codex_shared_state(entry)
  if type(entry) ~= "table" then
    return
  end

  if type(entry.data) == "table" then
    codex_cached = entry.data
  end

  codex_last_fetch = tonumber(entry.written_at) or codex_last_fetch
  codex_errors = tonumber(entry.error_count) or 0
  codex_last_error = entry.last_error
end

-- Path to the bundled Codex helper script (resolved lazily on first use)
local CODEX_SCRIPT = nil
local function get_codex_script()
  if not CODEX_SCRIPT then
    CODEX_SCRIPT = config.codex_script or resolve_codex_script()
    wezterm.log_info("agent-quota: codex script resolved to: " .. tostring(CODEX_SCRIPT))
  end
  return CODEX_SCRIPT
end

-- Same candidate logic as codex-limits.py, for the tmux-oracle helper the Go bridge
-- shells out to. WEZTERM_AGENT_QUOTA_TMUX_HELPER overrides every candidate.
local function resolve_tmux_helper()
  local home = os.getenv("HOME") or os.getenv("USERPROFILE") or ""
  local plugin_dir = dirname(current_file_path())
  local candidates = {}

  local env_path = os.getenv("WEZTERM_AGENT_QUOTA_TMUX_HELPER")
  if env_path and env_path ~= "" then
    candidates[#candidates + 1] = env_path
  end

  -- The tmux helper ships beside codex-limits.py, so prefer its resolved directory.
  local codex_dir = dirname(config.codex_script or get_codex_script())
  if codex_dir then
    candidates[#candidates + 1] = codex_dir .. "/tmux-quota.py"
    candidates[#candidates + 1] = codex_dir .. "\\tmux-quota.py"
  end

  if plugin_dir then
    local repo_root = dirname(plugin_dir)
    if repo_root then
      candidates[#candidates + 1] = repo_root .. "/tmux-quota.py"
      candidates[#candidates + 1] = repo_root .. "\\tmux-quota.py"
    end
    candidates[#candidates + 1] = plugin_dir .. "/tmux-quota.py"
    candidates[#candidates + 1] = plugin_dir .. "\\tmux-quota.py"
  end

  if home ~= "" then
    if is_windows then
      local appdata = os.getenv("LOCALAPPDATA") or (home .. "\\AppData\\Local")
      candidates[#candidates + 1] = appdata .. "\\wezterm\\tmux-quota.py"
      candidates[#candidates + 1] = home .. "\\dev\\Plugins\\agent-quota.wezterm\\tmux-quota.py"
    else
      candidates[#candidates + 1] = home .. "/dev/Plugins/wezterm-quota-limit/tmux-quota.py"
      candidates[#candidates + 1] = home .. "/dev/Plugins/agent-quota.wezterm/tmux-quota.py"
      candidates[#candidates + 1] = home .. "/.local/share/wezterm/tmux-quota.py"
    end
  end

  for _, candidate in ipairs(candidates) do
    if file_exists(candidate) then
      return candidate
    end
  end

  return candidates[1] or "tmux-quota.py"
end

-- Resolved lazily on first Go fetch (the plugin dir is not known at load time)
local TMUX_HELPER = nil
local function get_tmux_helper()
  if not TMUX_HELPER then
    TMUX_HELPER = resolve_tmux_helper()
  end
  return TMUX_HELPER
end

-- Format a Unix timestamp as time-until string
local function time_until_unix(ts)
  if not ts then return "?" end
  local diff = ts - os.time()
  if diff <= 0 then return "now" end
  if diff < 3600  then return string.format("%dm", math.floor(diff / 60)) end
  if diff < 86400 then return string.format("%dh%dm", math.floor(diff / 3600), math.floor((diff % 3600) / 60)) end
  return string.format("%dd%dh", math.floor(diff / 86400), math.floor((diff % 86400) / 3600))
end

local function codex_window_label(window_mins)
  local mins = tonumber(window_mins)
  if not mins or mins <= 0 then
    return "window"
  elseif mins % 1440 == 0 then
    return string.format("%dd", math.floor(mins / 1440))
  elseif mins % 60 == 0 then
    return string.format("%dh", math.floor(mins / 60))
  end
  return string.format("%dm", math.floor(mins))
end

-- Returns true if a Codex session is running.
-- Used only as a display hint — never gates quota fetching.
local function is_codex_running()
  local now = os.time()
  if now - codex_running_checked_at < PROCESS_CHECK_TTL then
    return codex_running_cached
  end

  local found = false
  if is_windows then
    local ok, stdout = wezterm.run_child_process({
      "tasklist", "/FI", "IMAGENAME eq codex.exe", "/NH", "/FO", "CSV",
    })
    found = ok and stdout ~= nil and stdout:find('"codex.exe"') ~= nil
  else
    -- Single ps call; parse in Lua instead of spawning sh + grep + grep
    local ok, stdout = wezterm.run_child_process({ "ps", "-eo", "comm=,tty=" })
    if ok and stdout then
      for line in stdout:gmatch("[^\n]+") do
        if line:match("^codex ") and not line:match("%s%?$") then
          found = true
          break
        end
      end
    end
  end

  codex_running_cached = found
  codex_running_checked_at = now
  return codex_running_cached
end

local function fetch_codex_limits()
  local now = os.time()

  -- In-memory fast path: skip all I/O if checked very recently
  if codex_last_result and (now - codex_last_result_at) < FETCH_GATE_SECS then
    return codex_last_result, codex_running_cached
  end

  local codex_active = is_codex_running()

  -- No running gate: quota is account-level and always fetchable.
  -- is_codex_running() is used only in the display layer as an activity hint.

  if not file_exists(get_codex_script()) then
    codex_cached = { error = "missing bundled codex helper" }
    codex_errors = codex_errors + 1
    codex_last_error = "missing bundled codex helper"
    codex_last_result = codex_cached
    codex_last_result_at = now
    return codex_cached, codex_active
  end

  -- Read shared cache, but discard it if it contains stale not-running data
  -- (written during a period when detection failed but Codex was actually running)
  local shared = read_shared_cache(CODEX_CACHE_PATH)
  if shared and type(shared.data) == "table" then
    local d = shared.data
    -- Only clear the old boolean not_running flag (written by a previous code version).
    -- The string error "not running" is legitimate output from codex app-server and
    -- must be respected with its backoff — do not clear it here.
    if d.not_running then
      os.remove(CODEX_CACHE_PATH)
      shared = nil
      codex_cached = nil
      codex_errors = 0
      codex_last_error = nil
    end
  end

  sync_codex_shared_state(shared)

  if shared_cache_is_fresh(shared, now) then
    if not (codex_active and type(shared.data) == "table" and shared.data.error == "not running") then
      codex_last_result = shared.data
      codex_last_result_at = now
      return shared.data, codex_active
    end
  end

  if not acquire_lock(CODEX_LOCK_DIR) then
    shared = read_shared_cache(CODEX_CACHE_PATH)
    sync_codex_shared_state(shared)
    if shared and shared.data then
      codex_last_result = shared.data
      codex_last_result_at = now
      return shared.data, codex_active
    end
    local fallback = codex_cached or { error = codex_last_error or "waiting for shared refresh" }
    codex_last_result = fallback
    codex_last_result_at = now
    return fallback, codex_active
  end

  local entry
  local locked_cache = read_shared_cache(CODEX_CACHE_PATH)
  sync_codex_shared_state(locked_cache)

  if shared_cache_is_fresh(locked_cache, now) then
    if not (codex_active and type(locked_cache.data) == "table" and locked_cache.data.error == "not running") then
      release_lock(CODEX_LOCK_DIR)
      codex_last_result = locked_cache.data
      codex_last_result_at = now
      return locked_cache.data, codex_active
    end
  end

  local raw_previous = (locked_cache and locked_cache.data) or codex_cached
  local previous_data = cacheable_data(raw_previous, now)
  local previous_errors = tonumber(locked_cache and locked_cache.error_count) or codex_errors or 0

  -- Query Codex rate limits via the helper script
  local python_cmd = is_windows and "python" or "python3"
  local success, stdout, stderr = wezterm.run_child_process({ python_cmd, get_codex_script() })
  local raw = stdout and stdout:match("^%s*(.-)%s*$") or ""

  if raw == "" then
    local err = (stderr and stderr ~= "") and stderr or "codex helper failed"
    entry = transient_refresh_entry(previous_data, previous_errors, err, now, raw_previous)
  else
    local ok, data = pcall(wezterm.json_parse, raw)

    if not ok or not data then
      local err = (stderr and stderr ~= "") and stderr or "codex helper parse failed"
      entry = transient_refresh_entry(previous_data, previous_errors, err, now, raw_previous)
    elseif data.error then
      local err = tostring(data.error)
      if err == "not running" then
        -- Codex app-server reports "not running" when no session is active.
        -- This is expected; use a short retry (30s) and don't escalate the error count.
        entry = build_cache_entry({ error = err }, 0, err, now, 30)
      else
        entry = transient_refresh_entry(previous_data, previous_errors, err, now, raw_previous)
      end
    elseif not success then
      local err = (stderr and stderr ~= "") and stderr or "codex helper failed"
      entry = transient_refresh_entry(previous_data, previous_errors, err, now, raw_previous)
    else
      local primary = data.rateLimits and data.rateLimits.primary
      local secondary = data.rateLimits and data.rateLimits.secondary

      if not primary then
        local err = "no rate limit data"
        entry = transient_refresh_entry(previous_data, previous_errors, err, now, raw_previous)
      else
        entry = build_cache_entry({
          primary_pct = primary.usedPercent,
          primary_reset = time_until_unix(primary.resetsAt),
          primary_reset_at = primary.resetsAt,
          secondary_pct = secondary and secondary.usedPercent or nil,
          secondary_reset = secondary and time_until_unix(secondary.resetsAt) or nil,
          secondary_reset_at = secondary and secondary.resetsAt or nil,
          primary_mins = primary.windowDurationMins,
          secondary_mins = secondary and secondary.windowDurationMins or nil,
        }, 0, nil, now)
      end
    end
  end

  local wrote, write_err = write_json_file(CODEX_CACHE_PATH, entry)
  if not wrote then
    wezterm.log_error("codex shared cache write failed: " .. tostring(write_err))
  end

  release_lock(CODEX_LOCK_DIR)
  sync_codex_shared_state(entry)
  codex_last_result = entry.data
  codex_last_result_at = now
  return entry.data, codex_active
end

-- ============================================================
-- CREDENTIALS FILE PATH (Claude)
-- ============================================================

-- Credentials file path
local function cred_path()
  local home = os.getenv("USERPROFILE") or os.getenv("HOME") or ""
  return home .. "/.claude/.credentials.json"
end

-- Read credentials file
local function read_credentials()
  local path = cred_path()
  local f = io.open(path, "r")
  if not f then
    f = io.open(path:gsub("/", "\\"), "r")
  end
  if not f then
    return nil, "no credentials file"
  end
  local content = f:read("*a")
  f:close()
  return content, nil
end

-- Read OAuth token and expiry from credentials file
local function get_token()
  local content, err = read_credentials()
  if not content then
    return nil, nil, err
  end

  -- Use %b{} to extract the claudeAiOauth block so expiresAt doesn't match
  -- an earlier field (e.g. discoveryState.expiresAt = 0) in the credentials file.
  local block = content:match('"claudeAiOauth"%s*:%s*(%b{})')
  if not block then
    return nil, nil, "no claudeAiOauth block in credentials"
  end

  local token = block:match('"accessToken"%s*:%s*"([^"]+)"')
  if not token then
    return nil, nil, "no accessToken in credentials"
  end

  local expires_at = block:match('"expiresAt"%s*:%s*(%d+)')
  return token, tonumber(expires_at), nil
end

-- Format time remaining until reset
local function time_until(reset_str)
  if not reset_str then
    return "?"
  end

  local reset_time = unix_from_iso_utc(reset_str)
  if not reset_time then
    return "?"
  end

  local diff = reset_time - os.time()

  if diff <= 0 then
    return "now"
  elseif diff < 3600 then
    return string.format("%dm", math.floor(diff / 60))
  elseif diff < 86400 then
    return string.format("%dh%dm", math.floor(diff / 3600), math.floor((diff % 3600) / 60))
  else
    return string.format("%dd%dh", math.floor(diff / 86400), math.floor((diff % 86400) / 3600))
  end
end

local function sync_claude_shared_state(entry)
  if type(entry) ~= "table" then
    return
  end

  if type(entry.data) == "table" then
    cached_data = entry.data
  end

  last_fetch_time = tonumber(entry.written_at) or last_fetch_time
  consecutive_errors = tonumber(entry.error_count) or 0
  last_error = entry.last_error
end

-- Detect Claude Code version (cached after first call)
local claude_version = nil
local function get_claude_version()
  if claude_version then
    return claude_version
  end
  local ok, stdout = pcall(function()
    local success, out = wezterm.run_child_process({ "claude", "--version" })
    if success and out then
      return out
    end
    return nil
  end)
  if ok and stdout then
    local ver = stdout:match("(%d+%.%d+%.%d+)")
    if ver then
      claude_version = ver
      return claude_version
    end
  end
  claude_version = "0.0.0"
  return claude_version
end

-- Make an API request to the usage endpoint
local function call_usage_api(token)
  local success, stdout, stderr = wezterm.run_child_process({
    "curl",
    "-s",
    "-m", "5",
    "-w", "\n%{http_code}",
    "https://api.anthropic.com/api/oauth/usage",
    "-H", "Authorization: Bearer " .. token,
    "-H", "anthropic-beta: oauth-2025-04-20",
    "-H", "Content-Type: application/json",
    "-H", "User-Agent: claude-code/" .. get_claude_version(),
  })

  if not success or not stdout or stdout == "" then
    return nil, nil, "curl failed"
  end

  local body, http_code = stdout:match("^(.*)\n(%d+)$")
  if not body then
    return stdout, nil, nil
  end

  return body, tonumber(http_code), nil
end

local function has_claude_cli_process(output)
  if type(output) ~= "string" then
    return false
  end

  for line in output:gmatch("[^\r\n]+") do
    if line:match("%S") and not line:find("--chrome-native-host", 1, true) then
      return true
    end
  end

  return false
end

local function is_claude_running()
  local now = os.time()
  if now - claude_running_checked_at < PROCESS_CHECK_TTL then
    return claude_running_cached
  end

  if is_windows then
    local ok, stdout = wezterm.run_child_process({
      "powershell", "-NoProfile", "-Command",
      "Get-CimInstance Win32_Process -Filter \"Name = 'claude.exe'\""
        .. " | ForEach-Object { $_.CommandLine }",
    })
    claude_running_cached = ok and has_claude_cli_process(stdout)
  else
    local ok, stdout = wezterm.run_child_process({ "pgrep", "-a", "-x", "claude" })
    claude_running_cached = ok and has_claude_cli_process(stdout)
  end

  claude_running_checked_at = now
  return claude_running_cached
end

-- Fetch usage data (synchronous curl call cached at the polling interval)
local function fetch_usage()
  local now = os.time()

  -- In-memory fast path: skip all I/O if checked very recently
  if claude_last_result and (now - claude_last_result_at) < FETCH_GATE_SECS then
    return claude_last_result
  end

  -- Always check process state first — never show stale data when Claude is closed
  if not is_claude_running() then
    claude_last_result = { not_running = true }
    claude_last_result_at = now
    return claude_last_result
  end

  local shared = read_shared_cache(CLAUDE_CACHE_PATH)
  sync_claude_shared_state(shared)

  if shared_cache_is_fresh(shared, now) then
    claude_last_result = shared.data
    claude_last_result_at = now
    return shared.data
  end

  if not acquire_lock(CLAUDE_LOCK_DIR) then
    shared = read_shared_cache(CLAUDE_CACHE_PATH)
    sync_claude_shared_state(shared)
    if shared and shared.data then
      claude_last_result = shared.data
      claude_last_result_at = now
      return shared.data
    end
    local fallback = cached_data or { error = last_error or "waiting for shared refresh" }
    claude_last_result = fallback
    claude_last_result_at = now
    return fallback
  end

  local entry
  local locked_cache = read_shared_cache(CLAUDE_CACHE_PATH)
  sync_claude_shared_state(locked_cache)

  if shared_cache_is_fresh(locked_cache, now) then
    release_lock(CLAUDE_LOCK_DIR)
    claude_last_result = locked_cache.data
    claude_last_result_at = now
    return locked_cache.data
  end

  local raw_previous = (locked_cache and locked_cache.data) or cached_data
  local previous_data = cacheable_data(raw_previous, now)
  local previous_errors = tonumber(locked_cache and locked_cache.error_count) or consecutive_errors or 0

  -- Re-read token from disk each fetch — Claude Code may have refreshed it
  local token, expires_at, err = get_token()
  if not token then
    entry = claude_transient_refresh_entry(previous_data, previous_errors, err, now, raw_previous)
  else
    -- If the token changed on disk (Claude Code refreshed it), reset error state
    if cached_token and token ~= cached_token then
      previous_errors = 0
      last_error = nil
    end
    cached_token = token

    -- If the token is expired, don't call the API — wait for Claude Code to refresh
    local now_ms = math.floor(now * 1000)
    if expires_at and now_ms >= expires_at then
      local token_err = "token expired — waiting for Claude Code"
      entry = claude_transient_refresh_entry(previous_data, previous_errors, token_err, now, raw_previous)
    else
      local body, status, curl_err = call_usage_api(token)

      if curl_err then
        entry = claude_transient_refresh_entry(previous_data, previous_errors, curl_err, now, raw_previous)
      elseif status == 429 then
        local next_errors = previous_errors + 1
        local wait = interval_for_errors(next_errors)
        local rate_err = string.format("rate limited (retry in %dm)", math.ceil(wait / 60))
        if previous_data then
          entry = build_cache_entry(previous_data, next_errors, rate_err, now, CLAUDE_RATE_LIMIT_RETRY_SECS)
        else
          entry = build_cache_entry({ syncing = true }, next_errors, rate_err, now, CLAUDE_RATE_LIMIT_RETRY_SECS)
        end
      elseif status == 401 or status == 403 then
        local auth_err = "auth failed — waiting for Claude Code"
        entry = claude_transient_refresh_entry(previous_data, previous_errors, auth_err, now, raw_previous)
      else
        local ok, data = pcall(wezterm.json_parse, body)
        if not ok or not data then
          local parse_err = "parse failed"
          entry = claude_transient_refresh_entry(previous_data, previous_errors, parse_err, now, raw_previous)
        elseif data.error then
          local api_err = data.error.message or "api error"
          entry = claude_transient_refresh_entry(previous_data, previous_errors, api_err, now, raw_previous)
        else
          entry = build_cache_entry(data, 0, nil, now, claude_success_retry_secs(data, now))
        end
      end
    end
  end

  local wrote, write_err = write_json_file(CLAUDE_CACHE_PATH, entry)
  if not wrote then
    wezterm.log_error("claude shared cache write failed: " .. tostring(write_err))
  end

  release_lock(CLAUDE_LOCK_DIR)
  sync_claude_shared_state(entry)
  claude_last_result = entry.data
  claude_last_result_at = now
  return entry.data
end

-- ============================================================
-- OPENCODE GO (tmux oracle bridge)
-- ============================================================
local GO_GATE_SECS = 15          -- min seconds between Go helper runs
local GO_UNSUPPORTED = "unsupported"

local go_last_report = nil
local go_last_run_at = 0

local function opencode_go_enabled()
  return type(config.opencode_go) == "table" and config.opencode_go.enabled == true
end

local function go_argv()
  local argv = { "python3", get_tmux_helper(), "--json", "--providers", "opencode-go" }
  local key_command = type(config.opencode_go) == "table" and config.opencode_go.key_command or nil
  if type(key_command) == "string" and key_command ~= "" then
    argv[#argv + 1] = "--opencode-key-command"
    argv[#argv + 1] = key_command
  end
  return argv
end

-- Only the documented shape is accepted; anything else is a failed bridge run.
local function parse_go_report(raw)
  if type(raw) ~= "string" or raw == "" then
    return nil
  end

  local ok, payload = pcall(wezterm.json_parse, raw)
  if not ok or type(payload) ~= "table" or type(payload.providers) ~= "table" then
    return nil
  end

  for _, entry in ipairs(payload.providers) do
    if type(entry) == "table" and entry.name == "opencode-go" then
      if type(entry.state) ~= "string" then
        return nil
      end

      local windows = {}
      if type(entry.windows) == "table" then
        for _, window in ipairs(entry.windows) do
          local percent = type(window) == "table" and tonumber(window.percent) or nil
          if percent and percent == percent and percent ~= math.huge and percent ~= -math.huge then
            local reset_at = tonumber(window.reset_at)
            windows[#windows + 1] = {
              label = type(window.label) == "string" and window.label or "usage",
              percent = percent,
              reset_at = reset_at,
              limited = window.limited == true,
            }
          end
        end
      end

      return { state = entry.state, stale = entry.stale == true, windows = windows }
    end
  end

  return nil
end

local function fetch_opencode_go()
  if not opencode_go_enabled() then
    return nil
  end

  -- The helper is POSIX-only; report it instead of spawning anything on Windows.
  if is_windows then
    return { state = GO_UNSUPPORTED, stale = false, windows = {} }
  end

  local now = os.time()
  if not go_last_report or (now - go_last_run_at) >= GO_GATE_SECS then
    go_last_run_at = now
    local ok, stdout, stderr = wezterm.run_child_process(go_argv())
    local report = nil
    if ok and type(stdout) == "string" then
      report = parse_go_report(stdout)
    end
    if not report then
      wezterm.log_error("agent-quota: opencode go helper failed: " .. tostring(stderr or stdout))
      report = { state = "unavailable", stale = false, windows = {} }
    end
    go_last_report = report
  end

  -- Between bridge runs, windows whose reset already passed must stop counting.
  local windows = {}
  for _, window in ipairs(go_last_report.windows) do
    if window.reset_at == nil or now < window.reset_at then
      windows[#windows + 1] = window
    end
  end

  local state = go_last_report.state
  if state == "ok" and #windows == 0 then
    state = "syncing"
  end

  return { state = state, stale = go_last_report.stale, windows = windows }
end

-- ============================================================
-- PROVIDER REPORTS (one normalised shape for the dense layouts)
-- ============================================================
local function claude_report(data)
  if type(data) ~= "table" then
    return { state = "unavailable", windows = {} }
  end
  if data.not_running then
    return { state = "not_running", windows = {} }
  end
  if is_rate_limited_error(data.error) then
    return { state = "rate_limited", windows = {} }
  end
  if data.syncing then
    return { state = "syncing", windows = {} }
  end
  if data.error then
    return { state = "error", windows = {} }
  end

  local five = type(data.five_hour) == "table" and data.five_hour or {}
  local seven = type(data.seven_day) == "table" and data.seven_day or {}
  return {
    state = "ok",
    windows = {
      {
        label = "5h",
        percent = tonumber(five.utilization) or 0,
        reset_at = unix_from_iso_utc(five.resets_at),
        limited = false,
      },
      {
        label = "7d",
        percent = tonumber(seven.utilization) or 0,
        reset_at = unix_from_iso_utc(seven.resets_at),
        limited = false,
      },
    },
  }
end

local function codex_report(cd, active)
  if type(cd) ~= "table" then
    return { state = "unavailable", windows = {} }
  end
  if not active then
    return { state = "not_running", windows = {} }
  end
  if cd.error == "not running" then
    return { state = "syncing", windows = {} }
  end
  if cd.syncing then
    return { state = "syncing", windows = {} }
  end
  if cd.ready then
    return { state = "ready", windows = {} }
  end
  if cd.error then
    return { state = "error", windows = {} }
  end

  local windows = {}
  if type(cd.primary_pct) == "number" then
    windows[#windows + 1] = {
      label = codex_window_label(cd.primary_mins),
      percent = cd.primary_pct,
      reset_at = tonumber(cd.primary_reset_at),
      limited = false,
    }
  end
  if type(cd.secondary_pct) == "number" then
    windows[#windows + 1] = {
      label = codex_window_label(cd.secondary_mins),
      percent = cd.secondary_pct,
      reset_at = tonumber(cd.secondary_reset_at),
      limited = false,
    }
  end
  if #windows == 0 then
    return { state = "syncing", windows = {} }
  end
  return { state = "ok", windows = windows }
end

-- ============================================================
-- DENSE LAYOUTS (same rules as the tmux --layout compact/minimal)
-- ============================================================
local GAUGE_GLYPHS = { "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█" }

local function gauge_cell(percent)
  local level = math.ceil((tonumber(percent) or 0) / 12.5)
  if level < 1 then
    level = 1
  elseif level > #GAUGE_GLYPHS then
    level = #GAUGE_GLYPHS
  end
  return GAUGE_GLYPHS[level]
end

local function status_label(value)
  local text = tostring(value or "usage"):gsub("[^A-Za-z0-9 _.-]", "")
  if text == "" then
    return "usage"
  end
  return text:sub(1, 48)
end

local function is_short_window(label)
  local first = label:match("^(%S+)")
  return first ~= nil and first:match("^%d+[mh]$") ~= nil
end

local function countdown_unix(reset_at, now)
  if type(reset_at) ~= "number" then
    return nil
  end

  local diff = math.max(0, math.floor(reset_at - now))
  local days = math.floor(diff / 86400)
  local hours = math.floor((diff % 86400) / 3600)
  local minutes = math.floor((diff % 3600) / 60)
  if days > 0 then
    return string.format("%dd%dh", days, hours)
  elseif hours > 0 then
    return string.format("%dh%02dm", hours, minutes)
  end
  return string.format("%dm", minutes)
end

local function dense_window(window, now)
  local label = status_label(window.label)
  local percent = tonumber(window.percent) or 0
  local short = is_short_window(label)
  local color = usage_color_esc(percent)
  local pct_color = window.limited and RED or color

  local parts = { BRIGHT .. label .. RESET }
  if short then
    local gauge_color = percent <= 0 and DIM or color
    parts[#parts + 1] = gauge_color .. gauge_cell(percent) .. RESET
  end
  local marker = window.limited and "!" or ""
  parts[#parts + 1] = pct_color .. string.format("%.0f%%", percent) .. marker .. RESET

  local remaining = countdown_unix(window.reset_at, now)
  if remaining and (short or window.limited or percent >= 80) then
    parts[#parts + 1] = DIM .. remaining .. RESET
  end
  return table.concat(parts, " ")
end

local function dense_state_text(state)
  if state == "unsupported" then
    return "unsupported"
  elseif state == "no_key" then
    return "no key"
  elseif state == "no_plan" then
    return "no plan"
  elseif state == "ready" then
    return "ready"
  elseif state == "syncing" or state == "rate_limited" then
    return "…"
  end
  return "?"
end

local function dense_provider(icon, name, report, layout, now)
  if report.state == "not_running" then
    return DIM .. icon .. " " .. name .. ":"
  end

  local head = BRIGHT .. icon .. " " .. name .. ":"
  if report.stale then
    head = head .. STALE .. "~" .. RESET
  end

  local windows = report.windows
  if #windows == 0 then
    return head .. " " .. BRIGHT .. dense_state_text(report.state) .. RESET
  end

  if layout == "minimal" then
    local highest = 0
    local limited = false
    for _, window in ipairs(windows) do
      local percent = tonumber(window.percent) or 0
      if percent > highest then
        highest = percent
      end
      if window.limited then
        limited = true
      end
    end
    local color = limited and RED or usage_color_esc(highest)
    local bang = limited and "!" or ""
    return head .. " " .. color .. string.format("%.0f%%", highest) .. bang .. RESET
  end

  local rendered = {}
  for _, window in ipairs(windows) do
    rendered[#rendered + 1] = dense_window(window, now)
  end
  return head .. " " .. table.concat(rendered, DIM .. " · ")
end

-- OpenCode Go in the legacy full style: bars, parenthesised countdowns.
local function full_go_str(report, now)
  local gi = " " .. (config.icons.opencode_go or "◈") .. " "
  local head = DIM .. gi .. BRIGHT .. "OpenCode Go: "
  local windows = report and report.windows or {}

  if #windows == 0 then
    local state = report and report.state or "unavailable"
    local text
    if state == "not_running" then
      text = "not running"
    elseif state == "unsupported" then
      text = "not supported on Windows"
    elseif state == "no_key" then
      text = "no key"
    elseif state == "no_plan" then
      text = "no Go plan"
    elseif state == "ready" then
      text = "ready"
    elseif state == "syncing" or state == "rate_limited" then
      text = "syncing..."
    else
      text = "unavailable"
    end
    return head .. DIM .. text
  end

  if report.stale then
    head = head .. STALE .. "stale "
  end
  local parts = {}
  for _, window in ipairs(windows) do
    local label = status_label(window.label)
    local percent = tonumber(window.percent) or 0
    local segment = BRIGHT .. label .. " "
    local bar = usage_bar_esc(percent)
    if bar then
      segment = segment .. bar .. DIM .. " "
    end
    segment = segment .. usage_color_esc(percent) .. string.format("%.0f%%", percent)
    if window.limited then
      segment = segment .. RED .. " limited"
    end
    local remaining = countdown_unix(window.reset_at, now)
    if remaining then
      segment = segment .. DIM .. " (" .. remaining .. ")"
    end
    parts[#parts + 1] = segment
  end
  local separator = DIM .. "  " .. (config.icons.week or "▪") .. " "
  return head .. table.concat(parts, separator)
end

-- Dashboard URL
local DASHBOARD_URL = "https://console.anthropic.com/settings/usage"

-- Build status string using raw ANSI escapes (avoids wezterm.format deserialization issues)
local function claude_full_str(data)
  local claude_str
  local ci = " " .. (config.icons.claude or config.icons.bolt or "⚡") .. " "
  if data.not_running then
    claude_str = DIM .. ci .. BRIGHT .. "Claude: " .. DIM .. "not running"
  elseif data.syncing then
    claude_str = DIM .. ci .. BRIGHT .. "Claude: " .. DIM .. "syncing..."
  elseif is_rate_limited_error(data.error) then
    claude_str = DIM .. ci .. BRIGHT .. "Claude: " .. DIM .. "syncing..."
  elseif data.error then
    claude_str = DIM .. ci .. "Claude: "
      .. hex_to_fg("#f7768e") .. tostring(data.error)
  else
    local five_pct   = data.five_hour and data.five_hour.utilization or 0
    local five_reset = data.five_hour and data.five_hour.resets_at
    local seven_pct  = data.seven_day and data.seven_day.utilization or 0
    local seven_reset = data.seven_day and data.seven_day.resets_at
    local five_bar   = usage_bar_esc(five_pct)
    local seven_bar  = usage_bar_esc(seven_pct)

    claude_str = DIM .. ci .. BRIGHT .. "Claude: "
      .. BRIGHT .. "5h "
    if five_bar then
      claude_str = claude_str .. five_bar .. DIM .. " "
    end
    claude_str = claude_str
      .. usage_color_esc(five_pct) .. string.format("%.0f%%", five_pct)
    if not config.compact then
      claude_str = claude_str .. DIM .. " (" .. time_until(five_reset) .. ")"
    end

    claude_str = claude_str .. DIM .. "  " .. config.icons.week .. " "
      .. BRIGHT .. "7d "
    if seven_bar then
      claude_str = claude_str .. seven_bar .. DIM .. " "
    end
    claude_str = claude_str
      .. usage_color_esc(seven_pct) .. string.format("%.0f%%", seven_pct)
    if not config.compact then
      claude_str = claude_str .. DIM .. " (" .. time_until(seven_reset) .. ")"
    end
  end
  return claude_str
end

local function codex_full_str(cd, codex_active)
  local codex_str
  local coi = " " .. (config.icons.codex or "✦") .. " "
  if not codex_active then
    codex_str = DIM .. coi .. BRIGHT .. "Codex: " .. DIM .. "not running"

  elseif cd.error == "not running" then
    codex_str = DIM .. coi .. BRIGHT .. "Codex: " .. DIM .. "loading..."
  elseif cd.syncing then
    codex_str = DIM .. coi .. BRIGHT .. "Codex: " .. DIM .. "syncing..."

  elseif cd.ready then
    codex_str = DIM .. coi .. BRIGHT .. "Codex: " .. hex_to_fg("#9ece6a") .. "ready"

  elseif cd.error then
    codex_str = DIM .. coi .. "Codex: " .. hex_to_fg("#f7768e") .. tostring(cd.error)

  elseif cd.primary_pct ~= nil then
    -- Full usage data from app-server
    local win_label = codex_window_label(cd.primary_mins)
    local primary_bar = usage_bar_esc(cd.primary_pct)
    local secondary_bar = cd.secondary_pct ~= nil and usage_bar_esc(cd.secondary_pct) or nil
    codex_str = DIM .. coi .. BRIGHT .. "Codex: "
      .. BRIGHT .. win_label .. " "
    if primary_bar then
      codex_str = codex_str .. primary_bar .. DIM .. " "
    end
    codex_str = codex_str
      .. usage_color_esc(cd.primary_pct) .. string.format("%.0f%%", cd.primary_pct)
    if cd.primary_reset and not config.compact then
      codex_str = codex_str .. DIM .. " (" .. cd.primary_reset .. ")"
    end
    if cd.secondary_pct ~= nil then
      local secondary_label = codex_window_label(cd.secondary_mins)
      codex_str = codex_str .. DIM .. "  " .. config.icons.week .. " "
        .. BRIGHT .. secondary_label .. " "
      if secondary_bar then
        codex_str = codex_str .. secondary_bar .. DIM .. " "
      end
      codex_str = codex_str
        .. usage_color_esc(cd.secondary_pct) .. string.format("%.0f%%", cd.secondary_pct)
      if cd.secondary_reset and not config.compact then
        codex_str = codex_str .. DIM .. " (" .. cd.secondary_reset .. ")"
      end
    end

  else
    codex_str = DIM .. coi .. BRIGHT .. "Codex: " .. DIM .. "loading..."
  end
  return codex_str
end

local function build_status_string(data, window, pane)
  local layout = config.layout
  local use_dense = layout == "compact" or layout == "minimal"
  local now = os.time()

  local cd, codex_active = fetch_codex_limits()
  local go_report = opencode_go_enabled() and fetch_opencode_go() or nil

  local claude_active = not (type(data) == "table" and data.not_running)

  local segments = {}
  local function push(active, dense_text, full_text)
    if config.hide_inactive and not active then
      return
    end
    segments[#segments + 1] = use_dense and dense_text or full_text
  end

  push(
    claude_active,
    dense_provider(
      config.icons.claude or config.icons.bolt or "⚡", "Claude",
      claude_report(data), layout, now
    ),
    claude_full_str(data)
  )

  push(
    codex_active,
    dense_provider(config.icons.codex or "✦", "Codex", codex_report(cd, codex_active), layout, now),
    codex_full_str(cd, codex_active)
  )

  if go_report then
    push(
      go_report.state ~= "not_running",
      dense_provider(config.icons.opencode_go or "◈", "OpenCode Go", go_report, layout, now),
      full_go_str(go_report, now)
    )
  end

  if #segments == 0 then
    return ""
  end

  if use_dense then
    local joiner = "  " .. DIM .. "│" .. RESET .. "  "
    return " " .. table.concat(segments, joiner) .. " " .. RESET
  end

  -- Legacy full line: the same separator and trailing reset as before.
  return table.concat(segments, DIM .. "  |") .. " " .. RESET
end

function M.apply_to_config(c, opts)
  if opts then
    config = deep_merge(config, opts)
    CODEX_SCRIPT = nil -- reset so get_codex_script() re-resolves with new config
    TMUX_HELPER = nil  -- and so the Go bridge re-resolves its helper
    go_last_report = nil
    go_last_run_at = 0
  end

  -- Add keybinding to open usage dashboard
  if config.dashboard_key then
    local act = wezterm.action
    local keys = c.keys or {}
    table.insert(keys, {
      key = config.dashboard_key.key,
      mods = config.dashboard_key.mods,
      action = act.EmitEvent("open-claude-dashboard"),
    })
    c.keys = keys

    wezterm.on("open-claude-dashboard", function()
      wezterm.open_with(DASHBOARD_URL)
    end)
  end

  -- Guard against duplicate handler registration
  if handler_registered then
    return
  end
  handler_registered = true

  wezterm.on("update-status", function(window, pane)
    local ok, err = pcall(function()
      local data = fetch_usage()
      local status = build_status_string(data, window, pane)

      if config.position == "left" then
        window:set_left_status(status)
      else
        window:set_right_status(status)
      end
    end)
    if not ok then
      wezterm.log_error("claude-usage: " .. tostring(err))
    end
  end)
end

-- Test-only hook; inert unless the harness sets the global before loading this file.
if rawget(_G, "WEZTERM_QUOTA_TEST") then
  M._test = {
    build_status_string = build_status_string,
    fetch_opencode_go = fetch_opencode_go,
    parse_go_report = parse_go_report,
    go_argv = go_argv,
    dense_provider = dense_provider,
    reset_go = function()
      go_last_report = nil
      go_last_run_at = 0
    end,
  }
end

return M
