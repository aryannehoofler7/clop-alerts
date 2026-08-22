# Buyer's-marketplace order alerts — design

**Date:** 2026-08-22
**Status:** implemented, reviewed, and verified against the live game on 2026-08-23
**Amended 2026-08-23:** hot-reloading `settings.json` (see
`2026-08-23-hot-reload-settings-design.md`) made the startup preflight no longer the only
preflight, which changed four statements in this document. Each is marked **Amended** in place
rather than rewritten away, so that what this design originally decided stays readable. Nothing
else here changed.

Live verification (2026-08-23): a scan of all 28 tradeable goods found 2 alliance buy orders for
Machinery Parts and correctly classified 26 enemy and 6 alliance buyers, flagging none of the ~23
unrelated buyers. The run changed no game state.

Alert when somepony you care about has a pending buy order for a good you care about, so you
can sell into it before somepony else does.

## The problem

The monitor watches messages, news, reports, and a 4chan thread. It does not watch the market.
A friend or alliance member who posts a buy order for a good you are sitting on is an
opportunity that expires the moment another player fills it, and nothing tells you about it.

The game already marks these buyers visually: on the buyer's marketplace, a buyer who is your
friend renders blue and one in your alliance renders green. This feature reads that same
signal, and repairs the one place where it is ambiguous.

## What the game does

Read from `clop/buyermarketplace.php`, `clop/backend/backend_buyermarketplace.php`,
`clop/backend/backend_viewalliance.php`, `clop/backend/backend_viewnation.php`, and
`clop/header.php` in the main CLOP repository.

### The order list only exists on POST

The deals query is inside `if ($_POST)` (`backend_buyermarketplace.php:264`). A plain GET of
`buyermarketplace.php` renders the form and nothing else. Reading orders therefore requires a
POST carrying `resource_id`.

### The POST needs a rotating CSRF token

`backend_buyermarketplace.php:52` rejects a POST whose `token_buyermarketplace` field does not
equal `$_SESSION['token_buyermarketplace']`, pushing `"Try again."` into `$errors`; the deals
query at `:264` sits inside the `if (!$errors)` block opened at `:58`, so a rejected POST
returns no orders. Line 55 then regenerates the token on *every* POST, and the freshly
rendered form carries the new value. Tokens therefore have to be chained: read one from the
current page, spend it on the next POST, and take the replacement out of that response.

### A refused POST is indistinguishable from an empty market — except for one banner

Two facts about that rejection combine into a trap, and both are load-bearing for the fetch
loop:

1. **The token rotates even when the POST is refused.** Lines 55-57 rotate unconditionally on
   any POST, *before* the `if (!$errors)` gate at `:58`. So the error page comes back carrying
   a fresh, valid token. In a loop that posts once per watched good, the next good's POST then
   succeeds and the loop silently self-heals — the refused good just contributes zero orders,
   and nothing anywhere reports that its request never ran. Chaining the token is therefore
   *not* enough to detect a refusal: the token check at the top of the next iteration can never
   fire on this path.

2. **The empty-market banner is the positive marker for the genuine case.**
   `buyermarketplace.php:162-166` renders
   `<div class="alert alert-warning">Nobody wants to buy that item.</div>` under
   `else if ($_POST['resource_id'] && empty($errors))` — that is, *only* when the request
   actually ran and found nobody. A refusal renders neither the table nor this banner.

So the response to each POST has to be checked directly, and the rule is: no order rows **and**
no banner means the request never ran, which is an error rather than an absence of buyers. This
is the same invariant as the alliance roster below — a fetch that failed must raise rather than
be reported as "nothing found", because a silent under-report is exactly the failure this
monitor exists to prevent.

### The colours

`buyermarketplace.php:63-93`, in this precedence order:

| Buyer | Bootstrap class | Colour |
|---|---|---|
| Friend (`friends` table) | `text-info` | blue |
| Enemy (`enemies` table) | `text-danger` | red |
| Same alliance | `text-success` | green |
| Anyone else | *(no span)* | plain |

Only one colour is rendered per buyer, and friend is tested before alliance. The colour is
therefore a complete answer for friendship and an incomplete one for alliance membership:

| Colour | Friend? | In your alliance? |
|---|---|---|
| blue | yes | **unknown** |
| red | no | **unknown** |
| green | no | yes |
| plain | no | no |

This did not matter while friends and alliance were a single combined check — blue or green,
you were alerted either way. With them separate it matters a great deal: `friends: false,
alliance: true` would silently skip an ally you had also friended, which is precisely the order
that configuration exists to catch. See *Resolving alliance membership* below.

### The colour trap

The same three classes are used elsewhere in the same row: the price cell is `text-danger` and
the amount cell is `text-success` (`buyermarketplace.php:97-98`). Matching colour classes
page-wide would report every row as an enemy and an ally simultaneously. Relation detection
must be scoped to the Buyer cell's `viewnation.php?nation_id=` anchor.

### Goods

28 resources are tradeable (`is_tradeable = 1` in `resourcedefs`): 16 goods and 12 DNA strains.
`Machinery Parts` is `resource_id` 10. The name-to-id map does not need to be hardcoded — the
page's own `<select>` carries it, so the settings can name goods the way the game does.

### Embargoes

Embargoed nations are filtered out server-side (`backend_buyermarketplace.php:270`), so they
never reach the monitor.

### Reaching the alliance roster without side effects

`myalliance.php` must never be fetched. `backend_myalliance.php:232` sets
`alliance_messages_last_checked = NOW()` on every load, which would destroy the alliance-message
alerting the monitor already does — the same hazard the existing fresh-login workaround in
`ClopClient.snapshot` exists to avoid.

`viewalliance.php` is safe: every mutating branch in `backend_viewalliance.php` is gated on a
`$_POST` field, so a GET only reads, and lines 134-140 list each member's `nation_id`. It does
**not** default to your own alliance, though: line 11 tests `$mysql['alliance_id']`, which is
populated from `$_POST` only, so a bare GET falls through to `(int)$_GET['alliance_id']` and
looks up alliance 0. The id has to be supplied.

Read-only hops obtain it:

- **Own `nation_id`** — usually free. `header.php:186-192` renders a `switchnation_id` selector
  whose selected option is `$_SESSION['nation_id']`, and the header is already on the `index.php`
  the monitor fetches every poll. But `header.php:182` renders that selector **only when the
  account has more than one nation**; a single-nation account gets `<li><a>Name</a></li>` with no
  id at all. In that case fall back to `empireoverview.php`, which renders a
  `switchnation_id` button carrying the `nation_id` of every nation on the account whatever the
  count (`empireoverview.php:36`), and whose backend contains no `INSERT`, `UPDATE`, or `DELETE`
  at all. Exactly one button means exactly one nation, so no name matching is needed.
- **Own `alliance_id`** — `viewnation.php?nation_id=<mine>` renders
  `viewalliance.php?alliance_id=N` (`viewnation.php:23`). `backend_viewnation.php` contains no
  `INSERT`, `UPDATE`, or `DELETE`. (It does merge `$_POST` into `$_GET` at line 3, but no
  mutating query exists in the file for that merge to reach.)

Only three pages link an alliance by id — `alliances.php`, `viewnation.php`, and
`viewuser.php` — and of those only `viewnation.php` can be reached from what the monitor already
knows, which is why the route goes through the nation rather than the user.

## Design

### Settings

A new `market` section in `settings.json`, following the `reports.ignore` convention that a
leading `#` switches an entry off:

```json
"market": {
  "_goods_help": "A good whose key starts with # is switched off: delete the # to watch that good's buyer's market. friends alerts on buyers on your friends list; alliance alerts on buyers in your alliance; a buyer who is both satisfies either. always and never are nation-name patterns that override both, matched like reports.ignore: case-insensitive, anywhere in the name, % standing for any run of characters. never beats always.",
  "goods": {
    "# Apples":          {"friends": true, "alliance": true, "always": [], "never": []},
    "# Machinery Parts": {"friends": true, "alliance": true, "always": [], "never": []},
    "# Oil":             {"friends": true, "alliance": true, "always": [], "never": []}
  }
}
```

All 28 tradeable goods ship present and commented out, each showing all four knobs, so the
options are visible in the file rather than only in the README. Watching a good means deleting
two characters from its key.

`alerts.market_orders` joins the four existing category toggles, so the whole feature can be
muted without re-commenting the goods you had switched on.

An absent `market` section records `market.goods` in `defaults_used` and watches nothing,
matching how every other optional section behaves.

### Resolving alliance membership

`friends` and `alliance` are independent checks, so each buyer is classified on two independent
facts rather than one colour:

- **is_friend** — the buyer's cell is blue. Exact: blue is rendered if and only if the buyer is
  on your friends list.
- **is_ally** — the buyer's `nation_id` appears in your alliance roster. Exact, and unaffected
  by the buyer also being a friend or an enemy.

The roster comes from one GET of `viewalliance.php?alliance_id=<mine>` per poll, with
`alliance_id` resolved through the read-only hops described above and cached until something
re-resolves it. The roster itself is re-read every poll, because members join and leave while
the monitor runs.

> **Amended 2026-08-23:** "cached for the process lifetime" as originally written. A reload that
> leaves at least one good watched now re-runs the preflight, which re-resolves `alliance_id` too.
> A reload that empties the watch list has nothing to resolve, so it clears `alliance_id` instead.

When no roster is fetched — because no watched good sets `alliance: true` — `is_ally` falls back
to the green colour, which is a correct positive and an incomplete negative. Nothing consults it
in that case except the alert label, which is then reporting only what was actually looked up.

**A fetched roster is never empty, and an empty one is a failure.** You are a member of the
alliance you are looking up, so your own nation is always in that member table
(`viewalliance.php:70`). An empty parse therefore means the fetch failed — an error page, an
expired session, drifted markup — not that the alliance is empty. This matters because a fetched
roster is treated as authoritative: an empty-but-successful-looking roster would demote every
genuine ally to a stranger, and the monitor would quietly stop alerting on exactly the case the
feature exists for. Silent degradation is the worst failure mode for a monitor, so the fetch
raises `MonitorError` on an empty result rather than returning it, the way `parse_pending_counts`
already raises when the structure it needs has gone. A nation with **no** alliance is a different
thing entirely: it never fetches at all, and its empty roster is legitimate.

The roster fetch is skipped entirely when no watched good sets `alliance: true` — a settings-level
test, so there is no per-row conditional-fetch logic to reason about. Leaving or joining an
alliance mid-run is not detected on its own.

> **Amended 2026-08-23:** this said "restart the monitor". Switching a good **on** in
> `market.goods` now re-runs the preflight and re-resolves the alliance on the next poll, so a
> restart is no longer the only way — but it is still not detected on its own, which is the part
> that stands. Switching the last good off resolves nothing; it clears the alliance instead.

A nation with no alliance resolves to an empty roster, so `alliance: true` matches nothing. That
is a startup note, not an error.

### Which name the overrides match, and at what level

`always` and `never` match the **nation** name — `n.name`, rendered in the Buyer column
(`backend_buyermarketplace.php:265`) — never the player's username. In CLOP the two are unrelated
strings: on the live game the player `Lacera Viscera` fields the nation `Fish Bucket`, and
`Winter Moon` fields `Green Mountain Republic`. A username written into `always` or `never` cannot
match anything, and fails silently, because "matched nothing" is indistinguishable from "that
nation posted no order".

The three settings also resolve at two different levels, which matters when a player owns more than
one nation:

| Setting | Resolved from | Level |
|---|---|---|
| `friends` | the blue span, which the game derives from `friends.friendee` — a **user_id** | player |
| `alliance` | `nation_id` membership of the roster, which lists every nation of every member user | player |
| `always` / `never` | the nation-name text of the one order | nation |

So `friends` and `alliance` cover every nation a player owns, while a `never` entry silences only
the nation it names. This is not a defect — naming a nation is what the Buyer column lets you do —
but it is the one asymmetry in the feature, and the README states it.

### Deciding on one order

For each order under a watched good, in this order:

1. the nation name matches a `never` pattern → skip
2. the nation name matches an `always` pattern → alert
3. `friends` is true and the buyer is a friend → alert
4. `alliance` is true and the buyer is in your alliance → alert
5. otherwise → skip

`never` beating `always` makes the blacklist absolute, which is what "never alert on" says.
`always` beating both relation checks means a named nation alerts even when it is an enemy, and
even with `friends` and `alliance` both false — so those two false plus a populated `always`
reads as "only these nations, whoever they are".

Because the two facts are resolved independently, a buyer who is both a friend and an ally
satisfies either check, and turning one off does not hide them from the other.

Nation-name patterns use the existing `matches_any_pattern` matching rule, unchanged, so the
settings file has one pattern convention rather than two.

### Fetching

Skipped entirely when no good is uncommented: zero extra requests, exactly as an unset
`fourchan.thread_url` costs nothing.

**Startup preflight**, mirroring the existing 4chan thread preflight. After login:

1. GET `buyermarketplace.php` and read the `<select>` to resolve every watched good name to its
   `resource_id`. A name that does not resolve is a fatal startup error that names the offending
   entry — a typo must not silently watch nothing. Resolving at startup rather than per poll means
   the game gaining or losing a good later cannot kill a monitor that is already running.

   > **Amended 2026-08-23:** the preflight also runs when a reload changes the watched goods, where
   > an unresolvable name is a refused reload rather than a fatal error. The startup path here is
   > unchanged.

2. If any watched good sets `alliance: true`, resolve the own-nation and own-alliance ids as
   above and report the alliance found, or that there is none.

**Per poll**, when at least one good is watched:

1. GET `viewalliance.php?alliance_id=<mine>` for the roster, if any watched good sets
   `alliance: true`.
2. GET `buyermarketplace.php` for the current `token_buyermarketplace`.
3. For each watched good, POST `buyermarketplace.php` with `token_buyermarketplace`,
   `mode` (empty, meaning resources), and `resource_id`. Take the next token from each
   response and spend it on the next POST, and check each response before moving on: no order
   rows *and* no `Nobody wants to buy that item.` banner means the page refused the request,
   which raises rather than being counted as "no buyers". Per *A refused POST is
   indistinguishable from an empty market* above, the token chain cannot catch this by
   itself — the refusal hands back a valid token and the loop would heal over it.

Cost per poll is `2 + N` requests for N watched goods, or `1 + N` when no good checks alliance.

This stays read-only. Every mutating branch in `backend_buyermarketplace.php` is gated on a
`offer`, `remove`, `sellone`, `sellall`, or `sellamount` POST field; none is ever sent, so the
POST only filters and displays. The two alliance-resolution pages are read-only on GET as
established above, and `myalliance.php` is never touched.

`ClopClient.snapshot` gains an `include_market` parameter. The re-read that follows a dismissed
dialog passes `False`, so acknowledging a popup does not replay the market requests merely to
refresh the message counts.

### Parsing

A new `HTMLParser` walks the deals table and, per row, reads price and amount from the first
two cells and, from the Buyer cell's `viewnation.php?nation_id=` anchor:

- **nation_id** — from the anchor's `href`, used for the roster membership test;
- **colour** — the class of the first `<span>` inside that anchor: `text-info` friend,
  `text-danger` enemy, `text-success` alliance, no span at all means no relation;
- **nation name** — that span's text, or, for an unstyled buyer with no span, the anchor's text
  up to the first `(`, which is where the region begins.

Scoping to the anchor is what avoids the colour trap described above.

A second small parser reads the member `nation_id`s out of the `viewalliance.php` page, and a
third reads the selected `switchnation_id` option out of the shared header.

### Alerting

Every poll while the order is pending, with no state and nothing persisted — the same
behaviour as the unread-message counters, and for the same reason: a standing buy order is a
current fact, not an event, and an order you were told about once three days ago is one you
have forgotten. `Snapshot` carries the orders as a non-persisted field, like `report_rows`.

One alert block per good that has matches, so several watched goods stay readable in a single
dialog. Each buyer is labelled with every relation that is true of them, so a friend who is also
an ally reads as both:

```
Buy orders for Machinery Parts:
  Some Nation (friend) wants 12 at 5,000 bits each
  Ally Nation (alliance) wants 3 at 4,800 bits each
  Both Nation (friend, alliance) wants 40 at 4,500 bits each
  Named Nation (no relation) wants 5 at 6,000 bits each
https://4clop.org/buyermarketplace.php
```

`(no relation)` appears only for a buyer matched by an `always` pattern.

**Good names are matched case-insensitively when orders are paired back to their settings.**
Two spellings of one good name are in play: the preflight resolves what the settings file says
against the game's own selector and keeps the **game's** canonical spelling, which is what each
fetched order is stamped with, while the watch list keeps the **user's** spelling, which is what
the alert header displays. Comparing those exactly would mean that a settings file saying
`machinery parts` fetched orders correctly, stamped them `Machinery Parts`, matched nothing, and
alerted on nothing — with no error and no log line, because an order for an unwatched good is
dropped silently by design. The two are therefore compared case-insensitively on purpose. This is
the one place in the feature where that is not obvious, since the drop it guards is meant to be
silent.

### Failures

A market page, roster page, or nation page that cannot be read raises `MonitorError` and reaches
the existing per-poll failure dialog — "still running, retries in N seconds". A bad `market`
section, like any other bad settings, and an unresolvable good name are fatal startup errors.
Neither path is new.

> **Amended 2026-08-23:** still fatal at startup. Reached through a reload instead, both are
> refused reloads that warn and leave the previous settings in force.

Three cases are worth naming because each would otherwise be a *silent* under-report rather than
a visible failure, which is the outcome this design most wants to avoid:

- a POST the page refused, detected by the missing empty-market banner (above);
- a roster page that parsed to no member nations, which cannot happen for an alliance you are in;
- an `alliance_id` that was never resolved, which is a different fact from being in no alliance
  and must not be answered with an empty roster.

For the last of these, `ClopClient.alliance_id` is deliberately tri-state: `None` not resolved,
`0` resolved to no alliance, `N` resolved to alliance N. The preflight assigns it and
`market_goods` together only once every step has succeeded, so a preflight that raises part-way
leaves the client untouched rather than watching goods with alliance detection quietly degraded
back to the green-colour heuristic the roster exists to replace.

## Testing

`test_clop_monitor.py`, against synthetic HTML as the existing tests do, never contacting the
hosted game:

- **settings** — commented-out keys watch nothing; uncommenting switches a good on; all 28 goods
  are present in the shipped example and all are commented out; malformed sections are rejected;
- **row parsing** — friend, enemy, alliance, and unstyled rows; nation_id extraction; the colour
  trap, proving a plain row is not read as an enemy because of its `text-danger` price cell;
- **relation resolution** — a blue buyer whose nation_id is in the roster is both friend and
  ally; a blue buyer absent from the roster is friend only; a red buyer in the roster is an ally;
  a green buyer is an ally;
- **the decision matrix** — `never` beating `always`; `always` beating both checks being false;
  `friends: false` alone still alerting an ally who is also a friend, which is the case that
  motivated splitting the checks; `alliance: false` alone still alerting that same buyer as a
  friend; an enemy silent unless named in `always`;
- **request shape** — the token from each response is spent on the next POST; the roster is not
  fetched when no watched good sets `alliance: true`; `include_market=False` on the
  post-dismissal re-read issues no market requests; `myalliance.php` is never requested.

Then, as a live sanity check, run against `Machinery Parts` on the hosted game, which had both
a friend and an alliance order pending when this was specified, and report what is actually
there.

## Rejected

- **Alerting only on change, with an order identity of (good, nation, price).** Fewer dialogs,
  but a standing order is exactly the thing you want to be reminded of, and being told once
  then never again defeats the purpose.
- **A single combined `friends_and_allies` check.** Simpler, and it sidesteps the colour
  ambiguity entirely, but it cannot express "only my alliance" or "only my friends".
- **Deriving alliance membership from the green colour alone.** Zero extra requests, but
  `alliance: true` would then miss an ally you had also friended and an ally you had marked an
  enemy, because the game renders only the higher-precedence colour.
- **Treating a blue buyer as satisfying both checks.** Also zero extra requests, and misses
  nothing, but `friends: false, alliance: true` would then alert on friends outside your
  alliance.
- **Fetching the roster lazily, only when an ambiguous blue or red row appears.** Cheaper in the
  common case, but it puts a conditional network fetch in the middle of row classification. The
  settings-level skip gets most of the benefit for none of the complexity.
- **Reading the roster from `myalliance.php`.** It is the natural page, and it marks alliance
  messages read as a side effect.
- **A shorthand where a good may be just `true` instead of an object.** A shorter file, but the
  knobs would only be discoverable in the README.
- **A flat list of goods with the four knobs as separate maps.** Splits one good's configuration
  across five places in the file.
- **Exact whole-name matching for `always` and `never`.** Safer against over-matching, but it
  would put a second, different matching rule in a settings file that already has one.
- **Weapons and armor markets.** `buyermarketplace.php?mode=weapons` and `mode=armor` work the
  same way and could be added later with a second goods map. Out of scope here.
