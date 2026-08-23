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
from typing import Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple


DEFAULT_BASE_URL = "https://4clop.org/"
DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_STATE_PATH = Path(__file__).resolve().parent / ".state" / "clop-monitor.json"
DEFAULT_SETTINGS_PATH = Path(__file__).resolve().parent / "settings.json"
DEFAULT_WAV_PATH = Path(__file__).resolve().parent / "sounds" / "twilight-clock-is-ticking.wav"
DEFAULT_ENV_PATH = Path(__file__).resolve().parent / ".env"
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
COUNT_RE = re.compile(r"\(\s*(\d+)\s*\)")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HAVE_SUFFIX_RE = re.compile(r"\s*\(Have \d+\)$")
#: The buyer's marketplace says this, and only this, when a POST ran and found no buyers
#: (buyermarketplace.php:164). It is the one positive signal that separates a genuinely empty
#: market from a request the page refused, which renders neither a table nor this banner.
EMPTY_MARKET_MARKER = "Nobody wants to buy that item."


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
class MarketOrder:
    """One pending buy order on the buyer's marketplace."""

    good: str
    nation_id: int
    nation_name: str
    amount: int
    price: int
    is_friend: bool = False
    is_enemy: bool = False
    is_ally: bool = False

    def relation_label(self) -> str:
        """Every relation that is true of this buyer, for the alert line."""
        labels = []
        if self.is_friend:
            labels.append("friend")
        if self.is_enemy:
            labels.append("enemy")
        if self.is_ally:
            labels.append("alliance")
        return ", ".join(labels) if labels else "no relation"


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


#: The only settings a market good may carry; anything else is a typo the loader rejects.
MARKET_GOOD_KNOBS = frozenset({"friends", "alliance", "always", "never"})


def switchable_patterns(raw: object, label: str) -> Tuple[str, ...]:
    """A list of match patterns, minus the ones switched off with a leading #.

    JSON has no comments, so a leading # switches an entry off in place: the shipped entries
    all sit there commented out, and enabling one means deleting two characters. Report
    ignore-patterns and a good's nation-name overrides are both loaded this way so that the
    settings file has one convention rather than two. ``label`` names the setting in any
    error, because the reader has to go and find it in the file.
    """
    if not isinstance(raw, list):
        raise MonitorError(f"Setting {label} must be a list of patterns")
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise MonitorError(f"Every pattern in {label} must be a non-empty string")
    return tuple(entry.strip() for entry in raw if not entry.strip().startswith("#"))


def market_good_flag(
    section: Dict[str, object], good: str, name: str, default: bool
) -> bool:
    """One of a good's true/false knobs, falling back to ``default`` when it is left out."""
    result = section.get(name, default)
    if not isinstance(result, bool):
        raise MonitorError(f"Setting {name} for market good {good!r} must be true or false")
    return result


def load_market_goods(
    market_value: object, defaults_used: List[str]
) -> Tuple[WatchedGood, ...]:
    """The goods switched on in the market section, in the order the file lists them.

    Appends to ``defaults_used`` when the section leaves the goods out, so that startup can
    name what it filled in.
    """
    if not isinstance(market_value, dict):
        raise MonitorError("The market setting must be a JSON object")
    if "goods" not in market_value:
        defaults_used.append("market.goods")
        return ()
    raw_goods = market_value["goods"]
    if not isinstance(raw_goods, dict):
        raise MonitorError("Setting market.goods must be a JSON object keyed by good name")

    market_goods: List[WatchedGood] = []
    watched_names: Dict[str, str] = {}
    for raw_name, raw_good in raw_goods.items():
        name = raw_name.strip()
        # JSON has no comments, so a leading # switches a good off where you can still
        # see it; watching one means deleting two characters.
        if name.startswith("#"):
            continue
        if not name:
            raise MonitorError("Every market.goods key must be a non-empty good name")
        if not isinstance(raw_good, dict):
            raise MonitorError(f"Settings for market good {name!r} must be a JSON object")
        # Two spellings of one good would mean two marketplace lookups and two alerts a
        # poll, from a file that looks fine; say which keys clash rather than pick one.
        clash = watched_names.get(name.casefold())
        if clash is not None:
            raise MonitorError(
                f"Market goods {clash!r} and {name!r} differ only by capitalisation; "
                "keep whichever one you want and delete the other"
            )
        watched_names[name.casefold()] = name
        # A misspelled knob would otherwise load as its default, which is the opposite of
        # what was written for a knob being switched off.
        unknown = sorted(set(raw_good) - MARKET_GOOD_KNOBS)
        if unknown:
            raise MonitorError(
                f"Market good {name!r} has unknown settings {', '.join(unknown)}; "
                f"valid settings are {', '.join(sorted(MARKET_GOOD_KNOBS))}"
            )
        market_goods.append(
            WatchedGood(
                name=name,
                friends=market_good_flag(raw_good, name, "friends", default=True),
                alliance=market_good_flag(raw_good, name, "alliance", default=True),
                always=switchable_patterns(
                    raw_good.get("always", []),
                    f"always for market good {name!r} (nation names)",
                ),
                never=switchable_patterns(
                    raw_good.get("never", []),
                    f"never for market good {name!r} (nation names)",
                ),
            )
        )
    return tuple(market_goods)


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
        market_orders=boolean_setting(alerts_value, "alerts", "market_orders", True),
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
        report_ignore = switchable_patterns(reports_value["ignore"], "reports.ignore")
    alerts = replace(alerts, report_ignore=report_ignore)

    alerts = replace(
        alerts, market_goods=load_market_goods(value.get("market", {}), defaults_used)
    )

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


@dataclass(frozen=True)
class LoadedSettings:
    """The settings in force, paired with the exact bytes they were parsed from.

    The bytes travel with the settings because they are what decides whether a reload has
    anything to do at all. They are never inspected, only compared.
    """

    settings: MonitorSettings
    #: None when there is no settings file, which is a state rather than a failure.
    source: Optional[bytes] = None


def read_settings_source(path: Path) -> Optional[bytes]:
    """The settings file's raw bytes, or None when there is no file."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise MonitorError(f"Could not read settings file {path}: {error}") from error


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


#: The tags that end a line inside a table cell. ``<br>`` is the one the game writes on
#: purpose, but the tick wraps its detail lines in ``<div>``s (cron/frequent.php:786-799) and
#: puts no ``<br>`` between the last detail line and ``Change in Satisfaction:`` — only the
#: ``</div>`` that closes the block. Treating the block elements as line ends too is what
#: makes the split the page's own visible lines rather than a subset of them. ``p`` is
#: defensive: no report the game writes contains one, and it is here because a block element
#: silently swallowed would be the failure this whole split exists to prevent.
LINE_BREAK_TAGS = frozenset({"br", "div", "p"})


class NewsTableParser(HTMLParser):
    """Collect timestamped, two-cell rows from the news table.

    ``line_separator`` decides what a cell's line breaks become. News reads as one sentence
    and keeps the space it always had; reports are judged and shown a line at a time
    (docs/2026-08-23-per-line-report-judging-design.md) and keep a newline.

    The news text is unchanged in practice but not by identity: none of the game's ten
    ``INSERT INTO news`` sites writes a block tag, so real news reads exactly as it always
    did, while a cell that *did* contain one now yields a space where the old parser ran the
    two halves together into a single word.

    Only these tags break a line — a newline in the cell's *text* does not. Seven report
    strings in the tick span two or three source lines (frequent.php:952, :985 and :1014 are
    two-line double-quoted strings; :1296, :1302, :1309 and :1315 are three-line heredocs),
    and each of their sentences has to survive as one line for a pattern to match it.
    """

    def __init__(self, line_separator: str = " ") -> None:
        super().__init__(convert_charrefs=True)
        self.line_separator = line_separator
        self._in_row = False
        #: The open cell's lines, each a list of text fragments; None outside a cell.
        self._cell_lines: Optional[List[List[str]]] = None
        self._row: List[str] = []
        self.rows: List[Tuple[str, str]] = []

    def _break_line(self) -> None:
        if self._cell_lines is not None:
            self._cell_lines.append([])

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        del attrs
        tag = tag.lower()
        if tag == "tr":
            self._in_row = True
            self._row = []
        elif tag == "td" and self._in_row:
            self._cell_lines = [[]]
        elif tag in LINE_BREAK_TAGS:
            self._break_line()

    def handle_data(self, data: str) -> None:
        if self._cell_lines is not None:
            self._cell_lines[-1].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "td" and self._cell_lines is not None:
            self._row.append(self._cell_text())
            self._cell_lines = None
        elif tag == "tr" and self._in_row:
            if len(self._row) == 2 and TIMESTAMP_RE.match(self._row[1]):
                self.rows.append((self._row[0], self._row[1]))
            self._row = []
            self._in_row = False
            self._cell_lines = None
        elif tag in LINE_BREAK_TAGS:
            # Both ends of a block count, so a cell laid out as <div>a</div><div>b</div> is two
            # lines rather than one. The empty line that a start-then-end pair leaves behind is
            # dropped below, which is also what makes a self-closing <br/> harmless here.
            self._break_line()

    def _cell_text(self) -> str:
        assert self._cell_lines is not None
        lines = (normalize_text(parts) for parts in self._cell_lines)
        return self.line_separator.join(line for line in lines if line)


def _path_from_href(href: str) -> str:
    return urllib.parse.urlsplit(href).path.rsplit("/", 1)[-1].lower()


def nation_id_from_href(href: str) -> Optional[int]:
    """The nation_id of a viewnation.php link, or None for any other link."""
    if _path_from_href(href) != "viewnation.php":
        return None
    query = urllib.parse.urlsplit(href).query
    values = urllib.parse.parse_qs(query).get("nation_id")
    if not values or not values[0].isdigit():
        return None
    return int(values[0])


def parse_market_number(text: str) -> Optional[int]:
    """A price or amount cell; prices are rendered with thousands separators."""
    digits = text.replace(",", "").strip()
    return int(digits) if digits.isdigit() else None


class BuyerMarketParser(HTMLParser):
    """Rows of the buyer's-marketplace deals table.

    The buyer's colour is taken from the first span inside the row's viewnation link and
    never from colour classes elsewhere in the row: the price cell is text-danger and the
    amount cell text-success in every row, so a page-wide class match would call every buyer
    an enemy and an ally at once.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        #: nation_id, nation name, colour class, amount, price
        self.rows: List[Tuple[int, str, str, int, int]] = []
        self._start_row()

    def _start_row(self) -> None:
        self._numbers: List[str] = []
        self._number_parts: Optional[List[str]] = None
        self._anchor_parts: Optional[List[str]] = None
        self._anchor_text = ""
        self._nation_id: Optional[int] = None
        self._colour = ""
        self._colour_taken = False
        self._span_parts: Optional[List[str]] = None
        self._span_text = ""

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "tr":
            self._start_row()
        elif tag == "div" and "col-md-1" in (attributes.get("class") or "").split():
            self._number_parts = []
        elif tag == "a" and self._nation_id is None:
            nation_id = nation_id_from_href(attributes.get("href") or "")
            if nation_id is not None:
                self._nation_id = nation_id
                self._anchor_parts = []
        elif tag == "span" and self._anchor_parts is not None and not self._colour_taken:
            self._colour_taken = True
            self._colour = (attributes.get("class") or "").strip()
            self._span_parts = []

    def handle_data(self, data: str) -> None:
        if self._number_parts is not None:
            self._number_parts.append(data)
        if self._span_parts is not None:
            self._span_parts.append(data)
        if self._anchor_parts is not None:
            self._anchor_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "div" and self._number_parts is not None:
            self._numbers.append(normalize_text(self._number_parts))
            self._number_parts = None
        elif tag == "span" and self._span_parts is not None:
            self._span_text = normalize_text(self._span_parts)
            self._span_parts = None
        elif tag == "a" and self._anchor_parts is not None:
            self._anchor_text = normalize_text(self._anchor_parts)
            self._anchor_parts = None
        elif tag == "tr":
            self._finish_row()
            self._start_row()

    def _finish_row(self) -> None:
        # The header row has the same two numeric cells but no buyer link, so requiring the
        # link is what keeps it out.
        if self._nation_id is None or len(self._numbers) < 2:
            return
        # The two col-md-1 cells are Offering (the price) then Amount, in that order
        # (buyermarketplace.php:56-58). Should the site ever add or reorder a numeric column,
        # the two would silently swap and the alerts would read plausibly but wrongly, so this
        # is the assumption to check first when the numbers look odd.
        price = parse_market_number(self._numbers[0])
        amount = parse_market_number(self._numbers[1])
        if price is None or amount is None:
            return
        # An unstyled buyer has no span, so the name is the anchor text up to the region,
        # which is the last bracket rather than the first in case a name ever contains one.
        name = (self._span_text or self._anchor_text.rsplit("(", 1)[0]).strip()
        if not name:
            return
        self.rows.append((self._nation_id, name, self._colour, amount, price))


def parse_market_orders(
    html: str, good: str, roster: Optional[FrozenSet[int]]
) -> List[MarketOrder]:
    """Every pending buy order for one good, in page order: highest price first.

    The page's order is ``ORDER BY m.price DESC, n.nation_id DESC``
    (backend_buyermarketplace.php:266), so the best offer leads and a tie goes to the newest
    nation; it is not a recency order and nothing here re-sorts it.

    ``roster`` is the set of nation ids in your alliance, or None when it was not looked up.
    Without it the green colour is the only alliance signal available; with it, membership is
    exact even for a buyer the game painted blue or red instead.
    """
    parser = BuyerMarketParser()
    parser.feed(html)
    orders: List[MarketOrder] = []
    for nation_id, name, colour, amount, price in parser.rows:
        classes = colour.split()
        is_green = "text-success" in classes
        orders.append(
            MarketOrder(
                good=good,
                nation_id=nation_id,
                nation_name=name,
                amount=amount,
                price=price,
                is_friend="text-info" in classes,
                is_enemy="text-danger" in classes,
                is_ally=(nation_id in roster) if roster is not None else is_green,
            )
        )
    return orders


class HiddenFieldParser(HTMLParser):
    """The value of the first hidden input with a given name.

    Only ``type="hidden"`` counts: a submit button carries the same name as the field it
    submits, and its value is the button's label, so accepting one would quietly send the
    label back as the CSRF token or the mode.
    """

    def __init__(self, name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.name = name.lower()
        self.value: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "input" or self.value is not None:
            return
        attributes = dict(attrs)
        if (attributes.get("type") or "").lower() != "hidden":
            return
        if (attributes.get("name") or "").lower() == self.name:
            self.value = attributes.get("value") or ""


def parse_hidden_field(html: str, name: str) -> Optional[str]:
    parser = HiddenFieldParser(name)
    parser.feed(html)
    return parser.value


class SelectOptionParser(HTMLParser):
    """The options of one named select, each with its value, text, and selected flag."""

    def __init__(self, select_name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.select_name = select_name.lower()
        self.options: List[Tuple[str, str, bool]] = []
        self._active = False
        self._value: Optional[str] = None
        self._selected = False
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "select":
            self._active = (attributes.get("name") or "").lower() == self.select_name
        elif tag == "option" and self._active:
            self._value = attributes.get("value") or ""
            self._selected = "selected" in attributes
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._value is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "option" and self._value is not None:
            self.options.append((self._value, normalize_text(self._parts), self._selected))
            self._value = None
        elif tag == "select":
            self._active = False


def parse_select_options(html: str, select_name: str) -> List[Tuple[str, str, bool]]:
    parser = SelectOptionParser(select_name)
    parser.feed(html)
    return parser.options


def parse_good_ids(html: str) -> Dict[str, int]:
    """Good name to resource_id, from the buyer's-marketplace selector.

    The option text carries a "(Have N)" suffix for a good you hold some of, because
    backend_buyermarketplace.php builds the option label that way; it is stripped so the
    settings can name goods the way the game does.
    """
    goods: Dict[str, int] = {}
    for value, text, _ in parse_select_options(html, "resource_id"):
        name = HAVE_SUFFIX_RE.sub("", text).strip()
        if value.isdigit() and name:
            goods[name] = int(value)
    return goods


def parse_current_nation_id(html: str) -> Optional[int]:
    """The active nation, from the header's nation switcher.

    header.php renders that switcher only for an account with more than one nation, so a
    single-nation account yields None and the id has to come from the empire overview.
    """
    for value, _, selected in parse_select_options(html, "switchnation_id"):
        if selected and value.isdigit():
            return int(value)
    return None


class ButtonValueParser(HTMLParser):
    """The values of every button with a given name."""

    def __init__(self, name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.name = name.lower()
        self.values: List[str] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "button":
            return
        attributes = dict(attrs)
        if (attributes.get("name") or "").lower() == self.name:
            self.values.append(attributes.get("value") or "")


def parse_empire_nation_ids(html: str) -> List[int]:
    """Every nation on the account, from the empire overview's switch buttons."""
    parser = ButtonValueParser("switchnation_id")
    parser.feed(html)
    return [int(value) for value in parser.values if value.isdigit()]


def _links(html: str) -> List[Tuple[str, str]]:
    """Every (href, text) pair on the page, in document order."""
    parser = LinkTextParser()
    parser.feed(html)
    return parser.links


def parse_alliance_link(html: str) -> Optional[Tuple[int, str]]:
    """The (id, name) of the alliance linked from a nation page, or None for no such link.

    viewnation.php:23 renders the link unconditionally, so a nation with no alliance yields
    id ``0`` rather than None; a caller deciding whether to fetch a roster has to treat that
    as "no alliance", not as alliance number zero.

    The name comes back with the id because the only caller that wants one wants both, and
    matching the link twice invites the two matches to drift apart.
    """
    for href, text in _links(html):
        if _path_from_href(href) != "viewalliance.php":
            continue
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("alliance_id")
        if values and values[0].isdigit():
            return int(values[0]), text
    return None


def parse_alliance_nation_ids(html: str) -> FrozenSet[int]:
    """Every member nation on an alliance page.

    viewalliance.php links a nation only from its member table (viewalliance.php:70), so every
    viewnation link on the page is a member of that alliance.

    **An empty result means the fetch failed, not that the alliance is empty.** You are a
    member of the alliance you looked up, so your own nation is always in that table and a
    real page can never come back with nothing; an empty set means an error page, a logged-out
    page, or drifted markup. A caller must raise on it rather than pass it on as a roster —
    handing an empty roster to ``parse_market_orders`` would demote every genuine ally to a
    stranger and the monitor would quietly stop alerting on the case it exists for. Compare
    ``parse_pending_counts``, which raises for the same reason. This stays a pure parser and
    reports what it found.
    """
    return frozenset(
        nation_id
        for href, _ in _links(html)
        if (nation_id := nation_id_from_href(href)) is not None
    )


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
    """Every timestamped report row on the page, newest first, one line per line of the page.

    The lines are kept because a report is judged and shown a line at a time: a tick packs
    routine production and a lost military force into the same row, and only per-line
    matching can silence the first without the second.
    """
    parser = NewsTableParser("\n")
    parser.feed(html)
    return parser.rows


def parse_latest_report(html: str) -> Optional[Tuple[str, str]]:
    rows = parse_report_rows(html)
    return rows[0] if rows else None


def matches_any_pattern(text: str, patterns: Sequence[str]) -> bool:
    """Whether the text matches any pattern.

    A pattern matches anywhere in the text and ignores case; ``%`` stands for any run of
    characters, so ``Build % completed successfully.`` covers whatever was built. Report
    ignore-patterns and market nation-name patterns share this rule so that the settings file
    has one convention rather than two.
    """
    for pattern in patterns:
        expression = ".*".join(re.escape(part) for part in pattern.split("%"))
        if re.search(expression, text, re.IGNORECASE):
            return True
    return False


def surviving_report_lines(message: str, patterns: Sequence[str]) -> List[str]:
    """The lines of a report that no ignore pattern silences, in page order.

    Each line is judged on its own because the game packs a whole tick into one report row:
    routine production and a force that starved to death arrive together, so a pattern
    matched against the row is all-or-nothing for both. Nothing surviving means the report
    raises no alert, which is what a matching pattern always meant; anything surviving is
    what the alert shows, so the one line that matters is not buried in the other forty.

    This is deliberately a domain-named entry point over the shared matching rule rather
    than a rename left half-finished: the reports call site is about reports, and the market
    call site is about nation names, so neither should read as the other.
    """
    return [line for line in message.split("\n") if not matches_any_pattern(line, patterns)]


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


def fourchan_preflight_message(post: FourChanPost) -> str:
    """What startup and a mid-run thread swap both say about a newly adopted baseline.

    Adopting post #N silently decides that every post up to #N will never alert, so the
    number is worth stating either way. Shared between the two paths so that reload output is
    recognisably the same thing as startup output rather than a second sentence that drifts.
    """
    return f"4chan thread preflight passed; latest post is #{post.number}."


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
    #: Every watched good's pending buy orders this poll. Not persisted: alerting is
    #: every-poll, so there is no baseline to keep.
    market_orders: Tuple[MarketOrder, ...] = ()

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
        #: (good name, resource_id) pairs, filled in by market_preflight.
        self.market_goods: Tuple[Tuple[str, int], ...] = ()
        #: The account's own alliance, with three distinct meanings:
        #: ``None`` it has not been resolved — no watched good checks alliance, or the
        #: preflight has not run or did not finish; membership is simply unknown.
        #: ``0`` it was resolved and this nation is in no alliance, so nobody is an ally.
        #: ``N`` it was resolved to alliance N, whose roster decides who is an ally.
        #: The zero comes from the game: viewnation.php:23 renders the alliance link even for
        #: a nation in no alliance, pointing it at alliance 0.
        self.alliance_id: Optional[int] = None
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

    def _alliance_roster(self) -> FrozenSet[int]:
        """The nation ids in this account's alliance.

        Read from viewalliance.php. myalliance.php is never used: it sets
        alliance_messages_last_checked on every load, which would silently mark the alliance
        messages this monitor exists to report.

        Requires a resolved ``alliance_id``: "in no alliance" and "not looked up" are
        different facts, and answering the second with an empty roster would report every
        ally as a stranger rather than admitting the gap.
        """
        if self.alliance_id is None:
            raise MonitorError(
                "The account's alliance has not been resolved; market_preflight must run "
                "before the alliance roster can be read"
            )
        if self.alliance_id == 0:
            return frozenset()
        roster = parse_alliance_nation_ids(
            self._open(f"viewalliance.php?alliance_id={self.alliance_id}")
        )
        # You are a member of the alliance you looked up, so your own nation is always in
        # that table: an empty parse means the fetch failed, not that the alliance is empty.
        # A fetched roster is treated as authoritative, so returning an empty one would
        # demote every ally to a stranger and silently stop the alerts this feature exists
        # for.
        if not roster:
            raise MonitorError(
                f"The alliance page for alliance {self.alliance_id} listed no member nations, "
                'so the roster could not be read. Set "alliance": false on your watched '
                "goods in settings.json to run without the alliance check."
            )
        return roster

    def _market_orders(self, roster: Optional[FrozenSet[int]]) -> Tuple[MarketOrder, ...]:
        """Every pending buy order for the watched goods.

        The order table exists only for a POST, and the page regenerates its CSRF token on
        every POST, so each response carries the token the next one has to spend.

        The form carries only the token, the mode and the good: offer, remove, sellone,
        sellall and sellamount are the fields that make the page change game state, so
        leaving them out is what keeps this a read-only filter of the deals table.
        """
        if not self.market_goods:
            return ()
        html = self._open("buyermarketplace.php")
        orders: List[MarketOrder] = []
        for good, resource_id in self.market_goods:
            token = parse_hidden_field(html, "token_buyermarketplace")
            if not token:
                raise MonitorError(
                    "The buyer's marketplace form has no CSRF token; the session may have "
                    "expired or the page may have changed"
                )
            html = self._open(
                "buyermarketplace.php",
                {
                    "token_buyermarketplace": token,
                    "mode": "",
                    "resource_id": str(resource_id),
                },
            )
            orders_for_good = parse_market_orders(html, good, roster)
            # A refused POST cannot be caught by the next iteration's token check: the page
            # rotates its token unconditionally on any POST
            # (backend_buyermarketplace.php:55-57), *before* the `if (!$errors)` gate, so an
            # error page still carries a fresh, valid token and the next good succeeds. The
            # loop would self-heal and the refused good would silently contribute nothing.
            # The empty-market banner is the game's positive marker for the genuine case:
            # buyermarketplace.php:162-166 renders it only under
            # `$_POST['resource_id'] && empty($errors)`, so no rows and no banner means the
            # request never ran.
            if not orders_for_good and EMPTY_MARKET_MARKER not in html:
                raise MonitorError(
                    f"The buyer's marketplace did not return the order table for {good}; "
                    "the session may have expired, or the page may have changed"
                )
            orders.extend(orders_for_good)
        return tuple(orders)

    def _own_nation_id(self, navigation_html: str) -> int:
        """The active nation's id.

        The header carries it in the nation switcher, but header.php renders that switcher
        only for an account with more than one nation, so a single-nation account is read
        from the empire overview instead, where exactly one button means exactly one nation.
        """
        nation_id = parse_current_nation_id(navigation_html)
        if nation_id is not None:
            return nation_id
        nation_ids = parse_empire_nation_ids(self._open("empireoverview.php"))
        if len(nation_ids) != 1:
            raise MonitorError(
                "Could not identify which nation is active: the header has no nation "
                f"switcher and the empire overview lists {len(nation_ids)} nations"
            )
        return nation_ids[0]

    def market_preflight(self, goods: Sequence[WatchedGood]) -> Optional[str]:
        """Resolve watched good names and, if needed, the account's own alliance.

        Run at startup, and again whenever a reload changes the watched goods, so that a
        mistyped good name is caught rather than silently watching nothing, and so that the
        game gaining or losing a good later cannot kill a monitor already running. What a
        failure costs depends on which caller ran it: at startup a mistyped name stops the
        monitor, where the same name in a reload is a refused reload that leaves the previous
        settings in force. Either way the assignment below is all-or-nothing, which is what
        lets the reload caller treat a failure as "nothing happened".
        """
        if not goods:
            return None
        available = parse_good_ids(self._open("buyermarketplace.php"))
        if not available:
            raise MonitorError("The buyer's marketplace listed no tradeable goods")
        by_folded = {name.casefold(): (name, value) for name, value in available.items()}
        resolved: List[Tuple[str, int]] = []
        unknown: List[str] = []
        for good in goods:
            match = by_folded.get(good.name.casefold())
            if match is None:
                unknown.append(good.name)
            else:
                resolved.append(match)
        if unknown:
            raise MonitorError(
                "These market.goods are not tradeable goods: "
                + ", ".join(sorted(unknown))
                + ". The tradeable goods are: "
                + ", ".join(sorted(available))
            )
        # The game's spelling is kept, not the settings file's, so the startup message names
        # the good the way the game does. build_alerts pairs orders back to their watch entry
        # case-insensitively precisely because of this; do not "fix" that by storing the
        # user's spelling here instead.
        market_goods = tuple(resolved)
        watching = ", ".join(name for name, _ in resolved)

        # Everything above resolved into locals, and everything below either resolves the
        # alliance or raises, so the two fields are assigned together at the end: a preflight
        # that failed part-way must not leave a caller watching goods with alliance detection
        # silently degraded to the green-colour heuristic the roster exists to replace.
        alliance_id: Optional[int] = None
        message = f"Market preflight passed; watching {watching} (friends only)."
        if any(good.alliance for good in goods):
            nation_id = self._own_nation_id(self._open("index.php"))
            link = parse_alliance_link(self._open(f"viewnation.php?nation_id={nation_id}"))
            if link is None:
                raise MonitorError(
                    f"Could not read the alliance of nation {nation_id}: its page has no "
                    'alliance link. Set "alliance": false on your watched goods in '
                    "settings.json to run without the alliance check."
                )
            alliance_id, alliance_name = link
            # viewnation.php links even a nation in no alliance, at alliance 0, so this is a
            # real answer rather than a missing one.
            if alliance_id == 0:
                message = (
                    f"Market preflight passed; watching {watching}. This nation has "
                    "no alliance, so the alliance check will never match."
                )
            else:
                message = (
                    f"Market preflight passed; watching {watching}; "
                    f"alliance is {alliance_name} (#{alliance_id})."
                )

        self.market_goods = market_goods
        self.alliance_id = alliance_id
        return message

    def login(self) -> str:
        html = self._open(
            "login.php",
            {"username": self.username, "password": self.password},
        )
        if not is_logged_in(html):
            raise AuthenticationError("Login failed; check the credentials or the hosted login flow")
        return html

    def snapshot(self, include_market: bool = True) -> Snapshot:
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
        market_orders: Tuple[MarketOrder, ...] = ()
        if include_market and self.market_goods:
            roster = self._alliance_roster() if self.alliance_id is not None else None
            market_orders = self._market_orders(roster)
        return Snapshot(
            user_messages=user_messages,
            alliance_messages=alliance_messages,
            latest_news=latest_news,
            fourchan_post=latest_fourchan_post,
            latest_report=report_rows[0] if report_rows else None,
            reports_checked=True,
            report_rows=tuple(report_rows),
            market_orders=market_orders,
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


def goods_to_watch(settings: AlertCategorySettings) -> Tuple[WatchedGood, ...]:
    """The goods to actually fetch, which is nothing at all when the category is muted.

    Muting has to stop the work rather than discard its result: a watch list left in place
    would otherwise still resolve the alliance and POST once per good every poll for orders
    nothing reads, and a good-name typo would still be a fatal startup error for a feature
    that is switched off.
    """
    return settings.market_goods if settings.market_orders else ()


def market_order_alerts(order: MarketOrder, good: WatchedGood) -> bool:
    """Whether one buy order raises an alert under this good's settings.

    never is absolute, which is what "never alert on" says; always then beats both relation
    checks, so both of them off with a populated always reads as "only these nations,
    whoever they are". The two relation checks are independent, so a buyer who is both a
    friend and an ally satisfies either one on its own.
    """
    if matches_any_pattern(order.nation_name, good.never):
        return False
    if matches_any_pattern(order.nation_name, good.always):
        return True
    if good.friends and order.is_friend:
        return True
    return good.alliance and order.is_ally


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
            surviving = surviving_report_lines(message, settings.report_ignore)
            if not surviving:
                continue
            body = "\n".join(surviving)
            preview = body if len(body) <= 800 else body[:797] + "..."
            alerts.append(
                f"New CLOP report ({posted}): {preview}\n"
                "https://4clop.org/reports.php"
            )
    # No previous to compare against, unlike the branches above: a standing buy order is a
    # current fact, not an event, so it alerts every poll for as long as it is pending.
    # goods_to_watch, rather than an inline market_orders check, so that "muted means nothing
    # is watched" is stated once and the alerting cannot drift from what the poll fetches.
    for good in goods_to_watch(settings):
        # market_preflight resolves good names case-insensitively and stores the game's
        # canonical spelling, which is what stamps each order, while good.name is
        # whatever was typed into settings.json. Comparing those exactly would silently
        # match nothing for a user who wrote "machinery parts".
        watched = good.name.casefold()
        lines = [
            f"  {order.nation_name} ({order.relation_label()}) "
            f"wants {order.amount:,} at {order.price:,} bits each"
            for order in current.market_orders
            if order.good.casefold() == watched and market_order_alerts(order, good)
        ]
        if lines:
            alerts.append(
                f"Buy orders for {good.name}:\n"
                + "\n".join(lines)
                + "\nhttps://4clop.org/buyermarketplace.php"
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


def sync_sheet_step(
    client: ClopClient, sheet: object, nation: str, notifier: Notifier
) -> None:
    """Sync the nation's tab from overview.php, ahead of the regular alerting.

    Two syncs off one page fetch, in this order:

    1. **Buildings** -- reconcile the have/disabled counts and pop up any corrections made.
    2. **Stockpiles** -- snapshot the six goods into R11:R16 and stamp W10 with the server time.

    They guard different regions of the sheet and are independent: one being skipped for a layout
    problem does not skip the other. The whole step is best-effort -- every failure is reported
    through the same blocking dialog and then swallowed, because sheet sync must never take the
    monitor down.

    A dropped session is re-logged-in before anything is trusted: a logged-out overview would look
    like a nation that owns nothing and holds nothing, and would zero the sheet.
    """
    from buildings import BuildingError, parse_overview_buildings, reconcile, sanity_check
    from sheets import SheetError
    from stockpiles import (
        StockpileError,
        check_labels,
        parse_overview_resources,
        parse_server_time,
        snapshot,
    )

    try:
        overview_html = client._open("overview.php")
        if not is_logged_in(overview_html):
            client.login()
            overview_html = client._open("overview.php")
            if not is_logged_in(overview_html):
                raise MonitorError("not logged in when reading overview.php")

        overview = parse_overview_buildings(overview_html)
        problems = sanity_check(sheet, nation, overview)
        if problems:
            notifier.notify_failure(
                "Building sync skipped — the sheet layout or building mapping looks wrong, so no "
                "building cells were changed:\n\n"
                + "\n".join(f"- {problem}" for problem in problems)
                + "\n\nRun 'python buildings.py' to recheck once the sheet is fixed."
            )
        else:
            corrections = reconcile(sheet, nation, overview)
            if corrections:
                notifier.notify(
                    "Building counts corrected on the sheet:\n\n"
                    + "\n".join(f"- {correction.describe()}" for correction in corrections)
                )

        # The stockpile snapshot is a scheduled refresh rather than an event, so a successful write
        # is deliberately silent -- at a 60s poll a popup for it would never stop firing.
        server_time = parse_server_time(overview_html)
        stock_problems = check_labels(sheet, nation)
        if stock_problems:
            notifier.notify_failure(
                "Stockpile snapshot skipped — the sheet's STOCK labels have moved, so nothing was "
                "written (R11:R16 and the W10 timestamp are untouched, and W10 will now go "
                "stale):\n\n"
                + "\n".join(f"- {problem}" for problem in stock_problems)
                + "\n\nRun 'python stockpiles.py' to recheck once the sheet is fixed."
            )
        else:
            snapshot(sheet, nation, parse_overview_resources(overview_html), server_time)
    except (MonitorError, SheetError, BuildingError, StockpileError) as error:
        notifier.notify_failure(f"Sheet sync failed: {error}\n\nThe monitor continues polling.")


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
            # The market is deliberately not re-read: its alerting is every-poll, so the next
            # poll reports whatever is still pending, and refetching would cost one request
            # per watched good for a value this refresh does not use.
            refreshed = client.snapshot(include_market=False)
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


def settings_changes(previous: MonitorSettings, current: MonitorSettings) -> Tuple[str, ...]:
    """The settings sections that differ, named the way settings.json names them.

    Named per section rather than as one "something changed" because the only question the
    person who just saved the file has is whether *their* edit took. ``defaults_used`` and
    ``file_found`` describe the file rather than what the monitor is doing, so they are not
    compared at all.

    The market is compared through ``goods_to_watch`` rather than ``alerts.market_goods`` so
    that muting ``alerts.market_orders`` reads as "nothing watched" and releases the
    preflight, exactly as deleting the goods would.
    """

    def categories(alerts: AlertCategorySettings) -> AlertCategorySettings:
        # reports.ignore and market.goods live on this dataclass but are their own sections
        # in the file, so they are held aside here and named separately below.
        return replace(alerts, report_ignore=(), market_goods=())

    changes: List[str] = []
    if categories(previous.alerts) != categories(current.alerts):
        changes.append("alerts")
    if previous.alerts.report_ignore != current.alerts.report_ignore:
        changes.append("reports.ignore")
    if goods_to_watch(previous.alerts) != goods_to_watch(current.alerts):
        changes.append("market.goods")
    if previous.sound != current.sound:
        changes.append("sound")
    if previous.cache != current.cache:
        changes.append("cache")
    if previous.fourchan_thread != current.fourchan_thread:
        changes.append("fourchan.thread_url")
    return tuple(changes)


def reload_settings(
    path: Path,
    loaded: LoadedSettings,
    client: ClopClient,
    notifier: Notifier,
    build_notifier: Callable[[SoundSettings], Notifier],
) -> Tuple[LoadedSettings, Notifier]:
    """Re-read settings.json for the coming poll, applying it in full or not at all.

    A file that cannot be read, parsed, validated or brought into service is refused whole:
    the monitor warns through the same blocking dialog as a failed poll, keeps the settings
    it already had, and goes on polling. Applying only the half that worked would leave the
    live configuration a mixture of two files with nothing naming which parts came from
    where, and ending an overnight run over a stray keystroke in a text file is worse than
    running on yesterday's settings for another minute.

    **This reconfigures ``client`` in place** — ``fourchan_thread``, ``initial_fourchan_post``,
    ``market_goods`` and ``alliance_id`` — while returning the settings and the notifier.
    The asymmetry is because a client is long-lived and holds a session, where a notifier
    holding changed sound settings is cheaper to replace than to mutate. Both returned values
    are the ones passed in whenever nothing changed or the reload was refused.
    """

    def refuse(reason: str) -> Tuple[LoadedSettings, Notifier]:
        """Warn, change nothing, and leave the next poll to try the file again."""
        notifier.notify_failure(reason)
        return loaded, notifier

    still_polling = (
        "The previous settings are still in force and the monitor will go on polling."
    )
    try:
        source = read_settings_source(path)
    except MonitorError as error:
        return refuse(f"settings.json could not be reloaded: {error}\n\n{still_polling}")

    # The file's own bytes are the gate, not the parsed result: load_settings validates
    # against the world outside the file — wav_path has to still exist — so parsing an
    # untouched file every 60 seconds turns a WAV on a USB stick, a network share or an
    # on-demand cloud path into a blocking dialog every poll that nobody caused, and because
    # this runs before the check, the game goes unread until somebody clicks OK. Only the
    # bytes can say "nobody touched this" without consulting anything outside the file.
    # This is not the mtime watching the design rejects: mtime is a proxy that can lie in
    # either direction, where the bytes are the thing itself.
    if source == loaded.source:
        return loaded, notifier

    settings = loaded.settings
    try:
        reloaded = load_settings(path)
    except MonitorError as error:
        return refuse(f"settings.json could not be reloaded: {error}\n\n{still_polling}")

    # An absent file loads cleanly as the built-in defaults, which is right at startup and
    # wrong here: a file that vanishes under a running monitor would switch every muted
    # category back on, drop the watched goods, and start writing the state file that
    # cache.persist_to_file had turned off — all under a confirmation line reading as though
    # the edit took. Mid-run that is far more likely a rename to settings.json.bak, or a
    # reload landing inside a non-atomic editor save, than a deliberate "revert everything",
    # and refusing it costs the deliberate case nothing: writing {} still asks for defaults.
    if settings.file_found and not reloaded.file_found:
        return refuse(
            f"The settings file has disappeared: there is no file at {path}\n\n"
            f"{still_polling} To go back to the built-in defaults on purpose, put an empty "
            "JSON object ({}) in the file instead of deleting it."
        )

    changes = settings_changes(settings, reloaded)
    # A cosmetic edit — reindenting, reordering keys — parses to the same settings. The new
    # bytes are still adopted, or that one edit would be re-parsed on every remaining poll.
    if not changes:
        return LoadedSettings(settings, source), notifier

    # Everything that can fail runs here, before anything is applied, and this block contains
    # exactly one mutating call: market_preflight, which is atomic by construction because it
    # assigns its two fields together at the end or not at all. Nothing else may join it. The
    # 4chan check goes first because it works through a throwaway client and touches nothing,
    # and an apply that lands before a later step raises is the partial reload this whole
    # function exists to prevent.
    thread_changed = "fourchan.thread_url" in changes
    market_changed = "market.goods" in changes
    goods = goods_to_watch(reloaded.alerts)
    fourchan_post: Optional[FourChanPost] = None
    market_message: Optional[str] = None
    try:
        if thread_changed and reloaded.fourchan_thread is not None:
            fourchan_post = ClopClient(
                client.base_url, "", "", fourchan_thread=reloaded.fourchan_thread
            )._latest_fourchan_post()
        if market_changed and goods:
            market_message = client.market_preflight(goods)
    except MonitorError as error:
        # ArchivedThreadError arrives here too, and is deliberately not fatal: a thread that
        # is already archived when you name it is a typo in a text file, not the game telling
        # a running watch that its job is over.
        return refuse(
            f"The new settings.json could not be brought into service: {error}\n\n"
            f"None of it was applied. {still_polling}"
        )

    # From here nothing can fail, so this is the one contiguous region that changes anything.
    if market_changed and not goods:
        # market_preflight returns early with nothing to resolve, so it leaves these fields
        # as it found them; a reload that emptied the watch list has to clear them or the
        # monitor keeps POSTing once a poll for goods nobody watches.
        client.market_goods = ()
        client.alliance_id = None
    if thread_changed:
        client.fourchan_thread = reloaded.fourchan_thread
        # The thread's current last post becomes the baseline, exactly as at startup, so the
        # swap does not alert for a post that was already there when it was configured.
        client.initial_fourchan_post = fourchan_post
    if reloaded.sound != settings.sound:
        notifier = build_notifier(reloaded.sound)
    # A confirmation rather than a warning, so it stays terminal output: popups are reserved
    # for things that are wrong. What a preflight resolved follows in the words startup uses,
    # because the alliance it found and the post it baselined are decisions the reader cannot
    # see anywhere else, and they only appear on a reload that changed that section.
    print("Settings reloaded: " + ", ".join(changes) + ".", flush=True)
    if market_message is not None:
        print(market_message, flush=True)
    if fourchan_post is not None:
        print(fourchan_preflight_message(fourchan_post), flush=True)
    return LoadedSettings(reloaded, source), notifier


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
        # The bytes are read before the parse, so that a file edited in between reads as
        # changed on the next poll rather than as already loaded.
        settings_source = read_settings_source(settings_path)
        settings = load_settings(settings_path)
        env_file = load_env_file(args.env_file.resolve())
    except MonitorError as error:
        return report_fatal(notifier, str(error), 2)

    startup_message = settings_startup_message(settings, settings_path)
    if startup_message is not None:
        print(startup_message, flush=True)

    webhook_url = os.environ.get("CLOP_WEBHOOK_URL") or env_file.get("CLOP_WEBHOOK_URL")

    def build_notifier(sound: SoundSettings) -> Notifier:
        """The notifier for a set of sound settings, which a reload may replace."""
        return Notifier(
            desktop=not args.no_desktop_notifications,
            webhook_url=webhook_url,
            sound=sound,
        )

    notifier = build_notifier(settings.sound)
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
        print(fourchan_preflight_message(initial_fourchan_post), flush=True)

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
        market_message = client.market_preflight(goods_to_watch(settings.alerts))
        if market_message is not None:
            print(market_message, flush=True)
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

        # Sheet sync (buildings + stockpiles) is on whenever CLOP_NATION names a tab in the shared
        # sheet. Unset -> the monitor runs exactly as before; a missing/unreachable tab -> warn and
        # stay off rather than fail every poll.
        from sheets import GoogleSheet, SheetError, nation_from_env

        building_sheet: Optional[GoogleSheet] = None
        building_nation: Optional[str] = None
        try:
            building_nation = nation_from_env(args.env_file.resolve())
        except SheetError:
            print("Sheet sync off (CLOP_NATION not set).", flush=True)
        else:
            building_sheet = GoogleSheet()
            try:
                building_sheet.require_tab(building_nation)
                print(
                    f"Sheet sync on: reconciling buildings and snapshotting stockpiles for "
                    f"{building_nation!r} each poll before alerting.",
                    flush=True,
                )
            except SheetError as error:
                notifier.notify_failure(f"Sheet sync is off: {error}")
                building_sheet = None

        loaded = LoadedSettings(settings, settings_source)
        while True:
            # Before the check rather than after it, so that an edit takes effect on the very
            # next poll rather than the one after.
            loaded, notifier = reload_settings(
                settings_path, loaded, client, notifier, build_notifier
            )
            settings = loaded.settings
            poll_failed = False
            try:
                # Sheet sync is its own process that fires first, before the regular
                # message/news/report alerting. It handles and reports its own failures.
                if building_sheet is not None and building_nation is not None:
                    sync_sheet_step(client, building_sheet, building_nation, notifier)
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
                    f"{current.fourchan_post.number if current.fourchan_post else 'disabled'}, "
                    f"market_orders={len(current.market_orders)}",
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
