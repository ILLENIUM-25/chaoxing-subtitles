"""Regression coverage for malformed subtitles and output path integrity."""

import json
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from download_date import export_date
from download_live import download_live
from export_storage import checked_output_file, cleanup_previous_exports
from project_core import CourseError
from search_subtitles import search_subtitles
from subtitle_format import parse_vtt, plain_text, valid_vtt


VALID = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n关键语句\n"
BAD = "WEBVTT\n\nnot-a-time --> still-not-a-time\n关键语句\n"


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.storage_base = patch("export_storage.BASE", self.root)
        self.storage_base.start()

    def tearDown(self):
        self.storage_base.stop()
        self.temporary.cleanup()

    def test_header_or_comment_with_arrow_does_not_count_as_subtitle(self):
        self.assertFalse(valid_vtt("WEBVTT\n\nNOTE x --> y\n"))
        self.assertFalse(valid_vtt(BAD))
        self.assertFalse(valid_vtt("WEBVTTgarbage\n\n00:00:01.000 --> 00:00:02.000\ntext\n"))
        self.assertFalse(valid_vtt("WEBVTT\n\n00:00:03.000 --> 00:00:02.000\ntext\n"))

    def test_invalid_cli_id_is_rejected_before_login(self):
        from download_live import main
        with patch.object(sys, "argv", ["download_live.py", "invalid-id"]), patch("session_manager.authenticated_client") as login, patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)
        login.assert_not_called()

    def test_text_and_location_share_the_same_parser(self):
        source = "WEBVTT\n\nNOTEbook-cue-id\n00:00:01.000 --> 00:00:02.000\n1 &amp; 2\n"
        self.assertEqual(plain_text(parse_vtt(source)), "1 & 2\n")

    def test_invalid_cached_subtitle_is_downloaded_again(self):
        file = self.root / "123_456.vtt"
        file.write_text(BAD, encoding="utf-8")
        def endpoint(request):
            if request.url.path.endswith("viewNewCourseLive1"):
                return httpx.Response(200, text="<script>data += '&workDetailId=456'</script>")
            return httpx.Response(200, text=VALID, headers={"content-type": "text/vtt"})
        with httpx.Client(transport=httpx.MockTransport(endpoint)) as client:
            self.assertEqual(download_live(client, "123", self.root), [file])
        self.assertTrue(valid_vtt(file.read_text(encoding="utf-8")))

    def make_report(self):
        directory = self.root / "subtitles/2026-10-29/第01节_测试"
        directory.mkdir(parents=True)
        vtt, txt = directory / "123_456.vtt", directory / "123_456.txt"
        vtt.write_text(VALID, encoding="utf-8")
        txt.write_text("关键语句\n", encoding="utf-8")
        report = {
            "date": "2026-10-29", "started_at": "2026-10-29T12:00:00+08:00",
            "courses": [{"live_id": "123", "course_name": "测试", "section": 1, "result": "complete", "replay_available": True, "files": [{"vtt": str(vtt), "txt": str(txt)}]}],
        }
        (directory.parent / "manifest.json").write_text(json.dumps(report), encoding="utf-8")
        return report, vtt, txt

    def test_manually_edited_text_does_not_produce_false_line_numbers(self):
        _, _, txt = self.make_report()
        txt.write_text("用户插入的一行\n关键语句\n", encoding="utf-8")
        result = search_subtitles("关键语句", output_root=self.root / "subtitles")
        match = result["matches"][0]
        self.assertIsNone(match["txt_file"])
        self.assertIsNone(match["txt_line"])
        self.assertGreater(match["vtt_line"], 0)
        self.assertEqual(result["text_mapping_warnings"], [txt.name])

    def test_malformed_new_caption_cannot_trigger_history_deletion(self):
        report, vtt, _ = self.make_report()
        older = self.root / "subtitles/2026-10-28"
        older.mkdir()
        vtt.write_text(BAD, encoding="utf-8")
        result = cleanup_previous_exports(self.root / "subtitles", report)
        self.assertEqual(result["status"], "skipped")
        self.assertTrue(older.exists())

    def test_partial_search_skips_corrupt_files_and_keeps_valid_matches(self):
        report, _, _ = self.make_report()
        invalid = self.root / "subtitles/2026-10-29/第01节_测试/999_888.vtt"
        invalid.write_text(BAD, encoding="utf-8")
        report["courses"][0]["files"].append({"vtt": str(invalid)})
        (invalid.parent.parent / "manifest.json").write_text(json.dumps(report), encoding="utf-8")
        result = search_subtitles("关键语句", output_root=self.root / "subtitles")
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["invalid_files"], 1)

    def test_progress_and_manifest_are_tagged_with_exact_job(self):
        report, vtt, _ = self.make_report()
        listing = {**report, "courses": [{**report["courses"][0], "result": "pending"}]}
        snapshots = []
        with patch("download_date.download_live", return_value=[vtt]):
            result = export_date(None, listing, self.root / "subtitles", keep_history=True, job_id="a" * 32, progress=lambda r: snapshots.append(dict(r["counts"])))
        self.assertEqual(result["job_id"], "a" * 32)
        self.assertEqual(snapshots[0], {"pending": 1})
        self.assertEqual(snapshots[-1], {"complete": 1})

    def test_output_directory_junction_never_writes_outside_target(self):
        if os.name != "nt":
            self.skipTest("Windows junction test")
        outside = self.root / "outside"
        outside.mkdir()
        junction = self.root / "linked"
        if not outside.resolve().is_relative_to(self.root) or junction.parent.resolve() != self.root:
            self.fail("Invalid fixture boundary")
        process = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
        if process.returncode:
            self.skipTest("Junction creation unavailable")
        try:
            with self.assertRaises(CourseError):
                checked_output_file(junction, "123_456.vtt")
            self.assertFalse((outside / "123_456.vtt").exists())
        finally:
            junction.rmdir()


if __name__ == "__main__":
    unittest.main()
