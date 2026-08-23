# Dashboard sync — design

Extend the existing stockpile snapshot so one pass over `overview.php` updates **both** the player's
own nation tab **and** the alliance-wide `Dashboard` tab: all 31 goods plus six nation-status rows.

Supersedes nothing — it builds on `2026-08-23-stockpile-snapshot-design.md`, which stays accurate for
the nation tab's purpose and its "overwrite, never diff" rule. What changes there is only *how the
cells are located* (lookup, not fixed addresses).

## Scope

In scope:

- parse `overview.php` **once** per sync into two reusable value objects, and write from those;
- keep writing the nation tab's six-good `HAVE` block and its timestamp;
- additionally write the nation's own column on `Dashboard`: 31 goods and 6 status rows;
- locate every target cell by looking up labels, so no row or column address is hardcoded;
- fail loudly and per-region: a layout problem on one tab never silently corrupts the other.

Out of scope (deliberately):

- the nation tab does **not** gain the status rows — `Dashboard` is the alliance-wide view;
- no `Dashboard`-wide timestamp beyond the per-nation `Active` row;
- no other nation's column is ever read for meaning or written — only the configured `CLOP_NATION`;
- no game actions. This remains read-the-game, write-the-sheet.

## Constraints found in the live sheet and the game source

These drove the design and are recorded so a later reader does not have to rediscover them.

1. **`Dashboard!A1` reads `READ ONLY`.** Confirmed by the sheet owner: that warns *people* off
   hand-filling the grid. The script is the intended writer.
2. **Nation names live in row 1, not column A.** `B1` is `TOTAL`, `C1`..`K1` are the nine nation tabs,
   and `L1` holds `#N/A` — a spare that must never match a lookup.
3. **The `Dashboard` goods block has spacer rows** at 6, 9, 21 and 28. They must survive every write.
4. **A nation tab's column Q holds two label blocks.** `Q10` is the `STOCK` header with the six goods
   beneath it, but `Q19` starts a second block (`COST`, then `Copper`, `M Part`, `V Part`, `P Part`).
   A global search of column Q for labels would be ambiguous, so the search must be *scoped to the
   run beneath the `STOCK` header*.
5. **`overview.py`'s `parse_panel` cannot read the Nation panel's per-tick figures.** It captures the
   first `<span>` of a value cell; the per-tick number is a *second* span in the same cell. Worse,
   `overview.php:50-60` emits a bare `(Ascending)` with no span at all under the Alicorn Elite and
   Transponyism governments, and `backend_overview.php:320-325` sets the per-tick to the literal
   string `Fixed` for Solar Vassal and Lunar Client. Reading whole-cell text is the only approach
   that covers all three shapes.
6. **The game's `commas()` corrupts non-integers.** `clop/backend/allfunctions.php:26-31` walks the
   *string* form of the number inserting a comma every three characters from the right, so a
   fractional GDP renders as `5,062,25.` rather than a number. GDP is integral in practice (every
   `gdp` contribution is a multiple of 1000 and every government multiplier is a half-integer), but
   the parser must refuse such a value rather than pass a mangled number to a shared sheet.

## Module layout

Two new pure modules hold the parsed page; the existing writer consumes them. Nothing new touches
the network, and nothing new knows about both the game and the sheet at once.

| Module | Role | Knows about |
|---|---|---|
| `goods.py` *(new)* | the 31-good vocabulary + `Stockpiles` | the game's names only |
| `nation.py` *(new)* | `NationStatus` — the Nation panel + server clock | the game's names only |
| `overview.py` *(edit)* | + whole-cell panel parsing | HTML |
| `sheets.py` *(edit)* | + `column_letter`, `find_in_row`, `index_column` | A1 geometry |
| `stockpiles.py` *(rewrite)* | locate both regions, write both regions | the sheet |
| `clop_monitor.py` *(edit)* | one extra parse, one call | wiring |

`stockpiles.py` keeps its name: the sheet owner's framing is that this is "essentially stockpiles",
and the name is already baked into the README and into popup text that tells people to run
`python stockpiles.py`.

`dashboard.py` is deliberately **not** created. An earlier draft had one; folding it in was the
owner's call, and it is the right one — a separate module would have re-fetched or re-parsed the same
page, which is exactly the duplication this design exists to remove.

## `goods.py`

One frozen table carries all three naming systems, so the duplicate label lists disappear:

```python
@dataclass(frozen=True)
class Good:
    game_name: str                 # resourcedefs.name, exactly as overview.php renders it
    resource_id: int               # for cross-referencing the game's seed data
    dashboard_label: str           # Dashboard column A
    stock_label: Optional[str]     # nation tab column Q; None for the 25 not in that block

GOODS: Tuple[Good, ...]            # 31 entries
```

with `BY_GAME_NAME`, `BY_DASHBOARD_LABEL`, `BY_STOCK_LABEL` and `BY_RESOURCE_ID` indexes built from
it. The contents are the table in `docs/2026-08-23-dashboard-goods-map.md`, which is itself derived
from `clop/tables with data.sql` (`resourcedefs` rows with `is_building = 0`).

Because both sheet regions are now located by label lookup, `GOODS` carries **no ordering
obligation**. The order values are written in comes from the rows the lookup found, not from this
list. That is what makes an inserted sheet row harmless.

`Stockpiles` is the parse-once handle, and is the piece explicitly future-proofed for the later
feature that needs to read current stockpiles:

```python
stock = Stockpiles.from_overview(html)   # the single parse per sync
stock["Apples"]        # 218
stock.get("Toys")      # 0 — absent from overview means the nation holds none
stock.as_dict()        # {game_name: qty}
"Gems" in stock
```

`from_overview` keeps the current `parse_overview_resources` rule: a quantity that is not a plain
integer raises `StockpileError` rather than being skipped. Skipping would let the good fall through
to zero and be written as "you hold none", stamped freshly verified.

`StockpileError` is defined here (it is raised here) and re-exported from `stockpiles.py`, so
`clop_monitor.py`'s existing import and the popup wording are unchanged.

## `nation.py`

```python
@dataclass(frozen=True)
class Reading:
    current: int
    per_tick: Union[int, str]      # int normally; "Ascending" or "Fixed" verbatim from the game
    def display(self) -> str:      # "218 (-5)"  /  "1500 (Ascending)"

@dataclass(frozen=True)
class NationStatus:
    government: str
    economy: str
    satisfaction: Reading
    se: Reading                    # Relationship with Solar Empire
    nlr: Reading                   # Relationship with New Lunar Republic
    gdp: int
    funds: int
    server_time: str               # "YYYY-MM-DD HH:MM:SS", exactly as the page printed it
```

`NationStatus.from_overview(html)` reads the `Nation` panel through the new whole-cell parser and
matches rows by their label — `Satisfaction`, `Relationship with Solar Empire`, `Relationship with
New Lunar Republic`, `GDP`, `Funds`, `Government Type`, `Economic Type`. Matching by label means the
conditional `Warning:` row (rendered when `active_economy` is false) is ignored for free.

Cell shapes handled:

| Panel cell text | Parsed as |
|---|---|
| `218 (-5 per tick)` | `Reading(218, -5)` |
| `1500 (Ascending)` | `Reading(1500, "Ascending")` |
| `-120 (Fixed per tick)` | `Reading(-120, "Fixed")` |
| `60,900 bits per tick` | `gdp = 60900` |
| `1,234,567 bits` | `funds = 1234567` |

A missing row, or a value that does not match its expected shape (including a `commas()`-corrupted
number), raises `NationStatusError`. Nothing is written when it does.

`parse_server_time` moves here from `stockpiles.py` — it is page-header data about the nation's
clock, and `NationStatus` is now its natural home. Both `NationStatusError` and `parse_server_time`
are re-exported from `stockpiles.py`; `clop_monitor.py` adds `NationStatusError` to the exception
tuple `sync_sheet_step` already catches.

## `overview.py` — whole-cell panel parsing

`PanelParser` gains a `cell_text` mode, and `parse_panel_text(html, heading)` exposes it. In that
mode, once the name cell has closed, the *next* `<td>` opens capture and capture ends only at that
cell's `</td>`, with whitespace collapsed. Tags inside the cell (`<span>`, `<br/>`) contribute their
text and do not end capture.

This is an extension of the existing parser rather than a second one, on purpose: the "arm only on a
`panel-heading` div whose text matches **exactly**" rule lives in `PanelParser`, and it is what stops
a favourite action named `Resources` from impersonating the Resources panel. A separate Nation-panel
parser would not inherit that protection.

`parse_panel` is untouched in behaviour; `buildings.py` and the goods parse keep using it.

## `sheets.py` — grid helpers

- `column_letter(index: int) -> str` — 0-based index to A1 column, correct past `Z` (`26 -> "AA"`).
- `find_in_row(row, text) -> Optional[int]` — exact match after `str(...).strip()`.
- `index_column(grid) -> Dict[str, List[int]]` — every non-empty column-A text to the **1-based** rows
  it occupies. Returning all occurrences (not the first) is what lets callers detect duplicates;
  deciding whether missing or duplicated is a problem stays with the caller.

## `stockpiles.py` — locating and writing

### Nation tab

Read `Q1:W60` once, then:

1. find `STOCK` in column Q → header row;
2. find `HAVE` **in that header row** → the value column (removes the hardcoded `R`; the header
   lookup also puts `NEED` and `BUY` one step away for the later feature);
3. walk down from the header row while column Q is non-empty, mapping label → row. Stopping at the
   first blank is what scopes the search away from the `COST` block at `Q19` (constraint 4);
4. require all six `stock_label`s in that run;
5. the timestamp cell is column `W` of the header row.

`W10` has no label to anchor on, so only its row is looked up. Its column is convention, and the
write is **guarded**: it happens only if the cell is empty or already holds a `YYYY-MM-DD HH:MM:SS`
value. Anything else is reported as a problem and nothing on this tab is written — the guard exists
so a layout change can never make the snapshot clobber a cell that now means something else.

### Dashboard tab

Read `A1:Z60` once, then:

1. `find_in_row(row 1, nation)` → the nation's column, via `column_letter`. `LePone(Z)` resolves to
   `C`. The `#N/A` in `L1` never matches;
2. `index_column` over column A → label → rows;
3. require all **37** labels — the 6 status labels and the 31 `dashboard_label`s — present exactly
   once.

| Row | Label | Value written |
|---|---|---|
| 2 | `Active` | `server_time`, forced to text |
| 3 | `Sat` | `satisfaction.display()` — e.g. `218 (-5)` |
| 4 | `NLR` | `nlr.display()` |
| 5 | `SE` | `se.display()` |
| 7 | `GDP` | `gdp` as a number, so column B's `TOTAL` can sum it |
| 8 | `Bits` | `funds` as a number |
| 10–42 | the 31 goods | `stock.get(game_name)` as a number; `0` when absent |

Row numbers above describe today's sheet; they are the *result* of the lookup, never an input to it.

`Active` holds a last-updated timestamp despite its label — the sheet owner's explicit instruction —
and uses the same `as_sheet_text` leading-apostrophe trick as the nation tab's stamp, so Sheets
stores the game's clock as text instead of silently reinterpreting it in the spreadsheet's timezone.

Satisfaction, `NLR` and `SE` go in as one combined text cell each, matching how the game displays
them. They are not summable across nations, and summing a relation would be meaningless anyway.

### Writing

The located rows are sorted and grouped into **contiguous runs**, one block write per run. On today's
layout that is five writes — `2:5`, `7:8`, `10:20`, `22:27`, `29:42` — plus one for the nation tab's
six-row block and one for its timestamp. Because the runs are computed from found rows, the spacer
rows are never included, and a sheet edit that moves a label just produces different runs.

Writes are **unconditional**, carrying forward the existing rule: an unreadable cell (`#REF!`, a
stray label) normalises to `0`, so it would compare equal to a good the nation holds none of and the
garbage would survive a diff-and-skip while the timestamp declared the row freshly verified.

The nation tab's timestamp is written **after** its values, so it can never claim freshness for a
block write that failed.

## Failure handling

`snapshot(sheet, nation, stock, status) -> Tuple[Report, List[str]]` locates and writes each region
independently and returns everything it wrote plus every problem it found.

- A problem in a region means **nothing in that region is written** — for the nation tab that
  includes its timestamp, because a fresh stamp over stale numbers is worse than an obviously stale
  one.
- The two regions are independent: a `Dashboard` layout problem still lets the nation tab update,
  and vice versa. This matches how `buildings.py` and the stockpile snapshot already relate.
- Every problem is collected, not just the first, so one dialog shows a person everything to fix.
- A `SheetError` (transport, dead endpoint) is not a layout problem and aborts both regions — it
  means the shared connection is down, so retrying the other half would only produce a second popup
  about the same outage.

Problem messages name the cell and what was actually found. The missing-column case names the nation
and lists row 1's contents, because "your tab is called something else" is the likely cause:

```
Dashboard sync skipped - no column in row 1 is named 'LePone(Z)'.
Row 1 reads: TOTAL, LePone(Z), quaity(P), Pure Apple Acres(B), ...
```

## Monitor wiring

`sync_sheet_step` keeps its shape and its ordering (buildings, then this). It gains one parse and
passes both value objects to one call:

```python
stock = Stockpiles.from_overview(overview_html)
status = NationStatus.from_overview(overview_html)
...
report, problems = snapshot(sheet, nation, stock, status)
if problems:
    notifier.notify_failure(...)
```

A successful write stays **silent**: at a 60-second poll a popup for a scheduled refresh would never
stop firing. `NationStatusError` joins the caught exception tuple.

The step remains best-effort — every failure reports through the blocking dialog and is then
swallowed, because sheet sync must never take the monitor down. The existing re-login guard stays: a
logged-out overview would look like a nation that owns nothing and would zero both tabs.

## Standalone diagnostic

`python stockpiles.py` stays read-only and never writes. It now reports both regions:

- the nation tab's resolved header row, `HAVE` column and timestamp cell, then the six-row
  label/game/overview/sheet table it already prints;
- the resolved `Dashboard` column, then the 37-row table in the same form;
- any problems from either region, followed by a blocking dialog.

Printing the resolved addresses is the point: when the sheet has been rearranged, this is what shows
a person where the script now thinks the block is.

## Testing

All offline, standard-library `unittest`, in the existing `FakeSheet` style.

`test_goods.py` — 31 entries; `game_name`, `dashboard_label` and the non-`None` `stock_label`s each
unique; `resource_id`s match the committed goods map; `Stockpiles.get` of an absent good is `0`; an
unreadable quantity raises.

`test_nation.py` — every cell shape in the table above, including `Ascending`, `Fixed`, and a
`commas()`-corrupted `5,062,25.` GDP rejected; a missing panel row raises; a missing server-time
stamp raises.

`test_overview.py` (extend) — `parse_panel_text` captures a two-span cell, a no-span cell, and a cell
whose per-tick is bare text; it still arms only on an exact heading match.

`test_sheets.py` (extend) — `column_letter` across `Z`/`AA`; `index_column` reports duplicates.

`test_stockpiles.py` (extend) — the behaviours that make "lookup, not hardcode" real:

- a row inserted above `STOCK` shifts the located block and the writes follow it;
- the `COST` block below the run is never picked up;
- moving `HAVE` to another column moves the values with it;
- a missing stock label ⇒ problem, and **nothing** written including the timestamp;
- a timestamp cell holding something that is not a timestamp ⇒ problem, nothing written;
- the `Dashboard` nation column not found ⇒ problem naming row 1's contents;
- a duplicated `Dashboard` label ⇒ problem;
- run grouping never includes rows 6, 9, 21 or 28;
- exact write payloads for a known `Stockpiles` + `NationStatus`.

## Documentation to update in the same change

- `docs/2026-08-23-dashboard-goods-map.md` — add the six status rows and record that the script
  writes this tab.
- `README.md` — the bullet list currently describes the six-good snapshot; extend it to both tabs.
- This spec — already the design of record.

---

## Amendment, 2026-08-24 — rename, rearrange, and the tick rows

The sheet changed the day after this was built. Recorded here rather than rewritten above, so the
reasoning that produced the design stays legible next to what actually happened to it.

**The tab was renamed** `Dashboard` → `Dashboard-Stockpile`, **and its blocks were moved**: the
status rows kept 2-5, `GDP`/`Bits` went from 7-8 to 14-15, and the goods block from 10-42 to 17-49.

The only code change either of those needed was the one constant, `stockpiles.DASHBOARD_TAB`. Every
row and column is found by looking a label up, so the rearrangement was something the code simply
followed — which is the payoff for the "no brittle hardcodes" decision in the original design, and
worth noting because it was tested by reality within a day. A tab *name* is the one thing no lookup
can recover for us, so it stays a constant with a comment saying why.

**A new block was added at rows 7-12**: `Apple - tick`, `Oil - tick`, `Coffee - tick`,
`M Part - tick`, `V Part - tick`, `Gems - tick` — how many ticks the nation's stock of each lasts.

- The source is the **Ticks-Worth** column of overview's Resources panel (`overview.php:181`,
  `$displayreserves`), which `parse_panel` structurally could not see: it captures only the first
  `<span>` of a row. `overview.py` therefore gained a third mode, `cells`, and `parse_panel_cells`.
- The column is located **by its heading**. The panel's `<thead>` row arrives through the parser
  like any other row, as `("Resource", ["Qty", "Generated", "Used", "Loss", "Net", "Ticks-Worth"])`,
  so the index is looked up rather than counted to. The icon cell has no `text-align: right`, so it
  drops out of both the header and the data rows and the columns line up either way.
- The quantity and the ticks-worth are two columns of the same row, so they are read in **one pass**
  (`goods._parse_resources_panel`) and both live on the same `Stockpiles` object: `stock["Apples"]`
  and `stock.ticks("Apples")`.
- The value is written **as the game prints it** — a whole number, or the literal `N/A` (net is zero
  or positive, so it never runs out) or `NONE` (already under one tick's worth). A good absent from
  the page reads as `N/A`, which is what the game itself would render for it: `overview.php:176-181`
  takes the `N/A` branch when the amount is not below the requirement and the net is not negative,
  and a good with no requirement satisfies both.
- If the panel has **no Ticks-Worth column at all**, the six rows are left exactly as they are and a
  problem is reported. They are not zeroed: "could not read it" and "you have none" are different
  claims, and only one of them would be true. Everything else on the tab is still written. This is
  the same per-region independence the original design applies to the two tabs, one level finer.

`DASHBOARD_LABELS` is now 43 labels — 6 status, 6 tick, 31 goods — and today's layout writes six
contiguous runs rather than five.

`Good` gained a fourth name, `tick_label`. Note it is a *third* vocabulary, not derivable from the
others: the sheet says `M Part - tick` (singular) where its quantity row says `M Parts` (plural) and
the nation tab says `mpart`.
