#!/usr/bin/env python3
"""agent-boardroom: message between Claude Code and Codex sessions on this machine (stdlib only).

  agent-boardroom list [--json]                     live sessions on both backends
  agent-boardroom whoami [--as claude|codex]        which session this command is running in
  agent-boardroom send <target> <text|-> [--reply-to ID] [--wait LEVEL [--wait-timeout S]] [--no-guard G]
  agent-boardroom reply <msg-id> <text|-> [--wait LEVEL [--wait-timeout S]] [--no-guard G]
  agent-boardroom receipt <msg-id> [--level LEVEL] [--record] [--timeout S]
  agent-boardroom name <new-name>                   rename the session you are running in
  agent-boardroom log [-n N] [--full] [--follow] [--json]
  agent-boardroom doctor                            bounded, read-only health checks
  agent-boardroom setup [--claude] [--codex] [--dry-run] [--force] [--uninstall]
                                                    install the bundled agent skills, then run doctor

Targets: claude:<id|name>, codex:<id|name>, or a bare id/name/unique prefix searched on both.
Every message carries the sender's backend + session id (the routing key), a msg-id and an
optional reply-to. Every send is logged to ~/.agent-boardroom/log.jsonl BEFORE delivery, then its
outcome. Sends are never retried automatically.

Outcomes and exit codes:
  0  written    (Claude) the inbox socket took the bytes; nothing acknowledged them
  0  accepted   (Codex)  the app-server answered with a queue_id
  1  failed     definitely not delivered (or never attempted)
  2  delivery-unknown   request written, no definite answer: do NOT resend blindly
  3  the transport step succeeded (written/accepted) but its outcome could not be logged: do NOT resend;
     also: a requested receipt observation was established but could not be saved
  4  sent (written/accepted) but the --wait receipt level was not established by the deadline:
     do NOT resend
Neither success means the other agent has read the message; its reply is the only real ack.
Receipt levels (no reply needed; structural evidence from the receiver's own history, not proof it
was understood): recorded, responded (Claude and Codex), turn_completed (Codex only).
`receipt` is inspection: exit 0 = completed report (observed or not_observed), 1 = invalid input or
the probe was unavailable/incomplete/error, 3 = --record couldn't save. It doesn't assess resend safety.

"""
import argparse
import getpass
import json
import os
import sys
import time

from . import __version__  # noqa: E402
from . import claude as br_claude  # noqa: E402
from . import setup as setup_mod  # noqa: E402
from . import codex as br_codex  # noqa: E402
import math  # noqa: E402

from .common import (ACCEPTED, BACKENDS, FAILED, GUARDS, MAX_BODY_BYTES, RECEIPT_LEVELS,  # noqa: E402
                       UNKNOWN, WRITTEN, BoardroomError, DeliveryUnknown, LogError, Observation, Session,
                       find_message, json_dumps_safe, log_event, new_msg_id, read_log, record_observation,
                       reserve_attempt, safe_text, same_endpoint, validate_routing_id, wrap)

BACKEND = {"claude": br_claude, "codex": br_codex}
# What a successful send means on each backend (D4/D7): Claude gives no acknowledgment at all.
SUCCESS_OUTCOME = {"claude": WRITTEN, "codex": ACCEPTED}


def warn(msg):
    # Error text can embed peer- or daemon-supplied strings (names, cwd, exception text), so it is
    # rendered safely too. Newlines inside a message are kept.
    print(f"agent-boardroom: {safe_text(msg)}", file=sys.stderr)


def line(*parts):
    """One human output line built from untrusted pieces: each piece is flattened and sanitized."""
    print(" ".join(safe_text(p, one_line=True) for p in parts))


# ---------------------------------------------------------------- identity

def me(as_backend=None):
    """Identify the calling session. Ambiguity is an error, never a guess (D2).

    Each backend's whoami() returns None when this process doesn't advertise that identity (its
    env var is absent), a verified Session, or raises when it advertises one that doesn't check out
    (stale, malformed, not live). An advertised-but-broken identity still counts: it means some
    agent launched us, and silently falling back to the other backend could mislabel a nested
    session. So:
      --as B         use B; it must verify. The other backend is ignored, since the caller resolved it.
      >1 advertised  error, whether the extra one verified or not: pass --as
      1 advertised   use it if it verified, otherwise fail with its error
      0 advertised   terminal sender (can send, can't receive)
    Env vars are inherited (Claude running `codex exec` sees both), which is why "both" is common.
    """
    if as_backend:
        s = BACKEND[as_backend].whoami()
        if s is None:
            raise BoardroomError(f"--as {as_backend}: this process doesn't advertise a {as_backend} identity")
        return s
    advertised = {}
    for name in BACKENDS:
        try:
            s = BACKEND[name].whoami()
        except (BoardroomError, OSError) as e:
            advertised[name] = e
            continue
        if s is not None:
            advertised[name] = s
    if len(advertised) > 1:
        detail = "; ".join(f"{k}: {'ok' if isinstance(v, Session) else v}" for k, v in advertised.items())
        raise BoardroomError(f"more than one agent identity is advertised ({detail}); pass --as claude or --as codex")
    if advertised:
        (name, value), = advertised.items()
        if isinstance(value, Session):
            return value
        raise BoardroomError(f"this process advertises a {name} identity that doesn't verify: {value}")
    user = getpass.getuser()
    return Session("terminal", f"terminal-{user}", f"Terminal ({user})")


# ---------------------------------------------------------------- discovery

def gather(backends=BACKENDS, strict=False):
    """List sessions. strict=True (sends) fails if any backend can't be listed; list is lenient."""
    out = []
    for name in backends:
        try:
            out += BACKEND[name].list_sessions()
        except (BoardroomError, OSError) as e:
            if strict:
                hint = (" A bare target can't be checked for ambiguity; use an explicit claude:/codex: prefix."
                        if len(backends) > 1 else "")
                raise BoardroomError(f"{name} backend unavailable ({e}).{hint}") from e
            warn(f"{name} backend unavailable: {e}")
    return out


def split_target(target):
    head, sep, rest = target.partition(":")
    return (head, rest) if sep and head in BACKENDS else ("", target)


def resolve(target):
    """Resolve a human target: exact id or name first, then a unique id/name prefix.

    A bare target searches both backends and fails closed if either can't be listed: the session
    you meant might be on the backend we couldn't see, and a prefix match on the other one would
    then send to the wrong recipient. An explicit prefix only needs its own
    backend.
    """
    backend, needle = split_target(target)
    if not needle:
        raise BoardroomError(f"empty target {target!r}")
    pool = gather([backend] if backend else BACKENDS, strict=True)
    hits = [s for s in pool if s.id == needle or s.name == needle] or \
           [s for s in pool if s.id.startswith(needle) or (s.name or "").startswith(needle)]
    if len(hits) == 1:
        return hits[0]
    choices = ", ".join(f"{s.address} ({s.name or 'unnamed'})" for s in (hits or pool)) or "none running"
    raise BoardroomError(f"{'ambiguous' if hits else 'no session matches'} {target!r}; candidates: {choices}")


def resolve_exact(backend, session_id):
    """Strict resolver for replies: exact backend + id, no name or prefix fallback.

    A reply must reach the session that sent the message or nobody. If that session is gone, its id
    could coincide with another session's name or id prefix, and a lenient match would misroute.
    """
    if backend not in BACKENDS:
        raise BoardroomError(f"{backend}:{session_id} cannot receive messages")
    validate_routing_id(session_id, "logged session id")
    hits = [s for s in gather([backend], strict=True) if s.id == session_id]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise BoardroomError(f"{backend}:{session_id} is no longer running; the reply was not sent")
    # Duplicate canonical ids (D26): never pick the first. --as can't resolve this,
    # because it only selects a backend; the user has to close the duplicate session.
    raise BoardroomError(f"{backend}:{session_id} is live {len(hits)} times; refusing to guess which one "
                         "should get the reply. Close the duplicate session(s), then retry.")


# ---------------------------------------------------------------- send

def read_body(parts):
    body = sys.stdin.read() if parts == ["-"] else " ".join(parts)
    if not body.strip():
        raise BoardroomError("empty message")
    if len(body.encode()) > MAX_BODY_BYTES:  # UX cap; the wire cap is enforced by the backend (D15)
        raise BoardroomError(f"message exceeds {MAX_BODY_BYTES // 1024} KiB")
    return body


def validate_wait(target, level, timeout):
    """Check --wait/--wait-timeout BEFORE any transport (contract §A). Returns (level, timeout)."""
    if level is None:
        if timeout is not None:
            raise BoardroomError("--wait-timeout needs --wait")
        return None, None
    supported = BACKEND[target.backend].supported_levels()
    if level not in supported:
        raise BoardroomError(f"--wait {level} isn't available for {target.backend} targets "
                             f"(supported: {', '.join(supported)}); nothing was sent")
    timeout = 30.0 if timeout is None else timeout
    if not math.isfinite(timeout) or timeout <= 0:
        raise BoardroomError("--wait-timeout must be a finite positive number; nothing was sent")
    return level, timeout


def deliver(sender, target, body, reply_to=None, disabled=(), wait=None, wait_timeout=None):
    # D14: a session messaging itself would re-trigger itself in a loop.
    if target.address == sender.address:
        raise BoardroomError("refusing to send a message to the session it came from")
    validate_routing_id(target.id, "target session id")
    wait, wait_timeout = validate_wait(target, wait, wait_timeout)
    # D13: re-check right before sending. Discovery may be seconds old, and a Claude session can
    # exit or a Codex thread unload in between. Best effort, not atomic: the target can still
    # vanish between this check and the write.
    target = BACKEND[target.backend].refresh(target)
    msg_id = new_msg_id()
    text = wrap(sender, body, msg_id, reply_to, target.backend)

    # D3 + v0.3 C: the guard checks (depth, rate, duplicate) and the attempt append happen under ONE
    # log lock, so concurrent senders can't both slip under a limit. The attempt is durably on disk
    # BEFORE delivery; if it can't be logged or a guard refuses, nothing is sent.
    attempt = {"event": "attempt", "msg_id": msg_id, "reply_to": reply_to,
               "from": sender.public(), "to": target.public(), "body": body}
    if disabled:
        attempt["guards_disabled"] = sorted(disabled)
    try:
        reserve_attempt(attempt, sender.address, target.address, body, reply_to, disabled)
    except LogError as e:
        raise BoardroomError(f"could not log the attempt, so the message was NOT sent: {e}") from e
    except BoardroomError as e:
        raise BoardroomError(f"{e} Nothing was sent.") from e

    # D4/D5: distinct outcomes, never a retry. Neither backend drops repeated copies, so resending
    # after an uncertain result can make the other agent act twice.
    try:
        receipt = BACKEND[target.backend].send(target, text, msg_id, sender.address)
    except DeliveryUnknown as e:
        _log_outcome(msg_id, UNKNOWN, detail=str(e))
        warn(f"delivery unknown for msg-id {msg_id}: {e}. Do NOT resend blindly; ask the target first.")
        sys.exit(2)
    except (BoardroomError, OSError) as e:
        _log_outcome(msg_id, FAILED, detail=str(e))
        raise BoardroomError(f"send failed (msg-id {msg_id}): {e}") from e

    outcome = SUCCESS_OUTCOME[target.backend]
    logged = _log_outcome(msg_id, outcome, receipt=receipt)
    line(f"{outcome}:", target.address, f"({target.name or 'unnamed'})", f"msg-id={msg_id}",
         *([f"reply-to={reply_to}"] if reply_to else []))
    if not logged:
        # The message went out; only the bookkeeping failed. Reporting this as a send failure
        # would invite a duplicate resend, so it gets its own exit code.
        warn(f"msg-id {msg_id}: transport {outcome}, but that outcome could NOT be logged (receipt {receipt}). "
             "Do NOT resend; `agent-boardroom log` will show it as pending."
             + (" The requested --wait was skipped." if wait else ""))
        sys.exit(3)
    if wait:
        wait_for_receipt(Session(target.backend, target.id, target.name, target.status, target.cwd),
                         msg_id, wait, wait_timeout, outcome)
    return msg_id


def _level_rank(level):
    return RECEIPT_LEVELS.index(level) if level in RECEIPT_LEVELS else -1


def _probe(session, msg_id, level, deadline):
    """One observe() call; backend exceptions become an 'error' observation (fixed reason only)."""
    try:
        return BACKEND[session.backend].observe(session, msg_id, level, deadline)
    except (BoardroomError, OSError):
        from .common import utc_now
        return Observation("error", None, utc_now(), {"backend": session.backend, "session_id": session.id},
                           reason="probe_failed")


def wait_for_receipt(session, msg_id, level, timeout, outcome):
    """Poll single bounded scans under ONE monotonic deadline (the CLI owns polling, contract §A).

    Never resends. Exits 0 (observed and saved), 3 (observed, not saved) or 4 (not established).
    """
    deadline = time.monotonic() + timeout
    best = last = None
    while True:
        obs = _probe(session, msg_id, level, deadline)
        last = obs
        if obs.level_reached is not None and (best is None or _level_rank(obs.level_reached) > _level_rank(best.level_reached)):
            best = obs
        # Defensive: "observed" must also mean the REQUESTED level was reached.
        if obs.state == "observed" and _level_rank(obs.level_reached) >= _level_rank(level):
            try:
                record_observation(msg_id, obs.level_reached, obs)
            except LogError as e:
                warn(f"receipt {obs.level_reached} observed for msg-id {msg_id}, but the receipt observation "
                     f"was not saved ({e}). The message itself was {outcome}; do NOT resend.")
                sys.exit(3)
            line(f"receipt: {obs.level_reached}", f"msg-id={msg_id}",
                 f"evidence_at={obs.evidence_at or '-'}", f"observed_at={obs.observed_at}")
            return
        if level == "turn_completed" and obs.state == "not_observed" and obs.reason == "turn_failed":
            # F4: the exact matched Codex turn is listed as failed, and a failed turn can never become
            # completed under the same id, so waiting longer can't help. Stop now; never resend.
            reached = best.level_reached if best else None
            warn(f"sent ({outcome}) msg-id={msg_id}; receipt turn_completed can't be established: the "
                 f"receiving turn failed in the current history snapshot (highest level seen: "
                 f"{reached or 'none'}). DO NOT RESEND; check the Codex thread.")
            sys.exit(4)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.5, remaining))
    reached = best.level_reached if best else None
    why = last.reason or last.state if last else "no probe ran"
    # The receipt ladder only sees NATIVE history. An agent that reads its queue through a tool (e.g.
    # Codex's thread/queue/list mid-turn) leaves no per-message trace, so "not established" is never
    # evidence that the message went unread.
    msg = (f"sent ({outcome}) msg-id={msg_id}; receipt {level} not established within {timeout:g} s "
           f"(highest level seen: {reached or 'none'}; last probe: {why}). DO NOT RESEND: the receiver "
           "may still read it later, or may already have read it through its queue tools, which leave "
           "no receipt trace.")
    if session.backend == "claude" and best is None:
        # D33 (resolved as a hint only): no probe in this whole wait found ANY positive level, so the
        # receiving Claude session's cross-session gate may be holding or refusing it. agent-boardroom
        # can't observe that decision (it reads no mode or settings, see docs/DESIGN.md), so this
        # is a factual pointer, never a claim. Suppressed once any probe saw recorded/responded:
        # the gate had admitted it by then.
        msg += (" Claude may be awaiting approval or refusing cross-session input; check the "
                "receiving session. agent-boardroom cannot observe its gate decision.")
    warn(msg)
    sys.exit(4)


def _log_outcome(msg_id, outcome, **extra):
    """Log an outcome. Returns False instead of raising: by now the send has already happened."""
    try:
        log_event({"event": "outcome", "msg_id": msg_id, "outcome": outcome, **extra})
        return True
    except LogError as e:
        warn(f"could not log outcome {outcome!r} for msg-id {msg_id}: {e}")
        return False


def message_status(msg_id):
    """Last logged outcome for a message, or None if only its attempt was logged."""
    outcomes = [r.get("outcome") for r in read_log() if r.get("event") == "outcome" and r.get("msg_id") == msg_id]
    return outcomes[-1] if outcomes else None


def normalize_reply_to(raw, disabled):
    """--reply-to must name a logged message (validated ancestry, contract §C); the stored value is
    always the full msg-id, never a prefix. Only an explicit --no-guard depth accepts an unknown id."""
    if raw is None:
        return None
    try:
        return find_message(raw)["msg_id"]
    except BoardroomError as e:
        if "depth" not in disabled:
            raise BoardroomError(f"--reply-to {raw!r} can't be verified against the log ({e}); refusing. "
                                 "Override with --no-guard depth only if you know the thread.") from e
        return validate_routing_id(raw, "reply-to id")


def cmd_send(a):
    disabled = tuple(a.no_guard or ())
    reply_to = normalize_reply_to(a.reply_to, disabled)
    deliver(me(a.as_backend), resolve(a.target), read_body(a.message), reply_to,
            disabled, a.wait, a.wait_timeout)


def cmd_reply(a):
    sender = me(a.as_backend)
    rec = find_message(a.msg_id)
    # Only a party to the original message may reply to it, compared by full backend + id.
    # Replying to a message we received goes to its sender; following up on
    # one we sent goes to its recipient. A third session that wants to join in uses
    # `send --reply-to` explicitly, so it's clear it was never addressed.
    if same_endpoint(rec.get("from"), sender):
        other = rec.get("to")
    elif same_endpoint(rec.get("to"), sender):
        other = rec.get("from")
    else:
        raise BoardroomError(f"{sender.address} was neither the sender nor the recipient of {rec['msg_id']}; "
                             "use `agent-boardroom send <target> --reply-to <msg-id>` to join a conversation")
    if not isinstance(other, dict):
        raise BoardroomError(f"log record for {rec['msg_id']} is missing its other endpoint")
    # Replying to a message whose own send failed is allowed: a reply is an explicit NEW message,
    # not a retransmission of the original, so D5 doesn't apply. We warn so the sender knows the
    # other side may never have seen what this reply refers to.
    status = message_status(rec["msg_id"])
    if status in (FAILED, UNKNOWN, None):
        warn(f"note: the original message {rec['msg_id'][:8]} is {status or 'pending (no outcome logged)'}; "
             "the recipient may not have seen it")
    target = resolve_exact(other.get("backend"), other.get("id"))
    deliver(sender, target, read_body(a.message), rec["msg_id"],
            tuple(a.no_guard or ()), a.wait, a.wait_timeout)


# ---------------------------------------------------------------- other commands

def cmd_list(a):
    sessions = gather()
    if a.json:
        print(json_dumps_safe([s.public() for s in sessions], indent=2))
        return
    if not sessions:
        print("No live sessions.")
        return
    try:
        mine = me().address
    except BoardroomError:
        mine = None
    for s in sorted(sessions, key=lambda s: (s.backend, s.name or "", s.id)):
        flag = "*" if s.address == mine else " "
        name = safe_text(s.name or "(unnamed)", one_line=True)
        addr = safe_text(s.address, one_line=True)
        print(f"{flag} {addr:46} {name:32.32} {safe_text(s.status, True):8} {safe_text(s.cwd, True)}")


def cmd_whoami(a):
    s = me(a.as_backend)
    if a.json:
        print(json_dumps_safe(s.public(), indent=2))
    else:
        line(s.address, f" {s.name or '(unnamed)'}")


def cmd_name(a):
    s = me(a.as_backend)
    if s.backend not in BACKENDS:
        raise BoardroomError("only Claude or Codex sessions can be renamed")
    new = a.name.strip()
    if not new or len(new) > 64:
        raise BoardroomError("name must be 1-64 characters")
    try:
        s = BACKEND[s.backend].rename(s, new)
    except DeliveryUnknown as e:
        warn(f"rename result unknown: {e}")
        sys.exit(2)
    line("renamed", s.address, "->", s.name)


def _addr(p):
    return f"{p.get('backend')}:{p.get('id')}" if isinstance(p, dict) else None


def derive_status(records):
    """Per-message derived view: last transport outcome, best receipt, and linked reply attempts.

    Linked reply attempts (contract §E): a later attempt R with R.reply_to == M.msg_id and endpoints
    SWAPPED relative to M (full backend+id). Labels: reply_recorded > reply_written/reply_accepted >
    reply_unknown > reply_pending. A failed R is not listed and never overwrites a success. Third-party
    --reply-to attempts are "referenced by", never replies. No label is a semantic acknowledgment.
    """
    attempts = {r["msg_id"]: r for r in records if r.get("event") == "attempt" and isinstance(r.get("msg_id"), str)}
    outcome, receipt = {}, {}
    for r in records:
        mid = r.get("msg_id")
        if r.get("event") == "outcome":
            outcome[mid] = r.get("outcome")
        elif r.get("event") == "observation" and r.get("state") == "observed":
            if _level_rank(r.get("level")) > _level_rank(receipt.get(mid)):
                receipt[mid] = r.get("level")
    rank = {"reply_recorded": 4, "reply_written": 3, "reply_accepted": 3, "reply_unknown": 2, "reply_pending": 1}
    replies, refs = {}, {}
    for r in attempts.values():
        parent = attempts.get(r.get("reply_to"))
        if parent is None:
            continue
        swapped = _addr(r.get("from")) == _addr(parent.get("to")) and _addr(r.get("to")) == _addr(parent.get("from"))
        if not swapped:
            refs.setdefault(parent["msg_id"], []).append(r["msg_id"])
            continue
        o = outcome.get(r["msg_id"])
        if o == FAILED:
            continue
        label = ("reply_recorded" if receipt.get(r["msg_id"]) else
                 {WRITTEN: "reply_written", ACCEPTED: "reply_accepted", UNKNOWN: "reply_unknown"}.get(o, "reply_pending"))
        best = replies.get(parent["msg_id"])
        if best is None or rank[label] > rank[best[0]]:
            replies[parent["msg_id"]] = (label, r["msg_id"])
    return outcome, receipt, replies, refs


def _fmt(rec, outcome, full, receipt=None, reply=None, refs=None):
    f, t = rec.get("from") or {}, rec.get("to") or {}
    who = lambda p: f"{p.get('name') or p.get('id')} [{p.get('backend')}:{str(p.get('id'))[:8]}]"
    # "pending" = an attempt with no outcome record. Either a send is in flight right now, or the
    # sender died between logging the attempt and logging the outcome. In the second case the
    # message may or may not have been delivered: treat it as delivery-unknown, not as unsent.
    status = outcome or "pending (no outcome logged: delivery unknown)"
    head = f"{rec.get('ts')}  {who(f)} -> {who(t)}  msg={str(rec.get('msg_id'))[:8]}  {status}"
    if receipt:
        head += f"  receipt={receipt}"
    if rec.get("reply_to"):
        head += f"  reply-to={str(rec['reply_to'])[:8]}"
    if reply:
        head += f"  {reply[0]}={reply[1][:8]}"
    if refs:
        head += f"  referenced-by={','.join(x[:8] for x in refs)}"
    head = safe_text(head, one_line=True)
    lines = safe_text(rec.get("body", "")).splitlines() or [""]
    if not full and len(lines) > 6:
        lines = lines[:6] + [f"... ({len(lines) - 6} more lines; --full to show)"]
    return head + "\n" + "\n".join("    " + ln for ln in lines)


def cmd_log(a):
    def render(records):
        outcome, receipt, replies, refs = derive_status(records)
        attempts = [r for r in records if r.get("event") == "attempt"]
        return [_fmt(r, outcome.get(r.get("msg_id")), a.full, receipt.get(r.get("msg_id")),
                     replies.get(r.get("msg_id")), refs.get(r.get("msg_id"))) for r in attempts]

    if a.n < 1:
        raise BoardroomError("-n must be at least 1")
    records = read_log()
    skipped = read_log.skipped
    if skipped:
        warn(f"{skipped} unreadable log line(s) skipped (a writer was interrupted mid-line); "
             "any message they described has unknown delivery")
    if a.json:
        for r in records[-a.n * 2:]:
            print(json_dumps_safe(r))
    else:
        for block in render(records)[-a.n:]:
            print(block + "\n")
    if not a.follow:
        return
    seen = len(records)
    try:
        while True:
            time.sleep(1)
            records = read_log()
            for r in records[seen:]:
                if a.json:
                    print(json_dumps_safe(r), flush=True)
                elif r.get("event") == "attempt":
                    print(_fmt(r, None, a.full) + "\n", flush=True)
                elif r.get("event") == "observation":
                    print(safe_text(f"{r.get('ts')}  msg={str(r.get('msg_id'))[:8]}  receipt {r.get('level')}", True)
                          + "\n", flush=True)
                else:
                    print(safe_text(f"{r.get('ts')}  msg={str(r.get('msg_id'))[:8]}  {r.get('outcome')}"
                                    + (f"  {r['detail']}" if r.get("detail") else ""), True) + "\n", flush=True)
            seen = len(records)
    except KeyboardInterrupt:
        pass


def cmd_receipt(a):
    """Inspection only (contract §A): read-only unless --record; never assesses resend safety."""
    rec = find_message(a.msg_id)
    to = rec.get("to") or {}
    backend = to.get("backend")
    if backend not in BACKENDS:
        raise BoardroomError(f"{backend}:{to.get('id')} has no receipt evidence to inspect")
    supported = BACKEND[backend].supported_levels()
    level = a.level or supported[-1]
    if level not in supported:
        raise BoardroomError(f"level {level} isn't available for {backend} (supported: {', '.join(supported)})")
    if not math.isfinite(a.timeout) or a.timeout <= 0:
        raise BoardroomError("--timeout must be a finite positive number")
    # The LOGGED destination, not a live lookup: historical checks work after the target has exited.
    session = Session(backend, validate_routing_id(to.get("id"), "logged session id"),
                      to.get("name") or "", "", to.get("cwd") or "")
    obs = _probe(session, rec["msg_id"], level, time.monotonic() + a.timeout)
    # Persist first (if asked), but ALWAYS print the evidence before any exit: a save failure must
    # never hide positive evidence.
    saved, save_error = None, None  # saved: None = not requested; True = written; False = dedup hit
    if a.record and obs.level_reached is not None:
        positive = Observation("observed", obs.level_reached, obs.observed_at, obs.source, obs.evidence_at)
        try:
            saved = record_observation(rec["msg_id"], obs.level_reached, positive)
        except LogError as e:
            save_error = e
    status = ("failed" if save_error else
              {None: "not_requested", True: "written", False: "already_recorded"}[saved])
    if a.json:
        print(json_dumps_safe({"msg_id": rec["msg_id"], "requested": level, "recorded": status,
                               **obs.__dict__}, indent=2))
    else:
        line(f"msg-id={rec['msg_id']}", f"to={backend}:{to.get('id')}", f"requested={level}")
        line(f"state={obs.state}", f"level_reached={obs.level_reached or 'none'}",
             f"reason={obs.reason or '-'}")
        line(f"observed_at={obs.observed_at}", f"evidence_at={obs.evidence_at or '-'}",
             "source=" + json_dumps_safe(obs.source, sort_keys=True))
        if saved is not None:
            line("recorded" if saved else "already recorded (dedup)")
        line("(inspection only: this does not assess whether a resend is safe)")
    if save_error is not None:
        warn(f"receipt observation not saved: {save_error}")
        sys.exit(3)
    if obs.state not in ("observed", "not_observed"):
        sys.exit(1)


def cmd_doctor(a):
    """Bounded, read-only health checks (contract §F). Changes nothing; prints no secrets."""
    if not math.isfinite(a.timeout) or a.timeout <= 0:
        raise BoardroomError("--timeout must be a finite positive number")
    deadline = time.monotonic() + a.timeout
    checks = []
    from .common import BOARDROOM_HOME, LOG_PATH
    try:
        mode = oct(os.stat(BOARDROOM_HOME).st_mode & 0o777) if BOARDROOM_HOME.exists() else None
        lmode = oct(os.stat(LOG_PATH).st_mode & 0o777) if LOG_PATH.exists() else None
        ok = mode in (None, "0o700") and lmode in (None, "0o600")
        checks.append({"check": "agent-boardroom log", "status": "ok" if ok else "warn",
                       "detail": f"{LOG_PATH} dir={mode or 'absent'} file={lmode or 'absent'}"})
    except OSError as e:
        checks.append({"check": "agent-boardroom log", "status": "error", "detail": type(e).__name__})
    for name in BACKENDS:
        fn = getattr(BACKEND[name], "doctor", None)
        if fn is None:
            checks.append({"check": f"{name} backend", "status": "warn", "detail": "no doctor() implemented"})
            continue
        try:
            checks += fn(deadline)
        except (BoardroomError, OSError) as e:
            checks.append({"check": f"{name} backend", "status": "error", "detail": type(e).__name__})
    if a.json:
        print(json_dumps_safe(checks, indent=2))
    else:
        for c in checks:
            line(f"[{c.get('status')}]", f"{c.get('check')}:", c.get("detail", ""))
    if any(c.get("status") == "error" for c in checks):
        sys.exit(1)


class Parser(argparse.ArgumentParser):
    # argparse exits 2 on usage errors, but 2 means "delivery unknown, do not resend" here. A typo
    # must never look like a possibly-delivered message, so usage errors exit 1.
    def error(self, message):
        # The message can echo argument values, so it is sanitized like any other output.
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {safe_text(message, one_line=True)}\n")


def _run(argv=None):
    p = Parser(prog="agent-boardroom", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"agent-boardroom {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_as(sp):
        sp.add_argument("--as", dest="as_backend", choices=BACKENDS,
                        help="which agent you are, when more than one identity is advertised")
        return sp

    sp = sub.add_parser("list"); sp.add_argument("--json", action="store_true")
    sp = with_as(sub.add_parser("whoami")); sp.add_argument("--json", action="store_true")
    def with_send_opts(sp):
        sp.add_argument("--wait", choices=RECEIPT_LEVELS,
                        help="after sending, wait for this receipt level (exit 4 if not established)")
        sp.add_argument("--wait-timeout", type=float, help="seconds to wait (default 30)")
        sp.add_argument("--no-guard", action="append", choices=GUARDS,
                        help="explicitly disable one loop/flood guard (recorded on the attempt)")
        return sp

    sp = with_send_opts(with_as(sub.add_parser("send"))); sp.add_argument("--reply-to"); sp.add_argument("target")
    sp.add_argument("message", nargs="+")
    sp = with_send_opts(with_as(sub.add_parser("reply"))); sp.add_argument("msg_id"); sp.add_argument("message", nargs="+")
    sp = sub.add_parser("receipt"); sp.add_argument("msg_id")
    sp.add_argument("--level", choices=RECEIPT_LEVELS); sp.add_argument("--record", action="store_true")
    sp.add_argument("--timeout", type=float, default=10.0); sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("doctor"); sp.add_argument("--timeout", type=float, default=10.0)
    sp.add_argument("--json", action="store_true")
    setup_mod.add_parser(sub)
    sp = with_as(sub.add_parser("name")); sp.add_argument("name")
    sp = sub.add_parser("log"); sp.add_argument("-n", type=int, default=20)
    sp.add_argument("--full", action="store_true"); sp.add_argument("--follow", action="store_true")
    sp.add_argument("--json", action="store_true")

    a = p.parse_args(argv)
    {"list": cmd_list, "whoami": cmd_whoami, "send": cmd_send, "reply": cmd_reply, "receipt": cmd_receipt,
     "name": cmd_name, "log": cmd_log, "doctor": cmd_doctor, "setup": setup_mod.cmd_setup}[a.cmd](a)


def main(argv=None):
    """Console entry point. This is THE error boundary: every exit path maps to the documented codes
    and expected failures never show a traceback. The installed `agent-boardroom` command, the
    repo-root dev shim and `python -m agent_boardroom` all come through here, so the exit-code
    contract (2 = delivery unknown, do not resend) is identical however the tool was started."""
    try:
        _run(argv)
    except DeliveryUnknown as e:  # anything that escaped a command's own handling, e.g. rename
        warn(f"result unknown: {e}. Do NOT retry blindly.")
        sys.exit(2)
    except BoardroomError as e:
        warn(str(e))
        sys.exit(1)
    except OSError as e:
        warn(f"system error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
