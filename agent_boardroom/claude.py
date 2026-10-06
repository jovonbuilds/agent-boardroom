"""Claude Code backend: local session registry + Unix-socket inbox (peerProtocol 1).


Reverse-engineered from Claude Code 2.1.290 -- an internal, undocumented protocol that can change
on any update. If something breaks, look at ~/.claude/sessions/<pid>.json first.
  registry  ~/.claude/sessions/<pid>.json  {sessionId, name, cwd, status, messagingSocketPath, ...}
  key       ~/.claude/sessions/<pid>.<sha256(advertised socket path)>.key  {"peerToken": ...}
            (the hash is of the path exactly as advertised, e.g. /tmp/..., not its /private/tmp realpath)
  wire      NDJSON over the socket:
              {"type":"auth","token":...}                                     optional off Windows
              {"type":"user","session_id":<target>,"message":{...},...}       a message
              {"type":"control","session_id":<target>,"action":"rename",...}  rename the receiver
  limits    the server accumulates decoded text and destroys the connection once the buffer exceeds
            1,048,576 UTF-16 code units, checked BEFORE splitting lines, so the cap covers auth line
            plus frame together. It also drops a connection with no complete line within 30 s.
  session_id  the receiver drops any frame whose session_id is present and differs from its own
            sessionId. We always send the target's id, so a reused PID or socket can't receive a
            message meant for a session that has since died.
The receiver applies its own inbound gate (prompting mode accepts; bypassPermissions holds for
user approval). We never assert from-mode, so we cannot talk past that gate (D6).
"""
import datetime
import glob
import hashlib
import json
import os
import re
import socket
import stat
import time

from .common import (BoardroomError, DeliveryUnknown, Observation, Session, run_bounded, safe_text,
                       utc_now, validate_routing_id)

SESS_DIR = os.path.expanduser(os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"), "sessions"))
WIRE_CAP_UTF16 = 1_048_576
WIRE_MARGIN_UTF16 = 4096  # headroom so a payload near the cap never trips it on a rounding difference


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _records():
    for path in glob.glob(os.path.join(SESS_DIR, "*.json")):
        try:
            with open(path) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        pid, sock, sid = rec.get("pid"), rec.get("messagingSocketPath"), rec.get("sessionId")
        if not isinstance(pid, int) or not isinstance(sock, str) or not sock or not isinstance(sid, str):
            continue
        # Crashed sessions can leave their .json behind, so a record is live only if the PID is
        # still running AND its socket file exists.
        if not _alive(pid) or not os.path.exists(sock):
            continue
        yield rec


def _to_session(rec):
    return Session("claude", rec["sessionId"], rec.get("name") or "", rec.get("status") or "",
                   rec.get("cwd") or "", {"pid": rec["pid"], "sock": rec["messagingSocketPath"]})


def list_sessions():
    return [_to_session(r) for r in _records()]


def _one_live(session_id, missing_msg):
    """The single live registry record with this sessionId. Zero is an error, and so is MORE than one.

    Two live processes advertising the same sessionId (e.g. one conversation resumed in two
    terminals, or stale registry state) can't be told apart by the session_id guard: both would
    accept the frame. Picking the first would be a silent guess (D2/D26), so we
    refuse and name the PIDs. The user has to close the duplicate session(s); renaming wouldn't help,
    since it changes the display name, not the sessionId. --as can't help here either,
    because it only chooses a backend.
    """
    hits = [r for r in _records() if r["sessionId"] == session_id]
    if not hits:
        raise BoardroomError(missing_msg)
    if len(hits) > 1:
        pids = ", ".join(str(r["pid"]) for r in hits)
        raise BoardroomError(f"Claude session id {session_id} is live in {len(hits)} processes (pids {pids}); "
                             "refusing to guess. Close the duplicate session(s), then retry.")
    return _to_session(hits[0])


def refresh(session):
    """Re-read the registry right before sending; the session must still be live, exactly once."""
    return _one_live(session.id, f"Claude session {session.id} is no longer running; run `agent-boardroom list` again")


def whoami():
    """None if this process doesn't advertise a Claude identity; raises if it advertises a bad one."""
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid is None:
        return None
    validate_routing_id(sid, "CLAUDE_CODE_SESSION_ID")
    return _one_live(sid, f"CLAUDE_CODE_SESSION_ID={sid} has no live registry entry in {SESS_DIR}")


def _peer_token(session):
    """Read the target's peerToken from its exact key file, or None if it has none.

    The receiver only requires the token on Windows; on macOS/Linux the 0700 socket dir is the
    protection. We send it anyway when present: it's what real Claude peers do, and it keeps
    working if auth becomes mandatory. The file is chosen by exact name, never "first match", so a
    stale key from another socket can't be picked up. Read per send, never logged or stored.
    """
    sock = session.detail["sock"]
    key = os.path.join(SESS_DIR, f"{session.detail['pid']}.{hashlib.sha256(sock.encode()).hexdigest()}.key")
    try:
        with open(key) as fh:
            token = json.load(fh).get("peerToken")
    except (OSError, ValueError, AttributeError):
        return None
    return token if isinstance(token, str) and token else None


def _payload(session, frames):
    """Serialize auth + frames and check them against the receiver's real buffer cap."""
    payload = ""
    token = _peer_token(session)
    if token:
        payload += json.dumps({"type": "auth", "token": token}) + "\n"
    payload += "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in frames)
    units = len(payload.encode("utf-16-le")) // 2  # JS string .length counts UTF-16 code units
    if units > WIRE_CAP_UTF16 - WIRE_MARGIN_UTF16:
        raise BoardroomError(f"serialized message is {units} UTF-16 units; Claude's inbox drops anything "
                             f"over {WIRE_CAP_UTF16}. Put large content in a file and send its path.")
    return payload.encode()


def _write(session, payload):
    """Connect and write. Failures before any byte is written are definite failures (D4)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        try:
            s.connect(session.detail["sock"])
        except OSError as e:
            raise BoardroomError(f"could not connect to {session.detail['sock']}: {e}") from e
        try:
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)
        except OSError as e:
            raise DeliveryUnknown(f"write to Claude inbox interrupted: {e}") from e
        try:
            # Wait for the server to close its side. Exiting right after the write could tear the
            # connection down before the receiver's line parser has run. The server sends no reply
            # bytes, so EOF (or this timeout) is the only signal there is, and neither one tells
            # us whether the receiver accepted, held, or dropped the frame. Hence "written", not
            # "accepted" (D7/D4).
            s.recv(1)
        except OSError:
            pass
    finally:
        s.close()


def send(session, text, msg_id, sender_address):
    """Write a user-message frame to the session inbox. Returns a receipt dict ("written" strength)."""
    validate_routing_id(session.id, "Claude target session id")
    # D6: deliberately no from-mode anywhere. The receiver compares a declared mode with its own,
    # so declaring "bypass" would walk a message past a skip-permissions session's human approval.
    # priority "next" = read at the receiver's next tool round; "now" would interrupt its work.
    frame = {"type": "user", "session_id": session.id,
             "message": {"role": "user", "content": text}, "priority": "next",
             "from": sender_address, "msg_id": msg_id, "uuid": msg_id}
    payload = _payload(session, [frame])  # size check happens before connecting
    _write(session, payload)
    # Key and value naming matches br_codex ("confirmation": "server-accepted") so the log reads
    # uniformly: this one only says the socket took the bytes.
    return {"transport": "claude-uds", "confirmation": "transport-written", "pid": session.detail["pid"]}


def rename(session, new_name):
    """Ask a Claude session to rename itself, then confirm via the registry.

    The rename handler wasn't traced end to end, so success is judged by observation: the
    registry's name must change within 3 s, otherwise the result is delivery-unknown.
    """
    validate_routing_id(session.id, "Claude session id")
    _write(session, _payload(session, [{"type": "control", "session_id": session.id,
                                        "action": "rename", "name": new_name}]))
    # From here the frame is written, so every failure, including the session vanishing during
    # read-back, is delivery-unknown. A plain error would exit 1, which means "safe to retry".
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            cur = refresh(session)
            if cur.name == new_name:
                return cur
            time.sleep(0.2)
        shown = refresh(session).name
    except (BoardroomError, OSError) as e:
        raise DeliveryUnknown(f"rename frame written but read-back failed ({e}); do not retry") from e
    raise DeliveryUnknown(f"rename frame written but the registry still shows {shown!r}; do not retry")


# ---------------------------------------------------------------- v0.3 receipts (docs/DESIGN.md)
#
# Evidence comes from the RECEIVER's own transcript, ~/.claude/projects/<slug(cwd)>/<sessionId>.jsonl
# (docs/DESIGN.md). Claude writes a peer message there as a type=user record whose uuid is the sender's
# msg_id (we send uuid=msg_id), and the model's next output as a type=assistant record whose
# parentUuid is that uuid. Both are structural evidence from persisted history, not a capture of the
# model request, and neither means "understood" or "acted on".
#
# Privacy: lines are parsed as JSON (so whole records are read), but only the fields below are kept;
# nothing is stored or printed. A cheap substring prefilter on the msg_id skips parsing every other
# line. The prefilter only narrows what gets parsed; evidence always comes from the parsed fields.

SUPPORTED_LEVELS = ("recorded", "responded")
SCAN_MAX_BYTES = 512 * 1024 * 1024  # largest transcript seen locally was ~58 MB


def supported_levels():
    return SUPPORTED_LEVELS


def _config_dir():
    return os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))


def _slug(cwd):
    # Observed: every non-alphanumeric character becomes "-" ("/.claude/worktrees" -> "--claude-worktrees").
    return re.sub(r"[^A-Za-z0-9-]", "-", cwd)


FALLBACK_MAX_DIRS = 5000


def locate_transcript(session_id, cwd, deadline=None):
    """Return (path, None) or (None, reason). Never guesses between several candidates.
    The glob fallback is skipped once the shared deadline is spent (reported as bound_reached)."""
    validate_routing_id(session_id, "Claude session id")
    projects = os.path.join(_config_dir(), "projects")
    if isinstance(cwd, str) and cwd:
        exact = os.path.join(projects, _slug(cwd), f"{session_id}.jsonl")
        if os.path.isfile(exact):
            return exact, None
    # Fallback: look for <session_id>.jsonl in each project dir, but bounded: a
    # deadline check per directory, a cap on directories visited, and an early stop at the second
    # match (two is already ambiguous). Never materializes an unbounded listing.
    name, hits, visited = f"{session_id}.jsonl", [], 0
    try:
        with os.scandir(projects) as it:
            for entry in it:
                if deadline is not None and time.monotonic() >= deadline:
                    return None, "bound_reached"
                visited += 1
                if visited > FALLBACK_MAX_DIRS:
                    return None, "bound_reached"
                if not entry.is_dir(follow_symlinks=False):
                    continue
                candidate = os.path.join(entry.path, name)
                if os.path.isfile(candidate):
                    hits.append(candidate)
                    if len(hits) > 1:
                        return None, "ambiguous_location"
    except OSError:
        return None, "history_missing"
    return (hits[0], None) if hits else (None, "history_missing")


LINE_MAX = 16 * 1024 * 1024  # one transcript record; a longer line is read in bounded pieces


def _project(rec):
    """Keep only the metadata the contract names. Content never leaves this function."""
    origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else None
    att = rec.get("attachment") if isinstance(rec.get("attachment"), dict) else {}
    att_origin = att.get("origin") if isinstance(att.get("origin"), dict) else {}
    return {"att_type": att.get("type"), "att_source_uuid": att.get("source_uuid"),
            "att_origin_kind": att_origin.get("kind"), "att_origin_msg_id": att_origin.get("msg_id"),"type": rec.get("type"), "uuid": rec.get("uuid"), "parentUuid": rec.get("parentUuid"),
            "sessionId": rec.get("sessionId"), "isSidechain": rec.get("isSidechain"),
            "isApiErrorMessage": rec.get("isApiErrorMessage"), "timestamp": rec.get("timestamp"),
            "has_origin": origin is not None and "kind" in origin,
            "origin_kind": origin.get("kind") if origin else None,
            "origin_msg_id": origin.get("msg_id") if origin else None}


def _iso_ms(value):
    """Normalize a transcript timestamp to ISO-8601 UTC with milliseconds and "Z", or None."""
    if not isinstance(value, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    return t.astimezone(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Busy-responded index bounds.
# Every record carrying a uuid OR a parent reference counts toward NODE_CAP. Only bounded strings are
# retained: uuid/parentUuid/type of at most ID_MAX code points (never truncated; an over-long one makes
# the busy claim unsupported) and a timestamp of at most 64; anything else (dicts, lists, numbers) is
# normalized to None and never kept raw. INDEX_MAX_BYTES caps retained payload, counted as 4 bytes
# per code point (Python's worst-case character width), so it holds for any Unicode. The payload
# can't exceed 32 MiB, plus per-entry dict/tuple overhead for at most NODE_CAP entries; the largest
# real transcript indexed 12,288 records in 5.9 MB.
NODE_CAP = 100_000
ID_MAX = 256
INDEX_MAX_BYTES = 32 * 1024 * 1024
MAX_HOPS = 16        # edges from a busy delivery to its assistant output (observed max: 7)
_NO_UUID = object()  # first_child sentinel: a child record without a valid uuid


def observe(session, msg_id, level, deadline):
    """ONE bounded scan of a fixed snapshot of the destination transcript (`session` = LOGGED `to`).

    recorded            prefiltered scan with early success (idle user record, or busy attachment).
    responded, idle     direct assistant child of the user record; early success (reviewed v0.3 rule).
    responded, busy     D34 / v0.3.1: the whole snapshot is indexed (every record's parent link,
                        counted BEFORE any filtering), then the chain from the matched attachment is
                        resolved: every parent on it has exactly one child, intermediates are
                        same-session non-sidechain attachments, ending at the first non-error
                        assistant within MAX_HOPS edges. Never early-returns; any ambiguity refuses.
    Bounds: snapshot = bytes [0, size at open); LINE_MAX, SCAN_MAX_BYTES, NODE_CAP and the shared
    deadline. Positive lower evidence (recorded) survives every exit path. No content is retained.
    """
    user = child = None
    torn_match = mismatch = schema = bad_child = False
    delivery = None
    walk = {}  # busy-path resolution: {"output": uuid, "hops": n, "ts": ...}

    def obs(state, reason=None):
        src = {"backend": "claude", "session_id": session.id}
        reached = ev = None
        if user is not None:
            src["entry_uuid"] = user["uuid"]
            src["delivery"] = delivery
            reached, ev = "recorded", _iso_ms(user["timestamp"])
            if child is not None:
                src["output_uuid"] = child["uuid"]
                reached, ev = "responded", _iso_ms(child["timestamp"])
            elif walk.get("output"):
                src["output_uuid"], src["hops"] = walk["output"], walk["hops"]
                reached, ev = "responded", _iso_ms(walk["ts"])
        return Observation(state, reached, utc_now(), src, ev, reason)

    if level not in SUPPORTED_LEVELS:
        return obs("unavailable", "unsupported_level")
    if time.monotonic() >= deadline:
        return obs("incomplete", "bound_reached")  # an expired budget touches no files at all
    try:
        validate_routing_id(msg_id, "msg id")
        path, reason = locate_transcript(session.id, session.cwd, deadline)
    except BoardroomError:
        return obs("error", "probe_failed")
    if path is None:
        return obs("incomplete" if reason == "bound_reached" else "unavailable", reason)

    index_mode = level == "responded"  # busy responded needs every record; recorded keeps the prefilter
    needles = {msg_id.encode()}
    read = 0
    complete = capped = unclassified = multi_match = oversized = False
    child_count, first_child, nodes, dups = {}, {}, {}, set()
    indexed = index_bytes = 0

    def satisfied():
        # Early success only where reviewed: recorded (either path), and idle direct-child responded.
        # A busy responded claim must see the whole snapshot first (D34).
        if user is None:
            return False
        if level == "recorded":
            return True
        return delivery == "user" and child is not None

    try:
        with open(path, "rb") as fh:
            # Snapshot = the bytes present at open. If it can't be established (no fstat, not a
            # regular file) we still read for lower evidence, but no busy responded is certified.
            try:
                st0 = os.fstat(fh.fileno())
                snapshot_ok = stat.S_ISREG(st0.st_mode)
            except (OSError, ValueError):
                st0, snapshot_ok = None, False
            size0 = st0.st_size if snapshot_ok else None
            limit_total = min(size0, SCAN_MAX_BYTES) if snapshot_ok else SCAN_MAX_BYTES
            while not satisfied():
                remaining = limit_total - read
                if time.monotonic() > deadline:
                    break
                if remaining <= 0:
                    # complete only if the whole snapshot was read, not merely the byte budget
                    complete = snapshot_ok and read >= size0
                    break
                raw = fh.readline(min(LINE_MAX, remaining))
                if not raw:
                    complete = (read >= size0) if snapshot_ok else True
                    break
                read += len(raw)
                if not raw.endswith(b"\n"):
                    if snapshot_ok and read >= size0:
                        # Partial final line at the snapshot boundary (a writer mid-append): it can't
                        # be classified, so higher evidence and negatives can't rely on it.
                        unclassified = torn_match = True
                        complete = True
                        break
                    # Over-long record: skip the rest in bounded pieces. Our id could straddle a chunk,
                    # and it could hide a branch, so it blocks complete negatives and busy responded.
                    torn_match = unclassified = True
                    while raw and not raw.endswith(b"\n"):
                        remaining = limit_total - read
                        if time.monotonic() > deadline or remaining <= 0:
                            break
                        raw = fh.readline(min(LINE_MAX, remaining))
                        read += len(raw)
                    continue
                indexing = index_mode and not capped
                if not indexing and not any(n in raw for n in needles):
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    unclassified = True
                    if any(n in raw for n in needles):
                        torn_match = True  # a record naming our id that we can't parse: not proof of absence
                    continue
                if not isinstance(rec, dict):
                    unclassified = True
                    continue
                if indexing:
                    # Index EVERY record before any filtering: child references are
                    # counted regardless of the child's type, sidechain flag, session or validity.
                    u, par, rtype = rec.get("uuid"), rec.get("parentUuid"), rec.get("type")
                    u_ok = isinstance(u, str) and bool(u)
                    if u_ok or isinstance(par, str):
                        # Only bounded STRINGS are ever retained: a non-string or
                        # over-long type is normalized to None (malformed if it lands on the path),
                        # never kept raw; ids are never truncated, and an over-long one is unusable.
                        if any(isinstance(x, str) and len(x) > ID_MAX for x in (u, par, rtype)):
                            oversized = True
                        u_keep = u if u_ok and len(u) <= ID_MAX else None
                        p_keep = par if isinstance(par, str) and len(par) <= ID_MAX else None
                        t_keep = rtype if isinstance(rtype, str) and len(rtype) <= ID_MAX else None
                        ts = rec.get("timestamp")
                        ts = ts if isinstance(ts, str) and len(ts) <= 64 else None
                        indexed += 1
                        # 4 bytes per code point: Python's worst-case storage per character, so the
                        # budget is a conservative byte bound for any Unicode, not a code-point count.
                        index_bytes += 4 * sum(len(x) for x in (u_keep, p_keep, t_keep, ts) if x)
                        if indexed > NODE_CAP or index_bytes > INDEX_MAX_BYTES:
                            # Stop INDEXING, not scanning: the prefiltered scan continues so lower
                            # evidence (recorded) is still found; busy responded becomes incomplete.
                            capped = True
                        else:
                            if p_keep is not None:
                                # Every parent reference counts, before any validity filtering.
                                child_count[p_keep] = child_count.get(p_keep, 0) + 1
                                first_child.setdefault(p_keep, u_keep if u_keep is not None else _NO_UUID)
                            if u_keep is not None:
                                if u_keep in nodes:
                                    dups.add(u_keep)
                                else:
                                    err = rec.get("isApiErrorMessage")
                                    # Three states, kept as a small scalar: absent, null or
                                    # False = ok, True = error, anything else = malformed. A malformed
                                    # flag must never become successful assistant evidence.
                                    err = "ok" if err is None or err is False else ("error" if err is True else "bad")
                                    nodes[u_keep] = (t_keep, rec.get("isSidechain") is False, err,
                                                     rec.get("sessionId") == session.id, ts)
                    if not any(n in raw for n in needles):
                        del rec
                        continue
                r = _project(rec)
                del rec  # content is not retained past projection
                if r["type"] == "user" and r["uuid"] == msg_id:
                    # Idle delivery: the peer message IS a user record whose uuid is our msg_id.
                    if r["sessionId"] != session.id:
                        mismatch = True
                    elif not r["has_origin"]:
                        schema = True
                    elif (r["isSidechain"] is False and r["origin_kind"] == "peer"
                          and r["origin_msg_id"] == msg_id):
                        if user is None:
                            user, delivery = r, "user"
                            needles.add(r["uuid"].encode())
                        else:
                            multi_match = True
                elif r["type"] == "attachment" and r["att_type"] == "queued_command" and (
                        r["att_source_uuid"] == msg_id or r["att_origin_msg_id"] == msg_id):
                    # Busy (mid-turn) delivery, see docs/DESIGN.md: an attachment with its OWN uuid, carrying our id
                    # twice. Both copies must agree; a conflict is unevaluable, never a match.
                    if r["att_source_uuid"] != msg_id or r["att_origin_msg_id"] != msg_id:
                        schema = True
                    elif r["sessionId"] != session.id:
                        mismatch = True
                    elif (r["isSidechain"] is False and r["att_origin_kind"] == "peer"
                          and isinstance(r["uuid"], str) and r["uuid"]):
                        if user is None:
                            user, delivery = r, "attachment"
                            needles.add(r["uuid"].encode())
                        else:
                            multi_match = True  # the same msg delivered twice: no unique root
                elif (user is not None and delivery == "user" and r["type"] == "assistant"
                      and child is None and r["parentUuid"] == user["uuid"]):
                    # Idle responded = a DIRECT child of the user record (reviewed v0.3 rule).
                    if not (isinstance(r["uuid"], str) and r["uuid"]):
                        bad_child = True  # linked to our message but unevaluable: not proof of absence
                    elif (r["isSidechain"] is False and not r["isApiErrorMessage"]
                          and r["sessionId"] == session.id):
                        child = r
            changed = not snapshot_ok
            if complete and snapshot_ok:
                # Snapshot integrity. Pure append is fine: we judged the captured
                # prefix. Shrinking, a different (st_dev, st_ino) at the path, or a same-size file
                # whose mtime/ctime moved is treated as a rewrite, so no busy claim is certified.
                # Residual same-user race, documented not solved: a truncate-and-regrow back past
                # size0 between our reads can't be fully excluded by start/end metadata alone.
                try:
                    st1, stp = os.fstat(fh.fileno()), os.stat(path)
                    changed = (st1.st_size < size0
                               or (stp.st_dev, stp.st_ino) != (st0.st_dev, st0.st_ino)
                               or (st1.st_size == size0 and (st1.st_mtime_ns, st1.st_ctime_ns)
                                   != (st0.st_mtime_ns, st0.st_ctime_ns)))
                except OSError:
                    changed = True
    except OSError:
        if satisfied():
            return obs("observed")
        return obs("error", "probe_failed")  # keeps any recorded/responded already established

    if satisfied():
        return obs("observed")
    if not complete:
        return obs("incomplete", "bound_reached")
    if user is None:
        if torn_match:
            return obs("incomplete", "probe_failed")
        if mismatch:
            return obs("unavailable", "session_mismatch")
        if schema:
            return obs("unavailable", "unsupported_schema")
        return obs("not_observed")
    if level == "recorded":
        return obs("observed")
    if changed:
        return obs("incomplete", "probe_failed")  # recorded kept; no higher claim on an unstable snapshot
    if delivery == "user":
        if child is None and torn_match:
            return obs("incomplete", "probe_failed")  # an unparseable record might be the response
        if child is None and bad_child:
            return obs("unavailable", "unsupported_schema")
        return obs("not_observed")
    if capped:
        return obs("incomplete", "bound_reached")  # the index budget ran out: recorded kept, no busy claim
    if oversized:
        return obs("unavailable", "unsupported_schema")  # an over-long identifier could hide a branch
    return _resolve_busy(obs, user["uuid"], child_count, first_child, nodes, dups,
                         unclassified, multi_match, walk)


def _resolve_busy(obs, root, child_count, first_child, nodes, dups, unclassified, multi_match, walk):
    """D34: resolve the busy-delivery chain from the complete snapshot index (see observe)."""
    if unclassified:
        return obs("incomplete", "probe_failed")  # an unclassifiable line could hide a branch
    if multi_match or root in dups:
        return obs("unavailable", "unsupported_schema")
    cur, hops, visited = root, 0, {root}
    while True:
        n = child_count.get(cur, 0)
        if n == 0:
            return obs("not_observed")  # nothing after it (yet)
        if n > 1:
            return obs("unavailable", "unsupported_schema")  # branch (sidechain siblings included)
        c = first_child[cur]
        hops += 1
        if hops > MAX_HOPS:
            return obs("incomplete", "bound_reached")
        if c is _NO_UUID or c in dups or c in visited or c not in nodes:
            return obs("unavailable", "unsupported_schema")
        ctype, main_chain, api_err, same_session, ts = nodes[c]
        if not isinstance(ctype, str) or not ctype:
            return obs("unavailable", "unsupported_schema")  # malformed, not a well-formed "other type"
        if not main_chain:
            return obs("unavailable", "unsupported_schema")  # a sole sidechain child
        if not same_session:
            return obs("unavailable", "session_mismatch")
        if ctype == "assistant":
            if api_err == "bad":
                return obs("unavailable", "unsupported_schema")  # malformed error flag: unevaluable
            if api_err == "error":
                return obs("not_observed")  # an error is not a response
            walk.update(output=c, hops=hops, ts=ts)
            return obs("observed")
        if ctype != "attachment":
            return obs("not_observed")  # user/system/anything else ends the walk: no responded claim
        visited.add(c)
        cur = c


DOCTOR_MAX_RECORDS = 10_000
# Claude Code versions whose private session-registry/socket/transcript behavior was verified
# (see docs/DESIGN.md). doctor warns when the installed version isn't one of these.
TESTED_CLAUDE_VERSIONS = ("2.1.290",)


def doctor(deadline):
    """Bounded, read-only checks for the Claude side. Returns [{"check","status","detail"}].

    Every probe respects the shared deadline: the version probe goes through common.run_bounded
    (time AND output bounded), and the registry scan stops at DOCTOR_MAX_RECORDS or the deadline.
    """
    out = []

    def add(check, status, detail):
        out.append({"check": check, "status": status, "detail": safe_text(detail, one_line=True)[:300]})

    verified = list(TESTED_CLAUDE_VERSIONS)
    try:
        text = run_bounded(["claude", "--version"], deadline, seconds=3.0)
        installed = (text.split() or ["?"])[0]
        if installed in verified:
            add("claude version", "ok", f"installed {installed} is a tested version ({', '.join(verified)})")
        else:
            add("claude version", "warn", f"installed {installed}; the private protocol was verified only "
                f"against {', '.join(verified) or 'none'}. Re-check it (see docs/DESIGN.md) before relying on it.")
    except (BoardroomError, OSError):
        add("claude version", "warn", "could not run `claude --version` within its bounds")
    add("claude version scope", "ok", "installed version is not proof of what running sessions use")

    live_cwd = {}  # sessionId -> cwd, gathered within the bounded scan (initialized before any branch)
    if os.path.isdir(SESS_DIR):
        total = live = 0
        bounded = False
        for path in glob.iglob(os.path.join(glob.escape(SESS_DIR), "*.json")):
            if total >= DOCTOR_MAX_RECORDS or time.monotonic() > deadline:
                bounded = True
                break
            total += 1
            try:
                with open(path, "rb") as fh:
                    rec = json.loads(fh.read(64 * 1024))
                pid, sock = rec.get("pid"), rec.get("messagingSocketPath")
                if isinstance(pid, int) and isinstance(sock, str) and _alive(pid) and os.path.exists(sock):
                    live += 1
                    if isinstance(rec.get("sessionId"), str):
                        live_cwd.setdefault(rec["sessionId"], []).append(rec.get("cwd"))
            except (OSError, ValueError, AttributeError):
                pass
        add("claude sessions", "warn" if bounded or not live else "ok",
            f"{SESS_DIR}: {live} live, {total - live} stale or unreadable (not deleted)"
            + ("; scan bounded, counts are partial" if bounded else ""))
    else:
        add("claude sessions", "error", f"{SESS_DIR} does not exist")

    # Skill status: read-only classification of the installed skill copy (see setup.inspect).
    try:
        from . import setup as _setup
        dest = _setup.destinations()["claude"]["dest"]
        state, detail = _setup.inspect("claude", dest, deadline)
        add("claude skill", {"current": "ok", "missing": "warn", "outdated": "warn", "modified": "warn",
                             "unmanaged": "ok", "symlink": "ok", "malformed": "warn", "unreadable": "warn"}[state],
            f"{state}: {detail}" + (" (run `agent-boardroom setup`)" if state in ("missing", "outdated") else ""))
    except (BoardroomError, OSError) as e:
        add("claude skill", "warn", f"could not inspect: {type(e).__name__}")

    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid is not None:
        # No unbounded follow-up work: reuse what the bounded scan saw rather
        # than calling whoami()/_records() again, and give the glob fallback the same deadline.
        cwds = live_cwd.get(sid, [])
        if len(cwds) != 1:
            add("claude transcript", "warn", "this session wasn't found exactly once in the bounded registry scan")
        elif time.monotonic() >= deadline:
            add("claude transcript", "warn", "unchecked: deadline reached")
        else:
            try:
                path, reason = locate_transcript(sid, cwds[0], deadline)
                add("claude transcript", "ok" if path else "warn",
                    f"this session's transcript {'resolves' if path else 'does not resolve (' + reason + ')'}")
            except BoardroomError as e:
                add("claude transcript", "warn", f"this session can't be verified: {e}")
    return out
