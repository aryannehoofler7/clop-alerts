# Stockpile snapshot — design

Date: 2026-08-23

## Goal

Record the nation's current stockpiles for six goods onto the nation's tab in the shared sheet, so
the planning columns beside them (NEED / BUY / TICKS) work off live numbers. The snapshot runs in the
same step as [building reconciliation](2026-08-23-building-reconciliation-design.md), immediately
after it and off the same `overview.php` fetch.

Two things are written:

- **`R11:R16`** — the HAVE quantity for each of the six goods named in `Q11:Q16`.
- **`W10`** — the CLOP **server** date/time the snapshot was taken, so a reader can tell whether the
  numbers are fresh.

Unlike building reconciliation this is a **snapshot, not a reconciliation**: the current R values are
not treated as data to be corrected and diffed against, they are simply replaced. Only these six
goods are recorded; the other resources the game tracks are deliberately out of scope.

## What the two ends look like (verified 2026-08-23 against the live site)

**overview.php** renders a "Resources" panel whose columns are
`Resource | Qty | Generated | Used | Loss | Net | Ticks-Worth`. Each row is

```html
<td style="width: 16px;"><img src="images/icons/Apples.png"/></td>   <!-- omitted if hideicons -->
<td style="text-align: right;">Apples</td>
<td><span class="text-success">226</span></td>                       <!-- Qty -->
<td style="text-align: center;">…</td>                               <!-- Generated, Used, … -->
```

Key facts:

- `Resource` is exactly `resourcedefs.name` for the non-building rows.
- Only resources the nation has a `resources` row for appear. **A good that is absent means 0** —
  the same rule as the Buildings panel.
- `Qty` is the first `<span>` in the row; it is comma-formatted (`commas()`), e.g. `1,204`.
- The row shape is **identical to the Buildings panel's**, so one parser serves both. The leading
  icon cell has no `text-align: right` and is skipped; the trailing `text-align: center` cells arrive
  after the Qty span has already been captured, so they are ignored.

**Server time** comes from the page header, not the local clock:
`<li><a>Server time: 2026-08-23 03:23:44</a></li>`, rendered by `clop/header.php:76` as PHP
`date("Y-m-d H:i:s")`. It is present on every logged-in page, including `overview.php`. The string is
written to `W10` **verbatim** — no parsing, no timezone conversion, no reformatting. Whatever
timezone the game server runs in, the sheet then shows the same wall-clock the game itself shows.

**The sheet** (`LePone(Z)`) has a stock block at rows 10–16:

| | Q | R | S | T | U |
|---|---|---|---|---|---|
| **10** | `STOCK` | `HAVE` | `NEED` | `BUY` | `TICKS (AFTER BUILD)` |
| **11** | `apple` | *have* | 560 | 560 | 0 |
| **12** | `oil` | *have* | 320 | 320 | 0 |
| **13** | `coffee` | *have* | 40 | 40 | 0 |
| **14** | `mpart` | *have* | 20 | 0 | inf |
| **15** | `vpart` | *have* | 0 | 0 | inf |
| **16** | `gems` | *have* | 120 | 0 | inf |

`V10` and `W10` are empty; `W10` is free for the timestamp.

## The name mapping (`STOCK_ROWS` in `stockpiles.py`)

The sheet's short lowercase labels map to `resourcedefs.name`. Sourced from the `resourcedefs` seed
data in `clop/tables with data.sql` (the non-building rows):

| Sheet label (`Q`) | Row | Game resource name |
|---|---|---|
| `apple` | 11 | `Apples` |
| `oil` | 12 | `Oil` |
| `coffee` | 13 | `Coffee` |
| `mpart` | 14 | `Machinery Parts` |
| `vpart` | 15 | `Vehicle Parts` |
| `gems` | 16 | `Gems` |

`mpart` and `vpart` are the two that are not guessable from the label alone; `Machinery Parts`
(resource_id 10) and `Vehicle Parts` (resource_id 9) are the only candidates in `resourcedefs`.

The mapping is an **ordered list**, not a dict: the order *is* the row order, and rows are derived as
`STOCK_FIRST_ROW + index`. This keeps the label→row and label→resource relationships in one place.

## Modules

### `overview.py` (new, extracted)

`buildings.BuildingsPanelParser` already parses the Resources panel correctly — it is only
hard-coded to arm on the panel heading `"Buildings"`. Rather than duplicate it or make `stockpiles`
depend on `buildings`, the parser moves to a new `overview.py` as `PanelParser(heading)`, returning
`list[tuple[name, value_text]]` for the named panel. `buildings.py` and `stockpiles.py` each import
it; neither depends on the other.

This is the only change to existing parsing behaviour, and it is a rename plus a constructor
argument — `buildings.parse_overview_buildings` keeps its signature and its results.

### `stockpiles.py` (new)

- `STOCK_ROWS: list[tuple[str, str]]`, `STOCK_FIRST_ROW = 11`, `TIMESTAMP_CELL = "W10"` — the table
  above as data.
- `parse_overview_resources(html) -> dict[str, int]` — `{resource_name: qty}` from the Resources
  panel via `PanelParser("Resources")`, stripping commas.
- `parse_server_time(html) -> str` — the `Server time: YYYY-MM-DD HH:MM:SS` string from the header.
  Raises `StockpileError` if it is absent: a missing timestamp means the page is not what we think it
  is, and a snapshot with no staleness marker is worse than no snapshot.
- `desired_stock(resources) -> list[int]` — the six quantities in row order; a good absent from the
  panel is `0`.
- `check_labels(sheet, nation) -> list[str]` — one read of `Q11:Q16`; returns the list of problems
  (empty = OK). A label is a problem when it does not match its expected `STOCK_ROWS` entry
  case-insensitively after stripping.
- `snapshot(sheet, nation, resources, server_time) -> list[tuple[str, int]]` — writes `R11:R16` as a
  single 6-row block, then `W10`. Returns the six `(label, qty)` pairs recorded.

### Drift safety

Rows are addressed **by position**, so a reordered or relabelled STOCK column would silently write
apples into the oil row. Before any write, `check_labels` confirms `Q11:Q16` reads exactly
`apple, oil, coffee, mpart, vpart, gems` in that order. On any mismatch **nothing is written — not
even `W10`** — and a blocking popup names the offending cell. Leaving `W10` stale is deliberate: it
is the signal that the recorded numbers are no longer being refreshed.

This mirrors `buildings.sanity_check`: a sheet the tool is unsure of is left completely untouched.

### Always overwrite, never diff

`R11:R16` is written on every run whether or not the values changed, and so is `W10`. Both are
unconditional on purpose.

An earlier draft read the current `R` values back (free, in the same read as the labels) and skipped
the write when all six already matched. That optimisation has a hole. `sheets.cell_int` normalises
an unreadable cell — `#REF!`, a stray label, a formula error — to `0`, which is indistinguishable
from a legitimate zero. So for a good the nation holds none of, a corrupted cell would compare equal
to the desired `0`, the write would be skipped, and `W10` would then stamp the sheet as freshly
verified with garbage still sitting in the cell. That is precisely the failure the staleness marker
exists to make visible, so the optimisation is not worth its cost.

Writing unconditionally also keeps the two cells' meanings honest and identical: **`W10` means *last
verified*, not *last changed*.** An old `W10` means the snapshot has stopped running, never merely
that nothing moved.

Cost is 3 endpoint calls per poll (one read for the labels, one block write, one cell write) on top
of what building reconciliation already does.

### Monitor integration

`reconcile_buildings_step` already fetches `overview.php`, recovers a dropped session before trusting
the page, and reports its own failures without taking the poll down. It is renamed **`sync_sheet_step`**
and gains the stockpile snapshot after the building reconcile, off the same HTML — the page is
fetched once per poll, not twice. Its existing guarantees carry over unchanged, including the
logged-out check (a logged-out overview would look like a nation holding nothing and would zero the
stock rows).

Popup policy:

- Label mismatch, a missing server time, or any transport/sheet failure → `notify_failure`, i.e. a
  blocking dialog, consistent with the rule that every warning is a popup.
- A routine stockpile write → **no popup**. It is a scheduled refresh rather than an event, and at a
  60s poll a popup would fire continuously. Building corrections keep their existing popup because
  they *are* events (the sheet was wrong and got fixed).

### Standalone script

`python stockpiles.py` logs in, fetches overview, and prints the six quantities, the server time, and
any label problems, with a matching exit code. It performs **no writes** — the counterpart to
`python buildings.py`.

## Testing

- `test_stockpiles.py`, offline: `parse_overview_resources` against synthetic panel HTML (comma
  formatting, with and without the icon cell, ignoring the Buildings/Weapons/Armor panels which have
  the same row shape); a good absent from the panel resolving to 0; `parse_server_time` finding the
  header string and raising when it is absent; `check_labels` accepting the expected labels and
  reporting a reordered, renamed, and blank one; `snapshot` writing `R11:R16` and `W10` against a
  stubbed `GoogleSheet`, including when the values already match; `STOCK_ROWS` naming
  six distinct goods that all exist in the game's non-building `resourcedefs` names.
- `test_buildings.py` continues to pass unchanged after the parser extraction (its assertions are
  about `parse_overview_buildings`, not the parser class).
- Live: run once against `LePone(Z)` — expect `R11:R16` to become `226, 40, 29, 0, 0, 6` from the
  observed overview, and `W10` to hold the server time from that same page load.

## Out of scope

No goods beyond the six listed; no writes to NEED / BUY / TICKS (those are the sheet owner's
formulas and inputs); no marketplace or trade actions; no new sheet rows or formatting; no timezone
normalisation of the server clock.
