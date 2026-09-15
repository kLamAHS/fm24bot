"""Tests for fm_bot.experiments.splits (spec 13.3)."""
from __future__ import annotations

import unittest

from ..experiments.splits import (
    FinalHoldout,
    HoldoutConsumedError,
    HoldoutError,
    HoldoutRegistry,
    SplitError,
    SplitUnit,
    assert_grouped,
    chronological_split,
    grouped_split,
    grouped_train_tune_test,
)
from ..state.store import Store


def units() -> list[SplitUnit]:
    """Six careers; careers A and B have laboratory branches that must travel with their parent."""
    out = []
    for career in "ABCDEF":
        out.append(SplitUnit(f"{career}-main-1", f"career-{career}", f"branch-{career}-main", "2024-02-17", "2024-02-24"))
        out.append(SplitUnit(f"{career}-main-2", f"career-{career}", f"branch-{career}-main", "2024-03-02", "2024-03-09"))
    out.append(SplitUnit("A-lab-1", "career-A", "branch-A-lab", "2024-02-17", "2024-02-24"))
    out.append(SplitUnit("B-lab-1", "career-B", "branch-B-lab", "2024-02-17", "2024-02-24"))
    return out


class GroupedSplitTests(unittest.TestCase):
    def test_branches_of_one_career_stay_together(self):
        folds = grouped_split(units(), n_folds=3, seed=7)
        self.assertEqual(len(folds), 3)
        all_units = units()
        for split in folds:
            assert_grouped(split, all_units)
            test_careers = set(split.groups["test"])
            self.assertTrue(set(split.groups["train"]).isdisjoint(test_careers))
            if "career-A" in test_careers:
                self.assertIn("A-lab-1", split.test)
                self.assertIn("A-main-1", split.test)
            else:
                self.assertIn("A-lab-1", split.train)
        # every unit is tested exactly once across folds
        tested = [u for split in folds for u in split.test]
        self.assertEqual(sorted(tested), sorted(u.unit_id for u in all_units))

    def test_split_is_deterministic_for_a_seed(self):
        self.assertEqual([s.to_json() for s in grouped_split(units(), n_folds=2, seed=3)], [s.to_json() for s in grouped_split(units(), n_folds=2, seed=3)])
        self.assertNotEqual(grouped_split(units(), n_folds=2, seed=3)[0].groups, grouped_split(units(), n_folds=2, seed=4)[0].groups)

    def test_too_few_careers_is_refused(self):
        with self.assertRaises(SplitError):
            grouped_split(units()[:2], n_folds=3, seed=1)
        with self.assertRaises(SplitError):
            grouped_split(units(), n_folds=1, seed=1)

    def test_train_tune_test_partition_by_career(self):
        split = grouped_train_tune_test(units(), seed=11, tune_fraction=0.2, test_fraction=0.2)
        assert_grouped(split, units())
        self.assertEqual(len(split.groups["test"]), 1)
        self.assertEqual(len(split.groups["tune"]), 1)
        self.assertEqual(len(split.groups["train"]), 4)
        self.assertEqual(len(split.train) + len(split.tune) + len(split.test), len(units()))

    def test_assert_grouped_detects_a_leaky_split(self):
        from ..experiments.splits import Split
        leaky = Split("leaky", ["A-main-1"], ["A-lab-1"])
        with self.assertRaises(SplitError):
            assert_grouped(leaky, units())


class ChronologicalSplitTests(unittest.TestCase):
    def test_embargo_removes_overlapping_outcome_windows(self):
        rows = [
            SplitUnit("past", "c", "b", "2024-01-27", "2024-02-03"),
            SplitUnit("straddles", "c", "b", "2024-02-24", "2024-03-02"),   # outcome unknown at the cutoff
            SplitUnit("just_after", "c", "b", "2024-03-01", "2024-03-09"),  # inside the embargo
            SplitUnit("future", "c", "b", "2024-03-09", "2024-03-16"),
            SplitUnit("no_end", "c", "b", "2024-01-01", None),               # cannot prove its outcome was known
        ]
        split = chronological_split(rows, cutoff="2024-03-01", embargo_days=7)
        self.assertEqual(split.train, ["past"])
        self.assertEqual(split.test, ["future"])
        self.assertEqual(sorted(split.embargoed), ["just_after", "no_end", "straddles"])
        no_embargo = chronological_split(rows, cutoff="2024-03-01")
        self.assertEqual(sorted(no_embargo.test), ["future", "just_after"])
        with self.assertRaises(SplitError):
            chronological_split(rows, cutoff="2024-03-01", embargo_days=-1)

    def test_exp02_a_timed_cutoff_embargoes_a_unit_that_straddles_it(self):
        """EXP 02: a cutoff with a time of day is compared on the same clock as the units, so a kick-off before it whose result came after it is embargoed, not tested."""
        rows = [
            SplitUnit("past", "c", "b", "2024-02-24 15:00", "2024-02-24 17:00"),
            SplitUnit("straddles", "c", "b", "2024-03-01 10:00", "2024-03-01 21:00"),   # began before the cutoff, outcome known after it
            SplitUnit("at_cutoff", "c", "b", "2024-03-01 15:00", "2024-03-01 17:00"),
            SplitUnit("later_same_day", "c", "b", "2024-03-01 19:45", "2024-03-01 21:30"),
        ]
        split = chronological_split(rows, cutoff="2024-03-01 15:00")
        self.assertEqual(split.train, ["past"])
        self.assertEqual(split.embargoed, ["straddles"], "a unit whose outcome was unknown at the cutoff may not be tested")
        self.assertEqual(split.test, ["at_cutoff", "later_same_day"])
        iso = chronological_split(rows, cutoff="2024-03-01T15:00")
        self.assertEqual((iso.train, iso.embargoed, iso.test), (split.train, split.embargoed, split.test), "both moment spellings are the same clock")

    def test_exp02_a_timed_embargo_window_ends_at_the_cutoff_time_of_day(self):
        """EXP 02: embargo_days keeps its time of day too, so the far end of the window is not pulled back to midnight."""
        rows = [
            SplitUnit("inside_embargo", "c", "b", "2024-03-06 09:00", "2024-03-06 11:00"),
            SplitUnit("on_boundary", "c", "b", "2024-03-06 15:00", "2024-03-06 17:00"),
            SplitUnit("after", "c", "b", "2024-03-07 15:00", "2024-03-07 17:00"),
        ]
        split = chronological_split(rows, cutoff="2024-03-01 15:00", embargo_days=5)
        self.assertEqual(split.embargoed, ["inside_embargo"])
        self.assertEqual(split.test, ["on_boundary", "after"])
        self.assertEqual(split.train, [])


class HoldoutTests(unittest.TestCase):
    def test_holdout_is_consumed_when_a_judged_model_changes(self):
        store = Store.memory()
        registry = HoldoutRegistry(store)
        holdout = registry.freeze("final-2024", ["career-E", "career-F"], {"lineup": "hash-a", "recovery": "hash-r"})
        self.assertFalse(holdout.consumed)
        registry.assert_training_excludes(holdout, ["career-A", "career-B"])
        with self.assertRaises(HoldoutError):
            registry.assert_training_excludes(holdout, ["career-A", "career-E"])
        registry.evaluate("final-2024", "lineup", "hash-a", {"points_per_match": 1.4})
        self.assertTrue(registry.usable_for_release_claim("final-2024", "lineup", "hash-a")[0])
        # the model is retrained after seeing the holdout result
        changed = registry.model_changed("final-2024", "lineup", "hash-b")
        self.assertTrue(changed.consumed)
        self.assertIn("lineup", changed.consumed_reason)
        with self.assertRaises(HoldoutConsumedError):
            registry.evaluate("final-2024", "recovery", "hash-r", {})
        self.assertFalse(registry.usable_for_release_claim("final-2024", "lineup", "hash-b")[0])
        # persisted and journaled; a fresh registry sees the consumption
        again = HoldoutRegistry(store).get("final-2024")
        self.assertTrue(again.consumed)
        self.assertEqual(len(store.journal_entries("holdout.consumed")), 1)
        # a new holdout is required for further claims
        fresh = registry.freeze("final-2025", ["career-G"], {"lineup": "hash-b"})
        self.assertFalse(fresh.consumed)

    def test_unfrozen_or_altered_candidates_cannot_be_judged(self):
        registry = HoldoutRegistry()
        registry.freeze("h", ["career-Z"], {"lineup": "hash-a"})
        with self.assertRaises(HoldoutError):
            registry.evaluate("h", "unknown-model", "x", {})
        with self.assertRaises(HoldoutConsumedError):
            registry.evaluate("h", "lineup", "hash-changed", {})   # altered after freezing: consumed
        self.assertTrue(registry.get("h").consumed)

    def test_freeze_rules(self):
        registry = HoldoutRegistry()
        with self.assertRaises(HoldoutError):
            registry.freeze("h", [], {"m": "x"})
        with self.assertRaises(HoldoutError):
            registry.freeze("h", ["c"], {})
        registry.freeze("h", ["c"], {"m": "x"})
        with self.assertRaises(HoldoutError):
            registry.freeze("h", ["c"], {"m": "x"})
        self.assertEqual(FinalHoldout.from_json(registry.get("h").to_json()).career_ids, ["c"])
        self.assertEqual(registry.usable_for_release_claim("nope", "m", "x"), (False, ["unknown holdout nope"]))

    def test_model_change_without_evaluation_does_not_consume(self):
        registry = HoldoutRegistry()
        registry.freeze("h", ["c"], {"m": "x"})
        self.assertFalse(registry.model_changed("h", "m", "y").consumed)


if __name__ == "__main__":
    unittest.main()
