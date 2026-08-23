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

It is stored as **text**, via `stockpiles.as_sheet_text`, which prefixes Sheets' own force-text
marker (a leading apostrophe, consumed on storage, so the cell still displays exactly what the game
printed). Left as a bare string, Sheets would recognise `2026-08-23 07:12:30` as a datetime and store
a date value — silently reinterpreting the game's clock as being in the *spreadsheet's* timezone.
That is the one failure mode verbatim copying exists to avoid, and it is invisible: a staleness
formula built on the mis-tagged date would be wrong by the offset while looking perfectly reasonable.
Storing text keeps the ambiguity where it belongs — with whoever writes a formula against it, who
must then convert explicitly.

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

`overview.py` also owns `panel_present`, `OverviewError`, and `require_valid_overview` — the policy
deciding whether a fetched page can be trusted at all. See [Is this page real?](#is-this-page-real)
for why that policy lives here rather than in either caller.

### `stockpiles.py` (new)

- `STOCK_ROWS: list[tuple[str, str]]`, `STOCK_FIRST_ROW = 11`, `TIMESTAMP_CELL = "W10"` — the table
  above as data.
- `parse_overview_resources(html) -> dict[str, int]` — `{resource_name: qty}` from the Resources
  panel via `PanelParser("Resources")`, stripping commas. Raises `StockpileError` if any row's Qty
  is not a plain integer. Dropping it silently would be worse: the good would fall through to `0`
  and be written to the sheet as "you hold none", stamped fresh — the same false-zero the read side
  is guarded against below. The game renders every Qty through its integer `commas()` helper, so
  anything else means the page changed and a human should look.
- `parse_server_time(html) -> str` — the `Server time: YYYY-MM-DD HH:MM:SS` string from the header.
  Raises `StockpileError` if it is absent: a missing timestamp means the page is not what we think it
  is, and a snapshot with no staleness marker is worse than no snapshot.

  Both parsers raising `StockpileError` on a page that does not look right is the module's single
  error-handling rule: **surface it, never absorb it.** `check_labels` is the same rule applied to
  the sheet end. Every one of them ends in a blocking popup with nothing written.
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

### Is this page real?

Drift safety guards the sheet end. This guards the game end, and it protects against a single
mistake: **reading a broken page as a nation that owns nothing and holds nothing.** That mistake is
the worst outcome this tool can produce, because it does not look like a failure. It zeroes the tab
and then stamps `W10` freshly verified — hiding the exact condition the staleness marker exists to
expose. `require_valid_overview` in `overview.py` refuses the page instead. It is called immediately
after the fetch, before anything is parsed and long before anything is written, so its messages can
say flatly that nothing was written.

The policy lives in `overview.py`, not in `buildings.py` or `stockpiles.py`, for two reasons. It is
expressed entirely in this module's vocabulary — panels, headings, whether the page finished — and
both callers need it while [neither may depend on the other](#modules). A copy in each would drift.

Three checks, each closing one way a broken page could pass for an empty nation.

**Both panel headings are present.** `overview.php` emits `<div class="panel-heading">Resources</div>`
and its Buildings counterpart from unconditional heredocs: no branch in the PHP can omit them, so
they appear even for a nation with nothing at all. That makes the distinction sharp and load-bearing:

| Heading | Table | Means |
|---|---|---|
| present | rows | normal |
| present | empty | a genuinely empty panel — a new nation owns no buildings |
| **missing** | — | **this is not an overview page** |

A missing heading is therefore proof of a broken render — a PHP fatal after `header.php` already
flushed, a maintenance page, a redirect somewhere else — not evidence about the nation. This is why
`panel_present` exists separately from `parse_panel` returning no rows; collapsing the two would
throw away the only signal that separates the second row of that table from the third.

The headings are checked in page order, so the first complaint tells you how far the response
actually got. That order is also doing structural work. Resources precedes Buildings on the page, so
a present Buildings heading proves the response ran past the Resources panel's closing `</table>` —
the Resources panel is complete, not merely started. One check, two facts.

**The page finished.** `footer.php` ends every page with `</html>`. Without it the response was cut
off, and the cut that the heading check cannot catch is one landing *after* the Buildings heading:
both headings present, every building row missing. Those would be read as buildings sold and written
to the sheet as a routine correction.

The test is deliberately **ends-with**, not *contains*. A `</html>` inside a comment, an attribute,
or an error message quoting some HTML would let a truncated page claim it finished, and the check has
to fail closed to be worth having.

**Not both panels empty.** `backend_overview.php` fills buildings and resources from a *single*
query. On PHP 5.4 a failed query makes `mysqli_fetch_array` warn rather than fatal, so the page
renders whole, `</html>` and both headings included, with both tables empty. Either panel may
legitimately be empty on its own — hence the check is on both at once, which is that one shared query
having died. The check names the two panels literally rather than looping `REQUIRED_PANELS`,
because the reasoning is about those two specifically sharing a query; a third required panel added
later must not silently join the conjunction and weaken it.

#### Two accepted costs

Both are consequences of failing closed, and both are worth paying:

- **A brand-new nation is blocked until its first tick.** Before the tick it has no `resources` rows
  and owns no buildings, so its page really is doubly empty and indistinguishable from a dead query.
  The snapshot refuses it and pops up. The first tick clears it permanently.
- **Anything appended after `footer.php` fails the completeness check**, even on a perfectly healthy
  page — a PHP notice from a shutdown handler, a debug line, something a proxy injects. If the page
  looks complete in a browser and this keeps firing, that trailing output is what to look for.

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

**All parsing happens before any writing.** The step validates the page, reads the server time, and
parses the resources first; only then does it reconcile buildings and snapshot stockpiles. This is
ordering as a safety property, not tidiness. Every way the page can turn out to be untrustworthy
raises before the first cell is touched, so "nothing was written" is something the failure messages
can state as fact rather than hedge. Interleaving the two — parse buildings, write buildings, parse
resources, discover the resources are unreadable — would leave the tab half-updated under a
timestamp that no longer describes it.

**The two syncs are independent for layout problems only.** A STOCK label mismatch skips the
stockpile snapshot and leaves the building reconcile to run, and vice versa: they guard different
regions of the sheet, and one region being wrong says nothing about the other.

A transport or sheet failure in either aborts **both**, deliberately. It means the shared connection
is down — the same `GoogleSheet`, the same network — so attempting the other half would fail the same
way and produce a second dialog about one outage.

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
- `test_overview.py` covers the trust gate directly, including the two decisions that look like
  brittleness and are not: the exact `panel-heading` comparison and the ends-with test for a
  finished page. Its `GameSourceAssumptionsTests` reads the game's own PHP — checked out beside
  this repo — to assert the three facts the gate rests on, and skips where it is not. Drift on the
  game side therefore fails here rather than silently letting a broken page through.

**Outcome of the first live run** (2026-08-23, `LePone(Z)`): `R11:R16` became `198, 30, 27, 0, 0, 12`
from the observed overview, the sheet's `BUY` and `TICKS` formulas recalculated off them, and `W10`
took the server stamp from that same page load.

That run is also how the `W10` text problem surfaced. Written bare, `2026-08-23 07:12:30` came back
from the sheet as `2026-08-23T07:12:30.000Z`: Sheets had parsed it into a date value, which reads
the game's clock as being in the *spreadsheet's* timezone — the single assumption this design
refuses to make. `stockpiles.as_sheet_text` now prefixes Sheets' own force-text marker, an
apostrophe, which is consumed on entry; re-verified live, `W10` reads back identical to the header
stamp. `python stockpiles.py` prints the sheet's stamp beside the game's so the two can be compared
by eye, because the monitor writes that cell without ever reading it back.

## A truncated response no longer kills the monitor

Found while reviewing this work and fixed here, because a monitor that has silently exited is the
one failure a user cannot notice: its only symptom is the absence of alerts.

`ClopClient._open` read the body inside a `try` catching `urllib.error.HTTPError` and
`urllib.error.URLError`. A response whose body is cut short mid-transfer — a `Content-Length` the
server never delivers — makes `response.read()` raise `http.client.IncompleteRead`, which subclasses
`HTTPException` and is neither of those. Nothing downstream caught it either: not `sync_sheet_step`'s
`except` tuple, not the poll loop's `except MonitorError`, not `main`'s. It reached the top and
terminated the monitor with a traceback and **no dialog at all**, breaking both `sync_sheet_step`'s
own promise that "sheet sync must never take the monitor down" and the rule that every warning is a
popup.

`_open` now also catches `http.client.HTTPException` and re-raises it as `MonitorError`, so it
routes into the existing blocking dialog and the poll loop carries on. `OpenTransportFailureTests`
in `test_clop_monitor.py` pins all three transport failures — `IncompleteRead`, a malformed status
line, and a plain `URLError` — as arriving in the form the callers know how to report.

This is *not* what the `</html>` completeness check above catches. That check handles a response
that arrives intact but was cut short server-side (PHP died mid-page, the connection closed
cleanly); `IncompleteRead` is the transport failing to deliver a body it promised, and the string
never reaches `require_valid_overview` at all.

## Out of scope

No goods beyond the six listed; no writes to NEED / BUY / TICKS (those are the sheet owner's
formulas and inputs); no marketplace or trade actions; no new sheet rows or formatting; no timezone
normalisation of the server clock.
