# The sheet is never reconciled against a page older than the dialog

**Date:** 2026-08-26
**Status:** implemented and tested

## The problem

Reported from live use, and the worst shape a fault in this tool can take — a **false correction**:

```
[2026-08-26T05:39:14+12:00] CLOP: Building counts corrected on the sheet:
- Toy Factory have 2 -> 1
```

The nation had two Toy Factories. The sheet said two. The monitor overwrote it with one, and
announced it in the same dialog it uses for real corrections, so nothing about the message said it
was wrong. A tool that silently replaces a right number with a wrong one and calls it a correction
is worse than a tool that does nothing.

Confirmed against both sides before changing anything:

```
overview.php  'Toy Factory' -> '2 '            # parses to (have 2, disabled 0)
nation tab    row 30  A='Toy Factory'  B=1     # what the monitor had just written
```

Every other one of the 36 rows matched the game exactly, in both regions. Only Toy Factory was
wrong, and only by the one build.

## Root cause

Not the parser, the mapping or the sheet layout — all three were right, then and now. It is the
**age of the page** the reconcile ran against.

One poll of `main()` does this, in this order:

```
overview_html, stockpiles = read_overview_stockpiles(client)   # (1) fetch the page
current, _ = check_and_notify(..., stockpiles)                 # (2) alert — BLOCKS on a modal dialog
sync_sheet_step(..., overview_html, stockpiles, ...)           # (3) reconcile the sheet against (1)
```

Step (2) is modal. `Notifier.notify` returns *"whether a desktop dialog blocked until dismissal"*,
and nothing is polled while one is on screen. So the gap between (1) and (3) is not the few hundred
milliseconds the code reads like — it is however long the alert stood there. Step (3) then writes
what the game looked like **before** the dialog appeared.

`check_and_notify` already knew this. On a paused alert it re-reads the game — *"The counts are
re-read because dismissing the popup is when messages get read"* — and returns `paused_for_alert`
saying so. The loop assigned that flag to `_` and threw it away, then handed step (3) the page from
before the pause.

The incident timeline is exactly that:

| UTC | |
|---|---|
| 17:01 | poll fetches `overview.php`; Toy Factory reads **1** |
| 17:01:07 | report alert (*"You bought 24 Gasoline…"*) goes up — the poll stops here |
| ~17:01–17:39 | a second Toy Factory is built, and the sheet is set to **2** by hand |
| ~17:39 | the dialog is dismissed; the poll resumes |
| 17:39:14 | the sync reconciles against the **17:01** page and writes `Toy Factory have 2 -> 1` |

Thirty-eight minutes of staleness, presented as a correction.

Two things made it land in the sheet rather than being caught:

- **Nothing about a stale page is detectable from the page.** `require_valid_overview` proves the
  response is a complete, normal overview render. A 38-minute-old one passes every check in it,
  because it *is* a complete, normal overview render — of the past.
- **A stale reconcile is indistinguishable from a real one.** Both are "the sheet disagrees with the
  page I hold", and both come out of `reconcile` as an ordinary `Correction`.

## The rule

**A page fetched before a dialog went up is not evidence about the game after it came down.**

Sheet sync may only reconcile against a page read *after* the last thing that paused the poll.
`paused_for_alert` is the whole signal; the loop now uses it:

```python
current, paused_for_alert = check_and_notify(...)
...
if paused_for_alert:
    overview_html = None
    stockpiles = None
```

`sync_sheet_step` already reads the page itself when it is handed none, so dropping the stale one is
the entire fix. It is deliberately expressed as *discard*, not as *re-fetch*: the sync owns the
decision about what page it needs, and the loop only says whether the one it has is still worth
anything.

Applied to `stockpiles` as well as `overview_html`, for the same reason. Both are parsed from that
one page, so both are exactly as old as it is, and the stockpile half writes goods quantities and
the dashboard column off it.

### What it costs

One extra `overview.php` fetch on the polls that paused — and only those. A poll that paused is by
definition a poll that was in no hurry, and a poll that did not pause reuses the page it already
read, so the steady-state request count is unchanged. That second half is a test of its own
(`test_an_unpaused_poll_still_reconciles_off_the_page_it_already_read`), because a fix that re-read
unconditionally would double every poll's overview traffic to close a hole that was not open.

### Why not stamp the page with a time and check its age

Considered and rejected. An age threshold needs a number nobody can justify (is a 90-second-old page
fine? a 5-minute-old one?), and it would still be answering a question the code already knows the
exact answer to. `paused_for_alert` is not a heuristic about staleness — it is the fact of it.

## How it is tested

`PageReconciledAgainstIsNewerThanTheDialogTests` in `test_clop_monitor.py`, driven end-to-end through
`main()` with a watched good (which is what makes the loop read overview up front), a notifier that
reports its dialog as having blocked, and a `read_overview_stockpiles` that returns a *different*
page after the pause — standing in for the game moving while the dialog was up.

- `test_a_dialog_that_paused_the_poll_costs_the_page_its_currency` — the sync reconciles against the
  page read after the dialog. This is the test that fails on the old code, with
  `['before-the-dialog'] != ['after-the-dialog']`.
- `test_an_unpaused_poll_still_reconciles_off_the_page_it_already_read` — no dialog, no re-read.

## What this does not cover

Inside `sync_sheet_step` the building popups still go up *before* `batched.flush()`, so a modal there
sits between the reconcile and the write. That cannot produce a wrong number — the decision was made
against a current page, and the stockpile timestamp is the game's own `server_time` carried on that
page rather than a clock reading — but it does mean the "Building counts corrected on the sheet"
dialog is shown before the write it describes has actually gone out. If the flush then fails, a
second dialog reports the failure and the sync is not cached, so the next poll writes it properly;
the first dialog was still, briefly, ahead of the facts. Left as it is here, and noted rather than
folded into this fix, which is about what the sheet is reconciled *against*.
