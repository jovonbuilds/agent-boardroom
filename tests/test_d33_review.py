"""Independent D33 regression: gate hints must use all positive evidence seen."""
import sys as _sys, pathlib as _pl  # noqa: E401
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # the repo root holds the modules
from contextlib import redirect_stderr
import io
import unittest
from unittest import mock

from agent_boardroom.common import Observation, Session
from test_review_regressions import cli


class D33Independent(unittest.TestCase):
    def test_later_empty_or_failed_probe_does_not_erase_admission_evidence(self):
        recorded = Observation("not_observed", "recorded", "2026-10-06T06:00:00Z",
                               {"backend": "claude", "entry_uuid": "root"})
        for state, reason in (("not_observed", None), ("error", "probe_failed"),
                              ("unavailable", "history_missing")):
            with self.subTest(state=state):
                later = Observation(state, None, "2026-10-06T06:00:01Z",
                                    {"backend": "claude"}, reason=reason)
                err = io.StringIO()
                with mock.patch.object(cli, "_probe", side_effect=[recorded, later]) as probe, \
                        mock.patch.object(cli.time, "monotonic", side_effect=[0, 0, 2]), \
                        mock.patch.object(cli.time, "sleep"), \
                        mock.patch.object(cli, "record_observation") as record, \
                        redirect_stderr(err), self.assertRaises(SystemExit) as exit_info:
                    cli.wait_for_receipt(Session("claude", "session"), "message", "responded", 1, "written")
                self.assertEqual(exit_info.exception.code, 4)
                self.assertEqual(probe.call_count, 2)
                self.assertIn("highest level seen: recorded", err.getvalue())
                self.assertIn("DO NOT RESEND", err.getvalue())
                self.assertNotIn("awaiting approval", err.getvalue())
                self.assertNotIn("gate decision", err.getvalue())
                record.assert_not_called()
