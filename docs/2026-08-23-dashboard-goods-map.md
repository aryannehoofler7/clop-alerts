# Dashboard goods map (`Dashboard!A10:A42` → game resource)

Reference for anything that reads or writes the shared sheet's **`Dashboard`** tab by row position.
Row order there is data, not decoration — the same rule `stockpiles.py` follows for `Q11:Q16` — so
this table is what a label check would be written against.

## What the tab looks like

`Dashboard!A1` reads `READ ONLY`: the grid is formula-driven and pulls from the per-nation tabs.
Row 1 is the header — `A` = row label, `B` = `TOTAL`, and `C` onward one column per nation tab
(`LePone(Z)`, `quaity(P)`, `Pure Apple Acres(B)`, `Republic(B)`, `Solarium(Z)`, `GLA(S)`,
`Fish Bucket(S)`, `Vladihoofstock(Z)`, `Buenos Mares(B)`, and an `#N/A` spare in `L`).

Rows 2–8 are nation status, and `stockpiles.py` writes them from the overview "Nation" panel:

| Cell | Label | Source on overview.php | Written as |
|---|---|---|---|
| A2 | `Active` | the page header's `Server time:` stamp | text, e.g. `2026-08-23 12:09:44` |
| A3 | `Sat` | `Satisfaction` | `-61 (-2)` — current, per tick in parentheses |
| A4 | `NLR` | `Relationship with New Lunar Republic` | `1500 (Ascending)` |
| A5 | `SE` | `Relationship with Solar Empire` | `-120 (3)` |
| A7 | `GDP` | `GDP` | a number, so column B's `TOTAL` sums it |
| A8 | `Bits` | `Funds` | a number |

`Active` holds a last-updated timestamp despite its label. The per-tick figure is the literal string
the game printed when it is not a number: `Ascending` under Alicorn Elite and Transponyism, `Fixed`
under Solar Vassal and Lunar Client.

The **goods block is `A10:A42`**. Rows 6, 9, 21 and 28 are blank spacers, and nothing follows
row 42.

## The mapping

The "game name" column is `resourcedefs.name` from the game's seed data
(`clop/tables with data.sql`) — the exact string that appears in the Resources panel of
`overview.php`, which is what `stockpiles.parse_overview_resources` keys on.

| Cell | Name on sheet | Name in game (overview.php) | `resource_id` |
|---|---|---|---|
| A10 | Energy | Energy | 4 |
| A11 | Apples | Apples | 3 |
| A12 | Coffee | Coffee | 20 |
| A13 | Oil | Oil | 1 |
| A14 | Gas | Gasoline | 25 |
| A15 | Gems | Gems | 26 |
| A16 | Cider | Cider | 18 |
| A17 | Pies | Pies | 13 |
| A18 | Toys | Toys | 47 |
| A19 | Tungsten | Tungsten | 27 |
| A20 | Plastics | Plastics | 28 |
| A21 | *(blank spacer)* | — | — |
| A22 | Drugs | Drugs | 42 |
| A23 | Copper | Copper | 2 |
| A24 | M Parts | Machinery Parts | 10 |
| A25 | V Parts | Vehicle Parts | 9 |
| A26 | P Parts | Precision Parts | 29 |
| A27 | Composites | Composites | 30 |
| A28 | *(blank spacer)* | — | — |
| A29 | Forbidden Research | Forbidden Research | 75 |
| A30 | Apotheosis Serum | Apotheosis Serum | 77 |
| A31 | DNA - Burro - Central | DNA - Central Burrozil | 69 |
| A32 | DNA - Burro - North | DNA - North Burrozil | 68 |
| A33 | DNA - Burro - South | DNA - South Burrozil | 70 |
| A34 | DNA - Prze - Central | DNA - Central Przewalskia | 72 |
| A35 | DNA - Prze - North | DNA - North Przewalskia | 71 |
| A36 | DNA - Prze - South | DNA - South Przewalskia | 73 |
| A37 | DNA - Saddle - Central | DNA - Central Saddle Arabia | 63 |
| A38 | DNA - Saddle - North | DNA - North Saddle Arabia | 62 |
| A39 | DNA - Saddle - South | DNA - South Saddle Arabia | 64 |
| A40 | DNA - Zebrica - Central | DNA - Central Zebrica | 66 |
| A41 | DNA - Zebrica - North | DNA - North Zebrica | 65 |
| A42 | DNA - Zebrica - South | DNA - South Zebrica | 67 |

**The coverage is exact.** The 31 labelled rows are all 31 rows of `resourcedefs` with
`is_building = 0` — every good in the game, no omissions, no rows on the sheet that do not
correspond to a resource. So a reader can treat this block as the complete goods list.

## Notes on the four abbreviations

`Gas`, `M Parts`, `V Parts` and `P Parts` are the only labels that are not the game name verbatim,
and each has exactly one candidate in `resourcedefs`:

- `Gas` → `Gasoline` (25). The only good whose name begins "Gas"; `Gems` is separately listed at A15.
- `M Parts` → `Machinery Parts` (10) — the only `* Parts` beginning with M.
- `V Parts` → `Vehicle Parts` (9) — likewise the only one beginning with V.
- `P Parts` → `Precision Parts` (29) — the only `* Parts` beginning with P. `Pies` (A17) is a
  separate row and not a "Parts" good, so there is no clash.

The DNA rows reverse the game's word order (`DNA - <direction> <region>` becomes
`DNA - <region> - <direction>`) and abbreviate two regions: `Burro` = Burrozil, `Prze` =
Przewalskia, with `Saddle` = Saddle Arabia and `Zebrica` unabbreviated. The sheet groups them
region-first then direction, which is *not* the order `overview.php` renders them in (see below).

## How the script writes this tab

`stockpiles.py` writes the nation's own column on every sync, off the same `overview.php` fetch that
updates the nation tab. Nothing is hardcoded: the column comes from matching `CLOP_NATION` against
row 1, and each row comes from looking its label up in column A. The located rows are grouped into
contiguous runs and written one block each — five writes on today's layout (`2:5`, `7:8`, `10:20`,
`22:27`, `29:42`) — so the spacer rows are never touched and an inserted row simply shifts the
answer.

If the nation's name is not in row 1, or a label is missing or duplicated in column A, **nothing on
this tab is written** and a blocking dialog names every problem. The nation tab still updates; the
two regions fail independently.

Writes are unconditional rather than diffed: an unreadable cell normalises to `0`, so it would
compare equal to a good the nation holds none of and the garbage would survive.

Run `python stockpiles.py` for a read-only report of where the lookups currently resolve to and what
a real run would write.

## Relationship to the other two orderings

Three different orders are in play; do not assume any one of them matches another.

1. **`Dashboard!A10:A42`** — this table. Hand-arranged by topic (fuels and food, then parts, then
   the endgame/DNA block), not alphabetical. Nothing in the code depends on it: rows are found by
   looking each label up, so this is the sheet's order, not the script's.
2. **The `overview.php` Resources panel** — `backend_overview.php` selects `ORDER BY rd.name`, but
   that order is discarded: the rows go into a name-keyed PHP array which `overview.php` then
   `ksort()`s. The result is *byte-order* alphabetical (case-sensitive ASCII), so e.g. all
   `DNA - …` rows sort Central → North → South within each region and land before `Drugs`
   (uppercase `N` precedes lowercase `r`). Only goods the nation actually holds a `resources` row
   for are rendered, plus any padded in from the required/affected maps — a good at zero with no
   production or consumption is simply absent from the page.
3. **A nation tab's `STOCK` block** (rows 11–16 today) — a six-row subset with its own lowercase
   labels: `apple`, `oil`, `coffee`, `mpart`, `vpart`, `gems`. These are **not** the Dashboard's
   labels. Both sets live together in `goods.py`'s `Good` records (`stock_label` and
   `dashboard_label`), so there is one table to keep right rather than two lists to keep in step.
   That block's order does not matter either — its rows are looked up by label as well.
