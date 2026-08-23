# What the game writes to `reports` — a reference

**Date:** 2026-08-23
**Source:** read from `D:\Koan\clop\clop` at that date. Recipe names come from the shipped seed
dump `clop/tables with data.sql`, the only recipe data in the repo; a live database that has gained
recipes since could differ.

Written because the monitor's shipped ignore-pattern `Build % completed successfully.` turned out to
miss most of what it was meant to cover. This is the catalogue that replaced it.

**Interface amendment, 2026-08-24.** The sentence-family catalogue below remains the internal
safety corpus, but it is no longer dumped into `settings.example.json`. The game writes one tick
report, so settings now expose one `Tick` selector; the monitor applies the catalogue internally to
remove routine tick lines while preserving warnings in the same report. Completed recipes likewise
use `Action: <recipe-name pattern>`, with shipped examples for `Build %`, `Burn Oil`, and
`Distribute Pies`. The old 25-entry list described below is implementation history, not the current
user-facing settings list.

**Revised the same day.** The first list this catalogue produced answered the miss by adding
patterns, and reached 33 of them: twelve for the completion sentence alone, two for relations, two
for satisfaction, two for the `Change in ...` totals. Twelve of the 33 were redundant — a pattern
matches anywhere in a line, so `% completed successfully.` already covered the eleven verb-specific
ones beside it — and the list still missed eight routine families, because effort had gone into
wordings that were already covered rather than into families that were not. The list is now **25
patterns covering 49 routine sentences**, one per family, and
[the rule below](#the-rule-the-list-is-held-to) is enforced by a test.

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
- `Even for the Solar Empire, there are limits to hate. (+%)` and the NLR twin
  (`frequent.php:745`, `:752`) — the floor on hate, the mirror of the relationship caps above
- `Some of the environmental damage has been repaired. (% sat)` (`frequent.php:355`)
- `The Solar Empire doesn't like your good relations with the New Lunar Republic. (-%)` and the NLR
  twin (`frequent.php:248`, `:254`) — a standing cost of courting both, not an event
- Trades the player initiated: `You dealt away % %.`, `You received % %.`,
  `You accepted a deal with %.`, `You transferred % % to % for % bits.`,
  `This nation paid % bits and % received % bits.` (`backend_transfer.php:60` — the wording for a
  money transfer, which does *not* start "You") — and `You bought % % from % for % bits.`, but see
  *The marketplace sentences cut both ways* below
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
  `This nation received % % from %.` — **someone else acted on you**, which is why these sit here
  rather than with the trades above
- `You sold % % to % and made % bits.` — but only on one of its two paths; see below
- `You have completed the forbidden research, and the facility has been automatically dismantled.`

### The marketplace sentences cut both ways

`You bought …` and `You sold …` are each written to **both** sides of a trade, in identical words,
so nothing in the text says which side you were:

| Written at | `You bought % % from % for % bits.` goes to | `You sold % % to % and made % bits.` goes to |
|---|---|---|
| `backend_marketplace.php:215` / `:234` | the actor, who clicked buy | the **passive** seller whose listing was taken |
| `backend_buyermarketplace.php:238` / `:257` | the **passive** buyer whose standing order was filled | the actor, who clicked sell |

So neither sentence is purely Routine or purely Notable, and the split above is a judgement about
which path matters more rather than a property of the text. The consequence for the monitor is
concrete and worth stating plainly: **the shipped `You bought …` pattern silences your own purchases
and also a stranger filling your standing buy order.** No `You sold …` pattern ships, because
switching one on would equally silence somebody buying from your standing sell order.

There is no way to separate them by pattern. Anyone who trades enough for the noise to matter can
add a `You sold …` pattern themselves, knowing what it costs.

## The shipped pattern set

Twenty-five patterns, all commented out, covering the 49 routine sentences above. The column on the
right is what each one reaches — not a paraphrase of the pattern, but the count of distinct game
sentences it silences.

| Pattern | Covers |
|---|---|
| `completed successfully.` | all 62 actions |
| `You spent % %.` | the resource cost of an action |
| `You paid % bits.` | the money cost of an action |
| `You gained % %.` | an action's yield, a tick's production, and money either way |
| `You bought % from % for % bits.` | both marketplaces (see the caveat below) |
| `You transferred % to % for % bits.` | a resource transfer you sent |
| `This nation paid % bits and` | a money transfer you sent |
| `You dealt away % %.` | resources, weapons, armour and bits given in a deal |
| `You accepted a deal with` | the deal you accepted — *not* `Your deal with % was accepted.` |
| `You have created the military force` | a new force |
| `Show Details` / `Hide Details` | the tick wrapper (2) |
| `Change in %:` | the satisfaction total and both empire relation totals (3) |
| `You're ascending;` | the wrapper an ascending empire gets instead |
| `Your % used %.` | government upkeep (7), production consumption, force upkeep (9) |
| `Your % drank %.` | the State Controlled and Free Market economies (2) |
| `Your relationship with the` | four wordings and both caps (6) |
| `Your population` | four satisfaction wordings and its cap (5) |
| `You hit the % cap of %.` | eight satisfaction caps and two relationship caps (10) |
| `there are limits to hate.` | the floor on hate, both empires (2) |
| `is hard to keep` | oversatisfaction decay and friendship decay (3) |
| `forgets eventually;` | enmity decay, both empires (2) |
| `siphoned off.` | both siphons (2) |
| `environmental damage has been repaired.` | the environment healing |
| `doesn't like your good relations with` | each empire's jealousy of the other (2) |

Three routine-looking families deliberately have **no** pattern, and the reasons are worth keeping
because each is a decision rather than an oversight:

- **`You received % %.`** — the deal *you* accepted and a deal *somebody else* accepted differ only
  by the trailing words `as part of your deal`, and a pattern short enough to cover the first covers
  the second. The second is somebody acting on you, so neither is silenced.
- **The eighteen Major Action lines** — `Government changed to %`, `Economy changed to %`,
  `Nation name changed from % to %`. A player takes one perhaps twice a year; seeing it confirmed is
  the point. (Several have no trailing full stop, and the game's own typo `Government revered from
  … to …` appears in five of them, so covering them would take three patterns for no benefit.)
- **`You lose % sat for having an empire of % nations.`** — a standing penalty worth watching, and
  filed under Notable above.

## The rule the list is held to

Two properties, both tested, and they are what stop the list growing back:

1. **Coverage** — the shipped set silences every line in the routine catalogue.
   (`test_the_shipped_set_silences_every_routine_line_the_game_writes`)
2. **No redundancy** — remove any one pattern and some routine line starts alerting again.
   (`test_no_shipped_pattern_is_redundant`)

Together they force one pattern per *family of game sentence* rather than one per wording, which is
the distinction the first list lost. The mechanism that makes families collapsible is that
**a pattern matches anywhere in a line**: the shortest phrase unique to a family covers every
variant of it, so `Your relationship with the` needs nothing appended to reach all six relation
sentences, and a leading or trailing `%` is always dead weight. That is why `% completed
successfully.` and `completed successfully.` are the same pattern, and why shipping
`Build % completed successfully.` beside the catch-all bought nothing at all.

The third property — that no shipped pattern silences anything in the Notable list — predates this
revision and is unchanged (`test_no_shipped_pattern_silences_a_notable_line`). It is why the
collapsing above is safe: every pattern was widened only until it still failed to match any Notable
sentence.

### What the two properties do not prove

Worth being exact, because "covers everything with no false positives" is stronger than what is
actually established here.

- **Both tests are only as good as the corpus.** `ROUTINE_REPORTS` and `NOTABLE_REPORTS` were read
  by hand from all 38 `INSERT INTO reports` sites and transcribed into the test file. They are not
  mechanically extracted from the PHP, so a sentence missed in the reading is missed by both tests.
  The corpus is the thing to extend when a report turns up that neither list predicted.
- **One routine family is deliberately left alerting.** `You received 5 Oil.` is written to the
  nation that *accepted* a deal (`backend_deals.php:260`), and `You received 5 Oil as part of your
  deal.` to the nation somebody else acted on (`:251`). No pattern can separate them, so neither is
  silenced and the routine one still alerts. The set therefore trades a known false alert for the
  certainty of never hiding the other — the same choice made for `You sold …`.
- **The eighteen Major Action lines are uncovered by choice**, so 18 of the 38 insert sites have no
  pattern at all. That is a decision about what is worth seeing, not coverage.
- **Nation names are player-supplied free text**, and every pattern is a substring rule. A nation
  named after a pattern — say one calling itself `A satisfied population is hard to keep` — would
  make `This nation received 20 Apples from …` match and be silenced. This is inherent to substring
  matching rather than new here, it is not defended against, and short patterns carry more of the
  risk than long ones. It is the one argument for a longer pattern than the shortest that works.
- **Verification is unit tests over fixtures.** The monitor has not been run against the live game
  with this list, so the fixtures being faithful to the pages the game actually renders rests on the
  same hand-reading as the corpus.

## `Change in Satisfaction:` — the trap that per-line judging removed

That phrase appears in every tick report, which makes it an attractive pattern. While the monitor
matched a pattern against the whole row, switching it on silenced every "Notable" line above that
arrived inside the tick — the lost force, the environmental damage, the starving government — and no
other pattern did any better, because the routine and the notable text share a row.

Judging each line separately is what fixed it: the phrase silences only the line it appears on.
The tick still takes an internal matcher per routine line to go quiet; the user-facing `Tick`
selector applies the complete safe set as one option.

## Known gaps in this catalogue

- `Inflation has taken away % bits.` (`frequent.php:108`) sits inside a commented-out block and
  cannot currently fire.
- `Serious empire problem - Report this bug!` (`frequent.php:42`, `allfunctions.php:143`) is
  unreachable — every call site passes a valid empire name.
- Nation-death paths (`frequent.php:903`, `:1516`, `:1536`) write to `messages` and `news`, not
  `reports`.
- **Seven** tick reports contain a literal newline mid-sentence, in two shapes: `frequent.php:952`
  (the revolt), `:985` and `:1014` (the two airstrikes) are two-line double-quoted strings, and
  `:1296`, `:1302`, `:1309` and `:1315` are three-line heredocs (the four combat lines). The
  monitor splits a report at the page's own line breaks and not at newlines in the text, so these
  stay one line and a pattern must read across as one line.
