# agent-boardroom design

How agent-boardroom works, which behavior of the two agents it relies on, and why it's built this
way. Code comments refer to the decision numbers (D1–D35) listed at the end.

What these descriptions are based on:
- **Claude Code:** observed behavior of a **private, undocumented interface**, verified against
  Claude Code 2.1.290 on macOS.
- **Codex:** the open-source `openai/codex` app-server at tag `rust-v0.160.1` (commit `d27764b8`).
  Its control socket is a documented concept; the queue and paginated-history endpoints used here
  are marked **experimental**.

The source corresponds to the **installed** CLI version. The running daemon's version isn't
verified, so these are compatibility observations, not guarantees.

## Files

| File | Role |
|---|---|
| `agent_boardroom/cli.py` | CLI: identity, target resolution, send/reply/receipt/log/doctor, guards, exit codes. `main()` is the single error boundary for the installed command, `python -m agent_boardroom` and the dev shim. |
| `agent_boardroom/common.py` | Shared types, the message envelope, validation, the durable log, guards, terminal-safe rendering |
| `agent_boardroom/claude.py` | Claude Code backend: session discovery, socket delivery, transcript receipts |
| `agent_boardroom/codex.py` | Codex backend: app-server WebSocket client, queue delivery, history receipts |
| `agent_boardroom/setup.py` | `setup`: installs the bundled skills under an ownership manifest (see below) |
| `agent_boardroom/skills/…` | The bundled skills, one canonical copy each, read at runtime via `importlib.resources` |
| `agent-boardroom` (repo root) | Development shim to run from a clone without installing |

## Packaging and `setup`

- The package is standard setuptools with a console entry point; runtime needs only the standard
  library. The skills ship as package data.
- `setup` records what it installs in a manifest (`.agent-boardroom-manifest.json`) inside each
  skill directory: tool, schema, version, timestamp and a sha256 per file. **Ownership is recognized
  only when the manifest passes the full schema check**; a partial, corrupt or foreign marker is
  reported as malformed and never authorizes a write or a delete.
- Before writing, every path in the new bundle is preflighted against the manifest *and* the
  filesystem. Unchanged managed files are updated freely; locally changed ones are refused unless
  `--force` (with a backup of exactly those files); an occupied path the manifest doesn't own is a
  conflict that no flag overrides; unmanaged directories are untouched unless `--adopt`, which
  backs up only the paths it replaces. Symlinks and special files, as the destination or as a leaf,
  are reported and refused, never followed. Paths retired from the bundle are left in place and
  reported as no longer managed.
- All reads are no-follow, regular-file only and bounded (manifest size, per-file size,
  inventory count, and a deadline that `doctor` passes through), so inspection can't block on a
  FIFO or a huge file; a bound is reported as `unreadable`.
- Uninstall removes only listed, unchanged regular files, and removes the directory only when
  empty. Files are written atomically and the manifest last. This is not a multi-file
  transaction: an interrupted upgrade can leave new files beside the old manifest (then reported
  as modified) or, on a fresh install, files without a manifest (reported as unmanaged). The
  backup taken before a forced replace holds the previous content for recovery.
- Deferred: a read-only check for a legacy duplicate of the skill at another path.
- `setup` has its own exit codes (0 clean, 1 refused or failed) and never borrows message-delivery
  meanings; `doctor` runs afterwards for information only.

## Identity and addressing

- **Addresses:** a session is addressed as `claude:<sessionId>` or `codex:<threadId>`. The ID is the
  routing key; names are display only (D1).
- **Who is sending:** the caller's identity comes from the environment each agent sets for the
  commands it runs (`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`), and is then verified against the
  live session. More than one identity, or a stale one, is an error; pass `--as` to pick (D2, D25).
- **Resolving a target:** exact ID or name first, then a unique prefix. A bare target fails closed if
  either backend can't be listed (D17).
- **Replies:** a reply resolves only the exact logged backend and ID (D16). The same ID live in two
  processes is refused (D26).

## Envelope

Every message is wrapped in a `<cross-session-message from="…" from-session="…" from-name="…">`
block, the format Claude Code itself uses for peer messages (D8):
- **Header:** a first line carrying the msg-id and reply-to.
- **Codex targets:** an added note that the text is from another agent, not the user.
- **Footer:** how to reply.

Claude Code rebuilds the parsed wrapper and requires an identical result. So attribute values are
validated or sanitized up front (D9):
- the `from` address and session ID are validated, never rewritten
- display names are cut to a safe character set of at most 64 characters

## The log

`~/.agent-boardroom/log.jsonl` holds one JSON event per line:

| Event | Meaning |
|---|---|
| `attempt` | written **before** delivery and `fsync`ed. No log means no send (D3). |
| `outcome` | `written`, `accepted`, `failed` or `delivery-unknown` (D4) |
| `observation` | a positive receipt level, written only on request (D28) |

How the file is protected (D10):
- appends take an exclusive `flock`
- writes go through a write-all loop, then `fsync`
- a torn last line is repaired on the next append
- the directory is 0700 and the file 0600

## Delivery: Claude Code (private interface)

- **Discovery.** Each interactive Claude Code session writes a JSON record under
  `$CLAUDE_CONFIG_DIR/sessions/<pid>.json`, containing its session ID, name, working directory and
  the path of a per-session Unix socket. A record counts as live only if its process is running
  *and* its socket exists.
- **Wire format.** Newline-delimited JSON over that socket:
  - an optional auth line, using a token the session publishes in a sibling key file named after a
    hash of the socket path (required only on Windows; agent-boardroom sends it when present)
  - then a `user` frame carrying the envelope

  The receiver drops a connection if its receive buffer passes about one million UTF-16 code units
  before line splitting, so agent-boardroom measures the whole serialized payload in those units
  before connecting (D15). Every frame names the target session, and the receiver discards frames
  meant for a different one. That guards against a reused process or socket (D18).
- **The receiver's gate** decides whether to accept, hold or refuse a peer message.
  - An effective cross-session setting (`crossSessionInbound`) is evaluated **first**, and can
    force accept, hold or refuse.
  - Otherwise a session running with skip-permissions typically holds peer messages that don't
    declare a matching permission mode.
  - agent-boardroom never declares a mode (D6), so it can't talk past that gate.
  - It can't observe the gate's decision either. A held message may later be released, and then
    produce history receipts if the receipt predicates are met.
- **Rename.** A control frame asks the session to rename itself. Success is confirmed by reading
  the registry back.

## Delivery: Codex (open source)

- **Transport.** agent-boardroom talks to the existing app-server daemon over WebSocket on its Unix
  control socket, using JSON-RPC. It never starts a server or resumes a thread (D13).
- **Methods.** Discovery uses `thread/loaded/list` and `thread/read`. Rename uses
  `thread/name/set`. Delivery uses `thread/queue/add`, which wakes a loaded *idle* thread.
  **Interrupted threads are not woken**: the message waits until the user resumes the thread.
- **Write boundary.** A failure before the request is written is a definite `failed`. A failure
  after it is `delivery-unknown`, never retried (D5).
- **Thread status.** Threads in an unknown status stay listed but can't be sent to (D22).
  `systemError` threads are refused (D23).

## Receipts (D28–D30, D34, D35)

Receipts are read-only probes of the **receiver's** persisted history, matched on the exact
message ID. There's no text matching. Each probe is one bounded scan under an absolute deadline.
Hitting a bound gives `incomplete`, which keeps any lower level already established. A missing
receipt is never treated as non-delivery.

**Claude Code**, from the receiver's transcript at `$CLAUDE_CONFIG_DIR/projects/<slug>/<sessionId>.jsonl`:

| Level | Idle delivery | Busy (mid-turn) delivery |
|---|---|---|
| `recorded` | a `user` record whose UUID is the msg-id, marked as a peer message, in the right session, not a sidechain | an injected `queued_command` attachment record carrying the msg-id as both its source ID and its origin message ID |
| `responded` | an `assistant` record that is a direct child of that record | the first assistant record reached through **only** attachment records (D34) |

The busy-delivery `responded` rule is strict:
- every parent on the path has exactly one child in the **whole** transcript snapshot
- the path has at most 16 links, and the assistant record isn't an API error
- branches, duplicates, cycles, malformed or unparseable records, over-long IDs and a rewritten
  file all refuse the claim

The scan builds an index of every record before deciding, so a competing branch written anywhere
in the file is seen. The index has fixed memory caps.

**Codex**, from `thread/items/list` and `thread/turns/list`:

| Level | Evidence |
|---|---|
| `recorded` | a `userMessage` item whose `clientId` equals the msg-id |
| `responded` | a later `agentMessage` in the same turn |
| `turn_completed` | that turn's status is `completed` with no error |

`turn_completed` requires `recorded`, plus `responded`, plus that **same** turn's status
`completed` with `error` null.

**Early stop (D35).** `--wait turn_completed` exits 4 early with reason `turn_failed` when the
exact matched turn is listed `failed`.
- The status is checked even if no response was found.
- Before claiming it, the probe finishes the remaining turn pages within its bounds; a duplicate
  turn ID, a bad page or an exhausted budget prevents the early stop.
- Lower evidence is kept.
- `completed` with an error is neither success nor an early stop.

Why `failed` is final in the reviewed implementation:
- Receipts read only the paginated history store, because the items endpoint rejects legacy
  threads ([read path](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/thread-store/src/local/thread_history/read.rs#L182-L209)).
- The projection maps a turn completion that carries an error to `failed`
  ([projection](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/app-server-protocol/src/protocol/thread_history_projection.rs#L35-L55)).
- The store's upsert only updates rows that are still in progress with no end marker, so the first
  terminal status wins ([terminal upsert](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/thread-store/src/local/thread_history.rs#L329-L338)).

`interrupted` is excluded:
- The list endpoint *reports* any in-progress turn as interrupted whenever its thread isn't active
  ([synthesized interruption](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/app-server/src/request_processors/thread_processor.rs#L5811-L5823)).
- Suspension deliberately records no terminal event
  ([suspension](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/core/src/session/turn_suspension.rs#L69-L73)).
- Recovery resubmits under the **same** turn ID
  ([recovery](https://github.com/openai/codex/blob/d27764b82f7118f674371e6d6e76271d9d606edb/codex-rs/core/src/session/mod.rs#L1023-L1034)).

So a reported interruption can later complete. All of this is compatibility evidence for that
source version, not attestation of the running daemon.

**Held messages (D33).** When a Claude `--wait` times out with no receipt level at all, the error
adds that the session may be awaiting approval or refusing cross-session input. agent-boardroom
can't observe that decision. It deliberately doesn't try to predict it from mode or settings files,
because the gate's real inputs (enabled settings sources, flags, runtime state) aren't observable.

## Guards and rendering

- **Guards (D31).** Reply depth is capped at 8, a sender may make 30 sends a minute, and an
  identical body to the same recipient within 10 s is refused. The guards are checked under the
  same log lock as the attempt append, so concurrent sends can't both slip under a limit. Each one
  can be overridden explicitly.
- **Rendering (D32).** All human output passes through `safe_text`, which removes whole terminal
  control sequences, control characters and bidi characters. JSON output uses ASCII escapes.

## Re-checking after an agent update

1. Run `agent-boardroom doctor`. It flags a Claude Code or Codex version that wasn't tested.
2. Check that a live Claude session still writes `sessions/<pid>.json` with `sessionId` and
   `messagingSocketPath`, and that a received message still appears in its transcript under the
   msg-id.
3. Run the test suite, then send a real message and check it with `agent-boardroom receipt <msg-id>`.

## Decisions

| # | Decision |
|---|---|
| D1 | Route by session ID; names are display only. |
| D2 | Refuse ambiguity everywhere; a broken advertised identity counts as ambiguity. |
| D3 | Log the attempt (`fsync`ed) before delivery; no log means no send. |
| D4 | Transport outcomes say how strong the delivery confirmation is: `written` (Claude socket) vs `accepted` (Codex queue). Separate from history receipts. |
| D5 | Never retry automatically. |
| D6 | Never declare a permission mode to Claude Code. |
| D7 | No listening socket or registry entry of our own. |
| D8 | One envelope format for both backends. |
| D9 | Validate IDs exactly; sanitize display names to a 64-character safe set. |
| D10 | Durable shared log: lock, write-all, `fsync`, tail repair, private permissions. |
| D11 | Standard library only. |
| D12 | One module per backend. |
| D13 | Never start daemons or resume unloaded threads; re-check liveness before sending. |
| D14 | Refuse to message your own session. |
| D15 | The body cap is a usability limit; the wire limit is checked on the serialized payload. |
| D16 | `reply` resolves only the exact logged backend and ID, and only a party to the message may reply. |
| D17 | A bare target fails closed if any backend can't be listed. |
| D18 | Always send the target session ID to Claude Code. |
| D19 | Usage errors exit 1, so they can't look like "delivery unknown" (2). |
| D20 | Replying to a failed or unknown original is allowed, with a warning. |
| D21 | *(retired)* |
| D22 | A Codex thread with an unknown status is listed but can't be sent to. |
| D23 | Sends to Codex `systemError` threads are refused. |
| D24 | WebSocket masking stays a simple per-byte loop. |
| D25 | Sender identity needs only an existing, loaded session; readiness rules apply to targets. |
| D26 | A session ID that is live more than once is refused everywhere. |
| D27 | *(retired)* |
| D28 | Receipts come from the receiver's own history by exact ID, separate from the transport outcome. |
| D29 | Receipt levels differ by backend; each is backed by real structure. |
| D30 | `--wait` timeout exits 4 ("do not resend"); `receipt` is read-only by default. |
| D31 | Loop and flood guards, checked atomically with the reservation. |
| D32 | All human output is terminal-safe. |
| D33 | Held Claude messages: a timeout hint only; no prediction and no native receipt listener. |
| D34 | Busy Claude deliveries: `recorded` by exact identity; `responded` through attachment-only ancestry. |
| D35 | Codex `--wait turn_completed` stops early only on a listed `failed` turn. |
