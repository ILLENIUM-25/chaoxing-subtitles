"""Download a Chaoxing replay subtitle using a liveId and saved login state."""

import argparse
import json
import re
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx


from project_core import (
    BASE, SCHEDULE_URL, REPLAY_URL, SUBTITLE_URL,
    CourseError, NoSubtitleError, SessionExpiredError,
)
from subtitle_format import parse_vtt, valid_vtt
from export_storage import checked_output_file


def load_cookies(auth: Path) -> CookieJar:
    """Preserve domain, path, expiry and Secure attributes from Playwright state."""
    try:
        state = json.loads(auth.read_text(encoding="utf-8-sig"))
        jar = CookieJar()
        for item in state["cookies"]:
            domain = item["domain"]
            expires = item.get("expires", -1)
            jar.set_cookie(Cookie(
                version=0, name=item["name"], value=item["value"],
                port=None, port_specified=False,
                domain=domain, domain_specified=True,
                domain_initial_dot=domain.startswith("."),
                path=item.get("path", "/"), path_specified=True,
                secure=item.get("secure", False),
                expires=int(expires) if expires > 0 else None,
                discard=expires <= 0, comment=None, comment_url=None,
                rest={"HttpOnly": item.get("httpOnly", False), "SameSite": item.get("sameSite", "Lax")}, rfc2109=False,
            ))
        return jar
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CourseError("无法读取登录态，请先生成 .local/auth.json。") from exc


def get_page(client: httpx.Client, url: str, params=None) -> httpx.Response:
    try:
        return _get_page(client, url, params)
    except SessionExpiredError:
        recover = getattr(client, "_chaoxing_recover", None)
        if recover is None:
            raise
        recover()
        # Each request is retried at most once after authentication recovery.
        return _get_page(client, url, params)


def _get_page(client: httpx.Client, url: str, params=None) -> httpx.Response:
    """Follow bounded redirects while keeping session traffic on Chaoxing hosts."""
    for _ in range(10):
        host = urlsplit(url).hostname or ""
        if host != "chaoxing.com" and not host.endswith(".chaoxing.com"):
            raise CourseError("页面跳转到了超星以外的域名，已停止请求。")
        response = client.get(url, params=params)
        params = None
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise CourseError("服务器返回了缺少目标地址的跳转。")
            url = urljoin(str(response.url), location)
            continue
        if response.status_code == 401:
            raise SessionExpiredError("超星登录已失效，需要恢复登录。")
        if response.status_code != 200:
            raise CourseError(f"服务器返回 HTTP {response.status_code}。")
        if login_response(response):
            raise SessionExpiredError("超星要求重新登录。")
        return response
    raise CourseError("页面跳转次数过多，可能需要重新登录。")


def login_response(response: httpx.Response) -> bool:
    parts = urlsplit(str(response.url))
    if (parts.hostname or "").startswith("passport") and "/login" in parts.path:
        return True
    content_type = response.headers.get("content-type", "").lower()
    if "vtt" in content_type:
        return False
    body = response.text
    if "html" in content_type or body.lstrip().startswith("<"):
        return (
            (len(body) < 12000 and any(message in body for message in ("请先登录", "登录已过期", "请重新登录")))
            or bool(re.search(r"<input\b[^>]*\btype\s*=\s*['\"]password['\"]", body, re.I))
        )
    if "json" in content_type:
        try:
            data = response.json()
            if isinstance(data, dict):
                message = str(data.get("msg", data.get("message", "")))
                return any(text in message.lower() for text in ("请先登录", "未登录", "请重新登录", "login expired", "not logged in"))
        except ValueError:
            pass
    return False


def find_work_detail_ids(response: httpx.Response) -> list[str]:
    """Read only the observed URL/assignment forms; never execute page JavaScript."""
    ids = set(parse_qs(urlsplit(str(response.url)).query).get("workDetailId", []))
    ids.update(re.findall(
        r"\bworkDetailId\s*[\"']?\s*[:=]\s*[\"']?\s*(\d+)",
        response.text,
    ))
    return sorted(item for item in ids if item.isdigit())


def download_live(
    client: httpx.Client, live_id: str, output_dir: Path, overwrite: bool = False
) -> list[Path]:
    if not live_id.isdigit():
        raise CourseError("liveId 必须是数字。")
    print(f"查询回放：liveId={live_id}", flush=True)
    replay = get_page(client, REPLAY_URL, {"isStudent": "1", "liveId": live_id})
    work_ids = find_work_detail_ids(replay)
    if not work_ids:
        raise CourseError("回放页面没有字幕编号，请检查登录态或回放是否可用。")
    paths = []
    for work_id in work_ids:
        output = checked_output_file(output_dir, f"{live_id}_{work_id}.vtt")
        if output.is_file() and not overwrite:
            try:
                cached_valid = valid_vtt(output.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError):
                cached_valid = False
            if cached_valid:
                print(f"已存在，跳过：{output}", flush=True)
                paths.append(output)
                continue
        print(f"下载字幕：workDetailId={work_id}", flush=True)
        response = get_page(client, SUBTITLE_URL, {"workDetailId": work_id})
        text = response.content.decode("utf-8-sig").lstrip(" \r\n\t")
        cues = parse_vtt(text)
        if not cues:
            raise NoSubtitleError("此回放返回了空字幕，未发现时间轴。")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = checked_output_file(output_dir, output.with_suffix(".vtt.part").name)
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(output)
        print(f"已保存：{output}（{output.stat().st_size} 字节）", flush=True)
        paths.append(output)
    return paths


def main() -> int:
    from session_manager import authenticated_client
    parser = argparse.ArgumentParser(description="输入回放 liveId，自动寻找字幕编号并下载。")
    parser.add_argument("live_id", help="例如：12828087")
    parser.add_argument("--auth", type=Path, default=BASE / ".local/auth.json")
    parser.add_argument("--output", type=Path, default=BASE / "subtitles")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if not args.live_id.isdigit():
        parser.error("live_id 必须是数字。")
    try:
        with authenticated_client(auth=args.auth) as client:
            download_live(client, args.live_id, args.output, args.overwrite)
        return 0
    except CourseError as exc:
        print(f"失败：{exc}", flush=True)
    except httpx.HTTPError as exc:
        print(
            f"网络请求失败（{type(exc).__name__}），请检查校园网、VPN 或代理。",
            flush=True,
        )
    except (OSError, UnicodeError):
        print("文件读取或保存失败，请检查文件编码及目录权限。", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
