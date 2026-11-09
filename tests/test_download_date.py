"""Date selection and subtitle text conversion tests."""

import unittest
from datetime import date, datetime

from download_date import (
    CourseError, filter_courses, target_week, vtt_to_text, week_monday,
)


def record(live_id, stamp, status=2):
    time = int(datetime.fromisoformat(stamp).timestamp() * 1000)
    return {
        "id": live_id, "startTime": {"time": time}, "status": status,
        "jie": 1, "courseName": "测试课程", "scheduleId": 1,
    }


class DateExportTests(unittest.TestCase):
    def test_date_filter_uses_china_timezone_and_preserves_distinct_replays(self):
        rows = [
            record(1, "2026-10-25T16:05:00+00:00"),
            record(2, "2026-10-26T00:05:00+08:00"),
            record(3, "2026-10-25T15:59:00+00:00"),
        ]
        courses = filter_courses(rows, date(2026, 10, 26))
        self.assertEqual([c["live_id"] for c in courses], ["1", "2"])

    def test_same_live_id_is_deduplicated_but_not_same_course_name(self):
        a = record(1, "2026-10-26T08:00:00+08:00")
        b = record(2, "2026-10-26T08:00:00+08:00")
        self.assertEqual(len(filter_courses([a, a, b], date(2026, 10, 26))), 2)

    def test_live_or_future_courses_are_not_downloadable(self):
        rows = [record(i + 1, "2026-10-26T08:00:00+08:00", status=i) for i in range(3)]
        self.assertEqual(
            [c["replay_available"] for c in filter_courses(rows, date(2026, 10, 26))],
            [False, False, True],
        )

    def test_missing_time_is_an_error_rather_than_silent_omission(self):
        with self.assertRaises(CourseError):
            filter_courses([{"id": 1}], date(2026, 10, 26))

    def test_week_mapping_across_year_boundary(self):
        self.assertEqual(target_week(date(2027, 1, 3), date(2026, 12, 28), 17), 17)
        self.assertEqual(target_week(date(2027, 1, 4), date(2026, 12, 28), 17), 18)

    def test_mixed_weeks_are_rejected(self):
        with self.assertRaises(CourseError):
            week_monday([
                record(1, "2026-10-26T08:00:00+08:00"),
                record(2, "2026-11-02T08:00:00+08:00"),
            ])

    def test_text_export_retains_spoken_numbers_and_discards_vtt_metadata(self):
        source = (
            "WEBVTT\n\nNOTE private note\nignore me\n\n"
            "1\n00:00:01.000 --> 00:00:02.000\n<v Teacher>2026 &amp; 示例</v>\n\n"
            "2\n00:00:02.000 --> 00:00:03.000\n下一句\n"
        )
        self.assertEqual(vtt_to_text(source), "2026 & 示例\n下一句\n")


if __name__ == "__main__":
    unittest.main()
