#!/usr/bin/env python3
"""Render process-aware Claude, Codex and OpenCode Go quotas for a tmux status line."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


API_URL = "https://api.anthropic.com/api/oauth/usage"
GO_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
GO_USER_AGENT = "agent-quota-tmux/1.0"
GO_AUTH_MAX_BYTES = 1024 * 1024
SUCCESS_TTL = 60
ERROR_TTL = 30
RATE_LIMIT_TTL = 300
NO_PLAN_TTL = 3600
MAX_ERROR_TTL = 1800
NETWORK_TIMEOUT = 5
CODEX_TIMEOUT = 15
KEY_COMMAND_TIMEOUT = 5
BAR_WIDTH = 8
PROVIDERS = ("claude", "codex", "opencode-go")
DEFAULT_PROVIDERS = ("claude", "codex")
PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex", "opencode-go": "OpenCode Go"}
UNICODE_ICONS = {"Claude": "⚡", "Codex": "✦", "OpenCode Go": "◈"}
ASCII_ICONS = {"Claude": "C", "Codex": "X", "OpenCode Go": "O"}
GAUGE_GLYPHS = "▁▂▃▄▅▆▇█"
LAYOUTS = ("full", "compact", "minimal", "auto")
AUTO_ORDER = ("full", "compact", "minimal")


def cache_dir() -> Path:
    configured = os.environ.get("TMUX_QUOTA_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".cache" / "wezterm-quota-limit-tmux"


def private_mkdir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    private_mkdir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


class RefreshLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def __enter__(self) -> bool:
        private_mkdir(self.path.parent)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def process_states(providers: tuple[str, ...] = DEFAULT_PROVIDERS) -> dict[str, bool]:
    requested = [name for name in PROVIDERS if name in providers]
    try:
        result = subprocess.run(
            ["ps", "-eo", "comm=,tty=,args="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {name: False for name in requested}

    states = {name: False for name in requested}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 2:
            continue
        command, tty = fields[0].lower(), fields[1]
        args = fields[2].lower() if len(fields) > 2 else command
        if "claude" in states and command == "claude" and "--chrome-native-host" not in args:
            states["claude"] = True
        if "codex" in states and command == "codex" and tty != "?" and "app-server" not in args:
            states["codex"] = True
        # Pi and OpenCode both run their TUI under their own comm on a pty. Launchers that
        # merely mention them (a CCB bridge running as python3) do not count.
        if "opencode-go" in states and tty != "?" and command in ("pi", "opencode"):
            states["opencode-go"] = True
    return states


def read_claude_token(credentials_path: Path, now: float | None = None) -> tuple[str | None, str | None]:
    try:
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None, "Claude credentials unavailable"

    oauth = credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
    if not isinstance(oauth, dict):
        return None, "Claude credentials unavailable"
    token = oauth.get("accessToken")
    expiry = oauth.get("expiresAt")
    if not isinstance(token, str) or not token or not isinstance(expiry, (int, float)):
        return None, "Claude credentials unavailable"
    if expiry <= (time.time() if now is None else now) * 1000:
        return None, "Claude credentials expired"
    return token, None


def claude_version() -> str:
    candidates = [Path.home() / ".local/bin/claude", Path.home() / ".claude/local/claude"]
    executable = shutil.which("claude") or next((str(p) for p in candidates if p.is_file()), None)
    if not executable:
        return "unknown"
    try:
        result = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=2, check=False
        )
        match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][\w.-]+)?\b", result.stdout)
        return match.group(0) if result.returncode == 0 and match else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def fetch_claude(credentials_path: Path, now: float | None = None) -> dict[str, Any]:
    token, error = read_claude_token(credentials_path, now)
    if not token:
        return {"status": "error", "error": error}
    request = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": f"claude-code/{claude_version()}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        status = "rate_limited" if exc.code == 429 else "error"
        message = "Claude rate limited" if exc.code == 429 else "Claude request failed"
        return {"status": status, "error": message}
    except (OSError, TimeoutError, ValueError, UnicodeError):
        return {"status": "error", "error": "Claude request failed"}
    if not isinstance(payload, dict):
        return {"status": "error", "error": "Claude response unavailable"}

    windows = []
    for key, window in payload.items():
        match = re.fullmatch(r"(five_hour|seven_day)(?:_([a-z0-9_]+))?", key)
        if not match:
            continue
        if not isinstance(window, dict):
            continue
        percent = window.get("utilization")
        if isinstance(percent, (int, float)) and 0 <= percent <= 100:
            label = "5h" if match.group(1) == "five_hour" else "7d"
            if match.group(2):
                label += " " + match.group(2).replace("_", " ")
            windows.append({"label": label, "percent": float(percent), "reset_at": parse_timestamp(window.get("resets_at"))})
    if not windows:
        return {"status": "error", "error": "Claude response unavailable"}
    return {"status": "ok", "windows": windows, "updated_at": time.time() if now is None else now}


def missing_go_key() -> dict[str, Any]:
    return {"status": "no_key", "error": "OpenCode Go key missing"}


def opencode_env_key() -> str | None:
    """The key exported for the tmux server; blank values count as unset."""
    key = os.environ.get("OPENCODE_API_KEY")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return None


def validated_go_key(raw: bytes) -> str | None:
    """One printable line of UTF-8 without surrounding whitespace, or None."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return validated_go_key_text(text)


def validated_go_key_text(text: str) -> str | None:
    """The same rules as validated_go_key, for a key that is already decoded text."""
    if text.endswith("\n"):
        text = text[:-1]
        if text.endswith("\r"):
            text = text[:-1]
    if not text or text != text.strip() or "\n" in text or "\r" in text:
        return None
    if any(not char.isprintable() for char in text):
        return None
    return text


def opencode_key_command_key(command: str) -> str | None:
    """Run an arguments-only key command and return its validated key, or None."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    try:
        completed = subprocess.run(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=KEY_COMMAND_TIMEOUT,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return validated_go_key(completed.stdout or b"")


def go_auth_path() -> Path:
    """OpenCode's saved login file; XDG_DATA_HOME counts only when it is absolute."""
    configured = os.environ.get("XDG_DATA_HOME")
    root = Path(configured) if isinstance(configured, str) and os.path.isabs(configured) else None
    base = root if root is not None else Path.home() / ".local/share"
    return base / "opencode" / "auth.json"


def saved_go_key(path: Path | None = None) -> str | None:
    """The Go API key from an OpenCode API-key login, or None. Fails closed."""
    target = go_auth_path() if path is None else path
    try:
        with target.open("rb") as stream:
            raw = stream.read(GO_AUTH_MAX_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(raw) > GO_AUTH_MAX_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    entry = payload.get("opencode-go")
    if not isinstance(entry, dict) or entry.get("type") != "api":
        return None
    key = entry.get("key")
    if not isinstance(key, str):
        return None
    return validated_go_key_text(key)


def opencode_go_key(command: str | None) -> str | None:
    """Resolve the bearer key: environment, then the command, then a saved login."""
    env_key = opencode_env_key()
    if env_key is not None:
        return env_key
    if command:
        return opencode_key_command_key(command)
    return saved_go_key()


def fetch_opencode_go(now: float | None = None, key_command: str | None = None) -> dict[str, Any]:
    key = opencode_go_key(key_command)
    if key is None:
        return missing_go_key()
    request = urllib.request.Request(
        GO_USAGE_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": GO_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=NETWORK_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            # Only OpenCode's own JSON error envelope means "this workspace has no Go plan".
            # An edge/WAF block (Cloudflare serves a JSON page with title/detail/error_code)
            # is a transport failure, not a plan statement.
            if opencode_error_envelope(exc):
                return {"status": "no_plan"}
            return {"status": "error", "error": "OpenCode Go request failed"}
        if exc.code == 429:
            return {"status": "rate_limited", "error": "OpenCode Go rate limited"}
        return {"status": "error", "error": "OpenCode Go request failed"}
    except (OSError, TimeoutError, ValueError, UnicodeError):
        return {"status": "error", "error": "OpenCode Go request failed"}
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {"status": "error", "error": "OpenCode Go response unavailable"}

    windows = []
    for meter_name, label in (("rolling", "5h"), ("weekly", "7d"), ("monthly", "mo")):
        meter = usage.get(meter_name)
        if not isinstance(meter, dict):
            continue
        meter_status = meter.get("status")
        if meter_status not in ("ok", "rate-limited"):
            continue
        percent = go_percent(meter.get("percent"))
        reset_at = go_reset_at(meter.get("resetsAt"))
        if percent is None or reset_at is None:
            continue
        window: dict[str, Any] = {"label": label, "percent": percent, "reset_at": reset_at}
        if meter_status == "rate-limited":
            window["limited"] = True
        windows.append(window)
    if not windows:
        return {"status": "error", "error": "OpenCode Go response unavailable"}
    return {"status": "ok", "windows": windows, "updated_at": time.time() if now is None else now}


def go_reset_at(value: Any) -> float | None:
    """Return the meter's reset timestamp only for an ISO-8601 string that parses finitely."""
    if not isinstance(value, str):
        return None
    timestamp = parse_timestamp(value)
    if timestamp is None or not math.isfinite(timestamp):
        return None
    return timestamp


def opencode_error_envelope(exc: urllib.error.HTTPError) -> bool:
    """True when a 403 body is OpenCode's JSON error envelope, not an edge/WAF page."""
    try:
        body = exc.read(4096)
    except (OSError, ValueError, TypeError):
        return False
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    # OpenCode wraps failures as {"type": "error", "error": {"type": ..., "message": ...}}.
    # Cloudflare's block page is also JSON, but carries title/detail/error_code and no error object.
    if not isinstance(payload, dict) or payload.get("cloudflare_error") is True:
        return False
    return isinstance(payload.get("error"), dict)


def go_percent(value: Any) -> float | None:
    """Return a finite percentage, rejecting booleans and values outside 0..100."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    percent = float(value)
    return percent if math.isfinite(percent) and 0 <= percent <= 100 else None


def parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def codex_window(value: Any, fallback: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    percent = value.get("usedPercent")
    minutes = value.get("windowDurationMins")
    if not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
        return None
    label = duration_label(minutes) if isinstance(minutes, int) and minutes > 0 else fallback
    return {"label": label, "percent": float(percent), "reset_at": parse_timestamp(value.get("resetsAt"))}


def duration_label(minutes: int) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def fetch_codex(helper: Path) -> dict[str, Any]:
    process = None
    try:
        python = sys.executable or shutil.which("python3") or "python3"
        process = subprocess.Popen(
            [python, str(helper)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True
        )
        stdout, _stderr = process.communicate(timeout=CODEX_TIMEOUT)
    except subprocess.TimeoutExpired:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            if process.poll() is None:
                process.wait()
        return {"status": "error", "error": "Codex request failed"}
    except OSError:
        return {"status": "error", "error": "Codex request failed"}
    finally:
        if process is not None:
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
    if process is None or process.returncode != 0:
        return {"status": "error", "error": "Codex request failed"}
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return {"status": "error", "error": "Codex response unavailable"}
    rate_limits = payload.get("rateLimits") if isinstance(payload, dict) else None
    if not isinstance(rate_limits, dict):
        return {"status": "error", "error": "Codex response unavailable"}
    windows = [w for w in (codex_window(rate_limits.get("primary"), "primary"), codex_window(rate_limits.get("secondary"), "secondary")) if w]
    if not windows:
        return {"status": "error", "error": "Codex response unavailable"}
    return {"status": "ok", "windows": windows, "updated_at": time.time()}


def unexpired_windows(data: dict[str, Any], now: float) -> list[dict[str, Any]]:
    windows = []
    for window in data.get("windows", []):
        if not isinstance(window, dict):
            continue
        reset_at = window.get("reset_at")
        if not isinstance(reset_at, (int, float)) or now < reset_at:
            windows.append(window)
    return windows


def display_entry(entry: dict[str, Any], now: float) -> dict[str, Any]:
    windows = unexpired_windows(entry, now)
    if len(windows) == len(entry.get("windows", [])):
        return entry
    visible = {**entry, "windows": windows}
    if not windows:
        visible["status"] = "syncing"
        visible.pop("stale", None)
        visible.pop("display_state", None)
    return visible


def retry_delay(error_count: int, rate_limited: bool = False) -> int:
    if rate_limited:
        return RATE_LIMIT_TTL
    return min(ERROR_TTL * (2 ** min(max(0, error_count - 1), 6)), MAX_ERROR_TTL)


def prior_error_count(cached: dict[str, Any] | None) -> int:
    value = cached.get("error_count") if cached else None
    return value if isinstance(value, int) and value >= 0 else 0


def provider_data(
    name: str,
    active: bool,
    now: float,
    fetcher: Any,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    if not active:
        return {"status": "not_running", "windows": []}
    root = cache_root or cache_dir()
    private_mkdir(root)
    cache_path = root / f"{name}.json"
    cached = read_json(cache_path)
    if cached and isinstance(cached.get("next_refresh_at"), (int, float)) and now < cached["next_refresh_at"]:
        return display_entry(cached, now)

    with RefreshLock(root / f"{name}.lock") as locked:
        if not locked:
            latest = read_json(cache_path)
            if latest and latest.get("windows"):
                visible = display_entry(latest, now)
                if visible.get("windows"):
                    return {**visible, "display_state": "stale"}
            return {"status": "syncing", "windows": []}
        cached = read_json(cache_path)
        if cached and isinstance(cached.get("next_refresh_at"), (int, float)) and now < cached["next_refresh_at"]:
            return display_entry(cached, now)
        if cached and cached.get("windows"):
            cached = {**cached, "windows": unexpired_windows(cached, now)}
        try:
            result = fetcher()
        except Exception:
            result = {"status": "error", "error": f"{name.title()} request failed"}
        if result.get("status") == "ok":
            windows = unexpired_windows(result, now)
            if windows:
                entry = {**result, "windows": windows, "error_count": 0, "next_refresh_at": now + SUCCESS_TTL}
            else:
                error_count = prior_error_count(cached) + 1
                entry = {
                    "status": "syncing",
                    "error": "waiting for fresh quota data",
                    "windows": [],
                    "error_count": error_count,
                    "next_refresh_at": now + retry_delay(error_count),
                }
        elif result.get("status") == "no_plan":
            # A workspace without a Go subscription replaces previous data outright: no stale
            # meters and no fast retry, because the answer only changes with a new subscription.
            entry = {
                "status": "no_plan",
                "windows": [],
                "error_count": 0,
                "next_refresh_at": now + NO_PLAN_TTL,
            }
        else:
            error_count = prior_error_count(cached) + 1
            retry = retry_delay(error_count, result.get("status") == "rate_limited")
            can_reuse = cached and (
                cached.get("status") == "ok" or cached.get("stale") is True
            )
            previous = cached if can_reuse and cached.get("windows") else None
            if previous:
                entry = {
                    **previous,
                    "status": result.get("status", "error"),
                    "error": result.get("error"),
                    "stale": True,
                    "error_count": error_count,
                    "next_refresh_at": now + retry,
                }
            else:
                entry = {
                    "status": result.get("status", "error"),
                    "error": result.get("error", f"{name.title()} unavailable"),
                    "windows": [],
                    "error_count": error_count,
                    "next_refresh_at": now + retry,
                }
        atomic_json(cache_path, entry)
        return entry


def countdown(reset_at: Any, now: float) -> str:
    if not isinstance(reset_at, (int, float)):
        return ""
    seconds = max(0, int(reset_at - now))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def tmux_text(value: Any) -> str:
    text = str(value)
    text = re.sub(r"[^A-Za-z0-9 _.-]", "", text)
    return text[:48]


def gauge_cell(percent: float) -> str:
    """One-cell usage gauge: eight levels, empty at zero and full at 100%."""
    if percent <= 0:
        return GAUGE_GLYPHS[0]
    level = min(len(GAUGE_GLYPHS), max(1, math.ceil(percent * len(GAUGE_GLYPHS) / 100)))
    return GAUGE_GLYPHS[level - 1]


def visible_width(text: str) -> int:
    """Cells consumed by rendered status text, ignoring tmux style sequences."""
    plain = re.sub(r"#\[[^\]]*\]", "", text)
    return sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in plain)


def threshold_colors(percent: float, limited: bool, use_unicode: bool) -> tuple[str, str]:
    """Usage colour for the gauge and for the percentage text."""
    if use_unicode:
        color = "#9ece6a" if percent < 50 else "#e0af68" if percent < 80 else "#f7768e"
        return color, "#f7768e" if limited else color
    color = "colour40" if percent < 50 else "colour220" if percent < 80 else "colour196"
    return color, "colour196" if limited else color


def layout_colors(use_unicode: bool) -> dict[str, str]:
    """Dim, state, label, countdown and stale colours shared by the dense layouts."""
    if use_unicode:
        return {
            "dim": "#565f89",
            "state": "#c0caf5",
            "label": "#c0caf5",
            "timer": "#a9b1d6",
            "stale": "#e0af68",
        }
    return {
        "dim": "colour240",
        "state": "colour245",
        "label": "colour252",
        "timer": "colour245",
        "stale": "colour214",
    }


def numeric_windows(data: dict[str, Any]) -> list[dict[str, Any]]:
    windows = data.get("windows") if isinstance(data.get("windows"), list) else []
    return [window for window in windows if isinstance(window.get("percent"), (int, float))]


def quota_bar(percent: float, use_unicode: bool = False) -> str:
    filled = min(BAR_WIDTH, max(0, round(percent * BAR_WIDTH / 100)))
    if use_unicode:
        return "█" * filled + "░" * (BAR_WIDTH - filled)
    return "[" + ("#" * filled) + ("-" * (BAR_WIDTH - filled)) + "]"


def styled_provider(
    name: str, data: dict[str, Any], now: float, use_unicode: bool = False
) -> str:
    if use_unicode:
        icon = UNICODE_ICONS.get(name, "✦")
        prefix = f"#[fg=#c0caf5]{icon} {name}:#[default]"
        state_color = "#c0caf5"
    else:
        icon = ASCII_ICONS.get(name, "?")
        prefix = f"#[fg=colour245]{icon} {name}:#[default]"
        state_color = "colour245"
    status = data.get("status")
    if status == "not_running":
        return prefix + f" #[fg={state_color}]not running#[default]"
    if status == "syncing":
        return prefix + f" #[fg={state_color}]syncing#[default]"
    if status == "no_plan":
        return prefix + f" #[fg={state_color}]no Go plan#[default]"
    if status == "no_key":
        return prefix + f" #[fg={state_color}]no key#[default]"
    windows = data.get("windows") if isinstance(data.get("windows"), list) else []
    if not windows:
        state = "syncing" if status == "rate_limited" else "unavailable"
        return prefix + f" #[fg={state_color}]{state}#[default]"
    rendered = []
    if data.get("stale") or data.get("display_state") == "stale":
        stale_color = "#e0af68" if use_unicode else "colour214"
        rendered.append(f"#[fg={stale_color}]stale#[default]")
    for window in windows:
        label = tmux_text(window.get("label", "usage")) or "usage"
        percent = window.get("percent")
        if not isinstance(percent, (int, float)):
            continue
        limited = window.get("limited") is True
        marker = " limited" if limited else ""
        color, percent_color = threshold_colors(percent, limited, use_unicode)
        bar = quota_bar(float(percent), use_unicode)
        remaining = countdown(window.get("reset_at"), now)
        if use_unicode:
            filled = bar.count("█")
            bar = f"#[fg={color}]{bar[:filled]}#[fg=#565f89]{bar[filled:]}#[default]"
            timer = f" #[fg=#a9b1d6]({remaining})#[default]" if remaining else ""
            rendered.append(
                f"#[fg=#c0caf5]{label}#[default] {bar} "
                f"#[fg={percent_color}]{percent:.0f}%{marker}#[default]{timer}"
            )
        else:
            suffix = f" ({remaining})" if remaining else ""
            rendered.append(f"#[fg=colour252]{label} {bar} #[fg={percent_color}]{percent:.0f}%{marker}{suffix}#[default]")
    separator = "  #[fg=#7a83a5]·#[default]  " if use_unicode else "  "
    unavailable_color = "#c0caf5" if use_unicode else "colour245"
    return prefix + (" " + separator.join(rendered) if rendered else f" #[fg={unavailable_color}]unavailable#[default]")


def state_marker(data: dict[str, Any], use_unicode: bool) -> str | None:
    """Dense-layout state text for a provider, or None when windows are drawn."""
    status = data.get("status")
    waiting = "\u2026" if use_unicode else "..."
    if status == "not_running":
        return ""
    if status == "syncing":
        return waiting
    if status == "no_plan":
        return "no plan"
    if status == "no_key":
        return "no key"
    if not numeric_windows(data):
        return waiting if status == "rate_limited" else "?"
    return None


def dense_provider_head(
    name: str, data: dict[str, Any], use_unicode: bool, colors: dict[str, str]
) -> tuple[str, str | None]:
    """Icon and provider name, plus the state suffix when no usage windows are drawn."""
    icon = (UNICODE_ICONS if use_unicode else ASCII_ICONS).get(name, "\u2726" if use_unicode else "?")
    prefix = f"{icon} {name}:"
    marker = state_marker(data, use_unicode)
    if marker == "":
        return f"#[fg={colors['dim']}]{prefix}#[default]", ""
    head = f"#[fg={colors['state']}]{prefix}#[default]"
    if data.get("stale") or data.get("display_state") == "stale":
        head += f"#[fg={colors['stale']}]~#[default]"
    if marker is None:
        return head, None
    return head, f" #[fg={colors['state']}]{marker}#[default]"


def is_short_window(label: str) -> bool:
    """True for a short rolling window, judged by the label's duration token."""
    first = label.split()[0] if label.split() else ""
    return re.fullmatch(r"\d+[mh]", first) is not None


def compact_window(
    window: dict[str, Any], now: float, use_unicode: bool, colors: dict[str, str]
) -> str:
    label = tmux_text(window.get("label", "usage")) or "usage"
    percent = float(window["percent"])
    limited = window.get("limited") is True
    color, percent_color = threshold_colors(percent, limited, use_unicode)
    marker = "!" if limited else ""
    short = is_short_window(label)
    parts = [f"#[fg={colors['label']}]{label}#[default]"]
    if short and use_unicode:
        gauge_color = colors["dim"] if percent <= 0 else color
        parts.append(f"#[fg={gauge_color}]{gauge_cell(percent)}#[default]")
    parts.append(f"#[fg={percent_color}]{percent:.0f}%{marker}#[default]")
    remaining = countdown(window.get("reset_at"), now)
    if remaining and (short or limited or percent >= 80):
        parts.append(f"#[fg={colors['timer']}]{remaining}#[default]")
    return " ".join(parts)


def compact_provider(name: str, data: dict[str, Any], now: float, use_unicode: bool) -> str:
    colors = layout_colors(use_unicode)
    head, state = dense_provider_head(name, data, use_unicode, colors)
    if state is not None:
        return head + state
    join = " #[fg=#7a83a5]\u00b7#[default] " if use_unicode else " / "
    windows = numeric_windows(data)
    rendered = [compact_window(window, now, use_unicode, colors) for window in windows]
    return head + " " + join.join(rendered)


def minimal_provider(name: str, data: dict[str, Any], use_unicode: bool) -> str:
    colors = layout_colors(use_unicode)
    head, state = dense_provider_head(name, data, use_unicode, colors)
    if state is not None:
        return head + state
    windows = numeric_windows(data)
    highest = max(float(window["percent"]) for window in windows)
    is_limited = any(window.get("limited") is True for window in windows)
    _, percent_color = threshold_colors(highest, is_limited, use_unicode)
    bang = "!" if is_limited else ""
    return f"{head} #[fg={percent_color}]{highest:.0f}%{bang}#[default]"


def layout_separator(layout: str, use_unicode: bool) -> str:
    if layout == "full":
        return "  #[fg=#7a83a5]\u00b7#[default]  " if use_unicode else "  #[fg=colour238]|#[default]  "
    if use_unicode:
        return "  #[fg=#7a83a5]\u2502#[default]  "
    return "  #[fg=colour238]|#[default]  "


def render_layout(
    layout: str,
    states: dict[str, bool],
    data: dict[str, dict[str, Any]],
    selected: list[str],
    now: float,
    use_unicode: bool,
) -> str:
    rendered = []
    for name in selected:
        entry = data[name] if states.get(name) else {"status": "not_running"}
        if layout == "minimal":
            rendered.append(minimal_provider(PROVIDER_NAMES[name], entry, use_unicode))
        elif layout == "compact":
            rendered.append(compact_provider(PROVIDER_NAMES[name], entry, now, use_unicode))
        else:
            rendered.append(styled_provider(PROVIDER_NAMES[name], entry, now, use_unicode))
    return layout_separator(layout, use_unicode).join(rendered)


def render_status(
    states: dict[str, bool],
    claude: dict[str, Any],
    codex: dict[str, Any],
    now: float | None = None,
    use_unicode: bool = False,
    providers: tuple[str, ...] = DEFAULT_PROVIDERS,
    opencode_go: dict[str, Any] | None = None,
    layout: str = "full",
    max_width: int | None = None,
    hide_inactive: bool = False,
) -> str:
    current = time.time() if now is None else now
    data = {"claude": claude, "codex": codex, "opencode-go": opencode_go or {}}
    selected = [name for name in PROVIDERS if name in providers]
    if hide_inactive:
        # A provider whose CLI is not running disappears before auto measures anything.
        selected = [name for name in selected if states.get(name)]
    if layout not in LAYOUTS:
        layout = "full"
    if layout == "auto":
        if isinstance(max_width, int):
            for candidate in AUTO_ORDER:
                text = render_layout(candidate, states, data, selected, current, use_unicode)
                if visible_width(text) <= max_width:
                    return text
            return render_layout("minimal", states, data, selected, current, use_unicode)
        layout = "compact"
    return render_layout(layout, states, data, selected, current, use_unicode)


def collect_status(
    providers: tuple[str, ...], key_command: str | None, now: float
) -> tuple[tuple[str, ...], dict[str, bool], dict[str, dict[str, Any]]]:
    """Fetch every selected provider once: canonical order, process states, raw entries."""
    selected = tuple(name for name in PROVIDERS if name in providers)
    states = process_states(selected)
    root = cache_dir()
    helper = Path(__file__).with_name("codex-limits.py")
    data: dict[str, dict[str, Any]] = {}
    for name in selected:
        active = states.get(name, False)
        if name == "claude":
            data[name] = provider_data(
                "claude", active, now,
                lambda: fetch_claude(Path.home() / ".claude/.credentials.json", now), root,
            )
        elif name == "codex":
            data[name] = provider_data("codex", active, now, lambda: fetch_codex(helper), root)
        else:
            fetcher = (
                (lambda: fetch_opencode_go(now, key_command)) if key_command
                else (lambda: fetch_opencode_go(now))
            )
            data[name] = provider_data("opencode-go", active, now, fetcher, root)
    return selected, states, data


def run_status(
    use_unicode: bool = False,
    providers: tuple[str, ...] = DEFAULT_PROVIDERS,
    layout: str = "full",
    max_width: int | None = None,
    key_command: str | None = None,
    hide_inactive: bool = False,
) -> str:
    now = time.time()
    selected, states, data = collect_status(providers, key_command, now)
    return render_status(
        states,
        data.get("claude", {}),
        data.get("codex", {}),
        now,
        use_unicode,
        selected,
        data.get("opencode-go"),
        layout,
        max_width,
        hide_inactive,
    )


JSON_STATES = (
    "ok", "not_running", "syncing", "no_plan", "no_key", "rate_limited", "unavailable", "error",
)


def json_number(value: Any) -> float | None:
    """A finite JSON number for a real numeric value, rejecting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def json_windows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The windows a layout would draw, as plain JSON numbers."""
    windows = data.get("windows") if isinstance(data.get("windows"), list) else []
    report = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        percent = json_number(window.get("percent"))
        if percent is None:
            continue
        report.append({
            "label": tmux_text(window.get("label", "usage")) or "usage",
            "percent": percent,
            "reset_at": json_number(window.get("reset_at")),
            "limited": window.get("limited") is True,
        })
    return report


def json_provider(name: str, data: dict[str, Any]) -> dict[str, Any]:
    status = data.get("status")
    return {
        "name": name,
        "state": status if status in JSON_STATES else "unavailable",
        "stale": data.get("stale") is True or data.get("display_state") == "stale",
        "windows": json_windows(data),
    }


def error_report(selected: tuple[str, ...]) -> dict[str, Any]:
    """The documented shape for a run that could not produce any data."""
    return {
        "version": 1,
        "providers": [
            {"name": name, "state": "error", "stale": False, "windows": []}
            for name in selected
        ],
    }


def json_report(providers: tuple[str, ...], key_command: str | None) -> dict[str, Any]:
    """One machine-readable snapshot with the same fetches, cache and backoff."""
    selected = tuple(name for name in PROVIDERS if name in providers)
    try:
        selected, _states, data = collect_status(providers, key_command, time.time())
        return {
            "version": 1,
            "providers": [json_provider(name, data.get(name, {})) for name in selected],
        }
    except Exception:
        return error_report(selected)


def parse_providers(value: str) -> tuple[str, ...]:
    names = [part.strip() for part in value.split(",") if part.strip()]
    if not names:
        raise ValueError("--providers needs at least one of: " + ", ".join(PROVIDERS))
    unknown = [name for name in names if name not in PROVIDERS]
    if unknown:
        raise ValueError(
            "unknown provider(s): " + ", ".join(unknown) + "; choose from: " + ", ".join(PROVIDERS)
        )
    return tuple(name for name in PROVIDERS if name in names)


def tmux_command(
    path: Path,
    use_unicode: bool = False,
    providers: tuple[str, ...] = DEFAULT_PROVIDERS,
    layout: str = "full",
    max_width: int | None = None,
    key_command: str | None = None,
    hide_inactive: bool = False,
) -> str:
    flags = []
    if use_unicode:
        flags.append("--unicode")
    if layout and layout != "full":
        flags.append("--layout " + shlex.quote(layout))
    if max_width is not None:
        flags.append("--max-width " + shlex.quote(str(int(max_width))))
    selected = tuple(name for name in PROVIDERS if name in providers)
    if selected and selected != DEFAULT_PROVIDERS:
        flags.append("--providers " + shlex.quote(",".join(selected)))
    if key_command:
        flags.append("--opencode-key-command " + shlex.quote(key_command))
    if hide_inactive:
        flags.append("--hide-inactive")
    return (
        "#(python3 "
        + shlex.quote(str(path.resolve()))
        + "".join(" " + flag for flag in flags)
        + ")"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmux-command", action="store_true", help="print a status-right command for this file")
    parser.add_argument("--unicode", action="store_true", help="render tmux-native Unicode icons and usage bars")
    parser.add_argument(
        "--providers",
        default=",".join(DEFAULT_PROVIDERS),
        help="comma-separated providers to show: " + ", ".join(PROVIDERS)
        + " (default: " + ",".join(DEFAULT_PROVIDERS) + ")",
    )
    parser.add_argument(
        "--layout",
        choices=LAYOUTS,
        default="full",
        help="render density: full (default), compact, minimal, or auto by --max-width",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=None,
        metavar="N",
        help="cells available for the quota text; selects the layout for --layout auto",
    )
    parser.add_argument(
        "--opencode-key-command",
        default=None,
        metavar="CMD",
        help="command whose first output line is the OpenCode Go key; a non-blank "
        "OPENCODE_API_KEY wins over it, it runs only when no environment key is usable, "
        "and a failing command never falls back to a saved OpenCode login",
    )
    parser.add_argument(
        "--hide-inactive",
        action="store_true",
        help="hide providers whose CLI is not running; this detects running tools in a terminal, "
        "not signed-in accounts or the model a session is using",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print one machine-readable JSON object instead of the tmux status line; "
        "--layout and --hide-inactive do not apply to it",
    )
    args = parser.parse_args()
    try:
        selected = parse_providers(args.providers)
    except ValueError as error:
        parser.error(str(error))
    if args.json:
        # A consumer parses this output, so a runtime failure still prints the documented
        # shape instead of falling through to the tmux status text.
        try:
            report = json_report(selected, args.opencode_key_command)
            text = json.dumps(report, separators=(",", ":"), allow_nan=False)
        except Exception:
            text = json.dumps(error_report(selected), separators=(",", ":"))
        print(text)
        return 0
    try:
        if args.tmux_command:
            print(tmux_command(
                Path(__file__), args.unicode, selected, args.layout, args.max_width,
                args.opencode_key_command, args.hide_inactive,
            ))
        elif (
            selected == DEFAULT_PROVIDERS
            and args.layout == "full"
            and args.max_width is None
            and not args.opencode_key_command
            and not args.hide_inactive
        ):
            # The default path keeps its original single-argument call.
            print(run_status(args.unicode))
        else:
            print(run_status(
                args.unicode, selected, args.layout, args.max_width, args.opencode_key_command,
                args.hide_inactive,
            ))
    except Exception:
        print("#[fg=colour245]Quota unavailable#[default]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
