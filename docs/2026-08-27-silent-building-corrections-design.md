# Building corrections are silent by default

**Date:** 2026-08-27
**Status:** implemented and tested
**Amends** the [building reconciliation
design](superpowers/specs/2026-08-23-building-reconciliation-design.md), which made every correction
raise a dialog unconditionally.

## The problem

`sync_sheet_step` reconciles the nation tab's building counts against `overview.php` and then pops a
blocking dialog listing what it changed:

```
Building counts corrected on the sheet:

- Basic Mine have 8 -> 10
```

The correction is right and wanted. The dialog is not. A desktop alert in this monitor is **modal** —
nothing is polled while one is on screen — so a correction the player already knows about (they built
the mines; that is why the count moved) stops the monitor until it is dismissed. Worse, dismissing it
throws away the fetched page and costs an extra overview fetch on the next poll, by the deliberate
rule in [the sheet is never reconciled against a page older than the
dialog](2026-08-26-stale-page-after-a-dialog-design.md).

The stockpile snapshot beside it has always been silent, on the grounds that a refresh is not an
event. A building correction was held to be different — "the monitor disagreeing with the sheet
rather than simply refreshing it" — but in practice the overwhelmingly common cause of a
disagreement is the player having built, bought or disabled something a minute ago. That is a
refresh too.

## Decision

**The correction still happens, and is still reported — to the terminal, not to a dialog.** A new
setting can restore the dialog for anyone who wants it.

### The setting

`alerts.building_corrections`, a boolean, **default `false`**.

It joins the existing `alerts` block (`user_messages`, `alliance_messages`, `news`, `reports`,
`market_orders`), loads through the same `boolean_setting` helper, and is therefore validated as
true/false and named in the startup "using built-in defaults for ..." line when the file leaves it
out. It is the only field on `AlertCategorySettings` whose default is `False`; that asymmetry is
deliberate and carries a comment saying so, because the surrounding fields would otherwise make
`True` look like the pattern.

### Silent means "does not interrupt", not "leaves no trace"

With the dialog off, the corrections are printed to stdout, in the same stream as the monitor's
per-poll status line:

```
Building counts corrected on the sheet: Basic Mine have 8 -> 10; Gem Mine disabled 0 -> 3
```

This is the whole reason the terminal line is not optional. Once a correction stops raising a
dialog, the scrollback is the *only* remaining record that the sheet and the game had drifted. A
correction that appears nowhere at all would make a genuine problem — a hand-edit being silently
reasserted, a mapping quietly zeroing a row — indistinguishable from nothing happening. The cost is
one line on the rare polls that corrected something.

When the dialog is switched on it replaces the line rather than joining it; the dialog already
carries the same facts, one per line, and printing both would double every correction in the
scrollback of the people who opted in.

### Scope: the whole dialog, both regions

`reconcile` corrects two regions — the **have** count in column B and the **disabled** count below
the `DISABLED:` marker — and reports both in one dialog. The setting silences both.

Gating only the have lines was considered and rejected. The two fields drift for the same reason (a
building was built, bought, sold or switched off), a partly-silenced dialog is harder to predict
than either an on or an off one, and it would mean a dialog whose contents depend on a setting
rather than on the game.

### What is *not* gated

Two other popups in the same step stay unconditional:

- the **sanity-check failure** ("Building sync skipped — the sheet layout or building mapping looks
  wrong"), and
- the **stockpile problems** list.

Both mean cells were **not** written. That is the opposite of a correction: the sheet is now known
to disagree with the game and nothing was done about it, which is exactly the state a player must be
interrupted for. The setting is named `building_corrections` — not `building_sync` — so that this
distinction is visible from the settings file.

`reconcile` itself is untouched. It writes every drifted cell and returns every `Correction` it made
exactly as before; only the caller's choice of reporting channel changes. Nothing about which cells
are written depends on an alerting setting, and that separation is what keeps the sheet correct
regardless of how the monitor is configured.

## Plumbing

`sync_sheet_step` gains a keyword-only parameter:

```python
def sync_sheet_step(
    client, sheet, nation, notifier,
    overview_html=None, stock=None, last_synced=None,
    *,
    alerts: AlertCategorySettings = AlertCategorySettings(),
) -> Optional[str]:
```

Keyword-only and last, so the existing positional call site (`overview_html, stockpiles,
sheet_synced`) is unchanged and the four tests that patch the function keep working. Passing the
whole `AlertCategorySettings` rather than a bare bool matches `check_and_notify`, which already
takes it that way, and means a second sheet-related alert switch needs no further signature change.

The default value is a default-constructed `AlertCategorySettings`, whose `building_corrections` is
`False` — so a caller that forgets to pass settings gets the documented default behaviour rather
than the loud one.

Hot reload needs no work. The poll loop passes `settings.alerts` freshly on every cycle, and
[hot-reload settings](2026-08-23-hot-reload-settings-design.md) replaces that object in place, so
editing the setting mid-run takes effect on the next poll like every other alert switch.

## Rejected

- **No output at all when the dialog is off.** The literal reading of "silently corrects". Rejected
  because it removes the last evidence that a drift ever happened; see above. "Silent" here means
  non-blocking.
- **A non-modal toast instead of a dialog.** The monitor has exactly one alerting mechanism, a
  blocking Windows dialog with a sound. Adding a second notification channel for one message is a
  large change to answer a question the terminal already answers.
- **Defaulting the setting to `true` to preserve existing behaviour.** The whole point of the
  request is that the current behaviour is wrong for the common case. A default that has to be
  turned off is not a fix.
- **Suppressing only corrections below some size**, e.g. a change of one building. Every threshold
  is arbitrary, and the size of a correction says nothing about whether it was expected.
