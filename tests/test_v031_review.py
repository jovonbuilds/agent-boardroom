"""Independent D34 graph/snapshot counterexamples. No live sessions or real logs.

The v0.3.1 contract counts all child references before filtering. A whole-prefix
index is necessary: empirical serialization order cannot prove no branches.
"""
import sys as _sys, pathlib as _pl  # noqa: E401
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # the repo root holds the modules
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agent_boardroom import claude
from agent_boardroom.common import Session


def node(uid, parent=None, kind="attachment", **extra):
    return {"uuid": uid, "parentUuid": parent, "type": kind, "sessionId": "session",
            "isSidechain": False, "timestamp": "2026-10-06T06:00:00Z", **extra}


def root():
    return node("root", attachment={"type": "queued_command", "source_uuid": "message",
                                    "origin": {"kind": "peer", "msg_id": "message"}})


def chain(hops):
    records = [root()]
    parent = "root"
    for i in range(1, hops):
        uid = "attachment-" + str(i)
        records.append(node(uid, parent))
        parent = uid
    records.append(node("answer", parent, "assistant"))
    return records


class D34Independent(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "transcript.jsonl"

    def write(self, records, trailing_newline=True):
        data = b"\n".join(json.dumps(r).encode() if isinstance(r, dict) else r for r in records)
        self.path.write_bytes(data + (b"\n" if trailing_newline else b""))

    def observe(self, level="responded"):
        with mock.patch.object(claude, "locate_transcript", return_value=(str(self.path), None)):
            return claude.observe(Session("claude", "session"), "message", level, time.monotonic() + 3)

    def assert_lower_only(self, obs, state=None):
        self.assertEqual(obs.level_reached, "recorded")
        self.assertNotIn("output_uuid", obs.source)
        if state:
            self.assertEqual(obs.state, state)

    def test_sibling_before_root_cannot_be_missed(self):
        self.write([node("early-sibling", "root"), *chain(2)])
        obs = self.observe()
        self.assert_lower_only(obs, "unavailable")
        self.assertEqual(obs.reason, "unsupported_schema")

    def test_malformed_metadata_cannot_retain_arbitrary_json_content(self):
        # A memory bound must cover retained objects, not only valid string IDs.
        # Inspect the graph boundary because an unrelated malformed node may
        # legitimately be ignored by resolution, provided its body is discarded.
        original = claude._resolve_busy
        for value in ({"payload": "x" * 5000}, ["x" * 5000]):
            with self.subTest(kind=type(value).__name__):
                self.write([*chain(2), node("unrelated", kind=value)])
                def inspect(*args):
                    for metadata in args[4].values():
                        self.assertTrue(all(v is None or isinstance(v, (str, bool))
                                            for v in metadata))
                    return original(*args)
                with mock.patch.object(claude, "_resolve_busy", side_effect=inspect):
                    self.observe()

    def test_unicode_payload_counts_toward_metadata_byte_budget(self):
        # 200 astral code points fit ID_MAX, but need at least 800 bytes.
        self.write([*chain(2), node("\U0001f600" * 200)])
        with mock.patch.object(claude, "INDEX_MAX_BYTES", 600):
            obs = self.observe()
        self.assert_lower_only(obs, "incomplete")
        self.assertEqual(obs.reason, "bound_reached")

    def test_malformed_truthy_api_error_flag_cannot_become_success(self):
        for value in ("true", 1, {"error": True}, [True]):
            with self.subTest(flag=value):
                records = chain(2)
                records[-1]["isApiErrorMessage"] = value
                self.write(records)
                self.assert_lower_only(self.observe())

    def test_reverse_serialization_is_not_a_missing_branch_proof(self):
        self.write(list(reversed(chain(3))))
        obs = self.observe()
        # A full index is allowed to resolve these links regardless of order.
        self.assertEqual((obs.state, obs.level_reached), ("observed", "responded"))
        self.assertEqual(obs.source["hops"], 3)

    def test_late_sibling_after_assistant_blocks_upgrade(self):
        self.write([*chain(2), node("late-sibling", "attachment-1")])
        self.assert_lower_only(self.observe(), "unavailable")

    def test_sidechain_and_wrong_session_children_are_not_filtered_before_branch_count(self):
        for extra in ({"isSidechain": True}, {"sessionId": "other"}):
            with self.subTest(extra=extra):
                self.write([*chain(2), node("sibling", "root", **extra)])
                self.assert_lower_only(self.observe(), "unavailable")

    def test_duplicate_uuid_on_any_path_position_is_ambiguous(self):
        for uid in ("root", "attachment-1", "answer"):
            for first in (True, False):
                with self.subTest(uid=uid, duplicate_first=first):
                    duplicate = node(uid, "unrelated", kind="system")
                    records = [duplicate, *chain(2)] if first else [*chain(2), duplicate]
                    self.write(records)
                    self.assert_lower_only(self.observe(), "unavailable")

    def test_cycles_stop_without_claiming_response(self):
        r = root()
        r["parentUuid"] = "middle"
        self.write([r, node("middle", "root")])
        self.assert_lower_only(self.observe(), "unavailable")

    def test_non_attachment_intermediate_stops_the_claim(self):
        for kind in ("user", "system", "future-type"):
            with self.subTest(kind=kind):
                self.write([root(), node("middle", "root", kind), node("answer", "middle", "assistant")])
                self.assert_lower_only(self.observe(), "not_observed")

    def test_malformed_child_and_missing_flags_cannot_establish_ancestry(self):
        for field in ("uuid", "type", "sessionId", "isSidechain"):
            with self.subTest(missing=field):
                middle = node("middle", "root")
                del middle[field]
                self.write([root(), middle, node("answer", "middle", "assistant")])
                self.assert_lower_only(self.observe(), "unavailable")

    def test_hop_boundary_includes_the_assistant_edge(self):
        self.write(chain(16))
        obs = self.observe()
        self.assertEqual((obs.state, obs.level_reached, obs.source.get("hops")), ("observed", "responded", 16))
        self.write(chain(17))
        obs = self.observe()
        self.assert_lower_only(obs, "incomplete")
        self.assertEqual(obs.reason, "bound_reached")

    def test_unclassified_unrelated_line_can_hide_a_branch(self):
        self.write([*chain(2), b'{"broken":'])
        self.assert_lower_only(self.observe(), "incomplete")

    def test_complete_json_without_final_newline_is_not_a_complete_snapshot(self):
        self.write(chain(2), trailing_newline=False)
        self.assert_lower_only(self.observe(), "incomplete")

    def test_recorded_only_does_not_require_whole_graph_validity(self):
        self.write([*chain(2), node("sibling", "root"), b'BROKEN'])
        obs = self.observe("recorded")
        self.assertEqual((obs.state, obs.level_reached), ("observed", "recorded"))

    def test_unrelated_append_is_outside_the_captured_snapshot(self):
        self.write(chain(2))
        actual_open = open
        path = self.path
        class AppendOnFirstRead:
            def __init__(self, fh):
                self.fh, self.did_append = fh, False
            def __enter__(self):return self
            def __exit__(self, *args):self.fh.close()
            def fileno(self):return self.fh.fileno()
            def readline(self, *args):
                if not self.did_append:
                    self.did_append = True
                    with actual_open(path, "ab") as writer:
                        writer.write(json.dumps(node("new-sibling", "root")).encode() + b"\n")
                return self.fh.readline(*args)
            def __getattr__(self, name):return getattr(self.fh, name)
        def wrapped_open(*args, **kwargs):
            fh = actual_open(*args, **kwargs)
            return AppendOnFirstRead(fh) if str(args[0]) == str(path) and "r" in args[1] else fh
        with mock.patch("builtins.open", side_effect=wrapped_open):
            obs = self.observe()
        self.assertEqual((obs.state, obs.level_reached), ("observed", "responded"))
        # The next snapshot must see the appended sibling and refuse the claim.
        self.assert_lower_only(self.observe(), "unavailable")

    def test_replacement_during_scan_preserves_lower_evidence(self):
        self.write(chain(2))
        original_stat = os.stat
        real_path = self.path
        calls = 0
        def replaced(path, *args, **kwargs):
            nonlocal calls
            result = original_stat(path, *args, **kwargs)
            if str(path) == str(real_path):
                calls += 1
                values = list(result)
                values[1] += 100  # different inode at the same path
                return os.stat_result(values)
            return result
        with mock.patch.object(claude.os, "stat", side_effect=replaced):
            obs = self.observe()
        self.assertGreater(calls, 0)
        self.assert_lower_only(obs, "incomplete")


if __name__ == "__main__":
    unittest.main()
