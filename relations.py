#!/usr/bin/env python3
"""When will the Solar Empire or the New Lunar Republic airstrike us?

    forecast = project(se=-120, nlr=76, drift=drift_for("Democracy", "Free Market"))
    forecast.first_strike        # Strike(tick=9, empire='Solar Empire', size=41)
    forecast.hours_until_strike  # 18

A faithful port of the empire-relation pipeline in ``clop/cron/frequent.php``, in the tick's own
execution order, including its off-by-ones and its one live bug. The whole mechanism -- and the
reasoning behind every constant here -- is written up in ``../docs/DEVELOPMENT.md``, section
"Empire relations: decay, jealousy, and exactly when the airstrikes start".

Why this exists rather than reading the number off the page: ``overview.php`` prints a ``(per tick)``
figure beside each relation, but it is a *parallel re-implementation*
(``backend_overview.php:238-358``) that over-predicts Solar Empire recovery below -700, ignores the
airstrike rebound, and is in any case only valid for the next single tick -- decay and jealousy are
step functions of the current values, so the rate changes underneath you. **Multiplying that figure
by a tick count gives the wrong answer.** Iterating the real pipeline does not.

The thing that actually kills a nation is the *sum*: the airstrike query fires on
``se_relation + nlr_relation < -50``, and jealousy subtracts from one relation without adding to the
other, so it drains that sum every tick. Balanced drift survives; lopsided drift does not, however
positive its total.

This module is pure arithmetic -- no network, no page parsing. Feed it the two numbers from
``NationStatus.se.current`` / ``.nlr.current`` in ``nation.py``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from math import ceil, floor
from typing import Dict, List, Optional, Tuple

#: Hours of game time in one tick. ``cron/frequent.php`` applies a fixed amount of everything per
#: run and is scheduled every two hours; see docs/DEVELOPMENT.md "Cadence".
HOURS_PER_TICK = 2

#: The airstrike query's gate (``frequent.php:962``). Tested against se + nlr, not against either.
STRIKE_SUM_THRESHOLD = -50

#: A superpower only joins the strike if its own relation is under this (``:966``, ``:995``).
STRIKE_RELATION_THRESHOLD = -25

RELATION_CAP = 1000
RELATION_FLOOR = -1000

SOLAR_EMPIRE = "Solar Empire"
NEW_LUNAR_REPUBLIC = "New Lunar Republic"

#: Per-tick (se, nlr) from ``nations.government`` (``frequent.php:497-611``). Governments absent
#: from this table -- Loose Despotism, Alicorn Elite, Transponyism -- move relations not at all.
#: Solar Vassal and Lunar Client are special-cased in ``drift_for``: their +60 is clamped straight
#: back to the 1000 cap every tick, so the relation is pinned, not drifting.
GOVERNMENT_DRIFT: Dict[str, Tuple[int, int]] = {
    "Independence": (-3, 6),
    "Decentralization": (-3, 4),
    "Democracy": (-3, 2),
    "Repression": (2, -3),
    "Authoritarianism": (4, -3),
    "Oppression": (6, -3),
}

#: Per-tick (se, nlr) from ``nations.economy`` (``frequent.php:625-644``). Any other economy is 0/0.
ECONOMY_DRIFT: Dict[str, Tuple[int, int]] = {
    "Free Market": (-3, 1),
    "State Controlled": (1, -3),
}

#: Per-tick (se, nlr) *per building owned* (``resourcedefs``, applied at ``frequent.php:305-313``).
#: The Forbidden Research Facility dwarfs every other term in the game.
BUILDING_DRIFT: Dict[str, Tuple[int, int]] = {
    "Sun Worship Center": (1, 0),
    "Moon Worship Center": (0, 1),
    "Drug Farm": (-1, -1),
    "Forbidden Research Facility": (-15, -15),
}


def friendship_decay(relation: int) -> int:
    """"A good friend is hard to keep" -- what a *positive* relation loses this tick.

    ``frequent.php:166-189``. Three stacking tiers at 250 / 400 / 800; 31/tick at the 1000 cap,
    which is why relations settle around 350-400 under a small positive drift instead of climbing.
    """
    if relation <= 250:
        return 0
    lost = floor((relation - 250) / 50)
    if relation > 400:
        lost += floor((relation - 400) / 50)
    if relation > 800:
        lost += floor((relation - 800) / 50)
    return lost


def forgiveness_nlr(relation: int) -> int:
    """"A bad enemy forgets eventually" -- what a *negative* NLR relation regains this tick.

    ``frequent.php:216-227``. Tiers at -450 / -700 / -900, worth +19/tick at the -1000 floor.
    """
    if relation >= -450:
        return 0
    regained = -ceil((relation + 450) / 50)
    if relation < -700:
        regained += -ceil((relation + 700) / 50)
    if relation < -900:
        regained += -ceil((relation + 900) / 50)
    return regained


def forgiveness_se(relation: int) -> int:
    """The Solar Empire's forgiveness -- deliberately *not* a mirror of ``forgiveness_nlr``.

    ``frequent.php:208`` and ``:211`` add the -700 and -900 tiers to ``$serelationeffect`` instead of
    ``$serelationneg``. ``$serelationeffect`` is the friendship-decay accumulator, already consumed
    and written to the database at ``:190``, so both statements are dead code.

    The Solar Empire therefore forgives at roughly half the New Lunar Republic's rate once you are
    past -700: **+11/tick at the floor, against the NLR's +19.** This is modelled, not corrected --
    the game runs the buggy version, and a forecast that assumed the fix would predict a recovery
    that never arrives.
    """
    if relation >= -450:
        return 0
    return -ceil((relation + 450) / 50)


@dataclass(frozen=True)
class Strike:
    """One superpower's airstrike: a forcegroup of ``size`` max-trained pegasi, already landed."""

    tick: int
    empire: str
    size: int

    def describe(self) -> str:
        return f"tick {self.tick}: {self.empire} sends {self.size} pegasi"


@dataclass(frozen=True)
class TickResult:
    se: int
    nlr: int
    strikes: Tuple[Strike, ...]

    @property
    def total(self) -> int:
        """The sum the airstrike query tests. This, not either relation, is the survival number."""
        return self.se + self.nlr


def advance(
    se: int,
    nlr: int,
    drift: Tuple[int, int],
    tick: int = 1,
    ascending: bool = False,
) -> TickResult:
    """Run one cron pass over one nation, in ``frequent.php``'s order.

    ``drift`` is the (se, nlr) your buildings, government, economy and standing actions add each
    tick -- see ``drift_for``. ``ascending`` is ``users.empiremax`` being set, which switches off
    forgiveness, the -1000 floor and the airstrike rebound all at once.
    """
    drift_se, drift_nlr = drift

    # 1. Friendship decay (:166-201), from the start-of-tick values. Applies while ascending too.
    se -= friendship_decay(se)
    nlr -= friendship_decay(nlr)

    # 2. "Forgets eventually" (:203-240) -- the whole block is gated on not ascending.
    if not ascending:
        se += forgiveness_se(se)
        nlr += forgiveness_nlr(nlr)

    # 3. Jealousy (:241-258). Both amounts are computed from the step-2 values *before* either is
    #    applied, so it is simultaneous. Not gated on empiremax. This is the drain on the sum.
    jealousy_of_se = floor(se / 50) if se > 0 else 0     # the NLR resents your SE standing
    jealousy_of_nlr = floor(nlr / 50) if nlr > 0 else 0  # and the SE resents your NLR standing
    se -= jealousy_of_nlr
    nlr -= jealousy_of_se

    # 4. Buildings (:305-313), government (:497-611), economy (:625-644).
    se += drift_se
    nlr += drift_nlr

    # 5. Caps (:728-757). The floor is skipped while ascending -- hate is unbounded on that path.
    se = min(se, RELATION_CAP)
    nlr = min(nlr, RELATION_CAP)
    if not ascending:
        se = max(se, RELATION_FLOOR)
        nlr = max(nlr, RELATION_FLOOR)

    # 6. The airstrike pass (:959-1024). One query over every nation, after they have all ticked.
    #    Both strengths read the same pre-rebound snapshot, so the SE rebound cannot soften the NLR
    #    strike, and both superpowers can fire on the same tick.
    strikes: List[Strike] = []
    if se + nlr < STRIKE_SUM_THRESHOLD:
        if se < STRIKE_RELATION_THRESHOLD:
            size = ceil(-se / 4)
            strikes.append(Strike(tick, SOLAR_EMPIRE, size))
            if not ascending:
                se += size
        if nlr < STRIKE_RELATION_THRESHOLD:
            size = ceil(-nlr / 4)
            strikes.append(Strike(tick, NEW_LUNAR_REPUBLIC, size))
            if not ascending:
                nlr += size

    return TickResult(se, nlr, tuple(strikes))


@dataclass
class Forecast:
    """The result of iterating ``advance`` -- what happens, and when."""

    history: List[TickResult] = field(default_factory=list)
    strikes: List[Strike] = field(default_factory=list)
    ticks_simulated: int = 0

    @property
    def first_strike(self) -> Optional[Strike]:
        """The first airstrike, or ``None`` if none landed inside the horizon."""
        return self.strikes[0] if self.strikes else None

    @property
    def ticks_until_strike(self) -> Optional[int]:
        return self.first_strike.tick if self.first_strike else None

    @property
    def hours_until_strike(self) -> Optional[int]:
        ticks = self.ticks_until_strike
        return ticks * HOURS_PER_TICK if ticks is not None else None


def project(
    se: int,
    nlr: int,
    drift: Tuple[int, int],
    ticks: int = 600,
    ascending: bool = False,
) -> Forecast:
    """Iterate the tick ``ticks`` times from the current relations and report what happens.

    600 ticks is 50 days of game time -- long enough that "no strike in the horizon" means the drift
    is genuinely balanced rather than merely slow.
    """
    forecast = Forecast(ticks_simulated=ticks)
    for tick in range(1, ticks + 1):
        result = advance(se, nlr, drift, tick=tick, ascending=ascending)
        se, nlr = result.se, result.nlr
        forecast.history.append(result)
        forecast.strikes.extend(result.strikes)
    return forecast


def drift_for(
    government: str = "",
    economy: str = "",
    buildings: Optional[Dict[str, int]] = None,
) -> Tuple[int, int]:
    """The (se, nlr) your standing situation adds each tick.

    ``buildings`` maps a building name from ``BUILDING_DRIFT`` to how many you own. Anything not in
    the tables contributes nothing, which is correct: Loose Despotism and most economies really are
    0/0, and most buildings have no ``resourcedefs.se_relation``.

    Solar Vassal and Lunar Client are *not* drift. Their +60/tick is clamped straight back to the
    1000 cap the same tick, so that relation is pinned; only the neglected one moves. Model a patron
    state by holding the patron relation at 1000 rather than by adding 60 here.
    """
    se, nlr = 0, 0
    for source, table in ((government, GOVERNMENT_DRIFT), (economy, ECONOMY_DRIFT)):
        step = table.get(source)
        if step:
            se += step[0]
            nlr += step[1]
    for name, count in (buildings or {}).items():
        step = BUILDING_DRIFT.get(name)
        if step:
            se += step[0] * count
            nlr += step[1] * count
    return se, nlr


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Forecast Solar Empire / New Lunar Republic airstrikes from current relations.",
        epilog="Example: relations.py --se -120 --nlr 76 --government Democracy",
    )
    parser.add_argument("--se", type=int, required=True, help="current se_relation")
    parser.add_argument("--nlr", type=int, required=True, help="current nlr_relation")
    parser.add_argument("--government", default="", help=f"one of: {', '.join(GOVERNMENT_DRIFT)}")
    parser.add_argument("--economy", default="", help=f"one of: {', '.join(ECONOMY_DRIFT)}")
    parser.add_argument(
        "--drift",
        nargs=2,
        type=int,
        metavar=("SE", "NLR"),
        help="override the per-tick drift outright, e.g. --drift -3 2",
    )
    parser.add_argument("--ticks", type=int, default=600, help="horizon (default 600 = 50 days)")
    parser.add_argument("--ascending", action="store_true", help="users.empiremax is set")
    parser.add_argument("--trace", action="store_true", help="print every tick")
    args = parser.parse_args(argv)

    drift = tuple(args.drift) if args.drift else drift_for(args.government, args.economy)
    forecast = project(args.se, args.nlr, drift, ticks=args.ticks, ascending=args.ascending)

    print(f"start   SE {args.se:>6}   NLR {args.nlr:>6}   sum {args.se + args.nlr:>6}")
    print(f"drift   SE {drift[0]:>+6}   NLR {drift[1]:>+6}   per tick ({HOURS_PER_TICK}h)")
    print()
    if args.trace:
        for result in forecast.history:
            marks = "  ".join(s.describe() for s in result.strikes)
            print(
                f"  SE {result.se:>6}  NLR {result.nlr:>6}  sum {result.total:>6}"
                f"{'   <-- ' + marks if marks else ''}"
            )
    first = forecast.first_strike
    if first is None:
        days = args.ticks * HOURS_PER_TICK / 24
        print(f"No airstrike within {args.ticks} ticks ({days:g} days).")
        # Below ~300 ticks a clean run means only that the horizon was short: a lopsided drift can
        # take 200 ticks to bite. Only a long horizon is evidence the drift is genuinely balanced.
        print(
            "This drift is balanced -- it never triggers."
            if args.ticks >= 300
            else "That is only as far as --ticks looked; a lopsided drift can take 200 ticks to bite."
        )
        return 0
    print(f"FIRST STRIKE  {first.describe()}")
    print(f"              in {forecast.hours_until_strike} hours of game time")
    print(f"              it fights at the next war tick (00:00 or 12:00 UTC)")
    later = [s for s in forecast.strikes[1:6]]
    if later:
        print("then          " + ", ".join(f"t{s.tick} {s.empire[:3]} {s.size}" for s in later))
    print(f"total         {len(forecast.strikes)} strikes in {args.ticks} ticks")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
