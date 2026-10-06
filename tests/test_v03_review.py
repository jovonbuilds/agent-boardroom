"""Independent v0.3 contract regressions, authored by Codex from counterexamples.

Do not touch the real agent-boardroom log or any running session. Claude owns fixes
in shared/CLI/Claude files; these tests constrain observable behavior instead.
"""
import sys as _sys, pathlib as _pl  # noqa: E401
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # the repo root holds the modules
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agent_boardroom import claude
from agent_boardroom import common
from test_review_regressions import cli
from agent_boardroom.common import Observation, Session, BoardroomError


def peer_user():
    return {"type": "user", "uuid": "message", "sessionId": "session", "isSidechain": False,
            "origin": {"kind": "peer", "msg_id": "message"}, "timestamp": "2026-10-06T05:00:00Z"}


def peer_child():
    return {"type": "assistant", "uuid": "child", "parentUuid": "message", "sessionId": "session",
            "isSidechain": False, "timestamp": "2026-10-06T05:00:01Z"}


class ReviewV03(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        patcher = mock.patch.multiple(common, BOARDROOM_HOME=self.root, LOG_PATH=self.root / "log.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)

    def receipt_fixture(self, lines, level="responded"):
        path = self.root / "transcript.jsonl"
        path.write_bytes(b"".join((json.dumps(x).encode() if isinstance(x, dict) else x) + b"\n" for x in lines))
        with mock.patch.object(claude, "locate_transcript", return_value=(str(path), None)):
            return claude.observe(Session("claude", "session"), "message", level, time.monotonic() + 2)

    def test_duplicate_logged_message_id_is_ambiguous_for_receipts(self):
        for target in ("first", "second"):
            common.log_event({"event": "attempt", "msg_id": "duplicate", "reply_to": None,
                              "to": {"backend": "claude", "id": target}})
        with self.assertRaises(BoardroomError):
            common.find_message("duplicate")

    def test_doctor_rejects_invalid_budget_before_backend_probes(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), mock.patch.dict(cli.BACKEND, {"claude": mock.Mock(), "codex": mock.Mock()}), \
                 self.assertRaises(BoardroomError):
                cli.cmd_doctor(argparse.Namespace(timeout=timeout, json=True))

    def busy_attachment(self):
        return {"type": "attachment", "uuid": "native-entry", "sessionId": "session", "isSidechain": False,
                "timestamp": "2026-10-06T05:00:00Z", "attachment": {"type": "queued_command", "source_uuid": "message",
                "origin": {"kind": "peer", "msg_id": "message"}}}

    def test_busy_attachment_records_exact_dual_message_identity(self):
        obs = self.receipt_fixture([self.busy_attachment()], level="recorded")
        self.assertEqual((obs.state, obs.level_reached), ("observed", "recorded"))
        self.assertEqual(obs.source["entry_uuid"], "native-entry")
        self.assertEqual(obs.source["delivery"], "attachment")

    def test_busy_attachment_direct_child_uses_native_entry_identity(self):
        child = peer_child()
        child["parentUuid"] = "native-entry"
        obs = self.receipt_fixture([self.busy_attachment(), child])
        self.assertEqual((obs.state, obs.level_reached), ("observed", "responded"))
        self.assertEqual(obs.source["output_uuid"], "child")

    def test_busy_attachment_walks_valid_attachment_ancestry_v031(self):
        intermediate = {"type": "attachment", "uuid": "intermediate", "parentUuid": "native-entry",
                        "sessionId": "session", "isSidechain": False,
                        "attachment": {"type": "total_tokens_reminder"}}
        child = peer_child()
        child["parentUuid"] = "intermediate"
        obs = self.receipt_fixture([self.busy_attachment(), intermediate, child])
        self.assertEqual((obs.state, obs.level_reached), ("observed", "responded"))
        self.assertEqual(obs.source["hops"], 2)

    def test_busy_attachment_rejects_conflicting_ids_session_and_sidechain(self):
        for which in ("source", "origin", "session", "sidechain"):
            with self.subTest(which=which):
                rec = self.busy_attachment()
                if which == "source":
                    rec["attachment"]["source_uuid"] = "different"
                elif which == "origin":
                    rec["attachment"]["origin"]["msg_id"] = "different"
                elif which == "session":
                    rec["sessionId"] = "different"
                else:
                    rec["isSidechain"] = True
                obs = self.receipt_fixture([rec], level="recorded")
                self.assertIsNone(obs.level_reached)

    def test_missing_ancestor_link_is_not_a_valid_root(self):
        records = [{"event": "attempt", "msg_id": "parent"}]
        with self.assertRaises(BoardroomError):
            common.check_guards(records, "claude:s", "codex:t", "body", "parent")

    def test_missing_child_uuid_cannot_prove_responded(self):
        child = peer_child()
        del child["uuid"]
        obs = self.receipt_fixture([peer_user(), child])
        self.assertEqual(obs.level_reached, "recorded")
        self.assertIn(obs.state, ("unavailable", "incomplete"))

    def test_positive_receipt_does_not_require_reading_unrelated_tail(self):
        user = peer_user()
        # The requested evidence is wholly inside the budget; only irrelevant
        # later history exceeds it. Positive proof must not become exit 4.
        first = json.dumps(user).encode() + b"\n"
        with mock.patch.object(claude, "SCAN_MAX_BYTES", len(first) + 10):
            obs = self.receipt_fixture([user, {"type": "unrelated", "payload": "x" * 100}], level="recorded")
        self.assertEqual((obs.state, obs.level_reached), ("observed", "recorded"))

    def test_matching_torn_line_is_not_complete_absence(self):
        obs = self.receipt_fixture([b'{"type":"user","uuid":"message",'])
        self.assertIn(obs.state, ("unavailable", "incomplete"))

    def test_read_failure_preserves_recorded(self):
        data = json.dumps(peer_user()).encode() + b"\n"
        class BreakAfterUser(io.BytesIO):
            def __init__(self):
                super().__init__(data)
            def readline(self, size=-1):
                if self.tell() >= len(data):
                    raise OSError("PRIVATE TRANSCRIPT BODY")
                return super().readline(size)
            def __next__(self):
                value = self.readline()
                if not value:
                    raise StopIteration
                return value
        with mock.patch.object(claude, "locate_transcript", return_value=("/fixture", None)), \
             mock.patch("builtins.open", return_value=BreakAfterUser()):
            obs = claude.observe(Session("claude", "session"), "message", "responded", time.monotonic() + 2)
        self.assertEqual(obs.level_reached, "recorded")
        self.assertIn(obs.state, ("error", "incomplete", "unavailable"))
        self.assertNotIn("PRIVATE", repr(obs))

    def test_argparse_errors_do_not_emit_escape_sequences(self):
        parser = cli.Parser(prog="agent-boardroom")
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            parser.parse_args(["--bad\x1b]52;c;secret\x07"])
        self.assertEqual(caught.exception.code, 1)
        self.assertNotIn("\x1b", err.getvalue())
        self.assertNotIn("\x07", err.getvalue())

    def test_list_sanitizes_registry_address_and_single_line_fields(self):
        out = io.StringIO()
        s = Session("claude", "bad\x1b[2J", "name\nFORGED", "idle", "/cwd\nFORGED")
        with mock.patch.object(cli, "gather", return_value=[s]), \
             mock.patch.object(cli, "me", return_value=Session("codex", "me")), redirect_stdout(out):
            cli.cmd_list(argparse.Namespace(json=False))
        self.assertNotIn("\x1b", out.getvalue())
        self.assertEqual(len(out.getvalue().splitlines()), 1)

    def test_receipt_json_stays_json_when_recording(self):
        record = {"msg_id": "message", "to": {"backend": "claude", "id": "session", "cwd": "/tmp"}}
        obs = Observation("observed", "recorded", "2026-10-06T05:00:00Z",
                          {"backend": "claude", "session_id": "session", "entry_uuid": "message"})
        backend = mock.Mock()
        backend.supported_levels.return_value = ("recorded", "responded")
        backend.observe.return_value = obs
        out = io.StringIO()
        with mock.patch.object(cli, "find_message", return_value=record), \
             mock.patch.dict(cli.BACKEND, {"claude": backend}), redirect_stdout(out), redirect_stderr(io.StringIO()):
            cli.cmd_receipt(argparse.Namespace(msg_id="message", level="recorded", timeout=1, json=True, record=True))
        value = json.loads(out.getvalue())
        self.assertEqual(value["msg_id"], "message")

    def test_receipt_log_failure_still_prints_positive_human_evidence(self):
        record = {"msg_id": "message", "to": {"backend": "claude", "id": "session"}}
        obs = Observation("observed", "recorded", "2026-10-06T05:00:00Z",
                          {"backend": "claude", "session_id": "session", "entry_uuid": "message"})
        backend = mock.Mock()
        backend.supported_levels.return_value = ("recorded", "responded")
        backend.observe.return_value = obs
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "find_message", return_value=record), \
             mock.patch.dict(cli.BACKEND, {"claude": backend}), \
             mock.patch.object(cli, "record_observation", side_effect=common.LogError("disk full")), \
             redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            cli.cmd_receipt(argparse.Namespace(msg_id="message", level="recorded", timeout=1, json=False, record=True))
        self.assertEqual(caught.exception.code, 3)
        self.assertIn("message", out.getvalue())
        self.assertIn("level_reached=recorded", out.getvalue())
        self.assertIn("observation not saved", err.getvalue())

    def test_wait_cannot_claim_higher_level_from_lower_observed_state(self):
        lower = Observation("observed", "recorded", "2026-10-06T05:00:00Z", {"item_id": "item"})
        with mock.patch.object(cli, "_probe", return_value=lower), \
             mock.patch.object(cli, "record_observation"), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.wait_for_receipt(Session("codex", "s"), "m", "turn_completed", 0, "accepted")
        self.assertEqual(caught.exception.code, 4)

    def test_expired_claude_probe_does_not_locate_or_open_files(self):
        with mock.patch.object(claude, "locate_transcript") as locate:
            obs = claude.observe(Session("claude", "s"), "m", "recorded", time.monotonic() - 1)
        self.assertEqual((obs.state, obs.reason), ("incomplete", "bound_reached"))
        locate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
