"""Persistent background job lifecycle tests."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from download_date import CHINA
from project_core import CourseError
from mcp_jobs import JobStore, now, write_json


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temporary.name))
        self.launch = patch("mcp_jobs.subprocess.Popen", return_value=SimpleNamespace(pid=12345))
        self.signature = patch("mcp_jobs.process_signature", return_value=(True, 100.0))
        self.popen = self.launch.start()
        self.signature.start()

    def tearDown(self):
        self.signature.stop()
        self.launch.stop()
        self.temporary.cleanup()

    def test_duplicate_active_date_reuses_job_and_does_not_launch_twice(self):
        first = self.store.start("2026-10-26")
        again = self.store.start("2026-10-26")
        self.assertEqual(first["job_id"], again["job_id"])
        self.assertTrue(again["reused"])
        self.assertEqual(self.popen.call_count, 1)

    def test_active_date_with_different_semester_is_rejected(self):
        self.store.start("2026-10-26", "18")
        with self.assertRaises(CourseError):
            self.store.start("2026-10-26", "17")

    def test_dead_worker_is_interrupted_and_can_be_restarted(self):
        first = self.store.start("2026-10-26")
        with patch("mcp_jobs.process_signature", return_value=(False, None)):
            self.assertEqual(self.store.summary(first["job_id"])["status"], "interrupted")
        second = self.store.start("2026-10-26")
        self.assertNotEqual(first["job_id"], second["job_id"])

    def test_reused_pid_is_not_mistaken_for_original_worker(self):
        first = self.store.start("2026-10-26")
        with patch("mcp_jobs.process_signature", return_value=(True, 200.0)):
            self.assertEqual(self.store.summary(first["job_id"])["status"], "interrupted")

    def test_invalid_dates_and_job_paths_are_rejected(self):
        for value in ("../auth.json", "2026-02-30", "20261026"):
            with self.assertRaises(CourseError):
                self.store.start(value)
        with self.assertRaises(CourseError):
            self.store.summary("../../auth")

    def test_previous_manifest_is_not_reported_as_new_job_progress(self):
        job = self.store.start("2026-10-26")
        state = self.store.state(job["job_id"])
        state.update(status="running", started_at=now())
        self.store.save(job["job_id"], state)
        manifest = self.store.root / "subtitles/2026-10-26/manifest.json"
        yesterday = (datetime.now(CHINA) - timedelta(days=1)).isoformat()
        write_json(manifest, {"date": "2026-10-26", "started_at": yesterday, "counts": {"complete": 10}})
        self.assertNotIn("counts", self.store.summary(job["job_id"]))
        write_json(manifest, {"date": "2026-10-26", "job_id": job["job_id"], "started_at": now(), "counts": {"pending": 7, "complete": 3}})
        self.assertEqual(self.store.summary(job["job_id"])["counts"]["complete"], 3)

    def test_later_export_never_replaces_an_older_jobs_counts(self):
        old = self.store.start("2026-10-26")
        state = self.store.state(old["job_id"])
        state.update(status="complete", started_at=now(), counts={"complete": 10})
        self.store.save(old["job_id"], state)
        newer = self.store.start("2026-10-26")
        manifest = self.store.root / "subtitles/2026-10-26/manifest.json"
        write_json(manifest, {"date": "2026-10-26", "job_id": newer["job_id"], "started_at": now(), "counts": {"pending": 10}})
        result = self.store.summary(old["job_id"])
        self.assertEqual(result["counts"], {"complete": 10})
        self.assertFalse(result["output_available"])


if __name__ == "__main__":
    unittest.main()
