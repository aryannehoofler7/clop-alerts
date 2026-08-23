# Dashboard Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One pass over `overview.php` updates both the player's own nation tab and the alliance-wide `Dashboard` tab — 31 goods plus six nation-status rows — with every target cell located by label lookup rather than a hardcoded address.

**Architecture:** Two new pure modules (`goods.py`, `nation.py`) parse the page once into reusable value objects (`Stockpiles`, `NationStatus`). `overview.py` gains whole-cell panel parsing so the Nation panel's per-tick figures are readable. `sheets.py` gains three grid helpers. `stockpiles.py` keeps its name but is rewritten to locate and write both sheet regions from those value objects. `clop_monitor.sync_sheet_step` changes by a handful of lines.

**Tech Stack:** Python 3.9+, standard library only. Tests are `unittest`, run with `python -m unittest` from `D:\Koan\clop\automation-poc`. No third-party packages — do not add any.

**Design of record:** `docs/superpowers/specs/2026-08-23-dashboard-sync-design.md`. The goods table is `docs/2026-08-23-dashboard-goods-map.md`.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `goods.py` | create | The 31-good vocabulary (`Good`, `GOODS`, four indexes) and `Stockpiles`, the parse-once handle on quantities. Knows the game's names; knows nothing about the sheet. |
| `nation.py` | create | `Reading` and `NationStatus` — the Nation panel plus the server clock. Knows the game's names; knows nothing about the sheet. |
| `overview.py` | modify | Add `cell_text` mode to `PanelParser` and the `parse_panel_text` entry point. |
| `sheets.py` | modify | Add `column_letter`, `find_in_row`, `index_column` — pure A1 geometry. |
| `stockpiles.py` | rewrite | Locate both sheet regions by lookup and write both. Re-exports the names `clop_monitor.py` imports. |
| `clop_monitor.py` | modify | `sync_sheet_step` parses into the two value objects and makes one `snapshot` call. |
| `test_goods.py` | create | Table integrity and `Stockpiles`. |
| `test_nation.py` | create | Every Nation-panel cell shape, including the game's non-numeric per-tick strings. |
| `test_overview.py` | modify | `parse_panel_text`. |
| `test_sheets.py` | modify | The three grid helpers. |
| `test_stockpiles.py` | rewrite | Locators, run grouping, write payloads, per-region independence. |
| `docs/2026-08-23-dashboard-goods-map.md` | modify | Add the status rows; record that the script writes this tab. |
| `README.md` | modify | Describe both tabs; refresh the test count. |

**Ordering note:** Tasks 1–5 create the pure building blocks and are independently committable. Task 6 onward depends on them. Do not start Task 6 before Task 5 is committed.

---

### Task 1: `goods.py` — the goods vocabulary

**Files:**
- Create: `D:\Koan\clop\automation-poc\goods.py`
- Test: `D:\Koan\clop\automation-poc\test_goods.py`

- [ ] **Step 1: Write the failing test**

Create `test_goods.py`:

```python
#!/usr/bin/env python3
"""Offline unit tests for goods.py -- no network."""

import unittest

from goods import (
    BY_DASHBOARD_LABEL,
    BY_GAME_NAME,
    BY_RESOURCE_ID,
    BY_STOCK_LABEL,
    GOODS,
    Good,
)


class GoodsTableTests(unittest.TestCase):
    def test_thirty_one_goods(self):
        # The game has exactly 31 rows in resourcedefs with is_building = 0.
        # See docs/2026-08-23-dashboard-goods-map.md.
        self.assertEqual(len(GOODS), 31)

    def test_game_names_unique(self):
        names = [good.game_name for good in GOODS]
        self.assertEqual(len(set(names)), len(names))

    def test_dashboard_labels_unique(self):
        labels = [good.dashboard_label for good in GOODS]
        self.assertEqual(len(set(labels)), len(labels))

    def test_resource_ids_unique(self):
        ids = [good.resource_id for good in GOODS]
        self.assertEqual(len(set(ids)), len(ids))

    def test_six_stock_labels_and_they_are_unique(self):
        labels = [good.stock_label for good in GOODS if good.stock_label]
        self.assertEqual(sorted(labels), ["apple", "coffee", "gems", "mpart", "oil", "vpart"])

    def test_the_four_abbreviated_dashboard_labels(self):
        # The only labels that are not the game name verbatim. Each has exactly one candidate
        # in resourcedefs, which is why the mapping is safe.
        self.assertEqual(BY_DASHBOARD_LABEL["Gas"].game_name, "Gasoline")
        self.assertEqual(BY_DASHBOARD_LABEL["M Parts"].game_name, "Machinery Parts")
        self.assertEqual(BY_DASHBOARD_LABEL["V Parts"].game_name, "Vehicle Parts")
        self.assertEqual(BY_DASHBOARD_LABEL["P Parts"].game_name, "Precision Parts")

    def test_dna_labels_reverse_the_game_word_order(self):
        self.assertEqual(
            BY_DASHBOARD_LABEL["DNA - Burro - Central"].game_name, "DNA - Central Burrozil"
        )
        self.assertEqual(
            BY_DASHBOARD_LABEL["DNA - Prze - South"].game_name, "DNA - South Przewalskia"
        )

    def test_known_resource_ids(self):
        self.assertEqual(BY_GAME_NAME["Apples"].resource_id, 3)
        self.assertEqual(BY_GAME_NAME["Machinery Parts"].resource_id, 10)
        self.assertEqual(BY_GAME_NAME["Apotheosis Serum"].resource_id, 77)
        self.assertEqual(BY_RESOURCE_ID[9].game_name, "Vehicle Parts")

    def test_stock_index_covers_only_the_six(self):
        self.assertEqual(len(BY_STOCK_LABEL), 6)
        self.assertEqual(BY_STOCK_LABEL["mpart"].game_name, "Machinery Parts")

    def test_good_is_frozen(self):
        with self.assertRaises(Exception):
            GOODS[0].game_name = "nope"


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_goods -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'goods'`

- [ ] **Step 3: Write the implementation**

Create `goods.py`:

```python
#!/usr/bin/env python3
"""The game's 31 goods, and a parse-once handle on how much of each a nation holds.

Every good carries all three names it is known by at once -- the game's, the ``Dashboard`` tab's,
and (for six of them) the nation tab's ``STOCK`` block -- so there is one table to keep right
instead of three lists to keep in step:

    Good(game_name="Machinery Parts", resource_id=10, dashboard_label="M Parts", stock_label="mpart")

``game_name`` is ``resourcedefs.name`` exactly as ``overview.php`` renders it; the table is the 31
rows of ``resourcedefs`` with ``is_building = 0``, taken from the game's seed data
(``clop/tables with data.sql``). ``docs/2026-08-23-dashboard-goods-map.md`` is the same table with
its provenance written out.

``GOODS`` carries **no ordering obligation**. Both sheet regions are located by looking their labels
up, so the order values are written in comes from the rows the lookup found. The order here happens
to match the Dashboard's for readability, and nothing may come to depend on that.

This module knows the game's vocabulary and nothing about the sheet. See ``stockpiles.py`` for the
writing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from overview import parse_panel


@dataclass(frozen=True)
class Good:
    """One tradeable/holdable resource, under each of the names it goes by."""

    game_name: str                  # resourcedefs.name, as overview.php renders it
    resource_id: int                # for cross-referencing the game's seed data
    dashboard_label: str            # Dashboard tab, column A
    stock_label: Optional[str] = None   # nation tab, column Q; None for the 25 not in that block


#: All 31 goods. Order matches the Dashboard's rows for readability only -- see the module docstring.
GOODS: Tuple[Good, ...] = (
    Good("Energy", 4, "Energy"),
    Good("Apples", 3, "Apples", "apple"),
    Good("Coffee", 20, "Coffee", "coffee"),
    Good("Oil", 1, "Oil", "oil"),
    Good("Gasoline", 25, "Gas"),
    Good("Gems", 26, "Gems", "gems"),
    Good("Cider", 18, "Cider"),
    Good("Pies", 13, "Pies"),
    Good("Toys", 47, "Toys"),
    Good("Tungsten", 27, "Tungsten"),
    Good("Plastics", 28, "Plastics"),
    Good("Drugs", 42, "Drugs"),
    Good("Copper", 2, "Copper"),
    Good("Machinery Parts", 10, "M Parts", "mpart"),
    Good("Vehicle Parts", 9, "V Parts", "vpart"),
    Good("Precision Parts", 29, "P Parts"),
    Good("Composites", 30, "Composites"),
    Good("Forbidden Research", 75, "Forbidden Research"),
    Good("Apotheosis Serum", 77, "Apotheosis Serum"),
    Good("DNA - Central Burrozil", 69, "DNA - Burro - Central"),
    Good("DNA - North Burrozil", 68, "DNA - Burro - North"),
    Good("DNA - South Burrozil", 70, "DNA - Burro - South"),
    Good("DNA - Central Przewalskia", 72, "DNA - Prze - Central"),
    Good("DNA - North Przewalskia", 71, "DNA - Prze - North"),
    Good("DNA - South Przewalskia", 73, "DNA - Prze - South"),
    Good("DNA - Central Saddle Arabia", 63, "DNA - Saddle - Central"),
    Good("DNA - North Saddle Arabia", 62, "DNA - Saddle - North"),
    Good("DNA - South Saddle Arabia", 64, "DNA - Saddle - South"),
    Good("DNA - Central Zebrica", 66, "DNA - Zebrica - Central"),
    Good("DNA - North Zebrica", 65, "DNA - Zebrica - North"),
    Good("DNA - South Zebrica", 67, "DNA - Zebrica - South"),
)

BY_GAME_NAME: Dict[str, Good] = {good.game_name: good for good in GOODS}
BY_DASHBOARD_LABEL: Dict[str, Good] = {good.dashboard_label: good for good in GOODS}
BY_STOCK_LABEL: Dict[str, Good] = {
    good.stock_label: good for good in GOODS if good.stock_label
}
BY_RESOURCE_ID: Dict[int, Good] = {good.resource_id: good for good in GOODS}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_goods -v`
Expected: PASS, 10 tests OK

- [ ] **Step 5: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add goods.py test_goods.py
git commit -m "feat: add the goods vocabulary table"
```

---

### Task 2: `goods.py` — `Stockpiles`

**Files:**
- Modify: `D:\Koan\clop\automation-poc\goods.py`
- Test: `D:\Koan\clop\automation-poc\test_goods.py`

The parse logic moves here verbatim from `stockpiles.parse_overview_resources`; Task 8 deletes the original.

- [ ] **Step 1: Write the failing test**

Append to `test_goods.py`, and extend the import at the top of the file to read:

```python
from goods import (
    BY_DASHBOARD_LABEL,
    BY_GAME_NAME,
    BY_RESOURCE_ID,
    BY_STOCK_LABEL,
    GOODS,
    Good,
    StockpileError,
    Stockpiles,
    parse_overview_resources,
)
```

```python
# A minimal overview.php Resources panel: icon cells, comma formatting, trailing centred columns,
# plus decoy panels that share its exact row shape.
RESOURCES_HTML = """
<div class="panel-heading">Resources</div>
<table class="table"><tbody>
  <tr>
    <td style="width: 16px;"><img src="images/icons/Apples.png"/></td>
    <td style="text-align: right;">Apples</td>
    <td><span class="text-success">1,226</span></td>
    <td style="text-align: center;"><span class="text-success">0</span></td>
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
        result = parse_overview_resources(RESOURCES_HTML)
        self.assertEqual(set(result), {"Apples", "Gems"})
        self.assertNotIn("Gem Mine", result)      # buildings panel ignored
        self.assertIsNone(result.get("Oil"))      # the weapons-panel decoy is not picked up

    def test_commas_stripped(self):
        self.assertEqual(parse_overview_resources(RESOURCES_HTML)["Apples"], 1226)

    def test_row_without_icon_cell_parsed(self):
        # hideicons drops the leading <td>; the name is then the first cell.
        self.assertEqual(parse_overview_resources(RESOURCES_HTML)["Gems"], 6)

    def test_unparseable_quantity_raises(self):
        html = """
<div class="panel-heading">Resources</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Apples</td><td><span>N/A</span></td></tr>
</tbody></table>
"""
        with self.assertRaises(StockpileError) as caught:
            parse_overview_resources(html)
        self.assertIn("Apples", str(caught.exception))

    def test_trailing_garbage_in_a_quantity_raises(self):
        # fullmatch, not match: "226 x" must not silently read as 226.
        html = """
<div class="panel-heading">Resources</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Apples</td><td><span>226 x</span></td></tr>
</tbody></table>
"""
        with self.assertRaises(StockpileError):
            parse_overview_resources(html)

    def test_missing_resources_panel_yields_no_rows(self):
        # The parser stays permissive; deciding an absent panel means a broken page is the
        # caller's job -- see overview.require_valid_overview.
        self.assertEqual(parse_overview_resources("<html></html>"), {})


class StockpilesTests(unittest.TestCase):
    def test_from_overview_reads_the_panel(self):
        stock = Stockpiles.from_overview(RESOURCES_HTML)
        self.assertEqual(stock["Apples"], 1226)
        self.assertEqual(stock["Gems"], 6)

    def test_absent_good_is_zero(self):
        # A good the nation holds none of is simply not rendered on the page.
        stock = Stockpiles.from_overview(RESOURCES_HTML)
        self.assertEqual(stock.get("Toys"), 0)
        self.assertEqual(stock["Toys"], 0)

    def test_contains(self):
        stock = Stockpiles.from_overview(RESOURCES_HTML)
        self.assertIn("Apples", stock)
        self.assertNotIn("Toys", stock)

    def test_as_dict_is_a_copy(self):
        stock = Stockpiles.from_overview(RESOURCES_HTML)
        snapshot = stock.as_dict()
        snapshot["Apples"] = 0
        self.assertEqual(stock["Apples"], 1226)

    def test_unknown_good_name_still_reads_zero(self):
        # Callers ask by game name; an unrecognised one is "you hold none", not an error. The
        # goods table is what decides which names are asked for.
        self.assertEqual(Stockpiles.from_overview(RESOURCES_HTML).get("Nonsense"), 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_goods -v`
Expected: FAIL — `ImportError: cannot import name 'Stockpiles' from 'goods'`

- [ ] **Step 3: Write the implementation**

Append to `goods.py`:

```python
class StockpileError(RuntimeError):
    """A quantity on overview.php that cannot be read as a whole number.

    Raised rather than skipping the row. Skipping would let the good fall through to zero and be
    written to the sheet as "you hold none", stamped freshly verified -- indistinguishable from the
    player actually having spent the lot. The game formats every Qty with its integer ``commas()``
    helper, so anything else means the page changed and a human should look.
    """


def parse_overview_resources(html: str) -> Dict[str, int]:
    """Return ``{game_name: qty}`` from the overview "Resources" panel.

    Only resources the nation has a row for are rendered, so a good it holds none of is absent from
    the result rather than present as zero; ``Stockpiles.get`` is what turns that into a zero.
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


class Stockpiles:
    """What the nation holds, parsed once from one overview.php fetch.

    This is the handle other features take instead of re-fetching or re-parsing the page::

        stock = Stockpiles.from_overview(html)
        stock["Apples"]     # 218
        stock.get("Toys")   # 0 -- absent from overview means the nation holds none
        "Gems" in stock     # False when the nation holds none

    ``get`` and ``__getitem__`` behave identically and never raise: a good missing from the page is
    a nation holding zero of it, which is the answer every caller wants. ``__contains__`` is there
    for the rarer caller that needs to tell "holds zero" from "was not on the page".
    """

    def __init__(self, amounts: Dict[str, int]) -> None:
        self._amounts = dict(amounts)

    @classmethod
    def from_overview(cls, html: str) -> "Stockpiles":
        """Parse the Resources panel. Raises ``StockpileError`` on an unreadable quantity."""
        return cls(parse_overview_resources(html))

    def get(self, game_name: str, default: int = 0) -> int:
        return self._amounts.get(game_name, default)

    def __getitem__(self, game_name: str) -> int:
        return self._amounts.get(game_name, 0)

    def __contains__(self, game_name: str) -> bool:
        return game_name in self._amounts

    def __len__(self) -> int:
        return len(self._amounts)

    def as_dict(self) -> Dict[str, int]:
        """A copy of the underlying mapping, safe for the caller to mutate."""
        return dict(self._amounts)

    def __repr__(self) -> str:
        return f"Stockpiles({self._amounts!r})"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_goods -v`
Expected: PASS, 21 tests OK

- [ ] **Step 5: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add goods.py test_goods.py
git commit -m "feat: add Stockpiles, the parse-once handle on held quantities"
```

---

### Task 3: `overview.py` — whole-cell panel parsing

**Files:**
- Modify: `D:\Koan\clop\automation-poc\overview.py:41-105`
- Test: `D:\Koan\clop\automation-poc\test_overview.py`

`parse_panel` captures the **first `<span>`** of a value cell. The Nation panel needs the whole cell, because the per-tick figure is a *second* span — and under some governments is not in a span at all.

- [ ] **Step 1: Write the failing test**

Add to `test_overview.py`, extending its import to include `parse_panel_text`:

```python
NATION_PANEL = """
<div class="panel-heading">Nation</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Government Type</td><td>Loose Despotism</td></tr>
  <tr><td style="text-align: right;">Economic Type</td><td>Poorly Defined</td></tr>
  <tr><td style="text-align: right;"><span class="text-danger">Warning:</span></td>
      <td><span class="text-danger">Your economic type is not active!</span></td></tr>
  <tr><td style="text-align: right;">Relationship with Solar Empire</td>
      <td><span class="text-danger">-120</span>
          (<span class="text-success">3</span> per tick)</td></tr>
  <tr><td style="text-align: right;">Relationship with New Lunar Republic</td>
      <td><span class="text-success">1500</span>
          (Ascending)</td></tr>
  <tr><td style="text-align: right;">Satisfaction</td>
      <td><span class="text-success">218</span> (<span class="text-danger">-5</span> per tick)</td></tr>
  <tr><td style="text-align: right;">GDP</td>
      <td><span class="text-success">60,900</span> bits per tick</td></tr>
  <tr><td style="text-align: right;">Funds</td>
      <td><span class="text-success">1,234,567</span> bits</td></tr>
</tbody></table>
"""


class ParsePanelTextTests(unittest.TestCase):
    def rows(self):
        return dict(parse_panel_text(NATION_PANEL, "Nation"))

    def test_two_span_cell_captured_whole(self):
        # parse_panel would stop at "-120" and lose the per-tick figure entirely.
        self.assertEqual(self.rows()["Relationship with Solar Empire"], "-120 (3 per tick)")

    def test_cell_whose_per_tick_is_bare_text(self):
        # Alicorn Elite / Transponyism render "(Ascending)" with no span at all.
        self.assertEqual(
            self.rows()["Relationship with New Lunar Republic"], "1500 (Ascending)"
        )

    def test_cell_with_no_span_captured(self):
        # parse_panel drops these rows entirely, because it needs a span to capture.
        self.assertEqual(self.rows()["Government Type"], "Loose Despotism")

    def test_trailing_text_after_the_span_kept(self):
        self.assertEqual(self.rows()["GDP"], "60,900 bits per tick")
        self.assertEqual(self.rows()["Funds"], "1,234,567 bits")

    def test_whitespace_collapsed(self):
        self.assertEqual(self.rows()["Satisfaction"], "218 (-5 per tick)")

    def test_name_cell_containing_a_span_still_reads_as_the_name(self):
        # The conditional "Warning:" row (rendered when active_economy is false).
        self.assertEqual(self.rows()["Warning:"], "Your economic type is not active!")

    def test_still_arms_only_on_an_exact_heading(self):
        # Favourite actions render as class="panel-heading h4" with a user-chosen label. A loose
        # match would let a favourite action named "Nation" impersonate the Nation panel.
        html = NATION_PANEL.replace('class="panel-heading"', 'class="panel-heading h4"')
        self.assertEqual(parse_panel_text(html, "Nation"), [])

    def test_parse_panel_is_unchanged_by_the_new_mode(self):
        # The span-capturing behaviour buildings.py and goods.py rely on must not shift.
        rows = dict(parse_panel(NATION_PANEL, "Nation"))
        self.assertEqual(rows["Satisfaction"], "218")
        self.assertNotIn("Government Type", rows)   # no span in that cell
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_overview -v`
Expected: FAIL — `ImportError: cannot import name 'parse_panel_text' from 'overview'`

- [ ] **Step 3: Write the implementation**

In `overview.py`, replace `PanelParser.__init__`'s signature and first lines (currently at line 44) so it reads:

```python
    def __init__(self, heading: str, cell_text: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._heading = heading
        self._cell_text = cell_text
        self._in_heading = False
```

In `handle_starttag`, replace the final `elif` branch (currently `elif tag == "span" and self._name is not None and self._value is None:`) with these two branches:

```python
        elif (
            self._cell_text
            and tag == "td"
            and self._name is not None
            and self._value is None
        ):
            self._capture = "cell"
            self._buf = []
        elif (
            not self._cell_text
            and tag == "span"
            and self._name is not None
            and self._value is None
        ):
            self._capture = "value"
            self._buf = []
```

In `handle_endtag`, insert a branch after the `if tag == "td" and self._capture == "name":` block:

```python
        elif tag == "td" and self._capture == "cell":
            # The whole cell, tags stripped and whitespace collapsed. Nested tags contribute their
            # text and do not end capture -- only this cell's own </td> does.
            self._value = " ".join("".join(self._buf).split())
            self._capture = None
```

Add after `parse_panel`:

```python
def parse_panel_text(html: str, heading: str) -> List[Tuple[str, str]]:
    """Return ``[(name, whole_cell_text), ...]`` for the panel headed ``heading``.

    ``parse_panel`` captures a value cell's *first* ``<span>``, which is right for Resources and
    Buildings and wrong for the Nation panel: its per-tick figure is a second span in the same cell
    (``overview.php:82-86``), and under Alicorn Elite / Transponyism the game emits a bare
    ``(Ascending)`` with no span at all (``overview.php:50-60``). Whole-cell text is the only shape
    that covers all three. As a side effect it also reads the span-less cells -- Government Type,
    Economic Type -- which ``parse_panel`` drops.

    This shares ``PanelParser`` rather than being a second parser on purpose: the "arm only on a
    ``panel-heading`` div whose text matches exactly" rule lives there, and it is what stops a
    favourite action named ``Nation`` impersonating the Nation panel.
    """
    parser = PanelParser(heading, cell_text=True)
    parser.feed(html)
    return parser.rows
```

Also update the module docstring's opening paragraph to mention both modes — after the existing sentence about the shared row shape, add:

```
``parse_panel`` captures the value cell's first ``<span>``; ``parse_panel_text`` captures the whole
cell instead, which the Nation panel needs. Both arm through the same heading rule.
```

- [ ] **Step 4: Run the whole suite to verify nothing regressed**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest -v 2>&1 | tail -5`
Expected: PASS, 0 failures. The `parse_panel` behaviour used by `buildings.py` and `test_buildings.py` must be untouched.

- [ ] **Step 5: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add overview.py test_overview.py
git commit -m "feat: add whole-cell panel parsing for the Nation panel"
```

---

### Task 4: `nation.py` — `Reading` and `NationStatus`

**Files:**
- Create: `D:\Koan\clop\automation-poc\nation.py`
- Test: `D:\Koan\clop\automation-poc\test_nation.py`

- [ ] **Step 1: Write the failing test**

Create `test_nation.py`:

```python
#!/usr/bin/env python3
"""Offline unit tests for nation.py -- no network.

The cases that matter are the game's *non-numeric* per-tick values. backend_overview.php sets
$seperturn to the literal string "Fixed" for Solar Vassal / Lunar Client (lines 320-325), and
overview.php emits a bare "(Ascending)" for Alicorn Elite / Transponyism (lines 50-60). A parser
that assumed an integer there would raise on two perfectly normal governments.
"""

import unittest

from nation import NationStatus, NationStatusError, Reading, parse_server_time


HEADER = '<li><a>Server time: 2026-08-23 11:42:12</a></li>'


def panel(
    se="<span>-120</span>\n          (<span>3</span> per tick)",
    nlr="<span>1500</span>\n          (<span>-7</span> per tick)",
    satisfaction='<span class="text-success">218</span> (<span>-5</span> per tick)',
    gdp='<span class="text-success">60,900</span> bits per tick',
    funds='<span class="text-success">1,234,567</span> bits',
    extra="",
    header=HEADER,
):
    return f"""{header}
<div class="panel-heading">Nation</div>
<table class="table"><tbody>
  <tr><td style="text-align: right;">Government Type</td><td>Loose Despotism</td></tr>
  <tr><td style="text-align: right;">Economic Type</td><td>Poorly Defined</td></tr>
  {extra}
  <tr><td style="text-align: right;">Relationship with Solar Empire</td><td>{se}</td></tr>
  <tr><td style="text-align: right;">Relationship with New Lunar Republic</td><td>{nlr}</td></tr>
  <tr><td style="text-align: right;">Satisfaction</td><td>{satisfaction}</td></tr>
  <tr><td style="text-align: right;">GDP</td><td>{gdp}</td></tr>
  <tr><td style="text-align: right;">Funds</td><td>{funds}</td></tr>
</tbody></table>
"""


class ReadingTests(unittest.TestCase):
    def test_display_numeric(self):
        self.assertEqual(Reading(218, -5).display(), "218 (-5)")

    def test_display_non_numeric_per_tick(self):
        self.assertEqual(Reading(1500, "Ascending").display(), "1500 (Ascending)")


class NationStatusTests(unittest.TestCase):
    def test_ordinary_page(self):
        status = NationStatus.from_overview(panel())
        self.assertEqual(status.government, "Loose Despotism")
        self.assertEqual(status.economy, "Poorly Defined")
        self.assertEqual(status.se, Reading(-120, 3))
        self.assertEqual(status.nlr, Reading(1500, -7))
        self.assertEqual(status.satisfaction, Reading(218, -5))
        self.assertEqual(status.gdp, 60900)
        self.assertEqual(status.funds, 1234567)
        self.assertEqual(status.server_time, "2026-08-23 11:42:12")

    def test_ascending_per_tick_kept_verbatim(self):
        status = NationStatus.from_overview(panel(se="<span>1500</span>\n          (Ascending)"))
        self.assertEqual(status.se, Reading(1500, "Ascending"))
        self.assertEqual(status.se.display(), "1500 (Ascending)")

    def test_fixed_per_tick_kept_verbatim(self):
        status = NationStatus.from_overview(
            panel(nlr="<span>40</span> (<span>Fixed</span> per tick)")
        )
        self.assertEqual(status.nlr, Reading(40, "Fixed"))

    def test_negative_current_with_commas(self):
        status = NationStatus.from_overview(panel(se="<span>-1,200</span> (<span>12</span> per tick)"))
        self.assertEqual(status.se, Reading(-1200, 12))

    def test_inactive_economy_warning_row_ignored(self):
        extra = (
            '<tr><td style="text-align: right;"><span class="text-danger">Warning:</span></td>'
            '<td><span class="text-danger">Your economic type is not active!</span></td></tr>'
        )
        self.assertEqual(NationStatus.from_overview(panel(extra=extra)).gdp, 60900)

    def test_corrupted_gdp_rejected(self):
        # The game's commas() helper (allfunctions.php:26-31) walks the *string* form inserting a
        # comma every three characters, so a fractional GDP renders as garbage. Never write that.
        with self.assertRaises(NationStatusError) as caught:
            NationStatus.from_overview(panel(gdp="<span>5,062,25.</span> bits per tick"))
        self.assertIn("GDP", str(caught.exception))

    def test_missing_row_raises(self):
        html = panel().replace(
            '<tr><td style="text-align: right;">Funds</td>'
            '<td><span class="text-success">1,234,567</span> bits</td></tr>',
            "",
        )
        with self.assertRaises(NationStatusError) as caught:
            NationStatus.from_overview(html)
        self.assertIn("Funds", str(caught.exception))

    def test_missing_nation_panel_raises(self):
        with self.assertRaises(NationStatusError):
            NationStatus.from_overview(HEADER + "<html></html>")

    def test_reading_without_parentheses_raises(self):
        with self.assertRaises(NationStatusError) as caught:
            NationStatus.from_overview(panel(satisfaction="<span>218</span>"))
        self.assertIn("Satisfaction", str(caught.exception))


class ServerTimeTests(unittest.TestCase):
    def test_stamp_read_verbatim(self):
        self.assertEqual(parse_server_time(HEADER), "2026-08-23 11:42:12")

    def test_missing_stamp_raises(self):
        # A page without it is not the page we think it is, and a snapshot with no staleness
        # marker is worse than no snapshot.
        with self.assertRaises(NationStatusError):
            parse_server_time("<html>nothing here</html>")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_nation -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nation'`

- [ ] **Step 3: Write the implementation**

Create `nation.py`:

```python
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
        """Parse the Nation panel and the page header. Raises ``NationStatusError`` on anything odd."""
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_nation -v`
Expected: PASS, 13 tests OK

Note: `test_reading_without_parentheses_raises` expects `"Satisfaction"` in the message, and
`_reading` is called with the field name `"Satisfaction"` — check the message text if it fails.

- [ ] **Step 5: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add nation.py test_nation.py
git commit -m "feat: add NationStatus, the parse-once handle on the Nation panel"
```

---

### Task 5: `sheets.py` — grid helpers

**Files:**
- Modify: `D:\Koan\clop\automation-poc\sheets.py` (add after `cell_int`, around line 79)
- Test: `D:\Koan\clop\automation-poc\test_sheets.py`

- [ ] **Step 1: Write the failing test**

Add to `test_sheets.py`, extending its `from sheets import (...)` block with `column_letter`, `find_in_row` and `index_column`:

```python
class ColumnLetterTests(unittest.TestCase):
    def test_first_columns(self):
        self.assertEqual(column_letter(0), "A")
        self.assertEqual(column_letter(2), "C")
        self.assertEqual(column_letter(25), "Z")

    def test_past_z(self):
        # The Dashboard has eleven populated columns today, but nothing may assume it stays
        # single-letter: a tenth nation joining walks it toward AA.
        self.assertEqual(column_letter(26), "AA")
        self.assertEqual(column_letter(27), "AB")
        self.assertEqual(column_letter(51), "AZ")
        self.assertEqual(column_letter(52), "BA")

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            column_letter(-1)


class FindInRowTests(unittest.TestCase):
    def test_exact_match(self):
        row = ["READ ONLY", "TOTAL", "LePone(Z)", "quaity(P)"]
        self.assertEqual(find_in_row(row, "LePone(Z)"), 2)

    def test_surrounding_whitespace_ignored_on_both_sides(self):
        self.assertEqual(find_in_row(["  LePone(Z) "], " LePone(Z)"), 0)

    def test_not_found_is_none(self):
        self.assertIsNone(find_in_row(["TOTAL", "#N/A"], "LePone(Z)"))

    def test_substring_does_not_match(self):
        # "LePone" must not resolve to the "LePone(Z)" column: the tab name is the whole cell.
        self.assertIsNone(find_in_row(["LePone(Z)"], "LePone"))

    def test_none_cells_skipped(self):
        self.assertEqual(find_in_row([None, "", "SE"], "SE"), 2)


class IndexColumnTests(unittest.TestCase):
    def test_rows_are_one_based(self):
        grid = [["Energy"], ["Apples"], ["Coffee"]]
        self.assertEqual(index_column(grid), {"Energy": [1], "Apples": [2], "Coffee": [3]})

    def test_blank_rows_skipped_not_numbered_away(self):
        grid = [["Sat"], [""], ["GDP"]]
        self.assertEqual(index_column(grid), {"Sat": [1], "GDP": [3]})

    def test_duplicates_all_reported(self):
        # Returning every occurrence is what lets the caller refuse to write an ambiguous sheet.
        grid = [["Gems"], ["Oil"], ["Gems"]]
        self.assertEqual(index_column(grid)["Gems"], [1, 3])

    def test_short_and_empty_rows_tolerated(self):
        self.assertEqual(index_column([[], ["Oil"], [None]]), {"Oil": [2]})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_sheets -v`
Expected: FAIL — `ImportError: cannot import name 'column_letter' from 'sheets'`

- [ ] **Step 3: Write the implementation**

Add to `sheets.py` immediately after `cell_int`:

```python
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
```

Extend the module's typing import (line 31) to:

```python
from typing import Any, Dict, List, Optional, Sequence
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_sheets -v`
Expected: PASS, all existing plus 12 new tests OK

- [ ] **Step 5: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add sheets.py test_sheets.py
git commit -m "feat: add column_letter, find_in_row and index_column to sheets"
```

---

### Task 6: `stockpiles.py` — locate the nation-tab block

**Files:**
- Rewrite: `D:\Koan\clop\automation-poc\stockpiles.py`
- Test: `D:\Koan\clop\automation-poc\test_stockpiles.py`

This task replaces the hardcoded `Q11:Q16` / `R11:R16` / `W10` addresses with a lookup. The rest of the module keeps working through the end of Task 8; write the new module top-down across Tasks 6–8 and only run the full suite green at the end of Task 8.

The live layout this must handle (from `LePone(Z)`): `Q10` = `STOCK`, `R10` = `HAVE`, `S10` = `NEED`, `T10` = `BUY`, `W10` = the timestamp, `Q11:Q16` = the six labels, `Q17:Q18` blank, then **`Q19` = `COST`** with `Copper`, `M Part`, `V Part`, `P Part` under it. Stopping at the first blank row is what keeps the `COST` block out.

- [ ] **Step 1: Write the failing test**

Replace the whole of `test_stockpiles.py` with this file. (The parse tests it used to hold now live in `test_goods.py` and `test_nation.py`; do not duplicate them.)

```python
#!/usr/bin/env python3
"""Offline unit tests for stockpiles.py -- no network.

These exist to prove "lookup, not hardcode" is real. Each test moves something on the fake sheet
that a hardcoded address would have got wrong.
"""

import unittest

from stockpiles import (
    NationBlock,
    contiguous_runs,
    locate_nation_block,
)


# Column Q onward of a nation tab, as read by NATION_SCAN_RANGE ("Q1:W60"). Index 0 is Q.
def nation_grid(header_row=10, have_at=1, labels=None, stamp="2026-08-23 11:42:12", tail=True):
    """Build the Q..W grid: blank rows, a STOCK header, the label run, then the COST block."""
    labels = ["apple", "oil", "coffee", "mpart", "vpart", "gems"] if labels is None else labels
    grid = [["", "", "", "", "", "", ""] for _ in range(header_row - 1)]
    header = ["STOCK", "", "", "", "", "", ""]
    header[have_at] = "HAVE"
    header[6] = stamp
    grid.append(header)
    for label in labels:
        grid.append([label, "0", "", "", "", "", ""])
    if tail:
        grid.append(["", "", "", "", "", "", ""])
        grid.append(["", "", "", "", "", "", ""])
        grid.append(["COST", "Bits", "", "", "", "", ""])
        grid.append(["Copper", "200", "", "", "", "", ""])
        grid.append(["M Part", "10", "", "", "", "", ""])
    return grid


class LocateNationBlockTests(unittest.TestCase):
    def test_todays_layout(self):
        block, problems = locate_nation_block(nation_grid())
        self.assertEqual(problems, [])
        self.assertEqual(block.header_row, 10)
        self.assertEqual(block.value_column, "R")
        self.assertEqual(block.timestamp_cell, "W10")
        self.assertEqual(
            block.rows,
            {"apple": 11, "oil": 12, "coffee": 13, "mpart": 14, "vpart": 15, "gems": 16},
        )

    def test_inserted_row_shifts_the_block(self):
        # Somebody adds a row above the STOCK header. A hardcoded Q11:Q16 would now write apples
        # into the oil row; the lookup just returns different rows.
        block, problems = locate_nation_block(nation_grid(header_row=12))
        self.assertEqual(problems, [])
        self.assertEqual(block.header_row, 12)
        self.assertEqual(block.rows["apple"], 13)
        self.assertEqual(block.timestamp_cell, "W12")

    def test_have_column_moved(self):
        # HAVE is found in the header row, so moving it moves the values with it.
        block, problems = locate_nation_block(nation_grid(have_at=3))
        self.assertEqual(problems, [])
        self.assertEqual(block.value_column, "T")

    def test_cost_block_below_is_not_picked_up(self):
        block, _ = locate_nation_block(nation_grid())
        self.assertNotIn("Copper", block.rows)
        self.assertNotIn("M Part", block.rows)
        self.assertNotIn("COST", block.rows)

    def test_missing_stock_header(self):
        grid = [["", "", "", "", "", "", ""] for _ in range(5)]
        block, problems = locate_nation_block(grid)
        self.assertIsNone(block)
        self.assertEqual(len(problems), 1)
        self.assertIn("STOCK", problems[0])

    def test_missing_have_header(self):
        grid = nation_grid()
        grid[9][1] = ""
        block, problems = locate_nation_block(grid)
        self.assertIsNone(block)
        self.assertIn("HAVE", problems[0])

    def test_missing_stock_label(self):
        block, problems = locate_nation_block(
            nation_grid(labels=["apple", "oil", "coffee", "mpart", "vpart"])
        )
        self.assertIsNone(block)
        self.assertEqual(len(problems), 1)
        self.assertIn("gems", problems[0])

    def test_all_missing_labels_reported_not_just_the_first(self):
        block, problems = locate_nation_block(nation_grid(labels=["apple", "oil"]))
        self.assertIsNone(block)
        self.assertEqual(len(problems), 4)

    def test_duplicate_label_in_the_run(self):
        block, problems = locate_nation_block(
            nation_grid(labels=["apple", "apple", "oil", "coffee", "mpart", "vpart", "gems"])
        )
        self.assertIsNone(block)
        self.assertTrue(any("apple" in problem and "twice" in problem for problem in problems))

    def test_run_ending_at_the_grid_edge(self):
        # No COST block below and no trailing blank: the run must end at the last row, not raise.
        block, problems = locate_nation_block(nation_grid(tail=False))
        self.assertEqual(problems, [])
        self.assertEqual(block.rows["gems"], 16)


class ContiguousRunsTests(unittest.TestCase):
    def test_single_run(self):
        self.assertEqual(
            contiguous_runs({"a": 11, "b": 12, "c": 13}),
            [[(11, "a"), (12, "b"), (13, "c")]],
        )

    def test_gaps_split_runs(self):
        self.assertEqual(
            contiguous_runs({"a": 2, "b": 3, "c": 7, "d": 8}),
            [[(2, "a"), (3, "b")], [(7, "c"), (8, "d")]],
        )

    def test_lone_row_is_its_own_run(self):
        self.assertEqual(contiguous_runs({"a": 5}), [[(5, "a")]])

    def test_unordered_input_sorted(self):
        self.assertEqual(contiguous_runs({"b": 3, "a": 2}), [[(2, "a"), (3, "b")]])

    def test_empty(self):
        self.assertEqual(contiguous_runs({}), [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_stockpiles -v`
Expected: FAIL — `ImportError: cannot import name 'DASHBOARD_LABELS' from 'stockpiles'`

- [ ] **Step 3: Write the implementation**

Replace the whole of `stockpiles.py` with the header, constants and nation-tab locator below. Tasks 7 and 8 append to this same file.

```python
#!/usr/bin/env python3
"""Snapshot the nation's stockpiles and status onto the shared sheet -- both tabs, one page fetch.

Two regions are written from one read of ``overview.php``:

1. the player's own **nation tab** -- the six goods in the ``STOCK`` block's ``HAVE`` column, plus
   the timestamp beside the header;
2. the alliance-wide **Dashboard tab** -- the nation's own column: all 31 goods and six status rows
   (``Active``, ``Sat``, ``NLR``, ``SE``, ``GDP``, ``Bits``).

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
    GOODS,
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

DASHBOARD_TAB = "Dashboard"

#: Row 1 holds the nation names, column A the row labels; 60 rows covers the block with room.
DASHBOARD_SCAN_RANGE = "A1:Z60"

#: The Dashboard's non-goods rows, in the order they appear. ``Active`` holds a last-updated
#: timestamp despite its label -- the sheet owner's instruction, recorded so it is not "fixed".
STATUS_LABELS: Tuple[str, ...] = ("Active", "Sat", "NLR", "SE", "GDP", "Bits")

#: Every label this module expects to find in the Dashboard's column A: 6 status + 31 goods.
DASHBOARD_LABELS: Tuple[str, ...] = STATUS_LABELS + tuple(
    good.dashboard_label for good in GOODS
)

_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


@dataclass(frozen=True)
class NationBlock:
    """Where the nation tab's STOCK block turned out to be."""

    header_row: int             # 1-based row of the STOCK header
    value_column: str           # A1 letter of the HAVE column
    rows: Dict[str, int]        # stock_label -> 1-based row
    timestamp_cell: str         # e.g. "W10"


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
        return (
            f"{cell} should be empty or hold a 'YYYY-MM-DD HH:MM:SS' stamp but reads {text!r}"
        )
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
        problems.append(
            f"no {STOCK_HEADER!r} header found in column Q of the nation tab"
        )
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
```

- [ ] **Step 4: Run the nation-block and runs tests**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_stockpiles.LocateNationBlockTests test_stockpiles.ContiguousRunsTests -v`
Expected: PASS, 15 tests OK. (`python -m unittest` as a whole will still fail — `clop_monitor.py` imports names this rewrite has not restored yet. Task 8 closes that.)

- [ ] **Step 5: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add stockpiles.py test_stockpiles.py
git commit -m "feat: locate the nation tab's STOCK block by lookup instead of fixed rows"
```

---

### Task 7: `stockpiles.py` — locate the Dashboard block

**Files:**
- Modify: `D:\Koan\clop\automation-poc\stockpiles.py` (append)
- Test: `D:\Koan\clop\automation-poc\test_stockpiles.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `test_stockpiles.py`, extending its `from stockpiles import (...)` block so it reads:

```python
from stockpiles import (
    DASHBOARD_LABELS,
    DASHBOARD_TAB,
    DashboardBlock,
    NationBlock,
    contiguous_runs,
    locate_dashboard_block,
    locate_nation_block,
)
```


```python
GOODS_LABELS = [
    "Energy", "Apples", "Coffee", "Oil", "Gas", "Gems", "Cider", "Pies", "Toys",
    "Tungsten", "Plastics",
    "",  # spacer, row 21
    "Drugs", "Copper", "M Parts", "V Parts", "P Parts", "Composites",
    "",  # spacer, row 28
    "Forbidden Research", "Apotheosis Serum",
    "DNA - Burro - Central", "DNA - Burro - North", "DNA - Burro - South",
    "DNA - Prze - Central", "DNA - Prze - North", "DNA - Prze - South",
    "DNA - Saddle - Central", "DNA - Saddle - North", "DNA - Saddle - South",
    "DNA - Zebrica - Central", "DNA - Zebrica - North", "DNA - Zebrica - South",
]

NATIONS = ["READ ONLY", "TOTAL", "LePone(Z)", "quaity(P)", "Pure Apple Acres(B)", "#N/A"]


def dashboard_grid(nations=None, labels=None, offset=0):
    """The Dashboard as read by DASHBOARD_SCAN_RANGE: row 1 nations, column A labels.

    ``offset`` inserts that many blank rows at the top, moving the whole block down.
    """
    nations = NATIONS if nations is None else nations
    labels = GOODS_LABELS if labels is None else labels
    grid = [list(nations)]
    grid.extend([[""] for _ in range(offset)])
    grid.append(["Active"])
    grid.append(["Sat"])
    grid.append(["NLR"])
    grid.append(["SE"])
    grid.append([""])          # spacer row 6
    grid.append(["GDP"])
    grid.append(["Bits"])
    grid.append([""])          # spacer row 9
    grid.extend([[label] for label in labels])
    return grid


class LocateDashboardBlockTests(unittest.TestCase):
    def test_todays_layout(self):
        block, problems = locate_dashboard_block(dashboard_grid(), "LePone(Z)")
        self.assertEqual(problems, [])
        self.assertEqual(block.column, "C")
        self.assertEqual(block.rows["Active"], 2)
        self.assertEqual(block.rows["Bits"], 8)
        self.assertEqual(block.rows["Energy"], 10)
        self.assertEqual(block.rows["Plastics"], 20)
        self.assertEqual(block.rows["Drugs"], 22)
        self.assertEqual(block.rows["Composites"], 27)
        self.assertEqual(block.rows["Forbidden Research"], 29)
        self.assertEqual(block.rows["DNA - Zebrica - South"], 42)

    def test_all_thirty_seven_labels_located(self):
        block, _ = locate_dashboard_block(dashboard_grid(), "LePone(Z)")
        self.assertEqual(len(block.rows), 37)
        self.assertEqual(set(block.rows), set(DASHBOARD_LABELS))

    def test_another_nations_column(self):
        block, _ = locate_dashboard_block(dashboard_grid(), "quaity(P)")
        self.assertEqual(block.column, "D")

    def test_inserted_row_shifts_every_row(self):
        block, problems = locate_dashboard_block(dashboard_grid(offset=3), "LePone(Z)")
        self.assertEqual(problems, [])
        self.assertEqual(block.rows["Active"], 5)
        self.assertEqual(block.rows["Energy"], 13)

    def test_nation_not_found_names_row_one(self):
        block, problems = locate_dashboard_block(dashboard_grid(), "Nowhere(X)")
        self.assertIsNone(block)
        self.assertEqual(len(problems), 1)
        self.assertIn("Nowhere(X)", problems[0])
        self.assertIn("LePone(Z)", problems[0])   # row 1's contents are shown

    def test_na_spare_never_matches(self):
        block, problems = locate_dashboard_block(dashboard_grid(), "#N/A")
        # It *would* match by text, so this pins that a nation named "#N/A" is not a thing we
        # protect against -- what matters is that a real nation name never resolves to it.
        self.assertIsNotNone(block)
        block, _ = locate_dashboard_block(dashboard_grid(), "LePone(Z)")
        self.assertEqual(block.column, "C")

    def test_missing_label(self):
        labels = list(GOODS_LABELS)
        labels[labels.index("Toys")] = ""
        block, problems = locate_dashboard_block(dashboard_grid(labels=labels), "LePone(Z)")
        self.assertIsNone(block)
        self.assertEqual(len(problems), 1)
        self.assertIn("Toys", problems[0])

    def test_duplicate_label(self):
        labels = list(GOODS_LABELS)
        labels[labels.index("Cider")] = "Gems"
        block, problems = locate_dashboard_block(dashboard_grid(labels=labels), "LePone(Z)")
        self.assertIsNone(block)
        self.assertTrue(any("Gems" in problem for problem in problems))
        self.assertTrue(any("Cider" in problem for problem in problems))

    def test_spacer_rows_are_not_labels(self):
        block, _ = locate_dashboard_block(dashboard_grid(), "LePone(Z)")
        self.assertNotIn(6, set(block.rows.values()))
        self.assertNotIn(9, set(block.rows.values()))
        self.assertNotIn(21, set(block.rows.values()))
        self.assertNotIn(28, set(block.rows.values()))

    def test_runs_from_todays_layout_skip_the_spacers(self):
        block, _ = locate_dashboard_block(dashboard_grid(), "LePone(Z)")
        spans = [(run[0][0], run[-1][0]) for run in contiguous_runs(block.rows)]
        self.assertEqual(spans, [(2, 5), (7, 8), (10, 20), (22, 27), (29, 42)])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_stockpiles.LocateDashboardBlockTests -v`
Expected: FAIL — `ImportError: cannot import name 'DashboardBlock' from 'stockpiles'`

- [ ] **Step 3: Write the implementation**

Append to `stockpiles.py`:

```python
@dataclass(frozen=True)
class DashboardBlock:
    """Where the nation's column and the labelled rows turned out to be on the Dashboard."""

    column: str                 # A1 letter of this nation's column
    rows: Dict[str, int]        # dashboard label -> 1-based row


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_stockpiles.LocateDashboardBlockTests -v`
Expected: PASS, 10 tests OK

- [ ] **Step 5: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add stockpiles.py test_stockpiles.py
git commit -m "feat: locate the Dashboard column and rows by lookup"
```

---

### Task 8: `stockpiles.py` — write both regions

**Files:**
- Modify: `D:\Koan\clop\automation-poc\stockpiles.py` (append)
- Test: `D:\Koan\clop\automation-poc\test_stockpiles.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `test_stockpiles.py`, extending its imports:

```python
from goods import Stockpiles
from nation import NationStatus, Reading
from stockpiles import STATUS_LABELS, Report, as_sheet_text, snapshot, status_values
```

```python
STATUS = NationStatus(
    government="Loose Despotism",
    economy="Poorly Defined",
    satisfaction=Reading(218, -5),
    se=Reading(-120, 3),
    nlr=Reading(1500, "Ascending"),
    gdp=60900,
    funds=1234567,
    server_time="2026-08-23 11:42:12",
)

STOCK = Stockpiles({"Apples": 1226, "Oil": 80, "Gems": 6, "Toys": 4})


class FakeSheet:
    """Stand-in for GoogleSheet: serves a canned grid per tab and records every write."""

    def __init__(self, nation_tab="LePone(Z)", nation=None, dashboard=None):
        self.grids = {
            nation_tab: nation if nation is not None else nation_grid(),
            DASHBOARD_TAB: dashboard if dashboard is not None else dashboard_grid(),
        }
        self.blocks = []   # (tab, a1, values) from write()
        self.cells = []    # (tab, a1, value) from write_cell()

    def read(self, tab, a1):
        return self.grids[tab]

    def write(self, tab, a1, values):
        self.blocks.append((tab, a1, values))
        return values

    def write_cell(self, tab, a1, value):
        self.cells.append((tab, a1, value))
        return value


class StatusValuesTests(unittest.TestCase):
    def test_every_status_label_covered(self):
        self.assertEqual(set(status_values(STATUS)), set(STATUS_LABELS))

    def test_active_is_the_server_time_forced_to_text(self):
        self.assertEqual(status_values(STATUS)["Active"], as_sheet_text("2026-08-23 11:42:12"))

    def test_readings_rendered_with_the_per_tick_in_parentheses(self):
        values = status_values(STATUS)
        self.assertEqual(values["Sat"], "218 (-5)")
        self.assertEqual(values["SE"], "-120 (3)")
        self.assertEqual(values["NLR"], "1500 (Ascending)")

    def test_gdp_and_bits_are_numbers_so_TOTAL_can_sum_them(self):
        values = status_values(STATUS)
        self.assertEqual(values["GDP"], 60900)
        self.assertEqual(values["Bits"], 1234567)
        self.assertIsInstance(values["GDP"], int)
        self.assertIsInstance(values["Bits"], int)


class SnapshotTests(unittest.TestCase):
    def run_snapshot(self, sheet):
        return snapshot(sheet, "LePone(Z)", STOCK, STATUS)

    def test_no_problems_on_todays_layout(self):
        sheet = FakeSheet()
        _report, problems = self.run_snapshot(sheet)
        self.assertEqual(problems, [])

    def test_nation_tab_values_in_sheet_row_order(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        nation_blocks = [b for b in sheet.blocks if b[0] == "LePone(Z)"]
        self.assertEqual(len(nation_blocks), 1)
        _tab, a1, values = nation_blocks[0]
        self.assertEqual(a1, "R11:R16")
        # apple, oil, coffee, mpart, vpart, gems -- the sheet's order, not the table's
        self.assertEqual(values, [[1226], [80], [0], [0], [0], [6]])

    def test_timestamp_written_after_the_values(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        self.assertEqual(sheet.cells, [("LePone(Z)", "W10", as_sheet_text(STATUS.server_time))])

    def test_dashboard_written_as_five_runs(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        ranges = [b[1] for b in sheet.blocks if b[0] == DASHBOARD_TAB]
        self.assertEqual(ranges, ["C2:C5", "C7:C8", "C10:C20", "C22:C27", "C29:C42"])

    def test_dashboard_status_run_payload(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        payload = dict((b[1], b[2]) for b in sheet.blocks if b[0] == DASHBOARD_TAB)
        self.assertEqual(
            payload["C2:C5"],
            [[as_sheet_text("2026-08-23 11:42:12")], ["218 (-5)"], ["1500 (Ascending)"], ["-120 (3)"]],
        )
        self.assertEqual(payload["C7:C8"], [[60900], [1234567]])

    def test_dashboard_goods_run_payload(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        payload = dict((b[1], b[2]) for b in sheet.blocks if b[0] == DASHBOARD_TAB)
        # Energy, Apples, Coffee, Oil, Gas, Gems, Cider, Pies, Toys, Tungsten, Plastics
        self.assertEqual(
            payload["C10:C20"],
            [[0], [1226], [0], [80], [0], [6], [0], [0], [4], [0], [0]],
        )

    def test_absent_good_written_as_zero(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        payload = dict((b[1], b[2]) for b in sheet.blocks if b[0] == DASHBOARD_TAB)
        self.assertEqual(payload["C29:C42"], [[0]] * 14)

    def test_spacer_rows_never_written(self):
        sheet = FakeSheet()
        self.run_snapshot(sheet)
        touched = set()
        for _tab, a1, _values in sheet.blocks:
            first, last = a1.split(":")
            start = int(first[1:]) if first[1].isdigit() else int(first[2:])
            end = int(last[1:]) if last[1].isdigit() else int(last[2:])
            touched.update(range(start, end + 1))
        for spacer in (6, 9, 21, 28):
            self.assertNotIn(spacer, touched)

    def test_dashboard_problem_does_not_stop_the_nation_tab(self):
        sheet = FakeSheet(dashboard=dashboard_grid(nations=["READ ONLY", "TOTAL"]))
        _report, problems = self.run_snapshot(sheet)
        self.assertTrue(problems)
        self.assertTrue(any(b[0] == "LePone(Z)" for b in sheet.blocks))
        self.assertFalse(any(b[0] == DASHBOARD_TAB for b in sheet.blocks))

    def test_nation_problem_does_not_stop_the_dashboard(self):
        sheet = FakeSheet(nation=nation_grid(labels=["apple", "oil"]))
        _report, problems = self.run_snapshot(sheet)
        self.assertTrue(problems)
        self.assertFalse(any(b[0] == "LePone(Z)" for b in sheet.blocks))
        self.assertTrue(any(b[0] == DASHBOARD_TAB for b in sheet.blocks))

    def test_nation_problem_withholds_the_timestamp_too(self):
        # A fresh stamp over stale numbers is worse than an obviously stale one.
        sheet = FakeSheet(nation=nation_grid(labels=["apple", "oil"]))
        self.run_snapshot(sheet)
        self.assertEqual(sheet.cells, [])

    def test_junk_in_the_timestamp_cell_blocks_the_whole_nation_tab(self):
        sheet = FakeSheet(nation=nation_grid(stamp="NEED BY"))
        _report, problems = self.run_snapshot(sheet)
        self.assertTrue(any("W10" in problem for problem in problems))
        self.assertFalse(any(b[0] == "LePone(Z)" for b in sheet.blocks))
        self.assertEqual(sheet.cells, [])

    def test_empty_timestamp_cell_is_fine(self):
        sheet = FakeSheet(nation=nation_grid(stamp=""))
        _report, problems = self.run_snapshot(sheet)
        self.assertEqual(problems, [])
        self.assertEqual(len(sheet.cells), 1)

    def test_report_records_what_was_written(self):
        sheet = FakeSheet()
        report, _problems = self.run_snapshot(sheet)
        self.assertEqual(report.timestamp, "2026-08-23 11:42:12")
        self.assertEqual(len(report.nation_writes), 1)
        self.assertEqual(len(report.dashboard_writes), 5)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_stockpiles.SnapshotTests -v`
Expected: FAIL — `ImportError: cannot import name 'Report' from 'stockpiles'`

- [ ] **Step 3: Write the implementation**

Append to `stockpiles.py`:

```python
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
        # Column W is 6 columns right of Q, and the grid starts at Q.
        current_stamp = _cell(nation_grid, block.header_row - 1, 6)
        stamp_problem = timestamp_problem(block.timestamp_cell, current_stamp)
        if stamp_problem:
            problems.append(stamp_problem)
        else:
            values = {
                good.stock_label: stock.get(good.game_name)
                for good in GOODS
                if good.stock_label
            }
            nation_writes = _write_runs(
                sheet, nation, block.value_column, block.rows, values
            )
            # After the values, never before: it can then never claim freshness for a failed write.
            sheet.write_cell(nation, block.timestamp_cell, as_sheet_text(status.server_time))
            stamped = status.server_time

    dashboard_grid = sheet.read(DASHBOARD_TAB, DASHBOARD_SCAN_RANGE)
    dashboard, dashboard_problems = locate_dashboard_block(dashboard_grid, nation)
    problems.extend(dashboard_problems)
    if dashboard is not None:
        values = status_values(status)
        for good in GOODS:
            values[good.dashboard_label] = stock.get(good.game_name)
        dashboard_writes = _write_runs(
            sheet, DASHBOARD_TAB, dashboard.column, dashboard.rows, values
        )

    return Report(nation_writes, dashboard_writes, stamped), problems
```

- [ ] **Step 4: Run the stockpiles tests**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest test_stockpiles -v`
Expected: PASS, 44 tests OK. (`python -m unittest` as a whole still fails until Task 10 rewires `clop_monitor.py`.)

- [ ] **Step 5: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add stockpiles.py test_stockpiles.py
git commit -m "feat: snapshot both the nation tab and the Dashboard from one fetch"
```

---

### Task 9: `stockpiles.py` — the standalone diagnostic

**Files:**
- Modify: `D:\Koan\clop\automation-poc\stockpiles.py` (append)

This is the script the monitor's own popups tell people to run, so its failures must arrive as
dialogs and it must never write.

- [ ] **Step 1: Write the implementation**

Append to `stockpiles.py`:

```python
def _standalone() -> int:
    """Login, read overview, and report what a real run would write. Writes nothing."""
    import os
    import sys

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
        print(f"  {STOCK_HEADER} header row  {block.header_row}")
        print(f"  {HAVE_HEADER} column      {block.value_column}")
        print(f"  timestamp cell     {block.timestamp_cell} = "
              f"{_cell(nation_grid, block.header_row - 1, 6)!r}")
        print(f"\n  {'row':<6}{'label':<10}{'game resource':<20}{'overview':>12}{'sheet':>12}")
        column_offset = ord(block.value_column[-1]) - ord("Q")
        for stock_label, row in sorted(block.rows.items(), key=lambda item: item[1]):
            good = BY_STOCK_LABEL[stock_label]
            stored = _cell(nation_grid, row - 1, column_offset)
            print(f"  {block.value_column + str(row):<6}{stock_label:<10}"
                  f"{good.game_name:<20}{stock.get(good.game_name):>12}"
                  f"{'' if stored is None else str(stored):>12}")

    print(f"\n--- {DASHBOARD_TAB} tab ---")
    if dashboard is None:
        print("  could not be located; see the problems below.")
    else:
        print(f"  column for {nation!r}: {dashboard.column}")
        values = status_values(status)
        for good in GOODS:
            values[good.dashboard_label] = stock.get(good.game_name)
        print(f"\n  {'cell':<8}{'label':<26}{'would write':>26}")
        for label, row in sorted(dashboard.rows.items(), key=lambda item: item[1]):
            cell = f"{dashboard.column}{row}"
            print(f"  {cell:<8}{label:<26}{str(values[label]):>26}")

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
```

- [ ] **Step 2: Verify the module imports cleanly**

Run: `cd /d/Koan/clop/automation-poc && python -c "import stockpiles; print(sorted(stockpiles.DASHBOARD_LABELS)[:3])"`
Expected: `['Active', 'Apotheosis Serum', 'Apples']`

- [ ] **Step 3: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add stockpiles.py
git commit -m "feat: report both sheet regions from the standalone stock check"
```

---

### Task 10: `clop_monitor.py` — wiring

**Files:**
- Modify: `D:\Koan\clop\automation-poc\clop_monitor.py:1782-1867`

- [ ] **Step 1: Update the docstring and imports**

In `sync_sheet_step`, replace the numbered list in the docstring (currently lines 1787-1793) with:

```
    Three syncs off one page fetch, in this order:

    1. **Buildings** -- reconcile the have/disabled counts and pop up any corrections made.
    2. **Stockpiles** -- snapshot the nation tab's six goods and stamp its timestamp.
    3. **Dashboard** -- write the nation's own column on the alliance-wide tab: all 31 goods and
       the six status rows.

    Steps 2 and 3 are one call into ``stockpiles.snapshot``, off one parse. They guard different
    regions of the sheet and are independent of each other and of the buildings step: one being
    skipped for a layout problem does not skip the others.
```

Replace the `from stockpiles import (...)` block (lines 1810-1816) with:

```python
    from stockpiles import (
        NationStatus,
        NationStatusError,
        StockpileError,
        Stockpiles,
        snapshot,
    )
```

- [ ] **Step 2: Replace the parse and the snapshot call**

Replace lines 1830-1831:

```python
        server_time = parse_server_time(overview_html)
        resources = parse_overview_resources(overview_html)
```

with:

```python
        # Parsed once, here: both sheet regions are written from these two objects, and nothing
        # downstream re-fetches or re-parses the page.
        stock = Stockpiles.from_overview(overview_html)
        status = NationStatus.from_overview(overview_html)
```

Replace the stockpile block (lines 1851-1863) with:

```python
        # The snapshot is a scheduled refresh rather than an event, so a successful write is
        # deliberately silent -- at a 60s poll a popup for it would never stop firing.
        phase = "the stockpile snapshot"
        _report, stock_problems = snapshot(sheet, nation, stock, status)
        if stock_problems:
            notifier.notify_failure(
                "Stockpile snapshot: part of the sheet was not written because its layout is not "
                "what the script expects. The affected region is untouched and its timestamp will "
                "now go stale. Run 'python stockpiles.py' to recheck once the sheet is fixed.\n\n"
                + "\n".join(f"- {problem}" for problem in stock_problems)
            )
```

- [ ] **Step 3: Add `NationStatusError` to the caught exceptions**

Replace line 1864:

```python
    except (MonitorError, OverviewError, SheetError, BuildingError, StockpileError) as error:
```

with:

```python
    except (
        MonitorError,
        OverviewError,
        SheetError,
        BuildingError,
        StockpileError,
        NationStatusError,
    ) as error:
```

- [ ] **Step 4: Run the whole suite**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest 2>&1 | tail -5`
Expected: `OK`, 0 failures and 0 errors. If `test_clop_monitor.NoTerminalOnlyFailuresTests` fails, a
new failure path prints without a dialog behind it — every new failure must go through
`notifier.notify_failure` or `popup_failure`.

- [ ] **Step 5: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add clop_monitor.py
git commit -m "feat: sync the Dashboard alongside the nation tab in one step"
```

---

### Task 11: Documentation

**Files:**
- Modify: `D:\Koan\clop\automation-poc\docs\2026-08-23-dashboard-goods-map.md`
- Modify: `D:\Koan\clop\automation-poc\README.md`

- [ ] **Step 1: Extend the goods map with the status rows**

In `docs/2026-08-23-dashboard-goods-map.md`, in the "What the tab looks like" section, replace the
sentence beginning "Rows 2–8 are nation status" with:

```markdown
Rows 2–8 are nation status, and `stockpiles.py` writes them from the overview "Nation" panel:

| Cell | Label | Source on overview.php | Written as |
|---|---|---|---|
| A2 | `Active` | the page header's `Server time:` stamp | text, e.g. `2026-08-23 11:42:12` |
| A3 | `Sat` | `Satisfaction` | `218 (-5)` — current, per tick in parentheses |
| A4 | `NLR` | `Relationship with New Lunar Republic` | `1500 (Ascending)` |
| A5 | `SE` | `Relationship with Solar Empire` | `-120 (3)` |
| A7 | `GDP` | `GDP` | a number, so column B's `TOTAL` sums it |
| A8 | `Bits` | `Funds` | a number |

`Active` holds a last-updated timestamp despite its label. The per-tick figure is the literal string
the game printed when it is not a number: `Ascending` under Alicorn Elite and Transponyism, `Fixed`
under Solar Vassal and Lunar Client.

The goods block is `A10:A42`. Rows 6, 9, 21 and 28 are blank spacers, and nothing follows row 42.
```

(Delete the old standalone sentence about the goods block and the spacers, so it is not stated twice.)

- [ ] **Step 2: Record that the script writes this tab**

Add a section to the same file, immediately before "## Relationship to the other two orderings":

```markdown
## How the script writes this tab

`stockpiles.py` writes the nation's own column on every sync, off the same `overview.php` fetch that
updates the nation tab. Nothing is hardcoded: the column comes from matching `CLOP_NATION` against
row 1, and each row comes from looking its label up in column A. The located rows are grouped into
contiguous runs and written one block each — five writes on today's layout (`2:5`, `7:8`, `10:20`,
`22:27`, `29:42`) — so the spacer rows are never touched and an inserted row simply shifts the
answer.

If the nation's name is not in row 1, or a label is missing or duplicated in column A, **nothing on
this tab is written** and a blocking dialog names every problem. The nation tab still updates.

Writes are unconditional rather than diffed: an unreadable cell normalises to `0`, so it would
compare equal to a good the nation holds none of and the garbage would survive.
```

- [ ] **Step 3: Update the README**

In `README.md`, replace the bullet describing the stockpile snapshot with:

```markdown
- snapshots your stockpiles onto the shared sheet from the same page read: the six goods in your own
  nation tab's `STOCK` block, and your whole column on the alliance-wide `Dashboard` tab — all 31
  goods plus satisfaction, both faction relationships, GDP, funds and a last-updated stamp;
```

- [ ] **Step 4: Refresh the test count**

Run: `cd /d/Koan/clop/automation-poc && python -m unittest 2>&1 | tail -3`

Take the number from `Ran N tests` and update the sentence in `README.md` that currently reads
"All **398** of them", along with the file list beside it — it names `test_sheets.py`,
`test_overview.py`, `test_buildings.py` and `test_stockpiles.py`; add `test_goods.py` and
`test_nation.py`.

- [ ] **Step 5: Commit**

```bash
cd /d/Koan/clop/automation-poc
git add README.md docs/2026-08-23-dashboard-goods-map.md
git commit -m "docs: document the Dashboard sync and its status rows"
```

---

### Task 12: Verify against the live sheet

**Files:** none — this is a run, not an edit.

The suite is entirely offline. Nothing has yet proved the lookups match the real sheet.

- [ ] **Step 1: Run the read-only diagnostic**

Run: `cd /d/Koan/clop/automation-poc && python stockpiles.py`

Expected: the nation tab resolves to `STOCK` header row `10`, `HAVE` column `R`, timestamp cell
`W10`; the Dashboard resolves to column `C` for `LePone(Z)`; all 37 Dashboard labels list with the
values a real run would write; no problems; exit code 0.

- [ ] **Step 2: Report the output**

Paste the diagnostic's output back to the user before any live write happens. If any problem is
listed, stop and report it — do not "fix" the shared sheet.

- [ ] **Step 3: Commit nothing**

This task produces no changes.

---

## Self-Review Notes

Checked against `docs/superpowers/specs/2026-08-23-dashboard-sync-design.md`:

- Spec §`goods.py` → Tasks 1–2. §`nation.py` → Task 4. §`overview.py` → Task 3. §`sheets.py` →
  Task 5. §"Nation tab" → Task 6. §"Dashboard tab" → Task 7. §"Writing" and §"Failure handling" →
  Task 8. §"Monitor wiring" → Task 10. §"Standalone diagnostic" → Task 9. §"Testing" → spread
  across the tasks that introduce each behaviour. §"Documentation to update" → Task 11.
- Constraint 4 (the `COST` block in column Q) is covered by
  `test_cost_block_below_is_not_picked_up`. Constraint 6 (the `commas()` corruption) by
  `test_corrupted_gdp_rejected`. Constraint 5 (per-tick spans) by the `ParsePanelTextTests`.
- Names are consistent across tasks: `Stockpiles.get`, `NationStatus.from_overview`,
  `Reading.display`, `locate_nation_block`, `locate_dashboard_block`, `contiguous_runs`,
  `status_values`, `snapshot`, `Report`, `timestamp_problem`, `column_letter`, `find_in_row`,
  `index_column`.
- Task 12 was added because every other task is offline; without it nothing checks the lookups
  against the real sheet before the monitor starts writing to a tab nine people read.
