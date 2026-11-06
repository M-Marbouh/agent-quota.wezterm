import contextlib
import importlib.util
import itertools
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

try:
    import fcntl
    import pty
    import termios
except ImportError:
    fcntl = pty = termios = None


SOURCE = Path(__file__).resolve().parents[1] / "tmux-quota.py"
SPEC = importlib.util.spec_from_file_location("tmux_quota", SOURCE)
quota = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(quota)


class CredentialTests(unittest.TestCase):
    def test_scopes_oauth_fields_and_rejects_bad_or_expired_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"expiresAt": 0, "claudeAiOauth": {"accessToken": "fixture-token", "expiresAt": 200000}}))
            self.assertEqual(quota.read_claude_token(path, now=100)[0], "fixture-token")
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fixture-token", "expiresAt": 100000}}))
            self.assertEqual(quota.read_claude_token(path, now=100)[0], None)
            path.write_text("not json")
            self.assertEqual(quota.read_claude_token(path, now=1)[0], None)
            path.unlink()
            self.assertEqual(quota.read_claude_token(path, now=1)[0], None)

    def test_invalid_or_expired_credentials_do_not_call_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            for value in ({}, {"claudeAiOauth": {"accessToken": "fixture-token", "expiresAt": 1}}):
                path.write_text(json.dumps(value))
                with mock.patch.object(quota.urllib.request, "urlopen") as urlopen:
                    quota.fetch_claude(path, now=2)
                    urlopen.assert_not_called()

    def test_zero_usage_and_missing_window_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "token", "expiresAt": 9999999999999}}))
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "five_hour": {"utilization": 0, "resets_at": "2026-10-05T15:00:00Z"},
                "seven_day_sonnet": {"utilization": 25, "resets_at": "2026-10-10T15:00:00Z"},
            }).encode()
            with mock.patch.object(quota, "claude_version", return_value="1.0.0"), mock.patch.object(
                quota.urllib.request, "urlopen", return_value=response
            ):
                result = quota.fetch_claude(path, now=100)
            self.assertEqual(result["windows"][0]["percent"], 0)
            self.assertEqual([w["label"] for w in result["windows"]], ["5h", "7d sonnet"])

    def test_http_auth_rate_limit_and_timeout_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "fixture-token", "expiresAt": 9999999999999}}))
            auth = urllib.error.HTTPError("https://example.invalid", 401, "private body", {}, None)
            limited = urllib.error.HTTPError("https://example.invalid", 429, "private body", {}, None)
            with mock.patch.object(quota, "claude_version", return_value="fixture"), mock.patch.object(
                quota.urllib.request, "urlopen", side_effect=auth
            ):
                auth_result = quota.fetch_claude(path, now=100)
            with mock.patch.object(quota, "claude_version", return_value="fixture"), mock.patch.object(
                quota.urllib.request, "urlopen", side_effect=limited
            ):
                limited_result = quota.fetch_claude(path, now=100)
            with mock.patch.object(quota, "claude_version", return_value="fixture"), mock.patch.object(
                quota.urllib.request, "urlopen", side_effect=TimeoutError()
            ):
                timeout_result = quota.fetch_claude(path, now=100)
        self.assertEqual(auth_result["error"], "Claude request failed")
        self.assertEqual(limited_result["status"], "rate_limited")
        self.assertEqual(timeout_result["error"], "Claude request failed")
        self.assertNotIn("fixture-token", repr((auth_result, limited_result, timeout_result)))


class ProviderTests(unittest.TestCase):
    def test_codex_windows_keep_zero_and_actual_duration(self):
        payload = {"rateLimits": {"primary": {"usedPercent": 0, "windowDurationMins": 300, "resetsAt": 2000},
                                  "secondary": {"usedPercent": 42, "windowDurationMins": 10080, "resetsAt": 3000}}}
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "helper.py"
            helper.write_text("print(" + repr(json.dumps(payload)) + ")\n")
            data = quota.fetch_codex(helper)
        self.assertEqual([(w["label"], w["percent"]) for w in data["windows"]], [("5h", 0), ("7d", 42)])

    def test_codex_missing_primary_keeps_a_returned_secondary_window(self):
        payload = {"rateLimits": {"secondary": {"usedPercent": 25, "windowDurationMins": 1440, "resetsAt": 3000}}}
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "helper.py"
            helper.write_text("print(" + repr(json.dumps(payload)) + ")\n")
            data = quota.fetch_codex(helper)
        self.assertEqual(data["windows"], [{"label": "1d", "percent": 25.0, "reset_at": 3000.0}])

    def test_existing_codex_helper_output_contract_is_consumed(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "codex"
            fake.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "for line in sys.stdin:\n"
                " request = json.loads(line)\n"
                " if request.get('method') == 'initialize':\n"
                "  print(json.dumps({'id': request['id'], 'result': {}}), flush=True)\n"
                " elif request.get('method') == 'account/rateLimits/read':\n"
                "  result = {'rateLimits': {'primary': {'usedPercent': 0, 'windowDurationMins': 300, 'resetsAt': 2000}}}\n"
                "  print(json.dumps({'id': request['id'], 'result': result}), flush=True)\n"
            )
            fake.chmod(0o700)
            with mock.patch.dict(os.environ, {"CODEX_BIN": str(fake)}):
                data = quota.fetch_codex(SOURCE.with_name("codex-limits.py"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["windows"], [{"label": "5h", "percent": 0.0, "reset_at": 2000.0}])

    def test_providers_fail_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claude = quota.provider_data("claude", True, 100, lambda: {"status": "error", "error": "safe"}, root)
            codex = quota.provider_data("codex", True, 100, lambda: {"status": "ok", "windows": []}, root)
        self.assertEqual(claude["status"], "error")
        self.assertEqual(codex["status"], "syncing")

    def test_auth_error_is_sanitized_and_backed_off(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def failed():
                calls.append(1)
                return {"status": "error", "error": "Claude request failed"}
            first = quota.provider_data("claude", True, 100, failed, root)
            second = quota.provider_data("claude", True, 110, failed, root)
        self.assertEqual(first["error"], "Claude request failed")
        self.assertEqual(second["next_refresh_at"], first["next_refresh_at"])
        self.assertEqual(len(calls), 1)

    def test_rate_limit_persists_retry_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def limited():
                calls.append(1)
                return {"status": "rate_limited", "error": "Claude rate limited"}
            first = quota.provider_data("claude", True, 100, limited, root)
            disk = quota.read_json(root / "claude.json")
            second = quota.provider_data("claude", True, 120, limited, root)
        self.assertEqual(disk["next_refresh_at"], 100 + quota.RATE_LIMIT_TTL)
        self.assertEqual(second["next_refresh_at"], disk["next_refresh_at"])
        self.assertEqual(len(calls), 1)

    def test_cache_files_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "quota-cache"
            quota.provider_data("claude", True, 100, lambda: {"status": "ok", "windows": []}, root)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "claude.json").stat().st_mode & 0o777, 0o600)

    def test_cache_reuse_and_reset_crossing_respects_retry_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            good = lambda: (calls.append("good") or {"status": "ok", "windows": [{"label": "5h", "percent": 20, "reset_at": 180}]})
            stale = lambda: (calls.append("bad") or {"status": "rate_limited", "error": "safe"})
            one = quota.provider_data("claude", True, 100, good, root)
            two = quota.provider_data("claude", True, 161, stale, root)
            self.assertEqual(two["status"], "rate_limited")
            crossed = quota.provider_data("claude", True, 180, good, root)
            self.assertEqual(crossed["windows"], [])
            self.assertEqual(crossed["status"], "syncing")
            self.assertEqual(crossed["next_refresh_at"], 161 + quota.RATE_LIMIT_TTL)
            waiting = quota.provider_data("claude", True, 460, good, root)
            self.assertEqual(waiting["windows"], [])
            recovered = quota.provider_data("claude", True, 461, lambda: (calls.append("good") or {"status": "ok", "windows": [{"label": "5h", "percent": 20, "reset_at": 900}]}), root)
        self.assertEqual(calls, ["good", "bad", "good"])
        self.assertEqual(recovered["windows"][0]["percent"], 20)
        self.assertEqual(one["windows"][0]["reset_at"], 180)

    def test_transient_backoff_survives_reset_crossing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def initial():
                calls.append("initial")
                return {"status": "ok", "windows": [{"label": "5h", "percent": 80, "reset_at": 180}]}
            def failed():
                calls.append("failed")
                return {"status": "error", "error": "safe"}
            quota.provider_data("claude", True, 100, initial, root)
            quota.provider_data("claude", True, 161, failed, root)
            expired = quota.provider_data("claude", True, 180, failed, root)
            waiting = quota.provider_data("claude", True, 190, failed, root)
            recovered = quota.provider_data("claude", True, 191, lambda: {"status": "ok", "windows": [{"label": "5h", "percent": 10, "reset_at": 900}]}, root)
        self.assertEqual(calls, ["initial", "failed"])
        self.assertEqual(expired["windows"], [])
        self.assertEqual(expired["status"], "syncing")
        self.assertEqual(recovered["windows"][0]["percent"], 10)

    def test_success_payload_with_past_reset_syncs_and_throttles_until_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def lagging():
                calls.append("lagging")
                return {"status": "ok", "windows": [{"label": "5h", "percent": 80, "reset_at": 180}]}
            def fresh():
                calls.append("fresh")
                return {"status": "ok", "windows": [{"label": "5h", "percent": 10, "reset_at": 900}]}
            first = quota.provider_data("claude", True, 181, lagging, root)
            second = quota.provider_data("claude", True, 182, lagging, root)
            self.assertEqual(first["status"], "syncing")
            self.assertEqual(second["status"], "syncing")
            self.assertEqual(second["windows"], [])
            recovered = quota.provider_data("claude", True, first["next_refresh_at"], fresh, root)
        self.assertEqual(calls, ["lagging", "fresh"])
        self.assertEqual(recovered["windows"][0]["percent"], 10)

    def test_transient_backoff_escalates_and_reset_does_not_trigger_early_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            def fetch():
                calls.append(1)
                return {"status": "error", "error": "safe"}
            first = quota.provider_data("claude", True, 100, fetch, root)
            second = quota.provider_data("claude", True, 130, fetch, root)
            third = quota.provider_data("claude", True, 190, fetch, root)
            quota.provider_data("claude", True, 300, fetch, root)
        self.assertEqual(first["next_refresh_at"], 130)
        self.assertEqual(second["next_refresh_at"], 190)
        self.assertEqual(third["next_refresh_at"], 310)
        self.assertEqual(len(calls), 3)
        self.assertEqual(quota.retry_delay(99), quota.MAX_ERROR_TTL)

    def test_failed_refresh_uses_explicit_stale_state_then_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota.provider_data("codex", True, 100, lambda: {"status": "ok", "windows": [{"label": "5h", "percent": 20, "reset_at": 500}]}, root)
            stale = quota.provider_data("codex", True, 161, lambda: {"status": "error", "error": "safe"}, root)
            recovered = quota.provider_data("codex", True, 192, lambda: {"status": "ok", "windows": [{"label": "5h", "percent": 30, "reset_at": 500}]}, root)
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["status"], "error")
        self.assertFalse(recovered.get("stale", False))

    def test_reset_data_is_not_rendered_as_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota.provider_data("claude", True, 100, lambda: {"status": "ok", "windows": [{"label": "5h", "percent": 80, "reset_at": 150}]}, root)
            result = quota.provider_data("claude", True, 150, lambda: {"status": "error", "error": "safe"}, root)
        self.assertEqual(result["windows"], [])
        self.assertEqual(result["status"], "syncing")

    def test_expired_window_is_dropped_while_safe_sibling_remains_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota.provider_data(
                "claude", True, 100,
                lambda: {"status": "ok", "windows": [
                    {"label": "5h", "percent": 80, "reset_at": 150},
                    {"label": "7d", "percent": 25, "reset_at": 500},
                ]},
                root,
            )
            result = quota.provider_data("claude", True, 161, lambda: {"status": "error", "error": "safe"}, root)
        self.assertTrue(result["stale"])
        self.assertEqual([window["label"] for window in result["windows"]], ["7d"])

    def test_stale_data_is_removed_when_reset_crosses_during_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quota.provider_data("claude", True, 100, lambda: {"status": "ok", "windows": [{"label": "5h", "percent": 80, "reset_at": 200}]}, root)
            stale = quota.provider_data("claude", True, 161, lambda: {"status": "error", "error": "safe"}, root)
            self.assertTrue(stale["stale"])
            crossed = quota.provider_data("claude", True, 200, lambda: {"status": "error", "error": "safe"}, root)
        self.assertEqual(crossed["windows"], [])
        self.assertEqual(crossed["status"], "error")

    def test_concurrent_refresh_runs_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            started = threading.Event()
            def fetch():
                calls.append(1)
                started.set()
                time.sleep(0.15)
                return {"status": "ok", "windows": []}
            first = threading.Thread(target=quota.provider_data, args=("claude", True, 100, fetch, root))
            first.start()
            self.assertTrue(started.wait(1))
            second = quota.provider_data("claude", True, 100, fetch, root)
            first.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["status"], "syncing")

    def test_process_lock_excludes_other_process_and_recovers_after_owner_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.lock"
            ready = Path(directory) / "ready"
            owner = Path(directory) / "owner.py"
            owner.write_text(
                "import importlib.util, time\nfrom pathlib import Path\n"
                f"spec = importlib.util.spec_from_file_location('quota_lock_test', {str(SOURCE)!r})\n"
                "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
                f"with module.RefreshLock(Path({str(path)!r})) as locked:\n"
                f" Path({str(ready)!r}).write_text(str(locked))\n time.sleep(60)\n"
            )
            first = subprocess.Popen(["python3", str(owner)])
            try:
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(ready.read_text(), "True")
                with quota.RefreshLock(path) as acquired:
                    self.assertFalse(acquired)
                first.terminate()
                first.wait(timeout=2)
                with quota.RefreshLock(path) as recovered:
                    self.assertTrue(recovered)
            finally:
                if first.poll() is None:
                    first.terminate()
                    first.wait(timeout=2)

    def test_timeout_and_process_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "helper.py"
            child_pid = Path(directory) / "pid"
            helper.write_text("import os, time\nfrom pathlib import Path\nPath(" + repr(str(child_pid)) + ").write_text(str(os.getpid()))\ntime.sleep(10)\n")
            with mock.patch.object(quota, "CODEX_TIMEOUT", 0.1):
                self.assertEqual(quota.fetch_codex(helper)["error"], "Codex request failed")
            pid = int(child_pid.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_timeout_kills_term_resistant_descendant_after_helper_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            helper = Path(directory) / "helper.py"
            child_pid = Path(directory) / "child-pid"
            helper.write_text(
                "import subprocess, sys\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                + repr("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)")
                + "])\n"
                f"Path({str(child_pid)!r}).write_text(str(child.pid))\n"
            )
            try:
                with mock.patch.object(quota, "CODEX_TIMEOUT", 0.3):
                    self.assertEqual(quota.fetch_codex(helper)["error"], "Codex request failed")
                pid = int(child_pid.read_text())
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    stat = Path(f"/proc/{pid}/stat")
                    if not stat.exists() or stat.read_text().split()[2] == "Z":
                        break
                    time.sleep(0.02)
                else:
                    self.fail("timed-out helper descendant remained running")
            finally:
                if child_pid.exists():
                    pid = int(child_pid.read_text())
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_cli_discovery_uses_known_paths_outside_path(self):
        with mock.patch.object(quota.shutil, "which", return_value=None), mock.patch.object(
            Path, "is_file", autospec=True, side_effect=lambda self: str(self).endswith("/.local/bin/claude")
        ), mock.patch.object(quota.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "Claude Code 2.1.0", "")):
            self.assertEqual(quota.claude_version(), "2.1.0")


class RenderingTests(unittest.TestCase):
    def test_process_states_and_tmux_format_are_safe(self):
        data = {"status": "ok", "windows": [{"label": "5h#{danger}", "percent": 0, "reset_at": 1000}]}
        text = quota.render_status({"claude": True, "codex": False}, data, {}, now=100)
        self.assertIn("not running", text)
        self.assertIn("0%", text)
        self.assertNotIn("#{danger}", text)
        self.assertNotIn("\033[", text)
        self.assertIn("#[fg=", text)

    def test_all_process_combinations(self):
        claude = {"status": "ok", "windows": [{"label": "5h", "percent": 20}]}
        codex = {"status": "ok", "windows": [{"label": "1d", "percent": 30}]}
        for use_unicode in (False, True):
            for states in ({"claude": False, "codex": False}, {"claude": True, "codex": False},
                           {"claude": False, "codex": True}, {"claude": True, "codex": True}):
                text = quota.render_status(states, claude, codex, now=1, use_unicode=use_unicode)
                self.assertEqual(text.count("not running"), sum(not active for active in states.values()))
                self.assertEqual("5h" in text, states["claude"])
                self.assertEqual("1d" in text, states["codex"])

    def test_default_ascii_rendering_remains_unchanged(self):
        claude = {"status": "ok", "windows": [{"label": "5h", "percent": 42, "reset_at": 1000}]}
        actual = quota.render_status({"claude": True, "codex": False}, claude, {}, now=100)
        self.assertEqual(
            actual,
            "#[fg=colour245]C Claude:#[default] #[fg=colour252]5h [###-----] #[fg=colour40]42% (15m)#[default]"
            "  #[fg=colour238]|#[default]  #[fg=colour245]X Codex:#[default] #[fg=colour245]not running#[default]",
        )
        self.assertEqual(quota.quota_bar(42), "[###-----]")

    def test_unicode_bar_has_eight_cells_and_clamps(self):
        self.assertEqual(quota.quota_bar(0, use_unicode=True), "░" * 8)
        self.assertEqual(quota.quota_bar(100, use_unicode=True), "█" * 8)
        self.assertEqual(quota.quota_bar(-25, use_unicode=True), "░" * 8)
        self.assertEqual(quota.quota_bar(125, use_unicode=True), "█" * 8)
        self.assertEqual(len(quota.quota_bar(42, use_unicode=True)), 8)

    def test_unicode_thresholds_timer_separation_and_style_reset(self):
        windows = [
            {"label": "under50", "percent": 49.9, "reset_at": 7200},
            {"label": "at50", "percent": 50},
            {"label": "under80", "percent": 79.9},
            {"label": "at80", "percent": 80},
        ]
        text = quota.render_status(
            {"claude": True, "codex": False},
            {"status": "ok", "windows": windows, "stale": True},
            {},
            now=0,
            use_unicode=True,
        )
        self.assertIn("⚡ Claude:", text)
        self.assertIn("#[fg=#e0af68]stale#[default]", text)
        for label, color in (("under50", "#9ece6a"), ("at50", "#e0af68"),
                             ("under80", "#e0af68"), ("at80", "#f7768e")):
            start = text.index(label)
            end = text.find("  #[fg=#7a83a5]·#[default]  ", start)
            segment = text[start:] if end < 0 else text[start:end]
            self.assertIn(f"#[fg={color}]", segment)
        self.assertIn("#[fg=#a9b1d6](2h00m)#[default]", text)
        self.assertIn("#[fg=#565f89]", text)
        self.assertTrue(text.endswith("#[default]"))
        self.assertNotIn("#[fg=colour", text)

    def test_unicode_states_multiple_windows_and_sanitization(self):
        text = quota.render_status(
            {"claude": True, "codex": True},
            {"status": "ok", "windows": [
                {"label": "5h#{bad}", "percent": 0},
                {"label": "7d", "percent": 100},
            ]},
            {"status": "syncing", "windows": []},
            now=1,
            use_unicode=True,
        )
        self.assertIn("⚡ Claude:", text)
        self.assertIn("✦ Codex:", text)
        self.assertIn("5hbad", text)
        self.assertIn("░" * 8, text)
        self.assertIn("█" * 8, text)
        self.assertIn("#[fg=#c0caf5]syncing#[default]", text)
        self.assertNotIn("#{bad}", text)
        self.assertTrue(text.endswith("#[default]"))

    def test_unicode_unavailable_and_rate_limited_states_are_readable(self):
        text = quota.render_status(
            {"claude": True, "codex": True},
            {"status": "error", "windows": []},
            {"status": "rate_limited", "windows": []},
            use_unicode=True,
        )
        self.assertIn("#[fg=#c0caf5]unavailable#[default]", text)
        self.assertIn("#[fg=#c0caf5]syncing#[default]", text)
        self.assertTrue(text.endswith("#[default]"))

    def test_run_status_passes_unicode_style_without_fetching(self):
        states = {"claude": True, "codex": False}
        claude = {"status": "ok", "windows": [{"label": "5h", "percent": 42}]}
        with mock.patch.object(quota, "process_states", return_value=states), mock.patch.object(
            quota, "cache_dir", return_value=Path("/unused")
        ), mock.patch.object(quota, "provider_data", side_effect=[claude, {}]), mock.patch.object(
            quota.time, "time", return_value=1
        ):
            text = quota.run_status(use_unicode=True)
        self.assertIn("⚡ Claude:", text)
        self.assertIn("█", text)

    def test_unicode_cli_command_and_render_flag(self):
        spaced = Path("/tmp/path with spaces/tmux-quota.py")
        self.assertEqual(
            quota.tmux_command(spaced, use_unicode=True),
            f"#(python3 {shlex.quote(str(spaced.resolve()))} --unicode)",
        )
        result = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command", "--unicode"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(result.stdout.strip(), quota.tmux_command(SOURCE, use_unicode=True))
        default_result = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        self.assertEqual(default_result.stdout.strip(), quota.tmux_command(SOURCE))
        output = io.StringIO()
        with mock.patch.object(quota, "run_status", return_value="UNICODE_STATUS") as run_status, \
             mock.patch.object(sys, "argv", [str(SOURCE), "--unicode"]), contextlib.redirect_stdout(output):
            self.assertEqual(quota.main(), 0)
        run_status.assert_called_once_with(True)
        self.assertEqual(output.getvalue(), "UNICODE_STATUS\n")

    def test_host_process_detection_includes_tmux_children(self):
        result = subprocess.CompletedProcess([], 0, "claude pts/4 claude --resume\ncodex pts/5 codex\nnode ? claude --chrome-native-host\ncodex ? codex app-server\n", "")
        with mock.patch.object(quota.subprocess, "run", return_value=result):
            self.assertEqual(quota.process_states(), {"claude": True, "codex": True})
        result.stdout = "node ? claude --chrome-native-host\ncodex ? codex background\n"
        with mock.patch.object(quota.subprocess, "run", return_value=result):
            self.assertEqual(quota.process_states(), {"claude": False, "codex": False})

    def test_path_with_spaces_is_shell_quoted(self):
        command = quota.tmux_command(Path("/tmp/path with spaces/tmux-quota.py"))
        self.assertIn("'/tmp/path with spaces/tmux-quota.py'", command)


@unittest.skipUnless(shutil.which("tmux") and pty is not None, "Unix tmux is required for the integration check")
class TmuxIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("CCB_SOURCE_DIR"), "set CCB_SOURCE_DIR to check the optional CCB integration")
    def test_ccb_extension_with_real_theme_scripts(self):
        ccb = Path(os.environ["CCB_SOURCE_DIR"]).resolve()
        self.assertIn("#{T:@ccb_status_right_extra}", (ccb / "config/ccb-tmux-on.sh").read_text())
        setup = (SOURCE.parent / "README.md").read_text().split("### CCB integration\n", 1)[1].split("```tmux\n", 1)[1].split("```", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket = str(root / "tmux.sock")
            binary = shutil.which("tmux")
            base = [binary, "-S", socket, "-f", "/dev/null"]

            def tmux(*args):
                return subprocess.check_output(base + list(args), text=True, timeout=5).rstrip("\n")

            def option(key):
                return tmux("show-options", "-Aqv", "-t", "fixture", key)

            def capture(required, forbidden=()):
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 240, 0, 0))
                client = subprocess.Popen(base + ["attach-session", "-t", "fixture"], stdin=slave, stdout=slave, stderr=slave, env={**os.environ, "TERM": "xterm-256color"})
                os.close(slave)
                os.set_blocking(master, False)
                output = bytearray()
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not all(part in output for part in required):
                        try:
                            output.extend(os.read(master, 65536))
                        except BlockingIOError:
                            time.sleep(0.05)
                    for part in required:
                        self.assertIn(part, output)
                    for part in forbidden:
                        self.assertNotIn(part, output)
                finally:
                    client.terminate()
                    client.wait(timeout=3)
                    os.close(master)

            scripts = root / "bin"
            scripts.mkdir()
            for name in ("ccb-tmux-on.sh", "ccb-tmux-off.sh"):
                shutil.copy2(ccb / "config" / name, scripts / name)
            for name, output in (("ccb", "vFIXTURE"), ("ccb-status.sh", "CCB_FIXTURE"), ("ccb-git.sh", "branch"), ("ccb-border.sh", "")):
                path = scripts / name
                path.write_text(f"#!/bin/sh\nprintf '%s' {shlex.quote(output)}\n")
                path.chmod(0o700)
            shim = scripts / "tmux"
            shim.write_text(f"#!/bin/sh\nexec {shlex.quote(binary)} -S {shlex.quote(socket)} \"$@\"\n")
            shim.chmod(0o700)
            clone = root / "clone with spaces 100%"
            clone.mkdir()
            fixture = clone / "tmux-quota.py"
            fixture.write_text(
                "import importlib.util, time\n"
                f"s = importlib.util.spec_from_file_location('quota_fixture', {str(SOURCE)!r})\n"
                "q = importlib.util.module_from_spec(s); s.loader.exec_module(q)\n"
                "now = time.time()\n"
                "def data(a, b):\n"
                "    return {'status': 'ok', 'windows': [{'label': '5h', 'percent': a, 'reset_at': now + 9000}, {'label': '7d', 'percent': b, 'reset_at': now + 345600}]}\n"
                "print(q.render_status({'claude': True, 'codex': True}, data(42, 18), data(88, 32), now, use_unicode=True))\n"
            )
            command = quota.tmux_command(fixture, use_unicode=True)
            config = root / "quota.conf"
            config.write_text(setup.replace("#(python3 /absolute/path/to/agent-quota.wezterm/tmux-quota.py --unicode)", command.replace("%", "%%")))
            try:
                tmux("new-session", "-d", "-s", "fixture", "-x", "240", "-y", "30", "sleep 60")
                pane = tmux("display-message", "-p", "#{pane_id}")
                session = tmux("display-message", "-p", "#{session_id}")
                window = tmux("display-message", "-p", "#{window_id}")
                env = {**os.environ, "PATH": str(scripts) + os.pathsep + os.environ["PATH"], "TMUX": socket + ",0,0", "TMUX_PANE": pane, "CCB_THEME": "dark", "CCB_RUNTIME_TMP": directory}
                args = ["--session", session, "--window", window, "--owner", "quota-test"]
                tmux("set-option", "-g", "status-right", "EXISTING " + command.replace("%", "%%"))
                tmux("set-option", "-g", "status-right-length", "180")
                tmux("source-file", str(config))
                keys = ("status", "status-style", "status-right", "status-right-length", "status-format[0]", "status-format[1]")
                before = {key: option(key) for key in keys}
                for _ in range(2):
                    subprocess.run([str(scripts / "ccb-tmux-on.sh"), *args], env=env, check=True, capture_output=True, timeout=15)
                    self.assertEqual(option("status-right").count("#{T:@ccb_status_right_extra}"), 1)
                    for _ in range(2):
                        tmux("source-file", str(config))
                        self.assertEqual(option("@ccb_status_right_extra").count("tmux-quota.py"), 1)
                    capture((b"CCB_FIXTURE", b"Claude", b"Codex", b"42%", b"88%", "█".encode(), "░".encode()))
                    tmux("set-option", "-t", session, "@ccb_status_right_extra", " SESSION_OVERRIDE 73%%")
                    tmux("source-file", str(config))
                    capture((b"CCB_FIXTURE", b"SESSION_OVERRIDE 73%"), (b"42%", b"88%"))
                    tmux("set-option", "-u", "-t", session, "@ccb_status_right_extra")
                    tmux("set-option", "-g", "@ccb_status_right_extra", " BROKEN_CONTROL #(quota-command-does-not-exist)")
                    capture((b"CCB_FIXTURE", b"BROKEN_CONTROL"), (b"42%", b"88%"))
                    tmux("source-file", str(config))
                    subprocess.run([str(scripts / "ccb-tmux-off.sh"), *args], env=env, check=True, capture_output=True, timeout=15)
                    self.assertEqual({key: option(key) for key in keys}, before)
                    self.assertEqual(option("status-right").count("tmux-quota.py"), 1)
                    self.assertFalse(tmux("show-options", "-q", "-t", session, "status-format"))
            finally:
                subprocess.run(base + ["kill-server"], capture_output=True, timeout=3)

    def test_documented_reload_checks_global_status_despite_session_override(self):
        readme = (SOURCE.parent / "README.md").read_text()
        append = next(line for line in readme.splitlines() if line.startswith("if-shell 'case"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket = str(root / "tmux.sock")
            base = ["tmux", "-S", socket, "-f", "/dev/null"]

            def tmux(*args):
                return subprocess.check_output(base + list(args), text=True, timeout=5).rstrip("\n")

            config = root / "tmux.conf"
            config.write_text(append + "\n")
            try:
                tmux("new-session", "-d", "-s", "fixture")
                tmux("set-option", "-g", "status-right", "KEEP_GLOBAL")
                tmux("set-option", "-t", "fixture", "status-right", "CCB_OVERRIDE")
                tmux("source-file", str(config))
                first = tmux("show-option", "-gqv", "status-right")
                self.assertEqual(first.count("tmux-quota.py"), 1)
                self.assertIn("KEEP_GLOBAL", first)
                for _ in range(2):
                    tmux("source-file", "-t", "fixture:", str(config))
                    self.assertEqual(tmux("show-option", "-gqv", "status-right"), first)
                self.assertEqual(tmux("show-option", "-t", "fixture", "-qv", "status-right"), "CCB_OVERRIDE")

                # Negative control: the former session-scoped guard duplicates the widget.
                config.write_text(
                    "if-shell -F '#{m:*tmux-quota.py*,#{status-right}}' '' "
                    "'set-option -ag status-right \" #(python3 /fixture/tmux-quota.py)\"'\n"
                )
                tmux("source-file", "-t", "fixture:", str(config))
                self.assertEqual(tmux("show-option", "-gqv", "status-right").count("tmux-quota.py"), 2)
            finally:
                subprocess.run(base + ["kill-server"], capture_output=True, timeout=3)

    def test_documented_append_displays_full_dual_provider_status_and_refreshes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket = f"quota-test-{os.getpid()}"
            counter = root / "count"
            script = root / "status script.py"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import importlib.util, time\n"
                "from pathlib import Path\n"
                f"source = Path({str(SOURCE)!r})\n"
                "spec = importlib.util.spec_from_file_location('tmux_quota_fixture', source)\n"
                "module = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(module)\n"
                f"p = Path({str(counter)!r})\n"
                "n = int(p.read_text()) if p.exists() else 0\n"
                "p.write_text(str(n + 1))\n"
                "now = time.time()\n"
                "claude = {'status': 'ok', 'windows': ["
                "{'label': '5h', 'percent': 42, 'reset_at': now + 9000},"
                "{'label': '7d', 'percent': 18, 'reset_at': now + 345600}]}\n"
                "codex = {'status': 'ok', 'windows': ["
                "{'label': '5h', 'percent': 88, 'reset_at': now + 7800},"
                "{'label': '7d', 'percent': 32, 'reset_at': now + 100800}]}\n"
                "print(module.render_status({'claude': True, 'codex': True}, claude, codex, now))\n"
            )
            script.chmod(0o700)
            base = ["tmux", "-L", socket, "-f", "/dev/null"]
            client = None
            master = slave = None
            try:
                subprocess.run(base + ["new-session", "-d", "-x", "180", "-y", "30"], check=True, timeout=3)
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 180, 0, 0))
                subprocess.run(base + ["set-option", "-g", "status-interval", "1"], check=True, timeout=3)
                subprocess.run(base + ["set-option", "-g", "status-right-length", "180"], check=True, timeout=3)
                subprocess.run(base + ["set-option", "-g", "status-right", "EXISTING"], check=True, timeout=3)
                command = quota.tmux_command(script)
                subprocess.run(base + ["set-option", "-ag", "status-right", f"  {command}"], check=True, timeout=3)
                right = subprocess.run(base + ["show-option", "-gv", "status-right"], check=True, capture_output=True, text=True, timeout=3).stdout
                self.assertIn("EXISTING", right)
                self.assertIn(str(script), right)
                env = {**os.environ, "TERM": "xterm-256color"}
                client = subprocess.Popen(base + ["attach-session", "-t", "0"], stdin=slave, stdout=slave, stderr=slave, env=env)
                os.close(slave)
                os.set_blocking(master, False)
                output = bytearray()
                required = (b"EXISTING", b"Claude", b"Codex", b"5h", b"7d", b"42%", b"88%")
                deadline = time.monotonic() + 7
                while not all(part in output for part in required) and time.monotonic() < deadline:
                    try:
                        output.extend(os.read(master, 65536))
                    except BlockingIOError:
                        time.sleep(0.05)
                for part in required:
                    self.assertIn(part, output)
                self.assertRegex(bytes(output), rb"\([^)]*h[0-9]*m\)")
                time.sleep(1.2)
                self.assertGreaterEqual(int(counter.read_text()), 2)
                while True:
                    try:
                        os.read(master, 65536)
                    except BlockingIOError:
                        break
                subprocess.run(base + ["set-option", "-g", "status-right", "EXISTING  #(quota-command-does-not-exist)"], check=True, timeout=3)
                broken = bytearray()
                deadline = time.monotonic() + 2.5
                while time.monotonic() < deadline:
                    try:
                        broken.extend(os.read(master, 65536))
                    except BlockingIOError:
                        time.sleep(0.05)
                self.assertIn(b"EXISTING", broken)
                self.assertNotIn(b"Claude", broken)
                self.assertNotIn(b"Codex", broken)
            finally:
                if client is not None:
                    client.terminate()
                    try:
                        client.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        client.kill()
                        client.wait()
                if master is not None:
                    os.close(master)
                if slave is not None:
                    try:
                        os.close(slave)
                    except OSError:
                        pass
                subprocess.run(base + ["kill-server"], capture_output=True, timeout=3)

    def test_tmux_conf_shell_fragment_handles_spaces_and_broken_command_fails(self):
        with tempfile.TemporaryDirectory(prefix="tmux config spaces ") as directory:
            root = Path(directory)
            socket = f"quota-spaces-{os.getpid()}"
            helper = root / "status helper.py"
            helper.write_text("#!/usr/bin/env python3\nprint('SPACES_PATH_OK')\n")
            helper.chmod(0o700)
            config = root / "tmux.conf"
            fragment = quota.tmux_command(helper)
            config.write_text(
                "set-option -g status-right-length 80\n"
                f'set-option -g status-right "KEEP {fragment}"\n'
            )
            base = ["tmux", "-L", socket, "-f", "/dev/null"]
            client = None
            master = slave = None
            try:
                subprocess.run(base + ["new-session", "-d", "-x", "120", "-y", "30"], check=True, timeout=3)
                subprocess.run(base + ["source-file", str(config)], check=True, timeout=3)
                stored = subprocess.run(base + ["show-option", "-gv", "status-right"], check=True, capture_output=True, text=True, timeout=3).stdout.strip()
                self.assertEqual(stored, f"KEEP {fragment}")
                master, slave = pty.openpty()
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 120, 0, 0))
                env = {**os.environ, "TERM": "xterm-256color"}
                client = subprocess.Popen(base + ["attach-session", "-t", "0"], stdin=slave, stdout=slave, stderr=slave, env=env)
                os.close(slave)
                slave = None
                os.set_blocking(master, False)
                output = bytearray()
                deadline = time.monotonic() + 5
                while b"SPACES_PATH_OK" not in output and time.monotonic() < deadline:
                    try:
                        output.extend(os.read(master, 65536))
                    except BlockingIOError:
                        time.sleep(0.05)
                self.assertIn(b"KEEP", output)
                self.assertIn(b"SPACES_PATH_OK", output)
                while True:
                    try:
                        os.read(master, 65536)
                    except BlockingIOError:
                        break
                subprocess.run(base + ["set-option", "-g", "status-right", "KEEP #(tmux-missing-helper-command)"], check=True, timeout=3)
                broken = bytearray()
                deadline = time.monotonic() + 2.5
                while time.monotonic() < deadline:
                    try:
                        broken.extend(os.read(master, 65536))
                    except BlockingIOError:
                        time.sleep(0.05)
                self.assertIn(b"KEEP", broken)
                self.assertNotIn(b"SPACES_PATH_OK", broken)
            finally:
                if client is not None:
                    client.terminate()
                    try:
                        client.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        client.kill()
                        client.wait()
                if master is not None:
                    os.close(master)
                if slave is not None:
                    os.close(slave)
                subprocess.run(base + ["kill-server"], capture_output=True, timeout=3)


GO_FIXTURE = {
    "usage": {
        "rolling": {"status": "ok", "percent": 0, "resetsAt": "2026-11-06T02:56:29.000Z"},
        "weekly": {"status": "ok", "percent": 0, "resetsAt": "2026-11-09T00:00:00.000Z"},
        "monthly": {"status": "ok", "percent": 0, "resetsAt": "2026-12-05T20:43:07.000Z"},
    }
}
GO_KEY = "opencode-go-leak-control-6b2f4a17"
NO_PLAN_403 = json.dumps({"type": "error", "error": {"type": "SubscriptionError", "message": "No Go plan"}}).encode()
CLOUDFLARE_403_JSON = json.dumps({
    "type": "https://developers.cloudflare.com/support/troubleshooting/http-status-codes/"
            "cloudflare-1xxx-errors/error-1010/",
    "title": "Error 1010: Access denied",
    "status": 403,
    "detail": "The site owner has blocked access based on your browser's signature.",
    "instance": "a55fa1251ad2114c",
    "error_code": 1010,
    "error_name": "browser_signature_banned",
    "cloudflare_error": True,
}).encode()
CLOUDFLARE_403_TEXT = b"error code: 1010"


class OpenCodeGoTests(unittest.TestCase):
    @staticmethod
    def _response(body: bytes) -> mock.MagicMock:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        return response

    @staticmethod
    def _http_error(code: int, body: bytes = b"", *, content_type: str = "application/json"):
        return urllib.error.HTTPError(
            "https://example.invalid", code, "private body", {"Content-Type": content_type}, io.BytesIO(body)
        )

    @staticmethod
    def _env(key):
        environment = {name: value for name, value in os.environ.items() if name != "OPENCODE_API_KEY"}
        if key is not None:
            environment["OPENCODE_API_KEY"] = key
        return mock.patch.dict(os.environ, environment, clear=True)

    def _fetch(self, payload=None, *, body=None, error=None, key=GO_KEY, now=100):
        if body is None:
            body = json.dumps(GO_FIXTURE if payload is None else payload).encode()
        with self._env(key), mock.patch.object(
            quota.urllib.request, "urlopen", side_effect=error, return_value=self._response(body)
        ) as urlopen:
            result = quota.fetch_opencode_go(now)
        return result, urlopen

    def _render(self, data, *, now=100, use_unicode=False):
        return quota.render_status(
            {"opencode-go": True}, {}, {}, now=now, use_unicode=use_unicode,
            providers=("opencode-go",), opencode_go=data,
        )

    def test_numeric_or_non_string_resets_are_discarded_without_render_overflow(self):
        weekly = "2026-11-09T00:00:00.000Z"
        monthly = "2026-12-05T20:43:07.000Z"
        # A JSON number that overflows to infinity: json.loads("1e309") == inf, and an
        # accepted inf reset_at made countdown() raise OverflowError while rendering.
        body = (
            b'{"usage":{"rolling":{"status":"ok","percent":5,"resetsAt":1e309},'
            b'"weekly":{"status":"ok","percent":7,"resetsAt":"' + weekly.encode() + b'"},'
            b'"monthly":{"status":"ok","percent":3,"resetsAt":"' + monthly.encode() + b'"}}}'
        )
        result, _ = self._fetch(body=body)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([window["label"] for window in result["windows"]], ["7d", "mo"])
        self.assertTrue(all(math.isfinite(window["reset_at"]) for window in result["windows"]))
        for use_unicode in (False, True):
            text = self._render(result, use_unicode=use_unicode)
            self.assertNotIn("5%", text)
            self.assertIn("7%", text)

        for value in (1793933789, 1793933789.5, float("inf"), None, True, [], {},
                      {"iso": weekly}, "", "not-a-timestamp"):
            with self.subTest(resetsAt=repr(value)):
                payload = {"usage": {
                    "rolling": {"status": "ok", "percent": 5, "resetsAt": value},
                    "weekly": {"status": "ok", "percent": 7, "resetsAt": weekly},
                }}
                result, _ = self._fetch(payload)
                self.assertEqual(result["status"], "ok")
                self.assertEqual([window["label"] for window in result["windows"]], ["7d"])
                self.assertNotIn("5%", self._render(result))

    def test_meter_status_must_be_ok_or_rate_limited(self):
        rolling = "2026-11-06T02:56:29.000Z"
        valid_weekly = {"status": "ok", "percent": 7, "resetsAt": "2026-11-09T00:00:00.000Z"}
        for status in (None, "", "weird", "OK", "rate_limited", "rate-limited ", 1, True, ["ok"]):
            with self.subTest(status=repr(status)):
                payload = {"usage": {
                    "rolling": {"status": status, "percent": 5, "resetsAt": rolling},
                    "weekly": valid_weekly,
                }}
                result, _ = self._fetch(payload)
                self.assertEqual(result["status"], "ok")
                self.assertEqual([window["label"] for window in result["windows"]], ["7d"])

        missing_status = {"usage": {
            "rolling": {"percent": 5, "resetsAt": rolling},
            "weekly": valid_weekly,
        }}
        result, _ = self._fetch(missing_status)
        self.assertEqual([window["label"] for window in result["windows"]], ["7d"])

        limited = {"usage": {
            "rolling": {"status": "rate-limited", "percent": 5, "resetsAt": rolling},
            "weekly": valid_weekly,
        }}
        result, _ = self._fetch(limited)
        self.assertTrue(result["windows"][0]["limited"])

        all_rejected = {"usage": {
            "rolling": {"status": "weird", "percent": 5, "resetsAt": rolling},
            "weekly": {"status": "ok", "percent": 5, "resetsAt": 1793933789},
            "monthly": {"status": "ok", "percent": 5},
        }}
        result, _ = self._fetch(all_rejected)
        self.assertEqual(result, {"status": "error", "error": "OpenCode Go response unavailable"})
        self.assertIn("unavailable", self._render(result))

    def test_real_fixture_parses_three_windows_and_keeps_zero_percent(self):
        result, urlopen = self._fetch()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [(window["label"], window["percent"]) for window in result["windows"]],
            [("5h", 0.0), ("7d", 0.0), ("mo", 0.0)],
        )
        self.assertEqual(result["windows"][0]["reset_at"], quota.parse_timestamp("2026-11-06T02:56:29.000Z"))
        self.assertNotIn("limited", result["windows"][0])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, quota.GO_USAGE_URL)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {GO_KEY}")
        self.assertEqual(request.get_header("User-agent"), quota.GO_USER_AGENT)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], quota.NETWORK_TIMEOUT)

    def test_nonzero_floats_and_rate_limited_meters(self):
        rolling = "2026-11-06T02:56:29.000Z"
        payload = {"usage": {
            "rolling": {"status": "rate-limited", "percent": 100, "resetsAt": rolling},
            "weekly": {"status": "ok", "percent": 42.5, "resetsAt": "2026-11-09T00:00:00.000Z"},
            "monthly": {"status": "ok", "percent": 0.4, "resetsAt": "2026-12-05T20:43:07.000Z"},
        }}
        result, _ = self._fetch(payload)
        self.assertEqual(
            [(window["label"], window["percent"], window.get("limited", False)) for window in result["windows"]],
            [("5h", 100.0, True), ("7d", 42.5, False), ("mo", 0.4, False)],
        )
        now = quota.parse_timestamp(rolling) - 600
        unicode_text = self._render(result, now=now, use_unicode=True)
        self.assertIn("◈ OpenCode Go:", unicode_text)
        self.assertIn("#[fg=#f7768e]100% limited#[default]", unicode_text)
        self.assertIn("#[fg=#c0caf5]5h#[default]", unicode_text)
        ascii_text = self._render(result, now=now)
        self.assertIn("O OpenCode Go:", ascii_text)
        self.assertIn("#[fg=colour196]100% limited (10m)#[default]", ascii_text)

    def test_invalid_percent_or_reset_drops_only_that_meter(self):
        rolling = "2026-11-06T02:56:29.000Z"
        partial = {"usage": {
            "rolling": {"status": "ok", "percent": 120, "resetsAt": rolling},
            "weekly": {"status": "ok", "percent": -1, "resetsAt": "2026-11-09T00:00:00.000Z"},
            "monthly": {"status": "ok", "percent": 55.5, "resetsAt": "2026-12-05T20:43:07.000Z"},
        }}
        result, _ = self._fetch(partial)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([(window["label"], window["percent"]) for window in result["windows"]], [("mo", 55.5)])

        unparseable_reset = {"usage": {
            "rolling": {"status": "ok", "percent": 10, "resetsAt": "not-a-timestamp"},
            "weekly": {"status": "ok", "percent": 20, "resetsAt": None},
            "monthly": {"status": "ok", "percent": 30},
        }}
        result, _ = self._fetch(unparseable_reset)
        self.assertEqual(result, {"status": "error", "error": "OpenCode Go response unavailable"})

        float_and_bool = {"usage": {
            "rolling": {"status": "ok", "percent": True, "resetsAt": rolling},
            "weekly": {"status": "ok", "percent": 0.0, "resetsAt": "2026-11-09T00:00:00.000Z"},
            "monthly": {"status": "ok", "percent": float("nan"), "resetsAt": "2026-12-05T20:43:07.000Z"},
        }}
        result, _ = self._fetch(float_and_bool)
        self.assertEqual([(window["label"], window["percent"]) for window in result["windows"]], [("7d", 0.0)])

        only_boolean = {"usage": {"rolling": {"status": "ok", "percent": True, "resetsAt": rolling}}}
        result, _ = self._fetch(only_boolean)
        self.assertEqual(result["error"], "OpenCode Go response unavailable")

        missing_meter = {"usage": {"weekly": {"status": "ok", "percent": 7, "resetsAt": rolling}}}
        result, _ = self._fetch(missing_meter)
        self.assertEqual([window["label"] for window in result["windows"]], ["7d"])

        for payload in ({"usage": {}}, {"usage": []}, {}, []):
            with self.subTest(payload=payload):
                result, _ = self._fetch(payload)
                self.assertEqual(result, {"status": "error", "error": "OpenCode Go response unavailable"})

        result, _ = self._fetch(body=b"<html>not json</html>")
        self.assertEqual(result, {"status": "error", "error": "OpenCode Go request failed"})

    def test_http_and_network_errors_map_to_sanitized_states(self):
        cases = [
            (self._http_error(401, b'{"type":"error","error":{"type":"AuthError","message":"Unauthorized"}}'),
             "error", "OpenCode Go request failed"),
            (self._http_error(403, NO_PLAN_403), "no_plan", None),
            (self._http_error(403, CLOUDFLARE_403_JSON), "error", "OpenCode Go request failed"),
            (self._http_error(403, CLOUDFLARE_403_TEXT, content_type="text/plain"),
             "error", "OpenCode Go request failed"),
            (self._http_error(403, b"<html><body>Blocked</body></html>", content_type="text/html"),
             "error", "OpenCode Go request failed"),
            (self._http_error(403, b"{}"), "error", "OpenCode Go request failed"),
            (self._http_error(403, b"[]"), "error", "OpenCode Go request failed"),
            (self._http_error(403, b""), "error", "OpenCode Go request failed"),
            (self._http_error(429, b'{"type":"error","error":{"type":"RateLimitError"}}'),
             "rate_limited", "OpenCode Go rate limited"),
            (self._http_error(500, b"{}"), "error", "OpenCode Go request failed"),
            (TimeoutError(), "error", "OpenCode Go request failed"),
            (urllib.error.URLError("unreachable private detail"), "error", "OpenCode Go request failed"),
        ]
        for error, status, message in cases:
            with self.subTest(status=status, error=type(error).__name__, detail=getattr(error, "code", "")):
                result, _ = self._fetch(error=error)
                self.assertEqual(result["status"], status)
                if message is None:
                    self.assertNotIn("windows", result)
                    self.assertEqual(self._render(result), self._render({"status": "no_plan"}))
                else:
                    self.assertEqual(result["error"], message)
                self.assertNotIn("private", repr(result))
                self.assertNotIn("Blocked", repr(result))
                self.assertNotIn("browser", repr(result))
                self.assertNotIn("1010", repr(result))
                self.assertNotIn(GO_KEY, repr(result))

    def test_403_envelope_probe_is_bounded_and_tolerates_unreadable_bodies(self):
        fp = mock.Mock()
        fp.read.return_value = b'{"type":"error","error":{"type":"SubscriptionError"}}'
        result, _ = self._fetch(error=urllib.error.HTTPError(
            "https://example.invalid", 403, "private body", {}, fp
        ))
        self.assertEqual(result, {"status": "no_plan"})
        fp.read.assert_called_once_with(4096)

        huge = self._http_error(403, b"x" * 200_000, content_type="text/plain")
        result, _ = self._fetch(error=huge)
        self.assertEqual(result, {"status": "error", "error": "OpenCode Go request failed"})

        unreadable = urllib.error.HTTPError(
            "https://example.invalid", 403, "private body", {},
            mock.Mock(read=mock.Mock(side_effect=OSError("stream closed"))),
        )
        result, _ = self._fetch(error=unreadable)
        self.assertEqual(result, {"status": "error", "error": "OpenCode Go request failed"})
        self.assertNotIn("stream closed", repr(result))

    def test_missing_key_is_local_and_starts_no_network_request(self):
        for key in (None, "", "   "):
            with self.subTest(key=key):
                result, urlopen = self._fetch(key=key)
                self.assertEqual(result, {"status": "no_key", "error": "OpenCode Go key missing"})
                urlopen.assert_not_called()
        self.assertIn("no key", self._render({"status": "no_key"}))
        self.assertIn("#[fg=colour245]no key#[default]", self._render({"status": "no_key"}))

    def test_no_plan_clears_stale_windows_throttles_and_recovers(self):
        forbidden = self._http_error(403, NO_PLAN_403)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = lambda: {"status": "ok", "windows": [{"label": "5h", "percent": 42, "reset_at": 5000}]}
            self.assertEqual(quota.provider_data("opencode-go", True, 100, good, root)["status"], "ok")
            stale = quota.provider_data(
                "opencode-go", True, 161, lambda: {"status": "error", "error": "safe"}, root
            )
            self.assertTrue(stale["stale"])
            self.assertEqual([window["label"] for window in stale["windows"]], ["5h"])

            with self._env(GO_KEY), mock.patch.object(quota.urllib.request, "urlopen", side_effect=forbidden):
                plan = quota.provider_data("opencode-go", True, 191, lambda: quota.fetch_opencode_go(191), root)
            self.assertEqual(plan["status"], "no_plan")
            self.assertEqual(plan["windows"], [])
            self.assertNotIn("stale", plan)
            self.assertEqual(plan["error_count"], 0)
            self.assertEqual(plan["next_refresh_at"], 191 + quota.NO_PLAN_TTL)
            self.assertEqual(quota.NO_PLAN_TTL, 3600)
            disk = quota.read_json(root / "opencode-go.json")
            self.assertEqual((disk["status"], disk["windows"], disk["error_count"]), ("no_plan", [], 0))
            text = self._render(plan, now=191)
            self.assertIn("no Go plan", text)
            self.assertNotIn("42%", text)
            self.assertNotIn("stale", text)

            def unexpected():
                raise AssertionError("no-plan cache should not refetch before its deadline")
            throttled = quota.provider_data("opencode-go", True, 191 + quota.NO_PLAN_TTL - 1, unexpected, root)
            self.assertEqual(throttled["status"], "no_plan")

            failed = quota.provider_data(
                "opencode-go", True, 191 + quota.NO_PLAN_TTL, lambda: {"status": "error", "error": "safe"}, root
            )
            self.assertEqual((failed["status"], failed["windows"]), ("error", []))
            recovered = quota.provider_data("opencode-go", True, failed["next_refresh_at"], good, root)
            self.assertEqual(recovered["status"], "ok")
            self.assertEqual(recovered["windows"][0]["percent"], 42)

    def test_secret_never_reaches_cache_output_or_error_text(self):
        errors = {
            "auth": self._http_error(401, b'{"type":"error","error":{"type":"AuthError"}}'),
            "no-plan": self._http_error(403, NO_PLAN_403),
            "cloudflare-json-403": self._http_error(403, CLOUDFLARE_403_JSON),
            "cloudflare-text-403": self._http_error(403, CLOUDFLARE_403_TEXT, content_type="text/plain"),
            "limited": self._http_error(429, b'{"type":"error","error":{"type":"RateLimitError"}}'),
            "timeout": TimeoutError(),
            "network": urllib.error.URLError("unreachable"),
        }
        expected = {
            "ok": "ok", "auth": "error", "no-plan": "no_plan", "cloudflare-json-403": "error",
            "cloudflare-text-403": "error", "limited": "rate_limited", "timeout": "error",
            "network": "error",
        }
        with tempfile.TemporaryDirectory() as directory:
            for label, error in (("ok", None), *errors.items()):
                with self.subTest(scenario=label):
                    root = Path(directory) / label
                    with self._env(GO_KEY), mock.patch.object(
                        quota.urllib.request, "urlopen", side_effect=error,
                        return_value=self._response(json.dumps(GO_FIXTURE).encode()),
                    ):
                        data = quota.provider_data(
                            "opencode-go", True, 100, lambda: quota.fetch_opencode_go(100), root
                        )
                    self.assertEqual(data["status"], expected[label])
                    if label == "ok":
                        self.assertEqual(len(data["windows"]), 3)
                    haystacks = {
                        "cache file": (root / "opencode-go.json").read_text(),
                        "result": repr(data),
                        "serialized": json.dumps(data),
                        "ascii": self._render(data),
                        "unicode": self._render(data, use_unicode=True),
                    }
                    for where, haystack in haystacks.items():
                        self.assertNotIn(GO_KEY, haystack, where)
                        self.assertNotIn("private body", haystack, where)
                        self.assertNotIn("error code: 1010", haystack, where)
                        self.assertNotIn("browser_signature_banned", haystack, where)
                        self.assertNotIn("1010", haystack, where)

    def test_unselected_providers_never_run_their_fetchers_or_read_credentials(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(quota, "process_states", return_value={"claude": True}), \
             mock.patch.object(quota, "cache_dir", return_value=Path(directory)), \
             mock.patch.object(quota, "fetch_claude", return_value={
                 "status": "ok", "windows": [{"label": "5h", "percent": 10, "reset_at": 5000}]
             }) as fetch_claude, \
             mock.patch.object(quota, "fetch_codex") as fetch_codex, \
             mock.patch.object(quota, "fetch_opencode_go") as fetch_opencode_go, \
             mock.patch.object(quota, "read_claude_token") as read_claude_token, \
             mock.patch.object(quota.time, "time", return_value=1):
            text = quota.run_status(providers=("claude",))
            files = {path.name for path in Path(directory).iterdir()}
        self.assertEqual(files, {"claude.json", "claude.lock"})
        fetch_claude.assert_called_once()
        fetch_codex.assert_not_called()
        fetch_opencode_go.assert_not_called()
        read_claude_token.assert_not_called()
        self.assertIn("Claude:", text)
        self.assertNotIn("Codex", text)
        self.assertNotIn("OpenCode", text)

    def test_opencode_go_only_writes_only_its_cache_file(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(quota, "process_states", return_value={"opencode-go": True}) as states, \
             mock.patch.object(quota, "cache_dir", return_value=Path(directory)), \
             mock.patch.object(quota, "fetch_opencode_go", return_value={
                 "status": "ok", "windows": [{"label": "5h", "percent": 30, "reset_at": 5000}]
             }) as fetch_opencode_go, \
             mock.patch.object(quota, "fetch_claude") as fetch_claude, \
             mock.patch.object(quota, "fetch_codex") as fetch_codex, \
             mock.patch.object(quota, "read_claude_token") as read_claude_token, \
             mock.patch.object(quota.time, "time", return_value=1):
            text = quota.run_status(providers=("opencode-go",))
            files = {path.name for path in Path(directory).iterdir()}
        self.assertEqual(files, {"opencode-go.json", "opencode-go.lock"})
        states.assert_called_once_with(("opencode-go",))
        fetch_opencode_go.assert_called_once_with(1)
        fetch_claude.assert_not_called()
        fetch_codex.assert_not_called()
        read_claude_token.assert_not_called()
        self.assertIn("O OpenCode Go:", text)
        self.assertIn("30%", text)
        self.assertNotIn("Claude", text)
        self.assertNotIn("Codex", text)

    def test_default_providers_still_call_only_claude_and_codex(self):
        with mock.patch.object(quota, "process_states", return_value={"claude": True, "codex": True}) as states, \
             mock.patch.object(quota, "cache_dir", return_value=Path("/unused")), \
             mock.patch.object(quota, "provider_data", return_value={"status": "not_running"}) as provider_data, \
             mock.patch.object(quota, "fetch_opencode_go") as fetch_opencode_go, \
             mock.patch.object(quota.time, "time", return_value=1):
            quota.run_status()
        states.assert_called_once_with(("claude", "codex"))
        self.assertEqual([call.args[0] for call in provider_data.call_args_list], ["claude", "codex"])
        fetch_opencode_go.assert_not_called()

    def test_provider_selection_is_canonical_and_deduplicated(self):
        self.assertEqual(quota.parse_providers("claude,codex"), ("claude", "codex"))
        self.assertEqual(quota.parse_providers("opencode-go, claude,claude"), ("claude", "opencode-go"))
        with self.assertRaises(ValueError):
            quota.parse_providers("")
        with self.assertRaises(ValueError):
            quota.parse_providers(",")
        with mock.patch.object(quota, "process_states", return_value={}) as states, \
             mock.patch.object(quota, "cache_dir", return_value=Path("/unused")), \
             mock.patch.object(quota, "provider_data", return_value={"status": "not_running"}), \
             mock.patch.object(quota.time, "time", return_value=1):
            quota.run_status(providers=quota.parse_providers("opencode-go,claude"))
        states.assert_called_once_with(("claude", "opencode-go"))

    def test_cli_providers_and_tmux_command_round_trip(self):
        spaced = Path("/tmp/path with spaces/tmux-quota.py")
        command = quota.tmux_command(spaced, use_unicode=True, providers=("claude", "opencode-go"))
        self.assertEqual(
            command,
            "#(python3 " + shlex.quote(str(spaced.resolve())) + " --unicode --providers claude,opencode-go)",
        )
        selected = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command", "--unicode", "--providers", "claude,opencode-go"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(selected.stdout.strip(), quota.tmux_command(
            SOURCE, use_unicode=True, providers=("claude", "opencode-go")
        ))
        self.assertIn(" --unicode --providers claude,opencode-go)", selected.stdout)
        only_go = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command", "--providers", "opencode-go"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(only_go.stdout.strip(), quota.tmux_command(SOURCE, providers=("opencode-go",)))
        self.assertIn("--providers opencode-go", only_go.stdout)
        default = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command"], check=True, capture_output=True, text=True, timeout=5
        )
        self.assertEqual(default.stdout.strip(), quota.tmux_command(SOURCE))
        self.assertNotIn("--providers", default.stdout)
        rejected = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command", "--providers", "claude,gemini"],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("gemini", rejected.stderr)
        empty = subprocess.run(
            [sys.executable, str(SOURCE), "--providers", ","], capture_output=True, text=True, timeout=5
        )
        self.assertEqual(empty.returncode, 2)
        self.assertIn("--providers needs at least one", empty.stderr)

    def test_process_detection_matches_pi_and_opencode_but_not_launchers(self):
        live = subprocess.CompletedProcess([], 0, (
            "pi              pts/16   pi\n"
            "opencode        pts/27   opencode\n"
            "codex           pts/5    codex\n"
            "python3         pts/15   python3 /home/x/.local/bin/ccb codex pi claude\n"
            "python3         ?        python3 /home/x/bin/ask pi --foreground\n"
        ), "")
        with mock.patch.object(quota.subprocess, "run", return_value=live):
            self.assertEqual(quota.process_states(("opencode-go",)), {"opencode-go": True})
            self.assertEqual(
                quota.process_states(),
                {"claude": False, "codex": True},
            )
        idle = subprocess.CompletedProcess([], 0, (
            "python3         pts/15   python3 /home/x/.local/bin/ccb codex pi claude\n"
            "python3         pts/27   python3 /home/x/memsearch-opencode/scripts/capture-daemon.py\n"
            "opencode        ?        opencode serve\n"
        ), "")
        with mock.patch.object(quota.subprocess, "run", return_value=idle):
            self.assertEqual(quota.process_states(("opencode-go",)), {"opencode-go": False})
        with mock.patch.object(quota.subprocess, "run", mock.Mock(side_effect=OSError("ps missing"))):
            self.assertEqual(
                quota.process_states(("claude", "opencode-go")),
                {"claude": False, "opencode-go": False},
            )

    def test_no_plan_and_no_key_rendering_states(self):
        self.assertIn("no Go plan", self._render({"status": "no_plan"}))
        self.assertIn("no key", self._render({"status": "no_key"}))
        self.assertIn("syncing", self._render({"status": "rate_limited", "windows": []}))
        self.assertIn("unavailable", self._render({"status": "error", "windows": []}))
        idle = quota.render_status({"opencode-go": False}, {}, {}, now=1, providers=("opencode-go",))
        self.assertIn("not running", idle)
        self.assertEqual(idle.count("OpenCode Go:"), 1)
        stale = self._render({"status": "error", "windows": [{"label": "5h", "percent": 12}], "stale": True})
        self.assertIn("stale", stale)
        self.assertIn("12%", stale)


class OpenCodeKeyCommandTests(unittest.TestCase):
    """The optional command that supplies the OpenCode Go key on demand."""

    COMMAND = "mcpctl vault get opencode-go-api-main"
    KEY = "command-supplied-key-5c1e9a7d3b2f80461c8d5e9a"
    PROVIDERS = ("claude", "codex", "opencode-go")

    @staticmethod
    def _completed(stdout=b"", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout, b"")

    def _env(self, key):
        return OpenCodeGoTests._env(key)

    def _response(self, body):
        return OpenCodeGoTests._response(body)

    def test_key_command_supplies_the_bearer_token_without_leaking_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self._env(None), \
                 mock.patch.object(
                     quota.subprocess, "run", return_value=self._completed(self.KEY.encode() + b"\n"),
                 ) as run, \
                 mock.patch.object(
                     quota.urllib.request, "urlopen",
                     return_value=self._response(json.dumps(GO_FIXTURE).encode()),
                 ) as urlopen:
                data = quota.provider_data(
                    "opencode-go", True, 100,
                    lambda: quota.fetch_opencode_go(100, self.COMMAND), root,
                )
            cache_text = (root / "opencode-go.json").read_text()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["windows"]), 3)
        run.assert_called_once()
        argv, kwargs = run.call_args
        self.assertEqual(argv[0], ["mcpctl", "vault", "get", "opencode-go-api-main"])
        self.assertIs(kwargs["shell"], False)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], quota.KEY_COMMAND_TIMEOUT)
        urlopen.assert_called_once()
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_header("Authorization"), f"Bearer {self.KEY}")
        rendered = quota.render_status(
            {"opencode-go": True}, {}, {}, now=100, use_unicode=True,
            providers=("opencode-go",), opencode_go=data,
        )
        for where, haystack in {
            "cache file": cache_text,
            "result": repr(data),
            "serialized": json.dumps(data),
            "rendered": rendered,
            "argv": repr(run.call_args),
        }.items():
            self.assertNotIn(self.KEY, haystack, where)

    def test_failed_lookup_reports_no_key_and_still_renders_other_providers(self):
        claude = {"status": "ok", "windows": [{"label": "5h", "percent": 11, "reset_at": 5000}]}
        codex = {"status": "ok", "windows": [{"label": "5h", "percent": 22, "reset_at": 5000}]}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(quota, "process_states", return_value={
                 "claude": True, "codex": True, "opencode-go": True,
             }), \
             mock.patch.object(quota, "cache_dir", return_value=Path(directory)), \
             mock.patch.object(quota, "fetch_claude", return_value=claude), \
             mock.patch.object(quota, "fetch_codex", return_value=codex), \
             mock.patch.object(quota.subprocess, "run", return_value=self._completed(b"", 2)) as run, \
             mock.patch.object(quota.urllib.request, "urlopen") as urlopen, \
             self._env(None), \
             mock.patch.object(quota.time, "time", return_value=1):
            text = quota.run_status(True, self.PROVIDERS, "full", None, self.COMMAND)
            cache_files = {path.name for path in Path(directory).iterdir()}
        run.assert_called_once()
        urlopen.assert_not_called()
        self.assertIn("no key", text)
        self.assertIn("11%", text)
        self.assertIn("22%", text)
        self.assertIn("Claude:", text)
        self.assertIn("Codex:", text)
        self.assertNotIn("not running", text)
        self.assertEqual(cache_files, {
            "claude.json", "claude.lock", "codex.json", "codex.lock",
            "opencode-go.json", "opencode-go.lock",
        })

    def test_key_command_never_runs_when_the_lookup_is_not_needed(self):
        windows = {"status": "ok", "windows": [{"label": "5h", "percent": 10, "reset_at": 5000}]}
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(quota, "cache_dir", return_value=Path(directory)), \
             mock.patch.object(quota, "fetch_opencode_go", return_value=windows) as fetch_go, \
             mock.patch.object(quota.subprocess, "run") as run, \
             self._env(None), \
             mock.patch.object(quota.time, "time", return_value=1):
            with mock.patch.object(quota, "process_states", return_value={"claude": True}), \
                 mock.patch.object(quota, "fetch_claude", return_value=windows):
                unselected = quota.run_status(False, ("claude",), "full", None, self.COMMAND)
            with mock.patch.object(quota, "process_states", return_value={"opencode-go": False}):
                inactive = quota.run_status(False, ("opencode-go",), "full", None, self.COMMAND)
            Path(directory).joinpath("opencode-go.json").write_text(json.dumps({
                **windows, "next_refresh_at": 10_000, "error_count": 0,
            }))
            with mock.patch.object(quota, "process_states", return_value={"opencode-go": True}) as states:
                fresh = quota.run_status(False, ("opencode-go",), "full", None, self.COMMAND)
            self.assertEqual(states.call_count, 1)
            Path(directory).joinpath("opencode-go.json").unlink()
            with mock.patch.object(quota, "process_states", return_value={"opencode-go": True}):
                with quota.RefreshLock(Path(directory) / "opencode-go.lock") as held:
                    self.assertTrue(held)
                    locked = quota.run_status(False, ("opencode-go",), "full", None, self.COMMAND)
        run.assert_not_called()
        self.assertIn("Claude:", unselected)
        self.assertNotIn("OpenCode", unselected)
        self.assertIn("not running", inactive)
        self.assertIn("10%", fresh)
        self.assertIn("syncing", locked)
        fetch_go.assert_not_called()

    def test_nonblank_environment_key_wins_over_the_command(self):
        with self._env(GO_KEY), \
             mock.patch.object(quota.subprocess, "run") as run, \
             mock.patch.object(
                 quota.urllib.request, "urlopen",
                 return_value=self._response(json.dumps(GO_FIXTURE).encode()),
             ) as urlopen:
            data = quota.fetch_opencode_go(100, self.COMMAND)
        self.assertEqual(data["status"], "ok")
        run.assert_not_called()
        urlopen.assert_called_once()
        self.assertEqual(
            urlopen.call_args[0][0].get_header("Authorization"), f"Bearer {GO_KEY}"
        )

    def test_blank_environment_key_falls_back_to_the_command(self):
        token = "fallback-command-key-71b3e0c9"
        for blank in ("", "   ", "\t\n"):
            with self.subTest(env=repr(blank)):
                with self._env(blank), \
                     mock.patch.object(
                         quota.subprocess, "run",
                         return_value=self._completed(token.encode() + b"\n"),
                     ) as run, \
                     mock.patch.object(
                         quota.urllib.request, "urlopen",
                         return_value=self._response(json.dumps(GO_FIXTURE).encode()),
                     ) as urlopen:
                    data = quota.fetch_opencode_go(100, self.COMMAND)
                self.assertEqual(data["status"], "ok")
                run.assert_called_once()
                self.assertEqual(
                    urlopen.call_args[0][0].get_header("Authorization"), f"Bearer {token}"
                )

    def test_blank_environment_key_without_a_command_still_reports_no_key(self):
        with self._env("  "), \
             mock.patch.object(quota.subprocess, "run") as run, \
             mock.patch.object(quota.urllib.request, "urlopen") as urlopen:
            data = quota.fetch_opencode_go(100)
        self.assertEqual(data, {"status": "no_key", "error": "OpenCode Go key missing"})
        run.assert_not_called()
        urlopen.assert_not_called()

    def test_key_command_failure_matrix_reports_no_key_without_http(self):
        timeout = subprocess.TimeoutExpired(self.COMMAND, 5)
        cases = [
            ("unbalanced quote", "tool 'unclosed", {}, False),
            ("missing executable", self.COMMAND, {"side_effect": FileNotFoundError("missing")}, True),
            ("timeout", self.COMMAND, {"side_effect": timeout}, True),
            ("nonzero exit", self.COMMAND, {"return_value": self._completed(b"", 2)}, True),
            ("invalid utf-8", self.COMMAND, {"return_value": self._completed(b"\xff\xfe\n")}, True),
            ("empty", self.COMMAND, {"return_value": self._completed(b"")}, True),
            ("newline only", self.COMMAND, {"return_value": self._completed(b"\n")}, True),
            ("embedded newline", self.COMMAND, {"return_value": self._completed(b"a\nb\n")}, True),
            ("carriage return", self.COMMAND, {"return_value": self._completed(b"a\rb")}, True),
            ("control character", self.COMMAND, {"return_value": self._completed(b"a\x07b\n")}, True),
            ("leading space", self.COMMAND, {"return_value": self._completed(b" key\n")}, True),
            ("trailing space", self.COMMAND, {"return_value": self._completed(b"key \n")}, True),
            ("space only", self.COMMAND, {"return_value": self._completed(b" \n")}, True),
        ]
        for label, command, run_kwargs, executes in cases:
            with self.subTest(case=label):
                with self._env(None), \
                     mock.patch.object(quota.subprocess, "run", **run_kwargs) as run, \
                     mock.patch.object(quota.urllib.request, "urlopen") as urlopen:
                    data = quota.fetch_opencode_go(100, command)
                self.assertEqual(data, {"status": "no_key", "error": "OpenCode Go key missing"})
                urlopen.assert_not_called()
                self.assertEqual(run.call_count, 1 if executes else 0)

    def test_key_command_accepts_one_trailing_line_ending(self):
        for raw in (b"key-value", b"key-value\n", b"key-value\r\n"):
            with self.subTest(raw=raw):
                self.assertEqual(quota.validated_go_key(raw), "key-value")
        for raw in (b"", b"\n", b"\r\n", b"a\nb", b"key value\n\n", b" key", b"key ", b"\xc3\x28"):
            with self.subTest(raw=raw):
                self.assertIsNone(quota.validated_go_key(raw))

    def test_tmux_command_quotes_the_option_without_running_it(self):
        with mock.patch.object(quota.subprocess, "run") as run:
            line = quota.tmux_command(
                SOURCE, True, self.PROVIDERS, "compact", None, self.COMMAND
            )
        run.assert_not_called()
        self.assertIn("--opencode-key-command " + shlex.quote(self.COMMAND), line)
        self.assertTrue(line.endswith(")"))
        self.assertIn("--layout compact", line)
        self.assertIn("--providers claude,codex,opencode-go", line)
        tricky = "tool with 'quote' and space"
        quoted = quota.tmux_command(SOURCE, False, quota.DEFAULT_PROVIDERS, "full", None, tricky)
        self.assertIn(shlex.quote(tricky), quoted)
        self.assertIn("'\"'\"'", quoted)
        default = quota.tmux_command(SOURCE)
        self.assertNotIn("--opencode-key-command", default)
        completed = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command", "--unicode", "--layout", "compact",
             "--providers", "claude,codex,opencode-go",
             "--opencode-key-command", self.COMMAND],
            check=True, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(completed.stdout.strip(), line)
        self.assertNotIn("command-supplied", completed.stdout)

    def test_cli_passes_the_key_command_into_the_run(self):
        output = io.StringIO()
        with mock.patch.object(quota, "run_status", return_value="STATUS") as run_status, \
             mock.patch.object(
                 sys, "argv",
                 [str(SOURCE), "--providers", "opencode-go", "--opencode-key-command", self.COMMAND],
             ), contextlib.redirect_stdout(output):
            self.assertEqual(quota.main(), 0)
        run_status.assert_called_once_with(
            False, ("opencode-go",), "full", None, self.COMMAND, False
        )
        self.assertEqual(output.getvalue(), "STATUS\n")
        with mock.patch.object(quota, "run_status", return_value="STATUS") as default_run, \
             mock.patch.object(sys, "argv", [str(SOURCE)]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(quota.main(), 0)
        default_run.assert_called_once_with(False)


class HideInactiveTests(unittest.TestCase):
    """--hide-inactive drops providers whose CLI process is not running."""

    NOW = 1793969150
    ALL = ("claude", "codex", "opencode-go")
    NAMES = {"claude": "Claude", "codex": "Codex", "opencode-go": "OpenCode Go"}
    ICONS = {"claude": "⚡", "codex": "✦", "opencode-go": "◈"}
    ASCII_ICONS = {"claude": "C", "codex": "X", "opencode-go": "O"}
    CLAUDE = {"status": "ok", "windows": [
        {"label": "5h", "percent": 3.0, "reset_at": 1793986200.457986},
        {"label": "7d", "percent": 3.0, "reset_at": 1794564000.458007},
    ]}
    CODEX = {"status": "ok", "windows": [
        {"label": "5h", "percent": 7.0, "reset_at": 1793987009.0},
        {"label": "7d", "percent": 22.0, "reset_at": 1794506129.0},
    ]}
    GO = {"status": "ok", "windows": [
        {"label": "5h", "percent": 0.0, "reset_at": 1793986584.0},
        {"label": "7d", "percent": 1.0, "reset_at": 1794182400.0},
        {"label": "mo", "percent": 0.0, "reset_at": 1796503387.0},
    ]}

    @staticmethod
    def _plain(text):
        return re.sub(r"#\[[^\]]*\]", "", text)

    def _render(self, states, layout="full", *, unicode_mode=True, max_width=None,
                hide_inactive=True, providers=None, claude=None, codex=None, go=None):
        providers = self.ALL if providers is None else providers
        return quota.render_status(
            states,
            self.CLAUDE if claude is None else claude,
            self.CODEX if codex is None else codex,
            self.NOW,
            unicode_mode,
            providers,
            self.GO if go is None else go,
            layout,
            max_width,
            hide_inactive,
        )

    def _activity_states(self):
        for bits in itertools.product((True, False), repeat=3):
            yield dict(zip(self.ALL, bits))

    def test_only_running_providers_are_rendered_in_every_layout(self):
        budgets = {"auto-wide": 400, "auto-tight": 40}
        for states, unicode_mode in itertools.product(self._activity_states(), (True, False)):
            active = [name for name in self.ALL if states[name]]
            for layout, budget in (("full", None), ("compact", None), ("minimal", None),
                                   ("auto", budgets["auto-wide"]), ("auto", budgets["auto-tight"])):
                label = f"{layout}-{budget}" if budget is not None else layout
                with self.subTest(activity=states, layout=label, unicode=unicode_mode):
                    text = self._render(states, layout, unicode_mode=unicode_mode,
                                        max_width=budget)
                    visible = self._plain(text)
                    for name in self.ALL:
                        icon = self.ICONS[name] if unicode_mode else self.ASCII_ICONS[name]
                        head = f"{icon} {self.NAMES[name]}:"
                        if states[name]:
                            self.assertIn(head, visible)
                        else:
                            self.assertNotIn(head, visible)
                            self.assertNotIn(self.NAMES[name], visible)
                    if not active:
                        self.assertEqual(text, "")
                        continue
                    # Rebuild the expected line from one-provider renders of the same layout.
                    chosen = layout
                    if layout == "auto":
                        chosen = next(
                            candidate for candidate in ("full", "compact", "minimal")
                            if self._render(states, candidate, unicode_mode=unicode_mode)
                            == text
                        )
                        self.assertIn(chosen, ("full", "compact", "minimal"))
                    parts = [
                        self._render(states, chosen, unicode_mode=unicode_mode,
                                     providers=(name,), claude=self.CLAUDE, codex=self.CODEX,
                                     go=self.GO)
                        for name in active
                    ]
                    separator = quota.layout_separator(chosen, unicode_mode)
                    self.assertEqual(text, separator.join(parts))
                    self.assertFalse(visible.startswith(separator.strip()))
                    self.assertFalse(visible.endswith(separator.strip()))
                    self.assertNotIn(separator.strip() + " " + separator.strip(), visible)

    def test_omitting_the_flag_matches_head_bytes(self):
        head_source = Path(tempfile.gettempdir()) / f"head-quota-hide-inactive-{os.getpid()}.py"
        head_source.write_text(subprocess.run(
            ["git", "show", "HEAD:tmux-quota.py"], cwd=str(SOURCE.parent),
            capture_output=True, text=True, check=True,
        ).stdout)
        try:
            spec = importlib.util.spec_from_file_location("head_quota_hide", head_source)
            head = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(head)
        finally:
            head_source.unlink(missing_ok=True)
        fixtures = [
            (self.CLAUDE, self.CODEX, self.GO),
            ({"status": "error", "windows": [{"label": "5h", "percent": 12}], "stale": True},
             {"status": "ok", "windows": [{"label": "1h", "percent": 85, "reset_at": 1793970000}]},
             {"status": "no_plan", "windows": []}),
            ({"status": "not_running"}, {"status": "syncing", "windows": []},
             {"status": "no_key", "windows": []}),
        ]
        layouts = [("full", None), ("compact", None), ("minimal", None),
                   ("auto", 400), ("auto", 40), ("auto", None)]
        checked = 0
        for states, unicode_mode, (claude, codex, go) in itertools.product(
            self._activity_states(), (True, False), fixtures
        ):
            for layout, budget in layouts:
                with self.subTest(activity=states, layout=layout, budget=budget,
                                  unicode=unicode_mode):
                    new_text = quota.render_status(
                        states, claude, codex, self.NOW, unicode_mode, self.ALL, go, layout, budget
                    )
                    old_text = head.render_status(
                        states, claude, codex, self.NOW, unicode_mode, self.ALL, go, layout, budget
                    )
                    self.assertEqual(new_text, old_text)
                    checked += 1
        self.assertEqual(checked, 8 * 2 * 3 * 6)

    def test_active_error_states_stay_visible_with_the_flag(self):
        states = {"claude": True, "codex": True, "opencode-go": True}
        shown = {
            "no_key": {"status": "no_key", "windows": []},
            "syncing": {"status": "syncing", "windows": []},
            "rate_limited": {"status": "rate_limited", "windows": []},
            "unavailable": {"status": "error", "windows": []},
            "no_plan": {"status": "no_plan", "windows": []},
            "stale": {"status": "error", "stale": True,
                      "windows": [{"label": "5h", "percent": 12, "reset_at": 1794000000}]},
        }
        full_text = {
            "no_key": "no key", "syncing": "syncing", "rate_limited": "syncing",
            "unavailable": "unavailable", "no_plan": "no Go plan", "stale": "stale",
        }
        dense_text = {
            "no_key": "no key", "syncing": "…", "rate_limited": "…",
            "unavailable": "?", "no_plan": "no plan", "stale": "~",
        }
        for name, data in shown.items():
            for layout in ("full", "compact", "minimal"):
                with self.subTest(state=name, layout=layout):
                    text = self._render(states, layout, claude=data, codex={}, go={},
                                        providers=("claude",))
                    expected = dense_text[name] if layout != "full" else full_text[name]
                    self.assertNotEqual(text, "")
                    self.assertIn("Claude:", self._plain(text))
                    self.assertIn(expected, self._plain(text))
        ascii_states = self._render(states, "compact", unicode_mode=False,
                                    claude=shown["no_key"], codex=shown["no_key"],
                                    go=shown["no_key"])
        self.assertEqual(self._plain(ascii_states),
                         "C Claude: no key  |  X Codex: no key  |  O OpenCode Go: no key")

    def test_inactive_providers_fetch_nothing_and_read_no_credentials(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(quota, "process_states", return_value={
                 "claude": False, "codex": False, "opencode-go": False,
             }), \
             mock.patch.object(quota, "cache_dir", return_value=Path(directory)), \
             mock.patch.object(quota, "fetch_claude") as fetch_claude, \
             mock.patch.object(quota, "fetch_codex") as fetch_codex, \
             mock.patch.object(quota, "fetch_opencode_go") as fetch_opencode_go, \
             mock.patch.object(quota, "read_claude_token") as read_claude_token, \
             mock.patch.object(quota.subprocess, "run") as run, \
             mock.patch.object(quota.urllib.request, "urlopen") as urlopen, \
             mock.patch.object(quota.time, "time", return_value=1):
            text = quota.run_status(
                True, self.ALL, "full", None, "secret-tool get opencode-go", True
            )
            leftovers = {path.name for path in Path(directory).iterdir()}
        self.assertEqual(text, "")
        fetch_claude.assert_not_called()
        fetch_codex.assert_not_called()
        fetch_opencode_go.assert_not_called()
        read_claude_token.assert_not_called()
        run.assert_not_called()
        urlopen.assert_not_called()
        self.assertEqual(leftovers, set())

    def test_all_inactive_leaves_an_empty_status_line(self):
        states = {"claude": False, "codex": False, "opencode-go": False}
        for layout in ("full", "compact", "minimal", "auto"):
            for unicode_mode in (True, False):
                with self.subTest(layout=layout, unicode=unicode_mode):
                    self.assertEqual(
                        self._render(states, layout, unicode_mode=unicode_mode, max_width=80), ""
                    )
        partly = {"claude": False, "codex": True, "opencode-go": False}
        self.assertEqual(self._plain(self._render(partly, "minimal")), "✦ Codex: 22%")

    def test_hidden_providers_do_not_inflate_auto_measurements(self):
        all_active = {"claude": True, "codex": True, "opencode-go": True}
        wide_width = quota.visible_width(
            self._render(all_active, "full", hide_inactive=False)
        )
        compact_width = quota.visible_width(
            self._render(all_active, "compact", hide_inactive=False)
        )
        only_go = {"claude": False, "codex": False, "opencode-go": True}
        go_full = self._render(only_go, "full")
        go_compact = self._render(only_go, "compact")
        go_minimal = self._render(only_go, "minimal")
        compact_width_go = quota.visible_width(go_compact)
        self.assertEqual(quota.visible_width(go_full), 91)
        self.assertEqual(compact_width_go, 44)
        self.assertEqual(quota.visible_width(go_minimal), 17)
        # Only the surviving provider is measured, so a small budget keeps it visible.
        self.assertEqual(self._render(only_go, "auto", max_width=91), go_full)
        self.assertEqual(self._render(only_go, "auto", max_width=compact_width_go), go_compact)
        self.assertEqual(
            self._render(only_go, "auto", max_width=compact_width_go - 1), go_minimal
        )
        self.assertLess(compact_width, wide_width)

    def test_cli_and_generator_carry_the_flag(self):
        command = quota.tmux_command(
            SOURCE, use_unicode=True, providers=self.ALL,
            layout="compact", max_width=None, key_command=None, hide_inactive=True,
        )
        self.assertIn("--layout compact", command)
        self.assertTrue(command.endswith("--hide-inactive)"))
        self.assertNotIn("--hide-inactive", quota.tmux_command(SOURCE))
        round_trip = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command", "--unicode", "--layout", "compact",
             "--providers", "claude,codex,opencode-go", "--hide-inactive"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(round_trip.stdout.strip(), command)
        argv = shlex.split(round_trip.stdout.strip().removeprefix("#(").removesuffix(")"))
        self.assertEqual(argv[0], "python3")
        self.assertIn("--hide-inactive", argv)
        with mock.patch.object(quota, "run_status", return_value="STATUS") as run_status, \
             mock.patch.object(sys, "argv", argv[1:]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(quota.main(), 0)
        run_status.assert_called_once_with(
            True, self.ALL, "compact", None, None, True
        )
        with mock.patch.object(quota, "run_status", return_value="STATUS") as default_run, \
             mock.patch.object(sys, "argv", [str(SOURCE)]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(quota.main(), 0)
        default_run.assert_called_once_with(False)


class LayoutTests(unittest.TestCase):
    """Dense layouts: compact, minimal, and width-driven auto selection."""

    NOW = 1793969150
    ALL = ("claude", "codex", "opencode-go")
    # Records captured from the live cache, rendered with a fixed clock.
    CLAUDE = {"status": "ok", "windows": [
        {"label": "5h", "percent": 3.0, "reset_at": 1793986200.457986},
        {"label": "7d", "percent": 3.0, "reset_at": 1794564000.458007},
    ]}
    CODEX = {"status": "ok", "windows": [
        {"label": "5h", "percent": 7.0, "reset_at": 1793987009.0},
        {"label": "7d", "percent": 22.0, "reset_at": 1794506129.0},
    ]}
    GO = {"status": "ok", "windows": [
        {"label": "5h", "percent": 0.0, "reset_at": 1793986584.0},
        {"label": "7d", "percent": 1.0, "reset_at": 1794182400.0},
        {"label": "mo", "percent": 0.0, "reset_at": 1796503387.0},
    ]}
    SAMPLE_CLAUDE = {"status": "ok", "windows": [
        {"label": "5h", "percent": 2.0, "reset_at": 1793986750},
        {"label": "7d", "percent": 2.0, "reset_at": 1794564000},
    ]}
    SAMPLE_CODEX = {"status": "ok", "windows": [
        {"label": "5h", "percent": 47.0, "reset_at": 1793970950},
        {"label": "7d", "percent": 20.0, "reset_at": 1794506129},
    ]}

    @staticmethod
    def _plain(text):
        return re.sub(r"#\[[^\]]*\]", "", text)

    def _render(self, layout="full", *, unicode=True, provider_set=None, max_width=None,
                claude=None, codex=None, go=None):
        provider_set = self.ALL if provider_set is None else provider_set
        states = {name: True for name in provider_set}
        return quota.render_status(
            states,
            self.CLAUDE if claude is None else claude,
            self.CODEX if codex is None else codex,
            self.NOW,
            unicode,
            provider_set,
            self.GO if go is None else go,
            layout,
            max_width,
        )

    def test_omitted_layout_stays_byte_identical_to_full(self):
        fixtures = [
            (self.CLAUDE, self.CODEX, self.GO),
            ({"status": "ok", "windows": [{"label": "5h", "percent": 42, "reset_at": 1794000000}]},
             {"status": "ok", "windows": [{"label": "1h", "percent": 85, "reset_at": 1793970000},
                                          {"label": "30d", "percent": 79, "reset_at": 1799000000}]},
             {"status": "ok", "windows": [{"label": "5h", "percent": 12.6, "reset_at": 1793970000}]}),
            ({"status": "error", "windows": [{"label": "5h", "percent": 12}], "stale": True},
             {"status": "ok", "windows": [{"label": "5h", "percent": 0}], "display_state": "stale"},
             {"status": "error", "windows": [], "stale": True}),
            ({"status": "syncing", "windows": []}, {"status": "not_running"},
             {"status": "no_plan", "windows": []}),
            ({"status": "ok", "windows": [{"label": "5h", "percent": 21, "reset_at": 1793970000,
                                           "limited": True}]},
             {"status": "ok", "windows": [{"label": "5h", "percent": 100, "reset_at": 1793970000}]},
             {"status": "no_key", "windows": []}),
        ]
        provider_sets = [("claude", "codex"), ("claude",), ("codex", "opencode-go"), self.ALL]
        checked = 0
        for unicode_mode in (False, True):
            for claude, codex, go in fixtures:
                for provider_set in provider_sets:
                    states = {name: True for name in provider_set}
                    omitted = quota.render_status(
                        states, claude, codex, self.NOW, unicode_mode, provider_set, go
                    )
                    explicit = quota.render_status(
                        states, claude, codex, self.NOW, unicode_mode, provider_set, go, "full"
                    )
                    with_budget = quota.render_status(
                        states, claude, codex, self.NOW, unicode_mode, provider_set, go, "full", 12
                    )
                    self.assertEqual(omitted, explicit)
                    self.assertEqual(omitted, with_budget)
                    checked += 1
        self.assertEqual(checked, 40)

    def test_compact_sample_keeps_provider_names(self):
        text = self._render("compact", claude=self.SAMPLE_CLAUDE, codex=self.SAMPLE_CODEX)
        self.assertEqual(
            self._plain(text),
            "⚡ Claude: 5h ▁ 2% 4h53m · 7d 2%  │  ✦ Codex: 5h ▄ 47% 30m · 7d 20%"
            "  │  ◈ OpenCode Go: 5h ▁ 0% 4h50m · 7d 1% · mo 0%",
        )
        self.assertEqual(quota.visible_width(text), 116)
        for icon, name in (("⚡", "Claude:"), ("✦", "Codex:"), ("◈", "OpenCode Go:")):
            self.assertIn(f"#[fg=#c0caf5]{icon} {name}#[default]", text)
        minimal = self._render("minimal", claude=self.SAMPLE_CLAUDE, codex=self.SAMPLE_CODEX)
        self.assertEqual(
            self._plain(minimal), "⚡ Claude: 2%  │  ✦ Codex: 47%  │  ◈ OpenCode Go: 1%"
        )
        self.assertEqual(quota.visible_width(minimal), 52)

    def test_compact_never_silently_falls_back_to_full(self):
        compact = self._render("compact")
        full = self._render("full")
        # Both layouts name providers now, so compare structure and width, not names.
        for name in ("Claude:", "Codex:", "OpenCode Go:"):
            self.assertIn(name, compact)
            self.assertIn(name, full)
        visible = self._plain(compact)
        self.assertNotIn("░", visible)
        self.assertNotIn("█", visible)
        self.assertIsNone(re.search(r"\[[#-]{8}\]", visible))
        self.assertNotIn("(", visible)
        self.assertNotIn(")", visible)
        compact_width = quota.visible_width(compact)
        full_width = quota.visible_width(full)
        self.assertGreater(full_width - compact_width, 80)
        self.assertNotEqual(self._plain(compact), self._plain(full))
        self.assertNotEqual(compact, full)

    def test_compact_ascii_uses_identifiers_and_no_gauge(self):
        text = self._render("compact", unicode=False, claude=self.SAMPLE_CLAUDE,
                            codex=self.SAMPLE_CODEX)
        self.assertEqual(
            self._plain(text),
            "C Claude: 5h 2% 4h53m / 7d 2%  |  X Codex: 5h 47% 30m / 7d 20%"
            "  |  O OpenCode Go: 5h 0% 4h50m / 7d 1% / mo 0%",
        )
        for glyph in quota.GAUGE_GLYPHS:
            self.assertNotIn(glyph, text)
        self.assertNotIn("│", text)

    def test_compact_window_visibility_rules_and_colors(self):
        claude = {"status": "ok", "windows": [
            {"label": "5h", "percent": 10.0, "reset_at": 1793986750},
            {"label": "7d", "percent": 79.0, "reset_at": 1794564000},
        ]}
        codex = {"status": "ok", "windows": [
            {"label": "1h", "percent": 85.0, "reset_at": 1793970950},
            {"label": "30d", "percent": 20.0, "reset_at": 1794506129},
        ]}
        text = self._render("compact", provider_set=("claude", "codex"), claude=claude, codex=codex)
        plain = self._plain(text)
        self.assertIn("5h ▁ 10% 4h53m", plain)
        self.assertIn("7d 79%", plain)
        self.assertNotIn("7d 79% 58d", plain)
        self.assertIn("1h ▇ 85% 30m", plain)
        self.assertIn("30d 20%", plain)
        for fragment in (
            "#[fg=#9ece6a]▁#[default] #[fg=#9ece6a]10%#[default]",
            "#[fg=#e0af68]79%#[default]",
            "#[fg=#f7768e]▇#[default] #[fg=#f7768e]85%#[default]",
        ):
            self.assertIn(fragment, text)
        stale_free = {"status": "ok", "windows": [
            {"label": "5h", "percent": 21.0, "reset_at": 1793986750, "limited": True},
            {"label": "7d", "percent": 20.0, "reset_at": 1794564000},
        ]}
        limited = self._render("compact", provider_set=("claude",), claude=stale_free, codex={}, go={})
        self.assertIn("#[fg=#f7768e]21%!#[default]", limited)
        self.assertEqual(self._plain(limited), "⚡ Claude: 5h ▂ 21%! 4h53m · 7d 20%")

    def test_short_windows_are_chosen_by_label_not_position(self):
        self.assertTrue(quota.is_short_window("5h"))
        self.assertTrue(quota.is_short_window("45m"))
        self.assertFalse(quota.is_short_window("7d"))
        self.assertFalse(quota.is_short_window("1d"))
        self.assertFalse(quota.is_short_window("mo"))
        self.assertFalse(quota.is_short_window("usage"))
        self.assertFalse(quota.is_short_window("7d opus"))
        self.assertFalse(quota.is_short_window("now"))
        self.assertFalse(quota.is_short_window(""))
        weekly_only = {"status": "ok", "windows": [
            {"label": "7d", "percent": 10.0, "reset_at": 1794564000},
        ]}
        monthly_only = {"status": "ok", "windows": [
            {"label": "mo", "percent": 10.0, "reset_at": 1796503387},
        ]}
        for label, data, expected in (
            ("weekly only", weekly_only, "◈ OpenCode Go: 7d 10%"),
            ("monthly only", monthly_only, "◈ OpenCode Go: mo 10%"),
        ):
            with self.subTest(case=label):
                text = self._render("compact", provider_set=("opencode-go",), claude={},
                                    codex={}, go=data)
                self.assertEqual(self._plain(text), expected)
                for glyph in quota.GAUGE_GLYPHS:
                    self.assertNotIn(glyph, text)
        mixed = {"status": "ok", "windows": [
            {"label": "1h", "percent": 10.0, "reset_at": 1793970950},
            {"label": "7d", "percent": 20.0, "reset_at": 1794506129},
        ]}
        text = self._render("compact", provider_set=("codex",), claude={}, codex=mixed, go={})
        self.assertEqual(self._plain(text), "✦ Codex: 1h ▁ 10% 30m · 7d 20%")
        self.assertEqual(text.count("▁"), 1)
        suffixed = {"status": "ok", "windows": [
            {"label": "7d opus", "percent": 10.0, "reset_at": 1794564000},
        ]}
        text = self._render("compact", provider_set=("claude",), claude=suffixed, codex={}, go={})
        self.assertEqual(self._plain(text), "⚡ Claude: 7d opus 10%")
        for glyph in quota.GAUGE_GLYPHS:
            self.assertNotIn(glyph, text)
        long_at_85 = {"status": "ok", "windows": [
            {"label": "7d", "percent": 85.0, "reset_at": 1794564000},
        ]}
        text = self._render("compact", provider_set=("claude",), claude=long_at_85, codex={}, go={})
        self.assertEqual(self._plain(text), "⚡ Claude: 7d 85% 6d21h")

    def test_short_window_gauge_and_countdown_survive_a_long_first_window(self):
        windows = {"status": "ok", "windows": [
            {"label": "7d", "percent": 20.0, "reset_at": 1794564000},
            {"label": "5h", "percent": 10.0, "reset_at": 1793970950},
        ]}
        text = self._render("compact", provider_set=("claude",), claude=windows, codex={}, go={})
        self.assertEqual(self._plain(text), "⚡ Claude: 7d 20% · 5h ▁ 10% 30m")
        self.assertEqual(text.count("▁"), 1)

    def test_dense_ascii_output_stays_ascii_only(self):
        markers = {
            "healthy": {"status": "ok", "windows": [
                {"label": "5h", "percent": 12.0, "reset_at": 1793970950},
                {"label": "7d", "percent": 93.0, "reset_at": 1794564000},
            ]},
            "limited": {"status": "ok", "windows": [
                {"label": "5h", "percent": 12.0, "reset_at": 1793970950, "limited": True},
            ]},
            "stale": {"status": "error", "stale": True, "windows": [
                {"label": "5h", "percent": 12.0, "reset_at": 1793970950},
            ]},
            "stale-display": {"status": "ok", "display_state": "stale", "windows": [
                {"label": "5h", "percent": 12.0},
            ]},
            "not_running": {"status": "not_running"},
            "syncing": {"status": "syncing", "windows": []},
            "rate_limited": {"status": "rate_limited", "windows": []},
            "no_plan": {"status": "no_plan", "windows": []},
            "no_key": {"status": "no_key", "windows": []},
            "unavailable": {"status": "error", "windows": []},
        }
        for layout in ("compact", "minimal"):
            for name, data in markers.items():
                with self.subTest(layout=layout, case=name):
                    for provider_set, claude, codex, go in (
                        (("claude",), data, {}, {}),
                        (self.ALL, data, data, data),
                    ):
                        text = self._render(layout, unicode=False, provider_set=provider_set,
                                            claude=claude, codex=codex, go=go)
                        visible = self._plain(text)
                        self.assertTrue(visible.isascii(), visible)
                        self.assertNotIn("…", visible)
                        self.assertNotIn("·", visible)
                        self.assertNotIn("│", visible)
                        self.assertTrue(text.endswith("#[default]"))
        self.assertEqual(self._plain(self._render("compact", unicode=False,
                                                 provider_set=("claude",),
                                                 claude={"status": "syncing", "windows": []},
                                                 codex={}, go={})), "C Claude: ...")
        self.assertEqual(self._plain(self._render("minimal", unicode=False,
                                                 provider_set=("claude",),
                                                 claude={"status": "rate_limited", "windows": []},
                                                 codex={}, go={})), "C Claude: ...")

    def test_gauge_level_boundaries(self):
        self.assertEqual(quota.gauge_cell(0), "▁")
        self.assertEqual(quota.gauge_cell(0.4), "▁")
        self.assertEqual(quota.gauge_cell(12.5), "▁")
        self.assertEqual(quota.gauge_cell(12.6), "▂")
        self.assertEqual(quota.gauge_cell(25), "▂")
        self.assertEqual(quota.gauge_cell(37.5), "▃")
        self.assertEqual(quota.gauge_cell(100), "█")
        self.assertEqual(quota.gauge_cell(150), "█")
        self.assertEqual(quota.gauge_cell(-25), "▁")
        self.assertEqual([quota.gauge_cell(level * 12.5) for level in range(1, 9)],
                         list("▁▂▃▄▅▆▇█"))

    def test_minimal_shows_highest_percent_and_flags_any_limited_window(self):
        claude = {"status": "ok", "windows": [
            {"label": "5h", "percent": 5.0, "reset_at": 1793986750, "limited": True},
            {"label": "7d", "percent": 95.0, "reset_at": 1794564000},
        ]}
        text = self._render("minimal", provider_set=("claude",), claude=claude, codex={}, go={})
        self.assertEqual(self._plain(text), "⚡ Claude: 95%!")
        self.assertEqual(text, "#[fg=#c0caf5]⚡ Claude:#[default] #[fg=#f7768e]95%!#[default]")
        healthy = {"status": "ok", "windows": [
            {"label": "5h", "percent": 5.0}, {"label": "7d", "percent": 95.0},
        ]}
        plain_healthy = self._render("minimal", provider_set=("claude",), claude=healthy,
                                     codex={}, go={})
        self.assertEqual(plain_healthy, "#[fg=#c0caf5]⚡ Claude:#[default] #[fg=#f7768e]95%#[default]")
        mid = {"status": "ok", "windows": [{"label": "5h", "percent": 42.0}]}
        self.assertEqual(
            self._render("minimal", provider_set=("claude",), claude=mid, codex={}, go={}),
            "#[fg=#c0caf5]⚡ Claude:#[default] #[fg=#9ece6a]42%#[default]",
        )

    def test_minimal_uses_highest_percent_without_countdown(self):
        text = self._render("minimal")
        self.assertEqual(self._plain(text), "⚡ Claude: 3%  │  ✦ Codex: 22%  │  ◈ OpenCode Go: 1%")
        self.assertIsNone(re.search(r"\d+[dh]\d*", self._plain(text)))
        ascii_text = self._render("minimal", unicode=False)
        self.assertEqual(self._plain(ascii_text), "C Claude: 3%  |  X Codex: 22%  |  O OpenCode Go: 1%")

    def test_state_markers_are_distinct_in_compact_and_minimal(self):
        markers = {
            "not_running": {"status": "not_running"},
            "syncing": {"status": "syncing", "windows": []},
            "rate_limited": {"status": "rate_limited", "windows": []},
            "no_plan": {"status": "no_plan", "windows": []},
            "no_key": {"status": "no_key", "windows": []},
            "unavailable": {"status": "error", "windows": []},
        }
        expected = {
            "not_running": "⚡ Claude:",
            "syncing": "⚡ Claude: …",
            "rate_limited": "⚡ Claude: …",
            "no_plan": "⚡ Claude: no plan",
            "no_key": "⚡ Claude: no key",
            "unavailable": "⚡ Claude: ?",
        }
        for layout in ("compact", "minimal"):
            seen = set()
            for name, data in markers.items():
                text = self._render(layout, provider_set=("claude",), claude=data, codex={}, go={})
                self.assertEqual(self._plain(text), expected[name], name)
                self.assertTrue(text.endswith("#[default]"))
                seen.add(self._plain(text))
            self.assertIn("⚡ Claude:", seen)
            self.assertEqual(len(seen), len(markers) - 1)
        dim_idle = self._render("compact", provider_set=("claude",),
                                claude={"status": "not_running"}, codex={}, go={})
        self.assertEqual(dim_idle, "#[fg=#565f89]⚡ Claude:#[default]")
        stale_compact = self._render(
            "compact", provider_set=("claude",),
            claude={"status": "error", "windows": [{"label": "5h", "percent": 12,
                                                    "reset_at": 1794000000}], "stale": True},
            codex={}, go={},
        )
        self.assertEqual(self._plain(stale_compact), "⚡ Claude:~ 5h ▁ 12% 8h34m")
        self.assertIn("#[fg=#e0af68]~#[default]", stale_compact)
        stale_minimal = self._render(
            "minimal", provider_set=("claude",),
            claude={"status": "error", "windows": [{"label": "5h", "percent": 12}], "display_state": "stale"},
            codex={}, go={},
        )
        self.assertEqual(self._plain(stale_minimal), "⚡ Claude:~ 12%")

    def test_auto_picks_the_first_layout_that_fits(self):
        full = self._render("full")
        compact = self._render("compact")
        minimal = self._render("minimal")
        full_width = quota.visible_width(full)
        compact_width = quota.visible_width(compact)
        minimal_width = quota.visible_width(minimal)
        self.assertGreater(full_width, compact_width + 100)
        self.assertGreater(compact_width, minimal_width)
        budgeted = {
            full_width + 10: full,
            full_width: full,
            full_width - 1: compact,
            compact_width: compact,
            compact_width - 1: minimal,
            minimal_width: minimal,
            minimal_width - 1: minimal,
            0: minimal,
        }
        for budget, expected in budgeted.items():
            self.assertEqual(self._render("auto", max_width=budget), expected, budget)
        self.assertEqual(self._render("auto", max_width=400), full)
        self.assertEqual(self._render("auto"), compact)

    def test_max_width_is_ignored_without_the_auto_layout(self):
        for layout in ("full", "compact", "minimal"):
            self.assertEqual(self._render(layout, max_width=1), self._render(layout))
        self.assertEqual(self._render("compact", max_width=1000), self._render("compact"))

    def test_visible_width_measures_tmux_cells(self):
        self.assertEqual(quota.visible_width("#[fg=#c0caf5]ab#[default]"), 2)
        self.assertEqual(quota.visible_width("⚡"), 2)
        self.assertEqual(quota.visible_width("✦◈│·▁█%"), 7)
        self.assertEqual(quota.visible_width(""), 0)
        self.assertEqual(quota.visible_width(self._render("minimal")), 52)

    def test_dense_layouts_reset_styles_and_never_emit_escapes(self):
        for layout in ("compact", "minimal"):
            for unicode_mode in (False, True):
                text = self._render(layout, unicode=unicode_mode)
                self.assertNotIn("\033[", text)
                self.assertTrue(text.endswith("#[default]"))
                self.assertGreaterEqual(text.count("#[default]"), text.count("#[fg="))

    def test_tmux_command_and_cli_pass_layout_and_max_width(self):
        command = quota.tmux_command(
            Path("/tmp/path with spaces/tmux-quota.py"), use_unicode=True,
            providers=self.ALL, layout="auto", max_width=60,
        )
        self.assertEqual(
            command,
            "#(python3 " + shlex.quote(str(Path("/tmp/path with spaces/tmux-quota.py").expanduser())) +
            " --unicode --layout auto --max-width 60 --providers claude,codex,opencode-go)",
        )
        self.assertEqual(command, quota.tmux_command(
            Path("/tmp/path with spaces/tmux-quota.py"), True, self.ALL, "auto", 60))
        run = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command", "--unicode", "--layout", "auto",
             "--max-width", "60", "--providers", "claude,codex,opencode-go"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(run.stdout.strip(), quota.tmux_command(SOURCE, True, self.ALL, "auto", 60))
        default = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command"], check=True, capture_output=True,
            text=True, timeout=5,
        )
        self.assertNotIn("--layout", default.stdout)
        self.assertNotIn("--max-width", default.stdout)
        compact_only = subprocess.run(
            [sys.executable, str(SOURCE), "--tmux-command", "--layout", "compact"],
            check=True, capture_output=True, text=True, timeout=5,
        )
        self.assertTrue(compact_only.stdout.strip().endswith(" --layout compact)"))
        rejected = subprocess.run(
            [sys.executable, str(SOURCE), "--layout", "dense"], capture_output=True, text=True,
            timeout=5,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("--layout", rejected.stderr)

    def test_cli_runs_selected_layout_without_touching_the_default_call(self):
        output = io.StringIO()
        with mock.patch.object(quota, "run_status", return_value="STATUS") as run_status, \
             mock.patch.object(sys, "argv", [str(SOURCE), "--unicode"]), \
             contextlib.redirect_stdout(output):
            self.assertEqual(quota.main(), 0)
        run_status.assert_called_once_with(True)
        with mock.patch.object(quota, "run_status", return_value="STATUS") as run_status, \
             mock.patch.object(sys, "argv", [str(SOURCE), "--layout", "compact"]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(quota.main(), 0)
        run_status.assert_called_once_with(False, quota.DEFAULT_PROVIDERS, "compact", None, None, False)
        with mock.patch.object(quota, "run_status", return_value="STATUS") as run_status, \
             mock.patch.object(sys, "argv", [str(SOURCE), "--layout", "auto", "--max-width", "60"]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(quota.main(), 0)
        run_status.assert_called_once_with(False, quota.DEFAULT_PROVIDERS, "auto", 60, None, False)


if __name__ == "__main__":
    unittest.main()