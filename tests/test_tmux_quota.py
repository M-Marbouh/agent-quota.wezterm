import contextlib
import importlib.util
import io
import json
import os
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


if __name__ == "__main__":
    unittest.main()
