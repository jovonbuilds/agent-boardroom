"""Codex backend: the existing app-server daemon's WebSocket-over-Unix control socket.


Uses thread/loaded/list, thread/read, thread/queue/add (wakes a loaded idle thread; interrupted
threads are not auto-woken) and thread/name/set. Never starts a server or resumes an unloaded thread.
clientUserMessageId is NOT a dedup key: a timeout after the queue/add request is written is
delivery-unknown and must not be retried.
"""
import base64
import hashlib
import json
import math
import os
import socket
import shutil
import struct
import time
from pathlib import Path
from datetime import datetime, timezone

from .common import (BoardroomError, DeliveryUnknown, Session, Observation,
                       utc_now, validate_routing_id)

MAX_BYTES = 8 * 1024 * 1024
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ServerError(BoardroomError):
    """The server answered with a JSON-RPC error: a definite failure."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class ProbeBound(BoardroomError):
    """A read-only observation exhausted its shared time or byte budget."""


def socket_path():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return Path(os.environ.get("AGENT_BOARDROOM_CODEX_SOCKET") or home / "app-server-control" / "app-server-control.sock")


class Client:
    def __init__(self, path=None, timeout=None, *, deadline=None, max_receive_bytes=None):
        if timeout is None:
            try:
                # Per-RPC ceiling; receipt probes additionally share one absolute
                # deadline across the complete connection and traversal.
                timeout = float(os.environ.get("AGENT_BOARDROOM_CODEX_TIMEOUT", "10"))
            except ValueError as error:
                raise BoardroomError("AGENT_BOARDROOM_CODEX_TIMEOUT must be a number") from error
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise BoardroomError("Codex timeout must be a finite positive number")
        self.timeout = timeout
        if deadline is not None and (not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
            raise BoardroomError("Codex deadline must be finite")
        self.deadline = deadline
        self.max_receive_bytes = max_receive_bytes
        self.received_bytes = 0
        self.buffer = bytearray()
        self.counter = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.sock.settimeout(self._remaining())
            self.sock.connect(str(path or socket_path()))
            self._handshake()
            self.initialize_result = self.call("initialize", {
                "clientInfo": {"name": "agent-boardroom", "title": "agent-boardroom messenger", "version": "0.3.0"},
                "capabilities": {"experimentalApi": True},
            })
            self.send_json({"method": "initialized"})
        except BaseException:
            self.sock.close()
            raise

    def _remaining(self):
        deadline = getattr(self, "deadline", None)
        if deadline is None:
            return self.timeout
        left = deadline - time.monotonic()
        if left <= 0:
            raise ProbeBound("Codex observation deadline reached")
        return min(self.timeout, left)

    def _rpc_deadline(self):
        return time.monotonic() + self._remaining()

    def _handshake(self):
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.settimeout(self._remaining())
        self.sock.sendall((
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\n\r\n").encode())
        deadline = self._rpc_deadline()
        while b"\r\n\r\n" not in self.buffer:
            self._receive(deadline)
            end = self.buffer.find(b"\r\n\r\n")
            if (end if end >= 0 else len(self.buffer)) > 65536:
                raise BoardroomError("oversized WebSocket handshake")
        header, rest = bytes(self.buffer).split(b"\r\n\r\n", 1)
        self.buffer = bytearray(rest)
        try:
            lines = header.decode("ascii").split("\r\n")
            version, status = lines[0].split()[:2]
            fields = {k.strip().lower(): v.strip() for k, v in
                      (line.split(":", 1) for line in lines[1:] if ":" in line)}
        except (UnicodeDecodeError, IndexError, ValueError) as e:
            raise BoardroomError(f"malformed WebSocket handshake from Codex: {e}") from e
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        # header names are case-insensitive (lowered above); the accept VALUE is compared exactly
        connection = {part.strip().lower() for part in fields.get("connection", "").split(",")}
        if (version != "HTTP/1.1" or status != "101"
                or fields.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in connection
                or fields.get("sec-websocket-accept") != expected):
            raise BoardroomError("Codex socket rejected the WebSocket handshake")

    def close(self):
        self.sock.close()

    def _receive(self, deadline):
        remaining = min(deadline - time.monotonic(), self._remaining())
        if remaining <= 0:
            raise BoardroomError("timed out waiting for Codex")
        self.sock.settimeout(remaining)
        chunk = self.sock.recv(65536)
        if not chunk:
            raise BoardroomError("Codex closed the connection")
        self.received_bytes = getattr(self, "received_bytes", 0) + len(chunk)
        cap = getattr(self, "max_receive_bytes", None)
        if cap is not None and self.received_bytes > cap:
            raise ProbeBound("Codex observation byte limit reached")
        self.buffer.extend(chunk)

    def _take(self, count, deadline):
        while len(self.buffer) < count:
            self._receive(deadline)
        out = bytes(self.buffer[:count])
        del self.buffer[:count]
        return out

    def send_frame(self, opcode, payload, before_write=None):
        if len(payload) > MAX_BYTES:
            raise BoardroomError("message exceeds 8 MiB limit")
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header = bytes([0x80 | opcode, 0x80 | n])
        elif n < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", n)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.settimeout(self._remaining())
        # Once sendall starts, an exception cannot tell us how many bytes reached
        # the peer. Serialization, limits, and frame construction above are still
        # definite local failures; only the socket-write boundary creates doubt.
        if before_write:
            before_write()
        self.sock.sendall(header + mask + masked)

    def send_json(self, value, before_write=None):
        self.send_frame(1, json.dumps(value, ensure_ascii=False).encode(), before_write)

    def read_json(self, deadline):
        message, started = bytearray(), False
        while True:
            if time.monotonic() >= deadline:
                raise BoardroomError("timed out waiting for Codex")
            first, second = self._take(2, deadline)
            opcode, final = first & 15, bool(first & 128)
            if first & 0x70 or second & 128:
                raise BoardroomError("unsupported WebSocket frame flags")
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", self._take(2, deadline))[0]
                if length < 126:
                    raise BoardroomError("noncanonical WebSocket frame length")
            elif length == 127:
                length = struct.unpack("!Q", self._take(8, deadline))[0]
                if length < 65536 or length >= 2 ** 63:
                    raise BoardroomError("invalid WebSocket frame length")
            if length > MAX_BYTES or len(message) + length > MAX_BYTES:
                raise BoardroomError("oversized WebSocket message")
            payload = self._take(length, deadline)
            if opcode in (8, 9, 10):
                if not final or length > 125:
                    raise BoardroomError("invalid WebSocket control frame")
                if opcode == 8:
                    raise BoardroomError("Codex closed the WebSocket")
                if opcode == 9:
                    self.send_frame(10, payload)
                continue
            if opcode == 1 and not started:
                started = True
            elif opcode != 0 or not started:
                raise BoardroomError("unexpected WebSocket message type")
            message.extend(payload)
            if final:
                return json.loads(message.decode())

    def call(self, method, params, mutating=False):
        """Never retry. A mutation is uncertain once its socket write is attempted.

        A matching, well-formed RPC error is an explicit rejection. Malformed
        responses are not rejection evidence, even if they contain an error key.
        Notifications may interleave with replies; the absolute deadline still
        applies while processing already-buffered frames.
        """
        self.counter += 1
        rid = self.counter
        write_attempted = False

        def mark_write():
            nonlocal write_attempted
            write_attempted = True

        try:
            self.send_json({"id": rid, "method": method, "params": params}, before_write=mark_write)
            deadline = self._rpc_deadline()
            while True:
                value = self.read_json(deadline)
                if not isinstance(value, dict):
                    raise BoardroomError(f"{method}: response is not a JSON object")
                if value.get("id") != rid:
                    if "id" in value and "method" in value:  # unsolicited server request
                        self.send_json({"id": value["id"], "error": {
                            "code": -32601, "message": "agent-boardroom does not handle server requests"}})
                    continue
                if "error" in value:
                    err = value["error"]
                    if (not isinstance(err, dict) or type(err.get("code")) is not int
                            or not isinstance(err.get("message"), str) or "result" in value):
                        raise BoardroomError(f"{method}: malformed RPC error response")
                    raise ServerError(f"{method}: {err.get('message')} (code {err.get('code')})", err["code"])
                if "result" not in value:
                    raise BoardroomError(f"{method}: missing server result")
                return value["result"]
        except ServerError:
            raise
        except (BoardroomError, OSError, ValueError, TypeError) as e:
            if mutating and write_attempted:
                # Partial or complete write, then no definite answer: the server may have acted.
                raise DeliveryUnknown(f"{method}: no definite response ({e}); do not retry") from e
            if isinstance(e, BoardroomError):
                raise
            raise BoardroomError(f"{method}: {e}") from e


def _session(record, expected_id=None):
    if not isinstance(record, dict):
        raise BoardroomError("Codex returned a malformed thread record")
    validate_routing_id(record.get("id"), "Codex thread id")
    if expected_id is not None and record["id"] != expected_id:
        raise BoardroomError("Codex returned a different thread id than requested")
    status = record.get("status")
    if not isinstance(status, dict) or not isinstance(status.get("type"), str) or not status["type"]:
        raise BoardroomError("Codex returned a missing or malformed thread status")
    for field in ("name", "cwd"):
        if record.get(field) is not None and not isinstance(record[field], str):
            raise BoardroomError(f"Codex returned non-text thread {field}")
    # Retain raw protocol metadata for discovery consumers. The common message
    # log only needs the public summary, so it does not duplicate these fields.
    return Session("codex", record["id"], record.get("name") or "",
                   status["type"], record.get("cwd") or "", {
                       "status": status, "source": record.get("source"),
                       "parentThreadId": record.get("parentThreadId"),
                   })


def _loaded(client):
    out, cursor, seen, cursors = [], None, set(), set()
    while True:
        page = client.call("thread/loaded/list", {"cursor": cursor, "limit": 100})
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise BoardroomError("Codex returned a malformed loaded-thread page")
        for tid in page["data"]:
            validate_routing_id(tid, "loaded Codex thread id")
            if tid in seen:
                continue
            seen.add(tid)
            record = _read_thread(client, tid)
            if record.status != "notLoaded":
                # Keep future status values visible. Silently dropping an
                # unfamiliar thread would hide ambiguity in name/prefix lookup.
                # Mutations still require a recognized usable status below.
                out.append(record)
        cursor = page.get("nextCursor")
        if not cursor:
            return out
        if not isinstance(cursor, str) or cursor in cursors:
            raise BoardroomError("Codex returned a repeated or invalid pagination cursor")
        cursors.add(cursor)


def _with_client(fn):
    client = Client()
    try:
        return fn(client)
    finally:
        client.close()


def list_sessions():
    return _with_client(_loaded)


def _read_thread(client, thread_id):
    validate_routing_id(thread_id, "Codex thread id")
    response = client.call("thread/read", {"threadId": thread_id, "includeTurns": False})
    if not isinstance(response, dict):
        raise BoardroomError("Codex returned a malformed thread/read response")
    return _session(response.get("thread"), expected_id=thread_id)


def _read_loaded(client, thread_id):
    rec = _read_thread(client, thread_id)
    if rec.status == "notLoaded":
        raise BoardroomError(f"Codex thread {thread_id} is not loaded; run `agent-boardroom list` again")
    return rec


def _read_live(client, thread_id):
    rec = _read_loaded(client, thread_id)
    if rec.status == "systemError":
        # A loaded thread is not necessarily usable. This is a conservative
        # agent-boardroom policy, not an assertion that queue/add itself rejects it.
        raise BoardroomError(f"Codex thread {thread_id} reports systemError; recover it in Codex before messaging")
    if rec.status not in ("idle", "active"):
        raise BoardroomError(f"Codex thread {thread_id} has unfamiliar status {rec.status!r}; messaging is refused")
    return rec


def refresh(session):
    return _with_client(lambda c: _read_live(c, session.id))


def whoami():
    tid = os.environ.get("CODEX_THREAD_ID")
    if tid is None:
        return None
    validate_routing_id(tid, "CODEX_THREAD_ID")
    # Identity and target readiness are different questions: an errored/future-
    # status thread can still identify itself and ask a healthy peer for help.
    # Only sends/renames to that thread apply the stricter target-state policy.
    return _with_client(lambda c: _read_loaded(c, tid))


def send(session, text, msg_id, sender_address):
    def go(client):
        # Best-effort preflight, not an atomic liveness guarantee: the thread can
        # unload immediately afterward. queue/add can persist the input in that
        # race; it does not itself resume an unloaded thread.
        _read_live(client, session.id)
        result = client.call("thread/queue/add", {
            "threadId": session.id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
            "clientUserMessageId": msg_id,
        }, mutating=True)
        queued = result.get("queuedSubmission") if isinstance(result, dict) else None
        queue_id = queued.get("id") if isinstance(queued, dict) else None
        if not isinstance(queue_id, str) or not queue_id:
            raise DeliveryUnknown("thread/queue/add returned no valid queue receipt; do not retry")
        return {"transport": "codex-app-server", "queue_id": queue_id, "confirmation": "server-accepted"}
    return _with_client(go)


def rename(session, new_name):
    def go(client):
        _read_live(client, session.id)
        result = client.call("thread/name/set", {"threadId": session.id, "name": new_name}, mutating=True)
        if not isinstance(result, dict):
            raise DeliveryUnknown("thread/name/set returned a malformed acknowledgment; do not retry")
        try:
            current = _read_live(client, session.id)
        except (BoardroomError, OSError) as error:
            raise DeliveryUnknown(f"Codex acknowledged the rename but readback failed ({error}); do not retry") from error
        if current.name != new_name:
            raise DeliveryUnknown(f"Codex acknowledged the rename but readback shows {current.name!r}; do not retry")
        return current
    return _with_client(go)


# Receipt probes deliberately do not reuse refresh/_read_live: persisted history
# can outlive the running session. Nor do they inspect queue disappearance, which
# can mean dispatch, deletion, or invalid input, none of which proves processing.
OBS_MAX_PAGES = 100
OBS_PAGE_SIZE = 50
OBS_MAX_BYTES = 16 * 1024 * 1024
TESTED_CLI_VERSION = "0.160.1"


class _SchemaError(Exception):
    pass


def supported_levels():
    return ("recorded", "responded", "turn_completed")


def _evidence_time(value, divisor=1000):
    """Only source timestamps enter evidence_at; never substitute the probe clock."""
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    try:
        return datetime.fromtimestamp(value / divisor, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        # Native ID + structural order prove the observation. A source clock
        # is optional metadata; its failure must not erase positive evidence.
        return None


class _History:
    """One traversal budget shared by every page, including turn-status lookup.

    The wire budget lives in Client so unsolicited notifications count as well.
    The parsed-page budget also makes mocks/non-socket clients obey the same bound.
    A limit is not absence: the caller retains previously established evidence.
    """
    def __init__(self, client, deadline):
        self.client, self.deadline = client, deadline
        self.pages, self.bytes = 0, 0

    def pages_for(self, method, params):
        cursor, seen = None, set()
        while True:
            if time.monotonic() >= self.deadline or self.pages >= OBS_MAX_PAGES:
                raise ProbeBound("Codex observation traversal limit reached")
            self.pages += 1
            result = self.client.call(method, {**params, "limit": OBS_PAGE_SIZE, "cursor": cursor})
            self.bytes += len(json.dumps(result, ensure_ascii=True).encode("ascii"))
            if self.bytes > OBS_MAX_BYTES or time.monotonic() >= self.deadline:
                raise ProbeBound("Codex observation traversal limit reached")
            if (not isinstance(result, dict) or not isinstance(result.get("data"), list)
                    or "nextCursor" not in result or len(result["data"]) > OBS_PAGE_SIZE):
                raise _SchemaError()
            next_cursor = result["nextCursor"]
            if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor
                                            or next_cursor in seen):
                raise _SchemaError()
            yield result["data"]
            if next_cursor is None:
                return
            seen.add(next_cursor)
            cursor = next_cursor


def _item(entry, expected_turn=None):
    if not isinstance(entry, dict) or not isinstance(entry.get("item"), dict):
        raise _SchemaError()
    item, tid = entry["item"], entry.get("turnId")
    # Item IDs are opaque protocol identifiers, not agent-boardroom routing IDs.
    # Do not reject a future valid item merely because it uses new punctuation.
    if any(not isinstance(v, str) or not v or len(v) > 512 for v in (tid, item.get("id"))):
        raise _SchemaError()
    if expected_turn is not None and tid != expected_turn:
        raise _SchemaError()
    if not isinstance(item.get("type"), str) or not item["type"]:
        raise _SchemaError()
    if item["type"] == "userMessage":
        if "clientId" not in item or (item["clientId"] is not None
                                       and not isinstance(item["clientId"], str)):
            raise _SchemaError()
    return item, tid


def observe(session, msg_id, level, deadline):
    """One bounded read-only scan, correlated exclusively by native clientId.

    Search newest-first across items (not turn summaries, which omit items),
    then scan only the matching turn ascending to establish output order. A
    separate paginated metadata lookup establishes that *same* turn's status.
    No body is retained, matched, returned, logged, or included in exceptions.
    A caller wanting newer evidence repeats the probe under its own wait budget.
    """
    source = {"backend": "codex", "session_id": session.id}
    reached, evidence_at, client = None, None, None

    def result(state, reason=None):
        return Observation(state, reached, utc_now(), dict(source), evidence_at, reason)

    if level not in supported_levels():
        return result("unavailable", "unsupported_level")
    if session.backend != "codex":
        return result("unavailable", "session_mismatch")
    try:
        validate_routing_id(session.id, "session id")
        validate_routing_id(msg_id, "message id")
        if not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
            return result("error", "probe_failed")
        if time.monotonic() >= deadline:
            return result("incomplete", "bound_reached")
        client = Client(deadline=deadline, max_receive_bytes=OBS_MAX_BYTES)
        metadata = client.call("thread/read", {"threadId": session.id, "includeTurns": False})
        thread = metadata.get("thread") if isinstance(metadata, dict) else None
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise _SchemaError()
        if thread["id"] != session.id:
            return result("unavailable", "session_mismatch")
        history = _History(client, deadline)
        matched = None
        skipped_unrelated = False
        for entries in history.pages_for("thread/items/list", {"threadId": session.id, "sortDirection": "desc"}):
            for entry in entries:
                try:
                    item, tid = _item(entry)
                except _SchemaError:
                    raw_item = entry.get("item") if isinstance(entry, dict) else None
                    if isinstance(raw_item, dict) and raw_item.get("type") == "userMessage":
                        raise
                    # An unrelated new/malformed tool item need not hide a valid
                    # older match. But skipped evidence makes a negative scan
                    # incomplete in meaning, so it cannot claim not_observed.
                    skipped_unrelated = True
                    continue
                if item["type"] == "userMessage" and item["clientId"] == msg_id:
                    matched = (item["id"], tid)
                    source.update(item_id=item["id"], turn_id=tid)
                    reached = "recorded"
                    evidence_at = _evidence_time(entry.get("completedAtMs"))
                    break
            if matched:
                break
        if not matched:
            return result("unavailable", "unsupported_schema") if skipped_unrelated else result("not_observed")
        if level == "recorded":
            return result("observed")

        matched_id, matched_turn = matched
        found_user = False
        for entries in history.pages_for("thread/items/list", {
                "threadId": session.id, "turnId": matched_turn, "sortDirection": "asc"}):
            for entry in entries:
                item, _ = _item(entry, matched_turn)
                if item["id"] == matched_id:
                    if item["type"] != "userMessage" or item["clientId"] != msg_id:
                        raise _SchemaError()
                    found_user = True
                    continue
                if found_user and item["type"] == "agentMessage":
                    # Reasoning alone is intentionally insufficient. This is a
                    # structural history signal, not semantic acknowledgment.
                    reached = "responded"
                    source["output_item_id"] = item["id"]
                    evidence_at = _evidence_time(entry.get("completedAtMs"))
                    break
            if reached == "responded":
                break
        if not found_user:
            # History changed between the two reads; the earlier positive
            # observation is still valid but further correlation is unavailable.
            return result("unavailable", "history_missing")
        if level == "responded":
            return result("observed" if reached == "responded" else "not_observed")
        # F4: a failed turn can end before any assistant output. Consult its
        # status even with only recorded evidence, but only for turn_completed.
        # Interrupted is deliberately excluded: the server also synthesizes it
        # for suspended in-progress turns which may recover under the same ID.
        # See docs/DESIGN.md (Codex receipts) for the source trace.
        failed_match = False
        for turns in history.pages_for("thread/turns/list", {
                "threadId": session.id, "sortDirection": "desc", "itemsView": "notLoaded"}):
            for turn in turns:
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                    raise _SchemaError()
                if turn["id"] != matched_turn:
                    continue
                if failed_match:
                    raise _SchemaError()  # no early-stop claim for ambiguous duplicate IDs
                if turn.get("status") not in ("completed", "interrupted", "failed", "inProgress") or "error" not in turn:
                    raise _SchemaError()
                error = turn["error"]
                if error is not None and (not isinstance(error, dict) or not isinstance(error.get("message"), str)):
                    raise _SchemaError()
                if turn["status"] == "failed":
                    if error is None:
                        raise _SchemaError()  # the reviewed projection requires a terminal error
                    failed_match = True
                    # A failed row is immutable in the reviewed paginated store.
                    # Finish bounded pagination before claiming unique identity;
                    # a later duplicate or exhausted budget must not stop a wait.
                    continue
                if reached == "responded" and turn["status"] == "completed" and error is None:
                    reached = "turn_completed"
                    evidence_at = _evidence_time(turn.get("completedAt"), divisor=1)
                return result("observed" if reached == "turn_completed" else "not_observed")
        if failed_match:
            return result("not_observed", "turn_failed")
        return result("unavailable", "history_missing")
    except ProbeBound:
        return result("incomplete", "bound_reached")
    except _SchemaError:
        return result("unavailable", "unsupported_schema")
    except ServerError as error:
        return result("unavailable", "unsupported_schema" if error.code == -32601 else "probe_failed")
    except (BoardroomError, OSError, ValueError, TypeError, RecursionError):
        # Never forward raw server errors: they may contain transcript content.
        if isinstance(deadline, (int, float)) and time.monotonic() >= deadline:
            return result("incomplete", "bound_reached")
        return result("error", "transport_error")
    finally:
        if client is not None:
            client.close()


def _version_output(executable, deadline):
    """Use the shared bounded subprocess reader for the installed CLI probe.

    Both backends need the same time/output/reaping guarantees; keeping those in
    br_common avoids two copies drifting as the diagnostic contract changes.
    """
    from .common import run_bounded
    return run_bounded([executable, "--version"], deadline, seconds=3.0, limit=16384)


def doctor(deadline):
    """Read-only diagnostics. Installed CLI and running-server evidence differ.

    Only successful RPCs prove the probed read capabilities. This never adds a
    queue item, starts/resumes a thread, or interprets lack of a session as a
    protocol failure. All error details use fixed categories, not server text.
    """
    # Imported here because rendering belongs to shared code; neither wire data
    # nor observation source identifiers should be destructively sanitized.
    from .common import safe_text
    rows = []

    def add(check, status, detail):
        rows.append({"check": "codex." + check, "status": status,
                     "detail": safe_text(str(detail)[:4096], one_line=True)})

    add("socket", "ok", str(socket_path()))
    try:
        from . import setup as _setup
        dest = _setup.destinations()["codex"]["dest"]
        state, detail = _setup.inspect("codex", dest, deadline)
        add("skill", {"current": "ok", "missing": "warn", "outdated": "warn", "modified": "warn",
                      "unmanaged": "ok", "symlink": "ok", "malformed": "warn", "unreadable": "warn"}[state],
            f"{state}: {detail}" + (" (run `agent-boardroom setup`)" if state in ("missing", "outdated") else ""))
    except (BoardroomError, OSError) as e:
        add("skill", "warn", f"could not inspect: {type(e).__name__}")
    executable = shutil.which("codex")
    if executable is None:
        add("installed_version", "warn", "CLI executable not found; a running server may still exist")
    else:
        try:
            version = _version_output(executable, deadline)
            expected = "codex-cli " + TESTED_CLI_VERSION
            add("installed_version", "ok" if version == expected else "warn",
                f"{version}; reviewed CLI: {TESTED_CLI_VERSION}; not the running-server version")
        except (BoardroomError, OSError):
            add("installed_version", "warn", "version probe unavailable or bounded")
    client = None
    try:
        client = Client(deadline=deadline, max_receive_bytes=OBS_MAX_BYTES)
        add("control_connection", "ok", "WebSocket initialize succeeded")
        add("running_version", "warn", "unknown; initialize has no verified server-version field")
        listing = client.call("thread/loaded/list", {"limit": 1})
        if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
            raise _SchemaError()
        add("loaded_list", "ok", "read-only RPC succeeded")
        if not listing["data"]:
            add("history_pagination", "warn", "unprobed: no loaded thread selected; no thread was resumed")
        else:
            tid = listing["data"][0]
            validate_routing_id(tid, "thread id")
            for method in ("thread/turns/list", "thread/items/list"):
                page = client.call(method, {"threadId": tid, "limit": 1})
                if not isinstance(page, dict) or not isinstance(page.get("data"), list) or "nextCursor" not in page:
                    raise _SchemaError()
                add(method.replace("/", "_"), "ok", "read-only RPC succeeded; bodies discarded")
    except (BoardroomError, OSError, ValueError, TypeError, _SchemaError):
        add("protocol_probe", "warn", "unavailable, unsupported, or probe bound reached; no mutation attempted")
    finally:
        if client is not None:
            client.close()
    return rows
