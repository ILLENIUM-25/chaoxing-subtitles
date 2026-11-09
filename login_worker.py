"""Restore SSO in a dedicated Edge profile; request user verification if needed."""

import os
import re
import sys
import time
from urllib.parse import unquote, urlsplit

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from download_live import REPLAY_URL, SCHEDULE_URL, CourseError
from export_storage import file_guard
from session_manager import (
    AUTH, HOME_URL, LOGIN, PROFILE, check_saved_session, now, read_state,
    schedule_ready, write_state,
)


def browser_proxy():
    value = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if not value:
        return None
    parts = urlsplit(value if "://" in value else "http://" + value)
    if not parts.hostname:
        raise CourseError("浏览器代理地址无法识别。")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    proxy = {"server": f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")}
    if parts.username:
        proxy["username"] = unquote(parts.username)
    if parts.password:
        proxy["password"] = unquote(parts.password)
    bypass = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if bypass:
        proxy["bypass"] = bypass
    return proxy


def seed_cookies(context):
    try:
        state = read_state(AUTH)
    except (OSError, ValueError):
        return
    cookies = [
        cookie for cookie in state.get("cookies", [])
        if (cookie.get("domain", "").lstrip(".") == "chaoxing.com" or cookie.get("domain", "").endswith(".chaoxing.com"))
        and (cookie.get("expires", -1) < 0 or cookie.get("expires", -1) > time.time())
    ]
    if cookies:
        context.add_cookies(cookies)


def validated_browser_state(context) -> bool:
    try:
        response = context.request.get(SCHEDULE_URL, timeout=12000)
        if response.status != 200:
            return False
        source = response.text()
        if not schedule_ready(source):
            return False
        # Hydrate the replay domain with a read-only HTTP request, without
        # opening a player or downloading video/media.
        replay = re.search(r"viewCourseLive\(\s*['\"]?(\d+)['\"]?\s*,\s*['\"]?2['\"]?\s*,", source)
        if replay:
            context.request.get(REPLAY_URL, params={"isStudent": "1", "liveId": replay.group(1)}, timeout=15000)
        snapshot = context.storage_state(indexed_db=True)
        with file_guard(AUTH.with_name("auth.lock")):
            write_state(AUTH, snapshot)
        return check_saved_session()["ready"]
    except PlaywrightError:
        return False


def navigate(page, url):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
    except PlaywrightError:
        # A slow page may still have completed its authentication redirects.
        pass


def recover_browser(update, interactive: bool, timeout_seconds: int, seed: bool, interactive_request=None) -> str:
    with sync_playwright() as playwright:
        options = {"channel": "msedge", "proxy": browser_proxy(), "viewport": {"width": 1280, "height": 850}}
        update("renewing", "正在使用专用 Edge 会话尝试自动恢复登录。")
        with playwright.chromium.launch_persistent_context(str(PROFILE), headless=True, **options) as context:
            if seed:
                seed_cookies(context)
            write_state(LOGIN / "profile_initialized.json", {"initialized_at": now()})
            page = context.pages[0] if context.pages else context.new_page()
            navigate(page, HOME_URL)
            navigate(page, SCHEDULE_URL)
            if validated_browser_state(context):
                return "browser_sso"
        if not interactive and not (interactive_request and interactive_request()):
            return "login_required"
        update("awaiting_user", "请在即将打开的 Edge 窗口扫码或完成验证；登录成功后程序会自动保存并继续。")
        with playwright.chromium.launch_persistent_context(str(PROFILE), headless=False, **options) as context:
            closed = [False]
            context.on("close", lambda *args: closed.__setitem__(0, True))
            page = context.pages[0] if context.pages else context.new_page()
            navigate(page, HOME_URL)
            deadline = time.monotonic() + timeout_seconds
            next_check = 0.0
            visited_schedule = False
            while time.monotonic() < deadline:
                if closed[0]:
                    raise CourseError("登录窗口已关闭，登录尚未完成。")
                if time.monotonic() >= next_check:
                    if validated_browser_state(context):
                        return "browser_interactive"
                    # Once the user reaches their personal space, visit the
                    # known course page to complete any browser-side SSO redirect.
                    for candidate in context.pages:
                        parts = urlsplit(candidate.url)
                        if not visited_schedule and parts.hostname == "i.mooc.chaoxing.com" and parts.path.startswith("/space"):
                            target = context.new_page()
                            navigate(target, SCHEDULE_URL)
                            visited_schedule = True
                            break
                    next_check = time.monotonic() + 5
                pages = context.pages
                if not pages:
                    raise CourseError("登录窗口已关闭，登录尚未完成。")
                try:
                    pages[0].wait_for_timeout(1000)
                except PlaywrightError:
                    if closed[0]:
                        raise CourseError("登录窗口已关闭，登录尚未完成。")
            raise CourseError("等待扫码或验证超时，请重新发起登录。")


def run(attempt_id: str) -> int:
    state = read_state(LOGIN / "status.json")
    if state.get("attempt_id") != attempt_id:
        return 1
    def update(status, message):
        if (LOGIN / f"interactive-{attempt_id}.json").is_file():
            state["interactive"] = True
        state.update(status=status, ready=status == "ready", message=message, updated_at=now())
        write_state(LOGIN / "status.json", state)
    try:
        with file_guard(LOGIN / "browser.lock"):
            update("checking", "正在检查已保存的超星会话。")
            saved = check_saved_session()
            if saved["ready"] and not state["force_refresh"]:
                mode = "http"
            else:
                first_profile = not (LOGIN / "profile_initialized.json").is_file()
                mode = recover_browser(
                    update, state["interactive"], state["timeout_seconds"], first_profile or saved["ready"],
                    interactive_request=lambda: (LOGIN / f"interactive-{attempt_id}.json").is_file(),
                )
            if mode == "login_required":
                state["finished_at"] = now()
                update("login_required", "静默恢复未成功，需要扫码或验证；请启动交互式登录。")
                return 1
            state.update(mode=mode, finished_at=now())
            update("ready", "登录已恢复并保存，可以继续查询和导出。")
            return 0
    except CourseError as exc:
        message = str(exc)
    except PlaywrightError as exc:
        message = f"Edge 登录恢复失败（{type(exc).__name__}），请检查浏览器或代理。"
    except httpx.HTTPError as exc:
        message = f"登录检查网络失败（{type(exc).__name__}），请检查校园网、VPN 或代理。"
    except Exception as exc:
        # Avoid raw exception strings: they may embed login URLs or headers.
        message = f"登录恢复失败（{type(exc).__name__}）。"
    state["finished_at"] = now()
    update("failed", message)
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: login_worker.py ATTEMPT_ID")
    raise SystemExit(run(sys.argv[1]))
