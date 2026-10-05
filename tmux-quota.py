#!/usr/bin/env python3
"""Render process-aware Claude and Codex quotas for a tmux status line."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


API_URL = "https://api.anthropic.com/api/oauth/usage"
SUCCESS_TTL = 60
ERROR_TTL = 30
RATE_LIMIT_TTL = 300
MAX_ERROR_TTL = 1800
NETWORK_TIMEOUT = 5
CODEX_TIMEOUT = 15
BAR_WIDTH = 8


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


def process_states() -> dict[str, bool]:
    try:
        result = subprocess.run(
            ["ps", "-eo", "comm=,tty=,args="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"claude": False, "codex": False}

    states = {"claude": False, "codex": False}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 2:
            continue
        command, tty = fields[0].lower(), fields[1]
        args = fields[2].lower() if len(fields) > 2 else command
        if command == "claude" and "--chrome-native-host" not in args:
            states["claude"] = True
        if command == "codex" and tty != "?" and "app-server" not in args:
            states["codex"] = True
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


def quota_bar(percent: float) -> str:
    filled = min(BAR_WIDTH, max(0, round(percent * BAR_WIDTH / 100)))
    return "[" + ("#" * filled) + ("-" * (BAR_WIDTH - filled)) + "]"


def styled_provider(name: str, data: dict[str, Any], now: float) -> str:
    icon = "C" if name == "Claude" else "X"
    prefix = f"#[fg=colour245]{icon} {name}:#[default]"
    status = data.get("status")
    if status == "not_running":
        return prefix + " #[fg=colour245]not running#[default]"
    if status == "syncing":
        return prefix + " #[fg=colour245]syncing#[default]"
    windows = data.get("windows") if isinstance(data.get("windows"), list) else []
    if not windows:
        state = "syncing" if status == "rate_limited" else "unavailable"
        return prefix + f" #[fg=colour245]{state}#[default]"
    rendered = []
    if data.get("stale") or data.get("display_state") == "stale":
        rendered.append("#[fg=colour214]stale#[default]")
    for window in windows:
        label = tmux_text(window.get("label", "usage")) or "usage"
        percent = window.get("percent")
        if not isinstance(percent, (int, float)):
            continue
        color = "colour40" if percent < 50 else "colour220" if percent < 80 else "colour196"
        bar = quota_bar(float(percent))
        remaining = countdown(window.get("reset_at"), now)
        suffix = f" ({remaining})" if remaining else ""
        rendered.append(f"#[fg=colour252]{label} {bar} #[fg={color}]{percent:.0f}%{suffix}#[default]")
    return prefix + (" " + "  ".join(rendered) if rendered else " #[fg=colour245]unavailable#[default]")


def render_status(states: dict[str, bool], claude: dict[str, Any], codex: dict[str, Any], now: float | None = None) -> str:
    current = time.time() if now is None else now
    return styled_provider("Claude", claude if states.get("claude") else {"status": "not_running"}, current) + "  #[fg=colour238]|#[default]  " + styled_provider("Codex", codex if states.get("codex") else {"status": "not_running"}, current)


def run_status() -> str:
    now = time.time()
    states = process_states()
    root = cache_dir()
    helper = Path(__file__).with_name("codex-limits.py")
    claude = provider_data("claude", states["claude"], now, lambda: fetch_claude(Path.home() / ".claude/.credentials.json", now), root)
    codex = provider_data("codex", states["codex"], now, lambda: fetch_codex(helper), root)
    return render_status(states, claude, codex, now)


def tmux_command(path: Path) -> str:
    return "#(python3 " + shlex.quote(str(path.resolve())) + ")"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmux-command", action="store_true", help="print a status-right command for this file")
    args = parser.parse_args()
    try:
        print(tmux_command(Path(__file__)) if args.tmux_command else run_status())
    except Exception:
        print("#[fg=colour245]Quota unavailable#[default]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
