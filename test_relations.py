#!/usr/bin/env python3
"""Offline unit tests for relations.py -- no network.

Every expectation here is a value read out of clop/cron/frequent.php by hand, not a number this
module produced. That distinction is the whole point: the module exists to predict the tick a
nation gets bombed on, and a test written from its own output would confirm nothing.

The cases that matter are the asymmetries. The Solar Empire's forgiveness is broken in a way the
New Lunar Republic's is not (frequent.php:208,211), jealousy drains the *sum* while the airstrike
query tests only the sum, and ``users.empiremax`` switches off three separate safety valves at once.
"""

import unittest

from relations import (
    BUILDING_DRIFT,
    NEW_LUNAR_REPUBLIC,
    SOLAR_EMPIRE,
    advance,
    drift_for,
    forgiveness_nlr,
    forgiveness_se,
    friendship_decay,
    project,
)

DEMOCRACY = (-3, 2)


class FriendshipDecayTests(unittest.TestCase):
    def test_nothing_below_the_first_tier(self):
        self.assertEqual(friendship_decay(250), 0)
        self.assertEqual(friendship_decay(-900), 0)

    def test_tiers_stack(self):
        # 300 is one tier only: floor(50/50).
        self.assertEqual(friendship_decay(300), 1)
        # 500 crosses two: floor(250/50) + floor(100/50).
        self.assertEqual(friendship_decay(500), 5 + 2)
        # 900 crosses all three: floor(650/50) + floor(500/50) + floor(100/50).
        self.assertEqual(friendship_decay(900), 13 + 10 + 2)

    def test_at_the_cap(self):
        """31/tick at 1000 -- the number the patron-state analysis in DEVELOPMENT.md turns on."""
        self.assertEqual(friendship_decay(1000), 15 + 12 + 4)
        self.assertEqual(friendship_decay(1000), 31)


class ForgivenessTests(unittest.TestCase):
    def test_neither_side_forgives_above_minus_450(self):
        self.assertEqual(forgiveness_se(-450), 0)
        self.assertEqual(forgiveness_nlr(-450), 0)

    def test_the_two_agree_in_the_first_tier(self):
        for relation in (-451, -500, -600, -700):
            self.assertEqual(forgiveness_se(relation), forgiveness_nlr(relation), relation)

    def test_the_nlr_stacks_all_three_tiers(self):
        # -ceil(-550/50) + -ceil(-300/50) + -ceil(-100/50) = 11 + 6 + 2
        self.assertEqual(forgiveness_nlr(-1000), 19)

    def test_the_se_loses_its_deeper_tiers_to_the_bug(self):
        """frequent.php:208,211 write to $serelationeffect, consumed and flushed at :190."""
        self.assertEqual(forgiveness_se(-1000), 11)
        self.assertLess(forgiveness_se(-1000), forgiveness_nlr(-1000))
        self.assertLess(forgiveness_se(-950), forgiveness_nlr(-950))


class JealousyTests(unittest.TestCase):
    def test_each_side_docks_you_for_the_other(self):
        """And off the *post-decay* value: frequent.php:241 reads $rs after :191 has adjusted it.

        Decay first: 400 loses floor(150/50) = 3 -> 397; 300 loses floor(50/50) = 1 -> 299.
        Only then jealousy, from those: floor(299/50) = 5 off the SE, floor(397/50) = 7 off the NLR.
        Reading jealousy off the start-of-tick 300 would give 6, and be one point wrong every tick.
        """
        result = advance(400, 300, (0, 0))
        self.assertEqual(result.se, 397 - 5)
        self.assertEqual(result.nlr, 299 - 7)

    def test_it_is_simultaneous_not_sequential(self):
        """Both amounts come off the same pre-jealousy values (frequent.php:241-246 then :247-258).

        Sequential application would compute the NLR's jealousy from an SE already reduced by 1,
        and floor(199/50) is 3 where floor(200/50) is 4.
        """
        result = advance(200, 200, (0, 0))
        self.assertEqual(result.se, 200 - 4)
        self.assertEqual(result.nlr, 200 - 4)

    def test_a_negative_relation_provokes_no_jealousy(self):
        result = advance(-100, 100, (0, 0))
        self.assertEqual(result.nlr, 100)  # nothing to envy on the SE side
        self.assertEqual(result.se, -100 - 2)  # but the SE still resents NLR 100

    def test_it_only_ever_drains_the_sum(self):
        """The mechanism behind 'imbalance kills': nothing hands the lost points to the other side."""
        before = 300 + 300
        result = advance(300, 300, (0, 0))
        self.assertLess(result.total, before)


class AirstrikeTriggerTests(unittest.TestCase):
    def test_the_gate_is_the_sum_not_either_relation(self):
        """frequent.php:962 -- WHERE nlr_relation + se_relation < -50."""
        # Hated by the Solar Empire, adored by the NLR: never touched.
        self.assertEqual(advance(-900, 900, (0, 0)).strikes, ())

    def test_a_patron_state_is_immune_at_1000_and_minus_1000(self):
        """1000 + (-1000) = 0, which clears the < -50 gate however much one side hates you."""
        self.assertEqual(advance(1000, -1000, (0, 0)).strikes, ())
        self.assertEqual(advance(-1000, 1000, (0, 0)).strikes, ())

    def test_a_relation_above_minus_25_does_not_join_in(self):
        """The sum is under -50 but only one side is angry enough (frequent.php:966, :995)."""
        result = advance(-60, 0, (0, 0))
        self.assertEqual([s.empire for s in result.strikes], [SOLAR_EMPIRE])

    def test_both_can_fire_on_the_same_tick(self):
        result = advance(-200, -200, (0, 0))
        self.assertEqual(
            [s.empire for s in result.strikes], [SOLAR_EMPIRE, NEW_LUNAR_REPUBLIC]
        )

    def test_both_strengths_read_the_same_pre_rebound_snapshot(self):
        """The SE's rebound must not shrink the NLR's force (both computed at :968 and :997)."""
        result = advance(-200, -200, (0, 0))
        self.assertEqual([s.size for s in result.strikes], [50, 50])

    def test_the_rebound_is_the_force_size(self):
        """ceil(-se/4) pegasi, and the same number added back (frequent.php:983)."""
        result = advance(-400, 0, (0, 0))
        self.assertEqual(result.strikes[0].size, 100)
        self.assertEqual(result.se, -400 + 100)


class AscendingTests(unittest.TestCase):
    def test_forgiveness_is_switched_off(self):
        """frequent.php:203 gates the whole block on !empiremax."""
        self.assertEqual(advance(-800, 0, (0, 0), empiremax=-10).se, -800)
        self.assertGreater(advance(-800, 0, (0, 0)).se, -800)

    def test_the_floor_is_switched_off(self):
        """:742 -- hate is unbounded on the ascension path, so drift keeps pushing past -1000.

        Paired with a high NLR so the sum stays near the gate and the airstrike rebound, which is
        also switched off while ascending, does not muddy the comparison.
        """
        forbidden = drift_for(buildings={"Forbidden Research Facility": 1})
        self.assertEqual(advance(-1000, 1000, forbidden).se, -1000)
        self.assertEqual(advance(-1000, 1000, forbidden, empiremax=-10).se, -1034)

    def test_the_airstrike_rebound_is_switched_off(self):
        """:982 -- they attack and the hate stays, so the next strike is bigger."""
        result = advance(-400, 0, (0, 0), empiremax=-10)
        self.assertEqual(result.strikes[0].size, 100)
        self.assertEqual(result.se, -400)

    def test_jealousy_still_applies(self):
        """:241-258 has no empiremax gate, unlike the three above.

        The NLR is dragged from +100 to the ratchet's -11 in the same tick, but not before the
        Solar Empire has docked you floor(100/50) = 2 for it.
        """
        result = advance(-100, 100, (0, 0), empiremax=-10)
        self.assertEqual(result.se, -102)
        self.assertEqual(result.nlr, -11)

    def test_the_ratchet_drags_both_relations_down_one_per_tick(self):
        """:1025-1042 -- empiremax counts down, and both relations are clamped to it."""
        result = advance(500, 500, (0, 0), empiremax=-10)
        self.assertEqual(result.empiremax, -11)
        self.assertEqual((result.se, result.nlr), (-11, -11))

    def test_ascending_is_attacked_by_both_sisters_on_tick_17(self):
        """The game sets empiremax = -10 and both relations to -10 (backend_majoractions.php:111).

        From there the sum is 2 x empiremax and falls 2 a tick, so it clears -50 on tick 17 -- 34
        hours. Alicorn Elite has no government drift, so nothing else is moving.
        """
        forecast = project(-10, -10, (0, 0), ticks=40, empiremax=-10)
        self.assertEqual(forecast.ticks_until_strike, 17)
        self.assertEqual(forecast.hours_until_strike, 34)
        first_tick = [s for s in forecast.strikes if s.tick == 17]
        self.assertEqual([(s.empire, s.size) for s in first_tick],
                         [(SOLAR_EMPIRE, 7), (NEW_LUNAR_REPUBLIC, 7)])

    def test_the_gate_is_strictly_below_minus_50(self):
        """A sum of exactly -50 is safe. The player guide's "-50 or below" is wrong by one tick.

        The ascent passes through exactly -50 on tick 15 and is not touched; -52 on tick 16 is not
        touched either, because the airstrike pass runs *before* the ratchet, on tick 15's numbers.
        """
        forecast = project(-10, -10, (0, 0), ticks=40, empiremax=-10)
        self.assertEqual(forecast.history[14].total, -50)
        self.assertEqual(forecast.history[14].strikes, ())
        self.assertEqual(forecast.history[15].strikes, ())

    def test_ascending_is_then_attacked_every_single_tick(self):
        """No rebound and no floor, so it never stops and each strike is bigger than the last."""
        forecast = project(-10, -10, (0, 0), ticks=60, empiremax=-10)
        se_strikes = [s for s in forecast.strikes if s.empire == SOLAR_EMPIRE]
        self.assertEqual([s.tick for s in se_strikes], list(range(17, 61)))
        self.assertGreater(se_strikes[-1].size, se_strikes[0].size)


class DriftTableTests(unittest.TestCase):
    def test_democracy(self):
        self.assertEqual(drift_for("Democracy"), DEMOCRACY)

    def test_government_and_economy_stack(self):
        self.assertEqual(drift_for("Democracy", "Free Market"), (-6, 3))

    def test_unknown_names_contribute_nothing(self):
        """Loose Despotism really is 0/0, and most economies are absent from frequent.php."""
        self.assertEqual(drift_for("Loose Despotism", "Poorly Defined"), (0, 0))

    def test_buildings_scale_with_how_many_you_own(self):
        self.assertEqual(drift_for(buildings={"Sun Worship Center": 5}), (5, 0))

    def test_one_forbidden_facility_outweighs_every_government(self):
        se, nlr = drift_for("Oppression", buildings={"Forbidden Research Facility": 1})
        self.assertEqual((se, nlr), (6 - 15, -3 - 15))
        self.assertEqual(BUILDING_DRIFT["Forbidden Research Facility"], (-15, -15))


class ForecastTests(unittest.TestCase):
    def test_balanced_drift_is_never_attacked(self):
        """Decay and jealousy cap both relations; the sum stays far above -50 forever."""
        for drift in ((0, 0), (1, 1), (3, 3)):
            self.assertIsNone(project(0, 0, drift, ticks=1200).first_strike, drift)

    def test_a_net_positive_but_lopsided_drift_still_ends_in_strikes(self):
        """+1/+2 gains three points a tick overall and is still bombed -- imbalance is what kills."""
        forecast = project(0, 0, (1, 2), ticks=600)
        self.assertIsNotNone(forecast.first_strike)
        self.assertEqual(forecast.first_strike.empire, SOLAR_EMPIRE)

    def test_the_neglected_side_is_the_one_that_attacks(self):
        forecast = project(0, 0, (4, -3), ticks=600)
        self.assertEqual(forecast.first_strike.empire, NEW_LUNAR_REPUBLIC)

    def test_democracy_from_neutral(self):
        forecast = project(0, 0, DEMOCRACY, ticks=600)
        self.assertEqual(forecast.ticks_until_strike, 38)
        self.assertEqual(forecast.hours_until_strike, 76)
        self.assertEqual(forecast.first_strike.size, 32)

    def test_democracy_settles_into_a_recurring_toll(self):
        """The rebound is big enough that strikes are periodic, not a spiral."""
        forecast = project(0, 0, DEMOCRACY, ticks=600)
        gaps = [b.tick - a.tick for a, b in zip(forecast.strikes, forecast.strikes[1:])]
        self.assertTrue(all(10 <= gap <= 15 for gap in gaps[5:]), gaps[5:])

    def test_democracy_pins_the_nlr_where_decay_cancels_the_drift(self):
        """+2/tick against floor((n-250)/50) settles at 350."""
        self.assertEqual(project(0, 0, DEMOCRACY, ticks=600).history[-1].nlr, 350)

    def test_topping_up_the_favoured_side_backfires_when_the_crisis_is_far_off(self):
        """A point of relation adds 1 to the sum now but 1/50 to the per-tick jealousy drain.

        It therefore pays for itself only if the strike lands within ~50 ticks. From SE +200 the
        strike is 109 ticks away, so 500 points of NLR goodwill *shortens* the countdown.
        """
        self.assertEqual(project(200, 0, DEMOCRACY, ticks=600).ticks_until_strike, 109)
        self.assertEqual(project(200, 500, DEMOCRACY, ticks=600).ticks_until_strike, 60)

    def test_topping_up_the_favoured_side_helps_when_the_crisis_is_near(self):
        """The same 400 points, spent from SE 0 where the strike is 38 ticks away, buys time."""
        self.assertEqual(project(0, 0, DEMOCRACY, ticks=600).ticks_until_strike, 38)
        self.assertEqual(project(0, 400, DEMOCRACY, ticks=600).ticks_until_strike, 45)

    def test_topping_up_the_negative_side_is_always_profit(self):
        """A relation at or below 0 provokes no jealousy at all, so the sum gain is free."""
        self.assertEqual(project(-100, 200, DEMOCRACY, ticks=600).ticks_until_strike, 30)
        self.assertEqual(project(0, 200, DEMOCRACY, ticks=600).ticks_until_strike, 46)
        self.assertEqual(project(100, 200, DEMOCRACY, ticks=600).ticks_until_strike, 61)

    def test_an_already_doomed_position_is_hit_on_the_next_tick(self):
        self.assertEqual(project(-300, 300, DEMOCRACY, ticks=600).ticks_until_strike, 7)
        self.assertEqual(project(-400, 400, DEMOCRACY, ticks=600).ticks_until_strike, 5)


if __name__ == "__main__":
    unittest.main()
