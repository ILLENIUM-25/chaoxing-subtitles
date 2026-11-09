"""One VTT parser shared by downloads, cleanup, text export and phrase location."""

import html
import re
from dataclasses import dataclass

from project_core import CourseError


TIMING = re.compile(
    r"(?P<start>(?:\d{2,}:)?[0-5]\d:[0-5]\d\.\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{2,}:)?[0-5]\d:[0-5]\d\.\d{3})(?:\s+.*)?$"
)


@dataclass
class Cue:
    start: str
    end: str
    text: str
    source_lines: list[int]
    txt_line: int


def seconds(stamp: str) -> float:
    return sum(float(value) * 60 ** index for index, value in enumerate(reversed(stamp.split(":"))))


def parse_vtt(source: str) -> list[Cue]:
    lines = source.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").splitlines()
    if not lines or not re.fullmatch(r"WEBVTT(?:[ \t].*)?", lines[0]):
        raise CourseError("文件不是有效的 VTT 字幕。")
    cues = []
    start = 0
    txt_line = 1
    for stop in range(len(lines) + 1):
        if stop < len(lines) and lines[stop].strip():
            continue
        block = lines[start:stop]
        first_line = start + 1
        start = stop + 1
        if not block:
            continue
        first = block[0]
        if first_line == 1 or first in {"NOTE", "STYLE", "REGION"} or first.startswith(("NOTE ", "NOTE\t")):
            continue
        timing = None
        for index, line in enumerate(block[:2]):
            match = TIMING.fullmatch(line.strip())
            if match:
                timing = index, match
                break
        if timing is None:
            raise CourseError("VTT 字幕包含无法识别的时间轴。")
        index, match = timing
        if seconds(match["end"]) <= seconds(match["start"]):
            raise CourseError("VTT 字幕结束时间必须晚于开始时间。")
        text = ""
        source_lines = []
        for offset, line in enumerate(block[index + 1:], index + 1):
            clean = html.unescape(re.sub(r"<[^>]*>", "", line)).strip()
            if not clean:
                continue
            line_number = first_line + offset
            if text:
                text += "\n"
                source_lines.append(line_number)
            text += clean
            source_lines.extend([line_number] * len(clean))
        if text:
            cues.append(Cue(match["start"], match["end"], text, source_lines, txt_line))
            txt_line += text.count("\n") + 1
    return cues


def plain_text(cues: list[Cue]) -> str:
    return "\n".join(cue.text for cue in cues) + ("\n" if cues else "")


def valid_vtt(source: str) -> bool:
    try:
        return bool(parse_vtt(source))
    except CourseError:
        return False
