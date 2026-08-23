# Stockpile Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the nation's stockpiles for six goods into `R11:R16` of its tab in the shared sheet, stamped with the CLOP server time in `W10`, off the same `overview.php` fetch the building reconciliation already does.

**Architecture:** The overview "Resources" panel has the same row shape as the "Buildings" panel, so `buildings.BuildingsPanelParser` moves to a new `overview.py` as `PanelParser(heading)` and serves both. A new `stockpiles.py` maps the sheet's six STOCK labels to `resourcedefs` names, verifies `Q11:Q16` still reads those labels before writing anything, and writes `R11:R16` plus `W10`. `clop_monitor.reconcile_buildings_step` is renamed `sync_sheet_step` and runs both syncs off one page fetch.

**Tech Stack:** Python 3 standard library only (`html.parser`, `re`, `urllib`), `unittest` for tests. No new dependencies — the whole project is stdlib-only by design.

**Spec:** `docs/superpowers/specs/2026-08-23-stockpile-snapshot-design.md`

---

## Background you need before starting

You are working in `D:\Koan\clop\automation-poc`, a standalone Python tool that polls the hosted
game at `https://4clop.org` and mirrors some of a player's state into a shared Google Sheet.

- **No pytest, no build step, no linter.** Tests are `unittest`. Run everything with
  `python -m unittest -v` from the repo root. Run one file with `python -m unittest test_stockpiles -v`.
- **All tests are offline.** They stub the network with fake clients and fake sheets; nothing in the
  test suite contacts Google or the game. Keep it that way.
- **`sheets.GoogleSheet`** talks to the sheet through an Apps Script endpoint. Its four relevant
  methods: `read(tab, a1) -> list[list]` (2-D grid), `write(tab, a1, values) -> list[list]`,
  `read_cell(tab, a1)`, `write_cell(tab, a1, value)`. A1 ranges are plain strings like `"Q11:R16"`.
- **`buildings.py`** is the existing sibling feature; read it first. `stockpiles.py` deliberately
  mirrors its shape (parse → check → write → report) so the two read the same way.
- **Commit on `main`.** That is this repo's convention; there is no PR flow.

Two things are easy to get wrong and are the reason for several steps below:

1. The `Qty` value on overview is comma-formatted (`1,204`), and a good the nation holds **none** of
   is simply **absent from the page** — it is not rendered as `0`.
2. `W10` is a staleness marker. It must never be written when the data write was skipped for a
   safety reason, or it would claim freshness that isn't there.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `overview.py` | Two jobs, both about the page rather than what is on it: parse one named panel out of an `overview.php` page, and decide (`require_valid_overview`) whether the page is a complete, normal render worth trusting at all. **The trust policy grew into this file after the plan was written, and belongs here.** It is stated purely in this module's own vocabulary — panels, headings, whether the page finished — and both `buildings.py` and `stockpiles.py` need it while neither may depend on the other (see the note below this table), so the only other homes are a copy in each (which will drift apart) or a fourth module holding one function. Its failure messages do name the sheet, because their job is to tell a reader that nothing was written to it; that is a deliberate exception, not a leak of sheet knowledge into the parser. If you are considering splitting this file, split the parser out and leave the policy — not the reverse. |
| `stockpiles.py` | The six-good stockpile snapshot: the label→resource mapping, reading quantities and the server time off overview, checking the sheet's labels, writing `R11:R16` + `W10`. |
| `test_stockpiles.py` | Offline tests for `stockpiles.py`. |

**Modified:**

| File | Change |
|---|---|
| `buildings.py` | Drops its private `BuildingsPanelParser` (moved to `overview.py`) and its private `_num` (moved to `sheets.cell_int`). Public behaviour unchanged. |
| `sheets.py` | Gains `cell_int()`, the shared "normalise a cell the sheet returned" helper. |
| `clop_monitor.py` | `reconcile_buildings_step` → `sync_sheet_step`, which adds the stockpile snapshot after the building reconcile, off the same HTML. |
| `test_buildings.py` | `FakeSheet` learns the `Q`/`R` stock block and block `write`; the step tests follow the rename. |
| `README.md` | New "Stockpile snapshot" section; the "Building reconciliation" and "Tests" sections get the rename and the new file. |

`stockpiles.py` does **not** import `buildings.py` and vice versa. Both depend on `overview.py` and
`sheets.py` only.

---

## Task 1: Extract the panel parser into `overview.py`

Pure refactor — no behaviour changes. `buildings.BuildingsPanelParser` already parses the Resources
panel correctly; it is only hard-coded to arm on the heading `"Buildings"`. Moving it out lets
`stockpiles.py` reuse it without depending on `buildings.py`.

**Files:**
- Create: `overview.py`
- Modify: `buildings.py:1-17` (docstring), `buildings.py:22-24` (imports), `buildings.py:59-139` (parser class + `parse_overview_buildings`)
- Test: `test_buildings.py` (existing, unchanged — it is the regression net for this task)

- [ ] **Step 1: Run the existing tests to establish the green baseline**

Run: `python -m unittest -v`

Expected: everything passes. Note the total count (e.g. `Ran 118 tests ... OK`) — the same count must
pass at the end of this task.

- [ ] **Step 2: Create `overview.py`**

```python
#!/usr/bin/env python3
"""Parse a named panel out of an overview.php page.

overview.php renders several panels -- Resources, Buildings, Weapons, Armor -- and they share one
row shape: a right-aligned name cell followed by a cell whose ``<span>`` holds the value.

    <td style="text-align: right;">Apples</td><td><span class="text-success">226</span></td>

``PanelParser`` arms only after the ``panel-heading`` div whose text matches the heading it was
given, and stops at that panel's ``</table>``, so the identically-shaped sibling panels are not
picked up. Two details of the real markup are handled by falling out of the rules above rather than
by special cases:

* the Resources panel's leading icon cell (``<td style="width: 16px;"><img/></td>``, present unless
  the nation set ``hideicons``) has no ``text-align: right``, so it is not mistaken for the name;
* the trailing centred cells (Generated / Used / Loss / ...) and the Buildings panel's form buttons
  contain further ``<span>``s, but they arrive after the value span has been captured and so are
  ignored.

``buildings.py`` and ``stockpiles.py`` both read overview through this; neither depends on the other.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import List, Optional, Sequence, Tuple


class PanelParser(HTMLParser):
    """Collect ``[(name, value_text), ...]`` from the overview panel headed ``heading``."""

    def __init__(self, heading: str) -> None:
        super().__init__(convert_charrefs=True)
        self._heading = heading
        self._in_heading = False
        self._heading_buf: List[str] = []
        self._pending_table = False
        self._in_table = False
        self._capture: Optional[str] = None  # "name" | "value" | None
        self._name: Optional[str] = None
        self._value: Optional[str] = None
        self._buf: List[str] = []
        self.rows: List[Tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        if tag == "div" and (attr.get("class") or "") == "panel-heading":
            self._in_heading = True
            self._heading_buf = []
            return
        if self._pending_table and tag == "table":
            self._pending_table = False
            self._in_table = True
            return
        if not self._in_table:
            return
        if tag == "tr":
            self._name = self._value = None
        elif tag == "td" and "text-align: right" in (attr.get("style") or "") and self._name is None:
            self._capture = "name"
            self._buf = []
        elif tag == "span" and self._name is not None and self._value is None:
            self._capture = "value"
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading_buf.append(data)
        elif self._capture is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._in_heading:
            self._in_heading = False
            if "".join(self._heading_buf).strip() == self._heading:
                self._pending_table = True
            return
        if not self._in_table:
            return
        if tag == "td" and self._capture == "name":
            self._name = "".join(self._buf).strip()
            self._capture = None
        elif tag == "span" and self._capture == "value":
            self._value = "".join(self._buf).strip()
            self._capture = None
        elif tag == "tr":
            if self._name and self._value is not None:
                self.rows.append((self._name, self._value))
        elif tag == "table":
            self._in_table = False


def parse_panel(html: str, heading: str) -> List[Tuple[str, str]]:
    """Return ``[(name, value_text), ...]`` for the overview panel headed ``heading``.

    An unknown heading yields an empty list rather than raising -- callers decide whether an empty
    panel is a problem.
    """
    parser = PanelParser(heading)
    parser.feed(html)
    return parser.rows
```

- [ ] **Step 3: Delete the parser class from `buildings.py`**

Delete the whole `class BuildingsPanelParser(HTMLParser):` block (currently `buildings.py:59-124`,
from the `class` line down to and including the `self._in_table = False` line at the end of
`handle_endtag`).

- [ ] **Step 4: Rewrite `parse_overview_buildings` to use `parse_panel`**

Replace the body of `parse_overview_buildings` (currently `buildings.py:127-139`) with:

```python
def parse_overview_buildings(html: str) -> Dict[str, Tuple[int, int]]:
    """Return ``{overview_name: (have, disabled)}`` for the owned buildings on overview.php."""
    result: Dict[str, Tuple[int, int]] = {}
    for name, count_text in parse_panel(html, "Buildings"):
        match = _COUNT_RE.match(count_text)
        if not match:
            continue
        have = int(match.group(1).replace(",", ""))
        disabled = int(match.group(2)) if match.group(2) else 0
        result[name] = (have, disabled)
    return result
```

- [ ] **Step 5: Fix the imports and docstring in `buildings.py`**

Replace the import block (currently `buildings.py:19-27`) with:

```python
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from building_map import GAME_TO_SHEET, SHEET_BUILDINGS
from overview import parse_panel
from sheets import GoogleSheet
```

(`html.parser.HTMLParser` is no longer used here.)

Then, in the module docstring, change the line

```
1. parse the overview "Buildings" panel into ``{game_name: (have, disabled)}`` (owned buildings
```

to

```
1. parse the overview "Buildings" panel (via ``overview.parse_panel``) into
   ``{game_name: (have, disabled)}`` (owned buildings
```

- [ ] **Step 6: Run the tests to prove the refactor changed nothing**

Run: `python -m unittest -v`

Expected: the same test count as Step 1, all passing. In particular
`ParserTests.test_only_buildings_panel_parsed` and `ParserTests.test_have_and_disabled_extracted`
must still pass — they are what proves `PanelParser` still scopes to the right panel.

- [ ] **Step 7: Commit**

```bash
git add overview.py buildings.py
git commit -m "refactor: extract the overview panel parser into overview.py"
```

---

## Task 2: Move cell normalising into `sheets.cell_int`

`buildings._num` turns whatever the sheet handed back into an `int`. `stockpiles.py` needs exactly
the same thing. Rather than duplicate it or import it across feature modules, it moves to `sheets.py`
— normalising a value the sheet returned is `sheets.py`'s own subject.

**Files:**
- Modify: `sheets.py:23-30` (imports), and add `cell_int` after the `Grid` type alias
- Modify: `buildings.py:165-174` (delete `_num`), `buildings.py:222`, `buildings.py:229`
- Test: `test_sheets.py` (add a test class)

- [ ] **Step 1: Write the failing test**

Append to `test_sheets.py`, immediately **before** the `if __name__ == "__main__":` block at the
bottom of the file:

```python
class CellIntTests(unittest.TestCase):
    def test_empty_cell_is_zero(self):
        self.assertEqual(sheets.cell_int(""), 0)
        self.assertEqual(sheets.cell_int(None), 0)

    def test_numbers_pass_through(self):
        self.assertEqual(sheets.cell_int(7), 7)
        self.assertEqual(sheets.cell_int(7.0), 7)

    def test_comma_formatted_text_parses(self):
        self.assertEqual(sheets.cell_int("1,204"), 1204)
        self.assertEqual(sheets.cell_int(" -3 "), -3)

    def test_non_numeric_text_is_zero(self):
        self.assertEqual(sheets.cell_int("n/a"), 0)
        self.assertEqual(sheets.cell_int("1.5"), 0)
```

If `test_sheets.py` does not already `import sheets` at the top (it may import names individually),
add `import sheets` to its imports.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest test_sheets.CellIntTests -v`

Expected: FAIL with `AttributeError: module 'sheets' has no attribute 'cell_int'`.

- [ ] **Step 3: Add `cell_int` to `sheets.py`**

Add `import re` to the import block at `sheets.py:23-30`, so it reads:

```python
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, List, Sequence
```

Then add this function immediately after the `Grid = List[List[Any]]` line (`sheets.py:55`):

```python
def cell_int(value: Any) -> int:
    """Normalise a value the sheet handed back into an integer; an empty cell is zero.

    The sheet returns real numbers as ``int``/``float`` and everything else as text, so a cell
    somebody typed ``1,204`` into arrives as a string. Anything that is not a whole number -- a
    label, a formula error, ``1.5`` -- reads as zero, because these cells are all counts.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    return int(text) if re.fullmatch(r"-?\d+", text) else 0
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest test_sheets.CellIntTests -v`

Expected: PASS, 4 tests.

- [ ] **Step 5: Point `buildings.py` at it**

Delete the `_num` function from `buildings.py` (currently `buildings.py:165-174`, the `def _num`
line through `return int(text) if re.fullmatch(...) else 0`).

Change the `sheets` import line in `buildings.py` from

```python
from sheets import GoogleSheet
```

to

```python
from sheets import GoogleSheet, cell_int
```

Then replace the two call sites in `reconcile`:

```python
                current = _num(column_b[have_row - 1])
```
becomes
```python
                current = cell_int(column_b[have_row - 1])
```

and

```python
            current = _num(column_b[disabled_row - 1])
```
becomes
```python
            current = cell_int(column_b[disabled_row - 1])
```

`import re` stays in `buildings.py` — `_COUNT_RE` still uses it.

- [ ] **Step 6: Run the full suite**

Run: `python -m unittest -v`

Expected: all pass, including every `ReconcileTests` case (they exercise both call sites, with empty
cells normalising to 0).

- [ ] **Step 7: Commit**

```bash
git add sheets.py buildings.py test_sheets.py
git commit -m "refactor: move sheet cell normalising into sheets.cell_int"
```

---

## Task 3: `stockpiles.py` — the mapping and reading quantities off overview

**Files:**
- Create: `stockpiles.py`
- Test: `test_stockpiles.py`

- [ ] **Step 1: Write the failing test**

Create `test_stockpiles.py`:

```python
#!/usr/bin/env python3
"""Offline unit tests for stockpiles.py -- no network."""

import unittest

from stockpiles import (
    STOCK_FIRST_ROW,
    STOCK_ROWS,
    desired_stock,
    parse_overview_resources,
)


# A minimal overview.php: the Resources panel (with icon cells, comma formatting and the trailing
# centred columns), plus decoy panels that share its exact row shape.
OVERVIEW_HTML = """
<li><a>Server time: 2026-08-23 03:23:44</a></li>
<div class="panel-heading">Resources</div>
<table class="table"><tbody>
  <tr>
    <td style="width: 16px;"><img src="images/icons/Apples.png"/></td>
    <td style="text-align: right;">Apples</td>
    <td><span class="text-success">1,226</span></td>
    <td style="text-align: center;"><span class="text-success">0</span></td>
    <td style="text-align: center;"><span class="text-danger">14</span></td>
  </tr>
  <tr>
    <td style="width: 16px;"><img src="images/icons/Coffee.png"/></td>
    <td style="text-align: right;">Coffee</td>
    <td><span class="text-success">29</span></td>
    <td style="text-align: center;"><span class="text-danger">1</span></td>
  </tr>
  <tr>
    <td style="text-align: right;">Gems</td>
    <td><span class="text-success">6</span></td>
  </tr>
</tbody></table>
<div class="panel-heading">Buildings</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Gem Mine</td><td><span>3</span></td></tr>
</tbody></table>
<div class="panel-heading">Weapons</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Oil</td><td><span>999</span></td></tr>
</tbody></table>
"""


class ParseResourcesTests(unittest.TestCase):
    def test_only_resources_panel_parsed(self):
        result = parse_overview_resources(OVERVIEW_HTML)
        self.assertEqual(set(result), {"Apples", "Coffee", "Gems"})
        self.assertNotIn("Gem Mine", result)     # buildings panel ignored
        self.assertEqual(result.get("Oil"), None)  # the weapons-panel decoy is not picked up

    def test_commas_stripped(self):
        self.assertEqual(parse_overview_resources(OVERVIEW_HTML)["Apples"], 1226)

    def test_row_without_icon_cell_parsed(self):
        # hideicons drops the leading <td>; the name is then the first cell.
        self.assertEqual(parse_overview_resources(OVERVIEW_HTML)["Gems"], 6)


class DesiredStockTests(unittest.TestCase):
    def test_row_order_matches_the_sheet(self):
        self.assertEqual(
            [label for label, _ in STOCK_ROWS],
            ["apple", "oil", "coffee", "mpart", "vpart", "gems"],
        )
        self.assertEqual(STOCK_FIRST_ROW, 11)

    def test_absent_good_is_zero(self):
        # The nation holds no machinery or vehicle parts, so they are absent from the page.
        self.assertEqual(desired_stock({}), [0, 0, 0, 0, 0, 0])

    def test_quantities_in_row_order(self):
        resources = parse_overview_resources(OVERVIEW_HTML)
        # apple, oil, coffee, mpart, vpart, gems
        self.assertEqual(desired_stock(resources), [1226, 0, 29, 0, 0, 6])


class MappingIntegrityTests(unittest.TestCase):
    def test_six_distinct_goods(self):
        self.assertEqual(len(STOCK_ROWS), 6)
        self.assertEqual(len({label for label, _ in STOCK_ROWS}), 6)
        self.assertEqual(len({game for _, game in STOCK_ROWS}), 6)

    def test_game_names_are_real_resourcedefs_names(self):
        # The non-building resourcedefs names, from clop/tables with data.sql.
        known = {
            "Oil", "Copper", "Apples", "Energy", "Vehicle Parts", "Machinery Parts", "Pies",
            "Cider", "Coffee", "Gasoline", "Gems", "Tungsten", "Plastics", "Precision Parts",
            "Composites", "Drugs", "Toys", "Forbidden Research", "Apotheosis Serum",
        }
        for _, game_name in STOCK_ROWS:
            self.assertIn(game_name, known)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest test_stockpiles -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'stockpiles'`.

- [ ] **Step 3: Create `stockpiles.py` with the mapping and the parser**

```python
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
    """The overview page is not what the snapshot expects (no server-time stamp)."""


def parse_overview_resources(html: str) -> Dict[str, int]:
    """Return ``{resource_name: qty}`` from the overview "Resources" panel.

    Only resources the nation has a row for are rendered, so a good it holds none of is absent from
    the result rather than present as zero; ``desired_stock`` is what turns that into a zero.
    """
    result: Dict[str, int] = {}
    for name, value_text in parse_panel(html, "Resources"):
        text = value_text.replace(",", "").strip()
        if re.fullmatch(r"-?\d+", text):
            result[name] = int(text)
    return result


def desired_stock(resources: Dict[str, int]) -> List[int]:
    """Return the six quantities in sheet row order; a good absent from overview is zero."""
    return [resources.get(game_name, 0) for _, game_name in STOCK_ROWS]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest test_stockpiles -v`

Expected: PASS, 8 tests (`ParseResourcesTests` 3, `DesiredStockTests` 3, `MappingIntegrityTests` 2).

- [ ] **Step 5: Commit**

```bash
git add stockpiles.py test_stockpiles.py
git commit -m "feat: map the sheet's six STOCK goods and read their quantities off overview"
```

---

## Task 4: `stockpiles.py` — the server-time stamp

**Files:**
- Modify: `stockpiles.py` (add `parse_server_time` after `desired_stock`)
- Test: `test_stockpiles.py`

- [ ] **Step 1: Write the failing test**

Add to `test_stockpiles.py`, after `DesiredStockTests` and before `MappingIntegrityTests`. Also add
`StockpileError` and `parse_server_time` to the `from stockpiles import (...)` list at the top.

```python
class ServerTimeTests(unittest.TestCase):
    def test_stamp_returned_verbatim(self):
        self.assertEqual(parse_server_time(OVERVIEW_HTML), "2026-08-23 03:23:44")

    def test_stamp_found_in_the_real_header_markup(self):
        html = '<li><a>Server time: 2026-01-02 09:05:00</a></li><li><a>Next tick: 0:36:16</a></li>'
        self.assertEqual(parse_server_time(html), "2026-01-02 09:05:00")

    def test_missing_stamp_raises(self):
        with self.assertRaises(StockpileError):
            parse_server_time("<html><body>Please log in.</body></html>")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest test_stockpiles.ServerTimeTests -v`

Expected: FAIL with `ImportError: cannot import name 'parse_server_time' from 'stockpiles'`.

- [ ] **Step 3: Add `parse_server_time` to `stockpiles.py`**

```python
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
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest test_stockpiles -v`

Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add stockpiles.py test_stockpiles.py
git commit -m "feat: read the CLOP server time stamp off the overview header"
```

---

## Task 5: `stockpiles.py` — the label check

**Files:**
- Modify: `stockpiles.py` (add `check_labels` after `parse_server_time`)
- Test: `test_stockpiles.py`

- [ ] **Step 1: Write the failing test**

Add to `test_stockpiles.py`. Add `check_labels` to the `from stockpiles import (...)` list, and put
this `FakeSheet` right after the `OVERVIEW_HTML` constant (the later tasks reuse it):

```python
EXPECTED_LABELS = ["apple", "oil", "coffee", "mpart", "vpart", "gems"]


class FakeSheet:
    """Stand-in for GoogleSheet: serves the Q label column and records every write."""

    def __init__(self, labels=None):
        self.labels = list(labels) if labels is not None else list(EXPECTED_LABELS)
        self.blocks = []   # (a1, values) from write()
        self.cells = []    # (a1, value) from write_cell()

    def read(self, tab, a1):
        return [[label] for label in self.labels]

    def write(self, tab, a1, values):
        self.blocks.append((a1, values))
        return values

    def write_cell(self, tab, a1, value):
        self.cells.append((a1, value))
        return value
```

Then the test class, after `ServerTimeTests`:

```python
class CheckLabelsTests(unittest.TestCase):
    def test_expected_labels_have_no_problems(self):
        self.assertEqual(check_labels(FakeSheet(), "T"), [])

    def test_labels_are_case_and_space_insensitive(self):
        problems = check_labels(FakeSheet(labels=[" Apple ", "OIL", "coffee",
                                                  "mpart", "vpart", "gems"]), "T")
        self.assertEqual(problems, [])

    def test_reordered_labels_flagged_with_their_cell(self):
        problems = check_labels(FakeSheet(labels=["oil", "apple", "coffee",
                                                  "mpart", "vpart", "gems"]), "T")
        self.assertEqual(len(problems), 2)
        self.assertIn("Q11", problems[0])
        self.assertIn("'apple'", problems[0])
        self.assertIn("'oil'", problems[0])

    def test_renamed_label_flagged(self):
        problems = check_labels(FakeSheet(labels=["apple", "oil", "coffee",
                                                  "mpart", "vpart", "diamonds"]), "T")
        self.assertEqual(len(problems), 1)
        self.assertIn("Q16", problems[0])

    def test_short_grid_flagged_rather_than_crashing(self):
        problems = check_labels(FakeSheet(labels=["apple", "oil"]), "T")
        self.assertEqual(len(problems), 4)     # rows 13-16 read as blank

    def test_reads_only_the_label_column(self):
        # The R values are deliberately not read: they are overwritten regardless.
        class RecordingSheet(FakeSheet):
            def read(self, tab, a1):
                self.read_range = a1
                return super().read(tab, a1)

        sheet = RecordingSheet()
        check_labels(sheet, "T")
        self.assertEqual(sheet.read_range, "Q11:Q16")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest test_stockpiles.CheckLabelsTests -v`

Expected: FAIL with `ImportError: cannot import name 'check_labels' from 'stockpiles'`.

- [ ] **Step 3: Add `check_labels` to `stockpiles.py`**

```python
def check_labels(sheet: GoogleSheet, nation: str) -> List[str]:
    """Confirm ``Q11:Q16`` still names the six goods, in order.

    Returns the list of problems; empty means the block is safe to write. A non-empty list names
    each offending cell and must stop **all** writing, ``W10`` included -- the rows are addressed by
    position, so a moved label means this module no longer knows which row is which.

    Only the labels are read. The current ``R`` values are deliberately not consulted: they are
    overwritten unconditionally, so there is nothing to compare them against.
    """
    grid = sheet.read(nation, LABEL_RANGE)
    problems: List[str] = []
    for index, (label, _) in enumerate(STOCK_ROWS):
        row = grid[index] if index < len(grid) else []
        found = str(row[0] if len(row) > 0 else "").strip()
        if found.lower() != label:
            problems.append(
                f"Q{STOCK_FIRST_ROW + index} should read {label!r} but reads {found!r}"
            )
    return problems
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest test_stockpiles -v`

Expected: PASS, 16 tests.

- [ ] **Step 5: Commit**

```bash
git add stockpiles.py test_stockpiles.py
git commit -m "feat: verify the sheet's STOCK labels before trusting the stock rows"
```

---

## Task 6: `stockpiles.py` — writing the snapshot

**Files:**
- Modify: `stockpiles.py` (add `snapshot` after `check_labels`)
- Test: `test_stockpiles.py`

- [ ] **Step 1: Write the failing test**

Add `snapshot`, `TIMESTAMP_CELL` and `VALUE_RANGE` to the `from stockpiles import (...)` list, then
add this class after `CheckLabelsTests`:

```python
class SnapshotTests(unittest.TestCase):
    def _resources(self):
        return parse_overview_resources(OVERVIEW_HTML)   # Apples 1226, Coffee 29, Gems 6

    def test_writes_the_value_block_and_the_timestamp(self):
        sheet = FakeSheet()
        written = snapshot(sheet, "T", self._resources(), "2026-08-23 03:23:44")
        self.assertEqual(
            sheet.blocks,
            [(VALUE_RANGE, [[1226], [0], [29], [0], [0], [6]])],
        )
        self.assertEqual(sheet.cells, [(TIMESTAMP_CELL, "2026-08-23 03:23:44")])
        self.assertEqual(
            written,
            [("apple", 1226), ("oil", 0), ("coffee", 29),
             ("mpart", 0), ("vpart", 0), ("gems", 6)],
        )

    def test_a_good_no_longer_held_is_written_back_to_zero(self):
        sheet = FakeSheet()
        snapshot(sheet, "T", {}, "2026-08-23 03:23:44")
        self.assertEqual(sheet.blocks, [(VALUE_RANGE, [[0], [0], [0], [0], [0], [0]])])

    def test_nothing_is_read(self):
        # The snapshot overwrites unconditionally, so it must not depend on the sheet's contents.
        class NoReadSheet(FakeSheet):
            def read(self, tab, a1):
                raise AssertionError("snapshot must not read the sheet")

        snapshot(NoReadSheet(), "T", self._resources(), "2026-08-23 03:23:44")

    def test_timestamp_written_last(self):
        # W10 claims the numbers beside it are fresh, so it must never land before they do.
        sheet = FakeSheet()
        order = []
        sheet.write = lambda tab, a1, values: order.append("values")
        sheet.write_cell = lambda tab, a1, value: order.append("stamp")
        snapshot(sheet, "T", self._resources(), "2026-08-23 03:23:44")
        self.assertEqual(order, ["values", "stamp"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m unittest test_stockpiles.SnapshotTests -v`

Expected: FAIL with `ImportError: cannot import name 'snapshot' from 'stockpiles'`.

- [ ] **Step 3: Add `snapshot` to `stockpiles.py`**

```python
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
    values, so it can never claim freshness for a block write that failed.

    The caller must have run ``check_labels`` and got no problems. This function trusts the rows.
    """
    wanted = desired_stock(resources)
    sheet.write(nation, VALUE_RANGE, [[value] for value in wanted])
    sheet.write_cell(nation, TIMESTAMP_CELL, server_time)
    return [(label, value) for (label, _), value in zip(STOCK_ROWS, wanted)]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m unittest test_stockpiles -v`

Expected: PASS, 20 tests.

- [ ] **Step 5: Commit**

```bash
git add stockpiles.py test_stockpiles.py
git commit -m "feat: write the stockpile snapshot to R11:R16 with a W10 timestamp"
```

---

## Task 7: The standalone `python stockpiles.py` script

The read-only counterpart to `python buildings.py` — what you run after editing the sheet's layout,
to see whether the tool still recognises it. It writes nothing.

**Files:**
- Modify: `stockpiles.py` (add `_standalone` and the `__main__` block at the end)
- Test: manual (it is the network entry point; the logic underneath is already covered)

- [ ] **Step 1: Add `_standalone` and the entry point to the end of `stockpiles.py`**

```python
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
    wanted = desired_stock(parse_overview_resources(html))
    problems = check_labels(sheet, nation)

    # The snapshot itself never reads these; they are shown here only so a human can see what a run
    # would change. One extra read is fine in a diagnostic invoked by hand.
    stored = [row[0] if row else "" for row in sheet.read(nation, VALUE_RANGE)]
    stored += [""] * (len(STOCK_ROWS) - len(stored))

    print(f"Server time: {parse_server_time(html)}")
    print(f"{'row':<5}{'label':<8}{'game resource':<18}{'overview':>10}{'sheet':>10}")
    for index, ((label, game_name), want) in enumerate(zip(STOCK_ROWS, wanted)):
        row = f"R{STOCK_FIRST_ROW + index}"
        print(f"{row:<5}{label:<8}{game_name:<18}{want:>10}{str(stored[index]):>10}")

    if problems:
        print(f"\nStock label check FAILED for {nation!r}:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"\nStock label check passed for {nation!r}. Nothing was written.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_standalone())
```

- [ ] **Step 2: Run it against the live sheet and game**

Run: `python stockpiles.py`

Expected: the server time, a six-row table, and `Stock label check passed for 'LePone(Z)'. Nothing
was written.` with exit code 0. The `overview` column should show the nation's real quantities and
the `sheet` column whatever `R11:R16` currently holds (all `0` before Task 9 runs the real thing).

Confirm nothing was written: `python -c "from sheets import *; s=GoogleSheet(); print(s.read(nation_from_env(), 'R11:R16'), s.read_cell(nation_from_env(), 'W10'))"` — `W10` should still be empty.

- [ ] **Step 3: Commit**

```bash
git add stockpiles.py
git commit -m "feat: add the read-only 'python stockpiles.py' label check"
```

---

## Task 8: Monitor integration — `sync_sheet_step`

`reconcile_buildings_step` already fetches `overview.php`, re-logs-in a dropped session before
trusting the page, and swallows its own failures into a popup. The stockpile snapshot needs all
three, so it joins that step rather than adding a second page fetch per poll.

The two syncs are **independent**: a broken *building* region says nothing about the Q/R stock block,
so a building sanity failure no longer returns early — the stockpile snapshot still runs, and vice
versa.

**Files:**
- Modify: `clop_monitor.py:1717-1759` (the step), `clop_monitor.py:2091-2129` (its caller and the startup messages)
- Test: `test_buildings.py:65-85` (`FakeSheet`), `test_buildings.py:247-248` (the HTML constants), `test_buildings.py:293-333` (`ReconcileStepTests`)

- [ ] **Step 1: Update `FakeSheet` in `test_buildings.py` so it can serve the stock block**

Replace the whole `class FakeSheet:` block (currently `test_buildings.py:65-85`) with:

```python
class FakeSheet:
    """Stand-in for GoogleSheet: serves a fixed A/B grid and the Q/R stock block, records writes."""

    def __init__(self, column_a, column_b, stock_labels=None):
        self._a = list(column_a)
        self._b = list(column_b)
        self._labels = list(stock_labels) if stock_labels is not None else [
            "apple", "oil", "coffee", "mpart", "vpart", "gems"
        ]
        self.writes = []   # (a1, value) from write_cell
        self.blocks = []   # (a1, values) from write

    def read(self, tab, a1):
        if a1.startswith("Q"):
            return [[label] for label in self._labels]
        n = max(len(self._a), len(self._b))
        return [[self._a[i] if i < len(self._a) else "",
                 self._b[i] if i < len(self._b) else ""] for i in range(n)]

    def write(self, tab, a1, values):
        self.blocks.append((a1, values))
        return values

    def write_cell(self, tab, a1, value):
        self.writes.append((a1, value))
        if a1.startswith("B"):
            row = int(a1[1:]) - 1
            while row >= len(self._b):
                self._b.append("")
            self._b[row] = value
        return value


def building_writes(sheet):
    """The column-B writes only -- the stockpile snapshot also writes W10."""
    return [(a1, value) for a1, value in sheet.writes if a1.startswith("B")]
```

- [ ] **Step 2: Give the logged-in test page a server-time stamp**

The step now parses one, so the fixture needs it. Replace `test_buildings.py:247`:

```python
LOGGED_IN_OVERVIEW = '<a href="logout.php">Logout</a>' + OVERVIEW_HTML
```

with:

```python
LOGGED_IN_OVERVIEW = (
    '<a href="logout.php">Logout</a><li><a>Server time: 2026-08-23 03:23:44</a></li>'
    + OVERVIEW_HTML
)
```

Leave `LOGGED_OUT_OVERVIEW` as it is — the step raises on the logged-out check before it ever looks
for a stamp.

- [ ] **Step 3: Rewrite the step tests for the new name and behaviour**

Replace the whole `class ReconcileStepTests(unittest.TestCase):` block (currently
`test_buildings.py:293-333`, through the end of `test_logged_out_overview_writes_nothing`) with:

```python
class SyncSheetStepTests(unittest.TestCase):
    def test_corrections_alert_and_write(self):
        from clop_monitor import sync_sheet_step

        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Basic Mine")] = 8   # overview says 10 (1 disabled)
        sheet = FakeSheet(col_a, col_b)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(len(notifier.alerts), 1)
        self.assertIn("Basic Mine have 8 -> 10", notifier.alerts[0])
        self.assertEqual(notifier.failures, [])
        self.assertTrue(building_writes(sheet))

    def test_stockpile_snapshot_is_stamped_but_never_popped_up(self):
        from clop_monitor import sync_sheet_step

        # A sheet where the buildings already match, so nothing but the snapshot can speak up.
        # OVERVIEW_HTML owns Bakery 2, Basic Copper Mine 10 (1 disabled), Gem Mine 1; every other
        # building is 0 on both sides ('' normalises to 0).
        col_a, names = full_column_a()
        col_b = [""] * 130
        col_b[8 + names.index("Bakery")] = 2
        col_b[8 + names.index("Basic Mine")] = 10
        col_b[8 + names.index("Gem Mine")] = 1
        col_b[57 + names.index("Basic Mine")] = 1     # its disabled row
        sheet = FakeSheet(col_a, col_b)
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertIn(("W10", "2026-08-23 03:23:44"), sheet.writes)
        # A routine snapshot is a scheduled refresh, not an event: no popup for it.
        self.assertEqual(notifier.failures, [])
        self.assertEqual(notifier.alerts, [])

    def test_bad_stock_labels_warn_and_write_no_stock_cells(self):
        from clop_monitor import sync_sheet_step

        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130,
                          stock_labels=["oil", "apple", "coffee", "mpart", "vpart", "gems"])
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("Stockpile snapshot skipped", notifier.failures[0])
        self.assertEqual(sheet.blocks, [])                              # no R block
        self.assertNotIn("W10", [a1 for a1, _ in sheet.writes])         # and no stamp

    def test_sanity_failure_warns_and_writes_no_building_cells(self):
        from clop_monitor import sync_sheet_step

        sheet = FakeSheet(sheet_column_a(), [""] * 130)  # most buildings missing -> sanity fails
        notifier = FakeNotifier()
        sync_sheet_step(FakeClient(LOGGED_IN_OVERVIEW), sheet, "T", notifier)
        self.assertEqual(notifier.alerts, [])
        self.assertEqual(len(notifier.failures), 1)
        self.assertIn("Building sync skipped", notifier.failures[0])
        self.assertEqual(building_writes(sheet), [])
        # The stock block is a different region of the sheet, so it is still snapshotted.
        self.assertTrue(sheet.blocks)
        self.assertIn("W10", [a1 for a1, _ in sheet.writes])

    def test_logged_out_overview_writes_nothing(self):
        from clop_monitor import sync_sheet_step

        col_a, _ = full_column_a()
        sheet = FakeSheet(col_a, [""] * 130)
        notifier = FakeNotifier()
        client = FakeClient(LOGGED_OUT_OVERVIEW)
        sync_sheet_step(client, sheet, "T", notifier)
        self.assertTrue(client.logins >= 1)          # it tried to re-login
        self.assertEqual(sheet.writes, [])           # but never wrote
        self.assertEqual(sheet.blocks, [])
        self.assertEqual(len(notifier.failures), 1)
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `python -m unittest test_buildings.SyncSheetStepTests -v`

Expected: all five FAIL with `ImportError: cannot import name 'sync_sheet_step' from 'clop_monitor'`.

- [ ] **Step 5: Rewrite the step in `clop_monitor.py`**

Replace the whole `def reconcile_buildings_step(...)` function (currently `clop_monitor.py:1717-1759`)
with:

```python
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
```

- [ ] **Step 6: Update the caller and the startup messages**

In `clop_monitor.py`, replace the comment and call at lines 2124-2129:

```python
                # Building reconciliation is its own process that fires first, before the regular
                # message/news/report alerting. It handles and reports its own failures.
                if building_sheet is not None and building_nation is not None:
                    reconcile_buildings_step(
                        client, building_sheet, building_nation, notifier
                    )
```

with:

```python
                # Sheet sync is its own process that fires first, before the regular
                # message/news/report alerting. It handles and reports its own failures.
                if building_sheet is not None and building_nation is not None:
                    sync_sheet_step(client, building_sheet, building_nation, notifier)
```

Then update the three startup strings at lines 2091-2112 so they name what actually happens.
Replace:

```python
        # Building reconciliation is on whenever CLOP_NATION names a tab in the shared sheet.
        # Unset -> the monitor runs exactly as before; a missing/unreachable tab -> warn and stay
        # off rather than fail every poll.
```
with:
```python
        # Sheet sync (buildings + stockpiles) is on whenever CLOP_NATION names a tab in the shared
        # sheet. Unset -> the monitor runs exactly as before; a missing/unreachable tab -> warn and
        # stay off rather than fail every poll.
```

Replace `print("Building sync off (CLOP_NATION not set).", flush=True)` with
`print("Sheet sync off (CLOP_NATION not set).", flush=True)`.

Replace:
```python
                print(
                    f"Building sync on: reconciling {building_nation!r} each poll before alerting.",
                    flush=True,
                )
```
with:
```python
                print(
                    f"Sheet sync on: reconciling buildings and snapshotting stockpiles for "
                    f"{building_nation!r} each poll before alerting.",
                    flush=True,
                )
```

Replace `notifier.notify_failure(f"Building sync is off: {error}")` with
`notifier.notify_failure(f"Sheet sync is off: {error}")`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m unittest test_buildings -v`

Expected: PASS, including all five `SyncSheetStepTests`.

- [ ] **Step 8: Run the whole suite**

Run: `python -m unittest -v`

Expected: everything passes. If anything still refers to `reconcile_buildings_step`, find it with
`grep -rn reconcile_buildings_step .` and fix it.

- [ ] **Step 9: Commit**

```bash
git add clop_monitor.py test_buildings.py
git commit -m "feat: snapshot stockpiles alongside the building reconcile in one sheet-sync step"
```

---

## Task 9: Documentation and live verification

**Files:**
- Modify: `README.md:694-733` (the "Building reconciliation" and "Tests" sections)
- Modify: `docs/superpowers/plans/2026-08-23-stockpile-snapshot.md` (tick the boxes as you go)

- [ ] **Step 1: Retitle the README section and add the stockpile snapshot**

In `README.md`, replace the heading line `## Building reconciliation` with these five lines (the
existing body text that follows it stays exactly as it is, now under the `### Building counts`
subheading):

```markdown
## Sheet sync

When `CLOP_NATION` is set, the monitor runs one extra step **first each poll, before the regular
alerting**: it reads `overview.php` once and syncs two parts of your nation tab from it — the
building counts, and the stockpile numbers. Either can be skipped for a layout problem without
affecting the other.

### Building counts
```

That existing body currently opens with "When `CLOP_NATION` is set, the monitor runs one extra step
**first each poll**…" — delete that first sentence, since the new intro now says it, so the
subsection starts at "it reads your nation's building counts from `overview.php`…" rewritten as
"It reads your nation's building counts from `overview.php`…".

Then, after the existing paragraph ending "the monitor's message/news/report alerting is never
affected either way.", append:

```markdown
### Stockpile snapshot

The same step records what you currently hold of six goods into the **STOCK block** of your tab —
`R11:R16`, beside the `apple / oil / coffee / mpart / vpart / gems` labels in column Q — and stamps
**`W10`** with the game's own server time, so you can see at a glance how fresh those numbers are.

This is a snapshot, not a reconciliation: the numbers are simply replaced, and a normal update is
**silent** (no popup). It happens every poll, so a popup would never stop firing. A good you hold
none of is written back to `0`.

`W10` means *last verified*, not *last changed* — it is refreshed on every poll even when nothing
moved. So a `W10` that has stopped advancing is the signal that something is wrong, and that is
exactly why nothing at all is written when the check below fails.

Those six rows are written **by position**, so before each write the tool confirms `Q11:Q16` still
reads `apple, oil, coffee, mpart, vpart, gems` in that order. If a label has moved or been renamed,
nothing is written — not the numbers and not the timestamp — and you get a popup naming the cell.
Run the same check yourself any time with:

```powershell
python .\stockpiles.py
```

It logs in, prints what overview says next to what the sheet says for each of the six goods, and
reports pass/fail with an exit code. It never writes. If a label has genuinely been renamed in the
sheet, update `STOCK_ROWS` in `stockpiles.py` (or the sheet) so they line up again.

Only these six goods are recorded. The `NEED`, `BUY` and `TICKS` columns beside them are yours; the
tool never touches them.
```

- [ ] **Step 2: Update the Tests section**

In `README.md`, change

```
The parser tests use synthetic HTML and never contact the hosted game. The Sheets and building tests
(`test_sheets.py`, `test_buildings.py`) stub the network, so they never contact Google or the game.
```

to

```
The parser tests use synthetic HTML and never contact the hosted game. The Sheets, building and
stockpile tests (`test_sheets.py`, `test_buildings.py`, `test_stockpiles.py`) stub the network, so
they never contact Google or the game.
```

- [ ] **Step 3: Verify the sheet's starting state, so the live run is provable**

Run:

```bash
python -c "from sheets import GoogleSheet, nation_from_env; s=GoogleSheet(); n=nation_from_env(); print('Q11:R16', s.read(n,'Q11:R16')); print('W10', repr(s.read_cell(n,'W10')))"
```

Expected: the six labels with their current `R` values, and `W10` as `''`. Write down what `R11:R16`
holds — you are about to change it.

- [ ] **Step 4: Run the read-only check against the live game**

Run: `python stockpiles.py`

Expected: exit code 0, `Stock label check passed`, and an `overview` column showing your real
quantities. If it fails here, stop and fix the mapping or the sheet before writing anything.

- [ ] **Step 5: Do one live write**

Run:

```bash
python -c "
import os
from clop_monitor import ClopClient, DEFAULT_BASE_URL, load_env_file
from sheets import DEFAULT_ENV_PATH, startup_check
import stockpiles as sp
env = load_env_file(DEFAULT_ENV_PATH)
c = ClopClient(DEFAULT_BASE_URL, os.environ.get('CLOP_USERNAME') or env['CLOP_USERNAME'],
               os.environ.get('CLOP_PASSWORD') or env['CLOP_PASSWORD'])
c.login()
html = c._open('overview.php')
sheet, nation = startup_check()
problems = sp.check_labels(sheet, nation)
assert not problems, problems
print(sp.snapshot(sheet, nation, sp.parse_overview_resources(html), sp.parse_server_time(html)))
"
```

Expected: a printed list of six `(label, qty)` pairs matching what `python stockpiles.py` reported.

- [ ] **Step 6: Confirm the sheet actually changed**

Run:

```bash
python -c "from sheets import GoogleSheet, nation_from_env; s=GoogleSheet(); n=nation_from_env(); print('R11:R16', s.read(n,'R11:R16')); print('W10', repr(s.read_cell(n,'W10')))"
```

Expected: `R11:R16` now holds the six quantities from Step 4, and `W10` holds a `YYYY-MM-DD HH:MM:SS`
string matching the server time from that page load. Open the sheet in a browser and eyeball the
STOCK block to be sure the numbers landed on the right rows.

- [ ] **Step 7: Commit**

```bash
git add README.md docs/superpowers/plans/2026-08-23-stockpile-snapshot.md
git commit -m "docs: document the stockpile snapshot and the renamed sheet-sync step"
```

---

## Done when

- `python -m unittest -v` passes with no failures.
- `python stockpiles.py` exits 0 against the live sheet and reports matching numbers.
- `R11:R16` and `W10` on the live tab hold the nation's real stockpiles and a server timestamp.
- `grep -rn reconcile_buildings_step .` returns nothing outside `docs/` and git history.
- The README describes both halves of the sheet sync.
