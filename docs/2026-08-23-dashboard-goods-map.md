# Dashboard-Stockpile row map

Reference for anything that reads or writes the shared sheet's **`Dashboard-Stockpile`** tab.

> **Renamed and rearranged on 2026-08-24, and rearranged again on 2026-08-25** — when a
> `Ticks Since Recorded` row was inserted as row 2, pushing every block below it down by one, and
> `A1` was turned from a `READ ONLY` notice into the sheet's "game now" clock. Nothing in the code
> broke on either occasion: every row is found by looking its label up, so the only change any of it
> ever needed was the tab *name* — the one thing no lookup can recover — in `stockpiles.DASHBOARD_TAB`.
> The row numbers below describe the sheet as it stands; they are the **result** of the lookups,
> never an input to them. Verified against the live sheet on 2026-08-25.

## What the tab looks like

Row 1 is the header — `B1` is `TOTAL (or min)` and `C1` onward one column per nation tab
(`LePone(Z)`, `quaity(P)`, `Pure Apple Acres(B)`, `Republic(B)`, `Solarium(Z)`, `GLA(S)`,
`Fish Bucket(S)`, `Vladihoofstock(Z)`, `Buenos Mares(B)`, and an `#N/A` spare in `L`).

`A1` is **the sheet's own clock**: game-now, as a real datetime formatted `yyyy-mm-dd hh:mm:ss` so
it reads like the game's stamps. It used to hold a `READ ONLY` notice aimed at people. The monitor
neither reads nor writes it; row 2 is what reads it, and that is the next section.

There are four labelled blocks the monitor writes, separated by blank spacer rows at 7, 14, 17, 29
and 36. Nothing follows row 50.

### Ticks Since Recorded — row 2, **the sheet's, not ours**

Inserted 2026-08-25, between the header and the first block the monitor writes. Each nation's cell
is a formula measuring how stale that column is: `A1` (game-now) against that column's `Active`
stamp, counted in **tick boundaries crossed** rather than elapsed hours. The formula and the
reasoning behind that definition are in
[`2026-08-25-game-time-is-utc.md`](2026-08-25-game-time-is-utc.md) — *How stale is a stamp, in ticks*.

**The monitor never writes this row, and must not start.** `Ticks Since Recorded` is not one of
`stockpiles.DASHBOARD_LABELS`, so the lookup does not resolve it, no write run can reach it, and the
formulas survive every sync. `test_stockpiles.py` pins that down in
`test_the_sheets_own_formula_row_is_not_located` and `test_the_sheets_own_rows_are_never_written`.

What the monitor *does* owe this row is the thing it already does: `Active` is written as text via
`stockpiles.as_sheet_text`, which is what keeps the formula's `DATEVALUE`/`TIMEVALUE` parse honest.
Storing that stamp as a date value instead would re-interpret the game's clock in the spreadsheet's
timezone, and the staleness number would be silently wrong by that offset.

### Status — rows 3–6, 15–16

Written from the overview "Nation" panel:

| Cell | Label | Source on overview.php | Written as |
|---|---|---|---|
| A3 | `Active` | the page header's `Server time:` stamp | text, e.g. `2026-08-23 12:26:13` |
| A4 | `Sat` | `Satisfaction` | `-61 (-2)` — current, per tick in parentheses |
| A5 | `NLR` | `Relationship with New Lunar Republic` | `1500 (Ascending)` |
| A6 | `SE` | `Relationship with Solar Empire` | `-120 (3)` |
| A15 | `GDP` | `GDP` | a number, so column B's `TOTAL (or min)` sums it |
| A16 | `Bits` | `Funds` | a number |

`Active` holds a last-updated timestamp despite its label. The per-tick figure is the literal string
the game printed when it is not a number: `Ascending` under Alicorn Elite and Transponyism, `Fixed`
under Solar Vassal and Lunar Client.

### Ticks-worth — rows 8–13

How long the current stock lasts at the current net rate, straight from the **Ticks-Worth** column
of the overview Resources panel — the same six goods the nation tab's `STOCK` block tracks, under a
third set of labels again (note the singular `M Part`, against the quantity block's plural
`M Parts`).

| Cell | Label | Game resource |
|---|---|---|
| A8 | `Apple - tick` | Apples |
| A9 | `Oil - tick` | Oil |
| A10 | `Coffee - tick` | Coffee |
| A11 | `M Part - tick` | Machinery Parts |
| A12 | `V Part - tick` | Vehicle Parts |
| A13 | `Gems - tick` | Gems |

The value is a whole number of ticks, or one of the game's own two words, passed through verbatim
rather than turned into a number:

- **`N/A`** — the net is zero or positive, so the stock never runs out. A good the nation holds none
  of and nothing consumes reads as `N/A` too, which is exactly what the game would print for it.
- **`NONE`** — there is already less than one tick's requirement left.

The Ticks-Worth column is located by **its heading** in the panel's `<thead>` row, not by counting
cells, because the game has changed that table's shape before. If the column is missing entirely
these six rows are **left exactly as they are** — not zeroed — and a dialog says so, because "we
could not read it" and "you have none" are different claims.

### Goods — rows 18–50

## The mapping

The "game name" column is `resourcedefs.name` from the game's seed data
(`clop/tables with data.sql`) — the exact string that appears in the Resources panel of
`overview.php`, which is what `stockpiles.parse_overview_resources` keys on.

| Cell | Name on sheet | Name in game (overview.php) | `resource_id` |
|---|---|---|---|
| A18 | Energy | Energy | 4 |
| A19 | Apples | Apples | 3 |
| A20 | Coffee | Coffee | 20 |
| A21 | Oil | Oil | 1 |
| A22 | Gas | Gasoline | 25 |
| A23 | Gems | Gems | 26 |
| A24 | Cider | Cider | 18 |
| A25 | Pies | Pies | 13 |
| A26 | Toys | Toys | 47 |
| A27 | Tungsten | Tungsten | 27 |
| A28 | Plastics | Plastics | 28 |
| A29 | *(blank spacer)* | — | — |
| A30 | Drugs | Drugs | 42 |
| A31 | Copper | Copper | 2 |
| A32 | M Parts | Machinery Parts | 10 |
| A33 | V Parts | Vehicle Parts | 9 |
| A34 | P Parts | Precision Parts | 29 |
| A35 | Composites | Composites | 30 |
| A36 | *(blank spacer)* | — | — |
| A37 | Forbidden Research | Forbidden Research | 75 |
| A38 | Apotheosis Serum | Apotheosis Serum | 77 |
| A39 | DNA - Burro - Central | DNA - Central Burrozil | 69 |
| A40 | DNA - Burro - North | DNA - North Burrozil | 68 |
| A41 | DNA - Burro - South | DNA - South Burrozil | 70 |
| A42 | DNA - Prze - Central | DNA - Central Przewalskia | 72 |
| A43 | DNA - Prze - North | DNA - North Przewalskia | 71 |
| A44 | DNA - Prze - South | DNA - South Przewalskia | 73 |
| A45 | DNA - Saddle - Central | DNA - Central Saddle Arabia | 63 |
| A46 | DNA - Saddle - North | DNA - North Saddle Arabia | 62 |
| A47 | DNA - Saddle - South | DNA - South Saddle Arabia | 64 |
| A48 | DNA - Zebrica - Central | DNA - Central Zebrica | 66 |
| A49 | DNA - Zebrica - North | DNA - North Zebrica | 65 |
| A50 | DNA - Zebrica - South | DNA - South Zebrica | 67 |

**The coverage is exact.** The 31 labelled rows are all 31 rows of `resourcedefs` with
`is_building = 0` — every good in the game, no omissions, no rows on the sheet that do not
correspond to a resource. So a reader can treat this block as the complete goods list.

## Notes on the four abbreviations

`Gas`, `M Parts`, `V Parts` and `P Parts` are the only labels that are not the game name verbatim,
and each has exactly one candidate in `resourcedefs`:

- `Gas` → `Gasoline` (25). The only good whose name begins "Gas"; `Gems` is separately listed at A23.
- `M Parts` → `Machinery Parts` (10) — the only `* Parts` beginning with M.
- `V Parts` → `Vehicle Parts` (9) — likewise the only one beginning with V.
- `P Parts` → `Precision Parts` (29) — the only `* Parts` beginning with P. `Pies` (A25) is a
  separate row and not a "Parts" good, so there is no clash.

The DNA rows reverse the game's word order (`DNA - <direction> <region>` becomes
`DNA - <region> - <direction>`) and abbreviate two regions: `Burro` = Burrozil, `Prze` =
Przewalskia, with `Saddle` = Saddle Arabia and `Zebrica` unabbreviated. The sheet groups them
region-first then direction, which is *not* the order `overview.php` renders them in (see below).

## How the script writes this tab

`stockpiles.py` writes the nation's own column on every sync, off the same `overview.php` fetch that
updates the nation tab. Nothing is hardcoded: the column comes from matching `CLOP_NATION` against
row 1, and each row comes from looking its label up in column A. The located rows are grouped into
contiguous runs and written one block each — six writes on today's layout (`3:6`, `8:13`, `15:16`,
`18:28`, `30:35`, `37:50`) — so the spacer rows are never touched and an inserted row simply shifts
the answer. The first run starts at row 3 for exactly that reason: rows 1 and 2 carry no label the
lookup knows, so no run can reach back over the header or the `Ticks Since Recorded` formulas.

If the nation's name is not in row 1, or a label is missing or duplicated in column A, **nothing on
this tab is written** and a blocking dialog names every problem. The nation tab still updates; the
two regions fail independently.

Writes are unconditional rather than diffed: an unreadable cell normalises to `0`, so it would
compare equal to a good the nation holds none of and the garbage would survive.

Run `python stockpiles.py` for a read-only report of where the lookups currently resolve to and what
a real run would write.

## Relationship to the other two orderings

Three different orders are in play; do not assume any one of them matches another.

1. **`Dashboard-Stockpile!A18:A50`** — this table. Hand-arranged by topic (fuels and food, then parts, then
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
   labels: `apple`, `oil`, `coffee`, `mpart`, `vpart`, `gems`. These are **not** the alliance tab's
   labels, and nor are its `- tick` ones. Both sets live together in `goods.py`'s `Good` records (`stock_label` and
   `dashboard_label`, `tick_label`), so there is one table to keep right rather than four lists to
   keep in step.
   That block's order does not matter either — its rows are looked up by label as well.
