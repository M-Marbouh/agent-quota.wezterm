#!/usr/bin/env python3
"""
Fetch Codex rate limits via the documented app-server stdio transport.

This avoids the old VS Code-only localhost WebSocket assumption and talks
directly to `codex app-server --listen stdio://` using JSON-RPC over JSONL.
"""

import asyncio
import json
import os
import re
import shutil
import sys
from glob import glob


REQUEST_TIMEOUT_SECS = 8


def json_error(message):
    print(json.dumps({"error": message}))


def nvm_version_key(path):
    match = re.search(r"/\.nvm/versions/node/v(\d+)\.(\d+)\.(\d+)/bin/codex$", path)
    if not match:
        return (-1, -1, -1)
    return tuple(int(part) for part in match.groups())


def find_codex_executable():
    env_bin = os.environ.get("CODEX_BIN")
    if env_bin and os.path.isfile(env_bin) and os.access(env_bin, os.X_OK):
        return env_bin

    resolved = shutil.which("codex")
    if resolved:
        return resolved

    home = os.environ.get("HOME") or ""
    nvm_matches = glob(os.path.join(home, ".nvm/versions/node/*/bin/codex"))
    if nvm_matches:
        return max(nvm_matches, key=nvm_version_key)

    fallback_paths = [
        os.path.join(home, ".local/bin/codex"),
        os.path.join(home, ".volta/bin/codex"),
        os.path.join(home, ".asdf/shims/codex"),
        os.path.join(home, "bin/codex"),
        "/usr/local/bin/codex",
        "/opt/homebrew/bin/codex",
        "/usr/bin/codex",
    ]
    for path in fallback_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path

    raise FileNotFoundError("codex not found on PATH")


def build_codex_env(codex_bin):
    env = os.environ.copy()
    codex_dir = os.path.dirname(codex_bin)
    existing_path = env.get("PATH", "")
    if existing_path:
        env["PATH"] = codex_dir + os.pathsep + existing_path
    else:
        env["PATH"] = codex_dir
    return env


async def read_message(stream, timeout):
    while True:
        raw = await asyncio.wait_for(stream.readline(), timeout=timeout)
        if not raw:
            raise RuntimeError("app-server closed")
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            # Ignore non-protocol noise and keep reading.
            continue


async def read_response(stream, request_id, timeout):
    while True:
        msg = await read_message(stream, timeout)
        if msg.get("id") == request_id:
            return msg


async def fetch():
    codex_bin = find_codex_executable()
    proc = await asyncio.create_subprocess_exec(
        codex_bin,
        "app-server",
        "--listen",
        "stdio://",
        env=build_codex_env(codex_bin),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        init_request = {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "wezterm-quota-limit",
                    "version": "1.0.0",
                }
            },
        }
        proc.stdin.write((json.dumps(init_request) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        init_response = await read_response(proc.stdout, 1, REQUEST_TIMEOUT_SECS)
        if init_response.get("error"):
            raise RuntimeError(init_response["error"].get("message", "initialize failed"))

        rate_limit_request = {
            "id": 2,
            "method": "account/rateLimits/read",
        }
        proc.stdin.write((json.dumps(rate_limit_request) + "\n").encode("utf-8"))
        await proc.stdin.drain()

        response = await read_response(proc.stdout, 2, REQUEST_TIMEOUT_SECS)
        if response.get("error"):
            raise RuntimeError(response["error"].get("message", "rate limit read failed"))

        print(json.dumps(response.get("result", {})))

    finally:
        if proc.stdin and not proc.stdin.is_closing():
            proc.stdin.close()

        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


def main():
    try:
        asyncio.run(fetch())
    except FileNotFoundError:
        json_error("codex not found on PATH")
        sys.exit(1)
    except asyncio.TimeoutError:
        json_error("timeout")
        sys.exit(1)
    except Exception as exc:
        json_error(str(exc))
        sys.exit(1)


if __name__ == "__main__":
    main()
