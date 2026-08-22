# What the game writes to `reports` — a reference

**Date:** 2026-08-23
**Source:** read from `D:\Koan\clop\clop` at that date. Recipe names come from the shipped seed
dump `clop/tables with data.sql`, the only recipe data in the repo; a live database that has gained
recipes since could differ.

Written because the monitor's shipped ignore-pattern `Build % completed successfully.` turned out to
miss most of what it was meant to cover. This is the catalogue that replaced it.

## How reports are written

`INSERT INTO reports (nation_id, report, time)` appears at **38 sites** across ten files:
`backend_actions.php`, `backend_buyermarketplace.php`, `backend_createforces.php`,
`backend_deals.php`, `backend_favoriteactions.php`, `backend_majoractions.php`,
`backend_makeequipment.php`, `backend_marketplace.php`, `backend_transfer.php`, and
`cron/frequent.php`.

Three structural facts matter more than the individual sites:

1. **A report is usually several sentences, not one.** `backend_actions.php:246` builds the row as
   `implode("<br/>", $infos)` — every on-page message from that action, joined. So one finished
   build arrives as the three lines `You spent 20 Machinery Parts.` / `You paid 50,000 bits.` /
   `Build Advanced Factory completed successfully.`. The monitor judges a report **one line at a
   time** (`docs/2026-08-23-per-line-report-judging-design.md`), so silencing that report takes a
   pattern for each of the three — and a report that also contained a warning would still alert,
   showing the warning alone.
2. **`reports.php` merges reports sharing a timestamp** (`backend_reports.php:5-12`), joining them
   with `<br/>`. A tick can surface as one large block.
3. **`cron/frequent.php:808` deletes reports older than 24 hours**, so the page is never a complete
   history.

Not every player-facing message becomes a report. `$errors[]` and many `$infos[]` are page-only and
never reach the table — including every "you cannot build that" refusal. **A failed or refused
action produces no report row at all**, because the `INSERT` sits inside `if (empty($errors))`.

## The completion sentence, and why the old pattern was wrong

There is exactly **one** completion sentence in the entire game:

```php
$infos[] = "{$rs3['name']} completed successfully.";
```

at `backend_actions.php:240`, `backend_favoriteactions.php:164` and `backend_makeequipment.php:111`.

`$rs3['name']` is the **recipe's own name**, straight from the `recipes` table. There is no separate
wording for upgrades, for bulk builds, or for anything else — the only thing that varies is the
name. So a pattern anchored on the word "Build" matches only recipes that happen to be *named*
"Build …".

Of the 62 recipes in the seed dump, the first words are:

| First word | Count |
|---|---|
| Build | 38 |
| Upgrade | 5 |
| Ship | 4 |
| Dig | 3 |
| Distribute | 3 |
| Plow | 2 |
| Manufacture | 2 |
| Smuggle | 2 |
| Burn | 1 |
| Drug | 1 |
| Receive | 1 |

The 24 distinct names that `Build %` misses: Dig Basic Copper Mine, Dig Gem Mine, Dig Tungsten Mine,
Plow Basic Apple Orchard, Plow Coffee Farm, Burn Oil, Drug Farm, Distribute Apples, Distribute Pies,
Distribute Money, Upgrade Oil Well, Upgrade Copper Mine, Upgrade Apple Orchard, Upgrade Plastics
Factory, Upgrade to Gasoline Combustion Facility, Manufacture Precision Parts, Manufacture
Composites, Ship Oil to the Solar Empire, Ship Oil to the New Lunar Republic, Ship Tungsten to the
Solar Empire, Ship Tungsten to the New Lunar Republic, Smuggle Drugs into the SE, Smuggle Drugs into
the NLR, Receive Factory Aid.

Weapons and armour are unaffected: all 19 weapon and 18 armour recipes are named `Build …`.

**The fix is `% completed successfully.`** — the true template, immune to a recipe being added or
renamed later, which a hard-coded name list would not be.

## Routine, and worth silencing

These are the game's own bookkeeping, or something the player just did. The monitor ships patterns
for the common ones, all commented out.

- `% completed successfully.` — every finished action
- `You spent % %.` / `You gained % %.` / `You paid % bits.` / `You gained % bits.`
- `You gained % % from your % %.` / `Your % % used up % %.` — per-tick production and consumption
- `Your relationship with the % has improved due to your %. (%)` — also `dwindled`, `recovered`,
  `worsened`, plus the two cap variants `Your relationship with the % can't get any better, despite
  the effects of your %.` and `… can't get any worse- your % sure tried, though!`
  (`allfunctions.php:157`, `:162`)
- `Your population's satisfaction has improved due to your %. (%)` — same four words, and the same
  cap variant `Your population can't be any more satisfied, despite the effects of your %.`
  (`allfunctions.php:190`)
- `Show Details` / `Hide Details` / `Change in Satisfaction: %` / `Change in SE Relation: %` /
  `Change in NLR Relation: %` — the per-tick wrapper, plus `You're ascending; your relationships
  with the Solar Empire and New Lunar Republic can only go down.` in place of the two relation
  totals for an ascending empire (`frequent.php:776-784`)
- Government and economy upkeep: `Your Democracy used 20 gasoline.`, `Your State Controllers drank 6
  cider.`, `Your Free Marketeers drank 6 coffee.`, `Your machinery of Oppression used 10 gasoline and
  5 machinery parts.`, and their siblings
- Satisfaction and relation caps: `You hit the Democracy satisfaction cap of 1500. (-%)` and the
  other eight
- `A satisfied population is hard to keep. (-% sat)`, `A good friend is hard to keep; …`, `A bad
  enemy forgets eventually; …`
- `As you have more than 50,000 %, % was siphoned off.` / `As you have more than 1,000 %, % were
  siphoned off.`
- Trades the player initiated: `You bought % % from % for % bits.`, `You dealt away % %.`,
  `You received % %.`, `You accepted a deal with %.`, `You transferred % % to % for % bits.`
- `You have created the military force %.`
- All eighteen Major Action lines — `Government changed to %`, `Economy changed to %`,
  `Nation name changed from % to %`. Note several have **no trailing full stop**, and the game's own
  typo `Government revered from … to …` appears in five of them.
- `Your % used up % apples.` / `… gasoline.` / `… coffee.` / `… gems.` — force upkeep, UTC hours
  0 and 12 only

## Notable, and never worth silencing

Something happened *to* the player, or they are losing something.

- `You couldn't pay the upkeep for your % and it's gone!` — permanent loss of a force
- `Your % scattered to the four winds!` / `Your % lost % size!` — combat losses
- `Your % (size %) were hit by %'s % (size %) for % damage (% hits)` — being attacked
- `Your satisfaction is below the minimum - your ponies are revolting!`
- `The Solar Empire hates you enough to send an airstrike …` and the NLR twin, plus
  `The % has attacked you for daring to ascend! (%)`
- `You don't have enough % to run your % %!` — production silently failing
- `Too many % cause environmental damage! (-% sat)`
- `You lose % satisfaction for having % disabled buildings.`
- `You lost 5 satisfaction for not having any buildings!`
- `You lost % satisfaction for having a military of total size %.`
- `You lose % sat for having an empire of % nations.`
- `Your % government lacks the % to function properly!` — the nation is starving
- `Your deal with % was rejected.` / `was accepted.`, `You received % % as part of your deal.`,
  `You sold % % to % and made % bits.`, `This nation received % % from %.` — **someone else acted
  on you**, which is why these sit here rather than with the trades above
- `You have completed the forbidden research, and the facility has been automatically dismantled.`

## `Change in Satisfaction:` — the trap that per-line judging removed

That phrase appears in every tick report, which makes it an attractive pattern. While the monitor
matched a pattern against the whole row, switching it on silenced every "Notable" line above that
arrived inside the tick — the lost force, the environmental damage, the starving government — and no
other pattern did any better, because the routine and the notable text share a row.

Judging each line separately is what fixed it: the phrase now silences the one line it appears on.
The tick still takes a pattern per routine line to go quiet, which is what the block from
`Show Details` down in `settings.example.json` is for.

## Known gaps in this catalogue

- `Inflation has taken away % bits.` (`frequent.php:108`) sits inside a commented-out block and
  cannot currently fire.
- `Serious empire problem - Report this bug!` (`frequent.php:42`, `allfunctions.php:143`) is
  unreachable — every call site passes a valid empire name.
- Nation-death paths (`frequent.php:903`, `:1516`, `:1536`) write to `messages` and `news`, not
  `reports`.
- Three tick reports contain a literal newline mid-sentence, from a heredoc spanning two source
  lines (`frequent.php:952`, `:985`, `:1310`). The monitor splits a report at the page's own line
  breaks and not at newlines in the text, so these stay one line and a pattern must read across as
  one line.
