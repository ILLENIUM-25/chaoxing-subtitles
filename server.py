"""Local stdio MCP server for Chaoxing course subtitle exports."""

import logging
from datetime import date as CalendarDate

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from download_date import list_courses_for_date
from download_live import CourseError, SessionExpiredError
from mcp_jobs import JobStore, validate_date
from search_subtitles import search_subtitles as search_local_subtitles
from session_manager import (
    authenticated_client, check_saved_session, login_status,
    start_login as launch_login,
)


mcp = FastMCP(
    "chaoxing-subtitles",
    instructions=(
        "查询和导出当前用户有权访问的超星课程回放字幕。日期使用 Asia/Shanghai 的 YYYY-MM-DD。"
        "首次使用先检查会话；导出通过 start_export 启动，再用返回的 job_id 查询状态。"
        "任务执行时建议间隔至少 5 秒查询，避免反复轮询。字幕保存在本地，默认只返回统计和文件位置。"
        "全部课程字幕成功导出后，默认清理更早日期的生成文件；未全部成功时保留旧字幕。"
        "可用 search_subtitles 临时定位关键语句的字幕时间点、行号和回放链接，不永久保存标注。"
        "登录失效时可用 start_login/get_login_status 恢复；导出任务会自动等待登录并继续。"
        "优先复用专用 Edge 会话，确需扫码或验证码时提示用户在本机窗口完成。"
        "不要索取密码、Cookie 或 auth.json 内容。"
    ),
)
jobs = JobStore()
read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)

# MCP configures logging globally; HTTP request URLs may contain login tickets
# or personal identifiers, so do not emit transport-level INFO logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def client():
    return authenticated_client(interactive=False, wait_for_login=False)


def guarded(operation) -> dict:
    try:
        return {"ok": True, **operation()}
    except SessionExpiredError as exc:
        return {"ok": False, "login_required": True, "error": str(exc), "login": login_status()}
    except CourseError as exc:
        return {"ok": False, "error": str(exc)}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"网络请求失败（{type(exc).__name__}），请检查校园网、VPN 或代理。"}
    except Exception as exc:
        return {"ok": False, "error": f"本地工具执行失败（{type(exc).__name__}）。"}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def check_session() -> dict:
    """检查并保存服务器更新的 Cookie；失效时启动静默恢复，返回 ready 和登录状态。"""
    def check():
        status = check_saved_session()
        if not status["ready"]:
            status["login"] = launch_login(interactive=False)
        return status
    return guarded(check)


@mcp.tool(annotations=read_only)
def list_courses(date: str, semester_id: str | None = None) -> dict:
    """列出指定日期的课程及回放状态。date 必须为 YYYY-MM-DD；不下载字幕。"""
    def listing():
        target = CalendarDate.fromisoformat(validate_date(date))
        if semester_id is not None and not semester_id.isdigit():
            raise CourseError("学期编号必须是数字。")
        with client() as connection:
            result = list_courses_for_date(connection, target, semester_id)
        result["total_records"] = len(result["courses"])
        result["total_replays"] = sum(c["replay_available"] for c in result["courses"])
        return result
    return guarded(listing)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True))
def start_export(date: str, semester_id: str | None = None, overwrite: bool = False, keep_history: bool = False) -> dict:
    """后台导出某天字幕并返回 job_id。全部成功后默认删除更早日期的生成文件；keep_history=True 可保留。"""
    def start():
        return jobs.start(date, semester_id, overwrite, keep_history)
    return guarded(start)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_export_status(job_id: str) -> dict:
    """查询 start_export 返回的任务编号，取得状态、进度、错误和本地文件位置。"""
    return guarded(lambda: jobs.summary(job_id))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def search_subtitles(query: str, date: str | None = None, limit: int = 10, context: int = 1) -> dict:
    """临时定位关键句：返回课程、字幕时间点、上下文、文件行号和回放链接。默认最近一次导出；不保存长期标注。"""
    return guarded(lambda: search_local_subtitles(query, date, limit, context))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def start_login(force_refresh: bool = False, interactive: bool = True) -> dict:
    """后台恢复超星登录并立即返回。先复用专用 Edge 会话；确需扫码或验证时打开本机窗口。"""
    return guarded(lambda: launch_login(force_refresh=force_refresh, interactive=interactive))


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
def get_login_status() -> dict:
    """查询后台登录恢复进度；awaiting_user 时请用户在本机 Edge 完成验证。"""
    return guarded(login_status)


if __name__ == "__main__":
    # stdout must contain MCP protocol data only. Export workers log separately.
    mcp.run(transport="stdio")
