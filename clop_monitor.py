#!/usr/bin/env python3
"""Read-only notification monitor for the hosted CLOP game."""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


DEFAULT_BASE_URL = "https://4clop.org/"
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_STATE_PATH = Path(__file__).resolve().parent / ".state" / "clop-monitor.json"
DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"
DEFAULT_WAV_PATH = Path(__file__).resolve().parent / "sounds" / "twilight-clock-is-ticking.wav"
DEFAULT_ENV_PATH = Path(__file__).resolve().parent / ".env"
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
COUNT_RE = re.compile(r"\(\s*(\d+)\s*\)")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MonitorError(RuntimeError):
    """A recoverable monitoring error."""


class AuthenticationError(MonitorError):
    """The supplied credentials were rejected or the login response changed."""


class ArchivedThreadError(MonitorError):
    """The configured 4chan thread can no longer receive new posts."""


@dataclass(frozen=True)
class WatchedGood:
    """One good's buyer's-market watch.

    ``friends`` and ``alliance`` are independent because the game renders only the
    higher-precedence colour, so a buyer who is both must satisfy either check on its own.
    ``always`` and ``never`` are nation-name patterns that override both.
    """

    name: str
    friends: bool = True
    alliance: bool = True
    always: Tuple[str, ...] = ()
    never: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AlertCategorySettings:
    user_messages: bool = True
    alliance_messages: bool = True
    news: bool = True
    reports: bool = True
    #: Patterns from reports.ignore; a report matching one of them raises no alert.
    report_ignore: Tuple[str, ...] = ()
    market_orders: bool = True
    #: Goods from market.goods that are switched on, in the order the file lists them.
    market_goods: Tuple[WatchedGood, ...] = ()


@dataclass(frozen=True)
class SoundSettings:
    wav_path: Optional[Path] = None
    loop_while_popup_open: bool = False
    repeat_interval_seconds: float = 10.0


@dataclass(frozen=True)
class CacheSettings:
    persist_to_file: bool = True


@dataclass(frozen=True)
class FourChanThreadSettings:
    page_url: str
    api_url: str
    board: str
    thread_id: int


@dataclass(frozen=True)
class MonitorSettings:
    alerts: AlertCategorySettings = AlertCategorySettings()
    sound: SoundSettings = SoundSettings()
    cache: CacheSettings = CacheSettings()
    fourchan_thread: Optional[FourChanThreadSettings] = None
    #: Dotted names of the settings that the file left out, in the order they are documented.
    defaults_used: Tuple[str, ...] = ()
    file_found: bool = True


def load_env_file(path: Path) -> Dict[str, str]:
    """Read a simple KEY=VALUE file without changing the process environment."""
    if not path.exists():
        return {}
    if not path.is_file():
        raise MonitorError(f"Environment file is not a file: {path}")

    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        raise MonitorError(f"Could not read environment file {path}: {error}") from error

    values: Dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            raise MonitorError(
                f"Invalid environment entry at {path}:{line_number}; expected NAME=VALUE"
            )
        name, value = stripped.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not ENV_NAME_RE.fullmatch(name):
            raise MonitorError(f"Invalid environment name at {path}:{line_number}: {name!r}")
        if value[:1] in {"'", '"'}:
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise MonitorError(f"Unclosed quoted value at {path}:{line_number}")
            value = value[1:-1]
        values[name] = value
    return values


def parse_fourchan_thread_url(url: str) -> FourChanThreadSettings:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "boards.4chan.org",
        "boards.4channel.org",
    }:
        raise MonitorError("4chan thread_url must be an HTTPS boards.4chan.org thread URL")
    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        len(path_parts) < 3
        or path_parts[1] != "thread"
        or not re.fullmatch(r"[a-z0-9]+", path_parts[0])
        or not path_parts[2].isdigit()
    ):
        raise MonitorError("4chan thread_url must have the form /<board>/thread/<number>")
    board = path_parts[0]
    thread_id = int(path_parts[2])
    page_url = f"https://boards.4chan.org/{board}/thread/{thread_id}"
    api_url = f"https://a.4cdn.org/{board}/thread/{thread_id}.json"
    return FourChanThreadSettings(page_url, api_url, board, thread_id)


def load_settings(path: Path) -> MonitorSettings:
    """Read the optional settings file; every absent key falls back to a built-in default.

    The file is a private, git-ignored override rather than part of the shipped code, so a
    clone that has never been configured still runs. Absent keys are recorded in
    ``defaults_used`` so the monitor can say what it filled in.
    """
    file_found = True
    value: object = {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        file_found = False
    except (OSError, ValueError) as error:
        raise MonitorError(f"Could not read settings file {path}: {error}") from error
    if not isinstance(value, dict):
        raise MonitorError("Settings must contain a JSON object")

    alerts_value = value.get("alerts", {})
    sound_value = value.get("sound", {})
    cache_value = value.get("cache", {})
    if (
        not isinstance(alerts_value, dict)
        or not isinstance(sound_value, dict)
        or not isinstance(cache_value, dict)
    ):
        raise MonitorError("The alerts, sound, and cache settings must be JSON objects")

    defaults_used: List[str] = []

    def boolean_setting(
        section: Dict[str, object], section_name: str, name: str, default: bool
    ) -> bool:
        if name not in section:
            defaults_used.append(f"{section_name}.{name}")
            return default
        result = section[name]
        if not isinstance(result, bool):
            raise MonitorError(f"Setting {name} must be true or false")
        return result

    alerts = AlertCategorySettings(
        user_messages=boolean_setting(alerts_value, "alerts", "user_messages", True),
        alliance_messages=boolean_setting(alerts_value, "alerts", "alliance_messages", True),
        news=boolean_setting(alerts_value, "alerts", "news", True),
        reports=boolean_setting(alerts_value, "alerts", "reports", True),
    )

    wav_path: Optional[Path] = None
    if "wav_path" not in sound_value:
        defaults_used.append("sound.wav_path")
        # The bundled clip is the default, but a copy that lost the sounds folder should
        # still start and fall back to the normal Windows alert sound.
        wav_path = DEFAULT_WAV_PATH if DEFAULT_WAV_PATH.is_file() else None
    elif sound_value["wav_path"] is not None:
        raw_wav_path = sound_value["wav_path"]
        if not isinstance(raw_wav_path, str) or not raw_wav_path.strip():
            raise MonitorError("Setting wav_path must be null or a non-empty string")
        expanded_path = Path(os.path.expandvars(os.path.expanduser(raw_wav_path.strip())))
        wav_path = expanded_path if expanded_path.is_absolute() else path.parent / expanded_path
        wav_path = wav_path.resolve()
        if wav_path.suffix.lower() != ".wav":
            raise MonitorError(f"Configured alert sound is not a WAV file: {wav_path}")
        if not wav_path.is_file():
            raise MonitorError(f"Configured alert WAV file was not found: {wav_path}")

    loop_while_popup_open = boolean_setting(
        sound_value, "sound", "loop_while_popup_open", False
    )

    if "repeat_interval_seconds" not in sound_value:
        defaults_used.append("sound.repeat_interval_seconds")
        repeat_interval = 10.0
    else:
        raw_interval = sound_value["repeat_interval_seconds"]
        if isinstance(raw_interval, bool) or not isinstance(raw_interval, (int, float)):
            raise MonitorError("Setting repeat_interval_seconds must be a number")
        repeat_interval = float(raw_interval)
        if repeat_interval <= 0:
            raise MonitorError("Setting repeat_interval_seconds must be greater than zero")

    sound = SoundSettings(
        wav_path=wav_path,
        loop_while_popup_open=loop_while_popup_open,
        repeat_interval_seconds=repeat_interval,
    )
    cache = CacheSettings(
        persist_to_file=boolean_setting(cache_value, "cache", "persist_to_file", True),
    )

    reports_value = value.get("reports", {})
    if not isinstance(reports_value, dict):
        raise MonitorError("The reports setting must be a JSON object")
    report_ignore: Tuple[str, ...] = ()
    if "ignore" not in reports_value:
        defaults_used.append("reports.ignore")
    else:
        raw_ignore = reports_value["ignore"]
        if not isinstance(raw_ignore, list):
            raise MonitorError("Setting reports.ignore must be a list of patterns")
        for entry in raw_ignore:
            if not isinstance(entry, str) or not entry.strip():
                raise MonitorError("Every reports.ignore pattern must be a non-empty string")
        # JSON has no comments, so a leading # switches a pattern off in place: the shipped
        # patterns all sit there commented out, and enabling one means deleting two characters.
        report_ignore = tuple(
            entry.strip() for entry in raw_ignore if not entry.strip().startswith("#")
        )
    alerts = replace(alerts, report_ignore=report_ignore)

    fourchan_thread: Optional[FourChanThreadSettings] = None
    fourchan_value = value.get("fourchan")
    if fourchan_value is None:
        # An explicit null means "deliberately off"; an absent section is a default.
        if "fourchan" not in value:
            defaults_used.append("fourchan.thread_url")
    elif not isinstance(fourchan_value, dict):
        raise MonitorError("The fourchan setting must be a JSON object or null")
    elif "thread_url" not in fourchan_value:
        defaults_used.append("fourchan.thread_url")
    elif fourchan_value["thread_url"] is not None:
        raw_thread_url = fourchan_value["thread_url"]
        if not isinstance(raw_thread_url, str) or not raw_thread_url.strip():
            raise MonitorError("4chan thread_url must be null or a non-empty string")
        fourchan_thread = parse_fourchan_thread_url(raw_thread_url.strip())

    return MonitorSettings(
        alerts=alerts,
        sound=sound,
        cache=cache,
        fourchan_thread=fourchan_thread,
        defaults_used=tuple(defaults_used),
        file_found=file_found,
    )


def settings_startup_message(settings: MonitorSettings, path: Path) -> Optional[str]:
    """Name the settings that came from built-in defaults, or None when nothing was filled in."""
    if not settings.file_found:
        return (
            f"Settings: no settings file at {path}; using built-in defaults "
            "(4chan thread monitoring off)."
        )
    if not settings.defaults_used:
        return None
    return "Settings: using defaults for " + ", ".join(settings.defaults_used) + "."


def normalize_text(parts: Sequence[str]) -> str:
    return " ".join("".join(parts).split())


class LinkTextParser(HTMLParser):
    """Collect link destinations and their visible text, including nested spans."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._active: Optional[Dict[str, object]] = None
        self.links: List[Tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "a" and self._active is None:
            attributes = dict(attrs)
            self._active = {"href": attributes.get("href") or "", "text": []}

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            text = self._active["text"]
            assert isinstance(text, list)
            text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._active is not None:
            href = str(self._active["href"])
            text_parts = self._active["text"]
            assert isinstance(text_parts, list)
            self.links.append((href, normalize_text(text_parts)))
            self._active = None


class NewsTableParser(HTMLParser):
    """Collect timestamped, two-cell rows from the news table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_row = False
        self._cell_parts: Optional[List[str]] = None
        self._row: List[str] = []
        self.rows: List[Tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        del attrs
        tag = tag.lower()
        if tag == "tr":
            self._in_row = True
            self._row = []
        elif tag == "td" and self._in_row:
            self._cell_parts = []
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "td" and self._cell_parts is not None:
            self._row.append(normalize_text(self._cell_parts))
            self._cell_parts = None
        elif tag == "tr" and self._in_row:
            if len(self._row) == 2 and TIMESTAMP_RE.match(self._row[1]):
                self.rows.append((self._row[0], self._row[1]))
            self._row = []
            self._in_row = False
            self._cell_parts = None


class FourChanCommentParser(HTMLParser):
    """Turn a 4chan API comment fragment into compact plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        del attrs
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def parse_fourchan_comment(fragment: str) -> str:
    parser = FourChanCommentParser()
    parser.feed(fragment)
    return normalize_text(parser.parts)


def is_logged_in(html: str) -> bool:
    parser = LinkTextParser()
    parser.feed(html)
    return any(_path_from_href(href) == "logout.php" for href, _ in parser.links)


def _path_from_href(href: str) -> str:
    return urllib.parse.urlsplit(href).path.rsplit("/", 1)[-1].lower()


def parse_pending_counts(html: str) -> Tuple[int, int]:
    parser = LinkTextParser()
    parser.feed(html)
    user_count: Optional[int] = None
    alliance_count: Optional[int] = None

    for href, text in parser.links:
        path = _path_from_href(href)
        if path == "messages.php" and text.lower().startswith("messages"):
            match = COUNT_RE.search(text)
            user_count = int(match.group(1)) if match else 0
        elif path == "myalliance.php" and text.lower().startswith("my alliance"):
            match = COUNT_RE.search(text)
            alliance_count = int(match.group(1)) if match else 0

    if user_count is None:
        raise MonitorError("Could not find the user-message count in the authenticated navigation")

    # Accounts without an alliance may not render the My Alliance link in future site versions.
    return user_count, alliance_count or 0


def parse_latest_news(html: str) -> Optional[Tuple[str, str]]:
    parser = NewsTableParser()
    parser.feed(html)
    return parser.rows[0] if parser.rows else None


def parse_report_rows(html: str) -> List[Tuple[str, str]]:
    """Every timestamped report row on the page, newest first."""
    parser = NewsTableParser()
    parser.feed(html)
    return parser.rows


def parse_latest_report(html: str) -> Optional[Tuple[str, str]]:
    rows = parse_report_rows(html)
    return rows[0] if rows else None


def report_is_ignored(message: str, patterns: Sequence[str]) -> bool:
    """Whether a report matches an ignore pattern.

    A pattern matches anywhere in the message and ignores case; ``%`` stands for any run of
    characters, so ``Build % completed successfully.`` covers whatever was built.
    """
    for pattern in patterns:
        expression = ".*".join(re.escape(part) for part in pattern.split("%"))
        if re.search(expression, message, re.IGNORECASE):
            return True
    return False


def new_reports_since(
    marker: Optional[Tuple[str, str]], rows: Sequence[Tuple[str, str]]
) -> List[Tuple[str, str]]:
    """The report rows newer than the last one seen, newest first.

    The marker is matched by identity rather than by time so that two reports sharing a
    timestamp are both delivered; its timestamp is only a fallback for when the marker has
    dropped off the page entirely.
    """
    if marker is None:
        return list(rows)
    if marker in rows:
        return list(rows[: list(rows).index(marker)])
    return [row for row in rows if row[1] > marker[1]]


@dataclass(frozen=True)
class FourChanPost:
    thread_url: str
    number: int
    posted_at: int
    name: str
    comment: str

    def to_json(self) -> Dict[str, object]:
        return {
            "thread_url": self.thread_url,
            "number": self.number,
            "posted_at": self.posted_at,
            "name": self.name,
            "comment": self.comment,
        }


@dataclass(frozen=True)
class Snapshot:
    user_messages: int
    alliance_messages: int
    latest_news: Optional[Tuple[str, str]]
    fourchan_post: Optional[FourChanPost] = None
    latest_report: Optional[Tuple[str, str]] = None
    reports_checked: bool = False
    #: Every report row read this poll, newest first. Not persisted: only the marker above is.
    report_rows: Tuple[Tuple[str, str], ...] = ()

    def to_json(self) -> Dict[str, object]:
        return {
            "user_messages": self.user_messages,
            "alliance_messages": self.alliance_messages,
            "latest_news": (
                {"message": self.latest_news[0], "posted": self.latest_news[1]}
                if self.latest_news is not None
                else None
            ),
            "fourchan_post": (
                self.fourchan_post.to_json() if self.fourchan_post is not None else None
            ),
            "latest_report": (
                {"message": self.latest_report[0], "posted": self.latest_report[1]}
                if self.latest_report is not None
                else None
            ),
            "reports_checked": self.reports_checked,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_json(cls, value: Dict[str, object]) -> "Snapshot":
        latest_news: Optional[Tuple[str, str]] = None
        raw_latest_news = value.get("latest_news")
        if isinstance(raw_latest_news, dict):
            latest_news = (
                str(raw_latest_news.get("message", "")),
                str(raw_latest_news.get("posted", "")),
            )
        else:
            # Migrate the proof of concept's original whole-page state format.
            raw_news = value.get("news", [])
            if isinstance(raw_news, list) and raw_news and isinstance(raw_news[0], dict):
                latest_news = (
                    str(raw_news[0].get("message", "")),
                    str(raw_news[0].get("posted", "")),
                )
        fourchan_post: Optional[FourChanPost] = None
        raw_fourchan_post = value.get("fourchan_post")
        if isinstance(raw_fourchan_post, dict):
            try:
                fourchan_post = FourChanPost(
                    thread_url=str(raw_fourchan_post.get("thread_url", "")),
                    number=int(raw_fourchan_post.get("number", 0)),
                    posted_at=int(raw_fourchan_post.get("posted_at", 0)),
                    name=str(raw_fourchan_post.get("name", "Anonymous")),
                    comment=str(raw_fourchan_post.get("comment", "")),
                )
            except (TypeError, ValueError) as error:
                raise MonitorError("Saved 4chan post identifiers are not integers") from error
        latest_report: Optional[Tuple[str, str]] = None
        raw_latest_report = value.get("latest_report")
        if isinstance(raw_latest_report, dict):
            latest_report = (
                str(raw_latest_report.get("message", "")),
                str(raw_latest_report.get("posted", "")),
            )
        reports_checked = value.get("reports_checked", False)
        if not isinstance(reports_checked, bool):
            raise MonitorError("Saved reports_checked value is not true or false")
        try:
            user_messages = int(value.get("user_messages", 0))
            alliance_messages = int(value.get("alliance_messages", 0))
        except (TypeError, ValueError) as error:
            raise MonitorError("Saved message counts are not integers") from error
        return cls(
            user_messages=user_messages,
            alliance_messages=alliance_messages,
            latest_news=latest_news,
            fourchan_post=fourchan_post,
            latest_report=latest_report,
            reports_checked=reports_checked,
        )


class ClopClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        timeout: int = 20,
        fourchan_thread: Optional[FourChanThreadSettings] = None,
        initial_fourchan_post: Optional[FourChanPost] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.fourchan_thread = fourchan_thread
        self.initial_fourchan_post = initial_fourchan_post
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.opener.addheaders = [("User-Agent", "CLOP-notification-monitor/0.1")]

    def _open(self, path: str, form: Optional[Dict[str, str]] = None) -> str:
        url = urllib.parse.urljoin(self.base_url, path)
        data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
        request = urllib.request.Request(url, data=data)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as error:
            raise MonitorError(f"HTTP {error.code} from {url}") from error
        except urllib.error.URLError as error:
            raise MonitorError(f"Could not reach {url}: {error.reason}") from error

    def _latest_fourchan_post(self) -> Optional[FourChanPost]:
        if self.fourchan_thread is None:
            return None
        if self.initial_fourchan_post is not None:
            post = self.initial_fourchan_post
            self.initial_fourchan_post = None
            return post
        request = urllib.request.Request(self.fourchan_thread.api_url)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise MonitorError(
                    f"Configured 4chan thread is unavailable: {self.fourchan_thread.page_url}"
                ) from error
            raise MonitorError(
                f"HTTP {error.code} from {self.fourchan_thread.api_url}"
            ) from error
        except urllib.error.URLError as error:
            raise MonitorError(
                f"Could not reach {self.fourchan_thread.api_url}: {error.reason}"
            ) from error
        except (ValueError, TypeError) as error:
            raise MonitorError("The configured 4chan thread returned invalid JSON") from error

        if not isinstance(payload, dict) or not isinstance(payload.get("posts"), list):
            raise MonitorError("The configured 4chan thread response has no posts list")
        posts = payload["posts"]
        if not posts or not isinstance(posts[-1], dict):
            raise MonitorError("The configured 4chan thread has no readable latest post")
        original_post = posts[0]
        if isinstance(original_post, dict) and original_post.get("archived") == 1:
            raise ArchivedThreadError(
                "Configured 4chan thread is archived and cannot receive new posts: "
                f"{self.fourchan_thread.page_url}. Set fourchan.thread_url to null or replace it "
                "with the new thread URL before starting the monitor."
            )
        latest = posts[-1]
        try:
            number = int(latest["no"])
            posted_at = int(latest["time"])
        except (KeyError, TypeError, ValueError) as error:
            raise MonitorError("The latest 4chan post has invalid identifiers") from error
        comment = parse_fourchan_comment(str(latest.get("com", "")))
        if not comment:
            filename = str(latest.get("filename", "")).strip()
            extension = str(latest.get("ext", "")).strip()
            comment = f"[image: {filename}{extension}]" if filename else "[no text]"
        return FourChanPost(
            thread_url=self.fourchan_thread.page_url,
            number=number,
            posted_at=posted_at,
            name=str(latest.get("name", "Anonymous")),
            comment=comment,
        )

    def login(self) -> str:
        html = self._open(
            "login.php",
            {"username": self.username, "password": self.password},
        )
        if not is_logged_in(html):
            raise AuthenticationError("Login failed; check the credentials or the hosted login flow")
        return html

    def snapshot(self) -> Snapshot:
        navigation_html = self._open("index.php")
        if not is_logged_in(navigation_html):
            self.login()
            navigation_html = self._open("index.php")
            if not is_logged_in(navigation_html):
                raise AuthenticationError("The session was not retained after login")

        user_messages, alliance_messages = parse_pending_counts(navigation_html)
        if alliance_messages > 0:
            # CLOP stores alliance_messages_last_checked in each PHP session at login. If the
            # alliance page is read in a browser, this long-running session otherwise retains the
            # old timestamp forever and repeatedly reports already-read messages. A fresh login
            # reloads the database timestamp without opening the alliance page or marking it read.
            refreshed_navigation_html = self.login()
            user_messages, alliance_messages = parse_pending_counts(refreshed_navigation_html)
        news_html = self._open("news.php?page=1")
        latest_news = parse_latest_news(news_html)
        if "<h3>News</h3>" in news_html and "No news yet." not in news_html and latest_news is None:
            raise MonitorError("The newest news entry could not be parsed")
        reports_html = self._open("reports.php")
        if not is_logged_in(reports_html) or "<h3>Reports</h3>" not in reports_html:
            raise MonitorError("The authenticated reports page could not be read")
        report_rows = parse_report_rows(reports_html)
        latest_fourchan_post = self._latest_fourchan_post()
        return Snapshot(
            user_messages=user_messages,
            alliance_messages=alliance_messages,
            latest_news=latest_news,
            fourchan_post=latest_fourchan_post,
            latest_report=report_rows[0] if report_rows else None,
            reports_checked=True,
            report_rows=tuple(report_rows),
        )


def load_snapshot(path: Path) -> Optional[Snapshot]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as error:
        raise MonitorError(f"Could not read state file {path}: {error}") from error
    if not isinstance(value, dict):
        raise MonitorError(f"State file {path} does not contain a JSON object")
    return Snapshot.from_json(value)


def save_snapshot(path: Path, snapshot: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(snapshot.to_json(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(str(temporary_path), str(path))
    except OSError as error:
        raise MonitorError(f"Could not write state file {path}: {error}") from error


def build_alerts(
    previous: Optional[Snapshot],
    current: Snapshot,
    settings: AlertCategorySettings = AlertCategorySettings(),
) -> List[str]:
    alerts: List[str] = []
    if settings.user_messages and current.user_messages > 0:
        alerts.append(f"{current.user_messages} unread user message(s) pending")
    if settings.alliance_messages and current.alliance_messages > 0:
        alerts.append(f"{current.alliance_messages} unread alliance message(s) pending")

    if settings.news and previous is not None and current.latest_news != previous.latest_news:
        if current.latest_news is not None:
            message, posted = current.latest_news
            alerts.append(f"Newest news changed ({posted}): {message}")
        else:
            alerts.append("The news feed is now empty")
    if settings.reports and previous is not None and previous.reports_checked:
        # A snapshot restored from the state file carries only the marker, and the older
        # single-report snapshots carried nothing else either.
        rows: Sequence[Tuple[str, str]] = current.report_rows or (
            (current.latest_report,) if current.latest_report is not None else ()
        )
        for message, posted in new_reports_since(previous.latest_report, rows):
            if report_is_ignored(message, settings.report_ignore):
                continue
            preview = message if len(message) <= 800 else message[:797] + "..."
            alerts.append(
                f"New CLOP report ({posted}): {preview}\n"
                "https://4clop.org/reports.php"
            )
    if (
        current.fourchan_post is not None
        and previous is not None
        and previous.fourchan_post is not None
        and current.fourchan_post.thread_url == previous.fourchan_post.thread_url
        and current.fourchan_post != previous.fourchan_post
    ):
        post = current.fourchan_post
        posted = datetime.fromtimestamp(post.posted_at, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        comment = post.comment if len(post.comment) <= 800 else post.comment[:797] + "..."
        alerts.append(
            f"New 4chan /{urllib.parse.urlsplit(post.thread_url).path.split('/')[1]}/ "
            f"post #{post.number} by {post.name} ({posted}): {comment}\n"
            f"{post.thread_url}#p{post.number}"
        )
    return alerts


class Notifier:
    def __init__(
        self,
        desktop: bool = True,
        webhook_url: Optional[str] = None,
        sound: SoundSettings = SoundSettings(),
    ) -> None:
        self.desktop = desktop
        self.webhook_url = webhook_url
        self.sound = sound

    def notify(self, message: str) -> bool:
        """Send an alert and return whether a desktop dialog blocked until dismissal."""
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"\a[{timestamp}] CLOP: {message}", flush=True)
        if self.webhook_url:
            self._webhook_notification(message)
        if self.desktop:
            return self._desktop_notification(message)
        return False

    def notify_failure(self, message: str) -> bool:
        """Report a failure through the same channels as an alert.

        A failure is never terminal-only: it uses the same blocking dialog, so polling
        pauses until it is acknowledged rather than continuing to fail unnoticed.
        """
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"\a[{timestamp}] CLOP monitor problem: {message}", file=sys.stderr, flush=True)
        if self.webhook_url:
            self._webhook_notification(f"Monitor problem: {message}")
        if self.desktop:
            return self._desktop_notification(
                message, title="CLOP monitor problem", error=True
            )
        return False

    def _desktop_notification(
        self, message: str, title: str = "CLOP monitor", error: bool = False
    ) -> bool:
        environment = os.environ.copy()
        environment.pop("CLOP_PASSWORD", None)
        environment.pop("CLOP_WEBHOOK_URL", None)
        if sys.platform != "win32":
            notify_send = shutil.which("notify-send")
            if notify_send:
                try:
                    subprocess.Popen(
                        [notify_send, title, message],
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                except OSError as error:
                    print(f"Desktop notification failed: {error}", file=sys.stderr, flush=True)
            return False

        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            return False
        # A MessageBoxIcon plays its own system sound, so suppress it when a WAV is configured.
        if self.sound.wav_path is not None:
            icon = "None"
        else:
            icon = "Error" if error else "Information"
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "[System.Windows.Forms.MessageBox]::Show("
            "$env:CLOP_NOTIFICATION_TEXT,$env:CLOP_NOTIFICATION_TITLE,"
            "[System.Windows.Forms.MessageBoxButtons]::OK,"
            f"[System.Windows.Forms.MessageBoxIcon]::{icon}) | Out-Null"
        )
        environment["CLOP_NOTIFICATION_TEXT"] = message[:2000]
        environment["CLOP_NOTIFICATION_TITLE"] = title
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        sound_stop: Optional[threading.Event] = None
        sound_thread: Optional[threading.Thread] = None
        winsound_module = None
        if self.sound.wav_path is not None:
            import winsound

            winsound_module = winsound
            sound_stop = threading.Event()

            def play_sound() -> None:
                assert self.sound.wav_path is not None
                while sound_stop is not None and not sound_stop.is_set():
                    try:
                        winsound.PlaySound(
                            str(self.sound.wav_path),
                            winsound.SND_FILENAME | winsound.SND_ASYNC,
                        )
                    except RuntimeError as error:
                        print(f"Alert WAV playback failed: {error}", file=sys.stderr, flush=True)
                        return
                    if not self.sound.loop_while_popup_open:
                        return
                    if sound_stop.wait(self.sound.repeat_interval_seconds):
                        return

            sound_thread = threading.Thread(target=play_sound, name="clop-alert-sound", daemon=True)
            sound_thread.start()
        try:
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
                check=False,
            )
            if completed.returncode == 0:
                return True
            print(
                f"Desktop notification failed with exit code {completed.returncode}",
                file=sys.stderr,
                flush=True,
            )
            return False
        except OSError as error:
            print(f"Desktop notification failed: {error}", file=sys.stderr, flush=True)
            return False
        finally:
            if sound_stop is not None:
                sound_stop.set()
            if winsound_module is not None:
                try:
                    winsound_module.PlaySound(None, winsound_module.SND_PURGE)
                except RuntimeError:
                    winsound_module.PlaySound(None, 0)
            if sound_thread is not None:
                sound_thread.join(timeout=1)

    def _webhook_notification(self, message: str) -> None:
        assert self.webhook_url is not None
        payload = json.dumps({"content": f"CLOP: {message}", "text": f"CLOP: {message}"}).encode(
            "utf-8"
        )
        request = urllib.request.Request(
            self.webhook_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "CLOP-monitor/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15):
                pass
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            print(f"Webhook notification failed: {error}", file=sys.stderr, flush=True)


def check_and_notify(
    client: ClopClient,
    previous: Optional[Snapshot],
    notifier: Notifier,
    state_path: Path,
    alert_settings: AlertCategorySettings = AlertCategorySettings(),
    persist_state: bool = True,
) -> Tuple[Snapshot, bool]:
    """Poll once, pausing for a Windows dialog and refreshing state after dismissal."""
    current = client.snapshot()
    alerts = build_alerts(previous, current, alert_settings)
    paused_for_alert = False
    if alerts:
        paused_for_alert = notifier.notify("\n\n".join(alerts))
        if paused_for_alert:
            # The counts are re-read because dismissing the popup is when messages get read.
            # The news, report, and 4chan markers deliberately stay at what was alerted on:
            # anything that arrived while the dialog was open has not been seen yet, and
            # adopting the refreshed markers here would step over it without ever showing it.
            refreshed = client.snapshot()
            current = replace(
                refreshed,
                latest_news=current.latest_news,
                latest_report=current.latest_report,
                report_rows=current.report_rows,
                fourchan_post=current.fourchan_post,
            )
            print("Alert dismissed; refreshed the monitoring snapshot.", flush=True)
    if persist_state:
        save_snapshot(state_path, current)
    return current, paused_for_alert


def validate_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("base URL must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise argparse.ArgumentTypeError("remote base URL must use HTTPS")
    return base_url


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", type=validate_base_url, default=DEFAULT_BASE_URL)
    parser.add_argument("--username", help="login username (or set CLOP_USERNAME)")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"poll interval in seconds (default: {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_PATH,
        help=f"optional credentials/environment file (default: {DEFAULT_ENV_PATH})",
    )
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--no-desktop-notifications", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    return parser


def report_fatal(notifier: Notifier, message: str, code: int) -> int:
    """Alert that the monitor is stopping, then return its exit code."""
    notifier.notify_failure(f"{message}\n\nThe monitor has stopped and is no longer polling.")
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.interval < 15:
        print("--interval must be at least 15 seconds", file=sys.stderr)
        return 2

    # Settings are needed to build the real notifier, so failures loading them are reported
    # through a bootstrap notifier that uses the default Windows alert sound.
    notifier = Notifier(
        desktop=not args.no_desktop_notifications,
        webhook_url=os.environ.get("CLOP_WEBHOOK_URL"),
    )
    settings_path = args.settings.resolve()
    try:
        settings = load_settings(settings_path)
        env_file = load_env_file(args.env_file.resolve())
    except MonitorError as error:
        return report_fatal(notifier, str(error), 2)

    startup_message = settings_startup_message(settings, settings_path)
    if startup_message is not None:
        print(startup_message, flush=True)

    notifier = Notifier(
        desktop=not args.no_desktop_notifications,
        webhook_url=os.environ.get("CLOP_WEBHOOK_URL") or env_file.get("CLOP_WEBHOOK_URL"),
        sound=settings.sound,
    )
    if args.test_notification:
        notifier.notify("Test notification")
        return 0

    initial_fourchan_post: Optional[FourChanPost] = None
    if settings.fourchan_thread is not None:
        try:
            preflight_client = ClopClient(
                args.base_url,
                "",
                "",
                fourchan_thread=settings.fourchan_thread,
            )
            initial_fourchan_post = preflight_client._latest_fourchan_post()
        except MonitorError as error:
            return report_fatal(notifier, str(error), 2)
        assert initial_fourchan_post is not None
        print(
            f"4chan thread preflight passed; latest post is #{initial_fourchan_post.number}.",
            flush=True,
        )

    username = (
        args.username
        or os.environ.get("CLOP_USERNAME")
        or env_file.get("CLOP_USERNAME")
        or input("CLOP username: ").strip()
    )
    password = (
        os.environ.get("CLOP_PASSWORD")
        or env_file.get("CLOP_PASSWORD")
        or getpass.getpass("CLOP password: ")
    )
    if not username or not password:
        return report_fatal(notifier, "A username and password are required", 2)

    client = ClopClient(
        args.base_url,
        username,
        password,
        fourchan_thread=settings.fourchan_thread,
        initial_fourchan_post=initial_fourchan_post,
    )
    try:
        previous = load_snapshot(args.state) if settings.cache.persist_to_file else None
        client.login()
        cache_status = (
            f"file cache enabled at {args.state.resolve()}"
            if settings.cache.persist_to_file
            else "file cache disabled; baselines are in memory only"
        )
        print(
            f"Logged in to {client.base_url}; polling every {args.interval} seconds. "
            f"{cache_status}. Press Ctrl+C to stop.",
            flush=True,
        )
        while True:
            poll_failed = False
            try:
                current, _ = check_and_notify(
                    client,
                    previous,
                    notifier,
                    args.state,
                    settings.alerts,
                    settings.cache.persist_to_file,
                )
                previous = current
                print(
                    f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                    f"checked: user={current.user_messages}, "
                    f"alliance={current.alliance_messages}, "
                    f"latest_news={current.latest_news[1] if current.latest_news else 'none'}, "
                    f"latest_report={current.latest_report[1] if current.latest_report else 'none'}, "
                    "fourchan_post="
                    f"{current.fourchan_post.number if current.fourchan_post else 'disabled'}",
                    flush=True,
                )
            except (AuthenticationError, ArchivedThreadError):
                raise
            except MonitorError as error:
                # A failed check pauses on the same blocking dialog as an alert. It is never
                # swallowed and retried silently, because a monitor that cannot read the game
                # is not monitoring it.
                poll_failed = True
                continuation = (
                    "The monitor has stopped."
                    if args.once
                    else f"The monitor is still running and retries in {args.interval} seconds."
                )
                notifier.notify_failure(f"This check failed: {error}\n\n{continuation}")

            if args.once:
                return 1 if poll_failed else 0
            time.sleep(args.interval)
    except MonitorError as error:
        return report_fatal(notifier, str(error), 1)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
