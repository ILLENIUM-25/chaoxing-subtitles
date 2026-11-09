"""Retention checks using isolated fixtures, including Windows junctions."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from download_date import export_date
from project_core import CourseError
from export_storage import cleanup_previous_exports, _generated_tree, checked_directory


VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n测试语句\n"


def fixture(root: Path, target: str):
    folder = root / target / "第01节_测试课程"
    folder.mkdir(parents=True)
    vtt = folder / "100_200.vtt"
    txt = folder / "100_200.txt"
    vtt.write_text(VTT, encoding="utf-8")
    txt.write_text("测试语句\n", encoding="utf-8")
    report = {
        "date": target, "semester_id": "18", "week": 8,
        "started_at": f"{target}T12:00:00+08:00",
        "courses": [{
            "result": "complete", "live_id": "100", "course_name": "测试课程",
            "section": 1, "replay_available": True,
            "files": [{"vtt": str(vtt), "txt": str(txt)}],
        }],
    }
    (root / target / "manifest.json").write_text(json.dumps(report), encoding="utf-8")
    return report


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / "subtitles"
        self.storage_base = patch("export_storage.BASE", self.root.parent)
        self.storage_base.start()
        self.report = fixture(self.root, "2026-10-29")

    def tearDown(self):
        self.storage_base.stop()
        self.temporary.cleanup()

    def test_success_removes_older_generated_export_and_duplicate_samples(self):
        fixture(self.root, "2026-10-28")
        fixture(self.root, "2026-10-30")
        (self.root / "200.vtt").write_text(VTT, encoding="utf-8")
        (self.root / "999.vtt").write_text("unclassified", encoding="utf-8")
        result = cleanup_previous_exports(self.root, self.report)
        self.assertEqual(result["removed_dates"], ["2026-10-28"])
        self.assertEqual(result["removed_legacy_files"], ["200.vtt"])
        self.assertFalse((self.root / "2026-10-28").exists())
        self.assertTrue((self.root / "2026-10-29").exists())
        self.assertTrue((self.root / "2026-10-30").exists())
        self.assertTrue((self.root / "999.vtt").exists())

    def test_partial_failure_or_empty_day_keeps_history(self):
        fixture(self.root, "2026-10-28")
        for status in ("failed", "no_subtitle", "pending", "unavailable"):
            self.report["courses"][0]["result"] = status
            self.assertEqual(cleanup_previous_exports(self.root, self.report)["status"], "skipped")
            self.assertTrue((self.root / "2026-10-28").exists())
        self.report["courses"] = []
        self.assertEqual(cleanup_previous_exports(self.root, self.report)["status"], "skipped")

    def test_missing_new_subtitle_keeps_history(self):
        fixture(self.root, "2026-10-28")
        Path(self.report["courses"][0]["files"][0]["txt"]).unlink()
        self.assertEqual(cleanup_previous_exports(self.root, self.report)["status"], "skipped")
        self.assertTrue((self.root / "2026-10-28").exists())

    def test_user_files_and_unrecognized_date_folders_are_preserved(self):
        fixture(self.root, "2026-10-28")
        (self.root / "2026-10-28/my-notes.txt").write_text("keep", encoding="utf-8")
        (self.root / "2026-10-27").mkdir()
        result = cleanup_previous_exports(self.root, self.report)
        self.assertEqual(result["removed_dates"], [])
        self.assertEqual(set(result["protected"]), {"2026-10-27", "2026-10-28"})

    def test_path_outside_output_root_is_rejected_before_removal(self):
        with self.assertRaises(CourseError):
            checked_directory(self.root, "../2026-10-28")
        outside = self.root.parent / "outside"
        outside.mkdir()
        self.assertFalse(_generated_tree(self.root, outside))

    def test_windows_junction_is_never_traversed_or_removed(self):
        import os
        if os.name != "nt":
            self.skipTest("Windows junction test")
        outside = self.root.parent / "outside"
        outside.mkdir()
        sentinel = outside / "keep.txt"
        sentinel.write_text("must survive", encoding="utf-8")
        junction = self.root / "2026-10-28"
        # Both fixture paths have been resolved and are inside this test's root.
        if not junction.parent.is_relative_to(Path(self.temporary.name).resolve()) or not outside.is_relative_to(Path(self.temporary.name).resolve()):
            self.fail("Invalid fixture boundary")
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
        if result.returncode:
            self.skipTest("Junction creation unavailable")
        try:
            result = cleanup_previous_exports(self.root, self.report)
            self.assertIn("2026-10-28", result["protected"])
            self.assertTrue(sentinel.is_file())
        finally:
            # Remove only the junction itself; never recurse into its target.
            junction.rmdir()

    def test_explicit_keep_history_disables_cleanup(self):
        fixture(self.root, "2026-10-28")
        course = self.report["courses"][0]
        listing = {**self.report, "courses": [{**course, "result": "pending"}]}
        vtt = Path(course["files"][0]["vtt"])
        with patch("download_date.download_live", return_value=[vtt]):
            result = export_date(None, listing, self.root, keep_history=True)
        self.assertEqual(result["cleanup"]["status"], "disabled")
        self.assertTrue((self.root / "2026-10-28").exists())


if __name__ == "__main__":
    unittest.main()
