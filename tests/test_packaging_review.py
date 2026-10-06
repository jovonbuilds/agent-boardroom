"""Independent setup regressions: preserve user files across malformed metadata and upgrades.

All filesystem changes are confined to temporary directories. These tests exercise the ownership
contract, including counterexamples not covered by the initial setup lifecycle tests.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_boardroom import setup


class PackagingOwnershipReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dest = self.root / "skill"

    def test_empty_marker_cannot_authorize_overwriting_unmanaged_skill(self):
        self.dest.mkdir()
        skill = self.dest / "SKILL.md"
        skill.write_bytes(b"user-authored skill")
        (self.dest / setup.MANIFEST).write_text('{"files": {}}')
        ok, _, _ = setup.install_one("codex", self.dest)
        self.assertFalse(ok)
        self.assertEqual(skill.read_bytes(), b"user-authored skill")

    def test_foreign_marker_cannot_authorize_deleting_user_file(self):
        self.dest.mkdir()
        note = self.dest / "notes.txt"
        note.write_bytes(b"user notes")
        (self.dest / setup.MANIFEST).write_text(json.dumps({
            "tool": "other-tool", "version": "1.0", "installed_at": "2026-01-01T00:00:00Z",
            "files": {"notes.txt": setup._sha(note.read_bytes())},
        }))
        ok, _, _ = setup.uninstall_one("codex", self.dest)
        self.assertFalse(ok)
        self.assertEqual(note.read_bytes(), b"user notes")

    def test_new_bundle_file_cannot_overwrite_preexisting_unowned_file(self):
        self.assertTrue(setup.install_one("codex", self.dest)[0])
        note = self.dest / "notes.txt"
        note.write_bytes(b"user notes, never installed by setup")
        updated = dict(setup.bundle_files("codex"), **{"notes.txt": b"new packaged reference"})
        with mock.patch.object(setup, "bundle_files", return_value=updated):
            ok, _, _ = setup.install_one("codex", self.dest)
        self.assertFalse(ok)
        self.assertEqual(note.read_bytes(), b"user notes, never installed by setup")

    def test_force_does_not_follow_managed_leaf_symlink(self):
        setup.install_one("codex", self.dest)
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside content")
        skill = self.dest / "SKILL.md"
        skill.unlink()
        skill.symlink_to(outside)
        ok, _, _ = setup.install_one("codex", self.dest, force=True)
        self.assertFalse(ok)
        self.assertTrue(skill.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside content")
        self.assertFalse(list(self.dest.glob(".backup-*")))

    def test_adopt_does_not_follow_unmanaged_leaf_symlink(self):
        self.dest.mkdir()
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside content")
        skill = self.dest / "SKILL.md"
        skill.symlink_to(outside)
        ok, _, _ = setup.install_one("codex", self.dest, adopt=True)
        self.assertFalse(ok)
        self.assertTrue(skill.is_symlink())
        self.assertFalse(list(self.dest.glob(".backup-*")))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires Unix FIFO")
    def test_nonregular_manifest_cannot_block_inspection(self):
        self.dest.mkdir()
        os.mkfifo(self.dest / setup.MANIFEST)
        # A regular-file check must reject a FIFO before opening it. The timeout is only a
        # test containment bound; no writer is ever attached, so an ordinary open hangs.
        script = (
            "import sys\nfrom pathlib import Path\n"
            "from agent_boardroom import setup\n"
            "state, detail = setup.inspect('codex', Path(sys.argv[1]))\n"
            "assert state == 'malformed', (state, detail)\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.dest)],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=2,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deep_json_is_classified_and_refused_without_raising(self):
        self.dest.mkdir()
        skill = self.dest / "SKILL.md"
        skill.write_bytes(b"user skill")
        (self.dest / setup.MANIFEST).write_text("[" * 2000 + "0" + "]" * 2000)
        self.assertEqual(setup.inspect("codex", self.dest)[0], "malformed")
        for kwargs in ({}, {"force": True}, {"adopt": True}):
            self.assertFalse(setup.install_one("codex", self.dest, **kwargs)[0])
        self.assertFalse(setup.uninstall_one("codex", self.dest)[0])
        self.assertEqual(skill.read_bytes(), b"user skill")

    def test_newline_name_in_manifest_is_not_ownership(self):
        setup.install_one("codex", self.dest)
        marker = self.dest / setup.MANIFEST
        m = json.loads(marker.read_text())
        note = self.dest / "notes.txt\n"
        note.write_bytes(b"user notes")
        m["files"][note.name] = setup._sha(note.read_bytes())
        marker.write_text(json.dumps(m))
        self.assertEqual(setup.inspect("codex", self.dest)[0], "malformed")
        self.assertFalse(setup.uninstall_one("codex", self.dest)[0])
        self.assertEqual(note.read_bytes(), b"user notes")

    def test_adoption_of_directory_leaf_is_refused_before_backup(self):
        self.dest.mkdir()
        leaf = self.dest / "SKILL.md"
        leaf.mkdir()
        (leaf / "notes.txt").write_bytes(b"user notes")
        for dry in (True, False):
            ok, _, _ = setup.install_one("codex", self.dest, adopt=True, dry_run=dry)
            self.assertFalse(ok)
        self.assertEqual((leaf / "notes.txt").read_bytes(), b"user notes")
        self.assertFalse(list(self.dest.glob(".backup-*")))

    def test_adoption_of_oversized_file_is_refused_before_backup(self):
        self.dest.mkdir()
        leaf = self.dest / "SKILL.md"
        leaf.write_bytes(b"user skill over the configured read bound")
        # This bounds the existing file being preserved, not the trusted package resource.
        with mock.patch.object(setup, "FILE_MAX", 8):
            for dry in (True, False):
                ok, _, _ = setup.install_one("codex", self.dest, adopt=True, dry_run=dry)
                self.assertFalse(ok)
        self.assertEqual(leaf.read_bytes(), b"user skill over the configured read bound")
        self.assertFalse(list(self.dest.glob(".backup-*")))


if __name__ == "__main__":
    unittest.main()
