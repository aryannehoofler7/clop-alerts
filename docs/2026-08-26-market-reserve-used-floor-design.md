# Market reserves stand on `Qty - Used`

**Date:** 2026-08-26
**Status:** implemented and tested
**Supersedes** the three independent reserve modes described in the README when the reserve feature
first shipped (commit `2651769`, 2026-08-24), which had no design doc of their own. This is that
doc, written when the omission produced a bug.

## The problem

Reported from live use: *"the buy order alerts — it's meant to only be for what you have quantity
to spare towards after your own 'used' amount. But I have 6 gems, use 6 gems, and I'm still getting
alerts on gems."*

The alerts were correct for the settings, and the settings were the shipped default. `Gems` was on
`"reserve": "none"`, and `none` meant *"`Qty` is above zero"*. Six gems held is `Qty` 6, so it
alerted. Nothing in any of the three modes read the `Used` column at all, so no configuration of
the feature as shipped could express what the feature was for.

Reproduced end-to-end before changing anything, with a friend's standing order for Gems and an
overview holding 6 with 6 used:

```
AssertionError: Lists differ:
  ['Buy orders for Gems:\n  Luna Sueno (friend) wants 12 at 900 bits each'] != []
```

That is now `test_a_stock_entirely_spent_each_tick_raises_no_alert`.

## What the game does

`clop/overview.php` renders the Resources panel seven columns wide:

| Column | PHP | Meaning |
|---|---|---|
| `Qty` | `$amount` | what is in the stockpile now |
| `Generated` | `$affectedresources[$name]` | produced per tick |
| `Used` | `$requiredresources[$name]` | **consumed per tick by your own buildings and population** |
| `Loss` | `$taxes[$name]` | taken per tick |
| `Net` | `($affected - $required) - $taxes` | the per-tick change |
| `Ticks-Worth` | see below | how long the stock lasts |

And `overview.php:171-181` computes the last of them:

```php
if      ($Qty < $Used)  $displayreserves = "NONE";
else if ($Net >= 0)     $displayreserves = "N/A";
else                    $displayreserves = floor(($Qty - $Used) / abs($Net));
```

Two facts in that snippet decide this design:

1. **`Qty - Used` is the game's own idea of spare stock.** It is the numerator the game divides to
   answer "how long will this last", and the quantity it tests to decide the stock is already short.
2. **`N/A` is blind to it.** The page prints `N/A` for *any* non-negative `Net`, whatever `Qty` and
   `Used` are. A nation generating exactly what it consumes reads `N/A` with nothing spare at all.

## Decision

**`spare = Qty - Used` is a floor under every reserve mode, checked before the mode is consulted.**
A tick's own consumption has to be on hand whatever the mode says, so what a mode measures is the
spare, never the raw quantity:

| Mode | Test |
|---|---|
| `none` | `spare > 0` |
| `qty` | `spare - reserve_amount > 0` |
| `ticks` | `spare > 0` **and** `Ticks-Worth > reserve_amount` |

`reserve` stops being three unrelated tests and becomes one floor plus a per-mode refinement of it.
`none` is then honestly named: it is no *additional* reserve, not no reserve.

### The ticks number is not adjusted, and that is the point

The instinct on hearing "every mode subtracts `Used` first" is to subtract it from the tick count
too. That would be wrong twice over. `Ticks-Worth` is `floor((Qty - Used) / |Net|)` — the game has
**already** taken `Used` off, and the number it prints is spare-based. Deducting `Used` from a
count of ticks would also be a unit error: units of stock taken off a number of ticks.

So on `ticks` the printed number is compared exactly as it stands. What the floor adds there is
one case and one only: `N/A`, which the game prints without consulting spare at all. `NONE` needs
nothing — it means `Qty < Used`, so the floor has already refused it. This is the whole of the
`ticks` change, and it is the case the reporter would have hit next had they switched modes.

### Where it sits in the decision order

The floor goes where the old positive-`Qty` guard was: after the buyer has matched, not before.

1. the order is the active nation's own → skip
2. the nation name matches a `never` pattern → skip
3. the nation name matches an `always` pattern, or `friends`/`alliance` matches → candidate
4. **`spare > 0`** → otherwise skip
5. the mode's own test → alert or skip

Keeping it at step 4 preserves what the original design said about `always`: it overrides the
relation checks, and it cannot invent stock that is not there. It also keeps the cheap string tests
ahead of the stockpile lookup that can raise.

### A missing `Used` column raises

`Stockpiles.used()` mirrors `Stockpiles.ticks()` exactly: the map is tri-state, and `None` — no
such column on the page — raises `StockpileError`, which `market_order_alerts` turns into a
`MonitorError` and the existing per-poll failure dialog. It does not fall back to zero.

Zero would be the dangerous default here. "We could not read your consumption" would silently
become "you have the whole stockpile spare", which is the exact over-alerting this change exists to
stop, arriving with no error and no log line. A good absent from the page still reads `0` used, and
correctly: a nation holding none of something is spending none of it, and the `spare > 0` test
refuses it on the quantity anyway.

This does mean a `Used` column that disappears takes the market feature down with a dialog every
poll rather than degrading. That is the same trade the roster fetch and the empty-market banner
already make in the [market alerts design](2026-08-22-market-buy-order-alerts-design.md): for a
monitor, a visible failure beats a silent under- or over-report. The README's troubleshooting table
names the message and says to comment the watched goods out to keep the rest of the monitor
running.

## What this changes for a settings file

Nothing has to be edited. Every good in a shipped `settings.json` is on `"reserve": "none"` with
`reserve_amount` 0, and those goods simply stop alerting on stock that is entirely committed. A
`qty` reserve now counts its threshold from the spare rather than the raw quantity, so an existing
`reserve_amount` reserves that much **on top of** a tick's consumption — which is what somebody
writing a reserve meant, and is strictly more conservative than before. No settings file becomes
invalid and no new mode was added.

## Rejected

- **A fourth `"used"` mode, leaving the other three alone.** The first shape considered, and the
  one the reporter was offered. Rejected on their instruction, and they were right: it makes
  "don't sell what you are eating" opt-in, and every mode that is not it stays wrong in the same
  way. Three modes that each need the same floor is a floor, not a fourth mode.
- **Redefining `none` only.** Fixes the reported case and leaves `qty` counting from the raw
  quantity and `ticks` alerting at `N/A` with nothing spare. Two bugs left standing to close one.
- **Subtracting `Used` from `Ticks-Worth` as well.** Double-counts, and mixes units. See above.
- **Treating a missing `Used` column as zero used.** One less failure mode, at the cost of the
  silent over-alerting this document exists to remove.
- **Subtracting `Loss` too.** `Loss` is what tax takes, not what you need on hand, and the reporter
  asked for `Used`. The game's own `Ticks-Worth` counts `Loss` in `Net` rather than in the
  numerator, so leaving it out of the floor keeps the two definitions of spare identical.
