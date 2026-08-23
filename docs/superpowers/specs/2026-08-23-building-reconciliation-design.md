# Building reconciliation — design

Date: 2026-08-23

## Goal

Before the monitor does its regular alerting, reconcile the nation's building counts on
`https://4clop.org/overview.php` against the nation's tab in the shared sheet, writing corrections
for any wrong **have** count (column B) or **disabled** count (column B in the disabled region), then
raise a blocking popup listing the corrections made. Reconciliation is its **own process that fires
first**, ahead of the message/news/report alerting; it never blocks that alerting.

A standalone sanity-check script verifies the name mappings and sheet structure still hold, so a
reformatted sheet or a new game building is caught rather than silently mis-written.

## What the two ends look like (verified)

**overview.php** renders a "Buildings" panel; each row the nation *owns* is
`<td …right>NAME</td><td><span>AMOUNT (N disabled)</span></td>`. Key facts:
- `NAME` is exactly `resourcedefs.name` (the query in `backend_overview.php:146` is
  `SELECT … rd.name … WHERE rd.is_building` — the canonical building list).
- Only **owned** buildings (amount ≥ 1) appear. A building absent from the panel means have 0,
  disabled 0.
- `AMOUNT` is the have count; the optional `(N disabled)` is the disabled count (0 if absent).

**The sheet** (`LePone(Z)`), column A = building, column B = count, in two regions:
- **Have region:** header `Building` / `Have` (seen at row 8), building rows below it, up to the
  `DISABLED:` marker.
- **Disabled region:** after the `DISABLED:` marker and a second `Building` header, one row per
  building. It is **not** a mirror of the have region — it omits Lunar Enviro, Solar Enviro, and
  Alicornification — so rows are located by **name within each region**, never by a fixed offset.

## The name mapping (`building_map.py`)

The 48 `is_building` rows in `resourcedefs` are the authoritative overview names. They map to the 36
sheet rows, mostly 1:1, with renames (e.g. `Basic Copper Mine`→`Basic Mine`,
`Cider Production Facility`→`Cider Facility`) and two **many-to-one** groups confirmed with the sheet
owner:
- **DNA**: all 12 `DNA Extraction Facility - <region>` buildings sum into the single `DNA` row (in
  practice a nation only holds its own region's facility, so the sum is one value).
- **Energy Collector**: `Solar Collector` + `Tidal Generator`, summed.

`building_map.py` is a data helper: `GAME_TO_SHEET` (game/overview name → sheet name, 48 entries) and
`SHEET_BUILDINGS` (the 36 sheet names). It is sourced from `resourcedefs` and cites that origin.

## Modules

### `buildings.py`

- `parse_overview_buildings(html) -> dict[str, tuple[int, int]]` — `{overview_name: (have,
  disabled)}` for owned buildings. An `HTMLParser` subclass scoped to the Buildings panel, in the
  same style as the monitor's other parsers, so it is unit-tested with synthetic HTML.
- `desired_counts(overview) -> dict[str, tuple[int, int]]` — fold overview onto sheet names via
  `GAME_TO_SHEET`, summing the many-to-one groups; every `SHEET_BUILDINGS` name gets a value,
  defaulting to `(0, 0)`. Raises on an overview name absent from the mapping (a new/renamed game
  building).
- `locate_regions(colA) -> Regions` — given column A's values, find the have-region and
  disabled-region name→row maps by scanning for the `Building`/`Have` header and the `DISABLED:`
  marker. Robust to row shifts.
- `reconcile(sheet, nation, overview) -> list[Correction]` — read column A + column B once, compute
  desired have/disabled per building, and **write only the cells whose value differs** (empty cell
  normalises to 0 for the comparison). Returns the corrections made (building, field, old, new).
- `sanity_check(sheet, nation, overview) -> list[str]` — returns a list of problems (empty = OK):
  a missing/!found have header or `DISABLED:` marker; a `SHEET_BUILDINGS` name absent from, or
  duplicated within, the have region; a disabled-region name not in the have region; any overview
  building not in `GAME_TO_SHEET`. Used both by the standalone script and inline before writing.

### Monitor integration

A `reconcile_buildings(client, sheet, nation, notifier)` step runs at the **start** of each poll,
before the snapshot/alerting:
1. Ensure the client is logged in, fetch `overview.php`, parse it.
2. Run `sanity_check`. If it returns problems: raise a **blocking popup** naming them, **skip all
   writes** this cycle (never write to a row it is unsure of), and return — regular alerting still
   proceeds.
3. `reconcile`. If any corrections were made, raise a **blocking popup** listing them (e.g.
   `Basic Mine have 8 → 10; Basic Mine disabled 0 → 1`).

Gated by a `sheet_sync` setting (default on) and only runs when `CLOP_NATION` is set. A transport or
sheet error during reconciliation raises a popup and is swallowed for that cycle; it does not abort
the poll (reconciliation is best-effort and must never take the monitor down).

### Standalone sanity script

`python buildings.py` logs in, fetches overview, runs `sanity_check`, and prints each problem (or a
clean bill) with a matching exit code — the tool to run whenever the sheet format may have changed.
It performs **no writes**.

## Testing

- `test_buildings.py`, offline: `parse_overview_buildings` against synthetic panel HTML (with and
  without a disabled clause, ignoring non-building panels); `desired_counts` folding + summing DNA /
  Energy Collector and raising on an unknown name; `locate_regions` on a synthetic column A;
  `reconcile` writing only changed cells (stubbed `GoogleSheet`); `sanity_check` catching each
  problem class. `GAME_TO_SHEET` covers every `SHEET_BUILDINGS` value and vice versa.
- Live: run reconciliation once against `LePone(Z)` — expect it to correct `Basic Mine` have and set
  its disabled count from the observed `10 (1 disabled)`, leaving matching cells untouched.

## Out of scope

No recycle/disable/build actions on the game (read-only there), no new sheet rows, no formatting.
Reconciliation only writes existing have/disabled cells.
