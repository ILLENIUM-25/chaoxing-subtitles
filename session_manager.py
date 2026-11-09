"""On-demand HTTP renewal and coordinated, local browser sign-in recovery."""

import contextlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from download_live import BASE, SCHEDULE_URL, CourseError, SessionExpiredError, get_page, load_cookies
from export_storage import file_guard
from project_core import CHINA


AUTH = BASE / ".local/auth.json"
LOGIN = BASE / ".local/login"
PROFILE = BASE / ".local/browser-profile"
HOME_URL = "https://i.mooc.chaoxing.com/space/index?ws=1"
ACTIVE = {"queued", "checking", "renewing", "awaiting_user"}


def now() -> str:
    return datetime.now(CHINA).isoformat()


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_state(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def cookie_key(cookie: dict):
    return cookie["domain"], cookie.get("path", "/"), cookie["name"]


def cookie_snapshot(client: httpx.Client, original: dict) -> dict:
    previous = {cookie_key(c): c for c in original.get("cookies", [])}
    result = {}
    for item in client.cookies.jar:
        if item.is_expired():
            continue
        key = (item.domain, item.path, item.name)
        old = previous.get(key, {})
        attributes = {key.lower(): value for key, value in item._rest.items()}
        http_only = attributes.get("httponly", old.get("httpOnly", False))
        same_site = attributes.get("samesite", old.get("sameSite", "Lax"))
        same_site = str(same_site).capitalize()
        if same_site not in {"Strict", "Lax", "None"}:
            same_site = "Lax"
        result[key] = {
            **old,
            "name": item.name, "value": item.value, "domain": item.domain,
            "path": item.path or "/", "secure": item.secure,
            "expires": item.expires if item.expires is not None else -1,
            "httpOnly": http_only is None or http_only is True or str(http_only).lower() == "true",
            "sameSite": same_site,
        }
    return result


def persist_http_cookies(auth: Path, original: dict, client: httpx.Client):
    """Merge response cookie changes without overwriting a newer browser login."""
    before = {cookie_key(c): c for c in original.get("cookies", [])}
    after = cookie_snapshot(client, original)
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    if not changed:
        return
    with file_guard(auth.with_name("auth.lock")):
        current = read_state(auth)
        merged = {cookie_key(c): c for c in current.get("cookies", [])}
        modified = False
        for key in changed:
            if merged.get(key) != before.get(key):
                continue
            if key in after:
                merged[key] = after[key]
            else:
                merged.pop(key, None)
            modified = True
        if modified:
            write_state(auth, {**current, "cookies": list(merged.values())})


def schedule_ready(body: str) -> bool:
    return all(marker in body for marker in ("semesterList", "function changeWeek", "getWeekDetail"))


def check_saved_session(auth: Path = AUTH) -> dict:
    try:
        original = read_state(auth)
        cookies = load_cookies(auth)
    except (CourseError, OSError, ValueError, KeyError, TypeError):
        return {"ready": False, "status": "login_required", "message": "本地登录态缺失或无法读取。"}
    with httpx.Client(cookies=cookies, timeout=20, follow_redirects=False, headers={"Referer": SCHEDULE_URL}) as client:
        try:
            response = get_page(client, SCHEDULE_URL)
        except SessionExpiredError:
            return {"ready": False, "status": "login_required", "message": "登录已失效，需要恢复。"}
        if not schedule_ready(response.text):
            raise CourseError("课表页面结构无法识别，不能确认登录状态。")
        from download_date import parse_schedule
        config = parse_schedule(response.text)
        persist_http_cookies(auth, original, client)
    verified = {"ready": True, "status": "ready", "semester_id": config["semester_id"], "checked_at": now(), "mode": "http"}
    if auth == AUTH:
        write_state(LOGIN / "verification.json", verified)
    return verified


def _process_signature(pid):
    from mcp_jobs import process_signature
    return process_signature(pid)


def login_status() -> dict:
    try:
        state = read_state(LOGIN / "status.json")
    except (OSError, ValueError):
        return {"status": "idle", "ready": False, "message": "尚未启动登录恢复。"}
    if state["status"] in ACTIVE:
        try:
            pid = read_state(LOGIN / "pid.json")
            if pid.get("attempt_id") == state.get("attempt_id"):
                alive, created = _process_signature(pid["pid"])
                dead = alive is False or (
                    created is not None and pid.get("created") is not None
                    and abs(created - pid["created"]) > 0.1
                )
            else:
                dead = (datetime.now(CHINA) - datetime.fromisoformat(state["requested_at"])).total_seconds() > 60
        except (OSError, ValueError, KeyError):
            dead = (datetime.now(CHINA) - datetime.fromisoformat(state["requested_at"])).total_seconds() > 60
        if dead:
            latest = read_state(LOGIN / "status.json")
            if latest.get("attempt_id") == state.get("attempt_id") and latest["status"] in ACTIVE:
                latest.update(status="interrupted", ready=False, message="登录恢复进程已退出，请重新发起登录。", finished_at=now())
                write_state(LOGIN / "status.json", latest)
            state = latest
    if state.get("attempt_id") and (LOGIN / f"interactive-{state['attempt_id']}.json").is_file():
        state["interactive"] = True
    return state


def start_login(force_refresh: bool = False, interactive: bool = True, timeout_seconds: int = 600) -> dict:
    if not 60 <= timeout_seconds <= 1800:
        raise CourseError("登录等待时间必须在 60 到 1800 秒之间。")
    with file_guard(LOGIN / "start.lock"):
        current = login_status()
        if current["status"] in ACTIVE:
            if interactive and not current.get("interactive"):
                write_state(LOGIN / f"interactive-{current['attempt_id']}.json", {"attempt_id": current["attempt_id"]})
                current["interactive"] = True
            return {**current, "reused": True}
        attempt = uuid.uuid4().hex
        state = {
            "attempt_id": attempt, "status": "queued", "ready": False,
            "force_refresh": force_refresh, "interactive": interactive,
            "timeout_seconds": timeout_seconds, "requested_at": now(),
            "message": "正在启动登录检查和恢复。",
        }
        write_state(LOGIN / "status.json", state)
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        try:
            with (LOGIN / "worker.log").open("wb") as log:
                process = subprocess.Popen(
                    [sys.executable, "-B", "-X", "utf8", "-u", str(BASE / "login_worker.py"), attempt],
                    cwd=str(BASE), stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, env=environment,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
            _, created = _process_signature(process.pid)
            write_state(LOGIN / "pid.json", {"attempt_id": attempt, "pid": process.pid, "created": created})
        except OSError as exc:
            state.update(status="failed", message="无法启动登录恢复进程。", finished_at=now())
            write_state(LOGIN / "status.json", state)
            raise CourseError(state["message"]) from exc
        return {**login_status(), "reused": False}


def ensure_session(auth: Path = AUTH, interactive=True, wait_for_login=True, callback=None, force_refresh=False) -> dict:
    if not force_refresh:
        status = check_saved_session(auth)
        if status["ready"]:
            return status
    if auth.resolve() != AUTH.resolve():
        raise SessionExpiredError("指定的登录态已失效；自动网页登录仅管理默认 auth.json。")
    request = start_login(force_refresh=force_refresh, interactive=interactive)
    if not wait_for_login:
        raise SessionExpiredError("登录恢复已在后台启动，请查询 get_login_status；需要验证时调用 start_login。")
    deadline = time.monotonic() + request.get("timeout_seconds", 600) + 180
    previous = None
    escalated = False
    while time.monotonic() < deadline:
        status = login_status()
        marker = (status["status"], status.get("message"))
        if marker != previous:
            if callback:
                callback("waiting_login", status)
            else:
                print(status.get("message", status["status"]), flush=True)
            previous = marker
        if status["status"] == "ready":
            verified = check_saved_session(auth)
            if not verified["ready"]:
                raise SessionExpiredError("浏览器恢复后仍无法访问课表，请重新登录。")
            if callback:
                callback("authenticated", verified)
            return verified
        if status["status"] == "login_required" and interactive and not escalated:
            start_login(force_refresh=True, interactive=True)
            escalated = True
        elif status["status"] not in ACTIVE:
            raise SessionExpiredError(status.get("message", "登录未完成。"))
        time.sleep(2)
    raise SessionExpiredError("等待登录超时，请完成浏览器中的登录后重试。")


@contextlib.contextmanager
def authenticated_client(auth: Path = AUTH, interactive=True, wait_for_login=True, callback=None):
    ensure_session(auth, interactive, wait_for_login, callback)
    original = read_state(auth)
    with httpx.Client(cookies=load_cookies(auth), timeout=30, follow_redirects=False, headers={"Referer": SCHEDULE_URL}) as client:
        recovery_failed = False
        def recover():
            nonlocal original, recovery_failed
            if recovery_failed:
                raise SessionExpiredError("登录未恢复，本批次不再重复打开登录窗口。")
            try:
                ensure_session(auth, interactive, wait_for_login, callback, force_refresh=True)
                original = read_state(auth)
                client.cookies = load_cookies(auth)
            except (CourseError, httpx.HTTPError):
                recovery_failed = True
                raise
        client._chaoxing_recover = recover
        try:
            yield client
        finally:
            # Cookie persistence must not hide an export exception or overwrite a
            # newer browser login. Only server-issued, non-conflicting deltas merge.
            try:
                persist_http_cookies(auth, original, client)
            except (OSError, ValueError, CourseError):
                pass
