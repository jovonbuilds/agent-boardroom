"""Independent Codex transport checks; only temporary mock sockets are used.

These test the boundary between definitely unsent and possibly delivered, since
mistaking the latter for failure can cause duplicate actions in the receiving chat.
"""
import sys as _sys, pathlib as _pl  # noqa: E401
_sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # the repo root holds the modules
import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock

from agent_boardroom import codex
from agent_boardroom.common import BoardroomError, DeliveryUnknown, Session


def take(sock, count):
    result = b""
    while len(result) < count:
        chunk = sock.recv(count - len(result))
        if not chunk:
            raise RuntimeError("unexpected EOF from test client")
        result += chunk
    return result


def read_frame(sock):
    first, second = take(sock, 2)
    if not second & 128:
        raise AssertionError("client frames must be masked")
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", take(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", take(sock, 8))[0]
    mask, payload = take(sock, 4), take(sock, length)
    return first & 15, bytes(value ^ mask[i % 4] for i, value in enumerate(payload))


def frame(opcode, payload, final=True):
    size = len(payload)
    header = bytes([(128 if final else 0) | opcode])
    if size < 126:
        header += bytes([size])
    elif size < 65536:
        header += bytes([126]) + struct.pack("!H", size)
    else:
        header += bytes([127]) + struct.pack("!Q", size)
    return header + payload


def handshake(sock):
    header = b""
    while not header.endswith(b"\r\n\r\n"):
        header += take(sock, 1)
    key = next(line.split(b": ", 1)[1] for line in header.split(b"\r\n")
               if line.startswith(b"Sec-WebSocket-Key:"))
    accept = base64.b64encode(hashlib.sha1(key + codex.WS_GUID.encode()).digest())
    # Case-insensitive header names/upgrade tokens, case-sensitive accept value.
    sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUPGRADE: WebSocket\r\n"
                 b"Connection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")


class SocketTests(unittest.TestCase):
    def run_peer(self, peer, client_work):
        failures = []
        with tempfile.TemporaryDirectory(prefix="br-codex-") as directory:
            path = Path(directory) / "s.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(path))
            listener.listen(1)
            listener.settimeout(5)

            def serve():
                try:
                    with listener.accept()[0] as sock:
                        sock.settimeout(5)
                        peer(sock)
                except BaseException as error:
                    failures.append(error)
                finally:
                    listener.close()

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                client_work(path)
            finally:
                thread.join(6)
            self.assertFalse(thread.is_alive(), "mock server did not stop")
            if failures:
                raise failures[0]

    def test_real_socket_with_unicode_large_frame_ping_fragments_and_notifications(self):
        def peer(sock):
            handshake(sock)
            for method in ("initialize", "thread/queue/add"):
                opcode, payload = read_frame(sock)
                request = json.loads(payload)
                self.assertEqual(opcode, 1)
                self.assertEqual(request["method"], method)
                if method == "thread/queue/add":
                    self.assertEqual(request["params"]["input"][0]["text"], "Résumé\n" + "x" * 70000)
                response = json.dumps({"id": request["id"], "result": {"ok": True}}).encode()
                middle = len(response) // 2
                sock.sendall(frame(1, b'{"method":"thread/queue/changed","params":{}}')
                             + frame(1, response[:middle], False)
                             + frame(9, b"ping") + frame(0, response[middle:]))
                self.assertEqual(read_frame(sock), (10, b"ping"))
                if method == "initialize":
                    self.assertEqual(json.loads(read_frame(sock)[1])["method"], "initialized")

        def work(path):
            client = codex.Client(path)
            try:
                result = client.call("thread/queue/add", {"input": [{"type": "text", "text": "Résumé\n" + "x" * 70000}]}, mutating=True)
                self.assertEqual(result, {"ok": True})
            finally:
                client.close()
        self.run_peer(peer, work)

    def test_peer_disconnect_after_mutation_is_unknown(self):
        def peer(sock):
            handshake(sock)
            request = json.loads(read_frame(sock)[1])
            sock.sendall(frame(1, json.dumps({"id": request["id"], "result": {}}).encode()))
            read_frame(sock)  # initialized notification
            self.assertEqual(json.loads(read_frame(sock)[1])["method"], "thread/queue/add")
            # A real socket closes after reading the request, without acknowledging it.

        def work(path):
            client = codex.Client(path)
            try:
                with self.assertRaises(DeliveryUnknown):
                    client.call("thread/queue/add", {}, mutating=True)
            finally:
                client.close()
        self.run_peer(peer, work)

    def test_malformed_handshake_is_clean_failure(self):
        def peer(sock):
            take(sock, 1)
            sock.sendall(b"BAD\r\n\r\n")
        self.run_peer(peer, lambda path: self.assertRaises(BoardroomError, codex.Client, path))


class FailureTests(unittest.TestCase):
    def client(self, response=None):
        client = codex.Client.__new__(codex.Client)
        client.sock = mock.Mock()
        client.counter = 0
        client.timeout = 1
        client.buffer = bytearray()
        client.read_json = mock.Mock(return_value=response)
        return client

    def test_partial_write_is_unknown_and_not_retried(self):
        client = self.client()
        client.sock.sendall.side_effect = OSError("write interrupted")
        with self.assertRaises(DeliveryUnknown):
            client.call("thread/queue/add", {}, mutating=True)
        client.sock.sendall.assert_called_once()

    def test_oversized_request_is_definitely_unsent(self):
        client = self.client()
        with mock.patch.object(codex, "MAX_BYTES", 5):
            with self.assertRaises(BoardroomError) as caught:
                client.call("thread/queue/add", {}, mutating=True)
        self.assertNotIsInstance(caught.exception, DeliveryUnknown)
        client.sock.sendall.assert_not_called()

    def test_serialization_error_is_definitely_unsent(self):
        client = self.client()
        with self.assertRaises(BoardroomError) as caught:
            client.call("thread/queue/add", {"bad": object()}, mutating=True)
        self.assertNotIsInstance(caught.exception, DeliveryUnknown)
        client.sock.sendall.assert_not_called()

    def test_malformed_rpc_after_write_is_unknown(self):
        for response in ([], None, {"id": 1}, {"id": 1, "error": "bad"},
                         {"id": 1, "error": {"code": -1, "message": "bad"}, "result": {}}):
            with self.subTest(response=response):
                with self.assertRaises(DeliveryUnknown):
                    self.client(response).call("thread/queue/add", {}, mutating=True)

    def test_explicit_rpc_rejection_is_definite(self):
        client = self.client({"id": 1, "error": {"code": -32601, "message": "unsupported"}})
        with self.assertRaises(codex.ServerError):
            client.call("thread/queue/add", {}, mutating=True)

    def test_missing_queue_receipt_is_unknown(self):
        for result in ({}, [], None, {"queuedSubmission": {"id": None}}):
            with self.subTest(result=result):
                client = mock.Mock()
                client.call.side_effect = [{"thread": {"id": "abc", "status": {"type": "idle"}}}, result]
                with mock.patch.object(codex, "Client", return_value=client):
                    with self.assertRaises(DeliveryUnknown):
                        codex.send(Session("codex", "abc"), "hello", "mid", "claude:xyz")

    def test_bad_liveness_or_mismatched_id_prevents_send(self):
        for record in ({"id": "abc"}, {"id": "other", "status": {"type": "idle"}},
                       {"id": "abc", "status": {"type": "notLoaded"}},
                       {"id": "abc", "status": {"type": "systemError"}},
                       {"id": "abc", "status": {"type": "future-status"}}):
            with self.subTest(record=record):
                client = mock.Mock()
                client.call.return_value = {"thread": record}
                with mock.patch.object(codex, "Client", return_value=client):
                    with self.assertRaises(BoardroomError):
                        codex.send(Session("codex", "abc"), "hello", "mid", "claude:xyz")
                self.assertEqual([c.args[0] for c in client.call.call_args_list], ["thread/read"])

    def test_future_status_remains_visible_for_ambiguity_checks(self):
        client = mock.Mock()
        client.call.side_effect = [
            {"data": ["future-id"], "nextCursor": None},
            {"thread": {"id": "future-id", "name": "review", "status": {"type": "future-status"}}},
        ]
        sessions = codex._loaded(client)
        self.assertEqual([(s.id, s.status) for s in sessions], [("future-id", "future-status")])

    def test_legacy_json_metadata_is_preserved(self):
        session = codex._session({"id": "abc", "source": "cli", "parentThreadId": "parent",
                                  "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}})
        self.assertEqual(session.detail, {"source": "cli", "parentThreadId": "parent",
                                         "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}})

    def test_rename_readback_mismatch_is_unknown(self):
        client = mock.Mock()
        record = {"thread": {"id": "abc", "name": "old", "status": {"type": "idle"}}}
        client.call.side_effect = [record, {}, record]
        with mock.patch.object(codex, "Client", return_value=client):
            with self.assertRaises(DeliveryUnknown):
                codex.rename(Session("codex", "abc"), "new")

    def test_pagination_cycle_fails_instead_of_hanging(self):
        client = mock.Mock()
        client.call.return_value = {"data": [], "nextCursor": "same-cursor"}
        with self.assertRaises(BoardroomError):
            codex._loaded(client)
        self.assertEqual(client.call.call_count, 2)

    def test_invalid_timeout_rejected_before_connect(self):
        for value in (-1, 0, float("nan"), float("inf")):
            with self.assertRaises(BoardroomError):
                codex.Client(timeout=value)

    def test_advertised_empty_identity_is_an_error_not_terminal_fallback(self):
        with mock.patch.dict(codex.os.environ, {"CODEX_THREAD_ID": ""}):
            with self.assertRaises(BoardroomError):
                codex.whoami()

    def test_sender_identity_does_not_require_send_target_readiness(self):
        for status in ("systemError", "future-status"):
            with self.subTest(status=status):
                client = mock.Mock()
                client.call.return_value = {"thread": {"id": "sender", "status": {"type": status}}}
                with mock.patch.dict(codex.os.environ, {"CODEX_THREAD_ID": "sender"}), \
                     mock.patch.object(codex, "Client", return_value=client):
                    self.assertEqual(codex.whoami().address, "codex:sender")

    def test_environment_timeout_rejects_invalid_values_before_connect(self):
        for value in ("bad", "0", "nan", "inf"):
            with mock.patch.dict(codex.os.environ, {"AGENT_BOARDROOM_CODEX_TIMEOUT": value}):
                with self.assertRaises(BoardroomError):
                    codex.Client()


if __name__ == "__main__":
    unittest.main()
