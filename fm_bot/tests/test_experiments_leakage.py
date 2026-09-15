"""Tests for fm_bot.experiments.leakage (spec 5.4, 13.3, EXP 02)."""
from __future__ import annotations

import unittest

from ..experiments.leakage import (
    CATEGORY_CONTRACT,
    CATEGORY_FINAL_MATCH_STATISTIC,
    CATEGORY_OUTCOME_DERIVED,
    CATEGORY_SCOUT_KNOWLEDGE,
    RULE_CROSS_BRANCH,
    RULE_FINAL_MATCH_STATISTIC,
    RULE_FUTURE_CONTRACT,
    RULE_FUTURE_KNOWLEDGE,
    RULE_LABEL_AS_FEATURE,
    RULE_LABEL_KNOWN_BEFORE_EVENT,
    RULE_LATER_SCOUT_KNOWLEDGE,
    RULE_UNKNOWN_AVAILABILITY,
    Dataset,
    DecisionExample,
    FeatureRow,
    FoldLeakageError,
    FoldScope,
    LabelRow,
    as_of_join,
    build_example,
    detect_leakage,
    game_moment_key,
)

MAIN, LAB = "branch-main", "branch-lab"
DECISION = "2024-02-17 10:00"   # the fixture's decision moment: before the 20 Feb cup tie


def row(name, value, known_at, *, branch=MAIN, entity=1015, event=None, category="feature", obs="obs-1"):
    return FeatureRow(entity, name, value, known_at, "2026-01-01T00:00:00+00:00", branch, obs, event, category)


def clean_rows():
    return [
        row("condition", 93.0, "2024-02-17 09:00"),
        row("condition", 90.0, "2024-02-10"),                                     # older reading, superseded
        row("last_match_rating", 7.1, "2024-02-10 17:00", event="2024-02-10 16:55", category=CATEGORY_FINAL_MATCH_STATISTIC),
        row("weekly_wage", 6100, "2023-07-01", event="2023-07-01", category=CATEGORY_CONTRACT),
        row("scout_knowledge", 100, "2024-01-05", category=CATEGORY_SCOUT_KNOWLEDGE),
    ]


def label():
    return LabelRow(1015, "minutes_next_fixture", 90, "2024-02-20 21:40", "2024-02-20 21:40", MAIN, "obs-9")


class AsOfJoinTests(unittest.TestCase):
    def test_game_moment_key_orders_dates_and_times(self):
        self.assertLess(game_moment_key("2024-02-17"), game_moment_key("2024-02-17 10:00"))
        self.assertLess(game_moment_key("2024-02-17 10:00"), game_moment_key("2024-02-17T10:01"))
        self.assertIsNone(game_moment_key(None))

    def test_join_keeps_only_prior_same_branch_latest_rows(self):
        rows = clean_rows() + [
            row("condition", 60.0, "2024-02-20 22:00"),                # after the decision
            row("condition", 80.0, "2024-02-16", branch=LAB),           # sibling branch
            row("morale", "Good", None),                                # unknown availability
        ]
        joined = {r.feature_name: r for r in as_of_join(rows, DECISION, MAIN)}
        self.assertEqual(joined["condition"].value, 93.0)
        self.assertNotIn("morale", joined)
        self.assertEqual(set(joined), {"condition", "last_match_rating", "weekly_wage", "scout_knowledge"})
        self.assertEqual(as_of_join(rows, DECISION, MAIN, entity_id=2001), [])

    def test_join_at_exact_moment_is_inclusive(self):
        rows = [row("condition", 91.0, DECISION)]
        self.assertEqual(len(as_of_join(rows, DECISION, MAIN)), 1)


class DetectLeakageTests(unittest.TestCase):
    def test_clean_dataset_passes(self):
        example = build_example("ex-1", 1015, DECISION, MAIN, clean_rows(), [label()])
        report = detect_leakage(Dataset("clean", [example]))
        self.assertTrue(report.passed, report.to_json())
        self.assertEqual(report.checked_examples, 1)
        self.assertEqual(report.checked_rows, 5)
        self.assertEqual(len(example.features), 4)

    def test_deliberately_contaminated_dataset_fails(self):
        """EXP 02: every kind of future information is caught and named."""
        contaminated = DecisionExample("ex-bad", 1015, DECISION, MAIN, [
            *clean_rows(),
            row("condition_after_cup", 55.0, "2024-02-20 22:00"),                                                          # known later
            row("condition_lab", 80.0, "2024-02-16", branch=LAB),                                                          # other branch
            row("cup_rating", 6.4, "2024-02-20 21:40", event="2024-02-20 21:40", category=CATEGORY_FINAL_MATCH_STATISTIC),   # final stats of a match not yet played
            row("new_contract_wage", 8000, "2024-02-17 09:00", event="2024-07-01", category=CATEGORY_CONTRACT),               # future contract
            row("scout_knowledge", 100, "2024-03-01", category=CATEGORY_SCOUT_KNOWLEDGE),                                     # later scout report
            row("minutes_next_fixture", 90, "2024-02-17 09:00"),                                                              # the label itself
            row("derived_from_outcome", 1, "2024-02-17 09:00", category=CATEGORY_OUTCOME_DERIVED),
            row("unknown_when", 1, None),
        ], [label()])
        report = detect_leakage(Dataset("contaminated", [contaminated]))
        self.assertFalse(report.passed)
        hit = report.rules_hit()
        for rule in (RULE_FUTURE_KNOWLEDGE, RULE_CROSS_BRANCH, RULE_FINAL_MATCH_STATISTIC, RULE_FUTURE_CONTRACT, RULE_LATER_SCOUT_KNOWLEDGE, RULE_LABEL_AS_FEATURE, RULE_UNKNOWN_AVAILABILITY):
            self.assertIn(rule, hit, rule)
        names = {(f.name, f.rule) for f in report.findings}
        self.assertIn(("condition_after_cup", RULE_FUTURE_KNOWLEDGE), names)
        self.assertIn(("condition_lab", RULE_CROSS_BRANCH), names)
        self.assertIn(("cup_rating", RULE_FINAL_MATCH_STATISTIC), names)
        self.assertIn(("new_contract_wage", RULE_FUTURE_CONTRACT), names)
        self.assertIn(("scout_knowledge", RULE_LATER_SCOUT_KNOWLEDGE), names)
        self.assertIn(("minutes_next_fixture", RULE_LABEL_AS_FEATURE), names)
        self.assertIn(("derived_from_outcome", RULE_LABEL_AS_FEATURE), names)
        self.assertIn(("unknown_when", RULE_UNKNOWN_AVAILABILITY), names)
        # the clean rows in the same example produce no findings
        self.assertNotIn("condition", {f.name for f in report.findings})

    def test_as_of_join_prevents_the_contamination_the_check_catches(self):
        rows = clean_rows() + [row("condition_after_cup", 55.0, "2024-02-20 22:00"), row("condition_lab", 80.0, "2024-02-16", branch=LAB)]
        example = build_example("ex-2", 1015, DECISION, MAIN, rows, [label()])
        self.assertTrue(detect_leakage(Dataset("joined", [example])).passed)

    def test_label_clocks_are_checked(self):
        bad_label = LabelRow(1015, "minutes_next_fixture", 90, "2024-02-20 21:40", "2024-02-18", MAIN, "obs-9")   # known before it happened
        other_branch = LabelRow(1015, "goal_difference", 1, "2024-02-20 21:40", "2024-02-20 21:40", LAB, "obs-9")
        no_clock = LabelRow(1015, "rating", 7.0, None, None, MAIN, "obs-9")
        report = detect_leakage(Dataset("labels", [DecisionExample("ex-3", 1015, DECISION, MAIN, [], [bad_label, other_branch, no_clock])]))
        self.assertEqual(report.rules_hit(), {RULE_LABEL_KNOWN_BEFORE_EVENT: 1, RULE_CROSS_BRANCH: 1, RULE_UNKNOWN_AVAILABILITY: 1})

    def test_final_match_statistic_needs_a_match_end_time(self):
        example = DecisionExample("ex-4", 1015, DECISION, MAIN, [row("rating", 7.0, "2024-02-10 17:00", category=CATEGORY_FINAL_MATCH_STATISTIC)], [])
        report = detect_leakage(Dataset("stat", [example]))
        self.assertEqual(report.rules_hit(), {RULE_FINAL_MATCH_STATISTIC: 1})


class FoldScopeTests(unittest.TestCase):
    def test_transforms_fitted_on_training_rows_only(self):
        train = ["ex-1", "ex-2", "ex-3"]
        with FoldScope("fold-1", train) as scope:
            scaler = scope.fit("scaler", "standardise_condition", train, {"mean": 91.0, "sd": 2.0})
            scope.fit("imputer", "median_sharpness", ["ex-1", "ex-2"])
            scope.fit("fracdiff", "wage_d", train, {"d": 0.4})
            scope.fit("feature_selection", "top_k", train, {"k": 12})
            scope.fit("outcome_label", "minutes_bucket", train)
        self.assertTrue(scope.clean)
        self.assertEqual(scaler.fold_id, "fold-1")
        self.assertEqual(len(scope.fitted), 5)
        self.assertEqual(scope.report()["training_examples"], 3)
        scope.assert_clean()

    def test_fit_touching_test_rows_fails_closed(self):
        with FoldScope("fold-2", ["ex-1", "ex-2"]) as scope:
            with self.assertRaises(FoldLeakageError) as ctx:
                scope.fit("scaler", "standardise", ["ex-1", "ex-2", "ex-9"])
            self.assertIn("ex-9", str(ctx.exception))
            self.assertFalse(scope.clean)
        with self.assertRaises(FoldLeakageError):
            scope.assert_clean()
        self.assertEqual(scope.fitted, [])

    def test_unknown_kind_and_closed_scope_are_refused(self):
        scope = FoldScope("fold-3", ["ex-1"])
        with self.assertRaises(FoldLeakageError):
            scope.fit("magic", "x", ["ex-1"])
        with scope:
            pass
        with self.assertRaises(FoldLeakageError):
            scope.fit("scaler", "late", ["ex-1"])


if __name__ == "__main__":
    unittest.main()
