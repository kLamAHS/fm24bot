"""Tests for the promise ledger (spec 11.3)."""
from __future__ import annotations

import unittest

from ..interactions.promises import (
    KIND_ALLOW_TRANSFER, KIND_NEW_CONTRACT, KIND_PLAYING_TIME, KIND_RECRUIT, KIND_STARTING_ROLE, MATCH_MINUTES, PLAYING_TIME_MINUTES_PER_FIXTURE,
    RENEWAL_ACTIONS, SEVERITY_BLOCKING, SEVERITY_INFO, SEVERITY_WARNING, STARTING_ROLE_MINUTES_PER_FIXTURE, STATUS_BROKEN, STATUS_EXPIRED, STATUS_KEPT,
    STATUS_WITHDRAWN, PromiseError, PromiseLedger, PromiseTerms, parse_terms, promise_from_observed,
)
from ..state.records import Promise
from ..state.store import Store
from . import fixtures as fx


def ledger() -> tuple[Store, PromiseLedger]:
    registered = fx.registered_store()
    return registered.store, PromiseLedger(registered.store, registered.branch_id)


class ParseTermsTests(unittest.TestCase):
    def test_keyword_table_is_labelled_heuristic(self):
        terms = parse_terms("I promise you will be a regular starter at ST this season")
        self.assertEqual((terms.kind, terms.position, terms.basis), (KIND_STARTING_ROLE, "ST", "heuristic"))
        self.assertEqual(parse_terms("You will get at least 30 minutes a game").minutes_per_fixture, 30)
        self.assertEqual(parse_terms("We will sign a new goalkeeper in January").kind, KIND_RECRUIT)
        self.assertEqual(parse_terms("We will offer you a new contract").kind, KIND_NEW_CONTRACT)
        self.assertEqual(parse_terms("We will listen to offers for you").kind, KIND_ALLOW_TRANSFER)
        self.assertEqual(parse_terms("Thanks for coming in").kind, "other")


class RecordTests(unittest.TestCase):
    def test_record_from_observed_choice(self):
        store, book = ledger()
        promise, terms = promise_from_observed(1001, "player", "You will be a regular starter", source="obs-dialogue-1", source_choice="choice:promise_start", deadline="2024-05-01")
        book.record(promise, terms)
        [loaded] = book.open()
        self.assertEqual((loaded.party_id, loaded.confidence, loaded.source_choice, loaded.status), (1001, "observed", "choice:promise_start", "open"))
        self.assertEqual(book.terms(promise.promise_id).kind, KIND_STARTING_ROLE)
        self.assertEqual(store.journal_entries(kind="promise_terms", ref_id=promise.promise_id)[0]["body"]["terms"]["kind"], KIND_STARTING_ROLE)

    def test_explicit_terms_are_preferred_and_persist_across_ledgers(self):
        store, book = ledger()
        promise, _ = promise_from_observed(1002, "player", "You'll get your chance", source="ui:inbox#77", terms=PromiseTerms(KIND_PLAYING_TIME, "DC", 45, 6))
        book.record(promise, PromiseTerms(KIND_PLAYING_TIME, "DC", 45, 6))
        self.assertEqual(promise.playing_time_minutes_per_fixture, 45)
        again = PromiseLedger(store, book.branch_id)
        self.assertEqual(again.terms(promise.promise_id), PromiseTerms(KIND_PLAYING_TIME, "DC", 45, 6))
        self.assertEqual(len(again.open()), 1)

    def test_promise_inferred_from_morale_is_refused(self):
        _, book = ledger()
        inferred = Promise("p-x", 1001, "player", "probably wants to start", None, "", None, None, "inferred", "morale:1001")
        with self.assertRaises(PromiseError):
            book.record(inferred)
        unsourced = Promise("p-y", 1001, "player", "You will start", None, "", None, None, "observed", "")
        with self.assertRaises(PromiseError):
            book.record(unsourced)
        with self.assertRaises(PromiseError):
            promise_from_observed(1001, "player", "You will start", source="")
        self.assertEqual(book.open(), [])


class CheckBeforeTests(unittest.TestCase):
    def setUp(self):
        self.store, self.book = ledger()
        self.starter, _ = promise_from_observed(1001, "player", "You will be a regular starter at ST", source="obs-d1", source_choice="c1")
        self.book.record(self.starter)
        self.leaver, _ = promise_from_observed(1005, "player", "We will listen to offers for you", source="obs-d2", source_choice="c2")
        self.book.record(self.leaver)

    def test_selling_a_promised_starter_is_blocking(self):
        [conflict] = self.book.check_before("sell_player", {"player_id": 1001})
        self.assertEqual((conflict.severity, conflict.promise_id), (SEVERITY_BLOCKING, self.starter.promise_id))
        self.assertTrue(conflict.blocking)
        self.assertEqual(self.book.check_before("sell_player", {"player_id": 1009}), [])

    def test_selling_a_player_promised_a_move_is_informational(self):
        [conflict] = self.book.check_before("accept_sale", {"player_id": 1005})
        self.assertEqual(conflict.severity, SEVERITY_INFO)

    def test_recruiting_into_a_promised_position_warns(self):
        [conflict] = self.book.check_before("commit.transfer_offer", {"position": "ST"})
        self.assertEqual((conflict.severity, conflict.party_id), (SEVERITY_WARNING, 1001))
        self.assertEqual(self.book.check_before("commit.transfer_offer", {"position": "GK"}), [])
        anywhere, _ = promise_from_observed(1003, "player", "You will get more games", source="obs-d3", source_choice="c3")
        self.book.record(anywhere)
        self.assertEqual(len(self.book.check_before("recruit", {"position": "GK"})), 1)

    def test_lineup_without_promised_starter_warns(self):
        [conflict] = self.book.check_before("advise.lineup", {"starters": [1002, 1003]})
        self.assertEqual((conflict.severity, conflict.party_id), (SEVERITY_WARNING, 1001))
        self.assertEqual(self.book.check_before("advise.lineup", {"starters": [1001, 1002]}), [])

    def test_conversation_lists_open_promises_as_info(self):
        [conflict] = self.book.check_before("conversations.player", {"player_id": 1001})
        self.assertEqual(conflict.severity, SEVERITY_INFO)

    def test_renewing_a_player_promised_a_move_contradicts_the_promise(self):
        """Spec 11.3: the ledger is checked before renewals, so renewing a player promised a transfer is a conflict, not silence."""
        for action in sorted(RENEWAL_ACTIONS):
            with self.subTest(action=action):
                [conflict] = self.book.check_before(action, {"player_id": 1005})
                self.assertEqual((conflict.severity, conflict.promise_id, conflict.party_id), (SEVERITY_BLOCKING, self.leaver.promise_id, 1005))
                self.assertTrue(conflict.blocking)
                self.assertIn("contradicts the promise to let 1005 leave", conflict.reason)
                self.assertIn(self.leaver.commitment, conflict.reason)
        self.assertEqual(self.book.check_before("contracts.renew", {"player_id": 1009}), [], "a player with no open promise raises nothing")

    def test_a_contract_offer_naming_no_player_is_recruitment_not_a_renewal(self):
        """Spec 11.3: ``commit.contract`` is a renewal only when it names a player already at the club; without one it is checked as recruitment."""
        [conflict] = self.book.check_before("commit.contract", {"position": "ST"})
        self.assertEqual((conflict.party_id, conflict.severity), (1001, SEVERITY_WARNING))
        self.assertIn("recruiting a ST competes with", conflict.reason)
        [loan] = self.book.check_before("loans.in", {"position": "ST", "player_id": 1005})
        self.assertEqual((loan.party_id, loan.severity, loan.promise_id), (1001, SEVERITY_WARNING, self.starter.promise_id), "a loan-in names the incoming player, so it stays a recruitment check")

    def test_renewal_fulfilling_a_promised_new_contract_is_informational(self):
        """Spec 11.3: a renewal that keeps a promised new deal is reported as progress, not as a breach."""
        promised, terms = promise_from_observed(1007, "player", "We will offer you a new contract in the summer", source="obs-d4", source_choice="c4")
        self.book.record(promised, terms)
        [conflict] = self.book.check_before("commit.contract", {"player_id": 1007})
        self.assertEqual((conflict.severity, conflict.promise_id), (SEVERITY_INFO, promised.promise_id))
        self.assertFalse(conflict.blocking)
        self.assertIn("fulfils the promise", conflict.reason)

    def test_renewal_that_commits_promised_playing_time_elsewhere_warns(self):
        """Spec 11.3: a renewal commits playing time, so a starting-role promise another player holds at that position competes with it."""
        conflicts = {c.party_id: c for c in self.book.check_before("contracts.renew", {"player_id": 1005, "position": "ST"})}
        self.assertEqual(sorted(conflicts), [1001, 1005])
        self.assertEqual(conflicts[1001].severity, SEVERITY_WARNING)
        self.assertEqual(conflicts[1001].promise_id, self.starter.promise_id)
        self.assertIn("commits playing time already promised to 1001", conflicts[1001].reason)
        self.assertEqual(conflicts[1005].severity, SEVERITY_BLOCKING)
        elsewhere = self.book.check_before("contracts.renew", {"player_id": 1005, "position": "GK"})
        self.assertEqual([c.party_id for c in elsewhere], [1005], "a promise at another position does not compete")

    def test_renewing_the_promised_starter_himself_is_not_a_competing_conflict(self):
        """Spec 11.3: a player's own starting-role promise does not compete with his own renewal; it is reported once, as information."""
        [conflict] = self.book.check_before("commit.contract", {"player_id": 1001, "position": "ST"})
        self.assertEqual((conflict.party_id, conflict.severity), (1001, SEVERITY_INFO))
        self.assertIn("open promise to 1001", conflict.reason)

    def test_a_promised_signing_is_not_progressed_by_a_renewal(self):
        """Spec 11.3: renewing an existing player is not the signing that was promised, so no progress is claimed."""
        signing, terms = promise_from_observed(1001, "player", "We will sign a new ST for you", source="obs-d5", source_choice="c5")
        self.book.record(signing, terms)
        self.assertEqual(terms.kind, KIND_RECRUIT)
        recruiting = [c.promise_id for c in self.book.check_before("commit.transfer_offer", {"position": "ST"})]
        self.assertIn(signing.promise_id, recruiting, "recruitment does progress it")
        renewing = {c.promise_id: c.severity for c in self.book.check_before("contracts.renew", {"player_id": 1005, "position": "ST"})}
        self.assertNotIn(signing.promise_id, renewing)
        self.assertEqual(renewing, {self.leaver.promise_id: SEVERITY_BLOCKING, self.starter.promise_id: SEVERITY_WARNING})


class MinutesTests(unittest.TestCase):
    def test_reservations_use_observed_minutes_or_labelled_defaults(self):
        _, book = ledger()
        starter, _ = promise_from_observed(1001, "player", "You will be a regular starter", source="obs-d1", source_choice="c1")
        book.record(starter)
        timed, _ = promise_from_observed(1002, "player", "You will play at least 30 minutes a game", source="obs-d2", source_choice="c2")
        book.record(timed)
        other, _ = promise_from_observed(1003, "player", "We will offer you a new contract", source="obs-d3", source_choice="c3")
        book.record(other)
        self.assertEqual(book.minutes_commitments(3), {1001: STARTING_ROLE_MINUTES_PER_FIXTURE * 3, 1002: 90})
        bases = {r.player_id: r.basis for r in book.minutes_reservations(3)}
        self.assertEqual(bases, {1001: "heuristic", 1002: "observed"})

    def test_deadline_and_fixture_span_limit_reservations(self):
        _, book = ledger()
        promise, _ = promise_from_observed(1001, "player", "You will get regular football", source="obs-d1", source_choice="c1", deadline="2024-03-05")
        book.record(promise)
        dates = ["2024-02-20", "2024-02-24", "2024-03-02", "2024-03-09", "2024-03-16"]
        self.assertEqual(book.minutes_commitments(5, dates), {1001: PLAYING_TIME_MINUTES_PER_FIXTURE * 3})
        capped, _ = promise_from_observed(1002, "player", "You will start", source="obs-d2", source_choice="c2", terms=PromiseTerms(KIND_STARTING_ROLE, None, 90, 2))
        book.record(capped, PromiseTerms(KIND_STARTING_ROLE, None, 90, 2))
        self.assertEqual(book.minutes_commitments(5)[1002], 180)

    def test_sel02_two_promises_to_one_player_reserve_the_same_minutes_in_either_ledger_order(self):
        """SEL 02 / spec 11.3: promised minutes are accumulated PER FIXTURE, so two open promises to one player with different
        fixture scopes reserve the same total whichever order the ledger returns them in (``list_promises`` orders by deadline),
        and neither scope caps the other away: under-reserving would hand the minutes planner capacity nobody promised."""
        one_match = PromiseTerms(KIND_STARTING_ROLE, None, 80, 1)
        three_matches = PromiseTerms(KIND_PLAYING_TIME, None, 30, 3)
        totals = []
        for first, second in ((one_match, three_matches), (three_matches, one_match)):
            _, book = ledger()
            for index, terms in enumerate((first, second)):
                # The deadline decides the ledger order; with no fixture dates it does not narrow either scope.
                promise, _ = promise_from_observed(1001, "player", "You will play", source=f"obs-{index}", source_choice=f"c{index}", deadline=f"2024-0{index + 3}-01", terms=terms)
                book.record(promise, terms)
            self.assertEqual([r.fixtures for r in book.minutes_reservations(3)], [first.fixtures, second.fixtures], "the ledger order is the one under test")
            totals.append(book.minutes_commitments(3)[1001])
        # Fixture 1 owes 80 + 30 capped at one match; fixtures 2 and 3 owe 30 each.
        self.assertEqual(totals, [MATCH_MINUTES + 30 + 30, MATCH_MINUTES + 30 + 30])
        self.assertGreaterEqual(totals[0], 30 * 3, "the three-fixture promise is never reserved away by the one-fixture promise")

    def test_combined_reservations_never_exceed_the_match_total(self):
        _, book = ledger()
        for index in range(2):
            p, _ = promise_from_observed(1001, "player", "You will start", source=f"obs-{index}", source_choice=f"c{index}", terms=PromiseTerms(KIND_STARTING_ROLE, None, 80, None))
            book.record(p, PromiseTerms(KIND_STARTING_ROLE, None, 80, None))
        self.assertEqual(book.minutes_commitments(2), {1001: MATCH_MINUTES * 2})


class LifecycleTests(unittest.TestCase):
    def test_progress_and_closure(self):
        store, book = ledger()
        promise, _ = promise_from_observed(1001, "player", "You will start", source="obs-d1", source_choice="c1")
        book.record(promise)
        book.update_progress(promise.promise_id, "obs-match-1", "started 1 of 1")
        self.assertEqual(book.get(promise.promise_id).progress, "started 1 of 1")
        kept = book.mark_kept(promise.promise_id, "obs-inbox-9")
        self.assertEqual(kept.status, STATUS_KEPT)
        self.assertEqual(book.open(), [])
        with self.assertRaises(PromiseError):
            book.mark_broken(promise.promise_id, "obs-inbox-10")
        kinds = [e["kind"] for e in store.journal_entries(ref_id=promise.promise_id)]
        self.assertEqual(kinds, ["promise", "promise_terms", "promise", "promise_progress", "promise", "promise_status"])

    def test_broken_keeps_consequences_only_when_observed(self):
        _, book = ledger()
        a, _ = promise_from_observed(1001, "player", "You will start", source="obs-d1", source_choice="c1")
        b, _ = promise_from_observed(1002, "player", "You will start", source="obs-d2", source_choice="c2")
        book.record(a)
        book.record(b)
        self.assertIsNone(book.mark_broken(a.promise_id, "obs-inbox-11").consequences)
        self.assertEqual(book.mark_broken(b.promise_id, "obs-inbox-12", "Player is unhappy and wants to leave").consequences, "Player is unhappy and wants to leave")
        self.assertEqual({p.status for p in book.for_party(1001, include_closed=True)}, {STATUS_BROKEN})

    def test_withdrawn_promise_is_closed_with_the_observation_that_released_the_club(self):
        """Spec 11.3: a counterparty releasing the club is its own recorded closure status, not a kept or broken promise."""
        store, book = ledger()
        promise, _ = promise_from_observed(1001, "player", "You will be a regular starter", source="obs-d1", source_choice="c1")
        book.record(promise)
        with self.assertRaises(PromiseError):
            book.withdraw(promise.promise_id, "")
        withdrawn = book.withdraw(promise.promise_id, "obs-inbox-14: player accepted a squad role instead")
        self.assertEqual(withdrawn.status, STATUS_WITHDRAWN)
        self.assertEqual(book.open(), [])
        self.assertEqual([p.status for p in book.for_party(1001, include_closed=True)], [STATUS_WITHDRAWN])
        [entry] = [e for e in store.journal_entries(kind="promise_status", ref_id=promise.promise_id)]
        self.assertEqual(entry["body"]["status"], STATUS_WITHDRAWN)
        self.assertIn("obs-inbox-14", entry["body"]["evidence"])
        with self.assertRaises(PromiseError):
            book.mark_kept(promise.promise_id, "obs-inbox-15")

    def test_expire_requires_evidence(self):
        _, book = ledger()
        promise, _ = promise_from_observed(1001, "player", "You will start", source="obs-d1", source_choice="c1", deadline="2024-03-01")
        book.record(promise)
        with self.assertRaises(PromiseError):
            book.expire(promise.promise_id, "")
        self.assertEqual(book.expire(promise.promise_id, "obs-date-passed").status, STATUS_EXPIRED)
        with self.assertRaises(PromiseError):
            book.expire("nope", "obs")


if __name__ == "__main__":
    unittest.main()
