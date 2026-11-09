"""List and export a student's Chaoxing replay subtitles for a calendar date."""

import argparse
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import httpx

from download_live import (
    BASE, SCHEDULE_URL, CourseError, NoSubtitleError, download_live, get_page,
)
from export_storage import checked_directory, checked_output_file, cleanup_previous_exports, export_guard
from project_core import CHINA
from subtitle_format import parse_vtt, plain_text


FRONT_LIVE = "https://newesxidian.chaoxing.com/frontLive"


class SemesterOptions(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_select = False
        self.selected = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select":
            self.in_select = attrs.get("id") == "semesterList"
        elif tag == "option" and self.in_select and "selected" in attrs:
            self.selected = attrs.get("value")

    def handle_endtag(self, tag):
        if tag == "select":
            self.in_select = False


def parse_schedule(source: str) -> dict:
    """Read the literal parameters from the school's observed page functions."""
    options = SemesterOptions()
    options.feed(source)
    week_match = re.search(r"getWeekDetail\(\s*['\"]?(\d+)['\"]?\s*\)", source)
    weeks = {int(w) for w in re.findall(r"changeWeek\(\s*(\d+)\s*\)", source)}
    data_match = re.search(
        r"function\s+changeWeek\b.*?data\s*:\s*(\{[^}]+\})", source, re.S
    )
    if not options.selected or not week_match or not weeks or not data_match:
        raise CourseError("课表页面结构无法识别，可能需要重新登录。")
    params = dict(re.findall(
        r"[\"'](\w+)[\"']\s*:\s*[\"']([^\"']*)[\"']", data_match.group(1)
    ))
    if not {"fid", "userId", "termYear", "termId", "type"}.issubset(params):
        raise CourseError("未能从课表读取完整的查询参数。")
    hidden = set(re.findall(
        r"if\s*\(\s*cs\.scheduleId\s*==\s*(\d+)\s*\)\s*\{\s*\}\s*else",
        source,
    ))
    return {
        "semester_id": options.selected,
        "current_week": int(week_match.group(1)),
        "weeks": weeks,
        "params": params,
        "hidden_schedule_ids": hidden,
    }


def request_json(client: httpx.Client, endpoint: str, params):
    response = get_page(client, f"{FRONT_LIVE}/{endpoint}", params)
    try:
        return response.json()
    except ValueError as exc:
        raise CourseError("课表接口没有返回 JSON，请检查登录态。") from exc


def request_week(client: httpx.Client, config: dict, week: int) -> list[dict]:
    records = request_json(
        client, "listStudentCourseLivePage", {**config["params"], "week": week}
    )
    if not isinstance(records, list) or not all(isinstance(c, dict) for c in records):
        raise CourseError("课程列表格式已变化，无法确认是否取得全部课程。")
    return records


def start_datetime(record: dict) -> datetime:
    """The observed API supplies Java Timestamp objects with epoch milliseconds."""
    try:
        start = record["startTime"]
        if not isinstance(start, dict):
            raise ValueError("unexpected timestamp representation")
        return datetime.fromtimestamp(int(start["time"]) / 1000, CHINA)
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise CourseError("课程缺少有效的上课时间，无法安全地按日期筛选。") from exc


def week_monday(records: list[dict]) -> date:
    mondays = set()
    for record in records:
        day = start_datetime(record).date()
        mondays.add(day - timedelta(days=day.weekday()))
    if len(mondays) != 1:
        raise CourseError("课程日期不属于同一周，无法确认课表周次。")
    return mondays.pop()


def target_week(target: date, anchor_monday: date, anchor_week: int) -> int:
    monday = target - timedelta(days=target.weekday())
    return anchor_week + (monday - anchor_monday).days // 7


def filter_courses(records: list[dict], target: date, hidden_ids=()) -> list[dict]:
    courses = {}
    for record in records:
        start = start_datetime(record)
        if start.date() != target:
            continue
        live_id = str(record.get("id", ""))
        if not live_id.isdigit():
            raise CourseError("课程缺少有效的回放编号，无法确认课程完整性。")
        try:
            status = int(record["status"])
            section = int(record["jie"])
        except (KeyError, ValueError, TypeError) as exc:
            raise CourseError("课程缺少有效的状态或节次。") from exc
        visible = str(record.get("scheduleId")) not in hidden_ids
        course = {
            "live_id": live_id,
            "course_name": str(record.get("courseName") or "未命名课程"),
            "place": str(record.get("place") or ""),
            "section": section,
            "start_time": start.isoformat(),
            "replay_available": status == 2 and visible,
            "course_status": status,
        }
        if live_id in courses and courses[live_id] != course:
            raise CourseError("同一个回放编号出现矛盾信息，请重新查询课表。")
        courses[live_id] = course
    return sorted(courses.values(), key=lambda c: (c["start_time"], int(c["live_id"])))


def list_courses_for_date(
    client: httpx.Client, target: date, semester_id: str | None = None
) -> dict:
    page = get_page(
        client, SCHEDULE_URL, {"semesterId": semester_id} if semester_id else None
    )
    config = parse_schedule(page.text)
    if semester_id and config["semester_id"] != semester_id:
        raise CourseError("网页没有选中指定学期，请检查 semester-id。")
    anchor_week = config["current_week"]
    records = request_week(client, config, anchor_week)
    # An empty current week has no full calendar dates. Find another populated
    # week in this semester rather than guessing the year from MM-DD labels.
    for week in sorted(config["weeks"]):
        if records:
            break
        if week != anchor_week:
            anchor_week = week
            records = request_week(client, config, week)
    if not records:
        raise CourseError("本学期没有可用于确认日期的课程。")
    week = target_week(target, week_monday(records), anchor_week)
    if week not in config["weeks"]:
        raise CourseError("日期不在当前学期的课表周次内，可用 --semester-id 指定其他学期。")
    detail = request_json(
        client, "getWeekDetail", {"week": week, "semesterId": config["semester_id"]}
    )
    try:
        month_day = tuple(int(n) for n in detail[f"date{target.isoweekday()}"].split("-"))
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise CourseError("无法读取课表日期，请检查接口是否变化。") from exc
    if month_day != (target.month, target.day):
        raise CourseError("课表日期与目标日期不一致，已停止导出。")
    if week != anchor_week:
        records = request_week(client, config, week)
    if records and week_monday(records) != target - timedelta(days=target.weekday()):
        raise CourseError("课程实际日期与目标周不一致，已停止导出。")
    return {
        "date": target.isoformat(),
        "semester_id": config["semester_id"],
        "week": week,
        "courses": filter_courses(records, target, config["hidden_schedule_ids"]),
    }


def vtt_to_text(source: str) -> str:
    return plain_text(parse_vtt(source))


def safe_directory(course: dict) -> str:
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", course["course_name"])
    title = title.rstrip(" .")[:60] or "未命名课程"
    return f"第{course['section']:02d}节_{title}"


def write_report(path: Path, report: dict):
    report["counts"] = dict(Counter(c["result"] for c in report["courses"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = checked_output_file(path.parent, path.with_suffix(".json.part").name)
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def export_date(
    client: httpx.Client, listing: dict, output_root: Path, overwrite: bool = False,
    keep_history: bool = False,
    *, job_id: str | None = None, progress=None,
) -> dict:
    # CLI and MCP workers share this lock so cleanup cannot remove another
    # export's output while that process is still writing it.
    with export_guard(output_root):
        return _export_date(client, listing, output_root, overwrite, keep_history, job_id, progress)


def _export_date(client, listing, output_root, overwrite, keep_history, job_id, progress):
    output_dir = checked_directory(output_root, listing["date"])
    report = {
        **listing,
        "started_at": datetime.now(CHINA).isoformat(),
        "courses": [
            {**course, "result": "pending" if course["replay_available"] else "unavailable"}
            for course in listing["courses"]
        ],
    }
    report_path = output_dir / "manifest.json"
    if job_id:
        report["job_id"] = job_id
    def save():
        write_report(report_path, report)
        if progress:
            progress(report)
    save()
    eligible = [c for c in report["courses"] if c["replay_available"]]
    for index, course in enumerate(eligible, 1):
        print(f"\n[{index}/{len(eligible)}] 第{course['section']}节 {course['course_name']}", flush=True)
        try:
            files = download_live(
                client, course["live_id"], output_dir / safe_directory(course), overwrite
            )
            course["files"] = []
            for file in files:
                txt = checked_output_file(file.parent, file.with_suffix(".txt").name)
                txt.write_text(vtt_to_text(file.read_text(encoding="utf-8-sig")), encoding="utf-8")
                course["files"].append({"vtt": str(file.resolve()), "txt": str(txt.resolve())})
            course["result"] = "complete"
        except NoSubtitleError as exc:
            course.update(result="no_subtitle", error=str(exc))
            print(f"无字幕：{exc}", flush=True)
        except CourseError as exc:
            course.update(result="failed", error=str(exc))
            print(f"失败：{exc}", flush=True)
        except httpx.HTTPError as exc:
            course.update(result="failed", error=f"网络错误：{type(exc).__name__}")
            print(course["error"], flush=True)
        except (OSError, UnicodeError):
            course.update(result="failed", error="文件读取或保存失败。")
            print(course["error"], flush=True)
        save()
    report["finished_at"] = datetime.now(CHINA).isoformat()
    if keep_history:
        report["cleanup"] = {"status": "disabled", "reason": "本次保留历史字幕。", "removed_dates": []}
    else:
        try:
            report["cleanup"] = cleanup_previous_exports(output_root, report)
        except (OSError, UnicodeError, CourseError):
            report["cleanup"] = {"status": "failed", "reason": "旧字幕清理未完成，请检查输出目录。", "removed_dates": []}
    save()
    cleanup = report["cleanup"]
    print(f"历史清理：{cleanup['status']}，删除日期：{cleanup['removed_dates']}", flush=True)
    print(f"\n结果：{report['counts']}\n清单：{report_path}", flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="按指定日期列出或下载超星课程回放字幕。")
    parser.add_argument("date", type=date.fromisoformat, help="YYYY-MM-DD，例如 2026-10-26")
    parser.add_argument("--list-only", action="store_true", help="只查看当天课程，不下载字幕")
    parser.add_argument("--semester-id", help="可选：指定课表中的学期编号")
    parser.add_argument("--auth", type=Path, default=BASE / ".local/auth.json")
    parser.add_argument("--output", type=Path, default=BASE / "subtitles")
    parser.add_argument("--overwrite", action="store_true", help="重新下载已存在的字幕")
    parser.add_argument("--keep-history", action="store_true", help="本次保留旧日期字幕；默认全部成功后清理更早日期")
    args = parser.parse_args()
    try:
        print(f"查询 {args.date.isoformat()} 的课表……", flush=True)
        from session_manager import authenticated_client
        with authenticated_client(auth=args.auth) as client:
            listing = list_courses_for_date(client, args.date, args.semester_id)
            courses = listing["courses"]
            print(
                f"第{listing['week']}周，共{len(courses)}条课程记录，"
                f"其中{sum(c['replay_available'] for c in courses)}条有回放。",
                flush=True,
            )
            for course in courses:
                state = "可下载" if course["replay_available"] else "暂无回放"
                print(
                    f"第{course['section']}节 {course['course_name']} "
                    f"liveId={course['live_id']} {state}", flush=True,
                )
            if args.list_only:
                return 0
            report = export_date(client, listing, args.output, args.overwrite, args.keep_history)
            if report["counts"].get("failed", 0):
                return 2
            return 3 if report["cleanup"]["status"] == "failed" else 0
    except CourseError as exc:
        print(f"失败：{exc}", flush=True)
    except httpx.HTTPError as exc:
        print(f"网络请求失败（{type(exc).__name__}），请检查校园网、VPN 或代理。", flush=True)
    except (OSError, UnicodeError):
        print("文件读取或保存失败，请检查文件编码及目录权限。", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
