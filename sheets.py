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
import urllib.error
import urllib.request
from typing import Any, List, Sequence


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

Grid = List[List[Any]]


class SheetError(RuntimeError):
    """Any failure reading or writing the sheet: transport, bad response, or a server-side error."""


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

    def __init__(self, exec_url: str = EXEC_URL, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.exec_url = exec_url
        self.timeout = timeout

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
        request = urllib.request.Request(
            self.exec_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            raise SheetError(f"HTTP {exc.code} from sheet endpoint: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SheetError(f"could not reach sheet endpoint: {exc.reason}") from exc

        if status != 200:
            raise SheetError(f"HTTP {status} from sheet endpoint: {raw[:200]!r}")
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


if __name__ == "__main__":
    # Live round-trip against the real sheet: read R11, flip it, read back, restore.
    sheet = GoogleSheet()
    before = sheet.read_cell("LePone(Z)", "R11")
    print(f"R11 before: {before!r}")
    print(f"R11 after write 42: {sheet.write_cell('LePone(Z)', 'R11', 42)!r}")
    print(f"R11 re-read: {sheet.read_cell('LePone(Z)', 'R11')!r}")
    print(f"R11 restored: {sheet.write_cell('LePone(Z)', 'R11', before)!r}")
