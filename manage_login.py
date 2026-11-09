"""Start login/renewal and display its progress, without asking for passwords."""

import argparse
import json
import time

from download_live import CourseError
from session_manager import ACTIVE, login_status, start_login


def main():
    parser = argparse.ArgumentParser(description="复用专用 Edge 登录；失效时扫码验证并自动保存。")
    parser.add_argument("--refresh", action="store_true", help="即使 HTTP 会话有效，也刷新专用浏览器会话")
    parser.add_argument("--silent", action="store_true", help="仅尝试静默恢复，不打开扫码窗口")
    parser.add_argument("--status", action="store_true", help="只查看最近一次登录恢复状态")
    args = parser.parse_args()
    try:
        if args.status:
            print(json.dumps(login_status(), ensure_ascii=False, indent=2))
            return 0
        state = start_login(force_refresh=args.refresh, interactive=not args.silent)
        previous = None
        while True:
            marker = (state["status"], state.get("message"))
            if marker != previous:
                print(state.get("message", state["status"]), flush=True)
                previous = marker
            if state["status"] not in ACTIVE:
                return 0 if state["status"] == "ready" else 1
            time.sleep(2)
            state = login_status()
    except CourseError as exc:
        print(f"登录失败：{exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
