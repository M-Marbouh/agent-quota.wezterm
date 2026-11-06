"""WezTerm plugin tests: legacy byte equality, dense layouts and the Go bridge.

The plugin is Lua, so these tests need a Lua runtime embedded in Python.  They
run under the system Python when ``lupa`` is importable (for example
``/tmp/quota-lua-venv/bin/python -m unittest tests.test_wezterm_plugin``) and
skip cleanly otherwise, which keeps the default ``unittest discover`` run free
of extra dependencies.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

try:
    import lupa
except ImportError:  # pragma: no cover - the harness skips without a Lua runtime
    lupa = None

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin" / "init.lua"
CODEX_HELPER = REPO / "codex-limits.py"
TMUX_HELPER = REPO / "tmux-quota.py"
BASELINE_COMMIT = "ac611fb"
NOW = 1793969150

SPEC = importlib.util.spec_from_file_location("tmux_quota_plugin_oracle", TMUX_HELPER)
quota = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(quota)

def iso_at(offset_secs):
    """An API-shaped ISO-8601 reset timestamp, relative to the harness clock."""
    import datetime

    return datetime.datetime.fromtimestamp(
        NOW + offset_secs, datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")


GO_FIXTURE = {
    "usage": {
        "rolling": {"status": "ok", "percent": 0, "resetsAt": iso_at(9840)},
        "weekly": {"status": "ok", "percent": 1, "resetsAt": iso_at(201_500)},
        "monthly": {"status": "ok", "percent": 0, "resetsAt": iso_at(2_500_000)},
    }
}
GO_KEY = "opencode-go-lua-harness-key-0f3a"

CLAUDE_OK = {
    "five_hour": {"utilization": 3, "resets_at": "2026-11-06T02:56:29.000Z"},
    "seven_day": {"utilization": 3, "resets_at": "2026-11-09T00:00:00.000Z"},
}
CLAUDE_FIXTURES = {
    "ok": CLAUDE_OK,
    "zero": {
        "five_hour": {"utilization": 0, "resets_at": "2026-11-06T02:56:29.000Z"},
        "seven_day": {"utilization": 0.4, "resets_at": "2026-11-09T00:00:00.000Z"},
    },
    "not_running": {"not_running": True},
    "syncing": {"syncing": True},
    "rate_limited": {"error": "rate limited (retry in 5m)"},
    "error": {"error": "auth failed — waiting for Claude Code"},
}

CODEX_OK = {
    "primary_pct": 7,
    "primary_reset": "30m",
    "primary_reset_at": NOW + 1800,
    "secondary_pct": 22,
    "secondary_reset": "4h0m",
    "secondary_reset_at": NOW + 14400,
    "primary_mins": 300,
    "secondary_mins": 10080,
}
CODEX_FIXTURES = {
    "ok": CODEX_OK,
    "primary_only": {
        "primary_pct": 47,
        "primary_reset": "1h2m",
        "primary_reset_at": NOW + 3720,
        "primary_mins": 60,
    },
    "error": {"error": "codex helper failed"},
    "syncing": {"syncing": True},
    "ready": {"ready": True},
    "empty": {},
    "app_server_not_running": {"error": "not running"},
}


def plain(text):
    """The visible text: ANSI colour escapes (WezTerm) and #[...] styles (tmux) removed."""
    return re.sub(r"#\[[^\]]*\]", "", re.sub(r"\x1b\[[0-9;]*m", "", text))


def to_lua(lua, value):
    """A real Lua table for nested Python data (lupa wraps Python dicts as userdata)."""
    if isinstance(value, dict):
        table = lua.table()
        for key, item in value.items():
            table[key] = to_lua(lua, item)
        return table
    if isinstance(value, (list, tuple)):
        table = lua.table()
        for index, item in enumerate(value, 1):
            table[index] = to_lua(lua, item)
        return table
    return value


@unittest.skipUnless(lupa is not None, "lupa is not installed; see this file's docstring")
class WezTermPluginTests(unittest.TestCase):
    ALL = ("claude", "codex", "opencode-go")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wq-lua-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "home").mkdir()
        (self.tmp / "win-temp").mkdir()
        self.user = "wqtest" + uuid.uuid4().hex[:10]
        environment = {
            "HOME": str(self.tmp / "home"),
            "USERPROFILE": str(self.tmp / "home"),
            "USER": self.user,
            # The plugin's Windows cache prefix comes from TEMP/TMP; keep it inside the
            # per-test directory so the Windows-target cases write no stray files.
            "TEMP": str(self.tmp / "win-temp"),
            "TMP": str(self.tmp / "win-temp"),
            "WEZTERM_AGENT_QUOTA_TMUX_HELPER": str(TMUX_HELPER),
        }
        patcher = mock.patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.now = NOW
        self.calls = []
        self.log_errors = []
        self.codex_running = True
        self.codex_stdout = ""
        self.go_stdout = ""
        self.go_ok = True
        self.codex_cache = Path("/tmp") / f"wezterm-quota-limit-{self.user}-codex.json"
        self.addCleanup(self._cleanup_cache)

    # -- infrastructure -------------------------------------------------

    def _cleanup_cache(self):
        self.codex_cache.unlink(missing_ok=True)
        lock = self.codex_cache.with_name(self.codex_cache.name + ".lock")
        if lock.is_dir():
            lock.rmdir()

    def _run_child(self, argv):
        args = [argv[index] for index in range(1, len(argv) + 1)]
        args = [argument if isinstance(argument, str) else str(argument) for argument in args]
        self.calls.append(args)
        joined = " ".join(args)
        program = os.path.basename(args[0]) if args else ""
        if program in ("ps", "pgrep"):
            return (True, "codex   pts/1   codex\n" if self.codex_running else "", "")
        if program == "tasklist":
            return (True, '"codex.exe","1234","Console","1","10 K"\n' if self.codex_running else "", "")
        if "tmux-quota.py" in joined:
            return (self.go_ok, self.go_stdout, "" if self.go_ok else "go helper failed")
        if "codex-limits.py" in joined:
            return (True, self.codex_stdout, "")
        return (True, "", "")

    def load(self, source=PLUGIN, *, opts=None, windows=False):
        runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
        lua = runtime
        triple = "x86_64-pc-windows-msvc" if windows else "x86_64-unknown-linux-gnu"

        stubs = lua.table()
        stubs["target_triple"] = triple
        action = lua.table()
        action["EmitEvent"] = lambda name: to_lua(lua, {"EmitEvent": name})
        stubs["action"] = action
        stubs["on"] = lambda *args: None
        stubs["open_with"] = lambda *args: None
        stubs["log_info"] = lambda *args: None
        stubs["log_error"] = lambda message: self.log_errors.append(str(message))
        stubs["run_child_process"] = self._run_child
        stubs["json_parse"] = lambda raw: to_lua(lua, json.loads(raw))
        lua.globals().wezterm = stubs
        lua.execute("package.loaded['wezterm'] = wezterm")
        lua.execute("WEZTERM_QUOTA_TEST = true")
        lua.globals().FAKE_NOW = self.now
        lua.execute("os.time = function() return FAKE_NOW end")

        module = lua.eval("dofile")(str(source))
        module["apply_to_config"](lua.table(), to_lua(lua, opts or {}))
        self.calls.clear()
        return lua, module

    def build(self, lua, module, claude, *, now=None):
        if now is not None:
            lua.globals().FAKE_NOW = now
        return module["_test"]["build_status_string"](to_lua(lua, claude), None, None)

    def prime_codex(self, data, *, next_refresh_at=None):
        entry = {
            "written_at": self.now,
            "next_refresh_at": self.now + 3600 if next_refresh_at is None else next_refresh_at,
            "error_count": 0,
            "data": data,
        }
        self.codex_cache.write_text(json.dumps(entry), encoding="utf-8")

    def baseline_source(self):
        shown = subprocess.run(
            ["git", "show", f"{BASELINE_COMMIT}:plugin/init.lua"],
            cwd=str(REPO), capture_output=True, text=True, check=False,
        )
        if shown.returncode != 0 or not shown.stdout.strip():
            self.skipTest(f"baseline commit {BASELINE_COMMIT} is not available here")
        text = shown.stdout.rstrip("\n")
        self.assertTrue(text.endswith("return M"), "baseline plugin has no trailing return M")
        text = text[: -len("return M")] + (
            "M._test = { build_status_string = build_status_string }\nreturn M"
        )
        path = self.tmp / "baseline-init.lua"
        path.write_text(text + "\n", encoding="utf-8")
        return path

    def go_stdout_for(self, data):
        """The real --json output for one raw opencode-go provider entry."""
        selected = ("opencode-go",)
        with mock.patch.object(
            quota, "collect_status", return_value=(selected, {"opencode-go": True}, {"opencode-go": data})
        ):
            report = quota.json_report(("opencode-go",), None)
        return json.dumps(report, separators=(",", ":"))

    def real_go_stdout(self):
        """Run the actual tmux-quota.py --json in a subprocess with only the network stubbed."""
        body = json.dumps(GO_FIXTURE).encode()
        home = str(self.tmp / "oracle-home")
        Path(home).mkdir(exist_ok=True)
        runner = f"""
import json, os, runpy, subprocess, sys, time, urllib.request
from unittest import mock

BODY = {body!r}
real_run = subprocess.run

class FakePS:
    returncode = 0
    stdout = "pi   pts/1   pi\\n"

def fake_run(argv, *args, **kwargs):
    if argv and argv[0] == "ps":
        return FakePS()
    return real_run(argv, *args, **kwargs)

class Response:
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return BODY

sys.argv = ["tmux-quota.py", "--json", "--providers", "opencode-go"]
with mock.patch.object(subprocess, "run", fake_run), \\
     mock.patch.object(urllib.request, "urlopen", return_value=Response()), \\
     mock.patch.object(time, "time", return_value={NOW!r}), \\
     mock.patch.dict(os.environ, {{"OPENCODE_API_KEY": {GO_KEY!r}, "HOME": {home!r}}}):
    runpy.run_path({str(TMUX_HELPER)!r}, run_name="__main__")
"""
        completed = subprocess.run(
            [sys.executable, "-c", runner],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "TMUX_QUOTA_CACHE_DIR": str(self.tmp / "oracle-cache")},
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["providers"][0]["state"], "ok")
        self.assertEqual(len(report["providers"][0]["windows"]), 3)
        return completed.stdout.strip()

    def mutated_plugin(self, replacements):
        """A copy of the plugin with exact source substitutions applied."""
        source = PLUGIN.read_text(encoding="utf-8")
        for old, new in replacements:
            self.assertIn(old, source)
            source = source.replace(old, new)
        path = self.tmp / f"mutated-{uuid.uuid4().hex[:8]}.lua"
        path.write_text(source, encoding="utf-8")
        return path

    def spawns(self, needle="tmux-quota.py"):
        return [args for args in self.calls if needle in " ".join(args)]

    def opts(self, **options):
        merged = {"codex_script": str(CODEX_HELPER)}
        merged.update(options)
        return merged

    # -- legacy equality -------------------------------------------------

    def test_default_options_are_byte_identical_to_the_baseline(self):
        baseline = self.baseline_source()
        checked = 0
        for claude_label, claude in CLAUDE_FIXTURES.items():
            for codex_label, codex in CODEX_FIXTURES.items():
                for compact in (False, True):
                    for layout in (None, "full"):
                        new_lua, new_module = self.load(
                            opts=self.opts(compact=compact, **({"layout": layout} if layout else {}))
                        )
                        old_lua, old_module = self.load(
                            source=baseline, opts=self.opts(compact=compact)
                        )
                        self.prime_codex(codex)
                        new_text = self.build(new_lua, new_module, claude)
                        self.prime_codex(codex)
                        old_text = self.build(old_lua, old_module, claude)
                        label = (claude_label, codex_label, f"compact={compact}", f"layout={layout}")
                        self.assertEqual(new_text, old_text, label)
                        self.assertNotIn("OpenCode Go", new_text, label)
                        checked += 1
        self.assertEqual(checked, len(CLAUDE_FIXTURES) * len(CODEX_FIXTURES) * 2 * 2)

    def test_baseline_comparison_detects_a_changed_legacy_byte(self):
        """Control: the equality check above is not vacuous."""
        baseline = self.baseline_source()
        source = PLUGIN.read_text(encoding="utf-8")
        mutated = source.replace(
            'claude_str = DIM .. ci .. BRIGHT .. "Claude: " .. DIM .. "not running"',
            'claude_str = DIM .. ci .. BRIGHT .. "Claude: " .. DIM .. "not-running"',
        )
        self.assertNotEqual(source, mutated, "the mutation anchor is gone")
        changed = self.tmp / "mutated-init.lua"
        changed.write_text(mutated, encoding="utf-8")

        new_lua, new_module = self.load(source=changed)
        old_lua, old_module = self.load(source=baseline)
        self.prime_codex(CODEX_OK)
        self.assertNotEqual(
            self.build(new_lua, new_module, {"not_running": True}),
            self.build(old_lua, old_module, {"not_running": True}),
        )

    def test_legacy_compact_flag_still_hides_countdowns(self):
        lua, module = self.load(opts=self.opts(compact=True))
        self.prime_codex(CODEX_OK)
        text = self.build(lua, module, CLAUDE_OK)
        self.assertNotIn(" (", text)
        self.assertNotIn("(4h", text)

    # -- dense layouts ---------------------------------------------------

    def test_dense_layouts_render_go_from_the_real_json_output(self):
        self.go_stdout = self.real_go_stdout()
        parsed = json.loads(self.go_stdout)
        self.assertEqual(parsed["providers"][0]["state"], "ok")
        self.assertEqual(len(parsed["providers"][0]["windows"]), 3)
        for layout in ("compact", "minimal"):
            with self.subTest(layout=layout):
                self.now = NOW
                lua, module = self.load(opts=self.opts(
                    layout=layout, opencode_go={"enabled": True}
                ))
                self.prime_codex(CODEX_OK)
                text = self.build(lua, module, CLAUDE_OK)
                self.assertEqual(len(self.spawns()), 1)
                self.assertEqual(self.spawns()[0][:4],
                                 ["python3", str(TMUX_HELPER), "--json", "--providers"])
                visible = plain(text)
                self.assertIn("OpenCode Go:", visible)
                self.assertIn("1%", visible)
                self.assertIn("│", visible)
                self.assertNotIn("#[", text)
                self.assertNotIn(GO_KEY, text)
                if layout == "compact":
                    go_part = visible.split("OpenCode Go:")[1]
                    self.assertIn("5h", go_part)
                    self.assertIn("▁", go_part)  # a short window keeps its one-cell gauge
                    self.assertIn("2h44m", go_part)  # countdown without parentheses
                    self.assertNotIn("(2h44m)", go_part)
                    self.assertEqual(go_part.count(" · "), 2)
                else:
                    self.assertNotIn("▁", visible)
                    self.assertIn("1%", visible)  # highest window only
                    self.assertNotIn("5h", visible)
                    self.assertNotIn("7d", visible)

    def test_minimal_uses_highest_percent_and_flags_limited_windows(self):
        self.go_stdout = self.go_stdout_for({
            "status": "ok", "stale": True,
            "windows": [
                {"label": "5h", "percent": 4, "reset_at": NOW + 600, "limited": False},
                {"label": "mo", "percent": 81, "reset_at": NOW + 86_400, "limited": True},
            ],
        })
        lua, module = self.load(opts=self.opts(layout="minimal", opencode_go={"enabled": True}))
        self.prime_codex(CODEX_OK)
        text = plain(self.build(lua, module, {"not_running": True}))
        self.assertIn("OpenCode Go:~", text)
        self.assertIn("81%!", text)
        self.assertNotIn("4%", text)
        self.assertNotIn("5h", text)

    def test_full_layout_renders_go_in_the_legacy_style(self):
        self.go_stdout = self.go_stdout_for({
            "status": "ok", "stale": False,
            "windows": [
                {"label": "5h", "percent": 0, "reset_at": NOW + 9840},
                {"label": "7d", "percent": 1, "reset_at": NOW + 200_000},
                {"label": "mo", "percent": 0, "reset_at": NOW + 2_500_000},
            ],
        })
        lua, module = self.load(opts=self.opts(layout="full", opencode_go={"enabled": True}))
        self.prime_codex(CODEX_OK)
        text = self.build(lua, module, CLAUDE_OK)
        visible = plain(text)
        self.assertIn("OpenCode Go:", visible)
        self.assertIn("(2h44m)", visible)  # full style keeps the parentheses
        self.assertIn("▪", text)  # legacy window separator
        self.assertEqual(text.count("  |"), 2)

    def test_go_state_texts_in_both_rendering_families(self):
        for state, dense, full in (
            ("not_running", "OpenCode Go:", "not running"),
            ("no_key", "no key", "no key"),
            ("no_plan", "no plan", "no Go plan"),
            ("syncing", "…", "syncing..."),
            ("rate_limited", "…", "syncing..."),
            ("unavailable", "?", "unavailable"),
            ("error", "?", "unavailable"),
        ):
            with self.subTest(state=state):
                for layout in ("compact", "full"):
                    lua, module = self.load(opts=self.opts(
                        layout=layout, opencode_go={"enabled": True}
                    ))
                    self.go_stdout = self.go_stdout_for({"status": state, "windows": []})
                    self.prime_codex(CODEX_OK)
                    text = plain(self.build(lua, module, {"not_running": True}))
                    self.assertIn("OpenCode Go:", text)
                    self.assertIn(dense if layout == "compact" else full, text)

    def test_cached_windows_with_a_missing_key_follow_the_tmux_state_text(self):
        """A good read, then the key disappears: tmux and WezTerm must agree on `no key`."""
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(GO_FIXTURE).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(quota, "cache_dir", return_value=root), \
                 mock.patch.object(quota, "process_states", return_value={"opencode-go": True}), \
                 mock.patch.object(quota.time, "time", return_value=NOW), \
                 mock.patch.dict(os.environ, {"OPENCODE_API_KEY": GO_KEY}), \
                 mock.patch.object(quota.urllib.request, "urlopen", return_value=response):
                good = quota.provider_data(
                    "opencode-go", True, NOW, lambda: quota.fetch_opencode_go(NOW), root,
                )
            with mock.patch.object(quota, "cache_dir", return_value=root), \
                 mock.patch.object(quota, "process_states", return_value={"opencode-go": True}), \
                 mock.patch.object(quota.time, "time", return_value=NOW + 61), \
                 mock.patch.object(
                     quota, "go_auth_path", return_value=Path("/nonexistent/tmux-quota-auth.json")
                 ), \
                 mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.object(quota.urllib.request, "urlopen") as urlopen:
                data = quota.provider_data(
                    "opencode-go", True, NOW + 61,
                    lambda: quota.fetch_opencode_go(NOW + 61), root,
                )
        self.assertEqual(good["status"], "ok")
        self.assertEqual(len(good["windows"]), 3)
        self.assertEqual(data["status"], "no_key")
        self.assertIs(data["stale"], True)
        self.assertEqual(len(data["windows"]), 3)
        urlopen.assert_not_called()

        self.go_stdout = self.go_stdout_for(data)
        for layout in ("full", "compact", "minimal"):
            with self.subTest(layout=layout):
                expected = plain(quota.render_status(
                    {"opencode-go": True}, {}, {}, now=NOW + 61, use_unicode=True,
                    providers=("opencode-go",), opencode_go=data, layout=layout,
                )).strip()
                self.assertIn("no key", expected)
                self.assertNotIn("%", expected)
                options = self.opts(
                    layout=layout, hide_inactive=True, opencode_go={"enabled": True},
                )
                lua, module = self.load(opts=options)
                self.prime_codex(CODEX_OK)
                self.codex_running = False
                visible = plain(
                    self.build(lua, module, {"not_running": True}, now=NOW + 61)
                ).strip()
                self.assertEqual(visible, expected)

                # Negative control: reverting this layout's precedence fix shows the
                # cached percentages instead of the state text.
                reverted = self.mutated_plugin(self.state_precedence_revert(layout))
                lua, module = self.load(source=reverted, opts=options)
                self.prime_codex(CODEX_OK)
                self.codex_running = False
                visible = plain(
                    self.build(lua, module, {"not_running": True}, now=NOW + 61)
                ).strip()
                self.assertNotEqual(visible, expected)
                self.assertIn("%", visible)

    def state_precedence_revert(self, layout):
        if layout == "full":
            return [(
                '  elseif state == "no_key" then\n    return head .. DIM .. "no key"\n  end',
                '  elseif state == "no_key" and #windows == 0 then\n'
                '    return head .. DIM .. "no key"\n  end',
            )]
        return [(
            '  if window_count > 0\n'
            '    and state ~= "syncing" and state ~= "no_plan" and state ~= "no_key" then\n'
            '    return nil\n  end',
            '  if window_count > 0 then\n    return nil\n  end',
        )]

    def test_legacy_compact_hides_go_countdowns_in_both_full_layouts(self):
        self.go_stdout = self.go_stdout_for({
            "status": "ok",
            "windows": [
                {"label": "5h", "percent": 40, "reset_at": NOW + 9840},
                {"label": "7d", "percent": 20, "reset_at": NOW + 201_500},
            ],
        })
        for layout in (None, "full"):
            for compact in (False, True):
                with self.subTest(layout=layout, compact=compact):
                    options = self.opts(compact=compact, opencode_go={"enabled": True})
                    if layout:
                        options["layout"] = layout
                    lua, module = self.load(opts=options)
                    self.prime_codex(CODEX_OK)
                    visible = plain(self.build(lua, module, CLAUDE_OK))
                    go_part = visible.split("OpenCode Go:")[1]
                    self.assertIn("40%", go_part)
                    if compact:
                        self.assertNotIn("(", visible)
                        self.assertNotIn("2h44m", go_part)
                    else:
                        self.assertIn("(2h44m)", go_part)

        # Negative control: without the guard the Go countdown survives compact mode.
        reverted = self.mutated_plugin([(
            '    if remaining and not config.compact then\n'
            '      segment = segment .. DIM .. " (" .. remaining .. ")"',
            '    if remaining then\n'
            '      segment = segment .. DIM .. " (" .. remaining .. ")"',
        )])
        for layout in (None, "full"):
            with self.subTest(control=layout):
                options = self.opts(compact=True, opencode_go={"enabled": True})
                if layout:
                    options["layout"] = layout
                lua, module = self.load(source=reverted, opts=options)
                self.prime_codex(CODEX_OK)
                visible = plain(self.build(lua, module, CLAUDE_OK))
                self.assertIn("(2h44m)", visible)

    def test_malformed_json_shows_an_unavailable_state_and_keeps_other_providers(self):
        for label, stdout, ok in (
            ("invalid json", "{not json", True),
            ("json array", "[]", True),
            ("missing provider", '{"version":1,"providers":[]}', True),
            ("state not a string", '{"providers":[{"name":"opencode-go","state":1}]}', True),
            ("wrong version shape", '{"providers":"nope"}', True),
            ("empty stdout", "", True),
            ("non-zero exit", "", False),
        ):
            with self.subTest(case=label):
                self.go_stdout = stdout
                self.go_ok = ok
                lua, module = self.load(opts=self.opts(
                    layout="compact", opencode_go={"enabled": True}
                ))
                self.prime_codex(CODEX_OK)
                text = plain(self.build(lua, module, CLAUDE_OK))
                self.assertIn("OpenCode Go:", text)
                self.assertIn("?", text)
                self.assertIn("Claude:", text)
                self.assertIn("Codex:", text)
                self.assertIn("3%", text)
                self.assertTrue(self.log_errors)

    # -- bridge plumbing -------------------------------------------------

    def test_go_disabled_spawns_nothing_for_go(self):
        for layout in (None, "full", "compact", "minimal"):
            with self.subTest(layout=layout):
                options = self.opts(**({"layout": layout} if layout else {}))
                lua, module = self.load(opts=options)
                self.go_stdout = self.real_go_stdout()
                self.prime_codex(CODEX_OK)
                text = self.build(lua, module, CLAUDE_OK)
                self.assertEqual(self.spawns(), [])
                self.assertNotIn("OpenCode Go", text)

    def test_windows_never_spawns_the_go_helper(self):
        self.go_stdout = self.real_go_stdout()
        for layout, expected in (("compact", "unsupported"), ("minimal", "unsupported"),
                                 ("full", "not supported on Windows")):
            with self.subTest(layout=layout):
                lua, module = self.load(
                    opts=self.opts(layout=layout, opencode_go={"enabled": True}), windows=True
                )
                self.prime_codex(CODEX_OK)
                self.now = NOW
                text = plain(self.build(lua, module, CLAUDE_OK))
                self.assertEqual(self.spawns(), [])
                self.assertIn("OpenCode Go:", text)
                self.assertIn(expected, text)

    def test_key_command_and_helper_override_are_forwarded(self):
        custom = str(self.tmp / "custom-tmux-quota.py")
        Path(custom).write_text("# placeholder\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"WEZTERM_AGENT_QUOTA_TMUX_HELPER": custom}):
            lua, module = self.load(opts=self.opts(
                layout="compact",
                opencode_go={"enabled": True, "key_command": "tool get opencode-go"},
            ))
            self.prime_codex(CODEX_OK)
            self.go_stdout = self.real_go_stdout()
            self.build(lua, module, {"not_running": True})
        argv = self.spawns()[0]
        self.assertEqual(argv[0], "python3")
        self.assertEqual(argv[1], custom)
        self.assertEqual(argv[2:], [
            "--json", "--providers", "opencode-go",
            "--opencode-key-command", "tool get opencode-go",
        ])

    def test_helper_is_found_next_to_codex_limits(self):
        install = self.tmp / "install"
        install.mkdir()
        for name in ("codex-limits.py", "tmux-quota.py"):
            (install / name).write_text("# placeholder\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {}):
            del os.environ["WEZTERM_AGENT_QUOTA_TMUX_HELPER"]
            lua, module = self.load(opts={
                "codex_script": str(install / "codex-limits.py"),
                "layout": "compact",
                "opencode_go": {"enabled": True},
            })
            self.prime_codex(CODEX_OK)
            self.go_stdout = self.real_go_stdout()
            self.build(lua, module, {"not_running": True})
        argv = self.spawns()[0]
        self.assertEqual(argv[1], str(install / "tmux-quota.py"))

    def test_helper_resolves_without_an_explicit_codex_script(self):
        with mock.patch.dict(os.environ, {}):
            del os.environ["WEZTERM_AGENT_QUOTA_TMUX_HELPER"]
            lua, module = self.load(opts={
                "layout": "compact",
                "opencode_go": {"enabled": True},
            })
            self.prime_codex(CODEX_OK)
            self.go_stdout = self.real_go_stdout()
            self.build(lua, module, {"not_running": True})
        argv = self.spawns()[0]
        self.assertEqual(argv[1], str(TMUX_HELPER))

    def test_go_gate_runs_the_helper_at_most_once_per_15s(self):
        lua, module = self.load(opts=self.opts(layout="compact", opencode_go={"enabled": True}))
        self.go_stdout = self.real_go_stdout()
        self.prime_codex(CODEX_OK)
        for offset in (0, 1, 14):
            self.build(lua, module, {"not_running": True}, now=NOW + offset)
        self.assertEqual(len(self.spawns()), 1)
        self.build(lua, module, {"not_running": True}, now=NOW + 15)
        self.assertEqual(len(self.spawns()), 2)
        self.build(lua, module, {"not_running": True}, now=NOW + 29)
        self.assertEqual(len(self.spawns()), 2)

    def test_expired_windows_are_dropped_between_bridge_runs(self):
        self.go_stdout = self.go_stdout_for({
            "status": "ok",
            "windows": [
                {"label": "5h", "percent": 40, "reset_at": NOW + 5},
                {"label": "7d", "percent": 9, "reset_at": NOW + 7},
            ],
        })
        lua, module = self.load(opts=self.opts(layout="compact", opencode_go={"enabled": True}))
        self.prime_codex(CODEX_OK)
        first = plain(self.build(lua, module, {"not_running": True}))
        self.assertIn("40%", first)
        self.assertIn("9%", first)
        self.assertEqual(len(self.spawns()), 1)
        # Still inside the 15 s gate, so no new helper run: the 5h window has expired.
        second = plain(self.build(lua, module, {"not_running": True}, now=NOW + 6))
        self.assertEqual(len(self.spawns()), 1)
        self.assertNotIn("40%", second)
        self.assertIn("9%", second)
        # Both windows gone: the provider drops back to the waiting state.
        third = plain(self.build(lua, module, {"not_running": True}, now=NOW + 10))
        self.assertEqual(len(self.spawns()), 1)
        self.assertIn("OpenCode Go: …", third)

    # -- activity filtering ---------------------------------------------

    def test_hide_inactive_keeps_separators_clean_and_can_empty_the_line(self):
        self.go_stdout = self.real_go_stdout()
        for layout in (None, "compact", "minimal", "full"):
            for states in (
                {"claude": True, "codex": True, "go": True},
                {"claude": False, "codex": True, "go": True},
                {"claude": True, "codex": False, "go": False},
                {"claude": False, "codex": False, "go": False},
            ):
                with self.subTest(layout=layout, states=states):
                    options = self.opts(
                        hide_inactive=True, opencode_go={"enabled": True},
                        **({"layout": layout} if layout else {}),
                    )
                    lua, module = self.load(opts=options)
                    self.go_stdout = self.go_stdout_for(
                        {"status": "ok" if states["go"] else "not_running",
                         "windows": ([{"label": "5h", "percent": 42, "reset_at": NOW + 600}]
                                     if states["go"] else [])}
                    )
                    self.prime_codex(CODEX_OK)
                    self.codex_running = states["codex"]
                    claude = CLAUDE_OK if states["claude"] else {"not_running": True}
                    text = self.build(lua, module, claude)
                    visible = plain(text)
                    if not any(states.values()):
                        self.assertEqual(text, "")
                        continue
                    for name, shown in (("Claude", states["claude"]),
                                        ("Codex", states["codex"]),
                                        ("OpenCode Go", states["go"])):
                        if shown:
                            self.assertIn(name, text)
                        else:
                            self.assertNotIn(name, text)
                    self.assertFalse(visible.strip().endswith("|"))
                    self.assertFalse(visible.strip().endswith("│"))

    def test_parse_go_report_rejects_non_documented_shapes(self):
        lua, module = self.load()
        parse = module["_test"]["parse_go_report"]
        good = '{"version":1,"providers":[{"name":"opencode-go","state":"ok",' \
               '"stale":true,"windows":[{"label":"5h","percent":12.5,"reset_at":100,' \
               '"limited":true}]}]}'
        report = parse(good)
        self.assertEqual(report["state"], "ok")
        self.assertIs(report["stale"], True)
        self.assertEqual(report["windows"][1]["label"], "5h")
        for bad in ("", "not json", "[]", "null",
                    '{"providers":[]}',
                    '{"providers":[{"name":"claude","state":"ok"}]}',
                    '{"providers":[{"name":"opencode-go","state":1}]}',
                    '{"providers":[{"name":"opencode-go"}]}'):
            with self.subTest(bad=bad):
                self.assertIsNone(parse(bad))


if __name__ == "__main__":
    unittest.main()
