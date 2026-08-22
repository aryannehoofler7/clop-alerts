# Buyer's-Marketplace Order Alerts Implementation Plan

> **Status (2026-08-23): completed build record, point-in-time.** This plan describes how the
> feature was built and is kept for that history; it is not maintained as a description of how the
> monitor behaves now. Where it conflicts with `docs/2026-08-22-market-buy-order-alerts-design.md`
> or `docs/2026-08-23-hot-reload-settings-design.md`, those are correct and this is superseded.
> Hot-reloading `settings.json` in particular changed several statements here about the preflight
> running only at startup and about needing a restart to re-resolve the alliance.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Alert every poll when somepony on your friends list, or in your alliance, has a pending buy order for a good you have switched on in `settings.json`.

**Architecture:** Extend the existing single-file `clop_monitor.py`. New settings dataclasses hold a per-good watch list; new `HTMLParser` subclasses read the buyer's-marketplace deal rows, the alliance roster, and the form fields needed to reach them; `ClopClient` gains a preflight that resolves good names to `resource_id`s and the account's own `alliance_id`, plus a per-poll fetch that chains the page's rotating CSRF token across one POST per watched good. `build_alerts` gains a branch that applies the per-good decision rules. No new state is persisted.

**Tech Stack:** Python 3.9+, standard library only (`html.parser`, `urllib`, `dataclasses`, `unittest`). No third-party packages. Windows-first.

**Design spec:** `docs/2026-08-22-market-buy-order-alerts-design.md`. Read it before starting.

---

## As built

This plan is a point-in-time record of what was *planned* on 2026-08-22, kept for history. Every
task below was completed, and the feature is implemented, reviewed, and live-verified. Where the
shipped code and this text disagree, **the code is right** — the divergences below were deliberate
improvements made during implementation, and the plan has not been rewritten to hide them.

- **Muting stops the work, not just the alert.** The plan calls `market_preflight(settings.alerts.market_goods)` ungated; the shipped code calls `goods_to_watch(settings.alerts)`, so `alerts.market_orders: false` skips the market fetching entirely instead of doing it and discarding the result.
- **Stricter settings loading.** The plan's loader has neither the unknown-knob rejection nor the case-clash rejection that shipped (and that `README.md:367-370` documents).
- **`parse_alliance_id` became `parse_alliance_link`**, returning `(id, name)`, which removed a duplicated link-walk.
- **`_market_orders` gained a refusal guard.** It now raises when a POST returns neither order rows nor the game's "Nobody wants to buy that item." banner; without it, a refused POST would be silently reported as "this good has no buy orders".
- **Task 11's status-line fragment** carried a trailing `", "` that would have produced malformed output; the separator was moved.
- **Two task details were simply wrong** and were corrected as encountered: Task 3's stated test count (13) and Task 10's `EMPIRE_OVERVIEW` fixture assumption. See the task reports.

---

## Background you need

The monitor is a read-only scraper of the hosted PHP game at `https://4clop.org`. Facts about the game's pages that this plan depends on, all verified against the source in `D:\Koan\clop\clop`:

- `buyermarketplace.php` renders its order table **only for a POST** (`backend_buyermarketplace.php:264`). A GET renders the form and nothing else.
- That POST must carry `token_buyermarketplace` matching the session's copy (`backend_buyermarketplace.php:52`), and **the token is regenerated on every POST** (line 55), with the new value rendered into the returned form. Tokens must be chained.
- Buyer colours (`buyermarketplace.php:63-93`): friend → `text-info`, enemy → `text-danger`, same alliance → `text-success`, nobody → no `<span>` at all. Friend is tested **before** alliance, so only one colour is ever rendered.
- **The colour trap:** the price cell is also `text-danger` and the amount cell also `text-success` (`buyermarketplace.php:97-98`). Relation detection must be scoped to the row's `viewnation.php?nation_id=` anchor or every buyer reads as an enemy and an ally at once.
- **Never fetch `myalliance.php`.** `backend_myalliance.php:232` sets `alliance_messages_last_checked = NOW()`, which would destroy the alliance-message alerting the monitor already does.
- `viewalliance.php` is read-only on GET (every mutating branch is `$_POST`-gated) and lists each member nation as a `viewnation.php?nation_id=` link (`viewalliance.php:70`). It needs `?alliance_id=N`: `backend_viewalliance.php:11` reads `$mysql['alliance_id']`, which is populated from `$_POST` only, so a bare GET looks up alliance 0.
- `header.php:186-192` renders a `<select name="switchnation_id">` with the current nation's option marked `selected` — but **only when the account has more than one nation** (`header.php:182`).
- `empireoverview.php:36` renders `<button name="switchnation_id" value="{nation_id}">` for every nation on the account, and `backend_empireoverview.php` contains no `INSERT`/`UPDATE`/`DELETE`.
- `viewnation.php:23` renders `<a href="viewalliance.php?alliance_id=N">`. `backend_viewnation.php` contains no `INSERT`/`UPDATE`/`DELETE`. It does merge `$_POST` into
  `$_GET` at line 3, but there is no mutating query in the file for that merge to reach.
- 28 resources are tradeable; `Machinery Parts` is `resource_id` 10.

**Conventions in this repo you must follow:**

- Comments explain *why*, not *what*. Look at the existing comments in `clop_monitor.py` (e.g. lines 685-687, 950-953) for the register.
- Tests use synthetic HTML and never contact the hosted game. `unittest`, run with `python -m unittest`.
- `settings.example.json` is tracked; `settings.json` is git-ignored. A `#` at the start of a settings entry switches it off, because JSON has no comments.
- Every existing `AlertCategorySettings(...)` call site uses keyword arguments, so fields may be added anywhere. `Snapshot(...)` **is** constructed positionally in tests, so new fields must go **last**.

---

## File Structure

**Modified: `clop_monitor.py`** — everything lands here. This is deliberate: the monitor is a single self-contained script that a non-technical successor copies as a folder and runs with `python .\clop_monitor.py`, and splitting it would need a third module for the shared primitives (`MonitorError`, `normalize_text`, `report_is_ignored`) to avoid a circular import. Do not restructure the file.

**Modified: `test_clop_monitor.py`** — new test classes appended, following the existing class-per-concern layout.

**Modified: `settings.example.json`** — new `market` section, new `alerts.market_orders` key.

**Modified: `README.md`** — new settings documentation.

**Created: nothing.**

---

## Task 1: Settings dataclasses

**Files:**
- Modify: `clop_monitor.py:50-57` (`AlertCategorySettings`)
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`:

```python
class WatchedGoodTests(unittest.TestCase):
    def test_a_watched_good_defaults_to_friends_and_alliance_with_no_overrides(self):
        good = clop_monitor.WatchedGood("Machinery Parts")
        self.assertEqual(good.name, "Machinery Parts")
        self.assertTrue(good.friends)
        self.assertTrue(good.alliance)
        self.assertEqual(good.always, ())
        self.assertEqual(good.never, ())

    def test_alert_categories_default_to_market_on_with_nothing_watched(self):
        settings = clop_monitor.AlertCategorySettings()
        self.assertTrue(settings.market_orders)
        self.assertEqual(settings.market_goods, ())
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.WatchedGoodTests -v`
Expected: FAIL with `AttributeError: module 'clop_monitor' has no attribute 'WatchedGood'`

- [x] **Step 3: Write minimal implementation**

In `clop_monitor.py`, insert immediately **before** `class AlertCategorySettings` (line 50):

```python
@dataclass(frozen=True)
class WatchedGood:
    """One good's buyer's-market watch.

    ``friends`` and ``alliance`` are independent because the game renders only the
    higher-precedence colour, so a buyer who is both must satisfy either check on its own.
    ``always`` and ``never`` are nation-name patterns that override both.
    """

    name: str
    friends: bool = True
    alliance: bool = True
    always: Tuple[str, ...] = ()
    never: Tuple[str, ...] = ()
```

Then add two fields to `AlertCategorySettings`, after `report_ignore`:

```python
    market_orders: bool = True
    #: Goods from market.goods that are switched on, in the order the file lists them.
    market_goods: Tuple[WatchedGood, ...] = ()
```

- [x] **Step 4: Run the whole suite**

Run: `python -m unittest -v`
Expected: PASS, no existing test broken.

- [x] **Step 5: Commit**

```bash
git add clop_monitor.py test_clop_monitor.py
git commit -m "feat: add per-good market watch settings dataclass"
```

---

## Task 2: Generalise the pattern matcher

`always` and `never` reuse the `reports.ignore` matching rule, so the matcher needs a name that is not about reports.

**Files:**
- Modify: `clop_monitor.py:441-451` (`report_is_ignored`)
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`:

```python
class PatternMatchTests(unittest.TestCase):
    def test_a_pattern_matches_anywhere_ignoring_case(self):
        self.assertTrue(clop_monitor.matches_any_pattern("Luna Sueno", ["luna"]))

    def test_a_wildcard_stands_for_any_run_of_characters(self):
        self.assertTrue(clop_monitor.matches_any_pattern("Big Pony Land", ["Big % Land"]))

    def test_no_patterns_match_nothing(self):
        self.assertFalse(clop_monitor.matches_any_pattern("Luna Sueno", []))
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.PatternMatchTests -v`
Expected: FAIL with `AttributeError: module 'clop_monitor' has no attribute 'matches_any_pattern'`

- [x] **Step 3: Write minimal implementation**

Replace `report_is_ignored` in `clop_monitor.py` (lines 441-451) with:

```python
def matches_any_pattern(text: str, patterns: Sequence[str]) -> bool:
    """Whether the text matches any pattern.

    A pattern matches anywhere in the text and ignores case; ``%`` stands for any run of
    characters, so ``Build % completed successfully.`` covers whatever was built. Report
    ignore-patterns and market nation-name patterns share this rule so that the settings file
    has one convention rather than two.
    """
    for pattern in patterns:
        expression = ".*".join(re.escape(part) for part in pattern.split("%"))
        if re.search(expression, text, re.IGNORECASE):
            return True
    return False


def report_is_ignored(message: str, patterns: Sequence[str]) -> bool:
    """Whether a report matches an ignore pattern."""
    return matches_any_pattern(message, patterns)
```

- [x] **Step 4: Run the whole suite**

Run: `python -m unittest -v`
Expected: PASS. The existing `ReportIgnorePatternTests` still pass unchanged.

- [x] **Step 5: Commit**

```bash
git add clop_monitor.py test_clop_monitor.py
git commit -m "refactor: name the pattern matcher for its rule, not its first caller"
```

---

## Task 3: Load the market settings section

**Files:**
- Modify: `clop_monitor.py:150-284` (`load_settings`)
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`:

```python
class MarketSettingsTests(unittest.TestCase):
    def load(self, market):
        value = {"sound": {"wav_path": None}, "market": market}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return load_settings(path)

    def test_a_commented_out_good_is_not_watched(self):
        settings = self.load({"goods": {"# Machinery Parts": {}}})
        self.assertEqual(settings.alerts.market_goods, ())

    def test_uncommenting_a_good_watches_it_with_the_documented_defaults(self):
        settings = self.load({"goods": {"Machinery Parts": {}}})
        self.assertEqual(
            settings.alerts.market_goods,
            (clop_monitor.WatchedGood("Machinery Parts", True, True, (), ()),),
        )

    def test_every_knob_is_loaded(self):
        settings = self.load(
            {
                "goods": {
                    "Oil": {
                        "friends": False,
                        "alliance": True,
                        "always": ["Luna Sueno"],
                        "never": ["Sombra"],
                    }
                }
            }
        )
        self.assertEqual(
            settings.alerts.market_goods,
            (clop_monitor.WatchedGood("Oil", False, True, ("Luna Sueno",), ("Sombra",)),),
        )

    def test_a_commented_out_nation_name_is_dropped(self):
        settings = self.load({"goods": {"Oil": {"always": ["# Luna Sueno", "Sombra"]}}})
        self.assertEqual(settings.alerts.market_goods[0].always, ("Sombra",))

    def test_goods_keep_the_order_the_file_lists_them_in(self):
        settings = self.load({"goods": {"Oil": {}, "Apples": {}, "Copper": {}}})
        self.assertEqual(
            [good.name for good in settings.alerts.market_goods],
            ["Oil", "Apples", "Copper"],
        )

    def test_a_good_whose_value_is_not_an_object_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "Machinery Parts"):
            self.load({"goods": {"Machinery Parts": True}})

    def test_a_non_boolean_knob_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "friends"):
            self.load({"goods": {"Oil": {"friends": "yes"}}})

    def test_a_non_list_name_override_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "never"):
            self.load({"goods": {"Oil": {"never": "Sombra"}}})

    def test_an_empty_name_override_entry_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "always"):
            self.load({"goods": {"Oil": {"always": ["  "]}}})

    def test_a_non_object_goods_section_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "market.goods"):
            self.load({"goods": ["Oil"]})

    def test_an_absent_market_section_is_reported_as_a_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"sound": {"wav_path": None}}), encoding="utf-8")
            settings = load_settings(path)
        self.assertEqual(settings.alerts.market_goods, ())
        self.assertIn("market.goods", settings.defaults_used)

    def test_market_orders_category_can_be_disabled(self):
        value = {"sound": {"wav_path": None}, "alerts": {"market_orders": False}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            settings = load_settings(path)
        self.assertFalse(settings.alerts.market_orders)
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.MarketSettingsTests -v`
Expected: FAIL — `market_goods` is always `()` and no `MonitorError` is raised.

- [x] **Step 3: Write minimal implementation**

In `clop_monitor.py`, add these two module-level helpers immediately **before** `def load_settings` (line 150):

```python
def market_boolean_setting(section: Dict[str, object], good: str, name: str) -> bool:
    result = section.get(name, True)
    if not isinstance(result, bool):
        raise MonitorError(f"Setting {name} for market good {good!r} must be true or false")
    return result


def market_name_patterns(section: Dict[str, object], good: str, name: str) -> Tuple[str, ...]:
    raw = section.get(name, [])
    if not isinstance(raw, list):
        raise MonitorError(
            f"Setting {name} for market good {good!r} must be a list of nation names"
        )
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise MonitorError(
                f"Every {name} entry for market good {good!r} must be a non-empty string"
            )
    # As in reports.ignore, a leading # switches one entry off without deleting it.
    return tuple(
        entry.strip() for entry in raw if not entry.strip().startswith("#")
    )
```

In `load_settings`, add `market_orders` to the `AlertCategorySettings(...)` construction at line 192:

```python
    alerts = AlertCategorySettings(
        user_messages=boolean_setting(alerts_value, "alerts", "user_messages", True),
        alliance_messages=boolean_setting(alerts_value, "alerts", "alliance_messages", True),
        news=boolean_setting(alerts_value, "alerts", "news", True),
        reports=boolean_setting(alerts_value, "alerts", "reports", True),
        market_orders=boolean_setting(alerts_value, "alerts", "market_orders", True),
    )
```

Then, immediately **after** the `alerts = replace(alerts, report_ignore=report_ignore)` line (line 259), insert:

```python
    market_value = value.get("market", {})
    if not isinstance(market_value, dict):
        raise MonitorError("The market setting must be a JSON object")
    market_goods: List[WatchedGood] = []
    if "goods" not in market_value:
        defaults_used.append("market.goods")
    else:
        raw_goods = market_value["goods"]
        if not isinstance(raw_goods, dict):
            raise MonitorError(
                "Setting market.goods must be a JSON object keyed by good name"
            )
        for raw_name, raw_good in raw_goods.items():
            name = raw_name.strip()
            # JSON has no comments, so a leading # switches a good off where you can still
            # see it; watching one means deleting two characters.
            if name.startswith("#"):
                continue
            if not name:
                raise MonitorError("Every market.goods key must be a non-empty good name")
            if not isinstance(raw_good, dict):
                raise MonitorError(
                    f"Settings for market good {name!r} must be a JSON object"
                )
            market_goods.append(
                WatchedGood(
                    name=name,
                    friends=market_boolean_setting(raw_good, name, "friends"),
                    alliance=market_boolean_setting(raw_good, name, "alliance"),
                    always=market_name_patterns(raw_good, name, "always"),
                    never=market_name_patterns(raw_good, name, "never"),
                )
            )
    alerts = replace(alerts, market_goods=tuple(market_goods))
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_clop_monitor.MarketSettingsTests -v`
Expected: PASS (13 tests)

Run: `python -m unittest -v`
Expected: PASS. `test_startup_message_names_only_the_omitted_settings` and `test_complete_settings_file_reports_nothing` may now fail because `market.goods` joined the defaults list — if so, update those two tests to include `market.goods` in the expected names and add a `market` section to the complete-settings fixture.

- [x] **Step 5: Commit**

```bash
git add clop_monitor.py test_clop_monitor.py
git commit -m "feat: load the per-good market watch settings"
```

---

## Task 4: Ship all 28 goods commented out

**Files:**
- Modify: `settings.example.json`
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`:

```python
#: Every tradeable good in the game (resourcedefs.is_tradeable = 1), in the order the
#: buyer's-marketplace selector lists them: alphabetical, with the DNA strains last.
TRADEABLE_GOODS = [
    "Apples",
    "Cider",
    "Coffee",
    "Composites",
    "Copper",
    "Drugs",
    "Gasoline",
    "Gems",
    "Machinery Parts",
    "Oil",
    "Pies",
    "Plastics",
    "Precision Parts",
    "Toys",
    "Tungsten",
    "Vehicle Parts",
    "DNA - Central Burrozil",
    "DNA - Central Przewalskia",
    "DNA - Central Saddle Arabia",
    "DNA - Central Zebrica",
    "DNA - North Burrozil",
    "DNA - North Przewalskia",
    "DNA - North Saddle Arabia",
    "DNA - North Zebrica",
    "DNA - South Burrozil",
    "DNA - South Przewalskia",
    "DNA - South Saddle Arabia",
    "DNA - South Zebrica",
]


class ShippedMarketGoodsTests(unittest.TestCase):
    def example(self):
        return json.loads(Path("settings.example.json").read_text(encoding="utf-8-sig"))

    def test_every_tradeable_good_ships_commented_out(self):
        goods = self.example()["market"]["goods"]
        self.assertEqual(list(goods), [f"# {name}" for name in TRADEABLE_GOODS])

    def test_every_shipped_good_shows_all_four_knobs(self):
        for key, good in self.example()["market"]["goods"].items():
            with self.subTest(good=key):
                self.assertEqual(
                    sorted(good), ["alliance", "always", "friends", "never"]
                )
                self.assertTrue(good["friends"])
                self.assertTrue(good["alliance"])
                self.assertEqual(good["always"], [])
                self.assertEqual(good["never"], [])

    def test_shipped_as_is_the_example_watches_nothing(self):
        settings = load_settings(Path("settings.example.json"))
        self.assertEqual(settings.alerts.market_goods, ())
        self.assertTrue(settings.alerts.market_orders)

    def test_uncommenting_a_shipped_good_switches_it_on(self):
        value = self.example()
        value["market"]["goods"] = {
            (key[2:] if key == "# Machinery Parts" else key): good
            for key, good in value["market"]["goods"].items()
        }
        # The bundled WAV is reached by a path relative to the settings file, which a temp
        # directory has no copy of; the sound is irrelevant here.
        value["sound"]["wav_path"] = None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            settings = load_settings(path)
        self.assertEqual(
            [good.name for good in settings.alerts.market_goods], ["Machinery Parts"]
        )
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.ShippedMarketGoodsTests -v`
Expected: FAIL with `KeyError: 'market'`

- [x] **Step 3: Write the settings example**

Add `"market_orders": true` to the `alerts` object in `settings.example.json`, then add this `market` section after the `reports` section:

```json
  "market": {
    "_goods_help": "A good whose key starts with # is switched off: delete the # to watch that good's buyer's market, and the monitor will alert every poll while a matching order is pending. friends alerts on buyers on your friends list (blue in game); alliance alerts on buyers in your alliance (green in game); a buyer who is both satisfies either. always and never are nation-name patterns that override both, matched like reports.ignore: case-insensitive, anywhere in the name, % standing for any run of characters. never beats always, and always beats both friends and alliance. An always or never entry starting with # is switched off too.",
    "goods": {
      "# Apples":                     {"friends": true, "alliance": true, "always": [], "never": []},
      "# Cider":                      {"friends": true, "alliance": true, "always": [], "never": []},
      "# Coffee":                     {"friends": true, "alliance": true, "always": [], "never": []},
      "# Composites":                 {"friends": true, "alliance": true, "always": [], "never": []},
      "# Copper":                     {"friends": true, "alliance": true, "always": [], "never": []},
      "# Drugs":                      {"friends": true, "alliance": true, "always": [], "never": []},
      "# Gasoline":                   {"friends": true, "alliance": true, "always": [], "never": []},
      "# Gems":                       {"friends": true, "alliance": true, "always": [], "never": []},
      "# Machinery Parts":            {"friends": true, "alliance": true, "always": [], "never": []},
      "# Oil":                        {"friends": true, "alliance": true, "always": [], "never": []},
      "# Pies":                       {"friends": true, "alliance": true, "always": [], "never": []},
      "# Plastics":                   {"friends": true, "alliance": true, "always": [], "never": []},
      "# Precision Parts":            {"friends": true, "alliance": true, "always": [], "never": []},
      "# Toys":                       {"friends": true, "alliance": true, "always": [], "never": []},
      "# Tungsten":                   {"friends": true, "alliance": true, "always": [], "never": []},
      "# Vehicle Parts":              {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - Central Burrozil":     {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - Central Przewalskia":  {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - Central Saddle Arabia":{"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - Central Zebrica":      {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - North Burrozil":       {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - North Przewalskia":    {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - North Saddle Arabia":  {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - North Zebrica":        {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - South Burrozil":       {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - South Przewalskia":    {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - South Saddle Arabia":  {"friends": true, "alliance": true, "always": [], "never": []},
      "# DNA - South Zebrica":        {"friends": true, "alliance": true, "always": [], "never": []}
    }
  },
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_clop_monitor.ShippedMarketGoodsTests -v`
Expected: PASS (4 tests)

Run: `python -m unittest -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add settings.example.json test_clop_monitor.py
git commit -m "feat: ship every tradeable good commented out in the settings example"
```

---

## Task 5: Parse the buyer's-marketplace order rows

**Files:**
- Modify: `clop_monitor.py` (new parser after `NewsTableParser`, ends line 367)
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`. The HTML mirrors `buyermarketplace.php:95-101` exactly, including the `text-danger` price cell and `text-success` amount cell that make the colour trap real:

```python
def market_row(nation_id, name, colour, amount, price, region="Saddle Arabia"):
    """One deals-table row shaped exactly as buyermarketplace.php renders it."""
    if colour:
        shown_name = f'<span class="{colour}">{name}</span>'
        shown_region = f'<span class="{colour}">{region}</span>'
    else:
        shown_name, shown_region = name, region
    return f"""
<tr><td><div class="row">
  <div class="col-md-1"><p class="text-danger">{price}</p></div>
  <div class="col-md-1"><p class="text-success">{amount}</p></div>
  <div class="col-md-5"><p><a href="viewnation.php?nation_id={nation_id}">{shown_name}
    (<img src="images/icons/Oil.png"/>{shown_region})</a></p></div>
  <div class="col-md-5"><div class="row"><div class="col-xs-6">
    <form action="buyermarketplace.php" method="post">
    <input type="hidden" name="resource_id" value="10"/>
    <input type="submit" name="sellone" value="Sell One" class="btn btn-primary"/>
    </form></div></div></div>
</div></td></tr>
"""


MARKET_TABLE_HEAD = """
<table class="table table-striped table-bordered">
<thead><tr><td><div class="row">
  <div class="col-md-1">Offering</div>
  <div class="col-md-1">Amount</div>
  <div class="col-md-5">Buyer</div>
  <div class="col-md-5">Actions</div>
</div></td></tr></thead><tbody>
"""


def market_page(*rows):
    return MARKET_TABLE_HEAD + "".join(rows) + "</tbody></table>"


class MarketRowParsingTests(unittest.TestCase):
    def parse(self, html, roster=None):
        return clop_monitor.parse_market_orders(html, "Machinery Parts", roster)

    def test_a_friend_row_is_read_as_a_friend(self):
        orders = self.parse(market_page(market_row(42, "Luna Sueno", "text-info", 12, "5,000")))
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order.good, "Machinery Parts")
        self.assertEqual(order.nation_id, 42)
        self.assertEqual(order.nation_name, "Luna Sueno")
        self.assertEqual(order.amount, 12)
        self.assertEqual(order.price, 5000)
        self.assertTrue(order.is_friend)
        self.assertFalse(order.is_enemy)

    def test_an_alliance_row_is_read_as_an_ally(self):
        orders = self.parse(market_page(market_row(7, "Ally Nation", "text-success", 3, "4,800")))
        self.assertTrue(orders[0].is_ally)
        self.assertFalse(orders[0].is_friend)

    def test_an_enemy_row_is_read_as_an_enemy(self):
        orders = self.parse(market_page(market_row(9, "Sombra", "text-danger", 1, "100")))
        self.assertTrue(orders[0].is_enemy)
        self.assertFalse(orders[0].is_friend)

    def test_an_unstyled_row_has_no_relation_despite_the_coloured_price_and_amount_cells(self):
        # The colour trap: the price cell is text-danger and the amount cell text-success in
        # every row, so a page-wide class match would call this stranger an enemy and an ally.
        orders = self.parse(market_page(market_row(5, "Some Stranger", "", 2, "9,000")))
        self.assertEqual(orders[0].nation_name, "Some Stranger")
        self.assertFalse(orders[0].is_friend)
        self.assertFalse(orders[0].is_enemy)
        self.assertFalse(orders[0].is_ally)

    def test_the_header_row_is_not_an_order(self):
        self.assertEqual(self.parse(market_page()), [])

    def test_every_row_is_returned_in_page_order(self):
        orders = self.parse(
            market_page(
                market_row(1, "First", "text-info", 1, "9,000"),
                market_row(2, "Second", "text-success", 2, "8,000"),
                market_row(3, "Third", "", 3, "7,000"),
            )
        )
        self.assertEqual([order.nation_name for order in orders], ["First", "Second", "Third"])

    def test_a_roster_decides_alliance_regardless_of_colour(self):
        html = market_page(
            market_row(42, "Friend And Ally", "text-info", 1, "1,000"),
            market_row(43, "Friend Only", "text-info", 1, "1,000"),
            market_row(44, "Enemy And Ally", "text-danger", 1, "1,000"),
        )
        orders = self.parse(html, roster=frozenset({42, 44}))
        self.assertEqual([order.is_ally for order in orders], [True, False, True])
        self.assertEqual([order.is_friend for order in orders], [True, True, False])

    def test_without_a_roster_only_green_reads_as_an_ally(self):
        html = market_page(
            market_row(42, "Friend", "text-info", 1, "1,000"),
            market_row(7, "Ally", "text-success", 1, "1,000"),
        )
        self.assertEqual([order.is_ally for order in self.parse(html)], [False, True])


class RelationLabelTests(unittest.TestCase):
    def label(self, **flags):
        return clop_monitor.MarketOrder("Oil", 1, "N", 1, 1, **flags).relation_label()

    def test_a_friend_who_is_also_an_ally_is_labelled_as_both(self):
        self.assertEqual(self.label(is_friend=True, is_ally=True), "friend, alliance")

    def test_a_buyer_with_no_relation_says_so(self):
        self.assertEqual(self.label(), "no relation")

    def test_an_enemy_is_named(self):
        self.assertEqual(self.label(is_enemy=True), "enemy")
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.MarketRowParsingTests test_clop_monitor.RelationLabelTests -v`
Expected: FAIL with `AttributeError: module 'clop_monitor' has no attribute 'parse_market_orders'`

- [x] **Step 3: Write minimal implementation**

In `clop_monitor.py`, add this constant beside the other module regexes (after line 35):

```python
HAVE_SUFFIX_RE = re.compile(r"\s*\(Have \d+\)$")
```

Add `FrozenSet` to the `typing` import on line 24:

```python
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple
```

Add the `MarketOrder` dataclass immediately after `class WatchedGood` from Task 1:

```python
@dataclass(frozen=True)
class MarketOrder:
    """One pending buy order on the buyer's marketplace."""

    good: str
    nation_id: int
    nation_name: str
    amount: int
    price: int
    is_friend: bool = False
    is_enemy: bool = False
    is_ally: bool = False

    def relation_label(self) -> str:
        """Every relation that is true of this buyer, for the alert line."""
        labels = []
        if self.is_friend:
            labels.append("friend")
        if self.is_enemy:
            labels.append("enemy")
        if self.is_ally:
            labels.append("alliance")
        return ", ".join(labels) if labels else "no relation"
```

Add these helpers and the parser after `NewsTableParser` (i.e. after line 367):

```python
def nation_id_from_href(href: str) -> Optional[int]:
    """The nation_id of a viewnation.php link, or None for any other link."""
    parsed = urllib.parse.urlsplit(href)
    if parsed.path.rsplit("/", 1)[-1].lower() != "viewnation.php":
        return None
    values = urllib.parse.parse_qs(parsed.query).get("nation_id")
    if not values or not values[0].isdigit():
        return None
    return int(values[0])


def parse_market_number(text: str) -> Optional[int]:
    """A price or amount cell; prices are rendered with thousands separators."""
    digits = text.replace(",", "").strip()
    return int(digits) if digits.isdigit() else None


class BuyerMarketParser(HTMLParser):
    """Rows of the buyer's-marketplace deals table.

    The buyer's colour is taken from the first span inside the row's viewnation link and
    never from colour classes elsewhere in the row: the price cell is text-danger and the
    amount cell text-success in every row, so a page-wide class match would call every buyer
    an enemy and an ally at once.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        #: nation_id, nation name, colour class, amount, price
        self.rows: List[Tuple[int, str, str, int, int]] = []
        self._start_row()

    def _start_row(self) -> None:
        self._numbers: List[str] = []
        self._number_parts: Optional[List[str]] = None
        self._anchor_parts: Optional[List[str]] = None
        self._anchor_text = ""
        self._nation_id: Optional[int] = None
        self._colour = ""
        self._colour_taken = False
        self._span_parts: Optional[List[str]] = None
        self._span_text = ""

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "tr":
            self._start_row()
        elif tag == "div" and "col-md-1" in (attributes.get("class") or "").split():
            self._number_parts = []
        elif tag == "a" and self._nation_id is None:
            nation_id = nation_id_from_href(attributes.get("href") or "")
            if nation_id is not None:
                self._nation_id = nation_id
                self._anchor_parts = []
        elif tag == "span" and self._anchor_parts is not None and not self._colour_taken:
            self._colour_taken = True
            self._colour = (attributes.get("class") or "").strip()
            self._span_parts = []

    def handle_data(self, data: str) -> None:
        if self._number_parts is not None:
            self._number_parts.append(data)
        if self._span_parts is not None:
            self._span_parts.append(data)
        if self._anchor_parts is not None:
            self._anchor_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "div" and self._number_parts is not None:
            self._numbers.append(normalize_text(self._number_parts))
            self._number_parts = None
        elif tag == "span" and self._span_parts is not None:
            self._span_text = normalize_text(self._span_parts)
            self._span_parts = None
        elif tag == "a" and self._anchor_parts is not None:
            self._anchor_text = normalize_text(self._anchor_parts)
            self._anchor_parts = None
        elif tag == "tr":
            self._finish_row()
            self._start_row()

    def _finish_row(self) -> None:
        # The header row has the same two numeric cells but no buyer link, so requiring the
        # link is what keeps it out.
        if self._nation_id is None or len(self._numbers) < 2:
            return
        price = parse_market_number(self._numbers[0])
        amount = parse_market_number(self._numbers[1])
        if price is None or amount is None:
            return
        # An unstyled buyer has no span, so the name is the anchor text up to the region.
        name = (self._span_text or self._anchor_text.split("(")[0]).strip()
        if not name:
            return
        self.rows.append((self._nation_id, name, self._colour, amount, price))


def parse_market_orders(
    html: str, good: str, roster: Optional[FrozenSet[int]]
) -> List[MarketOrder]:
    """Every pending buy order for one good, newest-priced first as the page orders them.

    ``roster`` is the set of nation ids in your alliance, or None when it was not looked up.
    Without it the green colour is the only alliance signal available; with it, membership is
    exact even for a buyer the game painted blue or red instead.
    """
    parser = BuyerMarketParser()
    parser.feed(html)
    orders: List[MarketOrder] = []
    for nation_id, name, colour, amount, price in parser.rows:
        classes = colour.split()
        is_green = "text-success" in classes
        orders.append(
            MarketOrder(
                good=good,
                nation_id=nation_id,
                nation_name=name,
                amount=amount,
                price=price,
                is_friend="text-info" in classes,
                is_enemy="text-danger" in classes,
                is_ally=(nation_id in roster) if roster is not None else is_green,
            )
        )
    return orders
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_clop_monitor.MarketRowParsingTests test_clop_monitor.RelationLabelTests -v`
Expected: PASS (11 tests)

- [x] **Step 5: Commit**

```bash
git add clop_monitor.py test_clop_monitor.py
git commit -m "feat: parse buyer's-marketplace order rows and buyer relations"
```

---

## Task 6: Parse the form fields and pages needed to reach the market

**Files:**
- Modify: `clop_monitor.py` (new parsers after `BuyerMarketParser`)
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`:

```python
MARKET_FORM = """
<form action="buyermarketplace.php" method="post" class="form-inline">
  <input type="hidden" name="token_buyermarketplace" value="abc123"/>
  <input type="hidden" name="mode" value=""/>
  <select name="resource_id" class="form-control">
    <option value=""></option>
    <option value="3">Apples</option>
    <option value="10" selected >Machinery Parts (Have 5)</option>
    <option value="1">Oil</option>
  </select>
</form>
"""

MULTI_NATION_HEADER = """
<li><a><form action="" method="post"><select name="switchnation_id">
<option value="11">First Nation</option>
<option value="12" selected >Second Nation</option>
</select></form></a></li>
"""

EMPIRE_OVERVIEW = """
<table><tr>
<td><form action="overview.php" method="post">
<button name="switchnation_id" type="submit" value="12">Only Nation</button></form></td>
</tr></table>
"""

NATION_PAGE = """
<center><h4>Alliance:
<a href="viewalliance.php?alliance_id=7">The Best Alliance</a></h4></center>
"""

ALLIANCE_PAGE = """
<table>
<tr><td><a href="viewuser.php?user_id=1">somepony</a></td>
<td><a href="viewnation.php?nation_id=12">Mine (<img src="x.png"/>Zebrica)</a>
<a href="viewnation.php?nation_id=13">Also Mine (<img src="x.png"/>Zebrica)</a></td></tr>
<tr><td><a href="viewuser.php?user_id=2">otherpony</a></td>
<td><a href="viewnation.php?nation_id=42">Theirs (<img src="x.png"/>Burrozil)</a></td></tr>
</table>
"""


class MarketFormParsingTests(unittest.TestCase):
    def test_the_csrf_token_is_read_from_the_hidden_field(self):
        self.assertEqual(
            clop_monitor.parse_hidden_field(MARKET_FORM, "token_buyermarketplace"), "abc123"
        )

    def test_an_absent_hidden_field_is_none(self):
        self.assertIsNone(clop_monitor.parse_hidden_field(MARKET_FORM, "token_absent"))

    def test_good_names_map_to_resource_ids_without_the_have_suffix(self):
        self.assertEqual(
            clop_monitor.parse_good_ids(MARKET_FORM),
            {"Apples": 3, "Machinery Parts": 10, "Oil": 1},
        )

    def test_the_current_nation_comes_from_the_selected_header_option(self):
        self.assertEqual(clop_monitor.parse_current_nation_id(MULTI_NATION_HEADER), 12)

    def test_a_header_without_a_switcher_has_no_nation_id(self):
        self.assertIsNone(clop_monitor.parse_current_nation_id(AUTHENTICATED_HEADER))

    def test_the_empire_overview_lists_every_nation_button(self):
        self.assertEqual(clop_monitor.parse_empire_nation_ids(EMPIRE_OVERVIEW), [12])

    def test_a_nation_page_yields_its_alliance_id(self):
        self.assertEqual(clop_monitor.parse_alliance_id(NATION_PAGE), 7)

    def test_a_nation_page_without_an_alliance_link_yields_none(self):
        self.assertIsNone(clop_monitor.parse_alliance_id("<h4>Alliance: none</h4>"))

    def test_the_alliance_page_yields_every_member_nation(self):
        self.assertEqual(
            clop_monitor.parse_alliance_nation_ids(ALLIANCE_PAGE),
            frozenset({12, 13, 42}),
        )
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.MarketFormParsingTests -v`
Expected: FAIL with `AttributeError: module 'clop_monitor' has no attribute 'parse_hidden_field'`

- [x] **Step 3: Write minimal implementation**

Add to `clop_monitor.py` after `parse_market_orders`:

```python
class HiddenFieldParser(HTMLParser):
    """The value of the first input with a given name."""

    def __init__(self, name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.name = name.lower()
        self.value: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "input" or self.value is not None:
            return
        attributes = dict(attrs)
        if (attributes.get("name") or "").lower() == self.name:
            self.value = attributes.get("value") or ""


def parse_hidden_field(html: str, name: str) -> Optional[str]:
    parser = HiddenFieldParser(name)
    parser.feed(html)
    return parser.value


class SelectOptionParser(HTMLParser):
    """The options of one named select, each with its value, text, and selected flag."""

    def __init__(self, select_name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.select_name = select_name.lower()
        self.options: List[Tuple[str, str, bool]] = []
        self._active = False
        self._value: Optional[str] = None
        self._selected = False
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "select":
            self._active = (attributes.get("name") or "").lower() == self.select_name
        elif tag == "option" and self._active:
            self._value = attributes.get("value") or ""
            self._selected = "selected" in attributes
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._value is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "option" and self._value is not None:
            self.options.append((self._value, normalize_text(self._parts), self._selected))
            self._value = None
        elif tag == "select":
            self._active = False


def parse_select_options(html: str, select_name: str) -> List[Tuple[str, str, bool]]:
    parser = SelectOptionParser(select_name)
    parser.feed(html)
    return parser.options


def parse_good_ids(html: str) -> Dict[str, int]:
    """Good name to resource_id, from the buyer's-marketplace selector.

    The option text carries a "(Have N)" suffix for a good you hold some of, because
    backend_buyermarketplace.php builds the option label that way; it is stripped so the
    settings can name goods the way the game does.
    """
    goods: Dict[str, int] = {}
    for value, text, _ in parse_select_options(html, "resource_id"):
        name = HAVE_SUFFIX_RE.sub("", text).strip()
        if value.isdigit() and name:
            goods[name] = int(value)
    return goods


def parse_current_nation_id(html: str) -> Optional[int]:
    """The active nation, from the header's nation switcher.

    header.php renders that switcher only for an account with more than one nation, so a
    single-nation account yields None and the id has to come from the empire overview.
    """
    for value, _, selected in parse_select_options(html, "switchnation_id"):
        if selected and value.isdigit():
            return int(value)
    return None


class ButtonValueParser(HTMLParser):
    """The values of every button with a given name."""

    def __init__(self, name: str) -> None:
        super().__init__(convert_charrefs=True)
        self.name = name.lower()
        self.values: List[str] = []

    def handle_starttag(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "button":
            return
        attributes = dict(attrs)
        if (attributes.get("name") or "").lower() == self.name:
            self.values.append(attributes.get("value") or "")


def parse_empire_nation_ids(html: str) -> List[int]:
    """Every nation on the account, from the empire overview's switch buttons."""
    parser = ButtonValueParser("switchnation_id")
    parser.feed(html)
    return [int(value) for value in parser.values if value.isdigit()]


def parse_alliance_id(html: str) -> Optional[int]:
    """The alliance linked from a nation page, or None when there is no such link."""
    parser = LinkTextParser()
    parser.feed(html)
    for href, _ in parser.links:
        if _path_from_href(href) != "viewalliance.php":
            continue
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("alliance_id")
        if values and values[0].isdigit():
            return int(values[0])
    return None


def parse_alliance_nation_ids(html: str) -> FrozenSet[int]:
    """Every member nation on an alliance page.

    viewalliance.php links a nation only from its member table, so every viewnation link on
    the page is a member of that alliance.
    """
    parser = LinkTextParser()
    parser.feed(html)
    return frozenset(
        nation_id
        for href, _ in parser.links
        if (nation_id := nation_id_from_href(href)) is not None
    )
```

> **Python 3.8 note:** the walrus in the final comprehension needs 3.8+, which the README already requires (3.9+). Keep it.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_clop_monitor.MarketFormParsingTests -v`
Expected: PASS (9 tests)

- [x] **Step 5: Commit**

```bash
git add clop_monitor.py test_clop_monitor.py
git commit -m "feat: parse the form fields and pages that reach the market and roster"
```

---

## Task 7: The alert decision for one order

**Files:**
- Modify: `clop_monitor.py` (new function before `build_alerts`, line 735)
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`:

```python
class MarketDecisionTests(unittest.TestCase):
    """never beats always; always beats both relation checks; the two checks are independent."""

    def order(self, name="Luna Sueno", **flags):
        return clop_monitor.MarketOrder("Oil", 1, name, 5, 1000, **flags)

    def decide(self, order, **knobs):
        return clop_monitor.market_order_alerts(order, clop_monitor.WatchedGood("Oil", **knobs))

    def test_a_friend_alerts_when_friends_is_on(self):
        self.assertTrue(self.decide(self.order(is_friend=True), friends=True, alliance=False))

    def test_a_friend_is_silent_when_friends_is_off(self):
        self.assertFalse(self.decide(self.order(is_friend=True), friends=False, alliance=False))

    def test_an_ally_alerts_when_alliance_is_on(self):
        self.assertTrue(self.decide(self.order(is_ally=True), friends=False, alliance=True))

    def test_an_ally_is_silent_when_alliance_is_off(self):
        self.assertFalse(self.decide(self.order(is_ally=True), friends=True, alliance=False))

    def test_a_friend_who_is_also_an_ally_alerts_on_the_alliance_check_alone(self):
        # The case that motivated splitting the checks: the game paints this buyer blue, so
        # a colour-only reading would have hidden them from "only my alliance".
        order = self.order(is_friend=True, is_ally=True)
        self.assertTrue(self.decide(order, friends=False, alliance=True))

    def test_a_friend_who_is_also_an_ally_alerts_on_the_friend_check_alone(self):
        order = self.order(is_friend=True, is_ally=True)
        self.assertTrue(self.decide(order, friends=True, alliance=False))

    def test_a_stranger_is_silent(self):
        self.assertFalse(self.decide(self.order(), friends=True, alliance=True))

    def test_an_enemy_is_silent(self):
        self.assertFalse(self.decide(self.order(is_enemy=True), friends=True, alliance=True))

    def test_an_always_name_alerts_with_both_checks_off(self):
        self.assertTrue(
            self.decide(self.order(), friends=False, alliance=False, always=("Luna %",))
        )

    def test_an_always_name_alerts_even_for_an_enemy(self):
        self.assertTrue(
            self.decide(self.order(is_enemy=True), friends=False, alliance=False,
                        always=("Luna Sueno",))
        )

    def test_a_never_name_silences_a_friend(self):
        self.assertFalse(
            self.decide(self.order(is_friend=True), friends=True, never=("Luna %",))
        )

    def test_never_beats_always(self):
        self.assertFalse(
            self.decide(self.order(), always=("Luna Sueno",), never=("Luna Sueno",))
        )

    def test_name_patterns_ignore_case(self):
        self.assertFalse(self.decide(self.order(is_friend=True), never=("luna sueno",)))
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.MarketDecisionTests -v`
Expected: FAIL with `AttributeError: module 'clop_monitor' has no attribute 'market_order_alerts'`

- [x] **Step 3: Write minimal implementation**

Add to `clop_monitor.py`, immediately before `def build_alerts` (line 735):

```python
def market_order_alerts(order: MarketOrder, good: WatchedGood) -> bool:
    """Whether one buy order raises an alert under this good's settings.

    never is absolute, which is what "never alert on" says; always then beats both relation
    checks, so both of them off with a populated always reads as "only these nations,
    whoever they are". The two relation checks are independent, so a buyer who is both a
    friend and an ally satisfies either one on its own.
    """
    if matches_any_pattern(order.nation_name, good.never):
        return False
    if matches_any_pattern(order.nation_name, good.always):
        return True
    if good.friends and order.is_friend:
        return True
    return good.alliance and order.is_ally
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_clop_monitor.MarketDecisionTests -v`
Expected: PASS (13 tests)

- [x] **Step 5: Commit**

```bash
git add clop_monitor.py test_clop_monitor.py
git commit -m "feat: decide which buy orders alert under a good's settings"
```

---

## Task 8: The market alert text

**Files:**
- Modify: `clop_monitor.py:488-497` (`Snapshot`), `clop_monitor.py:735-781` (`build_alerts`)
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`:

```python
class MarketAlertTests(unittest.TestCase):
    def snapshot(self, *orders):
        return Snapshot(0, 0, None, None, None, False, (), tuple(orders))

    def settings(self, *goods):
        return AlertCategorySettings(market_goods=tuple(goods))

    def test_matching_orders_become_one_block_per_good(self):
        current = self.snapshot(
            clop_monitor.MarketOrder("Machinery Parts", 42, "Luna Sueno", 12, 5000,
                                     is_friend=True),
            clop_monitor.MarketOrder("Machinery Parts", 7, "Ally Nation", 3, 4800,
                                     is_ally=True),
        )
        alerts = build_alerts(None, current, self.settings(clop_monitor.WatchedGood("Machinery Parts")))
        self.assertEqual(
            alerts,
            [
                "Buy orders for Machinery Parts:\n"
                "  Luna Sueno (friend) wants 12 at 5,000 bits each\n"
                "  Ally Nation (alliance) wants 3 at 4,800 bits each\n"
                "https://4clop.org/buyermarketplace.php"
            ],
        )

    def test_a_buyer_who_is_both_is_labelled_as_both(self):
        current = self.snapshot(
            clop_monitor.MarketOrder("Oil", 42, "Both Nation", 40, 4500,
                                     is_friend=True, is_ally=True),
        )
        alerts = build_alerts(None, current, self.settings(clop_monitor.WatchedGood("Oil")))
        self.assertIn("Both Nation (friend, alliance) wants 40 at 4,500 bits each", alerts[0])

    def test_a_good_with_no_matching_orders_produces_no_block(self):
        current = self.snapshot(clop_monitor.MarketOrder("Oil", 5, "Stranger", 1, 100))
        self.assertEqual(
            build_alerts(None, current, self.settings(clop_monitor.WatchedGood("Oil"))), []
        )

    def test_each_good_gets_its_own_block(self):
        current = self.snapshot(
            clop_monitor.MarketOrder("Oil", 1, "A", 1, 100, is_friend=True),
            clop_monitor.MarketOrder("Apples", 2, "B", 2, 200, is_friend=True),
        )
        alerts = build_alerts(
            None,
            current,
            self.settings(clop_monitor.WatchedGood("Oil"), clop_monitor.WatchedGood("Apples")),
        )
        self.assertEqual(len(alerts), 2)
        self.assertTrue(alerts[0].startswith("Buy orders for Oil:"))
        self.assertTrue(alerts[1].startswith("Buy orders for Apples:"))

    def test_the_market_category_can_be_disabled(self):
        current = self.snapshot(
            clop_monitor.MarketOrder("Oil", 1, "A", 1, 100, is_friend=True)
        )
        settings = AlertCategorySettings(
            market_orders=False, market_goods=(clop_monitor.WatchedGood("Oil"),)
        )
        self.assertEqual(build_alerts(None, current, settings), [])

    def test_market_orders_alert_on_every_poll_while_pending(self):
        order = clop_monitor.MarketOrder("Oil", 1, "A", 1, 100, is_friend=True)
        settings = self.settings(clop_monitor.WatchedGood("Oil"))
        previous = self.snapshot(order)
        current = self.snapshot(order)
        self.assertEqual(len(build_alerts(previous, current, settings)), 1)

    def test_market_orders_are_not_persisted(self):
        snapshot = self.snapshot(
            clop_monitor.MarketOrder("Oil", 1, "A", 1, 100, is_friend=True)
        )
        self.assertNotIn("market_orders", snapshot.to_json())
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.MarketAlertTests -v`
Expected: FAIL with `TypeError: __init__() takes from 4 to 8 positional arguments but 9 were given`

- [x] **Step 3: Write minimal implementation**

Add a field to `Snapshot`, **after** `report_rows` (line 497) so the positional construction in the existing tests keeps working:

```python
    #: Every watched good's pending buy orders this poll. Not persisted: alerting is
    #: every-poll, so there is no baseline to keep.
    market_orders: Tuple[MarketOrder, ...] = ()
```

Add this branch to `build_alerts`, immediately **after** the reports branch and before the 4chan branch (i.e. after line 765):

```python
    if settings.market_orders:
        for good in settings.market_goods:
            lines = [
                f"  {order.nation_name} ({order.relation_label()}) "
                f"wants {order.amount:,} at {order.price:,} bits each"
                for order in current.market_orders
                if order.good == good.name and market_order_alerts(order, good)
            ]
            if lines:
                alerts.append(
                    f"Buy orders for {good.name}:\n"
                    + "\n".join(lines)
                    + "\nhttps://4clop.org/buyermarketplace.php"
                )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_clop_monitor.MarketAlertTests -v`
Expected: PASS (7 tests)

Run: `python -m unittest -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add clop_monitor.py test_clop_monitor.py
git commit -m "feat: build the buy-order alert blocks"
```

---

## Task 9: Fetch the market and the alliance roster

**Files:**
- Modify: `clop_monitor.py:575-707` (`ClopClient`)
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`:

```python
class MarketFetchTests(unittest.TestCase):
    def client(self, pages, goods=(("Machinery Parts", 10),), alliance_id=None):
        """A client whose _open serves canned pages and records every call."""
        client = ClopClient("https://4clop.org", "user", "password")
        client.market_goods = goods
        client.alliance_id = alliance_id
        calls = []

        def fake_open(path, form=None):
            calls.append((path, form))
            if path == "myalliance.php":
                raise AssertionError("myalliance.php marks alliance messages read")
            if path not in pages:
                raise AssertionError(f"Unexpected path: {path}")
            page = pages[path]
            return page(form) if callable(page) else page

        client._open = fake_open
        return client, calls

    def test_each_post_spends_the_token_from_the_previous_response(self):
        tokens = iter(["token-1", "token-2", "token-3"])

        def form(_form=None):
            return MARKET_FORM.replace("abc123", next(tokens))

        client, calls = self.client(
            {"buyermarketplace.php": form},
            goods=(("Apples", 3), ("Oil", 1)),
        )
        client._market_orders(None)
        posts = [form_data for path, form_data in calls if form_data is not None]
        self.assertEqual(
            [post["token_buyermarketplace"] for post in posts], ["token-1", "token-2"]
        )
        self.assertEqual([post["resource_id"] for post in posts], ["3", "1"])
        self.assertEqual([post["mode"] for post in posts], ["", ""])

    def test_orders_are_tagged_with_the_good_that_was_requested(self):
        page = MARKET_FORM + market_page(market_row(42, "Luna Sueno", "text-info", 12, "5,000"))
        client, _ = self.client({"buyermarketplace.php": page})
        orders = client._market_orders(None)
        self.assertEqual([order.good for order in orders], ["Machinery Parts"])
        self.assertEqual(orders[0].nation_name, "Luna Sueno")

    def test_a_missing_token_is_a_monitor_error(self):
        client, _ = self.client({"buyermarketplace.php": "<form></form>"})
        with self.assertRaisesRegex(MonitorError, "CSRF token"):
            client._market_orders(None)

    def test_the_roster_comes_from_viewalliance_never_myalliance(self):
        client, calls = self.client(
            {"viewalliance.php?alliance_id=7": ALLIANCE_PAGE}, alliance_id=7
        )
        self.assertEqual(client._alliance_roster(), frozenset({12, 13, 42}))
        self.assertEqual([path for path, _ in calls], ["viewalliance.php?alliance_id=7"])

    def test_no_alliance_yields_an_empty_roster_without_a_request(self):
        client, calls = self.client({}, alliance_id=0)
        self.assertEqual(client._alliance_roster(), frozenset())
        self.assertEqual(calls, [])

    def test_an_empty_fetched_roster_is_a_failure_not_a_membership_of_none(self):
        # Your own nation is always on your own alliance page, so nothing there means the
        # fetch failed. Returning it would demote every ally to a stranger and silently
        # stop the alerts this feature exists for.
        client, _ = self.client(
            {"viewalliance.php?alliance_id=7": "<h3>Alliance</h3><table></table>"},
            alliance_id=7,
        )
        with self.assertRaisesRegex(MonitorError, "listed no member nations"):
            client._alliance_roster()

    def test_a_snapshot_without_market_goods_makes_no_market_requests(self):
        navigation = AUTHENTICATED_HEADER.replace("(7)", "").replace("(2)", "")
        client, calls = self.client(
            {
                "index.php": navigation,
                "news.php?page=1": navigation + "<h3>News</h3>No news yet.",
                "reports.php": navigation + "<h3>Reports</h3><table></table>",
            },
            goods=(),
        )
        self.assertEqual(client.snapshot().market_orders, ())
        self.assertNotIn("buyermarketplace.php", [path for path, _ in calls])

    def test_include_market_false_skips_the_market_requests(self):
        navigation = AUTHENTICATED_HEADER.replace("(7)", "").replace("(2)", "")
        client, calls = self.client(
            {
                "index.php": navigation,
                "news.php?page=1": navigation + "<h3>News</h3>No news yet.",
                "reports.php": navigation + "<h3>Reports</h3><table></table>",
            }
        )
        client.snapshot(include_market=False)
        self.assertNotIn("buyermarketplace.php", [path for path, _ in calls])

    def test_a_snapshot_with_goods_fetches_the_roster_and_the_orders(self):
        navigation = AUTHENTICATED_HEADER.replace("(7)", "").replace("(2)", "")
        page = MARKET_FORM + market_page(market_row(42, "Theirs", "text-danger", 12, "5,000"))
        client, calls = self.client(
            {
                "index.php": navigation,
                "news.php?page=1": navigation + "<h3>News</h3>No news yet.",
                "reports.php": navigation + "<h3>Reports</h3><table></table>",
                "buyermarketplace.php": page,
                "viewalliance.php?alliance_id=7": ALLIANCE_PAGE,
            },
            alliance_id=7,
        )
        snapshot = client.snapshot()
        # Nation 42 is in the roster, so the red enemy colour does not hide their membership.
        self.assertEqual(len(snapshot.market_orders), 1)
        self.assertTrue(snapshot.market_orders[0].is_ally)
        self.assertTrue(snapshot.market_orders[0].is_enemy)
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.MarketFetchTests -v`
Expected: FAIL with `AttributeError: 'ClopClient' object has no attribute '_market_orders'`

- [x] **Step 3: Write minimal implementation**

In `ClopClient.__init__` (after line 593), add:

```python
        #: (good name, resource_id) pairs, filled in by market_preflight.
        self.market_goods: Tuple[Tuple[str, int], ...] = ()
        #: The account's own alliance, or None when no watched good checks alliance.
        self.alliance_id: Optional[int] = None
```

Add these methods to `ClopClient`, after `_latest_fourchan_post` (i.e. after line 663):

```python
    def _alliance_roster(self) -> FrozenSet[int]:
        """The nation ids in this account's alliance.

        Read from viewalliance.php. myalliance.php is never used: it sets
        alliance_messages_last_checked on every load, which would silently mark the alliance
        messages this monitor exists to report.
        """
        if not self.alliance_id:
            return frozenset()
        roster = parse_alliance_nation_ids(
            self._open(f"viewalliance.php?alliance_id={self.alliance_id}")
        )
        # You are a member of the alliance you looked up, so your own nation is always in
        # that table: an empty parse means the fetch failed, not that the alliance is empty.
        # A fetched roster is treated as authoritative, so returning an empty one would
        # demote every ally to a stranger and silently stop the alerts this feature exists
        # for.
        if not roster:
            raise MonitorError(
                f"The alliance page for alliance {self.alliance_id} listed no member nations"
            )
        return roster

    def _market_orders(self, roster: Optional[FrozenSet[int]]) -> Tuple[MarketOrder, ...]:
        """Every pending buy order for the watched goods.

        The order table exists only for a POST, and the page regenerates its CSRF token on
        every POST, so each response carries the token the next one has to spend.
        """
        if not self.market_goods:
            return ()
        html = self._open("buyermarketplace.php")
        orders: List[MarketOrder] = []
        for good, resource_id in self.market_goods:
            token = parse_hidden_field(html, "token_buyermarketplace")
            if not token:
                raise MonitorError("The buyer's marketplace form has no CSRF token")
            html = self._open(
                "buyermarketplace.php",
                {
                    "token_buyermarketplace": token,
                    "mode": "",
                    "resource_id": str(resource_id),
                },
            )
            orders.extend(parse_market_orders(html, good, roster))
        return tuple(orders)
```

Change the `snapshot` signature (line 674) to `def snapshot(self, include_market: bool = True) -> Snapshot:` and, immediately before the `return Snapshot(` at line 699, insert:

```python
        market_orders: Tuple[MarketOrder, ...] = ()
        if include_market and self.market_goods:
            roster = self._alliance_roster() if self.alliance_id is not None else None
            market_orders = self._market_orders(roster)
```

Then add `market_orders=market_orders,` as the last argument of that `Snapshot(...)` construction.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_clop_monitor.MarketFetchTests -v`
Expected: PASS (8 tests)

- [x] **Step 5: Commit**

```bash
git add clop_monitor.py test_clop_monitor.py
git commit -m "feat: fetch buy orders and the alliance roster read-only"
```

---

## Task 10: The startup preflight

**Files:**
- Modify: `clop_monitor.py` (`ClopClient`)
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`:

```python
class MarketPreflightTests(unittest.TestCase):
    def client(self, pages):
        client = ClopClient("https://4clop.org", "user", "password")
        calls = []

        def fake_open(path, form=None):
            calls.append((path, form))
            if path == "myalliance.php":
                raise AssertionError("myalliance.php marks alliance messages read")
            if path not in pages:
                raise AssertionError(f"Unexpected path: {path}")
            return pages[path]

        client._open = fake_open
        return client, calls

    def test_nothing_watched_makes_no_requests(self):
        client, calls = self.client({})
        self.assertIsNone(client.market_preflight(()))
        self.assertEqual(calls, [])

    def test_good_names_resolve_to_resource_ids(self):
        client, _ = self.client({"buyermarketplace.php": MARKET_FORM})
        message = client.market_preflight(
            (clop_monitor.WatchedGood("Machinery Parts", alliance=False),)
        )
        self.assertEqual(client.market_goods, (("Machinery Parts", 10),))
        self.assertIn("Machinery Parts", message)

    def test_good_names_match_regardless_of_case(self):
        client, _ = self.client({"buyermarketplace.php": MARKET_FORM})
        client.market_preflight((clop_monitor.WatchedGood("machinery PARTS", alliance=False),))
        self.assertEqual(client.market_goods, (("Machinery Parts", 10),))

    def test_an_unknown_good_is_fatal_and_names_itself(self):
        client, _ = self.client({"buyermarketplace.php": MARKET_FORM})
        with self.assertRaisesRegex(MonitorError, "Machinry Parts"):
            client.market_preflight((clop_monitor.WatchedGood("Machinry Parts", alliance=False),))

    def test_the_alliance_is_resolved_through_the_header_switcher(self):
        client, calls = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": MULTI_NATION_HEADER,
                "viewnation.php?nation_id=12": NATION_PAGE,
            }
        )
        message = client.market_preflight((clop_monitor.WatchedGood("Oil"),))
        self.assertEqual(client.alliance_id, 7)
        self.assertIn("The Best Alliance", message)
        self.assertNotIn("empireoverview.php", [path for path, _ in calls])

    def test_a_single_nation_account_falls_back_to_the_empire_overview(self):
        client, calls = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": AUTHENTICATED_HEADER,
                "empireoverview.php": EMPIRE_OVERVIEW,
                "viewnation.php?nation_id=12": NATION_PAGE,
            }
        )
        client.market_preflight((clop_monitor.WatchedGood("Oil"),))
        self.assertEqual(client.alliance_id, 7)
        self.assertIn("empireoverview.php", [path for path, _ in calls])

    def test_an_unidentifiable_nation_is_a_monitor_error(self):
        client, _ = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": AUTHENTICATED_HEADER,
                "empireoverview.php": EMPIRE_OVERVIEW.replace('value="12"', 'value=""'),
            }
        )
        with self.assertRaisesRegex(MonitorError, "which nation is active"):
            client.market_preflight((clop_monitor.WatchedGood("Oil"),))

    def test_no_alliance_is_reported_not_an_error(self):
        client, _ = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": MULTI_NATION_HEADER,
                "viewnation.php?nation_id=12":
                    '<h4>Alliance: <a href="viewalliance.php?alliance_id=0">None</a></h4>',
            }
        )
        message = client.market_preflight((clop_monitor.WatchedGood("Oil"),))
        self.assertEqual(client.alliance_id, 0)
        self.assertIn("no alliance", message)

    def test_the_alliance_is_not_resolved_when_no_good_checks_it(self):
        client, calls = self.client({"buyermarketplace.php": MARKET_FORM})
        client.market_preflight((clop_monitor.WatchedGood("Oil", alliance=False),))
        self.assertIsNone(client.alliance_id)
        self.assertEqual([path for path, _ in calls], ["buyermarketplace.php"])
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.MarketPreflightTests -v`
Expected: FAIL with `AttributeError: 'ClopClient' object has no attribute 'market_preflight'`

- [x] **Step 3: Write minimal implementation**

Add to `ClopClient`, after `_market_orders`:

```python
    def _own_nation_id(self, navigation_html: str) -> int:
        """The active nation's id.

        The header carries it in the nation switcher, but header.php renders that switcher
        only for an account with more than one nation, so a single-nation account is read
        from the empire overview instead, where exactly one button means exactly one nation.
        """
        nation_id = parse_current_nation_id(navigation_html)
        if nation_id is not None:
            return nation_id
        nation_ids = parse_empire_nation_ids(self._open("empireoverview.php"))
        if len(nation_ids) != 1:
            raise MonitorError(
                "Could not identify which nation is active: the header has no nation "
                f"switcher and the empire overview lists {len(nation_ids)} nations"
            )
        return nation_ids[0]

    def market_preflight(self, goods: Sequence[WatchedGood]) -> Optional[str]:
        """Resolve watched good names and, if needed, the account's own alliance.

        Run once at startup so that a mistyped good name stops the monitor instead of
        silently watching nothing, and so that the game gaining or losing a good later
        cannot kill a monitor that is already running.
        """
        if not goods:
            return None
        available = parse_good_ids(self._open("buyermarketplace.php"))
        if not available:
            raise MonitorError("The buyer's marketplace listed no tradeable goods")
        by_lowercase = {name.lower(): (name, value) for name, value in available.items()}
        resolved: List[Tuple[str, int]] = []
        unknown: List[str] = []
        for good in goods:
            match = by_lowercase.get(good.name.lower())
            if match is None:
                unknown.append(good.name)
            else:
                resolved.append(match)
        if unknown:
            raise MonitorError(
                "These market.goods are not tradeable goods: "
                + ", ".join(sorted(unknown))
                + ". The tradeable goods are: "
                + ", ".join(sorted(available))
            )
        # The game's spelling is kept, not the settings file's, so the startup message names
        # the good the way the game does. build_alerts pairs orders back to their watch entry
        # case-insensitively precisely because of this; do not "fix" that by storing the
        # user's spelling here instead.
        self.market_goods = tuple(resolved)
        watching = ", ".join(name for name, _ in resolved)
        if not any(good.alliance for good in goods):
            return f"Market preflight passed; watching {watching} (friends only)."
        nation_id = self._own_nation_id(self._open("index.php"))
        nation_html = self._open(f"viewnation.php?nation_id={nation_id}")
        alliance_id = parse_alliance_id(nation_html)
        if alliance_id is None:
            raise MonitorError(f"Could not read the alliance of nation {nation_id}")
        self.alliance_id = alliance_id
        if not alliance_id:
            return (
                f"Market preflight passed; watching {watching}. This nation has "
                "no alliance, so the alliance check will never match."
            )
        alliance_name = ""
        for href, text in _links(nation_html):
            if _path_from_href(href) == "viewalliance.php":
                alliance_name = text
                break
        return (
            f"Market preflight passed; watching {watching}; "
            f"alliance is {alliance_name} (#{alliance_id})."
        )
```

Add this small helper beside `parse_alliance_id`, and use it there too so the link walk is written once:

```python
def _links(html: str) -> List[Tuple[str, str]]:
    parser = LinkTextParser()
    parser.feed(html)
    return parser.links
```

Refactor `parse_alliance_id` and `parse_alliance_nation_ids` to call `_links(html)` instead of constructing `LinkTextParser` themselves.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest test_clop_monitor.MarketPreflightTests -v`
Expected: PASS (9 tests)

Run: `python -m unittest -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add clop_monitor.py test_clop_monitor.py
git commit -m "feat: preflight watched good names and the account's alliance"
```

---

## Task 11: Wire it into the polling loop

**Files:**
- Modify: `clop_monitor.py:934-964` (`check_and_notify`), `clop_monitor.py:1077-1111` (`main`)
- Test: `test_clop_monitor.py`

- [x] **Step 1: Write the failing test**

Append to `test_clop_monitor.py`:

```python
class MarketDuringAnAlertTests(unittest.TestCase):
    def test_the_refresh_after_a_dismissal_skips_the_market(self):
        """Dismissing a dialog re-reads the counts; replaying N market POSTs to do that
        would cost a request per watched good for information the refresh does not use."""
        calls = []

        class FakeClient:
            def snapshot(self, include_market=True):
                calls.append(include_market)
                return Snapshot(1, 0, None, None, None, False, (), ())

        class FakeNotifier:
            def notify(self, message):
                del message
                return True

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            check_and_notify(FakeClient(), None, FakeNotifier(), state, persist_state=False)
        self.assertEqual(calls, [True, False])
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_clop_monitor.MarketDuringAnAlertTests -v`
Expected: FAIL — `calls` is `[True, True]`.

- [x] **Step 3: Write minimal implementation**

In `check_and_notify`, change the refresh call (line 953) from `refreshed = client.snapshot()` to:

```python
            # The market is deliberately not re-read: its alerting is every-poll, so the next
            # poll reports whatever is still pending, and refetching would cost one request
            # per watched good for a value this refresh does not use.
            refreshed = client.snapshot(include_market=False)
```

In `main`, replace the `client.login()` line (line 1079) with:

```python
        client.login()
        market_message = client.market_preflight(settings.alerts.market_goods)
        if market_message is not None:
            print(market_message, flush=True)
```

Then extend the per-poll status line (lines 1102-1111) by adding this before the closing `flush=True`:

```python
                    f"market_orders={len(current.market_orders)}, "
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m unittest -v`
Expected: PASS, whole suite.

- [x] **Step 5: Verify the monitor still starts with nothing watched**

Run: `python .\clop_monitor.py --settings settings.example.json --test-notification`
Expected: a popup appears; no market requests are made (the example watches nothing).

- [x] **Step 6: Commit**

```bash
git add clop_monitor.py test_clop_monitor.py
git commit -m "feat: run the market preflight and check on every poll"
```

---

## Task 12: Document it in the README

**Files:**
- Modify: `README.md`

- [x] **Step 1: Update the settings example block**

In the `## Settings` section, add `"market_orders": true` to the `alerts` object in the sample JSON and add the `market` section after `reports`, abbreviated to three goods with a note that all 28 ship:

```json
  "market": {
    "goods": {
      "# Machinery Parts": {"friends": true, "alliance": true, "always": [], "never": []},
      "# Oil":             {"friends": true, "alliance": true, "always": [], "never": []},
      "# Pies":            {"friends": true, "alliance": true, "always": [], "never": []}
    }
  }
```

- [x] **Step 2: Add the feature to the bullet list at the top of the README**

Insert after the reports bullet:

```markdown
- watches the buyer's marketplace for each good you switch on, and alerts while any friend or
  alliance member has a pending order for it;
```

- [x] **Step 3: Add a `### Watching the buyer's marketplace` section after `### Ignoring routine reports`**

It must cover, in prose matching the README's existing register:

- All 28 tradeable goods ship commented out; delete the `#` from a key to watch that good.
- `friends` and `alliance` are separate checks, and a buyer who is both satisfies either. Explain why this needed the alliance roster: the game paints only one colour per buyer and tests friend before alliance, so `alliance: true` read from colour alone would miss an ally you had also friended.
- `always` and `never` are nation-name patterns using the same rule as `reports.ignore` — case-insensitive, matching anywhere, `%` for any run of characters — and an entry starting with `#` is switched off. `never` beats `always`; `always` beats both checks, so both false plus a populated `always` means "only these nations".
- Alerts repeat every poll while an order is pending, unlike news and reports, because a standing order is a current fact rather than an event. Nothing about them is written to `.state/`.
- The request cost: one extra GET for the roster plus one POST per watched good, per poll. Skipped entirely when nothing is watched, and the roster is skipped when no watched good sets `alliance: true`.
- The monitor reads the alliance roster from `viewalliance.php`, never `myalliance.php`, so it never marks alliance messages read.
- Your `alliance_id` is resolved once at startup; joining or leaving an alliance while the monitor runs needs a restart.
- A good name that is not a tradeable good stops the monitor at startup and names the offending entry, rather than silently watching nothing. Names are matched case-insensitively.

- [x] **Step 4: Update the `## Failures` section**

Add the unresolvable good name and an unidentifiable active nation to the list of things that stop the monitor.

- [x] **Step 5: Verify the README's own example still parses**

Run: `python -m unittest test_clop_monitor.ShippedMarketGoodsTests -v`
Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: document the buyer's-marketplace order alerts"
```

---

## Task 13: Live sanity check against the hosted game

This is the check the user asked for. It contacts the real game, so it is the last task and it is manual.

**Files:**
- Modify: none (temporary `settings.json` only, which is git-ignored)

- [x] **Step 1: Switch Machinery Parts on in the private settings**

If `settings.json` does not exist, copy the example: `Copy-Item .\settings.example.json .\settings.json`. Then delete the `# ` from the `"# Machinery Parts"` key so it reads `"Machinery Parts"`.

- [x] **Step 2: Run one poll**

Run: `python .\clop_monitor.py --once --no-desktop-notifications`

Expected on stdout: a `Market preflight passed; watching Machinery Parts; alliance is ... (#N).` line, then a poll line ending `market_orders=<count>`, and, if any order matches, a `Buy orders for Machinery Parts:` block naming each buyer and their relation.

- [x] **Step 3: Compare against the game**

Open `https://4clop.org/buyermarketplace.php` in a browser, select **Machinery Parts**, and check that:

- every blue or green buyer in the table appears in the alert block, and
- no plain-coloured buyer appears, and
- the amounts and prices match the table.

The spec records that both a friend order and an alliance order were pending when this was designed. **If neither is pending now, that is not a failure of the code** — orders get filled. In that case pick another good that does have a coloured buyer, switch it on the same way, and repeat. Report to the user exactly what was found, including if nothing coloured was pending anywhere.

- [x] **Step 4: Confirm the read-only claim**

Re-run with the same settings and confirm the game state is unchanged: your funds are the same, no new report appeared on `reports.php`, and the alliance badge in the header still shows the same unread count (proving `myalliance.php` was not touched).

- [x] **Step 5: Report the result**

Report to the user: the preflight line, the alert block verbatim, and whether it matched the browser. Do not commit `settings.json` — it is git-ignored and personal.

---

## Notes for the implementer

- **Do not re-enable or touch `warcalc` / `huge-ovipositor`.** It is intentionally HTTP 410.
- **Never send `offer`, `remove`, `sellone`, `sellall`, or `sellamount`** in a market POST. Those are the five fields that make `backend_buyermarketplace.php` change game state; the monitor sends only `token_buyermarketplace`, `mode`, and `resource_id`.
- If a test in an earlier task fails after a later task's change, fix it rather than deleting it — the existing suite is the regression net for a project with no other tests.
- The market preflight runs inside `main`'s `try:` because it needs a logged-in client, so a bad good name exits **1**, not 2 like the 4chan preflight. That is expected.
