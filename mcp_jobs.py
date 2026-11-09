"""Persistent local export jobs launched independently of the MCP connection."""

import contextlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

from project_core import CHINA, BASE, CourseError
from export_storage import file_guard


ACTIVE = {"queued", "running"}


def now() -> str:
    return datetime.now(CHINA).isoformat()


def validate_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise CourseError("日期格式必须是 YYYY-MM-DD。")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise CourseError("日期不存在，请检查年月日。") from exc


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def process_signature(pid: int) -> tuple[bool | None, float | None]:
    """Check Windows processes without using os.kill, which can terminate them."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True, None
        except ProcessLookupError:
            return False, None
        except PermissionError:
            return None, None
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return (False, None) if ctypes.get_last_error() == 87 else (None, None)
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None, None
        if code.value != 259:  # STILL_ACTIVE
            return False, None
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(system), ctypes.byref(user),
        ):
            return True, None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return True, ticks / 10_000_000 - 11_644_473_600
    finally:
        kernel.CloseHandle(handle)


@contextlib.contextmanager
def date_guard(path: Path):
    """Serialize job startup for a date, including across multiple MCP processes."""
    with file_guard(path):
        yield


class JobStore:
    def __init__(self, root: Path = BASE):
        self.root = root.resolve()
        self.jobs = self.root / ".local/jobs"
        self.locks = self.root / ".local/job_locks"

    def directory(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise CourseError("任务编号无效，请使用 start_export 返回的 job_id。")
        return self.jobs / job_id

    def state(self, job_id: str) -> dict:
        try:
            return read_json(self.directory(job_id) / "state.json")
        except (OSError, ValueError) as exc:
            raise CourseError("没有找到该任务的有效状态。") from exc

    def save(self, job_id: str, state: dict):
        write_json(self.directory(job_id) / "state.json", state)

    def refresh(self, job_id: str) -> dict:
        state = self.state(job_id)
        if state["status"] not in ACTIVE:
            return state
        pid_path = self.directory(job_id) / "pid.json"
        dead = False
        if pid_path.is_file():
            pid = read_json(pid_path)
            alive, created = process_signature(pid["pid"])
            dead = alive is False
            if created is not None and pid.get("created") is not None:
                dead = dead or abs(created - pid["created"]) > 0.1
        else:
            elapsed = (datetime.now(CHINA) - datetime.fromisoformat(state["queued_at"])).total_seconds()
            dead = elapsed > 60
        if dead:
            # The process may have saved its final state since our first read.
            state = self.state(job_id)
            if state["status"] in ACTIVE:
                state.update(
                    status="interrupted", finished_at=now(),
                    error="后台进程已退出，任务未完成；可以重新发起导出。",
                )
                self.save(job_id, state)
        return state

    def summary(self, job_id: str) -> dict:
        state = self.refresh(job_id)
        result = {key: state[key] for key in (
            "job_id", "date", "semester_id", "status", "phase", "queued_at",
            "started_at", "finished_at", "total_records", "total_replays", "error",
            "login",
            "counts", "cleanup", "errors",
        ) if key in state}
        output_dir = self.root / "subtitles" / state["date"]
        manifest = output_dir / "manifest.json"
        result.update(output_dir=str(output_dir), manifest=str(manifest), output_available=False)
        if manifest.is_file() and state.get("started_at"):
            try:
                report = read_json(manifest)
                # A later export of the same date must never replace this job's
                # progress. Each worker now tags its report with the exact ID.
                current = (
                    report.get("date") == state["date"]
                    and report.get("job_id") == job_id
                )
                if current:
                    result["output_available"] = bool(report.get("finished_at"))
                    result["counts"] = report.get("counts", {})
                    result["cleanup"] = report.get("cleanup")
                    result["errors"] = [
                        {key: course[key] for key in ("live_id", "course_name", "result", "error") if key in course}
                        for course in report.get("courses", [])
                        if course.get("result") in {"failed", "no_subtitle"}
                    ]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return result

    def start(self, target: str, semester_id: str | None = None, overwrite: bool = False, keep_history: bool = False) -> dict:
        target = validate_date(target)
        if semester_id is not None and not semester_id.isdigit():
            raise CourseError("学期编号必须是数字。")
        with date_guard(self.locks / f"{target}.lock"):
            pointer = self.locks / f"{target}.json"
            if pointer.is_file():
                previous = read_json(pointer)["job_id"]
                state = self.refresh(previous)
                if state["status"] in ACTIVE:
                    if state.get("semester_id") != semester_id:
                        raise CourseError("同一天的其他学期任务正在执行，请等该任务结束。")
                    if state.get("keep_history", False) != keep_history or state.get("overwrite", False) != overwrite:
                        raise CourseError("同一天的任务正在使用其他导出或清理参数，请等该任务结束。")
                    return {**self.summary(previous), "reused": True}
            job_id = uuid.uuid4().hex
            directory = self.directory(job_id)
            request = {
                "job_id": job_id, "date": target, "semester_id": semester_id,
                "overwrite": overwrite, "keep_history": keep_history, "queued_at": now(),
            }
            write_json(directory / "request.json", request)
            self.save(job_id, {**request, "status": "queued", "phase": "queued"})
            write_json(pointer, {"job_id": job_id})
            environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            try:
                with (directory / "worker.log").open("wb") as log:
                    process = subprocess.Popen(
                        [sys.executable, "-X", "utf8", "-u", str(self.root / "export_worker.py"), job_id],
                        cwd=str(self.root), stdin=subprocess.DEVNULL,
                        stdout=log, stderr=subprocess.STDOUT, env=environment,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                        start_new_session=os.name != "nt",
                    )
                _, created = process_signature(process.pid)
                write_json(directory / "pid.json", {"pid": process.pid, "created": created})
            except OSError as exc:
                state = {**request, "status": "failed", "phase": "startup", "error": "无法启动后台导出进程。", "finished_at": now()}
                self.save(job_id, state)
                raise CourseError(state["error"]) from exc
            return {**self.summary(job_id), "reused": False}
