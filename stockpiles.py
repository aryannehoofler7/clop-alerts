#!/usr/bin/env python3
"""Snapshot the nation's stockpiles and status onto the shared sheet -- both tabs, one page fetch.

Two regions are written from one read of ``overview.php``:

1. the player's own **nation tab** -- the six goods in the ``STOCK`` block's ``HAVE`` column, plus
   the timestamp beside the header;
2. the alliance-wide **Dashboard-Stockpile tab** -- the nation's own column: all 31 goods, six
   status rows (``Active``, ``Sat``, ``NLR``, ``SE``, ``GDP``, ``Bits``), and six ``<good> - tick``
   rows carrying the Resources panel's Ticks-Worth column.

The parsing lives elsewhere and happens once: ``goods.Stockpiles`` and ``nation.NationStatus`` are
handed in already built, so nothing here re-fetches or re-parses the page.

**Every target cell is found by looking a label up.** No row number or column letter is hardcoded:
the nation tab's block is found from its ``STOCK`` header and the value column from the ``HAVE``
header beside it; the Dashboard's column is found from the nation's name in row 1 and its rows from
the labels in column A. The one exception is the nation tab's timestamp cell, which has no label to
anchor on -- its *row* is looked up and its column ``W`` is convention, so the write is guarded (see
``timestamp_problem``).

This is a **snapshot, not a reconciliation**: values in the sheet are replaced rather than diffed
and corrected, and a routine write is not an event worth alerting on. An earlier draft compared
against the sheet first and skipped unchanged blocks, but an unreadable cell (``#REF!``, a stray
label) normalises to ``0`` and so compares equal for a good the nation holds none of -- leaving the
garbage in place while the timestamp declared the row freshly verified. Overwriting always costs one
call per run and removes that hole.

The two regions are **independent**: a layout problem on one is reported and skipped without
stopping the other. A transport failure is not a layout problem and aborts both, deliberately -- it
means the shared connection is down, so retrying the other half would only produce a second popup
about the same outage.

This module never acts on the game -- it only reads overview.php and writes the sheet.

See ``docs/superpowers/specs/2026-08-23-dashboard-sync-design.md`` and, for the goods table,
``docs/2026-08-23-dashboard-goods-map.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from goods import (  # re-exported: clop_monitor and the diagnostics import them from here
    BY_STOCK_LABEL,
    BY_TICK_LABEL,
    GOODS,
    TICKS_HEADING,
    StockpileError,
    Stockpiles,
    parse_overview_resources,
)
from nation import (  # re-exported for the same reason
    NationStatus,
    NationStatusError,
    Reading,
    parse_server_time,
)
from sheets import GoogleSheet, column_letter, find_in_row, index_column

#: Column Q onward of a nation tab. Q holds the labels, the header row holds HAVE/NEED/BUY, and W
#: holds the timestamp; read to row 60 so a longer sheet is still covered.
NATION_SCAN_RANGE = "Q1:W60"

#: 0-based index of column Q, so an offset within a scanned row converts back to a column letter.
NATION_FIRST_COLUMN = 16

STOCK_HEADER = "STOCK"
HAVE_HEADER = "HAVE"

#: The timestamp's column. Its row is looked up; this is the one address left to convention,
#: because the cell has no label. ``timestamp_problem`` guards the write because of that.
TIMESTAMP_COLUMN = "W"

#: 0-based offset of column W within a NATION_SCAN_RANGE row (Q=0, R=1, ... W=6).
_TIMESTAMP_OFFSET = 6

#: The alliance-wide tab. Renamed from "Dashboard" on 2026-08-24; the tab name is the only thing
#: about that sheet this module hardcodes, because it is the one thing no lookup can find for us.
DASHBOARD_TAB = "Dashboard-Stockpile"

#: Row 1 holds the nation names, column A the row labels; 80 rows covers the block with room.
DASHBOARD_SCAN_RANGE = "A1:Z80"

#: The tab's non-goods rows. ``Active`` holds a last-updated timestamp despite its label -- the
#: sheet owner's instruction, recorded so it is not "fixed".
STATUS_LABELS: Tuple[str, ...] = ("Active", "Sat", "NLR", "SE", "GDP", "Bits")

#: The "<good> - tick" rows: how many ticks the nation's stock of each lasts. Same six goods as the
#: nation tab's STOCK block, under labels of their own again.
TICK_LABELS: Tuple[str, ...] = tuple(good.tick_label for good in GOODS if good.tick_label)

#: Every label this module expects in column A: 6 status + 6 tick + 31 goods.
DASHBOARD_LABELS: Tuple[str, ...] = (
    STATUS_LABELS + TICK_LABELS + tuple(good.dashboard_label for good in GOODS)
)

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@dataclass(frozen=True)
class NationBlock:
    """Where the nation tab's STOCK block turned out to be."""

    header_row: int             # 1-based row of the STOCK header
    value_column: str           # A1 letter of the HAVE column
    rows: Dict[str, int]        # stock_label -> 1-based row
    timestamp_cell: str         # e.g. "W10"


@dataclass(frozen=True)
class DashboardBlock:
    """Where the nation's column and the labelled rows turned out to be on the Dashboard."""

    column: str                 # A1 letter of this nation's column
    rows: Dict[str, int]        # dashboard label -> 1-based row


def _column_index(letters: str) -> int:
    """Inverse of ``sheets.column_letter``: ``"A" -> 0``, ``"AA" -> 26``.

    Used only by the diagnostic, to read back the column the ``HAVE`` lookup resolved to.
    """
    index = 0
    for character in letters:
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index - 1


def _cell(grid: List[List[Any]], row_index: int, column_index: int) -> Any:
    """Read one cell out of a ragged grid, tolerating short or missing rows."""
    if 0 <= row_index < len(grid):
        row = grid[row_index]
        if 0 <= column_index < len(row):
            return row[column_index]
    return None


def as_sheet_text(value: str) -> str:
    """Mark ``value`` so the sheet stores it as text rather than parsing it.

    Left to itself, Sheets reads ``2026-08-23 07:12:30`` as a date and stores a date value -- which
    silently reinterprets the game's clock as being in the *spreadsheet's* timezone. The whole point
    of copying the server's stamp across verbatim is that we do not know or assume which timezone
    the game runs in, so a staleness formula built on a mis-tagged date would be wrong by that
    offset without ever looking wrong.

    A leading apostrophe is Sheets' own force-text marker. It is consumed when the value is stored,
    so the cell shows exactly what the game printed.
    """
    return "'" + value


def timestamp_problem(cell: str, current: Any) -> Optional[str]:
    """Return a problem string if ``cell`` holds something that is not a timestamp.

    The timestamp cell is the one address not fully looked up -- only its row is. This guard is what
    makes that safe: if the sheet has been rearranged so column ``W`` of the header row now means
    something else, the write is refused instead of clobbering it.
    """
    text = "" if current is None else str(current).strip()
    if text and not _TIMESTAMP_RE.match(text):
        return f"{cell} should be empty or hold a 'YYYY-MM-DD HH:MM:SS' stamp but reads {text!r}"
    return None


def locate_nation_block(grid: List[List[Any]]) -> Tuple[Optional[NationBlock], List[str]]:
    """Find the STOCK block in a ``NATION_SCAN_RANGE`` grid. Returns ``(block, problems)``.

    A non-empty ``problems`` means ``block`` is ``None`` and nothing on that tab may be written.

    The label run stops at the first blank row. That is not tidiness -- column Q holds a *second*
    label block lower down (``COST``, then ``Copper``/``M Part``/``V Part``/``P Part``), so an
    unbounded search would be ambiguous.
    """
    problems: List[str] = []

    header_index: Optional[int] = None
    for index, row in enumerate(grid):
        cell = row[0] if len(row) > 0 else None
        if cell is not None and str(cell).strip() == STOCK_HEADER:
            header_index = index
            break
    if header_index is None:
        problems.append(f"no {STOCK_HEADER!r} header found in column Q of the nation tab")
        return None, problems

    offset = find_in_row(grid[header_index], HAVE_HEADER)
    if offset is None:
        problems.append(
            f"no {HAVE_HEADER!r} column found in row {header_index + 1} beside the "
            f"{STOCK_HEADER!r} header"
        )
        return None, problems

    seen: Dict[str, List[int]] = {}
    for index in range(header_index + 1, len(grid)):
        cell = _cell(grid, index, 0)
        label = "" if cell is None else str(cell).strip()
        if not label:
            break
        seen.setdefault(label, []).append(index + 1)

    rows: Dict[str, int] = {}
    for stock_label in sorted(BY_STOCK_LABEL):
        places = seen.get(stock_label, [])
        if not places:
            problems.append(
                f"stock label {stock_label!r} is missing from the run under the "
                f"{STOCK_HEADER!r} header on the nation tab"
            )
        elif len(places) > 1:
            places_text = ", ".join(str(place) for place in places)
            problems.append(
                f"stock label {stock_label!r} appears twice or more under the "
                f"{STOCK_HEADER!r} header on the nation tab (rows {places_text})"
            )
        else:
            rows[stock_label] = places[0]

    if problems:
        return None, problems
    return (
        NationBlock(
            header_row=header_index + 1,
            value_column=column_letter(NATION_FIRST_COLUMN + offset),
            rows=rows,
            timestamp_cell=f"{TIMESTAMP_COLUMN}{header_index + 1}",
        ),
        problems,
    )


def locate_dashboard_block(
    grid: List[List[Any]], nation: str
) -> Tuple[Optional[DashboardBlock], List[str]]:
    """Find this nation's column and all 37 labelled rows. Returns ``(block, problems)``.

    A non-empty ``problems`` means ``block`` is ``None`` and nothing on the Dashboard may be
    written. Every problem is collected, not just the first, so one dialog shows a person the whole
    list.

    The nation's name is matched exactly against row 1. The tab is shared, so the failure worth
    spelling out is "your tab is called something else": the message names the nation and prints
    row 1, which is where the answer is.
    """
    problems: List[str] = []

    header = grid[0] if grid else []
    offset = find_in_row(header, nation)
    if offset is None:
        shown = ", ".join(
            str(cell).strip() for cell in header if cell is not None and str(cell).strip()
        )
        problems.append(
            f"no column in row 1 of the {DASHBOARD_TAB} tab is named {nation!r}. "
            f"Row 1 reads: {shown or '(empty)'}"
        )
        return None, problems

    found = index_column(grid)
    rows: Dict[str, int] = {}
    for label in DASHBOARD_LABELS:
        places = found.get(label, [])
        if not places:
            problems.append(
                f"label {label!r} is missing from column A of the {DASHBOARD_TAB} tab"
            )
        elif len(places) > 1:
            places_text = ", ".join(str(place) for place in places)
            problems.append(
                f"label {label!r} appears {len(places)} times in column A of the "
                f"{DASHBOARD_TAB} tab (rows {places_text})"
            )
        else:
            rows[label] = places[0]

    if problems:
        return None, problems
    return DashboardBlock(column=column_letter(offset), rows=rows), problems


def contiguous_runs(rows: Dict[str, int]) -> List[List[Tuple[int, str]]]:
    """Group ``{label: row}`` into ascending runs of consecutive rows.

    One block write per run is what keeps the sheet's spacer rows untouched: they are not in
    ``rows``, so they fall between runs rather than inside one.
    """
    runs: List[List[Tuple[int, str]]] = []
    for row, label in sorted((row, label) for label, row in rows.items()):
        if runs and row == runs[-1][-1][0] + 1:
            runs[-1].append((row, label))
        else:
            runs.append([(row, label)])
    return runs


def status_values(status: NationStatus) -> Dict[str, Any]:
    """Map the Dashboard's six status labels to the values to write.

    Readings go in as one combined text cell each, exactly as the game displays them -- summing a
    relationship across nine nations would be meaningless. GDP and Funds go in as real numbers, so
    the Dashboard's ``TOTAL`` column can add them up.

    ``Active`` holds the server clock despite its label; see ``STATUS_LABELS``.
    """
    return {
        "Active": as_sheet_text(status.server_time),
        "Sat": status.satisfaction.display(),
        "NLR": status.nlr.display(),
        "SE": status.se.display(),
        "GDP": status.gdp,
        "Bits": status.funds,
    }


def dashboard_values(
    stock: Stockpiles,
    status: NationStatus,
    rows: Dict[str, int],
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Return ``(values, writable_rows)`` for the alliance tab.

    ``writable_rows`` is ``rows`` minus anything there is no value for. In practice that is only
    the ``- tick`` block, and only when overview.php had no ``Ticks-Worth`` column: those rows are
    then left exactly as they are rather than being zeroed, because "we could not read it" and
    "you have none" are different claims and only one of them is true.
    """
    values: Dict[str, Any] = status_values(status)
    for good in GOODS:
        values[good.dashboard_label] = stock.get(good.game_name)
    if stock.ticks_worth is not None:
        for label, good in BY_TICK_LABEL.items():
            values[label] = stock.ticks(good.game_name)
    return values, {label: row for label, row in rows.items() if label in values}


def _write_runs(
    sheet: GoogleSheet,
    tab: str,
    column: str,
    rows: Dict[str, int],
    values: Dict[str, Any],
) -> List[Tuple[str, List[Any]]]:
    """Write ``values`` into ``column`` at ``rows``, one block per contiguous run.

    Returns ``[(a1, [value, ...]), ...]`` describing what was sent, for the caller's report.
    """
    written: List[Tuple[str, List[Any]]] = []
    for run in contiguous_runs(rows):
        a1 = f"{column}{run[0][0]}:{column}{run[-1][0]}"
        block = [values[label] for _row, label in run]
        sheet.write(tab, a1, [[value] for value in block])
        written.append((a1, block))
    return written


@dataclass(frozen=True)
class Report:
    """What a ``snapshot`` actually wrote, for the standalone diagnostic to print."""

    nation_writes: List[Tuple[str, List[Any]]] = field(default_factory=list)
    dashboard_writes: List[Tuple[str, List[Any]]] = field(default_factory=list)
    timestamp: Optional[str] = None


def snapshot(
    sheet: GoogleSheet,
    nation: str,
    stock: Stockpiles,
    status: NationStatus,
) -> Tuple[Report, List[str]]:
    """Write both sheet regions from one already-parsed page. Returns ``(report, problems)``.

    Each region is located, then written only if its lookup was clean. A region with problems is
    skipped entirely -- for the nation tab that includes its timestamp, because a fresh stamp over
    stale numbers is worse than an obviously stale one. The other region still runs: they guard
    different parts of the sheet and one being unwritable says nothing about the other.

    Writes are unconditional; see the module docstring for why diff-and-skip was rejected.

    A ``SheetError`` from the endpoint propagates. That is not a layout problem -- it means the
    connection is down, and the caller aborts rather than retrying the other half.
    """
    problems: List[str] = []
    nation_writes: List[Tuple[str, List[Any]]] = []
    dashboard_writes: List[Tuple[str, List[Any]]] = []
    stamped: Optional[str] = None

    nation_grid = sheet.read(nation, NATION_SCAN_RANGE)
    block, nation_problems = locate_nation_block(nation_grid)
    problems.extend(nation_problems)
    if block is not None:
        current_stamp = _cell(nation_grid, block.header_row - 1, _TIMESTAMP_OFFSET)
        stamp_problem = timestamp_problem(block.timestamp_cell, current_stamp)
        if stamp_problem:
            problems.append(stamp_problem)
        else:
            values = {
                good.stock_label: stock.get(good.game_name)
                for good in GOODS
                if good.stock_label
            }
            nation_writes = _write_runs(sheet, nation, block.value_column, block.rows, values)
            # After the values, never before: it can then never claim freshness for a failed write.
            sheet.write_cell(nation, block.timestamp_cell, as_sheet_text(status.server_time))
            stamped = status.server_time

    dashboard_grid = sheet.read(DASHBOARD_TAB, DASHBOARD_SCAN_RANGE)
    dashboard, dashboard_problems = locate_dashboard_block(dashboard_grid, nation)
    problems.extend(dashboard_problems)
    if dashboard is not None:
        values, rows = dashboard_values(stock, status, dashboard.rows)
        if len(rows) < len(dashboard.rows):
            # Only the tick rows can be dropped, and only when the page had no such column.
            problems.append(
                f"overview.php has no {TICKS_HEADING} column, so the "
                f"{len(dashboard.rows) - len(rows)} '- tick' rows were left alone. "
                "Everything else on the tab was written."
            )
        dashboard_writes = _write_runs(sheet, DASHBOARD_TAB, dashboard.column, rows, values)

    return Report(nation_writes, dashboard_writes, stamped), problems


def _standalone() -> int:
    """Login, read overview, and report what a real run would write. Writes nothing."""
    import os

    from clop_monitor import ClopClient, DEFAULT_BASE_URL, load_env_file, popup_failure
    from overview import require_valid_overview
    from sheets import DEFAULT_ENV_PATH, startup_check

    env = load_env_file(DEFAULT_ENV_PATH)
    username = os.environ.get("CLOP_USERNAME") or env.get("CLOP_USERNAME")
    password = os.environ.get("CLOP_PASSWORD") or env.get("CLOP_PASSWORD")
    if not username or not password:
        popup_failure(
            "The stock check could not run: CLOP_USERNAME / CLOP_PASSWORD are not set.\n\n"
            "Add them to .env beside this script."
        )
        return 1

    sheet, nation = startup_check()
    client = ClopClient(DEFAULT_BASE_URL, username, password)
    client.login()
    html = client._open("overview.php")
    require_valid_overview(html)
    stock = Stockpiles.from_overview(html)
    status = NationStatus.from_overview(html)

    nation_grid = sheet.read(nation, NATION_SCAN_RANGE)
    block, nation_problems = locate_nation_block(nation_grid)
    dashboard_grid = sheet.read(DASHBOARD_TAB, DASHBOARD_SCAN_RANGE)
    dashboard, dashboard_problems = locate_dashboard_block(dashboard_grid, nation)
    problems = nation_problems + dashboard_problems

    print(f"{'server time (game)':<22}{status.server_time}")

    # Printing the resolved addresses is the point of this report: when the sheet has been
    # rearranged, this is what shows a person where the script now thinks each block is.
    print(f"\n--- nation tab {nation!r} ---")
    if block is None:
        print("  could not be located; see the problems below.")
    else:
        stamp = _cell(nation_grid, block.header_row - 1, _TIMESTAMP_OFFSET)
        print(f"  {STOCK_HEADER} header row  {block.header_row}")
        print(f"  {HAVE_HEADER} column      {block.value_column}")
        print(f"  timestamp cell     {block.timestamp_cell} = {stamp!r}")
        value_offset = _column_index(block.value_column) - NATION_FIRST_COLUMN
        print(f"\n  {'cell':<7}{'label':<10}{'game resource':<20}{'overview':>12}{'sheet':>12}")
        for stock_label, row in sorted(block.rows.items(), key=lambda item: item[1]):
            good = BY_STOCK_LABEL[stock_label]
            stored = _cell(nation_grid, row - 1, value_offset)
            print(
                f"  {block.value_column + str(row):<7}{stock_label:<10}"
                f"{good.game_name:<20}{stock.get(good.game_name):>12}"
                f"{'' if stored is None else str(stored):>12}"
            )

    print(f"\n--- {DASHBOARD_TAB} tab ---")
    if dashboard is None:
        print("  could not be located; see the problems below.")
    else:
        print(f"  column for {nation!r}: {dashboard.column}")
        values, writable = dashboard_values(stock, status, dashboard.rows)
        print(f"\n  {'cell':<8}{'label':<26}{'would write':>26}")
        for label, row in sorted(dashboard.rows.items(), key=lambda item: item[1]):
            cell = f"{dashboard.column}{row}"
            shown = str(values[label]) if label in writable else "(left alone)"
            print(f"  {cell:<8}{label:<26}{shown:>26}")

    if problems:
        print(f"\nProblems ({len(problems)}) -- a real run would skip the affected region:")
        for problem in problems:
            print(f"  - {problem}")
        popup_failure(
            "The sheet layout is not what the stockpile snapshot expects, so it would skip the "
            "affected region.\n\n"
            + "\n".join(f"- {problem}" for problem in problems)
        )
        return 1

    print("\nBoth regions located. This is a read-only report -- it never writes to the sheet.")
    return 0


if __name__ == "__main__":
    import sys

    from clop_monitor import MonitorError, popup_failure
    from overview import OverviewError
    from sheets import SheetError

    try:
        sys.exit(_standalone())
    except (
        StockpileError,
        NationStatusError,
        MonitorError,
        OverviewError,
        SheetError,
    ) as error:
        # A dialog, not a terminal line: this script is what the monitor's own popups tell people
        # to run, so it must not fail in a way only a terminal-watcher would notice.
        popup_failure(f"The stock check failed: {error}")
        sys.exit(1)
