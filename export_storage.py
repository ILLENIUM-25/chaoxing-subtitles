"""Serialize output changes and remove only verified older generated exports."""

import contextlib
import hashlib
import json
import os
import re
import shutil
import time
from datetime import date
from pathlib import Path

from project_core import BASE, CourseError
from subtitle_format import valid_vtt


def is_link(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & 0x400)  # Windows reparse point


@contextlib.contextmanager
def file_guard(path: Path, timeout: float = 3):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise CourseError("输出目录或任务正在使用，请稍后重试。")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def export_guard(output_root: Path, timeout: float = 600):
    key = hashlib.sha256(os.path.normcase(str(output_root.resolve())).encode()).hexdigest()[:24]
    with file_guard(BASE / ".local/export_locks" / f"{key}.lock", timeout=timeout):
        yield


def checked_directory(output_root: Path, target: str) -> Path:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target):
        raise CourseError("输出日期必须是 YYYY-MM-DD。")
    try:
        date.fromisoformat(target)
    except ValueError as exc:
        raise CourseError("输出日期不存在。") from exc
    root = output_root.absolute()
    if root.exists() and is_link(root):
        raise CourseError("字幕输出目录不能是符号链接或目录联接。")
    root = root.resolve()
    candidate = root / target
    if candidate.exists() and is_link(candidate):
        raise CourseError("日期目录不能是符号链接或目录联接。")
    if candidate.resolve().parent != root:
        raise CourseError("日期目录超出了字幕输出目录。")
    return candidate


def checked_output_file(directory: Path, name: str) -> Path:
    """Reject linked ancestors and linked destination/temporary files before I/O."""
    if Path(name).name != name or name in {"", ".", ".."}:
        raise CourseError("输出文件名无效。")
    directory = directory.absolute()
    for ancestor in (directory, *directory.parents):
        if (ancestor.exists() or ancestor.is_symlink()) and is_link(ancestor):
            raise CourseError("输出路径包含符号链接或目录联接，已停止写入。")
    target = directory / name
    if (target.exists() or target.is_symlink()) and is_link(target):
        raise CourseError("输出文件不能是符号链接或目录联接。")
    if target.resolve().parent != directory.resolve():
        raise CourseError("输出文件超出了指定目录。")
    return target


def cleanup_previous_exports(output_root: Path, report: dict) -> dict:
    result = {"status": "skipped", "removed_dates": [], "removed_legacy_files": [], "protected": [], "errors": []}
    fingerprints = set()
    courses = report.get("courses", [])
    if not courses or any(c.get("result") != "complete" for c in courses):
        return {**result, "reason": "本次课程未全部成功导出，保留历史字幕。"}
    current = checked_directory(output_root, report["date"])
    root = current.parent
    # Check the newly produced files before removing any previous output.
    for course in courses:
        if not course.get("files"):
            return {**result, "reason": "本次缺少已验证的字幕文件，保留历史字幕。"}
        for pair in course["files"]:
            for extension in ("vtt", "txt"):
                path = Path(pair.get(extension, ""))
                if (
                    not path.is_file() or is_link(path)
                    or not path.resolve().is_relative_to(current)
                    or path.suffix != f".{extension}"
                ):
                    return {**result, "reason": "本次字幕文件校验未通过，保留历史字幕。"}
                # Check each parent as well, to reject junctions inside a date directory.
                if any(is_link(parent) for parent in path.parents if parent != root and parent.is_relative_to(current)):
                    return {**result, "reason": "本次字幕路径包含链接，保留历史字幕。"}
                text = path.read_text(encoding="utf-8-sig").strip()
                if not text or (extension == "vtt" and not valid_vtt(text)):
                    return {**result, "reason": "本次字幕内容校验未通过，保留历史字幕。"}
                fingerprints.add((extension, hashlib.sha256(text.encode("utf-8")).hexdigest()))
    for candidate in sorted(root.iterdir()):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate.name):
            continue
        try:
            older = date.fromisoformat(candidate.name) < date.fromisoformat(report["date"])
        except ValueError:
            continue
        if not older:
            continue
        try:
            if not _generated_tree(root, candidate):
                result["protected"].append(candidate.name)
                continue
            # Both the resolved absolute boundary and every descendant were checked.
            # Repeat the boundary check immediately before recursive deletion.
            if is_link(candidate) or candidate.resolve().parent != root:
                result["protected"].append(candidate.name)
                continue
            for path in candidate.rglob("*"):
                if path.is_file() and path.suffix in {".vtt", ".txt"}:
                    text = path.read_text(encoding="utf-8-sig").strip()
                    fingerprints.add((path.suffix[1:], hashlib.sha256(text.encode("utf-8")).hexdigest()))
            shutil.rmtree(candidate)
            result["removed_dates"].append(candidate.name)
        except (OSError, ValueError, TypeError):
            result["errors"].append(candidate.name)
    # The first two sample scripts wrote numeric filenames directly in the
    # output root. Remove these only when their contents duplicate a verified
    # generated subtitle; leave all unclassified user files untouched.
    for path in sorted(root.iterdir()):
        if not re.fullmatch(r"\d+(?:_\d+)?\.(?:vtt|txt)", path.name):
            continue
        try:
            if not path.is_file() or is_link(path) or path.resolve().parent != root:
                continue
            text = path.read_text(encoding="utf-8-sig").strip()
            fingerprint = (path.suffix[1:], hashlib.sha256(text.encode("utf-8")).hexdigest())
            if fingerprint in fingerprints:
                path.unlink()
                result["removed_legacy_files"].append(path.name)
        except (OSError, UnicodeError):
            result["errors"].append(path.name)
    result["status"] = "failed" if result["errors"] else "complete"
    return result


def _generated_tree(root: Path, candidate: Path) -> bool:
    if not candidate.is_dir() or is_link(candidate) or candidate.resolve().parent != root:
        return False
    manifest = candidate / "manifest.json"
    if not manifest.is_file() or is_link(manifest):
        return False
    old = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(old, dict) or old.get("date") != candidate.name or not isinstance(old.get("courses"), list) or not old.get("started_at"):
        return False
    def walk_error(error):
        raise error
    for directory, folders, files in os.walk(candidate, followlinks=False, onerror=walk_error):
        for name in folders + files:
            path = Path(directory) / name
            if is_link(path) or not path.resolve().is_relative_to(candidate):
                return False
            if path.is_dir():
                if not re.match(r"第\d+节_", name):
                    return False
            elif path == manifest or (path.parent == candidate and name == "manifest.json.part"):
                continue
            elif not re.fullmatch(r"\d+_\d+\.(?:vtt|txt)(?:\.part)?", name):
                # Preserve the entire directory when it also contains user-added files.
                return False
    return True
