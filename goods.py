#!/usr/bin/env python3
"""The game's 31 goods, and a parse-once handle on how much of each a nation holds.

Every good carries every name it is known by at once -- the game's, the ``Dashboard-Stockpile``
tab's two blocks, and (for six of them) the nation tab's ``STOCK`` block -- so there is one table to
keep right instead of four lists to keep in step:

    Good("Machinery Parts", 10, "M Parts", stock_label="mpart", tick_label="M Part - tick")

``game_name`` is ``resourcedefs.name`` exactly as ``overview.php`` renders it; the table is the 31
rows of ``resourcedefs`` with ``is_building = 0``, taken from the game's seed data
(``clop/tables with data.sql``). ``docs/2026-08-23-dashboard-goods-map.md`` is the same table with
its provenance written out.

``GOODS`` carries **no ordering obligation**. Both sheet regions are located by looking their labels
up, so the order values are written in comes from the rows the lookup found. The order here happens
to match the sheet's for readability, and nothing may come to depend on that.

This module knows the game's vocabulary and nothing about the sheet. See ``stockpiles.py`` for the
writing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Union

from overview import parse_panel_cells

#: A Ticks-Worth reading: a whole number of ticks, or one of the game's words (see TICKS_WORDS).
TickCount = Union[int, str]


@dataclass(frozen=True)
class Good:
    """One tradeable/holdable resource, under each of the names it goes by."""

    game_name: str                  # resourcedefs.name, as overview.php renders it
    resource_id: int                # for cross-referencing the game's seed data
    dashboard_label: str            # Dashboard-Stockpile tab, column A, the quantity block
    stock_label: Optional[str] = None   # nation tab, column Q; None for the 25 not in that block
    tick_label: Optional[str] = None    # Dashboard-Stockpile "X - tick" block; same six goods


#: All 31 goods. Order matches the Dashboard's rows for readability only -- see the module docstring.
GOODS: Tuple[Good, ...] = (
    Good("Energy", 4, "Energy"),
    Good("Apples", 3, "Apples", "apple", "Apple - tick"),
    Good("Coffee", 20, "Coffee", "coffee", "Coffee - tick"),
    Good("Oil", 1, "Oil", "oil", "Oil - tick"),
    Good("Gasoline", 25, "Gas"),
    Good("Gems", 26, "Gems", "gems", "Gems - tick"),
    Good("Cider", 18, "Cider"),
    Good("Pies", 13, "Pies"),
    Good("Toys", 47, "Toys"),
    Good("Tungsten", 27, "Tungsten"),
    Good("Plastics", 28, "Plastics"),
    Good("Drugs", 42, "Drugs"),
    Good("Copper", 2, "Copper"),
    Good("Machinery Parts", 10, "M Parts", "mpart", "M Part - tick"),
    Good("Vehicle Parts", 9, "V Parts", "vpart", "V Part - tick"),
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
BY_TICK_LABEL: Dict[str, Good] = {good.tick_label: good for good in GOODS if good.tick_label}


#: The Resources panel's header cell naming its rightmost column.
TICKS_HEADING = "Ticks-Worth"

#: The header row's own name cell. It arrives through the parser like any other row, which is what
#: lets the Ticks-Worth column be found by heading rather than counted to.
RESOURCE_HEADING = "Resource"

#: What the game prints in the Ticks-Worth column when the number is not a number. ``N/A`` means the
#: net is zero or positive, so the stock never runs out; ``NONE`` means there is already less than
#: one tick's requirement left. Both are passed through verbatim rather than turned into a number.
TICKS_WORDS = ("N/A", "NONE")


class StockpileError(RuntimeError):
    """A quantity on overview.php that cannot be read as a whole number.

    Raised rather than skipping the row. Skipping would let the good fall through to zero and be
    written to the sheet as "you hold none", stamped freshly verified -- indistinguishable from the
    player actually having spent the lot. The game formats every Qty with its integer ``commas()``
    helper, so anything else means the page changed and a human should look.
    """


def _parse_resources_panel(html: str) -> "Tuple[Dict[str, int], Optional[Dict[str, TickCount]]]":
    """Return ``({game_name: qty}, {game_name: ticks} or None)`` in one pass over the panel.

    One pass, because the quantity and the ticks-worth are two columns of the same row and the
    caller has already paid for the page once.

    The ticks map is ``None`` -- not empty -- when the panel has no ``Ticks-Worth`` column at all.
    That is a different thing from a nation with no rows, and the caller reports it as a problem
    rather than writing zeros over a column it could not read.
    """
    amounts: Dict[str, int] = {}
    ticks: Dict[str, TickCount] = {}
    ticks_at: Optional[int] = None

    for name, cells in parse_panel_cells(html, "Resources"):
        if name == RESOURCE_HEADING:
            # The <thead> row. Find the column by its heading; never count to a fixed position.
            for index, heading in enumerate(cells):
                if heading.strip() == TICKS_HEADING:
                    ticks_at = index
            continue

        if not cells:
            continue
        text = cells[0].replace(",", "").strip()
        if not re.fullmatch(r"-?\d+", text):
            raise StockpileError(
                f"resource {name!r} has an unreadable quantity {cells[0]!r} on overview.php"
            )
        amounts[name] = int(text)

        if ticks_at is not None and ticks_at < len(cells):
            ticks[name] = _tick_count(name, cells[ticks_at])

    return amounts, (ticks if ticks_at is not None else None)


def _tick_count(name: str, text: str) -> "TickCount":
    """Read one Ticks-Worth cell: a whole number, or one of the game's words verbatim."""
    cleaned = text.replace(",", "").strip()
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    if cleaned.upper() in TICKS_WORDS:
        return cleaned.upper()
    raise StockpileError(
        f"resource {name!r} has an unreadable {TICKS_HEADING} value {text!r} on overview.php"
    )


def parse_overview_resources(html: str) -> Dict[str, int]:
    """Return ``{game_name: qty}`` from the overview "Resources" panel.

    Only resources the nation has a row for are rendered, so a good it holds none of is absent from
    the result rather than present as zero; ``Stockpiles.get`` is what turns that into a zero.
    """
    return _parse_resources_panel(html)[0]


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

    The same object also carries the panel's **Ticks-Worth** column -- how many ticks the current
    stock lasts at the current net rate::

        stock.ticks("Apples")   # 13, or "NONE", or "N/A"

    ``ticks_worth`` is ``None`` when the panel had no such column, which the caller must treat as
    "could not read it" rather than as zeros.
    """

    def __init__(
        self,
        amounts: Dict[str, int],
        ticks: "Optional[Dict[str, TickCount]]" = None,
    ) -> None:
        self._amounts = dict(amounts)
        self._ticks = None if ticks is None else dict(ticks)

    @classmethod
    def from_overview(cls, html: str) -> "Stockpiles":
        """Parse the Resources panel. Raises ``StockpileError`` on an unreadable value."""
        amounts, ticks = _parse_resources_panel(html)
        return cls(amounts, ticks)

    def get(self, game_name: str, default: int = 0) -> int:
        return self._amounts.get(game_name, default)

    @property
    def ticks_worth(self) -> "Optional[Dict[str, TickCount]]":
        """``{game_name: ticks}``, or ``None`` if the page had no Ticks-Worth column."""
        return None if self._ticks is None else dict(self._ticks)

    def ticks(self, game_name: str) -> "TickCount":
        """How many ticks of ``game_name`` the nation has left, as the game reports it.

        A good absent from the page reads as ``"N/A"``, which is what the game itself would print
        for it: holding none of something nothing consumes means the stock never runs out.
        """
        if self._ticks is None:
            raise StockpileError(
                f"overview.php had no {TICKS_HEADING} column, so ticks for {game_name!r} are unknown"
            )
        return self._ticks.get(game_name, "N/A")

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
