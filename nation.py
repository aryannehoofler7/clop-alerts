#!/usr/bin/env python3
"""The nation's status line on overview.php, parsed once into a reusable object.

    status = NationStatus.from_overview(html)
    status.satisfaction        # Reading(current=218, per_tick=-5)
    status.se                  # Reading(current=1500, per_tick="Ascending")
    status.gdp, status.funds   # 60900, 1234567
    status.server_time         # "2026-08-23 11:42:12"

``Reading.per_tick`` is an ``int`` normally, and otherwise the literal string the game printed.
Two governments make that necessary: ``backend_overview.php:320-325`` sets the per-tick to
``"Fixed"`` under Solar Vassal / Lunar Client, and ``overview.php:50-60`` prints ``(Ascending)``
under Alicorn Elite / Transponyism. Passing those through verbatim keeps the sheet showing what the
game shows rather than inventing a number for a nation whose relations do not tick.

Rows are matched by their label rather than by position, so the conditional ``Warning:`` row the
game inserts when ``active_economy`` is false is ignored for free.

This module knows the game's vocabulary and nothing about the sheet. See ``stockpiles.py`` for the
writing, and ``goods.py`` for the other half of one page fetch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Union

from overview import parse_panel_text

#: The clock the game prints on every logged-in page (``date("Y-m-d H:i:s")`` in clop/header.php).
_SERVER_TIME_RE = re.compile(r"Server time:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

#: "<current> (<per tick>)", the shape of the satisfaction and relationship cells.
_READING_RE = re.compile(r"^(-?[\d,]+)\s*\((.+?)\)$")

_INT_RE = re.compile(r"-?\d+")

_PER_TICK_SUFFIX = " per tick"


class NationStatusError(RuntimeError):
    """The Nation panel is missing a row, or a value is not the shape the game renders.

    A corrupted number counts: the game's ``commas()`` helper (``clop/backend/allfunctions.php``
    lines 26-31) walks the *string* form of a number inserting a comma every three characters from
    the right, so a fractional GDP arrives as ``5,062,25.``. Refusing it is the point -- a mangled
    number must never reach a sheet nine people read.
    """


@dataclass(frozen=True)
class Reading:
    """A current value and what it changes by each tick."""

    current: int
    per_tick: Union[int, str]

    def display(self) -> str:
        """The sheet's format: ``"218 (-5)"``, or ``"1500 (Ascending)"``."""
        return f"{self.current} ({self.per_tick})"


def parse_server_time(html: str) -> str:
    """Return the ``YYYY-MM-DD HH:MM:SS`` stamp from the page header, exactly as rendered.

    It goes onto the sheet unparsed and unconverted, so the sheet shows the same wall-clock the game
    shows and no timezone has to be assumed anywhere.

    Raises ``NationStatusError`` when the stamp is absent: a page without it is not the page we
    think it is, and a snapshot with no staleness marker is worse than no snapshot.
    """
    match = _SERVER_TIME_RE.search(html)
    if not match:
        raise NationStatusError(
            "no 'Server time:' stamp on the page -- this is not a normal CLOP page "
            "(an error or maintenance page, or a truncated response?)"
        )
    return match.group(1)


def _to_int(text: str, field: str) -> int:
    cleaned = text.replace(",", "").strip()
    if not _INT_RE.fullmatch(cleaned):
        raise NationStatusError(
            f"{field} reads {text!r} on overview.php, which is not a whole number"
        )
    return int(cleaned)


def _reading(text: str, field: str) -> Reading:
    match = _READING_RE.match(text.strip())
    if not match:
        raise NationStatusError(
            f"{field} reads {text!r} on overview.php, which is not '<value> (<per tick>)'"
        )
    inner = match.group(2).strip()
    if inner.endswith(_PER_TICK_SUFFIX):
        inner = inner[: -len(_PER_TICK_SUFFIX)].strip()
    cleaned = inner.replace(",", "")
    per_tick: Union[int, str] = int(cleaned) if _INT_RE.fullmatch(cleaned) else inner
    return Reading(_to_int(match.group(1), field), per_tick)


def _suffixed_int(text: str, suffix: str, field: str) -> int:
    value = text.strip()
    if value.endswith(suffix):
        value = value[: -len(suffix)].strip()
    return _to_int(value, field)


def _row(rows: Dict[str, str], label: str) -> str:
    if label not in rows:
        raise NationStatusError(f"the Nation panel on overview.php has no {label!r} row")
    return rows[label]


@dataclass(frozen=True)
class NationStatus:
    """Everything the Dashboard's status rows need, from one overview.php fetch."""

    government: str
    economy: str
    satisfaction: Reading
    se: Reading                 # Relationship with Solar Empire
    nlr: Reading                # Relationship with New Lunar Republic
    gdp: int
    funds: int
    server_time: str

    @classmethod
    def from_overview(cls, html: str) -> "NationStatus":
        """Parse the Nation panel and the page header.

        Raises ``NationStatusError`` on a missing row or a value that is not the shape the game
        renders.
        """
        rows: Dict[str, str] = dict(parse_panel_text(html, "Nation"))
        return cls(
            government=_row(rows, "Government Type"),
            economy=_row(rows, "Economic Type"),
            satisfaction=_reading(_row(rows, "Satisfaction"), "Satisfaction"),
            se=_reading(_row(rows, "Relationship with Solar Empire"), "Solar Empire relationship"),
            nlr=_reading(
                _row(rows, "Relationship with New Lunar Republic"),
                "New Lunar Republic relationship",
            ),
            gdp=_suffixed_int(_row(rows, "GDP"), " bits per tick", "GDP"),
            funds=_suffixed_int(_row(rows, "Funds"), " bits", "Funds"),
            server_time=parse_server_time(html),
        )
