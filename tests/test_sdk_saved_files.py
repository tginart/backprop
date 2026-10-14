"""Saved-file links stay live while uploaded versions stay immutable.

Architecture: create actual local links -> edit either returned or source path
-> trigger normal flush/finish -> inspect verified uploaded snapshots. Fault
injection covers unavailable links, explicit policy downgrade and path escape.
Main callers: unittest discovery; pinned filesystem.py:499-570 defines the
symlink/hardlink/copy strategy and Run.save documents the returned local paths.
"""
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk"))
import backprop


class SavedFileLinkContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = backprop.MemorySink()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.run = backprop.Run(project="saved-files", id="links", dir=self.root,
            settings={"silent": True, "console": "off", "disable_git": True, "flush_interval": 3600},
            _sink=self.store, _deployment={})

    def tearDown(self):
        self.run.finish()
        self.environment.stop()
        self.temp.cleanup()

    def source(self, name="weights.bin", content=b"first"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def records(self, name="weights.bin"):
        return [event["payload"] for event in self.store.events if event["type"] == "file" and event["payload"]["name"] == name]

    def test_returned_live_symlink_edits_and_source_edits_both_upload(self):
        source = self.source()
        destination = Path(self.run.save(source, base_path=self.root, policy="live")[0])
        self.assertTrue(destination.is_symlink())
        destination.write_bytes(b"via returned path")
        self.assertEqual(source.read_bytes(), b"via returned path")
        self.run.flush()
        source.write_bytes(b"via source path")
        self.run.flush()
        self.assertEqual([self.store.download_file(record["file"]) for record in self.records()],
                         [b"first", b"via returned path", b"via source path"])

    def test_symlink_false_uses_hardlink_and_retains_inode_after_source_replacement(self):
        self.run._settings.symlink = False
        source = self.source()
        destination = Path(self.run.save(source, base_path=self.root)[0])
        self.assertFalse(destination.is_symlink())
        self.assertTrue(source.samefile(destination))
        destination.write_bytes(b"linked edit")
        self.run.flush()
        self.assertEqual(self.store.download_file(self.records()[-1]["file"]), b"linked edit")
        replacement = self.source("replacement", b"new inode")
        replacement.replace(source)
        self.run.flush()
        self.assertEqual(destination.read_bytes(), b"linked edit")
        self.assertEqual(len(self.records()), 2)
        # A subsequent save relinks to the new source, matching pinned behavior.
        self.run.save(source, base_path=self.root)
        self.run.flush()
        self.assertTrue(source.samefile(destination))
        self.assertEqual(self.store.download_file(self.records()[-1]["file"]), b"new inode")

    def test_failed_symlink_falls_back_to_hardlink(self):
        source = self.source()
        with patch.object(Path, "symlink_to", side_effect=OSError(errno.EPERM, "not permitted")):
            destination = Path(self.run.save(source, base_path=self.root)[0])
        self.assertFalse(destination.is_symlink())
        self.assertTrue(source.samefile(destination))
        destination.write_bytes(b"hardlink edit")
        self.run.flush()
        self.assertEqual(self.store.download_file(self.records()[-1]["file"]), b"hardlink edit")

    def test_copy_fallback_warns_and_downgrades_live_to_now(self):
        source = self.source()
        with patch.object(Path, "symlink_to", side_effect=OSError("no symlinks")), \
             patch("backprop.file_logging.os.link", side_effect=OSError(errno.EXDEV, "different device")), \
             patch.object(self.run, "_warn") as warning:
            destination = Path(self.run.save(source, base_path=self.root, policy="live")[0])
        warning.assert_called_once()
        self.assertIn("policy='now'", warning.call_args.args[0])
        self.assertFalse(source.samefile(destination))
        self.assertEqual(self.run._files.tracked["weights.bin"]["policy"], "now")
        destination.write_bytes(b"untracked later copy edit")
        self.run.flush()
        self.assertEqual([self.store.download_file(record["file"]) for record in self.records()], [b"first"])

    def test_end_policy_snapshots_returned_path_only_at_finish(self):
        source = self.source()
        destination = Path(self.run.save(source, base_path=self.root, policy="end")[0])
        destination.write_bytes(b"final returned edit")
        self.run.flush()
        self.assertEqual(self.records(), [])
        self.run.finish()
        self.assertEqual([self.store.download_file(record["file"]) for record in self.records()], [b"final returned edit"])

    def test_end_copy_fallback_observes_returned_copy_and_does_not_recopy_source(self):
        source = self.source()
        with patch.object(Path, "symlink_to", side_effect=OSError("no symlinks")), \
             patch("backprop.file_logging.os.link", side_effect=OSError(errno.EXDEV, "different device")):
            destination = Path(self.run.save(source, base_path=self.root, policy="end")[0])
        source.write_bytes(b"source replacement")
        destination.write_bytes(b"final copy edit")
        self.run.finish()
        self.assertEqual(self.store.download_file(self.records()[-1]["file"]), b"final copy edit")

    def test_repeat_save_deduplicates_unchanged_bytes_and_glob_is_fixed(self):
        self.source("batch/one.txt")
        pattern = str(self.root / "batch/*.txt")
        self.run.save(pattern, base_path=self.root)
        self.run.save(pattern, base_path=self.root)
        self.source("batch/two.txt", b"not registered")
        self.run.flush()
        self.assertEqual(len(self.records("batch/one.txt")), 1)
        self.assertEqual(self.records("batch/two.txt"), [])

    def test_file_already_in_run_directory_is_not_unlinked(self):
        path = Path(self.run.dir) / "weights.bin"
        path.write_bytes(b"inside run")
        outputs = self.run.save(path, base_path=self.run.dir)
        self.assertEqual(outputs, [str(path)])
        path.write_bytes(b"inside changed")
        self.run.flush()
        self.assertEqual(self.store.download_file(self.records()[-1]["file"]), b"inside changed")

    def test_preexisting_parent_symlink_escape_is_rejected_before_writing(self):
        source = self.source("sub/file.bin")
        outside = self.root / "outside"
        outside.mkdir()
        (Path(self.run.dir) / "sub").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(backprop.UsageError, "parent escapes"):
            self.run.save(source, base_path=self.root)
        self.assertEqual(list(outside.iterdir()), [])

    def test_preexisting_hardlink_is_returned_and_updated_after_source_unlink(self):
        self.run._settings.symlink = False
        source = self.source()
        destination = Path(self.run.save(source, base_path=self.root)[0])
        source.unlink()
        destination.write_bytes(b"surviving hardlink")
        self.assertEqual(self.run.save(source, base_path=self.root, glob=False), [str(destination)])
        self.run.flush()
        self.assertEqual(self.store.download_file(self.records()[-1]["file"]), b"surviving hardlink")

    def test_glob_combines_existing_saved_files_and_new_source_files_once(self):
        self.run._settings.symlink = False
        source = self.source("batch/one.txt")
        first = self.run.save(str(self.root / "batch/*.txt"), base_path=self.root)[0]
        source.unlink()
        self.source("batch/two.txt", b"second")
        outputs = self.run.save(str(self.root / "batch/*.txt"), base_path=self.root)
        self.assertEqual(outputs, [first, str(Path(self.run.dir) / "batch/two.txt")])
        self.run.flush()
        self.assertEqual(len(self.records("batch/one.txt")), 1)
        self.assertEqual(len(self.records("batch/two.txt")), 1)

    def test_broken_preexisting_link_errors_before_materializing_other_matches(self):
        source = self.source("batch/gone.txt")
        destination = Path(self.run.save(source, base_path=self.root)[0])
        source.unlink()
        self.source("batch/new.txt", b"new")
        with self.assertRaisesRegex(FileNotFoundError, "broken symlink"):
            self.run.save(str(self.root / "batch/*.txt"), base_path=self.root)
        self.assertFalse((Path(self.run.dir) / "batch/new.txt").exists())
        # Broken tracked live files fail explicitly at flush too; remove the
        # test registration so teardown can close normally after verification.
        with self.assertRaisesRegex(FileNotFoundError, "disappeared"):
            self.run.flush()
        del self.run._files.tracked["batch/gone.txt"]
        self.assertTrue(destination.is_symlink())

    def test_preexisting_glob_parent_link_cannot_escape_the_run_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "file.bin").write_bytes(b"private")
        (Path(self.run.dir) / "sub").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(backprop.UsageError, "parent escapes"):
            self.run.save(str(self.root / "sub/*.bin"), base_path=self.root)
        self.assertEqual((outside / "file.bin").read_bytes(), b"private")


if __name__ == "__main__":
    unittest.main()
