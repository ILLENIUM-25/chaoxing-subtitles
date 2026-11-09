"""Shared configuration and errors, independent of CLI/module import identity."""

from datetime import timedelta, timezone
from pathlib import Path


BASE = Path(__file__).resolve().parent
CHINA = timezone(timedelta(hours=8))
SCHEDULE_URL = "https://newesxidian.chaoxing.com/frontLive/studentSelectCourse1"
REPLAY_URL = "https://newesxidian.chaoxing.com/live/viewNewCourseLive1"
SUBTITLE_URL = "http://newes.chaoxing.com/xidianpj/smartSupervisor/getVideoSubtitles2Vtt"


class CourseError(Exception):
    """A safe user-facing error without credential-bearing request details."""


class NoSubtitleError(CourseError):
    """A valid VTT document contains no subtitle cues."""


class SessionExpiredError(CourseError):
    """The site explicitly requires authentication again."""
