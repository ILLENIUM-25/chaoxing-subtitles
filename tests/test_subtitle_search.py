"""Temporary phrase search, timestamp and source-line mapping tests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from project_core import CourseError
from search_subtitles import parse_vtt, phrase_matches, search_subtitles


VTT = (
    "WEBVTT\n\nNOTE skip this comment\n\n"
    "cue-1\n00:01:02.000 --> 00:01:04.000\n<v Teacher>网络，安全</v>\n\n"
    "cue-2\n00:01:04.500 --> 00:01:06.000\n协议 TCP/IP &amp; TLS。\n\n"
    "cue-3\n00:02:00.000 --> 00:02:02.000\n再看网络安全协议。\n"
)


class SearchTests(unittest.TestCase):
    def test_cross_cue_phrase_reports_real_time_and_source_lines(self):
        cues = parse_vtt(VTT)
        result = list(phrase_matches(cues, "网络安全协议"))
        self.assertEqual(len(result), 2)
        match = result[0]
        self.assertEqual(match["quote"], "网络，安全\n协议")
        self.assertEqual(match["start_time"], "00:01:02.000")
        self.assertEqual(match["end_time"], "00:01:06.000")
        self.assertEqual(match["start_seconds"], 62)
        self.assertEqual(match["vtt_line"], 7)
        self.assertEqual(match["vtt_end_line"], 11)
        self.assertEqual(match["txt_line"], 1)
        self.assertEqual(match["txt_end_line"], 2)
        self.assertEqual(match["context_after"][0]["start_time"], "00:02:00.000")

    def test_punctuation_whitespace_and_case_are_ignored(self):
        matches = list(phrase_matches(parse_vtt(VTT), "tcp ip & tls", 0))
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["quote"], "TCP/IP & TLS")
        self.assertEqual(matches[0]["context_after"], [])

    def test_invalid_query_and_non_vtt_are_rejected(self):
        with self.assertRaises(CourseError):
            list(phrase_matches(parse_vtt(VTT), "，！？"))
        with self.assertRaises(CourseError):
            parse_vtt("<html>login</html>")

    def test_results_are_temporary_and_limited_with_working_replay_link(self):
        with tempfile.TemporaryDirectory() as temp, patch("export_storage.BASE", Path(temp)):
            root = Path(temp) / "subtitles"
            folder = root / "2026-10-29" / "第01节_测试"
            folder.mkdir(parents=True)
            vtt = folder / "123_456.vtt"
            vtt.write_text(VTT, encoding="utf-8")
            report = {
                "date": "2026-10-29", "started_at": "2026-10-29T12:00:00+08:00",
                "courses": [{"live_id": "123", "course_name": "测试", "section": 1, "files": [{"vtt": str(vtt)}]}],
            }
            (folder.parent / "manifest.json").write_text(json.dumps(report), encoding="utf-8")
            before = sorted(str(p) for p in root.rglob("*"))
            result = search_subtitles("网络安全协议", limit=1, output_root=root)
            self.assertTrue(result["temporary"])
            self.assertEqual(result["total_matches"], 2)
            self.assertTrue(result["truncated"])
            self.assertIn("liveId=123", result["matches"][0]["replay_url"])
            self.assertNotIn("t", parse_qs(urlsplit(result["matches"][0]["replay_url"]).query))
            self.assertEqual(before, sorted(str(p) for p in root.rglob("*")))
            with self.assertRaises(CourseError):
                search_subtitles("网络", "2026-10-28", output_root=root)


if __name__ == "__main__":
    unittest.main()
