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

This module never acts on the game -- it only reads overview.php and writes the sheet.

See ``docs/superpowers/specs/2026-08-23-stockpile-snapshot-design.md``.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from overview import parse_panel, require_valid_overview
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
            "no 'Server time:' stamp on the page -- this is not a normal CLOP page "
            "(an error or maintenance page, or a truncated response?)"
        )
    return match.group(1)


def check_labels(sheet: GoogleSheet, nation: str) -> List[str]:
    """Confirm ``Q11:Q16`` still names the six goods, in order.

    Returns the list of problems; empty means the block is safe to write. A non-empty list names
    each offending cell and must stop **all** writing, ``W10`` included -- the rows are addressed by
    position, so a moved label means this module no longer knows which row is which.

    Every problem is reported, not just the first, so one popup shows a person everything they need
    to put right.

    Only the labels are read. The current ``R`` values are deliberately not consulted: they are
    overwritten unconditionally, so there is nothing to compare them against.
    """
    grid = sheet.read(nation, LABEL_RANGE)
    problems: List[str] = []
    for index, (label, _) in enumerate(STOCK_ROWS):
        row = grid[index] if index < len(grid) else []
        cell = row[0] if len(row) > 0 else None
        found = "" if cell is None else str(cell).strip()
        if found.lower() != label.lower():
            problems.append(
                f"Q{STOCK_FIRST_ROW + index} should read {label!r} but reads {found!r}"
            )
    return problems


def snapshot(
    sheet: GoogleSheet,
    nation: str,
    resources: Dict[str, int],
    server_time: str,
) -> List[Tuple[str, int]]:
    """Write ``R11:R16`` and the ``W10`` stamp; return the six ``(label, qty)`` pairs recorded.

    Both writes are unconditional. An earlier draft compared against the sheet's current values and
    skipped the block write when they already matched, but an unreadable cell (``#REF!``, a stray
    label) normalises to ``0`` and so would compare equal for a good the nation holds none of --
    leaving the garbage in place while ``W10`` declared the row freshly verified. Overwriting always
    costs one endpoint call and removes that hole.

    ``W10`` therefore reads as *last verified* rather than *last changed*: an old stamp means the
    snapshot has stopped running, not merely that nothing has moved. It is written **after** the
    values, so it can never claim freshness for a block write that failed. The reverse partial
    failure -- values written, stamp not -- leaves fresh numbers under an old stamp, which
    understates freshness rather than overstating it, and the next run repairs it.

    The caller must have run ``check_labels`` and got no problems. This function trusts the rows.
    """
    wanted = desired_stock(resources)
    sheet.write(nation, VALUE_RANGE, [[value] for value in wanted])
    sheet.write_cell(nation, TIMESTAMP_CELL, server_time)
    return [(label, value) for (label, _), value in zip(STOCK_ROWS, wanted)]


def _standalone() -> int:
    """Login, read overview, and report the six quantities and the label check. Writes nothing."""
    import os

    from clop_monitor import ClopClient, DEFAULT_BASE_URL, load_env_file
    from sheets import DEFAULT_ENV_PATH, startup_check

    env = load_env_file(DEFAULT_ENV_PATH)
    username = os.environ.get("CLOP_USERNAME") or env.get("CLOP_USERNAME")
    password = os.environ.get("CLOP_PASSWORD") or env.get("CLOP_PASSWORD")
    if not username or not password:
        print("CLOP_USERNAME / CLOP_PASSWORD are not set (see .env).")
        return 1

    sheet, nation = startup_check()
    client = ClopClient(DEFAULT_BASE_URL, username, password)
    client.login()
    html = client._open("overview.php")
    require_valid_overview(html)
    wanted = desired_stock(parse_overview_resources(html))
    problems = check_labels(sheet, nation)

    # The snapshot itself never reads these; they are shown here only so a human can see what a run
    # would change. One extra read is fine in a diagnostic invoked by hand.
    # Shown as text, not normalised through cell_int: a diagnostic should surface whatever is
    # actually in the cell (including junk) rather than launder it into a plausible number.
    stored = [str(row[0]) if row and row[0] is not None else "" for row in
              sheet.read(nation, VALUE_RANGE)]
    stored += [""] * (len(STOCK_ROWS) - len(stored))

    print(f"Server time: {parse_server_time(html)}")
    if problems:
        print(f"\nStock label check FAILED for {nation!r}:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nThe row labels below may therefore be pointing at the wrong rows.")

    print(f"\n{'row':<5}{'label':<8}{'game resource':<18}{'overview':>12}{'sheet':>12}")
    for index, ((label, game_name), want) in enumerate(zip(STOCK_ROWS, wanted)):
        row = f"R{STOCK_FIRST_ROW + index}"
        print(f"{row:<5}{label:<8}{game_name:<18}{want:>12}{stored[index]:>12}")
    # Both columns are printed raw so they line up digit for digit -- comma-formatting one side of a
    # comparison makes an already-correct row look like a difference.
    legend = "\n'overview' is what the game reports; 'sheet' is what the sheet holds right now."
    if not problems:
        # Only true when the labels check out: a failed check means a real run writes nothing.
        legend += "\nA difference between them is normal -- it is what a real run would write."
    print(legend)

    if problems:
        return 1
    print(f"\nStock label check passed for {nation!r}. "
          "This is a read-only report -- it never writes to the sheet.")
    return 0


if __name__ == "__main__":
    import sys

    from overview import OverviewError
    from sheets import SheetError

    try:
        sys.exit(_standalone())
    except (StockpileError, OverviewError, SheetError) as error:
        print(f"Stock check failed: {error}", file=sys.stderr)
        sys.exit(1)
