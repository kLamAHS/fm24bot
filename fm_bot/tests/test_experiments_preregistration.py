"""Tests for fm_bot.experiments.preregistration (spec 13.2, 13.3, 13.5: declare it before you run it)."""
from __future__ import annotations

import unittest

from ..experiments.evaluation import PairedUnit, paired_comparison
from ..experiments.preregistration import (
    JOURNAL_DECLARED, JOURNAL_REPORTED, LOWER_IS_BETTER, VERDICT_EXPLORATORY, AnalysisPlan,
    Preregistration, PreregistrationError, PreregistrationRegistry, declare, first_model_experiment, validate,
)
from ..state.store import Store


def plan(**kw) -> AnalysisPlan:
    return AnalysisPlan(kw.pop("metric", "points_per_match"), seed=kw.pop("seed", 7), **kw)


def prereg(**kw) -> Preregistration:
    base = dict(
        hypothesis="The pressing change improves points per match for a squad of this class.",
        treatment={"policy": "press higher", "mentality": "positive"},
        baseline_id="fixed-legal-heuristic-v1",
        primary_outcome="points_per_match",
        smallest_useful_effect=0.15,
        stopping_rule="One look, after all declared careers finish their season; no extension.",
        analysis=plan(),
        guardrails=["eligibility_violations"],
        expected_sd=0.30,
    )
    base.update(kw)
    return declare(**base)


def units(n: int, *, treated: float, control: float, metric_seed: int = 0) -> list[PairedUnit]:
    return [PairedUnit(f"career-{i}", treated + i * 0.001, control) for i in range(n)]


class DeclarationTests(unittest.TestCase):
    def test_a_declaration_states_everything_the_specification_requires(self):
        """Spec 13.2: hypothesis, treatment, outcome, smallest useful effect and stopping rule, before running."""
        p = prereg()
        self.assertEqual(validate(p), [])
        self.assertEqual(p.analysis.metric, p.primary_outcome)
        self.assertGreater(p.required_clusters, 0, "a pilot standard deviation sizes the evaluation")
        for missing in ({"hypothesis": "  "}, {"stopping_rule": ""}, {"baseline_id": ""}, {"smallest_useful_effect": 0.0}):
            with self.assertRaises(PreregistrationError):
                prereg(**missing)

    def test_the_outcome_measured_must_be_the_outcome_declared(self):
        with self.assertRaises(PreregistrationError) as caught:
            prereg(analysis=plan(metric="goal_difference"))
        self.assertIn("primary outcome", str(caught.exception))

    def test_without_pilot_variability_the_evaluation_size_stays_undeclared(self):
        """Spec 13.5: do not choose a convenient count and assume it is sufficient."""
        p = prereg(expected_sd=None)
        self.assertIsNone(p.required_clusters)


class ImmutabilityTests(unittest.TestCase):
    def test_exp01_a_declaration_is_journaled_once_and_cannot_be_edited(self):
        """EXP 01 / spec 13.3: the declaration is frozen before the evaluation, so a later edit is refused rather than
        silently replacing what the result will be judged against."""
        store = Store.memory()
        registry = PreregistrationRegistry(store)
        p = prereg(prereg_id="prereg-press-1")
        registry.declare(p)
        self.assertEqual(registry.declare(p).fingerprint(), p.fingerprint(), "re-declaring the same content is idempotent")
        moved = prereg(prereg_id="prereg-press-1", smallest_useful_effect=0.05)
        with self.assertRaises(PreregistrationError) as caught:
            registry.declare(moved)
        self.assertIn("cannot be edited", str(caught.exception))
        entries = store.journal_entries(kind=JOURNAL_DECLARED, ref_id="prereg-press-1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["body"]["fingerprint"], p.fingerprint())
        self.assertEqual(registry.status("prereg-press-1"), "declared")

    def test_a_declaration_survives_a_restart(self):
        store = Store.memory()
        PreregistrationRegistry(store).declare(prereg(prereg_id="prereg-press-2"))
        reloaded = PreregistrationRegistry(store).get("prereg-press-2")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.stopping_rule, prereg().stopping_rule)


class ReportingTests(unittest.TestCase):
    def _registry(self, **kw):
        store = Store.memory()
        registry = PreregistrationRegistry(store)
        p = registry.declare(prereg(prereg_id="prereg-press-3", **kw))
        return store, registry, p

    def test_a_comparison_that_followed_the_declaration_is_confirmatory(self):
        store, registry, p = self._registry(expected_sd=0.30)
        comparison = paired_comparison(units(40, treated=1.9, control=1.3), metric="points_per_match", seed=7)
        result = registry.report(p.prereg_id, comparison, guardrails={"eligibility_violations": False})
        self.assertTrue(result.confirmatory)
        self.assertEqual(result.verdict, "improvement")
        self.assertEqual(result.deviations, [])
        self.assertEqual(store.journal_entries(kind=JOURNAL_REPORTED, ref_id=p.prereg_id)[0]["body"]["verdict"], "improvement")
        self.assertEqual(registry.status(p.prereg_id), "reported")

    def test_exp01_changing_the_outcome_or_the_seed_makes_the_result_exploratory(self):
        """EXP 01 / spec 13.5: a study analysed differently from its declaration has no confirmatory weight, however good
        the interval looks; the deviations are named rather than the verdict being quietly kept."""
        _, registry, p = self._registry()
        other_metric = paired_comparison(units(40, treated=1.9, control=1.3), metric="goal_difference", seed=7)
        moved = registry.report(p.prereg_id, other_metric, guardrails={"eligibility_violations": False})
        self.assertEqual(moved.verdict, VERDICT_EXPLORATORY)
        self.assertFalse(moved.confirmatory)
        self.assertIn("outcome measure changed", moved.deviations[0])
        other_seed = paired_comparison(units(40, treated=1.9, control=1.3), metric="points_per_match", seed=99)
        reseeded = registry.report(p.prereg_id, other_seed, guardrails={"eligibility_violations": False})
        self.assertEqual(reseeded.verdict, VERDICT_EXPLORATORY)
        self.assertTrue(any("seed changed" in d for d in reseeded.deviations))

    def test_an_undeclared_guardrail_is_recorded_and_a_declared_one_must_be_measured(self):
        _, registry, p = self._registry()
        unmeasured = registry.report(p.prereg_id, paired_comparison(units(40, treated=1.9, control=1.3), metric="points_per_match", seed=7), guardrails={})
        self.assertEqual(unmeasured.verdict, VERDICT_EXPLORATORY)
        self.assertIn("was not measured", unmeasured.deviations[0])
        extra = registry.report(p.prereg_id, paired_comparison(units(40, treated=1.9, control=1.3), metric="points_per_match", seed=7), guardrails={"eligibility_violations": False, "wall_time": True})
        self.assertTrue(extra.confirmatory, "an extra measurement never turns a confirmatory result into a failure")
        self.assertTrue(any("not declared" in reason for reason in extra.reasons))

    def test_a_degraded_guardrail_blocks_and_too_few_clusters_is_inconclusive(self):
        """Spec 13.5: no material degradation on the declared guardrails, and an underpowered result is inconclusive
        rather than negative."""
        _, registry, p = self._registry(expected_sd=0.30)
        degraded = registry.report(p.prereg_id, paired_comparison(units(40, treated=1.9, control=1.3), metric="points_per_match", seed=7), guardrails={"eligibility_violations": True})
        self.assertEqual(degraded.verdict, "blocked_by_guardrail")
        self.assertTrue(degraded.confirmatory, "it followed its declaration; the guardrail is what stopped it")
        few = registry.report(p.prereg_id, paired_comparison(units(5, treated=1.9, control=1.3), metric="points_per_match", seed=7), guardrails={"eligibility_violations": False})
        self.assertEqual(few.verdict, "inconclusive")
        self.assertTrue(any("below the" in reason for reason in few.reasons))

    def test_a_lower_is_better_outcome_is_judged_in_the_improving_direction(self):
        store = Store.memory()
        registry = PreregistrationRegistry(store)
        p = registry.declare(prereg(prereg_id="prereg-error-1", primary_outcome="readiness_absolute_error", direction=LOWER_IS_BETTER, smallest_useful_effect=2.0, threshold=2.0, expected_sd=3.0, analysis=plan(metric="readiness_absolute_error")))
        better = paired_comparison([PairedUnit(f"career-{i}", 4.0 + i * 0.01, 9.0) for i in range(40)], metric="readiness_absolute_error", seed=7)
        result = registry.report(p.prereg_id, better, guardrails={"eligibility_violations": False})
        self.assertEqual(result.verdict, "improvement")
        self.assertGreater(result.oriented_mean, 0, "a smaller error reads as a positive improvement")
        self.assertTrue(any("better when lower" in reason for reason in result.reasons))
        worse = paired_comparison([PairedUnit(f"career-{i}", 9.0 + i * 0.01, 4.0) for i in range(40)], metric="readiness_absolute_error", seed=7)
        self.assertEqual(registry.report(p.prereg_id, worse, guardrails={"eligibility_violations": False}).verdict, "no_improvement")

    def test_reporting_an_undeclared_experiment_is_refused(self):
        with self.assertRaises(PreregistrationError):
            PreregistrationRegistry(Store.memory()).report("prereg-never", paired_comparison(units(40, treated=1.9, control=1.3), metric="points_per_match", seed=7))


class FirstModelExperimentTests(unittest.TestCase):
    def test_bot012_the_first_model_experiment_is_declared_and_honestly_unrun(self):
        """Specification ticket BOT 012: one preregistered model experiment. It names its baseline, its outcome, its
        stopping rule and its guardrails, and says plainly that it cannot run until the laboratory capabilities exist."""
        p = first_model_experiment()
        self.assertEqual(validate(p), [])
        self.assertEqual(p.direction, LOWER_IS_BETTER)
        self.assertEqual(p.baseline_id, "recovery-baseline-v1")
        self.assertIsNone(p.required_clusters, "no pilot variability is known, so no size is claimed")
        self.assertIn("save_restore", p.notes)
        store = Store.memory()
        registry = PreregistrationRegistry(store)
        registry.declare(p)
        self.assertEqual(registry.status(p.prereg_id), "declared")
        self.assertEqual(registry.get(p.prereg_id).fingerprint(), p.fingerprint())


if __name__ == "__main__":
    unittest.main()
