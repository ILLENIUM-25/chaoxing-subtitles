"""Background worker. Its stdout goes to a local log, never the MCP wire."""

import sys
from datetime import date

import httpx

from download_date import export_date, list_courses_for_date
from download_live import BASE, CourseError
from mcp_jobs import JobStore, now, read_json
from session_manager import authenticated_client


def run(job_id: str) -> int:
    store = JobStore()
    request = read_json(store.directory(job_id) / "request.json")
    state = {**request, "status": "running", "phase": "querying", "started_at": now()}
    store.save(job_id, state)
    resume_phase = "querying"
    def session_progress(phase, details):
        nonlocal resume_phase
        if phase == "waiting_login":
            if state["phase"] != "waiting_login":
                resume_phase = state["phase"]
            state.update(phase="waiting_login", login=details)
        else:
            state.update(phase=resume_phase)
            state.pop("login", None)
        store.save(job_id, state)
    def export_progress(report):
        state.update(
            counts=dict(report.get("counts", {})),
            errors=[
                {key: course[key] for key in ("live_id", "course_name", "result", "error") if key in course}
                for course in report["courses"]
                if course.get("result") in {"failed", "no_subtitle"}
            ],
        )
        if "cleanup" in report:
            state["cleanup"] = report["cleanup"]
        store.save(job_id, state)
    try:
        with authenticated_client(callback=session_progress) as client:
            listing = list_courses_for_date(client, date.fromisoformat(request["date"]), request["semester_id"])
            state.update(
                phase="exporting", total_records=len(listing["courses"]),
                total_replays=sum(c["replay_available"] for c in listing["courses"]),
            )
            store.save(job_id, state)
            report = export_date(
                client, listing, BASE / "subtitles", request["overwrite"],
                request.get("keep_history", False), job_id=job_id, progress=export_progress,
            )
            export_progress(report)
            failed = report["counts"].get("failed", 0)
            cleanup_failed = report["cleanup"]["status"] == "failed"
            status = "partial_failed" if failed else "cleanup_failed" if cleanup_failed else "complete"
            state.update(status=status, phase="finished", finished_at=now())
            store.save(job_id, state)
            return 2 if failed else 3 if cleanup_failed else 0
    except CourseError as exc:
        error = str(exc)
    except httpx.HTTPError as exc:
        error = f"网络请求失败（{type(exc).__name__}），请检查校园网、VPN 或代理。"
    except Exception as exc:
        # Request exceptions can embed credential-bearing headers. Keep raw
        # exceptions out of protocol results and local logs alike.
        error = f"导出失败（{type(exc).__name__}）。"
    state.update(status="failed", error=error, phase="finished", finished_at=now())
    store.save(job_id, state)
    print(error, flush=True)
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: export_worker.py JOB_ID")
    raise SystemExit(run(sys.argv[1]))
