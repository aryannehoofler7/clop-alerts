#!/usr/bin/env python3
"""Reconcile a nation's building counts on overview.php against the shared sheet.

The flow, run as its own step before the monitor's regular alerting:

1. parse the overview "Buildings" panel (via ``overview.parse_panel``) into
   ``{game_name: (have, disabled)}`` (owned buildings
   only -- anything absent means zero);
2. fold those onto sheet building rows through ``building_map.GAME_TO_SHEET`` (summing the DNA and
   Energy Collector groups);
3. write only the sheet cells whose have/disabled count differs, in the have region (column B) and
   the disabled region (column B, below the ``DISABLED:`` marker);
4. report the corrections made so the caller can alert on them.

``sanity_check`` verifies the mappings and sheet structure still hold, so a reformatted sheet or a
renamed/new game building is caught rather than silently mis-written. This module never acts on the
game (no recycle/disable); it only reads overview and writes the sheet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from building_map import GAME_TO_SHEET, SHEET_BUILDINGS
from overview import parse_panel, require_valid_overview
from sheets import GoogleSheet, cell_int

#: Column A of a nation tab spans two regions inside these rows; read a little past the disabled
#: region so a longer sheet is still covered.
COLUMN_SCAN_RANGE = "A1:B130"

_COUNT_RE = re.compile(r"([\d,]+)\s*(?:\(\s*(\d+)\s*disabled\s*\))?", re.IGNORECASE)


class BuildingError(RuntimeError):
    """A building name overview reports that the mapping does not cover (new or renamed building)."""


@dataclass(frozen=True)
class Correction:
    building: str          # sheet building name
    field: str             # "have" or "disabled"
    old: int
    new: int

    def describe(self) -> str:
        return f"{self.building} {self.field} {self.old} -> {self.new}"


@dataclass(frozen=True)
class Regions:
    have_header: Optional[int]        # 0-based index of the "Building"/"Have" header row
    disabled_marker: Optional[int]    # 0-based index of the "DISABLED:" marker row
    have_rows: Dict[str, int]         # sheet building name -> 1-based row in the have region
    disabled_rows: Dict[str, int]     # sheet building name -> 1-based row in the disabled region


def parse_overview_buildings(html: str) -> Dict[str, Tuple[int, int]]:
    """Return ``{overview_name: (have, disabled)}`` for the owned buildings on overview.php.

    Raises ``BuildingError`` if a row's count is unreadable. Skipping the row would be far worse:
    ``desired_counts`` starts every building at zero, so a dropped row reconciles to 0, overwrites a
    correct cell, and is reported as an ordinary "Building counts corrected" popup -- indis-
    tinguishable from the player actually having demolished the lot. ``sanity_check`` cannot catch
    it either, because the row is already gone before it looks. This is the same rule
    ``stockpiles.parse_overview_resources`` follows for the Resources panel.
    """
    result: Dict[str, Tuple[int, int]] = {}
    for name, count_text in parse_panel(html, "Buildings"):
        match = _COUNT_RE.match(count_text)
        if not match or not match.group(1):
            raise BuildingError(
                f"building {name!r} has an unreadable count {count_text!r} on overview.php"
            )
        have = int(match.group(1).replace(",", ""))
        disabled = int(match.group(2)) if match.group(2) else 0
        result[name] = (have, disabled)
    return result


def desired_counts(
    overview: Dict[str, Tuple[int, int]], *, strict: bool = True
) -> Dict[str, Tuple[int, int]]:
    """Fold overview counts onto sheet building rows, summing the many-to-one groups.

    Every sheet building starts at ``(0, 0)``, so buildings the nation does not own reconcile to
    zero. With ``strict`` (the default), an overview name absent from ``GAME_TO_SHEET`` raises
    ``BuildingError``; ``strict=False`` skips it (``sanity_check`` reports it instead).
    """
    result: Dict[str, Tuple[int, int]] = {name: (0, 0) for name in SHEET_BUILDINGS}
    for game_name, (have, disabled) in overview.items():
        sheet_name = GAME_TO_SHEET.get(game_name)
        if sheet_name is None:
            if strict:
                raise BuildingError(
                    f"overview building {game_name!r} is not in the mapping (building_map.py)"
                )
            continue
        cur_have, cur_disabled = result[sheet_name]
        result[sheet_name] = (cur_have + have, cur_disabled + disabled)
    return result


def locate_regions(column_a: Sequence[object]) -> Regions:
    """Find the have- and disabled-region name->row maps from column A's values (0-based input)."""
    have_header: Optional[int] = None
    disabled_marker: Optional[int] = None
    for index, raw in enumerate(column_a):
        text = str(raw or "").strip()
        if have_header is None and text == "Building":
            have_header = index
        if disabled_marker is None and text.upper().startswith("DISABLED"):
            disabled_marker = index

    have_rows: Dict[str, int] = {}
    disabled_rows: Dict[str, int] = {}
    for index, raw in enumerate(column_a):
        text = str(raw or "").strip()
        if text not in SHEET_BUILDINGS:
            continue
        row = index + 1  # sheet rows are 1-based
        if disabled_marker is not None and index > disabled_marker:
            disabled_rows.setdefault(text, row)
        elif have_header is not None and index > have_header:
            have_rows.setdefault(text, row)
    return Regions(have_header, disabled_marker, have_rows, disabled_rows)


def _read_columns(sheet: GoogleSheet, nation: str) -> Tuple[List[object], List[object]]:
    grid = sheet.read(nation, COLUMN_SCAN_RANGE)
    column_a = [row[0] if len(row) > 0 else "" for row in grid]
    column_b = [row[1] if len(row) > 1 else "" for row in grid]
    return column_a, column_b


def reconcile(
    sheet: GoogleSheet, nation: str, overview: Dict[str, Tuple[int, int]]
) -> List[Correction]:
    """Write the sheet cells whose have/disabled count is wrong; return the corrections made."""
    column_a, column_b = _read_columns(sheet, nation)
    regions = locate_regions(column_a)
    desired = desired_counts(overview)

    corrections: List[Correction] = []
    for name in sorted(SHEET_BUILDINGS):
        want_have, want_disabled = desired[name]
        have_row = regions.have_rows.get(name)
        if have_row is not None:
            current = cell_int(column_b[have_row - 1])
            if current != want_have:
                sheet.write_cell(nation, f"B{have_row}", want_have)
                corrections.append(Correction(name, "have", current, want_have))
        disabled_row = regions.disabled_rows.get(name)
        if disabled_row is not None:
            current = cell_int(column_b[disabled_row - 1])
            if current != want_disabled:
                sheet.write_cell(nation, f"B{disabled_row}", want_disabled)
                corrections.append(Correction(name, "disabled", current, want_disabled))
    return corrections


def sanity_check(
    sheet: GoogleSheet, nation: str, overview: Dict[str, Tuple[int, int]]
) -> List[str]:
    """Return a list of structural/mapping problems (empty means the layout is trustworthy)."""
    column_a, _ = _read_columns(sheet, nation)
    regions = locate_regions(column_a)
    problems: List[str] = []

    if regions.have_header is None:
        problems.append("no 'Building' header found in column A")
    if regions.disabled_marker is None:
        problems.append("no 'DISABLED:' marker found in column A")

    # Every sheet building must appear exactly once in the have region.
    counts: Dict[str, int] = {}
    for index, raw in enumerate(column_a):
        text = str(raw or "").strip()
        if text in SHEET_BUILDINGS and (
            regions.disabled_marker is None or index < regions.disabled_marker
        ) and (regions.have_header is None or index > regions.have_header):
            counts[text] = counts.get(text, 0) + 1
    for name in sorted(SHEET_BUILDINGS):
        seen = counts.get(name, 0)
        if seen == 0:
            problems.append(f"sheet building {name!r} is missing from the have region")
        elif seen > 1:
            problems.append(f"sheet building {name!r} appears {seen} times in the have region")

    # Every building overview reports must be mapped, and a disabled count needs a disabled row.
    for game_name in sorted(overview):
        if game_name not in GAME_TO_SHEET:
            problems.append(f"overview building {game_name!r} is not in the mapping")
    for name, (_, want_disabled) in desired_counts(overview, strict=False).items():
        if want_disabled > 0 and name not in regions.disabled_rows:
            problems.append(
                f"building {name!r} has {want_disabled} disabled but no row in the disabled region"
            )
    return problems


def _standalone() -> int:
    """Login, fetch overview, run the sanity check read-only, and report. Returns an exit code."""
    import os
    import sys

    from clop_monitor import ClopClient, DEFAULT_BASE_URL, load_env_file, popup_failure
    from sheets import DEFAULT_ENV_PATH, startup_check

    env = load_env_file(DEFAULT_ENV_PATH)
    username = os.environ.get("CLOP_USERNAME") or env.get("CLOP_USERNAME")
    password = os.environ.get("CLOP_PASSWORD") or env.get("CLOP_PASSWORD")
    if not username or not password:
        popup_failure(
            "The building check could not run: CLOP_USERNAME / CLOP_PASSWORD are not set.\n\n"
            "Add them to .env beside this script."
        )
        return 1

    sheet, nation = startup_check()
    client = ClopClient(DEFAULT_BASE_URL, username, password)
    client.login()
    html = client._open("overview.php")
    require_valid_overview(html)
    overview = parse_overview_buildings(html)
    problems = sanity_check(sheet, nation, overview)
    if problems:
        print(f"Building mapping sanity check FAILED for {nation!r}:")
        for problem in problems:
            print(f"  - {problem}")
        popup_failure(
            f"The building mapping or sheet layout is wrong on tab {nation!r}, so the monitor "
            "will refuse to write the building rows. Fix the sheet, or building_map.py, so the "
            "names line up again.\n\n"
            + "\n".join(f"- {problem}" for problem in problems)
        )
        return 1
    print(f"Building mapping sanity check passed for {nation!r} "
          f"({len(overview)} owned buildings, {len(SHEET_BUILDINGS)} sheet rows).")
    return 0


if __name__ == "__main__":
    import sys

    from clop_monitor import MonitorError, popup_failure
    from overview import OverviewError
    from sheets import SheetError

    try:
        sys.exit(_standalone())
    except (BuildingError, MonitorError, OverviewError, SheetError) as error:
        # A dialog, not a terminal line: this script is what the monitor's own popups tell people
        # to run, so it must not fail in a way only a terminal-watcher would notice.
        popup_failure(f"The building check failed: {error}")
        sys.exit(1)
