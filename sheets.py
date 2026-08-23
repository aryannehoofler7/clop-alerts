#!/usr/bin/env python3
"""Keyless read/write access to the shared CLOP planning Google Sheet.

The sheet is shared ("anyone with the link can edit"), and so is this tool: every member who
clones the repo can read and update it with no Google account, no credential file, and no setup.

Writes to Google Sheets always require an identity *somewhere* -- API keys are read-only. We keep
that identity out of the repo by routing through an Apps Script web app bound to the sheet and
deployed as "execute as owner / accessible to anyone". The Google login lives inside that
deployment; this module holds only its public ``/exec`` URL. See
``docs/superpowers/specs/2026-08-23-google-sheets-module-design.md`` for the full rationale and the
Apps Script source to redeploy from if the deployment is ever lost.

Everything here is Python standard library only, matching the rest of this project.

    >>> sheet = GoogleSheet()
    >>> sheet.read_cell("LePone(Z)", "R11")
    0
    >>> sheet.write_cell("LePone(Z)", "R11", 42)
    42
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# Reuse the monitor's KEY=VALUE .env reader and default path so the nation name is resolved with the
# exact same rules as the credentials (process environment wins, then the .env file). Importing it
# has no side effects -- clop_monitor only runs anything under its own __main__ guard.
from clop_monitor import DEFAULT_ENV_PATH, load_env_file


#: Public Apps Script web-app endpoint bound to the shared sheet. Committed on purpose: the sheet
#: is shared, so the tool is too. Redeploy the script in the spec doc and replace this if it dies.
EXEC_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbxq-yk5Yd5kbBOAm7HKGXWDqsQg85XMTiO80HUCLR91PhoOAyFjLoFwBTh9_uSCYdc3uQ/exec"
)

#: The spreadsheet the endpoint is bound to. Kept for reference and for anonymous CSV reads
#: (``https://docs.google.com/spreadsheets/d/<id>/export?format=csv``), which need no endpoint.
SHEET_ID = "13LWTcalSlpwVAXAnwYo_9hqju5IAosfme5guDToJ3ug"

DEFAULT_TIMEOUT = 30.0

#: One initial request plus these two retries. A few seconds is long enough to absorb the brief
#: Google Apps Script 404/5xx glitches seen in production without holding the monitor up for an
#: entire polling interval. Writes are explicit assignments, so repeating the identical payload is
#: idempotent if Google applied it but dropped the response.
DEFAULT_RETRY_DELAYS = (1.0, 3.0)
RETRYABLE_HTTP_STATUSES = frozenset({404, 408, 425, 429, 500, 502, 503, 504})

#: Environment variable naming the sheet tab for your nation (its value is the exact tab name, e.g.
#: ``LePone(Z)``). Set it in ``.env`` alongside CLOP_USERNAME / CLOP_PASSWORD.
NATION_ENV = "CLOP_NATION"

Grid = List[List[Any]]


def cell_int(value: Any) -> int:
    """Normalise a value the sheet handed back into an integer; an empty cell is zero.

    The sheet returns real numbers as ``int``/``float`` and everything else as text, so a cell
    somebody typed ``1,204`` into arrives as a string. Text that is not a whole number -- a label,
    a formula error, the string ``"1.5"`` -- reads as zero, because these cells are all counts.

    A cell holding a real *number* is truncated rather than zeroed, so a numeric ``1.5`` gives
    ``1``. Nothing here writes a fraction, so this only arises if a person types one in; truncating
    it means the next reconcile quietly corrects the cell instead of flagging it, which is the
    milder of the two wrong answers available.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    return int(text) if re.fullmatch(r"-?\d+", text) else 0


def column_letter(index: int) -> str:
    """Convert a 0-based column index to its A1 letter: ``0 -> "A"``, ``26 -> "AA"``.

    Past ``Z`` matters here: the Dashboard is one column per nation, so the alliance growing walks
    it toward ``AA``. A single-letter shortcut would fail silently on the tenth nation.
    """
    if index < 0:
        raise ValueError(f"column index must be >= 0, got {index}")
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def find_in_row(row: Sequence[Any], text: str) -> Optional[int]:
    """Return the 0-based index of the cell in ``row`` equal to ``text``, or ``None``.

    Compared exactly after stripping both sides. A substring match would let ``LePone`` resolve to
    the ``LePone(Z)`` column, and the tab name is the whole cell or nothing.
    """
    wanted = text.strip()
    for index, cell in enumerate(row):
        if cell is not None and str(cell).strip() == wanted:
            return index
    return None


def index_column(grid: Grid) -> Dict[str, List[int]]:
    """Map each non-empty column-A label to the **1-based** rows it occupies.

    Every occurrence is returned, not just the first: reporting duplicates is what lets a caller
    refuse to write a sheet where a label has been pasted twice and the right row is ambiguous.
    """
    found: Dict[str, List[int]] = {}
    for number, row in enumerate(grid, 1):
        cell = row[0] if len(row) > 0 else None
        label = "" if cell is None else str(cell).strip()
        if label:
            found.setdefault(label, []).append(number)
    return found


class SheetError(RuntimeError):
    """Any failure reading or writing the sheet: transport, bad response, or a server-side error."""


def nation_from_env(env_path: Path = DEFAULT_ENV_PATH) -> str:
    """Return the configured nation tab name, or raise ``SheetError`` if it is not set.

    Resolution matches the credentials: a value already in the process environment wins, otherwise
    the ``.env`` file is consulted.
    """
    value = os.environ.get(NATION_ENV) or load_env_file(env_path).get(NATION_ENV)
    if not value:
        raise SheetError(
            f"{NATION_ENV} is not set. Add it to .env as {NATION_ENV}=<your nation> "
            "(e.g. LePone(Z)); its value is the exact name of your nation's tab in the sheet."
        )
    return value


def _as_grid(values: Any) -> Grid:
    """Coerce a scalar / 1-D row / 2-D block into the 2-D shape the Sheets API expects.

    ``5`` -> ``[[5]]``; ``[1, 2]`` -> ``[[1, 2]]`` (one row); ``[[1], [2]]`` stays as-is (a column).
    Strings are treated as a single scalar cell, not as a sequence of characters.
    """
    if isinstance(values, str) or not isinstance(values, Sequence):
        return [[values]]
    rows = list(values)
    if rows and all(
        isinstance(r, Sequence) and not isinstance(r, str) for r in rows
    ):
        return [list(r) for r in rows]
    return [list(rows)]


class GoogleSheet:
    """Read and write ranges of the shared sheet by A1 notation, via the Apps Script endpoint."""

    def __init__(
        self,
        exec_url: str = EXEC_URL,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        self.exec_url = exec_url
        self.timeout = timeout
        self.retry_delays = tuple(retry_delays)

    def read(self, tab: str, a1: str) -> Grid:
        """Return the values in ``tab!a1`` as a 2-D list of rows (as the sheet stores them)."""
        return self._call("read", tab, a1)

    def write(self, tab: str, a1: str, values: Any) -> Grid:
        """Write ``values`` to ``tab!a1`` and return what the sheet holds afterward.

        ``values`` may be a scalar, a flat row, or a 2-D block; it is coerced to 2-D. Its shape
        must match ``a1`` (a 1x1 range takes one cell, a 2x3 range takes two rows of three).
        """
        return self._call("write", tab, a1, _as_grid(values))

    def read_cell(self, tab: str, a1: str) -> Any:
        """Read a single-cell range and return its lone value (``""`` if the cell is empty)."""
        return self._first(self.read(tab, a1))

    def write_cell(self, tab: str, a1: str, value: Any) -> Any:
        """Write one value to a single cell and return the stored result."""
        return self._first(self.write(tab, a1, value))

    def tab_exists(self, tab: str) -> bool:
        """Return whether ``tab`` is a tab in the sheet.

        Probes with a trivial read of ``A1``: a success means the tab is there. The endpoint reports
        a missing tab as ``no such tab: <name>`` (its documented protocol error), which we map to
        ``False``; any other failure -- a network drop, a dead endpoint -- propagates as ``SheetError``
        so a mere outage is never mistaken for a missing tab.
        """
        try:
            self.read(tab, "A1")
        except SheetError as exc:
            if "no such tab" in str(exc).lower():
                return False
            raise
        return True

    def require_tab(self, tab: str) -> None:
        """Raise ``SheetError`` unless ``tab`` exists in the sheet."""
        if not self.tab_exists(tab):
            raise SheetError(
                f"nation tab {tab!r} does not exist in the shared sheet. Check {NATION_ENV} names a "
                "tab exactly -- it is case-, spacing-, and punctuation-sensitive."
            )

    # -- internals -------------------------------------------------------------------------

    @staticmethod
    def _first(grid: Grid) -> Any:
        for row in grid:
            for cell in row:
                return cell
        return ""

    def _call(self, action: str, tab: str, a1: str, values: Any = None) -> Grid:
        payload = {"action": action, "tab": tab, "range": a1}
        if values is not None:
            payload["values"] = values
        encoded = json.dumps(payload).encode("utf-8")
        attempts = len(self.retry_delays) + 1
        for attempt in range(attempts):
            request = urllib.request.Request(
                self.exec_url,
                data=encoded,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status = response.status
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:200].decode("utf-8", "replace")
                message = f"HTTP {exc.code} from sheet endpoint: {detail}"
                if exc.code in RETRYABLE_HTTP_STATUSES and attempt < len(self.retry_delays):
                    time.sleep(self.retry_delays[attempt])
                    continue
                suffix = f" after {attempts} attempts" if attempt else ""
                raise SheetError(message + suffix) from exc
            except urllib.error.URLError as exc:
                message = f"could not reach sheet endpoint: {exc.reason}"
                if attempt < len(self.retry_delays):
                    time.sleep(self.retry_delays[attempt])
                    continue
                suffix = f" after {attempts} attempts" if attempt else ""
                raise SheetError(message + suffix) from exc

            if status != 200:
                message = f"HTTP {status} from sheet endpoint: {raw[:200]!r}"
                if status in RETRYABLE_HTTP_STATUSES and attempt < len(self.retry_delays):
                    time.sleep(self.retry_delays[attempt])
                    continue
                suffix = f" after {attempts} attempts" if attempt else ""
                raise SheetError(message + suffix)
            break

        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            # A Google login page or an Apps Script error page arrives as HTML, not JSON.
            raise SheetError(
                "unexpected non-JSON response from sheet endpoint "
                "(is the deployment still 'Anyone' access?)"
            ) from exc

        if not isinstance(body, dict) or not body.get("ok"):
            message = ""
            if isinstance(body, dict):
                message = str(body.get("error", ""))
            raise SheetError(message or "sheet endpoint reported failure")
        return body.get("values", [])


def startup_check(
    exec_url: str = EXEC_URL, env_path: Path = DEFAULT_ENV_PATH
) -> "tuple[GoogleSheet, str]":
    """Resolve the configured nation and confirm its tab exists before any work begins.

    Raises ``SheetError`` if ``CLOP_NATION`` is unset, its tab is missing, or the sheet is
    unreachable. Returns the connected ``GoogleSheet`` and the nation name on success.
    """
    nation = nation_from_env(env_path)
    sheet = GoogleSheet(exec_url)
    sheet.require_tab(nation)
    return sheet, nation


if __name__ == "__main__":
    import sys

    # Startup check: resolve CLOP_NATION and verify its tab exists, then read one cell to prove the
    # round trip. Read-only -- it never edits the sheet.
    from clop_monitor import popup_failure

    try:
        sheet, nation = startup_check()
    except SheetError as error:
        # A dialog, not a terminal line -- the same rule the monitor and the other two scripts
        # follow. A failure nobody sees is the one this project refuses to ship.
        popup_failure(f"The sheet startup check failed: {error}")
        sys.exit(1)
    print(f"Nation tab {nation!r} found.")
    print(f"{nation}!R11 = {sheet.read_cell(nation, 'R11')!r}")
