"""Offline tests for agent-boardroom's CLI, envelope, log and Claude backend.

Run:  python3 -m unittest discover -s ~/Desktop/boardroom -p 'test_*.py' -v

No live Claude or Codex session is touched. The Claude backend is exercised against a real Unix
socket server in a temp directory, with a fake session registry. Codex backend tests live in
test_codex_backend.py (owned by Codex). Live behavior is the manual checklist in README.md.
"""
import sys as _sys, pathlib as _pl  # noqa: E401
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # the repo root holds the modules
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import multiprocessing
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
import threading
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent  # repo root
sys.path.insert(0, str(HERE))

from agent_boardroom import claude as br_claude  # noqa: E402
from agent_boardroom import common as br_common  # noqa: E402
from agent_boardroom.common import BoardroomError, DeliveryUnknown, LogError, Session  # noqa: E402

from agent_boardroom import cli  # noqa: E402

# Claude Code 2.1.290's envelope parser, transcribed from the binary. It matches this regex and then
# rebuilds the wrapper from the parsed fields, requiring an identical string. from-name is rebuilt
# with Jh(), modeled below. If our wrapper fails either step, Claude still receives the text but
# loses the structured sender fields.
F = r"A-Za-z0-9%:_/.\\-"
CLAUDE_PARSE = re.compile(
    rf'<cross-session-message(?: from="([{F}]+)")?(?: from-session="([A-Za-z0-9_-]{{1,80}})")?'
    rf'(?: hop-chain="([0-9a-f]{{24}}(?:,[0-9a-f]{{24}}){{0,31}})")?(?: from-name="([^"<>\n\r]+)")?'
    rf'(?: from-mode="(bypass|prompting)")?(?: from-plugin="([^"<>\n\r]+)")?>\n([\s\S]*)\n</cross-session-message>')


def claude_jh(name):
    """Model of Claude's Jh(): drop Cc/Cf/Cs/Zl/Zp, trim, truncate to 64 code points + '…'."""
    s = "".join(ch for ch in name if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Zl", "Zp")).strip()
    return s[:64] + "…" if len(s) > 64 else s


def claude_accepts(text):
    """True if Claude's parser would accept the envelope with its structured fields intact."""
    m = CLAUDE_PARSE.fullmatch(text)
    return bool(m) and (m.group(4) is None or claude_jh(m.group(4).replace('"', "")) == m.group(4))


CLAUDE_ME = Session("claude", "11111111-1111-4111-8111-111111111111", "alpha-session")
CODEX_THEM = Session("codex", "22222222-2222-4222-8222-222222222222", "beta-thread")
CLAUDE_OTHER = Session("claude", "aaaaaaaa-0000-0000-0000-000000000000", "third-party")


class TempHome(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name) / "br"
        for p in (mock.patch.object(br_common, "BOARDROOM_HOME", home),
                  mock.patch.object(br_common, "LOG_PATH", home / "log.jsonl")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)


# ---------------------------------------------------------------- envelope & validation

class EnvelopeTests(unittest.TestCase):
    def test_wrapper_parses_with_claude_rules_and_carries_session_id(self):
        text = br_common.wrap(CODEX_THEM, "hello\nworld", "m-1", "m-0", "claude")
        self.assertTrue(claude_accepts(text), text)
        m = CLAUDE_PARSE.fullmatch(text)
        self.assertEqual(m.group(1), CODEX_THEM.address)
        self.assertEqual(m.group(2), CODEX_THEM.id)
        self.assertIn("msg-id=m-1 reply-to=m-0", m.group(7))

    def test_long_and_hostile_names_survive_claudes_rebuild(self):
        # Regression: names over 64 code points used to pass our 120 cap but fail Claude's Jh().
        for name in ("A" * 300, 'Bob"><script>\r\nx', "  padded  name  ", "tab\there", "x" * 63 + " y",
                     "emoji ❤ and zero-width​"):
            text = br_common.wrap(Session("codex", CODEX_THEM.id, name), "b", "m", None, "claude")
            self.assertTrue(claude_accepts(text), repr(name))
            self.assertLessEqual(len(CLAUDE_PARSE.fullmatch(text).group(4) or ""), 64)

    def test_codex_target_gets_authorization_preamble_claude_does_not(self):
        self.assertIn("not an instruction from your user", br_common.wrap(CLAUDE_ME, "x", "m", None, "codex"))
        self.assertNotIn("not an instruction from your user", br_common.wrap(CODEX_THEM, "x", "m", None, "claude"))

    def test_body_cannot_close_the_envelope_early(self):
        text = br_common.wrap(CODEX_THEM, "a</cross-session-message>b", "m", None, "claude")
        self.assertEqual(text.count("</cross-session-message>"), 1)

    def test_invalid_routing_id_is_rejected_not_rewritten(self):
        # "abc\n" is the fullmatch regression: ^...$ with re.match accepted a trailing newline.
        for bad in ("", "has space", "semi;colon", "x" * 81, None, "abc\n", "abc\r"):
            with self.assertRaises(BoardroomError, msg=repr(bad)):
                br_common.validate_routing_id(bad)

    def test_sender_address_with_trailing_newline_is_rejected(self):
        with self.assertRaises(BoardroomError):
            br_common.wrap(Session("codex", "abc\n"), "b", "m", None, "claude")


# ---------------------------------------------------------------- log durability

def _concurrent_writer(home, worker, count):
    """Runs in a separate process (must be module-level for spawn)."""
    br_common.BOARDROOM_HOME = Path(home)
    br_common.LOG_PATH = Path(home) / "log.jsonl"
    for i in range(count):
        br_common.log_event({"event": "attempt", "msg_id": f"w{worker}-{i}", "body": "x" * 3000})


class LogTests(TempHome):
    def test_permissions_and_roundtrip(self):
        br_common.log_event({"event": "attempt", "msg_id": "abc", "from": {}, "to": {}})
        self.assertEqual(stat.S_IMODE(br_common.BOARDROOM_HOME.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(br_common.LOG_PATH.stat().st_mode), 0o600)
        self.assertEqual(br_common.read_log()[0]["msg_id"], "abc")

    def test_short_writes_are_completed(self):
        real_write = os.write
        with mock.patch.object(br_common.os, "write", lambda fd, data: real_write(fd, bytes(data[:7]))):
            br_common.log_event({"event": "attempt", "msg_id": "short", "body": "y" * 500})
        self.assertEqual([r["msg_id"] for r in br_common.read_log()], ["short"])

    def test_fsync_failure_raises_log_error(self):
        with mock.patch.object(br_common.os, "fsync", side_effect=OSError(5, "EIO")):
            with self.assertRaises(LogError):
                br_common.log_event({"event": "attempt", "msg_id": "x"})

    def test_torn_tail_is_repaired_not_glued(self):
        br_common.log_event({"event": "attempt", "msg_id": "first"})
        with open(br_common.LOG_PATH, "a") as fh:
            fh.write('{"event": "attempt", "msg_id": "torn')  # writer died mid-line
        br_common.log_event({"event": "attempt", "msg_id": "after"})
        self.assertEqual([r["msg_id"] for r in br_common.read_log()], ["first", "after"])

    def test_concurrent_writers_never_interleave(self):
        br_common.BOARDROOM_HOME.mkdir(mode=0o700, parents=True)
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_concurrent_writer, args=(str(br_common.BOARDROOM_HOME), w, 40))
                 for w in range(6)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        lines = br_common.LOG_PATH.read_text().splitlines()
        self.assertEqual(len(lines), 240)
        for line in lines:
            json.loads(line)  # every line intact

    def test_find_message_prefix_and_ambiguity(self):
        for mid in ("aaaa-1", "aaaa-2", "bbbb-1"):
            br_common.log_event({"event": "attempt", "msg_id": mid})
        self.assertEqual(br_common.find_message("bbbb")["msg_id"], "bbbb-1")
        with self.assertRaises(BoardroomError):
            br_common.find_message("aaaa")
        with self.assertRaises(BoardroomError):
            br_common.find_message("zzzz")


# ---------------------------------------------------------------- CLI with fake backends

class FakeBackend:
    def __init__(self, sessions=(), whoami=None, send_exc=None, list_exc=None,
                 levels=("recorded", "responded"), observations=()):
        self.sessions, self._whoami = list(sessions), whoami
        self.send_exc, self.list_exc, self.sent = send_exc, list_exc, []
        self.levels, self.observations, self.observed = levels, list(observations), []

    def supported_levels(self):
        return self.levels

    def observe(self, session, msg_id, level, deadline):
        self.observed.append((session.address, msg_id, level))
        if self.observations:
            return self.observations.pop(0)
        return br_common.Observation("not_observed", None, br_common.utc_now(), {"backend": session.backend})

    def list_sessions(self):
        if self.list_exc:
            raise self.list_exc
        return list(self.sessions)

    def refresh(self, s):
        return s

    def whoami(self):
        if isinstance(self._whoami, Exception):
            raise self._whoami
        return self._whoami

    def send(self, session, text, msg_id, sender_address):
        # The attempt must already be durably on disk when the backend is called (D3).
        assert any(r.get("msg_id") == msg_id and r["event"] == "attempt" for r in br_common.read_log())
        if self.send_exc:
            raise self.send_exc
        self.sent.append((session, text, msg_id, sender_address))
        return {"transport": "fake"}


class CliTests(TempHome):
    def use(self, claude, codex):
        p = mock.patch.dict(cli.BACKEND, {"claude": claude, "codex": codex})
        p.start()
        self.addCleanup(p.stop)

    # identity
    def test_two_verified_identities_is_an_error(self):
        self.use(FakeBackend(whoami=CLAUDE_ME), FakeBackend(whoami=CODEX_THEM))
        with self.assertRaises(BoardroomError):
            cli.me()
        self.assertEqual(cli.me("codex").address, CODEX_THEM.address)

    def test_broken_advertised_identity_is_not_silently_ignored(self):
        self.use(FakeBackend(whoami=CLAUDE_ME), FakeBackend(whoami=BoardroomError("stale CODEX_THREAD_ID")))
        with self.assertRaises(BoardroomError):
            cli.me()
        self.assertEqual(cli.me("claude").address, CLAUDE_ME.address)  # explicit --as resolves it

    def test_single_broken_identity_fails(self):
        self.use(FakeBackend(whoami=BoardroomError("no live registry entry")), FakeBackend())
        with self.assertRaises(BoardroomError):
            cli.me()

    def test_no_agent_identity_falls_back_to_terminal(self):
        self.use(FakeBackend(), FakeBackend())
        self.assertEqual(cli.me().backend, "terminal")

    # resolution
    def test_resolve_exact_then_prefix_and_ambiguity(self):
        twin = Session("codex", "22222222-ffff", "Other")
        self.use(FakeBackend([CLAUDE_ME]), FakeBackend([CODEX_THEM, twin]))
        self.assertEqual(cli.resolve("alpha").address, CLAUDE_ME.address)
        self.assertEqual(cli.resolve(f"codex:{twin.id}").address, twin.address)
        with self.assertRaises(BoardroomError):
            cli.resolve("codex:22222222")

    def test_bare_target_fails_closed_when_a_backend_is_down(self):
        self.use(FakeBackend([CLAUDE_ME]), FakeBackend(list_exc=BoardroomError("daemon down")))
        with self.assertRaises(BoardroomError):
            cli.resolve("alpha")
        self.assertEqual(cli.resolve("claude:alpha").address, CLAUDE_ME.address)

    def test_resolve_exact_never_falls_back_to_names(self):
        impostor = Session("claude", "bbbbbbbb-0000", name=CODEX_THEM.id)  # named after a dead id
        self.use(FakeBackend([impostor]), FakeBackend([]))
        with self.assertRaises(BoardroomError):
            cli.resolve_exact("claude", CODEX_THEM.id)
        with self.assertRaises(BoardroomError):
            cli.resolve_exact("claude", "bbbbbbbb")  # prefix of a real id: still no

    def test_resolve_exact_refuses_duplicate_canonical_ids(self):
        # Regression: duplicates used to resolve to the first one.
        first = Session("claude", "same-id", "first", detail={"pid": 111})
        second = Session("claude", "same-id", "second", detail={"pid": 222})
        self.use(FakeBackend([first, second]), FakeBackend())
        with self.assertRaises(BoardroomError) as ctx:
            cli.resolve_exact("claude", "same-id")
        self.assertIn("2 times", str(ctx.exception))
        with self.assertRaises(BoardroomError):  # the human-input resolver already refused these
            cli.resolve("claude:same-id")

    # delivery and outcomes
    def test_send_logs_attempt_then_backend_specific_outcome(self):
        codex = FakeBackend([CODEX_THEM])
        self.use(FakeBackend([CLAUDE_ME]), codex)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.deliver(CLAUDE_ME, CODEX_THEM, "hi")
        log = br_common.read_log()
        self.assertEqual([r["event"] for r in log], ["attempt", "outcome"])
        self.assertEqual(log[1]["outcome"], "accepted")
        self.assertTrue(out.getvalue().startswith("accepted:"))
        self.assertEqual(codex.sent[0][3], CLAUDE_ME.address)

    def test_claude_success_is_reported_as_written_not_accepted(self):
        self.use(FakeBackend([CLAUDE_OTHER]), FakeBackend())
        out = io.StringIO()
        with redirect_stdout(out):
            cli.deliver(CODEX_THEM, CLAUDE_OTHER, "hi")
        self.assertEqual(br_common.read_log()[-1]["outcome"], "written")
        self.assertTrue(out.getvalue().startswith("written:"))

    def test_attempt_log_failure_sends_nothing(self):
        codex = FakeBackend([CODEX_THEM])
        self.use(FakeBackend(), codex)
        # v0.3: the attempt goes through reserve_attempt (guards + append under one lock).
        with mock.patch.object(cli, "reserve_attempt", side_effect=LogError("disk full")):
            with self.assertRaises(BoardroomError) as ctx:
                cli.deliver(CLAUDE_ME, CODEX_THEM, "hi")
        self.assertIn("NOT sent", str(ctx.exception))
        self.assertEqual(codex.sent, [])

    def test_outcome_log_failure_after_delivery_exits_3_not_failed(self):
        codex = FakeBackend([CODEX_THEM])
        self.use(FakeBackend(), codex)
        real = br_common.log_event
        calls = []

        def flaky(rec):
            calls.append(rec["event"])
            if rec["event"] == "outcome":
                raise LogError("disk full")
            real(rec)
        with mock.patch.object(cli, "log_event", flaky), redirect_stdout(io.StringIO()), \
                redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as ctx:
            cli.deliver(CLAUDE_ME, CODEX_THEM, "hi")
        self.assertEqual(ctx.exception.code, 3)
        self.assertEqual(len(codex.sent), 1)
        self.assertIn("Do NOT resend", err.getvalue())

    def test_delivery_unknown_is_logged_and_exits_2_without_retry(self):
        codex = FakeBackend([CODEX_THEM], send_exc=DeliveryUnknown("timeout"))
        self.use(FakeBackend(), codex)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.deliver(CLAUDE_ME, CODEX_THEM, "hi")
        self.assertEqual(ctx.exception.code, 2)
        outcomes = [r for r in br_common.read_log() if r["event"] == "outcome"]
        self.assertEqual([o["outcome"] for o in outcomes], ["delivery-unknown"])

    def test_definite_failure_is_logged_as_failed(self):
        self.use(FakeBackend(), FakeBackend([CODEX_THEM], send_exc=BoardroomError("refused")))
        with self.assertRaises(BoardroomError):
            cli.deliver(CLAUDE_ME, CODEX_THEM, "hi")
        self.assertEqual(br_common.read_log()[-1]["outcome"], "failed")

    def test_refuses_to_message_itself(self):
        with self.assertRaises(BoardroomError):
            cli.deliver(CLAUDE_ME, CLAUDE_ME, "hi")

    # reply
    def _log_original(self):
        br_common.log_event({"event": "attempt", "msg_id": "orig-1", "reply_to": None,
                             "from": CODEX_THEM.public(), "to": CLAUDE_ME.public(), "body": "q"})

    def test_reply_targets_the_other_party_with_reply_to(self):
        codex = FakeBackend([CODEX_THEM])
        self.use(FakeBackend([CLAUDE_ME], whoami=CLAUDE_ME), codex)
        self._log_original()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli.cmd_reply(mock.Mock(as_backend=None, no_guard=None, wait=None, wait_timeout=None, msg_id="orig", message=["answer"]))
        session, text, _, _ = codex.sent[0]
        self.assertEqual(session.address, CODEX_THEM.address)
        self.assertIn("reply-to=orig-1", text)

    def test_reply_to_a_failed_original_is_a_new_message_with_a_warning(self):
        codex = FakeBackend([CODEX_THEM])
        self.use(FakeBackend([CLAUDE_ME], whoami=CLAUDE_ME), codex)
        self._log_original()
        br_common.log_event({"event": "outcome", "msg_id": "orig-1", "outcome": "failed"})
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
            cli.cmd_reply(mock.Mock(as_backend=None, no_guard=None, wait=None, wait_timeout=None, msg_id="orig", message=["answer"]))
        self.assertEqual(len(codex.sent), 1)  # sent once, as a new message: no retransmission
        self.assertIn("failed", err.getvalue())

    def test_log_rejects_nonpositive_n_and_flags_torn_lines(self):
        with self.assertRaises(BoardroomError):
            cli.cmd_log(mock.Mock(n=0, json=False, full=False, follow=False))
        br_common.log_event({"event": "attempt", "msg_id": "a1", "from": {}, "to": {}})
        with open(br_common.LOG_PATH, "a") as fh:
            fh.write("{torn\n")
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
            cli.cmd_log(mock.Mock(n=5, json=False, full=False, follow=False))
        self.assertIn("1 unreadable log line", err.getvalue())
        self.assertIn("delivery unknown", out.getvalue())  # attempt without outcome

    def test_third_party_cannot_reply_as_if_addressed(self):
        codex = FakeBackend([CODEX_THEM])
        self.use(FakeBackend([CLAUDE_OTHER], whoami=CLAUDE_OTHER), codex)
        self._log_original()
        with self.assertRaises(BoardroomError):
            cli.cmd_reply(mock.Mock(as_backend=None, no_guard=None, wait=None, wait_timeout=None, msg_id="orig", message=["butting in"]))
        self.assertEqual(codex.sent, [])

    def test_reply_compares_backend_not_just_id(self):
        # A codex session that happens to share the Claude recipient's id is not that recipient.
        lookalike = Session("codex", CLAUDE_ME.id, "lookalike")
        codex = FakeBackend([CODEX_THEM, lookalike], whoami=lookalike)
        self.use(FakeBackend([CLAUDE_ME]), codex)
        self._log_original()
        with self.assertRaises(BoardroomError):
            cli.cmd_reply(mock.Mock(as_backend=None, no_guard=None, wait=None, wait_timeout=None, msg_id="orig", message=["x"]))


# ---------------------------------------------------------------- Claude backend, real socket

class ClaudeBackendSocketTests(unittest.TestCase):
    """Exercise br_claude against a real Unix socket and a fake ~/.claude/sessions registry."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp", prefix="brt")  # short path: AF_UNIX limit is ~104
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.sess = os.path.join(self.dir, "sessions")
        os.mkdir(self.sess, 0o700)
        p = mock.patch.object(br_claude, "SESS_DIR", self.sess)
        p.start()
        self.addCleanup(p.stop)
        self.sock = os.path.join(self.dir, "s.sock")
        self.pid = os.getpid()
        with open(os.path.join(self.sess, f"{self.pid}.json"), "w") as fh:
            json.dump({"pid": self.pid, "sessionId": "sid-target", "name": "target",
                       "messagingSocketPath": self.sock, "status": "idle", "cwd": "/"}, fh)
        exact = hashlib.sha256(self.sock.encode()).hexdigest()
        for digest, token in (("0" * 64, "decoy-token"), (exact, "right-token")):
            with open(os.path.join(self.sess, f"{self.pid}.{digest}.key"), "w") as fh:
                json.dump({"peerToken": token}, fh)
        self.received, self.connections = [], 0

    def serve(self):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock)
        srv.listen(1)
        srv.settimeout(3)

        def run():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            self.connections += 1
            buf = b""
            with conn:
                while chunk := conn.recv(65536):
                    buf += chunk
            self.received = [json.loads(line) for line in buf.decode().splitlines() if line]
        t = threading.Thread(target=run)
        t.start()
        self.addCleanup(srv.close)
        return t

    def target(self):
        return br_claude.refresh(Session("claude", "sid-target"))

    def test_send_uses_exact_key_session_id_and_no_from_mode(self):
        t = self.serve()
        receipt = br_claude.send(self.target(), "<cross-session-message>x</cross-session-message>",
                                 "m-1", "codex:abc")
        t.join(3)
        auth, frame = self.received
        self.assertEqual(auth, {"type": "auth", "token": "right-token"})
        self.assertEqual(frame["type"], "user")
        self.assertEqual(frame["session_id"], "sid-target")
        self.assertEqual(frame["from"], "codex:abc")
        self.assertNotIn("from_mode", frame)
        self.assertNotIn("from-mode", frame["message"]["content"])
        self.assertEqual(receipt["confirmation"], "transport-written")

    def test_connection_refused_is_a_definite_failure(self):
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(self.sock)  # socket file exists, nobody listening
        dead.close()
        with self.assertRaises(BoardroomError) as ctx:
            br_claude.send(self.target(), "x", "m", "codex:abc")
        self.assertNotIsInstance(ctx.exception, DeliveryUnknown)

    def test_oversize_payload_is_refused_before_connecting(self):
        t = self.serve()
        body = "\x01" * 200_000  # json-escapes to \u0001: ~1.2M UTF-16 units from 200 KB
        with self.assertRaises(BoardroomError) as ctx:
            br_claude.send(self.target(), body, "m", "codex:abc")
        self.assertNotIsInstance(ctx.exception, DeliveryUnknown)
        t.join(4)
        self.assertEqual(self.connections, 0)

    def test_rename_readback_failure_after_write_is_unknown_not_failed(self):
        # Regression: the session vanishing during read-back used to raise a
        # plain BoardroomError, which means exit 1 / "safe to retry".
        t = self.serve()  # the socket must exist for the session to count as live
        target = self.target()
        with mock.patch.object(br_claude, "refresh", side_effect=BoardroomError("session gone")):
            with self.assertRaises(DeliveryUnknown):
                br_claude.rename(target, "new-name")
        t.join(3)
        self.assertEqual(self.received[-1]["action"], "rename")
        self.assertEqual(self.received[-1]["session_id"], "sid-target")

    def test_duplicate_live_session_id_is_refused_not_first_picked(self):
        # Regression: two live processes advertising one sessionId, each with
        # its own PID and socket. refresh/whoami used to return whichever record globbed first.
        t = self.serve()
        other_sock = os.path.join(self.dir, "dup.sock")
        open(other_sock, "w").close()
        with open(os.path.join(self.sess, f"{os.getppid()}.json"), "w") as fh:
            json.dump({"pid": os.getppid(), "sessionId": "sid-target", "name": "twin",
                       "messagingSocketPath": other_sock}, fh)
        with self.assertRaises(BoardroomError) as ctx:
            br_claude.refresh(Session("claude", "sid-target"))
        self.assertIn("2 processes", str(ctx.exception))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sid-target"}):
            with self.assertRaises(BoardroomError):
                br_claude.whoami()
        with self.assertRaises(BoardroomError):  # the send path refuses too, before any write
            cli.deliver(CODEX_THEM, Session("claude", "sid-target", detail={"pid": self.pid, "sock": self.sock}), "x")
        os.remove(os.path.join(self.sess, f"{os.getppid()}.json"))
        self.assertEqual(br_claude.refresh(Session("claude", "sid-target")).detail["pid"], self.pid)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:  # release the server thread
            s.connect(self.sock)
        t.join(3)

    def test_dead_pid_records_are_not_live(self):
        with open(os.path.join(self.sess, "999999.json"), "w") as fh:
            json.dump({"pid": 999999, "sessionId": "ghost", "messagingSocketPath": self.sock}, fh)
        open(self.sock, "w").close()
        ids = {s.id for s in br_claude.list_sessions()}
        self.assertIn("sid-target", ids)
        self.assertNotIn("ghost", ids)

    def test_whoami_with_stale_env_raises(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "not-registered"}):
            with self.assertRaises(BoardroomError):
                br_claude.whoami()
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(br_claude.whoami())


if __name__ == "__main__":
    unittest.main()


# ================================================================ v0.3 (docs/DESIGN.md)

class SafeTextTests(unittest.TestCase):
    """§D: whole control sequences are consumed, legitimate text after them survives."""

    def test_sequences_are_consumed_and_suffix_preserved(self):
        cases = {
            "red \x1b[31mALERT\x1b[0m done": "red ALERT done",
            "c1 csi \x9b2Jcleared": "c1 csi cleared",
            "link \x1b]8;;http://evil\x07text\x1b]8;;\x07 after": "link text after",
            "clip \x1b]52;c;ZXZpbA==\x1b\\ after": "clip  after",
            "rlo ‮evil‬ lri ⁦x⁩ ok": "rlo evil lri x ok",
            "over\rwrite": "overwrite",
            "tab\there": "tab here",
            "bell\x07 nul\x00 del\x7f end": "bell nul del end",
        }
        for raw, want in cases.items():
            self.assertEqual(br_common.safe_text(raw), want, repr(raw))

    def test_unterminated_osc_drops_only_introducer(self):
        out = br_common.safe_text("start \x1b]0;never terminated title, still readable")
        self.assertNotIn("\x1b", out)
        self.assertIn("still readable", out)

    def test_overlong_sequence_is_bounded(self):
        out = br_common.safe_text("\x1b[" + "1;" * 2000 + "m tail")
        self.assertNotIn("\x1b", out)
        self.assertTrue(out.endswith("m tail"))

    def test_one_line_flattens_newlines_body_keeps_them(self):
        self.assertEqual(br_common.safe_text("evil\nFAKE ROW", one_line=True), "evil FAKE ROW")
        self.assertEqual(br_common.safe_text("line1\nline2"), "line1\nline2")

    def test_json_safe_has_no_literal_controls(self):
        out = br_common.json_dumps_safe({"b": "x\x1b[2J‮y"})
        self.assertNotIn("\x1b", out)
        self.assertNotIn("‮", out)
        self.assertEqual(json.loads(out)["b"], "x\x1b[2J‮y")  # value preserved exactly


def _attempt(mid, reply_to=None, frm="claude:a", to="codex:b", body="x", ts=None):
    fb, fi = frm.split(":", 1)
    tb, ti = to.split(":", 1)
    rec = {"event": "attempt", "msg_id": mid, "reply_to": reply_to, "body": body,
           "from": {"backend": fb, "id": fi}, "to": {"backend": tb, "id": ti}}
    rec["ts"] = ts if ts is not None else __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z")
    return rec


class GuardTests(unittest.TestCase):
    """§C: depth root=0/child=parent+1 (8 ok, 9 refused), validated ancestry, rate, duplicates."""

    def chain(self, n):
        recs = [_attempt("m0")]
        for i in range(1, n + 1):
            recs.append(_attempt(f"m{i}", reply_to=f"m{i - 1}"))
        return recs

    def test_depth_boundaries(self):
        recs = self.chain(7)  # m7 has depth 7
        self.assertEqual(br_common.reply_depth(recs, None), (0, None))
        self.assertEqual(br_common.reply_depth(recs, "m7"), (8, None))
        br_common.check_guards(recs, "claude:a", "codex:b", "new", "m7")  # depth 8: allowed
        recs = self.chain(8)
        with self.assertRaises(BoardroomError):  # depth 9: refused
            br_common.check_guards(recs, "claude:a", "codex:b", "new", "m8")
        br_common.check_guards(recs, "claude:a", "codex:b", "new", "m8", disabled=("depth",))

    def test_missing_ambiguous_malformed_ancestors_are_refused(self):
        recs = [_attempt("m1", reply_to="gone")]
        with self.assertRaises(BoardroomError):
            br_common.check_guards(recs, "claude:a", "codex:b", "x", "m1")
        recs = [_attempt("dup"), _attempt("dup")]
        with self.assertRaises(BoardroomError):
            br_common.check_guards(recs, "claude:a", "codex:b", "x", "dup")
        recs = [_attempt("m1", reply_to="bad id\n")]
        with self.assertRaises(BoardroomError):
            br_common.check_guards(recs, "claude:a", "codex:b", "x", "m1")
        # an unrelated damaged chain never blocks a root send
        br_common.check_guards([_attempt("m1", reply_to="gone")], "claude:a", "codex:b", "root", None)

    def test_cycle_never_loops_even_with_guard_disabled(self):
        recs = [_attempt("a", reply_to="b"), _attempt("b", reply_to="a")]
        depth, problem = br_common.reply_depth(recs, "a")
        self.assertIn("cycle", problem)
        br_common.check_guards(recs, "claude:a", "codex:b", "fresh body", "a", disabled=("depth",))  # returns

    def test_rate_counts_failed_and_malformed_or_future_timestamps(self):
        now = __import__("time").time()
        recs = [_attempt(f"r{i}", body=f"b{i}") for i in range(28)]
        recs.append({"event": "outcome", "msg_id": "r0", "outcome": "failed"})
        recs.append(_attempt("bad-ts", body="z", ts="not a time"))
        br_common.check_guards(recs, "claude:a", "codex:b", "new", None, now=now)  # 29 < 30
        recs.append(_attempt("future", body="y", ts="2999-01-01T00:00:00+0000"))
        with self.assertRaises(BoardroomError):  # 30 counted (failed + malformed + future included)
            br_common.check_guards(recs, "claude:a", "codex:b", "new", None, now=now)
        br_common.check_guards(recs, "claude:a", "codex:b", "new", None, disabled=("rate",), now=now)
        # a different sender is unaffected
        br_common.check_guards(recs, "claude:other", "codex:b", "new", None, now=now)

    def test_duplicate_suppression_excludes_definitively_failed(self):
        recs = [_attempt("d1", body="same")]
        with self.assertRaises(BoardroomError):
            br_common.check_guards(recs, "claude:a", "codex:b", "same", None)
        br_common.check_guards(recs, "claude:a", "codex:b", "different", None)
        br_common.check_guards(recs, "claude:a", "codex:other", "same", None)  # recipient is in the key
        recs.append({"event": "outcome", "msg_id": "d1", "outcome": "failed"})
        br_common.check_guards(recs, "claude:a", "codex:b", "same", None)  # failed: resend is legit
        recs = [_attempt("d2", body="same"), {"event": "outcome", "msg_id": "d2", "outcome": "delivery-unknown"}]
        with self.assertRaises(BoardroomError):  # unknown still counts
            br_common.check_guards(recs, "claude:a", "codex:b", "same", None)


def _reserve_worker(home, n, out_q):
    br_common.BOARDROOM_HOME = Path(home)
    br_common.LOG_PATH = Path(home) / "log.jsonl"
    br_common.RATE_LIMIT = 5
    ok = 0
    for i in range(n):
        try:
            br_common.reserve_attempt(_attempt(f"{os.getpid()}-{i}", body=f"{os.getpid()}-{i}"),
                                      "claude:a", "codex:b", f"{os.getpid()}-{i}", None)
            ok += 1
        except BoardroomError:
            pass
    out_q.put(ok)


class AtomicReservationTests(TempHome):
    def test_concurrent_senders_cannot_exceed_the_rate_limit(self):
        br_common.BOARDROOM_HOME.mkdir(mode=0o700, parents=True)
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_reserve_worker, args=(str(br_common.BOARDROOM_HOME), 4, q)) for _ in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        total = sum(q.get(timeout=5) for _ in procs)
        self.assertEqual(total, 5)  # 16 tries, limit 5: check+append is atomic
        self.assertEqual(sum(1 for r in br_common.read_log() if r["event"] == "attempt"), 5)

    def test_record_observation_is_deduplicated(self):
        obs = br_common.Observation("observed", "recorded", br_common.utc_now(),
                                    {"backend": "claude", "session_id": "s", "entry_uuid": "m"})
        self.assertTrue(br_common.record_observation("m", "recorded", obs))
        self.assertFalse(br_common.record_observation("m", "recorded", obs))
        self.assertEqual(sum(1 for r in br_common.read_log() if r["event"] == "observation"), 1)


class ClaudeObserveTests(unittest.TestCase):
    """§A Claude receipts against fixture transcripts (sidechain/API-error/mismatch can't be seen live)."""

    SID, MID = "sid-dest", "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="brobs")
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir})
        p.start()
        self.addCleanup(p.stop)
        self.cwd = "/Users/x/my.proj_dir"
        self.proj = os.path.join(self.dir, "projects", br_claude._slug(self.cwd))
        os.makedirs(self.proj)
        self.session = Session("claude", self.SID, cwd=self.cwd)

    def write(self, records, sid=None, where=None):
        path = os.path.join(where or self.proj, f"{sid or self.SID}.jsonl")
        with open(path, "w") as fh:
            for r in records:
                fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        return path

    def user(self, **kw):
        rec = {"type": "user", "uuid": self.MID, "sessionId": self.SID, "isSidechain": False,
               "timestamp": "2026-10-05T01:00:00Z", "origin": {"kind": "peer", "msg_id": self.MID},
               "message": {"content": "SECRET BODY"}}
        rec.update(kw)
        return rec

    def asst(self, **kw):
        rec = {"type": "assistant", "uuid": "out-1", "parentUuid": self.MID, "sessionId": self.SID,
               "isSidechain": False, "timestamp": "2026-10-05T01:00:02Z"}
        rec.update(kw)
        return rec

    def observe(self, level="responded"):
        import time as _t
        return br_claude.observe(self.session, self.MID, level, _t.monotonic() + 5)

    def test_recorded_then_responded_via_direct_child(self):
        self.write([{"type": "queue-operation"}, self.user()])
        o = self.observe("responded")
        self.assertEqual((o.state, o.level_reached), ("not_observed", "recorded"))
        self.assertEqual(self.observe("recorded").state, "observed")
        self.write([self.user(), self.asst()])
        o = self.observe("responded")
        self.assertEqual((o.state, o.level_reached), ("observed", "responded"))
        self.assertEqual(o.source["output_uuid"], "out-1")
        self.assertEqual(o.evidence_at, "2026-10-05T01:00:02.000Z")  # normalized: UTC, ms, Z
        self.assertNotIn("SECRET", json.dumps(o.__dict__))  # no content leaks into the observation

    def test_sidechain_api_error_and_unlinked_outputs_do_not_count(self):
        for bad in (self.asst(isSidechain=True), self.asst(isApiErrorMessage=True),
                    self.asst(parentUuid="someone-else"), self.asst(sessionId="other")):
            self.write([self.user(), bad])
            self.assertEqual(self.observe().level_reached, "recorded", bad)
        self.write([self.user(isSidechain=True), self.asst()])
        self.assertEqual(self.observe().level_reached, None)  # responded requires recorded

    def test_session_mismatch_schema_and_missing(self):
        self.write([self.user(sessionId="someone-else")])
        o = self.observe()
        self.assertEqual((o.state, o.reason), ("unavailable", "session_mismatch"))
        self.write([self.user(origin=None)])
        self.assertEqual(self.observe().reason, "unsupported_schema")
        os.remove(os.path.join(self.proj, f"{self.SID}.jsonl"))
        o = self.observe()
        self.assertEqual((o.state, o.reason), ("unavailable", "history_missing"))

    def test_glob_fallback_needs_exactly_one_match(self):
        other = os.path.join(self.dir, "projects", "elsewhere")
        os.makedirs(other)
        self.write([self.user()], where=other)
        self.session = Session("claude", self.SID, cwd="/not/the/real/cwd")
        self.assertEqual(self.observe("recorded").state, "observed")
        self.write([self.user()], where=self.proj)
        o = self.observe("recorded")
        self.assertEqual((o.state, o.reason), ("unavailable", "ambiguous_location"))

    def test_bound_reached_is_incomplete_and_keeps_lower_evidence(self):
        self.write([self.user()] + ["{\"type\":\"filler\",\"pad\":\"%s\"}" % ("x" * 500)] * 50 + [self.asst()])
        with mock.patch.object(br_claude, "SCAN_MAX_BYTES", 5000):
            o = self.observe("responded")
        self.assertEqual((o.state, o.level_reached, o.reason), ("incomplete", "recorded", "bound_reached"))

    def test_unsupported_level_and_torn_lines(self):
        self.write(['{"type":"user","uuid":"' + self.MID + '", torn', self.user()])
        self.assertEqual(self.observe("recorded").state, "observed")
        self.assertEqual(self.observe("turn_completed").reason, "unsupported_level")


class V03CliTests(TempHome):
    def use(self, claude, codex):
        p = mock.patch.dict(cli.BACKEND, {"claude": claude, "codex": codex})
        p.start()
        self.addCleanup(p.stop)

    def obs(self, state, level=None):
        return br_common.Observation(state, level, br_common.utc_now(),
                                     {"backend": "codex", "session_id": CODEX_THEM.id, "item_id": "i1"})

    def test_wait_options_validated_before_any_transport(self):
        codex = FakeBackend([CODEX_THEM], levels=("recorded",))
        self.use(FakeBackend(), codex)
        for kw in ({"wait": "responded"}, {"wait": None, "wait_timeout": 5.0},
                   {"wait": "recorded", "wait_timeout": -1.0}, {"wait": "recorded", "wait_timeout": float("inf")}):
            with self.assertRaises(BoardroomError, msg=kw):
                cli.deliver(CLAUDE_ME, CODEX_THEM, "hi", **kw)
        self.assertEqual(codex.sent, [])
        self.assertEqual(br_common.read_log(), [])  # nothing reserved either

    def test_wait_observed_records_and_exits_0(self):
        codex = FakeBackend([CODEX_THEM], observations=[self.obs("not_observed"), self.obs("observed", "recorded")])
        self.use(FakeBackend(), codex)
        with redirect_stdout(io.StringIO()) as out:
            cli.deliver(CLAUDE_ME, CODEX_THEM, "hi", wait="recorded", wait_timeout=5.0)
        self.assertIn("receipt: recorded", out.getvalue())
        obs = [r for r in br_common.read_log() if r["event"] == "observation"]
        self.assertEqual([(o["level"], o["state"]) for o in obs], [("recorded", "observed")])

    def test_wait_not_established_exits_4_never_resends(self):
        codex = FakeBackend([CODEX_THEM])
        self.use(FakeBackend(), codex)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as ctx:
            cli.deliver(CLAUDE_ME, CODEX_THEM, "hi", wait="recorded", wait_timeout=0.3)
        self.assertEqual(ctx.exception.code, 4)
        self.assertEqual(len(codex.sent), 1)
        self.assertIn("DO NOT RESEND", err.getvalue())
        self.assertEqual(br_common.read_log()[1]["outcome"], "accepted")  # transport outcome untouched

    def test_wait_observation_not_saved_exits_3(self):
        codex = FakeBackend([CODEX_THEM], observations=[self.obs("observed", "recorded")])
        self.use(FakeBackend(), codex)
        with mock.patch.object(cli, "record_observation", side_effect=LogError("disk full")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as ctx:
            cli.deliver(CLAUDE_ME, CODEX_THEM, "hi", wait="recorded", wait_timeout=5.0)
        self.assertEqual(ctx.exception.code, 3)
        self.assertIn("not saved", err.getvalue())

    def _log_sent(self):
        br_common.log_event(dict(_attempt("orig-1", frm=CLAUDE_ME.address, to=CODEX_THEM.address),
                                 **{"to": dict(CODEX_THEM.public())}))

    def test_receipt_inspection_exit_codes_and_record(self):
        self._log_sent()
        cases = [("observed", "recorded", 0), ("not_observed", None, 0), ("incomplete", "recorded", 1),
                 ("unavailable", None, 1), ("error", None, 1)]
        for state, lvl, code in cases:
            o = br_common.Observation(state, lvl, br_common.utc_now(), {"backend": "codex"},
                                      reason=None if state in ("observed", "not_observed") else "probe_failed")
            self.use(FakeBackend(), FakeBackend(levels=("recorded",), observations=[o]))
            args = mock.Mock(msg_id="orig", level=None, record=False, timeout=5.0, json=False)
            with redirect_stdout(io.StringIO()) as out:
                if code:
                    with self.assertRaises(SystemExit) as ctx:
                        cli.cmd_receipt(args)
                    self.assertEqual(ctx.exception.code, code, state)
                else:
                    cli.cmd_receipt(args)
            self.assertIn("does not assess whether a resend is safe", out.getvalue())
        self.assertFalse(any(r["event"] == "observation" for r in br_common.read_log()))  # read-only
        self.use(FakeBackend(), FakeBackend(levels=("recorded",), observations=[self.obs("observed", "recorded")]))
        with redirect_stdout(io.StringIO()):
            cli.cmd_receipt(mock.Mock(msg_id="orig", level=None, record=True, timeout=5.0, json=False))
        self.assertTrue(any(r["event"] == "observation" for r in br_common.read_log()))

    def test_reply_to_must_be_logged_unless_depth_guard_disabled(self):
        with self.assertRaises(BoardroomError):
            cli.normalize_reply_to("deadbeef", ())
        self.assertEqual(cli.normalize_reply_to("deadbeef-0000", ("depth",)), "deadbeef-0000")
        self._log_sent()
        self.assertEqual(cli.normalize_reply_to("orig", ()), "orig-1")  # prefix normalized to full id

    def test_derived_reply_labels_are_strict(self):
        recs = [_attempt("M", frm="claude:a", to="codex:b"),
                _attempt("R1", reply_to="M", frm="codex:b", to="claude:a"),
                {"event": "outcome", "msg_id": "R1", "outcome": "written"},
                _attempt("R2", reply_to="M", frm="codex:b", to="claude:a"),
                {"event": "outcome", "msg_id": "R2", "outcome": "failed"},
                _attempt("T", reply_to="M", frm="claude:third", to="codex:b")]
        _, _, replies, refs = cli.derive_status(recs)
        self.assertEqual(replies["M"], ("reply_written", "R1"))  # failed R2 never overwrites
        self.assertEqual(refs["M"], ["T"])  # third party is "referenced by", not a reply
        recs.append({"event": "observation", "msg_id": "R1", "level": "recorded", "state": "observed"})
        self.assertEqual(cli.derive_status(recs)[2]["M"], ("reply_recorded", "R1"))

    def test_log_rendering_is_terminal_safe(self):
        evil = Session("codex", CODEX_THEM.id, "Bob\x1b]0;pwned\x07\nFAKE ROW")
        br_common.log_event(_attempt("e1", frm=CLAUDE_ME.address, to=CODEX_THEM.address,
                                     body="hi \x1b[2J‮evil"))
        recs = br_common.read_log()
        recs[0]["to"] = evil.public()
        out = cli._fmt(recs[0], "accepted", False)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("‮", out)
        self.assertEqual(out.count("\n"), 1)  # the name couldn't forge an extra row


class BoundTests(unittest.TestCase):
    """Read, subprocess and registry bounds."""

    def test_overlong_line_is_read_in_bounded_pieces(self):
        t = ClaudeObserveTests("test_unsupported_level_and_torn_lines")
        t.setUp()
        self.addCleanup(t.doCleanups)
        long_other = '{"type":"filler","pad":"%s"}' % ("y" * 5000)
        long_mention = '{"type":"user","pad":"%s","ref":"%s"}' % ("z" * 5000, t.MID)
        with mock.patch.object(br_claude, "LINE_MAX", 1024):
            t.write([long_other, t.user()])  # an unrelated long record doesn't hide our match
            self.assertEqual(t.observe("recorded").state, "observed")
            t.write([long_mention])  # a long record naming our id can't be evaluated: not absence
            o = t.observe("recorded")
            self.assertEqual((o.state, o.reason), ("incomplete", "probe_failed"))

    def test_run_bounded_limits_time_output_and_expired_deadlines(self):
        import time as _t
        py = sys.executable
        self.assertEqual(br_common.run_bounded([py, "-c", "print('ok 1.2.3')"], _t.monotonic() + 10), "ok 1.2.3")
        with self.assertRaises(BoardroomError):  # output bound
            br_common.run_bounded([py, "-c", "print('x' * 100000)"], _t.monotonic() + 10, limit=1000)
        start = _t.monotonic()
        with self.assertRaises(BoardroomError):  # time bound; the child is reaped, not left running
            br_common.run_bounded([py, "-c", "import time; time.sleep(30)"], _t.monotonic() + 10, seconds=0.5)
        self.assertLess(_t.monotonic() - start, 5)
        with self.assertRaises(BoardroomError):  # an expired shared deadline runs nothing
            br_common.run_bounded([py, "-c", "print(1)"], _t.monotonic() - 1)

    def test_doctor_registry_scan_is_bounded(self):
        d = tempfile.mkdtemp(prefix="brdoc")
        self.addCleanup(shutil.rmtree, d, True)
        for i in range(5):
            with open(os.path.join(d, f"{100000 + i}.json"), "w") as fh:
                json.dump({"pid": 999999, "messagingSocketPath": "/nonexistent"}, fh)
        import time as _t
        with mock.patch.object(br_claude, "SESS_DIR", d), mock.patch.object(br_claude, "DOCTOR_MAX_RECORDS", 2), \
                mock.patch.object(br_common, "run_bounded", side_effect=BoardroomError("x")), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            rows = br_claude.doctor(_t.monotonic() + 10)
        sessions = next(r for r in rows if r["check"] == "claude sessions")
        self.assertEqual(sessions["status"], "warn")
        self.assertIn("partial", sessions["detail"])


class ReservationTimeTests(TempHome):
    def test_timestamp_is_assigned_after_the_lock_and_guard(self):
        # Regression: a writer that waited on the lock must not append the time
        # it STARTED waiting; the record's ts is the reservation time.
        stamps = iter(["2026-01-01T00:00:00+0000", "2026-01-01T00:05:00+0000"])
        real_strftime = __import__("time").strftime

        def fake_strftime(fmt, *rest):
            return next(stamps) if fmt == "%Y-%m-%dT%H:%M:%S%z" and not rest else real_strftime(fmt, *rest)

        def slow_check(recs):
            next(stamps)  # time passes while we hold/wait on the lock: the first stamp is consumed
        with mock.patch.object(br_common.time, "strftime", fake_strftime):
            br_common.locked_append({"event": "attempt", "msg_id": "t1"}, check=slow_check)
        self.assertEqual(br_common.read_log()[0]["ts"], "2026-01-01T00:05:00+0000")

    def test_find_message_refuses_duplicate_records_for_one_id(self):
        for _ in range(2):
            br_common.log_event({"event": "attempt", "msg_id": "same-id", "reply_to": None})
        with self.assertRaises(BoardroomError):
            br_common.find_message("same-id")
        with self.assertRaises(BoardroomError):
            br_common.find_message("same")


class FinalReviewTests(TempHome):
    def test_receipt_record_failure_still_prints_positive_evidence_human_mode(self):
        br_common.log_event(dict(_attempt("orig-9", frm=CLAUDE_ME.address, to=CODEX_THEM.address),
                                 **{"to": dict(CODEX_THEM.public())}))
        o = br_common.Observation("observed", "recorded", br_common.utc_now(),
                                  {"backend": "codex", "item_id": "i9"}, "2026-01-01T00:00:00.000Z")
        with mock.patch.dict(cli.BACKEND, {"claude": FakeBackend(), "codex": FakeBackend(levels=("recorded",),
                                                                                        observations=[o])}), \
                mock.patch.object(cli, "record_observation", side_effect=LogError("disk full")), \
                redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as ctx:
            cli.cmd_receipt(mock.Mock(msg_id="orig-9", level=None, record=True, timeout=5.0, json=False))
        self.assertEqual(ctx.exception.code, 3)
        self.assertIn("level_reached=recorded", out.getvalue())
        self.assertIn("i9", out.getvalue())
        self.assertIn("not saved", err.getvalue())

    def test_transcript_fallback_is_bounded_and_stops_at_second_match(self):
        d = tempfile.mkdtemp(prefix="brfb")
        self.addCleanup(shutil.rmtree, d, True)
        projects = os.path.join(d, "projects")
        for i in range(30):
            os.makedirs(os.path.join(projects, f"p{i:02d}"))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": d}):
            self.assertEqual(br_claude.locate_transcript("sid-x", None), (None, "history_missing"))
            with mock.patch.object(br_claude, "FALLBACK_MAX_DIRS", 5):
                self.assertEqual(br_claude.locate_transcript("sid-x", None), (None, "bound_reached"))
            import time as _t
            self.assertEqual(br_claude.locate_transcript("sid-x", None, _t.monotonic() - 1), (None, "bound_reached"))
            for i in (3, 7):
                open(os.path.join(projects, f"p{i:02d}", "sid-x.jsonl"), "w").close()
            self.assertEqual(br_claude.locate_transcript("sid-x", None), (None, "ambiguous_location"))


class BusyAttachmentTests(unittest.TestCase):
    """Contract amendment (Codex ed2f8eb5): busy Claude deliveries are queued_command attachments.

    recorded: the exact attachment identity; responded: a DIRECT child of the attachment only.
    Walking through intermediate attachments is deliberately deferred, so that case stays recorded-only.
    """

    def setUp(self):
        self.t = ClaudeObserveTests("test_unsupported_level_and_torn_lines")
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.MID, self.SID = self.t.MID, self.t.SID

    def att(self, **kw):
        a = {"type": "queued_command", "source_uuid": self.MID, "delivery_id": "d-1", "commandMode": "prompt",
             "origin": {"kind": "peer", "msg_id": self.MID, "body": "SECRET BODY"}, "isMeta": True}
        a.update(kw.pop("attachment", {}))
        rec = {"type": "attachment", "uuid": "att-uuid-1", "parentUuid": "p0", "sessionId": self.SID,
               "isSidechain": False, "timestamp": "2026-10-05T02:00:00Z", "attachment": a}
        rec.update(kw)
        return rec

    def reply(self, parent, **kw):
        rec = {"type": "assistant", "uuid": "out-9", "parentUuid": parent, "sessionId": self.SID,
               "isSidechain": False, "timestamp": "2026-10-05T02:00:03Z"}
        rec.update(kw)
        return rec

    def test_exact_attachment_is_recorded_with_its_own_entry_uuid(self):
        self.t.write([self.att()])
        o = self.t.observe("recorded")
        self.assertEqual((o.state, o.level_reached), ("observed", "recorded"))
        self.assertEqual(o.source["entry_uuid"], "att-uuid-1")
        self.assertEqual(o.source["delivery"], "attachment")
        self.assertNotIn("SECRET", json.dumps(o.__dict__))

    def test_direct_child_of_the_attachment_is_responded(self):
        # The assistant line names the ATTACHMENT uuid, not the msg_id; the scan must still find it.
        self.t.write([self.att(), self.reply("att-uuid-1")])
        o = self.t.observe("responded")
        self.assertEqual((o.state, o.level_reached), ("observed", "responded"))
        self.assertEqual(o.source["output_uuid"], "out-9")

    def test_intermediate_attachment_reaches_responded_v031(self):
        # The real-world shape (fc9392c9): attachment -> reminder attachment -> assistant. Under v0.3
        # this was recorded-only; D34 (v0.3.1) resolves it through attachment-only ancestry.
        reminder = {"type": "attachment", "uuid": "rem-1", "parentUuid": "att-uuid-1", "sessionId": self.SID,
                    "isSidechain": False, "attachment": {"type": "total_tokens_reminder"}}
        self.t.write([self.att(), reminder, self.reply("rem-1")])
        o = self.t.observe("responded")
        self.assertEqual((o.state, o.level_reached, o.source["hops"]), ("observed", "responded", 2))

    def test_conflicting_ids_session_and_sidechain_are_excluded(self):
        self.t.write([self.att(attachment={"source_uuid": "someone-else"})])
        o = self.t.observe("recorded")
        self.assertEqual((o.state, o.reason), ("unavailable", "unsupported_schema"))
        self.t.write([self.att(attachment={"origin": {"kind": "peer", "msg_id": "someone-else"}})])
        self.assertEqual(self.t.observe("recorded").reason, "unsupported_schema")
        self.t.write([self.att(sessionId="other-session")])
        self.assertEqual(self.t.observe("recorded").reason, "session_mismatch")
        self.t.write([self.att(isSidechain=True)])
        self.assertEqual(self.t.observe("recorded").state, "not_observed")
        self.t.write([self.att(attachment={"origin": {"kind": "user", "msg_id": self.MID}})])
        self.assertEqual(self.t.observe("recorded").state, "not_observed")
        self.t.write([self.att(attachment={"type": "something_else"})])
        self.assertEqual(self.t.observe("recorded").state, "not_observed")
        self.t.write([self.att(uuid=None)])
        self.assertEqual(self.t.observe("recorded").state, "not_observed")

    def test_idle_user_path_still_works_alongside(self):
        self.t.write([self.t.user(), self.t.asst()])
        o = self.t.observe("responded")
        self.assertEqual((o.level_reached, o.source["delivery"]), ("responded", "user"))


class D34ChainTests(unittest.TestCase):
    """D34 / v0.3.1: busy responded via attachment-only ancestry, resolved from the WHOLE snapshot."""

    def setUp(self):
        self.t = ClaudeObserveTests("test_unsupported_level_and_torn_lines")
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.b = BusyAttachmentTests("test_exact_attachment_is_recorded_with_its_own_entry_uuid")
        self.b.t, self.b.MID, self.b.SID = self.t, self.t.MID, self.t.SID
        self.SID = self.t.SID

    def node(self, uid, parent, typ="attachment", **kw):
        rec = {"type": typ, "uuid": uid, "parentUuid": parent, "sessionId": self.SID,
               "isSidechain": False, "timestamp": "2026-10-05T03:00:00Z"}
        if typ == "attachment":
            rec["attachment"] = {"type": "total_tokens_reminder"}
        rec.update(kw)
        return rec

    def root(self):
        return self.b.att()  # uuid att-uuid-1

    def chain(self, n):
        """root -> (n-1) attachments -> assistant: n edges."""
        recs, parent = [self.root()], "att-uuid-1"
        for i in range(n - 1):
            recs.append(self.node(f"a{i}", parent))
            parent = f"a{i}"
        recs.append(self.node("answer", parent, "assistant"))
        return recs

    def obs(self, recs, level="responded"):
        self.t.write(recs)
        return self.t.observe(level)

    def assertLower(self, o, state):
        self.assertEqual((o.state, o.level_reached), (state, "recorded"))
        self.assertNotIn("output_uuid", o.source)

    def test_chain_lengths_and_cap(self):
        for n in (1, 2, 3, 16):
            o = self.obs(self.chain(n))
            self.assertEqual((o.state, o.level_reached, o.source.get("hops")), ("observed", "responded", n), n)
        o = self.obs(self.chain(17))
        self.assertLower(o, "incomplete")
        self.assertEqual(o.reason, "bound_reached")

    def test_early_sibling_before_root_is_a_branch(self):
        # Counterexample: B (child of the root) appears BEFORE the root in the file.
        recs = [self.node("B", "att-uuid-1"), self.root(), self.node("C", "att-uuid-1"),
                self.node("D", "C", "assistant")]
        self.assertLower(self.obs(recs), "unavailable")

    def test_child_before_parent_with_valid_structure_resolves(self):
        recs = [self.node("answer", "a0", "assistant"), self.root(), self.node("a0", "att-uuid-1")]
        o = self.obs(recs)
        self.assertEqual((o.level_reached, o.source.get("hops")), ("responded", 2))

    def test_branches_duplicates_and_cycles_refuse(self):
        cases = {
            "sibling after assistant": self.chain(2) + [self.node("late", "att-uuid-1")],
            "sidechain sibling": self.chain(2) + [self.node("side", "att-uuid-1", isSidechain=True)],
            "sole sidechain child": [self.root(), self.node("a0", "att-uuid-1", isSidechain=True),
                                     self.node("answer", "a0", "assistant")],
            "duplicate root": [self.root()] + self.chain(2)[1:] + [dict(self.root(), parentUuid="zz")],
            "duplicate intermediate": self.chain(2) + [self.node("a0", "elsewhere")],
            "duplicate endpoint": self.chain(2) + [self.node("answer", "elsewhere", "assistant")],
            "cycle": [self.root(), self.node("x", "att-uuid-1"), self.node("y", "x"),
                      dict(self.node("x2", "y"), uuid="x")],
            "child without uuid": [self.root(), {k: v for k, v in self.node("a0", "att-uuid-1").items() if k != "uuid"}],
            "oversized id": [self.root(), self.node("a" * 300, "att-uuid-1"),
                             self.node("answer", "a" * 300, "assistant")],
        }
        for name, recs in cases.items():
            o = self.obs(recs)
            self.assertEqual(o.level_reached, "recorded", name)
            self.assertIn(o.state, ("unavailable", "incomplete"), name)

    def test_walk_stops_without_claim_at_user_system_or_error(self):
        for typ, kw in (("user", {}), ("system", {}), ("assistant", {"isApiErrorMessage": True})):
            recs = [self.root(), self.node("a0", "att-uuid-1"), self.node("x", "a0", typ, **kw)]
            self.assertLower(self.obs(recs), "not_observed")
        self.assertLower(self.obs([self.root()]), "not_observed")  # nothing after it yet
        recs = [self.root(), self.node("a0", "att-uuid-1", sessionId="other"), self.node("answer", "a0", "assistant")]
        o = self.obs(recs)
        self.assertEqual((o.state, o.reason), ("unavailable", "session_mismatch"))

    def test_unclassified_lines_and_partial_final_line_block_the_claim(self):
        self.assertLower(self.obs(self.chain(2) + ['{"broken":']), "incomplete")
        path = os.path.join(self.t.proj, f"{self.SID}.jsonl")
        with open(path, "w") as fh:
            fh.write("\n".join(json.dumps(r) for r in self.chain(2)))  # no trailing newline
        self.assertLower(self.t.observe("responded"), "incomplete")

    def test_snapshot_shrink_replace_or_same_size_rewrite_blocks_the_claim(self):
        self.t.write(self.chain(2))
        real_fstat, real_stat = os.fstat, os.stat
        for scenario in ("shrink", "replace", "rewrite"):
            calls = {"n": 0}

            def fake_fstat(fd, _s=scenario):
                st = real_fstat(fd)
                calls["n"] += 1
                if calls["n"] == 1:
                    return st
                vals = list(st)
                if _s == "shrink":
                    vals[6] = st.st_size - 1
                    return os.stat_result(vals)
                if _s == "rewrite":
                    return os.stat_result(vals[:8] + [st.st_mtime + 5, st.st_ctime + 5],
                                          {"st_mtime_ns": st.st_mtime_ns + 5_000_000_000,
                                           "st_ctime_ns": st.st_ctime_ns + 5_000_000_000})
                return st

            def fake_stat(p, *a, _s=scenario, **k):
                st = real_stat(p, *a, **k)
                if _s == "replace" and str(p).endswith(".jsonl"):
                    vals = list(st)
                    vals[1] = st.st_ino + 1
                    return os.stat_result(vals)
                return st

            with mock.patch.object(br_claude.os, "fstat", fake_fstat), mock.patch.object(br_claude.os, "stat", fake_stat):
                o = self.t.observe("responded")
            self.assertLower(o, "incomplete")
            self.assertEqual(o.reason, "probe_failed", scenario)

    def test_pure_append_after_open_is_allowed(self):
        self.t.write(self.chain(2))
        real_fstat = os.fstat
        calls = {"n": 0}

        def grown(fd):
            st = real_fstat(fd)
            calls["n"] += 1
            if calls["n"] == 1:
                return st
            vals = list(st)
            vals[6] = st.st_size + 100
            return os.stat_result(vals[:8] + [st.st_mtime + 1, st.st_ctime + 1])

        with mock.patch.object(br_claude.os, "fstat", grown):
            o = self.t.observe("responded")
        self.assertEqual(o.level_reached, "responded")  # the captured prefix was complete and stable

    def test_index_caps_are_incomplete_not_silent(self):
        with mock.patch.object(br_claude, "NODE_CAP", 2):
            self.assertLower(self.obs(self.chain(3)), "incomplete")
        with mock.patch.object(br_claude, "INDEX_MAX_BYTES", 40):
            self.assertLower(self.obs(self.chain(3)), "incomplete")

    def test_recorded_and_idle_paths_keep_early_success(self):
        # recorded still returns before reading a broken tail
        self.assertEqual(self.obs(self.chain(2) + ['{"broken":'], level="recorded").state, "observed")
        self.t.write([self.t.user(), self.t.asst(), '{"broken":'])
        o = self.t.observe("responded")
        self.assertEqual((o.state, o.source["delivery"]), ("observed", "user"))  # idle direct child, early


class D34MemoryBoundTests(unittest.TestCase):
    """No raw containers retained; the budget is bytes-conservative for Unicode."""

    def setUp(self):
        self.c = D34ChainTests("test_chain_lengths_and_cap")
        self.c.setUp()
        self.addCleanup(self.c.t.doCleanups)

    def test_container_type_is_never_retained_raw(self):
        seen = {}
        real = br_claude._resolve_busy

        def spy(obs, root, child_count, first_child, nodes, *rest):
            seen["types"] = [n[0] for n in nodes.values()]
            return real(obs, root, child_count, first_child, nodes, *rest)

        recs = self.c.chain(2) + [self.c.node("unrelated", "elsewhere", typ={"payload": "x" * 5000})]
        with mock.patch.object(br_claude, "_resolve_busy", spy):
            o = self.c.obs(recs)
        self.assertEqual(o.level_reached, "responded")  # unrelated malformed record isn't on the path
        self.assertTrue(all(t is None or (isinstance(t, str) and len(t) <= br_claude.ID_MAX) for t in seen["types"]))
        self.assertNotIn({"payload": "x" * 5000}, seen["types"])

    def test_container_type_on_the_path_is_malformed(self):
        recs = [self.c.root(), self.c.node("a0", "att-uuid-1", typ=["attachment"]),
                self.c.node("answer", "a0", "assistant")]
        o = self.c.obs(recs)
        self.assertEqual((o.state, o.level_reached, o.reason), ("unavailable", "recorded", "unsupported_schema"))

    def test_budget_counts_unicode_conservatively(self):
        wide = "\U0001F600" * 200  # 200 code points, 800 UTF-8 bytes
        recs = self.c.chain(2) + [self.c.node(wide, "elsewhere")]
        retained = [x for r in recs for x in (r.get("uuid"), r.get("parentUuid"), r.get("type"), r.get("timestamp"))
                    if isinstance(x, str)]
        utf8 = sum(len(x.encode("utf-8")) for x in retained)
        code_points = sum(len(x) for x in retained)
        budget = utf8 - 1
        self.assertGreater(budget, code_points)  # a code-point counter would NOT trip this budget
        with mock.patch.object(br_claude, "INDEX_MAX_BYTES", budget):
            o = self.c.obs(recs)
        self.assertEqual((o.state, o.level_reached), ("incomplete", "recorded"))

    def test_oversized_child_still_counts_as_a_branch_reference(self):
        recs = self.c.chain(2) + [self.c.node("z" * 300, "att-uuid-1")]  # an over-long sibling of a0
        o = self.c.obs(recs)
        self.assertEqual((o.state, o.level_reached), ("unavailable", "recorded"))


class D34ErrorFlagTests(unittest.TestCase):
    """A malformed isApiErrorMessage must never certify a response."""

    def setUp(self):
        self.c = D34ChainTests("test_chain_lengths_and_cap")
        self.c.setUp()
        self.addCleanup(self.c.t.doCleanups)

    def endpoint(self, **kw):
        return [self.c.root(), self.c.node("a0", "att-uuid-1"), self.c.node("answer", "a0", "assistant", **kw)]

    def test_error_flag_states(self):
        for kw in ({}, {"isApiErrorMessage": False}):
            self.assertEqual(self.c.obs(self.endpoint(**kw)).level_reached, "responded", kw)
        o = self.c.obs(self.endpoint(isApiErrorMessage=True))
        self.assertEqual((o.state, o.level_reached), ("not_observed", "recorded"))
        for bad in ("true", 1, "yes", [True], {"x": 1}):
            o = self.c.obs(self.endpoint(isApiErrorMessage=bad))
            self.assertEqual((o.state, o.level_reached, o.reason), ("unavailable", "recorded", "unsupported_schema"), bad)


class F4TurnFailedTests(TempHome):
    """F4 (v0.3.2): --wait turn_completed stops early when the matched Codex turn is listed failed."""

    def use(self, codex):
        p = mock.patch.dict(cli.BACKEND, {"claude": FakeBackend(), "codex": codex})
        p.start()
        self.addCleanup(p.stop)

    def failed(self, level=None):
        return br_common.Observation("not_observed", level, br_common.utc_now(),
                                     {"backend": "codex", "item_id": "i1", "turn_id": "t1"}, reason="turn_failed")

    def test_reason_is_in_the_contract(self):
        self.assertEqual(self.failed("responded").reason, "turn_failed")

    def test_turn_failed_stops_immediately_with_exit_4(self):
        codex = FakeBackend([CODEX_THEM], levels=br_common.RECEIPT_LEVELS, observations=[self.failed("responded")])
        self.use(codex)
        import time as _t
        start = _t.monotonic()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(SystemExit) as ctx:
            cli.deliver(CLAUDE_ME, CODEX_THEM, "hi", wait="turn_completed", wait_timeout=20.0)
        self.assertEqual(ctx.exception.code, 4)
        self.assertLess(_t.monotonic() - start, 2.0)  # didn't poll to the 20 s deadline
        self.assertEqual(len(codex.observed), 1)  # exactly one probe
        self.assertEqual(len(codex.sent), 1)  # never resends
        msg = err.getvalue()
        self.assertIn("receiving turn failed", msg)
        self.assertIn("DO NOT RESEND", msg)
        self.assertIn("responded", msg)  # lower evidence reported
        self.assertNotIn("within", msg)  # no timeout wording

    def test_other_not_observed_keeps_polling(self):
        plain = br_common.Observation("not_observed", "recorded", br_common.utc_now(), {"backend": "codex"})
        codex = FakeBackend([CODEX_THEM], levels=br_common.RECEIPT_LEVELS, observations=[plain, plain, plain])
        self.use(codex)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.deliver(CLAUDE_ME, CODEX_THEM, "hi", wait="turn_completed", wait_timeout=1.2)
        self.assertEqual(ctx.exception.code, 4)
        self.assertGreater(len(codex.observed), 1)  # kept polling until the deadline

    def test_only_a_turn_completed_wait_stops_on_it(self):
        # Defensive: even if a backend emitted turn_failed for a responded wait, the CLI keeps polling.
        codex = FakeBackend([CODEX_THEM], levels=br_common.RECEIPT_LEVELS,
                            observations=[self.failed("recorded"), self.failed("recorded"), self.failed("recorded")])
        self.use(codex)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.deliver(CLAUDE_ME, CODEX_THEM, "hi", wait="responded", wait_timeout=1.2)
        self.assertGreater(len(codex.observed), 1)

    def test_receipt_reports_turn_failed_as_a_completed_inspection(self):
        br_common.log_event(dict(_attempt("orig-f", frm=CLAUDE_ME.address, to=CODEX_THEM.address),
                                 **{"to": dict(CODEX_THEM.public())}))
        self.use(FakeBackend(levels=br_common.RECEIPT_LEVELS, observations=[self.failed("responded")]))
        with redirect_stdout(io.StringIO()) as out:
            cli.cmd_receipt(mock.Mock(msg_id="orig-f", level=None, record=False, timeout=5.0, json=False))
        self.assertIn("reason=turn_failed", out.getvalue())  # exit 0: no SystemExit raised


class D33GateHintTests(TempHome):
    """D33 resolved as a hint: Claude --wait timeout with NO positive level across ALL probes."""

    HINT = "Claude may be awaiting approval"

    def run_wait(self, target, backend_name, observations, level="recorded"):
        fake = FakeBackend([target], levels=br_common.RECEIPT_LEVELS, observations=observations)
        p = mock.patch.dict(cli.BACKEND, {backend_name: fake, ("codex" if backend_name == "claude" else "claude"): FakeBackend()})
        p.start()
        self.addCleanup(p.stop)
        sender = CODEX_THEM if backend_name == "claude" else CLAUDE_ME
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as ctx:
            cli.deliver(sender, target, "hi", wait=level, wait_timeout=1.0)
        return ctx.exception.code, err.getvalue()

    def nothing(self, backend="claude"):
        return br_common.Observation("not_observed", None, br_common.utc_now(), {"backend": backend})

    def test_hint_on_claude_timeout_with_no_level_at_all(self):
        code, err = self.run_wait(CLAUDE_OTHER, "claude", [self.nothing()] * 5)
        self.assertEqual(code, 4)
        self.assertIn(self.HINT, err)
        self.assertIn("DO NOT RESEND", err)
        events = [r["event"] for r in br_common.read_log()]
        self.assertEqual(events, ["attempt", "outcome"])  # no new log events

    def test_no_hint_when_an_earlier_probe_saw_recorded(self):
        earlier = br_common.Observation("not_observed", "recorded", br_common.utc_now(), {"backend": "claude"})
        later = br_common.Observation("unavailable", None, br_common.utc_now(), {"backend": "claude"},
                                      reason="history_missing")
        code, err = self.run_wait(CLAUDE_OTHER, "claude", [earlier, later, later, later], level="responded")
        self.assertEqual(code, 4)
        self.assertNotIn(self.HINT, err)
        self.assertIn("highest level seen: recorded", err)

    def test_no_hint_for_codex_targets(self):
        code, err = self.run_wait(CODEX_THEM, "codex", [self.nothing("codex")] * 5)
        self.assertEqual(code, 4)
        self.assertNotIn(self.HINT, err)
