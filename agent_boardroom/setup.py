"""`agent-boardroom setup`: install the bundled agent skills, then run doctor.

Ownership model (agreed in review):
  - Each installed skill directory carries a manifest, `.agent-boardroom-manifest.json`, recording
    the tool, schema, bundle version and the sha256 of every file we wrote. Ownership is
    recognized ONLY when that manifest passes the full schema check; corrupt, foreign or partial
    metadata never becomes ownership.
  - Every path the new bundle wants is preflighted against the manifest AND the filesystem before
    any write: a path we don't own that is occupied is a conflict, never silently replaced.
  - missing destination            -> install, write manifest
  - managed, files match manifest   -> update to the current bundle (an ordinary upgrade; no --force)
  - managed, already current        -> no-op
  - managed, files CHANGED locally  -> refuse and name the paths; --force replaces only those files
                                       after backing them up next to the skill
  - unmanaged (no valid manifest)   -> leave alone, even if identical: similarity isn't ownership.
                                       --adopt backs up ONLY the paths the bundle will replace
  - any symlink (dir or leaf)       -> reported and refused; never followed, never backed up
Uninstall removes only files the manifest lists and that are unchanged; it removes the directory
only when nothing else is left. Setup never touches the message log, never edits shell or agent
config, and never sends anything.

Bounds: every open is no-follow and requires a regular file; the manifest is read up to
MANIFEST_MAX bytes; skill files are hashed incrementally up to FILE_MAX; inventories are capped at
FILES_MAX entries; inspection honours an optional monotonic deadline (doctor passes its own).

Exit codes are setup's own, not message-delivery meanings:
  0  every selected target ended in a clean state (installed / updated / current / removed)
  1  invalid input, or a target was refused (conflict) or failed; the output names which
Diagnostics from doctor are printed after setup and never change setup's exit code.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from importlib import resources
from pathlib import Path

from . import __version__
from .common import BoardroomError, safe_text

MANIFEST = ".agent-boardroom-manifest.json"
SKILL_DIR = "agent-boardroom"
BUNDLED = ("claude", "codex")
TOOL, SCHEMA = "agent-boardroom", 1
MANIFEST_MAX = 64 * 1024
FILE_MAX = 4 * 1024 * 1024
FILES_MAX = 64
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z.+-]{1,64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class _Bound(Exception):
    """A read/inventory bound or the deadline was hit."""


# ---------------------------------------------------------------- bundle (one source of truth)

def bundle_files(agent):
    """{relative path: bytes} for the packaged skill of `agent`, read via importlib.resources."""
    root = resources.files(__package__) / "skills" / agent / SKILL_DIR
    out = {}
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if entry.is_file():
            out[entry.name] = entry.read_bytes()
    if "SKILL.md" not in out or not all(_safe_name(n) for n in out):
        raise BoardroomError(f"packaged skill for {agent} is invalid (broken install)")
    return out


def _sha(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- bounded, no-follow filesystem

def _safe_name(name):
    """Manifest and bundle paths: plain file names only, never the manifest itself or a backup dir."""
    return (isinstance(name, str) and bool(_NAME_RE.fullmatch(name)) and "\x00" not in name
            and name != MANIFEST and not name.startswith(".backup-"))


def _open_regular(path):
    """Open a REGULAR file without following symlinks; None if absent; raises _Bound otherwise."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as e:  # ELOOP for a symlink with O_NOFOLLOW, EACCES, ...
        raise _Bound(f"{type(e).__name__}") from e
    try:
        import stat as _stat
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise _Bound("not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_bounded(path, limit, deadline=None):
    fd = _open_regular(path)
    if fd is None:
        return None
    try:
        chunks, total = [], 0
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise _Bound("deadline")
            chunk = os.read(fd, min(65536, limit + 1 - total))
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                raise _Bound("too large")
            chunks.append(chunk)
    finally:
        os.close(fd)


def _hash_bounded(path, deadline=None):
    """sha256 of a regular file (no-follow), hashed incrementally; None if absent."""
    fd = _open_regular(path)
    if fd is None:
        return None
    try:
        h, total = hashlib.sha256(), 0
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise _Bound("deadline")
            chunk = os.read(fd, 65536)
            if not chunk:
                return h.hexdigest()
            total += len(chunk)
            if total > FILE_MAX:
                raise _Bound("too large")
            h.update(chunk)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- manifest

def read_manifest(dest, deadline=None):
    """None if no manifest; a validated dict; raises BoardroomError for anything not fully valid.

    Only a manifest that passes every check confers ownership (review P1): tool, schema, a version,
    a timestamp, a bounded non-empty inventory of safe names with sha256 digests.
    """
    try:
        raw = _read_bounded(dest / MANIFEST, MANIFEST_MAX, deadline)
    except _Bound as e:
        if str(e) == "deadline":
            raise  # a spent deadline is "unreadable" (retry later), never "malformed" (fix the file)
        raise BoardroomError(f"manifest unreadable ({e})") from e
    if raw is None:
        return None
    try:
        m = json.loads(raw)
    except (ValueError, RecursionError) as e:  # a few KB of nested arrays can exhaust the parser
        raise BoardroomError("manifest is not valid JSON") from e
    # Exact scalar typing: in Python True == 1 and 1.0 == 1, so compare type as well as value.
    if (not isinstance(m, dict) or m.get("tool") != TOOL
            or type(m.get("schema")) is not int or m.get("schema") != SCHEMA):
        raise BoardroomError("manifest is not an agent-boardroom manifest")
    if not isinstance(m.get("version"), str) or not _VERSION_RE.fullmatch(m["version"]):
        raise BoardroomError("manifest version malformed")
    if not isinstance(m.get("installed_at"), str) or len(m["installed_at"]) > 40:
        raise BoardroomError("manifest timestamp malformed")
    files = m.get("files")
    if (not isinstance(files, dict) or not files or len(files) > FILES_MAX
            or not all(_safe_name(k) and isinstance(v, str) and _SHA_RE.fullmatch(v) for k, v in files.items())):
        raise BoardroomError("manifest inventory malformed")
    return m


# ---------------------------------------------------------------- destinations

def destinations(claude_home=None, codex_home=None):
    """Default skill destinations. Codex's documented user-skill path is ~/.agents/skills."""
    claude_home = Path(claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    codex_home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return {
        "claude": {"home": claude_home, "dest": claude_home / "skills" / SKILL_DIR},
        "codex": {"home": codex_home, "dest": Path.home() / ".agents" / "skills" / SKILL_DIR},
    }


# ---------------------------------------------------------------- inspection (read-only)

def _lstat_kind(path):
    """'absent' | 'file' | 'symlink' | 'dir' | 'special' without following symlinks."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "special"
    import stat as _stat
    if _stat.S_ISLNK(st.st_mode):
        return "symlink"
    if _stat.S_ISREG(st.st_mode):
        return "file"
    if _stat.S_ISDIR(st.st_mode):
        return "dir"
    return "special"


def analyze(agent, dest, deadline=None):
    """Full read-only picture of a destination. Never follows symlinks, never blocks on special files.

    Returns a dict:
      state     missing | symlink | unmanaged | malformed | current | outdated | modified | unreadable
      detail    human text
      manifest  the validated manifest or None
      changed   managed files whose content differs from the manifest (or that aren't regular files)
      conflicts bundle paths that exist on disk but aren't owned by the manifest
      bundle    the current bundle {name: bytes}
    """
    bundle = bundle_files(agent)
    r = {"state": None, "detail": "", "manifest": None, "changed": [], "conflicts": [], "bundle": bundle}
    kind = _lstat_kind(dest)
    if kind == "symlink":
        return dict(r, state="symlink", detail="destination is a symlink (legacy manual install); not managed by setup")
    if kind == "absent":
        return dict(r, state="missing", detail="not installed")
    if kind != "dir":
        return dict(r, state="unmanaged", detail="destination exists but is not a directory")
    try:
        manifest = read_manifest(dest, deadline)
    except _Bound as e:
        return dict(r, state="unreadable", detail=f"could not inspect within bounds ({e})")
    except BoardroomError as e:
        return dict(r, state="malformed", detail=str(e))
    owned = set(manifest["files"]) if manifest else set()
    # Preflight every bundle path against the filesystem (review P1): occupied and not ours = conflict.
    try:
        for name in bundle:
            if name not in owned and _lstat_kind(dest / name) != "absent":
                r["conflicts"].append(name)
        if manifest is None:
            return dict(r, state="unmanaged", detail="exists without an agent-boardroom manifest (not managed)")
        r["manifest"] = manifest
        for name, digest in manifest["files"].items():
            k = _lstat_kind(dest / name)
            if k != "file" or _hash_bounded(dest / name, deadline) != digest:
                r["changed"].append(name)
    except _Bound as e:
        return dict(r, state="unreadable", detail=f"could not inspect within bounds ({e})")
    if r["changed"]:
        return dict(r, state="modified", detail="locally changed: " + ", ".join(sorted(r["changed"])))
    up_to_date = (manifest["version"] == __version__ and set(manifest["files"]) == set(bundle)
                  and all(manifest["files"][n] == _sha(b) for n, b in bundle.items()))
    if up_to_date:
        return dict(r, state="current", detail=f"installed by agent-boardroom {__version__}")
    return dict(r, state="outdated", detail=f"installed by agent-boardroom {manifest['version']}; bundle is {__version__}")


def inspect(agent, dest, deadline=None):
    """(state, detail) for doctor and tests; see analyze()."""
    a = analyze(agent, dest, deadline)
    return a["state"], a["detail"]


# ---------------------------------------------------------------- mutation

def _atomic_write(path, data):
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_bundle(dest, bundle):
    """Write bundle files atomically, then the manifest last. Not a multi-file transaction: a failure
    mid-way leaves some new files with the OLD manifest (or none); backups made before a replace
    hold the previous content, and the next run reports the directory as modified or unmanaged."""
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in bundle.items():
        _atomic_write(dest / name, data)
    manifest = {"tool": TOOL, "schema": SCHEMA, "version": __version__,
                "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "files": {n: _sha(b) for n, b in bundle.items()}}
    _atomic_write(dest / MANIFEST, json.dumps(manifest, indent=1).encode())


def _backup(dest, names, deadline=None):
    """Copy the named REGULAR files into a unique .backup-<stamp>-<rand>/ dir; returns its path.
    Callers preflight with _backup_preflight first; a bound hit here is still raised as _Bound and
    classified per target by the caller, and nothing is replaced after a failed backup."""
    bdir = Path(tempfile.mkdtemp(dir=str(dest), prefix=f".backup-{time.strftime('%Y%m%d-%H%M%S')}-"))
    for n in names:
        data = _read_bounded(dest / n, FILE_MAX, deadline)
        if data is not None:
            _atomic_write(bdir / n, data)
    return bdir


def _leaf_symlinks(dest, names):
    return [n for n in names if _lstat_kind(dest / n) in ("symlink", "special")]


def _backup_preflight(dest, names, deadline=None):
    """Every file a backup would read must be a regular file within FILE_MAX, checked no-follow,
    BEFORE any backup dir or write exists. Returns the offending names (empty = safe)."""
    bad = []
    for n in names:
        k = _lstat_kind(dest / n)
        if k == "absent":
            continue
        if k != "file":
            bad.append(f"{n} ({k})")
            continue
        try:
            if os.lstat(dest / n).st_size > FILE_MAX:
                bad.append(f"{n} (larger than {FILE_MAX} bytes)")
        except OSError as e:
            bad.append(f"{n} ({type(e).__name__})")
        if deadline is not None and time.monotonic() > deadline:
            bad.append("(deadline reached during preflight)")
            break
    return bad


def install_one(agent, dest, force=False, adopt=False, dry_run=False, deadline=None):
    """Returns (ok, action, detail). Never raises for a per-target problem."""
    a = analyze(agent, dest, deadline)
    state, bundle = a["state"], a["bundle"]
    tag = "would be " if dry_run else ""
    if state == "symlink":
        return False, "refused", f"{a['detail']}. Remove the symlink to let setup manage this skill."
    if state == "malformed":
        return False, "refused", f"{a['detail']}; fix or remove {dest / MANIFEST} first"
    if state == "unreadable":
        return False, "refused", f"{a['detail']}; nothing was changed. Retry, or raise the bound if the files are legitimately large."
    # Symlinked or special leaves among the paths we'd touch are refused before any backup (P1).
    # "Touch" covers the bundle paths AND, for a managed dir, every manifest path (a retired file
    # that was locally modified is backed up by --force too, so it is preflighted as well).
    if state == "unmanaged":
        touch = [n for n in bundle if n in a["conflicts"]]
    elif a["manifest"]:
        touch = sorted(set(bundle) | set(a["manifest"]["files"]))
    else:
        touch = list(bundle)
    bad = _leaf_symlinks(dest, touch) if state != "missing" else []
    if bad:
        return False, "refused", f"these paths are symlinks or special files, not replaced: {', '.join(bad)}"
    try:
        return _install_mutate(a, dest, bundle, state, touch, force, adopt, dry_run, tag, deadline)
    except _Bound as e:
        return False, "failed", f"stopped before replacing anything: a file could not be read within bounds ({e})"
    except OSError as e:
        return False, "failed", f"{type(e).__name__}: {e}"


def _install_mutate(a, dest, bundle, state, touch, force, adopt, dry_run, tag, deadline):
    if state == "unmanaged":
        if not adopt:
            return False, "skipped", f"{a['detail']}. Re-run with --adopt to let setup take it over (replaced paths are backed up)."
        bad = _backup_preflight(dest, a["conflicts"], deadline)
        if bad:
            return False, "refused", f"cannot back up before replacing: {', '.join(bad)}"
        if not dry_run:
            if a["conflicts"]:
                _backup(dest, a["conflicts"], deadline)
            _write_bundle(dest, bundle)
        return True, f"{tag}adopted", f"backed up and replaced: {', '.join(sorted(a['conflicts'])) or 'nothing (no overlapping files)'}"
    # Managed from here. New bundle paths that collide with untracked user files are conflicts (P1).
    if a["conflicts"]:
        return False, "refused", (f"the new bundle would overwrite files setup doesn't own: {', '.join(sorted(a['conflicts']))}. "
                                  "Move them aside, then re-run.")
    if state == "current":
        return True, "current", a["detail"]
    if state == "modified":
        if not force:
            return False, "refused", f"{a['detail']}. Re-run with --force to replace those files (a backup is kept)."
        bad = _backup_preflight(dest, a["changed"], deadline)
        if bad:
            return False, "refused", f"cannot back up before replacing: {', '.join(bad)}"
        if not dry_run:
            bdir = _backup(dest, a["changed"], deadline)
            _write_bundle(dest, bundle)
            return True, "replaced", f"backed up to {bdir.name}/ and replaced: {', '.join(sorted(a['changed']))}"
        return True, "would be replaced", f"would back up and replace: {', '.join(sorted(a['changed']))}"
    # missing, or outdated-but-unchanged: an ordinary install/update, no force needed.
    retired = sorted(set(a["manifest"]["files"]) - set(bundle)) if a["manifest"] else []
    if not dry_run:
        _write_bundle(dest, bundle)
    note = f"; retired file(s) left in place, no longer managed: {', '.join(retired)}" if retired else ""
    return True, f"{tag}{'installed' if state == 'missing' else 'updated'}", f"{len(bundle)} file(s) -> {dest}{note}"


def uninstall_one(agent, dest, dry_run=False, deadline=None):
    a = analyze(agent, dest, deadline)
    state = a["state"]
    tag = "would be " if dry_run else ""
    if state == "missing":
        return True, "absent", "nothing to remove"
    if state in ("symlink", "unmanaged", "malformed", "unreadable"):
        return False, "left", f"{a['detail']}; not removed (not managed by setup)"
    if state == "modified":
        return False, "left", f"{a['detail']}; not removed. Delete it by hand if you want it gone."
    manifest = a["manifest"]
    if not dry_run:
        for name in manifest["files"]:
            if _lstat_kind(dest / name) == "file":
                (dest / name).unlink()
        (dest / MANIFEST).unlink()
        leftover = sorted(p.name for p in dest.iterdir())
        if leftover:
            ours = [n for n in leftover if n.startswith(".backup-")]
            note = (" (backups setup made for you; delete them when you no longer need them)"
                    if ours and len(ours) == len(leftover) else "")
            return True, "removed", f"managed files removed; left {dest} because it still holds: {', '.join(leftover)}{note}"
        dest.rmdir()
    return True, f"{tag}removed", f"{len(manifest['files'])} file(s) and the manifest"


# ---------------------------------------------------------------- CLI

def add_parser(sub):
    sp = sub.add_parser("setup", help="install the bundled agent skills, then run doctor")
    sp.add_argument("--claude", action="store_true", help="select the Claude Code skill (creates its dir)")
    sp.add_argument("--codex", action="store_true", help="select the Codex skill (creates its dir)")
    sp.add_argument("--dry-run", action="store_true", help="print what would change; create nothing")
    sp.add_argument("--force", action="store_true", help="replace locally changed managed files (backed up)")
    sp.add_argument("--adopt", action="store_true", help="take over an existing unmanaged skill dir (backed up)")
    sp.add_argument("--uninstall", action="store_true", help="remove skills setup installed and left unchanged")
    sp.add_argument("--no-doctor", action="store_true", help="skip the diagnostics after setup")
    sp.add_argument("--json", action="store_true")
    return sp


def select_targets(a, dests):
    """Explicit flags select exactly those targets. Otherwise autodetect by the agent home's EXISTENCE
    (an env var alone is not an installation). Existence is a convenience heuristic, not proof the
    agent is usable. Uninstall inspects selected destinations even if the home is gone."""
    explicit = [n for n in BUNDLED if getattr(a, n)]
    if explicit:
        return explicit, []
    chosen, notes = [], []
    for n in BUNDLED:
        if a.uninstall or dests[n]["home"].is_dir():
            chosen.append(n)
        else:
            notes.append(f"{n}: skipped, {dests[n]['home']} does not exist (pass --{n} to install anyway)")
    return chosen, notes


def cmd_setup(a, dests=None, run_doctor=None):
    dests = dests or destinations()
    targets, notes = select_targets(a, dests)
    deadline = time.monotonic() + 30
    # Preflight ALL selected targets read-only before writing any (agreed plan). This is not a
    # cross-directory transaction: a later target's I/O failure is still reported per target.
    plan, results, ok_all = [], [], True
    for n in targets:
        dest = dests[n]["dest"]
        try:
            fn = uninstall_one if a.uninstall else install_one
            kw = {} if a.uninstall else {"force": a.force, "adopt": a.adopt}
            ok, action, detail = fn(n, dest, dry_run=True, deadline=deadline, **kw)
        except (BoardroomError, OSError) as e:
            ok, action, detail = False, "failed", f"{type(e).__name__}: {e}"
        plan.append((n, dest, ok, action, detail))
    for n, dest, ok, action, detail in plan:
        if ok and not a.dry_run and not action.startswith(("current", "absent")):
            try:
                fn = uninstall_one if a.uninstall else install_one
                kw = {} if a.uninstall else {"force": a.force, "adopt": a.adopt}
                ok, action, detail = fn(n, dest, dry_run=False, deadline=deadline, **kw)
            except (BoardroomError, OSError) as e:
                ok, action, detail = False, "failed", f"{type(e).__name__}: {e}"
        ok_all &= ok
        results.append({"agent": n, "dest": str(dest), "ok": ok, "action": action, "detail": detail})
    if a.json:
        print(json.dumps({"results": results, "notes": notes, "dry_run": a.dry_run}, ensure_ascii=True, indent=1))
    else:
        for r in results:
            print(safe_text(f"[{'ok' if r['ok'] else 'FAIL'}] {r['agent']} skill: {r['action']} ({r['detail']})", one_line=True))
        for note in notes:
            print(safe_text(f"[--] {note}", one_line=True))
        if not targets:
            print("[--] no targets selected; pass --claude and/or --codex")
        if a.dry_run:
            print("[--] dry run: nothing was created, written or removed")
    if not a.uninstall and not a.dry_run and not a.no_doctor and not a.json:
        print("\ndiagnostics (do not affect setup's result):")
        (run_doctor or _doctor_default)()
    if not targets:
        sys.exit(1)
    sys.exit(0 if ok_all else 1)


def _doctor_default():
    from .cli import cmd_doctor
    try:
        cmd_doctor(argparse.Namespace(timeout=10.0, json=False))
    except SystemExit:
        pass  # doctor's exit status is reported in its own output, never as setup's
