"""Authentication detection, cookie renewal and user-assisted task resumption."""

import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import httpx

from download_live import SCHEDULE_URL, CourseError, SessionExpiredError, get_page, load_cookies
from mcp_jobs import JobStore, write_json
from session_manager import (
    authenticated_client, check_saved_session, ensure_session, persist_http_cookies,
    start_login,
)


SCHEDULE = """
<select id="semesterList"><option selected value="18">test</option></select>
<script>
getWeekDetail('8');
function getWeekDetail(week) {}
function changeWeek(week) {
  ajax({data: {"fid": '1', "userId": '2', "week": week,
               "termYear": '2026', "termId": '1', "type": '1'}});
}
</script><a onclick="changeWeek(8)">week</a>
"""


def state(value="old"):
    return {
        "cookies": [{
            "name": "sid", "value": value, "domain": ".chaoxing.com",
            "path": "/", "expires": -1, "secure": True,
            "httpOnly": True, "sameSite": "Lax",
        }],
        "origins": [{"origin": "https://example.chaoxing.com", "localStorage": [{"name": "test", "value": "local"}]}],
    }


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.auth = self.root / "auth.json"
        self.auth.write_text(json.dumps(state()), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_http_200_login_page_is_detected(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, text="<script>alert('请先登录')</script>", headers={"content-type": "text/html"}
        ))) as client:
            with self.assertRaises(SessionExpiredError):
                get_page(client, SCHEDULE_URL)

    def test_lecture_text_is_not_mistaken_for_login_page(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            200, text="WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n请先登录这个实验系统\n",
            headers={"content-type": "text/vtt"},
        ))) as client:
            self.assertEqual(get_page(client, SCHEDULE_URL).status_code, 200)

    def test_permission_error_does_not_trigger_reauthentication(self):
        with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403))) as client:
            recover = Mock()
            client._chaoxing_recover = recover
            with self.assertRaises(CourseError):
                get_page(client, SCHEDULE_URL)
            recover.assert_not_called()

    def test_each_request_retries_authentication_only_once(self):
        requests = []
        def expired(request):
            requests.append(request)
            return httpx.Response(401)
        with httpx.Client(transport=httpx.MockTransport(expired)) as client:
            client._chaoxing_recover = Mock()
            with self.assertRaises(SessionExpiredError):
                get_page(client, SCHEDULE_URL, {"test": "value"})
            self.assertEqual(client._chaoxing_recover.call_count, 1)
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1].url.params["test"], "value")

    def test_server_cookie_renewal_is_saved_without_losing_browser_storage(self):
        client = httpx.Client(cookies=load_cookies(self.auth), transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=SCHEDULE, headers={
                "content-type": "text/html", "set-cookie": "sid=renewed; Domain=.chaoxing.com; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=3600"
            })
        ))
        with patch("session_manager.httpx.Client", return_value=client):
            result = check_saved_session(self.auth)
        saved = json.loads(self.auth.read_text(encoding="utf-8"))
        self.assertTrue(result["ready"])
        self.assertEqual(saved["cookies"][0]["value"], "renewed")
        self.assertTrue(saved["cookies"][0]["secure"])
        self.assertTrue(saved["cookies"][0]["httpOnly"])
        self.assertGreater(saved["cookies"][0]["expires"], time.time())
        self.assertEqual(saved["origins"], state()["origins"])

    def test_stale_http_cookie_never_overwrites_newer_browser_login(self):
        original = state()
        with httpx.Client(cookies=load_cookies(self.auth)) as client:
            client.cookies.set("sid", "http-change", domain=".chaoxing.com", path="/")
            self.auth.write_text(json.dumps(state("new-browser")), encoding="utf-8")
            persist_http_cookies(self.auth, original, client)
        self.assertEqual(json.loads(self.auth.read_text())["cookies"][0]["value"], "new-browser")

    def test_live_http_client_recovers_and_reloads_the_new_session(self):
        def endpoint(request):
            return httpx.Response(200 if "sid=fresh" in request.headers.get("cookie", "") else 401, text="ok")
        client = httpx.Client(cookies=load_cookies(self.auth), transport=httpx.MockTransport(endpoint))
        calls = []
        def ensure(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                self.auth.write_text(json.dumps(state("fresh")), encoding="utf-8")
            return {"ready": True}
        with patch("session_manager.ensure_session", side_effect=ensure), patch("session_manager.httpx.Client", return_value=client):
            with authenticated_client(auth=self.auth) as connection:
                self.assertEqual(get_page(connection, SCHEDULE_URL).status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["force_refresh"])

    def test_nonwaiting_tool_returns_while_login_runs_in_background(self):
        with patch("session_manager.AUTH", self.auth), patch("session_manager.check_saved_session", return_value={"ready": False}), patch("session_manager.start_login") as launch:
            with self.assertRaises(SessionExpiredError):
                ensure_session(self.auth, interactive=False, wait_for_login=False)
            launch.assert_called_once()

    def test_waiting_export_continues_after_user_authentication(self):
        callback = Mock()
        with patch("session_manager.AUTH", self.auth), patch("session_manager.check_saved_session", side_effect=[{"ready": False}, {"ready": True}]), patch("session_manager.start_login", return_value={"timeout_seconds": 60}), patch("session_manager.time.sleep"), patch("session_manager.login_status", side_effect=[
            {"status": "awaiting_user", "message": "scan"},
            {"status": "ready", "message": "done"},
        ]):
            self.assertTrue(ensure_session(self.auth, callback=callback)["ready"])
        self.assertEqual(callback.call_args_list[0].args[0], "waiting_login")
        self.assertEqual(callback.call_args_list[-1].args[0], "authenticated")

    def test_parallel_login_requests_share_one_worker(self):
        with patch("session_manager.LOGIN", self.root / "login"), patch("session_manager._process_signature", return_value=(True, 100.0)), patch("session_manager.subprocess.Popen", return_value=SimpleNamespace(pid=12345)) as launch:
            first = start_login()
            second = start_login()
            self.assertEqual(first["attempt_id"], second["attempt_id"])
            self.assertTrue(second["reused"])
            self.assertEqual(launch.call_count, 1)

    def test_interactive_request_upgrades_active_silent_login(self):
        with patch("session_manager.LOGIN", self.root / "login"), patch("session_manager._process_signature", return_value=(True, 100.0)), patch("session_manager.subprocess.Popen", return_value=SimpleNamespace(pid=12345)) as launch:
            first = start_login(interactive=False)
            second = start_login(interactive=True)
            self.assertEqual(first["attempt_id"], second["attempt_id"])
            self.assertTrue(second["interactive"])
            self.assertTrue((self.root / "login" / f"interactive-{first['attempt_id']}.json").is_file())
            self.assertEqual(launch.call_count, 1)

    def test_silent_mode_does_not_open_verification_window(self):
        import login_worker
        context = MagicMock()
        context.__enter__.return_value = context
        context.pages = [MagicMock()]
        with patch("login_worker.LOGIN", self.root / "login"), patch("login_worker.PROFILE", self.root / "profile"), patch("login_worker.navigate"), patch("login_worker.validated_browser_state", return_value=False), patch("login_worker.sync_playwright") as playwright:
            launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
            launch.return_value = context
            self.assertEqual(login_worker.recover_browser(Mock(), False, 60, False), "login_required")
            self.assertEqual(launch.call_count, 1)
            self.assertTrue(launch.call_args.kwargs["headless"])

    def test_expired_browser_falls_back_to_user_verification_and_then_succeeds(self):
        import login_worker
        contexts = [MagicMock(), MagicMock()]
        for context in contexts:
            context.__enter__.return_value = context
            context.pages = [MagicMock()]
        with patch("login_worker.LOGIN", self.root / "login"), patch("login_worker.PROFILE", self.root / "profile"), patch("login_worker.navigate"), patch("login_worker.validated_browser_state", side_effect=[False, True]), patch("login_worker.sync_playwright") as playwright:
            launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
            launch.side_effect = contexts
            self.assertEqual(login_worker.recover_browser(Mock(), False, 60, False, interactive_request=lambda: True), "browser_interactive")
            self.assertEqual([call.kwargs["headless"] for call in launch.call_args_list], [True, False])

    def test_export_worker_exposes_waiting_state_and_resumes_original_task(self):
        import export_worker
        store = JobStore(self.root)
        job_id = "a" * 32
        write_json(store.directory(job_id) / "request.json", {
            "job_id": job_id, "date": "2026-10-26", "semester_id": None,
            "overwrite": False, "keep_history": False,
        })
        observed = []
        @contextlib.contextmanager
        def session(callback):
            callback("waiting_login", {"status": "awaiting_user", "message": "scan"})
            observed.append(store.state(job_id))
            callback("authenticated", {"ready": True})
            observed.append(store.state(job_id))
            yield Mock()
        listing = {"date": "2026-10-26", "courses": [{"replay_available": True}]}
        with patch("export_worker.JobStore", return_value=store), patch("export_worker.authenticated_client", side_effect=session), patch("export_worker.list_courses_for_date", return_value=listing), patch("export_worker.export_date", return_value={"counts": {"complete": 1}, "cleanup": {"status": "complete"}, "courses": [{"result": "complete"}]}):
            self.assertEqual(export_worker.run(job_id), 0)
        self.assertEqual(observed[0]["phase"], "waiting_login")
        self.assertEqual(observed[0]["login"]["status"], "awaiting_user")
        self.assertEqual(observed[1]["phase"], "querying")
        self.assertEqual(store.state(job_id)["status"], "complete")
        self.assertEqual(store.state(job_id)["counts"], {"complete": 1})


if __name__ == "__main__":
    unittest.main()
