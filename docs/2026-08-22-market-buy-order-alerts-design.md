# Buyer's-marketplace order alerts — design

**Date:** 2026-08-22
**Status:** approved, not yet implemented

Alert when somepony you care about has a pending buy order for a good you care about, so you
can sell into it before somepony else does.

## The problem

The monitor watches messages, news, reports, and a 4chan thread. It does not watch the market.
A friend or alliance member who posts a buy order for a good you are sitting on is an
opportunity that expires the moment another player fills it, and nothing tells you about it.

The game already marks these buyers visually: on the buyer's marketplace, a buyer who is your
friend renders blue and one in your alliance renders green. This feature reads that same
signal.

## What the game does

Read from `clop/buyermarketplace.php` and `clop/backend/backend_buyermarketplace.php` in the
main CLOP repository.

### The order list only exists on POST

The deals query is inside `if ($_POST)` (`backend_buyermarketplace.php:264`). A plain GET of
`buyermarketplace.php` renders the form and nothing else. Reading orders therefore requires a
POST carrying `resource_id`.

### The POST needs a rotating CSRF token

`backend_buyermarketplace.php:52` rejects a POST whose `token_buyermarketplace` field does not
equal `$_SESSION['token_buyermarketplace']`, and a rejected POST falls into `if (!$errors)` so
no orders are returned. Line 55 then regenerates the token on *every* POST, and the freshly
rendered form carries the new value. Tokens therefore have to be chained: read one from the
current page, spend it on the next POST, and take the replacement out of that response.

### The colours

`buyermarketplace.php:63-93`, in this precedence order:

| Buyer | Bootstrap class | Colour |
|---|---|---|
| Friend (`friends` table) | `text-info` | blue |
| Enemy (`enemies` table) | `text-danger` | red |
| Same alliance | `text-success` | green |
| Anyone else | *(no span)* | plain |

Friend is tested before alliance, so a friend who is also an ally renders blue, not green.
The friend and enemy lists are loaded in the same POST that returns the orders
(`backend_buyermarketplace.php:125-138`), so no extra request is needed to resolve relations.

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

## Design

### Settings

A new `market` section in `settings.json`, following the `reports.ignore` convention that a
leading `#` switches an entry off:

```json
"market": {
  "_goods_help": "A good whose key starts with # is switched off: delete the # to watch that good's buyer's market. friends_and_allies alerts on blue (friend) and green (same-alliance) buyers. always and never are nation-name patterns that override it, matched like reports.ignore: case-insensitive, anywhere in the name, % standing for any run of characters. never beats always.",
  "goods": {
    "# Apples":          {"friends_and_allies": true, "always": [], "never": []},
    "# Machinery Parts": {"friends_and_allies": true, "always": [], "never": []},
    "# Oil":             {"friends_and_allies": true, "always": [], "never": []}
  }
}
```

All 28 tradeable goods ship present and commented out, each showing all three knobs, so the
options are visible in the file rather than only in the README. Watching a good means deleting
two characters from its key.

`alerts.market_orders` joins the four existing category toggles, so the whole feature can be
muted without re-commenting the goods you had switched on.

An absent `market` section records `market.goods` in `defaults_used` and watches nothing,
matching how every other optional section behaves.

### Deciding on one order

For each order under a watched good, in this order:

1. the nation name matches a `never` pattern → skip
2. the nation name matches an `always` pattern → alert
3. `friends_and_allies` is true and the buyer is blue or green → alert
4. otherwise → skip

`never` beating `always` makes the blacklist absolute, which is what "never alert on" says.
`always` beating the relation check means a named nation alerts even when it is an enemy, and
even when `friends_and_allies` is false — so `friends_and_allies: false` with a populated
`always` reads as "only these nations, whoever they are".

Nation-name patterns use the existing `report_is_ignored` matching rule, unchanged, so the
settings file has one pattern convention rather than two.

### Fetching

Skipped entirely when no good is uncommented: zero extra requests, exactly as an unset
`fourchan.thread_url` costs nothing.

**Startup preflight**, mirroring the existing 4chan thread preflight. After login, GET
`buyermarketplace.php` and read the `<select>` to resolve every watched good name to its
`resource_id`. A name that does not resolve is a fatal startup error that names the offending
entry — a typo must not silently watch nothing. Resolving once at startup means the game
gaining or losing a good later cannot kill a monitor that is already running.

**Per poll**, when at least one good is watched:

1. GET `buyermarketplace.php` for the current `token_buyermarketplace`.
2. For each watched good, POST `buyermarketplace.php` with `token_buyermarketplace`,
   `mode` (empty, meaning resources), and `resource_id`. Take the next token from each
   response and spend it on the next POST.

Cost is `1 + N` requests per poll for N watched goods.

This stays read-only. Every mutating branch in the backend is gated on a `offer`, `remove`,
`sellone`, `sellall`, or `sellamount` POST field; none is ever sent, so the POST only filters
and displays.

`ClopClient.snapshot` gains an `include_market` parameter. The re-read that follows a dismissed
dialog passes `False`, so acknowledging a popup does not replay N market requests merely to
refresh the message counts.

### Parsing

A new `HTMLParser` walks the deals table and, per row, reads price and amount from the first
two cells and, from the Buyer cell's `viewnation.php?nation_id=` anchor:

- **relation** — the class of the first `<span>` inside that anchor: `text-info` friend,
  `text-success` alliance, `text-danger` enemy, no span at all means neutral;
- **nation name** — that span's text, or, for an unstyled neutral buyer with no span, the
  anchor's text up to the first `(`, which is where the region begins.

Scoping to the anchor is what avoids the colour trap described above.

### Alerting

Every poll while the order is pending, with no state and nothing persisted — the same
behaviour as the unread-message counters, and for the same reason: a standing buy order is a
current fact, not an event, and an order you were told about once three days ago is one you
have forgotten. `Snapshot` carries the orders as a non-persisted field, like `report_rows`.

One alert block per good that has matches, so several watched goods stay readable in a single
dialog:

```
Buy orders for Machinery Parts:
  Some Nation (friend) wants 12 at 5,000 bits each
  Ally Nation (alliance) wants 3 at 4,800 bits each
https://4clop.org/buyermarketplace.php
```

### Failures

A market page that cannot be read raises `MonitorError` and reaches the existing per-poll
failure dialog — "still running, retries in N seconds". A bad `market` section, like any other
bad settings, is a fatal startup error. Neither path is new.

## Testing

`test_clop_monitor.py`, against synthetic HTML as the existing tests do, never contacting the
hosted game:

- settings parsing: commented-out keys watch nothing, uncommenting switches a good on, all 28
  goods are present in the shipped example and all are commented out, malformed sections are
  rejected;
- row parsing: friend, alliance, enemy, and neutral rows; the colour trap, proving a plain
  neutral row is not read as an enemy because of its `text-danger` price cell;
- the decision matrix: `never` beating `always`, `always` beating `friends_and_allies: false`,
  `friends_and_allies: false` alone silencing a friend, an enemy silent unless named in
  `always`;
- token chaining: the token from each response is the one spent on the next POST;
- `include_market=False` on the post-dismissal re-read issues no market requests.

Then, as a live sanity check, run against `Machinery Parts` on the hosted game, which had both
a friend and an alliance order pending when this was specified, and report what is actually
there.

## Rejected

- **Alerting only on change, with an order identity of (good, nation, price).** Fewer dialogs,
  but a standing order is exactly the thing you want to be reminded of, and being told once
  then never again defeats the purpose.
- **A shorthand where a good may be just `true` instead of an object.** A shorter file, but the
  three knobs would only be discoverable in the README.
- **A flat list of goods with `friends_and_allies`, `always`, and `never` as separate maps.**
  Splits one good's configuration across four places in the file.
- **Exact whole-name matching for `always` and `never`.** Safer against over-matching, but it
  would put a second, different matching rule in a settings file that already has one.
- **Weapons and armor markets.** `buyermarketplace.php?mode=weapons` and `mode=armor` work the
  same way and could be added later with a second goods map. Out of scope here.
