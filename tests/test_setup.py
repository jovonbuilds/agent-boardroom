"""`agent-boardroom setup`: ownership manifest, upgrade without --force, refusals, uninstall, dry-run.

Everything runs against temporary destinations injected through `dests`; no real agent home is read
or written.
"""
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_boardroom import __version__, setup  # noqa: E402


def ns(**kw):
    base = dict(claude=False, codex=False, dry_run=False, force=False, adopt=False,
                uninstall=False, no_doctor=True, json=True)
    base.update(kw)
    return argparse.Namespace(**base)


class SetupHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.homes = {"claude": root / "claude-home", "codex": root / "codex-home"}
        self.dests = {n: {"home": self.homes[n], "dest": root / f"{n}-skills" / "agent-boardroom"} for n in self.homes}

    def run_setup(self, **kw):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            setup.cmd_setup(ns(**kw), dests=self.dests, run_doctor=lambda: None)
        return ctx.exception.code, json.loads(out.getvalue())

    def files(self, agent):
        d = self.dests[agent]["dest"]
        return sorted(p.name for p in d.iterdir()) if d.exists() else None


class TargetSelection(SetupHarness):
    def test_autodetect_requires_the_agent_home_to_exist(self):
        self.homes["claude"].mkdir()
        code, r = self.run_setup()
        self.assertEqual(code, 0)
        self.assertEqual([x["agent"] for x in r["results"]], ["claude"])
        self.assertTrue(any("codex: skipped" in n for n in r["notes"]))
        self.assertIsNone(self.files("codex"))

    def test_explicit_flags_override_autodetect_and_create_dirs(self):
        code, r = self.run_setup(codex=True)  # no homes exist at all
        self.assertEqual(code, 0)
        self.assertEqual([x["agent"] for x in r["results"]], ["codex"])
        self.assertIn("SKILL.md", self.files("codex"))
        self.assertIn(setup.MANIFEST, self.files("codex"))

    def test_no_targets_is_an_error(self):
        code, r = self.run_setup()
        self.assertEqual(code, 1)
        self.assertEqual(r["results"], [])


class Lifecycle(SetupHarness):
    def test_install_current_update_uninstall(self):
        code, r = self.run_setup(claude=True)
        self.assertEqual(code, 0)
        self.assertEqual(r["results"][0]["action"], "installed")
        self.assertEqual(setup.inspect("claude", self.dests["claude"]["dest"])[0], "current")

        code, r = self.run_setup(claude=True)
        self.assertEqual(r["results"][0]["action"], "current")

        # an older managed install: untouched files, older bundle -> plain update, no --force
        m = setup.read_manifest(self.dests["claude"]["dest"])
        skill = self.dests["claude"]["dest"] / "SKILL.md"
        skill.write_bytes(b"old bundle v0\n")
        m["version"] = "0.0.1"
        m["files"]["SKILL.md"] = setup._sha(b"old bundle v0\n")
        (self.dests["claude"]["dest"] / setup.MANIFEST).write_text(json.dumps(m))
        self.assertEqual(setup.inspect("claude", self.dests["claude"]["dest"])[0], "outdated")
        code, r = self.run_setup(claude=True)
        self.assertEqual((code, r["results"][0]["action"]), (0, "updated"))
        self.assertEqual(setup.inspect("claude", self.dests["claude"]["dest"])[0], "current")

        code, r = self.run_setup(claude=True, uninstall=True)
        self.assertEqual((code, r["results"][0]["action"]), (0, "removed"))
        self.assertIsNone(self.files("claude"))

    def test_modified_refuses_then_force_backs_up(self):
        self.run_setup(claude=True)
        skill = self.dests["claude"]["dest"] / "SKILL.md"
        skill.write_text("my local edits\n")
        code, r = self.run_setup(claude=True)
        self.assertEqual((code, r["results"][0]["action"]), (1, "refused"))
        self.assertIn("SKILL.md", r["results"][0]["detail"])
        self.assertEqual(skill.read_text(), "my local edits\n")  # untouched
        code, r = self.run_setup(claude=True, force=True)
        self.assertEqual((code, r["results"][0]["action"]), (0, "replaced"))
        backups = [p for p in self.dests["claude"]["dest"].iterdir() if p.name.startswith(".backup-")]
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "SKILL.md").read_text(), "my local edits\n")
        self.assertNotEqual(skill.read_text(), "my local edits\n")

    def test_modified_is_never_uninstalled(self):
        self.run_setup(claude=True)
        (self.dests["claude"]["dest"] / "SKILL.md").write_text("edited\n")
        code, r = self.run_setup(claude=True, uninstall=True)
        self.assertEqual((code, r["results"][0]["action"]), (1, "left"))
        self.assertEqual((self.dests["claude"]["dest"] / "SKILL.md").read_text(), "edited\n")

    def test_unmanaged_is_left_alone_even_if_identical_unless_adopted(self):
        dest = self.dests["claude"]["dest"]
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_bytes(setup.bundle_files("claude")["SKILL.md"])  # identical, no manifest
        code, r = self.run_setup(claude=True)
        self.assertEqual((code, r["results"][0]["action"]), (1, "skipped"))
        self.assertEqual(self.files("claude"), ["SKILL.md"])
        code, r = self.run_setup(claude=True, uninstall=True)
        self.assertEqual((code, r["results"][0]["action"]), (1, "left"))
        code, r = self.run_setup(claude=True, adopt=True)
        self.assertEqual((code, r["results"][0]["action"]), (0, "adopted"))
        self.assertEqual(setup.inspect("claude", dest)[0], "current")

    def test_extra_files_survive_uninstall(self):
        self.run_setup(claude=True)
        extra = self.dests["claude"]["dest"] / "notes.txt"
        extra.write_text("mine\n")
        code, r = self.run_setup(claude=True, uninstall=True)
        self.assertEqual(code, 0)
        self.assertTrue(extra.exists())
        self.assertFalse((self.dests["claude"]["dest"] / setup.MANIFEST).exists())
        self.assertIn("still holds", r["results"][0]["detail"])

    def test_symlink_destination_is_reported_never_followed(self):
        real = Path(self.tmp.name) / "real-skill"
        real.mkdir()
        (real / "SKILL.md").write_text("manual install\n")
        dest = self.dests["claude"]["dest"]
        dest.parent.mkdir(parents=True)
        dest.symlink_to(real)
        for kw in ({}, {"force": True}, {"adopt": True}, {"uninstall": True}):
            code, r = self.run_setup(claude=True, **kw)
            self.assertEqual(code, 1, kw)
            self.assertIn("symlink", r["results"][0]["detail"])
        self.assertEqual((real / "SKILL.md").read_text(), "manual install\n")
        self.assertTrue(dest.is_symlink())

    def test_malformed_manifest_is_refused_not_swept(self):
        dest = self.dests["claude"]["dest"]
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("x\n")
        (dest / setup.MANIFEST).write_text("{not json")
        for kw in ({}, {"force": True}, {"uninstall": True}):
            code, r = self.run_setup(claude=True, **kw)
            self.assertEqual(code, 1, kw)
        self.assertEqual((dest / "SKILL.md").read_text(), "x\n")
        (dest / setup.MANIFEST).write_text(json.dumps({"files": {"../evil": "00"}}))  # path escape
        self.assertEqual(setup.inspect("claude", dest)[0], "malformed")

    def test_dry_run_creates_nothing(self):
        code, r = self.run_setup(claude=True, dry_run=True)
        self.assertEqual(code, 0)
        self.assertTrue(r["results"][0]["action"].startswith("would "))
        self.assertIsNone(self.files("claude"))
        self.assertFalse(self.dests["claude"]["dest"].parent.exists())

    def test_partial_write_never_claims_unwritten_files(self):
        dest = self.dests["claude"]["dest"]
        real_write = setup._atomic_write
        calls = {"n": 0}

        def flaky(path, data):
            calls["n"] += 1
            if path.name == setup.MANIFEST:
                raise OSError("disk full")
            real_write(path, data)
        with mock.patch.object(setup, "_atomic_write", flaky):
            code, r = self.run_setup(claude=True)
        self.assertEqual((code, r["results"][0]["action"]), (1, "failed"))
        self.assertEqual(setup.inspect("claude", dest)[0], "unmanaged")  # files exist, no manifest: safe state

    def test_bundle_is_read_from_package_resources(self):
        for agent in ("claude", "codex"):
            b = setup.bundle_files(agent)
            self.assertIn("SKILL.md", b)
            self.assertIn(b"name: agent-boardroom", b["SKILL.md"])
            self.assertIn(__version__.encode()[:1], b"0123456789")  # version string is sane


if __name__ == "__main__":
    unittest.main()


class OwnershipHardening(SetupHarness):
    """Review round 2: strict manifest schema, filesystem preflight, no-follow, bounds, backups."""

    def dest(self):
        return self.dests["claude"]["dest"]

    def manifest_for(self, files, **override):
        m = {"tool": setup.TOOL, "schema": setup.SCHEMA, "version": __version__,
             "installed_at": "2026-01-01T00:00:00Z", "files": files}
        m.update(override)
        return m

    def test_partial_or_foreign_manifests_never_confer_ownership(self):
        d = self.dest()
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(b"user content")
        sha = setup._sha(b"user content")
        bad = [
            {"files": {}},
            {"files": {"SKILL.md": sha}},
            self.manifest_for({"SKILL.md": sha}, tool="other-tool"),
            self.manifest_for({"SKILL.md": sha}, schema=99),
            self.manifest_for({}),
            self.manifest_for({"SKILL.md": "nothex"}),
            self.manifest_for({"../evil": sha}),
            self.manifest_for({setup.MANIFEST: sha}),
            self.manifest_for({".backup-x": sha}),
            self.manifest_for({"SKILL.md": sha}, version="x" * 100),
            self.manifest_for({"SKILL.md": sha}, installed_at=123),
            self.manifest_for({f"f{i}": sha for i in range(setup.FILES_MAX + 1)}),
        ]
        for m in bad:
            (d / setup.MANIFEST).write_text(json.dumps(m))
            self.assertEqual(setup.inspect("claude", d)[0], "malformed", m)
            for kw in ({}, {"force": True}, {"adopt": True}):
                ok, action, _ = setup.install_one("claude", d, **kw)
                self.assertFalse(ok, (m, kw))
            ok, _, _ = setup.uninstall_one("claude", d)
            self.assertFalse(ok, m)
            self.assertEqual((d / "SKILL.md").read_bytes(), b"user content", m)

    def test_valid_schema_is_required_exactly(self):
        d = self.dest()
        setup.install_one("claude", d)
        m = setup.read_manifest(d)
        self.assertEqual((m["tool"], m["schema"]), (setup.TOOL, setup.SCHEMA))
        self.assertTrue(all(setup._SHA_RE.match(v) for v in m["files"].values()))

    def test_managed_update_refuses_new_bundle_path_that_is_occupied(self):
        d = self.dest()
        setup.install_one("claude", d)
        (d / "extra.md").write_bytes(b"mine")
        updated = dict(setup.bundle_files("claude"), **{"extra.md": b"packaged"})
        with mock.patch.object(setup, "bundle_files", return_value=updated):
            ok, action, detail = setup.install_one("claude", d)
            self.assertEqual((ok, action), (False, "refused"))
            self.assertIn("extra.md", detail)
            ok, _, _ = setup.install_one("claude", d, force=True)  # force is for OUR changed files only
            self.assertFalse(ok)
        self.assertEqual((d / "extra.md").read_bytes(), b"mine")

    def test_retired_bundle_paths_are_left_and_reported(self):
        d = self.dest()
        old = dict(setup.bundle_files("claude"), **{"old.md": b"retired soon"})
        with mock.patch.object(setup, "bundle_files", return_value=old):
            setup.install_one("claude", d)
        ok, action, detail = setup.install_one("claude", d)  # current bundle lacks old.md
        self.assertEqual((ok, action), (True, "updated"))
        self.assertIn("retired", detail)
        self.assertTrue((d / "old.md").exists())
        self.assertEqual(setup.inspect("claude", d)[0], "current")

    def test_adopt_backs_up_only_replaced_paths(self):
        d = self.dest()
        d.mkdir(parents=True)
        (d / "SKILL.md").write_bytes(b"old skill")
        (d / "notes.txt").write_bytes(b"notes")
        ok, action, _ = setup.install_one("claude", d, adopt=True)
        self.assertEqual((ok, action), (True, "adopted"))
        (bdir,) = [p for p in d.iterdir() if p.name.startswith(".backup-")]
        self.assertEqual(sorted(p.name for p in bdir.iterdir()), ["SKILL.md"])
        self.assertEqual((d / "notes.txt").read_bytes(), b"notes")

    def test_dangling_leaf_symlink_is_refused_everywhere(self):
        d = self.dest()
        setup.install_one("claude", d)
        (d / "SKILL.md").unlink()
        (d / "SKILL.md").symlink_to(d / "does-not-exist")
        self.assertEqual(setup.inspect("claude", d)[0], "modified")
        for kw in ({}, {"force": True}):
            ok, _, detail = setup.install_one("claude", d, **kw)
            self.assertFalse(ok)
        ok, _, _ = setup.uninstall_one("claude", d)
        self.assertFalse(ok)
        self.assertTrue((d / "SKILL.md").is_symlink())
        self.assertFalse(list(d.glob(".backup-*")))

    def test_backups_are_unique_within_one_second(self):
        d = self.dest()
        setup.install_one("claude", d)
        for _ in range(2):
            (d / "SKILL.md").write_bytes(b"edit\n")
            ok, action, _ = setup.install_one("claude", d, force=True)
            self.assertEqual((ok, action), (True, "replaced"))
        self.assertEqual(len(list(d.glob(".backup-*"))), 2)

    def test_bounds_and_deadline_classify_not_raise(self):
        d = self.dest()
        setup.install_one("claude", d)
        with mock.patch.object(setup, "FILE_MAX", 10):
            self.assertEqual(setup.inspect("claude", d)[0], "unreadable")
        import time as _t
        self.assertEqual(setup.inspect("claude", d, deadline=_t.monotonic() - 1)[0], "unreadable")
        (d / setup.MANIFEST).write_bytes(b"{" + b" " * (setup.MANIFEST_MAX + 10) + b"}")
        self.assertEqual(setup.inspect("claude", d)[0], "malformed")
        ok, _, _ = setup.install_one("claude", d)
        self.assertFalse(ok)

    def test_special_manifest_does_not_block(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no mkfifo")
        d = self.dest()
        d.mkdir(parents=True)
        os.mkfifo(d / setup.MANIFEST)
        self.assertEqual(setup.inspect("claude", d)[0], "malformed")  # returns promptly
        ok, _, _ = setup.install_one("claude", d)
        self.assertFalse(ok)

    def test_preflight_runs_before_any_write(self):
        # Preflight runs for every target before any write, but results stay per target (no
        # cross-directory transaction): codex is refused, claude is still installed and reported.
        real = Path(self.tmp.name) / "real"
        real.mkdir()
        cd = self.dests["codex"]["dest"]
        cd.parent.mkdir(parents=True)
        cd.symlink_to(real)
        code, r = self.run_setup(claude=True, codex=True)
        self.assertEqual(code, 1)
        actions = {x["agent"]: x["action"] for x in r["results"]}
        self.assertEqual(actions["codex"], "refused")
        self.assertEqual(actions["claude"], "installed")  # per-target honesty: claude still installed
        self.assertIsNotNone(self.files("claude"))


class OwnershipHardeningRound3(SetupHarness):
    def dest(self):
        return self.dests["claude"]["dest"]

    def test_schema_scalar_typing_is_exact(self):
        d = self.dest()
        setup.install_one("claude", d)
        m = setup.read_manifest(d)
        for bad in (True, 1.0, "1"):
            m2 = dict(m, schema=bad)
            (d / setup.MANIFEST).write_text(json.dumps(m2))
            self.assertEqual(setup.inspect("claude", d)[0], "malformed", bad)
        for bad in ("0.4.0\n", "0.4.0 "):
            m2 = dict(m, version=bad)
            (d / setup.MANIFEST).write_text(json.dumps(m2))
            self.assertEqual(setup.inspect("claude", d)[0], "malformed", repr(bad))
        m2 = dict(m, files=dict(m["files"], **{"SKILL.md": m["files"]["SKILL.md"] + "\n"}))
        (d / setup.MANIFEST).write_text(json.dumps(m2))
        self.assertEqual(setup.inspect("claude", d)[0], "malformed")

    def test_force_preflights_retired_modified_paths(self):
        d = self.dest()
        old = dict(setup.bundle_files("claude"), **{"old.md": b"retired"})
        with mock.patch.object(setup, "bundle_files", return_value=old):
            setup.install_one("claude", d)
        (d / "old.md").unlink()
        (d / "old.md").symlink_to(Path(self.tmp.name) / "elsewhere")  # retired AND now a symlink
        ok, _, detail = setup.install_one("claude", d, force=True)
        self.assertFalse(ok)
        self.assertIn("old.md", detail)
        self.assertFalse(list(d.glob(".backup-*")))

    def test_late_bound_during_backup_is_classified_and_replaces_nothing(self):
        d = self.dest()
        setup.install_one("claude", d)
        (d / "SKILL.md").write_bytes(b"edited\n")
        # The bound must fire inside the BACKUP step (inspection passed), to exercise the late path.
        with mock.patch.object(setup, "_backup", side_effect=setup._Bound("deadline")):
            ok, action, detail = setup.install_one("claude", d, force=True)
        self.assertEqual((ok, action), (False, "failed"))
        self.assertIn("before replacing anything", detail)
        self.assertEqual((d / "SKILL.md").read_bytes(), b"edited\n")

    def test_unreadable_wording_never_says_fix_or_remove(self):
        d = self.dest()
        setup.install_one("claude", d)
        with mock.patch.object(setup, "FILE_MAX", 1):
            ok, action, detail = setup.install_one("claude", d)
        self.assertEqual((ok, action), (False, "refused"))
        self.assertNotIn("fix or remove", detail)
        self.assertIn("Retry", detail)
