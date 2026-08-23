#!/usr/bin/env python3
"""Mapping between the game's building names and the shared sheet's building rows.

The game names on the left are exactly ``resourcedefs.name`` for every row with ``is_building = 1``
(the query in the game's ``backend/backend_overview.php`` that renders the overview "Buildings"
panel). They were taken from the game's seed data, ``clop/tables with data.sql`` -> ``resourcedefs``.

The sheet names on the right are the labels in column A of a nation's tab. The mapping is mostly 1:1
with renames, plus two deliberate many-to-one groups (confirmed with the sheet owner):

* every regional ``DNA Extraction Facility - <region>`` folds into the single ``DNA`` row. A nation
  only ever holds its own region's facility, so the sum is one value in practice.
* ``Solar Collector`` and ``Tidal Generator`` both fold into the single ``Energy Collector`` row.

`buildings.py` reconciles overview counts against the sheet through this table; `sanity_check` there
verifies it still covers every building the game reports and every row the sheet expects, so a
renamed building or a reformatted sheet is caught rather than silently mis-written.
"""

from __future__ import annotations

from typing import Dict, FrozenSet

#: Game / overview building name -> sheet column-A building name.
GAME_TO_SHEET: Dict[str, str] = {
    # Mines
    "Basic Copper Mine": "Basic Mine",
    "Mechanized Copper Mine": "Mecha Mine",
    "Gem Mine": "Gem Mine",
    "Tungsten Mine": "Tungsten Mine",
    # Orchards / farms
    "Basic Apple Orchard": "Basic Orchard",
    "Mechanized Apple Orchard": "Mecha Orchard",
    "Coffee Farm": "Coffee Farm",
    "Drug Farm": "Drug Farm",
    # Oil
    "Basic Oil Well": "Basic Oil",
    "Mechanized Oil Well": "Mech Oil",
    "Oil Fracker": "Fracker",
    # Energy / gas / plastics / processing
    "Cider Production Facility": "Cider Facility",
    "Solar Collector": "Energy Collector",
    "Tidal Generator": "Energy Collector",
    "Oil Combustion Facility": "Oil Combustion",
    "Gasoline Combustion Facility": "Gas Combustion",
    "Gasoline Refinery": "Gas Refinery",
    "Plastics Factory": "Plastics Factory",
    "Przewalskian Plastics Factory": "Prze Adv Plastic",
    "Toy Factory": "Toy Factory",
    # Satisfaction buildings
    "Bakery": "Bakery",
    "Bar": "Bar",
    "Coffee Shop": "Coffee Shop",
    "Statue": "Statue",
    "Toy and Candy Shop": "Toy & Candy",
    "Video Arcade": "Video Arcade",
    "Mall": "Mall",
    # Factories / DNA / military
    "Basic Factory": "Basic Factory",
    "Advanced Factory": "Adv Factory",
    "Barracks": "Barracks",
    "DNA Extraction Facility - N. SA": "DNA",
    "DNA Extraction Facility - C. SA": "DNA",
    "DNA Extraction Facility - S. SA": "DNA",
    "DNA Extraction Facility - N. Zebrica": "DNA",
    "DNA Extraction Facility - C. Zebrica": "DNA",
    "DNA Extraction Facility - S. Zebrica": "DNA",
    "DNA Extraction Facility - N. Burrozil": "DNA",
    "DNA Extraction Facility - C. Burrozil": "DNA",
    "DNA Extraction Facility - S. Burrozil": "DNA",
    "DNA Extraction Facility - N. Prze": "DNA",
    "DNA Extraction Facility - C. Prze": "DNA",
    "DNA Extraction Facility - S. Prze": "DNA",
    # Worship / environmental / endgame
    "Moon Worship Center": "Moon Worship",
    "Sun Worship Center": "Sun Worship",
    "Lunar Environmental Facility": "Lunar Enviro",
    "Solar Environmental Facility": "Solar Enviro",
    "Forbidden Research Facility": "Forbidden Research",
    "Alicornification Facility": "Alicornification",
}

#: Every distinct sheet building row the mapping targets (36 of them).
SHEET_BUILDINGS: FrozenSet[str] = frozenset(GAME_TO_SHEET.values())
