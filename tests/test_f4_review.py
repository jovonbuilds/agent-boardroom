"""Independent F4 CLI/backend integration fixtures; no sockets, sends or real logs."""
import sys as _sys, pathlib as _pl  # noqa: E401
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # the repo root holds the modules
from contextlib import redirect_stderr, redirect_stdout
import io
import unittest
from unittest import mock

from agent_boardroom import codex
from agent_boardroom.common import Session
from test_codex_observations import page, user, output, turn
from test_review_regressions import cli


def scan(entries, status):
    return [{"thread": {"id": "session"}}, page(user()), page(*entries), page(status)]


class F4Independent(unittest.TestCase):
    def run_wait(self, responses):
        client = mock.Mock()
        client.call.side_effect = responses
        err = io.StringIO()
        with mock.patch.object(codex, "Client", return_value=client), \
                mock.patch.dict(cli.BACKEND, {"codex": codex}), \
                mock.patch.object(cli.time, "sleep") as sleep, \
                mock.patch.object(cli, "record_observation") as record, \
                redirect_stderr(err), redirect_stdout(io.StringIO()):
            try:
                cli.wait_for_receipt(Session("codex", "session"), "message", "turn_completed", 30, "queued")
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue(), sleep, record, client

    def test_failure_before_output_stops_without_sleep_or_writing_a_success(self):
        code, err, sleep, record, client = self.run_wait(scan([user()], turn("failed", {"message": "SECRET"})))
        self.assertEqual(code, 4)
        self.assertIn("recorded", err)
        self.assertIn("DO NOT RESEND", err)
        self.assertNotIn("SECRET", err)
        self.assertNotIn("within", err)
        sleep.assert_not_called()
        record.assert_not_called()
        self.assertEqual(client.call.call_count, 4)

    def test_synthesized_interruption_can_later_complete_same_turn(self):
        responses = scan([user()], turn("interrupted")) + scan([user(), output()], turn())
        code, _, sleep, record, _ = self.run_wait(responses)
        self.assertEqual(code, 0)
        sleep.assert_called_once()
        record.assert_called_once()
        self.assertEqual(record.call_args.args[1], "turn_completed")
        self.assertEqual(record.call_args.args[2].source["turn_id"], "turn")

    def test_early_stop_reports_best_evidence_across_probes(self):
        responses = (scan([user(), output()], turn("inProgress"))
                     + scan([user()], turn("failed", {"message": "failed"})))
        code, err, sleep, record, _ = self.run_wait(responses)
        self.assertEqual(code, 4)
        self.assertIn("highest level seen: responded", err)
        sleep.assert_called_once()
        record.assert_not_called()
