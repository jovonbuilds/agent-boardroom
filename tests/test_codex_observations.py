"""Codex receipts: adversarial history fixtures, real deadlines, no live writes."""
import sys as _sys, pathlib as _pl  # noqa: E401
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # the repo root holds the modules
import json
from contextlib import nullcontext
import socket
import time
import unittest
from unittest import mock

from agent_boardroom import codex as c
from agent_boardroom.common import Session
import test_codex_backend as transport_tests
from test_codex_backend import handshake, read_frame, frame


def page(*entries, cursor=None):
    return {"data": list(entries), "nextCursor": cursor}


def user(mid="message", iid="user-item", tid="turn", **extra):
    return {"turnId": tid, "item": {"type": "userMessage", "id": iid, "clientId": mid},
            "completedAtMs": 1000, **extra}


def output(kind="agentMessage", iid="output", tid="turn"):
    return {"turnId": tid, "item": {"type": kind, "id": iid}, "completedAtMs": 2000}


def turn(status="completed", error=None, tid="turn"):
    return {"id": tid, "status": status, "error": error, "completedAt": 3}


class ObservationTests(unittest.TestCase):
    def probe(self, replies, level="turn_completed", **patches):
        client = mock.Mock()
        client.call.side_effect = [{"thread": {"id": "session", "status": {"type": "notLoaded"}}}, *replies]
        with mock.patch.object(c, "Client", return_value=client), (mock.patch.multiple(c, **patches) if patches else nullcontext()):
            result = c.observe(Session("codex", "session"), "message", level, time.monotonic() + 5)
        client.close.assert_called_once()
        self.assertTrue(all(call.args[0] in ("thread/read", "thread/items/list", "thread/turns/list")
                            for call in client.call.call_args_list))
        return result, client

    def test_paginated_exact_match_and_same_turn_completion(self):
        r, client = self.probe([page(user("other"), cursor="older"), page(user()),
                               page(output(iid="before"), user(), cursor="after"),
                               page(output("reasoning"), output()), page(turn())])
        self.assertEqual((r.state, r.level_reached), ("observed", "turn_completed"))
        self.assertEqual(r.source, {"backend": "codex", "session_id": "session", "item_id": "user-item",
                                   "turn_id": "turn", "output_item_id": "output"})
        self.assertEqual(r.evidence_at, "1970-01-01T00:00:03.000Z")
        self.assertNotEqual(r.observed_at, r.evidence_at)
        self.assertEqual(client.call.call_args_list[2].args[1]["cursor"], "older")

    def test_no_body_matching_or_reasoning_only_upgrade(self):
        fake = user(None)
        fake["item"]["content"] = [{"type": "text", "text": "message"}]
        r, _ = self.probe([page(fake)])
        self.assertEqual((r.state, r.level_reached), ("not_observed", None))
        r, _ = self.probe([page(user()), page(output(iid="before"), user(), output("reasoning")),
                           page(turn("inProgress"))])
        self.assertEqual((r.state, r.level_reached), ("not_observed", "recorded"))

    def test_missing_client_id_is_schema_failure_not_legacy_null(self):
        fake = user()
        del fake["item"]["clientId"]
        r, _ = self.probe([page(fake)])
        self.assertEqual((r.state, r.reason), ("unavailable", "unsupported_schema"))

    def test_different_turn_output_cannot_upgrade(self):
        r, _ = self.probe([page(user()), page(user(), output(tid="other-turn"))])
        self.assertEqual((r.state, r.level_reached, r.reason), ("unavailable", "recorded", "unsupported_schema"))

    def test_completed_requires_null_error_and_response(self):
        for t in (turn("failed"), turn("interrupted"), turn("inProgress"), turn(error={"message": "secret"})):
            r, _ = self.probe([page(user()), page(user(), output()), page(t)])
            self.assertEqual(r.level_reached, "responded")
            self.assertNotIn("secret", repr(r))
        r, _ = self.probe([page(user()), page(user()), page(turn())])
        self.assertEqual(r.level_reached, "recorded")

    def test_failed_turn_retains_lower_evidence_without_error_content(self):
        for has_output in (False, True):
            with self.subTest(has_output=has_output):
                entries = [user(), output()] if has_output else [user()]
                r, client = self.probe([page(user()), page(*entries),
                                       page(turn("failed", {"message": "PRIVATE ERROR"}))])
                self.assertEqual((r.state, r.reason), ("not_observed", "turn_failed"))
                self.assertEqual(r.level_reached, "responded" if has_output else "recorded")
                self.assertEqual(r.source["turn_id"], "turn")
                self.assertNotIn("turn_status", r.source)
                self.assertNotIn("PRIVATE", repr(r))
                self.assertEqual(client.call.call_args_list[-1].args[0], "thread/turns/list")

    def test_failed_shortcut_requires_unique_id_through_all_pages(self):
        failed = turn("failed", {"message": "error"})
        for later in (turn(), failed, turn("interrupted")):
            with self.subTest(later=later["status"]):
                r, _ = self.probe([page(user()), page(user()), page(failed, cursor="next"), page(later)])
                self.assertEqual((r.state, r.reason, r.level_reached),
                                 ("unavailable", "unsupported_schema", "recorded"))
        r, _ = self.probe([page(user()), page(user()), page(failed, cursor="next"), page(turn(tid="other"))])
        self.assertEqual((r.state, r.reason), ("not_observed", "turn_failed"))

    def test_failed_shortcut_cannot_outlive_scan_bounds_or_bad_later_page(self):
        failed = turn("failed", {"message": "error"})
        r, _ = self.probe([page(user()), page(user()), page(failed, cursor="next")], OBS_MAX_PAGES=3)
        self.assertEqual((r.state, r.reason, r.level_reached), ("incomplete", "bound_reached", "recorded"))
        r, _ = self.probe([page(user()), page(user()), page(failed, cursor="next"), page({})])
        self.assertEqual((r.state, r.reason), ("unavailable", "unsupported_schema"))

    def test_failed_shortcut_requires_valid_terminal_error(self):
        for error in (None, True, "private", [], {}, {"message": 42}):
            with self.subTest(error=error):
                r, _ = self.probe([page(user()), page(user()), page(turn("failed", error))])
                self.assertEqual((r.state, r.reason, r.level_reached),
                                 ("unavailable", "unsupported_schema", "recorded"))

    def test_interrupted_or_unfinished_turns_never_get_failure_reason(self):
        for t in (turn("interrupted"), turn("inProgress"), turn(), turn(error={"message": "error"})):
            with self.subTest(status=t["status"], error=t["error"]):
                r, _ = self.probe([page(user()), page(user()), page(t)])
                self.assertEqual((r.state, r.reason, r.level_reached), ("not_observed", None, "recorded"))

    def test_lower_level_requests_do_not_fetch_turn_status(self):
        for entries, expected in (([user()], "recorded"), ([user(), output()], "responded")):
            r, client = self.probe([page(user()), page(*entries)], "responded")
            self.assertEqual(r.level_reached, expected)
            self.assertIsNone(r.reason)
            self.assertFalse(any(call.args[0] == "thread/turns/list" for call in client.call.call_args_list))

    def test_recorded_short_circuits_without_assistant_lookup(self):
        r, client = self.probe([page(user())], "recorded")
        self.assertEqual(r.level_reached, "recorded")
        self.assertEqual(client.call.call_count, 2)

    def test_lower_evidence_survives_budget_and_cursor_cycle(self):
        r, _ = self.probe([page(user())], OBS_MAX_PAGES=1)
        self.assertEqual((r.state, r.level_reached, r.reason), ("incomplete", "recorded", "bound_reached"))
        r, _ = self.probe([page(user()), page(user(), cursor="x"), page(cursor="x")])
        self.assertEqual((r.state, r.level_reached, r.reason), ("unavailable", "recorded", "unsupported_schema"))

    def test_lower_evidence_survives_history_disappearing(self):
        r, _ = self.probe([page(user()), page()])
        self.assertEqual((r.state, r.level_reached, r.reason), ("unavailable", "recorded", "history_missing"))
        r, _ = self.probe([page(user()), page(user(), output()), page(turn(tid="different"))])
        self.assertEqual((r.state, r.level_reached), ("unavailable", "responded"))

    def test_server_errors_do_not_leak_bodies(self):
        for error in (c.ServerError("PRIVATE BODY", -32601), c.BoardroomError("PRIVATE BODY")):
            r, _ = self.probe([page(user()), error])
            self.assertEqual(r.level_reached, "recorded")
            self.assertNotIn("PRIVATE", repr(r))

    def test_deadline_or_unsupported_level_never_connects(self):
        with mock.patch.object(c, "Client") as client:
            r = c.observe(Session("codex", "s"), "m", "recorded", time.monotonic() - 1)
            self.assertEqual(r.state, "incomplete")
            r = c.observe(Session("codex", "s"), "m", "processed", time.monotonic() + 1)
            self.assertEqual(r.reason, "unsupported_level")
            client.assert_not_called()

    def test_wrong_session_response_is_refused(self):
        client = mock.Mock()
        client.call.return_value = {"thread": {"id": "wrong"}}
        with mock.patch.object(c, "Client", return_value=client):
            r = c.observe(Session("codex", "session"), "message", "recorded", time.monotonic() + 1)
        self.assertEqual(r.reason, "session_mismatch")
        self.assertEqual(client.call.call_count, 1)

    def test_byte_limit_not_absence(self):
        r, _ = self.probe([page(user())], OBS_MAX_BYTES=10)
        self.assertEqual((r.state, r.reason), ("incomplete", "bound_reached"))

    def test_invalid_timestamp_does_not_erase_positive_match(self):
        r, _ = self.probe([page(user(completedAtMs="private"))], "recorded")
        self.assertEqual((r.state, r.level_reached), ("observed", "recorded"))
        self.assertIsNone(r.evidence_at)
        self.assertNotIn("private", repr(r))

    def test_unrelated_unknown_item_does_not_hide_exact_match(self):
        bad = {"turnId": "turn", "item": {"type": "futureToolWithoutId"}}
        r, _ = self.probe([page(bad, user())], "recorded")
        self.assertEqual((r.state, r.level_reached), ("observed", "recorded"))
        r, _ = self.probe([page(bad)], "recorded")
        self.assertEqual((r.state, r.reason), ("unavailable", "unsupported_schema"))
        r, _ = self.probe([page(user()), page(user(), bad)], "responded")
        self.assertEqual((r.state, r.level_reached), ("unavailable", "recorded"))


class ProbeTransportTests(unittest.TestCase):
    def test_absolute_deadline_includes_initialize_not_just_history(self):
        def peer(sock):
            handshake(sock)
            read_frame(sock)
            # The client must time out and close while initialize is unanswered.
            self.assertEqual(sock.recv(1), b"")
        def work(path):
            start = time.monotonic()
            with self.assertRaises((c.ProbeBound, c.BoardroomError, socket.timeout)):
                c.Client(path, timeout=10, deadline=start + 0.15)
            self.assertLess(time.monotonic() - start, 1.5)
        transport_tests.SocketTests.run_peer(self, peer, work)

    def test_receive_budget_includes_notifications(self):
        def peer(sock):
            handshake(sock)
            req = json.loads(read_frame(sock)[1])
            sock.sendall(frame(1, json.dumps({"id": req["id"], "result": {}}).encode()))
            read_frame(sock)
            read_frame(sock)
            sock.sendall(frame(1, json.dumps({"method": "notice", "params": "x" * 1500}).encode()))
        def work(path):
            client = c.Client(path, deadline=time.monotonic() + 3, max_receive_bytes=1000)
            try:
                with self.assertRaises(c.ProbeBound):
                    client.call("thread/read", {"threadId": "s"})
            finally:
                client.close()
        transport_tests.SocketTests.run_peer(self, peer, work)


class DoctorTests(unittest.TestCase):
    def test_no_cli_no_loaded_thread_no_mutation(self):
        client = mock.Mock()
        client.call.return_value = page()
        with mock.patch.object(c.shutil, "which", return_value=None), \
             mock.patch.object(c, "Client", return_value=client), \
             mock.patch("agent_boardroom.common.safe_text", side_effect=lambda value, **kw: str(value), create=True):
            rows = c.doctor(time.monotonic() + 1)
        self.assertTrue(any(r["check"] == "codex.running_version" and "unknown" in r["detail"] for r in rows))
        self.assertEqual([call.args[0] for call in client.call.call_args_list], ["thread/loaded/list"])
        client.close.assert_called_once()

    def test_version_and_capabilities_separate_and_errors_redacted(self):
        with mock.patch.object(c.shutil, "which", return_value="/fake/codex"), \
             mock.patch.object(c, "_version_output", return_value="codex-cli 0.160.1"), \
             mock.patch.object(c, "Client", side_effect=c.ServerError("SECRET")), \
             mock.patch("agent_boardroom.common.safe_text", side_effect=lambda value, **kw: str(value), create=True):
            rows = c.doctor(time.monotonic() + 1)
        self.assertEqual(next(r for r in rows if r["check"] == "codex.installed_version")["status"], "ok")
        self.assertTrue(any(r["status"] == "warn" for r in rows))
        self.assertNotIn("SECRET", repr(rows))


if __name__ == "__main__":
    unittest.main()
