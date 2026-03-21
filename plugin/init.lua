local wezterm = require("wezterm")

local M = {}

-- Config defaults
local config = {
  poll_interval_secs = 60,
  position = "right", -- "left" or "right"
  dashboard_key = { key = "u", mods = "CTRL|SHIFT" }, -- keybind to open dashboard
  icons = {
    bolt = "⚡",
    week = "▪",
  },
  bars = {
    enabled = true,
    width = 6,
    full = "█",
    empty = "░",
  },
}

-- Cached usage data
local cached_data = nil
local last_fetch_time = 0
local consecutive_errors = 0
local last_error = nil
local handler_registered = false
local cached_token = nil

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

-- Legacy FormatItem helpers (kept for compatibility if wezterm.format works)
local function usage_color(pct)
  if pct >= 80 then
    return { Foreground = { Color = "#f7768e" } } -- red
  elseif pct >= 50 then
    return { Foreground = { Color = "#e0af68" } } -- yellow
  else
    return { Foreground = { Color = "#9ece6a" } } -- green
  end
end

local function dim()
  return { Foreground = { Color = "#565f89" } }
end

local function bright()
  return { Foreground = { Color = "#c0caf5" } }
end

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
  local info = debug.getinfo(1, "S")
  local source = info and info.source or nil
  if type(source) == "string" and source:sub(1, 1) == "@" then
    return source:sub(2)
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
  local plugin_dir = dirname(current_file_path())
  local candidates = {}

  if plugin_dir then
    candidates[#candidates + 1] = plugin_dir .. "/../codex-limits.py"
    candidates[#candidates + 1] = plugin_dir .. "/codex-limits.py"
  end

  for _, candidate in ipairs(candidates) do
    if file_exists(candidate) then
      return candidate
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
  elseif normalized <= 0 then
    filled = 0
  end

  return usage_color_esc(normalized) .. string.rep(full, filled)
    .. DIM .. string.rep(empty, width - filled)
end

local function cache_prefix()
  local user = os.getenv("USER") or os.getenv("USERNAME") or "user"
  user = user:gsub("[^%w_.-]", "_")
  return "/tmp/wezterm-quota-limit-" .. user
end

local SHARED_CACHE_PREFIX = cache_prefix()
local CLAUDE_CACHE_PATH = SHARED_CACHE_PREFIX .. "-claude.json"
local CLAUDE_LOCK_DIR = SHARED_CACHE_PREFIX .. "-claude.lock"
local CODEX_CACHE_PATH = SHARED_CACHE_PREFIX .. "-codex.json"
local CODEX_LOCK_DIR = SHARED_CACHE_PREFIX .. "-codex.lock"
local LOCK_TIMEOUT_SECS = 30

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

local function shared_cache_is_fresh(entry, now)
  if type(entry) ~= "table" or type(entry.data) ~= "table" then
    return false
  end

  local next_refresh_at = tonumber(entry.next_refresh_at)
  return next_refresh_at ~= nil and now < next_refresh_at
end

local function interval_for_errors(error_count)
  if not error_count or error_count <= 0 then
    return config.poll_interval_secs
  end

  return math.min(120 * (2 ^ (error_count - 1)), 1800)
end

local function build_cache_entry(data, error_count, last_err, now)
  return {
    written_at = now,
    next_refresh_at = now + interval_for_errors(error_count),
    error_count = error_count,
    last_error = last_err,
    data = data,
  }
end

local function lock_age_secs(lock_dir)
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
  local ok = wezterm.run_child_process({ "mkdir", lock_dir })
  if ok then
    return true
  end

  local age = lock_age_secs(lock_dir)
  if age and age > LOCK_TIMEOUT_SECS then
    wezterm.run_child_process({ "rmdir", lock_dir })
    return wezterm.run_child_process({ "mkdir", lock_dir })
  end

  return false
end

local function release_lock(lock_dir)
  wezterm.run_child_process({ "rmdir", lock_dir })
end

local function cacheable_data(data)
  if type(data) ~= "table" or data.not_running then
    return nil
  end
  return data
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

local function codex_cred_path()
  local home = os.getenv("HOME") or ""
  return home .. "/.codex/auth.json"
end

-- Decode the JWT exp field without any network call
-- JWT payload is base64url encoded — use python3 to decode it
local function get_codex_token_info()
  local f = io.open(codex_cred_path(), "r")
  if not f then return nil, nil, "no codex auth" end
  local content = f:read("*a")
  f:close()

  local token = content:match('"access_token"%s*:%s*"([^"]+)"')
  if not token then return nil, nil, "no access_token" end

  -- Extract JWT payload (middle segment)
  local payload_b64 = token:match("^[^.]+%.([^.]+)%.")
  if not payload_b64 then return token, nil, nil end

  -- Decode via python3 (always available on Linux)
  local ok, stdout = wezterm.run_child_process({
    "python3", "-c",
    string.format(
      "import base64,json,sys; p='%s'; p+='='*(4-len(p)%%4); d=json.loads(base64.urlsafe_b64decode(p)); print(d.get('exp',''))",
      payload_b64
    ),
  })

  local exp = (ok and stdout) and tonumber(stdout:match("(%d+)")) or nil
  return token, exp, nil
end

local function format_expiry(exp)
  if not exp then return nil end
  local diff = exp - os.time()
  if diff <= 0 then return "expired" end
  if diff < 3600   then return string.format("%dm", math.floor(diff / 60)) end
  if diff < 86400  then return string.format("%dh", math.floor(diff / 3600)) end
  return string.format("%dd", math.floor(diff / 86400))
end

-- Path to the bundled Codex helper script
local CODEX_SCRIPT = resolve_codex_script()

-- Format a Unix timestamp as time-until string
local function time_until_unix(ts)
  if not ts then return "?" end
  local diff = ts - os.time()
  if diff <= 0 then return "now" end
  if diff < 3600  then return string.format("%dm", math.floor(diff / 60)) end
  if diff < 86400 then return string.format("%dh%dm", math.floor(diff / 3600), math.floor((diff % 3600) / 60)) end
  return string.format("%dd%dh", math.floor(diff / 86400), math.floor((diff % 86400) / 3600))
end

local function basename(path)
  if not path or path == "" then
    return nil
  end
  return path:gsub("(.*[/\\])(.*)", "%2")
end

local function proc_info_looks_like_codex(proc)
  if not proc then
    return false
  end

  local exe = basename(proc.executable)
  if exe then
    exe = exe:lower()
    if exe == "codex" or exe == "codex.js" or exe == "cli.js" then
      return true
    end
  end

  local argv = proc.argv or {}
  for _, arg in ipairs(argv) do
    local lower = tostring(arg):lower()
    if lower:match("(^|/)codex$") or lower:match("/codex%.js$") or lower:match("/cli%.js$") then
      return true
    end
    if lower:find("@openai/codex", 1, true) then
      return true
    end
  end

  local children = proc.children or {}
  for _, child in pairs(children) do
    if proc_info_looks_like_codex(child) then
      return true
    end
  end

  return false
end

local function pane_looks_like_codex(pane)
  if not pane then
    return false
  end

  local proc_info = pane.get_foreground_process_info and pane:get_foreground_process_info() or nil
  if proc_info and proc_info_looks_like_codex(proc_info) then
    return true
  end

  local proc_name = pane.get_foreground_process_name and pane:get_foreground_process_name() or nil
  local exe = basename(proc_name)
  if exe then
    exe = exe:lower()
  end
  if exe == "codex" or exe == "codex.js" or exe == "cli.js" then
    return true
  end

  local title = pane.get_title and pane:get_title() or ""
  if title:lower():find("openai codex", 1, true) then
    return true
  end

  return false
end

local function mux_window_has_codex(mux_window)
  if not mux_window or not mux_window.tabs_with_info then
    return false
  end

  for _, tab_info in ipairs(mux_window:tabs_with_info()) do
    local tab = tab_info.tab
    if tab and tab.panes_with_info then
      for _, pane_info in ipairs(tab:panes_with_info()) do
        if pane_looks_like_codex(pane_info.pane) then
          return true
        end
      end
    end
  end

  return false
end

-- Check whether any local WezTerm pane is running Codex
local function is_codex_running(window, pane)
  if pane_looks_like_codex(pane) then
    return true
  end

  if wezterm.mux and wezterm.mux.all_windows then
    for _, mux_window in ipairs(wezterm.mux.all_windows()) do
      if mux_window_has_codex(mux_window) then
        return true
      end
    end
  end

  if window and window.mux_window then
    return mux_window_has_codex(window:mux_window())
  end

  return false
end

local function fetch_codex_limits(window, pane)
  local now = os.time()

  if not is_codex_running(window, pane) then
    codex_cached = { not_running = true }
    codex_errors = 0
    codex_last_error = nil
    return codex_cached
  end

  if not file_exists(CODEX_SCRIPT) then
    codex_cached = { error = "missing bundled codex helper" }
    codex_errors = codex_errors + 1
    codex_last_error = "missing bundled codex helper"
    return codex_cached
  end

  local shared = read_shared_cache(CODEX_CACHE_PATH)
  sync_codex_shared_state(shared)

  if shared_cache_is_fresh(shared, now) then
    return shared.data
  end

  if not acquire_lock(CODEX_LOCK_DIR) then
    shared = read_shared_cache(CODEX_CACHE_PATH)
    sync_codex_shared_state(shared)
    if shared and shared.data then
      return shared.data
    end
    return codex_cached or { error = codex_last_error or "waiting for shared refresh" }
  end

  local entry
  local locked_cache = read_shared_cache(CODEX_CACHE_PATH)
  sync_codex_shared_state(locked_cache)

  if shared_cache_is_fresh(locked_cache, now) then
    release_lock(CODEX_LOCK_DIR)
    return locked_cache.data
  end

  local previous_data = cacheable_data((locked_cache and locked_cache.data) or codex_cached)
  local previous_errors = tonumber(locked_cache and locked_cache.error_count) or codex_errors or 0

  -- Query Codex rate limits via the helper script
  local success, stdout, stderr = wezterm.run_child_process({ "python3", CODEX_SCRIPT })
  local raw = stdout and stdout:match("^%s*(.-)%s*$") or ""

  if raw == "" then
    local err = (stderr and stderr ~= "") and stderr or "codex helper failed"
    entry = build_cache_entry(previous_data or { error = err }, previous_errors + 1, err, now)
  else
    local ok, data = pcall(wezterm.json_parse, raw)

    if not ok or not data then
      local err = (stderr and stderr ~= "") and stderr or "codex helper parse failed"
      entry = build_cache_entry(previous_data or { error = err }, previous_errors + 1, err, now)
    elseif data.error then
      local err = tostring(data.error)
      entry = build_cache_entry(previous_data or { error = err }, previous_errors + 1, err, now)
    elseif not success then
      local err = (stderr and stderr ~= "") and stderr or "codex helper failed"
      entry = build_cache_entry(previous_data or { error = err }, previous_errors + 1, err, now)
    else
      local primary = data.rateLimits and data.rateLimits.primary
      local secondary = data.rateLimits and data.rateLimits.secondary

      if not primary then
        local err = "no rate limit data"
        entry = build_cache_entry(previous_data or { error = err }, previous_errors + 1, err, now)
      else
        entry = build_cache_entry({
          primary_pct = primary.usedPercent,
          primary_reset = time_until_unix(primary.resetsAt),
          secondary_pct = secondary and secondary.usedPercent or nil,
          secondary_reset = secondary and time_until_unix(secondary.resetsAt) or nil,
          primary_mins = primary.windowDurationMins,
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
  return entry.data
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

  local token = content:match('"claudeAiOauth"%s*:%s*{[^}]*"accessToken"%s*:%s*"([^"]+)"')
  if not token then
    return nil, nil, "no accessToken in credentials"
  end

  local expires_at = content:match('"expiresAt"%s*:%s*(%d+)')
  return token, tonumber(expires_at), nil
end

-- Format time remaining until reset
local function time_until(reset_str)
  if not reset_str then
    return "?"
  end

  -- Parse ISO 8601: 2026-03-08T04:59:59.000000+00:00
  local year, month, day, hour, min, sec =
    reset_str:match("(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)")
  if not year then
    return "?"
  end

  local reset_time = os.time({
    year = tonumber(year),
    month = tonumber(month),
    day = tonumber(day),
    hour = tonumber(hour),
    min = tonumber(min),
    sec = tonumber(sec),
  })

  -- reset_str is UTC, os.time gives local — adjust
  local now_local = os.time()
  local now_utc = os.time(os.date("!*t", now_local))
  local diff = reset_time - now_utc

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

-- Calculate how long to wait before next fetch (exponential backoff on errors)
local function current_interval()
  return interval_for_errors(consecutive_errors)
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

-- Check if Claude Code process is running
local function is_claude_running()
  local ok, stdout = wezterm.run_child_process({ "pgrep", "-x", "claude" })
  return ok and stdout and stdout:match("%d") ~= nil
end

-- Fetch usage data (synchronous curl call cached at the polling interval)
local function fetch_usage()
  local now = os.time()

  -- Always check process state first — never show stale data when Claude is closed
  if not is_claude_running() then
    return { not_running = true }
  end

  local shared = read_shared_cache(CLAUDE_CACHE_PATH)
  sync_claude_shared_state(shared)

  if shared_cache_is_fresh(shared, now) then
    return shared.data
  end

  if not acquire_lock(CLAUDE_LOCK_DIR) then
    shared = read_shared_cache(CLAUDE_CACHE_PATH)
    sync_claude_shared_state(shared)
    if shared and shared.data then
      return shared.data
    end
    return cached_data or { error = last_error or "waiting for shared refresh" }
  end

  local entry
  local locked_cache = read_shared_cache(CLAUDE_CACHE_PATH)
  sync_claude_shared_state(locked_cache)

  if shared_cache_is_fresh(locked_cache, now) then
    release_lock(CLAUDE_LOCK_DIR)
    return locked_cache.data
  end

  local previous_data = cacheable_data((locked_cache and locked_cache.data) or cached_data)
  local previous_errors = tonumber(locked_cache and locked_cache.error_count) or consecutive_errors or 0

  -- Re-read token from disk each fetch — Claude Code may have refreshed it
  local token, expires_at, err = get_token()
  if not token then
    entry = build_cache_entry(previous_data or { error = err }, previous_errors + 1, err, now)
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
      entry = build_cache_entry(previous_data or { error = token_err }, previous_errors + 1, token_err, now)
    else
      local body, status, curl_err = call_usage_api(token)

      if curl_err then
        entry = build_cache_entry(previous_data or { error = curl_err }, previous_errors + 1, curl_err, now)
      elseif status == 429 then
        local next_errors = previous_errors + 1
        local wait = interval_for_errors(next_errors)
        local rate_err = string.format("rate limited (retry in %dm)", math.ceil(wait / 60))
        entry = build_cache_entry(previous_data or { error = rate_err }, next_errors, rate_err, now)
      elseif status == 401 or status == 403 then
        local auth_err = "auth failed — waiting for Claude Code"
        entry = build_cache_entry(previous_data or { error = auth_err }, previous_errors + 1, auth_err, now)
      else
        local ok, data = pcall(wezterm.json_parse, body)
        if not ok or not data then
          local parse_err = "parse failed"
          entry = build_cache_entry(previous_data or { error = parse_err }, previous_errors + 1, parse_err, now)
        elseif data.error then
          local api_err = data.error.message or "api error"
          entry = build_cache_entry(previous_data or { error = api_err }, previous_errors + 1, api_err, now)
        else
          entry = build_cache_entry(data, 0, nil, now)
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
  return entry.data
end

-- Dashboard URL
local DASHBOARD_URL = "https://console.anthropic.com/settings/usage"

-- Build status string using raw ANSI escapes (avoids wezterm.format deserialization issues)
local function build_status_string(data, window, pane)
  -- ── Claude ──────────────────────────────────────────────
  local claude_str
  if data.not_running then
    claude_str = DIM .. " ⚡ " .. BRIGHT .. "Claude: " .. DIM .. "not running"
  elseif data.error then
    claude_str = DIM .. " ⚡ Claude: "
      .. hex_to_fg("#f7768e") .. tostring(data.error)
  else
    local five_pct   = data.five_hour and data.five_hour.utilization or 0
    local five_reset = data.five_hour and data.five_hour.resets_at
    local seven_pct  = data.seven_day and data.seven_day.utilization or 0
    local seven_reset = data.seven_day and data.seven_day.resets_at
    local five_bar   = usage_bar_esc(five_pct)
    local seven_bar  = usage_bar_esc(seven_pct)

    claude_str = DIM .. " ⚡ " .. BRIGHT .. "Claude: "
      .. BRIGHT .. "5h "
    if five_bar then
      claude_str = claude_str .. five_bar .. DIM .. " "
    end
    claude_str = claude_str
      .. usage_color_esc(five_pct) .. string.format("%.0f%%", five_pct)
      .. DIM .. " (" .. time_until(five_reset) .. ")"

    claude_str = claude_str .. DIM .. "  " .. config.icons.week .. " "
      .. BRIGHT .. "7d "
    if seven_bar then
      claude_str = claude_str .. seven_bar .. DIM .. " "
    end
    claude_str = claude_str
      .. usage_color_esc(seven_pct) .. string.format("%.0f%%", seven_pct)
      .. DIM .. " (" .. time_until(seven_reset) .. ")"
  end

  -- ── Codex ───────────────────────────────────────────────
  local codex_str
  local cd = fetch_codex_limits(window, pane)

  if cd.not_running then
    codex_str = DIM .. " ✦ " .. BRIGHT .. "Codex: " .. DIM .. "not running"

  elseif cd.ready then
    codex_str = DIM .. " ✦ " .. BRIGHT .. "Codex: " .. hex_to_fg("#9ece6a") .. "ready"

  elseif cd.error then
    codex_str = DIM .. " ✦ Codex: " .. hex_to_fg("#f7768e") .. tostring(cd.error)

  elseif cd.primary_pct ~= nil then
    -- Full usage data from app-server
    local win_label = cd.primary_mins and string.format("%dh", math.floor(cd.primary_mins / 60)) or "5h"
    local primary_bar = usage_bar_esc(cd.primary_pct)
    local secondary_bar = cd.secondary_pct ~= nil and usage_bar_esc(cd.secondary_pct) or nil
    codex_str = DIM .. " ✦ " .. BRIGHT .. "Codex: "
      .. BRIGHT .. win_label .. " "
    if primary_bar then
      codex_str = codex_str .. primary_bar .. DIM .. " "
    end
    codex_str = codex_str
      .. usage_color_esc(cd.primary_pct) .. string.format("%.0f%%", cd.primary_pct)
    if cd.primary_reset then
      codex_str = codex_str .. DIM .. " (" .. cd.primary_reset .. ")"
    end
    if cd.secondary_pct ~= nil then
      codex_str = codex_str .. DIM .. "  " .. config.icons.week .. " "
        .. BRIGHT .. "7d "
      if secondary_bar then
        codex_str = codex_str .. secondary_bar .. DIM .. " "
      end
      codex_str = codex_str
        .. usage_color_esc(cd.secondary_pct) .. string.format("%.0f%%", cd.secondary_pct)
      if cd.secondary_reset then
        codex_str = codex_str .. DIM .. " (" .. cd.secondary_reset .. ")"
      end
    end

  else
    codex_str = DIM .. " ✦ " .. BRIGHT .. "Codex: " .. hex_to_fg("#e0af68") .. "no data"
  end

  -- ── Join with separator ──────────────────────────────────
  return claude_str
    .. DIM .. "  |" .. codex_str
    .. " " .. RESET
end

-- Build status bar cells (legacy, for wezterm.format)
local function build_cells(data)
  local cells = {}

  if data.error then
    table.insert(cells, dim())
    table.insert(cells, { Text = " " .. config.icons.bolt .. " Claude: " })
    table.insert(cells, { Foreground = { Color = "#f7768e" } })
    table.insert(cells, { Text = tostring(data.error) .. " " })
    return cells
  end

  -- 5-hour window
  local five_pct = data.five_hour and data.five_hour.utilization or 0
  local five_reset = data.five_hour and data.five_hour.resets_at

  -- 7-day window
  local seven_pct = data.seven_day and data.seven_day.utilization or 0
  local seven_reset = data.seven_day and data.seven_day.resets_at

  -- Icon
  table.insert(cells, dim())
  table.insert(cells, { Text = " " .. config.icons.bolt .. " " })

  -- 5h usage
  table.insert(cells, bright())
  table.insert(cells, { Text = "5h " })
  table.insert(cells, usage_color(five_pct))
  table.insert(cells, { Text = string.format("%.0f%%", five_pct) })
  table.insert(cells, dim())
  table.insert(cells, { Text = " (" .. time_until(five_reset) .. ")" })

  -- Separator
  table.insert(cells, dim())
  table.insert(cells, { Text = "  " .. config.icons.week .. " " })

  -- 7d usage
  table.insert(cells, bright())
  table.insert(cells, { Text = "7d " })
  table.insert(cells, usage_color(seven_pct))
  table.insert(cells, { Text = string.format("%.0f%%", seven_pct) })
  table.insert(cells, dim())
  table.insert(cells, { Text = " (" .. time_until(seven_reset) .. ")" })
  table.insert(cells, { Text = " " })

  return cells
end

function M.apply_to_config(c, opts)
  if opts then
    config = deep_merge(config, opts)
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

return M
