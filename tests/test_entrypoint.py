"""The console entry point must keep the exit-code contract: cli.main is the error boundary.

Runs the real command as subprocesses (`python -m agent_boardroom` and the repo dev shim) with an
isolated state dir, so these checks hold however the tool was started.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent_boardroom import cli  # noqa: E402
from agent_boardroom.common import BoardroomError, DeliveryUnknown  # noqa: E402


class EntryBoundary(unittest.TestCase):
    def test_main_maps_exceptions_to_exit_codes(self):
        from unittest import mock
        for exc, code in ((DeliveryUnknown("x"), 2), (BoardroomError("x"), 1), (OSError("x"), 1)):
            with mock.patch.object(cli, "_run", side_effect=exc), self.assertRaises(SystemExit) as ctx:
                cli.main([])
            self.assertEqual(ctx.exception.code, code, type(exc).__name__)

    def _env(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "PYTHONPATH")}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env["AGENT_BOARDROOM_HOME"] = os.path.join(self.tmp.name, "home")
        env["AGENT_BOARDROOM_CODEX_SOCKET"] = os.path.join(self.tmp.name, "no-daemon.sock")
        return env

    def _launchers(self):
        return ([sys.executable, "-m", "agent_boardroom"], [sys.executable, str(ROOT / "agent-boardroom")])

    def test_usage_error_exits_1_and_version_exits_0_via_every_launcher(self):
        env = self._env()
        for cmd in self._launchers():
            r = subprocess.run(cmd + ["bogus"], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 1, cmd)
            self.assertNotIn("Traceback", r.stderr)
            r = subprocess.run(cmd + ["--version"], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, cmd)
            self.assertIn("agent-boardroom ", r.stdout)

    def test_transport_failure_is_exit_1_not_a_traceback(self):
        env = self._env()
        for cmd in self._launchers():
            # explicit codex: prefix, daemon socket absent -> backend unavailable -> failed (1), never 2
            r = subprocess.run(cmd + ["send", "codex:nobody", "hi"], cwd=str(ROOT), env=env,
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 1, (cmd, r.stderr))
            self.assertNotIn("Traceback", r.stderr)


if __name__ == "__main__":
    unittest.main()
