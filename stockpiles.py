#!/usr/bin/env python3
"""Snapshot the nation's stockpiles for six goods onto the shared sheet.

The flow, run in the same step as the building reconciliation and off the same overview.php fetch:

1. read the overview "Resources" panel into ``{resource_name: qty}`` (a good the nation holds none
   of is simply absent from the page, and reads as zero);
2. read the CLOP **server** time out of the page header;
3. verify the sheet's ``Q11:Q16`` still names the six goods, in order;
4. write ``R11:R16`` and stamp ``W10`` with the server time.

Unlike ``buildings.py`` this is a **snapshot, not a reconciliation**: the values already in the sheet
are replaced rather than diffed and corrected, and a routine write is not an event worth alerting
on. Both cells are written on every run, even when nothing changed -- see ``snapshot``.

Rows are addressed by position, so ``check_labels`` runs first every time: if ``Q11:Q16`` has been
reordered or relabelled, nothing is written at all -- not even ``W10``, because a fresh timestamp
over stale numbers is worse than an obviously stale one.

See ``docs/superpowers/specs/2026-08-23-stockpile-snapshot-design.md``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from overview import parse_panel
from sheets import GoogleSheet

#: Sheet STOCK label (column Q) -> game resource name, **in sheet row order**: the list index is the
#: offset from ``STOCK_FIRST_ROW``, so the order is data, not decoration.
#:
#: The game names are ``resourcedefs.name`` for the non-building rows, from the game's seed data
#: (``clop/tables with data.sql``). The two that are not guessable from the sheet's abbreviation are
#: ``mpart`` -> ``Machinery Parts`` (resource_id 10) and ``vpart`` -> ``Vehicle Parts`` (id 9); they
#: are the only two candidates in the table.
STOCK_ROWS: List[Tuple[str, str]] = [
    ("apple", "Apples"),
    ("oil", "Oil"),
    ("coffee", "Coffee"),
    ("mpart", "Machinery Parts"),
    ("vpart", "Vehicle Parts"),
    ("gems", "Gems"),
]

#: The stock block sits at rows 11-16 of a nation tab: Q = label, R = HAVE.
STOCK_FIRST_ROW = 11
STOCK_LAST_ROW = STOCK_FIRST_ROW + len(STOCK_ROWS) - 1

LABEL_RANGE = f"Q{STOCK_FIRST_ROW}:Q{STOCK_LAST_ROW}"
VALUE_RANGE = f"R{STOCK_FIRST_ROW}:R{STOCK_LAST_ROW}"

#: Where the "when was this taken" stamp goes. Empty in the sheet's layout; beside the STOCK header.
TIMESTAMP_CELL = "W10"

_SERVER_TIME_RE = re.compile(r"Server time:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


class StockpileError(RuntimeError):
    """The overview page is not what the snapshot expects.

    Raised for a missing server-time stamp or an unreadable quantity. Both mean the same thing to
    the caller: do not write anything to the sheet, and tell someone.
    """


def parse_overview_resources(html: str) -> Dict[str, int]:
    """Return ``{resource_name: qty}`` from the overview "Resources" panel.

    Only resources the nation has a row for are rendered, so a good it holds none of is absent from
    the result rather than present as zero; ``desired_stock`` is what turns that into a zero.

    Raises ``StockpileError`` if a row's Qty is not a plain integer. Skipping such a row would be
    the more dangerous choice: the good would fall through to zero and be written to the sheet as
    "you hold none", stamped freshly verified. The game formats every Qty with its integer
    ``commas()`` helper, so anything else means the page changed and a human should look.
    """
    result: Dict[str, int] = {}
    for name, value_text in parse_panel(html, "Resources"):
        text = value_text.replace(",", "").strip()
        if not re.fullmatch(r"-?\d+", text):
            raise StockpileError(
                f"resource {name!r} has an unreadable quantity {value_text!r} on overview.php"
            )
        result[name] = int(text)
    return result


def desired_stock(resources: Dict[str, int]) -> List[int]:
    """Return the six quantities in sheet row order; a good absent from overview is zero."""
    return [resources.get(game_name, 0) for _, game_name in STOCK_ROWS]


def parse_server_time(html: str) -> str:
    """Return the ``YYYY-MM-DD HH:MM:SS`` stamp from the page header, exactly as rendered.

    This is the game server's own clock (``date("Y-m-d H:i:s")`` in ``clop/header.php``), shown on
    every logged-in page. It goes into the sheet unparsed and unconverted, so the sheet shows the
    same wall-clock the game shows and no timezone has to be assumed anywhere.

    Raises ``StockpileError`` when the stamp is absent: a page without it is not the page we think
    it is, and a snapshot with no staleness marker is worse than no snapshot.
    """
    match = _SERVER_TIME_RE.search(html)
    if not match:
        raise StockpileError(
            "no 'Server time:' stamp on the page -- is this a logged-in CLOP page?"
        )
    return match.group(1)
