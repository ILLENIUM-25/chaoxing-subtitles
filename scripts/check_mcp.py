"""Protocol smoke test. Add --date YYYY-MM-DD for a real local export test."""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

# Allow direct execution from scripts/ while keeping production imports at root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from download_live import BASE


def payload(result):
    if result.isError:
        raise RuntimeError("MCP tool returned a protocol error")
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(next(c.text for c in result.content if c.type == "text"))


async def verify(target=None):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-X", "utf8", "-u", str(BASE / "server.py")],
        cwd=str(BASE), env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=60)) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            expected = {"check_session", "list_courses", "start_export", "get_export_status", "search_subtitles", "start_login", "get_login_status"}
            assert names == expected, names
            print("MCP connected. Tools:", ", ".join(sorted(names)), flush=True)
            invalid = payload(await session.call_tool("get_export_status", {"job_id": "../auth.json"}))
            assert invalid["ok"] is False
            print("Invalid job path rejected.", flush=True)
            if target is None:
                return
            ready = payload(await session.call_tool("check_session", {}))
            assert ready.get("ready") is True, ready
            print("Session ready.", flush=True)
            courses = payload(await session.call_tool("list_courses", {"date": target}))
            assert courses["ok"] is True, courses
            print("Courses:", courses["total_records"], "replays:", courses["total_replays"], flush=True)
            job = payload(await session.call_tool("start_export", {"date": target}))
            assert job["ok"] is True, job
            print("Job:", job["job_id"], flush=True)
            deadline = time.monotonic() + 300
            previous = None
            while time.monotonic() < deadline:
                state = payload(await session.call_tool("get_export_status", {"job_id": job["job_id"]}))
                assert state["ok"] is True, state
                progress = (state["status"], state.get("phase"), json.dumps(state.get("counts", {}), sort_keys=True))
                if progress != previous:
                    print("Progress:", progress, flush=True)
                    previous = progress
                if state["status"] not in {"queued", "running"}:
                    assert state["status"] == "complete", state
                    print("Export complete. Manifest:", state["manifest"], flush=True)
                    print("Cleanup:", json.dumps(state.get("cleanup"), ensure_ascii=False), flush=True)
                    located = payload(await session.call_tool("search_subtitles", {"query": "安全", "date": target, "limit": 1, "context": 0}))
                    assert located["ok"] is True, located
                    if located["matches"]:
                        first = located["matches"][0]
                        assert first["vtt_line"] > 0 and first["start_seconds"] >= 0
                        assert "liveId=" in first["replay_url"]
                    print("Phrase location tool passed. Matches:", located["total_matches"], flush=True)
                    return
                await asyncio.sleep(5)
            raise TimeoutError("Export test did not finish within five minutes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Optional: test a real export through MCP")
    args = parser.parse_args()
    asyncio.run(verify(args.date))
