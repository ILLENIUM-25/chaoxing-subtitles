"""Locate literal phrases in retained VTT files, without creating an archive."""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from download_live import BASE, REPLAY_URL, CourseError
from export_storage import checked_directory, export_guard, is_link
from subtitle_format import Cue, parse_vtt, plain_text, seconds


def normalize(text: str) -> str:
    return "".join(char for char in text.casefold() if char.isalnum())


def phrase_matches(cues: list[Cue], query: str, context: int = 1):
    needle = normalize(query)
    if not needle:
        raise CourseError("请输入包含文字或数字的关键语句。")
    normalized = []
    positions = []
    for cue_index, cue in enumerate(cues):
        for offset, char in enumerate(cue.text):
            for folded in char.casefold():
                if folded.isalnum():
                    normalized.append(folded)
                    positions.append((cue_index, offset))
    haystack = "".join(normalized)
    cursor = 0
    while True:
        found = haystack.find(needle, cursor)
        if found < 0:
            return
        first, begin = positions[found]
        last, end = positions[found + len(needle) - 1]
        quote = "\n".join(
            cue.text[begin if index == first else 0:end + 1 if index == last else len(cue.text)]
            for index, cue in enumerate(cues[first:last + 1], first)
        )
        def surrounding(items):
            return [{"start_time": cue.start, "text": cue.text} for cue in items]
        yield {
            "quote": quote,
            "start_time": cues[first].start,
            "end_time": cues[last].end,
            "start_seconds": seconds(cues[first].start),
            "end_seconds": seconds(cues[last].end),
            "vtt_line": cues[first].source_lines[begin],
            "vtt_end_line": cues[last].source_lines[end],
            "txt_line": cues[first].txt_line + cues[first].text[:begin].count("\n"),
            "txt_end_line": cues[last].txt_line + cues[last].text[:end].count("\n"),
            "context_before": surrounding(cues[max(0, first - context):first]),
            "context_after": surrounding(cues[last + 1:last + 1 + context]),
        }
        cursor = found + 1


def select_export(root: Path, target_date: str | None):
    if target_date is not None:
        directory = checked_directory(root, target_date)
        candidates = [directory]
    else:
        if not root.is_dir():
            raise CourseError("尚无字幕，请先导出课程字幕。")
        candidates = [p for p in root.iterdir() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)]
    available = []
    for candidate in candidates:
        try:
            directory = checked_directory(root, candidate.name)
            manifest = directory / "manifest.json"
            if not manifest.is_file() or is_link(manifest):
                continue
            report = json.loads(manifest.read_text(encoding="utf-8"))
            if report.get("date") == directory.name:
                stamp = datetime.fromisoformat(report["started_at"]).timestamp()
                available.append((stamp, directory, report))
        except (OSError, ValueError, KeyError, TypeError, CourseError):
            continue
    if not available:
        raise CourseError("该日期字幕不存在或已被清理，请先重新导出。")
    _, directory, report = max(available, key=lambda item: item[0])
    return directory, report


def search_subtitles(
    query: str, target_date: str | None = None, limit: int = 10,
    context: int = 1, output_root: Path = BASE / "subtitles",
) -> dict:
    if not 1 <= limit <= 50 or not 0 <= context <= 3:
        raise CourseError("limit 必须为 1–50，context 必须为 0–3。")
    if not query or len(query) > 500 or not normalize(query):
        raise CourseError("请输入 1–500 字的关键语句，且至少包含文字或数字。")
    with export_guard(output_root, timeout=3):
        directory, report = select_export(output_root, target_date)
        results = []
        total = 0
        missing = 0
        invalid = 0
        txt_warnings = []
        seen = set()
        for course in report.get("courses", []):
            for files in course.get("files", []):
                vtt = Path(files.get("vtt", ""))
                if not vtt.is_file():
                    missing += 1
                    continue
                if is_link(vtt) or not vtt.resolve().is_relative_to(directory):
                    raise CourseError("字幕路径超出当前日期目录。")
                if vtt.resolve() in seen:
                    continue
                seen.add(vtt.resolve())
                try:
                    cues = parse_vtt(vtt.read_text(encoding="utf-8-sig"))
                except (CourseError, OSError, UnicodeError):
                    invalid += 1
                    continue
                if not cues:
                    invalid += 1
                    continue
                txt = vtt.with_suffix(".txt")
                txt_valid = False
                try:
                    txt_valid = (
                        txt.is_file() and not is_link(txt)
                        and txt.resolve().is_relative_to(directory)
                        and txt.read_text(encoding="utf-8-sig") == plain_text(cues)
                    )
                except (OSError, UnicodeError):
                    pass
                if not txt_valid:
                    txt_warnings.append(txt.name)
                for match in phrase_matches(cues, query, context):
                    total += 1
                    if len(results) >= limit:
                        continue
                    live_id = course["live_id"]
                    match.update(
                        course_name=course["course_name"], section=course["section"],
                        live_id=live_id, date=report["date"],
                        replay_url=f"{REPLAY_URL}?{urlencode({'isStudent': '1', 'liveId': live_id})}",
                        vtt_file=str(vtt.resolve()),
                        txt_file=str(txt.resolve()) if txt_valid else None,
                    )
                    if not txt_valid:
                        match.update(txt_line=None, txt_end_line=None)
                    results.append(match)
        return {
            "date": report["date"], "query": query, "total_matches": total,
            "matches": results, "truncated": total > len(results),
            "missing_files": missing,
            "invalid_files": invalid,
            "text_mapping_warnings": txt_warnings,
            "match_mode": "忽略标点、空白和英文大小写的文字匹配，支持跨字幕片段。",
            "position_accuracy": "时间点对应字幕片段；回放链接打开页面后按该时间定位。",
            "temporary": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="搜索关键语句，返回视频时间点、上下文及字幕行号。")
    parser.add_argument("query")
    parser.add_argument("--date", help="YYYY-MM-DD；默认最近一次导出的日期")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--context", type=int, default=1)
    args = parser.parse_args()
    try:
        print(json.dumps(search_subtitles(args.query, args.date, args.limit, args.context), ensure_ascii=False, indent=2))
        return 0
    except CourseError as exc:
        print(f"定位失败：{exc}")
    except (OSError, UnicodeError, ValueError, KeyError):
        print("定位失败：字幕文件或清单无法读取，请重新导出。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
