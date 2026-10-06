"""Independent regression tests for shared and Claude-backend behavior.

These deliberately exercise counterexamples to the initial design claims:
wrong-party replies, stale IDs resembling names, and logging failure AFTER send.
"""
import sys as _sys, pathlib as _pl  # noqa: E401
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # the repo root holds the modules
import argparse
import hashlib
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from agent_boardroom import claude as br_claude
from agent_boardroom import common as br_common
from agent_boardroom.common import BoardroomError, DeliveryUnknown, LogError, Session

from agent_boardroom import cli


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        # Isolate all persistence, even if a future refactor moves the mocked
        # reservation boundary. An offline regression must not alter the real log.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.multiple(br_common, BOARDROOM_HOME=Path(directory.name),
                                      LOG_PATH=Path(directory.name) / "log.jsonl")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_invalid_extra_identity_is_not_ignored(self):
        claude, codex = mock.Mock(), mock.Mock()
        claude.whoami.side_effect = BoardroomError("stale advertised Claude id")
        codex.whoami.return_value = Session("codex", "codex-live")
        with mock.patch.dict(cli.BACKEND, {"claude": claude, "codex": codex}):
            with self.assertRaises(BoardroomError):
                cli.me()
            self.assertEqual(cli.me("codex").id, "codex-live")

    def test_bare_target_refuses_partial_discovery(self):
        claude, codex = mock.Mock(), mock.Mock()
        claude.list_sessions.side_effect = OSError("Claude registry unavailable")
        codex.list_sessions.return_value = [Session("codex", "alive", "review")]
        with mock.patch.dict(cli.BACKEND, {"claude": claude, "codex": codex}):
            with self.assertRaises(BoardroomError):
                cli.resolve("review")
            self.assertEqual(cli.resolve("codex:review").id, "alive")

    def test_reply_never_routes_missing_id_to_another_sessions_name(self):
        record = {"msg_id": "message-one", "from": {"backend": "claude", "id": "departed"},
                  "to": {"backend": "codex", "id": "reviewer"}}
        replacement = Session("claude", "different-id", "departed")
        args = argparse.Namespace(as_backend=None, msg_id="message-one", message=["reply"])
        with mock.patch.object(cli, "me", return_value=Session("codex", "reviewer")), \
             mock.patch.object(cli, "find_message", return_value=record), \
             mock.patch.object(cli, "read_log", return_value=[record, {
                 "event": "outcome", "msg_id": "message-one", "outcome": "accepted"}]), \
             mock.patch.object(cli, "gather", return_value=[replacement]), \
             mock.patch.object(cli, "deliver") as deliver:
            with self.assertRaises(BoardroomError):
                cli.cmd_reply(args)
            deliver.assert_not_called()

    def test_duplicate_canonical_id_is_ambiguous_even_for_exact_reply(self):
        records = [Session("claude", "same-id", "first", detail={"pid": 111}),
                   Session("claude", "same-id", "second", detail={"pid": 222})]
        with mock.patch.object(cli, "gather", return_value=records):
            with self.assertRaises(BoardroomError):
                cli.resolve_exact("claude", "same-id")

    def test_claude_refresh_cannot_switch_between_duplicate_session_ids(self):
        records = [{"sessionId": "same-id", "name": "first", "pid": 111, "messagingSocketPath": "/one"},
                   {"sessionId": "same-id", "name": "second", "pid": 222, "messagingSocketPath": "/two"}]
        with mock.patch.object(br_claude, "_records", return_value=iter(records)):
            with self.assertRaises(BoardroomError):
                br_claude.refresh(Session("claude", "same-id", detail={"pid": 222, "sock": "/two"}))

    def test_same_id_on_different_backend_is_not_an_endpoint(self):
        record = {"msg_id": "message-one", "from": {"backend": "claude", "id": "shared-id"},
                  "to": {"backend": "claude", "id": "other"}}
        args = argparse.Namespace(as_backend=None, msg_id="message-one", message=["reply"])
        with mock.patch.object(cli, "me", return_value=Session("codex", "shared-id")), \
             mock.patch.object(cli, "find_message", return_value=record), \
             mock.patch.object(cli, "deliver") as deliver:
            with self.assertRaises(BoardroomError):
                cli.cmd_reply(args)
            deliver.assert_not_called()

    def deliver_with_log_failure(self, send_error=None, fail_attempt=False):
        backend = mock.Mock()
        target, sender = Session("codex", "target"), Session("claude", "sender")
        backend.refresh.return_value = target
        backend.send.return_value = {"queue_id": "ack-123"}
        if send_error:
            backend.send.side_effect = send_error
        calls = []

        def log(record):
            calls.append(record)
            if fail_attempt or record["event"] == "outcome":
                raise LogError("simulated disk full")

        stderr = io.StringIO()
        with mock.patch.dict(cli.BACKEND, {"codex": backend}), \
             mock.patch.object(cli, "log_event", side_effect=log), \
             mock.patch.object(cli, "reserve_attempt", side_effect=lambda record, *args: log(record)), \
             redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            try:
                cli.deliver(sender, target, "Test")
            except (SystemExit, BoardroomError) as error:
                return backend, calls, stderr.getvalue(), error
        self.fail("expected logging failure to be surfaced")

    def test_failed_attempt_log_prevents_transport(self):
        backend, _, _, error = self.deliver_with_log_failure(fail_attempt=True)
        backend.send.assert_not_called()
        self.assertIsInstance(error, BoardroomError)
        self.assertIn("NOT sent", str(error))

    def test_failed_outcome_log_preserves_known_acceptance(self):
        backend, records, stderr, error = self.deliver_with_log_failure()
        backend.send.assert_called_once()
        self.assertIsInstance(error, SystemExit)
        self.assertEqual(error.code, 3)
        self.assertEqual(records[-1]["outcome"], "accepted")
        self.assertIn("ack-123", stderr)
        self.assertIn("Do NOT resend", stderr)

    def test_failed_outcome_log_does_not_hide_unknown_delivery(self):
        backend, records, stderr, error = self.deliver_with_log_failure(DeliveryUnknown("connection lost"))
        backend.send.assert_called_once()
        self.assertEqual(error.code, 2)
        self.assertEqual(records[-1]["outcome"], "delivery-unknown")
        self.assertIn("delivery unknown", stderr)

    def test_trailing_newline_in_routing_id_is_rejected(self):
        with self.assertRaises(BoardroomError):
            br_common.validate_routing_id("looks-valid\n")

    def test_claude_escaped_control_body_rejected_before_connect(self):
        session = Session("claude", "target", detail={"pid": 123, "sock": "/unused"})
        with mock.patch.object(br_claude, "_peer_token", return_value=None), \
             mock.patch.object(br_claude, "_write") as write:
            with self.assertRaises(BoardroomError):
                # Body is below the UX cap but its escaped wire representation is >1 MiB.
                br_claude.send(session, "\x01" * 200000, "mid", "codex:sender")
            write.assert_not_called()

    def test_exact_socket_key_and_session_guard(self):
        session = Session("claude", "intended-target", detail={"pid": 123, "sock": "/tmp/exact-path"})
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(br_claude, "SESS_DIR", directory):
            Path(directory, "123.wrong.key").write_text(json.dumps({"peerToken": "wrong-test-key"}))
            digest = hashlib.sha256(b"/tmp/exact-path").hexdigest()
            Path(directory, "123." + digest + ".key").write_text(json.dumps({"peerToken": "correct-test-key"}))
            with mock.patch.object(br_claude, "_write") as write:
                br_claude.send(session, "hello", "mid", "codex:sender")
            lines = [json.loads(line) for line in write.call_args.args[1].decode().splitlines()]
            self.assertEqual(lines[0]["token"], "correct-test-key")
            self.assertEqual(lines[1]["session_id"], "intended-target")
            self.assertNotIn("from-mode", lines[1])


if __name__ == "__main__":
    unittest.main()
